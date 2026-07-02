from __future__ import annotations

from borex.models.candle import Candle


def activity_proxy(candle: Candle) -> float:
    """Proxy de actividad cuando volume=0 (común en FX de Yahoo)."""
    if candle.volume > 0:
        return candle.volume
    return candle.range if candle.range > 0 else candle.body


def relative_activity(
    candles: list[Candle],
    index: int,
    period: int = 20,
    threshold: float = 1.3,
) -> bool:
    """Actividad actual >= threshold × media reciente."""
    start = max(0, index - period + 1)
    window = candles[start : index + 1]
    if len(window) < 3:
        return False
    current = activity_proxy(window[-1])
    avg = sum(activity_proxy(c) for c in window[:-1]) / (len(window) - 1)
    if avg <= 0:
        return current > 0
    return current >= avg * threshold
