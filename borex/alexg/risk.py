from __future__ import annotations

from borex.alexg.swings import SwingPoint, detect_swings
from borex.models.candle import Candle, SignalAction


def risk_reward(entry: float, stop_loss: float, take_profit: float) -> float:
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    if risk <= 0:
        return 0.0
    return reward / risk


def next_swing_target(
    swings: list[SwingPoint],
    index: int,
    action: SignalAction,
) -> float | None:
    """TP en siguiente swing high (long) o swing low (short)."""
    if action == SignalAction.BUY:
        highs = [s for s in swings if s.kind == "high" and s.index > index]
        return highs[0].price if highs else None
    lows = [s for s in swings if s.kind == "low" and s.index > index]
    return lows[0].price if lows else None


def structure_stop_loss(
    candles: list[Candle],
    index: int,
    action: SignalAction,
    retest_level: float | None,
) -> float:
    """SL bajo estructura de retest / mecha de rechazo."""
    candle = candles[index]
    buffer = candle.close * 0.001

    if action == SignalAction.BUY:
        wick_low = candle.low
        struct = retest_level if retest_level is not None else wick_low
        return min(wick_low, struct) - buffer

    wick_high = candle.high
    struct = retest_level if retest_level is not None else wick_high
    return max(wick_high, struct) + buffer


def apply_sl_multiplier(
    entry: float,
    stop_loss: float,
    action: SignalAction,
    mult: float = 1.0,
) -> float:
    """Escala la distancia entry→SL estructural (>1 = SL más ancho)."""
    if mult == 1.0:
        return stop_loss
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return stop_loss
    if action == SignalAction.BUY:
        return entry - risk * mult
    return entry + risk * mult


def structure_take_profit(
    candles: list[Candle],
    index: int,
    action: SignalAction,
    entry: float,
    stop_loss: float,
    min_rr: float,
    swings: list[SwingPoint] | None = None,
    max_tp_pct: float | None = None,
) -> float:
    """TP por RR (y opcional techo %%). Con max_tp_pct no usa swings lejanos."""
    risk = abs(entry - stop_loss)
    reward = risk * min_rr
    if max_tp_pct is not None:
        reward = min(reward, entry * max_tp_pct)
        if action == SignalAction.BUY:
            return entry + reward
        return entry - reward

    swing_target = None
    if swings:
        swing_target = next_swing_target(swings, index, action)

    min_target = (
        entry + reward
        if action == SignalAction.BUY
        else entry - reward
    )

    if swing_target is None:
        return min_target

    if action == SignalAction.BUY:
        return max(swing_target, min_target)
    return min(swing_target, min_target)


def passes_rr_filter(
    entry: float,
    stop_loss: float,
    take_profit: float,
    min_rr: float,
) -> bool:
    return risk_reward(entry, stop_loss, take_profit) >= min_rr
