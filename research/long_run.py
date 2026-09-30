"""Four years of hourly candles, 40,000 USDT split evenly over BTC (1%), ETH (2%), SOL (2%), 8 levels a side.

Policies for what happens when price leaves the range:
  never    one grid for the whole period; after a breakout it just sits
  monthly  rebuild at the current price every 30 days with the current equity
  breakout rebuild when the close has been outside the range for 72 hours

A rebuild costs 0.1% of equity (rebalancing coin/USDT with taker orders).
Hourly bars miss intra-hour swings, so realised profit here is conservative; the script
calibrates against 1m replay on the last 180 days.
"""
from __future__ import annotations

import csv
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gridbot.candles import _read  # noqa: E402
from gridbot.engine import Fill, GridEngine, Side, new_grid_id  # noqa: E402
from gridbot.replay import Candle, _legs, build_spec, run_replay  # noqa: E402

TOTAL = D("40000")
COINS = {"BTC-USDT": ("0.01", D("0.1"), D("0.00000001"), D("0.00001")),
         "ETH-USDT": ("0.02", D("0.01"), D("0.000001"), D("0.0001")),
         "SOL-USDT": ("0.02", D("0.01"), D("0.000001"), D("0.01"))}
MAKER, TAKER, RESET_COST = D("-0.0008"), D("-0.001"), D("0.001")
H_MONTH = 720


def load_1h(inst: str) -> list[Candle]:
    with open(Path("data/candles") / f"{inst}-1H.csv") as f:
        return [Candle(int(r[0]), D(r[1]), D(r[2]), D(r[3]), D(r[4])) for r in csv.reader(f)]


def simulate(cs: list[Candle], inst: str, policy: str) -> tuple[list[float], float, int]:
    """Hourly equity series, total realised profit, number of rebuilds."""
    sp, tick, lot, mn = COINS[inst]
    cap = TOTAL / 3
    equity: list[float] = []
    realised = D(0)
    rebuilds = 0
    i = 0
    while i < len(cs):
        spec = build_spec(inst, cs[i].open, D(sp), 8, 8, cap, -TAKER, tick, lot, mn)
        eng = GridEngine(spec, new_grid_id())
        seed = eng.make_seed(cs[i].open, cap)
        eng.on_fill(Fill("seed", seed.cl_ord_id, cs[i].open, seed.qty, seed.qty * TAKER, "BTC", 0))
        eng.initial_orders()
        tid, outside, start = 0, 0, i
        while i < len(cs):
            c = cs[i]
            for a, b in _legs(c):
                if b < a:
                    hit = sorted((o for o in eng.resting_orders() if o.side is Side.BUY and b <= o.price <= a), key=lambda o: -o.price)
                elif b > a:
                    hit = sorted((o for o in eng.resting_orders() if o.side is Side.SELL and a <= o.price <= b), key=lambda o: o.price)
                else:
                    continue
                for o in hit:
                    tid += 1
                    if o.side is Side.BUY:
                        eng.on_fill(Fill(f"r{tid}", o.cl_ord_id, o.price, o.remaining, o.remaining * MAKER, "BTC", 0))
                    else:
                        eng.on_fill(Fill(f"r{tid}", o.cl_ord_id, o.price, o.remaining, o.price * o.remaining * MAKER, "USDT", 0))
            equity.append(float(eng.equity(c.close)))
            outside = 0 if eng.in_range(c.close) else outside + 1
            i += 1
            if policy == "monthly" and i - start >= H_MONTH:
                break
            if policy == "breakout" and outside >= 72:
                break
        realised += eng.realised_profit
        cap = eng.equity(cs[i - 1].close) * (1 - RESET_COST)
        if i < len(cs):
            rebuilds += 1
    return equity, float(realised), rebuilds


def report(name: str, eq: list[float], realised: float, months: float, ts: list[int]) -> None:
    start, end = float(TOTAL), eq[-1]
    peak, mdd = start, 0.0
    for x in eq:
        peak = max(peak, x)
        mdd = max(mdd, (peak - x) / peak)
    m = [eq[k + H_MONTH - 1] - (eq[k - 1] if k else start) for k in range(0, len(eq) - H_MONTH + 1, H_MONTH)]
    years: dict[str, list[float]] = {}
    for k, x in enumerate(eq):
        years.setdefault(time.strftime("%Y", time.gmtime(ts[k] / 1000)), []).append(x)
    prev = start
    yr = []
    for y, xs in years.items():
        yr.append(f"{y} {xs[-1] - prev:+,.0f}")
        prev = xs[-1]
    print(f"{name:9} end {end:>9,.0f}  avg/month {(end - start) / months:>+7,.0f}  realised/month {realised / months:>6,.0f}"
          f"  worst month {min(m):>+8,.0f}  best month {max(m):>+8,.0f}  losing months {sum(1 for x in m if x < 0)}/{len(m)}  maxDD {mdd * 100:4.1f}%")
    print(f"          by year: {', '.join(yr)}")


def main() -> None:
    data = {i: load_1h(i) for i in COINS}
    n = min(len(v) for v in data.values())
    data = {i: v[-n:] for i, v in data.items()}
    ts = [c.ts_ms for c in data["BTC-USDT"]]
    months = n / H_MONTH
    print(f"{time.strftime('%Y-%m-%d', time.gmtime(ts[0] / 1000))} .. {time.strftime('%Y-%m-%d', time.gmtime(ts[-1] / 1000))}, {months:.1f} months, {TOTAL} USDT\n")
    hold = [sum(float(TOTAL / 3 / data[i][0].open * data[i][k].close) for i in COINS) for k in range(n)]
    report("hold", hold, 0.0, months, ts)
    for policy in ("never", "monthly", "breakout"):
        eqs, real, rb = [], 0.0, 0
        for inst in COINS:
            e, r, b = simulate(data[inst], inst, policy)
            eqs.append(e); real += r; rb += b
        report(policy, [sum(x) for x in zip(*eqs)], real, months, ts)
        print(f"          rebuilds {rb}")

    # calibration: realised on 1H vs 1m over the same last 180 days, monthly rebuilds
    print("\ncalibration, last 180 days, realised grid profit per coin (1H vs 1m replay, 30-day windows):")
    for inst, (sp, tick, lot, mn) in COINS.items():
        m1 = _read(Path("data/candles") / f"{inst}-1m.csv")[-180 * 1440:]
        h1 = [c for c in data[inst] if c.ts_ms >= m1[0].ts_ms]
        r1m = sum(float(run_replay(m1[s:s + 43200], D(sp), 8, 8, D(10000), MAKER, TAKER, tick, lot, mn, inst).realised_profit) for s in range(0, len(m1) - 43199, 43200))
        r1h = sum(float(run_replay(h1[s:s + 720], D(sp), 8, 8, D(10000), MAKER, TAKER, tick, lot, mn, inst).realised_profit) for s in range(0, len(h1) - 719, 720))
        print(f"  {inst}: 1m {r1m:,.0f}  1H {r1h:,.0f}  ratio {r1m / r1h:.2f}")


if __name__ == "__main__":
    main()
