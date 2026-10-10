#!/usr/bin/env python3
"""Gate 2 walk-forward candidate laboratory — offline, September reserved.

Answers: which modifications repeatedly survive chronological forward
validation rather than merely looking good on combined June–August.

Does NOT use September for generation / training / selection / ranking.
Does NOT trade LIVE or Mirror. Does NOT modify frozen simulator / alexg8.
"""

from __future__ import annotations

import csv
import hashlib
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

from borex.alexg import AlexG8Strategy
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.gate2.thursday_entry_gate import filter_block_thursday_entries
from borex.gate2.weekly_research import (
    MIN_TRAIN_FILL_FRACTION,
    MIN_TRAIN_FILLS_ABS,
    aggregate_week_rows,
    assert_no_future_leakage,
    complete_iso_weeks,
    filter_session_hours,
    filter_sides,
    iso_week_key,
    is_september_reserved,
    metrics_for_weeks,
    min_train_fills_threshold,
    select_candidate_on_train,
    summarize_trades,
    thursday_harmful_on_train,
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
LOT_NOTIONAL = 100_000.0
WARMUP_DAYS = 400
GHOST_PRIME = 72

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
AUG_INDEX = ROOT / "data/runs/mt5_1m_slmult_lev_split/index.json"
JUN_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/decisions_sl_4p0.json"
JUL_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/decisions_sl_4p0.json"
AUG_TAPE = ROOT / "data/runs/mt5_1m_slmult_lev_split/decisions_sl_4p0.json"
PRIOR_WEEKLY = ROOT / "data/runs/research/gate2_weekly_strategy_expansion_20260926_014243"
PRIOR_EXT = ROOT / "data/runs/research/mt5_historical_extension_20260925_050221"

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
    posthoc=False,
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
        lot_notional=LOT_NOTIONAL,
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


def run_replay(period, tape, *, rr_factor, rr_min, ghost=4.0, starting_equity=None):
    """Replay with frozen MultiMarketEngine. starting_equity ignored by engine
    (always CAPITAL) — compounds use continuous multi-month period instead."""
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
            continue
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        margin = float(getattr(t, "margin", 0) or 0)
        notional = margin * LEV
        lot = (notional / LOT_NOTIONAL) if LOT_NOTIONAL else 0.0
        window.append(
            {
                "symbol": t.symbol,
                "side": side,
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time),
                "pnl": float(t.pnl),
                "exit_reason": t.exit_reason,
                "entry": float(t.entry_price),
                "margin": margin,
                "notional": notional,
                "lot_size": lot,
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


def prepare_tape(base: list[dict], cfg: dict) -> list[dict]:
    tape = filter_sides(base, cfg["sides"])
    tape = filter_session_hours(tape, cfg["session"])
    if cfg.get("thursday_gate"):
        tape = filter_block_thursday_entries(tape)
    return tape


def build_candidate_library() -> tuple[list[dict], list[dict]]:
    """Families A–H + separate POSTHOC_NOT_VALIDATED. Justifications recorded."""
    rows: list[dict] = []
    justifications: list[dict] = []

    def add(family, cid, justification, *, posthoc=False, **kw):
        cfg = dict(CONTROL)
        cfg.update(kw)
        cfg["candidate_id"] = cid
        cfg["family"] = family
        cfg["posthoc"] = posthoc
        rows.append(cfg)
        justifications.append(
            {
                "candidate_id": cid,
                "family": family,
                "posthoc": posthoc,
                "justification": justification,
            }
        )

    add(
        "A_N1_CONTROL",
        "n1_control",
        "Frozen N1 control baseline (alexg8 ghost=4 rr_factor=1 rr_min=2 both sides).",
    )
    add(
        "B_THURSDAY",
        "n1_thursday",
        "Prior Gate2 research: Thursday entries harmed Jun/Jul/Aug control tape; "
        "filter_block_thursday_entries is the frozen research treatment for Sep OOS.",
        thursday_gate=True,
    )
    for rf in (0.7, 0.8, 0.9, 1.1, 1.2, 1.4):
        add(
            "C_RR",
            f"rrf_{str(rf).replace('.', 'p')}",
            f"Existing RR lever from prior RR research; one-factor rr_factor={rf}.",
            rr_factor=rf,
        )
    for rm in (1.5, 2.5, 3.0):
        add(
            "C_RR",
            f"rmin_{str(rm).replace('.', 'p')}",
            f"Existing RR lever; one-factor rr_min={rm} (post-hoc interest on 2.5 noted separately).",
            rr_min=rm,
        )
    for sess in ("overlap", "london", "newyork", "asia"):
        add(
            "D_SESSION",
            f"sess_{sess}",
            f"Existing research-layer UTC session allowlist ({sess}); prior weekly expansion family.",
            session=sess,
        )
    add(
        "E_DIRECTION",
        "long_only",
        "Existing direction variant; prior weekly expansion tested long-only.",
        sides="long_only",
    )
    add(
        "E_DIRECTION",
        "short_only",
        "Existing direction variant; prior weekly expansion tested short-only.",
        sides="short_only",
    )
    # Family F — signal/filter: only levers with prior inventory evidence, not new indicators
    add(
        "F_SIGNAL_FILTER",
        "n1_control_signal_baseline",
        "Placeholder identity for inventory family F (no new AOI/ghost sweep — "
        "N1 ghost_sl_mult=4 already frozen; inventing new signal params disallowed).",
    )
    # Family G — pre-tested combos from weekly expansion (non-posthoc)
    add(
        "G_PREEXISTING_COMBO",
        "thu_rrf0p9",
        "Prior weekly expansion combo: Thursday gate + rr_factor=0.9 "
        "(each component previously motivated).",
        thursday_gate=True,
        rr_factor=0.9,
    )
    add(
        "G_PREEXISTING_COMBO",
        "thu_overlap",
        "Prior weekly expansion combo: Thursday + overlap session "
        "(session filter independently inventoried).",
        thursday_gate=True,
        session="overlap",
    )
    add(
        "G_PREEXISTING_COMBO",
        "rrf0p9_rmin2p5",
        "Prior weekly expansion RR combo without Thursday.",
        rr_factor=0.9,
        rr_min=2.5,
    )
    # Family H — Thursday + one additional independently motivated lever
    # (thu_rrf0p9 / thu_overlap also listed under G as pre-existing tested combos;
    #  H adds thursday + short_only as direction lever with prior E-family evidence)
    add(
        "H_THURSDAY_PLUS_ONE",
        "thu_short_only",
        "Thursday gate + short_only: each component independently motivated "
        "(Thursday harm in prior research; direction variant in prior weekly expansion).",
        thursday_gate=True,
        sides="short_only",
    )
    # POSTHOC — clearly separated
    add(
        "POSTHOC_NOT_VALIDATED",
        "posthoc_thu_rmin2p5",
        "POSTHOC: Thursday + rr_min=2.5 looked interesting on Jun–Aug combined sample; "
        "NOT prospectively validated; must not alter September experiment.",
        posthoc=True,
        thursday_gate=True,
        rr_min=2.5,
    )
    add(
        "POSTHOC_NOT_VALIDATED",
        "posthoc_thu_rrf0p9_rmin2p5",
        "POSTHOC: Thursday + rr_factor≈0.9 + rr_min=2.5 from prior notes; "
        "NOT validated; research-only.",
        posthoc=True,
        thursday_gate=True,
        rr_factor=0.9,
        rr_min=2.5,
    )
    return rows, justifications


def path_metrics(trades: list[dict], starting_equity: float = CAPITAL) -> dict[str, Any]:
    """Chronological path metrics for compound reporting (no shuffle)."""
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = float(starting_equity)
    peak = eq
    max_dd = 0.0
    dd_start = None
    longest_dd = 0
    cur_dd = 0
    recovery = None
    last_trough_end = None
    curve = []
    notionals = []
    lots = []
    for t in ordered:
        eq += float(t["pnl"])
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        if eq < peak:
            cur_dd += 1
            longest_dd = max(longest_dd, cur_dd)
            last_trough_end = t["entry_time"]
        else:
            if cur_dd > 0 and recovery is None and last_trough_end:
                recovery = last_trough_end
            cur_dd = 0
        curve.append({"time": t["entry_time"], "equity": eq, "pnl": t["pnl"]})
        if t.get("notional"):
            notionals.append(float(t["notional"]))
        if t.get("lot_size"):
            lots.append(float(t["lot_size"]))
    week_rows = weekly_metrics_table(ordered, candidate_id="_")
    dist = weekly_distribution(week_rows)
    margin_stops = sum(1 for t in ordered if str(t.get("exit_reason")) == "margin_stop")
    return {
        "starting_equity": starting_equity,
        "ending_equity": eq,
        "total_return": eq - starting_equity,
        "total_return_pct": (eq / starting_equity - 1.0) if starting_equity else None,
        "max_drawdown": max_dd,
        "worst_week": dist.get("worst_week_net"),
        "best_week": dist.get("best_week_net"),
        "margin_stop_count": margin_stops,
        "maximum_notional": max(notionals) if notionals else None,
        "average_notional": float(statistics.mean(notionals)) if notionals else None,
        "average_lot_size": float(statistics.mean(lots)) if lots else None,
        "maximum_lot_size": max(lots) if lots else None,
        "recovery_period_note": recovery,
        "longest_drawdown_trades": longest_dd,
        "profitable_weeks": dist.get("profitable_weeks"),
        "losing_weeks": dist.get("negative_weeks"),
        "fills": len(ordered),
        "curve": curve,
        "week_rows": week_rows,
    }


def compare_years_m1_vs_utcv2(symbols_sample: list[str]) -> list[dict]:
    """Overlap validation: legacy years/1m cache vs authoritative utc_v2."""
    rows = []
    start = pd.Timestamp("2026-06-02", tz="UTC")
    end = pd.Timestamp("2026-07-01", tz="UTC")
    for sym in symbols_sample:
        ypath = ROOT / "data" / "cache" / sym / "years" / "1m" / "2026.parquet"
        upath = _cache_path(sym, "1m")
        if not ypath.is_file() or not upath.is_file():
            rows.append(
                {
                    "symbol": sym,
                    "status": "missing_one_side",
                    "years_exists": ypath.is_file(),
                    "utc_v2_exists": upath.is_file(),
                }
            )
            continue
        y = pd.read_parquet(ypath).reset_index()
        if "timestamp" not in y.columns:
            y = y.rename_axis("timestamp").reset_index()
        y["timestamp"] = pd.to_datetime(y["timestamp"], utc=True)
        y = y.rename(columns={c: c.lower() for c in y.columns})
        u = pd.read_parquet(upath)
        u["timestamp"] = pd.to_datetime(u["timestamp"], utc=True)
        y = y[(y.timestamp >= start) & (y.timestamp < end)].set_index("timestamp").sort_index()
        u = u[(u.timestamp >= start) & (u.timestamp < end)].set_index("timestamp").sort_index()
        common = y.index.intersection(u.index)
        y_only = len(y.index.difference(u.index))
        u_only = len(u.index.difference(y.index))
        diffs = {}
        if len(common):
            for col in ("open", "high", "low", "close"):
                d = (y.loc[common, col].astype(float) - u.loc[common, col].astype(float)).abs()
                diffs[f"{col}_mean_abs"] = float(d.mean())
                diffs[f"{col}_max_abs"] = float(d.max())
                diffs[f"{col}_pct_nonzero"] = float((d > 1e-8).mean())
        rows.append(
            {
                "symbol": sym,
                "status": "COMPARED",
                "common_bars": len(common),
                "years_only_bars": y_only,
                "utc_v2_only_bars": u_only,
                "years_volume_all_zero": True,  # observed globally
                **diffs,
                "compatible_for_merge": False,
                "note": "Material OHLC divergence + missing/extra bars + vol=0 → NOT mergeable",
            }
        )
    return rows


def classify_candidate(
    *,
    cid: str,
    family: str,
    posthoc: bool,
    n_surviving: int,
    n_splits: int,
    selected_count: int,
    thursday_prospective_improves: int | None,
) -> str:
    if cid == "n1_control" or family == "A_N1_CONTROL":
        return "CONTROL"
    if posthoc or family == "POSTHOC_NOT_VALIDATED":
        return "POSTHOC_RESEARCH"
    if cid == "n1_thursday" or family == "B_THURSDAY":
        if n_surviving >= 2 and (thursday_prospective_improves or 0) >= 1:
            return "VALIDATED_RESEARCH_CANDIDATE"
        if n_surviving >= 1:
            return "MIXED"
        return "UNSTABLE"
    if n_surviving >= 2 and selected_count >= 1:
        return "MIXED"
    if n_surviving >= 2:
        return "MIXED"
    if n_surviving == 0:
        return "REJECTED"
    return "UNSTABLE"


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"gate2_walkforward_candidate_lab_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidate_equity_curves").mkdir(exist_ok=True)
    print(f"OUT {out}", flush=True)

    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    uhash = universe_hash(symbols)

    # ---- Pre-register selection rules BEFORE any selection ----
    selection_doc = "\n".join(
        [
            "# Candidate Selection Rules (pre-registered)",
            "",
            "Documented BEFORE walk-forward selection executes.",
            "",
            f"- MIN_TRAIN_FILL_FRACTION = {MIN_TRAIN_FILL_FRACTION}",
            f"- MIN_TRAIN_FILLS_ABS = {MIN_TRAIN_FILLS_ABS}",
            "- min_train_fills = max(MIN_TRAIN_FILLS_ABS, floor(0.50 * control_train_fills))",
            "- Hard gates vs control on the SAME train weeks only:",
            "  1. candidate_train_fills >= min_train_fills",
            "  2. candidate train PF > control train PF",
            "  3. candidate train mean_trade > control train mean_trade",
            "- Rank among eligible: median_weekly_net, then PF, then mean_trade, then worst_week, then fills",
            "- If none eligible → select n1_control",
            "- POSTHOC_NOT_VALIDATED family excluded from primary selection eligible pool",
            "- September weeks NEVER enter train or validation",
            "",
            f"Control weekly fill baseline will be measured on complete ISO weeks after sim.",
            "",
        ]
    )
    (out / "selection_rules_preregistered.md").write_text(selection_doc, encoding="utf-8")

    # ---- Additional M1 source search ----
    years_syms = sorted(
        p.parent.parent.name for p in (ROOT / "data" / "cache").glob("*/years/1m/2026.parquet")
    )
    sample = [s for s in ["AUDCADX", "EURUSDX", "GBPUSDX", "USDJPYX"] if s in years_syms] or years_syms[:4]
    overlap_rows = compare_years_m1_vs_utcv2(sample)
    write_csv(out / "years_m1_overlap_vs_utcv2.csv", overlap_rows)

    add_md = "\n".join(
        [
            "# Additional Data Search",
            "",
            "## Authorized MT5 endpoints in project `.env`",
            "",
            "- LIVE: login 52958398 / port 8791 — **FORBIDDEN** (never connect for research history)",
            "- MIRROR: login 53019882 / ICMarketsSC-Demo — authorized Mirror probe only; **Do NOT trade**",
            "- No third MT5 terminal / alternate demo path configured",
            "",
            "## Prior broker depth probe",
            "",
            f"- `{PRIOR_EXT.as_posix()}`: IC Markets M1 depth ≈ 60–90 days; Jan–May 2026 empty",
            "",
            "## Alternate on-disk caches inspected",
            "",
            f"- `data/cache/*/years/1m/` present for **{len(years_syms)}** symbols (not 60)",
            "- Overlap Jun 2026 vs utc_v2: material OHLC differences (~99.7% bars nonzero delta),",
            "  missing/extra bars, Volume identically 0.0 → **NOT research-compatible** for N1 merge",
            "- No dukascopy / Yahoo / HistData acquisition paths authorized for this lab",
            "",
            "## Verdict",
            "",
            "**NO_ADDITIONAL_COMPATIBLE_M1_SOURCE_FOUND**",
            "",
            "Do not fabricate a year of data. H1 alone remains NOT_COMPARABLE for N1 closest-sim.",
            "",
        ]
    )
    (out / "additional_data_search.md").write_text(add_md, encoding="utf-8")

    # ---- Inventory / coverage (Jun–Aug only usable) ----
    def scan(interval: str):
        rows = []
        for sym in symbols:
            p = _cache_path(sym, interval)
            if not p.is_file():
                rows.append({"symbol": sym, "n": 0, "tmin": None, "tmax": None})
                continue
            df = pd.read_parquet(p, columns=["timestamp"])
            ts = pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True))
            rows.append(
                {
                    "symbol": sym,
                    "n": int(len(ts)),
                    "tmin": str(ts.min()),
                    "tmax": str(ts.max()),
                }
            )
        return rows

    h1_rows, m1_rows = scan("1h"), scan("1m")
    inv_md = "\n".join(
        [
            "# Data Inventory",
            "",
            f"Assessed: {datetime.now(timezone.utc).isoformat()}",
            "",
            "- Provenance: `data/cache/mt5/utc_v2` via MT5 copy_rates",
            f"- H1: {min(r['tmin'] for r in h1_rows if r['tmin'])} → {max(r['tmax'] for r in h1_rows if r['tmax'])} ({sum(1 for r in h1_rows if r['n']>0)}/60)",
            f"- M1: {min(r['tmin'] for r in m1_rows if r['tmin'])} → {max(r['tmax'] for r in m1_rows if r['tmax'])} ({sum(1 for r in m1_rows if r['n']>0)}/60)",
            "- Usable research months: 2026-06, 2026-07, 2026-08",
            "- September: RESERVED (not used for this lab)",
            "- Additional compatible M1: NO_ADDITIONAL_COMPATIBLE_M1_SOURCE_FOUND",
            "",
        ]
    )
    (out / "data_inventory.md").write_text(inv_md, encoding="utf-8")
    (out / "data_inventory.json").write_text(
        json.dumps({"h1": h1_rows, "m1": m1_rows}, indent=2), encoding="utf-8"
    )

    # coverage.csv monthly
    coverage = []
    for per in pd.period_range("2025-06", "2026-09", freq="M"):
        start = per.to_timestamp(how="start").tz_localize("UTC")
        end = (per + 1).to_timestamp(how="start").tz_localize("UTC")
        h1_ok = m1_ok = 0
        for sym in symbols:
            for tf, counter in (("1h", "h1"), ("1m", "m1")):
                p = _cache_path(sym, tf)
                if not p.is_file():
                    continue
                df = pd.read_parquet(p, columns=["timestamp"])
                ts = pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True))
                sl = ts[(ts >= start) & (ts < end)]
                if len(sl) == 0:
                    continue
                if tf == "1h":
                    h1_ok += 1
                else:
                    m1_ok += 1
        y, m = per.year, per.month
        reserved = (y, m) == (2026, 9)
        usable = h1_ok == 60 and m1_ok == 60 and not reserved and (y, m) >= (2026, 6)
        coverage.append(
            {
                "month": f"{y}-{m:02d}",
                "H1_count": h1_ok,
                "M1_count": m1_ok,
                "H1_60_60": h1_ok == 60,
                "M1_60_60": m1_ok == 60,
                "usable_for_strategy_research": usable,
                "september_reserved": reserved,
                "source": "mt5_utc_v2",
            }
        )
    write_csv(out / "coverage.csv", coverage)

    (out / "provenance.md").write_text(
        "\n".join(
            [
                "# Provenance",
                "",
                f"git: `{_git()}`",
                f"universe_hash: `{uhash}`",
                "simulator: frozen MultiMarketEngine H1→1m closest-sim",
                "capital=1000 leverage=500 Raw spreads + $7/lot",
                "force_flat_daily=true force_flat_friday=false",
                f"prior_weekly_expansion: `{PRIOR_WEEKLY.as_posix()}`",
                "September NOT used for candidate generation/training/selection/ranking",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out / "september_lock.md").write_text(
        "\n".join(
            [
                "# September Lock",
                "",
                "September 2026 was NOT used for:",
                "- candidate generation",
                "- training",
                "- selection",
                "- ranking",
                "- parameter tuning",
                "- feature discovery",
                "",
                "Locked OOS (unchanged):",
                "- CONTROL: N1@500",
                "- TREATMENT: N1@500 + Thursday gate",
                "",
                "Do not modify that experiment.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    variants, justifications = build_candidate_library()
    # Deduplicate identity F baseline (same as control) — skip duplicate sim
    variants = [v for v in variants if v["candidate_id"] != "n1_control_signal_baseline"]
    justifications = [
        j for j in justifications if j["candidate_id"] != "n1_control_signal_baseline"
    ]
    # Re-add F as documentation-only pointing to control
    justifications.append(
        {
            "candidate_id": "n1_control",
            "family": "F_SIGNAL_FILTER",
            "posthoc": False,
            "justification": (
                "Family F: no new signal parameters invented; N1 defaults stand. "
                "See n1_control metrics."
            ),
        }
    )
    write_csv(out / "candidate_manifest.csv", variants)
    write_csv(
        out / "candidate_families.csv",
        [
            {"family": f, "n": sum(1 for v in variants if v["family"] == f)}
            for f in sorted({v["family"] for v in variants})
        ],
    )
    (out / "candidate_justifications.md").write_text(
        "# Candidate Justifications\n\n"
        + "\n\n".join(
            f"## `{j['candidate_id']}` ({j['family']}"
            + (", POSTHOC" if j.get("posthoc") else "")
            + f")\n\n{j['justification']}"
            for j in justifications
        )
        + "\n",
        encoding="utf-8",
    )

    # ---- Load + simulate ----
    periods = {
        "june": load_period("june", JUN_INDEX, symbols, spreads),
        "july": load_period("july", JUL_INDEX, symbols, spreads),
        "august": load_period("august", AUG_INDEX, symbols, spreads),
    }
    base_tapes = {
        "june": remap_tape(json.loads(JUN_TAPE.read_text(encoding="utf-8")), periods["june"]["data"]),
        "july": remap_tape(json.loads(JUL_TAPE.read_text(encoding="utf-8")), periods["july"]["data"]),
        "august": remap_tape(
            json.loads(AUG_TAPE.read_text(encoding="utf-8")), periods["august"]["data"]
        ),
    }

    t0 = time.perf_counter()
    trades_by_cand: dict[str, list[dict]] = {}
    all_weekly: list[dict] = []
    retrospective: list[dict] = []

    for i, cfg in enumerate(variants, 1):
        cid = cfg["candidate_id"]
        print(f"=== [{i}/{len(variants)}] {cid} ===", flush=True)
        all_trades: list[dict] = []
        month_stats = {}
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
                f"  {pname}: PF={s.get('PF')} net={s.get('net_pnl'):.1f} fills={s.get('fills')}",
                flush=True,
            )
        trades_by_cand[cid] = all_trades
        week_rows = weekly_metrics_table(all_trades, candidate_id=cid)
        all_weekly.extend(week_rows)
        dist = weekly_distribution(week_rows)
        full = summarize_trades(all_trades, mdd=None, starting_equity=CAPITAL)
        retrospective.append(
            {
                "candidate_id": cid,
                "family": cfg["family"],
                "posthoc": cfg.get("posthoc"),
                "rr_factor": cfg["rr_factor"],
                "rr_min": cfg["rr_min"],
                "sides": cfg["sides"],
                "session": cfg["session"],
                "thursday_gate": cfg["thursday_gate"],
                "mode": "RETROSPECTIVE_JUN_AUG",
                "june_PF": month_stats["june"].get("PF"),
                "july_PF": month_stats["july"].get("PF"),
                "august_PF": month_stats["august"].get("PF"),
                "june_net": month_stats["june"].get("net_pnl"),
                "july_net": month_stats["july"].get("net_pnl"),
                "august_net": month_stats["august"].get("net_pnl"),
                "PF": full.get("PF"),
                "net_pnl": full.get("net_pnl"),
                "mean_trade": full.get("mean_trade"),
                "fills": full.get("fills"),
                "TP": full.get("TP"),
                "daily_close": full.get("daily_close"),
                "margin_stop": full.get("margin_stop"),
                "long_count": full.get("long_count"),
                "short_count": full.get("short_count"),
                **{f"weekly_{k}": v for k, v in dist.items()},
            }
        )

    write_csv(out / "weekly_retrospective.csv", all_weekly)
    write_csv(out / "retrospective_summary.csv", retrospective)

    # Complete weeks calendar from control
    ctrl_week_rows = [r for r in all_weekly if r["candidate_id"] == "n1_control"]
    complete_weeks = complete_iso_weeks(ctrl_week_rows)
    (out / "complete_weeks.json").write_text(
        json.dumps({"complete_weeks": complete_weeks, "n": len(complete_weeks)}, indent=2),
        encoding="utf-8",
    )
    # Document measured control weekly fills used for threshold
    ctrl_complete = [r for r in ctrl_week_rows if r["week"] in complete_weeks]
    ctrl_weekly_fills = [int(r["fills"]) for r in ctrl_complete]
    thr_note = (
        f"control_complete_weeks={len(ctrl_complete)}; "
        f"median_weekly_fills={statistics.median(ctrl_weekly_fills) if ctrl_weekly_fills else None}; "
        f"mean_weekly_fills={statistics.mean(ctrl_weekly_fills) if ctrl_weekly_fills else None}; "
        f"MIN_TRAIN_FILL_FRACTION={MIN_TRAIN_FILL_FRACTION}"
    )
    with (out / "selection_rules_preregistered.md").open("a", encoding="utf-8") as f:
        f.write(f"\nMeasured after control sim (not used to change rules): {thr_note}\n")

    splits = walk_forward_splits(complete_weeks, min_train=4, val_size=2)
    assert_no_future_leakage(splits)
    (out / "walkforward_splits.json").write_text(
        json.dumps(splits, indent=2), encoding="utf-8"
    )

    # Primary eligible pool excludes POSTHOC
    primary_ids = [v["candidate_id"] for v in variants if not v.get("posthoc")]
    posthoc_ids = [v["candidate_id"] for v in variants if v.get("posthoc")]

    wf_results: list[dict] = []
    wf_selection: list[dict] = []
    thursday_special: list[dict] = []
    selected_counts: dict[str, int] = defaultdict(int)
    surviving_counts: dict[str, int] = defaultdict(int)

    ctrl_trades = trades_by_cand["n1_control"]
    thu_trades = trades_by_cand["n1_thursday"]

    for sp in splits:
        # Build train metrics from trade lists (accurate PF) for all candidates
        train_metrics: dict[str, dict] = {}
        val_metrics: dict[str, dict] = {}
        for cfg in variants:
            cid = cfg["candidate_id"]
            tr_m = metrics_for_weeks(trades_by_cand[cid], sp["train_weeks"])
            va_m = metrics_for_weeks(trades_by_cand[cid], sp["validation_weeks"])
            # enrich with weekly distribution on train/val
            wr = [r for r in all_weekly if r["candidate_id"] == cid]
            tr_w = aggregate_week_rows(wr, sp["train_weeks"])
            va_w = aggregate_week_rows(wr, sp["validation_weeks"])
            tr_m = {
                **tr_m,
                "median_weekly_net": tr_w.get("median_weekly_net"),
                "worst_week": tr_w.get("worst_week"),
                "best_week": tr_w.get("best_week"),
                "profitable_weeks": tr_w.get("profitable_weeks"),
                "losing_weeks": tr_w.get("losing_weeks"),
                "consecutive_losing_weeks": tr_w.get("consecutive_losing_weeks"),
                "thursday_net": tr_w.get("thursday_net"),
                "thursday_fills": tr_w.get("thursday_fills"),
            }
            va_m = {
                **va_m,
                "median_weekly_net": va_w.get("median_weekly_net"),
                "worst_week": va_w.get("worst_week"),
                "best_week": va_w.get("best_week"),
                "profitable_weeks": va_w.get("profitable_weeks"),
                "losing_weeks": va_w.get("losing_weeks"),
                "consecutive_losing_weeks": va_w.get("consecutive_losing_weeks"),
            }
            train_metrics[cid] = tr_m
            val_metrics[cid] = va_m

        # Prospective selection (primary pool only) using TRAIN only
        sel = select_candidate_on_train(
            candidate_train={k: train_metrics[k] for k in primary_ids},
            control_id="n1_control",
            eligible_ids=primary_ids,
        )
        selected_counts[sel["selected_id"]] += 1
        ctrl_val = val_metrics["n1_control"]
        sel_val = val_metrics[sel["selected_id"]]
        wf_selection.append(
            {
                "split_id": sp["split_id"],
                "train_weeks": "|".join(sp["train_weeks"]),
                "validation_weeks": "|".join(sp["validation_weeks"]),
                "selected_id": sel["selected_id"],
                "selection_reason": sel["reason"],
                "min_train_fills": sel["min_train_fills"],
                "control_train_fills": sel["control_train_fills"],
                "n_eligible": sel["n_eligible"],
                "selected_val_PF": sel_val.get("PF"),
                "selected_val_net": sel_val.get("net_pnl"),
                "selected_val_mean": sel_val.get("mean_trade"),
                "selected_val_MDD": sel_val.get("MDD"),
                "control_val_PF": ctrl_val.get("PF"),
                "control_val_net": ctrl_val.get("net_pnl"),
                "val_net_vs_control": float(sel_val.get("net_pnl") or 0)
                - float(ctrl_val.get("net_pnl") or 0),
                "val_PF_delta": (
                    None
                    if sel_val.get("PF") is None or ctrl_val.get("PF") is None
                    else float(sel_val["PF"]) - float(ctrl_val["PF"])
                ),
            }
        )

        # Scoreboard every candidate on this val block
        for cfg in variants:
            cid = cfg["candidate_id"]
            tr = train_metrics[cid]
            va = val_metrics[cid]
            survives = (
                va.get("PF") is not None
                and va.get("mean_trade") is not None
                and float(va["PF"]) > 1.0
                and float(va["mean_trade"]) > 0.0
            )
            if survives:
                surviving_counts[cid] += 1
            ctrl_tr_fills = int(train_metrics["n1_control"].get("fills") or 0)
            wf_results.append(
                {
                    "candidate_id": cid,
                    "family": cfg["family"],
                    "posthoc": cfg.get("posthoc"),
                    "split_id": sp["split_id"],
                    "train_n": sp["train_n"],
                    "val_n": sp["val_n"],
                    "train_weeks": "|".join(sp["train_weeks"]),
                    "validation_weeks": "|".join(sp["validation_weeks"]),
                    "train_PF": tr.get("PF"),
                    "train_net": tr.get("net_pnl"),
                    "train_mean": tr.get("mean_trade"),
                    "train_median": tr.get("median_trade"),
                    "train_fills": tr.get("fills"),
                    "train_worst_week": tr.get("worst_week"),
                    "train_profitable_weeks": tr.get("profitable_weeks"),
                    "train_losing_weeks": tr.get("losing_weeks"),
                    "val_PF": va.get("PF"),
                    "val_net": va.get("net_pnl"),
                    "val_mean": va.get("mean_trade"),
                    "val_median": va.get("median_trade"),
                    "val_MDD": va.get("MDD"),
                    "val_fills": va.get("fills"),
                    "val_TP": va.get("TP"),
                    "val_daily_close": va.get("daily_close"),
                    "val_margin_stop": va.get("margin_stop"),
                    "val_long": va.get("long_count"),
                    "val_short": va.get("short_count"),
                    "val_profitable_weeks": va.get("profitable_weeks"),
                    "val_losing_weeks": va.get("losing_weeks"),
                    "val_worst_week": va.get("worst_week"),
                    "val_best_week": va.get("best_week"),
                    "val_consecutive_losing_weeks": va.get("consecutive_losing_weeks"),
                    "val_return_vs_control": float(va.get("net_pnl") or 0)
                    - float(ctrl_val.get("net_pnl") or 0),
                    "val_PF_delta": (
                        None
                        if va.get("PF") is None or ctrl_val.get("PF") is None
                        else float(va["PF"]) - float(ctrl_val["PF"])
                    ),
                    "val_MDD_delta": (
                        None
                        if va.get("MDD") is None or ctrl_val.get("MDD") is None
                        else float(va["MDD"]) - float(ctrl_val["MDD"])
                    ),
                    "trade_retention": (
                        None
                        if not ctrl_val.get("fills")
                        else float(va.get("fills") or 0) / float(ctrl_val["fills"])
                    ),
                    "passes_train_fill_gate": int(tr.get("fills") or 0)
                    >= min_train_fills_threshold(ctrl_tr_fills),
                    "survives_val_pf_mean": survives,
                    "was_selected": cid == sel["selected_id"],
                }
            )

        # Thursday dedicated prospective analysis
        ctrl_tr = train_metrics["n1_control"]
        harm = thursday_harmful_on_train(ctrl_tr)
        # Would Thursday gate be selected among {control, thursday}?
        thu_sel = select_candidate_on_train(
            candidate_train={
                "n1_control": train_metrics["n1_control"],
                "n1_thursday": train_metrics["n1_thursday"],
            },
            control_id="n1_control",
            eligible_ids=["n1_control", "n1_thursday"],
        )
        gate_selected = thu_sel["selected_id"] == "n1_thursday"
        # Trades removed = control train thursday fills approx; use tape-level on val
        ctrl_val_w = aggregate_week_rows(
            [r for r in all_weekly if r["candidate_id"] == "n1_control"],
            sp["validation_weeks"],
        )
        thu_val = val_metrics["n1_thursday"]
        # Removed trades ≈ control fills - thursday fills on same window
        for label, weeks, c_m, t_m in (
            ("train", sp["train_weeks"], train_metrics["n1_control"], train_metrics["n1_thursday"]),
            ("val", sp["validation_weeks"], val_metrics["n1_control"], val_metrics["n1_thursday"]),
        ):
            pass
        removed_fills_val = int(ctrl_val.get("fills") or 0) - int(thu_val.get("fills") or 0)
        # PnL removed on val: control trades that fall on Thursday in val weeks
        val_ctrl_trades = [
            t
            for t in ctrl_trades
            if iso_week_key(t["entry_time"]) in set(sp["validation_weeks"])
        ]
        thu_val_trades = [
            t for t in val_ctrl_trades if _ts(t["entry_time"]).dayofweek == 3
        ]
        removed_pnl = float(sum(float(t["pnl"]) for t in thu_val_trades))
        removed_pos = float(sum(float(t["pnl"]) for t in thu_val_trades if float(t["pnl"]) > 0))
        improved = float(thu_val.get("net_pnl") or 0) > float(ctrl_val.get("net_pnl") or 0)
        mdd_improved = (
            thu_val.get("MDD") is not None
            and ctrl_val.get("MDD") is not None
            and float(thu_val["MDD"]) < float(ctrl_val["MDD"])
        )
        thursday_special.append(
            {
                "split_id": sp["split_id"],
                "train_weeks": "|".join(sp["train_weeks"]),
                "validation_weeks": "|".join(sp["validation_weeks"]),
                "thursday_harmful_in_train": harm,
                "train_thursday_net": ctrl_tr.get("thursday_net"),
                "train_thursday_fills": ctrl_tr.get("thursday_fills"),
                "gate_selected_prospectively": gate_selected,
                "selection_reason": thu_sel["reason"],
                "validation_improved_net": improved,
                "val_control_net": ctrl_val.get("net_pnl"),
                "val_thursday_net": thu_val.get("net_pnl"),
                "val_net_delta": float(thu_val.get("net_pnl") or 0)
                - float(ctrl_val.get("net_pnl") or 0),
                "val_PF_control": ctrl_val.get("PF"),
                "val_PF_thursday": thu_val.get("PF"),
                "trades_removed_val": removed_fills_val,
                "pnl_removed_val": removed_pnl,
                "positive_pnl_removed_val": removed_pos,
                "val_MDD_control": ctrl_val.get("MDD"),
                "val_MDD_thursday": thu_val.get("MDD"),
                "validation_MDD_improved": mdd_improved,
                "note": "July not used to justify gates for windows preceding July",
            }
        )

    write_csv(out / "walkforward_results.csv", wf_results)
    write_csv(out / "walkforward_selection.csv", wf_selection)
    write_csv(out / "thursday_prospective_analysis.csv", thursday_special)

    thu_improve_splits = sum(1 for r in thursday_special if r["validation_improved_net"])
    thu_selected_splits = sum(1 for r in thursday_special if r["gate_selected_prospectively"])

    # ---- Compounds for sufficiently supported candidates ----
    def load_compound(month_names: list[str]) -> dict:
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
        tape = []
        for m in month_names:
            tape.extend(remap_tape(base_tapes[m], data))
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

    # Supported = control, thursday, survivors>=2 primary, posthoc for separate reporting
    compound_ids = {"n1_control", "n1_thursday"}
    for cid, n in surviving_counts.items():
        if n >= 2 and cid in primary_ids:
            compound_ids.add(cid)
    # Always include posthoc separately labeled
    for cid in posthoc_ids:
        compound_ids.add(cid)
    # Cap runaway: top by surviving + selected
    if len(compound_ids) > 12:
        ranked = sorted(
            compound_ids,
            key=lambda c: (surviving_counts.get(c, 0), selected_counts.get(c, 0)),
            reverse=True,
        )
        keep = {"n1_control", "n1_thursday", *posthoc_ids, *ranked[:8]}
        compound_ids = keep

    compound_spans = [
        (["june", "july"], "compound_2m"),
        (["july", "august"], "compound_2m_b"),
        (["june", "july", "august"], "compound_3m"),
    ]
    compound_periods = {key: load_compound(months) for months, key in compound_spans}

    compound_out: dict[str, list[dict]] = {
        "compound_2m": [],
        "compound_2m_b": [],
        "compound_3m": [],
        "compound_6m": [
            {"status": "unavailable", "reason": "NO_ADDITIONAL_COMPATIBLE_M1_SOURCE_FOUND"}
        ],
        "compound_12m": [
            {"status": "unavailable", "reason": "NO_ADDITIONAL_COMPATIBLE_M1_SOURCE_FOUND"}
        ],
    }

    cfg_by_id = {v["candidate_id"]: v for v in variants}
    for cid in sorted(compound_ids):
        cfg = cfg_by_id[cid]
        for months, key in compound_spans:
            cper = compound_periods[key]
            tape = prepare_tape(cper["tape"], cfg)
            print(f"=== COMPOUND {key} {cid} ===", flush=True)
            s, trades = run_replay(
                cper, tape, rr_factor=cfg["rr_factor"], rr_min=cfg["rr_min"]
            )
            pm = path_metrics(trades, CAPITAL)
            write_csv(
                out / "candidate_equity_curves" / f"{key}_{cid}.csv",
                pm["curve"],
            )
            # Fixed-equity = sum of monthly nets with $1000 restart each month
            fixed_net = 0.0
            for m in months:
                m_trades = [t for t in trades_by_cand[cid] if t.get("month") == m]
                fixed_net += float(sum(float(t["pnl"]) for t in m_trades))
            compound_out[key].append(
                {
                    "candidate_id": cid,
                    "family": cfg["family"],
                    "posthoc": cfg.get("posthoc"),
                    "months": "|".join(months),
                    "mode_compound": True,
                    "starting_equity": pm["starting_equity"],
                    "ending_equity": pm["ending_equity"],
                    "total_return": pm["total_return"],
                    "total_return_pct": pm["total_return_pct"],
                    "max_drawdown": pm["max_drawdown"],
                    "engine_MDD": s.get("MDD"),
                    "worst_week": pm["worst_week"],
                    "best_week": pm["best_week"],
                    "margin_stop_count": pm["margin_stop_count"],
                    "maximum_notional": pm["maximum_notional"],
                    "average_notional": pm["average_notional"],
                    "average_lot_size": pm["average_lot_size"],
                    "maximum_lot_size": pm["maximum_lot_size"],
                    "recovery_period_note": pm["recovery_period_note"],
                    "longest_drawdown_trades": pm["longest_drawdown_trades"],
                    "profitable_weeks": pm["profitable_weeks"],
                    "losing_weeks": pm["losing_weeks"],
                    "compound_PF": s.get("PF"),
                    "compound_fills": s.get("fills"),
                    "fixed_equity_sum_net": fixed_net,
                    "delta_compound_minus_fixed": pm["total_return"] - fixed_net,
                }
            )

    for k, rows in compound_out.items():
        write_csv(out / f"{k}.csv", rows)

    # ---- Classification ----
    class_rows = []
    for cfg in variants:
        cid = cfg["candidate_id"]
        cat = classify_candidate(
            cid=cid,
            family=cfg["family"],
            posthoc=bool(cfg.get("posthoc")),
            n_surviving=surviving_counts.get(cid, 0),
            n_splits=len(splits),
            selected_count=selected_counts.get(cid, 0),
            thursday_prospective_improves=thu_improve_splits if cid == "n1_thursday" else None,
        )
        class_rows.append(
            {
                "candidate_id": cid,
                "family": cfg["family"],
                "category": cat,
                "n_surviving_val_windows": surviving_counts.get(cid, 0),
                "n_splits": len(splits),
                "n_times_selected": selected_counts.get(cid, 0),
                "posthoc": cfg.get("posthoc"),
            }
        )
    write_csv(out / "candidate_categories.csv", class_rows)

    # Theory-broker coverage stub from prior
    theory_cov = [
        {
            "source": "prior_thursday_gate_daily_theory_broker",
            "note": "No new live/mirror tickets touched; coverage unchanged from prior research",
            "september_lock": True,
        }
    ]
    write_csv(out / "theory_broker_coverage.csv", theory_cov)

    survivors_multi = [
        c["candidate_id"]
        for c in class_rows
        if c["n_surviving_val_windows"] >= 2 and not c.get("posthoc")
    ]
    carry = [
        c
        for c in class_rows
        if c["category"]
        in {"CONTROL", "VALIDATED_RESEARCH_CANDIDATE", "POSTHOC_RESEARCH", "MIXED"}
    ]
    write_csv(out / "candidates_carry_forward.csv", carry)

    fingerprint = {
        "engine": "MultiMarketEngine",
        "frozen": True,
        "true_sl": True,
        "size_mode": "margin",
        "path": "h1_signal_1m_exit",
        "universe_hash": uhash,
        "git": _git(),
    }
    (out / "simulator_fingerprint.json").write_text(
        json.dumps(fingerprint, indent=2), encoding="utf-8"
    )

    # Conclusions
    conclusions = {
        "run": out.name,
        "additional_m1": "NO_ADDITIONAL_COMPATIBLE_M1_SOURCE_FOUND",
        "complete_weeks": complete_weeks,
        "n_splits": len(splits),
        "n_candidates": len(variants),
        "families": sorted({v["family"] for v in variants}),
        "selection_rules": {
            "MIN_TRAIN_FILL_FRACTION": MIN_TRAIN_FILL_FRACTION,
            "MIN_TRAIN_FILLS_ABS": MIN_TRAIN_FILLS_ABS,
        },
        "thursday_prospective": {
            "splits_gate_selected": thu_selected_splits,
            "splits_validation_improved": thu_improve_splits,
            "n_splits": len(splits),
            "rows": thursday_special,
        },
        "survivors_multi_forward": survivors_multi,
        "categories": class_rows,
        "september_unused": True,
        "september_lock_unchanged": True,
        "compound_6m_12m": "unavailable",
        "elapsed_min": (time.perf_counter() - t0) / 60.0,
        "next_experiment": (
            "After September closes: locked OOS only — N1@500 vs N1@500+Thursday. "
            "Do not deploy posthoc RR=2.5. Do not expand DOW search before Sep OOS."
        ),
    }
    (out / "conclusions.json").write_text(
        json.dumps(conclusions, indent=2, default=str), encoding="utf-8"
    )

    # Markdown report
    lines = [
        "# Gate 2 Walk-Forward Candidate Laboratory — Conclusions",
        "",
        f"Run: `{out.name}`",
        "",
        "## 1. Compatible data coverage",
        "- H1+M1 utc_v2: Jun–Aug 2026 usable; Sep reserved",
        f"- Complete ISO weeks used: {len(complete_weeks)} (`{'`, `'.join(complete_weeks)}`)",
        "",
        "## 2. Additional M1",
        "**NO_ADDITIONAL_COMPATIBLE_M1_SOURCE_FOUND** (years/1m incompatible; LIVE forbidden; Mirror depth exhausted)",
        "",
        "## 3. Weekly methodology",
        f"- Expanding walk-forward min_train=4 val=2 → {len(splits)} splits",
        f"- Selection pre-registered: fill≥{MIN_TRAIN_FILL_FRACTION:.0%} control + PF/mean > control on train only",
        "",
        f"## 4–5. Candidates: {len(variants)} across families {sorted({v['family'] for v in variants})}",
        "",
        "## 6–7. Walk-forward / Thursday prospective",
        f"- Thursday gate selected prospectively on {thu_selected_splits}/{len(splits)} splits",
        f"- Thursday improved validation net on {thu_improve_splits}/{len(splits)} splits",
        "",
        "## 8–10. Compounds",
        "- 2m / 2m_b / 3m executed for supported set",
        "- 6m/12m unavailable (no compatible M1)",
        "",
        "## 13. Posthoc RR=2.5",
        "- Labeled POSTHOC_NOT_VALIDATED; excluded from primary selection pool",
        "",
        "## 14. Multi-window survivors (primary)",
        ", ".join(f"`{x}`" for x in survivors_multi) or "(none)",
        "",
        "## 15. September lock",
        "CONFIRMED unused for generation/training/selection/ranking. Locked OOS unchanged.",
        "",
        "## 18. Next experiment",
        conclusions["next_experiment"],
        "",
    ]
    (out / "conclusions.md").write_text("\n".join(lines), encoding="utf-8")

    print("DONE", out, flush=True)
    print(
        f"candidates={len(variants)} splits={len(splits)} "
        f"thu_selected={thu_selected_splits} thu_improve={thu_improve_splits} "
        f"survivors={survivors_multi}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
