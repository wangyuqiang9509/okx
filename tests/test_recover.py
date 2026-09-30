from __future__ import annotations

import asyncio
from decimal import Decimal as D

import pytest

from gridbot.ledger import Ledger
from gridbot.recover import RecoverError, recover
from gridbot.runner import Supervisor
from test_supervisor import INSTS, START, config, exchange, feed, nosleep, oscillate


def run(c):
    return asyncio.run(c)


def essentials(sup_or_engines):
    out = {}
    for inst, r in sup_or_engines.items():
        e = r.engine
        out[inst] = {
            "grid": e.grid_id, "cash": e.cash_quote, "base": e.base_held, "realised": e.realised_profit, "trips": e.round_trips,
            "levels": {i: (o.side, o.cl_ord_id, o.qty, o.price, o.basis_quote) for i, o in e.levels.items() if o is not None},
        }
    return out


def old_machine(tmp_path):
    ex, ledger = exchange(), Ledger(tmp_path / "old.sqlite")
    sup = Supervisor(config(tmp_path), None, ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        await oscillate(sup, ex, "BTC-USDT", "0.025", 2)
        await oscillate(sup, ex, "ETH-USDT", "0.05", 2)
        await oscillate(sup, ex, "SOL-USDT", "0.05", 1)
        await feed(sup, ex, "SOL-USDT", START["SOL-USDT"] * D("0.95"))  # ends mid-move with sells outstanding
    run(go())
    return ex, ledger, sup


def test_recover_rebuilds_the_exact_grids_and_pool(tmp_path):
    ex, old_ledger, old = old_machine(tmp_path)
    new_ledger = Ledger(tmp_path / "new.sqlite")
    results = run(recover(config(tmp_path), new_ledger, ex))
    assert [r.inst_id for r in results] == list(INSTS)
    assert essentials({r.inst_id: r for r in results}) == essentials(old.runners)
    assert {k: v for k, v in new_ledger.pool().items()} == {k: v for k, v in old_ledger.pool().items()}
    for r in results:
        assert r.reunplaced == [] and r.renamed == 0


def test_recovered_ledger_starts_and_reconciles_clean(tmp_path):
    ex, _, _ = old_machine(tmp_path)
    new_ledger = Ledger(tmp_path / "new.sqlite")
    run(recover(config(tmp_path), new_ledger, ex))
    sup = Supervisor(config(tmp_path), None, new_ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        d = await sup.reconcile("t")
        await oscillate(sup, ex, "ETH-USDT", "0.05", 1)  # and keeps trading
        return d, await sup.reconcile("t")

    d1, d2 = run(go())
    assert d1["problems"] == {} and d2["problems"] == {}
    assert len(new_ledger.open_grids()) == 3  # resumed, nothing new created


def test_fills_while_nobody_runs_are_recovered(tmp_path):
    ex, _, _ = old_machine(tmp_path)
    ex.move("BTC-USDT", START["BTC-USDT"] * D("0.97"))  # during the move between machines
    ex.move("ETH-USDT", START["ETH-USDT"] * D("1.05"))
    new_ledger = Ledger(tmp_path / "new.sqlite")
    run(recover(config(tmp_path), new_ledger, ex))
    sup = Supervisor(config(tmp_path), None, new_ledger, ex, sleep=nosleep)

    async def go():
        await sup.start_grids()
        return await sup.reconcile("t")

    d = run(go())
    assert d["problems"] == {}
    assert not any(r.halted for r in sup.runners.values())


def test_exchange_cancel_and_replacement_is_recovered(tmp_path):
    ex, old_ledger, old = old_machine(tmp_path)
    eth = old.runners["ETH-USDT"]
    victim = next(o for o in eth.engine.resting_orders() if o.side.value == "buy")
    run(old.route(ex.exchange_cancel(victim.cl_ord_id)))  # OKX dropped it; runner re-placed it
    new_ledger = Ledger(tmp_path / "new.sqlite")
    results = run(recover(config(tmp_path), new_ledger, ex))
    assert essentials({r.inst_id: r for r in results}) == essentials(old.runners)


def test_start_on_empty_ledger_refuses_when_grid_orders_are_live(tmp_path):
    ex, _, _ = old_machine(tmp_path)
    sup = Supervisor(config(tmp_path), None, Ledger(tmp_path / "new.sqlite"), ex, sleep=nosleep)
    with pytest.raises(SystemExit, match="recover"):
        run(sup.start_grids())


def test_recover_refuses_a_ledger_that_already_has_grids(tmp_path):
    ex, old_ledger, _ = old_machine(tmp_path)
    with pytest.raises(RecoverError):
        run(recover(config(tmp_path), old_ledger, ex))


def test_recover_with_nothing_on_exchange_is_a_no_op(tmp_path):
    ex = exchange()
    assert run(recover(config(tmp_path), Ledger(tmp_path / "n.sqlite"), ex)) == []
