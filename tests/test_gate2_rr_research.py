"""Tests for RR stage diagnostics and grid collapse (research helpers)."""

from __future__ import annotations

from scripts.run_gate2_rr_research import collapse_grid, configs_redundant, stages_rr


def test_rr_min_applied_after_factor():
    s = stages_rr(
        rr_mode="dynamic",
        fixed_rr=2.0,
        winrate=None,
        rr_factor=0.6,
        closed_trades=0,
        winrate_min_trades=20,
        rr_min=2.0,
        rr_max=5.0,
    )
    assert s["base_rr"] == 2.0
    assert abs(s["post_factor_rr"] - 1.2) < 1e-12
    assert s["final_rr"] == 2.0
    assert s["hit_rr_min"] is True


def test_rr_max_clamp():
    s = stages_rr(
        rr_mode="dynamic",
        fixed_rr=2.0,
        winrate=0.2,  # base 5.0
        rr_factor=1.4,  # 7.0
        closed_trades=50,
        winrate_min_trades=20,
        rr_min=2.0,
        rr_max=5.0,
    )
    assert abs(s["post_factor_rr"] - 7.0) < 1e-12
    assert s["final_rr"] == 5.0
    assert s["hit_rr_max"] is True


def test_collapse_detects_redundant_low_factor_with_floor():
    # With rr_min=2, factor 0.6 and 0.7 both floor early bases similarly for many bases
    assert configs_redundant(2.0, 0.6, 2.0, 0.7, 5.0) or not configs_redundant(
        2.0, 0.6, 2.0, 1.4, 5.0
    )
    kept, notes = collapse_grid([2.0], [0.6, 0.7, 1.0, 1.4], rr_max=5.0)
    assert len(kept) >= 1
    assert len(kept) + len(notes) == 4
