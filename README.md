# Borex — Forex Backtesting Platform

Local-first backtesting for forex pairs (EUR/USD, GBP/USD, etc.). Download OHLCV, run strategies in mass, rank results, and prepare for AI parameter optimization.

## Why Python

Python fits data pipelines, pandas/numpy backtests, and future ML/AI optimizers. Your Go experience still applies to deploying APIs; the core engine stays Python. FastAPI serves the React UI.

## Yahoo data limits (verified)

| Timeframe | Max history |
|-----------|-------------|
| 1m | ~30 days (7-day chunks) |
| 15m / 30m | ~60 days |
| 1h / 4h | ~730 days |
| 1d / 1wk | Full history |

Deeper 1m history can be added later via a pluggable source (e.g. Dukascopy).

## Quick start

```powershell
pip install -r requirements.txt

# Download all forex pairs × timeframes
python -m borex download --all

# Single backtest
python -m borex backtest --strategy sma_cross -s EURUSD=X -t 1h

# Mass: all strategies × symbols × timeframes
python -m borex mass --workers 8

# API + React UI (live progress)
python -m borex serve
cd frontend ; npm install ; npm run dev
```

Open http://localhost:5173 — mass backtests and downloads stream live progress via SSE (progress bar, live feed, top results updating as each job finishes).

## CLI

| Command | Description |
|---------|-------------|
| `download --all` | Cache all pairs/timeframes |
| `backtest` | Single run |
| `mass` | Cartesian product run |
| `walkforward` | 70/30 split or `--rolling` windows |
| `walkforward --rolling --optimize` | Rolling OOS with train-window param search |
| `sweep` | Parameter grid search with SQLite persistence |
| `backtest --regimes` | Include bull/bear/sideways + vol breakdown |
| `audit --all` | Data integrity check (gaps, OHLC, manifests) |
| `audit --fix` | Rebuild corrupt datasets (duplicate-TF, bad chunks) |
| `audit --repair-manifests` | Backfill dataset manifests for reproducibility |
| `paper` | Paper trading on live-refreshed data |
| `monitor` | Session health, divergence, decision log |
| `revalidate` | Decay check: baseline vs recent window |
| `screen` | Mass sweep + rolling WF; promote OOS configs passing gates |
| `pipeline run` | Full testing pipeline: audit → screen → revalidate → retire |
| `pipeline tick` | Tick all active paper sessions |
| `pipeline watch` | Daemon: tick + periodic revalidate |
| `list --cached` | Show local data |
| `list --leaderboard` | Top results by metric |
| `serve` | FastAPI on :8000 |

## Defaults

- Capital: $1,000
- Leverage: 500× (configurable up to 5,000)
- Portfolio: up to 5 concurrent positions
- Exits: price-reached SL/TP; same-bar conflict → stop loss first; no exit on entry bar

## Risk management (Phase 3)

Position sizing and portfolio guardrails apply to `backtest`, `mass`, `walkforward`, and `sweep`:

| Flag | Description |
|------|-------------|
| `--size-mode fixed` | Default; use signal `size_pct` |
| `--size-mode atr_risk` | Size from ATR stop distance + `--risk-per-trade` |
| `--size-mode kelly` | Half-Kelly from closed-trade stats (after `--kelly-min-trades`) |
| `--max-drawdown 0.2` | Halt new entries after 20% peak drawdown |
| `--max-daily-loss 0.05` | Halt new entries after 5% daily loss |
| `--max-currency-exposure 1` | Limit net same-direction bets per currency |
| `--no-correlation-limit` | Disable currency exposure checks |

Example:

```powershell
python -m borex backtest --strategy sma_cross -s EURUSD=X -t 1h `
  --size-mode atr_risk --max-drawdown 0.2 --max-daily-loss 0.05
```

Backtest JSON includes `risk_stats` (halt reason, circuit breaker triggers, correlation blocks).

## Execution realism (Phase 4)

Model spread, slippage, fill timing, and reconcile theoretical vs actual PnL:

| Flag | Description |
|------|-------------|
| `--spread 0.00005` | Half-spread per side (~0.5 pip on EURUSD) |
| `--slippage-mode atr` | Scale slippage with ATR volatility |
| `--fill-mode next_open` | Enter on next bar open (not signal close) |
| `--entry-delay 1` | Additional bar latency before fill |
| `--commission 0.0001` | Per-side commission on notional |

Backtest JSON includes `execution_stats`: commission, spread/slippage cost, theoretical PnL, execution drag.

### Paper trading

Run strategies on refreshed Yahoo data with simulated capital:

```powershell
# Create session (warms up on full history)
python -m borex paper --strategy sma_cross -s EURUSD=X -t 1h --spread 0.00005

# Single tick (process new bars since last poll)
python -m borex paper --session <id>

# Poll every 5 minutes
python -m borex paper --session <id> --loop --poll 300

