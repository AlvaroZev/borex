"""Leverage sizing audit — proves true_sl+margin couples leverage to exposure/geometry."""

from __future__ import annotations

from scripts.run_gate2_strategy_optimization_v2 import leverage_sizing_audit


def test_leverage_changes_sl_distance_and_notional():
    audit = leverage_sizing_audit()
    assert audit["simulator_defect"] is False
    by = {r["leverage"]: r for r in audit["by_leverage"]}
    assert by[500.0]["sl_move_pct"] == 0.002
    assert by[5000.0]["sl_move_pct"] == 0.0002
    # Higher leverage → tighter SL and larger notional
    assert by[5000.0]["sl_distance_price"] < by[500.0]["sl_distance_price"]
    assert by[5000.0]["notional"] > by[500.0]["notional"]
    assert by[5000.0]["lots"] > by[500.0]["lots"]


def test_risk_at_sl_is_posted_margin():
    audit = leverage_sizing_audit()
    for r in audit["by_leverage"]:
        assert abs(r["risk_at_sl_usd"] - r["margin_posted"]) < 1e-9
