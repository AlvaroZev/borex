from __future__ import annotations

from pathlib import Path

import pandas as pd

from borex.models.candle import Candle


def normalize_timestamps(index: pd.Index) -> pd.DatetimeIndex:
    """Unifica timestamps a UTC (evita mezcla DST en cache/Yahoo)."""
    return pd.to_datetime(index, utc=True)


def normalize_dataframe_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = normalize_timestamps(out.index)
    return out


def dataframe_to_candles(df: pd.DataFrame) -> list[Candle]:
    """Convierte un DataFrame OHLCV a lista de velas."""
    required = {"Open", "High", "Low", "Close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Columnas faltantes: {missing}")

    df = normalize_dataframe_index(df)
    candles: list[Candle] = []
    for ts, row in df.iterrows():
        candles.append(
            Candle(
                timestamp=ts,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row.get("Volume", 0)),
            )
        )
    return candles


def load_yfinance(
    symbol: str,
    period: str = "2y",
    interval: str = "1d",
) -> list[Candle]:
    """Descarga datos históricos con yfinance."""
    import yfinance as yf

    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        raise ValueError(f"No se encontraron datos para {symbol}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return dataframe_to_candles(normalize_dataframe_index(df))


def load_csv(path: str | Path) -> list[Candle]:
    """Carga velas desde CSV con columnas Date, Open, High, Low, Close [, Volume]."""
    df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    df.columns = [c.strip().title() for c in df.columns]
    return dataframe_to_candles(normalize_dataframe_index(df))


def _interval_to_pandas_rule(interval: str) -> str:
    key = interval.strip().lower()
    mapping = {
        "1m": "1min",
        "2m": "2min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "60m": "60min",
        "1h": "1h",
        "90m": "90min",
        "4h": "4h",
        "1d": "1D",
        "5d": "5D",
        "1wk": "1W",
        "1mo": "1ME",
    }
    if key not in mapping:
        raise ValueError(f"Intervalo no soportado para resample: {interval!r}")
    return mapping[key]


def resample_candles(candles: list[Candle], target_interval: str) -> list[Candle]:
    """Agrega velas a un timeframe superior (ej. 1h -> 4h)."""
    if not candles:
        return []

    rows = [
        {
            "Open": c.open,
            "High": c.high,
            "Low": c.low,
            "Close": c.close,
            "Volume": c.volume,
        }
        for c in candles
    ]
    df = pd.DataFrame(rows, index=normalize_timestamps([c.timestamp for c in candles]))
    rule = _interval_to_pandas_rule(target_interval)
    resampled = (
        df.resample(rule)
        .agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )
        .dropna(subset=["Open", "Close"])
    )
    return dataframe_to_candles(resampled)


def load_filter_candles(
    symbol: str,
    period: str,
    execution_candles: list[Candle],
    execution_interval: str,
    filter_interval: str,
    cache_mode: str = "auto",
) -> list[Candle]:
    """
    Carga velas del timeframe de filtro.
    Prioriza resample desde las velas de ejecución si el TF encaja.
    """
    from borex.data.timeframe import interval_to_minutes, validate_higher_timeframe

    validate_higher_timeframe(execution_interval, filter_interval)
    exec_m = interval_to_minutes(execution_interval)
    filter_m = interval_to_minutes(filter_interval)

    if filter_m % exec_m == 0:
        resampled = resample_candles(execution_candles, filter_interval)
        if resampled:
            return resampled

    return load_market_data(symbol, period, filter_interval, cache_mode=cache_mode)


def load_market_data(
    symbol: str,
    period: str,
    interval: str,
    cache_mode: str = "auto",
    cache_dir: Path | None = None,
) -> list[Candle]:
    """Punto único de carga: cache local o Yahoo según modo."""
    from borex.data.cache import load_yfinance_cached

    return load_yfinance_cached(
        symbol, period, interval, cache_dir=cache_dir, mode=cache_mode
    )
