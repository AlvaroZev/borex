from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from borex.config import BacktestConfig
from borex.data.symbols import parse_forex_currencies
from borex.models.signal import Candle, Signal, SignalAction
from borex.strategy.indicators import atr_at


class SizeMode(str, Enum):
    FIXED = "fixed"
    ATR_RISK = "atr_risk"
    KELLY = "kelly"


@dataclass
class RiskStats:
    halted: bool = False
    halt_reason: str = ""
    circuit_breaker_triggers: int = 0
    correlation_blocks: int = 0

    def to_dict(self) -> dict:
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "circuit_breaker_triggers": self.circuit_breaker_triggers,
            "correlation_blocks": self.correlation_blocks,
        }


@dataclass
class RiskTracker:
    config: BacktestConfig
    stats: RiskStats = field(default_factory=RiskStats)
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    current_day: object | None = None

    def on_bar(self, equity: float, day: object) -> None:
        if self.current_day != day:
            self.current_day = day
            self.day_start_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        self._check_circuit_breakers(equity)

    def _check_circuit_breakers(self, equity: float) -> None:
        if self.stats.halted:
            return
        cfg = self.config
        if cfg.max_drawdown_pct is not None and self.peak_equity > 0:
            dd = (self.peak_equity - equity) / self.peak_equity
            if dd >= cfg.max_drawdown_pct:
                self._halt("max_drawdown")
                return
        if cfg.max_daily_loss_pct is not None and self.day_start_equity > 0:
            daily_loss = (self.day_start_equity - equity) / self.day_start_equity
            if daily_loss >= cfg.max_daily_loss_pct:
                self._halt("daily_loss")

    def _halt(self, reason: str) -> None:
        self.stats.halted = True
        self.stats.halt_reason = reason
        self.stats.circuit_breaker_triggers += 1

    def can_enter(self) -> bool:
        return not self.stats.halted

    def currency_exposure(self, open_trades: list) -> dict[str, int]:
        """Net directional exposure per currency from open trades."""
        exposure: dict[str, int] = defaultdict(int)
        for trade in open_trades:
            if not trade.symbol:
                continue
            parsed = parse_forex_currencies(trade.symbol)
            if not parsed:
                continue
            base, quote = parsed
            if trade.side.value == "long":
                exposure[base] += 1
                exposure[quote] -= 1
            else:
                exposure[base] -= 1
                exposure[quote] += 1
        return dict(exposure)

    def allows_correlation(self, open_trades: list, symbol: str, action: SignalAction) -> bool:
        if not self.config.correlation_limit:
            return True
        parsed = parse_forex_currencies(symbol)
        if not parsed:
            return True
        base, quote = parsed
        exposure = self.currency_exposure(open_trades)
        limit = self.config.max_currency_exposure
        if action == SignalAction.BUY:
            trial = dict(exposure)
            trial[base] = trial.get(base, 0) + 1
            trial[quote] = trial.get(quote, 0) - 1
        elif action == SignalAction.SELL:
            trial = dict(exposure)
            trial[base] = trial.get(base, 0) - 1
            trial[quote] = trial.get(quote, 0) + 1
        else:
            return True
        for net in trial.values():
            if abs(net) > limit:
                self.stats.correlation_blocks += 1
                return False
        return True

    def compute_size_pct(
        self,
        signal: Signal,
        candles: list[Candle],
        index: int,
        equity: float,
        closed_trades: list,
    ) -> float:
        base = max(0.0, signal.size_pct)
        mode = self.config.size_mode

        if mode == SizeMode.FIXED.value:
            return base

        if mode == SizeMode.ATR_RISK.value:
            atr = atr_at(candles, index, self.config.atr_period)
            if atr <= 0 or equity <= 0:
                return base
            entry = candles[index].close
            stop_dist = abs(entry - signal.stop_loss) if signal.stop_loss else atr * self.config.atr_stop_mult
            if stop_dist <= 0:
                return base
            stop_frac = stop_dist / entry
            risk_dollars = equity * self.config.risk_per_trade_pct
            margin = risk_dollars / max(stop_frac * self.config.leverage, 1e-9)
            cap = equity * self.config.position_size_pct
            margin = min(margin, cap)
            size_pct = margin / max(equity * self.config.position_size_pct, 1e-9)
            return max(0.05, min(base, size_pct))

        if mode == SizeMode.KELLY.value:
            if len(closed_trades) < self.config.kelly_min_trades:
                return base
            wins = [t for t in closed_trades if t.pnl > 0]
            losses = [t for t in closed_trades if t.pnl <= 0]
            if not wins or not losses:
                return base * 0.5
            win_rate = len(wins) / len(closed_trades)
            avg_win = sum(t.pnl for t in wins) / len(wins)
            avg_loss = abs(sum(t.pnl for t in losses) / len(losses))
            if avg_loss <= 0:
                return base
            b = avg_win / avg_loss
            kelly = win_rate - (1 - win_rate) / b
            frac = max(0.0, min(1.0, kelly * self.config.kelly_fraction))
            return max(0.05, min(base, base * frac))

        return base
