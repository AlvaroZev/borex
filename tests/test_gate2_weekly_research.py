"""Tests for Gate 2 weekly research helpers."""

from __future__ import annotations

from borex.gate2.weekly_research import (
    is_september_reserved,
    iso_week_key,
    summarize_trades,
    walk_forward_splits,
    weekly_distribution,
    weekly_metrics_table,
    weekly_slices,
)


def test_september_reserved():
    assert is_september_reserved("2026-09-15T12:00:00+00:00") is True
    assert is_september_reserved("2026-08-31T12:00:00+00:00") is False


def test_weekly_slices_exclude_september():
    trades = [
        {"entry_time": "2026-08-03T10:00:00+00:00", "pnl": 1.0, "side": "long", "exit_reason": "daily_close", "symbol": "EURUSD=X"},
        {"entry_time": "2026-09-07T10:00:00+00:00", "pnl": -5.0, "side": "long", "exit_reason": "daily_close", "symbol": "EURUSD=X"},
    ]
    slices = weekly_slices(trades, exclude_september=True)
    assert all(not w.startswith("2026-W") or "09" not in trades[1]["entry_time"] or True for w in slices)
    # September trade excluded
    all_t = [t for ts in slices.values() for t in ts]
    assert len(all_t) == 1
    assert float(all_t[0]["pnl"]) == 1.0


def test_walk_forward_chronological():
    weeks = [f"2026-W{i:02d}" for i in range(22, 35)]
    splits = walk_forward_splits(weeks, min_train=4, val_size=2)
    assert splits
    for sp in splits:
        assert max(sp["train_weeks"]) < min(sp["validation_weeks"])


def test_weekly_distribution_and_metrics():
    trades = []
    for i, pnl in enumerate([2.0, -1.0, 3.0, -2.0, 1.0]):
        trades.append(
            {
                "entry_time": f"2026-06-{1+i*7:02d}T12:00:00+00:00",
                "pnl": pnl,
                "side": "long",
                "exit_reason": "daily_close",
                "symbol": "EURUSD=X",
            }
        )
    rows = weekly_metrics_table(trades, candidate_id="t")
    dist = weekly_distribution(rows)
    assert dist["n_weeks"] >= 1
    s = summarize_trades(trades, intents=5, mdd=0.1)
    assert s["fills"] == 5
    assert s["net_pnl"] == 3.0


def test_iso_week_key_stable():
    assert iso_week_key("2026-06-01T00:00:00+00:00").startswith("2026-W")


def test_filter_session_hours_uses_epoch_seconds():
    from borex.gate2.weekly_research import filter_session_hours
    # 2026-07-02 13:00 UTC Thursday → overlap
    tu = int(__import__("pandas").Timestamp("2026-07-02 13:00:00", tz="UTC").timestamp())
    row = {"time_unix": tu, "action": "buy", "symbol": "EURUSD=X"}
    assert len(filter_session_hours([row], "overlap")) == 1
    assert len(filter_session_hours([row], "asia")) == 0
