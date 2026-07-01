from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction
from borex.patterns import candlestick as pat
from borex.patterns.candlestick import avg_body
from borex.strategy.base import Strategy

FilterMode = Literal["trend", "off"]


@dataclass
class CandlePatternStrategy(Strategy):
    """
    Estrategia basada en patrones de velas japonesas.

    Puedes activar/desactivar patrones y definir cuáles son alcistas o bajistas.
    Con MTF activo, filtra señales según la vela cerrada del timeframe superior.
    """

    name: str = "candle_patterns"
    filter_mode: FilterMode = "trend"
    enabled_patterns: set[str] = field(
        default_factory=lambda: {
            "hammer",
            "shooting_star",
            "bullish_engulfing",
            "bearish_engulfing",
            "morning_star",
            "evening_star",
            "three_white_soldiers",
            "three_black_crows",
        }
    )

    def _passes_mtf_filter(
        self,
        action: SignalAction,
        mtf: MultiTimeframeContext | None,
        index: int,
    ) -> bool:
        if mtf is None or self.filter_mode == "off":
            return True

        if self.filter_mode == "trend":
            return mtf.all_filters_align(index, action)

        return True

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        if index < 2:
            return None

        curr = candles[index]
        prev = candles[index - 1]
        avg = avg_body(candles[: index + 1])

        checks: list[tuple[str, SignalAction, bool]] = [
            ("hammer", SignalAction.BUY, pat.is_hammer(curr, avg)),
            ("shooting_star", SignalAction.SELL, pat.is_shooting_star(curr, avg)),
            (
                "bullish_engulfing",
                SignalAction.BUY,
                pat.is_bullish_engulfing(prev, curr),
            ),
            (
                "bearish_engulfing",
                SignalAction.SELL,
                pat.is_bearish_engulfing(prev, curr),
            ),
            (
                "morning_star",
                SignalAction.BUY,
                pat.is_morning_star(candles[index - 2], prev, curr, avg),
            ),
            (
                "evening_star",
                SignalAction.SELL,
                pat.is_evening_star(candles[index - 2], prev, curr, avg),
            ),
            (
                "three_white_soldiers",
                SignalAction.BUY,
                pat.is_three_white_soldiers(
                    candles[index - 2], prev, curr, avg
                ),
            ),
            (
                "three_black_crows",
                SignalAction.SELL,
                pat.is_three_black_crows(
                    candles[index - 2], prev, curr, avg
                ),
            ),
        ]

        for pattern_name, action, matched in checks:
            if matched and pattern_name in self.enabled_patterns:
                if not self._passes_mtf_filter(action, mtf, index):
                    continue
                return Signal(
                    action=action,
                    pattern=pattern_name,
                    index=index,
                    price=curr.close,
                    timestamp=curr.timestamp,
                )

        return None
