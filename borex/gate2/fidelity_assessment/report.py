"""Writers for Gate 2 E fidelity assessment artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from borex.gate2.fidelity_assessment import constants as C


def write_manifest(
    path: Path,
    *,
    run_id: str,
    git_commit: str,
    census: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    payload = {
        "run_id": run_id,
        "experiment": "gate2_fidelity_assessment",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "read_only": True,
        "mt5_connection": False,
        "orders": False,
        "strategy_modified": False,
        "simulator_modified": False,
        "source_runs": {
            "natural_micro": list(C.NATURAL_MICRO_RUNS),
            "replay": dict(C.REPLAY_RUNS),
            "controlled": list(C.CONTROLLED_RUNS.keys()),
            "natural_sample_registry": C.NATURAL_SAMPLE_REGISTRY,
        },
        "exclusion": {
            "ticket": C.EXCLUDED_SIM_FIDELITY_TICKET,
            "source_run": C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN,
            "reason": C.EXCLUDED_SIM_FIDELITY_REASON,
        },
        "census": {
            "raw_natural_rows": census["raw_natural_rows"],
            "deduplicated_natural_trades": census["deduplicated_natural_trades"],
        },
        "gate2_bucket_E": assessment.get("gate2_bucket_E"),
        "human_decision_required": True,
        "automatic_pass_fail": None,
        "no_single_fidelity_score": True,
        "artifacts": [
            "manifest.json",
            "master_trade_census.json",
            "master_trade_census.csv",
            "fidelity_assessment.json",
            "fidelity_assessment.md",
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_assessment_json(path: Path, *, run_id: str, assessment: dict[str, Any]) -> None:
    payload = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        **assessment,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def render_markdown(
    *,
    run_id: str,
    git_commit: str,
    assessment: dict[str, Any],
) -> str:
    a = assessment
    sample = a["simulator_fidelity_sample"]
    theory = a["theory_reproduction"]
    entry = a["entry_fidelity"]
    ev = a["exit_event_fidelity"]
    ep = a["exit_price_fidelity"]
    et = a["exit_time_fidelity"]
    ohlc = a["ohlc_resolution_boundary"]
    leak = a["simulator_independence_leakage"]
    lines: list[str] = []

    lines.append(f"# Gate 2 E Fidelity Assessment — `{run_id}`")
    lines.append("")
    lines.append("## Executive conclusion")
    lines.append("")
    lines.append(a["research_conclusion"]["statement"])
    lines.append("")
    lines.append(f"- Git commit: `{git_commit}`")
    lines.append(f"- Gate 2 bucket E status: **{a['gate2_bucket_E']}** (human decision required)")
    lines.append("- No single fidelity score, ranking, or automatic PASS/FAIL is emitted.")
    lines.append("")

    lines.append("## 1. Evidence lineage")
    lines.append("")
    lines.append("```text")
    lines.append("controlled probes (g2p / g2e / g2f)")
    lines.append("      ↓")
    lines.append("natural Level 4 (g2m_20260924_001929)")
    lines.append("      ↓")
    lines.append("Level 5 replay (g2r_20260924_032600)")
    lines.append("      ↓")
    lines.append("Level 5b continuous M1 (g2m_20260924_043542 → g2r_20260924_055920)")
    lines.append("      ↓")
    lines.append("current assessment")
    lines.append("```")
    lines.append("")
    lines.append("Evidence weights differ: Level 5b continuous M1 is highest relevance for")
    lines.append("Simulator↔Broker exit fidelity; Level 4 stitched M1 is medium; controlled")
    lines.append("probes validate plumbing only.")
    lines.append("")

    lines.append("## 2. Natural trade census")
    lines.append("")
    cs = a["census_summary"]
    lines.append(f"- Raw natural rows loaded: **{cs['raw_natural_rows']}**")
    lines.append(f"- Deduplicated natural trades: **{cs['deduplicated_natural_trades']}**")
    lines.append(f"- Duplicates dropped: **{cs['duplicates_dropped']}**")
    lines.append(
        f"- Simulator-fidelity sample (Level 5b): population={sample['population_n']}, "
        f"replayable={sample['replayable_n']}, excluded={sample['excluded_n']}"
    )
    lines.append("")
    lines.append("See `master_trade_census.csv` for the full trade-level table.")
    lines.append("")

    lines.append("## 3. Theory reproduction")
    lines.append("")
    lines.append("Independent of broker execution.")
    lines.append("")
    for run_id_, stats in (theory.get("by_run") or {}).items():
        lines.append(
            f"- `{run_id_}`: exact={stats.get('exact_fraction')}; "
            f"minor_price={stats.get('minor_price_difference')}; "
            f"timestamp_diff={stats.get('timestamp_difference')}; "
            f"unreproducible={stats.get('unreproducible')}; "
            f"signal_match={stats.get('signal_match_count')}/{stats.get('n')}"
        )
    lines.append(f"- Level 5b verified signal match: **{theory.get('level5b_signal_match')}**")
    lines.append("")

    lines.append("## 4. Entry fidelity")
    lines.append("")
    lines.append(f"- Sample N (valid simulator denominator): **{entry['n']}**")
    lines.append(
        f"- Median Theory→broker entry Δ: **{entry['median_theory_to_broker_pips']}** pips"
    )
    lines.append(
        f"- Max |Δ|: **{entry['max_abs_theory_to_broker_pips']}** pips"
    )
    lines.append(f"- p25/p75: {entry.get('p25')} / {entry.get('p75')}")
    lines.append(f"- Spread at entry median: {entry['spread_at_entry_pips'].get('median')} pips")
    lines.append(f"- Entry time deltas: {entry['entry_time_delta_sec'].get('note')}")
    lines.append("")
    lines.append("Per symbol:")
    lines.append("")
    for sym, st in (entry.get("per_symbol") or {}).items():
        lines.append(
            f"- `{sym}`: n={st['n']}, median={st['median']}, max|Δ|={st['max_abs']}"
        )
    lines.append("")
    lines.append(entry["interpretation"])
    lines.append("")

    lines.append("## 5. Exit-event fidelity")
    lines.append("")
    lines.append(
        f"- Same event: **{ev['same_event']}**; different: **{ev['different_event']}**; "
        f"unresolved: **{ev['unresolved']}** ({ev['agreement_fraction']})"
    )
    lines.append("")
    lines.append("| # | Symbol | Dir | Broker | Simulator | Same event | Exit Δ pips |")
    lines.append("| - | ------ | --- | ------ | --------- | ---------- | ----------: |")
    for row in ev.get("trade_table") or []:
        sym = str(row.get("symbol") or "").replace("=X", "")
        same = row.get("same_event")
        same_s = "—" if same is None else ("YES" if same else "NO")
        dlt = row.get("exit_delta_pips")
        dlt_s = "" if dlt is None else f"{float(dlt):.4g}"
        lines.append(
            f"| {row.get('n')} | {sym} | {row.get('direction')} | "
            f"{row.get('broker_exit_reason')} | {row.get('sim_exit_reason')} | "
            f"{same_s} | {dlt_s} |"
        )
    lines.append("")
    if ev.get("non_excluded_disagreements"):
        lines.append("### Non-excluded disagreements")
        lines.append("")
        for d in ev["non_excluded_disagreements"]:
            lines.append(f"- ticket `{d.get('broker_ticket')}`: {d}")
    else:
        lines.append("No non-excluded exit-event disagreements in the valid simulator sample.")
    lines.append("")

    lines.append("## 6. Exit-price fidelity")
    lines.append("")
    lines.append(
        f"- Among same-event trades with Δ: n={ep['n_with_delta']}, "
        f"median={ep['median_delta_pips']}, abs_median={ep['abs_median_delta_pips']}, "
        f"max|Δ|={ep['max_abs_delta_pips']}, p25={ep.get('p25')}, p75={ep.get('p75')}"
    )
    lines.append(
        f"- Separation: {ep['separation']['simulator_level']} vs {ep['separation']['broker_fill']}"
    )
    if ep.get("largest_outliers"):
        lines.append("- Largest outliers:")
        for o in ep["largest_outliers"]:
            lines.append(
                f"  - ticket `{o.get('broker_ticket')}` `{o.get('symbol')}` "
                f"Δ={o.get('exit_delta_pips')} (sim={o.get('sim_exit')}, "
                f"broker={o.get('broker_exit')})"
            )
    lines.append("")

    lines.append("## 7. Exit-time fidelity")
    lines.append("")
    lines.append(
        f"- Same M1 candle: **{et['same_m1_candle']}**; "
        f"different M1 candle: **{et['different_m1_candle']}**; "
        f"not comparable: **{et['not_comparable']}**"
    )
    lines.append(f"- {et['note']}")
    lines.append("")

    lines.append("## 8. OHLC resolution boundary")
    lines.append("")
    lines.append(
        f"- Ticket **`{ohlc['ticket']}`** (AUDUSD long, source `{ohlc['source_run']}`) "
        f"classified as **`{ohlc['class']}`**."
    )
    lines.append(f"- {ohlc['note']}")
    if ohlc.get("detail"):
        d = ohlc["detail"]
        lines.append(
            f"- Path detail: bars_evaluated={d.get('bars_evaluated')}, "
            f"m1_path_len={d.get('m1_path_len')}, "
            f"path_reason={d.get('path_insufficient_reason')}, "
            f"broker={d.get('broker_exit_reason')}, sim={d.get('sim_exit_reason')}"
        )
    lines.append(
        "- Excluded from simulator-fidelity disagreement counts. Not a simulator failure; "
        "not a missing-data failure."
    )
    lines.append("")

    lines.append("## 9. Simulator independence / no leakage")
    lines.append("")
    lines.append(f"- Audit result: **{leak['result']}**")
    lines.append(f"- Code path: `{leak.get('code_path_documented')}`")
    for n in leak.get("notes") or []:
        lines.append(f"- {n}")
    lines.append(
        f"- Falsification note: {leak['broker_copy_falsification']['independence_evidence']}"
    )
    lines.append("")

    lines.append("## 10. Remaining uncertainties")
    lines.append("")
    for u in a.get("remaining_limitations") or []:
        lines.append(f"- {u}")
    lines.append("")

    lines.append("## 11. Gate 2 E research conclusion")
    lines.append("")
    lines.append("### Established")
    lines.append("")
    for e in a.get("established") or []:
        lines.append(f"- {e}")
    lines.append("")
    lines.append("### Remaining for human research judgment")
    lines.append("")
    for e in a.get("remaining_limitations") or []:
        lines.append(f"- {e}")
    lines.append("")
    lines.append(a["research_conclusion"]["statement"])
    lines.append("")
    lines.append("This report does **not** output PASS or FAIL.")
    lines.append("")

    lines.append("## Controlled experiments (separate)")
    lines.append("")
    ctrl = a.get("controlled_experiments") or {}
    lines.append(ctrl.get("note") or "")
    lines.append("")
    for rid, meta in (ctrl.get("runs") or {}).items():
        lines.append(f"### `{rid}`")
        lines.append("")
        for item in meta.get("established") or []:
            lines.append(f"- {item}")
        lines.append("- Included in natural sim denominator: **false**")
        lines.append("")

    return "\n".join(lines)


def write_assessment_md(path: Path, **kwargs: Any) -> None:
    path.write_text(render_markdown(**kwargs), encoding="utf-8")
