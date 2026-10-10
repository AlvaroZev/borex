#!/usr/bin/env python3
"""Extend MT5 utc_v2 historical cache for unseen-OOS research (data only).

- Same provenance as N1 closest-sim (MT5 → data/cache/mt5/utc_v2)
- Mirror terminal historical copy_rates only (no orders)
- No HistData / no vendor mix / no universe shrink
- No strategy / simulator / backtest execution
- Restart-safe merge via existing fetch_mt5_candles write_cache path
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "borex_live"))

from dotenv import load_dotenv

load_dotenv(ROOT.parent / "borex_live" / ".env")

from borex.viewerMT5.mt5_feed import (  # noqa: E402
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
    fetch_mt5_candles,
)

JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"

# Prefer Mirror for historical reads (login 53019882). Never trade.
EXCLUDED_EVAL_MONTHS = {(2026, 5), (2026, 6), (2026, 7), (2026, 8)}
# Soft completeness thresholds (weekday FX; weekends empty).
MIN_M1_MONTH = 20_000
MIN_H1_MONTH = 400


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def universe_hash(symbols: list[str]) -> str:
    payload = "\n".join(symbols).encode()
    return hashlib.sha256(payload).hexdigest()


def month_bounds(year: int, month: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year=year, month=month, day=1, tz="UTC")
    if month == 12:
        end = pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC")
    else:
        end = pd.Timestamp(year=year, month=month + 1, day=1, tz="UTC")
    return start, end


def connect_mirror_readonly():
    """Connect Mirror MT5 for copy_rates only. No order APIs used."""
    from borex_live.mt5.client import Mt5Client

    path = os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", "")
    client = Mt5Client(path=path)
    client.connect()
    mt5 = client._mt5
    acc = mt5.account_info() if mt5 is not None else None
    login = int(getattr(acc, "login", 0) or 0)
    # Safety: refuse Live account
    if login == 52958398:
        client._connected = False
        client._mt5 = None
        raise SystemExit("Refusing Live account 52958398 for historical extension")
    return client, {
        "path": path,
        "login": login,
        "server": str(getattr(acc, "server", "") or ""),
        "offset_seconds": int(getattr(client, "server_offset_seconds", 0) or 0),
        "orders_placed": False,
        "trade_apis_called": False,
    }


def snapshot_cache(symbols: list[str], interval: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sym in symbols:
        path = _cache_path(sym, interval)
        if not path.is_file():
            out[sym] = {"exists": False}
            continue
        st = path.stat()
        df = pd.read_parquet(path, columns=["timestamp"])
        ts = pd.to_datetime(df["timestamp"], utc=True)
        out[sym] = {
            "exists": True,
            "n": int(len(ts)),
            "start": str(ts.min()) if len(ts) else None,
            "end": str(ts.max()) if len(ts) else None,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "path": str(path),
        }
    return out


def month_is_closed(year: int, month: int, *, now: pd.Timestamp | None = None) -> bool:
    """Calendar month is closed only after its end instant has passed."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    _, end = month_bounds(year, month)
    return now >= end


def month_stats(symbols: list[str], year: int, month: int) -> list[dict]:
    start, end = month_bounds(year, month)
    closed = month_is_closed(year, month)
    # Require last bar within 2 days of month end for closed months.
    end_coverage_cutoff = end - pd.Timedelta(days=2)
    rows = []
    for sym in symbols:
        row: dict[str, Any] = {
            "month": f"{year}-{month:02d}",
            "symbol": sym,
            "H1_available": False,
            "M1_available": False,
            "H1_bars": 0,
            "M1_bars": 0,
            "H1_gaps": 0,
            "M1_gaps": 0,
            "H1_dupes": 0,
            "M1_dupes": 0,
            "M1_last": None,
            "H1_last": None,
            "month_closed": closed,
            "covers_month_end": False,
            "valid": False,
        }
        for tf, key_av, key_n, key_g, key_d, key_last in (
            ("1h", "H1_available", "H1_bars", "H1_gaps", "H1_dupes", "H1_last"),
            ("1m", "M1_available", "M1_bars", "M1_gaps", "M1_dupes", "M1_last"),
        ):
            path = _cache_path(sym, tf)
            if not path.is_file():
                continue
            df = pd.read_parquet(path, columns=["timestamp"])
            ts = pd.to_datetime(df["timestamp"], utc=True).sort_values()
            sl = ts[(ts >= start) & (ts < end)]
            dupes = int(pd.Series(sl).duplicated().sum())
            if len(sl) >= 2:
                delta = pd.Timedelta("1h" if tf == "1h" else "1min")
                diffs = pd.Series(sl).diff().dropna()
                gaps = int((diffs > delta * 1.5).sum())
            else:
                gaps = 0
            row[key_av] = len(sl) > 0
            row[key_n] = int(len(sl))
            row[key_g] = gaps
            row[key_d] = dupes
            row[key_last] = str(sl.max()) if len(sl) else None
        m1_last = pd.Timestamp(row["M1_last"]) if row["M1_last"] else None
        h1_last = pd.Timestamp(row["H1_last"]) if row["H1_last"] else None
        covers_end = bool(
            closed
            and m1_last is not None
            and h1_last is not None
            and m1_last >= end_coverage_cutoff
            and h1_last >= end_coverage_cutoff
        )
        row["covers_month_end"] = covers_end
        row["valid"] = (
            closed
            and covers_end
            and row["H1_bars"] >= MIN_H1_MONTH
            and row["M1_bars"] >= MIN_M1_MONTH
            and row["H1_dupes"] == 0
            and row["M1_dupes"] == 0
        )
        rows.append(row)
    return rows


