from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"


@dataclass(frozen=True)
class Candle:
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    action: SignalAction
    stop_loss: float | None = None
    take_profit: float | None = None
    size_pct: float = 1.0
    tag: str = ""
