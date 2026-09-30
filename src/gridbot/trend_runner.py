"""Live Trend Strategy runner.

Once a day, after the UTC daily candle closes, compute every Instrument's weight from the
closed daily candles and trade each Sleeve back to its target with limit IOC orders.
The Book (cash + coin holdings) is funded from the Account Pool; Reconciliation checks
balances == pool + Book, exactly like the grids (which must all be closed first).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Protocol

from .config import TrendConfig
from .ledger import Ledger
from .okx import Balance, Instrument, OkxError, OrderSnapshot, PlaceResult, Ticker
from .trend import Market, SleevePlan, plan, signal

D = Decimal
log = logging.getLogger("trend")
DAY_MS = 86_400_000
QUOTE_TOL = D("0.05")
BASE_TOL_QUOTE = D("0.05")
HISTORY_DAYS = 260
KIND = "trend"


class TrendRest(Protocol):
    async def instrument(self, inst_id: str) -> Instrument: ...
    async def ticker(self, inst_id: str) -> Ticker: ...
    async def balances(self, *ccys: str) -> dict[str, Balance]: ...
    async def place_orders(self, inst_id: str, orders: list[dict[str, str]]) -> list[PlaceResult]: ...
    async def order(self, inst_id: str, cl_ord_id: str) -> OrderSnapshot | None: ...
    async def pending_orders(self, inst_id: str | None = None) -> list[OrderSnapshot]: ...
    async def daily_closes(self, inst_id: str, n: int = 300) -> list[tuple[int, D]]: ...


class StaleData(Exception):
    pass


@dataclass
class BookState:
    book_id: str
    quote_ccy: str
    cash: D
    holdings: dict[str, D]
    capital_in: D  # Book equity at creation, in quote, for PnL
    last_day: str = ""
    seq: int = 0
    trades: int = 0
    fees_quote: D = D(0)

    def to_dict(self) -> dict[str, Any]:
        return {"book_id": self.book_id, "quote_ccy": self.quote_ccy, "cash": str(self.cash),
                "holdings": {k: str(v) for k, v in self.holdings.items()}, "capital_in": str(self.capital_in),
                "last_day": self.last_day, "seq": self.seq, "trades": self.trades, "fees_quote": str(self.fees_quote)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BookState:
        return cls(d["book_id"], d["quote_ccy"], D(d["cash"]), {k: D(v) for k, v in d["holdings"].items()}, D(d["capital_in"]),
                   d.get("last_day", ""), int(d.get("seq", 0)), int(d.get("trades", 0)), D(d.get("fees_quote", "0")))


def utc_day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


class TrendRunner:
    def __init__(self, cfg: TrendConfig, ledger: Ledger, rest: TrendRest,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.rest = rest
        self.clock = clock
        self.sleep = sleep
        self.book: BookState
        self.insts: dict[str, Instrument] = {}
        self.halted = False
        self.closed = False
        self.lock = asyncio.Lock()

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    # ----- book lifecycle ---------------------------------------------------------
    async def ensure_book(self) -> None:
        for i in self.cfg.inst_ids:
            self.insts[i] = await self.rest.instrument(i)
        if self.ledger.open_grids():
            raise SystemExit("grids are still open in the ledger; close them first (`gridbot -c config/validate.toml cancel-all`)")
        row = self.ledger.open_book(KIND)
        if row is not None:
            self.book = BookState.from_dict(json.loads(row["state_json"]))
            self.halted = row["status"] == "halted"
            log.info("[trend %s] resuming (%s): cash %s, holdings %s, last rebalance %s",
                     self.book.book_id, row["status"], self.book.cash, self.book.holdings, self.book.last_day or "never")
            return
        stray = [o.cl_ord_id for o in await self.rest.pending_orders() if o.cl_ord_id.startswith("G") and o.inst_id in self.cfg.inst_ids]
        if stray:
            raise SystemExit(f"OKX has live grid orders ({stray[0]}...) not in this ledger; recover or cancel them first (DEPLOY.md)")
        quote = self.cfg.quote_ccy
        bases = [self.insts[i].base_ccy for i in self.cfg.inst_ids]
        pool = self.ledger.pool()
        if quote not in pool:  # brand-new ledger: everything in the account is unowned
            bal = await self.rest.balances(quote, *bases)
            for c in (quote, *bases):
                self.ledger.pool_set(c, bal[c].total)
            pool = self.ledger.pool()
        take_q = pool.get(quote, D(0)) if self.cfg.capital_quote is None else self.cfg.capital_quote
        if take_q > pool.get(quote, D(0)):
            raise SystemExit(f"config asks for {take_q} {quote} but the account pool holds {pool.get(quote, D(0))}")
        take: dict[str, D] = {quote: take_q}
        holdings: dict[str, D] = {}
        if self.cfg.adopt_pool_coins:
            for b in bases:
                if pool.get(b, D(0)) > 0:
                    take[b] = holdings[b] = pool[b]
        prices = {self.insts[i].base_ccy: (await self.rest.ticker(i)).last for i in self.cfg.inst_ids}
        capital_in = take_q + sum((q * prices[b] for b, q in holdings.items()), D(0))
        book_id = "T" + secrets.token_hex(4).upper()
        self.book = BookState(book_id, quote, take_q, holdings, capital_in)
        self.ledger.create_book(book_id, KIND, str(self.cfg.path), self.book.to_dict(), take)
        log.info("[trend %s] created with %s %s and %s (worth %.2f %s)", book_id, take_q, quote, holdings or "no coins", capital_in, quote)

    def save(self) -> None:
        self.ledger.save_book(self.book.book_id, self.book.to_dict())

    # ----- daily rebalance ---------------------------------------------------------
    def due_day(self) -> str | None:
        """The UTC day whose rebalance is due now, or None."""
        now = self.now_ms()
        day_start = now // DAY_MS * DAY_MS
        if now - day_start < self.cfg.rebalance_minute_utc * 60_000:
            return None
        day = utc_day(now)
        return None if self.book.last_day == day else day

    async def rebalance(self, day: str) -> list[SleevePlan]:
        day_start = self.now_ms() // DAY_MS * DAY_MS
        weights: dict[str, float] = {}
        sig_detail: dict[str, dict[str, Any]] = {}
        for i in self.cfg.inst_ids:
            closes = await self.rest.daily_closes(i, HISTORY_DAYS)
            if not closes or closes[-1][0] != day_start - DAY_MS:
                last = utc_day(closes[-1][0]) if closes else "none"
                raise StaleData(f"{i}: last closed daily candle is {last}, expected {utc_day(day_start - DAY_MS)}")
            s = signal([float(c) for _, c in closes], self.cfg.target_vol)
            weights[i] = s.weight
            sig_detail[i] = {"ensemble": s.ensemble, "votes": s.votes, "vol": round(s.vol, 4), "weight": round(s.weight, 4),
                             "close": str(closes[-1][1])}
        markets = []
        for i in self.cfg.inst_ids:
            t = await self.rest.ticker(i)
            inst = self.insts[i]
            markets.append(Market(i, inst.base_ccy, t.last, inst.lot_sz, inst.min_sz))
        plans = plan(self.book.cash, self.book.holdings, markets, weights, self.cfg.band, self.cfg.min_trade_quote)
        for p in plans:
            self.ledger.decision(self.book.book_id, day, p.inst_id, sig_detail[p.inst_id] | {
                "target": str(p.target_value.quantize(D("0.01"))), "current": str(p.current_value.quantize(D("0.01"))),
                "trade": None if p.trade is None else {"side": p.trade.side, "qty": str(p.trade.qty), "notional": str(p.trade.notional.quantize(D("0.01")))},
                "reason": p.reason})
            log.info("[trend %s] %s %s weight %.3f (votes %s, vol %.0f%%) target %.2f current %.2f -> %s",
                     self.book.book_id, day, p.inst_id, weights[p.inst_id], "".join(map(str, sig_detail[p.inst_id]["votes"])),
                     sig_detail[p.inst_id]["vol"] * 100, p.target_value, p.current_value,
                     f"{p.trade.side} {p.trade.qty}" if p.trade else p.reason)
        for side in ("sell", "buy"):
            for p in plans:
                if p.trade and p.trade.side == side:
                    await self._execute(p)
        self.book.last_day = day
        self.save()
        return plans

    async def _execute(self, p: SleevePlan) -> None:
        assert p.trade is not None
        inst = self.insts[p.inst_id]
        t = await self.rest.ticker(p.inst_id)
        slip = self.cfg.slippage
        if p.trade.side == "buy":
            px = (t.ask * (1 + slip) / inst.tick_sz).to_integral_value(rounding=ROUND_CEILING) * inst.tick_sz
            if px * p.trade.qty * D("1.001") > self.book.cash:  # price moved since planning
                qty = (self.book.cash / D("1.001") / px / inst.lot_sz).to_integral_value(rounding=ROUND_FLOOR) * inst.lot_sz
            else:
                qty = p.trade.qty
        else:
            px = (t.bid * (1 - slip) / inst.tick_sz).to_integral_value(rounding=ROUND_FLOOR) * inst.tick_sz
            qty = min(p.trade.qty, (self.book.holdings.get(inst.base_ccy, D(0)) / inst.lot_sz).to_integral_value(rounding=ROUND_FLOOR) * inst.lot_sz)
        if qty < inst.min_sz:
            log.warning("[trend %s] %s %s skipped: quantity %s below minimum after re-pricing", self.book.book_id, p.inst_id, p.trade.side, qty)
            return
        self.book.seq += 1
        cl = f"{self.book.book_id}{'B' if p.trade.side == 'buy' else 'S'}{self.book.seq:06d}"
        self.save()  # persist seq before sending, so a crash never reuses an id
        (res,) = await self.rest.place_orders(p.inst_id, [{"side": p.trade.side, "ordType": "ioc", "px": str(px), "sz": str(qty), "clOrdId": cl}])
        rec = {"cl_ord_id": cl, "idx": 0, "side": p.trade.side, "price": str(px), "qty": str(qty)}
        self.ledger.order_placed(self.book.book_id, rec, res.ord_id, "live" if res.ok else "rejected")
        if not res.ok:
            log.error("[trend %s] %s %s %s rejected: %s %s", self.book.book_id, p.inst_id, p.trade.side, qty, res.code, res.msg)
            self.ledger.event(self.book.book_id, "order_rejected", {"cl_ord_id": cl, "code": res.code, "msg": res.msg})
            return
        snap = None
        for _ in range(20):
            await self.sleep(0.5)
            snap = await self.rest.order(p.inst_id, cl)
            if snap is not None and snap.state in ("filled", "canceled", "mmp_canceled"):
                break
        if snap is None or snap.state not in ("filled", "canceled", "mmp_canceled"):
            raise OkxError("timeout", f"IOC order {cl} not final after 10s")
        self.ledger.order_state(cl, snap.state, snap.ord_id)
        self._apply_fill(inst, snap)

    def _apply_fill(self, inst: Instrument, snap: OrderSnapshot) -> None:
        if snap.acc_fill_sz <= 0:
            log.warning("[trend %s] %s %s got no fill", self.book.book_id, inst.inst_id, snap.cl_ord_id)
            return
        b = inst.base_ccy
        notional = snap.acc_fill_sz * snap.avg_px
        fee_base = snap.fee if snap.fee_ccy == b else D(0)
        fee_quote = snap.fee if snap.fee_ccy == inst.quote_ccy else D(0)
        if snap.side == "buy":
            self.book.holdings[b] = self.book.holdings.get(b, D(0)) + snap.acc_fill_sz + fee_base
            self.book.cash -= notional - fee_quote
        else:
            self.book.holdings[b] = self.book.holdings.get(b, D(0)) - snap.acc_fill_sz + fee_base
            self.book.cash += notional + fee_quote
        self.book.trades += 1
        self.book.fees_quote -= fee_quote + fee_base * snap.avg_px
        self.ledger.fill(self.book.book_id, f"trend:{snap.cl_ord_id}", snap.cl_ord_id, snap.avg_px, snap.acc_fill_sz, snap.fee, snap.fee_ccy, snap.u_time_ms)
        self.save()
        log.info("[trend %s] %s %s %s @ %s fee %s %s; cash %s %s",
                 self.book.book_id, inst.inst_id, snap.side, snap.acc_fill_sz, snap.avg_px, snap.fee, snap.fee_ccy, self.book.cash.quantize(D("0.0001")), inst.quote_ccy)

    # ----- reconciliation ------------------------------------------------------------
    async def reconcile(self, trigger: str) -> dict[str, str]:
        async with self.lock:
            quote = self.book.quote_ccy
            bases = [self.insts[i].base_ccy for i in self.cfg.inst_ids]
            bal = await self.rest.balances(quote, *bases)
            pool = self.ledger.pool()
            problems: dict[str, str] = {}
            diffs: dict[str, str] = {}
            for c in (quote, *bases):
                exp = pool.get(c, D(0)) + (self.book.cash if c == quote else self.book.holdings.get(c, D(0)))
                diff = bal[c].total - exp
                diffs[c] = str(diff)
                if c == quote:
                    bad = abs(diff) > QUOTE_TOL
                else:
                    px = (await self.rest.ticker(next(i for i in self.cfg.inst_ids if self.insts[i].base_ccy == c))).last
                    bad = abs(diff) * px > BASE_TOL_QUOTE
                if bad:
                    problems[c] = str(diff)
            detail = {"trigger": trigger, "ok": not problems, "diff": diffs, **({"problem": problems} if problems else {})}
            self.ledger.event(self.book.book_id, "reconcile", detail)
            if problems:
                log.error("[trend %s] reconcile (%s) mismatch %s", self.book.book_id, trigger, problems)
                self.halt(f"reconcile:{problems}")
            else:
                log.info("[trend %s] reconcile (%s) ok: %s", self.book.book_id, trigger, diffs)
            return problems

    def halt(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.ledger.set_book_status(self.book.book_id, "halted", reason)
        log.error("[trend %s] HALT: %s (no rebalances until `gridbot -c config/trend.toml trend-resume`)", self.book.book_id, reason)

    def poll_status(self) -> None:
        status, reason = self.ledger.book_status(self.book.book_id)
        if status == "closed" and not self.closed:
            self.closed = True
            log.warning("[trend %s] closed (%s); idling", self.book.book_id, reason)
        elif status == "halted" and not self.halted:
            self.halted = True
            log.error("[trend %s] halted: %s", self.book.book_id, reason)
        elif status == "active" and self.halted:
            self.halted = False
            log.info("[trend %s] resumed", self.book.book_id)

    async def snapshot(self) -> None:
        value = self.book.cash
        for i in self.cfg.inst_ids:
            b = self.insts[i].base_ccy
            if self.book.holdings.get(b):
                value += self.book.holdings[b] * (await self.rest.ticker(i)).last
        pnl = value - self.book.capital_in
        self.ledger.snapshot(self.book.book_id, self.book.cash, D(0), D(0), value, pnl)
        log.info("[trend %s] equity %.2f %s (in %.2f, pnl %+.2f), cash %.2f, holdings %s%s", self.book.book_id, value, self.book.quote_ccy,
                 self.book.capital_in, pnl, self.book.cash, {k: str(v) for k, v in self.book.holdings.items()}, " HALTED" if self.halted else "")

    # ----- main loop ---------------------------------------------------------------------
    async def tick(self) -> None:
        """One pass of the control loop; the test suite drives this directly."""
        self.poll_status()
        if self.closed or self.halted:
            return
        day = self.due_day()
        if day is None:
            return
        async with self.lock:
            try:
                await self.rebalance(day)
            except StaleData as e:
                log.warning("[trend %s] rebalance for %s postponed: %s", self.book.book_id, day, e)
                return
        await self.reconcile("rebalance")

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
                log.exception("[trend] loop error: %s", e)
                self.ledger.event(self.book.book_id, "error", {"error": str(e)})
            await self.sleep(30)

