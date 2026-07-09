"""Forex-adapted day-trading strategies (OHLCV only).

Stock-only setups (L2, options OI, halts, earnings) are not included.
See STRATEGY_NOTES in borex/strategy/daytrading/README.md for mapping.
"""

from __future__ import annotations

import numpy as np

from borex.models.params import ParamDef, ParamType
from borex.models.signal import Candle, Signal, SignalAction
from borex.strategy.base import Strategy, StrategyContext
from borex.strategy.indicators import (
    consecutive_green,
    ema,
    is_bearish_engulfing,
    is_bullish_engulfing,
    rsi,
    session_vwap,
    sl_tp,
    wick_ratio,
)


def _risk() -> list[ParamDef]:
    return [
        ParamDef("sl_pct", ParamType.FLOAT, 0.003, min=0.0005, max=0.02, step=0.0005),
        ParamDef("tp_pct", ParamType.FLOAT, 0.006, min=0.001, max=0.05, step=0.001),
    ]


class EmaStack(Strategy):
    """20/50/200 EMA stack — trade in alignment direction."""

    name = "ema_stack"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("ema_fast", ParamType.INT, 20, min=5, max=50),
            ParamDef("ema_mid", ParamType.INT, 50, min=20, max=100),
            ParamDef("ema_slow", ParamType.INT, 200, min=100, max=300),
            *_risk(),
        ]

    def warmup_bars(self) -> int:
        return int(self.params["ema_slow"]) + 2

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        f, m, s = int(self.params["ema_fast"]), int(self.params["ema_mid"]), int(self.params["ema_slow"])
        if index < s:
            return []
        closes = np.array([c.close for c in candles[: index + 1]])
        ef, em, es = ema(closes, f), ema(closes, m), ema(closes, s)
        c = candles[index]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if ef > em > es and c.close > ef:
            sl_p, tp_p = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=sl_p, take_profit=tp_p, tag="ema_stack_long")]
        if ef < em < es and c.close < ef:
            sl_p, tp_p = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=sl_p, take_profit=tp_p, tag="ema_stack_short")]
        return []


class VwapBounce(Strategy):
    """VWAP touch with rejection wick + volume (no blind first touch)."""

    name = "vwap_bounce"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("min_wick", ParamType.FLOAT, 0.55, min=0.4, max=0.8, step=0.05),
            ParamDef("vol_mult", ParamType.FLOAT, 1.5, min=1.0, max=3.0, step=0.25),
            *_risk(),
        ]

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        if index < 20:
            return []
        c = candles[index]
        vwap = session_vwap(candles, index)
        lower, upper = wick_ratio(c)
        avg_vol = np.mean([candles[i].volume for i in range(index - 19, index)]) or 1.0
        vol_ok = c.volume >= avg_vol * float(self.params["vol_mult"])
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        mw = float(self.params["min_wick"])
        if vol_ok and c.low <= vwap <= c.high and lower >= mw and c.close > vwap:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="vwap_bounce_long")]
        if vol_ok and c.low <= vwap <= c.high and upper >= mw and c.close < vwap:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="vwap_bounce_short")]
        return []


class ParabolicShort(Strategy):
    """N consecutive green candles then bearish engulfing."""

    name = "parabolic_short"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("green_bars", ParamType.INT, 5, min=3, max=10),
            *_risk(),
        ]

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        n = int(self.params["green_bars"])
        if index < n:
            return []
        if consecutive_green(candles, index - 1, n) and is_bearish_engulfing(candles[index - 1], candles[index]):
            c = candles[index]
            sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="parabolic_short")]
        return []


class RsiExhaustion(Strategy):
    """RSI extreme + reversal engulfing."""

    name = "rsi_exhaustion"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("period", ParamType.INT, 14, min=5, max=30),
            ParamDef("overbought", ParamType.FLOAT, 90.0, min=80, max=95),
            ParamDef("oversold", ParamType.FLOAT, 10.0, min=5, max=20),
            *_risk(),
        ]

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        p = int(self.params["period"])
        if index < p + 1:
            return []
        closes = np.array([c.close for c in candles[: index + 1]])
        r = rsi(closes, p)
        c, prev = candles[index], candles[index - 1]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if r >= float(self.params["overbought"]) and is_bearish_engulfing(prev, c):
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="rsi_exhaust_short")]
        if r <= float(self.params["oversold"]) and is_bullish_engulfing(prev, c):
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="rsi_exhaust_long")]
        return []


