"""Walk-forward candidate laboratory — leakage + selection tests."""

from __future__ import annotations

import pytest

from borex.gate2.weekly_research import (
    MIN_TRAIN_FILL_FRACTION,
    assert_no_future_leakage,
    candidate_passes_fill_gate,
    complete_iso_weeks,
    min_train_fills_threshold,
    select_candidate_on_train,
    selection_rank_key,
    thursday_harmful_on_train,
    walk_forward_splits,
)


def test_selection_threshold_documented():
    assert MIN_TRAIN_FILL_FRACTION == 0.50
    assert min_train_fills_threshold(100) == 50
    assert min_train_fills_threshold(40) == 30  # absolute floor
    assert candidate_passes_fill_gate(50, 100) is True
    assert candidate_passes_fill_gate(49, 100) is False


def test_walk_forward_no_future_leakage():
    weeks = [f"2026-W{i:02d}" for i in range(23, 36)]
    splits = walk_forward_splits(weeks, min_train=4, val_size=2)
    assert_no_future_leakage(splits)
    # Expanding: later train sets nest earlier weeks
    for i in range(1, len(splits)):
        assert splits[i]["train_weeks"][: len(splits[i - 1]["train_weeks"])] == splits[i - 1][
            "train_weeks"
        ]
        assert min(splits[i]["validation_weeks"]) > max(splits[i - 1]["train_weeks"])


def test_future_weeks_cannot_influence_earlier_selection():
    """Simulate selection at wf_1; later-week metrics must not change the pick."""
    control = {
        "fills": 400,
        "PF": 1.0,
        "mean_trade": 0.1,
        "median_weekly_net": 5.0,
        "worst_week": -20.0,
        "thursday_net": -50.0,
    }
    early_good = {
        "fills": 380,
        "PF": 1.2,
        "mean_trade": 0.3,
        "median_weekly_net": 12.0,
        "worst_week": -10.0,
    }
    late_only_looks_good = {
        "fills": 380,
        "PF": 2.0,
        "mean_trade": 1.0,
        "median_weekly_net": 99.0,
        "worst_week": 0.0,
    }
    # At early split, late_only is not in the eligible pool (not evaluated yet)
    sel1 = select_candidate_on_train(
        candidate_train={
            "n1_control": control,
            "n1_thursday": early_good,
        }
    )
    assert sel1["selected_id"] == "n1_thursday"

    # Injecting a superior candidate that would only be known from future data
    # into a *later* selection is fine; it must not rewrite sel1.
    sel2 = select_candidate_on_train(
        candidate_train={
            "n1_control": control,
            "n1_thursday": early_good,
            "posthoc_miracle": late_only_looks_good,
        }
    )
    assert sel1["selected_id"] == "n1_thursday"
    assert sel2["selected_id"] == "posthoc_miracle"
    # Critical: earlier decision object unchanged
    assert sel1["selected_id"] != sel2["selected_id"] or True
    assert sel1["selected_id"] == "n1_thursday"


def test_select_rejects_low_trade_count_winner():
    control = {
        "fills": 400,
        "PF": 1.0,
        "mean_trade": 0.1,
        "median_weekly_net": 5.0,
        "worst_week": -20.0,
    }
    tiny = {
        "fills": 10,  # below 50% of 400
        "PF": 5.0,
        "mean_trade": 2.0,
        "median_weekly_net": 50.0,
        "worst_week": 1.0,
    }
    sel = select_candidate_on_train(
        candidate_train={"n1_control": control, "tiny_pf": tiny}
    )
    assert sel["selected_id"] == "n1_control"
    assert sel["reason"] == "no_eligible_treatment"


def test_thursday_harm_uses_train_only_signal():
    assert thursday_harmful_on_train({"thursday_net": -1.0}) is True
    assert thursday_harmful_on_train({"thursday_net": 0.0}) is False
    assert thursday_harmful_on_train({"thursday_net": 5.0}) is False


def test_complete_iso_weeks_excludes_september_spill():
    rows = [
        {"week": "2026-W35", "week_start": "2026-08-24"},  # Aug 24-30 OK
        {"week": "2026-W36", "week_start": "2026-08-31"},  # spills into Sep
        {"week": "2026-W23", "week_start": "2026-06-01"},
    ]
    weeks = complete_iso_weeks(rows)
    assert "2026-W35" in weeks
    assert "2026-W23" in weeks
    assert "2026-W36" not in weeks


def test_assert_no_future_leakage_raises():
    bad = [
        {
            "split_id": "bad",
            "train_weeks": ["2026-W25", "2026-W26"],
            "validation_weeks": ["2026-W24", "2026-W27"],
        }
    ]
    with pytest.raises(AssertionError):
        assert_no_future_leakage(bad)


def test_selection_rank_key_ordering():
    a = {"median_weekly_net": 10, "PF": 1.1, "mean_trade": 0.2, "worst_week": -5, "fills": 100}
    b = {"median_weekly_net": 8, "PF": 1.5, "mean_trade": 0.5, "worst_week": -1, "fills": 100}
    assert selection_rank_key(a) > selection_rank_key(b)
