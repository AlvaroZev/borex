from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.aoi2 import (
    AOIZone,
    build_bidirectional_aoi,
    next_aoi_target,
    recent_aoi_at_bar,
    scale_tp_toward_target,
    stops_from_aoi_tp,
)
from borex.alexg.confirmation import (
    is_bearish_rejection,
    is_bullish_rejection,
    is_momentum_bearish,
    is_momentum_bullish,
)
from borex.alexg.risk import structure_stop_loss
from borex.alexg.swings import detect_swings
from borex.alexg.trend import Trend, detect_trend
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction
from borex.patterns.candlestick import (
    avg_body,
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_hammer,
    is_shooting_star,
    is_three_black_crows,
    is_three_white_soldiers,
)
from borex.strategy.base import Strategy


def _recent_candle_bias(candles: list[Candle], index: int, lookback: int = 8) -> Trend:
    """Short-term direction from recent closes — where price is going now."""
    start = max(0, index - lookback + 1)
    window = candles[start : index + 1]
    if len(window) < 3:
        return Trend.NEUTRAL

    bullish = sum(1 for c in window if c.is_bullish)
    net = window[-1].close - window[0].open
    if bullish >= len(window) * 0.6 and net > 0:
        return Trend.BULLISH
    if bullish <= len(window) * 0.4 and net < 0:
        return Trend.BEARISH
    return Trend.NEUTRAL


def _current_trend(
    swings: list,
    candles: list[Candle],
    index: int,
) -> Trend:
    """Structure + recent candles; no continuation-pattern prediction."""
    structure = detect_trend(swings)
    bias = _recent_candle_bias(candles, index)
    if structure == Trend.NEUTRAL:
        return bias
    if bias == Trend.NEUTRAL or bias == structure:
        return structure
    return Trend.NEUTRAL


def _bounce_signal_bullish(candles: list[Candle], index: int) -> str | None:
    if index < 1:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    if is_bullish_engulfing(prev, curr):
        return "bullish_engulfing"
    if is_bullish_rejection(curr, avg) or is_hammer(curr, avg):
        return "rejection"
    return None


def _bounce_signal_bearish(candles: list[Candle], index: int) -> str | None:
    if index < 1:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    if is_bearish_engulfing(prev, curr):
        return "bearish_engulfing"
    if is_bearish_rejection(curr, avg) or is_shooting_star(curr, avg):
        return "rejection"
    return None


def _continuation_signal_bullish(candles: list[Candle], index: int) -> str | None:
    if index < 2:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
    if is_bullish_engulfing(prev, curr):
        return "bullish_engulfing"
    if is_momentum_bullish(curr, avg):
        return "momentum"
    if is_three_white_soldiers(c1, c2, c3, avg):
        return "three_white_soldiers"
    return None


def _continuation_signal_bearish(candles: list[Candle], index: int) -> str | None:
    if index < 2:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
    if is_bearish_engulfing(prev, curr):
        return "bearish_engulfing"
    if is_momentum_bearish(curr, avg):
        return "momentum"
    if is_three_black_crows(c1, c2, c3, avg):
        return "three_black_crows"
    return None


def _entry_at_aoi(
    trend: Trend,
    aoi: AOIZone,
    candles: list[Candle],
    index: int,
) -> tuple[SignalAction, str, str] | None:
    """
    Bounce or continuation entry at AOI, aligned with current trend.
    Returns (action, setup_kind, signal_name) or None.
    """
    if trend == Trend.BULLISH:
        if aoi.kind == "support":
            sig = _bounce_signal_bullish(candles, index)
            if sig:
                return SignalAction.BUY, "bounce", sig
        elif aoi.kind == "resistance":
            sig = _continuation_signal_bullish(candles, index)
            if sig and candles[index].close > aoi.level:
                return SignalAction.BUY, "continuation", sig

    if trend == Trend.BEARISH:
        if aoi.kind == "resistance":
            sig = _bounce_signal_bearish(candles, index)
            if sig:
                return SignalAction.SELL, "bounce", sig
        elif aoi.kind == "support":
            sig = _continuation_signal_bearish(candles, index)
            if sig and candles[index].close < aoi.level:
                return SignalAction.SELL, "continuation", sig

    return None


def _rsi(candles: list[Candle], index: int, period: int = 14) -> float | None:
    if index <= period:
        return None
    gains = 0.0
    losses = 0.0
    start = index - period + 1
    for i in range(start, index + 1):
        diff = candles[i].close - candles[i - 1].close
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    if losses == 0:
        return 100.0
    rs = gains / losses if losses > 0 else 0.0
    return 100.0 - (100.0 / (1.0 + rs))


def _avg_volume(candles: list[Candle], index: int, lookback: int = 20) -> float:
    start = max(0, index - lookback + 1)
    window = candles[start : index + 1]
    if not window:
        return 0.0
    return sum(c.volume for c in window) / len(window)


def _volume_ok(candles: list[Candle], index: int, min_mult: float) -> bool:
    avg_vol = _avg_volume(candles, index)
    curr_vol = candles[index].volume
    # Forex data can have zero/noisy volume; only enforce if volume exists.
    if avg_vol <= 0 or curr_vol <= 0:
        return True
    return curr_vol >= avg_vol * min_mult


