"""Apply GhostSL (LIQ) transform to InternetSignal streams → activation tape.

LIQ here means GhostSL geometry — NOT alexg9liq liquidity filters.
"""

from __future__ import annotations

from borex.alexg.internet.strategies import InternetSignal
from borex.gate2.ghostsl import (
    GhostStatus,
    build_ghost_setup,
    resolve_activation_bar,
)
from borex.models.candle import Candle


def internet_to_ghost_setup(
    sig: InternetSignal,
    *,
    real_sl_mult: float = 1.0,
    rr: float | None = None,
) -> object:
    ghost_sl_dist = abs(sig.entry - sig.sl)
    if ghost_sl_dist <= 0:
        return None
    # default real RR from original signal geometry
    if rr is None:
        risk = ghost_sl_dist
        reward = abs(sig.tp - sig.entry)
        rr = (reward / risk) if risk > 0 else 2.0
    return build_ghost_setup(
        sig.entry,
        sig.side,
        ghost_sl_distance=ghost_sl_dist,
        real_sl_distance=ghost_sl_dist * real_sl_mult,
        rr=rr,
        signal_time=sig.timestamp,
        created_index=sig.index,
        pattern=sig.pattern + "|liq_ghostsl",
    )


def activate_ghost_on_bars(
    setup,
    candles: list[Candle],
    start_i: int,
) -> tuple[object | None, str]:
    """Walk forward from signal bar; return (activated setup or None, reason)."""
    for j in range(start_i + 1, min(len(candles), start_i + 1 + setup.expires_bars)):
        c = candles[j]
        st = resolve_activation_bar(
            setup, high=c.high, low=c.low, open_=c.open, close=c.close
        )
        if st == GhostStatus.PENDING_GHOST:
            continue
        if st == GhostStatus.INVALIDATED:
            setup.status = st
            return None, "activation_bar_unresolved"
        if st == GhostStatus.REAL_SL:
            setup.status = st
            setup.activation_time = c.timestamp
            return None, "real_sl_on_activation"
        if st == GhostStatus.REAL_TP:
            setup.status = st
            setup.activation_time = c.timestamp
            return None, "real_tp_on_activation"
        # REAL_ACTIVE
        setup.status = GhostStatus.REAL_ACTIVE
        setup.activation_time = c.timestamp
        return setup, "activated"
    setup.status = GhostStatus.EXPIRED
    return None, "expired"


def ghost_setup_to_decision(setup, symbol: str) -> dict:
    """Decision-tape row for MultiMarketEngine (immediate entry at real_entry)."""
    import pandas as pd

    t = pd.Timestamp(setup.activation_time)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return {
        "symbol": symbol,
        "time_unix": int(t.timestamp()),
        "action": "buy" if setup.side == "long" else "sell",
        "stop_loss": setup.real_sl,
        "take_profit": setup.real_tp,
        "pattern": setup.pattern,
        "price": setup.real_entry,
    }
