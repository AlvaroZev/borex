from __future__ import annotations

import pandas as pd

from borex.data.store import load_ohlcv
from borex.models.signal import Candle
from borex.strategy.base import candles_from_df

TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1wk": 10080,
}


def tf_minutes(tf: str) -> int:
    if tf not in TF_MINUTES:
        raise ValueError(f"Unknown timeframe: {tf}")
    return TF_MINUTES[tf]


def bar_open(ts: object) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def bar_close(ts: object, tf: str) -> pd.Timestamp:
    return bar_open(ts) + pd.Timedelta(minutes=tf_minutes(tf))


def build_htf_alignment(
    entry_candles: list[Candle],
    htf_candles: list[Candle],
    htf_tf: str,
) -> list[int]:
    """
    For each entry bar, index of the last fully closed HTF bar (no lookahead).
    Returns -1 if no HTF bar has closed yet.
    """
    if not htf_candles:
        return [-1] * len(entry_candles)

    htf_closes = [bar_close(c.timestamp, htf_tf) for c in htf_candles]
    align: list[int] = []
    htf_i = -1
    for ec in entry_candles:
        et = bar_open(ec.timestamp)
        while htf_i + 1 < len(htf_closes) and htf_closes[htf_i + 1] <= et:
            htf_i += 1
        align.append(htf_i)
    return align


def load_bias_dfs(
    symbol: str,
    bias_tfs: tuple[str, ...],
    *,
    end: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Load full HTF history (no start trim) so indicators/warmup have prior bars."""
    out: dict[str, pd.DataFrame] = {}
    for tf in bias_tfs:
        out[tf] = filter_df_range(load_ohlcv(symbol, tf), None, end)
    return out


def load_htf_candles(symbol: str, timeframe: str) -> list[Candle]:
    return candles_from_df(load_ohlcv(symbol, timeframe))


def filter_df_range(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    out = df
    if start is not None:
        out = out[out.index >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        out = out[out.index <= pd.Timestamp(end, tz="UTC")]
    return out
