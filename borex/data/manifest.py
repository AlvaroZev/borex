from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from borex.config import CACHE_DIR
from borex.data.symbols import cache_dir_name, to_canonical

MANIFEST_VERSION = 1


def _parquet_path(symbol: str, timeframe: str, cache_dir: Path | None = None) -> Path:
    root = cache_dir or CACHE_DIR
    return root / cache_dir_name(to_canonical(symbol)) / f"{timeframe}.parquet"


def manifest_path(symbol: str, timeframe: str, cache_dir: Path | None = None) -> Path:
    return _parquet_path(symbol, timeframe, cache_dir).with_suffix(".manifest.json")


def file_content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(
    parquet_path: Path,
    symbol: str,
    timeframe: str,
    *,
    source: str,
    cache_dir: Path | None = None,
) -> dict:
    """Write or update manifest for a parquet dataset."""
    canonical = to_canonical(symbol)
    path = manifest_path(canonical, timeframe, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(parquet_path)
    df.index = pd.to_datetime(df.index, utc=True)
    now = datetime.now(timezone.utc).isoformat()

    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    manifest = {
        "version": MANIFEST_VERSION,
        "symbol": canonical,
        "timeframe": timeframe,
        "source": source,
        "created_at": existing.get("created_at", now),
        "updated_at": now,
        "bars": len(df),
        "start": df.index.min().isoformat(),
        "end": df.index.max().isoformat(),
        "parquet_path": str(parquet_path),
        "content_hash": file_content_hash(parquet_path),
    }
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def read_manifest(symbol: str, timeframe: str, cache_dir: Path | None = None) -> dict | None:
    path = manifest_path(symbol, timeframe, cache_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def get_dataset_hash(symbol: str, timeframe: str, cache_dir: Path | None = None) -> str | None:
    manifest = read_manifest(symbol, timeframe, cache_dir)
    if manifest:
        return manifest.get("content_hash")
    parquet = _parquet_path(symbol, timeframe, cache_dir)
    if parquet.is_file():
        return file_content_hash(parquet)
    return None


def _infer_source(symbol: str, timeframe: str, cache_dir: Path | None = None) -> str:
    parquet = _parquet_path(symbol, timeframe, cache_dir)
    years_dir = parquet.parent / "years"
    if years_dir.is_dir() and any(years_dir.glob("*.parquet")):
        return "dukascopy"
    return "yahoo"


def backfill_manifest(
    symbol: str,
    timeframe: str,
    *,
    source: str | None = None,
    cache_dir: Path | None = None,
) -> dict | None:
    """Create manifest for an existing parquet file if missing."""
    parquet = _parquet_path(symbol, timeframe, cache_dir)
    if not parquet.is_file():
        return None
    existing = read_manifest(symbol, timeframe, cache_dir)
    if source:
        src = source
    elif existing and existing.get("source") not in (None, "unknown"):
        src = existing["source"]
    else:
        src = _infer_source(symbol, timeframe, cache_dir)
    if existing and existing.get("content_hash") == file_content_hash(parquet):
        if existing.get("source") in (None, "unknown") and src != "unknown":
            return write_manifest(
                parquet, symbol, timeframe, source=src, cache_dir=cache_dir
            )
        return existing
    return write_manifest(parquet, symbol, timeframe, source=src, cache_dir=cache_dir)
