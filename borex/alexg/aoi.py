from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.swings import SwingPoint
from borex.alexg.trend import Trend
from borex.models.candle import Candle


@dataclass
class AOI:
    level: float
    kind: str  # "support" | "resistance"
    touches: int
    swing_indices: list[int]


def _tolerance(price: float, pct: float) -> float:
    return price * pct


def recent_aoi_touch(
    candles: list[Candle],
    index: int,
    zones: list[AOI],
    lookback: int = 25,
    tolerance_pct: float = 0.002,
) -> AOI | None:
    """AOI tocada en las últimas N velas."""
    start = max(0, index - lookback)
    for i in range(start, index + 1):
        hit = price_at_aoi(candles[i], zones, tolerance_pct)
        if hit:
            return hit
    return None


def build_aoi_zones(
    swings: list[SwingPoint],
    candles: list[Candle],
    trend: Trend,
    tolerance_pct: float = 0.002,
    min_touches: int = 3,
) -> list[AOI]:
    """
    Construye zonas AOI desde swings y valida con mínimo 3 toques de precio.
    """
    if trend == Trend.NEUTRAL:
        return []

    candidates = (
        [s for s in swings if s.kind == "low"]
        if trend == Trend.BULLISH
        else [s for s in swings if s.kind == "high"]
    )
    if not candidates:
        return []

    kind = "support" if trend == Trend.BULLISH else "resistance"
    zones: list[AOI] = []
    seen_levels: list[float] = []

    for swing in candidates[-8:]:
        tol = _tolerance(swing.price, tolerance_pct)
        if any(abs(swing.price - lvl) <= tol * 2 for lvl in seen_levels):
            continue

        touches = 0
        touch_indices: list[int] = []
        for i, c in enumerate(candles):
            t = _tolerance(swing.price, tolerance_pct)
            if kind == "support" and c.low <= swing.price + t:
                touches += 1
                touch_indices.append(i)
            elif kind == "resistance" and c.high >= swing.price - t:
                touches += 1
                touch_indices.append(i)

        if touches >= min_touches:
            seen_levels.append(swing.price)
            zones.append(
                AOI(
                    level=swing.price,
                    kind=kind,
                    touches=touches,
                    swing_indices=touch_indices[-min_touches:],
                )
            )

    return zones


def price_at_aoi(
    candle: Candle,
    zones: list[AOI],
    tolerance_pct: float = 0.002,
) -> AOI | None:
    """True si el precio toca una zona AOI válida en esta vela."""
    for zone in zones:
        tol = _tolerance(zone.level, tolerance_pct)
        if zone.kind == "support":
            if candle.low <= zone.level + tol and candle.close >= zone.level - tol:
                return zone
        else:
            if candle.high >= zone.level - tol and candle.close <= zone.level + tol:
                return zone
    return None
