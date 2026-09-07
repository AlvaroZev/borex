#!/usr/bin/env python3
"""Paper bulk RR sweep: 3 strategies x 5 RR knobs, abort if cash < $500.

Checks the child log every 3 minutes. Does not run the full 10k-bar window:
a run is LOSS if cash < 500, SURVIVED if still >= 500 after max_minutes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv311" / "Scripts" / "python.exe"
LOG_DIR = ROOT / "sweep_logs"
RESULTS = ROOT / "sweep_rr_results.jsonl"

# Registry has no alexg7revised; aligned is the revised/feed-tuned g7.
STRATEGIES = ("alexg9", "alexg8", "alexg7aligned")
VARIANTS = (
    ("fixed", 3.0, 1.0),
    ("fixed", 5.5, 1.0),
    ("fixed", 3.0, 1.88),
    ("dynamic", 3.0, 1.0),
    ("dynamic", 3.0, 1.88),
)

CASH_RE = re.compile(r"cash \$([0-9]+(?:\.[0-9]+)?)", re.I)
TRADES_RE = re.compile(r"trades=(\d+)")
WR_RE = re.compile(r"WR\s+(\d+)/(\d+)\s+\(([\d.]+)%\)")
CHECK_EVERY_S = 180
MAX_MINUTES = 15
LOSS_CASH = 500.0
START_CAPITAL_HINT = 1000.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_log(text: str) -> dict:
    cash = None
    trades = None
    wr = None
    for m in CASH_RE.finditer(text):
        cash = float(m.group(1))
    for m in TRADES_RE.finditer(text):
        trades = int(m.group(1))
    for m in WR_RE.finditer(text):
        wr = {
            "wins": int(m.group(1)),
            "n": int(m.group(2)),
            "pct": float(m.group(3)),
        }
    return {"cash": cash, "trades": trades, "wr": wr}


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_one(strategy: str, rr_mode: str, min_rr: float, rr_factor: float, idx: int) -> dict:
    tag = f"{idx:02d}_{strategy}_{rr_mode}_rr{min_rr:g}_tp{rr_factor:g}"
    log_path = LOG_DIR / f"{tag}.log"
    LOG_DIR.mkdir(exist_ok=True)
    cmd = [
        str(PYTHON),
        "mirrorservice.py",
        "--demo",
        "--strategy",
        strategy,
        "--rr-mode",
        rr_mode,
        "--min-rr",
        str(min_rr),
        "--rr-factor",
        str(rr_factor),
        "--leverage",
        "5000",
        "--position-size",
        "0.01",
        "--max-positions",
        "60",
        "--interval",
        "1h",
        "--warmup-bars",
        "10000",
        "--catchup-bars",
        "500",
        "--port",
        "8792",
        "--commission-per-lot",
        "7.0",
        "--bulk",
        "--bulk-only",
        "--no-ui",
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    print(f"\n=== [{_now()}] START {tag} ===", flush=True)
    print(" ".join(cmd), flush=True)
    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=env,
        )
    t0 = time.time()
    last_stats = {"cash": START_CAPITAL_HINT, "trades": 0, "wr": None}
    verdict = "ERROR"
    reason = "unknown"
    try:
        while True:
            time.sleep(CHECK_EVERY_S)
            elapsed_m = (time.time() - t0) / 60.0
            text = log_path.read_text(encoding="utf-8", errors="replace")
            stats = _parse_log(text)
            if stats["cash"] is not None:
                last_stats = stats
            cash = last_stats.get("cash")
            print(
                f"  check {elapsed_m:.1f}m | {tag} | cash={cash} trades={last_stats.get('trades')} wr={last_stats.get('wr')}",
                flush=True,
            )
            if proc.poll() is not None:
                if "Traceback" in text:
                    verdict = "ERROR"
                    reason = "crashed"
                elif cash is not None and cash < LOSS_CASH:
                    verdict = "LOSS"
                    reason = f"finished cash ${cash:.2f} < {LOSS_CASH:g}"
                else:
                    verdict = "SURVIVED"
                    reason = "bulk completed"
                break
            if cash is not None and cash < LOSS_CASH:
                verdict = "LOSS"
                reason = f"cash ${cash:.2f} < {LOSS_CASH:g} at {elapsed_m:.1f}m"
                _kill(proc)
                break
            if elapsed_m >= MAX_MINUTES:
                if cash is not None and cash < LOSS_CASH:
                    verdict = "LOSS"
                    reason = f"cash ${cash:.2f} at {MAX_MINUTES}m cap"
                else:
                    verdict = "SURVIVED"
                    reason = f">=${LOSS_CASH:g} after {MAX_MINUTES}m (not full 10k bars)"
                _kill(proc)
                break
    finally:
        _kill(proc)
        try:
            proc.wait(timeout=15)
        except Exception:
            pass

    row = {
        "ts": _now(),
        "tag": tag,
        "strategy": strategy,
        "rr_mode": rr_mode,
        "min_rr": min_rr,
        "rr_factor": rr_factor,
        "verdict": verdict,
        "reason": reason,
        "cash": last_stats.get("cash"),
        "trades": last_stats.get("trades"),
        "wr": last_stats.get("wr"),
        "elapsed_min": round((time.time() - t0) / 60.0, 2),
        "log": str(log_path),
    }
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(f"=== [{_now()}] {verdict} {tag} | {reason} ===", flush=True)
    return row


def main() -> int:
    LOG_DIR.mkdir(exist_ok=True)
    if RESULTS.exists():
        RESULTS.unlink()
    rows = []
    n = 0
    for strat in STRATEGIES:
        for rr_mode, min_rr, rr_factor in VARIANTS:
            n += 1
            rows.append(_run_one(strat, rr_mode, min_rr, rr_factor, n))
    print("\n======== SWEEP SUMMARY ========", flush=True)
    for r in rows:
        print(
            f"{r['verdict']:9} {r['tag']:40} cash={r['cash']} trades={r['trades']} {r['reason']}",
            flush=True,
        )
    survived = [r for r in rows if r["verdict"] == "SURVIVED"]
    losses = [r for r in rows if r["verdict"] == "LOSS"]
    print(
        f"\nDone: {len(survived)} survived, {len(losses)} loss, {len(rows) - len(survived) - len(losses)} error / {len(rows)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
