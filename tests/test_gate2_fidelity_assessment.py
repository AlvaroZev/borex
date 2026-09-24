"""Tests for Gate 2 E fidelity assessment layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from borex.gate2.fidelity_assessment import constants as C
from borex.gate2.fidelity_assessment.analyze import (
    analyze_exit_events,
    analyze_exit_price,
    analyze_exit_time,
    assert_no_forbidden_pnl_keys,
    build_assessment,
    valid_simulator_sample,
)
from borex.gate2.fidelity_assessment.census import (
    build_census_row,
    build_master_census,
    classify_exit_time_agreement,
    dedupe_key,
)
from borex.gate2.fidelity_assessment.leakage import run_leakage_audit
from borex.gate2.fidelity_assessment.runner import run_fidelity_assessment


REPO = Path(__file__).resolve().parents[1]


def _minimal_recon(**overrides):
    base = {
        "theory_event_id": "tev_a",
        "execution_id": "exe_a",
        "broker_position": 111,
        "symbol": "EURUSD=X",
        "direction": "long",
        "theory_signal_utc": "2026-09-24T04:00:00+00:00",
        "broker_entry_utc": "2026-09-24T04:00:01+00:00",
        "broker_exit_utc": "2026-09-24T04:10:15+00:00",
        "theory_entry": 1.1,
        "broker_entry": 1.1,
        "entry_delta_pips": 0.0,
        "theory_sl": 1.09,
        "theory_tp": 1.11,
        "broker_sl": 1.09,
        "broker_tp": 1.11,
        "broker_exit": 1.09,
        "broker_exit_reason": "BROKER_SL",
        "spread_pips": 0.1,
        "mapping_status": "EXACT_MATCH",
    }
    base.update(overrides)
    return base


def _minimal_replay(**overrides):
    base = {
        "theory_event_id": "tev_a",
        "execution_id": "exe_a",
        "broker_ticket": 111,
        "data_status": "REPLAYED",
        "sim_entry": 1.1,
        "sim_sl": 1.09,
        "sim_tp": 1.11,
        "sim_exit": 1.09,
        "sim_exit_reason": "SIM_SL",
        "sim_exit_time": "2026-09-24T04:10:00+00:00",
        "exit_event_agreement": True,
        "exit_delta_pips_sim_level_vs_broker": 0.0,
        "theory_reproduction": {"signal_match": True, "differences": []},
        "discrepancy_classes": ["MATCH"],
        "sim_exit_detail": {
            "status": "HIT_SL",
            "bars_evaluated": 10,
            "m1_path_len": 10,
            "path_insufficient_reason": None,
        },
    }
    base.update(overrides)
    return base


def test_dedupe_key_stable():
    a = {"theory_event_id": "t1", "execution_id": "e1", "broker_ticket": "9"}
    b = {"theory_event_id": "t1", "execution_id": "e1", "broker_position": "9"}
    assert dedupe_key(a) == dedupe_key(b)


def test_deduplication_across_runs_in_census_builder(tmp_path: Path, monkeypatch):
    """Same identity appearing twice is kept once."""
    from borex.gate2.fidelity_assessment import census as census_mod

    trade = {
        "theory_event_id": "tev_dup",
        "execution_id": "exe_dup",
        "broker_position": 42,
        "symbol": "EURUSD=X",
        "direction": "long",
        "theory_signal_utc": "2026-09-24T01:00:00+00:00",
        "broker_entry_utc": "2026-09-24T01:00:01+00:00",
        "broker_exit_utc": "2026-09-24T01:05:00+00:00",
        "theory_entry": 1.0,
        "broker_entry": 1.0,
        "entry_delta_pips": 0.0,
        "theory_sl": 0.9,
        "theory_tp": 1.1,
        "broker_sl": 0.9,
        "broker_tp": 1.1,
        "broker_exit": 0.9,
        "broker_exit_reason": "BROKER_SL",
        "mapping_status": "EXACT_MATCH",
        "spread_pips": 0.1,
    }

    def fake_load(repo_root, source_run):
        return {
            "source_run": source_run,
            "micro_dir": tmp_path,
            "recon_rows": [dict(trade)],
            "theory_by_id": {},
            "execution_by_id": {},
            "has_continuous_m1": source_run == C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN,
            "replay_run_id": "g2r_fake",
            "replay_by_key": {},
            "evidence_weight": "test",
        }

    monkeypatch.setattr(census_mod, "load_natural_source", fake_load)
    monkeypatch.setattr(C, "NATURAL_MICRO_RUNS", ("run_a", "run_b"))
    census = census_mod.build_master_census(tmp_path)
    assert census["raw_natural_rows"] == 2
    assert census["deduplicated_natural_trades"] == 1
    assert len(census["duplicates_dropped"]) == 1


def test_excluded_ohlc_unresolvable_trade():
    source = {
        "source_run": C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN,
        "evidence_weight": "highest_sim_broker",
        "replay_run_id": "g2r_20260924_055920",
        "theory_by_id": {},
        "has_continuous_m1": True,
    }
    recon = _minimal_recon(
        broker_position=int(C.EXCLUDED_SIM_FIDELITY_TICKET),
        symbol="AUDUSD=X",
        direction="long",
    )
    replay = _minimal_replay(
        broker_ticket=int(C.EXCLUDED_SIM_FIDELITY_TICKET),
        data_status="INSUFFICIENT_SOURCE_DATA",
        sim_exit_reason=None,
        exit_event_agreement=None,
        sim_exit_detail={
            "status": "INSUFFICIENT_PATH",
            "bars_evaluated": 72,
            "m1_path_len": 72,
            "path_insufficient_reason": "path_exhausted_without_sl_tp_hit",
        },
        discrepancy_classes=["INSUFFICIENT_SOURCE_DATA", "MISSING_1M_DATA"],
    )
    row = build_census_row(source=source, recon=recon, replay=replay)
    assert row["replay_status"] == C.OHLC_PATH_UNRESOLVABLE
    assert row["discrepancy_class"] == C.OHLC_PATH_UNRESOLVABLE
    assert row["excluded_from_sim_fidelity"] is True
    assert row["exit_event_agreement"] is None


def test_replayable_denominator_excludes_only_ohlc_case():
    good = {
        "source_run": C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN,
        "broker_ticket": "1",
        "mapping_status": "EXACT_MATCH",
        "continuous_m1_archive": True,
        "theory_event_id": "tev",
        "broker_exit_time": "2026-09-24T05:00:00+00:00",
        "replay_data_status": "REPLAYED",
        "excluded_from_sim_fidelity": False,
        "exit_event_agreement": True,
        "sim_exit_reason": "SIM_SL",
        "broker_exit_reason": "BROKER_SL",
        "sim_exit_mapped_broker_label": "BROKER_SL",
        "exit_delta_pips": 0.1,
        "entry_delta_pips": 0.0,
        "symbol": "EURUSD=X",
        "direction": "long",
        "exit_time_comparison": classify_exit_time_agreement(
            sim_exit_time="2026-09-24T05:00:00+00:00",
            broker_exit_time="2026-09-24T05:00:12+00:00",
        ),
    }
    bad = {
        **good,
        "broker_ticket": C.EXCLUDED_SIM_FIDELITY_TICKET,
        "excluded_from_sim_fidelity": True,
        "exclusion_reason": C.OHLC_PATH_UNRESOLVABLE,
        "replay_data_status": "INSUFFICIENT_SOURCE_DATA",
    }
    other_run = {**good, "source_run": "g2m_20260924_001929", "broker_ticket": "99"}
    sample = valid_simulator_sample([good, bad, other_run])
    assert sample["replayable_n"] == 1
    assert sample["excluded_n"] == 1
    assert sample["excluded"][0]["broker_ticket"] == C.EXCLUDED_SIM_FIDELITY_TICKET
    assert "99" not in sample["included_tickets"]  # Level 4 not in denominator


def test_exit_event_agreement_stats():
    trades = [
        {
            "broker_exit_reason": "BROKER_SL",
            "sim_exit_reason": "SIM_SL",
            "sim_exit_mapped_broker_label": "BROKER_SL",
            "exit_event_agreement": True,
            "exit_delta_pips": 0.0,
            "broker_ticket": "1",
            "symbol": "EURUSD=X",
            "direction": "long",
        },
        {
            "broker_exit_reason": "BROKER_TP",
            "sim_exit_reason": "SIM_TP",
            "sim_exit_mapped_broker_label": "BROKER_TP",
            "exit_event_agreement": True,
            "exit_delta_pips": 0.1,
            "broker_ticket": "2",
            "symbol": "GBPUSD=X",
            "direction": "short",
        },
    ]
    ev = analyze_exit_events(trades)
    assert ev["same_event"] == 2
    assert ev["different_event"] == 0
    assert ev["agreement_fraction"] == "2/2"


def test_exit_price_statistics():
    trades = [
        {
            "exit_event_agreement": True,
            "sim_exit_mapped_broker_label": "BROKER_SL",
            "broker_exit_reason": "BROKER_SL",
            "exit_delta_pips": 0.0,
            "symbol": "EURUSD=X",
            "broker_ticket": "1",
            "direction": "long",
            "sim_exit": 1.0,
            "broker_exit": 1.0,
        },
        {
            "exit_event_agreement": True,
            "sim_exit_mapped_broker_label": "BROKER_SL",
            "broker_exit_reason": "BROKER_SL",
            "exit_delta_pips": 0.8,
            "symbol": "GBPUSD=X",
            "broker_ticket": "2",
            "direction": "long",
            "sim_exit": 1.0,
            "broker_exit": 1.00008,
        },
    ]
    ep = analyze_exit_price(trades)
    assert ep["n_with_delta"] == 2
    assert ep["median_delta_pips"] == pytest.approx(0.4)
    assert ep["max_abs_delta_pips"] == pytest.approx(0.8)
    assert ep["largest_outliers"]


def test_exit_time_statistics():
    trades = [
        {
            "broker_ticket": "1",
            "symbol": "EURUSD=X",
            "sim_exit_reason": "SIM_SL",
            "broker_exit_time": "2026-09-24T05:10:33+00:00",
            "exit_time_comparison": classify_exit_time_agreement(
                sim_exit_time="2026-09-24T05:10:00+00:00",
                broker_exit_time="2026-09-24T05:10:33+00:00",
            ),
        },
        {
            "broker_ticket": "2",
            "symbol": "EURUSD=X",
            "sim_exit_reason": "SIM_TP",
            "broker_exit_time": "2026-09-24T05:12:01+00:00",
            "exit_time_comparison": classify_exit_time_agreement(
                sim_exit_time="2026-09-24T05:11:00+00:00",
                broker_exit_time="2026-09-24T05:12:01+00:00",
            ),
        },
    ]
    et = analyze_exit_time(trades)
    assert et["same_m1_candle"] == 1
    assert et["different_m1_candle"] == 1


def test_broker_vs_simulator_separation_in_census_row():
    source = {
        "source_run": C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN,
        "evidence_weight": "highest_sim_broker",
        "replay_run_id": "g2r_x",
        "theory_by_id": {},
        "has_continuous_m1": True,
    }
    row = build_census_row(
        source=source,
        recon=_minimal_recon(broker_exit=1.0901),
        replay=_minimal_replay(sim_exit=1.09, exit_delta_pips_sim_level_vs_broker=1.0),
    )
    assert row["sim_exit"] == 1.09
    assert row["broker_exit"] == 1.0901
    assert row["sim_exit_reason"] == "SIM_SL"
    assert row["broker_exit_reason"] == "BROKER_SL"
    assert row["sim_exit"] != row["broker_exit"]


def test_controlled_vs_natural_separation():
    # Controlled runs must not appear in natural census sources
    assert "g2p_20260922_162842" not in C.NATURAL_MICRO_RUNS
    assert "g2e_20260923_170458" not in C.NATURAL_MICRO_RUNS
    assert "g2f_20260923_181609" not in C.NATURAL_MICRO_RUNS
    for meta in C.CONTROLLED_RUNS.values():
        assert meta["kind"].startswith("controlled") or meta["kind"] == "fidelity_reconciliation"


def test_no_pnl_metrics_in_assessment_payload():
    payload = {
        "entry_fidelity": {"median": 0.0},
        "nested": {"ok": True},
    }
    assert_no_forbidden_pnl_keys(payload)
    with pytest.raises(AssertionError):
        assert_no_forbidden_pnl_keys({"profit_factor": 1.2})
    with pytest.raises(AssertionError):
        assert_no_forbidden_pnl_keys({"metrics": {"wr": 0.5}})


def test_no_automatic_pass_fail_and_no_double_count(monkeypatch):
    # Use real census if available; otherwise skip heavy path
    micro = REPO / "data/runs/gate2_micro_fidelity/runs/g2m_20260924_043542"
    if not micro.is_dir():
        pytest.skip("Level 5b artifacts not present")
    census = build_master_census(REPO)
    tickets = [t["broker_ticket"] for t in census["trades"]]
    assert len(tickets) == len(set(tickets)) or census["duplicates_dropped"] == []
    # stronger: keys unique
    keys = [
        (t["theory_event_id"], t["execution_id"], t["broker_ticket"]) for t in census["trades"]
    ]
    assert len(keys) == len(set(keys))

    assessment = build_assessment(REPO, census=census)
    assert assessment["automatic_pass_fail"] is None
    assert assessment["gate2_bucket_E"] not in ("PASS", "FAIL")
    assert assessment["no_single_fidelity_score"] is True
    assert "fidelity_score" not in assessment
    assert_no_forbidden_pnl_keys(assessment)


def test_no_broker_result_leakage_into_simulator():
    audit = run_leakage_audit(REPO)
    assert audit["independent"] is True
    assert audit["result"] == "PASS_INDEPENDENT"
    assert audit["compare_call_order"]["broker_kwargs_passed_to_simulator"] is False
    assert "broker_exit" not in audit["simulate_exit_signature"]["parameters"]


@pytest.mark.parametrize(
    "path_exists",
    [
        REPO / "data/runs/gate2_micro_fidelity/runs/g2m_20260924_043542",
        REPO / "data/runs/gate2_fidelity_replay/runs/g2r_20260924_055920",
    ],
)
def test_level5b_artifacts_present_for_integration(path_exists: Path):
    if not path_exists.exists():
        pytest.skip(f"missing {path_exists}")


def test_end_to_end_assessment_run(tmp_path: Path):
    micro = REPO / "data/runs/gate2_micro_fidelity/runs/g2m_20260924_043542"
    if not micro.is_dir():
        pytest.skip("Level 5b artifacts not present")
    # Run against real repo; write into a temp overlay by patching out_dir via run_id under repo
    rid = "g2a_test_assessment_unit"
    out = REPO / "data/runs/gate2_fidelity_assessment/runs" / rid
    result = run_fidelity_assessment(REPO, run_id=rid)
    assert result["run_id"] == rid
    assert (out / "manifest.json").is_file()
    assert (out / "master_trade_census.json").is_file()
    assert (out / "master_trade_census.csv").is_file()
    assert (out / "fidelity_assessment.json").is_file()
    assert (out / "fidelity_assessment.md").is_file()

    assessment = json.loads((out / "fidelity_assessment.json").read_text(encoding="utf-8"))
    sample = assessment["simulator_fidelity_sample"]
    assert sample["replayable_n"] == 20
    assert sample["excluded_n"] == 1
    assert sample["excluded"][0]["broker_ticket"] == C.EXCLUDED_SIM_FIDELITY_TICKET
    assert sample["excluded"][0]["exclusion_reasons"] == [C.OHLC_PATH_UNRESOLVABLE]
    ev = assessment["exit_event_fidelity"]
    assert ev["same_event"] == 20
    assert ev["different_event"] == 0
    assert assessment["automatic_pass_fail"] is None
    assert assessment["gate2_bucket_E"] == "UNRESOLVED"
    assert assessment["simulator_independence_leakage"]["result"] == "PASS_INDEPENDENT"
    # PnL keys absent
    blob = json.dumps(assessment)
    for forbidden in ("profit_factor", '"wr"', "max_drawdown", "expectancy"):
        assert forbidden not in blob
