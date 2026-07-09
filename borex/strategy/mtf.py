from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

import numpy as np

from borex.models.signal import Candle
from borex.strategy.base import Strategy, StrategyContext
from borex.strategy.indicators import pd_day


@dataclass(frozen=True)
class MtfSpec:
    """bias_timeframes must be strictly higher than entry timeframe."""

    bias_timeframes: tuple[str, ...]
    entry_timeframes: tuple[str, ...]


class MtfContext:
    """Aligned higher-timeframe state at each entry bar (last closed HTF bar only)."""

    def __init__(
        self,
        entry_index: int,
        entry_candles: list[Candle],
        htf: dict[str, list[Candle]],
        align: dict[str, list[int]],
    ) -> None:
        self.entry_index = entry_index
        self.entry_candles = entry_candles
        self._htf = htf
        self._align = align

    def htf_idx(self, tf: str) -> int:
        return self._align[tf][self.entry_index]

    def htf_candles(self, tf: str) -> list[Candle]:
        return self._htf[tf]

    def htf_closed(self, tf: str) -> Candle | None:
        idx = self.htf_idx(tf)
        if idx < 0:
            return None
        return self._htf[tf][idx]

    def htf_closes_through(self, tf: str) -> np.ndarray:
        idx = self.htf_idx(tf)
        if idx < 0:
            return np.array([])
        return np.array([c.close for c in self._htf[tf][: idx + 1]], dtype=float)

    def first_htf_bar_of_day(self, tf: str) -> Candle | None:
        """First closed HTF bar of the current UTC day on the HTF series."""
        day = pd_day(self.entry_candles[self.entry_index].timestamp)
        idx = self.htf_idx(tf)
        if idx < 0:
            return None
        for i in range(idx, -1, -1):
            if pd_day(self._htf[tf][i].timestamp) != day:
                return self._htf[tf][i + 1] if i + 1 <= idx else None
        return self._htf[tf][0] if pd_day(self._htf[tf][0].timestamp) == day else None

    def prev_closed_htf_bar(self, tf: str) -> Candle | None:
        idx = self.htf_idx(tf)
        if idx < 1:
            return None
        return self._htf[tf][idx - 1]

    def day_htf_bars(self, tf: str) -> list[Candle]:
        day = pd_day(self.entry_candles[self.entry_index].timestamp)
        idx = self.htf_idx(tf)
        if idx < 0:
            return []
        return [c for i, c in enumerate(self._htf[tf][: idx + 1]) if pd_day(c.timestamp) == day]


class MtfStrategy(Strategy):
    @classmethod
    @abstractmethod
    def mtf_spec(cls) -> MtfSpec:
        ...

    def _mtf(self, ctx: StrategyContext) -> MtfContext:
        if ctx.mtf is None:
            raise RuntimeError(f"{self.name} requires multi-timeframe data")
        return ctx.mtf


def is_mtf_strategy(cls: type[Strategy]) -> bool:
    return issubclass(cls, MtfStrategy) and cls is not MtfStrategy


def validate_mtf_entry(strategy: Strategy, timeframe: str) -> None:
    if not is_mtf_strategy(type(strategy)):
        return
    allowed = strategy.mtf_spec().entry_timeframes
    if timeframe not in allowed:
        raise ValueError(
            f"Strategy {strategy.name} entry timeframe must be one of {allowed}, got {timeframe}"
        )
