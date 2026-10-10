#!/usr/bin/env python3
"""Locked September 2026 OOS — pre-registered n1_control vs n1_thursday.

Protocol:
  1) Audit Sep 1–30 H1/M1 coverage (60 pairs, utc_v2)
  2) Freeze manifest + hashes BEFORE evaluation
  3) Replay BOTH arms on the same September data
  4) Persist trade-level results, weekly/pair/equity forensics
  5) Classify SUPPORTED / NOT SUPPORTED / INCONCLUSIVE

Does NOT optimize against September. Does NOT evaluate post-hoc candidates.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
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
from borex.gate2.thursday_entry_gate import (
    THURSDAY,
    filter_block_thursday_entries,
    is_thursday_entry,
    thursday_entry_stats,
)
from borex.viewerMT5.mt5_feed import (
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
)

CAPITAL = 1000.0
LEV = 500.0
WARMUP_DAYS = 400
GHOST_PRIME_BARS = 72

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
AUG_INDEX = ROOT / "data/runs/mt5_1m_slmult_lev_split/index.json"
JUN_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/decisions_sl_4p0.json"
JUL_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/decisions_sl_4p0.json"
AUG_TAPE = ROOT / "data/runs/mt5_1m_slmult_lev_split/decisions_sl_4p0.json"
THU_PRIOR = ROOT / "data/runs/research/gate2_thursday_gate_20260925_195229"

# Day-3 convention consistent with locked Jun/Jul/Aug N1 packs.
SEP_ENTRY_START = pd.Timestamp("2026-09-03T03:00:00+00:00")
SEP_END = pd.Timestamp("2026-10-01T00:00:00+00:00")
SEP_DATA_START = pd.Timestamp("2026-09-01T00:00:00+00:00")

CONTROL = dict(
    strategy="alexg8",
    ghost_sl_mult=4.0,
    peer_blend=0.0,
    leverage=LEV,
    rr_mode="dynamic",
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=5.0,
    true_sl=True,
    true_sl_rr=2.0,
    size_mode="margin",
    position_size_pct=0.01,
    commission_per_lot=7.0,
    risk_include_commission=True,
    force_flat_daily=True,
    force_flat_friday=False,
    sides="both",
    thursday_gate=False,
    path="h1_signal_1m_exit",
    initial_capital=CAPITAL,
)

TREATMENT = dict(CONTROL, thursday_gate=True)

HYPOTHESIS = (
    "Excluding new entries whose entry day is Thursday would remove the "
    "disproportionate low-quality Thursday cluster while preserving/improving "
    "broader expectancy."
)


def _git() -> str:
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


def fingerprint(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys and not isinstance(r[k], (dict, list)):
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def load_cache_candles(symbol: str, interval: str, start: datetime, end: datetime):
    path = _cache_path(symbol, interval)
    if not path.is_file():
        return []
    df = pd.read_parquet(path)
    candles = _frame_to_candles(df)
    if interval.strip().lower() in {"1h", "h1"}:
        candles = _dedupe_hourly(candles)
    return _drop_forming_bar(_slice_candles(candles, start, end), interval)


def bt_cfg(spreads: dict[str, float]) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=5.0,
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_friday=False,
        force_flat_daily=True,
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=False,
    )


def audit_symbol(symbol: str, interval: str) -> dict[str, Any]:
    path = _cache_path(symbol, interval)
    row: dict[str, Any] = {
        "symbol": symbol,
        "interval": interval,
        "path": str(path.as_posix()),
        "status": "MISSING_FILE",
        "n": 0,
        "tmin": None,
        "tmax": None,
        "has_sep30": False,
        "duplicates": 0,
        "gaps": 0,
        "ohlc_invalid": 0,
        "monotonic": True,
    }
    if not path.is_file():
        return row
    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True)
    elif isinstance(df.index, pd.DatetimeIndex):
        ts = pd.to_datetime(df.index, utc=True)
    else:
        row["status"] = "NO_TIMESTAMP"
        return row
    mask = (ts >= SEP_DATA_START) & (ts < SEP_END)
    sub_ts = ts[mask].reset_index(drop=True)
    sub = df.loc[mask].reset_index(drop=True) if hasattr(df, "loc") else None
    n = int(len(sub_ts))
    row["n"] = n
    if n == 0:
        row["status"] = "NO_SEP_BARS"
        return row
    row["tmin"] = str(sub_ts.min())
    row["tmax"] = str(sub_ts.max())
    row["has_sep30"] = bool(
        ((sub_ts.dt.year == 2026) & (sub_ts.dt.month == 9) & (sub_ts.dt.day == 30)).any()
    )
    row["duplicates"] = int(sub_ts.duplicated().sum())
    row["monotonic"] = bool(sub_ts.is_monotonic_increasing)
    if n >= 2:
        delta = pd.Timedelta("1h" if interval in {"1h", "h1"} else "1min")
        row["gaps"] = int((sub_ts.diff().dropna() > delta * 1.5).sum())
    if sub is not None:
        cols = {c.lower(): c for c in sub.columns}
        need = ["open", "high", "low", "close"]
        if all(k in cols for k in need):
            o, h, l, c = (sub[cols[k]].astype(float) for k in need)
            bad = ~((h >= o) & (h >= c) & (h >= l) & (l <= o) & (l <= c) & (o > 0) & (c > 0))
            row["ohlc_invalid"] = int(bad.sum())
    row["status"] = (
        "COMPLETE_THROUGH_DAY30" if row["has_sep30"] else "INCOMPLETE_TODATE"
    )
    return row


def dataset_hash(symbols: list[str], interval: str) -> str:
    """Hash Sep-window timestamps+OHLC per symbol (stable, content-based)."""
    h = hashlib.sha256()
    for sym in symbols:
        path = _cache_path(sym, interval)
        h.update(sym.encode())
        h.update(b"\0")
        if not path.is_file():
            h.update(b"MISSING")
            continue
        df = pd.read_parquet(path)
        ts = pd.to_datetime(
            df["timestamp"] if "timestamp" in df.columns else df.index, utc=True
        )
        mask = (ts >= SEP_DATA_START) & (ts < SEP_END)
        sub = df.loc[mask]
        # Hash file bytes of filtered rows via parquet roundtrip of key cols
        cols = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in sub.columns]
        payload = sub[cols].to_csv(index=False).encode()
        h.update(payload)
    return h.hexdigest()


def remap_tape(tape: list[dict], data: dict) -> list[dict]:
    maps = {
        sym: {int(_ts(c.timestamp).timestamp()): i for i, c in enumerate(bars)}
        for sym, bars in data.items()
    }
    out = []
    for row in tape:
        sym = str(row.get("symbol") or "")
        idx = maps.get(sym, {}).get(int(float(row["time_unix"])))
        if idx is None:
            continue
        r = dict(row)
        r["index"] = int(idx)
        out.append(r)
    return out


def load_september_period(symbols: list[str], spreads: dict[str, float]) -> dict:
    entry_start = SEP_ENTRY_START
    end = SEP_END
    h1_start = (end - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    end_py = end.to_pydatetime()
    print("[september] loading cache …", flush=True)
    data, m1 = {}, {}
    for i, sym in enumerate(symbols, 1):
        bars = load_cache_candles(sym, "1h", h1_start, end_py)
        if len(bars) < 120:
            continue
        data[sym] = bars
        minutes = load_cache_candles(sym, "1m", m1_start, end_py)
        if minutes:
            m1[sym] = minutes
        if i % 20 == 0 or i == len(symbols):
            print(f"  {i}/{len(symbols)} H1={len(data)} M1={len(m1)}", flush=True)
    if len(data) < 60 or len(m1) < 60:
        raise SystemExit(f"Insufficient pairs H1={len(data)} M1={len(m1)}")
    master = pick_master_symbol(data)
    entry_i = next(i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= entry_start)
    prime_i = max(int(AlexG8Strategy().min_bars), entry_i - GHOST_PRIME_BARS)
    return {
        "name": "september",
        "entry_start": entry_start,
        "end": end,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": prime_i,
        "h1_pairs": len(data),
        "m1_pairs": len(m1),
    }


def scan_decisions(period: dict) -> list[dict]:
    print("[september] scanning alexg8 ghost_sl_mult=4 decisions …", flush=True)
    strategy = AlexG8Strategy(
        min_rr=2.0, execution_interval="1h", peer_blend=0.0, ghost_sl_mult=4.0
    )
    engine = MultiMarketEngine(strategy, bt_cfg(period["spreads"]), max_positions=0)
    engine._progress_every = 500
    collected: list[dict] = []
    t0 = time.perf_counter()
    engine.run(
        period["data"],
        timeframe="1h+1m",
        master_symbol=period["master"],
        same_bar_exit=False,
        start_master_i=period["prime_i"],
        ltf_by_symbol=period["m1"],
        collect_decisions=collected,
    )
    # Keep only intents at/after entry_start (eval window)
    tape = [
        r
        for r in collected
        if _ts(pd.Timestamp(float(r["time_unix"]), unit="s", tz="UTC"))
        >= period["entry_start"]
    ]
    print(
        f"[september] scanned raw={len(collected)} eval_intents={len(tape)} "
        f"in {(time.perf_counter()-t0)/60:.1f}m",
        flush=True,
    )
    return tape


def trade_to_row(t, trade_id: int, *, signal_row: dict | None = None) -> dict:
    side = t.side.value if hasattr(t.side, "value") else str(t.side)
    et = _ts(t.entry_time)
    xt = _ts(t.exit_time) if t.exit_time is not None else None
    risk = (
        abs(float(t.entry_price) - float(t.stop_loss))
        if t.stop_loss is not None
        else None
    )
    reward = (
        abs(float(t.take_profit) - float(t.entry_price))
        if t.take_profit is not None
        else None
    )
    rr = (reward / risk) if risk and risk > 0 and reward is not None else None
    notional = (
        float(t.margin) * LEV if t.margin else None
    )
    iso = et.isocalendar()
    return {
        "trade_id": trade_id,
        "pair": t.symbol,
        "direction": side,
        "signal_timestamp": (
            str(pd.Timestamp(float(signal_row["time_unix"]), unit="s", tz="UTC"))
            if signal_row and signal_row.get("time_unix") is not None
            else str(t.entry_time)
        ),
        "entry_timestamp": str(t.entry_time),
        "exit_timestamp": str(t.exit_time) if t.exit_time is not None else None,
        "signal_price": signal_row.get("entry") if signal_row else None,
        "entry_price": float(t.entry_price),
        "exit_price": float(t.exit_price) if t.exit_price is not None else None,
        "ghost_entry": signal_row.get("ghost_entry") if signal_row else None,
        "ghost_sl": signal_row.get("ghost_sl") if signal_row else (
            signal_row.get("stop_loss") if signal_row else None
        ),
        "real_entry": float(t.entry_price),
        "real_sl": float(t.stop_loss) if t.stop_loss is not None else None,
        "real_tp": float(t.take_profit) if t.take_profit is not None else None,
        "rr": rr,
        "exit_reason": t.exit_reason,
        "gross_pnl": float(t.pnl) + float(t.commission or 0.0),
        "commission": float(t.commission or 0.0),
        "swap": None,  # NOT PRODUCED by frozen simulator
        "net_pnl": float(t.pnl),
        "week": f"{iso.year}-W{iso.week:02d}",
        "ISO_week": int(iso.week),
        "date": str(et.date()),
        "weekday": et.day_name(),
        "session": getattr(t, "entry_session", "") or "",
        "position_size": float(t.margin) if t.margin else None,
        "notional": notional,
        "drawdown_at_entry": None,  # filled after equity curve
        "drawdown_at_exit": None,
        "pattern": getattr(t, "pattern", "") or "",
        "margin": float(t.margin) if t.margin else None,
        "entry_equity": float(t.entry_equity) if getattr(t, "entry_equity", None) else None,
    }


def run_replay(period: dict, tape: list[dict]) -> tuple[list[dict], list[dict], Any]:
    strategy = AlexG8Strategy(
        min_rr=2.0, execution_interval="1h", peer_blend=0.0, ghost_sl_mult=4.0
    )
    engine = MultiMarketEngine(strategy, bt_cfg(period["spreads"]), max_positions=0)
    engine._progress_every = 50_000
    t0 = time.perf_counter()
    result = engine.run(
        period["data"],
        timeframe="1h+1m",
        master_symbol=period["master"],
        same_bar_exit=False,
        start_master_i=period["prime_i"],
        ltf_by_symbol=period["m1"],
        decisions=tape,
    )
    # Index signals by (symbol, time_unix) for join
    sig_map = {}
    for r in tape:
        key = (str(r.get("symbol")), int(float(r["time_unix"])))
        sig_map[key] = r

    trades = []
    tid = 0
    for t in result.trades:
        if _ts(t.entry_time) < period["entry_start"]:
            continue
        if _ts(t.entry_time) >= period["end"]:
            continue
        tid += 1
        key = (str(t.symbol), int(_ts(t.entry_time).timestamp()))
        # ghost fills may shift bar; try floor-hour match
        sig = sig_map.get(key)
        if sig is None:
            key2 = (str(t.symbol), int(_ts(t.entry_time).floor("h").timestamp()))
            sig = sig_map.get(key2)
        trades.append(trade_to_row(t, tid, signal_row=sig))
    print(
        f"  replay fills={len(trades)} intents={len(tape)} "
        f"{(time.perf_counter()-t0)/60:.1f}m equity={result.final_equity:.2f}",
        flush=True,
    )
    return trades, tape, result


def summarize_trades(trades: list[dict], *, starting_equity: float = CAPITAL) -> dict:
    pnls = [float(t["net_pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp = sum(wins)
    gl = sum(losses)
    pf = (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None)
    exits = Counter(str(t["exit_reason"]) for t in trades)
    max_cl = cur = 0
    for p in pnls:
        if p <= 0:
            cur += 1
            max_cl = max(max_cl, cur)
        else:
            cur = 0
    # true sequential equity / MDD
    eq = starting_equity
    peak = eq
    max_dd = 0.0
    trough = eq
    peak_eq = eq
    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            trough = eq
            peak_eq = peak
    ending = starting_equity + sum(pnls)
    return {
        "fills": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "gross_profit": float(gp),
        "gross_loss": float(gl),
        "profit_factor": pf,
        "net_pnl": float(sum(pnls)) if pnls else 0.0,
        "mean_trade": float(statistics.mean(pnls)) if pnls else None,
        "median_trade": float(statistics.median(pnls)) if pnls else None,
        "std_trade": float(statistics.pstdev(pnls)) if len(pnls) > 1 else (0.0 if pnls else None),
        "max_drawdown": float(max_dd),
        "max_drawdown_pct": float(max_dd * 100.0),
        "max_consecutive_losses": max_cl,
        "TP_count": int(exits.get("take_profit", 0)),
        "SL_count": int(exits.get("stop_loss", 0) + exits.get("sl", 0)),
        "margin_stop_count": int(exits.get("margin_stop", 0)),
        "daily_flat_count": int(exits.get("daily_close", 0)),
        "friday_flat_count": int(exits.get("friday_close", 0) + exits.get("force_flat_friday", 0)),
        "starting_equity": starting_equity,
        "ending_equity": float(ending),
        "return_pct": float((ending / starting_equity - 1.0) * 100.0) if starting_equity else None,
        "peak_equity": float(peak_eq if pnls else starting_equity),
        "trough_equity": float(trough if pnls else starting_equity),
        "exit_mix": dict(exits),
    }


def equity_curve(trades: list[dict], starting: float = CAPITAL) -> list[dict]:
    rows = []
    eq = starting
    peak = starting
    for i, t in enumerate(sorted(trades, key=lambda x: _ts(x["entry_timestamp"]))):
        # chronological by exit for compounding path; use exit timestamp
        pass
    # Sort by exit time (PnL realized at exit)
    ordered = sorted(trades, key=lambda x: (_ts(x["exit_timestamp"] or x["entry_timestamp"]), x["trade_id"]))
    eq = starting
    peak = starting
    for t in ordered:
        eq += float(t["net_pnl"])
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        rows.append(
            {
                "trade_id": t["trade_id"],
                "exit_timestamp": t["exit_timestamp"],
                "entry_timestamp": t["entry_timestamp"],
                "pair": t["pair"],
                "net_pnl": t["net_pnl"],
                "equity": eq,
                "peak_equity": peak,
                "drawdown": dd,
                "drawdown_pct": dd * 100.0,
            }
        )
    return rows


def annotate_dd(trades: list[dict], curve: list[dict]) -> None:
    by_id = {r["trade_id"]: r for r in curve}
    # running dd before each trade: use previous curve point
    prev_dd = 0.0
    for r in curve:
        t = next(x for x in trades if x["trade_id"] == r["trade_id"])
        t["drawdown_at_entry"] = prev_dd
        t["drawdown_at_exit"] = r["drawdown"]
        prev_dd = r["drawdown"]


def weekly_rows(trades: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by[t["week"]].append(t)
    out = []
    for week in sorted(by):
        s = summarize_trades(by[week])
        thu = [t for t in by[week] if _ts(t["entry_timestamp"]).dayofweek == THURSDAY]
        out.append(
            {
                "WEEK": week,
                "fills": s["fills"],
                "net": s["net_pnl"],
                "PF": s["profit_factor"],
                "mean": s["mean_trade"],
                "drawdown": s["max_drawdown"],
                "WR": s["win_rate"],
                "Thursday_trades": len(thu),
                "Thursday_net": float(sum(float(t["net_pnl"]) for t in thu)),
            }
        )
    return out


def pair_rows(trades: list[dict], symbols: list[str]) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by[t["pair"]].append(t)
    out = []
    for sym in symbols:
        s = summarize_trades(by.get(sym, []))
        out.append(
            {
                "pair": sym,
                "fills": s["fills"],
                "net": s["net_pnl"],
                "PF": s["profit_factor"],
                "mean": s["mean_trade"],
                "WR": s["win_rate"],
            }
        )
    return out


def thursday_forensics(ctrl_trades: list[dict]) -> dict:
    thu = [t for t in ctrl_trades if _ts(t["entry_timestamp"]).dayofweek == THURSDAY]
    non = [t for t in ctrl_trades if _ts(t["entry_timestamp"]).dayofweek != THURSDAY]
    st = summarize_trades(thu) if thu else summarize_trades([])
    sn = summarize_trades(non) if non else summarize_trades([])
    return {
        "thursday_count": len(thu),
        "thursday_net": st["net_pnl"],
        "thursday_mean": st["mean_trade"],
        "thursday_PF": st["profit_factor"],
        "thursday_win_rate": st["win_rate"],
        "thursday_max_drawdown": st["max_drawdown"],
        "thursday_TP_count": st["TP_count"],
        "thursday_SL_count": st["SL_count"],
        "thursday_margin_stop_count": st["margin_stop_count"],
        "thursday_daily_flat_count": st["daily_flat_count"],
        "non_thursday_count": len(non),
        "non_thursday_net": sn["net_pnl"],
        "non_thursday_mean": sn["mean_trade"],
        "non_thursday_PF": sn["profit_factor"],
        "non_thursday_win_rate": sn["win_rate"],
    }


def classify_oos(ctrl: dict, trt: dict, thu_f: dict) -> tuple[str, list[str]]:
    """Pre-registered logic: treatment should improve net and/or PF/mean without
    collapsing throughput solely via trade deletion artifact.
    """
    reasons = []
    d_net = trt["net_pnl"] - ctrl["net_pnl"]
    d_pf = (
        (trt["profit_factor"] - ctrl["profit_factor"])
        if ctrl["profit_factor"] is not None and trt["profit_factor"] is not None
        and math.isfinite(ctrl["profit_factor"]) and math.isfinite(trt["profit_factor"])
        else None
    )
    d_mean = (
        (trt["mean_trade"] - ctrl["mean_trade"])
        if ctrl["mean_trade"] is not None and trt["mean_trade"] is not None
        else None
    )
    d_mdd = trt["max_drawdown"] - ctrl["max_drawdown"]
    retention = trt["fills"] / max(1, ctrl["fills"])
    reasons.append(f"delta_net={d_net:.4f}")
    reasons.append(f"delta_PF={d_pf}")
    reasons.append(f"delta_mean={d_mean}")
    reasons.append(f"delta_MDD={d_mdd:.6f}")
    reasons.append(f"fill_retention={retention:.4f}")
    reasons.append(
        f"control_thursday_net={thu_f['thursday_net']:.4f} "
        f"n={thu_f['thursday_count']}"
    )

    thu_negative = thu_f["thursday_count"] > 0 and thu_f["thursday_net"] < 0
    improved_net = d_net > 0
    improved_pf = d_pf is not None and d_pf > 0
    improved_mean = d_mean is not None and d_mean > 0
    degraded_net = d_net < 0
    degraded_pf = d_pf is not None and d_pf < 0

    if retention < 0.50 and improved_net:
        reasons.append("throughput_sensitive_retention_below_50pct")
        return "INCONCLUSIVE", reasons

    if improved_net and (improved_pf or improved_mean) and thu_negative:
        reasons.append("treatment_improved_net_and_quality_with_negative_Thu_cluster")
        return "SUPPORTED", reasons
    if improved_net and thu_negative and not degraded_pf:
        reasons.append("treatment_improved_net_Thu_negative_PF_not_worse")
        return "SUPPORTED", reasons
    if degraded_net and degraded_pf:
        reasons.append("treatment_worsened_net_and_PF")
        return "NOT SUPPORTED", reasons
    if degraded_net and not thu_negative:
        reasons.append("Thu_cluster_not_negative_in_September; treatment did not help")
        return "NOT SUPPORTED", reasons
    reasons.append("mixed_or_weak_signal")
    return "INCONCLUSIVE", reasons


def signals_to_rows(tape: list[dict], trades: list[dict]) -> list[dict]:
    filled_keys = set()
    for t in trades:
        filled_keys.add((t["pair"], str(t["entry_timestamp"])[:13]))
    rows = []
    for i, r in enumerate(tape, 1):
        tu = int(float(r["time_unix"]))
        ts = str(pd.Timestamp(tu, unit="s", tz="UTC"))
        became = (str(r.get("symbol")), ts[:13]) in filled_keys
        rows.append(
            {
                "signal_id": i,
                "pair": r.get("symbol"),
                "action": r.get("action"),
                "signal_timestamp": ts,
                "stop_loss": r.get("stop_loss"),
                "take_profit": r.get("take_profit"),
                "pattern": r.get("pattern"),
                "became_trade": became,
                "blocked_thursday": is_thursday_entry(r),
            }
        )
    return rows


def main() -> int:
    now = datetime.now(timezone.utc)
    if now < datetime(2026, 10, 1, tzinfo=timezone.utc):
        print("Calendar September not closed yet — refusing OOS.", flush=True)
        return 2

    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun["spread_pips_by_symbol"].keys())
    spreads = dict(jun["spread_pips_by_symbol"])
    assert len(symbols) == 60
    uhash = universe_hash(symbols)

    ts = now.strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"gate2_september_oos_final_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    # ---- 1) Data audit ----
    print("=== DATA AUDIT ===", flush=True)
    h1_rows = [audit_symbol(s, "1h") for s in symbols]
    m1_rows = [audit_symbol(s, "1m") for s in symbols]
    h1_ok = sum(1 for r in h1_rows if r["has_sep30"])
    m1_ok = sum(1 for r in m1_rows if r["has_sep30"])
    coverage_rows = []
    for a, b in zip(h1_rows, m1_rows):
        coverage_rows.append(
            {
                "symbol": a["symbol"],
                "H1_n": a["n"],
                "H1_tmin": a["tmin"],
                "H1_tmax": a["tmax"],
                "H1_status": a["status"],
                "H1_gaps": a["gaps"],
                "H1_dupes": a["duplicates"],
                "H1_ohlc_invalid": a["ohlc_invalid"],
                "M1_n": b["n"],
                "M1_tmin": b["tmin"],
                "M1_tmax": b["tmax"],
                "M1_status": b["status"],
                "M1_gaps": b["gaps"],
                "M1_dupes": b["duplicates"],
                "M1_ohlc_invalid": b["ohlc_invalid"],
            }
        )
    write_csv(out / "DATA_COVERAGE.csv", coverage_rows)

    if h1_ok != 60 or m1_ok != 60:
        msg = (
            f"September data incomplete: H1 {h1_ok}/60, M1 {m1_ok}/60 through Sep 30. "
            "OOS backtest NOT RUN. Extend utc_v2 M1 via MT5 first."
        )
        print(msg, flush=True)
        (out / "OOS_MANIFEST.md").write_text(
            f"# September OOS — BLOCKED\n\n{msg}\n", encoding="utf-8"
        )
        (out / "EXECUTIVE_SUMMARY.md").write_text(
            "# SEPTEMBER 2026 OOS\n\nSTATUS:\n**NOT PRODUCED**\n\n"
            f"Reason: {msg}\n",
            encoding="utf-8",
        )
        return 2

    # ---- 2) Freeze manifest + hashes BEFORE eval ----
    print("=== FREEZE MANIFEST ===", flush=True)
    h1_hash = dataset_hash(symbols, "1h")
    m1_hash = dataset_hash(symbols, "1m")
    ctrl_fp = fingerprint(CONTROL)
    trt_fp = fingerprint(TREATMENT)
    hashes = {
        "git_commit": _git(),
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "n1_control_sha256": ctrl_fp,
        "n1_thursday_sha256": trt_fp,
        "n1_control": CONTROL,
        "n1_thursday": TREATMENT,
        "september_H1_dataset_sha256": h1_hash,
        "september_M1_dataset_sha256": m1_hash,
        "universe_sha256": uhash,
        "universe_n": 60,
        "jun_tape_sha256": file_sha256(JUN_TAPE),
        "jul_tape_sha256": file_sha256(JUL_TAPE),
        "aug_tape_sha256": file_sha256(AUG_TAPE),
        "prior_thursday_gate_run": str(THU_PRIOR.as_posix()),
        "research_period": "2026-06 / 2026-07 / 2026-08",
        "oos_period": "2026-09 (locked holdout)",
        "eval_entry_start": str(SEP_ENTRY_START),
        "eval_end_exclusive": str(SEP_END),
        "data_window": "2026-09-01 .. 2026-09-30 inclusive",
        "hypotheses_tested": 1,
        "pre_registered_comparison": ["n1_control", "n1_thursday"],
        "posthoc_excluded": [
            "Thu×RR",
            "rr_factor sweeps",
            "GhostSL variants",
            "Internet strategies",
            "Donchian variants",
        ],
    }
    (out / "DATASET_HASHES.json").write_text(
        json.dumps(hashes, indent=2), encoding="utf-8"
    )
    (out / "SIMULATOR_CONFIG.json").write_text(
        json.dumps(
            {
                "engine": "MultiMarketEngine",
                "path": "h1_signal_1m_exit",
                "frozen": True,
                "modified_this_run": False,
                "config": CONTROL,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = f"""# OOS MANIFEST — September 2026 Locked Holdout

