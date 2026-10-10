#!/usr/bin/env python3
"""Focused Live-universe pending replay (72 / 500 / common-history).

Uses the Live process symbol list when available so universe matches.
Writes progress artifacts; common-history uses light checkpoints.
"""

from __future__ import annotations

import csv
import json
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

LIVE_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")
MAIN_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main")
sys.path[:0] = [str(MAIN_ROOT), str(LIVE_ROOT)]

from borex.alexg import AlexG8Strategy
from borex.alexg.force_flat import blocks_new_entries
from borex.alexg.multi_market import (
    MultiMarketContext,
    align_symbols_to_timeline,
    default_forex_universe,
)
from borex.backtest.engine import BacktestConfig
from borex.data.mt5_time import measure_mt5_server_offset_seconds, mt5_unix_to_utc
from borex.models.candle import Candle
from borex_live.execution.router import read_pending_snapshot
from borex_mirror.engine import _ThursdayEntryGate

RUN = Path(sys.argv[1])
RUN.mkdir(parents=True, exist_ok=True)

TARGET_WARMUP = 10_000
# Bound: prefer completing vs stalling forever
MAX_RUNTIME_SEC = int(sys.argv[2]) if len(sys.argv) > 2 else 3 * 60 * 60
PROGRESS_EVERY = 100


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def live_symbols() -> list[str]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8791/api/status", timeout=5) as r:
            data = json.loads(r.read().decode())
        syms = list(data.get("symbols") or [])
        if syms:
            return syms
    except Exception:
        pass
    return default_forex_universe()


def pending_rows(strategy: Any, *, source: str) -> list[dict[str, Any]]:
    snap = read_pending_snapshot(strategy)
    rows = []
    for g in snap.values():
        rows.append(
            {
                "symbol": g.symbol,
                "action": g.action,
                "planned_entry": g.planned_entry,
                "stop_loss": g.stop_loss,
                "take_profit": g.take_profit,
                "created_index": g.created_index,
                "expires_index": g.expires_index,
                "saw_near_sl": g.saw_near_sl,
                "pattern": (g.pattern or "")[:240],
                "source": source,
            }
        )
    return sorted(rows, key=lambda r: r["symbol"])


def rates_to_candles(rates, offset: int) -> list[Candle]:
    out: list[Candle] = []
    for r in rates:
        out.append(
            Candle(
                timestamp=pd.Timestamp(mt5_unix_to_utc(int(r["time"]), offset)),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["tick_volume"]),
            )
        )
    return out


def load_h1(symbols: list[str], count: int) -> tuple[dict[str, list[Candle]], int, list[dict]]:
    import MetaTrader5 as mt5

    status_rows: list[dict] = []
    assert mt5.initialize(timeout=90_000), mt5.last_error()
    offset = int(measure_mt5_server_offset_seconds(mt5) or 0)
    now = pd.Timestamp.now(tz="UTC")
    h1: dict[str, list[Candle]] = {}
    for i, sym in enumerate(symbols):
        t0 = time.time()
        mt5_sym = sym.replace("=X", "")
        try:
            info = mt5.symbol_info(mt5_sym)
            if info is None:
                mt5.symbol_select(mt5_sym, True)
                info = mt5.symbol_info(mt5_sym)
            if info is None:
                status_rows.append(
                    {"symbol": sym, "ok": False, "n_bars": 0, "error": "no_symbol", "sec": 0}
                )
                continue
            mt5.symbol_select(mt5_sym, True)
            rates = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_H1, 0, count)
            if rates is None or len(rates) < 120:
                status_rows.append(
                    {
                        "symbol": sym,
                        "ok": False,
                        "n_bars": 0 if rates is None else len(rates),
                        "error": str(mt5.last_error()),
                        "sec": round(time.time() - t0, 3),
                    }
                )
                continue
            bars = rates_to_candles(rates, offset)
            if bars and _ts(bars[-1].timestamp).floor("h") == now.floor("h"):
                bars = bars[:-1]
            if len(bars) < 120:
                status_rows.append(
                    {
                        "symbol": sym,
                        "ok": False,
                        "n_bars": len(bars),
                        "error": "too_short",
                        "sec": round(time.time() - t0, 3),
                    }
                )
                continue
            h1[sym] = bars
            status_rows.append(
                {
                    "symbol": sym,
                    "ok": True,
                    "n_bars": len(bars),
                    "first": str(bars[0].timestamp),
                    "last": str(bars[-1].timestamp),
                    "error": "",
                    "sec": round(time.time() - t0, 3),
                }
            )
        except Exception as exc:
            status_rows.append(
                {
                    "symbol": sym,
                    "ok": False,
                    "n_bars": 0,
                    "error": repr(exc),
                    "sec": round(time.time() - t0, 3),
                }
            )
        if (i + 1) % 5 == 0 or i + 1 == len(symbols):
            write_json(
                RUN / "PROGRESS.json",
                {
                    "phase": "load_h1",
                    "i": i + 1,
                    "n": len(symbols),
                    "ok_pairs": len(h1),
                    "utc": datetime.now(timezone.utc).isoformat(),
                },
            )
    mt5.shutdown()
    return h1, offset, status_rows


