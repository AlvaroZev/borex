from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

import sys

from borex.alexg.multi_market import (
    MultiMarketContext,
    align_symbols_to_timeline,
    pick_master_symbol,
)
from borex.backtest.costs import infer_pip_size, TradeCosts, apply_entry_fill, apply_exit_fill
from borex.backtest.decision_replay import index_decisions_by_bar
from borex.backtest.engine import BacktestConfig, BacktestResult, mirror_sl_tp_for_inverse
from borex.backtest.engine import _build_confirmation_stats, _msl_delay_bars, _signal_entry
from borex.backtest.margin_stops import (
    margin_stop_out_prices,
    resolve_rr,
    tighten_sl_to_margin_stop,
    tp_from_sl_rr,
)
from borex.backtest.multi_portfolio import MultiMarketPortfolio
from borex.backtest.portfolio import PositionSide, Trade
from borex.models.candle import Candle, Signal, SignalAction

if TYPE_CHECKING:
    from borex.alexg.strategy3 import AlexG3Strategy
    from borex.alexg.strategy4 import AlexG4Strategy


def _as_utc(ts: object) -> datetime:
    if isinstance(ts, datetime):
        t = ts
    else:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _hour_start(ts: object) -> datetime:
    t = _as_utc(ts)
    return t.replace(minute=0, second=0, microsecond=0)


def _unix(ts: object) -> float:
    return _as_utc(ts).timestamp()


def index_ltf_bars(
    ltf_by_symbol: Mapping[str, list[Candle]] | None,
) -> dict[str, tuple[list[Candle], list[float]]]:
    """Per-symbol 1m (or other LTF) bars with a unix timeline for bisect."""
    out: dict[str, tuple[list[Candle], list[float]]] = {}
    for symbol, bars in (ltf_by_symbol or {}).items():
        if not bars:
            continue
        stamps = [_unix(c.timestamp) for c in bars]
        out[symbol] = (bars, stamps)
    return out


def ltf_bars_in_hour(
    indexed: tuple[list[Candle], list[float]],
    hour_ts: object,
) -> list[Candle]:
    """Closed 1m bars whose open is inside the H1 bar [hour, hour+1h)."""
    bars, stamps = indexed
    start = _hour_start(hour_ts).timestamp()
    end = start + 3600.0
    lo = bisect.bisect_left(stamps, start)
    hi = bisect.bisect_left(stamps, end)
    return bars[lo:hi]


def decision_from_signal(symbol: str, signal: Signal) -> dict[str, Any]:
    """Serialize a strategy signal for later exec-only replay."""
    try:
        unix = int(_as_utc(signal.timestamp).timestamp())
    except Exception:
        unix = 0
    action = signal.action.value if hasattr(signal.action, "value") else str(signal.action)
    return {
        "symbol": symbol,
        "index": int(signal.index),
        "action": action,
        "pattern": signal.pattern or "",
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "price": float(signal.price or 0.0),
        "time_unix": unix,
        "score": float(signal.score or 0.0),
    }


