from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest

from fake_exchange import FakeExchange
from gridbot.config import RuntimeConfig, TrendConfig
from gridbot.ledger import Ledger
from gridbot.trend_runner import DAY_MS, TrendRunner

DAY0 = 1_790_000_000_000 // DAY_MS * DAY_MS  # a UTC midnight
INSTS = ("BTC-USDT", "ETH-USDT", "SOL-USDT")


async def nosleep(_: float) -> None:
    return None


class Clock:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> float:
        return self.ms / 1000


def series(start: float, daily: float, n: int = 260) -> list[D]:
    out, px = [], start
    for k in range(n):
        px *= 1 + daily + (0.01 if k % 2 else -0.01)  # a little noise so volatility is not zero
        out.append(D(str(round(px, 2))))
    return out


def market(day_start: int) -> FakeExchange:
    closes = {"BTC-USDT": series(60000, 0.002), "ETH-USDT": series(2500, 0.0), "SOL-USDT": series(200, -0.003)}
    ex = FakeExchange(prices={i: c[-1] for i, c in closes.items()}, bal={"USDT": D("300"), "SOL": D("0.5")})
    for i, c in closes.items():
        ex.daily[i] = [(day_start - (len(c) - k) * DAY_MS, px) for k, px in enumerate(c)]
    return ex


def cfg(tmp: Path) -> TrendConfig:
    return TrendConfig(INSTS, None, True, 0.40, D("0.05"), D("5"), D("0.001"), 5,
                       RuntimeConfig(tmp / "l.sqlite", tmp / "logs", 3600, 300), tmp / "trend.toml")


def setup(tmp: Path, minute: int = 10):
    ex = market(DAY0)
    ledger = Ledger(tmp / "l.sqlite")
    ledger.pool_set("USDT", D("300"))
    ledger.pool_set("SOL", D("0.5"))  # left behind by closed grids
    clock = Clock(DAY0 + minute * 60_000)
    return ex, ledger, clock, TrendRunner(cfg(tmp), ledger, ex, clock=clock, sleep=nosleep)


def run(c):
    return asyncio.run(c)


def test_book_adopts_pool_and_rebalances_to_targets(tmp_path):
    ex, ledger, clock, r = setup(tmp_path)

    async def go():
        await r.ensure_book()
        await r.tick()
        return await r.reconcile("t")

    problems = run(go())
    assert problems == {}
    b = r.book
    assert b.last_day and b.trades >= 2
    assert b.holdings.get("BTC", D(0)) > 0  # uptrend bought
    assert b.holdings.get("SOL", D(0)) == 0  # downtrend: the adopted SOL was sold completely
    assert ledger.pool().get("USDT") == 0 and ledger.pool().get("SOL") == 0
    decisions = ledger.decisions(b.book_id)
    assert len(decisions) == 3


def test_no_rebalance_before_the_close_and_only_once_a_day(tmp_path):
    ex, ledger, clock, r = setup(tmp_path, minute=2)

    async def go():
        await r.ensure_book()
        await r.tick()
        before = r.book.trades
        clock.ms = DAY0 + 10 * 60_000
        await r.tick()
        after_first = r.book.trades
        clock.ms += 3 * 3600_000
        await r.tick()
        return before, after_first, r.book.trades

    before, first, second = run(go())
    assert before == 0 and first > 0 and second == first


def test_stale_candles_postpone_the_rebalance(tmp_path):
    ex, ledger, clock, r = setup(tmp_path)
    ex.daily["ETH-USDT"] = ex.daily["ETH-USDT"][:-1]  # yesterday's candle missing

    async def go():
        await r.ensure_book()
        await r.tick()
        return r.book.trades, r.book.last_day

    trades, last_day = run(go())
    assert trades == 0 and last_day == ""


def test_balance_mismatch_halts_and_halt_blocks_trading(tmp_path):
    ex, ledger, clock, r = setup(tmp_path)

    async def go():
        await r.ensure_book()
        ex.bal["USDT"] -= D("10")  # withdrawal
        problems = await r.reconcile("t")
        await r.tick()
        return problems

    problems = run(go())
    assert "USDT" in problems and r.halted and r.book.trades == 0
    assert ledger.book_status(r.book.book_id)[0] == "halted"


def test_restart_resumes_the_book_without_trading_twice(tmp_path):
    ex, ledger, clock, r = setup(tmp_path)

    async def go():
        await r.ensure_book()
        await r.tick()
        r2 = TrendRunner(cfg(tmp_path), ledger, ex, clock=clock, sleep=nosleep)
        await r2.ensure_book()
        n = r2.book.trades
        await r2.tick()
        return r2, n

    r2, n = run(go())
    assert r2.book.book_id == r.book.book_id and r2.book.trades == n
    assert len(ledger.open_books()) == 1


def test_refuses_to_start_while_grids_are_open(tmp_path):
    from gridbot.engine import GridSpec
    ex, ledger, clock, r = setup(tmp_path)
    spec = GridSpec("BTC-USDT", D("80000"), D("0.01"), 1, 1, D("0.001"), D("0.1"), D("0.00000001"), D("0.00001"))
    ledger.create_grid("ABCDEF12", "x", spec.to_dict(), {})
    with pytest.raises(SystemExit, match="grids"):
        run(r.ensure_book())


def test_grids_refuse_to_start_while_a_book_is_active(tmp_path):
    from test_supervisor import config as grid_config
    from gridbot.runner import Supervisor
    ex, ledger, clock, r = setup(tmp_path)
    run(r.ensure_book())
    sup = Supervisor(grid_config(tmp_path), None, ledger, ex, sleep=nosleep)
    with pytest.raises(SystemExit, match="Trend Book"):
        run(sup.start_grids())


def test_close_hands_everything_back_to_the_pool(tmp_path):
    ex, ledger, clock, r = setup(tmp_path)

    async def go():
        await r.ensure_book()
        await r.tick()
    run(go())
    held = dict(r.book.holdings)
    cash = r.book.cash
    ledger.close_book(r.book.book_id, "manual")
    pool = ledger.pool()
    assert pool["USDT"] == cash
    for c, q in held.items():
        assert pool[c] == q
