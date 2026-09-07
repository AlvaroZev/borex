"""Unit tests for RR resolution and ablation grid."""

from __future__ import annotations

import pytest

from borex.alexg.ablation import (
    iter_ablation_grid,
    video1_default,
    video2_ghost,
    video2_winner,
)
from borex.alexg.ghost_entry import GhostSLEntryMixin, PendingSetup
from borex.alexg.sessions import TradingSession, in_session
from borex.alexg.structure_trend import StructureState, update_structure_trend
from borex.alexg.swings import SwingPoint
from borex.alexg.trend import Trend
from borex.backtest.engine import _confirmation_signal_from_pattern, _is_late_sl_entry
from borex.backtest.margin_stops import resolve_rr
from borex.models.candle import Candle, SignalAction
from datetime import datetime, timezone


def test_resolve_rr_fixed_default():
    assert resolve_rr(rr_mode="fixed", fixed_rr=3.0, winrate=0.5, rr_factor=1.0) == 3.0


def test_resolve_rr_fixed_with_factor():
    assert resolve_rr(rr_mode="fixed", fixed_rr=3.0, winrate=0.2, rr_factor=1.5) == 4.5


def test_resolve_rr_dynamic_from_winrate():
    # 25% WR → RR 4
    assert resolve_rr(rr_mode="dynamic", fixed_rr=3.0, winrate=0.25, rr_factor=1.0) == 4.0


def test_resolve_rr_dynamic_fallback_before_history():
    assert resolve_rr(rr_mode="dynamic", fixed_rr=3.0, winrate=None, rr_factor=1.0) == 3.0


def test_resolve_rr_dynamic_with_factor():
    assert resolve_rr(rr_mode="dynamic", fixed_rr=3.0, winrate=0.5, rr_factor=1.1) == pytest.approx(
        2.2
    )


def test_resolve_rr_rejects_bad_mode():
    with pytest.raises(ValueError):
        resolve_rr(rr_mode="weird", fixed_rr=3.0)


def test_resolve_rr_dynamic_clamps_min_max():
    # 10% WR → 10:1, capped at 6
    assert resolve_rr(
        rr_mode="dynamic",
        fixed_rr=2.0,
        winrate=0.10,
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=6.0,
    ) == 6.0
    # 80% WR → 1.25:1, floored at 2
    assert resolve_rr(
        rr_mode="dynamic",
        fixed_rr=2.0,
        winrate=0.80,
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=6.0,
    ) == 2.0
    # 25% WR → 4:1, inside the band
    assert resolve_rr(
        rr_mode="dynamic",
        fixed_rr=2.0,
        winrate=0.25,
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=6.0,
    ) == 4.0
    # 0 = no clamp (legacy)
    assert resolve_rr(
        rr_mode="dynamic",
        fixed_rr=2.0,
        winrate=0.10,
        rr_factor=1.0,
    ) == 10.0


def test_resolve_rr_ignores_tiny_winrate_sample():
    # 2 losses → WR=0 would otherwise keep default; 2 wins would collapse RR.
    assert resolve_rr(
        rr_mode="dynamic",
        fixed_rr=3.0,
        winrate=1.0,
        rr_factor=1.88,
        closed_trades=2,
        winrate_min_trades=20,
    ) == pytest.approx(5.64)
    assert resolve_rr(
        rr_mode="dynamic",
        fixed_rr=3.0,
        winrate=1.0,
        rr_factor=1.88,
        closed_trades=20,
        winrate_min_trades=20,
    ) == pytest.approx(1.88)


def test_alexg7aligned_ghost_fill_at_close():
    from borex.alexg import AlexG7AlignedStrategy, AlexG8Strategy

    assert AlexG7AlignedStrategy().ghost_fill_at_close is True
    assert AlexG8Strategy().ghost_fill_at_close is True
    from borex.alexg.strategy7 import AlexG7Strategy

    assert AlexG7Strategy().ghost_fill_at_close is False


def test_ablation_grid_is_400():
    grid = iter_ablation_grid()
    assert len(grid) == 400
    labels = {c.label() for c in grid}
    assert len(labels) == 400


