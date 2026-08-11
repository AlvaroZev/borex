#!/usr/bin/env python3
"""Last completed week: alexg7aligned on MT5 (live-matching params)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "deploy" / "borex_live"
SIBLING_LIVE = ROOT.parent / "borex_live"
OUT = ROOT / "data" / "runs" / "week_live_whatif"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))

from dotenv import load_dotenv

load_dotenv(SIBLING_LIVE / ".env")
load_dotenv(LIVE / ".env")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _iso(x) -> str:
    return _ts(x).isoformat()


def last_completed_week() -> tuple[pd.Timestamp, pd.Timestamp]:
    today = datetime.now(timezone.utc).date()
    weekday = today.weekday()
    if weekday >= 5:
        latest_monday = today - timedelta(days=weekday)
    else:
        latest_monday = today - timedelta(days=weekday + 7)
    start = pd.Timestamp(datetime(latest_monday.year, latest_monday.month, latest_monday.day, tzinfo=timezone.utc))
    end = start + timedelta(days=6)
    return start, end


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


def trade_row(t) -> dict:
    entry = getattr(t, "entry_time", None)
    exit_t = getattr(t, "exit_time", None)
    return {
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
        "pattern": (getattr(t, "pattern", "") or "")[:140],
    }


def main() -> int:
    capital = 1000.0
    eval_start, eval_end = last_completed_week()
    load_start = eval_start - timedelta(days=7)
    load_end = eval_end + timedelta(hours=12)

    print(
        f"What-if week (alexg7aligned / MT5): {eval_start.date()} → {eval_end.date()}\n"
        f"Load: {load_start.date()} → {load_end.date()}",
        flush=True,
    )

    from borex.alexg import AlexG7AlignedStrategy
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine
    from borex.viewerMT5.mt5_feed import (
        connect_mt5_client,
        ensure_live_on_path,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
    )

    ensure_live_on_path()
    # Prefer nested deploy live for MT5 client env
    sys.path.insert(0, str(LIVE))

    symbols = list_tradeable_yahoo_symbols()
    client = connect_mt5_client()
    mt5_all: dict = {}
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
            if len(candles) >= 120:
                mt5_all[sym] = candles
        except Exception as exc:
            if i <= 3:
                print(f"  skip {sym}: {exc}", flush=True)
        if i % 15 == 0 or i == len(symbols):
            print(f"  MT5 {len(mt5_all)}/{i}", flush=True)

    print(f"Loaded {len(mt5_all)} pairs", flush=True)
    lasts = []
    for c in mt5_all.values():
        if c:
            lasts.append(_ts(c[-1].timestamp))
    if lasts:
        print(f"Bar coverage latest={max(lasts)}", flush=True)

    strategy = AlexG7AlignedStrategy(
        min_rr=3.0, execution_interval="1h", peer_blend=0.0
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
    master = pick_master_symbol(mt5_all)
    print(f"master={master} bars={len(mt5_all[master])}", flush=True)

    engine = MultiMarketEngine(strategy, config, max_positions=60)
    result = engine.run(
        mt5_all, timeframe="1h", master_symbol=master, same_bar_exit=False
    )
    all_trades = [trade_row(t) for t in result.trades]
    week = []
    for r in all_trades:
        if not r["entry_time"]:
            continue
        et = _ts(r["entry_time"])
        if eval_start <= et < eval_end:
            week.append(r)

    summary = summarize(week, capital)
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "What-if: alexg7aligned as if live ran this week (MT5 OHLC, borex engine)",
        "params": {
            "strategy": "alexg7aligned",
            "min_rr": 3.0,
            "rr_factor": 1.88,
            "capital": capital,
            "leverage": 5000,
            "position_size": 0.01,
            "same_bar_exit": False,
            "commission_per_lot": 7.0,
            "risk_include_commission": True,
            "session": "overlap",
            "max_positions": 60,
        },
        "eval_start": _iso(eval_start),
        "eval_end": _iso(eval_end),
        "pairs": len(mt5_all),
        "summary": summary,
        "trades": week,
    }
    out = OUT / "report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== WEEK WHAT-IF ===", flush=True)
    print(
        f"{eval_start.date()} → {eval_end.date()} | pairs={len(mt5_all)}\n"
        f"trades={summary['trades']} WR={summary['win_rate']:.1%} "
        f"pnl=${summary['pnl_sum']:.2f} ret={100*summary['return_pct']:+.2f}%",
        flush=True,
    )
    print("\nTrades:", flush=True)
    for r in week:
        print(
            f"  {r['entry_time'][:16]} {r['symbol']:10} {r['side']:5} "
            f"pnl={r['pnl']:+.2f} {r['exit_reason']}",
            flush=True,
        )
    print(f"\nWrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
