from __future__ import annotations

from borex.alexg.swings import SwingPoint
from borex.alexg.trend import Trend
from borex.models.candle import Candle
from borex.patterns.candlestick import (
    avg_body,
    is_three_black_crows,
    is_three_white_soldiers,
)


def _rising_swing_lows(swings: list[SwingPoint], count: int = 3) -> bool:
    lows = [s for s in swings if s.kind == "low"]
    if len(lows) < count:
        return False
    tail = lows[-count:]
    return all(tail[i].price > tail[i - 1].price for i in range(1, count))


def _falling_swing_highs(swings: list[SwingPoint], count: int = 3) -> bool:
    highs = [s for s in swings if s.kind == "high"]
    if len(highs) < count:
        return False
    tail = highs[-count:]
    return all(tail[i].price < tail[i - 1].price for i in range(1, count))


def _is_bullish_flag(
    candles: list[Candle],
    index: int,
    pole_bars: int = 15,
    flag_bars: int = 10,
    pole_min_pct: float = 0.006,
) -> bool:
    """Impulso alcista + consolidación estrecha (bandera)."""
    start = index - pole_bars - flag_bars
    if start < 0:
        return False

    pole = candles[start : start + pole_bars]
    flag = candles[start + pole_bars : index + 1]
    if len(pole) < pole_bars or len(flag) < 3:
        return False

    pole_low = min(c.low for c in pole)
    pole_high = max(c.high for c in pole)
    if pole_low <= 0:
        return False
    if (pole_high - pole_low) / pole_low < pole_min_pct:
        return False

    pole_range = pole_high - pole_low
    flag_range = max(c.high for c in flag) - min(c.low for c in flag)
    if flag_range > pole_range * 0.55:
        return False

    # Bandera alcista: retroceso leve, sin romper el inicio del impulso
    if min(c.low for c in flag) < pole_low * 1.002:
        return False

    curr = candles[index]
    return curr.is_bullish and curr.close >= flag[-2].close


def _is_bearish_flag(
    candles: list[Candle],
    index: int,
    pole_bars: int = 15,
    flag_bars: int = 10,
    pole_min_pct: float = 0.006,
) -> bool:
    """Impulso bajista + consolidación estrecha (bandera)."""
    start = index - pole_bars - flag_bars
    if start < 0:
        return False

    pole = candles[start : start + pole_bars]
    flag = candles[start + pole_bars : index + 1]
    if len(pole) < pole_bars or len(flag) < 3:
        return False

    pole_high = max(c.high for c in pole)
    pole_low = min(c.low for c in pole)
    if pole_high <= 0:
        return False
    if (pole_high - pole_low) / pole_high < pole_min_pct:
        return False

    pole_range = pole_high - pole_low
    flag_range = max(c.high for c in flag) - min(c.low for c in flag)
    if flag_range > pole_range * 0.55:
        return False

    if max(c.high for c in flag) > pole_high * 0.998:
        return False

    curr = candles[index]
    return curr.is_bearish and curr.close <= flag[-2].close


def _is_bullish_pennant(
    candles: list[Candle],
    index: int,
    lookback: int = 20,
) -> bool:
    """Rango convergente tras impulso alcista."""
    if index < lookback:
        return False

    window = candles[index - lookback + 1 : index + 1]
    mid = len(window) // 2
    first, second = window[:mid], window[mid:]
    if len(first) < 4 or len(second) < 4:
        return False

    first_range = max(c.high for c in first) - min(c.low for c in first)
    second_range = max(c.high for c in second) - min(c.low for c in second)
    if first_range <= 0:
        return False
    if second_range >= first_range * 0.7:
        return False

    impulse_up = window[-1].close > window[0].open
    highs_falling = second[-1].high <= second[0].high
    lows_rising = second[-1].low >= second[0].low
    curr = candles[index]
    return impulse_up and highs_falling and lows_rising and curr.is_bullish


def _is_bearish_pennant(
    candles: list[Candle],
    index: int,
    lookback: int = 20,
) -> bool:
    if index < lookback:
        return False

    window = candles[index - lookback + 1 : index + 1]
    mid = len(window) // 2
    first, second = window[:mid], window[mid:]
    if len(first) < 4 or len(second) < 4:
        return False

    first_range = max(c.high for c in first) - min(c.low for c in first)
    second_range = max(c.high for c in second) - min(c.low for c in second)
    if first_range <= 0:
        return False
    if second_range >= first_range * 0.7:
        return False

    impulse_down = window[-1].close < window[0].open
    highs_falling = second[-1].high <= second[0].high
    lows_rising = second[-1].low >= second[0].low
    curr = candles[index]
    return impulse_down and highs_falling and lows_rising and curr.is_bearish


def detect_continuation_pattern(
    candles: list[Candle],
    index: int,
    trend: Trend,
    swings: list[SwingPoint],
) -> str | None:
    """
    Patrones de continuación para confirmar el pilar Trend.
    Retorna el nombre del patrón o None.
    """
    if trend == Trend.NEUTRAL or index < 2:
        return None

    avg = avg_body(candles[: index + 1])
    c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]

    if trend == Trend.BULLISH:
        if is_three_white_soldiers(c1, c2, c3, avg):
            return "three_white_soldiers"
        if _is_bullish_flag(candles, index):
            return "bullish_flag"
        if _is_bullish_pennant(candles, index):
            return "bullish_pennant"
        if _rising_swing_lows(swings):
            return "rising_swings"
        return None

    if is_three_black_crows(c1, c2, c3, avg):
        return "three_black_crows"
    if _is_bearish_flag(candles, index):
        return "bearish_flag"
    if _is_bearish_pennant(candles, index):
        return "bearish_pennant"
    if _falling_swing_highs(swings):
        return "falling_swings"
    return None
