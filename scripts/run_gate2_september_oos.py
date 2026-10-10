#!/usr/bin/env python3
"""Gate 2 September OOS gatekeeper — run ONLY if Sep 2026 is closed+complete.

If not closed: write pending artifact and STOP (no backtest).
Offline assessment of cache coverage does not require MT5.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.viewerMT5.mt5_feed import _cache_path

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
THU_PRIOR = ROOT / "data/runs/research/gate2_thursday_gate_20260925_195229"

CONTROL = dict(
    strategy="alexg8",
    ghost_sl_mult=4.0,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=5.0,
    true_sl_rr=2.0,
    rr_mode="dynamic",
    true_sl=True,
    size_mode="margin",
    leverage=500.0,
    position_size_pct=0.01,
    commission_per_lot=7.0,
    force_flat_daily=True,
    force_flat_friday=False,
    sides="both",
    path="h1_signal_1m_exit",
)

TREATMENT = dict(
    CONTROL,
    thursday_entry_gate="borex.gate2.thursday_entry_gate.filter_block_thursday_entries",
)


def _git() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


def fingerprint(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def _as_utc_index(ts) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(ts, utc=True))
    if idx.tz is None:
        return idx.tz_localize("UTC")
    return idx.tz_convert("UTC")


def analyze_interval(symbols: list[str], interval: str) -> list[dict[str, Any]]:
    sep_start = pd.Timestamp("2026-09-01", tz="UTC")
    sep_end = pd.Timestamp("2026-10-01", tz="UTC")
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        path = _cache_path(sym, interval)
        if not path.is_file():
            rows.append(
                {
                    "symbol": sym,
                    "interval": interval,
                    "status": "MISSING_FILE",
                    "n": 0,
                    "tmin": None,
                    "tmax": None,
                    "has_sep30": False,
                    "path": str(path.as_posix()),
                }
            )
            continue
        df = pd.read_parquet(path)
        if "timestamp" in df.columns:
            ts = _as_utc_index(df["timestamp"])
        elif isinstance(df.index, pd.DatetimeIndex):
            ts = _as_utc_index(df.index)
        else:
            rows.append(
                {
                    "symbol": sym,
                    "interval": interval,
                    "status": "NO_TIMESTAMP",
                    "n": 0,
                    "tmin": None,
                    "tmax": None,
                    "has_sep30": False,
                    "path": str(path.as_posix()),
                }
            )
            continue
        mask = (ts >= sep_start) & (ts < sep_end)
        n = int(mask.sum())
        if n == 0:
            rows.append(
                {
                    "symbol": sym,
                    "interval": interval,
                    "status": "NO_SEP_BARS",
                    "n": 0,
                    "tmin": None,
                    "tmax": None,
                    "has_sep30": False,
                    "path": str(path.as_posix()),
                }
            )
            continue
        sub = ts[mask]
        tmin, tmax = sub.min(), sub.max()
        has_day30 = bool(((sub.year == 2026) & (sub.month == 9) & (sub.day == 30)).any())
        status = "COMPLETE_THROUGH_DAY30" if has_day30 else "INCOMPLETE_TODATE"
        rows.append(
            {
                "symbol": sym,
                "interval": interval,
                "status": status,
                "n": n,
                "tmin": str(tmin),
                "tmax": str(tmax),
                "has_sep30": has_day30,
                "path": str(path.as_posix()),
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(r["status"] for r in rows)
    tmaxes = [r["tmax"] for r in rows if r.get("tmax")]
    return {
        "n_symbols": len(rows),
        "status_counts": dict(counts),
        "complete_through_day30": sum(1 for r in rows if r.get("has_sep30")),
        "incomplete_or_missing": sum(1 for r in rows if not r.get("has_sep30")),
        "earliest_tmax": min(tmaxes) if tmaxes else None,
        "latest_tmax": max(tmaxes) if tmaxes else None,
        "min_n": min((r["n"] for r in rows), default=0),
        "max_n": max((r["n"] for r in rows), default=0),
    }


def main() -> int:
    now = datetime.now(timezone.utc)
    sep_end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    calendar_closed = now >= sep_end

    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun["spread_pips_by_symbol"].keys())
    uhash = universe_hash(symbols)

    h1_rows = analyze_interval(symbols, "1h")
    m1_rows = analyze_interval(symbols, "1m")
    h1 = summarize(h1_rows)
    m1 = summarize(m1_rows)

    h1_complete = h1["complete_through_day30"] == 60
    m1_complete = m1["complete_through_day30"] == 60
    month_closed = bool(calendar_closed and h1_complete and m1_complete)

    missing_requirements: list[str] = []
    if not calendar_closed:
        missing_requirements.append(
            f"calendar_not_closed: now_utc={now.isoformat()} < 2026-10-01T00:00:00+00:00"
        )
    if not h1_complete:
        missing_requirements.append(
            f"h1_not_60_through_sep30: {h1['complete_through_day30']}/60 "
            f"(statuses={h1['status_counts']})"
        )
    if not m1_complete:
        missing_requirements.append(
            f"m1_not_60_through_sep30: {m1['complete_through_day30']}/60 "
            f"(statuses={m1['status_counts']})"
        )

    # Config fingerprints (locked candidate continuity)
    ctrl_fp = fingerprint(CONTROL)
    trt_fp = fingerprint(TREATMENT)
    diff_keys = sorted(set(CONTROL) ^ set(TREATMENT) | {
        k for k in CONTROL if CONTROL.get(k) != TREATMENT.get(k)
    })
    # Only allowed difference: thursday_entry_gate on treatment
    unexpected = [k for k in diff_keys if k != "thursday_entry_gate"]
    config_ok = unexpected == [] and "thursday_entry_gate" in TREATMENT

    sim_fp = {
        "engine": "MultiMarketEngine",
        "path": "h1_signal_1m_exit",
        "frozen": True,
        "modified_this_run": False,
        "note": "OOS not executed in pending mode",
    }

    ts = now.strftime("%Y%m%d_%H%M%S")
    if not month_closed:
        out = ROOT / "data" / "runs" / "research" / f"gate2_september_oos_pending_{ts}"
        out.mkdir(parents=True, exist_ok=True)

        coverage = {
            "assessed_at_utc": now.isoformat(),
            "month": "2026-09",
            "month_closed": False,
            "calendar_closed": calendar_closed,
            "required": {
                "window": "2026-09-01 .. 2026-09-30 inclusive",
                "h1_pairs": "60/60 through Sep 30",
                "m1_pairs": "60/60 through Sep 30",
                "provenance": "data/cache/mt5/utc_v2",
                "universe": "exact 60-pair N1 universe",
                "no_source_mixing": True,
                "simulator_frozen": True,
            },
            "observed": {
                "universe_n": len(symbols),
                "universe_hash": uhash,
                "h1": h1,
                "m1": m1,
            },
            "missing_requirements": missing_requirements,
            "oos_backtest_run": False,
            "prior_thursday_candidate": str(THU_PRIOR.as_posix()),
        }
        (out / "coverage.json").write_text(
            json.dumps(coverage, indent=2), encoding="utf-8"
        )
        (out / "configuration_fingerprints.json").write_text(
            json.dumps(
                {
                    "control": CONTROL,
                    "control_sha256": ctrl_fp,
                    "treatment": TREATMENT,
                    "treatment_sha256": trt_fp,
                    "diff_keys": diff_keys,
                    "config_ok_only_thursday_gate_differs": config_ok,
                    "unexpected_diffs": unexpected,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (out / "data_provenance.json").write_text(
            json.dumps(
                {
                    "git_commit": _git(),
                    "cache_root": "data/cache/mt5/utc_v2",
                    "universe_source": str(JUN_INDEX.as_posix()),
                    "universe_hash": uhash,
                    "symbols": symbols,
                    "h1_per_symbol": h1_rows,
                    "m1_per_symbol": m1_rows,
                    "simulator_fingerprint": sim_fp,
                    "may_excluded": True,
                    "september_todate_not_substituted_for_closed_oos": True,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        status_md = f"""# Gate 2 September OOS — PENDING

