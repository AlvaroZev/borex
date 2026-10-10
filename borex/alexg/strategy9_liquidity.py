"""AlexG9 Liquidity — research-only strategy families (DISCOVERY_ONLY).

Distinct from the thin live ``AlexG9Strategy`` (alexg8 geometry wrapper).
This module implements objective OHLC liquidity pattern entries.

Does NOT claim institutional intent. No lookahead in feature usability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from borex.alexg.liquidity.features import (
    build_snapshots,
    detect_sweep_reclaim_at_bar,
)
from borex.alexg.sessions import in_session
from borex.models.candle import Candle, Signal, SignalAction
from borex.strategy.base import Strategy

G9Family = Literal[
    "A_swing_sweep",
    "B_equal_sweep",
    "C_sweep_displacement",
    "D_sweep_structure",
    "E_sweep_fvg",
    "F_sweep_disp_fvg",
    "G_session_sweep",
    "H_pdh_pdl_sweep",
    "I_sweep_premium_discount",
    "J_structure_fvg",
    "K_continuation",
]


@dataclass
class AlexG9LiquidityStrategy(Strategy):
    """Standalone liquidity-pattern strategy for Gate 2 discovery."""

    name: str = "alexg9liq"
    family: G9Family = "A_swing_sweep"
    min_rr: float = 2.0
    min_bars: int = 120
    signal_cooldown: int = 8
    ghost_sl_mult: float = 4.0
    # MultiMarketEngine expects these attributes on strategies
    strength_lookback: int = 24
    min_currency_edge: float = 0.0
    min_confirming_pairs: int = 0
    # feature knobs
    swing_left: int = 3
    swing_right: int = 3
    equal_tol_atr: float = 0.15
    min_equal_touches: int = 2
    max_level_age: int = 200
    sweep_min_atr: float = 0.05
    sweep_max_atr: float = 2.0
    reclaim_required: bool = True
    max_bars_after_sweep: int = 5
    disp_body_atr_min: float = 0.8
    fvg_min_width_atr: float = 0.1
    dealing_lookback: int = 48
    require_discount_for_long: bool = True
    require_premium_for_short: bool = True
    continuation_mode: bool = False  # K family / optional
    session_filter: str = "all"
    sides: str = "both"  # both|long_only|short_only
    sl_atr_mult: float = 1.0
    tp_rr: float = 2.0

    _last_signal_index: dict[str, int] = field(default_factory=dict, repr=False)
    _current_symbol: str = field(default="", repr=False)
    _snap_cache_id: int = field(default=0, repr=False)
    _snaps: list = field(default_factory=list, repr=False)

    def set_context(self, symbol: str, market_ctx=None) -> None:
        self._current_symbol = symbol

    def _snaps_for(self, candles: list[Candle]):
        # Cache by symbol + length + endpoints (list id changes every engine call)
        sym = self._current_symbol or ""
        key = (
            sym,
            len(candles),
            str(getattr(candles[0], "timestamp", None)) if candles else None,
            str(getattr(candles[-1], "timestamp", None)) if candles else None,
        )
        if self._snaps and getattr(self, "_snap_cache_key", None) == key:
            return self._snaps
        self._snaps = build_snapshots(
            candles,
            swing_left=self.swing_left,
            swing_right=self.swing_right,
            equal_tol_atr=self.equal_tol_atr,
            min_equal_touches=self.min_equal_touches,
            max_level_age=self.max_level_age,
            dealing_lookback=self.dealing_lookback,
        )
        self._snap_cache_key = key
        self._snap_cache_id = id(candles)
        return self._snaps

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf=None,
    ) -> Signal | None:
        if index < self.min_bars or index >= len(candles):
            return None
        candle = candles[index]
        sym = self._current_symbol or "EURUSD=X"
        last = self._last_signal_index.get(sym, -10**9)
        if index - last < self.signal_cooldown:
            return None
        if not in_session(candle.timestamp, self.session_filter):
            return None

        snaps = self._snaps_for(candles)
        snap = snaps[index] if index < len(snaps) else None
        if snap is None or snap.atr <= 0:
            return None

        action = self._family_signal(candles, index, snap)
        if action is None:
            return None
        if self.sides == "long_only" and action != SignalAction.BUY:
            return None
        if self.sides == "short_only" and action != SignalAction.SELL:
            return None

        sl_dist = self.sl_atr_mult * snap.atr
        if action == SignalAction.BUY:
            entry = float(candle.close)
            sl = entry - sl_dist
            tp = entry + sl_dist * self.tp_rr
        else:
            entry = float(candle.close)
            sl = entry + sl_dist
            tp = entry - sl_dist * self.tp_rr

        self._last_signal_index[sym] = index
        return Signal(
            action=action,
            pattern=f"alexg9liq|{self.family}|{action.value}",
            index=index,
            price=entry,
            timestamp=candle.timestamp,
            stop_loss=sl,
            take_profit=tp,
            score=1.0,
        )

    def _family_signal(self, candles, index, snap) -> SignalAction | None:
        a = snap.atr
        min_d = self.sweep_min_atr * a
        max_d = self.sweep_max_atr * a
        fam = self.family

        def try_levels(levels: list[tuple[float, str]], side_pref: str | None = None):
            for price, tag in levels:
                for side, act in (("bullish", SignalAction.BUY), ("bearish", SignalAction.SELL)):
                    if side_pref and side != side_pref:
                        continue
                    ev = detect_sweep_reclaim_at_bar(
                        candles,
                        index,
                        price,
                        side=side,
                        min_distance=min_d,
                        max_distance=max_d,
                        reclaim_required=self.reclaim_required,
                    )
                    if ev is None:
                        continue
                    if index - ev.sweep_index > self.max_bars_after_sweep:
                        continue
                    return act, ev, tag
            return None

        # Collect candidate levels by family
        levels: list[tuple[float, str]] = []
        if fam in {
            "A_swing_sweep",
            "C_sweep_displacement",
            "D_sweep_structure",
            "E_sweep_fvg",
            "F_sweep_disp_fvg",
            "I_sweep_premium_discount",
            "K_continuation",
        }:
            if snap.swings_low:
                levels.append((snap.swings_low[-1].price, "swing_low"))
            if snap.swings_high:
                levels.append((snap.swings_high[-1].price, "swing_high"))
        if fam == "B_equal_sweep":
            for e in snap.equal_lows:
                levels.append((e.price, "eq_low"))
            for e in snap.equal_highs:
                levels.append((e.price, "eq_high"))
        if fam == "G_session_sweep":
            for p, tag in (
                (snap.prior_asia_low, "asia_low"),
                (snap.prior_asia_high, "asia_high"),
                (snap.prior_london_low, "lon_low"),
                (snap.prior_london_high, "lon_high"),
                (snap.prior_newyork_low, "ny_low"),
                (snap.prior_newyork_high, "ny_high"),
            ):
                if p is not None:
                    levels.append((p, tag))
        if fam == "H_pdh_pdl_sweep":
            for p, tag in (
                (snap.pdl, "pdl"),
                (snap.pdh, "pdh"),
                (snap.pwl, "pwl"),
                (snap.pwh, "pwh"),
            ):
                if p is not None:
                    levels.append((p, tag))

        if fam == "J_structure_fvg":
            # no sweep required
            if snap.structure_bull and snap.fvg and snap.fvg.side == "bullish":
                if snap.fvg.width_atr >= self.fvg_min_width_atr:
                    return SignalAction.BUY
            if snap.structure_bear and snap.fvg and snap.fvg.side == "bearish":
                if snap.fvg.width_atr >= self.fvg_min_width_atr:
                    return SignalAction.SELL
            return None

        hit = try_levels(levels)
        if hit is None:
            return None
        act, ev, _tag = hit

        # Continuation: flip interpretation
        if fam == "K_continuation" or self.continuation_mode:
            act = SignalAction.SELL if act == SignalAction.BUY else SignalAction.BUY

        if fam in {"C_sweep_displacement", "F_sweep_disp_fvg"}:
            d = snap.displacement
            if d is None or d.body_atr < self.disp_body_atr_min:
                return None
            if act == SignalAction.BUY and d.side != "bullish":
                return None
            if act == SignalAction.SELL and d.side != "bearish":
                return None

        if fam in {"D_sweep_structure"}:
            if act == SignalAction.BUY and snap.structure_bull is None:
                return None
            if act == SignalAction.SELL and snap.structure_bear is None:
                return None

        if fam in {"E_sweep_fvg", "F_sweep_disp_fvg"}:
            if snap.fvg is None or snap.fvg.width_atr < self.fvg_min_width_atr:
                return None
            if act == SignalAction.BUY and snap.fvg.side != "bullish":
                return None
            if act == SignalAction.SELL and snap.fvg.side != "bearish":
                return None

        if fam == "I_sweep_premium_discount":
            dr = snap.dealing
            if dr is None:
                return None
            if act == SignalAction.BUY and self.require_discount_for_long and dr.position > 0.5:
                return None
            if act == SignalAction.SELL and self.require_premium_for_short and dr.position < 0.5:
                return None

        return act
