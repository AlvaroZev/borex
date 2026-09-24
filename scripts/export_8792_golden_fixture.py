"""Export 8792 golden fixture from Railway backup + local MT5 cache.

Produces immutable artifacts under:
  data/runs/mirror_2d_8792/reconciliation/golden_fixture/

Does NOT overwrite prior reconciliation artifacts.
Does NOT call the broker / 8791.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
load_dotenv(LIVE / ".env")

OUT = ROOT / "data" / "runs" / "mirror_2d_8792" / "reconciliation" / "golden_fixture"
ENTRY_START = pd.Timestamp("2026-08-27 01:00:00", tz="UTC")
LIVE_END = pd.Timestamp("2026-08-28 23:00:00", tz="UTC")
WINDOW_START = "2026-08-27"
WINDOW_END = "2026-08-29"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_dump(path: Path, obj: Any) -> str:
    raw = json.dumps(obj, indent=2, default=str, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return _sha256_bytes(raw)


def db_url() -> str:
    return (
        os.environ.get("MIRROR_DATABASE_BACKUP_URL")
        or os.environ.get("DATABASE_BACKUP_URL")
        or os.environ.get("RAILWAY_DATABASE_URL")
        or os.environ.get("MIRROR_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    )


def write_input_path_trace() -> dict:
    """Document where Theory actually gets H1 bars (code-path truth)."""
    trace = {
        "question": "Does Theory consume live_candles DB rows as engine inputs?",
        "answer": "NO — not for Mirror/8792 Theory bar walk.",
        "mirror_8792_path": {
            "service": "borex_mirror.service.MirrorService.start",
            "load": "borex_live.data.feed.load_universe → bootstrap_candles",
            "live_connected": (
                "MT5 only via Mt5Client.fetch_bars; Dukascopy/YF NOT mixed when MT5 connected"
            ),
            "transforms": [
                "_drop_forming_bar (remove incomplete last bar vs wall clock)",
                "_trim to warmup_bars limit (default 10000)",
                "append new closed bars in process_once/_ingest_new_bars",
            ],
            "persistence": (
                "ShadowEngine.checkpoint → mirror_theory_state.state_blob "
                "(strategy+portfolio+indices). Bars themselves are NOT stored as "
                "the Theory input stream in live_candles for mirror."
            ),
        },
        "live_candles_table": {
            "purpose": (
                "Candles ingested live from MT5 AFTER service start for mt5service "
                "API/debug (excludes warmup/backdata). See models.LiveCandle docstring."
            ),
            "8792_window_h1_count_on_backup": "filled at export time",
            "implication": (
                "Same database rows ≠ same engine inputs. Fixture must use MT5-derived "
                "H1 (cache/fetch) labeled as SURROGATE unless a bar snapshot was saved "
                "at runtime."
            ),
        },
        "timezone": "UTC after MT5 offset correction in feed/client",
        "h1_boundary": ":00 UTC closed bars",
    }
    return trace


def export_db_snapshot(eng) -> dict:
    out: dict[str, Any] = {"source": "railway_backup_or_configured_url", "tables": {}}

    with eng.connect() as c:
        # live_candles stats
        lc_window = c.execute(
            text(
                "select count(*) from live_candles "
                "where interval='1h' and ts >= :a and ts < :b"
            ),
            {"a": WINDOW_START, "b": WINDOW_END},
        ).scalar()
        lc_total = c.execute(text("select count(*) from live_candles")).scalar()
        lc_intervals = dict(
            c.execute(
                text(
                    "select interval, count(*) from live_candles group by interval order by 1"
                )
            ).fetchall()
        )
        out["tables"]["live_candles"] = {
            "total": lc_total,
            "h1_in_8792_window": lc_window,
            "by_interval": lc_intervals,
            "note": (
                "If h1_in_8792_window==0, live_candles cannot be the Theory H1 stream "
                "for this window."
            ),
        }

        # mirror theory state (END-of-window checkpoint, not start)
        st = c.execute(
            text(
                "select strategy, config_hash, initial_capital, cash, equity, "
                "last_master_ts, updated_at, state_blob from mirror_theory_state where id=1"
            )
        ).mappings().first()
        if st:
            blob = bytes(st["state_blob"])
            blob_path = OUT / "mirror_theory_state_blob.pkl"
            blob_path.write_bytes(blob)
            meta = {
                "strategy": st["strategy"],
                "config_hash": st["config_hash"],
                "initial_capital": float(st["initial_capital"]),
                "cash": float(st["cash"]),
                "equity": float(st["equity"]),
                "last_master_ts": st["last_master_ts"],
                "updated_at": str(st["updated_at"]),
                "blob_sha256": _sha256_bytes(blob),
                "blob_bytes": len(blob),
                "blob_path": str(blob_path.name),
                "checkpoint_role": (
                    "END-of-window (or last live) checkpoint — NOT proven to be "
                    "state immediately before first reconciliation bar"
                ),
                "availability": "confirmed_from_db",
            }
            out["tables"]["mirror_theory_state"] = meta

            # Inspect blob without inventing fields
            try:
                payload = pickle.loads(blob)
                inspect = {
                    "keys": sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
                    "version": payload.get("version") if isinstance(payload, dict) else None,
                }
                if isinstance(payload, dict):
                    strat = payload.get("strategy")
                    pf = payload.get("portfolio")
                    indices = payload.get("indices") or {}
                    inspect["strategy_type"] = type(strat).__name__
                    inspect["strategy_name"] = getattr(strat, "name", None)
                    inspect["ghost_sl_mult"] = getattr(strat, "ghost_sl_mult", None)
                    inspect["min_rr"] = getattr(strat, "min_rr", None)
                    pending = getattr(strat, "_pending", {}) or {}
                    inspect["pending_ghost_pairs"] = sorted(pending.keys())
                    inspect["pending_ghost_n"] = len(pending)
                    inspect["portfolio_type"] = type(pf).__name__
                    inspect["portfolio_cash"] = float(getattr(pf, "cash", 0) or 0)
                    inspect["portfolio_initial_capital"] = float(
                        getattr(pf, "initial_capital", 0) or 0
                    )
                    open_t = getattr(pf, "open_trades", {}) or {}
                    closed_t = getattr(pf, "closed_trades", []) or []
                    inspect["open_positions_n"] = len(open_t)
                    inspect["open_pairs"] = sorted(open_t.keys())
                    inspect["closed_trades_n"] = len(closed_t)
                    inspect["indices_n"] = len(indices)
                    # sample pending geometry (real fields only)
                    samples = []
                    for sym, p in list(pending.items())[:5]:
                        samples.append(
                            {
                                "pair": sym,
                                "action": str(getattr(p.action, "value", p.action)),
                                "planned_entry": float(p.planned_entry),
                                "stop_loss": float(p.stop_loss),
                                "take_profit": float(p.take_profit),
                                "created_index": int(p.created_index),
                                "expires_index": int(p.expires_index),
                            }
                        )
                    inspect["pending_samples"] = samples
                    open_samples = []
                    for sym, t in list(open_t.items())[:5]:
                        open_samples.append(
                            {
                                "pair": sym,
                                "side": str(getattr(t.side, "value", t.side)),
                                "entry_price": float(t.entry_price),
                                "entry_time": str(t.entry_time),
                                "stop_loss": float(t.stop_loss or 0),
                                "take_profit": float(t.take_profit or 0),
                                "margin": float(t.margin or 0),
                            }
                        )
                    inspect["open_samples"] = open_samples
                (OUT / "mirror_theory_state_inspect.json").write_text(
                    json.dumps(inspect, indent=2, default=str), encoding="utf-8"
                )
                out["tables"]["mirror_theory_state"]["inspect_path"] = (
                    "mirror_theory_state_inspect.json"
                )
            except Exception as e:
                out["tables"]["mirror_theory_state"]["inspect_error"] = str(e)

        # mirror theory trades in window
        trades = c.execute(
            text(
                "select symbol, side, pattern, entry_price, stop_loss, take_profit, "
                "margin, rr_used, commission, status, entry_time, exit_price, "
                "exit_time, exit_reason, pnl, entry_index, exit_index "
                "from mirror_theory_trades "
                "where entry_time >= :a and entry_time < :b "
                "order by entry_time, symbol"
            ),
            {"a": WINDOW_START, "b": WINDOW_END},
        ).mappings().all()
        trade_rows = [dict(r) for r in trades]
        trades_hash = _json_dump(OUT / "mirror_theory_trades_window.json", trade_rows)
        out["tables"]["mirror_theory_trades"] = {
            "n_window": len(trade_rows),
            "sha256": trades_hash,
            "entry_min": trade_rows[0]["entry_time"] if trade_rows else None,
            "entry_max": trade_rows[-1]["entry_time"] if trade_rows else None,
            "status_counts": dict(Counter(str(r.get("status")) for r in trade_rows)),
            "exit_reason_counts": dict(
                Counter(str(r.get("exit_reason")) for r in trade_rows)
            ),
        }

        # portfolio
        pf = c.execute(
            text("select cash, initial_capital, updated_at from mirror_portfolio where id=1")
        ).mappings().first()
        out["tables"]["mirror_portfolio"] = dict(pf) if pf else None

        # service_runs — look for alexg8 / 8792 configs
        cols = [
            r[0]
            for r in c.execute(
                text(
                    "select column_name from information_schema.columns "
                    "where table_name='service_runs' order by ordinal_position"
                )
            )
        ]
        select_cols = [
            cname
            for cname in (
                "id",
                "strategy",
                "config_json",
                "started_at",
                "created_at",
                "port",
                "host",
            )
            if cname in cols
        ]
        runs = c.execute(
            text(
                f"select {', '.join(select_cols)} from service_runs "
                "order by id desc limit 30"
            )
        ).mappings().all()
        run_rows = []
        for r in runs:
            row = {k: r.get(k) for k in select_cols}
            for k, v in list(row.items()):
                if hasattr(v, "isoformat"):
                    row[k] = str(v)
            run_rows.append(row)
        _json_dump(OUT / "service_runs_recent.json", run_rows)
        out["tables"]["service_runs"] = {
            "n_exported": len(run_rows),
            "columns": cols,
            "alexg8_ids": [
                r.get("id") for r in run_rows if str(r.get("strategy")) == "alexg8"
            ],
        }

        # theory_state (mt5service — different product)
        ts = c.execute(
            text(
                "select strategy, config_hash, initial_capital, cash, equity, "
                "last_master_ts, updated_at from theory_state where id=1"
            )
        ).mappings().first()
        out["tables"]["theory_state_mt5service"] = {
            **({k: (str(v) if hasattr(v, "isoformat") else v) for k, v in dict(ts).items()} if ts else {}),
            "note": "Separate from mirror 8792; do not confuse with mirror_theory_state",
        }

    return out


def export_mt5_cache_surrogate(symbols: list[str] | None = None) -> dict:
    """Export H1 from utc_v2 cache as SURROGATE engine input (not proven live identical)."""
    from borex.viewerMT5.mt5_feed import _cache_path, _frame_to_candles, _slice_candles

    prior = ROOT / "data" / "runs" / "mirror_2d_8792" / "backtest.json"
    if symbols is None and prior.exists():
        symbols = sorted(
            (json.loads(prior.read_text(encoding="utf-8")).get("spread_pips_by_symbol") or {}).keys()
        )
    symbols = symbols or []

    start = (ENTRY_START - timedelta(days=400)).to_pydatetime()
    end = (LIVE_END + timedelta(hours=2)).to_pydatetime()
    candles_dir = OUT / "h1_surrogate_cache"
    candles_dir.mkdir(parents=True, exist_ok=True)

    per_pair = {}
    missing_pairs = []
    all_bytes = hashlib.sha256()
    for sym in symbols:
        cache_file = _cache_path(sym, "1h")
        if not cache_file.is_file():
            missing_pairs.append(sym)
            continue
        df = pd.read_parquet(cache_file)
        candles = [
            c
            for c in _frame_to_candles(df)
            if pd.Timestamp(c.timestamp).tz_convert("UTC").minute == 0
            and pd.Timestamp(c.timestamp).tz_convert("UTC").second == 0
        ]
        sliced = _slice_candles(candles, start, end)
        rows = [
            {
                "timestamp": str(c.timestamp),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume or 0),
            }
            for c in sliced
        ]
        safe = sym.replace("=", "").replace("/", "_")
        path = candles_dir / f"{safe}.json"
        raw = json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode()
        path.write_bytes(raw)
        all_bytes.update(raw)
        # duplicates / gaps
        ts_list = [pd.Timestamp(r["timestamp"]) for r in rows]
        dup = len(ts_list) - len(set(ts_list))
        gaps = 0
        for a, b in zip(ts_list, ts_list[1:]):
            if (b - a) > pd.Timedelta(hours=1):
                # ignore weekend gaps roughly
                if b.weekday() < 5 and a.weekday() < 5 and (b - a) <= pd.Timedelta(hours=72):
                    if (b - a) > pd.Timedelta(hours=1):
                        gaps += 1
        per_pair[sym] = {
            "n": len(rows),
            "earliest": rows[0]["timestamp"] if rows else None,
            "latest": rows[-1]["timestamp"] if rows else None,
            "duplicates": dup,
            "weekday_gap_edges": gaps,
            "file": path.name,
            "sha256": _sha256_bytes(raw),
        }

    meta = {
        "role": "SURROGATE",
        "provenance": (
            "MT5 utc_v2 parquet cache under borex-main/data/cache/mt5/utc_v2. "
            "NOT proven byte-identical to the in-memory series Theory saw on Aug 27–28."
        ),
        "timezone": "UTC",
        "h1_boundary": ":00",
        "forming_bar": "cache stores closed bars; live also drops forming bar at ingest",
        "warmup_days_exported": 400,
        "window": {"entry_start": str(ENTRY_START), "live_end": str(LIVE_END)},
        "n_pairs": len(per_pair),
        "missing_pairs": missing_pairs,
        "aggregate_sha256": all_bytes.hexdigest(),
        "per_pair": per_pair,
    }
    _json_dump(OUT / "h1_surrogate_manifest.json", meta)
    return meta


def build_config_snapshot(db_snap: dict, blob_inspect_path: Path) -> dict:
    """Classify knobs with evidence. Compute candidate config hashes."""
    from borex_live.config import LiveServiceConfig
    from borex_live.engine.shadow_engine import ShadowEngine
    from borex.backtest.engine import BacktestConfig
    from borex_live.strategy_registry import create_strategy

    st = (db_snap.get("tables") or {}).get("mirror_theory_state") or {}
    target_hash = st.get("config_hash")

    inspect = {}
    if blob_inspect_path.exists():
        inspect = json.loads(blob_inspect_path.read_text(encoding="utf-8"))

    knobs = {
        "strategy": {
            "value": st.get("strategy") or "alexg8",
            "class": "confirmed",
            "evidence": "mirror_theory_state.strategy",
        },
        "ghost_sl_mult": {
            "value": inspect.get("ghost_sl_mult"),
            "class": "confirmed" if inspect.get("ghost_sl_mult") is not None else "unknown",
            "evidence": "state_blob.strategy.ghost_sl_mult",
        },
        "min_rr": {
            "value": inspect.get("min_rr"),
            "class": "confirmed" if inspect.get("min_rr") is not None else "unknown",
            "evidence": "state_blob.strategy.min_rr",
        },
        "initial_capital": {
            "value": st.get("initial_capital"),
            "class": "confirmed",
            "evidence": "mirror_theory_state.initial_capital (END checkpoint)",
        },
        "cash_at_checkpoint": {
            "value": st.get("cash"),
            "class": "confirmed",
            "evidence": "mirror_theory_state.cash at last_master_ts",
        },
        "last_master_ts": {
            "value": st.get("last_master_ts"),
            "class": "confirmed",
            "evidence": "mirror_theory_state",
        },
        "config_hash_live": {
            "value": target_hash,
            "class": "confirmed",
            "evidence": "mirror_theory_state.config_hash",
        },
        # From prior 8792 recon geometry / exits — may not match this config_hash
        "leverage": {
            "value": 5000.0,
            "class": "reconstructed",
            "evidence": "8792 theory SL distance ≈ 1/5000 on dump trades",
        },
        "rr_mode": {
            "value": "dynamic",
            "class": "reconstructed",
            "evidence": "rr_used distribution in dump",
        },
        "rr_min": {
            "value": 2.0,
            "class": "reconstructed",
            "evidence": "rr_used floor in dump",
        },
        "rr_max": {
            "value": 6.0,
            "class": "reconstructed",
            "evidence": "rr_used ceiling in dump",
        },
        "rr_factor": {
            "value": 1.0,
            "class": "assumed",
            "evidence": "prior recon assumption; verify via config_hash match",
        },
        "force_flat_daily": {
            "value": True,
            "class": "reconstructed",
            "evidence": "daily_close exits in theory book",
        },
        "force_flat_utc_hour": {
            "value": 19,
            "class": "assumed",
            "evidence": "engine default",
        },
        "force_flat_friday": {
            "value": False,
            "class": "assumed",
            "evidence": "prior recon",
        },
        "commission_per_lot": {
            "value": 7.0,
            "class": "reconstructed",
            "evidence": "broker ~$3.50/side",
        },
        "risk_include_commission": {
            "value": True,
            "class": "assumed",
            "evidence": "not in dump; affects config_hash",
        },
        "commission_at_entry": {
            "value": False,
            "class": "assumed",
            "evidence": "not in dump; affects config_hash",
        },
        "max_positions": {
            "value": 60,
            "class": "assumed",
            "evidence": "CLI default",
        },
        "position_size_pct": {
            "value": 0.01,
            "class": "assumed",
            "evidence": "CLI default",
        },
        "spread_pips": {
            "value": 0.0,
            "class": "confirmed",
            "evidence": "MirrorEngine BacktestConfig does not set spreads",
        },
        "data_source_theory": {
            "value": "MT5 via load_universe (not live_candles)",
            "class": "confirmed",
            "evidence": "feed.py bootstrap_candles when MT5 connected",
        },
        "warmup_bars": {
            "value": 10000,
            "class": "assumed",
            "evidence": "MirrorConfig/LiveServiceConfig default",
        },
    }

    # Brute a few config variants to match live config_hash
    candidates = []
    base = dict(
        strategy="alexg8",
        interval="1h",
        leverage=5000.0,
        position_size_pct=0.01,
        max_positions=60,
        min_rr=float(inspect.get("min_rr") or 2.0),
        rr_factor=1.0,
        same_bar_exit=False,
        commission_per_lot=7.0,
        risk_include_commission=True,
        winrate_min_trades=20,
        rr_mode="dynamic",
        commission_at_entry=False,
        force_flat_friday=False,
        force_flat_daily=True,
        force_flat_utc_hour=19,
    )
    variants = [
        {},
        {"rr_factor": 1.88},
        {"min_rr": 3.0},
        {"min_rr": 3.0, "rr_factor": 1.88},
        {"risk_include_commission": False},
        {"commission_at_entry": True},
        {"risk_include_commission": False, "commission_at_entry": True},
        {"force_flat_friday": True},
        {"rr_min": 2.0, "rr_max": 6.0},  # may not be in hash fields
        {"initial_capital_note": st.get("initial_capital")},
    ]

    def cfg_hash_for(vals: dict) -> str:
        live_cfg = LiveServiceConfig(
            strategy=vals.get("strategy", base["strategy"]),
            capital=float(st.get("initial_capital") or 1000),
            leverage=float(vals.get("leverage", base["leverage"])),
            min_rr=float(vals.get("min_rr", base["min_rr"])),
            rr_factor=float(vals.get("rr_factor", base["rr_factor"])),
            rr_mode=str(vals.get("rr_mode", base["rr_mode"])),
            position_size_pct=float(vals.get("position_size_pct", base["position_size_pct"])),
            max_positions=int(vals.get("max_positions", base["max_positions"])),
            interval=str(vals.get("interval", base["interval"])),
            same_bar_exit=bool(vals.get("same_bar_exit", base["same_bar_exit"])),
            commission_per_lot=float(vals.get("commission_per_lot", base["commission_per_lot"])),
            risk_include_commission=bool(
                vals.get("risk_include_commission", base["risk_include_commission"])
            ),
            winrate_min_trades=int(vals.get("winrate_min_trades", base["winrate_min_trades"])),
            commission_at_entry=bool(
                vals.get("commission_at_entry", base["commission_at_entry"])
            ),
            force_flat_friday=bool(vals.get("force_flat_friday", base["force_flat_friday"])),
            force_flat_daily=bool(vals.get("force_flat_daily", base["force_flat_daily"])),
            force_flat_utc_hour=int(
                vals.get("force_flat_utc_hour", base["force_flat_utc_hour"])
            ),
            rr_min=float(vals.get("rr_min", 0.0) or 0.0),
            rr_max=float(vals.get("rr_max", 0.0) or 0.0),
        )
        strategy, _ = create_strategy(
            live_cfg.strategy,
            min_rr=live_cfg.min_rr,
            second_signal="off",
            execution_interval=live_cfg.interval,
        )
        bt = BacktestConfig(
            initial_capital=live_cfg.capital,
            leverage=live_cfg.leverage,
            position_size_pct=live_cfg.position_size_pct,
            size_mode="margin",
            true_sl=True,
            true_sl_rr=live_cfg.min_rr,
            rr_mode=live_cfg.rr_mode,
            rr_factor=live_cfg.rr_factor,
            rr_min=live_cfg.rr_min,
            rr_max=live_cfg.rr_max,
            commission_per_lot=live_cfg.commission_per_lot,
            risk_include_commission=live_cfg.risk_include_commission,
            commission_at_entry=live_cfg.commission_at_entry,
            force_flat_friday=live_cfg.force_flat_friday,
            force_flat_daily=live_cfg.force_flat_daily,
            force_flat_utc_hour=live_cfg.force_flat_utc_hour,
            winrate_min_trades=live_cfg.winrate_min_trades,
        )
        eng = ShadowEngine(strategy, live_cfg, bt)
        return eng.config_hash, {
            k: getattr(live_cfg, k)
            for k in (
                "strategy",
                "interval",
                "leverage",
                "position_size_pct",
                "max_positions",
                "min_rr",
                "rr_factor",
                "same_bar_exit",
                "commission_per_lot",
                "risk_include_commission",
                "winrate_min_trades",
                "rr_mode",
                "commission_at_entry",
                "force_flat_friday",
                "force_flat_daily",
                "force_flat_utc_hour",
            )
        }

    matched = None
    for var in variants:
        vals = {**base, **{k: v for k, v in var.items() if k != "initial_capital_note"}}
        try:
            h, fields = cfg_hash_for(vals)
        except Exception as e:
            candidates.append({"variant": var, "error": str(e)})
            continue
        row = {"variant": var, "config_hash": h, "match": h == target_hash, "fields": fields}
        candidates.append(row)
        if h == target_hash and matched is None:
            matched = row

    snap = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_config_hash": target_hash,
        "hash_match_found": matched is not None,
        "matched_variant": matched,
        "knobs": knobs,
        "hash_candidates_tried": candidates,
        "hash_fields_note": (
            "ShadowEngine._config_hash uses LiveServiceConfig fields listed in "
            "shadow_engine.py — rr_min/rr_max are included in LiveServiceConfig but "
            "verify whether they participate in the hash."
        ),
    }
    _json_dump(OUT / "config_snapshot.json", snap)
    return snap


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    url = db_url()
    if not url:
        raise SystemExit("No database URL configured")

    print("Connecting…", url.split("@")[-1], flush=True)
    eng = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 20})

    input_trace = write_input_path_trace()
    db_snap = export_db_snapshot(eng)
    input_trace["live_candles_table"]["8792_window_h1_count_on_backup"] = (
        db_snap["tables"]["live_candles"]["h1_in_8792_window"]
    )
    _json_dump(OUT / "input_path_trace.json", input_trace)
    _json_dump(OUT / "db_snapshot_meta.json", db_snap)

    print("Exporting MT5 cache surrogate H1…", flush=True)
    h1_meta = export_mt5_cache_surrogate()

    config_snap = build_config_snapshot(
        db_snap, OUT / "mirror_theory_state_inspect.json"
    )

    # Blocking assessment
    blocking = []
    if db_snap["tables"]["live_candles"]["h1_in_8792_window"] == 0:
        blocking.append(
            "live_candles has 0 H1 bars in 8792 window — cannot recover exact "
            "DB candle stream from that table; Theory used MT5 in-memory series."
        )
    st = db_snap["tables"].get("mirror_theory_state") or {}
    if st.get("last_master_ts"):
        ts = pd.Timestamp(st["last_master_ts"])
        if ts >= ENTRY_START:
            blocking.append(
                f"mirror_theory_state checkpoint is at {st['last_master_ts']} "
                "(at/after window), not immediately before first reconciliation bar. "
                "START-of-window checkpoint is MISSING from available DB."
            )
    if not config_snap.get("hash_match_found"):
        blocking.append(
            "Could not reproduce mirror_theory_state.config_hash from tried knob "
            "variants — runtime config not fully recovered."
        )

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"entry_start": str(ENTRY_START), "live_end": str(LIVE_END)},
        "db_host": url.split("@")[-1],
        "artifacts": sorted(p.name for p in OUT.iterdir() if p.is_file())
        + [f"h1_surrogate_cache/ ({h1_meta['n_pairs']} files)"],
        "hashes": {
            "h1_surrogate_aggregate": h1_meta.get("aggregate_sha256"),
            "mirror_theory_state_blob": st.get("blob_sha256"),
            "mirror_theory_trades_window": db_snap["tables"]["mirror_theory_trades"][
                "sha256"
            ],
            "target_config_hash": config_snap.get("target_config_hash"),
        },
        "recovered": {
            "end_checkpoint_blob": bool(st.get("blob_path")),
            "window_theory_trades": db_snap["tables"]["mirror_theory_trades"]["n_window"],
            "exact_live_candles_h1_stream": False,
            "start_of_window_checkpoint": False,
            "config_hash_matched": config_snap.get("hash_match_found"),
            "mt5_cache_surrogate_h1": True,
        },
        "blocking_issues": blocking,
        "case_a_vs_b_status": (
            "INPUT stream not proven identical (Case A still open). "
            "START state not recovered. Cannot yet claim Case B (same inputs, "
            "different transition)."
        ),
        "next_required": (
            "Obtain (1) H1 bar snapshot identical to Theory's in-memory series "
            "OR prove MT5 cache == that series; (2) theory checkpoint at/just before "
            "2026-08-27 01:00 UTC; (3) config_hash-matching knobs. Then run "
            "checkpoint parity."
        ),
    }
    _json_dump(OUT / "MANIFEST.json", manifest)
    print(json.dumps(manifest, indent=2), flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
