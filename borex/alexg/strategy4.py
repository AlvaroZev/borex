from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.ghost_entry import GhostSLEntryMixin, PendingSetup
from borex.alexg.strategy3 import AlexG3Strategy
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal

# Ghost machinery now lives in ghost_entry so alexg5revised can share it.
_PendingSetup = PendingSetup


@dataclass
class AlexG4Strategy(GhostSLEntryMixin, AlexG3Strategy):
    """
    AlexG4 — AlexG3 setup detection with SL-retest entry.

    When AlexG3 would fire, the trade is queued with its SL and TP.
    Entry happens only if price later touches the planned SL (limit fill).
    On fill, SL/TP are shifted from the new entry keeping the same risk/reward
    distances as the original plan (not the old absolute prices).
    Skip (no trade) if: SL is never touched, TP is hit first, or price only
    approaches SL without a fill (near-miss then leaves the zone).
    """

    name: str = "alexg4"

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
            outcome = self._pending_outcome(pending, index, candles)
            if outcome == "triggered":
                if not self._confirm_ghost_fill(pending, index, candles):
                    del self._pending[symbol]
                    return None
                del self._pending[symbol]
                self._last_signal_index[symbol] = index
                return self._entry_signal(pending, index, candles)
            if outcome in ("expired", "invalidated"):
                del self._pending[symbol]

        if symbol in self._pending:
            return None

        last = self._last_signal_index.get(symbol, -999)
        if index - last < self.signal_cooldown:
            return None

        setup = self._evaluate_setup(index, candles, mtf)
        if setup is None or setup.stop_loss is None or setup.take_profit is None:
            return None

        self._pending[symbol] = _PendingSetup(
            action=setup.action,
            pattern=setup.pattern,
            stop_loss=self._scaled_ghost_sl(setup.price, setup.stop_loss, setup.action),
            take_profit=setup.take_profit,
            planned_entry=setup.price,
            created_index=index,
            expires_index=index + self.sl_wait_max_bars,
        )
        self._last_signal_index[symbol] = index
        return None

