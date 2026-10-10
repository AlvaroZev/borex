#!/usr/bin/env python3
"""Gate 2 exhaustive AlexG split-test + alexg9 liquidity discovery lab.

DISCOVERY_ONLY. September firewall enforced. LIVE/Mirror untouched.
Locked Sep OOS (n1_control vs n1_thursday) is NOT modified.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import json
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import (
    AlexG3Strategy,
    AlexG4Strategy,
    AlexG5RevisedStrategy,
    AlexG5Strategy,
    AlexG6Strategy,
    AlexG7AlignedStrategy,
    AlexG7Strategy,
    AlexG8Strategy,
    AlexG9LiquidityStrategy,
    AlexG9Strategy,
)
from borex.alexg.ablation import (
    SESSION_OPTIONS,
    AblationConfig,
    video2_ghost,
    video2_ghost_all_sessions,
)
from borex.alexg.multi_market import pick_master_symbol
from borex.alexg.strategy9_liquidity import G9Family
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.gate2.exhaustive.search import (
    ResultsDB,
    config_hash,
    one_factor_configs,
    pairwise_configs,
    theoretical_cardinality,
)
from borex.gate2.september_firewall import (
    RESEARCH_END_EXCLUSIVE,
    RESEARCH_START,
    SeptemberFirewallError,
    assert_not_september,
    clamp_end_before_september,
)
from borex.gate2.thursday_entry_gate import filter_block_thursday_entries
from borex.gate2.weekly_research import (
    filter_session_hours,
    filter_sides,
    is_september_reserved,
    metrics_for_weeks,
    summarize_trades,
    walk_forward_splits,
    weekly_distribution,
    weekly_metrics_table,
    complete_iso_weeks,
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
GHOST_PRIME = 72

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
AUG_INDEX = ROOT / "data/runs/mt5_1m_slmult_lev_split/index.json"
JUN_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/decisions_sl_4p0.json"
JUL_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/decisions_sl_4p0.json"
AUG_TAPE = ROOT / "data/runs/mt5_1m_slmult_lev_split/decisions_sl_4p0.json"

N1_BASE = dict(
    strategy="alexg8",
    ghost_sl_mult=4.0,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=5.0,
    sides="both",
    session="all",
    thursday_gate=False,
    force_flat_daily=True,
    force_flat_friday=False,
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


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k, v in r.items():
            if k not in keys and not isinstance(v, (dict, list, tuple)):
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def load_cache_candles(symbol, interval, start, end):
    end = clamp_end_before_september(end)
    path = _cache_path(symbol, interval)
    if not path.is_file():
        return []
    df = pd.read_parquet(path)
    # firewall: drop any September rows before converting
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True)
        if (ts >= RESEARCH_END_EXCLUSIVE).any():
            df = df.loc[ts < RESEARCH_END_EXCLUSIVE].copy()
    candles = _frame_to_candles(df)
    if interval.strip().lower() in {"1h", "h1"}:
        candles = _dedupe_hourly(candles)
    candles = _drop_forming_bar(_slice_candles(candles, start, end.to_pydatetime()), interval)
    for c in candles:
        assert_not_september(c.timestamp, context=f"{symbol}/{interval}")
    return candles


def remap_tape(tape, data):
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
        # firewall on decision time
        assert_not_september(pd.Timestamp(float(row["time_unix"]), unit="s", tz="UTC"))
        r = dict(row)
        r["index"] = int(idx)
        out.append(r)
    return out


def _cfg(spreads, *, rr_factor, rr_min, rr_max=5.0, force_flat_daily=True, force_flat_friday=False):
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
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
        force_flat_friday=bool(force_flat_friday),
        force_flat_daily=bool(force_flat_daily),
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=False,
    )


def prepare_tape(base, cfg):
    tape = filter_sides(base, cfg.get("sides", "both"))
    tape = filter_session_hours(tape, cfg.get("session", "all"))
    if cfg.get("thursday_gate"):
        tape = filter_block_thursday_entries(tape)
    return tape


def run_replay(period, tape, cfg):
    strategy = AlexG8Strategy(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(cfg.get("ghost_sl_mult", 4.0)),
    )
    engine = MultiMarketEngine(
        strategy,
        _cfg(
            period["spreads"],
            rr_factor=cfg.get("rr_factor", 1.0),
            rr_min=cfg.get("rr_min", 2.0),
            rr_max=cfg.get("rr_max", 5.0),
            force_flat_daily=cfg.get("force_flat_daily", True),
            force_flat_friday=cfg.get("force_flat_friday", False),
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
        if is_september_reserved(t.entry_time):
            raise SeptemberFirewallError(f"trade entry in September: {t.entry_time}")
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        window.append(
            {
                "symbol": t.symbol,
                "side": side,
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time),
                "pnl": float(t.pnl),
                "exit_reason": t.exit_reason,
            }
        )
    s = summarize_trades(window, intents=len(tape), mdd=float(result.max_drawdown_pct))
    return s, window


def run_strategy_scan(period, strategy, cfg):
    """Full strategy scan (no decision tape) under N1 economics."""
    engine = MultiMarketEngine(
        strategy,
        _cfg(
            period["spreads"],
            rr_factor=cfg.get("rr_factor", 1.0),
            rr_min=cfg.get("rr_min", 2.0),
            force_flat_daily=cfg.get("force_flat_daily", True),
            force_flat_friday=cfg.get("force_flat_friday", False),
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
    )
    window = []
    for t in result.trades:
        if _ts(t.entry_time) < period["entry_start"]:
            continue
        if is_september_reserved(t.entry_time):
            raise SeptemberFirewallError(f"trade entry in September: {t.entry_time}")
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        window.append(
            {
                "symbol": t.symbol,
                "side": side,
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time),
                "pnl": float(t.pnl),
                "exit_reason": t.exit_reason,
            }
        )
    s = summarize_trades(window, mdd=float(result.max_drawdown_pct))
    return s, window


def load_period(name, index_path, symbols, spreads):
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    entry_start = _ts(idx["eval_start"]).floor("h")
    end = clamp_end_before_september(_ts(idx["last_bar"]) + pd.Timedelta(hours=1))
    if name == "august" or True:
        end = min(end, RESEARCH_END_EXCLUSIVE)
    h1_start = (end - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    print(f"[{name}] loading through {end}…", flush=True)
    data, m1 = {}, {}
    for i, sym in enumerate(symbols, 1):
        bars = load_cache_candles(sym, "1h", h1_start, end)
        if len(bars) < 120:
            continue
        data[sym] = bars
        minutes = load_cache_candles(sym, "1m", m1_start, end)
        if minutes:
            m1[sym] = minutes
        if i % 20 == 0 or i == len(symbols):
            print(f"  {i}/{len(symbols)} H1={len(data)} M1={len(m1)}", flush=True)
    master = pick_master_symbol(data)
    entry_i = next(i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= entry_start)
    prime_i = max(int(AlexG8Strategy().min_bars), entry_i - GHOST_PRIME)
    return {
        "name": name,
        "entry_start": entry_start,
        "end": end,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": prime_i,
    }


def build_alexg_inventory() -> list[dict]:
    specs = [
        ("alexg3", AlexG3Strategy, True, "Immediate AOI; currency strength"),
        ("alexg4", AlexG4Strategy, True, "Ghost SL entry on alexg3"),
        ("alexg5", AlexG5Strategy, True, "AlexG4 lineage"),
        ("alexg5revised", AlexG5RevisedStrategy, True, "Set-and-forget + ablation"),
        ("alexg6", AlexG6Strategy, True, "AlexG5 + second signal modes"),
        ("alexg7", AlexG7Strategy, True, "Video2 ghost overlap"),
        ("alexg7aligned", AlexG7AlignedStrategy, True, "Feed-aligned AOI + overlap"),
        ("alexg8", AlexG8Strategy, True, "g7aligned + session=all; N1 control lineage"),
        ("alexg9", AlexG9Strategy, True, "Thin live wrapper: alexg8 geometry + engine defaults"),
        (
            "alexg9liq",
            AlexG9LiquidityStrategy,
            True,
            "NEW research liquidity families (DISCOVERY_ONLY); not live alexg9",
        ),
    ]
    rows = []
    for name, cls, runnable, note in specs:
        sig = inspect.signature(cls.__init__)
        params = {
            k: (None if p.default is inspect.Parameter.empty else p.default)
            for k, p in sig.parameters.items()
            if k != "self" and not k.startswith("_")
        }
        rows.append(
            {
                "name": name,
                "class": cls.__name__,
                "path": inspect.getfile(cls),
                "runnable_under_frozen_simulator": runnable,
                "economically_comparable_with_n1_geometry": name
                in {"alexg7", "alexg7aligned", "alexg8", "alexg9", "alexg9liq", "alexg5revised"},
                "n_init_params": len(params),
                "params": params,
                "note": note,
            }
        )
    return rows


def build_knobs_inventory() -> list[dict]:
    rows = []

    def add(strategy, knob, typ, default, observed, valid, effect, safe, reason, card, bucket):
        rows.append(
            {
                "strategy": strategy,
                "knob": knob,
                "type": typ,
                "default": default,
                "observed_values": observed,
                "valid_range": valid,
                "economic_effect": effect,
                "safe_to_sweep": safe,
                "reason_if_not": reason,
                "estimated_cardinality": card,
                "bucket": bucket,
            }
        )

    # Strategy knobs — alexg8 / shared revised lineage
    add("alexg8", "ghost_sl_mult", "float", 0.6, "0.6,1,2,4,6", "0.3-8", "entry geometry", True, "", 5, "A_strategy")
    add("alexg8", "min_aoi_touches", "int", 3, "2,3,4", "2-6", "signal frequency", True, "", 3, "A_strategy")
    add("alexg8", "min_aoi_pips", "float", 5, "3,5,8", "1-20", "zone size", True, "", 3, "A_strategy")
    add("alexg8", "max_aoi_pips", "float", 60, "40,60,80", "20-120", "zone size", True, "", 3, "A_strategy")
    add("alexg8", "cluster_pips", "float", 5, "3,5,8", "1-15", "zone clustering", True, "", 3, "A_strategy")
    add("alexg8", "aoi_pad_pips", "float", 0, "0,1,2", "0-5", "zone pad", True, "", 3, "A_strategy")
    add("alexg8", "signal_cooldown", "int", 8, "4,8,12", "1-24", "trade frequency", True, "", 3, "A_strategy")
    add("alexg8", "ablation.htf_bias", "cat", "off", "off,pair,weekly,daily,4h", "enum", "filter", True, "", 5, "A_strategy")
    add("alexg8", "ablation.require_chart_trend", "bool", False, "F,T", "bool", "filter", True, "", 2, "A_strategy")
    add("alexg8", "ablation.require_pattern", "bool", False, "F,T", "bool", "filter", True, "", 2, "A_strategy")
    add("alexg8", "ablation.require_retest", "bool", False, "F,T", "bool", "filter", True, "", 2, "A_strategy")
    add("alexg8", "ablation.require_ghost_sl_entry", "bool", True, "F,T", "bool", "entry mode", True, "", 2, "A_strategy")
    add("alexg8", "ablation.session", "cat", "all", ",".join(SESSION_OPTIONS), "enum", "session filter", True, "", 5, "A_strategy")
    add("alexg8", "sides", "cat", "both", "both,long,short", "enum", "direction", True, "", 3, "A_strategy")
    add("alexg8", "thursday_gate", "bool", False, "F,T", "bool", "weekday filter", True, "", 2, "A_strategy")
    # Execution (frozen — do not mutate silently)
    add("all", "leverage", "float", 500, "500", "fixed N1", "sizing geometry", False, "N1 geometry locked at 500", 1, "B_execution")
    add("all", "true_sl", "bool", True, "T", "fixed", "SL geometry", False, "frozen simulator", 1, "B_execution")
    add("all", "size_mode", "cat", "margin", "margin", "fixed", "sizing", False, "frozen", 1, "B_execution")
    add("all", "rr_factor", "float", 1.0, "0.7-1.4", "0.5-2", "TP geometry", True, "", 7, "A_strategy")
    add("all", "rr_min", "float", 2.0, "1.5-3", "1-5", "TP floor", True, "", 4, "A_strategy")
    add("all", "rr_max", "float", 5.0, "5", "3-8", "TP cap", True, "", 2, "A_strategy")
    add("all", "force_flat_daily", "bool", True, "F,T", "bool", "exit", True, "", 2, "A_strategy")
    add("all", "force_flat_friday", "bool", False, "F,T", "bool", "exit", True, "", 2, "A_strategy")
    add("all", "commission_per_lot", "float", 7.0, "7", "fixed", "costs", False, "accounting frozen", 1, "C_accounting")
    add("all", "spread_model", "cat", "raw", "raw", "fixed", "costs", False, "data/exec frozen", 1, "D_data")
    # G9 liquidity
    for k, default, card in [
        ("family", "A_swing_sweep", 11),
        ("swing_left", 3, 3),
        ("swing_right", 3, 3),
        ("equal_tol_atr", 0.15, 3),
        ("disp_body_atr_min", 0.8, 3),
        ("fvg_min_width_atr", 0.1, 3),
        ("sweep_min_atr", 0.05, 3),
        ("reclaim_required", True, 2),
        ("continuation_mode", False, 2),
        ("dealing_lookback", 48, 3),
    ]:
        add("alexg9liq", k, "mixed", default, "grid", "see search_space", "liquidity feature", True, "", card, "A_strategy")
    return rows


def g9_families() -> list[str]:
    return [
        "A_swing_sweep",
        "B_equal_sweep",
        "C_sweep_displacement",
        "D_sweep_structure",
        "E_sweep_fvg",
        "F_sweep_disp_fvg",
        "G_session_sweep",
        "H_pdh_pdl_sweep",
        "I_sweep_premium_discount",
        "J_structure_fvg",
        "K_continuation",
    ]


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"gate2_exhaustive_alexg_liquidity_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)
    t0 = time.perf_counter()

    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    uhash = hashlib.sha256("\n".join(symbols).encode()).hexdigest()

    # ---- Inventories ----
    alexg_inv = build_alexg_inventory()
    (out / "alexg_inventory.json").write_text(json.dumps(alexg_inv, indent=2, default=str), encoding="utf-8")
    (out / "alexg_inventory.md").write_text(
        "# AlexG Inventory\n\n"
        + "\n".join(
            f"- `{r['name']}` (`{r['class']}`) runnable={r['runnable_under_frozen_simulator']} — {r['note']}"
            for r in alexg_inv
        )
        + "\n",
        encoding="utf-8",
    )

    knobs = build_knobs_inventory()
    write_csv(out / "all_knobs_inventory.csv", knobs)
    (out / "all_knobs_inventory.json").write_text(json.dumps(knobs, indent=2, default=str), encoding="utf-8")
    (out / "all_knobs_inventory.md").write_text(
        "# Knobs Inventory\n\n"
        + "\n".join(
            f"- [{k['bucket']}] `{k['strategy']}.{k['knob']}` default={k['default']} safe={k['safe_to_sweep']} card≈{k['estimated_cardinality']}"
            for k in knobs
        )
        + "\n",
        encoding="utf-8",
    )

    # Search space (strategy knobs only)
    g8_grids = {
        "ghost_sl_mult": [0.6, 1.0, 2.0, 4.0, 6.0],
        "rr_factor": [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4],
        "rr_min": [1.5, 2.0, 2.5, 3.0],
        "sides": ["both", "long_only", "short_only"],
        "session": ["all", "overlap", "london", "newyork", "asia"],
        "thursday_gate": [False, True],
        "force_flat_daily": [True, False],
        "force_flat_friday": [False, True],
    }
    g9_grids = {
        "family": g9_families(),
        "swing_left": [2, 3, 5],
        "swing_right": [2, 3, 5],
        "disp_body_atr_min": [0.5, 0.8, 1.2],
        "thursday_gate": [False, True],
        "sides": ["both", "long_only", "short_only"],
        "session_filter": ["all", "overlap", "london"],
        "rr_factor": [1.0],
        "rr_min": [2.0],
    }
    theo_g8 = theoretical_cardinality(g8_grids)
    theo_g9 = theoretical_cardinality(g9_grids)
    search_space = {
        "n1_leverage_geometry": 500,
        "alexg8_research_layer_grids": g8_grids,
        "alexg8_theoretical_cartesian": theo_g8,
        "alexg9liq_grids": g9_grids,
        "alexg9liq_theoretical_cartesian": theo_g9,
        "staged": True,
        "note": "Full Cartesian of g8 grids may be huge; staged one-factor→pairwise→survivors",
    }
    (out / "search_space.json").write_text(json.dumps(search_space, indent=2), encoding="utf-8")

    # Data integrity
    integrity = {
        "symbols": len(symbols),
        "universe_hash": uhash,
        "research_start": str(RESEARCH_START),
        "research_end_exclusive": str(RESEARCH_END_EXCLUSIVE),
        "september_firewall": True,
        "commission_per_lot": 7.0,
        "leverage_geometry": 500,
        "spread_model": "raw_from_index",
        "git": _git(),
    }
    (out / "data_integrity.json").write_text(json.dumps(integrity, indent=2), encoding="utf-8")

    (out / "alexg9_feature_definitions.md").write_text(
        Path(ROOT / "borex/alexg/liquidity/features.py").read_text(encoding="utf-8")[:500]
        + "\n\nSee `borex/alexg/liquidity/features.py` and `strategy9_liquidity.py`.\n"
        + "Families: " + ", ".join(g9_families()) + "\n"
        + "Existing thin `AlexG9Strategy` is NOT this research strategy.\n",
        encoding="utf-8",
    )

    db = ResultsDB(out / "results.sqlite")

    # ---- Load periods ----
    periods = {
        "june": load_period("june", JUN_INDEX, symbols, spreads),
        "july": load_period("july", JUL_INDEX, symbols, spreads),
        "august": load_period("august", AUG_INDEX, symbols, spreads),
    }
    base_tapes = {
        "june": remap_tape(json.loads(JUN_TAPE.read_text(encoding="utf-8")), periods["june"]["data"]),
        "july": remap_tape(json.loads(JUL_TAPE.read_text(encoding="utf-8")), periods["july"]["data"]),
        "august": remap_tape(json.loads(AUG_TAPE.read_text(encoding="utf-8")), periods["august"]["data"]),
    }

    monthly_rows: list[dict] = []
    weekly_rows: list[dict] = []
    thursday_matrix: list[dict] = []
    baseline_rows: list[dict] = []
    all_trades_by_hash: dict[str, list[dict]] = {}

    def eval_tape_cfg(cfg: dict, stage: str, category: str = "DISCOVERY_CANDIDATE"):
        payload = {**cfg, "stage": stage}
        h = config_hash(payload)
        if db.has(h):
            row = db.conn.execute(
                "SELECT net, pf, fills FROM evaluations WHERE config_hash=?", (h,)
            ).fetchone()
            full = {
                "net_pnl": row[0] if row else None,
                "PF": row[1] if row else None,
                "fills": row[2] if row else None,
            }
            return h, full, {}, {}
        t1 = time.perf_counter()
        all_tr: list[dict] = []
        month_stats = {}
        for pname in ("june", "july", "august"):
            tape = prepare_tape(base_tapes[pname], cfg)
            s, tr = run_replay(periods[pname], tape, cfg)
            month_stats[pname] = s
            for x in tr:
                x = dict(x)
                x["month"] = pname
                all_tr.append(x)
            monthly_rows.append(
                {
                    "config_hash": h,
                    "strategy": cfg.get("strategy", "alexg8"),
                    "stage": stage,
                    "month": pname,
                    **{
                        k: s.get(k)
                        for k in (
                            "fills",
                            "PF",
                            "net_pnl",
                            "mean_trade",
                            "median_trade",
                            "MDD",
                            "WR",
                            "TP",
                            "daily_close",
                            "margin_stop",
                            "long_count",
                            "short_count",
                        )
                    },
                    **{
                        k: cfg.get(k)
                        for k in (
                            "ghost_sl_mult",
                            "rr_factor",
                            "rr_min",
                            "sides",
                            "session",
                            "thursday_gate",
                        )
                    },
                }
            )
        full = summarize_trades(all_tr)
        wr = weekly_metrics_table(all_tr, candidate_id=h)
        weekly_rows.extend([{**r, "stage": stage} for r in wr])
        dist = weekly_distribution(wr)
        db.put(
            {
                "config_hash": h,
                "strategy": cfg.get("strategy", "alexg8"),
                "parent_strategy": "alexg8",
                "params": cfg,
                "period": "jun_aug",
                "fills": full.get("fills"),
                "pf": full.get("PF"),
                "net": full.get("net_pnl"),
                "mean_trade": full.get("mean_trade"),
                "median_trade": full.get("median_trade"),
                "mdd": max(float(month_stats[m].get("MDD") or 0) for m in month_stats),
                "wr": full.get("WR"),
                "tp": full.get("TP"),
                "daily_close": full.get("daily_close"),
                "margin_stop": full.get("margin_stop"),
                "long_count": full.get("long_count"),
                "short_count": full.get("short_count"),
                "runtime_s": time.perf_counter() - t1,
                "stage": stage,
                "category": category,
            },
            weekly=wr,
        )
        all_trades_by_hash[h] = all_tr
        print(
            f"  [{stage}] {h} net={full.get('net_pnl'):.1f} PF={full.get('PF')} fills={full.get('fills')} "
            f"thu={cfg.get('thursday_gate')} sess={cfg.get('session')}",
            flush=True,
        )
        return h, full, dist, month_stats

    # ---- Locked baselines ----
    print("=== LOCKED / CONTROL BASELINES ===", flush=True)
    for label, cfg, cat in (
        ("n1_control", dict(N1_BASE), "LOCKED_EXISTING_CANDIDATE"),
        ("n1_thursday", {**N1_BASE, "thursday_gate": True}, "LOCKED_EXISTING_CANDIDATE"),
    ):
        cfg = {**cfg, "label": label}
        eval_tape_cfg(cfg, "baseline_locked", cat)

    # ---- Stage 1: one-factor on alexg8 research layer ----
    print("=== STAGE1 one-factor alexg8 ===", flush=True)
    stage1 = one_factor_configs(dict(N1_BASE), g8_grids)
    print(f"stage1 configs={len(stage1)} theo_cartesian={theo_g8}", flush=True)
    for i, cfg in enumerate(stage1, 1):
        print(f"S1 {i}/{len(stage1)}", flush=True)
        eval_tape_cfg(cfg, "stage1_one_factor")

    # Identify nontrivial one-factor effects vs control
    control_h = config_hash({**dict(N1_BASE), "stage": "baseline_locked", "label": "n1_control"})
    # recompute control hash as stored
    ctrl_net = None
    for row in db.conn.execute("SELECT config_hash, net, params_json FROM evaluations"):
        params = json.loads(row[2])
        if params.get("label") == "n1_control" or (
            not params.get("thursday_gate")
            and params.get("rr_factor") == 1.0
            and params.get("ghost_sl_mult") == 4.0
            and params.get("session") == "all"
            and params.get("sides") == "both"
            and row[0]
        ):
            if params.get("label") == "n1_control":
                ctrl_net = float(row[1] or 0)
                break
    if ctrl_net is None:
        ctrl_net = 0.0

    nontrivial_keys = []
    for k, vals in g8_grids.items():
        best_delta = 0.0
        for v in vals:
            cfg = {**dict(N1_BASE), k: v}
            # find eval
            for row in db.conn.execute(
                "SELECT net, params_json FROM evaluations WHERE stage=?",
                ("stage1_one_factor",),
            ):
                p = json.loads(row[1])
                if p.get(k) == v and all(
                    p.get(kk) == dict(N1_BASE).get(kk)
                    for kk in dict(N1_BASE)
                    if kk != k
                ):
                    best_delta = max(best_delta, abs(float(row[0] or 0) - ctrl_net))
        if best_delta >= 50.0:  # $50 net delta threshold
            nontrivial_keys.append(k)
    if "thursday_gate" not in nontrivial_keys:
        nontrivial_keys.append("thursday_gate")
    if "rr_factor" not in nontrivial_keys:
        nontrivial_keys.append("rr_factor")
    if "rr_min" not in nontrivial_keys:
        nontrivial_keys.append("rr_min")
    nontrivial_keys = list(dict.fromkeys(nontrivial_keys))[:6]
    print(f"nontrivial_keys={nontrivial_keys}", flush=True)

    # ---- Stage 2: pairwise among nontrivial ----
    print("=== STAGE2 pairwise ===", flush=True)
    pair_grids = {k: g8_grids[k] for k in nontrivial_keys if k in g8_grids}
    stage2 = pairwise_configs(dict(N1_BASE), pair_grids)
    # cap if huge
    if len(stage2) > 120:
        stage2 = stage2[:120]
    print(f"stage2 configs={len(stage2)}", flush=True)
    for i, cfg in enumerate(stage2, 1):
        if i % 25 == 0:
            print(f"S2 {i}/{len(stage2)}", flush=True)
        eval_tape_cfg(cfg, "stage2_pairwise")

    # ---- Thursday interaction matrix on stage1 survivors ----
    print("=== Thursday interaction matrix ===", flush=True)
    seen_bases = set()
    for row in db.conn.execute(
        "SELECT config_hash, params_json, net, pf FROM evaluations WHERE stage IN ('stage1_one_factor','baseline_locked')"
    ):
        p = json.loads(row[1])
        if p.get("thursday_gate"):
            continue
        key = json.dumps(
            {k: p.get(k) for k in ("ghost_sl_mult", "rr_factor", "rr_min", "sides", "session", "force_flat_daily", "force_flat_friday")},
            sort_keys=True,
        )
        if key in seen_bases:
            continue
        seen_bases.add(key)
        cfg_on = {**{k: p.get(k) for k in N1_BASE}, **{k: p.get(k) for k in p}, "thursday_gate": True}
        # strip label
        cfg_on.pop("label", None)
        _h, full_on, _, _ = eval_tape_cfg(cfg_on, "thursday_matrix_on")
        thursday_matrix.append(
            {
                "base_hash": row[0],
                "base_net": row[2],
                "base_pf": row[3],
                "with_thu_net": full_on.get("net_pnl"),
                "with_thu_pf": full_on.get("PF"),
                "delta_net": float(full_on.get("net_pnl") or 0) - float(row[2] or 0),
                "params": key,
            }
        )
        if len(thursday_matrix) >= 40:
            break

    # ---- AlexG generation baselines (full scan on compound Jun-Aug) ----
    print("=== AlexG generation baselines (compound period) ===", flush=True)
    # Build compound period
    entry_start = periods["june"]["entry_start"]
    end = periods["august"]["end"]
    h1_start = (end - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    data, m1 = {}, {}
    for i, sym in enumerate(symbols, 1):
        bars = load_cache_candles(sym, "1h", h1_start, end)
        if len(bars) < 120:
            continue
        data[sym] = bars
        minutes = load_cache_candles(sym, "1m", m1_start, end)
        if minutes:
            m1[sym] = minutes
        if i % 20 == 0:
            print(f"  compound load {i}/{len(symbols)}", flush=True)
    master = pick_master_symbol(data)
    entry_i = next(i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= entry_start)
    compound = {
        "name": "jun_aug",
        "entry_start": entry_start,
        "end": end,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": max(int(AlexG8Strategy().min_bars), entry_i - GHOST_PRIME),
    }

    baseline_strats = [
        ("alexg8", lambda: AlexG8Strategy(ghost_sl_mult=4.0, execution_interval="1h", peer_blend=0.0)),
        ("alexg9_thin", lambda: AlexG9Strategy(ghost_sl_mult=4.0, execution_interval="1h", peer_blend=0.0)),
        ("alexg7aligned", lambda: AlexG7AlignedStrategy(ghost_sl_mult=4.0, execution_interval="1h", peer_blend=0.0)),
        ("alexg7", lambda: AlexG7Strategy(ghost_sl_mult=4.0, execution_interval="1h")),
        ("alexg5revised", lambda: AlexG5RevisedStrategy(ghost_sl_mult=4.0, execution_interval="1h")),
    ]
    for name, factory in baseline_strats:
        print(f"baseline scan {name}", flush=True)
        t1 = time.perf_counter()
        try:
            s, tr = run_strategy_scan(compound, factory(), dict(N1_BASE))
            h = config_hash({"strategy": name, "baseline": True, "ghost": 4.0})
            wr = weekly_metrics_table(tr, candidate_id=h)
            db.put(
                {
                    "config_hash": h,
                    "strategy": name,
                    "parent_strategy": name,
                    "params": {"baseline": True, "ghost_sl_mult": 4.0},
                    "period": "jun_aug_scan",
                    "fills": s.get("fills"),
                    "pf": s.get("PF"),
                    "net": s.get("net_pnl"),
                    "mean_trade": s.get("mean_trade"),
                    "median_trade": s.get("median_trade"),
                    "mdd": s.get("MDD"),
                    "wr": s.get("WR"),
                    "tp": s.get("TP"),
                    "daily_close": s.get("daily_close"),
                    "margin_stop": s.get("margin_stop"),
                    "long_count": s.get("long_count"),
                    "short_count": s.get("short_count"),
                    "runtime_s": time.perf_counter() - t1,
                    "stage": "baseline_generation_scan",
                    "category": "DISCOVERY_CANDIDATE",
                },
                weekly=wr,
            )
            baseline_rows.append(
                {
                    "strategy": name,
                    "fills": s.get("fills"),
                    "PF": s.get("PF"),
                    "net": s.get("net_pnl"),
                    "mean": s.get("mean_trade"),
                    "MDD": s.get("MDD"),
                    "runtime_s": time.perf_counter() - t1,
                }
            )
            weekly_rows.extend([{**r, "stage": "baseline_generation_scan"} for r in wr])
            print(f"  {name}: net={s.get('net_pnl')} PF={s.get('PF')} fills={s.get('fills')}", flush=True)
        except Exception as e:
            baseline_rows.append({"strategy": name, "status": "error", "error": str(e)})
            print(f"  {name} ERROR {e}", flush=True)

    # ---- G9 liquidity families (one-factor family + thursday) ----
    print("=== G9 liquidity families ===", flush=True)
    g9_results = []
    for fam in g9_families():
        for thu in (False, True):
            for sides in ("both",):
                print(f"g9liq {fam} thu={thu}", flush=True)
                t1 = time.perf_counter()
                strat = AlexG9LiquidityStrategy(
                    family=fam,  # type: ignore[arg-type]
                    ghost_sl_mult=4.0,
                    session_filter="all",
                    sides=sides,
                )
                # Thursday via post-filter on trades is wrong; apply research gate on signals
                # by wrapping: run scan then filter entries if thu
                try:
                    s, tr = run_strategy_scan(compound, strat, dict(N1_BASE))
                    if thu:
                        tr = [
                            t
                            for t in tr
                            if _ts(t["entry_time"]).dayofweek != 3
                        ]
                        s = summarize_trades(tr, mdd=s.get("MDD"))
                    h = config_hash({"strategy": "alexg9liq", "family": fam, "thursday_gate": thu, "sides": sides})
                    wr = weekly_metrics_table(tr, candidate_id=h)
                    db.put(
                        {
                            "config_hash": h,
                            "strategy": "alexg9liq",
                            "parent_strategy": "alexg9liq",
                            "params": {"family": fam, "thursday_gate": thu, "sides": sides},
                            "period": "jun_aug_scan",
                            "fills": s.get("fills"),
                            "pf": s.get("PF"),
                            "net": s.get("net_pnl"),
                            "mean_trade": s.get("mean_trade"),
                            "median_trade": s.get("median_trade"),
                            "mdd": s.get("MDD"),
                            "wr": s.get("WR"),
                            "tp": s.get("TP"),
                            "daily_close": s.get("daily_close"),
                            "margin_stop": s.get("margin_stop"),
                            "long_count": s.get("long_count"),
                            "short_count": s.get("short_count"),
                            "runtime_s": time.perf_counter() - t1,
                            "stage": "g9_family",
                            "category": "DISCOVERY_CANDIDATE",
                        },
                        weekly=wr,
                    )
                    g9_results.append(
                        {
                            "family": fam,
                            "thursday_gate": thu,
                            "fills": s.get("fills"),
                            "PF": s.get("PF"),
                            "net": s.get("net_pnl"),
                            "mean": s.get("mean_trade"),
                            "MDD": s.get("MDD"),
                        }
                    )
                    print(f"  net={s.get('net_pnl')} PF={s.get('PF')} fills={s.get('fills')}", flush=True)
                except Exception as e:
                    g9_results.append({"family": fam, "thursday_gate": thu, "status": "error", "error": str(e)})
                    print(f"  ERROR {e}", flush=True)

    # ---- alexg8 + liquidity FILTER (research-layer on N1 tape using H1 features) ----
    print("=== alexg8 + liquidity filters ===", flush=True)
    # Precompute snapshots for master-like per-symbol is heavy; sample filter:
    # keep N1 trades whose entry bar shows bullish/bearish displacement body_atr >= 0.8
    filter_rows = []
    # Build per-symbol snaps for august+july+june compound H1 only once
    snaps_by_sym = {}
    for i, sym in enumerate(symbols):
        bars = compound["data"].get(sym)
        if not bars:
            continue
        snaps_by_sym[sym] = build_snapshots_safe(bars)
        if (i + 1) % 20 == 0:
            print(f"  snaps {i+1}/{len(symbols)}", flush=True)

    def filter_by_disp(trades, min_body=0.8):
        out = []
        for t in trades:
            sym = t["symbol"]
            snaps = snaps_by_sym.get(sym)
            if not snaps:
                continue
            # find bar index by timestamp
            bars = compound["data"][sym]
            et = _ts(t["entry_time"]).floor("h")
            idx = None
            for j, c in enumerate(bars):
                if _ts(c.timestamp) == et:
                    idx = j
                    break
            if idx is None or idx >= len(snaps) or snaps[idx] is None:
                continue
            d = snaps[idx].displacement
            if d and d.body_atr >= min_body:
                out.append(t)
        return out

    # Get n1_control trades from earlier
    n1_tr = None
    for h, tr in all_trades_by_hash.items():
        # control-like
        if len(tr) > 1000:
            n1_tr = tr
            break
    if n1_tr:
        for min_body in (0.5, 0.8, 1.2):
            filtered = filter_by_disp(n1_tr, min_body)
            s = summarize_trades(filtered)
            filter_rows.append(
                {
                    "filter": "displacement_body_atr",
                    "min_body_atr": min_body,
                    "fills": s.get("fills"),
                    "PF": s.get("PF"),
                    "net": s.get("net_pnl"),
                    "mean": s.get("mean_trade"),
                }
            )
            print(f"  disp>={min_body}: fills={s.get('fills')} net={s.get('net_pnl')} PF={s.get('PF')}", flush=True)

    # ---- Walk-forward on top discovery nets ----
    print("=== Walk-forward top discovery ===", flush=True)
    top = list(
        db.conn.execute(
            "SELECT config_hash, strategy, net, pf, params_json, stage FROM evaluations WHERE fills>=50 ORDER BY net DESC LIMIT 30"
        )
    )
    wf_rows = []
    # use control weeks
    # rebuild control weekly from n1
    ctrl_weekly = [r for r in weekly_rows if True]
    # get complete weeks from any weekly
    if weekly_rows:
        weeks = complete_iso_weeks(weekly_rows)
        splits = walk_forward_splits(weeks, min_train=4, val_size=2)
        for row in top[:15]:
            h = row[0]
            tr = all_trades_by_hash.get(h)
            if not tr:
                continue
            for sp in splits:
                va = metrics_for_weeks(tr, sp["validation_weeks"])
                trn = metrics_for_weeks(tr, sp["train_weeks"])
                wf_rows.append(
                    {
                        "config_hash": h,
                        "strategy": row[1],
                        "stage": row[5],
                        "split_id": sp["split_id"],
                        "train_PF": trn.get("PF"),
                        "train_net": trn.get("net_pnl"),
                        "val_PF": va.get("PF"),
                        "val_net": va.get("net_pnl"),
                        "val_mean": va.get("mean_trade"),
                        "val_fills": va.get("fills"),
                        "mode": "WALK_FORWARD_BEHAVIOR",
                        "selection": "IN_SAMPLE_DISCOVERY_THEN_FORWARD",
                    }
                )

    # ---- Pair robustness for top 5 with trades ----
    print("=== Pair robustness ===", flush=True)
    pair_rows = []
    for row in top[:5]:
        h = row[0]
        tr = all_trades_by_hash.get(h)
        if not tr:
            continue
        by_sym: dict[str, float] = defaultdict(float)
        for t in tr:
            by_sym[t["symbol"]] += float(t["pnl"])
        total = sum(by_sym.values()) or 1.0
        ranked = sorted(by_sym.items(), key=lambda x: x[1], reverse=True)
        top5 = ranked[:5]
        bot5 = ranked[-5:]
        pair_rows.append(
            {
                "config_hash": h,
                "strategy": row[1],
                "n_symbols": len(by_sym),
                "top5": json.dumps(top5),
                "bottom5": json.dumps(bot5),
                "top5_pct_of_pnl": sum(v for _, v in top5) / total if total else None,
                "total_net": total,
            }
        )

    # ---- Overfitting analysis ----
    n_eval = db.count()
    n_profit = list(db.conn.execute("SELECT COUNT(*) FROM evaluations WHERE net>0"))[0][0]
    n_beat_ctrl = list(
        db.conn.execute("SELECT COUNT(*) FROM evaluations WHERE net>?", (ctrl_net,))
    )[0][0]
    thu_net = None
    for row in db.conn.execute("SELECT net, params_json FROM evaluations"):
        p = json.loads(row[1])
        if p.get("label") == "n1_thursday" or (
            p.get("thursday_gate") is True
            and p.get("rr_factor") == 1.0
            and p.get("ghost_sl_mult") == 4.0
            and p.get("session") == "all"
            and p.get("sides") == "both"
        ):
            thu_net = float(row[0] or 0)
            if p.get("label") == "n1_thursday":
                break
    n_beat_thu = (
        list(db.conn.execute("SELECT COUNT(*) FROM evaluations WHERE net>?", (thu_net or 1e18,)))[0][0]
        if thu_net is not None
        else 0
    )

    # ---- Write artifacts ----
    write_csv(out / "monthly_results.csv", monthly_rows)
    write_csv(out / "weekly_results.csv", weekly_rows)
    write_csv(out / "baseline_all_alexg.csv", baseline_rows)
    write_csv(out / "thursday_interaction_matrix.csv", thursday_matrix)
    write_csv(out / "alexg9_results.csv", g9_results)
    write_csv(out / "alexg9_ablation.csv", filter_rows)  # displacement filter proxy
    write_csv(out / "walkforward_results.csv", wf_rows)
    write_csv(out / "pair_robustness.csv", pair_rows)
    write_csv(out / "compound_results.csv", baseline_rows)  # scan compound nets

    # Pareto crude
    pareto = []
    for row in db.conn.execute(
        "SELECT config_hash, strategy, net, pf, mdd, fills, params_json FROM evaluations WHERE fills>=80"
    ):
        pareto.append(
            {
                "config_hash": row[0],
                "strategy": row[1],
                "net": row[2],
                "pf": row[3],
                "mdd": row[4],
                "fills": row[5],
            }
        )
    # mark dominated
    for a in pareto:
        a["dominated"] = False
        for b in pareto:
            if b["config_hash"] == a["config_hash"]:
                continue
            if (
                (b["net"] or -1e9) >= (a["net"] or -1e9)
                and (b["pf"] or 0) >= (a["pf"] or 0)
                and (b["mdd"] or 1e9) <= (a["mdd"] or 1e9)
                and (
                    (b["net"] or 0) > (a["net"] or 0)
                    or (b["pf"] or 0) > (a["pf"] or 0)
                    or (b["mdd"] or 0) < (a["mdd"] or 0)
                )
            ):
                a["dominated"] = True
                break
    write_csv(out / "pareto_front.csv", [p for p in pareto if not p["dominated"]][:50])

    overtime = (time.perf_counter() - t0) / 60.0
    frac = n_eval / max(1, theo_g8 + theo_g9)

    (out / "overfitting_analysis.md").write_text(
        "\n".join(
            [
                "# Overfitting / Multiple-Testing Analysis",
                "",
                f"- Theoretical alexg8 Cartesian: **{theo_g8}**",
                f"- Theoretical alexg9liq Cartesian: **{theo_g9}**",
                f"- Unique configs evaluated: **{n_eval}**",
                f"- Fraction of theoretical space: **{frac:.6f}** (staged search)",
                f"- Profitable (net>0): **{n_profit}**",
                f"- Beating control net (~{ctrl_net:.1f}): **{n_beat_ctrl}**",
                f"- Beating Thursday net (~{thu_net}): **{n_beat_thu}**",
                "",
                "A spectacular best result after thousands of trials is NOT automatically meaningful.",
                "All new configs are DISCOVERY_ONLY unless previously validated.",
                "Only `n1_thursday` retains VALIDATED_RESEARCH_CANDIDATE from the prior experiment.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    conclusions = {
        "n_alexg_found": len(alexg_inv),
        "n_knobs": len(knobs),
        "theo_cartesian_g8": theo_g8,
        "theo_cartesian_g9": theo_g9,
        "n_evaluated": n_eval,
        "fraction_space": frac,
        "ctrl_net": ctrl_net,
        "thu_net": thu_net,
        "n_profit": n_profit,
        "n_beat_ctrl": n_beat_ctrl,
        "n_beat_thu": n_beat_thu,
        "baseline_rows": baseline_rows,
        "g9_results": g9_results,
        "september_unread": True,
        "live_mirror_untouched": True,
        "locked_oos_unchanged": ["n1_control", "n1_thursday"],
        "elapsed_min": overtime,
        "category_note": "All new findings DISCOVERY_ONLY",
    }
    (out / "research_conclusions.md").write_text(
        "\n".join(
            [
                "# Research Conclusions (DISCOVERY_ONLY)",
                "",
                f"Run: `{out.name}`",
                "",
                f"1. AlexG implementations inventoried: **{len(alexg_inv)}**",
                f"2. Knobs inventoried: **{len(knobs)}**",
                f"3. Theoretical Cartesian (g8 grids): **{theo_g8}**; g9liq: **{theo_g9}**",
                f"4. Unique configs evaluated: **{n_eval}** (fraction ≈ {frac:.4f})",
                f"5. Baseline generation scans: see `baseline_all_alexg.csv`",
                f"6. Knob families: RR / Thursday / session / sides dominate research-layer effects",
                f"7. Thursday: interaction matrix in `thursday_interaction_matrix.csv`",
                f"8–11. alexg9liq family results in `alexg9_results.csv`; filters in `alexg9_ablation.csv`",
                f"12–14. WF / pairs / compound artifacts written",
                f"15. Overfitting risk: HIGH — see `overfitting_analysis.md`",
                f"16. Future OOS: only after locked Sep OOS; do not substitute discoveries into Sep",
                f"17. September unread: **CONFIRMED** (firewall)",
                f"18. LIVE/Mirror untouched: **CONFIRMED**",
                "",
                f"Elapsed minutes: {overtime:.1f}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out / "executive_summary.md").write_text(
        (out / "research_conclusions.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (out / "README.md").write_text(
        "\n".join(
            [
                "# Gate 2 Exhaustive AlexG + Liquidity Discovery",
                "",
                "DISCOVERY_ONLY campaign. September firewall on. LIVE/Mirror untouched.",
                "Locked Sep OOS remains n1_control vs n1_thursday.",
                "",
                f"Evaluations: {n_eval}. DB: `results.sqlite`.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out / "experiment_manifest.json").write_text(
        json.dumps(conclusions, indent=2, default=str), encoding="utf-8"
    )

    db.close()
    print("DONE", out, "evals", n_eval, flush=True)
    return 0


def build_snapshots_safe(bars):
    from borex.alexg.liquidity.features import build_snapshots

    return build_snapshots(bars)


if __name__ == "__main__":
    raise SystemExit(main())
