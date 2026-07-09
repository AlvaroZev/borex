# Trade viewer and market analysis UI

Web UI for inspecting AlexG3 backtests and multi-market strategy decisions. Built with FastAPI + [Lightweight Charts](https://tradingview.github.io/lightweight-charts/) v4.

**Added in 2026-07 (viewer + analysis work):**

- Doubled trade chart height in the trade viewer
- New `/analysis` page: AlexG3 decisions across all Dukascopy FX pairs
- Time-synced stacked charts (zoom, pan, crosshair)
- CSV export/import so `/analysis` can be reopened without rescanning signals

---

## Quick start

### Trade viewer (per-trade charts)

```powershell
cd "c:\Users\azeva\OneDrive\Documentos\work\trading\borex-main"
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --port 8765
```

- **Trade viewer:** http://127.0.0.1:8765/
- **Market analysis:** http://127.0.0.1:8765/analysis

Requires cached Dukascopy parquet under `data/cache/` (see `borex/data/store.py`). Use `--use-cache` to read cache only (no network).

### Analysis only (load saved CSV, no backtest, no scan)

```powershell
python -m borex.viewer --strategy alexg3 `
  --load-analysis data/analysis/alexg3_max_1h `
  --analysis-only --port 8765
```

Opens `/analysis` directly. No Dukascopy cache needed if the bundle includes `candles.csv`.

---

## Pages

### `/` — Trade viewer

Single-page app: trade list, confirmation stats, per-trade OHLC chart with markers.

| UI area | Content |
|---------|---------|
| Sidebar | Trades with PnL, trend, setup, AOI, confirmation label |
| Main panel | Trade detail + candlestick chart |
| Chart markers | Yellow = signal candle, green/red = entry/exit, blue/purple = AOI |

**Chart height:** default height was doubled (CSS ~680px / ~76vh; JS clamp 520–680px) so individual trade charts are easier to read.

Link in header: **Market Analysis →** goes to `/analysis`.

### `/analysis` — Multi-market AlexG3 decisions

Shows **all AlexG3 signals** the strategy would emit across the full FX universe (not only trades that received a portfolio slot in the backtest).

| Feature | Description |
|---------|-------------|
| Stacked charts | One chart per market, vertically scrollable |
| Decision fields | Trend, setup, AOI kind, confirmation (signal), currency bias, strength |
| AOI toggle | Show/hide support/resistance price lines |
| Filters | By trend, confirmation type, or single market |
| Chart order | Drag symbols in sidebar or use ↑/↓ buttons |
| Decision log | Scrollable table; click a row to jump all charts to that time |
| Responsive | Sidebar moves above charts on narrow screens; chart height uses `clamp()` |

**AlexG3 only:** `/api/analysis/*` returns 404 for non–multi-market sessions.

---

## Time sync across markets

Charts share the **master timeline** (`EURUSD=X` by default, or longest series if EURUSD is missing).

### Backend

- On scan, candles are aligned to master bar timestamps (`borex/viewer/analysis.py`).
- Each market’s OHLC in the API uses only bars that exist at those master times.
- Signals at the same master bar share the same `time_unix` and line up horizontally.

### Frontend

- **Zoom/pan sync** — changing visible range on one chart updates all others
- **Crosshair sync** — hover shows the same time on every chart
- **“Sync time across charts”** checkbox (default on)
- **Click decision row** — sets visible window and crosshair on all charts

Hint bar at top of `/analysis` shows master symbol and timeline range.

---

## CSV analysis bundle (save / load)

Avoid rescanning the full Dukascopy history on every viewer launch.

### Save after a full run

```powershell
python -m borex.viewer --strategy alexg5 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 --leverage 5000 --save-analysis --save-trades
```

Default output directory:

```text
data/runs/{strategy}_{period}_{interval}/
```

Example: `data/runs/alexg5_max_1h/`

Custom path (both flags share the same folder):

```powershell
--save-analysis data/my_run --save-trades data/my_run
```

### Bundle contents

| File | Contents |
|------|----------|
| `manifest.json` | Strategy, timeframe, symbols, master symbol, date ranges, bar counts, saved_at |
| `decisions.csv` | Every signal: time, symbol, action, trend, setup, aoi_kind, signal, SL/TP, pattern, strength, … |
| `candles.csv` | Master-aligned OHLC: `symbol`, `time_unix`, `open`, `high`, `low`, `close` |
| `aoi.csv` | Latest AOI zones per symbol: `level`, `kind`, `touches`, `recency` |
| `timeline.csv` | Master timeline `time_unix` column |
| `trades.csv` | Backtest trades (with `--save-trades`): entry/exit, PnL, SL/TP, pattern, margin, … |

Implementation: `borex/viewer/analysis_store.py` (analysis), `borex/viewer/trade_store.py` (trades).

### Load later

**Skip signal scan** (still runs backtest if not `--analysis-only`):

```powershell
python -m borex.viewer --strategy alexg3 -p max -i 1h --use-cache `
  --load-analysis data/analysis/alexg3_max_1h --port 8765
```

**Analysis page only** (fastest — no backtest, no scan, no cache):

```powershell
python -m borex.viewer --strategy alexg3 `
  --load-analysis data/analysis/alexg3_max_1h --analysis-only --port 8765
```

When loaded from CSV, `/analysis` shows a **“Loaded from CSV”** badge.

---

## CLI reference (`python -m borex.viewer`)

| Flag | Description |
|------|-------------|
| `--strategy alexg3` | Required for multi-market analysis |
| `-s` / `--symbol` | Display symbol; also preferred master timeline |
| `-p` / `--period` | History period (`max`, `60d`, …) |
| `-i` / `--interval` | Bar size (`1h`, …) |
| `--use-cache` | Dukascopy parquet only (`data/cache/`) |
| `--symbols` | Override FX universe (default: 10 pairs in `FOREX_PAIRS`) |
| `--save-analysis [DIR]` | Save CSV bundle after scan (default dir if DIR omitted) |
| `--save-trades [DIR]` | Save backtest `trades.csv` (same folder as analysis when both set) |
| `--load-analysis DIR` | Load bundle instead of scanning |
| `--analysis-only` | With `--load-analysis`: skip backtest; serve `/analysis` only |
| `--port` | HTTP port (default `8765`) |
| `--no-browser` | Do not open browser on start |

Other flags (`--capital`, `--leverage`, `--inversed`, `--max-positions`, …) apply to the backtest portion of the session.

---

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Trade viewer HTML |
| GET | `/analysis` | Market analysis HTML |
| GET | `/api/session` | Backtest summary + trades + confirmation stats |
| GET | `/api/trades/{id}/chart` | OHLC slice, markers, SL/TP/AOI for one trade |
| GET | `/api/analysis/overview` | Master symbol, symbols, decisions, timeline bounds |
| GET | `/api/analysis/markets?show_aoi=true` | Per-market candles, decisions, AOI levels |

Session is in-memory (503 if viewer started without a successful `run_session`).

---

## Code layout

```text
borex/viewer/
├── __main__.py          # CLI: backtest + optional scan/save/load + uvicorn
├── server.py            # FastAPI routes
├── context.py           # ViewerSession, trade summaries, trade charts
├── analysis.py          # scan_alexg3_decisions, MarketAnalysis
├── analysis_store.py    # CSV bundle save/load
└── static/
    ├── index.html       # Trade viewer
    └── analysis.html    # Multi-market analysis UI
```

### Analysis pipeline

1. Load all pairs from Dukascopy cache (`load_market_data`).
2. Run `MultiMarketEngine` backtest → trades for `/`.
3. Run `scan_alexg3_decisions` → all signals (no portfolio cap) for `/analysis`.
4. Optionally `save_analysis_bundle` → CSV files.
5. On `--load-analysis`, restore `MarketAnalysis` from CSV and skip step 3.

`scan_alexg3_decisions` mirrors the engine’s master-bar loop and calls `AlexG3Strategy.on_bar` per symbol with `MultiMarketContext` (currency strength filter included).

### Decision payload (on each signal)

Parsed from `alexg3` pattern string and exposed in UI/API:

- `trend` — bullish / bearish  
- `setup` — bounce / continuation  
- `aoi_kind` — support / resistance  
- `signal` / `signal_label` — confirmation (rejection, engulfing, momentum, …)  
- `currency_bias` — e.g. `EUR>USD`  
- `strength` — top currency strength snapshot  
- `stop_loss`, `take_profit`, `price`

---

## Related docs

- [ALEXG3_RESULTS_HISTORY.md](./ALEXG3_RESULTS_HISTORY.md) — experiment leaderboard and viewer launch commands  
- `scripts/run_alexg3_csv.py` — export executed trades to CSV (portfolio results, not full decision scan)

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `/analysis` 404 | Start viewer with `--strategy alexg3` and multi-market data |
| Slow startup on `max` | Use `--load-analysis` or `--analysis-only` after first `--save-analysis` |
| Empty charts in analysis-only | Ensure bundle has `candles.csv` from a prior save |
| No cache data | Copy `data/cache/` from main `borex` repo or download Dukascopy parquet first |
| Trade viewer empty with `--analysis-only` | Expected — use full run without `--analysis-only` for trades |
