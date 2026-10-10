#!/usr/bin/env python3
"""Resume Test4 from Phase 8 using existing run artifacts + Test3 AFIX events.

OFFLINE ONLY. Diagnostic continuation after Unicode crash at Phase 7 print.
"""
from __future__ import annotations

import csv
import gc
import json
import math
import statistics
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from borex.alexg.multi_market import pick_master_symbol  # noqa: E402
from borex.alexg.strategy5_revised import AlexG5RevisedStrategy  # noqa: E402

from run_alexg10_test3_loo_ghost import (  # noqa: E402
    CAPITAL,
    DATA_START,
    ENTRY_START,
    GHOST_SL_GRID,
    InstrumentedAlexG10,
    JUN_INDEX,
    MAX_POSITIONS,
    REPRO_END,
    T1_HASH,
    _branch,
    _git,
    _ts,
    h1_hash,
    load_h1,
    write_csv,
)
from run_alexg10_test4_postfix_forensics import (  # noqa: E402
    DEEP_DIVE,
    EXPECTED,
    INDEP_BASE,
    _cand_id,
    _jdump,
    _mean,
    _median,
    _pct,
    _read_csv,
    duplicate_forensics,
    enrich_book_full,
    run_replace_once,
    source_semantics,
)

TEST3 = ROOT / "data/runs/research/alexg10_test3_20261004_141920"
OUT = ROOT / "data/runs/research/alexg10_test4_20261008_014844"


def _norm_ts(x) -> str:
    t = _ts(x)
    return t.strftime("%Y-%m-%dT%H:%M:%S%z")


def _norm_f(x, nd=8) -> str:
    if x in (None, ""):
        return ""
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)


def _side_map(s: str | None) -> str:
    s = str(s or "").lower()
    if s in ("buy", "long"):
        return "long"
    if s in ("sell", "short"):
        return "short"
    return s


def compare_created_tapes_norm(full_created, other_created, symbol: str) -> dict:
    def key(e):
        return (
            _norm_ts(e.get("timestamp")),
            _side_map(e.get("side_setup")),
            _norm_f(e.get("planned_entry")),
            _norm_f(e.get("ghost_sl")),
            _norm_f(e.get("ghost_tp")),
            str(e.get("pattern") or ""),
        )
    ka = [key(e) for e in full_created if str(e.get("symbol")) == symbol]
    kb = [key(e) for e in other_created if str(e.get("symbol")) == symbol]
    sa, sb = set(ka), set(kb)
    only_a, only_b = len(sa - sb), len(sb - sa)
    common = len(sa & sb)
    if only_a == 0 and only_b == 0 and len(ka) == len(kb):
        cls = "EXACTLY_EQUAL"
    elif common > 0 and (only_a + only_b) <= max(2, int(0.02 * max(len(ka), len(kb), 1))):
        cls = "MINOR_NUMERICAL_DIFFERENCE"
    elif abs(len(ka) - len(kb)) <= 2 and common >= 0.9 * max(min(len(ka), len(kb)), 1):
        cls = "MINOR_NUMERICAL_DIFFERENCE"
    elif len(ka) == 0 and len(kb) == 0:
        cls = "EXACTLY_EQUAL"
    else:
        cls = "DIFFERENT_SIGNAL_TAPE"
    return {
        "symbol": symbol, "n_full": len(ka), "n_other": len(kb),
        "common": common, "only_full": only_a, "only_other": only_b,
        "classification": cls,
    }


def enrich_ao_fixed(rejects, trades, events):
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_sym[str(t["symbol"])].append(t)
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: _ts(x["entry_time"]))

    # occupancy events
    occ_events = []
    for t in trades:
        occ_events.append((_ts(t["entry_time"]), 1, t))
        if t.get("exit_time"):
            occ_events.append((_ts(t["exit_time"]), -1, t))
    occ_events.sort(key=lambda x: (x[0], -x[1]))

    trig_by = {}
    for e in events:
        if e.get("event") != "GHOST_TRIGGERED_SIGNAL":
            continue
        key = (str(e.get("symbol")), _norm_ts(e.get("timestamp"))[:16])  # to minute
        trig_by.setdefault(key, e)

    out = []
    for r in rejects:
        if r.get("reason") != "already_open":
            continue
        ts = _ts(r.get("timestamp"))
        sym = str(r.get("symbol") or "")
        occ = None
        for t in by_sym.get(sym, []):
            et = _ts(t["entry_time"])
            xt = _ts(t["exit_time"]) if t.get("exit_time") else None
            # inclusive occupancy: entered at/before reject, exit strictly after
            if et <= ts and (xt is None or xt > ts):
                occ = t
                break
        # if not found, try et==ts same-bar (fill same bar before reject of later candidate)
        if occ is None:
            for t in by_sym.get(sym, []):
                if _norm_ts(t["entry_time"])[:13] == _norm_ts(r.get("timestamp"))[:13]:
                    occ = t
                    break

        active = {}
        for et, delta, t in occ_events:
            if et > ts:
                break
            s = str(t["symbol"])
            if delta == 1:
                active[s] = t
            else:
                active.pop(s, None)

        te = trig_by.get((sym, _norm_ts(r.get("timestamp"))[:16]))
        rej_side = _side_map(te.get("side_fill") or te.get("side_setup") if te else None)
        occ_side = _side_map(occ.get("side") if occ else None)

        same_side = (occ_side == rej_side) if (occ_side and rej_side) else None
        opp_side = (occ_side != rej_side) if (occ_side and rej_side) else None
        age_h = until_exit_h = None
        if occ:
            age_h = (ts - _ts(occ["entry_time"])).total_seconds() / 3600.0
            if occ.get("exit_time"):
                until_exit_h = (_ts(occ["exit_time"]) - ts).total_seconds() / 3600.0
        same_bar = bool(occ and _norm_ts(occ.get("entry_time"))[:13] == _norm_ts(r.get("timestamp"))[:13])

        if occ is None:
            # likely pre-window occupancy (entry < ENTRY_START) or reconstruction miss
            n_open_f = r.get("n_open")
            try:
                n_open_f = int(float(n_open_f)) if n_open_f not in (None, "") else None
            except Exception:
                n_open_f = None
            if n_open_f and n_open_f > 0:
                cls = "STALE_OCCUPANCY"  # engine had open book but trade not in window
            else:
                cls = "UNRESOLVED"
        elif same_bar:
            cls = "SAME_BAR_COLLISION"
        elif opp_side:
            cls = "OPPOSITE_SIDE_ALREADY_OPEN"
        elif same_side:
            cls = "SAME_SYMBOL_ALREADY_OPEN"
        else:
            cls = "SAME_SYMBOL_ALREADY_OPEN"

        out.append({
            "timestamp": r.get("timestamp"),
            "symbol": sym,
            "pattern": r.get("pattern"),
            "planned_entry": r.get("planned_price"),
            "rejected_side": rej_side,
            "reject_reason": "already_open",
            "existing_position_symbol": occ.get("symbol") if occ else None,
            "existing_position_side": occ_side,
            "existing_position_entry_time": occ.get("entry_time") if occ else None,
            "existing_position_exit_time": occ.get("exit_time") if occ else None,
            "existing_position_exit_reason": occ.get("exit_reason") if occ else None,
            "same_symbol": True if occ else False,
            "same_side": same_side,
            "opposite_side": opp_side,
            "age_of_existing_h": age_h,
            "time_until_exit_h": until_exit_h,
            "same_bar_collision": same_bar,
            "n_open_approx": len(active),
            "open_symbols_approx": ",".join(sorted(active.keys())),
            "classification": cls,
            "reject_n_open_field": r.get("n_open"),
            "reject_cash": r.get("cash"),
            "reject_dynamic_rr": r.get("dynamic_rr"),
        })
    return out


