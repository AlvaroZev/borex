from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from borex.config import RESULTS_DB, BacktestConfig, IterationConfig
from borex.data.audit import audit_all
from borex.runner.screen import ScreenConfig, ScreenSummary, run_screen


@dataclass
class PipelineConfig:
    """End-to-end testing pipeline: audit → screen → paper tick → revalidate → retire."""

    run_audit: bool = True
    strict_audit: bool = True
    run_screen: bool = True
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    tick_paper: bool = False
    run_revalidate: bool = True
    kill_on_decay: bool = True
    iteration: IterationConfig = field(default_factory=IterationConfig)
    send_digest: bool = True
    save: bool = True


@dataclass
class PipelineSummary:
    run_id: str
    status: str  # ok | partial | failed
    audit: dict[str, Any] | None = None
    screen: dict[str, Any] | None = None
    paper_ticks: list[dict[str, Any]] = field(default_factory=list)
    revalidations: list[dict[str, Any]] = field(default_factory=list)
    retired: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "audit": self.audit,
            "screen": self.screen,
            "paper_ticks": self.paper_ticks,
            "revalidations": self.revalidations,
            "retired": self.retired,
            "errors": self.errors,
            "stopped_at": self.stopped_at,
        }


def _save_pipeline_run(summary: PipelineSummary) -> None:
    path = RESULTS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            report TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO pipeline_runs (id, created_at, status, report)
        VALUES (?, ?, ?, ?)
        """,
        (
            summary.run_id,
            datetime.now(timezone.utc).isoformat(),
            summary.status,
            json.dumps(summary.to_dict()),
        ),
    )
    conn.commit()
    conn.close()


def list_pipeline_runs(limit: int = 20) -> list[dict]:
    if not RESULTS_DB.is_file():
        return []
    conn = sqlite3.connect(RESULTS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, created_at, status FROM pipeline_runs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pipeline_run(run_id: str) -> dict | None:
    if not RESULTS_DB.is_file():
        return None
    conn = sqlite3.connect(RESULTS_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, created_at, status, report FROM pipeline_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    out["report"] = json.loads(out["report"])
    return out


def run_audit_step(*, strict: bool = True) -> tuple[dict[str, Any], list[str]]:
    reports = audit_all()
    ok = sum(1 for r in reports if r.status == "ok")
    warn = sum(1 for r in reports if r.status == "warn")
    fail = sum(1 for r in reports if r.status == "fail")
    summary = {
        "total": len(reports),
        "ok": ok,
        "warn": warn,
        "fail": fail,
        "passed": fail == 0 or not strict,
    }
    errors: list[str] = []
    if strict and fail:
        errors.append(f"Audit failed: {fail} dataset(s) with status fail")
    return summary, errors


def tick_active_sessions() -> list[dict[str, Any]]:
    from borex.runner.paper import list_active_sessions, paper_tick

    results: list[dict[str, Any]] = []
    for row in list_active_sessions(limit=200):
        sid = row["id"]
        try:
            out = paper_tick(sid, refresh_data=True)
            results.append({"session_id": sid, "ok": True, **out})
        except Exception as exc:
            results.append({"session_id": sid, "ok": False, "error": str(exc)})
    return results


def revalidate_active_sessions(
    *,
    iteration: IterationConfig | None = None,
    kill_on_decay: bool = False,
    save: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    from borex.runner.paper import kill_session, list_active_sessions, load_session
    from borex.runner.revalidate import run_revalidation

    icfg = iteration or IterationConfig()
    revalidations: list[dict[str, Any]] = []
    retired: list[dict[str, str]] = []

    for row in list_active_sessions(limit=200):
        session = load_session(row["id"])
        if not session:
            continue
        try:
            report = run_revalidation(
                session.strategy,
                session.symbol,
                session.timeframe,
                params=session.params,
                config=session.config,
                iteration=icfg,
                paper_session_id=session.id,
                save=save,
            )
            entry = {
                "session_id": session.id,
                "strategy": session.strategy,
                "symbol": session.symbol,
                "timeframe": session.timeframe,
                "verdict": report.verdict,
                "reasons": report.reasons[:3],
            }
            revalidations.append(entry)

            if kill_on_decay and report.verdict == "decayed":
                kill_session(session.id, reason="decayed")
                retired.append(
                    {
                        "session_id": session.id,
                        "strategy": session.strategy,
                        "symbol": session.symbol,
                        "reason": "decayed",
                    }
                )
        except Exception as exc:
            revalidations.append(
                {
                    "session_id": row["id"],
                    "strategy": row.get("strategy", ""),
                    "symbol": row.get("symbol", ""),
                    "verdict": "error",
                    "error": str(exc),
                }
            )

    return revalidations, retired


def _send_digest(summary: PipelineSummary) -> None:
    try:
        from borex.runner.alert_delivery import dispatch_pipeline_digest

        dispatch_pipeline_digest(summary.to_dict())
    except Exception:
        pass


def run_pipeline(cfg: PipelineConfig | None = None) -> PipelineSummary:
    """One-shot: audit → screen → optional tick → revalidate → optional retire → digest."""
    pcfg = cfg or PipelineConfig()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_pipeline_") + uuid.uuid4().hex[:8]
    summary = PipelineSummary(run_id=run_id, status="ok")

    if pcfg.run_audit:
        audit_summary, audit_errors = run_audit_step(strict=pcfg.strict_audit)
        summary.audit = audit_summary
        if audit_errors:
            summary.errors.extend(audit_errors)
            summary.status = "failed"
            summary.stopped_at = "audit"
            if pcfg.save:
                _save_pipeline_run(summary)
            if pcfg.send_digest:
                _send_digest(summary)
            return summary

    screen_result: ScreenSummary | None = None
    if pcfg.run_screen:
        try:
            screen_result = run_screen(pcfg.screen)
            summary.screen = screen_result.to_dict()
        except Exception as exc:
            summary.errors.append(f"Screen failed: {exc}")
            summary.status = "failed"
            summary.stopped_at = "screen"
            if pcfg.save:
                _save_pipeline_run(summary)
            if pcfg.send_digest:
                _send_digest(summary)
            return summary

    if pcfg.tick_paper:
        summary.paper_ticks = tick_active_sessions()

    if pcfg.run_revalidate:
        revals, retired = revalidate_active_sessions(
            iteration=pcfg.iteration,
            kill_on_decay=pcfg.kill_on_decay,
            save=True,
        )
        summary.revalidations = revals
        summary.retired = retired

    if summary.errors:
        summary.status = "partial" if summary.screen or summary.revalidations else "failed"
    elif summary.paper_ticks and any(not t.get("ok") for t in summary.paper_ticks):
        summary.status = "partial"
    elif summary.revalidations and any(r.get("verdict") == "decayed" for r in summary.revalidations):
        summary.status = "partial"

    if pcfg.save:
        _save_pipeline_run(summary)
    if pcfg.send_digest:
        _send_digest(summary)
    return summary


def run_pipeline_tick(
    *,
    revalidate: bool = False,
    kill_on_decay: bool = False,
    iteration: IterationConfig | None = None,
    send_digest: bool = False,
    save: bool = True,
) -> PipelineSummary:
    """Tick all active paper sessions; optionally revalidate and retire decayed."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_tick_") + uuid.uuid4().hex[:8]
    summary = PipelineSummary(run_id=run_id, status="ok")
    summary.paper_ticks = tick_active_sessions()

    if revalidate:
        revals, retired = revalidate_active_sessions(
            iteration=iteration,
            kill_on_decay=kill_on_decay,
            save=save,
        )
        summary.revalidations = revals
        summary.retired = retired

    if summary.paper_ticks and any(not t.get("ok") for t in summary.paper_ticks):
        summary.status = "partial"
    if summary.retired:
        summary.status = "partial"

    if save:
        _save_pipeline_run(summary)
    if send_digest:
        _send_digest(summary)
    return summary


def run_pipeline_watch(
    *,
    poll_seconds: float = 300.0,
    revalidate_every: int = 0,
    kill_on_decay: bool = True,
    iteration: IterationConfig | None = None,
    max_cycles: int | None = None,
) -> None:
    """Daemon: tick active sessions on an interval; periodic revalidate."""
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        revalidate = revalidate_every > 0 and cycles > 0 and cycles % revalidate_every == 0
        summary = run_pipeline_tick(
            revalidate=revalidate,
            kill_on_decay=kill_on_decay,
            iteration=iteration,
            send_digest=revalidate or False,
            save=True,
        )
        cycles += 1
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        n = len(summary.paper_ticks)
        ok = sum(1 for t in summary.paper_ticks if t.get("ok"))
        print(f"[watch] {ts} cycle={cycles} ticks={ok}/{n} revalidate={revalidate}", flush=True)
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(poll_seconds)
