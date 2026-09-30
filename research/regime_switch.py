"""Does switching strategy by market regime beat any single strategy? Four years, hourly, BTC/ETH/SOL.

Regime rule, fixed in advance (not tuned), decided from daily closes up to yesterday, applied today:
  close > SMA100 and ER30 >= 0.3  -> trend_up    hold the coin
  close < SMA100 and ER30 >= 0.3  -> trend_down  hold USDT
  otherwise                       -> range       run the grid (BTC 1%, ETH/SOL 2%, 8 levels a side,
                                                  rebuilt after 72h outside its range)
ER30 = efficiency ratio = |net 30-day move| / sum of daily absolute moves (1 = straight line, 0 = pure chop).

Every switch liquidates to USDT with a taker fee, then enters the new mode (another taker fee
for coin or the grid's Seed Buy). That double-counts some fees, so results are conservative.
Strategies compared on the same window (after 100 days of warm-up):
  hold, trend (close > SMA100 -> coin else USDT), grid (always, breakout rebuild), switch.
"""
from __future__ import annotations

import csv
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gridbot.engine import Fill, GridEngine, Side, new_grid_id  # noqa: E402
from gridbot.replay import Candle, _legs, build_spec  # noqa: E402

TOTAL = 40000.0
COINS = {"BTC-USDT": ("0.01", D("0.1"), D("0.00000001"), D("0.00001")),
         "ETH-USDT": ("0.02", D("0.01"), D("0.000001"), D("0.0001")),
         "SOL-USDT": ("0.02", D("0.01"), D("0.000001"), D("0.01"))}
MAKER, TAKER = D("-0.0008"), D("-0.001")
FEE = 0.001
SMA_N, ER_N, ER_MIN, WARM = 100, 30, 0.3, 100


def load(inst: str) -> list[Candle]:
    with open(Path("data/candles") / f"{inst}-1H.csv") as f:
        return [Candle(int(r[0]), D(r[1]), D(r[2]), D(r[3]), D(r[4])) for r in csv.reader(f)]


