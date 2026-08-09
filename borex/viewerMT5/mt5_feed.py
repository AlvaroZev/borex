"""Load OHLC from MetaTrader 5 for viewerMT5 backtests."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from borex.models.candle import Candle

ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOT = ROOT.parent / "borex_live"
MT5_CACHE_ROOT = ROOT / "data" / "cache" / "mt5"

_TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def ensure_live_on_path() -> Path:
    """Put borex_live on sys.path and load its .env (MT5 credentials)."""
    if not LIVE_ROOT.is_dir():
        raise RuntimeError(
            f"borex_live not found at {LIVE_ROOT}. "
            "viewerMT5 needs the sibling borex_live repo for MetaTrader5."
        )
    live = str(LIVE_ROOT)
    if live not in sys.path:
        sys.path.insert(0, live)
    try:
        from dotenv import load_dotenv

        load_dotenv(LIVE_ROOT / ".env")
    except Exception:
        pass
    return LIVE_ROOT


def period_to_range(
    period: str,
    *,
    end: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Parse viewer-style period (3y, 60d, max) into UTC [start, end)."""
    end = end or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)

    raw = (period or "3y").strip().lower()
    if raw in ("max", "all"):
        # Broker depth varies; request far enough back that MT5 returns what it has.
        return datetime(2010, 1, 1, tzinfo=timezone.utc), end

    m = re.fullmatch(r"(\d+)([ymwdh])", raw)
    if not m:
        raise ValueError(
            f"Unsupported period {period!r}; use e.g. 3y, 90d, 24m, or max"
        )
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "y":
        delta = timedelta(days=365 * n)
    elif unit == "m":
        delta = timedelta(days=30 * n)
    elif unit == "w":
        delta = timedelta(weeks=n)
    elif unit == "d":
        delta = timedelta(days=n)
    else:  # h
        delta = timedelta(hours=n)
    return end - delta, end


def _cache_path(symbol: str, interval: str) -> Path:
    safe = symbol.replace("=", "").replace("/", "_")
    return MT5_CACHE_ROOT / safe / f"{interval}.parquet"


def _rates_to_candles(rates) -> list[Candle]:
    candles: list[Candle] = []
    for row in rates:
        ts = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc)
        candles.append(
            Candle(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["tick_volume"]),
            )
        )
    return candles


