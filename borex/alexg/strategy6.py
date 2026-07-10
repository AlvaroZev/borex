from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from borex.alexg.strategy5 import AlexG5Strategy
from borex.alexg.strategy4 import _PendingSetup
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction

SecondSignalMode = Literal["off", "flip", "replace"]


@dataclass
class AlexG6Strategy(AlexG5Strategy):
    """
    AlexG6 — AlexG5 with opposite-signal handling while waiting for SL retest.

    While a pending ghost trade waits for the original SL:
    - off: cancel if an opposite setup appears (default)
    - flip: enter immediately in the new signal direction
    - replace: drop the old ghost, queue a new ghost in the opposite direction
      and wait for its SL retest before entering
    """

    name: str = "alexg6"
    second_signal: SecondSignalMode = "off"

    def _actions_opposite(self, left: SignalAction, right: SignalAction) -> bool:
        if left == SignalAction.HOLD or right == SignalAction.HOLD:
            return False
        return left != right

    def _immediate_signal(self, setup: Signal, index: int, candles: list[Candle]) -> Signal:
        return Signal(
            action=setup.action,
            pattern=setup.pattern,
            index=index,
            price=setup.price,
            timestamp=candles[index].timestamp,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            score=setup.score,
        )

    def _queue_pending(self, setup: Signal, index: int) -> None:
        symbol = self._current_symbol or "UNKNOWN"
        self._pending[symbol] = self._pending_setup_from_signal(setup, index)
        self._last_signal_index[symbol] = index

    def _pending_setup_from_signal(self, setup: Signal, index: int) -> _PendingSetup:
        return _PendingSetup(
            action=setup.action,
            pattern=setup.pattern,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            planned_entry=setup.price,
            created_index=index,
            expires_index=index + self.sl_wait_max_bars,
        )

    def _handle_opposite_setup(
        self,
        pending,
        setup: Signal,
        index: int,
        candles: list[Candle],
    ) -> Signal | None:
        symbol = self._current_symbol or "UNKNOWN"

        if self.second_signal == "off":
            del self._pending[symbol]
            return None

        if self.second_signal == "flip":
            del self._pending[symbol]
            self._last_signal_index[symbol] = index
            return self._immediate_signal(setup, index, candles)

        # replace: new ghost, wait for its SL
        self._pending[symbol] = self._pending_setup_from_signal(setup, index)
        self._last_signal_index[symbol] = index
        return None

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        symbol = self._current_symbol or "UNKNOWN"

        if index < self.min_bars:
            return None

        pending = self._pending.get(symbol)
        if pending is not None:
            if index > pending.expires_index:
                del self._pending[symbol]
            elif self._tp_touched(pending, candles[index]):
                del self._pending[symbol]
            else:
                opposite = self._evaluate_setup(index, candles, mtf)
                if (
                    opposite is not None
                    and opposite.stop_loss is not None
                    and opposite.take_profit is not None
                    and self._actions_opposite(pending.action, opposite.action)
                ):
                    signal = self._handle_opposite_setup(
                        pending, opposite, index, candles
                    )
                    if signal is not None:
                        return signal
                    if symbol not in self._pending:
                        return None
                    pending = self._pending[symbol]
                elif pending is not None and self._sl_touched(pending, candles[index]):
                    del self._pending[symbol]
                    self._last_signal_index[symbol] = index
                    return self._entry_signal(pending, index, candles)
                elif pending is not None:
                    if self._in_near_sl_zone(pending, candles[index]):
                        pending.saw_near_sl = True
                    if pending.saw_near_sl and self._left_near_sl_zone(
                        pending, candles[index]
                    ):
                        del self._pending[symbol]

        if symbol in self._pending:
            return None

        last = self._last_signal_index.get(symbol, -999)
        if index - last < self.signal_cooldown:
            return None

        setup = self._evaluate_setup(index, candles, mtf)
        if setup is None or setup.stop_loss is None or setup.take_profit is None:
            return None

        self._queue_pending(setup, index)
        return None
