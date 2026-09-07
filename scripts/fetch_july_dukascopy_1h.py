#!/usr/bin/env python3
"""Force-download Dukascopy 1h for Jun+Jul 2026 (month chunks) and merge into cache.

Year-range dukascopy-node calls often fail for 2026; month windows work.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

MAIN = Path(__file__).resolve().parents[1]
BOREX = MAIN.parent / "borex"
sys.path.insert(0, str(MAIN))
sys.path.insert(0, str(BOREX))

import borex.config as cfg

cfg.ROOT_DIR = MAIN
cfg.CACHE_DIR = MAIN / "data" / "cache"

from borex.data.dukascopy import load_dukascopy_csv  # type: ignore
from borex.data.dukascopy_download import (  # type: ignore
    SYMBOL_TO_DUKASCOPY,
    TIMEFRAME_TO_DUKASCOPY,
    _run_npx,
)
from borex.data.store import is_cached, load_ohlcv, save_ohlcv  # type: ignore
from borex.data.symbols import FOREX_PAIRS  # type: ignore

# Cover warmup for Jul 6 week through end of July
WINDOWS = [
    ("2026-06-01", "2026-07-01"),
    ("2026-07-01", "2026-08-01"),
]


def fetch_window(instrument: str, tf_token: str, start: str, end: str, out_dir: Path) -> pd.DataFrame:
    csv_path = _run_npx(instrument, tf_token, start, end, out_dir)
    return load_dukascopy_csv(csv_path)


def merge_into_cache(symbol: str, frames: list[pd.DataFrame]) -> tuple[int, str, str]:
    parts = list(frames)
    if is_cached(symbol, "1h", cfg.CACHE_DIR):
        parts.insert(0, load_ohlcv(symbol, "1h", cfg.CACHE_DIR))
    merged = pd.concat(parts).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    save_ohlcv(merged, symbol, "1h", source="dukascopy")
    return len(merged), str(merged.index.min()), str(merged.index.max())


def main() -> int:
    out_dir = MAIN / "download"
    out_dir.mkdir(parents=True, exist_ok=True)
    symbols = list(FOREX_PAIRS)
    print(f"Cache: {cfg.CACHE_DIR}", flush=True)
    print(f"Downloading 1h month chunks for {len(symbols)} pairs: {WINDOWS}", flush=True)

    ok, fail = 0, []
    for i, symbol in enumerate(symbols, 1):
        instrument = SYMBOL_TO_DUKASCOPY.get(symbol)
        if not instrument:
            fail.append((symbol, "no dukascopy instrument map"))
            print(f"  [{i}/{len(symbols)}] SKIP {symbol}: unmapped", flush=True)
            continue
        frames: list[pd.DataFrame] = []
        errors: list[str] = []
        for start, end in WINDOWS:
            try:
                df = fetch_window(instrument, "h1", start, end, out_dir)
                if df is not None and not df.empty:
                    frames.append(df)
                    print(
                        f"  [{i}/{len(symbols)}] {symbol} {start}..{end}: {len(df)} bars",
                        flush=True,
                    )
                else:
                    errors.append(f"{start}: empty")
            except Exception as exc:
                errors.append(f"{start}: {exc}")
                print(f"  [{i}/{len(symbols)}] {symbol} {start} FAIL: {exc}", flush=True)
        if not frames:
            fail.append((symbol, "; ".join(errors) or "no frames"))
            continue
        try:
            n, a, b = merge_into_cache(symbol, frames)
            print(f"  [{i}/{len(symbols)}] MERGED {symbol}: {n} bars {a} -> {b}", flush=True)
            ok += 1
        except Exception as exc:
            fail.append((symbol, f"merge: {exc}"))
            print(f"  [{i}/{len(symbols)}] MERGE FAIL {symbol}: {exc}", flush=True)

    print(f"\nDone: ok={ok} fail={len(fail)}", flush=True)
    for s, e in fail:
        print(f"  fail {s}: {e}", flush=True)

    # July coverage report
    print("\nJuly 2026 coverage:", flush=True)
    good = 0
    for symbol in symbols:
        try:
            if not is_cached(symbol, "1h", cfg.CACHE_DIR):
                print(f"  {symbol}: NO CACHE", flush=True)
                continue
            df = load_ohlcv(symbol, "1h", cfg.CACHE_DIR)
            idx = pd.DatetimeIndex(df.index)
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            n = int(((idx >= "2026-07-01") & (idx < "2026-08-01")).sum())
            last = idx.max()
            flag = "OK" if n >= 400 else "THIN"
            if n >= 400:
                good += 1
            print(f"  {flag} {symbol}: july={n} last={last}", flush=True)
        except Exception as exc:
            print(f"  ERR {symbol}: {exc}", flush=True)
    print(f"Pairs with solid July: {good}/{len(symbols)}", flush=True)
    return 0 if ok >= 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())
