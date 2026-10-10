"""Weekly strategy-expansion research helpers (offline, September-reserved).

Weekly batches are smaller chronological evaluation units; adjacent weeks may
remain correlated. This module does not modify the frozen simulator.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

# September 2026 reserved for locked OOS — never use for selection/training.
SEP_RESERVED_START = pd.Timestamp("2026-09-01", tz="UTC")
RESEARCH_END = pd.Timestamp("2026-09-01", tz="UTC")  # exclusive
RESEARCH_START = pd.Timestamp("2026-06-01", tz="UTC")

THURSDAY = 3


def ts_utc(value: Any) -> pd.Timestamp:
    t = pd.Timestamp(value)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def is_september_reserved(entry_time: Any) -> bool:
    return ts_utc(entry_time) >= SEP_RESERVED_START


def week_id(entry_time: Any) -> str:
    """ISO week label YYYY-Www (UTC)."""
    t = ts_utc(entry_time)
    iso = t.isocalendar()
    return f"{iso.year}-W{int(iso.week):02d}"


def week_bounds(week_label: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    year_s, w_s = week_label.split("-W")
    year, week = int(year_s), int(w_s)
    start = pd.Timestamp.fromisocalendar(year, week, 1).tz_localize("UTC")
    end = start + pd.Timedelta(days=7)
    return start, end


def research_weeks(
    *,
    start: pd.Timestamp = RESEARCH_START,
    end_exclusive: pd.Timestamp = RESEARCH_END,
) -> list[str]:
    """Fully contained ISO weeks inside [start, end_exclusive)."""
    # First Monday on/after start
    cur = start
    while cur.dayofweek != 0:
        cur += pd.Timedelta(days=1)
    out: list[str] = []
    while True:
        label = week_id(cur)
        w0, w1 = week_bounds(label)
        if w1 > end_exclusive:
            break
        if w0 >= start:
            out.append(label)
        cur = w1
    return out


def pf(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    gp = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    if gl == 0:
        return float("inf") if gp > 0 else None
    return float(gp / abs(gl))


def summarize_trades(trades: list[dict[str, Any]], *, intents: int | None = None) -> dict[str, Any]:
    pnls = [float(t["pnl"]) for t in trades]
    if not pnls:
        return {
            "intents": intents,
            "fills": 0,
            "WR": None,
            "PF": None,
            "net_pnl": 0.0,
            "mean_trade": None,
            "median_trade": None,
            "TP": 0,
            "daily_close": 0,
            "margin_stop": 0,
            "long_count": 0,
            "short_count": 0,
            "worst_trade": None,
            "max_consecutive_losses": 0,
            "thursday_fills": 0,
            "non_thursday_fills": 0,
        }
    wins = sum(1 for p in pnls if p > 0)
    exits = Counter(str(t["exit_reason"]) for t in trades)
    sides = Counter(str(t["side"]).lower() for t in trades)
    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        by_day[str(ts_utc(t["entry_time"]).date())] += float(t["pnl"])
    max_cl = cur = 0
    for p in pnls:
        if p <= 0:
            cur += 1
            max_cl = max(max_cl, cur)
        else:
            cur = 0
    thu = [t for t in trades if ts_utc(t["entry_time"]).dayofweek == THURSDAY]
    return {
        "intents": intents,
        "fills": len(trades),
        "WR": wins / len(trades),
        "PF": pf(pnls),
        "net_pnl": float(sum(pnls)),
        "mean_trade": float(statistics.mean(pnls)),
        "median_trade": float(statistics.median(pnls)),
        "TP": int(exits.get("take_profit", 0)),
        "daily_close": int(exits.get("daily_close", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
        "long_count": int(sides.get("long", 0) + sides.get("buy", 0)),
        "short_count": int(sides.get("short", 0) + sides.get("sell", 0)),
        "worst_trade": min(pnls),
        "worst_day_pnl": min(by_day.values()) if by_day else None,
        "max_consecutive_losses": max_cl,
        "thursday_fills": len(thu),
        "non_thursday_fills": len(trades) - len(thu),
        "exit_mix": dict(exits),
    }


def bucket_by_week(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        if is_september_reserved(t["entry_time"]):
            continue
        out[week_id(t["entry_time"])].append(t)
    return dict(out)


def weekly_metrics(trades: list[dict[str, Any]], weeks: Iterable[str]) -> list[dict[str, Any]]:
    by = bucket_by_week(trades)
    rows = []
    for w in weeks:
        st = summarize_trades(by.get(w, []))
        st["week"] = w
        w0, w1 = week_bounds(w)
        st["week_start"] = str(w0.date())
        st["week_end_exclusive"] = str(w1.date())
        rows.append(st)
    return rows


def weekly_distribution(week_rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [float(r["net_pnl"]) for r in week_rows]
    if not nets:
        return {"n_weeks": 0}
    pos = sum(1 for x in nets if x > 0)
    neg = sum(1 for x in nets if x < 0)
    return {
        "n_weeks": len(nets),
        "avg_weekly_net": float(statistics.mean(nets)),
        "median_weekly_net": float(statistics.median(nets)),
        "pct_profitable_weeks": pos / len(nets),
        "pct_negative_weeks": neg / len(nets),
        "best_week_net": max(nets),
        "worst_week_net": min(nets),
        "best_week": week_rows[nets.index(max(nets))]["week"],
        "worst_week": week_rows[nets.index(min(nets))]["week"],
        "sum_net": float(sum(nets)),
    }


def rolling_sums(week_rows: list[dict[str, Any]], window: int) -> list[dict[str, Any]]:
    out = []
    for i in range(len(week_rows)):
        if i + 1 < window:
            continue
        chunk = week_rows[i + 1 - window : i + 1]
        out.append(
            {
                "end_week": chunk[-1]["week"],
                "window": window,
                "net": sum(float(r["net_pnl"]) for r in chunk),
                "mean_week": statistics.mean(float(r["net_pnl"]) for r in chunk),
            }
        )
    return out


def walk_forward_splits(
    weeks: list[str], *, min_train: int = 4, val_len: int = 1
) -> list[dict[str, Any]]:
    """Expanding-window walk-forward: train[:k] → validate next val_len weeks."""
    splits = []
    i = min_train
    while i + val_len <= len(weeks):
        splits.append(
            {
                "train_weeks": weeks[:i],
                "val_weeks": weeks[i : i + val_len],
                "train_end": weeks[i - 1],
                "val_label": ",".join(weeks[i : i + val_len]),
            }
        )
        i += val_len
    return splits


def equity_curve_from_trades(
    trades: list[dict[str, Any]], *, starting_equity: float
) -> dict[str, Any]:
    """Path-dependent equity from chronologically sorted trade PnLs (fixed-size proxy).

    Note: this does NOT re-size positions. True margin compounding requires a
    sequential engine run; this curve is the fixed-equity cumulative path.
    """
    ordered = sorted(trades, key=lambda t: ts_utc(t["entry_time"]))
    eq = float(starting_equity)
    peak = eq
    max_dd = 0.0
    curve = [{"i": 0, "equity": eq, "pnl": 0.0, "entry_time": None}]
    for i, t in enumerate(ordered, 1):
        eq += float(t["pnl"])
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        curve.append(
            {
                "i": i,
                "equity": eq,
                "pnl": float(t["pnl"]),
                "entry_time": str(t["entry_time"]),
                "drawdown_from_peak": dd,
            }
        )
    return {
        "starting_equity": starting_equity,
        "ending_equity": eq,
        "total_return": (eq / starting_equity - 1.0) if starting_equity else None,
        "max_drawdown": max_dd,
        "n_trades": len(ordered),
        "curve": curve,
    }


def longest_losing_week_streak(week_rows: list[dict[str, Any]]) -> int:
    best = cur = 0
    for r in week_rows:
        if float(r["net_pnl"]) <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best
