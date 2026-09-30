"""Replay: feed historical 1m candles through the engine with simulated maker fills.

Fill model: each candle is walked as a path, open -> low -> high -> close for a
green candle and open -> high -> low -> close for a red one. A leg fills every
resting order it sweeps through, at the order's price. Counter-orders placed on
one leg can only fill on a later leg. Queue position is ignored, so Replay
still overstates Grid Profit a little.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Iterable

from .engine import ZERO, Fill, GridEngine, GridSpec, Side, new_grid_id, qty_for_capital


@dataclass(frozen=True)
class Candle:
    ts_ms: int
    open: D
    high: D
    low: D
    close: D


@dataclass
class ReplayResult:
    candles: int
    start_px: D
    end_px: D
    lower: D
    upper: D
    qty: D
    round_trips: int
    realised_profit: D
    final_equity: D
    buy_and_hold_equity: D
    max_drawdown_pct: D
    minutes_outside_range: int
    first_breakout_at: int | None  # index of first candle outside the range

    def summary(self, capital: D) -> str:
        lines = [
            f"candles            {self.candles} ({self.candles / 1440:.1f} days)",
            f"price              {self.start_px} -> {self.end_px}",
            f"range              {self.lower} .. {self.upper}   qty/level {self.qty}",
            f"round trips        {self.round_trips}",
            f"grid profit        {self.realised_profit:.4f} USDT ({self.realised_profit / capital * 100:.2f}% of capital)",
            f"final equity       {self.final_equity:.4f} USDT ({(self.final_equity / capital - 1) * 100:+.2f}%)",
            f"buy and hold       {self.buy_and_hold_equity:.4f} USDT ({(self.buy_and_hold_equity / capital - 1) * 100:+.2f}%)",
            f"max drawdown       {self.max_drawdown_pct:.2f}%",
            f"outside range      {self.minutes_outside_range} min"
            + (f", first at candle {self.first_breakout_at}" if self.first_breakout_at is not None else ""),
        ]
        return "\n".join(lines)


def _legs(c: Candle) -> list[tuple[D, D]]:
    pts = [c.open, c.low, c.high, c.close] if c.close >= c.open else [c.open, c.high, c.low, c.close]
    return list(zip(pts, pts[1:]))


def build_spec(
    inst_id: str,
    anchor: D,
    spacing: D,
    levels_below: int,
    levels_above: int,
    capital: D,
    taker_fee: D,
    tick_sz: D,
    lot_sz: D,
    min_sz: D,
) -> GridSpec:
    qty = qty_for_capital(capital, anchor, spacing, levels_below, levels_above, taker_fee, lot_sz, tick_sz)
    if qty < min_sz:
        raise ValueError(f"capital {capital} gives {qty} per level, below min size {min_sz}")
    return GridSpec(inst_id, anchor, spacing, levels_below, levels_above, qty, tick_sz, lot_sz, min_sz)


def run_replay(
    candles: Iterable[Candle],
    spacing: D,
    levels_below: int,
    levels_above: int,
    capital: D,
    maker_fee: D,
    taker_fee: D,
    tick_sz: D = D("0.1"),
    lot_sz: D = D("0.00000001"),
    min_sz: D = D("0.00001"),
    inst_id: str = "BTC-USDT",
) -> ReplayResult:
    it = iter(candles)
    first = next(it)
    spec = build_spec(inst_id, first.open, spacing, levels_below, levels_above, capital, taker_fee, tick_sz, lot_sz, min_sz)
    eng = GridEngine(spec, new_grid_id())
    seed = eng.make_seed(first.open, capital)
    eng.on_fill(Fill("seed", seed.cl_ord_id, first.open, seed.qty, seed.qty * taker_fee, "BTC", first.ts_ms))
    eng.initial_orders()

    peak = capital
    max_dd = ZERO
    outside = 0
    first_breakout: int | None = None
    n = 0
    last = first
    tid = 0
    for i, c in enumerate([first, *it]):
        n += 1
        last = c
        for a, b in _legs(c):
            if b < a:  # sweeping down: buys between a and b, highest first
                hit = [o for o in eng.resting_orders() if o.side is Side.BUY and b <= o.price <= a]
                hit.sort(key=lambda o: o.price, reverse=True)
            elif b > a:  # sweeping up: sells between a and b, lowest first
                hit = [o for o in eng.resting_orders() if o.side is Side.SELL and a <= o.price <= b]
                hit.sort(key=lambda o: o.price)
            else:
                continue
            for o in hit:
                tid += 1
                if o.side is Side.BUY:
                    f = Fill(f"r{tid}", o.cl_ord_id, o.price, o.remaining, o.remaining * maker_fee, "BTC", c.ts_ms)
                else:
                    f = Fill(f"r{tid}", o.cl_ord_id, o.price, o.remaining, o.price * o.remaining * maker_fee, "USDT", c.ts_ms)
                eng.on_fill(f)
        if not eng.in_range(c.close):
            outside += 1
            if first_breakout is None:
                first_breakout = i
        eq = eng.equity(c.close)
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100
        max_dd = max(max_dd, dd)

    return ReplayResult(
        candles=n,
        start_px=first.open,
        end_px=last.close,
        lower=spec.lower,
        upper=spec.upper,
        qty=spec.qty,
        round_trips=eng.round_trips,
        realised_profit=eng.realised_profit,
        final_equity=eng.equity(last.close),
        buy_and_hold_equity=capital / first.open * last.close * (1 + taker_fee),
        max_drawdown_pct=max_dd,
        minutes_outside_range=outside,
        first_breakout_at=first_breakout,
    )
