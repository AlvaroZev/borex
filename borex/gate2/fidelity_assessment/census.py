"""Master trade census: deduplicated natural trades + replay enrichment."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from borex.gate2.fidelity_assessment import constants as C
from borex.gate2.fidelity_replay.provenance import parse_utc
from borex.gate2.fidelity_replay.sim_exit import map_sim_reason_to_broker_label


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def dedupe_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Stable identity across multiple artifact views of the same trade."""
    return (
        str(row.get("theory_event_id") or ""),
        str(row.get("execution_id") or ""),
        str(row.get("broker_ticket") or row.get("broker_position") or ""),
    )


def floor_minute_iso(ts: str | datetime | None) -> str | None:
    t = parse_utc(ts)
    if t is None:
        return None
    floored = t.replace(second=0, microsecond=0)
    return floored.isoformat()


def classify_exit_time_agreement(
    *,
    sim_exit_time: str | None,
    broker_exit_time: str | None,
) -> dict[str, Any]:
    """Compare simulator exit candle open vs broker exit wall-clock (M1 region)."""
    if not sim_exit_time or not broker_exit_time:
        return {
            "comparable": False,
            "same_m1_candle": None,
            "different_m1_candle": None,
            "same_minute": None,
            "different_minute": None,
            "sim_exit_candle_open": floor_minute_iso(sim_exit_time) if sim_exit_time else None,
            "broker_exit_minute": floor_minute_iso(broker_exit_time) if broker_exit_time else None,
        }
    sim_m = floor_minute_iso(sim_exit_time)
    bro_m = floor_minute_iso(broker_exit_time)
    same = sim_m == bro_m
    return {
        "comparable": True,
        "same_m1_candle": same,
        "different_m1_candle": not same,
        "same_minute": same,
        "different_minute": not same,
        "sim_exit_candle_open": sim_m,
        "broker_exit_minute": bro_m,
    }


