from __future__ import annotations

import numpy as np

from borex.models.params import ParamDef, ParamType
from borex.models.signal import Candle, Signal, SignalAction
from borex.strategy.base import Strategy, StrategyContext


class SmaCross(Strategy):
    name = "sma_cross"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("fast", ParamType.INT, 10, min=2, max=100, step=2, description="Fast SMA period"),
            ParamDef("slow", ParamType.INT, 30, min=5, max=300, step=5, description="Slow SMA period"),
            ParamDef("sl_pct", ParamType.FLOAT, 0.003, min=0.0005, max=0.02, step=0.0005),
            ParamDef("tp_pct", ParamType.FLOAT, 0.006, min=0.001, max=0.05, step=0.001),
        ]

    def warmup_bars(self) -> int:
        return int(self.params["slow"]) + 2

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        slow = int(self.params["slow"])
        fast = int(self.params["fast"])
        if index < slow:
            return []

        closes = np.array([c.close for c in candles[: index + 1]], dtype=float)
        fast_ma = closes[-fast:].mean()
        slow_ma = closes[-slow:].mean()
        prev_fast = closes[-fast - 1 : -1].mean()
        prev_slow = closes[-slow - 1 : -1].mean()

        price = candles[index].close
        sl_pct = float(self.params["sl_pct"])
        tp_pct = float(self.params["tp_pct"])

        if prev_fast <= prev_slow and fast_ma > slow_ma:
            return [
                Signal(
                    action=SignalAction.BUY,
                    stop_loss=price * (1 - sl_pct),
                    take_profit=price * (1 + tp_pct),
                    tag="sma_golden",
                )
            ]
        if prev_fast >= prev_slow and fast_ma < slow_ma:
            return [
                Signal(
                    action=SignalAction.SELL,
                    stop_loss=price * (1 + sl_pct),
                    take_profit=price * (1 - tp_pct),
                    tag="sma_death",
                )
            ]
        return []
