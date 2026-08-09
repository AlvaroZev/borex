"""Pip-bounded Areas of Interest for Alex Set-and-Forget."""

from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.structure_trend import body_high, body_low
from borex.backtest.costs import infer_pip_size
from borex.models.candle import Candle


@dataclass
class PipAOI:
    """Support/resistance zone constrained to [min_pips, max_pips] width."""

    low: float
    high: float
    kind: str  # support | resistance
    touches: int
    first_touch_index: int
    last_touch_index: int
    source_tf: str = "daily"

    @property
    def level(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def width(self) -> float:
        return self.high - self.low

    def contains_body(self, candle: Candle) -> bool:
        """True if candle body overlaps the zone (Alex: bodies, not wicks)."""
        bh = body_high(candle)
        bl = body_low(candle)
        return bl <= self.high and bh >= self.low

    def contains_close(self, candle: Candle) -> bool:
        return self.low <= candle.close <= self.high


def _pip_size(symbol: str) -> float:
    return infer_pip_size(symbol)


def build_pip_aoi_zones(
    candles: list[Candle],
    *,
    symbol: str = "EURUSD=X",
    min_touches: int = 3,
    min_pips: float = 5.0,
    max_pips: float = 60.0,
    max_age_bars: int | None = None,
    asof_index: int | None = None,
    source_tf: str = "daily",
    cluster_pips: float = 5.0,
) -> list[PipAOI]:
    """
    Build AOIs from body reaction clusters.

    A level needs >= min_touches, width in [min_pips, max_pips], and optionally
    must still be within max_age_bars of asof_index (daily≈2y, weekly≈5y).
    """
    if not candles:
        return []
    end = asof_index if asof_index is not None else len(candles) - 1
    end = min(end, len(candles) - 1)
    start = 0
    if max_age_bars is not None and max_age_bars > 0:
        start = max(0, end - max_age_bars)

    pip = _pip_size(symbol)
    cluster = cluster_pips * pip
    min_w = min_pips * pip
    max_w = max_pips * pip

    # Candidate body extremes as touch points.
    points: list[tuple[int, float]] = []
    for i in range(start, end + 1):
        c = candles[i]
        points.append((i, body_high(c)))
        points.append((i, body_low(c)))
    points.sort(key=lambda x: x[1])

    zones: list[PipAOI] = []
    used: set[int] = set()
    n = len(points)
    i = 0
    while i < n:
        if i in used:
            i += 1
            continue
        # Grow a cluster from points[i] within max_w.
        j = i
        while j + 1 < n and points[j + 1][1] - points[i][1] <= max_w:
            j += 1
        cluster_pts = points[i : j + 1]
        # Tighten to densest sub-window that still has min_touches and >= min_w.
        best: PipAOI | None = None
        for left in range(len(cluster_pts)):
            for right in range(left + min_touches - 1, len(cluster_pts)):
                lo = cluster_pts[left][1]
                hi = cluster_pts[right][1]
                width = hi - lo
                if width < min_w or width > max_w:
                    continue
                idxs = [cluster_pts[k][0] for k in range(left, right + 1)]
                unique_bars = sorted(set(idxs))
                if len(unique_bars) < min_touches:
                    continue
                # Prefer denser, more recent clusters.
                mid = (lo + hi) / 2.0
                # Classify by whether price spent more time above/below mid later.
                kind = "support"
                after = candles[unique_bars[-1] : end + 1]
                if after:
                    above = sum(1 for c in after if c.close > mid)
                    below = len(after) - above
                    kind = "resistance" if above < below else "support"
                cand = PipAOI(
                    low=lo,
                    high=hi,
                    kind=kind,
                    touches=len(unique_bars),
                    first_touch_index=unique_bars[0],
                    last_touch_index=unique_bars[-1],
                    source_tf=source_tf,
                )
                if best is None or cand.touches > best.touches or (
                    cand.touches == best.touches
                    and cand.last_touch_index > best.last_touch_index
                ):
                    best = cand
        if best is not None:
            # De-dupe near-identical zones.
            if not any(
                abs(z.level - best.level) <= cluster and z.kind == best.kind
                for z in zones
            ):
                zones.append(best)
                for k in range(i, j + 1):
                    used.add(k)
        i += 1

    zones.sort(key=lambda z: z.last_touch_index, reverse=True)
    return zones


def aoi_at_close(
    candle: Candle,
    zones: list[PipAOI],
    *,
    pad: float = 0.0,
) -> PipAOI | None:
    """Return AOI if close is inside a zone (entry condition).

    ``pad`` expands each zone by that price amount on both sides — useful when
    comparing feeds whose closes differ by a few pips.
    """
    for zone in zones:
        lo = zone.low - pad
        hi = zone.high + pad
        if lo <= candle.close <= hi:
            return zone
    return None


def stop_beyond_aoi(
    zone: PipAOI,
    action: str,
    *,
    symbol: str = "EURUSD=X",
    buffer_pips: float = 6.0,
) -> float:
    """SL 5–7 pips beyond the AOI (default 6)."""
    buf = buffer_pips * _pip_size(symbol)
    if action == "buy":
        return zone.low - buf
    return zone.high + buf
