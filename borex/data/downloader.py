from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import yfinance as yf

from borex.config import CACHE_DIR
from borex.data.limits import INTERVAL_LIMITS
from borex.data.store import cache_path, load_ohlcv, save_ohlcv
from borex.data.symbols import FOREX_PAIRS
from borex.data.timeframes import SUPPORTED_TIMEFRAMES, TIMEFRAMES

ProgressCallback = Callable[[dict, bool], None]


def download_symbol(
    symbol: str,
    timeframe: str,
    *,
    cache_dir: Path | None = None,
    force: bool = False,
    delay: float = 0.5,
) -> Path:
    path = cache_path(symbol, timeframe, cache_dir)
    if path.is_file() and not force:
        return path

    tf = TIMEFRAMES[timeframe]
    limit = INTERVAL_LIMITS[timeframe]
    frames: list[pd.DataFrame] = []

    if limit.max_lookback_days is None:
        df = _fetch(symbol, tf.yahoo_interval, period="max")
        if not df.empty:
            frames.append(df)
    elif limit.chunk_days >= limit.max_lookback_days:
        # Single period request avoids off-by-one day range errors.
        df = _fetch(symbol, tf.yahoo_interval, period=f"{limit.max_lookback_days}d")
        if not df.empty:
            frames.append(df)
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=limit.max_lookback_days)
        cursor_end = end
        while cursor_end > start:
            cursor_start = max(start, cursor_end - timedelta(days=limit.chunk_days))
            chunk = _fetch(
                symbol,
                tf.yahoo_interval,
                start=cursor_start.strftime("%Y-%m-%d"),
                end=cursor_end.strftime("%Y-%m-%d"),
            )
            if chunk.empty:
                break
            frames.append(chunk)
            cursor_end = cursor_start - timedelta(seconds=1)
            if delay > 0:
                time.sleep(delay)

    if not frames:
        raise ValueError(f"No data returned for {symbol} {timeframe}")

    merged = pd.concat(frames).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    return save_ohlcv(merged, symbol, timeframe, cache_dir, source="yahoo")


def download_all(
    *,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    cache_dir: Path | None = None,
    force: bool = False,
    delay: float = 0.5,
    on_progress: ProgressCallback | None = None,
) -> list[dict]:
    syms = symbols or FOREX_PAIRS
    tfs = timeframes or SUPPORTED_TIMEFRAMES
    results: list[dict] = []
    for symbol in syms:
        for tf in tfs:
            try:
                path = cache_path(symbol, tf, cache_dir)
                was_cached = path.is_file() and not force
                if not was_cached:
                    path = download_symbol(symbol, tf, cache_dir=cache_dir, force=force, delay=delay)
                df = load_ohlcv(symbol, tf, cache_dir)
                row = {
                    "symbol": symbol,
                    "timeframe": tf,
                    "bars": len(df),
                    "start": str(df.index.min()),
                    "end": str(df.index.max()),
                    "path": str(path),
                    "source": "yahoo",
                    "status": "skipped" if was_cached else "ok",
                }
                results.append(row)
                if on_progress:
                    on_progress(row, True)
            except Exception as exc:
                row = {
                    "symbol": symbol,
                    "timeframe": tf,
                    "status": "error",
                    "error": str(exc),
                }
                results.append(row)
                if on_progress:
                    on_progress(row, False)
            if delay > 0:
                time.sleep(delay)
    return results


def _fetch(
    symbol: str,
    interval: str,
    *,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    kwargs: dict = {"interval": interval, "progress": False, "auto_adjust": False}
    if period:
        kwargs["period"] = period
    else:
        kwargs["start"] = start
        kwargs["end"] = end
    df = yf.download(symbol, **kwargs)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df
