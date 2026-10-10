"""Unit tests for GhostSL two-stage geometry (Gate 2 user definition)."""

from __future__ import annotations

import pytest

from borex.gate2.ghostsl import (
    GhostStatus,
    build_ghost_setup,
    ghost_sl_from_g,
    real_levels_from_entry,
    resolve_activation_bar,
)
from borex.gate2.september_firewall import SeptemberFirewallError, assert_not_september


def test_ghostsl_long_geometry():
    s = build_ghost_setup(
        1.1000,
        "long",
        ghost_sl_distance=0.0100,
        real_sl_distance=0.0100,
        rr=2.0,
    )
    assert s.ghost_sl == pytest.approx(1.0900)
    assert s.real_entry == pytest.approx(1.0900)
    assert s.real_sl == pytest.approx(1.0800)
    assert s.real_tp == pytest.approx(1.1100)
    # real levels NOT anchored to G
    assert s.real_sl != s.ghost_entry - 0.0100 or s.real_entry != s.ghost_entry
    s.validate_geometry()


def test_ghostsl_short_geometry():
    s = build_ghost_setup(
        1.1000,
        "short",
        ghost_sl_distance=0.0100,
        real_sl_distance=0.0100,
        rr=2.0,
    )
    assert s.ghost_sl == pytest.approx(1.1100)
    assert s.real_entry == pytest.approx(1.1100)
    assert s.real_sl == pytest.approx(1.1200)
    assert s.real_tp == pytest.approx(1.0900)
    s.validate_geometry()


def test_real_sl_tp_recalculated_from_real_entry_not_g():
    # If we kept G-anchored TP of G+2*ghost_risk, TP would differ
    g = 1.2000
    ghost_sl = ghost_sl_from_g(g, "long", sl_distance=0.005)
    real_sl, real_tp = real_levels_from_entry(
        ghost_sl, "long", real_sl_distance=0.005, rr=3.0
    )
    # G-anchored wrong TP would be g + 3*0.005 = 1.215
    wrong_g_anchored_tp = g + 3 * 0.005
    assert real_tp == pytest.approx(ghost_sl + 0.015)
    assert real_tp != pytest.approx(wrong_g_anchored_tp)


def test_activation_bar_both_sl_tp_invalidated():
    s = build_ghost_setup(1.10, "long", ghost_sl_distance=0.01, rr=1.0)
    # bar spans through real_entry, real_sl, and real_tp
    st = resolve_activation_bar(
        s, high=s.real_tp + 0.001, low=s.real_sl - 0.001, open_=1.10, close=1.09
    )
    assert st == GhostStatus.INVALIDATED


def test_activation_bar_entry_only_active():
    s = build_ghost_setup(1.10, "long", ghost_sl_distance=0.01, rr=2.0)
    st = resolve_activation_bar(
        s,
        high=s.real_entry + 0.001,
        low=s.real_entry - 0.0001,
        open_=1.095,
        close=1.091,
    )
    assert st == GhostStatus.REAL_ACTIVE


def test_september_firewall_still_active():
    with pytest.raises(SeptemberFirewallError):
        assert_not_september("2026-09-01T00:00:00+00:00")
