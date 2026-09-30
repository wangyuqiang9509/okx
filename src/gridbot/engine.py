"""Pure grid engine: no I/O, no clock, no exchange.

The engine owns the Grid state (one Order per Level at most), consumes Fill and
cancel events and emits order intents. The runner and the Replay fixture both
drive the same code; nothing here may import httpx, sqlite or asyncio.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Any

D = Decimal
ZERO = D(0)
ONE = D(1)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class EngineError(Exception):
    """Invariant violated. The runner must Halt on this."""


def quantize_down(x: D, step: D) -> D:
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_nearest(x: D, step: D) -> D:
    return (x / step).to_integral_value(rounding=ROUND_HALF_EVEN) * step


def new_grid_id() -> str:
    return secrets.token_hex(4).upper()


@dataclass(frozen=True)
class GridSpec:
    inst_id: str
    anchor: D
    spacing: D  # fraction between adjacent Levels, e.g. Decimal("0.01")
    levels_below: int
    levels_above: int
    qty: D  # base quantity bought at every buy Level
    tick_sz: D
    lot_sz: D
    min_sz: D

    def price(self, idx: int) -> D:
        return quantize_nearest(self.anchor * (ONE + self.spacing) ** idx, self.tick_sz)

    def indices(self) -> range:
        return range(-self.levels_below, self.levels_above + 1)

    @property
    def lower(self) -> D:
        return self.price(-self.levels_below)

    @property
    def upper(self) -> D:
        return self.price(self.levels_above)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "anchor": str(self.anchor),
            "spacing": str(self.spacing),
            "levels_below": self.levels_below,
            "levels_above": self.levels_above,
            "qty": str(self.qty),
            "tick_sz": str(self.tick_sz),
            "lot_sz": str(self.lot_sz),
            "min_sz": str(self.min_sz),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GridSpec:
        return cls(
            inst_id=d["inst_id"],
            anchor=D(d["anchor"]),
            spacing=D(d["spacing"]),
            levels_below=int(d["levels_below"]),
            levels_above=int(d["levels_above"]),
            qty=D(d["qty"]),
            tick_sz=D(d["tick_sz"]),
            lot_sz=D(d["lot_sz"]),
            min_sz=D(d["min_sz"]),
        )


def qty_for_capital(
    capital_quote: D,
    anchor: D,
    spacing: D,
    levels_below: int,
    levels_above: int,
    taker_fee: D,
    lot_sz: D,
    tick_sz: D,
) -> D:
    """Base quantity per Level so that all buy Levels plus the Seed Buy spend `capital_quote`."""
    probe = GridSpec("", anchor, spacing, levels_below, levels_above, ONE, tick_sz, lot_sz, ZERO)
    buy_notional = sum((probe.price(i) for i in range(-levels_below, 0)), ZERO)
    seed_notional = anchor * levels_above * (ONE + taker_fee)
    return quantize_down(capital_quote / (buy_notional + seed_notional), lot_sz)


@dataclass
class Order:
    cl_ord_id: str
    idx: int
    side: Side
    price: D
    qty: D
    basis_quote: D = ZERO  # sells: USDT paid for the base being sold
    basis_cl_ord_id: str = ""  # sells: the buy Order that produced the base
    filled_sz: D = ZERO
    quote_flow: D = ZERO  # buys: USDT spent; sells: USDT received net of fee
    base_flow: D = ZERO  # buys: base received net of fee; sells: base delivered
    fee_total: D = ZERO  # cumulative fee as OKX reports it (negative)
    trade_ids: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> D:
        return self.qty - self.filled_sz

    def to_dict(self) -> dict[str, Any]:
        return {
            "cl_ord_id": self.cl_ord_id,
            "idx": self.idx,
            "side": self.side.value,
            "price": str(self.price),
            "qty": str(self.qty),
            "basis_quote": str(self.basis_quote),
            "basis_cl_ord_id": self.basis_cl_ord_id,
            "filled_sz": str(self.filled_sz),
            "quote_flow": str(self.quote_flow),
            "base_flow": str(self.base_flow),
            "fee_total": str(self.fee_total),
            "trade_ids": list(self.trade_ids),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Order:
        return cls(
            cl_ord_id=d["cl_ord_id"],
            idx=int(d["idx"]),
            side=Side(d["side"]),
            price=D(d["price"]),
            qty=D(d["qty"]),
            basis_quote=D(d["basis_quote"]),
            basis_cl_ord_id=d["basis_cl_ord_id"],
            filled_sz=D(d["filled_sz"]),
            quote_flow=D(d["quote_flow"]),
            base_flow=D(d["base_flow"]),
            fee_total=D(d.get("fee_total", "0")),
            trade_ids=list(d["trade_ids"]),
        )


@dataclass(frozen=True)
class Fill:
    trade_id: str
    cl_ord_id: str
    price: D
    size: D
    fee: D  # as OKX reports it: negative number in fee_ccy
    fee_ccy: str
    ts_ms: int


@dataclass(frozen=True)
class PlaceOrder:
    order: Order


@dataclass(frozen=True)
class GridProfitRealised:
    sell: Order
    profit: D


Action = PlaceOrder | GridProfitRealised

SEED_IDX = 0


class GridEngine:
    def __init__(
        self,
        spec: GridSpec,
        grid_id: str,
        base_ccy: str = "BTC",
        quote_ccy: str = "USDT",
    ) -> None:
        self.spec = spec
        self.grid_id = grid_id
        self.base_ccy = base_ccy
        self.quote_ccy = quote_ccy
        self.levels: dict[int, Order | None] = {i: None for i in spec.indices()}
        self.seed: Order | None = None
        self.cash_quote = ZERO
        self.base_held = ZERO
        self.realised_profit = ZERO
        self.round_trips = 0
        self.seq = 0
        self.unplaced: set[str] = set()  # intents not yet on the exchange (Halt, errors)
        # Counter-orders whose Level is still held by an order that has filled on the exchange
        # but whose fill we have not processed yet (fills reported out of price order).
        self.deferred: list[Order] = []

    # ----- ids -------------------------------------------------------------
    def new_cl_ord_id(self, side: Side, idx: int) -> str:
        self.seq += 1
        tag = "B" if side is Side.BUY else "S"
        return f"G{self.grid_id}{tag}{idx + 500:03d}{self.seq:06d}"

    # ----- lookup ----------------------------------------------------------
    def find(self, cl_ord_id: str) -> Order | None:
        if self.seed is not None and self.seed.cl_ord_id == cl_ord_id:
            return self.seed
        for o in self.levels.values():
            if o is not None and o.cl_ord_id == cl_ord_id:
                return o
        return None

    def resting_orders(self) -> list[Order]:
        return [o for o in self.levels.values() if o is not None]

    # ----- start-up ----------------------------------------------------------
    def make_seed(self, price: D, capital_quote: D) -> Order:
        """The Seed Buy: base for every sell Level above the anchor. Level index 0, not a Level order."""
        if self.seed is not None:
            raise EngineError("seed already exists")
        self.cash_quote = capital_quote
        qty = quantize_down(self.spec.qty * self.spec.levels_above, self.spec.lot_sz)
        self.seed = Order(self.new_cl_ord_id(Side.BUY, SEED_IDX), SEED_IDX, Side.BUY, price, qty)
        return self.seed

    def initial_orders(self) -> list[PlaceOrder]:
        """Sells above the anchor funded by the Seed Buy, buys below. Call once the seed is done."""
        if self.seed is None:
            raise EngineError("no seed")
        if any(o is not None for o in self.levels.values()):
            raise EngineError("grid already populated")
        n_above = self.spec.levels_above
        actions: list[PlaceOrder] = []
        if n_above > 0:
            sell_qty = quantize_down(self.seed.base_flow / n_above, self.spec.lot_sz)
            if sell_qty < self.spec.min_sz:
                raise EngineError(f"seed base {self.seed.base_flow} too small for {n_above} sell levels")
            basis_each = self.seed.quote_flow / n_above
            for idx in range(1, n_above + 1):
                actions.append(self._must(self._new_order(idx, Side.SELL, sell_qty, basis_each, self.seed.cl_ord_id)))
        for idx in range(-self.spec.levels_below, 0):
            actions.append(self._must(self._new_order(idx, Side.BUY, self.spec.qty)))
        return actions

    @staticmethod
    def _must(p: PlaceOrder | None) -> PlaceOrder:
        if p is None:
            raise EngineError("initial level unexpectedly occupied")
        return p

    def _new_order(
        self, idx: int, side: Side, qty: D, basis_quote: D = ZERO, basis_id: str = ""
    ) -> PlaceOrder | None:
        """Occupy Level `idx` with a new order, or defer it if the Level is still held."""
        if idx not in self.levels:
            raise EngineError(f"level {idx} out of range")
        order = Order(
            self.new_cl_ord_id(side, idx), idx, side, self.spec.price(idx), qty, basis_quote, basis_id
        )
        if self.levels[idx] is not None:
            self.deferred.append(order)
            return None
        self.levels[idx] = order
        return PlaceOrder(order)

    def _release(self, idx: int) -> list[Action]:
        """Level `idx` just freed: hand it to the first order deferred for it, if any."""
        self.levels[idx] = None
        for i, o in enumerate(self.deferred):
            if o.idx == idx:
                del self.deferred[i]
                self.levels[idx] = o
                return [PlaceOrder(o)]
        return []

    # ----- events ----------------------------------------------------------
    def on_fill(self, fill: Fill) -> list[Action]:
        order = self.find(fill.cl_ord_id)
        if order is None:
            raise EngineError(f"fill for unknown order {fill.cl_ord_id}")
        if fill.trade_id in order.trade_ids:
            return []
        order.trade_ids.append(fill.trade_id)
        order.filled_sz += fill.size
        order.fee_total += fill.fee
        notional = fill.price * fill.size
        if order.side is Side.BUY:
            fee_base = fill.fee if fill.fee_ccy == self.base_ccy else ZERO
            fee_quote = fill.fee if fill.fee_ccy == self.quote_ccy else ZERO
            order.base_flow += fill.size + fee_base  # fee is negative
            order.quote_flow += notional - fee_quote
            self.base_held += fill.size + fee_base
            self.cash_quote -= notional - fee_quote
        else:
            fee_quote = fill.fee if fill.fee_ccy == self.quote_ccy else ZERO
            fee_base = fill.fee if fill.fee_ccy == self.base_ccy else ZERO
            order.base_flow += fill.size
            order.quote_flow += notional + fee_quote
            self.base_held -= fill.size - fee_base
            self.cash_quote += notional + fee_quote
        if order.filled_sz + self.spec.lot_sz > order.qty:
            return self._complete(order)
        return []

    def on_order_done(
        self, cl_ord_id: str, acc_fill_sz: D, avg_px: D, fee: D, fee_ccy: str, ts_ms: int
    ) -> list[Action]:
        """Catch-up from a REST order snapshot (missed WS pushes). Synthesises one Fill for the delta."""
        order = self.find(cl_ord_id)
        if order is None:
            return []  # already completed via an earlier fill or snapshot
        delta = acc_fill_sz - order.filled_sz
        if delta <= ZERO:
            return []
        # The snapshot's avgPx and fee cover the whole order; the delta gets the fee not yet seen.
        fee_delta = fee - order.fee_total
        fill = Fill(f"snap:{cl_ord_id}:{acc_fill_sz}", cl_ord_id, avg_px, delta, fee_delta, fee_ccy, ts_ms)
        return self.on_fill(fill)

    def on_order_cancelled(self, cl_ord_id: str) -> list[Action]:
        """Exchange dropped a resting order we did not cancel (post_only would have crossed, etc.)."""
        order = self.find(cl_ord_id)
        if order is None or order is self.seed:
            return []
        self.unplaced.discard(cl_ord_id)
        if order.remaining < self.spec.min_sz and order.filled_sz > ZERO:
            return self._complete(order)
        replacement = Order(
            self.new_cl_ord_id(order.side, order.idx),
            order.idx,
            order.side,
            order.price,
            order.remaining,
            order.basis_quote,
            order.basis_cl_ord_id,
            filled_sz=ZERO,
            quote_flow=order.quote_flow,
            base_flow=order.base_flow,
            fee_total=order.fee_total,
            trade_ids=list(order.trade_ids),
        )
        self.levels[order.idx] = replacement
        return [PlaceOrder(replacement)]

    def _complete(self, order: Order) -> list[Action]:
        self.unplaced.discard(order.cl_ord_id)
        if order is self.seed:
            return []
        actions: list[Action] = list(self._release(order.idx))
        if order.side is Side.BUY:
            sell_qty = quantize_down(order.base_flow, self.spec.lot_sz)
            if sell_qty < self.spec.min_sz:
                raise EngineError(f"buy {order.cl_ord_id} yielded {order.base_flow}, below min size")
            p = self._new_order(order.idx + 1, Side.SELL, sell_qty, order.quote_flow, order.cl_ord_id)
            return actions + ([p] if p else [])
        profit = order.quote_flow - order.basis_quote
        self.realised_profit += profit
        self.round_trips += 1
        actions.insert(0, GridProfitRealised(order, profit))
        if order.idx - 1 >= -self.spec.levels_below:
            p = self._new_order(order.idx - 1, Side.BUY, self.spec.qty)
            if p:
                actions.append(p)
        return actions

    # ----- derived -----------------------------------------------------------
    def equity(self, last_px: D) -> D:
        return self.cash_quote + self.base_held * last_px

    def in_range(self, px: D) -> bool:
        return self.spec.lower <= px <= self.spec.upper

    # ----- persistence -------------------------------------------------------
    def state_dict(self) -> dict[str, Any]:
        return {
            "grid_id": self.grid_id,
            "spec": self.spec.to_dict(),
            "base_ccy": self.base_ccy,
            "quote_ccy": self.quote_ccy,
            "levels": {str(i): (o.to_dict() if o else None) for i, o in self.levels.items()},
            "seed": self.seed.to_dict() if self.seed else None,
            "cash_quote": str(self.cash_quote),
            "base_held": str(self.base_held),
            "realised_profit": str(self.realised_profit),
            "round_trips": self.round_trips,
            "seq": self.seq,
            "unplaced": sorted(self.unplaced),
            "deferred": [o.to_dict() for o in self.deferred],
        }

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> GridEngine:
        eng = cls(GridSpec.from_dict(d["spec"]), d["grid_id"], d["base_ccy"], d["quote_ccy"])
        eng.levels = {int(k): (Order.from_dict(v) if v else None) for k, v in d["levels"].items()}
        eng.seed = Order.from_dict(d["seed"]) if d["seed"] else None
        eng.cash_quote = D(d["cash_quote"])
        eng.base_held = D(d["base_held"])
        eng.realised_profit = D(d["realised_profit"])
        eng.round_trips = int(d["round_trips"])
        eng.seq = int(d["seq"])
        eng.unplaced = set(d["unplaced"])
        eng.deferred = [Order.from_dict(o) for o in d.get("deferred", [])]
        return eng
