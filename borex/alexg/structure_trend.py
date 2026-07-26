"""Alex Set-and-Forget structure trend (body-based HH/HL / LH/LL legs)."""

from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.swings import SwingPoint
from borex.alexg.trend import Trend
from borex.models.candle import Candle


def body_high(candle: Candle) -> float:
    return max(candle.open, candle.close)


def body_low(candle: Candle) -> float:
    return min(candle.open, candle.close)


def detect_body_swings(candles: list[Candle], lookback: int = 5) -> list[SwingPoint]:
    """Fractal swings on candle bodies (wicks ignored), matching Alex's focus."""
    if len(candles) < lookback * 2 + 1:
        return []

    swings: list[SwingPoint] = []
    for i in range(lookback, len(candles) - lookback):
        highs = [body_high(c) for c in candles[i - lookback : i + lookback + 1]]
        lows = [body_low(c) for c in candles[i - lookback : i + lookback + 1]]
        bh = body_high(candles[i])
        bl = body_low(candles[i])
        if bh >= max(highs):
            swings.append(SwingPoint(i, bh, "high"))
        elif bl <= min(lows):
            swings.append(SwingPoint(i, bl, "low"))
    return swings


@dataclass
class StructureLeg:
    """Active structure range that defines the current trend."""

    trend: Trend
    anchor_low: float
    anchor_high: float
    low_index: int
    high_index: int


@dataclass
class StructureState:
    trend: Trend = Trend.NEUTRAL
    leg: StructureLeg | None = None


def update_structure_trend(
    state: StructureState,
    swings: list[SwingPoint],
) -> StructureState:
    """
    Update Alex-style structure from ordered swing points.

    Uptrend: defined by higher-low / higher-high. Break HH → continue with new
    HL = most recent swing low. Break HL → flip to downtrend.
    Downtrend mirrors that with LH / LL.
    """
    if len(swings) < 2:
        return state

    # Bootstrap: first opposite swing pair forms the initial leg.
    # Fractals often start with same-kind duplicates (high,high / low,low); only
    # using swings[0], swings[1] left weekly/4h stuck at NEUTRAL forever, which
    # zeroed video1 (htf_bias=pair requires weekly or 4h to agree with daily).
    if state.leg is None:
        for i in range(len(swings) - 1):
            first, second = swings[i], swings[i + 1]
            if first.kind == "low" and second.kind == "high" and second.price > first.price:
                state.trend = Trend.BULLISH
                state.leg = StructureLeg(
                    Trend.BULLISH, first.price, second.price, first.index, second.index
                )
                break
            if first.kind == "high" and second.kind == "low" and second.price < first.price:
                state.trend = Trend.BEARISH
                state.leg = StructureLeg(
                    Trend.BEARISH, second.price, first.price, second.index, first.index
                )
                break
        else:
            return state

    if state.leg is None:
        return state

    leg = state.leg
    for swing in swings:
        if swing.index <= max(leg.low_index, leg.high_index):
            continue

        if state.trend == Trend.BULLISH:
            if swing.kind == "high" and swing.price > leg.anchor_high:
                # Continue uptrend: new HH, most recent low becomes HL.
                recent_lows = [
                    s
                    for s in swings
                    if s.kind == "low" and leg.high_index < s.index <= swing.index
                ]
                new_low = recent_lows[-1] if recent_lows else SwingPoint(
                    leg.low_index, leg.anchor_low, "low"
                )
                leg = StructureLeg(
                    Trend.BULLISH,
                    new_low.price,
                    swing.price,
                    new_low.index,
                    swing.index,
                )
                state.leg = leg
            elif swing.kind == "low" and swing.price < leg.anchor_low:
                # Break HL → flip to downtrend.
                recent_highs = [
                    s
                    for s in swings
                    if s.kind == "high" and leg.low_index < s.index <= swing.index
                ]
                new_high = recent_highs[-1] if recent_highs else SwingPoint(
                    leg.high_index, leg.anchor_high, "high"
                )
                state.trend = Trend.BEARISH
                leg = StructureLeg(
                    Trend.BEARISH,
                    swing.price,
                    new_high.price,
                    swing.index,
                    new_high.index,
                )
                state.leg = leg
        elif state.trend == Trend.BEARISH:
            if swing.kind == "low" and swing.price < leg.anchor_low:
                recent_highs = [
                    s
                    for s in swings
                    if s.kind == "high" and leg.low_index < s.index <= swing.index
                ]
                new_high = recent_highs[-1] if recent_highs else SwingPoint(
                    leg.high_index, leg.anchor_high, "high"
                )
                leg = StructureLeg(
                    Trend.BEARISH,
                    swing.price,
                    new_high.price,
                    swing.index,
                    new_high.index,
                )
                state.leg = leg
            elif swing.kind == "high" and swing.price > leg.anchor_high:
                recent_lows = [
                    s
                    for s in swings
                    if s.kind == "low" and leg.high_index < s.index <= swing.index
                ]
                new_low = recent_lows[-1] if recent_lows else SwingPoint(
                    leg.low_index, leg.anchor_low, "low"
                )
                state.trend = Trend.BULLISH
                leg = StructureLeg(
                    Trend.BULLISH,
                    new_low.price,
                    swing.price,
                    new_low.index,
                    swing.index,
                )
                state.leg = leg

    return state


def structure_trend_at(
    candles: list[Candle],
    index: int,
    lookback: int = 5,
) -> Trend:
    """Trend at ``index`` using body swings and Alex structure rules."""
    window = candles[: index + 1]
    swings = detect_body_swings(window, lookback)
    state = StructureState()
    update_structure_trend(state, swings)
    return state.trend
