from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from borex.config import CACHE_DIR
from borex.data.symbols import cache_dir_name, from_cache_dir, to_canonical


def manifest_path(symbol: str, timeframe: str, cache_dir: Path | None = None) -> Path:
    return cache_path(symbol, timeframe, cache_dir).with_suffix(".manifest.json")


def read_manifest(symbol: str, timeframe: str, cache_dir: Path | None = None) -> dict | None:
    path = manifest_path(symbol, timeframe, cache_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cache_path(symbol: str, timeframe: str, cache_dir: Path | None = None) -> Path:
    root = cache_dir or CACHE_DIR
    return root / cache_dir_name(to_canonical(symbol)) / f"{timeframe}.parquet"


def is_cached(symbol: str, timeframe: str, cache_dir: Path | None = None) -> bool:
    return cache_path(symbol, timeframe, cache_dir).is_file()


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
                    "source": manifest.get("source") if manifest else "dukascopy",
                }
            )
    return rows


def repair_ohlc(df: pd.DataFrame) -> pd.DataFrame:
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
