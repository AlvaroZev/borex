"""Frozen Gate 2 micro-fidelity experiment parameters.

Do NOT tune these for PnL / WR / PF. Change only via explicit research decision
and a new STRATEGY_VERSION string.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STRATEGY_NAME = "gate2_micro_fidelity"
STRATEGY_VERSION = "v1_20260924_n20"
CONFIG_ID = "micro_fidelity_v1_n20_unlimited_4pip"

# Mirror identity (never Live)
EXPECTED_MIRROR_LOGIN = 53019882
FORBIDDEN_LIVE_LOGIN = 52958398
REQUIRED_MAGIC = 88002
FORBIDDEN_LIVE_MAGIC = 88001
MIRROR_PORT = 8792
EXPECTED_SERVER = "ICMarketsSC-Demo"
COMMENT_PREFIX = "bx_g2m_"  # MT5 comment max 31; isolates from g2p/g2e/natural

# Explicit protected leftovers — never touch
PROTECTED_EURDKK_TICKETS: frozenset[int] = frozenset(
    {
        1890205005,
        1891279302,
        1893184489,
        1917854332,
        1940979693,
    }
)
PROTECTED_SYMBOL_PREFIX = "EURDKK"

# Live submission enabled after explicit human approval (2026-09-23).
LIVE_EXECUTION_ENABLED = True


@dataclass(frozen=True)
class MicroFidelityConfig:
    """Immutable experiment configuration snapshot."""

    strategy_name: str = STRATEGY_NAME
    strategy_version: str = STRATEGY_VERSION
    config_id: str = CONFIG_ID
    timeframe: str = "1m"
    symbols_yahoo: tuple[str, ...] = (
        "EURUSD=X",
        "GBPUSD=X",
        "USDJPY=X",
        "AUDUSD=X",
        "USDCHF=X",
        "USDCAD=X",
    )
    # Signal: completed 1m previous-bar breakout vs mean range
    lookback_bars: int = 5
    breakout_frac: float = 0.25
    min_breakout_pips: float = 0.5
    cooldown_bars: int = 3
    # Execution
    volume_lots: float = 0.01
    max_spread_pips: float = 2.0
    # Symmetric SL/TP target; same 4.0 pips as g2m_20260923_221921 (broker min cleared)
    sl_tp_target_pips: float = 4.0
    max_sl_tp_pips: float = 8.0
    rr: float = 1.0  # TP distance = SL distance * rr
    max_open_total: int = 20
    max_open_per_symbol: int = 20
    max_new_orders_per_hour: int = 120
    # Runtime: 0 duration = unlimited; stop after target completed trades
    experiment_duration_seconds: int = 0
    target_completed_trades: int = 20
    heartbeat_seconds: float = 15.0
    poll_seconds: float = 2.0
    max_tick_age_seconds: float = 120.0
    # Identity
    magic: int = REQUIRED_MAGIC
    comment_prefix: str = COMMENT_PREFIX
    mirror_login: int = EXPECTED_MIRROR_LOGIN
    mirror_port: int = MIRROR_PORT
    expected_server: str = EXPECTED_SERVER
    protected_eurdkk_tickets: frozenset[int] = field(
        default_factory=lambda: PROTECTED_EURDKK_TICKETS
    )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["protected_eurdkk_tickets"] = sorted(self.protected_eurdkk_tickets)
        d["symbols_yahoo"] = list(self.symbols_yahoo)
        return d


FROZEN_CONFIG = MicroFidelityConfig()
