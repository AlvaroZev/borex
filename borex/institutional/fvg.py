from __future__ import annotations

from dataclasses import dataclass

from borex.models.candle import Candle


@dataclass(frozen=True)
class FairValueGap:
    """Imbalance de 3 velas: gap entre high[i-2] y low[i] (bullish) o viceversa."""
    index: int
    top: float
    bottom: float
    direction: str  # "bullish" | "bearish"


def detect_fvgs(candles: list[Candle], start: int, end: int) -> list[FairValueGap]:
    """Detecta FVGs en el rango [start, end]."""
    gaps: list[FairValueGap] = []
    for i in range(max(start + 2, 2), end + 1):
        c0 = candles[i - 2]
        c2 = candles[i]
        if c2.low > c0.high:
            gaps.append(FairValueGap(i, c2.low, c0.high, "bullish"))
        elif c2.high < c0.low:
            gaps.append(FairValueGap(i, c0.low, c2.high, "bearish"))
    return gaps


def recent_fvg_fill(
    candles: list[Candle],
    index: int,
    direction: str,
    lookback: int = 20,
) -> FairValueGap | None:
    """Precio actual toca/rellena un FVG reciente en la dirección del trade."""
    start = max(0, index - lookback)
    gaps = detect_fvgs(candles, start, index)
    candle = candles[index]

    for gap in reversed(gaps):
        if gap.direction != direction:
            continue
        if gap.bottom <= candle.low <= gap.top or gap.bottom <= candle.close <= gap.top:
            return gap
        if direction == "bullish" and candle.low <= gap.top and candle.close >= gap.bottom:
            return gap
        if direction == "bearish" and candle.high >= gap.bottom and candle.close <= gap.top:
            return gap
    return None