Frozen at (UTC): {hashes['frozen_at_utc']}
Git commit: `{hashes['git_commit']}`

## Period roles

| Period | Role |
|---|---|
| June 2026 | research / training / discovery |
| July 2026 | research / training / discovery |
| August 2026 | research / training / discovery |
| **September 2026** | **LOCKED OOS (this experiment)** |

## Pre-registered comparison (exactly one hypothesis)

- CONTROL: `n1_control` (Thursday gate OFF)
- TREATMENT: `n1_thursday` (Thursday gate ON)
- Sole difference: `thursday_gate` false → true

## Hashes (do not alter after evaluation starts)

- N1 control config: `{ctrl_fp}`
- N1 Thursday config: `{trt_fp}`
- September H1 dataset: `{h1_hash}`
- September M1 dataset: `{m1_hash}`
- 60-pair universe: `{uhash}`

## Data coverage

- H1 through Sep 30: **{h1_ok}/60**
- M1 through Sep 30: **{m1_ok}/60**
- Provenance: `data/cache/mt5/utc_v2` (MT5 only)
- Eval entry start (day-3 N1 convention): `{SEP_ENTRY_START}`
- Eval end exclusive: `{SEP_END}`

## Integrity rules

- No September optimization
- No post-hoc candidate evaluation as OOS
- No Live/Mirror contamination
- Multiple-testing count for this OOS claim: **1**
"""
    (out / "OOS_MANIFEST.md").write_text(manifest, encoding="utf-8")

    # ---- 3) Load period + scan decisions + run BOTH arms ----
    period = load_september_period(symbols, spreads)
    tape_ctrl = scan_decisions(period)
    (out / "september_decisions_sl_4p0.json").write_text(
        json.dumps(tape_ctrl, indent=2), encoding="utf-8"
    )
    hashes["september_decision_tape_sha256"] = fingerprint(tape_ctrl)
    hashes["september_decision_tape_n"] = len(tape_ctrl)
    (out / "DATASET_HASHES.json").write_text(
        json.dumps(hashes, indent=2), encoding="utf-8"
    )

    tape_trt = filter_block_thursday_entries(tape_ctrl)
    tape_stats = {
        "control": thursday_entry_stats(tape_ctrl),
        "treatment": thursday_entry_stats(tape_trt),
    }
    (out / "TAPE_STATS.json").write_text(
        json.dumps(tape_stats, indent=2), encoding="utf-8"
    )

    print("=== CONTROL n1_control ===", flush=True)
    ctrl_trades, ctrl_tape, ctrl_result = run_replay(period, tape_ctrl)
    print("=== TREATMENT n1_thursday ===", flush=True)
    trt_trades, trt_tape, trt_result = run_replay(period, tape_trt)

    # Freeze raw results immediately
    write_csv(out / "CONTROL_TRADES.csv", ctrl_trades)
    write_csv(out / "TREATMENT_TRADES.csv", trt_trades)
    (out / "CONTROL_TRADES.json").write_text(
        json.dumps(ctrl_trades, indent=2), encoding="utf-8"
    )
    (out / "TREATMENT_TRADES.json").write_text(
        json.dumps(trt_trades, indent=2), encoding="utf-8"
    )
    write_csv(out / "CONTROL_SIGNALS.csv", signals_to_rows(ctrl_tape, ctrl_trades))
    write_csv(out / "TREATMENT_SIGNALS.csv", signals_to_rows(trt_tape, trt_trades))
    print("RAW RESULTS FROZEN", flush=True)

    # ---- 4) Forensics (descriptive only) ----
    ctrl_s = summarize_trades(ctrl_trades)
    trt_s = summarize_trades(trt_trades)
    write_csv(out / "CONTROL_MONTHLY.csv", [{"month": "2026-09", **ctrl_s}])
    write_csv(out / "TREATMENT_MONTHLY.csv", [{"month": "2026-09", **trt_s}])

    ctrl_eq = equity_curve(ctrl_trades)
    trt_eq = equity_curve(trt_trades)
    annotate_dd(ctrl_trades, ctrl_eq)
    annotate_dd(trt_trades, trt_eq)
    write_csv(out / "CONTROL_TRADES.csv", ctrl_trades)
    write_csv(out / "TREATMENT_TRADES.csv", trt_trades)
    write_csv(out / "CONTROL_EQUITY_CURVE.csv", ctrl_eq)
    write_csv(out / "TREATMENT_EQUITY_CURVE.csv", trt_eq)

    w_ctrl = weekly_rows(ctrl_trades)
    w_trt = weekly_rows(trt_trades)
    write_csv(out / "WEEKLY_CONTROL.csv", w_ctrl)
    write_csv(out / "WEEKLY_TREATMENT.csv", w_trt)
    w_cmp = []
    weeks = sorted({r["WEEK"] for r in w_ctrl} | {r["WEEK"] for r in w_trt})
    wc = {r["WEEK"]: r for r in w_ctrl}
    wt = {r["WEEK"]: r for r in w_trt}
    for w in weeks:
        c = wc.get(w, {})
        t = wt.get(w, {})
        w_cmp.append(
            {
                "WEEK": w,
                "control_net": c.get("net"),
                "treatment_net": t.get("net"),
                "control_PF": c.get("PF"),
                "treatment_PF": t.get("PF"),
                "control_fills": c.get("fills"),
                "treatment_fills": t.get("fills"),
                "control_mean": c.get("mean"),
                "treatment_mean": t.get("mean"),
                "control_drawdown": c.get("drawdown"),
                "treatment_drawdown": t.get("drawdown"),
                "Thursday_trades": c.get("Thursday_trades"),
                "Thursday_net": c.get("Thursday_net"),
                "delta_net": (
                    (t.get("net") - c.get("net"))
                    if c.get("net") is not None and t.get("net") is not None
                    else None
                ),
            }
        )
    write_csv(out / "WEEKLY_COMPARISON.csv", w_cmp)

    p_ctrl = pair_rows(ctrl_trades, symbols)
    p_trt = pair_rows(trt_trades, symbols)
    write_csv(out / "PAIR_CONTROL.csv", p_ctrl)
    write_csv(out / "PAIR_TREATMENT.csv", p_trt)
    pc = {r["pair"]: r for r in p_ctrl}
    pt = {r["pair"]: r for r in p_trt}
    p_cmp = []
    for sym in symbols:
        c, t = pc[sym], pt[sym]
        p_cmp.append(
            {
                "pair": sym,
                "control_fills": c["fills"],
                "control_net": c["net"],
                "control_PF": c["PF"],
                "treatment_fills": t["fills"],
                "treatment_net": t["net"],
                "treatment_PF": t["PF"],
                "delta_net": t["net"] - c["net"],
                "delta_fills": t["fills"] - c["fills"],
                "helped": (t["net"] - c["net"]) > 0,
                "hurt": (t["net"] - c["net"]) < 0,
                "no_thursday_activity": c["fills"] == t["fills"],
            }
        )
    write_csv(out / "PAIR_COMPARISON.csv", p_cmp)

    thu_f = thursday_forensics(ctrl_trades)
    write_csv(out / "THURSDAY_FORENSICS.csv", [thu_f])

    # Causal decomposition
    causal = {
        "control_total_net": ctrl_s["net_pnl"],
        "control_thursday_net": thu_f["thursday_net"],
        "control_non_thursday_net": thu_f["non_thursday_net"],
        "control_minus_thursday": ctrl_s["net_pnl"] - thu_f["thursday_net"],
        "treatment_total_net": trt_s["net_pnl"],
        "treatment_minus_control_non_thu": trt_s["net_pnl"] - thu_f["non_thursday_net"],
        "fills_removed": ctrl_s["fills"] - trt_s["fills"],
        "thursday_fills_control": thu_f["thursday_count"],
        "mean_effect": (
            (trt_s["mean_trade"] - ctrl_s["mean_trade"])
            if ctrl_s["mean_trade"] is not None and trt_s["mean_trade"] is not None
            else None
        ),
        "TP_delta": trt_s["TP_count"] - ctrl_s["TP_count"],
        "margin_stop_delta": trt_s["margin_stop_count"] - ctrl_s["margin_stop_count"],
        "daily_flat_delta": trt_s["daily_flat_count"] - ctrl_s["daily_flat_count"],
    }
    (out / "THURSDAY_CAUSAL_DECOMPOSITION.json").write_text(
        json.dumps(causal, indent=2), encoding="utf-8"
    )

    # Primary comparison
    status, reasons = classify_oos(ctrl_s, trt_s, thu_f)
    d_net = trt_s["net_pnl"] - ctrl_s["net_pnl"]
    d_mean = (
        trt_s["mean_trade"] - ctrl_s["mean_trade"]
        if ctrl_s["mean_trade"] is not None and trt_s["mean_trade"] is not None
        else None
    )
    d_pf = None
    if (
        ctrl_s["profit_factor"] is not None
        and trt_s["profit_factor"] is not None
        and math.isfinite(ctrl_s["profit_factor"])
        and math.isfinite(trt_s["profit_factor"])
    ):
        d_pf = trt_s["profit_factor"] - ctrl_s["profit_factor"]
    comparison = {
        "status": status,
        "reasons": reasons,
        "hypothesis": HYPOTHESIS,
        "control": ctrl_s,
        "treatment": trt_s,
        "delta_net": d_net,
        "delta_mean": d_mean,
        "delta_PF": d_pf,
        "delta_MDD": trt_s["max_drawdown"] - ctrl_s["max_drawdown"],
        "delta_fills": trt_s["fills"] - ctrl_s["fills"],
        "delta_winrate": (
            (trt_s["win_rate"] - ctrl_s["win_rate"])
            if ctrl_s["win_rate"] is not None and trt_s["win_rate"] is not None
            else None
        ),
        "treatment_control_net_ratio": (
            trt_s["net_pnl"] / ctrl_s["net_pnl"]
            if ctrl_s["net_pnl"] not in (0, None)
            else None
        ),
        "thursday_forensics": thu_f,
        "causal": causal,
        "hypotheses_tested": 1,
        "evidence_class": "PRE-REGISTERED OOS RESULT",
    }
    write_csv(
        out / "OOS_COMPARISON.csv",
        [
            {
                "metric": k,
                "control": ctrl_s.get(k),
                "treatment": trt_s.get(k),
                "delta": (
                    (trt_s[k] - ctrl_s[k])
                    if isinstance(ctrl_s.get(k), (int, float))
                    and isinstance(trt_s.get(k), (int, float))
                    else None
                ),
            }
            for k in (
                "fills",
                "wins",
                "losses",
                "win_rate",
                "gross_profit",
                "gross_loss",
                "profit_factor",
                "net_pnl",
                "mean_trade",
                "median_trade",
                "std_trade",
                "max_drawdown",
                "max_consecutive_losses",
                "TP_count",
                "SL_count",
                "margin_stop_count",
                "daily_flat_count",
                "friday_flat_count",
                "starting_equity",
                "ending_equity",
                "return_pct",
            )
        ],
    )
    (out / "OOS_STATISTICS.json").write_text(
        json.dumps(comparison, indent=2, default=str), encoding="utf-8"
    )

    # ---- 5) Jun–Sep table from prior locked Thursday gate + this Sep ----
    prior_ctrl = {}
    prior_trt = {}
    if (THU_PRIOR / "control_results.csv").is_file():
        with (THU_PRIOR / "control_results.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                prior_ctrl[r["period"]] = r
        with (THU_PRIOR / "treatment_results.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                prior_trt[r["period"]] = r
    jun_sep = []
    for month, key in (
        ("June", "june"),
        ("July", "july"),
        ("August", "august"),
    ):
        c = prior_ctrl.get(key, {})
        t = prior_trt.get(key, {})
        jun_sep.append(
            {
                "Month": month,
                "Control_PF": c.get("PF"),
                "Control_Net": c.get("net_pnl"),
                "Control_fills": c.get("fills"),
                "Thursday_PF": t.get("PF"),
                "Thursday_Net": t.get("net_pnl"),
                "Thursday_fills": t.get("fills"),
                "source": "gate2_thursday_gate_20260925_195229",
            }
        )
    jun_sep.append(
        {
            "Month": "September",
            "Control_PF": ctrl_s["profit_factor"],
            "Control_Net": ctrl_s["net_pnl"],
            "Control_fills": ctrl_s["fills"],
            "Thursday_PF": trt_s["profit_factor"],
            "Thursday_Net": trt_s["net_pnl"],
            "Thursday_fills": trt_s["fills"],
            "source": out.name,
        }
    )
    write_csv(out / "MONTHLY_COMPARISON_JUN_SEP.csv", jun_sep)

    # Regime descriptive
    def regime(trades: list[dict]) -> dict:
        if not trades:
            return {}
        dow = Counter(t["weekday"] for t in trades)
        sess = Counter(t["session"] or "unknown" for t in trades)
        side = Counter(t["direction"] for t in trades)
        exits = Counter(t["exit_reason"] for t in trades)
        rrs = [float(t["rr"]) for t in trades if t.get("rr") is not None]
        return {
            "fills": len(trades),
            "weekday": dict(dow),
            "session": dict(sess),
            "direction": dict(side),
            "exits": dict(exits),
            "rr_mean": float(statistics.mean(rrs)) if rrs else None,
            "rr_median": float(statistics.median(rrs)) if rrs else None,
            "unique_pairs": len({t["pair"] for t in trades}),
        }

    (out / "REGIME_DESCRIPTIVE.json").write_text(
        json.dumps(
            {"control": regime(ctrl_trades), "treatment": regime(trt_trades)},
            indent=2,
        ),
        encoding="utf-8",
    )

    # Weekly summary stats
    ctrl_week_nets = [r["net"] for r in w_ctrl]
    trt_week_nets = [r["net"] for r in w_trt]

    # ---- Reports ----
    helped = sum(1 for r in p_cmp if r["helped"])
    hurt = sum(1 for r in p_cmp if r["hurt"])
    none = sum(1 for r in p_cmp if r["no_thursday_activity"])

    exec_md = f"""# SEPTEMBER 2026 OOS

