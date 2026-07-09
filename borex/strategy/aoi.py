"""Areas of interest (S/R), swing structure, trend bias, and candle entry patterns."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from borex.models.signal import Candle
from borex.strategy.indicators import (
    atr_at,
    is_bearish_engulfing,
    is_bullish_engulfing,
    wick_ratio,
)

Trend = str  # "up" | "down" | "neutral"


@dataclass(frozen=True)
class SwingPoint:
    index: int
    price: float
    kind: str  # "high" | "low"


@dataclass(frozen=True)
class AoiLevel:
    price: float
    kind: str  # "resistance" | "support"
    last_touch_index: int
    touches: int


def is_swing_high(candles: list[Candle], index: int, left: int, right: int) -> bool:
    if index < left or index >= len(candles) - right:
        return False
    h = candles[index].high
    for j in range(index - left, index):
        if candles[j].high >= h:
            return False
    for j in range(index + 1, index + right + 1):
        if candles[j].high >= h:
            return False
    return True


def is_swing_low(candles: list[Candle], index: int, left: int, right: int) -> bool:
    if index < left or index >= len(candles) - right:
        return False
    lo = candles[index].low
    for j in range(index - left, index):
        if candles[j].low <= lo:
            return False
    for j in range(index + 1, index + right + 1):
        if candles[j].low <= lo:
            return False
    return True


def find_swings(
    candles: list[Candle],
    end_index: int,
    *,
    left: int,
    right: int,
    lookback: int,
) -> list[SwingPoint]:
    start = max(left, end_index - lookback + 1)
    stop = min(end_index - right, end_index)
    out: list[SwingPoint] = []
    for i in range(start, stop + 1):
        if is_swing_high(candles, i, left, right):
            out.append(SwingPoint(i, candles[i].high, "high"))
        if is_swing_low(candles, i, left, right):
            out.append(SwingPoint(i, candles[i].low, "low"))
    return out


def _recency_weight(index: int, end_index: int, half_life: float) -> float:
    age = max(0, end_index - index)
    return float(np.exp(-age / max(half_life, 1.0)))


def build_aoi_levels(
    candles: list[Candle],
    end_index: int,
    *,
    left: int,
    right: int,
    lookback: int,
    merge_dist: float,
    half_life: float = 80.0,
) -> list[AoiLevel]:
    """Cluster swing highs/lows into S/R zones; recent swings weigh more."""
    swings = find_swings(candles, end_index, left=left, right=right, lookback=lookback)
    if not swings:
        return []

    swings.sort(key=lambda s: (s.price, -s.index))
    clusters: list[list[tuple[float, float, str, int]]] = []

    for sp in swings:
        w = _recency_weight(sp.index, end_index, half_life)
        placed = False
        for cluster in clusters:
            ref = cluster[0][0]
            if abs(sp.price - ref) <= merge_dist:
                cluster.append((sp.price, w, sp.kind, sp.index))
                placed = True
                break
        if not placed:
            clusters.append([(sp.price, w, sp.kind, sp.index)])

    levels: list[AoiLevel] = []
    for cluster in clusters:
        total_w = sum(c[1] for c in cluster)
        if total_w <= 0:
            continue
        price = sum(c[0] * c[1] for c in cluster) / total_w
        highs = sum(1 for c in cluster if c[2] == "high")
        lows = sum(1 for c in cluster if c[2] == "low")
        kind = "resistance" if highs >= lows else "support"
        last_idx = max(c[3] for c in cluster)
        levels.append(AoiLevel(price=price, kind=kind, last_touch_index=last_idx, touches=len(cluster)))

    levels.sort(key=lambda lv: lv.price)
    return levels


def trend_from_structure(
    candles: list[Candle],
    end_index: int,
    *,
    left: int,
    right: int,
    lookback: int,
) -> Trend:
    """HH+HL = up, LH+LL = down; no prediction — current swing structure only."""
    swings = find_swings(candles, end_index, left=left, right=right, lookback=lookback)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return _trend_from_candles(candles, end_index)

    h1, h2 = highs[-2].price, highs[-1].price
    l1, l2 = lows[-2].price, lows[-1].price
    if h2 > h1 and l2 > l1:
        return "up"
    if h2 < h1 and l2 < l1:
        return "down"
    return "neutral"


def _trend_from_candles(candles: list[Candle], end_index: int, n: int = 6) -> Trend:
    """Fallback: recent closed-bar direction (no forecasting)."""
    if end_index < n:
        return "neutral"
    window = candles[end_index - n + 1 : end_index + 1]
    ups = sum(1 for c in window if c.close > c.open)
    downs = sum(1 for c in window if c.close < c.open)
    if ups >= n - 1 and window[-1].close > window[0].close:
        return "up"
    if downs >= n - 1 and window[-1].close < window[0].close:
        return "down"
    return "neutral"


def price_at_level(c: Candle, level: float, tolerance: float) -> bool:
    return c.low - tolerance <= level <= c.high + tolerance


def nearest_level(
    price: float,
    levels: list[AoiLevel],
    *,
    tolerance: float,
    kind: str | None = None,
) -> AoiLevel | None:
    best: AoiLevel | None = None
    best_dist = float("inf")
    for lv in levels:
        if kind and lv.kind != kind:
            continue
        dist = abs(price - lv.price)
        if dist <= tolerance and dist < best_dist:
            best = lv
            best_dist = dist
    return best


def next_level_in_direction(
    entry: float,
    levels: list[AoiLevel],
    direction: str,
    *,
    min_distance: float,
) -> float | None:
    if direction == "long":
        candidates = [lv.price for lv in levels if lv.price > entry + min_distance]
        return min(candidates) if candidates else None
    candidates = [lv.price for lv in levels if lv.price < entry - min_distance]
    return max(candidates) if candidates else None


def stops_from_reward_risk(
    entry: float,
    tp: float,
    direction: str,
    min_rr: float,
) -> tuple[float, float] | None:
    """TP fixed at next AOI; SL sized so reward/risk >= min_rr."""
    if min_rr <= 0:
        return None
    if direction == "long":
        reward = tp - entry
        if reward <= 0:
            return None
        risk = reward / min_rr
        sl = entry - risk
        if sl <= 0 or sl >= entry:
            return None
        return sl, tp
    reward = entry - tp
    if reward <= 0:
        return None
    risk = reward / min_rr
    sl = entry + risk
    if sl <= entry:
        return None
    return sl, tp


def is_bullish_rejection(c: Candle, min_wick: float) -> bool:
    lower, upper = wick_ratio(c)
    body_top = max(c.open, c.close)
    mid = (c.high + c.low) / 2
    return lower >= min_wick and body_top >= mid and c.close >= c.open * 0.999


def is_bearish_rejection(c: Candle, min_wick: float) -> bool:
    lower, upper = wick_ratio(c)
    body_bot = min(c.open, c.close)
    mid = (c.high + c.low) / 2
    return upper >= min_wick and body_bot <= mid and c.close <= c.open * 1.001


def is_bullish_continuation(prev: Candle, c: Candle, level: float, min_body_pct: float) -> bool:
    rng = c.high - c.low
    if rng <= 0:
        return False
    body = (c.close - c.open) / rng
    return prev.close <= level and c.close > level and body >= min_body_pct and c.close > c.open


def is_bearish_continuation(prev: Candle, c: Candle, level: float, min_body_pct: float) -> bool:
    rng = c.high - c.low
    if rng <= 0:
        return False
    body = (c.open - c.close) / rng
    return prev.close >= level and c.close < level and body >= min_body_pct and c.close < c.open


def entry_signal_at_level(
    candles: list[Candle],
    index: int,
    level: AoiLevel,
    *,
    min_wick: float,
    min_body_pct: float,
) -> str | None:
    """Return 'long' | 'short' when a candlestick confirms at the AOI."""
    if index < 1:
        return None
    c, prev = candles[index], candles[index - 1]

    if level.kind == "support":
        if is_bullish_rejection(c, min_wick) or is_bullish_engulfing(prev, c):
            return "long"
        if is_bullish_continuation(prev, c, level.price, min_body_pct):
            return "long"
        if is_bearish_continuation(prev, c, level.price, min_body_pct):
            return "short"
    else:
        if is_bearish_rejection(c, min_wick) or is_bearish_engulfing(prev, c):
            return "short"
        if is_bearish_continuation(prev, c, level.price, min_body_pct):
            return "short"
        if is_bullish_continuation(prev, c, level.price, min_body_pct):
            return "long"
    return None


def atr_tolerance(candles: list[Candle], index: int, period: int, mult: float) -> float:
    return atr_at(candles, index, period) * mult
