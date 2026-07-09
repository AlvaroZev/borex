from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from borex.models.candle import Candle
from borex.viewer.analysis import MarketAnalysis

MANIFEST_FILE = "manifest.json"
DECISIONS_FILE = "decisions.csv"
CANDLES_FILE = "candles.csv"
AOI_FILE = "aoi.csv"
TIMELINE_FILE = "timeline.csv"

DECISION_FIELDS = [
    "time",
    "time_unix",
    "index",
    "symbol",
    "action",
    "trend",
    "setup",
    "aoi_kind",
    "signal",
    "signal_label",
    "currency_bias",
    "strength",
    "pattern",
    "stop_loss",
    "take_profit",
    "price",
    "price_fmt",
]

CANDLE_FIELDS = ["symbol", "time_unix", "open", "high", "low", "close"]
AOI_FIELDS = ["symbol", "level", "kind", "touches", "recency"]


def _ts_unix(ts: object) -> int:
    import pandas as pd

    return int(pd.Timestamp(ts).timestamp())


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def save_analysis_bundle(
    analysis: MarketAnalysis,
    out_dir: Path,
    *,
    timeframe: str = "1h",
    strategy_name: str = "alexg3",
    extra_meta: dict[str, Any] | None = None,
) -> Path:
    """Write decisions, aligned candles, AOI, and timeline CSVs + manifest."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_csv(out_dir / DECISIONS_FILE, DECISION_FIELDS, analysis.all_decisions)

    candle_rows: list[dict[str, Any]] = []
    for sym in analysis.symbols:
        for bar in analysis.aligned_candles.get(sym, []):
            candle_rows.append(
                {
                    "symbol": sym,
                    "time_unix": bar["time"],
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                }
            )
    _write_csv(out_dir / CANDLES_FILE, CANDLE_FIELDS, candle_rows)

    aoi_rows: list[dict[str, Any]] = []
    for sym in analysis.symbols:
        for z in analysis.aoi_by_symbol.get(sym, []):
            aoi_rows.append({"symbol": sym, **z})
    _write_csv(out_dir / AOI_FILE, AOI_FIELDS, aoi_rows)

    _write_csv(
        out_dir / TIMELINE_FILE,
        ["time_unix"],
        [{"time_unix": t} for t in analysis.master_timeline_unix],
    )

    files = {
        "decisions": DECISIONS_FILE,
        "candles": CANDLES_FILE,
        "aoi": AOI_FILE,
        "timeline": TIMELINE_FILE,
    }
    if extra_meta and extra_meta.get("trades_file"):
        files["trades"] = extra_meta["trades_file"]

    manifest = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy_name,
        "timeframe": timeframe,
        "master_symbol": analysis.master_symbol,
        "symbols": analysis.symbols,
        "bar_counts": analysis.bar_counts,
        "date_ranges": analysis.date_ranges,
        "timeline_start": (
            analysis.master_timeline_unix[0] if analysis.master_timeline_unix else None
        ),
        "timeline_end": (
            analysis.master_timeline_unix[-1] if analysis.master_timeline_unix else None
        ),
        "timeline_bars": len(analysis.master_timeline_unix),
        "total_decisions": analysis.total_decisions,
        "files": files,
    }
    if extra_meta:
        manifest.update(extra_meta)

    manifest_path = out_dir / MANIFEST_FILE
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out_dir


def load_analysis_bundle(
    in_dir: Path,
    candles_by_symbol: dict[str, list[Candle]] | None = None,
) -> MarketAnalysis:
    """Restore MarketAnalysis from a saved CSV bundle."""
    in_dir = Path(in_dir)
    manifest_path = in_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing manifest: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    symbols: list[str] = list(manifest.get("symbols", []))

    timeline_rows = _read_csv(in_dir / TIMELINE_FILE)
    master_timeline_unix = [int(r["time_unix"]) for r in timeline_rows]

    symbol_ts_unix: dict[str, dict[int, int]] = {}
    if candles_by_symbol:
        from borex.alexg.multi_market import align_symbols_to_timeline

        master = manifest["master_symbol"]
        if master in candles_by_symbol:
            ts_maps = align_symbols_to_timeline(
                candles_by_symbol[master], candles_by_symbol
            )
            symbol_ts_unix = {
                sym: {_ts_unix(ts): idx for ts, idx in ts_map.items()}
                for sym, ts_map in ts_maps.items()
            }

    decisions_raw = _read_csv(in_dir / DECISIONS_FILE)
    all_decisions: list[dict[str, Any]] = []
    decisions_by_symbol: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    for row in decisions_raw:
        d = {
            "time": row["time"],
            "time_unix": int(float(row["time_unix"])),
            "index": int(float(row["index"])),
            "symbol": row["symbol"],
            "action": row["action"],
            "trend": row.get("trend", ""),
            "setup": row.get("setup", ""),
            "aoi_kind": row.get("aoi_kind", ""),
            "signal": row.get("signal", ""),
            "signal_label": row.get("signal_label", ""),
            "currency_bias": row.get("currency_bias", ""),
            "strength": row.get("strength", ""),
            "pattern": row.get("pattern", ""),
            "stop_loss": _float_or_none(row.get("stop_loss")),
            "take_profit": _float_or_none(row.get("take_profit")),
            "price": _float_or_none(row.get("price")),
            "price_fmt": row.get("price_fmt", ""),
        }
        all_decisions.append(d)
        sym = d["symbol"]
        if sym not in decisions_by_symbol:
            decisions_by_symbol[sym] = []
            if sym not in symbols:
                symbols.append(sym)
        decisions_by_symbol[sym].append(d)

    all_decisions.sort(key=lambda r: (r["time_unix"], r["symbol"]))

    aligned_candles: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    for row in _read_csv(in_dir / CANDLES_FILE):
        sym = row["symbol"]
        bar = {
            "time": int(float(row["time_unix"])),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        if sym not in aligned_candles:
            aligned_candles[sym] = []
            if sym not in symbols:
                symbols.append(sym)
        aligned_candles[sym].append(bar)

    aoi_by_symbol: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    for row in _read_csv(in_dir / AOI_FILE):
        sym = row["symbol"]
        zone = {
            "level": float(row["level"]),
            "kind": row["kind"],
            "touches": int(float(row["touches"])),
            "recency": int(float(row["recency"])),
        }
        if sym not in aoi_by_symbol:
            aoi_by_symbol[sym] = []
        aoi_by_symbol[sym].append(zone)

    bar_counts = dict(manifest.get("bar_counts", {}))
    for sym, bars in aligned_candles.items():
        bar_counts[sym] = len(bars)

    analysis = MarketAnalysis(
        master_symbol=manifest["master_symbol"],
        symbols=sorted(symbols),
        master_timeline_unix=master_timeline_unix,
        symbol_ts_unix=symbol_ts_unix,
        decisions_by_symbol=decisions_by_symbol,
        all_decisions=all_decisions,
        aoi_by_symbol=aoi_by_symbol,
        aligned_candles=aligned_candles,
        bar_counts=bar_counts,
        date_ranges=dict(manifest.get("date_ranges", {})),
        timeframe=manifest.get("timeframe", ""),
        saved_at=manifest.get("saved_at", ""),
        source_path=str(in_dir.resolve()),
    )
    return analysis


def default_analysis_dir(
    timeframe: str = "1h",
    period: str = "max",
    strategy: str = "alexg3",
) -> Path:
    from borex.config import ROOT_DIR

    safe_period = period.replace("/", "-")
    return ROOT_DIR / "data" / "runs" / f"{strategy}_{safe_period}_{timeframe}"


def resolve_run_dir(
    *,
    strategy: str,
    period: str,
    interval: str,
    save_analysis: str | None,
    save_trades: str | None,
) -> Path | None:
    """Shared output folder when saving analysis and/or trades."""
    if save_analysis is None and save_trades is None:
        return None
    explicit = save_analysis if save_analysis else save_trades
    if explicit:
        path = Path(explicit)
        if path.suffix.lower() == ".csv":
            return path.parent
        return path
    return default_analysis_dir(interval, period, strategy)
