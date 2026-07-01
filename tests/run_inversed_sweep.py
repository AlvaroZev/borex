#!/usr/bin/env python3
"""Grid search AlexG invertido en GBPUSD (solo cache local)."""

from __future__ import annotations

import csv
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg.strategy import AlexGMethodStrategy
from borex.backtest.engine import BacktestConfig, BacktestEngine
from borex.data import build_full_mtf_context, load_market_data


def main() -> int:
    min_rrs = [2.0, 3.0, 5.0]
    max_tps: list[float | None] = [0.01, 0.02]
    sl_mults = [0.75, 1.0, 1.25]
    symbol, period, interval = "GBPUSD=X", "730d", "1h"

    print("Loading cache (once)...", flush=True)
    candles = load_market_data(symbol, period, interval, cache_mode="only")
    mtf = build_full_mtf_context(candles, interval, symbol, period, cache_mode="only")
    print(f"Loaded {len(candles)} candles\n", flush=True)

    rows: list[dict] = []
    combos = list(itertools.product(min_rrs, max_tps, sl_mults))
    print(f"AlexG INVERSED sweep — {symbol} {period} {interval} — {len(combos)} combos\n")

    for i, (min_rr, max_tp, sl_mult) in enumerate(combos, 1):
        tp_label = f"{max_tp*100:.1f}%" if max_tp else "none"
        print(f"[{i}/{len(combos)}] RR={min_rr} TP={tp_label} SLx={sl_mult}...", flush=True)
        try:
            strategy = AlexGMethodStrategy(
                min_rr=min_rr,
                max_tp_pct=max_tp,
                sl_mult=sl_mult,
            )
            config = BacktestConfig(
                initial_capital=10_000,
                leverage=1.0,
                inversed=True,
                stop_loss_pct=None,
                take_profit_pct=None,
            )
            result = BacktestEngine(strategy, config).run(
                candles, symbol=symbol, timeframe=interval, mtf=mtf
            )
            row = {
                "min_rr": min_rr,
                "max_tp_pct": max_tp if max_tp is not None else "",
                "sl_mult": sl_mult,
                "return_pct": round(result.total_return_pct * 100, 2),
                "max_dd_pct": round(result.max_drawdown_pct * 100, 2),
                "trades": result.total_trades,
                "win_rate": round(result.win_rate * 100, 1),
                "wins": result.winning_trades,
                "losses": result.losing_trades,
                "final_equity": round(result.final_equity, 2),
            }
            rows.append(row)
            print(
                f"       -> {row['return_pct']:+.2f}%  WR={row['win_rate']}%  "
                f"trades={row['trades']}  DD={row['max_dd_pct']:.2f}%"
            )
        except Exception as exc:
            print(f"       ERR: {exc}")

    out_csv = ROOT / "tests" / "inversed_sweep_results.csv"
    out_txt = ROOT / "tests" / "inversed_sweep_results.txt"
    if rows:
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        ranked = sorted(rows, key=lambda r: (r["return_pct"], -r["max_dd_pct"]), reverse=True)
        lines = [
            "ALEXG INVERSED SWEEP — GBPUSD 730d 1h",
            f"Combos: {len(rows)}",
            "",
            "TOP 15 BY RETURN:",
            f"{'RR':>4} {'maxTP':>7} {'SLx':>5} {'Ret%':>8} {'WR%':>6} {'DD%':>7} {'Trades':>6}",
        ]
        for r in ranked[:15]:
            tp = r["max_tp_pct"] if r["max_tp_pct"] != "" else "none"
            lines.append(
                f"{r['min_rr']:4.1f} {str(tp):>7} {r['sl_mult']:5.2f} "
                f"{r['return_pct']:8.2f} {r['win_rate']:6.1f} "
                f"{r['max_dd_pct']:7.2f} {r['trades']:6d}"
            )
        out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nSaved: {out_csv}")
        print(f"Saved: {out_txt}")
        best = ranked[0]
        print(
            f"\nBEST: RR={best['min_rr']} max_tp={best['max_tp_pct'] or 'none'} "
            f"sl_mult={best['sl_mult']} -> {best['return_pct']:+.2f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
