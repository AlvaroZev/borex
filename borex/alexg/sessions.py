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


def _as_utc(ts: datetime | object) -> datetime:
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            import pandas as pd

            dt = pd.Timestamp(ts).to_pydatetime()
        except Exception:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_session(ts: datetime | object) -> TradingSession | None:
    """Session a fill belongs to (most specific window).

    Overlap (London–NY) wins, then London, New York, Asia. Off-hours → None.
    """
    dt = _as_utc(ts)
    if in_session(dt, TradingSession.OVERLAP):
        return TradingSession.OVERLAP
    if in_session(dt, TradingSession.LONDON):
        return TradingSession.LONDON
    if in_session(dt, TradingSession.NEWYORK):
        return TradingSession.NEWYORK
    if in_session(dt, TradingSession.ASIA):
        return TradingSession.ASIA
    return None


def session_close_hour(session: TradingSession | str) -> int:
    """UTC hour the session *ends* (bar that opens at session close).

    Live flattens 5–10m before this print. On H1 we flatten that bar at *open*
    so the last in-session hour still trades, then we exit at the close hour.
    """
    if isinstance(session, str):
        session = TradingSession(session.strip().lower())
    if session == TradingSession.ALL:
        session = TradingSession.NEWYORK
    _start, end = _SESSION_WINDOWS[session]
    return int(end.hour) % 24


# Back-compat alias (now the close hour, not the previous bar).
session_last_bar_hour = session_close_hour
