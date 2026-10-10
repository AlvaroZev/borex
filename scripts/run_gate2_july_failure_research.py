#!/usr/bin/env python3
"""Gate 2 July failure research — forensic first, then optional filter test.

Control: N1@500-equivalent (alexg8 ghost=4, rr_factor=1, rr_min=2, both sides).
Offline only. No Live/Mirror trading. Simulator unmodified.

Protocol
--------
1. Descriptive forensics (July vs June vs August) — no optimization.
2. Inventory existing alexg8 filter levers.
3. Pre-register ONE hypothesis only if a credible EXISTING lever maps to the
   largest measurable failure class. Otherwise STOP (no contaminated search).
4. If a hypothesis is registered: select on June+August, freeze, validate July.
5. Append daily Theory↔Broker; September-to-date descriptive only.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg.ablation import AblationConfig
from borex.alexg.sessions import TradingSession, classify_session, in_session
from borex.alexg.strategy7_aligned import AlexG7AlignedStrategy
from borex.alexg.strategy8 import AlexG8Strategy
from borex.gate2.daily_theory_broker import (
    aggregate_day,
    build_daily_dataset,
    write_daily_artifacts,
)

SOURCES = {
    "june": ROOT / "data/runs/mt5_1m_n1_oos_jun2026/sl_4p0_lev_500.json",
    "july": ROOT / "data/runs/mt5_1m_n1_oos_jul2026/sl_4p0_lev_500.json",
    "august": ROOT / "data/runs/mt5_1m_n1_tpfactor/sl_4p0_lev_500_rr_1p0.json",
}
PRIOR_RR = ROOT / "data/runs/research/gate2_rr_research_20260925_185421"
PRIOR_V2 = ROOT / "data/runs/research/gate2_strategy_optimization_v2_20260925_172803"
PRIOR_DAILY = PRIOR_RR / "daily_theory_broker.json"
CENSUS = (
    ROOT
    / "data/runs/gate2_fidelity_assessment/runs/g2a_20260924_130000/master_trade_census.json"
)
LONG_ONLY = ROOT / "data/runs/research/n1_long_only_ablation_20260925_030340"

CONTROL = dict(
    strategy="alexg8",
    ghost_sl_mult=4.0,
    leverage=500.0,
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
    ablation_session="all",
)

POSTHOC_RR25 = dict(
    strategy="alexg8",
    ghost_sl_mult=4.0,
    leverage=500.0,
    rr_factor=0.9,
    rr_min=2.5,
    rr_max=5.0,
    sides="both",
    note="REFERENCE ONLY — not pre-registered, not for deployment",
)


def _git() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def _mean(xs: list[float]) -> float | None:
    return float(statistics.mean(xs)) if xs else None


def _median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def _pf(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    gp = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    if gl == 0:
        return float("inf") if gp > 0 else None
    return float(gp / abs(gl))


def _wr(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    return sum(1 for x in pnls if x > 0) / len(pnls)


def bucket_stats(pnls: list[float]) -> dict[str, Any]:
    gp = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    return {
        "n": len(pnls),
        "sum_pnl": float(sum(pnls)) if pnls else 0.0,
        "mean_pnl": _mean(pnls),
        "median_pnl": _median(pnls),
        "wr": _wr(pnls),
        "pf": _pf(pnls),
        "gross_profit": float(gp),
        "gross_loss": float(gl),
        "win_n": sum(1 for x in pnls if x > 0),
        "loss_n": sum(1 for x in pnls if x <= 0),
    }


def parse_pattern(p: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "raw": p,
        "structure": None,
        "tf": None,
        "trigger": None,
        "bias": None,
        "trend": None,
        "pat": None,
        "ret": None,
        "ghost": None,
        "session_tag": None,
        "fill": None,
        "aoi_mid": None,
        "aoi_lo": None,
        "aoi_hi": None,
    }
    if not p:
        return out
    parts = str(p).split("|")
    if len(parts) >= 5:
        out["structure"] = parts[2]
        out["tf"] = parts[3]
        out["trigger"] = parts[4]
    for part in parts:
        if part.startswith("bias="):
            out["bias"] = part.split("=", 1)[1]
        elif part.startswith("trend"):
            out["trend"] = part
        elif part.startswith("pat"):
            out["pat"] = part
        elif part.startswith("ret"):
            out["ret"] = part
        elif part.startswith("ghost"):
            out["ghost"] = part
        elif part.startswith("sess="):
            out["session_tag"] = part.split("=", 1)[1]
        elif part.startswith("fill:"):
            out["fill"] = part.split(":", 1)[1]
        elif part.startswith("g:"):
            bits = part.split(":")
            if len(bits) >= 5:
                try:
                    out["aoi_mid"] = float(bits[2])
                    out["aoi_lo"] = float(bits[3])
                    out["aoi_hi"] = float(bits[4])
                except ValueError:
                    pass
    return out


def exclusive_session_bucket(hour: int) -> str:
    """Non-overlapping session labels for breakdown (UTC)."""
    if 12 <= hour < 16:
        return "overlap"
    if 7 <= hour < 12:
        return "london_pre_overlap"
    if 16 <= hour < 21:
        return "newyork_post_overlap"
    if 0 <= hour < 7:
        return "asia"
    return "off_hours"


def load_month(label: str, path: Path) -> dict[str, Any]:
    d = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for t in d.get("trades") or []:
        entry_t = pd.Timestamp(t["entry_time"])
        exit_t = pd.Timestamp(t["exit_time"])
        if entry_t.tzinfo is None:
            entry_t = entry_t.tz_localize("UTC")
        else:
            entry_t = entry_t.tz_convert("UTC")
        hour = int(entry_t.hour)
        dow = int(entry_t.dayofweek)
        pat = parse_pattern(t.get("pattern"))
        sess_cls = classify_session(entry_t)
        hold_h = (exit_t - entry_t).total_seconds() / 3600.0
        rows.append(
            {
                "month": label,
                "symbol": t["symbol"],
                "side": str(t["side"]).lower(),
                "entry_time": str(entry_t),
                "exit_time": str(exit_t),
                "entry_hour_utc": hour,
                "dow": dow,
                "dow_name": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dow],
                "hold_hours": hold_h,
                "entry": float(t["entry"]),
                "exit": float(t["exit"]),
                "sl": float(t["sl"]),
                "tp": float(t["tp"]),
                "pnl": float(t["pnl"]),
                "margin": float(t.get("margin") or 0.0),
                "rr": float(t.get("rr") or 0.0),
                "exit_reason": t["exit_reason"],
                "structure": pat["structure"],
                "tf": pat["tf"],
                "trigger": pat["trigger"],
                "session_tag": pat["session_tag"],
                "fill": pat["fill"],
                "session_classified": sess_cls.value if sess_cls else "none",
                "session_exclusive": exclusive_session_bucket(hour),
                "in_asia": in_session(entry_t, TradingSession.ASIA),
                "in_london": in_session(entry_t, TradingSession.LONDON),
                "in_newyork": in_session(entry_t, TradingSession.NEWYORK),
                "in_overlap": in_session(entry_t, TradingSession.OVERLAP),
                "day": str(entry_t.date()),
            }
        )
    s = d.get("summary") or {}
    return {
        "label": label,
        "source": str(path.as_posix()),
        "intents": d.get("intents"),
        "summary": s,
        "knobs": d.get("knobs"),
        "entry_start": d.get("entry_start"),
        "last_bar": d.get("last_bar"),
        "ghost_sl_mult": d.get("ghost_sl_mult"),
        "leverage": d.get("leverage"),
        "rr_factor": d.get("rr_factor"),
        "trades": rows,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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


def group_breakdown(
    trades: list[dict[str, Any]], key: str, *, total_net: float
) -> list[dict[str, Any]]:
    by: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by[t.get(key)].append(t)
    rows = []
    for k, ts in by.items():
        pnls = [t["pnl"] for t in ts]
        st = bucket_stats(pnls)
        exits = Counter(t["exit_reason"] for t in ts)
        n = max(1, len(ts))
        st.update(
            {
                key: k,
                "tp_n": exits.get("take_profit", 0),
                "daily_close_n": exits.get("daily_close", 0),
                "margin_stop_n": exits.get("margin_stop", 0),
                "tp_share": exits.get("take_profit", 0) / n,
                "daily_close_share": exits.get("daily_close", 0) / n,
                "margin_stop_share": exits.get("margin_stop", 0) / n,
                "pnl_contribution_share": (
                    st["sum_pnl"] / total_net if abs(total_net) > 1e-12 else None
                ),
            }
        )
        rows.append(st)
    rows.sort(key=lambda r: r["sum_pnl"])
    return rows


def exit_decomposition(trades: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by[str(t["exit_reason"])].append(float(t["pnl"]))
    n = max(1, len(trades))
    out = {}
    for reason, pnls in sorted(by.items()):
        st = bucket_stats(pnls)
        st["share"] = len(pnls) / n
        out[reason] = st
    return out


def expectancy_bridge(a: dict, b: dict, label_a: str, label_b: str) -> dict[str, Any]:
    ea = exit_decomposition(a["trades"])
    eb = exit_decomposition(b["trades"])
    reasons = sorted(set(ea) | set(eb))
    deltas = []
    for r in reasons:
        xa = (ea.get(r) or {}).get("sum_pnl") or 0.0
        xb = (eb.get(r) or {}).get("sum_pnl") or 0.0
        deltas.append(
            {
                "exit_reason": r,
                f"{label_a}_sum_pnl": xa,
                f"{label_b}_sum_pnl": xb,
                "delta_sum_pnl": xb - xa,
                f"{label_a}_mean": (ea.get(r) or {}).get("mean_pnl"),
                f"{label_b}_mean": (eb.get(r) or {}).get("mean_pnl"),
                f"{label_a}_n": (ea.get(r) or {}).get("n") or 0,
                f"{label_b}_n": (eb.get(r) or {}).get("n") or 0,
            }
        )
    deltas.sort(key=lambda x: x["delta_sum_pnl"])
    total_delta = sum(d["delta_sum_pnl"] for d in deltas)
    for d in deltas:
        d["fraction_of_total_delta"] = (
            d["delta_sum_pnl"] / total_delta if abs(total_delta) > 1e-9 else None
        )
    return {
        f"{label_a}_net": sum(t["pnl"] for t in a["trades"]),
        f"{label_b}_net": sum(t["pnl"] for t in b["trades"]),
        "net_delta": sum(t["pnl"] for t in b["trades"]) - sum(t["pnl"] for t in a["trades"]),
        "by_exit_delta_sorted_worst_first": deltas,
        "primary_contributor": deltas[0] if deltas else None,
    }


def direction_bridge(a: dict, b: dict, label_a: str, label_b: str) -> dict[str, Any]:
    out = {}
    for side in ("long", "short"):
        pa = [t["pnl"] for t in a["trades"] if t["side"] == side]
        pb = [t["pnl"] for t in b["trades"] if t["side"] == side]
        sa, sb = bucket_stats(pa), bucket_stats(pb)
        out[side] = {
            label_a: sa,
            label_b: sb,
            "delta_sum_pnl": sb["sum_pnl"] - sa["sum_pnl"],
            "delta_mean_pnl": (
                (sb["mean_pnl"] - sa["mean_pnl"])
                if sa["mean_pnl"] is not None and sb["mean_pnl"] is not None
                else None
            ),
            "delta_n": sb["n"] - sa["n"],
        }
    return out


def quality_vs_count(a: dict, b: dict, label_a: str, label_b: str) -> dict[str, Any]:
    """Separate fewer-trades vs worse-expectancy vs bad-class admission."""
    na = len(a["trades"])
    nb = len(b["trades"])
    ma = _mean([t["pnl"] for t in a["trades"]]) or 0.0
    mb = _mean([t["pnl"] for t in b["trades"]]) or 0.0
    # Decomposition: Δnet ≈ n_b*m_b - n_a*m_a = m_a*(n_b-n_a) + n_b*(m_b-m_a)
    count_component = ma * (nb - na)
    quality_component = nb * (mb - ma)
    return {
        f"{label_a}_n": na,
        f"{label_b}_n": nb,
        f"{label_a}_mean": ma,
        f"{label_b}_mean": mb,
        "net_delta": nb * mb - na * ma,
        "count_component_holding_mean_fixed": count_component,
        "quality_component_holding_n_fixed_at_b": quality_component,
        "interpretation": (
            "quality_dominated"
            if abs(quality_component) >= abs(count_component)
            else "count_dominated"
        ),
    }


def session_enum_counterfactual(trades: list[dict[str, Any]], session: str) -> dict[str, Any]:
    """Descriptive: keep trades whose entry falls in existing session enum window."""
    keep = []
    for t in trades:
        et = pd.Timestamp(t["entry_time"])
        if in_session(et, session):
            keep.append(t["pnl"])
    return bucket_stats(keep)


def build_filter_inventory() -> dict[str, Any]:
    s8 = AlexG8Strategy()
    s7 = AlexG7AlignedStrategy()
    abl: AblationConfig = s8.ablation
    items = [
        {
            "name": "ablation.session",
            "what": "Allow setups only inside named UTC session window",
            "data_used": "candle timestamp vs asia/london/newyork/overlap/all windows",
            "affects": "entry_selection",
            "enabled": True,
            "current_default": abl.session,
            "alexg7_default": s7.ablation.session,
            "domain": ["asia", "london", "newyork", "overlap", "all"],
        },
        {
            "name": "ablation.htf_bias",
            "what": "Require higher-TF direction agreement before setup",
            "data_used": "HTF structure/trend derived from resampled candles",
            "affects": "entry_selection",
            "enabled": abl.htf_bias != "off",
            "current_default": abl.htf_bias,
            "domain": ["off", "pair", "weekly", "daily", "4h"],
        },
        {
            "name": "ablation.require_chart_trend",
            "what": "Require execution-TF trend aligned with trade direction",
            "data_used": "structure_trend_at on chart TF",
            "affects": "entry_selection",
            "enabled": bool(abl.require_chart_trend),
            "current_default": abl.require_chart_trend,
        },
        {
            "name": "ablation.require_pattern",
            "what": "Require candlestick pattern confirmation",
            "data_used": "engulfing/hammer/soldiers/etc.",
            "affects": "entry_selection",
            "enabled": bool(abl.require_pattern),
            "current_default": abl.require_pattern,
        },
        {
            "name": "ablation.require_retest",
            "what": "Require AOI retest before entry",
            "data_used": "pending retest state machine",
            "affects": "entry_selection",
            "enabled": bool(abl.require_retest),
            "current_default": abl.require_retest,
        },
        {
            "name": "ablation.require_ghost_sl_entry",
            "what": "Queue ghost fill at planned SL / close fill",
            "data_used": "pending ghost setup",
            "affects": "entry_selection",
            "enabled": bool(abl.require_ghost_sl_entry),
            "current_default": abl.require_ghost_sl_entry,
        },
        {
            "name": "ghost_sl_mult",
            "what": "Scale distance from entry to ghost SL",
            "data_used": "planned SL geometry",
            "affects": "entry_selection",
            "enabled": True,
            "current_default": float(s8.ghost_sl_mult),
            "n1_control": 4.0,
            "note": "Already swept in v2 optimization; not re-opened here",
        },
        {
            "name": "min_aoi_touches / min_aoi_pips / max_aoi_pips / cluster_pips / aoi_pad_pips",
            "what": "AOI zone construction / membership thresholds",
            "data_used": "pip AOI zones from HTF",
            "affects": "entry_selection",
            "enabled": True,
            "current_defaults": {
                "min_aoi_touches": s8.min_aoi_touches,
                "min_aoi_pips": s8.min_aoi_pips,
                "max_aoi_pips": s8.max_aoi_pips,
                "cluster_pips": s8.cluster_pips,
                "aoi_pad_pips": s8.aoi_pad_pips,
            },
            "note": (
                "Pattern-tag AOI width is not a clean pip-normalized quality score "
                "across the 60-pair universe; not used as primary lever without "
                "cleaner instrumentation."
            ),
        },
        {
            "name": "signal_cooldown",
            "what": "Minimum bars between signals per symbol",
            "data_used": "bar index",
            "affects": "entry_selection",
            "enabled": True,
            "current_default": s8.signal_cooldown,
        },
        {
            "name": "sides (tape filter long_only/short_only/both)",
            "what": "Research-side filter on decision tape / intents",
            "data_used": "signal action buy/sell",
            "affects": "entry_selection",
            "enabled": True,
            "current_default": "both",
            "prior_result": "long_only ablation NOT_SUPPORTED (n1_long_only_ablation_20260925_030340)",
        },
        {
            "name": "rr_factor / rr_min / rr_max / rr_mode",
            "what": "Dynamic RR geometry clamps",
            "data_used": "portfolio winrate → resolve_rr",
            "affects": "exit_behavior (TP distance via true_sl)",
            "enabled": True,
            "current_defaults": {
                "rr_factor": 1.0,
                "rr_min": 2.0,
                "rr_max": 5.0,
                "rr_mode": "dynamic",
            },
            "note": "RR-floor hunting stopped after gate2_rr_research (relax not_supported)",
        },
        {
            "name": "force_flat_daily / force_flat_friday",
            "what": "Forced flatten at daily/Friday session close",
            "data_used": "weekday + session close hour",
            "affects": "exit_behavior",
            "enabled": True,
            "current_defaults": {"force_flat_daily": True, "force_flat_friday": False},
            "note": "Exit timing — does not filter Thursday entries",
        },
        {
            "name": "day_of_week entry filter",
            "what": "NOT PRESENT in alexg8 / AblationConfig",
            "data_used": "n/a",
            "affects": "n/a",
            "enabled": False,
            "current_default": None,
            "note": "Largest July loss cluster is Thursday — no existing DOW entry lever",
        },
        {
            "name": "side×session compound filter",
            "what": "NOT PRESENT as a single existing parameter",
            "data_used": "n/a",
            "affects": "n/a",
            "enabled": False,
            "current_default": None,
            "note": (
                "long ∩ newyork_post_overlap is systematically negative across "
                "Jun/Jul/Aug, but expressing it requires inventing a compound gate"
            ),
        },
        {
            "name": "pair / universe filter",
            "what": "External universe list (60 pairs) — not an alexg8 strategy knob",
            "data_used": "symbol list",
            "affects": "entry_selection",
            "enabled": True,
            "current_default": "60-pair frozen universe",
            "note": "July exotic concentration noted; universe cuts deferred by protocol",
        },
    ]
    return {
        "strategy": "alexg8",
        "ablation_factory": "video2_ghost_all_sessions",
        "items": items,
    }


def summarize_from_artifact(month: dict[str, Any]) -> dict[str, Any]:
    trades = month["trades"]
    pnls = [t["pnl"] for t in trades]
    s = month.get("summary") or {}
    exits = Counter(t["exit_reason"] for t in trades)
    sides = Counter(t["side"] for t in trades)
    n = max(1, len(trades))
    return {
        "source": month["source"],
        "intents": month.get("intents"),
        "fills": len(trades),
        "WR": _wr(pnls),
        "PF": s.get("profit_factor") if s.get("profit_factor") is not None else _pf(pnls),
        "net_pnl": s.get("net_pnl") if s.get("net_pnl") is not None else float(sum(pnls)),
        "mean_trade": _mean(pnls),
        "median_trade": _median(pnls),
        "MDD": s.get("max_drawdown_pct"),
        "TP": int(exits.get("take_profit", 0)),
        "daily_close": int(exits.get("daily_close", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
        "TP_share": exits.get("take_profit", 0) / n,
        "daily_close_share": exits.get("daily_close", 0) / n,
        "margin_stop_share": exits.get("margin_stop", 0) / n,
        "long_count": int(sides.get("long", 0)),
        "short_count": int(sides.get("short", 0)),
    }


def load_posthoc_rr25() -> dict[str, Any]:
    """Pull reference metrics from prior RR research (no re-optimization)."""
    path = PRIOR_RR / "conclusions.json"
    if not path.is_file():
        return {"available": False, "reason": f"missing {path}"}
    conc = json.loads(path.read_text(encoding="utf-8"))
    # Prefer dyn_rmin2p5_rf0p9 validated block
    validated = conc.get("validated") or []
    hit = next((v for v in validated if v.get("id") == "dyn_rmin2p5_rf0p9"), None)
    ctrl = {}
    cm = PRIOR_RR / "control_metrics.json"
    if cm.is_file():
        ctrl = json.loads(cm.read_text(encoding="utf-8")).get("by_period") or {}
    return {
        "available": True,
        "config": POSTHOC_RR25,
        "candidate_id": "dyn_rmin2p5_rf0p9",
        "by_period": (hit or {}).get("by_period"),
        "control_by_period": ctrl,
        "source": str(path.as_posix()),
        "status": "REFERENCE_ONLY_NOT_FOR_DEPLOYMENT",
    }


def expand_daily(out_dir: Path) -> dict:
    base = build_daily_dataset(CENSUS)
    rows = list(base["trade_rows"])
    seen = {(r.get("theory_event_id"), r.get("broker_ticket")) for r in rows}
    extra = 0
    for prior_path in (PRIOR_DAILY, PRIOR_V2 / "daily_theory_broker.json"):
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
    # Schema alias requested by research brief
    by_day = out_dir / "daily_theory_broker_by_day.csv"
    alias = out_dir / "daily_theory_broker.csv"
    if by_day.is_file():
        alias.write_bytes(by_day.read_bytes())
    return dataset


def decide_hypothesis(forensic: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Pre-register or STOP. Must not use July P&L to pick a threshold."""
    jul = forensic["months"]["july"]
    # Largest July negative clusters from exclusive session / dow / side×session
    thu = next(
        (r for r in forensic["july_dow"] if r.get("dow_name") == "Thu"),
        None,
    )
    long_ny = next(
        (
            r
            for r in forensic["july_side_session"]
            if r.get("side") == "long" and r.get("session_exclusive") == "newyork_post_overlap"
        ),
        None,
    )
    long_lon = next(
        (
            r
            for r in forensic["july_side_session"]
            if r.get("side") == "long" and r.get("session_exclusive") == "london_pre_overlap"
        ),
        None,
    )
    short_asia = next(
        (
            r
            for r in forensic["july_side_session"]
            if r.get("side") == "short" and r.get("session_exclusive") == "asia"
        ),
        None,
    )

    session_cf = forensic["session_enum_counterfactuals"]
    # Existing session enums: does any improve JUN+AUG mean/PF vs control while
    # remaining a credible July targeting lever? Descriptive only.
    ctrl_ja = session_cf["all"]["june_plus_august"]
    credible_session = None
    for sess in ("asia", "london", "newyork", "overlap"):
        ja = session_cf[sess]["june_plus_august"]
        ju = session_cf[sess]["july"]
        # Require JUN+AUG dual improve on PF+mean AND July improve on PF+mean (descriptive gate)
        if (
            ja.get("pf") is not None
            and ctrl_ja.get("pf") is not None
            and ja.get("mean_pnl") is not None
            and ctrl_ja.get("mean_pnl") is not None
            and ju.get("pf") is not None
            and session_cf["all"]["july"].get("pf") is not None
            and ja["pf"] > ctrl_ja["pf"]
            and ja["mean_pnl"] > ctrl_ja["mean_pnl"]
            and ju["pf"] > session_cf["all"]["july"]["pf"]
            and (ju.get("mean_pnl") or -1e9) > (session_cf["all"]["july"].get("mean_pnl") or 0)
        ):
            # Also reject catastrophic count collapse (<40% fills)
            if ja["n"] >= 0.4 * ctrl_ja["n"]:
                credible_session = sess
                break

    long_only_note = None
    if LONG_ONLY.is_dir():
        long_only_note = "long_only already tested → NOT_SUPPORTED"

    if credible_session:
        return {
            "action": "TEST",
            "hypothesis": (
                f"Restricting ablation.session from 'all' to '{credible_session}' may "
                f"remove a disproportionate share of July negative expectancy while "
                f"preserving or improving June+August combined expectancy."
            ),
            "filter": "ablation.session",
            "control_value": "all",
            "candidates": [credible_session],
            "selection_period": "june_plus_august",
            "validation_period": "july",
            "justification": {
                "thursday_cluster_sum": (thu or {}).get("sum_pnl"),
                "long_ny_sum": (long_ny or {}).get("sum_pnl"),
                "long_london_pre_sum": (long_lon or {}).get("sum_pnl"),
                "short_asia_sum": (short_asia or {}).get("sum_pnl"),
                "selected_session_counterfactual": session_cf[credible_session],
                "long_only_prior": long_only_note,
            },
        }

    return {
        "action": "STOP",
        "hypothesis": None,
        "reason": (
            "No credible EXISTING filter lever maps to the largest measurable July "
            "failure classes without inventing new strategy concepts."
        ),
        "largest_july_negative_clusters": {
            "thursday_entries": thu,
            "long_london_pre_overlap": long_lon,
            "short_asia": short_asia,
            "long_newyork_post_overlap": long_ny,
        },
        "why_not_existing_levers": [
            (
                "Thursday (largest July ΣPnL cluster, also negative in June/August) "
                "has no day-of-week entry filter in alexg8/AblationConfig."
            ),
            (
                "long ∩ newyork_post_overlap is systematically negative across "
                "June/July/August, but requires a compound side×session gate that "
                "does not exist as a single parameter."
            ),
            (
                "Existing ablation.session allowlist values (asia/london/newyork/overlap) "
                "fail the descriptive JUN+AUG dual-improve gate versus session=all "
                "(london/overlap destroy June edge that lives largely in off_hours)."
            ),
            (
                "sides=long_only already pre-registered and tested → NOT_SUPPORTED."
            ),
            (
                "RR floor / factor hunting stopped after gate2_rr_research "
                "(relax rr_min not_supported)."
            ),
            (
                "AOI width / max_aoi_pips not used: pattern-tag widths are not a clean "
                "cross-pair quality score; counterfactuals collapse trade count."
            ),
            (
                "Pair/universe cuts deferred by protocol despite July exotic concentration."
            ),
        ],
        "long_only_prior": long_only_note,
        "session_counterfactuals_summary": {
            sess: {
                "june_plus_august_mean": session_cf[sess]["june_plus_august"].get("mean_pnl"),
                "june_plus_august_pf": session_cf[sess]["june_plus_august"].get("pf"),
                "july_mean": session_cf[sess]["july"].get("mean_pnl"),
                "july_pf": session_cf[sess]["july"].get("pf"),
            }
            for sess in ("all", "asia", "london", "newyork", "overlap")
        },
    }


