#!/usr/bin/env python3
"""Gate 2 weekly strategy expansion — offline, September reserved.

Uses only Jun/Jul/Aug 2026 fully covered H1+M1 utc_v2 data for research.
September is RESERVED for locked OOS (not used for selection).
M1 broker depth ~60-90d → ≥12 months NOT available via authorized pipeline.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
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
from borex.alexg.ablation import AblationConfig, video2_ghost_all_sessions
from borex.alexg.multi_market import pick_master_symbol
from borex.alexg.strategy7_aligned import AlexG7AlignedStrategy
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.gate2.daily_theory_broker import (
    aggregate_day,
    build_daily_dataset,
    write_daily_artifacts,
)
from borex.gate2.thursday_entry_gate import filter_block_thursday_entries
from borex.gate2.weekly_research import (
    compound_label,
    filter_session_hours,
    filter_sides,
    iso_week_key,
    is_september_reserved,
    metrics_for_weeks,
    rolling_week_nets,
    summarize_trades,
    walk_forward_splits,
    weekly_distribution,
    weekly_metrics_table,
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
PRIOR_EXT = ROOT / "data/runs/research/mt5_historical_extension_20260925_050221"
PRIOR_THU = ROOT / "data/runs/research/gate2_thursday_gate_20260925_195229"
PRIOR_DAILY = PRIOR_THU / "daily_theory_broker.json"
CENSUS = (
    ROOT
    / "data/runs/gate2_fidelity_assessment/runs/g2a_20260924_130000/master_trade_census.json"
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


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


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
    path = _cache_path(symbol, interval)
    if not path.is_file():
        return []
    df = pd.read_parquet(path)
    candles = _frame_to_candles(df)
    if interval.strip().lower() in {"1h", "h1"}:
        candles = _dedupe_hourly(candles)
    return _drop_forming_bar(_slice_candles(candles, start, end), interval)


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
        r = dict(row)
        r["index"] = int(idx)
        out.append(r)
    return out


def _cfg(spreads, *, rr_factor, rr_min, rr_max=5.0) -> BacktestConfig:
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


def run_replay(period, tape, *, rr_factor, rr_min, ghost=4.0):
    strategy = AlexG8Strategy(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(ghost),
    )
    engine = MultiMarketEngine(
        strategy, _cfg(period["spreads"], rr_factor=rr_factor, rr_min=rr_min), max_positions=0
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
            continue  # hard reserve even if leaked into window
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
                "margin": float(getattr(t, "margin", 0) or 0),
            }
        )
    s = summarize_trades(
        window,
        intents=len(tape),
        mdd=float(result.max_drawdown_pct),
        starting_equity=CAPITAL,
    )
    return s, window


def load_period(name, index_path, symbols, spreads):
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    entry_start = _ts(idx["eval_start"]).floor("h")
    end = _ts(idx["last_bar"]) + pd.Timedelta(hours=1)
    # Clamp August end before September
    if name == "august":
        end = min(end, _ts("2026-09-01"))
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


def as_utc_index(ts):
    idx = pd.DatetimeIndex(pd.to_datetime(ts, utc=True))
    return idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")


def build_inventory(symbols: list[str]) -> dict[str, Any]:
    def scan(interval: str):
        rows = []
        for sym in symbols:
            p = _cache_path(sym, interval)
            if not p.is_file():
                rows.append({"symbol": sym, "n": 0, "tmin": None, "tmax": None})
                continue
            df = pd.read_parquet(p, columns=["timestamp"])
            ts = as_utc_index(df["timestamp"])
            rows.append(
                {
                    "symbol": sym,
                    "n": int(len(ts)),
                    "tmin": str(ts.min()),
                    "tmax": str(ts.max()),
                    "dupes": int(pd.Series(ts).duplicated().sum()),
                }
            )
        tmins = [r["tmin"] for r in rows if r["tmin"]]
        tmaxs = [r["tmax"] for r in rows if r["tmax"]]
        return {
            "files": sum(1 for r in rows if r["n"] > 0),
            "earliest": min(tmins) if tmins else None,
            "latest": max(tmaxs) if tmaxs else None,
            "min_n": min(r["n"] for r in rows),
            "max_n": max(r["n"] for r in rows),
            "dupes_total": sum(r.get("dupes") or 0 for r in rows),
            "rows": rows,
        }

    h1, m1 = scan("1h"), scan("1m")
    return {
        "assessed_at_utc": datetime.now(timezone.utc).isoformat(),
        "provenance": "data/cache/mt5/utc_v2 via MT5 copy_rates (established pipeline)",
        "h1": {k: v for k, v in h1.items() if k != "rows"},
        "m1": {k: v for k, v in m1.items() if k != "rows"},
        "h1_rows": h1["rows"],
        "m1_rows": m1["rows"],
        "broker_depth_note": (
            "Prior Mirror probe (mt5_historical_extension_20260925_050221): "
            "IC Markets M1 terminal depth ~60-90 days; Jan–May 2026 M1 not recoverable."
        ),
        "ge12_months_m1_h1_available": False,
        "usable_strategy_months_excluding_sep": ["2026-06", "2026-07", "2026-08"],
        "september_reserved": True,
    }


def coverage_matrices(symbols: list[str]) -> tuple[list[dict], list[dict]]:
    monthly = []
    weekly = []
    # months from 2025-06 through 2026-09
    months = pd.period_range("2025-06", "2026-09", freq="M")
    for per in months:
        start = per.to_timestamp(how="start").tz_localize("UTC")
        end = (per + 1).to_timestamp(how="start").tz_localize("UTC")
        h1_ok = m1_ok = 0
        h1_gaps = m1_gaps = 0
        for sym in symbols:
            for tf, counter in (("1h", "h1"), ("1m", "m1")):
                p = _cache_path(sym, tf)
                if not p.is_file():
                    continue
                df = pd.read_parquet(p, columns=["timestamp"])
                ts = as_utc_index(df["timestamp"])
                sl = ts[(ts >= start) & (ts < end)]
                if len(sl) == 0:
                    continue
                if tf == "1h":
                    h1_ok += 1
                else:
                    m1_ok += 1
                if len(sl) >= 2:
                    delta = pd.Timedelta("1h" if tf == "1h" else "1min")
                    gaps = int((pd.Series(sl).diff().dropna() > delta * 1.5).sum())
                    if tf == "1h":
                        h1_gaps += gaps
                    else:
                        m1_gaps += gaps
        y, m = per.year, per.month
        usable = (
            h1_ok == 60
            and m1_ok == 60
            and (y, m) != (2026, 9)
            and (y, m) >= (2026, 6)
        )
        reserved = (y, m) == (2026, 9)
        monthly.append(
            {
                "month": f"{y}-{m:02d}",
                "H1_60_60": h1_ok == 60,
                "M1_60_60": m1_ok == 60,
                "H1_count": h1_ok,
                "M1_count": m1_ok,
                "H1_gaps_sum": h1_gaps,
                "M1_gaps_sum": m1_gaps,
                "source": "mt5_utc_v2",
                "normalization": "utc_v2",
                "continuous": h1_ok == 60 and m1_ok == 60,
                "usable_for_strategy_research": usable,
                "september_reserved": reserved,
                "note": (
                    "RESERVED_OOS"
                    if reserved
                    else ("USABLE" if usable else "NOT_USABLE_MISSING_M1_OR_OUTSIDE_WINDOW")
                ),
            }
        )

    # weekly for Jun–Aug only
    weeks = pd.date_range("2026-06-01", "2026-08-31", freq="W-MON", tz="UTC")
    for ws in weeks:
        we = ws + pd.Timedelta(days=7)
        h1_ok = m1_ok = 0
        for sym in symbols:
            for tf in ("1h", "1m"):
                p = _cache_path(sym, tf)
                if not p.is_file():
                    continue
                df = pd.read_parquet(p, columns=["timestamp"])
                ts = as_utc_index(df["timestamp"])
                sl = ts[(ts >= ws) & (ts < we)]
                if len(sl) == 0:
                    continue
                if tf == "1h":
                    h1_ok += 1
                else:
                    m1_ok += 1
        weekly.append(
            {
                "week_start": str(ws.date()),
                "week": iso_week_key(ws),
                "H1_60_60": h1_ok == 60,
                "M1_60_60": m1_ok == 60,
                "H1_count": h1_ok,
                "M1_count": m1_ok,
                "fully_covered": h1_ok == 60 and m1_ok == 60,
                "usable_for_strategy_research": h1_ok == 60 and m1_ok == 60,
                "september_excluded": True,
            }
        )
    return monthly, weekly


def parameter_inventory() -> dict[str, Any]:
    s8 = AlexG8Strategy()
    s7 = AlexG7AlignedStrategy()
    abl = s8.ablation
    items = [
        {"family": "A_SIGNAL", "name": "min_aoi_touches", "default": s8.min_aoi_touches, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "min_aoi_pips", "default": s8.min_aoi_pips, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "max_aoi_pips", "default": s8.max_aoi_pips, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "cluster_pips", "default": s8.cluster_pips, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "aoi_pad_pips", "default": s8.aoi_pad_pips, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "signal_cooldown", "default": s8.signal_cooldown, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "ablation.htf_bias", "default": abl.htf_bias, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "ablation.require_chart_trend", "default": abl.require_chart_trend, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "ablation.require_pattern", "default": abl.require_pattern, "affects": "entry"},
        {"family": "A_SIGNAL", "name": "ablation.require_retest", "default": abl.require_retest, "affects": "entry"},
        {"family": "B_ENTRY", "name": "ghost_sl_mult", "default": 4.0, "n1": 4.0, "affects": "entry"},
        {"family": "B_ENTRY", "name": "ablation.require_ghost_sl_entry", "default": abl.require_ghost_sl_entry, "affects": "entry"},
        {"family": "B_ENTRY", "name": "sides", "default": "both", "affects": "entry"},
        {"family": "B_ENTRY", "name": "thursday_entry_gate", "default": False, "frozen_candidate": True, "affects": "entry"},
        {"family": "C_RR", "name": "rr_mode", "default": "dynamic", "affects": "exit_geometry"},
        {"family": "C_RR", "name": "rr_factor", "default": 1.0, "affects": "exit_geometry"},
        {"family": "C_RR", "name": "rr_min", "default": 2.0, "affects": "exit_geometry"},
        {"family": "C_RR", "name": "rr_max", "default": 5.0, "affects": "exit_geometry"},
        {"family": "D_RISK", "name": "leverage", "default": 500.0, "note": "geometry coupled via true_sl", "affects": "sizing+geometry"},
        {"family": "D_RISK", "name": "position_size_pct", "default": 0.01, "affects": "sizing"},
        {"family": "D_RISK", "name": "size_mode", "default": "margin", "affects": "sizing"},
        {"family": "E_SESSION", "name": "ablation.session", "default": abl.session, "alexg7": s7.ablation.session, "affects": "entry"},
        {"family": "E_SESSION", "name": "force_flat_daily", "default": True, "affects": "exit"},
        {"family": "E_SESSION", "name": "force_flat_friday", "default": False, "affects": "exit"},
        {"family": "F_UNIVERSE", "name": "60_pair_universe", "default": "frozen", "affects": "entry"},
    ]
    return {
        "strategy": "alexg8",
        "note": "Do not invent new indicators; research uses existing levers + research-layer gates",
        "items": items,
        "n_parameters": len(items),
    }


def build_variant_manifest() -> list[dict[str, Any]]:
    """Controlled one/two-factor library (not full Cartesian)."""
    rows: list[dict[str, Any]] = []

    def add(family, cid, **kw):
        cfg = dict(CONTROL)
        cfg.update(kw)
        rows.append({"candidate_id": cid, "family": family, **cfg})

    # Family A — N1
    add("A_N1", "n1_control")
    # Family F — Thursday frozen candidate
    add("F_THURSDAY", "n1_thursday", thursday_gate=True)
    # Family D — RR factor (one-factor)
    for rf in (0.7, 0.8, 0.9, 1.1, 1.2, 1.4):
        add("D_RR", f"rrf_{str(rf).replace('.', 'p')}", rr_factor=rf)
    # Family D — RR min
    for rm in (1.5, 2.5, 3.0):
        add("D_RR", f"rmin_{str(rm).replace('.', 'p')}", rr_min=rm)
    # Family G — direction
    add("G_DIRECTION", "long_only", sides="long_only")
    add("G_DIRECTION", "short_only", sides="short_only")
    # Family E — session (research-layer hour filter)
    for sess in ("overlap", "london", "newyork", "asia"):
        add("E_SESSION", f"sess_{sess}", session=sess)
    # Family B — time gate interactions with N1 (thursday already F)
    # Family H — combinations of individually interesting gates (small)
    add("H_COMBO", "thu_rrf0p9", thursday_gate=True, rr_factor=0.9)
    add("H_COMBO", "thu_rmin2p5", thursday_gate=True, rr_min=2.5)
    add("H_COMBO", "thu_overlap", thursday_gate=True, session="overlap")
    add("H_COMBO", "rrf0p9_rmin2p5", rr_factor=0.9, rr_min=2.5)
    return rows


def prepare_tape(base: list[dict], cfg: dict) -> list[dict]:
    tape = filter_sides(base, cfg["sides"])
    tape = filter_session_hours(tape, cfg["session"])
    if cfg.get("thursday_gate"):
        tape = filter_block_thursday_entries(tape)
    return tape


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
        "extra_rows_from_prior": extra,
        "trade_rows": rows,
        "daily_aggregates": daily,
        "n_days": len(daily),
        "n_trade_rows": len(rows),
        "limitations": base["limitations"],
    }
    write_daily_artifacts(dataset, out_dir)
    (out_dir / "daily_theory_broker.json").write_text(
        json.dumps(dataset, indent=2, default=str), encoding="utf-8"
    )
    return dataset


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"gate2_weekly_strategy_expansion_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "equity_curves").mkdir(exist_ok=True)
    (out / "candidate_reports").mkdir(exist_ok=True)
    print(f"OUT {out}", flush=True)

    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    uhash = universe_hash(symbols)

    # ---- PART 1–4 inventory / coverage ----
    inv = build_inventory(symbols)
    (out / "data_inventory.md").write_text(
        "\n".join(
            [
                "# Data Inventory",
                "",
                f"Assessed: {inv['assessed_at_utc']}",
                "",
                f"- Provenance: `{inv['provenance']}`",
                f"- H1 earliest/latest: `{inv['h1']['earliest']}` → `{inv['h1']['latest']}` ({inv['h1']['files']}/60)",
                f"- M1 earliest/latest: `{inv['m1']['earliest']}` → `{inv['m1']['latest']}` ({inv['m1']['files']}/60)",
                f"- ≥12 months M1+H1 available: **{inv['ge12_months_m1_h1_available']}**",
                f"- Broker depth: {inv['broker_depth_note']}",
                f"- Usable strategy months (ex-Sep): `{inv['usable_strategy_months_excluding_sep']}`",
                f"- September reserved: **{inv['september_reserved']}**",
                "",
                "Weekly batches are smaller chronological evaluation units; adjacent weeks may remain correlated.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out / "data_inventory.json").write_text(json.dumps(inv, indent=2), encoding="utf-8")

    monthly, weekly_cov = coverage_matrices(symbols)
    write_csv(out / "coverage_monthly.csv", monthly)
    write_csv(out / "coverage_weekly.csv", weekly_cov)

    (out / "data_provenance.md").write_text(
        "\n".join(
            [
                "# Data Provenance",
                "",
                "source: IC Markets MT5 Mirror `copy_rates`",
                "→ raw: MT5 rates",
                "→ normalization: `borex.viewerMT5.mt5_feed` utc_v2 parquet",
                "→ validation: 60-pair universe + gap/dupe scans",
                "→ research-ready: closest-sim H1 signal / 1m exit",
                "",
                f"universe_hash: `{uhash}`",
                "No Yahoo / HistData / vendor mix.",
                "September 2026 reserved from selection even if present in cache.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out / "historical_extension_report.md").write_text(
        "\n".join(
            [
                "# Historical Extension Report",
                "",
                "No new extension executed in this campaign.",
                f"Prior probe: `{PRIOR_EXT.as_posix()}`",
                "",
                "## Can we get ≥12 months?",
                "",
                "**No** via authorized broker pipeline.",
                "IC Markets M1 depth ≈ 60–90 days; Jan–May 2026 M1 returned empty in prior Mirror probe.",
                "H1 alone exists further back (~2025-09+ for 60/60) but is NOT comparable for N1 closest-sim without M1 → marked **NOT_COMPARABLE** for primary strategy research.",
                "",
                "Oldest usable strategy date: **2026-06-01** (M1+H1).",
                "Newest usable strategy date for research: **2026-08-31** (Sep reserved).",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out / "september_reservation.md").write_text(
        "\n".join(
            [
                "# September Reservation",
                "",
                "September 2026 is LOCKED for the pre-registered OOS:",
                "- CONTROL: N1@500",
                "- TREATMENT: N1@500 + Thursday gate",
                "",
                "This weekly expansion campaign does **not** use September for:",
                "optimization, candidate generation, parameter selection, or shortlisting.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    inv_params = parameter_inventory()
    (out / "strategy_parameter_inventory.md").write_text(
        "# Strategy Parameter Inventory\n\n"
        + "\n".join(
            f"- `{it['family']}` / `{it['name']}` default=`{it.get('default')}` affects=`{it.get('affects')}`"
            for it in inv_params["items"]
        )
        + f"\n\nn_parameters={inv_params['n_parameters']}\n",
        encoding="utf-8",
    )
    (out / "strategy_parameter_inventory.json").write_text(
        json.dumps(inv_params, indent=2), encoding="utf-8"
    )

    variants = build_variant_manifest()
    write_csv(out / "strategy_variant_manifest.csv", variants)
    print(f"Variants: {len(variants)}", flush=True)

    # ---- Load periods (Jun/Jul/Aug only) ----
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

    all_weekly: list[dict] = []
    candidate_summaries: list[dict] = []
    trades_by_cand: dict[str, list[dict]] = {}
    monthly_by_cand: dict[str, dict[str, dict]] = {}

    t_campaign = time.perf_counter()
    for i, cfg in enumerate(variants, 1):
        cid = cfg["candidate_id"]
        print(f"=== [{i}/{len(variants)}] {cid} family={cfg['family']} ===", flush=True)
        all_trades: list[dict] = []
        month_stats: dict[str, dict] = {}
        for pname in ("june", "july", "august"):
            tape = prepare_tape(base_tapes[pname], cfg)
            s, trades = run_replay(
                periods[pname],
                tape,
                rr_factor=cfg["rr_factor"],
                rr_min=cfg["rr_min"],
                ghost=cfg["ghost_sl_mult"],
            )
            month_stats[pname] = s
            for t in trades:
                t2 = dict(t)
                t2["month"] = pname
                all_trades.append(t2)
            print(
                f"  {pname}: PF={s.get('PF')} mean={s.get('mean_trade')} "
                f"net={s.get('net_pnl'):.1f} fills={s.get('fills')}",
                flush=True,
            )
        trades_by_cand[cid] = all_trades
        monthly_by_cand[cid] = month_stats
        week_rows = weekly_metrics_table(all_trades, candidate_id=cid)
        all_weekly.extend(week_rows)
        dist = weekly_distribution(week_rows)
        # exceptional-week flag: one week > 60% of total abs positive contribution
        nets = [float(r["net_pnl"]) for r in week_rows]
        pos = [x for x in nets if x > 0]
        exceptional = False
        if pos and sum(pos) > 0:
            exceptional = max(pos) / sum(pos) >= 0.60
        # fixed-equity (sum of monthly nets with $1000 restart each month)
        fixed_net = sum(float(month_stats[m]["net_pnl"]) for m in month_stats)
        fixed_mdd = max(float(month_stats[m]["MDD"] or 0) for m in month_stats)
        candidate_summaries.append(
            {
                "candidate_id": cid,
                "family": cfg["family"],
                "rr_factor": cfg["rr_factor"],
                "rr_min": cfg["rr_min"],
                "sides": cfg["sides"],
                "session": cfg["session"],
                "thursday_gate": cfg["thursday_gate"],
                "june_PF": month_stats["june"].get("PF"),
                "july_PF": month_stats["july"].get("PF"),
                "august_PF": month_stats["august"].get("PF"),
                "june_net": month_stats["june"].get("net_pnl"),
                "july_net": month_stats["july"].get("net_pnl"),
                "august_net": month_stats["august"].get("net_pnl"),
                "june_mean": month_stats["june"].get("mean_trade"),
                "july_mean": month_stats["july"].get("mean_trade"),
                "august_mean": month_stats["august"].get("mean_trade"),
                "fixed_equity_net_jun_jul_aug": fixed_net,
                "fixed_equity_max_month_MDD": fixed_mdd,
                "total_fills": sum(int(month_stats[m]["fills"]) for m in month_stats),
                "exceptional_single_week_dominated": exceptional,
                **{f"weekly_{k}": v for k, v in dist.items()},
            }
        )
        # per-candidate short report
        (out / "candidate_reports" / f"{cid}.json").write_text(
            json.dumps(
                {"config": cfg, "months": month_stats, "weekly_distribution": dist},
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    write_csv(out / "all_weekly_results.csv", all_weekly)
    write_csv(out / "candidate_families.csv", candidate_summaries)

    # ---- Walk-forward ----
    # Use control weeks as calendar; same weeks for all
    ctrl_weeks = sorted({r["week"] for r in all_weekly if r["candidate_id"] == "n1_control"})
    splits = walk_forward_splits(ctrl_weeks, min_train=4, val_size=2)
    wf_rows = []
    for cfg in variants:
        cid = cfg["candidate_id"]
        trades = trades_by_cand[cid]
        for sp in splits:
            tr = metrics_for_weeks(trades, sp["train_weeks"])
            va = metrics_for_weeks(trades, sp["validation_weeks"])
            wf_rows.append(
                {
                    "candidate_id": cid,
                    "family": cfg["family"],
                    "split_id": sp["split_id"],
                    "train_n": sp["train_n"],
                    "val_n": sp["val_n"],
                    "train_weeks": "|".join(sp["train_weeks"]),
                    "validation_weeks": "|".join(sp["validation_weeks"]),
                    "train_PF": tr.get("PF"),
                    "train_mean": tr.get("mean_trade"),
                    "train_net": tr.get("net_pnl"),
                    "val_PF": va.get("PF"),
                    "val_mean": va.get("mean_trade"),
                    "val_net": va.get("net_pnl"),
                    "val_MDD": va.get("MDD"),
                    "val_fills": va.get("fills"),
                }
            )
    write_csv(out / "walk_forward_results.csv", wf_rows)

    # Survive multiple forward windows: val PF>1 and val mean>0 on ≥2 splits
    survive = []
    by_c = defaultdict(list)
    for r in wf_rows:
        by_c[r["candidate_id"]].append(r)
    for cid, rs in by_c.items():
        wins = [
            r
            for r in rs
            if r.get("val_PF") is not None
            and r.get("val_mean") is not None
            and float(r["val_PF"]) > 1.0
            and float(r["val_mean"]) > 0.0
        ]
        survive.append(
            {
                "candidate_id": cid,
                "family": next(v["family"] for v in variants if v["candidate_id"] == cid),
                "n_splits": len(rs),
                "n_surviving_val_windows": len(wins),
                "survives_multi_forward": len(wins) >= 2,
                "val_nets": [r.get("val_net") for r in rs],
            }
        )
    write_csv(out / "walk_forward_survival.csv", survive)

    # ---- Compound simulations (sequential months, single continuous run) ----
    # Compound = one replay spanning concatenated chronology with capital continuity
    # Implement by chaining: run june, then july with starting equity = ending, etc.
    # Engine always starts at CAPITAL — approximate compound by scaling is WRONG.
    # Instead: merge periods into one continuous dataset for Jun→Aug.

    def load_compound(month_names: list[str]) -> dict:
        """Build continuous period from first month start through last month end."""
        first, last = month_names[0], month_names[-1]
        entry_start = periods[first]["entry_start"]
        end = periods[last]["end"]
        h1_start = (end - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
        m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
        end_py = end.to_pydatetime()
        print(f"[compound {'+'.join(month_names)}] loading…", flush=True)
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
        # stitch tapes
        tape = []
        for m in month_names:
            raw = base_tapes[m]
            # re-remap onto compound data
            tape.extend(remap_tape(raw, data))
        # dedupe by (symbol,time_unix)
        seen = set()
        uniq = []
        for r in sorted(tape, key=lambda x: (x["time_unix"], x["symbol"])):
            k = (r["symbol"], int(float(r["time_unix"])))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(r)
        return {
            "name": "+".join(month_names),
            "entry_start": entry_start,
            "end": end,
            "data": data,
            "m1": m1,
            "spreads": spreads,
            "master": master,
            "prime_i": max(int(AlexG8Strategy().min_bars), entry_i - GHOST_PRIME),
            "tape": uniq,
            "months": month_names,
        }

    compound_spans = [
        (["june", "july"], "compound_2m"),
        (["july", "august"], "compound_2m_b"),
        (["june", "july", "august"], "compound_3m"),
    ]
    # shortlist for compound: control, thursday, top fixed-equity, survivors
    ranked = sorted(
        candidate_summaries,
        key=lambda r: (
            float(r.get("weekly_median_weekly_net") or -1e9),
            float(r.get("fixed_equity_net_jun_jul_aug") or -1e9),
        ),
        reverse=True,
    )
    shortlist_ids = {"n1_control", "n1_thursday"}
    for r in ranked[:5]:
        shortlist_ids.add(r["candidate_id"])
    for r in survive:
        if r["survives_multi_forward"]:
            shortlist_ids.add(r["candidate_id"])
    shortlist = [v for v in variants if v["candidate_id"] in shortlist_ids]
    write_csv(
        out / "shortlisted_candidates.csv",
        [c for c in candidate_summaries if c["candidate_id"] in shortlist_ids],
    )

    compound_results: dict[str, list[dict]] = {
        "compound_2m": [],
        "compound_2m_b": [],
        "compound_3m": [],
        "compound_6m": [],
        "compound_12m": [],
    }
    # 6m/12m unavailable
    compound_results["compound_6m"].append(
        {"status": "unavailable", "reason": "M1 history < 6 months under utc_v2 broker depth"}
    )
    compound_results["compound_12m"].append(
        {"status": "unavailable", "reason": "M1 history < 12 months under utc_v2 broker depth"}
    )

    compound_periods = {}
    for months, key in compound_spans:
        compound_periods[key] = load_compound(months)

    for cfg in shortlist:
        cid = cfg["candidate_id"]
        for key, cper in compound_periods.items():
            tape = prepare_tape(cper["tape"], cfg)
            print(f"=== COMPOUND {key} {cid} ===", flush=True)
            s, trades = run_replay(
                cper, tape, rr_factor=cfg["rr_factor"], rr_min=cfg["rr_min"]
            )
            # equity curve sample
            eq = CAPITAL
            curve = []
            for t in sorted(trades, key=lambda x: x["entry_time"]):
                eq += float(t["pnl"])
                curve.append({"time": t["entry_time"], "equity": eq, "pnl": t["pnl"]})
            write_csv(out / "equity_curves" / f"{key}_{cid}.csv", curve)
            # non-compound reference = sum of constituent monthly fixed resets
            months = cper["months"]
            fixed = sum(
                float(monthly_by_cand[cid][m]["net_pnl"])
                for m in months
                if cid in monthly_by_cand
            )
            compound_results[key].append(
                {
                    "candidate_id": cid,
                    "family": cfg["family"],
                    "months": "|".join(months),
                    "compound_net": s.get("net_pnl"),
                    "compound_PF": s.get("PF"),
                    "compound_mean": s.get("mean_trade"),
                    "compound_MDD": s.get("MDD"),
                    "compound_fills": s.get("fills"),
                    "compound_ending_equity": s.get("ending_equity"),
                    "compound_margin_stops": s.get("margin_stop"),
                    "fixed_equity_sum_net": fixed,
                    "delta_compound_minus_fixed": (
                        float(s.get("net_pnl") or 0) - float(fixed)
                    ),
                    "thursday_fills": s.get("thursday_fills"),
                }
            )

    for k, rows in compound_results.items():
        write_csv(out / f"{k}.csv", rows)

    # ---- Scoreboard ----
    scoreboard = []
    for c in candidate_summaries:
        cid = c["candidate_id"]
        surv = next(x for x in survive if x["candidate_id"] == cid)
        wf = [r for r in wf_rows if r["candidate_id"] == cid]
        scoreboard.append(
            {
                **{k: c[k] for k in c if not isinstance(c[k], (dict, list))},
                "n_surviving_val_windows": surv["n_surviving_val_windows"],
                "survives_multi_forward": surv["survives_multi_forward"],
                "mean_val_net": (
                    statistics.mean([float(r["val_net"]) for r in wf if r.get("val_net") is not None])
                    if wf
                    else None
                ),
            }
        )
    write_csv(out / "weekly_oos_scoreboard.csv", scoreboard)

    daily = expand_daily(out)

    # Carry-forward candidates (raw metrics, not deployment)
    carry = [
        c
        for c in scoreboard
        if c["candidate_id"] in {"n1_control", "n1_thursday"}
        or (
            c.get("survives_multi_forward")
            and not c.get("exceptional_single_week_dominated")
            and float(c.get("weekly_median_weekly_net") or -1) > 0
        )
    ]
    write_csv(out / "candidates_carry_forward.csv", carry)

    provenance = {
        "git_commit": _git(),
        "universe_hash": uhash,
        "simulator": "frozen MultiMarketEngine H1→1m closest-sim",
        "capital": CAPITAL,
        "leverage": LEV,
        "september_reserved": True,
        "ge12_months": False,
        "n_variants": len(variants),
        "n_families": len({v["family"] for v in variants}),
        "elapsed_min": (time.perf_counter() - t_campaign) / 60.0,
    }
    (out / "data_provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    (out / "simulator_fingerprint.json").write_text(
        json.dumps(
            {
                "engine": "MultiMarketEngine",
                "frozen": True,
                "true_sl": True,
                "size_mode": "margin",
                "path": "h1_signal_1m_exit",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "universe_hash.txt").write_text(uhash + "\n", encoding="utf-8")

    conclusions = {
        "available_history": {
            "h1": inv["h1"],
            "m1": inv["m1"],
            "ge12_months": False,
            "oldest_usable": "2026-06-01",
            "newest_usable_research": "2026-08-31",
            "september_reserved": True,
        },
        "n_parameters": inv_params["n_parameters"],
        "n_variants": len(variants),
        "n_families": len({v["family"] for v in variants}),
        "walk_forward_splits": splits,
        "survivors": [s for s in survive if s["survives_multi_forward"]],
        "carry_forward_candidate_ids": [c["candidate_id"] for c in carry],
        "compound_availability": {
            "2m": True,
            "3m": True,
            "6m": False,
            "12m": False,
        },
        "theory_broker": {
            "n_days": daily.get("n_days"),
            "n_trade_rows": daily.get("n_trade_rows"),
        },
        "next_experiments": [
            "After Sep closes: locked OOS N1 vs N1+Thursday only",
            "Do not expand DOW search before Sep OOS",
            "Do not combine rr_min=2.5 with Thursday before Sep OOS",
            "If broker later exposes deeper M1 archive via authorized channel, re-inventory",
        ],
        "weekly_correlation_note": (
            "Weekly batches are smaller chronological evaluation units; "
            "adjacent weeks may remain correlated."
        ),
    }
    (out / "conclusions.json").write_text(
        json.dumps(conclusions, indent=2, default=str), encoding="utf-8"
    )

    # conclusions.md
    lines = [
        "# Gate 2 Weekly Strategy Expansion — Conclusions",
        "",
        f"Run: `{out.name}`",
        "",
        "## Data",
        f"- ≥12 months M1+H1: **False** (broker M1 depth ~60–90d)",
        f"- Oldest usable: 2026-06-01 | Newest research-usable: 2026-08-31",
        f"- September: **RESERVED** (not used)",
        "",
        f"## Variants: {len(variants)} across {len({v['family'] for v in variants})} families",
        "",
        "## Walk-forward survivors (val PF>1 & mean>0 on ≥2 windows)",
        "",
    ]
    for s in conclusions["survivors"]:
        lines.append(f"- `{s['candidate_id']}` ({s['family']}) windows={s['n_surviving_val_windows']}")
    lines += [
        "",
        "## Carry-forward (raw metrics only — not deployment)",
        "",
        ", ".join(f"`{x}`" for x in conclusions["carry_forward_candidate_ids"]),
        "",
        "## Compound",
        "- 2m/3m: executed for shortlist",
        "- 6m/12m: unavailable (M1 depth)",
        "",
        "## Next",
        "",
    ]
    for n in conclusions["next_experiments"]:
        lines.append(f"- {n}")
    lines.append("")
    (out / "conclusions.md").write_text("\n".join(lines), encoding="utf-8")

    print("DONE", out, flush=True)
    print("VARIANTS", len(variants), "SURVIVORS", len(conclusions["survivors"]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
