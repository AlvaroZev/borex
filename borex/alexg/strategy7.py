"""AlexG7 — video-2 winner filters + ghost SL entry."""

from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.ablation import AblationConfig, video2_ghost
from borex.alexg.strategy5_revised import AlexG5RevisedStrategy


@dataclass
class AlexG7Strategy(AlexG5RevisedStrategy):
    """
    Video 2 ablation winner (all filters off, London–NY overlap) plus the
    video-1 ghost rule: queue a setup and fill only when price tags its
    planned SL.
    """

    name: str = "alexg7"
    ablation: AblationConfig = field(default_factory=video2_ghost)
