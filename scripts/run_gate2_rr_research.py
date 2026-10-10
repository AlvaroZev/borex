#!/usr/bin/env python3
"""Gate 2 RR research — pre-registered rr_min + fixed-RR ablations.

Control: N1@500-equivalent (ghost=4, rr_factor=1, rr_min=2, dynamic, both sides).
Offline. No Live/Mirror trading. Simulator unmodified.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG8Strategy
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.margin_stops import (
    margin_stop_out_prices,
    resolve_rr,
    rr_from_winrate,
)
from borex.backtest import margin_stops as margin_stops_mod
from borex.backtest import multi_market_engine as mme_mod
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.backtest.portfolio import PositionSide
from borex.gate2.daily_theory_broker import (
    aggregate_day,
    build_daily_dataset,
    trade_row_from_census,
    write_daily_artifacts,
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
CENSUS = (
    ROOT
    / "data/runs/gate2_fidelity_assessment/runs/g2a_20260924_130000/master_trade_census.json"
)
PRIOR_SEP = (
    ROOT
    / "data/runs/research/gate2_strategy_optimization_20260925_164821"
    / "september_todate_decisions_sl_4p0.json"
)
PRIOR_DAILY = (
    ROOT
    / "data/runs/research/gate2_strategy_optimization_v2_20260925_172803"
    / "daily_theory_broker.json"
)

CONTROL = dict(
    strategy="alexg8",
    ghost_sl_mult=4.0,
    leverage=LEV,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=5.0,
    true_sl_rr=2.0,
    rr_mode="dynamic",
    true_sl=True,
    size_mode="margin",
    sides="both",
    force_flat_daily=True,
    force_flat_friday=False,
    winrate_min_trades=20,
)

RR_MINS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5)
RR_FACTORS = (0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.4)
FIXED_RRS = (0.75, 1.0, 1.25, 1.5, 2.0)


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


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


def stages_rr(
    *,
    rr_mode: str,
    fixed_rr: float,
    winrate: float | None,
    rr_factor: float,
    closed_trades: int,
    winrate_min_trades: int,
    rr_min: float,
    rr_max: float,
) -> dict[str, Any]:
    """Mirror resolve_rr stages for diagnostics (no behavior change)."""
    wr = winrate
    wr_suppressed = False
    if (
        closed_trades is not None
        and winrate_min_trades > 0
        and int(closed_trades) < int(winrate_min_trades)
    ):
        wr = None
        wr_suppressed = True
    mode = (rr_mode or "fixed").strip().lower()
    if mode == "dynamic":
        raw_dynamic = None if wr is None else (1.0 / wr if wr > 0 else None)
        base = rr_from_winrate(wr, fixed_rr)
    else:
        raw_dynamic = None
        base = fixed_rr if fixed_rr > 0 else 3.0
    factor = rr_factor if rr_factor > 0 else 1.0
    post_factor = base * factor
    lo = float(rr_min or 0.0)
    hi = float(rr_max or 0.0)
    final = post_factor
    hit_min = hit_max = False
    if lo > 0 and final < lo:
        final = lo
        hit_min = True
    if hi > 0 and final > hi:
        final = hi
        hit_max = True
    return {
        "rr_mode": mode,
        "winrate_input": winrate,
        "winrate_used": wr,
        "wr_suppressed_by_min_trades": wr_suppressed,
        "closed_trades": closed_trades,
        "raw_dynamic_rr": raw_dynamic,
        "base_rr": base,
        "rr_factor": factor,
        "post_factor_rr": post_factor,
        "rr_min": lo,
        "rr_max": hi,
        "final_rr": final,
        "hit_rr_min": hit_min,
        "hit_rr_max": hit_max,
    }


def install_rr_logger(bucket: list[dict]) -> Callable[[], None]:
    """Patch resolve_rr in the modules the engine uses; return uninstall."""

    def logged_resolve_rr(**kwargs):
        info = stages_rr(
            rr_mode=kwargs.get("rr_mode", "fixed"),
            fixed_rr=float(kwargs.get("fixed_rr") or 3.0),
            winrate=kwargs.get("winrate"),
            rr_factor=float(kwargs.get("rr_factor") or 1.0),
            closed_trades=int(kwargs.get("closed_trades") or 0),
            winrate_min_trades=int(kwargs.get("winrate_min_trades") or 0),
            rr_min=float(kwargs.get("rr_min") or 0.0),
            rr_max=float(kwargs.get("rr_max") or 0.0),
        )
        out = resolve_rr(**kwargs)
        info["returned_rr"] = out
        bucket.append(info)
        return out

    prev_mme = mme_mod.resolve_rr
    prev_ms = margin_stops_mod.resolve_rr
    mme_mod.resolve_rr = logged_resolve_rr  # type: ignore
    margin_stops_mod.resolve_rr = logged_resolve_rr  # type: ignore

    def uninstall():
        mme_mod.resolve_rr = prev_mme
        margin_stops_mod.resolve_rr = prev_ms

    return uninstall


def configs_redundant(rr_min_a: float, f_a: float, rr_min_b: float, f_b: float, rr_max: float = 5.0) -> bool:
    """True if clamp(base*f, rmin, rmax) identical for all base in a dense grid."""
    bases = [i / 20 for i in range(20, 201)]  # 1.0 .. 10.0
    for b in bases:
        def fin(f, lo):
            x = b * f
            if lo > 0:
                x = max(x, lo)
            if rr_max > 0:
                x = min(x, rr_max)
            return x

        if abs(fin(f_a, rr_min_a) - fin(f_b, rr_min_b)) > 1e-12:
            return False
    return True


def collapse_grid(rr_mins, rr_factors, rr_max=5.0) -> tuple[list[tuple[float, float]], list[dict]]:
    """Keep unique (rr_min, rr_factor); document collapsed duplicates."""
    kept: list[tuple[float, float]] = []
    notes: list[dict] = []
    for rm, rf in itertools.product(rr_mins, rr_factors):
        dup_of = None
        for km, kf in kept:
            if configs_redundant(rm, rf, km, kf, rr_max):
                dup_of = (km, kf)
                break
        if dup_of:
            notes.append(
                {
                    "skipped": {"rr_min": rm, "rr_factor": rf},
                    "equivalent_to": {"rr_min": dup_of[0], "rr_factor": dup_of[1]},
                    "reason": "clamp(base*rr_factor, rr_min, rr_max) identical for base in [1,10]",
                }
            )
        else:
            kept.append((rm, rf))
    return kept, notes


# ---------- data / replay (mirrors v2 helpers) ----------


def load_cache_candles(symbol: str, interval: str, start: datetime, end: datetime):
    path = _cache_path(symbol, interval)
    if not path.is_file():
        return []
    df = pd.read_parquet(path)
    candles = _frame_to_candles(df)
    if interval.strip().lower() in {"1h", "h1"}:
        candles = _dedupe_hourly(candles)
    return _drop_forming_bar(_slice_candles(candles, start, end), interval)


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


def _cfg(
    spreads: dict[str, float],
    *,
    rr_factor: float,
    rr_min: float,
    rr_max: float = 5.0,
    rr_mode: str = "dynamic",
    true_sl_rr: float = 2.0,
) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(true_sl_rr),
        rr_mode=rr_mode,
        rr_factor=float(rr_factor),
        rr_min=float(rr_min),
        rr_max=float(rr_max),
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


def summarize(trades: list[dict], intents: int, mdd: float, entry_start, last_bar) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    days = max(1.0, (_ts(last_bar) - _ts(entry_start)).total_seconds() / 86400.0)
    sides = Counter(str(t["side"]).lower() for t in trades)
    exits = Counter(str(t["exit_reason"]) for t in trades)
    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        by_day[str(t["entry_time"])[:10]] += float(t["pnl"])
    day_pnls = list(by_day.values())
    max_cl = cur = 0
    for p in pnls:
        if p <= 0:
            cur += 1
            max_cl = max(max_cl, cur)
        else:
            cur = 0
    gp = sum(p for p in pnls if p > 0)
    gl = sum(p for p in pnls if p <= 0)
    pf = (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None)
    n = len(trades) or 1
    return {
        "intents": intents,
        "fills": len(trades),
        "trades_per_day": len(trades) / days,
        "WR": (wins / len(trades)) if trades else None,
        "PF": pf,
        "net_pnl": float(sum(pnls)) if pnls else 0.0,
        "mean_trade": float(statistics.mean(pnls)) if pnls else None,
        "median_trade": float(statistics.median(pnls)) if pnls else None,
        "MDD": float(mdd),
        "ending_equity": CAPITAL + (sum(pnls) if pnls else 0.0),
        "TP": int(exits.get("take_profit", 0)),
        "daily_close": int(exits.get("daily_close", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
        "TP_share": int(exits.get("take_profit", 0)) / n,
        "daily_close_share": int(exits.get("daily_close", 0)) / n,
        "margin_stop_share": int(exits.get("margin_stop", 0)) / n,
        "long_count": int(sides.get("long", 0) + sides.get("buy", 0)),
        "short_count": int(sides.get("short", 0) + sides.get("sell", 0)),
        "worst_day_pnl": min(day_pnls) if day_pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "max_consecutive_losses": max_cl,
        "exit_mix": dict(exits),
    }


def run_replay(
    period: dict,
    tape: list[dict],
    *,
    rr_factor: float,
    rr_min: float,
    rr_max: float = 5.0,
    rr_mode: str = "dynamic",
    true_sl_rr: float = 2.0,
    log_rr: bool = False,
) -> tuple[dict, list[dict]]:
    rr_log: list[dict] = []
    uninstall = install_rr_logger(rr_log) if log_rr else (lambda: None)
    try:
        strategy = AlexG8Strategy(
            min_rr=2.0,
            execution_interval="1h",
            peer_blend=0.0,
            ghost_sl_mult=4.0,
        )
        engine = MultiMarketEngine(
            strategy,
            _cfg(
                period["spreads"],
                rr_factor=rr_factor,
                rr_min=rr_min,
                rr_max=rr_max,
                rr_mode=rr_mode,
                true_sl_rr=true_sl_rr,
            ),
            max_positions=0,
        )
        engine._progress_every = 50_000
        result = engine.run(
            period["data"],
            timeframe="1h+1m",
            master_symbol=period["master"],
            same_bar_exit=False,
            start_master_i=period["prime_i"],
            ltf_by_symbol=period["m1"],
            decisions=tape,
        )
        window = []
        for t in result.trades:
            if _ts(t.entry_time) < period["entry_start"]:
                continue
            side = t.side.value if hasattr(t.side, "value") else str(t.side)
            window.append(
                {
                    "symbol": t.symbol,
                    "side": side,
                    "entry_time": str(t.entry_time),
                    "exit_time": str(t.exit_time),
                    "pnl": float(t.pnl),
                    "exit_reason": t.exit_reason,
                    "entry": float(t.entry_price),
                    "sl": float(t.stop_loss or 0),
                    "tp": float(t.take_profit or 0),
                    "planned_rr": float(t.score or 0),
                }
            )
        s = summarize(
            window,
            len(tape),
            float(result.max_drawdown_pct),
            period["entry_start"],
            period["data"][period["master"]][-1].timestamp,
        )
        return s, rr_log
    finally:
        uninstall()


def load_period(name, index_path, symbols, spreads, entry_start=None, end=None):
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    if entry_start is None:
        entry_start = _ts(idx["eval_start"]).floor("h")
    if end is None:
        end = _ts(idx["last_bar"]) + pd.Timedelta(hours=1)
    h1_start = (end - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    end_py = end.to_pydatetime()
    print(f"[{name}] loading…", flush=True)
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
    master = pick_master_symbol(data)
    entry_i = next(
        i
        for i, c in enumerate(data[master])
        if _ts(c.timestamp) >= entry_start
    )
    prime_i = max(int(AlexG8Strategy().min_bars), entry_i - GHOST_PRIME_BARS)
    return {
        "name": name,
        "entry_start": entry_start,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": prime_i,
    }


def dual_improved(a: dict, b: dict) -> bool:
    """a improved vs b on PF and mean_trade."""
    if a.get("PF") is None or b.get("PF") is None:
        return False
    if a.get("mean_trade") is None or b.get("mean_trade") is None:
        return False
    return float(a["PF"]) > float(b["PF"]) and float(a["mean_trade"]) > float(
        b["mean_trade"]
    )


def deltas(cand: dict, ctrl: dict) -> dict:
    out = {}
    for k in (
        "fills",
        "WR",
        "PF",
        "net_pnl",
        "mean_trade",
        "median_trade",
        "MDD",
        "TP",
        "daily_close",
        "margin_stop",
        "TP_share",
        "daily_close_share",
        "margin_stop_share",
    ):
        if cand.get(k) is not None and ctrl.get(k) is not None:
            out[f"delta_{k}"] = cand[k] - ctrl[k]
    return out


def rr_log_stats(rr_log: list[dict], trades: list[dict] | None = None) -> dict:
    if not rr_log:
        return {"n": 0}
    # Align roughly: one resolve per open attempt; use all
    hit_min = sum(1 for r in rr_log if r.get("hit_rr_min"))
    hit_max = sum(1 for r in rr_log if r.get("hit_rr_max"))
    by_side = {"long": {"n": 0, "hit_min": 0}, "short": {"n": 0, "hit_min": 0}}
    # side not in rr_log — skip side split unless we enrich later
    finals = [float(r["final_rr"]) for r in rr_log]
    posts = [float(r["post_factor_rr"]) for r in rr_log]
    raws = [float(r["raw_dynamic_rr"]) for r in rr_log if r.get("raw_dynamic_rr") is not None]
    return {
        "n_resolves": len(rr_log),
        "hit_rr_min_n": hit_min,
        "hit_rr_min_share": hit_min / len(rr_log),
        "hit_rr_max_n": hit_max,
        "hit_rr_max_share": hit_max / len(rr_log),
        "final_rr_mean": float(statistics.mean(finals)),
        "final_rr_median": float(statistics.median(finals)),
        "post_factor_mean": float(statistics.mean(posts)),
        "raw_dynamic_n": len(raws),
        "raw_dynamic_mean": float(statistics.mean(raws)) if raws else None,
        "wr_suppressed_n": sum(1 for r in rr_log if r.get("wr_suppressed_by_min_trades")),
    }


def expand_daily(out_dir: Path) -> dict:
    base = build_daily_dataset(CENSUS)
    rows = list(base["trade_rows"])
    seen = {(r.get("theory_event_id"), r.get("broker_ticket")) for r in rows}
    extra = 0
    if PRIOR_DAILY.is_file():
        prior = json.loads(PRIOR_DAILY.read_text(encoding="utf-8"))
        for r in prior.get("trade_rows") or []:
            key = (r.get("theory_event_id"), r.get("broker_ticket"))
            if key in seen:
                continue
            rows.append(r)
            seen.add(key)
            extra += 1
    by_day: dict[str, list] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("day") or "UNKNOWN")].append(r)
    daily = [aggregate_day(by_day[d]) for d in sorted(by_day)]
    dataset = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_census": str(CENSUS),
        "prior_daily": str(PRIOR_DAILY) if PRIOR_DAILY.is_file() else None,
        "extra_rows_from_prior": extra,
        "trade_rows": rows,
        "daily_aggregates": daily,
        "limitations": base["limitations"],
    }
    write_daily_artifacts(dataset, out_dir)
    (out_dir / "daily_theory_broker.json").write_text(
        json.dumps(dataset, indent=2, default=str), encoding="utf-8"
    )
    return dataset


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"gate2_rr_research_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    # ---- Pre-registration (written BEFORE results) ----
    prereg = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "hypothesis": (
            "The current rr_min floor may suppress useful lower-RR targets, "
            "particularly when rr_factor < 1, and relaxing the floor may improve "
            "trade economics or reduce adverse daily-close behavior."
        ),
        "control": CONTROL,
        "primary_selection_august": (
            "Candidate must improve August vs control on BOTH PF and mean_trade."
        ),
        "primary_validation": (
            "Same frozen candidate must improve vs control on BOTH June and July "
            "(PF and mean_trade). September-to-date is descriptive only."
        ),
        "failure_if": (
            "August-only improvement, or June-only / July-only improvement, "
            "or higher net with worse MDD without dual-metric improvement."
        ),
        "classifications_allowed": [
            "supported",
            "not_supported",
            "mixed",
            "improved_versus_control",
            "degraded_versus_control",
            "unstable_across_periods",
        ],
        "results_inspected_before_writing_this_file": False,
    }
    (out / "preregistration.md").write_text(
        "# Pre-registration — RR floor / dynamic RR research\n\n"
        + json.dumps(prereg, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (out / "preregistration.json").write_text(json.dumps(prereg, indent=2), encoding="utf-8")

    # ---- RR implementation audit ----
    audit_md = """# RR Implementation Audit

