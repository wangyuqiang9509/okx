from __future__ import annotations

import csv
import math
import random
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from gridbot.trend import MIN_HISTORY, Market, backtest, plan, signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))


def walk(n: int, seed: int, drift: float = 0.0) -> list[float]:
    rnd = random.Random(seed)
    px, out = 100.0, []
    for _ in range(n):
        px *= math.exp(rnd.gauss(drift, 0.04))
        out.append(px)
    return out


def test_signal_matches_research_implementation():
    from trend_study import ensemble, vol_scale
    for seed in range(5):
        c = walk(400, seed, 0.001 * (seed - 2))
        ens, vt = ensemble(c), vol_scale(c, ensemble(c))
        for i in range(MIN_HISTORY, len(c)):
            s = signal(c[: i + 1], 0.40)
            assert s.ensemble == pytest.approx(ens[i])
            assert s.weight == pytest.approx(vt[i])


def test_signal_extremes():
    up = [100 * 1.01 ** k for k in range(300)]
    down = list(reversed(up))
    assert signal(up, 0.4).ensemble == 1.0
    assert signal(down, 0.4).ensemble == 0.0 and signal(down, 0.4).weight == 0.0
    with pytest.raises(ValueError):
        signal(up[:100], 0.4)


def test_high_volatility_scales_weight_down():
    rnd = random.Random(1)
    c = [100.0]
    for _ in range(299):
        c.append(c[-1] * math.exp(0.003 + rnd.choice((-0.08, 0.08))))  # ~150% annual vol, trending up
    s = signal(c, 0.4)
    assert s.vol > 1.0 and s.weight < s.ensemble * 0.5


MK = [Market("BTC-USDT", "BTC", D("80000"), D("0.00000001"), D("0.00001")),
      Market("ETH-USDT", "ETH", D("2500"), D("0.000001"), D("0.0001")),
      Market("SOL-USDT", "SOL", D("120"), D("0.000001"), D("0.01"))]


def test_plan_from_all_cash_buys_to_targets():
    p = plan(D("300"), {}, MK, {"BTC-USDT": 1.0, "ETH-USDT": 0.5, "SOL-USDT": 0.0}, D("0.05"), D("5"))
    by = {x.inst_id: x for x in p}
    assert by["BTC-USDT"].trade.side == "buy" and by["BTC-USDT"].trade.notional <= D("100")
    assert by["ETH-USDT"].trade.notional == pytest.approx(D("50"), abs=D("0.01"))
    assert by["SOL-USDT"].trade is None


def test_plan_respects_band_and_minimum():
    holdings = {"BTC": D("0.00125")}  # 100 USDT at 80k
    p = plan(D("200"), holdings, MK, {"BTC-USDT": 0.97, "ETH-USDT": 0.0, "SOL-USDT": 0.02}, D("0.05"), D("5"))
    assert all(x.trade is None for x in p)  # 3% drift < 5% band; SOL target 2 USDT < minimum trade


def test_plan_exit_sells_everything():
    holdings = {"SOL": D("0.5")}
    p = plan(D("240"), holdings, MK, {"BTC-USDT": 0.0, "ETH-USDT": 0.0, "SOL-USDT": 0.0}, D("0.05"), D("5"))
    sol = next(x for x in p if x.inst_id == "SOL-USDT")
    assert sol.trade.side == "sell" and sol.trade.qty == D("0.5")


def test_plan_buys_never_exceed_cash_plus_sell_proceeds():
    holdings = {"BTC": D("0.001")}  # 80 USDT of BTC, 20 cash: sleeve 33.3 each
    p = plan(D("20"), holdings, MK, {"BTC-USDT": 1.0, "ETH-USDT": 1.0, "SOL-USDT": 1.0}, D("0.05"), D("1"))
    sells = sum(x.trade.notional for x in p if x.trade and x.trade.side == "sell")
    buys = sum(x.trade.notional for x in p if x.trade and x.trade.side == "buy")
    assert sells > 0
    assert buys <= (D("20") + sells * D("0.999")) / D("1.001")


def test_plan_scales_buys_when_cash_is_short():
    # prices moved: holdings worth more than their targets cannot be sold (within band), cash is tiny
    holdings = {"BTC": D("0.00124")}  # 99.2 USDT; sleeve ~ (1 + 99.2)/3
    p = plan(D("1"), holdings, MK, {"BTC-USDT": 1.0, "ETH-USDT": 1.0, "SOL-USDT": 1.0}, D("0.05"), D("0.5"))
    buys = sum(x.trade.notional for x in p if x.trade and x.trade.side == "buy")
    sells = sum(x.trade.notional for x in p if x.trade and x.trade.side == "sell")
    assert buys <= (D("1") + sells * D("0.999")) / D("1.001")


def load_1d(inst):
    path = Path("data/candles") / f"{inst}-1D.csv"
    if not path.exists():
        pytest.skip("daily candles not downloaded (research/fetch_1d.py)")
    with open(path) as f:
        return [(int(r[0]), float(r[4])) for r in csv.reader(f)]


def test_backtest_with_live_rules_is_close_to_research():
    """The live rule (5% band, 5 USDT minimum) should not drift far from the frictionless research curve."""
    from trend_study import ensemble, equity, vol_scale
    data = {i: load_1d(i) for i in ("BTC-USDT", "ETH-USDT", "SOL-USDT")}
    t0 = max(v[0][0] for v in data.values())
    closes = {i: [c for t, c in v if t >= t0] for i, v in data.items()}
    n = min(len(v) for v in closes.values())
    closes = {i: v[:n] for i, v in closes.items()}
    live = backtest(closes, 200, 0.40, 0.05, 5.0, 10_000.0)
    sleeves = [equity(c, vol_scale(c, ensemble(c)), 200) for c in closes.values()]
    research = [1.0]
    for k in range(1, len(sleeves[0])):
        research.append(research[-1] * sum(e[k] / e[k - 1] for e in sleeves) / 3)
    years = (len(live) - 1) / 365
    cagr_live = (live[-1] / live[0]) ** (1 / years) - 1
    cagr_research = research[-1] ** (1 / years) - 1
    assert abs(cagr_live - cagr_research) < 0.03, (cagr_live, cagr_research)
