from __future__ import annotations

import re
from datetime import timedelta

import pandas as pd


def normalize_timeframe(interval: str) -> str:
    """Map Yahoo-style intervals to Dukascopy cache timeframe codes."""
    key = interval.strip().lower()
    aliases = {
        "60m": "1h",
        "90m": "90m",
        "5d": "1d",
        "1mo": "1wk",
    }
    return aliases.get(key, key)


def slice_df_by_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """Slice OHLCV to the last N days/weeks/months/years (Yahoo-style period strings)."""
    period = period.strip().lower()
    if period in ("max", "all"):
        return df

    m = re.fullmatch(r"(\d+)(d|wk|mo|y)", period)
    if not m:
        return df

    n = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        delta = timedelta(days=n)
    elif unit == "wk":
        delta = timedelta(weeks=n)
    elif unit == "mo":
        delta = timedelta(days=n * 30)
    else:
        delta = timedelta(days=n * 365)

    end = df.index.max()
    start = end - delta
    return df[df.index >= start]
