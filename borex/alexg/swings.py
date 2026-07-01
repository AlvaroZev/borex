from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from borex.models.candle import Candle


@dataclass(frozen=True)
class SwingPoint:
    index: int
    price: float
    kind: str  # "high" | "low"


def detect_swings(candles: list[Candle], lookback: int = 5) -> list[SwingPoint]:
    """Detecta swing highs/lows (fractales locales)."""
    if len(candles) < lookback * 2 + 1:
        return []

    swings: list[SwingPoint] = []
    for i in range(lookback, len(candles) - lookback):
        seg = candles[i - lookback : i + lookback + 1]
        highs = [c.high for c in seg]
        lows = [c.low for c in seg]
        if candles[i].high >= max(highs):
            swings.append(SwingPoint(i, candles[i].high, "high"))
        elif candles[i].low <= min(lows):
            swings.append(SwingPoint(i, candles[i].low, "low"))
    return swings
