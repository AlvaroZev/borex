#!/usr/bin/env python3
"""AUTHORIZED Mirror data-only extension: utc_v2 H1+M1 for Jan–May 2026.

Safety:
- Mirror terminal only (MIRROR_MT5_PATH)
- Refuse Live login 52958398
- copy_rates / cache write only — NO orders
- NO September fetch / NO September research use
- NO parameter mining
- Preserve Jun–Aug M1 bar counts

After acquisition: write coverage + recommendation for baseline replay.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "borex_live"))

load_dotenv(ROOT.parent / "borex_live" / ".env")

from borex.viewerMT5.mt5_feed import _cache_path, fetch_mt5_candles  # noqa: E402

JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
LIVE_LOGIN = 52958398
MIN_M1 = 20_000
MIN_H1 = 400
TARGET_MONTHS = [(2026, m) for m in range(1, 6)]  # Jan–May only


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    except Exception:
        return "unknown"


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


def month_bounds(year: int, month: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year=year, month=month, day=1, tz="UTC")
    if month == 12:
        end = pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC")
    else:
        end = pd.Timestamp(year=year, month=month + 1, day=1, tz="UTC")
    return start, end


def connect_mirror_readonly():
    from borex_live.mt5.client import Mt5Client

    path = os.environ.get("MIRROR_MT5_PATH") or ""
    login = int(os.environ.get("MIRROR_MT5_LOGIN", "0") or 0)
    password = os.environ.get("MIRROR_MT5_PASSWORD", "")
    server = os.environ.get("MIRROR_MT5_DEMO_SERVER") or os.environ.get("MIRROR_MT5_SERVER", "")
    if not path:
        raise SystemExit("MIRROR_MT5_PATH not set")
    client = Mt5Client(path=path, login=login, password=password, server=server)
    client.connect()
    mt5 = client._mt5
    acc = mt5.account_info() if mt5 is not None else None
    got = int(getattr(acc, "login", 0) or 0)
    if got == LIVE_LOGIN:
        client.disconnect()
        raise SystemExit(f"Refusing Live account {LIVE_LOGIN}")
    if login and got != login:
        client.disconnect()
        raise SystemExit(f"Expected Mirror login {login}, got {got}")
    return client, {
        "path": path,
        "login": got,
        "server": str(getattr(acc, "server", "") or ""),
        "offset_seconds": int(getattr(client, "server_offset_seconds", 0) or 0),
        "orders_placed": False,
        "trade_apis_called": False,
    }


def snapshot_m1_junaug(symbols: list[str]) -> list[dict]:
    rows = []
    for sym in symbols:
        path = _cache_path(sym, "1m")
        if not path.is_file():
            rows.append({"symbol": sym, "jun": 0, "jul": 0, "aug": 0, "exists": False})
            continue
        ts = pd.to_datetime(pd.read_parquet(path, columns=["timestamp"])["timestamp"], utc=True)
        rows.append(
            {
                "symbol": sym,
                "exists": True,
                "jun": int(((ts >= "2026-06-01") & (ts < "2026-07-01")).sum()),
                "jul": int(((ts >= "2026-07-01") & (ts < "2026-08-01")).sum()),
                "aug": int(((ts >= "2026-08-01") & (ts < "2026-09-01")).sum()),
                "n": int(len(ts)),
                "start": str(ts.min()) if len(ts) else None,
                "end": str(ts.max()) if len(ts) else None,
            }
        )
    return rows


def compare_junaug(before: list[dict], after: list[dict]) -> dict:
    by_b = {r["symbol"]: r for r in before}
    issues = []
    for a in after:
        b = by_b.get(a["symbol"]) or {}
        for k in ("jun", "jul", "aug"):
            if int(a.get(k) or 0) != int(b.get(k) or 0):
                issues.append({"symbol": a["symbol"], "month": k, "before": b.get(k), "after": a.get(k)})
    return {"ok": len(issues) == 0, "changed_n": len(issues), "changes_sample": issues[:20]}


def month_coverage(symbols: list[str], year: int, month: int) -> dict:
    start, end = month_bounds(year, month)
    h1_ok = m1_ok = 0
    m1_counts = []
    h1_counts = []
    missing_m1 = []
    for sym in symbols:
        for tf, counts, ok_name in (("1h", h1_counts, "h1"), ("1m", m1_counts, "m1")):
            path = _cache_path(sym, tf)
            n = 0
            if path.is_file():
                ts = pd.to_datetime(pd.read_parquet(path, columns=["timestamp"])["timestamp"], utc=True)
                n = int(((ts >= start) & (ts < end)).sum())
            counts.append(n)
        if h1_counts[-1] >= MIN_H1:
            h1_ok += 1
        if m1_counts[-1] >= MIN_M1:
            m1_ok += 1
        else:
            missing_m1.append(f"{sym}:{m1_counts[-1]}")
    complete = h1_ok == 60 and m1_ok == 60
    return {
        "period": f"{year}-{month:02d}",
        "h1_ok": h1_ok,
        "m1_ok": m1_ok,
        "complete_60": complete,
        "classification": "A_FULLY_USABLE" if complete else ("C_H1_ONLY" if h1_ok == 60 and m1_ok == 0 else "B_PARTIAL_OR_INCOMPLETE"),
        "h1_median": int(pd.Series(h1_counts).median()) if h1_counts else 0,
        "m1_median": int(pd.Series(m1_counts).median()) if m1_counts else 0,
        "m1_min": int(min(m1_counts)) if m1_counts else 0,
        "missing_m1_sample": ";".join(missing_m1[:10]),
    }


def fetch_range(client, symbols, *, interval, start, end, label) -> dict:
    ok = 0
    fail = []
    acquired = []
    t0 = time.time()
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
            fail.append({"symbol": sym, "reason": str(exc)[:240]})
        if i % 5 == 0 or i == len(symbols):
            print(f"  [{label} {interval}] {i}/{len(symbols)} ok={ok} fail={len(fail)}", flush=True)
    return {
        "label": label,
        "interval": interval,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "ok_pairs": ok,
        "fail_n": len(fail),
        "failures": fail[:30],
        "acquired_sample": acquired[:3],
        "elapsed_s": round(time.time() - t0, 1),
    }


def probe_pre_june_depth(client) -> dict:
    """EURUSD depth probe for Jan–May only (no September)."""
    from borex.data.mt5_time import mt5_unix_to_utc, utc_to_mt5_request

    mt5 = client._mt5
    off = int(getattr(client, "server_offset_seconds", 0) or 0)
    sym = client.ensure_symbol("EURUSD=X")
    month_probes = {}
    for y, m in TARGET_MONTHS:
        a, b = month_bounds(y, m)
        rates = mt5.copy_rates_range(
            sym,
            client._tf("1m"),
            utc_to_mt5_request(a.to_pydatetime(), off),
            utc_to_mt5_request(b.to_pydatetime(), off),
        )
        n = 0 if rates is None else len(rates)
        first = last = None
        if n:
            first = str(mt5_unix_to_utc(int(rates[0]["time"]), off))
            last = str(mt5_unix_to_utc(int(rates[-1]["time"]), off))
        month_probes[f"{y}-{m:02d}"] = {"bars": n, "first": first, "last": last}
    return {"eurusd_m1_jan_may": month_probes}


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data/runs/research" / f"gate2_pre_june_m1_extension_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    jul = json.loads(JUL_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jul["spread_pips_by_symbol"].keys())
    assert len(symbols) == 60

    coverage_before = [month_coverage(symbols, y, m) for y, m in TARGET_MONTHS]
    # also Jun–Aug reference
    for y, m in [(2026, 6), (2026, 7), (2026, 8)]:
        coverage_before.append(month_coverage(symbols, y, m))

    baseline = snapshot_m1_junaug(symbols)
    (out / "junaug_m1_baseline_before.json").write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    (out / "coverage_before.json").write_text(json.dumps(coverage_before, indent=2), encoding="utf-8")

    client, conn_meta = connect_mirror_readonly()
    print(
        f"Connected Mirror login={conn_meta['login']} server={conn_meta['server']} "
        f"(historical copy_rates only; Live refused)",
        flush=True,
    )
    acquisition_log: list[dict] = []
    depth = {}
    try:
        depth = probe_pre_june_depth(client)
        print(f"Broker Jan-May M1 probe: {json.dumps(depth, indent=2)}", flush=True)

        # Fetch H1 then M1 for each target month (backward priority: May→Jan)
        for y, m in reversed(TARGET_MONTHS):
            a, b = month_bounds(y, m)
            print(f"=== H1 {y}-{m:02d} ===", flush=True)
            acquisition_log.append(
                fetch_range(client, symbols, interval="1h", start=a.to_pydatetime(), end=b.to_pydatetime(), label=f"{y}-{m:02d}")
            )
            print(f"=== M1 {y}-{m:02d} ===", flush=True)
            acquisition_log.append(
                fetch_range(client, symbols, interval="1m", start=a.to_pydatetime(), end=b.to_pydatetime(), label=f"{y}-{m:02d}")
            )
    finally:
        try:
            client.disconnect()
        except Exception:
            client._connected = False
            client._mt5 = None
        print("Disconnected (no orders).", flush=True)

    coverage_after = [month_coverage(symbols, y, m) for y, m in TARGET_MONTHS]
    for y, m in [(2026, 6), (2026, 7), (2026, 8)]:
        coverage_after.append(month_coverage(symbols, y, m))

    after_base = snapshot_m1_junaug(symbols)
    preserv = compare_junaug(baseline, after_base)

    newly_a = [
        r["period"]
        for r in coverage_after
        if r["period"].startswith("2026-0") and int(r["period"].split("-")[1]) <= 5 and r["complete_60"]
    ]
    before_a = {r["period"] for r in coverage_before if r.get("complete_60")}
    newly_complete = [p for p in newly_a if p not in before_a]

    report = {
        "git_commit": git_commit(),
        "universe_n": len(symbols),
        "universe_hash": universe_hash(symbols),
        "connection": conn_meta,
        "broker_depth_probe": depth,
        "coverage_before": coverage_before,
        "coverage_after": coverage_after,
        "newly_complete_pre_june": newly_complete,
        "pre_june_complete_months": newly_a,
        "junaug_preserved": preserv,
        "acquisition_log": acquisition_log,
        "safety": {
            "LIVE_TOUCHED": False,
            "MIRROR_DATA_ONLY": True,
            "ORDERS_PLACED": False,
            "SEPTEMBER_FETCHED": False,
            "SEP_OOS_LOCK_MODIFIED": False,
            "PARAMETER_MINING": False,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    (out / "extension_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "junaug_m1_baseline_after.json").write_text(json.dumps(after_base, indent=2), encoding="utf-8")
    (out / "coverage_after.json").write_text(json.dumps(coverage_after, indent=2), encoding="utf-8")

    with (out / "coverage_matrix.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(coverage_after[0].keys()))
        w.writeheader()
        for r in coverage_after:
            w.writerow(r)

    md = [
        "# Pre-June M1 Extension Report (AUTHORIZED Mirror data-only)",
        "",
        f"**Out:** `{out.name}`",
        f"**Mirror login:** `{conn_meta['login']}` server=`{conn_meta['server']}`",
        "",
        "## Safety",
        "",
        "- LIVE_TOUCHED = FALSE (refused 52958398)",
        "- MIRROR = data-only copy_rates",
        "- ORDERS_PLACED = FALSE",
        "- SEPTEMBER_FETCHED = FALSE",
        "- SEP_OOS_LOCK_MODIFIED = FALSE",
        "",
        "## Jun–Aug integrity",
        "",
        f"- Preserved: **{preserv['ok']}** (changed_n={preserv['changed_n']})",
        "",
        "## Coverage after",
        "",
        "| Period | H1 ok | M1 ok | Complete 60? | Class | M1 median |",
        "| --- | ---: | ---: | --- | --- | ---: |",
    ]
    for r in coverage_after:
        md.append(
            f"| {r['period']} | {r['h1_ok']} | {r['m1_ok']} | {r['complete_60']} | "
            f"{r['classification']} | {r['m1_median']} |"
        )
    md += [
        "",
        f"**Newly complete pre-June months:** {newly_complete or 'NONE'}",
        f"**All complete pre-June months now:** {newly_a or 'NONE'}",
        "",
        "Next: if any pre-June month is A_FULLY_USABLE, replay n1_control vs n1_thursday (no new knobs).",
        "",
    ]
    (out / "EXTENSION_REPORT.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"newly_complete": newly_complete, "pre_june_complete": newly_a, "junaug_ok": preserv["ok"]}, indent=2))
    print(f"WROTE {out}")
    return 0 if preserv["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