def render_july_forensic_md(forensic: dict[str, Any], decision: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# July Failure Forensics (descriptive)")
    lines.append("")
    lines.append("Read-only. No optimization. Control artifacts: N1@500-equivalent.")
    lines.append("")
    lines.append("## Month snapshots")
    lines.append("")
    for key in ("june", "july", "august"):
        m = forensic["control_metrics"][key]
        lines.append(
            f"- **{key}**: fills={m['fills']} WR={100*(m['WR'] or 0):.1f}% "
            f"PF={m['PF']:.3f} net={m['net_pnl']:.2f} mean={m['mean_trade']:.4f} "
            f"MDD={None if m['MDD'] is None else 100*float(m['MDD']):.1f}% "
            f"TP={m['TP']} DC={m['daily_close']} MS={m['margin_stop']}"
        )
    lines.append("")
    lines.append("## Gap decomposition (June→July)")
    lines.append("")
    q = forensic["june_to_july_quality_vs_count"]
    lines.append(
        f"- Net Δ = **{q['net_delta']:.2f}** "
        f"(count component {q['count_component_holding_mean_fixed']:.2f}, "
        f"quality component {q['quality_component_holding_n_fixed_at_b']:.2f}) → "
        f"**{q['interpretation']}**"
    )
    lines.append(
        f"- Fills {q['june_n']}→{q['july_n']}; mean {q['june_mean']:.4f}→{q['july_mean']:.4f}"
    )
    lines.append("")
    lines.append("Classification:")
    lines.append("")
    for c in forensic["failure_class_labels"]:
        lines.append(f"- `{c}`")
    lines.append("")
    lines.append("## Exit bridge June→July")
    lines.append("")
    b = forensic["june_to_july_exit_bridge"]
    lines.append("| Exit | Jun Σ | Jul Σ | ΔΣ | Jun n | Jul n |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for d in b["by_exit_delta_sorted_worst_first"]:
        lines.append(
            f"| {d['exit_reason']} | {d['june_sum_pnl']:.2f} | {d['july_sum_pnl']:.2f} | "
            f"{d['delta_sum_pnl']:.2f} | {d['june_n']} | {d['july_n']} |"
        )
    lines.append("")
    lines.append("## Direction bridge")
    lines.append("")
    for side, st in forensic["june_to_july_direction_bridge"].items():
        lines.append(
            f"- **{side}**: June Σ={st['june']['sum_pnl']:.2f} mean={st['june']['mean_pnl']:.4f} "
            f"→ July Σ={st['july']['sum_pnl']:.2f} mean={st['july']['mean_pnl']:.4f} "
            f"(ΔΣ={st['delta_sum_pnl']:.2f})"
        )
    lines.append("")
    lines.append("## Largest July negative clusters")
    lines.append("")
    lines.append("| Cluster | n | ΣPnL | mean | PF |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for label, row in [
        ("Thursday", next((r for r in forensic["july_dow"] if r.get("dow_name") == "Thu"), {})),
        (
            "long × london_pre_overlap",
            next(
                (
                    r
                    for r in forensic["july_side_session"]
                    if r.get("side") == "long"
                    and r.get("session_exclusive") == "london_pre_overlap"
                ),
                {},
            ),
        ),
        (
            "short × asia",
            next(
                (
                    r
                    for r in forensic["july_side_session"]
                    if r.get("side") == "short" and r.get("session_exclusive") == "asia"
                ),
                {},
            ),
        ),
        (
            "long × newyork_post_overlap",
            next(
                (
                    r
                    for r in forensic["july_side_session"]
                    if r.get("side") == "long"
                    and r.get("session_exclusive") == "newyork_post_overlap"
                ),
                {},
            ),
        ),
    ]:
        if not row:
            continue
        lines.append(
            f"| {label} | {row.get('n')} | {row.get('sum_pnl'):.2f} | "
            f"{row.get('mean_pnl'):.4f} | {row.get('pf')} |"
        )
    lines.append("")
    lines.append("## Hypothesis decision")
    lines.append("")
    lines.append(f"- **action**: `{decision['action']}`")
    if decision["action"] == "STOP":
        lines.append(f"- **reason**: {decision['reason']}")
        for w in decision.get("why_not_existing_levers") or []:
            lines.append(f"  - {w}")
    else:
        lines.append(f"- **hypothesis**: {decision['hypothesis']}")
    lines.append("")
    return "\n".join(lines)


def render_filter_inventory_md(inv: dict[str, Any]) -> str:
    lines = ["# Existing Filter Inventory (alexg8)", ""]
    lines.append(f"Ablation factory: `{inv['ablation_factory']}`")
    lines.append("")
    for it in inv["items"]:
        lines.append(f"## `{it['name']}`")
        lines.append("")
        for k in (
            "what",
            "data_used",
            "affects",
            "enabled",
            "current_default",
            "current_defaults",
            "domain",
            "prior_result",
            "note",
            "n1_control",
            "alexg7_default",
        ):
            if k in it and it[k] is not None:
                lines.append(f"- **{k}**: `{it[k]}`")
        lines.append("")
    return "\n".join(lines)


def render_preregistration_md(decision: dict[str, Any]) -> str:
    lines = ["# Pre-registration — Gate 2 July Failure Research", ""]
    lines.append(f"Written at (UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("## Protocol")
    lines.append("")
    lines.append("- Optimization / selection: **June + August only**")
    lines.append("- Validation: **July** (frozen after selection)")
    lines.append("- September-to-date: descriptive only")
    lines.append("- May: excluded (M1 provenance)")
    lines.append("- Do not invent new indicators / compound gates in this campaign")
    lines.append("")
    if decision["action"] == "STOP":
        lines.append("## Decision: STOP — no defensible hypothesis")
        lines.append("")
        lines.append(decision["reason"])
        lines.append("")
        lines.append("### Why existing levers were rejected")
        lines.append("")
        for w in decision.get("why_not_existing_levers") or []:
            lines.append(f"- {w}")
        lines.append("")
        lines.append("### No optimization will be run")
        lines.append("")
        lines.append(
            "Running a search without a pre-justified existing lever would contaminate "
            "July validation and violate Part 4 of the research brief."
        )
    else:
        lines.append("## Hypothesis")
        lines.append("")
        lines.append(decision["hypothesis"])
        lines.append("")
        lines.append(f"- Filter: `{decision['filter']}`")
        lines.append(f"- Control value: `{decision['control_value']}`")
        lines.append(f"- Candidates: `{decision['candidates']}`")
        lines.append(
            "- Success: improve June+August dual (PF+mean) vs control AND improve July "
            "dual vs control; no catastrophic fill collapse; not leverage/margin artifact."
        )
    lines.append("")
    return "\n".join(lines)


def render_report_md(
    out_name: str,
    forensic: dict[str, Any],
    decision: dict[str, Any],
    daily: dict[str, Any],
    posthoc: dict[str, Any],
    conclusions: dict[str, Any],
) -> str:
    lines = [f"# Gate 2 July Failure Research — `{out_name}`", ""]
    lines.append("Offline. LIVE/Mirror untouched. Simulator unmodified.")
    lines.append("")
    lines.append("## 1. Exact July failure mechanism(s)")
    lines.append("")
    for c in forensic["failure_class_labels"]:
        lines.append(f"- `{c}`")
    lines.append("")
    lines.append(forensic["mechanism_summary"])
    lines.append("")
    lines.append("## 2. Largest measurable contributors")
    lines.append("")
    for item in forensic["largest_contributors"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 3. June vs July vs August")
    lines.append("")
    for key in ("june", "july", "august"):
        m = forensic["control_metrics"][key]
        lines.append(
            f"- **{key}**: PF={m['PF']:.3f} net={m['net_pnl']:.2f} "
            f"mean={m['mean_trade']:.4f} fills={m['fills']} "
            f"TP={m['TP']} DC={m['daily_close']} MS={m['margin_stop']}"
        )
    lines.append("")
    lines.append("## 4. Existing filters capable of targeting the failure class")
    lines.append("")
    lines.append(
        "See `filter_inventory.md`. **No single existing entry filter cleanly targets "
        "the largest July clusters** (Thursday; long×NY-post) without inventing new concepts."
    )
    lines.append("")
    lines.append("## 5. Pre-registered hypothesis")
    lines.append("")
    if decision["action"] == "STOP":
        lines.append("**None — STOP after forensics.**")
        lines.append("")
        lines.append(decision["reason"])
    else:
        lines.append(decision["hypothesis"])
    lines.append("")
    lines.append("## 6–8. Optimization / selection / July validation")
    lines.append("")
    lines.append(conclusions.get("optimization_status", "not_run"))
    lines.append("")
    lines.append("## 9. September-to-date (descriptive)")
    lines.append("")
    sep = conclusions.get("september_todate_descriptive") or {}
    if sep:
        lines.append(
            f"- Control: PF={sep.get('PF')} net={sep.get('net_pnl')} mean={sep.get('mean_trade')}"
        )
    else:
        lines.append("- Not re-simulated in this campaign; see prior RR/v2 artifacts.")
    lines.append("")
    lines.append("## 10. Interaction with rr_min=2.5 reference")
    lines.append("")
    lines.append(
        f"- Status: `{posthoc.get('status')}` / available={posthoc.get('available')}"
    )
    lines.append("- No new filter candidate to cross with RR=2.5 (search not run).")
    lines.append("")
    lines.append("## 11. Hypothesis result")
    lines.append("")
    lines.append(f"**{conclusions['hypothesis_result']}**")
    lines.append("")
    lines.append("## 12. Mirror consideration")
    lines.append("")
    lines.append(conclusions["mirror_consideration"])
    lines.append("")
    lines.append("## 13. Theory ↔ Broker coverage")
    lines.append("")
    lines.append(
        f"- Days: {daily.get('n_days')} | trade rows: {daily.get('n_trade_rows')} "
        f"| extra from prior: {daily.get('extra_rows_from_prior')}"
    )
    lines.append("")
    lines.append("## 14. Exact next experiment")
    lines.append("")
    lines.append(conclusions["next_experiment"])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_name = f"gate2_july_failure_research_{ts}"
    out = ROOT / "data" / "runs" / "research" / out_name
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    months = {k: load_month(k, p) for k, p in SOURCES.items()}
    control_metrics = {k: summarize_from_artifact(v) for k, v in months.items()}

    # ---- Dimension breakdowns ----
    forensic_tables: dict[str, Any] = {}
    for key, month in months.items():
        trades = month["trades"]
        total_net = sum(t["pnl"] for t in trades) or 1.0
        forensic_tables[f"{key}_daily"] = group_breakdown(trades, "day", total_net=total_net)
        forensic_tables[f"{key}_pair"] = group_breakdown(trades, "symbol", total_net=total_net)
        forensic_tables[f"{key}_direction"] = group_breakdown(trades, "side", total_net=total_net)
        forensic_tables[f"{key}_exit"] = group_breakdown(
            trades, "exit_reason", total_net=total_net
        )
        forensic_tables[f"{key}_hour"] = group_breakdown(
            trades, "entry_hour_utc", total_net=total_net
        )
        forensic_tables[f"{key}_dow"] = group_breakdown(trades, "dow_name", total_net=total_net)
        forensic_tables[f"{key}_session_exclusive"] = group_breakdown(
            trades, "session_exclusive", total_net=total_net
        )
        forensic_tables[f"{key}_rr"] = group_breakdown(trades, "rr", total_net=total_net)
        # side × session
        tagged = []
        for t in trades:
            r = dict(t)
            r["side_session"] = f"{t['side']}|{t['session_exclusive']}"
            tagged.append(r)
        forensic_tables[f"{key}_side_session_flat"] = group_breakdown(
            tagged, "side_session", total_net=total_net
        )

    # Convenience July-focused tables for decision
    july_trades = months["july"]["trades"]
    total_jul = sum(t["pnl"] for t in july_trades) or 1.0
    july_side_session_rows = []
    by_ss: dict[tuple, list] = defaultdict(list)
    for t in july_trades:
        by_ss[(t["side"], t["session_exclusive"])].append(t)
    for (side, sess), ts in by_ss.items():
        st = bucket_stats([t["pnl"] for t in ts])
        exits = Counter(t["exit_reason"] for t in ts)
        n = max(1, len(ts))
        st.update(
            {
                "side": side,
                "session_exclusive": sess,
                "tp_n": exits.get("take_profit", 0),
                "daily_close_n": exits.get("daily_close", 0),
                "margin_stop_n": exits.get("margin_stop", 0),
                "tp_share": exits.get("take_profit", 0) / n,
                "daily_close_share": exits.get("daily_close", 0) / n,
                "margin_stop_share": exits.get("margin_stop", 0) / n,
                "pnl_contribution_share": st["sum_pnl"] / total_jul,
            }
        )
        july_side_session_rows.append(st)
    july_side_session_rows.sort(key=lambda r: r["sum_pnl"])

    # Session enum counterfactuals (descriptive)
    session_cf: dict[str, Any] = {}
    for sess in ("all", "asia", "london", "newyork", "overlap"):
        session_cf[sess] = {
            "june": session_enum_counterfactual(months["june"]["trades"], sess),
            "july": session_enum_counterfactual(months["july"]["trades"], sess),
            "august": session_enum_counterfactual(months["august"]["trades"], sess),
            "june_plus_august": bucket_stats(
                [
                    t["pnl"]
                    for m in ("june", "august")
                    for t in months[m]["trades"]
                    if in_session(pd.Timestamp(t["entry_time"]), sess)
                ]
            ),
        }

    # Descriptive compound counterfactual (NOT a runnable existing filter)
    compound_cf = {}
    for label, pred in [
        (
            "exclude_thursday",
            lambda t: t["dow_name"] != "Thu",
        ),
        (
            "exclude_long_ny_post",
            lambda t: not (
                t["side"] == "long" and t["session_exclusive"] == "newyork_post_overlap"
            ),
        ),
        (
            "exclude_ny_post_hours",
            lambda t: t["session_exclusive"] != "newyork_post_overlap",
        ),
    ]:
        compound_cf[label] = {
            m: bucket_stats([t["pnl"] for t in months[m]["trades"] if pred(t)])
            for m in ("june", "july", "august")
        }
        compound_cf[label]["june_plus_august"] = bucket_stats(
            [
                t["pnl"]
                for m in ("june", "august")
                for t in months[m]["trades"]
                if pred(t)
            ]
        )

    j2j_exit = expectancy_bridge(months["june"], months["july"], "june", "july")
    a2j_exit = expectancy_bridge(months["august"], months["july"], "august", "july")
    j2j_dir = direction_bridge(months["june"], months["july"], "june", "july")
    j2j_qc = quality_vs_count(months["june"], months["july"], "june", "july")

    failure_labels = [
        "QUALITY_DOMINATED_EXPECTANCY_DETERIORATION",
        "TAKE_PROFIT_COUNT_COLLAPSE",
        "THURSDAY_CONCENTRATION",
        "SESSION_CLUSTER_ASYMMETRY",
        "DIRECTIONAL_ASYMMETRY_SHORT_FLIP",
    ]
    if j2j_qc["interpretation"] == "count_dominated":
        failure_labels[0] = "COUNT_DOMINATED_THROUGHPUT_CHANGE"

    mechanism_summary = (
        "July underperforms primarily because existing trades have worse expectancy "
        f"(quality-dominated June→July Δnet={j2j_qc['net_delta']:.2f}; "
        f"fills {j2j_qc['june_n']}→{j2j_qc['july_n']}, "
        f"mean {j2j_qc['june_mean']:.4f}→{j2j_qc['july_mean']:.4f}), "
        "with a concurrent take_profit count drop and a large Thursday entry cluster. "
        "This is not a throughput collapse, not a margin_stop worsening vs June, "
        "and not explained by admitting an entirely new exit-mix regime."
    )

    thu = next(
        (r for r in forensic_tables["july_dow"] if r.get("dow_name") == "Thu"),
        {},
    )
    largest = [
        f"Thursday entries: n={thu.get('n')} ΣPnL={thu.get('sum_pnl')} mean={thu.get('mean_pnl')} PF={thu.get('pf')}",
        f"Exit bridge primary: {j2j_exit.get('primary_contributor')}",
        f"Short side June→July ΔΣ={j2j_dir['short']['delta_sum_pnl']:.2f}",
        f"Long side June→July ΔΣ={j2j_dir['long']['delta_sum_pnl']:.2f}",
    ]
    if july_side_session_rows:
        worst = july_side_session_rows[0]
        largest.append(
            f"Worst side×session: {worst['side']}×{worst['session_exclusive']} "
            f"Σ={worst['sum_pnl']:.2f} n={worst['n']}"
        )

    forensic = {
        "months": {k: {"n": len(v["trades"]), "source": v["source"]} for k, v in months.items()},
        "control_metrics": control_metrics,
        "june_to_july_exit_bridge": j2j_exit,
        "august_to_july_exit_bridge": a2j_exit,
        "june_to_july_direction_bridge": j2j_dir,
        "june_to_july_quality_vs_count": j2j_qc,
        "july_dow": forensic_tables["july_dow"],
        "july_side_session": july_side_session_rows,
        "session_enum_counterfactuals": session_cf,
        "compound_counterfactuals_not_existing_filters": compound_cf,
        "failure_class_labels": failure_labels,
        "mechanism_summary": mechanism_summary,
        "largest_contributors": largest,
        "unavailable_fields": [
            "named signal strength/confidence score",
            "explicit volatility regime field",
            "pre-cost PnL split",
            "day-of-week entry filter parameter",
            "side×session compound parameter",
        ],
    }

    inventory = build_filter_inventory()
    decision = decide_hypothesis(forensic, inventory)

    # Write preregistration BEFORE any hypothetical search
    (out / "preregistration.md").write_text(
        render_preregistration_md(decision), encoding="utf-8"
    )
    print(f"Pre-registration written: action={decision['action']}", flush=True)

    # Dimension CSVs (only available dimensions)
    write_csv(out / "july_daily_breakdown.csv", forensic_tables["july_daily"])
    write_csv(out / "july_pair_breakdown.csv", forensic_tables["july_pair"])
    write_csv(out / "july_direction_breakdown.csv", forensic_tables["july_direction"])
    write_csv(out / "july_exit_breakdown.csv", forensic_tables["july_exit"])
    # time = hour + dow + session
    time_rows = []
    for r in forensic_tables["july_hour"]:
        time_rows.append({"dimension": "entry_hour_utc", **r})
    for r in forensic_tables["july_dow"]:
        time_rows.append({"dimension": "dow", **r})
    for r in forensic_tables["july_session_exclusive"]:
        time_rows.append({"dimension": "session_exclusive", **r})
    write_csv(out / "july_time_breakdown.csv", time_rows)
    write_csv(out / "july_rr_breakdown.csv", forensic_tables["july_rr"])
    write_csv(out / "july_side_session_breakdown.csv", july_side_session_rows)

    # Cross-month comparison CSV
    cross = []
    for month in ("june", "july", "august"):
        for r in forensic_tables[f"{month}_direction"]:
            cross.append(
                {
                    "month": month,
                    "dimension": "side",
                    "key": r.get("side"),
                    **{k: v for k, v in r.items() if k != "side"},
                }
            )
        for r in forensic_tables[f"{month}_exit"]:
            cross.append(
                {
                    "month": month,
                    "dimension": "exit_reason",
                    "key": r.get("exit_reason"),
                    **{k: v for k, v in r.items() if k != "exit_reason"},
                }
            )
        for r in forensic_tables[f"{month}_session_exclusive"]:
            cross.append(
                {
                    "month": month,
                    "dimension": "session_exclusive",
                    "key": r.get("session_exclusive"),
                    **{k: v for k, v in r.items() if k != "session_exclusive"},
                }
            )
        for r in forensic_tables[f"{month}_dow"]:
            cross.append(
                {
                    "month": month,
                    "dimension": "dow",
                    "key": r.get("dow_name"),
                    **{k: v for k, v in r.items() if k != "dow_name"},
                }
            )
    write_csv(out / "cross_month_dimension_summary.csv", cross)

    (out / "july_forensic.md").write_text(
        render_july_forensic_md(forensic, decision), encoding="utf-8"
    )
    (out / "filter_inventory.md").write_text(
        render_filter_inventory_md(inventory), encoding="utf-8"
    )
    (out / "filter_inventory.json").write_text(
        json.dumps(inventory, indent=2, default=str), encoding="utf-8"
    )
    (out / "forensic_payload.json").write_text(
        json.dumps(forensic, indent=2, default=str), encoding="utf-8"
    )

    # Control + posthoc reference
    (out / "control_metrics.json").write_text(
        json.dumps({"control": CONTROL, "by_period": control_metrics}, indent=2),
        encoding="utf-8",
    )
    posthoc = load_posthoc_rr25()
    (out / "posthoc_rr25_reference.json").write_text(
        json.dumps(posthoc, indent=2, default=str), encoding="utf-8"
    )

    # Optimization intentionally not run
    opt_rows = [
        {
            "status": "not_run",
            "reason": decision.get("reason") or decision.get("action"),
            "action": decision["action"],
        }
    ]
    write_csv(out / "optimization_results.csv", opt_rows)
    write_csv(out / "validation_results.csv", opt_rows)

    # September descriptive from prior control metrics if present
    sep_desc = None
    cm = PRIOR_RR / "control_metrics.json"
    if cm.is_file():
        prior_ctrl = json.loads(cm.read_text(encoding="utf-8"))
        sep_desc = (prior_ctrl.get("by_period") or {}).get("september_todate")

    daily = expand_daily(out)

    symbols = sorted({t["symbol"] for m in months.values() for t in m["trades"]})
    provenance = {
        "git_commit": _git(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "control": CONTROL,
        "sources": {k: str(v.as_posix()) for k, v in SOURCES.items()},
        "universe_symbols_observed": symbols,
        "universe_hash": hashlib.sha256("\n".join(symbols).encode()).hexdigest(),
        "simulator": "frozen MultiMarketEngine / closest-sim 1m — unmodified this campaign",
        "data_cache": "data/cache/mt5/utc_v2 (via prior N1 artifacts)",
        "may_excluded": True,
        "september_role": "descriptive_only",
        "decision_action": decision["action"],
    }
    (out / "data_provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )

    conclusions = {
        "hypothesis_result": (
            "no_defensible_hypothesis"
            if decision["action"] == "STOP"
            else "pending_validation"
        ),
        "optimization_status": (
            "not_run — STOP after forensic analysis; no existing filter lever "
            "was credible under the pre-registration rules"
            if decision["action"] == "STOP"
            else "run"
        ),
        "failure_mechanisms": failure_labels,
        "mechanism_summary": mechanism_summary,
        "largest_contributors": largest,
        "pre_registration_action": decision["action"],
        "pre_registration": decision,
        "june_july_august_control": control_metrics,
        "september_todate_descriptive": sep_desc,
        "posthoc_rr25_reference_status": posthoc.get("status"),
        "mirror_consideration": (
            "No — no approved candidate. Do not trade Mirror. "
            "Prior long-only NOT_SUPPORTED; RR-floor relax not_supported; "
            "this campaign produced no new filter candidate."
        ),
        "daily_theory_broker": {
            "n_days": daily.get("n_days"),
            "n_trade_rows": daily.get("n_trade_rows"),
            "extra_rows_from_prior": daily.get("extra_rows_from_prior"),
        },
        "next_experiment": (
            "Either (A) explicitly authorize inventing ONE new research-only gate "
            "pre-registered against the largest forensic cluster — prefer "
            "day-of-week Thursday exclusion OR long∩newyork_post_overlap exclusion — "
            "with June+August selection and July validation; or (B) wait for "
            "September calendar close and run frozen N1@500 as clean unseen-month OOS; "
            "or (C) instrument pip-normalized AOI/entry-quality fields then re-assess "
            "existing min/max_aoi_pips levers. Do not resume broad RR-floor hunting. "
            "Do not Mirror-deploy."
        ),
        "classifications": {
            "july_vs_june": "degraded_versus_control",
            "hypothesis_test": "not_run",
            "existing_filter_lever_found": False,
        },
    }
    (out / "conclusions.json").write_text(
        json.dumps(conclusions, indent=2, default=str), encoding="utf-8"
    )

    (out / "report.md").write_text(
        render_report_md(out_name, forensic, decision, daily, posthoc, conclusions),
        encoding="utf-8",
    )

    # candidate_selection / per_period empty stubs for schema completeness
    (out / "candidate_selection.json").write_text(
        json.dumps(
            {
                "selected": [],
                "reason": "no_defensible_hypothesis",
                "action": decision["action"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "per_period_metrics.json").write_text(
        json.dumps(
            {
                "control": control_metrics,
                "treatments": {},
                "note": "No treatments evaluated — STOP after forensics",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("DONE", out, flush=True)
    print("ACTION", decision["action"], flush=True)
    print("HYPOTHESIS_RESULT", conclusions["hypothesis_result"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
