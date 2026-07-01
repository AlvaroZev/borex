from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from borex.alexg.swings import SwingPoint
from borex.models.candle import Candle


class Trend(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class TrendContext:
    """Pilar 1: estructura HH/HL o LL/LH + patrón de continuación."""

    trend: Trend
    structure_ok: bool
    continuation_ok: bool
    continuation_pattern: str | None

    @property
    def is_valid(self) -> bool:
        return self.structure_ok and self.continuation_ok and self.trend != Trend.NEUTRAL


def detect_trend(swings: list[SwingPoint]) -> Trend:
    """
    Tendencia por estructura HH/HL (alcista) o LL/LH (bajista).
    Requiere al menos 2 swings de cada tipo con secuencia válida.
    """
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        ll = lows[-1].price < lows[-2].price
        lh = highs[-1].price < highs[-2].price

        if hh and hl:
            if len(highs) >= 3 and len(lows) >= 3:
                if (
                    highs[-2].price > highs[-3].price
                    and lows[-2].price > lows[-3].price
                ):
                    return Trend.BULLISH
            return Trend.BULLISH

        if ll and lh:
            if len(highs) >= 3 and len(lows) >= 3:
                if (
                    lows[-2].price < lows[-3].price
                    and highs[-2].price < highs[-3].price
                ):
                    return Trend.BEARISH
            return Trend.BEARISH

    return Trend.NEUTRAL


def detect_trend_context(
    swings: list[SwingPoint],
    candles: list[Candle],
    index: int,
) -> TrendContext:
    """
    Pilar Trend: estructura de mercado + patrón de continuación alineado.
    Ambos son obligatorios para trend_ok.
    """
    from borex.alexg.continuation import detect_continuation_pattern

    trend = detect_trend(swings)
    if trend == Trend.NEUTRAL:
        return TrendContext(
            trend=trend,
            structure_ok=False,
            continuation_ok=False,
            continuation_pattern=None,
        )

    pattern = detect_continuation_pattern(candles, index, trend, swings)
    return TrendContext(
        trend=trend,
        structure_ok=True,
        continuation_ok=pattern is not None,
        continuation_pattern=pattern,
    )


def structure_shift(swings: list[SwingPoint], trend: Trend) -> bool:
    """True si el último swing rompe la estructura previa (cambio de tendencia)."""
    if trend == Trend.BULLISH and len(swings) >= 2:
        lows = [s for s in swings if s.kind == "low"]
        if len(lows) >= 2 and lows[-1].price < lows[-2].price:
            return True
    if trend == Trend.BEARISH and len(swings) >= 2:
        highs = [s for s in swings if s.kind == "high"]
        if len(highs) >= 2 and highs[-1].price > highs[-2].price:
            return True
    return False
