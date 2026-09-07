"""AlexG8 — alexg7aligned geometry + ghost, no session filter.

Same knobs as ``AlexG7AlignedStrategy`` (video2 ghost, feed-alignment AOI),
but ``session="all"`` so new setups may queue outside London–NY overlap.
Ghost fills still do not re-check session.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.ablation import AblationConfig, video2_ghost_all_sessions
from borex.alexg.strategy7_aligned import AlexG7AlignedStrategy


@dataclass
class AlexG8Strategy(AlexG7AlignedStrategy):
    """alexg7aligned without overlap-only session gating."""

    name: str = "alexg8"
    ablation: AblationConfig = field(default_factory=video2_ghost_all_sessions)
