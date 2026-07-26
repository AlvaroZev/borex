"""Forex session filters for ablation / Set-and-Forget."""

from __future__ import annotations

from datetime import datetime, time, timezone
from enum import Enum


class TradingSession(str, Enum):
    ASIA = "asia"
    LONDON = "london"
    NEWYORK = "newyork"
    OVERLAP = "overlap"  # London–NY
    ALL = "all"


# UTC windows (approx. industry-standard FX sessions).
_SESSION_WINDOWS: dict[TradingSession, tuple[time, time]] = {
    TradingSession.ASIA: (time(0, 0), time(9, 0)),
    TradingSession.LONDON: (time(7, 0), time(16, 0)),
    TradingSession.NEWYORK: (time(12, 0), time(21, 0)),
    TradingSession.OVERLAP: (time(12, 0), time(16, 0)),
}


def _in_window(ts: datetime, start: time, end: time) -> bool:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    t = ts.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= t < end
    # wraps midnight
    return t >= start or t < end


def in_session(ts: datetime, session: TradingSession | str) -> bool:
    """True if timestamp falls in the requested session (UTC)."""
    if isinstance(session, str):
        session = TradingSession(session.strip().lower())
    if session == TradingSession.ALL:
        return True
    start, end = _SESSION_WINDOWS[session]
    return _in_window(ts, start, end)
