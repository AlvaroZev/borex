#!/usr/bin/env python3
"""Frozen N1 unseen-month validation — coverage-first, then optional backtest.

Selection rule (pre-performance):
  Among valid fully covered months excluding May/Jun/Jul/Aug 2026,
  choose the most recent. If none, INSUFFICIENT_DATA_COVERAGE (no backtest).
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG8Strategy
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.viewerMT5.mt5_feed import (
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
)

JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
JUN_REPORT = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/sl_4p0_lev_500.json"
JUL_REPORT = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/sl_4p0_lev_500.json"
AUG_REPORT = ROOT / "data/runs/mt5_1m_n1_tpfactor/sl_4p0_lev_500_rr_1p0.json"
JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"

# Same temporal convention as Jun→Jul N1 OOS (day-3 03:00 UTC → next month day-3).
EXCLUDED_MONTHS = {(2026, 5), (2026, 6), (2026, 7), (2026, 8)}
WARMUP_DAYS = 400
GHOST_PRIME_BARS = 72
CAPITAL = 1000.0

# June Raw spreads are the N1 cost model reference (realistic Raw).
# July index currently stores tighter live-probe spreads — do not use those for N1.
FROZEN_KNOBS = {
    "strategy": "alexg8",
    "ghost_sl_mult": 4.0,
    "leverage": 500.0,
    "rr_mode": "dynamic",
    "true_sl_rr": 2.0,
    "rr_factor": 1.0,
    "rr_min": 2.0,
    "rr_max": 5.0,
    "risk_include_commission": True,
    "commission_at_entry": False,
    "commission_per_lot": 7.0,
    "force_flat_friday": False,
    "force_flat_daily": True,
    "intra_hour_sl": False,
    "path": "h1_signal_1m_exit",
    "max_positions": 0,
    "signal_exec_split": True,
    "capital": CAPITAL,
}

# Soft thresholds for a "full" FX calendar month of M1 (weekends empty).
# ~20–23 trading days × ~1440 ≈ 28k–33k; require substantial coverage.
MIN_M1_BARS_PER_PAIR = 20_000
MIN_H1_BARS_PER_PAIR = 400
# Material gap: largest missing weekday stretch inside month (hours for H1 / minutes for M1)
MAX_M1_GAP_MINUTES = 180  # 3h contiguous weekday gap
MAX_H1_GAP_HOURS = 72


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _month_bounds(year: int, month: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year=year, month=month, day=1, tz="UTC")
    if month == 12:
        end = pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC")
    else:
        end = pd.Timestamp(year=year, month=month + 1, day=1, tz="UTC")
    return start, end


def _eval_window(year: int, month: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """N1 OOS convention: day-3 03:00 UTC → next month day-3 03:00."""
    start = pd.Timestamp(year=year, month=month, day=3, hour=3, tz="UTC")
    if month == 12:
        end = pd.Timestamp(year=year + 1, month=1, day=3, hour=3, tz="UTC")
    else:
        end = pd.Timestamp(year=year, month=month + 1, day=3, hour=3, tz="UTC")
    return start, end


def _file_hash(path: Path) -> str:
    st = path.stat()
    # Large M1 files: hash metadata identity, not full bytes.
    if st.st_size > 4_000_000:
        payload = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()
        return hashlib.sha256(payload).hexdigest() + ":meta"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_timestamps(symbol: str, interval: str) -> pd.DatetimeIndex | None:
    path = _cache_path(symbol, interval)
    if not path.is_file():
        return None
    df = pd.read_parquet(path, columns=["timestamp"])
    ts = pd.to_datetime(df["timestamp"], utc=True).sort_values()
    return pd.DatetimeIndex(ts)


def weekday_gaps(ts: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp, freq: str) -> dict:
    """Count gaps on weekdays inside [start, end)."""
    sl = ts[(ts >= start) & (ts < end)]
    if len(sl) < 2:
        return {"n_bars": int(len(sl)), "max_gap": None, "gap_count": None}
    delta = pd.Timedelta(freq)
    diffs = sl.to_series().diff().dropna()
    # Ignore weekend-sized jumps (Fri→Mon)
    material = diffs[diffs > delta * 1.5]
    # Filter out gaps that start on Friday evening / span weekend roughly
    max_gap = material.max() if len(material) else pd.Timedelta(0)
    return {
        "n_bars": int(len(sl)),
        "max_gap_seconds": float(max_gap.total_seconds()) if max_gap is not None else 0.0,
        "gap_events": int(len(material)),
    }


def assess_month(
    symbols: list[str],
    year: int,
    month: int,
    meta_ranges: dict[tuple[str, str], dict | None],
) -> dict[str, Any]:
    start, end = _month_bounds(year, month)
    excluded = (year, month) in EXCLUDED_MONTHS
    h1_ok = []
    m1_ok = []
    warmup_ok = []
    pair_detail = []

    for sym in symbols:
        h = meta_ranges.get((sym, "1h"))
        m = meta_ranges.get((sym, "1m"))
        h_cover = bool(
            h
            and h["start"] <= start + pd.Timedelta(days=3)
            and h["end"] >= end - pd.Timedelta(days=2)
        )
        m_cover = bool(
            m
            and m["start"] <= start + pd.Timedelta(days=3)
            and m["end"] >= end - pd.Timedelta(days=2)
        )
        w_ok = bool(h and h["start"] <= end - pd.Timedelta(days=350))
        if h_cover:
            h1_ok.append(sym)
        if m_cover:
            m1_ok.append(sym)
        if w_ok:
            warmup_ok.append(sym)
        pair_detail.append(
            {
                "symbol": sym,
                "h1_cover": h_cover,
                "m1_cover": m_cover,
                "h1_warmup": w_ok,
                "h1_range": [str(h["start"]), str(h["end"]), h["n"]] if h else None,
                "m1_range": [str(m["start"]), str(m["end"]), m["n"]] if m else None,
            }
        )

    if excluded:
        status = "EXCLUDE_ALREADY_USED"
    elif len(h1_ok) == 60 and len(m1_ok) == 60 and len(warmup_ok) == 60:
        status = "RANGE_PASS"  # still need bar-count / gap pass
    else:
        status = "INSUFFICIENT"

    return {
        "year": year,
        "month": month,
        "label": f"{year}-{month:02d}",
        "calendar_start": str(start),
        "calendar_end": str(end),
        "excluded_prior_use": excluded,
        "h1_pairs": len(h1_ok),
        "m1_pairs": len(m1_ok),
        "h1_warmup_pairs": len(warmup_ok),
        "status": status,
        "pair_detail_summary": {
            "h1_missing": [p["symbol"] for p in pair_detail if not p["h1_cover"]][:10],
            "m1_missing": [p["symbol"] for p in pair_detail if not p["m1_cover"]][:10],
            "warmup_missing": [p["symbol"] for p in pair_detail if not p["h1_warmup"]][:10],
        },
    }


def deepen_coverage(symbols: list[str], year: int, month: int) -> dict[str, Any]:
    """Bar counts + gap stats for a RANGE_PASS month (still pre-PnL)."""
    start, end = _month_bounds(year, month)
    h1_counts = []
    m1_counts = []
    h1_gaps = []
    m1_gaps = []
    failures = []
    for sym in symbols:
        h_ts = load_timestamps(sym, "1h")
        m_ts = load_timestamps(sym, "1m")
        if h_ts is None or m_ts is None:
            failures.append({"symbol": sym, "reason": "missing_parquet"})
            continue
        hg = weekday_gaps(h_ts, start, end, "1h")
        mg = weekday_gaps(m_ts, start, end, "1min")
        h1_counts.append(hg["n_bars"])
        m1_counts.append(mg["n_bars"])
        h1_gaps.append(hg)
        m1_gaps.append(mg)
        if hg["n_bars"] < MIN_H1_BARS_PER_PAIR:
            failures.append({"symbol": sym, "reason": "h1_bar_count", **hg})
        if mg["n_bars"] < MIN_M1_BARS_PER_PAIR:
            failures.append({"symbol": sym, "reason": "m1_bar_count", **mg})
        if (mg.get("max_gap_seconds") or 0) > MAX_M1_GAP_MINUTES * 60:
            # Allow weekend; material weekday gaps still flagged via gap_events heuristic
            if (mg.get("gap_events") or 0) > 5:
                failures.append({"symbol": sym, "reason": "m1_gaps", **mg})
        if (hg.get("max_gap_seconds") or 0) > MAX_H1_GAP_HOURS * 3600 and (
            hg.get("gap_events") or 0
        ) > 3:
            failures.append({"symbol": sym, "reason": "h1_gaps", **hg})

    ok = len(failures) == 0 and len(h1_counts) == 60 and len(m1_counts) == 60
    return {
        "ok": ok,
        "h1_bar_counts": {
            "min": min(h1_counts) if h1_counts else None,
            "median": int(statistics.median(h1_counts)) if h1_counts else None,
            "max": max(h1_counts) if h1_counts else None,
        },
        "m1_bar_counts": {
            "min": min(m1_counts) if m1_counts else None,
            "median": int(statistics.median(m1_counts)) if m1_counts else None,
            "max": max(m1_counts) if m1_counts else None,
        },
        "failures_n": len(failures),
        "failures_sample": failures[:15],
        "thresholds": {
            "min_m1_bars": MIN_M1_BARS_PER_PAIR,
            "min_h1_bars": MIN_H1_BARS_PER_PAIR,
            "max_m1_gap_minutes": MAX_M1_GAP_MINUTES,
            "max_h1_gap_hours": MAX_H1_GAP_HOURS,
        },
    }


def build_range_meta(symbols: list[str]) -> tuple[dict, list[dict]]:
    meta: dict[tuple[str, str], dict | None] = {}
    hashes = []
    for sym in symbols:
        for tf in ("1h", "1m"):
            path = _cache_path(sym, tf)
            if not path.is_file():
                meta[(sym, tf)] = None
                continue
            df = pd.read_parquet(path, columns=["timestamp"])
            ts = pd.to_datetime(df["timestamp"], utc=True)
            info = {
                "start": ts.min(),
                "end": ts.max(),
                "n": int(len(ts)),
                "path": str(path),
                "hash": _file_hash(path),
            }
            meta[(sym, tf)] = info
            hashes.append(
                {
                    "symbol": sym,
                    "interval": tf,
                    "n": info["n"],
                    "start": str(info["start"]),
                    "end": str(info["end"]),
                    "hash": info["hash"],
                }
            )
    return meta, hashes


def select_month(candidates: list[dict]) -> dict[str, Any]:
    """Deterministic: most recent VALID among RANGE_PASS that pass deepen."""
    # Only months already marked for deepen are VALID
    valid = [c for c in candidates if c.get("final_status") == "VALID_CANDIDATE"]
    valid.sort(key=lambda c: (c["year"], c["month"]), reverse=True)
    return {
        "selection_rule": (
            "Among valid fully covered months excluding May/Jun/Jul/Aug 2026, "
            "prefer the most recent; if none, INSUFFICIENT_DATA_COVERAGE."
        ),
        "valid_candidates": [
            {"label": c["label"], "year": c["year"], "month": c["month"]} for c in valid
        ],
        "selected": valid[0] if valid else None,
        "performance_inspected_before_selection": False,
    }


# --- optional backtest path (only if a month is selected) ---


def load_cache_candles(symbol: str, interval: str, start: datetime, end: datetime):
    path = _cache_path(symbol, interval)
    if not path.is_file():
        return []
    df = pd.read_parquet(path)
    candles = _frame_to_candles(df)
    hourly = interval.strip().lower() in {"1h", "h1"}
    if hourly:
        candles = _dedupe_hourly(candles)
    candles = _slice_candles(candles, start, end)
    return _drop_forming_bar(candles, interval)


def _cfg(spreads: dict[str, float]) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=float(FROZEN_KNOBS["leverage"]),
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(FROZEN_KNOBS["true_sl_rr"]),
        rr_mode=str(FROZEN_KNOBS["rr_mode"]),
        rr_factor=float(FROZEN_KNOBS["rr_factor"]),
        rr_min=float(FROZEN_KNOBS["rr_min"]),
        rr_max=float(FROZEN_KNOBS["rr_max"]),
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=bool(FROZEN_KNOBS["risk_include_commission"]),
        commission_at_entry=bool(FROZEN_KNOBS["commission_at_entry"]),
        force_flat_friday=bool(FROZEN_KNOBS["force_flat_friday"]),
        force_flat_daily=bool(FROZEN_KNOBS["force_flat_daily"]),
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=bool(FROZEN_KNOBS["intra_hour_sl"]),
    )


def _trade_row(t) -> dict:
    return {
        "symbol": t.symbol,
        "side": t.side.value if hasattr(t.side, "value") else str(t.side),
        "entry_time": str(t.entry_time),
        "exit_time": str(t.exit_time),
        "entry": float(t.entry_price),
        "exit": float(t.exit_price or 0),
        "sl": float(t.stop_loss or 0),
        "tp": float(t.take_profit or 0),
        "pnl": float(t.pnl),
        "exit_reason": t.exit_reason,
        "pattern": str(t.pattern or ""),
    }


def _pf(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    gp = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    if gl == 0:
        return float("inf") if gp > 0 else None
    return float(gp / abs(gl))


def summarize(trades: list[dict], intents: int, mdd: float, entry_start, last_bar) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    days = max(1.0, (_ts(last_bar) - _ts(entry_start)).total_seconds() / 86400.0)
    sides = Counter(str(t["side"]).lower() for t in trades)
    exits = Counter(str(t["exit_reason"]) for t in trades)
    return {
        "intents": intents,
        "fills": len(trades),
        "trades_per_day": len(trades) / days,
        "WR": (wins / len(trades)) if trades else None,
        "PF": _pf(pnls),
        "net_pnl": float(sum(pnls)) if pnls else 0.0,
        "mean_trade_pnl": float(statistics.mean(pnls)) if pnls else None,
        "median_trade_pnl": float(statistics.median(pnls)) if pnls else None,
        "MDD": float(mdd),
        "ending_equity": CAPITAL + (sum(pnls) if pnls else 0.0),
        "long_trades": int(sides.get("long", 0) + sides.get("buy", 0)),
        "short_trades": int(sides.get("short", 0) + sides.get("sell", 0)),
        "exit_reasons": dict(exits),
        "TP": int(exits.get("take_profit", 0)),
        "daily_close": int(exits.get("daily_close", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
        "other": {
            k: v
            for k, v in exits.items()
            if k not in {"take_profit", "daily_close", "margin_stop"}
        },
    }


def prior_window(path: Path, label: str) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    s = d.get("summary") or {}
    trades = d.get("trades") or []
    pnls = [float(t["pnl"]) for t in trades]
    exits = Counter(str(t.get("exit_reason")) for t in trades)
    entry = _ts(d.get("entry_start"))
    last = _ts(d.get("last_bar"))
    days = max(1.0, (last - entry).total_seconds() / 86400.0)
    return {
        "label": label,
        "intents": d.get("intents"),
        "fills": s.get("trades"),
        "trades_per_day": (s.get("trades") or 0) / days,
        "WR": s.get("win_rate"),
        "PF": s.get("profit_factor"),
        "net_pnl": s.get("net_pnl"),
        "mean_trade_pnl": float(statistics.mean(pnls)) if pnls else None,
        "MDD": s.get("max_drawdown_pct"),
        "ending_equity": s.get("account"),
        "TP": int(exits.get("take_profit", 0)),
        "daily_close": int(exits.get("daily_close", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
    }


def run_frozen_n1(
    *,
    symbols: list[str],
    spreads: dict[str, float],
    year: int,
    month: int,
) -> dict[str, Any]:
    entry_start, end_ts = _eval_window(year, month)
    end = end_ts.to_pydatetime()
    h1_start = (end_ts - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()

    print(f"Loading cache H1 {h1_start.date()} -> {end.date()}", flush=True)
    data: dict = {}
    m1: dict = {}
    for i, sym in enumerate(symbols, 1):
        bars = load_cache_candles(sym, "1h", h1_start, end)
        if len(bars) < 120:
            raise SystemExit(f"H1 insufficient {sym}: {len(bars)}")
        data[sym] = bars
        minutes = load_cache_candles(sym, "1m", m1_start, end)
        if not minutes:
            raise SystemExit(f"M1 missing {sym}")
        m1[sym] = minutes
        if i % 10 == 0 or i == len(symbols):
            print(f"  {i}/{len(symbols)} H1={len(data)} 1m={len(m1)}", flush=True)

    master = pick_master_symbol(data)
    master_bars = data[master]
    entry_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= entry_start),
        None,
    )
    if entry_i is None:
        raise SystemExit("no master bar at entry_start")
    warmup = int(AlexG8Strategy().min_bars)
    prime_i = max(warmup, entry_i - GHOST_PRIME_BARS)

    strategy = AlexG8Strategy(
        min_rr=float(FROZEN_KNOBS["true_sl_rr"]),
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(FROZEN_KNOBS["ghost_sl_mult"]),
    )
    engine = MultiMarketEngine(strategy, _cfg(spreads), max_positions=0)
    engine._progress_every = 250
    tape: list[dict] = []
    t0 = time.perf_counter()
    result = engine.run(
        data,
        timeframe="1h+1m",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=prime_i,
        ltf_by_symbol=m1,
        collect_decisions=tape,
    )
    elapsed = time.perf_counter() - t0
    window = [_trade_row(t) for t in result.trades if _ts(t.entry_time) >= entry_start]
    last_bar = master_bars[-1].timestamp
    summary = summarize(
        window, len(tape), float(result.max_drawdown_pct), entry_start, last_bar
    )
    summary["elapsed_sec"] = elapsed
    summary["entry_start"] = str(entry_start)
    summary["last_bar"] = str(last_bar)
    summary["master"] = master
    summary["h1_pairs"] = len(data)
    summary["m1_pairs"] = len(m1)
    return {"summary": summary, "trades": window, "intents_tape_n": len(tape)}


def classify(summary: dict | None, coverage_fail: bool) -> str:
    if coverage_fail:
        return "INSUFFICIENT_DATA_COVERAGE"
    assert summary is not None
    pf = summary.get("PF")
    net = summary.get("net_pnl")
    mean = summary.get("mean_trade_pnl")
    if pf is None or net is None or mean is None:
        return "MIXED_OOS_OBSERVATION"
    pos = float(pf) > 1.0 and float(net) > 0 and float(mean) > 0
    neg = float(pf) < 1.0 and float(net) < 0 and float(mean) < 0
    if pos:
        return "ADDITIONAL_POSITIVE_OOS_OBSERVATION"
    if neg:
        return "ADDITIONAL_NEGATIVE_OOS_OBSERVATION"
    return "MIXED_OOS_OBSERVATION"


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    jul_idx = json.loads(JUL_INDEX.read_text(encoding="utf-8"))
    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    # Universe from July index (pinned 60); Raw spreads from June N1 OOS.
    symbols = sorted(jul_idx["spread_pips_by_symbol"].keys())
    assert len(symbols) == 60
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    assert set(spreads) == set(symbols)

    print(f"Universe n={len(symbols)} provenance=mt5/utc_v2 cache", flush=True)
    meta, hashes = build_range_meta(symbols)

    # Coverage table BEFORE any backtest / PnL
    candidates = []
    for year in (2025, 2026):
        for month in range(1, 13):
            row = assess_month(symbols, year, month, meta)
            candidates.append(row)

    print("\n=== CANDIDATE COVERAGE (pre-selection, pre-PnL) ===", flush=True)
    for c in candidates:
        if c["year"] == 2026 or c["m1_pairs"] > 0 or c["status"] != "INSUFFICIENT":
            print(
                f"  {c['label']}: {c['status']} "
                f"H1={c['h1_pairs']}/60 M1={c['m1_pairs']}/60 "
                f"warmup={c['h1_warmup_pairs']}/60",
                flush=True,
            )

    # Deepen RANGE_PASS months only
    for c in candidates:
        if c["status"] != "RANGE_PASS":
            c["final_status"] = c["status"]
            continue
        print(f"Deepening coverage {c['label']} ...", flush=True)
        deep = deepen_coverage(symbols, c["year"], c["month"])
        c["deep"] = deep
        c["final_status"] = "VALID_CANDIDATE" if deep["ok"] else "INSUFFICIENT_GAPS_OR_BARS"

    selection = select_month(candidates)
    selected = selection["selected"]
    print(f"\nSelection: {selected}", flush=True)

    yyyymm = "none" if not selected else f"{selected['year']}{selected['month']:02d}"
    out = ROOT / "data" / "runs" / "research" / f"n1_unseen_month_{yyyymm}_{ts}"
    out.mkdir(parents=True, exist_ok=True)

    coverage_payload = {
        "provenance": {
            "source": "MT5 historical via borex data/cache/mt5/utc_v2 parquet",
            "vendor": "MT5",
            "timezone": "UTC",
            "universe_source": str(JUL_INDEX),
            "spreads_source": str(JUN_INDEX),
            "note": "No HistData; no vendor mix; no universe shrink.",
        },
        "excluded_months": [
            {"label": "2026-05", "reason": "already_attempted_unavailable_or_used"},
            {"label": "2026-06", "reason": "already_evaluated_OOS"},
            {"label": "2026-07", "reason": "already_evaluated_OOS"},
            {"label": "2026-08", "reason": "already_evaluated_IS"},
        ],
        "selection_rule": selection["selection_rule"],
        "performance_inspected_before_selection": False,
        "candidates": [
            {
                k: v
                for k, v in c.items()
                if k != "pair_detail"  # keep payload lean
            }
            for c in candidates
        ],
        "selection": selection,
        "file_hashes": hashes,
        "global_m1_intersection": {
            "earliest_max_start": str(
                max(
                    meta[(s, "1m")]["start"]
                    for s in symbols
                    if meta.get((s, "1m"))
                )
            ),
            "latest_min_end": str(
                min(meta[(s, "1m")]["end"] for s in symbols if meta.get((s, "1m")))
            ),
        },
    }

    results = None
    classification = "INSUFFICIENT_DATA_COVERAGE"
    comparison = None

    if selected:
        # Run frozen N1 ONLY after selection
        print(
            f"Running frozen N1 on {selected['label']} (no tuning)...",
            flush=True,
        )
        run = run_frozen_n1(
            symbols=symbols,
            spreads=spreads,
            year=selected["year"],
            month=selected["month"],
        )
        results = run["summary"]
        results["trades_n"] = len(run["trades"])
        (out / "trades.json").write_text(
            json.dumps(run["trades"], indent=2), encoding="utf-8"
        )
        classification = classify(results, coverage_fail=False)
        comparison = {
            "new_month": {"label": selected["label"], **results},
            "june_oos": prior_window(JUN_REPORT, "June OOS"),
            "july_oos": prior_window(JUL_REPORT, "July OOS"),
            "august_is": prior_window(AUG_REPORT, "August IS"),
        }
    else:
        print("No valid candidate — skipping backtest.", flush=True)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "experiment": "n1_unseen_month_validation",
        "classification": classification,
        "selected_month": selected,
        "offline": True,
        "live_trading": False,
        "out_dir": str(out),
    }
    config_snapshot = {
        "knobs": FROZEN_KNOBS,
        "spreads": spreads,
        "universe": symbols,
        "simulator": "MultiMarketEngine FROZEN_FOR_STRATEGY_RESEARCH",
        "path": "h1_signal_1m_exit",
        "long_only_ablation": "NOT_INCORPORATED",
    }

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "data_coverage.json").write_text(
        json.dumps(coverage_payload, indent=2, default=str), encoding="utf-8"
    )
    (out / "config_snapshot.json").write_text(
        json.dumps(config_snapshot, indent=2), encoding="utf-8"
    )
    (out / "results.json").write_text(
        json.dumps(
            {
                "classification": classification,
                "selected_month": selected,
                "backtest_run": results is not None,
                "summary": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "comparison.json").write_text(
        json.dumps(comparison, indent=2) if comparison else json.dumps(
            {"note": "No backtest — insufficient coverage"}, indent=2
        ),
        encoding="utf-8",
    )

    # Report
    lines = [
        "# N1 Unseen-Month Validation",
        "",
        f"Generated: {manifest['created_at']}",
        f"Commit: `{manifest['git_commit']}`",
        f"Classification: **{classification}**",
        "",
        "## Selection rule (pre-PnL)",
        "",
        selection["selection_rule"],
        "",
        f"Performance inspected before selection: `{selection['performance_inspected_before_selection']}`",
        "",
        "## Candidate coverage",
        "",
        "| Month | Status | H1 | M1 | Warmup |",
        "|-------|--------|----|----|--------|",
    ]
    for c in candidates:
        if c["year"] < 2025:
            continue
        if c["year"] == 2025 and c["m1_pairs"] == 0 and c["status"] == "INSUFFICIENT":
            continue
        lines.append(
            f"| {c['label']} | {c.get('final_status', c['status'])} | "
            f"{c['h1_pairs']}/60 | {c['m1_pairs']}/60 | {c['h1_warmup_pairs']}/60 |"
        )
    lines += [
        "",
        f"**Selected:** `{selected}`",
        "",
        "## Provenance",
        "",
        "- Source: MT5 `data/cache/mt5/utc_v2` (same N1 closest-sim provenance)",
        "- No HistData / no vendor mix / no universe shrink",
        f"- M1 intersection: {coverage_payload['global_m1_intersection']}",
        "",
    ]
    if results is None:
        lines += [
            "## Result",
            "",
            "Backtest **not run** — `INSUFFICIENT_DATA_COVERAGE`.",
            "",
            "No fully covered unseen calendar month exists under the pinned 60-pair "
            "MT5 M1 cache outside May/Jun/Jul/Aug 2026.",
            "",
            "## What this establishes",
            "",
            "Under current same-provenance MT5 cache depth, another clean frozen-N1 "
            "unseen-month OOS observation cannot be produced without new M1 history.",
            "",
            "## What this does not establish",
            "",
            "Nothing about N1 edge, robustness, or July diagnosis. No parameter "
            "change is implied.",
            "",
            "## Next research decision",
            "",
            "Extend MT5 M1 cache for a pre-June or post-August month at 60/60 "
            "coverage, then re-run this exact frozen-N1 protocol. Do not tune "
            "from this coverage failure.",
        ]
    else:
        lines += [
            "## Result",
            "",
            "```",
            json.dumps(results, indent=2),
            "```",
            "",
            f"Classification: {classification}",
        ]

    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nCLASSIFICATION={classification}", flush=True)
    print(f"Artifacts -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
