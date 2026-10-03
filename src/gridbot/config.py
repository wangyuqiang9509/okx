from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class GridConfig:
    inst_id: str
    capital_quote: Decimal
    spacing: Decimal  # fraction
    levels_below: int
    levels_above: int
    seed_slippage: Decimal  # fraction


@dataclass(frozen=True)
class RuntimeConfig:
    ledger_path: Path
    log_dir: Path
    reconcile_interval_s: int
    snapshot_interval_s: int


@dataclass(frozen=True)
class Config:
    grids: tuple[GridConfig, ...]
    runtime: RuntimeConfig
    path: Path

    def grid_for(self, inst_id: str) -> GridConfig:
        for g in self.grids:
            if g.inst_id == inst_id:
                return g
        raise KeyError(f"{inst_id} is not configured in {self.path}")


def load_config(path: str | Path) -> Config:
    p = Path(path)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    # `[grid]` (one table) or `[[grids]]` (array of tables)
    raw_grids = raw.get("grids") or ([raw["grid"]] if "grid" in raw else [])
    if not raw_grids:
        raise ValueError(f"{p}: no [grid] or [[grids]] section")
    grids = tuple(_grid(g) for g in raw_grids)
    insts = [g.inst_id for g in grids]
    if len(set(insts)) != len(insts):
        raise ValueError(f"{p}: an instrument appears twice: {insts}")
    quotes = {g.inst_id.split("-")[1] for g in grids}
    if len(quotes) != 1:
        raise ValueError(f"{p}: all grids must share one quote currency, got {quotes}")
    r = raw["runtime"]
    runtime = RuntimeConfig(
        ledger_path=Path(r.get("ledger_path", "data/gridbot.sqlite")),
        log_dir=Path(r.get("log_dir", "data/logs")),
        reconcile_interval_s=int(r.get("reconcile_interval_s", 3600)),
        snapshot_interval_s=int(r.get("snapshot_interval_s", 300)),
    )
    return Config(grids=grids, runtime=runtime, path=p)


def _grid(g: dict[str, object]) -> GridConfig:
    grid = GridConfig(
        inst_id=str(g["inst_id"]),
        capital_quote=Decimal(str(g["capital_quote"])),
        spacing=Decimal(str(g["spacing_pct"])) / Decimal(100),
        levels_below=int(str(g["levels_below"])),
        levels_above=int(str(g["levels_above"])),
        seed_slippage=Decimal(str(g.get("seed_slippage_pct", "0.1"))) / Decimal(100),
    )
    if grid.levels_below < 1 or grid.levels_above < 1:
        raise ValueError(f"{grid.inst_id}: levels_below and levels_above must both be >= 1")
    if grid.spacing <= 0:
        raise ValueError(f"{grid.inst_id}: spacing must be positive")
    return grid


@dataclass(frozen=True)
class Credentials:
    api_key: str
    secret_key: str
    passphrase: str


def load_credentials() -> Credentials:
    _load_dotenv(Path(".env"))
    try:
        return Credentials(
            api_key=os.environ["OKX_API_KEY"],
            secret_key=os.environ["OKX_SECRET_KEY"],
            passphrase=os.environ["OKX_PASSPHRASE"],
        )
    except KeyError as e:
        raise SystemExit(f"missing credential {e.args[0]} (set it in .env or the environment)") from None


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class TrendConfig:
    inst_ids: tuple[str, ...]
    capital_quote: Decimal | None  # None = everything in the pool
    adopt_pool_coins: bool
    target_vol: float
    band: Decimal
    min_trade_quote: Decimal
    slippage: Decimal
    rebalance_minute_utc: int  # minutes after 00:00 UTC
    runtime: RuntimeConfig
    path: Path

    @property
    def quote_ccy(self) -> str:
        return self.inst_ids[0].split("-")[1]


def load_trend_config(path: str | Path) -> TrendConfig:
    p = Path(path)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    if "trend" not in raw:
        raise ValueError(f"{p}: no [trend] section")
    t, r = raw["trend"], raw["runtime"]
    insts = tuple(str(i) for i in t["inst_ids"])
    if len({i.split("-")[1] for i in insts}) != 1:
        raise ValueError(f"{p}: all instruments must share one quote currency")
    cap = t.get("capital_quote", "all")
    hh, mm = str(t.get("rebalance_utc", "00:05")).split(":")
    return TrendConfig(
        inst_ids=insts,
        capital_quote=None if str(cap) == "all" else Decimal(str(cap)),
        adopt_pool_coins=bool(t.get("adopt_pool_coins", True)),
        target_vol=float(t["target_vol_pct"]) / 100,
        band=Decimal(str(t.get("rebalance_band_pct", 5))) / 100,
        min_trade_quote=Decimal(str(t.get("min_trade_quote", 5))),
        slippage=Decimal(str(t.get("slippage_pct", "0.1"))) / 100,
        rebalance_minute_utc=int(hh) * 60 + int(mm),
        runtime=RuntimeConfig(
            ledger_path=Path(r.get("ledger_path", "data/gridbot.sqlite")),
            log_dir=Path(r.get("log_dir", "data/logs")),
            reconcile_interval_s=int(r.get("reconcile_interval_s", 3600)),
            snapshot_interval_s=int(r.get("snapshot_interval_s", 300)),
        ),
        path=p,
    )


@dataclass(frozen=True)
class MartingaleConfig:
    inst_id: str
    capital_quote: Decimal | None  # None = all quote in the pool
    step: Decimal  # fraction
    mult: Decimal
    adds: int
    take_profit: Decimal  # fraction
    slippage: Decimal  # fraction, Opening Buy IOC price = ask * (1 + this)
    poll_interval_s: float
    runtime: RuntimeConfig
    path: Path

    @property
    def quote_ccy(self) -> str:
        return self.inst_id.split("-")[1]


def load_martingale_config(path: str | Path) -> MartingaleConfig:
    p = Path(path)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    if "martingale" not in raw:
        raise ValueError(f"{p}: no [martingale] section")
    m, r = raw["martingale"], raw["runtime"]
    inst = str(m["inst_id"])
    if inst not in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        raise ValueError(f"{p}: only BTC-USDT, ETH-USDT and SOL-USDT are allowed, got {inst}")
    cap = m.get("capital_quote", "all")
    cfg = MartingaleConfig(
        inst_id=inst,
        capital_quote=None if str(cap) == "all" else Decimal(str(cap)),
        step=Decimal(str(m["step_pct"])) / 100,
        mult=Decimal(str(m["mult"])),
        adds=int(m["adds"]),
        take_profit=Decimal(str(m["take_profit_pct"])) / 100,
        slippage=Decimal(str(m.get("slippage_pct", "0.1"))) / 100,
        poll_interval_s=float(m.get("poll_interval_s", 5)),
        runtime=RuntimeConfig(
            ledger_path=Path(r.get("ledger_path", "data/gridbot.sqlite")),
            log_dir=Path(r.get("log_dir", "data/logs")),
            reconcile_interval_s=int(r.get("reconcile_interval_s", 3600)),
            snapshot_interval_s=int(r.get("snapshot_interval_s", 300)),
        ),
        path=p,
    )
    if cfg.step <= 0 or cfg.take_profit <= 0 or cfg.mult < 1 or cfg.adds < 0:
        raise ValueError(f"{p}: step and take-profit must be positive, mult >= 1, adds >= 0")
    return cfg
