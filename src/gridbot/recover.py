"""Rebuild the Ledger from OKX alone, for a new machine that has only the code and the API key.

For every configured Instrument with resting grid orders on OKX:
  1. read resting orders and the last 7 days of filled/cancelled orders,
  2. recover the Grid's spec from its initial orders (anchor, spacing, quantity),
  3. replay the Seed Buy and every later fill or exchange cancel through the engine,
  4. require the rebuilt Levels to match the resting orders exactly.
The Account Pool is then set to whatever the account holds beyond the rebuilt grids.
Nothing is placed or cancelled on the exchange.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal as D
from typing import Protocol

from .config import Config, GridConfig
from .engine import Fill, GridEngine, GridProfitRealised, GridSpec, Order, Side
from .ledger import Ledger
from .okx import Balance, Instrument, OrderSnapshot

CL_RE = re.compile(r"^G([0-9A-F]{8})([BS])(\d{3})(\d{6})(?:R(\d+))?$")
POOL_TOL = D("-0.05")


class RecoverError(Exception):
    pass


class RecoverRest(Protocol):
    async def instrument(self, inst_id: str) -> Instrument: ...
    async def balances(self, *ccys: str) -> dict[str, Balance]: ...
    async def pending_orders(self, inst_id: str | None = None) -> list[OrderSnapshot]: ...
    async def orders_history(self, inst_id: str) -> list[OrderSnapshot]: ...


@dataclass(frozen=True)
class ClId:
    grid_id: str
    side: Side
    idx: int
    seq: int
    seed_attempt: int | None


def parse_cl(cl: str) -> ClId | None:
    m = CL_RE.match(cl)
    if not m:
        return None
    return ClId(m[1], Side.BUY if m[2] == "B" else Side.SELL, int(m[3]) - 500, int(m[4]), int(m[5]) if m[5] else None)


@dataclass
class Recovered:
    inst_id: str
    engine: GridEngine
    created_ms: int
    resting: int
    replayed_events: int
    reunplaced: list[str]
    renamed: int
    profits: list[tuple[GridProfitRealised, int]] = field(default_factory=list)
    orders: list[OrderSnapshot] = field(default_factory=list)


async def grid_orders_on_exchange(rest: RecoverRest, inst_id: str) -> list[OrderSnapshot]:
    return [o for o in await rest.pending_orders(inst_id) if parse_cl(o.cl_ord_id)]


def _find_anchor(inst: Instrument, g: GridConfig, initial: dict[str, OrderSnapshot]) -> D:
    probes = [(parse_cl(c), o.px) for c, o in initial.items()]
    ref = next(((p, px) for p, px in probes if p and p.idx == 1), None) or next((p, px) for p, px in probes if p)
    assert ref[0] is not None
    guess = (ref[1] / (1 + g.spacing) ** ref[0].idx / inst.tick_sz).to_integral_value() * inst.tick_sz
    for k in range(0, 400):
        for sign in (1, -1):
            anchor = guess + sign * k * inst.tick_sz
            spec = GridSpec(inst.inst_id, anchor, g.spacing, g.levels_below, g.levels_above, D(1), inst.tick_sz, inst.lot_sz, inst.min_sz)
            if all(p is not None and spec.price(p.idx) == px for p, px in probes):
                return anchor
    raise RecoverError(f"{inst.inst_id}: no anchor reproduces the initial order prices; was the grid created with the current config's spacing and levels?")


async def recover_grid(rest: RecoverRest, g: GridConfig) -> Recovered | None:
    inst = await rest.instrument(g.inst_id)
    pending = await grid_orders_on_exchange(rest, g.inst_id)
    if not pending:
        return None
    gids = {p.grid_id for o in pending if (p := parse_cl(o.cl_ord_id))}
    if len(gids) != 1:
        raise RecoverError(f"{g.inst_id}: resting orders belong to several grids {sorted(gids)}; cancel the stale ones first")
    gid = gids.pop()
    history = [o for o in await rest.orders_history(g.inst_id) if (p := parse_cl(o.cl_ord_id)) and p.grid_id == gid]
    everything = {o.cl_ord_id: o for o in history} | {o.cl_ord_id: o for o in pending}

    def attempt(o: OrderSnapshot) -> int:
        p = parse_cl(o.cl_ord_id)
        return (p.seed_attempt or 0) if p else 0

    seeds = sorted((o for o in history if attempt(o) > 0), key=attempt)
    if not seeds:
        raise RecoverError(f"{g.inst_id}: grid {gid} has no Seed Buy in the last 7 days of OKX history; recover needs the old ledger file instead")
    n_levels = g.levels_below + g.levels_above
    initial = {c: o for c, o in everything.items() if (p := parse_cl(c)) and p.seed_attempt is None and 2 <= p.seq <= 1 + n_levels}
    if len(initial) != n_levels:
        raise RecoverError(f"{g.inst_id}: found {len(initial)} of {n_levels} initial orders; config levels differ from the grid's?")
    buys = {o.sz for c, o in initial.items() if (p := parse_cl(c)) and p.side is Side.BUY}
    if len(buys) != 1:
        raise RecoverError(f"{g.inst_id}: initial buy sizes differ {buys}")
    qty = buys.pop()
    anchor = _find_anchor(inst, g, initial)
    spec = GridSpec(g.inst_id, anchor, g.spacing, g.levels_below, g.levels_above, qty, inst.tick_sz, inst.lot_sz, inst.min_sz)
    eng = GridEngine(spec, gid, inst.base_ccy, inst.quote_ccy)

    seed = eng.make_seed(anchor, g.capital_quote)
    if seeds[0].sz != seed.qty:
        raise RecoverError(f"{g.inst_id}: seed size {seeds[0].sz} does not match {seed.qty} from capital {g.capital_quote}; was capital_quote changed?")
    for s in seeds:
        if s.acc_fill_sz > 0:
            eng.on_fill(Fill(f"seed:{s.cl_ord_id}", seed.cl_ord_id, s.avg_px, s.acc_fill_sz, s.fee, s.fee_ccy, s.u_time_ms))
    placed = eng.initial_orders()
    if {a.order.cl_ord_id for a in placed} != set(initial):
        raise RecoverError(f"{g.inst_id}: rebuilt initial orders do not match OKX")

    renamed = 0

    def resolve(cl: str) -> Order | None:
        nonlocal renamed
        o = eng.find(cl)
        if o is not None:
            return o
        p = parse_cl(cl)
        cand = eng.levels.get(p.idx) if p else None
        if p and cand is not None and cand.side is p.side and cand.cl_ord_id not in everything:
            cand.cl_ord_id = cl  # the live runner numbered this counter-order differently
            renamed += 1
            return cand
        return None

    def is_event(o: OrderSnapshot) -> bool:
        p = parse_cl(o.cl_ord_id)
        return p is not None and p.seed_attempt is None and o.state in ("filled", "canceled", "mmp_canceled")

    events = [o for o in history if is_event(o)]

    def order_key(o: OrderSnapshot) -> tuple[int, int, D]:
        p = parse_cl(o.cl_ord_id)
        assert p is not None
        # same millisecond: buys fill top-down, sells bottom-up
        return (o.u_time_ms, 0 if p.side is Side.BUY else 1, -o.px if p.side is Side.BUY else o.px)

    profits: list[tuple[GridProfitRealised, int]] = []
    for ev in sorted(events, key=order_key):
        if resolve(ev.cl_ord_id) is None:
            raise RecoverError(f"{g.inst_id}: history order {ev.cl_ord_id} ({ev.state}) does not fit the rebuilt grid")
        acts = []
        if ev.acc_fill_sz > 0:
            acts += eng.on_order_done(ev.cl_ord_id, ev.acc_fill_sz, ev.avg_px, ev.fee, ev.fee_ccy, ev.u_time_ms)
        if ev.state in ("canceled", "mmp_canceled"):
            acts += eng.on_order_cancelled(ev.cl_ord_id)
        profits += [(a, ev.u_time_ms) for a in acts if isinstance(a, GridProfitRealised)]

    for o in pending:
        if resolve(o.cl_ord_id) is None:
            raise RecoverError(f"{g.inst_id}: resting order {o.cl_ord_id} does not fit the rebuilt grid")
        if o.acc_fill_sz > 0:
            eng.on_order_done(o.cl_ord_id, o.acc_fill_sz, o.avg_px, o.fee, o.fee_ccy, o.u_time_ms)

    on_exchange = {o.cl_ord_id for o in pending}
    reunplaced = [o.cl_ord_id for o in eng.resting_orders() if o.cl_ord_id not in on_exchange]
    eng.unplaced = set(reunplaced)  # the old runner never got these onto OKX; the new one will
    eng.seq = max([eng.seq] + [p.seq for c in everything if (p := parse_cl(c))])
    return Recovered(g.inst_id, eng, min(s.c_time_ms for s in seeds), len(on_exchange), len(events), reunplaced, renamed,
                     profits, list(everything.values()))


async def recover(cfg: Config, ledger: Ledger, rest: RecoverRest) -> list[Recovered]:
    if ledger.open_grids():
        raise RecoverError("the ledger already has open grids; recover is only for an empty ledger on a new machine")
    results = [r for g in cfg.grids if (r := await recover_grid(rest, g)) is not None]
    if not results:
        return []
    quote = results[0].engine.quote_ccy
    ccys = sorted({quote, *(r.engine.base_ccy for r in results)})
    bal = await rest.balances(*ccys)
    pool = {quote: bal[quote].total - sum((r.engine.cash_quote for r in results), D(0))}
    for r in results:
        pool[r.engine.base_ccy] = bal[r.engine.base_ccy].total - r.engine.base_held
    bad = {c: v for c, v in pool.items() if v < POOL_TOL}
    if bad:
        raise RecoverError(f"rebuilt grids hold more than the account has: {bad}")

    for r in results:
        e = r.engine
        ledger.create_grid(e.grid_id, str(cfg.path), e.spec.to_dict(), e.state_dict(), created_ms=r.created_ms)
        for o in r.orders:
            p = parse_cl(o.cl_ord_id)
            side = "seed" if p and p.seed_attempt else o.side
            ledger.order_placed(e.grid_id, {"cl_ord_id": o.cl_ord_id, "idx": p.idx if p else 0, "side": side, "price": str(o.px), "qty": str(o.sz)},
                                o.ord_id, o.state, ts_ms=o.u_time_ms or o.c_time_ms)
            if o.acc_fill_sz > 0:
                ledger.fill(e.grid_id, f"recovered:{o.cl_ord_id}", o.cl_ord_id, o.avg_px, o.acc_fill_sz, o.fee, o.fee_ccy, o.u_time_ms)
        for a, ts in r.profits:
            ledger.profit(e.grid_id, a.sell.cl_ord_id, a.sell.basis_cl_ord_id, a.profit, ts_ms=ts)
        ledger.event(e.grid_id, "recovered", {"resting": r.resting, "events": r.replayed_events, "unplaced": r.reunplaced, "renamed": r.renamed,
                                              "cash": str(e.cash_quote), "base": str(e.base_held), "realised": str(e.realised_profit)})
    for c, v in pool.items():
        ledger.pool_set(c, max(v, D(0)))
    return results


def summary(results: list[Recovered], ledger: Ledger) -> str:
    lines = []
    for r in results:
        e = r.engine
        lines.append(f"{r.inst_id}: grid {e.grid_id} recovered, {r.resting} resting orders matched, {r.replayed_events} past orders replayed, "
                     f"{e.round_trips} round trips, realised {e.realised_profit:+.4f}, cash {e.cash_quote:.4f} {e.quote_ccy}, base {e.base_held} {e.base_ccy}"
                     + (f", {len(r.reunplaced)} order(s) to place on start" if r.reunplaced else ""))
    lines.append("account pool: " + ", ".join(f"{v} {k}" for k, v in sorted(ledger.pool().items())))
    return "\n".join(lines)

