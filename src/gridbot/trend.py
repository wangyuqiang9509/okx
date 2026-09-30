"""Trend Strategy: pure signal and rebalance planning, shared by backtest and live runner.

Signal for one Instrument from its daily UTC closes, last element = most recent closed day:
  ensemble = mean of six binary votes: close > SMA50/100/200, close > close 30/90/180 days ago
  vol      = population stdev of the last 30 daily log returns, annualised with sqrt(365)
  weight   = ensemble * min(1, target_vol / vol)
The weight is the share of the Instrument's Sleeve (Book equity / number of Instruments) to hold
in the coin; the rest stays in USDT. See research/2026-09-30-trend-and-range.md.
"""
from __future__ import annotations

import math
import statistics as st
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

D = Decimal

SMA_LENGTHS = (50, 100, 200)
MOM_LENGTHS = (30, 90, 180)
VOL_WINDOW = 30
MIN_HISTORY = max(max(SMA_LENGTHS), max(MOM_LENGTHS) + 1, VOL_WINDOW + 1)


@dataclass(frozen=True)
class TrendSignal:
    ensemble: float
    vol: float
    weight: float
    votes: tuple[int, ...]  # sma50, sma100, sma200, mom30, mom90, mom180


def signal(closes: Sequence[float], target_vol: float) -> TrendSignal:
    if len(closes) < MIN_HISTORY:
        raise ValueError(f"need {MIN_HISTORY} daily closes, got {len(closes)}")
    c = closes[-1]
    votes = [1 if c > sum(closes[-n:]) / n else 0 for n in SMA_LENGTHS]
    votes += [1 if c > closes[-1 - n] else 0 for n in MOM_LENGTHS]
    ens = sum(votes) / len(votes)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - VOL_WINDOW, len(closes))]
    vol = st.pstdev(rets) * math.sqrt(365)
    weight = ens * min(1.0, target_vol / vol) if vol > 0 else ens
    return TrendSignal(ens, vol, weight, tuple(votes))


@dataclass(frozen=True)
class Market:
    inst_id: str
    base_ccy: str
    price: D
    lot_sz: D
    min_sz: D


@dataclass(frozen=True)
class Trade:
    inst_id: str
    side: str  # "buy" | "sell"
    qty: D
    notional: D  # at planning price


@dataclass(frozen=True)
class SleevePlan:
    inst_id: str
    weight: float
    target_value: D
    current_value: D
    trade: Trade | None
    reason: str


def _qdown(x: D, step: D) -> D:
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def plan(
    cash: D,
    holdings: dict[str, D],  # base ccy -> qty
    markets: Sequence[Market],
    weights: dict[str, float],  # inst_id -> weight
    band: D,  # fraction of the sleeve a position may drift before trading
    min_trade: D,  # quote
    fee: D = D("0.001"),
) -> list[SleevePlan]:
    """Trades that bring every Sleeve to its target. Sells first; buys scaled to the cash available."""
    equity = cash + sum((holdings.get(m.base_ccy, D(0)) * m.price for m in markets), D(0))
    sleeve = equity / len(markets)
    out: list[SleevePlan] = []
    for m in markets:
        w = D(str(round(weights[m.inst_id], 6)))
        target = sleeve * w
        current = holdings.get(m.base_ccy, D(0)) * m.price
        delta = target - current
        if abs(delta) < max(band * sleeve, min_trade):
            out.append(SleevePlan(m.inst_id, weights[m.inst_id], target, current, None, "within band"))
            continue
        if delta < 0:
            qty = _qdown(-delta / m.price, m.lot_sz)
            if w == 0:  # exit completely, leave no dust behind
                qty = _qdown(holdings.get(m.base_ccy, D(0)), m.lot_sz)
            side = "sell"
        else:
            qty = _qdown(delta / m.price, m.lot_sz)
            side = "buy"
        if qty < m.min_sz:
            out.append(SleevePlan(m.inst_id, weights[m.inst_id], target, current, None, "below exchange minimum"))
            continue
        out.append(SleevePlan(m.inst_id, weights[m.inst_id], target, current, Trade(m.inst_id, side, qty, qty * m.price), "rebalance"))

    # buys may not spend more than the cash left after sells, net of fees
    sells = sum((p.trade.notional for p in out if p.trade and p.trade.side == "sell"), D(0))
    buys = sum((p.trade.notional for p in out if p.trade and p.trade.side == "buy"), D(0))
    budget = (cash + sells * (1 - fee)) / (1 + fee)
    if buys > budget and buys > 0:
        scale = budget / buys
        mk = {m.inst_id: m for m in markets}
        scaled: list[SleevePlan] = []
        for p in out:
            if p.trade and p.trade.side == "buy":
                m = mk[p.inst_id]
                qty = _qdown(p.trade.qty * scale, m.lot_sz)
                if qty < m.min_sz:
                    scaled.append(SleevePlan(p.inst_id, p.weight, p.target_value, p.current_value, None, "no cash left"))
                else:
                    scaled.append(SleevePlan(p.inst_id, p.weight, p.target_value, p.current_value, Trade(p.inst_id, "buy", qty, qty * m.price), "rebalance, scaled to cash"))
            else:
                scaled.append(p)
        out = scaled
    return out


def backtest(
    closes: dict[str, list[float]],  # aligned daily closes per inst, same length
    start: int,
    target_vol: float,
    band: float,
    min_trade: float,
    capital: float,
    cost: float = 0.0015,
) -> list[float]:
    """Daily equity of the live rebalancing rule (bands and minimum trade included), from index `start`."""
    insts = list(closes)
    cash = capital
    qty = {i: 0.0 for i in insts}
    eq = [capital]
    n = len(closes[insts[0]])
    for d in range(start, n - 1):
        px = {i: closes[i][d] for i in insts}
        e = cash + sum(qty[i] * px[i] for i in insts)
        sleeve = e / len(insts)
        for i in insts:
            w = signal(closes[i][: d + 1], target_vol).weight
            delta = sleeve * w - qty[i] * px[i]
            if abs(delta) < max(band * sleeve, min_trade):
                continue
            if w == 0:
                delta = -qty[i] * px[i]
            qty[i] += delta / px[i]
            cash -= delta + abs(delta) * cost
        eq.append(cash + sum(qty[i] * closes[i][d + 1] for i in insts))
    return eq
