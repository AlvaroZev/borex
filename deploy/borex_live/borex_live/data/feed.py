from __future__ import annotations

import logging
from typing import Callable

from borex.models.candle import Candle

from borex_live.config import LiveServiceConfig
from borex_live.mt5.client import Mt5Client
from borex_live.paths import ensure_borex_main_on_path

logger = logging.getLogger(__name__)

# Enough for alexg lookbacks on live; more is nicer but not required.
MIN_WARMUP_BARS = 50


def _trim(candles: list[Candle], limit: int) -> list[Candle]:
    if limit <= 0 or len(candles) <= limit:
        return candles
    return candles[-limit:]


def _finalize(candles: list[Candle], interval: str, limit: int) -> list[Candle]:
    return _trim(_drop_forming_bar(candles, interval), limit)


def _from_dukascopy(yahoo_symbol: str, interval: str) -> list[Candle]:
    ensure_borex_main_on_path()
    from borex.data import load_market_data

    return load_market_data(
        yahoo_symbol,
        period="max",
        interval=interval,
        cache_mode="only",
    )


def _from_mt5(
    yahoo_symbol: str,
    interval: str,
    mt5: Mt5Client,
    count: int,
) -> list[Candle]:
    if not mt5.connected or mt5.dry_run:
        return []
    mt5.ensure_symbol(yahoo_symbol)
    return mt5.fetch_bars(yahoo_symbol, interval, count=count + 1)


def _yf_period_for_interval(interval: str) -> str:
    key = interval.strip().lower()
    return {
        "1m": "7d",
        "5m": "60d",
        "15m": "60d",
        "30m": "60d",
        "1h": "60d",
        "4h": "2y",
        "1d": "2y",
    }.get(key, "60d")


def _from_yfinance(yahoo_symbol: str, interval: str) -> list[Candle]:
    ensure_borex_main_on_path()
    from borex.data.loader import load_yfinance

    return load_yfinance(
        yahoo_symbol,
        period=_yf_period_for_interval(interval),
        interval=interval,
    )


def bootstrap_candles(
    yahoo_symbol: str,
    interval: str,
    *,
    mt5: Mt5Client,
    warmup_bars: int,
    borex_main_root=None,
) -> list[Candle]:
    """
    Warmup pull order:
      1. Dukascopy parquet cache (skipped for sub-hour — too large for live LTF)
      2. MT5 history
      3. Yahoo Finance (yfinance)

    ~50 closed bars is enough. If nothing is available, return [] — the pair
    stays in the universe and fills from live MT5 bars.
    """
    need = max(MIN_WARMUP_BARS, int(warmup_bars or MIN_WARMUP_BARS))
    key = interval.strip().lower()
    skip_duka = key in {"1s", "1m", "5m", "15m", "30m"}
    sources: list[tuple[str, Callable[[], list[Candle]]]] = []
    if not skip_duka:
        sources.append(("dukascopy", lambda: _from_dukascopy(yahoo_symbol, interval)))
    sources.extend(
        [
            ("mt5", lambda: _from_mt5(yahoo_symbol, interval, mt5, need)),
            ("yfinance", lambda: _from_yfinance(yahoo_symbol, interval)),
        ]
    )

    best: list[Candle] = []
    best_source = ""

    for name, loader in sources:
        try:
            bars = loader() or []
            bars = _drop_forming_bar(bars, interval)
        except Exception as exc:
            logger.debug("%s warmup miss for %s: %s", name, yahoo_symbol, exc)
            continue
        if not bars:
            continue
        if len(bars) >= MIN_WARMUP_BARS:
            logger.info(
                "Bootstrap %s from %s (%d bars)",
                yahoo_symbol,
                name,
                len(bars),
            )
            return _finalize(bars, interval, need)
        if len(bars) > len(best):
            best = bars
            best_source = name

    if best:
        logger.info(
            "Bootstrap %s from %s (%d bars, below %d target)",
            yahoo_symbol,
            best_source,
            len(best),
            MIN_WARMUP_BARS,
        )
        return _finalize(best, interval, need)

    logger.warning(
        "No warmup data for %s (dukascopy/mt5/yfinance); keeping pair with empty history",
        yahoo_symbol,
    )
    return []


def _interval_seconds(interval: str) -> int:
    key = interval.strip().lower()
    mapping = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }
    return mapping.get(key, 3600)


