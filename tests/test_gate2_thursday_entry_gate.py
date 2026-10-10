"""Regression tests for research-only Thursday entry gate."""

from __future__ import annotations

from borex.gate2.thursday_entry_gate import (
    THURSDAY,
    filter_block_thursday_entries,
    is_thursday_entry,
    thursday_entry_stats,
)


def _row(time_unix: int, action: str = "buy", symbol: str = "EURUSD=X") -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "time_unix": time_unix,
        "index": 1,
        "price": 1.0,
        "stop_loss": 0.9,
        "take_profit": 1.1,
        "pattern": "alexg8|test",
        "score": 2.0,
    }


def test_thursday_constant():
    assert THURSDAY == 3


def test_blocks_thursday_keeps_others():
    # 2026-07-02 Thursday 12:00 UTC
    thu = int(__import__("pandas").Timestamp("2026-07-02 12:00:00", tz="UTC").timestamp())
    # 2026-07-01 Wednesday 12:00 UTC
    wed = int(__import__("pandas").Timestamp("2026-07-01 12:00:00", tz="UTC").timestamp())
    # 2026-07-03 Friday 12:00 UTC
    fri = int(__import__("pandas").Timestamp("2026-07-03 12:00:00", tz="UTC").timestamp())
    tape = [_row(wed, "buy"), _row(thu, "sell"), _row(fri, "buy"), _row(thu, "buy")]
    assert is_thursday_entry(tape[1]) is True
    assert is_thursday_entry(tape[0]) is False
    out = filter_block_thursday_entries(tape)
    assert len(out) == 2
    assert all(not is_thursday_entry(r) for r in out)
    assert {r["time_unix"] for r in out} == {wed, fri}


def test_non_thursday_rows_unchanged_identity_fields():
    wed = int(__import__("pandas").Timestamp("2026-06-03 08:00:00", tz="UTC").timestamp())
    row = _row(wed, "sell", symbol="GBPUSD=X")
    row["pattern"] = "keep-me"
    out = filter_block_thursday_entries([row])
    assert len(out) == 1
    assert out[0] is row  # same object; no rewrite
    assert out[0]["pattern"] == "keep-me"
    assert out[0]["action"] == "sell"


def test_gate_emits_no_close_actions():
    """Gate only filters; it never injects flatten/close intents."""
    thu = int(__import__("pandas").Timestamp("2026-07-09 15:00:00", tz="UTC").timestamp())
    wed = int(__import__("pandas").Timestamp("2026-07-08 15:00:00", tz="UTC").timestamp())
    tape = [_row(thu), _row(wed)]
    out = filter_block_thursday_entries(tape)
    assert all(str(r.get("action")).lower() in {"buy", "sell", "long", "short"} for r in out)
    assert not any("close" in str(r.get("action", "")).lower() for r in out)
    assert not any("flatten" in str(r.get("action", "")).lower() for r in out)


def test_control_tape_identical_when_no_thursday():
    wed = int(__import__("pandas").Timestamp("2026-06-03 08:00:00", tz="UTC").timestamp())
    fri = int(__import__("pandas").Timestamp("2026-06-05 08:00:00", tz="UTC").timestamp())
    tape = [_row(wed), _row(fri, "sell")]
    out = filter_block_thursday_entries(tape)
    assert out == tape
    assert len(out) == len(tape)


def test_stats():
    thu = int(__import__("pandas").Timestamp("2026-07-02 12:00:00", tz="UTC").timestamp())
    wed = int(__import__("pandas").Timestamp("2026-07-01 12:00:00", tz="UTC").timestamp())
    st = thursday_entry_stats([_row(thu), _row(wed), _row(thu)])
    assert st["n"] == 3
    assert st["thursday_n"] == 2
    assert st["non_thursday_n"] == 1
