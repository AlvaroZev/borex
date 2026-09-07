#!/usr/bin/env python3
"""Analyze live vs theory for the trading week."""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

TEMP = Path(os.environ["TEMP"])


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_ts(s):
    if not s:
        return None
    try:
        ts = pd.Timestamp(s)
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts


def side_norm(s: object) -> str:
    s = str(s or "").lower()
    if s in ("buy", "long"):
        return "buy"
    if s in ("sell", "short"):
        return "sell"
    return s


def hour_key(ts) -> str:
    if ts is None:
        return ""
    return str(ts.floor("h"))


def annotate(rows, source):
    out = []
    for r in rows:
        et = parse_ts(r.get("entry_time"))
        xt = parse_ts(r.get("exit_time"))
        out.append({**r, "_et": et, "_xt": xt, "_side": side_norm(r.get("side")), "_src": source})
    return out


def summarize(rows, label):
    closed = [r for r in rows if str(r.get("status")) == "closed"]
    opens = [r for r in rows if str(r.get("status")) == "open"]
    pnls = [float(r.get("pnl") or 0) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by_day: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for r in closed:
        d = str(r["_et"].date()) if r["_et"] is not None else "na"
        by_day[d]["n"] += 1
        by_day[d]["pnl"] += float(r.get("pnl") or 0)
        by_day[d]["wins"] += int(float(r.get("pnl") or 0) > 0)
    return {
        "label": label,
        "total": len(rows),
        "open": len(opens),
        "closed": len(closed),
        "pnl": sum(pnls),
        "wr": (len(wins) / len(closed) * 100 if closed else None),
        "avg_win": (sum(wins) / len(wins) if wins else None),
        "avg_loss": (sum(losses) / len(losses) if losses else None),
        "pf": (sum(wins) / abs(sum(losses)) if losses and sum(losses) else None),
        "by_day": dict(sorted(by_day.items())),
        "exits": dict(Counter(r.get("exit_reason") or "?" for r in closed)),
        "sides": dict(Counter(r["_side"] for r in rows)),
        "symbols": len({r["symbol"] for r in rows}),
    }


def main() -> int:
    raw = load_json(TEMP / "borex_week_trades.json")
    dash_path = TEMP / "borex_dashboard_week.json"
    dash = load_json(dash_path) if dash_path.exists() else {}

    week_start = pd.Timestamp("2026-08-17", tz="UTC")
    week_end = pd.Timestamp("2026-08-22", tz="UTC")
    epoch = pd.Timestamp("2026-08-14", tz="UTC")

    L = annotate(raw["live"], "live")
    T = annotate(raw["theory"], "theory")
    Lw = [r for r in L if r["_et"] is not None and week_start <= r["_et"] < week_end]
    Tw = [r for r in T if r["_et"] is not None and week_start <= r["_et"] < week_end]

    used: set[int] = set()
    matches = []
    theory_only = []
    for t in Tw:
        key = (t["symbol"], t["_side"], hour_key(t["_et"]))
        mi = None
        for i, l in enumerate(Lw):
            if i in used:
                continue
            if (l["symbol"], l["_side"], hour_key(l["_et"])) == key:
                mi = i
                break
        if mi is None:
            theory_only.append(t)
        else:
            used.add(mi)
            matches.append((t, Lw[mi]))
    live_only = [Lw[i] for i in range(len(Lw)) if i not in used]

    def entry_slip(t, l):
        try:
            return float(l["entry_price"]) - float(t["entry_price"])
        except Exception:
            return None

    def pnl_delta(t, l):
        if str(t.get("status")) != "closed" or str(l.get("status")) != "closed":
            return None
        return float(l.get("pnl") or 0) - float(t.get("pnl") or 0)

    slips = [entry_slip(t, l) for t, l in matches if entry_slip(t, l) is not None]
    pnl_deltas = [pnl_delta(t, l) for t, l in matches if pnl_delta(t, l) is not None]

    div = []
    for t, l in matches:
        d = pnl_delta(t, l)
        if d is None:
            continue
        div.append(
            {
                "symbol": t["symbol"],
                "side": t["_side"],
                "hour": hour_key(t["_et"]),
                "theory_pnl": float(t.get("pnl") or 0),
                "live_pnl": float(l.get("pnl") or 0),
                "delta": d,
                "theory_entry": float(t["entry_price"]),
                "live_entry": float(l["entry_price"]),
                "entry_delta": entry_slip(t, l),
                "theory_exit": t.get("exit_reason"),
                "live_exit": l.get("exit_reason"),
            }
        )
    div.sort(key=lambda x: abs(x["delta"]), reverse=True)

    live_closed_pnl = sum(float(r.get("pnl") or 0) for r in Lw if r.get("status") == "closed")
    theory_closed_pnl = sum(float(r.get("pnl") or 0) for r in Tw if r.get("status") == "closed")

    # Same-bar outcome agreement on matched closed
    same_sign = 0
    opp_sign = 0
    for t, l in matches:
        if str(t.get("status")) != "closed" or str(l.get("status")) != "closed":
            continue
        tp = float(t.get("pnl") or 0)
        lp = float(l.get("pnl") or 0)
        if tp == 0 or lp == 0:
            continue
        if (tp > 0) == (lp > 0):
            same_sign += 1
        else:
            opp_sign += 1

    report = {
        "week": "2026-08-17 → 2026-08-21 (entries UTC)",
        "live_week": summarize(Lw, "live"),
        "theory_week": summarize(Tw, "theory"),
        "live_epoch": summarize(
            [r for r in L if r["_et"] is not None and r["_et"] >= epoch], "live_epoch"
        ),
        "theory_epoch": summarize(
            [r for r in T if r["_et"] is not None and r["_et"] >= epoch], "theory_epoch"
        ),
        "match": {
            "matched": len(matches),
            "theory_only": len(theory_only),
            "live_only": len(live_only),
            "match_rate_of_theory_pct": (len(matches) / len(Tw) * 100 if Tw else None),
            "match_rate_of_live_pct": (len(matches) / len(Lw) * 100 if Lw else None),
            "same_sign_outcomes": same_sign,
            "opposite_sign_outcomes": opp_sign,
        },
        "slippage": {
            "n": len(slips),
            "mean_entry_delta": (sum(slips) / len(slips) if slips else None),
            "median_entry_delta": (float(pd.Series(slips).median()) if slips else None),
            "mean_pnl_delta": (sum(pnl_deltas) / len(pnl_deltas) if pnl_deltas else None),
            "median_pnl_delta": (float(pd.Series(pnl_deltas).median()) if pnl_deltas else None),
            "sum_pnl_delta_matched": (sum(pnl_deltas) if pnl_deltas else None),
        },
        "top_divergences": div[:20],
        "theory_only_top_symbols": Counter(r["symbol"] for r in theory_only).most_common(12),
        "live_only_top_symbols": Counter(r["symbol"] for r in live_only).most_common(12),
        "theory_only_hours": Counter(hour_key(r["_et"]) for r in theory_only).most_common(15),
        "live_only_hours": Counter(hour_key(r["_et"]) for r in live_only).most_common(15),
        "closed_pnl_week": {
            "live": live_closed_pnl,
            "theory": theory_closed_pnl,
            "delta_live_minus_theory": live_closed_pnl - theory_closed_pnl,
        },
        "portfolio": raw.get("portfolio"),
        "theory_state": raw.get("theory_state"),
        "waiting_ghosts": raw.get("waiting_ghosts"),
        "dashboard_runtime": dash.get("runtime"),
        "dashboard_account": dash.get("account"),
        "dashboard_theory": {
            "active": (dash.get("theory") or {}).get("active"),
            "equity": (dash.get("theory") or {}).get("equity"),
            "cash": (dash.get("theory") or {}).get("cash"),
            "open": len((dash.get("theory") or {}).get("open_trades") or []),
            "last_master_ts": (dash.get("theory") or {}).get("last_master_ts"),
        },
        "dashboard_open_live": len(dash.get("open_trades") or []),
        "theory_only_sample": [
            {
                "symbol": r["symbol"],
                "side": r["_side"],
                "entry": str(r["_et"]),
                "status": r.get("status"),
                "pnl": r.get("pnl"),
                "exit": r.get("exit_reason"),
            }
            for r in theory_only[:25]
        ],
        "live_only_sample": [
            {
                "symbol": r["symbol"],
                "side": r["_side"],
                "entry": str(r["_et"]),
                "status": r.get("status"),
                "pnl": r.get("pnl"),
                "exit": r.get("exit_reason"),
            }
            for r in live_only[:25]
        ],
    }

    outp = TEMP / "borex_week_analysis.json"
    outp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("week", "match", "closed_pnl_week", "slippage")}, indent=2))
    print(
        "LIVE",
        report["live_week"]["closed"],
        "closed",
        f"pnl={report['live_week']['pnl']:.2f}",
        f"wr={report['live_week']['wr']}",
    )
    print(
        "THEORY",
        report["theory_week"]["closed"],
        "closed",
        f"pnl={report['theory_week']['pnl']:.2f}",
        f"wr={report['theory_week']['wr']}",
    )
    print("by_day live", report["live_week"]["by_day"])
    print("by_day theory", report["theory_week"]["by_day"])
    print("wrote", outp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
