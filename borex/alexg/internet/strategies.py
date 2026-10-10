"""Internet1–20 mechanical strategy signals (DISCOVERY_ONLY).

Each strategy emits at most one signal per closed bar using only past data.
Sources documented in internet_strategy_sources.json — rules paraphrased,
not copied verbatim from copyrighted text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from borex.alexg.internet.indicators import (
    atr,
    bollinger,
    donchian_high,
    donchian_low,
    ema_series,
    rsi,
    sma,
)
from borex.models.candle import Candle

Side = Literal["long", "short"]


@dataclass(frozen=True)
class InternetSignal:
    side: Side
    entry: float
    sl: float
    tp: float
    pattern: str
    index: int
    timestamp: object


SignalFn = Callable[..., InternetSignal | None]


def _rr_levels(entry: float, side: Side, sl_dist: float, rr: float) -> tuple[float, float]:
    if side == "long":
        return entry - sl_dist, entry + sl_dist * rr
    return entry + sl_dist, entry - sl_dist * rr


def internet1_donchian20(candles: list[Candle], i: int, *, rr: float = 2.0, atr_mult: float = 2.0) -> InternetSignal | None:
    """Turtle System-1 style: close beyond prior 20-bar Donchian; SL=2*ATR."""
    hi = donchian_high(candles, i, 20)
    lo = donchian_low(candles, i, 20)
    a = atr(candles, i, 14)
    if hi is None or lo is None or a is None or a <= 0:
        return None
    c = candles[i]
    if c.close > hi:
        sl, tp = _rr_levels(c.close, "long", atr_mult * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet1|donchian20", i, c.timestamp)
    if c.close < lo:
        sl, tp = _rr_levels(c.close, "short", atr_mult * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet1|donchian20", i, c.timestamp)
    return None


def internet2_donchian55(candles: list[Candle], i: int, *, rr: float = 2.0, atr_mult: float = 2.0) -> InternetSignal | None:
    hi = donchian_high(candles, i, 55)
    lo = donchian_low(candles, i, 55)
    a = atr(candles, i, 14)
    if hi is None or lo is None or a is None or a <= 0:
        return None
    c = candles[i]
    if c.close > hi:
        sl, tp = _rr_levels(c.close, "long", atr_mult * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet2|donchian55", i, c.timestamp)
    if c.close < lo:
        sl, tp = _rr_levels(c.close, "short", atr_mult * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet2|donchian55", i, c.timestamp)
    return None


def internet3_sma_cross(candles: list[Candle], i: int, *, fast: int = 10, slow: int = 30, rr: float = 2.0) -> InternetSignal | None:
    if i < slow + 1:
        return None
    closes = [c.close for c in candles[: i + 1]]
    f0, f1 = sma(closes[:-1], fast), sma(closes, fast)
    s0, s1 = sma(closes[:-1], slow), sma(closes, slow)
    a = atr(candles, i, 14)
    if None in (f0, f1, s0, s1, a) or a <= 0:
        return None
    c = candles[i]
    if f0 <= s0 and f1 > s1:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet3|sma_cross", i, c.timestamp)
    if f0 >= s0 and f1 < s1:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet3|sma_cross", i, c.timestamp)
    return None


def internet4_ema_trend(candles: list[Candle], i: int, *, period: int = 50, rr: float = 2.0) -> InternetSignal | None:
    if i < period + 1:
        return None
    closes = [c.close for c in candles[: i + 1]]
    emas = ema_series(closes, period)
    a = atr(candles, i, 14)
    if emas[i] is None or emas[i - 1] is None or a is None or a <= 0:
        return None
    c = candles[i]
    # price cross above rising EMA
    if closes[i - 1] <= emas[i - 1] and closes[i] > emas[i] and emas[i] > emas[i - 1]:
        sl, tp = _rr_levels(c.close, "long", 2.0 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet4|ema_trend", i, c.timestamp)
    if closes[i - 1] >= emas[i - 1] and closes[i] < emas[i] and emas[i] < emas[i - 1]:
        sl, tp = _rr_levels(c.close, "short", 2.0 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet4|ema_trend", i, c.timestamp)
    return None


def internet5_atr_breakout(candles: list[Candle], i: int, *, lookback: int = 20, k: float = 1.5, rr: float = 2.0) -> InternetSignal | None:
    if i < lookback + 1:
        return None
    a = atr(candles, i - 1, 14)
    if a is None or a <= 0:
        return None
    prev = candles[i - 1]
    c = candles[i]
    up = prev.close + k * a
    dn = prev.close - k * a
    if c.close > up:
        sl, tp = _rr_levels(c.close, "long", k * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet5|atr_breakout", i, c.timestamp)
    if c.close < dn:
        sl, tp = _rr_levels(c.close, "short", k * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet5|atr_breakout", i, c.timestamp)
    return None


def internet6_rsi_mr(candles: list[Candle], i: int, *, period: int = 14, rr: float = 1.5) -> InternetSignal | None:
    closes = [c.close for c in candles[: i + 1]]
    r0 = rsi(closes, i - 1, period) if i >= 1 else None
    r1 = rsi(closes, i, period)
    a = atr(candles, i, 14)
    if r0 is None or r1 is None or a is None or a <= 0:
        return None
    c = candles[i]
    if r0 <= 30 < r1:
        sl, tp = _rr_levels(c.close, "long", 1.0 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet6|rsi_mr", i, c.timestamp)
    if r0 >= 70 > r1:
        sl, tp = _rr_levels(c.close, "short", 1.0 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet6|rsi_mr", i, c.timestamp)
    return None


def internet7_bollinger_mr(candles: list[Candle], i: int, *, rr: float = 1.0) -> InternetSignal | None:
    closes = [c.close for c in candles[: i + 1]]
    bb = bollinger(closes, i, 20, 2.0)
    a = atr(candles, i, 14)
    if bb is None or a is None or a <= 0:
        return None
    lo, mid, hi = bb
    c = candles[i]
    if c.close < lo:
        sl, tp = c.close - a, mid  # target mid band
        return InternetSignal("long", c.close, sl, tp, "internet7|bb_mr", i, c.timestamp)
    if c.close > hi:
        sl, tp = c.close + a, mid
        return InternetSignal("short", c.close, sl, tp, "internet7|bb_mr", i, c.timestamp)
    return None


def internet8_ma_pullback(candles: list[Candle], i: int, *, trend: int = 50, rr: float = 2.0) -> InternetSignal | None:
    if i < trend + 5:
        return None
    closes = [c.close for c in candles[: i + 1]]
    t = sma(closes, trend)
    a = atr(candles, i, 14)
    if t is None or a is None or a <= 0:
        return None
    c = candles[i]
    # uptrend pullback: close near MA from above then bounce
    if closes[i] > t and candles[i - 1].low <= t <= candles[i - 1].high and c.close > t and c.close > c.open:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet8|ma_pullback", i, c.timestamp)
    if closes[i] < t and candles[i - 1].low <= t <= candles[i - 1].high and c.close < t and c.close < c.open:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet8|ma_pullback", i, c.timestamp)
    return None


def internet9_momentum_roc(candles: list[Candle], i: int, *, n: int = 12, thr: float = 0.003, rr: float = 2.0) -> InternetSignal | None:
    if i < n + 1:
        return None
    c = candles[i]
    prev = candles[i - n]
    roc = (c.close - prev.close) / prev.close if prev.close else 0.0
    a = atr(candles, i, 14)
    if a is None or a <= 0:
        return None
    if roc > thr and candles[i - 1].close - candles[i - 1 - n].close <= thr * candles[i - 1 - n].close:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet9|roc", i, c.timestamp)
    if roc < -thr and candles[i - 1].close - candles[i - 1 - n].close >= -thr * candles[i - 1 - n].close:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet9|roc", i, c.timestamp)
    return None


def internet10_vol_breakout(candles: list[Candle], i: int, *, lookback: int = 20, rr: float = 2.0) -> InternetSignal | None:
    if i < lookback + 1:
        return None
    ranges = [candles[j].high - candles[j].low for j in range(i - lookback, i)]
    avg_r = sum(ranges) / lookback
    c = candles[i]
    a = atr(candles, i, 14)
    if a is None or a <= 0 or avg_r <= 0:
        return None
    # contraction then expansion break
    if (c.high - c.low) > 1.8 * avg_r and c.close > c.open and c.close > max(x.close for x in candles[i - 5 : i]):
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet10|vol_bo", i, c.timestamp)
    if (c.high - c.low) > 1.8 * avg_r and c.close < c.open and c.close < min(x.close for x in candles[i - 5 : i]):
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet10|vol_bo", i, c.timestamp)
    return None


def internet11_range_breakout(candles: list[Candle], i: int, *, n: int = 24, rr: float = 2.0) -> InternetSignal | None:
    hi = donchian_high(candles, i, n)
    lo = donchian_low(candles, i, n)
    a = atr(candles, i, 14)
    if hi is None or lo is None or a is None or a <= 0:
        return None
    c = candles[i]
    width = hi - lo
    if width <= 0:
        return None
    # only if range relatively tight vs ATR
    if width > 4 * a:
        return None
    if c.close > hi:
        sl, tp = _rr_levels(c.close, "long", max(a, width * 0.5), rr)
        return InternetSignal("long", c.close, sl, tp, "internet11|range_bo", i, c.timestamp)
    if c.close < lo:
        sl, tp = _rr_levels(c.close, "short", max(a, width * 0.5), rr)
        return InternetSignal("short", c.close, sl, tp, "internet11|range_bo", i, c.timestamp)
    return None


def internet12_pdh_pdl_break(candles: list[Candle], i: int, *, rr: float = 2.0) -> InternetSignal | None:
    """Previous calendar-day high/low breakout (causal after day completes)."""
    import pandas as pd

    if i < 30:
        return None
    cur_day = pd.Timestamp(candles[i].timestamp)
    if cur_day.tzinfo is None:
        cur_day = cur_day.tz_localize("UTC")
    else:
        cur_day = cur_day.tz_convert("UTC")
    day = cur_day.date()
    # find previous day bars
    prev_bars = []
    for j in range(i - 1, -1, -1):
        t = pd.Timestamp(candles[j].timestamp)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        if t.date() == day:
            continue
        prev_day = t.date()
        # collect that day
        for k in range(j, -1, -1):
            tk = pd.Timestamp(candles[k].timestamp)
            tk = tk.tz_localize("UTC") if tk.tzinfo is None else tk.tz_convert("UTC")
            if tk.date() != prev_day:
                break
            prev_bars.append(candles[k])
        break
    if not prev_bars:
        return None
    pdh = max(b.high for b in prev_bars)
    pdl = min(b.low for b in prev_bars)
    a = atr(candles, i, 14)
    if a is None or a <= 0:
        return None
    c = candles[i]
    if c.close > pdh:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet12|pdh", i, c.timestamp)
    if c.close < pdl:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet12|pdl", i, c.timestamp)
    return None


def internet13_session_london_bo(candles: list[Candle], i: int, *, rr: float = 2.0) -> InternetSignal | None:
    """Break of Asia range (00–07 UTC) during London open hour 07–09 UTC."""
    import pandas as pd

    c = candles[i]
    t = pd.Timestamp(c.timestamp)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    if not (7 <= t.hour < 9):
        return None
    # asia highs/lows same day 0-7
    asia = []
    for j in range(i, -1, -1):
        tj = pd.Timestamp(candles[j].timestamp)
        tj = tj.tz_localize("UTC") if tj.tzinfo is None else tj.tz_convert("UTC")
        if tj.date() != t.date():
            break
        if 0 <= tj.hour < 7:
            asia.append(candles[j])
    if len(asia) < 3:
        return None
    ahi, alo = max(x.high for x in asia), min(x.low for x in asia)
    a = atr(candles, i, 14)
    if a is None or a <= 0:
        return None
    if c.close > ahi:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet13|london_bo", i, c.timestamp)
    if c.close < alo:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet13|london_bo", i, c.timestamp)
    return None


def internet14_swing_bo(candles: list[Candle], i: int, *, n: int = 5, rr: float = 2.0) -> InternetSignal | None:
    if i < n * 2 + 2:
        return None
    # simple fractal: break prior swing high/low confirmed n bars ago
    piv = i - n - 1
    if piv < n:
        return None
    seg = candles[piv - n : piv + n + 1]
    is_sh = candles[piv].high >= max(x.high for x in seg)
    is_sl = candles[piv].low <= min(x.low for x in seg)
    a = atr(candles, i, 14)
    if a is None or a <= 0:
        return None
    c = candles[i]
    if is_sh and c.close > candles[piv].high:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet14|swing_bo", i, c.timestamp)
    if is_sl and c.close < candles[piv].low:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet14|swing_bo", i, c.timestamp)
    return None


def internet15_adx_trend_filter(candles: list[Candle], i: int, *, rr: float = 2.0) -> InternetSignal | None:
    """DI+/DI- cross with crude ADX proxy (smoothed |DI+ - DI-|)."""
    if i < 30:
        return None
    period = 14
    plus_dm = minus_dm = tr_sum = 0.0
    for j in range(i - period + 1, i + 1):
        up = candles[j].high - candles[j - 1].high
        dn = candles[j - 1].low - candles[j].low
        plus_dm += up if up > dn and up > 0 else 0.0
        minus_dm += dn if dn > up and dn > 0 else 0.0
        tr_sum += max(
            candles[j].high - candles[j].low,
            abs(candles[j].high - candles[j - 1].close),
            abs(candles[j].low - candles[j - 1].close),
        )
    if tr_sum <= 0:
        return None
    di_p = 100 * plus_dm / tr_sum
    di_m = 100 * minus_dm / tr_sum
    adx_proxy = abs(di_p - di_m)
    if adx_proxy < 15:
        return None
    a = atr(candles, i, 14)
    if a is None or a <= 0:
        return None
    c = candles[i]
    # prior bar DI
    # signal on cross approx: di_p > di_m and close > prior
    if di_p > di_m and c.close > candles[i - 1].close:
        sl, tp = _rr_levels(c.close, "long", 2.0 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet15|adx", i, c.timestamp)
    if di_m > di_p and c.close < candles[i - 1].close:
        sl, tp = _rr_levels(c.close, "short", 2.0 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet15|adx", i, c.timestamp)
    return None


def internet16_macd_cross(candles: list[Candle], i: int, *, rr: float = 2.0) -> InternetSignal | None:
    if i < 40:
        return None
    closes = [c.close for c in candles[: i + 1]]
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    if e12[i] is None or e26[i] is None or e12[i - 1] is None or e26[i - 1] is None:
        return None
    macd0 = e12[i - 1] - e26[i - 1]
    macd1 = e12[i] - e26[i]
    # signal line approx EMA9 of macd — use simple of last 9 macd values
    macds = []
    for j in range(i - 8, i + 1):
        if e12[j] is None or e26[j] is None:
            return None
        macds.append(e12[j] - e26[j])
    sig = sum(macds) / 9
    a = atr(candles, i, 14)
    if a is None or a <= 0:
        return None
    c = candles[i]
    if macd0 <= sig and macd1 > sig:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet16|macd", i, c.timestamp)
    if macd0 >= sig and macd1 < sig:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet16|macd", i, c.timestamp)
    return None


def internet17_stoch_mr(candles: list[Candle], i: int, *, n: int = 14, rr: float = 1.5) -> InternetSignal | None:
    if i < n + 1:
        return None
    window = candles[i - n + 1 : i + 1]
    hh, ll = max(x.high for x in window), min(x.low for x in window)
    if hh <= ll:
        return None
    k = 100 * (candles[i].close - ll) / (hh - ll)
    prev_w = candles[i - n : i]
    phh, pll = max(x.high for x in prev_w), min(x.low for x in prev_w)
    k0 = 100 * (candles[i - 1].close - pll) / (phh - pll) if phh > pll else 50
    a = atr(candles, i, 14)
    if a is None or a <= 0:
        return None
    c = candles[i]
    if k0 <= 20 < k:
        sl, tp = _rr_levels(c.close, "long", 1.0 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet17|stoch", i, c.timestamp)
    if k0 >= 80 > k:
        sl, tp = _rr_levels(c.close, "short", 1.0 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet17|stoch", i, c.timestamp)
    return None


def internet18_keltner_bo(candles: list[Candle], i: int, *, period: int = 20, rr: float = 2.0) -> InternetSignal | None:
    if i < period + 1:
        return None
    closes = [c.close for c in candles[: i + 1]]
    mid = sma(closes, period)
    a = atr(candles, i, period)
    if mid is None or a is None or a <= 0:
        return None
    upper, lower = mid + 1.5 * a, mid - 1.5 * a
    c = candles[i]
    if c.close > upper:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet18|keltner", i, c.timestamp)
    if c.close < lower:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet18|keltner", i, c.timestamp)
    return None


def internet19_pdh_sweep_reclaim(candles: list[Candle], i: int, *, rr: float = 2.0) -> InternetSignal | None:
    """Sweep prior day low then close back above (bullish); inverse for high."""
    import pandas as pd

    sig_bo = internet12_pdh_pdl_break(candles, i, rr=rr)
    # reuse PDH/PDL calc
    if i < 30:
        return None
    cur_day = pd.Timestamp(candles[i].timestamp)
    cur_day = cur_day.tz_localize("UTC") if cur_day.tzinfo is None else cur_day.tz_convert("UTC")
    day = cur_day.date()
    prev_bars = []
    for j in range(i - 1, -1, -1):
        t = pd.Timestamp(candles[j].timestamp)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        if t.date() == day:
            continue
        prev_day = t.date()
        for k in range(j, -1, -1):
            tk = pd.Timestamp(candles[k].timestamp)
            tk = tk.tz_localize("UTC") if tk.tzinfo is None else tk.tz_convert("UTC")
            if tk.date() != prev_day:
                break
            prev_bars.append(candles[k])
        break
    if not prev_bars:
        return None
    pdh = max(b.high for b in prev_bars)
    pdl = min(b.low for b in prev_bars)
    a = atr(candles, i, 14)
    if a is None or a <= 0:
        return None
    c = candles[i]
    # bullish sweep: low < pdl and close > pdl
    if c.low < pdl and c.close > pdl:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet19|sweep_reclaim", i, c.timestamp)
    if c.high > pdh and c.close < pdh:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet19|sweep_reclaim", i, c.timestamp)
    return None


def internet20_hybrid_ma_rsi(candles: list[Candle], i: int, *, rr: float = 2.0) -> InternetSignal | None:
    """SMA trend filter + RSI pullback entry."""
    if i < 55:
        return None
    closes = [c.close for c in candles[: i + 1]]
    trend = sma(closes, 50)
    r = rsi(closes, i, 14)
    a = atr(candles, i, 14)
    if trend is None or r is None or a is None or a <= 0:
        return None
    c = candles[i]
    if c.close > trend and 35 <= r <= 45 and c.close > c.open:
        sl, tp = _rr_levels(c.close, "long", 1.5 * a, rr)
        return InternetSignal("long", c.close, sl, tp, "internet20|ma_rsi", i, c.timestamp)
    if c.close < trend and 55 <= r <= 65 and c.close < c.open:
        sl, tp = _rr_levels(c.close, "short", 1.5 * a, rr)
        return InternetSignal("short", c.close, sl, tp, "internet20|ma_rsi", i, c.timestamp)
    return None


INTERNET_FNS: dict[str, SignalFn] = {
    "internet1": internet1_donchian20,
    "internet2": internet2_donchian55,
    "internet3": internet3_sma_cross,
    "internet4": internet4_ema_trend,
    "internet5": internet5_atr_breakout,
    "internet6": internet6_rsi_mr,
    "internet7": internet7_bollinger_mr,
    "internet8": internet8_ma_pullback,
    "internet9": internet9_momentum_roc,
    "internet10": internet10_vol_breakout,
    "internet11": internet11_range_breakout,
    "internet12": internet12_pdh_pdl_break,
    "internet13": internet13_session_london_bo,
    "internet14": internet14_swing_bo,
    "internet15": internet15_adx_trend_filter,
    "internet16": internet16_macd_cross,
    "internet17": internet17_stoch_mr,
    "internet18": internet18_keltner_bo,
    "internet19": internet19_pdh_sweep_reclaim,
    "internet20": internet20_hybrid_ma_rsi,
}
