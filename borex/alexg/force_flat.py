"""Force-flat at the originating session close (H1 bar granularity).

H1 candle timestamps are bar *open* times (UTC). Processing a bar means it
already closed. Daily flatten fires on the bar that *opens* at session end
(London 16:00, NY 21:00, Asia 09:00) and exits at that bar's **open** — the
H1 stand-in for live "5–10 minutes before the close hour". The previous
(last in-session) hour still runs SL/TP.

Friday: flatten leftovers on the NY close hour (weekend gate).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from borex.alexg.sessions import (
    TradingSession,
    classify_session,
    session_close_hour,
)


def _to_utc(ts: datetime | object) -> datetime:
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


def _cfg_bool(config: Any, name: str, default: bool = False) -> bool:
    return bool(getattr(config, name, default))


def _cfg_int(config: Any, name: str, default: int) -> int:
    return int(getattr(config, name, default) or default)


def _trade_session(trade: Any) -> TradingSession | None:
    if trade is None:
        return None
    raw = getattr(trade, "entry_session", None) or ""
    if raw:
        try:
            sess = TradingSession(str(raw).strip().lower())
            if sess != TradingSession.ALL:
                return sess
        except ValueError:
            pass
    entry = getattr(trade, "entry_time", None)
    if entry is None:
        return None
    return classify_session(entry)


def _friday_flat_hour(config: Any) -> int:
    """Weekend gate: last NY hour, unless a non-legacy custom hour is set.

    Default 19 used to mean '1h before 21:00 NY' as a global clock. Session
    close uses the 21:00 bar (exit at open ≈ NY close). Treat 19 as that default.
    """
    custom = _cfg_int(config, "force_flat_friday_from_hour", 19)
    if custom == 19:
        return session_close_hour(TradingSession.NEWYORK)
    return custom


def force_flat_reason(
    ts: datetime | object,
    config: Any,
    trade: Any = None,
) -> str | None:
    """Return a force-flat exit reason for this bar/trade, or None."""
    if not (_cfg_bool(config, "force_flat_friday") or _cfg_bool(config, "force_flat_daily")):
        return None

    dt = _to_utc(ts)
    weekday = dt.weekday()  # Mon=0 … Fri=4
    hour = dt.hour

    if _cfg_bool(config, "force_flat_friday") and weekday == 4:
        if hour >= _friday_flat_hour(config):
            return "friday_close"

    if _cfg_bool(config, "force_flat_daily") and weekday <= 4:
        session = _trade_session(trade)
        if session is None:
            return None
        if hour == session_close_hour(session):
            return "daily_close"

    return None


def blocks_new_entries(ts: datetime | object, config: Any) -> bool:
    """True when new entries/ghost queues should not be opened on this bar."""
    if not (_cfg_bool(config, "force_flat_friday") or _cfg_bool(config, "force_flat_daily")):
        return False

    dt = _to_utc(ts)
    weekday = dt.weekday()
    hour = dt.hour

    if _cfg_bool(config, "force_flat_friday") and weekday == 4:
        if hour >= _friday_flat_hour(config):
            return True

    if _cfg_bool(config, "force_flat_daily") and weekday <= 4:
        # Only block when no session is open (e.g. NY 21:00). London 16:00
        # still has NY, so new entries may continue.
        if classify_session(dt) is None:
            for sess in (
                TradingSession.ASIA,
                TradingSession.LONDON,
                TradingSession.NEWYORK,
                TradingSession.OVERLAP,
            ):
                if hour == session_close_hour(sess):
                    return True

    return False
