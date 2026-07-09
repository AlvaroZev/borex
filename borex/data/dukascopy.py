from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from borex.data.store import save_ohlcv

# dukascopy-node instrument id -> Yahoo-style symbol used by borex
DUKASCOPY_TO_SYMBOL: dict[str, str] = {
    "eurusd": "EURUSD=X",
    "gbpusd": "GBPUSD=X",
    "usdjpy": "USDJPY=X",
    "audusd": "AUDUSD=X",
    "usdcad": "USDCAD=X",
    "usdchf": "USDCHF=X",
    "nzdusd": "NZDUSD=X",
    "eurgbp": "EURGBP=X",
    "eurjpy": "EURJPY=X",
    "gbpjpy": "GBPJPY=X",
}

# dukascopy-node timeframe token -> borex timeframe code
DUKASCOPY_TO_TIMEFRAME: dict[str, str] = {
    "m1": "1m",
    "m15": "15m",
    "m30": "30m",
    "h1": "1h",
    "h4": "4h",
    "d1": "1d",
    "w1": "1wk",
}


def parse_dukascopy_filename(path: Path) -> tuple[str, str] | None:
    """
    Parse dukascopy-node CSV names like:
    eurusd-h1-bid-2015-01-01-2026-01-01.csv
    """
    m = re.match(
        r"^([a-z]{6})-([a-z0-9]+)-(?:bid|ask)-\d{4}-\d{2}-\d{2}-\d{4}-\d{2}-\d{2}\.csv$",
        path.name,
        re.IGNORECASE,
    )
    if not m:
        return None
    instrument = m.group(1).lower()
    tf_token = m.group(2).lower()
    symbol = DUKASCOPY_TO_SYMBOL.get(instrument)
    timeframe = DUKASCOPY_TO_TIMEFRAME.get(tf_token)
    if not symbol or not timeframe:
        return None
    return symbol, timeframe


def load_dukascopy_csv(path: str | Path) -> pd.DataFrame:
    """Load a dukascopy-node CSV (timestamp ms + OHLC)."""
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    ts_col = cols.get("timestamp")
    if not ts_col:
        raise ValueError(f"No timestamp column in {path}")

    out = pd.DataFrame()
    out.index = pd.to_datetime(df[ts_col], unit="ms", utc=True)
    for src, dst in [("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close")]:
        if src in cols:
            out[dst] = df[cols[src]].astype(float)
    if "volume" in cols:
        out["Volume"] = df[cols["volume"]].astype(float)
    return out.sort_index()


def import_dukascopy_csv(
    path: str | Path,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> Path:
    """Import dukascopy-node CSV into borex parquet cache."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)

    parsed = parse_dukascopy_filename(p)
    sym = symbol or (parsed[0] if parsed else None)
    tf = timeframe or (parsed[1] if parsed else None)
    if not sym or not tf:
        raise ValueError(
            f"Could not detect symbol/timeframe from {p.name}. "
            "Pass --symbol EURUSD=X --timeframe 1h explicitly."
        )

    df = load_dukascopy_csv(p)
    return save_ohlcv(df, sym, tf, source="dukascopy")
