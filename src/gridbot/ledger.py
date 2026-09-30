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
CREATE TABLE IF NOT EXISTS books (
  id TEXT PRIMARY KEY, kind TEXT, created_ms INTEGER, status TEXT, halt_reason TEXT,
  config_path TEXT, state_json TEXT, updated_ms INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT, day TEXT, inst_id TEXT, ts_ms INTEGER, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS account_pool (
  ccy TEXT PRIMARY KEY, amount TEXT, updated_ms INTEGER
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
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(grids)")}
        if "inst_id" not in cols:
            self.db.execute("ALTER TABLE grids ADD COLUMN inst_id TEXT")
            self.db.execute("UPDATE grids SET inst_id = json_extract(spec_json, '$.inst_id')")
        # v1 kept a per-grid offset; v2 keeps one account-level pool of funds no grid owns.
        if self.pool() == {}:
            open_rows = self.open_grids()
            if len(open_rows) == 1:
                row = open_rows[0]
                spec = json.loads(row["spec_json"])
                base_ccy, quote_ccy = spec["inst_id"].split("-")
                self.pool_set(quote_ccy, D(row["quote_offset"]))
                self.pool_set(base_ccy, D(row["base_offset"]))

    def close(self) -> None:
        self.db.close()

    # ----- grids ------------------------------------------------------------------
    def create_grid(self, grid_id: str, config_path: str, spec: dict[str, Any], state: dict[str, Any], created_ms: int | None = None) -> None:
        created = created_ms or now_ms()
        self.db.execute(
            "INSERT INTO grids (id, created_ms, status, halt_reason, config_path, spec_json, state_json, quote_offset, base_offset, updated_ms, inst_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (grid_id, created, "active", "", config_path, json.dumps(spec), json.dumps(state), "", "", now_ms(), spec["inst_id"]),
        )

    def open_grid(self, inst_id: str | None = None) -> sqlite3.Row | None:
        a: tuple[str, ...]
        if inst_id is None:
            q, a = "SELECT * FROM grids WHERE status IN ('active','halted') ORDER BY created_ms DESC LIMIT 1", ()
        else:
            q, a = "SELECT * FROM grids WHERE status IN ('active','halted') AND inst_id=? ORDER BY created_ms DESC LIMIT 1", (inst_id,)
        return cast(sqlite3.Row | None, self.db.execute(q, a).fetchone())

    def open_grids(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM grids WHERE status IN ('active','halted') ORDER BY created_ms").fetchall()

    # ----- account pool: funds in the account that belong to no open grid ----------------
    def pool(self) -> dict[str, D]:
        return {r["ccy"]: D(r["amount"]) for r in self.db.execute("SELECT * FROM account_pool")}

    def pool_set(self, ccy: str, amount: D) -> None:
        self.db.execute("INSERT OR REPLACE INTO account_pool VALUES (?,?,?)", (ccy, str(amount), now_ms()))

    def pool_add(self, ccy: str, delta: D) -> None:
        self.pool_set(ccy, self.pool().get(ccy, D(0)) + delta)

    # ----- books: non-grid strategies holding cash and coins ------------------------------
    def open_book(self, kind: str) -> sqlite3.Row | None:
        return cast(sqlite3.Row | None, self.db.execute(
            "SELECT * FROM books WHERE kind=? AND status IN ('active','halted') ORDER BY created_ms DESC LIMIT 1", (kind,)).fetchone())

    def open_books(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM books WHERE status IN ('active','halted')").fetchall()

    def create_book(self, book_id: str, kind: str, config_path: str, state: dict[str, Any], take: dict[str, D]) -> None:
        """Create a Book funded from the Account Pool, atomically."""
        pool = self.pool()
        for ccy, amt in take.items():
            if pool.get(ccy, D(0)) < amt:
                raise ValueError(f"pool has {pool.get(ccy, D(0))} {ccy}, book wants {amt}")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for ccy, amt in take.items():
                self.pool_add(ccy, -amt)
            self.db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?)",
                            (book_id, kind, now_ms(), "active", "", config_path, json.dumps(state), now_ms()))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self.event(book_id, "book_created", {"take": {k: str(v) for k, v in take.items()}})

    def save_book(self, book_id: str, state: dict[str, Any]) -> None:
        self.db.execute("UPDATE books SET state_json=?, updated_ms=? WHERE id=?", (json.dumps(state), now_ms(), book_id))

    def book_status(self, book_id: str) -> tuple[str, str]:
        row = self.db.execute("SELECT status, halt_reason FROM books WHERE id=?", (book_id,)).fetchone()
        return (row["status"], row["halt_reason"]) if row else ("missing", "")

    def set_book_status(self, book_id: str, status: str, reason: str = "") -> None:
        self.db.execute("UPDATE books SET status=?, halt_reason=?, updated_ms=? WHERE id=?", (status, reason, now_ms(), book_id))
        self.event(book_id, "status", {"status": status, "reason": reason})

    def close_book(self, book_id: str, reason: str) -> dict[str, D]:
        """Mark closed and hand cash and coins back to the pool, atomically. Trades nothing."""
        row = self.db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if row is None or row["status"] == "closed":
            return {}
        st = json.loads(row["state_json"])
        release = {st["quote_ccy"]: D(st["cash"])} | {c: D(q) for c, q in st["holdings"].items()}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for ccy, amt in release.items():
                self.pool_add(ccy, amt)
            self.db.execute("UPDATE books SET status='closed', halt_reason=?, updated_ms=? WHERE id=?", (reason, now_ms(), book_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self.event(book_id, "status", {"status": "closed", "reason": reason, "released": {k: str(v) for k, v in release.items()}})
        return release

    def decision(self, book_id: str, day: str, inst_id: str, detail: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO decisions (book_id, day, inst_id, ts_ms, detail_json) VALUES (?,?,?,?,?)",
                        (book_id, day, inst_id, now_ms(), json.dumps(detail, default=str)))

    def decisions(self, book_id: str, limit: int = 30) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM decisions WHERE book_id=? ORDER BY id DESC LIMIT ?", (book_id, limit)).fetchall()

    def close_grid(self, grid_id: str, reason: str) -> tuple[D, D]:
        """Mark closed and hand the grid's remaining cash and base back to the pool, atomically."""
        row = self.grid(grid_id)
        assert row is not None
        if row["status"] == "closed":
            return D(0), D(0)
        state = json.loads(row["state_json"])
        base_ccy, quote_ccy = state["base_ccy"], state["quote_ccy"]
        cash, base = D(state["cash_quote"]), D(state["base_held"])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.pool_add(quote_ccy, cash)
            self.pool_add(base_ccy, base)
            self.db.execute("UPDATE grids SET status='closed', halt_reason=?, updated_ms=? WHERE id=?", (reason, now_ms(), grid_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self.event(grid_id, "status", {"status": "closed", "reason": reason, "released_quote": str(cash), "released_base": str(base)})
        return cash, base

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
    def order_placed(self, grid_id: str, o: dict[str, Any], ord_id: str, state: str = "live", ts_ms: int | None = None) -> None:
        ts = ts_ms or now_ms()
        self.db.execute(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,COALESCE((SELECT created_ms FROM orders WHERE cl_ord_id=?),?),?)",
            (o["cl_ord_id"], grid_id, o["idx"], o["side"], o["price"], o["qty"], state, ord_id, o["cl_ord_id"], ts, ts),
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

    def profit(self, grid_id: str, sell_id: str, buy_id: str, profit: D, ts_ms: int | None = None) -> None:
        self.db.execute("INSERT INTO profits (grid_id, sell_cl_ord_id, buy_cl_ord_id, profit, ts_ms) VALUES (?,?,?,?,?)", (grid_id, sell_id, buy_id, str(profit), ts_ms or now_ms()))

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
