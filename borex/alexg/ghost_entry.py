"""Ghost trade / enter-at-SL execution shared by AlexG4+ and alexg5revised."""

from __future__ import annotations

from dataclasses import dataclass, field

from borex.models.candle import Candle, Signal, SignalAction


@dataclass
class PendingSetup:
    """A queued (ghost) setup waiting for price to come back and tag its SL."""

    action: SignalAction
    pattern: str
    stop_loss: float
    take_profit: float
    planned_entry: float
    created_index: int
    expires_index: int
    saw_near_sl: bool = False


def scale_ghost_sl(
    entry: float,
    stop_loss: float,
    action: SignalAction,
    mult: float,
) -> float:
    """
    Scale the distance from planned ghost entry to the ghost SL.

    ``mult=1`` keeps the structural SL. ``mult<1`` pulls SL closer to entry
    (easier fill, smaller ghost risk). ``mult>1`` pushes SL farther.
    """
    if mult <= 0:
        raise ValueError(f"ghost_sl_mult must be > 0 (got {mult})")
    dist = abs(entry - stop_loss) * mult
    if action == SignalAction.BUY:
        return entry - dist
    return entry + dist


@dataclass
class GhostSLEntryMixin:
    """
    Queue a setup as a ghost trade, then fill at its planned SL.

    Hosts must provide `min_rr`, `sl_near_risk_fraction` and a `_pending`
    dict field. Skip (no trade) if: SL is never touched, TP is hit first, or
    price only approaches SL without a fill (near-miss then leaves the zone).
    """

    sl_wait_max_bars: int = 72
    sl_near_risk_fraction: float = 0.25
    # Scales |planned_entry − structural_SL| for the resting ghost limit.
    ghost_sl_mult: float = 1.0
    # Extra pips beyond SL that still count as a fill touch (feed noise).
    sl_touch_pad_pips: float = 0.0
    # True for alexg7aligned/alexg8 live: H1-close market, not limit-at-SL.
    # Engine then fills at the tagging bar close (|fill:close|).
    ghost_fill_at_close: bool = False

    _pending: dict[str, PendingSetup] = field(default_factory=dict, repr=False)

    def _sl_touch_pad(self, symbol: str = "EURUSD=X") -> float:
        if self.sl_touch_pad_pips <= 0:
            return 0.0
        from borex.backtest.costs import infer_pip_size

        return self.sl_touch_pad_pips * infer_pip_size(symbol)

    def _scaled_ghost_sl(
        self,
        entry: float,
        stop_loss: float,
        action: SignalAction,
    ) -> float:
        return scale_ghost_sl(entry, stop_loss, action, self.ghost_sl_mult)

    def _sl_risk_distance(self, pending: PendingSetup) -> float:
        return abs(pending.planned_entry - pending.stop_loss)

    def _near_sl_band(self, pending: PendingSetup) -> float:
        return self._sl_risk_distance(pending) * self.sl_near_risk_fraction

    def _sl_touched(self, pending: PendingSetup, candle: Candle) -> bool:
        pad = self._sl_touch_pad()
        if pending.action == SignalAction.BUY:
            return candle.low <= pending.stop_loss + pad
        return candle.high >= pending.stop_loss - pad

    def _tp_touched(self, pending: PendingSetup, candle: Candle) -> bool:
        if pending.action == SignalAction.BUY:
            return candle.high >= pending.take_profit
        return candle.low <= pending.take_profit

    def _in_near_sl_zone(self, pending: PendingSetup, candle: Candle) -> bool:
        band = self._near_sl_band(pending)
        sl = pending.stop_loss
        if pending.action == SignalAction.BUY:
            return sl < candle.low <= sl + band
        return sl - band <= candle.high < sl

    def _left_near_sl_zone(self, pending: PendingSetup, candle: Candle) -> bool:
        band = self._near_sl_band(pending)
        sl = pending.stop_loss
        if pending.action == SignalAction.BUY:
            return candle.close > sl + band
        return candle.close < sl - band

    def _pending_outcome(
        self,
        pending: PendingSetup,
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
        pending: PendingSetup,
        fill_price: float,
    ) -> tuple[float, float]:
        """Same risk/reward distances as the original plan, from the fill price."""
        risk = abs(pending.planned_entry - pending.stop_loss)
        reward = abs(pending.take_profit - pending.planned_entry)
        if pending.action == SignalAction.BUY:
            return fill_price - risk, fill_price + reward
        return fill_price + risk, fill_price - reward

    def _ghost_tag(self, pending: PendingSetup) -> str:
        """`g:` metadata: the engine fills these signals at SL on the touch bar."""
        return (
            f"g:{pending.created_index}:{pending.planned_entry:.8f}:"
            f"{pending.stop_loss:.8f}:{pending.take_profit:.8f}"
        )

    def _confirm_ghost_fill(
        self,
        pending: PendingSetup,
        index: int,
        candles: list[Candle],
    ) -> bool:
        """Hook for subclasses (alexg8): extra filters at the SL-touch bar."""
        return True

    def _entry_signal(
        self,
        pending: PendingSetup,
        index: int,
        candles: list[Candle],
    ) -> Signal:
        candle = candles[index]
        fill_at_close = bool(getattr(self, "ghost_fill_at_close", False))
        fill_price = float(candle.close) if fill_at_close else pending.stop_loss
        stop_loss, take_profit = self._stops_from_late_entry(pending, fill_price)
        extra = "|fill:close" if fill_at_close else ""
        return Signal(
            action=pending.action,
            pattern=f"{pending.pattern}|{self._ghost_tag(pending)}{extra}",
            index=index,
            price=fill_price,
            timestamp=candle.timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
            score=self.min_rr,
        )
