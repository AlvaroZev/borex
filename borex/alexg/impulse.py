from __future__ import annotations

from borex.models.candle import Candle


def _activity(candle: Candle) -> float:
    """Volume when present; otherwise candle range as FX activity proxy."""
    if candle.volume > 0:
        return candle.volume
    return max(candle.range, 1e-12)


def impulse_strength(
    candles: list[Candle],
    index: int,
    *,
    lookback: int = 20,
    ema_span: int = 14,
) -> float:
    """
    Smoothed directional impulse from candle body size × activity.

    Unlike oscillators that snap toward 0 on a single reverse bar, this is an
    EMA of signed body strength — it decays gradually and measures how strong
    recent candles are in the current direction.
    """
    if index < 1 or lookback <= 0 or ema_span <= 0:
        return 0.0

    start = max(1, index - lookback + 1)
    alpha = 2.0 / (ema_span + 1.0)
    ema = 0.0
    initialized = False

    for i in range(start, index + 1):
        c = candles[i]
        avg_range = sum(candles[j].range for j in range(max(0, i - lookback + 1), i + 1))
        n = i - max(0, i - lookback + 1) + 1
        avg_range = avg_range / n if n else 1e-12
        if avg_range <= 0:
            avg_range = 1e-12

        signed = (c.close - c.open) / avg_range
        # Relative activity vs recent mean (1.0 if flat / no volume).
        acts = [_activity(candles[j]) for j in range(max(0, i - lookback + 1), i + 1)]
        mean_act = sum(acts) / len(acts) if acts else 1.0
        vol_w = _activity(c) / mean_act if mean_act > 0 else 1.0
        sample = signed * min(max(vol_w, 0.25), 3.0)

        if not initialized:
            ema = sample
            initialized = True
        else:
            ema = alpha * sample + (1.0 - alpha) * ema

    return ema
