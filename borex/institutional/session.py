from __future__ import annotations

from enum import Enum

import pandas as pd

from borex.models.candle import Candle


class TradingSession(str, Enum):
    ASIA = "asia"
    LONDON = "london"
    NEW_YORK = "new_york"
    OVERLAP = "overlap"
    OFF_HOURS = "off_hours"


def session_at(candle: Candle) -> TradingSession:
    """
    Sesiones FX en UTC (aprox.):
    Asia 00-08, London 08-16, NY 13-21, Overlap 13-16.
    """
    ts = pd.Timestamp(candle.timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    hour = ts.hour

    in_london = 8 <= hour < 16
    in_ny = 13 <= hour < 21
    in_asia = 0 <= hour < 8

    if in_london and in_ny:
        return TradingSession.OVERLAP
    if in_london:
        return TradingSession.LONDON
    if in_ny:
        return TradingSession.NEW_YORK
    if in_asia:
        return TradingSession.ASIA
    return TradingSession.OFF_HOURS


def is_institutional_session(session: TradingSession) -> bool:
    """London, NY y overlap son ventanas de mayor flujo institucional."""
    return session in (
        TradingSession.LONDON,
        TradingSession.NEW_YORK,
        TradingSession.OVERLAP,
    )
