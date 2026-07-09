"""AOI bounce / continuation — top-down trend, S/R zones, candlestick confirmation.

1. HTF (4h) swing structure defines current trend — trade only with it.
2. Entry TF swing clusters form areas of interest (recent swings weighted).
3. Wait for rejection or continuation candlestick at an AOI.
4. TP at next AOI in trade direction; SL sized for reward:risk >= min_rr.
"""

from __future__ import annotations

from borex.models.params import ParamDef, ParamType
from borex.models.signal import Candle, Signal, SignalAction
from borex.strategy.aoi import (
    atr_tolerance,
    build_aoi_levels,
    entry_signal_at_level,
    nearest_level,
    next_level_in_direction,
    price_at_level,
    stops_from_reward_risk,
    trend_from_structure,
)
from borex.strategy.base import StrategyContext
from borex.strategy.mtf import MtfSpec, MtfStrategy


class AoiBounce(MtfStrategy):
    name = "aoi_bounce"

    @classmethod
    def mtf_spec(cls) -> MtfSpec:
        return MtfSpec(bias_timeframes=("4h",), entry_timeframes=("15m", "30m", "1h"))

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("swing_left", ParamType.INT, 3, min=2, max=8, description="Swing pivot bars left"),
            ParamDef("swing_right", ParamType.INT, 3, min=2, max=8, description="Swing pivot bars right"),
            ParamDef("aoi_lookback", ParamType.INT, 200, min=80, max=500, step=20),
            ParamDef("trend_lookback", ParamType.INT, 120, min=60, max=300, step=20),
            ParamDef("atr_period", ParamType.INT, 14, min=7, max=28),
            ParamDef("touch_atr", ParamType.FLOAT, 0.35, min=0.15, max=0.8, step=0.05),
            ParamDef("merge_atr", ParamType.FLOAT, 0.5, min=0.2, max=1.2, step=0.1),
            ParamDef("min_wick", ParamType.FLOAT, 0.55, min=0.4, max=0.75, step=0.05),
            ParamDef("min_body_pct", ParamType.FLOAT, 0.45, min=0.3, max=0.7, step=0.05),
            ParamDef("min_rr", ParamType.FLOAT, 2.0, min=2.0, max=4.0, step=0.5),
        ]

    def warmup_bars(self) -> int:
        return int(self.params["aoi_lookback"]) + int(self.params["swing_left"]) + int(self.params["swing_right"]) + 5

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        lb = int(self.params["aoi_lookback"])
        if index < lb:
            return []

        mtf = self._mtf(ctx)
        bias_idx = mtf.htf_idx("4h")
        if bias_idx < int(self.params["trend_lookback"]):
            return []

        left = int(self.params["swing_left"])
        right = int(self.params["swing_right"])
        atr_p = int(self.params["atr_period"])
        touch_tol = atr_tolerance(candles, index, atr_p, float(self.params["touch_atr"]))
        merge_dist = atr_tolerance(candles, index, atr_p, float(self.params["merge_atr"]))
        min_dist = atr_tolerance(candles, index, atr_p, 0.25)
        min_wick = float(self.params["min_wick"])
        min_body = float(self.params["min_body_pct"])
        min_rr = float(self.params["min_rr"])

        htf_candles = mtf.htf_candles("4h")
        trend = trend_from_structure(
            htf_candles,
            bias_idx,
            left=left,
            right=right,
            lookback=int(self.params["trend_lookback"]),
        )
        if trend == "neutral":
            return []

        levels = build_aoi_levels(
            candles,
            index,
            left=left,
            right=right,
            lookback=lb,
            merge_dist=merge_dist,
        )
        if len(levels) < 2:
            return []

        c = candles[index]
        active = nearest_level(c.close, levels, tolerance=touch_tol)
        if active is None or not price_at_level(c, active.price, touch_tol):
            return []

        side = entry_signal_at_level(
            candles,
            index,
            active,
            min_wick=min_wick,
            min_body_pct=min_body,
        )
        if side is None:
            return []
        if trend == "up" and side != "long":
            return []
        if trend == "down" and side != "short":
            return []

        entry = c.close
        tp_price = next_level_in_direction(entry, levels, side, min_distance=min_dist)
        if tp_price is None:
            return []

        stops = stops_from_reward_risk(entry, tp_price, side, min_rr)
        if stops is None:
            return []
        sl, tp = stops

        if side == "long":
            return [
                Signal(
                    SignalAction.BUY,
                    stop_loss=sl,
                    take_profit=tp,
                    tag="aoi_bounce_long",
                )
            ]
        return [
            Signal(
                SignalAction.SELL,
                stop_loss=sl,
                take_profit=tp,
                tag="aoi_bounce_short",
            )
        ]
