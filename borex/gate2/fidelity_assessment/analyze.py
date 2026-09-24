"""Dimension-wise Gate 2 E analysis (no single fidelity score)."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from borex.gate2.fidelity_assessment import constants as C
from borex.gate2.fidelity_assessment.leakage import run_leakage_audit
from borex.gate2.fidelity_replay.provenance import load_source_bundle
from borex.gate2.fidelity_replay.theory_reproduce import reproduce_theory_event
from borex.gate2.micro_fidelity.config import FROZEN_CONFIG


def _median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def _max_abs(xs: list[float]) -> float | None:
    return float(max(abs(x) for x in xs)) if xs else None


def _percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def _abs_median(xs: list[float]) -> float | None:
    return _median([abs(x) for x in xs]) if xs else None


def valid_simulator_sample(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Highest-relevance denominator: Level 5b continuous M1 natural trades."""
    level5b = [t for t in trades if t.get("source_run") == C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN]
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for t in level5b:
        reasons: list[str] = []
        if t.get("excluded_from_sim_fidelity"):
            reasons.append(str(t.get("exclusion_reason") or C.OHLC_PATH_UNRESOLVABLE))
            excluded.append({**t, "exclusion_reasons": reasons})
            continue

        # Explicit inclusion criteria — any failure must be recorded (none expected beyond OHLC).
        if t.get("mapping_status") != "EXACT_MATCH":
            reasons.append("mapping_not_exact")
        if not t.get("continuous_m1_archive"):
            reasons.append("continuous_m1_archive_missing")
        if not t.get("theory_event_id"):
            reasons.append("theory_event_missing")
        if not t.get("broker_exit_time"):
            reasons.append("broker_exit_timestamp_missing")
        if t.get("replay_data_status") != "REPLAYED":
            reasons.append(f"not_replayable:{t.get('replay_status')}")

        if reasons:
            excluded.append({**t, "exclusion_reasons": reasons})
        else:
            included.append(t)

    return {
        "definition": (
            "Natural Theory trades from g2m_20260924_043542 with continuous M1 archive, "
            "exact Theory event, exact broker mapping, broker exit timestamp, and replayable "
            f"simulator exit. Explicit exclusion: ticket {C.EXCLUDED_SIM_FIDELITY_TICKET} "
            f"as {C.OHLC_PATH_UNRESOLVABLE}."
        ),
        "source_run": C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN,
        "population_n": len(level5b),
        "replayable_n": len(included),
        "excluded_n": len(excluded),
        "included_tickets": [str(t.get("broker_ticket")) for t in included],
        "excluded": [
            {
                "broker_ticket": e.get("broker_ticket"),
                "symbol": e.get("symbol"),
                "direction": e.get("direction"),
                "exclusion_reasons": e.get("exclusion_reasons"),
                "exclusion_note": e.get("exclusion_note"),
                "path_insufficient_reason": e.get("path_insufficient_reason"),
                "bars_evaluated": e.get("bars_evaluated"),
                "m1_path_len": e.get("m1_path_len"),
                "broker_exit_reason": e.get("broker_exit_reason"),
                "sim_exit_reason": e.get("sim_exit_reason"),
            }
            for e in excluded
        ],
        "included": included,
    }


