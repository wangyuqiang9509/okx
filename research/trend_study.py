"""Step 1 and 2: is trend following on BTC/ETH/SOL robust, and does volatility targeting cut drawdowns?

Daily UTC closes. A signal computed at today's close sets the position held over tomorrow
(close-to-close), so there is no look-ahead. Long only, weight 0..1 of the coin's sleeve.
Cost: 0.15% of traded value (0.10% taker + 0.05% slippage).

Signals (all fixed in advance):
  sma_N     close > N-day simple moving average
  mom_N     close > close N days ago
  ensemble  mean of sma50, sma100, sma200, mom30, mom90, mom180  (weight in sixths)
  +vt       weight scaled by min(1, 40% / 30-day annualised volatility)
"""
from __future__ import annotations

import csv
import math
import statistics as st
import time
from pathlib import Path

COST = 0.0015
TARGET_VOL = 0.40
INSTS = ("BTC-USDT", "ETH-USDT", "SOL-USDT")


def load(inst: str) -> tuple[list[int], list[float]]:
    with open(Path("data/candles") / f"{inst}-1D.csv") as f:
        rows = [(int(r[0]), float(r[4])) for r in csv.reader(f)]
    return [r[0] for r in rows], [r[1] for r in rows]


def sma_sig(c: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    s = 0.0
    for i, x in enumerate(c):
        s += x
        if i >= n:
            s -= c[i - n]
        if i >= n - 1:
            out[i] = 1.0 if x > s / n else 0.0
    return out


def mom_sig(c: list[float], n: int) -> list[float | None]:
    return [None if i < n else (1.0 if c[i] > c[i - n] else 0.0) for i in range(len(c))]


def ensemble(c: list[float]) -> list[float | None]:
    parts = [sma_sig(c, 50), sma_sig(c, 100), sma_sig(c, 200), mom_sig(c, 30), mom_sig(c, 90), mom_sig(c, 180)]
    return [None if any(p[i] is None for p in parts) else sum(p[i] for p in parts) / 6 for i in range(len(c))]  # type: ignore[misc]


def vol_scale(c: list[float], w: list[float | None], n: int = 30) -> list[float | None]:
    out: list[float | None] = []
    rets = [0.0] + [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    for i in range(len(c)):
        if w[i] is None or i < n:
            out.append(None)
            continue
        v = st.pstdev(rets[i - n + 1:i + 1]) * math.sqrt(365)
        out.append(w[i] * min(1.0, TARGET_VOL / v) if v > 0 else w[i])
    return out


def equity(c: list[float], w: list[float | None], start: int) -> list[float]:
    """Daily equity from index `start`, holding w[i] over day i -> i+1."""
    eq, pos = [1.0], 0.0
    for i in range(start, len(c) - 1):
        target = w[i] if w[i] is not None else 0.0
        e = eq[-1] * (1 - abs(target - pos) * COST)
        pos = target
        e *= 1 + pos * (c[i + 1] / c[i] - 1)
        # drift of the weight with price
        pos = pos * (c[i + 1] / c[i]) / (1 + pos * (c[i + 1] / c[i] - 1)) if pos else 0.0
        eq.append(e)
    return eq


def stats(eq: list[float], ts: list[int]) -> dict[str, float]:
    days = len(eq) - 1
    cagr = eq[-1] ** (365 / days) - 1
    r = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    vol = st.pstdev(r) * math.sqrt(365)
    sharpe = (st.mean(r) * 365) / vol if vol else 0.0
    peak, mdd = eq[0], 0.0
    for x in eq:
        peak = max(peak, x)
        mdd = max(mdd, 1 - x / peak)
    monthly = [eq[k + 30] / eq[k] - 1 for k in range(0, len(eq) - 30, 30)]
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "mdd": mdd, "calmar": cagr / mdd if mdd else 0.0,
            "worst_m": min(monthly), "pos_m": sum(1 for m in monthly if m > 0) / len(monthly)}


def yearly(eq: list[float], ts: list[int]) -> dict[str, float]:
    out: dict[str, float] = {}
    first: dict[str, float] = {}
    prev = eq[0]
    for k in range(1, len(eq)):
        y = time.strftime("%Y", time.gmtime(ts[k] / 1000))
        first.setdefault(y, prev)
        out[y] = eq[k] / first[y] - 1
        prev = eq[k]
    return out


def line(name: str, s: dict[str, float]) -> str:
    return (f"{name:14} CAGR {s['cagr'] * 100:>6.1f}%  vol {s['vol'] * 100:>5.1f}%  Sharpe {s['sharpe']:>5.2f}  "
            f"maxDD {s['mdd'] * 100:>5.1f}%  Calmar {s['calmar']:>5.2f}  worst30d {s['worst_m'] * 100:>6.1f}%  up-months {s['pos_m'] * 100:>3.0f}%")


def main() -> None:
    data = {i: load(i) for i in INSTS}
    WARM = 200

    print("=== Step 1a: SMA length sweep, each coin over its full history (after 200-day warm-up)")
    for inst, (ts, c) in data.items():
        print(f"-- {inst} {time.strftime('%Y-%m', time.gmtime(ts[WARM] / 1000))} .. {time.strftime('%Y-%m', time.gmtime(ts[-1] / 1000))}")
        print(line("hold", stats(equity(c, [1.0] * len(c), WARM), ts[WARM:])))
        for n in (20, 30, 50, 75, 100, 150, 200):
            print(line(f"sma{n}", stats(equity(c, sma_sig(c, n), WARM), ts[WARM:])))

    print("\n=== Step 1b: momentum lookback sweep")
    for inst, (ts, c) in data.items():
        print(f"-- {inst}")
        for n in (10, 30, 60, 90, 180, 365):
            if n < WARM or n == 365:
                print(line(f"mom{n}", stats(equity(c, mom_sig(c, n), max(WARM, n)), ts[max(WARM, n):])))

    print("\n=== Step 2: fixed ensemble, with and without volatility targeting")
    for inst, (ts, c) in data.items():
        ens = ensemble(c)
        print(f"-- {inst}")
        print(line("hold", stats(equity(c, [1.0] * len(c), WARM), ts[WARM:])))
        print(line("hold+vt", stats(equity(c, vol_scale(c, [1.0] * len(c)), WARM), ts[WARM:])))
        print(line("ensemble", stats(equity(c, ens, WARM), ts[WARM:])))
        print(line("ensemble+vt", stats(equity(c, vol_scale(c, ens), WARM), ts[WARM:])))

    # equal-weight portfolio over the common period (SOL's history), daily rebalanced sleeves
    print("\n=== Portfolio: 1/3 each, common period, sleeves rebalanced to equal weight monthly")
    t0 = max(data[i][0][0] for i in INSTS)
    aligned = {i: (data[i][0][data[i][0].index(t0):], data[i][1][data[i][0].index(t0):]) for i in INSTS}
    n = min(len(v[1]) for v in aligned.values())
    ts = aligned["BTC-USDT"][0][:n]
    curves: dict[str, list[list[float]]] = {}
    for name, fn in (("hold", lambda c: [1.0] * len(c)), ("hold+vt", lambda c: vol_scale(c, [1.0] * len(c))),
                     ("sma100", lambda c: sma_sig(c, 100)), ("ensemble", ensemble), ("ensemble+vt", lambda c: vol_scale(c, ensemble(c)))):
        curves[name] = [equity(aligned[i][1][:n], fn(aligned[i][1][:n]), WARM) for i in INSTS]
    print(f"{time.strftime('%Y-%m-%d', time.gmtime(ts[WARM] / 1000))} .. {time.strftime('%Y-%m-%d', time.gmtime(ts[-1] / 1000))}")
    port: dict[str, list[float]] = {}
    for name, eqs in curves.items():
        # monthly rebalance: chain 30-day segments of the average of sleeve returns
        pe = [1.0]
        for k in range(1, len(eqs[0])):
            pe.append(pe[-1] * sum(e[k] / e[k - 1] for e in eqs) / 3)
        port[name] = pe
        print(line(name, stats(pe, ts[WARM:])))
    print("\nby year:")
    ys = {name: yearly(pe, ts[WARM:]) for name, pe in port.items()}
    years = list(ys["hold"])
    print(f"{'':14}" + "".join(f"{y:>9}" for y in years))
    for name, y in ys.items():
        print(f"{name:14}" + "".join(f"{y[k] * 100:>8.0f}%" for k in years))


if __name__ == "__main__":
    main()