def regimes(cs: list[Candle]) -> list[str]:
    """Regime for each day, from data up to the previous day's close."""
    closes = [float(cs[min(d * 24 + 23, len(cs) - 1)].close) for d in range(len(cs) // 24)]
    out = []
    for d in range(len(closes)):
        y = d - 1
        if y < SMA_N:
            out.append("warmup")
            continue
        sma = sum(closes[y - SMA_N + 1:y + 1]) / SMA_N
        path = sum(abs(closes[i] - closes[i - 1]) for i in range(y - ER_N + 1, y + 1))
        er = abs(closes[y] - closes[y - ER_N]) / path if path else 0
        if er >= ER_MIN:
            out.append("trend_up" if closes[y] > sma else "trend_down")
        else:
            out.append("range")
    return out


class Grid:
    def __init__(self, inst: str, cap: float, c: Candle) -> None:
        sp, tick, lot, mn = COINS[inst]
        self.inst = inst
        spec = build_spec(inst, c.open, D(sp), 8, 8, D(str(cap)), -TAKER, tick, lot, mn)
        self.eng = GridEngine(spec, new_grid_id())
        seed = self.eng.make_seed(c.open, D(str(cap)))
        self.eng.on_fill(Fill("seed", seed.cl_ord_id, c.open, seed.qty, seed.qty * TAKER, "BTC", 0))
        self.eng.initial_orders()
        self.tid = 0
        self.outside = 0

    def step(self, c: Candle) -> None:
        e = self.eng
        for a, b in _legs(c):
            if b < a:
                hit = sorted((o for o in e.resting_orders() if o.side is Side.BUY and b <= o.price <= a), key=lambda o: -o.price)
            elif b > a:
                hit = sorted((o for o in e.resting_orders() if o.side is Side.SELL and a <= o.price <= b), key=lambda o: o.price)
            else:
                continue
            for o in hit:
                self.tid += 1
                if o.side is Side.BUY:
                    e.on_fill(Fill(f"r{self.tid}", o.cl_ord_id, o.price, o.remaining, o.remaining * MAKER, "BTC", 0))
                else:
                    e.on_fill(Fill(f"r{self.tid}", o.cl_ord_id, o.price, o.remaining, o.price * o.remaining * MAKER, "USDT", 0))
        self.outside = 0 if e.in_range(c.close) else self.outside + 1

    def value(self, px: float) -> tuple[float, float]:
        return float(self.eng.cash_quote), float(self.eng.base_held) * px


def run(cs: list[Candle], inst: str, mode_of_day, start: int) -> list[float]:
    cash, coin_qty, grid, mode = TOTAL / 3, 0.0, None, "usdt"
    eq = []
    for h in range(start, len(cs)):
        c = cs[h]
        if (h - start) % 24 == 0:
            want = mode_of_day(h // 24)
            if want != mode:
                px = float(c.open)
                if grid is not None:  # liquidate grid: sell its coin
                    gc, gv = grid.value(px)
                    cash, grid = gc + gv * (1 - FEE), None
                elif coin_qty:
                    cash, coin_qty = cash + coin_qty * px * (1 - FEE), 0.0
                if want == "coin":
                    coin_qty, cash = cash * (1 - FEE) / px, 0.0
                elif want == "grid":
                    grid = Grid(inst, cash, c)
                mode = want
        if grid is not None:
            grid.step(c)
            if grid.outside >= 72:  # breakout rebuild, as in long_run.py
                gc, gv = grid.value(float(c.close))
                grid = Grid(inst, gc + gv * (1 - FEE), cs[h + 1] if h + 1 < len(cs) else c)
            gc, gv = grid.value(float(c.close))
            eq.append(gc + gv)
        else:
            eq.append(cash + coin_qty * float(c.close))
    return eq


def report(name: str, eq: list[float], ts: list[int]) -> None:
    months = len(eq) / 720
    peak, mdd = TOTAL, 0.0
    for x in eq:
        peak = max(peak, x)
        mdd = max(mdd, (peak - x) / peak)
    m = [eq[k + 719] - (eq[k - 1] if k else TOTAL) for k in range(0, len(eq) - 719, 720)]
    years: dict[str, float] = {}
    prev = TOTAL
    for k, x in enumerate(eq):
        years[time.strftime("%Y", time.gmtime(ts[k] / 1000))] = x
    parts = []
    for y, x in years.items():
        parts.append(f"{y} {x - prev:+,.0f}")
        prev = x
    print(f"{name:7} end {eq[-1]:>9,.0f}  avg/month {(eq[-1] - TOTAL) / months:>+7,.0f}  worst month {min(m):>+8,.0f}"
          f"  losing months {sum(1 for x in m if x < 0):>2}/{len(m)}  maxDD {mdd * 100:4.1f}%   {', '.join(parts)}")


def main() -> None:
    data = {i: load(i) for i in COINS}
    n = min(len(v) for v in data.values())
    data = {i: v[-n:] for i, v in data.items()}
    start = WARM * 24 + 24
    ts = [c.ts_ms for c in data["BTC-USDT"]][start:]
    print(f"window {time.strftime('%Y-%m-%d', time.gmtime(ts[0] / 1000))} .. {time.strftime('%Y-%m-%d', time.gmtime(ts[-1] / 1000))}"
          f" ({len(ts) / 720:.1f} months), {TOTAL:,.0f} USDT split over BTC/ETH/SOL\n")
    results: dict[str, list[list[float]]] = {k: [] for k in ("hold", "trend", "grid", "switch")}
    mix: dict[str, int] = {}
    switches = 0
    for inst, cs in data.items():
        reg = regimes(cs)
        sma_up = []
        closes = [float(cs[min(d * 24 + 23, len(cs) - 1)].close) for d in range(len(cs) // 24)]
        for d in range(len(closes)):
            y = d - 1
            sma_up.append(y >= SMA_N and closes[y] > sum(closes[y - SMA_N + 1:y + 1]) / SMA_N)
        day = lambda d, r=reg: r[min(d, len(r) - 1)]  # noqa: E731
        results["hold"].append(run(cs, inst, lambda d: "coin", start))
        results["trend"].append(run(cs, inst, lambda d, s=sma_up: "coin" if s[min(d, len(s) - 1)] else "usdt", start))
        results["grid"].append(run(cs, inst, lambda d: "grid", start))
        to_mode = {"trend_up": "coin", "trend_down": "usdt", "range": "grid", "warmup": "usdt"}
        results["switch"].append(run(cs, inst, lambda d: to_mode[day(d)], start))
        used = [r for r in reg[start // 24:]]
        for r in used:
            mix[r] = mix.get(r, 0) + 1
        switches += sum(1 for a, b in zip(used, used[1:]) if a != b)
    for name, eqs in results.items():
        report(name, [sum(x) for x in zip(*eqs)], ts)
    total_days = sum(mix.values())
    print("\nregime share (coin-days): " + ", ".join(f"{k} {v / total_days * 100:.0f}%" for k, v in sorted(mix.items())) + f"; regime changes {switches} over 3 coins")


if __name__ == "__main__":
    main()
