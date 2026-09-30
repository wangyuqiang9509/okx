from decimal import Decimal as D

import pytest

from gridbot.engine import (
    EngineError,
    Fill,
    GridEngine,
    GridProfitRealised,
    GridSpec,
    PlaceOrder,
    Side,
    qty_for_capital,
)

MAKER = D("-0.0008")
TAKER = D("-0.001")


def spec(anchor="80000", spacing="0.01", below=3, above=3, qty="0.001"):
    return GridSpec("BTC-USDT", D(anchor), D(spacing), below, above, D(qty), D("0.1"), D("0.00000001"), D("0.00001"))


def buy_fill(order, tid, px=None, size=None, rate=MAKER):
    px = px or order.price
    size = size or order.remaining
    return Fill(tid, order.cl_ord_id, px, size, size * rate, "BTC", 0)


def sell_fill(order, tid, px=None, size=None, rate=MAKER):
    px = px or order.price
    size = size or order.remaining
    return Fill(tid, order.cl_ord_id, px, size, px * size * rate, "USDT", 0)


def seeded_engine():
    eng = GridEngine(spec(), "TEST0001")
    seed = eng.make_seed(D("80000"), D("1000"))
    assert seed.qty == D("0.003")
    eng.on_fill(buy_fill(seed, "seed", rate=TAKER))
    actions = eng.initial_orders()
    return eng, actions


def test_prices_are_geometric_and_tick_rounded():
    s = spec()
    assert s.price(0) == D("80000")
    assert s.price(1) == D("80800.0")
    assert s.price(-1) == D("79207.9")
    assert s.lower == s.price(-3) and s.upper == s.price(3)


def test_qty_for_capital_spends_roughly_all_capital():
    q = qty_for_capital(D("500"), D("80000"), D("0.01"), 8, 8, D("0.001"), D("0.00000001"), D("0.1"))
    s = spec(qty=str(q), below=8, above=8)
    spent = sum(s.price(i) * q for i in range(-8, 0)) + D("80000") * 8 * q * D("1.001")
    assert D("499") < spent <= D("500")
    assert q >= s.min_sz


def test_initial_layout_sells_above_buys_below():
    eng, actions = seeded_engine()
    assert all(isinstance(a, PlaceOrder) for a in actions)
    sides = {a.order.idx: a.order.side for a in actions}
    assert sides == {1: Side.SELL, 2: Side.SELL, 3: Side.SELL, -1: Side.BUY, -2: Side.BUY, -3: Side.BUY}
    assert eng.levels[0] is None
    sells = [a.order for a in actions if a.order.side is Side.SELL]
    # seed received 0.003 * (1 - 0.001) BTC; each sell gets a third of it
    assert all(o.qty == D("0.000999") for o in sells)
    assert all(o.basis_cl_ord_id == eng.seed.cl_ord_id for o in sells)
    assert eng.base_held == D("0.002997")
    assert eng.cash_quote == D("1000") - D("80000") * D("0.003")


def test_buy_fill_places_sell_one_level_up_with_basis():
    eng, _ = seeded_engine()
    buy = eng.levels[-1]
    actions = eng.on_fill(buy_fill(buy, "t1"))
    assert len(actions) == 1 and isinstance(actions[0], PlaceOrder)
    sell = actions[0].order
    assert sell.idx == 0 and sell.side is Side.SELL and sell.price == eng.spec.price(0)
    assert sell.qty == D("0.0009992")  # 0.001 minus 0.08% fee in BTC
    assert sell.basis_quote == buy.price * D("0.001")
    assert sell.basis_cl_ord_id == buy.cl_ord_id
    assert eng.levels[-1] is None and eng.levels[0] is sell


def test_sell_fill_realises_profit_and_places_buy_below():
    eng, _ = seeded_engine()
    buy = eng.levels[-1]
    (place_sell,) = eng.on_fill(buy_fill(buy, "t1"))
    sell = place_sell.order
    actions = eng.on_fill(sell_fill(sell, "t2"))
    assert isinstance(actions[0], GridProfitRealised)
    proceeds = sell.price * sell.qty * (1 + MAKER)
    assert actions[0].profit == proceeds - buy.price * D("0.001")
    assert actions[0].profit > 0
    assert isinstance(actions[1], PlaceOrder)
    assert actions[1].order.idx == -1 and actions[1].order.side is Side.BUY
    assert eng.round_trips == 1 and eng.realised_profit == actions[0].profit


def test_full_sweep_down_then_up_conserves_value():
    eng, _ = seeded_engine()
    start_cash, start_base = eng.cash_quote, eng.base_held
    # price falls through every buy Level
    for idx in (-1, -2, -3):
        eng.on_fill(buy_fill(eng.levels[idx], f"b{idx}"))
    assert eng.resting_orders() and all(o.side is Side.SELL for o in eng.resting_orders())
    assert eng.levels[-3] is None  # lower bound: nothing more to do
    # then rises through every sell Level
    for idx in range(-2, 4):
        eng.on_fill(sell_fill(eng.levels[idx], f"s{idx}"))
    assert all(o.side is Side.BUY for o in eng.resting_orders())
    assert eng.levels[3] is None  # upper bound: holding only USDT plus dust
    assert eng.base_held < D("0.00001")
    assert eng.cash_quote > start_cash + start_base * D("80000")
    assert eng.round_trips == 6


