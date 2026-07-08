from __future__ import annotations

from dataclasses import dataclass, field

from borex.backtest.portfolio import PositionSide, Trade
from borex.models.candle import SignalAction


@dataclass
class MultiMarketPortfolio:
    """Shared cash across several FX pairs; one open position per symbol."""

    initial_capital: float = 10_000.0
    position_size_pct: float = 0.01
    leverage: float = 1.0
    maintenance_margin_ratio: float = 0.0
    size_mode: str = "margin"
    max_positions: int = 5
    cash: float = field(init=False)
    open_trades: dict[str, Trade] = field(default_factory=dict)
    closed_trades: list[Trade] = field(default_factory=list)
    liquidated: bool = False

    def __post_init__(self) -> None:
        self.cash = self.initial_capital

    @property
    def equity(self) -> float:
        if not self.open_trades:
            return max(0.0, self.cash)
        return self.cash + sum(t.margin for t in self.open_trades.values())

    @property
    def win_rate(self) -> float | None:
        if not self.closed_trades:
            return None
        wins = sum(1 for t in self.closed_trades if t.pnl > 0)
        return wins / len(self.closed_trades)

    def _pnl_pct(self, trade: Trade, price: float) -> float:
        if trade.side == PositionSide.LONG:
            return (price - trade.entry_price) / trade.entry_price
        return (trade.entry_price - price) / trade.entry_price

    def _unrealized_pnl(self, trade: Trade, price: float) -> float:
        move = self._pnl_pct(trade, price)
        if self.size_mode == "margin":
            return trade.margin * move * self.leverage
        return trade.margin * move

    def equity_at_prices(self, prices: dict[str, float]) -> float:
        if not self.open_trades:
            return max(0.0, self.cash)
        unrealized = 0.0
        margin = 0.0
        for sym, trade in self.open_trades.items():
            px = prices.get(sym, trade.entry_price)
            margin += trade.margin
            unrealized += self._unrealized_pnl(trade, px)
        return max(0.0, self.cash + margin + unrealized)

    def compute_margin(
        self,
        entry_price: float,
        stop_loss: float | None = None,
        risk_per_trade_pct: float | None = None,
        size_mode: str | None = None,
    ) -> float:
        mode = size_mode or self.size_mode
        uninvested = self.cash
        if mode == "margin":
            return uninvested * self.position_size_pct
        cap = min(self.equity * self.position_size_pct, uninvested)
        if risk_per_trade_pct is None:
            return cap
        if stop_loss is None or entry_price <= 0 or self.leverage <= 0:
            return cap
        sl_dist_pct = abs(entry_price - stop_loss) / entry_price
        if sl_dist_pct <= 0:
            return cap
        risk_margin = self.equity * risk_per_trade_pct / (self.leverage * sl_dist_pct)
        return min(risk_margin, cap)

    def can_open(self, symbol: str = "") -> bool:
        if self.liquidated or not self.cash:
            return False
        if symbol and symbol in self.open_trades:
            return False
        if len(self.open_trades) >= self.max_positions:
            return False
        return self.compute_margin(1.0) > 0

    def get_trade(self, symbol: str) -> Trade | None:
        return self.open_trades.get(symbol)

    def margin_stop_out_price(self, symbol: str) -> float | None:
        trade = self.open_trades.get(symbol)
        if trade is None or self.leverage <= 0 or self.size_mode != "margin":
            return None
        move = 1.0 / self.leverage
        if trade.side == PositionSide.LONG:
            return trade.entry_price * (1.0 - move)
        return trade.entry_price * (1.0 + move)

    def open_position(
        self,
        symbol: str,
        action: SignalAction,
        index: int,
        price: float,
        timestamp: object,
        pattern: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        score: float = 0.0,
        risk_per_trade_pct: float | None = None,
        size_mode: str | None = None,
    ) -> bool:
        if not self.can_open(symbol):
            return False

        entry_equity = self.equity_at_prices({symbol: price})
        entry_cash = self.cash
        margin = self.compute_margin(
            price, stop_loss, risk_per_trade_pct, size_mode=size_mode
        )
        if margin <= 0:
            return False

        side = PositionSide.LONG if action == SignalAction.BUY else PositionSide.SHORT
        self.cash -= margin
        self.open_trades[symbol] = Trade(
            side=side,
            entry_index=index,
            entry_price=price,
            entry_time=timestamp,
            pattern=pattern,
            symbol=symbol,
            stop_loss=stop_loss,
            take_profit=take_profit,
            score=score,
            margin=margin,
            entry_cash=entry_cash,
            entry_equity=entry_equity,
        )
        return True

    def close_position(
        self,
        symbol: str,
        index: int,
        price: float,
        timestamp: object,
        reason: str = "signal",
    ) -> Trade | None:
        trade = self.open_trades.get(symbol)
        if trade is None:
            return None

        trade.exit_index = index
        trade.exit_price = price
        trade.exit_time = timestamp
        trade.exit_reason = reason
        trade.pnl_pct = self._pnl_pct(trade, price)

        margin = trade.margin
        if self.size_mode == "margin":
            raw_pnl = margin * trade.pnl_pct * self.leverage
            if raw_pnl <= -margin or reason == "margin_stop":
                trade.pnl = -margin
                if reason == "stop_loss":
                    trade.exit_reason = "margin_stop"
            else:
                trade.pnl = raw_pnl
        else:
            trade.pnl = margin * trade.pnl_pct

        self.cash += margin + trade.pnl
        del self.open_trades[symbol]

        if self.cash <= 0 and self.size_mode != "margin":
            self.liquidated = True
            self.cash = 0.0

        self.closed_trades.append(trade)
        return trade

    def charge_commission(self, amount: float) -> None:
        self.cash = max(0.0, self.cash - amount)
        if self.cash <= 0:
            self.liquidated = True
