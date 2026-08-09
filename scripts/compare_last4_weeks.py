#!/usr/bin/env python3
"""Independent weekly MT5 vs Dukascopy backtests for the last N weeks.

No live deals. Params match live: alexg7aligned, min-rr 3, rr-factor 1.88,
$1k, 5000x, 1% risk, same-bar off, $7/lot RT + risk-include-commission.
Each week uses +1 week warmup of OHLC, then scores only eval-week entries.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LIVE_ROOT = ROOT.parent / "borex_live"
OUT_DIR = ROOT / "data" / "runs" / "week_compare_last4_full"
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
    entry = getattr(t, "entry_time", None)
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
        "rr_used": float(getattr(t, "score", 0) or 0),
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


def last_n_week_windows(n: int = 4) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Most recent N completed Mon–Sat(00:00) eval windows (covers Fri session)."""
    today = datetime.now(timezone.utc).date()
    weekday = today.weekday()
    if weekday >= 5:
        latest_monday = today - timedelta(days=weekday)
    else:
        latest_monday = today - timedelta(days=weekday + 7)
    weeks = []
    for i in range(n):
        mon = latest_monday - timedelta(days=7 * i)
        start = pd.Timestamp(datetime(mon.year, mon.month, mon.day, tzinfo=timezone.utc))
        end = start + timedelta(days=6)
        weeks.append((start, end))
    return weeks  # newest first


def slice_candles(
    candles_by_symbol: dict,
    start: pd.Timestamp,
    end: pd.Timestamp,
    min_bars: int = 120,
) -> dict:
    out = {}
    for sym, candles in candles_by_symbol.items():
        sliced = [c for c in candles if start <= _ts(c.timestamp) < end]
        if len(sliced) >= min_bars:
            out[sym] = sliced
    return out


def run_backtest(candles_by_symbol: dict, label: str, capital: float):
    from borex.alexg import AlexG7AlignedStrategy
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    if not candles_by_symbol:
        return None, []

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
        f"  [{label}] pairs={len(candles_by_symbol)} master={master} "
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
    return result, [trade_row(t, label) for t in result.trades]


def cache_coverage(candles_by_symbol: dict) -> dict:
    if not candles_by_symbol:
        return {"pairs": 0, "earliest": None, "latest": None}
    firsts, lasts = [], []
    for candles in candles_by_symbol.values():
        if candles:
            firsts.append(_ts(candles[0].timestamp))
            lasts.append(_ts(candles[-1].timestamp))
    return {
        "pairs": len(candles_by_symbol),
        "earliest": _iso(min(firsts)) if firsts else None,
        "latest": _iso(max(lasts)) if lasts else None,
    }


