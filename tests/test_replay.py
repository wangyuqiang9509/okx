from decimal import Decimal as D

from gridbot.replay import Candle, run_replay


def candle(i, o, h, l, c):
    return Candle(i * 60_000, D(o), D(h), D(l), D(c))


def test_oscillation_inside_range_earns_grid_profit():
    # 80000 -> down 2% -> back up 2%, repeated. Spacing 1%, 3 levels each side.
    cs = []
    px = D("80000")
    for k in range(20):
        lo = px * D("0.98")
        cs.append(candle(2 * k, px, px, lo, lo))
        cs.append(candle(2 * k + 1, lo, px, lo, px))
    r = run_replay(cs, D("0.01"), 3, 3, D("1000"), D("-0.0008"), D("-0.001"))
    assert r.round_trips >= 20
    assert r.realised_profit > 0
    assert r.minutes_outside_range == 0
    assert r.final_equity > D("1000")


def test_crash_below_range_ends_fully_in_btc_with_no_further_action():
    cs = [candle(0, "80000", "80000", "80000", "80000"), candle(1, "80000", "80000", "70000", "70000"), candle(2, "70000", "70000", "60000", "60000")]
    r = run_replay(cs, D("0.01"), 3, 3, D("1000"), D("-0.0008"), D("-0.001"))
    assert r.round_trips == 0
    assert r.minutes_outside_range == 2
    assert r.first_breakout_at == 1
    # every buy filled, nothing sold: equity tracks BTC price
    assert r.final_equity < D("1000")
    assert r.max_drawdown_pct > 20


def test_rally_above_range_ends_in_usdt():
    cs = [candle(0, "80000", "80000", "80000", "80000"), candle(1, "80000", "90000", "80000", "90000"), candle(2, "90000", "95000", "90000", "95000")]
    r = run_replay(cs, D("0.01"), 3, 3, D("1000"), D("-0.0008"), D("-0.001"))
    assert r.round_trips == 3  # the three seeded sells
    assert r.realised_profit > 0
    assert r.final_equity < r.buy_and_hold_equity
    assert r.minutes_outside_range == 2
