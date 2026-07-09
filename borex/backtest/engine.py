from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from borex.backtest.costs import CostModel
from borex.backtest.execution import ExitReason, adverse_price, check_exits
from borex.backtest.fills import PendingEntry, fill_signal_price, schedule_entry
from borex.backtest.metrics import BacktestMetrics, compute_metrics
from borex.backtest.portfolio import Portfolio, Trade
from borex.backtest.reconcile import finalize_execution_stats
from borex.backtest.risk import RiskTracker
from borex.config import BacktestConfig
from borex.data.mtf import build_htf_alignment, filter_df_range, tf_minutes
from borex.data.store import load_ohlcv
from borex.models.signal import Candle, SignalAction
from borex.strategy.base import Strategy, StrategyContext, candles_from_df
from borex.strategy.indicators import pd_day
from borex.strategy.mtf import MtfContext, is_mtf_strategy


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    timeframe: str
    params: dict
    metrics: BacktestMetrics
    equity_curve: list[float] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    split: str = "full"
    mtf_bias: list[str] = field(default_factory=list)
    risk_stats: dict = field(default_factory=dict)
    execution_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "params": self.params,
            "split": self.split,
            "metrics": self.metrics.to_dict(),
        }
        if self.mtf_bias:
            out["mtf_bias"] = self.mtf_bias
        if self.risk_stats:
            out["risk_stats"] = self.risk_stats
        if self.execution_stats:
            out["execution_stats"] = self.execution_stats
        return out


@dataclass
class EngineState:
    portfolio: Portfolio
    risk: RiskTracker
    pending: list[PendingEntry] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    last_index: int = -1


_BARS_PER_YEAR = {
    "1m": 365 * 24 * 60,
    "15m": 365 * 24 * 4,
    "30m": 365 * 24 * 2,
    "1h": 365 * 24,
    "4h": 365 * 6,
    "1d": 365,
    "1wk": 52,
}


def _validate_mtf(entry_tf: str, bias_tfs: tuple[str, ...]) -> None:
    entry_m = tf_minutes(entry_tf)
    for tf in bias_tfs:
        if tf_minutes(tf) <= entry_m:
            raise ValueError(
                f"MTF bias timeframe {tf} must be higher than entry timeframe {entry_tf}"
            )


