from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import sys

from borex.alexg.multi_market import (
    MultiMarketContext,
    align_symbols_to_timeline,
    pick_master_symbol,
)
from borex.backtest.costs import infer_pip_size, TradeCosts, apply_entry_fill, apply_exit_fill
from borex.backtest.engine import BacktestConfig, BacktestResult, mirror_sl_tp_for_inverse
from borex.backtest.engine import _build_confirmation_stats, _signal_entry
from borex.backtest.margin_stops import (
    margin_stop_out_prices,
    rr_from_winrate,
    tighten_sl_to_margin_stop,
    tp_from_sl_rr,
)
from borex.backtest.multi_portfolio import MultiMarketPortfolio
from borex.backtest.portfolio import PositionSide, Trade
from borex.models.candle import Candle, Signal, SignalAction

if TYPE_CHECKING:
    from borex.alexg.strategy3 import AlexG3Strategy
    from borex.alexg.strategy4 import AlexG4Strategy


@dataclass
class MultiMarketBacktestResult(BacktestResult):
    symbols: list[str] = field(default_factory=list)
    master_symbol: str = ""

    def summary(self) -> str:
        base = super().summary()
        extra = [
            f"Universo: {len(self.symbols)} pares",
            f"Master timeline: {self.master_symbol}",
        ]
        return base + "\n" + "\n".join(extra)


