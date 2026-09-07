"""AlexG5 Revised — Set-and-Forget rules from video 1, with ablation toggles."""

from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.ablation import AblationConfig, video1_default
from borex.alexg.aoi_setforget import (
    PipAOI,
    _pip_size,
    aoi_at_close,
    build_pip_aoi_zones,
    stop_beyond_aoi,
)
from borex.alexg.aoi2 import next_aoi_target, stops_from_aoi_tp
from borex.alexg.aoi2 import AOIZone
from borex.alexg.ghost_entry import GhostSLEntryMixin, PendingSetup
from borex.alexg.sessions import TradingSession, in_session
from borex.alexg.structure_trend import structure_trend_at
from borex.alexg.trend import Trend
from borex.data.loader import resample_candles
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal, SignalAction
from borex.patterns.candlestick import (
    avg_body,
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_hammer,
    is_shooting_star,
    is_three_black_crows,
    is_three_white_soldiers,
)
from borex.strategy.base import Strategy

# Approx bars for AOI validity windows when building from resampled HTF.
_DAILY_MAX_AGE = 365 * 2  # ~2 years of daily bars
_WEEKLY_MAX_AGE = 52 * 5  # ~5 years of weekly bars


def _pattern_name(candles: list[Candle], index: int, action: SignalAction) -> str | None:
    if index < 2:
        return None
    curr = candles[index]
    prev = candles[index - 1]
    avg = avg_body(candles[: index + 1])
    c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]

    if action == SignalAction.BUY:
        if is_bullish_engulfing(prev, curr):
            return "bullish_engulfing"
        if is_hammer(curr, avg):
            return "hammer"
        if is_three_white_soldiers(c1, c2, c3, avg):
            return "three_white_soldiers"
        return None

    if is_bearish_engulfing(prev, curr):
        return "bearish_engulfing"
    if is_shooting_star(curr, avg):
        return "shooting_star"
    if is_three_black_crows(c1, c2, c3, avg):
        return "three_black_crows"
    return None


def _pip_zones_as_aoi_zones(zones: list[PipAOI]) -> list[AOIZone]:
    out: list[AOIZone] = []
    for z in zones:
        out.append(
            AOIZone(
                level=z.level,
                kind=z.kind,
                touches=z.touches,
                swing_indices=[z.first_touch_index, z.last_touch_index],
                last_touch_index=z.last_touch_index,
            )
        )
    return out


def _htf_trend(candles: list[Candle], lookback: int = 3) -> Trend:
    if len(candles) < lookback * 2 + 5:
        return Trend.NEUTRAL
    return structure_trend_at(candles, len(candles) - 1, lookback=lookback)


