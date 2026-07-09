from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from borex.models.signal import SignalAction


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class Trade:
    id: int
    side: PositionSide
    entry_index: int
    entry_price: float
    entry_time: object
    pattern: str
    symbol: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None
    margin: float = 0.0
    entry_equity: float = 0.0
    exit_index: int | None = None
    exit_price: float | None = None
    exit_time: object | None = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    commission: float = 0.0
    exit_reason: str = ""
    signal_entry_price: float = 0.0
    signal_exit_price: float | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_index is None


@dataclass
class Portfolio:
    initial_capital: float = 1_000.0
    leverage: float = 500.0
    position_size_pct: float = 0.1
    max_positions: int = 5
    maintenance_margin_ratio: float = 0.5
    cash: float = 0.0
    open_trades: list[Trade] = None  # type: ignore[assignment]
    closed_trades: list[Trade] = None  # type: ignore[assignment]
    liquidated: bool = False
    halted: bool = False
    _next_id: int = 1

    def __post_init__(self) -> None:
        if self.open_trades is None:
            self.open_trades = []
        if self.closed_trades is None:
            self.closed_trades = []
        self.cash = self.initial_capital

    @property
    def used_margin(self) -> float:
        return sum(t.margin for t in self.open_trades)

    @property
    def equity(self) -> float:
        if not self.open_trades:
            return max(0.0, self.cash)
        return self.cash + self.used_margin + sum(self._unrealized(t, t.entry_price) for t in self.open_trades)

    def _pnl_pct(self, trade: Trade, price: float) -> float:
        if trade.side == PositionSide.LONG:
            return (price - trade.entry_price) / trade.entry_price
        return (trade.entry_price - price) / trade.entry_price

    def _pnl_pct_at(self, trade: Trade, entry: float, exit_px: float) -> float:
        if entry <= 0:
            return 0.0
        if trade.side == PositionSide.LONG:
            return (exit_px - entry) / entry
        return (entry - exit_px) / entry

    def _unrealized(self, trade: Trade, price: float) -> float:
        return trade.margin * self._pnl_pct(trade, price) * self.leverage

    def equity_at(self, prices: dict[int, float]) -> float:
        if not self.open_trades:
            return max(0.0, self.cash)
        unrealized = 0.0
        for t in self.open_trades:
            px = prices.get(t.id, t.entry_price)
            unrealized += self._unrealized(t, px)
        return max(0.0, self.cash + self.used_margin + unrealized)

    def can_open(self, size_pct: float = 1.0) -> bool:
        if self.liquidated or self.halted or len(self.open_trades) >= self.max_positions:
            return False
        margin = self.equity * self.position_size_pct * size_pct
        return margin > 0 and (self.used_margin + margin) <= self.equity

    def open_position(
        self,
        action: SignalAction,
        index: int,
        price: float,
        timestamp: object,
        pattern: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        size_pct: float = 1.0,
        symbol: str = "",
        signal_entry_price: float = 0.0,
    ) -> Trade | None:
        if action == SignalAction.CLOSE or not self.can_open(size_pct):
            return None

        entry_equity = self.equity
        margin = entry_equity * self.position_size_pct * size_pct
        if margin <= 0:
            return None

        side = PositionSide.LONG if action == SignalAction.BUY else PositionSide.SHORT
        trade = Trade(
            id=self._next_id,
            side=side,
            entry_index=index,
            entry_price=price,
            entry_time=timestamp,
            pattern=pattern,
            symbol=symbol,
            stop_loss=stop_loss,
            take_profit=take_profit,
            margin=margin,
            entry_equity=entry_equity,
            signal_entry_price=signal_entry_price or price,
        )
        self._next_id += 1
        self.cash -= margin
        self.open_trades.append(trade)
        return trade

    def close_position(
        self,
        trade: Trade,
        index: int,
        price: float,
        timestamp: object,
        reason: str = "signal",
    ) -> Trade:
        trade.exit_index = index
        trade.exit_price = price
        trade.exit_time = timestamp
        trade.exit_reason = reason
        trade.pnl_pct = self._pnl_pct(trade, price)
        trade.pnl = trade.margin * trade.pnl_pct * self.leverage

        self.cash += trade.margin + trade.pnl
        self.open_trades = [t for t in self.open_trades if t.id != trade.id]

        if self.cash <= 0:
            self.cash = 0.0
            self.liquidated = True
            for open_t in list(self.open_trades):
                open_t.exit_index = index
                open_t.exit_price = price
                open_t.exit_time = timestamp
                open_t.exit_reason = "liquidation_cascade"
                open_t.pnl = -open_t.entry_equity
                self.closed_trades.append(open_t)
            self.open_trades.clear()

        self.closed_trades.append(trade)
        return trade

    def charge_commission(self, amount: float) -> None:
        self.cash = max(0.0, self.cash - amount)
        if self.cash <= 0:
            self.liquidated = True

    def to_state(self) -> dict:
        def _trade(t: Trade) -> dict:
            return {
                "id": t.id,
                "side": t.side.value,
                "entry_index": t.entry_index,
                "entry_price": t.entry_price,
                "entry_time": str(t.entry_time),
                "pattern": t.pattern,
                "symbol": t.symbol,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "margin": t.margin,
                "entry_equity": t.entry_equity,
                "exit_index": t.exit_index,
                "exit_price": t.exit_price,
                "exit_time": str(t.exit_time) if t.exit_time is not None else None,
                "pnl": t.pnl,
                "pnl_pct": t.pnl_pct,
                "commission": t.commission,
                "exit_reason": t.exit_reason,
                "signal_entry_price": t.signal_entry_price,
                "signal_exit_price": t.signal_exit_price,
            }

        return {
            "initial_capital": self.initial_capital,
            "leverage": self.leverage,
            "position_size_pct": self.position_size_pct,
            "max_positions": self.max_positions,
            "maintenance_margin_ratio": self.maintenance_margin_ratio,
            "cash": self.cash,
            "liquidated": self.liquidated,
            "halted": self.halted,
            "_next_id": self._next_id,
            "open_trades": [_trade(t) for t in self.open_trades],
            "closed_trades": [_trade(t) for t in self.closed_trades],
        }

    @classmethod
    def from_state(cls, state: dict) -> Portfolio:
        p = cls(
            initial_capital=state["initial_capital"],
            leverage=state["leverage"],
            position_size_pct=state["position_size_pct"],
            max_positions=state["max_positions"],
            maintenance_margin_ratio=state["maintenance_margin_ratio"],
        )
        p.cash = state["cash"]
        p.liquidated = state.get("liquidated", False)
        p.halted = state.get("halted", False)
        p._next_id = state.get("_next_id", 1)

        def _load(raw: dict) -> Trade:
            return Trade(
                id=raw["id"],
                side=PositionSide(raw["side"]),
                entry_index=raw["entry_index"],
                entry_price=raw["entry_price"],
                entry_time=raw["entry_time"],
                pattern=raw["pattern"],
                symbol=raw.get("symbol", ""),
                stop_loss=raw.get("stop_loss"),
                take_profit=raw.get("take_profit"),
                margin=raw["margin"],
                entry_equity=raw["entry_equity"],
                exit_index=raw.get("exit_index"),
                exit_price=raw.get("exit_price"),
                exit_time=raw.get("exit_time"),
                pnl=raw.get("pnl", 0.0),
                pnl_pct=raw.get("pnl_pct", 0.0),
                commission=raw.get("commission", 0.0),
                exit_reason=raw.get("exit_reason", ""),
                signal_entry_price=raw.get("signal_entry_price", raw["entry_price"]),
                signal_exit_price=raw.get("signal_exit_price"),
            )

        p.open_trades = [_load(t) for t in state.get("open_trades", [])]
        p.closed_trades = [_load(t) for t in state.get("closed_trades", [])]
        return p
