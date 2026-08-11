# Borex Live — runbook

Windows-hosted MT5 demo live service. Strategies come from `borex-main` / monorepo
`borex/`; this package only runs the live loop, MT5 orders, Postgres state, and UI.

**Do not dockerize the whole service** — MetaTrader5 Python needs host IPC to
`terminal64.exe`. Only Postgres runs in Docker.

---

## Current production params (use these)

| Flag | Value | Notes |
|------|-------|--------|
| `--strategy` | `alexg7aligned` | Video2 ghost + London–NY overlap (12:00–16:00 UTC) for new setups |
| `--leverage` / `-l` | `5000` | |
| `--rr-factor` | `1.88` | Must pass explicitly (CLI default is still 2.5) |
| `--min-rr` | `3.0` | |
| `--capital` | `889.69` | Match DB / MT5 equity after week reset |
| `--position-size` | `0.01` | 1% risk |
| `--max-positions` | `60` | All FX pairs |
| `--interval` / `-i` | `1h` | |
| `--port` | `8790` | Dashboard |
| `--no-same-bar-exit` | (on) | Default is off; pass explicitly to match backtests |
| `--demo` | (on) | Uses `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_DEMO_SERVER` from `.env` |

Session filter for `alexg7aligned`: **overlap only**. Ghost fills do **not** re-check session.

Related strategy: `alexg8` = same geometry as `alexg7aligned` but `session="all"`.

