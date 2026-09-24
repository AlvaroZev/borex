"""Safety helpers for Gate 2 micro fidelity (Mirror-only)."""

from __future__ import annotations

from typing import Any

from borex.gate2.micro_fidelity.config import (
    EXPECTED_MIRROR_LOGIN,
    EXPECTED_SERVER,
    FORBIDDEN_LIVE_LOGIN,
    FORBIDDEN_LIVE_MAGIC,
    MIRROR_PORT,
    PROTECTED_EURDKK_TICKETS,
    PROTECTED_SYMBOL_PREFIX,
    REQUIRED_MAGIC,
)


class MicroSafetyError(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"SAFETY FAIL: {message}")


def assert_mirror_identity(account: dict[str, Any], *, magic: int = REQUIRED_MAGIC) -> None:
    login = int(account.get("login") or 0)
    server = str(account.get("server") or "")
    client_magic = int(account.get("magic") or magic)
    if login == FORBIDDEN_LIVE_LOGIN:
        raise MicroSafetyError(f"LIVE login detected: {login}")
    if login != EXPECTED_MIRROR_LOGIN:
        raise MicroSafetyError(f"login {login} != Mirror {EXPECTED_MIRROR_LOGIN}")
    if server != EXPECTED_SERVER:
        raise MicroSafetyError(f"server {server!r} != {EXPECTED_SERVER!r}")
    if client_magic != REQUIRED_MAGIC:
        raise MicroSafetyError(f"magic {client_magic} != {REQUIRED_MAGIC}")
    if client_magic == FORBIDDEN_LIVE_MAGIC:
        raise MicroSafetyError("live magic 88001")
    port = int(account.get("port_convention") or MIRROR_PORT)
    if port != MIRROR_PORT:
        raise MicroSafetyError(f"port_convention {port} != {MIRROR_PORT}")
    if not account.get("server_offset_confident", True):
        raise MicroSafetyError("server offset not confident")


def assert_not_live_port(port: int | None) -> None:
    if port is not None and int(port) == 8791:
        raise MicroSafetyError("Live port 8791 forbidden")


def filter_experiment_positions(
    positions: list[dict[str, Any]],
    *,
    comment_prefix: str,
    magic: int = REQUIRED_MAGIC,
) -> list[dict[str, Any]]:
    out = []
    for p in positions:
        sym = str(p.get("symbol") or "").upper()
        if sym.startswith(PROTECTED_SYMBOL_PREFIX):
            continue
        if int(p.get("magic") or 0) != int(magic):
            continue
        c = str(p.get("comment") or "")
        if c.startswith(comment_prefix):
            out.append(p)
    return out


def snapshot_eurdkk_tickets(positions: list[dict[str, Any]]) -> set[int]:
    return {
        int(p["ticket"])
        for p in positions
        if str(p.get("symbol") or "").upper().startswith(PROTECTED_SYMBOL_PREFIX)
    }


def assert_protected_eurdkk_intact(
    positions: list[dict[str, Any]],
    *,
    baseline: set[int] | None = None,
) -> dict[str, Any]:
    """Fail if any baseline EURDKK ticket disappeared or if we select them.

    Known leftover tickets are documented; absence of a historical ticket is
    recorded but not a hard fail (account may have changed before this run).
    Disappearance of a ticket that was present at run start IS a hard fail.
    """
    present = snapshot_eurdkk_tickets(positions)
    expected = set(PROTECTED_EURDKK_TICKETS)
    missing_documented = sorted(expected - present)
    extra = sorted(present - expected)
    if baseline is not None:
        disappeared = sorted(set(baseline) - present)
        if disappeared:
            raise MicroSafetyError(
                f"protected EURDKK tickets disappeared during run: {disappeared}"
            )
    return {
        "documented_expected": sorted(expected),
        "present": sorted(present),
        "missing_documented": missing_documented,
        "extra_eurdkk": extra,
        "baseline": sorted(baseline) if baseline is not None else None,
        "ok": True,
    }


def reject_if_duplicate_theory(
    *,
    theory_store_has: bool,
    execution_store_has: bool,
    open_experiment_for_symbol: bool,
) -> str | None:
    """Return rejection reason or None if safe to proceed."""
    if theory_store_has and execution_store_has:
        return "theory_event_already_executed"
    if theory_store_has and not execution_store_has:
        return "theory_event_exists_without_execution_ambiguous"
    if open_experiment_for_symbol:
        return "open_experiment_position_for_symbol"
    return None


def spread_gate(spread_pips: float | None, max_spread_pips: float) -> dict[str, Any]:
    if spread_pips is None:
        return {"ok": False, "rejection_reason": "spread_unknown"}
    if float(spread_pips) > float(max_spread_pips):
        return {
            "ok": False,
            "rejection_reason": f"spread_pips={spread_pips} > max={max_spread_pips}",
        }
    return {"ok": True, "rejection_reason": None}
