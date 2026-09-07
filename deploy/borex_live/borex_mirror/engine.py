from __future__ import annotations

import logging
from typing import Any

from borex.backtest.engine import BacktestConfig
from borex.models.candle import Candle

from borex_live.engine.shadow_engine import ShadowEngine
from borex_live.strategy_registry import create_strategy
from borex_mirror.config import MirrorConfig
from borex_mirror.executor import MirrorExecutor
from borex_mirror.repository import MirrorRepository

logger = logging.getLogger(__name__)


class MirrorEngine:
    """
    Shared bar processor: theory step → mirror opens/closes.

    Used identically for:
      - bulk / hour-by-hour offline feed (execute_mt5=False → paper)
      - live MT5 hour feed (execute_mt5=True → market at close)
    """

    def __init__(self, cfg: MirrorConfig, mt5: Any = None) -> None:
        self.cfg = cfg
        self.live_cfg = cfg.to_live_cfg()
        strategy, _ = create_strategy(
            cfg.strategy,
            min_rr=cfg.min_rr,
            second_signal=cfg.second_signal,
            execution_interval=cfg.interval,
        )
        self.bt_config = BacktestConfig(
            initial_capital=cfg.capital,
            leverage=cfg.leverage,
            position_size_pct=cfg.position_size_pct,
            size_mode="margin",
            true_sl=True,
            true_sl_rr=cfg.min_rr,
            rr_mode=cfg.rr_mode,
            rr_factor=cfg.rr_factor,
            rr_min=float(getattr(cfg, "rr_min", 0.0) or 0.0),
            rr_max=float(getattr(cfg, "rr_max", 0.0) or 0.0),
            stop_loss_pct=None,
            take_profit_pct=None,
            commission_per_lot=cfg.commission_per_lot,
            min_commission_per_side=cfg.min_commission_per_side,
            lot_notional=cfg.lot_notional,
            risk_include_commission=cfg.risk_include_commission,
            winrate_min_trades=cfg.winrate_min_trades,
            commission_at_entry=cfg.commission_at_entry,
            force_flat_friday=cfg.force_flat_friday,
            force_flat_daily=cfg.force_flat_daily,
            force_flat_utc_hour=cfg.force_flat_utc_hour,
            force_flat_friday_from_hour=cfg.force_flat_friday_from_hour,
        )
        self.shadow = ShadowEngine(strategy, self.live_cfg, self.bt_config)
        self.executor = MirrorExecutor(cfg, mt5)
        self._open_keys: set[tuple[str, str, str]] = set()

    @staticmethod
    def _trade_key(trade: Any) -> tuple[str, str, str]:
        side = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
        return (trade.symbol, str(trade.entry_time), str(trade.pattern or ""))

    def _sync_open_keys(self) -> None:
        self._open_keys = {
            self._trade_key(t) for t in self.shadow.portfolio.open_trades.values()
        }

    def start(
        self,
        repo: MirrorRepository,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
    ) -> dict[str, Any]:
        info = self.shadow.start(repo, candles_by_symbol, master_symbol)
        self._sync_open_keys()
        # Ensure any restored open theory trades have mirror rows (paper if needed)
        for trade in self.shadow.portfolio.open_trades.values():
            key = self._trade_key(trade)
            if repo.find_mirror_trade(*key) is None:
                self.executor.mirror_open(
                    repo, trade, execute_mt5=False  # don't re-fire MT5 on restart
                )
        return info

    def process_master_bar(
        self,
        repo: MirrorRepository,
        master_index: int,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
        *,
        execute_mt5: bool,
    ) -> dict[str, int]:
        """
        One closed master bar — THE shared path for bulk and live.

        1) Snapshot open keys / closed count
        2) Theory step (ShadowEngine)
        3) Persist theory checkpoint
        4) Mirror new opens to MT5/paper (same bar)
        5) Mirror new closes
        """
        before_open = set(self._open_keys)
        before_closed_n = len(self.shadow.portfolio.closed_trades)

        self.shadow.step_master_bar(master_index, candles_by_symbol, master_symbol)
        self.shadow.checkpoint(repo, candles_by_symbol)

        after_open = {
            self._trade_key(t) for t in self.shadow.portfolio.open_trades.values()
        }
        opened_keys = after_open - before_open
        opened = 0
        for trade in self.shadow.portfolio.open_trades.values():
            if self._trade_key(trade) in opened_keys:
                self.executor.mirror_open(repo, trade, execute_mt5=execute_mt5)
                opened += 1

        closed = 0
        for trade in self.shadow.portfolio.closed_trades[before_closed_n:]:
            self.executor.mirror_close(repo, trade, execute_mt5=execute_mt5)
            closed += 1

        self._open_keys = after_open
        return {"opened": opened, "closed": closed}

    def process_range(
        self,
        repo: MirrorRepository,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
        start: int,
        stop: int,
        *,
        execute_mt5: bool,
    ) -> dict[str, int]:
        opened = closed = 0
        master = candles_by_symbol[master_symbol]
        for mi in range(max(0, start), min(stop, len(master))):
            stats = self.process_master_bar(
                repo,
                mi,
                candles_by_symbol,
                master_symbol,
                execute_mt5=execute_mt5,
            )
            opened += stats["opened"]
            closed += stats["closed"]
        return {"opened": opened, "closed": closed, "bars": max(0, stop - start)}

    def process_available(
        self,
        repo: MirrorRepository,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
        *,
        execute_mt5: bool,
    ) -> dict[str, int]:
        master = candles_by_symbol.get(master_symbol) or []
        if not master or str(master[-1].timestamp) == self.shadow.last_master_ts:
            return {"opened": 0, "closed": 0, "bars": 0}
        cursor = self.shadow._master_index_for_ts(master, self.shadow.last_master_ts)
        if cursor is None:
            raise RuntimeError(
                "Mirror theory cursor outside loaded history; restart for new epoch"
            )
        return self.process_range(
            repo,
            candles_by_symbol,
            master_symbol,
            cursor + 1,
            len(master),
            execute_mt5=execute_mt5,
        )