STATUS:
**{status}**

## Pre-registered hypothesis

{HYPOTHESIS}

## Primary result (September only)

| Metric | Control | Thursday | Delta (Thu−Ctrl) |
|---|---:|---:|---:|
| fills | {ctrl_s['fills']} | {trt_s['fills']} | {trt_s['fills']-ctrl_s['fills']} |
| win rate | {ctrl_s['win_rate']} | {trt_s['win_rate']} | {comparison['delta_winrate']} |
| profit factor | {ctrl_s['profit_factor']} | {trt_s['profit_factor']} | {d_pf} |
| net PnL | {ctrl_s['net_pnl']:.4f} | {trt_s['net_pnl']:.4f} | {d_net:.4f} |
| mean trade | {ctrl_s['mean_trade']} | {trt_s['mean_trade']} | {d_mean} |
| max drawdown | {ctrl_s['max_drawdown']:.6f} | {trt_s['max_drawdown']:.6f} | {trt_s['max_drawdown']-ctrl_s['max_drawdown']:.6f} |
| ending equity | {ctrl_s['ending_equity']:.2f} | {trt_s['ending_equity']:.2f} | {trt_s['ending_equity']-ctrl_s['ending_equity']:.2f} |

## Classification reasons

"""
    for r in reasons:
        exec_md += f"- {r}\n"
    exec_md += f"""
