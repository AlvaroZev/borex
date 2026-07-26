from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.strategy6 import AlexG6Strategy
from borex.alexg.strategy4 import _PendingSetup
from borex.models.candle import Candle, Signal


@dataclass
class AlexG6bStrategy(AlexG6Strategy):
    """
    AlexG6b — delay true/margin SL arming until the bar after fill.

    Late entry still fills when ghost SL is touched, but stop-loss / margin-stop
    protection starts on the next bar so the fill-bar adverse wick cannot wipe
    the position immediately. TP remains active on the fill bar.

    Also tags flip (immediate) entries — those used to bypass `_entry_signal`
    and skip the delay.
    """

    name: str = "alexg6b"
    margin_sl_delay_bars: int = 1

    def _with_msl_delay(self, signal: Signal) -> Signal:
        delay = max(0, int(self.margin_sl_delay_bars))
        if delay <= 0:
            return signal
        tag = f"msl_delay:{delay}"
        if f"|{tag}" in signal.pattern or signal.pattern.endswith(tag):
            return signal
        return Signal(
            action=signal.action,
            pattern=f"{signal.pattern}|{tag}",
            index=signal.index,
            price=signal.price,
            timestamp=signal.timestamp,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            score=signal.score,
        )

    def _entry_signal(
        self,
        pending: _PendingSetup,
        index: int,
        candles: list[Candle],
    ) -> Signal:
        return self._with_msl_delay(super()._entry_signal(pending, index, candles))

    def _immediate_signal(
        self,
        setup: Signal,
        index: int,
        candles: list[Candle],
    ) -> Signal:
        return self._with_msl_delay(super()._immediate_signal(setup, index, candles))
