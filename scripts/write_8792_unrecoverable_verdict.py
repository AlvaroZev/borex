"""Formal 8792 recoverability verdict after exhaustive START-state / H1 search.

Does not tune strategy. Does not invent start state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = (
    ROOT
    / "data"
    / "runs"
    / "mirror_2d_8792"
    / "reconciliation"
    / "golden_fixture"
    / "recovery_search"
)
OUT.mkdir(parents=True, exist_ok=True)

VERDICT = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "window": {
        "label": "mirror_2d_8792",
        "start_utc": "2026-08-27 01:00:00+00:00",
        "end_utc": "2026-08-28 19:00:00+00:00",
    },
    "8792_REPLAYABILITY": "UNRECOVERABLE",
    "statement": (
        "8792 cannot be used as an exact replay benchmark. "
        "Artifacts are insufficient to prove checkpoint parity because both "
        "the exact input stream and the exact initial state are missing."
    ),
    "recovery": {
        "START_state_recovered": False,
        "exact_Theory_H1_input_recovered": False,
        "earlier_checkpoint_usable_for_forward_replay": False,
        "end_checkpoint_recovered": True,
        "end_checkpoint_restore_deterministic_x2": True,
        "config_hash_matched": True,
    },
    "sources_checked": [
        {
            "source": "Railway backup Postgres (DATABASE_BACKUP_URL / hayabusa)",
            "result": (
                "mirror_theory_state: single row last_master_ts=2026-08-28 19:00 UTC "
                "(END only). No rows with last_master_ts < 2026-08-27 01:00. "
                "theory_state is mt5service alexg9 — not 8792. "
                "live_candles H1 in 8792 window: 0 rows. "
                "mirror_theory_trades before window: 0."
            ),
        },
        {
            "source": "Local Postgres :5433",
            "result": "Unavailable (connection refused) during search.",
        },
        {
            "source": "mirror_events / service_events / pending_ghosts / portfolio tables",
            "result": (
                "Events and trade rows exist inside the window; none encode a "
                "full Theory state blob at/before window start."
            ),
        },
        {
            "source": "Filesystem (borex_live + borex-main) *.pkl/*.log/*.sql/*.bak Aug 20–30",
            "result": (
                "No pre-window Theory pickle. Disk hits are: END blob export, "
                "lab _candles.pkl caches, Aug 22 shadow_parity experiments, "
                "sweep logs — none are 8792 start checkpoints."
            ),
        },
        {
            "source": "Prior golden_fixture / parity reports",
            "result": "Confirm END-only checkpoint + SURROGATE H1 labeling.",
        },
        {
            "source": "Theory input path (code)",
            "result": (
                "Mirror uses load_universe → bootstrap_candles → MT5 "
                "fetch_bars(count=warmup+1) → drop forming → trim warmup. "
                "Not live_candles."
            ),
        },
    ],
    "unavailable": [
        "Theory state immediately before 2026-08-27 01:00 UTC",
        "Any earlier checkpoint that can be restored and replayed to 01:00",
        "Exact in-memory H1 series Theory consumed during the original run",
        "Bar-level engine-boundary capture for 8792",
        "Proof that today's MT5 history equals August MT5 history",
    ],
    "mt5_history_label": "SURROGATE_MT5_HISTORY",
    "mt5_history_note": (
        "Exported utc_v2 / cache H1 is labeled SURROGATE only. Identity with "
        "August Theory input is unproven (broker revisions, gaps, timezone)."
    ),
    "forbidden_next_steps": [
        "Do NOT manufacture a start state",
        "Do NOT tune replay until it matches 8792",
        "Do NOT optimize strategy / run N1 or parameter sweeps",
        "Do NOT rewrite MultiMarket based on 8792 alone",
    ],
    "required_next_step": (
        "Capture a NEW controlled golden window with engine-boundary H1 input, "
        "exact initial state, state transitions, config, broker events, and "
        "state checksums. Prove Replay A == Replay B before Layer A fidelity."
    ),
    "research_gate": {
        "Layer_A": "NOT VALIDATED",
        "strategy_optimization": "NO-GO",
        "architecture_decision": "DEFERRED",
        "Option_2": "PROVISIONAL",
        "H1_plus_1m_execution": "DEFERRED",
    },
    "prior_parity_metrics_for_context_only": {
        "cold_bulk_recall": 0.335,
        "historical_warmup_recall": 0.236,
        "note": "Incomplete evidence — not a fidelity target to fit.",
    },
}

MT5_PATH = {
    "generated_at": VERDICT["generated_at"],
    "title": "Exact Theory H1 ingest path (Mirror / ShadowEngine)",
    "path": [
        "MirrorService loads symbols",
        "borex_live.data.feed.load_universe(symbols, cfg, mt5)",
        "bootstrap_candles(sym, cfg.interval, mt5, warmup_bars=cfg.warmup_bars)",
        "When MT5 connected and not dry_run: ONLY source = mt5",
        "_from_mt5 → Mt5Client.fetch_bars(yahoo_symbol, interval, count=need+1)",
        "MT5 copy_rates_from_pos(mt5_sym, timeframe, from_pos=0, count)",
        "Timestamps: mt5_unix_to_utc(server_offset_seconds); H1 snapped via snap_h1_open",
        "Volume field = tick_volume",
        "_drop_forming_bar (remove last bar if open+interval > now UTC)",
        "_trim to warmup_bars (default LiveServiceConfig.warmup_bars=10000)",
        "Live appends via append_live_bar (fetch 3, drop forming, append if new)",
        "ShadowEngine.step_master_bar consumes candles_by_symbol at master_index",
    ],
    "not_used_for_theory_warmup": [
        "live_candles Postgres table (post-start ingest / API; excludes warmup)",
        "Dukascopy cache when MT5 connected",
        "yfinance when MT5 connected",
    ],
    "key_parameters": {
        "interval": "1h (Mirror)",
        "warmup_bars_default": 10000,
        "MIN_WARMUP_BARS": 50,
        "fetch_count": "warmup_bars + 1 (then drop forming + trim)",
        "timezone": "UTC after server_offset correction",
        "symbol_mapping": "yahoo_to_mt5 / mt5_to_yahoo via Mt5Client.ensure_symbol",
    },
    "identity_today_vs_august": {
        "status": "UNPROVEN",
        "label_if_refetched": "SURROGATE_MT5_HISTORY",
        "risks": [
            "broker historical corrections",
            "missing/weekend bars",
            "symbol-history changes",
            "timezone/server_offset drift",
            "price revisions",
        ],
    },
}


def main() -> None:
    (OUT / "8792_REPLAYABILITY.json").write_text(
        json.dumps(VERDICT, indent=2), encoding="utf-8"
    )
    (OUT / "mt5_theory_input_path.json").write_text(
        json.dumps(MT5_PATH, indent=2), encoding="utf-8"
    )
    summary = OUT / "8792_UNRECOVERABLE.txt"
    summary.write_text(
        "\n".join(
            [
                "8792_REPLAYABILITY = UNRECOVERABLE",
                "",
                VERDICT["statement"],
                "",
                "START state recovered: NO",
                "Exact Theory H1 input recovered: NO",
                "MT5 history label: SURROGATE_MT5_HISTORY",
                "",
                "Next: capture NEW golden window at ShadowEngine input boundary.",
                "Do NOT force-fit 8792. Do NOT optimize strategy.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"wrote": str(OUT), "verdict": "UNRECOVERABLE"}, indent=2))


if __name__ == "__main__":
    main()
