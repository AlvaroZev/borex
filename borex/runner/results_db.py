from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            split TEXT NOT NULL,
            params TEXT NOT NULL,
            metrics TEXT NOT NULL,
            run_group TEXT,
            dataset_hash TEXT
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(backtest_runs)")}
    if "dataset_hash" not in cols:
        conn.execute("ALTER TABLE backtest_runs ADD COLUMN dataset_hash TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_group ON backtest_runs(run_group)"
    )
    conn.commit()


def save_result(
    result: dict,
    *,
    run_group: str | None = None,
    db_path: Path | None = None,
) -> int:
    conn = _connect(db_path)
    cur = conn.execute(
        """
        INSERT INTO backtest_runs (
            created_at, strategy, symbol, timeframe, split, params, metrics, run_group, dataset_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            result["strategy"],
            result["symbol"],
            result["timeframe"],
            result.get("split", "full"),
            json.dumps(result.get("params", {})),
            json.dumps(result.get("metrics", {})),
            run_group,
            result.get("dataset_hash"),
        ),
    )
    conn.commit()
    row_id = int(cur.lastrowid)
    conn.close()
    return row_id


def list_results(
    *,
    run_group: str | None = None,
    limit: int = 500,
    db_path: Path | None = None,
) -> list[dict]:
    conn = _connect(db_path)
    if run_group:
        rows = conn.execute(
            """
            SELECT * FROM backtest_runs WHERE run_group = ?
            ORDER BY id DESC LIMIT ?
            """,
            (run_group, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "strategy": r["strategy"],
                "symbol": r["symbol"],
                "timeframe": r["timeframe"],
                "split": r["split"],
                "params": json.loads(r["params"]),
                "metrics": json.loads(r["metrics"]),
                "run_group": r["run_group"],
                "dataset_hash": r["dataset_hash"],
            }
        )
    return out


def leaderboard(
    *,
    metric: str = "sharpe",
    run_group: str | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict]:
    rows = list_results(run_group=run_group, limit=10_000, db_path=db_path)
    rows.sort(key=lambda r: r["metrics"].get(metric, 0), reverse=True)
    return rows[:limit]
