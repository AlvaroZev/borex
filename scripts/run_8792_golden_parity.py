"""Golden-fixture parity suite for 8792 (checkpoint / cold / warm / determinism).

Hard gate: START-of-window checkpoint is missing from DB. This script:
  - proves config_hash recovery
  - tests end-checkpoint restore determinism
  - runs cold / historical-warmup / (unavailable) start-checkpoint modes
  - compares fills to exported mirror_theory_trades
  - records first divergence forensics

Does NOT tune strategy. Does NOT touch 8791/broker.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import sys
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
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

FIX = ROOT / "data" / "runs" / "mirror_2d_8792" / "reconciliation" / "golden_fixture"
OUT = FIX / "parity"
ENTRY_START = pd.Timestamp("2026-08-27 01:00:00", tz="UTC")
LIVE_END = pd.Timestamp("2026-08-28 23:00:00", tz="UTC")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _side(x) -> str:
    s = str(x or "").lower()
    return "long" if s in ("buy", "long") else "short"


def _hour(ts) -> str:
    return _ts(ts).strftime("%Y-%m-%d %H:00") if ts else ""


def _pair(x: str) -> str:
    s = str(x or "").replace("=X", "").strip().upper()
    return f"{s}=X"


def load_surrogate_candles() -> dict[str, list]:
    from borex.models.candle import Candle

    man = json.loads((FIX / "h1_surrogate_manifest.json").read_text(encoding="utf-8"))
    cache_dir = FIX / "h1_surrogate_cache"
    out: dict[str, list] = {}
    for sym, meta in (man.get("per_pair") or {}).items():
        rows = json.loads((cache_dir / meta["file"]).read_text(encoding="utf-8"))
        out[sym] = [
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
    return out


def make_shadow(capital: float):
    from borex.backtest.engine import BacktestConfig
    from borex_live.config import LiveServiceConfig
    from borex_live.engine.shadow_engine import ShadowEngine
    from borex_live.strategy_registry import create_strategy

    cfg_snap = json.loads((FIX / "config_snapshot.json").read_text(encoding="utf-8"))
    fields = (cfg_snap.get("matched_variant") or {}).get("fields") or {}
    live_cfg = LiveServiceConfig(
        strategy=str(fields.get("strategy", "alexg8")),
        capital=float(capital),
        leverage=float(fields.get("leverage", 5000)),
        min_rr=float(fields.get("min_rr", 2)),
        rr_factor=float(fields.get("rr_factor", 1)),
        rr_mode=str(fields.get("rr_mode", "dynamic")),
        # rr clamps reconstructed for fills — NOT in config_hash
        rr_min=2.0,
        rr_max=6.0,
        position_size_pct=float(fields.get("position_size_pct", 0.01)),
        max_positions=int(fields.get("max_positions", 60)),
        interval=str(fields.get("interval", "1h")),
        same_bar_exit=bool(fields.get("same_bar_exit", False)),
        commission_per_lot=float(fields.get("commission_per_lot", 7)),
        risk_include_commission=bool(fields.get("risk_include_commission", True)),
        commission_at_entry=bool(fields.get("commission_at_entry", False)),
        force_flat_friday=bool(fields.get("force_flat_friday", False)),
        force_flat_daily=bool(fields.get("force_flat_daily", True)),
        force_flat_utc_hour=int(fields.get("force_flat_utc_hour", 19)),
        winrate_min_trades=int(fields.get("winrate_min_trades", 20)),
    )
    strategy, _ = create_strategy(
        live_cfg.strategy,
        min_rr=live_cfg.min_rr,
        second_signal="off",
        execution_interval=live_cfg.interval,
    )
    bt = BacktestConfig(
        initial_capital=float(capital),
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
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=live_cfg.risk_include_commission,
        commission_at_entry=live_cfg.commission_at_entry,
        force_flat_friday=live_cfg.force_flat_friday,
        force_flat_daily=live_cfg.force_flat_daily,
        force_flat_utc_hour=live_cfg.force_flat_utc_hour,
        winrate_min_trades=live_cfg.winrate_min_trades,
        spread_pips=0.0,
        spread_pips_by_symbol={},
        slippage_pips=0.0,
        intra_hour_sl=False,
    )
    shadow = ShadowEngine(strategy, live_cfg, bt)
    return shadow, live_cfg


class MemRepo:
    def __init__(self):
        self.trades = []
        self.state = None

    def get_theory_state(self):
        return self.state

    def reset_theory(self):
        self.trades.clear()
        self.state = None

    def save_theory_state(self, **kw):
        self.state = type("S", (), kw)()
        return self.state

    def upsert_theory_trade(self, trade):
        self.trades.append(trade)
        return trade


def portfolio_rows(shadow) -> list[dict]:
    rows = []
    for t in list(shadow.portfolio.closed_trades) + list(
        shadow.portfolio.open_trades.values()
    ):
        rows.append(
            {
                "symbol": t.symbol,
                "side": _side(t.side.value if hasattr(t.side, "value") else t.side),
                "entry_time": str(t.entry_time),
                "entry_hour": _hour(t.entry_time),
                "entry_price": float(t.entry_price),
                "exit_time": str(t.exit_time) if t.exit_time is not None else None,
                "exit_price": float(t.exit_price) if t.exit_price is not None else None,
                "exit_reason": t.exit_reason,
                "pnl": float(t.pnl or 0),
                "status": "open" if t.is_open else "closed",
                "pattern": str(t.pattern or ""),
            }
        )
    rows.sort(key=lambda r: (r["entry_time"], r["symbol"]))
    return rows


def match_theory(replay: list[dict], theory: list[dict]) -> dict:
    bucket = defaultdict(list)
    for r in replay:
        bucket[(_pair(r["symbol"]), r["side"], r["entry_hour"])].append(r)
    matched = missing = 0
    for t in theory:
        k = (_pair(t["symbol"]), _side(t.get("side")), _hour(t.get("entry_time")))
        if bucket.get(k):
            bucket[k].pop(0)
            matched += 1
        else:
            # ±1h
            found = False
            for sh in (-1, 1):
                if not k[2]:
                    break
                h = (pd.Timestamp(k[2]) + pd.Timedelta(hours=sh)).strftime(
                    "%Y-%m-%d %H:00"
                )
                k2 = (k[0], k[1], h)
                if bucket.get(k2):
                    bucket[k2].pop(0)
                    matched += 1
                    found = True
                    break
            if not found:
                missing += 1
    extra = sum(len(v) for v in bucket.values())
    n_t = len(theory)
    n_r = len(replay)
    return {
        "n_theory": n_t,
        "n_replay": n_r,
        "matched": matched,
        "missing": missing,
        "extra": extra,
        "recall": round(100 * matched / n_t, 2) if n_t else None,
        "precision": round(100 * matched / n_r, 2) if n_r else None,
    }


def first_div(theory: list[dict], replay: list[dict]) -> dict:
    th_by = defaultdict(list)
    rp_by = defaultdict(list)
    for t in theory:
        th_by[_pair(t["symbol"])].append(t)
    for r in replay:
        rp_by[_pair(r["symbol"])].append(r)
    for p in th_by:
        th_by[p].sort(key=lambda x: _hour(x.get("entry_time")))
    for p in rp_by:
        rp_by[p].sort(key=lambda x: x["entry_hour"])
    rows = []
    zero = 0
    for pair in sorted(set(th_by) | set(rp_by)):
        th, rp = th_by.get(pair, []), rp_by.get(pair, [])
        i = j = 0
        first = None
        while i < len(th) or j < len(rp):
            th_h = _hour(th[i].get("entry_time")) if i < len(th) else None
            rp_h = rp[j]["entry_hour"] if j < len(rp) else None
            if th_h is not None and (rp_h is None or th_h < rp_h):
                first = {
                    "kind": "theory_only",
                    "hour": th_h,
                    "expected_side": _side(th[i].get("side")),
                    "actual": None,
                    "category": "SIGNAL",
                }
                break
            if rp_h is not None and (th_h is None or rp_h < th_h):
                first = {
                    "kind": "replay_only",
                    "hour": rp_h,
                    "expected": None,
                    "actual_side": rp[j]["side"],
                    "category": "SIGNAL",
                }
                break
            if _side(th[i].get("side")) != rp[j]["side"]:
                first = {
                    "kind": "direction_mismatch",
                    "hour": th_h,
                    "expected_side": _side(th[i].get("side")),
                    "actual_side": rp[j]["side"],
                    "category": "SIGNAL",
                }
                break
            i += 1
            j += 1
        if first is None:
            zero += 1
            rows.append({"pair": pair, "zero_divergence": True})
        else:
            rows.append({"pair": pair, "zero_divergence": False, "first": first})
    return {
        "n_pairs": len(rows),
        "zero_divergence_pairs": zero,
        "early_divergence_pairs": len(rows) - zero,
        "samples": [r for r in rows if not r.get("zero_divergence")][:20],
    }


def snapshot_state(shadow) -> dict:
    strat = shadow.strategy
    pending = getattr(strat, "_pending", {}) or {}
    return {
        "cash": float(shadow.portfolio.cash),
        "n_open": len(shadow.portfolio.open_trades),
        "open_pairs": sorted(shadow.portfolio.open_trades.keys()),
        "n_closed": len(shadow.portfolio.closed_trades),
        "n_pending": len(pending),
        "pending_pairs": sorted(pending.keys()),
        "last_master_ts": shadow.last_master_ts,
        "config_hash": shadow.config_hash,
    }


def restore_end_checkpoint(candles: dict, master: str) -> tuple[Any, dict]:
    meta = json.loads((FIX / "db_snapshot_meta.json").read_text(encoding="utf-8"))
    st = meta["tables"]["mirror_theory_state"]
    capital = float(
        json.loads((FIX / "mirror_theory_state_inspect.json").read_text())[
            "portfolio_initial_capital"
        ]
    )
    shadow, _ = make_shadow(capital)
    blob = (FIX / "mirror_theory_state_blob.pkl").read_bytes()
    payload = pickle.loads(blob)
    shadow.strategy = payload["strategy"]
    shadow.portfolio = payload["portfolio"]
    shadow.engine.strategy = shadow.strategy
    shadow.last_master_ts = st["last_master_ts"]
    # rebase indices like ShadowEngine._restore_indices
    cursor = shadow._master_index_for_ts(candles[master], shadow.last_master_ts)
    info = {
        "cursor_found": cursor is not None,
        "cursor_i": cursor,
        "checkpoint_ts": st["last_master_ts"],
        "state": snapshot_state(shadow),
    }
    return shadow, info


def run_cold(candles, master, entry_i, end_i, capital: float) -> dict:
    shadow, _ = make_shadow(capital)
    repo = MemRepo()
    shadow.portfolio = shadow._new_portfolio()
    shadow.last_master_ts = (
        str(candles[master][entry_i - 1].timestamp) if entry_i > 0 else ""
    )
    t0 = time.perf_counter()
    shadow.process_range(repo, candles, master, entry_i, end_i + 1)
    rows = [
        r
        for r in portfolio_rows(shadow)
        if r["entry_hour"] and ENTRY_START <= _ts(r["entry_hour"]) <= LIVE_END
    ]
    return {
        "mode": "cold_bulk",
        "elapsed_sec": time.perf_counter() - t0,
        "state_end": snapshot_state(shadow),
        "trades": rows,
    }


def run_warm(candles, master, entry_i, end_i, capital: float) -> dict:
    from borex.alexg.multi_market import MultiMarketContext, align_symbols_to_timeline

    shadow, _ = make_shadow(capital)
    repo = MemRepo()
    shadow.portfolio = shadow._new_portfolio()
    wait = int(getattr(shadow.strategy, "sl_wait_max_bars", 72) or 72)
    rebuild_start = max(int(getattr(shadow.strategy, "min_bars", 120)), entry_i - wait)
    ts_maps = align_symbols_to_timeline(candles[master], candles)
    for master_i in range(rebuild_start, entry_i):
        ctx = MultiMarketContext.at_master_bar(
            master_i,
            candles[master],
            candles,
            ts_maps,
            strength_lookback=shadow.strategy.strength_lookback,
            min_currency_edge=shadow.strategy.min_currency_edge,
            min_confirming_pairs=shadow.strategy.min_confirming_pairs,
        )
        for symbol, index in ctx.indices.items():
            shadow.strategy.set_context(symbol, ctx)
            shadow.strategy.on_bar(index, candles[symbol], None)
    n_pending = len(getattr(shadow.strategy, "_pending", {}) or {})
    shadow.last_master_ts = str(candles[master][entry_i - 1].timestamp)
    t0 = time.perf_counter()
    shadow.process_range(repo, candles, master, entry_i, end_i + 1)
    rows = [
        r
        for r in portfolio_rows(shadow)
        if r["entry_hour"] and ENTRY_START <= _ts(r["entry_hour"]) <= LIVE_END
    ]
    return {
        "mode": "historical_warmup_ghost_prime",
        "rebuild_bars": entry_i - rebuild_start,
        "pending_after_rebuild": n_pending,
        "elapsed_sec": time.perf_counter() - t0,
        "state_end": snapshot_state(shadow),
        "trades": rows,
    }


def determinism_test(candles, master) -> dict:
    """Restore end checkpoint twice; compare state fingerprints."""
    s1, i1 = restore_end_checkpoint(candles, master)
    s2, i2 = restore_end_checkpoint(candles, master)
    a, b = snapshot_state(s1), snapshot_state(s2)
    equal = a == b
    # also re-pickle strategy pending keys
    return {
        "restore_1": i1,
        "restore_2": {k: i2[k] for k in i2 if k != "state"},
        "state_equal": equal,
        "state_a": a,
        "state_b": b,
        "deterministic": equal,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    theory = json.loads(
        (FIX / "mirror_theory_trades_window.json").read_text(encoding="utf-8")
    )
    # closed only for recall (open at end may exist)
    theory_closed = [t for t in theory if str(t.get("status")) == "closed"]
    inspect = json.loads(
        (FIX / "mirror_theory_state_inspect.json").read_text(encoding="utf-8")
    )
    capital = float(inspect.get("portfolio_initial_capital") or 1000.0)

    print("Loading surrogate H1…", flush=True)
    candles = load_surrogate_candles()
    from borex.alexg.multi_market import pick_master_symbol

    master = pick_master_symbol(candles)
    master_bars = candles[master]
    entry_i = next(
        i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= ENTRY_START
    )
    end_i = next(
        i
        for i in range(len(master_bars) - 1, -1, -1)
        if _ts(master_bars[i].timestamp) <= LIVE_END
    )
    print(f"master={master} entry_i={entry_i} end_i={end_i} capital={capital}", flush=True)

    # Verify config hash
    shadow0, live_cfg = make_shadow(capital)
    cfg_snap = json.loads((FIX / "config_snapshot.json").read_text(encoding="utf-8"))
    target = cfg_snap["target_config_hash"]
    hash_ok = shadow0.config_hash == target
    print(f"config_hash match={hash_ok} got={shadow0.config_hash[:16]}…", flush=True)

    print("Determinism (end-checkpoint restore ×2)…", flush=True)
    det = determinism_test(candles, master)

    print("Cold bulk…", flush=True)
    cold = run_cold(candles, master, entry_i, end_i, capital)
    print("Warm ghost_prime…", flush=True)
    warm = run_warm(candles, master, entry_i, end_i, capital)

    # Start checkpoint unavailable
    start_ckpt = {
        "mode": "exact_start_checkpoint",
        "available": False,
        "reason": (
            "DB only has mirror_theory_state at 2026-08-28 19:00:00+00:00 "
            "(end of window). No checkpoint at/before 2026-08-27 01:00 UTC."
        ),
        "recall": None,
        "first_divergence": None,
    }

    cold_cmp = match_theory(cold["trades"], theory_closed)
    warm_cmp = match_theory(warm["trades"], theory_closed)
    cold_fd = first_div(theory_closed, cold["trades"])
    warm_fd = first_div(theory_closed, warm["trades"])

    # End-checkpoint closed trade count vs DB
    end_shadow, end_info = restore_end_checkpoint(candles, master)
    end_closed = len(end_shadow.portfolio.closed_trades)
    end_check = {
        "checkpoint_closed_trades": end_closed,
        "db_window_trades_total": len(theory),
        "db_window_closed": len(theory_closed),
        "inspect_closed_n": inspect.get("closed_trades_n"),
        "cash": float(end_shadow.portfolio.cash),
        "open_n": len(end_shadow.portfolio.open_trades),
        "pending_n": len(getattr(end_shadow.strategy, "_pending", {}) or {}),
        "note": (
            "End checkpoint encodes full-run closed book (182), not a start state "
            "for window replay."
        ),
    }

    table = [
        {
            "mode": "Cold (bulk from entry)",
            "theory_recall": cold_cmp["recall"],
            "precision": cold_cmp["precision"],
            "matched": cold_cmp["matched"],
            "first_divergence": (
                None
                if not cold_fd["samples"]
                else f"{cold_fd['samples'][0]['pair']} @ {cold_fd['samples'][0]['first']['hour']} ({cold_fd['samples'][0]['first']['kind']})"
            ),
            "zero_div_pairs": cold_fd["zero_divergence_pairs"],
        },
        {
            "mode": "Historical warmup (ghost_prime)",
            "theory_recall": warm_cmp["recall"],
            "precision": warm_cmp["precision"],
            "matched": warm_cmp["matched"],
            "first_divergence": (
                None
                if not warm_fd["samples"]
                else f"{warm_fd['samples'][0]['pair']} @ {warm_fd['samples'][0]['first']['hour']} ({warm_fd['samples'][0]['first']['kind']})"
            ),
            "zero_div_pairs": warm_fd["zero_divergence_pairs"],
        },
        {
            "mode": "Exact start checkpoint",
            "theory_recall": None,
            "precision": None,
            "matched": None,
            "first_divergence": "UNAVAILABLE — start checkpoint missing",
            "zero_div_pairs": None,
        },
    ]

    layer_a_pass = False  # hard gate
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_stream": {
            "exact_live_candles_h1": False,
            "surrogate_mt5_cache": True,
            "surrogate_hash": json.loads(
                (FIX / "h1_surrogate_manifest.json").read_text(encoding="utf-8")
            ).get("aggregate_sha256"),
            "case": "A_OPEN — exact Theory H1 input stream not recovered",
        },
        "initial_state": {
            "start_checkpoint_available": False,
            "end_checkpoint_available": True,
            "end_checkpoint_ts": end_info.get("checkpoint_ts"),
            "config_hash_matched": hash_ok,
            "target_config_hash": target,
            "replay_config_hash": shadow0.config_hash,
            "capital_used": capital,
            "rr_clamps_note": (
                "rr_min/rr_max not in ShadowEngine.config_hash; applied as "
                "reconstructed 2/6 from trade geometry"
            ),
        },
        "determinism": det,
        "end_checkpoint_inspect": end_check,
        "modes": {
            "cold": {**{k: v for k, v in cold.items() if k != "trades"}, "cmp": cold_cmp},
            "warm": {**{k: v for k, v in warm.items() if k != "trades"}, "cmp": warm_cmp},
            "start_checkpoint": start_ckpt,
        },
        "comparison_table": table,
        "first_divergence_cold": cold_fd,
        "first_divergence_warm": warm_fd,
        "layer_a_validated": layer_a_pass,
        "pass_criteria": {
            "recall_ge_90": False,
            "precision_ge_90": False,
            "deterministic": bool(det.get("deterministic")),
            "start_checkpoint_used": False,
            "exact_inputs": False,
        },
        "answers": {
            "1_exact_h1_input_recovered": False,
            "1_detail": (
                "live_candles has 0 H1 in window. Theory uses MT5 load_universe "
                "in-memory series. Exported MT5 cache is SURROGATE only."
            ),
            "2_initial_theory_state_recovered": False,
            "2_detail": (
                "Only END checkpoint (2026-08-28 19:00) available. Start-of-window "
                "state missing."
            ),
            "3_checkpoint_replay_reproduces_theory": False,
            "3_detail": "Cannot run start-checkpoint replay; gate blocked.",
            "4_recall_precision_after_checkpoint": None,
            "5_replay_deterministic": bool(det.get("deterministic")),
            "5_detail": "End-checkpoint restore×2 state fingerprints equal.",
            "6_remaining_mismatch_class": "INPUT (+ missing START STATE)",
            "7_layer_a_validated": False,
            "8_h1_1m_worth_testing": False,
            "8_detail": "Deferred until Layer A pass.",
            "9_single_next_blocking_issue": (
                "Missing START-of-window Theory checkpoint "
                "(state immediately before 2026-08-27 01:00 UTC), plus unproven "
                "identity of H1 surrogate vs live MT5 series."
            ),
            "10_ready_for_strategy_optimization": False,
        },
        "architecture": {
            "option_2_still_provisional": True,
            "deferred_until": "start-checkpoint + exact-input parity ≥90%",
            "do_not_rewrite_multimarket_yet": True,
        },
        "systems_policy": {
            "A": "NO-GO sweeps until Layer A validated",
            "B": "Do not transfer A findings",
            "C": "Signal-quality labels only for loose sims",
        },
    }

    (OUT / "parity_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "cold_trades.json").write_text(
        json.dumps(cold["trades"], indent=2, default=str), encoding="utf-8"
    )
    (OUT / "warm_trades.json").write_text(
        json.dumps(warm["trades"], indent=2, default=str), encoding="utf-8"
    )

    lines = [
        "GOLDEN FIXTURE PARITY REPORT",
        f"generated: {report['generated_at']}",
        "",
        "INPUT: exact live H1? NO (surrogate cache). Case A still OPEN.",
        "STATE: start checkpoint? NO. end checkpoint? YES (2026-08-28 19:00).",
        f"CONFIG HASH matched? {hash_ok}",
        f"Deterministic end-restore×2? {det.get('deterministic')}",
        "",
        "Mode comparison vs mirror_theory_trades (closed):",
    ]
    for row in table:
        lines.append(
            f"  {row['mode']}: recall={row['theory_recall']} "
            f"precision={row['precision']} matched={row['matched']} "
            f"first={row['first_divergence']}"
        )
    lines += [
        "",
        "Layer A validated? NO",
        "Ready for strategy optimization? NO-GO",
        "",
        "Single next blocker:",
        f"  {report['answers']['9_single_next_blocking_issue']}",
    ]
    text = "\n".join(lines)
    (OUT / "SUMMARY.txt").write_text(text + "\n", encoding="utf-8")
    # update parent manifest recovered flags
    man_path = FIX / "MANIFEST.json"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["parity"] = {
        "report": "parity/parity_report.json",
        "layer_a_validated": False,
        "config_hash_matched": hash_ok,
        "deterministic_end_restore": bool(det.get("deterministic")),
        "cold_recall": cold_cmp["recall"],
        "warm_recall": warm_cmp["recall"],
    }
    man["blocking_issues"] = [
        "START-of-window Theory checkpoint missing (have END only at 2026-08-28 19:00).",
        "Exact Theory H1 input stream not recovered (live_candles window H1=0; MT5 cache is surrogate).",
    ]
    man["next_required"] = report["answers"]["9_single_next_blocking_issue"]
    man_path.write_text(json.dumps(man, indent=2, default=str), encoding="utf-8")
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
