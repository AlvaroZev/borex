"""Orchestrate Gate 2 E final fidelity assessment (read-only)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from borex.gate2.fidelity_assessment.analyze import build_assessment
from borex.gate2.fidelity_assessment.census import build_master_census, write_census_artifacts
from borex.gate2.fidelity_assessment.report import (
    write_assessment_json,
    write_assessment_md,
    write_manifest,
)


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def make_run_id(now: datetime | None = None) -> str:
    ts = now or datetime.now(timezone.utc)
    return f"g2a_{ts.strftime('%Y%m%d_%H%M%S')}"


def run_fidelity_assessment(
    repo_root: Path | str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Synthesize Gate 2 E assessment from existing artifacts. Never connects to MT5."""
    root = Path(repo_root)
    rid = run_id or make_run_id()
    out_dir = root / "data" / "runs" / "gate2_fidelity_assessment" / "runs" / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    commit = _git_commit(root)

    census = build_master_census(root)
    write_census_artifacts(out_dir, census)
    assessment = build_assessment(root, census=census)

    write_manifest(
        out_dir / "manifest.json",
        run_id=rid,
        git_commit=commit,
        census=census,
        assessment=assessment,
    )
    write_assessment_json(out_dir / "fidelity_assessment.json", run_id=rid, assessment=assessment)
    write_assessment_md(
        out_dir / "fidelity_assessment.md",
        run_id=rid,
        git_commit=commit,
        assessment=assessment,
    )

    return {
        "run_id": rid,
        "out_dir": str(out_dir),
        "git_commit": commit,
        "census": census,
        "assessment": assessment,
    }
