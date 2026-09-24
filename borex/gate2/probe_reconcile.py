"""Gate 2 controlled-probe deal reconciliation helpers (no order placement)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from borex.data.mt5_time import mt5_unix_to_utc, utc_to_mt5_request

# MetaTrader deal.entry
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
# MetaTrader deal.type
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1


def serialize_deal(d: Any, offset_seconds: int) -> dict[str, Any]:
    raw = int(getattr(d, "time", 0) or 0)
    reason_raw = getattr(d, "reason", None)
    return {
        "ticket": int(getattr(d, "ticket", 0) or 0),
        "order": int(getattr(d, "order", 0) or 0),
        "position_id": int(getattr(d, "position_id", 0) or 0),
        "symbol": str(getattr(d, "symbol", "") or ""),
        "type": int(getattr(d, "type", -1)),
        "entry": int(getattr(d, "entry", -1)),
        "volume": float(getattr(d, "volume", 0) or 0),
        "price": float(getattr(d, "price", 0) or 0),
        "profit": float(getattr(d, "profit", 0) or 0),
        "commission": float(getattr(d, "commission", 0) or 0),
        "swap": float(getattr(d, "swap", 0) or 0),
        "time_unix_raw": raw,
        "time_utc": mt5_unix_to_utc(raw, offset_seconds).isoformat() if raw else None,
        "comment": str(getattr(d, "comment", "") or ""),
        "magic": int(getattr(d, "magic", 0) or 0),
        # ENUM_DEAL_REASON: 4=SL, 5=TP (MetaTrader5.DEAL_REASON_*)
        "reason": int(reason_raw) if reason_raw is not None else None,
    }


def _entry_code(row: dict[str, Any]) -> int:
    v = row.get("entry")
    if v is None:
        return -1
    return int(v)


def validate_deal_row(
    row: dict[str, Any],
    *,
    position_ticket: int,
    symbol_mt5: str,
    magic: int,
    expected_side: str | None = None,
) -> list[str]:
    """Return list of mismatch reasons (empty => ok)."""
    reasons: list[str] = []
    if int(row.get("position_id") or 0) != int(position_ticket):
        reasons.append("position_id_mismatch")
    if str(row.get("symbol") or "") != str(symbol_mt5):
        reasons.append("symbol_mismatch")
    if int(row.get("magic") or 0) != int(magic):
        reasons.append("magic_mismatch")
    entry_code = _entry_code(row)
    if expected_side in ("buy", "long") and entry_code == DEAL_ENTRY_IN:
        if int(row.get("type") if row.get("type") is not None else -1) != DEAL_TYPE_BUY:
            reasons.append("direction_mismatch_entry_buy")
    if expected_side in ("sell", "short") and entry_code == DEAL_ENTRY_IN:
        if int(row.get("type") if row.get("type") is not None else -1) != DEAL_TYPE_SELL:
            reasons.append("direction_mismatch_entry_sell")
    return reasons


def classify_entry_exit(deals: list[dict[str, Any]]) -> tuple[dict | None, dict | None]:
    entries = [d for d in deals if _entry_code(d) == DEAL_ENTRY_IN]
    exits = [d for d in deals if _entry_code(d) == DEAL_ENTRY_OUT]
    entry = entries[-1] if entries else None
    exit_ = exits[-1] if exits else None
    return entry, exit_


def summarize_economics(deals: list[dict[str, Any]]) -> dict[str, float]:
    entry, exit_ = classify_entry_exit(deals)
    commission = sum(float(d.get("commission") or 0) for d in deals)
    swap = sum(float(d.get("swap") or 0) for d in deals)
    gross = float(exit_.get("profit") or 0) if exit_ else 0.0
    return {
        "commission": commission,
        "swap": swap,
        "gross_pnl": gross,
        "net_pnl": gross + commission + swap,
    }


def filter_deals_matching(
    rows: list[dict[str, Any]],
    *,
    position_ticket: int,
    symbol_mt5: str,
    magic: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into accepted matches vs rejected (wrong ticket/symbol/magic)."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        reasons = validate_deal_row(
            row, position_ticket=position_ticket, symbol_mt5=symbol_mt5, magic=magic
        )
        if reasons:
            rejected.append({**row, "reject_reasons": reasons})
        else:
            accepted.append(row)
    return accepted, rejected


def reconcile_position_deals(
    *,
    position_ticket: int,
    symbol_mt5: str,
    magic: int,
    offset_seconds: int,
    expected_side: str | None = None,
    primary_raw: list[Any] | None,
    supplementary_raw: list[Any] | None = None,
    require_entry_exit: bool = True,
) -> dict[str, Any]:
    """Pure reconciliation from already-fetched MT5 deal objects / None.

    primary_raw: result of history_deals_get(position=ticket) (preferred).
    supplementary_raw: optional dated window deals.
    """
    primary_rows = [serialize_deal(d, offset_seconds) for d in (primary_raw or [])]
    supp_rows = [serialize_deal(d, offset_seconds) for d in (supplementary_raw or [])]

    primary_ok, primary_rej = filter_deals_matching(
        primary_rows, position_ticket=position_ticket, symbol_mt5=symbol_mt5, magic=magic
    )
    # Direction check on primary entry
    direction_rejects: list[str] = []
    if expected_side and primary_ok:
        for row in primary_ok:
            direction_rejects.extend(
                validate_deal_row(
                    row,
                    position_ticket=position_ticket,
                    symbol_mt5=symbol_mt5,
                    magic=magic,
                    expected_side=expected_side,
                )
            )
        direction_rejects = [r for r in direction_rejects if r.startswith("direction_")]

    supp_ok, supp_rej = filter_deals_matching(
        supp_rows, position_ticket=position_ticket, symbol_mt5=symbol_mt5, magic=magic
    )

    # Prefer primary; fall back to supplementary only if primary empty
    source = "position"
    deals = primary_ok
    if not deals and supp_ok:
        source = "time_window_fallback"
        deals = supp_ok
    elif deals and supp_ok:
        source = "position_with_window_evidence"

    entry, exit_ = classify_entry_exit(deals)
    econ = summarize_economics(deals)

    if primary_raw is None and not supp_ok:
        status = "missing_history"
        fail_closed = True
        detail = "history_deals_get returned None/empty and no supplementary matches"
    elif not deals:
        status = "no_matching_deals"
        fail_closed = True
        detail = "deals present but none matched position/symbol/magic"
    elif require_entry_exit and (entry is None or exit_ is None):
        status = "incomplete_entry_exit"
        fail_closed = True
        detail = f"entry={'ok' if entry else 'missing'} exit={'ok' if exit_ else 'missing'}"
    elif direction_rejects:
        status = "direction_mismatch"
        fail_closed = True
        detail = ",".join(sorted(set(direction_rejects)))
    else:
        status = "reconciled"
        fail_closed = False
        detail = None

    return {
        "reconciliation_status": status,
        "reconciliation_fail_closed": fail_closed,
        "reconciliation_detail": detail,
        "reconciliation_source": source,
        "position_ticket": int(position_ticket),
        "deals": deals,
        "entry_deal": entry,
        "exit_deal": exit_,
        "order_ticket_entry": int(entry.get("order") or 0) if entry else None,
        "order_ticket_exit": int(exit_.get("order") or 0) if exit_ else None,
        "rejected_primary": primary_rej,
        "rejected_supplementary": supp_rej,
        "supplementary_match_count": len(supp_ok),
        "server_offset_seconds": int(offset_seconds),
        **econ,
    }


def fetch_supplementary_deals(
    mt5_api: Any,
    *,
    since_utc: datetime,
    until_utc: datetime | None,
    offset_seconds: int,
) -> list[Any]:
    """Dated window query — supplementary only."""
    now = until_utc or datetime.now(timezone.utc)
    req0 = utc_to_mt5_request(since_utc - timedelta(seconds=30), offset_seconds)
    req1 = utc_to_mt5_request(now + timedelta(seconds=5), offset_seconds)
    return list(mt5_api.history_deals_get(req0, req1) or [])


def fetch_primary_deals_by_position(mt5_api: Any, position_ticket: int) -> list[Any] | None:
    """Primary path: history_deals_get(position=...). None if API returns None."""
    got = mt5_api.history_deals_get(position=int(position_ticket))
    if got is None:
        return None
    return list(got)


def reconcile_from_mt5(
    mt5_api: Any,
    *,
    position_ticket: int,
    symbol_mt5: str,
    magic: int,
    offset_seconds: int,
    since_utc: datetime,
    expected_side: str | None = None,
    include_supplementary: bool = True,
) -> dict[str, Any]:
    primary = fetch_primary_deals_by_position(mt5_api, position_ticket)
    supplementary = None
    if include_supplementary:
        supplementary = fetch_supplementary_deals(
            mt5_api,
            since_utc=since_utc,
            until_utc=datetime.now(timezone.utc),
            offset_seconds=offset_seconds,
        )
    return reconcile_position_deals(
        position_ticket=position_ticket,
        symbol_mt5=symbol_mt5,
        magic=magic,
        offset_seconds=offset_seconds,
        expected_side=expected_side,
        primary_raw=primary,
        supplementary_raw=supplementary,
    )


# Guard used by tests: must never call order_send
def assert_no_order_api(mt5_api: Any) -> None:
    send = getattr(mt5_api, "order_send", None)
    if callable(send):
        # Wrap detection — tests use FakeMT5 without order_send or with counter
        pass
