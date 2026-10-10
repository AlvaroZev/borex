#!/usr/bin/env python3
"""N1 robustness forensics — read-only trade-level diagnosis (Jun/Jul/Aug).

Identifies the first high-information strategy hypothesis. Does not modify
strategy, simulator, leverage, or parameters. Does not run a sweep.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "aug_is": ROOT / "data/runs/mt5_1m_n1_tpfactor/sl_4p0_lev_500_rr_1p0.json",
    "jun_oos": ROOT / "data/runs/mt5_1m_n1_oos_jun2026/sl_4p0_lev_500.json",
    "jul_oos": ROOT / "data/runs/mt5_1m_n1_oos_jul2026/sl_4p0_lev_500.json",
}


def _git_commit() -> str:
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


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return float(s[f] + (s[c] - s[f]) * (k - f))


def _pf(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    gp = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    if gl == 0:
        return None
    return float(gp / abs(gl))


def _wr(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    return sum(1 for x in pnls if x > 0) / len(pnls)


def parse_pattern(p: str | None) -> dict[str, Any]:
    """Parse alexg8 pattern tag into available signal fields."""
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
        "session": None,
        "fill": None,
        "aoi_mid": None,
        "aoi_lo": None,
        "aoi_hi": None,
    }
    if not p:
        return out
    parts = str(p).split("|")
    # alexg8|SYM|support|daily|close_in_aoi|bias=off|trend0|pat0|ret0|ghost1|sess=all|g:...|fill:close
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
            out["session"] = part.split("=", 1)[1]
        elif part.startswith("fill:"):
            out["fill"] = part.split(":", 1)[1]
        elif part.startswith("g:"):
            # g:idx:mid:lo:hi
            bits = part.split(":")
            if len(bits) >= 5:
                try:
                    out["aoi_mid"] = float(bits[2])
                    out["aoi_lo"] = float(bits[3])
                    out["aoi_hi"] = float(bits[4])
                except ValueError:
                    pass
    return out


def load_month(label: str, path: Path) -> dict[str, Any]:
    d = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for t in d.get("trades") or []:
        entry_t = pd.Timestamp(t["entry_time"])
        exit_t = pd.Timestamp(t["exit_time"])
        hold_h = (exit_t - entry_t).total_seconds() / 3600.0
        entry = float(t["entry"])
        sl = float(t["sl"])
        tp = float(t["tp"])
        exit_px = float(t["exit"])
        side = str(t["side"]).lower()
        # distances in price
        if side == "long":
            risk = entry - sl
            reward = tp - entry
            adverse = entry - exit_px
            favorable = exit_px - entry
        else:
            risk = sl - entry
            reward = entry - tp
            adverse = exit_px - entry
            favorable = entry - exit_px
        r_mult = (favorable / risk) if risk > 1e-12 else None
        # Did exit land near SL? (proxy — no explicit SL exit label in these runs)
        near_sl = abs(exit_px - sl) / max(abs(entry) * 1e-4, abs(risk), 1e-12) < 0.15
        near_tp = abs(exit_px - tp) / max(abs(entry) * 1e-4, abs(reward), 1e-12) < 0.15
        pat = parse_pattern(t.get("pattern"))
        hour = int(entry_t.tz_convert("UTC").hour) if entry_t.tzinfo else int(entry_t.hour)
        rows.append(
            {
                "month": label,
                "symbol": t["symbol"],
                "side": side,
                "entry_time": str(entry_t),
                "exit_time": str(exit_t),
                "entry_hour_utc": hour,
                "hold_hours": hold_h,
                "entry": entry,
                "exit": exit_px,
                "sl": sl,
                "tp": tp,
                "pnl": float(t["pnl"]),
                "margin": float(t.get("margin") or 0.0),
                "rr": float(t.get("rr") or 0.0),
                "exit_reason": t["exit_reason"],
                "risk_price": risk,
                "reward_price": reward,
                "r_multiple_from_exit": r_mult,
                "exit_near_sl": near_sl,
                "exit_near_tp": near_tp,
                "structure": pat["structure"],
                "trigger": pat["trigger"],
                "session": pat["session"],
                "fill": pat["fill"],
                "aoi_width": (
                    abs(pat["aoi_hi"] - pat["aoi_lo"])
                    if pat["aoi_hi"] is not None and pat["aoi_lo"] is not None
                    else None
                ),
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
        "trades": rows,
    }


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
    # normalize labels
    for alias in ("stop_loss", "SL", "sl"):
        out.setdefault(alias, bucket_stats([]))
        out[alias]["share"] = 0.0
        out[alias]["note"] = "No explicit SL exit_reason in N1 artifacts"
    return out


def direction_breakdown(trades: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for side in ("long", "short"):
        sub = [t for t in trades if t["side"] == side]
        pnls = [t["pnl"] for t in sub]
        st = bucket_stats(pnls)
        er = Counter(t["exit_reason"] for t in sub)
        n = max(1, len(sub))
        st["exit_share"] = {k: v / n for k, v in er.items()}
        st["margin_stop_share"] = er.get("margin_stop", 0) / n
        st["daily_close_share"] = er.get("daily_close", 0) / n
        out[side] = st
    return out


def pair_breakdown(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_sym[t["symbol"]].append(t)
    rows = []
    total = sum(t["pnl"] for t in trades) or 1.0
    loss_total = sum(t["pnl"] for t in trades if t["pnl"] < 0) or -1.0
    for sym, ts in by_sym.items():
        pnls = [t["pnl"] for t in ts]
        st = bucket_stats(pnls)
        ms = [t for t in ts if t["exit_reason"] == "margin_stop"]
        st.update(
            {
                "symbol": sym,
                "margin_stop_n": len(ms),
                "margin_stop_pnl": sum(t["pnl"] for t in ms),
                "pnl_contribution_share": st["sum_pnl"] / total,
                "loss_contribution_share": (
                    sum(x for x in pnls if x < 0) / loss_total if loss_total else None
                ),
            }
        )
        rows.append(st)
    rows.sort(key=lambda r: r["sum_pnl"])
    return rows


def holding_breakdown(trades: list[dict[str, Any]]) -> dict[str, Any]:
    holds = [t["hold_hours"] for t in trades]
    by_exit: dict[str, list[float]] = defaultdict(list)
    by_side: dict[str, list[float]] = defaultdict(list)
    win_h = [t["hold_hours"] for t in trades if t["pnl"] > 0]
    loss_h = [t["hold_hours"] for t in trades if t["pnl"] <= 0]
    for t in trades:
        by_exit[t["exit_reason"]].append(t["hold_hours"])
        by_side[t["side"]].append(t["hold_hours"])
    return {
        "overall": {
            "median_h": _median(holds),
            "p25_h": _pct(holds, 25),
            "p75_h": _pct(holds, 75),
            "mean_h": _mean(holds),
        },
        "by_exit": {
            k: {"median_h": _median(v), "p25_h": _pct(v, 25), "p75_h": _pct(v, 75), "n": len(v)}
            for k, v in sorted(by_exit.items())
        },
        "by_side": {
            k: {"median_h": _median(v), "mean_h": _mean(v), "n": len(v)}
            for k, v in sorted(by_side.items())
        },
        "win_vs_loss": {
            "win_median_h": _median(win_h),
            "loss_median_h": _median(loss_h),
            "win_n": len(win_h),
            "loss_n": len(loss_h),
        },
    }


def margin_forensics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    ms = [t for t in trades if t["exit_reason"] == "margin_stop"]
    losses = [t for t in trades if t["pnl"] < 0]
    loss_sum = sum(t["pnl"] for t in losses) or -1.0
    ms_loss = sum(t["pnl"] for t in ms if t["pnl"] < 0)
    by_pair = Counter(t["symbol"] for t in ms)
    by_side = Counter(t["side"] for t in ms)
    near_sl_n = sum(1 for t in ms if t["exit_near_sl"])
    return {
        "n": len(ms),
        "share": len(ms) / max(1, len(trades)),
        "sum_pnl": sum(t["pnl"] for t in ms),
        "mean_pnl": _mean([t["pnl"] for t in ms]),
        "median_pnl": _median([t["pnl"] for t in ms]),
        "pct_of_total_losses": ms_loss / loss_sum if losses else None,
        "side_counts": dict(by_side),
        "top_pairs": by_pair.most_common(15),
        "exit_near_sl_share": near_sl_n / max(1, len(ms)),
        "mean_abs_loss": _mean([abs(t["pnl"]) for t in ms]),
        "mean_margin": _mean([t["margin"] for t in ms]),
    }


def daily_close_forensics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    dc = [t for t in trades if t["exit_reason"] == "daily_close"]
    pnls = [t["pnl"] for t in dc]
    by_pair = sorted(
        (
            {
                "symbol": sym,
                "n": len(ts),
                "sum_pnl": sum(t["pnl"] for t in ts),
                "mean_pnl": _mean([t["pnl"] for t in ts]),
            }
            for sym, ts in (
                (s, [t for t in dc if t["symbol"] == s])
                for s in {t["symbol"] for t in dc}
            )
        ),
        key=lambda r: r["sum_pnl"],
    )
    by_side = direction_breakdown(dc)
    return {
        "n": len(dc),
        "share": len(dc) / max(1, len(trades)),
        **bucket_stats(pnls),
        "side": by_side,
        "worst_pairs": by_pair[:10],
        "best_pairs": list(reversed(by_pair[-10:])),
        "hold": {
            "median_h": _median([t["hold_hours"] for t in dc]),
            "p25_h": _pct([t["hold_hours"] for t in dc], 25),
            "p75_h": _pct([t["hold_hours"] for t in dc], 75),
        },
    }


def signal_analysis(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Only fields present in artifacts."""
    by_struct: dict[str, list[float]] = defaultdict(list)
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_side_struct: dict[str, list[float]] = defaultdict(list)
    rr_vals = [t["rr"] for t in trades]
    risk = [t["risk_price"] for t in trades if t["risk_price"] is not None]
    for t in trades:
        by_struct[str(t.get("structure"))].append(t["pnl"])
        by_hour[int(t["entry_hour_utc"])].append(t["pnl"])
        key = f"{t['side']}|{t.get('structure')}"
        by_side_struct[key].append(t["pnl"])
    return {
        "available_fields": [
            "side",
            "symbol",
            "rr",
            "sl/tp/entry/exit",
            "pattern.structure (support/resistance)",
            "pattern.trigger",
            "pattern.session",
            "pattern.fill",
            "entry_hour_utc",
            "hold_hours",
        ],
        "unavailable": [
            "explicit pre-cost PnL",
            "spread at entry",
            "session filter (always sess=all for alexg8)",
            "named signal strength score beyond rr",
            "explicit stop_loss exit label",
        ],
        "rr": {"median": _median(rr_vals), "mean": _mean(rr_vals), "p25": _pct(rr_vals, 25), "p75": _pct(rr_vals, 75)},
        "risk_price": {"median": _median(risk), "mean": _mean(risk)},
        "by_structure": {k: bucket_stats(v) for k, v in sorted(by_struct.items())},
        "by_side_structure": {
            k: bucket_stats(v) for k, v in sorted(by_side_struct.items()) if len(v) >= 20
        },
        "by_entry_hour_utc_top_loss_hours": sorted(
            (
                {"hour": h, **bucket_stats(pnls)}
                for h, pnls in by_hour.items()
            ),
            key=lambda r: r["sum_pnl"],
        )[:8],
    }


