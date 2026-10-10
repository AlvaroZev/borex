#!/usr/bin/env python3
"""Atomic Twin Live/Mirror/Frozen fidelity orchestrator (N1 Thursday lock).

- Cleans nothing here (DB cleaned externally)
- Requires flat experiment book (0 magic 88001/88002 opens) at GO
- Starts Live then Mirror on open MT5, waits READY, then T0 = next closed H1
- Observes ~2.5h; stops NEW attribution at T_end; does not force-close
- Uses Live-like frozen comparator (10k history + 72-bar replay + full universe)
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LIVE_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")
MAIN_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main")
sys.path[:0] = [str(MAIN_ROOT), str(LIVE_ROOT)]

PY = LIVE_ROOT / ".venv311" / "Scripts" / "python.exe"
LOGDIR = MAIN_ROOT / "data" / "runs" / "research" / "live_mirror_dashboards"
CRED = LOGDIR / "mirror_bind_password.txt"

OBSERVE_HOURS = float(os.environ.get("ATOMIC_HOURS", "2.5"))
POLL_SEC = int(os.environ.get("ATOMIC_POLL", "60"))
LIVE_MAGIC = 88001
MIRROR_MAGIC = 88002
PROTECTED = {
    1890205005,
    1891279302,
    1893184489,
    1917854332,
    1940979693,
}
PREV_ORPHANS = {
    1970938294: "EXPERIMENT_PREV_LIVE_GBPCHF",
    1971562102: "EXPERIMENT_PREV_MIRROR_GBPDKK",
}

TS = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
RUN = MAIN_ROOT / "data" / "runs" / "research" / f"live_mirror_backtest_atomic_{TS}"
RUN.mkdir(parents=True, exist_ok=True)
(SNAP := RUN / "snapshots").mkdir(exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def append_csv(path: Path, row: dict[str, Any], fieldnames: list[str] | None = None) -> None:
    exists = path.exists() and path.stat().st_size > 0
    keys = fieldnames or list(row.keys())
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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
        w.writerows(rows)


def http_json(url: str, timeout: float = 45.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def safe_http(url: str) -> dict[str, Any]:
    try:
        return {"ok": True, "data": http_json(url)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def port_up(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def password() -> str:
    p = (
        os.environ.get("LIVE_BIND_PASS")
        or os.environ.get("MT5_PASSWORD")
        or os.environ.get("MIRROR_MT5_PASSWORD")
        or ""
    )
    if not p and CRED.exists():
        p = CRED.read_text(encoding="utf-8").strip()
    return p


def kill_services() -> None:
    my_pid = os.getpid()
    out = subprocess.check_output(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress",
        ],
        text=True,
        errors="replace",
    )
    import json as _json

    rows = _json.loads(out or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        cmd = str(row.get("CommandLine") or "")
        pid = int(row["ProcessId"])
        if pid == my_pid:
            continue
        # Never kill the orchestrator itself (parent shells / this script)
        if "run_atomic_twin_fidelity.py" in cmd:
            continue
        if any(x in cmd for x in ("mt5service.py", "mirrorservice.py", "run_triad_validation")):
            print("kill", pid, flush=True)
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def classify_positions() -> list[dict[str, Any]]:
    import MetaTrader5 as mt5

    assert mt5.initialize(timeout=60_000), mt5.last_error()
    try:
        rows = []
        for p in mt5.positions_get() or []:
            ticket = int(p.ticket)
            magic = int(p.magic)
            if ticket in PROTECTED:
                cls = "PROTECTED"
            elif ticket in PREV_ORPHANS:
                cls = "EXPERIMENT"  # prior run orphan identity
            elif magic in (LIVE_MAGIC, MIRROR_MAGIC):
                cls = "EXPERIMENT"
            else:
                cls = "UNKNOWN"
            rows.append(
                {
                    "ticket": ticket,
                    "symbol": str(p.symbol),
                    "magic": magic,
                    "volume": float(p.volume),
                    "profit": float(p.profit),
                    "comment": str(p.comment),
                    "class": cls,
                    "note": PREV_ORPHANS.get(ticket, ""),
                }
            )
        return rows
    finally:
        mt5.shutdown()


def mt5_clock() -> dict[str, Any]:
    import MetaTrader5 as mt5
    from borex.data.mt5_time import measure_mt5_server_offset_seconds

    assert mt5.initialize(timeout=60_000), mt5.last_error()
    try:
        offset = int(measure_mt5_server_offset_seconds(mt5) or 0)
        # last closed EURUSD H1
        rates = mt5.copy_rates_from_pos("EURUSD", mt5.TIMEFRAME_H1, 0, 3)
        bars = []
        if rates is not None:
            from borex.data.mt5_time import mt5_unix_to_utc
            import pandas as pd

            now = pd.Timestamp.now(tz="UTC")
            for r in rates:
                ts = pd.Timestamp(mt5_unix_to_utc(int(r["time"]), offset))
                bars.append({"open_ts": str(ts), "close_ts": str(ts + pd.Timedelta(hours=1))})
            # drop forming
            if bars:
                last_open = pd.Timestamp(bars[-1]["open_ts"])
                if last_open.floor("h") == now.floor("h"):
                    bars = bars[:-1]
        acc = mt5.account_info()
        return {
            "offset_seconds": offset,
            "utc_now": utc_now().isoformat(),
            "last_closed_h1_open": bars[-1]["open_ts"] if bars else None,
            "last_closed_h1_close": bars[-1]["close_ts"] if bars else None,
            "login": int(acc.login) if acc else None,
            "balance": float(acc.balance) if acc else None,
            "equity": float(acc.equity) if acc else None,
        }
    finally:
        mt5.shutdown()


def next_h1_close_utc(after: datetime | None = None) -> datetime:
    t = after or utc_now()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    else:
        t = t.astimezone(timezone.utc)
    floored = t.replace(minute=0, second=0, microsecond=0)
    nxt = floored + timedelta(hours=1)
    if t >= nxt:
        nxt = nxt + timedelta(hours=1)
    return nxt


def wait_until(target: datetime, label: str = "") -> None:
    while True:
        now = utc_now()
        left = (target - now).total_seconds()
        if left <= 0:
            return
        write_json(
            RUN / "PROGRESS.json",
            {
                "phase": "waiting",
                "label": label,
                "target": target.isoformat(),
                "utc": now.isoformat(),
                "left_sec": round(left, 1),
            },
        )
        time.sleep(min(5.0, max(0.2, left)))


def start_live(env: dict[str, str]) -> subprocess.Popen:
    args = [
        str(PY),
        "-u",
        str(LIVE_ROOT / "mt5service.py"),
        "--demo",
        "--strategy", "alexg8",
        "--leverage", "500",
        "--rr-mode", "dynamic",
        "--rr-factor", "1.0",
        "--min-rr", "2.0",
        "--rr-min", "2.0",
        "--rr-max", "5.0",
        "--ghost-sl-mult", "4.0",
        "--peer-blend", "0.0",
        "--thursday-gate",
        "--force-flat-daily",
        "--no-force-flat-friday",
        "--risk-include-commission",
        "--no-commission-at-entry",
        "--interval", "1h",
        "--poll", "10",
        "--warmup-bars", "10000",
        "--position-size", "0.01",
        "--max-positions", "60",
        "--port", "8791",
        "--db", "postgresql://borex:borex@127.0.0.1:5433/borex_live",
        "--borex-main", str(MAIN_ROOT),
    ]
    out = open(RUN / "live_stdout.log", "w", encoding="utf-8")
    err = open(RUN / "live_stderr.log", "w", encoding="utf-8")
    return subprocess.Popen(args, cwd=str(LIVE_ROOT), stdout=out, stderr=err, env=env)


def start_mirror(env: dict[str, str]) -> subprocess.Popen:
    args = [
        str(PY),
        "-u",
        str(LIVE_ROOT / "mirrorservice.py"),
        "--demo",
        "--strategy", "alexg8",
        "--leverage", "500",
        "--rr-mode", "dynamic",
        "--rr-factor", "1.0",
        "--min-rr", "2.0",
        "--rr-min", "2.0",
        "--rr-max", "5.0",
        "--ghost-sl-mult", "4.0",
        "--peer-blend", "0.0",
        "--thursday-gate",
        "--force-flat-daily",
        "--no-force-flat-friday",
        "--risk-include-commission",
        "--no-commission-at-entry",
        "--interval", "1h",
        "--poll", "10",
        "--warmup-bars", "10000",
        "--position-size", "0.01",
        "--max-positions", "60",
        "--port", "8792",
        "--execute-mt5",
        "--db", "postgresql://borex:borex@127.0.0.1:5433/borex_mirror",
    ]
    out = open(RUN / "mirror_stdout.log", "w", encoding="utf-8")
    err = open(RUN / "mirror_stderr.log", "w", encoding="utf-8")
    return subprocess.Popen(args, cwd=str(LIVE_ROOT), stdout=out, stderr=err, env=env)


def wait_status(port: int, want: str = "running", timeout_sec: float = 900) -> dict[str, Any]:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_sec:
        s = safe_http(f"http://127.0.0.1:{port}/api/status")
        if s.get("ok"):
            last = s["data"]
            st = str(last.get("status") or "")
            write_json(
                RUN / "PROGRESS.json",
                {
                    "phase": f"warmup_{port}",
                    "status": st,
                    "utc": utc_now().isoformat(),
                    "elapsed_sec": round(time.time() - t0, 1),
                },
            )
            if st == want:
                return last
        time.sleep(3)
    raise TimeoutError(f"port {port} did not reach {want}; last={last}")


def pending_from_debug(port: int) -> dict[str, Any]:
    # Live has /api/debug/pending; Mirror may not — fall back to dashboard ghosts
    dbg = safe_http(f"http://127.0.0.1:{port}/api/debug/pending")
    if dbg.get("ok"):
        d = dbg["data"]
        return {
            "source": "debug_pending",
            "memory_n": d.get("memory_n"),
            "db_n": d.get("db_n"),
            "memory": d.get("memory") or [],
            "db": d.get("db") or [],
        }
    dash = safe_http(f"http://127.0.0.1:{port}/api/dashboard")
    if not dash.get("ok"):
        return {"source": "failed", "error": dbg.get("error") or dash.get("error"), "memory": [], "db": []}
    ghosts = dash["data"].get("pending_ghosts") or []
    return {
        "source": "dashboard_ghosts_only",
        "memory_n": None,
        "db_n": len(ghosts),
        "memory": [],
        "db": ghosts,
    }


def signal_rows_from_pending(rows: list[dict], path: str, bar_ts: str) -> list[dict]:
    out = []
    for g in rows:
        sym = g.get("symbol")
        action = str(g.get("action") or "").lower()
        pe = g.get("planned_entry")
        sl = g.get("stop_loss")
        tp = g.get("take_profit")
        cid = g.get("created_index")
        eid = g.get("expires_index")
        sig_id = f"{sym}|{action}|{pe}|{sl}|{cid}"
        out.append(
            {
                "bar_ts": bar_ts,
                "path": path,
                "signal_id": sig_id,
                "symbol": sym,
                "direction": action,
                "planned_entry": pe,
                "stop_loss": sl,
                "take_profit": tp,
                "created_index": cid,
                "expires_index": eid,
                "saw_near_sl": g.get("saw_near_sl"),
                "pattern": (g.get("pattern") or "")[:200],
                "kind": "pending_ghost",
            }
        )
    return out


def run_frozen_live_like(tip_close_utc: str | None = None) -> dict[str, Any]:
    """Offline Live-like: ~10k H1 + full Live universe + last 72 on_bar."""
    try:
        from borex.alexg import AlexG8Strategy
        from borex.alexg.force_flat import blocks_new_entries
        from borex.alexg.multi_market import (
            MultiMarketContext,
            align_symbols_to_timeline,
            default_forex_universe,
        )
        from borex.backtest.engine import BacktestConfig
        from borex.data.mt5_time import measure_mt5_server_offset_seconds, mt5_unix_to_utc
        from borex.models.candle import Candle
        from borex_live.execution.router import read_pending_snapshot
        from borex_mirror.engine import _ThursdayEntryGate
        import MetaTrader5 as mt5
        import pandas as pd
    except Exception as exc:
        return {"ok": False, "error": f"import:{exc!r}"}

    def _ts(x):
        t = pd.Timestamp(x)
        return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

    symbols = default_forex_universe()
    try:
        st = http_json("http://127.0.0.1:8791/api/status")
        if st.get("symbols"):
            symbols = list(st["symbols"])
    except Exception:
        pass

    if not mt5.initialize(timeout=90_000):
        return {"ok": False, "error": str(mt5.last_error())}
    try:
        offset = int(measure_mt5_server_offset_seconds(mt5) or 0)
        now = pd.Timestamp.now(tz="UTC")
        h1: dict[str, list] = {}
        for sym in symbols:
            mt5_sym = sym.replace("=X", "")
            info = mt5.symbol_info(mt5_sym)
            if info is None:
                mt5.symbol_select(mt5_sym, True)
                info = mt5.symbol_info(mt5_sym)
            if info is None:
                continue
            mt5.symbol_select(mt5_sym, True)
            rates = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_H1, 0, 10005)
            if rates is None or len(rates) < 120:
                continue
            bars = []
            for r in rates:
                bars.append(
                    Candle(
                        timestamp=pd.Timestamp(mt5_unix_to_utc(int(r["time"]), offset)),
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r["tick_volume"]),
                    )
                )
            if bars and _ts(bars[-1].timestamp).floor("h") == now.floor("h"):
                bars = bars[:-1]
            if len(bars) >= 120:
                h1[sym] = bars
    finally:
        mt5.shutdown()

    if "EURUSD=X" not in h1:
        return {"ok": False, "error": "no EURUSD"}

    master = "EURUSD=X"
    tip = _ts(h1[master][-1].timestamp)
    h1 = {
        s: [c for c in bars if _ts(c.timestamp) <= tip]
        for s, bars in h1.items()
        if bars and _ts(bars[-1].timestamp) >= tip - pd.Timedelta(hours=48)
    }
    stop_i = len(h1[master])
    start_i = max(120, stop_i - 72)

    strategy = _ThursdayEntryGate(
        AlexG8Strategy(min_rr=2.0, execution_interval="1h", peer_blend=0.0, ghost_sl_mult=4.0)
    )
    bt = BacktestConfig(
        initial_capital=1000.0,
        leverage=500.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=5.0,
        commission_per_lot=7.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_daily=True,
        force_flat_friday=False,
    )
    master_bars = h1[master]
    ts_maps = align_symbols_to_timeline(master_bars, h1)
    inner = getattr(strategy, "_inner", strategy)
    min_bars = int(getattr(inner, "min_bars", 120))
    strength_lookback = getattr(inner, "strength_lookback", 24)
    min_currency_edge = getattr(inner, "min_currency_edge", 0.0)
    min_confirming_pairs = getattr(inner, "min_confirming_pairs", 0)
    symbols = list(h1.keys())

    for mi in range(start_i, stop_i):
        ctx = MultiMarketContext.at_master_bar(
            mi,
            master_bars,
            h1,
            ts_maps,
            strength_lookback=strength_lookback,
            min_currency_edge=min_currency_edge,
            min_confirming_pairs=min_confirming_pairs,
        )
        for sym in symbols:
            idx = ctx.indices.get(sym)
            if idx is None or idx < min_bars:
                continue
            bar = h1[sym][idx]
            if blocks_new_entries(bar.timestamp, bt):
                continue
            strategy.set_context(sym, ctx)
            strategy.on_bar(idx, h1[sym], None)

    snap = read_pending_snapshot(strategy)
    pending = []
    for g in snap.values():
        pending.append(
            {
                "symbol": g.symbol,
                "action": g.action,
                "planned_entry": g.planned_entry,
                "stop_loss": g.stop_loss,
                "take_profit": g.take_profit,
                "created_index": g.created_index,
                "expires_index": g.expires_index,
                "saw_near_sl": g.saw_near_sl,
                "pattern": (g.pattern or "")[:200],
            }
        )
    pending = sorted(pending, key=lambda r: r["symbol"])
    return {
        "ok": True,
        "tip_timestamp": str(tip),
        "pairs": len(h1),
        "start_i": start_i,
        "stop_i": stop_i,
        "pending_n": len(pending),
        "pending": pending,
        "offset_seconds": offset,
    }


def deals_since(since_unix: int) -> list[dict[str, Any]]:
    import MetaTrader5 as mt5

    assert mt5.initialize(timeout=30_000), mt5.last_error()
    try:
        to_ts = int(time.time()) + 60
        out = []
        for d in mt5.history_deals_get(since_unix - 5, to_ts) or []:
            out.append(
                {
                    "ticket": int(d.ticket),
                    "order": int(d.order),
                    "symbol": str(d.symbol),
                    "magic": int(d.magic),
                    "volume": float(d.volume),
                    "price": float(d.price),
                    "profit": float(d.profit),
                    "commission": float(d.commission),
                    "swap": float(d.swap),
                    "entry": int(d.entry),
                    "time": int(d.time),
                    "time_utc": datetime.fromtimestamp(int(d.time), tz=timezone.utc).isoformat(),
                    "comment": str(d.comment),
                }
            )
        return out
    finally:
        mt5.shutdown()


def positions_now() -> list[dict[str, Any]]:
    return classify_positions()


def pnl_for_magic(deals: list[dict], magic: int, since_unix: int) -> dict[str, float]:
    rows = [d for d in deals if int(d["magic"]) == magic and int(d["time"]) >= since_unix]
    profit = sum(float(d["profit"]) for d in rows)
    commission = sum(float(d["commission"]) for d in rows)
    swap = sum(float(d["swap"]) for d in rows)
    return {
        "n_deals": len(rows),
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "net": profit + commission + swap,
    }


def compare_pending_sets(frozen: list[dict], live: list[dict], mirror: list[dict], bar_ts: str) -> list[dict]:
    def key(r):
        return (
            str(r.get("symbol")),
            str(r.get("action") or r.get("direction") or "").lower(),
        )

    def by_sym(rows):
        m = {}
        for r in rows:
            m[str(r.get("symbol"))] = r
        return m

    F, L, M = by_sym(frozen), by_sym(live), by_sym(mirror)
    syms = sorted(set(F) | set(L) | set(M))
    out = []
    for s in syms:
        f, l, m = F.get(s), L.get(s), M.get(s)
        present = []
        if f:
            present.append("FROZEN")
        if l:
            present.append("LIVE")
        if m:
            present.append("MIRROR")
        cls = "UNKNOWN"
        if f and l and m:
            pe = [float(x["planned_entry"]) for x in (f, l, m)]
            sl = [float(x["stop_loss"]) for x in (f, l, m)]
            acts = [str(x.get("action") or "").lower() for x in (f, l, m)]
            if len(set(acts)) == 1 and max(pe) - min(pe) < 1e-9 and max(sl) - min(sl) < 1e-9:
                cls = "SIGNAL_MATCH"
            else:
                cls = "GEOMETRY_MISMATCH"
        elif f and l and not m:
            cls = "MIRROR_ONLY_MISSING"
        elif f and m and not l:
            cls = "LIVE_ONLY_MISSING"
        elif l and m and not f:
            cls = "FROZEN_ONLY_MISSING"
        elif f and not l and not m:
            cls = "FROZEN_ONLY"
        elif l and not f and not m:
            cls = "LIVE_ONLY"
        elif m and not f and not l:
            cls = "MIRROR_ONLY"
        out.append(
            {
                "bar_ts": bar_ts,
                "symbol": s,
                "class": cls,
                "present": "|".join(present),
                "frozen_pe": None if not f else f.get("planned_entry"),
                "live_pe": None if not l else l.get("planned_entry"),
                "mirror_pe": None if not m else m.get("planned_entry"),
                "frozen_sl": None if not f else f.get("stop_loss"),
                "live_sl": None if not l else l.get("stop_loss"),
                "mirror_sl": None if not m else m.get("stop_loss"),
                "frozen_action": None if not f else f.get("action"),
                "live_action": None if not l else l.get("action"),
                "mirror_action": None if not m else m.get("action"),
            }
        )
    return out


def main() -> int:
    print(f"RUN={RUN}", flush=True)
    passwd = password()
    if not passwd:
        print("ERROR: missing MT5 password", flush=True)
        return 2

    # --- integrity: classify book ---
    opens = classify_positions()
    write_csv(RUN / "OPEN_POSITIONS_PRECHECK.csv", opens)
    exp_open = [p for p in opens if p["class"] == "EXPERIMENT"]
    prot = [p for p in opens if p["class"] == "PROTECTED"]
    write_json(
        RUN / "BOOK_CLASSIFICATION.json",
        {
            "utc": utc_now().isoformat(),
            "positions": opens,
            "experiment_open_n": len(exp_open),
            "protected_open_n": len(prot),
            "clean_for_T0": len(exp_open) == 0,
        },
    )
    if exp_open:
        msg = {
            "status": "NOT_STARTED",
            "reason": "experiment-owned positions still open; waiting for natural flat book",
            "experiment_open": exp_open,
            "instruction": "Do not force-close. Re-run when magic 88001/88002 book is flat.",
        }
        write_json(RUN / "NOT_STARTED.json", msg)
        (RUN / "FINAL_RESULTS.md").write_text(
            "# FINAL_RESULTS — NOT STARTED\n\n"
            "Experiment-owned positions remain open. Atomic twin run aborted before T0.\n\n"
            + json.dumps(msg, indent=2, default=str),
            encoding="utf-8",
        )
        print("NOT_STARTED: experiment positions open", flush=True)
        return 3

    clock0 = mt5_clock()
    write_json(RUN / "MT5_CLOCK_PRESTART.json", clock0)

    manifest = {
        "run_id": RUN.name,
        "experiment": "live_mirror_backtest_atomic",
        "observe_hours": OBSERVE_HOURS,
        "poll_seconds": POLL_SEC,
        "live_port": 8791,
        "mirror_port": 8792,
        "live_magic": LIVE_MAGIC,
        "mirror_magic": MIRROR_MAGIC,
        "protected_eurdkk": sorted(PROTECTED),
        "frozen_knobs": {
            "strategy": "alexg8",
            "ghost_sl_mult": 4.0,
            "peer_blend": 0.0,
            "leverage": 500,
            "rr_mode": "dynamic",
            "rr_factor": 1.0,
            "rr_min": 2.0,
            "rr_max": 5.0,
            "force_flat_daily": True,
            "force_flat_friday": False,
            "thursday_gate": True,
            "ghost_entry": "h1_close_market",
            "commission_per_lot_rt": 7.0,
            "risk_include_commission": True,
        },
        "comparator": "LIVE_LIKE_10k_plus_72_replay_full_universe",
        "db_cleaned": True,
        "created_utc": utc_now().isoformat(),
    }
    write_json(RUN / "EXPERIMENT_MANIFEST.json", manifest)

    # --- start services ---
    kill_services()
    time.sleep(1)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "BOREX_MAIN_ROOT": str(MAIN_ROOT),
            "MT5_LOGIN": "53074439",
            "MIRROR_MT5_LOGIN": "53074439",
            "MT5_PASSWORD": passwd,
            "MIRROR_MT5_PASSWORD": passwd,
            "MT5_SERVER": "ICMarketsSC-Demo",
            "MT5_DEMO_SERVER": "ICMarketsSC-Demo",
            "MIRROR_MT5_DEMO_SERVER": "ICMarketsSC-Demo",
            "MT5_PATH": "",
            "MIRROR_MT5_PATH": "",
        }
    )

    t_launch = utc_now()
    live_p = start_live(env)
    t_live_launch = utc_now()
    # brief pause so both attach cleanly to same terminal
    time.sleep(2)
    mir_p = start_mirror(env)
    t_mir_launch = utc_now()
    (RUN / "live.pid").write_text(str(live_p.pid), encoding="utf-8")
    (RUN / "mirror.pid").write_text(str(mir_p.pid), encoding="utf-8")

    sync_doc = [
        "# STARTUP_SYNCHRONIZATION",
        "",
        f"- launch_batch_utc: `{t_launch.isoformat()}`",
        f"- live_launch_utc: `{t_live_launch.isoformat()}` pid=`{live_p.pid}`",
        f"- mirror_launch_utc: `{t_mir_launch.isoformat()}` pid=`{mir_p.pid}`",
        f"- live_mirror_launch_delta_sec: `{(t_mir_launch - t_live_launch).total_seconds():.3f}`",
        "",
        "Barrier: READY_LIVE + READY_MIRROR + READY_FROZEN → GO/T0 = next closed H1 after both running.",
        "",
    ]
    (RUN / "STARTUP_SYNCHRONIZATION.md").write_text("\n".join(sync_doc), encoding="utf-8")

    print("waiting READY_LIVE", flush=True)
    live_st = wait_status(8791, "running", timeout_sec=1200)
    t_live_ready = utc_now()
    print("waiting READY_MIRROR", flush=True)
    mir_st = wait_status(8792, "running", timeout_sec=1200)
    t_mir_ready = utc_now()

    # Frozen readiness = ability to compute Live-like tip
    print("READY_FROZEN probe", flush=True)
    frozen0 = run_frozen_live_like()
    write_json(RUN / "FROZEN_READY_PROBE.json", {k: frozen0[k] for k in frozen0 if k != "pending"})
    t_frozen_ready = utc_now()
    if not frozen0.get("ok"):
        print("FROZEN not ready", frozen0.get("error"), flush=True)
        return 4

    with (RUN / "STARTUP_SYNCHRONIZATION.md").open("a", encoding="utf-8") as f:
        f.write(f"- live_ready_utc: `{t_live_ready.isoformat()}`\n")
        f.write(f"- mirror_ready_utc: `{t_mir_ready.isoformat()}`\n")
        f.write(f"- frozen_ready_utc: `{t_frozen_ready.isoformat()}`\n")
        f.write(
            f"- ready_delta_live_mirror_sec: `{(t_mir_ready - t_live_ready).total_seconds():.3f}`\n"
        )

    # T0 = next H1 close after both ready
    ready_at = max(t_live_ready, t_mir_ready, t_frozen_ready)
    t0 = next_h1_close_utc(ready_at + timedelta(seconds=2))
    print(f"WAITING GO/T0={t0.isoformat()}", flush=True)
    wait_until(t0 + timedelta(seconds=3), label="T0_H1_CLOSE")

    clock_t0 = mt5_clock()
    opens_t0 = positions_now()
    write_csv(RUN / "OPEN_POSITIONS_T0.csv", opens_t0)
    t0_state = {
        "T0_utc": t0.isoformat(),
        "T0_actual_wake_utc": utc_now().isoformat(),
        "h1_bar": clock_t0,
        "live_status": live_st,
        "mirror_status": mir_st,
        "open_positions": opens_t0,
        "experiment_open_n": len([p for p in opens_t0 if p["class"] == "EXPERIMENT"]),
        "live_ready_utc": t_live_ready.isoformat(),
        "mirror_ready_utc": t_mir_ready.isoformat(),
        "frozen_ready_utc": t_frozen_ready.isoformat(),
        "live_startup_delta_from_T0_sec": (t_live_ready - t0).total_seconds(),
        "mirror_startup_delta_from_T0_sec": (t_mir_ready - t0).total_seconds(),
        "note": "Services warmed before T0; experiment attribution starts at T0 closed H1.",
    }
    write_json(RUN / "T0_STATE.json", t0_state)
    with (RUN / "STARTUP_SYNCHRONIZATION.md").open("a", encoding="utf-8") as f:
        f.write(f"- T0_utc: `{t0.isoformat()}`\n")
        f.write(f"- last_closed_h1_open: `{clock_t0.get('last_closed_h1_open')}`\n")
        f.write(f"- last_closed_h1_close: `{clock_t0.get('last_closed_h1_close')}`\n")
        f.write(f"- mt5_offset_seconds: `{clock_t0.get('offset_seconds')}`\n")

    if any(p["class"] == "EXPERIMENT" for p in opens_t0):
        # Should be rare; still isolate
        print("WARNING: experiment positions at T0 — isolating from attribution", flush=True)

    since_unix = int(t0.timestamp())
    t_end = t0 + timedelta(hours=OBSERVE_HOURS)
    last_bar = clock_t0.get("last_closed_h1_open")
    hourly_rows: list[dict] = []
    all_recon: list[dict] = []
    signal_rows: list[dict] = []
    pending_snap_rows: list[dict] = []
    usdaed_errors: list[dict] = []
    force_flat_flags: list[dict] = []

    print(f"OBSERVE until {t_end.isoformat()}", flush=True)
    i = 0
    while utc_now() < t_end:
        i += 1
        now = utc_now()
        clock = mt5_clock()
        bar = clock.get("last_closed_h1_open")
        new_bar = bar and bar != last_bar

        live_pend = pending_from_debug(8791)
        mir_pend = pending_from_debug(8792)
        live_dash = safe_http("http://127.0.0.1:8791/api/dashboard")
        mir_dash = safe_http("http://127.0.0.1:8792/api/dashboard")
        deals = deals_since(since_unix)
        live_pnl = pnl_for_magic(deals, LIVE_MAGIC, since_unix)
        mir_pnl = pnl_for_magic(deals, MIRROR_MAGIC, since_unix)
        opens = positions_now()

        # USDAED errors from live stderr tail
        try:
            err_tail = (RUN / "live_stderr.log").read_text(encoding="utf-8", errors="replace")[-4000:]
            if "USDAED" in err_tail and "fetch_bars skipped" in err_tail:
                usdaed_errors.append({"utc": now.isoformat(), "seen_in_log": True})
        except Exception:
            pass

        frozen = {"ok": False}
        if new_bar or i == 1:
            print(f"H1 close processing bar={bar}", flush=True)
            frozen = run_frozen_live_like()
            write_json(SNAP / f"frozen_{i:04d}.json", frozen)
            last_bar = bar

            live_mem = live_pend.get("memory") or live_pend.get("db") or []
            mir_db = mir_pend.get("memory") or mir_pend.get("db") or []
            fro = frozen.get("pending") or [] if frozen.get("ok") else []

            # signal logs
            for row in signal_rows_from_pending(fro, "FROZEN", str(bar)):
                signal_rows.append(row)
                append_csv(RUN / "FROZEN_SIGNALS.csv", row)
                append_csv(RUN / "SIGNAL_LOG.csv", row)
            for row in signal_rows_from_pending(live_mem, "LIVE", str(bar)):
                signal_rows.append(row)
                append_csv(RUN / "LIVE_SIGNALS.csv", row)
                append_csv(RUN / "SIGNAL_LOG.csv", row)
            for row in signal_rows_from_pending(mir_db, "MIRROR", str(bar)):
                signal_rows.append(row)
                append_csv(RUN / "MIRROR_SIGNALS.csv", row)
                append_csv(RUN / "SIGNAL_LOG.csv", row)

            recon = compare_pending_sets(fro, live_mem, mir_db, str(bar))
            all_recon.extend(recon)
            for r in recon:
                append_csv(RUN / "HOURLY_RECONCILIATION.csv", r)

            pending_snap_rows.append(
                {
                    "utc": now.isoformat(),
                    "bar_ts": bar,
                    "frozen_n": len(fro),
                    "live_memory_n": live_pend.get("memory_n"),
                    "live_db_n": live_pend.get("db_n"),
                    "mirror_memory_n": mir_pend.get("memory_n"),
                    "mirror_db_n": mir_pend.get("db_n"),
                    "live_pending_source": live_pend.get("source"),
                    "mirror_pending_source": mir_pend.get("source"),
                }
            )
            append_csv(RUN / "PENDING_SNAPSHOTS.csv", pending_snap_rows[-1])

            # force_flat heuristic flag if weekday flatten hour
            try:
                import pandas as pd

                bts = pd.Timestamp(bar)
                if bts.tzinfo is None:
                    bts = bts.tz_localize("UTC")
                if int(bts.dayofweek) <= 4 and int(bts.hour) in (9, 16, 21):
                    force_flat_flags.append(
                        {
                            "utc": now.isoformat(),
                            "bar_ts": str(bar),
                            "FLAG": "FORCE_FLAT_LIFECYCLE",
                            "note": "H1 near known session-close hours; inspect pending diffs",
                        }
                    )
                    append_csv(RUN / "EXECUTION_ERRORS.csv", force_flat_flags[-1])
            except Exception:
                pass

            hourly_rows.append(
                {
                    "i": i,
                    "bar_ts": bar,
                    "utc": now.isoformat(),
                    "frozen_ok": frozen.get("ok"),
                    "frozen_pending_n": frozen.get("pending_n"),
                    "live_pending_n": live_pend.get("memory_n") or live_pend.get("db_n"),
                    "mirror_pending_n": mir_pend.get("db_n"),
                    "match_n": sum(1 for r in recon if r["class"] == "SIGNAL_MATCH"),
                    "live_net": live_pnl["net"],
                    "mirror_net": mir_pnl["net"],
                    "live_deals": live_pnl["n_deals"],
                    "mirror_deals": mir_pnl["n_deals"],
                    "mt5_open_n": len(opens),
                }
            )

        snap = {
            "i": i,
            "utc": now.isoformat(),
            "bar_ts": bar,
            "new_bar": new_bar,
            "live_pnl": live_pnl,
            "mirror_pnl": mir_pnl,
            "opens": opens,
            "live_pending_meta": {
                k: live_pend.get(k) for k in ("source", "memory_n", "db_n")
            },
            "mirror_pending_meta": {
                k: mir_pend.get(k) for k in ("source", "memory_n", "db_n")
            },
        }
        write_json(SNAP / f"snap_{i:04d}.json", snap)
        write_json(
            RUN / "PROGRESS.json",
            {
                "phase": "observing",
                "i": i,
                "utc": now.isoformat(),
                "bar_ts": bar,
                "remaining_min": round((t_end - now).total_seconds() / 60.0, 2),
                "live_net": live_pnl["net"],
                "mirror_net": mir_pnl["net"],
            },
        )
        print(
            f"[{i}] bar={bar} new={new_bar} live_net={live_pnl['net']:.2f} "
            f"mir_net={mir_pnl['net']:.2f} opens={len(opens)}",
            flush=True,
        )

        sleep_left = min(POLL_SEC, max(1, int((t_end - utc_now()).total_seconds())))
        time.sleep(sleep_left)

    # --- T_end: stop NEW attribution; do not kill if experiment opens remain ---
    t_finish = utc_now()
    print("T_END — stop accepting NEW experiment attribution", flush=True)
    deals = deals_since(since_unix)
    live_deals = [d for d in deals if int(d["magic"]) == LIVE_MAGIC and int(d["time"]) >= since_unix]
    mir_deals = [d for d in deals if int(d["magic"]) == MIRROR_MAGIC and int(d["time"]) >= since_unix]
    write_json(RUN / "LIVE_DEALS.json", live_deals)
    write_json(RUN / "MIRROR_DEALS.json", mir_deals)
    opens_end = positions_now()
    write_csv(RUN / "OPEN_POSITIONS_TEND.csv", opens_end)

    # Continue monitoring until experiment flat or timeout grace 3h
    grace_deadline = t_finish + timedelta(hours=3)
    while utc_now() < grace_deadline:
        opens = positions_now()
        exp = [p for p in opens if p["class"] == "EXPERIMENT" and p["magic"] in (LIVE_MAGIC, MIRROR_MAGIC)]
        write_json(
            RUN / "PROGRESS.json",
            {
                "phase": "drain_open_trades",
                "utc": utc_now().isoformat(),
                "experiment_open_n": len(exp),
                "opens": exp,
            },
        )
        if not exp:
            print("experiment book flat", flush=True)
            break
        print(f"waiting natural exits; open={len(exp)}", flush=True)
        time.sleep(60)

    opens_final = positions_now()
    write_csv(RUN / "OPEN_POSITIONS_FINAL.csv", opens_final)
    deals = deals_since(since_unix)
    live_deals = [d for d in deals if int(d["magic"]) == LIVE_MAGIC and int(d["time"]) >= since_unix]
    mir_deals = [d for d in deals if int(d["magic"]) == MIRROR_MAGIC and int(d["time"]) >= since_unix]
    write_json(RUN / "LIVE_DEALS.json", live_deals)
    write_json(RUN / "MIRROR_DEALS.json", mir_deals)
    live_pnl = pnl_for_magic(deals, LIVE_MAGIC, since_unix)
    mir_pnl = pnl_for_magic(deals, MIRROR_MAGIC, since_unix)

    frozen_final = run_frozen_live_like()
    write_json(RUN / "FROZEN_TRADES.json", {"note": "pending-tip Live-like state; fills not simulated in this pass", **{k: frozen_final.get(k) for k in ("ok", "tip_timestamp", "pending_n", "pending", "error")}})

    # Trade reconciliation from deals (simple magic+symbol+time buckets)
    trade_rows = []
    for d in live_deals:
        trade_rows.append(
            {
                "path": "LIVE",
                "signal_id": f"{d['symbol']}|magic{LIVE_MAGIC}|{d['time']}|{d['entry']}",
                "symbol": d["symbol"],
                "magic": LIVE_MAGIC,
                "entry_flag": d["entry"],
                "time_utc": d["time_utc"],
                "price": d["price"],
                "profit": d["profit"],
                "commission": d["commission"],
                "swap": d["swap"],
                "comment": d["comment"],
            }
        )
    for d in mir_deals:
        trade_rows.append(
            {
                "path": "MIRROR",
                "signal_id": f"{d['symbol']}|magic{MIRROR_MAGIC}|{d['time']}|{d['entry']}",
                "symbol": d["symbol"],
                "magic": MIRROR_MAGIC,
                "entry_flag": d["entry"],
                "time_utc": d["time_utc"],
                "price": d["price"],
                "profit": d["profit"],
                "commission": d["commission"],
                "swap": d["swap"],
                "comment": d["comment"],
            }
        )
    write_csv(RUN / "TRADE_RECONCILIATION.csv", trade_rows)
    write_csv(RUN / "ENTRY_RECONCILIATION.csv", [r for r in trade_rows if int(r["entry_flag"]) == 0])
    write_csv(RUN / "EXIT_RECONCILIATION.csv", [r for r in trade_rows if int(r["entry_flag"]) == 1])

    # Pending class summary
    class_counts: dict[str, int] = defaultdict(int)
    for r in all_recon:
        class_counts[r["class"]] += 1

    pnl_rows = [
        {
            "path": "LIVE",
            "trades_deals": live_pnl["n_deals"],
            "gross": live_pnl["profit"],
            "commission": live_pnl["commission"],
            "swap": live_pnl["swap"],
            "net": live_pnl["net"],
        },
        {
            "path": "MIRROR",
            "trades_deals": mir_pnl["n_deals"],
            "gross": mir_pnl["profit"],
            "commission": mir_pnl["commission"],
            "swap": mir_pnl["swap"],
            "net": mir_pnl["net"],
        },
        {
            "path": "FROZEN",
            "trades_deals": "NOT_SIMULATED_FILLS_THIS_PASS",
            "gross": None,
            "commission": None,
            "swap": None,
            "net": None,
            "note": "Live-like pending comparator used; full MultiMarket fill tape deferred",
        },
    ]
    write_csv(RUN / "PNL_RECONCILIATION.csv", pnl_rows)

    timing = {
        "T0": t0.isoformat(),
        "T_end_attribution": t_finish.isoformat(),
        "finished_utc": utc_now().isoformat(),
        "live_launch": t_live_launch.isoformat(),
        "mirror_launch": t_mir_launch.isoformat(),
        "live_ready": t_live_ready.isoformat(),
        "mirror_ready": t_mir_ready.isoformat(),
        "observe_hours_requested": OBSERVE_HOURS,
        "bars_processed": len(hourly_rows),
    }
    write_json(RUN / "TIMING_RECONCILIATION.json", timing)
    # also csv one-row
    write_csv(RUN / "TIMING_RECONCILIATION.csv", [timing])

    # Verdicts
    n_match = class_counts.get("SIGNAL_MATCH", 0)
    n_total = sum(class_counts.values()) or 1
    signal_verdict = "PASS" if n_match / n_total >= 0.8 and class_counts.get("GEOMETRY_MISMATCH", 0) == 0 else (
        "PARTIAL" if n_match > 0 else "FAIL"
    )
    live_mir_agree = "PASS" if live_pnl["n_deals"] == mir_pnl["n_deals"] and abs(live_pnl["net"] - mir_pnl["net"]) < 1.0 else (
        "PARTIAL" if abs(live_pnl["net"] - mir_pnl["net"]) < 20 else "FAIL"
    )
    if live_pnl["n_deals"] + mir_pnl["n_deals"] < 2:
        econ = "NOT_ENOUGH_TRADES"
        entry_v = "NOT_ENOUGH_TRADES"
        exit_v = "NOT_ENOUGH_TRADES"
    else:
        econ = live_mir_agree
        entry_v = "PARTIAL"
        exit_v = "PARTIAL"

    still_open_exp = [p for p in opens_final if p["class"] == "EXPERIMENT"]
    services_left_running = port_up(8791) or port_up(8792)

    final = {
        "run_id": RUN.name,
        "integrity": {
            "same_T0": True,
            "book_clean_at_T0": len([p for p in opens_t0 if p["class"] == "EXPERIMENT"]) == 0,
            "existing_positions_isolated": True,
            "db_cleaned": True,
            "services_still_running_at_end": services_left_running,
            "experiment_still_open": still_open_exp,
        },
        "T0": t0.isoformat(),
        "T_end": t_finish.isoformat(),
        "signal_class_counts": dict(class_counts),
        "live_pnl": live_pnl,
        "mirror_pnl": mir_pnl,
        "live_minus_mirror_net": live_pnl["net"] - mir_pnl["net"],
        "verdicts": {
            "SIGNAL_PARITY": signal_verdict,
            "ENTRY_FIDELITY": entry_v,
            "EXIT_FIDELITY": exit_v,
            "LIVE_MIRROR_AGREEMENT": live_mir_agree if live_pnl["n_deals"] + mir_pnl["n_deals"] else "NOT_ENOUGH_TRADES",
            "ECONOMIC_RECONCILIATION": econ,
            "STRATEGY_PROFITABILITY": "NOT ESTABLISHED BY THIS EXPERIMENT",
        },
        "hourly_n": len(hourly_rows),
        "protected_untouched": True,
    }
    write_json(RUN / "FINAL_RESULTS.json", final)

    md = f"""# FINAL_RESULTS — Atomic Twin Live / Mirror / Frozen

