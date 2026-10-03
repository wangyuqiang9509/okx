"""High-frequency spot martingale that refills when stuck: can small capital double?

User's plan (2026-10-03): spot only, BTC/ETH/SOL only, tight-step doubling ladder; when the ladder is
used up, keep holding it and deposit another small unit to start a fresh ladder at the current price.

Model, per coin, all amounts in units of one deposit U:
- Cash pool starts at 1 U. The active ladder sizes its base so that the full ladder spends the pool.
- Each `step` fall below the last buy buys `mult` x the previous size, up to `adds` times.
- When price reaches average cost * (1 + tp) the ladder sells everything; proceeds return to the pool
  (profits compound) and a new ladder opens at once.
- When the active ladder is used up it becomes a stuck ladder that only waits for its own take-profit.
  The next ladder opens from whatever cash came back from earlier stuck ladders; if that is below
  0.5 U the user deposits another 1 U. At most MAX_DEPOSITS deposits; after that the bot waits.
- Doubling = equity (cash + coins at market) >= 2 x total deposited, checked at the horizon end.
Hourly candles, path open->low->high->close (green) or open->high->low->close (red). Fee 0.1%/side.
A new start every 7 days from 2022-10. Pre-declared parameter sets below; exploratory, not a rule search.
"""
from __future__ import annotations

import csv
import statistics as st
from pathlib import Path

FEE = 0.001
MAX_DEPOSITS = 5
H_MONTH = 730

SETS = {
    "hf-0.5  step 0.5% x2, 7 adds, tp 0.5%": (0.005, 2.0, 7, 0.005),
    "hf-1    step 1%   x2, 6 adds, tp 0.8%": (0.01, 2.0, 6, 0.008),
    "hf-1.5  step 1.5% x2, 6 adds, tp 1%  ": (0.015, 2.0, 6, 0.01),
}


def load(inst: str, bar: str = "1H") -> list[tuple[float, float, float, float]]:
    with open(Path("data/candles") / f"{inst}-{bar}.csv") as f:
        return [(float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in csv.reader(f)]


class Ladder:
    def __init__(self, cash: float, px: float, step: float, mult: float, adds: int) -> None:
        self.base = cash / sum(mult ** k for k in range(adds + 1))
        self.step, self.mult, self.adds = step, mult, adds
        self.budget = cash
        self.qty = self.base * (1 - FEE) / px
        self.cost = self.base
        self.last, self.n = px, 0

    def full(self) -> bool:
        return self.n == self.adds

    def add_down(self, b: float) -> None:
        while self.n < self.adds and b <= self.last * (1 - self.step):
            px = self.last * (1 - self.step)
            size = self.base * self.mult ** (self.n + 1)
            self.qty += size * (1 - FEE) / px
            self.cost += size
            self.last, self.n = px, self.n + 1

    def target(self, tp: float) -> float:
        return self.cost / self.qty * (1 + tp)


def run(c, start: int, hours: int, step: float, mult: float, adds: int, tp: float) -> dict[str, float]:
    deposits, pool = 1.0, 1.0
    active: Ladder | None = None
    stuck: list[Ladder] = []
    cycles = 0
    end = min(start + hours, len(c))
    for h in range(start, end):
        o, hi, lo, cl = c[h]
        if active is None:
            if pool < 0.5 and deposits < MAX_DEPOSITS:
                pool += 1.0
                deposits += 1
            if pool >= 0.5:
                active, pool = Ladder(pool, o, step, mult, adds), 0.0
        pts = [o, hi, lo, cl] if cl < o else [o, lo, hi, cl]
        for a, b in zip(pts, pts[1:]):
            if b < a:
                if active is not None:
                    active.add_down(b)
            else:
                for lad in [active, *stuck]:
                    if lad is not None and b >= lad.target(tp):
                        pool += lad.budget - lad.cost + lad.qty * lad.target(tp) * (1 - FEE)
                        cycles += 1
                        if lad is active:
                            active = None
                        else:
                            stuck.remove(lad)
                if active is None and pool >= 0.5:  # reopen inside the hour at this point's price
                    active, pool = Ladder(pool, b, step, mult, adds), 0.0
        if active is not None and active.full():
            pool += active.budget - active.cost
            active.budget = active.cost
            stuck.append(active)
            active = None
    px = c[end - 1][3]
    equity = pool + sum(l.budget - l.cost + l.qty * px for l in ([active] if active else []) + stuck)
    return {"deposits": deposits, "equity": equity, "mult": equity / deposits, "cycles": cycles}


def hold(c, start: int, hours: int) -> float:
    end = min(start + hours, len(c)) - 1
    return c[end][3] / c[start][0] * (1 - FEE) ** 2


def main() -> None:
    for months in (3, 6, 12):
        hours = months * H_MONTH
        print(f"\n===== horizon {months} months (equity / deposits; doubling = >= 2.0x) =====")
        for inst in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
            c = load(inst)
            starts = range(0, len(c) - hours, 24 * 7)
            hs = [hold(c, s, hours) for s in starts]
            print(f"{inst} ({len(starts)} starts)  hold: median {st.median(hs):.2f}x worst {min(hs):.2f}x "
                  f"best {max(hs):.2f}x  doubled {sum(x >= 2 for x in hs) / len(hs):.0%}")
            for name, p in SETS.items():
                rs = [run(c, s, hours, *p) for s in starts]
                m = [r["mult"] for r in rs]
                print(f"  {name}  median {st.median(m):.2f}x worst {min(m):.2f}x best {max(m):.2f}x  "
                      f"doubled {sum(x >= 2 for x in m) / len(m):.0%}  "
                      f"deposits median {st.median(r['deposits'] for r in rs):.0f} max {max(r['deposits'] for r in rs):.0f}  "
                      f"lost >50% {sum(x < 0.5 for x in m) / len(m):.0%}")


if __name__ == "__main__":
    main()
