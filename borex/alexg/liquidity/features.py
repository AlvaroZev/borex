"""Objective OHLC liquidity feature primitives for alexg9 liquidity research.

NO LOOKAHEAD:
- A swing with right confirmation becomes usable only at bar index
  pivot + right (after confirming bars have closed).
- Previous-day / previous-week levels usable only after period close.
- Session high/low for a completed session usable only after session end;
  in-progress session levels use only bars observed so far (marked partial).
- FVG requires the 3rd candle closed; usable at that bar's index.
- Structure shift confirmed only after the confirming close.

These are observable price patterns — not institutional-intent claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from borex.alexg.sessions import TradingSession, classify_session, in_session
from borex.models.candle import Candle

Side = Literal["bullish", "bearish"]


def _ts(c: Candle) -> pd.Timestamp:
    t = pd.Timestamp(c.timestamp)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def atr(candles: list[Candle], end_i: int, period: int = 14) -> float:
    """Causal ATR using bars [0..end_i] inclusive (end_i must be closed)."""
    if end_i < 1 or period < 1:
        return 0.0
    start = max(1, end_i - period + 1)
    trs = []
    for i in range(start, end_i + 1):
        c, p = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        trs.append(tr)
    return float(sum(trs) / len(trs)) if trs else 0.0


def pip_size(symbol: str) -> float:
    s = symbol.upper().replace("=X", "").replace("X", "")
    if s.endswith("JPY"):
        return 0.01
    return 0.0001


@dataclass(frozen=True)
class CausalSwing:
    pivot_index: int
    usable_index: int  # first bar index where swing is confirmed/usable
    price: float
    kind: Literal["high", "low"]
    left: int
    right: int


def detect_causal_swings(
    candles: list[Candle],
    *,
    left: int = 3,
    right: int = 3,
) -> list[CausalSwing]:
    """Fractal swings; usable only after `right` confirming bars close."""
    out: list[CausalSwing] = []
    n = len(candles)
    if n < left + right + 1:
        return out
    # Pivot candidate i confirmed when we reach i+right
    for i in range(left, n - right):
        hi = candles[i].high
        lo = candles[i].low
        is_high = all(hi >= candles[j].high for j in range(i - left, i + right + 1) if j != i)
        is_low = all(lo <= candles[j].low for j in range(i - left, i + right + 1) if j != i)
        usable = i + right
        if is_high:
            out.append(
                CausalSwing(i, usable, hi, "high", left, right)
            )
        elif is_low:
            out.append(
                CausalSwing(i, usable, lo, "low", left, right)
            )
    return out


def swings_usable_at(swings: list[CausalSwing], bar_index: int) -> list[CausalSwing]:
    return [s for s in swings if s.usable_index <= bar_index]


@dataclass(frozen=True)
class EqualLevel:
    kind: Literal["high", "low"]
    price: float
    touch_indices: tuple[int, ...]
    usable_index: int  # max usable among member swings
    age_bars_at: int  # set relative to query time by caller


def find_equal_levels(
    swings: list[CausalSwing],
    *,
    bar_index: int,
    tolerance: float,
    min_touches: int = 2,
    max_age_bars: int = 500,
) -> list[EqualLevel]:
    usable = [
        s
        for s in swings_usable_at(swings, bar_index)
        if bar_index - s.usable_index <= max_age_bars
    ]
    levels: list[EqualLevel] = []
    used = set()
    for i, s in enumerate(usable):
        if i in used:
            continue
        cluster = [s]
        idxs = {i}
        for j, t in enumerate(usable):
            if j in idxs:
                continue
            if t.kind == s.kind and abs(t.price - s.price) <= tolerance:
                cluster.append(t)
                idxs.add(j)
        if len(cluster) >= min_touches:
            used |= idxs
            price = sum(c.price for c in cluster) / len(cluster)
            levels.append(
                EqualLevel(
                    kind=s.kind,
                    price=price,
                    touch_indices=tuple(sorted(c.pivot_index for c in cluster)),
                    usable_index=max(c.usable_index for c in cluster),
                    age_bars_at=bar_index - max(c.usable_index for c in cluster),
                )
            )
    return levels


@dataclass(frozen=True)
class SweepEvent:
    side: Side  # bullish = swept low then reclaimed; bearish = swept high
    level_price: float
    sweep_index: int  # bar that pierced
    reclaim_index: int  # bar that closed back
    distance: float
    level_kind: Literal["swing", "equal", "session", "pdh", "pdl", "pwh", "pwl"]


def detect_sweep_reclaim_at_bar(
    candles: list[Candle],
    bar_index: int,
    level_price: float,
    *,
    side: Side,
    min_distance: float = 0.0,
    max_distance: float = 1e9,
    reclaim_required: bool = True,
) -> SweepEvent | None:
    """Detect sweep+reclaim using only bars <= bar_index (bar_index closed)."""
    if bar_index < 1 or bar_index >= len(candles):
        return None
    c = candles[bar_index]
    if side == "bullish":
        # pierce below level earlier in window, reclaim close above on this bar
        pierced = c.low < level_price - min_distance
        # look back a few bars for pierce if reclaim is today
        pierce_i = None
        for j in range(max(0, bar_index - 5), bar_index + 1):
            if candles[j].low <= level_price - min_distance:
                dist = level_price - candles[j].low
                if min_distance <= dist <= max_distance:
                    pierce_i = j
        if pierce_i is None:
            return None
        if reclaim_required and not (c.close > level_price):
            return None
        if not reclaim_required and not pierced:
            return None
        dist = level_price - candles[pierce_i].low
        return SweepEvent(
            "bullish", level_price, pierce_i, bar_index, dist, "swing"
        )
    # bearish
    pierce_i = None
    for j in range(max(0, bar_index - 5), bar_index + 1):
        if candles[j].high >= level_price + min_distance:
            dist = candles[j].high - level_price
            if min_distance <= dist <= max_distance:
                pierce_i = j
    if pierce_i is None:
        return None
    if reclaim_required and not (c.close < level_price):
        return None
    dist = candles[pierce_i].high - level_price
    return SweepEvent(
        "bearish", level_price, pierce_i, bar_index, dist, "swing"
    )


@dataclass(frozen=True)
class Displacement:
    side: Side
    bar_index: int
    body_atr: float
    range_atr: float
    close_loc: float  # 0=low .. 1=high


def measure_displacement(
    candles: list[Candle],
    bar_index: int,
    *,
    atr_period: int = 14,
) -> Displacement | None:
    if bar_index < 1 or bar_index >= len(candles):
        return None
    c = candles[bar_index]
    a = atr(candles, bar_index, atr_period)
    if a <= 0:
        return None
    body = abs(c.close - c.open)
    rng = max(1e-12, c.high - c.low)
    close_loc = (c.close - c.low) / rng
    side: Side = "bullish" if c.close >= c.open else "bearish"
    return Displacement(side, bar_index, body / a, rng / a, close_loc)


@dataclass(frozen=True)
class FairValueGap:
    side: Side
    index: int  # middle candle index; usable at index+1 (3rd candle) == usable_index
    usable_index: int
    top: float
    bottom: float
    width: float
    width_atr: float


def detect_fvg_at(
    candles: list[Candle],
    bar_index: int,
    *,
    atr_period: int = 14,
) -> FairValueGap | None:
    """3-candle FVG; bar_index is the 3rd (confirming) closed candle."""
    if bar_index < 2:
        return None
    c1, c2, c3 = candles[bar_index - 2], candles[bar_index - 1], candles[bar_index]
    a = atr(candles, bar_index, atr_period) or 1e-12
    # bullish: c3.low > c1.high
    if c3.low > c1.high:
        top, bottom = c3.low, c1.high
        return FairValueGap(
            "bullish",
            bar_index - 1,
            bar_index,
            top,
            bottom,
            top - bottom,
            (top - bottom) / a,
        )
    if c3.high < c1.low:
        top, bottom = c1.low, c3.high
        return FairValueGap(
            "bearish",
            bar_index - 1,
            bar_index,
            top,
            bottom,
            top - bottom,
            (top - bottom) / a,
        )
    return None


def fvg_mitigation_pct(
    candles: list[Candle],
    fvg: FairValueGap,
    bar_index: int,
) -> float:
    """0 = untouched, 1 = fully filled, using highs/lows after usable_index through bar_index."""
    if bar_index < fvg.usable_index:
        return 0.0
    width = max(1e-12, fvg.width)
    if fvg.side == "bullish":
        # fill from top down
        lowest = min(candles[i].low for i in range(fvg.usable_index, bar_index + 1))
        filled = max(0.0, fvg.top - lowest)
        return min(1.0, filled / width)
    highest = max(candles[i].high for i in range(fvg.usable_index, bar_index + 1))
    filled = max(0.0, highest - fvg.bottom)
    return min(1.0, filled / width)


@dataclass(frozen=True)
class StructureShift:
    side: Side
    bar_index: int
    broken_swing_price: float
    broken_swing_index: int


def detect_structure_shift(
    candles: list[Candle],
    swings: list[CausalSwing],
    bar_index: int,
    *,
    side: Side,
) -> StructureShift | None:
    usable = swings_usable_at(swings, bar_index)
    c = candles[bar_index]
    if side == "bullish":
        # break above recent swing high
        highs = [s for s in usable if s.kind == "high"]
        if not highs:
            return None
        last = max(highs, key=lambda s: s.usable_index)
        if c.close > last.price:
            return StructureShift("bullish", bar_index, last.price, last.pivot_index)
    else:
        lows = [s for s in usable if s.kind == "low"]
        if not lows:
            return None
        last = max(lows, key=lambda s: s.usable_index)
        if c.close < last.price:
            return StructureShift("bearish", bar_index, last.price, last.pivot_index)
    return None


@dataclass(frozen=True)
class DealingRange:
    high: float
    low: float
    mid: float
    position: float  # 0 at low .. 1 at high
    lookback: int


def dealing_range_at(
    candles: list[Candle],
    bar_index: int,
    *,
    lookback: int = 48,
) -> DealingRange | None:
    if bar_index < 1 or lookback < 2:
        return None
    start = max(0, bar_index - lookback + 1)
    window = candles[start : bar_index + 1]
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    if hi <= lo:
        return None
    mid = 0.5 * (hi + lo)
    pos = (candles[bar_index].close - lo) / (hi - lo)
    return DealingRange(hi, lo, mid, pos, lookback)


@dataclass
class PeriodLevelTracker:
    """Causal PDH/PDL/PWH/PWL — previous period only after period completes."""

    pdh: float | None = None
    pdl: float | None = None
    pwh: float | None = None
    pwl: float | None = None
    _cur_day: object | None = field(default=None, repr=False)
    _day_hi: float | None = field(default=None, repr=False)
    _day_lo: float | None = field(default=None, repr=False)
    _cur_week: object | None = field(default=None, repr=False)
    _week_hi: float | None = field(default=None, repr=False)
    _week_lo: float | None = field(default=None, repr=False)

    def update(self, candle: Candle) -> None:
        t = _ts(candle)
        day = t.date()
        iso = t.isocalendar()
        week = (int(iso.year), int(iso.week))
        if self._cur_day is None:
            self._cur_day = day
            self._day_hi, self._day_lo = candle.high, candle.low
        elif day != self._cur_day:
            # prior day completes → becomes PDH/PDL
            self.pdh, self.pdl = self._day_hi, self._day_lo
            self._cur_day = day
            self._day_hi, self._day_lo = candle.high, candle.low
        else:
            self._day_hi = max(self._day_hi or candle.high, candle.high)
            self._day_lo = min(self._day_lo or candle.low, candle.low)

        if self._cur_week is None:
            self._cur_week = week
            self._week_hi, self._week_lo = candle.high, candle.low
        elif week != self._cur_week:
            self.pwh, self.pwl = self._week_hi, self._week_lo
            self._cur_week = week
            self._week_hi, self._week_lo = candle.high, candle.low
        else:
            self._week_hi = max(self._week_hi or candle.high, candle.high)
            self._week_lo = min(self._week_lo or candle.low, candle.low)


@dataclass
class SessionLevelTracker:
    """Running session highs/lows; completed-session snapshot on session change."""

    asia_high: float | None = None
    asia_low: float | None = None
    london_high: float | None = None
    london_low: float | None = None
    newyork_high: float | None = None
    newyork_low: float | None = None
    # completed prior session snapshots
    prior_asia_high: float | None = None
    prior_asia_low: float | None = None
    prior_london_high: float | None = None
    prior_london_low: float | None = None
    prior_newyork_high: float | None = None
    prior_newyork_low: float | None = None
    _last_sess: TradingSession | None = field(default=None, repr=False)
    _accum_hi: float | None = field(default=None, repr=False)
    _accum_lo: float | None = field(default=None, repr=False)

    def update(self, candle: Candle) -> None:
        sess = classify_session(candle.timestamp)
        if sess is None:
            return
        # map overlap into newyork for level tracking simplicity
        key = sess
        if key == TradingSession.OVERLAP:
            key = TradingSession.NEWYORK
        if self._last_sess is not None and key != self._last_sess:
            self._finalize(self._last_sess)
            self._accum_hi = candle.high
            self._accum_lo = candle.low
        else:
            self._accum_hi = max(self._accum_hi or candle.high, candle.high)
            self._accum_lo = min(self._accum_lo or candle.low, candle.low)
        self._last_sess = key
        self._set_running(key, self._accum_hi, self._accum_lo)

    def _set_running(self, key: TradingSession, hi: float, lo: float) -> None:
        if key == TradingSession.ASIA:
            self.asia_high, self.asia_low = hi, lo
        elif key == TradingSession.LONDON:
            self.london_high, self.london_low = hi, lo
        elif key == TradingSession.NEWYORK:
            self.newyork_high, self.newyork_low = hi, lo

    def _finalize(self, key: TradingSession) -> None:
        if self._accum_hi is None or self._accum_lo is None:
            return
        if key == TradingSession.ASIA:
            self.prior_asia_high, self.prior_asia_low = self._accum_hi, self._accum_lo
        elif key == TradingSession.LONDON:
            self.prior_london_high, self.prior_london_low = self._accum_hi, self._accum_lo
        elif key == TradingSession.NEWYORK:
            self.prior_newyork_high, self.prior_newyork_low = self._accum_hi, self._accum_lo


@dataclass(frozen=True)
class LiquiditySnapshot:
    """Causal feature snapshot at a closed bar index."""

    bar_index: int
    atr: float
    swings_high: tuple[CausalSwing, ...]
    swings_low: tuple[CausalSwing, ...]
    equal_highs: tuple[EqualLevel, ...]
    equal_lows: tuple[EqualLevel, ...]
    displacement: Displacement | None
    fvg: FairValueGap | None
    structure_bull: StructureShift | None
    structure_bear: StructureShift | None
    dealing: DealingRange | None
    pdh: float | None
    pdl: float | None
    pwh: float | None
    pwl: float | None
    prior_asia_high: float | None
    prior_asia_low: float | None
    prior_london_high: float | None
    prior_london_low: float | None
    prior_newyork_high: float | None
    prior_newyork_low: float | None


def build_snapshots(
    candles: list[Candle],
    *,
    swing_left: int = 3,
    swing_right: int = 3,
    equal_tol_atr: float = 0.15,
    min_equal_touches: int = 2,
    max_level_age: int = 500,
    atr_period: int = 14,
    dealing_lookback: int = 48,
) -> list[LiquiditySnapshot | None]:
    """Precompute causal snapshots for every bar index (None until warm)."""
    swings = detect_causal_swings(candles, left=swing_left, right=swing_right)
    period = PeriodLevelTracker()
    sessions = SessionLevelTracker()
    out: list[LiquiditySnapshot | None] = [None] * len(candles)
    for i, c in enumerate(candles):
        period.update(c)
        sessions.update(c)
        if i < swing_left + swing_right:
            continue
        a = atr(candles, i, atr_period)
        tol = (equal_tol_atr * a) if a > 0 else 0.0
        usable = swings_usable_at(swings, i)
        eq = find_equal_levels(
            swings,
            bar_index=i,
            tolerance=tol,
            min_touches=min_equal_touches,
            max_age_bars=max_level_age,
        )
        eq_h = tuple(e for e in eq if e.kind == "high")
        eq_l = tuple(e for e in eq if e.kind == "low")
        out[i] = LiquiditySnapshot(
            bar_index=i,
            atr=a,
            swings_high=tuple(s for s in usable if s.kind == "high"),
            swings_low=tuple(s for s in usable if s.kind == "low"),
            equal_highs=eq_h,
            equal_lows=eq_l,
            displacement=measure_displacement(candles, i, atr_period=atr_period),
            fvg=detect_fvg_at(candles, i, atr_period=atr_period),
            structure_bull=detect_structure_shift(
                candles, swings, i, side="bullish"
            ),
            structure_bear=detect_structure_shift(
                candles, swings, i, side="bearish"
            ),
            dealing=dealing_range_at(candles, i, lookback=dealing_lookback),
            pdh=period.pdh,
            pdl=period.pdl,
            pwh=period.pwh,
            pwl=period.pwl,
            prior_asia_high=sessions.prior_asia_high,
            prior_asia_low=sessions.prior_asia_low,
            prior_london_high=sessions.prior_london_high,
            prior_london_low=sessions.prior_london_low,
            prior_newyork_high=sessions.prior_newyork_high,
            prior_newyork_low=sessions.prior_newyork_low,
        )
    return out
