"""AlexG9 — alexg8 geometry with session-end flatten, fixed RR, commission on top.

Changes vs alexg8:
  - Force-flat before NY close (daily Mon–Thu; from 19:00 UTC on Friday)
  - Fixed RR (no dynamic 1/winrate scaling that produced huge TP vs ~1% SL)
  - 1% margin into market; round-turn commission charged separately at entry
"""

from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.ablation import AblationConfig, video2_ghost_all_sessions
from borex.alexg.strategy8 import AlexG8Strategy


@dataclass
class AlexG9Strategy(AlexG8Strategy):
    """alexg8 + alexg9 live/backtest exit & sizing defaults (see engine config)."""

    name: str = "alexg9"
    ablation: AblationConfig = field(default_factory=video2_ghost_all_sessions)