class BacktestEngine:
    DecisionHandler = Callable[[dict], None]

    def __init__(
        self,
        config: BacktestConfig | None = None,
        *,
        decision_handler: DecisionHandler | None = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.costs = CostModel.from_config(self.config)
        self.decision_handler = decision_handler
        self._min_log_bar: int = 0

    def set_decision_context(
        self,
        handler: DecisionHandler | None,
        *,
        min_bar_index: int = 0,
    ) -> None:
        self.decision_handler = handler
        self._min_log_bar = min_bar_index

    def _log_decision(self, bar_index: int, bar_ts: object, **fields: object) -> None:
        if not self.decision_handler or bar_index < self._min_log_bar:
            return
        self.decision_handler(
            {
                "bar_index": bar_index,
                "bar_ts": str(bar_ts),
                **fields,
            }
        )

    def _needs_pending_fills(self) -> bool:
        return self.config.fill_mode == "next_open" or self.config.entry_delay_bars > 0

    def _execute_pending(
        self,
        i: int,
        c: Candle,
        candles: list[Candle],
        portfolio: Portfolio,
        risk: RiskTracker,
        pending: list[PendingEntry],
        symbol: str,
    ) -> None:
        due = [p for p in pending if p.execute_index == i]
        for p in due:
            if not risk.can_enter():
                self._log_decision(
                    i, c.timestamp,
                    event_type="block",
                    action=p.action.value,
                    reason=risk.stats.halt_reason or "halted",
                )
                continue
            if not risk.allows_correlation(portfolio.open_trades, p.symbol, p.action):
                self._log_decision(
                    i, c.timestamp,
                    event_type="block",
                    action=p.action.value,
                    reason="correlation_limit",
                )
                continue
            signal_price = fill_signal_price(
                i,
                config=self.config,
                open_price=c.open,
                close_price=c.close,
            )
            if self.config.fill_mode == "next_open":
                signal_price = c.open
            else:
                signal_price = c.close
            side = "long" if p.action == SignalAction.BUY else "short"
            entry_px = self.costs.entry_price(
                side, signal_price, candles=candles, index=i
            )
            opened = portfolio.open_position(
                p.action,
                i,
                entry_px,
                c.timestamp,
                p.tag,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                size_pct=p.size_pct,
                symbol=p.symbol,
                signal_entry_price=signal_price,
            )
            if opened:
                self.costs.charge_open(
                    portfolio,
                    opened,
                    signal_price=signal_price,
                    candles=candles,
                    index=i,
                )
                self._log_decision(
                    i, c.timestamp,
                    event_type="entry",
                    action=p.action.value,
                    reason="pending_fill",
                    detail={"trade_id": opened.id, "price": opened.entry_price, "tag": p.tag},
                )
        pending[:] = [p for p in pending if p.execute_index > i]

    def _process_bar(
        self,
        i: int,
        candles: list[Candle],
        *,
        portfolio: Portfolio,
        risk: RiskTracker,
        pending: list[PendingEntry],
        strategy: Strategy,
        symbol: str,
        timeframe: str,
        bias_tfs: tuple[str, ...],
        htf_candles: dict[str, list],
        align: dict[str, list[int]],
    ) -> None:
        c = candles[i]
        day = pd_day(c.timestamp)
        eq = portfolio.equity
        was_halted = risk.stats.halted
        risk.on_bar(eq, day)
        if not was_halted and risk.stats.halted:
            self._log_decision(
                i, c.timestamp,
                event_type="halt",
                reason=risk.stats.halt_reason,
            )
        portfolio.halted = risk.stats.halted

        self._execute_pending(i, c, candles, portfolio, risk, pending, symbol)

        exits = check_exits(portfolio.open_trades, i, c.low, c.high)
        trade_map = {t.id: t for t in portfolio.open_trades}
        for ev in exits:
            trade = trade_map.get(ev.trade_id)
            if trade is None:
                continue
            px = self.costs.charge_close(
                portfolio, trade, ev.price, candles=candles, index=i
            )
            portfolio.close_position(trade, i, px, c.timestamp, reason=ev.reason.value)
            self._log_decision(
                i, c.timestamp,
                event_type="exit",
                action=trade.side.value,
                reason=ev.reason.value,
                detail={"trade_id": trade.id, "price": px, "pnl": trade.pnl},
            )

        for trade in list(portfolio.open_trades):
            if trade.entry_index == i:
                continue
            adv = adverse_price(trade, c.low, c.high)
            eq = portfolio.equity_at({t.id: adverse_price(t, c.low, c.high) for t in portfolio.open_trades})
            threshold = trade.margin * portfolio.maintenance_margin_ratio
            if eq <= threshold:
                px = self.costs.charge_close(
                    portfolio, trade, adv, candles=candles, index=i
                )
                portfolio.close_position(
                    trade, i, px, c.timestamp, reason=ExitReason.LIQUIDATION.value
                )
                self._log_decision(
                    i, c.timestamp,
                    event_type="exit",
                    action=trade.side.value,
                    reason=ExitReason.LIQUIDATION.value,
                    detail={"trade_id": trade.id, "price": px},
                )

        if portfolio.liquidated:
            return

        mtf_ctx = None
        if bias_tfs:
            mtf_ctx = MtfContext(i, candles, htf_candles, align)

        ctx = StrategyContext(
            symbol=symbol,
            timeframe=timeframe,
            open_trades=len(portfolio.open_trades),
            mtf=mtf_ctx,
        )
        signals = strategy.on_bar(i, candles, ctx)
        for sig in signals:
            self._log_decision(
                i, c.timestamp,
                event_type="signal",
                action=sig.action.value,
                reason=sig.tag or "strategy",
                detail={
                    "stop_loss": sig.stop_loss,
                    "take_profit": sig.take_profit,
                    "size_pct": sig.size_pct,
                },
            )
            if sig.action == SignalAction.CLOSE:
                for trade in list(portfolio.open_trades):
                    px = self.costs.charge_close(
                        portfolio, trade, c.close, candles=candles, index=i
                    )
                    portfolio.close_position(
                        trade, i, px, c.timestamp, reason=ExitReason.SIGNAL.value
                    )
                    self._log_decision(
                        i, c.timestamp,
                        event_type="exit",
                        action=trade.side.value,
                        reason=ExitReason.SIGNAL.value,
                        detail={"trade_id": trade.id, "price": px, "pnl": trade.pnl},
                    )
                continue

            if not risk.can_enter():
                self._log_decision(
                    i, c.timestamp,
                    event_type="block",
                    action=sig.action.value,
                    reason=risk.stats.halt_reason or "halted",
                )
                continue
            if not risk.allows_correlation(portfolio.open_trades, symbol, sig.action):
                self._log_decision(
                    i, c.timestamp,
                    event_type="block",
                    action=sig.action.value,
                    reason="correlation_limit",
                )
                continue

            size_pct = risk.compute_size_pct(
                sig, candles, i, portfolio.equity, portfolio.closed_trades
            )

            if self._needs_pending_fills():
                entry = schedule_entry(
                    i,
                    sig,
                    symbol=symbol,
                    size_pct=size_pct,
                    signal_price=c.close,
                    config=self.config,
                    bar_count=len(candles),
                )
                if entry:
                    pending.append(entry)
                    self._log_decision(
                        i, c.timestamp,
                        event_type="pending",
                        action=sig.action.value,
                        reason="scheduled",
                        detail={"execute_index": entry.execute_index},
                    )
                continue

            signal_price = c.close
            side = "long" if sig.action == SignalAction.BUY else "short"
            entry_px = self.costs.entry_price(
                side, signal_price, candles=candles, index=i
            )
            opened = portfolio.open_position(
                sig.action,
                i,
                entry_px,
                c.timestamp,
                sig.tag,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                size_pct=size_pct,
                symbol=symbol,
                signal_entry_price=signal_price,
            )
            if opened:
                self.costs.charge_open(
                    portfolio,
                    opened,
                    signal_price=signal_price,
                    candles=candles,
                    index=i,
                )
                self._log_decision(
                    i, c.timestamp,
                    event_type="entry",
                    action=sig.action.value,
                    reason=sig.tag or "fill",
                    detail={"trade_id": opened.id, "price": opened.entry_price},
                )

    def run(
        self,
        strategy: Strategy,
        df: pd.DataFrame,
        *,
        symbol: str = "",
        timeframe: str = "1h",
        split: str = "full",
        htf_dfs: dict[str, pd.DataFrame] | None = None,
        state: EngineState | None = None,
        start_index: int | None = None,
        end_index: int | None = None,
    ) -> BacktestResult:
        candles = candles_from_df(df)
        bias_tfs: tuple[str, ...] = ()
        htf_candles: dict[str, list] = {}
        align: dict[str, list[int]] = {}

        if is_mtf_strategy(type(strategy)):
            spec = strategy.mtf_spec()
            bias_tfs = spec.bias_timeframes
            _validate_mtf(timeframe, bias_tfs)
            for tf in bias_tfs:
                hdf = (htf_dfs or {}).get(tf)
                if hdf is None:
                    hdf = load_ohlcv(symbol, tf)
                htf_candles[tf] = candles_from_df(hdf)
                align[tf] = build_htf_alignment(candles, htf_candles[tf], tf)

        warmup = strategy.warmup_bars()
        if state:
            portfolio = state.portfolio
            risk = state.risk
            pending = state.pending
            equity_curve = state.equity_curve
            if start_index is not None:
                bar_start = start_index
            elif state.last_index < 0:
                bar_start = warmup
            else:
                bar_start = state.last_index + 1
        else:
            portfolio = Portfolio(
                initial_capital=self.config.initial_capital,
                leverage=self.config.leverage,
                position_size_pct=self.config.position_size_pct,
                max_positions=self.config.max_positions,
                maintenance_margin_ratio=self.config.maintenance_margin_ratio,
            )
            risk = RiskTracker(self.config)
            risk.peak_equity = self.config.initial_capital
            risk.day_start_equity = self.config.initial_capital
            pending = []
            equity_curve = []
            bar_start = start_index if start_index is not None else warmup

        bar_end = end_index if end_index is not None else len(candles)

        for i in range(bar_start, bar_end):
            self._process_bar(
                i,
                candles,
                portfolio=portfolio,
                risk=risk,
                pending=pending,
                strategy=strategy,
                symbol=symbol,
                timeframe=timeframe,
                bias_tfs=bias_tfs,
                htf_candles=htf_candles,
                align=align,
            )
            if portfolio.liquidated:
                equity_curve.append(0.0)
                break
            equity_curve.append(portfolio.equity)

        if state:
            state.last_index = bar_end - 1 if bar_end > 0 else state.last_index
            state.pending = pending
            state.equity_curve = equity_curve

        bpy = _BARS_PER_YEAR.get(timeframe, 365 * 24)
        bars_processed = max(0, (bar_end - bar_start) if not portfolio.liquidated else len(equity_curve))
        metrics = compute_metrics(
            portfolio,
            equity_curve,
            bars_processed or len(candles) - warmup,
            bars_per_year=bpy,
        )
        exec_stats = finalize_execution_stats(portfolio, self.costs.stats)

        return BacktestResult(
            strategy=strategy.name,
            symbol=symbol,
            timeframe=timeframe,
            params=strategy.params,
            metrics=metrics,
            equity_curve=equity_curve,
            trades=portfolio.closed_trades,
            split=split,
            mtf_bias=list(bias_tfs),
            risk_stats=risk.stats.to_dict(),
            execution_stats=exec_stats.to_dict(),
        )

    def run_incremental(
        self,
        strategy: Strategy,
        df: pd.DataFrame,
        state: EngineState,
        *,
        symbol: str,
        timeframe: str,
        htf_dfs: dict[str, pd.DataFrame] | None = None,
    ) -> tuple[BacktestResult, EngineState]:
        """Process only bars after state.last_index (for paper trading ticks)."""
        candles = candles_from_df(df)
        start = state.last_index + 1
        if start >= len(candles):
            bpy = _BARS_PER_YEAR.get(timeframe, 365 * 24)
            metrics = compute_metrics(
                state.portfolio,
                state.equity_curve,
                max(1, len(state.equity_curve)),
                bars_per_year=bpy,
            )
            exec_stats = finalize_execution_stats(state.portfolio, self.costs.stats)
            result = BacktestResult(
                strategy=strategy.name,
                symbol=symbol,
                timeframe=timeframe,
                params=strategy.params,
                metrics=metrics,
                equity_curve=state.equity_curve,
                trades=state.portfolio.closed_trades,
                split="paper",
                risk_stats=state.risk.stats.to_dict(),
                execution_stats=exec_stats.to_dict(),
            )
            return result, state

        result = self.run(
            strategy,
            df,
            symbol=symbol,
            timeframe=timeframe,
            split="paper",
            htf_dfs=htf_dfs,
            state=state,
            start_index=start,
        )
        state.last_index = len(candles) - 1
        return result, state

    def warmup_state(
        self,
        strategy: Strategy,
        df: pd.DataFrame,
        *,
        symbol: str,
        timeframe: str,
        htf_dfs: dict[str, pd.DataFrame] | None = None,
    ) -> tuple[BacktestResult, EngineState]:
        """Run full history and return engine state for incremental paper ticks."""
        state = self.initial_state()
        candles = candles_from_df(df)
        if len(candles) <= strategy.warmup_bars():
            return self.run(strategy, df, symbol=symbol, timeframe=timeframe, htf_dfs=htf_dfs), state
        result = self.run(
            strategy,
            df,
            symbol=symbol,
            timeframe=timeframe,
            htf_dfs=htf_dfs,
            state=state,
            end_index=len(candles),
        )
        state.last_index = len(candles) - 1
        return result, state

    def initial_state(self) -> EngineState:
        portfolio = Portfolio(
            initial_capital=self.config.initial_capital,
            leverage=self.config.leverage,
            position_size_pct=self.config.position_size_pct,
            max_positions=self.config.max_positions,
            maintenance_margin_ratio=self.config.maintenance_margin_ratio,
        )
        risk = RiskTracker(self.config)
        risk.peak_equity = self.config.initial_capital
        risk.day_start_equity = self.config.initial_capital
        return EngineState(portfolio=portfolio, risk=risk)
