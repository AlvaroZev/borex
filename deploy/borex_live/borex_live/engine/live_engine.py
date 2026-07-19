from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from borex.alexg.multi_market import (
    MultiMarketContext,
    align_symbols_to_timeline,
    pick_master_symbol,
)
from borex.backtest.engine import BacktestConfig, _signal_entry
from borex.backtest.margin_stops import margin_stop_out_prices, rr_from_winrate
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.backtest.portfolio import PositionSide
from borex.models.candle import Candle, Signal, SignalAction

from borex_live.config import LiveServiceConfig
from borex_live.entry_mode import EntryMode
from borex_live.execution.router import ExecutionRouter, read_pending_snapshot
from borex_live.store.repository import StateRepository


@dataclass
class LiveStepResult:
    master_index: int
    signals: list[tuple[str, Signal]]
    exits: list[tuple[str, str, float]]


class LiveEngine:
    """
    Incremental wrapper around borex MultiMarketEngine decision logic.
    Processes one master bar at a time; persistence handled externally.
    """

    def __init__(
        self,
        strategy: Any,
        cfg: LiveServiceConfig,
        repo: StateRepository,
        router: ExecutionRouter,
        entry_mode: EntryMode,
    ) -> None:
        self.strategy = strategy
        self.cfg = cfg
        self.repo = repo
        self.router = router
        self.entry_mode = entry_mode
        self.bt_config = BacktestConfig(
            initial_capital=cfg.capital,
            leverage=cfg.leverage,
            position_size_pct=cfg.position_size_pct,
            size_mode="margin",
            true_sl=True,
            true_sl_rr=cfg.min_rr,
            rr_factor=cfg.rr_factor,
            stop_loss_pct=None,
            take_profit_pct=None,
        )
        self._mm = MultiMarketEngine(
            strategy=strategy,
            config=self.bt_config,
            max_positions=cfg.max_positions,
        )

    def _cash(self) -> float:
        pf = self.repo.get_portfolio(self.cfg.capital)
        return float(pf.cash)

    def _compute_sltp(
        self,
        signal: Signal,
        exec_price: float,
        side: PositionSide,
    ) -> tuple[float, float, float]:
        wr = self.repo.win_rate()
        rr = rr_from_winrate(wr, self.bt_config.true_sl_rr) * self.bt_config.rr_factor
        sl, tp = margin_stop_out_prices(exec_price, side, self.bt_config.leverage, rr)
        return sl, tp, rr

    def _margin_for_entry(self) -> float:
        return self._cash() * self.cfg.position_size_pct

    def step_master_bar(
        self,
        master_index: int,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
    ) -> LiveStepResult:
        master_candles = candles_by_symbol[master_symbol]
        symbols = list(candles_by_symbol.keys())
        ts_maps = align_symbols_to_timeline(master_candles, candles_by_symbol)
        ctx = MultiMarketContext.at_master_bar(
            master_index,
            master_candles,
            candles_by_symbol,
            ts_maps,
            strength_lookback=self.strategy.strength_lookback,
            min_currency_edge=self.strategy.min_currency_edge,
            min_confirming_pairs=self.strategy.min_confirming_pairs,
        )

        exits: list[tuple[str, str, float]] = []
        open_db = {t.symbol: t for t in self.repo.open_trades()}

        for sym, idx in ctx.indices.items():
            if sym not in open_db:
                continue
            candle = candles_by_symbol[sym][idx]
            trade = open_db[sym]
            if self._mm._check_exit_live(sym, candle, trade, self.repo):
                exits.append((sym, "closed", candle.close))

        candidates: list[tuple[str, Signal, int]] = []
        for sym in symbols:
            idx = ctx.indices.get(sym)
            if idx is None or idx < self.strategy.min_bars:
                continue
            if sym in open_db:
                continue
            before = read_pending_snapshot(self.strategy)
            self.strategy.set_context(sym, ctx)
            signal = self.strategy.on_bar(idx, candles_by_symbol[sym], None)
            after = read_pending_snapshot(self.strategy)
            self.router.sync_ghost_pending_orders(before, after)

            if signal is None:
                continue
            candidates.append((sym, signal, idx))

        candidates.sort(key=lambda x: x[1].score, reverse=True)
        fired: list[tuple[str, Signal]] = []
        open_count = len(self.repo.open_trades())
        for sym, signal, idx in candidates:
            if open_count >= self.cfg.max_positions:
                break
            if sym in {t.symbol for t in self.repo.open_trades()}:
                continue
            self._process_entry(sym, signal, idx, candles_by_symbol[sym])
            fired.append((sym, signal))
            open_count += 1

        return LiveStepResult(master_index=master_index, signals=fired, exits=exits)

    def _process_entry(
        self,
        symbol: str,
        signal: Signal,
        index: int,
        candles: list[Candle],
    ) -> None:
        entry = _signal_entry(signal, index, candles)
        if entry is None:
            return
        _, mid_price, _ = entry
        side = (
            PositionSide.LONG
            if signal.action == SignalAction.BUY
            else PositionSide.SHORT
        )
        exec_price = mid_price
        sl, tp, rr = self._compute_sltp(signal, exec_price, side)
        margin = self._margin_for_entry()
        expected_loss = margin
        expected_win = margin * rr

        if self.entry_mode == EntryMode.IMMEDIATE:
            before = {t.symbol for t in self.repo.open_trades()}
            ticket = self.router.handle_immediate_signal(symbol, signal, sl, tp)
            self.router.handle_entry_fill(
                symbol,
                signal,
                sl,
                tp,
                margin=margin,
                rr_used=rr,
                expected_win=expected_win,
                expected_loss=expected_loss,
                mt5_ticket=ticket,
            )
            after = {t.symbol for t in self.repo.open_trades()}
            if symbol not in before and symbol in after:
                self.repo.set_cash(self._cash() - margin)
            return

        # GHOST: broker pending should already be working at strategy SL.
        # Prefer strategy structural stops from the late-entry signal.
        before = {t.symbol for t in self.repo.open_trades()}
        ghost_sl = float(signal.stop_loss) if signal.stop_loss is not None else sl
        ghost_tp = float(signal.take_profit) if signal.take_profit is not None else tp
        self.router.handle_entry_fill(
            symbol,
            signal,
            ghost_sl,
            ghost_tp,
            margin=margin,
            rr_used=rr,
            expected_win=expected_win,
            expected_loss=expected_loss,
        )
        after = {t.symbol for t in self.repo.open_trades()}
        if symbol not in before and symbol in after:
            self.repo.set_cash(self._cash() - margin)


