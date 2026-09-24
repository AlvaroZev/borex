"""Gate 2 natural-timing sample collector (read-only observation).

Purpose
-------
Accumulate naturally occurring Mirror magic=88002 closed round-trips using the
corrected UTC deal pipeline, classify populations, and score Level-1 economic
fidelity vs Theory when a clean 1:1 map exists.

Hard rules
----------
- Magic must be 88002 only (refuse 88001).
- Do NOT modify strategy, simulator, SL/TP, sizing, leverage, or architecture.
- Do NOT force-match trades.
- Do NOT close positions to manufacture evidence.
- Catch-up / session-aftermath trades stay in a separate population.
- Gate 2 remains NO-GO until a sufficiently large *clean natural* sample exists.
- Strategy optimization remains NO-GO.

Usage
-----
  # Seed / refresh from a known capture window (offline UTC dump)
  python scripts/run_gate2_natural_sample_collect.py --seed-window data/runs/golden_windows/mirror_live_exec_20260908_143358

  # Pull live Mirror history into the registry (UTC-normalized)
  python scripts/run_gate2_natural_sample_collect.py --pull-live --since 2026-09-08T14:00:00+00:00

  # Emit checkpoint report for current clean-natural count
  python scripts/run_gate2_natural_sample_collect.py --checkpoint
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

from run_gate2_broker_fidelity import (  # noqa: E402
    pair_broker_rounds,
    yahoo_from_mt5,
)

MAGIC = 88002
FORBIDDEN_MAGIC = 88001
REGISTRY_ROOT = ROOT / "data" / "runs" / "gate2_natural_sample"
REGISTRY_PATH = REGISTRY_ROOT / "registry.json"
STATUS_PATH = REGISTRY_ROOT / "STATUS.md"
CHECKPOINTS_DIR = REGISTRY_ROOT / "checkpoints"

# Bar-open Theory label vs wall-clock close-fill: expect ~0–7200s for H1 close fills.
CLEAN_ENTRY_SKEW_MAX_SEC = 7200
# Catch-up heuristic: many entries within a short cluster after idle.
BURST_CLUSTER_SEC = 90
BURST_MIN_ENTRIES = 4
# Known catch-up burst in the forensic window (bars flushed after idle).
KNOWN_CATCHUP_WINDOWS: list[tuple[str, str, str]] = [
    # (source_window_id, burst_start_utc, burst_end_utc)
    (
        "mirror_live_exec_20260908_143358",
        "2026-09-08T19:39:00+00:00",
        "2026-09-08T20:05:00+00:00",
    ),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(x: Any) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(x: Any) -> str | None:
    if x is None:
        return None
    return _ts(x).isoformat()


def _median_abs(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(abs(v) for v in vals))


def _exit_class_broker(comment: str | None, still_open: bool) -> str:
    if still_open:
        return "still_open"
    c = (comment or "").lower()
    if "[sl" in c or c.startswith("sl ") or " stop" in c:
        return "sl"
    if "[tp" in c or c.startswith("tp "):
        return "tp"
    if "flatten" in c or "force_flat" in c or "eod" in c:
        return "flatten"
    return "other"


def _exit_class_theory(reason: str | None) -> str:
    r = (reason or "").lower()
    if not r:
        return "unknown"
    if "margin_stop" in r or r in ("sl", "stop", "stop_loss"):
        return "sl"
    if "take_profit" in r or r == "tp":
        return "tp"
    if "flatten" in r or "force_flat" in r or "eod" in r:
        return "flatten"
    if "open_eow" in r:
        return "still_open"
    return "other"


def _sl_tp_agree(theory_cls: str, broker_cls: str) -> bool | None:
    if theory_cls in ("unknown",) or broker_cls in ("other", "still_open"):
        # margin_stop (theory) vs broker [sl] is agreement for economic class
        pass
    if theory_cls == "sl" and broker_cls == "sl":
        return True
    if theory_cls == "tp" and broker_cls == "tp":
        return True
    if theory_cls == "flatten" and broker_cls == "flatten":
        return True
    if theory_cls == "still_open" or broker_cls == "still_open":
        return None
    # Theory margin_stop already mapped to sl above
    if theory_cls == "other" or broker_cls == "other":
        return False
    return False


def _parse_sl_from_comment(comment: str | None) -> float | None:
    if not comment:
        return None
    m = re.search(r"\[sl\s+([-+0-9.]+)\]", comment, flags=re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"\[tp\s+([-+0-9.]+)\]", comment, flags=re.I)
    if m:
        return float(m.group(1))
    return None


def empty_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "purpose": "Gate2 natural-timing Mirror 88002 sample accumulation",
        "magic": MAGIC,
        "port": 8792,
        "gate2": "NO-GO",
        "strategy_optimization": "NO-GO",
        "created_at": _utc_now().isoformat(),
        "updated_at": _utc_now().isoformat(),
        "targets": {
            "initial_review_clean_natural_closed": 20,
            "stronger_assessment_clean_natural_closed": 30,
            "preferred_range": "30-50+",
        },
        "observation_windows": [],
        "broker_round_trips": [],
        "theory_trades": [],
        "mappings": {
            "clean_natural": [],
            "ambiguous": [],
            "unmapped_broker": [],
            "unmapped_theory": [],
            "excluded_catch_up": [],
            "excluded_pre_window": [],
            "still_open": [],
        },
        "tick_fidelity": [],
        "checkpoints_emitted": [],
        "notes": [
            "Level 1 = economic fidelity (no second-level timestamp equality required).",
            "Level 2 = tick fidelity only when ticks exist; OHLC miss of second-level timing is not an engine defect.",
            "Do not force-match. Catch-up burst trades are never clean_natural.",
        ],
    }


def load_registry() -> dict[str, Any]:
    REGISTRY_ROOT.mkdir(parents=True, exist_ok=True)
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_PATH.exists():
        reg = empty_registry()
        save_registry(reg)
        return reg
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def save_registry(reg: dict[str, Any]) -> None:
    reg["updated_at"] = _utc_now().isoformat()
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, default=str), encoding="utf-8")


def classify_population(
    entry_time: str | None,
    *,
    observation_start: str | None,
    observation_end: str | None,
    source_window: str | None,
    entry_times_sorted: list[pd.Timestamp],
) -> str:
    if entry_time is None:
        return "unknown"
    et = _ts(entry_time)
    if observation_start and et < _ts(observation_start):
        return "pre_window"
    # Known forensic catch-up windows
    for wid, b0, b1 in KNOWN_CATCHUP_WINDOWS:
        if source_window and wid in source_window and _ts(b0) <= et <= _ts(b1):
            return "catch_up_burst"
    # Generic burst: ≥BURST_MIN_ENTRIES within BURST_CLUSTER_SEC of this entry
    cluster = [
        t
        for t in entry_times_sorted
        if abs((t - et).total_seconds()) <= BURST_CLUSTER_SEC
    ]
    if len(cluster) >= BURST_MIN_ENTRIES:
        return "catch_up_burst"
    if observation_end and et > _ts(observation_end) + timedelta(minutes=30):
        return "aftermath"
    return "natural"


def broker_record_from_paired(
    t: dict[str, Any],
    *,
    source_window: str,
    observation_start: str | None,
    observation_end: str | None,
    entry_times_sorted: list[pd.Timestamp],
) -> dict[str, Any]:
    still_open = bool(t.get("still_open")) or t.get("broker_exit_time") is None
    entry_t = t.get("broker_entry_time")
    pop = classify_population(
        entry_t,
        observation_start=observation_start,
        observation_end=observation_end,
        source_window=source_window,
        entry_times_sorted=entry_times_sorted,
    )
    comment = t.get("comment")
    broker_exit_class = _exit_class_broker(comment, still_open)
    sl_from_cmt = _parse_sl_from_comment(comment)
    commission = float(t.get("commission") or 0.0)
    swap = float(t.get("swap") or 0.0)
    profit = t.get("profit")
    gross = None
    net = None
    if profit is not None and not still_open:
        # MT5: profit typically excludes commission/swap; those are separate deal fields.
        gross = float(profit)
        net = float(profit) + commission + swap
    return {
        "id": f"broker:{int(t.get('entry_ticket') or 0)}",
        "source_window": source_window,
        "symbol": t.get("symbol") or yahoo_from_mt5(t.get("symbol_mt5") or ""),
        "symbol_mt5": t.get("symbol_mt5"),
        "direction": t.get("side"),
        "broker_entry_timestamp_utc": _iso(entry_t),
        "broker_entry_price": t.get("broker_entry_price"),
        "broker_exit_timestamp_utc": _iso(t.get("broker_exit_time")),
        "broker_exit_price": t.get("broker_exit_price"),
        "sl_tp_level_broker": sl_from_cmt,
        "exit_class_broker": broker_exit_class,
        "volume": t.get("volume"),
        "commission": commission,
        "swap": swap,
        "gross_pnl": gross,
        "net_pnl": net,
        "entry_ticket": t.get("entry_ticket"),
        "exit_ticket": t.get("exit_ticket"),
        "order_comment": comment,
        "magic": MAGIC,
        "population": pop,
        "present_before_capture_window": pop == "pre_window",
        "opened_naturally_during_observation": pop == "natural",
        "still_open": still_open,
        "closed_round_trip": (not still_open) and entry_t is not None,
    }


def load_theory_from_window(window: Path) -> list[dict[str, Any]]:
    path = window / "execution_fidelity" / "h1_only_replay_a.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for tr in data.get("trades") or []:
        out.append(
            {
                "id": f"theory:{tr.get('signal_id') or tr.get('symbol')}|{tr.get('entry_time')}",
                "source_window": window.name,
                "symbol": tr.get("symbol"),
                "direction": tr.get("side"),
                "signal_intent_timestamp": _iso(tr.get("entry_time")),
                "theory_entry_price": tr.get("entry"),
                "theory_exit_timestamp": _iso(tr.get("exit_time")),
                "theory_exit_price": tr.get("exit"),
                "sl_level": tr.get("sl"),
                "tp_level": tr.get("tp"),
                "exit_reason": tr.get("exit_reason"),
                "exit_class_theory": _exit_class_theory(tr.get("exit_reason")),
                "volume": None,
                "commission": tr.get("commission"),
                "swap": None,
                "gross_pnl": tr.get("gross_pnl"),
                "net_pnl": tr.get("pnl"),
                "signal_id": tr.get("signal_id"),
                "pattern": tr.get("pattern"),
            }
        )
    return out


def upsert_by_id(items: list[dict[str, Any]], row: dict[str, Any]) -> None:
    rid = row["id"]
    for i, existing in enumerate(items):
        if existing.get("id") == rid:
            items[i] = {**existing, **row}
            return
    items.append(row)


def attempt_maps(reg: dict[str, Any]) -> None:
    """Rebuild mapping lists without force-matching.

    Clean natural requires:
      - broker closed RT
      - population == natural
      - symbol + direction match
      - unique Theory candidate within CLEAN_ENTRY_SKEW_MAX_SEC
      - timestamps already UTC-normalized at ingest
    Catch-up / pre-window never become clean_natural.
    """
    brokers = list(reg.get("broker_round_trips") or [])
    theories = list(reg.get("theory_trades") or [])

    clean: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    excluded_catch: list[dict[str, Any]] = []
    excluded_pre: list[dict[str, Any]] = []
    still_open: list[dict[str, Any]] = []
    used_theory: set[str] = set()
    used_broker: set[str] = set()

    for b in brokers:
        bid = b["id"]
        if b.get("still_open") or not b.get("closed_round_trip"):
            still_open.append({"broker_id": bid, "symbol": b.get("symbol")})
            used_broker.add(bid)
            continue
        pop = b.get("population")
        if pop == "catch_up_burst":
            excluded_catch.append(
                {
                    "broker_id": bid,
                    "symbol": b.get("symbol"),
                    "broker_entry_timestamp_utc": b.get("broker_entry_timestamp_utc"),
                    "reason": "catch_up_burst_population",
                }
            )
            used_broker.add(bid)
            continue
        if pop == "pre_window":
            excluded_pre.append(
                {
                    "broker_id": bid,
                    "symbol": b.get("symbol"),
                    "broker_entry_timestamp_utc": b.get("broker_entry_timestamp_utc"),
                    "reason": "pre_window",
                }
            )
            used_broker.add(bid)
            continue
        if pop != "natural":
            ambiguous.append(
                {
                    "broker_id": bid,
                    "symbol": b.get("symbol"),
                    "reason": f"population={pop}",
                }
            )
            used_broker.add(bid)
            continue

        # Candidate Theory trades: same symbol + direction, unused, within skew
        cands: list[tuple[float, dict[str, Any]]] = []
        be = _ts(b["broker_entry_timestamp_utc"])
        for th in theories:
            if th["id"] in used_theory:
                continue
            if th.get("symbol") != b.get("symbol"):
                continue
            if th.get("direction") and b.get("direction") and th["direction"] != b["direction"]:
                continue
            te = _ts(th["signal_intent_timestamp"])
            skew = abs((be - te).total_seconds())
            if skew <= CLEAN_ENTRY_SKEW_MAX_SEC:
                cands.append((skew, th))
        cands.sort(key=lambda x: x[0])

        if not cands:
            continue  # leave as unmapped_broker later

        # Ambiguous if two candidates within 30% of best skew or both < 600s of each other
        if len(cands) >= 2 and (cands[1][0] - cands[0][0]) < 600:
            ambiguous.append(
                {
                    "broker_id": bid,
                    "symbol": b.get("symbol"),
                    "reason": "multiple_theory_candidates",
                    "candidates": [
                        {
                            "theory_id": c[1]["id"],
                            "skew_sec": c[0],
                            "signal_intent_timestamp": c[1].get("signal_intent_timestamp"),
                        }
                        for c in cands[:3]
                    ],
                }
            )
            used_broker.add(bid)
            continue

        skew, th = cands[0]
        used_theory.add(th["id"])
        used_broker.add(bid)

        entry_err = None
        if th.get("theory_entry_price") is not None and b.get("broker_entry_price") is not None:
            entry_err = float(th["theory_entry_price"]) - float(b["broker_entry_price"])
        exit_err = None
        if th.get("theory_exit_price") is not None and b.get("broker_exit_price") is not None:
            exit_err = float(th["theory_exit_price"]) - float(b["broker_exit_price"])
        pnl_err = None
        if th.get("net_pnl") is not None and b.get("net_pnl") is not None:
            pnl_err = float(th["net_pnl"]) - float(b["net_pnl"])

        t_cls = th.get("exit_class_theory") or _exit_class_theory(th.get("exit_reason"))
        b_cls = b.get("exit_class_broker")
        # Treat theory margin_stop as SL economically
        if (th.get("exit_reason") or "").lower() == "margin_stop":
            t_cls = "sl"

        clean.append(
            {
                "match_id": f"{bid}::{th['id']}",
                "mapping_class": "clean_natural",
                "symbol": b.get("symbol"),
                "direction": b.get("direction"),
                "signal_intent_timestamp": th.get("signal_intent_timestamp"),
                "broker_entry_timestamp_utc": b.get("broker_entry_timestamp_utc"),
                "broker_entry_price": b.get("broker_entry_price"),
                "theory_entry_price": th.get("theory_entry_price"),
                "entry_price_error": entry_err,
                "entry_timing_skew_sec": skew,
                "broker_exit_timestamp_utc": b.get("broker_exit_timestamp_utc"),
                "broker_exit_price": b.get("broker_exit_price"),
                "theory_exit_price": th.get("theory_exit_price"),
                "exit_price_error": exit_err,
                "sl_tp_level": th.get("sl_level") or b.get("sl_tp_level_broker"),
                "sl_level_theory": th.get("sl_level"),
                "tp_level_theory": th.get("tp_level"),
                "sl_tp_level_broker": b.get("sl_tp_level_broker"),
                "exit_reason_theory": th.get("exit_reason"),
                "exit_class_theory": t_cls,
                "exit_class_broker": b_cls,
                "exit_class_agree": _sl_tp_agree(t_cls, b_cls),
                "volume": b.get("volume"),
                "commission_broker": b.get("commission"),
                "commission_theory": th.get("commission"),
                "swap": b.get("swap"),
                "gross_pnl_broker": b.get("gross_pnl"),
                "net_pnl_broker": b.get("net_pnl"),
                "net_pnl_theory": th.get("net_pnl"),
                "net_pnl_error": pnl_err,
                "present_before_capture_window": False,
                "opened_naturally_during_observation": True,
                "maps_clean_1_to_1": True,
                "broker_id": bid,
                "theory_id": th["id"],
                "entry_ticket": b.get("entry_ticket"),
                "source_window": b.get("source_window"),
                "level1_economic": {
                    "identity_ok": True,
                    "direction_ok": th.get("direction") == b.get("direction"),
                    "entry_price_error": entry_err,
                    "exit_price_error": exit_err,
                    "exit_class_agree": _sl_tp_agree(t_cls, b_cls),
                    "net_pnl_error": pnl_err,
                    "volume": b.get("volume"),
                },
                "level2_tick": None,
            }
        )

    unmapped_broker = [
        {
            "broker_id": b["id"],
            "symbol": b.get("symbol"),
            "population": b.get("population"),
            "broker_entry_timestamp_utc": b.get("broker_entry_timestamp_utc"),
            "closed_round_trip": b.get("closed_round_trip"),
            "still_open": b.get("still_open"),
        }
        for b in brokers
        if b["id"] not in used_broker
    ]
    unmapped_theory = [
        {
            "theory_id": th["id"],
            "symbol": th.get("symbol"),
            "signal_intent_timestamp": th.get("signal_intent_timestamp"),
            "exit_reason": th.get("exit_reason"),
        }
        for th in theories
        if th["id"] not in used_theory
    ]

    # Attach known tick fidelity for AUDJPY forensic case if present
    tick_rows = list(reg.get("tick_fidelity") or [])
    tick_by_ticket = {
        int(r.get("entry_ticket") or 0): r for r in tick_rows if r.get("entry_ticket")
    }
    for m in clean:
        ticket = int(m.get("entry_ticket") or 0)
        if ticket in tick_by_ticket:
            m["level2_tick"] = tick_by_ticket[ticket]

    reg["mappings"] = {
        "clean_natural": clean,
        "ambiguous": ambiguous,
        "unmapped_broker": unmapped_broker,
        "unmapped_theory": unmapped_theory,
        "excluded_catch_up": excluded_catch,
        "excluded_pre_window": excluded_pre,
        "still_open": still_open,
    }


def seed_audjpy_tick_fidelity(reg: dict[str, Any], window: Path) -> None:
    tick_path = (
        window
        / "execution_fidelity"
        / "audjpy_ticks_150018_150050_utc.json"
    )
    row = {
        "entry_ticket": 1543910449,
        "order": 1917027881,
        "symbol": "AUDJPY=X",
        "tick_artifact": str(tick_path.resolve().relative_to(ROOT.resolve())).replace("\\", "/"),
        "broker_sl": 111.265,
        "first_executable_ask_touch_utc": "2026-09-08T15:00:45.915+00:00",
        "first_executable_ask": 111.266,
        "first_bid_ge_sl_utc": "2026-09-08T15:00:46.516+00:00",
        "broker_exit_utc": "2026-09-08T15:00:46+00:00",
        "simulated_exit_utc_ohlc": "2026-09-08T15:00:00+00:00",
        "bid_ask_side": "ask_for_short_cover",
        "first_touch_agrees_with_broker_exit": True,
        "exit_time_error_sec_vs_ohlc_stamp": 46.0,
        "note": (
            "Level-2 only. OHLC engine cannot reproduce second-level ask touch; "
            "not treated as Level-1 engine defect."
        ),
    }
    existing = [r for r in (reg.get("tick_fidelity") or []) if int(r.get("entry_ticket") or 0) != 1543910449]
    existing.append(row)
    reg["tick_fidelity"] = existing


def ingest_window(reg: dict[str, Any], window: Path) -> dict[str, Any]:
    deals_path = window / "broker_deals_magic_88002.json"
    if not deals_path.exists():
        raise SystemExit(f"Missing {deals_path}")
    raw = json.loads(deals_path.read_text(encoding="utf-8"))
    if not raw.get("timestamp_normalized"):
        raise SystemExit(
            f"{deals_path} is not timestamp_normalized — run Gate2 UTC refresh first"
        )
    deals = [d for d in (raw.get("deals") or []) if int(d.get("magic") or 0) == MAGIC]
    bad = [d for d in (raw.get("deals") or []) if int(d.get("magic") or 0) == FORBIDDEN_MAGIC]
    if bad:
        raise SystemExit("HARD FAIL: magic 88001 deals present")
    if any(int(d.get("magic") or 0) != MAGIC for d in deals):
        raise SystemExit("HARD FAIL: non-88002 deals after filter")

    paired = pair_broker_rounds(deals)
    obs_start = raw.get("since")
    obs_end = raw.get("until")
    entry_times = sorted(
        _ts(p["broker_entry_time"])
        for p in paired
        if p.get("broker_entry_time") is not None
    )

    win_meta = {
        "id": window.name,
        "path": str(window),
        "observation_start_utc": obs_start,
        "observation_end_utc": obs_end,
        "server_offset_seconds": raw.get("server_offset_seconds"),
        "normalization": raw.get("normalization"),
        "n_deals": len(deals),
        "n_paired": len(paired),
        "ingested_at": _utc_now().isoformat(),
    }
    existing_windows = [w for w in reg.get("observation_windows") or [] if w.get("id") != window.name]
    existing_windows.append(win_meta)
    reg["observation_windows"] = existing_windows

    for p in paired:
        row = broker_record_from_paired(
            p,
            source_window=window.name,
            observation_start=obs_start,
            observation_end=obs_end,
            entry_times_sorted=entry_times,
        )
        upsert_by_id(reg.setdefault("broker_round_trips", []), row)

    for th in load_theory_from_window(window):
        upsert_by_id(reg.setdefault("theory_trades", []), th)

    seed_audjpy_tick_fidelity(reg, window)
    attempt_maps(reg)
    return win_meta


def pull_live(reg: dict[str, Any], since: str, until: str | None) -> dict[str, Any]:
    from borex_live.mt5.client import Mt5Client
    from run_mirror_golden_live_execute import count_magic_deals

    since_dt = _ts(since).to_pydatetime()
    until_dt = _ts(until).to_pydatetime() if until else _utc_now()
    client = Mt5Client(
        path=os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", ""),
    )
    client.connect()
    if not client.connected:
        raise SystemExit("HARD FAIL: MT5 not connected for --pull-live")
    offset = int(getattr(client, "server_offset_seconds", 0) or 0)
    n, rows = count_magic_deals(client, MAGIC, since_dt, until_utc=until_dt)
    bad = [r for r in rows if int(r.get("magic") or 0) == FORBIDDEN_MAGIC]
    if bad:
        raise SystemExit("HARD FAIL: magic 88001 in live pull")

    pull_id = f"live_pull_{_utc_now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = REGISTRY_ROOT / "pulls"
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = {
        "since": since_dt.isoformat(),
        "until": until_dt.isoformat(),
        "n": n,
        "deals": rows,
        "magic": MAGIC,
        "port": 8792,
        "timestamp_normalized": True,
        "server_offset_seconds": offset,
        "normalization": "mt5_unix_to_utc via Mt5Client.server_offset_seconds",
        "pulled_at": _utc_now().isoformat(),
        "pull_id": pull_id,
    }
    dump_path = out_dir / f"{pull_id}_deals.json"
    dump_path.write_text(json.dumps(dump, indent=2), encoding="utf-8")

    paired = pair_broker_rounds(rows)
    entry_times = sorted(
        _ts(p["broker_entry_time"])
        for p in paired
        if p.get("broker_entry_time") is not None
    )
    win_meta = {
        "id": pull_id,
        "path": str(dump_path),
        "observation_start_utc": since_dt.isoformat(),
        "observation_end_utc": until_dt.isoformat(),
        "server_offset_seconds": offset,
        "normalization": dump["normalization"],
        "n_deals": n,
        "n_paired": len(paired),
        "ingested_at": _utc_now().isoformat(),
        "kind": "live_pull",
    }
    reg.setdefault("observation_windows", []).append(win_meta)
    for p in paired:
        row = broker_record_from_paired(
            p,
            source_window=pull_id,
            observation_start=since_dt.isoformat(),
            observation_end=until_dt.isoformat(),
            entry_times_sorted=entry_times,
        )
        # Live pulls: if entry falls inside a known catch-up window, keep that label
        for wid, b0, b1 in KNOWN_CATCHUP_WINDOWS:
            et = row.get("broker_entry_timestamp_utc")
            if et and _ts(b0) <= _ts(et) <= _ts(b1):
                row["population"] = "catch_up_burst"
                row["opened_naturally_during_observation"] = False
                row["present_before_capture_window"] = False
        upsert_by_id(reg.setdefault("broker_round_trips", []), row)

    attempt_maps(reg)
    # intentionally no mt5.shutdown on shared terminal
    return win_meta


def counts(reg: dict[str, Any]) -> dict[str, Any]:
    m = reg.get("mappings") or {}
    brokers = reg.get("broker_round_trips") or []
    return {
        "clean_natural_closed": len(m.get("clean_natural") or []),
        "ambiguous": len(m.get("ambiguous") or []),
        "unmapped_broker": len(m.get("unmapped_broker") or []),
        "unmapped_theory": len(m.get("unmapped_theory") or []),
        "excluded_catch_up": len(m.get("excluded_catch_up") or []),
        "excluded_pre_window": len(m.get("excluded_pre_window") or []),
        "still_open": len(m.get("still_open") or []),
        "broker_closed_natural": sum(
            1
            for b in brokers
            if b.get("closed_round_trip") and b.get("population") == "natural"
        ),
        "broker_closed_catch_up": sum(
            1
            for b in brokers
            if b.get("closed_round_trip") and b.get("population") == "catch_up_burst"
        ),
        "tick_fidelity_rows": len(reg.get("tick_fidelity") or []),
    }


def economic_stats(clean: list[dict[str, Any]]) -> dict[str, Any]:
    entry_errs = [m["entry_price_error"] for m in clean if m.get("entry_price_error") is not None]
    exit_errs = [m["exit_price_error"] for m in clean if m.get("exit_price_error") is not None]
    pnl_errs = [m["net_pnl_error"] for m in clean if m.get("net_pnl_error") is not None]
    agree = [m.get("exit_class_agree") for m in clean if m.get("exit_class_agree") is not None]
    sim_pnl = sum(float(m["net_pnl_theory"]) for m in clean if m.get("net_pnl_theory") is not None)
    bro_pnl = sum(float(m["net_pnl_broker"]) for m in clean if m.get("net_pnl_broker") is not None)
    return {
        "n": len(clean),
        "median_abs_entry_error": _median_abs(entry_errs),
        "median_abs_exit_error": _median_abs(exit_errs),
        "sl_tp_classification_agreement": (
            None if not agree else float(sum(1 for a in agree if a) / len(agree))
        ),
        "median_abs_pnl_error": _median_abs(pnl_errs),
        "total_theory_net_pnl": sim_pnl if clean else None,
        "total_broker_net_pnl": bro_pnl if clean else None,
        "total_net_pnl_difference_theory_minus_broker": (
            (sim_pnl - bro_pnl) if clean else None
        ),
    }


def failure_patterns(clean: list[dict[str, Any]]) -> dict[str, Any]:
    by_symbol: dict[str, list[float]] = {}
    by_dir: dict[str, list[float]] = {}
    by_exit: dict[str, int] = {}
    for m in clean:
        sym = str(m.get("symbol"))
        d = str(m.get("direction"))
        if m.get("entry_price_error") is not None:
            by_symbol.setdefault(sym, []).append(float(m["entry_price_error"]))
            by_dir.setdefault(d, []).append(float(m["entry_price_error"]))
        key = f"{m.get('exit_class_theory')}→{m.get('exit_class_broker')}"
        by_exit[key] = by_exit.get(key, 0) + 1
    return {
        "entry_error_by_symbol_median_abs": {
            k: _median_abs(v) for k, v in sorted(by_symbol.items())
        },
        "entry_error_by_direction_median_abs": {
            k: _median_abs(v) for k, v in sorted(by_dir.items())
        },
        "exit_class_pairs": by_exit,
    }


def tick_stats(reg: dict[str, Any], clean: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for m in clean:
        t = m.get("level2_tick")
        if t:
            rows.append(t)
    if not rows:
        return {"n_with_ticks": 0, "note": "No Level-2 tick rows attached to clean matches"}
    return {
        "n_with_ticks": len(rows),
        "first_touch_agreement_rate": float(
            sum(1 for r in rows if r.get("first_touch_agrees_with_broker_exit")) / len(rows)
        ),
        "exit_time_errors_sec_vs_ohlc_stamp": [
            r.get("exit_time_error_sec_vs_ohlc_stamp") for r in rows
        ],
        "bid_ask_side": [r.get("bid_ask_side") for r in rows],
    }


def write_checkpoint(reg: dict[str, Any], force: bool = False) -> Path | None:
    c = counts(reg)
    n = c["clean_natural_closed"]
    thresholds = [10, 20, 30]
    emitted = set(reg.get("checkpoints_emitted") or [])
    to_emit = [t for t in thresholds if n >= t and (force or f"n{t}" not in emitted)]
    if force and not to_emit:
        # always allow explicit checkpoint snapshot labeled by current n
        label = f"n{n}_manual"
    elif to_emit:
        label = f"n{max(to_emit)}"
    else:
        return None

    clean = list((reg.get("mappings") or {}).get("clean_natural") or [])
    report = {
        "checkpoint": label,
        "generated_at": _utc_now().isoformat(),
        "gate2": "NO-GO",
        "strategy_optimization": "NO-GO",
        "mapping": c,
        "economic_fidelity_level1": economic_stats(clean),
        "tick_fidelity_level2": tick_stats(reg, clean),
        "failure_patterns": failure_patterns(clean),
        "clean_match_ids": [m.get("match_id") for m in clean],
        "stop_rules": {
            "systematic_simulator_defect": False,
            "consistent_mapping_data_problem": False,
            "cannot_reconcile_economically": False,
            "sufficient_sample_for_decision": n >= 30,
        },
        "next_action": (
            "Continue natural observation"
            if n < 20
            else (
                "Initial review possible; prefer 30–50 before fidelity decision"
                if n < 30
                else "Sample size may support a fidelity decision — review Level-1 metrics"
            )
        ),
    }
    path = CHECKPOINTS_DIR / f"checkpoint_{label}_{_utc_now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = CHECKPOINTS_DIR / f"checkpoint_{label}_{_utc_now().strftime('%Y%m%d_%H%M%S')}.md"
    md.write_text(render_checkpoint_md(report), encoding="utf-8")
    if not force or label.startswith("n") and label[1:].isdigit():
        emitted.add(label if label.startswith("n") and label[1:].split("_")[0].isdigit() else label)
        # track threshold keys
        for t in to_emit:
            emitted.add(f"n{t}")
    reg["checkpoints_emitted"] = sorted(emitted)
    save_registry(reg)
    return path


def render_checkpoint_md(report: dict[str, Any]) -> str:
    m = report["mapping"]
    e = report["economic_fidelity_level1"]
    t = report["tick_fidelity_level2"]
    lines = [
        f"# Gate 2 checkpoint — {report['checkpoint']}",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        f"**Gate 2:** {report['gate2']}  ",
        f"**Strategy optimization:** {report['strategy_optimization']}",
        "",
        "## Mapping",
        "",
        f"- clean matched (natural): **{m['clean_natural_closed']}**",
        f"- ambiguous: {m['ambiguous']}",
        f"- unmapped broker: {m['unmapped_broker']}",
        f"- unmapped Theory: {m['unmapped_theory']}",
        f"- excluded catch-up: {m['excluded_catch_up']}",
        f"- still open: {m['still_open']}",
        "",
        "## Economic fidelity (Level 1)",
        "",
        f"- median |entry error|: `{e.get('median_abs_entry_error')}`",
        f"- median |exit error|: `{e.get('median_abs_exit_error')}`",
        f"- SL/TP class agreement: `{e.get('sl_tp_classification_agreement')}`",
        f"- median |PnL error|: `{e.get('median_abs_pnl_error')}`",
        f"- total Theory net PnL: `{e.get('total_theory_net_pnl')}`",
        f"- total broker net PnL: `{e.get('total_broker_net_pnl')}`",
        f"- Theory − broker PnL: `{e.get('total_net_pnl_difference_theory_minus_broker')}`",
        "",
        "## Tick fidelity (Level 2)",
        "",
        f"- rows with ticks: {t.get('n_with_ticks')}",
        f"- first-touch agreement: `{t.get('first_touch_agreement_rate')}`",
        f"- exit-time error vs OHLC stamp (sec): `{t.get('exit_time_errors_sec_vs_ohlc_stamp')}`",
        "",
        "## Next action",
        "",
        report.get("next_action") or "",
        "",
    ]
    return "\n".join(lines)


def write_status(reg: dict[str, Any]) -> None:
    c = counts(reg)
    clean = (reg.get("mappings") or {}).get("clean_natural") or []
    lines = [
        "# Gate 2 natural sample - STATUS",
        "",
        f"Updated: `{reg.get('updated_at')}`",
        "",
        "## Research order",
        "",
        "1. Correct timestamp pipeline - **DONE**",
        "2. Validate trade identity/mapping - **IN PROGRESS**",
        "3. Validate economic execution fidelity - **IN PROGRESS**",
        "4. Accumulate sufficient natural Mirror sample - **ACTIVE**",
        "5. Decide whether simulator is adequate - **LATER**",
        "6. Resume strategy research / OOS / optimization - **BLOCKED**",
        "",
        "## Locks",
        "",
        "- Gate 2: **NO-GO**",
        "- Strategy optimization: **NO-GO**",
        "- Do not touch live `8791` / magic `88001`",
        "- Do not close Mirror positions to manufacture evidence",
        "- Do not modify alexg8/9, ghost_sl_mult, leverage, RR, occupancy, sizing, SL/TP, execution",
        "",
        "## Sample counters",
        "",
        f"| Metric | Count |",
        f"|--|--|",
        f"| Clean natural closed matches | **{c['clean_natural_closed']}** |",
        f"| Broker closed natural (incl. unmapped) | {c['broker_closed_natural']} |",
        f"| Broker closed catch-up (excluded) | {c['broker_closed_catch_up']} |",
        f"| Ambiguous | {c['ambiguous']} |",
        f"| Unmapped broker | {c['unmapped_broker']} |",
        f"| Unmapped Theory | {c['unmapped_theory']} |",
        f"| Still open | {c['still_open']} |",
        f"| Tick-fidelity rows | {c['tick_fidelity_rows']} |",
        "",
        f"Target: >=20 for initial review; preferably 30-50+.",
        "",
        "## Clean matches so far",
        "",
    ]
    if not clean:
        lines.append("_None yet._")
    else:
        lines.append("| Symbol | Dir | Broker entry UTC | Entry err | Exit class | Net PnL err |")
        lines.append("|--|--|--|--|--|--|")
        for m in clean:
            lines.append(
                f"| {m.get('symbol')} | {m.get('direction')} | "
                f"{m.get('broker_entry_timestamp_utc')} | {m.get('entry_price_error')} | "
                f"{m.get('exit_class_theory')}->{m.get('exit_class_broker')} | "
                f"{m.get('net_pnl_error')} |"
            )
    lines += [
        "",
        "## How to continue",
        "",
        "```powershell",
        "# After a natural Mirror session with UTC deals dump in a window:",
        "python scripts/run_gate2_natural_sample_collect.py --seed-window data/runs/golden_windows/<window>",
        "",
        "# Or pull live history (Mirror terminal logged in; magic 88002 only):",
        "python scripts/run_gate2_natural_sample_collect.py --pull-live --since 2026-09-08T14:00:00+00:00",
        "",
        "# Force a checkpoint snapshot anytime:",
        "python scripts/run_gate2_natural_sample_collect.py --checkpoint --force",
        "```",
        "",
        "Checkpoints auto-emit at 10 / 20 / 30 clean natural matches.",
        "",
    ]
    STATUS_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed-window", default="", help="Ingest a golden/live capture window")
    ap.add_argument("--pull-live", action="store_true", help="Pull live MT5 magic=88002 deals")
    ap.add_argument("--since", default="2026-09-08T14:00:00+00:00")
    ap.add_argument("--until", default="")
    ap.add_argument("--checkpoint", action="store_true", help="Write checkpoint if thresholds met")
    ap.add_argument("--force", action="store_true", help="Force checkpoint even below thresholds")
    ap.add_argument("--remap", action="store_true", help="Rebuild mappings only")
    args = ap.parse_args()

    reg = load_registry()
    did = False

    if args.seed_window:
        meta = ingest_window(reg, Path(args.seed_window))
        save_registry(reg)
        print(json.dumps({"seeded": meta, "counts": counts(reg)}, indent=2, default=str))
        did = True

    if args.pull_live:
        meta = pull_live(reg, args.since, args.until or None)
        save_registry(reg)
        print(json.dumps({"pulled": meta, "counts": counts(reg)}, indent=2, default=str))
        did = True

    if args.remap:
        attempt_maps(reg)
        save_registry(reg)
        print(json.dumps({"remapped": True, "counts": counts(reg)}, indent=2))
        did = True

    if args.checkpoint or args.force:
        path = write_checkpoint(reg, force=bool(args.force))
        print(json.dumps({"checkpoint": str(path) if path else None, "counts": counts(reg)}, indent=2))
        did = True

    if not did:
        # Default: show status / counts
        attempt_maps(reg)
        save_registry(reg)
        print(json.dumps({"counts": counts(reg), "gate2": "NO-GO"}, indent=2))

    write_status(reg)
    c = counts(reg)
    print(
        f"STATUS: clean_natural={c['clean_natural_closed']} "
        f"catch_up_closed={c['broker_closed_catch_up']} "
        f"still_open={c['still_open']} | Gate2=NO-GO | opt=NO-GO",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
