#!/usr/bin/env python3
"""Pack AlexG10 Test3 artifacts into a small compendium (<= few files) for handoff."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main")
SRC = ROOT / "data" / "runs" / "research" / "alexg10_test3_20261004_141920"
OUT = ROOT / "data" / "runs" / "research" / "compendium" / "alexg10_test3"
OUT.mkdir(parents=True, exist_ok=True)

SKIP_NAMES = {"_h1_payload.pkl"}
SKIP_SUFFIX_HUGE = ()  # handle by size/name rules
MAX_EVENTS_BYTES = 500_000  # skip embedding giant POST_FIX event dumps


def is_huge_ledger(p: Path) -> bool:
    name = p.name.upper()
    if p.suffix.lower() != ".csv":
        return False
    if "EVENTS" in name and p.stat().st_size > MAX_EVENTS_BYTES:
        return True
    if "REJECTS" in name and p.stat().st_size > MAX_EVENTS_BYTES:
        return True
    return False


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_error": str(exc), "_path": p.name}


def main() -> int:
    files = sorted([p for p in SRC.iterdir() if p.is_file() and p.name not in SKIP_NAMES])
    inventory = []
    for p in files:
        inventory.append(
            {
                "name": p.name,
                "bytes": p.stat().st_size,
                "mtime_utc": datetime.fromtimestamp(
                    p.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "included_in": [],
            }
        )

    # ---------- JSON bundle: all *.json ----------
    json_bundle = {
        "meta": {
            "label": "alexg10_test3_COMPENDIUM",
            "source_dir": str(SRC),
            "packed_utc": datetime.now(timezone.utc).isoformat(),
            "interpretation": {
                "PRE_FIX": "INVALIDATED_FOR_CAUSAL_INFERENCE — SHARED_ZONE_HTF_CACHE_BUG",
                "POST_FIX": "VALID Test3 regime",
                "live_mirror": "Do not promote Live/Mirror from these offline results yet",
            },
        },
        "summaries": {},
        "other_json": {},
    }
    metrics_rows = []
    for p in files:
        if p.suffix.lower() != ".json":
            continue
        obj = load_json(p)
        if p.name.endswith("_SUMMARY.json"):
            json_bundle["summaries"][p.name] = obj
            m = obj.get("metrics") or {}
            f = obj.get("funnel") or {}
            cs = obj.get("cache_stats") or {}
            metrics_rows.append(
                {
                    "source_file": p.name,
                    "table": "summary_metrics",
                    "name": obj.get("name"),
                    "code_mode": obj.get("code_mode"),
                    "ghost_sl_mult": obj.get("ghost_sl_mult"),
                    "exclude": json.dumps(obj.get("exclude"), default=str),
                    "n_symbols": obj.get("n_symbols"),
                    "fills": m.get("fills"),
                    "net_pnl": m.get("net_pnl"),
                    "profit_factor": m.get("profit_factor"),
                    "ending_equity": m.get("ending_equity") or obj.get("final_equity_engine"),
                    "max_drawdown": m.get("max_drawdown"),
                    "TP": m.get("TP"),
                    "margin_stop": m.get("margin_stop"),
                    "candidates": f.get("candidate_signals"),
                    "ghosts_created": f.get("ghosts_created"),
                    "ghosts_triggered": f.get("ghosts_triggered_signals"),
                    "ghost_fill_rate": f.get("ghost_fill_signal_rate"),
                    "zone_polluted_reuses": cs.get("zone_polluted_reuses"),
                    "elapsed_s": obj.get("elapsed_s"),
                    "n_events": obj.get("n_events"),
                    "note": obj.get("note"),
                }
            )
            for inv in inventory:
                if inv["name"] == p.name:
                    inv["included_in"].append("COMPENDIUM.json#summaries")
        else:
            json_bundle["other_json"][p.name] = obj
            for inv in inventory:
                if inv["name"] == p.name:
                    inv["included_in"].append("COMPENDIUM.json#other_json")

    json_path = OUT / "alexg10_test3_COMPENDIUM.json"
    json_path.write_text(
        json.dumps(json_bundle, indent=2, default=str), encoding="utf-8"
    )

    # ---------- CSV bundle: metrics + all non-huge CSVs with source_file ----------
    csv_path = OUT / "alexg10_test3_COMPENDIUM.csv"
    # Collect union of fieldnames dynamically
    rows: list[dict] = list(metrics_rows)
    for p in files:
        if p.suffix.lower() != ".csv":
            continue
        if is_huge_ledger(p):
            rows.append(
                {
                    "source_file": p.name,
                    "table": "omitted_huge_ledger",
                    "bytes": p.stat().st_size,
                    "note": "Omitted from CSV merge (too large); see original artifact dir",
                }
            )
            for inv in inventory:
                if inv["name"] == p.name:
                    inv["included_in"].append("COMPENDIUM.csv#omitted_pointer")
            continue
        try:
            with p.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for i, row in enumerate(reader):
                    out = {"source_file": p.name, "table": "csv_row", "row_index": i}
                    out.update(row)
                    rows.append(out)
            for inv in inventory:
                if inv["name"] == p.name:
                    inv["included_in"].append("COMPENDIUM.csv")
        except Exception as exc:
            rows.append(
                {
                    "source_file": p.name,
                    "table": "csv_read_error",
                    "note": str(exc),
                }
            )

    # stable-ish field order
    preferred = [
        "source_file",
        "table",
        "row_index",
        "name",
        "code_mode",
        "ghost_sl_mult",
        "universe",
        "fills",
        "net_pnl",
        "net",
        "profit_factor",
        "PF",
        "candidates",
        "zone_polluted_reuses",
        "note",
        "bytes",
    ]
    fieldnames: list[str] = []
    seen = set()
    for k in preferred:
        if any(k in r for r in rows) and k not in seen:
            fieldnames.append(k)
            seen.add(k)
    for r in rows:
        for k in r.keys():
            if k not in seen:
                fieldnames.append(k)
                seen.add(k)

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # ---------- MD bundle ----------
    md_parts = []
    md_parts.append("# AlexG10 Test 3 — Compendium\n")
    md_parts.append(
        f"Packed `{datetime.now(timezone.utc).isoformat()}` from `{SRC}`.\n\n"
        "## Handoff rules\n"
        "- **PRE_FIX = INVALIDATED** for causal inference (`SHARED_ZONE_HTF_CACHE_BUG`).\n"
        "- **POST_FIX = valid** regime.\n"
        "- Do not promote Live/Mirror from these offline results yet.\n"
        "- This folder is intentionally ≤4 files for AIs with ~20-file attach limits.\n\n"
        "## Files in this pack\n"
        "- `alexg10_test3_COMPENDIUM.md` (this file)\n"
        "- `alexg10_test3_COMPENDIUM.csv` (summary metrics + all non-huge CSVs)\n"
        "- `alexg10_test3_COMPENDIUM.json` (all JSON artifacts)\n"
        "- `alexg10_test3_INVENTORY.csv` (what was included / omitted)\n\n"
    )

    for name in (
        "PRE_FIX_INVALIDATED.json",
        "TEST3_EXECUTIVE_SUMMARY.md",
        "TEST3_REPORT.md",
        "TEST3_MANIFEST.json",
        "BUG_SHARED_ZONE_CACHE.json",
        "PRE_VS_POST_FIX.json",
        "A4_MECHANISM_MINUS_TOP8.json",
        "FIRST_DIVERGENCE.json",
        "GHOST_SL_EFFECT_SUMMARY.json",
    ):
        p = SRC / name
        if not p.exists():
            continue
        md_parts.append(f"\n---\n\n## Embedded: `{name}`\n\n")
        if p.suffix.lower() == ".md":
            md_parts.append(p.read_text(encoding="utf-8", errors="replace"))
            md_parts.append("\n")
        else:
            md_parts.append("```json\n")
            md_parts.append(
                json.dumps(load_json(p), indent=2, default=str)[:200_000]
            )
            md_parts.append("\n```\n")
        for inv in inventory:
            if inv["name"] == name and "COMPENDIUM.md" not in inv["included_in"]:
                inv["included_in"].append("COMPENDIUM.md")

    # Embed key grid CSVs as markdown tables (small)
    md_parts.append("\n---\n\n## Key grid CSVs (preview)\n")
    for name in (
        "GHOST_SL_GRID_POST_FIX_FULL.csv",
        "GHOST_SL_GRID.csv",
        "GHOST_SL_LOO_MATRIX.csv",
        "GHOST_SL_FILL_FUNNEL.csv",
    ):
        p = SRC / name
        if not p.exists():
            continue
        md_parts.append(f"\n### `{name}`\n\n```csv\n")
        text = p.read_text(encoding="utf-8", errors="replace")
        md_parts.append(text if len(text) < 100_000 else text[:100_000] + "\n...TRUNCATED...\n")
        md_parts.append("```\n")
        for inv in inventory:
            if inv["name"] == name and "COMPENDIUM.md" not in inv["included_in"]:
                inv["included_in"].append("COMPENDIUM.md#preview")

    # Mark remaining md files
    for p in files:
        if p.suffix.lower() == ".md":
            for inv in inventory:
                if inv["name"] == p.name and not inv["included_in"]:
                    inv["included_in"].append("see_source_or_COMPENDIUM.md")

    md_path = OUT / "alexg10_test3_COMPENDIUM.md"
    md_path.write_text("".join(md_parts), encoding="utf-8")

    inv_path = OUT / "alexg10_test3_INVENTORY.csv"
    with inv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["name", "bytes", "mtime_utc", "included_in"]
        )
        w.writeheader()
        for inv in inventory:
            w.writerow(
                {
                    **inv,
                    "included_in": "|".join(inv["included_in"]) or "NOT_EMBEDDED",
                }
            )

    readme = OUT / "README.md"
    readme.write_text(
        "# AlexG10 Test3 Compendium\n\n"
        "Attach these **4 files** (plus optional README) to another AI instead of the raw 195-artifact folder:\n\n"
        "1. `alexg10_test3_COMPENDIUM.md`\n"
        "2. `alexg10_test3_COMPENDIUM.csv`\n"
        "3. `alexg10_test3_COMPENDIUM.json`\n"
        "4. `alexg10_test3_INVENTORY.csv`\n\n"
        f"Source: `{SRC}`\n",
        encoding="utf-8",
    )

    print("OUT", OUT)
    for p in sorted(OUT.iterdir()):
        print(f"  {p.name}\t{p.stat().st_size}")
    print("rows_csv", len(rows), "summaries", len(json_bundle["summaries"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
