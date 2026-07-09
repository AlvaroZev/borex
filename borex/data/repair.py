from __future__ import annotations

from pathlib import Path

import pandas as pd

from borex.config import CACHE_DIR
from borex.data.manifest import file_content_hash
from borex.data.store import (
    _normalize_df,
    cache_path,
    is_cached,
    legacy_year_chunk_path,
    list_year_chunk_years,
    load_ohlcv,
    save_ohlcv,
    save_year_chunk,
    year_chunk_path,
)
from borex.data.symbols import FOREX_PAIRS, cache_dir_name, to_canonical
from borex.data.timeframes import SUPPORTED_TIMEFRAMES, TIMEFRAMES

TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1wk": 10080,
}

TF_BUILD_ORDER = ["1m", "15m", "30m", "1h", "4h", "1d", "1wk"]


def detect_timeframe(df: pd.DataFrame) -> str | None:
    if len(df) < 2:
        return None
    med_min = df.index.to_series().diff().dropna().median().total_seconds() / 60
    if med_min <= 0:
        return None
    best = min(TF_MINUTES, key=lambda tf: abs(TF_MINUTES[tf] - med_min))
    err = abs(TF_MINUTES[best] - med_min) / TF_MINUTES[best]
    return best if err <= 0.35 else None


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = TIMEFRAMES[timeframe].pandas_rule
    out = df.resample(rule, label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return _normalize_df(out.dropna(subset=["Open", "High", "Low", "Close"]))


def _has_valid_ohlc(df: pd.DataFrame) -> bool:
    if df.empty:
        return False
    cols = ["Open", "High", "Low", "Close"]
    if not set(cols).issubset(df.columns):
        return False
    return bool(df[cols].notna().all(axis=1).any())


def _read_parquet(path: Path) -> pd.DataFrame:
    df = _normalize_df(pd.read_parquet(path))
    if not _has_valid_ohlc(df):
        raise ValueError(f"Invalid OHLC in {path}")
    return df


def migrate_legacy_year_chunks(symbol: str, *, cache_dir: Path | None = None) -> list[str]:
    """Move flat years/{year}.parquet into years/{timeframe}/{year}.parquet."""
    sym = to_canonical(symbol)
    sym_dir = cache_path(sym, "1d", cache_dir).parent
    legacy_dir = sym_dir / "years"
    if not legacy_dir.is_dir():
        return []

    moved: list[str] = []
    for path in sorted(legacy_dir.glob("*.parquet")):
        try:
            year = int(path.stem)
        except ValueError:
            continue
        df = _read_parquet(path)
        tf = detect_timeframe(df)
        if not tf:
            continue
        dest = year_chunk_path(sym, tf, year, cache_dir)
        if dest.is_file():
            path.unlink()
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(dest)
        path.unlink()
        moved.append(f"{sym} {year} -> {tf}")
    return moved


def _duplicate_timeframe_groups(symbol: str, cache_dir: Path | None = None) -> dict[str, list[str]]:
    """Map content hash -> timeframes sharing identical parquet (corrupt)."""
    sym = to_canonical(symbol)
    by_hash: dict[str, list[str]] = {}
    for tf in SUPPORTED_TIMEFRAMES:
        path = cache_path(sym, tf, cache_dir)
        if not path.is_file():
            continue
        h = file_content_hash(path)
        by_hash.setdefault(h, []).append(tf)
    return {h: tfs for h, tfs in by_hash.items() if len(tfs) > 1}


def _merge_year_chunks(symbol: str, timeframe: str, cache_dir: Path | None = None) -> pd.DataFrame | None:
    years = list_year_chunk_years(symbol, timeframe, cache_dir)
    if not years:
        return None
    frames: list[pd.DataFrame] = []
    for y in years:
        p = year_chunk_path(symbol, timeframe, y, cache_dir)
        try:
            frames.append(_read_parquet(p))
        except ValueError:
            continue
    if not frames:
        return None
    merged = pd.concat(frames).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged


def _load_source_for_tf(
    symbol: str,
    target_tf: str,
    built: dict[str, pd.DataFrame],
    *,
    cache_dir: Path | None = None,
) -> pd.DataFrame | None:
    target_m = TF_MINUTES[target_tf]
    merged = _merge_year_chunks(symbol, target_tf, cache_dir)
    if merged is not None and not merged.empty:
        detected = detect_timeframe(merged)
        if detected == target_tf or detected is None:
            return merged

    for src_tf in TF_BUILD_ORDER:
        if TF_MINUTES[src_tf] >= target_m:
            continue
        if src_tf in built and not built[src_tf].empty:
            return resample_ohlcv(built[src_tf], target_tf)
        if list_year_chunk_years(symbol, src_tf, cache_dir):
            src_df = _merge_year_chunks(symbol, src_tf, cache_dir)
            if src_df is not None and _has_valid_ohlc(src_df):
                out = resample_ohlcv(src_df, target_tf)
                if _has_valid_ohlc(out):
                    return out
        path = cache_path(symbol, src_tf, cache_dir)
        if path.is_file():
            try:
                src_df = _read_parquet(path)
                if detect_timeframe(src_df) == src_tf:
                    out = resample_ohlcv(src_df, target_tf)
                    if _has_valid_ohlc(out):
                        return out
            except ValueError:
                pass
    return None


def repair_symbol_timeframe(
    symbol: str,
    timeframe: str,
    *,
    cache_dir: Path | None = None,
    built: dict[str, pd.DataFrame] | None = None,
    force: bool = False,
) -> dict:
    sym = to_canonical(symbol)
    tf = timeframe
    built = built if built is not None else {}

    if tf == "1wk":
        if "1d" not in built and is_cached(sym, "1d", cache_dir):
            try:
                daily = load_ohlcv(sym, "1d", cache_dir)
                if _has_valid_ohlc(daily):
                    built["1d"] = daily
            except ValueError:
                pass
        if "1d" in built and _has_valid_ohlc(built["1d"]):
            df = resample_ohlcv(built["1d"], "1wk")
            path = save_ohlcv(df, sym, "1wk", cache_dir, source="dukascopy")
            built["1wk"] = df
            return {"symbol": sym, "timeframe": tf, "bars": len(df), "status": "ok", "path": str(path)}
        return {"symbol": sym, "timeframe": tf, "status": "skipped", "reason": "no daily data"}

    path = cache_path(sym, tf, cache_dir)
    if not force and path.is_file():
        try:
            existing = _read_parquet(path)
            detected = detect_timeframe(existing)
            if _has_valid_ohlc(existing) and (detected is None or detected == tf):
                built[tf] = existing
                return {"symbol": sym, "timeframe": tf, "bars": len(existing), "status": "skipped", "reason": "already valid"}
            if _has_valid_ohlc(existing) and detected and TF_MINUTES[detected] < TF_MINUTES[tf]:
                df = resample_ohlcv(existing, tf)
                if _has_valid_ohlc(df):
                    save_ohlcv(df, sym, tf, cache_dir, source="dukascopy")
                    built[tf] = df
                    return {"symbol": sym, "timeframe": tf, "bars": len(df), "status": "ok", "source": "resampled_existing"}
        except ValueError:
            pass

    dup_groups = _duplicate_timeframe_groups(sym, cache_dir)
    corrupt_tfs = {t for tfs in dup_groups.values() for t in tfs}

    df: pd.DataFrame | None = None
    source = "merged"

    if tf in corrupt_tfs or force or not path.is_file():
        merged = _merge_year_chunks(sym, tf, cache_dir)
        if merged is not None and _has_valid_ohlc(merged):
            detected = detect_timeframe(merged)
            if detected is None or detected == tf:
                df = merged
            elif detected and TF_MINUTES[detected] < TF_MINUTES[tf]:
                df = resample_ohlcv(merged, tf)
                source = f"resampled_from_chunks_{detected}"

        if df is None or not _has_valid_ohlc(df):
            df = _load_source_for_tf(sym, tf, built, cache_dir=cache_dir)
            source = "resampled"

    if df is None or df.empty or not _has_valid_ohlc(df):
        return {"symbol": sym, "timeframe": tf, "status": "skipped", "reason": "no source data"}

    path = save_ohlcv(df, sym, tf, cache_dir, source="dukascopy")
    built[tf] = df
    return {
        "symbol": sym,
        "timeframe": tf,
        "bars": len(df),
        "status": "ok",
        "source": source,
        "path": str(path),
    }


def repair_symbol(symbol: str, *, cache_dir: Path | None = None) -> list[dict]:
    sym = to_canonical(symbol)
    migrate_legacy_year_chunks(sym, cache_dir=cache_dir)
    built: dict[str, pd.DataFrame] = {}
    results: list[dict] = []
    for tf in TF_BUILD_ORDER:
        results.append(repair_symbol_timeframe(sym, tf, cache_dir=cache_dir, built=built))
    return results


def repair_all(*, cache_dir: Path | None = None) -> list[dict]:
    root = cache_dir or CACHE_DIR
    if not root.is_dir():
        return []
    symbols: list[str] = []
    for sym_dir in sorted(root.iterdir()):
        if sym_dir.is_dir():
            symbols.append(from_cache_dir_name(sym_dir.name))
    if not symbols:
        symbols = list(FOREX_PAIRS)

    results: list[dict] = []
    for sym in symbols:
        results.extend(repair_symbol(sym, cache_dir=root))
    return results


def from_cache_dir_name(name: str) -> str:
    from borex.data.symbols import from_cache_dir

    return from_cache_dir(name)
