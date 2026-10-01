"""Spot martingale ('EA'-style) with profits withdrawn: can 1000 USDT pay back 1000 before it gets stuck?

Spot only, no leverage, so no liquidation: the ladder is sized so that every add together spends
exactly the 1000 USDT; when the ladder is used up the bot simply holds the coins until price
recovers to the take-profit. Rules per cycle: buy base; each time price falls `step` below the
last buy, buy `mult` x the previous size (up to `adds` times); when price is `tp` above the average
cost, sell everything and WITHDRAW the profit. Capital stays 1000 at the start of every cycle.
Hourly candles, path open->low->high->close (green) or open->high->low->close (red). Fee 0.1%/side.
A new start every 7 days; each run lasts 12 months. Exploratory.
"""
from __future__ import annotations

import csv
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from decimal import Decimal  # noqa: E402

from gridbot.replay import Candle, _legs  # noqa: E402

FEE = 0.001
CAP = 1000.0
H_YEAR = 365 * 24

SETS = {
    "aggressive  step 1.5% x2, 6 adds, tp 1%": (0.015, 2.0, 6, 0.01),
    "moderate    step 3%   x2, 5 adds, tp 1.5%": (0.03, 2.0, 5, 0.015),
    "cautious    step 5%   x1.5, 6 adds, tp 2%": (0.05, 1.5, 6, 0.02),
}


def load(inst: str) -> list[tuple[float, float, float, float]]:
    with open(Path("data/candles") / f"{inst}-1H.csv") as f:
        return [(float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in csv.reader(f)]


def run(c: list[tuple[float, float, float, float]], start: int, hours: int, step: float, mult: float, adds: int, tp: float) -> dict[str, float]:
    base = CAP / sum(mult ** k for k in range(adds + 1))
    withdrawn, cycles, stuck_h = 0.0, 0, 0
    month_w = [0.0] * 13
    payback_h = None
    qty = cost = 0.0
    last = None
    n_add = 0
    for h in range(start, min(start + hours, len(c))):
        o, hi, lo, cl = c[h]
        if last is None:  # open a cycle at the hour's open
            qty, cost, last, n_add = base * (1 - FEE) / o, base, o, 0
        pts = [o, hi, lo, cl] if cl < o else [o, lo, hi, cl]
        for a, b in zip(pts, pts[1:]):
            if last is None:
                break
            if b < a:
                while n_add < adds and b <= last * (1 - step):
                    px = last * (1 - step)
                    size = base * mult ** (n_add + 1)
                    qty += size * (1 - FEE) / px
                    cost += size
                    last, n_add = px, n_add + 1
            else:
                target = cost / qty * (1 + tp)
                if b >= target:
                    proceeds = qty * target * (1 - FEE)
                    profit = proceeds - cost
                    withdrawn += profit
                    month_w[min((h - start) // 730, 12)] += profit
                    cycles += 1
                    if payback_h is None and withdrawn >= CAP:
                        payback_h = h - start
                    last = None
        if last is not None and n_add == adds:
            stuck_h += 1
    end = min(start + hours, len(c)) - 1
    left = (CAP - cost) + qty * c[end][3] if last is not None else CAP
    return {"withdrawn": withdrawn, "left": left, "total": withdrawn + left - CAP, "cycles": cycles,
            "stuck": stuck_h / (end - start + 1), "payback_m": payback_h / 730 if payback_h is not None else None,
            "zero_months": sum(1 for x in month_w[:12] if x <= 0)}


def main() -> None:
    for inst in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        c = load(inst)
        starts = range(0, len(c) - H_YEAR, 24 * 7)
        print(f"== {inst}: {len(starts)} one-year runs, starting every week from 2022-10")
        for name, (step, mult, adds, tp) in SETS.items():
            rs = [run(c, s, H_YEAR, step, mult, adds, tp) for s in starts]
            w = [r["withdrawn"] for r in rs]
            tot = [r["total"] for r in rs]
            paid = [r for r in rs if r["payback_m"] is not None]
            print(f"  {name}")
            print(f"    withdrawn in 12 months: median {st.median(w):>6.0f}  worst {min(w):>5.0f}  best {max(w):>5.0f}   "
                  f"months with no income: median {st.median(r['zero_months'] for r in rs):.0f}/12")
            print(f"    paid back 1000 within 12 months: {len(paid) / len(rs):>4.0%}"
                  + (f" (median {st.median(r['payback_m'] for r in paid):.1f} months)" if paid else ""))
            print(f"    after 12 months, withdrawn + what is left - 1000: median {st.median(tot):>+6.0f}  worst {min(tot):>+6.0f}   "
                  f"time stuck with the ladder full: median {st.median(r['stuck'] for r in rs):.0%}, worst {max(r['stuck'] for r in rs):.0%}")


if __name__ == "__main__":
    main()
