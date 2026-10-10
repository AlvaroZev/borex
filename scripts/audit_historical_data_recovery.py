#!/usr/bin/env python3
"""Phase 1–3: Local historical data recovery audit (offline, no MT5/LIVE/Mirror/Sep).

Writes coverage matrix + dataset manifest into an existing recovery run directory.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Pinned N1 60-pair universe from July index (authoritative for research)
JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
UTC_V2 = ROOT / "data/cache/mt5/utc_v2"
UTC_V1 = ROOT / "data/cache/mt5/utc"
LEGACY = ROOT / "data/cache"

MONTHS = [
    (2025, 10), (2025, 11), (2025, 12),
    (2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5),
    (2026, 6), (2026, 7), (2026, 8),
]

# Soft completeness (weekday FX; weekends empty) — same as extend script
MIN_M1 = 20_000
MIN_H1 = 400


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    except Exception:
        return "unknown"


def load_universe() -> list[str]:
    idx = json.loads(JUL_INDEX.read_text(encoding="utf-8"))
    syms = list(idx.get("symbols") or [])
    if not syms:
        # fallback: utc_v2 dirs
        syms = sorted(p.name for p in UTC_V2.iterdir() if p.is_dir())
    return syms


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


def month_bounds(y: int, m: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year=y, month=m, day=1, tz="UTC")
    if m == 12:
        end = pd.Timestamp(year=y + 1, month=1, day=1, tz="UTC")
    else:
        end = pd.Timestamp(year=y, month=m + 1, day=1, tz="UTC")
    return start, end


def read_ts(path: Path) -> pd.Series | None:
    if not path.is_file():
        return None
    try:
        df = pd.read_parquet(path, columns=["timestamp"])
        return pd.to_datetime(df["timestamp"], utc=True)
    except Exception:
        try:
            df = pd.read_parquet(path)
            col = "timestamp" if "timestamp" in df.columns else df.columns[0]
            return pd.to_datetime(df[col], utc=True)
        except Exception:
            return None


def inspect_file(path: Path) -> dict:
    out = {
        "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "n": 0,
        "start": None,
        "end": None,
        "error": None,
    }
    if not path.is_file():
        return out
    try:
        df = pd.read_parquet(path)
        out["columns"] = list(df.columns)
        ts_col = "timestamp" if "timestamp" in df.columns else None
        if ts_col is None:
            for c in df.columns:
                if "time" in c.lower():
                    ts_col = c
                    break
        if ts_col:
            ts = pd.to_datetime(df[ts_col], utc=True)
            out["n"] = int(len(ts))
            if len(ts):
                out["start"] = str(ts.min())
                out["end"] = str(ts.max())
                # minute alignment sample
                sample = ts.dropna().head(500)
                if len(sample):
                    out["pct_on_minute"] = float((sample.dt.second == 0).mean())
                    out["pct_on_hour"] = float(((sample.dt.minute == 0) & (sample.dt.second == 0)).mean())
        # OHLC presence
        for k in ("open", "high", "low", "close"):
            out[f"has_{k}"] = k in df.columns
        out["has_spread"] = any("spread" in c.lower() for c in df.columns)
        out["has_volume"] = any(c in df.columns for c in ("volume", "tick_volume", "real_volume"))
        # duplicates / NaNs
        if ts_col and len(df):
            out["n_dup_ts"] = int(ts.duplicated().sum())
            ohlc = [c for c in ("open", "high", "low", "close") if c in df.columns]
            if ohlc:
                out["n_nan_ohlc"] = int(df[ohlc].isna().any(axis=1).sum())
    except Exception as e:
        out["error"] = str(e)
    return out


def month_count(ts: pd.Series | None, y: int, m: int) -> int:
    if ts is None or len(ts) == 0:
        return 0
    start, end = month_bounds(y, m)
    return int(((ts >= start) & (ts < end)).sum())


def classify_month(h1_ok: int, m1_ok: int, n_univ: int, h1_partial: int, m1_partial: int) -> str:
    """A/B/C/D classification."""
    if h1_ok == n_univ and m1_ok == n_univ:
        return "A_FULLY_USABLE"
    if m1_ok >= int(0.8 * n_univ) and h1_ok >= int(0.8 * n_univ):
        return "B_PARTIALLY_USABLE"
    if h1_ok >= int(0.8 * n_univ) and m1_ok < int(0.5 * n_univ):
        return "C_H1_ONLY"
    if h1_partial > 0 or m1_partial > 0:
        return "B_PARTIALLY_USABLE" if (h1_partial + m1_partial) > 0 and m1_partial > 0 else "C_H1_ONLY" if h1_partial > 0 else "D_INVALID"
    return "D_INVALID"


def main() -> int:
    out_dirs = sorted((ROOT / "data/runs/research").glob("gate2_historical_data_recovery_*"))
    if not out_dirs:
        raise SystemExit("No recovery run directory found")
    OUT = out_dirs[-1]
    print(f"OUT={OUT}")

    symbols = load_universe()
    uh = universe_hash(symbols)
    print(f"universe n={len(symbols)} hash={uh[:12]}")

    sources = {
        "utc_v2": {
            "root": UTC_V2,
            "h1": lambda s: UTC_V2 / s / "1h.parquet",
            "m1": lambda s: UTC_V2 / s / "1m.parquet",
            "convention": "utc_v2 (whole-hour H1, closed bars; N1 research standard)",
        },
        "utc_v1": {
            "root": UTC_V1,
            "h1": lambda s: UTC_V1 / s / "1h.parquet",
            "m1": lambda s: UTC_V1 / s / "1m.parquet",
            "convention": "utc/v1 (mixed :45 snaps historically; NOT N1 standard)",
        },
        "legacy_flat": {
            "root": LEGACY,
            "h1": lambda s: LEGACY / s / "1h.parquet",
            "m1": lambda s: LEGACY / s / "1m.parquet",
            "convention": "legacy data/cache/{SYM} flat (provenance TBD; not utc_v2 path)",
        },
        "legacy_years_2026": {
            "root": LEGACY,
            "h1": lambda s: LEGACY / s / "years" / "1h" / "2026.parquet",
            "m1": lambda s: LEGACY / s / "years" / "1m" / "2026.parquet",
            "convention": "legacy year shard 2026 (provenance TBD)",
        },
        "legacy_years_2025": {
            "root": LEGACY,
            "h1": lambda s: LEGACY / s / "years" / "1h" / "2025.parquet",
            "m1": lambda s: LEGACY / s / "years" / "1m" / "2025.parquet",
            "convention": "legacy year shard 2025 (provenance TBD)",
        },
    }

    # Also check mt5/{SYM} without utc_v2
    sources["mt5_sym_flat"] = {
        "root": ROOT / "data/cache/mt5",
        "h1": lambda s: ROOT / "data/cache/mt5" / s / "1h.parquet",
        "m1": lambda s: ROOT / "data/cache/mt5" / s / "1m.parquet",
        "convention": "data/cache/mt5/{SYM} flat (non utc_v2)",
    }

    manifest_rows: list[dict] = []
    coverage_rows: list[dict] = []

    # Preload timestamps for speed (per source/symbol/interval)
    ts_cache: dict[tuple[str, str, str], pd.Series | None] = {}

    for src_name, src in sources.items():
        print(f"\n=== SOURCE {src_name} ===")
        h1_present = 0
        m1_present = 0
        for sym in symbols:
            for iv, getter in (("1h", src["h1"]), ("1m", src["m1"])):
                path = getter(sym)
                info = inspect_file(path)
                info.update({
                    "source": src_name,
                    "symbol": sym,
                    "interval": iv,
                    "convention": src["convention"],
                })
                manifest_rows.append(info)
                key = (src_name, sym, iv)
                if info["exists"] and not info.get("error"):
                    ts_cache[key] = read_ts(path)
                    if iv == "1h":
                        h1_present += 1
                    else:
                        m1_present += 1
                else:
                    ts_cache[key] = None
        print(f"  files with data: H1={h1_present}/{len(symbols)} M1={m1_present}/{len(symbols)}")

        for y, m in MONTHS:
            h1_ok = m1_ok = h1_partial = m1_partial = 0
            h1_counts = []
            m1_counts = []
            missing_h1 = []
            missing_m1 = []
            for sym in symbols:
                hc = month_count(ts_cache[(src_name, sym, "1h")], y, m)
                mc = month_count(ts_cache[(src_name, sym, "1m")], y, m)
                h1_counts.append(hc)
                m1_counts.append(mc)
                if hc >= MIN_H1:
                    h1_ok += 1
                elif hc > 0:
                    h1_partial += 1
                    missing_h1.append(f"{sym}:{hc}")
                else:
                    missing_h1.append(f"{sym}:0")
                if mc >= MIN_M1:
                    m1_ok += 1
                elif mc > 0:
                    m1_partial += 1
                    missing_m1.append(f"{sym}:{mc}")
                else:
                    missing_m1.append(f"{sym}:0")
            cls = classify_month(h1_ok, m1_ok, len(symbols), h1_partial, m1_partial)
            # strategy usable = A only for comparable N1 economics; B diagnostic
            strategy_usable = cls == "A_FULLY_USABLE" and src_name == "utc_v2"
            utc_v2_compatible = src_name == "utc_v2"
            coverage_rows.append({
                "source": src_name,
                "year": y,
                "month": m,
                "period": f"{y}-{m:02d}",
                "h1_symbols_ok": h1_ok,
                "m1_symbols_ok": m1_ok,
                "h1_symbols_partial": h1_partial,
                "m1_symbols_partial": m1_partial,
                "complete_60": h1_ok == 60 and m1_ok == 60,
                "h1_median_bars": int(pd.Series(h1_counts).median()) if h1_counts else 0,
                "m1_median_bars": int(pd.Series(m1_counts).median()) if m1_counts else 0,
                "h1_min_bars": int(min(h1_counts)) if h1_counts else 0,
                "m1_min_bars": int(min(m1_counts)) if m1_counts else 0,
                "gaps_note": f"h1_fail={60-h1_ok};m1_fail={60-m1_ok}",
                "utc_v2_compatible": utc_v2_compatible,
                "classification": cls,
                "strategy_usable": strategy_usable,
                "missing_m1_sample": ";".join(missing_m1[:8]),
                "missing_h1_sample": ";".join(missing_h1[:8]),
            })
            print(
                f"  {y}-{m:02d}: H1_ok={h1_ok} M1_ok={m1_ok} "
                f"med_m1={int(pd.Series(m1_counts).median()) if m1_counts else 0} -> {cls}"
            )

    # Write CSVs
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
                w.writerow({k: r.get(k) for k in keys})

    write_csv(OUT / "DATASET_MANIFEST.csv", manifest_rows)
    write_csv(OUT / "DATA_COVERAGE_MATRIX.csv", coverage_rows)

    # Earliest fully usable utc_v2 date
    usable = [
        r for r in coverage_rows
        if r["source"] == "utc_v2" and r["classification"] == "A_FULLY_USABLE"
    ]
    earliest_full = min((r["period"] for r in usable), default=None)

    # Compare utc_v2 vs legacy for May 2026 specifically
    may_compare = [r for r in coverage_rows if r["period"] == "2026-05"]

    # Prior research artifacts mentioning May
    prior_notes = []
    for p in [
        ROOT / "data/runs/research",
    ]:
        if p.is_dir():
            for f in p.rglob("*may*"):
                if f.is_file() and f.suffix in {".md", ".json", ".csv"}:
                    prior_notes.append(str(f.relative_to(ROOT)))

    meta = {
        "git_commit": git_commit(),
        "universe_n": len(symbols),
        "universe_hash": uh,
        "symbols": symbols,
        "earliest_fully_usable_utc_v2_month": earliest_full,
        "sources_audited": list(sources.keys()),
        "min_m1_threshold": MIN_M1,
        "min_h1_threshold": MIN_H1,
        "september_excluded": True,
        "live_touched": False,
        "mirror_touched": False,
        "mt5_terminal_used": False,
        "prior_may_artifacts_sample": prior_notes[:40],
        "may_2026_by_source": may_compare,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (OUT / "audit_meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

    # Quick DATA_RECOVERY_REPORT draft (will be finalized after baselines)
    lines = [
        "# DATA RECOVERY REPORT",
        "",
        f"**Run directory:** `{OUT.name}`",
        f"**Git:** `{meta['git_commit']}`",
        f"**Universe:** {len(symbols)} symbols (hash `{uh[:16]}…`)",
        "",
        "## Safety",
        "",
        "- `SEPTEMBER_READ = FALSE`",
        "- `LIVE_TOUCHED = FALSE`",
        "- `MIRROR_TOUCHED = FALSE`",
        "- `MT5_TERMINAL_USED = FALSE`",
        "- `SEP_OOS_LOCK_MODIFIED = FALSE`",
        "",
        "## Question 1 — Can we recover pre-June data?",
        "",
        "Local offline audit of all candidate caches (no broker download).",
        "",
        f"**Earliest fully comparable utc_v2 H1+M1 month (60/60):** `{earliest_full}`",
        "",
        "## Coverage matrix (utc_v2 — strategy-comparable)",
        "",
        "| Period | H1 ok | M1 ok | Complete 60? | Class | Strategy usable? |",
        "| --- | ---: | ---: | --- | --- | --- |",
    ]
    for r in coverage_rows:
        if r["source"] != "utc_v2":
            continue
        lines.append(
            f"| {r['period']} | {r['h1_symbols_ok']} | {r['m1_symbols_ok']} | "
            f"{r['complete_60']} | {r['classification']} | {r['strategy_usable']} |"
        )
    lines += [
        "",
        "## Other sources (compatibility)",
        "",
        "| Source | Convention | Notes |",
        "| --- | --- | --- |",
    ]
    for name, src in sources.items():
        lines.append(f"| `{name}` | {src['convention']} | See DATA_COVERAGE_MATRIX.csv |")
    lines += [
        "",
        "## May 2026 cross-source",
        "",
    ]
    for r in may_compare:
        lines.append(
            f"- `{r['source']}`: H1_ok={r['h1_symbols_ok']} M1_ok={r['m1_symbols_ok']} "
            f"class={r['classification']} strategy_usable={r['strategy_usable']}"
        )
    lines += [
        "",
        "## Searched locations",
        "",
        "- `data/cache/mt5/utc_v2/{SYM}/{1h,1m}.parquet` (N1 standard)",
        "- `data/cache/mt5/utc/{SYM}/…` (v1)",
        "- `data/cache/mt5/{SYM}/…` (flat)",
        "- `data/cache/{SYM}/…` + `years/{1h,1m}/{YYYY}.parquet` (legacy)",
        "- Existing run indexes: `mt5_1m_n1_oos_jun2026`, `jul2026`, `slmult_lev_split`",
        "- Scripts documenting prior May attempts: `run_n1_may_oos_integrity_gate.py`, `extend_mt5_historical_coverage.py`",
        "",
        "**Not used:** MT5 terminal, Mirror login, Live, broker download, September files.",
        "",
        "See `DATA_COVERAGE_MATRIX.csv`, `DATASET_MANIFEST.csv`, `audit_meta.json`.",
        "",
    ]
    (OUT / "DATA_RECOVERY_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("\nWROTE", OUT / "DATA_RECOVERY_REPORT.md")
    print("earliest_full", earliest_full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
