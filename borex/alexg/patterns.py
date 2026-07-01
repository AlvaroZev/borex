from __future__ import annotations

from borex.alexg.swings import SwingPoint
from borex.alexg.trend import Trend
from borex.models.candle import Candle


def detect_double_top(
    swings: list[SwingPoint],
    trend: Trend,
    tolerance_pct: float = 0.003,
) -> bool:
    """Doble techo: dos swing highs similares en contexto bajista/neutral."""
    if trend not in (Trend.BEARISH, Trend.NEUTRAL):
        return False
    highs = [s for s in swings if s.kind == "high"]
    if len(highs) < 2:
        return False
    h1, h2 = highs[-2], highs[-1]
    tol = h1.price * tolerance_pct
    return abs(h1.price - h2.price) <= tol * 3


def detect_head_shoulders(
    swings: list[SwingPoint],
    candles: list[Candle],
    index: int,
    tolerance_pct: float = 0.004,
) -> bool:
    """
    H&S simplificado: 3 picos, central más alto, cuello roto a la baja.
    Entrada preferida post break + retest (manejado en break_retest).
    """
    highs = [s for s in swings if s.kind == "high" and s.index <= index]
    if len(highs) < 3:
        return False

    left, head, right = highs[-3], highs[-2], highs[-1]
    tol = head.price * tolerance_pct

    if not (head.price > left.price and head.price > right.price):
        return False
    if abs(left.price - right.price) > tol * 4:
        return False

    lows_between = [
        s
        for s in swings
        if s.kind == "low" and left.index < s.index < right.index
    ]
    if not lows_between:
        return False

    neckline = sum(s.price for s in lows_between) / len(lows_between)
    curr = candles[index]
    return min(curr.open, curr.close) < neckline
