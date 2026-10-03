"""AlexG10 — stop-run harvest: ghost entry → ghost SL as take-profit.

Same setup detection as alexg8 (AOI + ghost queue), but:

  * Wait for price to tag the **planned ghost entry** (not the ghost SL).
  * Enter **opposite** the original setup (trade toward the stop).
  * Take-profit = original ghost SL.
  * Protective stop = 1R mirrored on the far side of entry
    (``SL = 2*entry - ghost_SL``).

Example: alexg8 would queue BUY at E with SL=S (S<E). AlexG10 shorts at E
with TP=S and SL=2E−S.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.ablation import AblationConfig, video2_ghost_all_sessions
from borex.alexg.ghost_entry import PendingSetup
from borex.alexg.strategy8 import AlexG8Strategy
from borex.models.candle import Candle, Signal, SignalAction


@dataclass
class AlexG10Strategy(AlexG8Strategy):
    """alexg8 geometry; fill at planned entry toward ghost SL (flipped)."""

    name: str = "alexg10"
    ablation: AblationConfig = field(default_factory=video2_ghost_all_sessions)
    # Entry is at planned_entry; H1-close fill matches Live/alexg8 path.
    ghost_fill_at_close: bool = True

    def _entry_touched(self, pending: PendingSetup, candle: Candle) -> bool:
        """Price traded through the planned ghost entry."""
        pad = self._sl_touch_pad()
        e = float(pending.planned_entry)
        return float(candle.low) <= e + pad and float(candle.high) >= e - pad

    def _pending_outcome(
        self,
        pending: PendingSetup,
        index: int,
        candles: list[Candle],
    ) -> str:
        if index > pending.expires_index:
            return "expired"

        candle = candles[index]

        # Original TP hit first → bounce completed, cancel stop-run idea.
        if self._tp_touched(pending, candle):
            return "invalidated"

        # Ghost SL tagged before we ever tagged entry → missed the path.
        if self._sl_touched(pending, candle) and not self._entry_touched(
            pending, candle
        ):
            return "invalidated"

        if self._entry_touched(pending, candle):
            return "triggered"

        return "waiting"

    def _entry_signal(
        self,
        pending: PendingSetup,
        index: int,
        candles: list[Candle],
    ) -> Signal:
        candle = candles[index]
        fill_at_close = bool(getattr(self, "ghost_fill_at_close", False))
        entry = float(pending.planned_entry)
        ghost_sl = float(pending.stop_loss)
        fill_price = float(candle.close) if fill_at_close else entry

        # Flip: trade from entry toward ghost SL.
        if pending.action == SignalAction.BUY:
            action = SignalAction.SELL
        else:
            action = SignalAction.BUY

        risk = abs(entry - ghost_sl)
        if risk <= 0:
            risk = abs(fill_price) * 1e-4

        # TP is the ghost SL level (structural). SL is 1R on the far side of fill.
        take_profit = ghost_sl
        if action == SignalAction.SELL:
            stop_loss = fill_price + risk
        else:
            stop_loss = fill_price - risk

        extra = "|fill:close" if fill_at_close else "|fill:entry"
        return Signal(
            action=action,
            pattern=(
                f"{pending.pattern}|alexg10|entry_to_gsl|"
                f"{self._ghost_tag(pending)}{extra}"
            ),
            index=index,
            price=fill_price,
            timestamp=candle.timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
            score=1.0,
        )
