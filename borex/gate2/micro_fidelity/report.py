"""Fidelity reporting for Gate 2 micro experiment (N=1 early + N=20 full)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from borex.backtest.costs import infer_pip_size
from borex.gate2.exit_classify import (
    EXIT_BROKER_SL,
    EXIT_BROKER_TP,
    EXIT_CLEANUP_MARKET_CLOSE,
    EXIT_TIMEOUT,
    EXIT_UNKNOWN,
    classify_broker_exit,
)
from borex.gate2.micro_fidelity.theory import map_theory_to_execution

EXIT_STRATEGY_MARKET_EXIT = "STRATEGY_MARKET_EXIT"
EXIT_CLEANUP_MARKET_EXIT = "CLEANUP_MARKET_EXIT"


def normalize_exit_label(
    classification: str,
    *,
    intentional_strategy_exit: bool = False,
) -> str:
    if intentional_strategy_exit:
        return EXIT_STRATEGY_MARKET_EXIT
    if classification == EXIT_CLEANUP_MARKET_CLOSE:
        return EXIT_CLEANUP_MARKET_EXIT
    return classification


def classify_micro_exit(
    *,
    exit_deal: dict[str, Any] | None,
    intentional_cleanup: bool = False,
    intentional_strategy_exit: bool = False,
    timed_out: bool = False,
    position_still_open: bool = False,
    requested_sl: float | None = None,
    requested_tp: float | None = None,
) -> dict[str, Any]:
    raw = classify_broker_exit(
        exit_deal=exit_deal,
        intentional_cleanup_close=intentional_cleanup,
        intentional_probe_close=intentional_strategy_exit,
        timed_out=timed_out,
        position_still_open=position_still_open,
        requested_sl=requested_sl,
        requested_tp=requested_tp,
    )
    label = normalize_exit_label(
        raw.classification,
        intentional_strategy_exit=intentional_strategy_exit,
    )
    # When strategy exit flag set, classify_broker_exit returns PROBE_MARKET_CLOSE
    if intentional_strategy_exit:
        label = EXIT_STRATEGY_MARKET_EXIT
    d = raw.to_dict()
    d["classification"] = label
    return d


def entry_delta_pips(*, symbol: str, theory_entry: float, broker_entry: float) -> float:
    pip = infer_pip_size(symbol)
    if pip <= 0:
        return 0.0
    return (float(broker_entry) - float(theory_entry)) / pip


def early_diagnostic(row: dict[str, Any]) -> dict[str, Any]:
    """N=1 early diagnostic — never claims full system validation."""
    mapping = row.get("mapping_status")
    entry_dp = row.get("entry_delta_pips")
    exit_reason = row.get("broker_exit_reason")
    conf = row.get("confidence")

    verdict = "INSUFFICIENT_EVIDENCE"
    reasons: list[str] = []
    if mapping != "EXACT_MATCH":
        reasons.append(f"mapping={mapping}")
        verdict = "POTENTIAL_FIDELITY_PROBLEM" if mapping == "MAPPING_ERROR" else "INSUFFICIENT_EVIDENCE"
    elif conf != "HIGH":
        reasons.append(f"confidence={conf}")
        verdict = "INSUFFICIENT_EVIDENCE"
    elif entry_dp is None:
        reasons.append("missing_entry_delta")
        verdict = "INSUFFICIENT_EVIDENCE"
    elif abs(float(entry_dp)) > 2.0:
        reasons.append(f"entry_delta_pips={entry_dp}")
        verdict = "POTENTIAL_FIDELITY_PROBLEM"
    elif exit_reason not in (EXIT_BROKER_SL, EXIT_BROKER_TP, EXIT_STRATEGY_MARKET_EXIT, EXIT_CLEANUP_MARKET_EXIT, EXIT_TIMEOUT, EXIT_UNKNOWN):
        reasons.append(f"unexpected_exit={exit_reason}")
        verdict = "POTENTIAL_FIDELITY_PROBLEM"
    else:
        reasons.append("single_clean_observation_only")
        verdict = "CONSISTENT_WITH_EXPECTED_FIDELITY"

    return {
        "verdict": verdict,
        "n": 1,
        "note": "N=1 is an early diagnostic only; does not validate Gate 2.",
        "reasons": reasons,
        "observation": {
            "theory_event_id": row.get("theory_event_id"),
            "broker_position": row.get("broker_position"),
            "symbol": row.get("symbol"),
            "direction": row.get("direction"),
            "theory_signal_utc": row.get("theory_signal_utc"),
            "broker_entry_utc": row.get("broker_entry_utc"),
            "entry_time_delta_sec": row.get("entry_time_delta_sec"),
            "theory_entry": row.get("theory_entry"),
            "broker_entry": row.get("broker_entry"),
            "entry_delta_pips": entry_dp,
            "theory_sl": row.get("theory_sl"),
            "theory_tp": row.get("theory_tp"),
            "broker_sl": row.get("broker_sl"),
            "broker_tp": row.get("broker_tp"),
            "broker_exit_reason": exit_reason,
            "theory_expected_exit_model": row.get("theory_expected_exit_model"),
            "broker_exit": row.get("broker_exit"),
            "exit_delta_pips": row.get("exit_delta_pips"),
            "spread_pips": row.get("spread_pips"),
            "commission": row.get("commission"),
            "net_pnl": row.get("net_pnl"),
            "mapping_status": mapping,
            "confidence": conf,
        },
    }


def build_reconciliation_row(
    *,
    theory: dict[str, Any],
    execution: dict[str, Any],
    reconcile: dict[str, Any],
    exit_class: dict[str, Any],
    intentional_cleanup: bool = False,
) -> dict[str, Any]:
    mapping = map_theory_to_execution(
        theory_event_id=theory.get("theory_event_id"),
        execution_theory_event_id=execution.get("theory_event_id"),
    )
    symbol = str(theory.get("symbol") or "")
    theory_entry = float(theory.get("expected_entry") or 0)
    broker_entry = None
    broker_exit = None
    entry_deal = reconcile.get("entry_deal") or execution.get("entry_deal")
    exit_deal = reconcile.get("exit_deal")
    if entry_deal:
        broker_entry = float(entry_deal.get("price") or 0) or None
    if exit_deal:
        broker_exit = float(exit_deal.get("price") or 0) or None

    entry_dp = None
    exit_dp = None
    if broker_entry is not None and theory_entry:
        entry_dp = entry_delta_pips(symbol=symbol, theory_entry=theory_entry, broker_entry=broker_entry)

    theory_exit_ref = None
    br = exit_class.get("classification")
    if br == EXIT_BROKER_SL:
        theory_exit_ref = float(theory.get("expected_sl") or 0) or None
    elif br == EXIT_BROKER_TP:
        theory_exit_ref = float(theory.get("expected_tp") or 0) or None
    if broker_exit is not None and theory_exit_ref:
        exit_dp = entry_delta_pips(symbol=symbol, theory_entry=theory_exit_ref, broker_entry=broker_exit)

    entry_skew = None
    # timestamps as ISO strings when present
    t_sig = theory.get("signal_timestamp_utc")
    b_entry_t = (entry_deal or {}).get("time_utc") or (entry_deal or {}).get("time_utc_iso")
    b_exit_t = (exit_deal or {}).get("time_utc") or (exit_deal or {}).get("time_utc_iso")

    return {
        "theory_event_id": theory.get("theory_event_id"),
        "execution_id": execution.get("execution_id"),
        "broker_position": execution.get("position_ticket") or reconcile.get("position_ticket"),
        "symbol": symbol,
        "direction": theory.get("direction"),
        "theory_signal_utc": t_sig,
        "broker_entry_utc": b_entry_t,
        "broker_exit_utc": b_exit_t,
        "entry_time_delta_sec": entry_skew,
        "theory_entry": theory_entry,
        "broker_entry": broker_entry,
        "entry_delta_pips": entry_dp,
        "theory_sl": theory.get("expected_sl"),
        "theory_tp": theory.get("expected_tp"),
        "broker_sl": execution.get("actual_sl"),
        "broker_tp": execution.get("actual_tp"),
        "broker_exit": broker_exit,
        "exit_delta_pips": exit_dp,
        "broker_exit_reason": br,
        "theory_expected_exit_model": theory.get("expected_exit_model"),
        "spread_pips": theory.get("spread_pips"),
        "commission": reconcile.get("commission"),
        "swap": reconcile.get("swap"),
        "gross_pnl": reconcile.get("gross_pnl"),
        "net_pnl": reconcile.get("net_pnl"),
        "mapping_status": mapping["mapping_status"],
        "confidence": mapping["confidence"],
        "mapping_detail": mapping.get("detail"),
        "intentional_cleanup": intentional_cleanup,
        "reconcile_status": reconcile.get("status"),
        "exit_classification": exit_class,
    }


def summarize_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [
        r
        for r in rows
        if r.get("mapping_status") == "EXACT_MATCH"
        and r.get("reconcile_status") == "reconciled"
        and r.get("broker_exit_reason")
        not in (None, EXIT_UNKNOWN)
    ]
    return {
        "broker_trades": len(rows),
        "exact_mappings": sum(1 for r in rows if r.get("mapping_status") == "EXACT_MATCH"),
        "mapping_errors": sum(1 for r in rows if r.get("mapping_status") == "MAPPING_ERROR"),
        "unmapped": sum(1 for r in rows if r.get("mapping_status") == "UNMAPPED"),
        "clean_completed": len(clean),
        "broker_sl": sum(1 for r in clean if r.get("broker_exit_reason") == EXIT_BROKER_SL),
        "broker_tp": sum(1 for r in clean if r.get("broker_exit_reason") == EXIT_BROKER_TP),
        "cleanup_exits": sum(1 for r in rows if r.get("broker_exit_reason") == EXIT_CLEANUP_MARKET_EXIT),
        "timeouts": sum(1 for r in rows if r.get("broker_exit_reason") == EXIT_TIMEOUT),
    }


def write_fidelity_report(
    *,
    run_dir: Path,
    run_id: str,
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    early: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = summarize_metrics(rows)
    report = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "early_diagnostic": early,
        "gate2_bucket_E": "UNRESOLVED" if metrics["clean_completed"] < 20 else "EVIDENCE_READY_FOR_HUMAN",
        "human_decision_required": True,
        "note": "Do not auto PASS/FAIL Gate 2 from this report.",
        "manifest_ref": manifest.get("run_id"),
    }
    (run_dir / "fidelity_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    lines = [
        f"# Gate 2 Micro Fidelity — `{run_id}`",
        "",
        "**READ-ONLY reconciliation of Theory→broker observations.**",
        "",
        "## Metrics",
        "",
        f"- Broker trades: **{metrics['broker_trades']}**",
        f"- Exact mappings: **{metrics['exact_mappings']}**",
        f"- Mapping errors: **{metrics['mapping_errors']}**",
        f"- Unmapped: **{metrics['unmapped']}**",
        f"- Clean completed: **{metrics['clean_completed']}**",
        f"- Broker SL: **{metrics['broker_sl']}**",
        f"- Broker TP: **{metrics['broker_tp']}**",
        f"- Cleanup exits: **{metrics['cleanup_exits']}**",
        f"- Timeouts: **{metrics['timeouts']}**",
        "",
        "## Early diagnostic (N=1)",
        "",
    ]
    if early:
        lines += [
            f"- Verdict: **{early.get('verdict')}**",
            f"- Note: {early.get('note')}",
            "",
        ]
    else:
        lines += ["- Not yet available (no clean completed trade).", ""]
    lines += [
        "## Gate 2",
        "",
        f"- Bucket E status: **{report['gate2_bucket_E']}**",
        "- Human research decision required. No auto PASS/FAIL.",
        "",
    ]
    (run_dir / "fidelity_report.md").write_text("\n".join(lines), encoding="utf-8")
    (run_dir / "trade_reconciliation.json").write_text(
        json.dumps({"run_id": run_id, "rows": rows, "metrics": metrics}, indent=2, default=str),
        encoding="utf-8",
    )
    return report
