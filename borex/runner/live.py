from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

# No result for this long while "running" => stalled (not dead — may be a huge download).
STALL_THRESHOLD_SEC = 300
# Worker thread gone but status still running => crashed.
DEAD_THRESHOLD_SEC = 30


@dataclass
class RunEvent:
    type: str  # started | job_start | heartbeat | result | done | error | stalled
    run_id: str
    kind: str
    payload: dict = field(default_factory=dict)


@dataclass
class RunState:
    run_id: str
    kind: str
    status: str = "pending"  # pending | running | done | error | stalled
    total: int = 0
    completed: int = 0
    failed: int = 0
    started_at: str = ""
    finished_at: str = ""
    run_group: str | None = None
    recent: list[dict] = field(default_factory=list)
    error: str = ""
    current_job: dict | None = None
    last_activity_at: str = ""

    def to_dict(self) -> dict:
        now = datetime.now(timezone.utc)
        started = datetime.fromisoformat(self.started_at) if self.started_at else now
        last = (
            datetime.fromisoformat(self.last_activity_at)
            if self.last_activity_at
            else started
        )
        secs_idle = max(0.0, (now - last).total_seconds())
        elapsed = max(0.0, (now - started).total_seconds())
        worker_alive = runs.is_worker_alive(self.run_id)

        is_stalled = (
            self.status == "running"
            and worker_alive
            and secs_idle >= STALL_THRESHOLD_SEC
        )
        is_dead = (
            self.status in ("pending", "running")
            and not worker_alive
            and secs_idle >= DEAD_THRESHOLD_SEC
        )

        health = "ok"
        if is_dead:
            health = "dead"
        elif is_stalled:
            health = "stalled"
        elif self.status == "running" and self.current_job:
            health = "working"
        elif self.status == "running":
            health = "running"

        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "run_group": self.run_group,
            "recent": self.recent[-30:],
            "error": self.error,
            "pct": round(100 * self.completed / self.total, 1) if self.total else 0,
            "current_job": self.current_job,
            "last_activity_at": self.last_activity_at,
            "seconds_since_activity": round(secs_idle, 1),
            "elapsed_seconds": round(elapsed, 1),
            "worker_alive": worker_alive,
            "health": health,
            "is_stalled": is_stalled,
            "is_dead": is_dead,
        }


class RunManager:
    """In-memory run tracker for live SSE updates."""

    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._events: dict[str, list[RunEvent]] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, total: int, run_group: str | None = None) -> str:
        run_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        state = RunState(
            run_id=run_id,
            kind=kind,
            status="pending",
            total=total,
            started_at=now,
            last_activity_at=now,
            run_group=run_group,
        )
        with self._lock:
            self._runs[run_id] = state
            self._events[run_id] = []
        return run_id

    def register_worker(self, run_id: str, thread: threading.Thread) -> None:
        with self._lock:
            self._workers[run_id] = thread

    def is_worker_alive(self, run_id: str) -> bool:
        with self._lock:
            thread = self._workers.get(run_id)
        if thread is None:
            return True
        return thread.is_alive()

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def get_enriched(self, run_id: str) -> dict | None:
        state = self.get(run_id)
        if not state:
            return None
        data = state.to_dict()
        if data.get("is_dead") and state.status == "running":
            self.mark_error(run_id, "Background worker stopped unexpectedly")
            state = self.get(run_id)
            if state:
                data = state.to_dict()
        return data

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            items = sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
        return [r.to_dict() for r in items[:limit]]

    def _push(self, run_id: str, event: RunEvent) -> None:
        with self._lock:
            if run_id in self._events:
                self._events[run_id].append(event)

    def drain_events(self, run_id: str, after: int = 0) -> tuple[list[RunEvent], int]:
        with self._lock:
            events = self._events.get(run_id, [])
            new = events[after:]
            return new, len(events)

    def _touch_locked(self, state: RunState) -> None:
        state.last_activity_at = datetime.now(timezone.utc).isoformat()

    def touch(self, run_id: str, *, message: str = "") -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if not state:
                return
            self._touch_locked(state)
            kind = state.kind
            job = state.current_job
        payload: dict = {"progress": self.get_enriched(run_id) or {}}
        if message:
            payload["message"] = message
        if job:
            payload["current_job"] = job
        self._push(run_id, RunEvent("heartbeat", run_id, kind, payload))

    def set_current_job(self, run_id: str, symbol: str, timeframe: str, year: int | None = None) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if not state:
                return
            state.current_job = {
                "symbol": symbol,
                "timeframe": timeframe,
                "year": year,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            self._touch_locked(state)
            kind = state.kind
            job = dict(state.current_job)
        self._push(
            run_id,
            RunEvent(
                "job_start",
                run_id,
                kind,
                {"current_job": job, "progress": self.get_enriched(run_id) or {}},
            ),
        )

    def clear_current_job(self, run_id: str) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state:
                state.current_job = None

    def mark_running(self, run_id: str) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state:
                state.status = "running"
                self._touch_locked(state)
                kind = state.kind
            else:
                kind = ""
        self._push(
            run_id,
            RunEvent("started", run_id, kind, {"progress": self.get_enriched(run_id) or {}}),
        )

    def on_result(self, run_id: str, result: dict, *, ok: bool) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if not state:
                return
            state.completed += 1
            if not ok:
                state.failed += 1
            state.current_job = None
            state.recent.append(result)
            if len(state.recent) > 50:
                state.recent = state.recent[-50:]
            self._touch_locked(state)
            kind = state.kind
        self._push(
            run_id,
            RunEvent(
                "result",
                run_id,
                kind if state else "",
                {"ok": ok, "result": result, "progress": self.get_enriched(run_id) or {}},
            ),
        )

    def mark_done(self, run_id: str) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state:
                state.status = "done"
                state.current_job = None
                state.finished_at = datetime.now(timezone.utc).isoformat()
                self._touch_locked(state)
                kind = state.kind
            else:
                kind = ""
        self._push(
            run_id,
            RunEvent("done", run_id, kind, {"progress": self.get_enriched(run_id) or {}}),
        )

    def mark_error(self, run_id: str, message: str) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state:
                if state.status != "error":
                    state.status = "error"
                    state.error = message
                    state.current_job = None
                    state.finished_at = datetime.now(timezone.utc).isoformat()
                    self._touch_locked(state)
                kind = state.kind
            else:
                kind = ""
        self._push(
            run_id,
            RunEvent("error", run_id, kind, {"message": message, "progress": self.get_enriched(run_id) or {}}),
        )


runs = RunManager()


def progress_callback(run_id: str) -> Callable[[dict, bool], None]:
    def cb(result: dict, ok: bool) -> None:
        runs.on_result(run_id, result, ok=ok)

    return cb


def dukascopy_callbacks(run_id: str) -> tuple[Callable[[str, str, int | None], None], ProgressCallback, Callable[[], None]]:
    def on_job_start(symbol: str, timeframe: str, year: int | None = None) -> None:
        runs.set_current_job(run_id, symbol, timeframe, year=year)

    def on_progress(result: dict, ok: bool) -> None:
        runs.on_result(run_id, result, ok=ok)

    def on_activity() -> None:
        runs.touch(run_id)

    return on_job_start, on_progress, on_activity