def _replay_index(replay_trades: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for t in replay_trades:
        key = (
            str(t.get("theory_event_id") or ""),
            str(t.get("execution_id") or ""),
            str(t.get("broker_ticket") or ""),
        )
        out[key] = t
    return out


def _data_quality(source_run: str, has_continuous_m1: bool) -> str:
    if has_continuous_m1:
        return "continuous_m1_archive"
    return "theory_embedded_stitch"


def _apply_ohlc_exclusion(row: dict[str, Any]) -> dict[str, Any]:
    """Reclassify the explicit AUDUSD OHLC-path boundary case."""
    ticket = str(row.get("broker_ticket") or "")
    source = str(row.get("source_run") or "")
    if ticket == C.EXCLUDED_SIM_FIDELITY_TICKET and source == C.EXCLUDED_SIM_FIDELITY_SOURCE_RUN:
        row = dict(row)
        row["replay_status"] = C.OHLC_PATH_UNRESOLVABLE
        row["discrepancy_class"] = C.OHLC_PATH_UNRESOLVABLE
        row["excluded_from_sim_fidelity"] = True
        row["exclusion_reason"] = C.EXCLUDED_SIM_FIDELITY_REASON
        row["exclusion_note"] = C.EXCLUDED_SIM_FIDELITY_NOTE
        # Do not treat as disagreement
        row["exit_event_agreement"] = None
        row["exit_event_class"] = C.OHLC_PATH_UNRESOLVABLE
    return row


def load_natural_source(
    repo_root: Path,
    source_run: str,
) -> dict[str, Any]:
    micro_dir = repo_root / "data" / "runs" / "gate2_micro_fidelity" / "runs" / source_run
    recon = _load_json(micro_dir / "trade_reconciliation.json")
    theory = _load_jsonl(micro_dir / "theory_events.jsonl")
    executions = _load_jsonl(micro_dir / "broker_executions.jsonl")
    has_m1 = (micro_dir / "m1_archive").is_dir() and any(
        (micro_dir / "m1_archive").glob("candles_*.jsonl")
    )
    replay_id = C.REPLAY_RUNS.get(source_run)
    replay_trades: list[dict[str, Any]] = []
    if replay_id:
        rp = (
            repo_root
            / "data"
            / "runs"
            / "gate2_fidelity_replay"
            / "runs"
            / replay_id
            / "trade_replay.json"
        )
        if rp.is_file():
            replay_trades = list(_load_json(rp).get("trades") or [])
    return {
        "source_run": source_run,
        "micro_dir": micro_dir,
        "recon_rows": list(recon.get("rows") or []),
        "theory_by_id": {str(t["theory_event_id"]): t for t in theory},
        "execution_by_id": {str(e.get("execution_id") or ""): e for e in executions},
        "has_continuous_m1": has_m1,
        "replay_run_id": replay_id,
        "replay_by_key": _replay_index(replay_trades),
        "evidence_weight": C.EVIDENCE_WEIGHT.get(source_run, "unknown"),
    }


def build_census_row(
    *,
    source: dict[str, Any],
    recon: dict[str, Any],
    replay: dict[str, Any] | None,
) -> dict[str, Any]:
    source_run = source["source_run"]
    teid = str(recon.get("theory_event_id") or "")
    eid = str(recon.get("execution_id") or "")
    ticket = str(recon.get("broker_position") or "")
    theory = source["theory_by_id"].get(teid) or {}
    has_m1 = bool(source["has_continuous_m1"])

    sim_exit_reason = (replay or {}).get("sim_exit_reason")
    broker_exit_reason = recon.get("broker_exit_reason")
    broker_exit_utc = recon.get("broker_exit_utc")
    sim_exit_time = (replay or {}).get("sim_exit_time")

    # Prefer reconciliation broker_exit_utc (Level 5b) over replay null.
    if (replay or {}).get("broker_exit_time"):
        broker_exit_utc = (replay or {}).get("broker_exit_time")

    data_status = (replay or {}).get("data_status")
    path_reason = ((replay or {}).get("sim_exit_detail") or {}).get("path_insufficient_reason")
    bars_eval = ((replay or {}).get("sim_exit_detail") or {}).get("bars_evaluated")
    m1_path_len = ((replay or {}).get("sim_exit_detail") or {}).get("m1_path_len")

    event_agree = (replay or {}).get("exit_event_agreement")
    if event_agree is True:
        exit_event_class = "same_event"
    elif event_agree is False:
        exit_event_class = "different_event"
    elif data_status == "REPLAYED":
        exit_event_class = "unresolved"
    else:
        exit_event_class = "unresolved"

    classes = list((replay or {}).get("discrepancy_classes") or [])
    discrepancy = classes[0] if classes else None

    time_cmp = classify_exit_time_agreement(
        sim_exit_time=sim_exit_time,
        broker_exit_time=broker_exit_utc,
    )

    row: dict[str, Any] = {
        "source_run": source_run,
        "replay_run_id": source.get("replay_run_id"),
        "evidence_weight": source["evidence_weight"],
        "experiment_class": "natural_theory",
        "theory_event_id": teid,
        "execution_id": eid,
        "broker_ticket": ticket,
        "symbol": recon.get("symbol") or theory.get("symbol"),
        "direction": recon.get("direction") or theory.get("direction"),
        "signal_time": recon.get("theory_signal_utc") or theory.get("signal_timestamp_utc"),
        "broker_entry_time": recon.get("broker_entry_utc"),
        "broker_exit_time": broker_exit_utc,
        "theory_entry": recon.get("theory_entry"),
        "sim_entry": (replay or {}).get("sim_entry"),
        "broker_entry": recon.get("broker_entry"),
        "theory_sl": recon.get("theory_sl"),
        "sim_sl": (replay or {}).get("sim_sl"),
        "broker_sl": recon.get("broker_sl"),
        "theory_tp": recon.get("theory_tp"),
        "sim_tp": (replay or {}).get("sim_tp"),
        "broker_tp": recon.get("broker_tp"),
        "sim_exit": (replay or {}).get("sim_exit"),
        "broker_exit": recon.get("broker_exit"),
        "sim_exit_reason": sim_exit_reason,
        "broker_exit_reason": broker_exit_reason,
        "sim_exit_mapped_broker_label": map_sim_reason_to_broker_label(sim_exit_reason),
        "entry_delta_pips": recon.get("entry_delta_pips"),
        "exit_delta_pips": (replay or {}).get("exit_delta_pips_sim_level_vs_broker"),
        "exit_delta_pips_theory_level_vs_broker": (replay or {}).get(
            "exit_delta_pips_theory_level_vs_broker"
        ),
        "spread_pips": recon.get("spread_pips"),
        "entry_time_delta_sec": recon.get("entry_time_delta_sec"),
        "mapping_status": recon.get("mapping_status"),
        "data_quality": _data_quality(source_run, has_m1),
        "continuous_m1_archive": has_m1,
        "replay_data_status": data_status,
        "replay_status": data_status or "NO_REPLAY_ARTIFACT",
        "path_insufficient_reason": path_reason,
        "bars_evaluated": bars_eval,
        "m1_path_len": m1_path_len,
        "exit_event_agreement": event_agree,
        "exit_event_class": exit_event_class,
        "discrepancy_class": discrepancy,
        "discrepancy_classes": classes,
        "theory_reproduction_signal_match": ((replay or {}).get("theory_reproduction") or {}).get(
            "signal_match"
        ),
        "theory_reproduction_differences": ((replay or {}).get("theory_reproduction") or {}).get(
            "differences"
        )
        or [],
        "exit_time_comparison": time_cmp,
        "excluded_from_sim_fidelity": False,
        "exclusion_reason": None,
        "exclusion_note": None,
        # Explicitly omit trade PnL from fidelity census fields used by analyzers.
        "pnl_excluded_from_fidelity": True,
    }
    return _apply_ohlc_exclusion(row)


def build_master_census(repo_root: Path | str) -> dict[str, Any]:
    root = Path(repo_root)
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    raw_count = 0

    for source_run in C.NATURAL_MICRO_RUNS:
        source = load_natural_source(root, source_run)
        for recon in source["recon_rows"]:
            raw_count += 1
            key = (
                str(recon.get("theory_event_id") or ""),
                str(recon.get("execution_id") or ""),
                str(recon.get("broker_position") or ""),
            )
            replay = source["replay_by_key"].get(key)
            # Also try ticket-only / theory-only fallbacks for older replay keys
            if replay is None:
                for rk, rt in source["replay_by_key"].items():
                    if rk[0] == key[0] or rk[2] == key[2]:
                        replay = rt
                        break
            row = build_census_row(source=source, recon=recon, replay=replay)
            if key in seen:
                duplicates.append(
                    {
                        "key": list(key),
                        "kept_source": seen[key]["source_run"],
                        "dropped_source": row["source_run"],
                    }
                )
                continue
            seen[key] = row

    trades = list(seen.values())
    trades.sort(
        key=lambda r: (
            str(r.get("source_run") or ""),
            str(r.get("signal_time") or ""),
            str(r.get("broker_ticket") or ""),
        )
    )
    for i, t in enumerate(trades, 1):
        t["census_index"] = i

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_natural_rows": raw_count,
        "deduplicated_natural_trades": len(trades),
        "duplicates_dropped": duplicates,
        "trades": trades,
    }


