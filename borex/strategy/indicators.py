from __future__ import annotations

import numpy as np

from borex.models.signal import Candle


def pd_day(ts: object) -> str:
    return str(ts)[:10]


def ema(values: np.ndarray, period: int) -> float:
    if len(values) < period:
        return float(values[-1]) if len(values) else 0.0
    alpha = 2.0 / (period + 1)
    e = float(values[0])
    for v in values[1:]:
        e = alpha * float(v) + (1 - alpha) * e
    return e


def ema_series(values: np.ndarray, period: int) -> np.ndarray:
    out = np.empty(len(values))
    if len(values) == 0:
        return out
    alpha = 2.0 / (period + 1)
    e = float(values[0])
    out[0] = e
    for i in range(1, len(values)):
        e = alpha * float(values[i]) + (1 - alpha) * e
        out[i] = e
    return out


def rsi(closes: np.ndarray, period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1) :])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def session_vwap(candles: list[Candle], index: int) -> float:
    """VWAP reset at UTC midnight (forex daily session proxy)."""
    day = pd_day(candles[index].timestamp)
    num = 0.0
    den = 0.0
    for i in range(index + 1):
        if pd_day(candles[i].timestamp) != day:
            continue
        tp = (candles[i].high + candles[i].low + candles[i].close) / 3.0
        vol = max(candles[i].volume, 1.0)
        num += tp * vol
        den += vol
    return num / den if den > 0 else candles[index].close


def is_bullish_engulfing(prev: Candle, cur: Candle) -> bool:
    return cur.close > cur.open and prev.close < prev.open and cur.close > prev.open and cur.open < prev.close


def is_bearish_engulfing(prev: Candle, cur: Candle) -> bool:
    return cur.close < cur.open and prev.close > prev.open and cur.close < prev.open and cur.open > prev.close


def consecutive_green(candles: list[Candle], end: int, n: int) -> bool:
    if end < n - 1:
        return False
    for i in range(end - n + 1, end + 1):
        if candles[i].close <= candles[i].open:
            return False
    return True


def wick_ratio(c: Candle) -> tuple[float, float]:
    rng = c.high - c.low
    if rng <= 0:
        return 0.0, 0.0
    lower = min(c.open, c.close) - c.low
    upper = c.high - max(c.open, c.close)
    return lower / rng, upper / rng


def sl_tp(price: float, side: str, sl_pct: float, tp_pct: float) -> tuple[float, float]:
    if side == "long":
        return price * (1 - sl_pct), price * (1 + tp_pct)
    return price * (1 + sl_pct), price * (1 - tp_pct)


def true_range(cur: Candle, prev: Candle | None) -> float:
    if prev is None:
        return cur.high - cur.low
    return max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))


def atr_at(candles: list[Candle], index: int, period: int = 14) -> float:
    if index < 0 or not candles:
        return 0.0
    start = max(1, index - period + 1)
    trs: list[float] = []
    for i in range(start, index + 1):
        trs.append(true_range(candles[i], candles[i - 1]))
    return sum(trs) / len(trs) if trs else 0.0
