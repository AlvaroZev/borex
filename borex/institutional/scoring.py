from __future__ import annotations

from dataclasses import dataclass

from borex.data.mtf import MultiTimeframeContext
from borex.institutional.fvg import FairValueGap
from borex.institutional.liquidity import LiquiditySweep
from borex.institutional.session import TradingSession, is_institutional_session
from borex.institutional.structure import StructureBias, StructureEvent
from borex.institutional.vwap import VwapState
from borex.models.candle import SignalAction


@dataclass
class InstitutionalScore:
    total: float
    liquidity: float
    vwap: float
    structure: float
    fvg: float
    volume: float
    session: float
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
    action: SignalAction,
    sweep: LiquiditySweep | None,
    vwap: VwapState | None,
    structure_bias: StructureBias,
    bos: StructureEvent | None,
    fvg: FairValueGap | None,
    volume_ok: bool,
    session: TradingSession,
    mtf: MultiTimeframeContext | None,
    index: int,
) -> InstitutionalScore:
    liq_pts = 0.0
    if sweep is not None:
        if action == SignalAction.BUY and sweep.direction == "bullish":
            liq_pts = 25.0
        elif action == SignalAction.SELL and sweep.direction == "bearish":
            liq_pts = 25.0

    vwap_pts = 0.0
    if vwap is not None:
        if action == SignalAction.BUY and vwap.deviation_pct < 0:
            vwap_pts = 20.0
        elif action == SignalAction.SELL and vwap.deviation_pct > 0:
            vwap_pts = 20.0
        elif abs(vwap.deviation_pct) < 0.001:
            vwap_pts = 10.0

    struct_pts = 0.0
    if action == SignalAction.BUY and structure_bias == StructureBias.BULLISH:
        struct_pts = 15.0
    elif action == SignalAction.SELL and structure_bias == StructureBias.BEARISH:
        struct_pts = 15.0
    if bos is not None:
        if action == SignalAction.BUY and bos.bias == StructureBias.BULLISH:
            struct_pts = min(20.0, struct_pts + 5.0)
        elif action == SignalAction.SELL and bos.bias == StructureBias.BEARISH:
            struct_pts = min(20.0, struct_pts + 5.0)

    fvg_pts = 15.0 if fvg is not None else 0.0
    vol_pts = 10.0 if volume_ok else 0.0
    sess_pts = 10.0 if is_institutional_session(session) else 0.0

    mtf_pts = 0.0
    if mtf is not None and mtf.filter_intervals:
        aligned = 0
        for interval in mtf.filter_intervals:
            c = mtf.filter_candle_at(index, interval)
            if c is None:
                continue
            if action == SignalAction.BUY and c.is_bullish:
                aligned += 1
            elif action == SignalAction.SELL and c.is_bearish:
                aligned += 1
        mtf_pts = (aligned / len(mtf.filter_intervals)) * 20.0

    total = liq_pts + vwap_pts + struct_pts + fvg_pts + vol_pts + sess_pts + mtf_pts
    return InstitutionalScore(
        total=total,
        liquidity=liq_pts,
        vwap=vwap_pts,
        structure=struct_pts,
        fvg=fvg_pts,
        volume=vol_pts,
        session=sess_pts,
        mtf=mtf_pts,
        grade=grade_score(total),
    )
