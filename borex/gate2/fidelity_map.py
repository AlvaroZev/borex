"""Gate 2 fidelity reconciliation helpers (read-only; no strategy/simulator edits).

CONTROLLED harness probes (g2p/g2e) are NOT Theory/alexg signals.
For those, the reference is the frozen plan / intended trigger — not Theory.

NATURAL samples use existing registry mappings (attempt_maps) and stay separate.
Never force-map broker trades to Theory events.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

# Mapping statuses (explicit; no silent closest-match)
MAP_EXACT_MATCH = "EXACT_MATCH"
MAP_TIME_SKEW = "MAPPED_WITH_TIME_SKEW"
MAP_PRICE_SKEW = "MAPPED_WITH_PRICE_SKEW"
MAP_AMBIGUOUS = "AMBIGUOUS"
MAP_UNMAPPED = "UNMAPPED"
MAP_BROKER_ONLY = "BROKER_ONLY"
MAP_THEORY_ONLY = "THEORY_ONLY"
MAP_PLAN_MATCH = "PLAN_MATCH"  # controlled: broker vs frozen plan
MAP_PLAN_PRICE_SKEW = "PLAN_WITH_PRICE_SKEW"
MAP_THEORY_NA = "THEORY_SIGNAL_NOT_APPLICABLE"

# Observation layers
LAYER_BROKER = "BROKER_OBSERVED"
LAYER_THEORY = "THEORY_EXPECTED"
LAYER_SIM = "SIMULATOR_REPLAYED"
LAYER_PLAN = "FROZEN_PLAN"
LAYER_UNRESOLVED = "UNRESOLVED"

# Discrepancy taxonomy
DISC_TIME_REPRESENTATION = "TIME_REPRESENTATION"
DISC_PRICE_SIDE = "PRICE_SIDE"
DISC_SPREAD = "SPREAD"
DISC_SLIPPAGE = "SLIPPAGE"
DISC_H1_RESOLUTION = "H1_RESOLUTION"
DISC_1M_RESOLUTION = "1M_RESOLUTION"
DISC_FIRST_TOUCH = "FIRST_TOUCH_RULE"
DISC_BOTH_WICK = "BOTH_WICK_RULE"
DISC_FORMING_BAR = "FORMING_BAR"
DISC_BROKER_EXECUTION = "BROKER_EXECUTION"
DISC_COMMISSION = "COMMISSION_COST"
DISC_SWAP = "SWAP_COST"
DISC_MAPPING_AMBIGUITY = "MAPPING_AMBIGUITY"
DISC_MISSING_DATA = "MISSING_DATA"
DISC_UNKNOWN = "UNKNOWN"
DISC_EXIT_MODEL = "EXIT_MODEL"  # scenario label vs actual exit path
DISC_NOT_SIGNAL = "NOT_SIGNAL_DRIVEN"

# Bid/ask conventions (execution model)
CONV_LONG_ENTRY = "ASK"
CONV_LONG_EXIT = "BID"
CONV_SHORT_ENTRY = "BID"
CONV_SHORT_EXIT = "ASK"
CONV_LONG_SL_TRIGGER = "BID"
CONV_LONG_TP_TRIGGER = "BID"
CONV_SHORT_SL_TRIGGER = "ASK"
CONV_SHORT_TP_TRIGGER = "ASK"


@dataclass(frozen=True)
class ExecutionConvention:
    side: str
    entry_side: str
    exit_side: str
    sl_trigger_side: str
    tp_trigger_side: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def execution_convention(side: str) -> ExecutionConvention:
    s = side.lower()
    if s in ("buy", "long"):
        return ExecutionConvention(
            side="long",
            entry_side=CONV_LONG_ENTRY,
            exit_side=CONV_LONG_EXIT,
            sl_trigger_side=CONV_LONG_SL_TRIGGER,
            tp_trigger_side=CONV_LONG_TP_TRIGGER,
        )
    return ExecutionConvention(
        side="short",
        entry_side=CONV_SHORT_ENTRY,
        exit_side=CONV_SHORT_EXIT,
        sl_trigger_side=CONV_SHORT_SL_TRIGGER,
        tp_trigger_side=CONV_SHORT_TP_TRIGGER,
    )


def pip_size_for_symbol(symbol: str) -> float:
    s = (symbol or "").upper().replace("=X", "")
    if s.endswith("JPY"):
        return 0.01
    return 0.0001


def price_delta_pips(a: float | None, b: float | None, *, symbol: str) -> float | None:
    if a is None or b is None:
        return None
    pip = pip_size_for_symbol(symbol)
    if pip <= 0:
        return None
    return (float(a) - float(b)) / pip


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    t = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def time_skew_seconds(a: str | None, b: str | None) -> float | None:
    da, db = parse_iso(a), parse_iso(b)
    if da is None or db is None:
        return None
    return (da - db).total_seconds()


def classify_plan_mapping(
    *,
    entry_delta_pips: float | None,
    exit_delta_pips: float | None,
    exact_pip_tol: float = 0.05,
    skew_pip_tol: float = 2.0,
) -> str:
    """Controlled: map broker fills to frozen plan prices (not Theory)."""
    ed = abs(entry_delta_pips) if entry_delta_pips is not None else None
    xd = abs(exit_delta_pips) if exit_delta_pips is not None else None
    if ed is None and xd is None:
        return MAP_UNMAPPED
    bad = [v for v in (ed, xd) if v is not None and v > skew_pip_tol]
    if bad:
        return MAP_PLAN_PRICE_SKEW
    near = [v for v in (ed, xd) if v is not None]
    if near and all(v <= exact_pip_tol for v in near):
        return MAP_PLAN_MATCH
    if near and all(v <= skew_pip_tol for v in near):
        return MAP_PLAN_PRICE_SKEW
    return MAP_PLAN_MATCH


def classify_theory_candidates(
    candidates: list[tuple[float, Any]],
    *,
    ambiguous_gap_sec: float = 600.0,
) -> str:
    """Natural Theory mapping status from (skew_sec, theory) candidates.

    Empty → UNMAPPED. One clear winner → EXACT_MATCH or TIME_SKEW.
    Two close candidates → AMBIGUOUS (never pick closest for cosmetics).
    """
    if not candidates:
        return MAP_UNMAPPED
    candidates = sorted(candidates, key=lambda x: x[0])
    if len(candidates) >= 2 and (candidates[1][0] - candidates[0][0]) < ambiguous_gap_sec:
        return MAP_AMBIGUOUS
    skew = candidates[0][0]
    if skew <= 1.0:
        return MAP_EXACT_MATCH
    return MAP_TIME_SKEW


def controlled_trade_row(probe: dict[str, Any], *, sample: str, source_batch: str) -> dict[str, Any]:
    """Build one human-reviewable fidelity row from a controlled probe result."""
    symbol = str(probe.get("symbol_mt5") or probe.get("symbol") or "EURUSD")
    side = str(probe.get("side") or probe.get("direction") or "")
    conv = execution_convention(side)
    ed = probe.get("entry_deal") or {}
    xd = probe.get("exit_deal") or {}

    plan_entry = probe.get("live_entry_ref") or probe.get("proposed_entry_price")
    broker_entry = ed.get("price") if ed else probe.get("actual_entry_price")
    broker_exit = xd.get("price") if xd else None
    plan_sl = probe.get("live_sl") or probe.get("proposed_sl") or probe.get("broker_sl_requested")
    plan_tp = probe.get("live_tp") or probe.get("proposed_tp") or probe.get("broker_tp_requested")

    # For market-close probes, "plan exit" is not a Theory TP/SL — mark N/A
    exit_class = probe.get("exit_trigger_classification") or probe.get("exit_reason")
    intended = probe.get("intended_trigger")  # sl/tp for g2e; None for g2p labels

    entry_d = price_delta_pips(broker_entry, plan_entry, symbol=symbol)
    # Exit vs intended level when broker SL/TP; else vs plan unavailable
    plan_exit_ref = None
    if exit_class == "BROKER_SL" or (intended == "sl" and (xd.get("reason") == 4)):
        plan_exit_ref = plan_sl
    elif exit_class == "BROKER_TP" or (intended == "tp" and (xd.get("reason") == 5)):
        plan_exit_ref = plan_tp
    exit_d = price_delta_pips(broker_exit, plan_exit_ref, symbol=symbol) if plan_exit_ref else None

    mapping = classify_plan_mapping(entry_delta_pips=entry_d, exit_delta_pips=exit_d)
    discrepancies: list[str] = [DISC_NOT_SIGNAL]
    if exit_class in ("probe_market_close", "PROBE_MARKET_CLOSE") and probe.get("scenario_id"):
        if "SL" in str(probe.get("scenario_id")) or "TP" in str(probe.get("scenario_id")):
            discrepancies.append(DISC_EXIT_MODEL)
    if entry_d is not None and abs(entry_d) > 0.05:
        discrepancies.append(DISC_SLIPPAGE if abs(entry_d) < 2 else DISC_BROKER_EXECUTION)
    if exit_d is not None and abs(exit_d) > 0.5:
        discrepancies.append(DISC_BROKER_EXECUTION)

    # Confidence: HIGH only if reconciled + classification confident when broker trigger
    recon_ok = probe.get("reconciliation_status") == "reconciled"
    cls = probe.get("exit_classification") or {}
    conf = "LOW"
    if recon_ok and mapping in (MAP_PLAN_MATCH, MAP_PLAN_PRICE_SKEW):
        if exit_class in ("BROKER_SL", "BROKER_TP") and cls.get("confident"):
            conf = "HIGH"
        elif exit_class in ("probe_market_close", "PROBE_MARKET_CLOSE"):
            conf = "HIGH"  # plumbing validated; Theory N/A is explicit
        else:
            conf = "MEDIUM"

    return {
        "sample": sample,
        "source_batch": source_batch,
        "broker_position": probe.get("position_ticket"),
        "symbol": symbol,
        "direction": conv.side,
        "broker_entry_utc": ed.get("time_utc") or probe.get("submitted_at_utc"),
        "theory_event_utc": None,
        "theory_layer": LAYER_UNRESOLVED,
        "reference_layer": LAYER_PLAN,
        "entry_time_delta_sec": None,
        "broker_entry": broker_entry,
        "theory_entry": None,
        "plan_entry": plan_entry,
        "entry_delta_pips_vs_plan": entry_d,
        "broker_exit": broker_exit,
        "theory_exit": None,
        "plan_exit_ref": plan_exit_ref,
        "exit_delta_pips_vs_plan": exit_d,
        "broker_exit_reason": exit_class,
        "theory_exit_reason": None,
        "deal_reason": xd.get("reason"),
        "simulator_result": None,
        "sim_h1_only": None,
        "sim_h1_1m": None,
        "one_m_evidence": "unavailable",
        "mapping_status": mapping,
        "theory_mapping_status": MAP_THEORY_NA,
        "discrepancy_classes": discrepancies,
        "confidence": conf,
        "execution_convention": conv.to_dict(),
        "plan_sl": plan_sl,
        "plan_tp": plan_tp,
        "actual_sl": probe.get("actual_sl"),
        "actual_tp": probe.get("actual_tp"),
        "commission": probe.get("commission"),
        "swap": probe.get("swap"),
        "gross_pnl": probe.get("gross_pnl"),
        "net_pnl": probe.get("net_pnl"),
        "scenario_id": probe.get("scenario_id"),
        "intended_trigger": intended,
        "reconciliation_status": probe.get("reconciliation_status"),
        "server_offset_seconds": probe.get("server_offset_seconds"),
        "note": (
            "Controlled harness probe — not an alexg/Theory signal. "
            "Reference is frozen plan / intended broker trigger, not Theory book."
        ),
    }


def natural_row_from_registry_match(m: dict[str, Any]) -> dict[str, Any]:
    """Convert an existing clean_natural registry mapping to the fidelity table schema."""
    symbol = str(m.get("symbol") or "")
    entry_err = m.get("entry_price_error")
    exit_err = m.get("exit_price_error")
    pip = pip_size_for_symbol(symbol)
    entry_d = (float(entry_err) / pip) if entry_err is not None and pip else None
    exit_d = (float(exit_err) / pip) if exit_err is not None and pip else None
    skew = m.get("entry_timing_skew_sec")
    if skew is None:
        mapping = MAP_UNMAPPED
    elif abs(float(skew)) <= 1.0 and (entry_d is None or abs(entry_d) <= 0.05):
        mapping = MAP_EXACT_MATCH
    elif abs(float(skew)) <= 7200:
        mapping = MAP_TIME_SKEW if abs(float(skew)) > 1.0 else MAP_PRICE_SKEW
    else:
        mapping = MAP_UNMAPPED
    if entry_d is not None and abs(entry_d) > 0.05 and mapping == MAP_EXACT_MATCH:
        mapping = MAP_PRICE_SKEW

    discs: list[str] = []
    if skew is not None and abs(float(skew)) > 60:
        discs.append(DISC_TIME_REPRESENTATION)
        discs.append(DISC_H1_RESOLUTION)
    if entry_d is not None and abs(entry_d) > 0.5:
        discs.append(DISC_PRICE_SIDE)
        discs.append(DISC_SPREAD)
    if m.get("exit_class_agree") is False:
        discs.append(DISC_FIRST_TOUCH)
    if m.get("level2_tick"):
        discs.append(DISC_1M_RESOLUTION)
    if not discs:
        discs.append(DISC_UNKNOWN)

    conf = "HIGH" if m.get("maps_clean_1_to_1") and mapping in (MAP_EXACT_MATCH, MAP_TIME_SKEW, MAP_PRICE_SKEW) else "MEDIUM"
    if m.get("level2_tick") is None:
        conf = "MEDIUM" if conf == "HIGH" else conf

    side = str(m.get("direction") or "")
    return {
        "sample": "NATURAL",
        "source_batch": m.get("source_window"),
        "broker_position": m.get("entry_ticket") or m.get("broker_id"),
        "symbol": symbol,
        "direction": side,
        "broker_entry_utc": m.get("broker_entry_timestamp_utc"),
        "theory_event_utc": m.get("signal_intent_timestamp"),
        "theory_layer": LAYER_THEORY,
        "reference_layer": LAYER_THEORY,
        "entry_time_delta_sec": skew,
        "broker_entry": m.get("broker_entry_price"),
        "theory_entry": m.get("theory_entry_price"),
        "plan_entry": None,
        "entry_delta_pips_vs_plan": None,
        "entry_delta_pips_vs_theory": entry_d,
        "broker_exit": m.get("broker_exit_price"),
        "theory_exit": m.get("theory_exit_price"),
        "plan_exit_ref": None,
        "exit_delta_pips_vs_plan": None,
        "exit_delta_pips_vs_theory": exit_d,
        "broker_exit_reason": m.get("exit_class_broker"),
        "theory_exit_reason": m.get("exit_class_theory"),
        "deal_reason": None,
        "simulator_result": None,
        "sim_h1_only": "not_replayed_in_this_report",
        "sim_h1_1m": "not_replayed_in_this_report",
        "one_m_evidence": "available" if m.get("level2_tick") else "unavailable",
        "mapping_status": mapping,
        "theory_mapping_status": mapping,
        "discrepancy_classes": discs,
        "confidence": conf,
        "execution_convention": execution_convention(side).to_dict() if side else None,
        "plan_sl": None,
        "plan_tp": None,
        "actual_sl": m.get("sl_tp_level_broker"),
        "actual_tp": None,
        "commission": m.get("commission_broker"),
        "swap": m.get("swap"),
        "gross_pnl": m.get("gross_pnl_broker"),
        "net_pnl": m.get("net_pnl_broker"),
        "net_pnl_theory": m.get("net_pnl_theory"),
        "scenario_id": None,
        "intended_trigger": None,
        "reconciliation_status": "registry_clean_natural",
        "server_offset_seconds": 10800,
        "level2_tick": m.get("level2_tick"),
        "note": "Natural alexg/Theory sample from existing Gate2 registry (no force-match).",
    }


def summarize_populations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    controlled = [r for r in rows if r.get("sample") == "CONTROLLED"]
    natural = [r for r in rows if r.get("sample") == "NATURAL"]

    def _map_counts(rs: list[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rs:
            k = str(r.get("mapping_status") or "UNKNOWN")
            out[k] = out.get(k, 0) + 1
        return out

    def _disc_counts(rs: list[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rs:
            for d in r.get("discrepancy_classes") or []:
                out[str(d)] = out.get(str(d), 0) + 1
        return out

    return {
        "controlled": {
            "n_broker_trades": len(controlled),
            "mapping_counts": _map_counts(controlled),
            "theory_mapping": MAP_THEORY_NA,
            "discrepancy_counts": _disc_counts(controlled),
            "broker_sl_rows": sum(1 for r in controlled if r.get("broker_exit_reason") == "BROKER_SL"),
            "broker_tp_rows": sum(1 for r in controlled if r.get("broker_exit_reason") == "BROKER_TP"),
            "market_close_rows": sum(
                1
                for r in controlled
                if str(r.get("broker_exit_reason") or "").endswith("MARKET_CLOSE")
                or r.get("broker_exit_reason") == "probe_market_close"
            ),
        },
        "natural": {
            "n_mapped_rows_in_report": len(natural),
            "mapping_counts": _map_counts(natural),
            "discrepancy_counts": _disc_counts(natural),
        },
    }
