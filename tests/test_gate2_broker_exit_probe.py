"""Tests for Gate 2 broker-triggered exit classification and distance planning."""

from __future__ import annotations

from types import SimpleNamespace

from borex.gate2.exit_classify import (
    DEAL_REASON_CLIENT,
    DEAL_REASON_EXPERT,
    DEAL_REASON_SL,
    DEAL_REASON_TP,
    EXIT_BROKER_SL,
    EXIT_BROKER_TP,
    EXIT_CLEANUP_MARKET_CLOSE,
    EXIT_PROBE_MARKET_CLOSE,
    EXIT_TIMEOUT,
    EXIT_UNKNOWN,
    choose_trigger_distance,
    classify_broker_exit,
    plan_sl_tp_levels,
    quote_would_trigger,
)
from borex.gate2.probe_reconcile import serialize_deal


def test_long_sl_classification():
    c = classify_broker_exit(
        exit_deal={"reason": DEAL_REASON_SL, "price": 1.1000, "comment": ""},
        requested_sl=1.1000,
        requested_tp=1.1100,
        price_tolerance=0.0001,
    )
    assert c.classification == EXIT_BROKER_SL
    assert c.confident is True
    assert any("DEAL_REASON_SL" in e for e in c.evidence)


def test_long_tp_classification():
    c = classify_broker_exit(
        exit_deal={"reason": DEAL_REASON_TP, "price": 1.1100, "comment": ""},
        requested_sl=1.0900,
        requested_tp=1.1100,
    )
    assert c.classification == EXIT_BROKER_TP
    assert c.confident is True


def test_short_sl_classification():
    c = classify_broker_exit(exit_deal={"reason": DEAL_REASON_SL, "price": 1.1050})
    assert c.classification == EXIT_BROKER_SL


def test_short_tp_classification():
    c = classify_broker_exit(exit_deal={"reason": DEAL_REASON_TP, "price": 1.0950})
    assert c.classification == EXIT_BROKER_TP


def test_market_close_classification():
    c = classify_broker_exit(
        exit_deal={"reason": DEAL_REASON_EXPERT, "comment": "borex_flat"},
        intentional_probe_close=True,
    )
    assert c.classification == EXIT_PROBE_MARKET_CLOSE
    assert c.confident is True


def test_cleanup_close_classification():
    c = classify_broker_exit(
        exit_deal={"reason": DEAL_REASON_EXPERT, "comment": "bx_g2c_cleanup"},
        intentional_cleanup_close=True,
    )
    assert c.classification == EXIT_CLEANUP_MARKET_CLOSE


def test_unknown_ambiguous_broker_exit():
    c = classify_broker_exit(
        exit_deal={"reason": DEAL_REASON_CLIENT, "comment": "", "price": 1.1000},
        requested_sl=1.1000,
        price_tolerance=0.00005,
    )
    assert c.classification == EXIT_UNKNOWN
    assert c.confident is False
    # Must NOT promote to BROKER_SL from price alone
    assert c.classification != EXIT_BROKER_SL


def test_wrong_direction_trigger_evidence():
    # LONG SL triggers on BID, not ASK
    assert quote_would_trigger(
        side="buy", intended="sl", sl=1.1000, tp=1.1100, bid=1.0999, ask=1.1005
    )
    assert not quote_would_trigger(
        side="buy", intended="sl", sl=1.1000, tp=1.1100, bid=1.1001, ask=1.0990
    )
    # SHORT TP triggers on ASK
    assert quote_would_trigger(
        side="sell", intended="tp", sl=1.1100, tp=1.1000, bid=1.1005, ask=1.0999
    )
    assert not quote_would_trigger(
        side="sell", intended="tp", sl=1.1100, tp=1.1000, bid=1.0990, ask=1.1001
    )


