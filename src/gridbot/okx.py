"""Thin OKX v5 client: REST signing, the private orders WebSocket, and the few endpoints the grid needs."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
import websockets

from .config import Credentials

REST_BASE = "https://www.okx.com"
WS_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"

log = logging.getLogger("okx")


class OkxError(Exception):
    def __init__(self, code: str, msg: str, data: Any = None) -> None:
        super().__init__(f"okx {code}: {msg}")
        self.code = code
        self.msg = msg
        self.data = data


@dataclass(frozen=True)
class Instrument:
    inst_id: str
    base_ccy: str
    quote_ccy: str
    tick_sz: D
    lot_sz: D
    min_sz: D


@dataclass(frozen=True)
class Ticker:
    last: D
    bid: D
    ask: D


@dataclass(frozen=True)
class Fees:
    maker: D  # negative, e.g. -0.0008
    taker: D


@dataclass(frozen=True)
class Balance:
    ccy: str
    total: D  # cashBal: includes what is frozen in resting orders
    avail: D


@dataclass(frozen=True)
class OrderSnapshot:
    cl_ord_id: str
    ord_id: str
    state: str  # live | partially_filled | filled | canceled | mmp_canceled
    side: str
    px: D
    sz: D
    acc_fill_sz: D
    avg_px: D
    fee: D
    fee_ccy: str
    u_time_ms: int
    cancel_source: str


@dataclass(frozen=True)
class PlaceResult:
    cl_ord_id: str
    ord_id: str
    ok: bool
    code: str
    msg: str


def _sign(secret: str, prehash: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()).decode()


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _snapshot(o: dict[str, Any]) -> OrderSnapshot:
    return OrderSnapshot(
        cl_ord_id=o.get("clOrdId", ""),
        ord_id=o.get("ordId", ""),
        state=o["state"],
        side=o["side"],
        px=D(o["px"] or "0"),
        sz=D(o["sz"] or "0"),
        acc_fill_sz=D(o.get("accFillSz") or "0"),
        avg_px=D(o.get("avgPx") or "0"),
        fee=D(o.get("fee") or "0"),
        fee_ccy=o.get("feeCcy") or "",
        u_time_ms=int(o.get("uTime") or 0),
        cancel_source=o.get("cancelSource") or "",
    )


class OkxRest:
    def __init__(self, creds: Credentials | None, base: str = REST_BASE) -> None:
        self.creds = creds
        self.http = httpx.AsyncClient(base_url=base, timeout=15)

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, path: str, params: dict[str, str] | None = None, body: Any = None) -> Any:
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json"}
        if self.creds is not None:
            ts = _ts()
            headers |= {
                "OK-ACCESS-KEY": self.creds.api_key,
                "OK-ACCESS-SIGN": _sign(self.creds.secret_key, ts + method + path + query + payload),
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.creds.passphrase,
            }
        r = await self.http.request(method, path + query, content=payload or None, headers=headers)
        try:
            data = r.json()
        except ValueError:
            raise OkxError(str(r.status_code), r.text[:200]) from None
        if data.get("code") != "0":
            raise OkxError(data.get("code", "?"), data.get("msg", ""), data.get("data"))
        return data["data"]

    # ----- public --------------------------------------------------------------
    async def instrument(self, inst_id: str) -> Instrument:
        (d,) = await self._request("GET", "/api/v5/public/instruments", {"instType": "SPOT", "instId": inst_id})
        if d["state"] != "live":
            raise OkxError("state", f"{inst_id} is {d['state']}")
        return Instrument(inst_id, d["baseCcy"], d["quoteCcy"], D(d["tickSz"]), D(d["lotSz"]), D(d["minSz"]))

    async def ticker(self, inst_id: str) -> Ticker:
        (d,) = await self._request("GET", "/api/v5/market/ticker", {"instId": inst_id})
        return Ticker(D(d["last"]), D(d["bidPx"]), D(d["askPx"]))

    async def server_time_ms(self) -> int:
        (d,) = await self._request("GET", "/api/v5/public/time")
        return int(d["ts"])

    # ----- account ---------------------------------------------------------------
    async def fees(self, inst_id: str) -> Fees:
        (d,) = await self._request("GET", "/api/v5/account/trade-fee", {"instType": "SPOT", "instId": inst_id})
        return Fees(D(d["maker"]), D(d["taker"]))

    async def balances(self, *ccys: str) -> dict[str, Balance]:
        (d,) = await self._request("GET", "/api/v5/account/balance", {"ccy": ",".join(ccys)})
        out = {c: Balance(c, D(0), D(0)) for c in ccys}
        for x in d["details"]:
            out[x["ccy"]] = Balance(x["ccy"], D(x["cashBal"] or "0"), D(x["availBal"] or "0"))
        return out

    # ----- orders ------------------------------------------------------------------
    async def place_orders(self, inst_id: str, orders: list[dict[str, str]]) -> list[PlaceResult]:
        """post_only / ioc limit orders, at most 20 per call. Never raises on a per-order rejection."""
        results: list[PlaceResult] = []
        for i in range(0, len(orders), 20):
            chunk = [{"instId": inst_id, "tdMode": "cash", **o} for o in orders[i : i + 20]]
            try:
                data = await self._request("POST", "/api/v5/trade/batch-orders", body=chunk)
            except OkxError as e:
                # code 1 = all failed, 2 = partial; data carries per-order sCode
                if e.data is None:
                    raise
                data = e.data
            for o, r in zip(chunk, data):
                results.append(PlaceResult(o["clOrdId"], r.get("ordId", ""), r.get("sCode") == "0", r.get("sCode", "?"), r.get("sMsg", "")))
        return results

    async def cancel_orders(self, inst_id: str, cl_ord_ids: list[str]) -> list[PlaceResult]:
        results: list[PlaceResult] = []
        for i in range(0, len(cl_ord_ids), 20):
            chunk = [{"instId": inst_id, "clOrdId": c} for c in cl_ord_ids[i : i + 20]]
            try:
                data = await self._request("POST", "/api/v5/trade/cancel-batch-orders", body=chunk)
            except OkxError as e:
                if e.data is None:
                    raise
                data = e.data
            for o, r in zip(chunk, data):
                results.append(PlaceResult(o["clOrdId"], r.get("ordId", ""), r.get("sCode") == "0", r.get("sCode", "?"), r.get("sMsg", "")))
        return results

    async def order(self, inst_id: str, cl_ord_id: str) -> OrderSnapshot | None:
        try:
            (d,) = await self._request("GET", "/api/v5/trade/order", {"instId": inst_id, "clOrdId": cl_ord_id})
        except OkxError as e:
            if e.code in ("51603", "51001"):  # order does not exist
                return None
            raise
        return _snapshot(d)

    async def pending_orders(self, inst_id: str) -> list[OrderSnapshot]:
        out: list[OrderSnapshot] = []
        after = ""
        while True:
            params = {"instType": "SPOT", "instId": inst_id, "limit": "100"}
            if after:
                params["after"] = after
            data = await self._request("GET", "/api/v5/trade/orders-pending", params)
            out += [_snapshot(o) for o in data]
            if len(data) < 100:
                return out
            after = data[-1]["ordId"]


@dataclass(frozen=True)
class WsFill:
    cl_ord_id: str
    ord_id: str
    trade_id: str
    px: D
    sz: D
    fee: D
    fee_ccy: str
    ts_ms: int


@dataclass(frozen=True)
class WsOrderUpdate:
    snapshot: OrderSnapshot
    fill: WsFill | None


async def private_orders_stream(
    creds: Credentials, inst_id: str, url: str = WS_PRIVATE, on_connect: Callable[[], Awaitable[None]] | None = None
) -> AsyncIterator[WsOrderUpdate]:
    """Yields order updates forever, reconnecting with backoff. `on_connect` runs after every (re)subscribe."""
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=None, open_timeout=15) as ws:
                ts = str(int(time.time()))
                await ws.send(json.dumps({"op": "login", "args": [{
                    "apiKey": creds.api_key, "passphrase": creds.passphrase, "timestamp": ts,
                    "sign": _sign(creds.secret_key, ts + "GET" + "/users/self/verify"),
                }]}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if resp.get("event") != "login" or resp.get("code") != "0":
                    raise OkxError(str(resp.get("code")), f"ws login failed: {resp}")
                await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "orders", "instType": "SPOT", "instId": inst_id}]}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if resp.get("event") != "subscribe":
                    raise OkxError(str(resp.get("code")), f"ws subscribe failed: {resp}")
                log.info("ws connected and subscribed to orders %s", inst_id)
                backoff = 1.0
                if on_connect is not None:
                    await on_connect()
                last_ping = time.monotonic()
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        if time.monotonic() - last_ping > 20:
                            await ws.send("ping")
                            last_ping = time.monotonic()
                        continue
                    if raw == "pong":
                        continue
                    msg = json.loads(raw)
                    if "event" in msg:
                        if msg["event"] == "error":
                            raise OkxError(str(msg.get("code")), str(msg.get("msg")))
                        continue
                    for o in msg.get("data", []):
                        snap = _snapshot(o)
                        fill = None
                        if o.get("tradeId") and D(o.get("fillSz") or "0") > 0:
                            fill = WsFill(
                                snap.cl_ord_id, snap.ord_id, o["tradeId"], D(o["fillPx"]), D(o["fillSz"]),
                                D(o.get("fillFee") or "0"), o.get("fillFeeCcy") or "", int(o.get("fillTime") or snap.u_time_ms),
                            )
                        yield WsOrderUpdate(snap, fill)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - any transport error means reconnect
            log.warning("ws disconnected: %s; reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