def coverage_summary(matrix: list[dict]) -> dict:
    by_month: dict[str, list[dict]] = {}
    for r in matrix:
        by_month.setdefault(r["month"], []).append(r)
    out = {}
    for m, rows in sorted(by_month.items()):
        h1 = sum(1 for r in rows if r["H1_bars"] >= MIN_H1_MONTH)
        m1 = sum(1 for r in rows if r["M1_bars"] >= MIN_M1_MONTH)
        valid = sum(1 for r in rows if r["valid"])
        closed = bool(rows[0].get("month_closed")) if rows else False
        out[m] = {
            "pairs": len(rows),
            "month_closed": closed,
            "H1_60": h1,
            "M1_60": m1,
            "valid_60": valid,
            # Pair presence threshold only (may be in-progress month)
            "pair_coverage_60_60": h1 == 60 and m1 == 60,
            # Full calendar-month eligibility for frozen-N1 OOS
            "complete_60_60": valid == 60 and closed,
            "H1_bars_min": min(r["H1_bars"] for r in rows),
            "M1_bars_min": min(r["M1_bars"] for r in rows),
            "M1_bars_median": int(
                pd.Series([r["M1_bars"] for r in rows]).median()
            ),
            "M1_last_min": min(
                (r["M1_last"] for r in rows if r.get("M1_last")), default=None
            ),
        }
    return out


def probe_broker_depth(client) -> dict:
    from borex.data.mt5_time import mt5_unix_to_utc, utc_to_mt5_request

    mt5 = client._mt5
    off = int(getattr(client, "server_offset_seconds", 0) or 0)
    sym = client.ensure_symbol("EURUSD=X")
    end = datetime.now(timezone.utc)
    depth = {}
    for days in (30, 60, 90, 120):
        start = end - timedelta(days=days)
        # chunked to avoid empty long-range quirk
        total = 0
        earliest = latest = None
        cursor = start
        while cursor < end:
            nxt = min(cursor + timedelta(days=10), end)
            rates = mt5.copy_rates_range(
                sym,
                client._tf("1m"),
                utc_to_mt5_request(cursor, off),
                utc_to_mt5_request(nxt, off),
            )
            n = 0 if rates is None else len(rates)
            if n:
                f = mt5_unix_to_utc(int(rates[0]["time"]), off)
                l = mt5_unix_to_utc(int(rates[-1]["time"]), off)
                earliest = f if earliest is None else min(earliest, f)
                latest = l if latest is None else max(latest, l)
                total += n
            cursor = nxt
        depth[f"last_{days}d"] = {
            "bars": total,
            "earliest": str(earliest) if earliest else None,
            "latest": str(latest) if latest else None,
        }

    month_probes = {}
    for y, m in [
        (2026, 1),
        (2026, 2),
        (2026, 3),
        (2026, 4),
        (2026, 5),
        (2026, 9),
    ]:
        a, b = month_bounds(y, m)
        rates = mt5.copy_rates_range(
            sym,
            client._tf("1m"),
            utc_to_mt5_request(a.to_pydatetime(), off),
            utc_to_mt5_request(min(b, pd.Timestamp(end)).to_pydatetime(), off),
        )
        n = 0 if rates is None else len(rates)
        first = last = None
        if n:
            first = str(mt5_unix_to_utc(int(rates[0]["time"]), off))
            last = str(mt5_unix_to_utc(int(rates[-1]["time"]), off))
        month_probes[f"{y}-{m:02d}"] = {"bars": n, "first": first, "last": last}
    return {"eurusd_depth": depth, "month_probes": month_probes}