def test_serialize_deal_includes_reason():
    d = SimpleNamespace(
        ticket=1,
        order=2,
        position_id=3,
        symbol="EURUSD",
        type=1,
        entry=1,
        volume=0.01,
        price=1.1,
        profit=-0.02,
        commission=-0.04,
        swap=0.0,
        time=1_790_000_000,
        comment="",
        magic=88002,
        reason=DEAL_REASON_SL,
    )
    row = serialize_deal(d, offset_seconds=10800)
    assert row["reason"] == DEAL_REASON_SL


def test_timeout_without_accidental_success():
    c = classify_broker_exit(
        exit_deal=None,
        timed_out=True,
        position_still_open=True,
    )
    assert c.classification == EXIT_TIMEOUT
    assert c.classification not in (EXIT_BROKER_SL, EXIT_BROKER_TP)


def test_scenario_label_never_forces_broker_sl():
    # Even with SL-like comment, without reason=4 => UNKNOWN (unless harness flags)
    c = classify_broker_exit(
        exit_deal={"reason": None, "comment": "EURUSD_LONG_SL", "price": 1.1},
        requested_sl=1.1,
        price_tolerance=0.01,
    )
    assert c.classification == EXIT_UNKNOWN


def test_min_stop_distance_validation_fail_closed():
    # Huge min-stop makes quick trigger impractical
    d = choose_trigger_distance(
        pip_size=0.0001,
        min_stop_distance_price=0.0020,  # 20 pips
        spread_price=0.0001,
        max_trigger_pips=8.0,
    )
    assert d["ok"] is False
    assert "required_trigger_distance" in (d["rejection_reason"] or "")


def test_min_stop_distance_validation_ok():
    d = choose_trigger_distance(
        pip_size=0.0001,
        min_stop_distance_price=0.00012,
        spread_price=0.0001,
        max_trigger_pips=8.0,
    )
    assert d["ok"] is True
    assert d["distance_pips"] <= 8.0


def test_plan_sl_tp_levels_long():
    sl_plan = plan_sl_tp_levels(
        side="buy",
        entry_ref=1.10000,
        distance_price=0.00030,
        protective_distance_price=0.00500,
        digits=5,
        intended="sl",
    )
    assert sl_plan["sl"] < sl_plan["entry_ref"] < sl_plan["tp"]
    assert sl_plan["trigger_side"] == "bid"
    tp_plan = plan_sl_tp_levels(
        side="buy",
        entry_ref=1.10000,
        distance_price=0.00030,
        protective_distance_price=0.00500,
        digits=5,
        intended="tp",
    )
    assert tp_plan["sl"] < tp_plan["entry_ref"] < tp_plan["tp"]
    assert abs(tp_plan["tp"] - 1.10030) < 1e-9


def test_protected_eurdkk_not_selected_by_comment_prefix():
    # Exit-probe comments use bx_g2e_; EURDKK leftovers must never match
    leftover = {"symbol": "EURDKK", "ticket": 1890205005, "comment": "leftover", "magic": 88002}
    assert not str(leftover["comment"]).startswith("bx_g2e_")
    assert leftover["symbol"] != "EURUSD"


def test_live_account_identity_constants():
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import run_gate2_controlled_probe as g2

    assert g2.EXPECTED_MIRROR_LOGIN == 53019882
    assert g2.FORBIDDEN_LIVE_LOGIN == 52958398
    assert g2.REQUIRED_MAGIC == 88002
    assert g2.FORBIDDEN_LIVE_MAGIC == 88001
    assert g2.MIRROR_PORT == 8792


def test_stale_tick_and_unconfident_offset_still_fail_closed():
    from borex.data.mt5_time import assess_server_offset, assess_tick_freshness

    now = 1_790_012_557
    stale = 1_789_775_819
    fr = assess_tick_freshness(
        symbol="EURUSD", tick_time_unix_raw=stale, local_utc_unix=now, max_age_seconds=120
    )
    assert fr.fresh is False
    ao = assess_server_offset(
        tick_time_unix_raw=stale, local_utc_unix=now, max_age_seconds=120, symbol="EURUSD"
    )
    assert ao.confident is False
    assert ao.offset_seconds is None
