from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from borex.backtest.portfolio import PositionSide, Trade


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    LIQUIDATION = "liquidation"
    SIGNAL = "signal"


@dataclass(frozen=True)
class ExitEvent:
    trade_id: int
    price: float
    reason: ExitReason


def _hit_long(trade: Trade, low: float, high: float) -> ExitEvent | None:
    sl_hit = trade.stop_loss is not None and low <= trade.stop_loss
    tp_hit = trade.take_profit is not None and high >= trade.take_profit
    if sl_hit and tp_hit:
        # Conservative: assume stop loss before take profit on same bar.
        return ExitEvent(trade.id, trade.stop_loss, ExitReason.STOP_LOSS)  # type: ignore[arg-type]
    if sl_hit:
        return ExitEvent(trade.id, trade.stop_loss, ExitReason.STOP_LOSS)  # type: ignore[arg-type]
    if tp_hit:
        return ExitEvent(trade.id, trade.take_profit, ExitReason.TAKE_PROFIT)  # type: ignore[arg-type]
    return None


def _hit_short(trade: Trade, low: float, high: float) -> ExitEvent | None:
    sl_hit = trade.stop_loss is not None and high >= trade.stop_loss
    tp_hit = trade.take_profit is not None and low <= trade.take_profit
    if sl_hit and tp_hit:
        return ExitEvent(trade.id, trade.stop_loss, ExitReason.STOP_LOSS)  # type: ignore[arg-type]
    if sl_hit:
        return ExitEvent(trade.id, trade.stop_loss, ExitReason.STOP_LOSS)  # type: ignore[arg-type]
    if tp_hit:
        return ExitEvent(trade.id, trade.take_profit, ExitReason.TAKE_PROFIT)  # type: ignore[arg-type]
    return None


def check_exits(
    trades: list[Trade],
    bar_index: int,
    low: float,
    high: float,
    *,
    skip_entry_bar: bool = True,
) -> list[ExitEvent]:
    """Price-reached exits. Skips entry bar to avoid same-bar auto win/loss."""
    events: list[ExitEvent] = []
    for trade in trades:
        if skip_entry_bar and trade.entry_index == bar_index:
            continue
        if trade.side == PositionSide.LONG:
            ev = _hit_long(trade, low, high)
        else:
            ev = _hit_short(trade, low, high)
        if ev:
            events.append(ev)
    return events


def adverse_price(trade: Trade, low: float, high: float) -> float:
    if trade.side == PositionSide.LONG:
        return low
    return high
