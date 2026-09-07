#!/usr/bin/env python3
"""
Smoke: alexg7 on Dukascopy cache vs MT5 1h OHLC for one pair / one month.

Runs both same_bar_exit modes (off = +xxx% compounding path; on = stricter).
Writes JSON for scripts/smoke_compare_viewer.py.

Note: MT5 Strategy Tester runs MQL5 EAs, not this Python strategy. This script
compares our engine on MT5 bars vs our engine on cache bars. Trade CSVs can be
used for manual visual checks in MT5.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LIVE_ROOT = ROOT.parent / "borex_live"
if LIVE_ROOT.is_dir():
    load_dotenv(LIVE_ROOT / ".env")
    sys.path.insert(0, str(LIVE_ROOT))

from borex.alexg import AlexG7Strategy
from borex.backtest import BacktestConfig, MultiMarketEngine
from borex.data import load_market_data
from borex.models.candle import Candle


def _ts(c: Candle) -> pd.Timestamp:
    t = pd.Timestamp(c.timestamp)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _slice(candles: list[Candle], start: pd.Timestamp, end: pd.Timestamp) -> list[Candle]:
    out = []
    for c in candles:
        t = _ts(c)
        if start <= t < end:
            out.append(c)
    return out


def _with_warmup(
    candles: list[Candle],
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    warmup_bars: int = 2000,
) -> list[Candle]:
    """Keep bars before window for AOI/warmup, run through window_end."""
    before = [c for c in candles if _ts(c) < window_start]
    during = [c for c in candles if window_start <= _ts(c) < window_end]
    warm = before[-warmup_bars:] if len(before) > warmup_bars else before
    return warm + during


def fetch_mt5_h1(symbol_yahoo: str, start: datetime, end: datetime) -> list[Candle]:
    import MetaTrader5 as mt5

    from borex_live.mt5.client import Mt5Client
    from borex_live.mt5.symbols import yahoo_to_mt5

    path = os.environ.get("MT5_PATH", "")
    login = int(os.environ.get("MT5_LOGIN", "0") or 0)
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_DEMO_SERVER") or os.environ.get("MT5_SERVER", "")

    client = Mt5Client(path=path, login=login, password=password, server=server)
    client.connect()
    try:
        mt5_sym = yahoo_to_mt5(symbol_yahoo)
        client.ensure_symbol(symbol_yahoo)
        rates = mt5.copy_rates_range(
            mt5_sym, mt5.TIMEFRAME_H1, start, end
        )
        if rates is None:
            raise RuntimeError(f"MT5 copy_rates_range failed: {mt5.last_error()}")
        candles: list[Candle] = []
        for row in rates:
            ts = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc)
            candles.append(
                Candle(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["tick_volume"]),
                )
            )
        return candles
    finally:
        client.disconnect()


def run_alexg7(
    candles: list[Candle],
    symbol: str,
    *,
    same_bar_exit: bool,
    capital: float,
    leverage: float,
    position_size: float,
    min_rr: float,
) -> dict:
    strategy = AlexG7Strategy(min_rr=min_rr, execution_interval="1h")
    config = BacktestConfig(
        initial_capital=capital,
        leverage=leverage,
        size_mode="margin",
        position_size_pct=position_size,
        true_sl=True,
        true_sl_rr=min_rr,
        rr_mode="dynamic",
        rr_factor=1.0,
        stop_loss_pct=None,
        take_profit_pct=None,
    )
    engine = MultiMarketEngine(strategy, config, max_positions=1)
    result = engine.run(
        {symbol: candles},
        timeframe="1h",
        master_symbol=symbol,
        same_bar_exit=same_bar_exit,
    )
    trades = []
    for t in result.trades:
        trades.append(
            {
                "symbol": t.symbol or symbol,
                "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                "pattern": t.pattern,
                "entry_time": str(t.entry_time) if t.entry_time else "",
                "exit_time": str(t.exit_time) if t.exit_time else "",
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price) if t.exit_price is not None else None,
                "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
                "take_profit": float(t.take_profit) if t.take_profit is not None else None,
                "pnl": float(t.pnl),
                "exit_reason": t.exit_reason or "",
            }
        )
    equity = [
        {"i": i, "equity": float(eq)} for i, eq in enumerate(result.equity_curve)
    ]
    return {
        "same_bar_exit": same_bar_exit,
        "trades": len(result.trades),
        "wins": result.winning_trades,
        "losses": result.losing_trades,
        "win_rate": result.win_rate,
        "final_equity": result.final_equity,
        "return_pct": result.total_return_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "profit_factor": result.profit_factor,
        "trade_list": trades,
        "equity_curve": equity,
    }


def filter_trades_in_window(payload: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    kept = []
    for t in payload["trade_list"]:
        if not t["entry_time"]:
            continue
        et = pd.Timestamp(t["entry_time"])
        if et.tzinfo is None:
            et = et.tz_localize("UTC")
        else:
            et = et.tz_convert("UTC")
        if start <= et < end:
            kept.append(t)
    pnl = sum(t["pnl"] for t in kept)
    wins = sum(1 for t in kept if t["pnl"] > 0)
    out = dict(payload)
    out["trade_list"] = kept
    out["trades"] = len(kept)
    out["wins"] = wins
    out["losses"] = len(kept) - wins
    out["win_rate"] = (wins / len(kept)) if kept else 0.0
    out["window_pnl"] = pnl
    return out


def candles_payload(candles: list[Candle], start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    rows = []
    for c in candles:
        t = _ts(c)
        if start <= t < end:
            rows.append(
                {
                    "t": t.isoformat(),
                    "o": c.open,
                    "h": c.high,
                    "l": c.low,
                    "c": c.close,
                }
            )
    return rows


def live_order_of_ops() -> dict:
    return {
        "title": "Live alexg7 order of operations (vs backtest same-bar)",
        "steps": [
            {
                "when": "Setup bar (strategy.on_bar queues ghost)",
                "live": "DB pending ghost + MT5 pending LIMIT at ghost SL with protective SL/TP attached via late_entry_stops",
                "backtest_no_same_bar": "Pending ghost in strategy._pending only",
                "backtest_same_bar": "Same",
            },
            {
                "when": "Price tags ghost SL (fill)",
                "live": "Broker fills the pending (intrabar). sync_ghost_fills books the live trade. Protective SL/TP already on the position — broker can stop out later on THAT same bar if price continues.",
                "backtest_no_same_bar": "Fill on bar N; exit checks start on bar N+1 → can survive a fill-bar wick through protective SL",
                "backtest_same_bar": "Fill on bar N then immediately _check_exit on bar N → often margin_stop on the fill candle (closer to live broker intrabar stop)",
            },
            {
                "when": "After fill",
                "live": "MT5 manages SL/TP; reconcile_open_with_mt5 closes DB when position disappears",
                "backtest_no_same_bar": "Engine OHLC stop/TP from next bars",
                "backtest_same_bar": "Engine OHLC stop/TP including fill bar",
            },
            {
                "when": "Unrealized MTM / DD",
                "live": "Broker equity (real liquidation rules)",
                "backtest_no_same_bar": "Current portfolio: uncapped MTM → DD can read ~100% while cash remains",
                "backtest_same_bar": "Same MTM marking unless cap restored",
            },
        ],
        "strategy_tester_note": (
            "MT5 Strategy Tester cannot run this Python alexg7. Closest check: "
            "run our engine on MT5 OHLC (this smoke) and optionally overlay trade "
            "CSV markers in MT5 charts manually."
        ),
    }


def default_month_window(now: datetime | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Last full calendar month in UTC."""
    now = now or datetime.now(timezone.utc)
    first_this = pd.Timestamp(year=now.year, month=now.month, day=1, tz="UTC")
    end = first_this
    start = (first_this - pd.offsets.MonthBegin(1)).tz_convert("UTC")
    return start, end


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="EURUSD=X")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--leverage", type=float, default=5000.0)
    ap.add_argument("--position-size", type=float, default=0.01)
    ap.add_argument("--min-rr", type=float, default=3.0)
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "runs" / "smoke_mt5_vs_cache.json",
    )
    args = ap.parse_args()

    window_start, window_end = default_month_window()
    # History for warmup/AOI: 18 months before window end
    hist_start = window_end - pd.DateOffset(months=18)
    print(
        f"Window: {window_start.date()} -> {window_end.date()} | "
        f"history from {hist_start.date()} | symbol={args.symbol}",
        flush=True,
    )

    print("Loading Dukascopy/cache…", flush=True)
    cache_all = load_market_data(args.symbol, "max", "1h", cache_mode="only")
    cache_series = _with_warmup(cache_all, window_start, window_end, warmup_bars=3000)
    print(f"  cache bars used: {len(cache_series)}", flush=True)

    print("Fetching MT5 H1…", flush=True)
    mt5_all = fetch_mt5_h1(
        args.symbol,
        hist_start.to_pydatetime(),
        window_end.to_pydatetime(),
    )
    mt5_series = _with_warmup(mt5_all, window_start, window_end, warmup_bars=3000)
    print(f"  mt5 bars used: {len(mt5_series)}", flush=True)
    if len(mt5_series) < 200:
        print("ERROR: not enough MT5 bars (is terminal logged in / history available?)", file=sys.stderr)
        return 1

    runs = {}
    for source, series in (("cache", cache_series), ("mt5", mt5_series)):
        for same_bar in (False, True):
            key = f"{source}_samebar_{'on' if same_bar else 'off'}"
            print(f"Running alexg7 {key}…", flush=True)
            raw = run_alexg7(
                series,
                args.symbol,
                same_bar_exit=same_bar,
                capital=args.capital,
                leverage=args.leverage,
                position_size=args.position_size,
                min_rr=args.min_rr,
            )
            filtered = filter_trades_in_window(raw, window_start, window_end)
            print(
                f"  window trades={filtered['trades']} "
                f"pnl={filtered['window_pnl']:.2f} "
                f"final_eq(full)={filtered['final_equity']:.2f} "
                f"dd={filtered['max_drawdown_pct']:.2%}",
                flush=True,
            )
            runs[key] = filtered

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": args.symbol,
        "interval": "1h",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "params": {
            "capital": args.capital,
            "leverage": args.leverage,
            "position_size": args.position_size,
            "min_rr": args.min_rr,
            "rr_mode": "dynamic",
            "strategy": "alexg7",
        },
        "live_order_of_ops": live_order_of_ops(),
        "ohlc": {
            "cache": candles_payload(cache_series, window_start, window_end),
            "mt5": candles_payload(mt5_series, window_start, window_end),
        },
        "runs": runs,
        "compare_hint": {
            "primary": "cache_samebar_off vs mt5_samebar_off (+xxx% path)",
            "live_like": "cache_samebar_on vs mt5_samebar_on",
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}", flush=True)

    # Also dump trade CSVs for manual MT5 overlay
    import csv

    for key, run in runs.items():
        csv_path = args.out.with_name(f"smoke_{key}_trades.csv")
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            cols = [
                "entry_time",
                "exit_time",
                "side",
                "entry_price",
                "exit_price",
                "stop_loss",
                "take_profit",
                "pnl",
                "exit_reason",
                "pattern",
            ]
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for t in run["trade_list"]:
                w.writerow(t)
        print(f"Wrote {csv_path}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