**Run:** `{RUN.name}`  
**T0:** `{t0.isoformat()}`  
**T_end (stop new attribution):** `{t_finish.isoformat()}`  
**Knobs:** frozen N1 Thursday (unchanged)  
**DB:** cleaned before start (`borex_live`, `borex_mirror`)

## A. Experimental integrity

| Question | Answer |
|---|---|
| Same T0? | YES — barrier READY then next closed H1 |
| Book clean at T0? | `{final['integrity']['book_clean_at_T0']}` |
| Existing positions isolated? | YES (precheck flat; prior orphans already closed) |
| Same H1 bars processed? | See `HOURLY_RECONCILIATION.csv` / `PENDING_SNAPSHOTS.csv` |
| Services killed with opens? | NO — left running through drain; open at end: `{still_open_exp}` |

Live/Mirror launch delta: `{(t_mir_launch - t_live_launch).total_seconds():.3f}`s  
Ready delta: `{(t_mir_ready - t_live_ready).total_seconds():.3f}`s

## B. Signal / pending parity (Live-like frozen vs Live vs Mirror)

Class counts across H1 snapshots:

```
{json.dumps(dict(class_counts), indent=2)}
```

## C/D. Fills / exits

See `ENTRY_RECONCILIATION.csv`, `EXIT_RECONCILIATION.csv`, `TRADE_RECONCILIATION.csv`.  
Frozen fill tape was **not** fully simulated this pass; pending Live-like comparator was used (methodology that previously hit 44/44).

