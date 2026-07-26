from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.strategy6 import AlexG6Strategy
from borex.alexg.strategy4 import _PendingSetup
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction


@dataclass
class AlexG6aStrategy(AlexG6Strategy):
    """
    AlexG6a — enter at bar close after an SL-touch retest that holds.

    Ghost waits for SL to be tagged. Decision happens only when that bar
    closes (the moment the simulation "knows" the candle):
    - long: close >= SL
    - short: close <= SL
    If the close fails the hold, cancel the ghost (no trade).

    Entry price is the trigger bar's **close** (current price at decision),
    not the earlier SL wick. SL/TP are re-anchored from that fill.
    """

    name: str = "alexg6a"

    def _fill_bar_closes_in_favor(
        self,
        action: SignalAction,
        sl_price: float,
        candle: Candle,
    ) -> bool:
        if action == SignalAction.BUY:
            return candle.close >= sl_price
        if action == SignalAction.SELL:
            return candle.close <= sl_price
        return False

    def _entry_signal(
        self,
        pending: _PendingSetup,
        index: int,
        candles: list[Candle],
    ) -> Signal:
        # Realistic: we only know to enter at bar close → fill at close.
        fill_price = candles[index].close
        stop_loss, take_profit = self._stops_from_late_entry(pending, fill_price)
        return Signal(
            action=pending.action,
            pattern=f"{pending.pattern}|{self._ghost_tag(pending)}|fill:close",
            index=index,
            price=fill_price,
            timestamp=candles[index].timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
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
                    candle = candles[index]
                    sl = pending.stop_loss
                    if self._fill_bar_closes_in_favor(pending.action, sl, candle):
                        del self._pending[symbol]
                        self._last_signal_index[symbol] = index
                        return self._entry_signal(pending, index, candles)
                    # Wick tagged SL but close rejected the move — abort ghost.
                    del self._pending[symbol]
                    return None
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