def main() -> int:
    capital = 1000.0
    n_weeks = 4
    weeks = last_n_week_windows(n_weeks)
    # Load from warmup of oldest week through end of newest
    oldest_start = weeks[-1][0] - timedelta(days=7)
    newest_end = weeks[0][1] + timedelta(days=1)

    print(
        f"Last {n_weeks} weeks (newest→oldest):\n"
        + "\n".join(f"  {a.date()} → {b.date()}" for a, b in weeks),
        flush=True,
    )
    print(f"Bulk load: {oldest_start.date()} → {newest_end.date()}", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from borex.viewerMT5.mt5_feed import (
        connect_mt5_client,
        ensure_live_on_path,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
    )
    from borex.data.loader import load_market_data

    ensure_live_on_path()
    symbols = list_tradeable_yahoo_symbols()
    print(f"Universe: {len(symbols)} pairs", flush=True)

    client = connect_mt5_client()
    mt5_all: dict = {}
    for i, sym in enumerate(symbols, 1):
        try:
            candles = fetch_mt5_candles(
                sym,
                "1h",
                oldest_start.to_pydatetime(),
                newest_end.to_pydatetime(),
                client=client,
                use_cache=True,
                write_cache=True,
            )
            if len(candles) >= 120:
                mt5_all[sym] = candles
        except Exception as exc:
            if i <= 3:
                print(f"  MT5 skip {sym}: {exc}", flush=True)
        if i % 15 == 0 or i == len(symbols):
            print(f"  MT5 {len(mt5_all)}/{i}", flush=True)

    duka_all: dict = {}
    for i, sym in enumerate(symbols, 1):
        try:
            candles = load_market_data(sym, "90d", "1h", cache_mode="only")
            sliced = [
                c
                for c in candles
                if oldest_start <= _ts(c.timestamp) < newest_end
            ]
            if len(sliced) >= 120:
                duka_all[sym] = sliced
        except Exception as exc:
            if i <= 5 or "no cache" in str(exc).lower():
                print(f"  Duka skip {sym}: {exc}", flush=True)
            continue
        if i % 15 == 0 or i == len(symbols):
            print(f"  Duka {len(duka_all)}/{i}", flush=True)

    # Require same 60-intent: report missing
    missing_duka = [s for s in symbols if s not in duka_all]
    if missing_duka:
        print(f"Duka missing {len(missing_duka)} pairs: {', '.join(missing_duka)}", flush=True)

    print(
        f"Loaded mt5={cache_coverage(mt5_all)} duka={cache_coverage(duka_all)}",
        flush=True,
    )

    week_reports = []
    for wi, (eval_start, eval_end) in enumerate(weeks, 1):
        load_start = eval_start - timedelta(days=7)
        load_end = eval_end + timedelta(hours=12)
        label = f"W{wi}_{eval_start.date()}"
        print(f"\n=== {label} eval {eval_start.date()} → {eval_end.date()} ===", flush=True)

        mt5_slice = slice_candles(mt5_all, load_start, load_end, min_bars=120)
        duka_slice = slice_candles(duka_all, load_start, load_end, min_bars=120)

        _, mt5_trades = run_backtest(mt5_slice, f"bt_mt5_{label}", capital)
        _, duka_trades = run_backtest(duka_slice, f"bt_duka_{label}", capital)

        mt5_week = filter_week(mt5_trades, eval_start, eval_end)
        duka_week = filter_week(duka_trades, eval_start, eval_end)

        week_reports.append(
            {
                "week_index": wi,
                "label": label,
                "eval_start": _iso(eval_start),
                "eval_end": _iso(eval_end),
                "load_start": _iso(load_start),
                "coverage": {
                    "mt5_pairs": len(mt5_slice),
                    "duka_pairs": len(duka_slice),
                    "common_pairs": len(set(mt5_slice) & set(duka_slice)),
                },
                "summary": {
                    "bt_mt5": summarize(mt5_week, capital),
                    "bt_duka": summarize(duka_week, capital),
                },
                "trades": {
                    "bt_mt5": mt5_week,
                    "bt_duka": duka_week,
                },
            }
        )
        sm, sd = week_reports[-1]["summary"]["bt_mt5"], week_reports[-1]["summary"]["bt_duka"]
        print(
            f"  MT5: n={sm['trades']} WR={sm['win_rate']:.1%} ret={sm['return_pct']:.2%} "
            f"| Duka: n={sd['trades']} WR={sd['win_rate']:.1%} ret={sd['return_pct']:.2%} "
            f"(pairs mt5={len(mt5_slice)} duka={len(duka_slice)})",
            flush=True,
        )

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
            "warmup_days": 7,
            "note": "Independent weeks; live deals excluded",
        },
        "bulk_coverage": {
            "mt5": cache_coverage(mt5_all),
            "duka": cache_coverage(duka_all),
        },
        "weeks": week_reports,
    }

    out_path = OUT_DIR / "report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}", flush=True)

    # compact summary table
    print("\nWeek | MT5 trades | MT5 ret% | Duka trades | Duka ret% | Duka pairs")
    for w in week_reports:
        sm, sd = w["summary"]["bt_mt5"], w["summary"]["bt_duka"]
        print(
            f"{w['eval_start'][:10]} | {sm['trades']:10d} | {100*sm['return_pct']:+7.2f} | "
            f"{sd['trades']:11d} | {100*sd['return_pct']:+8.2f} | {w['coverage']['duka_pairs']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
