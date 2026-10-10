"""GhostSL research package — LIQ = GhostSL geometry (not liquidity filter)."""

from borex.gate2.ghostsl.model import (
    GhostSetup,
    GhostStatus,
    bar_touches_real_entry,
    bar_touches_real_sl,
    bar_touches_real_tp,
    build_ghost_setup,
    ghost_sl_from_g,
    real_levels_from_entry,
    resolve_activation_bar,
)

__all__ = [
    "GhostSetup",
    "GhostStatus",
    "bar_touches_real_entry",
    "bar_touches_real_sl",
    "bar_touches_real_tp",
    "build_ghost_setup",
    "ghost_sl_from_g",
    "real_levels_from_entry",
    "resolve_activation_bar",
]