## Evidence class

**PRE-REGISTERED OOS RESULT** (hypotheses tested = 1)

Post-hoc discovery candidates (Thu×RR, GhostSL, Internet, Donchian, …) are **not** part of this claim.

## Separate evidence (not mixed)

- A. Strategy OOS evidence: this report
- B. Execution fidelity evidence: prior Live/Mirror campaign (kept separate)

## Artifacts

`{out}`
"""
    (out / "EXECUTIVE_SUMMARY.md").write_text(exec_md, encoding="utf-8")

    # Future hypotheses — separated
    future = f"""# FUTURE_HYPOTHESES — reserved for October+ (NOT September OOS)

September OOS status: **{status}**

These items are explicitly **not** part of the September claim:

1. Thu × RR compounds from the massive discovery lab
2. `rr_factor` ≈ 0.9–1.1 / `rr_min` ≈ 2.5 sweeps
3. GhostSL geometry variants
4. Internet / Donchian strategy families
5. AlexG10 flipped-entry / no-daily-flat experiments
6. Any parameter retune suggested by September residuals

If a pattern in September forensics looks promising, register it here for
**October+** validation — do not retune against September.
"""
    (out / "FUTURE_HYPOTHESES.md").write_text(future, encoding="utf-8")

    # Full report with Q1–Q16
    report = f"""# SEPTEMBER_OOS_REPORT

