#!/usr/bin/env python3
"""Gate 2 strategy optimization v2 — leverage audit + N1@500 control search.

Offline. No Live/Mirror trading. Simulator unmodified unless a clear defect
is proven (none expected: true_sl+margin intentionally couples leverage to
SL/TP geometry and notional).
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
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG8Strategy
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.costs import commission_for_margin, margin_for_risk_net_commission
from borex.backtest.engine import BacktestConfig
from borex.backtest.margin_stops import margin_stop_move_pct, margin_stop_out_prices
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.backtest.portfolio import Portfolio, PositionSide
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
WARMUP_DAYS = 400
GHOST_PRIME_BARS = 72
LEV_CONTROL = 500.0  # historical N1 economic control — NOT optimized this pass

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
AUG_INDEX = ROOT / "data/runs/mt5_1m_slmult_lev_split/index.json"
JUN_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/decisions_sl_4p0.json"
JUL_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/decisions_sl_4p0.json"
AUG_TAPE_DIR = ROOT / "data/runs/mt5_1m_slmult_lev_split"
CENSUS = (
    ROOT
    / "data/runs/gate2_fidelity_assessment/runs/g2a_20260924_130000/master_trade_census.json"
)
PRIOR_OPT = ROOT / "data/runs/research/gate2_strategy_optimization_20260925_164821"
SEP_TAPE_PRIOR = PRIOR_OPT / "september_todate_decisions_sl_4p0.json"

CONTROL = {
    "strategy": "alexg8",
    "ghost_sl_mult": 4.0,
    "leverage": LEV_CONTROL,
    "rr_factor": 1.0,
    "rr_min": 2.0,
    "rr_max": 5.0,
    "true_sl_rr": 2.0,
    "rr_mode": "dynamic",
    "true_sl": True,
    "size_mode": "margin",
    "force_flat_daily": True,
    "force_flat_friday": False,
    "sides": "both",
    "commission_per_lot": 7.0,
}

# Available August signal tapes (ghost_sl_mult tags)
AUG_GHOSTS = (3.2, 3.6, 4.0, 4.4, 4.8, 5.2, 5.6, 6.0)
RRS_STAGE1 = (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.6, 1.8, 2.0)
SIDES = ("both", "long_only", "short_only")


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


def ghost_tag(g: float) -> str:
    return str(g).replace(".", "p")


# ---------------------------------------------------------------------------
# Part 1 — leverage / sizing audit (no code changes)
# ---------------------------------------------------------------------------


def leverage_sizing_audit() -> dict[str, Any]:
    """Deterministic identical-trade comparison across leverages under N1 knobs."""
    entry = 1.1000
    equity = 1000.0
    cash = 1000.0
    position_size_pct = 0.01
    rr = 2.0  # early-book dynamic fallback (true_sl_rr)
    rows = []
    for lev in (500.0, 1000.0, 5000.0):
        sl, tp = margin_stop_out_prices(entry, PositionSide.LONG, lev, rr)
        move = margin_stop_move_pct(lev)
        risk_budget = cash * position_size_pct
        margin = margin_for_risk_net_commission(
            risk_budget,
            lev,
            commission_per_lot=7.0,
            lot_notional=100_000.0,
            min_commission_per_side=0.04,
        )
        notional = margin * lev
        lots = notional / 100_000.0
        # 1 pip on EURUSD-style (0.0001)
        pip = 0.0001
        pnl_per_pip = notional * (pip / entry)
        # risk at SL: full margin wipe under true_sl/margin_stop
        risk_at_sl = margin
        # strategy structural SL from tape is REPLACED when true_sl=True
        rows.append(
            {
                "leverage": lev,
                "entry": entry,
                "true_sl_sl": sl,
                "true_sl_tp": tp,
                "sl_move_pct": move,
                "sl_distance_price": abs(entry - sl),
                "sl_distance_pips_eurusd": abs(entry - sl) / pip,
                "margin_posted": margin,
                "notional": notional,
                "lots": lots,
                "pnl_per_pip": pnl_per_pip,
                "risk_at_sl_usd": risk_at_sl,
                "commission_round_trip_est": 2
                * commission_for_margin(
                    margin,
                    lev,
                    commission_per_lot=7.0,
                    lot_notional=100_000.0,
                    min_commission_per_side=0.04,
                ),
            }
        )

    chain = {
        "config_path": (
            "true_sl=True + size_mode=margin (N1 / alexg closest-sim default path)"
        ),
        "steps": [
            "1. Signal emits entry + structural SL/TP (ghost_sl_mult affects ghost entry geometry at scan).",
            "2. MultiMarketEngine._open_signal resolves dynamic RR (rr_mode × rr_factor, clamped).",
            "3. BECAUSE true_sl and size_mode=margin: structural SL/TP are REPLACED by "
            "margin_stop_out_prices(entry, side, leverage, rr).",
            "4. SL distance = entry/leverage (fractional). Higher leverage → tighter SL/TP.",
            "5. Margin posted ≈ free_cash × position_size_pct (commission-adjusted via leverage).",
            "6. Notional = margin × leverage → higher leverage → larger lots / $ per pip.",
            "7. PnL = margin × price_move% × leverage; margin_stop loses exactly posted margin.",
            "8. Therefore leverage changes BOTH intended exposure AND stop/target geometry — "
            "not merely unused account capacity.",
        ],
        "identical_inputs": {
            "entry": entry,
            "side": "LONG",
            "equity": equity,
            "cash": cash,
            "position_size_pct": position_size_pct,
            "rr_used_for_true_sl": rr,
            "spread": "n/a (geometry-only diagnostic)",
            "structural_sl_from_strategy": "IGNORED under true_sl",
        },
        "by_leverage": rows,
        "conclusion": (
            "Changing leverage under N1 knobs changes strategy exposure (lots/notional) "
            "AND exit geometry (SL/TP). lev=5000 is not an inert account setting in this "
            "simulator path. This is intentional true_sl+margin semantics, not a silent defect."
        ),
        "simulator_defect": False,
        "code_changed": False,
    }
    return chain


def write_leverage_audit_md(audit: dict, path: Path) -> None:
    lines = [
        "# Leverage / Position Sizing Audit",
        "",
        "## Question",
        "",
        "Does changing leverage change intended strategy exposure, or only account margin capacity?",
        "",
        "## Answer",
        "",
        f"**{audit['conclusion']}**",
        "",
        f"Simulator defect found: `{audit['simulator_defect']}`",
        f"Code changed: `{audit['code_changed']}`",
        "",
        "## Chain",
        "",
    ]
    for s in audit["steps"]:
        lines.append(f"- {s}")
    lines += ["", "## Deterministic comparison (identical entry/RR/cash)", "", "```json"]
    lines.append(json.dumps(audit["by_leverage"], indent=2))
    lines += [
        "```",
        "",
        "## Implication for optimization",
        "",
        "Use historical **lev=500** as the economic control. Do not treat lev=5000 as "
        "the optimization baseline merely because MT5 advertises 5000x — under true_sl it "
        "rebuilds SL/TP at 0.02% risk distance and ~7.7× notional vs lev=500.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Part 8 — expand daily theory↔broker (append-only enrichment)
# ---------------------------------------------------------------------------


def expand_daily_theory_broker(out_dir: Path) -> dict[str, Any]:
    base = build_daily_dataset(CENSUS)
    # Append rows from micro recon + g2f if they add days/tickets not already present
    seen = {
        (
            str(r.get("theory_event_id") or ""),
            str(r.get("broker_ticket") or ""),
        )
        for r in base["trade_rows"]
    }
    extra_rows: list[dict] = []

    # g2f reconciliation
    g2f = ROOT / "data/runs/gate2_fidelity_reconciliation/runs"
    if g2f.is_dir():
        for run in sorted(g2f.glob("g2f_*")):
            recon = run / "trade_reconciliation.json"
            if not recon.is_file():
                continue
            data = json.loads(recon.read_text(encoding="utf-8"))
            for row in data.get("rows") or []:
                # Map g2f-ish fields into census-like shape when possible
                tev = str(row.get("theory_event_id") or row.get("event_id") or "")
                ticket = str(row.get("broker_ticket") or row.get("broker_position") or "")
                key = (tev, ticket)
                if key in seen or (not tev and not ticket):
                    continue
                pseudo = {
                    "signal_time": row.get("signal_time")
                    or row.get("theory_time")
                    or row.get("entry_time"),
                    "theory_event_id": tev,
                    "execution_id": row.get("execution_id"),
                    "broker_ticket": ticket,
                    "symbol": row.get("symbol"),
                    "direction": row.get("direction") or row.get("side"),
                    "theory_entry": row.get("theory_entry") or row.get("theory_entry_price"),
                    "broker_entry": row.get("broker_entry") or row.get("broker_entry_price"),
                    "entry_delta_pips": row.get("entry_delta_pips"),
                    "theory_sl": row.get("theory_sl"),
                    "broker_sl": row.get("broker_sl"),
                    "theory_tp": row.get("theory_tp"),
                    "broker_tp": row.get("broker_tp"),
                    "broker_exit_time": row.get("broker_exit_time") or row.get("exit_time"),
                    "broker_exit_reason": row.get("broker_exit_reason")
                    or row.get("exit_reason"),
                    "sim_exit_reason": row.get("sim_exit_reason"),
                    "mapping_status": row.get("mapping_status") or row.get("status"),
                    "exit_event_agreement": row.get("exit_event_agreement"),
                    "exclusion_reason": row.get("exclusion_reason")
                    or row.get("exclude_reason"),
                    "discrepancy_classes": row.get("discrepancy_classes") or [],
                    "pnl_excluded_from_fidelity": bool(row.get("excluded")),
                    "source_run": run.name,
                    "experiment_class": row.get("experiment_class") or "reconciliation",
                    "evidence_weight": "g2f",
                }
                seen.add(key)
                extra_rows.append(trade_row_from_census(pseudo))

    # Prior campaign daily artifact (append-only reference)
    prior_daily = PRIOR_OPT / "daily_theory_broker.json"
    prior_note = None
    if prior_daily.is_file():
        prior = json.loads(prior_daily.read_text(encoding="utf-8"))
        prior_note = {
            "prior_run": str(PRIOR_OPT),
            "prior_days": [d["day"] for d in prior.get("daily_aggregates") or []],
            "prior_n": len(prior.get("trade_rows") or []),
        }

    all_rows = list(base["trade_rows"]) + extra_rows
    by_day: dict[str, list] = defaultdict(list)
    for r in all_rows:
        by_day[r["day"]].append(r)
    daily = [aggregate_day(by_day[d]) for d in sorted(by_day)]
    dataset = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_census": str(CENSUS),
        "append_only_prior": prior_note,
        "extra_rows_added": len(extra_rows),
        "trade_rows": all_rows,
        "daily_aggregates": daily,
        "limitations": base["limitations"]
        + [
            "Expanded from g2f reconciliation where tickets were not already in census.",
            "Still thin calendar coverage; strategy conclusions must not rest on this alone.",
        ],
    }
    write_daily_artifacts(dataset, out_dir)
    # also plain names requested
    (out_dir / "daily_theory_broker.json").write_text(
        json.dumps(dataset, indent=2, default=str), encoding="utf-8"
    )
    return dataset


# ---------------------------------------------------------------------------
# Backtest helpers (reuse v1 patterns; fixed lev=500)
# ---------------------------------------------------------------------------


def load_cache_candles(symbol: str, interval: str, start: datetime, end: datetime):
    path = _cache_path(symbol, interval)
    if not path.is_file():
        return []
    df = pd.read_parquet(path)
    candles = _frame_to_candles(df)
    if interval.strip().lower() in {"1h", "h1"}:
        candles = _dedupe_hourly(candles)
    candles = _slice_candles(candles, start, end)
    return _drop_forming_bar(candles, interval)


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


def filter_sides(tape: list[dict], sides: str) -> list[dict]:
    if sides == "both":
        return list(tape)
    if sides == "long_only":
        return [r for r in tape if str(r.get("action", "")).lower() in {"buy", "long"}]
    if sides == "short_only":
        return [r for r in tape if str(r.get("action", "")).lower() in {"sell", "short"}]
    raise ValueError(sides)


def _cfg(spreads: dict[str, float], *, rr_factor: float) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV_CONTROL,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(CONTROL["true_sl_rr"]),
        rr_mode=str(CONTROL["rr_mode"]),
        rr_factor=float(rr_factor),
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
    by_pair = Counter(t["symbol"] for t in trades)
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
        "other_exits": {
            k: v
            for k, v in exits.items()
            if k not in {"take_profit", "daily_close", "margin_stop"}
        },
        "long_count": int(sides.get("long", 0) + sides.get("buy", 0)),
        "short_count": int(sides.get("short", 0) + sides.get("sell", 0)),
        "top_pair_share": (by_pair.most_common(1)[0][1] / len(trades)) if trades else None,
        "worst_day_pnl": min(day_pnls) if day_pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "max_consecutive_losses": max_cl,
        "n_trading_days": len(by_day),
        "exit_mix": dict(exits),
    }


def run_replay(period: dict, tape: list[dict], rr_factor: float, ghost: float) -> dict:
    strategy = AlexG8Strategy(
        min_rr=float(CONTROL["true_sl_rr"]),
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(ghost),
    )
    engine = MultiMarketEngine(
        strategy, _cfg(period["spreads"], rr_factor=rr_factor), max_positions=0
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
    window = [
        {
            "symbol": t.symbol,
            "side": t.side.value if hasattr(t.side, "value") else str(t.side),
            "entry_time": str(t.entry_time),
            "exit_time": str(t.exit_time),
            "pnl": float(t.pnl),
            "exit_reason": t.exit_reason,
            "sl": float(t.stop_loss or 0),
            "tp": float(t.take_profit or 0),
            "entry": float(t.entry_price),
        }
        for t in result.trades
        if _ts(t.entry_time) >= period["entry_start"]
    ]
    s = summarize(
        window,
        len(tape),
        float(result.max_drawdown_pct),
        period["entry_start"],
        period["data"][period["master"]][-1].timestamp,
    )
    # geometry sample for causal analysis
    if window:
        sl_dists = [abs(t["entry"] - t["sl"]) / t["entry"] for t in window if t["sl"]]
        s["median_sl_dist_pct"] = float(statistics.median(sl_dists)) if sl_dists else None
        s["mean_sl_dist_pct"] = float(statistics.mean(sl_dists)) if sl_dists else None
    else:
        s["median_sl_dist_pct"] = None
        s["mean_sl_dist_pct"] = None
    return s


def load_period(
    name: str,
    *,
    index_path: Path,
    symbols: list[str],
    spreads: dict,
    entry_start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> dict:
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    if entry_start is None:
        entry_start = _ts(idx["eval_start"]).floor("h")
    if end is None:
        end = _ts(idx["last_bar"]) + pd.Timedelta(hours=1)
    h1_start = (end - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    end_py = end.to_pydatetime()
    print(f"[{name}] loading cache…", flush=True)
    data: dict = {}
    m1: dict = {}
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
    master_bars = data[master]
    entry_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= entry_start), None
    )
    warmup = int(AlexG8Strategy().min_bars)
    prime_i = max(warmup, entry_i - GHOST_PRIME_BARS)
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


def load_aug_tapes(data: dict) -> dict[float, list[dict]]:
    out = {}
    for g in AUG_GHOSTS:
        path = AUG_TAPE_DIR / f"decisions_sl_{ghost_tag(g)}.json"
        if not path.is_file():
            print(f"  missing tape {path.name}", flush=True)
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        out[g] = remap_tape(raw, data)
        print(f"  ghost={g} tape n={len(out[g])}", flush=True)
    return out


def cfg_id(ghost, rr, sides) -> str:
    return f"g{ghost_tag(ghost)}_lev{int(LEV_CONTROL)}_rr{str(rr).replace('.', 'p')}_{sides}"


def label_vs_control(cand_aug: dict, ctrl_aug: dict) -> str:
    """August-only ranking label vs control."""
    if cand_aug.get("mean_trade") is None or ctrl_aug.get("mean_trade") is None:
        return "mixed"
    if cand_aug.get("PF") is None or ctrl_aug.get("PF") is None:
        return "mixed"
    better = float(cand_aug["PF"]) > float(ctrl_aug["PF"]) and float(
        cand_aug["mean_trade"]
    ) > float(ctrl_aug["mean_trade"])
    worse = float(cand_aug["PF"]) < float(ctrl_aug["PF"]) and float(
        cand_aug["mean_trade"]
    ) < float(ctrl_aug["mean_trade"])
    if better:
        return "improved_versus_control"
    if worse:
        return "degraded_versus_control"
    return "mixed"


def label_validation(by_period: dict, ctrl_by: dict) -> str:
    better = worse = 0
    for p in ("june", "july"):
        c, n = by_period.get(p), ctrl_by.get(p)
        if not c or not n or c.get("PF") is None or n.get("PF") is None:
            continue
        if c.get("mean_trade") is None or n.get("mean_trade") is None:
            continue
        same = math.isclose(float(c["PF"]), float(n["PF"])) and math.isclose(
            float(c["mean_trade"]), float(n["mean_trade"])
        )
        if (
            float(c["PF"]) >= float(n["PF"])
            and float(c["mean_trade"]) >= float(n["mean_trade"])
            and not same
        ):
            better += 1
        elif (
            float(c["PF"]) <= float(n["PF"])
            and float(c["mean_trade"]) <= float(n["mean_trade"])
            and not same
        ):
            worse += 1
    if better == 2 and worse == 0:
        return "improved_versus_control"
    if worse == 2 and better == 0:
        return "degraded_versus_control"
    if better and worse:
        return "mixed_across_validation_periods"
    if better == 1 and worse == 0:
        return "unstable_across_validation_periods"
    return "mixed_across_validation_periods"


def causal_delta(cand: dict, ctrl: dict) -> dict:
    """Describe why metrics moved (exit mix / geometry)."""
    out = {}
    for k in ("TP", "daily_close", "margin_stop", "fills", "WR", "mean_trade", "PF"):
        if cand.get(k) is not None and ctrl.get(k) is not None:
            out[f"delta_{k}"] = cand[k] - ctrl[k]
    if cand.get("median_sl_dist_pct") and ctrl.get("median_sl_dist_pct"):
        out["delta_median_sl_dist_pct"] = (
            cand["median_sl_dist_pct"] - ctrl["median_sl_dist_pct"]
        )
    notes = []
    if out.get("delta_TP", 0) < 0 and out.get("delta_mean_trade", 0) > 0:
        notes.append("fewer TPs but higher mean trade (winners larger / losers smaller)")
    if out.get("delta_TP", 0) > 0 and out.get("delta_WR", 0) > 0:
        notes.append("more TPs and higher WR")
    if out.get("delta_margin_stop", 0) < 0:
        notes.append("fewer margin stops")
    if out.get("delta_daily_close", 0) != 0:
        notes.append("daily_close count shifted")
    out["notes"] = notes
    return out


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"gate2_strategy_optimization_v2_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    # Part 1
    audit = leverage_sizing_audit()
    (out / "leverage_diagnostic.json").write_text(
        json.dumps(audit, indent=2), encoding="utf-8"
    )
    write_leverage_audit_md(audit, out / "leverage_sizing_audit.md")
    print("Part1 leverage audit done; defect=", audit["simulator_defect"], flush=True)

    # Part 8
    daily = expand_daily_theory_broker(out)
    print(
        f"Part8 daily theory<->broker days={ [d['day'] for d in daily['daily_aggregates']] } "
        f"n={len(daily['trade_rows'])} extra={daily['extra_rows_added']}",
        flush=True,
    )

    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    uhash = universe_hash(symbols)

    # Load periods
    periods = {
        "august": load_period("august", index_path=AUG_INDEX, symbols=symbols, spreads=spreads),
        "june": load_period("june", index_path=JUN_INDEX, symbols=symbols, spreads=spreads),
        "july": load_period("july", index_path=JUL_INDEX, symbols=symbols, spreads=spreads),
    }
    # Sep-to-date descriptive
    m1_ends = []
    for sym in symbols:
        p = _cache_path(sym, "1m")
        if p.is_file():
            m1_ends.append(pd.to_datetime(pd.read_parquet(p, columns=["timestamp"])["timestamp"], utc=True).max())
    sep_end = min(m1_ends) + pd.Timedelta(minutes=1)
    periods["september_todate"] = load_period(
        "september_todate",
        index_path=AUG_INDEX,
        symbols=symbols,
        spreads=spreads,
        entry_start=_ts("2026-09-03T03:00:00+00:00"),
        end=sep_end,
    )

    print("Loading August ghost tapes…", flush=True)
    aug_tapes = load_aug_tapes(periods["august"]["data"])
    jun_tape = remap_tape(json.loads(JUN_TAPE.read_text(encoding="utf-8")), periods["june"]["data"])
    jul_tape = remap_tape(json.loads(JUL_TAPE.read_text(encoding="utf-8")), periods["july"]["data"])
    if SEP_TAPE_PRIOR.is_file():
        sep_raw = json.loads(SEP_TAPE_PRIOR.read_text(encoding="utf-8"))
        # Sep tape from prior scan already has indices for prior load window —
        # remap by time onto current sep period data
        sep_tape = remap_tape(sep_raw, periods["september_todate"]["data"])
        print(f"Sep tape remapped n={len(sep_tape)} from prior campaign", flush=True)
    else:
        sep_tape = []
        print("WARNING: no prior Sep tape; skipping Sep descriptive", flush=True)

    search_manifest = {
        "control": CONTROL,
        "leverage_fixed": LEV_CONTROL,
        "leverage_optimized": False,
        "stage1": {
            "ghost_sl_mult": list(AUG_GHOSTS),
            "rr_factor": list(RRS_STAGE1),
            "sides": list(SIDES),
            "period": "august_IS",
            "note": "Cartesian on August only; rank on August before Jun/Jul peek",
        },
        "alexg8_exposed_params_inspected": {
            "ghost_sl_mult": "signal-side ghost SL distance (scan tape)",
            "ablation.session": "default all — not swept this pass",
            "ablation.htf_bias": "default off (video2_ghost_all_sessions) — not swept",
            "min_rr / true_sl_rr": "RR base for dynamic/true_sl path",
            "rr_factor": "TP multiplier (exec)",
            "peer_blend": "0.0 in N1 — not swept",
        },
        "true_sl_implication": (
            "Under true_sl, exit SL/TP = f(leverage, rr); ghost_sl_mult changes "
            "which entries appear on the tape, not the final SL distance."
        ),
    }
    (out / "search_manifest.json").write_text(
        json.dumps(search_manifest, indent=2), encoding="utf-8"
    )

    # -------- STAGE 1: August Cartesian --------
    stage1 = []
    total = len(AUG_GHOSTS) * len(RRS_STAGE1) * len(SIDES)
    n = 0
    t0 = time.perf_counter()
    for ghost, rr, sides in itertools.product(AUG_GHOSTS, RRS_STAGE1, SIDES):
        n += 1
        if ghost not in aug_tapes:
            continue
        tape = filter_sides(aug_tapes[ghost], sides)
        s = run_replay(periods["august"], tape, rr, ghost)
        row = {
            "id": cfg_id(ghost, rr, sides),
            "stage": 1,
            "ghost_sl_mult": ghost,
            "rr_factor": rr,
            "sides": sides,
            "leverage": LEV_CONTROL,
            "august": s,
        }
        stage1.append(row)
        if n % 20 == 0 or n == total:
            print(
                f"  stage1 {n}/{total} last={row['id']} "
                f"PF={s.get('PF')} mean={s.get('mean_trade')} "
                f"elapsed={(time.perf_counter()-t0)/60:.1f}m",
                flush=True,
            )

    # Control on August
    ctrl_aug = next(
        r
        for r in stage1
        if r["ghost_sl_mult"] == 4.0 and r["rr_factor"] == 1.0 and r["sides"] == "both"
    )
    for r in stage1:
        r["august_label_vs_control"] = label_vs_control(r["august"], ctrl_aug["august"])
        r["august_causal"] = causal_delta(r["august"], ctrl_aug["august"])

    # Rank August-only: improved, then by mean_trade, PF (no Jun/jul)
    improved = [r for r in stage1 if r["august_label_vs_control"] == "improved_versus_control"]
    improved.sort(
        key=lambda r: (
            r["august"].get("mean_trade") or -1e9,
            r["august"].get("PF") or 0,
            r["august"].get("net_pnl") or 0,
        ),
        reverse=True,
    )
    # Also keep best both-sides differently from directional
    top_both = [r for r in improved if r["sides"] == "both"][:5]
    top_dir = [r for r in improved if r["sides"] != "both"][:3]
    shortlist_aug = []
    seen_ids = set()
    for r in top_both + top_dir:
        if r["id"] not in seen_ids:
            shortlist_aug.append(r)
            seen_ids.add(r["id"])
    # Always include control
    if ctrl_aug["id"] not in seen_ids:
        shortlist_aug.insert(0, ctrl_aug)

    print(
        f"Stage1 done n={len(stage1)} improved={len(improved)} "
        f"shortlist_aug={len(shortlist_aug)}",
        flush=True,
    )

    # -------- STAGE 2: local refine around top both-sides region --------
    stage2 = []
    if top_both:
        g0 = top_both[0]["ghost_sl_mult"]
        r0 = top_both[0]["rr_factor"]
        # neighbors among available ghosts/rrs
        g_idx = list(AUG_GHOSTS).index(g0) if g0 in AUG_GHOSTS else 0
        ghosts_loc = [
            AUG_GHOSTS[i]
            for i in range(max(0, g_idx - 1), min(len(AUG_GHOSTS), g_idx + 2))
        ]
        rr_list = list(RRS_STAGE1)
        r_idx = rr_list.index(r0) if r0 in rr_list else 0
        rrs_loc = [
            rr_list[i]
            for i in range(max(0, r_idx - 2), min(len(rr_list), r_idx + 3))
        ]
        for ghost, rr, sides in itertools.product(ghosts_loc, rrs_loc, ("both",)):
            cid = cfg_id(ghost, rr, sides)
            if any(x["id"] == cid for x in stage1):
                # reuse
                existing = next(x for x in stage1 if x["id"] == cid)
                stage2.append({**existing, "stage": 2, "refine": True})
                continue
            tape = filter_sides(aug_tapes[ghost], sides)
            s = run_replay(periods["august"], tape, rr, ghost)
            row = {
                "id": cid,
                "stage": 2,
                "refine": True,
                "ghost_sl_mult": ghost,
                "rr_factor": rr,
                "sides": sides,
                "leverage": LEV_CONTROL,
                "august": s,
            }
            row["august_label_vs_control"] = label_vs_control(s, ctrl_aug["august"])
            row["august_causal"] = causal_delta(s, ctrl_aug["august"])
            stage2.append(row)
            stage1.append(row)  # preserve every tested config
        print(f"Stage2 refine around g={g0} rr={r0} n_extra region done", flush=True)

    # Re-pick shortlist after refine (August only)
    improved = [r for r in stage1 if r["august_label_vs_control"] == "improved_versus_control"]
    improved.sort(
        key=lambda r: (
            r["august"].get("mean_trade") or -1e9,
            r["august"].get("PF") or 0,
        ),
        reverse=True,
    )
    shortlist_aug = []
    seen_ids = set()
    for r in [ctrl_aug] + [x for x in improved if x["sides"] == "both"][:5] + [
        x for x in improved if x["sides"] != "both"
    ][:2]:
        if r["id"] not in seen_ids:
            shortlist_aug.append(r)
            seen_ids.add(r["id"])

    # -------- Validate shortlist on June/July (and Sep descriptive) --------
    # Only ghost=4.0 has Jun/Jul tapes without re-scan. For other ghosts, scan.
    validated = []
    ctrl_full = {
        "id": ctrl_aug["id"],
        "ghost_sl_mult": 4.0,
        "rr_factor": 1.0,
        "sides": "both",
        "leverage": LEV_CONTROL,
        "by_period": {},
    }

    def tape_for(period_name: str, ghost: float, sides: str) -> list[dict] | None:
        if period_name == "august":
            if ghost not in aug_tapes:
                return None
            return filter_sides(aug_tapes[ghost], sides)
        if period_name == "june":
            if ghost != 4.0:
                return None  # filled by scan cache below
            return filter_sides(jun_tape, sides)
        if period_name == "july":
            if ghost != 4.0:
                return None
            return filter_sides(jul_tape, sides)
        if period_name == "september_todate":
            if ghost != 4.0 or not sep_tape:
                return None
            return filter_sides(sep_tape, sides)
        return None

    # Scan cache for non-4.0 ghosts on jun/jul if needed
    scan_cache: dict[tuple[str, float], list[dict]] = {}

    def ensure_tape(period_name: str, ghost: float, sides: str) -> list[dict] | None:
        t = tape_for(period_name, ghost, sides)
        if t is not None:
            return t
        if period_name not in ("june", "july") or ghost == 4.0:
            return None
        key = (period_name, ghost)
        if key not in scan_cache:
            print(f"  SCAN {period_name} ghost={ghost} (shortlist only)…", flush=True)
            P = periods[period_name]
            strategy = AlexG8Strategy(
                min_rr=float(CONTROL["true_sl_rr"]),
                execution_interval="1h",
                peer_blend=0.0,
                ghost_sl_mult=float(ghost),
            )
            engine = MultiMarketEngine(
                strategy, _cfg(P["spreads"], rr_factor=1.0), max_positions=0
            )
            engine._progress_every = 500
            collected: list[dict] = []
            engine.run(
                P["data"],
                timeframe="1h+1m",
                master_symbol=P["master"],
                same_bar_exit=False,
                start_master_i=P["prime_i"],
                ltf_by_symbol=P["m1"],
                collect_decisions=collected,
            )
            scan_cache[key] = collected
            print(f"  scanned n={len(collected)}", flush=True)
        return filter_sides(scan_cache[key], sides)

    # Control full periods
    for pname in ("june", "july", "august", "september_todate"):
        tape = ensure_tape(pname, 4.0, "both")
        if tape is None:
            continue
        ctrl_full["by_period"][pname] = run_replay(periods[pname], tape, 1.0, 4.0)
        print(
            f"CONTROL {pname}: PF={ctrl_full['by_period'][pname].get('PF')} "
            f"net={ctrl_full['by_period'][pname].get('net_pnl')}",
            flush=True,
        )

    for r in shortlist_aug:
        by_period = {"august": r["august"]}
        for pname in ("june", "july", "september_todate"):
            tape = ensure_tape(pname, float(r["ghost_sl_mult"]), r["sides"])
            if tape is None:
                print(f"  skip validate {r['id']} on {pname}", flush=True)
                continue
            by_period[pname] = run_replay(
                periods[pname], tape, float(r["rr_factor"]), float(r["ghost_sl_mult"])
            )
            print(
                f"  {r['id']} {pname}: PF={by_period[pname].get('PF')} "
                f"mean={by_period[pname].get('mean_trade')}",
                flush=True,
            )
        item = {
            "id": r["id"],
            "ghost_sl_mult": r["ghost_sl_mult"],
            "rr_factor": r["rr_factor"],
            "sides": r["sides"],
            "leverage": LEV_CONTROL,
            "is_control": r["id"] == ctrl_aug["id"],
            "august_label_vs_control": r.get("august_label_vs_control"),
            "august_causal": r.get("august_causal"),
            "by_period": by_period,
            "validation_label": label_validation(by_period, ctrl_full["by_period"]),
            "causal_june": causal_delta(by_period["june"], ctrl_full["by_period"]["june"])
            if "june" in by_period and "june" in ctrl_full["by_period"]
            else None,
            "causal_july": causal_delta(by_period["july"], ctrl_full["by_period"]["july"])
            if "july" in by_period and "july" in ctrl_full["by_period"]
            else None,
        }
        validated.append(item)

    # Write artifacts
    (out / "control_metrics.json").write_text(
        json.dumps(ctrl_full, indent=2), encoding="utf-8"
    )
    (out / "shortlisted_candidates.json").write_text(
        json.dumps(validated, indent=2), encoding="utf-8"
    )
    (out / "per_period_metrics.json").write_text(
        json.dumps(
            {
                "control": ctrl_full,
                "shortlist": validated,
                "august_improved_n": len(improved),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    flat = []
    for r in stage1:
        flat.append(
            {
                "id": r["id"],
                "stage": r.get("stage", 1),
                "ghost_sl_mult": r["ghost_sl_mult"],
                "rr_factor": r["rr_factor"],
                "sides": r["sides"],
                "leverage": LEV_CONTROL,
                "period": "august",
                "period_role": "optimization_IS",
                "label_vs_control": r.get("august_label_vs_control"),
                **{
                    k: r["august"].get(k)
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
                        "long_count",
                        "short_count",
                        "worst_day_pnl",
                        "worst_trade",
                        "max_consecutive_losses",
                        "top_pair_share",
                        "median_sl_dist_pct",
                    )
                },
            }
        )
    for item in validated:
        for pname, s in item["by_period"].items():
            if pname == "august":
                continue
            flat.append(
                {
                    "id": item["id"],
                    "stage": "validation",
                    "ghost_sl_mult": item["ghost_sl_mult"],
                    "rr_factor": item["rr_factor"],
                    "sides": item["sides"],
                    "leverage": LEV_CONTROL,
                    "period": pname,
                    "period_role": {
                        "june": "validation_OOS",
                        "july": "validation_OOS",
                        "september_todate": "descriptive",
                    }.get(pname),
                    "label_vs_control": item.get("validation_label"),
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
                            "long_count",
                            "short_count",
                            "worst_day_pnl",
                            "worst_trade",
                            "max_consecutive_losses",
                            "top_pair_share",
                            "median_sl_dist_pct",
                        )
                    },
                }
            )

    with (out / "all_results.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
        w.writeheader()
        w.writerows(flat)

    consistent = [
        v
        for v in validated
        if not v["is_control"]
        and v["validation_label"] == "improved_versus_control"
        and v["august_label_vs_control"] == "improved_versus_control"
    ]

    provenance = {
        "data": "MT5 data/cache/mt5/utc_v2",
        "spreads": "June Raw N1",
        "universe_sha256": uhash,
        "universe_n": 60,
        "simulator": "MultiMarketEngine FROZEN_FOR_STRATEGY_RESEARCH",
        "simulator_fingerprint": {
            "git": _git(),
            "true_sl": True,
            "size_mode": "margin",
            "leverage_control": LEV_CONTROL,
        },
        "no_live": True,
        "no_mirror_trading": True,
    }
    (out / "data_provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )

    conclusions = {
        "leverage_semantics": audit["conclusion"],
        "simulator_defect": False,
        "control_id": ctrl_full["id"],
        "control_by_period": {
            p: {
                k: ctrl_full["by_period"][p].get(k)
                for k in ("fills", "PF", "net_pnl", "mean_trade", "MDD", "TP", "margin_stop")
            }
            for p in ctrl_full["by_period"]
        },
        "n_configs_stage1": len(stage1),
        "august_improved_n": len(improved),
        "shortlist": [
            {
                "id": v["id"],
                "params": {
                    "ghost": v["ghost_sl_mult"],
                    "rr": v["rr_factor"],
                    "sides": v["sides"],
                },
                "august_label": v["august_label_vs_control"],
                "validation_label": v["validation_label"],
                "by_period": {
                    p: {
                        k: v["by_period"][p].get(k)
                        for k in ("PF", "net_pnl", "mean_trade", "MDD")
                    }
                    for p in v["by_period"]
                },
            }
            for v in validated
        ],
        "consistently_improved_vs_n1_500": [c["id"] for c in consistent],
        "daily_theory_broker_days": [d["day"] for d in daily["daily_aggregates"]],
        "daily_theory_broker_n": len(daily["trade_rows"]),
        "mirror_candidates": [c["id"] for c in consistent]
        or ["none_with_consistent_jun_jul_improvement"],
        "next_experiment": (
            "If any consistently_improved_vs_n1_500: authorized Mirror paper for "
            "CONTROL + that candidate. Else: deepen causal analysis on August-best "
            "both-sides region without deploying directional packs; finish Sep M1 month."
        ),
    }
    (out / "conclusions.json").write_text(
        json.dumps(conclusions, indent=2), encoding="utf-8"
    )

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git(),
        "experiment": "gate2_strategy_optimization_v2",
        "out_dir": str(out),
        "n_configs_tested": len(stage1),
        "control": CONTROL,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Report
    lines = [
        "# Gate 2 Strategy Optimization v2",
        "",
        f"Generated: {manifest['created_at']}",
        f"Commit: `{manifest['git_commit']}`",
        "",
        "## 1. Leverage / sizing semantics",
        "",
        audit["conclusion"],
        "",
        "See `leverage_sizing_audit.md` and `leverage_diagnostic.json`.",
        "",
        "## 2. Control (historical N1@500)",
        "",
    ]
    for p, s in ctrl_full["by_period"].items():
        lines.append(
            f"- **{p}**: fills={s.get('fills')} PF={s.get('PF')} net={s.get('net_pnl')} "
            f"mean={s.get('mean_trade')} MDD={s.get('MDD')} "
            f"TP={s.get('TP')} margin_stop={s.get('margin_stop')} daily_close={s.get('daily_close')}"
        )
    lines += [
        "",
        f"## 3. Search",
        "",
        f"- Configs tested (August stage1+refine): **{len(stage1)}**",
        f"- August improved vs control: **{len(improved)}**",
        f"- Leverage: fixed at {LEV_CONTROL} (not optimized)",
        "",
        "## 4. Shortlist + validation",
        "",
    ]
    for v in validated:
        lines.append(f"### `{v['id']}`")
        lines.append(
            f"- August: {v['august_label_vs_control']} | Validation: {v['validation_label']}"
        )
        lines.append(f"- Causal Aug: {v.get('august_causal')}")
        for p, s in v["by_period"].items():
            lines.append(
                f"  - {p}: PF={s.get('PF')} net={s.get('net_pnl')} mean={s.get('mean_trade')} "
                f"TP={s.get('TP')} dc={s.get('daily_close')} ms={s.get('margin_stop')}"
            )
        lines.append("")
    lines += [
        "## 5. Consistent improvers vs N1@500",
        "",
        json.dumps(conclusions["consistently_improved_vs_n1_500"], indent=2),
        "",
        "## 6. Daily Theory ↔ Broker",
        "",
        f"- Days: {conclusions['daily_theory_broker_days']}",
        f"- Trades: {conclusions['daily_theory_broker_n']}",
        "",
        "## 7. Next",
        "",
        conclusions["next_experiment"],
        "",
        "No Live/Mirror trading performed.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(conclusions, indent=2), flush=True)
    print(f"DONE -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
