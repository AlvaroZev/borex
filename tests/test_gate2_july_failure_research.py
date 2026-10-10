"""Tests for Gate 2 July failure research helpers (offline, no MT5)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_gate2_july_failure_research.py"


def _load():
    spec = importlib.util.spec_from_file_location("july_fail_research", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_exclusive_session_buckets(mod):
    assert mod.exclusive_session_bucket(13) == "overlap"
    assert mod.exclusive_session_bucket(8) == "london_pre_overlap"
    assert mod.exclusive_session_bucket(17) == "newyork_post_overlap"
    assert mod.exclusive_session_bucket(3) == "asia"
    assert mod.exclusive_session_bucket(22) == "off_hours"


def test_quality_vs_count_decomposition(mod):
    a = {
        "trades": [{"pnl": 1.0}] * 10,
    }
    b = {
        "trades": [{"pnl": -1.0}] * 8,
    }
    q = mod.quality_vs_count(a, b, "june", "july")
    assert q["june_n"] == 10
    assert q["july_n"] == 8
    assert q["interpretation"] in {"quality_dominated", "count_dominated"}
    # Δnet = 8*(-1) - 10*(1) = -18
    assert abs(q["net_delta"] - (-18.0)) < 1e-9
    # count component = 1.0 * (8-10) = -2
    assert abs(q["count_component_holding_mean_fixed"] - (-2.0)) < 1e-9
    # quality = 8 * (-1 - 1) = -16
    assert abs(q["quality_component_holding_n_fixed_at_b"] - (-16.0)) < 1e-9
    assert q["interpretation"] == "quality_dominated"


def test_bucket_stats_and_pf(mod):
    st = mod.bucket_stats([10.0, -5.0, 2.0])
    assert st["n"] == 3
    assert st["sum_pnl"] == 7.0
    assert st["pf"] == pytest.approx(12.0 / 5.0)


def test_parse_pattern_alexg8(mod):
    p = (
        "alexg8|EURUSD=X|support|daily|close_in_aoi|bias=off|trend0|pat0|"
        "ret0|ghost1|sess=all|g:10:1.1:1.0:1.2|fill:close"
    )
    out = mod.parse_pattern(p)
    assert out["structure"] == "support"
    assert out["tf"] == "daily"
    assert out["session_tag"] == "all"
    assert out["aoi_lo"] == 1.0
    assert out["aoi_hi"] == 1.2


def test_filter_inventory_includes_session_and_missing_dow(mod):
    inv = mod.build_filter_inventory()
    names = [i["name"] for i in inv["items"]]
    assert any("ablation.session" == n for n in names)
    assert any("day_of_week" in n for n in names)
    dow = next(i for i in inv["items"] if "day_of_week" in i["name"])
    assert dow["enabled"] is False


def test_decide_hypothesis_stops_without_credible_session(mod):
    """Synthetic forensic where no session enum dual-improves JUN+AUG+July."""
    forensic = {
        "months": {"july": {}},
        "july_dow": [
            {
                "dow_name": "Thu",
                "n": 70,
                "sum_pnl": -200.0,
                "mean_pnl": -2.0,
                "pf": 0.3,
            }
        ],
        "july_side_session": [
            {
                "side": "long",
                "session_exclusive": "newyork_post_overlap",
                "sum_pnl": -60.0,
                "n": 40,
            },
            {
                "side": "long",
                "session_exclusive": "london_pre_overlap",
                "sum_pnl": -100.0,
                "n": 45,
            },
            {
                "side": "short",
                "session_exclusive": "asia",
                "sum_pnl": -80.0,
                "n": 50,
            },
        ],
        "session_enum_counterfactuals": {
            "all": {
                "june_plus_august": {"n": 800, "mean_pnl": 0.4, "pf": 1.12},
                "july": {"n": 370, "mean_pnl": -0.3, "pf": 0.91},
            },
            "asia": {
                "june_plus_august": {"n": 200, "mean_pnl": 0.05, "pf": 1.01},
                "july": {"n": 100, "mean_pnl": -0.6, "pf": 0.8},
            },
            "london": {
                # worse JUN+AUG than all
                "june_plus_august": {"n": 300, "mean_pnl": 0.2, "pf": 1.05},
                "july": {"n": 150, "mean_pnl": -0.4, "pf": 0.86},
            },
            "newyork": {
                "june_plus_august": {"n": 300, "mean_pnl": -0.2, "pf": 0.9},
                "july": {"n": 130, "mean_pnl": -0.2, "pf": 0.88},
            },
            "overlap": {
                "june_plus_august": {"n": 150, "mean_pnl": 0.1, "pf": 1.05},
                "july": {"n": 60, "mean_pnl": 0.2, "pf": 1.1},
            },
        },
    }
    decision = mod.decide_hypothesis(forensic, {"items": []})
    assert decision["action"] == "STOP"
    assert decision["hypothesis"] is None
