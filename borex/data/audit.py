from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from borex.config import CACHE_DIR
from borex.data.manifest import backfill_manifest, read_manifest
from borex.data.store import cache_path, list_cached
from borex.data.repair import detect_timeframe
from borex.data.symbols import to_canonical
from borex.data.timeframes import SUPPORTED_TIMEFRAMES

INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1wk": 10080,
}

INTRADAY_TIMEFRAMES = frozenset({"1m", "15m", "30m", "1h", "4h"})


@dataclass
class AuditReport:
    symbol: str
    timeframe: str
    bars: int
    start: str
    end: str
    status: str  # ok | warn | fail
    duplicate_timestamps: int = 0
    ohlc_violations: int = 0
    invalid_prices: int = 0
    large_gaps: int = 0
    largest_gap_minutes: float = 0.0
    completeness_pct: float | None = None
    expected_bars: int | None = None
    missing_bars_estimate: int | None = None
    manifest_hash: str | None = None
    manifest_source: str | None = None
    detected_timeframe: str | None = None
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "start": self.start,
            "end": self.end,
            "status": self.status,
            "duplicate_timestamps": self.duplicate_timestamps,
            "ohlc_violations": self.ohlc_violations,
            "invalid_prices": self.invalid_prices,
            "large_gaps": self.large_gaps,
            "largest_gap_minutes": round(self.largest_gap_minutes, 1),
            "completeness_pct": self.completeness_pct,
            "expected_bars": self.expected_bars,
            "missing_bars_estimate": self.missing_bars_estimate,
            "manifest_hash": self.manifest_hash,
            "manifest_source": self.manifest_source,
            "detected_timeframe": self.detected_timeframe,
            "issues": self.issues,
        }


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    out.columns = [c.strip().title() for c in out.columns]
    out.index = pd.to_datetime(out.index, utc=True)
    out = out.sort_index()
    return out


def _is_weekend_gap(t0: pd.Timestamp, t1: pd.Timestamp) -> bool:
    """Forex closes Fri ~22:00 UTC, reopens Sun ~22:00 UTC."""
    gap_hours = (t1 - t0).total_seconds() / 3600
    if gap_hours > 74:
        return False
    day = t0.normalize()
    end_day = t1.normalize()
    while day <= end_day:
        if day.weekday() >= 5:
            return True
        day += pd.Timedelta(days=1)
    return False


def _is_benign_gap(delta: pd.Timedelta, timeframe: str) -> bool:
    """Holiday/thin-session gaps that are normal for forex."""
    gap_hours = delta.total_seconds() / 3600
    gap_days = delta.total_seconds() / 86400
    if timeframe in INTRADAY_TIMEFRAMES:
        return gap_hours <= 24
    if timeframe == "1d":
        return gap_days <= 5
    if timeframe == "1wk":
        return gap_days <= 8
    return False


def _is_expected_gap(t0: pd.Timestamp, t1: pd.Timestamp, timeframe: str) -> bool:
    if timeframe in INTRADAY_TIMEFRAMES and _is_weekend_gap(t0, t1):
        return True
    # Daily/weekly: Fri→Mon and holiday gaps are normal for forex dailies.
    if timeframe in ("1d", "1wk"):
        gap_days = (t1.normalize() - t0.normalize()).days
        if t0.weekday() == 4 and t1.weekday() == 0 and gap_days <= 3:
            return True
        if gap_days <= 4:
            return True
    return False