# SEPTEMBER 2026 OOS
STATUS:
**{status}**

Generated (UTC): {datetime.now(timezone.utc).isoformat()}
Git: `{_git()}`
Out: `{out}`

## Why this status

"""
    for r in reasons:
        report += f"- {r}\n"

    report += f"""
## Locked configurations

Sole difference: Thursday entry gate OFF → ON.

- Control fingerprint: `{ctrl_fp}`
- Treatment fingerprint: `{trt_fp}`
- Starting equity (canonical frozen research): **${CAPITAL:.0f}**
- Leverage: {LEV}×
- Path: H1 signal + M1 exit (`h1_signal_1m_exit`)

## Data

- H1 Sep 1–30: **{h1_ok}/60**
- M1 Sep 1–30: **{m1_ok}/60**
- Eval entries: `{SEP_ENTRY_START}` → `{SEP_END}` (exclusive end)
- H1 hash: `{h1_hash}`
- M1 hash: `{m1_hash}`

## Headline metrics

### Control
{json.dumps(ctrl_s, indent=2, default=str)}

### Treatment
{json.dumps(trt_s, indent=2, default=str)}

## Thursday forensics (CONTROL)

{json.dumps(thu_f, indent=2, default=str)}

## Causal decomposition

{json.dumps(causal, indent=2, default=str)}

