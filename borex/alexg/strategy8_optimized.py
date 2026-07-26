"""AlexG8Optimized — same rules as alexg8, lazy LTF only near ghost SL fill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from borex.alexg.ltf_confirm import (
    LtfSeries,
    confirm_intervals,
    htf_bar_window,
)
from borex.alexg.strategy8 import AlexG8Strategy
from borex.data.timeframe import interval_to_timedelta
from borex.models.candle import Candle


def _df_window_to_series(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> LtfSeries:
    """Slice a cached OHLCV frame to [start, end) and index as LtfSeries."""
    if df.empty:
        return LtfSeries(candles=[], times=[])
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    else:
        start = start.tz_convert("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    else:
        end = end.tz_convert("UTC")

    # Inclusive start, exclusive end (matches end_index_before semantics).
    sl = df.loc[(df.index >= start) & (df.index < end)]
    if sl.empty:
        return LtfSeries(candles=[], times=[])

    candles: list[Candle] = []
    times: list[float] = []
    for ts, row in sl.iterrows():
        candles.append(
            Candle(
                timestamp=ts,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]) if "Volume" in row.index else 0.0,
            )
        )
        times.append(float(pd.Timestamp(ts).timestamp()))
    return LtfSeries(candles=candles, times=times)


@dataclass
class AlexG8OptimizedStrategy(AlexG8Strategy):
    """
    Same confirmation rules as alexg8, but LTF work is deferred:

    - No full-universe 1m preload at startup.
    - LTF is touched only when HTF first tags the ghost SL (fill attempt).
    - Only the HTF-bar window + a short pre-window (structure lookback) is
      materialized into candles — never the whole 1m history.
    - Parquet frames are loaded lazily per (symbol, tf) the first time that
      symbol actually needs a confirm (symbols that never tag SL stay cold).
    """

    name: str = "alexg8optimized"

    # Extra pad before structure lookback (minutes of LTF) for swing safety.
    ltf_vicinity_pad_bars: int = 12

    _ltf_period: str = field(default="max", repr=False)
    _ltf_cache_mode: str = field(default="only", repr=False)
    # (symbol, tf) -> full OHLCV DataFrame (loaded once, sliced many times)
    _ltf_df_cache: dict[tuple[str, str], pd.DataFrame] = field(
        default_factory=dict, repr=False
    )
    _ltf_load_errors: set[tuple[str, str]] = field(default_factory=set, repr=False)
    _ltf_stats: dict[str, int] = field(
        default_factory=lambda: {
            "confirms": 0,
            "df_loads": 0,
            "window_bars": 0,
            "skips_no_data": 0,
        },
        repr=False,
    )

    def configure_lazy_ltf(self, period: str, cache_mode: str) -> None:
        """Wire cache settings used when a ghost SL fill needs LTF."""
        self._ltf_period = period
        self._ltf_cache_mode = cache_mode

    def attach_ltf(self, ltf_by_symbol: dict[str, dict[str, list[Candle]]]) -> None:
        """Optional: still accept preloaded series (tests / live)."""
        super().attach_ltf(ltf_by_symbol)

    def _get_ltf_df(self, symbol: str, tf: str) -> pd.DataFrame | None:
        key = (symbol, tf)
        if key in self._ltf_df_cache:
            return self._ltf_df_cache[key]
        if key in self._ltf_load_errors:
            return None
        try:
            from borex.config import CACHE_DIR
            from borex.data.period import normalize_timeframe, slice_df_by_period
            from borex.data.store import is_cached, load_ohlcv

            timeframe = normalize_timeframe(tf)
            if self._ltf_cache_mode != "off" and is_cached(symbol, timeframe, CACHE_DIR):
                df = load_ohlcv(symbol, timeframe, CACHE_DIR)
                df = slice_df_by_period(df, self._ltf_period)
            else:
                if self._ltf_cache_mode == "only":
                    raise FileNotFoundError(
                        f"No Dukascopy cache for {symbol} {timeframe}"
                    )
                from borex.data.loader import load_market_data

                candles = load_market_data(
                    symbol, self._ltf_period, tf, cache_mode=self._ltf_cache_mode
                )
                if not candles:
                    raise ValueError("empty LTF series")
                df = pd.DataFrame(
                    {
                        "Open": [c.open for c in candles],
                        "High": [c.high for c in candles],
                        "Low": [c.low for c in candles],
                        "Close": [c.close for c in candles],
                        "Volume": [c.volume for c in candles],
                    },
                    index=pd.DatetimeIndex(
                        [pd.Timestamp(c.timestamp) for c in candles], tz="UTC"
                    ),
                )
            self._ltf_df_cache[key] = df
            self._ltf_stats["df_loads"] += 1
            return df
        except Exception:
            self._ltf_load_errors.add(key)
            return None

    def _lazy_ltf_for_confirm(
        self,
        symbol: str,
        htf_candle: Candle,
    ) -> dict[str, LtfSeries]:
        """
        Build only the LTF vicinity around the HTF SL-touch bar.

        Window = [htf_start - (structure_lookback + pad) * ltf_delta, htf_end).
        """
        htf_start, htf_end = htf_bar_window(htf_candle, self.execution_interval)
        out: dict[str, LtfSeries] = {}
        for tf in self.ltf_intervals:
            # Prefer already-attached series (tests / live refresh) if present.
            attached = self._ltf_by_symbol.get(symbol, {}).get(tf)
            if attached is not None:
                # Slice attached series to vicinity instead of scanning all bars.
                try:
                    ltf_delta = interval_to_timedelta(tf)
                except ValueError:
                    ltf_delta = pd.Timedelta(minutes=1)
                pre = (
                    self.ltf_structure_lookback + self.ltf_vicinity_pad_bars
                ) * ltf_delta
                win_start = htf_start - pre
                start_i = attached.start_index_at_or_after(win_start)
                end_i = attached.end_index_before(htf_end)
                chunk = attached.window(start_i, end_i)
                out[tf] = (
                    LtfSeries.from_candles(chunk)
                    if chunk
                    else LtfSeries(candles=[], times=[])
                )
                self._ltf_stats["window_bars"] += len(out[tf].candles)
                continue

            df = self._get_ltf_df(symbol, tf)
            if df is None or df.empty:
                self._ltf_stats["skips_no_data"] += 1
                continue
            try:
                ltf_delta = interval_to_timedelta(tf)
            except ValueError:
                ltf_delta = pd.Timedelta(minutes=1)
            pre = (self.ltf_structure_lookback + self.ltf_vicinity_pad_bars) * ltf_delta
            win_start = htf_start - pre
            series = _df_window_to_series(df, win_start, htf_end)
            if series.candles:
                out[tf] = series
                self._ltf_stats["window_bars"] += len(series.candles)
            else:
                self._ltf_stats["skips_no_data"] += 1
        return out

    def _confirm_ghost_fill(self, pending: Any, index: int, candles: list[Candle]) -> bool:
        """
        Only runs after HTF tags the ghost SL (caller already gated that).
        Skips all LTF work from ghost queue time until that first SL touch.
        """
        self._ltf_stats["confirms"] += 1
        symbol = self._current_symbol or "UNKNOWN"
        htf_candle = candles[index]
        by_tf = self._lazy_ltf_for_confirm(symbol, htf_candle)
        # Remap confirm window: series already starts near htf_start - lookback,
        # so pass the same htf window; ltf_tp_direction_ok still finds first touch.
        ok, _passed = confirm_intervals(
            pending.action,
            by_tf,
            self.ltf_intervals,
            stop_loss=pending.stop_loss,
            htf_candle=htf_candle,
            execution_interval=self.execution_interval,
            mode=self.ltf_confirm_mode,
            missing_policy=self.ltf_missing_policy,
            structure_lookback=self.ltf_structure_lookback,
            swing_lookback=self.ltf_swing_lookback,
            confirm_bars=self.ltf_confirm_bars,
            require_structure=self.ltf_require_structure,
            require_momentum=self.ltf_require_momentum,
            require_touch_reject=self.ltf_require_touch_reject,
        )
        return ok

    def ltf_stats_summary(self) -> str:
        s = self._ltf_stats
        return (
            f"alexg8optimized LTF: confirms={s['confirms']} "
            f"df_loads={s['df_loads']} window_bars={s['window_bars']} "
            f"skips_no_data={s['skips_no_data']}"
        )
