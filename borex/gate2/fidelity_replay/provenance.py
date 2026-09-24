"""Load and hash source-run artifacts; stitch run-embedded 1m candles."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from borex.gate2.micro_fidelity.candles import MinuteCandle


SOURCE_RUN_ID = "g2m_20260924_001929"
DEFAULT_SOURCE_REL = Path("data/runs/gate2_micro_fidelity/runs") / SOURCE_RUN_ID


def parse_utc(ts: str | datetime | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        t = ts
    else:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candle_dict_to_minute(c: dict[str, Any]) -> MinuteCandle:
    ts_raw = c.get("candle_open_time_utc") or c.get("timestamp_utc")
    return MinuteCandle(
        timestamp_utc=parse_utc(ts_raw),  # type: ignore[arg-type]
        open=float(c["open"]),
        high=float(c["high"]),
        low=float(c["low"]),
        close=float(c["close"]),
        tick_count=int(c.get("tick_volume") or c.get("tick_count") or 0),
        completed=bool(c.get("completed", True)),
    )


@dataclass
class SourceBundle:
    """Immutable view of the Level-4 source run used for Level-5 replay."""

    source_run_id: str
    source_dir: Path
    theory_events: list[dict[str, Any]]
    executions: list[dict[str, Any]]
    reconciliation_rows: list[dict[str, Any]]
    theory_by_id: dict[str, dict[str, Any]]
    execution_by_theory_id: dict[str, dict[str, Any]]
    # symbol -> timestamp_iso -> candle dict
    stitched_m1: dict[str, dict[str, dict[str, Any]]]
    file_hashes: dict[str, str]
    data_notes: list[str] = field(default_factory=list)
    substitution_occurred: bool = False
    m1_archive_present: bool = False
    m1_data_tier: str = "theory_embedded_stitch"  # or continuous_m1_archive

    def m1_bars_sorted(self, symbol: str) -> list[MinuteCandle]:
        bars = self.stitched_m1.get(symbol) or {}
        out = [candle_dict_to_minute(bars[k]) for k in sorted(bars)]
        return out


def resolve_source_dir(repo_root: Path, source_run_id: str = SOURCE_RUN_ID) -> Path:
    path = repo_root / "data" / "runs" / "gate2_micro_fidelity" / "runs" / source_run_id
    if not path.is_dir():
        raise FileNotFoundError(f"source run directory missing: {path}")
    return path


def _load_continuous_m1_archive(source_dir: Path) -> dict[str, dict[str, dict[str, Any]]] | None:
    archive_dir = source_dir / "m1_archive"
    if not archive_dir.is_dir():
        return None
    candle_files = list(archive_dir.glob("candles_*.jsonl"))
    if not candle_files:
        return None
    stitched: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for path in candle_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            sym = str(row.get("symbol") or "")
            ts = str(row.get("candle_open_time_utc") or "")
            if not sym or not ts:
                continue
            # Normalize to MinuteCandle-compatible keys for replay
            stitched[sym][ts] = {
                "timestamp_utc": ts,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "tick_count": row.get("tick_volume") or row.get("tick_count") or 0,
                "completed": True,
                "data_source": row.get("data_source"),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "spread_pips": row.get("spread_pips"),
            }
    return dict(stitched) if stitched else None


def load_source_bundle(
    repo_root: Path,
    *,
    source_run_id: str = SOURCE_RUN_ID,
) -> SourceBundle:
    source_dir = resolve_source_dir(repo_root, source_run_id)
    required = (
        "theory_events.jsonl",
        "broker_executions.jsonl",
        "trade_reconciliation.json",
        "manifest.json",
    )
    missing = [n for n in required if not (source_dir / n).is_file()]
    if missing:
        raise FileNotFoundError(f"source run missing artifacts: {missing}")

    notes: list[str] = []
    hashes: dict[str, str] = {}
    for name in list(required) + ["fidelity_report.json", "early_diagnostic.json"]:
        p = source_dir / name
        if p.is_file():
            hashes[name] = sha256_file(p)

    theory_events: list[dict[str, Any]] = []
    for line in (source_dir / "theory_events.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            theory_events.append(json.loads(line))

    executions: list[dict[str, Any]] = []
    for line in (source_dir / "broker_executions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            executions.append(json.loads(line))

    recon = json.loads((source_dir / "trade_reconciliation.json").read_text(encoding="utf-8"))
    rows = list(recon.get("rows") or [])

    theory_by_id = {str(t["theory_event_id"]): t for t in theory_events}
    execution_by_theory_id = {
        str(e["theory_event_id"]): e for e in executions if e.get("theory_event_id")
    }

    continuous = _load_continuous_m1_archive(source_dir)
    if continuous is not None:
        stitched = continuous
        m1_archive_present = True
        m1_data_tier = "continuous_m1_archive"
        notes.extend(
            [
                "Continuous M1 archive present (Level 5b): m1_archive/candles_*.jsonl.",
                "Post-signal exit paths use continuous archive (exact runtime MT5 fetch_bars).",
                "No Yahoo/MT5 provider mixing; substitution_occurred=false.",
            ]
        )
        for p in (source_dir / "m1_archive").glob("**/*"):
            if p.is_file():
                hashes[f"m1_archive/{p.relative_to(source_dir / 'm1_archive').as_posix()}"] = (
                    sha256_file(p)
                )
    else:
        stitched = defaultdict(dict)
        for t in theory_events:
            sym = str(t.get("symbol") or "")
            for c in t.get("candle_sequence") or []:
                ts = str(c.get("timestamp_utc") or "")
                if ts:
                    stitched[sym][ts] = dict(c)
            for key in ("candle_ohlc", "prev_candle_ohlc"):
                c = t.get(key)
                if isinstance(c, dict) and c.get("timestamp_utc"):
                    stitched[sym][str(c["timestamp_utc"])] = dict(c)
        stitched = dict(stitched)
        m1_archive_present = False
        m1_data_tier = "theory_embedded_stitch"
        notes.extend(
            [
                "No standalone continuous M1 archive exists for this run.",
                "M1 inputs are Theory-embedded candle_ohlc / prev_candle_ohlc / candle_sequence only.",
                "Post-signal exit-path bars are stitched from later Theory events' sequences "
                "in the same run (exact runtime captures; not Yahoo/MT5 re-download).",
                "No external market-data substitution is performed.",
                "Broker exit timestamps may be absent from older trade_reconciliation.json.",
            ]
        )

    return SourceBundle(
        source_run_id=source_run_id,
        source_dir=source_dir,
        theory_events=theory_events,
        executions=executions,
        reconciliation_rows=rows,
        theory_by_id=theory_by_id,
        execution_by_theory_id=execution_by_theory_id,
        stitched_m1=stitched,
        file_hashes=hashes,
        data_notes=notes,
        substitution_occurred=False,
        m1_archive_present=m1_archive_present,
        m1_data_tier=m1_data_tier,
    )


def causal_candles_for_theory(theory: dict[str, Any]) -> list[MinuteCandle]:
    """Candles permitted at Theory signal time: this event's sequence only.

    Fail-closed: never mixes in later events' bars for signal reproduction.
    Latest permitted bar open = source_candle_timestamp_utc.
    """
    source_ts = parse_utc(theory.get("source_candle_timestamp_utc"))
    seq = theory.get("candle_sequence") or []
    out: list[MinuteCandle] = []
    for c in seq:
        ts = parse_utc(c.get("timestamp_utc"))
        if ts is None or source_ts is None:
            continue
        if ts <= source_ts:
            out.append(candle_dict_to_minute(c))
    out.sort(key=lambda x: x.timestamp_utc)
    # Deduplicate by timestamp
    dedup: dict[datetime, MinuteCandle] = {c.timestamp_utc: c for c in out}
    return [dedup[k] for k in sorted(dedup)]


def post_signal_path(
    bundle: SourceBundle,
    *,
    symbol: str,
    signal_ts: datetime,
) -> tuple[list[MinuteCandle], str | None]:
    """Return continuous completed M1 bars with open >= signal_ts from stitch.

    Returns (bars, insufficiency_reason). insufficiency_reason is set when the
    first required bar (signal minute) is missing — caller still receives any
    later contiguous segment starting at the first available bar for diagnostics,
    but should treat missing first bar / gaps before a hit as INSUFFICIENT.
    """
    all_bars = bundle.m1_bars_sorted(symbol)
    start = signal_ts
    candidates = [c for c in all_bars if c.timestamp_utc >= start]
    if not candidates:
        return [], "no_post_signal_m1_in_run_artifacts"

    # Require the bar that opens at signal_ts (forming bar at entry) when present;
    # if missing, fail closed for continuous path from entry.
    if candidates[0].timestamp_utc != start:
        return candidates, f"missing_entry_minute_bar_expected_{start.isoformat()}_got_{candidates[0].timestamp_utc.isoformat()}"

    continuous: list[MinuteCandle] = [candidates[0]]
    expect = start + timedelta(minutes=1)
    for c in candidates[1:]:
        if c.timestamp_utc != expect:
            # truncate at first gap — remaining bars are not a continuous path
            break
        continuous.append(c)
        expect = expect + timedelta(minutes=1)
    return continuous, None
