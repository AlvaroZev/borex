from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from borex.alexg.multi_market import (
    MultiMarketContext,
    align_symbols_to_timeline,
    pick_master_symbol,
)
from borex.backtest.engine import BacktestConfig, _is_late_sl_entry, _signal_entry
from borex.backtest.margin_stops import margin_stop_out_prices, resolve_rr
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
            rr_mode=getattr(cfg, "rr_mode", "dynamic") or "dynamic",
            rr_factor=cfg.rr_factor,
            rr_min=float(getattr(cfg, "rr_min", 0.0) or 0.0),
            rr_max=float(getattr(cfg, "rr_max", 0.0) or 0.0),
            stop_loss_pct=None,
            take_profit_pct=None,
            commission_per_lot=float(getattr(cfg, "commission_per_lot", 0.0) or 0.0),
            min_commission_per_side=float(getattr(cfg, "min_commission_per_side", 0.04) or 0.04),
            lot_notional=float(getattr(cfg, "lot_notional", 100_000.0) or 100_000.0),
            risk_include_commission=bool(getattr(cfg, "risk_include_commission", True)),
            winrate_min_trades=int(getattr(cfg, "winrate_min_trades", 20) or 20),
            commission_at_entry=bool(getattr(cfg, "commission_at_entry", False)),
            force_flat_friday=bool(getattr(cfg, "force_flat_friday", False)),
            force_flat_daily=bool(getattr(cfg, "force_flat_daily", False)),
            force_flat_utc_hour=int(getattr(cfg, "force_flat_utc_hour", 19) or 19),
            force_flat_friday_from_hour=int(getattr(cfg, "force_flat_friday_from_hour", 19) or 19),
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
        closed_n = len(self.repo.closed_trades())
        rr = resolve_rr(
            rr_mode=self.bt_config.rr_mode,
            fixed_rr=self.bt_config.true_sl_rr,
            winrate=wr,
            rr_factor=self.bt_config.rr_factor,
            closed_trades=closed_n,
            winrate_min_trades=self.bt_config.winrate_min_trades,
            rr_min=getattr(self.bt_config, "rr_min", 0.0) or 0.0,
            rr_max=getattr(self.bt_config, "rr_max", 0.0) or 0.0,
        )
        sl, tp = margin_stop_out_prices(exec_price, side, self.bt_config.leverage, rr)
        return sl, tp, rr

    def _margin_for_entry(self) -> float:
        """Posted margin so SL price loss (+ commission) ≈ balance × position_size_pct.

        Prefer MT5 balance over equity so open floating PnL does not change risk.
        """
        bal = 0.0
        mt5 = getattr(getattr(self, "router", None), "mt5", None)
        if mt5 is not None and getattr(mt5, "connected", False) and not getattr(mt5, "dry_run", True):
            try:
                bal = float(mt5.account_balance() or 0.0)
                if bal <= 0:
                    bal = float(mt5.account_equity() or 0.0)
            except Exception:
                bal = 0.0
        if bal <= 0:
            bal = self._cash()
        risk = bal * self.cfg.position_size_pct
        if (
            self.bt_config.risk_include_commission
            and self.bt_config.commission_per_lot > 0
            and self.bt_config.leverage > 0
        ):
            from borex.backtest.costs import margin_for_risk_net_commission

            return margin_for_risk_net_commission(
                risk,
                self.bt_config.leverage,
                commission_per_lot=self.bt_config.commission_per_lot,
                lot_notional=self.bt_config.lot_notional,
                min_commission_per_side=self.bt_config.min_commission_per_side,
            )
        return risk

    def step_master_bar(
        self,
        master_index: int,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
        *,
        allow_broker_orders: bool = True,
        sync_pending: bool = True,
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
            # same_bar_exit=False: do not evaluate SL/TP on the entry bar
            # (entry is booked after that H1 bar has already closed).
            if not allow_broker_orders:
                continue
            if (
                not self.cfg.same_bar_exit
                and _same_bar_timestamp(trade.entry_time, candle.timestamp)
            ):
                continue
            flat = self._try_force_flat(sym, candle, trade)
            if flat:
                exits.append(flat)
                continue
            if self._mm._check_exit_live(sym, candle, trade, self.repo):
                exits.append((sym, "closed", candle.close))

        candidates: list[tuple[str, Signal, int]] = []
        from borex.alexg.force_flat import blocks_new_entries

        for sym in symbols:
            idx = ctx.indices.get(sym)
            if idx is None or idx < self.strategy.min_bars:
                continue
            bar = candles_by_symbol[sym][idx]
            if blocks_new_entries(bar.timestamp, self.bt_config):
                continue
            before = read_pending_snapshot(self.strategy) if sync_pending else {}
            self.strategy.set_context(sym, ctx)
            signal = self.strategy.on_bar(idx, candles_by_symbol[sym], None)
            if sync_pending:
                after = read_pending_snapshot(self.strategy)
                # DB-only ghost queue / cancel — never places MT5 limits
                self.router.sync_ghost_pending_orders(before, after)

            if signal is None:
                continue
            candidates.append((sym, signal, idx))

        candidates.sort(key=lambda x: x[1].score, reverse=True)
        fired: list[tuple[str, Signal]] = []
        if not allow_broker_orders:
            return LiveStepResult(master_index=master_index, signals=fired, exits=exits)
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

    def _deduct_entry_cash(self, symbol: str, before: set[str], after: set[str], margin: float) -> None:
        if symbol not in before and symbol in after:
            self.repo.set_cash(self._cash() - margin)
            if self.bt_config.commission_at_entry:
                from borex.backtest.costs import commission_for_margin

                comm = commission_for_margin(
                    margin,
                    self.cfg.leverage,
                    commission_per_lot=self.bt_config.commission_per_lot,
                    lot_notional=self.bt_config.lot_notional,
                    min_commission_per_side=self.bt_config.min_commission_per_side,
                )
                if comm > 0:
                    self.repo.set_cash(self._cash() - comm)

    def _try_force_flat(
        self,
        symbol: str,
        candle: Candle,
        trade: Any,
    ) -> tuple[str, str, float] | None:
        from borex.alexg.force_flat import force_flat_reason

        reason = force_flat_reason(candle.timestamp, self.bt_config, trade=trade)
        if reason is None:
            return None
        side = _live_trade_side(trade)
        exit_price = float(candle.open)
        pnl = _mark_to_market_pnl(
            side,
            float(trade.entry_price),
            exit_price,
            float(trade.margin),
            self.cfg.leverage,
        )
        self.router.force_close_position(symbol, int(trade.mt5_ticket or 0))
        self.repo.close_live_trade(
            int(trade.id),
            exit_price=exit_price,
            exit_time=str(candle.timestamp),
            exit_reason=reason,
            pnl=pnl,
        )
        pf = self.repo.get_portfolio(0)
        self.repo.set_cash(pf.cash + float(trade.margin) + pnl)
        self.repo.log_event("trade_closed", f"{symbol} {reason} pnl={pnl:.2f}")
        return (symbol, reason, exit_price)

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
        # Ghost H1-close: fill at tagging-bar close (theory |fill:close|), then
        # true-SL from that price. Still send a market order (slippage vs close).
        exec_price = mid_price
        if (
            self.entry_mode == EntryMode.GHOST
            and _is_late_sl_entry(signal)
            and candles
        ):
            exec_price = float(candles[index].close)
        sl, tp, rr = self._compute_sltp(signal, exec_price, side)
        margin = self._margin_for_entry()
        expected_loss = margin
        expected_win = margin * rr

        if self.entry_mode == EntryMode.IMMEDIATE:
            before = {t.symbol for t in self.repo.open_trades()}
            ticket = self.router.handle_immediate_signal(
                symbol,
                signal,
                sl,
                tp,
                margin=margin,
                rr_used=rr,
            )
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
            self._deduct_entry_cash(symbol, before, after, margin)
            return

        # GHOST fill on closed H1: market enter with true_sl protective stops
        # (matches backtest true_sl + same_bar_exit=False entry timing).
        before = {t.symbol for t in self.repo.open_trades()}
        self.router.handle_entry_fill(
            symbol,
            signal,
            sl,
            tp,
            margin=margin,
            rr_used=rr,
            expected_win=expected_win,
            expected_loss=expected_loss,
        )
        after = {t.symbol for t in self.repo.open_trades()}
        self._deduct_entry_cash(symbol, before, after, margin)


def _mark_to_market_pnl(
    side: PositionSide,
    entry_price: float,
    exit_price: float,
    margin: float,
    leverage: float,
) -> float:
    if entry_price <= 0 or margin <= 0:
        return 0.0
    if side == PositionSide.LONG:
        pct = (exit_price - entry_price) / entry_price
    else:
        pct = (entry_price - exit_price) / entry_price
    raw = margin * pct * leverage
    return max(-margin, raw)


def _live_trade_side(db_trade) -> PositionSide:
    return PositionSide.LONG if db_trade.side == "buy" else PositionSide.SHORT


def _same_bar_timestamp(entry_time: object, candle_ts: object) -> bool:
    """True when trade.entry_time refers to this candle's open timestamp."""
    try:
        import pandas as pd

        a = pd.Timestamp(entry_time)
        b = pd.Timestamp(candle_ts)
        if a.tzinfo is None:
            a = a.tz_localize("UTC")
        else:
            a = a.tz_convert("UTC")
        if b.tzinfo is None:
            b = b.tz_localize("UTC")
        else:
            b = b.tz_convert("UTC")
        return a == b
    except Exception:
        return str(entry_time) == str(candle_ts)


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
