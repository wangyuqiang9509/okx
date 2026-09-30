"""Step 3: can a gated grid add anything next to the trend ensemble?

Window 2022-10-01 .. 2026-09-30 (hourly data). Daily signals come from the full daily history,
so there is no warm-up gap. Gates (decided from yesterday's close, applied today):
  always    grid every day
  up        grid when ensemble >= 0.5, else USDT
  range     grid when ER30 < 0.3, else USDT
  up+range  grid when ensemble >= 0.5 and ER30 < 0.3, else USDT
Each gated grid is compared with cash and with the trend ensemble (+vol targeting),
then blended 30% into the trend portfolio.
"""
from __future__ import annotations

import math
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regime_switch import COINS, load as load_1h, run  # noqa: E402
from trend_study import ensemble, equity, line, load as load_1d, stats, vol_scale, yearly  # noqa: E402

DAY = 86_400_000


def er30(c: list[float]) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    for i in range(30, len(c)):
        path = sum(abs(c[k] - c[k - 1]) for k in range(i - 29, i + 1))
        out[i] = abs(c[i] - c[i - 30]) / path if path else 0.0
    return out


def main() -> None:
    sleeves: dict[str, list[list[float]]] = {}
    ts_days: list[int] = []
    for inst in COINS:
        hs = load_1h(inst)
        dts, dc = load_1d(inst)
        idx = {t: i for i, t in enumerate(dts)}
        ens, er = ensemble(dc), er30(dc)
        day0 = hs[0].ts_ms // DAY * DAY
        hs = [h for h in hs if h.ts_ms >= day0]
        days = sorted({h.ts_ms // DAY * DAY for h in hs})

        def sig(d: int, arr: list[float | None]) -> float:
            i = idx.get(days[min(d, len(days) - 1)])
            v = arr[i - 1] if i else None
            return v if v is not None else 0.0

        gates = {
            "always": lambda d: "grid",
            "up": lambda d: "grid" if sig(d, ens) >= 0.5 else "usdt",
            "range": lambda d: "grid" if sig(d, er) < 0.3 else "usdt",
            "up+range": lambda d: "grid" if sig(d, ens) >= 0.5 and sig(d, er) < 0.3 else "usdt",
        }
        for name, g in gates.items():
            eq_h = run(hs, inst, g, 0)
            daily = [eq_h[k] / eq_h[0] for k in range(0, len(eq_h), 24)]
            sleeves.setdefault(f"grid:{name}", []).append(daily)
        # trend on the same days
        start = idx[days[0]]
        n = len(sleeves["grid:always"][-1])
        w = vol_scale(dc, ens)
        sleeves.setdefault("trend+vt", []).append(equity(dc, w, start)[:n])
        sleeves.setdefault("trend", []).append(equity(dc, ens, start)[:n])
        sleeves.setdefault("hold", []).append(equity(dc, [1.0] * len(dc), start)[:n])
        ts_days = days[:n]

    n = min(len(e) for es in sleeves.values() for e in es)

    def port(eqs: list[list[float]]) -> list[float]:
        pe = [1.0]
        for k in range(1, n):
            pe.append(pe[-1] * sum(e[k] / e[k - 1] for e in eqs) / len(eqs))
        return pe

    P = {name: port(eqs) for name, eqs in sleeves.items()}
    ts = ts_days[:n]
    print(f"three-coin portfolios, {n} days from 2022-10-01\n")
    for name, pe in P.items():
        print(line(name, stats(pe, ts)))

    def rets(pe: list[float]) -> list[float]:
        return [pe[k] / pe[k - 1] - 1 for k in range(1, len(pe))]

    print("\ncorrelation of daily returns with trend+vt:")
    base = rets(P["trend+vt"])
    for name in P:
        if name.startswith("grid"):
            print(f"  {name:14} {st.correlation(base, rets(P[name])):+.2f}")

    print("\nblends, rebalanced daily:")
    for g in ("grid:up", "grid:range", "grid:up+range"):
        for share in (0.3,):
            rb, rg = rets(P["trend+vt"]), rets(P[g])
            pe = [1.0]
            for a, b in zip(rb, rg):
                pe.append(pe[-1] * (1 + (1 - share) * a + share * b))
            print(line(f"70/30 {g[5:]}", stats(pe, ts)))
    print("\nby year:")
    ys = {k: yearly(P[k], ts) for k in ("hold", "trend", "trend+vt", "grid:always", "grid:up", "grid:range", "grid:up+range")}
    years = list(ys["hold"])
    print(f"{'':14}" + "".join(f"{y:>8}" for y in years))
    for k, y in ys.items():
        print(f"{k:14}" + "".join(f"{y[t] * 100:>7.0f}%" for t in years))
    _ = math


if __name__ == "__main__":
    main()