def test_video_presets():
    v1 = video1_default()
    assert v1.htf_bias == "pair"
    assert v1.require_chart_trend is True
    assert v1.require_pattern is True
    assert v1.require_ghost_sl_entry is True
    v2 = video2_winner()
    assert v2.htf_bias == "off"
    assert v2.require_pattern is False
    assert v2.require_ghost_sl_entry is False
    assert v2.session == "overlap"
    v2g = video2_ghost()
    assert v2g.htf_bias == "off"
    assert v2g.require_chart_trend is False
    assert v2g.require_pattern is False
    assert v2g.require_ghost_sl_entry is True
    assert v2g.session == "overlap"
    assert "ghost1" in v2g.label()


def test_alexg7_defaults_to_video2_ghost():
    from borex.alexg import AlexG7Strategy

    s = AlexG7Strategy()
    assert s.name == "alexg7"
    assert s.ablation == video2_ghost()
    assert s.ablation.require_ghost_sl_entry is True


def test_alexg8_is_aligned_all_sessions():
    from borex.alexg import AlexG7AlignedStrategy, AlexG8Strategy
    from borex.alexg.ablation import video2_ghost_all_sessions

    s = AlexG8Strategy()
    assert s.name == "alexg8"
    assert s.ablation == video2_ghost_all_sessions()
    assert s.ablation.session == "all"
    assert s.ablation.require_ghost_sl_entry is True
    # Same geometry as alexg7aligned
    a = AlexG7AlignedStrategy()
    assert s.ghost_sl_mult == a.ghost_sl_mult
    assert s.aoi_pad_pips == a.aoi_pad_pips
    assert s.sl_buffer_pips == a.sl_buffer_pips
    assert a.ablation.session == "overlap"
    assert a.ghost_fill_at_close is True
    assert s.ghost_fill_at_close is True


def test_alexg9_extends_g8():
    from borex.alexg import AlexG8Strategy, AlexG9Strategy

    s = AlexG9Strategy()
    g8 = AlexG8Strategy()
    assert s.name == "alexg9"
    assert s.ablation.session == "all"
    assert s.ghost_sl_mult == g8.ghost_sl_mult


def test_force_flat_is_origin_session_not_fixed_utc():
    from types import SimpleNamespace
    from datetime import datetime, timezone

    from borex.alexg.force_flat import blocks_new_entries, force_flat_reason
    from borex.alexg.sessions import classify_session, session_close_hour
    from borex.backtest.engine import BacktestConfig

    cfg = BacktestConfig(force_flat_friday=True, force_flat_daily=True)
    thu_london_close = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
    thu_london_last = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    thu_ny_close = datetime(2026, 8, 20, 21, 0, tzinfo=timezone.utc)
    thu_asia_close = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    thu_mid = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    old_global = datetime(2026, 8, 20, 19, 0, tzinfo=timezone.utc)
    fri_ny_close = datetime(2026, 8, 21, 21, 0, tzinfo=timezone.utc)
    fri_london_close = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)

    london = SimpleNamespace(entry_session="london", entry_time=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc))
    ny = SimpleNamespace(entry_session="newyork", entry_time=datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc))
    asia = SimpleNamespace(entry_session="asia", entry_time=datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc))
    overlap = SimpleNamespace(entry_session="overlap", entry_time=datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc))

    assert classify_session(london.entry_time).value == "london"
    assert session_close_hour("london") == 16
    assert session_close_hour("newyork") == 21
    assert session_close_hour("asia") == 9
    assert session_close_hour("overlap") == 16

    # Last in-session hour still trades; flatten on the close-hour bar.
    assert force_flat_reason(thu_london_last, cfg, trade=london) is None
    assert force_flat_reason(thu_london_close, cfg, trade=london) == "daily_close"
    assert force_flat_reason(old_global, cfg, trade=london) is None
    assert force_flat_reason(thu_ny_close, cfg, trade=london) is None
    assert force_flat_reason(thu_ny_close, cfg, trade=ny) == "daily_close"
    assert force_flat_reason(thu_asia_close, cfg, trade=asia) == "daily_close"
    assert force_flat_reason(thu_london_close, cfg, trade=overlap) == "daily_close"
    assert force_flat_reason(thu_mid, cfg, trade=london) is None

    # Friday: London session-closes at 16:00; leftovers flatten at NY 21:00.
    assert force_flat_reason(fri_london_close, cfg, trade=london) == "daily_close"
    assert force_flat_reason(fri_ny_close, cfg, trade=ny) == "friday_close"
    assert force_flat_reason(fri_ny_close, cfg, trade=london) == "friday_close"

    assert blocks_new_entries(thu_london_close, cfg) is False  # NY still open
    assert blocks_new_entries(thu_london_last, cfg) is False
    assert blocks_new_entries(thu_ny_close, cfg) is True
    assert blocks_new_entries(old_global, cfg) is False
    assert blocks_new_entries(thu_mid, cfg) is False

    off = BacktestConfig(force_flat_friday=True, force_flat_daily=False)
    assert force_flat_reason(thu_london_close, off, trade=london) is None
    assert force_flat_reason(fri_ny_close, off, trade=ny) == "friday_close"


