from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest

from fake_exchange import FakeExchange
from gridbot.config import Config, GridConfig, RuntimeConfig
from gridbot.ledger import Ledger
from gridbot.runner import Supervisor

INSTS = ("BTC-USDT", "ETH-USDT", "SOL-USDT")
START = {"BTC-USDT": D("83000"), "ETH-USDT": D("2700"), "SOL-USDT": D("120")}


async def nosleep(_: float) -> None:
    return None


def config(tmp: Path, insts=INSTS, capital="100") -> Config:
    spacing = {"BTC-USDT": "0.01", "ETH-USDT": "0.02", "SOL-USDT": "0.02"}
    grids = tuple(GridConfig(i, D(capital), D(spacing[i]), 8, 8, D("0.001")) for i in insts)
    return Config(grids, RuntimeConfig(tmp / "l.sqlite", tmp / "logs", 3600, 300), tmp / "multi.toml")


def exchange(usdt="350", btc="0.001") -> FakeExchange:
    return FakeExchange(prices=dict(START), bal={"USDT": D(usdt), "BTC": D(btc)})


def run(coro):
    return asyncio.run(coro)


async def feed(sup: Supervisor, ex: FakeExchange, inst: str, px: D) -> None:
    for u in ex.move(inst, px):
        await sup.route(u)


async def oscillate(sup: Supervisor, ex: FakeExchange, inst: str, pct: str, times: int = 3) -> None:
    p0 = START[inst]
    for _ in range(times):
        await feed(sup, ex, inst, p0 * (1 - D(pct)))
        await feed(sup, ex, inst, p0 * (1 + D(pct)))
    await feed(sup, ex, inst, p0)


def test_three_grids_created_with_pool_accounting(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")
    sup = Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        return await sup.reconcile("t")

    detail = run(go())
    assert detail["problems"] == {}
    assert [r["inst_id"] for r in ledger.open_grids()] == list(INSTS)
    pool = ledger.pool()
    assert pool["USDT"] == D("50") and pool["BTC"] == D("0.001")  # untouched funds stay outside the grids
    assert pool["ETH"] == 0 and pool["SOL"] == 0
    for inst in INSTS:
        assert len([o for o in ex.orders.values() if o.inst_id == inst and o.state == "live"]) == 16


def test_oscillation_on_all_three_reconciles_clean(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")
    sup = Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        await oscillate(sup, ex, "BTC-USDT", "0.025")
        await oscillate(sup, ex, "ETH-USDT", "0.05")
        await oscillate(sup, ex, "SOL-USDT", "0.05")
        return await sup.reconcile("t")

    detail = run(go())
    assert detail["problems"] == {}, detail
    for inst in INSTS:
        r = sup.runners[inst]
        assert r.engine.round_trips >= 8 and r.engine.realised_profit > 0
        assert not r.halted and not r.engine.unplaced and not r.engine.deferred


def test_restart_catches_up_fills_missed_while_offline(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")
    cfg = config(tmp_path)

    async def go():
        sup = Supervisor(cfg, None, ledger, ex, sleep=nosleep)
        await sup.start_grids()
        # process is down: the market moves, orders fill, nobody listens
        ex.move("ETH-USDT", D("2700") * D("0.95"))
        ex.move("SOL-USDT", D("120") * D("1.05"))
        sup2 = Supervisor(cfg, None, ledger, ex, sleep=nosleep)
        await sup2.start_grids()
        return sup2, await sup2.reconcile("t")

    sup2, detail = run(go())
    assert detail["problems"] == {}, detail
    assert not any(r.halted for r in sup2.runners.values())
    assert len(ledger.open_grids()) == 3  # resumed, not recreated
    eth = sup2.runners["ETH-USDT"]
    assert sum(1 for o in eth.engine.resting_orders() if o.side.value == "sell") > 8  # counter-sells placed after catch-up
    assert sup2.runners["SOL-USDT"].engine.round_trips >= 2


def test_base_mismatch_halts_only_that_grid(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")
    sup = Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        ex.bal["SOL"] += D("0.5")  # someone deposited SOL
        return await sup.reconcile("t")

    detail = run(go())
    assert set(detail["problems"]) == {"SOL-USDT"}
    assert sup.runners["SOL-USDT"].halted
    assert not sup.runners["BTC-USDT"].halted and not sup.runners["ETH-USDT"].halted


def test_quote_mismatch_halts_everything(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")
    sup = Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        ex.bal["USDT"] -= D("5")  # withdrawal
        return await sup.reconcile("t")

    detail = run(go())
    assert "*" in detail["problems"]
    assert all(r.halted for r in sup.runners.values())


def test_halted_grid_queues_counter_orders_and_places_them_on_resume(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")
    sup = Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        eth = sup.runners["ETH-USDT"]
        ledger.set_status(eth.grid_id, "halted", "manual")
        await sup.control()
        await feed(sup, ex, "ETH-USDT", D("2700") * D("0.97"))  # one buy fills while halted
        unplaced = set(eth.engine.unplaced)
        ledger.set_status(eth.grid_id, "active", "")
        await sup.control()
        return eth, unplaced, await sup.reconcile("t")

    eth, unplaced, detail = run(go())
    assert len(unplaced) == 1
    assert not eth.engine.unplaced
    assert detail["problems"] == {}


def test_closing_one_grid_releases_funds_and_others_keep_reconciling(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")
    sup = Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        await oscillate(sup, ex, "BTC-USDT", "0.025", 1)
        btc = sup.runners["BTC-USDT"]
        # what `gridbot cancel-all --inst BTC-USDT` does
        ids = [o.cl_ord_id for o in await ex.pending_orders("BTC-USDT")]
        await ex.cancel_orders("BTC-USDT", ids)
        ledger.close_grid(btc.grid_id, "manual_cancel_all")
        await sup.control()
        return await sup.reconcile("t")

    detail = run(go())
    assert detail["problems"] == {}, detail
    assert not sup.runners["ETH-USDT"].halted and not sup.runners["SOL-USDT"].halted
    assert sup.runners["BTC-USDT"].closed
    assert [r["inst_id"] for r in ledger.open_grids()] == ["ETH-USDT", "SOL-USDT"]
    assert ledger.pool()["USDT"] > D("90")  # 50 idle + the BTC grid's cash (about half its capital)
    assert ledger.pool()["BTC"] > D("0.001")  # plus the BTC it still held


def test_second_grid_added_later_takes_capital_from_pool(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")

    async def go():
        sup = Supervisor(config(tmp_path, ("BTC-USDT",)), None, ledger, ex, sleep=nosleep)
        await sup.start_grids()
        await oscillate(sup, ex, "BTC-USDT", "0.025", 1)
        sup2 = Supervisor(config(tmp_path, ("BTC-USDT", "ETH-USDT")), None, ledger, ex, sleep=nosleep)
        await sup2.start_grids()
        return await sup2.reconcile("t")

    detail = run(go())
    assert detail["problems"] == {}, detail
    assert ledger.pool()["USDT"] == D("150")


def test_not_enough_pool_refuses_to_create(tmp_path):
    ex, ledger = exchange(usdt="250"), Ledger(tmp_path / "l.sqlite")
    sup = Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep)
    with pytest.raises(SystemExit):
        run(sup.start_grids())
    assert len(ledger.open_grids()) == 2  # BTC and ETH created, SOL refused


def test_open_grid_missing_from_config_is_refused(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "l.sqlite")
    run(Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep).start_grids())
    with pytest.raises(SystemExit):
        run(Supervisor(config(tmp_path, ("BTC-USDT",)), None, ledger, ex, sleep=nosleep).start_grids())
