from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.swings import SwingPoint
from borex.models.candle import Candle


@dataclass(frozen=True)
class LiquiditySweep:
    """Stop hunt: mecha rompe swing level y cierre vuelve dentro del rango."""
    index: int
    swept_level: float
    direction: str  # "bullish" (sweep lows) | "bearish" (sweep highs)


def detect_liquidity_sweep(
    candles: list[Candle],
    index: int,
    swings: list[SwingPoint],
    lookback: int = 30,
    tolerance_pct: float = 0.0005,
) -> LiquiditySweep | None:
    """
    Detecta barrido de liquidez en la vela actual.
    Bullish: low < swing low reciente, close > swept level.
    Bearish: high > swing high reciente, close < swept level.
    """
    if index < 1:
        return None

    candle = candles[index]
    min_idx = max(0, index - lookback)

    recent_lows = [s for s in swings if s.kind == "low" and min_idx <= s.index < index]
    recent_highs = [s for s in swings if s.kind == "high" and min_idx <= s.index < index]

    if recent_lows:
        level = min(s.price for s in recent_lows)
        buffer = level * tolerance_pct
        if candle.low < level - buffer and candle.close > level:
            return LiquiditySweep(index, level, "bullish")

    if recent_highs:
        level = max(s.price for s in recent_highs)
        buffer = level * tolerance_pct
        if candle.high > level + buffer and candle.close < level:
            return LiquiditySweep(index, level, "bearish")

    return None
