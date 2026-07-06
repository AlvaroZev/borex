from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.aoi import AOI, _tolerance, price_at_aoi
from borex.alexg.swings import SwingPoint
from borex.models.candle import Candle, SignalAction


@dataclass
class AOIZone(AOI):
    """AOI with recency score (higher = more recent touches)."""

    last_touch_index: int = 0

    @property
    def recency(self) -> int:
        return self.last_touch_index


def _zone_from_swings(
    swings: list[SwingPoint],
    candles: list[Candle],
    kind: str,
    tolerance_pct: float,
    min_touches: int,
    max_candidates: int = 12,
) -> list[AOIZone]:
    swing_kind = "low" if kind == "support" else "high"
    candidates = [s for s in swings if s.kind == swing_kind][-max_candidates:]
    zones: list[AOIZone] = []
    seen_levels: list[float] = []

    for swing in candidates:
        tol = _tolerance(swing.price, tolerance_pct)
        if any(abs(swing.price - lvl) <= tol * 2 for lvl in seen_levels):
            continue

        touches = 0
        touch_indices: list[int] = []
        for i, c in enumerate(candles):
            t = _tolerance(swing.price, tolerance_pct)
            if kind == "support" and c.low <= swing.price + t:
                touches += 1
                touch_indices.append(i)
            elif kind == "resistance" and c.high >= swing.price - t:
                touches += 1
                touch_indices.append(i)

        if touches >= min_touches:
            seen_levels.append(swing.price)
            zones.append(
                AOIZone(
                    level=swing.price,
                    kind=kind,
                    touches=touches,
                    swing_indices=touch_indices[-min_touches:],
                    last_touch_index=max(touch_indices) if touch_indices else swing.index,
                )
            )

    zones.sort(key=lambda z: z.recency, reverse=True)
    return zones


def build_bidirectional_aoi(
    swings: list[SwingPoint],
    candles: list[Candle],
    tolerance_pct: float = 0.002,
    min_touches: int = 2,
) -> list[AOIZone]:
    """Support and resistance zones; recent levels listed first."""
    supports = _zone_from_swings(
        swings, candles, "support", tolerance_pct, min_touches
    )
    resistances = _zone_from_swings(
        swings, candles, "resistance", tolerance_pct, min_touches
    )
    all_zones = supports + resistances
    all_zones.sort(key=lambda z: z.recency, reverse=True)
    return all_zones


def recent_aoi_at_bar(
    candles: list[Candle],
    index: int,
    zones: list[AOIZone],
    lookback: int = 20,
    tolerance_pct: float = 0.002,
) -> AOIZone | None:
    """AOI touched recently; prefers the most recently active zone."""
    start = max(0, index - lookback)
    best: AOIZone | None = None
    best_touch = -1

    for i in range(start, index + 1):
        hit = price_at_aoi(candles[i], zones, tolerance_pct)
        if hit is None:
            continue
        zone = next((z for z in zones if z.level == hit.level and z.kind == hit.kind), None)
        if zone is None:
            continue
        if i >= best_touch:
            best_touch = i
            best = zone

    return best


def next_aoi_target(
    entry: float,
    zones: list[AOIZone],
    action: SignalAction,
) -> float | None:
    """Next AOI in trade direction (TP target)."""
    if action == SignalAction.BUY:
        above = [z for z in zones if z.kind == "resistance" and z.level > entry]
        if not above:
            return None
        return min(z.level for z in above)

    below = [z for z in zones if z.kind == "support" and z.level < entry]
    if not below:
        return None
    return max(z.level for z in below)


def stops_from_aoi_tp(
    entry: float,
    take_profit: float,
    action: SignalAction,
    structural_sl: float,
    min_rr: float,
) -> tuple[float, float] | None:
    """
    TP is fixed at the next AOI. SL is derived for RR >= min_rr,
    but cannot be tighter than the structural invalidation level.
    """
    reward = abs(take_profit - entry)
    if reward <= 0:
        return None

    if action == SignalAction.BUY:
        calc_sl = entry - reward / min_rr
        if calc_sl > structural_sl:
            risk = entry - structural_sl
            if risk <= 0 or reward / risk < min_rr:
                return None
            return structural_sl, take_profit
        return calc_sl, take_profit

    calc_sl = entry + reward / min_rr
    if calc_sl < structural_sl:
        risk = structural_sl - entry
        if risk <= 0 or reward / risk < min_rr:
            return None
        return structural_sl, take_profit
    return calc_sl, take_profit
