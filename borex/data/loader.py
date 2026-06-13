from __future__ import annotations

from pathlib import Path

import pandas as pd

from borex.models.candle import Candle


def dataframe_to_candles(df: pd.DataFrame) -> list[Candle]:
    """Convierte un DataFrame OHLCV a lista de velas."""
    required = {"Open", "High", "Low", "Close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Columnas faltantes: {missing}")

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

    return dataframe_to_candles(df)


def load_csv(path: str | Path) -> list[Candle]:
    """Carga velas desde CSV con columnas Date, Open, High, Low, Close [, Volume]."""
    df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    df.columns = [c.strip().title() for c in df.columns]
    return dataframe_to_candles(df)