class VolumeAbsorption(Strategy):
    """High volume, small body — trade with prior bar direction."""

    name = "volume_absorption"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("vol_mult", ParamType.FLOAT, 2.0, min=1.5, max=4.0, step=0.25),
            ParamDef("max_body_pct", ParamType.FLOAT, 0.25, min=0.1, max=0.4),
            *_risk(),
        ]

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        if index < 21:
            return []
        c, prev = candles[index], candles[index - 1]
        rng = c.high - c.low
        if rng <= 0:
            return []
        body_pct = abs(c.close - c.open) / rng
        avg_vol = np.mean([candles[i].volume for i in range(index - 20, index)]) or 1.0
        if c.volume < avg_vol * float(self.params["vol_mult"]):
            return []
        if body_pct > float(self.params["max_body_pct"]):
            return []
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if prev.close > prev.open:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="vol_absorb_long")]
        if prev.close < prev.open:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="vol_absorb_short")]
        return []


class RipAndDip(Strategy):
    """Break range high, dip, reclaim — long."""

    name = "rip_and_dip"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("lookback", ParamType.INT, 20, min=10, max=60),
            *_risk(),
        ]

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        lb = int(self.params["lookback"])
        if index < lb + 2:
            return []
        window = candles[index - lb - 2 : index - 2]
        range_hi = max(c.high for c in window)
        b1, b2, b3 = candles[index - 2], candles[index - 1], candles[index]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if b1.high > range_hi and b2.close < b2.open and b3.close > b1.high:
            s, t = sl_tp(b3.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="rip_and_dip")]
        range_lo = min(c.low for c in window)
        if b1.low < range_lo and b2.close > b2.open and b3.close < b1.low:
            s, t = sl_tp(b3.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="rip_and_dip_short")]
        return []


class PanicBounce(Strategy):
    """Sharp drop then bullish reversal (dip trade / bagholder bounce proxy)."""

    name = "panic_bounce"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("drop_pct", ParamType.FLOAT, 0.008, min=0.003, max=0.03, step=0.001),
            ParamDef("lookback", ParamType.INT, 10, min=5, max=30),
            *_risk(),
        ]

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        lb = int(self.params["lookback"])
        if index < lb + 1:
            return []
        peak = max(candles[i].high for i in range(index - lb, index))
        c, prev = candles[index], candles[index - 1]
        drop = (peak - c.low) / peak if peak > 0 else 0
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if drop >= float(self.params["drop_pct"]) and is_bullish_engulfing(prev, c):
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="panic_bounce")]
        trough = min(candles[i].low for i in range(index - lb, index))
        rip = (c.high - trough) / trough if trough > 0 else 0
        if rip >= float(self.params["drop_pct"]) and is_bearish_engulfing(prev, c):
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="panic_rip_short")]
        return []


class BullFlagFakeout(Strategy):
    """Micro flag, fake breakdown, reclaim."""

    name = "bull_flag_fakeout"

    @classmethod
    def param_schema(cls) -> list[ParamDef]:
        return [
            ParamDef("flag_bars", ParamType.INT, 5, min=3, max=15),
            *_risk(),
        ]

    def on_bar(self, index: int, candles: list[Candle], ctx: StrategyContext) -> list[Signal]:
        fb = int(self.params["flag_bars"])
        if index < fb + 3:
            return []
        pole = candles[index - fb - 3 : index - fb]
        flag = candles[index - fb : index]
        if pole[-1].close <= pole[0].open:
            return []
        flag_lo = min(c.low for c in flag)
        flag_hi = max(c.high for c in flag)
        c = candles[index]
        prev = candles[index - 1]
        sl, tp = float(self.params["sl_pct"]), float(self.params["tp_pct"])
        if prev.close < flag_lo and c.close > flag_lo:
            s, t = sl_tp(c.close, "long", sl, tp)
            return [Signal(SignalAction.BUY, stop_loss=s, take_profit=t, tag="flag_fakeout_long")]
        if pole[0].open < pole[-1].close:
            return []
        if prev.close > flag_hi and c.close < flag_hi:
            s, t = sl_tp(c.close, "short", sl, tp)
            return [Signal(SignalAction.SELL, stop_loss=s, take_profit=t, tag="flag_fakeout_short")]
        return []


ALL_STRATEGIES: list[type[Strategy]] = [
    EmaStack,
    VwapBounce,
    ParabolicShort,
    RsiExhaustion,
    VolumeAbsorption,
    RipAndDip,
    PanicBounce,
    BullFlagFakeout,
]
