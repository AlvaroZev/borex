"""Research-only Thursday entry exclusion gate.

Does NOT modify alexg8 / production strategy. Filters decision-tape intents
whose signal/entry timestamp falls on Thursday (UTC, Mon=0 … Thu=3).

Existing positions are untouched: this module never emits flatten/close
actions; it only drops new entry intents.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

THURSDAY = 3  # pandas / Python weekday: Monday=0 … Sunday=6


def _ts_utc(value: Any) -> pd.Timestamp:
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.replace(".", "", 1).isdigit()
    ):
        t = pd.Timestamp(float(value), unit="s", tz="UTC")
    else:
        t = pd.Timestamp(value)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
    return t


def entry_timestamp(row: dict[str, Any]) -> pd.Timestamp:
    """Resolve intent time from a decision-tape row."""
    if row.get("time_unix") is not None:
        return _ts_utc(row["time_unix"])
    for key in ("entry_time", "time", "timestamp"):
        if row.get(key) is not None:
            return _ts_utc(row[key])
    raise ValueError("decision row missing time_unix/entry_time")


def is_thursday_entry(row: dict[str, Any]) -> bool:
    return int(entry_timestamp(row).dayofweek) == THURSDAY


def filter_block_thursday_entries(tape: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop NEW entry intents dated Thursday UTC; preserve all other rows."""
    return [row for row in tape if not is_thursday_entry(row)]


def thursday_entry_stats(tape: list[dict[str, Any]]) -> dict[str, Any]:
    thu = sum(1 for r in tape if is_thursday_entry(r))
    return {
        "n": len(tape),
        "thursday_n": thu,
        "non_thursday_n": len(tape) - thu,
        "thursday_share": (thu / len(tape)) if tape else None,
    }
