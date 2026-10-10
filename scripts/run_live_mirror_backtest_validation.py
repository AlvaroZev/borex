#!/usr/bin/env python3
"""6h Live↔Mirror↔Backtest fidelity validation for frozen n1_thursday.

Single-terminal mode:
  - DEMO/MIRROR broker path: MT5 account 53074439 @ lev 500, magic 88002
  - BACKTEST path: Mirror ShadowEngine theory book (same knobs)
  - LIVE 52958398: NOT run (single terminal; lev 5000 ≠ N1 500)

Does not touch protected EURDKK tickets on 53019882.
Does not read/optimize September. No param mining.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LIVE_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")
MAIN_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main")
sys.path[:0] = [str(LIVE_ROOT), str(MAIN_ROOT)]

from dotenv import load_dotenv

load_dotenv(LIVE_ROOT / ".env")

# Experiment credentials (process-local; do not overwrite shared .env Live login)
os.environ["MT5_LOGIN"] = "53074439"
os.environ["MIRROR_MT5_LOGIN"] = "53074439"
os.environ["MT5_PASSWORD"] = "5$kHlhL9CmNm0O"
os.environ["MIRROR_MT5_PASSWORD"] = "5$kHlhL9CmNm0O"
os.environ["MT5_SERVER"] = "ICMarketsSC-Demo"
os.environ["MT5_DEMO_SERVER"] = "ICMarketsSC-Demo"
os.environ["MIRROR_MT5_DEMO_SERVER"] = "ICMarketsSC-Demo"
os.environ["BOREX_MAIN_ROOT"] = str(MAIN_ROOT)

from borex_mirror.config import MirrorConfig
from borex_mirror.service import MirrorService
from borex_mirror.repository import MirrorRepository

RUN_ID = "live_mirror_backtest_validation_20260928_164833"
OUT = MAIN_ROOT / "data" / "runs" / "research" / RUN_ID
OUT.mkdir(parents=True, exist_ok=True)
DB_PATH = OUT / "experiment_mirror.sqlite"
DB_URL = f"sqlite+pysqlite:///{DB_PATH.as_posix()}"

OBSERVE_HOURS = 6.0
POLL_SECONDS = 30
PROTECTED_EURDKK = {
    1890205005,
    1891279302,
    1893184489,
    1917854332,
    1940979693,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(OUT / "harness.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("n1_validation")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _csv_write(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def build_cfg() -> MirrorConfig:
    return MirrorConfig(
        strategy="alexg8",
        demo=True,
        capital=0.0,
        leverage=500.0,
        min_rr=2.0,
        rr_factor=1.0,
        rr_mode="dynamic",
        rr_min=2.0,
        rr_max=5.0,
        position_size_pct=0.01,
        max_positions=60,
        interval="1h",
        master_yahoo="EURUSD=X",
        warmup_bars=10_000,
        catchup_bars=500,
        port=8792,
        host="127.0.0.1",
        dry_run=False,
        execute_mt5=True,
        same_bar_exit=False,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=True,
        commission_at_entry=False,
        winrate_min_trades=20,
        force_flat_friday=False,
        force_flat_daily=True,
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        database_url=DB_URL,
        database_backup_url="",
        mt5_path=os.environ.get("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe"),
        mt5_login=53074439,
        mt5_password=os.environ["MT5_PASSWORD"],
        mt5_server="ICMarketsSC-Demo",
        borex_main_root=MAIN_ROOT,
        mt5_magic=88002,
        ghost_sl_mult=4.0,
        peer_blend=0.0,
        thursday_gate=True,
        accept_new_entries=True,
    )


def snapshot_mt5(service: MirrorService) -> dict[str, Any]:
    mt5 = service.mt5
    acc = None
    try:
        import MetaTrader5 as m

        acc = m.account_info()
    except Exception:
        pass
    positions = []
    orders = []
    try:
        raw = mt5.positions() if hasattr(mt5, "positions") else None
    except Exception:
        raw = None
    try:
        import MetaTrader5 as m

        for p in m.positions_get() or []:
            positions.append(
                {
                    "ticket": int(p.ticket),
                    "symbol": p.symbol,
                    "magic": int(p.magic),
                    "comment": str(p.comment),
                    "volume": float(p.volume),
                    "profit": float(p.profit),
                    "type": int(p.type),
                }
            )
        for o in m.orders_get() or []:
            orders.append(
                {
                    "ticket": int(o.ticket),
                    "symbol": o.symbol,
                    "magic": int(o.magic),
                    "comment": str(o.comment),
                    "volume": float(o.volume_current),
                }
            )
    except Exception as exc:
        logger.exception("MT5 snapshot failed: %s", exc)
    info = {
        "timestamp_utc": _utc_now().isoformat(),
        "login": int(acc.login) if acc else None,
        "server": str(acc.server) if acc else None,
        "balance": float(acc.balance) if acc else None,
        "equity": float(acc.equity) if acc else None,
        "leverage": int(acc.leverage) if acc else None,
        "positions": positions,
        "orders": orders,
        "protected_eurdkk_on_this_account": [
            p for p in positions if int(p["ticket"]) in PROTECTED_EURDKK
        ],
    }
    return info


def export_artifacts(service: MirrorService, meta: dict[str, Any]) -> None:
    with service._session() as session:
        repo = MirrorRepository(session)
        theory = []
        mirror = []
        try:
            from borex_mirror import models as mm
            from sqlalchemy import select

            for t in session.scalars(select(mm.MirrorTheoryTrade)).all():
                theory.append(
                    {
                        "symbol": t.symbol,
                        "side": t.side,
                        "pattern": t.pattern,
                        "entry_time": t.entry_time,
                        "entry_price": t.entry_price,
                        "stop_loss": t.stop_loss,
                        "take_profit": t.take_profit,
                        "rr_used": t.rr_used,
                        "margin": t.margin,
                        "commission": t.commission,
                        "status": t.status,
                        "exit_time": t.exit_time,
                        "exit_price": t.exit_price,
                        "exit_reason": t.exit_reason,
                        "pnl": t.pnl,
                    }
                )
            for t in session.scalars(select(mm.MirrorTrade)).all():
                mirror.append(
                    {
                        "symbol": t.symbol,
                        "side": t.side,
                        "pattern": t.pattern,
                        "entry_time": t.entry_time,
                        "requested_entry_price": t.theory_entry,
                        "actual_entry_price": t.entry_price,
                        "ticket": t.mt5_ticket,
                        "volume": t.volume,
                        "stop_loss": t.stop_loss,
                        "take_profit": t.take_profit,
                        "status": t.status,
                        "exit_time": t.exit_time,
                        "exit_price": t.exit_price,
                        "exit_reason": t.exit_reason,
                        "pnl": t.pnl,
                        "commission": None,
                        "spread": None,
                        "paper": t.paper,
                    }
                )
        except Exception:
            logger.exception("export query failed")
            theory, mirror = [], []

    signal_rows = []
    for t in theory:
        signal_rows.append(
            {
                "timestamp": t["entry_time"],
                "pair": t["symbol"],
                "direction": t["side"],
                "signal_id": f"{t['symbol']}|{t['entry_time']}|{t['pattern']}",
                "ghost_entry": "",
                "ghost_sl": t["stop_loss"],
                "real_entry": t["entry_price"],
                "real_sl": t["stop_loss"],
                "real_tp": t["take_profit"],
                "rr": t["rr_used"],
                "decision_status": t["status"],
            }
        )

    _csv_write(
        OUT / "SIGNAL_LOG.csv",
        signal_rows,
        [
            "timestamp",
            "pair",
            "direction",
            "signal_id",
            "ghost_entry",
            "ghost_sl",
            "real_entry",
            "real_sl",
            "real_tp",
            "rr",
            "decision_status",
        ],
    )
    _csv_write(
        OUT / "BACKTEST_TRADES.csv",
        [
            {
                "theory_entry_time": t["entry_time"],
                "pair": t["symbol"],
                "direction": t["side"],
                "theory_entry_price": t["entry_price"],
                "theory_exit_time": t["exit_time"],
                "theory_exit_price": t["exit_price"],
                "theory_exit_reason": t["exit_reason"],
                "theory_net_pnl": t["pnl"],
                "theory_cost": t["commission"],
                "status": t["status"],
                "signal_id": f"{t['symbol']}|{t['entry_time']}|{t['pattern']}",
            }
            for t in theory
        ],
        [
            "theory_entry_time",
            "pair",
            "direction",
            "theory_entry_price",
            "theory_exit_time",
            "theory_exit_price",
            "theory_exit_reason",
            "theory_net_pnl",
            "theory_cost",
            "status",
            "signal_id",
        ],
    )
    _csv_write(
        OUT / "MIRROR_TRADES.csv",
        [
            {
                **m,
                "signal_id": f"{m.get('symbol')}|{m.get('entry_time')}|{m.get('pattern')}",
            }
            for m in mirror
        ],
        [
            "symbol",
            "side",
            "pattern",
            "entry_time",
            "requested_entry_price",
            "actual_entry_price",
            "ticket",
            "volume",
            "stop_loss",
            "take_profit",
            "status",
            "exit_time",
            "exit_price",
            "exit_reason",
            "pnl",
            "commission",
            "spread",
            "signal_id",
        ],
    )
    # Live not run on this terminal
    _csv_write(
        OUT / "LIVE_TRADES.csv",
        [],
        [
            "symbol",
            "side",
            "entry_time",
            "requested_entry_price",
            "actual_entry_price",
            "ticket",
            "status",
            "exit_time",
            "exit_price",
            "exit_reason",
            "pnl",
            "note",
        ],
    )

    # Simple reconciliation by signal_id
    th_by = {f"{t['symbol']}|{t['entry_time']}|{t['pattern']}": t for t in theory}
    mi_by = {
        f"{m.get('symbol')}|{m.get('entry_time')}|{m.get('pattern')}": m for m in mirror
    }
    keys = sorted(set(th_by) | set(mi_by))
    recon = []
    for k in keys:
        th = th_by.get(k)
        mi = mi_by.get(k)
        reason = ""
        if th and not mi:
            reason = "signal existed but mirror row missing"
        elif mi and not th:
            reason = "mirror without theory"
        recon.append(
            {
                "signal_id": k,
                "theory_entry": th["entry_price"] if th else None,
                "mirror_entry": (mi or {}).get("actual_entry_price"),
                "entry_price_delta": (
                    float((mi or {}).get("actual_entry_price") or 0)
                    - float(th["entry_price"])
                    if th and mi and (mi or {}).get("actual_entry_price") is not None
                    else None
                ),
                "theory_pnl": th["pnl"] if th else None,
                "mirror_pnl": (mi or {}).get("pnl"),
                "pnl_delta_mirror_minus_theory": (
                    float((mi or {}).get("pnl") or 0) - float(th["pnl"] or 0)
                    if th and mi
                    else None
                ),
                "mapping_note": reason,
            }
        )
    _csv_write(
        OUT / "TRADE_RECONCILIATION.csv",
        recon,
        [
            "signal_id",
            "theory_entry",
            "mirror_entry",
            "entry_price_delta",
            "theory_pnl",
            "mirror_pnl",
            "pnl_delta_mirror_minus_theory",
            "mapping_note",
        ],
    )

    matched = sum(1 for r in recon if r["theory_entry"] is not None and r["mirror_entry"] is not None)
    fidelity = {
        "signal_gate_matched_over_total": f"{matched}/{len(recon)}",
        "theory_trade_count": len(theory),
        "mirror_trade_count": len(mirror),
        "live_trade_count": 0,
        "live_status": "SKIPPED_SINGLE_TERMINAL_WRONG_LEV_ON_52958398",
        "sum_theory_pnl": sum(float(t["pnl"] or 0) for t in theory if t["status"] == "closed"),
        "sum_mirror_pnl": sum(float(m.get("pnl") or 0) for m in mirror if m.get("status") == "closed"),
        "open_theory": sum(1 for t in theory if t["status"] == "open"),
        "open_mirror": sum(1 for m in mirror if m.get("status") == "open"),
        "exported_at_utc": _utc_now().isoformat(),
        **meta,
    }
    _csv_write(
        OUT / "FIDELITY_METRICS.csv",
        [fidelity],
        list(fidelity.keys()),
    )
    _write_json(OUT / "FIDELITY_METRICS.json", fidelity)
    _write_json(OUT / "runtime_snapshot.json", service.runtime)


def write_manifest(start: datetime, end_entries: datetime, snap: dict) -> None:
    manifest = {
        "experiment_id": RUN_ID,
        "started_utc": start.isoformat(),
        "new_entries_until_utc": end_entries.isoformat(),
        "observe_hours": OBSERVE_HOURS,
        "frozen_strategy": {
            "candidate": "n1_thursday",
            "strategy": "alexg8",
            "ghost_sl_mult": 4.0,
            "peer_blend": 0.0,
            "leverage": 500,
            "rr_mode": "dynamic",
            "rr_factor": 1.0,
            "rr_min": 2.0,
            "rr_max": 5.0,
            "min_rr": 2.0,
            "force_flat_daily": True,
            "force_flat_friday": False,
            "thursday_gate": True,
            "risk_include_commission": True,
            "commission_at_entry": False,
            "commission_per_lot": 7.0,
            "size_mode": "margin",
            "true_sl": True,
        },
        "accounts": {
            "MIRROR_DEMO_EXEC": {
                "login": 53074439,
                "server": "ICMarketsSC-Demo",
                "magic": 88002,
                "port": 8792,
                "leverage_verified": snap.get("leverage"),
                "balance_at_start": snap.get("balance"),
            },
            "LIVE": {
                "login": 52958398,
                "magic": 88001,
                "port": 8791,
                "status": "SKIPPED_SINGLE_TERMINAL",
                "reason": "Only one MT5 terminal; Live lev was 5000≠N1 500; terminal switched to 53074439",
            },
            "OLD_MIRROR": {
                "login": 53019882,
                "status": "NOT_USED",
                "protected_eurdkk_tickets": sorted(PROTECTED_EURDKK),
                "note": "Untouched; not logged in",
            },
        },
        "paths": {
            "BACKTEST": "Mirror ShadowEngine theory book (H1 incremental; same knobs)",
            "MIRROR": "MT5 market fills on 53074439 magic 88002",
            "LIVE": "not run this session",
        },
        "preexisting_positions": snap.get("positions") or [],
        "preexisting_orders": snap.get("orders") or [],
        "database": DB_URL,
        "rules": [
            "no September optimize/read for params",
            "no param mining",
            "stop NEW entries at T+6h; leave opens to natural exits",
            "never touch protected EURDKK tickets",
        ],
    }
    (OUT / "EXPERIMENT_MANIFEST.md").write_text(
        "# EXPERIMENT_MANIFEST\n\n```json\n"
        + json.dumps(manifest, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    _write_json(OUT / "EXPERIMENT_MANIFEST.json", manifest)


def main() -> int:
    start = _utc_now()
    end_entries = start + timedelta(hours=OBSERVE_HOURS)
    logger.info("Starting N1 validation | out=%s | until=%s", OUT, end_entries.isoformat())

    cfg = build_cfg()
    service = MirrorService(cfg)
    try:
        service.start(init_engine=True)
    except Exception:
        logger.error("start failed\n%s", traceback.format_exc())
        _write_json(OUT / "START_FAILURE.json", {"error": traceback.format_exc()})
        return 1

    snap = snapshot_mt5(service)
    _write_json(OUT / "PREEXISTING_STATE.json", snap)
    if int(snap.get("login") or 0) != 53074439:
        logger.error("Wrong account logged in: %s", snap.get("login"))
        service.stop()
        return 2
    if int(snap.get("leverage") or 0) != 500:
        logger.error("Wrong leverage: %s", snap.get("leverage"))
        service.stop()
        return 3
    if snap.get("protected_eurdkk_on_this_account"):
        logger.error("Protected tickets present on experiment account — abort")
        service.stop()
        return 4

    write_manifest(start, end_entries, snap)
    _write_json(
        OUT / "STATUS.json",
        {
            "phase": "observing_new_entries",
            "started_utc": start.isoformat(),
            "new_entries_until_utc": end_entries.isoformat(),
            "accept_new_entries": True,
        },
    )

    last_export = 0.0
    try:
        while True:
            now = _utc_now()
            if now >= end_entries and cfg.accept_new_entries:
                cfg.accept_new_entries = False
                logger.info("T+6h reached — stopping NEW entries; continuing reconcile")
                _write_json(
                    OUT / "STATUS.json",
                    {
                        "phase": "no_new_entries_reconciling",
                        "started_utc": start.isoformat(),
                        "new_entries_until_utc": end_entries.isoformat(),
                        "accept_new_entries": False,
                        "gate_closed_utc": now.isoformat(),
                    },
                )
                export_artifacts(
                    service,
                    {
                        "phase": "gate_closed",
                        "open_positions_at_6h": snapshot_mt5(service).get("positions"),
                    },
                )
                _write_json(
                    OUT / "OPEN_POSITIONS_AT_6H.json",
                    snapshot_mt5(service),
                )
                # Keep reconciling closes for up to +2h after gate, then finalize
            hard_stop = end_entries + timedelta(hours=2)
            if now >= hard_stop:
                logger.info("Hard stop after +2h post-gate — writing FINAL_RESULTS")
                break

            try:
                stats = service.process_once()
                logger.info("tick %s", stats)
            except Exception:
                logger.exception("process_once failed")

            if time.time() - last_export > 300:
                export_artifacts(service, {"phase": "periodic"})
                last_export = time.time()

            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Interrupted — exporting")
    finally:
        export_artifacts(service, {"phase": "final"})
        snap_end = snapshot_mt5(service)
        _write_json(OUT / "FINAL_ACCOUNT_SNAPSHOT.json", snap_end)
        final = {
            "experiment_id": RUN_ID,
            "started_utc": start.isoformat(),
            "ended_utc": _utc_now().isoformat(),
            "new_entries_until_utc": end_entries.isoformat(),
            "accept_new_entries_final": cfg.accept_new_entries,
            "account_end": snap_end,
            "runtime": service.runtime,
            "artifacts": [
                "EXPERIMENT_MANIFEST.md",
                "SIGNAL_LOG.csv",
                "BACKTEST_TRADES.csv",
                "MIRROR_TRADES.csv",
                "LIVE_TRADES.csv",
                "TRADE_RECONCILIATION.csv",
                "FIDELITY_METRICS.csv",
                "OPEN_POSITIONS_AT_6H.json",
                "harness.log",
            ],
            "interpretation": (
                "Sample fidelity evidence only — not strategy confirmation. "
                "LIVE path skipped (single terminal). BACKTEST=theory shadow; MIRROR=MT5 fills."
            ),
        }
        _write_json(OUT / "FINAL_RESULTS.json", final)
        (OUT / "FINAL_RESULTS.md").write_text(
            "# FINAL_RESULTS\n\n```json\n" + json.dumps(final, indent=2, default=str) + "\n```\n",
            encoding="utf-8",
        )
        _write_json(
            OUT / "STATUS.json",
            {"phase": "stopped", "ended_utc": _utc_now().isoformat()},
        )
        try:
            service.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
