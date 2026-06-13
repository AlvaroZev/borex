#!/usr/bin/env python3
"""CLI para ejecutar backtests con patrones de velas."""

from __future__ import annotations

import argparse
import sys

from borex.backtest import BacktestConfig, BacktestEngine
from borex.data import load_csv, load_yfinance
from borex.strategy import CandlePatternStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Borex — backtesting con patrones de velas japonesas"
    )
    parser.add_argument(
        "--symbol", "-s", default="EURUSD=X", help="Símbolo (yfinance)"
    )
    parser.add_argument(
        "--period", "-p", default="30d", help="Periodo histórico (yfinance)"
    )
    parser.add_argument(
        "--interval", "-i", default="1h", help="Intervalo de velas (yfinance)"
    )
    parser.add_argument(
        "--csv", help="Ruta a CSV en lugar de yfinance (columnas Date,OHLCV)"
    )
    parser.add_argument(
        "--capital", type=float, default=10_000, help="Capital inicial"
    )
    parser.add_argument(
        "--stop-loss", type=float, default=0.02, help="Stop loss (fracción, ej. 0.02 = 2%%)"
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=0.04,
        help="Take profit (fracción, ej. 0.04 = 4%%)",
    )
    parser.add_argument(
        "--patterns",
        nargs="*",
        help="Patrones a usar (default: todos). Ej: hammer bullish_engulfing",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Mostrar cada trade"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        if args.csv:
            candles = load_csv(args.csv)
            symbol = args.csv
            timeframe = "csv"
        else:
            candles = load_yfinance(args.symbol, args.period, args.interval)
            symbol = args.symbol
            timeframe = args.interval
    except Exception as exc:
        print(f"Error cargando datos: {exc}", file=sys.stderr)
        return 1

    if len(candles) < 20:
        print("Datos insuficientes para backtest (mínimo ~20 velas).", file=sys.stderr)
        return 1

    strategy = CandlePatternStrategy()
    if args.patterns:
        strategy.enabled_patterns = set(args.patterns)

    config = BacktestConfig(
        initial_capital=args.capital,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
    )

    engine = BacktestEngine(strategy, config)
    result = engine.run(candles, symbol=symbol, timeframe=timeframe)

    print(result.summary())
    print(f"Velas analizadas: {len(candles)}")
    print()

    if args.verbose and result.trades:
        print("Trades:")
        print("-" * 80)
        for i, t in enumerate(result.trades, 1):
            sign = "+" if t.pnl >= 0 else ""
            print(
                f"  {i:3d}. {t.side.value:5s} | {t.pattern:22s} | "
                f"entry {t.entry_price:.5f} -> exit {t.exit_price:.5f} | "
                f"PnL {sign}{t.pnl:.2f} ({sign}{t.pnl_pct:.2%}) [{t.exit_reason}]"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
