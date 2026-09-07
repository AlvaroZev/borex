#!/usr/bin/env python3
"""This week (Mon 2026-08-10 → now): live DB/MT5 vs alexg7aligned theoretical."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")

EVAL_START = pd.Timestamp("2026-08-10T00:00:00+00:00")
EVAL_END = pd.Timestamp(datetime.now(timezone.utc))
LOAD_START = EVAL_START - timedelta(days=7)
CAPITAL = 889.69
OUT = ROOT / "data" / "runs" / "week_live_vs_theory_aug10"


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _iso(x) -> str:
    return _ts(x).isoformat()


def dump_db(url: str, label: str) -> dict:
    from borex_live.store.models import (
        LiveTrade,
        PendingGhost,
        ServiceEvent,
        init_db,
    )

    if not url:
        print(f"[{label}] no URL", flush=True)
        return {"trades": [], "ghosts": [], "events": []}
    Session = init_db(url)
    with Session() as s:
        trades = []
        for t in s.query(LiveTrade).order_by(LiveTrade.id.asc()).all():
            trades.append(
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "side": t.side,
                    "status": t.status,
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time,
                    "entry": t.entry_price,
                    "sl": t.stop_loss,
                    "tp": t.take_profit,
                    "pnl": t.pnl,
                    "ticket": t.mt5_ticket,
                    "rr": t.rr_used,
                    "exit_reason": t.exit_reason,
                    "pattern": (t.pattern or "")[:140],
                }
            )
        ghosts = []
        for g in s.query(PendingGhost).order_by(PendingGhost.id.asc()).all():
            ghosts.append(
                {
                    "id": g.id,
                    "symbol": g.symbol,
                    "action": g.action,
                    "status": g.status,
                    "planned": g.planned_entry,
                    "sl": g.stop_loss,
                    "tp": g.take_profit,
                    "created_at": str(getattr(g, "created_at", "")),
                }
            )
        events = []
        for e in s.query(ServiceEvent).order_by(ServiceEvent.id.asc()).all():
            events.append(
                {
                    "ts": str(e.ts),
                    "kind": e.kind,
                    "message": (e.message or "")[:180],
                }
            )
    print(
        f"[{label}] trades={len(trades)} ghosts={len(ghosts)} events={len(events)} "
        f"trade_symbols={Counter(t['symbol'] for t in trades)} "
        f"ghost_symbols={Counter(g['symbol'] for g in ghosts)}",
        flush=True,
    )
    return {"trades": trades, "ghosts": ghosts, "events": events}


def dump_mt5_deals() -> list[dict]:
    from borex_live.mt5.client import Mt5Client

    path = os.environ.get("MT5_PATH", "")
    login = int(os.environ.get("MT5_LOGIN", "0") or 0)
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_DEMO_SERVER") or os.environ.get("MT5_SERVER", "")
    client = Mt5Client(path=path, login=login, password=password, server=server)
    client.connect()
    try:
        mt5 = client._mt5
        start = EVAL_START.to_pydatetime() - timedelta(days=1)
        end = datetime.now(timezone.utc) + timedelta(hours=1)
        deals = mt5.history_deals_get(start, end)
        rows = []
        if deals:
            for d in deals:
                comment = d.comment or ""
                rows.append(
                    {
                        "ticket": d.ticket,
                        "order": d.order,
                        "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                        "symbol": d.symbol,
                        "type": d.type,
                        "entry": d.entry,  # 0 in, 1 out
                        "volume": d.volume,
                        "price": d.price,
                        "profit": d.profit,
                        "commission": d.commission,
                        "swap": d.swap,
                        "magic": d.magic,
                        "comment": comment,
                        "position_id": d.position_id,
                    }
                )
        print(f"[mt5] deals={len(rows)}", flush=True)
        by_magic = Counter(r["magic"] for r in rows)
        by_comment = Counter(
            ("smoke" if "smoke" in r["comment"].lower() else "borex" if "bx_" in r["comment"].lower() or r["magic"] == 88001 else "other")
            for r in rows
        )
        print(f"[mt5] magic={dict(by_magic)} kind={dict(by_comment)}", flush=True)
        pos_in = [r for r in rows if r["entry"] == 0 and r["magic"] in (88001, 77001)]
        print("[mt5] entries (magic 88001 live / 77001 smoke):", flush=True)
        for r in pos_in:
            print(
                f"  {r['time'][:16]} {r['symbol']:8} mag={r['magic']} vol={r['volume']} "
                f"px={r['price']} cmt={r['comment'][:40]}",
                flush=True,
            )
        return rows
    finally:
        client.disconnect()


def run_theory() -> list[dict]:
    from borex.alexg import AlexG7AlignedStrategy
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine
    from borex.viewerMT5.mt5_feed import (
        connect_mt5_client,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
    )

    symbols = list_tradeable_yahoo_symbols()
    client = connect_mt5_client()
    mt5_all: dict = {}
    try:
        for i, sym in enumerate(symbols, 1):
            try:
                candles = fetch_mt5_candles(
                    sym,
                    "1h",
                    LOAD_START.to_pydatetime(),
                    (EVAL_END + timedelta(hours=2)).to_pydatetime(),
                    client=client,
                    use_cache=True,
                    write_cache=True,
                )
                if len(candles) >= 80:
                    mt5_all[sym] = candles
            except Exception as exc:
                if i <= 5:
                    print(f"  skip {sym}: {exc}", flush=True)
            if i % 15 == 0 or i == len(symbols):
                print(f"  MT5 loaded {len(mt5_all)}/{i}", flush=True)
    finally:
        client.disconnect()

    print(f"pairs={len(mt5_all)}", flush=True)
    strategy = AlexG7AlignedStrategy(min_rr=3.0, execution_interval="1h", peer_blend=0.0)
    config = BacktestConfig(
        initial_capital=CAPITAL,
        leverage=5000.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=3.0,
        rr_mode="dynamic",
        rr_factor=1.88,
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        risk_include_commission=True,
    )
    master = pick_master_symbol(mt5_all)
    engine = MultiMarketEngine(strategy, config, max_positions=60)
    result = engine.run(mt5_all, timeframe="1h", master_symbol=master, same_bar_exit=False)
    week = []
    for t in result.trades:
        et = getattr(t, "entry_time", None)
        if not et:
            continue
        et = _ts(et)
        if EVAL_START <= et < EVAL_END:
            week.append(
                {
                    "symbol": t.symbol,
                    "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                    "entry_time": _iso(t.entry_time),
                    "exit_time": _iso(t.exit_time) if t.exit_time else "",
                    "entry": float(t.entry_price),
                    "sl": float(t.stop_loss) if t.stop_loss is not None else None,
                    "tp": float(t.take_profit) if t.take_profit is not None else None,
                    "pnl": float(t.pnl or 0),
                    "exit_reason": t.exit_reason or "",
                    "pattern": (t.pattern or "")[:140],
                }
            )
    print(
        f"THEORY week trades={len(week)} symbols={Counter(r['symbol'] for r in week)}",
        flush=True,
    )
    for r in week:
        print(
            f"  {r['entry_time'][:16]} {r['symbol']:10} {r['side']:5} "
            f"E={r['entry']} SL={r['sl']} TP={r['tp']} pnl={r['pnl']:+.2f} {r['exit_reason']}",
            flush=True,
        )
    return week


def main() -> int:
    print(f"Window {EVAL_START} → {EVAL_END}", flush=True)
    local = dump_db(os.environ.get("DATABASE_URL", ""), "local")
    backup = dump_db(
        os.environ.get("DATABASE_BACKUP_URL", "") or os.environ.get("RAILWAY_DATABASE_URL", ""),
        "railway",
    )
    mt5_deals = dump_mt5_deals()
    theory = run_theory()

    live_week = []
    for t in local["trades"]:
        if not t.get("entry_time"):
            continue
        try:
            et = _ts(t["entry_time"])
        except Exception:
            continue
        if EVAL_START <= et < EVAL_END:
            live_week.append(t)

    aud_theory = [r for r in theory if "AUDCAD" in r["symbol"]]
    aud_live = [t for t in live_week if "AUDCAD" in t["symbol"]]

    print("\n=== AUDCAD theory vs live ===", flush=True)
    print(f"theory n={len(aud_theory)} live n={len(aud_live)}", flush=True)
    for r in aud_theory:
        print(f"  T {r['entry_time'][:16]} {r['side']} E={r['entry']:.5f} SL={r['sl']} TP={r['tp']} pnl={r['pnl']:+.2f}", flush=True)
    for t in aud_live:
        print(
            f"  L {str(t['entry_time'])[:16]} {t['side']} E={t['entry']} SL={t['sl']} TP={t['tp']} "
            f"pnl={t['pnl']} ticket={t['ticket']} {t['status']}",
            flush=True,
        )

    ghost_kinds = Counter(e["kind"] for e in local["events"])
    ghost_msgs = [e for e in local["events"] if "ghost" in (e["kind"] or "").lower() or "ghost" in (e["message"] or "").lower()]
    print(f"\nevents kinds={dict(ghost_kinds)} ghost-ish={len(ghost_msgs)}", flush=True)
    for e in ghost_msgs:
        print(f"  {e['ts'][:19]} {e['kind']} {e['message']}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "window": {"start": _iso(EVAL_START), "end": _iso(EVAL_END)},
        "local": local,
        "railway": backup,
        "mt5_deals": mt5_deals,
        "theory": theory,
        "audcad_theory": aud_theory,
        "audcad_live": aud_live,
    }
    path = OUT / "report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
