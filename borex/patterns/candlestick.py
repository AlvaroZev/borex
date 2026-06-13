from __future__ import annotations

from borex.models.candle import Candle


def avg_body(candles: list[Candle], lookback: int = 14) -> float:
    if not candles:
        return 0.0
    sample = candles[-lookback:]
    return sum(c.body for c in sample) / len(sample)


def _body_ratio(candle: Candle) -> float:
    if candle.range == 0:
        return 0.0
    return candle.body / candle.range


def is_doji(candle: Candle, threshold: float = 0.1) -> bool:
    """Cuerpo muy pequeño respecto al rango total."""
    return candle.range > 0 and _body_ratio(candle) <= threshold


def is_hammer(candle: Candle, avg_body: float) -> bool:
    """Martillo alcista: mecha inferior larga, cuerpo pequeño arriba."""
    if candle.range == 0 or avg_body == 0:
        return False
    body = max(candle.body, avg_body * 0.3)
    return (
        candle.lower_wick >= body * 2
        and candle.upper_wick <= body * 0.5
        and candle.body <= avg_body * 1.2
    )


def is_shooting_star(candle: Candle, avg_body: float) -> bool:
    """Estrella fugaz bajista: mecha superior larga."""
    if candle.range == 0 or avg_body == 0:
        return False
    body = max(candle.body, avg_body * 0.3)
    return (
        candle.upper_wick >= body * 2
        and candle.lower_wick <= body * 0.5
        and candle.body <= avg_body * 1.2
    )


def is_bullish_engulfing(prev: Candle, curr: Candle) -> bool:
    """Vela alcista envuelve completamente la bajista anterior."""
    return (
        prev.is_bearish
        and curr.is_bullish
        and curr.open <= prev.close
        and curr.close >= prev.open
        and curr.body > prev.body
    )


def is_bearish_engulfing(prev: Candle, curr: Candle) -> bool:
    """Vela bajista envuelve completamente la alcista anterior."""
    return (
        prev.is_bullish
        and curr.is_bearish
        and curr.open >= prev.close
        and curr.close <= prev.open
        and curr.body > prev.body
    )


def is_morning_star(c1: Candle, c2: Candle, c3: Candle, avg_body: float) -> bool:
    """Tres velas: bajista, indecisión, alcista fuerte."""
    if avg_body == 0:
        return False
    gap_down = c2.high < c1.close or c2.open < c1.close
    gap_up = c3.open > c2.close
    return (
        c1.is_bearish
        and c1.body >= avg_body * 0.5
        and c2.body <= avg_body * 0.6
        and c3.is_bullish
        and c3.body >= avg_body * 0.5
        and c3.close > c1.body_mid
        and (gap_down or gap_up)
    )


def is_evening_star(c1: Candle, c2: Candle, c3: Candle, avg_body: float) -> bool:
    """Tres velas: alcista, indecisión, bajista fuerte."""
    if avg_body == 0:
        return False
    gap_up = c2.low > c1.close or c2.open > c1.close
    gap_down = c3.open < c2.close
    return (
        c1.is_bullish
        and c1.body >= avg_body * 0.5
        and c2.body <= avg_body * 0.6
        and c3.is_bearish
        and c3.body >= avg_body * 0.5
        and c3.close < c1.body_mid
        and (gap_up or gap_down)
    )


def is_three_white_soldiers(c1: Candle, c2: Candle, c3: Candle, avg_body: float) -> bool:
    """Tres velas alcistas consecutivas con cuerpos crecientes."""
    if avg_body == 0:
        return False
    candles = [c1, c2, c3]
    if not all(c.is_bullish for c in candles):
        return False
    bodies = [c.body for c in candles]
    return (
        bodies[0] >= avg_body * 0.4
        and bodies[1] >= bodies[0] * 0.8
        and bodies[2] >= bodies[1] * 0.8
        and c2.open >= c1.open
        and c3.open >= c2.open
        and c2.close > c1.close
        and c3.close > c2.close
    )


def is_three_black_crows(c1: Candle, c2: Candle, c3: Candle, avg_body: float) -> bool:
    """Tres velas bajistas consecutivas con cuerpos crecientes."""
    if avg_body == 0:
        return False
    candles = [c1, c2, c3]
    if not all(c.is_bearish for c in candles):
        return False
    bodies = [c.body for c in candles]
    return (
        bodies[0] >= avg_body * 0.4
        and bodies[1] >= bodies[0] * 0.8
        and bodies[2] >= bodies[1] * 0.8
        and c2.open <= c1.open
        and c3.open <= c2.open
        and c2.close < c1.close
        and c3.close < c2.close
    )
