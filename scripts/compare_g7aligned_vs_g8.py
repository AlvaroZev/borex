#!/usr/bin/env python3
"""Compare alexg7aligned vs alexg8 on MT5 bars: last 4 weeks + full July.

Same live-like params: min-rr 3, rr-factor 1.88, $1k, 5000x, 1% risk,
same-bar off, $7/lot RT + risk-include-commission, max-positions 60.
Weeks: independent (+1w warmup, score eval-week entries only).
Month: Jul 1–Aug 1 with Jun 24 warmup; score Jul entries only.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LIVE_ROOT = ROOT.parent / "borex_live"
OUT_DIR = ROOT / "data" / "runs" / "g7aligned_vs_g8"
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


def filter_window(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
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
    return weeks


def slice_candles(candles_by_symbol: dict, start: pd.Timestamp, end: pd.Timestamp, min_bars: int = 120) -> dict:
    out = {}
    for sym, candles in candles_by_symbol.items():
        sliced = [c for c in candles if start <= _ts(c.timestamp) < end]
        if len(sliced) >= min_bars:
            out[sym] = sliced
    return out


def make_strategy(name: str, min_rr: float = 3.0):
    from borex.alexg import AlexG7AlignedStrategy, AlexG8Strategy

    if name == "alexg8":
        return AlexG8Strategy(min_rr=min_rr, execution_interval="1h", peer_blend=0.0)
    return AlexG7AlignedStrategy(min_rr=min_rr, execution_interval="1h", peer_blend=0.0)


def run_backtest(candles_by_symbol: dict, label: str, strategy_name: str, capital: float):
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    if not candles_by_symbol:
        return []

    strategy = make_strategy(strategy_name)
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
        f"  [{label}] {strategy_name} pairs={len(candles_by_symbol)} "
        f"master={master} bars={len(candles_by_symbol[master])}",
        flush=True,
    )
    engine = MultiMarketEngine(strategy, config, max_positions=60)
    result = engine.run(
        candles_by_symbol,
        timeframe="1h",
        master_symbol=master,
        same_bar_exit=False,
    )
    return [trade_row(t, label) for t in result.trades]


def main() -> int:
    capital = 1000.0
    weeks = last_n_week_windows(4)
    month_start = pd.Timestamp(datetime(2026, 7, 1, tzinfo=timezone.utc))
    month_end = pd.Timestamp(datetime(2026, 8, 1, tzinfo=timezone.utc))
    month_load_start = month_start - timedelta(days=7)

    oldest_start = min(weeks[-1][0] - timedelta(days=7), month_load_start)
    newest_end = max(weeks[0][1] + timedelta(days=1), month_end)

    print(
        "alexg7aligned vs alexg8 | MT5 H1 | last 4 weeks + July\n"
        + "\n".join(f"  week {a.date()} → {b.date()}" for a, b in weeks)
        + f"\n  month {month_start.date()} → {month_end.date()}",
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

    print(f"Loaded pairs={len(mt5_all)}", flush=True)

    strategies = ("alexg7aligned", "alexg8")
    week_reports = []

    for wi, (eval_start, eval_end) in enumerate(weeks, 1):
        load_start = eval_start - timedelta(days=7)
        load_end = eval_end + timedelta(hours=12)
        label = f"W{wi}_{eval_start.date()}"
        print(f"\n=== {label} eval {eval_start.date()} → {eval_end.date()} ===", flush=True)
        sliced = slice_candles(mt5_all, load_start, load_end, min_bars=120)
        summaries = {}
        trades_by = {}
        for name in strategies:
            rows = run_backtest(sliced, f"{name}_{label}", name, capital)
            week_rows = filter_window(rows, eval_start, eval_end)
            summaries[name] = summarize(week_rows, capital)
            trades_by[name] = week_rows
            s = summaries[name]
            print(
                f"  {name}: n={s['trades']} WR={s['win_rate']:.1%} ret={s['return_pct']:.2%} "
                f"(pairs={len(sliced)})",
                flush=True,
            )
        week_reports.append(
            {
                "week_index": wi,
                "label": label,
                "eval_start": _iso(eval_start),
                "eval_end": _iso(eval_end),
                "pairs": len(sliced),
                "summary": summaries,
                "trades": trades_by,
            }
        )

    print(f"\n=== July month {month_start.date()} → {month_end.date()} ===", flush=True)
    month_slice = slice_candles(
        mt5_all,
        month_load_start,
        month_end + timedelta(hours=12),
        min_bars=120,
    )
    month_summaries = {}
    month_trades = {}
    for name in strategies:
        rows = run_backtest(month_slice, f"{name}_july", name, capital)
        jul_rows = filter_window(rows, month_start, month_end)
        month_summaries[name] = summarize(jul_rows, capital)
        month_trades[name] = jul_rows
        s = month_summaries[name]
        print(
            f"  {name}: n={s['trades']} WR={s['win_rate']:.1%} ret={s['return_pct']:.2%} "
            f"(pairs={len(month_slice)})",
            flush=True,
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feed": "mt5",
        "params": {
            "strategies": list(strategies),
            "min_rr": 3.0,
            "rr_factor": 1.88,
            "capital": capital,
            "leverage": 5000,
            "position_size": 0.01,
            "same_bar_exit": False,
            "commission_per_lot": 7.0,
            "risk_include_commission": True,
            "max_positions": 60,
            "note": "alexg8 = alexg7aligned without session overlap filter",
        },
        "pairs_loaded": len(mt5_all),
        "weeks": [
            {
                "week_index": w["week_index"],
                "label": w["label"],
                "eval_start": w["eval_start"],
                "eval_end": w["eval_end"],
                "pairs": w["pairs"],
                "summary": w["summary"],
                "trades": w["trades"],
            }
            for w in week_reports
        ],
        "july": {
            "eval_start": _iso(month_start),
            "eval_end": _iso(month_end),
            "load_start": _iso(month_load_start),
            "pairs": len(month_slice),
            "summary": month_summaries,
            "trades": month_trades,
        },
    }

    out_path = OUT_DIR / "report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}", flush=True)

    print("\nWeek | g7aligned n/ret | alexg8 n/ret")
    for w in week_reports:
        g7 = w["summary"]["alexg7aligned"]
        g8 = w["summary"]["alexg8"]
        print(
            f"{w['eval_start'][:10]} | {g7['trades']:3d} / {100*g7['return_pct']:+6.2f}% | "
            f"{g8['trades']:3d} / {100*g8['return_pct']:+6.2f}%"
        )
    g7m = month_summaries["alexg7aligned"]
    g8m = month_summaries["alexg8"]
    print(
        f"July     | {g7m['trades']:3d} / {100*g7m['return_pct']:+6.2f}% | "
        f"{g8m['trades']:3d} / {100*g8m['return_pct']:+6.2f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
