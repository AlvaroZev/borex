from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
import sys

from borex.backtest.costs import TradeCosts, apply_entry_fill, apply_exit_fill, infer_pip_size
from borex.backtest.margin_stops import (
    margin_stop_out_prices,
    rr_from_winrate,
    tighten_sl_to_margin_stop,
    tp_from_sl_rr,
)
from borex.backtest.portfolio import Portfolio, PositionSide, Trade
from borex.models.candle import Candle, Signal, SignalAction
from borex.strategy.base import Strategy

if TYPE_CHECKING:
    from borex.data.mtf import MultiTimeframeContext


def mirror_sl_tp_for_inverse(
    entry: float, stop_loss: float, take_profit: float
) -> tuple[float, float]:
    """
    Mirror SL/TP around entry when flipping trade direction.
    Preserves the same risk/reward distances (not a naive swap).
    """
    return 2.0 * entry - stop_loss, 2.0 * entry - take_profit


@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    position_size_pct: float = 1.0
    leverage: float = 1.0
    maintenance_margin_ratio: float = 0.0  # stop-out: equity <= margin × ratio
    inversed: bool = False
    stop_loss_pct: float | None = 0.02  # 2% stop loss
    take_profit_pct: float | None = 0.04  # 4% take profit
    risk_per_trade_pct: float | None = None  # riesgo fijo si SL toca (ej. 0.01 = 1%)
    size_mode: str = "fixed_risk"  # fixed_risk | margin
    close_on_opposite_signal: bool = False
    true_sl: bool = False  # SL at margin wipe; TP at true_sl_rr
    true_sl_rr: float = 2.0
    rr_factor: float = 1.0  # multiply winrate-derived RR (e.g. 1.1 = 10% wider TP)
    spread_pips: float = 0.0
    slippage_pips: float = 0.0
    commission_per_trade: float = 0.0
    pip_size: float | None = None  # auto desde símbolo si None


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
    filter_intervals: list[str] | None = None
    total_commission: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    avg_planned_rr: float = 0.0
    confirmation_stats: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        tf = self.timeframe
        if self.filter_intervals:
            filt = "+".join(self.filter_intervals)
            tf = f"{self.timeframe} (filtro: {filt})"
        lines = [
            f"Estrategia: {self.strategy_name}",
            f"Símbolo: {self.symbol} ({tf})",
            f"Capital inicial: ${self.config.initial_capital:,.2f}",
            f"Apalancamiento: {self.config.leverage:g}x",
            f"Size mode: {self.config.size_mode}",
            f"Margen por trade: {self.config.position_size_pct:.2%} del cash libre",
            f"True SL: {'sí' if self.config.true_sl else 'no'}",
            f"Invertido: {'sí' if self.config.inversed else 'no'}",
            f"Capital final: ${self.final_equity:,.2f}",
            f"Retorno total: {self.total_return_pct:.2%}",
            f"Max drawdown: {self.max_drawdown_pct:.2%}",
            f"Trades: {self.total_trades} (W: {self.winning_trades} / L: {self.losing_trades})",
            f"Win rate: {self.win_rate:.2%}",
        ]
        if self.total_trades > 0:
            lines.append(
                f"Avg win: ${self.avg_win:,.2f} | Avg loss: ${self.avg_loss:,.2f} | "
                f"Profit factor: {self.profit_factor:.2f}"
            )
            if self.avg_planned_rr > 0:
                lines.append(f"Planned RR (avg): {self.avg_planned_rr:.2f}:1")
        if (
            self.config.spread_pips
            or self.config.slippage_pips
            or self.config.commission_per_trade
        ):
            lines.append(
                f"Costos: spread {self.config.spread_pips:g} pips | "
                f"slippage {self.config.slippage_pips:g} pips | "
                f"comisión ${self.config.commission_per_trade:.2f}/trade"
            )
            lines.append(f"Comisión total pagada: ${self.total_commission:,.2f}")
        if self.confirmation_stats:
            lines.append("Confirmaciones (W/L/Total):")
            for row in self.confirmation_stats:
                lines.append(
                    f"  - {row['signal']}: {row['wins']}/{row['losses']}/{row['total']} "
                    f"(WR {row['win_rate']:.2%}, PnL ${row['pnl']:+,.2f})"
                )
        return "\n".join(lines)


