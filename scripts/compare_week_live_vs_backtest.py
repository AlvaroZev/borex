#!/usr/bin/env python3
"""Compare this trading week: MT5 backtest vs Dukascopy vs live DB/MT5 deals.

Live-matching params: alexg7aligned, min-rr 3, rr-factor 1.88, $1k, 5000x,
position-size 1%, same-bar off, commission $7/lot RT + risk-include-commission.
Data window = eval week + 1 week warmup.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LIVE_ROOT = ROOT.parent / "borex_live"
OUT_DIR = ROOT / "data" / "runs" / "week_compare_2026w31"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE_ROOT))

from dotenv import load_dotenv

load_dotenv(LIVE_ROOT / ".env")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _iso(x) -> str:
    return _ts(x).isoformat()


def trade_row(t, source: str) -> dict:
    entry = getattr(t, "entry_time", None) or getattr(t, "timestamp", None)
    exit_t = getattr(t, "exit_time", None)
    return {
        "source": source,
        "symbol": getattr(t, "symbol", ""),
        "side": getattr(t, "side", None).value
        if hasattr(getattr(t, "side", None), "value")
        else str(getattr(t, "side", "")),
        "entry_time": _iso(entry) if entry else "",
        "exit_time": _iso(exit_t) if exit_t else "",
        "entry": float(getattr(t, "entry_price", 0) or 0),
        "sl": float(t.stop_loss) if getattr(t, "stop_loss", None) is not None else None,
        "tp": float(t.take_profit) if getattr(t, "take_profit", None) is not None else None,
        "pnl": float(getattr(t, "pnl", 0) or 0),
        "margin": float(getattr(t, "margin", 0) or 0),
        "exit_reason": getattr(t, "exit_reason", "") or "",
        "pattern": (getattr(t, "pattern", "") or "")[:120],
        "rr_used": float(getattr(t, "rr_used", 0) or getattr(t, "score", 0) or 0),
        "ticket": getattr(t, "mt5_ticket", None),
    }


def filter_week(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    out = []
    for r in rows:
        if not r.get("entry_time"):
            continue
        et = _ts(r["entry_time"])
        if start <= et < end:
            out.append(r)
    return out


def summarize(rows: list[dict], capital: float) -> dict:
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(rows)) if rows else 0.0,
        "pnl_sum": total,
        "return_pct": (total / capital) if capital else 0.0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
    }


def load_live_db_trades(start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    from borex_live.store.models import LiveTrade, init_db

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("No DATABASE_URL — skipping live DB trades", flush=True)
        return []
    Session = init_db(url)
    session = Session()
    try:
        rows = session.query(LiveTrade).order_by(LiveTrade.id).all()
        all_rows = []
        for t in rows:
            side = t.side
            all_rows.append(
                {
                    "source": "live_db",
                    "symbol": t.symbol,
                    "side": side,
                    "entry_time": t.entry_time or "",
                    "exit_time": t.exit_time or "",
                    "entry": float(t.entry_price or 0),
                    "sl": float(t.stop_loss) if t.stop_loss is not None else None,
                    "tp": float(t.take_profit) if t.take_profit is not None else None,
                    "pnl": float(t.pnl or 0),
                    "margin": float(t.margin or 0),
                    "exit_reason": t.exit_reason or "",
                    "pattern": (t.pattern or "")[:120],
                    "rr_used": float(t.rr_used or 0),
                    "ticket": t.mt5_ticket,
                    "status": t.status,
                }
            )
        return filter_week(all_rows, start, end)
    finally:
        session.close()


def load_mt5_deals(start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """Broker deal history for borex magic (position open/close pairs)."""
    from borex.viewerMT5.mt5_feed import connect_mt5_client, ensure_live_on_path

    ensure_live_on_path()
    client = connect_mt5_client()
    mt5 = client._mt5
    magic = int(getattr(client, "MAGIC", 20250731))
    deals = mt5.history_deals_get(start.to_pydatetime(), end.to_pydatetime())
    if not deals:
        return []

    by_pos: dict[int, list] = {}
    for d in deals:
        d_magic = int(getattr(d, "magic", 0) or 0)
        comment = str(getattr(d, "comment", "") or "").lower()
        if d_magic != magic and "borex" not in comment:
            continue
        pid = int(getattr(d, "position_id", 0) or 0)
        if pid <= 0:
            continue
        by_pos.setdefault(pid, []).append(d)

    rows = []
    for pid, ds in by_pos.items():
        ds = sorted(ds, key=lambda x: int(x.time))
        ins = [d for d in ds if int(getattr(d, "entry", -1)) == 0]
        outs = [d for d in ds if int(getattr(d, "entry", -1)) == 1]
        if not ins or not outs:
            continue
        d0 = ins[0]
        d1 = outs[-1]
        sym = str(d0.symbol)
        yahoo = sym if sym.endswith("=X") else f"{sym}=X"
        side = "long" if int(d0.type) == 0 else "short"
        pnl = sum(
            float(d.profit or 0) + float(d.swap or 0) + float(d.commission or 0)
            for d in outs
        )
        rows.append(
            {
                "source": "mt5_deals",
                "symbol": yahoo,
                "side": side,
                "entry_time": _iso(datetime.fromtimestamp(int(d0.time), tz=timezone.utc)),
                "exit_time": _iso(datetime.fromtimestamp(int(d1.time), tz=timezone.utc)),
                "entry": float(d0.price),
                "sl": None,
                "tp": None,
                "pnl": pnl,
                "margin": 0.0,
                "exit_reason": "broker",
                "pattern": "",
                "rr_used": 0.0,
                "ticket": pid,
                "volume": float(d0.volume),
            }
        )
    return filter_week(rows, start, end)


def run_backtest(candles_by_symbol: dict, label: str, capital: float):
    from borex.alexg import AlexG7AlignedStrategy
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    strategy = AlexG7AlignedStrategy(
        min_rr=3.0,
        execution_interval="1h",
        peer_blend=0.0,
    )
    config = BacktestConfig(
        initial_capital=capital,
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
    master = pick_master_symbol(candles_by_symbol)
    print(
        f"[{label}] pairs={len(candles_by_symbol)} master={master} "
        f"bars={len(candles_by_symbol[master])}",
        flush=True,
    )
    engine = MultiMarketEngine(strategy, config, max_positions=60)
    result = engine.run(
        candles_by_symbol,
        timeframe="1h",
        master_symbol=master,
        same_bar_exit=False,
    )
    rows = [trade_row(t, label) for t in result.trades]
    return result, rows


def slice_candles(candles_by_symbol: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    out = {}
    for sym, candles in candles_by_symbol.items():
        sliced = [c for c in candles if start <= _ts(c.timestamp) < end]
        if len(sliced) >= 120:
            out[sym] = sliced
    return out


def main() -> int:
    # Last completed FX week relative to "now" (Sun Aug 2 2026 → Mon Jul 27 – Sat Aug 2)
    now = datetime.now(timezone.utc)
    # Eval: Monday 00:00 UTC of last week through Saturday 00:00 (covers Fri session)
    today = now.date()
    weekday = today.weekday()  # Mon=0 … Sun=6
    # Most recently completed Mon–Fri block
    if weekday >= 5:  # Sat/Sun → Monday of the week that just ended
        eval_monday = today - timedelta(days=weekday)
    else:
        eval_monday = today - timedelta(days=weekday + 7)
    eval_start = pd.Timestamp(
        datetime(eval_monday.year, eval_monday.month, eval_monday.day, tzinfo=timezone.utc)
    )
    eval_end = eval_start + timedelta(days=6)  # Sat 00:00 — includes Fri session
    load_start = eval_start - timedelta(days=7)
    load_end = eval_end + timedelta(days=1)

    capital = 1000.0
    print(
        f"Eval week: {eval_start} → {eval_end}\n"
        f"Load (warmup+eval): {load_start} → {load_end}",
        flush=True,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- Live DB + MT5 deals ---
    live_db = load_live_db_trades(eval_start, eval_end)
    print(f"Live DB trades in week: {len(live_db)}", flush=True)
    try:
        mt5_deals = load_mt5_deals(eval_start, eval_end)
    except Exception as exc:
        print(f"MT5 deals load failed: {exc}", flush=True)
        mt5_deals = []
    print(f"MT5 borex deals in week: {len(mt5_deals)}", flush=True)

    # --- Load symbols ---
    from borex.viewerMT5.mt5_feed import (
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
        connect_mt5_client,
        ensure_live_on_path,
    )
    from borex.data.loader import load_market_data

    ensure_live_on_path()
    symbols = list_tradeable_yahoo_symbols()
    print(f"Universe: {len(symbols)} pairs", flush=True)

    # --- MT5 data ---
    client = connect_mt5_client()
    mt5_candles = {}
    try:
        for i, sym in enumerate(symbols, 1):
            try:
                candles = fetch_mt5_candles(
                    sym,
                    "1h",
                    load_start.to_pydatetime(),
                    load_end.to_pydatetime(),
                    client=client,
                    use_cache=True,
                    write_cache=True,
                )
            except Exception as exc:
                print(f"  MT5 skip {sym}: {exc}", flush=True)
                continue
            if len(candles) >= 120:
                mt5_candles[sym] = candles
            if i % 10 == 0 or i == len(symbols):
                print(f"  MT5 loaded {len(mt5_candles)}/{i}", flush=True)
    finally:
        pass

    # --- Dukascopy ---
    duka_candles = {}
    for i, sym in enumerate(symbols, 1):
        try:
            # Load a bit more via period then slice — cache may be deep
            candles = load_market_data(sym, "30d", "1h", cache_mode="auto")
            sliced = [c for c in candles if load_start <= _ts(c.timestamp) < load_end]
            if len(sliced) >= 120:
                duka_candles[sym] = sliced
        except Exception as exc:
            if i <= 5:
                print(f"  Duka skip {sym}: {exc}", flush=True)
            continue
        if i % 10 == 0 or i == len(symbols):
            print(f"  Duka loaded {len(duka_candles)}/{i}", flush=True)

    # Intersect symbols present in both for fairer pair-count comparison note
    common = sorted(set(mt5_candles) & set(duka_candles))
    print(
        f"Pairs: mt5={len(mt5_candles)} duka={len(duka_candles)} common={len(common)}",
        flush=True,
    )

    mt5_result, mt5_all = run_backtest(mt5_candles, "bt_mt5", capital)
    duka_result, duka_all = run_backtest(duka_candles, "bt_duka", capital)

    mt5_week = filter_week(mt5_all, eval_start, eval_end)
    duka_week = filter_week(duka_all, eval_start, eval_end)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "strategy": "alexg7aligned",
            "min_rr": 3.0,
            "rr_factor": 1.88,
            "rr_mode": "dynamic",
            "capital": capital,
            "leverage": 5000,
            "position_size": 0.01,
            "same_bar_exit": False,
            "commission_per_lot": 7.0,
            "risk_include_commission": True,
            "max_positions": 60,
            "peer_blend": 0.0,
        },
        "window": {
            "eval_start": _iso(eval_start),
            "eval_end": _iso(eval_end),
            "load_start": _iso(load_start),
            "load_end": _iso(load_end),
            "warmup_days": 7,
        },
        "coverage": {
            "mt5_pairs": len(mt5_candles),
            "duka_pairs": len(duka_candles),
            "common_pairs": len(common),
        },
        "summary": {
            "live_db": summarize(live_db, capital),
            "mt5_deals": summarize(mt5_deals, capital),
            "bt_mt5": summarize(mt5_week, capital),
            "bt_duka": summarize(duka_week, capital),
        },
        "full_period_bt_trades": {
            "bt_mt5": len(mt5_all),
            "bt_duka": len(duka_all),
        },
        "trades": {
            "live_db": live_db,
            "mt5_deals": mt5_deals,
            "bt_mt5": mt5_week,
            "bt_duka": duka_week,
        },
        "bt_equity": {
            "bt_mt5_final": float(mt5_result.final_equity),
            "bt_duka_final": float(duka_result.final_equity),
            "bt_mt5_return_full_window": float(mt5_result.total_return_pct),
            "bt_duka_return_full_window": float(duka_result.total_return_pct),
            "note": "final equity includes warmup-week trades; week return_pct uses sum of eval-week trade PnL / capital",
        },
    }

    out_path = OUT_DIR / "report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    print(json.dumps(report["summary"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
