"""Gate 2 broker fidelity: frozen tape + REAL magic=88002 deals vs H1 / H1+1m.

Hard rules:
  - Refuse if zero magic=88002 deals in window
  - Never include magic=88001
  - Do not regenerate signals
  - Do not introduce Raw spreads
  - Strategy optimization remains NO-GO

Usage:
  python scripts/run_gate2_broker_fidelity.py --window data/runs/golden_windows/mirror_live_exec_<stamp>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path[:0] = [str(ROOT), str(LIVE), str(ROOT / "scripts")]
load_dotenv(LIVE / ".env")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _side(x) -> str:
    s = str(x or "").lower()
    if s in ("buy", "long", "0"):
        return "long"
    if s in ("sell", "short", "1"):
        return "short"
    return s


def _err_stats(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0, "mean_signed": None, "mae": None, "median": None, "p95": None, "max_abs": None}
    a = sorted(abs(v) for v in vals)
    p95 = a[min(len(a) - 1, max(0, int(math.ceil(0.95 * len(a)) - 1)))]
    return {
        "n": len(vals),
        "mean_signed": float(sum(vals) / len(vals)),
        "mae": float(sum(a) / len(a)),
        "median": float(statistics.median(vals)),
        "p95": float(p95),
        "max_abs": float(max(a)),
    }


def yahoo_from_mt5(sym: str) -> str:
    s = str(sym).upper().replace(".a", "").replace(".", "")
    if s.endswith("=X"):
        return s
    # strip common suffixes
    for suf in ("M", "mini"):
        pass
    if len(s) >= 6:
        return f"{s[:6]}=X"
    return f"{s}=X"


def load_broker_deals(window: Path) -> dict[str, Any]:
    p = window / "broker_deals_magic_88002.json"
    if not p.exists():
        raise SystemExit(f"Missing {p} — live capture did not write broker deals")
    data = json.loads(p.read_text(encoding="utf-8"))
    deals = [d for d in data.get("deals") or [] if int(d.get("magic") or 0) == 88002]
    bad = [d for d in data.get("deals") or [] if int(d.get("magic") or 0) == 88001]
    if bad:
        raise SystemExit("HARD FAIL: magic 88001 deals present in dump")
    if not deals:
        raise SystemExit("HARD FAIL: zero magic=88002 deals — Gate 2 incomplete")
    data["deals"] = deals
    data["n"] = len(deals)
    return data


def refresh_broker_deals_utc(window: Path) -> dict[str, Any]:
    """Re-dump magic=88002 deals with server→UTC conversion into the window file.

    Prefer live MT5 via Mt5Client.server_offset_seconds. If IPC fails but the
    existing dump already has time_unix_raw + server_offset_seconds, re-normalize
    offline. Never invent deals.
    """
    from borex.data.mt5_time import mt5_unix_to_utc

    p = window / "broker_deals_magic_88002.json"
    if not p.exists():
        raise SystemExit(f"Missing {p}")
    prev = json.loads(p.read_text(encoding="utf-8"))
    since = _ts(prev.get("since") or "1970-01-01T00:00:00+00:00").to_pydatetime()
    until = _ts(prev.get("until") or datetime.now(timezone.utc).isoformat()).to_pydatetime()

    # Always keep a copy of the pre-fix dump once.
    bak = window / "broker_deals_magic_88002.pre_utc_fix.json"
    if not bak.exists():
        bak.write_text(json.dumps(prev, indent=2), encoding="utf-8")

    refreshed: dict[str, Any] | None = None
    err: str | None = None
    try:
        from borex_live.mt5.client import Mt5Client
        from run_mirror_golden_live_execute import count_magic_deals

        client = Mt5Client(
            path=os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", ""),
        )
        client.connect()
        if not client.connected:
            raise RuntimeError("MT5 not connected")
        # Widen slightly so server-clock query edges don't drop the round-trip
        n, rows = count_magic_deals(
            client,
            88002,
            since - timedelta(hours=6),
            until_utc=until + timedelta(hours=6),
        )
        # Keep only deals whose UTC time falls in the original capture window
        # (with 1h pad for close latency).
        lo = since - timedelta(hours=1)
        hi = until + timedelta(hours=1)
        kept = []
        for r in rows:
            t = _ts(r["time"]).to_pydatetime()
            if lo <= t <= hi:
                kept.append(r)
        # If pad filter emptied a non-empty refresh, keep all refreshed rows
        if not kept and rows:
            kept = rows
        refreshed = {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "n": len(kept),
            "deals": kept,
            "account_login": prev.get("account_login"),
            "magic": 88002,
            "port": 8792,
            "timestamp_normalized": True,
            "server_offset_seconds": int(getattr(client, "server_offset_seconds", 0) or 0),
            "normalization": "mt5_unix_to_utc via Mt5Client.server_offset_seconds",
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }
        # intentionally no mt5.shutdown on shared terminal
    except Exception as exc:
        err = str(exc)
        # Offline fallback: re-label existing raw unix if present
        deals_in = list(prev.get("deals") or [])
        if deals_in and all("time_unix_raw" in d for d in deals_in):
            offset = int(
                prev.get("server_offset_seconds")
                or deals_in[0].get("server_offset_seconds")
                or 0
            )
            out_deals = []
            for d in deals_in:
                raw = int(d["time_unix_raw"])
                nd = dict(d)
                nd["time"] = mt5_unix_to_utc(raw, offset).isoformat()
                nd["time_utc_source"] = "mt5_unix_to_utc_offline"
                nd["server_offset_seconds"] = offset
                out_deals.append(nd)
            refreshed = {
                **prev,
                "n": len(out_deals),
                "deals": out_deals,
                "timestamp_normalized": True,
                "server_offset_seconds": offset,
                "normalization": "offline mt5_unix_to_utc from time_unix_raw",
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "mt5_refresh_error": err,
            }
        else:
            # Last-resort: forensic-known ICMarkets +3h on ISO labels that lack raw unix
            # Only when prior dump is the known broken fromtimestamp(UTC) path.
            offset = 3 * 3600
            out_deals = []
            for d in deals_in:
                nd = dict(d)
                t = _ts(d["time"]).to_pydatetime()
                # Invert broken conversion: labeled_as_utc was actually server clock
                # true_utc = server_unix - offset; broken used fromtimestamp(server) as UTC
                # so true_utc = labeled - offset
                true_utc = t - timedelta(seconds=offset)
                nd["time_before_fix"] = nd["time"]
                nd["time"] = true_utc.isoformat()
                nd["time_utc_source"] = "subtract_assumed_gmt3_from_mislabeled_utc"
                nd["server_offset_seconds"] = offset
                out_deals.append(nd)
            refreshed = {
                **prev,
                "n": len(out_deals),
                "deals": out_deals,
                "timestamp_normalized": True,
                "server_offset_seconds": offset,
                "normalization": (
                    "fallback subtract GMT+3 from mislabeled UTC ISO "
                    "(no MT5 / no time_unix_raw)"
                ),
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "mt5_refresh_error": err,
                "warning": (
                    "Used assumed +3h offset because MT5 refresh failed and dump "
                    "lacks time_unix_raw. Prefer live refresh when IPC is healthy."
                ),
            }

    assert refreshed is not None
    p.write_text(json.dumps(refreshed, indent=2), encoding="utf-8")
    (window / "broker_deals_utc_refresh_meta.json").write_text(
        json.dumps(
            {
                "ok": True,
                "path": str(p),
                "n": refreshed.get("n"),
                "server_offset_seconds": refreshed.get("server_offset_seconds"),
                "normalization": refreshed.get("normalization"),
                "mt5_refresh_error": refreshed.get("mt5_refresh_error") or err,
                "warning": refreshed.get("warning"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return refreshed


def pair_broker_rounds(deals: list[dict]) -> list[dict]:
    """Pair entry/exit deals by position_id/order/comment when possible."""
    # MT5 deal.entry: 0=IN, 1=OUT, ...
    opens = [d for d in deals if int(d.get("entry") or 0) == 0]
    closes = [d for d in deals if int(d.get("entry") or 0) != 0]
    # Fallback: alternate by symbol chronologically
    by_sym: dict[str, list] = {}
    for d in sorted(deals, key=lambda x: x["time"]):
        by_sym.setdefault(d["symbol"], []).append(d)

    trades = []
    for sym, rows in by_sym.items():
        stack = []
        for d in rows:
            entry_flag = int(d.get("entry") or 0)
            if entry_flag == 0:
                stack.append(d)
            else:
                op = stack.pop(0) if stack else None
                trades.append(
                    {
                        "symbol_mt5": sym,
                        "symbol": yahoo_from_mt5(sym),
                        "side": "long" if int(op["type"]) == 0 else "short" if op else "?",
                        "broker_entry_time": op["time"] if op else None,
                        "broker_entry_price": float(op["price"]) if op else None,
                        "broker_exit_time": d["time"],
                        "broker_exit_price": float(d["price"]),
                        "volume": float(d.get("volume") or (op or {}).get("volume") or 0),
                        "profit": float(d.get("profit") or 0),
                        "commission": float(d.get("commission") or 0)
                        + float((op or {}).get("commission") or 0),
                        "swap": float(d.get("swap") or 0) + float((op or {}).get("swap") or 0),
                        "entry_ticket": int((op or {}).get("ticket") or 0),
                        "exit_ticket": int(d["ticket"]),
                        "comment": d.get("comment") or (op or {}).get("comment"),
                        "magic": 88002,
                        "open_unmatched": op is None,
                    }
                )
        for op in stack:
            trades.append(
                {
                    "symbol_mt5": sym,
                    "symbol": yahoo_from_mt5(sym),
                    "side": "long" if int(op["type"]) == 0 else "short",
                    "broker_entry_time": op["time"],
                    "broker_entry_price": float(op["price"]),
                    "broker_exit_time": None,
                    "broker_exit_price": None,
                    "volume": float(op.get("volume") or 0),
                    "profit": None,
                    "commission": float(op.get("commission") or 0),
                    "swap": float(op.get("swap") or 0),
                    "entry_ticket": int(op["ticket"]),
                    "exit_ticket": None,
                    "comment": op.get("comment"),
                    "magic": 88002,
                    "still_open": True,
                }
            )
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", required=True)
    args = ap.parse_args()
    window = Path(args.window)
    fix = window / "golden_fixture"
    out = window / "execution_fidelity"
    out.mkdir(parents=True, exist_ok=True)

    broker_raw = load_broker_deals(window)
    if not broker_raw.get("timestamp_normalized"):
        print("Refreshing broker deal timestamps → UTC…", flush=True)
        broker_raw = refresh_broker_deals_utc(window)
        broker_raw = load_broker_deals(window)
    else:
        print(
            f"Broker deals already UTC-normalized "
            f"(offset={broker_raw.get('server_offset_seconds')})",
            flush=True,
        )
    broker_trades = pair_broker_rounds(broker_raw["deals"])
    (out / "broker_trade_dump.json").write_text(
        json.dumps(
            {
                "raw": broker_raw,
                "paired_trades": broker_trades,
                "timestamp_normalized": bool(broker_raw.get("timestamp_normalized")),
                "server_offset_seconds": broker_raw.get("server_offset_seconds"),
                "normalization": broker_raw.get("normalization"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Quality gates
    quality = {
        "mt5_deals_exist": True,
        "all_magic_88002": all(int(d.get("magic") or 0) == 88002 for d in broker_raw["deals"]),
        "no_magic_88001": True,
        "port": 8792,
        "n_deals": broker_raw["n"],
        "n_paired_trades": len(broker_trades),
        "mapping_notes": [],
    }
    if not quality["all_magic_88002"]:
        raise SystemExit("HARD FAIL: non-88002 deals in dump")

    # Freeze tape from Layer-A fixture if present
    if not (fix / "initial_state.pkl").exists():
        verdict = {
            "gate2": "NO-GO",
            "blocker": "Broker deals exist but Layer-A golden_fixture incomplete — cannot freeze validated tape",
            "quality": quality,
        }
        (out / "GATE2_VERDICT.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        print(json.dumps(verdict, indent=2))
        return 3

    # Reuse execution fidelity machinery against broker book
    import run_execution_fidelity as ef

    ef.FIX = fix
    ef.OUT = out
    ef.WINDOW = window

    print("Freezing signal tape from live fixture…", flush=True)
    tape = ef.freeze_signal_tape(fix)
    (out / "frozen_signal_tape.json").write_text(
        json.dumps(tape, indent=2, default=str), encoding="utf-8"
    )

    h1 = ef.load_h1_from_fixture(fix)
    man = json.loads((fix / "manifest.json").read_text(encoding="utf-8")) if (fix / "manifest.json").exists() else {}
    extra = man.get("extra") or {}
    w0 = extra.get("first_test_ts") or (tape["fills"][0]["entry_time"] if tape["fills"] else None)
    w1 = extra.get("last_test_ts") or (tape["fills"][-1]["entry_time"] if tape["fills"] else None)
    master = extra.get("master_symbol") or "EURUSD=X"
    if not w0 or not tape["fills"]:
        verdict = {
            "gate2": "NO-GO",
            "blocker": "No Theory fills in frozen tape to map against broker",
            "quality": quality,
        }
        (out / "GATE2_VERDICT.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        print(json.dumps(verdict, indent=2))
        return 3

    fill_syms = sorted({f["pair"] for f in tape["fills"]})
    m1_available = True
    try:
        m1, m1_meta = ef.load_m1_for_symbols(fill_syms, str(w0), str(w1))
        if not m1:
            m1_available = False
            m1_meta = dict(m1_meta or {})
            m1_meta["available"] = False
            m1_meta.setdefault(
                "error",
                "MT5 connected but returned no 1m bars for fill symbols",
            )
    except Exception as exc:
        m1_available = False
        m1 = {}
        m1_meta = {
            "label": "1M_UNAVAILABLE_MT5_IPC",
            "available": False,
            "not_broker_faithful_ticks": True,
            "error": str(exc),
            "pairs_loaded": [],
            "pairs_missing": fill_syms,
        }
    (out / "m1_source_meta.json").write_text(json.dumps(m1_meta, indent=2), encoding="utf-8")
    decisions = ef.decisions_from_fills(tape["fills"], h1)

    h1_a = ef.run_execution(
        mode="h1_only", h1=h1, m1=None, decisions=decisions, master=master, start_ts=str(w0), end_ts=str(w1)
    )
    h1_b = ef.run_execution(
        mode="h1_only", h1=h1, m1=None, decisions=decisions, master=master, start_ts=str(w0), end_ts=str(w1)
    )
    if m1_available:
        m1_a = ef.run_execution(
            mode="h1_1m", h1=h1, m1=m1, decisions=decisions, master=master, start_ts=str(w0), end_ts=str(w1)
        )
        m1_b = ef.run_execution(
            mode="h1_1m", h1=h1, m1=m1, decisions=decisions, master=master, start_ts=str(w0), end_ts=str(w1)
        )
    else:
        m1_a = {
            "mode": "h1_1m",
            "fingerprint": None,
            "trades": [],
            "skipped": True,
            "reason": m1_meta.get("error") or "1m unavailable",
        }
        m1_b = dict(m1_a)
    det = {
        "h1_only_deterministic": h1_a["fingerprint"] == h1_b["fingerprint"],
        "h1_1m_deterministic": (
            None
            if not m1_available
            else m1_a["fingerprint"] == m1_b["fingerprint"]
        ),
        "h1_1m_available": m1_available,
    }
    if not det["h1_only_deterministic"]:
        print("STOP nondeterministic", det)
        return 2
    if m1_available and not det["h1_1m_deterministic"]:
        print("STOP nondeterministic", det)
        return 2

    # Map Theory/sim fills to broker by symbol + nearest entry time
    def map_to_broker(sim_trades: list[dict]) -> dict[str, Any]:
        unused = list(broker_trades)
        matches = []
        unmapped_sim = []
        for s in sim_trades:
            best = None
            best_dt = None
            for b in unused:
                if yahoo_from_mt5(b["symbol"]) != s["symbol"] and b["symbol"] != s["symbol"]:
                    # try normalized
                    if b.get("symbol") != s["symbol"]:
                        continue
                if b.get("broker_entry_time") is None:
                    continue
                dt = abs((_ts(b["broker_entry_time"]) - _ts(s["entry_time"])).total_seconds())
                # allow up to 2h skew for mirror-at-close latency
                if dt <= 7200 and (best_dt is None or dt < best_dt):
                    best, best_dt = b, dt
            if best is None:
                unmapped_sim.append(s)
                quality["mapping_notes"].append(
                    f"unmapped_sim {s['symbol']}@{s['entry_time']}"
                )
                continue
            unused.remove(best)
            entry_err = float(s["entry"]) - float(best["broker_entry_price"])
            exit_err = None
            if s.get("exit") is not None and best.get("broker_exit_price") is not None:
                exit_err = float(s["exit"]) - float(best["broker_exit_price"])
            pnl_err = None
            if s.get("pnl") is not None and best.get("profit") is not None:
                # broker profit is typically net of commission/swap already depending on deal fields
                broker_net = float(best["profit"]) + float(best.get("commission") or 0) + float(best.get("swap") or 0)
                # Actually MT5 deal.profit is usually without commission; commission separate
                broker_net = float(best["profit"]) + float(best.get("commission") or 0) + float(best.get("swap") or 0)
                pnl_err = float(s["pnl"]) - broker_net
            matches.append(
                {
                    "symbol": s["symbol"],
                    "theory_entry_time": s["entry_time"],
                    "broker_entry_time": best["broker_entry_time"],
                    "entry_timing_sec": best_dt,
                    "theory_entry": s["entry"],
                    "broker_entry": best["broker_entry_price"],
                    "entry_error": entry_err,
                    "theory_exit": s.get("exit"),
                    "broker_exit": best.get("broker_exit_price"),
                    "exit_error": exit_err,
                    "theory_pnl": s.get("pnl"),
                    "broker_profit": best.get("profit"),
                    "broker_commission": best.get("commission"),
                    "broker_swap": best.get("swap"),
                    "net_pnl_error": pnl_err,
                    "theory_reason": s.get("exit_reason"),
                    "broker_ticket": best.get("entry_ticket"),
                }
            )
        unmapped_broker = unused
        for b in unmapped_broker:
            quality["mapping_notes"].append(
                f"unmapped_broker {b.get('symbol')}@{b.get('broker_entry_time')} ticket={b.get('entry_ticket')}"
            )
        entry_errs = [m["entry_error"] for m in matches if m["entry_error"] is not None]
        exit_errs = [m["exit_error"] for m in matches if m["exit_error"] is not None]
        pnl_errs = [m["net_pnl_error"] for m in matches if m["net_pnl_error"] is not None]
        return {
            "n_matched": len(matches),
            "n_unmapped_sim": len(unmapped_sim),
            "n_unmapped_broker": len(unmapped_broker),
            "entry_error": _err_stats(entry_errs),
            "exit_error": _err_stats(exit_errs),
            "net_pnl_error": _err_stats(pnl_errs),
            "matches": matches,
            "unmapped_sim": unmapped_sim,
            "unmapped_broker": unmapped_broker,
        }

    mh1 = map_to_broker(h1_a["trades"])
    mm1 = (
        map_to_broker(m1_a["trades"])
        if m1_available
        else {
            "n_matched": 0,
            "n_unmapped_sim": 0,
            "n_unmapped_broker": len(broker_trades),
            "entry_error": _err_stats([]),
            "exit_error": _err_stats([]),
            "net_pnl_error": _err_stats([]),
            "matches": [],
            "unmapped_sim": [],
            "unmapped_broker": broker_trades,
            "skipped": True,
            "reason": m1_meta.get("error") or "1m unavailable",
        }
    )
    (out / "broker_vs_h1_only.json").write_text(json.dumps(mh1, indent=2, default=str), encoding="utf-8")
    (out / "broker_vs_h1_1m.json").write_text(json.dumps(mm1, indent=2, default=str), encoding="utf-8")
    (out / "h1_only_replay_a.json").write_text(json.dumps(h1_a, indent=2), encoding="utf-8")
    (out / "h1_1m_replay_a.json").write_text(json.dumps(m1_a, indent=2), encoding="utf-8")
    (out / "determinism_report.json").write_text(json.dumps(det, indent=2), encoding="utf-8")
    quality["m1_available"] = m1_available
    quality["n_theory_fills"] = len(tape["fills"])
    quality["statistically_thin"] = broker_raw["n"] < 10 or len(
        [t for t in broker_trades if not t.get("still_open")]
    ) < 5
    (out / "data_quality.json").write_text(json.dumps(quality, indent=2), encoding="utf-8")

    h1_entry = (mh1["entry_error"] or {}).get("mae")
    m1_entry = (mm1["entry_error"] or {}).get("mae") if m1_available else None
    h1_exit = (mh1["exit_error"] or {}).get("mae")
    m1_exit = (mm1["exit_error"] or {}).get("mae") if m1_available else None

    # Pipeline-valid = clean 1:1 map + deterministic H1; NOT statistical validation
    mapping_clean = (
        mh1["n_matched"] > 0
        and mh1["n_unmapped_broker"] == 0
        and mh1["n_unmapped_sim"] == 0
    )
    pipeline_ok = bool(mapping_clean and det["h1_only_deterministic"])
    statistical_ok = bool(
        pipeline_ok
        and m1_available
        and det.get("h1_1m_deterministic")
        and not quality["statistically_thin"]
    )

    # Do NOT crown Gate 2 PASS on thin n≈1 round-trip even if errors look small
    if not mapping_clean:
        gate2 = "NO-GO"
        blocker = "Broker deals could not be cleanly mapped 1:1 to Theory intents"
    elif not m1_available:
        gate2 = "INCOMPLETE"
        blocker = f"H1+1m leg blocked: {m1_meta.get('error') or 'MT5 1m unavailable'}"
    elif quality["statistically_thin"]:
        gate2 = "PIPELINE_OK_STATISTICALLY_THIN"
        blocker = (
            "Technical broker↔sim comparison ran, but n is too small for Gate 2 PASS "
            "(need more natural magic=88002 round-trips)"
        )
    else:
        gate2 = "PASS"
        blocker = None

    prefer = "h1_only"
    if m1_available and (
        (m1_exit is not None and h1_exit is not None and m1_exit < h1_exit * 0.8)
        or (m1_entry is not None and h1_entry is not None and m1_entry < h1_entry * 0.8)
    ):
        prefer = "h1_1m"

    answers = {
        "1_h1_only_reproduces_broker": "PARTIAL/SEE_ERRORS" if mh1["n_matched"] else "NO",
        "2_h1_1m_reproduces_broker": (
            "UNAVAILABLE"
            if not m1_available
            else ("PARTIAL/SEE_ERRORS" if mm1["n_matched"] else "NO")
        ),
        "3_lower_entry_error": (
            "UNAVAILABLE"
            if not m1_available
            else ("h1_only" if (h1_entry or 1e9) <= (m1_entry or 1e9) else "h1_1m")
        ),
        "4_lower_exit_error": (
            "UNAVAILABLE"
            if not m1_available
            else ("h1_only" if (h1_exit or 1e9) <= (m1_exit or 1e9) else "h1_1m")
        ),
        "5_better_outcome_agreement": "see matches",
        "6_lower_gross_pnl_error": "see net proxy",
        "7_lower_net_pnl_error": (
            "UNAVAILABLE"
            if not m1_available
            else (
                "h1_only"
                if ((mh1["net_pnl_error"] or {}).get("mae") or 1e9)
                <= ((mm1["net_pnl_error"] or {}).get("mae") or 1e9)
                else "h1_1m"
            )
        ),
        "8_layer_b": "entry/exit/timing vs broker — see broker_vs_*.json",
        "9_layer_c": "commission/swap differences — see matches",
        "10_m1_material_improve": prefer == "h1_1m" if m1_available else None,
        "11_lean_a_justified": prefer == "h1_only" if m1_available else None,
        "12_tick_emulator_justified": False,
        "gate2": gate2,
        "blocker": blocker,
        "pipeline_valid": pipeline_ok,
        "statistically_valid": statistical_ok,
        "statistically_thin": quality["statistically_thin"],
        "strategy_optimization": "NO-GO",
        "prefer_execution": prefer if m1_available else "h1_only_only_available",
        "h1_vs_broker": {"entry_mae": h1_entry, "exit_mae": h1_exit, "matched": mh1["n_matched"]},
        "m1_vs_broker": {
            "entry_mae": m1_entry,
            "exit_mae": m1_exit,
            "matched": mm1["n_matched"],
            "available": m1_available,
        },
        "m1_label": m1_meta.get("label"),
    }
    (out / "final_answers.json").write_text(json.dumps(answers, indent=2), encoding="utf-8")
    (out / "GATE2_VERDICT.json").write_text(
        json.dumps(
            {
                "gate2": gate2,
                "blocker": blocker,
                "pipeline_valid": pipeline_ok,
                "statistically_valid": statistical_ok,
                "statistically_thin": quality["statistically_thin"],
                "strategy_optimization": "NO-GO",
                "answers": answers,
                "quality": quality,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "README.md").write_text(
        "\n".join(
            [
                "# Gate 2 — broker execution fidelity",
                "",
                f"Verdict: **{gate2}**",
                f"Blocker: {blocker}",
                f"Pipeline valid: {pipeline_ok}",
                f"Statistically valid: {statistical_ok}",
                "",
                f"Broker deals (magic 88002): {broker_raw['n']}",
                f"Mapped H1-only: {mh1['n_matched']} (unmapped sim={mh1['n_unmapped_sim']} broker={mh1['n_unmapped_broker']})",
                f"Mapped H1+1m: {mm1['n_matched']} (available={m1_available})",
                "",
                "Strategy optimization: **NO-GO**",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "gate2": gate2,
                "blocker": blocker,
                "pipeline_valid": pipeline_ok,
                "statistically_valid": statistical_ok,
                "answers": answers,
            },
            indent=2,
        )
    )
    # Nonzero unless full statistical PASS
    return 0 if gate2 == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
