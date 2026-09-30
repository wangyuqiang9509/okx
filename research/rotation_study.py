"""Relative-strength rotation between BTC, ETH, SOL on top of the live Trend Strategy.

PRE-REGISTERED (written before any result was seen, 2026-10-01):
  Design data:  everything before 2025-01-01.  Holdout: 2025-01-01 onward, looked at once, at the end.
  Baseline:     live rule. Each coin's trend weight w_i (six-vote ensemble x min(1, 40%/vol30)) applied
                to an equal third of equity: target_i = E * (1/3) * w_i.
  Variants:     target_i = E * s_i * w_i, where the sleeve shares s_i come from relative strength:
                rank coins each day by the average of their ranks on 30/90/180-day return (1 = strongest).
                  tilt      s = 0.5 / 0.3 / 0.2 by rank
                  top2      s = 0.5 / 0.5 / 0
                  top1      s = 1 / 0 / 0
                Robustness only (not candidates): tilt with a single 30, 90 or 180-day lookback.
  Second sample: same rules on BTC/ETH only, 2018-07 .. 2024-12 (tilt 0.65/0.35, top1 1/0).
  Adopt a variant only if ALL hold:
    1. Sharpe above baseline in the 3-coin design period AND in the 2-coin sample,
    2. it beats baseline in >= 80% of block-bootstrap resamples of the 3-coin design period,
    3. on the holdout its Sharpe is no more than 0.10 below baseline.
  Trials are counted and reported.
Costs: 0.15% of traded value. Daily rebalancing to target, no band (same for all variants).
"""
from __future__ import annotations

import math
import random
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trend_study import ensemble, load, vol_scale  # noqa: E402

COST = 0.0015
HOLDOUT = int(time.mktime(time.strptime("2025-01-01", "%Y-%m-%d"))) * 1000 - time.timezone * 1000
LOOKS = (30, 90, 180)
TRIALS: list[str] = []


def aligned(insts: tuple[str, ...]) -> tuple[list[int], dict[str, list[float]]]:
    data = {i: load(i) for i in insts}
    t0 = max(v[0][0] for v in data.values())
    out = {i: v[1][v[0].index(t0):] for i, v in data.items()}
    ts = data[insts[0]][0][data[insts[0]][0].index(t0):]
    n = min(len(v) for v in out.values())
    return ts[:n], {i: v[:n] for i, v in out.items()}


def ranks(closes: dict[str, list[float]], d: int, looks: tuple[int, ...]) -> list[str]:
    """Coins ordered strongest first by average rank over the lookbacks."""
    insts = list(closes)
    score = {i: 0.0 for i in insts}
    for L in looks:
        order = sorted(insts, key=lambda i: -(closes[i][d] / closes[i][d - L] - 1))
        for r, i in enumerate(order):
            score[i] += r
    return sorted(insts, key=lambda i: (score[i], -(closes[i][d] / closes[i][d - 90] - 1)))


def targets(closes: dict[str, list[float]], shares: tuple[float, ...] | None, looks: tuple[int, ...] = LOOKS) -> list[dict[str, float]]:
    insts = list(closes)
    w = {i: vol_scale(closes[i], ensemble(closes[i])) for i in insts}
    n = len(closes[insts[0]])
    out: list[dict[str, float]] = []
    for d in range(n):
        if any(w[i][d] is None for i in insts) or d < max(looks):
            out.append({i: 0.0 for i in insts})
            continue
        if shares is None:
            s = {i: 1 / len(insts) for i in insts}
        else:
            s = dict(zip(ranks(closes, d, looks), shares))
        out.append({i: s[i] * w[i][d] for i in insts})  # type: ignore[operator]
    return out


def simulate(closes: dict[str, list[float]], tg: list[dict[str, float]], a: int, b: int) -> tuple[list[float], float]:
    """Daily portfolio returns over days a..b-1 and yearly turnover."""
    insts = list(closes)
    pos = {i: 0.0 for i in insts}
    rets, turn = [], 0.0
    for d in range(a, b - 1):
        t = tg[d]
        to = sum(abs(t[i] - pos[i]) for i in insts)
        turn += to
        gross = sum(t[i] * (closes[i][d + 1] / closes[i][d] - 1) for i in insts)
        rets.append((1 - to * COST) * (1 + gross) - 1)
        pos = {i: t[i] * (closes[i][d + 1] / closes[i][d]) / (1 + gross) for i in insts}
    return rets, turn / (len(rets) / 365)


def m(rets: list[float]) -> dict[str, float]:
    e = peak = 1.0
    mdd = 0.0
    for x in rets:
        e *= 1 + x
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    sd = st.pstdev(rets)
    return {"cagr": e ** (365 / len(rets)) - 1, "vol": sd * math.sqrt(365), "sharpe": st.mean(rets) / sd * math.sqrt(365) if sd else 0.0, "mdd": mdd}


