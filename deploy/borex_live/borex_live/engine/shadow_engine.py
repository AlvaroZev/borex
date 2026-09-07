from __future__ import annotations

import hashlib
import json
import logging
import pickle
from typing import Any

from borex.alexg.multi_market import MultiMarketContext, align_symbols_to_timeline
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.backtest.multi_portfolio import MultiMarketPortfolio
from borex.models.candle import Candle

from borex_live.config import LiveServiceConfig
from borex_live.store.repository import StateRepository

logger = logging.getLogger(__name__)


class ShadowEngine:
    """Persistent incremental theory portfolio with no MT5 execution boundary."""

    STATE_VERSION = 1

    def __init__(
        self,
        strategy: Any,
        cfg: LiveServiceConfig,
        bt_config: BacktestConfig,
    ) -> None:
        self.strategy = strategy
        self.cfg = cfg
        self.bt_config = bt_config
        self.engine = MultiMarketEngine(
            strategy=strategy,
            config=bt_config,
            max_positions=cfg.max_positions,
        )
        self.portfolio = self._new_portfolio()
        self.config_hash = self._config_hash()
        self.last_master_ts = ""
        self._last_indices: dict[str, int] = {}
        self._persisted_closed_count = 0

    def _new_portfolio(self) -> MultiMarketPortfolio:
        c = self.bt_config
        return MultiMarketPortfolio(
            initial_capital=c.initial_capital,
            position_size_pct=c.position_size_pct,
            leverage=c.leverage,
            maintenance_margin_ratio=c.maintenance_margin_ratio,
            size_mode=c.size_mode,
            max_positions=self.cfg.max_positions,
            commission_per_lot=c.commission_per_lot,
            commission_per_trade=c.commission_per_trade,
            min_commission_per_side=c.min_commission_per_side,
            lot_notional=c.lot_notional,
            risk_include_commission=c.risk_include_commission,
        )

    def _config_hash(self) -> str:
        values = {
            "version": self.STATE_VERSION,
            "strategy": self.cfg.strategy,
            "interval": self.cfg.interval,
            "leverage": self.cfg.leverage,
            "position_size_pct": self.cfg.position_size_pct,
            "max_positions": self.cfg.max_positions,
            "min_rr": self.cfg.min_rr,
            "rr_factor": self.cfg.rr_factor,
            "same_bar_exit": self.cfg.same_bar_exit,
            "commission_per_lot": self.cfg.commission_per_lot,
            "risk_include_commission": self.cfg.risk_include_commission,
            "winrate_min_trades": self.cfg.winrate_min_trades,
            "rr_mode": getattr(self.cfg, "rr_mode", "dynamic"),
            "commission_at_entry": getattr(self.cfg, "commission_at_entry", False),
            "force_flat_friday": getattr(self.cfg, "force_flat_friday", False),
            "force_flat_daily": getattr(self.cfg, "force_flat_daily", False),
            "force_flat_utc_hour": getattr(self.cfg, "force_flat_utc_hour", 19),
        }
        raw = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _master_index_for_ts(master: list[Candle], ts: str) -> int | None:
        for i, candle in enumerate(master):
            if str(candle.timestamp) == str(ts):
                return i
        return None

    def start(
        self,
        repo: StateRepository,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
    ) -> dict[str, int | str]:
        """Restore a matching checkpoint, catch up downtime, or begin now."""
        master = candles_by_symbol.get(master_symbol) or []
        if not master:
            return {"mode": "empty", "processed": 0}

        state = repo.get_theory_state()
        if state is not None and state.config_hash == self.config_hash:
            cursor_i = self._master_index_for_ts(master, state.last_master_ts)
            if cursor_i is not None:
                try:
                    payload = pickle.loads(state.state_blob)
                    if int(payload.get("version", 0)) != self.STATE_VERSION:
                        raise ValueError("theory state version changed")
                    self.strategy = payload["strategy"]
                    self.portfolio = payload["portfolio"]
                    self.engine.strategy = self.strategy
                    self.last_master_ts = state.last_master_ts
                    self._persisted_closed_count = len(self.portfolio.closed_trades)
                    self._restore_indices(
                        payload.get("indices", {}),
                        candles_by_symbol,
                        master,
                        cursor_i,
                    )
                    processed = self.process_range(
                        repo,
                        candles_by_symbol,
                        master_symbol,
                        cursor_i + 1,
                        len(master),
                    )
                    logger.info(
                        "Theory restored at %s; caught up %d master bar(s)",
                        state.last_master_ts,
                        processed,
                    )
                    return {"mode": "restored", "processed": processed}
                except Exception:
                    logger.exception("Theory checkpoint restore failed; starting a new epoch")

        if state is not None:
            logger.warning("Theory config/timeline changed; starting a clean theory epoch")
        repo.reset_theory()
        self.portfolio = self._new_portfolio()
        self._rebuild_strategy_only(candles_by_symbol, master_symbol)
        latest_i = len(master) - 1
        self.last_master_ts = str(master[latest_i].timestamp)
        self._last_indices = self._indices_at(
            candles_by_symbol,
            master,
            latest_i,
        )
        self.checkpoint(repo, candles_by_symbol)
        logger.info("Theory epoch initialized at %s (future bars only)", self.last_master_ts)
        return {"mode": "initialized", "processed": 0}

    def _indices_at(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        master: list[Candle],
        master_index: int,
    ) -> dict[str, int]:
        ts_maps = align_symbols_to_timeline(master, candles_by_symbol)
        ts = master[master_index].timestamp
        return {
            symbol: index
            for symbol, mapping in ts_maps.items()
            if (index := mapping.get(ts)) is not None
        }

    def _restore_indices(
        self,
        saved: dict[str, int],
        candles_by_symbol: dict[str, list[Candle]],
        master: list[Candle],
        master_index: int,
    ) -> None:
        current = self._indices_at(candles_by_symbol, master, master_index)
        deltas = {
            symbol: current[symbol] - int(old)
            for symbol, old in saved.items()
            if symbol in current
        }
        for symbol, pending in getattr(self.strategy, "_pending", {}).items():
            delta = deltas.get(symbol, 0)
            pending.created_index += delta
            pending.expires_index += delta
        for symbol, old in list(getattr(self.strategy, "_last_signal_index", {}).items()):
            self.strategy._last_signal_index[symbol] = int(old) + deltas.get(symbol, 0)
        for symbol, value in list(getattr(self.strategy, "_pending_retest", {}).items()):
            action, level, armed_index, left_zone = value
            self.strategy._pending_retest[symbol] = (
                action,
                level,
                int(armed_index) + deltas.get(symbol, 0),
                left_zone,
            )
        for symbol, trade in self.portfolio.open_trades.items():
            delta = deltas.get(symbol, 0)
            trade.entry_index += delta
            if trade.sl_armed_from_index is not None:
                trade.sl_armed_from_index += delta
        self._clear_derived_strategy_cache()
        self._last_indices = current

    def _clear_derived_strategy_cache(self) -> None:
        for name, value in (
            ("_zones_cache", []),
            ("_zones_cache_index", -(10**9)),
            ("_htf_cache", {}),
            ("_htf_cache_index", -(10**9)),
            ("_quant_id", 0),
            ("_quant_candles", None),
            ("_current_symbol", ""),
        ):
            if hasattr(self.strategy, name):
                setattr(self.strategy, name, value)

    def _rebuild_strategy_only(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
    ) -> None:
        master = candles_by_symbol[master_symbol]
        wait = int(getattr(self.strategy, "sl_wait_max_bars", 72) or 72)
        start = max(int(getattr(self.strategy, "min_bars", 120)), len(master) - wait)
        ts_maps = align_symbols_to_timeline(master, candles_by_symbol)
        for master_i in range(start, len(master)):
            ctx = MultiMarketContext.at_master_bar(
                master_i,
                master,
                candles_by_symbol,
                ts_maps,
                strength_lookback=self.strategy.strength_lookback,
                min_currency_edge=self.strategy.min_currency_edge,
                min_confirming_pairs=self.strategy.min_confirming_pairs,
            )
            for symbol, index in ctx.indices.items():
                self.strategy.set_context(symbol, ctx)
                self.strategy.on_bar(index, candles_by_symbol[symbol], None)

    def process_range(
        self,
        repo: StateRepository,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
        start: int,
        stop: int,
    ) -> int:
        processed = 0
        for master_i in range(max(0, start), min(stop, len(candles_by_symbol[master_symbol]))):
            self.step_master_bar(master_i, candles_by_symbol, master_symbol)
            processed += 1
        if processed:
            self.checkpoint(repo, candles_by_symbol)
        return processed

    def process_available(
        self,
        repo: StateRepository,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
    ) -> int:
        master = candles_by_symbol.get(master_symbol) or []
        if not master or str(master[-1].timestamp) == self.last_master_ts:
            return 0
        cursor = self._master_index_for_ts(master, self.last_master_ts)
        if cursor is None:
            raise RuntimeError(
                "Theory cursor fell outside loaded MT5 history; restart to open a new epoch"
            )
        return self.process_range(
            repo,
            candles_by_symbol,
            master_symbol,
            cursor + 1,
            len(master),
        )

    def step_master_bar(
        self,
        master_index: int,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
    ) -> None:
        master = candles_by_symbol[master_symbol]
        ts_maps = align_symbols_to_timeline(master, candles_by_symbol)
        ctx = MultiMarketContext.at_master_bar(
            master_index,
            master,
            candles_by_symbol,
            ts_maps,
            strength_lookback=self.strategy.strength_lookback,
            min_currency_edge=self.strategy.min_currency_edge,
            min_confirming_pairs=self.strategy.min_confirming_pairs,
        )
        prices = {
            symbol: candles_by_symbol[symbol][index].close
            for symbol, index in ctx.indices.items()
        }
        for symbol in list(self.portfolio.open_trades):
            index = ctx.indices.get(symbol)
            if index is not None:
                self.engine._check_exit(
                    self.portfolio,
                    symbol,
                    index,
                    candles_by_symbol[symbol][index],
                )

        candidates = []
        min_bars = int(getattr(self.strategy, "min_bars", 120))
        for symbol, index in ctx.indices.items():
            if index < min_bars:
                continue
            self.strategy.set_context(symbol, ctx)
            signal = self.strategy.on_bar(index, candles_by_symbol[symbol], None)
            if signal is not None:
                candidates.append((symbol, signal, index))
        candidates.sort(key=lambda item: item[1].score, reverse=True)
        for symbol, signal, index in candidates:
            if self.portfolio.book_is_full():
                break
            if not self.portfolio.can_open(symbol):
                continue
            self.engine._open_signal(
                self.portfolio,
                symbol,
                signal,
                index,
                candles_by_symbol[symbol],
            )

        self.last_master_ts = str(master[master_index].timestamp)
        self._last_indices = dict(ctx.indices)
        self._latest_equity = self.portfolio.equity_at_prices(prices)

    def checkpoint(
        self,
        repo: StateRepository,
        candles_by_symbol: dict[str, list[Candle]],
    ) -> None:
        for trade in self.portfolio.open_trades.values():
            repo.upsert_theory_trade(trade)
        for trade in self.portfolio.closed_trades[self._persisted_closed_count :]:
            repo.upsert_theory_trade(trade)
        self._persisted_closed_count = len(self.portfolio.closed_trades)

        self._clear_derived_strategy_cache()
        payload = {
            "version": self.STATE_VERSION,
            "strategy": self.strategy,
            "portfolio": self.portfolio,
            "indices": self._last_indices,
        }
        blob = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        prices = {
            symbol: candles[-1].close
            for symbol, candles in candles_by_symbol.items()
            if candles
        }
        equity = self.portfolio.equity_at_prices(prices)
        repo.save_theory_state(
            strategy=self.cfg.strategy,
            config_hash=self.config_hash,
            initial_capital=self.bt_config.initial_capital,
            cash=self.portfolio.cash,
            equity=equity,
            last_master_ts=self.last_master_ts,
            state_blob=blob,
        )

