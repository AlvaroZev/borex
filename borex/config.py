from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "data" / "cache"
RESULTS_DB = ROOT_DIR / "data" / "results.db"


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 1_000.0
    leverage: float = 500.0
    max_leverage: float = 5_000.0
    position_size_pct: float = 0.1  # margin per trade as fraction of equity
    max_positions: int = 5
    commission_pct: float = 0.0  # per side, tunable
    slippage_pct: float = 0.0
    maintenance_margin_ratio: float = 0.5  # liquidate when equity <= margin * ratio
    # Position sizing: fixed | atr_risk | kelly
    size_mode: str = "fixed"
    risk_per_trade_pct: float = 0.01  # fraction of equity risked to stop (atr_risk)
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    kelly_fraction: float = 0.5  # half-Kelly cap
    kelly_min_trades: int = 20
    # Portfolio circuit breakers (None = disabled)
    max_daily_loss_pct: float | None = None
    max_drawdown_pct: float | None = None
    # Correlation / currency exposure
    correlation_limit: bool = True
    max_currency_exposure: int = 1  # max net same-direction bets per currency
    # Execution realism (Phase 4)
    spread_pct: float = 0.0  # half-spread per side (e.g. 0.00005 ~ 0.5 pip EURUSD)
    slippage_mode: str = "fixed"  # fixed | atr
    slippage_atr_mult: float = 0.1
    fill_mode: str = "close"  # close | next_open
    entry_delay_bars: int = 0  # latency simulation


@dataclass(frozen=True)
class LiveConfig:
    """Phase 5 — live/paper deployment guardrails."""
    max_consecutive_errors: int = 3
    stale_data_minutes: int = 180
    divergence_warn_pct: float | None = 0.20  # warn if live return diverges from baseline
    kill_on_liquidation: bool = True
    kill_on_halt: bool = True
    flatten_on_kill: bool = False  # reserved; logs intent only for paper


def make_live_config(**kwargs) -> LiveConfig:
    allowed = {f.name for f in fields(LiveConfig)}
    filtered = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    return LiveConfig(**filtered)


def live_config_dict(cfg: LiveConfig) -> dict:
    return asdict(cfg)


@dataclass(frozen=True)
class IterationConfig:
    """Phase 6 — periodic re-validation and capital scaling thresholds."""
    recent_months: int = 3
    baseline_months: int | None = None
    decay_sharpe_delta: float = 0.5
    decay_return_pct: float = 15.0
    decay_profit_factor: float = 0.3
    min_recent_trades: int = 10
    min_paper_days: int = 14
    min_paper_trades: int = 20
    scale_up_factor: float = 1.5
    max_capital_multiplier: float = 5.0


def make_iteration_config(**kwargs) -> IterationConfig:
    allowed = {f.name for f in fields(IterationConfig)}
    filtered = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    return IterationConfig(**filtered)


def iteration_config_dict(cfg: IterationConfig) -> dict:
    return asdict(cfg)


def make_backtest_config(**kwargs) -> BacktestConfig:
    allowed = {f.name for f in fields(BacktestConfig)}
    filtered = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    return BacktestConfig(**filtered)


def backtest_config_dict(cfg: BacktestConfig) -> dict:
    return asdict(cfg)
