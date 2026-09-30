"""Fetch and cache OKX 1m candles for Replay. Public endpoints only."""
from __future__ import annotations

import csv
import time
from decimal import Decimal as D
from pathlib import Path
from typing import Callable

import httpx

from .replay import Candle

PUBLIC = "https://www.okx.com"


def load_candles(inst_id: str, days: int, cache_dir: Path, log: Callable[[str], None] = print) -> list[Candle]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{inst_id}-1m.csv"
    cached = _read(path)
    now_ms = int(time.time() * 1000)
    want_from = now_ms - days * 86_400_000
    have_from = cached[0].ts_ms if cached else now_ms
    have_to = cached[-1].ts_ms if cached else 0
    fresh: list[Candle] = []
    with httpx.Client(base_url=PUBLIC, timeout=20) as client:
        if not cached or have_from > want_from:
            fresh += _fetch(client, inst_id, until=have_from if cached else None, since=want_from, log=log)
        if cached and have_to < now_ms - 120_000:
            fresh += _fetch(client, inst_id, until=None, since=have_to + 1, log=log)
    merged = {c.ts_ms: c for c in [*cached, *fresh]}
    out = [merged[k] for k in sorted(merged)]
    _write(path, out)
    return [c for c in out if c.ts_ms >= want_from]


def _fetch(client: httpx.Client, inst_id: str, until: int | None, since: int, log: Callable[[str], None]) -> list[Candle]:
    """Walk backwards with `after` (records earlier than ts) until `since`."""
    got: list[Candle] = []
    after = until
    while True:
        params = {"instId": inst_id, "bar": "1m", "limit": "100"}
        if after is not None:
            params["after"] = str(after)
        r = client.get("/api/v5/market/history-candles", params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("code") != "0":
            raise RuntimeError(f"history-candles: {body}")
        rows = body["data"]
        if not rows:
            break
        batch = [Candle(int(x[0]), D(x[1]), D(x[2]), D(x[3]), D(x[4])) for x in rows if x[8] == "1"]
        got += batch
        oldest = int(rows[-1][0])
        if oldest <= since:
            break
        after = oldest
        if len(got) % 2000 < 100:
            log(f"  fetched {len(got)} candles, back to {time.strftime('%Y-%m-%d %H:%M', time.gmtime(oldest / 1000))}")
        time.sleep(0.12)  # 20 req / 2 s
    return [c for c in got if c.ts_ms >= since]


def _read(path: Path) -> list[Candle]:
    if not path.exists():
        return []
    with path.open() as f:
        return [Candle(int(r[0]), D(r[1]), D(r[2]), D(r[3]), D(r[4])) for r in csv.reader(f)]


def _write(path: Path, rows: list[Candle]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        for c in rows:
            w.writerow([c.ts_ms, c.open, c.high, c.low, c.close])