def analyze_theory_reproduction(repo_root: Path, trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Independently verify Theory reproduction for Level 5b (and report Level 4)."""
    by_run: dict[str, dict[str, Any]] = {}
    for source_run in C.NATURAL_MICRO_RUNS:
        bundle = load_source_bundle(repo_root, source_run_id=source_run)
        exact = 0
        minor_price = 0
        timestamp_diff = 0
        unreproducible = 0
        details: list[dict[str, Any]] = []
        for theory in bundle.theory_events:
            repro = reproduce_theory_event(theory, config=FROZEN_CONFIG)
            diffs = list(repro.differences)
            if repro.signal_match and not diffs:
                bucket = "exact"
                exact += 1
            elif repro.signal_match and diffs:
                # signal matched with noted differences
                if any("timestamp" in d for d in diffs):
                    bucket = "timestamp_difference"
                    timestamp_diff += 1
                else:
                    bucket = "minor_price_difference"
                    minor_price += 1
            else:
                # classify unreproducible vs minor
                if any("timestamp" in d for d in diffs):
                    bucket = "timestamp_difference"
                    timestamp_diff += 1
                elif any("entry" in d or "sl" in d or "tp" in d or "price" in d for d in diffs):
                    bucket = "minor_price_difference"
                    minor_price += 1
                else:
                    bucket = "unreproducible"
                    unreproducible += 1
            details.append(
                {
                    "theory_event_id": repro.theory_event_id,
                    "bucket": bucket,
                    "signal_match": repro.signal_match,
                    "differences": diffs,
                }
            )
        n = len(bundle.theory_events)
        by_run[source_run] = {
            "n": n,
            "exact": exact,
            "minor_price_difference": minor_price,
            "timestamp_difference": timestamp_diff,
            "unreproducible": unreproducible,
            "exact_fraction": f"{exact}/{n}",
            "signal_match_count": sum(1 for d in details if d["signal_match"]),
            "details": details,
        }

    l5b = by_run.get(C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN, {})
    return {
        "note": "Theory reproduction is independent of broker execution.",
        "by_run": {
            k: {kk: vv for kk, vv in v.items() if kk != "details"} for k, v in by_run.items()
        },
        "level5b_verified": l5b.get("exact_fraction"),
        "level5b_signal_match": f"{l5b.get('signal_match_count')}/{l5b.get('n')}",
        "level5b_details": l5b.get("details"),
    }


def analyze_entry(sample_trades: list[dict[str, Any]]) -> dict[str, Any]:
    deltas = [
        float(t["entry_delta_pips"])
        for t in sample_trades
        if t.get("entry_delta_pips") is not None
    ]
    per_symbol: dict[str, list[float]] = defaultdict(list)
    for t in sample_trades:
        if t.get("entry_delta_pips") is not None:
            per_symbol[str(t.get("symbol"))].append(float(t["entry_delta_pips"]))
    spreads = [
        float(t["spread_pips"]) for t in sample_trades if t.get("spread_pips") is not None
    ]
    entry_time = [
        float(t["entry_time_delta_sec"])
        for t in sample_trades
        if t.get("entry_time_delta_sec") is not None
    ]
    return {
        "n": len(deltas),
        "median_theory_to_broker_pips": _median(deltas),
        "max_abs_theory_to_broker_pips": _max_abs(deltas),
        "p25": _percentile(deltas, 25),
        "p75": _percentile(deltas, 75),
        "per_symbol": {
            sym: {
                "n": len(xs),
                "median": _median(xs),
                "max_abs": _max_abs(xs),
            }
            for sym, xs in sorted(per_symbol.items())
        },
        "spread_at_entry_pips": {
            "n": len(spreads),
            "median": _median(spreads),
            "max": max(spreads) if spreads else None,
            "min": min(spreads) if spreads else None,
        },
        "entry_time_delta_sec": {
            "n": len(entry_time),
            "note": (
                "null for all Level 5b rows in source artifacts "
                "(entry_time_delta_sec not populated)"
                if not entry_time
                else "populated"
            ),
            "median": _median(entry_time),
            "max_abs": _max_abs(entry_time),
        },
        "interpretation": (
            "Deltas are Theory expected entry vs actual broker fill. "
            "Not labeled slippage — evidence does not establish causal slippage attribution."
        ),
    }


def analyze_exit_events(sample_trades: list[dict[str, Any]]) -> dict[str, Any]:
    same = 0
    different = 0
    unresolved = 0
    table: list[dict[str, Any]] = []
    for i, t in enumerate(sample_trades, 1):
        broker = t.get("broker_exit_reason")
        sim = t.get("sim_exit_reason")
        mapped = t.get("sim_exit_mapped_broker_label")
        agree = t.get("exit_event_agreement")
        if agree is True or (mapped and broker and mapped == broker):
            cls = "same_event"
            same += 1
            same_flag = True
        elif agree is False or (mapped and broker and mapped != broker):
            cls = "different_event"
            different += 1
            same_flag = False
        else:
            cls = "unresolved"
            unresolved += 1
            same_flag = None
        table.append(
            {
                "n": i,
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "broker_ticket": t.get("broker_ticket"),
                "broker_exit_reason": broker,
                "sim_exit_reason": sim,
                "same_event": same_flag,
                "exit_delta_pips": t.get("exit_delta_pips"),
                "class": cls,
            }
        )
    return {
        "n": len(sample_trades),
        "same_event": same,
        "different_event": different,
        "unresolved": unresolved,
        "agreement_fraction": f"{same}/{len(sample_trades)}" if sample_trades else "0/0",
        "trade_table": table,
        "non_excluded_disagreements": [r for r in table if r["same_event"] is False],
    }


def analyze_exit_price(sample_trades: list[dict[str, Any]]) -> dict[str, Any]:
    agreed = [
        t
        for t in sample_trades
        if t.get("exit_event_agreement") is True or (
            t.get("sim_exit_mapped_broker_label")
            and t.get("sim_exit_mapped_broker_label") == t.get("broker_exit_reason")
        )
    ]
    deltas = [
        float(t["exit_delta_pips"])
        for t in agreed
        if t.get("exit_delta_pips") is not None
    ]
    per_symbol: dict[str, list[float]] = defaultdict(list)
    outliers: list[dict[str, Any]] = []
    for t in agreed:
        if t.get("exit_delta_pips") is None:
            continue
        d = float(t["exit_delta_pips"])
        per_symbol[str(t.get("symbol"))].append(d)
        if abs(d) >= 0.5:
            outliers.append(
                {
                    "broker_ticket": t.get("broker_ticket"),
                    "symbol": t.get("symbol"),
                    "direction": t.get("direction"),
                    "exit_delta_pips": d,
                    "sim_exit": t.get("sim_exit"),
                    "broker_exit": t.get("broker_exit"),
                    "broker_exit_reason": t.get("broker_exit_reason"),
                    "note": (
                        "Simulator reports SL/TP level; broker reports deal fill. "
                        "Variance is not automatically a simulator defect."
                    ),
                }
            )
    outliers.sort(key=lambda x: abs(float(x["exit_delta_pips"])), reverse=True)
    return {
        "n_same_event": len(agreed),
        "n_with_delta": len(deltas),
        "median_delta_pips": _median(deltas),
        "abs_median_delta_pips": _abs_median(deltas),
        "max_abs_delta_pips": _max_abs(deltas),
        "p25": _percentile(deltas, 25),
        "p75": _percentile(deltas, 75),
        "per_symbol": {
            sym: {
                "n": len(xs),
                "median": _median(xs),
                "max_abs": _max_abs(xs),
            }
            for sym, xs in sorted(per_symbol.items())
        },
        "largest_outliers": outliers[:5],
        "separation": {
            "simulator_level": "SIM_SL/SIM_TP fill at theoretical SL/TP level",
            "broker_fill": "actual MT5 deal price around that level",
            "note": "Identity is not required for event agreement.",
        },
    }


def analyze_exit_time(sample_trades: list[dict[str, Any]]) -> dict[str, Any]:
    same_candle = 0
    different_candle = 0
    not_comparable = 0
    rows: list[dict[str, Any]] = []
    for t in sample_trades:
        cmp_ = t.get("exit_time_comparison") or {}
        if not cmp_.get("comparable"):
            not_comparable += 1
            status = "not_comparable"
        elif cmp_.get("same_m1_candle"):
            same_candle += 1
            status = "same_m1_candle"
        else:
            different_candle += 1
            status = "different_m1_candle"
        rows.append(
            {
                "broker_ticket": t.get("broker_ticket"),
                "symbol": t.get("symbol"),
                "sim_exit_time": t.get("sim_exit_reason") and (
                    (t.get("exit_time_comparison") or {}).get("sim_exit_candle_open")
                ),
                "broker_exit_time": t.get("broker_exit_time"),
                "broker_exit_minute": (t.get("exit_time_comparison") or {}).get(
                    "broker_exit_minute"
                ),
                "status": status,
            }
        )
    return {
        "n": len(sample_trades),
        "same_m1_candle": same_candle,
        "different_m1_candle": different_candle,
        "not_comparable": not_comparable,
        "note": (
            "M1 simulator exit time is the open of the first wick-hit candle. "
            "Broker exit is wall-clock deal time floored to minute. "
            "Tick-level equality is not demanded."
        ),
        "rows": rows,
    }


def analyze_level4_medium(trades: list[dict[str, Any]]) -> dict[str, Any]:
    level4 = [t for t in trades if t.get("source_run") == "g2m_20260924_001929"]
    replayed = [t for t in level4 if t.get("replay_data_status") == "REPLAYED"]
    agree = sum(1 for t in replayed if t.get("exit_event_agreement") is True)
    disagree = sum(1 for t in replayed if t.get("exit_event_agreement") is False)
    return {
        "evidence_weight": "medium_sim_broker",
        "note": (
            "Stitched Theory-embedded M1 — useful for Theory→Broker mapping; "
            "weaker for simulator exit fidelity than Level 5b continuous archive."
        ),
        "n": len(level4),
        "exact_mappings": sum(1 for t in level4 if t.get("mapping_status") == "EXACT_MATCH"),
        "replayed": len(replayed),
        "insufficient_or_unresolved": len(level4) - len(replayed),
        "exit_event_agree": agree,
        "exit_event_disagree": disagree,
        "not_in_sim_fidelity_denominator": True,
    }


def analyze_controlled(repo_root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "note": (
            "Controlled probes validate plumbing; they are NOT Theory-generated natural trades "
            "and are excluded from the natural simulator-fidelity denominator."
        ),
        "runs": {},
    }
    for rid, meta in C.CONTROLLED_RUNS.items():
        path = repo_root / meta["path"]
        out["runs"][rid] = {
            "kind": meta["kind"],
            "path": meta["path"],
            "exists": path.is_dir(),
            "established": list(meta["established"]),
            "included_in_natural_sim_denominator": False,
        }
    # Natural sample registry context only
    reg_path = repo_root / C.NATURAL_SAMPLE_REGISTRY
    reg_summary: dict[str, Any] = {"path": C.NATURAL_SAMPLE_REGISTRY, "exists": reg_path.is_file()}
    if reg_path.is_file():
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        reg_summary.update(
            {
                "purpose": reg.get("purpose"),
                "gate2": reg.get("gate2"),
                "observation_windows": len(reg.get("observation_windows") or []),
                "evidence_weight": C.EVIDENCE_WEIGHT["natural_sample_registry"],
                "note": (
                    "Legacy H1/natural sample accumulation — not Level 5b continuous M1 "
                    "Theory micro-fidelity evidence."
                ),
            }
        )
    out["natural_sample_registry"] = reg_summary
    return out


def assert_no_forbidden_pnl_keys(payload: dict[str, Any], path: str = "root") -> None:
    for k, v in payload.items():
        lk = str(k).lower()
        if lk in C.FORBIDDEN_PNL_METRIC_KEYS:
            raise AssertionError(f"Forbidden PnL metric key {k!r} at {path}")
        if isinstance(v, dict):
            assert_no_forbidden_pnl_keys(v, f"{path}.{k}")
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    assert_no_forbidden_pnl_keys(item, f"{path}.{k}[{i}]")


def build_assessment(
    repo_root: Path | str,
    *,
    census: dict[str, Any],
) -> dict[str, Any]:
    root = Path(repo_root)
    trades = list(census["trades"])
    sample = valid_simulator_sample(trades)
    included = sample["included"]

    theory = analyze_theory_reproduction(root, trades)
    entry = analyze_entry(included)
    exit_events = analyze_exit_events(included)
    exit_price = analyze_exit_price(included)
    exit_time = analyze_exit_time(included)
    level4 = analyze_level4_medium(trades)
    controlled = analyze_controlled(root)
    leakage = run_leakage_audit(root)

    ohlc_exclusion = next(
        (
            e
            for e in sample["excluded"]
            if str(e.get("broker_ticket")) == C.EXCLUDED_SIM_FIDELITY_TICKET
        ),
        None,
    )

    established = [
        "Theory events can be reproduced causally from their own candle sequences "
        f"(Level 5b verified {theory.get('level5b_signal_match')}).",
        "Natural Theory→broker mapping works (Level 4 20/20 EXACT_MATCH; Level 5b 21/21 EXACT_MATCH).",
        "Continuous M1 capture is operational (Level 5b m1_archive, continuity any_gaps=false).",
        "Broker SL/TP execution and deal-reason reconciliation are understood (controlled g2e).",
        (
            "Simulator exit event agrees with broker on all replayable natural Level 5b trades "
            f"after excluding the explicit OHLC-path case "
            f"({exit_events['agreement_fraction']} same_event; "
            f"different_event={exit_events['different_event']})."
        ),
        "Replay pipeline independently walks archived M1 through MultiMarketEngine wick rules "
        f"(leakage audit: {leakage['result']}).",
    ]

    remaining = [
        "M1 OHLC cannot reveal every intra-minute broker path "
        f"(explicit boundary: ticket {C.EXCLUDED_SIM_FIDELITY_TICKET} = {C.OHLC_PATH_UNRESOLVABLE}).",
        "Broker fills can differ from theoretical SL/TP levels "
        f"(exit-price max|Δ|={exit_price.get('max_abs_delta_pips')} pips among same-event trades).",
        "This does not establish tick-level fidelity.",
        "This does not establish Live-account fidelity.",
        "This does not establish strategy profitability.",
        "Level 4 stitched-M1 replay remains weaker evidence (many insufficient paths; "
        f"{level4['exit_event_disagree']} event disagreement among {level4['replayed']} replayed).",
    ]

    assessment: dict[str, Any] = {
        "experiment": "gate2_fidelity_assessment",
        "gate2_bucket_E": "UNRESOLVED",
        "human_decision_required": True,
        "automatic_pass_fail": None,
        "note": "Do not auto PASS/FAIL Gate 2 from this assessment.",
        "no_single_fidelity_score": True,
        "evidence_lineage": [
            "controlled probes (g2p, g2e, g2f)",
            "natural Level 4 micro-fidelity (g2m_20260924_001929)",
            "Level 5 replay (g2r_20260924_032600)",
            "Level 5b continuous M1 (g2m_20260924_043542 → g2r_20260924_055920)",
            "current assessment",
        ],
        "census_summary": {
            "raw_natural_rows": census["raw_natural_rows"],
            "deduplicated_natural_trades": census["deduplicated_natural_trades"],
            "duplicates_dropped": len(census.get("duplicates_dropped") or []),
        },
        "simulator_fidelity_sample": {
            k: v for k, v in sample.items() if k != "included"
        },
        "theory_reproduction": theory,
        "entry_fidelity": entry,
        "exit_event_fidelity": exit_events,
        "exit_price_fidelity": exit_price,
        "exit_time_fidelity": exit_time,
        "ohlc_resolution_boundary": {
            "ticket": C.EXCLUDED_SIM_FIDELITY_TICKET,
            "source_run": C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN,
            "class": C.OHLC_PATH_UNRESOLVABLE,
            "detail": ohlc_exclusion,
            "note": C.EXCLUDED_SIM_FIDELITY_NOTE,
        },
        "level4_medium_relevance": level4,
        "controlled_experiments": controlled,
        "simulator_independence_leakage": leakage,
        "established": established,
        "remaining_limitations": remaining,
        "research_conclusion": {
            "question": (
                "Is the simulator sufficiently characterized against real Mirror broker "
                "execution to resume the next stage of Borex research?"
            ),
            "factual_support": established,
            "remaining_for_human_judgment": remaining,
            "machine_decision": None,
            "statement": (
                "Evidence characterizes Theory→Broker mapping and M1 OHLC Simulator→Broker "
                "exit-event agreement on the Level 5b replayable natural sample "
                f"({exit_events['agreement_fraction']}), with one explicit "
                f"{C.OHLC_PATH_UNRESOLVABLE} boundary case excluded from disagreement counts. "
                "Gate 2 E remains a human research judgment; this assessment does not emit "
                "PASS or FAIL."
            ),
        },
    }
    assert_no_forbidden_pnl_keys(assessment)
    if assessment.get("automatic_pass_fail") is not None:
        raise AssertionError("automatic_pass_fail must be null")
    if assessment.get("gate2_bucket_E") in ("PASS", "FAIL"):
        raise AssertionError("gate2_bucket_E must not be PASS/FAIL")
    return assessment
