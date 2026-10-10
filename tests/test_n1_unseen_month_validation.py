"""Coverage-first selection for N1 unseen-month validation (no PnL)."""

from __future__ import annotations

from scripts.run_n1_unseen_month_validation import EXCLUDED_MONTHS, select_month


def test_excluded_months_are_may_jun_jul_aug_2026():
    assert EXCLUDED_MONTHS == {(2026, 5), (2026, 6), (2026, 7), (2026, 8)}


def test_select_month_picks_most_recent_valid():
    candidates = [
        {"year": 2024, "month": 11, "label": "2024-11", "final_status": "VALID_CANDIDATE"},
        {"year": 2024, "month": 12, "label": "2024-12", "final_status": "VALID_CANDIDATE"},
        {"year": 2025, "month": 1, "label": "2025-01", "final_status": "INSUFFICIENT"},
    ]
    sel = select_month(candidates)
    assert sel["performance_inspected_before_selection"] is False
    assert sel["selected"]["label"] == "2024-12"


def test_select_month_none_when_empty():
    sel = select_month(
        [{"year": 2026, "month": 9, "label": "2026-09", "final_status": "INSUFFICIENT"}]
    )
    assert sel["selected"] is None
