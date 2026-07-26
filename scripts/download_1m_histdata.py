#!/usr/bin/env python3
"""Download 1m FX bars from HistData.com (not Dukascopy) for the Yahoo 1h window.

Yahoo 1h cache spans ~2023-09-18 .. ~2026-07. Yahoo only keeps ~30d of 1m,
so this pulls HistData M1 ASCII zips and writes borex parquet cache.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
from histdata import download_hist_data as dl
from histdata.api import Platform as P
from histdata.api import TimeFrame as TF

MAIN_DIR = Path(__file__).resolve().parents[1]
BOREX_REPO = MAIN_DIR.parent / "borex"
sys.path.insert(0, str(BOREX_REPO))

import borex.config as cfg

cfg.ROOT_DIR = MAIN_DIR
cfg.CACHE_DIR = MAIN_DIR / "data" / "cache"

from borex.data.store import cache_info, save_ohlcv
from borex.data.symbols import FOREX_PAIRS

# Match cached 1h start; HistData times are EST (UTC-5, no DST).
START = pd.Timestamp("2023-09-18", tz="UTC")
END = pd.Timestamp(date.today().isoformat(), tz="UTC") + pd.Timedelta(days=1)
HISTDATA_TZ = "Etc/GMT+5"  # fixed UTC-5 (POSIX sign flip)
DOWNLOAD_DIR = MAIN_DIR / "download" / "histdata"
TIMEFRAME = "1m"
PAIR_WAIT_SEC = 2.0


def _pair_token(symbol: str) -> str:
    return symbol.replace("=X", "").replace("/", "").lower()


def _jobs_for_range(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[str, str | None]]:
    """Past years = full-year zip; current year = monthly zips."""
    jobs: list[tuple[str, str | None]] = []
    today = date.today()
    for year in range(start.year, end.year + 1):
        if year < today.year:
            jobs.append((str(year), None))
        elif year == today.year:
            first_month = start.month if year == start.year else 1
            last_month = min(end.month, today.month)
            for month in range(first_month, last_month + 1):
                jobs.append((str(year), str(month)))
    return jobs


def _load_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        raw = zf.read(csv_name)
    df = pd.read_csv(
        io.BytesIO(raw),
        sep=";",
        header=None,
        names=["DateTime", "Open", "High", "Low", "Close", "Volume"],
    )
    # HistData: "YYYYMMDD HHMMSS" in EST (UTC-5, no DST)
    ts = pd.to_datetime(df["DateTime"], format="%Y%m%d %H%M%S")
    df = df.drop(columns=["DateTime"])
    df.index = ts.dt.tz_localize(HISTDATA_TZ).dt.tz_convert("UTC")
    df = df.astype(float)
    return df.sort_index()


def _download_pair(symbol: str, *, force: bool = False, delay: float = PAIR_WAIT_SEC) -> dict:
    pair = _pair_token(symbol)
    out_dir = DOWNLOAD_DIR / pair
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = _jobs_for_range(START, END)
    frames: list[pd.DataFrame] = []
    for i, (year, month) in enumerate(jobs, 1):
        label = f"{year}-{int(month):02d}" if month else year
        print(f"  [{i}/{len(jobs)}] {symbol} {label}", flush=True)
        try:
            zip_path = Path(
                dl(
                    year=year,
                    month=month,
                    pair=pair,
                    platform=P.GENERIC_ASCII,
                    time_frame=TF.ONE_MINUTE,
                    output_directory=str(out_dir),
                    verbose=False,
                )
            )
        except Exception as exc:
            # Current month often unpublished yet on HistData.
            print(f"    skip (unavailable): {exc}", flush=True)
            continue
        if not zip_path.is_file() or zip_path.stat().st_size < 100:
            print(f"    skip (empty zip): {zip_path}", flush=True)
            continue
        try:
            frames.append(_load_zip(zip_path))
        except Exception as exc:
            print(f"    skip (bad zip): {exc}", flush=True)
            continue
        if delay > 0 and i < len(jobs):
            time.sleep(delay)

    if not frames:
        raise ValueError(f"No HistData frames for {symbol}")

    merged = pd.concat(frames).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    merged = merged.loc[(merged.index >= START) & (merged.index < END)]
    if merged.empty:
        raise ValueError(f"No bars in [{START.date()} .. {END.date()}) for {symbol}")

    path = save_ohlcv(merged, symbol, TIMEFRAME, source="histdata")
    return {
        "symbol": symbol,
        "bars": len(merged),
        "start": str(merged.index.min()),
        "end": str(merged.index.max()),
        "path": str(path),
        "status": "ok",
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--pair",
        "-s",
        action="append",
        dest="pairs",
        help="Symbol (repeatable). Default: all FOREX_PAIRS",
    )
    p.add_argument("--force", action="store_true", help="Re-download even if cache looks full")
    p.add_argument(
        "--delay",
        type=float,
        default=PAIR_WAIT_SEC,
        help=f"Seconds between zip requests (default {PAIR_WAIT_SEC})",
    )
    p.add_argument(
        "--min-bars",
        type=int,
        default=500_000,
        help="Skip pair if cached 1m already has at least this many bars (unless --force)",
    )
    p.add_argument(
        "--end-tolerance-days",
        type=int,
        default=21,
        help="Skip if cache end is within this many days of today (HistData lags)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols = args.pairs or list(FOREX_PAIRS)
    normalized: list[str] = []
    for s in symbols:
        sym = s.upper()
        if not sym.endswith("=X"):
            sym = f"{sym}=X"
        normalized.append(sym)

    print(f"Cache: {cfg.CACHE_DIR}", flush=True)
    print(f"Window: {START.date()} .. {END.date()} (HistData M1, EST->UTC)", flush=True)
    print(f"Pairs: {len(normalized)}", flush=True)

    ok = 0
    end_ok = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=args.end_tolerance_days)
    for i, symbol in enumerate(normalized, 1):
        info = cache_info(symbol, TIMEFRAME)
        if (
            not args.force
            and info
            and info["bars"] >= args.min_bars
            and info["start"] <= START
            and info["end"] >= end_ok
        ):
            print(
                f"\n[{i}/{len(normalized)}] SKIP {symbol} "
                f"({info['bars']:,} bars {info['start'].date()}->{info['end'].date()})",
                flush=True,
            )
            ok += 1
            continue

        print(f"\n[{i}/{len(normalized)}] {symbol}", flush=True)
        try:
            row = _download_pair(symbol, force=args.force, delay=args.delay)
            print(
                f"  OK {row['bars']:,} bars {row['start']} -> {row['end']}",
                flush=True,
            )
            ok += 1
        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)
            return 1

    print(f"\nDone: {ok}/{len(normalized)} pairs", flush=True)
    return 0 if ok == len(normalized) else 1


if __name__ == "__main__":
    raise SystemExit(main())