def _is_late_sl_entry(signal: Signal) -> bool:
    """AlexG4+ late fills include ghost metadata in the pattern."""
    return "|g:" in signal.pattern


def _confirmation_signal_from_pattern(pattern: str) -> str:
    parts = pattern.split("|")
    if not parts:
        return "unknown"
    if parts[0] in ("alexg2",) and len(parts) >= 5:
        return parts[4]
    if parts[0] in ("alexg3", "alexg4", "alexg5", "alexg6") and len(parts) >= 7:
        return parts[6]
    return parts[-1] if parts[-1] else "unknown"


def _signal_entry(
    signal: Signal, index: int, candles: list[Candle]
) -> tuple[int, float, object] | None:
    """Execution bar/price for a signal (late AlexG fills at SL on the touch bar)."""
    if _is_late_sl_entry(signal):
        if index >= len(candles):
            return None
        candle = candles[index]
        return index, signal.price, candle.timestamp
    if index + 1 >= len(candles):
        return None
    next_candle = candles[index + 1]
    return index + 1, next_candle.open, next_candle.timestamp


def _build_confirmation_stats(trades: list[Trade]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, float]] = {}
    for t in trades:
        key = _confirmation_signal_from_pattern(t.pattern)
        if key not in grouped:
            grouped[key] = {"wins": 0, "losses": 0, "total": 0, "pnl": 0.0}
        grouped[key]["total"] += 1
        grouped[key]["pnl"] += t.pnl
        if t.pnl > 0:
            grouped[key]["wins"] += 1
        else:
            grouped[key]["losses"] += 1

    out: list[dict[str, Any]] = []
    for signal, g in grouped.items():
        total = int(g["total"])
        wins = int(g["wins"])
        losses = int(g["losses"])
        out.append(
            {
                "signal": signal,
                "wins": wins,
                "losses": losses,
                "total": total,
                "win_rate": (wins / total) if total else 0.0,
                "pnl": float(g["pnl"]),
            }
        )
    out.sort(key=lambda r: (r["win_rate"], r["pnl"]), reverse=True)
    return out