def make_strategy() -> Any:
    s = AlexG8Strategy(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=4.0,
    )
    return _ThursdayEntryGate(s)


def make_bt_config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=1000.0,
        leverage=500.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=5.0,
        commission_per_lot=7.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_daily=True,
        force_flat_friday=False,
    )


def replay_pending_only(
    name: str,
    h1: dict[str, list[Candle]],
    master: str,
    *,
    start_i: int,
    stop_i: int,
    deadline: float,
) -> dict[str, Any]:
    strategy = make_strategy()
    bt = make_bt_config()
    symbols = list(h1.keys())
    master_bars = h1[master]
    stop_i = min(stop_i, len(master_bars))
    min_bars = int(getattr(getattr(strategy, "_inner", strategy), "min_bars", 120))
    start_i = max(min_bars, start_i)
    ts_maps = align_symbols_to_timeline(master_bars, h1)
    inner = getattr(strategy, "_inner", strategy)
    strength_lookback = getattr(inner, "strength_lookback", 24)
    min_currency_edge = getattr(inner, "min_currency_edge", 0.0)
    min_confirming_pairs = getattr(inner, "min_confirming_pairs", 0)

    for mi in range(start_i, stop_i):
        if time.time() > deadline:
            pend = pending_rows(strategy, source=name)
            write_json(
                RUN / f"CHECKPOINT_{name}.json",
                {
                    "name": name,
                    "failed": "timeout",
                    "master_i": mi,
                    "pending_n": len(pend),
                    "pending_symbols": [p["symbol"] for p in pend],
                },
            )
            raise TimeoutError(f"{name} timeout at master_i={mi}")

        ctx = MultiMarketContext.at_master_bar(
            mi,
            master_bars,
            h1,
            ts_maps,
            strength_lookback=strength_lookback,
            min_currency_edge=min_currency_edge,
            min_confirming_pairs=min_confirming_pairs,
        )
        for sym in symbols:
            idx = ctx.indices.get(sym)
            if idx is None or idx < min_bars:
                continue
            bar = h1[sym][idx]
            if blocks_new_entries(bar.timestamp, bt):
                continue
            strategy.set_context(sym, ctx)
            strategy.on_bar(idx, h1[sym], None)

        if (mi - start_i) % PROGRESS_EVERY == 0 or mi == stop_i - 1:
            pend = pending_rows(strategy, source=name)
            write_json(
                RUN / "PROGRESS.json",
                {
                    "phase": name,
                    "master_i": mi,
                    "start_i": start_i,
                    "stop_i": stop_i,
                    "pct": round(100.0 * (mi - start_i + 1) / max(1, stop_i - start_i), 2),
                    "pending_n": len(pend),
                    "pending_symbols": [p["symbol"] for p in pend],
                    "utc": datetime.now(timezone.utc).isoformat(),
                },
            )

    rows = pending_rows(strategy, source=name)
    gb = next((r for r in rows if r["symbol"] == "GBPUSD=X"), None)
    return {
        "name": name,
        "start_i": start_i,
        "stop_i": stop_i,
        "bars_processed": stop_i - start_i,
        "tip_timestamp": str(master_bars[stop_i - 1].timestamp),
        "pending_n": len(rows),
        "pending": rows,
        "gbpusd": gb,
        "completed": True,
    }


