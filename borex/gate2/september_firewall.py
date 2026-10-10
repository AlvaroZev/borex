"""Hard September firewall for Gate 2 discovery campaigns.

Any strategy-research read of timestamps on/after 2026-09-01 must fail loudly.
Does not affect the separate locked September OOS runner.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

SEPTEMBER_FIREWALL_START = pd.Timestamp("2026-09-01", tz="UTC")
RESEARCH_END_EXCLUSIVE = SEPTEMBER_FIREWALL_START
RESEARCH_START = pd.Timestamp("2026-06-01", tz="UTC")


class SeptemberFirewallError(RuntimeError):
    """Raised when discovery code attempts to use September (or later) bars."""


def _as_utc_ts(x: Any) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def assert_not_september(ts: Any, *, context: str = "") -> None:
    t = _as_utc_ts(ts)
    if t >= SEPTEMBER_FIREWALL_START:
        raise SeptemberFirewallError(
            f"September firewall violated: ts={t.isoformat()} context={context!r}. "
            "Discovery campaigns must not read strategy data with timestamp >= 2026-09-01."
        )


def assert_research_window(ts: Any, *, context: str = "") -> None:
    t = _as_utc_ts(ts)
    if t < RESEARCH_START:
        raise SeptemberFirewallError(
            f"Before research window: ts={t.isoformat()} context={context!r}"
        )
    assert_not_september(t, context=context)


def clamp_end_before_september(end: Any) -> pd.Timestamp:
    """Clamp an end timestamp to strictly before September 2026."""
    t = _as_utc_ts(end)
    if t > RESEARCH_END_EXCLUSIVE:
        return RESEARCH_END_EXCLUSIVE
    return t


def filter_rows_before_september(
    rows: list[dict],
    *,
    time_key: str = "entry_time",
) -> list[dict]:
    """Drop any rows that touch September; raise if any were present when strict."""
    out = []
    for r in rows:
        t = _as_utc_ts(r[time_key])
        if t >= SEPTEMBER_FIREWALL_START:
            raise SeptemberFirewallError(
                f"Row leaked into September: {time_key}={t.isoformat()} row={r!r}"
            )
        out.append(r)
    return out


def guard_candle_list(candles: list, *, context: str = "") -> list:
    """Ensure no candle timestamp is in September; return same list if OK."""
    for c in candles:
        ts = getattr(c, "timestamp", None)
        if ts is None:
            continue
        assert_not_september(ts, context=context)
    return candles


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