## Chain

1. `_open_signal` calls `resolve_rr(...)`.
2. `rr_mode=dynamic`: base = `1/winrate` if closed_trades >= winrate_min_trades (20), else base = `true_sl_rr` (2.0).
3. `rr_mode=fixed`: base = `true_sl_rr` (research ablation only).
4. `post_factor = base * rr_factor`.
5. **Then** clamps: `rr = max(post_factor, rr_min)` if rr_min>0; `rr = min(rr, rr_max)` if rr_max>0.
6. Under `true_sl` + `size_mode=margin`: SL/TP = `margin_stop_out_prices(entry, side, leverage, rr)`.
7. Trade `score` stores the planned RR used at open.

## Order of operations (confirmed in code)

`rr_min` / `rr_max` apply **after** `rr_factor`.

## What determines un-clamped dynamic RR

- Portfolio sample winrate among closed trades (after min_trades gate)
- Fallback `true_sl_rr` when winrate unavailable
- Then multiplied by `rr_factor`

## Not from signal characteristics

RR is **not** computed from per-signal structure strength; it is portfolio WR-based (dynamic) or fixed.
"""
    (out / "rr_implementation_audit.md").write_text(audit_md, encoding="utf-8")

    # ---- Daily theory-broker ----
    daily = expand_daily(out)
    print(
        f"Daily theory<->broker days={[d['day'] for d in daily['daily_aggregates']]} "
        f"n={len(daily['trade_rows'])}",
        flush=True,
    )

    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    uhash = universe_hash(symbols)

    periods = {
        "august": load_period("august", AUG_INDEX, symbols, spreads),
        "june": load_period("june", JUN_INDEX, symbols, spreads),
        "july": load_period("july", JUL_INDEX, symbols, spreads),
    }
    m1_ends = []
    for sym in symbols:
        p = _cache_path(sym, "1m")
        if p.is_file():
            m1_ends.append(
                pd.to_datetime(
                    pd.read_parquet(p, columns=["timestamp"])["timestamp"], utc=True
                ).max()
            )
    sep_end = min(m1_ends) + pd.Timedelta(minutes=1)
    periods["september_todate"] = load_period(
        "september_todate",
        AUG_INDEX,
        symbols,
        spreads,
        entry_start=_ts("2026-09-03T03:00:00+00:00"),
        end=sep_end,
    )

    tapes = {
        "august": remap_tape(json.loads(AUG_TAPE.read_text(encoding="utf-8")), periods["august"]["data"]),
        "june": remap_tape(json.loads(JUN_TAPE.read_text(encoding="utf-8")), periods["june"]["data"]),
        "july": remap_tape(json.loads(JUL_TAPE.read_text(encoding="utf-8")), periods["july"]["data"]),
    }
    if PRIOR_SEP.is_file():
        tapes["september_todate"] = remap_tape(
            json.loads(PRIOR_SEP.read_text(encoding="utf-8")),
            periods["september_todate"]["data"],
        )
    else:
        tapes["september_todate"] = []

    # ---- Control RR distribution on all periods ----
    print("Control RR distribution diagnostics…", flush=True)
    control_by_period = {}
    rr_rows_csv = []
    clamp_summary = {}
    for pname in ("june", "july", "august", "september_todate"):
        if not tapes.get(pname):
            continue
        s, rr_log = run_replay(
            periods[pname],
            tapes[pname],
            rr_factor=1.0,
            rr_min=2.0,
            rr_max=5.0,
            log_rr=True,
        )
        control_by_period[pname] = s
        stats = rr_log_stats(rr_log)
        clamp_summary[pname] = stats
        print(
            f"  CONTROL {pname}: PF={s.get('PF')} mean={s.get('mean_trade')} "
            f"hit_min={stats.get('hit_rr_min_n')}/{stats.get('n_resolves')}",
            flush=True,
        )
        for i, r in enumerate(rr_log):
            rr_rows_csv.append({"period": pname, "i": i, **r})

    with (out / "rr_distribution.csv").open("w", encoding="utf-8", newline="") as f:
        if rr_rows_csv:
            w = csv.DictWriter(f, fieldnames=list(rr_rows_csv[0].keys()))
            w.writeheader()
            w.writerows(rr_rows_csv)

    (out / "control_metrics.json").write_text(
        json.dumps(
            {"control": CONTROL, "by_period": control_by_period, "rr_clamp": clamp_summary},
            indent=2,
        ),
        encoding="utf-8",
    )

    # Side split of clamp: use planned_rr on trades + approximate from log order
    # Enrich clamp summary with long/short planned_rr from control august trades
    for pname in list(control_by_period.keys()):
        s, rr_log = run_replay(
            periods[pname],
            tapes[pname],
            rr_factor=1.0,
            rr_min=2.0,
            log_rr=True,
        )
        # reopen not needed — already have; skip duplicate heavy runs
        break

    # ---- Collapse redundant (rr_min, rr_factor) ----
    kept, collapse_notes = collapse_grid(RR_MINS, RR_FACTORS, rr_max=5.0)
    print(
        f"Grid collapse: {len(RR_MINS)*len(RR_FACTORS)} -> {len(kept)} unique "
        f"(skipped {len(collapse_notes)})",
        flush=True,
    )

    # ---- STAGE: August rr_min × rr_factor ----
    aug_results = []
    ctrl_aug = control_by_period["august"]
    t0 = time.perf_counter()
    for i, (rm, rf) in enumerate(kept, 1):
        s, _ = run_replay(
            periods["august"], tapes["august"], rr_factor=rf, rr_min=rm, log_rr=False
        )
        row = {
            "id": f"dyn_rmin{str(rm).replace('.', 'p')}_rf{str(rf).replace('.', 'p')}",
            "family": "rr_min_sweep",
            "rr_mode": "dynamic",
            "rr_min": rm,
            "rr_factor": rf,
            "rr_max": 5.0,
            "true_sl_rr": 2.0,
            "august": s,
            "august_dual_improved": dual_improved(s, ctrl_aug),
            "delta_august": deltas(s, ctrl_aug),
        }
        aug_results.append(row)
        if i % 10 == 0 or i == len(kept):
            print(
                f"  rr_min sweep {i}/{len(kept)} {row['id']} "
                f"PF={s.get('PF')} mean={s.get('mean_trade')} "
                f"improved={row['august_dual_improved']} "
                f"{(time.perf_counter()-t0)/60:.1f}m",
                flush=True,
            )

    # Also run skipped combos? No — collapsed. Document.

    # ---- Fixed-RR ablation on August ----
    fixed_results = []
    for fr in FIXED_RRS:
        # Isolated: rr_mode=fixed, true_sl_rr=fr, rr_factor=1, rr_min=0, rr_max=0
        s, _ = run_replay(
            periods["august"],
            tapes["august"],
            rr_factor=1.0,
            rr_min=0.0,
            rr_max=0.0,
            rr_mode="fixed",
            true_sl_rr=fr,
        )
        row = {
            "id": f"fixed_rr{str(fr).replace('.', 'p')}",
            "family": "fixed_rr_ablation",
            "rr_mode": "fixed",
            "rr_min": 0.0,
            "rr_factor": 1.0,
            "rr_max": 0.0,
            "true_sl_rr": fr,
            "research_only": True,
            "august": s,
            "august_dual_improved": dual_improved(s, ctrl_aug),
            "delta_august": deltas(s, ctrl_aug),
        }
        fixed_results.append(row)
        print(
            f"  fixed RR={fr} PF={s.get('PF')} mean={s.get('mean_trade')} "
            f"improved={row['august_dual_improved']}",
            flush=True,
        )

    # ---- Select August candidates (pre-registered) BEFORE looking at Jun/Jul ----
    # Selection must only use August fields.
    selected = [
        r
        for r in aug_results + fixed_results
        if r["august_dual_improved"]
    ]
    selected.sort(
        key=lambda r: (
            r["august"].get("mean_trade") or -1e9,
            r["august"].get("PF") or 0,
        ),
        reverse=True,
    )
    # Cap shortlist size
    selected = selected[:10]
    selection_doc = {
        "rule": prereg["primary_selection_august"],
        "selected_ids": [r["id"] for r in selected],
        "n_selected": len(selected),
        "note": "Selection used August only; June/July not inspected for selection.",
        "collapse_notes": collapse_notes,
        "n_unique_rr_min_factor": len(kept),
    }
    (out / "candidate_selection.json").write_text(
        json.dumps(selection_doc, indent=2), encoding="utf-8"
    )
    print(f"August-selected candidates: {selection_doc['selected_ids']}", flush=True)

    # ---- Validate selected on June/July; Sep descriptive ----
    validated = []
    for r in selected:
        by = {"august": r["august"]}
        for pname in ("june", "july", "september_todate"):
            if not tapes.get(pname):
                continue
            s, rr_log = run_replay(
                periods[pname],
                tapes[pname],
                rr_factor=float(r["rr_factor"]),
                rr_min=float(r["rr_min"]),
                rr_max=float(r.get("rr_max") or 0.0),
                rr_mode=str(r["rr_mode"]),
                true_sl_rr=float(r["true_sl_rr"]),
                log_rr=(pname in ("june", "july")),
            )
            by[pname] = s
            if pname in ("june", "july"):
                by[f"{pname}_rr_stats"] = rr_log_stats(rr_log)
            print(
                f"  {r['id']} {pname}: PF={s.get('PF')} mean={s.get('mean_trade')}",
                flush=True,
            )
        jun_ok = dual_improved(by.get("june") or {}, control_by_period["june"])
        jul_ok = dual_improved(by.get("july") or {}, control_by_period["july"])
        if jun_ok and jul_ok:
            val_label = "improved_versus_control"
            hyp = "supported"
        elif (jun_ok and not jul_ok) or (jul_ok and not jun_ok):
            val_label = "unstable_across_periods"
            hyp = "mixed"
        elif dual_improved(control_by_period["june"], by.get("june") or {}) and dual_improved(
            control_by_period["july"], by.get("july") or {}
        ):
            val_label = "degraded_versus_control"
            hyp = "not_supported"
        else:
            val_label = "mixed"
            hyp = "mixed"
        # MDD caution
        mixed_dd = False
        for p in ("june", "july", "august"):
            if p in by and p in control_by_period:
                if by[p].get("MDD") is not None and control_by_period[p].get("MDD") is not None:
                    if float(by[p]["MDD"]) > float(control_by_period[p]["MDD"]) * 1.25:
                        mixed_dd = True
        validated.append(
            {
                **{k: r[k] for k in r if k != "august"},
                "by_period": {k: v for k, v in by.items() if not k.endswith("_rr_stats")},
                "rr_stats": {k: by[k] for k in by if k.endswith("_rr_stats")},
                "delta_vs_control": {
                    p: deltas(by[p], control_by_period[p])
                    for p in ("june", "july", "august")
                    if p in by and p in control_by_period
                },
                "june_dual_improved": jun_ok,
                "july_dual_improved": jul_ok,
                "validation_label": val_label,
                "hypothesis_label": hyp,
                "mdd_caution": mixed_dd,
            }
        )

    # If no August candidates, hypothesis not_supported
    if not selected:
        overall_hyp = "not_supported"
    elif any(v["hypothesis_label"] == "supported" for v in validated):
        overall_hyp = "supported"
    elif any(v["hypothesis_label"] == "mixed" for v in validated):
        overall_hyp = "mixed"
    else:
        overall_hyp = "not_supported"

    # ---- Write CSVs ----
    def flat_row(family, r, period, s, label=None):
        return {
            "id": r.get("id"),
            "family": family,
            "rr_mode": r.get("rr_mode"),
            "rr_min": r.get("rr_min"),
            "rr_factor": r.get("rr_factor"),
            "rr_max": r.get("rr_max"),
            "true_sl_rr": r.get("true_sl_rr"),
            "period": period,
            "label": label,
            **{
                k: s.get(k)
                for k in (
                    "intents",
                    "fills",
                    "WR",
                    "PF",
                    "net_pnl",
                    "mean_trade",
                    "median_trade",
                    "MDD",
                    "ending_equity",
                    "TP",
                    "daily_close",
                    "margin_stop",
                    "TP_share",
                    "daily_close_share",
                    "margin_stop_share",
                    "long_count",
                    "short_count",
                    "worst_day_pnl",
                    "worst_trade",
                    "max_consecutive_losses",
                )
            },
        }

    rr_min_csv = []
    for r in aug_results:
        rr_min_csv.append(
            flat_row("rr_min_sweep", r, "august", r["august"], r["august_dual_improved"])
        )
    with (out / "rr_min_results.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rr_min_csv[0].keys()))
        w.writeheader()
        w.writerows(rr_min_csv)

    fixed_csv = [
        flat_row("fixed_rr", r, "august", r["august"], r["august_dual_improved"])
        for r in fixed_results
    ]
    with (out / "fixed_rr_results.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fixed_csv[0].keys()))
        w.writeheader()
        w.writerows(fixed_csv)

    all_rows = []
    # control
    for p, s in control_by_period.items():
        all_rows.append(
            flat_row(
                "control",
                {
                    "id": "g4p0_lev500_rr1p0_both",
                    "rr_mode": "dynamic",
                    "rr_min": 2.0,
                    "rr_factor": 1.0,
                    "rr_max": 5.0,
                    "true_sl_rr": 2.0,
                },
                p,
                s,
            )
        )
    for r in aug_results:
        all_rows.append(flat_row("rr_min_sweep", r, "august", r["august"]))
    for r in fixed_results:
        all_rows.append(flat_row("fixed_rr_ablation", r, "august", r["august"]))
    for v in validated:
        for p, s in v["by_period"].items():
            if p == "august":
                continue
            all_rows.append(
                flat_row(v.get("family", "candidate"), v, p, s, v.get("validation_label"))
            )

    with (out / "all_results.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)

    (out / "per_period_metrics.json").write_text(
        json.dumps(
            {
                "control": control_by_period,
                "clamp_summary": clamp_summary,
                "validated": validated,
                "august_rr_min_n": len(aug_results),
                "august_fixed_n": len(fixed_results),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    consistent = [
        v["id"]
        for v in validated
        if v["hypothesis_label"] == "supported" and not v.get("mdd_caution")
    ]

    # Mechanism notes from August near control
    mechanism = {
        "rr_min_effect_on_august": [],
        "fixed_rr_vs_dynamic_control": [],
    }
    for r in sorted(aug_results, key=lambda x: (x["rr_min"], x["rr_factor"])):
        if r["rr_factor"] == 1.0:
            mechanism["rr_min_effect_on_august"].append(
                {
                    "rr_min": r["rr_min"],
                    "PF": r["august"].get("PF"),
                    "mean": r["august"].get("mean_trade"),
                    "TP": r["august"].get("TP"),
                    "daily_close": r["august"].get("daily_close"),
                    "margin_stop": r["august"].get("margin_stop"),
                    "TP_share": r["august"].get("TP_share"),
                    "daily_close_share": r["august"].get("daily_close_share"),
                    "delta": r["delta_august"],
                }
            )
    for r in fixed_results:
        mechanism["fixed_rr_vs_dynamic_control"].append(
            {
                "fixed_rr": r["true_sl_rr"],
                "PF": r["august"].get("PF"),
                "mean": r["august"].get("mean_trade"),
                "TP": r["august"].get("TP"),
                "daily_close": r["august"].get("daily_close"),
                "dual_improved": r["august_dual_improved"],
                "delta": r["delta_august"],
            }
        )

    provenance = {
        "data": "MT5 data/cache/mt5/utc_v2",
        "universe_sha256": uhash,
        "universe_n": 60,
        "spreads": "June Raw N1",
        "simulator": "MultiMarketEngine FROZEN_FOR_STRATEGY_RESEARCH",
        "git": _git(),
        "leverage": LEV,
        "ghost_sl_mult": 4.0,
        "sides": "both",
        "no_live": True,
        "no_mirror": True,
    }
    (out / "data_provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    conclusions = {
        "hypothesis_result": overall_hyp,
        "rr_implementation": {
            "rr_min_after_rr_factor": True,
            "dynamic_base": "1/winrate after 20 closed trades else true_sl_rr=2.0",
            "true_sl_uses_final_rr_for_tp": True,
        },
        "control_clamp_summary": clamp_summary,
        "n_rr_min_configs_tested": len(aug_results),
        "n_fixed_rr_tested": len(fixed_results),
        "n_collapsed_redundant": len(collapse_notes),
        "august_selected": selection_doc["selected_ids"],
        "validated": [
            {
                "id": v["id"],
                "validation_label": v["validation_label"],
                "hypothesis_label": v["hypothesis_label"],
                "june_dual_improved": v["june_dual_improved"],
                "july_dual_improved": v["july_dual_improved"],
                "mdd_caution": v["mdd_caution"],
                "by_period": {
                    p: {
                        k: v["by_period"][p].get(k)
                        for k in ("PF", "mean_trade", "net_pnl", "MDD", "TP", "daily_close")
                    }
                    for p in v["by_period"]
                },
            }
            for v in validated
        ],
        "consistently_improved_vs_n1_500": consistent,
        "mechanism": mechanism,
        "daily_theory_broker_days": [d["day"] for d in daily["daily_aggregates"]],
        "daily_theory_broker_n": len(daily["trade_rows"]),
        "next_experiment": (
            "If supported: Mirror paper for CONTROL + supported candidate under authorization. "
            "If not_supported: stop RR-floor hunt; next focus July failure modes or finish "
            "September calendar OOS for frozen N1@500."
        ),
    }
    (out / "conclusions.json").write_text(json.dumps(conclusions, indent=2), encoding="utf-8")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git(),
        "experiment": "gate2_rr_research",
        "hypothesis_result": overall_hyp,
        "out_dir": str(out),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Report
    lines = [
        "# Gate 2 RR Research",
        "",
        f"Generated: {manifest['created_at']}",
        f"Commit: `{manifest['git_commit']}`",
        f"Hypothesis result: **{overall_hyp}**",
        "",
        "## Pre-registration",
        "",
        "See `preregistration.md` (written before result inspection).",
        "",
        "## Control clamp summary",
        "",
        "```json",
        json.dumps(clamp_summary, indent=2),
        "```",
        "",
        f"## August selection ({len(selected)} candidates)",
        "",
        json.dumps(selection_doc["selected_ids"], indent=2),
        "",
        "## Validation",
        "",
    ]
    if not validated:
        lines.append("No August dual-metric improvers; no OOS validation shortlist.")
    for v in validated:
        lines.append(f"### {v['id']} — {v['validation_label']} / hyp={v['hypothesis_label']}")
        for p, s in v["by_period"].items():
            lines.append(
                f"- {p}: PF={s.get('PF')} mean={s.get('mean_trade')} net={s.get('net_pnl')} "
                f"MDD={s.get('MDD')} TP={s.get('TP')} dc={s.get('daily_close')}"
            )
        lines.append("")
    lines += [
        "## Consistent improvers vs N1@500",
        "",
        json.dumps(consistent, indent=2),
        "",
        "## Next",
        "",
        conclusions["next_experiment"],
        "",
        "No Live/Mirror trading.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(conclusions, indent=2), flush=True)
    print(f"DONE -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
