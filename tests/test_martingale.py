from __future__ import annotations

import asyncio
import csv
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from fake_exchange import FakeExchange
from gridbot.config import MartingaleConfig, RuntimeConfig
from gridbot.ledger import Ledger
from gridbot.martingale_runner import MartingaleRunner

INST = "SOL-USDT"


async def nosleep(_: float) -> None:
    return None


def cfg(tmp: Path) -> MartingaleConfig:
    return MartingaleConfig(INST, None, D("0.01"), D("2"), 6, D("0.008"), D("0.001"), 5,
                            RuntimeConfig(tmp / "l.sqlite", tmp / "logs", 3600, 300), tmp / "martingale.toml")


def setup(tmp: Path, px: str = "100", usdt: str = "300") -> tuple[FakeExchange, Ledger, MartingaleRunner]:
    ex = FakeExchange(prices={INST: D(px)}, bal={"USDT": D(usdt)})
    ledger = Ledger(tmp / "l.sqlite")
    ledger.pool_set("USDT", D(usdt))
    return ex, ledger, MartingaleRunner(cfg(tmp), ledger, ex, sleep=nosleep)


def run(c):  # type: ignore[no-untyped-def]
    return asyncio.run(c)


async def move(ex: FakeExchange, r: MartingaleRunner, px: str) -> None:
    ex.move(INST, D(px))
    await r.tick()


def test_cycle_adds_take_profit_and_reopen(tmp_path):
    ex, ledger, r = setup(tmp_path)

    async def go():
        await r.ensure_book()
        await r.tick()
        s = r.s
        assert s.in_cycle and s.n_add == 0 and s.qty > 0
        assert {o.role for o in s.orders.values()} == {"add", "tp"}
        base_cost = s.cost
        assert base_cost == pytest.approx(D(300) / 127, rel=D("0.02"))
        assert await r.reconcile("t") == {}

        await move(ex, r, "98.9")  # one Level down
        assert s.n_add == 1
        assert s.cost == pytest.approx(base_cost * 3, rel=D("0.02"))
        tp = s.role("tp")
        assert tp is not None and tp.sz == pytest.approx(s.qty, abs=D("0.000001"))
        assert tp.px == pytest.approx(s.avg_cost() * D("1.008"), abs=D("0.01"))
        assert await r.reconcile("t") == {}

        await move(ex, r, str(tp.px))  # Take-Profit fills, next Cycle opens
        assert s.cycles == 1 and s.realised > 0 and s.in_cycle and s.n_add == 0
        assert await r.reconcile("t") == {}
        return s

    s = run(go())
    assert s.cash + s.qty * D("99.7") > D(300)


def test_stuck_ladder_holds_then_recovers(tmp_path):
    ex, ledger, r = setup(tmp_path)

    async def go():
        await r.ensure_book()
        await r.tick()
        px = D(100)
        for _ in range(8):  # more falls than there are Adds
            px *= D("0.99")
            await move(ex, r, str(px.quantize(D("0.01"))))
        s = r.s
        assert s.n_add == 6 and s.role("add") is None and s.role("tp") is not None
        assert s.cash < D("1")  # the whole Ladder is spent
        assert await r.reconcile("t") == {}
        await move(ex, r, str(s.role("tp").px))  # type: ignore[union-attr]
        assert s.cycles == 1 and s.realised > 0
        assert await r.reconcile("t") == {}

    run(go())


def test_restart_does_not_buy_twice(tmp_path):
    ex, ledger, r = setup(tmp_path)

    async def go():
        await r.ensure_book()
        await r.tick()
        r2 = MartingaleRunner(cfg(tmp_path), ledger, ex, sleep=nosleep)
        await r2.ensure_book()
        await r2.tick()
        await r2.tick()
        buys = [o for o in ex.orders.values() if o.side == "buy" and o.state == "filled"]
        assert len(buys) == 1
        assert r2.s.qty == r.s.qty and await r2.reconcile("t") == {}

    run(go())


def test_refuses_when_another_book_is_open(tmp_path):
    ex, ledger, r = setup(tmp_path)
    ledger.create_book("TDEAD", "trend", "x", {"quote_ccy": "USDT", "cash": "0", "holdings": {}}, {})
    with pytest.raises(SystemExit):
        run(r.ensure_book())


def test_matches_research_simulation_on_history(tmp_path):
    """Drive two months of SOL hourly candles through the runner; it must land close to the research model."""
    path = Path("data/candles/SOL-USDT-1H.csv")
    if not path.exists():
        pytest.skip("no candle data")
    rows = [(float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in csv.reader(path.open())][-1460:]
    sys.path.insert(0, "research")
    import martingale_refill_study as research  # noqa: PLC0415

    research.MAX_DEPOSITS = 1
    expected = research.run(rows, 0, len(rows), 0.01, 2.0, 6, 0.008)["mult"]

    ex, ledger, r = setup(tmp_path, px=str(rows[0][0]))

    async def go():
        await r.ensure_book()
        last = rows[0][0]
        for o, hi, lo, cl in rows:
            for px in ([o, hi, lo, cl] if cl < o else [o, lo, hi, cl]):
                n = max(1, int(abs(px / last - 1) / 0.002))  # walk in <= 0.2% steps, as a live market would
                for k in range(1, n + 1):
                    await move(ex, r, f"{last + (px - last) * k / n:.2f}")
                    await r.tick()
                last = px
        assert await r.reconcile("t") == {}
        return (r.s.cash + r.s.qty * D(str(rows[-1][3]))) / D(300)

    got = float(run(go()))
    assert got == pytest.approx(expected, abs=0.03)


def test_refuses_new_book_when_okx_has_unknown_ladder_orders(tmp_path):
    ex, ledger, r = setup(tmp_path)
    run(ex.place_orders(INST, [{"side": "buy", "ordType": "limit", "px": "90", "sz": "0.1", "clOrdId": "MDEADBEEFA000001"}]))
    with pytest.raises(SystemExit):
        run(r.ensure_book())


def test_network_error_after_add_fill_still_resizes_take_profit(tmp_path):
    ex, ledger, r = setup(tmp_path)
    real_cancel = ex.cancel_orders
    calls = {"n": 0}

    async def flaky_cancel(inst_id, ids):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("network down")
        return await real_cancel(inst_id, ids)

    ex.cancel_orders = flaky_cancel  # type: ignore[method-assign]

    async def go():
        await r.ensure_book()
        await r.tick()
        ex.move(INST, D("98.9"))
        with pytest.raises(OSError):
            await r.tick()
        assert r.s.n_add == 1  # the step was recorded with the fill
        await r.tick()
        tp = r.s.role("tp")
        assert tp is not None and tp.sz == pytest.approx(r.s.qty, abs=D("0.000001"))
        add = r.s.role("add")
        assert add is not None and add.px < D("98.9")  # the next Add is one Level further down, not a repeat
        assert await r.reconcile("t") == {}

    run(go())
