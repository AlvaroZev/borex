from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.trend import Trend
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import SignalAction


@dataclass
class ConfluenceScore:
    total: float
    trend: float
    aoi: float
    break_retest: float
    confirmation: float
    pattern: float
    mtf: float
    grade: str


def grade_score(score: float) -> str:
    if score >= 100:
        return "A+"
    if score >= 70:
        return "Valid"
    if score >= 40:
        return "Watchlist"
    return "Ignore"


def compute_score(
    trend_ok: bool,
    aoi_ok: bool,
    break_retest_ok: bool,
    confirmation_ok: bool,
    pattern_present: bool,
    mtf: MultiTimeframeContext | None,
    index: int,
    action: SignalAction,
) -> ConfluenceScore:
    trend_pts = 30.0 if trend_ok else 0.0
    aoi_pts = 20.0 if aoi_ok else 0.0
    br_pts = 20.0 if break_retest_ok else 0.0
    conf_pts = 15.0 if confirmation_ok else 0.0
    pat_pts = 10.0 if pattern_present else 0.0

    mtf_pts = 0.0
    if mtf is not None:
        aligned = 0
        for interval in mtf.filter_intervals:
            c = mtf.filter_candle_at(index, interval)
            if c is None:
                continue
            if action == SignalAction.BUY and c.is_bullish:
                aligned += 1
            elif action == SignalAction.SELL and c.is_bearish:
                aligned += 1
        if mtf.filter_intervals:
            mtf_pts = (aligned / len(mtf.filter_intervals)) * 20.0

    total = trend_pts + aoi_pts + br_pts + conf_pts + pat_pts + mtf_pts
    return ConfluenceScore(
        total=total,
        trend=trend_pts,
        aoi=aoi_pts,
        break_retest=br_pts,
        confirmation=conf_pts,
        pattern=pat_pts,
        mtf=mtf_pts,
        grade=grade_score(total),
    )


def mtf_trend_aligned(
    mtf: MultiTimeframeContext | None,
    index: int,
    trend: Trend,
) -> bool:
    """Todos los TF superiores confirman la dirección macro."""
    if mtf is None:
        return True
    action = SignalAction.BUY if trend == Trend.BULLISH else SignalAction.SELL
    return mtf.all_filters_align(index, action)
