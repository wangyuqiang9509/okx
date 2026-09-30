"""How sure are we that trend+vt beats holding? Stationary block bootstrap of daily returns.

Portfolio of BTC/ETH/SOL (1/3 each, daily rebalanced sleeves), 2021-04 .. 2026-09, same
curves as trend_study.py. Resample 20-day blocks of the paired daily returns 5,000 times and
look at the distribution of Sharpe(trend+vt) - Sharpe(hold) and of the max-drawdown gap.
"""
from __future__ import annotations

import math
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trend_study import INSTS, ensemble, equity, load, vol_scale  # noqa: E402

WARM, BLOCK, REPS = 200, 20, 5000


def curves() -> dict[str, list[float]]:
    data = {i: load(i) for i in INSTS}
    t0 = max(v[0][0] for v in data.values())
    closes = {i: v[1][v[0].index(t0):] for i, v in data.items()}
    n = min(len(c) for c in closes.values())
    out = {}
    for name, fn in (("hold", lambda c: [1.0] * len(c)), ("trend+vt", lambda c: vol_scale(c, ensemble(c))), ("trend", ensemble)):
        eqs = [equity(closes[i][:n], fn(closes[i][:n]), WARM) for i in INSTS]
        r = [sum(e[k] / e[k - 1] for e in eqs) / 3 - 1 for k in range(1, len(eqs[0]))]
        out[name] = r
    return out


def sharpe(r: list[float]) -> float:
    sd = st.pstdev(r)
    return st.mean(r) / sd * math.sqrt(365) if sd else 0.0


def mdd(r: list[float]) -> float:
    e = peak = 1.0
    worst = 0.0
    for x in r:
        e *= 1 + x
        peak = max(peak, e)
        worst = max(worst, 1 - e / peak)
    return worst


def main() -> None:
    c = curves()
    n = len(c["hold"])
    print(f"{n} days; Sharpe hold {sharpe(c['hold']):.2f}, trend {sharpe(c['trend']):.2f}, trend+vt {sharpe(c['trend+vt']):.2f}")
    print(f"maxDD hold {mdd(c['hold']):.1%}, trend {mdd(c['trend']):.1%}, trend+vt {mdd(c['trend+vt']):.1%}\n")
    rnd = random.Random(7)
    for other in ("trend+vt", "trend"):
        ds, dd = [], []
        for _ in range(REPS):
            idx: list[int] = []
            while len(idx) < n:
                s = rnd.randrange(n)
                idx += [(s + k) % n for k in range(BLOCK)]
            idx = idx[:n]
            h = [c["hold"][k] for k in idx]
            t = [c[other][k] for k in idx]
            ds.append(sharpe(t) - sharpe(h))
            dd.append(mdd(h) - mdd(t))
        ds.sort()
        dd.sort()
        q = lambda xs, p: xs[int(p * len(xs))]  # noqa: E731
        print(f"{other} vs hold, {REPS} block-bootstrap resamples:")
        print(f"  Sharpe gap median {q(ds, .5):+.2f}, 90% range {q(ds, .05):+.2f} .. {q(ds, .95):+.2f}, share of resamples where {other} wins {sum(1 for x in ds if x > 0) / REPS:.0%}")
        print(f"  maxDD reduction median {q(dd, .5):+.1%}, 90% range {q(dd, .05):+.1%} .. {q(dd, .95):+.1%}, share where drawdown is smaller {sum(1 for x in dd if x > 0) / REPS:.0%}")


if __name__ == "__main__":
    main()
