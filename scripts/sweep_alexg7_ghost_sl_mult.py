#!/usr/bin/env python3
"""Sweep alexg7 ghost_sl_mult on the first 20 FOREX pairs."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG7Strategy
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest import BacktestConfig, MultiMarketEngine
from borex.data import load_market_data
from borex.data.symbols import FOREX_PAIRS

DEFAULT_MULTS = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]
PAIRS_20 = FOREX_PAIRS[:20]


def _parse_mults(raw: str) -> list[float]:
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def _run_one(
    mult: float,
    candles: dict,
    *,
    rr_mode: str,
    min_rr: float,
    capital: float,
    leverage: float,
    position_size: float,
    max_positions: int,
) -> dict:
    print(f"\n===== ghost_sl_mult={mult:g} =====", flush=True)
    t0 = time.perf_counter()
    strategy = AlexG7Strategy(
        min_rr=min_rr,
        execution_interval="1h",
        ghost_sl_mult=mult,
    )
    config = BacktestConfig(
        initial_capital=capital,
        leverage=leverage,
        size_mode="margin",
        position_size_pct=position_size,
        true_sl=True,
        true_sl_rr=min_rr,
        rr_mode=rr_mode,
        rr_factor=1.0,
        stop_loss_pct=None,
        take_profit_pct=None,
    )
    master = pick_master_symbol(candles, preferred="EURUSD=X")
    engine = MultiMarketEngine(strategy, config, max_positions=max_positions)
    result = engine.run(candles, timeframe="1h", master_symbol=master)
    elapsed = time.perf_counter() - t0
    row = {
        "ghost_sl_mult": mult,
        "trades": result.total_trades,
        "wins": result.winning_trades,
        "losses": result.losing_trades,
        "win_rate": round(result.win_rate, 6),
        "return_pct": round(result.total_return_pct, 6),
        "final_equity": round(result.final_equity, 2),
        "max_dd_pct": round(result.max_drawdown_pct, 6),
        "profit_factor": round(result.profit_factor, 6),
        "elapsed_sec": round(elapsed, 1),
    }
    print(
        f"[mult={mult:g}] trades={row['trades']} WR={row['win_rate']:.2%} "
        f"ret={row['return_pct'] * 100:.2f}% final=${row['final_equity']:,.2f} "
        f"PF={row['profit_factor']:.3f} dd={row['max_dd_pct'] * 100:.2f}% "
        f"({row['elapsed_sec']}s)",
        flush=True,
    )
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--mults",
        default=",".join(str(m) for m in DEFAULT_MULTS),
        help="Comma-separated ghost_sl_mult values (default: 0.1..3.0 grid)",
    )
    ap.add_argument("--rr-mode", choices=["fixed", "dynamic"], default="dynamic")
    ap.add_argument("--min-rr", type=float, default=3.0)
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--leverage", type=float, default=5000.0)
    ap.add_argument("--position-size", type=float, default=0.01)
    ap.add_argument("--max-positions", type=int, default=9999)
    ap.add_argument("--period", default="max")
    ap.add_argument("--use-cache", action="store_true", default=True)
    args = ap.parse_args()

    mults = _parse_mults(args.mults)
    if not mults:
        print("No multipliers given", file=sys.stderr)
        return 1
    if any(m <= 0 for m in mults):
        print("All multipliers must be > 0", file=sys.stderr)
        return 1

    cache_mode = "only" if args.use_cache else "auto"
    print(
        f"alexg7 ghost_sl_mult sweep | pairs={len(PAIRS_20)} | "
        f"mults={mults} | rr={args.rr_mode} | $ {args.capital:g} | "
        f"{args.leverage:g}x",
        flush=True,
    )

    candles: dict = {}
    for i, sym in enumerate(PAIRS_20, 1):
        print(f"  [{i}/{len(PAIRS_20)}] load {sym}...", flush=True)
        candles[sym] = load_market_data(sym, args.period, "1h", cache_mode=cache_mode)
        print(f"       {len(candles[sym])} bars", flush=True)

    rows = [
        _run_one(
            m,
            candles,
            rr_mode=args.rr_mode,
            min_rr=args.min_rr,
            capital=args.capital,
            leverage=args.leverage,
            position_size=args.position_size,
            max_positions=args.max_positions,
        )
        for m in mults
    ]

    out_dir = ROOT / "data" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"alexg7_ghost_sl_mult_sweep_{stamp}.json"
    csv_path = out_dir / f"alexg7_ghost_sl_mult_sweep_{stamp}.csv"
    payload = {
        "params": {
            "strategy": "alexg7",
            "pairs": PAIRS_20,
            "n_pairs": len(PAIRS_20),
            "rr_mode": args.rr_mode,
            "min_rr": args.min_rr,
            "capital": args.capital,
            "leverage": args.leverage,
            "position_size": args.position_size,
            "max_positions": args.max_positions,
            "mults": mults,
        },
        "runs": rows,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("\n===== SWEEP SUMMARY =====", flush=True)
    print(
        f"{'mult':>6} {'trades':>7} {'WR':>8} {'return%':>12} "
        f"{'final$':>14} {'PF':>7} {'maxDD%':>8}",
        flush=True,
    )
    best = max(rows, key=lambda r: r["final_equity"])
    for r in rows:
        mark = " *" if r is best else ""
        print(
            f"{r['ghost_sl_mult']:6g} {r['trades']:7d} {r['win_rate']:8.2%} "
            f"{r['return_pct'] * 100:12.2f} {r['final_equity']:14,.2f} "
            f"{r['profit_factor']:7.3f} {r['max_dd_pct'] * 100:8.2f}{mark}",
            flush=True,
        )
    print(f"\nBest final equity: mult={best['ghost_sl_mult']:g} -> ${best['final_equity']:,.2f}")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