## Weekly path

Control profitable weeks: {sum(1 for x in ctrl_week_nets if x > 0)} / {len(ctrl_week_nets)}
Treatment profitable weeks: {sum(1 for x in trt_week_nets if x > 0)} / {len(trt_week_nets)}
Control median weekly net: {statistics.median(ctrl_week_nets) if ctrl_week_nets else None}
Treatment median weekly net: {statistics.median(trt_week_nets) if trt_week_nets else None}
Control best/worst week: {(max(ctrl_week_nets), min(ctrl_week_nets)) if ctrl_week_nets else None}
Treatment best/worst week: {(max(trt_week_nets), min(trt_week_nets)) if trt_week_nets else None}

See `WEEKLY_COMPARISON.csv`.

## Pair-level

- Pairs where Thursday gate helped (Δnet>0): {helped}
- Pairs where it hurt (Δnet<0): {hurt}
- Pairs with no Thursday activity (fills unchanged): {none}

## June→September (Jun–Aug from prior locked Thursday-gate run; Sep = this OOS)

| Month | Control PF | Control Net | Thursday PF | Thursday Net |
|---|---:|---:|---:|---:|
"""
    for r in jun_sep:
        report += (
            f"| {r['Month']} | {r['Control_PF']} | {r['Control_Net']} | "
            f"{r['Thursday_PF']} | {r['Thursday_Net']} |\n"
        )

    report += f"""
