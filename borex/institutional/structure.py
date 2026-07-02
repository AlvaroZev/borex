from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from borex.alexg.swings import SwingPoint
from borex.models.candle import Candle


class StructureBias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class StructureEvent:
    index: int
    kind: str  # "bos" | "choch"
    bias: StructureBias
    level: float


def _recent_swings(
    swings: list[SwingPoint], index: int, kind: str, count: int = 3
) -> list[SwingPoint]:
    filtered = [s for s in swings if s.kind == kind and s.index <= index]
    return filtered[-count:]


def detect_structure_bias(
    swings: list[SwingPoint],
    index: int,
) -> StructureBias:
    """HH/HL → bullish, LL/LH → bearish."""
    highs = _recent_swings(swings, index, "high", 2)
    lows = _recent_swings(swings, index, "low", 2)
    if len(highs) < 2 or len(lows) < 2:
        return StructureBias.NEUTRAL

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    ll = lows[-1].price < lows[-2].price
    lh = highs[-1].price < highs[-2].price

    if hh and hl:
        return StructureBias.BULLISH
    if ll and lh:
        return StructureBias.BEARISH
    return StructureBias.NEUTRAL


def detect_break_of_structure(
    candles: list[Candle],
    index: int,
    swings: list[SwingPoint],
    bias: StructureBias,
) -> StructureEvent | None:
    """BOS: cierre rompe último swing high/low en dirección de la estructura."""
    candle = candles[index]
    if bias == StructureBias.BULLISH:
        highs = _recent_swings(swings, index - 1, "high", 1)
        if not highs:
            return None
        level = highs[-1].price
        if candle.close > level:
            return StructureEvent(index, "bos", StructureBias.BULLISH, level)
    elif bias == StructureBias.BEARISH:
        lows = _recent_swings(swings, index - 1, "low", 1)
        if not lows:
            return None
        level = lows[-1].price
        if candle.close < level:
            return StructureEvent(index, "bos", StructureBias.BEARISH, level)
    return None
