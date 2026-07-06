from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.aoi2 import (
    AOIZone,
    build_bidirectional_aoi,
    next_aoi_target,
    recent_aoi_at_bar,
    stops_from_aoi_tp,
)
from borex.alexg.confirmation import (
    is_bearish_rejection,
    is_bullish_rejection,
    is_momentum_bearish,
    is_momentum_bullish,
)
from borex.alexg.risk import structure_stop_loss
from borex.alexg.swings import detect_swings
from borex.alexg.trend import Trend, detect_trend
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction
from borex.patterns.candlestick import (
    avg_body,
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_hammer,
    is_shooting_star,
    is_three_black_crows,
    is_three_white_soldiers,
)
from borex.strategy.base import Strategy


def _recent_candle_bias(candles: list[Candle], index: int, lookback: int = 8) -> Trend:
    """Short-term direction from recent closes — where price is going now."""
    start = max(0, index - lookback + 1)
    window = candles[start : index + 1]
    if len(window) < 3:
        return Trend.NEUTRAL

    bullish = sum(1 for c in window if c.is_bullish)
    net = window[-1].close - window[0].open
    if bullish >= len(window) * 0.6 and net > 0:
        return Trend.BULLISH
    if bullish <= len(window) * 0.4 and net < 0:
        return Trend.BEARISH
    return Trend.NEUTRAL


def _current_trend(
    swings: list,
    candles: list[Candle],
    index: int,
) -> Trend:
    """Structure + recent candles; no continuation-pattern prediction."""
    structure = detect_trend(swings)
    bias = _recent_candle_bias(candles, index)
    if structure == Trend.NEUTRAL:
        return bias
    if bias == Trend.NEUTRAL or bias == structure:
        return structure
    return Trend.NEUTRAL


def _bounce_signal_bullish(candles: list[Candle], index: int) -> str | None:
    if index < 1:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    if is_bullish_engulfing(prev, curr):
        return "bullish_engulfing"
    if is_bullish_rejection(curr, avg) or is_hammer(curr, avg):
        return "rejection"
    return None


def _bounce_signal_bearish(candles: list[Candle], index: int) -> str | None:
    if index < 1:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    if is_bearish_engulfing(prev, curr):
        return "bearish_engulfing"
    if is_bearish_rejection(curr, avg) or is_shooting_star(curr, avg):
        return "rejection"
    return None


def _continuation_signal_bullish(candles: list[Candle], index: int) -> str | None:
    if index < 2:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
    if is_bullish_engulfing(prev, curr):
        return "bullish_engulfing"
    if is_momentum_bullish(curr, avg):
        return "momentum"
    if is_three_white_soldiers(c1, c2, c3, avg):
        return "three_white_soldiers"
    return None


def _continuation_signal_bearish(candles: list[Candle], index: int) -> str | None:
    if index < 2:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
    if is_bearish_engulfing(prev, curr):
        return "bearish_engulfing"
    if is_momentum_bearish(curr, avg):
        return "momentum"
    if is_three_black_crows(c1, c2, c3, avg):
        return "three_black_crows"
    return None


def _entry_at_aoi(
    trend: Trend,
    aoi: AOIZone,
    candles: list[Candle],
    index: int,
) -> tuple[SignalAction, str, str] | None:
    """
    Bounce or continuation entry at AOI, aligned with current trend.
    Returns (action, setup_kind, signal_name) or None.
    """
    if trend == Trend.BULLISH:
        if aoi.kind == "support":
            sig = _bounce_signal_bullish(candles, index)
            if sig:
                return SignalAction.BUY, "bounce", sig
        elif aoi.kind == "resistance":
            sig = _continuation_signal_bullish(candles, index)
            if sig and candles[index].close > aoi.level:
                return SignalAction.BUY, "continuation", sig

    if trend == Trend.BEARISH:
        if aoi.kind == "resistance":
            sig = _bounce_signal_bearish(candles, index)
            if sig:
                return SignalAction.SELL, "bounce", sig
        elif aoi.kind == "support":
            sig = _continuation_signal_bearish(candles, index)
            if sig and candles[index].close < aoi.level:
                return SignalAction.SELL, "continuation", sig

    return None


@dataclass
class AlexG2Strategy(Strategy):
    """
    AlexG2 — top-down trend, AOI bounce/continuation, candlestick confirmation.

    1. Trade only in the direction of current market trend (structure + candles).
    2. Areas of interest from swing highs/lows; recent levels preferred.
    3. Wait for candlestick rejection or continuation at an AOI.
    4. TP at the next AOI in trade direction; SL sized for min_rr (default 2:1).
    """

    name: str = "alexg2"
    min_rr: float = 2.0
    swing_lookback: int = 5
    aoi_tolerance_pct: float = 0.002
    min_aoi_touches: int = 2
    min_bars: int = 80
    signal_cooldown: int = 8

    _last_signal_index: int = field(default=-999, repr=False)

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        if index < self.min_bars:
            return None

        window = candles[: index + 1]
        swings = detect_swings(window, self.swing_lookback)
        if len(swings) < 4:
            return None

        trend = _current_trend(swings, window, index)
        if trend == Trend.NEUTRAL:
            return None

        zones = build_bidirectional_aoi(
            swings,
            window,
            self.aoi_tolerance_pct,
            self.min_aoi_touches,
        )
        if not zones:
            return None

        aoi = recent_aoi_at_bar(
            window, index, zones, lookback=20, tolerance_pct=self.aoi_tolerance_pct
        )
        if aoi is None:
            return None

        entry_info = _entry_at_aoi(trend, aoi, window, index)
        if entry_info is None:
            return None

        action, setup_kind, signal_name = entry_info

        if mtf is not None and not mtf.all_filters_align(index, action):
            return None

        entry = candles[index].close
        tp_level = next_aoi_target(entry, zones, action)
        if tp_level is None:
            return None

        structural_sl = structure_stop_loss(window, index, action, aoi.level)
        stops = stops_from_aoi_tp(
            entry, tp_level, action, structural_sl, self.min_rr
        )
        if stops is None:
            return None
        sl, tp = stops

        if index - self._last_signal_index < self.signal_cooldown:
            return None
        self._last_signal_index = index

        pattern = f"alexg2|{trend.value}|{setup_kind}|{aoi.kind}|{signal_name}"
        return Signal(
            action=action,
            pattern=pattern,
            index=index,
            price=entry,
            timestamp=candles[index].timestamp,
            stop_loss=sl,
            take_profit=tp,
            score=self.min_rr,
        )
