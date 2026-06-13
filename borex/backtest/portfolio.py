from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from borex.models.candle import SignalAction


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class Trade:
    side: PositionSide
    entry_index: int
    entry_price: float
    entry_time: object
    pattern: str
    exit_index: int | None = None
    exit_price: float | None = None
    exit_time: object | None = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_index is None


@dataclass
class Portfolio:
    initial_capital: float = 10_000.0
    position_size_pct: float = 1.0  # fracción del capital por trade
    cash: float = field(init=False)
    open_trade: Trade | None = None
    closed_trades: list[Trade] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.initial_capital

    @property
    def equity(self) -> float:
        return self.cash

    def open_position(
        self,
        action: SignalAction,
        index: int,
        price: float,
        timestamp: object,
        pattern: str,
    ) -> None:
        if self.open_trade is not None:
            return

        side = PositionSide.LONG if action == SignalAction.BUY else PositionSide.SHORT
        self.open_trade = Trade(
            side=side,
            entry_index=index,
            entry_price=price,
            entry_time=timestamp,
            pattern=pattern,
        )

    def close_position(
        self,
        index: int,
        price: float,
        timestamp: object,
        reason: str = "signal",
    ) -> Trade | None:
        if self.open_trade is None:
            return None

        trade = self.open_trade
        trade.exit_index = index
        trade.exit_price = price
        trade.exit_time = timestamp
        trade.exit_reason = reason

        size = self.initial_capital * self.position_size_pct
        if trade.side == PositionSide.LONG:
            trade.pnl_pct = (price - trade.entry_price) / trade.entry_price
        else:
            trade.pnl_pct = (trade.entry_price - price) / trade.entry_price

        trade.pnl = size * trade.pnl_pct
        self.cash += trade.pnl
        self.closed_trades.append(trade)
        self.open_trade = None
        return trade
