from __future__ import annotations

from pathlib import Path

import pandas as pd

from borex.config import CACHE_DIR
from borex.data.manifest import read_manifest, write_manifest
from borex.data.symbols import cache_dir_name, from_cache_dir, to_canonical


def cache_path(symbol: str, timeframe: str, cache_dir: Path | None = None) -> Path:
    root = cache_dir or CACHE_DIR
    return root / cache_dir_name(to_canonical(symbol)) / f"{timeframe}.parquet"


def year_chunk_path(symbol: str, timeframe: str, year: int, cache_dir: Path | None = None) -> Path:
    """Per-timeframe year chunks (avoids cross-TF overwrites)."""
    sym_dir = cache_path(symbol, timeframe, cache_dir).parent
    return sym_dir / "years" / timeframe / f"{year}.parquet"


def legacy_year_chunk_path(symbol: str, year: int, cache_dir: Path | None = None) -> Path:
    sym_dir = cache_path(symbol, "1d", cache_dir).parent
    return sym_dir / "years" / f"{year}.parquet"


def list_year_chunk_years(
    symbol: str, timeframe: str, cache_dir: Path | None = None
) -> list[int]:
    chunk_dir = year_chunk_path(symbol, timeframe, 2000, cache_dir).parent
    if not chunk_dir.is_dir():
        return []
    years: list[int] = []
    for p in chunk_dir.glob("*.parquet"):
        try:
            years.append(int(p.stem))
        except ValueError:
            continue
    return sorted(years)


def is_cached(symbol: str, timeframe: str, cache_dir: Path | None = None) -> bool:
    return cache_path(symbol, timeframe, cache_dir).is_file()


def is_year_cached(symbol: str, timeframe: str, year: int, cache_dir: Path | None = None) -> bool:
    return year_chunk_path(symbol, timeframe, year, cache_dir).is_file()


def has_year_data(
    symbol: str,
    timeframe: str,
    year: int,
    cache_dir: Path | None = None,
) -> bool:
    """Year available in per-year chunk or inside merged parquet."""
    if is_year_cached(symbol, timeframe, year, cache_dir):
        return True
    if not is_cached(symbol, timeframe, cache_dir):
        return False
    df = load_ohlcv(symbol, timeframe, cache_dir)
    return bool((df.index.year == year).any())


def cache_info(symbol: str, timeframe: str, cache_dir: Path | None = None) -> dict | None:
    path = cache_path(symbol, timeframe, cache_dir)
    if not path.is_file():
        return None
    df = _normalize_df(pd.read_parquet(path))
    return {
        "path": str(path),
        "bars": len(df),
        "start": df.index.min(),
        "end": df.index.max(),
    }


def covers_range(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    *,
    cache_dir: Path | None = None,
    end_tolerance_days: int = 3,
) -> bool:
    """True if cached parquet already spans [start, end]."""
    info = cache_info(symbol, timeframe, cache_dir)
    if not info:
        return False
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    tol = pd.Timedelta(days=end_tolerance_days)
    return info["start"] <= start_ts and info["end"] >= (end_ts - tol)


def save_year_chunk(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    year: int,
    cache_dir: Path | None = None,
) -> Path:
    path = year_chunk_path(symbol, timeframe, year, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = _normalize_df(df)
    out.to_parquet(path)
    return path


def save_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    cache_dir: Path | None = None,
    *,
    source: str = "unknown",
) -> Path:
    path = cache_path(symbol, timeframe, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = _normalize_df(df)
    out.to_parquet(path)
    write_manifest(path, symbol, timeframe, source=source, cache_dir=cache_dir)
    return path


def load_ohlcv(symbol: str, timeframe: str, cache_dir: Path | None = None) -> pd.DataFrame:
    path = cache_path(symbol, timeframe, cache_dir)
    if not path.is_file():
        raise FileNotFoundError(f"No cached data for {symbol} {timeframe} at {path}")
    return _normalize_df(pd.read_parquet(path))


def list_cached(cache_dir: Path | None = None) -> list[dict]:
    root = cache_dir or CACHE_DIR
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for sym_dir in sorted(root.iterdir()):
        if not sym_dir.is_dir():
            continue
        for f in sorted(sym_dir.glob("*.parquet")):
            df = pd.read_parquet(f)
            sym = from_cache_dir(sym_dir.name)
            tf = f.stem
            manifest = read_manifest(sym, tf, root)
            rows.append(
                {
                    "symbol": sym,
                    "timeframe": tf,
                    "bars": len(df),
                    "start": str(df.index.min()),
                    "end": str(df.index.max()),
                    "path": str(f),
                    "source": manifest.get("source") if manifest else None,
                    "dataset_hash": manifest.get("content_hash") if manifest else None,
                }
            )
    return rows


def repair_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure High/Low envelope Open/Close (fixes merge artifacts)."""
    out = df.copy()
    body_hi = out[["Open", "Close"]].max(axis=1)
    body_lo = out[["Open", "Close"]].min(axis=1)
    out["High"] = out["High"].where(out["High"] >= body_hi, body_hi)
    out["Low"] = out["Low"].where(out["Low"] <= body_lo, body_lo)
    return out


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    out.columns = [c.strip().title() for c in out.columns]
    out.index = pd.to_datetime(out.index, utc=True)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    required = {"Open", "High", "Low", "Close"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    return repair_ohlc(out)