def _candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    rows = [
        {
            "timestamp": pd.Timestamp(c.timestamp).tz_convert("UTC")
            if pd.Timestamp(c.timestamp).tzinfo
            else pd.Timestamp(c.timestamp, tz="UTC"),
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in candles
    ]
    if not rows:
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
    return pd.DataFrame(rows).drop_duplicates("timestamp").sort_values("timestamp")


def _frame_to_candles(df: pd.DataFrame) -> list[Candle]:
    out: list[Candle] = []
    for row in df.itertuples(index=False):
        ts = pd.Timestamp(row.timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        out.append(
            Candle(
                timestamp=ts.to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(getattr(row, "volume", 0.0) or 0.0),
            )
        )
    return out


def _slice_candles(
    candles: list[Candle], start: datetime, end: datetime
) -> list[Candle]:
    out: list[Candle] = []
    for c in candles:
        t = pd.Timestamp(c.timestamp)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        if start <= t.to_pydatetime() < end:
            out.append(c)
    return out


def connect_mt5_client():
    ensure_live_on_path()
    from borex_live.mt5.client import Mt5Client

    path = os.environ.get("MT5_PATH", "")
    login = int(os.environ.get("MT5_LOGIN", "0") or 0)
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_DEMO_SERVER") or os.environ.get("MT5_SERVER", "")
    client = Mt5Client(path=path, login=login, password=password, server=server)
    client.connect()
    return client


def list_tradeable_yahoo_symbols(client=None) -> list[str]:
    """All broker Forex pairs that are tradeable in Market Watch."""
    own = client is None
    if own:
        client = connect_mt5_client()
    try:
        return client.list_forex_yahoo_symbols()
    finally:
        if own:
            client.disconnect()


def _expected_bars(interval: str, start: datetime, end: datetime) -> int:
    sec = _TF_SECONDS.get(interval.lower(), 3600)
    # Forex ~5/7 of calendar hours; use that as a soft target for cache freshness.
    return max(1, int(0.7 * (end - start).total_seconds() / sec))


def _copy_rates_chunked(client, mt5_sym: str, interval: str, start: datetime, end: datetime):
    """Fetch OHLC; if a long range fails/short, chunk or use from_pos."""
    import numpy as np

    mt5 = client._mt5
    tf = client._tf(interval)

    def one(a: datetime, b: datetime):
        return mt5.copy_rates_range(mt5_sym, tf, a, b)

    rates = one(start, end)
    expected = _expected_bars(interval, start, end)

    if rates is None or len(rates) == 0 or len(rates) < int(0.55 * expected):
        # Fallback: chunk by calendar year (helps some terminals / maxbars edges)
        parts = []
        cursor = start
        while cursor < end:
            nxt = min(
                datetime(cursor.year + 1, 1, 1, tzinfo=timezone.utc),
                end,
            )
            chunk = one(cursor, nxt)
            if chunk is not None and len(chunk):
                parts.append(chunk)
            cursor = nxt
        if parts:
            rates = np.concatenate(parts)

    if rates is None or len(rates) == 0 or len(rates) < int(0.55 * expected):
        # Last resort: deepest terminal buffer, then clip to window
        deep = mt5.copy_rates_from_pos(mt5_sym, tf, 0, 100_000)
        if deep is not None and len(deep):
            if rates is None or len(deep) > len(rates):
                rates = deep

    return rates


def fetch_mt5_candles(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    client=None,
    use_cache: bool = True,
    write_cache: bool = True,
) -> list[Candle]:
    """
    Load [start, end) OHLC for one Yahoo-style symbol from MT5.
    Optional parquet cache under data/cache/mt5/.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    expected = _expected_bars(interval, start, end)
    # Accept cache only if it covers a meaningful share of the requested window
    # (avoids sticky short caches that permanently drop pairs).
    cache_ok_bars = max(200, int(0.55 * expected))

    cache_file = _cache_path(symbol, interval)
    if use_cache and cache_file.is_file():
        try:
            df = pd.read_parquet(cache_file)
            cached = _frame_to_candles(df)
            sliced = _slice_candles(cached, start, end)
            if sliced and len(sliced) >= cache_ok_bars:
                return sliced
        except Exception:
            pass

    own = client is None
    if own:
        client = connect_mt5_client()
    try:
        mt5_sym = client.ensure_symbol(symbol)
        rates = _copy_rates_chunked(client, mt5_sym, interval, start, end)
        if rates is None or len(rates) == 0:
            err = client._mt5.last_error() if client._mt5 is not None else None
            raise RuntimeError(f"MT5 copy_rates failed for {mt5_sym} {interval}: {err}")
        candles = _rates_to_candles(rates)
        candles = _slice_candles(candles, start, end)
        if write_cache and candles:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            # Merge with any existing cache so a deeper fetch grows the file
            if cache_file.is_file():
                try:
                    prev = _frame_to_candles(pd.read_parquet(cache_file))
                    merged = {pd.Timestamp(c.timestamp).value: c for c in prev}
                    for c in candles:
                        merged[pd.Timestamp(c.timestamp).value] = c
                    candles_to_store = [
                        merged[k] for k in sorted(merged.keys())
                    ]
                    _candles_to_frame(candles_to_store).to_parquet(
                        cache_file, index=False
                    )
                    candles = _slice_candles(candles_to_store, start, end)
                except Exception:
                    _candles_to_frame(candles).to_parquet(cache_file, index=False)
            else:
                _candles_to_frame(candles).to_parquet(cache_file, index=False)
        return candles
    finally:
        if own:
            client.disconnect()


def load_mt5_universe(
    symbols: list[str],
    interval: str,
    period: str = "3y",
    *,
    min_bars: int | None = None,
    use_cache: bool = True,
    progress: bool = True,
) -> dict[str, list[Candle]]:
    """
    Fetch OHLC for every symbol. Keeps any pair with enough bars to trade
    (warmup), even if broker history is shorter than the full period.
    """
    start, end = period_to_range(period)
    if min_bars is None:
        # Strategy warmup is ~100–200 bars; don't require full 3y coverage.
        min_bars = 500

    client = connect_mt5_client()
    out: dict[str, list[Candle]] = {}
    try:
        total = len(symbols)
        for i, sym in enumerate(symbols, 1):
            try:
                candles = fetch_mt5_candles(
                    sym,
                    interval,
                    start,
                    end,
                    client=client,
                    use_cache=use_cache,
                    write_cache=True,
                )
            except Exception as exc:
                if progress:
                    print(
                        f"  [{i}/{total}] skip {sym}: {exc}",
                        flush=True,
                        file=sys.stderr,
                    )
                continue
            if len(candles) < min_bars:
                if progress:
                    print(
                        f"  [{i}/{total}] skip {sym}: {len(candles)} bars "
                        f"(need >={min_bars})",
                        flush=True,
                        file=sys.stderr,
                    )
                continue
            out[sym] = candles
            if progress:
                first = candles[0].timestamp
                last = candles[-1].timestamp
                print(
                    f"  [{i}/{total}] {sym}: {len(candles)} bars  {first} -> {last}",
                    flush=True,
                    file=sys.stderr,
                )
    finally:
        client.disconnect()
    return out
