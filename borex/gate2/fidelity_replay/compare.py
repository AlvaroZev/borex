"""Trade-by-trade comparison of Theory reproduction, simulator, and broker."""

from __future__ import annotations

from typing import Any

from borex.backtest.costs import infer_pip_size
from borex.gate2.fidelity_replay import taxonomy as T
from borex.gate2.fidelity_replay.provenance import (
    SourceBundle,
    parse_utc,
    post_signal_path,
)
from borex.gate2.fidelity_replay.sim_exit import (
    map_sim_reason_to_broker_label,
    simulate_entry_from_quote,
    simulate_exit_on_m1_path,
)
from borex.gate2.fidelity_replay.theory_reproduce import (
    TheoryReproduction,
    reproduce_theory_event,
)
from borex.gate2.micro_fidelity.config import FROZEN_CONFIG
from borex.gate2.micro_fidelity.strategy import plan_symmetric_sl_tp


def _digits(symbol: str) -> int:
    return 3 if "JPY" in symbol.upper().replace("=X", "") else 5


def _price_delta_pips(symbol: str, a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    pip = infer_pip_size(symbol)
    if pip <= 0:
        return None
    return (float(b) - float(a)) / pip


def _abs_pips(symbol: str, a: float | None, b: float | None) -> float | None:
    d = _price_delta_pips(symbol, a, b)
    return abs(d) if d is not None else None


def classify_trade(
    *,
    theory_ok: bool,
    theory_diffs: list[str],
    sim_status: str,
    sim_reason: str | None,
    broker_reason: str | None,
    both_wick: bool,
    entry_delta_theory_broker: float | None,
    entry_delta_theory_sim: float | None,
    exit_delta_sim_broker: float | None,
    path_insufficient: bool,
    look_ahead: bool,
) -> list[str]:
    classes: list[str] = []
    if look_ahead:
        classes.append(T.UNCLASSIFIED)
        return classes
    if path_insufficient or sim_status in ("INSUFFICIENT_PATH", "NO_HIT_IN_PATH"):
        classes.append(T.INSUFFICIENT_SOURCE_DATA)
        classes.append(T.MISSING_1M_DATA)
        return classes
    if not theory_ok:
        classes.append(T.THEORY_REPLAY_DIFFERENCE)
    if entry_delta_theory_sim is not None and abs(entry_delta_theory_sim) > 1e-9:
        classes.append(T.PRICE_SIDE_DIFFERENCE)
        classes.append(T.ENTRY_PRICE_DIFFERENCE)
    if entry_delta_theory_broker is not None and abs(entry_delta_theory_broker) > 0.05:
        # broker vs theory entry already measured in Level 4; keep if material
        if T.ENTRY_PRICE_DIFFERENCE not in classes:
            classes.append(T.ENTRY_PRICE_DIFFERENCE)

    broker_mapped = broker_reason
    sim_mapped = map_sim_reason_to_broker_label(sim_reason)
    if both_wick and broker_mapped and sim_mapped and broker_mapped != sim_mapped:
        classes.append(T.BOTH_WICK_AMBIGUITY)
    elif sim_mapped and broker_mapped and sim_mapped != broker_mapped:
        if sim_mapped == "BROKER_SL":
            classes.append(T.SL_TRIGGER_MODEL_DIFFERENCE)
        else:
            classes.append(T.TP_TRIGGER_MODEL_DIFFERENCE)
        classes.append(T.CANDLE_RESOLUTION_LIMIT)

    if (
        exit_delta_sim_broker is not None
        and abs(exit_delta_sim_broker) > 0.5
        and sim_mapped
        and broker_mapped
        and sim_mapped == broker_mapped
    ):
        # Same exit side but material price gap on 1m level fill vs broker deal
        classes.append(T.CANDLE_RESOLUTION_LIMIT)
        if abs(exit_delta_sim_broker) >= 1.0:
            classes.append(T.BROKER_EXECUTION_VARIANCE)

    if not classes:
        classes.append(T.MATCH)
    return classes


def compare_one_trade(
    bundle: SourceBundle,
    row: dict[str, Any],
    *,
    census_index: int,
) -> dict[str, Any]:
    teid = str(row.get("theory_event_id") or "")
    theory = bundle.theory_by_id.get(teid)
    execution = bundle.execution_by_theory_id.get(teid) or {}
    if theory is None:
        return {
            "census_index": census_index,
            "theory_event_id": teid,
            "execution_id": row.get("execution_id"),
            "broker_ticket": row.get("broker_position"),
            "data_status": "INSUFFICIENT_SOURCE_DATA",
            "discrepancy_classes": [T.INSUFFICIENT_SOURCE_DATA],
            "error": "theory_event_missing",
        }

    repro: TheoryReproduction = reproduce_theory_event(theory, config=FROZEN_CONFIG)
    symbol = str(theory.get("symbol") or row.get("symbol") or "")
    direction = str(theory.get("direction") or row.get("direction") or "")
    signal_ts = parse_utc(theory.get("signal_timestamp_utc"))
    assert signal_ts is not None

    bid = float(theory.get("bid") or 0.0)
    ask = float(theory.get("ask") or 0.0)
    spread_pips = float(theory.get("spread_pips") or 0.0)
    theory_entry = float(theory.get("expected_entry") or 0.0)
    theory_sl = float(theory.get("expected_sl") or 0.0)
    theory_tp = float(theory.get("expected_tp") or 0.0)

    sim_entry_res = simulate_entry_from_quote(
        symbol=symbol,
        direction=direction,
        bid=bid,
        ask=ask,
        spread_pips=spread_pips,
    )
    # Simulator-planned SL/TP from sim entry + same distance as Theory
    dist = float((theory.get("distance_selection") or {}).get("distance_price") or 0.0)
    if dist <= 0:
        dist = float(FROZEN_CONFIG.sl_tp_target_pips) * infer_pip_size(symbol)
    sim_plan = plan_symmetric_sl_tp(
        side="buy" if direction == "long" else "sell",
        entry_ref=sim_entry_res.sim_entry,
        distance_price=dist,
        digits=_digits(symbol),
        rr=float(FROZEN_CONFIG.rr),
    )

    # Exit walk uses Theory SL/TP (exact levels attached to the broker order)
    bars, insuff = post_signal_path(bundle, symbol=symbol, signal_ts=signal_ts)
    # Fail closed: any missing entry-minute / gap before we can evaluate → insufficient
    # If continuous from signal, walk until hit; exhausted path without hit → insufficient
    sim_exit = simulate_exit_on_m1_path(
        symbol=symbol,
        direction=direction,
        sl=theory_sl,
        tp=theory_tp,
        bars=bars if insuff is None else [],
        path_insufficient_reason=insuff,
        apply_spread_on_exit=False,  # compare level-fill to broker deal; document separately
        spread_pips=spread_pips,
    )
    if sim_exit.status == "NO_HIT_IN_PATH":
        # Re-tag as insufficient source (cannot observe exit within persisted M1)
        sim_exit_status = "INSUFFICIENT_PATH"
        path_insufficient = True
    else:
        sim_exit_status = sim_exit.status
        path_insufficient = sim_exit.status == "INSUFFICIENT_PATH"

    broker_entry = row.get("broker_entry")
    broker_exit = row.get("broker_exit")
    broker_reason = row.get("broker_exit_reason")
    broker_sl = row.get("broker_sl")
    broker_tp = row.get("broker_tp")

    entry_delta_theory_broker = row.get("entry_delta_pips")
    if entry_delta_theory_broker is None and broker_entry is not None:
        entry_delta_theory_broker = _price_delta_pips(symbol, theory_entry, float(broker_entry))

    entry_delta_theory_sim = _price_delta_pips(symbol, theory_entry, sim_entry_res.sim_entry)
    exit_delta_sim_broker = _price_delta_pips(
        symbol,
        sim_exit.exit_price_level,
        float(broker_exit) if broker_exit is not None else None,
    )
    exit_delta_theory_sl_broker = None
    if broker_reason == "BROKER_SL" and broker_exit is not None:
        exit_delta_theory_sl_broker = _price_delta_pips(symbol, theory_sl, float(broker_exit))
    elif broker_reason == "BROKER_TP" and broker_exit is not None:
        exit_delta_theory_sl_broker = _price_delta_pips(symbol, theory_tp, float(broker_exit))

    classes = classify_trade(
        theory_ok=repro.signal_match,
        theory_diffs=repro.differences,
        sim_status=sim_exit_status,
        sim_reason=sim_exit.exit_reason,
        broker_reason=str(broker_reason) if broker_reason else None,
        both_wick=bool(sim_exit.both_wick_on_exit_bar),
        entry_delta_theory_broker=float(entry_delta_theory_broker)
        if entry_delta_theory_broker is not None
        else None,
        entry_delta_theory_sim=entry_delta_theory_sim,
        exit_delta_sim_broker=exit_delta_sim_broker,
        path_insufficient=path_insufficient,
        look_ahead=repro.used_future_candle,
    )

    event_agree = (
        map_sim_reason_to_broker_label(sim_exit.exit_reason) == broker_reason
        if sim_exit.exit_reason and broker_reason and not path_insufficient
        else None
    )

    return {
        "census_index": census_index,
        "theory_event_id": teid,
        "execution_id": row.get("execution_id") or execution.get("execution_id"),
        "broker_ticket": row.get("broker_position"),
        "symbol": symbol,
        "direction": direction,
        "data_status": "INSUFFICIENT_SOURCE_DATA" if path_insufficient else "REPLAYED",
        "look_ahead": {
            "used_future_candle": repro.used_future_candle,
            "latest_permitted_market_ts": repro.latest_permitted_market_ts,
            "source_candle_timestamp_utc": theory.get("source_candle_timestamp_utc"),
        },
        "theory_reproduction": {
            "ok": repro.ok,
            "signal_match": repro.signal_match,
            "differences": repro.differences,
            "replay_direction": repro.replay_direction,
            "replay_signal_timestamp_utc": repro.replay_signal_timestamp_utc,
            "replay_entry": repro.replay_entry,
            "replay_sl": repro.replay_sl,
            "replay_tp": repro.replay_tp,
            "detail": repro.detail,
        },
        "signal_time_theory": theory.get("signal_timestamp_utc"),
        "signal_time_replay": repro.replay_signal_timestamp_utc,
        "theory_entry": theory_entry,
        "broker_entry": broker_entry,
        "sim_entry": sim_entry_res.sim_entry,
        "sim_entry_mid": sim_entry_res.mid_price,
        "theory_sl": theory_sl,
        "broker_sl": broker_sl,
        "sim_sl": float(sim_plan["sl"]),
        "theory_tp": theory_tp,
        "broker_tp": broker_tp,
        "sim_tp": float(sim_plan["tp"]),
        "broker_exit": broker_exit,
        "sim_exit": sim_exit.exit_price_level,
        "sim_exit_filled_with_spread": sim_exit.exit_price_filled,
        "broker_exit_time": None,  # not persisted in source artifacts
        "sim_exit_time": sim_exit.exit_timestamp_utc,
        "broker_exit_reason": broker_reason,
        "sim_exit_reason": sim_exit.exit_reason,
        "exit_event_agreement": event_agree,
        "entry_delta_pips_theory_broker": entry_delta_theory_broker,
        "entry_delta_pips_theory_sim": entry_delta_theory_sim,
        "exit_delta_pips_sim_level_vs_broker": exit_delta_sim_broker,
        "exit_delta_pips_theory_level_vs_broker": exit_delta_theory_sl_broker,
        "entry_time_delta_sec": row.get("entry_time_delta_sec"),
        "exit_time_delta_sec": None,
        "spread_at_signal": spread_pips,
        "broker_trigger_distance_pips": abs(
            float(exit_delta_theory_sl_broker) if exit_delta_theory_sl_broker is not None else 0.0
        )
        if exit_delta_theory_sl_broker is not None
        else None,
        "sim_trigger_distance_pips": 0.0
        if sim_exit.exit_price_level is not None
        else None,  # level fill
        "broker_net_pnl": row.get("net_pnl"),
        "sim_net_pnl": None,  # intentionally not computed (not a PnL experiment)
        "both_wick_on_sim_exit_bar": sim_exit.both_wick_on_exit_bar,
        "sim_exit_detail": {
            "status": sim_exit_status,
            "bars_evaluated": sim_exit.bars_evaluated,
            "path_insufficient_reason": sim_exit.path_insufficient_reason
            or (insuff if path_insufficient else None),
            "detail": sim_exit.detail,
            "m1_path_len": len(bars),
            "exit_walk_uses": "theory_sl_tp_levels",
        },
        "discrepancy_classes": classes,
        "user_list_ticket_note": "census_index follows reconciliation row order (signal-time sort applied in runner)",
    }


def compare_all_trades(bundle: SourceBundle) -> list[dict[str, Any]]:
    rows = sorted(
        bundle.reconciliation_rows,
        key=lambda r: parse_utc(r.get("theory_signal_utc")) or parse_utc("1970-01-01T00:00:00+00:00"),
    )
    return [compare_one_trade(bundle, row, census_index=i) for i, row in enumerate(rows, 1)]
