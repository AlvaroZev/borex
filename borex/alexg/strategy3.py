from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
from borex.alexg.swings import detect_swings
from borex.alexg.trend import Trend
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction
from borex.strategy.base import Strategy

if TYPE_CHECKING:
    from borex.alexg.multi_market import MultiMarketContext


@dataclass
class AlexG3Strategy(Strategy):
    """
    AlexG3 — AlexG2 entries filtered by cross-market currency strength.

    - Same AOI / candlestick logic as AlexG2.
    - Reads all FX pairs at once: which currency is strengthening vs weakening.
    - Only long when base currency is relatively strong; only short when weak.
    - Designed for multi-market engine (many pairs, shared portfolio).
    """

    name: str = "alexg3"
    min_rr: float = 2.0
    tp_fraction: float = 1.0
    swing_lookback: int = 5
    aoi_tolerance_pct: float = 0.002
    min_aoi_touches: int = 2
    min_bars: int = 80
    signal_cooldown: int = 8
    strength_lookback: int = 24
    min_currency_edge: float = 0.00005
    min_confirming_pairs: int = 2
    require_currency_filter: bool = True
    filter_false_positives: bool = True
    disabled_signals: tuple[str, ...] = ()

    _last_signal_index: dict[str, int] = field(default_factory=dict, repr=False)
    _current_symbol: str = field(default="", repr=False)
    _market_ctx: MultiMarketContext | None = field(default=None, repr=False)

    def set_context(
        self,
        symbol: str,
        market_ctx: MultiMarketContext | None,
    ) -> None:
        self._current_symbol = symbol
        self._market_ctx = market_ctx

    def _evaluate_setup(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        symbol = self._current_symbol or "UNKNOWN"

        if index < self.min_bars:
            return None

        window = candles[: index + 1]
        swings = detect_swings(window, self.swing_lookback)
        if len(swings) < 4:
            return None

        trend = _current_trend(swings, window, index)
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
            window, index, zones, lookback=20, tolerance_pct=self.aoi_tolerance_pct
        )
        if aoi is None:
            return None

        entry_info = _entry_at_aoi(trend, aoi, window, index)
        if entry_info is None:
            return None

        action, setup_kind, signal_name = entry_info
        if signal_name in self.disabled_signals:
            return None
        if self.filter_false_positives and not passes_confirmation_quality_filter(
            window, index, action, setup_kind, signal_name
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

        structural_sl = structure_stop_loss(window, index, action, aoi.level)
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

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        symbol = self._current_symbol or "UNKNOWN"

        if index < self.min_bars:
            return None

        last = self._last_signal_index.get(symbol, -999)
        if index - last < self.signal_cooldown:
            return None

        signal = self._evaluate_setup(index, candles, mtf)
        if signal is None:
            return None

        self._last_signal_index[symbol] = index
        return signal
