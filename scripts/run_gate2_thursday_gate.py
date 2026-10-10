#!/usr/bin/env python3
"""Gate 2 Thursday entry exclusion — pre-registered research-only gate.

Control: N1@500-equivalent. Treatment: identical + block Thursday NEW entries.
Selection: June + August only. July: frozen OOS validation if qualified.
Offline. No Live/Mirror. Simulator / alexg8 unmodified.
"""

from __future__ import annotations

import csv
import hashlib
import json
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
from borex.gate2.daily_theory_broker import (
    aggregate_day,
    build_daily_dataset,
    write_daily_artifacts,
)
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
CENSUS = (
    ROOT
    / "data/runs/gate2_fidelity_assessment/runs/g2a_20260924_130000/master_trade_census.json"
)
PRIOR_OPT = ROOT / "data/runs/research/gate2_strategy_optimization_20260925_164821"
PRIOR_RR = ROOT / "data/runs/research/gate2_rr_research_20260925_185421"
PRIOR_JULY = ROOT / "data/runs/research/gate2_july_failure_research_20260925_191732"
SEP_TAPE_PRIOR = PRIOR_OPT / "september_todate_decisions_sl_4p0.json"
PRIOR_DAILY = PRIOR_JULY / "daily_theory_broker.json"

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
)

HYPOTHESIS = (
    "Excluding new entries whose entry day is Thursday will improve strategy "
    "expectancy by removing a disproportionate share of low-quality trades "
    "associated with the July Thursday loss cluster, while preserving "
    "sufficient positive expectancy on June and August."
)

# Retention below this → trade-throughput sensitive classification
MIN_FILL_RETENTION = 0.50


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


def _cfg(spreads: dict[str, float]) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(CONTROL["true_sl_rr"]),
        rr_mode=str(CONTROL["rr_mode"]),
        rr_factor=float(CONTROL["rr_factor"]),
        rr_min=float(CONTROL["rr_min"]),
        rr_max=float(CONTROL["rr_max"]),
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
    thu_trades = [t for t in trades if _ts(t["entry_time"]).dayofweek == THURSDAY]
    non_thu = [t for t in trades if _ts(t["entry_time"]).dayofweek != THURSDAY]
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
        "thursday_fills": len(thu_trades),
        "thursday_net": float(sum(t["pnl"] for t in thu_trades)) if thu_trades else 0.0,
        "non_thursday_fills": len(non_thu),
        "non_thursday_net": float(sum(t["pnl"] for t in non_thu)) if non_thu else 0.0,
        "non_thursday_mean": (
            float(statistics.mean(t["pnl"] for t in non_thu)) if non_thu else None
        ),
    }


def run_replay(period: dict, tape: list[dict]) -> tuple[dict, list[dict]]:
    strategy = AlexG8Strategy(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=4.0,
    )
    engine = MultiMarketEngine(strategy, _cfg(period["spreads"]), max_positions=0)
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
            }
        )
    s = summarize(
        window,
        len(tape),
        float(result.max_drawdown_pct),
        period["entry_start"],
        period["data"][period["master"]][-1].timestamp,
    )
    return s, window


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
        i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= entry_start
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


def deltas(cand: dict, ctrl: dict) -> dict:
    out = {}
    for k in (
        "fills",
        "intents",
        "WR",
        "PF",
        "net_pnl",
        "mean_trade",
        "median_trade",
        "MDD",
        "TP",
        "daily_close",
        "margin_stop",
        "long_count",
        "short_count",
        "worst_day_pnl",
        "worst_trade",
        "max_consecutive_losses",
        "thursday_fills",
        "thursday_net",
        "non_thursday_mean",
    ):
        if cand.get(k) is not None and ctrl.get(k) is not None:
            out[f"delta_{k}"] = cand[k] - ctrl[k]
    return out


