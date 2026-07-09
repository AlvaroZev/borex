from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd

from borex.models.params import ParamDef, resolve_params
from borex.models.signal import Candle, Signal


@dataclass
class StrategyContext:
    symbol: str
    timeframe: str
    open_trades: int = 0
    mtf: object | None = None  # MtfContext when running MTF strategies


class Strategy(ABC):
    """All strategies expose a param schema for sweeps and future AI tuning."""

    name: str = "base"

    @classmethod
    @abstractmethod
    def param_schema(cls) -> list[ParamDef]:
        ...

    def __init__(self, params: dict | None = None) -> None:
        self.params = resolve_params(self.param_schema(), params)

    def warmup_bars(self) -> int:
        return 50

    @abstractmethod
    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        ...

    def to_metadata(self) -> dict:
        return {
            "name": self.name,
            "params": self.params,
            "schema": [p.to_dict() for p in self.param_schema()],
        }


def candles_from_df(df: pd.DataFrame) -> list[Candle]:
    out: list[Candle] = []
    for ts, row in df.iterrows():
        out.append(
            Candle(
                timestamp=ts,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row.get("Volume", 0)),
            )
        )
    return out