def _indices_at_master(
    master_candles: list[Candle],
    master_i: int,
    ts_maps: dict[str, dict[object, int]],
) -> dict[str, int]:
    """Resolve per-symbol bar indices without currency-strength work."""
    ts = master_candles[master_i].timestamp
    indices: dict[str, int] = {}
    for symbol, ts_map in ts_maps.items():
        idx = ts_map.get(ts)
        if idx is not None:
            indices[symbol] = idx
    return indices


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
        by_sym = getattr(self.config, "spread_pips_by_symbol", None) or {}
        spread = float(by_sym.get(symbol, self.config.spread_pips) or 0.0)
        return TradeCosts(
            spread_pips=spread,
            slippage_pips=self.config.slippage_pips,
            commission_per_trade=self.config.commission_per_trade,
            pip_size=pip,
        )

    def run(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        timeframe: str = "1h",
        master_symbol: str | None = None,
        *,
        same_bar_exit: bool = False,
        decisions: list[Mapping[str, Any]] | None = None,
        start_master_i: int | None = None,
        ltf_by_symbol: Mapping[str, list[Candle]] | None = None,
        collect_decisions: list[dict[str, Any]] | None = None,
    ) -> MultiMarketBacktestResult:
        """
        Run multi-market backtest.

        same_bar_exit: if True, check protective SL/TP on the same bar a new
        fill opens (stricter / more live-like for ghost fills). Default False
        matches the high-compounding backtest path.

        start_master_i: first master-timeline bar to trade. Per-symbol warmup
        stays ``strategy.min_bars`` so a short exotic is not silenced just
        because EURUSD has 6k hours of history.

        decisions: optional saved analysis rows (from --save-analysis /
        --load-analysis). When provided, strategy.on_bar is skipped and entries
        are replayed from the cache — use for PnL-only param sweeps
        (commission, leverage, rr-factor, same-bar, capital, …).

        ltf_by_symbol: optional 1m (or other) path for SL/TP. When present,
        each H1 hour walks those bars in time order so the first touch wins
        (broker-like intra-hour stops). Missing LTF falls back to the H1 wick.

        collect_decisions: if a list is passed, every signal from on_bar is
        appended (scan tape). Occupancy does not mute the scan; fills still
        respect one-position-per-pair / cash / max_positions.
        """
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
            commission_per_lot=self.config.commission_per_lot,
            commission_per_trade=self.config.commission_per_trade,
            min_commission_per_side=self.config.min_commission_per_side,
            lot_notional=self.config.lot_notional,
            risk_include_commission=self.config.risk_include_commission,
        )

        equity_curve: list[float] = [portfolio.equity]
        peak = portfolio.equity
        max_dd = 0.0
        warmup = int(self.strategy.min_bars)
        begin = warmup if start_master_i is None else max(warmup, int(start_master_i))
        replay = decisions is not None
        default_score = float(getattr(self.strategy, "min_rr", 0.0) or 0.0)
        by_bar = (
            index_decisions_by_bar(decisions, default_score=default_score)
            if replay
            else None
        )
        if replay:
            print(
                f"[alexg3] replaying {len(decisions):,} cached decisions "
                f"({len(by_bar):,} bar keys) — strategy scan skipped",
                flush=True,
                file=sys.stderr,
            )

        ltf_index = index_ltf_bars(ltf_by_symbol)

        for master_i in range(begin, len(master_candles)):
            if portfolio.liquidated:
                break

            if replay:
                # Cached decisions already embed currency/AOI filters — skip
                # strength recomputation on every master bar.
                indices = _indices_at_master(master_candles, master_i, ts_maps)
                ctx = None
            else:
                ctx = MultiMarketContext.at_master_bar(
                    master_i,
                    master_candles,
                    candles_by_symbol,
                    ts_maps,
                    strength_lookback=self.strategy.strength_lookback,
                    min_currency_edge=self.strategy.min_currency_edge,
                    min_confirming_pairs=self.strategy.min_confirming_pairs,
                )
                indices = ctx.indices

            prices: dict[str, float] = {}
            for sym, idx in indices.items():
                prices[sym] = candles_by_symbol[sym][idx].close

            for sym in list(portfolio.open_trades.keys()):
                idx = indices.get(sym)
                if idx is None:
                    continue
                candle = candles_by_symbol[sym][idx]
                prices[sym] = candle.close
                self._check_exit_path(portfolio, sym, idx, candle, ltf_index.get(sym))

            candidates: list[tuple[str, Signal, int]] = []
            if replay:
                assert by_bar is not None
                for sym, idx in indices.items():
                    if idx < warmup:
                        continue
                    for signal in by_bar.get((sym, idx), ()):
                        candidates.append((sym, signal, idx))
            else:
                assert ctx is not None
                for sym in symbols:
                    idx = indices.get(sym)
                    if idx is None or idx < warmup:
                        continue
                    self.strategy.set_context(sym, ctx)
                    signal = self.strategy.on_bar(idx, candles_by_symbol[sym], None)
                    if signal is not None:
                        candidates.append((sym, signal, idx))
                        if collect_decisions is not None:
                            collect_decisions.append(
                                decision_from_signal(sym, signal)
                            )

            candidates.sort(key=lambda x: x[1].score, reverse=True)
            opened: list[str] = []
            for sym, signal, idx in candidates:
                if portfolio.book_is_full():
                    break
                if not portfolio.can_open(sym):
                    continue
                self._open_signal(portfolio, sym, signal, idx, candles_by_symbol[sym])
                if same_bar_exit and sym in portfolio.open_trades:
                    opened.append(sym)

            # Optional: protective exits on the fill bar (ghost SL often tags here).
            if same_bar_exit:
                for sym in opened:
                    idx = indices.get(sym)
                    if idx is None:
                        continue
                    self._check_exit_path(
                        portfolio,
                        sym,
                        idx,
                        candles_by_symbol[sym][idx],
                        ltf_index.get(sym),
                    )

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
        from borex.alexg.force_flat import blocks_new_entries

        if blocks_new_entries(candles[index].timestamp, self.config):
            return

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
        rr = resolve_rr(
            rr_mode=self.config.rr_mode,
            fixed_rr=self.config.true_sl_rr,
            winrate=portfolio.win_rate,
            rr_factor=self.config.rr_factor,
            closed_trades=len(portfolio.closed_trades),
            winrate_min_trades=self.config.winrate_min_trades,
            rr_min=self.config.rr_min,
            rr_max=self.config.rr_max,
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

        delay = _msl_delay_bars(signal.pattern)
        sl_armed = (exec_index + delay) if delay > 0 else None
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
            sl_armed_from_index=sl_armed,
        )
        if self.config.commission_at_entry:
            opened = portfolio.get_trade(symbol)
            if opened is not None:
                from borex.backtest.costs import commission_for_margin

                comm = commission_for_margin(
                    opened.margin,
                    self.config.leverage,
                    commission_per_lot=self.config.commission_per_lot,
                    commission_per_trade=self.config.commission_per_trade,
                    lot_notional=self.config.lot_notional,
                    min_commission_per_side=self.config.min_commission_per_side,
                )
                if comm > 0:
                    opened.commission = comm
                    portfolio.charge_commission(comm)
                    self._total_commission += comm

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
        closed = portfolio.closed_trades[-1] if portfolio.closed_trades else None
        if closed is not None and not (
            self.config.commission_at_entry and closed.commission > 0
        ):
            from borex.backtest.costs import commission_for_margin

            commission = commission_for_margin(
                closed.margin,
                self.config.leverage,
                commission_per_lot=self.config.commission_per_lot,
                commission_per_trade=self.config.commission_per_trade,
                lot_notional=self.config.lot_notional,
                min_commission_per_side=self.config.min_commission_per_side,
            )
            if commission > 0:
                closed.commission = commission
                closed.pnl -= commission
                portfolio.charge_commission(commission)
                self._total_commission += commission
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

    def _check_exit_path(
        self,
        portfolio: MultiMarketPortfolio,
        symbol: str,
        index: int,
        h1_candle: Candle,
        ltf: tuple[list[Candle], list[float]] | None,
    ) -> bool:
        """Walk 1m bars in the H1 hour when available; else the H1 wick."""
        if ltf is not None:
            minutes = ltf_bars_in_hour(ltf, h1_candle.timestamp)
            if minutes:
                for minute in minutes:
                    if self._check_exit(
                        portfolio, symbol, index, minute, wick_sl=True
                    ):
                        return True
                return False
        return self._check_exit(portfolio, symbol, index, h1_candle)

    def _check_exit(
        self,
        portfolio: MultiMarketPortfolio,
        symbol: str,
        index: int,
        candle: Candle,
        *,
        wick_sl: bool | None = None,
    ) -> bool:
        trade = portfolio.get_trade(symbol)
        if trade is None:
            return False

        from borex.alexg.force_flat import force_flat_reason

        flat = force_flat_reason(candle.timestamp, self.config, trade=trade)
        if flat is not None:
            # Session-end print (bar open), not the following hour's close.
            self._close(portfolio, symbol, index, candle.open, candle.timestamp, flat)
            return True

        sl_armed = trade.sl_is_armed(index)
        if wick_sl is None:
            wick_sl = bool(getattr(self.config, "intra_hour_sl", True))
        sl = trade.stop_loss
        ms = (
            portfolio.margin_stop_out_price(symbol)
            if sl_armed and self.config.size_mode == "margin"
            else None
        )
        sl_wick = sl_armed and (
            (ms is not None and self._hit_stop(trade, candle, ms, wick=True))
            or (sl is not None and self._hit_stop(trade, candle, sl, wick=True))
        )
        tp_wick = self._hit_take_profit(trade, candle)

        # Same hour tagged both sides → SL first (broker path). Never TP that bar.
        if sl_wick and tp_wick:
            if ms is not None and self._hit_stop(trade, candle, ms, wick=True):
                self._close(portfolio, symbol, index, ms, candle.timestamp, "margin_stop")
            else:
                self._close(portfolio, symbol, index, sl, candle.timestamp, "stop_loss")
            return True

        if tp_wick:
            self._close(
                portfolio,
                symbol,
                index,
                float(trade.take_profit),
                candle.timestamp,
                "take_profit",
            )
            return True

        if sl_armed and ms is not None and self._hit_stop(trade, candle, ms, wick=wick_sl):
            self._close(portfolio, symbol, index, ms, candle.timestamp, "margin_stop")
            return True
        if sl_armed and sl and self._hit_stop(trade, candle, sl, wick=wick_sl):
            self._close(portfolio, symbol, index, sl, candle.timestamp, "stop_loss")
            return True
        return False

    @staticmethod
    def _hit_take_profit(trade: Trade, candle: Candle) -> bool:
        tp = trade.take_profit
        if not tp:
            return False
        if trade.side == PositionSide.LONG:
            return candle.high >= tp
        return candle.low <= tp

    @staticmethod
    def _hit_stop(trade: Trade, candle: Candle, price: float, *, wick: bool) -> bool:
        level = candle.low if wick else candle.close
        high = candle.high if wick else candle.close
        if trade.side == PositionSide.LONG:
            return level <= price
        return high >= price

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
