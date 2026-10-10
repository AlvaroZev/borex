"""Tests for Gate 2 daily theory↔broker dataset builder."""

from __future__ import annotations

from borex.gate2.daily_theory_broker import aggregate_day, trade_row_from_census


def test_trade_row_preserves_exclusion_reason():
    raw = {
        "signal_time": "2026-09-24T01:00:00+00:00",
        "theory_event_id": "t1",
        "execution_id": "e1",
        "broker_ticket": "9",
        "symbol": "EURUSD=X",
        "direction": "long",
        "theory_entry": 1.1,
        "broker_entry": 1.1,
        "entry_delta_pips": 0.0,
        "mapping_status": "EXACT_MATCH",
        "pnl_excluded_from_fidelity": True,
        "discrepancy_classes": ["INSUFFICIENT_SOURCE_DATA"],
        "broker_exit_reason": "BROKER_TP",
    }
    row = trade_row_from_census(raw)
    assert row["day"] == "2026-09-24"
    assert "INSUFFICIENT_SOURCE_DATA" in str(row["exclusion_reason"])
    assert row["same_event_agreement"] is True


def test_aggregate_day_counts():
    rows = [
        trade_row_from_census(
            {
                "signal_time": "2026-09-24T01:00:00+00:00",
                "broker_ticket": "1",
                "theory_entry": 1.0,
                "broker_entry": 1.0,
                "entry_delta_pips": 0.5,
                "mapping_status": "EXACT_MATCH",
                "broker_exit_reason": "BROKER_TP",
                "exit_event_agreement": True,
            }
        ),
        trade_row_from_census(
            {
                "signal_time": "2026-09-24T02:00:00+00:00",
                "broker_ticket": "2",
                "theory_entry": 1.0,
                "broker_entry": 1.0,
                "entry_delta_pips": -0.5,
                "mapping_status": "EXACT_MATCH",
                "broker_exit_reason": "BROKER_SL",
                "exit_event_agreement": False,
                "exclusion_reason": "test_exclusion",
            }
        ),
    ]
    agg = aggregate_day(rows)
    assert agg["fills"] == 2
    assert agg["TP_count"] == 1
    assert agg["SL_or_margin_stop_count"] == 1
    assert agg["excluded_count"] == 1
