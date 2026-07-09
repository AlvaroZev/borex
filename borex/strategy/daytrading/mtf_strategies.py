from __future__ import annotations

import numpy as np

from borex.models.params import ParamDef, ParamType
from borex.models.signal import Candle, Signal, SignalAction
from borex.strategy.base import StrategyContext
from borex.strategy.indicators import (
    ema,
    is_bearish_engulfing,
    is_bullish_engulfing,
    rsi,
    session_vwap,
    sl_tp,
    wick_ratio,
)
from borex.strategy.mtf import MtfSpec, MtfStrategy


def _risk() -> list[ParamDef]:
    return [
        ParamDef("sl_pct", ParamType.FLOAT, 0.003, min=0.0005, max=0.02, step=0.0005),
        ParamDef("tp_pct", ParamType.FLOAT, 0.006, min=0.001, max=0.05, step=0.001),
    ]


class EmaTrendBias(MtfStrategy):
    """100 EMA on 1H sets bias; pullback to entry EMA on lower TF."""

    name = "ema_trend_bias"

    @classmethod
    def mtf_spec(cls) -> MtfSpec:
        return MtfSpec(bias_timeframes=("1h",), entry_timeframes=("1m", "15m", "30m"))

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("bias_ema", ParamType.INT, 100, min=50, max=200, step=10),
            ParamDef("entry_ema", ParamType.INT, 20, min=5, max=50, step=5),
            *_risk(),
        ]

    def warmup_bars(self) -> int:
        return int(self.params["entry_ema"]) + 5

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        mtf = self._mtf(ctx)
        bias_idx = mtf.htf_idx("1h")
        if bias_idx < int(self.params["bias_ema"]):
            return []
        htf_closes = mtf.htf_closes_through("1h")
        bias_ma = ema(htf_closes, int(self.params["bias_ema"]))
        closes = np.array([c.close for c in candles[: index + 1]])
        fast_ma = ema(closes, int(self.params["entry_ema"]))
        c = candles[index]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        htf_close = mtf.htf_closed("1h").close  # type: ignore[union-attr]
        if htf_close > bias_ma and c.low <= fast_ma <= c.high:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="ema_mtf_long")]
        if htf_close < bias_ma and c.low <= fast_ma <= c.high:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="ema_mtf_short")]
        return []


class FirstHourTrend(MtfStrategy):
    """First 1H bar of day sets range; trade continuation on lower TF."""

    name = "first_hour_trend"

    @classmethod
    def mtf_spec(cls) -> MtfSpec:
        return MtfSpec(bias_timeframes=("1h",), entry_timeframes=("15m", "30m"))

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return _risk()

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        if index < 2:
            return []
        mtf = self._mtf(ctx)
        first = mtf.first_htf_bar_of_day("1h")
        if first is None:
            return []
        # Only trade after first hour has closed
        day_bars = mtf.day_htf_bars("1h")
        if len(day_bars) < 2:
            return []
        hi = first.high
        lo = first.low
        mid = (hi + lo) / 2
        c = candles[index]
        prev = candles[index - 1]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if prev.close <= hi and c.close > hi:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="first_hour_break_long")]
        if prev.close >= lo and c.close < lo:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="first_hour_break_short")]
        htf = mtf.htf_closed("1h")
        if htf and htf.close > mid and c.close > prev.close:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="first_hour_bias_long")]
        if htf and htf.close < mid and c.close < prev.close:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="first_hour_bias_short")]
        return []


class OpeningRangeBreakout(MtfStrategy):
    """ORB: first 1H bar = range; break on entry TF."""

    name = "opening_range_breakout"

    @classmethod
    def mtf_spec(cls) -> MtfSpec:
        return MtfSpec(bias_timeframes=("1h",), entry_timeframes=("15m", "30m", "1m"))

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return _risk()

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        if index < 1:
            return []
        mtf = self._mtf(ctx)
        first = mtf.first_htf_bar_of_day("1h")
        if first is None or len(mtf.day_htf_bars("1h")) < 2:
            return []
        hi, lo = first.high, first.low
        c, prev = candles[index], candles[index - 1]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if prev.close <= hi and c.close > hi:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="orb_mtf_long")]
        if prev.close >= lo and c.close < lo:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="orb_mtf_short")]
        return []


class OpeningRangeRetest(MtfStrategy):
    """ORB break on 1H range, retest entry on lower TF."""

    name = "opening_range_retest"

    @classmethod
    def mtf_spec(cls) -> MtfSpec:
        return MtfSpec(bias_timeframes=("1h",), entry_timeframes=("15m", "30m"))

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return _risk()

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        if index < 3:
            return []
        mtf = self._mtf(ctx)
        first = mtf.first_htf_bar_of_day("1h")
        if first is None or len(mtf.day_htf_bars("1h")) < 2:
            return []
        hi, lo = first.high, first.low
        c = candles[index]
        day = pd_day(c.timestamp)
        day_idxs = [i for i in range(index + 1) if pd_day(candles[i].timestamp) == day]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        broke_up = any(candles[i].close > hi for i in day_idxs[:-1])
        if broke_up and c.low <= hi <= c.high and c.close > hi:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="orb_retest_mtf_long")]
        broke_dn = any(candles[i].close < lo for i in day_idxs[:-1])
        if broke_dn and c.low <= lo <= c.high and c.close < lo:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="orb_retest_mtf_short")]
        return []


class StopHuntReversal(MtfStrategy):
    """Previous day H/L from 1D; fake break on entry TF."""

    name = "stop_hunt_reversal"

    @classmethod
    def mtf_spec(cls) -> MtfSpec:
        return MtfSpec(bias_timeframes=("1d",), entry_timeframes=("15m", "30m", "1h"))

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return _risk()

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        mtf = self._mtf(ctx)
        prev_day = mtf.htf_closed("1d")
        if prev_day is None:
            return []
        prev_hi, prev_lo = prev_day.high, prev_day.low
        c = candles[index]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if c.high > prev_hi and c.close < prev_hi:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="stop_hunt_mtf_short")]
        if c.low < prev_lo and c.close > prev_lo:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="stop_hunt_mtf_long")]
        return []


class SessionOpenReversal(MtfStrategy):
    """Fade extended first 1H move; reversal candle on entry TF."""

    name = "session_open_reversal"

    @classmethod
    def mtf_spec(cls) -> MtfSpec:
        return MtfSpec(bias_timeframes=("1h",), entry_timeframes=("15m", "30m"))

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("move_pct", ParamType.FLOAT, 0.004, min=0.002, max=0.02),
            *_risk(),
        ]

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        if index < 1:
            return []
        mtf = self._mtf(ctx)
        first = mtf.first_htf_bar_of_day("1h")
        if first is None or len(mtf.day_htf_bars("1h")) < 2:
            return []
        move = (first.close - first.open) / first.open if first.open else 0
        c, prev = candles[index], candles[index - 1]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        thr = float(self.params["move_pct"])
        if move > thr and is_bearish_engulfing(prev, c):
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="session_fade_mtf_short")]
        if move < -thr and is_bullish_engulfing(prev, c):
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="session_fade_mtf_long")]
        return []


MTF_STRATEGIES: list[type[MtfStrategy]] = [
    EmaTrendBias,
    FirstHourTrend,
    OpeningRangeBreakout,
    OpeningRangeRetest,
    StopHuntReversal,
    SessionOpenReversal,
]
