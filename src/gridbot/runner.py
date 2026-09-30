"""Live runner.

`Supervisor` owns everything shared: one REST client, one private WebSocket,
the Ledger, one lock, and account-level Reconciliation. Each `GridRunner`
owns one Grid on one Instrument: its engine, order placement and Halt.

Reconciliation model: every unit of every currency in the account belongs
either to exactly one open Grid (engine cash_quote / base_held) or to the
account pool (ledger.account_pool). The quote currency is shared by all
grids; each base currency belongs to one grid. A quote mismatch halts every
grid, a base mismatch halts only the grid trading that base.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal as D
from typing import Any, Protocol

from .config import Config, Credentials, GridConfig
from .engine import (
    ZERO,
    Action,
    EngineError,
    Fill,
    GridEngine,
    GridProfitRealised,
    GridSpec,
    Order,
    PlaceOrder,
    Side,
    new_grid_id,
    qty_for_capital,
)
from .ledger import Ledger
from .okx import (
    Balance,
    Fees,
    Instrument,
    OkxError,
    OrderSnapshot,
    PlaceResult,
    Ticker,
    WsOrderUpdate,
    private_orders_stream,
)

log = logging.getLogger("gridbot")

QUOTE_TOL = D("0.05")  # in quote currency
BASE_TOL_QUOTE = D("0.05")  # base tolerance expressed as quote value
PLACE_RETRY_LIMIT = 12


class Rest(Protocol):
    async def instrument(self, inst_id: str) -> Instrument: ...
    async def ticker(self, inst_id: str) -> Ticker: ...
    async def fees(self, inst_id: str) -> Fees: ...
    async def balances(self, *ccys: str) -> dict[str, Balance]: ...
    async def place_orders(self, inst_id: str, orders: list[dict[str, str]]) -> list[PlaceResult]: ...
    async def order(self, inst_id: str, cl_ord_id: str) -> OrderSnapshot | None: ...
    async def pending_orders(self, inst_id: str | None = None) -> list[OrderSnapshot]: ...


class GridRunner:
    def __init__(self, sup: Supervisor, gcfg: GridConfig) -> None:
        self.sup = sup
        self.gcfg = gcfg
        self.inst: Instrument
        self.engine: GridEngine
        self.grid_id = ""
        self.halted = False
        self.closed = False
        self.cancelled_by_us: set[str] = set()
        self.place_failures: dict[str, int] = {}

    @property
    def rest(self) -> Rest:
        return self.sup.rest

    @property
    def ledger(self) -> Ledger:
        return self.sup.ledger

    @property
    def tag(self) -> str:
        return f"[{self.gcfg.inst_id} {self.grid_id}]"

    # ----- lifecycle --------------------------------------------------------------
    async def load(self) -> bool:
        """Resume the open grid for this instrument. False if there is none."""
        self.inst = await self.rest.instrument(self.gcfg.inst_id)
        row = self.ledger.open_grid(self.gcfg.inst_id)
        if row is None:
            return False
        self.engine = GridEngine.from_state(json.loads(row["state_json"]))
        self.grid_id = self.engine.grid_id
        self.halted = row["status"] == "halted"
        log.info("%s resuming (%s) with %d resting orders", self.tag, row["status"], len(self.engine.resting_orders()))
        self.ledger.event(self.grid_id, "resume", {"status": row["status"]})
        return True

    async def create(self) -> None:
        g = self.gcfg
        inst = self.inst
        fees = await self.rest.fees(g.inst_id)
        bal = await self.rest.balances(inst.quote_ccy, inst.base_ccy)
        if bal[inst.quote_ccy].avail < g.capital_quote:
            raise SystemExit(f"{g.inst_id}: need {g.capital_quote} {inst.quote_ccy} available, have {bal[inst.quote_ccy].avail}")
        pool = self.ledger.pool()
        if inst.quote_ccy not in pool:
            # first grid ever: everything currently in the account is outside any grid
            self.ledger.pool_set(inst.quote_ccy, bal[inst.quote_ccy].total)
        if inst.base_ccy not in pool:
            self.ledger.pool_set(inst.base_ccy, bal[inst.base_ccy].total)
        pool = self.ledger.pool()
        if pool[inst.quote_ccy] < g.capital_quote:
            raise SystemExit(
                f"{g.inst_id}: account pool holds only {pool[inst.quote_ccy]} {inst.quote_ccy} not owned by another grid"
                " (deposited since? run `gridbot rebaseline`)"
            )
        t = await self.rest.ticker(g.inst_id)
        anchor = t.last
        qty = qty_for_capital(g.capital_quote, anchor, g.spacing, g.levels_below, g.levels_above, -fees.taker, inst.lot_sz, inst.tick_sz)
        if qty < inst.min_sz:
            raise SystemExit(f"{g.inst_id}: capital {g.capital_quote} gives {qty} per level, below min size {inst.min_sz}")
        spec = GridSpec(g.inst_id, anchor, g.spacing, g.levels_below, g.levels_above, qty, inst.tick_sz, inst.lot_sz, inst.min_sz)
        self.engine = GridEngine(spec, new_grid_id(), inst.base_ccy, inst.quote_ccy)
        self.grid_id = self.engine.grid_id
        log.info("%s creating: anchor %s range %s..%s qty/level %s fees maker %s taker %s",
                 self.tag, anchor, spec.lower, spec.upper, qty, fees.maker, fees.taker)
        seed = self.engine.make_seed(anchor, g.capital_quote)
        self.ledger.create_grid(self.grid_id, str(self.sup.cfg.path), spec.to_dict(), self.engine.state_dict())
        self.ledger.pool_add(inst.quote_ccy, -g.capital_quote)
        await self._seed_buy(seed)
        actions: list[Action] = list(self.engine.initial_orders())
        await self.apply(actions)
        self.ledger.event(self.grid_id, "created", {"spec": spec.to_dict(), "seed_base": str(seed.base_flow), "seed_quote": str(seed.quote_flow)})

    async def _seed_buy(self, seed: Order) -> None:
        """Limit IOC at ask plus slippage cap, retried until the seed quantity is (nearly) filled."""
        remaining = seed.qty
        for attempt in range(1, 4):
            if remaining < self.inst.min_sz:
                break
            t = await self.rest.ticker(self.inst.inst_id)
            px = (t.ask * (1 + self.gcfg.seed_slippage) / self.inst.tick_sz).to_integral_value() * self.inst.tick_sz
            cl = f"{seed.cl_ord_id}R{attempt}"
            (res,) = await self.rest.place_orders(self.inst.inst_id, [{"side": "buy", "ordType": "ioc", "px": str(px), "sz": str(remaining), "clOrdId": cl}])
            self.ledger.order_placed(self.grid_id, {"cl_ord_id": cl, "idx": 0, "side": "seed", "price": str(px), "qty": str(remaining)}, res.ord_id, "live" if res.ok else "rejected")
            if not res.ok:
                raise SystemExit(f"{self.inst.inst_id}: seed buy rejected: {res.code} {res.msg}")
            snap = None
            for _ in range(20):
                await self.sup.sleep(0.5)
                snap = await self.rest.order(self.inst.inst_id, cl)
                if snap is not None and snap.state in ("filled", "canceled", "mmp_canceled"):
                    break
            if snap is None:
                raise SystemExit(f"{self.inst.inst_id}: seed buy vanished")
            self.ledger.order_state(cl, snap.state, snap.ord_id)
            if snap.acc_fill_sz > ZERO:
                fill = Fill(f"seed:{cl}", seed.cl_ord_id, snap.avg_px, snap.acc_fill_sz, snap.fee, snap.fee_ccy, snap.u_time_ms)
                self.ledger.fill(self.grid_id, fill.trade_id, cl, fill.price, fill.size, fill.fee, fill.fee_ccy, fill.ts_ms)
                self.engine.on_fill(fill)
                remaining -= snap.acc_fill_sz
            log.info("%s seed attempt %d: filled %s at %s, remaining %s", self.tag, attempt, snap.acc_fill_sz, snap.avg_px, remaining)
        if seed.base_flow <= ZERO:
            raise SystemExit(f"{self.inst.inst_id}: seed buy got no fill; try again")
        self.ledger.save_state(self.grid_id, self.engine.state_dict())

    # ----- placing -------------------------------------------------------------------
    def _clamped_px(self, o: Order, bid: D, ask: D) -> D:
        tick = self.inst.tick_sz
        if o.side is Side.BUY:
            return min(o.price, ask - tick)
        return max(o.price, bid + tick)

    async def _place(self, orders: list[Order]) -> None:
        if not orders:
            return
        if self.halted or self.closed:
            for o in orders:
                self.engine.unplaced.add(o.cl_ord_id)
            log.warning("%s halted: %d order(s) left unplaced", self.tag, len(orders))
            return
        t = await self.rest.ticker(self.inst.inst_id)
        payload = []
        for o in orders:
            px = self._clamped_px(o, t.bid, t.ask)
            payload.append({"side": o.side.value, "ordType": "post_only", "px": str(px), "sz": str(o.qty), "clOrdId": o.cl_ord_id})
        results = await self.rest.place_orders(self.inst.inst_id, payload)
        for o, p, r in zip(orders, payload, results):
            rec = o.to_dict() | {"price": p["px"]}
            if r.ok:
                self.engine.unplaced.discard(o.cl_ord_id)
                self.place_failures.pop(o.cl_ord_id, None)
                self.ledger.order_placed(self.grid_id, rec, r.ord_id, "live")
                log.info("%s placed %s L%+d %s %s @ %s", self.tag, o.cl_ord_id, o.idx, o.side.value, o.qty, p["px"])
            else:
                self.engine.unplaced.add(o.cl_ord_id)
                n = self.place_failures[o.cl_ord_id] = self.place_failures.get(o.cl_ord_id, 0) + 1
                self.ledger.order_placed(self.grid_id, rec, "", "rejected")
                self.ledger.event(self.grid_id, "place_failed", {"cl_ord_id": o.cl_ord_id, "code": r.code, "msg": r.msg, "attempt": n})
                log.warning("%s place failed %s L%+d: %s %s (attempt %d)", self.tag, o.cl_ord_id, o.idx, r.code, r.msg, n)
                if n >= PLACE_RETRY_LIMIT:
                    self.halt(f"place_retry_exhausted:{o.cl_ord_id}:{r.code}")

    async def apply(self, actions: list[Action]) -> None:
        to_place: list[Order] = []
        for a in actions:
            if isinstance(a, GridProfitRealised):
                self.ledger.profit(self.grid_id, a.sell.cl_ord_id, a.sell.basis_cl_ord_id, a.profit)
                log.info("%s grid profit %+.4f %s (L%+d sell %s vs buy %s); total %.4f over %d round trips",
                         self.tag, a.profit, self.inst.quote_ccy, a.sell.idx, a.sell.cl_ord_id, a.sell.basis_cl_ord_id,
                         self.engine.realised_profit, self.engine.round_trips)
            elif isinstance(a, PlaceOrder):
                to_place.append(a.order)
        self.ledger.save_state(self.grid_id, self.engine.state_dict())
        await self._place(to_place)
        self.ledger.save_state(self.grid_id, self.engine.state_dict())

    # ----- events (caller holds the supervisor lock) ------------------------------------
    async def on_update(self, u: WsOrderUpdate) -> None:
        snap = u.snapshot
        order = self.engine.find(snap.cl_ord_id)
        if order is None:
            if not snap.cl_ord_id.startswith(f"G{self.grid_id}"):
                log.warning("%s update for foreign order %s (%s)", self.tag, snap.cl_ord_id or snap.ord_id, snap.state)
                self.ledger.event(self.grid_id, "foreign_order", {"cl_ord_id": snap.cl_ord_id, "ord_id": snap.ord_id, "state": snap.state})
            return
        actions: list[Action] = []
        f = u.fill
        if f is not None and self.ledger.fill(self.grid_id, f.trade_id, f.cl_ord_id, f.px, f.sz, f.fee, f.fee_ccy, f.ts_ms):
            actions += self.engine.on_fill(Fill(f.trade_id, f.cl_ord_id, f.px, f.sz, f.fee, f.fee_ccy, f.ts_ms))
            log.info("%s fill %s L%+d %s %s @ %s fee %s %s", self.tag, snap.cl_ord_id, order.idx, order.side.value, f.sz, f.px, f.fee, f.fee_ccy)
        if snap.state == "filled":
            self.ledger.order_state(snap.cl_ord_id, "filled", snap.ord_id)
            actions += self.catch_up(snap)
        elif snap.state in ("canceled", "mmp_canceled"):
            self.ledger.order_state(snap.cl_ord_id, snap.state, snap.ord_id)
            if snap.cl_ord_id in self.cancelled_by_us:
                self.cancelled_by_us.discard(snap.cl_ord_id)
            else:
                log.warning("%s exchange cancelled %s L%+d (source %s); re-placing", self.tag, snap.cl_ord_id, order.idx, snap.cancel_source)
                self.ledger.event(self.grid_id, "exchange_cancel", {"cl_ord_id": snap.cl_ord_id, "source": snap.cancel_source})
                actions += self.engine.on_order_cancelled(snap.cl_ord_id)
        elif snap.state in ("live", "partially_filled"):
            self.ledger.order_state(snap.cl_ord_id, snap.state, snap.ord_id)
        await self.apply(actions)

    def catch_up(self, snap: OrderSnapshot) -> list[Action]:
        order = self.engine.find(snap.cl_ord_id)
        if order is None or snap.acc_fill_sz <= order.filled_sz:
            return []
        trade_id = f"snap:{snap.cl_ord_id}:{snap.acc_fill_sz}"
        self.ledger.fill(self.grid_id, trade_id, snap.cl_ord_id, snap.avg_px, snap.acc_fill_sz - order.filled_sz,
                         snap.fee - order.fee_total, snap.fee_ccy, snap.u_time_ms)
        log.info("%s catch-up fill from snapshot for %s: %s of %s", self.tag, snap.cl_ord_id, snap.acc_fill_sz, snap.sz)
        return self.engine.on_order_done(snap.cl_ord_id, snap.acc_fill_sz, snap.avg_px, snap.fee, snap.fee_ccy, snap.u_time_ms)

    async def sync_orders(self, pending: dict[str, OrderSnapshot]) -> dict[str, Any]:
        """Bring engine orders in line with the exchange. `pending` = this instrument's resting orders."""
        actions: list[Action] = []
        missing: list[str] = []
        # Process as the market would have filled them: buys from the top down, then sells from
        # the bottom up. Out-of-order processing is still safe (the engine defers), just slower.
        resting = self.engine.resting_orders()
        ordered = sorted((o for o in resting if o.side is Side.BUY), key=lambda o: -o.price) + \
            sorted((o for o in resting if o.side is Side.SELL), key=lambda o: o.price)
        for o in ordered:
            if self.engine.find(o.cl_ord_id) is not o:
                continue  # replaced while processing an earlier order
            if o.cl_ord_id in pending or o.cl_ord_id in self.engine.unplaced:
                continue
            snap = await self.rest.order(self.inst.inst_id, o.cl_ord_id)
            if snap is None:
                self.engine.unplaced.add(o.cl_ord_id)
                missing.append(o.cl_ord_id)
            elif snap.state == "filled":
                self.ledger.order_state(snap.cl_ord_id, "filled", snap.ord_id)
                actions += self.catch_up(snap)
            elif snap.state in ("canceled", "mmp_canceled"):
                self.ledger.order_state(snap.cl_ord_id, snap.state, snap.ord_id)
                self.cancelled_by_us.discard(snap.cl_ord_id)
                actions += self.engine.on_order_cancelled(snap.cl_ord_id)
        foreign = [c for c in pending if self.engine.find(c) is None]
        await self.apply(actions)
        return {"pending": len(pending), "resting": len(self.engine.resting_orders()), "unplaced": len(self.engine.unplaced),
                "caught_up": len(actions), "missing": missing, "foreign": foreign}

    # ----- control ---------------------------------------------------------------------
    def poll_status(self) -> None:
        status, reason = self.ledger.status(self.grid_id)
        if status == "closed" and not self.closed:
            self.closed = True
            log.warning("%s closed (%s); runner stops managing it", self.tag, reason)
        elif status == "halted" and not self.halted:
            self.halted = True
            log.error("%s halted: %s", self.tag, reason)
        elif status == "active" and self.halted:
            self.halted = False
            log.info("%s resumed", self.tag)

    async def retry_unplaced(self) -> None:
        if self.halted or self.closed or not self.engine.unplaced:
            return
        orders = [o for c in sorted(self.engine.unplaced) if (o := self.engine.find(c)) is not None]
        await self._place(orders)
        self.ledger.save_state(self.grid_id, self.engine.state_dict())

    def halt(self, reason: str) -> None:
        if self.halted or self.closed:
            return
        self.halted = True
        self.ledger.set_status(self.grid_id, "halted", reason)
        log.error("%s HALT: %s (resting orders stay on the exchange; run `gridbot resume` after fixing)", self.tag, reason)

    async def snapshot(self) -> None:
        t = await self.rest.ticker(self.inst.inst_id)
        e = self.engine
        eq = e.equity(t.last)
        self.ledger.snapshot(self.grid_id, e.cash_quote, e.base_held, t.last, eq, e.realised_profit)
        log.info("%s snapshot px %s equity %.4f cash %.4f base %s realised %.4f trips %d resting %d %s%s",
                 self.tag, t.last, eq, e.cash_quote, e.base_held, e.realised_profit, e.round_trips, len(e.resting_orders()),
                 "HALTED " if self.halted else "", "" if e.in_range(t.last) else "OUT_OF_RANGE")


