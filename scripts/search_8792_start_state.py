"""Exhaustive search for 8792 START-of-window Theory state + related artifacts.

Searches Railway/local DB tables and known on-disk run dirs.
Does NOT invent state. Does NOT tune replay.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
load_dotenv(LIVE / ".env")

OUT = (
    ROOT
    / "data"
    / "runs"
    / "mirror_2d_8792"
    / "reconciliation"
    / "golden_fixture"
    / "recovery_search"
)
TARGET = "2026-08-27 01:00:00+00:00"
WINDOW_A = "2026-08-26"
WINDOW_B = "2026-08-29"


def urls() -> list[tuple[str, str]]:
    pairs = []
    for label, key in (
        ("local", "MIRROR_DATABASE_URL"),
        ("local2", "DATABASE_URL"),
        ("backup", "MIRROR_DATABASE_BACKUP_URL"),
        ("backup2", "DATABASE_BACKUP_URL"),
        ("railway", "RAILWAY_DATABASE_URL"),
    ):
        u = os.environ.get(key) or ""
        if u and not any(u == x for _, x in pairs):
            pairs.append((label, u))
    return pairs


def probe_db(label: str, url: str) -> dict:
    host = url.split("@")[-1]
    out: dict = {"label": label, "host": host, "ok": False}
    try:
        eng = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 20})
        with eng.connect() as c:
            out["ok"] = True
            insp = inspect(eng)
            tables = sorted(insp.get_table_names())
            out["tables"] = tables

            # Any table with state/checkpoint/blob/snapshot in name
            interesting = [
                t
                for t in tables
                if any(
                    k in t.lower()
                    for k in (
                        "state",
                        "check",
                        "blob",
                        "snap",
                        "theory",
                        "mirror",
                        "event",
                        "log",
                        "audit",
                        "ghost",
                        "candle",
                        "run",
                        "backup",
                    )
                )
            ]
            out["interesting_tables"] = interesting

            findings = []

            # mirror_theory_state — only id=1 typically; check columns + history
            if "mirror_theory_state" in tables:
                cols = [c["name"] for c in insp.get_columns("mirror_theory_state")]
                row = c.execute(
                    text(
                        "select * from mirror_theory_state where id=1"
                    )
                ).mappings().first()
                # Don't dump blob
                if row:
                    d = {k: row[k] for k in row.keys() if k != "state_blob"}
                    if "state_blob" in row.keys():
                        d["state_blob_len"] = len(bytes(row["state_blob"] or b""))
                    findings.append(
                        {
                            "source": "mirror_theory_state",
                            "columns": cols,
                            "row": {k: str(v) if hasattr(v, "isoformat") else v for k, v in d.items()},
                            "is_before_window_start": str(d.get("last_master_ts") or "")
                            < TARGET,
                            "last_master_ts": d.get("last_master_ts"),
                        }
                    )

            if "theory_state" in tables:
                cols = [c["name"] for c in insp.get_columns("theory_state")]
                row = c.execute(text("select * from theory_state where id=1")).mappings().first()
                if row:
                    d = {k: row[k] for k in row.keys() if k != "state_blob"}
                    if "state_blob" in row.keys():
                        d["state_blob_len"] = len(bytes(row["state_blob"] or b""))
                    findings.append(
                        {
                            "source": "theory_state",
                            "columns": cols,
                            "row": {k: str(v) if hasattr(v, "isoformat") else v for k, v in d.items()},
                            "note": "mt5service book — not mirror 8792",
                            "is_before_window_start": str(d.get("last_master_ts") or "")
                            < TARGET,
                        }
                    )

            # History tables?
            for t in tables:
                cols = [c["name"] for c in insp.get_columns(t)]
                lower = {c.lower() for c in cols}
                if "state_blob" in lower or (
                    "config_hash" in lower and "last_master_ts" in lower
                ):
                    n = c.execute(text(f'select count(*) from "{t}"')).scalar()
                    findings.append(
                        {
                            "source": t,
                            "kind": "possible_state_table",
                            "columns": cols,
                            "n_rows": n,
                        }
                    )
                    # If multiple rows with timestamps, find nearest before TARGET
                    ts_col = None
                    for cand in (
                        "last_master_ts",
                        "updated_at",
                        "created_at",
                        "ts",
                        "timestamp",
                    ):
                        if cand in lower:
                            ts_col = next(c for c in cols if c.lower() == cand)
                            break
                    if ts_col and n and n > 0:
                        try:
                            # nearest before
                            q = text(
                                f'select * from "{t}" where cast("{ts_col}" as text) < :tgt '
                                f'order by cast("{ts_col}" as text) desc limit 3'
                            )
                            rows = c.execute(q, {"tgt": TARGET}).mappings().all()
                            slim = []
                            for r in rows:
                                slim.append(
                                    {
                                        k: (
                                            f"<blob {len(bytes(v))}b>"
                                            if k.lower() == "state_blob"
                                            else (str(v) if hasattr(v, "isoformat") else v)
                                        )
                                        for k, v in dict(r).items()
                                    }
                                )
                            findings.append(
                                {
                                    "source": t,
                                    "kind": "rows_before_target",
                                    "ts_col": ts_col,
                                    "rows": slim,
                                }
                            )
                        except Exception as e:
                            findings.append(
                                {"source": t, "kind": "before_query_fail", "error": str(e)[:200]}
                            )
                            c.rollback()

            # mirror_events / service_events around window
            for t in ("mirror_events", "service_events"):
                if t not in tables:
                    continue
                cols = [c["name"] for c in insp.get_columns(t)]
                # sample messages mentioning checkpoint/reset/epoch
                msg_col = next(
                    (c for c in cols if c.lower() in ("message", "msg", "detail", "payload")),
                    None,
                )
                ts_col = next(
                    (
                        c
                        for c in cols
                        if c.lower() in ("created_at", "ts", "timestamp", "at")
                    ),
                    None,
                )
                if msg_col and ts_col:
                    try:
                        rows = c.execute(
                            text(
                                f'select * from "{t}" where cast("{ts_col}" as text) >= :a '
                                f'and cast("{ts_col}" as text) < :b '
                                f"order by \"{ts_col}\" limit 50"
                            ),
                            {"a": WINDOW_A, "b": WINDOW_B},
                        ).mappings().all()
                        findings.append(
                            {
                                "source": t,
                                "kind": "window_events_sample",
                                "n": len(rows),
                                "sample": [
                                    {
                                        k: str(v)[:200]
                                        for k, v in dict(r).items()
                                        if k.lower() != "state_blob"
                                    }
                                    for r in rows[:15]
                                ],
                            }
                        )
                        # keyword search broader
                        kw = c.execute(
                            text(
                                f'select count(*) from "{t}" where '
                                f"lower(cast(\"{msg_col}\" as text)) like '%checkpoint%' "
                                f"or lower(cast(\"{msg_col}\" as text)) like '%epoch%' "
                                f"or lower(cast(\"{msg_col}\" as text)) like '%reset%'"
                            )
                        ).scalar()
                        findings.append(
                            {"source": t, "kind": "keyword_counts_checkpoint_epoch_reset", "n": kw}
                        )
                    except Exception as e:
                        findings.append({"source": t, "error": str(e)[:200]})
                        c.rollback()

            # pending_ghosts
            if "pending_ghosts" in tables:
                n = c.execute(text("select count(*) from pending_ghosts")).scalar()
                cols = [c["name"] for c in insp.get_columns("pending_ghosts")]
                findings.append(
                    {"source": "pending_ghosts", "n": n, "columns": cols}
                )

            # live_candles nearest coverage
            if "live_candles" in tables:
                cov = c.execute(
                    text(
                        "select interval, min(ts), max(ts), count(*) "
                        "from live_candles group by interval order by 1"
                    )
                ).fetchall()
                findings.append(
                    {
                        "source": "live_candles_coverage",
                        "rows": [
                            {
                                "interval": r[0],
                                "min_ts": str(r[1]),
                                "max_ts": str(r[2]),
                                "n": r[3],
                            }
                            for r in cov
                        ],
                    }
                )
                n_h1 = c.execute(
                    text(
                        "select count(*) from live_candles "
                        "where interval='1h' and ts>=:a and ts<:b"
                    ),
                    {"a": "2026-08-27", "b": "2026-08-29"},
                ).scalar()
                findings.append({"source": "live_candles_h1_8792_window", "n": n_h1})

            # earliest mirror theory trade
            if "mirror_theory_trades" in tables:
                bounds = c.execute(
                    text(
                        "select min(entry_time), max(entry_time), count(*) "
                        "from mirror_theory_trades"
                    )
                ).fetchone()
                findings.append(
                    {
                        "source": "mirror_theory_trades_bounds",
                        "min_entry": bounds[0],
                        "max_entry": bounds[1],
                        "n": bounds[2],
                    }
                )
                before = c.execute(
                    text(
                        "select count(*) from mirror_theory_trades "
                        "where entry_time < :tgt"
                    ),
                    {"tgt": TARGET},
                ).scalar()
                findings.append(
                    {
                        "source": "mirror_theory_trades_before_window",
                        "n": before,
                        "note": "trades before window imply continuous run, but not a checkpoint",
                    }
                )

            out["findings"] = findings
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:400]}"
    return out


def search_disk() -> dict:
    hits = []
    roots = [
        ROOT / "data",
        LIVE,
        Path(r"c:\Users\azeva\OneDrive\Documentos\work\trading\borex-main\data\runs"),
    ]
    patterns = (
        "*theory_state*",
        "*checkpoint*",
        "*fixture*",
        "*state_blob*",
        "*.pkl",
        "*parity*",
        "*mirror*state*",
    )
    for root in roots:
        if not root.exists():
            continue
        for pat in patterns:
            try:
                for p in root.rglob(pat):
                    if p.is_file() and p.stat().st_size > 0:
                        hits.append(
                            {
                                "path": str(p),
                                "size": p.stat().st_size,
                                "mtime": datetime.fromtimestamp(
                                    p.stat().st_mtime, tz=timezone.utc
                                ).isoformat(),
                            }
                        )
            except Exception:
                continue
    # dedupe
    seen = set()
    uniq = []
    for h in sorted(hits, key=lambda x: x["path"]):
        if h["path"] in seen:
            continue
        seen.add(h["path"])
        uniq.append(h)
    return {"n": len(uniq), "hits": uniq[:80]}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_start": TARGET,
        "question": "Does any artifact encode Theory state immediately before window start?",
        "databases": [],
        "disk": search_disk(),
    }
    for label, url in urls():
        print(f"probing {label}…", flush=True)
        report["databases"].append(probe_db(label, url))

    # Verdict draft
    start_found = False
    earlier_ckpt = []
    for db in report["databases"]:
        for f in db.get("findings") or []:
            if f.get("is_before_window_start") is True:
                start_found = True
                earlier_ckpt.append({"db": db["label"], "finding": f})
            if f.get("kind") == "rows_before_target" and f.get("rows"):
                earlier_ckpt.append({"db": db["label"], "finding": f})

    report["start_checkpoint_found"] = start_found
    report["earlier_checkpoint_candidates"] = earlier_ckpt
    report["verdict"] = (
        "FOUND_CANDIDATE"
        if start_found or earlier_ckpt
        else "NO_START_CHECKPOINT_IN_SEARCHED_SOURCES"
    )

    path = OUT / "start_state_search.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("verdict", "start_checkpoint_found")}, indent=2))
    print("earlier_candidates", len(earlier_ckpt))
    print("disk_hits", report["disk"]["n"])
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
