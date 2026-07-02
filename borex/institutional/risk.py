from __future__ import annotations

from borex.models.candle import Candle, SignalAction


def atr(candles: list[Candle], index: int, period: int = 14) -> float:
    """Average True Range."""
    start = max(1, index - period + 1)
    if start > index:
        return 0.0

    trs: list[float] = []
    for i in range(start, index + 1):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def risk_reward(entry: float, stop_loss: float, take_profit: float) -> float:
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    if risk <= 0:
        return 0.0
    return reward / risk


def atr_stop_loss(
    entry: float,
    action: SignalAction,
    atr_value: float,
    mult: float = 1.5,
    structure_level: float | None = None,
) -> float:
    """SL por ATR, ajustado si hay nivel estructural más cercano."""
    distance = atr_value * mult
    if action == SignalAction.BUY:
        atr_sl = entry - distance
        if structure_level is not None:
            return min(atr_sl, structure_level - entry * 0.0002)
        return atr_sl
    atr_sl = entry + distance
    if structure_level is not None:
        return max(atr_sl, structure_level + entry * 0.0002)
    return atr_sl


def atr_take_profit(
    entry: float,
    stop_loss: float,
    action: SignalAction,
    min_rr: float,
    vwap_target: float | None = None,
) -> float:
    """TP por RR mínimo; si VWAP está en el camino, lo usa como objetivo parcial mínimo."""
    risk = abs(entry - stop_loss)
    reward = risk * min_rr
    if action == SignalAction.BUY:
        rr_tp = entry + reward
        if vwap_target is not None and vwap_target > entry:
            return max(rr_tp, vwap_target)
        return rr_tp
    rr_tp = entry - reward
    if vwap_target is not None and vwap_target < entry:
        return min(rr_tp, vwap_target)
    return rr_tp


def passes_rr_filter(
    entry: float,
    stop_loss: float,
    take_profit: float,
    min_rr: float,
) -> bool:
    return risk_reward(entry, stop_loss, take_profit) >= min_rr
