from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from borex.data.loader import dataframe_to_candles, load_csv, normalize_dataframe_index
from borex.models.candle import Candle

DEFAULT_CACHE_DIR = Path("data/cache")


def _safe_filename_part(value: str) -> str:
    return re.sub(r"[^\w.]+", "_", value.strip()).strip("_")


def cache_path(
    symbol: str,
    period: str,
    interval: str,
    cache_dir: Path | None = None,
) -> Path:
    root = cache_dir or DEFAULT_CACHE_DIR
    name = f"{_safe_filename_part(symbol)}_{_safe_filename_part(period)}_{_safe_filename_part(interval)}.csv"
    return root / name


def cache_exists(
    symbol: str,
    period: str,
    interval: str,
    cache_dir: Path | None = None,
) -> bool:
    return cache_path(symbol, period, interval, cache_dir).is_file()


def save_cache_df(
    df: pd.DataFrame,
    symbol: str,
    period: str,
    interval: str,
    cache_dir: Path | None = None,
) -> Path:
    path = cache_path(symbol, period, interval, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = normalize_dataframe_index(df)
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    out.index.name = "Date"
    out.to_csv(path)
    return path


def load_cache(
    symbol: str,
    period: str,
    interval: str,
    cache_dir: Path | None = None,
) -> list[Candle] | None:
    path = cache_path(symbol, period, interval, cache_dir)
    if not path.is_file():
        return None
    return load_csv(path)


def download_to_cache(
    symbol: str,
    period: str,
    interval: str,
    cache_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Descarga de Yahoo y guarda CSV local. Reutilizable en backtests."""
    import yfinance as yf

    path = cache_path(symbol, period, interval, cache_dir)
    if path.is_file() and not force:
        return path

    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        raise ValueError(f"No se encontraron datos para {symbol} ({period}, {interval})")

    return save_cache_df(df, symbol, period, interval, cache_dir)


def load_yfinance_cached(
    symbol: str,
    period: str = "2y",
    interval: str = "1d",
    cache_dir: Path | None = None,
    mode: str = "auto",
) -> list[Candle]:
    """
    mode:
      auto — cache si existe, si no Yahoo (sin guardar)
      only — solo cache (falla si no existe)
      off  — solo Yahoo
      write — Yahoo y guarda en cache
    """
    if mode in ("auto", "only"):
        cached = load_cache(symbol, period, interval, cache_dir)
        if cached:
            return cached
        if mode == "only":
            path = cache_path(symbol, period, interval, cache_dir)
            raise FileNotFoundError(
                f"Cache no encontrado: {path}. "
                f"Ejecuta: python download_cache.py --symbol {symbol!r} -p {period} -i {interval}"
            )

    import yfinance as yf

    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        raise ValueError(f"No se encontraron datos para {symbol}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = normalize_dataframe_index(df)

    if mode == "write":
        save_cache_df(df, symbol, period, interval, cache_dir)

    return dataframe_to_candles(df)
