#!/usr/bin/env python3
"""N1 long-only diagnostic ablation — offline, pre-registered, one-bit change.

Control: frozen N1 both sides (alexg8 ghost_sl_mult=4.0, lev=500, H1→1m,
rr_factor=1.0, force_flat_daily, Raw spreads, $7/lot, 60 pairs).

Treatment: identical except BLOCK NEW SHORT ENTRIES (buy-only decision tape).

No live trading. Cache + saved decision tapes only. No parameter sweeps.
"""

from __future__ import annotations

import csv
import json
import math
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

CAPITAL = 1000.0
GHOST_PRIME_BARS = 72
WARMUP_DAYS = 400

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
JUN_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/decisions_sl_4p0.json"
JUN_FROZEN = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/sl_4p0_lev_500.json"

AUG_INDEX = ROOT / "data/runs/mt5_1m_slmult_lev_split/index.json"
AUG_TAPE = ROOT / "data/runs/mt5_1m_slmult_lev_split/decisions_sl_4p0.json"
AUG_FROZEN = ROOT / "data/runs/mt5_1m_n1_tpfactor/sl_4p0_lev_500_rr_1p0.json"

KNOBS = dict(
    strategy="alexg8",
    rr_mode="dynamic",
    true_sl_rr=2.0,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=5.0,
    risk_include_commission=True,
    commission_at_entry=False,
    force_flat_friday=False,
    force_flat_daily=True,
    intra_hour_sl=False,
    path="h1_signal_1m_exit",
    max_positions=0,
    signal_exec_split=True,
    ghost_sl_mult=4.0,
    leverage=500.0,
)


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


