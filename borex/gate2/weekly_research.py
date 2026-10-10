"""Weekly research helpers for Gate 2 strategy expansion (offline).

Weekly batches are smaller chronological evaluation units; adjacent weeks may
remain correlated. September is reserved and must not enter selection.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import pandas as pd

THURSDAY = 3
SEPTEMBER_RESERVED = (2026, 9)


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def iso_week_key(ts) -> str:
    t = _ts(ts)
    iso = t.isocalendar()
    return f"{int(iso.year)}-W{int(iso.week):02d}"


def week_start_monday(ts) -> pd.Timestamp:
    t = _ts(ts).normalize()
    return t - pd.Timedelta(days=int(t.dayofweek))


def is_september_reserved(ts) -> bool:
    t = _ts(ts)
    return (t.year, t.month) == SEPTEMBER_RESERVED


def bucket_stats(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {
            "n": 0,
            "net": 0.0,
            "mean": None,
            "median": None,
            "wr": None,
            "pf": None,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
        }
    gp = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    pf = (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None)
    return {
        "n": len(pnls),
        "net": float(sum(pnls)),
        "mean": float(statistics.mean(pnls)),
        "median": float(statistics.median(pnls)),
        "wr": sum(1 for x in pnls if x > 0) / len(pnls),
        "pf": pf,
        "gross_profit": float(gp),
        "gross_loss": float(gl),
    }


def summarize_trades(
    trades: list[dict[str, Any]],
    *,
    intents: int | None = None,
    mdd: float | None = None,
    starting_equity: float = 1000.0,
) -> dict[str, Any]:
    pnls = [float(t["pnl"]) for t in trades]
    st = bucket_stats(pnls)
    exits = Counter(str(t.get("exit_reason")) for t in trades)
    sides = Counter(str(t.get("side")).lower() for t in trades)
    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        by_day[str(_ts(t["entry_time"]).date())] += float(t["pnl"])
    day_pnls = list(by_day.values())
    max_cl = cur = 0
    for p in pnls:
        if p <= 0:
            cur += 1
            max_cl = max(max_cl, cur)
        else:
            cur = 0
    thu = [t for t in trades if _ts(t["entry_time"]).dayofweek == THURSDAY]
    non = [t for t in trades if _ts(t["entry_time"]).dayofweek != THURSDAY]
    n = max(1, len(trades))
    return {
        "intents": intents,
        "fills": len(trades),
        "WR": st["wr"],
        "PF": st["pf"],
        "net_pnl": st["net"],
        "mean_trade": st["mean"],
        "median_trade": st["median"],
        "MDD": mdd,
        "ending_equity": starting_equity + st["net"],
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
        "worst_day_pnl": min(day_pnls) if day_pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "max_consecutive_losses": max_cl,
        "thursday_fills": len(thu),
        "non_thursday_fills": len(non),
        "thursday_net": float(sum(float(t["pnl"]) for t in thu)) if thu else 0.0,
        "pair_top": (
            Counter(t["symbol"] for t in trades).most_common(1)[0]
            if trades
            else None
        ),
    }


def weekly_slices(
    trades: list[dict[str, Any]],
    *,
    exclude_september: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        if exclude_september and is_september_reserved(t["entry_time"]):
            continue
        out[iso_week_key(t["entry_time"])].append(t)
    return dict(sorted(out.items()))


def weekly_metrics_table(
    trades: list[dict[str, Any]],
    *,
    candidate_id: str,
    exclude_september: bool = True,
) -> list[dict[str, Any]]:
    rows = []
    for week, ts in weekly_slices(trades, exclude_september=exclude_september).items():
        s = summarize_trades(ts)
        ws = week_start_monday(ts[0]["entry_time"])
        rows.append(
            {
                "candidate_id": candidate_id,
                "week": week,
                "week_start": str(ws.date()),
                **{k: v for k, v in s.items() if not isinstance(v, (dict, list, tuple))},
            }
        )
    return rows


def weekly_distribution(week_rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [float(r["net_pnl"]) for r in week_rows if r.get("net_pnl") is not None]
    if not nets:
        return {"n_weeks": 0}
    pos = sum(1 for x in nets if x > 0)
    neg = sum(1 for x in nets if x <= 0)
    # longest losing week sequence
    longest = cur = 0
    for x in nets:
        if x <= 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return {
        "n_weeks": len(nets),
        "profitable_weeks": pos,
        "negative_weeks": neg,
        "pct_profitable_weeks": pos / len(nets),
        "avg_weekly_net": float(statistics.mean(nets)),
        "median_weekly_net": float(statistics.median(nets)),
        "best_week_net": max(nets),
        "worst_week_net": min(nets),
        "longest_losing_week_streak": longest,
        "total_net": float(sum(nets)),
    }


def rolling_week_nets(week_rows: list[dict[str, Any]], window: int) -> list[dict[str, Any]]:
    ordered = sorted(week_rows, key=lambda r: r["week"])
    nets = [float(r["net_pnl"]) for r in ordered]
    out = []
    for i in range(len(ordered)):
        if i + 1 < window:
            continue
        chunk = nets[i + 1 - window : i + 1]
        out.append(
            {
                "week": ordered[i]["week"],
                "window": window,
                "rolling_net": float(sum(chunk)),
                "rolling_mean_week": float(statistics.mean(chunk)),
            }
        )
    return out


def walk_forward_splits(
    week_keys: list[str],
    *,
    min_train: int = 4,
    val_size: int = 2,
) -> list[dict[str, Any]]:
    """Chronological expanding/rolling walk-forward over ISO week keys."""
    keys = sorted(week_keys)
    splits = []
    i = min_train
    while i + val_size <= len(keys):
        train = keys[:i]
        val = keys[i : i + val_size]
        splits.append(
            {
                "split_id": f"wf_{len(splits)+1}",
                "train_weeks": train,
                "validation_weeks": val,
                "train_n": len(train),
                "val_n": len(val),
            }
        )
        i += val_size
    return splits


def metrics_for_weeks(
    trades: list[dict[str, Any]], week_keys: list[str]
) -> dict[str, Any]:
    want = set(week_keys)
    sub = [t for t in trades if iso_week_key(t["entry_time"]) in want]
    return summarize_trades(sub)


def filter_session_hours(tape: list[dict], session: str) -> list[dict]:
    """Research-layer session allowlist on decision time_unix (UTC)."""
    if session == "all":
        return list(tape)

    def ok(hour: int) -> bool:
        if session == "asia":
            return 0 <= hour < 9
        if session == "london":
            return 7 <= hour < 16
        if session == "newyork":
            return 12 <= hour < 21
        if session == "overlap":
            return 12 <= hour < 16
        raise ValueError(session)

    out = []
    for row in tape:
        # time_unix is epoch seconds (same as thursday_entry_gate)
        hour = int(pd.Timestamp(float(row["time_unix"]), unit="s", tz="UTC").hour)
        if ok(hour):
            out.append(row)
    return out


def filter_sides(tape: list[dict], sides: str) -> list[dict]:
    if sides == "both":
        return list(tape)
    if sides == "long_only":
        return [r for r in tape if str(r.get("action", "")).lower() in {"buy", "long"}]
    if sides == "short_only":
        return [r for r in tape if str(r.get("action", "")).lower() in {"sell", "short"}]
    raise ValueError(sides)


def compound_label(months: list[str]) -> str:
    return f"compound_{len(months)}m_" + "_".join(months)


# ---------------------------------------------------------------------------
# Walk-forward candidate laboratory — selection rules (pre-registered)
# ---------------------------------------------------------------------------

# Documented before selection runs. Fraction of control train fills required.
MIN_TRAIN_FILL_FRACTION = 0.50
MIN_TRAIN_FILLS_ABS = 30


def complete_iso_weeks(
    week_rows: list[dict[str, Any]],
    *,
    research_start: str = "2026-06-01",
    research_end: str = "2026-08-31",
) -> list[str]:
    """ISO weeks whose Monday..Sunday both fall inside the research window.

    Partial weeks that spill into September are excluded even if Sep trades
    were already stripped — the calendar unit itself is incomplete.
    """
    start = _ts(research_start).normalize()
    end = _ts(research_end).normalize()
    seen: dict[str, pd.Timestamp] = {}
    for r in week_rows:
        w = str(r["week"])
        if w in seen:
            continue
        ws = _ts(r["week_start"]).normalize() if r.get("week_start") else None
        if ws is None:
            continue
        seen[w] = ws
    out = []
    for w, ws in sorted(seen.items()):
        we = ws + pd.Timedelta(days=6)
        if ws >= start and we <= end:
            out.append(w)
    return out


def aggregate_week_rows(
    week_rows: list[dict[str, Any]], week_keys: list[str]
) -> dict[str, Any]:
    """Aggregate precomputed weekly metric rows over a subset of weeks."""
    want = set(week_keys)
    rows = [r for r in week_rows if r["week"] in want]
    if not rows:
        return {
            "fills": 0,
            "PF": None,
            "net_pnl": 0.0,
            "mean_trade": None,
            "median_trade": None,
            "MDD": None,
            "TP": 0,
            "daily_close": 0,
            "margin_stop": 0,
            "long_count": 0,
            "short_count": 0,
            "thursday_fills": 0,
            "thursday_net": 0.0,
            "profitable_weeks": 0,
            "losing_weeks": 0,
            "worst_week": None,
            "best_week": None,
            "consecutive_losing_weeks": 0,
            "n_weeks": 0,
        }
    fills = int(sum(int(r.get("fills") or 0) for r in rows))
    net = float(sum(float(r.get("net_pnl") or 0) for r in rows))
    # reconstruct PF from weekly gross is impossible without trade lists;
    # approximate mean as net/fills; PF left as volume-weighted of weekly PF
    # when fills known — prefer trade-level metrics_for_weeks when available.
    mean = (net / fills) if fills else None
    nets = [float(r.get("net_pnl") or 0) for r in sorted(rows, key=lambda x: x["week"])]
    pos_w = sum(1 for x in nets if x > 0)
    neg_w = sum(1 for x in nets if x <= 0)
    longest = cur = 0
    for x in nets:
        if x <= 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    # volume-weighted PF proxy from weekly PF when present
    pf_num = pf_den = 0.0
    for r in rows:
        pf = r.get("PF")
        f = int(r.get("fills") or 0)
        if pf is None or f <= 0:
            continue
        # recover gp/gl from pf ≈ gp/|gl| and net = gp+gl is underdetermined;
        # use fill-weighted average of finite weekly PFs as descriptive only
        try:
            pf_f = float(pf)
        except (TypeError, ValueError):
            continue
        if pf_f == float("inf"):
            continue
        pf_num += pf_f * f
        pf_den += f
    pf_proxy = (pf_num / pf_den) if pf_den else None
    medians = [float(r["median_trade"]) for r in rows if r.get("median_trade") is not None]
    return {
        "fills": fills,
        "PF": pf_proxy,
        "net_pnl": net,
        "mean_trade": mean,
        "median_trade": float(statistics.median(medians)) if medians else None,
        "MDD": None,  # path-dependent; use trade-level when available
        "TP": int(sum(int(r.get("TP") or 0) for r in rows)),
        "daily_close": int(sum(int(r.get("daily_close") or 0) for r in rows)),
        "margin_stop": int(sum(int(r.get("margin_stop") or 0) for r in rows)),
        "long_count": int(sum(int(r.get("long_count") or 0) for r in rows)),
        "short_count": int(sum(int(r.get("short_count") or 0) for r in rows)),
        "thursday_fills": int(sum(int(r.get("thursday_fills") or 0) for r in rows)),
        "thursday_net": float(sum(float(r.get("thursday_net") or 0) for r in rows)),
        "profitable_weeks": pos_w,
        "losing_weeks": neg_w,
        "worst_week": min(nets) if nets else None,
        "best_week": max(nets) if nets else None,
        "consecutive_losing_weeks": longest,
        "n_weeks": len(rows),
        "median_weekly_net": float(statistics.median(nets)) if nets else None,
        "mean_weekly_net": float(statistics.mean(nets)) if nets else None,
    }


def min_train_fills_threshold(control_train_fills: int) -> int:
    """Pre-registered minimum fills: 50% of control train fills, floor 30."""
    return max(MIN_TRAIN_FILLS_ABS, int(MIN_TRAIN_FILL_FRACTION * int(control_train_fills)))


def candidate_passes_fill_gate(candidate_fills: int, control_train_fills: int) -> bool:
    return int(candidate_fills) >= min_train_fills_threshold(control_train_fills)


def selection_rank_key(train_metrics: dict[str, Any]) -> tuple:
    """Higher is better. No subjective score — lexicographic measurable keys."""
    return (
        float(train_metrics.get("median_weekly_net") or -1e18),
        float(train_metrics.get("PF") or -1e18),
        float(train_metrics.get("mean_trade") or -1e18),
        float(train_metrics.get("worst_week") or -1e18),
        int(train_metrics.get("fills") or 0),
    )


def select_candidate_on_train(
    *,
    candidate_train: dict[str, dict[str, Any]],
    control_id: str = "n1_control",
    eligible_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Select one candidate using ONLY training-window metrics.

    Hard gates vs control on the same train weeks:
      - fills >= 50% of control train fills (and >= 30)
      - train PF > control PF
      - train mean_trade > control mean_trade
    Then rank by selection_rank_key. Control itself is never auto-selected
    as 'treatment' unless no other eligible candidate exists.
    """
    if control_id not in candidate_train:
        raise KeyError(control_id)
    ctrl = candidate_train[control_id]
    ctrl_fills = int(ctrl.get("fills") or 0)
    thr = min_train_fills_threshold(ctrl_fills)
    ids = eligible_ids or list(candidate_train.keys())
    eligible = []
    for cid in ids:
        if cid == control_id:
            continue
        m = candidate_train[cid]
        if not candidate_passes_fill_gate(int(m.get("fills") or 0), ctrl_fills):
            continue
        c_pf, t_pf = ctrl.get("PF"), m.get("PF")
        c_mean, t_mean = ctrl.get("mean_trade"), m.get("mean_trade")
        if c_pf is None or t_pf is None or c_mean is None or t_mean is None:
            continue
        if float(t_pf) <= float(c_pf):
            continue
        if float(t_mean) <= float(c_mean):
            continue
        eligible.append((cid, m))
    if not eligible:
        return {
            "selected_id": control_id,
            "reason": "no_eligible_treatment",
            "min_train_fills": thr,
            "control_train_fills": ctrl_fills,
            "n_eligible": 0,
        }
    eligible.sort(key=lambda x: selection_rank_key(x[1]), reverse=True)
    best_id, best_m = eligible[0]
    return {
        "selected_id": best_id,
        "reason": "train_pf_and_mean_gt_control_then_rank",
        "min_train_fills": thr,
        "control_train_fills": ctrl_fills,
        "n_eligible": len(eligible),
        "selected_train_PF": best_m.get("PF"),
        "selected_train_mean": best_m.get("mean_trade"),
        "selected_train_median_weekly_net": best_m.get("median_weekly_net"),
    }


def thursday_harmful_on_train(control_train: dict[str, Any]) -> bool:
    """Prospective Thursday harm signal: train-window Thursday net < 0."""
    return float(control_train.get("thursday_net") or 0) < 0.0


def assert_no_future_leakage(splits: list[dict[str, Any]]) -> None:
    """Raise if any validation week is chronologically before any train week."""
    for sp in splits:
        train = sp["train_weeks"]
        val = sp["validation_weeks"]
        if not train or not val:
            raise AssertionError(f"empty train/val in {sp.get('split_id')}")
        if max(train) >= min(val):
            raise AssertionError(
                f"leakage in {sp.get('split_id')}: train={train} val={val}"
            )
        # expanding: later splits must not shrink earliest train start oddly
        # (allowed to expand). Only chronological order enforced here.
