"""Exploratory: would US index and gold tokens diversify the crypto Trend Strategy?

Tokens on OKX (XSPY, XQQQ, XAUT...) only exist since 2025-2026, so this uses the underlying
ETFs' adjusted daily closes (Yahoo) as proxies: SPY, QQQ, GLD. Crypto daily UTC closes from OKX.
Calendar: UTC days. An ETF's price on a non-trading day is its last close (return 0), and its
trend signal only updates on trading days. Trend rule identical to live (six votes, vol target 40%),
with lookbacks counted in each asset's own observations and volatility annualised by sqrt(252)
for ETFs. Cost 0.15% of traded value for everything. Not pre-registered: exploratory, trials counted.
"""
from __future__ import annotations

import json
import math
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rotation_study import m  # noqa: E402
from trend_study import ensemble, load  # noqa: E402

COST = 0.0015
TRIALS = 0


def yf(sym: str) -> dict[str, float]:
    d = json.load(open(f"data/candles/YF-{sym}.json"))["chart"]["result"][0]
    return {time.strftime("%Y-%m-%d", time.gmtime(t)): c for t, c in zip(d["timestamp"], d["indicators"]["adjclose"][0]["adjclose"]) if c}


def crypto(inst: str) -> dict[str, float]:
    ts, c = load(inst)
    return {time.strftime("%Y-%m-%d", time.gmtime(t / 1000)): x for t, x in zip(ts, c)}


def weights_on_own_calendar(dates: list[str], closes: list[float], ann: float) -> dict[str, float]:
    ens = ensemble(closes)
    out: dict[str, float] = {}
    for i, d in enumerate(dates):
        if ens[i] is None or i < 31:
            continue
        rets = [math.log(closes[k] / closes[k - 1]) for k in range(i - 29, i + 1)]
        v = st.pstdev(rets) * math.sqrt(ann)
        out[d] = ens[i] * min(1.0, 0.40 / v) if v else ens[i]  # type: ignore[operator]
    return out


def main() -> None:
    global TRIALS
    series = {"BTC": crypto("BTC-USDT"), "ETH": crypto("ETH-USDT"), "SOL": crypto("SOL-USDT"),
              "SPY": yf("SPY"), "QQQ": yf("QQQ"), "GLD": yf("GLD")}
    days = sorted(series["BTC"])
    # forward-fill ETFs onto the UTC calendar
    px: dict[str, dict[str, float]] = {}
    for k, s in series.items():
        last, filled = None, {}
        for d in days:
            if d in s:
                last = s[d]
            if last is not None:
                filled[d] = last
        px[k] = filled
    w: dict[str, dict[str, float]] = {}
    for k, s in series.items():
        ds = sorted(s)
        own = weights_on_own_calendar(ds, [s[d] for d in ds], 365 if k in ("BTC", "ETH", "SOL") else 252)
        last, filled = None, {}
        for d in days:
            if d in own:
                last = own[d]
            if last is not None:
                filled[d] = last
        w[k] = filled

    # --- 1. correlation of returns on US trading days, BTC measured over the same dates
    print("1. correlation with BTC (returns between consecutive US trading days)")
    trade = [d for d in sorted(series["SPY"]) if d in series["BTC"]]
    def rets(k: str, ds: list[str]) -> list[float]:
        return [px[k][ds[i]] / px[k][ds[i - 1]] - 1 for i in range(1, len(ds))]
    for k in ("SPY", "QQQ", "GLD", "ETH"):
        by_year = {}
        for y in range(2018, 2027):
            ds = [d for d in trade if d.startswith(str(y))]
            if len(ds) > 50:
                by_year[y] = st.correlation(rets("BTC", ds), rets(k, ds))
        print(f"   {k}: " + "  ".join(f"{y} {c:+.2f}" for y, c in by_year.items()))
    rb = rets("BTC", trade)
    worst = sorted(range(len(rb)), key=lambda i: rb[i])[: len(rb) // 20]
    print("   on BTC's worst 5% of days (avg BTC {:.1%}):".format(st.mean(rb[i] for i in worst)) + "  ".join(
        f"{k} {st.mean(rets(k, trade)[i] for i in worst):+.2%}" for k in ("SPY", "QQQ", "GLD", "ETH")))

    # --- 2. trend strategy on each asset alone, and portfolios
    def portfolio(keys: tuple[str, ...], d0: str, d1: str = "9999") -> list[float]:
        ds = [d for d in days if d0 <= d < d1 and all(d in w[k] for k in keys)]
        pos = {k: 0.0 for k in keys}
        out = []
        for i in range(len(ds) - 1):
            d, nxt = ds[i], ds[i + 1]
            tgt = {k: w[k][d] / len(keys) for k in keys}
            to = sum(abs(tgt[k] - pos[k]) for k in keys)
            r = {k: px[k][nxt] / px[k][d] - 1 for k in keys}
            g = sum(tgt[k] * r[k] for k in keys)
            out.append((1 - to * COST) * (1 + g) - 1)
            pos = {k: tgt[k] * (1 + r[k]) / (1 + g) for k in keys}
        return out

    def show(name: str, r: list[float]) -> None:
        global TRIALS
        TRIALS += 1
        x = m(r)
        print(f"   {name:34} CAGR {x['cagr']:>6.1%}  vol {x['vol']:>6.1%}  Sharpe {x['sharpe']:>5.2f}  maxDD {x['mdd']:>6.1%}")

    for label, d0, d1 in (("2. BTC+ETH era, 2018-08 .. 2024-12", "2018-08-01", "2025-01-01"),
                          ("   same, 2025-01 .. 2026-09 (holdout period)", "2025-01-01", "9999")):
        print(label)
        for keys in (("BTC", "ETH"), ("SPY",), ("QQQ",), ("GLD",), ("BTC", "ETH", "QQQ", "GLD"), ("BTC", "ETH", "SPY", "GLD")):
            show("trend on " + "+".join(keys), portfolio(keys, d0, d1))
    for label, d0, d1 in (("3. three coins era, 2021-04 .. 2024-12", "2021-04-19", "2025-01-01"),
                          ("   same, 2025-01 .. 2026-09 (holdout period)", "2025-01-01", "9999")):
        print(label)
        for keys in (("BTC", "ETH", "SOL"), ("BTC", "ETH", "SOL", "QQQ", "GLD")):
            show("trend on " + "+".join(keys), portfolio(keys, d0, d1))
    print(f"\ntrials run: {TRIALS}")


if __name__ == "__main__":
    main()
