"""Daily Theory ↔ Broker validation dataset from Gate 2 census artifacts.

Extends the g2a master_trade_census rather than inventing a parallel pipeline.
Read-only; never connects to MT5.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _day_key(trade: dict[str, Any]) -> str:
    for k in ("signal_time", "broker_entry_time", "sim_entry_time"):
        v = trade.get(k)
        if v:
            return str(v)[:10]
    return "UNKNOWN"


def trade_row_from_census(t: dict[str, Any]) -> dict[str, Any]:
    """Normalize one census trade into the daily validation schema."""
    mapping = str(t.get("mapping_status") or "")
    exit_agree = t.get("exit_event_agreement")
    if exit_agree is None and t.get("exit_event_class") == "agree":
        exit_agree = True
    excl = t.get("exclusion_reason")
    if t.get("excluded_from_sim_fidelity") and not excl:
        excl = "excluded_from_sim_fidelity"
    if t.get("pnl_excluded_from_fidelity") and not excl:
        classes = t.get("discrepancy_classes") or []
        excl = ",".join(str(x) for x in classes) if classes else "pnl_excluded_from_fidelity"
    replayable = (
        not t.get("excluded_from_sim_fidelity")
        and str(t.get("replay_status") or "")
        not in {"INSUFFICIENT_SOURCE_DATA", "OHLC_PATH_UNRESOLVABLE"}
        and mapping in {"EXACT_MATCH", "MATCH", "ALIGNED", ""}
    )
    naturally_executable = bool(
        t.get("broker_ticket") and t.get("theory_entry") is not None
    )
    entry_timing_ok = None
    dts = t.get("entry_time_delta_sec")
    if dts is not None:
        entry_timing_ok = abs(float(dts)) <= 60.0

    same_event = mapping in {"EXACT_MATCH", "MATCH", "ALIGNED"} or (
        bool(t.get("theory_event_id")) and bool(t.get("broker_ticket"))
    )

    return {
        "day": _day_key(t),
        "theory_event_id": t.get("theory_event_id"),
        "execution_id": t.get("execution_id"),
        "broker_ticket": t.get("broker_ticket"),
        "symbol": t.get("symbol"),
        "direction": t.get("direction"),
        "theory_entry": t.get("theory_entry"),
        "broker_entry": t.get("broker_entry"),
        "entry_delta_pips": t.get("entry_delta_pips"),
        "theory_sl": t.get("theory_sl"),
        "broker_sl": t.get("broker_sl"),
        "theory_tp": t.get("theory_tp"),
        "broker_tp": t.get("broker_tp"),
        "exit_timestamp_broker": t.get("broker_exit_time"),
        "exit_timestamp_sim": None,
        "exit_event_broker": t.get("broker_exit_reason"),
        "exit_event_sim": t.get("sim_exit_reason"),
        "theory_outcome": t.get("sim_exit_mapped_broker_label") or t.get("sim_exit_reason"),
        "broker_outcome": t.get("broker_exit_reason"),
        "theory_pnl": None,
        "broker_pnl": None,
        "same_event_agreement": same_event,
        "entry_timing_agreement": entry_timing_ok,
        "exit_event_agreement": exit_agree,
        "naturally_executable_replayable": bool(naturally_executable and replayable),
        "exclusion_reason": excl,
        "mapping_status": mapping,
        "discrepancy_class": t.get("discrepancy_class"),
        "discrepancy_classes": t.get("discrepancy_classes"),
        "source_run": t.get("source_run"),
        "replay_status": t.get("replay_status"),
        "path_insufficient_reason": t.get("path_insufficient_reason"),
        "evidence_weight": t.get("evidence_weight"),
        "experiment_class": t.get("experiment_class"),
    }


def aggregate_day(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    excl = [r for r in rows if r.get("exclusion_reason")]
    same_ev = [r for r in rows if r.get("same_event_agreement")]
    exit_ag = [r for r in rows if r.get("exit_event_agreement") is True]
    exit_cmp = [r for r in rows if r.get("exit_event_agreement") is not None]
    entry_d = [
        float(r["entry_delta_pips"])
        for r in rows
        if r.get("entry_delta_pips") is not None
    ]
    broker_exits = Counter(str(r.get("exit_event_broker") or "UNKNOWN") for r in rows)
    return {
        "day": rows[0]["day"] if rows else None,
        "intents": n,
        "fills": sum(1 for r in rows if r.get("broker_ticket")),
        "completed_trades": sum(1 for r in rows if r.get("exit_event_broker")),
        "win_rate": None,
        "profit_factor": None,
        "net_pnl": None,
        "mean_trade": None,
        "median_trade": None,
        "TP_count": broker_exits.get("BROKER_TP", 0) + broker_exits.get("take_profit", 0),
        "SL_or_margin_stop_count": (
            broker_exits.get("BROKER_SL", 0)
            + broker_exits.get("margin_stop", 0)
            + broker_exits.get("stop_loss", 0)
        ),
        "daily_close_count": broker_exits.get("daily_close", 0)
        + broker_exits.get("BROKER_DAILY_CLOSE", 0),
        "other_exit_count": sum(
            v
            for k, v in broker_exits.items()
            if k
            not in {
                "BROKER_TP",
                "take_profit",
                "BROKER_SL",
                "margin_stop",
                "stop_loss",
                "daily_close",
                "BROKER_DAILY_CLOSE",
                "UNKNOWN",
                "None",
            }
        ),
        "theory_broker_trade_agreement_rate": (len(same_ev) / n) if n else None,
        "theory_broker_exit_event_agreement_rate": (
            (len(exit_ag) / len(exit_cmp)) if exit_cmp else None
        ),
        "total_abs_entry_price_error_pips": float(sum(abs(x) for x in entry_d))
        if entry_d
        else None,
        "median_abs_entry_delta_pips": float(statistics.median([abs(x) for x in entry_d]))
        if entry_d
        else None,
        "excluded_count": len(excl),
        "exclusion_reasons": dict(Counter(str(r.get("exclusion_reason")) for r in excl)),
        "replayable_count": sum(
            1 for r in rows if r.get("naturally_executable_replayable")
        ),
        "exit_reason_counts": dict(broker_exits),
        "trade_count": n,
    }


def build_daily_dataset(census_path: Path) -> dict[str, Any]:
    census = json.loads(census_path.read_text(encoding="utf-8"))
    trades = census.get("trades") or []
    rows = [trade_row_from_census(t) for t in trades]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    daily = [aggregate_day(by_day[d]) for d in sorted(by_day)]
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_census": str(census_path),
        "trade_rows": rows,
        "daily_aggregates": daily,
        "limitations": [
            "Natural theory↔broker census concentrates on available Gate 2 "
            "micro-fidelity days (primarily 2026-09-24).",
            "USD theory/broker PnL is not on the fidelity census; exit-mix and "
            "price deltas are the primary agreement metrics.",
            "Controlled probe trades are excluded from this natural dataset.",
            "Every exclusion_reason is preserved; discrepancies are not silently dropped.",
        ],
    }


def write_daily_artifacts(dataset: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "daily_theory_broker.json").write_text(
        json.dumps(dataset, indent=2, default=str), encoding="utf-8"
    )
    rows = dataset["trade_rows"]
    if rows:
        keys = list(rows[0].keys())
        with (out_dir / "daily_theory_broker_trades.csv").open(
            "w", encoding="utf-8", newline=""
        ) as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                flat = dict(r)
                if isinstance(flat.get("discrepancy_classes"), list):
                    flat["discrepancy_classes"] = "|".join(
                        str(x) for x in flat["discrepancy_classes"]
                    )
                w.writerow(flat)
    daily = dataset["daily_aggregates"]
    if daily:
        flat_daily = []
        for d in daily:
            fd = {
                k: v
                for k, v in d.items()
                if k not in {"exclusion_reasons", "exit_reason_counts"}
            }
            fd["exclusion_reasons_json"] = json.dumps(d.get("exclusion_reasons") or {})
            fd["exit_reason_counts_json"] = json.dumps(d.get("exit_reason_counts") or {})
            flat_daily.append(fd)
        with (out_dir / "daily_theory_broker_by_day.csv").open(
            "w", encoding="utf-8", newline=""
        ) as f:
            w = csv.DictWriter(f, fieldnames=list(flat_daily[0].keys()))
            w.writeheader()
            w.writerows(flat_daily)
