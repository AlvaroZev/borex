"""Convert MetaTrader bar/tick times to true UTC.

ICMarkets (and many brokers) expose `rate["time"]` / tick.time as a Unix-like
value of the **server wall clock**, not UTC. Treating that as UTC labels
13:00 server as 13:00Z and breaks London–NY session filters (~3h off in summer).

Measure offset once: ``tick.time - time.time()``, keep only 0 / ±2h / ±3h.
Weekend-stale ticks (PC timezone, Friday last quote) must not become a 14h shift.

Gate 2 / execution fidelity must use :func:`assess_server_offset` (fail-closed)
rather than treating a silent ``0`` from a stale tick as a confident UTC offset.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Default max age for trusting a tick for offset / freeze quotes (Gate 2).
DEFAULT_MAX_TICK_AGE_SECONDS = 120
# Allowed snapped offsets (seconds) when tick is fresh.
_ALLOWED_OFFSET_HOURS = (0, 2, 3, -2, -3)


def snap_server_offset_seconds(raw_offset: int) -> int:
    """Quantize a measured server-clock skew.

    IC Markets is UTC, UTC+2, or UTC+3. A PC timezone can make
    ``tick.time - time.time()`` look like −14h; applying that shifts every H1
    and breaks multi-pair alignment. Out-of-range probes are treated as UTC.

    NOTE: A return of 0 can mean either "broker is UTC" or "untrusted/out-of-range".
    For fail-closed consumers use :func:`assess_server_offset` instead.
    """
    if abs(int(raw_offset)) < 120:
        return 0
    hours = int(round(int(raw_offset) / 3600.0))
    if hours not in _ALLOWED_OFFSET_HOURS:
        return 0
    return hours * 3600


@dataclass(frozen=True)
class TickFreshness:
    symbol: str
    tick_time_unix_raw: int
    local_utc_unix: int
    local_utc_iso: str
    tick_time_as_if_utc_iso: str | None
    tick_age_seconds: float | None
    max_age_seconds: float
    fresh: bool
    decision: str
    rejection_reason: str | None
    bid: float | None = None
    ask: float | None = None
    server_time_unix_raw: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServerOffsetAssessment:
    """Fail-closed server offset measurement."""

    offset_seconds: int | None
    confident: bool
    raw_offset_seconds: int | None
    snapped_offset_seconds: int | None
    tick_freshness: TickFreshness | None
    decision: str
    rejection_reason: str | None
    probe_symbol: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def tick_staleness_seconds(tick_unix: int, now_unix: int | None = None) -> float:
    """How far the tick lags local UTC. 0 if tick.time is at/ahead of UTC (GMT+2/+3)."""
    now = int(time.time() if now_unix is None else now_unix)
    return max(0.0, float(now - int(tick_unix)))


def tick_age_seconds(tick_unix: int, now_unix: int | None = None) -> float:
    """Backward-compatible alias: lag vs local UTC (0 when server clock is ahead)."""
    return tick_staleness_seconds(tick_unix, now_unix)


def assess_tick_freshness(
    *,
    symbol: str,
    tick_time_unix_raw: int,
    local_utc_unix: int | None = None,
    max_age_seconds: float = DEFAULT_MAX_TICK_AGE_SECONDS,
    bid: float | None = None,
    ask: float | None = None,
    server_time_unix_raw: int | None = None,
) -> TickFreshness:
    """Decide whether a broker tick is fresh enough to trust for freeze/offset.

    IC Markets encodes ``tick.time`` on the server wall clock (often UTC+2/+3),
    so a live tick can sit ~10800s *ahead* of local UTC. Staleness is therefore
    ``max(0, local_utc - tick.time)`` — not ``local_utc - tick.time`` signed age.
    """
    now = int(time.time() if local_utc_unix is None else local_utc_unix)
    raw = int(tick_time_unix_raw or 0)
    local_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    max_ahead = 3 * 3600 + float(max_age_seconds)
    if raw <= 0:
        return TickFreshness(
            symbol=symbol,
            tick_time_unix_raw=raw,
            local_utc_unix=now,
            local_utc_iso=local_iso,
            tick_time_as_if_utc_iso=None,
            tick_age_seconds=None,
            max_age_seconds=float(max_age_seconds),
            fresh=False,
            decision="reject",
            rejection_reason="missing_tick_time",
            bid=bid,
            ask=ask,
            server_time_unix_raw=server_time_unix_raw,
        )
    as_if = datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
    staleness = tick_staleness_seconds(raw, now)
    ahead = float(raw - now)
    if staleness > float(max_age_seconds):
        return TickFreshness(
            symbol=symbol,
            tick_time_unix_raw=raw,
            local_utc_unix=now,
            local_utc_iso=local_iso,
            tick_time_as_if_utc_iso=as_if,
            tick_age_seconds=staleness,
            max_age_seconds=float(max_age_seconds),
            fresh=False,
            decision="reject",
            rejection_reason=f"stale_tick_age_{staleness:.1f}s_gt_max_{float(max_age_seconds):.1f}s",
            bid=bid,
            ask=ask,
            server_time_unix_raw=server_time_unix_raw,
        )
    if ahead > max_ahead:
        return TickFreshness(
            symbol=symbol,
            tick_time_unix_raw=raw,
            local_utc_unix=now,
            local_utc_iso=local_iso,
            tick_time_as_if_utc_iso=as_if,
            tick_age_seconds=staleness,
            max_age_seconds=float(max_age_seconds),
            fresh=False,
            decision="reject",
            rejection_reason=f"tick_too_far_ahead_{ahead:.1f}s",
            bid=bid,
            ask=ask,
            server_time_unix_raw=server_time_unix_raw,
        )
    return TickFreshness(
        symbol=symbol,
        tick_time_unix_raw=raw,
        local_utc_unix=now,
        local_utc_iso=local_iso,
        tick_time_as_if_utc_iso=as_if,
        tick_age_seconds=staleness,
        max_age_seconds=float(max_age_seconds),
        fresh=True,
        decision="accept",
        rejection_reason=None,
        bid=bid,
        ask=ask,
        server_time_unix_raw=server_time_unix_raw,
    )


def assess_server_offset(
    *,
    tick_time_unix_raw: int,
    local_utc_unix: int | None = None,
    max_age_seconds: float = DEFAULT_MAX_TICK_AGE_SECONDS,
    symbol: str = "EURUSD",
    bid: float | None = None,
    ask: float | None = None,
) -> ServerOffsetAssessment:
    """Fail-closed offset assessment. Never reports confident 0 from a stale tick."""
    now = int(time.time() if local_utc_unix is None else local_utc_unix)
    freshness = assess_tick_freshness(
        symbol=symbol,
        tick_time_unix_raw=tick_time_unix_raw,
        local_utc_unix=now,
        max_age_seconds=max_age_seconds,
        bid=bid,
        ask=ask,
        server_time_unix_raw=int(tick_time_unix_raw or 0) or None,
    )
    if not freshness.fresh:
        return ServerOffsetAssessment(
            offset_seconds=None,
            confident=False,
            raw_offset_seconds=None
            if freshness.tick_age_seconds is None and not tick_time_unix_raw
            else (int(tick_time_unix_raw) - now if tick_time_unix_raw else None),
            snapped_offset_seconds=None,
            tick_freshness=freshness,
            decision="reject",
            rejection_reason=freshness.rejection_reason or "stale_or_invalid_tick",
            probe_symbol=symbol,
        )

    raw_offset = int(tick_time_unix_raw) - now
    # Legacy guard: deeply negative still means unusable even if age gate used
    # server-encoded times; age gate should already catch this when tick is
    # Friday-stale vs Monday UTC. Keep explicit.
    if raw_offset < -4 * 3600:
        return ServerOffsetAssessment(
            offset_seconds=None,
            confident=False,
            raw_offset_seconds=raw_offset,
            snapped_offset_seconds=None,
            tick_freshness=freshness,
            decision="reject",
            rejection_reason="raw_offset_too_negative_untrusted",
            probe_symbol=symbol,
        )

    if abs(raw_offset) < 120:
        snapped = 0
        return ServerOffsetAssessment(
            offset_seconds=0,
            confident=True,
            raw_offset_seconds=raw_offset,
            snapped_offset_seconds=snapped,
            tick_freshness=freshness,
            decision="accept",
            rejection_reason=None,
            probe_symbol=symbol,
        )

    hours = int(round(raw_offset / 3600.0))
    if hours not in _ALLOWED_OFFSET_HOURS:
        return ServerOffsetAssessment(
            offset_seconds=None,
            confident=False,
            raw_offset_seconds=raw_offset,
            snapped_offset_seconds=0,  # legacy snap would have returned 0 — not trusted
            tick_freshness=freshness,
            decision="reject",
            rejection_reason=f"offset_hours_{hours}_not_in_allowed",
            probe_symbol=symbol,
        )

    snapped = hours * 3600
    return ServerOffsetAssessment(
        offset_seconds=snapped,
        confident=True,
        raw_offset_seconds=raw_offset,
        snapped_offset_seconds=snapped,
        tick_freshness=freshness,
        decision="accept",
        rejection_reason=None,
        probe_symbol=symbol,
    )


def measure_mt5_server_offset_seconds(mt5_module, *, probe_symbol: str = "EURUSD") -> int:
    """Seconds to subtract from MT5 timestamps to get UTC.

    Legacy helper for H1/session alignment: stale/out-of-range probes return 0
    so weekend-stale ticks do not shift the bar grid by many hours.

    Gate 2 execution fidelity must call :func:`measure_mt5_server_offset_assessment`
    and fail closed when ``confident`` is False — do not treat this return of 0
    as proof the broker clock is UTC.
    """
    assessment = measure_mt5_server_offset_assessment(mt5_module, probe_symbol=probe_symbol)
    if assessment.confident and assessment.offset_seconds is not None:
        return int(assessment.offset_seconds)
    # Legacy: untrusted → 0 (H1-safe). Callers needing fail-closed must use assessment.
    return 0


def measure_mt5_server_offset_assessment(
    mt5_module,
    *,
    probe_symbol: str = "EURUSD",
    max_age_seconds: float = DEFAULT_MAX_TICK_AGE_SECONDS,
    now_unix: int | None = None,
) -> ServerOffsetAssessment:
    """Measure offset with explicit confidence (fail-closed friendly)."""
    if mt5_module is None:
        return ServerOffsetAssessment(
            offset_seconds=None,
            confident=False,
            raw_offset_seconds=None,
            snapped_offset_seconds=None,
            tick_freshness=None,
            decision="reject",
            rejection_reason="mt5_module_none",
            probe_symbol=probe_symbol,
        )
    tick = None
    symbol_used = probe_symbol
    try:
        tick = mt5_module.symbol_info_tick(probe_symbol)
    except Exception:
        tick = None
    if tick is None:
        try:
            info = mt5_module.symbols_get()
            if info:
                symbol_used = str(info[0].name)
                tick = mt5_module.symbol_info_tick(symbol_used)
        except Exception:
            tick = None
    if tick is None or not getattr(tick, "time", None):
        return ServerOffsetAssessment(
            offset_seconds=None,
            confident=False,
            raw_offset_seconds=None,
            snapped_offset_seconds=None,
            tick_freshness=None,
            decision="reject",
            rejection_reason="tick_unavailable",
            probe_symbol=symbol_used,
        )
    return assess_server_offset(
        tick_time_unix_raw=int(tick.time),
        local_utc_unix=now_unix,
        max_age_seconds=max_age_seconds,
        symbol=symbol_used,
        bid=float(getattr(tick, "bid", 0) or 0) or None,
        ask=float(getattr(tick, "ask", 0) or 0) or None,
    )


def snap_h1_open(dt: datetime) -> datetime:
    """H1 bar open in UTC with minutes/seconds cleared."""
    if dt.tzinfo is None:
        utc = dt.replace(tzinfo=timezone.utc)
    else:
        utc = dt.astimezone(timezone.utc)
    return utc.replace(minute=0, second=0, microsecond=0)


def mt5_unix_to_utc(mt5_unix: int, offset_seconds: int = 0) -> datetime:
    """MT5 bar/tick unix → timezone-aware UTC datetime."""
    return datetime.fromtimestamp(int(mt5_unix) - int(offset_seconds or 0), tz=timezone.utc)


def utc_to_mt5_request(utc: datetime, offset_seconds: int = 0) -> datetime:
    """UTC instant → datetime to pass into copy_rates_range (server clock)."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    else:
        utc = utc.astimezone(timezone.utc)
    if not offset_seconds:
        return utc
    return utc + timedelta(seconds=int(offset_seconds))
