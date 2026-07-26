"""Lower-timeframe confirmation that ghost SL fills are moving toward TP."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

import pandas as pd

from borex.alexg.structure_trend import structure_trend_at
from borex.alexg.trend import Trend
from borex.data.timeframe import interval_to_timedelta
from borex.models.candle import Candle, SignalAction


def _ts(ts: object) -> pd.Timestamp:
    return pd.Timestamp(ts)


def _ts_unix(ts: object) -> float:
    return float(_ts(ts).timestamp())


@dataclass
class LtfSeries:
    """Indexed LTF candles for O(log n) windowing."""

    candles: list[Candle]
    times: list[float]  # unix seconds, ascending

    @classmethod
    def from_candles(cls, candles: list[Candle]) -> LtfSeries:
        ordered = sorted(candles, key=lambda c: _ts_unix(c.timestamp))
        return cls(candles=ordered, times=[_ts_unix(c.timestamp) for c in ordered])

    def end_index_before(self, end: pd.Timestamp) -> int:
        """Count of candles with timestamp < end."""
        return bisect_left(self.times, float(end.timestamp()))

    def start_index_at_or_after(self, start: pd.Timestamp) -> int:
        return bisect_left(self.times, float(start.timestamp()))

    def window(self, start_i: int, end_i_exclusive: int) -> list[Candle]:
        start_i = max(0, start_i)
        end_i_exclusive = min(len(self.candles), end_i_exclusive)
        if start_i >= end_i_exclusive:
            return []
        return self.candles[start_i:end_i_exclusive]


def htf_bar_window(
    htf_candle: Candle,
    execution_interval: str,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """UTC window covered by one HTF bar (open → open+duration)."""
    start = _ts(htf_candle.timestamp)
    end = start + interval_to_timedelta(execution_interval)
    return start, end


def first_sl_touch_index(
    candles: list[Candle],
    action: SignalAction,
    stop_loss: float,
) -> int | None:
    """Index of first candle in ``candles`` that tags the planned SL."""
    for i, c in enumerate(candles):
        if action == SignalAction.BUY and c.low <= stop_loss:
            return i
        if action == SignalAction.SELL and c.high >= stop_loss:
            return i
    return None


def touch_rejects_toward_tp(
    action: SignalAction,
    candle: Candle,
    stop_loss: float,
) -> bool:
    """
    SL-touch LTF bar must reclaim toward TP (no lookahead).

    BUY: tags SL with its low, but closes back above SL.
    SELL: tags SL with its high, but closes back below SL.
    """
    if action == SignalAction.BUY:
        return candle.low <= stop_loss and candle.close > stop_loss
    return candle.high >= stop_loss and candle.close < stop_loss


def momentum_toward_tp(
    action: SignalAction,
    candles: list[Candle],
    *,
    confirm_bars: int = 3,
) -> bool:
    """
    Net close move over the last ``confirm_bars`` (ending at the touch bar).

    BUY → rising closes; SELL → falling closes. Needs at least 2 bars.
    """
    if len(candles) < 2:
        return False
    window = candles[-max(2, confirm_bars) :]
    delta = window[-1].close - window[0].close
    if action == SignalAction.BUY:
        return delta > 0
    return delta < 0


def structure_toward_tp(
    action: SignalAction,
    candles: list[Candle],
    *,
    swing_lookback: int = 3,
) -> bool:
    """Alex body-structure trend on LTF matches TP direction."""
    if len(candles) < swing_lookback * 2 + 3:
        return False
    trend = structure_trend_at(candles, len(candles) - 1, lookback=swing_lookback)
    if action == SignalAction.BUY:
        return trend == Trend.BULLISH
    return trend == Trend.BEARISH


def ltf_tp_direction_ok(
    action: SignalAction,
    ltf: LtfSeries | list[Candle],
    *,
    stop_loss: float,
    htf_start: pd.Timestamp,
    htf_end: pd.Timestamp,
    structure_lookback: int = 120,
    swing_lookback: int = 3,
    confirm_bars: int = 3,
    require_structure: bool = True,
    require_momentum: bool = True,
    require_touch_reject: bool = True,
) -> bool:
    """
    Confirm LTF is traveling toward TP at the ghost SL touch.

    No lookahead past the first LTF bar that tags SL inside the HTF window:
    only bars up to and including that touch bar are used.
    """
    series = ltf if isinstance(ltf, LtfSeries) else LtfSeries.from_candles(ltf)
    end_i = series.end_index_before(htf_end)
    if end_i <= 0:
        return False

    # Bars inside the HTF window (and a little pre-window context for structure).
    win_start = series.start_index_at_or_after(htf_start)
    pre_start = max(0, win_start - structure_lookback)
    context = series.window(pre_start, end_i)
    if not context:
        return False

    # Touch must occur inside the HTF bar itself.
    inside = series.window(win_start, end_i)
    if not inside:
        return False
    touch_inside = first_sl_touch_index(inside, action, stop_loss)
    if touch_inside is None:
        return False

    touch_global = win_start + touch_inside
    # Inclusive through touch bar only — never post-touch bars.
    through_touch = series.window(pre_start, touch_global + 1)
    if not through_touch:
        return False
    touch_bar = through_touch[-1]

    # Keep structure window bounded.
    if len(through_touch) > structure_lookback:
        through_touch = through_touch[-structure_lookback:]

    checks: list[bool] = []
    if require_touch_reject:
        checks.append(touch_rejects_toward_tp(action, touch_bar, stop_loss))
    if require_structure:
        checks.append(
            structure_toward_tp(action, through_touch, swing_lookback=swing_lookback)
        )
    if require_momentum:
        checks.append(
            momentum_toward_tp(action, through_touch, confirm_bars=confirm_bars)
        )
    if not checks:
        return True
    return all(checks)


def confirm_intervals(
    action: SignalAction,
    ltf_by_tf: dict[str, LtfSeries | list[Candle]],
    intervals: tuple[str, ...],
    *,
    stop_loss: float,
    htf_candle: Candle,
    execution_interval: str,
    mode: str = "any",
    missing_policy: str = "reject",
    **kwargs,
) -> tuple[bool, list[str]]:
    """
    Run LTF confirmation across configured intervals.

    ``mode``: ``any`` (default) or ``all`` among intervals that have data.
    ``missing_policy``: ``reject`` if none of the intervals are available.
    """
    available = [tf for tf in intervals if ltf_by_tf.get(tf)]
    if not available:
        return (missing_policy == "allow"), []

    htf_start, htf_end = htf_bar_window(htf_candle, execution_interval)
    passed: list[str] = []
    failed: list[str] = []
    for tf in available:
        ok = ltf_tp_direction_ok(
            action,
            ltf_by_tf[tf],
            stop_loss=stop_loss,
            htf_start=htf_start,
            htf_end=htf_end,
            **kwargs,
        )
        (passed if ok else failed).append(tf)

    mode_l = (mode or "any").strip().lower()
    if mode_l == "all":
        return (len(failed) == 0 and len(passed) == len(available)), passed
    return (len(passed) > 0), passed
