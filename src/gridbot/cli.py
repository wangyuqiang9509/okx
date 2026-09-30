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
from typing import Any

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
    from .runner import Supervisor

    setup_logging(cfg)
    creds = load_credentials()

    async def main() -> None:
        rest = OkxRest(creds)
        ledger = Ledger(cfg.runtime.ledger_path)
        try:
            await Supervisor(cfg, creds, ledger, rest).run()
        finally:
            await rest.close()
            ledger.close()

    asyncio.run(main())


def _targets(ledger: Ledger, inst: str | None) -> list[sqlite3.Row]:
    rows = ledger.open_grids()
    if inst:
        rows = [r for r in rows if r["inst_id"] == inst]
    if not rows:
        raise SystemExit(f"no open grid{' on ' + inst if inst else ''} in ledger")
    return rows


def cmd_status(cfg: Config, inst: str | None, levels: bool) -> None:
    ledger = Ledger(cfg.runtime.ledger_path)
    rows = _targets(ledger, inst)
    total_eq = D(0)
    for row in rows:
        eng = GridEngine.from_state(json.loads(row["state_json"]))
        snap = ledger.last_snapshot(row["id"])
        s = eng.spec
        print(f"== {row['inst_id']}  grid {row['id']}  {row['status']}  {row['halt_reason'] or ''}")
        print(f"   created {_ts(row['created_ms'])}  range {s.lower} .. {s.upper}  spacing {s.spacing * 100}%  qty/level {s.qty}")
        print(f"   cash {eng.cash_quote:.4f} {eng.quote_ccy}  base {eng.base_held} {eng.base_ccy}  realised {eng.realised_profit:+.4f} over {eng.round_trips} round trips")
        if snap:
            total_eq += D(snap["equity"])
            print(f"   last snapshot {_ts(snap['ts_ms'])}: px {snap['last_px']} equity {D(snap['equity']):.4f}")
        if eng.unplaced:
            print(f"   UNPLACED: {sorted(eng.unplaced)}")
        recs = ledger.events_since(row["id"], now_ms() - DAY_MS, "reconcile")
        if recs:
            last = json.loads(recs[-1]["detail_json"])
            print(f"   reconcile last 24h: {len(recs)} runs, last {_ts(recs[-1]['ts_ms'])} ok={last.get('ok')} {last.get('problem', '')}")
        if levels or inst:
            for idx in sorted(eng.levels, reverse=True):
                o = eng.levels[idx]
                mark = "  <- anchor" if idx == 0 else ""
                if o is None:
                    print(f"     L{idx:+3d} {s.price(idx):>12}  -{mark}")
                else:
                    flag = " (unplaced)" if o.cl_ord_id in eng.unplaced else ""
                    print(f"     L{idx:+3d} {s.price(idx):>12}  {o.side.value:4} {o.qty} filled {o.filled_sz}{flag}{mark}")
    pool = ledger.pool()
    print(f"account pool (not in any grid): " + ", ".join(f"{v} {k}" for k, v in sorted(pool.items())))
    if len(rows) > 1:
        print(f"grids total equity at last snapshots: {total_eq:.4f}")


def cmd_halt(cfg: Config, inst: str | None, resume: bool) -> None:
    ledger = Ledger(cfg.runtime.ledger_path)
    for row in _targets(ledger, inst):
        ledger.set_status(row["id"], "active" if resume else "halted", "" if resume else "manual")
        print(f"{row['inst_id']} grid {row['id']} -> {'active' if resume else 'halted'} (runner picks it up within 5s)")


def cmd_cancel_all(cfg: Config, inst: str | None) -> None:
    from .okx import OkxRest

    setup_logging(cfg)
    creds = load_credentials()
    ledger = Ledger(cfg.runtime.ledger_path)
    rows = _targets(ledger, inst)

    async def main() -> None:
        rest = OkxRest(creds)
        try:
            for row in rows:
                inst_id = row["inst_id"]
                ledger.set_status(row["id"], "halted", "manual_cancel_all")
                pending = await rest.pending_orders(inst_id)
                ids = [o.cl_ord_id for o in pending if o.cl_ord_id.startswith("G")]
                if ids:
                    res = await rest.cancel_orders(inst_id, ids)
                    bad = [r for r in res if not r.ok]
                    for r in res:
                        ledger.order_state(r.cl_ord_id, "canceled" if r.ok else "cancel_failed")
                    print(f"{inst_id}: cancelled {len(res) - len(bad)} of {len(res)} orders")
                    if bad:
                        for r in bad:
                            print(f"  FAILED {r.cl_ord_id}: {r.code} {r.msg}")
                        raise SystemExit(f"{inst_id}: some cancels failed; grid left halted, not closed")
                else:
                    print(f"{inst_id}: no pending grid orders")
                await asyncio.sleep(2)  # let the runner record any fill that raced the cancel
                q, b = ledger.close_grid(row["id"], "manual_cancel_all")
                print(f"{inst_id}: grid {row['id']} closed, released {q:.4f} quote and {b} base to the account pool")
        finally:
            await rest.close()

    asyncio.run(main())
    print("restart the runner to create fresh grids for the closed instruments")


