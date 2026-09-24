"""Capture a NEW controlled golden window at the ShadowEngine input boundary.

Offline / paper by default — does NOT touch 8791 or live 88001.

Uses the same ShadowEngine path as Mirror. Writes a self-contained fixture:

  golden_fixture/
    manifest.json, config.json, config_hash.txt,
    initial_state.json/.pkl, h1_input/, state_trace/, signal_trace/,
    broker_events/, checksums/, hashes.json

Usage (PowerShell):
  $env:BOREX_GOLDEN_BARS='12'
  borex_live\\.venv311\\Scripts\\python.exe scripts\\run_controlled_golden_capture.py

Data source priority:
  1) Existing 8792 SURROGATE H1 cache (labeled SURROGATE in manifest) — for
     pipeline smoke / determinism rehearsal only
  2) Live MT5 bootstrap if --mt5 and terminal connected (still SURROGATE for
     historical identity; EXACT only for bars captured at engine boundary
     during THIS run)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")


def _load_surrogate() -> dict[str, list]:
    from borex.models.candle import Candle

    fix = ROOT / "data" / "runs" / "mirror_2d_8792" / "reconciliation" / "golden_fixture"
    man = json.loads((fix / "h1_surrogate_manifest.json").read_text(encoding="utf-8"))
    cache_dir = fix / "h1_surrogate_cache"
    out: dict[str, list] = {}
    for sym, meta in (man.get("per_pair") or {}).items():
        rows = json.loads((cache_dir / meta["file"]).read_text(encoding="utf-8"))
        out[sym] = [
            Candle(
                timestamp=r["timestamp"],
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r.get("volume") or 0),
            )
            for r in rows
        ]
    return out


def _make_shadow(capital: float = 1000.0):
    from borex.backtest.engine import BacktestConfig
    from borex_live.config import LiveServiceConfig
    from borex_live.engine.shadow_engine import ShadowEngine
    from borex_live.strategy_registry import create_strategy

    live_cfg = LiveServiceConfig(
        strategy="alexg8",
        capital=float(capital),
        leverage=5000.0,
        min_rr=2.0,
        rr_factor=1.0,
        rr_mode="dynamic",
        rr_min=2.0,
        rr_max=6.0,
        same_bar_exit=False,
        commission_per_lot=7.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_daily=True,
        force_flat_utc_hour=19,
        force_flat_friday=False,
        max_positions=60,
        position_size_pct=0.02,
    )
    strategy, _ = create_strategy("alexg8", min_rr=2.0, second_signal=False, execution_interval="1h")
    # Match known 8792 ghost geometry if attribute exists
    if hasattr(strategy, "ghost_sl_mult"):
        strategy.ghost_sl_mult = 0.6
    bt = BacktestConfig(
        initial_capital=float(capital),
        leverage=5000.0,
        position_size_pct=0.02,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=6.0,
        commission_per_lot=7.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_daily=True,
        force_flat_utc_hour=19,
        force_flat_friday=False,
    )
    return ShadowEngine(strategy, live_cfg, bt), live_cfg, bt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=int(os.environ.get("BOREX_GOLDEN_BARS", "12")))
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument(
        "--out",
        type=str,
        default="",
        help="Output golden_fixture directory",
    )
    ap.add_argument(
        "--window-start",
        type=str,
        default="2026-08-27 01:00:00+00:00",
        help="First master bar timestamp to capture (UTC)",
    )
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = Path(
        args.out
        or (
            ROOT
            / "data"
            / "runs"
            / "golden_windows"
            / f"controlled_{stamp}"
            / "golden_fixture"
        )
    )
    out.mkdir(parents=True, exist_ok=True)

    from borex_live.engine.golden_capture import GoldenCaptureSession, wrap_step_with_capture

    candles = _load_surrogate()
    # Prefer EURUSD as master if present
    master = "EURUSD=X" if "EURUSD=X" in candles else max(
        candles, key=lambda s: len(candles[s])
    )
    master_series = candles[master]

    import pandas as pd

    def _ts(x):
        t = pd.Timestamp(x)
        return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

    start_ts = _ts(args.window_start)
    start_i = None
    for i, c in enumerate(master_series):
        if _ts(c.timestamp) == start_ts:
            start_i = i
            break
    if start_i is None:
        # fall back: last bars
        start_i = max(0, len(master_series) - args.bars - 1)
        print(f"WARN: window-start not found; using index {start_i}")

    # Warm strategy state without capture up to start_i (inclusive cursor)
    shadow, live_cfg, bt = _make_shadow(args.capital)
    # Rebuild ghosts/patterns on history before window (same as epoch warm path)
    # Use truncated series ending at start_i so indices align
    warm_candles = {s: series[: start_i + 1] for s, series in candles.items()}
    shadow._rebuild_strategy_only(warm_candles, master)
    shadow.last_master_ts = str(warm_candles[master][-1].timestamp)
    shadow._last_indices = shadow._indices_at(
        warm_candles, warm_candles[master], len(warm_candles[master]) - 1
    )

    capture = GoldenCaptureSession(out, label=f"controlled_{stamp}")
    capture.capture_config(live_cfg, bt, shadow.config_hash)
    capture.capture_initial_state(shadow, warm_candles, master_symbol=master)

    # Extend series for stepping: need bars after start
    stop = min(len(master_series), start_i + 1 + args.bars)
    for mi in range(start_i + 1, stop):
        # Full history up to mi (engine expects full arrays)
        step_candles = {s: series[: mi + 1] for s, series in candles.items()}
        wrap_step_with_capture(shadow, capture, mi, step_candles, master)

    # Self-contained replay series: full arrays through last captured master bar
    last_mi = stop - 1
    full = {s: series[: last_mi + 1] for s, series in candles.items()}
    capture.capture_full_series_snapshot(full, master_symbol=master, label="full_series")

    man = capture.finalize(
        extra={
            "master_symbol": master,
            "bars_requested": args.bars,
            "window_start": args.window_start,
            "h1_source": "SURROGATE_MT5_HISTORY_cache",
            "h1_source_note": (
                "Historical cache used for offline rehearsal. Bars recorded under "
                "h1_input/ are still EXACT for THIS capture session (engine boundary). "
                "For live golden, set BOREX_GOLDEN_CAPTURE_DIR on Mirror and run live."
            ),
            "safety": "offline; no MT5 orders; no 8791 touch",
        }
    )
    print(json.dumps({"fixture": str(out), "manifest": str(man), "bars": capture._bar_seq}, indent=2))


if __name__ == "__main__":
    main()
