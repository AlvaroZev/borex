"""Ablation configuration for Alex Set-and-Forget rule pills (video 2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from typing import Iterable, Literal

HtfBias = Literal["off", "pair", "weekly", "daily", "4h"]
SessionName = Literal["asia", "london", "newyork", "overlap", "all"]

HTF_BIAS_OPTIONS: tuple[HtfBias, ...] = ("off", "pair", "weekly", "daily", "4h")
SESSION_OPTIONS: tuple[SessionName, ...] = (
    "asia",
    "london",
    "newyork",
    "overlap",
    "all",
)


@dataclass(frozen=True)
class AblationConfig:
    """
    Six rule pills from the Revelio ablation study.

    1. htf_bias — higher-timeframe direction filter
    2. require_chart_trend — trade TF trend must align with trade direction
    3. require_pattern — candlestick pattern trigger
    4. require_retest — AOI retest logic
    5. require_ghost_sl_entry — queue a ghost trade, fill at its planned SL
    6. session — which session(s) may fire entries
    """

    htf_bias: HtfBias = "pair"
    require_chart_trend: bool = True
    require_pattern: bool = True
    require_retest: bool = False
    require_ghost_sl_entry: bool = True
    session: SessionName = "all"

    def label(self) -> str:
        trend = "trend1" if self.require_chart_trend else "trend0"
        pat = "pat1" if self.require_pattern else "pat0"
        ret = "ret1" if self.require_retest else "ret0"
        ghost = "ghost1" if self.require_ghost_sl_entry else "ghost0"
        return (
            f"bias={self.htf_bias}|{trend}|{pat}|{ret}|{ghost}|"
            f"sess={self.session}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


def video1_default() -> AblationConfig:
    """Original Set-and-Forget rules emphasized in video 1."""
    return AblationConfig(
        htf_bias="pair",
        require_chart_trend=True,
        require_pattern=True,
        require_retest=False,
        require_ghost_sl_entry=True,
        session="all",
    )


def video2_winner() -> AblationConfig:
    """Ablation winner from video 2: everything off, London–NY overlap."""
    return AblationConfig(
        htf_bias="off",
        require_chart_trend=False,
        require_pattern=False,
        require_retest=False,
        require_ghost_sl_entry=False,
        session="overlap",
    )


def video2_ghost() -> AblationConfig:
    """Video 2 winner filters + ghost SL entry (alexg7 default)."""
    return AblationConfig(
        htf_bias="off",
        require_chart_trend=False,
        require_pattern=False,
        require_retest=False,
        require_ghost_sl_entry=True,
        session="overlap",
    )


def video2_ghost_all_sessions() -> AblationConfig:
    """Same as video2_ghost, but allow setups in any session (alexg8)."""
    return AblationConfig(
        htf_bias="off",
        require_chart_trend=False,
        require_pattern=False,
        require_retest=False,
        require_ghost_sl_entry=True,
        session="all",
    )


def iter_ablation_grid(
    *,
    htf_biases: Iterable[HtfBias] = HTF_BIAS_OPTIONS,
    chart_trend: Iterable[bool] = (False, True),
    patterns: Iterable[bool] = (False, True),
    retests: Iterable[bool] = (False, True),
    ghosts: Iterable[bool] = (False, True),
    sessions: Iterable[SessionName] = SESSION_OPTIONS,
) -> list[AblationConfig]:
    """Full combinatorial grid (default = 5×2×2×2×2×5 = 400 configs)."""
    out: list[AblationConfig] = []
    for bias, trend, pat, ret, ghost, sess in product(
        htf_biases, chart_trend, patterns, retests, ghosts, sessions
    ):
        out.append(
            AblationConfig(
                htf_bias=bias,
                require_chart_trend=trend,
                require_pattern=pat,
                require_retest=ret,
                require_ghost_sl_entry=ghost,
                session=sess,
            )
        )
    return out
