"""GhostSL — explicit two-stage entry geometry (research).

User definition (Gate 2):
  G = ghost entry from ordinary strategy
  GHOST_SL = normal SL calculated from G
  REAL_ENTRY = GHOST_SL   (do NOT trade at G)
  When price reaches REAL_ENTRY → activate
  REAL_SL / REAL_TP are recalculated FROM REAL_ENTRY (not anchored to G)

LIQ in this campaign means GhostSL activation geometry — NOT a liquidity filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

Side = Literal["long", "short"]


class GhostStatus(str, Enum):
    PENDING_GHOST = "PENDING_GHOST"
    GHOST_TRIGGERED = "GHOST_TRIGGERED"
    REAL_ACTIVE = "REAL_ACTIVE"
    REAL_SL = "REAL_SL"
    REAL_TP = "REAL_TP"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


@dataclass
class GhostSetup:
    ghost_entry: float
    ghost_sl: float
    real_entry: float
    real_sl: float
    real_tp: float
    side: Side
    signal_time: object | None = None
    activation_time: object | None = None
    status: GhostStatus = GhostStatus.PENDING_GHOST
    pattern: str = ""
    expires_bars: int = 72
    created_index: int = 0

    def validate_geometry(self) -> None:
        """Raise if directional geometry is inconsistent."""
        if self.side == "long":
            if not (self.ghost_sl < self.ghost_entry):
                raise ValueError(
                    f"LONG ghost_sl must be below G: G={self.ghost_entry} ghost_sl={self.ghost_sl}"
                )
            if self.real_entry != self.ghost_sl:
                raise ValueError("LONG real_entry must equal ghost_sl")
            if not (self.real_sl < self.real_entry < self.real_tp):
                raise ValueError(
                    f"LONG real geometry invalid: sl={self.real_sl} entry={self.real_entry} tp={self.real_tp}"
                )
        else:
            if not (self.ghost_sl > self.ghost_entry):
                raise ValueError(
                    f"SHORT ghost_sl must be above G: G={self.ghost_entry} ghost_sl={self.ghost_sl}"
                )
            if self.real_entry != self.ghost_sl:
                raise ValueError("SHORT real_entry must equal ghost_sl")
            if not (self.real_tp < self.real_entry < self.real_sl):
                raise ValueError(
                    f"SHORT real geometry invalid: tp={self.real_tp} entry={self.real_entry} sl={self.real_sl}"
                )


def ghost_sl_from_g(
    ghost_entry: float,
    side: Side,
    *,
    sl_distance: float,
) -> float:
    """Normal/ghost SL from G using absolute distance."""
    if sl_distance <= 0:
        raise ValueError("sl_distance must be > 0")
    if side == "long":
        return ghost_entry - sl_distance
    return ghost_entry + sl_distance


def real_levels_from_entry(
    real_entry: float,
    side: Side,
    *,
    real_sl_distance: float,
    real_tp_distance: float | None = None,
    rr: float | None = None,
) -> tuple[float, float]:
    """Recalculate REAL_SL / REAL_TP from REAL_ENTRY (never anchored to G).

    Provide either ``real_tp_distance`` or ``rr`` (tp = rr * real_sl_distance).
    """
    if real_sl_distance <= 0:
        raise ValueError("real_sl_distance must be > 0")
    if real_tp_distance is None:
        if rr is None or rr <= 0:
            raise ValueError("provide real_tp_distance or positive rr")
        real_tp_distance = rr * real_sl_distance
    if real_tp_distance <= 0:
        raise ValueError("real_tp_distance must be > 0")
    if side == "long":
        return real_entry - real_sl_distance, real_entry + real_tp_distance
    return real_entry + real_sl_distance, real_entry - real_tp_distance


def build_ghost_setup(
    ghost_entry: float,
    side: Side,
    *,
    ghost_sl_distance: float,
    real_sl_distance: float | None = None,
    real_tp_distance: float | None = None,
    rr: float | None = 2.0,
    signal_time: object | None = None,
    created_index: int = 0,
    pattern: str = "",
    expires_bars: int = 72,
) -> GhostSetup:
    """Two-stage transform: G → ghost_sl=REAL_ENTRY → REAL_SL/TP from REAL_ENTRY."""
    ghost_sl = ghost_sl_from_g(ghost_entry, side, sl_distance=ghost_sl_distance)
    real_entry = ghost_sl
    rsl_dist = float(real_sl_distance if real_sl_distance is not None else ghost_sl_distance)
    real_sl, real_tp = real_levels_from_entry(
        real_entry,
        side,
        real_sl_distance=rsl_dist,
        real_tp_distance=real_tp_distance,
        rr=rr,
    )
    setup = GhostSetup(
        ghost_entry=float(ghost_entry),
        ghost_sl=float(ghost_sl),
        real_entry=float(real_entry),
        real_sl=float(real_sl),
        real_tp=float(real_tp),
        side=side,
        signal_time=signal_time,
        status=GhostStatus.PENDING_GHOST,
        pattern=pattern,
        expires_bars=expires_bars,
        created_index=created_index,
    )
    setup.validate_geometry()
    return setup


def bar_touches_real_entry(setup: GhostSetup, high: float, low: float) -> bool:
    if setup.side == "long":
        return low <= setup.real_entry
    return high >= setup.real_entry


def bar_touches_real_sl(setup: GhostSetup, high: float, low: float) -> bool:
    if setup.side == "long":
        return low <= setup.real_sl
    return high >= setup.real_sl


def bar_touches_real_tp(setup: GhostSetup, high: float, low: float) -> bool:
    if setup.side == "long":
        return high >= setup.real_tp
    return low <= setup.real_tp


def resolve_activation_bar(
    setup: GhostSetup,
    *,
    high: float,
    low: float,
    open_: float,
    close: float,
    same_bar_policy: str = "frozen_simulator",
) -> GhostStatus:
    """Resolve activation-bar ambiguity using frozen-simulator style caution.

    If REAL_ENTRY is reached on a bar that also touches REAL_SL and/or REAL_TP,
    we do **not** invent a favorable path. Mark INVALIDATED when both SL and TP
    are touched on the activation bar (path unresolved — same spirit as
    existing fidelity unresolved same-bar exits). If only SL is also touched
    on activation, treat as REAL_SL (conservative). If only TP, REAL_TP.
    """
    if not bar_touches_real_entry(setup, high, low):
        return GhostStatus.PENDING_GHOST
    hit_sl = bar_touches_real_sl(setup, high, low)
    hit_tp = bar_touches_real_tp(setup, high, low)
    if hit_sl and hit_tp:
        return GhostStatus.INVALIDATED  # unresolved path — do not invent ordering
    if hit_sl:
        return GhostStatus.REAL_SL
    if hit_tp:
        return GhostStatus.REAL_TP
    return GhostStatus.REAL_ACTIVE
