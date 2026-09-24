"""Immutable Theory event store + restart-safe execution index."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def new_theory_event_id() -> str:
    return f"tev_{uuid.uuid4().hex[:16]}"


def new_execution_id() -> str:
    return f"exe_{uuid.uuid4().hex[:16]}"


@dataclass
class ImmutableJsonlStore:
    """Append-only JSONL. Refuses overwrite of existing theory_event_id."""

    path: Path
    _seen_ids: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            for row in self.iter_rows():
                tid = row.get("theory_event_id") or row.get("execution_id")
                if tid:
                    self._seen_ids.add(str(tid))

    def iter_rows(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    def append(self, row: dict[str, Any], *, id_key: str) -> None:
        key = str(row.get(id_key) or "")
        if not key:
            raise ValueError(f"missing {id_key}")
        if key in self._seen_ids:
            raise ValueError(f"immutable refusal: duplicate {id_key}={key}")
        # Theory events must never be mutated after write
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        self._seen_ids.add(key)

    def has_id(self, key: str) -> bool:
        return key in self._seen_ids

    def find_by(self, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.iter_rows():
            if row.get(field) == value:
                return row
        return None


def build_theory_event(
    *,
    run_id: str,
    git_commit: str | None,
    signal: dict[str, Any],
    quote: dict[str, Any],
    expected_entry: float,
    expected_sl: float,
    expected_tp: float,
    expected_risk_usd: float,
    distance_meta: dict[str, Any],
    config: dict[str, Any],
    candle_sequence: list[dict[str, Any]],
    theory_event_id: str | None = None,
) -> dict[str, Any]:
    """Build immutable Theory event BEFORE broker submission."""
    tid = theory_event_id or new_theory_event_id()
    return {
        "theory_event_id": tid,
        "run_id": run_id,
        "git_commit": git_commit,
        "recorded_at_utc": _iso(),
        "strategy_name": signal.get("strategy_name"),
        "strategy_version": signal.get("strategy_version"),
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "side": signal.get("side"),
        "signal_timestamp_utc": signal.get("signal_timestamp_utc"),
        "source_candle_timestamp_utc": signal.get("source_candle_timestamp_utc"),
        "candle_ohlc": signal.get("source_candle"),
        "prev_candle_ohlc": signal.get("prev_candle"),
        "timeframe": signal.get("timeframe"),
        "pattern": signal.get("pattern"),
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
        "spread_price": quote.get("spread_price"),
        "spread_pips": quote.get("spread_pips"),
        "tick_timestamp_unix_raw": quote.get("tick_time_unix_raw"),
        "tick_age_seconds": quote.get("tick_age_seconds"),
        "expected_entry": expected_entry,
        "expected_entry_side": signal.get("expected_entry_side"),
        "expected_sl": expected_sl,
        "expected_tp": expected_tp,
        "expected_risk_usd": expected_risk_usd,
        "expected_exit_model": "BROKER_SL_OR_TP",
        "sl_trigger_side": distance_meta.get("sl_trigger_side"),
        "tp_trigger_side": distance_meta.get("tp_trigger_side"),
        "distance_selection": distance_meta.get("distance_selection"),
        "strategy_parameters": signal.get("parameters"),
        "config": config,
        "candle_sequence": candle_sequence,
        "execution_id": None,  # filled only by linking record; Theory row itself stays immutable
        "immutable": True,
    }


def link_execution_record(
    *,
    theory_event_id: str,
    execution_id: str,
    comment: str,
    order_result: dict[str, Any],
    position_ticket: int | None,
    actual_sl: float | None,
    actual_tp: float | None,
    entry_deal: dict[str, Any] | None,
    mode: str,
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "theory_event_id": theory_event_id,
        "recorded_at_utc": _iso(),
        "mode": mode,
        "comment": comment,
        "order_result": order_result,
        "position_ticket": position_ticket,
        "actual_sl": actual_sl,
        "actual_tp": actual_tp,
        "entry_deal": entry_deal,
    }


def map_theory_to_execution(
    *,
    theory_event_id: str | None,
    execution_theory_event_id: str | None,
    broker_comment_theory_id: str | None = None,
) -> dict[str, Any]:
    """Direct ID mapping. Never nearest-timestamp when IDs exist."""
    if not theory_event_id:
        return {
            "mapping_status": "UNMAPPED",
            "confidence": "LOW",
            "detail": "missing_theory_event_id",
        }
    if not execution_theory_event_id:
        return {
            "mapping_status": "UNMAPPED",
            "confidence": "LOW",
            "detail": "execution_missing_theory_link",
        }
    if theory_event_id != execution_theory_event_id:
        return {
            "mapping_status": "MAPPING_ERROR",
            "confidence": "LOW",
            "detail": (
                f"theory_event_id={theory_event_id} != "
                f"execution.theory_event_id={execution_theory_event_id}"
            ),
        }
    if broker_comment_theory_id and broker_comment_theory_id != theory_event_id[-8:]:
        # comment carries short suffix only; mismatch is soft warning
        return {
            "mapping_status": "EXACT_MATCH",
            "confidence": "MEDIUM",
            "detail": "id_match_comment_suffix_mismatch",
        }
    return {
        "mapping_status": "EXACT_MATCH",
        "confidence": "HIGH",
        "detail": "direct_theory_event_id",
    }