def fetch_range(
    client,
    symbols: list[str],
    *,
    interval: str,
    start: datetime,
    end: datetime,
    label: str,
) -> dict:
    """Fetch and merge into utc_v2 via existing helper (dedupe on write)."""
    ok = 0
    fail = []
    acquired = []
    for i, sym in enumerate(symbols, 1):
        try:
            bars = fetch_mt5_candles(
                sym,
                interval,
                start,
                end,
                client=client,
                use_cache=True,
                write_cache=True,
            )
            n = len(bars) if bars else 0
            acquired.append(
                {
                    "symbol": sym,
                    "bars_returned": n,
                    "first": str(bars[0].timestamp) if bars else None,
                    "last": str(bars[-1].timestamp) if bars else None,
                }
            )
            if n > 0:
                ok += 1
            else:
                fail.append({"symbol": sym, "reason": "empty"})
        except Exception as exc:
            fail.append({"symbol": sym, "reason": str(exc)[:200]})
        if i % 5 == 0 or i == len(symbols):
            print(
                f"  [{label} {interval}] {i}/{len(symbols)} ok={ok} fail={len(fail)}",
                flush=True,
            )
    return {
        "label": label,
        "interval": interval,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "ok_pairs": ok,
        "fail_n": len(fail),
        "failures": fail[:20],
        "acquired_sample": acquired[:3],
        "acquired_n": len(acquired),
    }


def jun_aug_preserved(before: dict, after: dict, symbols: list[str]) -> dict:
    """Ensure June–August M1 bar counts did not shrink / corrupt."""
    issues = []
    for sym in symbols:
        b = before.get(sym) or {}
        a = after.get(sym) or {}
        if not b.get("exists"):
            continue
        # Re-count Jun-Aug from files
        path = _cache_path(sym, "1m")
        df = pd.read_parquet(path, columns=["timestamp"])
        ts = pd.to_datetime(df["timestamp"], utc=True)
        for label, s, e in (
            ("jun", "2026-06-01", "2026-07-01"),
            ("jul", "2026-07-01", "2026-08-01"),
            ("aug", "2026-08-01", "2026-09-01"),
        ):
            # Use baseline counts stored in before snapshot extras if present
            pass
        if a.get("n", 0) < b.get("n", 0):
            issues.append(
                {
                    "symbol": sym,
                    "reason": "total_bars_decreased",
                    "before_n": b.get("n"),
                    "after_n": a.get("n"),
                }
            )
        # start should not move later (losing early history)
        if b.get("start") and a.get("start") and pd.Timestamp(a["start"]) > pd.Timestamp(
            b["start"]
        ):
            issues.append(
                {
                    "symbol": sym,
                    "reason": "start_moved_later",
                    "before_start": b.get("start"),
                    "after_start": a.get("start"),
                }
            )
    # Dedicated Jun-Aug count compare using baseline file if available
    return {"ok": len(issues) == 0, "issues": issues[:20], "issues_n": len(issues)}


