#!/usr/bin/env python3
"""Download 2026 1m Dukascopy data (monthly chunks) with interactive pair-by-pair control."""

from __future__ import annotations

import argparse
import calendar
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

MAIN_DIR = Path(__file__).resolve().parents[1]
BOREX_REPO = MAIN_DIR.parent / "borex"
sys.path.insert(0, str(BOREX_REPO))

import borex.config as cfg

cfg.ROOT_DIR = MAIN_DIR
cfg.CACHE_DIR = MAIN_DIR / "data" / "cache"

from borex.data.dukascopy import load_dukascopy_csv
from borex.data.dukascopy_download import (
    DOWNLOAD_DIR,
    SYMBOL_TO_DUKASCOPY,
    TIMEFRAME_TO_DUKASCOPY,
    _run_npx,
    merge_year_chunks,
)
from borex.data.store import cache_info, list_year_chunk_years, save_year_chunk, year_chunk_path
from borex.data.symbols import FOREX_PAIRS

YEAR = 2026
END = date.today()
TIMEFRAME = "1m"
SKIP_IF_BARS = 200_000
MAX_RETRIES = 5
RETRY_WAIT_SEC = 30
MONTH_WAIT_SEC = 60


def _month_ranges(year: int, end: date) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for month in range(1, 13):
        start_d = date(year, month, 1)
        if start_d > end:
            break
        last_day = calendar.monthrange(year, month)[1]
        end_d = min(date(year, month, last_day), end)
        out.append((start_d.isoformat(), end_d.isoformat(), month))
    return out


def _needs_download(symbol: str) -> bool:
    info = cache_info(symbol, TIMEFRAME)
    if info is None:
        return True
    return info["bars"] < SKIP_IF_BARS


def _status_rows() -> list[str]:
    rows: list[str] = []
    for sym in FOREX_PAIRS:
        info = cache_info(sym, TIMEFRAME)
        if info and info["bars"] >= SKIP_IF_BARS:
            tag = "OK"
            detail = f"{info['bars']:,} bars"
        elif info:
            tag = "thin"
            detail = f"{info['bars']:,} bars"
        else:
            tag = "missing"
            detail = "—"
        rows.append(f"  {sym:12s} [{tag:7s}] {detail}")
    return rows


def _download_chunk(instrument: str, tf_token: str, start_s: str, end_s: str) -> pd.DataFrame:
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            csv_path = _run_npx(instrument, tf_token, start_s, end_s, DOWNLOAD_DIR)
            return load_dukascopy_csv(csv_path)
        except Exception as exc:
            last_err = exc
            print(f"    retry {attempt}/{MAX_RETRIES}: {exc}", flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SEC)
    assert last_err is not None
    raise last_err


def _append_month_chunk(symbol: str, month_df: pd.DataFrame) -> None:
    chunk_path = year_chunk_path(symbol, TIMEFRAME, YEAR)
    if chunk_path.is_file():
        existing = pd.read_parquet(chunk_path)
        existing.index = pd.to_datetime(existing.index, utc=True)
        merged = pd.concat([existing, month_df]).sort_index()
    else:
        merged = month_df.sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    save_year_chunk(merged, symbol, TIMEFRAME, YEAR)


def _finalize_pair(symbol: str) -> None:
    years = sorted(set(list_year_chunk_years(symbol, TIMEFRAME)) | {YEAR})
    merge_year_chunks(symbol, TIMEFRAME, years)


def _download_pair_monthly(
    symbol: str,
    *,
    months: list[int] | None = None,
    month_wait_sec: int = MONTH_WAIT_SEC,
) -> None:
    instrument = SYMBOL_TO_DUKASCOPY.get(symbol)
    tf_token = TIMEFRAME_TO_DUKASCOPY.get(TIMEFRAME)
    if not instrument or not tf_token:
        raise ValueError(f"Unsupported symbol/timeframe: {symbol} {TIMEFRAME}")

    all_months = _month_ranges(YEAR, END)
    if months is not None:
        allowed = set(months)
        all_months = [m for m in all_months if m[2] in allowed]
    if not all_months:
        raise ValueError("No months to download")

    total = len(all_months)
    for i, (start_s, end_s, month) in enumerate(all_months, 1):
        print(f"  {month:02d}/2026 ({i}/{total}): {start_s} .. {end_s}", flush=True)
        month_df = _download_chunk(instrument, tf_token, start_s, end_s)
        _append_month_chunk(symbol, month_df)
        print(f"    saved (+{len(month_df):,} bars)", flush=True)
        if i < total and month_wait_sec > 0:
            print(f"    waiting {month_wait_sec}s before next month...", flush=True)
            time.sleep(month_wait_sec)

    _finalize_pair(symbol)
    info = cache_info(symbol, TIMEFRAME)
    if info:
        print(
            f"  done: {info['bars']:,} bars {info['start']} -> {info['end']}",
            flush=True,
        )


def _launch_viewer() -> int:
    print("\nLaunching alexg6-1m viewer...", flush=True)
    cmd = [
        sys.executable,
        "-m",
        "borex.viewer",
        "--strategy",
        "alexg6-1m",
        "-s",
        "EURUSD=X",
        "-p",
        "max",
        "-i",
        "1m",
        "--use-cache",
        "--capital",
        "1000",
        "--leverage",
        "5000",
        "--rr-factor",
        "2.3",
        "--close-on-opposite",
        "--second-signal",
        "off",
        "--position-size",
        "0.012",
        "--port",
        "8807",
    ]
    return subprocess.call(cmd, cwd=str(MAIN_DIR))


