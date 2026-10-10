#!/usr/bin/env python3
"""Export Live pending DB (dashboard) + optional /api/debug/pending memory dump."""

from __future__ import annotations

import csv
import json
import sys
import urllib.request
from pathlib import Path

RUN = Path(sys.argv[1])
RUN.mkdir(parents=True, exist_ok=True)
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8791"


def get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.loads(r.read().decode())


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    dash = get("/api/dashboard")
    ghosts = list(dash.get("pending_ghosts") or [])
    rows = []
    for g in ghosts:
        rows.append(
            {
                "symbol": g.get("symbol"),
                "action": g.get("action"),
                "planned_entry": g.get("planned_entry"),
                "stop_loss": g.get("stop_loss"),
                "take_profit": g.get("take_profit"),
                "created_index": g.get("created_index"),
                "expires_index": g.get("expires_index"),
                "saw_near_sl": g.get("saw_near_sl"),
                "status": g.get("status"),
                "mt5_ticket": g.get("mt5_ticket"),
                "pattern": (g.get("pattern") or "")[:240],
                "source": "db_dashboard",
            }
        )
    write_csv(RUN / "LIVE_PENDING_DB.csv", sorted(rows, key=lambda r: str(r["symbol"])))
    (RUN / "LIVE_PENDING_DB.json").write_text(
        json.dumps({"n": len(rows), "rows": rows}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"DB waiting={len(rows)}")

    mem_rows = []
    db_debug = []
    try:
        dbg = get("/api/debug/pending")
        (RUN / "LIVE_DEBUG_PENDING_RAW.json").write_text(
            json.dumps(dbg, indent=2, default=str), encoding="utf-8"
        )
        mem_rows = list(dbg.get("memory") or [])
        db_debug = list(dbg.get("db") or [])
        write_csv(RUN / "LIVE_PENDING_MEMORY.csv", sorted(mem_rows, key=lambda r: r["symbol"]))
        print(f"memory={dbg.get('memory_n')} db_debug={dbg.get('db_n')}")
    except Exception as exc:
        (RUN / "LIVE_PENDING_MEMORY.csv").write_text("", encoding="utf-8")
        (RUN / "LIVE_MEMORY_DUMP_STATUS.txt").write_text(
            f"NOT PRODUCED via /api/debug/pending: {exc}\n", encoding="utf-8"
        )
        print(f"memory dump failed: {exc}")

    # Comparison memory vs DB
    mem_by = {r["symbol"]: r for r in mem_rows}
    db_by = {r["symbol"]: r for r in (db_debug or rows)}
    syms = sorted(set(mem_by) | set(db_by))
    cmp_rows = []
    for s in syms:
        m, d = mem_by.get(s), db_by.get(s)
        if m and d:
            pe_ok = abs(float(m["planned_entry"]) - float(d["planned_entry"])) < 1e-9
            sl_ok = abs(float(m["stop_loss"]) - float(d["stop_loss"])) < 1e-9
            cls = "MATCH" if pe_ok and sl_ok and str(m["action"]).lower() == str(d["action"]).lower() else "GEOMETRY_MISMATCH"
        elif m and not d:
            cls = "MEMORY_ONLY"
        elif d and not m:
            cls = "DB_ONLY"
        else:
            cls = "UNKNOWN"
        cmp_rows.append(
            {
                "symbol": s,
                "class": cls,
                "mem_action": None if not m else m.get("action"),
                "db_action": None if not d else d.get("action"),
                "mem_planned_entry": None if not m else m.get("planned_entry"),
                "db_planned_entry": None if not d else d.get("planned_entry"),
                "mem_stop_loss": None if not m else m.get("stop_loss"),
                "db_stop_loss": None if not d else d.get("stop_loss"),
                "mem_expires_index": None if not m else m.get("expires_index"),
                "db_expires_index": None if not d else d.get("expires_index"),
            }
        )
    write_csv(RUN / "LIVE_PENDING_MEMORY_DB_COMPARISON.csv", cmp_rows)
    print(f"comparison rows={len(cmp_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
