"""Monthly realised Grid Profit vs total equity change for the multi.toml mix, 180 days.

Rolling 30-day windows every 15 days, fresh grid each window. Reports per coin and for
the equal-weight mix (BTC 1%, ETH 2%, SOL 2%, 8 levels each side).
"""
from __future__ import annotations

import statistics as st
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gridbot.candles import _read  # noqa: E402
from gridbot.replay import run_replay  # noqa: E402

MIX = {"BTC-USDT": ("1.0", D("0.1"), D("0.00000001"), D("0.00001")),
       "ETH-USDT": ("2.0", D("0.01"), D("0.000001"), D("0.0001")),
       "SOL-USDT": ("2.0", D("0.01"), D("0.000001"), D("0.01"))}
CAP = D("10000")
W, STEP = 30 * 1440, 15 * 1440

per: dict[str, list[tuple[float, float, float]]] = {}
for inst, (sp, tick, lot, mn) in MIX.items():
    cs = _read(Path("data/candles") / f"{inst}-1m.csv")[-180 * 1440:]
    rows = []
    for s in range(0, len(cs) - W + 1, STEP):
        r = run_replay(cs[s:s + W], D(sp) / 100, 8, 8, CAP, D("-0.0008"), D("-0.001"), tick, lot, mn, inst)
        rows.append((float(r.realised_profit / CAP * 100), float((r.final_equity / CAP - 1) * 100), float((r.buy_and_hold_equity / CAP - 1) * 100)))
    per[inst] = rows
    rp = [x[0] for x in rows]; eq = [x[1] for x in rows]
    print(f"{inst} {sp}%: realised median {st.median(rp):.2f}% mean {st.mean(rp):.2f}% min {min(rp):.2f}% max {max(rp):.2f}% | equity median {st.median(eq):+.2f}% worst {min(eq):+.2f}%")

n = len(per["BTC-USDT"])
mix_rp = [st.mean(per[i][k][0] for i in MIX) for k in range(n)]
mix_eq = [st.mean(per[i][k][1] for i in MIX) for k in range(n)]
mix_hold = [st.mean(per[i][k][2] for i in MIX) for k in range(n)]
print(f"\nMIX realised per window: {[round(x, 2) for x in mix_rp]}")
print(f"MIX equity   per window: {[round(x, 2) for x in mix_eq]}")
print(f"MIX hold     per window: {[round(x, 2) for x in mix_hold]}")
print(f"MIX realised: median {st.median(mix_rp):.2f}% mean {st.mean(mix_rp):.2f}% min {min(mix_rp):.2f}% max {max(mix_rp):.2f}%")
print(f"MIX equity:   median {st.median(mix_eq):+.2f}% mean {st.mean(mix_eq):+.2f}% worst {min(mix_eq):+.2f}% best {max(mix_eq):+.2f}%")