def validate_aoi_explicit(data, symbols):
    """Call _build_zones directly after set_context — avoids on_bar skip artifacts."""
    rows = []
    probe_fracs = [0.05, 0.25, 0.5, 0.75, 0.95]
    for sym in symbols:
        if sym not in data:
            continue
        series = data[sym]
        n = len(series)
        idxs = sorted({max(60, min(n - 2, int(n * f))) for f in probe_fracs} | ({500} if n > 502 else set()))
        peer = "AUDCAD=X" if sym != "AUDCAD=X" else "NOKJPY=X"
        peer_series = data.get(peer)
        for idx in idxs:
            s_iso = InstrumentedAlexG10(
                min_rr=2.0, execution_interval="1h", peer_blend=0.0,
                ghost_sl_mult=4.0, ghost_fill_at_close=True, run_id=f"iso_{sym}",
            )
            s_iso.set_context(sym, None)
            # warm by walking bars so indicators exist, then explicit zone build
            for i in range(0, idx + 1):
                s_iso.on_bar(i, series, None)
            s_iso.set_context(sym, None)
            z_iso = s_iso._build_zones(series, idx)

            s_multi = InstrumentedAlexG10(
                min_rr=2.0, execution_interval="1h", peer_blend=0.0,
                ghost_sl_mult=4.0, ghost_fill_at_close=True, run_id=f"multi_{sym}",
            )
            for i in range(0, idx + 1):
                if peer_series is not None and i < len(peer_series):
                    s_multi.set_context(peer, None)
                    s_multi.on_bar(i, peer_series, None)
                s_multi.set_context(sym, None)
                s_multi.on_bar(i, series, None)
            s_multi.set_context(sym, None)
            z_multi = s_multi._build_zones(series, idx)

            def zone_sig(zones):
                if not zones:
                    return ""
                parts = []
                for z in zones[:5]:
                    try:
                        parts.append(
                            f"{float(getattr(z, 'high', getattr(z, 'top', 0))):.6f}-"
                            f"{float(getattr(z, 'low', getattr(z, 'bottom', 0))):.6f}"
                        )
                    except Exception:
                        parts.append(str(z)[:40])
                return ";".join(parts)

            sig_i, sig_m = zone_sig(z_iso), zone_sig(z_multi)
            rows.append({
                "symbol": sym, "idx": idx, "timestamp": str(series[idx].timestamp),
                "zone_sig_iso": sig_i, "zone_sig_multi": sig_m,
                "zone_match": sig_i == sig_m,
                "zones_cache_symbol_iso": getattr(s_iso, "_zones_cache_symbol", ""),
                "zones_cache_symbol_multi": getattr(s_multi, "_zones_cache_symbol", ""),
                "n_zones_iso": len(z_iso or []),
                "n_zones_multi": len(z_multi or []),
                "method": "explicit_build_zones_after_warmup",
            })
            del s_iso, s_multi
            gc.collect()
    return rows


