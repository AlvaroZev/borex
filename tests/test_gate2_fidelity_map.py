"""Tests for Gate 2 fidelity mapping helpers (read-only; no orders)."""

from __future__ import annotations

from borex.gate2.fidelity_map import (
    CONV_LONG_ENTRY,
    CONV_LONG_EXIT,
    CONV_LONG_SL_TRIGGER,
    CONV_LONG_TP_TRIGGER,
    CONV_SHORT_ENTRY,
    CONV_SHORT_EXIT,
    CONV_SHORT_SL_TRIGGER,
    CONV_SHORT_TP_TRIGGER,
    DISC_NOT_SIGNAL,
    MAP_AMBIGUOUS,
    MAP_EXACT_MATCH,
    MAP_PLAN_MATCH,
    MAP_PLAN_PRICE_SKEW,
    MAP_THEORY_NA,
    MAP_TIME_SKEW,
    MAP_UNMAPPED,
    classify_plan_mapping,
    classify_theory_candidates,
    controlled_trade_row,
    execution_convention,
    parse_iso,
    price_delta_pips,
    time_skew_seconds,
)


def test_exact_plan_mapping():
    assert classify_plan_mapping(entry_delta_pips=0.0, exit_delta_pips=0.02) == MAP_PLAN_MATCH


def test_time_skew_theory_mapping():
    assert classify_theory_candidates([(120.0, {"id": "a"})]) == MAP_TIME_SKEW
    assert classify_theory_candidates([(0.5, {"id": "a"})]) == MAP_EXACT_MATCH


def test_ambiguous_theory_mapping_no_force():
    assert (
        classify_theory_candidates([(100.0, {"id": "a"}), (150.0, {"id": "b"})]) == MAP_AMBIGUOUS
    )


def test_unmapped_broker_theory():
    assert classify_theory_candidates([]) == MAP_UNMAPPED


def test_theory_only_constant_present():
    from borex.gate2.fidelity_map import MAP_THEORY_ONLY

    assert MAP_THEORY_ONLY == "THEORY_ONLY"


def test_long_bid_ask_convention():
    c = execution_convention("buy")
    assert c.entry_side == CONV_LONG_ENTRY == "ASK"
    assert c.exit_side == CONV_LONG_EXIT == "BID"


def test_short_bid_ask_convention():
    c = execution_convention("sell")
    assert c.entry_side == CONV_SHORT_ENTRY == "BID"
    assert c.exit_side == CONV_SHORT_EXIT == "ASK"


def test_long_sl_tp_trigger_sides():
    c = execution_convention("long")
    assert c.sl_trigger_side == CONV_LONG_SL_TRIGGER == "BID"
    assert c.tp_trigger_side == CONV_LONG_TP_TRIGGER == "BID"


def test_short_sl_tp_trigger_sides():
    c = execution_convention("short")
    assert c.sl_trigger_side == CONV_SHORT_SL_TRIGGER == "ASK"
    assert c.tp_trigger_side == CONV_SHORT_TP_TRIGGER == "ASK"


def test_h1_timestamp_parse_and_skew():
    a = "2026-09-08T14:00:00+00:00"
    b = "2026-09-08T15:00:46+00:00"
    assert parse_iso(a) is not None
    skew = time_skew_seconds(b, a)
    assert skew is not None and abs(skew - 3646) < 1


def test_server_offset_documented_in_controlled_row():
    row = controlled_trade_row(
        {
            "side": "buy",
            "symbol_mt5": "EURUSD",
            "live_entry_ref": 1.13724,
            "actual_entry_price": 1.13724,
            "entry_deal": {"price": 1.13724, "time_utc": "2026-09-23T17:04:56+00:00", "reason": 3},
            "exit_deal": {
                "price": 1.13701,
                "time_utc": "2026-09-23T17:06:19+00:00",
                "reason": 4,
                "comment": "[sl 1.13704]",
            },
            "broker_sl_requested": 1.13704,
            "exit_trigger_classification": "BROKER_SL",
            "exit_classification": {"confident": True},
            "reconciliation_status": "reconciled",
            "intended_trigger": "sl",
            "server_offset_seconds": 10800,
            "scenario_id": "BROKER_EXIT_LONG_SL",
            "position_ticket": 1,
            "commission": -0.08,
            "net_pnl": -0.31,
        },
        sample="CONTROLLED",
        source_batch="g2e_test",
    )
    assert row["theory_mapping_status"] == MAP_THEORY_NA
    assert DISC_NOT_SIGNAL in row["discrepancy_classes"]
    assert row["server_offset_seconds"] == 10800
    assert row["mapping_status"] in (MAP_PLAN_MATCH, MAP_PLAN_PRICE_SKEW)


def test_stale_untrusted_timestamp_parse_none():
    assert parse_iso(None) is None
    assert parse_iso("not-a-time") is None


def test_missing_1m_marked_unavailable():
    row = controlled_trade_row(
        {
            "side": "buy",
            "symbol_mt5": "EURUSD",
            "live_entry_ref": 1.1,
            "entry_deal": {"price": 1.1},
            "exit_deal": {"price": 1.1, "reason": 0},
            "exit_reason": "probe_market_close",
            "scenario_id": "EURUSD_LONG_SL",
            "reconciliation_status": "reconciled",
            "position_ticket": 2,
        },
        sample="CONTROLLED",
        source_batch="g2p_test",
    )
    assert row["one_m_evidence"] == "unavailable"
    assert "EXIT_MODEL" in row["discrepancy_classes"]


def test_commission_fields_pass_through():
    row = controlled_trade_row(
        {
            "side": "buy",
            "symbol_mt5": "EURUSD",
            "live_entry_ref": 1.1,
            "entry_deal": {"price": 1.1},
            "exit_deal": {"price": 1.1, "reason": 5},
            "exit_trigger_classification": "BROKER_TP",
            "exit_classification": {"confident": True},
            "intended_trigger": "tp",
            "broker_tp_requested": 1.1,
            "reconciliation_status": "reconciled",
            "commission": -0.08,
            "swap": 0.0,
            "gross_pnl": 0.21,
            "net_pnl": 0.13,
            "position_ticket": 3,
        },
        sample="CONTROLLED",
        source_batch="g2e",
    )
    assert row["commission"] == -0.08
    assert row["net_pnl"] == 0.13


def test_no_forced_match_when_two_close_candidates():
    status = classify_theory_candidates([(10.0, "t1"), (20.0, "t2")])
    assert status == MAP_AMBIGUOUS


def test_controlled_vs_natural_sample_separation():
    c = controlled_trade_row(
        {
            "side": "buy",
            "symbol_mt5": "EURUSD",
            "live_entry_ref": 1.0,
            "entry_deal": {"price": 1.0},
            "exit_deal": {"price": 1.0},
            "exit_reason": "probe_market_close",
            "reconciliation_status": "reconciled",
            "position_ticket": 9,
        },
        sample="CONTROLLED",
        source_batch="x",
    )
    assert c["sample"] == "CONTROLLED"
    assert c["theory_mapping_status"] == MAP_THEORY_NA


def test_plan_price_skew_classification():
    assert classify_plan_mapping(entry_delta_pips=1.0, exit_delta_pips=0.0) == MAP_PLAN_PRICE_SKEW


def test_price_delta_pips_eurusd_and_jpy():
    assert abs(price_delta_pips(1.13724, 1.13704, symbol="EURUSD") - 2.0) < 1e-9
    assert abs(price_delta_pips(110.55, 110.50, symbol="AUDJPY=X") - 5.0) < 1e-9
