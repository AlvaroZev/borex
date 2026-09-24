"""Continuous M1 candle archive for Gate 2 Level 5b exact-replay evidence.

Records every completed 1m candle for all experiment symbols throughout a run,
independent of whether trades are open. Trade windows index into this immutable
archive (no per-trade candle duplication).

Fail closed on provider substitution: only ingest bars from the same source that
drives Theory (MT5 fetch_bars via the Mirror client). Gaps are recorded explicitly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from borex.gate2.micro_fidelity.candles import MinuteCandle, floor_minute_utc

PRE_SIGNAL_BUFFER_MINUTES = 20
POST_EXIT_TAIL_MINUTES = 2
DATA_SOURCE_MT5_FETCH_BARS = "mt5_fetch_bars"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def iso_utc(dt: datetime) -> str:
    """Canonical UTC ISO-8601 with timezone offset (+00:00)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


@dataclass
class M1ArchiveStats:
    candles_appended: int = 0
    gaps_recorded: int = 0
    duplicates_skipped: int = 0


@dataclass
class ContinuousM1Archive:
    """Append-only per-symbol M1 JSONL archive + trade window index."""

    run_dir: Path
    symbols: Sequence[str]
    data_source: str = DATA_SOURCE_MT5_FETCH_BARS
    server_offset_seconds: int | None = None
    pre_signal_buffer_minutes: int = PRE_SIGNAL_BUFFER_MINUTES
    post_exit_tail_minutes: int = POST_EXIT_TAIL_MINUTES
    _last_open_utc: dict[str, datetime] = field(default_factory=dict, init=False)
    _known_opens: dict[str, set[str]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.archive_dir = Path(self.run_dir) / "m1_archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.gaps_path = self.archive_dir / "gaps.jsonl"
        self.windows_path = self.archive_dir / "trade_windows.jsonl"
        self.manifest_path = self.archive_dir / "archive_manifest.json"
        for sym in self.symbols:
            self._known_opens[sym] = set()
            self._hydrate_known(sym)
        self._write_manifest(event="init")

    def candles_path(self, symbol: str) -> Path:
        safe = symbol.replace("=", "_").replace("/", "_")
        return self.archive_dir / f"candles_{safe}.jsonl"

    def _hydrate_known(self, symbol: str) -> None:
        path = self.candles_path(symbol)
        if not path.is_file():
            return
        last: datetime | None = None
        known = self._known_opens.setdefault(symbol, set())
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            ts = str(row.get("candle_open_time_utc") or "")
            if not ts:
                continue
            known.add(ts)
            dt = parse_utc(ts)
            if dt is not None and (last is None or dt > last):
                last = dt
        if last is not None:
            self._last_open_utc[symbol] = last

    def _append_jsonl(self, path: Path, row: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def _write_manifest(self, *, event: str, extra: dict[str, Any] | None = None) -> None:
        payload = {
            "updated_at_utc": iso_utc(_utc_now()),
            "event": event,
            "data_source": self.data_source,
            "server_offset_seconds": self.server_offset_seconds,
            "symbols": list(self.symbols),
            "pre_signal_buffer_minutes": self.pre_signal_buffer_minutes,
            "post_exit_tail_minutes": self.post_exit_tail_minutes,
            "candle_files": {
                sym: self.candles_path(sym).name for sym in self.symbols
            },
            "notes": [
                "Continuous completed M1 only; forming bars never archived.",
                "No Yahoo/MT5 provider mixing; fail closed on empty fetch.",
                "Trade windows index into this archive; candles are not duplicated per trade.",
            ],
        }
        if extra:
            payload.update(extra)
        self.manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def record_gap(
        self,
        *,
        symbol: str,
        expected_open: datetime,
        got_open: datetime | None,
        missing_minutes: int,
        reason: str,
    ) -> None:
        self._append_jsonl(
            self.gaps_path,
            {
                "type": "gap",
                "symbol": symbol,
                "timeframe": "1m",
                "expected_open_utc": iso_utc(expected_open),
                "got_open_utc": iso_utc(got_open) if got_open else None,
                "missing_minutes": int(missing_minutes),
                "reason": reason,
                "recorded_at_utc": iso_utc(_utc_now()),
                "data_source": self.data_source,
            },
        )

    def candle_record(
        self,
        candle: MinuteCandle,
        *,
        symbol: str,
        quote: dict[str, Any] | None = None,
        fetch_failed: bool = False,
    ) -> dict[str, Any]:
        bid = ask = spread = None
        if quote:
            if quote.get("bid") is not None:
                bid = float(quote["bid"])
            if quote.get("ask") is not None:
                ask = float(quote["ask"])
            if quote.get("spread_pips") is not None:
                spread = float(quote["spread_pips"])
        return {
            "symbol": symbol,
            "timeframe": "1m",
            "candle_open_time_utc": iso_utc(floor_minute_utc(candle.timestamp_utc)),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "tick_volume": int(candle.tick_count),
            "bid": bid,
            "ask": ask,
            "spread_pips": spread,
            "data_source": self.data_source,
            "capture_time_utc": iso_utc(_utc_now()),
            "server_offset_seconds": self.server_offset_seconds,
            "fetch_failed": bool(fetch_failed),
        }

    def ingest_completed(
        self,
        symbol: str,
        candles: Sequence[MinuteCandle],
        *,
        quote: dict[str, Any] | None = None,
        fetch_failed: bool = False,
    ) -> M1ArchiveStats:
        """Append new completed candles in order; record gaps; skip duplicates."""
        stats = M1ArchiveStats()
        if fetch_failed:
            self.record_gap(
                symbol=symbol,
                expected_open=self._last_open_utc.get(symbol, floor_minute_utc(_utc_now())),
                got_open=None,
                missing_minutes=0,
                reason="fetch_failed_fail_closed",
            )
            stats.gaps_recorded += 1
            return stats

        known = self._known_opens.setdefault(symbol, set())
        ordered = sorted(
            [c for c in candles if c.completed],
            key=lambda c: c.timestamp_utc,
        )
        for c in ordered:
            open_ts = floor_minute_utc(c.timestamp_utc)
            key = iso_utc(open_ts)
            if key in known:
                stats.duplicates_skipped += 1
                continue
            last = self._last_open_utc.get(symbol)
            if last is not None:
                expect = last + timedelta(minutes=1)
                if open_ts > expect:
                    missing = int((open_ts - expect).total_seconds() // 60)
                    self.record_gap(
                        symbol=symbol,
                        expected_open=expect,
                        got_open=open_ts,
                        missing_minutes=max(1, missing),
                        reason="missing_completed_m1_between_persisted_bars",
                    )
                    stats.gaps_recorded += 1
                elif open_ts < last:
                    # Out-of-order older bar — skip (do not rewrite history)
                    stats.duplicates_skipped += 1
                    continue
            row = self.candle_record(c, symbol=symbol, quote=quote, fetch_failed=False)
            self._append_jsonl(self.candles_path(symbol), row)
            known.add(key)
            self._last_open_utc[symbol] = open_ts
            stats.candles_appended += 1
        return stats

    def open_trade_window(
        self,
        *,
        theory_event_id: str,
        execution_id: str | None,
        broker_ticket: int | None,
        symbol: str,
        signal_time_utc: datetime | str,
        source_candle_utc: datetime | str | None,
    ) -> dict[str, Any]:
        sig = parse_utc(signal_time_utc)
        assert sig is not None
        src = parse_utc(source_candle_utc)
        start = sig - timedelta(minutes=int(self.pre_signal_buffer_minutes))
        row = {
            "theory_event_id": theory_event_id,
            "execution_id": execution_id,
            "broker_ticket": broker_ticket,
            "symbol": symbol,
            "signal_time_utc": iso_utc(sig),
            "source_candle_utc": iso_utc(src) if src else None,
            "window_start_utc": iso_utc(start),
            "entry_observed_utc": iso_utc(sig),
            "exit_observed_utc": None,
            "window_end_utc": None,
            "status": "OPEN",
            "pre_signal_buffer_minutes": self.pre_signal_buffer_minutes,
            "post_exit_tail_minutes": self.post_exit_tail_minutes,
            "recorded_at_utc": iso_utc(_utc_now()),
        }
        self._append_jsonl(self.windows_path, row)
        return row

    def close_trade_window(
        self,
        *,
        theory_event_id: str,
        exit_observed_utc: datetime | str,
        broker_ticket: int | None = None,
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        exit_ts = parse_utc(exit_observed_utc)
        assert exit_ts is not None
        end = exit_ts + timedelta(minutes=int(self.post_exit_tail_minutes))
        row = {
            "theory_event_id": theory_event_id,
            "execution_id": execution_id,
            "broker_ticket": broker_ticket,
            "exit_observed_utc": iso_utc(exit_ts),
            "window_end_utc": iso_utc(end),
            "status": "CLOSED",
            "recorded_at_utc": iso_utc(_utc_now()),
            "update": "close",
        }
        self._append_jsonl(self.windows_path, row)
        return row

    def iter_symbol_candles(self, symbol: str) -> Iterable[dict[str, Any]]:
        path = self.candles_path(symbol)
        if not path.is_file():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)

    def path_for_window(
        self,
        *,
        symbol: str,
        start_utc: datetime | str,
        end_utc: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        start = parse_utc(start_utc)
        end = parse_utc(end_utc) if end_utc else None
        assert start is not None
        out: list[dict[str, Any]] = []
        for row in self.iter_symbol_candles(symbol):
            ts = parse_utc(row.get("candle_open_time_utc"))
            if ts is None:
                continue
            if ts < start:
                continue
            if end is not None and ts > end:
                break
            out.append(row)
        return out

    def continuity_report(self, symbol: str) -> dict[str, Any]:
        rows = list(self.iter_symbol_candles(symbol))
        if not rows:
            return {
                "symbol": symbol,
                "n": 0,
                "continuous": True,
                "gaps": [],
                "first": None,
                "last": None,
            }
        gaps: list[dict[str, Any]] = []
        prev = parse_utc(rows[0]["candle_open_time_utc"])
        assert prev is not None
        for row in rows[1:]:
            cur = parse_utc(row["candle_open_time_utc"])
            assert cur is not None
            expect = prev + timedelta(minutes=1)
            if cur != expect:
                gaps.append(
                    {
                        "after": iso_utc(prev),
                        "expected": iso_utc(expect),
                        "got": iso_utc(cur),
                        "missing_minutes": int((cur - expect).total_seconds() // 60),
                    }
                )
            prev = cur
        return {
            "symbol": symbol,
            "n": len(rows),
            "continuous": len(gaps) == 0,
            "gaps": gaps,
            "first": rows[0]["candle_open_time_utc"],
            "last": rows[-1]["candle_open_time_utc"],
        }

    def finalize_summary(self) -> dict[str, Any]:
        reports = {sym: self.continuity_report(sym) for sym in self.symbols}
        summary = {
            "symbols": reports,
            "total_candles": sum(int(r["n"]) for r in reports.values()),
            "any_gaps": any(not r["continuous"] for r in reports.values()),
            "finalized_at_utc": iso_utc(_utc_now()),
        }
        (self.archive_dir / "continuity_report.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        self._write_manifest(event="finalize", extra={"continuity": summary})
        return summary


def load_trade_windows(archive_dir: Path) -> dict[str, dict[str, Any]]:
    """Latest state per theory_event_id from append-only trade_windows.jsonl."""
    path = archive_dir / "trade_windows.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        teid = str(row.get("theory_event_id") or "")
        if not teid:
            continue
        cur = out.get(teid, {})
        cur.update({k: v for k, v in row.items() if v is not None})
        out[teid] = cur
    return out
