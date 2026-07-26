from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.aoi2 import (
    build_bidirectional_aoi,
    next_aoi_target,
    recent_aoi_at_bar,
    scale_tp_toward_target,
    stops_from_aoi_tp,
)
from borex.alexg.currency_strength import parse_pair
from borex.alexg.risk import structure_stop_loss
from borex.alexg.strategy2 import (
    _current_trend,
    _entry_at_aoi,
    passes_confirmation_quality_filter,
)
from borex.alexg.strategy6 import AlexG6Strategy
from borex.alexg.swings import detect_swings
from borex.alexg.trend import Trend
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal


@dataclass
class AlexG6_1mStrategy(AlexG6Strategy):
    """
    AlexG6 tuned for 1-minute bars.

    Hour-bar defaults are scaled ~×60 so wall-clock windows match alexg6 on 1h:
    - swing fractal ~1h (60 bars) rather than 5 minutes
    - SL wait ~72 hours, cooldown ~8 hours, min history ~80 hours
    Structure scans use a rolling window so 1m backtests stay tractable.
    """

    name: str = "alexg6-1m"
    # ~1h swing fractal on 1m (full ×60 of swing_lookback=5 would be 300 and very heavy)
    swing_lookback: int = 60
    signal_cooldown: int = 480  # 8h
    sl_wait_max_bars: int = 4320  # 72h
    min_bars: int = 4800  # 80h
    strength_lookback: int = 1440  # 24h
    aoi_recent_lookback: int = 1200  # 20h
    structure_window: int = 10_000  # rolling bars for swing/AOI

    def _evaluate_setup(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        symbol = self._current_symbol or "UNKNOWN"

        if index < self.min_bars:
            return None

        start = max(0, index + 1 - self.structure_window)
        window = candles[start : index + 1]
        wi = len(window) - 1

        swings = detect_swings(window, self.swing_lookback)
        if len(swings) < 4:
            return None

        trend = _current_trend(swings, window, wi)
        if trend == Trend.NEUTRAL:
            return None

        zones = build_bidirectional_aoi(
            swings,
            window,
            self.aoi_tolerance_pct,
            self.min_aoi_touches,
        )
        if not zones:
            return None

        aoi = recent_aoi_at_bar(
            window,
            wi,
            zones,
            lookback=self.aoi_recent_lookback,
            tolerance_pct=self.aoi_tolerance_pct,
        )
        if aoi is None:
            return None

        entry_info = _entry_at_aoi(trend, aoi, window, wi)
        if entry_info is None:
            return None

        action, setup_kind, signal_name = entry_info
        if signal_name in self.disabled_signals:
            return None
        if self.filter_false_positives and not passes_confirmation_quality_filter(
            window, wi, action, setup_kind, signal_name
        ):
            return None

        if mtf is not None and not mtf.all_filters_align(index, action):
            return None

        ctx = self._market_ctx
        if self.require_currency_filter and ctx is not None:
            if not ctx.allows_trade(symbol, action):
                return None

        try:
            base, quote = parse_pair(symbol)
        except ValueError:
            base, quote = "?", "?"

        entry = candles[index].close
        tp_level = next_aoi_target(entry, zones, action)
        if tp_level is None:
            return None
        tp_level = scale_tp_toward_target(entry, tp_level, action, self.tp_fraction)

        structural_sl = structure_stop_loss(window, wi, action, aoi.level)
        stops = stops_from_aoi_tp(
            entry, tp_level, action, structural_sl, self.min_rr
        )
        if stops is None:
            return None
        sl, tp = stops

        strong = ctx.strongest if ctx else "-"
        weak = ctx.weakest if ctx else "-"
        strength_tag = ctx.strength_summary() if ctx else "na"
        pattern = (
            f"{self.name}|{base}{quote}|{strong}>{weak}|{trend.value}|"
            f"{setup_kind}|{aoi.kind}|{signal_name}|{strength_tag}"
        )

        return Signal(
            action=action,
            pattern=pattern,
            index=index,
            price=entry,
            timestamp=candles[index].timestamp,
            stop_loss=sl,
            take_profit=tp,
            score=self.min_rr,
        )