@dataclass
class AlexG5RevisedStrategy(GhostSLEntryMixin, Strategy):
    """
    Set-and-Forget inspired strategy (video 1 rules), configurable for ablation.

    Default = video1_default(): HTF pair bias, chart trend, patterns, ghost
    entry, all sessions.
    A setup fires when price closes inside an AOI. SL is 5–7 pips beyond the
    AOI; TP is nearest opposing structure with min_rr.
    With the ghost pill on (video 1), the setup is queued and filled only if
    price comes back to tag its planned SL; with it off, entry is immediate at
    the AOI close.
    """

    name: str = "alexg5revised"
    min_rr: float = 2.0
    swing_lookback: int = 5
    min_aoi_touches: int = 3
    min_aoi_pips: float = 5.0
    max_aoi_pips: float = 60.0
    cluster_pips: float = 5.0
    # Expand AOI membership by this many pips when testing close-in-zone.
    aoi_pad_pips: float = 0.0
    # If >0, snap OHLC to this pip grid before zone/signal logic (feed align).
    ohlc_quantize_pips: float = 0.0
    # Blend this feed's OHLC toward an attached peer series (0=off, 1=replace).
    # Used by alexg7aligned to sync broker vs Dukascopy decisions.
    peer_blend: float = 0.0
    sl_buffer_pips: float = 6.0
    min_bars: int = 120
    signal_cooldown: int = 8
    # Present so Nautilus multi-market signal engine can build context
    # (revised ignores currency-strength filtering).
    strength_lookback: int = 24
    min_currency_edge: float = 0.0
    min_confirming_pairs: int = 0
    ablation: AblationConfig = field(default_factory=video1_default)
    execution_interval: str = "1h"
    # Rebuild expensive HTF/AOI work every N execution bars (1h → ~daily).
    zone_refresh_bars: int = 24

    _last_signal_index: dict[str, int] = field(default_factory=dict, repr=False)
    _current_symbol: str = field(default="", repr=False)
    # symbol -> (action, zone_level, armed_index, left_zone)
    _pending_retest: dict[str, tuple[SignalAction, float, int, bool]] = field(
        default_factory=dict, repr=False
    )
    _zones_cache: list = field(default_factory=list, repr=False)
    _zones_cache_index: int = field(default=-10**9, repr=False)
    _htf_cache: dict = field(default_factory=dict, repr=False)
    _htf_cache_index: int = field(default=-10**9, repr=False)
    _quant_id: int = field(default=0, repr=False)
    _quant_candles: list | None = field(default=None, repr=False)
    _peer_by_hour: dict = field(default_factory=dict, repr=False)

    def set_context(self, symbol: str, market_ctx=None) -> None:
        self._current_symbol = symbol

    def attach_peer_series(self, peer: list[Candle] | None) -> None:
        """Hour-indexed peer OHLC for peer_blend (clears quantize cache)."""
        self._peer_by_hour = {}
        self._quant_id = 0
        self._quant_candles = None
        if not peer:
            return
        for c in peer:
            t = c.timestamp
            try:
                import pandas as pd

                ts = pd.Timestamp(t)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                else:
                    ts = ts.tz_convert("UTC")
                key = ts.floor("h")
            except Exception:
                key = t
            self._peer_by_hour[key] = c

    def _quantized(self, candles: list[Candle]) -> list[Candle]:
        q = float(self.ohlc_quantize_pips or 0.0)
        blend = float(self.peer_blend or 0.0)
        if q <= 0 and blend <= 0:
            return candles
        cid = id(candles)
        if self._quant_candles is not None and self._quant_id == cid:
            return self._quant_candles
        symbol = self._current_symbol or "EURUSD=X"
        step = q * _pip_size(symbol) if q > 0 else 0.0

        def snap(x: float) -> float:
            if step <= 0:
                return x
            return round(x / step) * step

        out: list[Candle] = []
        for c in candles:
            o, h, l, cl = c.open, c.high, c.low, c.close
            if blend > 0 and self._peer_by_hour:
                try:
                    import pandas as pd

                    ts = pd.Timestamp(c.timestamp)
                    if ts.tzinfo is None:
                        ts = ts.tz_localize("UTC")
                    else:
                        ts = ts.tz_convert("UTC")
                    peer = self._peer_by_hour.get(ts.floor("h"))
                except Exception:
                    peer = None
                if peer is not None:
                    b = min(1.0, max(0.0, blend))
                    o = (1 - b) * o + b * peer.open
                    h = (1 - b) * h + b * peer.high
                    l = (1 - b) * l + b * peer.low
                    cl = (1 - b) * cl + b * peer.close
            o, h, l, cl = snap(o), snap(h), snap(l), snap(cl)
            out.append(
                Candle(
                    timestamp=c.timestamp,
                    open=o,
                    high=max(o, h, l, cl),
                    low=min(o, h, l, cl),
                    close=cl,
                    volume=c.volume,
                )
            )
        self._quant_id = cid
        self._quant_candles = out
        return out

    def apply_ablation(self, config: AblationConfig) -> None:
        self.ablation = config

    def _build_zones(self, candles: list[Candle], index: int) -> list[PipAOI]:
        refresh = max(1, self.zone_refresh_bars)
        if (
            self._zones_cache
            and index - self._zones_cache_index < refresh
            and index >= self._zones_cache_index
        ):
            return self._zones_cache

        symbol = self._current_symbol or "EURUSD=X"
        window = candles[: index + 1]
        zones: list[PipAOI] = []

        daily = resample_candles(window, "1d") if self.execution_interval != "1d" else window
        if daily:
            zones.extend(
                build_pip_aoi_zones(
                    daily,
                    symbol=symbol,
                    min_touches=self.min_aoi_touches,
                    min_pips=self.min_aoi_pips,
                    max_pips=self.max_aoi_pips,
                    max_age_bars=_DAILY_MAX_AGE,
                    source_tf="daily",
                    cluster_pips=self.cluster_pips,
                )
            )

        weekly = resample_candles(window, "1wk") if self.execution_interval != "1wk" else window
        if weekly:
            zones.extend(
                build_pip_aoi_zones(
                    weekly,
                    symbol=symbol,
                    min_touches=self.min_aoi_touches,
                    min_pips=self.min_aoi_pips,
                    max_pips=self.max_aoi_pips,
                    max_age_bars=_WEEKLY_MAX_AGE,
                    source_tf="weekly",
                    cluster_pips=self.cluster_pips,
                )
            )
        self._zones_cache = zones
        self._zones_cache_index = index
        return zones

    def _htf_frames(self, candles: list[Candle], index: int) -> dict[str, list[Candle]]:
        refresh = max(1, self.zone_refresh_bars)
        if (
            self._htf_cache
            and index - self._htf_cache_index < refresh
            and index >= self._htf_cache_index
        ):
            return self._htf_cache
        window = candles[: index + 1]
        frames = {
            "1d": resample_candles(window, "1d"),
            "1wk": resample_candles(window, "1wk"),
            "4h": resample_candles(window, "4h"),
        }
        self._htf_cache = frames
        self._htf_cache_index = index
        return frames

    def _htf_bias_ok(self, candles: list[Candle], index: int, action: SignalAction) -> bool:
        bias = self.ablation.htf_bias
        if bias == "off":
            return True

        frames = self._htf_frames(candles, index)
        daily = frames.get("1d") or []
        weekly = frames.get("1wk") or []
        h4 = frames.get("4h") or []

        d_trend = _htf_trend(daily) if daily else Trend.NEUTRAL
        w_trend = _htf_trend(weekly) if weekly else Trend.NEUTRAL
        h4_trend = _htf_trend(h4) if h4 else Trend.NEUTRAL
        want = Trend.BULLISH if action == SignalAction.BUY else Trend.BEARISH

        if bias == "weekly":
            return w_trend == want
        if bias == "daily":
            return d_trend == want
        if bias == "4h":
            return h4_trend == want
        # pair: weekly+daily OR daily+4h
        wd = w_trend == want and d_trend == want
        dh = d_trend == want and h4_trend == want
        return wd or dh

    def _chart_trend_ok(self, candles: list[Candle], index: int, action: SignalAction) -> bool:
        if not self.ablation.require_chart_trend:
            return True
        # Cap lookback window so structure scan stays tractable on long series.
        start = max(0, index - 1500)
        window = candles[start : index + 1]
        trend = structure_trend_at(window, len(window) - 1, lookback=self.swing_lookback)
        if action == SignalAction.BUY:
            return trend == Trend.BULLISH
        return trend == Trend.BEARISH

    def _session_ok(self, candle: Candle) -> bool:
        return in_session(candle.timestamp, TradingSession(self.ablation.session))

    def _retest_ok(
        self,
        symbol: str,
        index: int,
        candle: Candle,
        zone: PipAOI,
        action: SignalAction,
    ) -> bool:
        if not self.ablation.require_retest:
            return True

        pending = self._pending_retest.get(symbol)
        inside = zone.contains_close(candle)

        if pending is None:
            if inside:
                self._pending_retest[symbol] = (action, zone.level, index, False)
            return False

        pend_action, pend_level, armed_at, left = pending
        same_zone = pend_action == action and abs(pend_level - zone.level) <= max(
            zone.width, 1e-9
        )
        if not same_zone:
            if inside:
                self._pending_retest[symbol] = (action, zone.level, index, False)
            else:
                self._pending_retest.pop(symbol, None)
            return False

        if not left and not inside and index > armed_at:
            self._pending_retest[symbol] = (action, zone.level, armed_at, True)
            return False

        if left and inside and index > armed_at + 1:
            del self._pending_retest[symbol]
            return True

        return False

    def _direction_from_aoi(self, zone: PipAOI) -> SignalAction | None:
        if zone.kind == "support":
            return SignalAction.BUY
        if zone.kind == "resistance":
            return SignalAction.SELL
        return None

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        candles = self._quantized(candles)
        symbol = self._current_symbol or "UNKNOWN"
        if index < self.min_bars:
            return None

        # Resolve a waiting ghost before looking for a new setup. A queued
        # ghost is a resting limit order, so no session/filter re-check here.
        pending = self._pending.get(symbol)
        if pending is not None:
            outcome = self._pending_outcome(pending, index, candles)
            if outcome == "triggered":
                if not self._confirm_ghost_fill(pending, index, candles):
                    # Subclass rejected the fill (e.g. alexg8 wrong-way LTF).
                    del self._pending[symbol]
                    return None
                del self._pending[symbol]
                self._last_signal_index[symbol] = index
                return self._entry_signal(pending, index, candles)
            if outcome in ("expired", "invalidated"):
                del self._pending[symbol]
            else:
                return None

        last = self._last_signal_index.get(symbol, -999)
        if index - last < self.signal_cooldown:
            return None

        candle = candles[index]
        if not self._session_ok(candle):
            return None

        zones = self._build_zones(candles, index)
        if not zones:
            return None

        zone = aoi_at_close(
            candle,
            zones,
            pad=self.aoi_pad_pips
            * _pip_size(symbol if symbol != "UNKNOWN" else "EURUSD=X"),
        )
        if zone is None:
            return None

        action = self._direction_from_aoi(zone)
        if action is None:
            return None

        if not self._htf_bias_ok(candles, index, action):
            return None
        if not self._chart_trend_ok(candles, index, action):
            return None

        pattern = "close_in_aoi"
        if self.ablation.require_pattern:
            named = _pattern_name(candles, index, action)
            if named is None:
                return None
            pattern = named

        if not self._retest_ok(symbol, index, candle, zone, action):
            return None

        entry = candle.close
        sl = stop_beyond_aoi(
            zone,
            "buy" if action == SignalAction.BUY else "sell",
            symbol=symbol if symbol != "UNKNOWN" else "EURUSD=X",
            buffer_pips=self.sl_buffer_pips,
        )
        aoi_zones = _pip_zones_as_aoi_zones(zones)
        tp_level = next_aoi_target(entry, aoi_zones, action)
        if tp_level is None:
            # Fallback: fixed RR from SL distance.
            risk = abs(entry - sl)
            if risk <= 0:
                return None
            if action == SignalAction.BUY:
                tp_level = entry + risk * self.min_rr
            else:
                tp_level = entry - risk * self.min_rr

        stops = stops_from_aoi_tp(entry, tp_level, action, sl, self.min_rr)
        if stops is None:
            return None
        sl, tp = stops

        self._last_signal_index[symbol] = index
        tag = self.ablation.label()
        setup_pattern = (
            f"{self.name}|{symbol}|{zone.kind}|{zone.source_tf}|{pattern}|{tag}"
        )

        if self.ablation.require_ghost_sl_entry:
            ghost_sl = self._scaled_ghost_sl(entry, sl, action)
            self._pending[symbol] = PendingSetup(
                action=action,
                pattern=setup_pattern,
                stop_loss=ghost_sl,
                take_profit=tp,
                planned_entry=entry,
                created_index=index,
                expires_index=index + self.sl_wait_max_bars,
            )
            return None

        return Signal(
            action=action,
            pattern=setup_pattern,
            index=index,
            price=entry,
            timestamp=candle.timestamp,
            stop_loss=sl,
            take_profit=tp,
            score=self.min_rr,
        )
