"""1-minute candle construction for Gate 2 micro fidelity.

Canonical signal timeframe is completed 1m bars. Forming bars are tracked
separately and never used as signal sources.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


def floor_minute_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.replace(second=0, microsecond=0)


@dataclass(frozen=True)
class MinuteCandle:
    """Completed or forming 1m candle (UTC open timestamp)."""

    timestamp_utc: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0
    completed: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp_utc"] = self.timestamp_utc.isoformat()
        return d

    @property
    def range(self) -> float:
        return float(self.high) - float(self.low)


@dataclass
class CandleBuilder:
    """Build 1m OHLC from mid (or last) prices stamped in UTC."""

    forming: MinuteCandle | None = None
    completed: list[MinuteCandle] | None = None

    def __post_init__(self) -> None:
        if self.completed is None:
            self.completed = []

    def update(self, price: float, ts: datetime) -> MinuteCandle | None:
        """Ingest one price. Returns newly completed candle if a minute rolled."""
        minute = floor_minute_utc(ts)
        px = float(price)
        if self.forming is None:
            self.forming = MinuteCandle(
                timestamp_utc=minute,
                open=px,
                high=px,
                low=px,
                close=px,
                tick_count=1,
                completed=False,
            )
            return None
        if minute == self.forming.timestamp_utc:
            self.forming = MinuteCandle(
                timestamp_utc=minute,
                open=self.forming.open,
                high=max(self.forming.high, px),
                low=min(self.forming.low, px),
                close=px,
                tick_count=self.forming.tick_count + 1,
                completed=False,
            )
            return None
        if minute < self.forming.timestamp_utc:
            raise ValueError(
                f"timestamp moved backwards: {minute.isoformat()} < "
                f"{self.forming.timestamp_utc.isoformat()}"
            )
        done = MinuteCandle(
            timestamp_utc=self.forming.timestamp_utc,
            open=self.forming.open,
            high=self.forming.high,
            low=self.forming.low,
            close=self.forming.close,
            tick_count=self.forming.tick_count,
            completed=True,
        )
        assert self.completed is not None
        self.completed.append(done)
        self.forming = MinuteCandle(
            timestamp_utc=minute,
            open=px,
            high=px,
            low=px,
            close=px,
            tick_count=1,
            completed=False,
        )
        return done

    def seed_from_bars(self, bars: Sequence[MinuteCandle]) -> None:
        """Replace completed history (e.g. from MT5 copy_rates). Forming cleared."""
        self.completed = [b for b in bars if b.completed]
        self.forming = None


def candles_from_mt5_rates(
    rates: Iterable[Any],
    *,
    server_offset_seconds: int,
) -> list[MinuteCandle]:
    """Convert MT5 rate rows to completed UTC MinuteCandle list.

    MT5 rate.time is broker server wall-clock unix; convert with offset.
    """
    out: list[MinuteCandle] = []
    offset = int(server_offset_seconds)
    for r in rates:
        raw = int(getattr(r, "time", 0) or 0)
        utc = datetime.fromtimestamp(raw - offset, tz=timezone.utc)
        out.append(
            MinuteCandle(
                timestamp_utc=floor_minute_utc(utc),
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                tick_count=int(getattr(r, "tick_volume", 0) or 0),
                completed=True,
            )
        )
    out.sort(key=lambda c: c.timestamp_utc)
    return out
