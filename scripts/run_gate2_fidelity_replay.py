#!/usr/bin/env python3
"""Gate 2 Level 5 exact replay fidelity — READ-ONLY.

Consumes g2m_20260924_001929 artifacts only. Never connects to MT5.
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
    parser.add_argument(
        "--source-run",
        default="g2m_20260924_001929",
        help="Source micro-fidelity run id",
    )
    parser.add_argument("--run-id", default=None, help="Optional deterministic run id")
    args = parser.parse_args(argv)

    repo_root = args.repo_root
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[1]

    # Import after path resolution for script execution
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from borex.gate2.fidelity_replay.runner import run_fidelity_replay

    result = run_fidelity_replay(
        repo_root,
        source_run_id=args.source_run,
        run_id=args.run_id,
    )
    summary = result["summary"]
    print(json.dumps(
        {
            "run_id": result["run_id"],
            "out_dir": result["out_dir"],
            "source_run_id": result["source_run_id"],
            "git_commit": result["git_commit"],
            "trades_total": summary["trades_total"],
            "trades_replayed": summary["trades_replayed"],
            "trades_insufficient_source_data": summary["trades_insufficient_source_data"],
            "theory_reproduction_match": summary["theory_reproduction_match"],
            "gate2_bucket_E": "UNRESOLVED",
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