def row(name: str, rets: list[float], turn: float) -> str:
    x = m(rets)
    return f"  {name:22} CAGR {x['cagr']:>7.1%}  vol {x['vol']:>6.1%}  Sharpe {x['sharpe']:>5.2f}  maxDD {x['mdd']:>6.1%}  turnover {turn:>5.1f}x/yr"


def boot_win(a: list[float], b: list[float], reps: int = 5000, block: int = 20) -> float:
    """Share of paired block-bootstrap resamples where Sharpe(a) > Sharpe(b)."""
    rnd = random.Random(11)
    n, wins = len(a), 0
    for _ in range(reps):
        idx: list[int] = []
        while len(idx) < n:
            s = rnd.randrange(n)
            idx += [(s + k) % n for k in range(block)]
        idx = idx[:n]
        if m([a[k] for k in idx])["sharpe"] > m([b[k] for k in idx])["sharpe"]:
            wins += 1
    return wins / reps


def main() -> None:
    ts3, c3 = aligned(("BTC-USDT", "ETH-USDT", "SOL-USDT"))
    start3 = 200
    cut3 = next(k for k, t in enumerate(ts3) if t >= HOLDOUT)
    day = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    variants = {"baseline (equal)": None, "tilt 50/30/20": (0.5, 0.3, 0.2), "top2 50/50": (0.5, 0.5, 0.0), "top1": (1.0, 0.0, 0.0)}
    tgs = {k: targets(c3, v) for k, v in variants.items()}

    print(f"3 coins, DESIGN {day(ts3[start3])} .. {day(ts3[cut3 - 1])}")
    design: dict[str, list[float]] = {}
    for k in variants:
        r, t = simulate(c3, tgs[k], start3, cut3)
        design[k] = r
        TRIALS.append(f"3coin-design:{k}")
        print(row(k, r, t))
    print("  robustness, tilt with one lookback:")
    for L in LOOKS:
        r, t = simulate(c3, targets(c3, (0.5, 0.3, 0.2), (L,)), start3, cut3)
        TRIALS.append(f"3coin-design:tilt-{L}")
        print(row(f"tilt, {L}-day only", r, t))

    ts2, c2 = aligned(("BTC-USDT", "ETH-USDT"))
    start2 = 200
    cut2 = next(k for k, t in enumerate(ts2) if t >= HOLDOUT)
    print(f"\n2 coins (BTC/ETH), DESIGN {day(ts2[start2])} .. {day(ts2[cut2 - 1])}")
    two = {"baseline (equal)": None, "tilt 65/35": (0.65, 0.35), "top1": (1.0, 0.0)}
    design2: dict[str, list[float]] = {}
    for k, v in two.items():
        r, t = simulate(c2, targets(c2, v), start2, cut2)
        design2[k] = r
        TRIALS.append(f"2coin-design:{k}")
        print(row(k, r, t))

    print("\npre-registered criteria 1 and 2 (design data only):")
    pairs = {"tilt 50/30/20": "tilt 65/35", "top2 50/50": None, "top1": "top1"}
    survivors = []
    for k, k2 in pairs.items():
        s3 = m(design[k])["sharpe"] - m(design["baseline (equal)"])["sharpe"]
        s2 = (m(design2[k2])["sharpe"] - m(design2["baseline (equal)"])["sharpe"]) if k2 else None
        win = boot_win(design[k], design["baseline (equal)"])
        ok = s3 > 0 and (s2 is None or s2 > 0) and win >= 0.8
        s2txt = f"{s2:+.2f}" if s2 is not None else "n/a"
        print(f"  {k:14} Sharpe gap 3-coin {s3:+.2f}, 2-coin {s2txt}, bootstrap win {win:.0%} -> {'PASS' if ok else 'fail'}")
        if ok:
            survivors.append(k)

    print(f"\nHOLDOUT {day(ts3[cut3])} .. {day(ts3[-1])} (looked at once)")
    hold: dict[str, list[float]] = {}
    for k in ["baseline (equal)", *variants.keys() - {"baseline (equal)"}]:
        r, t = simulate(c3, tgs[k], cut3, len(ts3))
        hold[k] = r
        print(row(k, r, t))
    for k in survivors:
        gap = m(hold[k])["sharpe"] - m(hold["baseline (equal)"])["sharpe"]
        print(f"  criterion 3 for {k}: holdout Sharpe gap {gap:+.2f} -> {'PASS' if gap >= -0.10 else 'fail'}")
    if not survivors:
        print("  no variant passed criteria 1 and 2; holdout shown for information only")
    print(f"\ntrials run: {len(TRIALS)}")

    # context: how similar are the three coins?
    r = {i: [c3[i][d + 1] / c3[i][d] - 1 for d in range(start3, cut3 - 1)] for i in c3}
    print("daily return correlation (design): " + ", ".join(f"{a[:3]}-{b[:3]} {st.correlation(r[a], r[b]):.2f}" for a, b in (("BTC-USDT", "ETH-USDT"), ("BTC-USDT", "SOL-USDT"), ("ETH-USDT", "SOL-USDT"))))


if __name__ == "__main__":
    main()