def aggregate_periods(summaries: list[dict]) -> dict:
    """Aggregate trade-level economics across periods via weighted sums."""
    fills = sum(int(s["fills"]) for s in summaries)
    intents = sum(int(s["intents"]) for s in summaries)
    net = sum(float(s["net_pnl"]) for s in summaries)
    # Reconstruct approx PF from unavailable gross — use weighted mean trade + net
    # Prefer recomputing from stored fields: mean * fills
    mean = (net / fills) if fills else None
    # Approximate PF unavailable without trade lists — caller may pass trades
    gp = gl = 0.0  # filled below if we have exit blocks only — leave None
    mdd = max(float(s["MDD"]) for s in summaries) if summaries else None
    tp = sum(int(s["TP"]) for s in summaries)
    dc = sum(int(s["daily_close"]) for s in summaries)
    ms = sum(int(s["margin_stop"]) for s in summaries)
    longs = sum(int(s["long_count"]) for s in summaries)
    shorts = sum(int(s["short_count"]) for s in summaries)
    wr_num = sum(float(s["WR"] or 0) * int(s["fills"]) for s in summaries)
    wr = (wr_num / fills) if fills else None
    return {
        "fills": fills,
        "intents": intents,
        "net_pnl": net,
        "mean_trade": mean,
        "WR": wr,
        "PF": None,  # set by aggregate_from_trades when available
        "MDD_max_across_periods": mdd,
        "TP": tp,
        "daily_close": dc,
        "margin_stop": ms,
        "long_count": longs,
        "short_count": shorts,
        "thursday_fills": sum(int(s.get("thursday_fills") or 0) for s in summaries),
        "thursday_net": sum(float(s.get("thursday_net") or 0) for s in summaries),
        "non_thursday_fills": sum(int(s.get("non_thursday_fills") or 0) for s in summaries),
        "non_thursday_net": sum(float(s.get("non_thursday_net") or 0) for s in summaries),
    }


