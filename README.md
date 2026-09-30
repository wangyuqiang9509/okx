# OKX Grid Bot

Fixed-range spot grids on OKX, several instruments in one process (BTC, ETH, SOL against USDT). Vocabulary in `CONTEXT.md`, decisions in `docs/adr/`.

## Setup

```sh
uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -e ".[dev]"
cp .env.example .env   # fill in OKX API key (trade permission, IP-bound)
.venv/bin/pytest
```

## Commands

All take `--config` (default `config/validate.toml`). Most take `--inst ETH-USDT` to act on one grid; without it they act on all.

| command | what it does |
|---|---|
| `gridbot replay --days 30` | feed the last 30 days of 1m candles through the engine for each configured grid |
| `gridbot start` | resume every open grid in the config, create the missing ones at the current price |
| `gridbot status [--levels]` | per grid: range, cash/base, realised profit, last reconciliation; plus the account pool |
| `gridbot halt` / `resume` | stop / restart placing new orders; resting orders stay on OKX |
| `gridbot cancel-all` | cancel resting grid orders, close the grid, return its funds to the account pool; holdings stay |
| `gridbot rebaseline` | after you deposit or withdraw, reset the account pool to match balances |
| `gridbot check` | per grid, promotion criteria over the last 24h |

To replace a grid (new range or parameters): `cancel-all --inst X`, edit the config, restart the runner.

### Configs

- `config/validate.toml`: one 50 USDT BTC grid, ±3% / 0.3%, for exercising code.
- `config/live.toml`: one 500 USDT BTC grid, ±8% / 1%.
- `config/multi.toml`: BTC 1%, ETH 2%, SOL 2%, 100 USDT each, 8 Levels per side.

An open grid is always resumed with the parameters it was created with; config values only apply when a grid is created.

### Account pool

Money in the account that no grid owns. Every reconciliation checks that actual balances equal pool plus what the open grids hold. Depositing or withdrawing breaks that on purpose, and the grids halt; run `gridbot rebaseline`, then `gridbot resume`.

## Docker

```sh
docker compose up -d --build
docker compose logs -f
docker exec gridbot gridbot status
docker exec gridbot gridbot halt --inst SOL-USDT
```

The config is chosen by `command:` in `docker-compose.yml`. The ledger keeps every grid's history.

## Rollout

1. `replay` on both configs.
2. `validate.toml`: 50 USDT, ±3% / 0.3%, run 24h, then `check`.
3. `multi.toml`: BTC, ETH, SOL at 100 USDT each, run 30 days.
