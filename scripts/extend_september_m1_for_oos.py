#!/usr/bin/env python3
"""Extend utc_v2 M1 through closed September 2026 for locked OOS (data only).

MT5 copy_rates → data/cache/mt5/utc_v2 only.
No HistData / no vendor mix / no strategy / no orders.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "borex_live"))

from dotenv import load_dotenv

load_dotenv(ROOT.parent / "borex_live" / ".env")

import importlib.util  # noqa: E402

from borex.viewerMT5.mt5_feed import _cache_path  # noqa: E402


def _load_extend_mod():
    path = ROOT / "scripts" / "extend_mt5_historical_coverage.py"
    spec = importlib.util.spec_from_file_location("extend_mt5_historical_coverage", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_EXT = _load_extend_mod()
fetch_range = _EXT.fetch_range
month_stats = _EXT.month_stats
universe_hash = _EXT.universe_hash


def _connect_historical_readonly():
    """Attach to Mirror demo for copy_rates only. Refuse Live login."""
    import os

    from borex_live.mt5.client import Mt5Client

    # Host layout: MT5_PATH -> Mirror demo 53019882; MIRROR_MT5_PATH -> Live.
    path = os.environ.get("MT5_PATH") or os.environ.get("MIRROR_MT5_PATH") or ""
    login = os.environ.get("MIRROR_MT5_LOGIN") or os.environ.get("MT5_LOGIN")
    password = os.environ.get("MIRROR_MT5_PASSWORD") or os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MIRROR_MT5_SERVER") or os.environ.get("MT5_SERVER") or ""
    client = Mt5Client(
        path=path,
        login=int(login) if login else None,
        password=password or None,
        server=server or None,
    )
    client.connect()
    mt5 = client._mt5
    acc = mt5.account_info() if mt5 is not None else None
    acc_login = int(getattr(acc, "login", 0) or 0)
    if acc_login == 52958398:
        client._connected = False
        client._mt5 = None
        raise SystemExit("Refusing Live account 52958398 for historical extension")
    if acc_login != 53019882:
        print(f"WARNING: expected Mirror 53019882, got {acc_login}", flush=True)
    return client, {
        "path": path,
        "login": acc_login,
        "server": str(getattr(acc, "server", "") or ""),
        "offset_seconds": int(getattr(client, "server_offset_seconds", 0) or 0),
        "orders_placed": False,
        "trade_apis_called": False,
    }

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"


def m1_sep_tmax(symbols: list[str]) -> dict:
    sep_start = pd.Timestamp("2026-09-01", tz="UTC")
    sep_end = pd.Timestamp("2026-10-01", tz="UTC")
    out = {}
    n30 = 0
    for sym in symbols:
        path = _cache_path(sym, "1m")
        if not path.is_file():
            out[sym] = {"tmax": None, "has_sep30": False, "n": 0}
            continue
        df = pd.read_parquet(path, columns=["timestamp"])
        ts = pd.to_datetime(df["timestamp"], utc=True)
        sub = ts[(ts >= sep_start) & (ts < sep_end)]
        has30 = bool(
            ((sub.dt.year == 2026) & (sub.dt.month == 9) & (sub.dt.day == 30)).any()
        )
        if has30:
            n30 += 1
        out[sym] = {
            "tmax": str(sub.max()) if len(sub) else None,
            "has_sep30": has30,
            "n": int(len(sub)),
        }
    return {"per_symbol": out, "complete_through_sep30": n30, "n_symbols": len(symbols)}


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"september_m1_extension_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun["spread_pips_by_symbol"].keys())
    assert len(symbols) == 60
    uhash = universe_hash(symbols)

    before = m1_sep_tmax(symbols)
    print(
        f"BEFORE M1 through Sep30: {before['complete_through_sep30']}/60",
        flush=True,
    )
    (out / "m1_before.json").write_text(
        json.dumps(before, indent=2), encoding="utf-8"
    )

    if before["complete_through_sep30"] == 60:
        print("Already complete — nothing to fetch.", flush=True)
        (out / "status.md").write_text(
            "# September M1 extension\n\nAlready 60/60 through Sep 30.\n",
            encoding="utf-8",
        )
        return 0

    # Prefer the terminal that actually holds Mirror demo login 53019882.
    # On this host MIRROR_MT5_PATH points at Live (52958398); MT5_PATH is Mirror.
    client, conn_meta = _connect_historical_readonly()
    print(
        f"Connected login={conn_meta['login']} server={conn_meta['server']} "
        f"(historical only, orders_placed={conn_meta['orders_placed']})",
        flush=True,
    )
    try:
        # From a few days before the hole through now (covers Sep 25→30 + early Oct).
        start = datetime(2026, 9, 24, tzinfo=timezone.utc)
        end = datetime.now(timezone.utc) + timedelta(hours=1)
        print(f"=== Extend M1 {start.isoformat()} -> {end.isoformat()} ===", flush=True)
        log_m1 = fetch_range(
            client, symbols, interval="1m", start=start, end=end, label="sep_m1_gap"
        )
        print("=== Extend H1 same window (safety) ===", flush=True)
        log_h1 = fetch_range(
            client, symbols, interval="1h", start=start, end=end, label="sep_h1_gap"
        )
    finally:
        client._connected = False
        client._mt5 = None
        print("Disconnected (no orders).", flush=True)

    after = m1_sep_tmax(symbols)
    matrix = month_stats(symbols, 2026, 9)
    print(
        f"AFTER M1 through Sep30: {after['complete_through_sep30']}/60",
        flush=True,
    )
    (out / "m1_after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    (out / "acquisition_log.json").write_text(
        json.dumps({"m1": log_m1, "h1": log_h1, "account": conn_meta}, indent=2, default=str),
        encoding="utf-8",
    )
    (out / "september_month_stats.json").write_text(
        json.dumps(matrix, indent=2, default=str), encoding="utf-8"
    )

    ok = after["complete_through_sep30"] == 60
    status = f"""# September M1 extension for locked OOS

Generated: {datetime.now(timezone.utc).isoformat()}
Universe hash: `{uhash}`
Account login: `{conn_meta['login']}` (read-only historical)
Orders placed: `{conn_meta['orders_placed']}`

## Coverage

- BEFORE M1 through Sep 30: **{before['complete_through_sep30']}/60**
- AFTER M1 through Sep 30: **{after['complete_through_sep30']}/60**
- Ready for locked September OOS: **{ok}**

## Provenance

- Source: MT5 `copy_rates` via `fetch_mt5_candles`
- Cache: `data/cache/mt5/utc_v2`
- No HistData / no vendor mix
- No strategy backtest executed
"""
    (out / "status.md").write_text(status, encoding="utf-8")
    print(f"READY_FOR_OOS={ok}", flush=True)
    print(f"Artifacts -> {out}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
