from __future__ import annotations

from borex.models.candle import Candle
from borex.patterns.candlestick import (
    avg_body,
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_hammer,
    is_shooting_star,
)


def is_bullish_rejection(candle: Candle, avg: float) -> bool:
    """Mecha inferior larga rechazando soporte."""
    if candle.range == 0:
        return False
    return is_hammer(candle, avg) or (
        candle.lower_wick >= candle.body * 1.5
        and candle.is_bullish
        and candle.lower_wick / candle.range >= 0.5
    )


def is_bearish_rejection(candle: Candle, avg: float) -> bool:
    """Mecha superior larga rechazando resistencia."""
    if candle.range == 0:
        return False
    return is_shooting_star(candle, avg) or (
        candle.upper_wick >= candle.body * 1.5
        and candle.is_bearish
        and candle.upper_wick / candle.range >= 0.5
    )


def is_momentum_bullish(candle: Candle, avg: float) -> bool:
    return candle.is_bullish and candle.body >= avg * 1.2


def is_momentum_bearish(candle: Candle, avg: float) -> bool:
    return candle.is_bearish and candle.body >= avg * 1.2


def bullish_confirmation(candles: list[Candle], index: int) -> str | None:
    if index < 1:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])

    if is_bullish_engulfing(prev, curr):
        return "bullish_engulfing"
    if is_bullish_rejection(curr, avg):
        return "wick_rejection"
    if is_momentum_bullish(curr, avg):
        return "momentum"
    if is_hammer(curr, avg):
        return "hammer"
    return None


def bearish_confirmation(candles: list[Candle], index: int) -> str | None:
    if index < 1:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])

    if is_bearish_engulfing(prev, curr):
        return "bearish_engulfing"
    if is_bearish_rejection(curr, avg):
        return "wick_rejection"
    if is_momentum_bearish(curr, avg):
        return "momentum"
    if is_shooting_star(curr, avg):
        return "shooting_star"
    return None
