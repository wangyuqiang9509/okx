# OKX BTC Grid Bot

Fixed-range spot grid on OKX BTC-USDT. Vocabulary in `CONTEXT.md`, decisions in `docs/adr/`.

## Setup

```sh
uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -e ".[dev]"
cp .env.example .env   # fill in OKX API key (trade permission, IP-bound)
.venv/bin/pytest
```

## Commands

All take `--config config/validate.toml` (default) or `--config config/live.toml`.

| command | what it does |
|---|---|
| `gridbot replay --days 30` | feed the last 30 days of 1m candles through the engine, print Grid Profit and breakouts |
| `gridbot start` | resume the open grid, or create one at the current price (Seed Buy, then all Level orders) |
| `gridbot status` | grid, Levels, cash/base, realised profit, last reconciliation (ledger only, no API call) |
| `gridbot halt` / `resume` | stop / restart placing new orders; resting orders stay on OKX |
| `gridbot cancel-all` | cancel every resting order and close the grid; you keep whatever you hold |
| `gridbot reset` | cancel-all, then start a fresh grid |
| `gridbot check` | promotion criteria over the last 24h: no code-caused Halt, all reconciliations clean, every sell matched to a buy |

## Docker

```sh
docker compose up -d --build
docker compose logs -f
docker exec gridbot gridbot status
docker exec gridbot gridbot halt
```

Switch capital by editing `command:` in `docker-compose.yml` to `config/live.toml` after `gridbot check` passes; the ledger keeps both grids' history.

## Rollout

1. `replay` on both configs.
2. `validate.toml`: 50 USDT, ±3% / 0.3%, run 24h, then `check`.
3. `live.toml`: 500 USDT, ±8% / 1%, run 30 days.