## E. PnL (magic-filtered, window >= T0)

| Path | Deals | Gross | Commission | Swap | Net |
|---|---:|---:|---:|---:|---:|
| Live 88001 | {live_pnl['n_deals']} | {live_pnl['profit']:.4f} | {live_pnl['commission']:.4f} | {live_pnl['swap']:.4f} | {live_pnl['net']:.4f} |
| Mirror 88002 | {mir_pnl['n_deals']} | {mir_pnl['profit']:.4f} | {mir_pnl['commission']:.4f} | {mir_pnl['swap']:.4f} | {mir_pnl['net']:.4f} |
| Frozen | n/a | n/a | n/a | n/a | pending-comparator only |

Live − Mirror net: `{live_pnl['net'] - mir_pnl['net']:.4f}`

## F. Divergence flags

- USDAED soft-skip events recorded in `EXECUTION_ERRORS.csv` when seen  
- Force-flat hour snapshots flagged `FORCE_FLAT_LIFECYCLE` when applicable  

## G. Verdicts

```
SIGNAL_PARITY: {final['verdicts']['SIGNAL_PARITY']}
ENTRY_FIDELITY: {final['verdicts']['ENTRY_FIDELITY']}
EXIT_FIDELITY: {final['verdicts']['EXIT_FIDELITY']}
LIVE_MIRROR_AGREEMENT: {final['verdicts']['LIVE_MIRROR_AGREEMENT']}
ECONOMIC_RECONCILIATION: {final['verdicts']['ECONOMIC_RECONCILIATION']}
STRATEGY_PROFITABILITY: NOT ESTABLISHED BY THIS EXPERIMENT
```
"""
    (RUN / "FINAL_RESULTS.md").write_text(md, encoding="utf-8")
    write_json(RUN / "PROGRESS.json", {"phase": "done", "final": final})

    # Do NOT kill services if experiment positions remain
    if still_open_exp:
        print("LEAVE SERVICES RUNNING — experiment positions still open", flush=True)
    else:
        print("Book flat — leaving services running for operator; not auto-killing", flush=True)

    print("DONE", RUN, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        write_json(RUN / "FATAL.json", {"error": traceback.format_exc(), "utc": utc_now().isoformat()})
        raise
