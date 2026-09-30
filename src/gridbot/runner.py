"""Live runner: drives the engine with OKX order pushes, places intents, reconciles hourly."""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal as D
from typing import Any

from .config import Config, Credentials
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
from .ledger import Ledger, now_ms
from .okx import Instrument, OkxError, OkxRest, OrderSnapshot, WsOrderUpdate, private_orders_stream

log = logging.getLogger("gridbot")

QUOTE_TOL = D("0.05")
BASE_TOL = D("0.0000002")
PLACE_RETRY_LIMIT = 12  # per order, within one control window


class Runner:
    def __init__(self, cfg: Config, creds: Credentials, ledger: Ledger, rest: OkxRest) -> None:
        self.cfg = cfg
        self.creds = creds
        self.ledger = ledger
        self.rest = rest
        self.lock = asyncio.Lock()
        self.halted = False
        self.closing = asyncio.Event()
        self.cancelled_by_us: set[str] = set()
        self.place_failures: dict[str, int] = {}
        self.inst: Instrument
        self.engine: GridEngine
        self.grid_id: str

    # ----- lifecycle --------------------------------------------------------------
    async def run(self) -> None:
        self.inst = await self.rest.instrument(self.cfg.grid.inst_id)
        row = self.ledger.open_grid()
        if row is not None:
            self.engine = GridEngine.from_state(json.loads(row["state_json"]))
            self.grid_id = self.engine.grid_id
            self.halted = row["status"] == "halted"
            log.info("resuming grid %s (%s) with %d resting orders", self.grid_id, row["status"], len(self.engine.resting_orders()))
            self.ledger.event(self.grid_id, "resume", {"status": row["status"]})
        else:
            await self._create_grid()
        tasks = [
            asyncio.create_task(self._ws_loop(), name="ws"),
            asyncio.create_task(self._periodic(self.cfg.runtime.reconcile_interval_s, self._reconcile_tick), name="reconcile"),
            asyncio.create_task(self._periodic(self.cfg.runtime.snapshot_interval_s, self._snapshot), name="snapshot"),
            asyncio.create_task(self._periodic(5, self._control), name="control"),
        ]
        try:
            await self.closing.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("grid %s closed, runner exiting", self.grid_id)

    async def _create_grid(self) -> None:
        g = self.cfg.grid
        fees = await self.rest.fees(g.inst_id)
        bal = await self.rest.balances(self.inst.base_ccy, self.inst.quote_ccy)
        quote_before, base_before = bal[self.inst.quote_ccy], bal[self.inst.base_ccy]
        if quote_before.avail < g.capital_quote:
            raise SystemExit(f"need {g.capital_quote} {self.inst.quote_ccy} available, have {quote_before.avail}")
        t = await self.rest.ticker(g.inst_id)
        anchor = t.last
        qty = qty_for_capital(g.capital_quote, anchor, g.spacing, g.levels_below, g.levels_above, -fees.taker, self.inst.lot_sz, self.inst.tick_sz)
        if qty < self.inst.min_sz:
            raise SystemExit(f"capital {g.capital_quote} gives {qty} per level, below min size {self.inst.min_sz}")
        spec = GridSpec(g.inst_id, anchor, g.spacing, g.levels_below, g.levels_above, qty, self.inst.tick_sz, self.inst.lot_sz, self.inst.min_sz)
        self.engine = GridEngine(spec, new_grid_id(), self.inst.base_ccy, self.inst.quote_ccy)
        self.grid_id = self.engine.grid_id
        log.info("creating grid %s: anchor %s range %s..%s qty/level %s fees maker %s taker %s", self.grid_id, anchor, spec.lower, spec.upper, qty, fees.maker, fees.taker)
        seed = self.engine.make_seed(anchor, g.capital_quote)
        self.ledger.create_grid(self.grid_id, str(self.cfg.path), spec.to_dict(), self.engine.state_dict(), quote_before.total - g.capital_quote, base_before.total)
        await self._seed_buy(seed)
        actions: list[Action] = list(self.engine.initial_orders())
        self.ledger.save_state(self.grid_id, self.engine.state_dict())
        await self._apply(actions)
        self.ledger.event(self.grid_id, "created", {"spec": spec.to_dict(), "seed_base": str(seed.base_flow), "seed_quote": str(seed.quote_flow)})

    async def _seed_buy(self, seed: Order) -> None:
        """Limit IOC at ask plus slippage cap, retried until the seed quantity is (nearly) filled."""
        remaining = seed.qty
        for attempt in range(1, 4):
            if remaining < self.inst.min_sz:
                break
            t = await self.rest.ticker(self.inst.inst_id)
            px = (t.ask * (1 + self.cfg.grid.seed_slippage) / self.inst.tick_sz).to_integral_value() * self.inst.tick_sz
            cl = f"{seed.cl_ord_id}R{attempt}"
            (res,) = await self.rest.place_orders(self.inst.inst_id, [{"side": "buy", "ordType": "ioc", "px": str(px), "sz": str(remaining), "clOrdId": cl}])
            self.ledger.order_placed(self.grid_id, {"cl_ord_id": cl, "idx": 0, "side": "seed", "price": str(px), "qty": str(remaining)}, res.ord_id, "live" if res.ok else "rejected")
            if not res.ok:
                raise SystemExit(f"seed buy rejected: {res.code} {res.msg}")
            snap = None
            for _ in range(20):
                await asyncio.sleep(0.5)
                snap = await self.rest.order(self.inst.inst_id, cl)
                if snap is not None and snap.state in ("filled", "canceled", "mmp_canceled"):
                    break
            if snap is None:
                raise SystemExit("seed buy vanished")
            self.ledger.order_state(cl, snap.state, snap.ord_id)
            if snap.acc_fill_sz > ZERO:
                fill = Fill(f"seed:{cl}", seed.cl_ord_id, snap.avg_px, snap.acc_fill_sz, snap.fee, snap.fee_ccy, snap.u_time_ms)
                self.ledger.fill(self.grid_id, fill.trade_id, cl, fill.price, fill.size, fill.fee, fill.fee_ccy, fill.ts_ms)
                self.engine.on_fill(fill)
                remaining -= snap.acc_fill_sz
            log.info("seed attempt %d: filled %s at %s, remaining %s", attempt, snap.acc_fill_sz, snap.avg_px, remaining)
        if seed.base_flow <= ZERO:
            raise SystemExit("seed buy got no fill; try again")
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
        if self.halted:
            for o in orders:
                self.engine.unplaced.add(o.cl_ord_id)
            log.warning("halted: %d order(s) left unplaced", len(orders))
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
                log.info("placed %s L%+d %s %s @ %s", o.cl_ord_id, o.idx, o.side.value, o.qty, p["px"])
            else:
                self.engine.unplaced.add(o.cl_ord_id)
                n = self.place_failures[o.cl_ord_id] = self.place_failures.get(o.cl_ord_id, 0) + 1
                self.ledger.order_placed(self.grid_id, rec, "", "rejected")
                self.ledger.event(self.grid_id, "place_failed", {"cl_ord_id": o.cl_ord_id, "code": r.code, "msg": r.msg, "attempt": n})
                log.warning("place failed %s L%+d: %s %s (attempt %d)", o.cl_ord_id, o.idx, r.code, r.msg, n)
                if n >= PLACE_RETRY_LIMIT:
                    await self._halt(f"place_retry_exhausted:{o.cl_ord_id}:{r.code}")

    async def _apply(self, actions: list[Action]) -> None:
        to_place: list[Order] = []
        for a in actions:
            if isinstance(a, GridProfitRealised):
                self.ledger.profit(self.grid_id, a.sell.cl_ord_id, a.sell.basis_cl_ord_id, a.profit)
                log.info("grid profit %+.4f USDT (L%+d sell %s vs buy %s); total %.4f over %d round trips", a.profit, a.sell.idx, a.sell.cl_ord_id, a.sell.basis_cl_ord_id, self.engine.realised_profit, self.engine.round_trips)
            elif isinstance(a, PlaceOrder):
                to_place.append(a.order)
        self.ledger.save_state(self.grid_id, self.engine.state_dict())
        await self._place(to_place)
        self.ledger.save_state(self.grid_id, self.engine.state_dict())

    # ----- websocket -------------------------------------------------------------------
    async def _ws_loop(self) -> None:
        async def on_connect() -> None:
            # every (re)connect: pick up fills that happened while no socket was open
            await self.reconcile("ws_connect")

        async for u in private_orders_stream(self.creds, self.inst.inst_id, on_connect=on_connect):
            try:
                await self._on_update(u)
            except EngineError as e:
                await self._halt(f"engine:{e}")
            except OkxError as e:
                log.error("okx error while handling update: %s", e)
                self.ledger.event(self.grid_id, "error", {"where": "ws_update", "error": str(e)})

    async def _on_update(self, u: WsOrderUpdate) -> None:
        snap = u.snapshot
        async with self.lock:
            order = self.engine.find(snap.cl_ord_id)
            if order is None:
                if not snap.cl_ord_id.startswith(f"G{self.grid_id}"):
                    log.warning("update for foreign order %s (%s)", snap.cl_ord_id or snap.ord_id, snap.state)
                    self.ledger.event(self.grid_id, "foreign_order", {"cl_ord_id": snap.cl_ord_id, "ord_id": snap.ord_id, "state": snap.state})
                return
            actions: list[Action] = []
            if u.fill is not None and self.ledger.fill(self.grid_id, u.fill.trade_id, u.fill.cl_ord_id, u.fill.px, u.fill.sz, u.fill.fee, u.fill.fee_ccy, u.fill.ts_ms):
                actions += self.engine.on_fill(Fill(u.fill.trade_id, u.fill.cl_ord_id, u.fill.px, u.fill.sz, u.fill.fee, u.fill.fee_ccy, u.fill.ts_ms))
                log.info("fill %s L%+d %s %s @ %s fee %s %s", snap.cl_ord_id, order.idx, order.side.value, u.fill.sz, u.fill.px, u.fill.fee, u.fill.fee_ccy)
            if snap.state == "filled":
                self.ledger.order_state(snap.cl_ord_id, "filled", snap.ord_id)
                actions += self._catch_up(snap)
            elif snap.state in ("canceled", "mmp_canceled"):
                self.ledger.order_state(snap.cl_ord_id, snap.state, snap.ord_id)
                if snap.cl_ord_id in self.cancelled_by_us:
                    self.cancelled_by_us.discard(snap.cl_ord_id)
                else:
                    log.warning("exchange cancelled %s L%+d (source %s); re-placing", snap.cl_ord_id, order.idx, snap.cancel_source)
                    self.ledger.event(self.grid_id, "exchange_cancel", {"cl_ord_id": snap.cl_ord_id, "source": snap.cancel_source})
                    actions += self.engine.on_order_cancelled(snap.cl_ord_id)
            elif snap.state in ("live", "partially_filled"):
                self.ledger.order_state(snap.cl_ord_id, snap.state, snap.ord_id)
            await self._apply(actions)

    def _catch_up(self, snap: OrderSnapshot) -> list[Action]:
        order = self.engine.find(snap.cl_ord_id)
        if order is None or snap.acc_fill_sz <= order.filled_sz:
            return []
        trade_id = f"snap:{snap.cl_ord_id}:{snap.acc_fill_sz}"
        self.ledger.fill(self.grid_id, trade_id, snap.cl_ord_id, snap.avg_px, snap.acc_fill_sz - order.filled_sz, snap.fee - order.fee_total, snap.fee_ccy, snap.u_time_ms)
        log.info("catch-up fill from snapshot for %s: %s of %s", snap.cl_ord_id, snap.acc_fill_sz, snap.sz)
        return self.engine.on_order_done(snap.cl_ord_id, snap.acc_fill_sz, snap.avg_px, snap.fee, snap.fee_ccy, snap.u_time_ms)

    # ----- reconciliation -----------------------------------------------------------------
    async def _reconcile_tick(self) -> None:
        await self.reconcile("periodic")

    async def reconcile(self, trigger: str) -> None:
        for attempt in (1, 2):
            try:
                ok, detail = await self._reconcile_once()
            except OkxError as e:
                log.error("reconcile (%s) failed to query OKX: %s", trigger, e)
                self.ledger.event(self.grid_id, "reconcile", {"trigger": trigger, "ok": False, "error": str(e)})
                return
            except EngineError as e:
                await self._halt(f"engine:{e}")
                return
            if ok or attempt == 2:
                break
            await asyncio.sleep(3)  # a fill may have landed between the two REST calls
        self.ledger.event(self.grid_id, "reconcile", {"trigger": trigger, "ok": ok, **detail})
        if ok:
            log.info("reconcile (%s) ok: %s", trigger, detail)
        else:
            await self._halt(f"reconcile:{detail.get('problem')}")

    async def _reconcile_once(self) -> tuple[bool, dict[str, Any]]:
        async with self.lock:
            bal = await self.rest.balances(self.inst.base_ccy, self.inst.quote_ccy)
            pending = {o.cl_ord_id: o for o in await self.rest.pending_orders(self.inst.inst_id)}
            actions: list[Action] = []
            missing: list[str] = []
            for o in self.engine.resting_orders():
                if o.cl_ord_id in pending or o.cl_ord_id in self.engine.unplaced:
                    continue
                snap = await self.rest.order(self.inst.inst_id, o.cl_ord_id)
                if snap is None:
                    self.engine.unplaced.add(o.cl_ord_id)
                    missing.append(o.cl_ord_id)
                elif snap.state == "filled":
                    self.ledger.order_state(snap.cl_ord_id, "filled", snap.ord_id)
                    actions += self._catch_up(snap)
                elif snap.state in ("canceled", "mmp_canceled"):
                    self.ledger.order_state(snap.cl_ord_id, snap.state, snap.ord_id)
                    self.cancelled_by_us.discard(snap.cl_ord_id)
                    actions += self.engine.on_order_cancelled(snap.cl_ord_id)
            foreign = [c for c in pending if self.engine.find(c) is None]
            row = self.ledger.grid(self.grid_id)
            assert row is not None
            exp_quote = D(row["quote_offset"]) + self.engine.cash_quote
            exp_base = D(row["base_offset"]) + self.engine.base_held
            dq = bal[self.inst.quote_ccy].total - exp_quote
            db = bal[self.inst.base_ccy].total - exp_base
            detail: dict[str, Any] = {
                "pending": len(pending), "resting": len(self.engine.resting_orders()), "unplaced": len(self.engine.unplaced),
                "caught_up": len(actions), "missing": missing, "foreign": foreign,
                "quote_diff": str(dq), "base_diff": str(db),
            }
            problem = None
            if foreign:
                problem = f"foreign_orders:{len(foreign)}"
            elif abs(dq) > QUOTE_TOL or abs(db) > BASE_TOL:
                problem = f"balance_mismatch:quote{dq}:base{db}"
            if problem:
                detail["problem"] = problem
            await self._apply(actions)
            return problem is None, detail

    # ----- control -------------------------------------------------------------------------
    async def _control(self) -> None:
        status, reason = self.ledger.status(self.grid_id)
        if status == "closed":
            self.closing.set()
            return
        if status == "halted" and not self.halted:
            self.halted = True
            log.error("halted by command: %s", reason)
        elif status == "active" and self.halted:
            self.halted = False
            log.info("resumed")
        if not self.halted and self.engine.unplaced:
            async with self.lock:
                orders = [o for c in sorted(self.engine.unplaced) if (o := self.engine.find(c)) is not None]
                await self._place(orders)
                self.ledger.save_state(self.grid_id, self.engine.state_dict())

    async def _halt(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.ledger.set_status(self.grid_id, "halted", reason)
        log.error("HALT: %s (resting orders stay on the exchange; run `gridbot resume` after fixing)", reason)

    async def _snapshot(self) -> None:
        t = await self.rest.ticker(self.inst.inst_id)
        e = self.engine
        eq = e.equity(t.last)
        self.ledger.snapshot(self.grid_id, e.cash_quote, e.base_held, t.last, eq, e.realised_profit)
        log.info("snapshot px %s equity %.4f cash %.4f base %s realised %.4f trips %d resting %d %s%s", t.last, eq, e.cash_quote, e.base_held, e.realised_profit, e.round_trips, len(e.resting_orders()), "HALTED " if self.halted else "", "" if e.in_range(t.last) else "OUT_OF_RANGE")

    async def _periodic(self, interval: int, fn: Any) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("periodic %s failed: %s", getattr(fn, "__name__", fn), e)
                self.ledger.event(self.grid_id, "error", {"where": getattr(fn, "__name__", "?"), "error": str(e)})
            await asyncio.sleep(interval)
