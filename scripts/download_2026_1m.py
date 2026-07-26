#!/usr/bin/env python3
"""Download 2026 1m Dukascopy bars for all FX pairs (one symbol at a time)."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

MAIN_DIR = Path(__file__).resolve().parents[1]
BOREX_REPO = MAIN_DIR.parent / "borex"
sys.path.insert(0, str(BOREX_REPO))

import borex.config as cfg

cfg.ROOT_DIR = MAIN_DIR
cfg.CACHE_DIR = MAIN_DIR / "data" / "cache"

from borex.data.dukascopy_download import download_year_chunk, merge_year_chunks
from borex.data.store import cache_info, list_year_chunk_years
from borex.data.symbols import FOREX_PAIRS

YEAR = 2026
START = "2026-01-01"
END = date.today().isoformat()
TIMEFRAME = "1m"


def main() -> int:
    print(f"Cache: {cfg.CACHE_DIR}", flush=True)
    print(f"Downloading {TIMEFRAME} {START}..{END} for {len(FOREX_PAIRS)} pairs", flush=True)

    for i, symbol in enumerate(FOREX_PAIRS, 1):
        print(f"\n[{i}/{len(FOREX_PAIRS)}] {symbol}", flush=True)
        try:
            download_year_chunk(
                symbol,
                TIMEFRAME,
                YEAR,
                start=START,
                end=END,
                force=True,
            )
            years = list_year_chunk_years(symbol, TIMEFRAME)
            if YEAR not in years:
                years.append(YEAR)
            merge_year_chunks(symbol, TIMEFRAME, sorted(set(years)))
            info = cache_info(symbol, TIMEFRAME)
            if info:
                print(
                    f"  ok: {info['bars']:,} bars "
                    f"{info['start']} -> {info['end']}",
                    flush=True,
                )
            else:
                print("  warn: merged but cache_info empty", flush=True)
        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)
            return 1

    print("\nAll downloads complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