def test_commission_at_entry_charges_on_open():
    from borex.backtest.multi_portfolio import MultiMarketPortfolio
    from borex.backtest.costs import commission_for_margin

    portfolio = MultiMarketPortfolio(
        initial_capital=1000.0,
        position_size_pct=0.01,
        leverage=5000.0,
        size_mode="margin",
        commission_per_lot=7.0,
        risk_include_commission=False,
    )
    margin = portfolio.compute_margin(1.1)
    assert margin == pytest.approx(10.0, rel=1e-3)
    comm = commission_for_margin(margin, 5000.0, commission_per_lot=7.0)
    assert comm > 0
    portfolio.charge_commission(comm)
    assert portfolio.cash == pytest.approx(1000.0 - comm, rel=1e-3)


def test_session_overlap_utc():
    ts = datetime(2024, 3, 4, 13, 30, tzinfo=timezone.utc)
    assert in_session(ts, TradingSession.OVERLAP)
    assert in_session(ts, TradingSession.LONDON)
    assert in_session(ts, TradingSession.NEWYORK)
    assert not in_session(ts, TradingSession.ASIA)


def test_structure_trend_flip_on_hl_break():
    # Bootstrap bullish leg: low@1 then high@3
    swings = [
        SwingPoint(0, 1.0, "low"),
        SwingPoint(1, 3.0, "high"),
        SwingPoint(2, 2.0, "low"),  # inside — no change
        SwingPoint(3, 0.5, "low"),  # breaks HL → bearish
    ]
    state = StructureState()
    update_structure_trend(state, swings)
    assert state.trend == Trend.BEARISH
    assert state.leg is not None
    assert state.leg.anchor_low == 0.5


def test_structure_trend_bootstraps_past_same_kind_prefix():
    """Leading duplicate highs must not leave structure stuck at NEUTRAL."""
    swings = [
        SwingPoint(0, 3.0, "high"),
        SwingPoint(1, 3.0, "high"),
        SwingPoint(2, 1.0, "low"),
        SwingPoint(3, 2.5, "high"),  # continues after bearish bootstrap
    ]
    state = StructureState()
    update_structure_trend(state, swings)
    assert state.trend == Trend.BEARISH
    assert state.leg is not None
    assert state.leg.anchor_high == 3.0
    assert state.leg.anchor_low == 1.0


def _bar(low: float, high: float, close: float) -> Candle:
    return Candle(timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
                  open=(low + high) / 2, high=high, low=low, close=close)


def _long_ghost() -> PendingSetup:
    """Long setup planned at 1.1000 with SL 1.0900 and TP 1.1300."""
    return PendingSetup(
        action=SignalAction.BUY,
        pattern="alexg5revised|EURUSD=X|support|daily|hammer|bias=pair",
        stop_loss=1.0900,
        take_profit=1.1300,
        planned_entry=1.1000,
        created_index=10,
        expires_index=10 + 72,
    )