def compare_jun_aug_counts(
    baseline_rows: list[dict], symbols: list[str]
) -> dict:
    by = {r["symbol"]: r for r in baseline_rows}
    issues = []
    for sym in symbols:
        path = _cache_path(sym, "1m")
        df = pd.read_parquet(path, columns=["timestamp"])
        ts = pd.to_datetime(df["timestamp"], utc=True)
        cur = {
            "jun": int(((ts >= "2026-06-01") & (ts < "2026-07-01")).sum()),
            "jul": int(((ts >= "2026-07-01") & (ts < "2026-08-01")).sum()),
            "aug": int(((ts >= "2026-08-01") & (ts < "2026-09-01")).sum()),
        }
        base = by[sym]
        for k in ("jun", "jul", "aug"):
            if cur[k] != base[k]:
                issues.append(
                    {
                        "symbol": sym,
                        "month": k,
                        "before": base[k],
                        "after": cur[k],
                    }
                )
    return {
        "ok": len(issues) == 0,
        "changed_n": len(issues),
        "changes_sample": issues[:15],
        "note": "June–August M1 bar counts must be unchanged after extension.",
    }


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"mt5_historical_extension_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    jul = json.loads(JUL_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jul["spread_pips_by_symbol"].keys())
    assert len(symbols) == 60
    uhash = universe_hash(symbols)

    # Months to assess in matrix
    months = [(2026, m) for m in range(1, 13)]

    print("Building coverage_before ...", flush=True)
    matrix_before = []
    for y, m in months:
        matrix_before.extend(month_stats(symbols, y, m))
    summary_before = coverage_summary(matrix_before)

    baseline_path = ROOT / "data/runs/research/_tmp_m1_baseline.json"
    if baseline_path.is_file():
        baseline_rows = json.loads(baseline_path.read_text(encoding="utf-8"))
    else:
        # Build inline
        baseline_rows = []
        for sym in symbols:
            path = _cache_path(sym, "1m")
            df = pd.read_parquet(path, columns=["timestamp"])
            t = pd.to_datetime(df["timestamp"], utc=True)
            baseline_rows.append(
                {
                    "symbol": sym,
                    "jun": int(((t >= "2026-06-01") & (t < "2026-07-01")).sum()),
                    "jul": int(((t >= "2026-07-01") & (t < "2026-08-01")).sum()),
                    "aug": int(((t >= "2026-08-01") & (t < "2026-09-01")).sum()),
                }
            )

    m1_before = snapshot_cache(symbols, "1m")
    h1_before = snapshot_cache(symbols, "1h")

    client, conn_meta = connect_mirror_readonly()
    print(
        f"Connected Mirror login={conn_meta['login']} server={conn_meta['server']} "
        f"offset={conn_meta['offset_seconds']}s (historical only)",
        flush=True,
    )
    try:
        depth = probe_broker_depth(client)
        print(f"Broker depth: {json.dumps(depth['eurusd_depth'], indent=2)}", flush=True)
        print(f"Month probes: {json.dumps(depth['month_probes'], indent=2)}", flush=True)

        acquisition_log = []
        now = datetime.now(timezone.utc)

        # 1) Attempt Jan–Apr 2026 M1 (expected fail if beyond broker depth)
        for y, m in [(2026, 1), (2026, 2), (2026, 3), (2026, 4)]:
            a, b = month_bounds(y, m)
            print(f"=== Attempt M1 {y}-{m:02d} (priority backward) ===", flush=True)
            acquisition_log.append(
                fetch_range(
                    client,
                    symbols,
                    interval="1m",
                    start=a.to_pydatetime(),
                    end=b.to_pydatetime(),
                    label=f"{y}-{m:02d}",
                )
            )

        # 2) May M1 attempt (document)
        a, b = month_bounds(2026, 5)
        print("=== Attempt M1 2026-05 ===", flush=True)
        acquisition_log.append(
            fetch_range(
                client,
                symbols,
                interval="1m",
                start=a.to_pydatetime(),
                end=b.to_pydatetime(),
                label="2026-05",
            )
        )

        # 3) Extend forward: Sep (and any missing late Aug→now) M1+H1
        # Use chunked windows to stay within broker depth
        fwd_start = datetime(2026, 8, 25, tzinfo=timezone.utc)
        fwd_end = now + timedelta(hours=1)
        print("=== Extend M1 Aug25->now ===", flush=True)
        acquisition_log.append(
            fetch_range(
                client,
                symbols,
                interval="1m",
                start=fwd_start,
                end=fwd_end,
                label="forward_m1",
            )
        )
        print("=== Extend H1 Aug25->now ===", flush=True)
        acquisition_log.append(
            fetch_range(
                client,
                symbols,
                interval="1h",
                start=datetime(2026, 8, 1, tzinfo=timezone.utc),
                end=fwd_end,
                label="forward_h1",
            )
        )
    finally:
        client._connected = False
        client._mt5 = None
        print("Disconnected (no orders).", flush=True)

    print("Building coverage_after ...", flush=True)
    matrix_after = []
    for y, m in months:
        matrix_after.extend(month_stats(symbols, y, m))
    summary_after = coverage_summary(matrix_after)

    m1_after = snapshot_cache(symbols, "1m")
    preserv = compare_jun_aug_counts(baseline_rows, symbols)
    start_preserv = jun_aug_preserved(m1_before, m1_after, symbols)

    # Newly complete months outside excluded set
    newly = []
    for m, after in summary_after.items():
        y, mo = map(int, m.split("-"))
        if (y, mo) in EXCLUDED_EVAL_MONTHS:
            continue
        before = summary_before.get(m) or {}
        if after.get("complete_60_60") and not before.get("complete_60_60"):
            newly.append(m)
        elif after.get("complete_60_60") and before.get("complete_60_60"):
            pass

    complete_unseen = [
        m
        for m, s in summary_after.items()
        if s.get("complete_60_60")
        and tuple(map(int, m.split("-"))) not in EXCLUDED_EVAL_MONTHS
    ]

    # Next eligible: most recent complete unseen, else none
    next_eligible = None
    if complete_unseen:
        next_eligible = sorted(complete_unseen)[-1]

    # Write artifacts
    (out / "coverage_before.json").write_text(
        json.dumps(
            {
                "summary": summary_before,
                "matrix_n": len(matrix_before),
                "m1_snapshot": m1_before,
                "h1_snapshot_ends": {
                    s: h1_before[s].get("end") for s in symbols[:5]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "coverage_after.json").write_text(
        json.dumps(
            {
                "summary": summary_after,
                "matrix_n": len(matrix_after),
                "newly_complete_unseen": newly,
                "complete_unseen_months": complete_unseen,
                "next_eligible": next_eligible,
                "acquisition_log": acquisition_log,
                "broker_depth": depth,
                "integrity": {
                    "jun_aug_bar_counts_preserved": preserv,
                    "m1_start_preserved": start_preserv,
                    "source_mixing": False,
                    "vendor": "MT5_only",
                },
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    with (out / "coverage_matrix.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "month",
                "symbol",
                "H1_available",
                "M1_available",
                "H1_bars",
                "M1_bars",
                "H1_gaps",
                "M1_gaps",
                "valid",
            ],
        )
        w.writeheader()
        for r in matrix_after:
            w.writerow(
                {
                    "month": r["month"],
                    "symbol": r["symbol"],
                    "H1_available": r["H1_available"],
                    "M1_available": r["M1_available"],
                    "H1_bars": r["H1_bars"],
                    "M1_bars": r["M1_bars"],
                    "H1_gaps": r["H1_gaps"],
                    "M1_gaps": r["M1_gaps"],
                    "valid": r["valid"],
                }
            )

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "task": "mt5_historical_extension",
        "strategy_run": False,
        "orders_placed": False,
        "account": conn_meta,
        "universe_n": 60,
        "universe_sha256": uhash,
        "universe_source": str(JUL_INDEX),
        "provenance": "MT5 → data/cache/mt5/utc_v2",
        "normalization": "utc_v2 existing mt5_feed helpers",
        "out_dir": str(out),
        "next_eligible_oos_month": next_eligible,
        "newly_complete_unseen": newly,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Markdown report
    lines = [
        "# MT5 Historical Extension",
        "",
        f"Generated: {manifest['created_at']}",
        f"Commit: `{manifest['git_commit']}`",
        f"Account (read-only): login `{conn_meta['login']}` server `{conn_meta['server']}`",
        f"Orders placed: `{conn_meta['orders_placed']}`",
        "",
        "## Provenance",
        "",
        "- Source: MT5 `copy_rates` via existing `fetch_mt5_candles`",
        "- Cache: `data/cache/mt5/utc_v2`",
        "- No HistData / Yahoo / other vendor",
        f"- Universe: 60 pairs sha256=`{uhash}`",
        "",
        "## Broker depth (EURUSD probe)",
        "",
        "```json",
        json.dumps(depth, indent=2, default=str),
        "```",
        "",
        "## Coverage before",
        "",
        "```json",
        json.dumps(summary_before, indent=2),
        "```",
        "",
        "## Coverage after",
        "",
        "```json",
        json.dumps(summary_after, indent=2),
        "```",
        "",
        f"**Newly complete unseen months:** {newly or 'none'}",
        f"**Next eligible OOS month:** `{next_eligible}`",
        "",
        "## Integrity",
        "",
        f"- June–August M1 counts preserved: `{preserv['ok']}` (changes={preserv['changed_n']})",
        f"- M1 history start preserved: `{start_preserv['ok']}`",
        "- Source mixing: false",
        "",
        "## Note",
        "",
        "IC Markets MT5 M1 terminal depth is approximately 60 days. "
        "January–April 2026 M1 cannot be recovered from the live terminal once "
        "outside that window. September can be extended as the broker accumulates "
        "bars; a full calendar September requires coverage through 2026-10-01.",
        "",
        "No strategy backtest was run.",
    ]
    (out / "coverage_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Cleanup temp baseline marker (optional keep)
    print(f"\nNEXT_ELIGIBLE={next_eligible}", flush=True)
    print(f"NEWLY_COMPLETE={newly}", flush=True)
    print(f"Artifacts -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
