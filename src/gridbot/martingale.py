"""Spot Martingale: pure ladder state and arithmetic, no I/O.

A Cycle opens with an Opening Buy of `base` quote, where base = cash / (1 + mult + ... + mult**adds)
so that the whole Ladder spends exactly the Book's cash. Each time price falls `step` below the last
Level, an Add buys mult times the previous size. One Take-Profit sell for the whole position rests at
average cost * (1 + tp); when it fills the Cycle ends, its profit stays in the Book (compounding) and
the next Cycle opens. With every Add used the Ladder is Stuck: it holds and waits for the Take-Profit.
See research/2026-10-03-martingale-refill.md and docs/adr/0005-martingale-for-small-capital.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from .okx import Instrument, OrderSnapshot

D = Decimal


@dataclass
class TrackedOrder:
    """An order this Book sent, with how much of its cumulative fill is already in the Book."""
    cl_ord_id: str
    role: str  # open | add | tp
    side: str
    px: D
    sz: D
    applied_sz: D = D(0)
    applied_quote: D = D(0)
    applied_fee: D = D(0)

    def to_dict(self) -> dict[str, str]:
        return {"cl_ord_id": self.cl_ord_id, "role": self.role, "side": self.side, "px": str(self.px), "sz": str(self.sz),
                "applied_sz": str(self.applied_sz), "applied_quote": str(self.applied_quote), "applied_fee": str(self.applied_fee)}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> TrackedOrder:
        return cls(d["cl_ord_id"], d["role"], d["side"], D(d["px"]), D(d["sz"]), D(d["applied_sz"]), D(d["applied_quote"]), D(d["applied_fee"]))


@dataclass
class LadderState:
    book_id: str
    inst_id: str
    base_ccy: str
    quote_ccy: str
    cash: D  # quote owned by the Book, including what rests in an Add
    qty: D  # base owned by the Book, including what rests in the Take-Profit
    cost: D  # quote paid for `qty` in the current Cycle (reduced pro rata by partial sells)
    capital_in: D
    base_quote: D = D(0)  # Opening Buy size of the current Cycle
    n_add: int = 0
    last_level: D = D(0)  # price of the last Level bought; the next Add rests `step` below it
    in_cycle: bool = False
    cycle_cash: D = D(0)  # Book equity when the Cycle opened, for its profit
    seq: int = 0
    cycles: int = 0
    realised: D = D(0)
    fees_quote: D = D(0)
    orders: dict[str, TrackedOrder] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"book_id": self.book_id, "inst_id": self.inst_id, "base_ccy": self.base_ccy, "quote_ccy": self.quote_ccy,
                "cash": str(self.cash), "qty": str(self.qty), "cost": str(self.cost), "capital_in": str(self.capital_in),
                "base_quote": str(self.base_quote), "n_add": self.n_add, "last_level": str(self.last_level),
                "in_cycle": self.in_cycle, "cycle_cash": str(self.cycle_cash), "seq": self.seq, "cycles": self.cycles,
                "realised": str(self.realised), "fees_quote": str(self.fees_quote),
                "orders": {k: o.to_dict() for k, o in self.orders.items()},
                # close_book() hands these back to the Account Pool
                "holdings": {self.base_ccy: str(self.qty)}}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LadderState:
        return cls(d["book_id"], d["inst_id"], d["base_ccy"], d["quote_ccy"], D(d["cash"]), D(d["qty"]), D(d["cost"]),
                   D(d["capital_in"]), D(d["base_quote"]), int(d["n_add"]), D(d["last_level"]), bool(d["in_cycle"]),
                   D(d["cycle_cash"]), int(d["seq"]), int(d["cycles"]), D(d["realised"]), D(d["fees_quote"]),
                   {k: TrackedOrder.from_dict(o) for k, o in d["orders"].items()})

    def role(self, role: str) -> TrackedOrder | None:
        return next((o for o in self.orders.values() if o.role == role), None)

    def avg_cost(self) -> D:
        return self.cost / self.qty if self.qty > 0 else D(0)


@dataclass(frozen=True)
class Params:
    step: D
    mult: D
    adds: int
    tp: D

    def ladder_units(self) -> D:
        return sum((self.mult ** k for k in range(self.adds + 1)), D(0))


def floor_to(x: D, unit: D) -> D:
    return (x / unit).to_integral_value(rounding=ROUND_FLOOR) * unit


def ceil_to(x: D, unit: D) -> D:
    return (x / unit).to_integral_value(rounding=ROUND_CEILING) * unit


FEE_PAD = D("1.002")  # keep this much headroom over notional so a buy never exceeds the Book's cash


def opening_size(s: LadderState, p: Params, inst: Instrument, ask: D, slip: D) -> tuple[D, D, D] | None:
    """(base_quote, IOC price, qty) for the Opening Buy, or None if it would be below the minimum."""
    base_quote = s.cash / p.ladder_units()
    px = ceil_to(ask * (1 + slip), inst.tick_sz)
    qty = floor_to(min(base_quote, s.cash / FEE_PAD) / px, inst.lot_sz)
    return None if qty < inst.min_sz else (base_quote, px, qty)


def next_add(s: LadderState, p: Params, inst: Instrument) -> tuple[D, D] | None:
    """(price, qty) of the next Add, or None when the Ladder is Stuck or the cash is too small."""
    if s.n_add >= p.adds:
        return None
    px = floor_to(s.last_level * (1 - p.step), inst.tick_sz)
    want = s.base_quote * p.mult ** (s.n_add + 1)
    qty = floor_to(min(want, s.cash / FEE_PAD) / px, inst.lot_sz)
    return None if qty < inst.min_sz else (px, qty)


def take_profit(s: LadderState, p: Params, inst: Instrument) -> tuple[D, D] | None:
    """(price, qty) of the Take-Profit for the whole position, or None if it is below the minimum."""
    qty = floor_to(s.qty, inst.lot_sz)
    if qty < inst.min_sz:
        return None
    return ceil_to(s.avg_cost() * (1 + p.tp), inst.tick_sz), qty


def apply_snapshot(s: LadderState, o: TrackedOrder, snap: OrderSnapshot, inst: Instrument) -> D:
    """Bring the Book up to `snap`'s cumulative fill. Returns the newly filled base quantity."""
    d_sz = snap.acc_fill_sz - o.applied_sz
    if d_sz <= 0:
        return D(0)
    total_quote = snap.acc_fill_sz * snap.avg_px
    d_quote = total_quote - o.applied_quote
    d_fee = snap.fee - o.applied_fee  # OKX fees are negative
    fee_base = d_fee if snap.fee_ccy == inst.base_ccy else D(0)
    fee_quote = d_fee if snap.fee_ccy == inst.quote_ccy else D(0)
    if o.side == "buy":
        s.qty += d_sz + fee_base
        s.cash -= d_quote - fee_quote
        s.cost += d_quote - fee_quote
    else:
        sold_cost = s.avg_cost() * d_sz
        s.qty -= d_sz - fee_base
        s.cash += d_quote + fee_quote
        s.cost -= sold_cost
    s.fees_quote += fee_quote + fee_base * snap.avg_px
    o.applied_sz, o.applied_quote, o.applied_fee = snap.acc_fill_sz, total_quote, snap.fee
    return d_sz


def equity(s: LadderState, last: D) -> D:
    return s.cash + s.qty * last