Assessed at (UTC): {now.isoformat()}

## Decision

**DO NOT RUN** the locked September OOS backtest.

`month_closed=false`

No control/treatment September simulation was executed.

## Why

"""
        for m in missing_requirements:
            status_md += f"- `{m}`\n"
        status_md += f"""
## Current coverage snapshot

- Universe: **{len(symbols)}/60** (hash `{uhash[:16]}…`)
- H1 complete through Sep 30: **{h1['complete_through_day30']}/60**
- M1 complete through Sep 30: **{m1['complete_through_day30']}/60**
- H1 latest tmax observed: `{h1['latest_tmax']}`
- M1 latest tmax observed: `{m1['latest_tmax']}`
- H1 status counts: `{h1['status_counts']}`
- M1 status counts: `{m1['status_counts']}`

## Locked configurations (not executed)

- Control fingerprint: `{ctrl_fp}`
- Treatment fingerprint: `{trt_fp}`
- Diff keys: `{diff_keys}` (must be only `thursday_entry_gate`)
- Config integrity OK: `{config_ok}`

## Protocol reminder

- Do **not** substitute September-to-date for closed-month OOS.
- Thursday gate remains a **frozen research candidate**.
- Do **not** Mirror-deploy.
- Do **not** combine with rr_min=2.5.
- Next: re-run this gatekeeper after `2026-10-01` once utc_v2 H1+M1 are 60/60 through Sep 30.

## Artifacts

- `coverage.json`
- `configuration_fingerprints.json`
- `data_provenance.json`
"""
        (out / "status.md").write_text(status_md, encoding="utf-8")
        print(f"PENDING {out}", flush=True)
        print("month_closed=false", flush=True)
        print("oos_backtest_run=false", flush=True)
        for m in missing_requirements:
            print(f"MISSING: {m}", flush=True)
        return 0

    # Closed path would continue to OOS — not reached while pending.
    print("month_closed=true — OOS runner not implemented in this gatekeeper pass", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