def _pick_pair(prompt: str, allow_pending_only: bool = True) -> str | None:
    pending = [s for s in FOREX_PAIRS if _needs_download(s)]
    print("\nPairs:", flush=True)
    for i, sym in enumerate(FOREX_PAIRS, 1):
        mark = "*" if sym in pending else " "
        print(f"  {i:2d}. {mark} {sym}", flush=True)
    raw = input(f"\n{prompt} [number or symbol, Enter=cancel]: ").strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(FOREX_PAIRS):
            sym = FOREX_PAIRS[idx]
            if allow_pending_only and not _needs_download(sym):
                print(f"{sym} already has enough data.", flush=True)
                return None
            return sym
        print("Invalid number.", flush=True)
        return None
    sym = raw.upper()
    if not sym.endswith("=X"):
        sym = f"{sym}=X" if len(sym) == 6 else sym
    if sym not in FOREX_PAIRS:
        print(f"Unknown pair: {sym}", flush=True)
        return None
    if allow_pending_only and not _needs_download(sym):
        print(f"{sym} already has enough data.", flush=True)
        return None
    return sym


def _interactive_loop(month_wait_sec: int) -> int:
    while True:
        pending = [s for s in FOREX_PAIRS if _needs_download(s)]
        print("\n" + "=" * 52, flush=True)
        print("  Dukascopy 2026 1m — monthly, 1 pair at a time", flush=True)
        print(f"  Pending: {len(pending)}/{len(FOREX_PAIRS)}  |  pause: {month_wait_sec}s/mo", flush=True)
        print("=" * 52, flush=True)
        print("  1) Download NEXT pending pair (all months)", flush=True)
        print("  2) Pick a pair to download (all months)", flush=True)
        print("  3) Pick a pair + ONE month only", flush=True)
        print("  4) Show cache status", flush=True)
        print("  5) Launch alexg6-1m viewer (8807)", flush=True)
        print("  6) Exit", flush=True)

        choice = input("\nChoice [1-6]: ").strip()
        if choice == "1":
            if not pending:
                print("All pairs already downloaded.", flush=True)
                continue
            symbol = pending[0]
            print(f"\n>>> {symbol}", flush=True)
            try:
                _download_pair_monthly(symbol, month_wait_sec=month_wait_sec)
            except Exception as exc:
                print(f"ERROR: {exc}", flush=True)
        elif choice == "2":
            symbol = _pick_pair("Pair to download")
            if symbol:
                print(f"\n>>> {symbol}", flush=True)
                try:
                    _download_pair_monthly(symbol, month_wait_sec=month_wait_sec)
                except Exception as exc:
                    print(f"ERROR: {exc}", flush=True)
        elif choice == "3":
            symbol = _pick_pair("Pair", allow_pending_only=False)
            if not symbol:
                continue
            month_raw = input("Month [1-12]: ").strip()
            if not month_raw.isdigit() or not 1 <= int(month_raw) <= 12:
                print("Invalid month.", flush=True)
                continue
            month = int(month_raw)
            print(f"\n>>> {symbol} month {month:02d}", flush=True)
            try:
                _download_pair_monthly(symbol, months=[month], month_wait_sec=0)
            except Exception as exc:
                print(f"ERROR: {exc}", flush=True)
        elif choice == "4":
            print("\nCache status:", flush=True)
            for row in _status_rows():
                print(row, flush=True)
        elif choice == "5":
            return _launch_viewer()
        elif choice == "6":
            return 0
        else:
            print("Invalid choice.", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--auto",
        action="store_true",
        help="Download all pending pairs without menu, then launch viewer",
    )
    p.add_argument("--pair", "-s", help="Download one pair (all months) and exit")
    p.add_argument(
        "--month",
        type=int,
        choices=range(1, 13),
        help="With --pair: download only this month (1-12)",
    )
    p.add_argument(
        "--month-wait",
        type=int,
        default=MONTH_WAIT_SEC,
        help=f"Seconds to wait between months (default: {MONTH_WAIT_SEC})",
    )
    p.add_argument(
        "--no-viewer",
        action="store_true",
        help="Do not launch alexg6 viewer after --auto",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.pair:
        sym = args.pair.upper()
        if not sym.endswith("=X"):
            sym = f"{sym}=X"
        months = [args.month] if args.month else None
        try:
            _download_pair_monthly(
                sym,
                months=months,
                month_wait_sec=0 if args.month else args.month_wait,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", flush=True)
            return 1
        return 0

    if args.auto:
        pending = [s for s in FOREX_PAIRS if _needs_download(s)]
        print(f"Auto: {len(pending)} pairs, monthly, {args.month_wait}s pause", flush=True)
        for i, symbol in enumerate(pending, 1):
            print(f"\n[{i}/{len(pending)}] {symbol}", flush=True)
            try:
                _download_pair_monthly(symbol, month_wait_sec=args.month_wait)
            except Exception as exc:
                print(f"ERROR: {exc}", flush=True)
                return 1
        if args.no_viewer:
            return 0
        return _launch_viewer()

    return _interactive_loop(args.month_wait)


if __name__ == "__main__":
    raise SystemExit(main())