# Monkey-patch helper for live exit checks against DB trades
def _check_exit_live(
    engine: MultiMarketEngine,
    symbol: str,
    candle: Candle,
    db_trade,
    repo: StateRepository,
) -> bool:
    side = PositionSide.LONG if db_trade.side == "buy" else PositionSide.SHORT
    sl, tp = db_trade.stop_loss, db_trade.take_profit
    exit_price = None
    reason = None
    if side == PositionSide.LONG:
        if sl and candle.low <= sl:
            exit_price, reason = sl, "stop_loss"
        elif tp and candle.high >= tp:
            exit_price, reason = tp, "take_profit"
    else:
        if sl and candle.high >= sl:
            exit_price, reason = sl, "stop_loss"
        elif tp and candle.low <= tp:
            exit_price, reason = tp, "take_profit"
    if exit_price is None:
        return False
    pnl = db_trade.expected_loss_usd * -1 if reason == "stop_loss" else db_trade.expected_win_usd
    repo.close_live_trade(
        int(db_trade.id),
        exit_price=exit_price,
        exit_time=str(candle.timestamp),
        exit_reason=reason or "unknown",
        pnl=pnl,
    )
    pf = repo.get_portfolio(0)
    repo.set_cash(pf.cash + db_trade.margin + pnl)
    repo.log_event("trade_closed", f"{symbol} {reason} pnl={pnl:.2f}")
    return True


MultiMarketEngine._check_exit_live = _check_exit_live  # type: ignore[attr-defined]
