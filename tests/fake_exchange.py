"""In-memory OKX stand-in for Supervisor tests. Fills are exact; fees follow OKX conventions
(buy fee charged in base, sell fee in quote, both negative)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal as D

from gridbot.okx import Balance, Fees, Instrument, OrderSnapshot, PlaceResult, Ticker, WsFill, WsOrderUpdate

MAKER, TAKER = D("-0.0008"), D("-0.001")

INSTRUMENTS = {
    "BTC-USDT": Instrument("BTC-USDT", "BTC", "USDT", D("0.1"), D("0.00000001"), D("0.00001")),
    "ETH-USDT": Instrument("ETH-USDT", "ETH", "USDT", D("0.01"), D("0.000001"), D("0.0001")),
    "SOL-USDT": Instrument("SOL-USDT", "SOL", "USDT", D("0.01"), D("0.000001"), D("0.01")),
}


@dataclass
class FakeOrder:
    inst_id: str
    cl_ord_id: str
    ord_id: str
    side: str
    px: D
    sz: D
    filled: D = D(0)
    fee: D = D(0)
    fee_ccy: str = ""
    state: str = "live"
    notional: D = D(0)
    c_time: int = 0
    u_time: int = 0

    def snap(self) -> OrderSnapshot:
        avg = self.notional / self.filled if self.filled else D(0)
        return OrderSnapshot(self.inst_id, self.cl_ord_id, self.ord_id, self.state, self.side, self.px, self.sz,
                             self.filled, avg, self.fee, self.fee_ccy, self.u_time, "", self.c_time)


@dataclass
class FakeExchange:
    prices: dict[str, D]
    bal: dict[str, D]
    orders: dict[str, FakeOrder] = field(default_factory=dict)
    seq: int = 0
    trade_seq: int = 0
    clock: int = 1_700_000_000_000
    daily: dict[str, list[tuple[int, D]]] = field(default_factory=dict)

    async def daily_closes(self, inst_id: str, n: int = 300) -> list[tuple[int, D]]:
        return self.daily.get(inst_id, [])[-n:]

    def tick(self) -> int:
        self.clock += 1000
        return self.clock

    async def instrument(self, inst_id: str) -> Instrument:
        return INSTRUMENTS[inst_id]

    async def ticker(self, inst_id: str) -> Ticker:
        px = self.prices[inst_id]
        return Ticker(px, px - INSTRUMENTS[inst_id].tick_sz, px)

    async def fees(self, inst_id: str) -> Fees:
        return Fees(MAKER, TAKER)

    def frozen(self, ccy: str) -> D:
        out = D(0)
        for o in self.orders.values():
            if o.state not in ("live", "partially_filled"):
                continue
            base, quote = o.inst_id.split("-")
            if o.side == "buy" and quote == ccy:
                out += o.px * (o.sz - o.filled)
            if o.side == "sell" and base == ccy:
                out += o.sz - o.filled
        return out

    async def balances(self, *ccys: str) -> dict[str, Balance]:
        return {c: Balance(c, self.bal.get(c, D(0)), self.bal.get(c, D(0)) - self.frozen(c)) for c in ccys}

    async def place_orders(self, inst_id: str, orders: list[dict[str, str]]) -> list[PlaceResult]:
        out = []
        for o in orders:
            self.seq += 1
            now = self.tick()
            fo = FakeOrder(inst_id, o["clOrdId"], str(self.seq), o["side"], D(o["px"]), D(o["sz"]), c_time=now, u_time=now)
            last = self.prices[inst_id]
            if o["ordType"] == "post_only" and ((fo.side == "buy" and fo.px >= last) or (fo.side == "sell" and fo.px < last)):
                out.append(PlaceResult(fo.cl_ord_id, "", False, "51124", "post_only would cross"))
                continue
            self.orders[fo.cl_ord_id] = fo
            crossed = (fo.side == "buy" and fo.px >= last) or (fo.side == "sell" and fo.px <= last)
            if o["ordType"] == "limit" and crossed:  # a plain limit that crosses takes liquidity at once
                self._fill(fo, last, TAKER)
            if o["ordType"] == "ioc":
                self._fill(fo, last, TAKER)
                if fo.state != "filled":
                    fo.state = "canceled"
            out.append(PlaceResult(fo.cl_ord_id, fo.ord_id, True, "0", ""))
        return out

    async def cancel_orders(self, inst_id: str, cl_ord_ids: list[str]) -> list[PlaceResult]:
        for c in cl_ord_ids:
            self.orders[c].state = "canceled"
            self.orders[c].u_time = self.tick()
        return [PlaceResult(c, self.orders[c].ord_id, True, "0", "") for c in cl_ord_ids]

    async def order(self, inst_id: str, cl_ord_id: str) -> OrderSnapshot | None:
        o = self.orders.get(cl_ord_id)
        return o.snap() if o else None

    async def orders_history(self, inst_id: str) -> list[OrderSnapshot]:
        done = [o for o in self.orders.values() if o.inst_id == inst_id and o.state in ("filled", "canceled")]
        return [o.snap() for o in sorted(done, key=lambda o: -o.u_time)]

    def exchange_cancel(self, cl_ord_id: str) -> WsOrderUpdate:
        o = self.orders[cl_ord_id]
        o.state, o.u_time = "canceled", self.tick()
        return WsOrderUpdate(o.snap(), None)

    async def pending_orders(self, inst_id: str | None = None) -> list[OrderSnapshot]:
        return [o.snap() for o in self.orders.values()
                if o.state in ("live", "partially_filled") and (inst_id is None or o.inst_id == inst_id)]

    # ----- market simulation ------------------------------------------------------------
    def _fill(self, o: FakeOrder, px: D, rate: D) -> WsFill:
        base, quote = o.inst_id.split("-")
        sz = o.sz - o.filled
        self.trade_seq += 1
        if o.side == "buy":
            fee, ccy = sz * rate, base
            self.bal[quote] = self.bal.get(quote, D(0)) - px * sz
            self.bal[base] = self.bal.get(base, D(0)) + sz + fee
        else:
            fee, ccy = px * sz * rate, quote
            self.bal[base] = self.bal.get(base, D(0)) - sz
            self.bal[quote] = self.bal.get(quote, D(0)) + px * sz + fee
        o.filled += sz
        o.notional += px * sz
        o.fee += fee
        o.fee_ccy = ccy
        o.state = "filled"
        o.u_time = self.tick()
        return WsFill(o.cl_ord_id, o.ord_id, f"t{self.trade_seq}", px, sz, fee, ccy, 0)

    def move(self, inst_id: str, px: D) -> list[WsOrderUpdate]:
        """Set the price and fill every resting order it crosses, at the order's price."""
        self.prices[inst_id] = px
        ups = []
        for o in list(self.orders.values()):
            if o.inst_id != inst_id or o.state not in ("live", "partially_filled"):
                continue
            if (o.side == "buy" and px <= o.px) or (o.side == "sell" and px >= o.px):
                f = self._fill(o, o.px, MAKER)
                ups.append(WsOrderUpdate(o.snap(), f))
        return ups
