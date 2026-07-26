from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfluenceWeights:
    """
    Linear confluence weights (MLP-ready feature vector).

    Train later by fitting these (or a small MLP) on labeled setups;
    for now they are hand-tuned priors.
    """

    trend: float = 0.25
    aoi: float = 0.20
    confirmation: float = 0.20
    currency: float = 0.15
    head_shoulders: float = 0.10
    impulse: float = 0.10


def score_confluence(
    *,
    trend_ok: bool,
    aoi_ok: bool,
    confirmation_ok: bool,
    currency_ok: bool,
    hs_aligned: bool,
    impulse_aligned: bool,
    weights: ConfluenceWeights | None = None,
) -> float:
    w = weights or ConfluenceWeights()
    total = (
        (w.trend if trend_ok else 0.0)
        + (w.aoi if aoi_ok else 0.0)
        + (w.confirmation if confirmation_ok else 0.0)
        + (w.currency if currency_ok else 0.0)
        + (w.head_shoulders if hs_aligned else 0.0)
        + (w.impulse if impulse_aligned else 0.0)
    )
    denom = (
        w.trend
        + w.aoi
        + w.confirmation
        + w.currency
        + w.head_shoulders
        + w.impulse
    )
    if denom <= 0:
        return 0.0
    return total / denom
