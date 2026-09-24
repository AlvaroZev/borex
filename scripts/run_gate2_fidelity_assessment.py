#!/usr/bin/env python3
"""Gate 2 E final fidelity assessment — READ-ONLY.

Consumes existing Gate 2 artifacts only. Never connects to MT5.
Does not modify strategy or simulator.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: parent of scripts/)",
    )
    parser.add_argument("--run-id", default=None, help="Optional deterministic run id")
    args = parser.parse_args(argv)

    repo_root = args.repo_root
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[1]

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from borex.gate2.fidelity_assessment.runner import run_fidelity_assessment

    result = run_fidelity_assessment(repo_root, run_id=args.run_id)
    a = result["assessment"]
    sample = a["simulator_fidelity_sample"]
    ev = a["exit_event_fidelity"]
    ep = a["exit_price_fidelity"]
    et = a["exit_time_fidelity"]
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "out_dir": result["out_dir"],
                "git_commit": result["git_commit"],
                "deduplicated_natural_trades": a["census_summary"]["deduplicated_natural_trades"],
                "sim_sample_replayable": sample["replayable_n"],
                "sim_sample_excluded": sample["excluded_n"],
                "theory_level5b": a["theory_reproduction"].get("level5b_signal_match"),
                "exit_event_agreement": ev["agreement_fraction"],
                "exit_price_median": ep.get("median_delta_pips"),
                "exit_time_same_m1": et.get("same_m1_candle"),
                "leakage": a["simulator_independence_leakage"]["result"],
                "gate2_bucket_E": a["gate2_bucket_E"],
                "automatic_pass_fail": a["automatic_pass_fail"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