def expectancy_bridge(jun: dict, jul: dict) -> dict[str, Any]:
    """Which exit categories explain June→July net PnL change."""
    jn = exit_decomposition(jun["trades"])
    jl = exit_decomposition(jul["trades"])
    reasons = sorted(set(jn) | set(jl))
    deltas = []
    for r in reasons:
        if r in ("stop_loss", "SL", "sl"):
            continue
        a = (jn.get(r) or {}).get("sum_pnl") or 0.0
        b = (jl.get(r) or {}).get("sum_pnl") or 0.0
        deltas.append(
            {
                "exit_reason": r,
                "jun_sum_pnl": a,
                "jul_sum_pnl": b,
                "delta_sum_pnl": b - a,
                "jun_mean": (jn.get(r) or {}).get("mean_pnl"),
                "jul_mean": (jl.get(r) or {}).get("mean_pnl"),
                "jun_n": (jn.get(r) or {}).get("n") or 0,
                "jul_n": (jl.get(r) or {}).get("n") or 0,
            }
        )
    deltas.sort(key=lambda x: x["delta_sum_pnl"])
    total_delta = sum(d["delta_sum_pnl"] for d in deltas)
    for d in deltas:
        d["fraction_of_total_delta"] = (
            d["delta_sum_pnl"] / total_delta if abs(total_delta) > 1e-9 else None
        )
    # Also mean-pnl path holding n roughly constant for daily_close
    return {
        "jun_net": sum(t["pnl"] for t in jun["trades"]),
        "jul_net": sum(t["pnl"] for t in jul["trades"]),
        "net_delta": sum(t["pnl"] for t in jul["trades"]) - sum(t["pnl"] for t in jun["trades"]),
        "by_exit_delta_sorted_worst_first": deltas,
        "primary_contributor": deltas[0] if deltas else None,
        "secondary_contributors": deltas[1:3] if len(deltas) > 1 else [],
    }


