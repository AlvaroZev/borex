from __future__ import annotations

import itertools
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from borex.config import ROOT_DIR
from borex.data.dukascopy import DUKASCOPY_TO_SYMBOL, load_dukascopy_csv
from borex.data.store import (
    cache_path,
    covers_range,
    has_year_data,
    is_cached,
    is_year_cached,
    load_ohlcv,
    save_ohlcv,
    save_year_chunk,
    year_chunk_path,
)
from borex.data.symbols import FOREX_PAIRS

ProgressCallback = Callable[[dict, bool], None]
JobStartCallback = Callable[[str, str, int | None], None]
ActivityCallback = Callable[[], None]

SYMBOL_TO_DUKASCOPY: dict[str, str] = {v: k for k, v in DUKASCOPY_TO_SYMBOL.items()}

TIMEFRAME_TO_DUKASCOPY: dict[str, str] = {
    "1m": "m1",
    "15m": "m15",
    "30m": "m30",
    "1h": "h1",
    "4h": "h4",
    "1d": "d1",
}

DUKASCOPY_TF_ORDER = ["1d", "4h", "1h", "30m", "15m", "1m"]

DEFAULT_START = "2020-01-01"
DEFAULT_END = "2026-12-31"
DEFAULT_DUKASCOPY_TIMEFRAMES = ("15m", "1h", "4h", "1wk")
DOWNLOAD_DIR = ROOT_DIR / "download"


@dataclass(frozen=True)
class DukascopyWorkItem:
    symbol: str
    timeframe: str
    year: int | None = None
    action: str = "download"  # download | skip | merge | weekly


def iter_year_ranges(start: str, end: str) -> list[tuple[str, str, int]]:
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    out: list[tuple[str, str, int]] = []
    for year in range(start_dt.year, end_dt.year + 1):
        ys = datetime(year, 1, 1, tzinfo=timezone.utc)
        ye = datetime(year, 12, 31, tzinfo=timezone.utc)
        if ys < start_dt:
            ys = start_dt
        if ye > end_dt:
            ye = end_dt
        out.append((ys.strftime("%Y-%m-%d"), ye.strftime("%Y-%m-%d"), year))
    return out


