from __future__ import annotations

from dataclasses import dataclass, field

from borex.backtest.portfolio import Portfolio, Trade
from borex.models.candle import Candle, Signal, SignalAction
from borex.strategy.base import Strategy


@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    position_size_pct: float = 1.0
    stop_loss_pct: float | None = 0.02  # 2% stop loss
    take_profit_pct: float | None = 0.04  # 4% take profit
    close_on_opposite_signal: bool = True
    commission_pct: float = 0.0


@dataclass
class BacktestResult:
    strategy_name: str
    symbol: str
    timeframe: str
    config: BacktestConfig
    trades: list[Trade]
    final_equity: float
    total_return_pct: float
    win_rate: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    max_drawdown_pct: float
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Estrategia: {self.strategy_name}",
            f"Símbolo: {self.symbol} ({self.timeframe})",
            f"Capital inicial: ${self.config.initial_capital:,.2f}",
            f"Capital final: ${self.final_equity:,.2f}",
            f"Retorno total: {self.total_return_pct:.2%}",
            f"Max drawdown: {self.max_drawdown_pct:.2%}",
            f"Trades: {self.total_trades} (W: {self.winning_trades} / L: {self.losing_trades})",
            f"Win rate: {self.win_rate:.2%}",
        ]
        return "\n".join(lines)


class BacktestEngine:
    def __init__(self, strategy: Strategy, config: BacktestConfig | None = None):
        self.strategy = strategy
        self.config = config or BacktestConfig()

    def run(
        self,
        candles: list[Candle],
        symbol: str = "UNKNOWN",
        timeframe: str = "1d",
    ) -> BacktestResult:
        portfolio = Portfolio(
            initial_capital=self.config.initial_capital,
            position_size_pct=self.config.position_size_pct,
        )
        equity_curve: list[float] = [portfolio.equity]
        peak_equity = portfolio.equity

        for i in range(len(candles)):
            candle = candles[i]

            # Gestionar stop loss / take profit en la vela actual
            if portfolio.open_trade is not None:
                closed = self._check_exit_levels(portfolio, i, candle)
                if closed:
                    equity_curve.append(portfolio.equity)
                    peak_equity = max(peak_equity, portfolio.equity)
                    continue

            signal = self.strategy.on_bar(i, candles)
            if signal is None:
                equity_curve.append(portfolio.equity)
                peak_equity = max(peak_equity, portfolio.equity)
                continue

            self._handle_signal(portfolio, signal, i, candles)
            equity_curve.append(portfolio.equity)
            peak_equity = max(peak_equity, portfolio.equity)

        # Cerrar posición abierta al final del backtest
        if portfolio.open_trade is not None:
            last = candles[-1]
            portfolio.close_position(
                len(candles) - 1,
                last.close,
                last.timestamp,
                reason="end_of_data",
            )
            equity_curve.append(portfolio.equity)

        return self._build_result(
            portfolio, equity_curve, peak_equity, symbol, timeframe
        )

    def _handle_signal(
        self,
        portfolio: Portfolio,
        signal: Signal,
        index: int,
        candles: list[Candle],
    ) -> None:
        # Ejecutar en la apertura de la siguiente vela (evita look-ahead bias)
        if index + 1 >= len(candles):
            return

        next_candle = candles[index + 1]
        exec_price = next_candle.open
        exec_index = index + 1

        if portfolio.open_trade is not None and self.config.close_on_opposite_signal:
            current = portfolio.open_trade
            is_opposite = (
                current.side.value == "long" and signal.action == SignalAction.SELL
            ) or (
                current.side.value == "short" and signal.action == SignalAction.BUY
            )
            if is_opposite:
                portfolio.close_position(
                    exec_index, exec_price, next_candle.timestamp, reason="opposite_signal"
                )

        if portfolio.open_trade is None and signal.action != SignalAction.HOLD:
            portfolio.open_position(
                signal.action,
                exec_index,
                exec_price,
                next_candle.timestamp,
                signal.pattern,
            )

    def _check_exit_levels(
        self, portfolio: Portfolio, index: int, candle: Candle
    ) -> bool:
        trade = portfolio.open_trade
        if trade is None:
            return False

        sl = self.config.stop_loss_pct
        tp = self.config.take_profit_pct

        if trade.side.value == "long":
            sl_price = trade.entry_price * (1 - sl) if sl else None
            tp_price = trade.entry_price * (1 + tp) if tp else None

            if sl_price and candle.low <= sl_price:
                portfolio.close_position(index, sl_price, candle.timestamp, "stop_loss")
                return True
            if tp_price and candle.high >= tp_price:
                portfolio.close_position(index, tp_price, candle.timestamp, "take_profit")
                return True
        else:
            sl_price = trade.entry_price * (1 + sl) if sl else None
            tp_price = trade.entry_price * (1 - tp) if tp else None

            if sl_price and candle.high >= sl_price:
                portfolio.close_position(index, sl_price, candle.timestamp, "stop_loss")
                return True
            if tp_price and candle.low <= tp_price:
                portfolio.close_position(index, tp_price, candle.timestamp, "take_profit")
                return True

        return False

    def _build_result(
        self,
        portfolio: Portfolio,
        equity_curve: list[float],
        peak_equity: float,
        symbol: str,
        timeframe: str,
    ) -> BacktestResult:
        trades = portfolio.closed_trades
        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]

        max_dd = 0.0
        peak = equity_curve[0] if equity_curve else portfolio.initial_capital
        for eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = (peak - eq) / peak
                max_dd = max(max_dd, dd)

        initial = self.config.initial_capital
        final = portfolio.equity
        total_return = (final - initial) / initial if initial else 0.0
        win_rate = len(winners) / len(trades) if trades else 0.0

        return BacktestResult(
            strategy_name=self.strategy.name,
            symbol=symbol,
            timeframe=timeframe,
            config=self.config,
            trades=trades,
            final_equity=final,
            total_return_pct=total_return,
            win_rate=win_rate,
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            max_drawdown_pct=max_dd,
            equity_curve=equity_curve,
        )