def test_ghost_fills_at_sl_and_keeps_rr_distances():
    host = GhostSLEntryMixin()
    host.min_rr = 3.0
    pending = _long_ghost()

    # Drifts sideways well above SL → still waiting.
    assert host._pending_outcome(pending, 11, {11: _bar(1.0980, 1.1020, 1.1000)}) == "waiting"
    # Tags the planned SL → fill.
    assert host._pending_outcome(pending, 12, {12: _bar(1.0890, 1.1000, 1.0950)}) == "triggered"

    signal = host._entry_signal(pending, 12, {12: _bar(1.0890, 1.1000, 1.0950)})
    assert signal.price == pytest.approx(1.0900)  # filled at the ghost SL
    # Same 100 pip risk / 300 pip reward, re-anchored from the fill.
    assert signal.stop_loss == pytest.approx(1.0800)
    assert signal.take_profit == pytest.approx(1.1200)
    assert _is_late_sl_entry(signal)


def test_ghost_invalidated_when_tp_hits_first():
    host = GhostSLEntryMixin()
    pending = _long_ghost()
    hit_tp = {5: _bar(1.1100, 1.1350, 1.1300)}
    assert host._pending_outcome(pending, 5, hit_tp) == "invalidated"


def test_ghost_invalidated_on_near_miss_then_leaving():
    host = GhostSLEntryMixin()
    pending = _long_ghost()
    # Dips into the near-SL band (SL..SL+25% of risk = 1.0900..1.0925) and
    # closes inside it, so the setup is still alive.
    assert host._pending_outcome(pending, 11, {11: _bar(1.0910, 1.0930, 1.0920)}) == "waiting"
    assert pending.saw_near_sl
    # Then closes back above the band → setup is gone.
    assert host._pending_outcome(pending, 12, {12: _bar(1.0950, 1.1050, 1.1040)}) == "invalidated"


def test_ghost_expires_after_wait_window():
    host = GhostSLEntryMixin()
    pending = _long_ghost()
    assert host._pending_outcome(pending, pending.expires_index + 1, {}) == "expired"


def test_ablation_labels_carry_ghost_pill():
    assert "ghost1" in video1_default().label()
    assert "ghost0" in video2_winner().label()


def test_confirmation_pattern_survives_ghost_suffix():
    base = "alexg5revised|EURUSD=X|support|daily|hammer|bias=pair|trend1"
    assert _confirmation_signal_from_pattern(base) == "hammer"
    assert _confirmation_signal_from_pattern(f"{base}|g:10:1.1:1.09:1.13") == "hammer"
    g8 = "alexg8|EURUSD=X|support|daily|close_in_aoi|ghost1"
    assert _confirmation_signal_from_pattern(f"{g8}|g:10:1.1:1.09:1.13|ltf_ok") == "close_in_aoi"


def test_scale_ghost_sl_buy_and_sell():
    from borex.alexg.ghost_entry import scale_ghost_sl

    # BUY: entry 1.10, structural SL 1.09 → dist 0.01
    assert scale_ghost_sl(1.10, 1.09, SignalAction.BUY, 1.0) == pytest.approx(1.09)
    assert scale_ghost_sl(1.10, 1.09, SignalAction.BUY, 0.5) == pytest.approx(1.095)
    assert scale_ghost_sl(1.10, 1.09, SignalAction.BUY, 2.0) == pytest.approx(1.08)
    # SELL: entry 1.10, structural SL 1.11
    assert scale_ghost_sl(1.10, 1.11, SignalAction.SELL, 0.1) == pytest.approx(1.101)
    assert scale_ghost_sl(1.10, 1.11, SignalAction.SELL, 3.0) == pytest.approx(1.13)


def test_alexg7_accepts_ghost_sl_mult():
    from borex.alexg import AlexG7Strategy

    s = AlexG7Strategy(ghost_sl_mult=0.5)
    assert s.ghost_sl_mult == 0.5
    assert s._scaled_ghost_sl(1.10, 1.09, SignalAction.BUY) == pytest.approx(1.095)
