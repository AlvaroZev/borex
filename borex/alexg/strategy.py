from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.aoi import build_aoi_zones, recent_aoi_touch
from borex.alexg.break_retest import BreakRetestState, update_break_retest
from borex.alexg.confirmation import bearish_confirmation, bullish_confirmation
from borex.alexg.patterns import detect_double_top, detect_head_shoulders
from borex.alexg.risk import (
    apply_sl_multiplier,
    passes_rr_filter,
    structure_stop_loss,
    structure_take_profit,
)
from borex.alexg.scoring import compute_score
from borex.alexg.swings import detect_swings
from borex.alexg.trend import Trend, detect_trend_context
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction
from borex.strategy.base import Strategy


@dataclass
class AlexGMethodStrategy(Strategy):
    """
    AlexG Method — sistema de confluencia en 4 pilares:
    Trend (estructura + continuación) + AOI + Break & Retest + Confirmación.

    Solo genera señales cuando los cuatro pilares alinean, score >= min_score
    y RR >= min_rr.
    """

    name: str = "alexg_method"
    min_score: float = 70.0
    min_rr: float = 3.0
    max_tp_pct: float | None = None
    sl_mult: float = 1.0
    swing_lookback: int = 5
    aoi_tolerance_pct: float = 0.002
    min_aoi_touches: int = 3
    min_bars: int = 80

    _break_retest: BreakRetestState = field(default_factory=BreakRetestState, repr=False)
    _last_signal_index: int = field(default=-999, repr=False)

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        if index < self.min_bars:
            return None

        window = candles[: index + 1]
        swings = detect_swings(window, self.swing_lookback)
        if len(swings) < 4:
            return None

        trend_ctx = detect_trend_context(swings, window, index)
        if not trend_ctx.is_valid:
            return None

        trend = trend_ctx.trend

        zones = build_aoi_zones(
            swings,
            window,
            trend,
            self.aoi_tolerance_pct,
            self.min_aoi_touches,
        )
        if not zones:
            return None

        aoi = recent_aoi_touch(
            window, index, zones, lookback=25, tolerance_pct=self.aoi_tolerance_pct
        )
        br = update_break_retest(
            window,
            index,
            zones,
            self._break_retest,
            self.aoi_tolerance_pct,
        )

        bull_conf = bullish_confirmation(window, index)
        bear_conf = bearish_confirmation(window, index)

        double_top = detect_double_top(swings, trend, self.aoi_tolerance_pct)
        head_shoulders = detect_head_shoulders(swings, window, index)
        pattern_tag = ""
        if double_top:
            pattern_tag = "double_top"
        elif head_shoulders:
            pattern_tag = "head_shoulders"

        if trend == Trend.BULLISH:
            trend_ok = trend_ctx.is_valid
            aoi_ok = aoi is not None and aoi.kind == "support"
            br_ok = br.bullish_confirmed or (
                self._break_retest.bullish_retest_ready and aoi_ok
            )
            conf_ok = bull_conf is not None
            action = SignalAction.BUY
            conf_name = bull_conf or ""
        else:
            trend_ok = trend_ctx.is_valid
            aoi_ok = aoi is not None and aoi.kind == "resistance"
            br_ok = br.bearish_confirmed or (
                self._break_retest.bearish_retest_ready and aoi_ok
            )
            conf_ok = bear_conf is not None
            action = SignalAction.SELL
            conf_name = bear_conf or ""

        if not (trend_ok and aoi_ok and br_ok and conf_ok):
            return None

        pattern_present = bool(pattern_tag)
        score = compute_score(
            trend_ok,
            aoi_ok,
            br_ok,
            conf_ok,
            pattern_present,
            mtf,
            index,
            action,
        )

        # MTF es confluencia (hasta +20 pts), no bloqueo duro salvo score mínimo
        if mtf is not None and score.mtf < 10.0:
            return None

        if score.total < self.min_score:
            return None

        entry = candles[index].close
        sl = structure_stop_loss(window, index, action, br.broken_level)
        sl = apply_sl_multiplier(entry, sl, action, self.sl_mult)
        tp = structure_take_profit(
            window,
            index,
            action,
            entry,
            sl,
            self.min_rr,
            swings,
            max_tp_pct=self.max_tp_pct,
        )

        if not passes_rr_filter(entry, sl, tp, self.min_rr):
            return None

        if index - self._last_signal_index < 10:
            return None
        self._last_signal_index = index

        label_parts = ["alexg", score.grade]
        if trend_ctx.continuation_pattern:
            label_parts.append(trend_ctx.continuation_pattern)
        label_parts.append(conf_name)
        if pattern_tag:
            label_parts.append(pattern_tag)
        pattern = "|".join(p for p in label_parts if p)

        return Signal(
            action=action,
            pattern=pattern,
            index=index,
            price=entry,
            timestamp=candles[index].timestamp,
            stop_loss=sl,
            take_profit=tp,
            score=score.total,
        )
