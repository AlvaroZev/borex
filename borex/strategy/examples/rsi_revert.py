from __future__ import annotations

import numpy as np

from borex.models.params import ParamDef, ParamType
from borex.models.signal import Candle, Signal, SignalAction
from borex.strategy.base import Strategy, StrategyContext


def _rsi(closes: np.ndarray, period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1) :])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class RsiMeanRevert(Strategy):
    name = "rsi_revert"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("period", ParamType.INT, 14, min=5, max=50, step=1),
            ParamDef("oversold", ParamType.FLOAT, 30.0, min=10, max=40, step=2),
            ParamDef("overbought", ParamType.FLOAT, 70.0, min=60, max=90, step=2),
            ParamDef("sl_pct", ParamType.FLOAT, 0.004, min=0.0005, max=0.02, step=0.0005),
            ParamDef("tp_pct", ParamType.FLOAT, 0.008, min=0.001, max=0.05, step=0.001),
        ]

    def warmup_bars(self) -> int:
        return int(self.params["period"]) + 5

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        period = int(self.params["period"])
        if index < period + 1:
            return []

        closes = np.array([c.close for c in candles[: index + 1]], dtype=float)
        rsi = _rsi(closes, period)
        price = candles[index].close
        sl_pct = float(self.params["sl_pct"])
        tp_pct = float(self.params["tp_pct"])
        oversold = float(self.params["oversold"])
        overbought = float(self.params["overbought"])

        if rsi <= oversold:
            return [
                Signal(
                    action=SignalAction.BUY,
                    stop_loss=price * (1 - sl_pct),
                    take_profit=price * (1 + tp_pct),
                    tag="rsi_oversold",
                )
            ]
        if rsi >= overbought:
            return [
                Signal(
                    action=SignalAction.SELL,
                    stop_loss=price * (1 + sl_pct),
                    take_profit=price * (1 - tp_pct),
                    tag="rsi_overbought",
                )
            ]
        return []