## Executive questions

### Q1 — Was September data complete and valid for all 60 pairs?
Yes for H1 ({h1_ok}/60) and M1 ({m1_ok}/60) through Sep 30 in utc_v2. See `DATA_COVERAGE.csv`.

### Q2 — What happened to N1 control?
fills={ctrl_s['fills']}, WR={ctrl_s['win_rate']}, PF={ctrl_s['profit_factor']}, net={ctrl_s['net_pnl']:.4f}, MDD={ctrl_s['max_drawdown']:.6f}, ending_equity={ctrl_s['ending_equity']:.2f}

### Q3 — What happened to N1 Thursday?
fills={trt_s['fills']}, WR={trt_s['win_rate']}, PF={trt_s['profit_factor']}, net={trt_s['net_pnl']:.4f}, MDD={trt_s['max_drawdown']:.6f}, ending_equity={trt_s['ending_equity']:.2f}

### Q4 — Did Thursday improve or worsen September net PnL?
Delta net (treatment − control) = **{d_net:.4f}** → {"higher" if d_net > 0 else "lower" if d_net < 0 else "unchanged"} net PnL.

### Q5 — Did it improve/worsen PF?
Delta PF = **{d_pf}**.

### Q6 — Did it change drawdown?
Delta MDD = **{trt_s['max_drawdown']-ctrl_s['max_drawdown']:.6f}** (true sequential equity-curve drawdown).

