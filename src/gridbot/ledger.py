"""SQLite Ledger: every Order, Fill, Grid Profit, Halt and Reconciliation, plus the engine state snapshot."""
from __future__ import annotations

import json
import sqlite3
from typing import cast
import time
from decimal import Decimal as D
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS grids (
  id TEXT PRIMARY KEY, created_ms INTEGER, status TEXT, halt_reason TEXT,
  config_path TEXT, spec_json TEXT, state_json TEXT,
  quote_offset TEXT, base_offset TEXT, updated_ms INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
  cl_ord_id TEXT PRIMARY KEY, grid_id TEXT, idx INTEGER, side TEXT, px TEXT, sz TEXT,
  state TEXT, ord_id TEXT, created_ms INTEGER, updated_ms INTEGER
);
CREATE TABLE IF NOT EXISTS fills (
  trade_id TEXT PRIMARY KEY, cl_ord_id TEXT, grid_id TEXT, px TEXT, sz TEXT, fee TEXT, fee_ccy TEXT, ts_ms INTEGER
);
CREATE TABLE IF NOT EXISTS profits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, grid_id TEXT, sell_cl_ord_id TEXT, buy_cl_ord_id TEXT, profit TEXT, ts_ms INTEGER
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, grid_id TEXT, ts_ms INTEGER, kind TEXT, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, grid_id TEXT, ts_ms INTEGER, quote TEXT, base TEXT, last_px TEXT, equity TEXT, realised TEXT
);
CREATE INDEX IF NOT EXISTS events_grid_ts ON events (grid_id, ts_ms);
CREATE INDEX IF NOT EXISTS fills_grid_ts ON fills (grid_id, ts_ms);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ----- grids ------------------------------------------------------------------
    def create_grid(self, grid_id: str, config_path: str, spec: dict[str, Any], state: dict[str, Any], quote_offset: D, base_offset: D) -> None:
        self.db.execute(
            "INSERT INTO grids VALUES (?,?,?,?,?,?,?,?,?,?)",
            (grid_id, now_ms(), "active", "", config_path, json.dumps(spec), json.dumps(state), str(quote_offset), str(base_offset), now_ms()),
        )

    def open_grid(self) -> sqlite3.Row | None:
        return cast(sqlite3.Row | None, self.db.execute("SELECT * FROM grids WHERE status IN ('active','halted') ORDER BY created_ms DESC LIMIT 1").fetchone())

    def grid(self, grid_id: str) -> sqlite3.Row | None:
        return cast(sqlite3.Row | None, self.db.execute("SELECT * FROM grids WHERE id=?", (grid_id,)).fetchone())

    def save_state(self, grid_id: str, state: dict[str, Any]) -> None:
        self.db.execute("UPDATE grids SET state_json=?, updated_ms=? WHERE id=?", (json.dumps(state), now_ms(), grid_id))

    def set_status(self, grid_id: str, status: str, reason: str = "") -> None:
        self.db.execute("UPDATE grids SET status=?, halt_reason=?, updated_ms=? WHERE id=?", (status, reason, now_ms(), grid_id))
        self.event(grid_id, "status", {"status": status, "reason": reason})

    def status(self, grid_id: str) -> tuple[str, str]:
        row = self.db.execute("SELECT status, halt_reason FROM grids WHERE id=?", (grid_id,)).fetchone()
        return (row["status"], row["halt_reason"]) if row else ("missing", "")

    # ----- orders / fills / profits ------------------------------------------------
    def order_placed(self, grid_id: str, o: dict[str, Any], ord_id: str, state: str = "live") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,COALESCE((SELECT created_ms FROM orders WHERE cl_ord_id=?),?),?)",
            (o["cl_ord_id"], grid_id, o["idx"], o["side"], o["price"], o["qty"], state, ord_id, o["cl_ord_id"], now_ms(), now_ms()),
        )

    def order_state(self, cl_ord_id: str, state: str, ord_id: str = "") -> None:
        if ord_id:
            self.db.execute("UPDATE orders SET state=?, ord_id=?, updated_ms=? WHERE cl_ord_id=?", (state, ord_id, now_ms(), cl_ord_id))
        else:
            self.db.execute("UPDATE orders SET state=?, updated_ms=? WHERE cl_ord_id=?", (state, now_ms(), cl_ord_id))

    def fill(self, grid_id: str, trade_id: str, cl_ord_id: str, px: D, sz: D, fee: D, fee_ccy: str, ts_ms: int) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?)", (trade_id, cl_ord_id, grid_id, str(px), str(sz), str(fee), fee_ccy, ts_ms)
        )
        return cur.rowcount == 1

    def profit(self, grid_id: str, sell_id: str, buy_id: str, profit: D) -> None:
        self.db.execute("INSERT INTO profits (grid_id, sell_cl_ord_id, buy_cl_ord_id, profit, ts_ms) VALUES (?,?,?,?,?)", (grid_id, sell_id, buy_id, str(profit), now_ms()))

    def event(self, grid_id: str, kind: str, detail: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO events (grid_id, ts_ms, kind, detail_json) VALUES (?,?,?,?)", (grid_id, now_ms(), kind, json.dumps(detail, default=str)))

    def snapshot(self, grid_id: str, quote: D, base: D, last_px: D, equity: D, realised: D) -> None:
        self.db.execute(
            "INSERT INTO snapshots (grid_id, ts_ms, quote, base, last_px, equity, realised) VALUES (?,?,?,?,?,?,?)",
            (grid_id, now_ms(), str(quote), str(base), str(last_px), str(equity), str(realised)),
        )

    # ----- reads for status / check ---------------------------------------------------
    def events_since(self, grid_id: str, since_ms: int, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.db.execute("SELECT * FROM events WHERE grid_id=? AND ts_ms>=? AND kind=? ORDER BY ts_ms", (grid_id, since_ms, kind)).fetchall()
        return self.db.execute("SELECT * FROM events WHERE grid_id=? AND ts_ms>=? ORDER BY ts_ms", (grid_id, since_ms)).fetchall()

    def profits_since(self, grid_id: str, since_ms: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM profits WHERE grid_id=? AND ts_ms>=? ORDER BY ts_ms", (grid_id, since_ms)).fetchall()

    def fills_since(self, grid_id: str, since_ms: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT f.*, o.side, o.idx FROM fills f LEFT JOIN orders o ON o.cl_ord_id=f.cl_ord_id WHERE f.grid_id=? AND f.ts_ms>=? ORDER BY f.ts_ms", (grid_id, since_ms)).fetchall()

    def last_snapshot(self, grid_id: str) -> sqlite3.Row | None:
        return cast(sqlite3.Row | None, self.db.execute("SELECT * FROM snapshots WHERE grid_id=? ORDER BY ts_ms DESC LIMIT 1", (grid_id,)).fetchone())

    def snapshots(self, grid_id: str, since_ms: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM snapshots WHERE grid_id=? AND ts_ms>=? ORDER BY ts_ms", (grid_id, since_ms)).fetchall()
