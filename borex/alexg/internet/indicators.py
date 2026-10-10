"""Shared OHLC indicator helpers for Internet1–20 (causal, closed-bar)."""

from __future__ import annotations

from borex.models.candle import Candle


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period < 1:
        return None
    chunk = values[-period:]
    return sum(chunk) / period


def ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    s = sum(values[:period]) / period
    out[period - 1] = s
    for i in range(period, len(values)):
        s = values[i] * k + s * (1 - k)
        out[i] = s
    return out


def atr(candles: list[Candle], end_i: int, period: int = 14) -> float | None:
    if end_i < 1 or period < 1 or end_i + 1 < period:
        return None
    trs = []
    start = max(1, end_i - period + 1)
    for i in range(start, end_i + 1):
        c, p = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def rsi(closes: list[float], end_i: int, period: int = 14) -> float | None:
    if end_i < period:
        return None
    gains = losses = 0.0
    for i in range(end_i - period + 1, end_i + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g = gains / period
    avg_l = losses / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger(
    closes: list[float], end_i: int, period: int = 20, n_std: float = 2.0
) -> tuple[float, float, float] | None:
    if end_i + 1 < period:
        return None
    chunk = closes[end_i - period + 1 : end_i + 1]
    mid = sum(chunk) / period
    var = sum((x - mid) ** 2 for x in chunk) / period
    sd = var**0.5
    return mid - n_std * sd, mid, mid + n_std * sd


def donchian_high(candles: list[Candle], end_i: int, lookback: int) -> float | None:
    # prior lookback bars excluding current (breakout of preceding N)
    if end_i < lookback:
        return None
    window = candles[end_i - lookback : end_i]
    return max(c.high for c in window)


def donchian_low(candles: list[Candle], end_i: int, lookback: int) -> float | None:
    if end_i < lookback:
        return None
    window = candles[end_i - lookback : end_i]
    return min(c.low for c in window)


def stdev(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    chunk = values[-period:]
    m = sum(chunk) / period
    return (sum((x - m) ** 2 for x in chunk) / period) ** 0.5