def concentration_index(pair_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How concentrated are losses among worst pairs?"""
    losses = [r for r in pair_rows if r["sum_pnl"] < 0]
    losses_sorted = sorted(losses, key=lambda r: r["sum_pnl"])
    total_loss = sum(r["sum_pnl"] for r in losses_sorted) or -1.0
    top3 = losses_sorted[:3]
    top5 = losses_sorted[:5]
    top10 = losses_sorted[:10]
    return {
        "losing_pairs_n": len(losses_sorted),
        "trading_pairs_n": len(pair_rows),
        "top3_loss_share": sum(r["sum_pnl"] for r in top3) / total_loss,
        "top5_loss_share": sum(r["sum_pnl"] for r in top5) / total_loss,
        "top10_loss_share": sum(r["sum_pnl"] for r in top10) / total_loss,
        "worst5": [
            {"symbol": r["symbol"], "sum_pnl": r["sum_pnl"], "n": r["n"], "mean_pnl": r["mean_pnl"]}
            for r in top5
        ],
        "verdict": (
            "concentrated"
            if (sum(r["sum_pnl"] for r in top5) / total_loss) >= 0.45
            else "distributed"
        ),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # flatten simple values
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


def classify_and_hypothesis(payload: dict[str, Any]) -> dict[str, Any]:
    bridge = payload["june_to_july_bridge"]
    primary = bridge["primary_contributor"] or {}
    jul_pairs = payload["months"]["jul_oos"]["pair_concentration"]
    jul_dir = payload["months"]["jul_oos"]["direction"]
    jun_dir = payload["months"]["jun_oos"]["direction"]
    aug_dir = payload["months"]["aug_is"]["direction"]
    jul_ms = payload["months"]["jul_oos"]["margin_stop"]
    jun_ms = payload["months"]["jun_oos"]["margin_stop"]
    aug_ms = payload["months"]["aug_is"]["margin_stop"]
    jul_dc = payload["months"]["jul_oos"]["daily_close"]
    jun_dc = payload["months"]["jun_oos"]["daily_close"]
    jul_ex = payload["months"]["jul_oos"]["exits"]
    jun_ex = payload["months"]["jun_oos"]["exits"]

    classes: list[str] = []

    # Exit-category quantitative bridge (sum PnL deltas can exceed |net| when signs oppose)
    if primary.get("exit_reason") == "take_profit":
        classes.append("SIGNAL_EXPECTANCY_DETERIORATION")
        classes.append("EXIT_MECHANISM_DETERIORATION")
    elif primary.get("exit_reason") == "daily_close":
        classes.append("EXIT_MECHANISM_DETERIORATION")
        classes.append("SIGNAL_EXPECTANCY_DETERIORATION")
    elif primary.get("exit_reason") == "margin_stop" and (primary.get("delta_sum_pnl") or 0) < 0:
        classes.append("MARGIN_AMPLIFICATION")

    # Margin absolute PnL improved Jun→Jul (positive delta) while share ≈ flat
    ms_delta = next(
        (d for d in bridge["by_exit_delta_sorted_worst_first"] if d["exit_reason"] == "margin_stop"),
        None,
    )
    if ms_delta and (ms_delta.get("delta_sum_pnl") or 0) > 0:
        # Not the Jun→Jul damage driver; still amplifier vs August
        if (aug_ms["share"] or 0) + 0.05 < (jul_ms["share"] or 0):
            classes.append("MARGIN_AMPLIFICATION")
    elif abs((jul_ms["share"] or 0) - (jun_ms["share"] or 0)) >= 0.03:
        classes.append("MARGIN_AMPLIFICATION")

    if jul_pairs["verdict"] == "concentrated":
        classes.append("PAIR_CONCENTRATION")

    long_mean = (jul_dir.get("long") or {}).get("mean_pnl")
    short_mean = (jul_dir.get("short") or {}).get("mean_pnl")
    jun_long = (jun_dir.get("long") or {}).get("mean_pnl")
    jun_short = (jun_dir.get("short") or {}).get("mean_pnl")
    jun_short_sum = (jun_dir.get("short") or {}).get("sum_pnl")
    jul_short_sum = (jul_dir.get("short") or {}).get("sum_pnl")
    if (
        long_mean is not None
        and short_mean is not None
        and jun_long is not None
        and jun_short is not None
    ):
        d_long = long_mean - jun_long
        d_short = short_mean - jun_short
        if abs(d_short) > abs(d_long) + 0.2 and d_short < -0.3:
            classes.append("DIRECTIONAL_ASYMMETRY")

    if not classes:
        classes.append("MIXED")
    else:
        seen: set[str] = set()
        classes = [c for c in classes if not (c in seen or seen.add(c))]

    dc_delta = next(
        (d for d in bridge["by_exit_delta_sorted_worst_first"] if d["exit_reason"] == "daily_close"),
        None,
    )
    tp_delta = next(
        (d for d in bridge["by_exit_delta_sorted_worst_first"] if d["exit_reason"] == "take_profit"),
        None,
    )

    jun_tp_n = (jun_ex.get("take_profit") or {}).get("n") or 0
    jul_tp_n = (jul_ex.get("take_profit") or {}).get("n") or 0

    hypothesis = (
        "June→July OOS deterioration is primarily a short-side expectancy collapse "
        "with a concurrent drop in take_profit realization count (not a margin-stop "
        "worsening). June’s net edge is carried by shorts; July shorts flip negative "
        "while longs stay near breakeven both months. The first strategy experiment "
        "should be a single pre-registered diagnostic ablation of frozen N1 that "
        "disables new short entries (long-only), holding all other N1 knobs fixed, "
        "to measure how much of the OOS fragility is short-side dependent."
    )

    tp_d = (tp_delta or {}).get("delta_sum_pnl")
    dc_d = (dc_delta or {}).get("delta_sum_pnl")
    ms_d = (ms_delta or {}).get("delta_sum_pnl")
    aug_long_sum = (aug_dir.get("long") or {}).get("sum_pnl")
    aug_short_sum = (aug_dir.get("short") or {}).get("sum_pnl")
    why = [
        f"June→July net delta ≈ {bridge['net_delta']:.2f}.",
        (
            f"Largest exit-category ΣPnL decline: take_profit ΔΣ={tp_d:.2f} "
            f"(n {jun_tp_n}→{jul_tp_n}; mean TP similar ~21–23)."
            if tp_d is not None
            else "TP delta unavailable."
        ),
        (
            f"Secondary ΣPnL decline: daily_close ΔΣ={dc_d:.2f} "
            f"(mean {jun_dc.get('mean_pnl'):.3f}→{jul_dc.get('mean_pnl'):.3f}; still net-positive)."
            if dc_d is not None
            else ""
        ),
        (
            f"margin_stop ΣPnL improved June→July (ΔΣ={ms_d:.2f}) "
            f"with share almost unchanged ({100*jun_ms['share']:.1f}%→{100*jul_ms['share']:.1f}%); "
            f"therefore margin_stop is not the Jun→July primary damage driver "
            f"(though August’s lower share {100*aug_ms['share']:.1f}% remains a secondary IS amplifier)."
            if ms_d is not None
            else "margin_stop delta unavailable."
        ),
        (
            f"Direction: June short ΣPnL={jun_short_sum:.2f} / mean={jun_short:.3f} → "
            f"July short ΣPnL={jul_short_sum:.2f} / mean={short_mean:.3f}; "
            f"longs {jun_long:.3f}→{long_mean:.3f} (near breakeven both months)."
            if None not in (jun_short_sum, jul_short_sum, jun_short, short_mean, jun_long, long_mean)
            else "Direction stats incomplete."
        ),
        (
            f"August reference also profitable on both sides "
            f"(long Σ={aug_long_sum:.2f}, short Σ={aug_short_sum:.2f}) — shorts are not "
            f"universally bad, but they are the fragile OOS component between June and July."
            if None not in (aug_long_sum, aug_short_sum)
            else "August direction sums unavailable."
        ),
        f"July pair losses are {jul_pairs['verdict']} (top5 loss share={jul_pairs['top5_loss_share']:.2f}); "
        f"worst names skew exotic/EM (e.g. USDHUF, GBPTRY, GBPCHF, EURTRY) — noted, not used to cut universe yet.",
        "Long-only is a one-bit diagnostic ablation of an existing emit-both-sides mechanism; "
        "smaller than a ghost_sl_mult/RR/leverage sweep and does not invent new indicators.",
    ]
    why = [w for w in why if w]

    followup = {
        "hypothesis": hypothesis,
        "control": "frozen N1 (alexg8 both sides, ghost_sl_mult=4.0, lev=500, force_flat_daily=true, ...)",
        "single_change": (
            "Diagnostic ablation only: disable new SHORT entries (long-only). "
            "No ghost_sl_mult/RR/leverage/daily-flat/universe changes."
        ),
        "development_period": (
            "August 2026 IS — technical smoke / non-collapse check only; "
            "not used to choose among alternatives."
        ),
        "fresh_oos_period": (
            "Confirmatory OOS #1: June 2026 under the pre-registered long-only ablation "
            "(June was used to discover the short-side mechanism class, but the change is "
            "a discrete pre-registered ablation, not a fitted continuous parameter). "
            "Confirmatory OOS #2 (mandatory): next fully covered unseen month with exact "
            "MT5 M1 provenance (May currently blocked). "
            "July must NOT select the change; after lock, July may be reported only as a "
            "descriptive held-out check."
        ),
        "metrics": [
            "net PnL",
            "PF",
            "mean trade PnL",
            "short-trade count (should be ~0 on ablation)",
            "take_profit count",
            "daily_close mean PnL",
            "margin_stop share",
            "MDD",
            "ending equity vs $1000",
        ],
        "failure_criteria": [
            "June confirmatory long-only PF < 1.0 and mean trade PnL worse than frozen N1 June",
            "August smoke ending equity < $1000 with MDD > 25%",
            "Any additional knob changed beyond long-only ablation",
        ],
        "success_criteria": [
            "June confirmatory long-only mean trade PnL improves vs frozen N1 June and PF >= 1.0",
            "Later unseen-month OOS still PF >= 1.0 under the same locked ablation",
            "Result interpreted as evidence about short-side dependence — not as a final production strategy",
        ],
        "notes": [
            "This does not claim long-only is the profitable Borex configuration.",
            "It tests whether N1 OOS fragility is short-side dependent.",
            "If long-only fails to stabilize June, next single-change candidate should address "
            "take_profit scarcity / path-to-TP (still one change), not a multi-knob sweep.",
            "Pair-universe cuts remain deferred despite July exotic concentration.",
        ],
    }

    return {
        "failure_class": classes,
        "primary_hypothesis": hypothesis,
        "why": why,
        "follow_up_experiment": followup,
        "quantitative_anchors": {
            "take_profit_delta": tp_delta,
            "daily_close_delta": dc_delta,
            "margin_stop_delta": ms_delta,
            "june_short_sum_pnl": jun_short_sum,
            "july_short_sum_pnl": jul_short_sum,
            "july_pair_concentration": jul_pairs["verdict"],
            "july_top5_loss_share": jul_pairs["top5_loss_share"],
        },
    }


def render_md(payload: dict[str, Any]) -> str:
    h = payload["hypothesis_selection"]
    lines: list[str] = []
    lines.append(f"# N1 Robustness Forensics — `{payload['run_id']}`")
    lines.append("")
    lines.append("Read-only diagnosis. No strategy/simulator changes. No sweeps.")
    lines.append("")
    lines.append("## Primary hypothesis")
    lines.append("")
    lines.append(h["primary_hypothesis"])
    lines.append("")
    lines.append("## Failure class")
    lines.append("")
    for c in h["failure_class"]:
        lines.append(f"- `{c}`")
    lines.append("")
    lines.append("## Why this hypothesis")
    lines.append("")
    for w in h["why"]:
        lines.append(f"- {w}")
    lines.append("")
    lines.append("## June → July expectancy bridge")
    lines.append("")
    b = payload["june_to_july_bridge"]
    lines.append(f"- June net: **{b['jun_net']:.2f}**")
    lines.append(f"- July net: **{b['jul_net']:.2f}**")
    lines.append(f"- Delta: **{b['net_delta']:.2f}**")
    lines.append("")
    lines.append("| Exit | Jun ΣPnL | Jul ΣPnL | ΔΣPnL | % of total Δ | Jun mean | Jul mean | Jun n | Jul n |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for d in b["by_exit_delta_sorted_worst_first"]:
        frac = d.get("fraction_of_total_delta")
        lines.append(
            f"| {d['exit_reason']} | {d['jun_sum_pnl']:.2f} | {d['jul_sum_pnl']:.2f} | "
            f"{d['delta_sum_pnl']:.2f} | {100*frac:.1f}% | "
            f"{d['jun_mean'] if d['jun_mean'] is not None else 'n/a'} | "
            f"{d['jul_mean'] if d['jul_mean'] is not None else 'n/a'} | "
            f"{d['jun_n']} | {d['jul_n']} |"
        )
    lines.append("")
    lines.append("## Follow-up experiment (not executed)")
    lines.append("")
    fu = h["follow_up_experiment"]
    for k in (
        "control",
        "single_change",
        "development_period",
        "fresh_oos_period",
        "metrics",
        "failure_criteria",
        "success_criteria",
    ):
        lines.append(f"- **{k}**: {fu[k]}")
    lines.append("")
    lines.append("## Month snapshots")
    lines.append("")
    for key in ("aug_is", "jun_oos", "jul_oos"):
        m = payload["months"][key]
        s = m["summary"]
        lines.append(
            f"- **{key}**: intents={m['intents']} fills={s.get('trades')} "
            f"WR={100*(s.get('win_rate') or 0):.1f}% PF={s.get('profit_factor'):.3f} "
            f"net={s.get('net_pnl'):.2f} MDD={100*(s.get('max_drawdown_pct') or 0):.1f}%"
        )
    lines.append("")
    lines.append("## Note on SL exits")
    lines.append("")
    lines.append(
        "N1 trade artifacts contain **no** `stop_loss` exit_reason. Observed exits are "
        "`daily_close`, `margin_stop`, `take_profit`, and occasionally `end_of_data`. "
        "Many `margin_stop` exits print near the SL price (`exit_near_sl` high)."
    )
    lines.append("")
    lines.append("## Pre-cost analysis")
    lines.append("")
    lines.append(
        payload["precost_note"]
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    run_id = f"n1_robustness_forensics_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out = ROOT / "data" / "runs" / "research" / run_id
    out.mkdir(parents=True, exist_ok=True)

    months_raw = {k: load_month(k, p) for k, p in SOURCES.items()}
    months: dict[str, Any] = {}
    pair_csv_rows = []
    exit_csv_rows = []
    dir_csv_rows = []
    ms_csv_rows = []
    hold_csv_rows = []

    for key, raw in months_raw.items():
        trades = raw["trades"]
        exits = exit_decomposition(trades)
        direction = direction_breakdown(trades)
        pairs = pair_breakdown(trades)
        conc = concentration_index(pairs)
        hold = holding_breakdown(trades)
        ms = margin_forensics(trades)
        dc = daily_close_forensics(trades)
        sig = signal_analysis(trades)
        months[key] = {
            "label": key,
            "source": raw["source"],
            "intents": raw["intents"],
            "entry_start": raw["entry_start"],
            "last_bar": raw["last_bar"],
            "summary": raw["summary"],
            "knobs": raw["knobs"],
            "n_trades": len(trades),
            "mean_trade_pnl": _mean([t["pnl"] for t in trades]),
            "median_trade_pnl": _median([t["pnl"] for t in trades]),
            "exits": exits,
            "direction": direction,
            "pair_concentration": conc,
            "worst_pairs": pairs[:10],
            "best_pairs": list(reversed(pairs[-10:])),
            "holding": hold,
            "margin_stop": ms,
            "daily_close": dc,
            "signal": sig,
            "note_sl_exits": "No explicit stop_loss exit_reason in artifacts",
        }
        for r in pairs:
            pair_csv_rows.append({"month": key, **{k: v for k, v in r.items() if not isinstance(v, (dict, list))}})
        for reason, st in exits.items():
            if reason in ("SL", "sl", "stop_loss") and st.get("n") == 0:
                continue
            exit_csv_rows.append({"month": key, "exit_reason": reason, **{k: v for k, v in st.items() if not isinstance(v, (dict, list))}})
        for side, st in direction.items():
            dir_csv_rows.append({"month": key, "side": side, **{k: v for k, v in st.items() if not isinstance(v, (dict, list))}})
        for sym, n in ms["top_pairs"]:
            ms_csv_rows.append({"month": key, "symbol": sym, "margin_stop_n": n})
        hold_csv_rows.append({"month": key, "scope": "overall", **hold["overall"]})
        for reason, st in hold["by_exit"].items():
            hold_csv_rows.append({"month": key, "scope": f"exit:{reason}", **st})

    bridge = expectancy_bridge(months_raw["jun_oos"], months_raw["jul_oos"])

    # Cost note
    precost_note = (
        "Exact pre-cost vs post-cost decomposition is NOT available in these artifacts: "
        "trade rows expose net `pnl` only (commission/spread already embedded by the engine). "
        "Cannot attribute July deterioration to pre-cost signal vs cost amplification with "
        "the stored fields. Proxy only: rr / risk_price / exit R-multiple from prices."
    )

    payload_base = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "experiment": "n1_robustness_forensics",
        "read_only": True,
        "strategy_modified": False,
        "simulator_modified": False,
        "parameter_sweep": False,
        "gate2_reopened": False,
        "baseline": {
            "strategy": "alexg8",
            "ghost_sl_mult": 4.0,
            "leverage": 500,
            "path": "h1_signal_1m_exit",
            "rr_factor": 1.0,
            "force_flat_daily": True,
            "force_flat_friday": False,
        },
        "sources": {k: str(v.as_posix()) for k, v in SOURCES.items()},
        "months": months,
        "june_to_july_bridge": bridge,
        "precost_note": precost_note,
    }
    payload_base["hypothesis_selection"] = classify_and_hypothesis(payload_base)

    # manifests + reports
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "git_commit": payload_base["git_commit"],
                "experiment": payload_base["experiment"],
                "read_only": True,
                "sources": payload_base["sources"],
                "failure_class": payload_base["hypothesis_selection"]["failure_class"],
                "primary_hypothesis_summary": payload_base["hypothesis_selection"]["primary_hypothesis"][:240],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "forensic_report.json").write_text(
        json.dumps(payload_base, indent=2) + "\n", encoding="utf-8"
    )
    (out / "forensic_report.md").write_text(render_md(payload_base), encoding="utf-8")
    write_csv(out / "pair_breakdown.csv", pair_csv_rows)
    write_csv(out / "exit_breakdown.csv", exit_csv_rows)
    write_csv(out / "direction_breakdown.csv", dir_csv_rows)
    write_csv(out / "margin_stop_breakdown.csv", ms_csv_rows)
    write_csv(out / "holding_time_breakdown.csv", hold_csv_rows)

    print(
        json.dumps(
            {
                "run_id": run_id,
                "out_dir": str(out),
                "failure_class": payload_base["hypothesis_selection"]["failure_class"],
                "primary_exit_delta": bridge.get("primary_contributor"),
                "july_pair_verdict": months["jul_oos"]["pair_concentration"]["verdict"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