def aggregate_from_trades(trade_lists: list[list[dict]], intents: int) -> dict:
    trades = [t for tl in trade_lists for t in tl]
    pnls = [float(t["pnl"]) for t in trades]
    gp = sum(p for p in pnls if p > 0)
    gl = sum(p for p in pnls if p <= 0)
    pf = (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None)
    wins = sum(1 for p in pnls if p > 0)
    thu = [t for t in trades if _ts(t["entry_time"]).dayofweek == THURSDAY]
    non = [t for t in trades if _ts(t["entry_time"]).dayofweek != THURSDAY]
    exits = Counter(t["exit_reason"] for t in trades)
    sides = Counter(str(t["side"]).lower() for t in trades)
    return {
        "fills": len(trades),
        "intents": intents,
        "net_pnl": float(sum(pnls)) if pnls else 0.0,
        "mean_trade": float(statistics.mean(pnls)) if pnls else None,
        "median_trade": float(statistics.median(pnls)) if pnls else None,
        "WR": (wins / len(trades)) if trades else None,
        "PF": pf,
        "TP": int(exits.get("take_profit", 0)),
        "daily_close": int(exits.get("daily_close", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
        "long_count": int(sides.get("long", 0) + sides.get("buy", 0)),
        "short_count": int(sides.get("short", 0) + sides.get("sell", 0)),
        "thursday_fills": len(thu),
        "thursday_net": float(sum(t["pnl"] for t in thu)) if thu else 0.0,
        "thursday_mean": float(statistics.mean(t["pnl"] for t in thu)) if thu else None,
        "non_thursday_fills": len(non),
        "non_thursday_net": float(sum(t["pnl"] for t in non)) if non else 0.0,
        "non_thursday_mean": float(statistics.mean(t["pnl"] for t in non)) if non else None,
        "gross_profit": float(gp),
        "gross_loss": float(gl),
    }


def selection_ok(ctrl_agg: dict, trt_agg: dict) -> tuple[bool, list[str]]:
    reasons = []
    ok = True
    for metric, label in (("PF", "PF"), ("mean_trade", "mean_trade"), ("net_pnl", "net_pnl")):
        c, t = ctrl_agg.get(metric), trt_agg.get(metric)
        if c is None or t is None:
            ok = False
            reasons.append(f"missing {label}")
            continue
        if float(t) <= float(c):
            ok = False
            reasons.append(f"{label} not improved ({t} <= {c})")
        else:
            reasons.append(f"{label} improved ({c} → {t})")
    fills_c = max(1, int(ctrl_agg.get("fills") or 1))
    retention = int(trt_agg.get("fills") or 0) / fills_c
    reasons.append(f"fill_retention={retention:.4f}")
    throughput_sensitive = retention < MIN_FILL_RETENTION
    if throughput_sensitive:
        reasons.append("trade_throughput_sensitive")
        # Still can "pass" metrics but flagged — protocol says classify explicitly;
        # do not auto-fail solely on retention unless catastrophic. User: "Do not
        # consider successful if improvement merely by eliminating most trading."
        # If retention < 50%, treat as not meeting success even if metrics up.
        ok = False
        reasons.append("failed: fill retention below 50% threshold")
    return ok, reasons


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k, v in r.items():
            if k not in keys and not isinstance(v, (dict, list)):
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def flat_period_row(arm: str, period: str, s: dict, d: dict | None = None) -> dict:
    row = {"arm": arm, "period": period, **{k: v for k, v in s.items() if not isinstance(v, (dict, list))}}
    if d:
        row.update({k: v for k, v in d.items() if not isinstance(v, (dict, list))})
    return row


def thursday_bucket(trades: list[dict]) -> dict:
    thu = [t for t in trades if _ts(t["entry_time"]).dayofweek == THURSDAY]
    pnls = [float(t["pnl"]) for t in thu]
    exits = Counter(t["exit_reason"] for t in thu)
    gp = sum(p for p in pnls if p > 0)
    gl = sum(p for p in pnls if p <= 0)
    return {
        "n": len(thu),
        "net": float(sum(pnls)) if pnls else 0.0,
        "mean": float(statistics.mean(pnls)) if pnls else None,
        "PF": (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None),
        "TP": int(exits.get("take_profit", 0)),
        "daily_close": int(exits.get("daily_close", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
        "gross_profit_removed": float(gp),
        "gross_loss_removed": float(gl),
    }


def expand_daily(out_dir: Path) -> dict:
    base = build_daily_dataset(CENSUS)
    rows = list(base["trade_rows"])
    seen = {(r.get("theory_event_id"), r.get("broker_ticket")) for r in rows}
    extra = 0
    for prior_path in (
        PRIOR_DAILY,
        PRIOR_RR / "daily_theory_broker.json",
        PRIOR_OPT.parent
        / "gate2_strategy_optimization_v2_20260925_172803"
        / "daily_theory_broker.json",
    ):
        if not prior_path.is_file():
            continue
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
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
        "extra_rows_from_prior": extra,
        "trade_rows": rows,
        "daily_aggregates": daily,
        "limitations": base["limitations"],
        "n_days": len(daily),
        "n_trade_rows": len(rows),
    }
    write_daily_artifacts(dataset, out_dir)
    (out_dir / "daily_theory_broker.json").write_text(
        json.dumps(dataset, indent=2, default=str), encoding="utf-8"
    )
    by_day_csv = out_dir / "daily_theory_broker_by_day.csv"
    if by_day_csv.is_file():
        (out_dir / "daily_theory_broker.csv").write_bytes(by_day_csv.read_bytes())
    return dataset


def write_preregistration(out: Path) -> None:
    text = f"""# Pre-registration — Thursday Entry Exclusion Gate

Written at (UTC): {datetime.now(timezone.utc).isoformat()}

**Written BEFORE any July treatment evaluation.**

## Hypothesis

{HYPOTHESIS}

## Treatment (exact)

```
IF entry_day_of_week == Thursday (UTC):
    block NEW entry intent
ELSE:
    preserve N1 behavior
```

- Do NOT modify existing exits.
- Do NOT close existing positions because calendar day is Thursday.
- Do NOT alter Thursday positions opened on earlier days.
- Only block NEW entries on Thursday.
- No other gate (no session / direction / rr_min combo).

## Control

N1@500-equivalent: alexg8, ghost_sl_mult=4.0, rr_factor=1.0, rr_min=2.0,
rr_max=5.0, both sides, lev=500-equivalent sizing, Raw+$7, 60 pairs,
H1→closest-sim 1m, force_flat_daily=true, force_flat_friday=false.

## Anti-leakage

- Selection / qualification: **June + August aggregate only**
- July: frozen OOS validation **after** selection freeze
- September-to-date: descriptive only
- May: excluded

## Primary selection criteria (June + August aggregate)

Treatment must improve versus control on ALL of:

1. aggregate PF (higher)
2. aggregate mean trade (higher)
3. aggregate net P&L (higher / positive improvement)

Also report MDD, fills, WR, TP, daily_close, margin_stop, long/short.
Fill retention = treatment_fills / control_fills.
If retention < 50%: classify **trade_throughput_sensitive** and fail selection.

## Implementation

Research-layer decision-tape filter:
`borex.gate2.thursday_entry_gate.filter_block_thursday_entries`
Does not modify production alexg8 or the frozen simulator.
"""
    (out / "preregistration.md").write_text(text, encoding="utf-8")


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"gate2_thursday_gate_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    # ---- PART 2: preregistration BEFORE any July treatment inspection ----
    write_preregistration(out)
    print("Pre-registration written (before July treatment).", flush=True)

    # Universe / spreads from June index (same 60-pair N1 universe)
    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    _ = json.loads(AUG_INDEX.read_text(encoding="utf-8"))  # provenance touch

    periods: dict[str, dict] = {}
    for name, ipath in (
        ("june", JUN_INDEX),
        ("july", JUL_INDEX),
        ("august", AUG_INDEX),
    ):
        periods[name] = load_period(name, ipath, symbols, spreads)

    # September descriptive window if tape exists
    sep_tape_raw = None
    if SEP_TAPE_PRIOR.is_file():
        sep_end = _ts("2026-09-25") + pd.Timedelta(hours=1)
        sep_start = _ts("2026-09-01")
        periods["september_todate"] = load_period(
            "september_todate",
            AUG_INDEX,
            symbols,
            spreads,
            entry_start=sep_start,
            end=sep_end,
        )
        sep_tape_raw = json.loads(SEP_TAPE_PRIOR.read_text(encoding="utf-8"))

    tapes_ctrl: dict[str, list] = {}
    tapes_trt: dict[str, list] = {}
    tape_stats: dict[str, Any] = {}
    for name, tpath in (("june", JUN_TAPE), ("july", JUL_TAPE), ("august", AUG_TAPE)):
        raw = json.loads(tpath.read_text(encoding="utf-8"))
        remapped = remap_tape(raw, periods[name]["data"])
        tapes_ctrl[name] = remapped
        tapes_trt[name] = filter_block_thursday_entries(remapped)
        tape_stats[name] = {
            "control": thursday_entry_stats(remapped),
            "treatment": thursday_entry_stats(tapes_trt[name]),
            "blocked": thursday_entry_stats(remapped)["thursday_n"],
        }
        print(
            f"  tape {name}: ctrl={len(remapped)} trt={len(tapes_trt[name])} "
            f"blocked_thu={tape_stats[name]['blocked']}",
            flush=True,
        )

    if sep_tape_raw is not None and "september_todate" in periods:
        remapped = remap_tape(sep_tape_raw, periods["september_todate"]["data"])
        tapes_ctrl["september_todate"] = remapped
        tapes_trt["september_todate"] = filter_block_thursday_entries(remapped)
        tape_stats["september_todate"] = {
            "control": thursday_entry_stats(remapped),
            "treatment": thursday_entry_stats(tapes_trt["september_todate"]),
        }

    # ---- STAGE A: June + August only (anti-leakage) ----
    ctrl_period: dict[str, dict] = {}
    trt_period: dict[str, dict] = {}
    ctrl_trades: dict[str, list] = {}
    trt_trades: dict[str, list] = {}

    for pname in ("june", "august"):
        print(f"=== CONTROL {pname} ===", flush=True)
        t0 = time.perf_counter()
        s, trades = run_replay(periods[pname], tapes_ctrl[pname])
        ctrl_period[pname] = s
        ctrl_trades[pname] = trades
        print(
            f"  ctrl {pname}: PF={s['PF']} mean={s['mean_trade']} net={s['net_pnl']} "
            f"fills={s['fills']} thu_fills={s['thursday_fills']} "
            f"{(time.perf_counter()-t0)/60:.1f}m",
            flush=True,
        )
        print(f"=== TREATMENT {pname} ===", flush=True)
        t0 = time.perf_counter()
        s, trades = run_replay(periods[pname], tapes_trt[pname])
        trt_period[pname] = s
        trt_trades[pname] = trades
        print(
            f"  trt  {pname}: PF={s['PF']} mean={s['mean_trade']} net={s['net_pnl']} "
            f"fills={s['fills']} thu_fills={s['thursday_fills']} "
            f"{(time.perf_counter()-t0)/60:.1f}m",
            flush=True,
        )

    ctrl_agg = aggregate_from_trades(
        [ctrl_trades["june"], ctrl_trades["august"]],
        intents=ctrl_period["june"]["intents"] + ctrl_period["august"]["intents"],
    )
    trt_agg = aggregate_from_trades(
        [trt_trades["june"], trt_trades["august"]],
        intents=trt_period["june"]["intents"] + trt_period["august"]["intents"],
    )
    # MDD: take max across periods from summaries
    ctrl_agg["MDD_max_across_periods"] = max(
        float(ctrl_period["june"]["MDD"]), float(ctrl_period["august"]["MDD"])
    )
    trt_agg["MDD_max_across_periods"] = max(
        float(trt_period["june"]["MDD"]), float(trt_period["august"]["MDD"])
    )
    retention = trt_agg["fills"] / max(1, ctrl_agg["fills"])
    qualified, sel_reasons = selection_ok(ctrl_agg, trt_agg)

    selection = {
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "Frozen BEFORE any July treatment evaluation",
        "hypothesis": HYPOTHESIS,
        "qualified": qualified,
        "reasons": sel_reasons,
        "fill_retention": retention,
        "trade_throughput_sensitive": retention < MIN_FILL_RETENTION,
        "control_aggregate_june_august": ctrl_agg,
        "treatment_aggregate_june_august": trt_agg,
        "delta_aggregate": {
            k: trt_agg[k] - ctrl_agg[k]
            for k in ("PF", "mean_trade", "net_pnl", "fills")
            if ctrl_agg.get(k) is not None and trt_agg.get(k) is not None
        },
        "by_period": {
            "june": {
                "control": ctrl_period["june"],
                "treatment": trt_period["june"],
                "delta": deltas(trt_period["june"], ctrl_period["june"]),
            },
            "august": {
                "control": ctrl_period["august"],
                "treatment": trt_period["august"],
                "delta": deltas(trt_period["august"], ctrl_period["august"]),
            },
        },
        "frozen_treatment": {
            "gate": "block_thursday_new_entries",
            "module": "borex.gate2.thursday_entry_gate.filter_block_thursday_entries",
            "control": CONTROL,
        },
    }
    (out / "june_august_selection.json").write_text(
        json.dumps(selection, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"SELECTION qualified={qualified} retention={retention:.3f} reasons={sel_reasons}",
        flush=True,
    )

    # ---- STAGE B: July (validation if qualified; else descriptive control-only optional) ----
    july_validation: dict[str, Any] = {
        "evaluated": False,
        "reason": None,
    }
    # Always run July control for record continuity; treatment only if qualified
    # to avoid using treatment July in any decision (already frozen).
    print("=== CONTROL july ===", flush=True)
    t0 = time.perf_counter()
    s_jul_c, trades_jul_c = run_replay(periods["july"], tapes_ctrl["july"])
    ctrl_period["july"] = s_jul_c
    ctrl_trades["july"] = trades_jul_c
    print(
        f"  ctrl july: PF={s_jul_c['PF']} mean={s_jul_c['mean_trade']} "
        f"net={s_jul_c['net_pnl']} {(time.perf_counter()-t0)/60:.1f}m",
        flush=True,
    )

    if qualified:
        print("=== TREATMENT july (frozen validation) ===", flush=True)
        t0 = time.perf_counter()
        s_jul_t, trades_jul_t = run_replay(periods["july"], tapes_trt["july"])
        trt_period["july"] = s_jul_t
        trt_trades["july"] = trades_jul_t
        print(
            f"  trt  july: PF={s_jul_t['PF']} mean={s_jul_t['mean_trade']} "
            f"net={s_jul_t['net_pnl']} {(time.perf_counter()-t0)/60:.1f}m",
            flush=True,
        )
        july_improved = (
            s_jul_t.get("PF") is not None
            and s_jul_c.get("PF") is not None
            and s_jul_t.get("mean_trade") is not None
            and s_jul_c.get("mean_trade") is not None
            and float(s_jul_t["PF"]) > float(s_jul_c["PF"])
            and float(s_jul_t["mean_trade"]) > float(s_jul_c["mean_trade"])
        )
        july_validation = {
            "evaluated": True,
            "qualified_from_june_august": True,
            "control": s_jul_c,
            "treatment": s_jul_t,
            "delta": deltas(s_jul_t, s_jul_c),
            "july_dual_improved_pf_mean": july_improved,
            "fill_retention": s_jul_t["fills"] / max(1, s_jul_c["fills"]),
            "classification": (
                "improved_versus_control" if july_improved else "degraded_versus_control"
                if (
                    float(s_jul_t["PF"]) < float(s_jul_c["PF"])
                    and float(s_jul_t["mean_trade"]) < float(s_jul_c["mean_trade"])
                )
                else "mixed"
            ),
        }
    else:
        july_validation = {
            "evaluated": False,
            "qualified_from_june_august": False,
            "reason": "NOT_SUPPORTED on June+August — July treatment not used for optimization",
            "control_descriptive": s_jul_c,
            "note": (
                "July control reported for record. Treatment July may be run "
                "descriptively below for complete research record only."
            ),
        }
        # Descriptive treatment July for complete record (explicitly not for selection)
        print("=== TREATMENT july (descriptive only; selection already failed) ===", flush=True)
        t0 = time.perf_counter()
        s_jul_t, trades_jul_t = run_replay(periods["july"], tapes_trt["july"])
        trt_period["july"] = s_jul_t
        trt_trades["july"] = trades_jul_t
        july_validation["treatment_descriptive_only"] = s_jul_t
        july_validation["delta_descriptive_only"] = deltas(s_jul_t, s_jul_c)
        print(
            f"  trt  july (desc): PF={s_jul_t['PF']} mean={s_jul_t['mean_trade']} "
            f"net={s_jul_t['net_pnl']} {(time.perf_counter()-t0)/60:.1f}m",
            flush=True,
        )

    (out / "july_validation.json").write_text(
        json.dumps(july_validation, indent=2, default=str), encoding="utf-8"
    )

    # ---- September descriptive ----
    sep_desc = {}
    if "september_todate" in tapes_ctrl:
        print("=== september_todate descriptive ===", flush=True)
        sc, _ = run_replay(periods["september_todate"], tapes_ctrl["september_todate"])
        st, _ = run_replay(periods["september_todate"], tapes_trt["september_todate"])
        ctrl_period["september_todate"] = sc
        trt_period["september_todate"] = st
        sep_desc = {"control": sc, "treatment": st, "delta": deltas(st, sc), "role": "descriptive_only"}

    # ---- Mechanism analysis ----
    mech_lines = [
        "# Mechanism Analysis — Thursday Entry Gate",
        "",
        "Research-only. Distinguishes harmful-Thursday vs trade-count artifacts.",
        "",
    ]
    mechanism: dict[str, Any] = {"by_period": {}}
    for pname in ("june", "august", "july"):
        c_thu = thursday_bucket(ctrl_trades[pname])
        t_thu = thursday_bucket(trt_trades[pname])
        c = ctrl_period[pname]
        t = trt_period[pname]
        removed_net = c_thu["net"]  # control Thursday P&L no longer taken
        # Positive P&L also removed = gross_profit on Thursday control
        mechanism["by_period"][pname] = {
            "control_thursday": c_thu,
            "treatment_thursday": t_thu,
            "control_non_thursday_mean": c.get("non_thursday_mean"),
            "treatment_non_thursday_mean": t.get("non_thursday_mean"),
            "control_non_thursday_net": c.get("non_thursday_net"),
            "treatment_non_thursday_net": t.get("non_thursday_net"),
            "pnl_removed_approx_control_thursday_net": removed_net,
            "positive_pnl_also_removed_gross": c_thu["gross_profit_removed"],
            "negative_pnl_removed_gross": c_thu["gross_loss_removed"],
            "fill_delta": t["fills"] - c["fills"],
            "net_delta": t["net_pnl"] - c["net_pnl"],
        }
        mech_lines.append(f"## {pname}")
        mech_lines.append("")
        mech_lines.append(
            f"- Control Thursday: n={c_thu['n']} net={c_thu['net']:.2f} "
            f"mean={c_thu['mean']} PF={c_thu['PF']} "
            f"TP={c_thu['TP']} DC={c_thu['daily_close']} MS={c_thu['margin_stop']}"
        )
        mech_lines.append(
            f"- Treatment Thursday fills: {t_thu['n']} (expected 0)"
        )
        mech_lines.append(
            f"- Non-Thursday mean: control={c.get('non_thursday_mean')} "
            f"treatment={t.get('non_thursday_mean')}"
        )
        mech_lines.append(
            f"- Approx P&L removed (control Thu net)={removed_net:.2f}; "
            f"gross profit also removed={c_thu['gross_profit_removed']:.2f}; "
            f"gross loss removed={c_thu['gross_loss_removed']:.2f}"
        )
        mech_lines.append(
            f"- Period net Δ (trt−ctrl)={t['net_pnl']-c['net_pnl']:.2f}; "
            f"fills Δ={t['fills']-c['fills']}"
        )
        mech_lines.append("")

    # Classification A vs B on June+August
    ja_ctrl_thu_net = (
        mechanism["by_period"]["june"]["control_thursday"]["net"]
        + mechanism["by_period"]["august"]["control_thursday"]["net"]
    )
    ja_net_delta = trt_agg["net_pnl"] - ctrl_agg["net_pnl"]
    non_thu_mean_shift = (
        (trt_agg.get("non_thursday_mean") or 0) - (ctrl_agg.get("non_thursday_mean") or 0)
    )
    if (
        abs(ja_net_delta - (-ja_ctrl_thu_net)) < abs(ja_ctrl_thu_net) * 0.35 + 5
        and abs(non_thu_mean_shift) < abs(ctrl_agg.get("non_thursday_mean") or 1) * 0.25 + 0.05
    ):
        mech_class = (
            "A_thursday_genuinely_harmful_on_sample — "
            "aggregate improvement ≈ removing control Thursday net with limited "
            "non-Thursday expectancy shift"
        )
    else:
        mech_class = (
            "B_or_mixed — improvement not well explained solely by Thursday removal "
            "(non-Thursday expectancy and/or portfolio path effects material)"
        )
    mechanism["june_august_mechanism_class"] = mech_class
    mechanism["june_august_thursday_net_control"] = ja_ctrl_thu_net
    mechanism["june_august_net_delta"] = ja_net_delta
    mech_lines.append("## June+August mechanism class")
    mech_lines.append("")
    mech_lines.append(f"- `{mech_class}`")
    mech_lines.append(f"- Control Thu net (Jun+Aug)={ja_ctrl_thu_net:.2f}")
    mech_lines.append(f"- Treatment−control net Δ={ja_net_delta:.2f}")
    mech_lines.append("")
    (out / "mechanism_analysis.md").write_text("\n".join(mech_lines), encoding="utf-8")
    (out / "mechanism_analysis.json").write_text(
        json.dumps(mechanism, indent=2, default=str), encoding="utf-8"
    )

    # ---- CSVs ----
    ctrl_rows = [flat_period_row("control", p, ctrl_period[p]) for p in ctrl_period]
    trt_rows = [flat_period_row("treatment", p, trt_period[p], deltas(trt_period[p], ctrl_period[p])) for p in trt_period]
    write_csv(out / "control_results.csv", ctrl_rows)
    write_csv(out / "treatment_results.csv", trt_rows)

    daily_metrics = []
    for p in ("june", "august", "july"):
        for arm, trades in (("control", ctrl_trades[p]), ("treatment", trt_trades[p])):
            by_day: dict[str, list[float]] = defaultdict(list)
            for t in trades:
                by_day[str(t["entry_time"])[:10]].append(float(t["pnl"]))
            for day, pnls in sorted(by_day.items()):
                daily_metrics.append(
                    {
                        "arm": arm,
                        "period": p,
                        "day": day,
                        "dow": _ts(day).dayofweek,
                        "n": len(pnls),
                        "net": sum(pnls),
                        "mean": statistics.mean(pnls),
                    }
                )
    write_csv(out / "daily_metrics.csv", daily_metrics)

    # RR=2.5 reference only
    rr25 = {"available": False}
    rr_path = PRIOR_RR / "conclusions.json"
    if rr_path.is_file():
        conc = json.loads(rr_path.read_text(encoding="utf-8"))
        hit = next(
            (v for v in (conc.get("validated") or []) if v.get("id") == "dyn_rmin2p5_rf0p9"),
            None,
        )
        rr25 = {
            "available": True,
            "status": "REFERENCE_ONLY_NOT_FOR_DEPLOYMENT",
            "config": {
                "ghost_sl_mult": 4.0,
                "rr_factor": 0.9,
                "rr_min": 2.5,
                "sides": "both",
                "leverage": 500.0,
            },
            "candidate_id": "dyn_rmin2p5_rf0p9",
            "by_period": (hit or {}).get("by_period"),
            "note": "Not combined with Thursday gate in this experiment.",
            "source": str(rr_path.as_posix()),
        }
    (out / "rr25_reference.json").write_text(
        json.dumps(rr25, indent=2, default=str), encoding="utf-8"
    )

    daily = expand_daily(out)

    # Hypothesis classification
    if not qualified:
        hyp_result = "not_supported"
        overall = "not_supported"
    else:
        jul_cls = july_validation.get("classification")
        if jul_cls == "improved_versus_control":
            hyp_result = "supported"
            overall = "supported"
        elif jul_cls == "degraded_versus_control":
            hyp_result = "not_supported"
            overall = "mixed"  # Jun+Aug improved, July degraded
        else:
            hyp_result = "mixed"
            overall = "mixed"

    # Stability: if Jun, Aug individually disagree
    jun_imp = (
        trt_period["june"]["PF"] > ctrl_period["june"]["PF"]
        and trt_period["june"]["mean_trade"] > ctrl_period["june"]["mean_trade"]
    )
    aug_imp = (
        trt_period["august"]["PF"] > ctrl_period["august"]["PF"]
        and trt_period["august"]["mean_trade"] > ctrl_period["august"]["mean_trade"]
    )
    if qualified and jun_imp != aug_imp:
        overall = "unstable"

    provenance = {
        "git_commit": _git(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "control": CONTROL,
        "gate_module": "borex.gate2.thursday_entry_gate",
        "universe_symbols": symbols,
        "universe_hash": universe_hash(symbols),
        "simulator": "frozen MultiMarketEngine H1→1m closest-sim — unmodified",
        "tape_stats": tape_stats,
        "sources": {
            "june_index": str(JUN_INDEX.as_posix()),
            "july_index": str(JUL_INDEX.as_posix()),
            "august_index": str(AUG_INDEX.as_posix()),
            "prior_forensic": str(PRIOR_JULY.as_posix()),
        },
        "may_excluded": True,
        "september_role": "descriptive_only",
    }
    (out / "data_provenance.json").write_text(
        json.dumps(provenance, indent=2, default=str), encoding="utf-8"
    )

    conclusions = {
        "hypothesis": HYPOTHESIS,
        "hypothesis_result": hyp_result,
        "overall_classification": overall,
        "june_august_qualified": qualified,
        "selection_reasons": sel_reasons,
        "fill_retention_june_august": retention,
        "trade_throughput_sensitive": retention < MIN_FILL_RETENTION,
        "june_control": ctrl_period["june"],
        "june_treatment": trt_period["june"],
        "august_control": ctrl_period["august"],
        "august_treatment": trt_period["august"],
        "july_validation": july_validation,
        "september_todate_descriptive": sep_desc,
        "mechanism_class": mech_class,
        "rr25_reference_status": rr25.get("status"),
        "mirror_consideration": (
            "Do NOT deploy. Research-only gate. Even if periods improve, "
            "requires additional OOS (closed September) before Mirror consideration."
        ),
        "daily_theory_broker": {
            "n_days": daily.get("n_days"),
            "n_trade_rows": daily.get("n_trade_rows"),
            "extra_rows_from_prior": daily.get("extra_rows_from_prior"),
        },
        "next_experiment": (
            "If supported/mixed: freeze Thursday gate + run closed-September OOS "
            "under established provenance (do not Mirror yet). "
            "If not_supported: do not iterate DOW set (Wed/Fri); either test the "
            "alternate authorized compound long∩NY-post gate as a NEW pre-registration, "
            "or wait for September N1@500 OOS. Do not combine with rr_min=2.5 yet."
        ),
        "implementation": {
            "type": "research_layer_decision_tape_filter",
            "function": "filter_block_thursday_entries",
            "production_alexg8_modified": False,
            "simulator_modified": False,
        },
    }
    (out / "conclusions.json").write_text(
        json.dumps(conclusions, indent=2, default=str), encoding="utf-8"
    )

    # report.md
    lines = [
        f"# Gate 2 Thursday Entry Gate — `{out.name}`",
        "",
        "Offline research-only. LIVE/Mirror untouched. Simulator/alexg8 unmodified.",
        "",
        "## 1. Pre-registered hypothesis",
        "",
        HYPOTHESIS,
        "",
        "## 2. Exact implementation",
        "",
        "- Module: `borex.gate2.thursday_entry_gate.filter_block_thursday_entries`",
        "- Blocks decision-tape intents with `time_unix` on Thursday UTC",
        "- Does not emit closes/flattens; does not alter exits or sizing/RR/costs",
        "",
        "## 3. June control vs treatment",
        "",
        f"- Control: PF={ctrl_period['june']['PF']} net={ctrl_period['june']['net_pnl']:.2f} "
        f"mean={ctrl_period['june']['mean_trade']} fills={ctrl_period['june']['fills']} "
        f"thu_fills={ctrl_period['june']['thursday_fills']}",
        f"- Treatment: PF={trt_period['june']['PF']} net={trt_period['june']['net_pnl']:.2f} "
        f"mean={trt_period['june']['mean_trade']} fills={trt_period['june']['fills']} "
        f"thu_fills={trt_period['june']['thursday_fills']}",
        "",
        "## 4. August control vs treatment",
        "",
        f"- Control: PF={ctrl_period['august']['PF']} net={ctrl_period['august']['net_pnl']:.2f} "
        f"mean={ctrl_period['august']['mean_trade']} fills={ctrl_period['august']['fills']}",
        f"- Treatment: PF={trt_period['august']['PF']} net={trt_period['august']['net_pnl']:.2f} "
        f"mean={trt_period['august']['mean_trade']} fills={trt_period['august']['fills']}",
        "",
        "## 5. June+August selection",
        "",
        f"- **qualified**: `{qualified}`",
        f"- Aggregate control PF={ctrl_agg['PF']} mean={ctrl_agg['mean_trade']} net={ctrl_agg['net_pnl']:.2f}",
        f"- Aggregate treatment PF={trt_agg['PF']} mean={trt_agg['mean_trade']} net={trt_agg['net_pnl']:.2f}",
        f"- Fill retention: **{retention:.3f}**",
        f"- Reasons: {sel_reasons}",
        "",
        "## 6–7. July validation",
        "",
        json.dumps(july_validation, indent=2, default=str)[:4000],
        "",
        "## 8. Trade-count retention",
        "",
        f"- June+August: {retention:.4f}",
        "",
        "## 9–10. Mechanism",
        "",
        f"- Class: `{mech_class}`",
        "- See `mechanism_analysis.md`",
        "",
        "## 11. rr_min=2.5 reference",
        "",
        f"- `{rr25.get('status')}` — not combined",
        "",
        "## 12. September-to-date",
        "",
        f"- {sep_desc if sep_desc else 'unavailable'}",
        "",
        "## 13. Theory ↔ Broker",
        "",
        f"- Days={daily.get('n_days')} rows={daily.get('n_trade_rows')} "
        f"extra={daily.get('extra_rows_from_prior')}",
        "",
        "## 14. Classification",
        "",
        f"- hypothesis_result: **{hyp_result}**",
        f"- overall: **{overall}**",
        "",
        "## 15. Next experiment",
        "",
        conclusions["next_experiment"],
        "",
        "No profitability/robustness claim. Do not Mirror-deploy.",
        "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")

    print("DONE", out, flush=True)
    print("QUALIFIED", qualified, "RESULT", hyp_result, "OVERALL", overall, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
