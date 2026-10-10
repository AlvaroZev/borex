"""Config hashing, search-space builders, and SQLite result store for Gate 2 discovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from itertools import combinations, product
from pathlib import Path
from typing import Any, Iterable


def config_hash(cfg: dict[str, Any]) -> str:
    """Stable hash of a flat/nested JSON-serializable config."""
    blob = json.dumps(cfg, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class ResultsDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evaluations (
              config_hash TEXT PRIMARY KEY,
              strategy TEXT,
              parent_strategy TEXT,
              params_json TEXT,
              period TEXT,
              fills INTEGER,
              pf REAL,
              net REAL,
              mean_trade REAL,
              median_trade REAL,
              mdd REAL,
              wr REAL,
              tp INTEGER,
              daily_close INTEGER,
              margin_stop INTEGER,
              long_count INTEGER,
              short_count INTEGER,
              status TEXT,
              runtime_s REAL,
              stage TEXT,
              category TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly (
              config_hash TEXT,
              week TEXT,
              fills INTEGER,
              pf REAL,
              net REAL,
              mean_trade REAL,
              PRIMARY KEY (config_hash, week)
            )
            """
        )
        self.conn.commit()

    def has(self, h: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM evaluations WHERE config_hash=?", (h,)
        ).fetchone()
        return row is not None

    def put(self, row: dict[str, Any], weekly: list[dict] | None = None) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO evaluations (
              config_hash, strategy, parent_strategy, params_json, period,
              fills, pf, net, mean_trade, median_trade, mdd, wr,
              tp, daily_close, margin_stop, long_count, short_count,
              status, runtime_s, stage, category
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["config_hash"],
                row.get("strategy"),
                row.get("parent_strategy"),
                json.dumps(row.get("params") or {}, default=str),
                row.get("period"),
                row.get("fills"),
                row.get("pf"),
                row.get("net"),
                row.get("mean_trade"),
                row.get("median_trade"),
                row.get("mdd"),
                row.get("wr"),
                row.get("tp"),
                row.get("daily_close"),
                row.get("margin_stop"),
                row.get("long_count"),
                row.get("short_count"),
                row.get("status", "ok"),
                row.get("runtime_s"),
                row.get("stage"),
                row.get("category"),
            ),
        )
        if weekly:
            for w in weekly:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO weekly (config_hash, week, fills, pf, net, mean_trade)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        row["config_hash"],
                        w.get("week"),
                        w.get("fills"),
                        w.get("PF") or w.get("pf"),
                        w.get("net_pnl") or w.get("net"),
                        w.get("mean_trade"),
                    ),
                )
        self.conn.commit()

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0])

    def close(self) -> None:
        self.conn.close()


def one_factor_configs(base: dict, grids: dict[str, list]) -> list[dict]:
    out = [dict(base)]
    seen = {config_hash(base)}
    for k, vals in grids.items():
        for v in vals:
            cfg = dict(base)
            cfg[k] = v
            h = config_hash(cfg)
            if h not in seen:
                seen.add(h)
                out.append(cfg)
    return out


def pairwise_configs(base: dict, grids: dict[str, list], keys: list[str] | None = None) -> list[dict]:
    keys = keys or list(grids.keys())
    out = []
    seen = set()
    for a, b in combinations(keys, 2):
        for va, vb in product(grids[a], grids[b]):
            cfg = dict(base)
            cfg[a] = va
            cfg[b] = vb
            h = config_hash(cfg)
            if h not in seen:
                seen.add(h)
                out.append(cfg)
    return out


def cartesian_configs(base: dict, grids: dict[str, list]) -> list[dict]:
    keys = list(grids.keys())
    out = []
    seen = set()
    for vals in product(*[grids[k] for k in keys]):
        cfg = dict(base)
        for k, v in zip(keys, vals):
            cfg[k] = v
        h = config_hash(cfg)
        if h not in seen:
            seen.add(h)
            out.append(cfg)
    return out


def theoretical_cardinality(grids: dict[str, list]) -> int:
    n = 1
    for vals in grids.values():
        n *= max(1, len(vals))
    return n
