"""Live Mirror golden window with REAL broker fills (Gate 2 prerequisite).

Safety hard-fails:
  - port must be 8792
  - magic must be 88002
  - execute_mt5 must be True
  - refuses 8791 / 88001
  - never calls mt5.shutdown()
  - ephemeral SQLite only
  - no credentials in fixture

This process polls for NEW closed H1 bars and mirrors Theory opens to MT5.
It does NOT fire historical bars as market orders.

Exit when:
  --min-broker-fills reached, OR
  --max-hours elapsed (then Gate 2 may still be NO-GO if fills==0)

Usage (PowerShell):
  $env:BOREX_GOLDEN_CAPTURE_DIR = \"...\\golden_fixture\"
  borex_live\\.venv311\\Scripts\\python.exe scripts\\run_mirror_golden_live_execute.py --max-hours 6 --min-broker-fills 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path[:0] = [str(ROOT), str(LIVE)]

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def safety_check(cfg, account: dict[str, Any], capture_dir: Path) -> None:
    print("=" * 72)
    print("GATE-2 LIVE MIRROR — SAFETY BANNER")
    print("=" * 72)
    print(f"  environment     : Mirror / Theory ONLY")
    print(f"  port            : {cfg.port}")
    print(f"  magic           : {cfg.mt5_magic}")
    print(f"  execute_mt5     : {cfg.execute_mt5}")
    print(f"  dry_run         : {cfg.dry_run}")
    print(f"  account login   : {account.get('login')}")
    print(f"  account server  : {account.get('server')}")
    print(f"  balance         : {account.get('balance')}")
    print(f"  capture dir     : {capture_dir}")
    print(f"  database        : ephemeral sqlite")
    print("=" * 72)
    if int(cfg.port) == 8791 or int(cfg.mt5_magic) == 88001:
        raise SystemExit("HARD FAIL: live 8791/88001 path detected")
    if int(cfg.port) != 8792:
        raise SystemExit(f"HARD FAIL: port must be 8792, got {cfg.port}")
    if int(cfg.mt5_magic) != 88002:
        raise SystemExit(f"HARD FAIL: magic must be 88002, got {cfg.mt5_magic}")
    if not cfg.execute_mt5:
        raise SystemExit("HARD FAIL: execute_mt5 must be True for Gate 2")
    if cfg.dry_run:
        raise SystemExit("HARD FAIL: dry_run must be False for real broker fills")


def count_magic_deals(
    mt5,
    magic: int,
    since_utc: datetime,
    *,
    until_utc: datetime | None = None,
) -> tuple[int, list[dict]]:
    """Dump magic deals with timestamps normalized to genuine UTC.

    MT5 ``deal.time`` follows the broker server clock (often GMT+2/+3).
    Gate 2 must not treat that value as UTC via raw ``fromtimestamp(..., UTC)``.
    Uses ``Mt5Client.server_offset_seconds`` + ``mt5_unix_to_utc`` when available.
    """
    t1 = until_utc or datetime.now(timezone.utc)
    if since_utc.tzinfo is None:
        since_utc = since_utc.replace(tzinfo=timezone.utc)
    if t1.tzinfo is None:
        t1 = t1.replace(tzinfo=timezone.utc)

    offset = int(getattr(mt5, "server_offset_seconds", 0) or 0)
    try:
        from borex.data.mt5_time import mt5_unix_to_utc, utc_to_mt5_request

        req0 = utc_to_mt5_request(since_utc, offset)
        req1 = utc_to_mt5_request(t1, offset)
        to_utc = lambda u: mt5_unix_to_utc(int(u), offset)
    except Exception:
        req0, req1 = since_utc, t1
        to_utc = lambda u: datetime.fromtimestamp(int(u), tz=timezone.utc)

    api = getattr(mt5, "_mt5", None) or mt5
    deals = api.history_deals_get(req0, req1) or []
    rows = []
    for d in deals:
        if int(getattr(d, "magic", 0) or 0) != magic:
            continue
        # skip balance/credit
        if int(getattr(d, "type", -1)) in (2, 3):  # DEAL_TYPE_BALANCE/CREDIT often
            continue
        raw_unix = int(d.time)
        rows.append(
            {
                "ticket": int(d.ticket),
                "order": int(getattr(d, "order", 0) or 0),
                "symbol": str(d.symbol),
                "type": int(d.type),
                "entry": int(getattr(d, "entry", 0) or 0),
                "volume": float(d.volume),
                "price": float(d.price),
                "profit": float(d.profit),
                "commission": float(getattr(d, "commission", 0) or 0),
                "swap": float(getattr(d, "swap", 0) or 0),
                "time": to_utc(raw_unix).isoformat(),
                "time_unix_raw": raw_unix,
                "time_utc_source": "mt5_unix_to_utc",
                "server_offset_seconds": offset,
                "magic": int(d.magic),
                "comment": str(getattr(d, "comment", "") or ""),
            }
        )
    return len(rows), rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-hours", type=float, default=6.0)
    ap.add_argument("--min-broker-fills", type=int, default=2, help="deal rows with magic 88002")
    ap.add_argument("--poll", type=int, default=30)
    ap.add_argument("--warmup-bars", type=int, default=2500)
    ap.add_argument("--capital", type=float, default=0.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    window = Path(
        args.out
        or (ROOT / "data" / "runs" / "golden_windows" / f"mirror_live_exec_{stamp}")
    )
    fix = window / "golden_fixture"
    fix.mkdir(parents=True, exist_ok=True)
    os.environ["BOREX_GOLDEN_CAPTURE_DIR"] = str(fix)

    from borex_mirror.config import MirrorConfig
    from borex_mirror.service import MirrorService

    db_path = window / "mirror_gate2_ephemeral.sqlite"
    if db_path.exists():
        db_path.unlink()

    cfg = MirrorConfig(
        strategy="alexg8",
        demo=True,
        capital=float(args.capital),
        leverage=5000.0,
        min_rr=2.0,
        rr_factor=1.0,
        rr_mode="dynamic",
        rr_min=2.0,
        rr_max=6.0,
        position_size_pct=0.01,
        max_positions=60,
        interval="1h",
        master_yahoo="EURUSD=X",
        warmup_bars=int(args.warmup_bars),
        catchup_bars=50,
        port=8792,
        host="127.0.0.1",
        dry_run=False,
        execute_mt5=True,
        same_bar_exit=False,
        commission_per_lot=7.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_friday=False,
        force_flat_daily=True,
        force_flat_utc_hour=19,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
        database_backup_url="",
        mt5_path=os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", ""),
        mt5_magic=88002,
    )
    cfg.mt5_login = int(
        os.environ.get("MIRROR_MT5_LOGIN") or os.environ.get("MT5_LOGIN", "0") or 0
    )
    cfg.mt5_password = (
        os.environ.get("MIRROR_MT5_PASSWORD") or os.environ.get("MT5_PASSWORD", "")
    )
    cfg.mt5_server = (
        os.environ.get("MIRROR_MT5_DEMO_SERVER")
        or os.environ.get("MT5_DEMO_SERVER")
        or os.environ.get("MT5_SERVER", "")
    )

    status_path = window / "live_status.json"
    started = datetime.now(timezone.utc)

    try:
        print("Connecting MT5 (Mirror)…", flush=True)
        service = MirrorService(cfg)
        # start() connects + load_universe + init engine at tip (future bars only)
        service.start(init_engine=True)
        if hasattr(service.engine.shadow.strategy, "ghost_sl_mult"):
            service.engine.shadow.strategy.ghost_sl_mult = 0.6

        ai = service.mt5._mt5.account_info() if service.mt5.connected else None
        account = {
            "login": int(ai.login) if ai else None,
            "server": str(ai.server) if ai else None,
            "balance": float(ai.balance) if ai else None,
            "trade_mode": int(getattr(ai, "trade_mode", -1)) if ai else None,
        }
        safety_check(cfg, account, fix)

        if not service.mt5.connected:
            raise SystemExit("HARD FAIL: MT5 not connected")

        # Baseline deal count at start (only count NEW deals after this)
        baseline_n, _ = count_magic_deals(service.mt5, 88002, started)
        print(f"Baseline magic=88002 deals since start marker: will count NEW only (baseline snapshot n≈{baseline_n})", flush=True)
        print(
            f"Live loop: poll={args.poll}s max_hours={args.max_hours} "
            f"min_broker_fills={args.min_broker_fills}",
            flush=True,
        )
        print(
            "Waiting for NEW closed H1 bars → Theory step → real MT5 mirror orders…",
            flush=True,
        )

        while True:
            now = datetime.now(timezone.utc)
            elapsed_h = (now - started).total_seconds() / 3600.0
            tick = service.process_once()
            n_deals, deal_rows = count_magic_deals(service.mt5, 88002, started)
            # Also count positions opened with magic 88002
            pos = service.mt5._mt5.positions_get() or []
            n_pos = sum(1 for p in pos if int(getattr(p, "magic", 0) or 0) == 88002)

            status = {
                "updated_at": _utc(),
                "started_at": started.isoformat(),
                "elapsed_hours": elapsed_h,
                "last_tick": tick,
                "new_magic_88002_deals": n_deals,
                "open_magic_88002_positions": n_pos,
                "config_hash": service.engine.shadow.config_hash if service.engine else None,
                "last_master_ts": service.engine.shadow.last_master_ts if service.engine else None,
                "capture_dir": str(fix),
                "port": 8792,
                "magic": 88002,
                "execute_mt5": True,
            }
            status_path.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
            print(
                f"[{_utc()}] tick={tick} deals_88002_since_start={n_deals} "
                f"open_pos={n_pos} elapsed_h={elapsed_h:.2f}",
                flush=True,
            )

            if n_deals >= args.min_broker_fills:
                print("Min broker fills reached — freezing capture.", flush=True)
                break
            if elapsed_h >= args.max_hours:
                print("Max hours reached.", flush=True)
                break
            time.sleep(max(5, int(args.poll)))

        # Finalize golden capture if present
        if service.engine and getattr(service.engine, "golden", None) is not None:
            service.engine.golden.finalize(
                extra={
                    "mode": "live_execute_mt5",
                    "port": 8792,
                    "magic": 88002,
                    "execute_mt5": True,
                    "started_at": started.isoformat(),
                    "ended_at": _utc(),
                    "broker_deals_since_start": n_deals,
                }
            )

        n_deals, deal_rows = count_magic_deals(service.mt5, 88002, started)
        offset = int(getattr(service.mt5, "server_offset_seconds", 0) or 0)
        (window / "broker_deals_magic_88002.json").write_text(
            json.dumps(
                {
                    "since": started.isoformat(),
                    "until": _utc(),
                    "n": n_deals,
                    "deals": deal_rows,
                    "account_login": account.get("login"),
                    "magic": 88002,
                    "port": 8792,
                    "timestamp_normalized": True,
                    "server_offset_seconds": offset,
                    "normalization": "mt5_unix_to_utc via Mt5Client.server_offset_seconds",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (window / "GATE2_CAPTURE_SUMMARY.json").write_text(
            json.dumps(
                {
                    "window": str(window),
                    "broker_fills_obtained": n_deals > 0,
                    "n_deals": n_deals,
                    "n_open_positions": n_pos,
                    "next": (
                        "run_gate2_broker_fidelity.py"
                        if n_deals > 0
                        else "Gate 2 remains NO-GO — no real broker fills"
                    ),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(json.dumps({"window": str(window), "n_deals": n_deals}, indent=2))
        # Do NOT call service.stop() if it shuts down MT5 — check stop()
        # MirrorService.stop may disconnect; avoid shutdown for shared terminal
        return 0 if n_deals > 0 else 3
    except Exception:
        traceback.print_exc()
        (window / "GATE2_CAPTURE_ERROR.json").write_text(
            json.dumps({"error": traceback.format_exc(), "at": _utc()}, indent=2),
            encoding="utf-8",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
