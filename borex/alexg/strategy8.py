"""AlexG8 — alexg7 ghost entry + LTF TP-direction confirm at SL fill."""

from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.ltf_confirm import LtfSeries, confirm_intervals
from borex.alexg.strategy7 import AlexG7Strategy
from borex.models.candle import Candle, Signal


@dataclass
class AlexG8Strategy(AlexG7Strategy):
    """
    Same rules as alexg7 (video-2 filters off + ghost SL entry), plus:

    When price tags the ghost SL on the execution TF (typically 1h), require
    lower-TF (1m and/or 1s) evidence at the touch that price is reclaiming
    toward TP — so we don't fill a ghost that is still driving through the stop.

    Confirmation uses only LTF bars up to the SL-touch bar (no post-touch
    lookahead). Default checks: touch rejection + structure + momentum.
    """

    name: str = "alexg8"
    ltf_intervals: tuple[str, ...] = ("1m",)
    # "any" = at least one available LTF confirms; "all" = every available must.
    ltf_confirm_mode: str = "any"
    # If no LTF series is attached for the symbol: reject the fill (safe default).
    ltf_missing_policy: str = "reject"
    ltf_structure_lookback: int = 120
    ltf_swing_lookback: int = 3
    ltf_confirm_bars: int = 3
    ltf_require_structure: bool = True
    ltf_require_momentum: bool = True
    ltf_require_touch_reject: bool = True

    # symbol -> {interval -> indexed series}
    _ltf_by_symbol: dict[str, dict[str, LtfSeries]] = field(
        default_factory=dict, repr=False
    )

    def attach_ltf(self, ltf_by_symbol: dict[str, dict[str, list[Candle]]]) -> None:
        """Attach preloaded LTF series keyed by symbol then interval."""
        indexed: dict[str, dict[str, LtfSeries]] = {}
        for sym, by_tf in ltf_by_symbol.items():
            indexed[sym] = {
                tf: series if isinstance(series, LtfSeries) else LtfSeries.from_candles(series)
                for tf, series in by_tf.items()
            }
        self._ltf_by_symbol = indexed

    def _confirm_ghost_fill(self, pending, index: int, candles: list[Candle]) -> bool:
        symbol = self._current_symbol or "UNKNOWN"
        by_tf = self._ltf_by_symbol.get(symbol, {})
        ok, _passed = confirm_intervals(
            pending.action,
            by_tf,
            self.ltf_intervals,
            stop_loss=pending.stop_loss,
            htf_candle=candles[index],
            execution_interval=self.execution_interval,
            mode=self.ltf_confirm_mode,
            missing_policy=self.ltf_missing_policy,
            structure_lookback=self.ltf_structure_lookback,
            swing_lookback=self.ltf_swing_lookback,
            confirm_bars=self.ltf_confirm_bars,
            require_structure=self.ltf_require_structure,
            require_momentum=self.ltf_require_momentum,
            require_touch_reject=self.ltf_require_touch_reject,
        )
        return ok

    def _entry_signal(self, pending, index: int, candles: list[Candle]) -> Signal:
        signal = super()._entry_signal(pending, index, candles)
        return Signal(
            action=signal.action,
            pattern=f"{signal.pattern}|ltf_ok",
            index=signal.index,
            price=signal.price,
            timestamp=signal.timestamp,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            score=signal.score,
        )
