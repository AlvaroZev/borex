"""Controlled 8792 reconciliation: Broker / Mirror / Shadow H1 / Closest H1+1m.

Validates simulator fidelity on the existing Aug 27–28 8792 window.
Does NOT tune knobs to fit this sample. Does NOT run grids.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(LIVE / ".env")

from compare_8792_books import (  # noqa: E402
    book_hour,
    parse_html,
    utc_hour,
    yahoo,
)

BASE = ROOT / "data" / "runs" / "mirror_2d_8792"
OUT = BASE / "reconciliation"
HTML = Path(r"c:\Users\azeva\OneDrive\Documentos\ReportHistory-53019882.html")
DUMP = BASE / "broker_theory_dump.json"
PRIOR_BT = BASE / "backtest.json"

ENTRY_START = pd.Timestamp("2026-08-27 01:00:00", tz="UTC")
LIVE_END = datetime(2026, 8, 28, 23, 59, tzinfo=timezone.utc)
WARMUP_DAYS = 400
GHOST_PRIME_BARS = 72
CAPITAL = 1000.0
SERVER_UTC_HOURS = 3

# Confirmed / reconstructed 8792 window knobs (see knobs_audit.json).
KNOBS = dict(
    strategy="alexg8",
    ghost_sl_mult=0.6,
    leverage=5000.0,
    rr_mode="dynamic",
    true_sl_rr=2.0,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=6.0,
    position_size_pct=0.01,
    size_mode="margin",
    true_sl=True,
    max_positions=60,
    commission_per_lot=7.0,
    min_commission_per_side=0.04,
    lot_notional=100_000.0,
    risk_include_commission=True,
    commission_at_entry=False,
    force_flat_friday=False,
    force_flat_daily=True,
    force_flat_utc_hour=19,
    intra_hour_sl=False,
    ghost_fill_at_close=True,
)


@dataclass
class TradeEvent:
    source: str
    pair: str
    direction: str
    signal_time: Any = None
    signal_price: Any = None
    ghost_entry: Any = None
    actual_entry: Any = None
    entry_bid: Any = None
    entry_ask: Any = None
    position_size: Any = None
    leverage: Any = None
    sl_price: Any = None
    tp_price: Any = None
    exit_time: Any = None
    exit_price: Any = None
    exit_bid: Any = None
    exit_ask: Any = None
    exit_reason: Any = None
    commission: Any = None
    swap: Any = None
    gross_pnl: Any = None
    net_pnl: Any = None
    starting_equity: Any = None
    ending_equity: Any = None
    ticket: Any = None
    pattern: Any = None
    margin: Any = None
    rr_used: Any = None
    raw_id: Any = None

    def to_dict(self) -> dict:
        return asdict(self)


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _side(x: str) -> str:
    s = str(x or "").lower()
    return "long" if s in ("buy", "long") else "short"


def _pair(x: str) -> str:
    return yahoo(x)


def _parse_ghost(pattern: str) -> dict:
    out = {"ghost_entry": None, "ghost_sl": None, "ghost_tp": None, "created_index": None}
    p = str(pattern or "")
    if "|g:" not in p and not p.startswith("g:"):
        return out
    try:
        frag = p.split("|g:")[-1] if "|g:" in p else p[p.find("g:") :]
        frag = frag.split("|")[0]
        parts = frag.split(":")
        # g:idx:entry:sl:tp
        if len(parts) >= 5 and parts[0] == "g":
            out["created_index"] = int(parts[1])
            out["ghost_entry"] = float(parts[2])
            out["ghost_sl"] = float(parts[3])
            out["ghost_tp"] = float(parts[4])
    except Exception:
        pass
    return out


def _exit_bucket(reason: str) -> str:
    w = str(reason or "").lower()
    if "margin" in w or "dollar_sl" in w or w in ("sl", "stop_loss", "stopout") or "[sl" in w:
        return "sl"
    if "take_profit" in w or w == "tp" or "[tp" in w:
        return "tp"
    if "daily" in w or "flat" in w or "borex_flat" in w:
        return "daily"
    if "end_of" in w:
        return "eod"
    if "mt5_closed" in w:
        return "sl"  # live often labels broker SL as mt5_closed
    if not w or w == "other":
        return "other"
    return "other"


def write_knobs_audit() -> dict:
    """Document what can/cannot be reconstructed for the 8792 window."""
    dump = json.loads(DUMP.read_text(encoding="utf-8"))
    theory = dump.get("theory_closed") or []
    prior = json.loads(PRIOR_BT.read_text(encoding="utf-8")) if PRIOR_BT.exists() else {}

    # Leverage from true_sl margin wipe distance
    levs = []
    for t in theory:
        e = float(t.get("entry_price") or 0)
        sl = float(t.get("stop_loss") or 0)
        if e > 0 and sl > 0:
            r = abs(sl - e) / e
            if r > 1e-12:
                levs.append(int(round(1.0 / r)))
    lev_mode = Counter(levs).most_common(1)[0] if levs else (None, 0)

    # ghost_sl_mult from pattern geometry + min_rr=2
    mult_hits = 0
    mult_checks = 0
    for t in theory:
        g = _parse_ghost(t.get("pattern") or "")
        ge, gsl, gtp = g["ghost_entry"], g["ghost_sl"], g["ghost_tp"]
        if ge is None or gsl is None or gtp is None:
            continue
        side = _side(t.get("side"))
        ghost_risk = abs(ge - gsl)
        if ghost_risk <= 0:
            continue
        # structural risk if mult=0.6; TP at min_rr * structural from entry
        for cand in (0.6, 1.0, 0.8, 1.2):
            struct = ghost_risk / cand
            expect_tp = ge - 2.0 * struct if side == "short" else ge + 2.0 * struct
            if abs(expect_tp - gtp) / max(abs(gtp), 1e-12) < 1e-6:
                mult_checks += 1
                if cand == 0.6:
                    mult_hits += 1
                break

    rr_used = Counter(round(float(t.get("rr_used") or 0), 2) for t in theory)
    reasons = Counter(str(t.get("exit_reason") or "") for t in theory)
    fill_close = sum(1 for t in theory if "fill:close" in str(t.get("pattern") or ""))

    audit = {
        "window": {
            "entry_start_utc": str(ENTRY_START),
            "live_end_utc": LIVE_END.isoformat(),
            "account": 53019882,
            "port": 8792,
            "magic_note": "mirror 88002 path; do not touch 8791/88001",
        },
        "dump_limitations": {
            "runtime_object_saved": False,
            "dash_raw_meta_keys": ["mode", "cash", "account"],
            "note": (
                "broker_theory_dump.json did not persist live runtime/CLI. "
                "Knobs below are reconstructed from theory geometry, exit "
                "reasons, patterns, and the prior same-window offline backtest."
            ),
        },
        "knobs": {
            "strategy": {
                "value": "alexg8",
                "confidence": "confirmed",
                "evidence": "pattern prefix alexg8 on all theory trades",
            },
            "ghost_sl_mult": {
                "value": 0.6,
                "confidence": "confirmed",
                "evidence": (
                    f"ghost pattern TP matches min_rr=2 * (ghost_risk/0.6) on "
                    f"{mult_hits}/{mult_checks} checked trades; AlexG8 default; "
                    "mirrorservice has no --ghost-sl-mult override"
                ),
            },
            "ghost_fill_at_close": {
                "value": True,
                "confidence": "confirmed",
                "evidence": f"fill:close in {fill_close}/{len(theory)} theory patterns",
            },
            "leverage": {
                "value": lev_mode[0],
                "confidence": "confirmed",
                "evidence": f"abs(SL-entry)/entry ≈ 1/L on {lev_mode[1]}/{len(theory)} trades",
            },
            "true_sl_rr / min_rr": {
                "value": 2.0,
                "confidence": "confirmed",
                "evidence": "ghost TP geometry + rr_used floor at 2.0",
            },
            "rr_mode": {
                "value": "dynamic",
                "confidence": "confirmed",
                "evidence": "rr_used takes multiple clamped values (not a single fixed RR)",
            },
            "rr_min": {
                "value": 2.0,
                "confidence": "confirmed",
                "evidence": f"rr_used distribution={dict(rr_used)}",
            },
            "rr_max": {
                "value": 6.0,
                "confidence": "confirmed",
                "evidence": f"rr_used distribution={dict(rr_used)}",
            },
            "rr_factor": {
                "value": 1.0,
                "confidence": "high",
                "evidence": (
                    "only rr_used∈{2,6}; prior same-window backtest used 1.0; "
                    "not persisted in dump — if live used another factor, "
                    "signal RR clamps still bind at 2/6"
                ),
            },
            "force_flat_daily": {
                "value": True,
                "confidence": "confirmed",
                "evidence": f"theory exit_reasons include daily_close={reasons.get('daily_close', 0)}",
            },
            "force_flat_friday": {
                "value": False,
                "confidence": "high",
                "evidence": (
                    "Fri 2026-08-28 traded through session; prior backtest OFF; "
                    "daily flatten already covers 19:00 UTC"
                ),
            },
            "force_flat_utc_hour": {
                "value": 19,
                "confidence": "high",
                "evidence": "engine/MirrorConfig default; daily_close cluster at :00 after 19h window",
            },
            "position_size_pct": {
                "value": 0.01,
                "confidence": "high",
                "evidence": "mirrorservice default; margin≈1% equity trajectory",
            },
            "max_positions": {
                "value": 60,
                "confidence": "high",
                "evidence": "mirrorservice default; prior backtest used 60",
            },
            "occupancy": {
                "value": "one_open_per_pair + max_positions=60",
                "confidence": "high",
                "evidence": "MultiMarketEngine / mirror defaults",
            },
            "commission_per_lot": {
                "value": 7.0,
                "confidence": "confirmed",
                "evidence": "broker commission ≈ $3.50/lot/side (round-turn $7)",
            },
            "risk_include_commission": {
                "value": True,
                "confidence": "assumed",
                "evidence": (
                    "NOT in dump. Prior same-window offline backtest used True. "
                    "mirrorservice CLI default is False. Accounting layer may be "
                    "contaminated if live differed."
                ),
                "blocker_if_strict": False,
            },
            "commission_at_entry": {
                "value": False,
                "confidence": "assumed",
                "evidence": (
                    "NOT in dump. Prior same-window offline backtest used False. "
                    "mirrorservice CLI default is True."
                ),
                "blocker_if_strict": False,
            },
            "spread_assumptions": {
                "value": "MT5 Raw snapshot at prior backtest time (per-symbol)",
                "confidence": "medium",
                "evidence": "spread_pips_by_symbol in backtest.json; not tick-path spreads",
            },
            "intra_hour_sl (H1-only)": {
                "value": False,
                "confidence": "confirmed",
                "evidence": "prior backtest + live H1 shadow does not wick-exit on H1 alone",
            },
            "starting_capital": {
                "value": 1000.0,
                "confidence": "confirmed",
                "evidence": "MT5 report initial deposit 1000.00",
            },
        },
        "replay_knobs_used": KNOBS,
        "prior_backtest_knobs": prior.get("knobs"),
        "missing_not_blocking": [
            "Exact launch argv (reconstructed from outcomes)",
            "Per-fill historical spread (using Raw snapshot)",
            "risk_include_commission / commission_at_entry (assumed from prior parity backtest)",
        ],
        "would_block_if_absent": [
            "strategy identity",
            "ghost_sl_mult",
            "leverage",
            "RR clamps",
            "daily flatten on/off",
        ],
        "verdict": (
            "PROCEED: signal/exec knobs reconstructed with confirmed/high confidence. "
            "Accounting commission flags assumed from prior same-window backtest — "
            "flag Layer C if PnL gaps concentrate in commission rather than fills."
        ),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "knobs_audit.json"
    path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    print("wrote", path, flush=True)
    return audit


def normalize_broker(rows: list[dict]) -> list[TradeEvent]:
    out = []
    for r in rows:
        # broker comment is entry tag, not exit reason — exit_reason may be set by compare
        reason = r.get("exit_reason") or "other"
        if reason == "other":
            # infer from SL/TP vs close
            close = float(r.get("close") or 0)
            sl = float(r.get("sl") or 0)
            tp = float(r.get("tp") or 0)
            if sl and abs(close - sl) <= max(1e-5, abs(sl) * 1e-5):
                reason = "sl"
            elif tp and abs(close - tp) <= max(1e-5, abs(tp) * 1e-5):
                reason = "tp"
        profit = float(r.get("profit") or 0)
        comm = float(r.get("commission") or 0)
        swap = float(r.get("swap") or 0)
        out.append(
            TradeEvent(
                source="broker",
                pair=_pair(r.get("symbol") or ""),
                direction=_side(r.get("side")),
                # open ≈ signal for market-at-close path
                signal_time=str(r.get("open_time") or ""),
                actual_entry=float(r.get("open") or 0),
                position_size=float(r.get("volume") or 0),
                leverage=KNOBS["leverage"],
                sl_price=float(r.get("sl") or 0) or None,
                tp_price=float(r.get("tp") or 0) or None,
                exit_time=str(r.get("close_time") or ""),
                exit_price=float(r.get("close") or 0),
                exit_reason=reason,
                commission=comm,
                swap=swap,
                gross_pnl=profit,
                net_pnl=float(r.get("net") or (profit + comm + swap)),
                ticket=r.get("ticket"),
                pattern=r.get("comment"),
                raw_id=r.get("ticket"),
            )
        )
    return out


def normalize_book(rows: list[dict], source: str) -> list[TradeEvent]:
    out = []
    for r in rows:
        g = _parse_ghost(r.get("pattern") or "")
        entry = float(r.get("entry_price") or r.get("entry") or 0)
        exit_p = float(r.get("exit_price") or r.get("exit") or 0)
        pnl = float(r.get("pnl") or 0)
        out.append(
            TradeEvent(
                source=source,
                pair=_pair(r.get("symbol") or ""),
                direction=_side(r.get("side")),
                signal_time=str(r.get("entry_time") or ""),
                signal_price=g.get("ghost_entry"),
                ghost_entry=g.get("ghost_entry"),
                actual_entry=entry,
                position_size=None,
                leverage=KNOBS["leverage"],
                sl_price=float(r.get("stop_loss") or r.get("sl") or 0) or None,
                tp_price=float(r.get("take_profit") or r.get("tp") or 0) or None,
                exit_time=str(r.get("exit_time") or ""),
                exit_price=exit_p,
                exit_reason=r.get("exit_reason"),
                commission=None,
                swap=None,
                gross_pnl=None,
                net_pnl=pnl,
                pattern=r.get("pattern"),
                margin=float(r.get("margin") or 0) or None,
                rr_used=float(r.get("rr_used") or r.get("rr") or 0) or None,
                raw_id=r.get("id"),
            )
        )
    return out


def normalize_offline(rows: list[dict], source: str) -> list[TradeEvent]:
    out = []
    for r in rows:
        g = _parse_ghost(r.get("pattern") or "")
        out.append(
            TradeEvent(
                source=source,
                pair=_pair(r.get("symbol") or ""),
                direction=_side(r.get("side")),
                signal_time=str(r.get("entry_time") or ""),
                signal_price=g.get("ghost_entry"),
                ghost_entry=g.get("ghost_entry"),
                actual_entry=float(r.get("entry") or r.get("entry_price") or 0),
                leverage=KNOBS["leverage"],
                sl_price=float(r.get("sl") or r.get("stop_loss") or 0) or None,
                tp_price=float(r.get("tp") or r.get("take_profit") or 0) or None,
                exit_time=str(r.get("exit_time") or ""),
                exit_price=float(r.get("exit") or r.get("exit_price") or 0),
                exit_reason=r.get("exit_reason"),
                net_pnl=float(r.get("pnl") or 0),
                pattern=r.get("pattern"),
                margin=float(r.get("margin") or 0) or None,
                rr_used=float(r.get("rr") or r.get("rr_used") or 0) or None,
            )
        )
    return out


def _entry_hour_key(ev: TradeEvent, *, broker: bool) -> str:
    ts = ev.signal_time or ""
    if broker:
        return utc_hour(ts)
    return book_hour(ts)


def match_events(
    reference: list[TradeEvent],
    candidate: list[TradeEvent],
    *,
    ref_broker: bool,
    cand_broker: bool = False,
    max_hour_shift: int = 1,
) -> dict:
    """Deterministic greedy match: pair + direction + temporal proximity."""
    bucket: dict[tuple, list[TradeEvent]] = defaultdict(list)
    for ev in candidate:
        hour = _entry_hour_key(ev, broker=cand_broker)
        bucket[(ev.pair, ev.direction, hour)].append(ev)

    matched = []
    unmatched_ref = []
    for ref in reference:
        hour = _entry_hour_key(ref, broker=ref_broker)
        found = None
        used_shift = 0
        for sh in range(0, max_hour_shift + 1):
            shifts = [0] if sh == 0 else [-sh, sh]
            for s in shifts:
                h = hour
                if s and h:
                    h = (
                        pd.Timestamp(h) + pd.Timedelta(hours=s)
                    ).strftime("%Y-%m-%d %H:00")
                key = (ref.pair, ref.direction, h)
                if bucket.get(key):
                    found = bucket[key].pop(0)
                    used_shift = s
                    break
            if found is not None:
                break
        if found is None:
            unmatched_ref.append(ref)
            continue

        entry_err = None
        exit_err = None
        if ref.actual_entry and found.actual_entry:
            entry_err = abs(float(found.actual_entry) - float(ref.actual_entry))
        if ref.exit_price and found.exit_price:
            exit_err = abs(float(found.exit_price) - float(ref.exit_price))
        pnl_err = None
        if ref.net_pnl is not None and found.net_pnl is not None:
            pnl_err = float(found.net_pnl) - float(ref.net_pnl)

        same_signal = True  # matched on pair/dir/time
        same_dir = ref.direction == found.direction
        exit_same = _exit_bucket(ref.exit_reason) == _exit_bucket(found.exit_reason)
        entry_close = entry_err is not None and (
            entry_err <= max(1e-4, abs(float(ref.actual_entry or 0)) * 1e-4)
            or entry_err / max(abs(float(ref.actual_entry or 1)), 1e-12) < 0.0005
        )
        exit_close = exit_err is not None and (
            exit_err <= max(1e-4, abs(float(ref.exit_price or 0)) * 1e-4)
            or exit_err / max(abs(float(ref.exit_price or 1)), 1e-12) < 0.0005
        )

        if same_signal and same_dir and entry_close and exit_same and exit_close:
            kind = "same_trade_accounting_diff" if (pnl_err and abs(pnl_err) > 0.5) else "aligned"
        elif same_signal and same_dir and entry_close and not exit_same:
            kind = "same_fill_different_exit"
        elif same_signal and same_dir and not entry_close:
            kind = "same_signal_different_fill"
        else:
            kind = "partial_match"

        matched.append(
            {
                "kind": kind,
                "hour_shift": used_shift,
                "ref": ref.to_dict(),
                "cand": found.to_dict(),
                "entry_abs_err": entry_err,
                "exit_abs_err": exit_err,
                "pnl_err": pnl_err,
                "exit_bucket_ref": _exit_bucket(ref.exit_reason),
                "exit_bucket_cand": _exit_bucket(found.exit_reason),
                "checks": {
                    "same_signal": same_signal,
                    "same_direction": same_dir,
                    "entry_approx": bool(entry_close),
                    "exit_reason_same": exit_same,
                    "exit_approx": bool(exit_close),
                    "gross_pnl_approx": None,
                    "net_pnl_approx": bool(pnl_err is not None and abs(pnl_err) < 1.0),
                },
            }
        )

    unmatched_cand = [ev for vals in bucket.values() for ev in vals]
    return {
        "matched": matched,
        "unmatched_ref": [e.to_dict() for e in unmatched_ref],
        "unmatched_cand": [e.to_dict() for e in unmatched_cand],
        "n_matched": len(matched),
        "n_missing": len(unmatched_ref),
        "n_extra": len(unmatched_cand),
    }


def _mae(vals: list[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None and not math.isnan(float(v))]
    if not xs:
        return None
    return sum(xs) / len(xs)


def summarize_match(name: str, m: dict, *, ref_label: str, cand_label: str) -> dict:
    matched = m["matched"]
    entry_errs = [x["entry_abs_err"] for x in matched]
    exit_errs = [x["exit_abs_err"] for x in matched]
    pnl_errs = [x["pnl_err"] for x in matched if x["pnl_err"] is not None]
    exit_mismatch = sum(
        1 for x in matched if x["exit_bucket_ref"] != x["exit_bucket_cand"]
    )
    kinds = Counter(x["kind"] for x in matched)
    ref_pnl = sum(float(x["ref"].get("net_pnl") or 0) for x in matched)
    cand_pnl = sum(float(x["cand"].get("net_pnl") or 0) for x in matched)
    ref_comm = sum(
        float(x["ref"].get("commission") or 0)
        for x in matched
        if x["ref"].get("commission") is not None
    )
    ref_swap = sum(
        float(x["ref"].get("swap") or 0)
        for x in matched
        if x["ref"].get("swap") is not None
    )
    return {
        "pair": f"{ref_label} vs {cand_label}",
        "signal_overlap_pct": round(
            100 * m["n_matched"] / max(m["n_matched"] + m["n_missing"], 1), 1
        ),
        "matched_trades": m["n_matched"],
        "missing_trades": m["n_missing"],
        "extra_trades": m["n_extra"],
        "entry_mae": _mae(entry_errs),
        "exit_mae": _mae(exit_errs),
        "mean_pnl_error": (sum(pnl_errs) / len(pnl_errs)) if pnl_errs else None,
        "mean_abs_pnl_error": (
            sum(abs(x) for x in pnl_errs) / len(pnl_errs) if pnl_errs else None
        ),
        "total_pnl_ref_matched": round(ref_pnl, 2),
        "total_pnl_cand_matched": round(cand_pnl, 2),
        "commission_ref_matched": round(ref_comm, 2) if ref_comm else None,
        "swap_ref_matched": round(ref_swap, 2) if ref_swap else None,
        "exit_mismatch_pct": round(100 * exit_mismatch / max(len(matched), 1), 1),
        "kinds": dict(kinds),
        "checks_pass_rate": {
            k: round(
                100
                * sum(1 for x in matched if x["checks"].get(k))
                / max(len(matched), 1),
                1,
            )
            for k in (
                "same_signal",
                "same_direction",
                "entry_approx",
                "exit_reason_same",
                "exit_approx",
                "net_pnl_approx",
            )
        },
    }


def classify_h1_vs_1m(
    broker: list[TradeEvent],
    h1_events: list[TradeEvent],
    m1_events: list[TradeEvent],
) -> dict:
    m_h1 = match_events(broker, h1_events, ref_broker=True)
    m_m1 = match_events(broker, m1_events, ref_broker=True)

    # Index matches by (pair, direction, broker hour)
    def idx(m):
        d = {}
        for row in m["matched"]:
            ref = row["ref"]
            hour = utc_hour(ref.get("signal_time") or "")
            d[(ref["pair"], ref["direction"], hour)] = row
        return d

    ih, im = idx(m_h1), idx(m_m1)
    keys = set(ih) | set(im)
    classes = Counter()
    rows = []
    for k in keys:
        h = ih.get(k)
        m = im.get(k)
        # "agrees" = exit price within tolerance OR exit bucket+pnl closer
        def agrees(row):
            if row is None:
                return False
            return bool(row["checks"]["exit_approx"]) or (
                row["checks"]["exit_reason_same"]
                and row["pnl_err"] is not None
                and abs(row["pnl_err"]) < 2.0
            )

        ah, am = agrees(h), agrees(m)
        if ah and am:
            cls = "both_agree"
        elif ah and not am:
            cls = "broker_agrees_h1"
        elif am and not ah:
            cls = "broker_agrees_1m"
        else:
            cls = "neither_agrees"
        classes[cls] += 1
        rows.append(
            {
                "key": {"pair": k[0], "direction": k[1], "hour": k[2]},
                "class": cls,
                "h1_pnl_err": None if h is None else h.get("pnl_err"),
                "m1_pnl_err": None if m is None else m.get("pnl_err"),
                "h1_exit_err": None if h is None else h.get("exit_abs_err"),
                "m1_exit_err": None if m is None else m.get("exit_abs_err"),
                "h1_exit_bucket": None if h is None else h.get("exit_bucket_cand"),
                "m1_exit_bucket": None if m is None else m.get("exit_bucket_cand"),
                "broker_exit_bucket": (h or m)["exit_bucket_ref"] if (h or m) else None,
                "broker_pnl": (h or m)["ref"].get("net_pnl") if (h or m) else None,
            }
        )

    def mean_abs(field, cls=None):
        xs = [
            abs(float(r[field]))
            for r in rows
            if r.get(field) is not None and (cls is None or r["class"] == cls)
        ]
        return (sum(xs) / len(xs)) if xs else None

    return {
        "n_keys": len(keys),
        "classes": dict(classes),
        "mean_abs_pnl_err_h1": mean_abs("h1_pnl_err"),
        "mean_abs_pnl_err_1m": mean_abs("m1_pnl_err"),
        "rows": rows,
    }


def run_offline(variant: str, data, m1, spreads, master, prime_i) -> dict:
    from borex.alexg import AlexG8Strategy
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    strategy = AlexG8Strategy(
        min_rr=float(KNOBS["true_sl_rr"]),
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(KNOBS["ghost_sl_mult"]),
    )
    config = BacktestConfig(
        initial_capital=CAPITAL,
        leverage=float(KNOBS["leverage"]),
        position_size_pct=float(KNOBS["position_size_pct"]),
        size_mode=KNOBS["size_mode"],
        true_sl=True,
        true_sl_rr=float(KNOBS["true_sl_rr"]),
        rr_mode=KNOBS["rr_mode"],
        rr_factor=float(KNOBS["rr_factor"]),
        rr_min=float(KNOBS["rr_min"]),
        rr_max=float(KNOBS["rr_max"]),
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=float(KNOBS["commission_per_lot"]),
        min_commission_per_side=float(KNOBS["min_commission_per_side"]),
        lot_notional=float(KNOBS["lot_notional"]),
        risk_include_commission=bool(KNOBS["risk_include_commission"]),
        commission_at_entry=bool(KNOBS["commission_at_entry"]),
        force_flat_friday=bool(KNOBS["force_flat_friday"]),
        force_flat_daily=bool(KNOBS["force_flat_daily"]),
        force_flat_utc_hour=int(KNOBS["force_flat_utc_hour"]),
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=bool(KNOBS["intra_hour_sl"]),
    )
    engine = MultiMarketEngine(strategy, config, max_positions=int(KNOBS["max_positions"]))
    engine._progress_every = 50
    t0 = time.perf_counter()
    if variant == "shadow_h1":
        result = engine.run(
            data,
            timeframe="1h",
            master_symbol=master,
            same_bar_exit=False,
            start_master_i=prime_i,
        )
    elif variant == "closest_h1_1m":
        result = engine.run(
            data,
            timeframe="1h+1m",
            master_symbol=master,
            same_bar_exit=False,
            start_master_i=prime_i,
            ltf_by_symbol=m1,
        )
    else:
        raise ValueError(variant)
    elapsed = time.perf_counter() - t0
    window = [t for t in result.trades if _ts(t.entry_time) >= ENTRY_START]

    def row(t):
        return {
            "symbol": t.symbol,
            "side": t.side.value if hasattr(t.side, "value") else str(t.side),
            "entry_time": str(t.entry_time),
            "exit_time": str(t.exit_time),
            "entry": float(t.entry_price),
            "exit": float(t.exit_price or 0),
            "sl": float(t.stop_loss or 0),
            "tp": float(t.take_profit or 0),
            "pnl": float(t.pnl),
            "margin": float(t.margin or 0),
            "rr": float(t.score or 0),
            "exit_reason": t.exit_reason,
            "pattern": str(t.pattern or ""),
        }

    report = {
        "variant": variant,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "knobs": KNOBS,
        "master": master,
        "h1_pairs": len(data),
        "m1_pairs": len(m1 or {}),
        "entry_start": str(ENTRY_START),
        "last_bar": str(data[master][-1].timestamp),
        "n_trades_window": len(window),
        "n_trades_full": int(result.total_trades),
        "final_equity": float(result.final_equity),
        "max_drawdown_pct": float(result.max_drawdown_pct),
        "exit_reasons": dict(Counter(t.exit_reason for t in window)),
        "net_pnl": sum(float(t.pnl) for t in window),
        "elapsed_sec": elapsed,
        "trades": [row(t) for t in window],
    }
    path = OUT / f"offline_{variant}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(
        f"[{variant}] trades={len(window)} net={report['net_pnl']:.2f} "
        f"equity={report['final_equity']:.2f} in {elapsed/60:.1f}m -> {path}",
        flush=True,
    )
    return report


def load_market():
    from run_mt5_1m_lev_split import load_mt5
    from borex.alexg.multi_market import pick_master_symbol
    from borex.alexg import AlexG8Strategy

    end = LIVE_END
    h1_start = end - timedelta(days=WARMUP_DAYS)
    m1_start = ENTRY_START.to_pydatetime() - timedelta(days=2)
    # Pin universe to prior backtest spreads keys (same 60 pairs).
    prior = json.loads(PRIOR_BT.read_text(encoding="utf-8"))
    symbols = sorted((prior.get("spread_pips_by_symbol") or {}).keys())
    spreads_prior = prior.get("spread_pips_by_symbol") or {}
    print(f"Pinned symbols n={len(symbols)}", flush=True)
    h1, m1, spreads = load_mt5(h1_start, end, m1_start, symbols=symbols)
    # Prefer prior Raw snapshot for reproducibility of this reconciliation.
    for k, v in spreads_prior.items():
        spreads.setdefault(k, v)
    for k, v in spreads_prior.items():
        spreads[k] = v
    master = pick_master_symbol(h1)
    master_bars = h1[master]
    entry_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= ENTRY_START),
        None,
    )
    if entry_i is None:
        raise SystemExit("no master bar on/after entry start")
    warmup = int(AlexG8Strategy().min_bars)
    prime_i = max(warmup, entry_i - GHOST_PRIME_BARS)
    return h1, m1, spreads, master, prime_i


def largest_mismatches(m: dict, n: int = 12) -> list[dict]:
    rows = [x for x in m["matched"] if x.get("pnl_err") is not None]
    rows.sort(key=lambda x: abs(float(x["pnl_err"])), reverse=True)
    out = []
    for x in rows[:n]:
        out.append(
            {
                "kind": x["kind"],
                "pair": x["ref"]["pair"],
                "direction": x["ref"]["direction"],
                "broker_entry_time": x["ref"].get("signal_time"),
                "broker_entry": x["ref"].get("actual_entry"),
                "broker_exit": x["ref"].get("exit_price"),
                "broker_exit_reason": x["ref"].get("exit_reason"),
                "broker_pnl": x["ref"].get("net_pnl"),
                "sim_entry": x["cand"].get("actual_entry"),
                "sim_exit": x["cand"].get("exit_price"),
                "sim_exit_reason": x["cand"].get("exit_reason"),
                "sim_pnl": x["cand"].get("net_pnl"),
                "pnl_err": x["pnl_err"],
                "entry_abs_err": x["entry_abs_err"],
                "exit_abs_err": x["exit_abs_err"],
                "component": (
                    "execution"
                    if x["kind"] in ("same_signal_different_fill", "same_fill_different_exit")
                    else "accounting"
                    if x["kind"] == "same_trade_accounting_diff"
                    else "mixed"
                ),
            }
        )
    return out


def fidelity_verdict(agg: dict, h1m1: dict) -> dict:
    """Classify closest-sim fidelity for System A research."""
    closest = agg.get("Broker_vs_Closest_H1_1m") or {}
    shadow = agg.get("Broker_vs_Shadow_H1") or {}
    mirror = agg.get("Broker_vs_Mirror") or {}

    overlap = float(closest.get("signal_overlap_pct") or 0)
    mae_pnl = closest.get("mean_abs_pnl_error")
    exit_mm = float(closest.get("exit_mismatch_pct") or 100)
    mirror_gap = abs(float((mirror.get("mean_pnl_error") or 0)))

    m1_better = False
    if h1m1.get("mean_abs_pnl_err_1m") is not None and h1m1.get("mean_abs_pnl_err_h1") is not None:
        m1_better = h1m1["mean_abs_pnl_err_1m"] < h1m1["mean_abs_pnl_err_h1"] * 0.85

    # Heuristics — instrument validation, not profitability.
    if (
        overlap >= 85
        and mae_pnl is not None
        and mae_pnl < 2.0
        and exit_mm < 25
        and mirror_gap < 1.0
    ):
        grade = "A"
        text = "broker-faithful enough for absolute research"
    elif overlap >= 50 and mae_pnl is not None and mae_pnl < 8.0:
        grade = "B"
        text = "useful for relative ranking but not absolute expectancy"
    else:
        grade = "C"
        text = "insufficiently faithful for meaningful strategy research"

    if grade == "A" and not m1_better and float(shadow.get("mean_abs_pnl_error") or 99) < float(
        mae_pnl or 99
    ):
        # Downgrade caution: if H1 is closer, absolute trust in 1m path is weak.
        grade = "B"
        text = (
            "useful for relative ranking but not absolute expectancy "
            "(H1 unexpectedly closer than 1m on this window)"
        )

    next_action = "continue closest-sim research"
    if grade == "C":
        next_action = "fix specific simulator mechanics"
    elif exit_mm >= 40:
        next_action = "improve execution model"
    elif overlap < 70:
        next_action = "fix specific simulator mechanics"
    elif not m1_better and grade == "B":
        next_action = "improve execution model"
    elif mae_pnl is not None and mae_pnl > 3 and overlap >= 80:
        next_action = "improve market-data model"

    return {
        "grade": grade,
        "label": text,
        "next_action": next_action,
        "m1_improves_vs_h1": m1_better,
        "notes": [
            f"Broker↔Mirror remains the reference (mean PnL gap ~{mirror.get('mean_pnl_error')})",
            f"Closest overlap={overlap}% exit_mismatch={exit_mm}% MAE_pnl={mae_pnl}",
            f"1m vs H1 class counts: {h1m1.get('classes')}",
        ],
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    audit = write_knobs_audit()
    print("KNOBS verdict:", audit["verdict"], flush=True)

    html = parse_html(HTML)
    for r in html["closed"]:
        c = str(r.get("comment") or "").lower()
        if "[sl" in c:
            r["exit_reason"] = "sl"
        elif "[tp" in c:
            r["exit_reason"] = "tp"
        elif "borex_flat" in c:
            r["exit_reason"] = "daily_flat"
        else:
            # price-infer later in normalize
            r["exit_reason"] = r.get("exit_reason") or "other"

    dump = json.loads(DUMP.read_text(encoding="utf-8"))
    broker_ev = normalize_broker(html["closed"])
    mirror_ev = normalize_book(dump.get("mirror_closed") or [], "mirror")
    theory_ev = normalize_book(dump.get("theory_closed") or [], "theory")

    # Persist normalized books (do not overwrite originals).
    books_path = OUT / "normalized_books.json"
    books_path.write_text(
        json.dumps(
            {
                "broker": [e.to_dict() for e in broker_ev],
                "mirror": [e.to_dict() for e in mirror_ev],
                "theory": [e.to_dict() for e in theory_ev],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print("wrote", books_path, "broker", len(broker_ev), flush=True)

    print("Loading market data + running offline variants...", flush=True)
    h1, m1, spreads, master, prime_i = load_market()
    shadow_rep = run_offline("shadow_h1", h1, None, spreads, master, prime_i)
    closest_rep = run_offline("closest_h1_1m", h1, m1, spreads, master, prime_i)

    shadow_ev = normalize_offline(shadow_rep["trades"], "shadow_h1")
    closest_ev = normalize_offline(closest_rep["trades"], "closest_h1_1m")

    matches = {
        "Broker_vs_Mirror": match_events(broker_ev, mirror_ev, ref_broker=True),
        "Broker_vs_Theory": match_events(broker_ev, theory_ev, ref_broker=True),
        "Broker_vs_Shadow_H1": match_events(broker_ev, shadow_ev, ref_broker=True),
        "Broker_vs_Closest_H1_1m": match_events(broker_ev, closest_ev, ref_broker=True),
        "Mirror_vs_Shadow_H1": match_events(mirror_ev, shadow_ev, ref_broker=False),
        "Mirror_vs_Closest_H1_1m": match_events(mirror_ev, closest_ev, ref_broker=False),
    }

    agg = {
        name: summarize_match(name, m, ref_label=name.split("_vs_")[0], cand_label=name.split("_vs_")[1])
        for name, m in matches.items()
    }

    # Comparison matrix requested by charter
    matrix = {}
    for label, key in [
        ("Broker", None),
        ("Mirror", "Broker_vs_Mirror"),
        ("Shadow_H1", "Broker_vs_Shadow_H1"),
        ("Closest_H1_1m", "Broker_vs_Closest_H1_1m"),
    ]:
        if key is None:
            matrix[label] = {
                "n": len(broker_ev),
                "total_pnl": round(sum(float(e.net_pnl or 0) for e in broker_ev), 2),
                "commission": round(sum(float(e.commission or 0) for e in broker_ev), 2),
                "swap": round(sum(float(e.swap or 0) for e in broker_ev), 2),
            }
        else:
            s = agg[key]
            matrix[label] = {
                "signal_overlap_pct_vs_broker": s["signal_overlap_pct"],
                "matched_trades": s["matched_trades"],
                "entry_mae": s["entry_mae"],
                "exit_mae": s["exit_mae"],
                "mean_pnl_error": s["mean_pnl_error"],
                "mean_abs_pnl_error": s["mean_abs_pnl_error"],
                "total_pnl_matched": s["total_pnl_cand_matched"],
                "exit_mismatch_pct": s["exit_mismatch_pct"],
                "missing_trades": s["missing_trades"],
                "extra_trades": s["extra_trades"],
            }

    h1m1 = classify_h1_vs_1m(broker_ev, shadow_ev, closest_ev)
    verdict = fidelity_verdict(agg, h1m1)

    # Layer summaries
    layers = {
        "A_signal": {
            "broker_n": len(broker_ev),
            "mirror_overlap": agg["Broker_vs_Mirror"]["signal_overlap_pct"],
            "theory_overlap": agg["Broker_vs_Theory"]["signal_overlap_pct"],
            "shadow_overlap": agg["Broker_vs_Shadow_H1"]["signal_overlap_pct"],
            "closest_overlap": agg["Broker_vs_Closest_H1_1m"]["signal_overlap_pct"],
            "shadow_missing": agg["Broker_vs_Shadow_H1"]["missing_trades"],
            "shadow_extra": agg["Broker_vs_Shadow_H1"]["extra_trades"],
            "closest_missing": agg["Broker_vs_Closest_H1_1m"]["missing_trades"],
            "closest_extra": agg["Broker_vs_Closest_H1_1m"]["extra_trades"],
        },
        "B_execution": {
            "shadow_entry_mae": agg["Broker_vs_Shadow_H1"]["entry_mae"],
            "closest_entry_mae": agg["Broker_vs_Closest_H1_1m"]["entry_mae"],
            "shadow_exit_mae": agg["Broker_vs_Shadow_H1"]["exit_mae"],
            "closest_exit_mae": agg["Broker_vs_Closest_H1_1m"]["exit_mae"],
            "shadow_exit_mismatch_pct": agg["Broker_vs_Shadow_H1"]["exit_mismatch_pct"],
            "closest_exit_mismatch_pct": agg["Broker_vs_Closest_H1_1m"]["exit_mismatch_pct"],
            "h1_vs_1m": {
                "classes": h1m1["classes"],
                "mean_abs_pnl_err_h1": h1m1["mean_abs_pnl_err_h1"],
                "mean_abs_pnl_err_1m": h1m1["mean_abs_pnl_err_1m"],
            },
        },
        "C_accounting": {
            "broker_total_net": matrix["Broker"]["total_pnl"],
            "broker_commission": matrix["Broker"]["commission"],
            "broker_swap": matrix["Broker"]["swap"],
            "mirror_mean_pnl_error_vs_broker": agg["Broker_vs_Mirror"]["mean_pnl_error"],
            "closest_mean_pnl_error_vs_broker": agg["Broker_vs_Closest_H1_1m"]["mean_pnl_error"],
            "closest_mean_abs_pnl_error": agg["Broker_vs_Closest_H1_1m"]["mean_abs_pnl_error"],
            "commission_flags_confidence": "assumed",
        },
        "D_data": {
            "timezone": "broker HTML server local GMT+3 → UTC-3h; books UTC",
            "h1_boundary": ":00 UTC after offset correction",
            "m1_pairs_loaded": closest_rep["m1_pairs"],
            "h1_pairs_loaded": closest_rep["h1_pairs"],
            "spreads": "Raw snapshot from prior backtest (not tick reconstruction)",
            "bid_ask": "unavailable in books; mid/close + spread_pips model in offline",
        },
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "objective": "simulator fidelity reconciliation (not profitability)",
        "knobs": KNOBS,
        "matrix": matrix,
        "aggregate_matches": agg,
        "layers": layers,
        "h1_vs_1m": {
            "classes": h1m1["classes"],
            "mean_abs_pnl_err_h1": h1m1["mean_abs_pnl_err_h1"],
            "mean_abs_pnl_err_1m": h1m1["mean_abs_pnl_err_1m"],
            "n_keys": h1m1["n_keys"],
        },
        "largest_mismatches_closest": largest_mismatches(matches["Broker_vs_Closest_H1_1m"]),
        "largest_mismatches_shadow": largest_mismatches(matches["Broker_vs_Shadow_H1"]),
        "fidelity_verdict": verdict,
        "systems_note": {
            "A": "alexg8/9 24/7 compounding — this harness validates the measuring instrument",
            "B": "alexg7aligned funded — out of scope",
            "C": "Telegram signals — out of scope",
            "rr_wr": "Higher RR generally lowers WR over same period; treat as tradeoff",
        },
    }
    (OUT / "aggregate_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "trade_matches.json").write_text(
        json.dumps(
            {k: {
                "n_matched": v["n_matched"],
                "n_missing": v["n_missing"],
                "n_extra": v["n_extra"],
                "matched": v["matched"],
                "unmatched_ref": v["unmatched_ref"][:50],
                "unmatched_cand": v["unmatched_cand"][:50],
            } for k, v in matches.items()},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    (OUT / "h1_vs_1m_rows.json").write_text(
        json.dumps(h1m1, indent=2, default=str), encoding="utf-8"
    )

    # Human-readable summary
    lines = [
        "8792 RECONCILIATION SUMMARY",
        f"generated: {report['generated_at']}",
        "",
        "KNOBS (replay):",
        json.dumps(KNOBS, indent=2),
        "",
        "MATRIX vs Broker:",
        json.dumps(matrix, indent=2, default=str),
        "",
        "H1 vs 1m agreement classes:",
        json.dumps(h1m1["classes"], indent=2),
        f"mean_abs_pnl_err H1={h1m1['mean_abs_pnl_err_h1']} 1m={h1m1['mean_abs_pnl_err_1m']}",
        "",
        "FIDELITY VERDICT:",
        f"  {verdict['grade']} — {verdict['label']}",
        f"  next: {verdict['next_action']}",
        "",
        "Largest Closest-sim PnL mismatches:",
    ]
    for row in report["largest_mismatches_closest"][:8]:
        lines.append(
            f"  {row['pair']} {row['direction']} kind={row['kind']} "
            f"broker_pnl={row['broker_pnl']} sim_pnl={row['sim_pnl']} "
            f"err={row['pnl_err']:.2f} component={row['component']} "
            f"exit {row['broker_exit_reason']}→{row['sim_exit_reason']}"
        )
    text_path = OUT / "SUMMARY.txt"
    text_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print("wrote", text_path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
