# Borex Live — MT5 local service

Independent live trading service for Borex strategies. Uses `borex-main` as a
library for signal logic only; backtests and the viewer are not modified.

## Features

- **MT5 primary data** with Dukascopy cache fallback for warmup
- **Entry modes**: `ghost` (alexg4/5/6 → MT5 pending at SL) and `immediate` (alexg3+)
- **PostgreSQL state** on Railway — survives restarts, movable across machines
- **Live dashboard** at `http://127.0.0.1:8790/`

## Setup

```powershell
cd "c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Copy and fill in `.env` (see `.env.example`):

```ini
DATABASE_URL=postgresql://user:pass@host:port/railway
MT5_LOGIN=12345678
MT5_PASSWORD=your-password
MT5_DEMO_SERVER=Broker-Demo
```

`mt5service.py` loads `.env` automatically on startup.

## MT5 connection (Quantdemy-style)

Follow the same initialize shape as
[Quantdemy: Conectar Python y MetaTrader 5](https://quantdemy.com/conectar-python-y-metatrader-5):

```python
mt5.initialize(
    path=r"C:\Program Files\MetaTrader 5\terminal64.exe",
    login=...,
    password=...,
    server="ICMarketsSC-Demo",
    timeout=60000,
    portable=False,
)
```

**Use Python 3.11** (MetaTrader5 is unreliable on 3.13 here):

```powershell
cd "c:\Users\azeva\OneDrive\Documentos\work\trading\borex_live"
# already created:
.\.venv311\Scripts\Activate.ps1
python scripts\test_mt5_connect.py
```

If you see `(-10005, 'IPC timeout')`:

1. Open MT5 and log into the demo
2. **Tools → Options → Expert Advisors** → enable **Allow algorithmic trading** + **Allow DLL imports**
3. Click the **Algo Trading** button in the toolbar (must be green)
4. Prefer a **portable** MT5 install outside `Program Files` (e.g. `C:\MT5\`) and set `MT5_PATH` in `.env`
5. Run `scripts\test_mt5_connect.py` from a normal Windows Terminal (not only Cursor)

When `test_mt5_connect.py` prints `SUCCESS` and `trade_allowed True`, start the service:

```powershell
.\.venv311\Scripts\python.exe mt5service.py --demo --strategy alexg5 --leverage 5000 --rr-factor 2.5
```


```powershell
python mt5service.py --demo --strategy alexg5 --leverage 5000 --rr-factor 2.5 `
  --capital 1000 --position-size 0.01 --interval 1h --port 8790
```

AlexG6:

```powershell
python mt5service.py --demo --strategy alexg6 --leverage 5000 --rr-factor 2.5 `
  --second-signal off
```

Dry-run (no MT5 orders, no DB):

```powershell
python mt5service.py --dry-run --strategy alexg5 --tick-once --no-ui
```

## Entry modes

| Strategy | Mode | MT5 behavior |
|----------|------|----------------|
| alexg3 | `immediate` | Market order + SL/TP on signal |
| alexg4/5/6 | `ghost` | Pending limit at ghost SL when setup queues |

Future non-ghost strategies: register with `EntryMode.IMMEDIATE` in
`borex_live/strategy_registry.py`.

## Project layout

```
borex_live/
  mt5service.py          CLI
  borex_live/
    service.py           main loop
    engine/live_engine.py
    execution/router.py  ghost vs immediate routing
    mt5/client.py
    data/feed.py
    store/               Postgres models + repository
    api/server.py
    static/live.html
```