def test_partial_fills_accumulate_and_complete_once():
    eng, _ = seeded_engine()
    buy = eng.levels[-1]
    assert eng.on_fill(buy_fill(buy, "p1", size=D("0.0004"))) == []
    assert eng.on_fill(buy_fill(buy, "p1", size=D("0.0004"))) == []  # duplicate trade id ignored
    actions = eng.on_fill(buy_fill(buy, "p2", size=D("0.0006")))
    assert len(actions) == 1 and actions[0].order.qty == D("0.0009992")


def test_snapshot_catch_up_uses_fee_delta():
    eng, _ = seeded_engine()
    buy = eng.levels[-1]
    eng.on_fill(buy_fill(buy, "w1", size=D("0.0004")))
    total_fee = D("0.001") * MAKER
    actions = eng.on_order_done(buy.cl_ord_id, D("0.001"), buy.price, total_fee, "BTC", 0)
    assert actions[0].order.qty == D("0.0009992")
    assert eng.on_order_done(buy.cl_ord_id, D("0.001"), buy.price, total_fee, "BTC", 0) == []


def test_cancelled_order_is_replaced_at_same_level():
    eng, _ = seeded_engine()
    buy = eng.levels[-1]
    (replace,) = eng.on_order_cancelled(buy.cl_ord_id)
    assert replace.order.idx == -1 and replace.order.cl_ord_id != buy.cl_ord_id
    assert replace.order.price == buy.price and replace.order.qty == buy.qty
    assert eng.find(buy.cl_ord_id) is None


def test_unknown_fill_is_an_invariant_error():
    eng, _ = seeded_engine()
    with pytest.raises(EngineError):
        eng.on_fill(Fill("x", "nope", D(1), D(1), D(0), "BTC", 0))


def test_state_round_trip():
    eng, _ = seeded_engine()
    eng.on_fill(buy_fill(eng.levels[-1], "t1"))
    eng.unplaced.add("abc")
    clone = GridEngine.from_state(eng.state_dict())
    assert clone.state_dict() == eng.state_dict()
    assert clone.find(eng.levels[0].cl_ord_id) is not None


def test_cl_ord_id_is_alnum_and_short():
    eng, actions = seeded_engine()
    for a in actions:
        assert a.order.cl_ord_id.isalnum() and len(a.order.cl_ord_id) <= 32


def test_out_of_order_sell_fills_defer_instead_of_failing():
    """Price sweeps up through L-1 and L0 but the L0 fill is reported first."""
    eng, _ = seeded_engine()
    eng.on_fill(buy_fill(eng.levels[-1], "b1"))  # sell now at L0
    eng.on_fill(buy_fill(eng.levels[-2], "b2"))  # sell now at L-1
    sell_l0 = eng.levels[0]
    sell_lm1 = eng.levels[-1]
    assert sell_l0.side is Side.SELL and sell_lm1.side is Side.SELL
    acts = eng.on_fill(sell_fill(sell_l0, "s0"))  # L0 first: its buy-back belongs at L-1, still held
    assert [type(a) for a in acts] == [GridProfitRealised]
    assert len(eng.deferred) == 1 and eng.deferred[0].idx == -1
    acts = eng.on_fill(sell_fill(sell_lm1, "s1"))  # now L-1 frees: deferred buy takes it
    placed = [a.order for a in acts if isinstance(a, PlaceOrder)]
    assert {(o.idx, o.side) for o in placed} == {(-1, Side.BUY), (-2, Side.BUY)}
    assert eng.deferred == []
    assert eng.levels[-1].side is Side.BUY and eng.levels[-2].side is Side.BUY


def test_out_of_order_buy_fills_defer_instead_of_failing():
    """Price sweeps down through L-1 and L-2 but the L-2 fill is reported first."""
    eng, _ = seeded_engine()
    b1, b2 = eng.levels[-1], eng.levels[-2]
    acts = eng.on_fill(buy_fill(b2, "b2"))  # its sell belongs at L-1, still held by b1
    assert acts == [] and len(eng.deferred) == 1
    acts = eng.on_fill(buy_fill(b1, "b1"))
    placed = {(a.order.idx, a.order.side) for a in acts if isinstance(a, PlaceOrder)}
    assert placed == {(-1, Side.SELL), (0, Side.SELL)}
    assert eng.deferred == []


def test_deferred_orders_survive_state_round_trip():
    eng, _ = seeded_engine()
    eng.on_fill(buy_fill(eng.levels[-2], "b2"))
    clone = GridEngine.from_state(eng.state_dict())
    assert len(clone.deferred) == 1 and clone.state_dict() == eng.state_dict()
