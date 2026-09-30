"""Compare fixed grids on BTC, ETH, SOL over rolling 30-day windows of the last 180 days.

Each window starts a fresh grid at that window's first price, like a monthly reset.
Reports per coin and spacing: median and worst window, how often the grid beat holding,
and an equal-weight three-coin portfolio.
"""
from __future__ import annotations

import statistics as st
import sys
from decimal import Decimal as D
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gridbot.candles import _read  # noqa: E402
from gridbot.replay import run_replay  # noqa: E402

CAP = D("300")
WINDOW = 30 * 1440
STEP = 15 * 1440  # half-overlapping windows
LEVELS = 8
SPACINGS = ("0.5", "1.0", "1.5", "2.0")
MAKER, TAKER = D("-0.0008"), D("-0.001")


def meta(inst: str) -> tuple[D, D, D]:
    m = httpx.get("https://www.okx.com/api/v5/public/instruments", params={"instType": "SPOT", "instId": inst}).json()["data"][0]
    return D(m["tickSz"]), D(m["lotSz"]), D(m["minSz"])


def main() -> None:
    results: dict[tuple[str, str], list[tuple[float, float, float, float]]] = {}
    for inst in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        cs = _read(Path("data/candles") / f"{inst}-1m.csv")
        cs = cs[-180 * 1440 :]
        tick, lot, mn = meta(inst)
        first, last = float(cs[0].close), float(cs[-1].close)
        print(f"{inst}: {len(cs) / 1440:.0f} days, {first:.2f} -> {last:.2f} ({(last / first - 1) * 100:+.1f}%)")
        for sp in SPACINGS:
            rows = []
            for s in range(0, len(cs) - WINDOW + 1, STEP):
                r = run_replay(cs[s : s + WINDOW], D(sp) / 100, LEVELS, LEVELS, CAP, MAKER, TAKER, tick, lot, mn, inst)
                rows.append((
                    float((r.final_equity / CAP - 1) * 100),
                    float((r.buy_and_hold_equity / CAP - 1) * 100),
                    float(r.max_drawdown_pct),
                    r.minutes_outside_range / r.candles * 100,
                ))
            results[(inst, sp)] = rows

    n = len(next(iter(results.values())))
    print(f"\n{n} rolling 30-day windows per row, capital {CAP}, {LEVELS} levels each side\n")
    print(f"{'inst':9} {'spacing':>7} {'median%':>8} {'worst%':>7} {'best%':>7} {'hold med%':>9} {'beat hold':>9} {'worstDD%':>8} {'outside%':>8}")
    for (inst, sp), rows in results.items():
        eq = [r[0] for r in rows]
        hold = [r[1] for r in rows]
        beat = sum(1 for r in rows if r[0] > r[1])
        print(f"{inst:9} {sp + '%':>7} {st.median(eq):>+8.2f} {min(eq):>+7.2f} {max(eq):>+7.2f} {st.median(hold):>+9.2f} {beat:>4}/{len(rows):<4} {max(r[2] for r in rows):>8.2f} {st.mean(r[3] for r in rows):>8.1f}")

    print("\nequal-weight portfolio of the three coins, same spacing on each:")
    for sp in SPACINGS:
        port = [st.mean(results[(i, sp)][k][0] for i in ("BTC-USDT", "ETH-USDT", "SOL-USDT")) for k in range(n)]
        hold = [st.mean(results[(i, sp)][k][1] for i in ("BTC-USDT", "ETH-USDT", "SOL-USDT")) for k in range(n)]
        print(f"  spacing {sp}%: median {st.median(port):+.2f}%  worst {min(port):+.2f}%  best {max(port):+.2f}%   hold median {st.median(hold):+.2f}% worst {min(hold):+.2f}%")


if __name__ == "__main__":
    main()