class Supervisor:
    def __init__(self, cfg: Config, creds: Credentials | None, ledger: Ledger, rest: Rest,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.cfg = cfg
        self.creds = creds
        self.ledger = ledger
        self.rest = rest
        self.sleep = sleep
        self.lock = asyncio.Lock()
        self.runners: dict[str, GridRunner] = {}
        self.stop = asyncio.Event()
        self.quote_ccy = cfg.grids[0].inst_id.split("-")[1]

    def live(self) -> list[GridRunner]:
        """Runners that own an open grid (a runner waiting to create its grid has none yet)."""
        return [r for r in self.runners.values() if r.grid_id and not r.closed]

    # ----- lifecycle --------------------------------------------------------------
    async def start_grids(self) -> None:
        """Resume every configured grid that is open, then create the missing ones one by one."""
        async with self.lock:
            to_create: list[GridRunner] = []
            for g in self.cfg.grids:
                r = GridRunner(self, g)
                self.runners[g.inst_id] = r
                if not await r.load():
                    to_create.append(r)
            unconfigured = [row["inst_id"] for row in self.ledger.open_grids() if row["inst_id"] not in self.runners]
            if unconfigured:
                raise SystemExit(f"open grid(s) on {unconfigured} are not in {self.cfg.path}; add them or run `gridbot cancel-all --inst ...`")
        # Existing grids must be caught up before new ones take money from the pool.
        if len(to_create) < len(self.runners):
            await self.reconcile("startup")
        for r in to_create:
            async with self.lock:
                await r.create()
        if to_create:
            await self.reconcile("created")

    async def run(self) -> None:
        await self.start_grids()
        tasks = [
            asyncio.create_task(self._ws_loop(), name="ws"),
            asyncio.create_task(self._periodic(self.cfg.runtime.reconcile_interval_s, self._reconcile_tick), name="reconcile"),
            asyncio.create_task(self._periodic(self.cfg.runtime.snapshot_interval_s, self.snapshot_all), name="snapshot"),
            asyncio.create_task(self._periodic(5, self.control), name="control"),
        ]
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("all grids closed, runner exiting")

    # ----- websocket -------------------------------------------------------------------
    async def _ws_loop(self) -> None:
        assert self.creds is not None

        async def on_connect() -> None:
            await self.reconcile("ws_connect")

        async for u in private_orders_stream(self.creds, list(self.runners), on_connect=on_connect):
            await self.route(u)

    async def route(self, u: WsOrderUpdate) -> None:
        r = self.runners.get(u.snapshot.inst_id)
        if r is None or r.closed:
            return
        async with self.lock:
            try:
                await r.on_update(u)
            except EngineError as e:
                r.halt(f"engine:{e}")
            except OkxError as e:
                log.error("%s okx error while handling update: %s", r.tag, e)
                self.ledger.event(r.grid_id, "error", {"where": "ws_update", "error": str(e)})

    # ----- reconciliation -----------------------------------------------------------------
    async def _reconcile_tick(self) -> None:
        await self.reconcile("periodic")

    async def reconcile(self, trigger: str) -> dict[str, Any]:
        detail: dict[str, Any] = {}
        for attempt in (1, 2):
            try:
                detail = await self._reconcile_once()
            except OkxError as e:
                log.error("reconcile (%s) failed to query OKX: %s", trigger, e)
                for r in self.live():
                    self.ledger.event(r.grid_id, "reconcile", {"trigger": trigger, "ok": False, "error": str(e)})
                return {"error": str(e)}
            if not detail["problems"] or attempt == 2:
                break
            await self.sleep(3)  # a fill may have landed between the REST calls
        problems: dict[str, str] = detail["problems"]
        for r in self.live():
            mine = problems.get(r.gcfg.inst_id) or problems.get("*")
            self.ledger.event(r.grid_id, "reconcile", {"trigger": trigger, "ok": mine is None, **({"problem": mine} if mine else {}), **detail})
            if mine:
                r.halt(f"reconcile:{mine}")
        if problems:
            log.error("reconcile (%s) problems: %s", trigger, problems)
        else:
            log.info("reconcile (%s) ok: %s", trigger, {k: v for k, v in detail.items() if k != "problems"})
        return detail

    async def _reconcile_once(self) -> dict[str, Any]:
        async with self.lock:
            live = self.live()
            pending_all = await self.rest.pending_orders()
            by_inst: dict[str, dict[str, OrderSnapshot]] = {}
            for o in pending_all:
                by_inst.setdefault(o.inst_id, {})[o.cl_ord_id] = o
            problems: dict[str, str] = {}
            orders: dict[str, Any] = {}
            for r in live:
                try:
                    d = await r.sync_orders(by_inst.get(r.gcfg.inst_id, {}))
                except EngineError as e:
                    r.halt(f"engine:{e}")
                    continue
                orders[r.gcfg.inst_id] = d
                if d["foreign"]:
                    problems[r.gcfg.inst_id] = f"foreign_orders:{len(d['foreign'])}"
            # balances: re-read open grids from the ledger, a CLI may have closed one meanwhile
            for r in live:
                r.poll_status()
            live = self.live()
            ccys = sorted({self.quote_ccy, *(r.inst.base_ccy for r in live)})
            bal = await self.rest.balances(*ccys)
            pool = self.ledger.pool()
            balances: dict[str, Any] = {}
            exp_q = pool.get(self.quote_ccy, ZERO) + sum((r.engine.cash_quote for r in live), ZERO)
            dq = bal[self.quote_ccy].total - exp_q
            balances[self.quote_ccy] = str(dq)
            if abs(dq) > QUOTE_TOL:
                problems["*"] = f"{self.quote_ccy}_mismatch:{dq}"
            for r in live:
                ccy = r.inst.base_ccy
                exp_b = pool.get(ccy, ZERO) + r.engine.base_held
                db = bal[ccy].total - exp_b
                balances[ccy] = str(db)
                if abs(db) * r.engine.spec.anchor > BASE_TOL_QUOTE and r.gcfg.inst_id not in problems:
                    problems[r.gcfg.inst_id] = f"{ccy}_mismatch:{db}"
            return {"orders": orders, "balance_diff": balances, "problems": problems}

    # ----- periodic ----------------------------------------------------------------------
    async def control(self) -> None:
        async with self.lock:
            for r in list(self.live()):
                r.poll_status()
                await r.retry_unplaced()
        if not self.live():
            self.stop.set()

    async def snapshot_all(self) -> None:
        for r in self.live():
            await r.snapshot()

    async def _periodic(self, interval: int, fn: Callable[[], Awaitable[Any]]) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                name = getattr(fn, "__name__", "?")
                log.exception("periodic %s failed: %s", name, e)
                for r in self.live():
                    self.ledger.event(r.grid_id, "error", {"where": name, "error": str(e)})
            await self.sleep(interval)