def main() -> int:
    t0 = time.time()
    deadline = t0 + MAX_RUNTIME_SEC
    symbols = live_symbols()
    if "EURUSD=X" not in symbols:
        symbols = ["EURUSD=X"] + list(symbols)

    import MetaTrader5 as mt5

    assert mt5.initialize(timeout=60_000), mt5.last_error()
    rates = mt5.copy_rates_from_pos("EURUSD", mt5.TIMEFRAME_H1, 0, TARGET_WARMUP + 50)
    mt5.shutdown()
    avail = 0 if rates is None else len(rates)
    use_count = min(TARGET_WARMUP + 5, avail)

    write_json(
        RUN / "WARMUP_CONFIG.json",
        {
            "target_warmup_bars": TARGET_WARMUP,
            "eurusd_rates_available_incl_forming": avail,
            "request_count": use_count,
            "symbol_source": "live_api_or_default",
            "n_symbols_requested": len(symbols),
            "ghost_sl_mult": 4.0,
            "peer_blend": 0.0,
            "thursday_gate": True,
            "force_flat_daily": True,
            "max_runtime_sec": MAX_RUNTIME_SEC,
            "note": "No fabricated bars. Common-history may timeout; partial variants still valid.",
        },
    )

    h1, offset, sym_status = load_h1(symbols, use_count)
    write_csv(RUN / "SYMBOL_STATUS.csv", sym_status)
    if "EURUSD=X" not in h1:
        write_json(RUN / "WARMUP_SENSITIVITY.json", {"completed": False, "error": "no EURUSD"})
        return 1

    master = "EURUSD=X"
    tip = _ts(h1[master][-1].timestamp)
    # Keep all symbols that reach the master tip (no aggressive length cull)
    h1 = {
        s: [c for c in bars if _ts(c.timestamp) <= tip]
        for s, bars in h1.items()
        if bars and _ts(bars[-1].timestamp) >= tip - pd.Timedelta(hours=48)
    }
    if master not in h1:
        write_json(RUN / "WARMUP_SENSITIVITY.json", {"completed": False, "error": "master dropped"})
        return 1

    master_n = len(h1[master])
    stop_i = master_n
    results: dict[str, Any] = {
        "completed": False,
        "offset_seconds": offset,
        "common_history_bars": master_n,
        "pairs": len(h1),
        "tip_timestamp": str(tip),
        "variants": [],
        "elapsed_sec": None,
    }
    write_json(
        RUN / "PROGRESS.json",
        {
            "phase": "config_ready",
            "master_n": master_n,
            "pairs": len(h1),
            "tip": str(tip),
            "utc": datetime.now(timezone.utc).isoformat(),
        },
    )

    try:
        short = replay_pending_only(
            "FROZEN_SHORT_~500",
            h1,
            master,
            start_i=max(120, stop_i - 500),
            stop_i=stop_i,
            deadline=deadline,
        )
        results["variants"].append(short)
        write_csv(RUN / "FROZEN_PENDING_SHORT.csv", short["pending"])
        write_json(RUN / "WARMUP_SENSITIVITY.json", {**results, "partial": True})

        live_like = replay_pending_only(
            "LIVE_LIKE_REPLAY_72",
            h1,
            master,
            start_i=max(120, stop_i - 72),
            stop_i=stop_i,
            deadline=deadline,
        )
        results["variants"].append(live_like)
        write_csv(RUN / "FROZEN_PENDING_LIVE_LIKE_72.csv", live_like["pending"])
        write_json(RUN / "WARMUP_SENSITIVITY.json", {**results, "partial": True})

        # Extended warmup (last 2000) as bounded proxy if full history too slow
        ext = replay_pending_only(
            "FROZEN_EXTENDED_~2000",
            h1,
            master,
            start_i=max(120, stop_i - 2000),
            stop_i=stop_i,
            deadline=deadline,
        )
        results["variants"].append(ext)
        write_csv(RUN / "FROZEN_PENDING_EXTENDED_2000.csv", ext["pending"])
        write_json(RUN / "WARMUP_SENSITIVITY.json", {**results, "partial": True})

        long = replay_pending_only(
            "FROZEN_COMMON_HISTORY",
            h1,
            master,
            start_i=120,
            stop_i=stop_i,
            deadline=deadline,
        )
        results["variants"].append(long)
        write_csv(RUN / "FROZEN_PENDING.csv", long["pending"])
        results["completed"] = True
        results["elapsed_sec"] = round(time.time() - t0, 1)
        write_json(RUN / "WARMUP_SENSITIVITY.json", results)
    except Exception as exc:
        results["completed"] = False
        results["error"] = repr(exc)
        results["traceback"] = traceback.format_exc()
        results["elapsed_sec"] = round(time.time() - t0, 1)
        write_json(RUN / "WARMUP_SENSITIVITY.json", results)
        print("FAILED", exc)
        # Still success code 0 if short+live_like present
        names = {v["name"] for v in results["variants"]}
        return 0 if {"FROZEN_SHORT_~500", "LIVE_LIKE_REPLAY_72"} <= names else 2

    print(
        "DONE",
        [(v["name"], v["pending_n"]) for v in results["variants"]],
        "completed=",
        results["completed"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