def census_to_csv(trades: list[dict[str, Any]]) -> str:
    fields = [
        "census_index",
        "source_run",
        "replay_run_id",
        "evidence_weight",
        "theory_event_id",
        "execution_id",
        "broker_ticket",
        "symbol",
        "direction",
        "signal_time",
        "broker_entry_time",
        "broker_exit_time",
        "theory_entry",
        "sim_entry",
        "broker_entry",
        "theory_sl",
        "sim_sl",
        "broker_sl",
        "theory_tp",
        "sim_tp",
        "broker_tp",
        "sim_exit",
        "broker_exit",
        "sim_exit_reason",
        "broker_exit_reason",
        "entry_delta_pips",
        "exit_delta_pips",
        "spread_pips",
        "data_quality",
        "replay_status",
        "exit_event_class",
        "exit_event_agreement",
        "discrepancy_class",
        "excluded_from_sim_fidelity",
        "exclusion_reason",
        "theory_reproduction_signal_match",
        "continuous_m1_archive",
        "mapping_status",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for t in trades:
        w.writerow({k: t.get(k) for k in fields})
    return buf.getvalue()


def write_census_artifacts(out_dir: Path, census: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "master_trade_census.json").write_text(
        json.dumps(census, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "master_trade_census.csv").write_text(
        census_to_csv(census["trades"]), encoding="utf-8"
    )