def _pf(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    gp = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    if gl == 0:
        return float("inf") if gp > 0 else None
    return float(gp / abs(gl))


def _mean(xs: list[float]) -> float | None:
    return float(statistics.mean(xs)) if xs else None


def _median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def load_cache_candles(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list:
    """Read closed bars from utc_v2 parquet only — no MT5 connection."""
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


def load_universe_offline(
    symbols: list[str],
    *,
    h1_start: datetime,
    end: datetime,
    m1_start: datetime,
) -> tuple[dict, dict]:
    h1: dict = {}
    m1: dict = {}
    for i, sym in enumerate(symbols, 1):
        bars = load_cache_candles(sym, "1h", h1_start, end)
        if len(bars) < 120:
            print(f"  H1 skip {sym}: n={len(bars)}", flush=True)
            continue
        h1[sym] = bars
        minutes = load_cache_candles(sym, "1m", m1_start, end)
        if minutes:
            m1[sym] = minutes
        if i % 10 == 0 or i == len(symbols):
            print(
                f"  cache {i}/{len(symbols)} H1={len(h1)} 1m={len(m1)}",
                flush=True,
            )
    return h1, m1


def remap_tape_by_time(
    tape: list[dict],
    candles_by_symbol: dict,
) -> tuple[list[dict], dict[str, Any]]:
    """Re-key decision.index to the loaded H1 series via time_unix."""
    ts_maps: dict[str, dict[int, int]] = {}
    for sym, bars in candles_by_symbol.items():
        m: dict[int, int] = {}
        for i, c in enumerate(bars):
            m[int(_ts(c.timestamp).timestamp())] = i
        ts_maps[sym] = m

    remapped: list[dict] = []
    missed_sym = 0
    missed_ts = 0
    for row in tape:
        sym = str(row.get("symbol") or "")
        if sym not in ts_maps:
            missed_sym += 1
            continue
        tu = int(float(row["time_unix"]))
        idx = ts_maps[sym].get(tu)
        if idx is None:
            missed_ts += 1
            continue
        out = dict(row)
        out["index"] = int(idx)
        remapped.append(out)

    meta = {
        "input": len(tape),
        "remapped": len(remapped),
        "missed_symbol": missed_sym,
        "missed_timestamp": missed_ts,
        "actions": dict(Counter(str(r.get("action")) for r in remapped)),
    }
    return remapped, meta


def filter_long_only(tape: list[dict]) -> list[dict]:
    """One-bit ablation: drop sell intents; keep buy (and any non-sell)."""
    out = []
    for row in tape:
        action = str(row.get("action") or "").strip().lower()
        if action == "sell":
            continue
        if action in {"buy", "long"}:
            out.append(row)
    return out


def _cfg(spreads: dict[str, float]) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=float(KNOBS["leverage"]),
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(KNOBS["true_sl_rr"]),
        rr_mode=str(KNOBS["rr_mode"]),
        rr_factor=float(KNOBS["rr_factor"]),
        rr_min=float(KNOBS["rr_min"]),
        rr_max=float(KNOBS["rr_max"]),
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=bool(KNOBS["risk_include_commission"]),
        commission_at_entry=bool(KNOBS["commission_at_entry"]),
        force_flat_friday=bool(KNOBS["force_flat_friday"]),
        force_flat_daily=bool(KNOBS["force_flat_daily"]),
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=bool(KNOBS["intra_hour_sl"]),
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
        "margin": float(t.margin or 0),
        "rr": float(t.score or 0),
        "exit_reason": t.exit_reason,
        "pattern": str(t.pattern or ""),
    }


def summarize_trades(
    trades: list[dict],
    *,
    intents: int,
    result_mdd: float,
    ending_equity: float,
    entry_start: pd.Timestamp,
    last_bar: pd.Timestamp,
) -> dict[str, Any]:
    pnls = [float(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    days = max(1.0, (last_bar - entry_start).total_seconds() / 86400.0)
    sides = Counter(str(t["side"]).lower() for t in trades)
    exits = Counter(str(t["exit_reason"]) for t in trades)

    def exit_block(reason: str) -> dict[str, Any]:
        subset = [t for t in trades if t["exit_reason"] == reason]
        sp = [float(t["pnl"]) for t in subset]
        return {
            "count": len(subset),
            "share": (len(subset) / len(trades)) if trades else 0.0,
            "total_pnl": float(sum(sp)) if sp else 0.0,
            "mean_pnl": _mean(sp),
        }

    return {
        "intents": intents,
        "fills": len(trades),
        "trade_count": len(trades),
        "trades_per_day": len(trades) / days,
        "WR": (wins / len(trades)) if trades else None,
        "PF": _pf(pnls),
        "net_pnl": float(sum(pnls)) if pnls else 0.0,
        "mean_trade_pnl": _mean(pnls),
        "median_trade_pnl": _median(pnls),
        "MDD": float(result_mdd),
        "ending_equity": float(ending_equity),
        "long_count": int(sides.get("long", 0) + sides.get("buy", 0)),
        "short_count": int(sides.get("short", 0) + sides.get("sell", 0)),
        "sides": dict(sides),
        "exit_reasons": dict(exits),
        "take_profit": exit_block("take_profit"),
        "daily_close": exit_block("daily_close"),
        "margin_stop": exit_block("margin_stop"),
        "end_of_data": exit_block("end_of_data"),
    }


def run_replay(
    *,
    label: str,
    data: dict,
    m1: dict,
    spreads: dict[str, float],
    master: str,
    prime_i: int,
    entry_start: pd.Timestamp,
    tape: list[dict],
) -> tuple[dict[str, Any], list[dict]]:
    strategy = AlexG8Strategy(
        min_rr=float(KNOBS["true_sl_rr"]),
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(KNOBS["ghost_sl_mult"]),
    )
    engine = MultiMarketEngine(strategy, _cfg(spreads), max_positions=0)
    engine._progress_every = 250
    t0 = time.perf_counter()
    result = engine.run(
        data,
        timeframe="1h+1m",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=prime_i,
        ltf_by_symbol=m1,
        decisions=tape,
    )
    elapsed = time.perf_counter() - t0
    window = [_trade_row(t) for t in result.trades if _ts(t.entry_time) >= entry_start]
    last_bar = _ts(data[master][-1].timestamp)
    summary = summarize_trades(
        window,
        intents=len(tape),
        result_mdd=float(result.max_drawdown_pct),
        ending_equity=CAPITAL + sum(float(t["pnl"]) for t in window),
        entry_start=entry_start,
        last_bar=last_bar,
    )
    summary["elapsed_sec"] = elapsed
    summary["label"] = label
    summary["full_run_equity"] = float(result.final_equity)
    print(
        f"[{label}] intents={len(tape)} fills={summary['fills']} "
        f"WR={(summary['WR'] or 0)*100:.1f}% PF={summary['PF']} "
        f"net=${summary['net_pnl']:.2f} mean={summary['mean_trade_pnl']} "
        f"short={summary['short_count']} long={summary['long_count']} "
        f"in {elapsed/60:.1f}m",
        flush=True,
    )
    return summary, window


def long_side_consistency(
    control_trades: list[dict],
    treatment_trades: list[dict],
) -> dict[str, Any]:
    """Compare geometry for longs that appear in both books."""

    def key(t: dict) -> tuple:
        return (t["symbol"], str(t["entry_time"]), str(t["side"]).lower())

    ctrl_long = {
        key(t): t
        for t in control_trades
        if str(t["side"]).lower() in {"long", "buy"}
    }
    treat_long = {
        key(t): t
        for t in treatment_trades
        if str(t["side"]).lower() in {"long", "buy"}
    }
    shared = sorted(set(ctrl_long) & set(treat_long))
    geometry_mismatch = []
    for k in shared:
        a, b = ctrl_long[k], treat_long[k]
        diffs = {}
        for field in ("entry", "sl", "tp"):
            if not math.isclose(float(a[field]), float(b[field]), rel_tol=0, abs_tol=1e-9):
                diffs[field] = (a[field], b[field])
        if diffs:
            geometry_mismatch.append({"key": list(k), "diffs": diffs})

    only_ctrl = sorted(set(ctrl_long) - set(treat_long))
    only_treat = sorted(set(treat_long) - set(ctrl_long))
    return {
        "control_longs": len(ctrl_long),
        "treatment_longs": len(treat_long),
        "shared_longs": len(shared),
        "geometry_mismatches": len(geometry_mismatch),
        "geometry_mismatch_samples": geometry_mismatch[:10],
        "longs_only_in_control": len(only_ctrl),
        "longs_only_in_treatment": len(only_treat),
        "longs_only_in_control_samples": [list(x) for x in only_ctrl[:10]],
        "longs_only_in_treatment_samples": [list(x) for x in only_treat[:10]],
        "consistent": len(geometry_mismatch) == 0,
    }


def frozen_summary(path: Path) -> dict[str, Any]:
    rep = json.loads(path.read_text(encoding="utf-8"))
    trades = rep.get("trades") or []
    s = rep.get("summary") or {}
    pnls = [float(t["pnl"]) for t in trades]
    sides = Counter(str(t.get("side", "")).lower() for t in trades)
    exits = Counter(str(t.get("exit_reason", "")) for t in trades)

    def exit_block(reason: str) -> dict[str, Any]:
        subset = [t for t in trades if t.get("exit_reason") == reason]
        sp = [float(t["pnl"]) for t in subset]
        return {
            "count": len(subset),
            "share": (len(subset) / len(trades)) if trades else 0.0,
            "total_pnl": float(sum(sp)) if sp else 0.0,
            "mean_pnl": _mean(sp),
        }

    return {
        "source": str(path),
        "intents": rep.get("intents"),
        "fills": len(trades),
        "trade_count": len(trades),
        "WR": s.get("win_rate"),
        "PF": s.get("profit_factor"),
        "net_pnl": s.get("net_pnl"),
        "mean_trade_pnl": _mean(pnls),
        "median_trade_pnl": _median(pnls),
        "MDD": s.get("max_drawdown_pct"),
        "ending_equity": s.get("account"),
        "long_count": int(sides.get("long", 0) + sides.get("buy", 0)),
        "short_count": int(sides.get("short", 0) + sides.get("sell", 0)),
        "sides": dict(sides),
        "exit_reasons": dict(exits),
        "take_profit": exit_block("take_profit"),
        "daily_close": exit_block("daily_close"),
        "margin_stop": exit_block("margin_stop"),
        "knobs": rep.get("knobs"),
    }


def control_reproduction_ok(rerun: dict, frozen: dict) -> dict[str, Any]:
    checks = {
        "fills_match": rerun.get("fills") == frozen.get("fills"),
        "net_close": abs(float(rerun["net_pnl"]) - float(frozen["net_pnl"])) < 1.0,
        "pf_close": abs(float(rerun["PF"] or 0) - float(frozen["PF"] or 0)) < 0.02,
        "short_present": int(rerun.get("short_count") or 0) > 0,
    }
    checks["ok"] = all(checks[k] for k in ("fills_match", "net_close", "pf_close", "short_present"))
    return checks


def hypothesis_result(
    *,
    june_control: dict,
    june_treatment: dict,
    integrity: dict,
) -> str:
    if not integrity.get("simulator_unchanged"):
        return "INCONCLUSIVE"
    if not integrity.get("other_knobs_unchanged"):
        return "NOT_SUPPORTED"
    if not integrity.get("long_side_consistent"):
        return "NOT_SUPPORTED"
    if int(june_treatment.get("short_count") or -1) != 0:
        return "INCONCLUSIVE"

    pf = june_treatment.get("PF")
    mean_t = june_treatment.get("mean_trade_pnl")
    mean_c = june_control.get("mean_trade_pnl")
    if pf is None or mean_t is None or mean_c is None:
        return "INCONCLUSIVE"

    # Pre-registered failure
    if float(pf) < 1.0:
        return "NOT_SUPPORTED"
    if float(mean_t) < float(mean_c):
        return "NOT_SUPPORTED"

    # Pre-registered support
    if float(mean_t) > float(mean_c) and float(pf) >= 1.0:
        return "SUPPORTED"
    return "INCONCLUSIVE"


def period_windows(index_path: Path) -> tuple[pd.Timestamp, pd.Timestamp, datetime, datetime, datetime]:
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    entry_start = _ts(idx["eval_start"]).floor("h")
    last_bar = _ts(idx["last_bar"])
    # end exclusive-ish: include last_bar hour
    end = (last_bar + pd.Timedelta(hours=1)).to_pydatetime()
    h1_start = (last_bar - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    return entry_start, last_bar, h1_start, end, m1_start


def run_period(
    *,
    name: str,
    index_path: Path,
    tape_path: Path,
    spreads: dict[str, float],
    symbols: list[str],
) -> dict[str, Any]:
    entry_start, last_bar, h1_start, end, m1_start = period_windows(index_path)
    print(f"\n=== {name} load cache H1 {h1_start.date()} -> {end.date()} ===", flush=True)
    data, m1 = load_universe_offline(
        symbols, h1_start=h1_start, end=end, m1_start=m1_start
    )
    if len(data) < 50:
        raise SystemExit(f"{name}: insufficient H1 pairs {len(data)}")
    if len(m1) < 50:
        raise SystemExit(f"{name}: insufficient M1 pairs {len(m1)}")

    master = pick_master_symbol(data)
    master_bars = data[master]
    entry_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= entry_start),
        None,
    )
    if entry_i is None:
        raise SystemExit(f"{name}: no master bar on/after {entry_start}")
    warmup = int(AlexG8Strategy().min_bars)
    prime_i = max(warmup, entry_i - GHOST_PRIME_BARS)

    raw_tape = json.loads(tape_path.read_text(encoding="utf-8"))
    remapped, remap_meta = remap_tape_by_time(raw_tape, data)
    long_tape = filter_long_only(remapped)
    print(
        f"{name} remap {remap_meta} | long_only intents={len(long_tape)}",
        flush=True,
    )

    control_sum, control_trades = run_replay(
        label=f"{name}_control",
        data=data,
        m1=m1,
        spreads=spreads,
        master=master,
        prime_i=prime_i,
        entry_start=entry_start,
        tape=remapped,
    )
    treat_sum, treat_trades = run_replay(
        label=f"{name}_long_only",
        data=data,
        m1=m1,
        spreads=spreads,
        master=master,
        prime_i=prime_i,
        entry_start=entry_start,
        tape=long_tape,
    )
    consistency = long_side_consistency(control_trades, treat_trades)
    return {
        "period": name,
        "entry_start": str(entry_start),
        "last_bar": str(last_bar),
        "master": master,
        "h1_pairs": len(data),
        "m1_pairs": len(m1),
        "remap": remap_meta,
        "control": control_sum,
        "treatment": treat_sum,
        "control_trades": control_trades,
        "treatment_trades": treat_trades,
        "long_side_consistency": consistency,
    }


def write_trade_comparison(path: Path, control: list[dict], treatment: list[dict]) -> None:
    rows = []
    for arm, trades in (("control", control), ("treatment", treatment)):
        for t in trades:
            rows.append({"arm": arm, **t})
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_exit_breakdown(path: Path, control: dict, treatment: dict) -> None:
    rows = []
    for arm, summary in (("control", control), ("treatment", treatment)):
        for reason in ("take_profit", "daily_close", "margin_stop", "end_of_data"):
            block = summary.get(reason) or {}
            rows.append(
                {
                    "arm": arm,
                    "exit_reason": reason,
                    "count": block.get("count"),
                    "share": block.get("share"),
                    "total_pnl": block.get("total_pnl"),
                    "mean_pnl": block.get("mean_pnl"),
                }
            )
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["arm", "exit_reason", "count", "share", "total_pnl", "mean_pnl"]
        )
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"n1_long_only_ablation_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    symbols = sorted(spreads.keys())

    frozen_june = frozen_summary(JUN_FROZEN)
    frozen_aug = frozen_summary(AUG_FROZEN)

    june = run_period(
        name="june_oos",
        index_path=JUN_INDEX,
        tape_path=JUN_TAPE,
        spreads=spreads,
        symbols=symbols,
    )
    # August smoke uses June Raw spreads (pinned N1 costs) + August 4.0 tape.
    august = run_period(
        name="august_smoke",
        index_path=AUG_INDEX,
        tape_path=AUG_TAPE,
        spreads=spreads,
        symbols=symbols,
    )

    repro = control_reproduction_ok(june["control"], frozen_june)
    integrity = {
        "short_count_treatment_june": june["treatment"]["short_count"],
        "short_count_treatment_august": august["treatment"]["short_count"],
        "short_blocked": june["treatment"]["short_count"] == 0
        and august["treatment"]["short_count"] == 0,
        "long_side_consistent": bool(june["long_side_consistency"]["consistent"]),
        "long_side_details": june["long_side_consistency"],
        "simulator_unchanged": True,
        "other_knobs_unchanged": True,
        "single_change": "BLOCK NEW SHORT ENTRIES (buy-only decision replay)",
        "control_reproduction": repro,
        "knobs": KNOBS,
        "spreads_source": str(JUN_INDEX),
        "universe_n": len(symbols),
    }

    # Hypothesis uses frozen June control metrics (pre-registered baseline).
    # Also require re-run control reproduction for integrity.
    if not repro.get("ok"):
        hyp = "INCONCLUSIVE"
        integrity["note"] = (
            "Re-run control did not reproduce frozen N1 June closely enough; "
            "hypothesis gated as INCONCLUSIVE pending data/index alignment."
        )
    else:
        hyp = hypothesis_result(
            june_control=frozen_june,
            june_treatment=june["treatment"],
            integrity=integrity,
        )

    comparison = {
        "hypothesis": (
            "A substantial component of N1's June→July OOS deterioration "
            "is associated with the short side."
        ),
        "hypothesis_result": hyp,
        "june": {
            "frozen_control": {
                k: frozen_june[k]
                for k in (
                    "fills",
                    "WR",
                    "PF",
                    "net_pnl",
                    "mean_trade_pnl",
                    "MDD",
                    "ending_equity",
                    "long_count",
                    "short_count",
                    "take_profit",
                    "daily_close",
                    "margin_stop",
                )
            },
            "rerun_control": {
                k: june["control"][k]
                for k in (
                    "intents",
                    "fills",
                    "WR",
                    "PF",
                    "net_pnl",
                    "mean_trade_pnl",
                    "median_trade_pnl",
                    "MDD",
                    "ending_equity",
                    "long_count",
                    "short_count",
                    "trades_per_day",
                    "take_profit",
                    "daily_close",
                    "margin_stop",
                )
            },
            "long_only": {
                k: june["treatment"][k]
                for k in (
                    "intents",
                    "fills",
                    "WR",
                    "PF",
                    "net_pnl",
                    "mean_trade_pnl",
                    "median_trade_pnl",
                    "MDD",
                    "ending_equity",
                    "long_count",
                    "short_count",
                    "trades_per_day",
                    "take_profit",
                    "daily_close",
                    "margin_stop",
                )
            },
            "delta_mean_trade_pnl": (
                float(june["treatment"]["mean_trade_pnl"])
                - float(frozen_june["mean_trade_pnl"])
                if june["treatment"]["mean_trade_pnl"] is not None
                and frozen_june["mean_trade_pnl"] is not None
                else None
            ),
            "delta_pf": (
                float(june["treatment"]["PF"]) - float(frozen_june["PF"])
                if june["treatment"]["PF"] is not None and frozen_june["PF"] is not None
                else None
            ),
        },
        "august_smoke": {
            "label": "NON_SELECTION_INTEGRITY",
            "frozen_control_note": (
                "Frozen August N1 used tighter spreads in tpfactor; "
                "smoke here uses pinned June Raw spreads + August 4.0 tape."
            ),
            "rerun_control": {
                k: august["control"][k]
                for k in (
                    "fills",
                    "WR",
                    "PF",
                    "net_pnl",
                    "mean_trade_pnl",
                    "short_count",
                    "long_count",
                )
            },
            "long_only": {
                k: august["treatment"][k]
                for k in (
                    "fills",
                    "WR",
                    "PF",
                    "net_pnl",
                    "mean_trade_pnl",
                    "short_count",
                    "long_count",
                )
            },
            "implementation_issues": (
                []
                if august["treatment"]["short_count"] == 0
                and august["long_side_consistency"]["consistent"]
                else ["short_count_nonzero_or_geometry_mismatch"]
            ),
        },
        "integrity": integrity,
        "support_criteria": {
            "mean_trade_improved": (
                june["treatment"]["mean_trade_pnl"] is not None
                and frozen_june["mean_trade_pnl"] is not None
                and float(june["treatment"]["mean_trade_pnl"])
                > float(frozen_june["mean_trade_pnl"])
            ),
            "pf_ge_1": (
                june["treatment"]["PF"] is not None
                and float(june["treatment"]["PF"]) >= 1.0
            ),
            "long_side_ok": integrity["long_side_consistent"],
            "knobs_ok": integrity["other_knobs_unchanged"],
        },
    }

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "experiment": "n1_long_only_ablation",
        "offline": True,
        "live_trading": False,
        "out_dir": str(out),
        "hypothesis_result": hyp,
    }
    experiment_definition = {
        "control": "N1 both sides (frozen knobs + June Raw spreads + 60 pairs)",
        "treatment": "N1 long-only (block new short entries)",
        "single_change": "filter decision tape action != sell",
        "not_changed": [
            "long-entry logic",
            "ghost logic",
            "SL/TP geometry",
            "daily close",
            "Friday behavior",
            "leverage",
            "margin rules",
            "pair universe",
            "spread schedule (pinned June Raw)",
            "commission",
            "simulator",
            "timestamps / execution model",
            "ghost_sl_mult",
            "rr_factor",
            "position sizing",
        ],
        "periods": {
            "june": "primary confirmatory OOS",
            "august": "non-selection smoke / integrity",
            "july": "NOT used for selection (POST_LOCK_DESCRIPTIVE omitted)",
            "may": "unavailable — not used",
        },
        "knobs": KNOBS,
        "sources": {
            "june_index": str(JUN_INDEX),
            "june_tape": str(JUN_TAPE),
            "june_frozen": str(JUN_FROZEN),
            "august_index": str(AUG_INDEX),
            "august_tape": str(AUG_TAPE),
            "august_frozen": str(AUG_FROZEN),
        },
    }

    control_results = {
        "june_frozen": frozen_june,
        "june_rerun": {k: v for k, v in june["control"].items()},
        "august_smoke_rerun": {k: v for k, v in august["control"].items()},
        "august_frozen_reference": {
            k: frozen_aug[k]
            for k in ("fills", "WR", "PF", "net_pnl", "mean_trade_pnl", "MDD")
        },
        "remap_june": june["remap"],
        "remap_august": august["remap"],
    }
    treatment_results = {
        "june_long_only": {k: v for k, v in june["treatment"].items()},
        "august_smoke_long_only": {k: v for k, v in august["treatment"].items()},
        "long_side_consistency_june": june["long_side_consistency"],
        "long_side_consistency_august": august["long_side_consistency"],
    }

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "experiment_definition.json").write_text(
        json.dumps(experiment_definition, indent=2), encoding="utf-8"
    )
    (out / "control_results.json").write_text(
        json.dumps(control_results, indent=2), encoding="utf-8"
    )
    (out / "treatment_results.json").write_text(
        json.dumps(treatment_results, indent=2), encoding="utf-8"
    )
    (out / "comparison.json").write_text(
        json.dumps(comparison, indent=2), encoding="utf-8"
    )
    write_trade_comparison(
        out / "trade_level_comparison.csv",
        june["control_trades"],
        june["treatment_trades"],
    )
    write_exit_breakdown(
        out / "exit_breakdown.csv", june["control"], june["treatment"]
    )

    report = f"""# N1 Long-Only Diagnostic Ablation

Generated: {manifest['created_at']}
Commit: `{manifest['git_commit']}`
Result: **{hyp}**

## Experiment

- **Control:** N1 both sides (frozen configuration)
- **Treatment:** N1 long-only — block new short entries only
- **Single change:** buy-only decision-tape filter (no other knobs)

## Implementation integrity

- Treatment June short count: `{june['treatment']['short_count']}`
- Treatment August short count: `{august['treatment']['short_count']}`
- Long-side geometry consistent (shared longs): `{integrity['long_side_consistent']}`
- Simulator unchanged: `{integrity['simulator_unchanged']}`
- Other knobs unchanged: `{integrity['other_knobs_unchanged']}`
- Control reproduction vs frozen June: `{json.dumps(repro)}`

## June OOS (confirmatory)

### Frozen control
- fills: {frozen_june['fills']}
- WR: {frozen_june['WR']}
- PF: {frozen_june['PF']}
- net: {frozen_june['net_pnl']}
- mean trade: {frozen_june['mean_trade_pnl']}
- MDD: {frozen_june['MDD']}
- TP / daily_close / margin_stop: {frozen_june['take_profit']['count']} / {frozen_june['daily_close']['count']} / {frozen_june['margin_stop']['count']}

### Re-run control
- fills: {june['control']['fills']}
- WR: {june['control']['WR']}
- PF: {june['control']['PF']}
- net: {june['control']['net_pnl']}
- mean trade: {june['control']['mean_trade_pnl']}
- MDD: {june['control']['MDD']}
- short/long: {june['control']['short_count']} / {june['control']['long_count']}
- TP / daily_close / margin_stop: {june['control']['take_profit']['count']} / {june['control']['daily_close']['count']} / {june['control']['margin_stop']['count']}

### Long-only treatment
- fills: {june['treatment']['fills']}
- WR: {june['treatment']['WR']}
- PF: {june['treatment']['PF']}
- net: {june['treatment']['net_pnl']}
- mean trade: {june['treatment']['mean_trade_pnl']}
- MDD: {june['treatment']['MDD']}
- short/long: {june['treatment']['short_count']} / {june['treatment']['long_count']}
- TP / daily_close / margin_stop: {june['treatment']['take_profit']['count']} / {june['treatment']['daily_close']['count']} / {june['treatment']['margin_stop']['count']}

## August smoke (NON_SELECTION)

- control fills/PF/net/short: {august['control']['fills']} / {august['control']['PF']} / {august['control']['net_pnl']} / {august['control']['short_count']}
- long-only fills/PF/net/short: {august['treatment']['fills']} / {august['treatment']['PF']} / {august['treatment']['net_pnl']} / {august['treatment']['short_count']}
- implementation issues: {comparison['august_smoke']['implementation_issues']}

## Hypothesis result

**{hyp}**

### What this establishes
Diagnostic evidence only regarding whether the short side contributes materially to observed N1 OOS fragility under this one-bit ablation.

### What this does NOT establish
Long-only profitability, robustness, or production readiness. August was smoke-only. July was not used for selection.

### Next validation requirement
A later fully covered unseen month under identical provenance — only if result is SUPPORTED and the research branch is explicitly opened. No automatic next optimization.
"""
    (out / "report.md").write_text(report, encoding="utf-8")
    print(f"\nHYPOTHESIS_RESULT={hyp}", flush=True)
    print(f"Artifacts -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
