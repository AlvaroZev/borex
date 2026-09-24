"""Prove golden-fixture replay determinism: Replay A == Replay B.

Consumes only the fixture tree (initial_state.pkl + h1_input bundles).
Does not touch live DB / MT5 / 8791.

Exit nonzero if any divergence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))


def _load_fixture_candles(fix: Path) -> tuple[str, dict[str, list], list[dict]]:
    """Load full_series (prefer exact pickle) for bit-faithful replay."""
    from borex.models.candle import Candle

    def _load_series(label: str) -> tuple[str, dict[str, list]]:
        series_dir = fix / "h1_input" / label
        sm = json.loads((series_dir / "_manifest.json").read_text(encoding="utf-8"))
        master_symbol = sm.get("master_symbol") or ""
        pkl = series_dir / "_series.pkl"
        if pkl.exists():
            series = pickle.loads(pkl.read_bytes())
            if not master_symbol:
                master_symbol = max(series, key=lambda s: len(series[s]))
            return master_symbol, series
        series = {}
        for sym, meta in (sm.get("pairs") or {}).items():
            rows = json.loads((series_dir / meta["file"]).read_text(encoding="utf-8"))
            series[sym] = [
                Candle(
                    timestamp=r["timestamp"],
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r.get("volume") or 0),
                )
                for r in rows
            ]
        if not master_symbol:
            master_symbol = max(series, key=lambda s: len(series[s]))
        return master_symbol, series

    full_dir = fix / "h1_input" / "full_series"
    if (full_dir / "_manifest.json").exists():
        master_symbol, series = _load_series("full_series")
    else:
        master_symbol, series = _load_series("warmup_series")

    steps = []
    for bdir in sorted((fix / "h1_input").iterdir()):
        if not bdir.is_dir() or bdir.name in {"warmup_series", "full_series"}:
            continue
        meta = bdir / "_bundle.json"
        if not meta.exists():
            continue
        bundle = json.loads(meta.read_text(encoding="utf-8"))
        steps.append(
            {
                "master_symbol": master_symbol,
                "master_index": int(bundle["master_index"]),
                "master_ts": bundle["master_ts"],
                "input_hash": bundle["input_hash"],
                "sequence": bundle["sequence"],
            }
        )
    if not steps:
        raise SystemExit(f"No h1_input step bundles in {fix}")
    return master_symbol, series, steps


def _restore_shadow(fix: Path):
    from borex.backtest.engine import BacktestConfig
    from borex_live.config import LiveServiceConfig
    from borex_live.engine.shadow_engine import ShadowEngine
    from borex_live.strategy_registry import create_strategy

    cfg_json = json.loads((fix / "config.json").read_text(encoding="utf-8"))
    live_fields = cfg_json.get("live_or_mirror") or {}
    # LiveServiceConfig may not accept all mirror fields — filter
    allowed = {
        "strategy",
        "capital",
        "leverage",
        "min_rr",
        "rr_factor",
        "rr_mode",
        "rr_min",
        "rr_max",
        "same_bar_exit",
        "commission_per_lot",
        "risk_include_commission",
        "commission_at_entry",
        "force_flat_daily",
        "force_flat_utc_hour",
        "force_flat_friday",
        "max_positions",
        "position_size_pct",
        "interval",
    }
    kwargs = {k: v for k, v in live_fields.items() if k in allowed and v is not None}
    kwargs.setdefault("strategy", "alexg8")
    live_cfg = LiveServiceConfig(**kwargs)
    strategy, _ = create_strategy(
        live_cfg.strategy,
        min_rr=float(getattr(live_cfg, "min_rr", 2) or 2),
        second_signal=False,
        execution_interval=str(getattr(live_cfg, "interval", "1h") or "1h"),
    )
    bt_fields = cfg_json.get("backtest") or {}
    bt_allowed = {
        "initial_capital",
        "leverage",
        "position_size_pct",
        "size_mode",
        "true_sl",
        "true_sl_rr",
        "rr_mode",
        "rr_factor",
        "rr_min",
        "rr_max",
        "commission_per_lot",
        "risk_include_commission",
        "commission_at_entry",
        "force_flat_daily",
        "force_flat_utc_hour",
        "force_flat_friday",
    }
    bt_kwargs = {k: v for k, v in bt_fields.items() if k in bt_allowed and v is not None}
    bt_kwargs.setdefault("initial_capital", float(getattr(live_cfg, "capital", 1000) or 1000))
    bt = BacktestConfig(**bt_kwargs)
    shadow = ShadowEngine(strategy, live_cfg, bt)

    blob = pickle.loads((fix / "initial_state.pkl").read_bytes())
    shadow.strategy = blob["strategy"]
    shadow.portfolio = blob["portfolio"]
    shadow.engine.strategy = shadow.strategy
    shadow._last_indices = dict(blob.get("indices") or {})
    shadow.last_master_ts = str(blob.get("last_master_ts") or "")
    shadow._persisted_closed_count = len(shadow.portfolio.closed_trades)
    return shadow


def _run_once(fix: Path) -> dict:
    from borex_live.engine.state_checksum import master_input_hash, state_hash

    shadow = _restore_shadow(fix)
    master_symbol, series, steps = _load_fixture_candles(fix)

    checksums = []
    signals = []
    for step in steps:
        mi = int(step["master_index"])
        # Match capture: positional prefix by master index; short pairs keep full length.
        candles = {
            sym: (bars[: mi + 1] if len(bars) > mi else list(bars))
            for sym, bars in series.items()
            if bars
        }
        if master_symbol not in candles or len(candles[master_symbol]) <= mi:
            raise RuntimeError(
                f"Incomplete fixture series for master={master_symbol} mi={mi} "
                f"len={len(candles.get(master_symbol) or [])}"
            )

        # Recompute input hash from loaded series (do not trust copied bundle hash alone)
        from borex.alexg.multi_market import align_symbols_to_timeline

        master = candles[master_symbol]
        ts_maps = align_symbols_to_timeline(master, candles)
        ts = master[mi].timestamp
        indices = {
            symbol: mapping[ts]
            for symbol, mapping in ts_maps.items()
            if ts in mapping
        }
        ih = master_input_hash(master_symbol, mi, candles, indices)

        before_closed = len(shadow.portfolio.closed_trades)
        before_open = set(shadow.portfolio.open_trades.keys())
        before_pending = set((getattr(shadow.strategy, "_pending", {}) or {}).keys())
        shadow.step_master_bar(mi, candles, master_symbol)
        # Match MirrorEngine / live capture: checkpoint clears derived strategy caches
        # between bars. These caches are excluded from state_hash but affect on_bar.
        shadow._clear_derived_strategy_cache()
        sh = state_hash(shadow)
        checksums.append(
            {
                "sequence": step["sequence"],
                "master_ts": step["master_ts"],
                "state_hash": sh,
                "input_hash": ih,
                "input_hash_fixture": step["input_hash"],
                "input_hash_match_fixture": ih == step["input_hash"],
            }
        )
        for sym in sorted(set(shadow.portfolio.open_trades) - before_open):
            t = shadow.portfolio.open_trades[sym]
            signals.append(
                {
                    "kind": "fill_open",
                    "symbol": sym,
                    "entry_time": str(t.entry_time),
                    "pattern": str(t.pattern or ""),
                }
            )
        for t in shadow.portfolio.closed_trades[before_closed:]:
            signals.append(
                {
                    "kind": "fill_close",
                    "symbol": t.symbol,
                    "entry_time": str(t.entry_time),
                    "exit_time": str(getattr(t, "exit_time", "") or ""),
                    "pnl": float(getattr(t, "pnl", 0) or 0),
                    "pattern": str(getattr(t, "pattern", "") or ""),
                }
            )
        after_pending = set((getattr(shadow.strategy, "_pending", {}) or {}).keys())
        for sym in sorted(after_pending - before_pending):
            p = shadow.strategy._pending[sym]
            signals.append(
                {
                    "kind": "ghost_arm",
                    "symbol": sym,
                    "pattern": str(p.pattern),
                    "planned_entry": float(p.planned_entry),
                    "stop_loss": float(p.stop_loss),
                }
            )
        for sym in sorted(before_pending - after_pending):
            signals.append({"kind": "ghost_clear", "symbol": sym})

    final = {
        "checksums": checksums,
        "signals": signals,
        "final_state_hash": checksums[-1]["state_hash"] if checksums else "",
        "n_closed": len(shadow.portfolio.closed_trades),
        "n_open": len(shadow.portfolio.open_trades),
        "cash": float(shadow.portfolio.cash),
    }
    raw = json.dumps(final, sort_keys=True, separators=(",", ":")).encode()
    final["fingerprint"] = hashlib.sha256(raw).hexdigest()
    return final


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--fixture",
        required=True,
        help="Path to golden_fixture directory",
    )
    args = ap.parse_args()
    fix = Path(args.fixture)
    a = _run_once(fix)
    b = _run_once(fix)
    out = fix / "determinism"
    out.mkdir(parents=True, exist_ok=True)
    (out / "replay_a.json").write_text(json.dumps(a, indent=2), encoding="utf-8")
    (out / "replay_b.json").write_text(json.dumps(b, indent=2), encoding="utf-8")
    ok = a["fingerprint"] == b["fingerprint"]
    report = {
        "deterministic": ok,
        "fingerprint_a": a["fingerprint"],
        "fingerprint_b": b["fingerprint"],
        "n_bars": len(a["checksums"]),
        "final_state_hash": a["final_state_hash"],
    }
    (out / "determinism_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
