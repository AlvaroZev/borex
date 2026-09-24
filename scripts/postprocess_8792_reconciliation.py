"""Post-process reconciliation report with Theory vs offline Layer A diagnosis."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_8792_reconciliation import (  # noqa: E402
    largest_mismatches,
    match_events,
    normalize_book,
    normalize_offline,
    summarize_match,
)

OUT = ROOT / "data" / "runs" / "mirror_2d_8792" / "reconciliation"
BASE = OUT.parent


def main() -> int:
    dump = json.loads((BASE / "broker_theory_dump.json").read_text(encoding="utf-8"))
    sh = json.loads((OUT / "offline_shadow_h1.json").read_text(encoding="utf-8"))
    cl = json.loads((OUT / "offline_closest_h1_1m.json").read_text(encoding="utf-8"))
    agg = json.loads((OUT / "aggregate_report.json").read_text(encoding="utf-8"))

    te = normalize_book(dump["theory_closed"], "theory")
    se = normalize_offline(sh["trades"], "shadow")
    ce = normalize_offline(cl["trades"], "closest")

    extra = {}
    closest_match = None
    for name, a, b in (
        ("Theory_vs_Shadow_H1", te, se),
        ("Theory_vs_Closest_H1_1m", te, ce),
    ):
        m = match_events(a, b, ref_broker=False)
        s = summarize_match(name, m, ref_label="Theory", cand_label=name.split("_vs_")[1])
        extra[name] = s
        if "Closest" in name:
            closest_match = m

    agg["aggregate_matches"].update(extra)
    if closest_match is not None:
        agg["largest_mismatches_theory_closest"] = largest_mismatches(closest_match, n=10)

    agg["layers"]["A_signal"]["theory_vs_shadow_overlap"] = extra["Theory_vs_Shadow_H1"][
        "signal_overlap_pct"
    ]
    agg["layers"]["A_signal"]["theory_vs_closest_overlap"] = extra[
        "Theory_vs_Closest_H1_1m"
    ]["signal_overlap_pct"]
    agg["layers"]["A_signal"]["diagnosis"] = (
        "Live Theory/Mirror reproduce broker signals (~98-100%). "
        "Offline MultiMarketEngine with identical knobs only overlaps ~24% of Theory "
        "and ~27% of Broker. Primary fidelity break is Layer A (signal/state path), "
        "not 1m vs H1 exit modeling. Likely causes: batch MultiMarket cash/occupancy "
        "trajectory vs live ShadowEngine hour-feed with MT5 balance; ghost/warmup init; "
        "master clock. 1m path does not fix missing signals."
    )
    agg["layers"]["B_execution"]["note"] = (
        "On the sparse matched subset, 1m vs H1 MAE is nearly identical; "
        "class counts do not show clear 1m superiority. Exit-label mismatch vs broker "
        "is inflated by broker comment/exit labeling junk (Mirror PnL still ~$0.12)."
    )

    agg["fidelity_verdict"] = {
        "grade": "C",
        "label": "insufficiently faithful for meaningful strategy research",
        "scope": (
            "live-window broker/mirror reproduction with MultiMarket offline closest-sim"
        ),
        "next_action": "build a different replay architecture",
        "next_action_detail": (
            "Cheapest diagnostic with material confidence gain: replay the same H1 "
            "bars through live MirrorEngine/ShadowEngine bulk path (identical code as "
            "8792), then compare Theory↔bulk and Broker↔bulk. This separates "
            "MultiMarket-batch divergence from execution-model error. Do not tune "
            "spreads/RR/SL to fit this sample. Do not run another N1 grid until "
            "Layer A overlap is restored."
        ),
        "m1_improves_vs_h1": False,
        "ranking_note": (
            "Prior working assumption B (relative ranking inside MultiMarket) is not "
            "refuted by a ranking test here; this experiment shows the offline engine "
            "does not reproduce the live trade set, so live transfer of ranked configs "
            "is not yet justified."
        ),
        "notes": [
            "Broker↔Mirror reference intact (100% match, mean PnL gap ~$0.12)",
            "Closest overlap vs broker 27.3%; vs theory 23.6%",
            "1m does not materially beat H1 on matched-trade PnL error",
        ],
    }

    (OUT / "aggregate_report.json").write_text(
        json.dumps(agg, indent=2, default=str), encoding="utf-8"
    )

    lines = (OUT / "SUMMARY.txt").read_text(encoding="utf-8").rstrip().splitlines()
    lines += [
        "",
        "THEORY vs OFFLINE (Layer A diagnosis):",
        (
            f"  Theory vs Shadow overlap="
            f"{extra['Theory_vs_Shadow_H1']['signal_overlap_pct']}% "
            f"matched={extra['Theory_vs_Shadow_H1']['matched_trades']}"
        ),
        (
            f"  Theory vs Closest overlap="
            f"{extra['Theory_vs_Closest_H1_1m']['signal_overlap_pct']}% "
            f"matched={extra['Theory_vs_Closest_H1_1m']['matched_trades']}"
        ),
        "  => Offline MultiMarket is not replaying the live shadow tape.",
        "",
        "REFINED NEXT ACTION:",
        "  build a different replay architecture",
        "  (MirrorEngine/ShadowEngine bulk bar replay — same code as live 8792)",
    ]
    (OUT / "SUMMARY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("updated aggregate + summary")
    print("verdict", agg["fidelity_verdict"]["grade"], agg["fidelity_verdict"]["next_action"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
