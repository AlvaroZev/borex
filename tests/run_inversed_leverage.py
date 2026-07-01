#!/usr/bin/env python3
"""Leverage 10-5000x — mejor config invertida (solo cache)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg.strategy import AlexGMethodStrategy
from borex.backtest.engine import BacktestConfig, BacktestEngine
from borex.data import build_full_mtf_context, load_market_data

# Mejor return del sweep: RR3, TP2%, SL×1.25
MIN_RR, MAX_TP, SL_MULT = 3.0, 0.02, 1.25
LEVERAGES = [10, 50, 100, 500, 1000, 5000]


def main() -> None:
    symbol, period, interval = "GBPUSD=X", "730d", "1h"
    print("Loading cache (only)...", flush=True)
    candles = load_market_data(symbol, period, interval, cache_mode="only")
    mtf = build_full_mtf_context(candles, interval, symbol, period, cache_mode="only")
    print(f"Config: inversed RR={MIN_RR} TP={MAX_TP*100:.0f}% SLx={SL_MULT}\n")
    print(f"{'Lev':>6} {'Return%':>10} {'Final$':>14} {'WR%':>6} {'DD%':>8} {'Trades':>6}")
    print("-" * 56)
    rows = []
    for i, lev in enumerate(LEVERAGES, 1):
        print(f"Running {i}/{len(LEVERAGES)} lev={lev}x...", flush=True)
        strategy = AlexGMethodStrategy(
            min_rr=MIN_RR, max_tp_pct=MAX_TP, sl_mult=SL_MULT
        )
        config = BacktestConfig(
            initial_capital=10_000,
            leverage=float(lev),
            inversed=True,
            stop_loss_pct=None,
            take_profit_pct=None,
        )
        result = BacktestEngine(strategy, config).run(
            candles, symbol=symbol, timeframe=interval, mtf=mtf
        )
        row = (
            lev,
            result.total_return_pct * 100,
            result.final_equity,
            result.win_rate * 100,
            result.max_drawdown_pct * 100,
            result.total_trades,
        )
        rows.append(row)
        print(
            f"{lev:6.0f} {row[1]:10.2f} {row[2]:14,.2f} {row[3]:6.1f} "
            f"{row[4]:8.2f} {row[5]:6d}"
        )

    out = ROOT / "tests" / "inversed_leverage_results.txt"
    lines = [
        "ALEXG INVERSED LEVERAGE — GBPUSD 730d 1h",
        f"RR={MIN_RR} max_tp={MAX_TP} sl_mult={SL_MULT}",
        "",
        f"{'Lev':>6} {'Return%':>10} {'Final$':>14} {'WR%':>6} {'DD%':>8} {'Trades':>6}",
    ]
    for r in rows:
        lines.append(
            f"{r[0]:6.0f} {r[1]:10.2f} {r[2]:14,.2f} {r[3]:6.1f} {r[4]:8.2f} {r[5]:6d}"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
