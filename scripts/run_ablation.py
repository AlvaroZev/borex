#!/usr/bin/env python3
"""Run ablation grid for alexg5revised (video-2 style rule pills)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from borex.alexg.ablation import AblationConfig, iter_ablation_grid, video1_default, video2_winner
from borex.alexg.strategy5_revised import AlexG5RevisedStrategy
from borex.backtest import BacktestConfig, BacktestEngine
from borex.data import load_market_data


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", "-s", default="EURUSD=X")
    p.add_argument("--period", "-p", default="2y")
    p.add_argument("--interval", "-i", default="1h")
    p.add_argument("--capital", type=float, default=10_000.0)
    p.add_argument("--leverage", "-l", type=float, default=500.0)
    p.add_argument("--min-rr", type=float, default=3.0)
    p.add_argument("--rr-mode", choices=["fixed", "dynamic"], default="fixed")
    p.add_argument("--rr-factor", type=float, default=1.0)
    p.add_argument("--use-cache", action="store_true")
    p.add_argument(
        "--quick",
        action="store_true",
        help="Only video1 default + video2 winner (smoke), not full 200 grid",
    )
    p.add_argument(
        "--max-configs",
        type=int,
        default=0,
        help="Cap number of ablation configs (0 = all)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("data/runs/ablation_alexg5revised.csv"),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore existing .partial.csv checkpoint and re-run every config",
    )
    return p.parse_args()


def _configs(args: argparse.Namespace) -> list[AblationConfig]:
    if args.quick:
        return [video1_default(), video2_winner()]
    grid = iter_ablation_grid()
    if args.max_configs and args.max_configs > 0:
        return grid[: args.max_configs]
    return grid


def main() -> int:
    args = parse_args()
    cache_mode = "only" if args.use_cache else "auto"
    try:
        candles = load_market_data(
            args.symbol, args.period, args.interval, cache_mode=cache_mode
        )
    except Exception as exc:
        print(f"Data error: {exc}", file=sys.stderr)
        return 1

    configs = _configs(args)
    print(f"Ablation: {len(configs)} configs on {args.symbol} {args.period} {args.interval}")
    print(f"Bars: {len(candles)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    # Checkpoint file so an abort/crash never loses completed configs, and a
    # re-run can skip labels that already finished.
    partial_path = args.out.with_suffix(".partial.csv")
    done_labels: set[str] = set()
    if partial_path.is_file() and not args.force:
        with partial_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done_labels.add(row["label"])
                for key in ("trades", "rank_input"):
                    row[key] = int(float(row[key]))
                for key in (
                    "win_rate",
                    "return_pct",
                    "max_dd_pct",
                    "profit_factor",
                    "final_equity",
                ):
                    row[key] = float(row[key])
                for key in ("require_chart_trend", "require_pattern", "require_retest"):
                    row[key] = row[key] == "True"
                rows.append(row)
        if rows:
            print(f"Resuming: {len(rows)} configs already done in {partial_path}")

    partial_fh = partial_path.open("a", newline="", encoding="utf-8")
    partial_writer: csv.DictWriter | None = None

    for i, abl in enumerate(configs, 1):
        if abl.label() in done_labels:
            continue
        strategy = AlexG5RevisedStrategy(
            min_rr=args.min_rr,
            ablation=abl,
            execution_interval=args.interval,
        )
        strategy.set_context(args.symbol)
        config = BacktestConfig(
            initial_capital=args.capital,
            leverage=args.leverage,
            size_mode="fixed_risk",
            risk_per_trade_pct=0.01,
            true_sl=False,
            true_sl_rr=args.min_rr,
            rr_mode=args.rr_mode,
            rr_factor=args.rr_factor,
            stop_loss_pct=None,
            take_profit_pct=None,
        )
        engine = BacktestEngine(strategy, config)
        result = engine.run(candles, args.symbol, args.interval)
        row = {
            "rank_input": i,
            "label": abl.label(),
            **abl.to_dict(),
            "trades": result.total_trades,
            "win_rate": round(result.win_rate, 6),
            "return_pct": round(result.total_return_pct, 6),
            "max_dd_pct": round(result.max_drawdown_pct, 6),
            "profit_factor": round(result.profit_factor, 6),
            "final_equity": round(result.final_equity, 2),
        }
        rows.append(row)
        if partial_writer is None:
            partial_writer = csv.DictWriter(partial_fh, fieldnames=list(row.keys()))
            if partial_fh.tell() == 0:
                partial_writer.writeheader()
        partial_writer.writerow(row)
        partial_fh.flush()
        print(
            f"[{i}/{len(configs)}] PF={row['profit_factor']:.3f} "
            f"WR={row['win_rate']:.1%} ret={row['return_pct']:.1%} "
            f"n={row['trades']} | {abl.label()}",
            flush=True,
        )

    partial_fh.close()
    rows.sort(key=lambda r: (r["profit_factor"], r["return_pct"]), reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank_pf"] = rank

    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)

    summary = {
        "symbol": args.symbol,
        "period": args.period,
        "interval": args.interval,
        "configs": len(rows),
        "top": rows[:5] if rows else [],
        "out": str(args.out),
    }
    summary_path = args.out.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nWrote {args.out}")
    print(f"Summary {summary_path}")
    if rows:
        best = rows[0]
        print(
            f"Best PF={best['profit_factor']:.3f} {best['label']} "
            f"(ret={best['return_pct']:.1%}, n={best['trades']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
