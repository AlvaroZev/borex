from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.confirmation import bearish_confirmation, bullish_confirmation
from borex.alexg.confluence import ConfluenceWeights, score_confluence
from borex.alexg.impulse import impulse_strength
from borex.alexg.patterns import detect_head_shoulders, detect_inverse_head_shoulders
from borex.alexg.strategy6 import AlexG6Strategy, SecondSignalMode
from borex.alexg.swings import detect_swings
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction


@dataclass
class AlexGMarketStrategy(AlexG6Strategy):
    """
    AlexG Market — full market-strategy brief on top of AlexG6 (cancel on opposite).

    Maps to the trading brief:
    1. Trend (structure) — only trade with current direction
    2. Areas of interest — recent levels preferred
    3. Candlestick entry confirmation (rejection / continuation)
    4. Wait for confirmation at AOI (no prediction)
    5. TP at next AOI; structural RR >= min_rr (default 2)
    6. Multi-market currency strength (AlexG3+)
    7. Confirmation quality timing filter
    8. Head & shoulders / inverse H&S as confluence
    9. Impulse strength (smoothed body×activity; does not snap to 0)
    10. Late entry at planned SL + margin-stop exits (AlexG4/5)
    11. While ghost waits for SL: opposite candle signal or opposite setup cancels

    Extra: weighted confluence score (linear weights; MLP-ready later).
    """

    name: str = "alexg-market"
    second_signal: SecondSignalMode = "off"
    min_rr: float = 2.0
    tp_fraction: float = 1.0
    min_confluence: float = 0.55
    impulse_lookback: int = 20
    impulse_ema_span: int = 14
    impulse_threshold: float = 0.15
    confluence_weights: ConfluenceWeights = field(default_factory=ConfluenceWeights)

    def _opposite_candle_signal(
        self,
        pending_action: SignalAction,
        index: int,
        candles: list[Candle],
    ) -> bool:
        """
        Abort ghost only on a clear opposite confirmation candle.

        Matches cases like: bullish wick ghost, then bearish momentum bar → cancel.
        Ignores weak opposite wicks that often print near the SL zone.
        """
        strong_bearish = {"momentum", "bearish_engulfing", "three_black_crows"}
        strong_bullish = {"momentum", "bullish_engulfing", "three_white_soldiers"}
        if pending_action == SignalAction.BUY:
            name = bearish_confirmation(candles, index)
            return name in strong_bearish
        if pending_action == SignalAction.SELL:
            name = bullish_confirmation(candles, index)
            return name in strong_bullish
        return False

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        symbol = self._current_symbol or "UNKNOWN"
        if index >= self.min_bars:
            pending = self._pending.get(symbol)
            if (
                pending is not None
                and index <= pending.expires_index
                and not self._tp_touched(pending, candles[index])
                # If SL is touched this bar, allow the late fill (super handles it).
                # Only abort on an intermediate opposite candle before the retest.
                and not self._sl_touched(pending, candles[index])
                and self._opposite_candle_signal(pending.action, index, candles)
            ):
                # e.g. bullish ghost at 12:00, bearish momentum at 13:00 -> abort
                # (entry would have been at 14:00)
                del self._pending[symbol]
                return None
        return super().on_bar(index, candles, mtf)

    def _evaluate_setup(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        setup = super()._evaluate_setup(index, candles, mtf)
        if setup is None:
            return None

        window = candles[: index + 1]
        swings = detect_swings(window, self.swing_lookback)
        hs = detect_head_shoulders(swings, window, index, self.aoi_tolerance_pct)
        ihs = detect_inverse_head_shoulders(
            swings, window, index, self.aoi_tolerance_pct
        )

        if setup.action == SignalAction.BUY:
            hs_aligned = ihs
            hs_tag = "inv_hs" if ihs else ("hs_conflict" if hs else "hs_none")
        else:
            hs_aligned = hs
            hs_tag = "hs" if hs else ("inv_hs_conflict" if ihs else "hs_none")

        impulse = impulse_strength(
            candles,
            index,
            lookback=self.impulse_lookback,
            ema_span=self.impulse_ema_span,
        )
        if setup.action == SignalAction.BUY:
            impulse_aligned = impulse >= self.impulse_threshold
        else:
            impulse_aligned = impulse <= -self.impulse_threshold

        ctx = self._market_ctx
        currency_ok = True
        if self.require_currency_filter and ctx is not None:
            currency_ok = ctx.allows_trade(
                self._current_symbol or "UNKNOWN", setup.action
            )

        conf = score_confluence(
            trend_ok=True,
            aoi_ok=True,
            confirmation_ok=True,
            currency_ok=currency_ok,
            hs_aligned=hs_aligned,
            impulse_aligned=impulse_aligned,
            weights=self.confluence_weights,
        )
        if conf < self.min_confluence:
            return None

        return Signal(
            action=setup.action,
            pattern=(
                f"{setup.pattern}|cf:{conf:.2f}|imp:{impulse:+.2f}|{hs_tag}"
            ),
            index=setup.index,
            price=setup.price,
            timestamp=setup.timestamp,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            score=conf,
        )