python -m borex paper --list
```

API: `POST /api/paper/sessions`, `POST /api/paper/sessions/{id}/tick`, `GET /api/paper/sessions`

## Live deployment (Phase 5)

Kill-switch, decision audit log, and live vs backtest divergence monitoring for paper sessions:

| Flag / command | Description |
|----------------|-------------|
| `--max-errors 3` | Kill after consecutive tick failures |
| `--stale-minutes 180` | Kill on tick if data is stale |
| `--divergence-warn 0.2` | Alert when live return diverges 20% from baseline |
| `paper --kill --session ID` | Manual kill-switch |
| `paper --resume --session ID` | Reset kill-switch |
| `monitor --session ID` | Health, divergence, alerts |
| `monitor --session ID --decisions` | Full decision audit trail |

Every signal, entry, exit, block, halt, and error is logged to SQLite (`decision_log`). Alerts stored in `live_alerts`.

```powershell
python -m borex paper --strategy sma_cross -s EURUSD=X -t 1h --max-errors 3
python -m borex paper --session <id> --monitor
python -m borex monitor --session <id> --decisions --alerts
python -m borex paper --session <id> --kill --kill-reason "testing"
```

API: `GET /api/paper/sessions/{id}/monitor`, `/decisions`, `/alerts`, `POST .../kill`, `POST .../resume`

## Iteration (Phase 6)

Periodic re-validation detects strategy decay when recent performance diverges from historical baseline:

| Flag | Description |
|------|-------------|
| `--recent-months 3` | Recent window to re-test |
| `--baseline-months 12` | Baseline window (default: all history before recent) |
| `--decay-sharpe 0.5` | Sharpe drop threshold for `decayed` verdict |
| `--decay-return 15` | Return lag threshold (percentage points) |
| `--session ID` | Include paper session for capital scale recommendation |

Verdicts: `healthy`, `warning`, `decayed`, `insufficient_data`. Capital recommendation requires min paper days/trades, healthy decay verdict, and no kill-switch.

```powershell
python -m borex revalidate --strategy sma_cross -s EURUSD=X -t 1h --recent-months 3
python -m borex revalidate --strategy sma_cross -s EURUSD=X --session <paper_id>
python -m borex revalidate --list
python -m borex revalidate --id <run_id>
```

API: `POST /api/revalidate`, `GET /api/revalidate`, `GET /api/revalidate/{id}`

### Automated screen pipeline

Run parameter sweeps + rolling walk-forward across strategies × symbols × timeframes. Only configs that pass OOS gates are promoted (ranked by avg OOS Sharpe). Optional `--create-paper` spins up paper sessions for the top N.

| Gate flag | Default | Description |
|-----------|---------|-------------|
| `--min-oos-sharpe` | 0.5 | Min average OOS Sharpe across folds |
| `--min-oos-trades` | 10 | Min total OOS trades |
| `--max-oos-drawdown` | 35 | Max OOS drawdown (%) |
| `--min-positive-folds` | 0.5 | Min fraction of folds with positive OOS return |

Defaults to **1h** entry timeframe only (pass `--timeframes` for more). Uses train-window optimization like `walkforward --rolling --optimize`.

```powershell
# Quick smoke (1 strategy × 1 pair)
python -m borex screen --strategies sma_cross --symbols EURUSD=X --workers 1 --max-combos 4 --max-points 2

# Full screen with cost realism + paper for top 3
python -m borex screen --spread 0.00005 --min-oos-sharpe 0.5 --create-paper --top-n-paper 3

python -m borex screen --list
python -m borex screen --id <run_id>
```

API: `POST /api/screen`, `GET /api/screen`, `GET /api/screen/{id}`

React UI **Research** tab includes a screen panel (defaults to sma_cross + EURUSD for a quick run).

### Testing pipeline (hands-off automation)

Chain audit, screen, paper, revalidate, and auto-retire decayed sessions:

```powershell
# Weekly research (creates paper for top promoted by default)
python -m borex pipeline run --spread 0.00005 --min-oos-sharpe 0.5 --workers 8

# Revalidate-only pass (skip screen)
python -m borex pipeline run --skip-screen

# Tick all active paper sessions (+ revalidate + kill decayed)
python -m borex pipeline tick --revalidate

# Daemon: tick every 5 min, revalidate daily (~288 cycles @ 5min)
python -m borex pipeline watch --poll 300 --revalidate-every 288

python -m borex pipeline list
python -m borex pipeline show --id <run_id>
```

**Task Scheduler setup (Windows):**
- Weekly: `pipeline run` (Sunday night)
- Always-on: `pipeline watch` (separate terminal/service)
- Webhook digest sent on each `pipeline run` if alerts enabled

API: `POST /api/pipeline/run`, `POST /api/pipeline/tick`, `GET /api/pipeline`, `GET /api/pipeline/{id}`

### Alert delivery + UI

Outbound alerts fire on every `live_alerts` insert (stale data, divergence, kill-switch, etc.):

- Configure in UI **Paper & revalidate** tab, or `data/alert_config.json`
- Env override: `BOREX_WEBHOOK_URL`, `BOREX_SLACK_WEBHOOK_URL`
- API: `GET/PUT /api/alerts/config`, `POST /api/alerts/test`

React UI tab **Paper & revalidate**: session monitor, alerts/decisions feed, webhook config, decay re-validation.

## Adding strategies

Implement `Strategy` in `borex/strategy/`, expose `param_schema()`, register with `@register`. Your strategy list in the next prompt will be added here.

## Project layout

```
borex/
  data/       download + parquet cache
  strategy/   pluggable strategies + param schemas
  backtest/   engine, portfolio, execution
  runner/     mass runs, walk-forward, SQLite results
  api/        FastAPI for React UI
frontend/     Vite + React dashboard
```
