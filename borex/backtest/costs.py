from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from borex.backtest.portfolio import Portfolio, Trade
from borex.config import BacktestConfig
from borex.models.signal import Candle
from borex.strategy.indicators import atr_at


class SlippageMode(str, Enum):
    FIXED = "fixed"
    ATR = "atr"


class FillMode(str, Enum):
    CLOSE = "close"
    NEXT_OPEN = "next_open"


@dataclass
class ExecutionStats:
    total_commission: float = 0.0
    total_spread_cost: float = 0.0
    total_slippage_cost: float = 0.0
    theoretical_pnl: float = 0.0
    actual_pnl: float = 0.0
    execution_drag: float = 0.0
    fills: int = 0

    def to_dict(self) -> dict:
        return {
            "total_commission": round(self.total_commission, 4),
            "total_spread_cost": round(self.total_spread_cost, 4),
            "total_slippage_cost": round(self.total_slippage_cost, 4),
            "theoretical_pnl": round(self.theoretical_pnl, 4),
            "actual_pnl": round(self.actual_pnl, 4),
            "execution_drag": round(self.execution_drag, 4),
            "fills": self.fills,
        }


@dataclass
class CostModel:
    commission_pct: float = 0.0
    slippage_pct: float = 0.0
    spread_pct: float = 0.0
    slippage_mode: str = SlippageMode.FIXED.value
    slippage_atr_mult: float = 0.1
    atr_period: int = 14
    _stats: ExecutionStats = field(default_factory=ExecutionStats, repr=False)

    @classmethod
    def from_config(cls, config: BacktestConfig) -> CostModel:
        return cls(
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct,
            spread_pct=config.spread_pct,
            slippage_mode=config.slippage_mode,
            slippage_atr_mult=config.slippage_atr_mult,
            atr_period=config.atr_period,
        )

    @property
    def stats(self) -> ExecutionStats:
        return self._stats

    def _side_slippage(self, candles: list[Candle] | None, index: int | None) -> float:
        slip = self.slippage_pct
        if (
            self.slippage_mode == SlippageMode.ATR.value
            and candles is not None
            and index is not None
            and index >= 0
        ):
            atr = atr_at(candles, index, self.atr_period)
            price = candles[index].close
            if price > 0 and atr > 0:
                slip += (atr / price) * self.slippage_atr_mult
        return slip

    def entry_price(
        self,
        side: str,
        price: float,
        *,
        candles: list[Candle] | None = None,
        index: int | None = None,
    ) -> float:
        slip = self._side_slippage(candles, index)
        spread = self.spread_pct
        if side == "long":
            return price * (1 + spread + slip)
        return price * (1 - spread - slip)

    def exit_price(
        self,
        side: str,
        price: float,
        *,
        candles: list[Candle] | None = None,
        index: int | None = None,
    ) -> float:
        slip = self._side_slippage(candles, index)
        spread = self.spread_pct
        if side == "long":
            return price * (1 - spread - slip)
        return price * (1 + spread + slip)

    def commission(self, notional: float) -> float:
        return notional * self.commission_pct

    def _record_fill(
        self,
        portfolio: Portfolio,
        trade: Trade,
        signal_price: float,
        fill_price: float,
    ) -> None:
        if signal_price <= 0:
            return
        notional = trade.margin * portfolio.leverage
        spread_cost = notional * self.spread_pct
        slip_frac = abs(fill_price - signal_price) / signal_price - self.spread_pct
        slip_frac = max(0.0, slip_frac)
        slip_cost = notional * slip_frac
        self._stats.total_spread_cost += spread_cost
        self._stats.total_slippage_cost += slip_cost
        self._stats.fills += 1

    def charge_open(
        self,
        portfolio: Portfolio,
        trade: Trade,
        *,
        signal_price: float,
        candles: list[Candle] | None = None,
        index: int | None = None,
    ) -> None:
        notional = trade.margin * portfolio.leverage
        fee = self.commission(notional)
        trade.commission += fee
        self._stats.total_commission += fee
        portfolio.charge_commission(fee)
        self._record_fill(portfolio, trade, signal_price, trade.entry_price)

    def charge_close(
        self,
        portfolio: Portfolio,
        trade: Trade,
        exit_signal_price: float,
        *,
        candles: list[Candle] | None = None,
        index: int | None = None,
    ) -> float:
        notional = trade.margin * portfolio.leverage
        fee = self.commission(notional)
        trade.commission += fee
        self._stats.total_commission += fee
        portfolio.charge_commission(fee)
        px = self.exit_price(
            trade.side.value,
            exit_signal_price,
            candles=candles,
            index=index,
        )
        trade.signal_exit_price = exit_signal_price
        self._record_fill(portfolio, trade, exit_signal_price, px)
        return px
