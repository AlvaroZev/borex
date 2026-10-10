"""SQLite experiment registry — resumable, cache-aware."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class ExperimentRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=60)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS experiments (
              experiment_id TEXT PRIMARY KEY,
              parent_id TEXT,
              group_id TEXT,
              strategy TEXT,
              family TEXT,
              config_hash TEXT UNIQUE,
              params_json TEXT,
              data_range TEXT,
              execution_model TEXT,
              status TEXT,
              worker_id TEXT,
              start_time REAL,
              end_time REAL,
              runtime_s REAL,
              result_json TEXT,
              error TEXT,
              category TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_exp_status ON experiments(status);
            CREATE INDEX IF NOT EXISTS idx_exp_group ON experiments(group_id);
            CREATE TABLE IF NOT EXISTS hardware_jobs (
              worker_id TEXT,
              pid INTEGER,
              started REAL,
              notes TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT
            );
            """
        )
        self.conn.commit()

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
            (key, json.dumps(value, default=str)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def register(self, exp: dict[str, Any]) -> bool:
        """Insert pending experiment if config_hash unknown. Returns True if new."""
        try:
            self.conn.execute(
                """
                INSERT INTO experiments (
                  experiment_id, parent_id, group_id, strategy, family, config_hash,
                  params_json, data_range, execution_model, status, category
                ) VALUES (?,?,?,?,?,?,?,?,?,'pending',?)
                """,
                (
                    exp["experiment_id"],
                    exp.get("parent_id"),
                    exp.get("group_id"),
                    exp.get("strategy"),
                    exp.get("family"),
                    exp["config_hash"],
                    json.dumps(exp.get("params") or {}, default=str),
                    exp.get("data_range", "2026-06..2026-08"),
                    exp.get("execution_model", "n1_frozen"),
                    exp.get("category", "DISCOVERY_ONLY"),
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def is_done(self, config_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM experiments WHERE config_hash=?", (config_hash,)
        ).fetchone()
        return bool(row and row[0] == "done")

    def mark_running(self, config_hash: str, worker_id: str) -> None:
        self.conn.execute(
            """
            UPDATE experiments SET status='running', worker_id=?, start_time=?
            WHERE config_hash=? AND status IN ('pending','failed')
            """,
            (worker_id, time.time(), config_hash),
        )
        self.conn.commit()

    def mark_done(self, config_hash: str, result: dict, runtime_s: float) -> None:
        self.conn.execute(
            """
            UPDATE experiments SET status='done', end_time=?, runtime_s=?, result_json=?, error=NULL
            WHERE config_hash=?
            """,
            (time.time(), runtime_s, json.dumps(result, default=str), config_hash),
        )
        self.conn.commit()

    def mark_failed(self, config_hash: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE experiments SET status='failed', end_time=?, error=?
            WHERE config_hash=?
            """,
            (time.time(), error[:2000], config_hash),
        )
        self.conn.commit()

    def pending(self, group_id: str | None = None) -> list[dict]:
        if group_id:
            rows = self.conn.execute(
                "SELECT experiment_id, config_hash, params_json, group_id, strategy, family FROM experiments WHERE status IN ('pending','failed') AND group_id=?",
                (group_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT experiment_id, config_hash, params_json, group_id, strategy, family FROM experiments WHERE status IN ('pending','failed')"
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "experiment_id": r[0],
                    "config_hash": r[1],
                    "params": json.loads(r[2]),
                    "group_id": r[3],
                    "strategy": r[4],
                    "family": r[5],
                }
            )
        return out

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM experiments GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def done_results(self, group_id: str | None = None) -> list[dict]:
        if group_id:
            rows = self.conn.execute(
                "SELECT experiment_id, strategy, family, group_id, params_json, result_json, category FROM experiments WHERE status='done' AND group_id=?",
                (group_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT experiment_id, strategy, family, group_id, params_json, result_json, category FROM experiments WHERE status='done'"
            ).fetchall()
        out = []
        for r in rows:
            res = json.loads(r[5]) if r[5] else {}
            out.append(
                {
                    "experiment_id": r[0],
                    "strategy": r[1],
                    "family": r[2],
                    "group_id": r[3],
                    "params": json.loads(r[4]),
                    **res,
                    "category": r[6],
                }
            )
        return out

    def close(self) -> None:
        self.conn.close()