def _drop_forming_bar(candles: list[Candle], interval: str) -> list[Candle]:
    """Remove incomplete last bar (open time + interval still in the future)."""
    if not candles:
        return candles
    import pandas as pd

    last = candles[-1]
    open_ts = pd.Timestamp(last.timestamp)
    if open_ts.tzinfo is None:
        open_ts = open_ts.tz_localize("UTC")
    else:
        open_ts = open_ts.tz_convert("UTC")
    close_ts = open_ts + pd.Timedelta(seconds=_interval_seconds(interval))
    now = pd.Timestamp.now(tz="UTC")
    if close_ts > now:
        return candles[:-1]
    return candles


def append_live_bar(
    store: dict[str, list[Candle]],
    yahoo_symbol: str,
    interval: str,
    mt5: Mt5Client,
) -> Candle | None:
    """Fetch latest closed bar from MT5 and append if new."""
    bars = mt5.fetch_bars(yahoo_symbol, interval, count=3, from_pos=0)
    if len(bars) < 2:
        return None
    closed_bars = _drop_forming_bar(bars, interval)
    if not closed_bars:
        return None
    closed = closed_bars[-1]
    series = store.setdefault(yahoo_symbol, [])
    if series and series[-1].timestamp >= closed.timestamp:
        return None
    series.append(closed)
    return closed


def load_universe(
    symbols: list[str],
    cfg: LiveServiceConfig,
    mt5: Mt5Client,
) -> dict[str, list[Candle]]:
    """Load every symbol. Empty history is OK — pair remains tradeable once live bars arrive."""
    out: dict[str, list[Candle]] = {}
    for sym in symbols:
        try:
            if mt5.connected and not mt5.dry_run:
                mt5.ensure_symbol(sym)
        except Exception as exc:
            logger.warning("MT5 symbol_select failed for %s: %s", sym, exc)
        try:
            out[sym] = bootstrap_candles(
                sym,
                cfg.interval,
                mt5=mt5,
                warmup_bars=cfg.warmup_bars,
            )
        except Exception as exc:
            logger.warning(
                "Warmup error for %s (%s); keeping empty history",
                sym,
                exc,
            )
            out[sym] = []
    empty = sum(1 for bars in out.values() if not bars)
    logger.info(
        "Universe ready: %d pairs (%d with warmup, %d empty)",
        len(out),
        len(out) - empty,
        empty,
    )
    return out


def load_ltf_universe(
    symbols: list[str],
    intervals: tuple[str, ...] | list[str],
    *,
    mt5: Mt5Client,
    warmup_bars: int = 500,
) -> dict[str, dict[str, list[Candle]]]:
    """
    Lower-TF series for alexg8 ghost-fill confirmation.
    Shape: symbol -> {interval -> candles}.
    """
    out: dict[str, dict[str, list[Candle]]] = {}
    tfs = [str(i).strip() for i in intervals if str(i).strip()]
    if not tfs:
        return out
    need = max(MIN_WARMUP_BARS, int(warmup_bars or MIN_WARMUP_BARS))
    loaded = 0
    for sym in symbols:
        by_tf: dict[str, list[Candle]] = {}
        for tf in tfs:
            try:
                bars = bootstrap_candles(sym, tf, mt5=mt5, warmup_bars=need)
            except Exception as exc:
                logger.warning("LTF warmup miss %s %s: %s", sym, tf, exc)
                bars = []
            if bars:
                by_tf[tf] = bars
                loaded += 1
        if by_tf:
            out[sym] = by_tf
    logger.info(
        "LTF universe ready: %d symbols, %d series (%s)",
        len(out),
        loaded,
        ",".join(tfs),
    )
    return out


def refresh_ltf_bars(
    store: dict[str, dict[str, list[Candle]]],
    symbols: list[str],
    intervals: tuple[str, ...] | list[str],
    *,
    mt5: Mt5Client,
    keep: int = 500,
) -> int:
    """Append newest closed LTF bars from MT5 into an existing LTF store."""
    if mt5.dry_run or not mt5.connected:
        return 0
    added = 0
    tfs = [str(i).strip() for i in intervals if str(i).strip()]
    for sym in symbols:
        by_tf = store.setdefault(sym, {})
        for tf in tfs:
            try:
                bars = mt5.fetch_bars(sym, tf, count=5)
            except Exception as exc:
                logger.debug("LTF refresh miss %s %s: %s", sym, tf, exc)
                continue
            closed = _drop_forming_bar(bars, tf)
            if not closed:
                continue
            series = by_tf.setdefault(tf, [])
            for bar in closed:
                if series and series[-1].timestamp > bar.timestamp:
                    continue
                if series and series[-1].timestamp == bar.timestamp:
                    series[-1] = bar
                    continue
                series.append(bar)
                added += 1
            if keep > 0 and len(series) > keep:
                by_tf[tf] = series[-keep:]
    return added