def cmd_rebaseline(cfg: Config) -> None:
    """After a deposit or withdrawal: pool := account balance minus what the open grids own."""
    from .okx import OkxRest

    creds = load_credentials()
    ledger = Ledger(cfg.runtime.ledger_path)
    rows = ledger.open_grids()
    owned: dict[str, D] = {}
    for row in rows:
        st = json.loads(row["state_json"])
        owned[st["quote_ccy"]] = owned.get(st["quote_ccy"], D(0)) + D(st["cash_quote"])
        owned[st["base_ccy"]] = owned.get(st["base_ccy"], D(0)) + D(st["base_held"])
    quote = cfg.grids[0].inst_id.split("-")[1]
    ccys = sorted({quote, *owned, *(g.inst_id.split("-")[0] for g in cfg.grids), *ledger.pool()})

    async def main() -> None:
        rest = OkxRest(creds)
        try:
            bal = await rest.balances(*ccys)
        finally:
            await rest.close()
        old = ledger.pool()
        for c in ccys:
            new = bal[c].total - owned.get(c, D(0))
            if new < 0:
                raise SystemExit(f"{c}: grids own {owned.get(c)} but account holds {bal[c].total}; not rebaselining")
            ledger.pool_set(c, new)
            print(f"{c}: pool {old.get(c, D(0))} -> {new}  (account {bal[c].total}, grids {owned.get(c, D(0))})")

    asyncio.run(main())


def cmd_recover(cfg: Config) -> None:
    from .okx import OkxRest
    from .recover import RecoverError, recover, summary

    creds = load_credentials()
    ledger = Ledger(cfg.runtime.ledger_path)

    async def main() -> None:
        rest = OkxRest(creds)
        try:
            results = await recover(cfg, ledger, rest)
        except RecoverError as e:
            raise SystemExit(f"RECOVER FAILED: {e}") from None
        finally:
            await rest.close()
        if not results:
            print("no live grid orders on OKX for the configured instruments; nothing to recover. `gridbot start` will create fresh grids.")
            return
        print(summary(results, ledger))
        print("RECOVER OK. Next: start the runner.")

    asyncio.run(main())


OKX_HINTS = {
    "50105": "passphrase is wrong (OKX_PASSPHRASE)",
    "50111": "API key is wrong (OKX_API_KEY)",
    "50113": "secret is wrong (OKX_SECRET_KEY)",
    "50110": "this machine's IP is not in the API key's IP allowlist",
    "50119": "API key does not exist or was deleted",
    "50102": "clock is off by more than 30s; sync the system time",
    "60032": "key belongs to another OKX region; this bot uses www.okx.com",
}


def cmd_doctor(cfg: Config) -> None:
    from .okx import OkxError, OkxRest
    from .recover import grid_orders_on_exchange

    creds = load_credentials()
    ledger = Ledger(cfg.runtime.ledger_path)
    quote = cfg.grids[0].inst_id.split("-")[1]
    ccys = sorted({quote, *(g.inst_id.split("-")[0] for g in cfg.grids)})

    async def main() -> int:
        rest = OkxRest(creds)
        try:
            try:
                bal = await rest.balances(*ccys)
            except OkxError as e:
                print(f"CREDENTIALS FAILED: okx {e.code} {e.msg}")
                if e.code in OKX_HINTS:
                    print(f"  likely cause: {OKX_HINTS[e.code]}")
                return 2
            print("credentials OK (read + trade)")
            print("balances: " + ", ".join(f"{bal[c].total} {c}" for c in ccys))
            live = {g.inst_id: await grid_orders_on_exchange(rest, g.inst_id) for g in cfg.grids}
        finally:
            await rest.close()
        known = {r["inst_id"] for r in ledger.open_grids()}
        need_recover = False
        for g in cfg.grids:
            n = len(live[g.inst_id])
            state = "in ledger" if g.inst_id in known else "not in ledger"
            print(f"{g.inst_id}: {n} live grid orders on OKX, {state}")
            if n and g.inst_id not in known:
                need_recover = True
        if need_recover and known:
            print("NEXT: ledger is partial; stop and ask the user (do not run recover or start)")
            return 3
        if need_recover:
            print("NEXT: run `gridbot recover`, then start")
        elif known:
            print("NEXT: ledger matches; start the runner")
        else:
            print(f"NEXT: no grids anywhere; start will create new ones (needs {sum(g.capital_quote for g in cfg.grids)} {quote} free)")
        return 0

    code = asyncio.run(main())
    if code:
        sys.exit(code)


def cmd_replay(cfg: Config, inst: str | None, days: int) -> None:
    import httpx

    from .candles import load_candles
    from .replay import run_replay

    maker, taker = D("-0.0008"), D("-0.001")
    for g in cfg.grids:
        if inst and g.inst_id != inst:
            continue
        m = httpx.get("https://www.okx.com/api/v5/public/instruments", params={"instType": "SPOT", "instId": g.inst_id}).json()["data"][0]
        candles = load_candles(g.inst_id, days, Path("data/candles"))
        r = run_replay(candles, g.spacing, g.levels_below, g.levels_above, g.capital_quote, maker, taker,
                       D(m["tickSz"]), D(m["lotSz"]), D(m["minSz"]), g.inst_id)
        print(f"== {g.inst_id}: capital {g.capital_quote}, -{g.levels_below}/+{g.levels_above} levels at {g.spacing * 100}% (fees maker {maker} taker {taker})")
        print(r.summary(g.capital_quote))