class MultiMarketEngine:
    """Backtest AlexG3 across many FX pairs with one shared portfolio."""

    def __init__(
        self,
        strategy: AlexG3Strategy,
        config: BacktestConfig | None = None,
        max_positions: int = 5,
    ):
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.max_positions = max_positions
        self._total_commission = 0.0
        self._progress_every = 5

    def _costs(self, symbol: str) -> TradeCosts:
        pip = self.config.pip_size or infer_pip_size(symbol)
        return TradeCosts(
            spread_pips=self.config.spread_pips,
            slippage_pips=self.config.slippage_pips,
            commission_per_trade=self.config.commission_per_trade,
            pip_size=pip,
        )

    def run(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        timeframe: str = "1h",
        master_symbol: str | None = None,
    ) -> MultiMarketBacktestResult:
        if not candles_by_symbol:
            raise ValueError("No hay velas cargadas")

        master = master_symbol or pick_master_symbol(candles_by_symbol)
        master_candles = candles_by_symbol[master]
        symbols = [s for s in candles_by_symbol if s in candles_by_symbol]
        ts_maps = align_symbols_to_timeline(master_candles, candles_by_symbol)

        portfolio = MultiMarketPortfolio(
            initial_capital=self.config.initial_capital,
            position_size_pct=self.config.position_size_pct,
            leverage=self.config.leverage,
            maintenance_margin_ratio=self.config.maintenance_margin_ratio,
            size_mode=self.config.size_mode,
            max_positions=self.max_positions,
        )

        equity_curve: list[float] = [portfolio.equity]
        peak = portfolio.equity
        max_dd = 0.0
        min_bars = self.strategy.min_bars

        for master_i in range(min_bars, len(master_candles)):
            if portfolio.liquidated:
                break

            ctx = MultiMarketContext.at_master_bar(
                master_i,
                master_candles,
                candles_by_symbol,
                ts_maps,
                strength_lookback=self.strategy.strength_lookback,
                min_currency_edge=self.strategy.min_currency_edge,
                min_confirming_pairs=self.strategy.min_confirming_pairs,
            )

            prices: dict[str, float] = {}
            for sym, idx in ctx.indices.items():
                prices[sym] = candles_by_symbol[sym][idx].close

            for sym in list(portfolio.open_trades.keys()):
                idx = ctx.indices.get(sym)
                if idx is None:
                    continue
                candle = candles_by_symbol[sym][idx]
                prices[sym] = candle.close
                if self._check_exit(portfolio, sym, idx, candle):
                    continue

            candidates: list[tuple[str, Signal, int]] = []
            for sym in symbols:
                idx = ctx.indices.get(sym)
                if idx is None or idx < min_bars:
                    continue
                if sym in portfolio.open_trades:
                    continue
                self.strategy.set_context(sym, ctx)
                signal = self.strategy.on_bar(idx, candles_by_symbol[sym], None)
                if signal is not None:
                    candidates.append((sym, signal, idx))

            candidates.sort(key=lambda x: x[1].score, reverse=True)
            for sym, signal, idx in candidates:
                if not portfolio.can_open(sym):
                    break
                self._open_signal(portfolio, sym, signal, idx, candles_by_symbol[sym])

            eq = portfolio.equity_at_prices(prices)
            equity_curve.append(eq)
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)

        for sym, trade in list(portfolio.open_trades.items()):
            candles = candles_by_symbol[sym]
            last = candles[-1]
            self._close(portfolio, sym, len(candles) - 1, last.close, last.timestamp, "end_of_data")

        equity_curve.append(portfolio.equity)
        return self._build_result(
            portfolio, equity_curve, max_dd, symbols, master, timeframe
        )

    def _open_signal(
        self,
        portfolio: MultiMarketPortfolio,
        symbol: str,
        signal: Signal,
        index: int,
        candles: list[Candle],
    ) -> None:
        entry = _signal_entry(signal, index, candles)
        if entry is None:
            return
        exec_index, mid_price, exec_timestamp = entry
        costs = self._costs(symbol)
        action = self._effective_action(signal.action)
        side = PositionSide.LONG if action == SignalAction.BUY else PositionSide.SHORT
        exec_price = apply_entry_fill(mid_price, side, costs)

        stop_loss = signal.stop_loss
        take_profit = signal.take_profit
        rr = (
            rr_from_winrate(portfolio.win_rate, self.config.true_sl_rr)
            * self.config.rr_factor
        )

        # Inverse flips the fill side first. Mirror analysis SL/TP onto that
        # side BEFORE margin sizing — never after (that double-flips levels).
        if (
            self.config.inversed
            and stop_loss is not None
            and take_profit is not None
        ):
            stop_loss, take_profit = mirror_sl_tp_for_inverse(
                exec_price, stop_loss, take_profit
            )

        if self.config.true_sl and self.config.size_mode == "margin":
            stop_loss, take_profit = margin_stop_out_prices(
                exec_price, side, self.config.leverage, rr
            )
        elif self.config.size_mode == "margin":
            if stop_loss is not None:
                stop_loss = tighten_sl_to_margin_stop(
                    exec_price, stop_loss, side, self.config.leverage
                )
            if stop_loss is not None:
                take_profit = tp_from_sl_rr(exec_price, stop_loss, side, rr)
        elif stop_loss is not None:
            take_profit = tp_from_sl_rr(exec_price, stop_loss, side, rr)

        portfolio.open_position(
            symbol,
            action,
            exec_index,
            exec_price,
            exec_timestamp,
            signal.pattern,
            stop_loss=stop_loss,
            take_profit=take_profit,
            score=rr,
            risk_per_trade_pct=self.config.risk_per_trade_pct,
            size_mode=self.config.size_mode,
        )

    def _close(
        self,
        portfolio: MultiMarketPortfolio,
        symbol: str,
        index: int,
        price: float,
        timestamp: object,
        reason: str,
    ) -> None:
        trade = portfolio.get_trade(symbol)
        if trade is None:
            return
        costs = self._costs(symbol)
        fill = apply_exit_fill(price, trade.side, costs)
        portfolio.close_position(symbol, index, fill, timestamp, reason)
        if self.config.commission_per_trade > 0:
            portfolio.charge_commission(self.config.commission_per_trade)
            self._total_commission += self.config.commission_per_trade
        n = len(portfolio.closed_trades)
        if n > 0 and n % self._progress_every == 0:
            closed = portfolio.closed_trades[-1]
            wins = sum(1 for t in portfolio.closed_trades if t.pnl > 0)
            sign = "+" if closed.pnl >= 0 else ""
            print(
                f"[alexg3] trades={n} | last {closed.symbol} {closed.side.value} "
                f"PnL {sign}{closed.pnl:.2f} [{closed.exit_reason}] | "
                f"WR {wins}/{n} ({wins / n:.1%}) | cash ${portfolio.cash:,.2f}",
                flush=True,
                file=sys.stderr,
            )

    def _check_exit(
        self,
        portfolio: MultiMarketPortfolio,
        symbol: str,
        index: int,
        candle: Candle,
    ) -> bool:
        trade = portfolio.get_trade(symbol)
        if trade is None:
            return False

        if self.config.size_mode == "margin":
            ms = portfolio.margin_stop_out_price(symbol)
            if ms is not None and self._hit_margin_stop(trade, candle, ms):
                self._close(portfolio, symbol, index, ms, candle.timestamp, "margin_stop")
                return True

        if trade.side == PositionSide.LONG:
            sl, tp = trade.stop_loss, trade.take_profit
            if sl and candle.low <= sl:
                self._close(portfolio, symbol, index, sl, candle.timestamp, "stop_loss")
                return True
            if tp and candle.high >= tp:
                self._close(portfolio, symbol, index, tp, candle.timestamp, "take_profit")
                return True
        else:
            sl, tp = trade.stop_loss, trade.take_profit
            if sl and candle.high >= sl:
                self._close(portfolio, symbol, index, sl, candle.timestamp, "stop_loss")
                return True
            if tp and candle.low <= tp:
                self._close(portfolio, symbol, index, tp, candle.timestamp, "take_profit")
                return True
        return False

    @staticmethod
    def _hit_margin_stop(trade: Trade, candle: Candle, ms_price: float) -> bool:
        if trade.side == PositionSide.LONG:
            return candle.low <= ms_price
        return candle.high >= ms_price

    def _effective_action(self, action: SignalAction) -> SignalAction:
        if not self.config.inversed or action == SignalAction.HOLD:
            return action
        if action == SignalAction.BUY:
            return SignalAction.SELL
        return SignalAction.BUY

    def _build_result(
        self,
        portfolio: MultiMarketPortfolio,
        equity_curve: list[float],
        max_dd: float,
        symbols: list[str],
        master: str,
        timeframe: str,
    ) -> MultiMarketBacktestResult:
        trades = portfolio.closed_trades
        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]
        initial = self.config.initial_capital
        final = max(0.0, portfolio.cash)
        total_return = (final - initial) / initial if initial else 0.0

        planned_rrs: list[float] = []
        for t in trades:
            if t.stop_loss is None or t.take_profit is None or t.entry_price <= 0:
                continue
            risk = abs(t.entry_price - t.stop_loss)
            reward = abs(t.take_profit - t.entry_price)
            if risk > 0:
                planned_rrs.append(reward / risk)

        sym_label = f"multi ({len(symbols)} pairs, master {master})"
        result = MultiMarketBacktestResult(
            strategy_name=self.strategy.name,
            symbol=sym_label,
            timeframe=timeframe,
            config=self.config,
            trades=trades,
            final_equity=final,
            total_return_pct=total_return,
            win_rate=len(winners) / len(trades) if trades else 0.0,
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            max_drawdown_pct=max_dd,
            total_commission=self._total_commission,
            equity_curve=equity_curve,
            avg_win=sum(t.pnl for t in winners) / len(winners) if winners else 0.0,
            avg_loss=sum(t.pnl for t in losers) / len(losers) if losers else 0.0,
            profit_factor=(
                sum(t.pnl for t in winners) / abs(sum(t.pnl for t in losers))
                if losers and sum(t.pnl for t in losers) != 0
                else 0.0
            ),
            avg_planned_rr=sum(planned_rrs) / len(planned_rrs) if planned_rrs else 0.0,
            confirmation_stats=_build_confirmation_stats(trades),
            symbols=symbols,
            master_symbol=master,
        )
        return result
