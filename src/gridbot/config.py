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
    grid: GridConfig
    runtime: RuntimeConfig
    path: Path


def load_config(path: str | Path) -> Config:
    p = Path(path)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    g = raw["grid"]
    r = raw["runtime"]
    grid = GridConfig(
        inst_id=str(g["inst_id"]),
        capital_quote=Decimal(str(g["capital_quote"])),
        spacing=Decimal(str(g["spacing_pct"])) / Decimal(100),
        levels_below=int(g["levels_below"]),
        levels_above=int(g["levels_above"]),
        seed_slippage=Decimal(str(g.get("seed_slippage_pct", "0.1"))) / Decimal(100),
    )
    if grid.levels_below < 1 or grid.levels_above < 1:
        raise ValueError("levels_below and levels_above must both be >= 1")
    if grid.spacing <= 0:
        raise ValueError("spacing must be positive")
    runtime = RuntimeConfig(
        ledger_path=Path(r.get("ledger_path", "data/gridbot.sqlite")),
        log_dir=Path(r.get("log_dir", "data/logs")),
        reconcile_interval_s=int(r.get("reconcile_interval_s", 3600)),
        snapshot_interval_s=int(r.get("snapshot_interval_s", 300)),
    )
    return Config(grid=grid, runtime=runtime, path=p)


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
