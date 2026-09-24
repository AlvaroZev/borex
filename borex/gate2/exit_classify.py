"""Evidence-based broker exit classification for Gate 2 SL/TP probes.

Do not infer BROKER_SL / BROKER_TP from scenario labels.
Primary evidence: MetaTrader deal.reason (ENUM_DEAL_REASON).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# MetaTrader5 ENUM_DEAL_REASON (mirrors package constants)
DEAL_REASON_CLIENT = 0
DEAL_REASON_EXPERT = 1
DEAL_REASON_DEALER = 2
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
DEAL_REASON_SO = 6  # stop-out

EXIT_BROKER_SL = "BROKER_SL"
EXIT_BROKER_TP = "BROKER_TP"
EXIT_PROBE_MARKET_CLOSE = "PROBE_MARKET_CLOSE"
EXIT_CLEANUP_MARKET_CLOSE = "CLEANUP_MARKET_CLOSE"
EXIT_TIMEOUT = "TIMEOUT"
EXIT_UNKNOWN = "UNKNOWN"

REASON_NAME = {
    DEAL_REASON_CLIENT: "DEAL_REASON_CLIENT",
    DEAL_REASON_EXPERT: "DEAL_REASON_EXPERT",
    DEAL_REASON_DEALER: "DEAL_REASON_DEALER",
    DEAL_REASON_SL: "DEAL_REASON_SL",
    DEAL_REASON_TP: "DEAL_REASON_TP",
    DEAL_REASON_SO: "DEAL_REASON_SO",
}


@dataclass(frozen=True)
class ExitClassification:
    classification: str
    confident: bool
    evidence: list[str]
    deal_reason: int | None = None
    deal_reason_name: str | None = None
    supporting_notes: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def reason_name(reason: int | None) -> str | None:
    if reason is None:
        return None
    return REASON_NAME.get(int(reason), f"DEAL_REASON_UNKNOWN_{int(reason)}")


def classify_broker_exit(
    *,
    exit_deal: dict[str, Any] | None,
    intentional_probe_close: bool = False,
    intentional_cleanup_close: bool = False,
    timed_out: bool = False,
    position_still_open: bool = False,
    requested_sl: float | None = None,
    requested_tp: float | None = None,
    exit_price: float | None = None,
    price_tolerance: float = 0.0,
) -> ExitClassification:
    """Classify exit using broker evidence. Never use scenario labels."""
    notes: list[str] = []
    evidence: list[str] = []

    if intentional_cleanup_close:
        evidence.append("harness_flag:intentional_cleanup_close")
        return ExitClassification(
            classification=EXIT_CLEANUP_MARKET_CLOSE,
            confident=True,
            evidence=evidence,
            supporting_notes=notes or None,
        )

    if intentional_probe_close:
        evidence.append("harness_flag:intentional_probe_close")
        return ExitClassification(
            classification=EXIT_PROBE_MARKET_CLOSE,
            confident=True,
            evidence=evidence,
            supporting_notes=notes or None,
        )

    if timed_out and position_still_open:
        evidence.append("timeout_with_position_still_open")
        return ExitClassification(
            classification=EXIT_TIMEOUT,
            confident=True,
            evidence=evidence,
            supporting_notes=notes or None,
        )

    if exit_deal is None:
        if timed_out:
            evidence.append("timeout_without_exit_deal")
            return ExitClassification(
                classification=EXIT_TIMEOUT,
                confident=True,
                evidence=evidence,
            )
        evidence.append("missing_exit_deal")
        return ExitClassification(
            classification=EXIT_UNKNOWN,
            confident=False,
            evidence=evidence,
            supporting_notes=["position closed or unobserved without exit deal row"],
        )

    reason = exit_deal.get("reason")
    if reason is not None:
        reason = int(reason)
    rname = reason_name(reason)
    comment = str(exit_deal.get("comment") or "")
    price = float(exit_deal.get("price") or exit_price or 0) or None

    # Supporting (non-decisive) price proximity notes
    if price is not None and price_tolerance >= 0:
        if requested_sl is not None and abs(price - float(requested_sl)) <= float(price_tolerance):
            notes.append(f"exit_price_near_requested_sl Δ={abs(price - float(requested_sl)):.6f}")
        if requested_tp is not None and abs(price - float(requested_tp)) <= float(price_tolerance):
            notes.append(f"exit_price_near_requested_tp Δ={abs(price - float(requested_tp)):.6f}")

    if reason == DEAL_REASON_SL:
        evidence.append(f"exit_deal.reason={reason} ({rname})")
        return ExitClassification(
            classification=EXIT_BROKER_SL,
            confident=True,
            evidence=evidence,
            deal_reason=reason,
            deal_reason_name=rname,
            supporting_notes=notes or None,
        )

    if reason == DEAL_REASON_TP:
        evidence.append(f"exit_deal.reason={reason} ({rname})")
        return ExitClassification(
            classification=EXIT_BROKER_TP,
            confident=True,
            evidence=evidence,
            deal_reason=reason,
            deal_reason_name=rname,
            supporting_notes=notes or None,
        )

    if reason is not None:
        evidence.append(f"exit_deal.reason={reason} ({rname})")
    else:
        evidence.append("exit_deal.reason_missing")

    if comment:
        evidence.append(f"exit_deal.comment={comment!r}")
        if comment.startswith("borex_flat") or comment.startswith("bx_g2c_"):
            notes.append(
                "comment suggests expert/market close but reason is not SL/TP; "
                "not classified as broker trigger"
            )

    # Price proximity alone is insufficient
    if notes and reason not in (DEAL_REASON_SL, DEAL_REASON_TP):
        notes.append("price proximity is supporting only; not used as sole classifier")

    return ExitClassification(
        classification=EXIT_UNKNOWN,
        confident=False,
        evidence=evidence,
        deal_reason=reason,
        deal_reason_name=rname,
        supporting_notes=notes or None,
    )


def choose_trigger_distance(
    *,
    pip_size: float,
    min_stop_distance_price: float,
    spread_price: float,
    freeze_level_price: float = 0.0,
    max_trigger_pips: float = 8.0,
    buffer_pips: float = 0.5,
) -> dict[str, Any]:
    """Pick a controlled trigger distance; fail-closed if impractical.

    Distance must clear broker min-stop / freeze and exceed a small buffer over spread,
    but stay within max_trigger_pips so a short wait window remains feasible.
    """
    if pip_size <= 0:
        return {
            "ok": False,
            "rejection_reason": "invalid_pip_size",
            "distance_price": None,
            "distance_pips": None,
        }
    buffer = float(buffer_pips) * float(pip_size)
    need = max(
        float(min_stop_distance_price) * 1.25 if min_stop_distance_price > 0 else 0.0,
        float(freeze_level_price) * 1.25 if freeze_level_price > 0 else 0.0,
        float(spread_price) + buffer,
        2.0 * float(pip_size),
    )
    pips = need / float(pip_size)
    if pips > float(max_trigger_pips) + 1e-12:
        return {
            "ok": False,
            "rejection_reason": (
                f"required_trigger_distance_{pips:.2f}_pips_gt_max_{float(max_trigger_pips):.2f}"
            ),
            "distance_price": need,
            "distance_pips": pips,
            "min_stop_distance_price": min_stop_distance_price,
            "spread_price": spread_price,
            "freeze_level_price": freeze_level_price,
            "max_trigger_pips": max_trigger_pips,
        }
    return {
        "ok": True,
        "rejection_reason": None,
        "distance_price": need,
        "distance_pips": pips,
        "min_stop_distance_price": min_stop_distance_price,
        "spread_price": spread_price,
        "freeze_level_price": freeze_level_price,
        "max_trigger_pips": max_trigger_pips,
    }


def plan_sl_tp_levels(
    *,
    side: str,
    entry_ref: float,
    distance_price: float,
    protective_distance_price: float,
    digits: int,
    intended: str,
) -> dict[str, Any]:
    """Build SL/TP for a broker-trigger probe.

    intended: 'sl' | 'tp'
    - SL probe: tight SL at distance; far protective TP
    - TP probe: tight TP at distance; far protective SL
    """
    d = float(distance_price)
    prot = float(protective_distance_price)
    e = float(entry_ref)
    side_l = side.lower()
    if intended == "sl":
        if side_l in ("buy", "long"):
            sl, tp = round(e - d, digits), round(e + prot, digits)
            trigger_side = "bid"
        else:
            sl, tp = round(e + d, digits), round(e - prot, digits)
            trigger_side = "ask"
    elif intended == "tp":
        if side_l in ("buy", "long"):
            tp, sl = round(e + d, digits), round(e - prot, digits)
            trigger_side = "bid"
        else:
            tp, sl = round(e - d, digits), round(e + prot, digits)
            trigger_side = "ask"
    else:
        raise ValueError(f"intended must be 'sl' or 'tp', got {intended!r}")
    return {
        "side": side_l,
        "intended_trigger": intended,
        "entry_ref": e,
        "sl": sl,
        "tp": tp,
        "trigger_side": trigger_side,
        "trigger_distance_price": d,
        "protective_distance_price": prot,
    }


def quote_would_trigger(
    *,
    side: str,
    intended: str,
    sl: float,
    tp: float,
    bid: float,
    ask: float,
) -> bool:
    """Whether current bid/ask would trigger the intended broker level (correct side)."""
    side_l = side.lower()
    if intended == "sl":
        if side_l in ("buy", "long"):
            return float(bid) <= float(sl)
        return float(ask) >= float(sl)
    if intended == "tp":
        if side_l in ("buy", "long"):
            return float(bid) >= float(tp)
        return float(ask) <= float(tp)
    return False