def cmd_check(cfg: Config, inst: str | None) -> None:
    ledger = Ledger(cfg.runtime.ledger_path)
    failed = False
    for row in _targets(ledger, inst):
        failed |= not _check_grid(ledger, row)
    if failed:
        sys.exit(1)
    print("PASS: safe to promote to the next capital level")


def _check_grid(ledger: Ledger, row: sqlite3.Row) -> bool:
    gid = row["id"]
    since = now_ms() - DAY_MS
    problems: list[str] = []
    uptime_h = (now_ms() - row["created_ms"]) / 3_600_000
    if uptime_h < 24:
        problems.append(f"grid is only {uptime_h:.1f}h old (need 24h)")
    def detail(e: sqlite3.Row) -> dict[str, Any]:
        d: dict[str, Any] = json.loads(e["detail_json"])
        return d

    halts = [e for e in ledger.events_since(gid, since, "status") if detail(e).get("status") == "halted"]
    code_halts = [e for e in halts if not detail(e).get("reason", "").startswith("manual")]
    if code_halts:
        problems.append(f"{len(code_halts)} non-manual halt(s): " + "; ".join(detail(e).get("reason", "") for e in code_halts))
    recs = ledger.events_since(gid, since, "reconcile")
    bad_recs = [e for e in recs if not detail(e).get("ok")]
    if not recs:
        problems.append("no reconciliation in the last 24h")
    if bad_recs:
        problems.append(f"{len(bad_recs)} of {len(recs)} reconciliations failed: " + "; ".join(str(detail(e).get("problem", detail(e).get("error", "?"))) for e in bad_recs))
    sells = ledger.db.execute("SELECT cl_ord_id FROM orders WHERE grid_id=? AND side='sell' AND state='filled' AND updated_ms>=?", (gid, since)).fetchall()
    profits = {p["sell_cl_ord_id"]: p for p in ledger.profits_since(gid, since - DAY_MS)}
    unmatched = [s["cl_ord_id"] for s in sells if s["cl_ord_id"] not in profits or not profits[s["cl_ord_id"]]["buy_cl_ord_id"]]
    if unmatched:
        problems.append(f"{len(unmatched)} filled sell(s) without a matching buy: {unmatched[:5]}")
    errors = ledger.events_since(gid, since, "error")
    realised = sum((D(p["profit"]) for p in profits.values()), D(0))
    print(f"== {row['inst_id']} grid {gid} uptime {uptime_h:.1f}h  status {row['status']}")
    print(f"   last 24h: {len(recs)} reconciliations, {len(halts)} halts, {len(sells)} sells filled, {len(errors)} errors, realised {realised:+.4f}")
    if problems:
        print("   FAIL")
        for p in problems:
            print(f"     - {p}")
        return False
    print("   pass")
    return True


def _ts(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) + "Z"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gridbot", description="Fixed-range spot grid bot for OKX")
    p.add_argument("--config", "-c", default="config/validate.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_inst(name: str, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument("--inst", help="only this instrument, e.g. ETH-USDT (default: all)")
        return sp

    sub.add_parser("start", help="run every configured grid (resumes open ones, creates missing ones)")
    st = with_inst("status", "show grid state from the ledger")
    st.add_argument("--levels", action="store_true", help="list every Level")
    with_inst("halt", "stop placing new orders; resting orders stay")
    with_inst("resume", "lift a halt")
    with_inst("cancel-all", "cancel resting grid orders and close the grid; holdings stay")
    sub.add_parser("rebaseline", help="after a deposit or withdrawal, reset the account pool to match balances")
    sub.add_parser("doctor", help="check the API key, balances, and whether to recover or start")
    sub.add_parser("recover", help="on a new machine: rebuild the ledger from OKX order history (places nothing)")
    rp = with_inst("replay", "replay recent 1m candles through the engine")
    rp.add_argument("--days", type=int, default=30)
    with_inst("check", "promotion criteria over the last 24h")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    inst = getattr(a, "inst", None)
    match a.cmd:
        case "start":
            cmd_start(cfg)
        case "status":
            cmd_status(cfg, inst, a.levels)
        case "halt":
            cmd_halt(cfg, inst, resume=False)
        case "resume":
            cmd_halt(cfg, inst, resume=True)
        case "cancel-all":
            cmd_cancel_all(cfg, inst)
        case "rebaseline":
            cmd_rebaseline(cfg)
        case "doctor":
            cmd_doctor(cfg)
        case "recover":
            cmd_recover(cfg)
        case "replay":
            cmd_replay(cfg, inst, a.days)
        case "check":
            cmd_check(cfg, inst)


if __name__ == "__main__":
    main()
