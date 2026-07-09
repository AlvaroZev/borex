from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_TIMEFRAMES: list[str] = ["1m", "15m", "30m", "1h", "4h", "1d", "1wk"]


@dataclass(frozen=True)
class Timeframe:
    code: str
    yahoo_interval: str
    pandas_rule: str


TIMEFRAMES: dict[str, Timeframe] = {
    "1m": Timeframe("1m", "1m", "1min"),
    "15m": Timeframe("15m", "15m", "15min"),
    "30m": Timeframe("30m", "30m", "30min"),
    "1h": Timeframe("1h", "1h", "1h"),
    "4h": Timeframe("4h", "4h", "4h"),
    "1d": Timeframe("1d", "1d", "1D"),
    "1wk": Timeframe("1wk", "1wk", "1W"),
}
