# OKX Quant Bot

Long-only spot strategies on OKX for BTC, ETH and SOL against USDT: a Spot Martingale on SOL (what runs while capital is small, see ADR-0005), a daily Trend Strategy (for when capital is large, see ADR-0004) and fixed-range grids. One strategy runs on the account at a time. Vocabulary in `CONTEXT.md`, decisions in `docs/adr/`.

**Moving to or setting up a machine: follow [DEPLOY.md](DEPLOY.md).** It is written so an AI agent can run it end to end; the only manual step is filling `.env`.

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
| `gridbot doctor` | check the API key and balances, and say whether to `recover` or `start` |
| `gridbot recover` | on an empty ledger: rebuild every live grid from OKX order history (read only) |
| `gridbot check` | per grid, promotion criteria over the last 24h |

### Trend Strategy (`-c config/trend.toml`)

| command | what it does |
|---|---|
| `gridbot trend-plan` | today's six votes, volatility and weight per coin, and the trades a rebalance would send; places nothing |
| `gridbot trend-run` | create the Book from the account pool (or resume it) and rebalance daily at 00:05 UTC |
| `gridbot trend-status` | Book cash and holdings, PnL, recent decisions, reconciliation |
| `gridbot trend-halt` / `trend-resume` | stop / restart rebalancing; holdings stay |
| `gridbot trend-close` | close the Book and hand everything to the account pool; sells nothing |
| `gridbot trend-backtest [--years N]` | the live rebalancing rule on `data/candles/*-1D.csv` (`python research/fetch_1d.py`) |

### Spot Martingale (`-c config/martingale.toml`)

| command | what it does |
|---|---|
| `gridbot mart-plan` | the whole Ladder at today's price for the Book's (or the pool's) cash; places nothing |
| `gridbot mart-run` | create the Book from the account pool's USDT (or resume it) and run Cycles, polling every 5s |
| `gridbot mart-status` | cash, holding, average cost, add count, resting orders, cycles, realised profit, reconciliation |
| `gridbot mart-halt` / `mart-resume` | stop / restart placing orders; resting orders and holdings stay |
| `gridbot mart-close` | cancel the Ladder's resting orders and hand everything to the account pool; sells nothing |
| `gridbot sell-pool BTC\|ETH\|SOL` | sell what the account pool holds of that coin for USDT (IOC), when switching strategies |

### Grids

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
