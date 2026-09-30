from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from decimal import Decimal as D
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, load_config, load_credentials
from .engine import GridEngine
from .ledger import Ledger, now_ms

DAY_MS = 86_400_000


def setup_logging(cfg: Config) -> None:
    cfg.runtime.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(cfg.runtime.log_dir / "gridbot.log", maxBytes=20_000_000, backupCount=10)
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def cmd_start(cfg: Config) -> None:
    from .okx import OkxRest
    from .runner import Runner

    setup_logging(cfg)
    creds = load_credentials()

    async def main() -> None:
        rest = OkxRest(creds)
        ledger = Ledger(cfg.runtime.ledger_path)
        try:
            await Runner(cfg, creds, ledger, rest).run()
        finally:
            await rest.close()
            ledger.close()

    asyncio.run(main())


def _open_grid(ledger: Ledger) -> sqlite3.Row:
    row = ledger.open_grid()
    if row is None:
        row = ledger.db.execute("SELECT * FROM grids ORDER BY created_ms DESC LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("no grid in ledger")
    return row


def cmd_status(cfg: Config) -> None:
    ledger = Ledger(cfg.runtime.ledger_path)
    row = _open_grid(ledger)
    eng = GridEngine.from_state(json.loads(row["state_json"]))
    snap = ledger.last_snapshot(row["id"])
    print(f"grid {row['id']}  status {row['status']}  {row['halt_reason'] or ''}")
    print(f"created {_ts(row['created_ms'])}  config {row['config_path']}")
    s = eng.spec
    print(f"range {s.lower} .. {s.upper}  spacing {s.spacing * 100}%  qty/level {s.qty}")
    print(f"cash {eng.cash_quote:.4f} USDT  base {eng.base_held} BTC  realised {eng.realised_profit:+.4f} USDT over {eng.round_trips} round trips")
    if snap:
        print(f"last snapshot {_ts(snap['ts_ms'])}: px {snap['last_px']} equity {D(snap['equity']):.4f}")
    if eng.unplaced:
        print(f"UNPLACED: {sorted(eng.unplaced)}")
    print("levels:")
    for idx in sorted(eng.levels, reverse=True):
        o = eng.levels[idx]
        mark = "  <- anchor" if idx == 0 else ""
        if o is None:
            print(f"  L{idx:+3d} {s.price(idx):>10}  -{mark}")
        else:
            flag = " (unplaced)" if o.cl_ord_id in eng.unplaced else ""
            print(f"  L{idx:+3d} {s.price(idx):>10}  {o.side.value:4} {o.qty} filled {o.filled_sz}{flag}{mark}")
    recs = ledger.events_since(row["id"], now_ms() - DAY_MS, "reconcile")
    if recs:
        last = json.loads(recs[-1]["detail_json"])
        print(f"reconcile last 24h: {len(recs)} runs, last {_ts(recs[-1]['ts_ms'])} ok={last.get('ok')} {last.get('problem', '')}")


def cmd_halt(cfg: Config, resume: bool) -> None:
    ledger = Ledger(cfg.runtime.ledger_path)
    row = _open_grid(ledger)
    if row["status"] == "closed":
        raise SystemExit("grid is closed")
    ledger.set_status(row["id"], "active" if resume else "halted", "" if resume else "manual")
    print(f"grid {row['id']} -> {'active' if resume else 'halted'} (runner picks it up within 5s)")


def cmd_cancel_all(cfg: Config, then_start: bool = False) -> None:
    from .okx import OkxRest

    setup_logging(cfg)
    creds = load_credentials()
    ledger = Ledger(cfg.runtime.ledger_path)
    row = _open_grid(ledger)

    async def main() -> None:
        rest = OkxRest(creds)
        try:
            pending = await rest.pending_orders(cfg.grid.inst_id)
            ids = [o.cl_ord_id for o in pending if o.cl_ord_id]
            if ids:
                ledger.set_status(row["id"], "halted", "manual_cancel_all")
                res = await rest.cancel_orders(cfg.grid.inst_id, ids)
                bad = [r for r in res if not r.ok]
                for r in res:
                    ledger.order_state(r.cl_ord_id, "canceled" if r.ok else "cancel_failed")
                print(f"cancelled {len(res) - len(bad)} of {len(res)} orders")
                if bad:
                    for r in bad:
                        print(f"  FAILED {r.cl_ord_id}: {r.code} {r.msg}")
                    raise SystemExit("some cancels failed; grid left halted, not closed")
            else:
                print("no pending orders")
            if row["status"] != "closed":
                ledger.set_status(row["id"], "closed", "manual_cancel_all")
            bal = await rest.balances("BTC", "USDT")
            print(f"grid {row['id']} closed. holding {bal['BTC'].total} BTC, {bal['USDT'].total} USDT")
        finally:
            await rest.close()

    asyncio.run(main())
    if then_start:
        time.sleep(6)  # let a running runner notice 'closed' and exit
        cmd_start(cfg)


def cmd_replay(cfg: Config, days: int) -> None:
    from .candles import load_candles
    from .replay import run_replay

    maker, taker = D("-0.0008"), D("-0.001")
    try:
        creds = load_credentials()
        from .okx import Fees, OkxRest

        async def fees() -> Fees:
            rest = OkxRest(creds)
            try:
                return await rest.fees(cfg.grid.inst_id)
            finally:
                await rest.close()

        f = asyncio.run(fees())
        maker, taker = f.maker, f.taker
        print(f"account fees: maker {maker} taker {taker}")
    except SystemExit:
        print(f"no credentials; assuming fees maker {maker} taker {taker}")
    candles = load_candles(cfg.grid.inst_id, days, Path("data/candles"))
    g = cfg.grid
    r = run_replay(candles, g.spacing, g.levels_below, g.levels_above, g.capital_quote, maker, taker)
    print(f"replay of {cfg.path}: capital {g.capital_quote}, ±{g.levels_above * g.spacing * 100:.1f}%/{g.spacing * 100}% spacing")
    print(r.summary(g.capital_quote))


def cmd_check(cfg: Config) -> None:
    ledger = Ledger(cfg.runtime.ledger_path)
    row = _open_grid(ledger)
    gid = row["id"]
    since = now_ms() - DAY_MS
    problems: list[str] = []
    uptime_h = (now_ms() - row["created_ms"]) / 3_600_000
    if uptime_h < 24:
        problems.append(f"grid is only {uptime_h:.1f}h old (need 24h)")
    halts = [e for e in ledger.events_since(gid, since, "status") if json.loads(e["detail_json"]).get("status") == "halted"]
    code_halts = [e for e in halts if not json.loads(e["detail_json"]).get("reason", "").startswith("manual")]
    if code_halts:
        problems.append(f"{len(code_halts)} non-manual halt(s): " + "; ".join(json.loads(e["detail_json"]).get("reason", "") for e in code_halts))
    recs = ledger.events_since(gid, since, "reconcile")
    bad_recs = [e for e in recs if not json.loads(e["detail_json"]).get("ok")]
    if not recs:
        problems.append("no reconciliation in the last 24h")
    if bad_recs:
        problems.append(f"{len(bad_recs)} of {len(recs)} reconciliations failed: " + "; ".join(json.loads(e["detail_json"]).get("problem", json.loads(e["detail_json"]).get("error", "?")) for e in bad_recs))
    sells = ledger.db.execute("SELECT cl_ord_id FROM orders WHERE grid_id=? AND side='sell' AND state='filled' AND updated_ms>=?", (gid, since)).fetchall()
    profits = {p["sell_cl_ord_id"]: p for p in ledger.profits_since(gid, since - DAY_MS)}
    unmatched = [s["cl_ord_id"] for s in sells if s["cl_ord_id"] not in profits or not profits[s["cl_ord_id"]]["buy_cl_ord_id"]]
    if unmatched:
        problems.append(f"{len(unmatched)} filled sell(s) without a matching buy: {unmatched[:5]}")
    errors = ledger.events_since(gid, since, "error")
    realised = sum((D(p["profit"]) for p in profits.values()), D(0))
    print(f"grid {gid} uptime {uptime_h:.1f}h  status {row['status']}")
    print(f"last 24h: {len(recs)} reconciliations, {len(halts)} halts, {len(sells)} sells filled, {len(errors)} errors, realised {realised:+.4f} USDT")
    if problems:
        print("FAIL")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("PASS: safe to promote to the next capital level")


def _ts(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) + "Z"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gridbot", description="Fixed-range spot grid bot for OKX")
    p.add_argument("--config", "-c", default="config/validate.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="run the grid (creates one if none is open)")
    sub.add_parser("status", help="show grid state from the ledger")
    sub.add_parser("halt", help="stop placing new orders; resting orders stay")
    sub.add_parser("resume", help="lift a halt")
    sub.add_parser("cancel-all", help="cancel every resting order and close the grid; holdings stay")
    sub.add_parser("reset", help="cancel-all, then start a fresh grid at the current price")
    rp = sub.add_parser("replay", help="replay recent 1m candles through the engine")
    rp.add_argument("--days", type=int, default=30)
    sub.add_parser("check", help="promotion criteria over the last 24h")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    match a.cmd:
        case "start":
            cmd_start(cfg)
        case "status":
            cmd_status(cfg)
        case "halt":
            cmd_halt(cfg, resume=False)
        case "resume":
            cmd_halt(cfg, resume=True)
        case "cancel-all":
            cmd_cancel_all(cfg)
        case "reset":
            cmd_cancel_all(cfg, then_start=True)
        case "replay":
            cmd_replay(cfg, a.days)
        case "check":
            cmd_check(cfg)


if __name__ == "__main__":
    main()
