#!/usr/bin/env python3
"""
Ensure ~3 years of 1m FX data in data/cache.

Existing HistData/Dukascopy parquet is reused. Missing range is filled via the
sibling borex dukascopy downloader when available.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAIN_DIR = Path(__file__).resolve().parents[1]
BOREX_REPO = MAIN_DIR.parent / "borex"
# Prefer sibling borex (dukascopy download + cache_info), then borex-main.
sys.path.insert(0, str(MAIN_DIR))
if BOREX_REPO.is_dir():
    sys.path.insert(0, str(BOREX_REPO))

import borex.config as cfg

cfg.ROOT_DIR = MAIN_DIR
cfg.CACHE_DIR = MAIN_DIR / "data" / "cache"

from borex.data.symbols import FOREX_PAIRS  # type: ignore

try:
    from borex.data.store import cache_info, is_cached  # type: ignore
except ImportError:
    # Fallback: borex-main store without cache_info — use manifests.
    from borex.data.store import is_cached, read_manifest  # type: ignore

    def cache_info(symbol: str, timeframe: str, cache_dir=None):
        return read_manifest(symbol, timeframe, cache_dir)


TARGET_YEARS = 3
# Existing HistData cache starts ~2023-09 (~2.75y). Accept that as enough for LTF work.
MIN_COVERAGE_DAYS = int(365 * 2.7)
TIMEFRAME = "1m"


def _parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _fmt_day(value) -> str:
    return _parse_ts(value).strftime("%Y-%m-%d")


def coverage_ok(info: dict, *, min_days: int = MIN_COVERAGE_DAYS) -> bool:
    start = _parse_ts(info["start"])
    end = _parse_ts(info["end"])
    return (end - start) >= timedelta(days=min_days)


def main() -> int:
    print(f"Cache: {cfg.CACHE_DIR}")
    print(f"Target: ~{TARGET_YEARS}y of {TIMEFRAME} for {len(FOREX_PAIRS)} pairs\n")

    now = datetime.now(timezone.utc)
    target_start = (now - timedelta(days=365 * TARGET_YEARS)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    ok = 0
    need = 0
    for i, symbol in enumerate(FOREX_PAIRS, 1):
        info = cache_info(symbol, TIMEFRAME) if is_cached(symbol, TIMEFRAME) else None
        if info and coverage_ok(info):
            print(
                f"[{i}/{len(FOREX_PAIRS)}] OK  {symbol}: "
                f"{info.get('bars', '?')} bars "
                f"{_fmt_day(info['start'])} -> {_fmt_day(info['end'])}"
            )
            ok += 1
            continue

        need += 1
        if info:
            print(
                f"[{i}/{len(FOREX_PAIRS)}] SHORT {symbol}: "
                f"{_fmt_day(info['start'])} -> {_fmt_day(info['end'])} "
                f"(want ~{TARGET_YEARS}y)"
            )
        else:
            print(f"[{i}/{len(FOREX_PAIRS)}] MISSING {symbol}")

        try:
            from borex.data.dukascopy_download import (  # type: ignore
                download_year_chunk,
                merge_year_chunks,
            )
            from borex.data.store import list_year_chunk_years  # type: ignore

            start_year = int(target_start[:4])
            end_year = int(end[:4])
            for year in range(start_year, end_year + 1):
                ys = f"{year}-01-01"
                ye = f"{year}-12-31"
                if year == start_year:
                    ys = target_start
                if year == end_year:
                    ye = end
                print(f"  downloading {year} {ys}..{ye} ...", flush=True)
                download_year_chunk(
                    symbol, TIMEFRAME, year, start=ys, end=ye, force=False
                )
            years = list_year_chunk_years(symbol, TIMEFRAME)
            years = sorted(set(years) | set(range(start_year, end_year + 1)))
            merge_year_chunks(symbol, TIMEFRAME, years)
            info2 = cache_info(symbol, TIMEFRAME)
            if info2:
                print(
                    f"  merged: {info2.get('bars', '?')} bars "
                    f"{_fmt_day(info2['start'])} -> {_fmt_day(info2['end'])}"
                )
                ok += 1
                need -= 1
        except Exception as exc:
            print(f"  ERROR: {exc}")
            print(
                "  Tip: install/run sibling borex dukascopy tools, "
                "or copy existing 1m parquet into data/cache."
            )

    report = {
        "target_years": TARGET_YEARS,
        "timeframe": TIMEFRAME,
        "ok": ok,
        "still_needed": need,
        "pairs": FOREX_PAIRS,
    }
    out = MAIN_DIR / "data" / "runs" / "1m_coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nDone. ok={ok} still_needed={need}. Report: {out}")
    return 0 if need == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
