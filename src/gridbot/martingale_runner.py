"""Live Spot Martingale runner on one Instrument.

Every few seconds: read the Book's resting Add and Take-Profit from OKX and bring the Book up to their
fills. A filled Add moves the Ladder down one Level and replaces the Take-Profit; a filled Take-Profit
ends the Cycle and the next one opens with an IOC Opening Buy. Both resting orders are plain limit
orders, so one that is already crossed when placed fills at once at a better price.
Reconciliation checks balances == Account Pool + Book, like the other strategies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Protocol

from .config import MartingaleConfig
from .ledger import Ledger
from .martingale import (LadderState, Params, TrackedOrder, apply_snapshot, equity, next_add, opening_size,
                         take_profit)
from .okx import Balance, Instrument, OkxError, OrderSnapshot, PlaceResult, Ticker

D = Decimal
log = logging.getLogger("martingale")
KIND = "martingale"
QUOTE_TOL = D("0.05")
BASE_TOL_QUOTE = D("0.05")
FINAL = ("filled", "canceled", "mmp_canceled")


class MartRest(Protocol):
    async def instrument(self, inst_id: str) -> Instrument: ...
    async def ticker(self, inst_id: str) -> Ticker: ...
    async def balances(self, *ccys: str) -> dict[str, Balance]: ...
    async def place_orders(self, inst_id: str, orders: list[dict[str, str]]) -> list[PlaceResult]: ...
    async def cancel_orders(self, inst_id: str, cl_ord_ids: list[str]) -> list[PlaceResult]: ...
    async def order(self, inst_id: str, cl_ord_id: str) -> OrderSnapshot | None: ...
    async def pending_orders(self, inst_id: str | None = None) -> list[OrderSnapshot]: ...


class MartingaleRunner:
    def __init__(self, cfg: MartingaleConfig, ledger: Ledger, rest: MartRest,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.cfg = cfg
        self.p = Params(cfg.step, cfg.mult, cfg.adds, cfg.take_profit)
        self.ledger = ledger
        self.rest = rest
        self.clock = clock
        self.sleep = sleep
        self.s: LadderState
        self.inst: Instrument
        self.halted = False
        self.closed = False
        self.lock = asyncio.Lock()

    @property
    def tag(self) -> str:
        return f"[mart {self.s.book_id}]"

    # ----- book lifecycle ---------------------------------------------------------------
    async def ensure_book(self) -> None:
        self.inst = await self.rest.instrument(self.cfg.inst_id)
        if self.ledger.open_grids():
            raise SystemExit("grids are still open in the ledger; close them first (`gridbot -c config/validate.toml cancel-all`)")
        others = [r["kind"] for r in self.ledger.open_books() if r["kind"] != KIND]
        if others:
            raise SystemExit(f"a {others[0]} book is open; close it first (e.g. `gridbot -c config/trend.toml trend-close`)")
        row = self.ledger.open_book(KIND)
        if row is not None:
            self.s = LadderState.from_dict(json.loads(row["state_json"]))
            self.halted = row["status"] == "halted"
            log.info("%s resuming (%s): cash %s, %s %s, add %d/%d, cycles %d", self.tag, row["status"], self.s.cash,
                     self.s.qty, self.s.base_ccy, self.s.n_add, self.p.adds, self.s.cycles)
            return
        stray = [o.cl_ord_id for o in await self.rest.pending_orders() if o.cl_ord_id[:1] in ("G", "M")]
        if stray:
            raise SystemExit(f"OKX has live grid or martingale orders ({stray[0]}...) not in this ledger; bring the old"
                             " ledger over or cancel them first (DEPLOY.md)")
        q, b = self.inst.quote_ccy, self.inst.base_ccy
        pool = self.ledger.pool()
        if q not in pool:  # brand-new ledger: everything in the account is unowned
            bal = await self.rest.balances(q, b)
            for c in (q, b):
                self.ledger.pool_set(c, bal[c].total)
            pool = self.ledger.pool()
        take = pool.get(q, D(0)) if self.cfg.capital_quote is None else self.cfg.capital_quote
        if take > pool.get(q, D(0)):
            raise SystemExit(f"config asks for {take} {q} but the account pool holds {pool.get(q, D(0))}")
        book_id = "M" + secrets.token_hex(4).upper()
        self.s = LadderState(book_id, self.cfg.inst_id, b, q, cash=take, qty=D(0), cost=D(0), capital_in=take)
        self.ledger.create_book(book_id, KIND, str(self.cfg.path), self.s.to_dict(), {q: take})
        log.info("%s created on %s with %s %s; ladder step %s x%s, %d adds, take-profit %s", self.tag, self.cfg.inst_id,
                 take, q, self.p.step, self.p.mult, self.p.adds, self.p.tp)

    def save(self) -> None:
        self.ledger.save_book(self.s.book_id, self.s.to_dict())

    # ----- orders --------------------------------------------------------------------------
    async def _place(self, role: str, side: str, ord_type: str, px: D, qty: D) -> TrackedOrder | None:
        self.s.seq += 1
        cl = f"{self.s.book_id}{role[0].upper()}{self.s.seq:06d}"
        o = TrackedOrder(cl, role, side, px, qty)
        self.s.orders[cl] = o
        self.save()  # persist before sending, so a crash never loses track of a live order
        (res,) = await self.rest.place_orders(self.cfg.inst_id, [{"side": side, "ordType": ord_type, "px": str(px), "sz": str(qty), "clOrdId": cl}])
        self.ledger.order_placed(self.s.book_id, {"cl_ord_id": cl, "idx": self.s.n_add, "side": side, "price": str(px), "qty": str(qty)},
                                 res.ord_id, "live" if res.ok else "rejected")
        if not res.ok:
            del self.s.orders[cl]
            self.save()
            log.error("%s %s %s %s @ %s rejected: %s %s", self.tag, role, side, qty, px, res.code, res.msg)
            self.ledger.event(self.s.book_id, "order_rejected", {"cl_ord_id": cl, "code": res.code, "msg": res.msg})
            return None
        log.info("%s placed %s %s %s @ %s (%s)", self.tag, role, side, qty, px, cl)
        return o

    async def _sync(self, o: TrackedOrder, final_wait: bool = False) -> OrderSnapshot | None:
        """Apply `o`'s fills. Drop it from the Book once it is final. With final_wait, poll until final."""
        snap = await self.rest.order(self.cfg.inst_id, o.cl_ord_id)
        for _ in range(20 if final_wait else 0):
            if snap is not None and snap.state in FINAL:
                break
            await self.sleep(0.5)
            snap = await self.rest.order(self.cfg.inst_id, o.cl_ord_id)
        if snap is None:  # never reached OKX (crash between save and send)
            log.warning("%s %s %s not found on OKX; forgetting it", self.tag, o.role, o.cl_ord_id)
            del self.s.orders[o.cl_ord_id]
            self.save()
            return None
        got = apply_snapshot(self.s, o, snap, self.inst)
        if got:
            self.ledger.fill(self.s.book_id, f"mart:{o.cl_ord_id}:{snap.acc_fill_sz}", o.cl_ord_id, snap.avg_px, got,
                             snap.fee, snap.fee_ccy, snap.u_time_ms)
            log.info("%s %s %s filled %s (total %s/%s) @ avg %s; cash %.4f, %s %s, avg cost %s", self.tag, o.role, o.cl_ord_id,
                     got, snap.acc_fill_sz, o.sz, snap.avg_px, self.s.cash, self.s.qty, self.s.base_ccy, self.s.avg_cost().quantize(D("0.0001")))
        if snap.state in FINAL:
            self.ledger.order_state(o.cl_ord_id, snap.state, snap.ord_id)
            del self.s.orders[o.cl_ord_id]
            # move the Ladder in the same save that forgets the order, so a crash cannot lose the step
            if o.role == "add" and snap.state == "filled":
                self.s.n_add += 1
                self.s.last_level = o.px
                log.info("%s add %d/%d filled at level %s; avg cost now %s", self.tag, self.s.n_add, self.p.adds, o.px,
                         self.s.avg_cost().quantize(D("0.0001")))
            elif o.role == "open" and snap.acc_fill_sz > 0:
                self.s.in_cycle, self.s.last_level = True, snap.avg_px
                log.info("%s cycle %d opened at %s with %.4f %s", self.tag, self.s.cycles + 1, snap.avg_px, self.s.base_quote, self.s.quote_ccy)
        elif final_wait:
            raise OkxError("timeout", f"order {o.cl_ord_id} not final after 10s")
        self.save()
        return snap

    async def _cancel(self, o: TrackedOrder) -> None:
        await self.rest.cancel_orders(self.cfg.inst_id, [o.cl_ord_id])
        await self._sync(o, final_wait=True)

    async def _place_resting(self) -> None:
        """Make sure the Take-Profit (for the whole position) and the next Add are resting."""
        if self.s.role("tp") is None and (tp := take_profit(self.s, self.p, self.inst)) is not None:
            await self._place("tp", "sell", "limit", *tp)
        if self.s.role("add") is None and (add := next_add(self.s, self.p, self.inst)) is not None:
            await self._place("add", "buy", "limit", *add)

    # ----- cycle --------------------------------------------------------------------------
    async def _open_cycle(self) -> None:
        if (o := self.s.role("open")) is not None:  # crashed while an Opening Buy was in flight
            await self._sync(o, final_wait=True)
        if take_profit(self.s, self.p, self.inst) is not None:  # the Opening Buy filled before a crash
            self.s.in_cycle, self.s.last_level = True, self.s.avg_cost()
            self.save()
            log.warning("%s resuming a cycle whose opening buy filled before a restart", self.tag)
            await self._place_resting()
            return
        t = await self.rest.ticker(self.cfg.inst_id)
        size = opening_size(self.s, self.p, self.inst, t.ask, self.cfg.slippage)
        if size is None:
            log.error("%s cash %s is too small for an opening buy; halting", self.tag, self.s.cash)
            self.halt("cash below minimum order")
            return
        base_quote, px, qty = size
        self.s.cycle_cash = equity(self.s, t.last)
        self.s.base_quote, self.s.n_add = base_quote, 0
        o = await self._place("open", "buy", "ioc", px, qty)
        if o is None:
            return
        await self._sync(o, final_wait=True)
        if not self.s.in_cycle:
            log.warning("%s opening buy got no fill; retrying next tick", self.tag)
            return
        await self._place_resting()

    async def _close_cycle(self) -> None:
        if (add := self.s.role("add")) is not None:
            await self._cancel(add)
        last = (await self.rest.ticker(self.cfg.inst_id)).last
        profit = equity(self.s, last) - self.s.cycle_cash
        self.s.cycles += 1
        self.s.realised += profit
        self.s.in_cycle = False
        self.save()
        self.ledger.profit(self.s.book_id, "", "", profit)
        log.info("%s cycle %d closed after %d adds: profit %+.4f %s, cash %.4f, total realised %+.4f", self.tag, self.s.cycles,
                 self.s.n_add, profit, self.s.quote_ccy, self.s.cash, self.s.realised)

    async def step(self) -> None:
        """Bring the Book up to OKX and react: an Add filled, the Take-Profit filled, or no Cycle yet."""
        if not self.s.in_cycle:
            await self._open_cycle()
            return
        for o in list(self.s.orders.values()):
            await self._sync(o)
        want = take_profit(self.s, self.p, self.inst)
        tp = self.s.role("tp")
        if tp is None and want is None:  # sold out: the Cycle is over
            await self._close_cycle()
            await self._open_cycle()
            return
        if tp is not None and want is not None and want[1] > tp.sz - tp.applied_sz:  # an Add filled: resize the Take-Profit
            await self._cancel(tp)
        await self._place_resting()

    # ----- reconciliation -------------------------------------------------------------------
    async def reconcile(self, trigger: str) -> dict[str, str]:
        async with self.lock:
            for attempt in range(2):
                before = self.s.to_dict()
                for o in list(self.s.orders.values()):
                    await self._sync(o)
                q, b = self.s.quote_ccy, self.s.base_ccy
                bal = await self.rest.balances(q, b)
                for o in list(self.s.orders.values()):  # a fill that landed between the sync and the balance read
                    await self._sync(o)
                if self.s.to_dict() == before or attempt == 1:
                    break
            pool = self.ledger.pool()
            last = (await self.rest.ticker(self.cfg.inst_id)).last
            diffs = {q: bal[q].total - pool.get(q, D(0)) - self.s.cash, b: bal[b].total - pool.get(b, D(0)) - self.s.qty}
            problems = {c: str(d) for c, d in diffs.items() if (abs(d) if c == q else abs(d) * last) > (QUOTE_TOL if c == q else BASE_TOL_QUOTE)}
            detail = {"trigger": trigger, "ok": not problems, "diff": {c: str(d) for c, d in diffs.items()}, **({"problem": problems} if problems else {})}
            self.ledger.event(self.s.book_id, "reconcile", detail)
            if problems:
                log.error("%s reconcile (%s) mismatch %s", self.tag, trigger, problems)
                self.halt(f"reconcile:{problems}")
            else:
                log.info("%s reconcile (%s) ok: %s", self.tag, trigger, detail["diff"])
            return problems

    def halt(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.ledger.set_book_status(self.s.book_id, "halted", reason)
        log.error("%s HALT: %s (resting orders stay; `gridbot -c config/martingale.toml mart-resume` to continue)", self.tag, reason)

    def poll_status(self) -> None:
        status, reason = self.ledger.book_status(self.s.book_id)
        if status == "closed" and not self.closed:
            self.closed = True
            log.warning("%s closed (%s); idling", self.tag, reason)
        elif status == "halted" and not self.halted:
            self.halted = True
            log.error("%s halted: %s", self.tag, reason)
        elif status == "active" and self.halted:
            self.halted = False
            log.info("%s resumed", self.tag)

    async def snapshot(self) -> None:
        last = (await self.rest.ticker(self.cfg.inst_id)).last
        eq = equity(self.s, last)
        pnl = eq - self.s.capital_in
        self.ledger.snapshot(self.s.book_id, self.s.cash, self.s.qty, last, eq, pnl)
        tp = self.s.role("tp")
        add = self.s.role("add")
        log.info("%s px %s equity %.2f (in %.2f, pnl %+.2f) cash %.2f %s %s add %d/%d avg %s tp %s next add %s cycles %d%s",
                 self.tag, last, eq, self.s.capital_in, pnl, self.s.cash, self.s.qty, self.s.base_ccy, self.s.n_add, self.p.adds,
                 self.s.avg_cost().quantize(D("0.01")), tp.px if tp else "-", add.px if add else ("STUCK" if self.s.in_cycle else "-"),
                 self.s.cycles, " HALTED" if self.halted else "")

    # ----- main loop ----------------------------------------------------------------------------
    async def tick(self) -> None:
        """One pass of the control loop; the test suite drives this directly."""
        self.poll_status()
        if self.closed or self.halted:
            return
        async with self.lock:
            await self.step()

    async def run(self) -> None:
        await self.ensure_book()
        await self.reconcile("startup")
        last_rec = last_snap = self.clock()
        await self.snapshot()
        while True:
            try:
                await self.tick()
                now = self.clock()
                if not self.closed and now - last_rec >= self.cfg.runtime.reconcile_interval_s:
                    await self.reconcile("periodic")
                    last_rec = now
                if not self.closed and now - last_snap >= self.cfg.runtime.snapshot_interval_s:
                    await self.snapshot()
                    last_snap = now
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep running, the next tick retries
                log.exception("[mart] loop error: %s", e)
                self.ledger.event(self.s.book_id, "error", {"error": str(e)})
            await self.sleep(self.cfg.poll_interval_s)
