#!/usr/bin/env python3
"""Run alexg3 multi-market backtest and export trades CSV."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG3Strategy
from borex.alexg.multi_market import default_forex_universe, pick_master_symbol
from borex.backtest import BacktestConfig, MultiMarketEngine
from borex.data import load_market_data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--leverage", "-l", type=float, default=5000.0)
    parser.add_argument("--period", "-p", default="max")
    parser.add_argument("--interval", "-i", default="1h")
    parser.add_argument("--position-size", type=float, default=0.01)
    parser.add_argument("--max-positions", type=int, default=9999)
    parser.add_argument(
        "--out",
        default="data/alexg3_1000_5000x_trades.csv",
        help="Output CSV path",
    )
    parser.add_argument("--allow-false-positives", action="store_true")
    args = parser.parse_args()

    universe = default_forex_universe()
    candles_by_symbol: dict = {}
    for sym in universe:
        try:
            candles_by_symbol[sym] = load_market_data(
                sym, args.period, args.interval, cache_mode="only"
            )
            print(
                f"loaded {sym}: {len(candles_by_symbol[sym])} bars",
                flush=True,
            )
        except Exception as exc:
            print(f"skip {sym}: {exc}", flush=True)

    if len(candles_by_symbol) < 2:
        print("Need at least 2 pairs with data.", file=sys.stderr)
        return 1

    master = pick_master_symbol(candles_by_symbol, "EURUSD=X")
    strategy = AlexG3Strategy(
        min_rr=2.0,
        tp_fraction=1.0,
        filter_false_positives=not args.allow_false_positives,
    )
    config = BacktestConfig(
        initial_capital=args.capital,
        leverage=args.leverage,
        size_mode="margin",
        position_size_pct=args.position_size,
        close_on_opposite_signal=True,
        stop_loss_pct=None,
        take_profit_pct=None,
    )
    engine = MultiMarketEngine(
        strategy, config, max_positions=args.max_positions
    )
    print(
        f"Running alexg3 | capital=${args.capital:,.0f} | lev={args.leverage:g}x | "
        f"pairs={len(candles_by_symbol)} | master={master}",
        flush=True,
    )
    result = engine.run(
        candles_by_symbol, timeframe=args.interval, master_symbol=master
    )
    print(result.summary(), flush=True)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "id",
        "symbol",
        "side",
        "pattern",
        "signal",
        "setup",
        "aoi_kind",
        "trend",
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "stop_loss",
        "take_profit",
        "planned_rr",
        "entry_cash",
        "entry_equity",
        "margin",
        "notional",
        "pnl",
        "pnl_pct",
        "account_pct",
        "margin_return_pct",
        "exit_reason",
        "score",
    ]
    rows = []
    for i, t in enumerate(result.trades, 1):
        parts = t.pattern.split("|")
        signal = parts[6] if len(parts) > 6 else ""
        setup = parts[4] if len(parts) > 4 else ""
        aoi = parts[5] if len(parts) > 5 else ""
        trend = parts[3] if len(parts) > 3 else ""
        risk = abs(t.entry_price - t.stop_loss) if t.stop_loss else 0
        reward = abs(t.take_profit - t.entry_price) if t.take_profit else 0
        rr = reward / risk if risk > 0 else 0
        entry_cap = t.entry_cash or t.entry_equity
        rows.append(
            {
                "id": i,
                "symbol": t.symbol,
                "side": t.side.value,
                "pattern": t.pattern,
                "signal": signal,
                "setup": setup,
                "aoi_kind": aoi,
                "trend": trend,
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "planned_rr": round(rr, 4),
                "entry_cash": round(entry_cap, 4),
                "entry_equity": round(t.entry_equity, 4),
                "margin": round(t.margin, 4),
                "notional": round(t.margin * args.leverage, 4),
                "pnl": round(t.pnl, 4),
                "pnl_pct": t.pnl_pct,
                "account_pct": (t.pnl / entry_cap) if entry_cap else 0,
                "margin_return_pct": (t.pnl / t.margin) if t.margin else 0,
                "exit_reason": t.exit_reason,
                "score": t.score,
            }
        )

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV written: {out.resolve()} ({len(rows)} trades)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
