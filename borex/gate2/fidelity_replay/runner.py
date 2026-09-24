"""Orchestrate Gate 2 Level 5 exact replay fidelity (read-only)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from borex.gate2.fidelity_replay.compare import compare_all_trades
from borex.gate2.fidelity_replay.provenance import SOURCE_RUN_ID, load_source_bundle
from borex.gate2.fidelity_replay.report import (
    summarize,
    write_manifest,
    write_report_json,
    write_report_md,
    write_trade_replay,
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
    return f"g2r_{ts.strftime('%Y%m%d_%H%M%S')}"


def run_fidelity_replay(
    repo_root: Path | str,
    *,
    source_run_id: str = SOURCE_RUN_ID,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute Level 5 replay and write artifacts. Never connects to MT5."""
    root = Path(repo_root)
    bundle = load_source_bundle(root, source_run_id=source_run_id)
    trades = compare_all_trades(bundle)
    summary = summarize(trades)
    rid = run_id or make_run_id()
    out_dir = root / "data" / "runs" / "gate2_fidelity_replay" / "runs" / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    commit = _git_commit(root)

    write_manifest(
        out_dir / "manifest.json",
        run_id=rid,
        source_run_id=source_run_id,
        git_commit=commit,
        trades=trades,
        summary=summary,
        file_hashes=bundle.file_hashes,
        data_notes=bundle.data_notes,
        substitution_occurred=bundle.substitution_occurred,
    )
    write_trade_replay(out_dir / "trade_replay.json", trades, summary)
    write_report_json(
        out_dir / "fidelity_replay_report.json",
        run_id=rid,
        summary=summary,
        trades=trades,
    )
    write_report_md(
        out_dir / "fidelity_replay_report.md",
        run_id=rid,
        source_run_id=source_run_id,
        git_commit=commit,
        summary=summary,
        trades=trades,
        data_notes=bundle.data_notes,
        substitution_occurred=bundle.substitution_occurred,
    )

    return {
        "run_id": rid,
        "out_dir": str(out_dir),
        "source_run_id": source_run_id,
        "git_commit": commit,
        "summary": summary,
        "trades": trades,
    }
