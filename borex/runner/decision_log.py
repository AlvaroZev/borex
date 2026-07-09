from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from borex.config import RESULTS_DB


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or RESULTS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            bar_ts TEXT,
            bar_index INTEGER,
            event_type TEXT NOT NULL,
            action TEXT,
            reason TEXT,
            detail TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_session ON decision_log(session_id, id DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            severity TEXT NOT NULL,
            code TEXT NOT NULL,
            message TEXT NOT NULL,
            detail TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_session ON live_alerts(session_id, id DESC)"
    )
    conn.commit()


def log_decision(
    session_id: str,
    *,
    event_type: str,
    bar_index: int | None = None,
    bar_ts: str | None = None,
    action: str = "",
    reason: str = "",
    detail: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> int:
    conn = _connect(db_path)
    cur = conn.execute(
        """
        INSERT INTO decision_log (
            session_id, created_at, bar_ts, bar_index, event_type, action, reason, detail
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            datetime.now(timezone.utc).isoformat(),
            bar_ts,
            bar_index,
            event_type,
            action,
            reason,
            json.dumps(detail or {}),
        ),
    )
    conn.commit()
    row_id = int(cur.lastrowid)
    conn.close()
    return row_id


def list_decisions(
    session_id: str,
    *,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        """
        SELECT id, session_id, created_at, bar_ts, bar_index, event_type, action, reason, detail
        FROM decision_log
        WHERE session_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        item = dict(r)
        item["detail"] = json.loads(item["detail"] or "{}")
        out.append(item)
    return out


def create_alert(
    session_id: str,
    *,
    severity: str,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> int:
    conn = _connect(db_path)
    cur = conn.execute(
        """
        INSERT INTO live_alerts (session_id, created_at, severity, code, message, detail)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            datetime.now(timezone.utc).isoformat(),
            severity,
            code,
            message,
            json.dumps(detail or {}),
        ),
    )
    conn.commit()
    row_id = int(cur.lastrowid)
    conn.close()

    try:
        from borex.runner.alert_delivery import dispatch_alert

        dispatch_alert(
            session_id,
            severity=severity,
            code=code,
            message=message,
            detail=detail,
            alert_id=row_id,
        )
    except Exception:
        pass

    return row_id


def list_alerts(
    session_id: str,
    *,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        """
        SELECT id, session_id, created_at, severity, code, message, detail
        FROM live_alerts
        WHERE session_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        item = dict(r)
        item["detail"] = json.loads(item["detail"] or "{}")
        out.append(item)
    return out