### Q7 — How many trades did it remove?
**{ctrl_s['fills']-trt_s['fills']}** fills (control Thursday fills={thu_f['thursday_count']}).

### Q8 — Were September Thursdays actually a negative cluster?
Control Thursday net = **{thu_f['thursday_net']:.4f}** (n={thu_f['thursday_count']}, PF={thu_f['thursday_PF']}, WR={thu_f['thursday_win_rate']}).

### Q9 — Did the Thursday effect appear consistently across September weeks?
See `WEEKLY_COMPARISON.csv`. Treatment−control weekly delta path is preserved there (not collapsed to a single average).

### Q10 — Did it appear across pairs or was it concentrated?
helped={helped}, hurt={hurt}, no_thursday_activity={none}. See `PAIR_COMPARISON.csv`.

### Q11 — Did the mechanism observed in June–August repeat?
June–August prior: Thursday treatment improved aggregate net/PF with negative Thursday cluster.
September control Thursday net={thu_f['thursday_net']:.4f}; treatment Δnet={d_net:.4f}.
Mechanism repeat: **{"yes_negative_Thu_and_treatment_helped" if (thu_f["thursday_net"] < 0 and d_net > 0) else "no_or_partial"}**.

### Q12 — What does June→September look like chronologically?
See table above / `MONTHLY_COMPARISON_JUN_SEP.csv`. Sep is the holdout; Jun–Aug are prior locked-run figures.

### Q13 — What does the true compounded equity curve show?
Control: start {ctrl_s['starting_equity']:.2f} → end {ctrl_s['ending_equity']:.2f} (return {ctrl_s['return_pct']:.2f}%), MDD {ctrl_s['max_drawdown']:.6f}.
Treatment: start {trt_s['starting_equity']:.2f} → end {trt_s['ending_equity']:.2f} (return {trt_s['return_pct']:.2f}%), MDD {trt_s['max_drawdown']:.6f}.
Files: `CONTROL_EQUITY_CURVE.csv`, `TREATMENT_EQUITY_CURVE.csv`.

### Q14 — What evidence supports or contradicts the original Thursday hypothesis?
Status **{status}**. Reasons listed above. This is the sole pre-registered OOS comparison.

### Q15 — What remains unresolved?
- Live/Mirror execution fidelity PnL validation remains separate and not cleanly closed.
- Weekly path instability / pair concentration effects.
- Whether any post-hoc lab candidate would survive a *new* pre-registration on October+.

### Q16 — What hypotheses should be reserved for October+?
See `FUTURE_HYPOTHESES.md`. Do not retune on September.

## Multiple-testing discipline

September OOS hypotheses tested: **1**
(`n1_control` vs `n1_thursday`)

237 discovery experiments from the massive lab do **not** enter this OOS claim.

## Safety

- Live settings untouched
- Protected EURDKK untouched
- No orders placed
- Simulator economics unchanged
"""
    (out / "SEPTEMBER_OOS_REPORT.md").write_text(report, encoding="utf-8")

    print(f"STATUS={status}", flush=True)
    print(f"delta_net={d_net:.4f} delta_PF={d_pf} fills {ctrl_s['fills']}->{trt_s['fills']}", flush=True)
    print(f"DONE {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
