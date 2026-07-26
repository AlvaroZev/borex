#!/usr/bin/env python3
"""Compare alexg5 on original 10 pairs vs full 20-pair universe."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG5Strategy
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest import BacktestConfig, MultiMarketEngine
from borex.data import load_market_data
from borex.data.symbols import FOREX_PAIRS

ORIGINAL_10 = FOREX_PAIRS[:10]
FULL_20 = FOREX_PAIRS[:20]

PERIOD = "max"
INTERVAL = "1h"
CACHE_MODE = "only"


def _load(universe: list[str]) -> dict:
    candles: dict = {}
    for i, sym in enumerate(universe, 1):
        print(f"  [{i}/{len(universe)}] load {sym}...", flush=True)
        candles[sym] = load_market_data(sym, PERIOD, INTERVAL, cache_mode=CACHE_MODE)
        print(f"       {len(candles[sym])} bars", flush=True)
    return candles


def _run(label: str, universe: list[str]) -> dict:
    print(f"\n===== {label}: {len(universe)} pairs =====", flush=True)
    t0 = time.perf_counter()
    candles = _load(universe)
    master = pick_master_symbol(candles, preferred="EURUSD=X")
    strategy = AlexG5Strategy(min_rr=3.0)
    config = BacktestConfig(
        initial_capital=1000.0,
        leverage=5000.0,
        size_mode="margin",
        position_size_pct=0.01,
        true_sl=True,
        true_sl_rr=3.0,
        rr_mode="fixed",
        rr_factor=1.0,
        stop_loss_pct=None,
        take_profit_pct=None,
    )
    engine = MultiMarketEngine(strategy, config, max_positions=9999)
    result = engine.run(candles, timeframe=INTERVAL, master_symbol=master)
    elapsed = time.perf_counter() - t0
    row = {
        "label": label,
        "n_pairs": len(universe),
        "pairs": universe,
        "master": master,
        "master_bars": len(candles[master]),
        "trades": result.total_trades,
        "wins": result.winning_trades,
        "losses": result.losing_trades,
        "win_rate": round(result.win_rate, 6),
        "return_pct": round(result.total_return_pct, 6),
        "max_dd_pct": round(result.max_drawdown_pct, 6),
        "profit_factor": round(result.profit_factor, 6),
        "final_equity": round(result.final_equity, 2),
        "initial_capital": 1000.0,
        "elapsed_sec": round(elapsed, 1),
    }
    print(result.summary(), flush=True)
    print(
        f"[{label}] trades={row['trades']} WR={row['win_rate']:.2%} "
        f"ret={row['return_pct']:.2f}% PF={row['profit_factor']:.3f} "
        f"final=${row['final_equity']:.2f} dd={row['max_dd_pct']:.2f}% "
        f"({row['elapsed_sec']}s)",
        flush=True,
    )
    return row


def main() -> int:
    print(
        f"alexg5 compare | period={PERIOD} interval={INTERVAL} "
        f"RR fixed 3 | $1000 | 5000x | margin 1% | max_pos 9999",
        flush=True,
    )
    print(f"original10={len(ORIGINAL_10)} full20={len(FULL_20)}", flush=True)

    rows = [
        _run("pairs10", ORIGINAL_10),
        _run("pairs20", FULL_20),
    ]

    out_dir = ROOT / "data" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "alexg5_pairs10_vs_20.json"
    payload = {"params": {
        "strategy": "alexg5",
        "period": PERIOD,
        "interval": INTERVAL,
        "rr_mode": "fixed",
        "min_rr": 3.0,
        "capital": 1000.0,
        "leverage": 5000.0,
        "position_size_pct": 0.01,
        "max_positions": 9999,
    }, "runs": rows}
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    a, b = rows[0], rows[1]
    print("\n===== COMPARISON =====", flush=True)
    print(f"{'metric':20} {'10 pairs':>12} {'20 pairs':>12} {'delta':>12}", flush=True)
    for key, fmt in [
        ("trades", "{:.0f}"),
        ("win_rate", "{:.2%}"),
        ("return_pct", "{:.2f}%"),
        ("profit_factor", "{:.3f}"),
        ("final_equity", "${:.2f}"),
        ("max_dd_pct", "{:.2f}%"),
        ("elapsed_sec", "{:.0f}s"),
    ]:
        va, vb = a[key], b[key]
        if key == "win_rate":
            line = f"{key:20} {va:12.2%} {vb:12.2%} {vb - va:+12.2%}"
        elif key == "final_equity":
            line = f"{key:20} ${va:11.2f} ${vb:11.2f} ${vb - va:+11.2f}"
        elif key in ("return_pct", "max_dd_pct"):
            line = f"{key:20} {va:11.2f}% {vb:11.2f}% {vb - va:+11.2f}%"
        elif key == "profit_factor":
            line = f"{key:20} {va:12.3f} {vb:12.3f} {vb - va:+12.3f}"
        elif key == "elapsed_sec":
            line = f"{key:20} {va:11.0f}s {vb:11.0f}s {vb - va:+11.0f}s"
        else:
            line = f"{key:20} {va:12.0f} {vb:12.0f} {vb - va:+12.0f}"
        print(line, flush=True)

    print(f"\nWrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
