"""Yahoo Finance intraday history limits (verified 2026-07)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntervalLimit:
    max_lookback_days: int | None  # None = use period=max
    chunk_days: int
    note: str


# 1h/4h: ~730 days. 1d/1wk: decades. 1m: ~30d via 7d chunks. 15m/30m: ~60d.
INTERVAL_LIMITS: dict[str, IntervalLimit] = {
    "1m": IntervalLimit(30, 7, "Yahoo allows ~30 days of 1m; fetched in 7-day chunks"),
    "15m": IntervalLimit(60, 60, "Yahoo allows ~60 days of 15m"),
    "30m": IntervalLimit(60, 60, "Yahoo allows ~60 days of 30m"),
    "1h": IntervalLimit(730, 730, "Yahoo allows ~730 days of 1h"),
    "4h": IntervalLimit(730, 730, "Yahoo allows ~730 days of 4h"),
    "1d": IntervalLimit(None, 0, "Daily: full history via period=max"),
    "1wk": IntervalLimit(None, 0, "Weekly: full history via period=max"),
}
