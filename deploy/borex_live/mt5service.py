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
    p.add_argument("--strategy", default="alexg5", help="alexg3|alexg4|alexg5|alexg6")
    p.add_argument("--leverage", "-l", type=float, default=5000.0)
    p.add_argument("--rr-factor", type=float, default=2.5)
    p.add_argument("--min-rr", type=float, default=2.0)
    p.add_argument("--capital", type=float, default=1000.0)
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
    p.add_argument("--default-lot", type=float, default=0.01)
    p.add_argument("--warmup-bars", type=int, default=300)
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--poll", type=int, default=30, help="Seconds between bar checks")
    p.add_argument("--db", default="", help="Postgres URL (or DATABASE_URL env)")
    p.add_argument("--mt5-path", default="", help="Path to terminal64.exe")
    p.add_argument("--dry-run", action="store_true", help="Log only, no MT5 orders")
    p.add_argument("--borex-main", type=Path, default=None, help="Path to borex-main repo")
    p.add_argument("--tick-once", action="store_true", help="Process one bar and exit")
    p.add_argument("--no-ui", action="store_true", help="Skip FastAPI dashboard")
    return p.parse_args()


def build_config(args: argparse.Namespace) -> LiveServiceConfig:
    db = args.db or os.environ.get("DATABASE_URL", "")
    symbols = [
        s.strip() for s in (args.symbols or "").split(",") if s.strip()
    ]
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
        default_lot=args.default_lot,
        dry_run=args.dry_run,
        port=args.port,
        host=args.host,
        warmup_bars=args.warmup_bars,
        database_url=db,
        mt5_path=args.mt5_path or os.environ.get("MT5_PATH", ""),
        symbols=symbols,
        borex_main_root=args.borex_main
        or (Path(os.environ["BOREX_MAIN_ROOT"]) if os.environ.get("BOREX_MAIN_ROOT") else None),
    )
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
