#!/usr/bin/env python3
"""CLI for Borex Mirror — theory decides, MT5 mirrors at candle close.

Same bar processor for:
  - live hour-by-hour MT5 feed (--demo)
  - bulk / offline paper (--bulk / --no-execute-mt5)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from borex_mirror.api import bind_service, create_app
from borex_mirror.config import MirrorConfig
from borex_mirror.service import MirrorService


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Borex Mirror (theory → market at close)")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--live-account", action="store_true")
    p.add_argument("--strategy", default="alexg9")
    p.add_argument("--leverage", "-l", type=float, default=5000.0)
    p.add_argument("--rr-mode", choices=("fixed", "dynamic"), default="fixed")
    p.add_argument("--min-rr", type=float, default=3.0)
    p.add_argument("--rr-factor", type=float, default=1.0)
    p.add_argument("--rr-min", type=float, default=0.0, help="Clamp resolved RR (0=off)")
    p.add_argument("--rr-max", type=float, default=0.0, help="Clamp resolved RR (0=off)")
    p.add_argument(
        "--capital",
        type=float,
        default=0.0,
        help="0 = fetch MT5 balance at startup",
    )
    p.add_argument("--position-size", type=float, default=0.01)
    p.add_argument("--max-positions", type=int, default=60)
    p.add_argument("--interval", "-i", default="1h")
    p.add_argument("--master", default="EURUSD=X")
    p.add_argument("--symbols", default="")
    p.add_argument("--warmup-bars", type=int, default=10_000)
    p.add_argument("--catchup-bars", type=int, default=500)
    p.add_argument("--port", type=int, default=8792)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--poll", type=int, default=30)
    p.add_argument(
        "--db",
        default="",
        help="Mirror Postgres URL (or MIRROR_DATABASE_URL / DATABASE_URL)",
    )
    p.add_argument("--mt5-path", default="")
    p.add_argument("--commission-per-lot", type=float, default=7.0)
    p.add_argument(
        "--risk-include-commission",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Shrink size so SL+commission ≈ 1%% risk (alexg8 $2k test: on)",
    )
    p.add_argument(
        "--commission-at-entry",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Charge round-turn commission at entry (alexg8 $2k test: off)",
    )
    p.add_argument(
        "--force-flat-friday",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Flatten leftovers Friday NY close (alexg8 $2k test: off)",
    )
    p.add_argument(
        "--force-flat-daily",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Flatten each trade at origin-session close (alexg8 $2k test: on)",
    )
    p.add_argument(
        "--execute-mt5",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Place real MT5 orders (default on). --no-execute-mt5 = paper mirror",
    )
    p.add_argument("--dry-run", action="store_true", help="MT5 dry-run stubs")
    p.add_argument("--tick-once", action="store_true")
    p.add_argument("--no-ui", action="store_true")
    p.add_argument(
        "--bulk",
        action="store_true",
        help="Replay loaded history through shared processor (always paper for history; then continues live)",
    )
    p.add_argument(
        "--bulk-only",
        action="store_true",
        help="With --bulk: exit after history replay (no live loop)",
    )
    p.add_argument("--bulk-start", default="", help="ISO ts to start bulk trading window")
    return p.parse_args()


def build_config(args: argparse.Namespace) -> MirrorConfig:
    db = (
        args.db
        or os.environ.get("MIRROR_DATABASE_URL", "")
        or os.environ.get("DATABASE_URL", "")
    )
    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
    cfg = MirrorConfig(
        strategy=args.strategy,
        demo=args.demo and not args.live_account,
        capital=args.capital,
        leverage=args.leverage,
        min_rr=args.min_rr,
        rr_factor=args.rr_factor,
        rr_mode=args.rr_mode,
        position_size_pct=args.position_size,
        max_positions=args.max_positions,
        interval=args.interval,
        master_yahoo=args.master,
        warmup_bars=args.warmup_bars,
        catchup_bars=args.catchup_bars,
        port=args.port,
        host=args.host,
        dry_run=args.dry_run,
        execute_mt5=bool(args.execute_mt5) and not args.dry_run,
        commission_per_lot=float(args.commission_per_lot),
        database_url=db,
        database_backup_url=(
            os.environ.get("MIRROR_DATABASE_BACKUP_URL", "")
            or os.environ.get("DATABASE_BACKUP_URL", "")
            or os.environ.get("RAILWAY_DATABASE_URL", "")
        ),
        mt5_path=(
            args.mt5_path
            or os.environ.get("MIRROR_MT5_PATH", "")
            or os.environ.get("MT5_PATH", "")
        ),
        symbols=symbols,
        rr_min=float(args.rr_min),
        rr_max=float(args.rr_max),
        risk_include_commission=bool(args.risk_include_commission),
        commission_at_entry=bool(args.commission_at_entry),
        force_flat_friday=bool(args.force_flat_friday),
        force_flat_daily=bool(args.force_flat_daily),
    )
    if args.demo:
        cfg.mt5_login = int(
            os.environ.get("MIRROR_MT5_LOGIN")
            or os.environ.get("MT5_LOGIN", "0")
            or 0
        )
        cfg.mt5_password = (
            os.environ.get("MIRROR_MT5_PASSWORD")
            or os.environ.get("MT5_PASSWORD", "")
        )
        cfg.mt5_server = (
            os.environ.get("MIRROR_MT5_DEMO_SERVER")
            or os.environ.get("MT5_DEMO_SERVER")
            or os.environ.get("MT5_SERVER", "")
        )
    elif args.live_account:
        cfg.mt5_login = int(
            os.environ.get("MIRROR_MT5_LOGIN")
            or os.environ.get("MT5_LOGIN", "0")
            or 0
        )
        cfg.mt5_password = (
            os.environ.get("MIRROR_MT5_PASSWORD")
            or os.environ.get("MT5_PASSWORD", "")
        )
        cfg.mt5_server = (
            os.environ.get("MIRROR_MT5_LIVE_SERVER")
            or os.environ.get("MT5_LIVE_SERVER")
            or os.environ.get("MT5_SERVER", "")
        )
    # Prefer dedicated second-terminal path
    cfg.mt5_path = (
        args.mt5_path
        or os.environ.get("MIRROR_MT5_PATH", "")
        or os.environ.get("MT5_PATH", "")
    )
    return cfg


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    cfg = build_config(args)

    if not cfg.database_url and not cfg.dry_run:
        print(
            "ERROR: set --db or MIRROR_DATABASE_URL / DATABASE_URL",
            file=sys.stderr,
        )
        return 1

    service = MirrorService(cfg)
    # Bulk: load MT5 history first, then replay (don't init epoch at tip yet).
    service.start(init_engine=not args.bulk)

    if args.bulk:
        # Never fire historical opens as live market orders.
        print(
            "Bulk replay: paper only (historical theory→mirror). "
            "Live MT5 mirroring starts after replay for NEW bars.",
            flush=True,
        )
        stats = service.process_bulk(
            start_ts=args.bulk_start or None,
            execute_mt5=False,
        )
        print(stats, flush=True)
        if args.bulk_only or args.tick_once:
            service.stop()
            return 0
        # Continue into live loop with configured execute_mt5 for future bars.

    if args.tick_once:
        print(service.process_once())
        service.stop()
        return 0

    if not args.no_ui:
        bind_service(service)
        app = create_app()

        def _serve() -> None:
            uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")

        threading.Thread(target=_serve, daemon=True).start()
        print(f"Mirror dashboard http://{cfg.host}:{cfg.port}/")

    try:
        service.run_loop(poll_seconds=args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