def _find_csv(instrument: str, tf_token: str, start: str, end: str, directory: Path) -> Path | None:
    pattern = f"{instrument}-{tf_token}-bid-{start}-{end}.csv"
    direct = directory / pattern
    if direct.is_file():
        return direct
    matches = sorted(
        directory.glob(f"{instrument}-{tf_token}-bid-{start}-*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _run_npx(
    instrument: str,
    tf_token: str,
    start: str,
    end: str,
    out_dir: Path,
    *,
    timeout_sec: int = 7200,
    on_activity: ActivityCallback | None = None,
) -> Path:
    import threading

    stop = threading.Event()

    def _heartbeat() -> None:
        while not stop.wait(10):
            if on_activity:
                on_activity()

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()
    if on_activity:
        on_activity()

    try:
        cmd = [
            "npx",
            "dukascopy-node",
            "-i",
            instrument,
            "-from",
            start,
            "-to",
            end,
            "-t",
            tf_token,
            "-f",
            "csv",
            "-dir",
            str(out_dir),
            "-s",
        ]
        run_kwargs: dict = {
            "cwd": str(ROOT_DIR),
            "capture_output": True,
            "text": True,
            "timeout": timeout_sec,
        }
        if sys.platform == "win32":
            run_kwargs["shell"] = True
            cmd = subprocess.list2cmdline(cmd)
        result = subprocess.run(cmd, **run_kwargs)
        if result.returncode != 0:
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            err = stdout if stdout else stderr
            if stdout and stderr and "fetch failed" in stdout.lower():
                err = f"{stdout}\n{stderr}"
            elif not err:
                err = "dukascopy-node failed"
            raise RuntimeError(err[:800])

        csv_path = _find_csv(instrument, tf_token, start, end, out_dir)
        if not csv_path:
            raise FileNotFoundError(f"No CSV for {instrument} {tf_token} {start}..{end}")
        return csv_path
    finally:
        stop.set()


def download_year_chunk(
    symbol: str,
    timeframe: str,
    year: int,
    *,
    start: str,
    end: str,
    download_dir: Path | None = None,
    force: bool = False,
    on_activity: ActivityCallback | None = None,
) -> Path:
    """Download one calendar year and store under cache/.../years/{year}.parquet."""
    chunk_path = year_chunk_path(symbol, timeframe, year)
    if not force and has_year_data(symbol, timeframe, year):
        if not chunk_path.is_file() and is_cached(symbol, timeframe):
            df = load_ohlcv(symbol, timeframe)
            ydf = df[df.index.year == year]
            if not ydf.empty:
                save_year_chunk(ydf, symbol, timeframe, year)
        return chunk_path

    instrument = SYMBOL_TO_DUKASCOPY.get(symbol)
    tf_token = TIMEFRAME_TO_DUKASCOPY.get(timeframe)
    if not instrument or not tf_token:
        raise ValueError(f"Unsupported symbol/timeframe for Dukascopy: {symbol} {timeframe}")

    out_dir = download_dir or DOWNLOAD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = _run_npx(instrument, tf_token, start, end, out_dir, on_activity=on_activity)
    df = load_dukascopy_csv(csv_path)
    return save_year_chunk(df, symbol, timeframe, year)


def merge_year_chunks(symbol: str, timeframe: str, years: list[int]) -> Path:
    frames: list[pd.DataFrame] = []
    main_df: pd.DataFrame | None = None
    if is_cached(symbol, timeframe):
        main_df = load_ohlcv(symbol, timeframe)

    for year in sorted(set(years)):
        p = year_chunk_path(symbol, timeframe, year)
        if p.is_file():
            frames.append(_normalize_year_df(pd.read_parquet(p)))
        elif main_df is not None:
            ydf = main_df[main_df.index.year == year]
            if not ydf.empty:
                frames.append(ydf)
    if not frames:
        raise FileNotFoundError(f"No year chunks for {symbol} {timeframe}")
    merged = pd.concat(frames).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    return save_ohlcv(merged, symbol, timeframe, source="dukascopy")


def _normalize_year_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = pd.to_datetime(out.index, utc=True)
    return out


def _build_weekly_from_daily(symbol: str, *, force: bool = False) -> tuple[Path, str]:
    if is_cached(symbol, "1wk") and not force:
        return cache_path(symbol, "1wk"), "skipped"
    if not is_cached(symbol, "1d"):
        raise FileNotFoundError("daily data required to build weekly")
    daily = load_ohlcv(symbol, "1d")
    weekly = daily.resample("1W").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna()
    return save_ohlcv(weekly, symbol, "1wk", source="dukascopy"), "ok"


def plan_dukascopy_work(
    *,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    start: str = DEFAULT_START,
    end: str | None = None,
    force: bool = False,
) -> list[DukascopyWorkItem]:
    syms = symbols or FOREX_PAIRS
    requested = timeframes or list(DEFAULT_DUKASCOPY_TIMEFRAMES)
    tfs = [t for t in DUKASCOPY_TF_ORDER if t in requested and t != "1wk"]
    if not tfs:
        tfs = [t for t in requested if t != "1wk"]
    end = end or DEFAULT_END
    items: list[DukascopyWorkItem] = []

    for symbol, tf in itertools.product(syms, tfs):
        if not force and covers_range(symbol, tf, start, end):
            items.append(DukascopyWorkItem(symbol, tf, action="skip"))
            continue
        for _, _, year in iter_year_ranges(start, end):
            if not force and has_year_data(symbol, tf, year):
                items.append(DukascopyWorkItem(symbol, tf, year, "skip"))
            else:
                items.append(DukascopyWorkItem(symbol, tf, year, "download"))
        items.append(DukascopyWorkItem(symbol, tf, action="merge"))

    if "1wk" in requested:
        for symbol in syms:
            if not force and is_cached(symbol, "1wk"):
                items.append(DukascopyWorkItem(symbol, "1wk", action="skip"))
            else:
                items.append(DukascopyWorkItem(symbol, "1wk", action="weekly"))

    return items


def count_dukascopy_jobs(
    *,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    start: str = DEFAULT_START,
    end: str | None = None,
    force: bool = False,
) -> int:
    return len(
        plan_dukascopy_work(
            symbols=symbols,
            timeframes=timeframes,
            start=start,
            end=end,
            force=force,
        )
    )


def _row(symbol: str, tf: str, path: Path, status: str, **extra) -> dict:
    df = load_ohlcv(symbol, tf)
    return {
        "symbol": symbol,
        "timeframe": tf,
        "bars": len(df),
        "start": str(df.index.min()),
        "end": str(df.index.max()),
        "path": str(path),
        "source": "dukascopy",
        "status": status,
        **extra,
    }


def download_all_dukascopy(
    *,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    start: str = DEFAULT_START,
    end: str | None = None,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
    on_job_start: JobStartCallback | None = None,
    on_activity: ActivityCallback | None = None,
) -> list[dict]:
    end = end or DEFAULT_END
    work = plan_dukascopy_work(
        symbols=symbols,
        timeframes=timeframes,
        start=start,
        end=end,
        force=force,
    )
    results: list[dict] = []

    # Group consecutive year jobs per symbol×tf, then merge
    i = 0
    while i < len(work):
        item = work[i]
        if item.action == "weekly":
            if on_job_start:
                on_job_start(item.symbol, "1wk", None)
            try:
                path, status = _build_weekly_from_daily(item.symbol, force=force)
                row = _row(item.symbol, "1wk", path, status)
                results.append(row)
                if on_progress:
                    on_progress(row, True)
            except Exception as exc:
                row = {"symbol": item.symbol, "timeframe": "1wk", "source": "dukascopy", "status": "error", "error": str(exc)}
                results.append(row)
                if on_progress:
                    on_progress(row, False)
            i += 1
            continue

        if item.action == "skip" and item.year is None:
            if on_job_start:
                on_job_start(item.symbol, item.timeframe, None)
            path = cache_path(item.symbol, item.timeframe)
            info = load_ohlcv(item.symbol, item.timeframe)
            row = {
                "symbol": item.symbol,
                "timeframe": item.timeframe,
                "bars": len(info),
                "start": str(info.index.min()),
                "end": str(info.index.max()),
                "path": str(path),
                "source": "dukascopy",
                "status": "skipped",
            }
            results.append(row)
            if on_progress:
                on_progress(row, True)
            i += 1
            continue

        # Year batch until merge
        symbol, tf = item.symbol, item.timeframe
        year_items: list[DukascopyWorkItem] = []
        while i < len(work) and work[i].symbol == symbol and work[i].timeframe == tf and work[i].year is not None:
            year_items.append(work[i])
            i += 1
        merge_item = work[i] if i < len(work) and work[i].action == "merge" else None
        if merge_item:
            i += 1

        year_nums = [y.year for y in year_items if y.year is not None]
        for yi in year_items:
            if on_job_start:
                on_job_start(yi.symbol, yi.timeframe, yi.year)
            if yi.action == "skip":
                row = {
                    "symbol": yi.symbol,
                    "timeframe": yi.timeframe,
                    "year": yi.year,
                    "source": "dukascopy",
                    "status": "skipped",
                }
                results.append(row)
                if on_progress:
                    on_progress(row, True)
                continue
            try:
                yr = yi.year
                assert yr is not None
                ranges = {y: (s, e) for s, e, y in iter_year_ranges(start, end)}
                ys, ye = ranges[yr]
                download_year_chunk(
                    yi.symbol,
                    yi.timeframe,
                    yr,
                    start=ys,
                    end=ye,
                    force=force,
                    on_activity=on_activity,
                )
                row = {
                    "symbol": yi.symbol,
                    "timeframe": yi.timeframe,
                    "year": yr,
                    "source": "dukascopy",
                    "status": "ok",
                }
                results.append(row)
                if on_progress:
                    on_progress(row, True)
            except Exception as exc:
                row = {
                    "symbol": yi.symbol,
                    "timeframe": yi.timeframe,
                    "year": yi.year,
                    "source": "dukascopy",
                    "status": "error",
                    "error": str(exc),
                }
                results.append(row)
                if on_progress:
                    on_progress(row, False)

        if merge_item:
            if on_job_start:
                on_job_start(symbol, tf, None)
            try:
                path = merge_year_chunks(symbol, tf, year_nums)
                row = _row(symbol, tf, path, "ok")
                results.append(row)
                if on_progress:
                    on_progress(row, True)
            except Exception as exc:
                row = {"symbol": symbol, "timeframe": tf, "source": "dukascopy", "status": "error", "error": str(exc)}
                results.append(row)
                if on_progress:
                    on_progress(row, False)

    return results
