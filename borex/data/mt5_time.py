"""Convert MetaTrader bar/tick times to true UTC.

ICMarkets (and many brokers) expose `rate["time"]` / tick.time as a Unix-like
value of the **server wall clock**, not UTC. Treating that as UTC labels
13:00 server as 13:00Z and breaks London–NY session filters (~3h off in summer).

Measure offset once: ``tick.time - time.time()``, keep only 0 / ±2h / ±3h.
Weekend-stale ticks (PC timezone, Friday last quote) must not become a 14h shift.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


def snap_server_offset_seconds(raw_offset: int) -> int:
    """Quantize a measured server-clock skew.

    IC Markets is UTC, UTC+2, or UTC+3. A PC timezone can make
    ``tick.time - time.time()`` look like −14h; applying that shifts every H1
    and breaks multi-pair alignment. Out-of-range probes are treated as UTC.
    """
    if abs(int(raw_offset)) < 120:
        return 0
    hours = int(round(int(raw_offset) / 3600.0))
    if hours not in (0, 2, 3, -2, -3):
        return 0
    return hours * 3600


def measure_mt5_server_offset_seconds(mt5_module, *, probe_symbol: str = "EURUSD") -> int:
    """Seconds to subtract from MT5 timestamps to get UTC. 0 if already UTC."""
    if mt5_module is None:
        return 0
    tick = None
    try:
        tick = mt5_module.symbol_info_tick(probe_symbol)
    except Exception:
        tick = None
    if tick is None:
        try:
            info = mt5_module.symbols_get()
            if info:
                tick = mt5_module.symbol_info_tick(info[0].name)
        except Exception:
            tick = None
    if tick is None or not getattr(tick, "time", None):
        return 0
    now = int(time.time())
    tick_unix = int(tick.time)
    offset = tick_unix - now
    # Live GMT+2/+3 ticks sit 0–3h ahead of UTC. A Friday quote on Saturday
    # looks 10–40h behind and must not move the H1 grid.
    if offset < -4 * 3600:
        return 0
    return snap_server_offset_seconds(offset)


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