def _count_trading_minutes(start: pd.Timestamp, end: pd.Timestamp) -> int:
    """Approximate forex trading minutes (Mon 00:00 UTC – Fri 23:59 UTC)."""
    if start >= end:
        return 0
    total = 0
    day = start.normalize()
    end_day = end.normalize()
    while day <= end_day:
        if day.weekday() < 5:
            day_start = max(start, day)
            day_end = min(end, day + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))
            if day_end >= day_start:
                total += int((day_end - day_start).total_seconds() // 60) + 1
        day += pd.Timedelta(days=1)
    return total


def _expected_bars(start: pd.Timestamp, end: pd.Timestamp, timeframe: str) -> int:
    interval = INTERVAL_MINUTES.get(timeframe, 60)
    if timeframe in INTRADAY_TIMEFRAMES:
        trading_mins = _count_trading_minutes(start, end)
        return max(1, trading_mins // interval)
    span_days = (end.normalize() - start.normalize()).days + 1
    if timeframe == "1d":
        return max(1, int(span_days * 5 / 7))
    if timeframe == "1wk":
        return max(1, span_days // 7)
    return max(1, int((end - start).total_seconds() // (interval * 60)))


def audit_dataset(
    symbol: str,
    timeframe: str,
    *,
    cache_dir: Path | None = None,
    gap_multiplier: float = 3.0,
) -> AuditReport:
    canonical = to_canonical(symbol)
    path = cache_path(canonical, timeframe, cache_dir)
    if not path.is_file():
        return AuditReport(
            symbol=canonical,
            timeframe=timeframe,
            bars=0,
            start="",
            end="",
            status="fail",
            issues=[f"No cached data at {path}"],
        )

    raw = _normalize_columns(pd.read_parquet(path))
    dupes = int(raw.index.duplicated().sum())
    df = raw[~raw.index.duplicated(keep="last")]
    detected_tf = detect_timeframe(df) if len(df) > 1 else None

    issues: list[str] = []
    ohlc_bad = 0
    invalid = 0
    if {"Open", "High", "Low", "Close"}.issubset(df.columns):
        hi = df[["Open", "Close"]].max(axis=1)
        lo = df[["Open", "Close"]].min(axis=1)
        tol = 1e-6 * df["Close"].abs().clip(lower=1.0)
        ohlc_bad = int((df["High"] + tol < hi).sum() + (df["Low"] - tol > lo).sum())
        invalid = int(
            (df[["Open", "High", "Low", "Close"]] <= 0).any(axis=1).sum()
        )
    else:
        issues.append("Missing OHLC columns")

    if dupes:
        issues.append(f"{dupes} duplicate timestamp(s)")
    if ohlc_bad:
        issues.append(f"{ohlc_bad} OHLC consistency violation(s)")
    if invalid:
        issues.append(f"{invalid} bar(s) with zero/negative price(s)")

    interval_m = INTERVAL_MINUTES.get(timeframe, 60)
    expected_delta = pd.Timedelta(minutes=interval_m)
    gap_threshold = expected_delta * gap_multiplier

    large_gaps = 0
    significant_gaps = 0
    largest_gap_min = 0.0
    if len(df) > 1:
        diffs = df.index.to_series().diff().dropna()
        for i, delta in diffs.items():
            if delta <= gap_threshold:
                continue
            prev_idx = df.index[df.index.get_loc(i) - 1]
            if _is_expected_gap(prev_idx, i, timeframe):
                continue
            large_gaps += 1
            gap_min = delta.total_seconds() / 60
            largest_gap_min = max(largest_gap_min, gap_min)
            if not _is_benign_gap(delta, timeframe):
                significant_gaps += 1

    if large_gaps:
        note = f"{large_gaps} gap(s) > {gap_multiplier}x expected interval"
        if significant_gaps == 0:
            note += " (weekends/holidays only)"
        issues.append(note)

    expected = _expected_bars(df.index.min(), df.index.max(), timeframe)
    missing_est = max(0, expected - len(df))
    completeness = round(100.0 * len(df) / expected, 1) if expected else None
    if completeness is not None and completeness > 100.0:
        completeness = 100.0
        missing_est = 0
    if completeness is not None and completeness < 90.0 and timeframe in INTRADAY_TIMEFRAMES:
        if timeframe == "1m" and completeness >= 45.0:
            issues.append(
                f"Completeness ~{completeness}% (Yahoo ~30-day 1m limit; use Dukascopy for more)"
            )
        else:
            issues.append(f"Completeness ~{completeness}% ({missing_est} bars missing est.)")

    if detected_tf and detected_tf != timeframe:
        issues.append(f"Timeframe mismatch: file is {timeframe} but bars are ~{detected_tf}")

    manifest = read_manifest(canonical, timeframe, cache_dir)
    if not manifest:
        issues.append("No dataset manifest (run audit --repair-manifests)")

    status = "ok"
    if dupes or ohlc_bad or invalid or (detected_tf and detected_tf != timeframe):
        status = "fail"
    elif (significant_gaps >= 2 or (timeframe in ("1d", "1wk") and significant_gaps >= 1)) or not manifest:
        status = "warn"
    elif completeness is not None and completeness < 90.0 and timeframe in INTRADAY_TIMEFRAMES:
        if timeframe == "1m" and completeness >= 45.0:
            status = "ok"  # expected Yahoo 1m limit
        else:
            status = "warn"

    return AuditReport(
        symbol=canonical,
        timeframe=timeframe,
        bars=len(df),
        start=df.index.min().isoformat(),
        end=df.index.max().isoformat(),
        status=status,
        duplicate_timestamps=dupes,
        ohlc_violations=ohlc_bad,
        invalid_prices=invalid,
        large_gaps=large_gaps,
        largest_gap_minutes=largest_gap_min,
        completeness_pct=completeness,
        expected_bars=expected,
        missing_bars_estimate=missing_est,
        manifest_hash=manifest.get("content_hash") if manifest else None,
        manifest_source=manifest.get("source") if manifest else None,
        detected_timeframe=detected_tf,
        issues=issues,
    )


def audit_all(
    *,
    cache_dir: Path | None = None,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> list[AuditReport]:
    root = cache_dir or CACHE_DIR
    if symbols and timeframes:
        pairs = [(to_canonical(s), tf) for s in symbols for tf in timeframes]
    else:
        cached = list_cached(cache_dir)
        pairs = [(row["symbol"], row["timeframe"]) for row in cached]

    reports: list[AuditReport] = []
    for sym, tf in pairs:
        if tf not in SUPPORTED_TIMEFRAMES:
            continue
        reports.append(audit_dataset(sym, tf, cache_dir=root))
    return reports


def repair_manifests(*, cache_dir: Path | None = None) -> list[dict]:
    """Backfill manifests for all cached parquet files."""
    root = cache_dir or CACHE_DIR
    repaired: list[dict] = []
    for row in list_cached(root):
        sym = row["symbol"]
        tf = row["timeframe"]
        manifest = backfill_manifest(sym, tf, cache_dir=root)
        if manifest:
            repaired.append({"symbol": sym, "timeframe": tf, "hash": manifest["content_hash"]})
    return repaired