def passes_confirmation_quality_filter(
    candles: list[Candle],
    index: int,
    action: SignalAction,
    _setup_kind: str,
    signal_name: str,
) -> bool:
    curr = candles[index]
    avg = avg_body(candles[: index + 1])
    if avg <= 0:
        return True
    body = curr.body
    rng = max(curr.range, 1e-9)
    rsi = _rsi(candles, index)

    if signal_name in ("bullish_engulfing", "bearish_engulfing"):
        if index < 1:
            return False
        prev = candles[index - 1]
        if body < avg * 0.8 or body < prev.body * 1.1:
            return False
        if signal_name == "bullish_engulfing" and rsi is not None and rsi > 45:
            return False
        if signal_name == "bearish_engulfing" and rsi is not None and rsi < 55:
            return False
        return _volume_ok(candles, index, 1.1)

    if signal_name == "rejection":
        wick_ratio = (
            (curr.lower_wick / max(body, 1e-9))
            if action == SignalAction.BUY
            else (curr.upper_wick / max(body, 1e-9))
        )
        if wick_ratio < 2.0:
            return False
        if action == SignalAction.BUY and rsi is not None and rsi > 40:
            return False
        if action == SignalAction.SELL and rsi is not None and rsi < 60:
            return False
        return _volume_ok(candles, index, 1.0)

    if signal_name == "momentum":
        if body < avg * 1.4:
            return False
        close_near_extreme = (
            curr.close >= curr.high - rng * 0.2
            if action == SignalAction.BUY
            else curr.close <= curr.low + rng * 0.2
        )
        if not close_near_extreme:
            return False
        return _volume_ok(candles, index, 1.2)

    if signal_name in ("three_white_soldiers", "three_black_crows"):
        if index < 2:
            return False
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        min_body = avg * 0.8
        if min(c1.body, c2.body, c3.body) < min_body:
            return False
        if signal_name == "three_white_soldiers" and rsi is not None and rsi < 50:
            return False
        if signal_name == "three_black_crows" and rsi is not None and rsi > 50:
            return False
        vols = [c1.volume, c2.volume, c3.volume]
        if all(v > 0 for v in vols) and vols[2] < vols[1] * 0.9:
            return False
        return True

    return True


@dataclass
class AlexG2Strategy(Strategy):
    """
    AlexG2 — top-down trend, AOI bounce/continuation, candlestick confirmation.

    1. Trade only in the direction of current market trend (structure + candles).
    2. Areas of interest from swing highs/lows; recent levels preferred.
    3. Wait for candlestick rejection or continuation at an AOI.
    4. TP at the next AOI in trade direction; SL sized for min_rr (default 2:1).
    """

    name: str = "alexg2"
    min_rr: float = 2.0
    tp_fraction: float = 1.0  # 1.0 = full next AOI; 0.7 = 70% of the way
    swing_lookback: int = 5
    aoi_tolerance_pct: float = 0.002
    min_aoi_touches: int = 2
    min_bars: int = 80
    signal_cooldown: int = 8
    filter_false_positives: bool = True
    disabled_signals: tuple[str, ...] = ()

    _last_signal_index: int = field(default=-999, repr=False)

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        if index < self.min_bars:
            return None

        window = candles[: index + 1]
        swings = detect_swings(window, self.swing_lookback)
        if len(swings) < 4:
            return None

        trend = _current_trend(swings, window, index)
        if trend == Trend.NEUTRAL:
            return None

        zones = build_bidirectional_aoi(
            swings,
            window,
            self.aoi_tolerance_pct,
            self.min_aoi_touches,
        )
        if not zones:
            return None

        aoi = recent_aoi_at_bar(
            window, index, zones, lookback=20, tolerance_pct=self.aoi_tolerance_pct
        )
        if aoi is None:
            return None

        entry_info = _entry_at_aoi(trend, aoi, window, index)
        if entry_info is None:
            return None

        action, setup_kind, signal_name = entry_info
        if signal_name in self.disabled_signals:
            return None
        if self.filter_false_positives and not passes_confirmation_quality_filter(
            window, index, action, setup_kind, signal_name
        ):
            return None

        if mtf is not None and not mtf.all_filters_align(index, action):
            return None

        entry = candles[index].close
        tp_level = next_aoi_target(entry, zones, action)
        if tp_level is None:
            return None
        tp_level = scale_tp_toward_target(
            entry, tp_level, action, self.tp_fraction
        )

        structural_sl = structure_stop_loss(window, index, action, aoi.level)
        stops = stops_from_aoi_tp(
            entry, tp_level, action, structural_sl, self.min_rr
        )
        if stops is None:
            return None
        sl, tp = stops

        if index - self._last_signal_index < self.signal_cooldown:
            return None
        self._last_signal_index = index

        pattern = f"alexg2|{trend.value}|{setup_kind}|{aoi.kind}|{signal_name}"
        return Signal(
            action=action,
            pattern=pattern,
            index=index,
            price=entry,
            timestamp=candles[index].timestamp,
            stop_loss=sl,
            take_profit=tp,
            score=self.min_rr,
        )