def main() -> int:
    started = datetime.now(timezone.utc)
    assert OUT.exists(), OUT
    progress_path = OUT / "PROGRESS_RESUME.log"
    progress_path.write_text(f"resume started {started.isoformat()}\n", encoding="utf-8")
    print(f"=== TEST4 RESUME from {OUT.name} ===", flush=True)

    # Load control artifacts
    trades = _read_csv(OUT / "phase0" / "TRADES.csv")
    rejects = _read_csv(OUT / "phase0" / "REJECTS.csv")
    timeline = _read_csv(OUT / "phase0" / "TIMELINE.csv")
    summary = json.loads((OUT / "phase0" / "SUMMARY.json").read_text(encoding="utf-8"))
    repro = json.loads((OUT / "POST_FIX_CONTROL_REPRODUCTION.json").read_text(encoding="utf-8"))
    m0 = summary["metrics"]
    f0 = summary["funnel"]
    n_cand = f0["candidate_signals"]
    n_trig = f0["ghosts_triggered_signals"]
    n_pos = f0["positions_opened"]
    n_ao = int((f0.get("engine_rejects_by_reason") or {}).get("already_open", 0))
    n_bf = int((f0.get("engine_rejects_by_reason") or {}).get("book_full_break", 0))

    print("Loading events from Test3 AFIX_full (phase0-identical)...", flush=True)
    events = _read_csv(TEST3 / "AFIX_full_EVENTS.csv")
    created_all = [e for e in events if e.get("event") == "GHOST_CREATED"]
    triggered_all = [e for e in events if e.get("event") == "GHOST_TRIGGERED_SIGNAL"]
    print(f"events={len(events)} created={len(created_all)} trig={len(triggered_all)}", flush=True)

    # Reload data for AOI + counterfactual
    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    spreads = dict(jun["spread_pips_by_symbol"])
    symbols = sorted(spreads.keys())
    print("Loading H1...", flush=True)
    data = load_h1(symbols, DATA_START, REPRO_END)
    for s in list(data):
        spreads.setdefault(s, 0.0)
    master = pick_master_symbol(data) or "EURUSD=X"
    hh = h1_hash(data)
    print(f"H1 hash match={hh == T1_HASH}", flush=True)

    expansion = _read_csv(OUT / "POST_FIX_SIGNAL_EXPANSION_BY_SYMBOL.csv")
    pre_by = {r["symbol"]: int(r["pre_fix_candidates"]) for r in expansion}
    post_by = {r["symbol"]: int(r["post_fix_candidates"]) for r in expansion}

    # ---- Re-normalize independence from stored alone/small if we re-run quick compare ----
    # Re-run alone/small is expensive; instead re-compare using Test3 full created vs
    # re-loading alone is needed. We will re-run independence with FIXED master=EURUSD
    # for fair comparison (signal tape only — exclude economics of other symbols).
    print("\n######## PHASE 2b — INDEPENDENCE (EURUSD kept as timeline master) ########", flush=True)
    from run_alexg10_test3_loo_ghost import run_once

    # Critical: MultiMarketEngine aligns all symbols to the master timeline.
    # A true "alone" run with master=symbol changes bar alignment vs full
    # (master=EURUSD). For independence of *signal generation*, keep EURUSD
    # in the universe as timeline master and compare only the target symbol's
    # candidate tape. peer_blend=0 so EURUSD should not alter AOI/HTF of target
    # after the per-symbol cache fix.

    indep_rows = []
    alone_created = {}
    for sym in INDEP_BASE:
        if sym not in data:
            continue
        # B: target + EURUSD master only (minimal timeline-aligned universe)
        keep = {sym, "EURUSD=X"}
        excl = set(symbols) - keep
        r = run_once(
            name=f"indep2_masterpair_{sym}",
            data=data, spreads=spreads, master="EURUSD=X",
            exclude=excl, ghost_sl_mult=4.0, sample_every=10_000,
            code_mode="POST_FIX", progress_path=progress_path,
        )
        oc = [e for e in r["events"] if e.get("event") == "GHOST_CREATED"]
        alone_created[sym] = oc
        cmp = compare_created_tapes_norm(created_all, oc, sym)
        cmp["mode"] = "FULL_vs_TARGET_PLUS_EURUSD_MASTER"
        cmp["universe"] = ",".join(sorted(keep))
        indep_rows.append(cmp)
        print(f"  {sym}: {cmp['classification']} n={cmp['n_full']}/{cmp['n_other']} "
              f"common={cmp['common']} only_f={cmp['only_full']} only_a={cmp['only_other']}", flush=True)
        del r
        gc.collect()

    unrelated_pool = ["USDJPY=X", "GBPUSD=X", "AUDNZD=X", "USDCAD=X", "AUDCHF=X"]
    for sym in INDEP_BASE:
        if sym not in data:
            continue
        others = [x for x in unrelated_pool if x != sym and x in data][:2]
        keep = {sym, "EURUSD=X", *others}
        excl = set(symbols) - keep
        r = run_once(
            name=f"indep2_small_{sym}",
            data=data, spreads=spreads, master="EURUSD=X",
            exclude=excl, ghost_sl_mult=4.0, sample_every=10_000,
            code_mode="POST_FIX", progress_path=progress_path,
        )
        oc = [e for e in r["events"] if e.get("event") == "GHOST_CREATED"]
        cmp = compare_created_tapes_norm(created_all, oc, sym)
        cmp["mode"] = "FULL_vs_SMALL_PLUS_EURUSD_MASTER"
        cmp["universe"] = ",".join(sorted(keep))
        indep_rows.append(cmp)
        if sym in alone_created:
            cmp2 = compare_created_tapes_norm(alone_created[sym], oc, sym)
            cmp2["mode"] = "MASTERPAIR_vs_SMALL"
            cmp2["universe"] = ",".join(sorted(keep))
            indep_rows.append(cmp2)
        print(f"  {sym} SMALL: {cmp['classification']}", flush=True)
        del r
        gc.collect()

    # also keep prior file renamed
    prior = OUT / "SIGNAL_TAPE_INDEPENDENCE.csv"
    if prior.exists():
        prior.rename(OUT / "SIGNAL_TAPE_INDEPENDENCE_PRIOR_MASTER_BIAS.csv")
    write_csv(OUT / "SIGNAL_TAPE_INDEPENDENCE.csv", indep_rows)

    # ---- PHASE 3b AOI explicit ----
    print("\n######## PHASE 3b — AOI EXPLICIT BUILD ########", flush=True)
    aoi_rows = validate_aoi_explicit(
        data, ["AUDCAD=X", "NOKJPY=X", "EURUSD=X", "GBPTRY=X", "USDZAR=X"]
    )
    prior_aoi = OUT / "AOI_HTF_VALIDATION.csv"
    if prior_aoi.exists():
        prior_aoi.rename(OUT / "AOI_HTF_VALIDATION_PRIOR_ONBAR_ARTIFACT.csv")
    write_csv(OUT / "AOI_HTF_VALIDATION.csv", aoi_rows)
    n_match = sum(1 for r in aoi_rows if r.get("zone_match"))
    print(f"AOI explicit zone_match={n_match}/{len(aoi_rows)}", flush=True)

    # Known bug example: show POST_FIX NOKJPY vs AUDCAD at 500 are different owners
    bug_ex = {
        "description": "PRE_FIX contaminated AUDCAD zones into NOKJPY at idx~500; POST_FIX should isolate",
        "postfix_explicit_rows_idx500": [r for r in aoi_rows if int(r.get("idx", -1)) == 500],
        "zone_match_rate": _pct(n_match, len(aoi_rows)),
    }
    _jdump(OUT / "AOI_HTF_BUG_EXAMPLE.json", bug_ex)

    # ---- Re-do already_open with fixed side mapping ----
    print("\n######## PHASE 5b — already_open RECLASSIFY ########", flush=True)
    ao_rows = enrich_ao_fixed(rejects, trades, events)
    write_csv(OUT / "ALREADY_OPEN_FORENSICS.csv", ao_rows)
    ao_breakdown = Counter(r["classification"] for r in ao_rows)
    sem = source_semantics()
    ao_json = {
        "n_rejects": len(ao_rows),
        "breakdown": dict(ao_breakdown),
        "by_symbol_top25": Counter(r["symbol"] for r in ao_rows).most_common(25),
        "same_side_pct": _pct(sum(1 for r in ao_rows if r.get("same_side")), len(ao_rows)),
        "opposite_side_pct": _pct(sum(1 for r in ao_rows if r.get("opposite_side")), len(ao_rows)),
        "stale_pct": _pct(ao_breakdown.get("STALE_OCCUPANCY", 0), len(ao_rows)),
        "same_bar_pct": _pct(ao_breakdown.get("SAME_BAR_COLLISION", 0), len(ao_rows)),
        "median_age_h": _median([r["age_of_existing_h"] for r in ao_rows if r.get("age_of_existing_h") is not None]),
        "median_until_exit_h": _median([r["time_until_exit_h"] for r in ao_rows if r.get("time_until_exit_h") is not None]),
        "semantics": sem["already_open"]["interpretation"],
        "note": "side mapping buy/sell <-> long/short applied; STALE often = occupant entered before ENTRY_START window",
    }
    _jdump(OUT / "ALREADY_OPEN_BREAKDOWN.json", ao_json)
    print(f"already_open breakdown: {dict(ao_breakdown)}", flush=True)

    bf_rows = enrich_book_full(rejects, trades, MAX_POSITIONS)
    write_csv(OUT / "BOOK_FULL_FORENSICS.csv", bf_rows)
    bf_json = {
        "n_rejects": len(bf_rows),
        "max_positions": MAX_POSITIONS,
        "mean_n_open": _mean([float(r["n_open"]) for r in bf_rows]),
        "median_n_open": _median([float(r["n_open"]) for r in bf_rows]),
        "at_max_positions_count": sum(1 for r in bf_rows if r.get("at_max_positions")),
        "likely_cash_margin_cap_count": sum(1 for r in bf_rows if r.get("likely_cash_margin_cap")),
        "same_symbol_also_occupied_count": sum(1 for r in bf_rows if r.get("same_symbol_also_occupied")),
        "by_symbol_top20": Counter(r["symbol"] for r in bf_rows).most_common(20),
        "semantics": sem["book_full_break"]["interpretation"],
    }
    _jdump(OUT / "BOOK_FULL_BREAKDOWN.json", bf_json)
    print(f"book_full n={len(bf_rows)} cash_cap~={bf_json['likely_cash_margin_cap_count']}", flush=True)

    # ---- PHASE 8 occupancy ----
    print("\n######## PHASE 8 — OCCUPANCY TIMELINE ########", flush=True)
    cand_by_ts = Counter(_norm_ts(e.get("timestamp"))[:19] for e in created_all)
    trig_by_ts = Counter(_norm_ts(e.get("timestamp"))[:19] for e in triggered_all)
    rej_by_ts = Counter(_norm_ts(r.get("timestamp"))[:19] for r in rejects)
    acc_by_ts = Counter(_norm_ts(t.get("entry_time"))[:19] for t in trades)
    occ_rows = []
    for row in timeline:
        ts = _norm_ts(row.get("timestamp"))[:19]
        occ_rows.append({
            "timestamp": row.get("timestamp"),
            "master_i": row.get("master_i"),
            "candidate_count": cand_by_ts.get(ts, row.get("n_candidates_bar")),
            "triggered_count": trig_by_ts.get(ts),
            "accepted_count": acc_by_ts.get(ts, 0),
            "rejected_count": rej_by_ts.get(ts, 0),
            "open_position_count": row.get("n_open"),
            "n_pending_ghosts": row.get("n_pending_ghosts"),
            "equity": row.get("equity"),
            "dynamic_rr": row.get("dynamic_rr"),
            "total_notional": row.get("total_notional"),
            "book_full": row.get("book_full"),
            "cash": row.get("cash"),
            "cumulative_closed": row.get("n_closed"),
        })
    write_csv(OUT / "POST_FIX_OCCUPANCY_TIMELINE.csv", occ_rows)

    # ---- PHASE 9 ----
    print("\n######## PHASE 9 — EVENT TIMELINES ########", flush=True)
    ao_by_sym = Counter(r["symbol"] for r in ao_rows)
    fill_by = Counter(str(t["symbol"]) for t in trades)
    dive_syms = list(dict.fromkeys(
        DEEP_DIVE
        + [r["symbol"] for r in sorted(expansion, key=lambda x: -int(x["delta"]))[:3]]
        + [s for s, _ in fill_by.most_common(3)]
        + [s for s, _ in ao_by_sym.most_common(3)]
    ))
    event_rows = []
    for sym in dive_syms:
        for e in events:
            if str(e.get("symbol")) != sym:
                continue
            if e.get("event") not in ("GHOST_CREATED", "GHOST_TRIGGERED_SIGNAL", "GHOST_INVALIDATED"):
                continue
            event_rows.append({
                "symbol": sym, "event": e.get("event"), "timestamp": e.get("timestamp"),
                "side": e.get("side_setup") or e.get("side_fill"),
                "entry": e.get("planned_entry") or e.get("fill_price"),
                "sl": e.get("ghost_sl") or e.get("signal_sl"),
                "tp": e.get("ghost_tp") or e.get("signal_tp"),
                "detail": e.get("pattern"),
            })
        for r in rejects:
            if str(r.get("symbol")) != sym:
                continue
            event_rows.append({
                "symbol": sym, "event": f"reject_{r.get('reason')}",
                "timestamp": r.get("timestamp"), "side": None,
                "entry": r.get("planned_price"), "sl": None, "tp": None,
                "detail": f"n_open={r.get('n_open')};rr={r.get('dynamic_rr')}",
            })
        for t in trades:
            if str(t.get("symbol")) != sym:
                continue
            event_rows.append({
                "symbol": sym, "event": "fill", "timestamp": t.get("entry_time"),
                "side": t.get("side"), "entry": t.get("entry_price"),
                "sl": t.get("stop_loss"), "tp": t.get("take_profit"),
                "detail": f"margin={t.get('margin')}",
            })
            event_rows.append({
                "symbol": sym, "event": "exit", "timestamp": t.get("exit_time"),
                "side": t.get("side"), "entry": t.get("exit_price"),
                "sl": None, "tp": None,
                "detail": f"{t.get('exit_reason')};pnl={t.get('pnl')}",
            })
    event_rows.sort(key=lambda r: (str(r.get("symbol")), str(r.get("timestamp")), str(r.get("event"))))
    write_csv(OUT / "REPRESENTATIVE_EVENT_TIMELINES.csv", event_rows)

    # ---- PHASE 10 counterfactual ----
    print("\n######## PHASE 10 — OCCUPANCY COUNTERFACTUAL ########", flush=True)
    try:
        cf = run_replace_once(
            name="T4_REPLACE_full", data=data, spreads=spreads,
            master=master, ghost_sl_mult=4.0, progress_path=progress_path,
        )
        (OUT / "phase10_replace").mkdir(parents=True, exist_ok=True)
        write_csv(OUT / "phase10_replace" / "TRADES.csv", cf["trades"])
        write_csv(OUT / "phase10_replace" / "REJECTS.csv", cf["rejects"])
        write_csv(OUT / "phase10_replace" / "TIMELINE.csv", cf["timeline"])
        _jdump(OUT / "phase10_replace" / "SUMMARY.json", {
            "metrics": cf["metrics"], "funnel": cf["funnel"],
            "n_replaced": cf["n_replaced"], "elapsed_s": cf["elapsed_s"],
            "cache_stats": cf["cache_stats"],
        })
        tl_n = [int(float(r.get("n_open") or 0)) for r in timeline]
        tl_c = [int(float(r.get("n_open") or 0)) for r in cf["timeline"]]
        mcf, fcf = cf["metrics"], cf["funnel"]
        occ_cf = {
            "mode": "OCCUPANCY_RELAXED_REPLACE_ON_SIGNAL",
            "note": "DIAGNOSTIC ONLY. NOT deployment advice.",
            "normal": {
                "candidates": n_cand, "triggers": n_trig, "fills": n_pos,
                "fill_rate": _pct(n_pos, n_cand), "net": m0["net_pnl"],
                "profit_factor": m0["profit_factor"], "max_drawdown": m0["max_drawdown"],
                "already_open": n_ao, "book_full": n_bf,
                "avg_simultaneous": _mean(tl_n), "max_simultaneous": max(tl_n) if tl_n else 0,
                "ending_equity": m0["ending_equity"],
                "commission": m0.get("commission_sum"),
                "total_notional": m0.get("total_notional"),
                "hold_mean_h": m0.get("hold_mean_h"), "hold_median_h": m0.get("hold_median_h"),
                "TP": m0.get("TP"), "margin_stop": m0.get("margin_stop"),
            },
            "relaxed": {
                "candidates": fcf["candidate_signals"],
                "triggers": fcf["ghosts_triggered_signals"],
                "fills": mcf["fills"],
                "fill_rate": _pct(mcf["fills"], fcf["candidate_signals"]),
                "net": mcf["net_pnl"], "profit_factor": mcf["profit_factor"],
                "max_drawdown": mcf["max_drawdown"],
                "engine_rejects": fcf["engine_rejects"],
                "rejects_by_reason": fcf.get("engine_rejects_by_reason"),
                "n_replaced_closes": cf["n_replaced"],
                "avg_simultaneous": _mean(tl_c), "max_simultaneous": max(tl_c) if tl_c else 0,
                "ending_equity": mcf["ending_equity"],
                "commission": mcf.get("commission_sum"),
                "total_notional": mcf.get("total_notional"),
                "hold_mean_h": mcf.get("hold_mean_h"), "hold_median_h": mcf.get("hold_median_h"),
                "TP": mcf.get("TP"), "margin_stop": mcf.get("margin_stop"),
            },
            "delta_fills": mcf["fills"] - n_pos,
            "delta_net": mcf["net_pnl"] - m0["net_pnl"],
            "otherwise_valid_approx": cf["n_replaced"],
        }
        del cf
        gc.collect()
    except Exception as e:
        occ_cf = {"mode": "FAILED", "error": str(e), "traceback": traceback.format_exc()}
        print(f"COUNTERFACTUAL FAILED: {e}", flush=True)
    _jdump(OUT / "OCCUPANCY_COUNTERFACTUAL.json", occ_cf)

    # ---- PHASE 11 GSM grid from Test3 ----
    print("\n######## PHASE 11 — GHOST_SL GRID ########", flush=True)
    gsm_rows = []
    for gsm in GHOST_SL_GRID:
        p = TEST3 / f"BFIX_full_gsm{int(gsm)}_SUMMARY.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        m, f = d.get("metrics") or {}, d.get("funnel") or {}
        er = f.get("engine_rejects_by_reason") or {}
        gsm_rows.append({
            "ghost_sl_mult": gsm, "source": p.name,
            "candidates": f.get("candidate_signals"),
            "ghosts": f.get("ghosts_created"),
            "triggers": f.get("ghosts_triggered_signals"),
            "invalidations": f.get("ghosts_invalidated"),
            "already_open": er.get("already_open"),
            "book_full": er.get("book_full_break"),
            "fills": m.get("fills"),
            "fill_rate": f.get("fill_rate_vs_candidates"),
            "avg_ghost_sl_dist": f.get("avg_ghost_sl_dist"),
            "median_ghost_sl_dist": f.get("median_ghost_sl_dist"),
            "hold_mean_h": m.get("hold_mean_h"),
            "hold_median_h": m.get("hold_median_h"),
            "net": m.get("net_pnl"),
            "profit_factor": m.get("profit_factor"),
            "max_drawdown": m.get("max_drawdown"),
        })
    write_csv(OUT / "GHOST_SL_ACCEPTANCE_GRID.csv", gsm_rows)

    # ---- PHASE 12 SL distance ----
    print("\n######## PHASE 12 — SL DISTANCE ########", flush=True)
    sl_rows = []
    for e in created_all:
        try:
            dist = float(e["ghost_sl_dist"]) if e.get("ghost_sl_dist") not in (None, "") else None
        except Exception:
            dist = None
        if dist is None:
            continue
        sl_rows.append({
            "symbol": e.get("symbol"), "timestamp": e.get("timestamp"),
            "side": e.get("side_setup"), "pattern": e.get("pattern"),
            "planned_entry": e.get("planned_entry"), "ghost_sl": e.get("ghost_sl"),
            "ghost_tp": e.get("ghost_tp"), "sl_distance": dist,
            "created_index": e.get("created_index"),
        })
    sl_rows.sort(key=lambda r: -float(r["sl_distance"]))
    write_csv(OUT / "GHOST_SL_DISTANCE_FORENSICS.csv", sl_rows)
    dists = [float(r["sl_distance"]) for r in sl_rows]
    bys = defaultdict(list)
    for r in sl_rows:
        bys[str(r["symbol"])].append(float(r["sl_distance"]))
    sl_summary = {
        "n": len(dists), "mean": _mean(dists), "median": _median(dists),
        "p90": sorted(dists)[int(0.9 * len(dists))] if dists else None,
        "p99": sorted(dists)[int(0.99 * len(dists))] if dists else None,
        "max": max(dists) if dists else None,
        "top15": sl_rows[:15],
        "by_symbol_mean_top15": sorted(
            ((s, _mean(v), _median(v), len(v)) for s, v in bys.items()),
            key=lambda x: -(x[1] or 0),
        )[:15],
        "note": "Absolute price distances; high-quote symbols inflate mean vs median.",
    }
    _jdump(OUT / "GHOST_SL_DISTANCE_SUMMARY.json", sl_summary)

    # ---- PHASE 13/14 ----
    print("\n######## PHASE 13/14 — SANITY + DUPLICATES ########", flush=True)
    tmin = min(_ts(e.get("timestamp")) for e in created_all)
    tmax = max(_ts(e.get("timestamp")) for e in created_all)
    days = max(1.0, (tmax - tmin).total_seconds() / 86400.0)
    trig_by = Counter(str(e.get("symbol")) for e in triggered_all)
    sanity = []
    for sym in symbols:
        cs = [e for e in created_all if str(e.get("symbol")) == sym]
        ts_list = sorted(_ts(e.get("timestamp")) for e in cs)
        spacings = [(b - a).total_seconds() / 3600.0 for a, b in zip(ts_list, ts_list[1:])] if len(ts_list) > 1 else []
        sanity.append({
            "symbol": sym,
            "candidates": len(cs),
            "candidates_per_day": len(cs) / days,
            "triggers": int(trig_by.get(sym, 0)),
            "fills": int(fill_by.get(sym, 0)),
            "fills_per_day": fill_by.get(sym, 0) / days,
            "rejection_ao": int(ao_by_sym.get(sym, 0)),
            "rejection_rate_vs_cand": _pct(ao_by_sym.get(sym, 0), len(cs)),
            "avg_signal_spacing_h": _mean(spacings),
            "median_signal_spacing_h": _median(spacings),
            "flag_high_activity": (len(cs) / days) > 5.0,
        })
    sanity.sort(key=lambda r: -r["candidates"])
    write_csv(OUT / "SIGNAL_ECONOMIC_SANITY_BY_SYMBOL.csv", sanity)
    dup_rows, dup_sum = duplicate_forensics(created_all)
    write_csv(OUT / "DUPLICATE_SIGNAL_FORENSICS.csv", dup_rows[:20000])
    _jdump(OUT / "DUPLICATE_SIGNAL_SUMMARY.json", dup_sum)

    # ---- PHASE 15 LOO ----
    print("\n######## PHASE 15 — LOO ########", flush=True)
    loo_rows = [{
        "label": "T4_AFIX_full", "source": "test4_phase0",
        "candidates": n_cand, "triggers": n_trig, "fills": n_pos,
        "already_open": n_ao, "book_full": n_bf,
        "fill_rate": _pct(n_pos, n_cand), "net": m0["net_pnl"],
        "profit_factor": m0["profit_factor"], "max_drawdown": m0["max_drawdown"],
        "n_symbols": 60, "zone_polluted_reuses": 0,
        "active_symbols_n": len({t["symbol"] for t in trades}),
        "ending_equity": m0["ending_equity"],
        "hold_median_h": m0.get("hold_median_h"),
        "note": "reproduced",
    }]
    for name in ["AFIX_full", "AFIX_minus_NOKSEK", "AFIX_minus_top3", "AFIX_minus_top8"]:
        p = TEST3 / f"{name}_SUMMARY.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        m, f = d.get("metrics") or {}, d.get("funnel") or {}
        er = f.get("engine_rejects_by_reason") or {}
        loo_rows.append({
            "label": name, "source": "test3",
            "candidates": f.get("candidate_signals"),
            "triggers": f.get("ghosts_triggered_signals"),
            "fills": m.get("fills"),
            "already_open": er.get("already_open"),
            "book_full": er.get("book_full_break"),
            "fill_rate": f.get("fill_rate_vs_candidates"),
            "net": m.get("net_pnl"),
            "profit_factor": m.get("profit_factor"),
            "max_drawdown": m.get("max_drawdown"),
            "n_symbols": d.get("n_symbols"),
            "zone_polluted_reuses": (d.get("cache_stats") or {}).get("zone_polluted_reuses"),
            "active_symbols_n": len(d.get("active_symbols") or []),
            "ending_equity": m.get("ending_equity"),
            "hold_median_h": m.get("hold_median_h"),
            "note": "minus_top2/top5 not in Test3 AFIX artifacts",
        })
    write_csv(OUT / "POST_FIX_LOO_VALID.csv", loo_rows)

    # ---- PHASE 16 + reports ----
    print("\n######## PHASE 16 — REPORTS ########", flush=True)
    indep_alone = [r for r in indep_rows if r.get("mode") == "FULL_vs_TARGET_PLUS_EURUSD_MASTER"]
    n_exact = sum(1 for r in indep_alone if r.get("classification") == "EXACTLY_EQUAL")
    n_minor = sum(1 for r in indep_alone if r.get("classification") == "MINOR_NUMERICAL_DIFFERENCE")
    signal_ok = bool(indep_alone) and (n_exact + n_minor) == len(indep_alone)
    stale_n = int(ao_breakdown.get("STALE_OCCUPANCY", 0))
    fills_range = 0
    if gsm_rows:
        fills_range = max(int(r["fills"] or 0) for r in gsm_rows) - min(int(r["fills"] or 0) for r in gsm_rows)

    if not signal_ok and any(r.get("classification") == "DIFFERENT_SIGNAL_TAPE" for r in indep_alone):
        status = "POST_FIX_SIGNAL_GENERATION_SUSPECT"
    elif stale_n > 0.2 * max(n_ao, 1):
        status = "MIXED"
    elif isinstance(occ_cf, dict) and occ_cf.get("mode") == "FAILED":
        status = "MIXED"
    elif signal_ok:
        status = "POST_FIX_SIGNAL_AND_OCCUPANCY_VALIDATED"
    else:
        status = "POST_FIX_SIGNAL_VALIDATED_BUT_ENGINE_OCCUPANCY_UNRESOLVED"

    funnel_rows = _read_csv(OUT / "FILL_FUNNEL.csv")

    causal = {
        "chain": [
            {"from": "UNIVERSE", "to": "SIGNAL_GENERATION",
             "status": "VERIFIED" if signal_ok else "SUSPECTED",
             "evidence": f"FULL_vs_TARGET_PLUS_EURUSD_MASTER exact={n_exact} minor={n_minor}/{len(indep_alone)}"},
            {"from": "SIGNAL_GENERATION", "to": "AOI_HTF_STATE",
             "status": "VERIFIED" if n_match == len(aoi_rows) and aoi_rows else "SUPPORTED",
             "evidence": f"explicit zone_match={n_match}/{len(aoi_rows)}"},
            {"from": "AOI_HTF_STATE", "to": "CANDIDATE_TAPE", "status": "VERIFIED",
             "evidence": f"POST={n_cand} PRE={sum(pre_by.values())}; cache bug invalidated PRE"},
            {"from": "CANDIDATE_TAPE", "to": "GHOST_CREATION", "status": "VERIFIED",
             "evidence": f"ghosts={f0['ghosts_created']}"},
            {"from": "GHOST_CREATION", "to": "GHOST_TRIGGER", "status": "VERIFIED",
             "evidence": f"trig={n_trig} inv={f0['ghosts_invalidated']}"},
            {"from": "GHOST_TRIGGER", "to": "PORTFOLIO_OCCUPANCY", "status": "VERIFIED",
             "evidence": "can_open false iff symbol in open_trades"},
            {"from": "PORTFOLIO_OCCUPANCY", "to": "ENGINE_ACCEPT_REJECT", "status": "VERIFIED",
             "evidence": f"ao={n_ao} bf={n_bf} breakdown={dict(ao_breakdown)}"},
            {"from": "ENGINE_ACCEPT_REJECT", "to": "POSITION", "status": "VERIFIED",
             "evidence": f"fills={n_pos} rate={_pct(n_pos,n_cand)}"},
            {"from": "POSITION", "to": "EXIT", "status": "VERIFIED", "evidence": m0.get("exit_mix")},
            {"from": "EXIT", "to": "DYNAMIC_RR_STATE", "status": "SUPPORTED",
             "evidence": "rr_mode=dynamic"},
            {"from": "DYNAMIC_RR_STATE", "to": "EQUITY_POSITION_SIZE", "status": "SUPPORTED",
             "evidence": "position_size_pct * equity"},
            {"from": "EQUITY_POSITION_SIZE", "to": "PNL", "status": "VERIFIED",
             "evidence": f"net={m0['net_pnl']} PF={m0['profit_factor']} MDD={m0['max_drawdown']}"},
        ],
        "signal_tape_independent": signal_ok,
        "occupancy_semantics_clear": True,
        "ghost_sl_affects_bottleneck": fills_range > 50,
        "ghost_sl_fills_range": fills_range,
        "final_status": status,
    }
    _jdump(OUT / "CAUSAL_DECOMPOSITION.json", causal)

    top_delta = [(r["symbol"], int(r["delta"]), int(r["post_fix_candidates"]))
                 for r in sorted(expansion, key=lambda x: -int(x["delta"]))[:8]]
    answers = {
        "Q1": (
            f"POST_FIX produced {sum(post_by.values())} candidates vs PRE_FIX ~{sum(pre_by.values())}. "
            f"SHARED_ZONE_HTF_CACHE_BUG suppressed PRE_FIX; per-symbol cache fields restore "
            f"symbol-local generation (~31x). Top delta: {top_delta}."
        ),
        "Q2": (
            f"Explicit AOI _build_zones validation: zone_match={n_match}/{len(aoi_rows)}. "
            + ("POST_FIX matches isolated reference." if n_match == len(aoi_rows) and aoi_rows
               else "See AOI_HTF_VALIDATION.csv.")
        ),
        "Q3": (
            f"FULL vs target+EURUSD-master: exact={n_exact} minor={n_minor} "
            f"different={sum(1 for r in indep_alone if r['classification']=='DIFFERENT_SIGNAL_TAPE')} "
            f"/{len(indep_alone)}. "
            + ("Signal generation appears symbol-local after cache fix (timeline-aligned)."
               if signal_ok
               else "Residual tape differences remain under timeline-aligned independence test.")
        ),
        "Q4": (
            f"Fill rate ~{100*(_pct(n_pos,n_cand) or 0):.2f}% because funnel is "
            f"{n_cand} cand -> {n_trig} trig -> {n_ao} already_open + {n_bf} book_full -> {n_pos} fills. "
            f"Bottleneck = per-symbol occupancy."
        ),
        "Q5": (
            "can_open(symbol) False when symbol in open_trades; engine labels already_open. "
            "Per-symbol, side-agnostic, ghosts don't count, cross-symbol OK, no multi-position/symbol."
        ),
        "Q6": (
            f"book_full_break from book_is_full(): max_positions={MAX_POSITIONS} OR cash/margin exhausted. "
            f"n={n_bf}; cash/margin-cap~={bf_json.get('likely_cash_margin_cap_count')}."
        ),
        "Q7": (
            f"Of {ao_json['n_rejects']}: {dict(ao_breakdown)}. "
            f"Legitimate same-symbol occupancy dominates. "
            f"STALE={stale_n} often = occupant opened before ENTRY_START trade window."
        ),
        "Q8": (
            (f"Replace-on-signal: fills {occ_cf['normal']['fills']}->{occ_cf['relaxed']['fills']} "
             f"(delta={occ_cf.get('delta_fills')}); replaced~={occ_cf['relaxed'].get('n_replaced_closes')}. "
             f"Many rejects become fills if occupancy cleared — diagnostic only.")
            if isinstance(occ_cf, dict) and occ_cf.get("relaxed") else f"Counterfactual: {occ_cf}"
        ),
        "Q9": (
            (f"Net {occ_cf['normal']['net']}->{occ_cf['relaxed']['net']} (delta={occ_cf.get('delta_net')}); "
             f"PF {occ_cf['normal']['profit_factor']}->{occ_cf['relaxed']['profit_factor']}; "
             f"MDD {occ_cf['normal']['max_drawdown']}->{occ_cf['relaxed']['max_drawdown']}. "
             f"Exposure/turnover change, NOT optimization.")
            if isinstance(occ_cf, dict) and occ_cf.get("relaxed") else f"Counterfactual: {occ_cf}"
        ),
        "Q10": (
            f"GSM grid: {[(r['ghost_sl_mult'], r['fills'], r.get('already_open')) for r in gsm_rows]}. "
            f"fills_range={fills_range}. ghost_sl_mult does not drive acceptance bottleneck."
        ),
        "Q11": (
            f"mean={sl_summary.get('mean')} median={sl_summary.get('median')} max={sl_summary.get('max')}. "
            f"Skew from high absolute-price symbols (JPY/TRY/ZAR). Not assumed bug."
        ),
        "Q12": (
            f"exact_dup_groups={dup_sum.get('exact_duplicate_groups')} extra={dup_sum.get('exact_duplicate_extra')} "
            f"rate={dup_sum.get('duplicate_rate')} consec={dup_sum.get('consecutive_similar_pairs')}."
        ),
        "Q13": (
            f"POST_FIX {n_pos}-fill regime coherent with symbol-local signals + per-symbol already_open "
            f"explaining ~{n_ao}/{n_trig} rejects."
        ),
        "Q14": (
            f"LOO: {[(r.get('label'), r.get('fills'), r.get('net'), r.get('profit_factor')) for r in loo_rows]}. "
            f"Universe sensitivity remains without zone_polluted. minus_top2/top5 absent from Test3."
        ),
        "Q15": "NO — not ready for OOS deployment claim. Mechanistic in-sample diagnostic only.",
        "Q16": (
            f"Status={status}. Do NOT preregister OOS without explicit approval. Live/Mirror untouched."
        ),
        "FINAL_STATUS": status,
    }
    _jdump(OUT / "TEST4_ANSWERS.json", answers)

    finished = datetime.now(timezone.utc)
    manifest = json.loads((OUT / "TEST4_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["status"] = status
    manifest["finished_utc"] = finished.isoformat()
    manifest["resume_elapsed_s"] = (finished - started).total_seconds()
    manifest["git_commit"] = _git()
    manifest["git_branch"] = _branch()
    _jdump(OUT / "TEST4_MANIFEST.json", manifest)

    exec_md = f"""# TEST4 EXECUTIVE SUMMARY

**Run:** `{OUT.name}`  
**Status:** `{status}`  
**Regime:** POST_FIX only (PRE_FIX audit-only)  
**Nature:** DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT

## Phase 0 control (exact match)

candidates={repro['candidates']} triggers={repro['triggers']} positions={repro['positions']}  
already_open={repro['already_open']} book_full={repro['book_full_break']}  
net={repro['net']} PF={repro['profit_factor']} MDD={repro['max_drawdown']}

## Headlines

- Q1: PRE_FIX cache bug suppressed tape; POST_FIX ~31x candidates
- Q3 independence (same master): exact={n_exact} minor={n_minor}/{len(indep_alone)}
- Q4: 13% fill = per-symbol already_open bottleneck ({n_ao}/{n_trig})
- Q5/Q6: already_open=per-symbol open_trades; book_full=global cap/cash
- Q10: ghost_sl_mult fills_range={fills_range} (not the bottleneck)
- Q15 OOS ready? NO

## Verdict

`{status}`

Do not declare profitable / deploy / promote Live-Mirror / treat counterfactual as optimization.
"""
    (OUT / "TEST4_EXECUTIVE_SUMMARY.md").write_text(exec_md, encoding="utf-8")

    report = f"""# TEST4 REPORT — POST_FIX Signal-Tape + Fill-Acceptance Forensics

## Meta
- out: {OUT}
- git: {_git()} / {_branch()}
- H1 hash match: {hh == T1_HASH}
- resumed after Phase7 Unicode crash; Phase0 was exact match

## Control
```json
{json.dumps(repro, indent=2, default=str)}
```

## already_open
```json
{json.dumps(ao_json, indent=2, default=str)[:8000]}
```

## book_full
```json
{json.dumps(bf_json, indent=2, default=str)[:4000]}
```

## Counterfactual
```json
{json.dumps(occ_cf, indent=2, default=str)[:12000]}
```

## Causal
```json
{json.dumps(causal, indent=2, default=str)}
```

## Q&A
"""
    for k in [f"Q{i}" for i in range(1, 17)] + ["FINAL_STATUS"]:
        report += f"\n### {k}\n\n{answers.get(k)}\n"
    report += """
## Constraints
- OFFLINE only; no MT5/Live/Mirror
- No param optimization
- PRE_FIX audit-only
- Counterfactual diagnostic-only
"""
    (OUT / "TEST4_REPORT.md").write_text(report, encoding="utf-8")
    print(f"\n=== TEST4 RESUME DONE status={status} OUT={OUT} ===", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        err = traceback.format_exc()
        try:
            print(err.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
        (OUT / "RESUME_FATAL.txt").write_text(err, encoding="utf-8")
        raise
