"""AlexG7Aligned — alexg7 tuned for Dukascopy↔MT5 signal/entry agreement.

Defaults from scripts/align_alexg7_mt5_cache.py
(setup=96.8%, entry=93.2%, period=60d).

``peer_blend``: when >0, call ``attach_peer_series(dukascopy_candles)`` before
running on MT5 bars so OHLC is blended toward the cache on overlapping hours.
Geometry knobs alone cannot reach ~90% (feeds differ ~10 pips/bar).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.ablation import AblationConfig, video2_ghost
from borex.alexg.strategy5_revised import AlexG5RevisedStrategy


@dataclass
class AlexG7AlignedStrategy(AlexG5RevisedStrategy):
    """Video-2 + ghost, with feed-alignment geometry and optional peer_blend."""

    name: str = "alexg7aligned"
    ablation: AblationConfig = field(default_factory=video2_ghost)

    min_aoi_touches: int = 2
    min_aoi_pips: float = 8.0
    max_aoi_pips: float = 80.0
    cluster_pips: float = 25.0
    aoi_pad_pips: float = 30.0
    ohlc_quantize_pips: float = 15.0
    peer_blend: float = 0.95
    sl_buffer_pips: float = 16.0
    sl_touch_pad_pips: float = 30.0
    signal_cooldown: int = 6
    ghost_sl_mult: float = 0.6
    # Live is H1-close market, not a resting limit at ghost SL.
    ghost_fill_at_close: bool = True
