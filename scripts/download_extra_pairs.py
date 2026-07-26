#!/usr/bin/env python3
"""Download OHLCV for MT5 expansion pairs (1h first, then 1m)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

MAIN_DIR = Path(__file__).resolve().parents[1]
BOREX_REPO = MAIN_DIR.parent / "borex"
sys.path.insert(0, str(MAIN_DIR))
if BOREX_REPO.is_dir():
    sys.path.insert(0, str(BOREX_REPO))

import borex.config as cfg

cfg.ROOT_DIR = MAIN_DIR
cfg.CACHE_DIR = MAIN_DIR / "data" / "cache"

from borex.data.dukascopy_download import download_all_dukascopy  # type: ignore
from borex.data.symbols import FOREX_PAIRS, MT5_EXTRA_PAIRS  # type: ignore


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-i",
        "--interval",
        default="1h",
        choices=("1h", "1m", "15m", "30m", "4h", "1d"),
        help="Timeframe to download (default: 1h)",
    )
    ap.add_argument("--start", default="2023-01-01", help="UTC start date YYYY-MM-DD")
    ap.add_argument(
        "--end",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="UTC end date YYYY-MM-DD",
    )
    ap.add_argument("--force", action="store_true", help="Re-download even if cached")
    ap.add_argument(
        "--all",
        action="store_true",
        help="Download all FOREX_PAIRS (default: only MT5 expansion set)",
    )
    args = ap.parse_args()

    pairs = list(FOREX_PAIRS) if args.all else list(MT5_EXTRA_PAIRS)
    if not pairs:
        print("No pairs to download", file=sys.stderr)
        return 1

    print(f"Cache: {cfg.CACHE_DIR}")
    print(f"Universe size: {len(FOREX_PAIRS)}  downloading: {len(pairs)}")
    print(f"Pairs: {', '.join(pairs)}")
    print(f"Interval: {args.interval}  Range: {args.start} .. {args.end}\n")

    def on_job_start(symbol: str, timeframe: str, year: int | None) -> None:
        y = f" {year}" if year is not None else ""
        print(f"  start {symbol} {timeframe}{y}", flush=True)

    def on_progress(row: dict, ok: bool) -> None:
        status = row.get("status", "?")
        sym = row.get("symbol", "?")
        tf = row.get("timeframe", "?")
        year = row.get("year")
        y = f" {year}" if year is not None else ""
        bars = row.get("bars")
        extra = f" bars={bars}" if bars is not None else ""
        err = row.get("error")
        msg = f"  [{status}] {sym} {tf}{y}{extra}"
        if err:
            msg += f"  ERR: {err}"
        print(msg, flush=True)

    results = download_all_dukascopy(
        symbols=pairs,
        timeframes=[args.interval],
        start=args.start,
        end=args.end,
        force=args.force,
        on_job_start=on_job_start,
        on_progress=on_progress,
    )

    errors = [r for r in results if r.get("status") == "error"]
    merges = [r for r in results if r.get("status") in ("ok", "skipped") and "bars" in r]
    print(f"\nDone: {len(merges)} pair/tf summaries, {len(errors)} errors")
    for r in merges:
        print(
            f"  {r['symbol']} {r['timeframe']}: {r.get('bars')} bars "
            f"{r.get('start')} -> {r.get('end')} [{r.get('status')}]"
        )
    if errors:
        print("\nErrors:")
        for r in errors:
            print(f"  {r.get('symbol')} {r.get('timeframe')} {r.get('year')}: {r.get('error')}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
