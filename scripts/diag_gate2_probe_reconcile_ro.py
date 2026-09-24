"""READ-ONLY reconciliation diagnostic for Gate 2 controlled probe batch.

Places ZERO orders. Does not close/modify positions. Mirror demo only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path[:0] = [str(ROOT), str(LIVE)]

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")

from borex.data.mt5_time import (  # noqa: E402
    measure_mt5_server_offset_seconds,
    mt5_unix_to_utc,
    snap_server_offset_seconds,
    utc_to_mt5_request,
)
from borex_live.mt5.client import Mt5Client  # noqa: E402

EXPECTED_LOGIN = 53019882
FORBIDDEN_LIVE = 52958398
REQUIRED_MAGIC = 88002
TICKETS = [1949041505, 1949043224, 1949045181, 1949046271, 1949049134, 1949051052]
BATCH_START = datetime(2026, 9, 21, 17, 42, 0, tzinfo=timezone.utc)
BATCH_END = datetime(2026, 9, 21, 17, 50, 0, tzinfo=timezone.utc)
OUT = ROOT / "data" / "runs" / "gate2_controlled_probe" / "batches" / "g2p_20260921_174237" / "reconcile_ro.json"


def connect() -> Mt5Client:
    path = (
        os.environ.get("MIRROR_MT5_PATH")
        or os.environ.get("MT5_PATH")
        or r"C:\Program Files\MetaTrader 5\terminal64.exe"
    )
    login = int(os.environ.get("MIRROR_MT5_LOGIN") or os.environ.get("MT5_LOGIN") or 0)
    password = os.environ.get("MIRROR_MT5_PASSWORD") or os.environ.get("MT5_PASSWORD") or ""
    server = os.environ.get("MIRROR_MT5_SERVER") or os.environ.get("MT5_SERVER") or ""
    if login != EXPECTED_LOGIN:
        raise SystemExit(f"REFUSE: login env {login} != Mirror {EXPECTED_LOGIN}")
    client = Mt5Client(path=path, login=login, password=password, server=server)
    client.MAGIC = REQUIRED_MAGIC
    client.connect()
    ai = client._mt5.account_info()
    if ai is None or int(ai.login) == FORBIDDEN_LIVE or int(ai.login) != EXPECTED_LOGIN:
        raise SystemExit(f"REFUSE: account_info login={getattr(ai, 'login', None)}")
    return client


def deal_row(d, offset: int) -> dict:
    raw = int(d.time)
    return {
        "ticket": int(d.ticket),
        "order": int(getattr(d, "order", 0) or 0),
        "position_id": int(getattr(d, "position_id", 0) or 0),
        "symbol": str(d.symbol),
        "type": int(d.type),
        "entry": int(getattr(d, "entry", 0) or 0),
        "volume": float(d.volume),
        "price": float(d.price),
        "profit": float(d.profit),
        "commission": float(getattr(d, "commission", 0) or 0),
        "swap": float(getattr(d, "swap", 0) or 0),
        "magic": int(getattr(d, "magic", 0) or 0),
        "comment": str(getattr(d, "comment", "") or ""),
        "time_unix_raw": raw,
        "time_utc_with_offset": mt5_unix_to_utc(raw, offset).isoformat(),
        "time_as_if_utc": datetime.fromtimestamp(raw, tz=timezone.utc).isoformat(),
    }


def order_row(o, offset: int) -> dict:
    raw = int(o.time_setup) if getattr(o, "time_setup", None) else int(getattr(o, "time_done", 0) or 0)
    return {
        "ticket": int(o.ticket),
        "position_id": int(getattr(o, "position_id", 0) or 0),
        "symbol": str(o.symbol),
        "type": int(o.type),
        "state": int(getattr(o, "state", -1)),
        "volume_initial": float(getattr(o, "volume_initial", 0) or 0),
        "price_open": float(getattr(o, "price_open", 0) or 0),
        "sl": float(getattr(o, "sl", 0) or 0),
        "tp": float(getattr(o, "tp", 0) or 0),
        "magic": int(getattr(o, "magic", 0) or 0),
        "comment": str(getattr(o, "comment", "") or ""),
        "time_setup_unix_raw": int(getattr(o, "time_setup", 0) or 0),
        "time_done_unix_raw": int(getattr(o, "time_done", 0) or 0),
        "time_setup_utc": mt5_unix_to_utc(int(getattr(o, "time_setup", 0) or 0), offset).isoformat()
        if getattr(o, "time_setup", None)
        else None,
    }


def main() -> None:
    client = connect()
    mt5 = client._mt5
    ai = mt5.account_info()
    now_utc = datetime.now(timezone.utc)
    now_unix = int(time.time())

    tick = mt5.symbol_info_tick("EURUSD")
    tick_unix = int(tick.time) if tick else None
    raw_off = (tick_unix - now_unix) if tick_unix else None
    snapped = snap_server_offset_seconds(raw_off) if raw_off is not None else None
    measured = measure_mt5_server_offset_seconds(mt5)
    client_off = int(getattr(client, "server_offset_seconds", 0) or 0)

    # Batch-time tick from artifact for independent check
    artifact_tick = 1789775819
    artifact_utc_approx = int(datetime(2026, 9, 21, 17, 42, 37, tzinfo=timezone.utc).timestamp())
    artifact_raw_off = artifact_tick - artifact_utc_approx

    open_pos = mt5.positions_get() or []
    open_by_ticket = {int(p.ticket): p for p in open_pos}
    probe_open = [int(p.ticket) for p in open_pos if int(p.ticket) in TICKETS]
    eurdkk = [
        {"ticket": int(p.ticket), "symbol": str(p.symbol), "magic": int(p.magic)}
        for p in open_pos
        if str(p.symbol).upper().startswith("EURDKK")
    ]

    # --- history windows ---
    windows = {}
    for label, offset in [("offset_0", 0), ("offset_10800", 10800), ("client_offset", client_off)]:
        req0 = utc_to_mt5_request(BATCH_START - timedelta(hours=1), offset)
        req1 = utc_to_mt5_request(BATCH_END + timedelta(hours=1), offset)
        # also wide UTC-naive window covering possible server clock
        deals_range = mt5.history_deals_get(req0, req1)
        orders_range = mt5.history_orders_get(req0, req1)
        err = mt5.last_error()
        windows[label] = {
            "req0": req0.isoformat(),
            "req1": req1.isoformat(),
            "offset_used": offset,
            "n_deals_raw": 0 if deals_range is None else len(deals_range),
            "n_orders_raw": 0 if orders_range is None else len(orders_range),
            "last_error": list(err) if err else None,
            "deals_matching_tickets": [],
            "orders_matching_tickets": [],
            "deals_magic_88002": [],
        }
        if deals_range:
            for d in deals_range:
                row = deal_row(d, offset)
                if row["position_id"] in TICKETS or row["ticket"] in TICKETS or row["order"] in TICKETS:
                    windows[label]["deals_matching_tickets"].append(row)
                if row["magic"] == REQUIRED_MAGIC and row["symbol"] == "EURUSD":
                    windows[label]["deals_magic_88002"].append(row)
        if orders_range:
            for o in orders_range:
                row = order_row(o, offset)
                if row["ticket"] in TICKETS or row["position_id"] in TICKETS:
                    windows[label]["orders_matching_tickets"].append(row)

    # history_deals_get(position=...) — preferred ticket-scoped lookup
    # (this MetaTrader5 build has no history_select attribute)
    select_tests = {
        "api_note": "MetaTrader5 module has no history_select; using history_deals_get range + position=",
        "by_position_ticket": {},
        "by_order_ticket": {},
    }
    for t in TICKETS:
        got = None
        err = None
        try:
            got = mt5.history_deals_get(position=t)
            err = mt5.last_error()
        except TypeError as e:
            err = ["TypeError", str(e)]
        select_tests["by_position_ticket"][str(t)] = {
            "n": 0 if got is None else len(got),
            "last_error": list(err) if isinstance(err, tuple) else err,
            "deals": [deal_row(d, client_off) for d in (got or [])],
        }
        got_o = None
        try:
            got_o = mt5.history_orders_get(position=t)
        except TypeError as e:
            got_o = None
            err = ["TypeError", str(e)]
        select_tests["by_order_ticket"][str(t)] = {
            "n_orders": 0 if got_o is None else len(got_o),
            "orders": [order_row(o, client_off) for o in (got_o or [])],
        }

    # Reproduce harness _deals_for_position exactly (narrow window, magic filter, offset=client)
    harness_repro = {}
    for t in TICKETS:
        since = BATCH_START
        offset = client_off
        req0 = utc_to_mt5_request(since - timedelta(seconds=30), offset)
        req1 = utc_to_mt5_request(BATCH_END + timedelta(seconds=5), offset)
        deals = mt5.history_deals_get(req0, req1) or []
        matched = []
        for d in deals:
            if int(getattr(d, "magic", 0) or 0) != REQUIRED_MAGIC:
                continue
            if int(getattr(d, "position_id", 0) or 0) != t:
                continue
            matched.append(deal_row(d, offset))
        harness_repro[str(t)] = {
            "req0": req0.isoformat(),
            "req1": req1.isoformat(),
            "offset": offset,
            "n_raw_in_window": len(deals),
            "n_matched": len(matched),
            "matched": matched,
        }

    # Wide absolute unix-naive datetime sweep (no offset conversion)
    wide0 = datetime(2026, 9, 21, 0, 0, 0)
    wide1 = datetime(2026, 9, 22, 0, 0, 0)
    wide_deals = mt5.history_deals_get(wide0, wide1)
    wide_orders = mt5.history_orders_get(wide0, wide1)
    wide_match_deals = [
        deal_row(d, client_off)
        for d in (wide_deals or [])
        if int(getattr(d, "position_id", 0) or 0) in TICKETS
        or int(d.ticket) in TICKETS
        or int(getattr(d, "order", 0) or 0) in TICKETS
    ]
    wide_match_orders = [
        order_row(o, client_off)
        for o in (wide_orders or [])
        if int(o.ticket) in TICKETS or int(getattr(o, "position_id", 0) or 0) in TICKETS
    ]
    # also comment-prefix search
    comment_hits = [
        deal_row(d, client_off)
        for d in (wide_deals or [])
        if "g2p" in str(getattr(d, "comment", "") or "").lower()
        or "bx_g2p" in str(getattr(d, "comment", "") or "").lower()
    ]

    report = {
        "mode": "read-only-reconcile",
        "batch_id": "g2p_20260921_174237",
        "generated_at_utc": now_utc.isoformat(),
        "new_orders_placed": 0,
        "account": {
            "login": int(ai.login),
            "server": str(ai.server),
            "balance": float(ai.balance),
            "equity": float(ai.equity),
            "magic_client": REQUIRED_MAGIC,
            "login_verified": int(ai.login) == EXPECTED_LOGIN,
            "live_account_protected": int(ai.login) != FORBIDDEN_LIVE,
        },
        "offset": {
            "now_unix": now_unix,
            "now_utc": now_utc.isoformat(),
            "tick_unix_raw": tick_unix,
            "tick_time_as_if_utc": datetime.fromtimestamp(tick_unix, tz=timezone.utc).isoformat()
            if tick_unix
            else None,
            "tick_bid": float(tick.bid) if tick else None,
            "tick_ask": float(tick.ask) if tick else None,
            "raw_offset_tick_minus_now": raw_off,
            "snapped_offset": snapped,
            "measure_mt5_server_offset_seconds": measured,
            "client_server_offset_seconds": client_off,
            "artifact_tick_time_unix_raw": artifact_tick,
            "artifact_utc_approx_unix": artifact_utc_approx,
            "artifact_raw_offset": artifact_raw_off,
            "artifact_snapped": snap_server_offset_seconds(artifact_raw_off),
            "prior_mirror_evidence_offset": 10800,
        },
        "open_positions": {
            "total": len(open_pos),
            "probe_tickets_still_open": probe_open,
            "eurdkk_protected": eurdkk,
            "eurdkk_count": len(eurdkk),
        },
        "windows": windows,
        "select_tests": select_tests,
        "harness_repro_narrow": harness_repro,
        "wide_calendar_day": {
            "wide0": wide0.isoformat(),
            "wide1": wide1.isoformat(),
            "n_deals": 0 if wide_deals is None else len(wide_deals),
            "n_orders": 0 if wide_orders is None else len(wide_orders),
            "matched_deals": wide_match_deals,
            "matched_orders": wide_match_orders,
            "comment_g2p_hits": comment_hits,
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nArtifact: {OUT}")
    # disconnect without shutdown if other apps need terminal — still OK for RO
    client._connected = False
    client._mt5 = None


if __name__ == "__main__":
    main()
