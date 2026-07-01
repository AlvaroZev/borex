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
    stop_loss: float | None = None
    take_profit: float | None = None
    score: float = 0.0
    margin: float = 0.0
    entry_equity: float = 0.0
    exit_index: int | None = None
    exit_price: float | None = None
    exit_time: object | None = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    commission: float = 0.0
    exit_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_index is None


@dataclass
class Portfolio:
    initial_capital: float = 10_000.0
    position_size_pct: float = 1.0  # fracción del equity usada como margen
    leverage: float = 1.0
    maintenance_margin_ratio: float = 0.0  # liquidar cuando equity <= margin × ratio
    cash: float = field(init=False)
    open_trade: Trade | None = None
    closed_trades: list[Trade] = field(default_factory=list)
    liquidated: bool = False

    def __post_init__(self) -> None:
        self.cash = self.initial_capital

    @property
    def equity(self) -> float:
        if self.open_trade is None:
            return max(0.0, self.cash)
        return self.equity_at(self.open_trade.entry_price)

    def _pnl_pct(self, trade: Trade, price: float) -> float:
        if trade.side == PositionSide.LONG:
            return (price - trade.entry_price) / trade.entry_price
        return (trade.entry_price - price) / trade.entry_price

    def _min_equity(self) -> float:
        if self.open_trade is None:
            return 0.0
        return self.open_trade.margin * self.maintenance_margin_ratio

    def equity_at(self, price: float) -> float:
        if self.open_trade is None:
            return max(0.0, self.cash)
        trade = self.open_trade
        unrealized = trade.margin * self._pnl_pct(trade, price) * self.leverage
        return max(0.0, self.cash + trade.margin + unrealized)

    def margin_level_at(self, price: float) -> float:
        """Equity / margen usado. Inf si no hay posición."""
        if self.open_trade is None or self.open_trade.margin <= 0:
            return float("inf")
        return self.equity_at(price) / self.open_trade.margin

    def adverse_price(self, low: float, high: float) -> float:
        """Peor precio intrabar para la posición abierta."""
        if self.open_trade is None:
            return low
        if self.open_trade.side == PositionSide.LONG:
            return low
        return high

    def equity_at_adverse(self, low: float, high: float) -> float:
        return self.equity_at(self.adverse_price(low, high))

    def is_margin_call_at(self, price: float) -> bool:
        if self.open_trade is None:
            return False
        return self.equity_at(price) <= self._min_equity()

    def liquidation_price(self) -> float | None:
        trade = self.open_trade
        if trade is None or trade.margin <= 0 or self.leverage <= 0:
            return None
        threshold = self._min_equity()
        move = (threshold - trade.entry_equity) / (trade.margin * self.leverage)
        if trade.side == PositionSide.LONG:
            return trade.entry_price * (1.0 + move)
        return trade.entry_price * (1.0 - move)

    def can_open(self) -> bool:
        return (
            not self.liquidated
            and self.open_trade is None
            and self.cash > 0
        )

    def open_position(
        self,
        action: SignalAction,
        index: int,
        price: float,
        timestamp: object,
        pattern: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        score: float = 0.0,
    ) -> bool:
        if not self.can_open():
            return False

        entry_equity = self.equity
        margin = entry_equity * self.position_size_pct
        if margin <= 0:
            return False

        side = PositionSide.LONG if action == SignalAction.BUY else PositionSide.SHORT
        self.cash -= margin
        self.open_trade = Trade(
            side=side,
            entry_index=index,
            entry_price=price,
            entry_time=timestamp,
            pattern=pattern,
            stop_loss=stop_loss,
            take_profit=take_profit,
            score=score,
            margin=margin,
            entry_equity=entry_equity,
        )
        return True

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
        trade.pnl_pct = self._pnl_pct(trade, price)

        margin = trade.margin
        trade.pnl = margin * trade.pnl_pct * self.leverage
        self.cash += margin + trade.pnl
        self.open_trade = None

        threshold = margin * self.maintenance_margin_ratio
        if reason == "liquidation":
            trade.pnl = threshold - trade.entry_equity
            self.cash = max(0.0, threshold)
        elif self.cash <= 0:
            trade.pnl = -trade.entry_equity
            self.cash = 0.0
        else:
            self.cash = max(0.0, self.cash)

        if self.cash <= 0:
            self.liquidated = True

        self.closed_trades.append(trade)
        return trade

    def charge_commission(self, amount: float) -> None:
        self.cash = max(0.0, self.cash - amount)
        if self.cash <= 0:
            self.liquidated = True
