from __future__ import annotations

from dataclasses import dataclass

from borex.config import BacktestConfig
from borex.models.signal import Signal, SignalAction


@dataclass
class PendingEntry:
    execute_index: int
    action: SignalAction
    signal_price: float
    stop_loss: float | None
    take_profit: float | None
    size_pct: float
    tag: str
    symbol: str


def schedule_entry(
    bar_index: int,
    sig: Signal,
    *,
    symbol: str,
    size_pct: float,
    signal_price: float,
    config: BacktestConfig,
    bar_count: int,
) -> PendingEntry | None:
    if sig.action not in (SignalAction.BUY, SignalAction.SELL):
        return None

    delay = max(0, config.entry_delay_bars)
    if config.fill_mode == "next_open":
        execute = bar_index + 1 + delay
    else:
        execute = bar_index + delay

    if execute >= bar_count:
        return None

    return PendingEntry(
        execute_index=execute,
        action=sig.action,
        signal_price=signal_price,
        stop_loss=sig.stop_loss,
        take_profit=sig.take_profit,
        size_pct=size_pct,
        tag=sig.tag,
        symbol=symbol,
    )


def fill_signal_price(
    bar_index: int,
    *,
    config: BacktestConfig,
    open_price: float,
    close_price: float,
) -> float:
    if config.fill_mode == "next_open" and config.entry_delay_bars == 0:
        return open_price
    return close_price
