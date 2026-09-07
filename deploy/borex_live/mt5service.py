#!/usr/bin/env python3
"""CLI for the Borex live MT5 service (independent from borex-main backtests)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

import uvicorn

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from borex_live.api.server import bind_service, create_app
from borex_live.config import LiveServiceConfig
from borex_live.service import LiveService


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Borex live MT5 service")
    p.add_argument("--demo", action="store_true", help="Use MT5 demo account env vars")
    p.add_argument("--live-account", action="store_true", help="Use live MT5 credentials")
    p.add_argument(
        "--strategy",
        default="alexg7",
        help="alexg3|alexg4|alexg5|alexg5revised|alexg6|alexg7|alexg7aligned|alexg8|alexg9",
    )
    p.add_argument("--leverage", "-l", type=float, default=5000.0)
    p.add_argument(
        "--rr-mode",
        choices=("fixed", "dynamic"),
        default="",
        help="TP RR mode (alexg9 default: fixed)",
    )
    p.add_argument("--rr-factor", type=float, default=2.5)
    p.add_argument("--min-rr", type=float, default=3.0)
    p.add_argument("--rr-min", type=float, default=0.0, help="Clamp resolved RR after factor (0=off)")
    p.add_argument("--rr-max", type=float, default=0.0, help="Clamp resolved RR after factor (0=off)")
    p.add_argument(
        "--capital",
        type=float,
        default=0.0,
        help="Account size for theory/ghost baseline (0=fetch MT5 balance at startup)",
    )
    p.add_argument("--position-size", type=float, default=0.01)
    p.add_argument(
        "--max-positions",
        type=int,
        default=60,
        help="Max concurrent open positions (default 60 = all FX pairs)",
    )
    p.add_argument("--interval", "-i", default="1h")
    p.add_argument("--master", default="EURUSD=X")
    p.add_argument(
        "--symbols",
        default="",
        help="Comma-separated Yahoo symbols; default = all MT5 Forex pairs",
    )
    p.add_argument("--second-signal", choices=["off", "flip", "replace"], default="off")
    p.add_argument(
        "--ltf-intervals",
        default="1m",
        help="Unused (legacy LTF); alexg8 no longer confirms on lower TFs",
    )
    p.add_argument(
        "--ltf-confirm-mode",
        choices=["any", "all"],
        default="any",
        help="Unused (legacy LTF); alexg8 no longer confirms on lower TFs",
    )
    p.add_argument("--default-lot", type=float, default=0.01)
    p.add_argument(
        "--warmup-bars",
        type=int,
        default=10_000,
        help="MT5 H1 history used to rebuild strategy state (default 10000)",
    )
    p.add_argument(
        "--catchup-bars",
        type=int,
        default=500,
        help="Bars fetched each poll to recover from laptop sleep (default 500)",
    )
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--poll", type=int, default=30, help="Seconds between bar checks")
    p.add_argument("--db", default="", help="Primary Postgres URL (or DATABASE_URL env)")
    p.add_argument(
        "--db-backup",
        default="",
        help="Optional Railway/backup URL (or DATABASE_BACKUP_URL env)",
    )
    p.add_argument(
        "--backup-interval",
        type=int,
        default=300,
        help="Seconds between local→backup sync (0=disable; default 300)",
    )
    p.add_argument("--mt5-path", default="", help="Path to terminal64.exe")
    p.add_argument(
        "--same-bar-exit",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="If set, allow SL/TP on the entry bar. Default/--no-same-bar-exit: skip (backtest match)",
    )
    p.add_argument(
        "--commission-per-lot",
        type=float,
        default=7.0,
        help="Round-turn USD commission per 1.0 lot (default 7, matches backtest)",
    )
    p.add_argument(
        "--risk-include-commission",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Shrink size so SL price loss + commission ≈ 1%% risk (default on)",
    )
    p.add_argument(
        "--commission-at-entry",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Charge round-turn commission at entry; margin stays at full position-size (alexg9 default on)",
    )
    p.add_argument(
        "--force-flat-friday",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Flatten all opens from Friday force-flat hour (alexg9 default on)",
    )
    p.add_argument(
        "--force-flat-daily",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Flatten opens daily before NY close (alexg9 default on)",
    )
    p.add_argument(
        "--force-flat-utc-hour",
        type=int,
        default=19,
        help="H1 bar open hour (UTC) for daily force-flat (default 19 = exit before 21:00 NY)",
    )
    p.add_argument("--dry-run", action="store_true", help="Log only, no MT5 orders")
    p.add_argument("--borex-main", type=Path, default=None, help="Path to borex-main repo")
    p.add_argument("--tick-once", action="store_true", help="Process one bar and exit")
    p.add_argument("--no-ui", action="store_true", help="Skip FastAPI dashboard")
    return p.parse_args()


def build_config(args: argparse.Namespace) -> LiveServiceConfig:
    db = args.db or os.environ.get("DATABASE_URL", "")
    backup = (
        args.db_backup
        or os.environ.get("DATABASE_BACKUP_URL", "")
        or os.environ.get("RAILWAY_DATABASE_URL", "")
    )
    symbols = [
        s.strip() for s in (args.symbols or "").split(",") if s.strip()
    ]
    ltf_intervals = tuple(
        s.strip() for s in (args.ltf_intervals or "1m").split(",") if s.strip()
    ) or ("1m",)
    is_g9 = args.strategy == "alexg9"
    argv = " ".join(sys.argv)
    cfg = LiveServiceConfig(
        strategy=args.strategy,
        demo=args.demo and not args.live_account,
        capital=args.capital,
        leverage=args.leverage,
        rr_factor=args.rr_factor,
        min_rr=args.min_rr,
        position_size_pct=args.position_size,
        max_positions=args.max_positions,
        interval=args.interval,
        master_yahoo=args.master,
        second_signal=args.second_signal,
        ltf_intervals=ltf_intervals,
        ltf_confirm_mode=args.ltf_confirm_mode,
        default_lot=args.default_lot,
        dry_run=args.dry_run,
        port=args.port,
        host=args.host,
        warmup_bars=args.warmup_bars,
        catchup_bars=args.catchup_bars,
        database_url=db,
        database_backup_url=backup,
        backup_interval_seconds=int(args.backup_interval),
        mt5_path=args.mt5_path or os.environ.get("MT5_PATH", ""),
        symbols=symbols,
        borex_main_root=args.borex_main
        or (Path(os.environ["BOREX_MAIN_ROOT"]) if os.environ.get("BOREX_MAIN_ROOT") else None),
        same_bar_exit=bool(args.same_bar_exit),
        commission_per_lot=float(args.commission_per_lot),
        risk_include_commission=bool(args.risk_include_commission),
        rr_mode=args.rr_mode or ("fixed" if is_g9 else "dynamic"),
        rr_min=float(args.rr_min),
        rr_max=float(args.rr_max),
        commission_at_entry=bool(args.commission_at_entry),
        force_flat_friday=bool(args.force_flat_friday),
        force_flat_daily=bool(args.force_flat_daily),
        force_flat_utc_hour=int(args.force_flat_utc_hour),
    )
    if is_g9:
        if args.rr_factor == 2.5:
            cfg.rr_factor = 1.0
        if "--risk-include-commission" not in argv and "--no-risk-include-commission" not in argv:
            cfg.risk_include_commission = False
        if "--commission-at-entry" not in argv and "--no-commission-at-entry" not in argv:
            cfg.commission_at_entry = True
        if "--force-flat-friday" not in argv and "--no-force-flat-friday" not in argv:
            cfg.force_flat_friday = True
        if "--force-flat-daily" not in argv and "--no-force-flat-daily" not in argv:
            cfg.force_flat_daily = True
        if not args.rr_mode:
            cfg.rr_mode = "fixed"
    if args.demo:
        cfg.mt5_login = int(os.environ.get("MT5_LOGIN", "0") or 0)
        cfg.mt5_password = os.environ.get("MT5_PASSWORD", "")
        cfg.mt5_server = os.environ.get("MT5_DEMO_SERVER", os.environ.get("MT5_SERVER", ""))
    elif args.live_account:
        cfg.mt5_login = int(os.environ.get("MT5_LOGIN", "0") or 0)
        cfg.mt5_password = os.environ.get("MT5_PASSWORD", "")
        cfg.mt5_server = os.environ.get("MT5_LIVE_SERVER", os.environ.get("MT5_SERVER", ""))
    return cfg


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    cfg = build_config(args)

    if not cfg.database_url and not cfg.dry_run:
        print("ERROR: set --db or DATABASE_URL (required unless --dry-run)", file=sys.stderr)
        return 1

    service = LiveService(cfg)

    if args.tick_once:
        service.start()
        print(service.process_once())
        service.stop()
        return 0

    if not args.no_ui:
        bind_service(service)
        app = create_app()

        def _run_api():
            uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")

        threading.Thread(target=_run_api, daemon=True).start()
        print(f"Dashboard: http://{cfg.host}:{cfg.port}/")

    try:
        service.run_loop(poll_seconds=args.poll)
    except KeyboardInterrupt:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
