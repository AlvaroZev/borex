"""EURUSD alexg6: 1h vs 1m winrate (terminal). 1m runs monthly chunks in parallel."""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG6Strategy
from borex.backtest import BacktestConfig, BacktestEngine
from borex.data import load_market_data
from borex.models.candle import Candle

SYMBOL = "EURUSD=X"
PERIOD = "1y"
COMMON = dict(
    initial_capital=1000,
    leverage=5000,
    size_mode="margin",
    true_sl=True,
    rr_factor=2.3,
    close_on_opposite_signal=True,
    position_size_pct=0.012,
)
PROGRESS_EVERY = 1000


def _month_key(ts) -> str:
    return f"{ts.year:04d}-{ts.month:02d}"


def _split_by_month(candles: list[Candle]) -> list[tuple[str, list[Candle]]]:
    chunks: list[tuple[str, list[Candle]]] = []
    cur_key = ""
    buf: list[Candle] = []
    for c in candles:
        key = _month_key(c.timestamp)
        if key != cur_key:
            if buf:
                chunks.append((cur_key, buf))
            cur_key = key
            buf = [c]
        else:
            buf.append(c)
    if buf:
        chunks.append((cur_key, buf))
    return chunks


def _init_worker() -> None:
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"


def _run_chunk(payload: tuple[str, list[Candle], dict]) -> dict:
    month, candles, cfg = payload
    strategy = AlexG6Strategy(second_signal="off")
    label = f"{SYMBOL}[{month}]"
    t0 = time.perf_counter()
    result = BacktestEngine(strategy, BacktestConfig(**cfg)).run(
        candles,
        symbol=label,
        timeframe="1m",
        progress_every=PROGRESS_EVERY,
    )
    elapsed = time.perf_counter() - t0
    return {
        "month": month,
        "bars": len(candles),
        "trades": result.total_trades,
        "wins": result.winning_trades,
        "losses": result.losing_trades,
        "return_pct": result.total_return_pct,
        "pf": result.profit_factor,
        "max_dd": result.max_drawdown_pct,
        "elapsed_s": elapsed,
    }


def _run_1h() -> dict:
    strategy = AlexG6Strategy(second_signal="off")
    candles = load_market_data(SYMBOL, PERIOD, "1h", cache_mode="only")
    print(f"1h bars: {len(candles):,}", flush=True)
    t0 = time.perf_counter()
    r = BacktestEngine(strategy, BacktestConfig(**COMMON)).run(
        candles, symbol=SYMBOL, timeframe="1h", progress_every=PROGRESS_EVERY
    )
    return {
        "tf": "1h",
        "bars": len(candles),
        "trades": r.total_trades,
        "wins": r.winning_trades,
        "wr": r.win_rate,
        "return_pct": r.total_return_pct,
        "pf": r.profit_factor,
        "max_dd": r.max_drawdown_pct,
        "elapsed_s": time.perf_counter() - t0,
    }


def main() -> int:
    # Cap workers: many open viewers + OpenBLAS can OOM with high process counts.
    workers = min(4, max(1, (os.cpu_count() or 4) - 1))
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    skip_1h = "--skip-1h" in sys.argv
    print(
        f"{SYMBOL} alexg6 | {PERIOD} | second-signal off | rr=2.3 | margin 1.2%",
        flush=True,
    )
    print(f"progress every {PROGRESS_EVERY:,} bars | 1m workers={workers}", flush=True)
    print("-" * 60, flush=True)

    if skip_1h:
        h = {
            "tf": "1h",
            "bars": 6143,
            "trades": 28,
            "wins": 5,
            "wr": 0.1786,
            "return_pct": 0.917,
            "pf": 3.71,
            "max_dd": 0.239,
            "elapsed_s": 0.0,
        }
        print(
            f"=== 1h (cached) === trades={h['trades']} WR={h['wr']:.2%} "
            f"return={h['return_pct']:+.1%} PF={h['pf']:.2f} "
            f"maxDD={h['max_dd']:.1%}",
            flush=True,
        )
    else:
        h = _run_1h()
        print(
            f"=== 1h === trades={h['trades']} WR={h['wr']:.2%} "
            f"return={h['return_pct']:+.1%} PF={h['pf']:.2f} "
            f"maxDD={h['max_dd']:.1%} ({h['elapsed_s']:.1f}s)",
            flush=True,
        )
    print("-" * 60, flush=True)

    print("Loading 1m candles...", flush=True)
    candles = load_market_data(SYMBOL, PERIOD, "1m", cache_mode="only")
    chunks = _split_by_month(candles)
    print(
        f"1m bars: {len(candles):,} -> {len(chunks)} monthly chunks (parallel)",
        flush=True,
    )
    for month, ch in chunks:
        print(f"  {month}: {len(ch):,} bars", flush=True)

    t0 = time.perf_counter()
    payloads = [(month, ch, COMMON) for month, ch in chunks]
    results: list[dict] = []
    with ProcessPoolExecutor(
        max_workers=min(workers, len(chunks)),
        initializer=_init_worker,
    ) as pool:
        futs = {pool.submit(_run_chunk, p): p[0] for p in payloads}
        for fut in as_completed(futs):
            row = fut.result()
            results.append(row)
            wr = row["wins"] / row["trades"] if row["trades"] else 0.0
            print(
                f"[chunk done] {row['month']}: trades={row['trades']} WR={wr:.2%} "
                f"bars={row['bars']:,} ({row['elapsed_s']:.1f}s)",
                flush=True,
            )

    results.sort(key=lambda r: r["month"])
    trades = sum(r["trades"] for r in results)
    wins = sum(r["wins"] for r in results)
    wr = wins / trades if trades else 0.0
    elapsed = time.perf_counter() - t0

    print("-" * 60, flush=True)
    print(
        f"=== 1m (parallel months) === trades={trades} WR={wr:.2%} "
        f"wall={elapsed/60:.1f}m",
        flush=True,
    )
    for r in results:
        mwr = r["wins"] / r["trades"] if r["trades"] else 0.0
        print(
            f"  {r['month']}: trades={r['trades']:3d} WR={mwr:6.2%} "
            f"ret={r['return_pct']:+.1%} PF={r['pf']:.2f}",
            flush=True,
        )
    print("-" * 60, flush=True)
    print(f"1h WR={h['wr']:.2%}  vs  1m WR={wr:.2%}", flush=True)
    print(
        "Note: 1m months run independently (fresh capital each); WR is pooled.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