Dashboard: [http://127.0.0.1:8790/](http://127.0.0.1:8790/)

---

## Paths

Pick one layout:

| Layout | Live root | Strategies root |
|--------|-----------|-----------------|
| Sibling (this machine) | `c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live` | sibling `borex-main` (auto) or `BOREX_MAIN_ROOT` |
| Monorepo `ci-cd` | `...\borex\deploy\borex_live` | set `BOREX_MAIN_ROOT` to repo root |

Examples below use the **sibling** path. For monorepo, `cd` into `deploy\borex_live` and set:

```powershell
$env:BOREX_MAIN_ROOT = "C:\path\to\borex"   # repo root that contains borex\
```

---

## 0. Prerequisites

- **Python 3.11** (MetaTrader5 is unreliable on 3.12/3.13 here)
- Docker Desktop (for local Postgres)
- MetaTrader 5 installed and logged into the **demo** account
- Tools → Options → Expert Advisors:
  - Allow algorithmic trading
  - Allow DLL imports
- Toolbar **Algo Trading** button green
- Use the **master** password in `.env`, not the investor password

---

## 1. One-time setup

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live

py -3.11 -m venv .venv311
.\.venv311\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
```

If strategies live in the monorepo / `borex-main`, also install that package’s deps when needed:

```powershell
# sibling:
pip install -r ..\borex-main\requirements.txt
# or monorepo:
# pip install -r ..\..\requirements.txt
```

Copy env and fill credentials:

```powershell
copy .env.example .env
notepad .env
```

Expected `.env` shape:

```ini
# Primary = local Docker Postgres (port 5433)
DATABASE_URL=postgresql://borex:borex@127.0.0.1:5433/borex_live

# Optional offsite mirror (Railway). Trading does not depend on this.
DATABASE_BACKUP_URL=postgresql://postgres:PASSWORD@HOST:PORT/railway

MT5_LOGIN=52958398
MT5_PASSWORD="your-master-password"
MT5_DEMO_SERVER=ICMarketsSC-Demo
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe

# Only if borex-main is not a sibling folder:
# BOREX_MAIN_ROOT=c:\Users\azeva\OneDrive\Documentos\work\trading\borex-main
```

`mt5service.py` loads `.env` on startup.

---

## 2. Start local Postgres

Start **Docker Desktop**, then:

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live
docker compose up -d
docker compose ps
```

Healthy container: `borex_live_pg` on host port **5433**.

### Optional: seed local from Railway (once)

```powershell
.\.venv311\Scripts\Activate.ps1
python scripts\db_sync.py from-railway
```

### Manual push local → Railway

```powershell
python scripts\db_sync.py to-railway
```

While live is running, local → Railway sync also runs every **300s** by default
(`--backup-interval 300`). Failures are logged; trading keeps using local DB.
Disable with `--backup-interval 0`.

---

## 3. Verify MT5

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live
.\.venv311\Scripts\Activate.ps1
python scripts\test_mt5_connect.py
```

Need `SUCCESS` and `trade_allowed True`.

If `(-10005, 'IPC timeout')`:

1. Open MT5 and log into the demo
2. Enable Expert Advisors + Algo Trading (see prerequisites)
3. Prefer a portable MT5 install outside `Program Files` and set `MT5_PATH` in `.env`
4. Run the smoke test from a normal Windows Terminal (not only Cursor)

---

## 4. Run live (canonical command)

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live
$env:PYTHONIOENCODING = "utf-8"
.\.venv311\Scripts\python.exe mt5service.py `
  --demo `
  --strategy alexg7aligned `
  --leverage 5000 `
  --rr-factor 1.88 `
  --min-rr 3.0 `
  --capital 889.69 `
  --position-size 0.01 `
  --max-positions 60 `
  --interval 1h `
  --port 8790 `
  --no-same-bar-exit
```

One-liner:

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live; $env:PYTHONIOENCODING="utf-8"; .\.venv311\Scripts\python.exe mt5service.py --demo --strategy alexg7aligned --leverage 5000 --rr-factor 1.88 --min-rr 3.0 --capital 889.69 --position-size 0.01 --max-positions 60 --interval 1h --port 8790 --no-same-bar-exit
```

Open UI: [http://127.0.0.1:8790/](http://127.0.0.1:8790/)

Useful log lines on startup:

- `strategy alexg7aligned`
- `same_bar_exit=off`
- `db_backup=on` (if `DATABASE_BACKUP_URL` set)
- hourly session status (overlap window)

Stop with `Ctrl+C`.

---

## 5. Week reset (cash / wipe trades)

Does **not** close MT5 positions — flatten the terminal yourself first if needed.
Keeps `bar_cursors` so historical H1 bars are not replayed.

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live
.\.venv311\Scripts\Activate.ps1
python scripts\reset_week.py --cash 889.69 --yes
```

Then restart live with the **same** `--capital 889.69`.

---

## 6. Alternate strategies / modes

### alexg8 (same as g7aligned, all sessions)

```powershell
.\.venv311\Scripts\python.exe mt5service.py --demo --strategy alexg8 --leverage 5000 --rr-factor 1.88 --min-rr 3.0 --capital 889.69 --position-size 0.01 --max-positions 60 --interval 1h --port 8790 --no-same-bar-exit
```

### Dry-run (no MT5 orders; DB optional)

```powershell
.\.venv311\Scripts\python.exe mt5service.py --dry-run --strategy alexg7aligned --leverage 5000 --rr-factor 1.88 --min-rr 3.0 --capital 889.69 --tick-once --no-ui
```

### Single poll then exit (with UI skipped)

```powershell
.\.venv311\Scripts\python.exe mt5service.py --demo --strategy alexg7aligned --leverage 5000 --rr-factor 1.88 --min-rr 3.0 --capital 889.69 --tick-once --no-ui
```

---

## 7. Matching backtest (viewerMT5)

Parity command used against MT5 H1 data (from `borex-main` / monorepo root):

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex-main
$py = "c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live\.venv311\Scripts\python.exe"
& $py -m borex.viewerMT5 `
  --strategy alexg7aligned `
  -i 1h -p 3y `
  --port 8775 `
  --no-same-bar-exit `
  --rr-factor 1.88 `
  --min-rr 3.0 `
  --capital 1000 `
  -l 5000 `
  --position-size 0.01 `
  --commission-per-lot 7.0 `
  --risk-include-commission `
  --no-browser
```

Notes:

- Backtest often uses `--capital 1000`; live week after reset uses **889.69**.
- Live lot sizing is 1% price-risk; commission-net sizing in live may still lag the backtest path.

---

## 8. Useful flags reference

| Flag | Default | Our value |
|------|---------|-----------|
| `--demo` | off | **on** |
| `--strategy` | `alexg7` | **`alexg7aligned`** |
| `--leverage` | `5000` | `5000` |
| `--rr-factor` | `2.5` | **`1.88`** |
| `--min-rr` | `3.0` | `3.0` |
| `--capital` | `1000` | **`889.69`** |
| `--position-size` | `0.01` | `0.01` |
| `--max-positions` | `60` | `60` |
| `--interval` | `1h` | `1h` |
| `--port` | `8790` | `8790` |
| `--poll` | `30` | (default) seconds between bar checks |
| `--same-bar-exit` / `--no-same-bar-exit` | no same-bar | **`--no-same-bar-exit`** |
| `--db` | `DATABASE_URL` | local Docker URL |
| `--db-backup` | `DATABASE_BACKUP_URL` | Railway |
| `--backup-interval` | `300` | seconds; `0` = off |
| `--dry-run` | off | no orders |
| `--tick-once` | off | one cycle then exit |
| `--no-ui` | off | skip FastAPI |

---

## 9. Entry modes

| Strategy | Mode | MT5 behavior |
|----------|------|----------------|
| alexg3 | `immediate` | Market + SL/TP on signal |
| alexg4/5/6/7/7aligned | `ghost` | Pending / ghost at SL; market on H1-close confirm |
| alexg8 | `ghost` | Same as g7aligned, any session |

---

## 10. Project layout

```
borex_live/
  mt5service.py              CLI entrypoint
  docker-compose.yml         local Postgres :5433
  .env / .env.example
  scripts/
    test_mt5_connect.py
    db_sync.py               from-railway | to-railway
    reset_week.py            wipe trades/ghosts, set cash
  borex_live/
    service.py               main loop + backup tick
    config.py
    engine/live_engine.py
    execution/router.py
    mt5/client.py
    data/feed.py
    store/                   models, repository, backup_sync
    api/server.py
    static/live.html
```

---

## Quick checklist (every restart)

1. Docker Desktop running → `docker compose up -d`
2. MT5 open, demo logged in, Algo Trading green
3. `.env` has local `DATABASE_URL` (+ optional `DATABASE_BACKUP_URL`)
4. Run the **canonical** `alexg7aligned` command with `--capital 889.69` and `--rr-factor 1.88`
5. Open [http://127.0.0.1:8790/](http://127.0.0.1:8790/)
