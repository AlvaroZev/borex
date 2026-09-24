"""Gate 2 — execution fidelity on frozen Layer-A signal tape.

Window: mirror_live_20260908_043045
Hard rules:
  - Do NOT regenerate alexg8 / patterns / occupancy
  - Do NOT optimize strategy
  - Do NOT introduce Raw spreads silently
  - Separate Layer B (execution) vs Layer C (accounting)

Reference broker:
  Golden window used execute_mt5=False → paper mirror only.
  Real MT5 deals for magic 88002 are probed; if absent, reference is labeled
  THEORY_SHADOW_H1 (Layer-A Theory fills/exits) and PAPER_MIRROR opens.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import statistics
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path[:0] = [str(ROOT), str(LIVE), str(ROOT / "scripts")]
load_dotenv(LIVE / ".env")

WINDOW = ROOT / "data" / "runs" / "golden_windows" / "mirror_live_20260908_043045"
FIX = WINDOW / "golden_fixture"
OUT = WINDOW / "execution_fidelity"
COMMISSION_PER_LOT = 7.0
LOT_NOTIONAL = 100_000.0
LEVERAGE = 5000.0
NO_RAW_SPREAD = True  # Theory BacktestConfig had Raw spreads = 0


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _side(x) -> str:
    s = str(getattr(x, "value", x) or "").lower()
    if s in ("buy", "long"):
        return "long"
    if s in ("sell", "short"):
        return "short"
    return s


def _err_stats(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {
            "n": 0,
            "mean_signed": None,
            "mae": None,
            "median": None,
            "p95": None,
            "max_abs": None,
            "total": None,
        }
    a = sorted(abs(v) for v in vals)
    p95 = a[min(len(a) - 1, max(0, int(math.ceil(0.95 * len(a)) - 1)))]
    return {
        "n": len(vals),
        "mean_signed": float(sum(vals) / len(vals)),
        "mae": float(sum(a) / len(a)),
        "median": float(statistics.median(vals)),
        "p95": float(p95),
        "max_abs": float(max(a)),
        "total": float(sum(vals)),
    }


def freeze_signal_tape(fix: Path | None = None) -> dict[str, Any]:
    """Immutable tape from validated Layer-A capture (no regeneration)."""
    from replay_golden_fixture_twice import _load_fixture_candles, _restore_shadow

    fixture = Path(fix) if fix is not None else FIX
    shadow = _restore_shadow(fixture)
    master, series, steps = _load_fixture_candles(fixture)
    events: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    eid = 0

    for step in steps:
        mi = int(step["master_index"])
        candles = {
            s: (b[: mi + 1] if len(b) > mi else list(b))
            for s, b in series.items()
            if b
        }
        before_hash = __import__(
            "borex_live.engine.state_checksum", fromlist=["state_hash"]
        ).state_hash(shadow)
        before_open = set(shadow.portfolio.open_trades)
        before_closed = len(shadow.portfolio.closed_trades)
        before_pending = set(getattr(shadow.strategy, "_pending", {}) or {})
        occ_before = {
            "n_open": len(shadow.portfolio.open_trades),
            "symbols": sorted(shadow.portfolio.open_trades),
            "cash": float(shadow.portfolio.cash),
        }

        shadow.step_master_bar(mi, candles, master)
        shadow._clear_derived_strategy_cache()
        after_hash = __import__(
            "borex_live.engine.state_checksum", fromlist=["state_hash"]
        ).state_hash(shadow)
        master_ts = str(candles[master][mi].timestamp)

        for sym in sorted(set(shadow.portfolio.open_trades) - before_open):
            t = shadow.portfolio.open_trades[sym]
            eid += 1
            # Parse ghost geometry from pattern if present
            ghost_entry = ghost_sl = ghost_tp = None
            pat = str(t.pattern or "")
            if "|g:" in pat:
                try:
                    g = pat.split("|g:")[1].split("|")[0]
                    parts = g.split(":")
                    if len(parts) >= 4:
                        ghost_entry = float(parts[1])
                        ghost_sl = float(parts[2])
                        ghost_tp = float(parts[3])
                except Exception:
                    pass
            structural_risk = None
            if t.stop_loss is not None:
                structural_risk = abs(float(t.entry_price) - float(t.stop_loss))
            row = {
                "event_id": f"fill_open_{eid:04d}",
                "kind": "fill_open",
                "signal_id": f"{sym}|{t.entry_time}|{pat}",
                "pair": sym,
                "direction": _side(t.side),
                "signal_timestamp": master_ts,
                "entry_time": str(t.entry_time),
                "signal_price": float(t.entry_price),
                "ghost_entry": ghost_entry,
                "ghost_sl": ghost_sl,
                "ghost_tp": ghost_tp,
                "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
                "take_profit": float(t.take_profit) if t.take_profit is not None else None,
                "structural_risk": structural_risk,
                "intended_rr": float(t.score or 0),
                "margin": float(t.margin or 0),
                "leverage": LEVERAGE,
                "occupancy_before": occ_before,
                "pattern": pat,
                "entry_index": int(t.entry_index),
                "sl_armed_from_index": t.sl_armed_from_index,
                "state_hash_before": before_hash,
                "state_hash_after": after_hash,
                "master_index": mi,
            }
            events.append(row)
            fills.append(row)

        for t in shadow.portfolio.closed_trades[before_closed:]:
            eid += 1
            events.append(
                {
                    "event_id": f"fill_close_{eid:04d}",
                    "kind": "fill_close",
                    "signal_id": f"{t.symbol}|{t.entry_time}|{t.pattern}",
                    "pair": t.symbol,
                    "direction": _side(t.side),
                    "signal_timestamp": master_ts,
                    "entry_time": str(t.entry_time),
                    "exit_time": str(t.exit_time),
                    "exit_price": float(t.exit_price or 0),
                    "exit_reason": str(t.exit_reason or ""),
                    "gross_pnl_approx": float(t.pnl) + float(t.commission or 0),
                    "commission": float(t.commission or 0),
                    "net_pnl": float(t.pnl),
                    "pattern": str(t.pattern or ""),
                    "state_hash_before": before_hash,
                    "state_hash_after": after_hash,
                }
            )

        for sym in sorted(set(getattr(shadow.strategy, "_pending", {}) or {}) - before_pending):
            p = shadow.strategy._pending[sym]
            eid += 1
            events.append(
                {
                    "event_id": f"ghost_arm_{eid:04d}",
                    "kind": "ghost_arm",
                    "signal_id": f"ghost|{sym}|{master_ts}|{p.pattern}",
                    "pair": sym,
                    "direction": _side(p.action),
                    "signal_timestamp": master_ts,
                    "ghost_entry": float(p.planned_entry),
                    "ghost_sl": float(p.stop_loss),
                    "ghost_tp": float(p.take_profit),
                    "pattern": str(p.pattern),
                    "state_hash_before": before_hash,
                    "state_hash_after": after_hash,
                }
            )
        for sym in sorted(before_pending - set(getattr(shadow.strategy, "_pending", {}) or {})):
            eid += 1
            events.append(
                {
                    "event_id": f"ghost_clear_{eid:04d}",
                    "kind": "ghost_clear",
                    "signal_id": f"ghost_clear|{sym}|{master_ts}",
                    "pair": sym,
                    "signal_timestamp": master_ts,
                    "state_hash_before": before_hash,
                    "state_hash_after": after_hash,
                }
            )

    # Still-open at end of window
    open_end = []
    for sym, t in shadow.portfolio.open_trades.items():
        open_end.append(
            {
                "pair": sym,
                "direction": _side(t.side),
                "entry_time": str(t.entry_time),
                "entry_price": float(t.entry_price),
                "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
                "take_profit": float(t.take_profit) if t.take_profit is not None else None,
                "margin": float(t.margin or 0),
                "pattern": str(t.pattern or ""),
                "signal_id": f"{sym}|{t.entry_time}|{t.pattern}",
            }
        )

    tape = {
        "schema": "frozen_signal_tape.v1",
        "frozen": True,
        "source_window": "mirror_live_20260908_043045",
        "layer_a_validated": True,
        "do_not_regenerate_signals": True,
        "config_hash": shadow.config_hash,
        "n_events": len(events),
        "n_fills": len(fills),
        "events": events,
        "fills": fills,
        "open_at_window_end": open_end,
        "assumptions": {
            "signal_price": "Theory fill price at H1 close (fill:close)",
            "sizing": "Theory margin / leverage path at fill time",
            "occupancy": "Already resolved in Layer A; tape includes resulting fills only",
            "spread": "NONE (no Raw spreads) — matches Theory BacktestConfig",
        },
    }
    raw = json.dumps(tape, sort_keys=True, separators=(",", ":"), default=str).encode()
    tape["tape_sha256"] = hashlib.sha256(raw).hexdigest()
    return tape


def probe_broker(window_start: str, window_end: str) -> dict[str, Any]:
    """Probe MT5 deal history for magic 88002; load paper events."""
    paper = []
    p = FIX / "broker_events" / "stream.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                paper.append(json.loads(line))

    out: dict[str, Any] = {
        "reference_label": "THEORY_SHADOW_H1",
        "execute_mt5_during_capture": False,
        "paper_mirror_events": paper,
        "mt5_deals_magic_88002": [],
        "mt5_deals_magic_88001_count_only": 0,
        "mt5_ok": False,
        "note": "",
    }
    try:
        from borex_live.mt5.client import Mt5Client

        mt5 = Mt5Client(
            path=os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", ""),
            login=int(os.environ.get("MIRROR_MT5_LOGIN") or os.environ.get("MT5_LOGIN", "0") or 0),
            password=os.environ.get("MIRROR_MT5_PASSWORD") or os.environ.get("MT5_PASSWORD", ""),
            server=(
                os.environ.get("MIRROR_MT5_DEMO_SERVER")
                or os.environ.get("MT5_DEMO_SERVER")
                or os.environ.get("MT5_SERVER", "")
            ),
        )
        mt5.MAGIC = 88002
        mt5.connect()
        if not mt5.connected:
            out["note"] = "MT5 not connected"
            return out
        out["mt5_ok"] = True
        out["account_login"] = int(mt5._mt5.account_info().login)
        # history deals
        t0 = _ts(window_start).to_pydatetime() - timedelta(hours=2)
        t1 = _ts(window_end).to_pydatetime() + timedelta(hours=6)
        deals = mt5._mt5.history_deals_get(t0, t1) or []
        for d in deals:
            magic = int(getattr(d, "magic", 0) or 0)
            if magic == 88001:
                out["mt5_deals_magic_88001_count_only"] += 1
                continue
            if magic != 88002:
                continue
            out["mt5_deals_magic_88002"].append(
                {
                    "ticket": int(d.ticket),
                    "order": int(getattr(d, "order", 0) or 0),
                    "symbol": str(d.symbol),
                    "type": int(d.type),
                    "volume": float(d.volume),
                    "price": float(d.price),
                    "profit": float(d.profit),
                    "commission": float(getattr(d, "commission", 0) or 0),
                    "swap": float(getattr(d, "swap", 0) or 0),
                    "time": datetime.fromtimestamp(int(d.time), tz=timezone.utc).isoformat(),
                    "magic": magic,
                    "comment": str(getattr(d, "comment", "") or ""),
                }
            )
        if out["mt5_deals_magic_88002"]:
            out["reference_label"] = "BROKER_MT5_MAGIC_88002"
            out["note"] = "Real Mirror deals found in window"
        else:
            out["note"] = (
                "No MT5 deals with magic 88002 in window — capture used "
                "execute_mt5=False. Broker fidelity vs live fills cannot be "
                "measured; reference falls back to THEORY_SHADOW_H1 exits."
            )
    except Exception as exc:
        out["note"] = f"MT5 probe failed: {exc}"
    return out


def load_h1_from_fixture(fix: Path | None = None) -> dict[str, list]:
    import pickle

    root = Path(fix) if fix is not None else FIX
    series = pickle.loads((root / "h1_input" / "full_series" / "_series.pkl").read_bytes())
    return series


def load_m1_for_symbols(symbols: list[str], start: str, end: str) -> tuple[dict[str, list], dict]:
    """Fetch 1m OHLC for traded symbols. Label as 1M_OHLC_SURROGATE (not bid/ask ticks)."""
    from borex.viewerMT5.mt5_feed import _drop_forming_bar, fetch_mt5_candles
    from borex_live.mt5.client import Mt5Client

    meta = {
        "label": "1M_OHLC_SURROGATE",
        "not_broker_faithful_ticks": True,
        "spread_model": "NONE (no Raw)",
        "timezone": "UTC after server_offset",
        "source": "MT5 copy_rates via fetch_mt5_candles",
        "pairs_loaded": [],
        "pairs_missing": [],
    }
    client = Mt5Client(
        path=os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", ""),
    )
    client.connect()
    if not client.connected:
        meta["error"] = "MT5 not connected"
        return {}, meta

    t0 = _ts(start).to_pydatetime() - timedelta(hours=2)
    t1 = _ts(end).to_pydatetime() + timedelta(hours=2)
    out: dict[str, list] = {}
    for sym in symbols:
        try:
            bars = fetch_mt5_candles(
                sym, "1m", t0, t1, client=client, use_cache=True, write_cache=True
            )
            bars = _drop_forming_bar(list(bars), "1m")
            if bars:
                out[sym] = bars
                meta["pairs_loaded"].append(sym)
            else:
                meta["pairs_missing"].append(sym)
        except Exception as exc:
            meta["pairs_missing"].append(f"{sym}:{exc}")
    meta["server_offset_seconds"] = int(getattr(client, "server_offset_seconds", 0) or 0)
    return out, meta


def _build_engine(*, use_ltf: bool):
    from borex.alexg import AlexG8Strategy
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    # Dummy strategy — decisions replayed from tape; scan skipped
    strategy = AlexG8Strategy(min_rr=2.0, execution_interval="1h", ghost_sl_mult=0.6)
    cfg = BacktestConfig(
        initial_capital=1000.0,
        leverage=LEVERAGE,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=6.0,
        commission_per_lot=COMMISSION_PER_LOT,
        min_commission_per_side=0.04,
        lot_notional=LOT_NOTIONAL,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_daily=True,
        force_flat_utc_hour=19,
        force_flat_friday=False,
        spread_pips=0.0,  # explicit: no Raw
        slippage_pips=0.0,
        intra_hour_sl=False if use_ltf else True,  # H1 wick vs 1m walk
    )
    engine = MultiMarketEngine(strategy, cfg, max_positions=60)
    return engine, cfg


def decisions_from_fills(fills: list[dict], h1: dict[str, list]) -> list[dict]:
    """Convert frozen fill_open events into MultiMarketEngine decision records."""
    from borex.backtest.multi_market_engine import decision_from_signal
    from borex.models.candle import Signal, SignalAction

    out = []
    for f in fills:
        sym = f["pair"]
        series = h1.get(sym) or []
        entry_ts = _ts(f["entry_time"])
        idx = None
        for i, c in enumerate(series):
            if _ts(c.timestamp) == entry_ts:
                idx = i
                break
        if idx is None:
            # fallback to captured entry_index
            idx = int(f.get("entry_index") or 0)
        action = SignalAction.BUY if f["direction"] == "long" else SignalAction.SELL
        sig = Signal(
            action=action,
            index=idx,
            timestamp=entry_ts.to_pydatetime(),
            price=float(f["signal_price"]),
            stop_loss=f.get("stop_loss"),
            take_profit=f.get("take_profit"),
            score=float(f.get("intended_rr") or 2.0),
            pattern=str(f.get("pattern") or ""),
        )
        d = decision_from_signal(sym, sig)
        d["signal_id"] = f["signal_id"]
        d["event_id"] = f["event_id"]
        d["margin_hint"] = f.get("margin")
        out.append(d)
    return out


def run_execution(
    *,
    mode: str,
    h1: dict[str, list],
    m1: dict[str, list] | None,
    decisions: list[dict],
    master: str,
    start_ts: str,
    end_ts: str,
) -> dict[str, Any]:
    """Exec-only replay of frozen decisions (no strategy scan)."""
    use_ltf = mode == "h1_1m"
    engine, cfg = _build_engine(use_ltf=use_ltf)

    # Truncate to window end so we don't invent post-window bars;
    # still-open Theory positions remain open (no forced end_of_data beyond window).
    end = _ts(end_ts)
    h1_w = {
        sym: [c for c in bars if _ts(c.timestamp) <= end]
        for sym, bars in h1.items()
    }
    m1_w = None
    if m1:
        m1_w = {
            sym: [c for c in bars if _ts(c.timestamp) <= end + pd.Timedelta(hours=1)]
            for sym, bars in m1.items()
        }

    master_bars = h1_w[master]
    start_i = 0
    target = _ts(start_ts)
    for i, c in enumerate(master_bars):
        if _ts(c.timestamp) >= target:
            start_i = i
            break

    result = engine.run(
        h1_w,
        timeframe="1h+1m" if use_ltf else "1h",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=max(0, start_i),
        ltf_by_symbol=m1_w if use_ltf else None,
        decisions=list(decisions),
    )

    trades = []
    for t in result.trades:
        if _ts(t.entry_time) < target:
            continue
        reason = str(t.exit_reason or "")
        # Map engine's terminal flatten of window-end opens
        if reason == "end_of_data":
            reason = "open_eow"
        trades.append(
            {
                "symbol": t.symbol,
                "side": _side(t.side),
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time) if t.exit_time else None,
                "entry": float(t.entry_price),
                "exit": float(t.exit_price or 0) if reason != "open_eow" else None,
                "sl": float(t.stop_loss or 0),
                "tp": float(t.take_profit or 0),
                "pnl": float(t.pnl) if reason != "open_eow" else None,
                "commission": float(t.commission or 0),
                "gross_pnl": (
                    float(t.pnl) + float(t.commission or 0)
                    if reason != "open_eow"
                    else None
                ),
                "margin": float(t.margin or 0),
                "exit_reason": reason,
                "pattern": str(t.pattern or ""),
                "signal_id": f"{t.symbol}|{t.entry_time}|{t.pattern}",
            }
        )

    fingerprint = hashlib.sha256(
        json.dumps(trades, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "mode": mode,
        "assumptions": {
            "entry_fill_rule": (
                "MultiMarketEngine._signal_entry: late ghost fill:close → H1 close "
                "(same as ShadowEngine). No Raw spread."
            ),
            "spread": "0 (no Raw)",
            "slippage": "0",
            "sl_tp": "true_sl+margin → margin_stop_out_prices(entry, lev, rr)",
            "same_bar_exit": False,
            "exit_path": (
                "1m first-touch walk within H1 hour; SL-before-TP if both"
                if use_ltf
                else "H1 wick (_check_exit); SL-before-TP if both"
            ),
            "flatten": "force_flat_daily @ 19 UTC",
            "commission": f"${COMMISSION_PER_LOT}/lot via portfolio",
            "swap": "NOT MODELED",
            "1m_label": "1M_OHLC_SURROGATE" if use_ltf else "n/a",
            "window_truncation": str(end_ts),
        },
        "n_trades": len(trades),
        "trades": trades,
        "fingerprint": fingerprint,
        "final_equity": float(result.final_equity),
    }


def theory_reference_trades(tape: dict) -> list[dict]:
    """Build reference book from frozen tape fill_open + fill_close (+ open end)."""
    by_id: dict[str, dict] = {}
    for e in tape["events"]:
        if e["kind"] == "fill_open":
            by_id[e["signal_id"]] = {
                "signal_id": e["signal_id"],
                "symbol": e["pair"],
                "side": e["direction"],
                "entry_time": e["entry_time"],
                "entry": e["signal_price"],
                "sl": e.get("stop_loss"),
                "tp": e.get("take_profit"),
                "margin": e.get("margin"),
                "pattern": e.get("pattern"),
                "source": "THEORY_SHADOW_H1",
            }
        elif e["kind"] == "fill_close":
            row = by_id.get(e["signal_id"]) or {
                "signal_id": e["signal_id"],
                "symbol": e["pair"],
                "side": e["direction"],
                "entry_time": e["entry_time"],
                "source": "THEORY_SHADOW_H1",
            }
            row.update(
                {
                    "exit_time": e["exit_time"],
                    "exit": e["exit_price"],
                    "exit_reason": e["exit_reason"],
                    "pnl": e["net_pnl"],
                    "commission": e["commission"],
                    "gross_pnl": e["gross_pnl_approx"],
                }
            )
            by_id[e["signal_id"]] = row
    for o in tape.get("open_at_window_end") or []:
        sid = o["signal_id"]
        row = by_id.get(sid) or {}
        row.update(
            {
                **o,
                "symbol": o["pair"],
                "side": o["direction"],
                "entry": o["entry_price"],
                "exit_time": None,
                "exit": None,
                "exit_reason": "open_eow",
                "pnl": None,
                "commission": None,
                "gross_pnl": None,
                "source": "THEORY_SHADOW_H1",
                "signal_id": sid,
            }
        )
        by_id[sid] = row
    return list(by_id.values())


def match_books(ref: list[dict], sim: list[dict]) -> dict[str, Any]:
    ref_by = {r["signal_id"]: r for r in ref if r.get("signal_id")}
    sim_by = {s["signal_id"]: s for s in sim if s.get("signal_id")}
    # also try symbol|entry_time key
    def alt_key(r):
        return f"{r.get('symbol')}|{r.get('entry_time')}"

    ref_alt = {alt_key(r): r for r in ref}
    sim_alt = {alt_key(s): s for s in sim}

    matched = []
    used_sim = set()
    for sid, r in ref_by.items():
        s = sim_by.get(sid)
        if s is None:
            s = sim_alt.get(alt_key(r))
        if s is None:
            continue
        used_sim.add(s.get("signal_id") or alt_key(s))
        entry_err = (
            float(s["entry"]) - float(r["entry"])
            if r.get("entry") is not None and s.get("entry") is not None
            else None
        )
        exit_err = None
        if r.get("exit") is not None and s.get("exit") is not None:
            exit_err = float(s["exit"]) - float(r["exit"])
        pnl_err = None
        if r.get("pnl") is not None and s.get("pnl") is not None:
            pnl_err = float(s["pnl"]) - float(r["pnl"])
        gross_err = None
        if r.get("gross_pnl") is not None and s.get("gross_pnl") is not None:
            gross_err = float(s["gross_pnl"]) - float(r["gross_pnl"])
        comm_err = None
        if r.get("commission") is not None and s.get("commission") is not None:
            comm_err = float(s["commission"]) - float(r["commission"])
        outcome_agree = str(r.get("exit_reason") or "") == str(s.get("exit_reason") or "")
        # Layer split: price path vs accounting
        layer_b = {
            "entry_error": entry_err,
            "exit_error": exit_err,
            "outcome_agree": outcome_agree,
            "ref_reason": r.get("exit_reason"),
            "sim_reason": s.get("exit_reason"),
        }
        layer_c = {
            "commission_error": comm_err,
            "net_pnl_error": pnl_err,
            "gross_pnl_error": gross_err,
        }
        matched.append(
            {
                "signal_id": sid,
                "symbol": r.get("symbol"),
                "entry_time": r.get("entry_time"),
                "layer_b": layer_b,
                "layer_c": layer_c,
                "ref": r,
                "sim": s,
            }
        )

    only_ref = [r for sid, r in ref_by.items() if sid not in {m["signal_id"] for m in matched}]
    only_sim = [
        s
        for sid, s in sim_by.items()
        if sid not in used_sim and alt_key(s) not in used_sim
    ]

    entry_errs = [m["layer_b"]["entry_error"] for m in matched if m["layer_b"]["entry_error"] is not None]
    exit_errs = [m["layer_b"]["exit_error"] for m in matched if m["layer_b"]["exit_error"] is not None]
    pnl_errs = [m["layer_c"]["net_pnl_error"] for m in matched if m["layer_c"]["net_pnl_error"] is not None]
    gross_errs = [
        m["layer_c"]["gross_pnl_error"] for m in matched if m["layer_c"]["gross_pnl_error"] is not None
    ]
    outcome_n = sum(1 for m in matched if m["layer_b"]["outcome_agree"])

    return {
        "n_ref": len(ref),
        "n_sim": len(sim),
        "n_matched": len(matched),
        "only_ref": only_ref,
        "only_sim": only_sim,
        "outcome_agreement_pct": (outcome_n / len(matched) * 100.0) if matched else None,
        "entry_error": _err_stats(entry_errs),
        "exit_error": _err_stats(exit_errs),
        "net_pnl_error": _err_stats(pnl_errs),
        "gross_pnl_error": _err_stats(gross_errs),
        "matches": matched,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("Freezing signal tape…", flush=True)
    tape = freeze_signal_tape()
    (OUT / "frozen_signal_tape.json").write_text(
        json.dumps(tape, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "FROZEN").write_text(
        f"frozen=true\ntape_sha256={tape['tape_sha256']}\ndo_not_modify\n",
        encoding="utf-8",
    )

    man = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
    w0 = man["extra"]["first_test_ts"]
    w1 = man["extra"]["last_test_ts"]
    master = man["extra"]["master_symbol"]

    print("Probing broker…", flush=True)
    broker = probe_broker(w0, w1)
    (OUT / "broker_trade_dump.json").write_text(
        json.dumps(broker, indent=2, default=str), encoding="utf-8"
    )

    h1 = load_h1_from_fixture()
    fill_syms = sorted({f["pair"] for f in tape["fills"]})
    print(f"Loading 1m for {fill_syms}…", flush=True)
    m1, m1_meta = load_m1_for_symbols(fill_syms, w0, w1)
    (OUT / "m1_source_meta.json").write_text(json.dumps(m1_meta, indent=2), encoding="utf-8")

    decisions = decisions_from_fills(tape["fills"], h1)
    (OUT / "frozen_decisions.json").write_text(
        json.dumps(decisions, indent=2, default=str), encoding="utf-8"
    )

    ref = theory_reference_trades(tape)
    (OUT / "reference_theory_trades.json").write_text(
        json.dumps(ref, indent=2, default=str), encoding="utf-8"
    )

    print("H1-only execution A/B…", flush=True)
    h1_a = run_execution(
        mode="h1_only", h1=h1, m1=None, decisions=decisions, master=master, start_ts=w0, end_ts=w1
    )
    h1_b = run_execution(
        mode="h1_only", h1=h1, m1=None, decisions=decisions, master=master, start_ts=w0, end_ts=w1
    )
    print("H1+1m execution A/B…", flush=True)
    m1_a = run_execution(
        mode="h1_1m", h1=h1, m1=m1, decisions=decisions, master=master, start_ts=w0, end_ts=w1
    )
    m1_b = run_execution(
        mode="h1_1m", h1=h1, m1=m1, decisions=decisions, master=master, start_ts=w0, end_ts=w1
    )

    (OUT / "h1_only_replay_a.json").write_text(json.dumps(h1_a, indent=2), encoding="utf-8")
    (OUT / "h1_only_replay_b.json").write_text(json.dumps(h1_b, indent=2), encoding="utf-8")
    (OUT / "h1_1m_replay_a.json").write_text(json.dumps(m1_a, indent=2), encoding="utf-8")
    (OUT / "h1_1m_replay_b.json").write_text(json.dumps(m1_b, indent=2), encoding="utf-8")

    det = {
        "h1_only_deterministic": h1_a["fingerprint"] == h1_b["fingerprint"],
        "h1_1m_deterministic": m1_a["fingerprint"] == m1_b["fingerprint"],
        "h1_only_fp": h1_a["fingerprint"],
        "h1_1m_fp": m1_a["fingerprint"],
    }
    (OUT / "determinism_report.json").write_text(json.dumps(det, indent=2), encoding="utf-8")
    if not det["h1_only_deterministic"] or not det["h1_1m_deterministic"]:
        print("STOP: execution nondeterministic", det)
        return 2

    match_h1 = match_books(ref, h1_a["trades"])
    match_m1 = match_books(ref, m1_a["trades"])
    (OUT / "trade_matching_h1_only.json").write_text(
        json.dumps(match_h1, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "trade_matching_h1_1m.json").write_text(
        json.dumps(match_m1, indent=2, default=str), encoding="utf-8"
    )

    # Cross: H1 vs H1+1m
    cross = match_books(h1_a["trades"], m1_a["trades"])
    (OUT / "trade_matching_h1_vs_h1_1m.json").write_text(
        json.dumps(cross, indent=2, default=str), encoding="utf-8"
    )

    broker_available = broker["reference_label"] == "BROKER_MT5_MAGIC_88002"
    layer_b = {
        "title": "Layer B — execution",
        "broker_available": broker_available,
        "broker_label": broker["reference_label"],
        "h1_only_vs_reference": {
            "entry_error": match_h1["entry_error"],
            "exit_error": match_h1["exit_error"],
            "outcome_agreement_pct": match_h1["outcome_agreement_pct"],
        },
        "h1_1m_vs_reference": {
            "entry_error": match_m1["entry_error"],
            "exit_error": match_m1["exit_error"],
            "outcome_agreement_pct": match_m1["outcome_agreement_pct"],
        },
        "h1_vs_h1_1m": {
            "exit_error": cross["exit_error"],
            "outcome_agreement_pct": cross["outcome_agreement_pct"],
            "note": "Direct comparison of execution models on same frozen tape",
        },
        "m1_data": m1_meta,
        "spread": "NONE (explicit; Theory had no Raw spreads)",
    }
    layer_c = {
        "title": "Layer C — accounting",
        "h1_only_vs_reference": {
            "net_pnl_error": match_h1["net_pnl_error"],
            "gross_pnl_error": match_h1["gross_pnl_error"],
        },
        "h1_1m_vs_reference": {
            "net_pnl_error": match_m1["net_pnl_error"],
            "gross_pnl_error": match_m1["gross_pnl_error"],
        },
        "swap_modeled": False,
        "commission_model": f"{COMMISSION_PER_LOT}/lot",
    }
    (OUT / "layer_b_report.json").write_text(json.dumps(layer_b, indent=2), encoding="utf-8")
    (OUT / "layer_c_report.json").write_text(json.dumps(layer_c, indent=2), encoding="utf-8")

    # Architecture scores (qualitative + this experiment evidence)
    h1_exit_mae = (match_h1["exit_error"] or {}).get("mae")
    m1_exit_mae = (match_m1["exit_error"] or {}).get("mae")
    cross_exit_mae = (cross["exit_error"] or {}).get("mae")
    m1_changes = bool(cross_exit_mae and cross_exit_mae > 1e-12) or (
        (cross.get("outcome_agreement_pct") or 100) < 100
    )

    if not broker_available:
        winner = "INCONCLUSIVE_VS_BROKER"
        rec = (
            "Cannot crown H1-only vs H1+1m for broker fidelity: no real MT5 fills "
            "in this window (execute_mt5=False). Next: Mirror golden with "
            "execute_mt5=True (magic 88002 only) OR import broker dump for same tape."
        )
        gate2 = False
        blocker = (
            "Missing real broker execution observation for the frozen tape window"
        )
    else:
        # would compare to broker — not reached in this run
        winner = "TBD"
        rec = ""
        gate2 = False
        blocker = "unreachable"

    # Prefer simpler if models identical vs theory
    if broker_available is False and not m1_changes and (h1_exit_mae or 0) == 0:
        arch_pref = "A_shadow_plus_h1_only"
        arch_note = (
            "On this window H1+1m did not change outcomes vs H1-only relative to "
            "Theory H1 reference; prefer simpler H1-only until broker evidence exists."
        )
    elif m1_changes and not broker_available:
        arch_pref = "UNDECIDED_NEED_BROKER"
        arch_note = (
            "1m changes exits vs H1; without broker fills we cannot know which is closer."
        )
    else:
        arch_pref = "UNDECIDED"
        arch_note = ""

    architecture = {
        "options": [
            {
                "id": "A",
                "name": "ShadowEngine + H1-only exec",
                "broker_fidelity": "unknown_here" if not broker_available else "measured",
                "determinism": "pass",
                "compute_cost": "low",
                "scalability_60": "high",
                "sweep_speed": "high",
                "complexity": "low",
                "maintainability": "high",
                "data_requirements": "H1 only",
            },
            {
                "id": "B",
                "name": "ShadowEngine + H1+1m exec",
                "broker_fidelity": "unknown_here" if not broker_available else "measured",
                "determinism": "pass",
                "compute_cost": "medium",
                "scalability_60": "medium",
                "sweep_speed": "medium",
                "complexity": "medium",
                "maintainability": "medium",
                "data_requirements": "H1 + 1M_OHLC_SURROGATE",
            },
            {
                "id": "C",
                "name": "ShadowEngine + tick/event broker emulator",
                "broker_fidelity": "potential_highest",
                "determinism": "unknown",
                "compute_cost": "high",
                "scalability_60": "low",
                "sweep_speed": "low",
                "complexity": "high",
                "maintainability": "low",
                "data_requirements": "ticks/bid-ask",
                "note": "Do NOT auto-choose without broker evidence that A/B fail",
            },
        ],
        "recommendation": arch_pref,
        "recommendation_note": arch_note or rec,
        "m1_materially_changed_exits_vs_h1": m1_changes,
        "gate2_execution_fidelity_passed": gate2,
        "blocker": blocker,
    }
    (OUT / "architecture_comparison.json").write_text(
        json.dumps(architecture, indent=2), encoding="utf-8"
    )

    answers = {
        "1_h1_only_reproduces_broker": (
            "CANNOT_EVALUATE_NO_BROKER_FILLS"
            if not broker_available
            else "see matching"
        ),
        "2_h1_1m_reproduces_broker": (
            "CANNOT_EVALUATE_NO_BROKER_FILLS"
            if not broker_available
            else "see matching"
        ),
        "3_lower_entry_error": "tie_or_n/a — both use frozen Theory entry",
        "4_lower_exit_error_vs_theory_ref": (
            "h1_only"
            if (h1_exit_mae or 0) <= (m1_exit_mae or 0)
            else "h1_1m"
        ),
        "5_better_outcome_agreement_vs_theory": (
            "h1_only"
            if (match_h1.get("outcome_agreement_pct") or 0)
            >= (match_m1.get("outcome_agreement_pct") or 0)
            else "h1_1m"
        ),
        "6_lower_gross_pnl_error_vs_theory": (
            "h1_only"
            if ((match_h1["gross_pnl_error"] or {}).get("mae") or 0)
            <= ((match_m1["gross_pnl_error"] or {}).get("mae") or 0)
            else "h1_1m"
        ),
        "7_lower_net_pnl_error_vs_theory": (
            "h1_only"
            if ((match_h1["net_pnl_error"] or {}).get("mae") or 0)
            <= ((match_m1["net_pnl_error"] or {}).get("mae") or 0)
            else "h1_1m"
        ),
        "8_remaining_error_layer_b": (
            "N/A vs broker; vs Theory H1, residual is exit-path (B) if H1+1m differs"
        ),
        "9_remaining_error_layer_c": "Swap not modeled; commission model shared",
        "10_m1_materially_improves_fidelity": (
            "UNKNOWN_WITHOUT_BROKER"
            if not broker_available
            else ("yes" if m1_changes else "no")
        ),
        "11_canonical_execution_architecture": architecture["recommendation"],
        "12_validated_enough_for_system_a_research": False,
        "13_exact_remaining_blocker": blocker,
        "metrics_h1_vs_theory": match_h1,
        "metrics_m1_vs_theory": match_m1,
        "metrics_h1_vs_m1": {
            "outcome_agreement_pct": cross.get("outcome_agreement_pct"),
            "exit_error": cross.get("exit_error"),
        },
        "determinism": det,
        "m1_label": "1M_OHLC_SURROGATE",
        "strategy_optimization": "NO-GO",
    }
    (OUT / "final_answers.json").write_text(
        json.dumps(answers, indent=2, default=str), encoding="utf-8"
    )

    readme = OUT / "README.md"
    readme.write_text(
        "\n".join(
            [
                "# Execution fidelity — Gate 2",
                "",
                f"Window: `mirror_live_20260908_043045`",
                f"Frozen tape sha256: `{tape['tape_sha256']}`",
                "",
                "## Critical constraint",
                "",
                "Layer A capture used `execute_mt5=False`. There are **no real broker fills**",
                "for magic 88002 in this window. Reference book = `THEORY_SHADOW_H1`.",
                "",
                "Therefore Gate 2 **cannot pass** broker fidelity yet.",
                "",
                "## Determinism",
                "",
                f"- H1-only A==B: **{det['h1_only_deterministic']}**",
                f"- H1+1m A==B: **{det['h1_1m_deterministic']}**",
                "",
                "## Answers",
                "",
                f"1. H1-only vs broker? **{answers['1_h1_only_reproduces_broker']}**",
                f"2. H1+1m vs broker? **{answers['2_h1_1m_reproduces_broker']}**",
                f"3. Lower entry error? **{answers['3_lower_entry_error']}**",
                f"4. Lower exit error (vs Theory)? **{answers['4_lower_exit_error_vs_theory_ref']}**",
                f"5. Better outcome agreement (vs Theory)? **{answers['5_better_outcome_agreement_vs_theory']}**",
                f"6. Lower gross PnL error? **{answers['6_lower_gross_pnl_error_vs_theory']}**",
                f"7. Lower net PnL error? **{answers['7_lower_net_pnl_error_vs_theory']}**",
                f"8. Remaining Layer B? **{answers['8_remaining_error_layer_b']}**",
                f"9. Remaining Layer C? **{answers['9_remaining_error_layer_c']}**",
                f"10. 1m materially improves fidelity? **{answers['10_m1_materially_improves_fidelity']}**",
                f"11. Canonical architecture? **{answers['11_canonical_execution_architecture']}**",
                f"12. Ready for System-A research? **NO**",
                f"13. Blocker? **{answers['13_exact_remaining_blocker']}**",
                "",
                "## Next required experiment",
                "",
                "Re-capture a short Mirror golden window with `execute_mt5=True`, magic **88002**,",
                "port **8792**, still never touching 8791 — freeze that broker deal tape,",
                "then re-run this exact comparison.",
                "",
                "**No strategy optimization.**",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(json.dumps({
        "out": str(OUT),
        "tape_sha256": tape["tape_sha256"],
        "determinism": det,
        "broker_label": broker["reference_label"],
        "gate2": gate2,
        "blocker": blocker,
        "h1_vs_theory_outcome": match_h1.get("outcome_agreement_pct"),
        "m1_vs_theory_outcome": match_m1.get("outcome_agreement_pct"),
        "h1_vs_m1_outcome": cross.get("outcome_agreement_pct"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
