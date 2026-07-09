from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.strategy3 import AlexG3Strategy
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction


@dataclass
class _PendingSetup:
    action: SignalAction
    pattern: str
    stop_loss: float
    take_profit: float
    planned_entry: float
    created_index: int
    expires_index: int
    saw_near_sl: bool = False


@dataclass
class AlexG4Strategy(AlexG3Strategy):
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
    sl_wait_max_bars: int = 72
    sl_near_risk_fraction: float = 0.25

    _pending: dict[str, _PendingSetup] = field(default_factory=dict, repr=False)

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
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            planned_entry=setup.price,
            created_index=index,
            expires_index=index + self.sl_wait_max_bars,
        )
        self._last_signal_index[symbol] = index
        return None

    def _sl_risk_distance(self, pending: _PendingSetup) -> float:
        return abs(pending.planned_entry - pending.stop_loss)

    def _near_sl_band(self, pending: _PendingSetup) -> float:
        return self._sl_risk_distance(pending) * self.sl_near_risk_fraction

    def _sl_touched(self, pending: _PendingSetup, candle: Candle) -> bool:
        if pending.action == SignalAction.BUY:
            return candle.low <= pending.stop_loss
        return candle.high >= pending.stop_loss

    def _tp_touched(self, pending: _PendingSetup, candle: Candle) -> bool:
        if pending.action == SignalAction.BUY:
            return candle.high >= pending.take_profit
        return candle.low <= pending.take_profit

    def _in_near_sl_zone(self, pending: _PendingSetup, candle: Candle) -> bool:
        band = self._near_sl_band(pending)
        sl = pending.stop_loss
        if pending.action == SignalAction.BUY:
            return sl < candle.low <= sl + band
        return sl - band <= candle.high < sl

    def _left_near_sl_zone(self, pending: _PendingSetup, candle: Candle) -> bool:
        band = self._near_sl_band(pending)
        sl = pending.stop_loss
        if pending.action == SignalAction.BUY:
            return candle.close > sl + band
        return candle.close < sl - band

    def _pending_outcome(
        self,
        pending: _PendingSetup,
        index: int,
        candles: list[Candle],
    ) -> str:
        if index > pending.expires_index:
            return "expired"

        candle = candles[index]

        if self._tp_touched(pending, candle):
            return "invalidated"

        if self._sl_touched(pending, candle):
            return "triggered"

        if self._in_near_sl_zone(pending, candle):
            pending.saw_near_sl = True

        if pending.saw_near_sl and self._left_near_sl_zone(pending, candle):
            return "invalidated"

        return "waiting"

    def _stops_from_late_entry(
        self,
        pending: _PendingSetup,
        fill_price: float,
    ) -> tuple[float, float]:
        """Same risk/reward distances as the original plan, from the fill price."""
        risk = abs(pending.planned_entry - pending.stop_loss)
        reward = abs(pending.take_profit - pending.planned_entry)
        if pending.action == SignalAction.BUY:
            return fill_price - risk, fill_price + reward
        return fill_price + risk, fill_price - reward

    def _entry_signal(
        self,
        pending: _PendingSetup,
        index: int,
        candles: list[Candle],
    ) -> Signal:
        fill_price = pending.stop_loss
        stop_loss, take_profit = self._stops_from_late_entry(pending, fill_price)
        ghost = (
            f"g:{pending.created_index}:{pending.planned_entry:.8f}:"
            f"{pending.stop_loss:.8f}:{pending.take_profit:.8f}"
        )
        return Signal(
            action=pending.action,
            pattern=f"{pending.pattern}|{ghost}",
            index=index,
            price=fill_price,
            timestamp=candles[index].timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
            score=self.min_rr,
        )