class BacktestEngine:
    def __init__(self, strategy: Strategy, config: BacktestConfig | None = None):
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self._costs: TradeCosts | None = None
        self._total_commission: float = 0.0
        self._progress_every = 5

    def _trade_costs(self, symbol: str) -> TradeCosts:
        pip = self.config.pip_size or infer_pip_size(symbol)
        return TradeCosts(
            spread_pips=self.config.spread_pips,
            slippage_pips=self.config.slippage_pips,
            commission_per_trade=self.config.commission_per_trade,
            pip_size=pip,
        )

    def _fill_entry(self, mid_price: float, side) -> float:
        assert self._costs is not None
        return apply_entry_fill(mid_price, side, self._costs)

    def _fill_exit(self, mid_price: float, side) -> float:
        assert self._costs is not None
        return apply_exit_fill(mid_price, side, self._costs)

    def _close_with_costs(
        self,
        portfolio: Portfolio,
        index: int,
        mid_price: float,
        timestamp: object,
        reason: str,
    ) -> None:
        trade = portfolio.open_trade
        if trade is None:
            return
        fill = self._fill_exit(mid_price, trade.side)
        portfolio.close_position(index, fill, timestamp, reason)
        commission = self.config.commission_per_trade
        if commission > 0 and portfolio.closed_trades:
            closed = portfolio.closed_trades[-1]
            closed.commission = commission
            portfolio.charge_commission(commission)
            self._total_commission += commission
        n = len(portfolio.closed_trades)
        if n > 0 and n % self._progress_every == 0:
            closed = portfolio.closed_trades[-1]
            wins = sum(1 for t in portfolio.closed_trades if t.pnl > 0)
            sign = "+" if closed.pnl >= 0 else ""
            print(
                f"[backtest] trades={n} | last {closed.side.value} "
                f"PnL {sign}{closed.pnl:.2f} [{closed.exit_reason}] | "
                f"WR {wins}/{n} ({wins / n:.1%}) | cash ${portfolio.cash:,.2f}",
                flush=True,
                file=sys.stderr,
            )

    def run(
        self,
        candles: list[Candle],
        symbol: str = "UNKNOWN",
        timeframe: str = "1d",
        mtf: MultiTimeframeContext | None = None,
    ) -> BacktestResult:
        self._costs = self._trade_costs(symbol)
        self._total_commission = 0.0
        portfolio = Portfolio(
            initial_capital=self.config.initial_capital,
            position_size_pct=self.config.position_size_pct,
            leverage=self.config.leverage,
            maintenance_margin_ratio=self.config.maintenance_margin_ratio,
            size_mode=self.config.size_mode,
        )
        equity_curve: list[float] = [portfolio.equity]
        peak_equity = portfolio.equity
        max_dd = 0.0

        for i in range(len(candles)):
            if portfolio.liquidated:
                break

            candle = candles[i]

            # Gestionar liquidación / stop loss / take profit en la vela actual
            if portfolio.open_trade is not None:
                closed = self._check_exit_levels(portfolio, i, candle)
                eq_close, eq_worst = self._equity_snapshot(portfolio, candle)
                equity_curve.append(eq_close)
                peak_equity, max_dd = self._update_drawdown(
                    peak_equity, max_dd, eq_close, eq_worst
                )
                if closed or portfolio.liquidated:
                    continue

            signal = self.strategy.on_bar(i, candles, mtf)
            if signal is None:
                eq_close, eq_worst = self._equity_snapshot(portfolio, candle)
                equity_curve.append(eq_close)
                peak_equity, max_dd = self._update_drawdown(
                    peak_equity, max_dd, eq_close, eq_worst
                )
                continue

            self._handle_signal(portfolio, signal, i, candles)
            eq_close, eq_worst = self._equity_snapshot(portfolio, candle)
            equity_curve.append(eq_close)
            peak_equity, max_dd = self._update_drawdown(
                peak_equity, max_dd, eq_close, eq_worst
            )

        # Cerrar posición abierta al final del backtest
        if portfolio.open_trade is not None:
            last = candles[-1]
            self._close_with_costs(
                portfolio,
                len(candles) - 1,
                last.close,
                last.timestamp,
                "end_of_data",
            )
            equity_curve.append(portfolio.equity)

        return self._build_result(
            portfolio, equity_curve, max_dd, symbol, timeframe, mtf
        )

    def _equity_snapshot(
        self, portfolio: Portfolio, candle: Candle
    ) -> tuple[float, float]:
        eq_close = self._mark_equity(portfolio, candle)
        if portfolio.open_trade is None:
            return eq_close, eq_close
        eq_worst = portfolio.equity_at_adverse(candle.low, candle.high)
        return eq_close, eq_worst

    @staticmethod
    def _update_drawdown(
        peak: float, max_dd: float, eq_close: float, eq_worst: float
    ) -> tuple[float, float]:
        peak = max(peak, eq_close)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq_worst) / peak)
        return peak, max_dd

    def _effective_action(self, action: SignalAction) -> SignalAction:
        if not self.config.inversed or action == SignalAction.HOLD:
            return action
        if action == SignalAction.BUY:
            return SignalAction.SELL
        return SignalAction.BUY

    def _handle_signal(
        self,
        portfolio: Portfolio,
        signal: Signal,
        index: int,
        candles: list[Candle],
    ) -> None:
        entry = _signal_entry(signal, index, candles)
        if entry is None:
            return
        exec_index, mid_price, exec_timestamp = entry
        action = self._effective_action(signal.action)

        if portfolio.open_trade is not None and self.config.close_on_opposite_signal:
            current = portfolio.open_trade
            is_opposite = (
                current.side.value == "long" and action == SignalAction.SELL
            ) or (
                current.side.value == "short" and action == SignalAction.BUY
            )
            if is_opposite:
                self._close_with_costs(
                    portfolio,
                    exec_index,
                    mid_price,
                    exec_timestamp,
                    "opposite_signal",
                )

        if portfolio.can_open() and action != SignalAction.HOLD:
            side = PositionSide.LONG if action == SignalAction.BUY else PositionSide.SHORT
            exec_price = self._fill_entry(mid_price, side)
            stop_loss = signal.stop_loss
            take_profit = signal.take_profit
            rr = (
                rr_from_winrate(portfolio.win_rate, self.config.true_sl_rr)
                * self.config.rr_factor
            )

            # Inverse flips fill side first. Mirror analysis SL/TP onto that
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
                    exec_price,
                    side,
                    self.config.leverage,
                    rr,
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

    def _mark_equity(self, portfolio: Portfolio, candle: Candle) -> float:
        if portfolio.open_trade is None:
            return portfolio.equity
        return portfolio.equity_at(candle.close)

    def _check_exit_levels(
        self, portfolio: Portfolio, index: int, candle: Candle
    ) -> bool:
        trade = portfolio.open_trade
        if trade is None:
            return False

        if self.config.size_mode == "margin":
            ms_price = portfolio.margin_stop_out_price()
            if ms_price is not None and self._hit_margin_stop(trade, candle, ms_price):
                self._close_with_costs(
                    portfolio, index, ms_price, candle.timestamp, "margin_stop"
                )
                return True
        else:
            liq_price = portfolio.liquidation_price()
            adverse = portfolio.adverse_price(candle.low, candle.high)
            if liq_price is not None and portfolio.is_margin_call_at(adverse):
                self._close_with_costs(
                    portfolio, index, liq_price, candle.timestamp, "liquidation"
                )
                return True

        sl = self.config.stop_loss_pct
        tp = self.config.take_profit_pct

        if trade.side.value == "long":
            sl_price = trade.stop_loss
            tp_price = trade.take_profit
            if sl_price is None and sl:
                sl_price = trade.entry_price * (1 - sl)
            if tp_price is None and tp:
                tp_price = trade.entry_price * (1 + tp)

            if sl_price and candle.low <= sl_price:
                self._close_with_costs(
                    portfolio, index, sl_price, candle.timestamp, "stop_loss"
                )
                return True
            if tp_price and candle.high >= tp_price:
                self._close_with_costs(
                    portfolio, index, tp_price, candle.timestamp, "take_profit"
                )
                return True
        else:
            sl_price = trade.stop_loss
            tp_price = trade.take_profit
            if sl_price is None and sl:
                sl_price = trade.entry_price * (1 + sl)
            if tp_price is None and tp:
                tp_price = trade.entry_price * (1 - tp)

            if sl_price and candle.high >= sl_price:
                self._close_with_costs(
                    portfolio, index, sl_price, candle.timestamp, "stop_loss"
                )
                return True
            if tp_price and candle.low <= tp_price:
                self._close_with_costs(
                    portfolio, index, tp_price, candle.timestamp, "take_profit"
                )
                return True

        return False

    def _hit_margin_stop(
        self, trade: Trade, candle: Candle, ms_price: float
    ) -> bool:
        if trade.side == PositionSide.LONG:
            return candle.low <= ms_price
        return candle.high >= ms_price

    def _build_result(
        self,
        portfolio: Portfolio,
        equity_curve: list[float],
        max_dd: float,
        symbol: str,
        timeframe: str,
        mtf: MultiTimeframeContext | None = None,
    ) -> BacktestResult:
        trades = portfolio.closed_trades
        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]

        initial = self.config.initial_capital
        final = max(0.0, portfolio.cash)
        total_return = (final - initial) / initial if initial else 0.0
        win_rate = len(winners) / len(trades) if trades else 0.0

        avg_win = sum(t.pnl for t in winners) / len(winners) if winners else 0.0
        avg_loss = sum(t.pnl for t in losers) / len(losers) if losers else 0.0
        gross_win = sum(t.pnl for t in winners)
        gross_loss = abs(sum(t.pnl for t in losers))
        profit_factor = gross_win / gross_loss if gross_loss > 0 else 0.0

        planned_rrs: list[float] = []
        for t in trades:
            if t.stop_loss is None or t.take_profit is None or t.entry_price <= 0:
                continue
            risk = abs(t.entry_price - t.stop_loss)
            reward = abs(t.take_profit - t.entry_price)
            if risk > 0:
                planned_rrs.append(reward / risk)
        avg_planned_rr = sum(planned_rrs) / len(planned_rrs) if planned_rrs else 0.0
        confirmation_stats = _build_confirmation_stats(trades)

        return BacktestResult(
            strategy_name=self.strategy.name,
            symbol=symbol,
            timeframe=timeframe,
            filter_intervals=mtf.filter_intervals if mtf else None,
            config=self.config,
            trades=trades,
            final_equity=final,
            total_return_pct=total_return,
            win_rate=win_rate,
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            max_drawdown_pct=max_dd,
            total_commission=self._total_commission,
            equity_curve=equity_curve,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            avg_planned_rr=avg_planned_rr,
            confirmation_stats=confirmation_stats,
        )
