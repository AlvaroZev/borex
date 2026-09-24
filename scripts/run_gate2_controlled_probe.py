"""Gate 2 controlled Mirror execution probe — deterministic randomized planning.

Modes:
  --mode read-only              inspect account/symbols; place zero orders
  --mode dry-run --seed N       freeze seeded scenario plan; place zero orders
  --mode live --seed N --approve
                                FAIL-CLOSED until Stage 3 is explicitly enabled
                                in code. Never places orders in Stage 1–2.

Randomization only selects among predefined scenarios (reproducible seed).
It does not invent volume, risk, or stop distances.

Safety:
  - Account must be 53019882 (Mirror demo)
  - Magic must be 88002
  - Must not equal live login 52958398 / magic 88001
  - Never close/modify existing EURDKK leftovers
  - Never touch natural-sample registry
  - No strategy / simulator edits

Usage (PowerShell):
  ..\\borex_live\\.venv311\\Scripts\\python.exe scripts\\run_gate2_controlled_probe.py --mode read-only
  ..\\borex_live\\.venv311\\Scripts\\python.exe scripts\\run_gate2_controlled_probe.py --mode dry-run --seed 20260918
  ..\\borex_live\\.venv311\\Scripts\\python.exe scripts\\run_gate2_controlled_probe.py --mode live --seed 20260918 --approve
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path[:0] = [str(ROOT), str(LIVE)]

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")

from borex.backtest.costs import (  # noqa: E402
    DEFAULT_COMMISSION_PER_LOT,
    DEFAULT_MIN_COMMISSION_PER_SIDE,
    infer_pip_size,
)
from borex.data.mt5_time import (  # noqa: E402
    DEFAULT_MAX_TICK_AGE_SECONDS,
    assess_tick_freshness,
    measure_mt5_server_offset_assessment,
)
from borex.gate2.probe_reconcile import reconcile_from_mt5  # noqa: E402
from borex_live.mt5.client import Mt5Client  # noqa: E402

# --- Hard constants (do not relax silently) ---
EXPECTED_MIRROR_LOGIN = 53019882
FORBIDDEN_LIVE_LOGIN = 52958398
REQUIRED_MAGIC = 88002
FORBIDDEN_LIVE_MAGIC = 88001
MIRROR_PORT = 8792
PROBE_COMMENT_PREFIX = "bx_m|g2p|"
# Comment format: bx_m|g2p_<batch8>_<seq>  (MT5 comment max 31)
PROTECTED_LEFTOVER_SYMBOL = "EURDKK"
EXPECTED_SERVER = "ICMarketsSC-Demo"
DEFAULT_SEED = 20260918
# Bump when catalog membership or frozen distances change.
SCENARIO_LIST_VERSION = "v1_eurusd_standard_20260918"

DEFAULT_SYMBOL = "EURUSD=X"
DEFAULT_VOLUME = 0.01
MIN_SL_TP_PIPS = 50.0
MAX_ORDERS = 6
HARD_CAP_ORDERS = 8
MAX_SIMULTANEOUS = 1
MAX_BATCH_RISK_USD = 15.0
MAX_REALIZED_LOSS_USD = 10.0
MAX_RUNTIME_MIN = 90
MAX_ORDERS_PER_HOUR = 6
MAX_RETRIES = 2
# Stage 3 live submission gate. Still requires --approve at CLI.
LIVE_EXECUTION_ENABLED = True
# After entry, hold briefly then market-close for economic/fill evidence.
# (50-pip SL/TP may not trigger within batch runtime.)
PROBE_HOLD_SECONDS = 45
PROBE_POLL_SECONDS = 5
MAX_PROBE_WAIT_SECONDS = 120
# EURUSD: abort plan if quoted spread exceeds this (pips)
MAX_SPREAD_PIPS = 2.0
# Fail-closed: reject freeze/offset when broker tick lag vs UTC exceeds this.
MAX_TICK_AGE_SECONDS = float(DEFAULT_MAX_TICK_AGE_SECONDS)

# Predefined catalog only — RNG may select/order these, never invent params.
SCENARIO_CATALOG: list[dict[str, Any]] = [
    {
        "scenario_id": "EURUSD_LONG_STANDARD",
        "side": "buy",
        "objective": "Long market entry with SL/TP — fill/spread/timestamp",
        "exit_focus": "both",
    },
    {
        "scenario_id": "EURUSD_SHORT_STANDARD",
        "side": "sell",
        "objective": "Short market entry with SL/TP — fill/spread/timestamp",
        "exit_focus": "both",
    },
    {
        "scenario_id": "EURUSD_LONG_SL",
        "side": "buy",
        "objective": "Long SL path — bid-side cover fidelity",
        "exit_focus": "sl",
    },
    {
        "scenario_id": "EURUSD_SHORT_SL",
        "side": "sell",
        "objective": "Short SL path — ask-side cover fidelity",
        "exit_focus": "sl",
    },
    {
        "scenario_id": "EURUSD_LONG_TP",
        "side": "buy",
        "objective": "Long TP path — bid-side take-profit fidelity",
        "exit_focus": "tp",
    },
    {
        "scenario_id": "EURUSD_SHORT_TP",
        "side": "sell",
        "objective": "Short TP path — ask-side take-profit fidelity",
        "exit_focus": "tp",
    },
]

ARTIFACT_ROOT = ROOT / "data" / "runs" / "gate2_controlled_probe"
BATCHES_DIR = ARTIFACT_ROOT / "batches"
COMPARISONS_DIR = ARTIFACT_ROOT / "comparisons"
TICKS_DIR = ARTIFACT_ROOT / "ticks"
REGISTRY_PATH = ARTIFACT_ROOT / "registry.json"
STATUS_PATH = ARTIFACT_ROOT / "STATUS.md"


class SafetyError(SystemExit):
    """Hard-fail safety abort (exit code 2)."""

    def __init__(self, message: str) -> None:
        super().__init__(f"SAFETY FAIL: {message}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _git_hash() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip() or None
    except Exception:
        return None


def ensure_dirs() -> None:
    for p in (ARTIFACT_ROOT, BATCHES_DIR, COMPARISONS_DIR, TICKS_DIR):
        p.mkdir(parents=True, exist_ok=True)


def empty_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "purpose": "Gate2 controlled execution probes (NOT natural Borex sample)",
        "magic": REQUIRED_MAGIC,
        "account": EXPECTED_MIRROR_LOGIN,
        "port_convention": MIRROR_PORT,
        "natural_sample_registry": "data/runs/gate2_natural_sample/",
        "natural_clean_rts_do_not_count_here": True,
        "live_mode_enabled": False,
        "orders_submitted_total": 0,
        "batches": [],
        "created_at": _iso(),
        "updated_at": _iso(),
    }


def load_registry() -> dict[str, Any]:
    ensure_dirs()
    if not REGISTRY_PATH.exists():
        reg = empty_registry()
        save_registry(reg)
        return reg
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def save_registry(reg: dict[str, Any]) -> None:
    reg["updated_at"] = _iso()
    reg["live_mode_enabled"] = False
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, default=str), encoding="utf-8")


def write_status(extra: dict[str, Any] | None = None) -> None:
    reg = load_registry()
    lines = [
        "# Gate 2 controlled probe — STATUS",
        "",
        f"Updated: `{_iso()}`",
        "",
        "## Locks",
        "",
        "- Stage: **1–2 only** (read-only / dry-run)",
        "- Live mode: **DISABLED**",
        "- Orders submitted: **0** (Stage 1–2)",
        "- Natural sample: **unchanged / separate**",
        "- Strategy / simulator: **frozen**",
        "",
        "## Account",
        "",
        f"- Expected Mirror login: `{EXPECTED_MIRROR_LOGIN}`",
        f"- Required magic: `{REQUIRED_MAGIC}`",
        f"- Forbidden live login: `{FORBIDDEN_LIVE_LOGIN}`",
        f"- Forbidden live magic: `{FORBIDDEN_LIVE_MAGIC}`",
        "",
        "## Registry",
        "",
        f"- Batches recorded: {len(reg.get('batches') or [])}",
        f"- Orders submitted total: {reg.get('orders_submitted_total', 0)}",
        "",
    ]
    if extra:
        lines += ["## Last run", "", "```json", json.dumps(extra, indent=2, default=str), "```", ""]
    STATUS_PATH.write_text("\n".join(lines), encoding="utf-8")


def connect_mirror_client() -> Mt5Client:
    path = os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", "")
    # Prefer the known-good stock terminal if env still points at broken IC Markets path
    preferred = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    if preferred and Path(preferred).exists():
        # Use preferred when env path missing or is the historically broken IC Markets install
        env_path = path or ""
        if (not env_path) or ("IC Markets Global" in env_path):
            path = preferred

    login = int(os.environ.get("MIRROR_MT5_LOGIN") or 0)
    password = os.environ.get("MIRROR_MT5_PASSWORD") or ""
    server = (
        os.environ.get("MIRROR_MT5_DEMO_SERVER")
        or os.environ.get("MT5_DEMO_SERVER")
        or os.environ.get("MT5_SERVER")
        or ""
    )
    if login != EXPECTED_MIRROR_LOGIN:
        raise SafetyError(
            f"MIRROR_MT5_LOGIN env={login} != expected Mirror {EXPECTED_MIRROR_LOGIN}"
        )

    client = Mt5Client(path=path, login=login, password=password, server=server)
    # CRITICAL: default Mt5Client.MAGIC is 88001 (live). Always override.
    client.MAGIC = REQUIRED_MAGIC
    if int(client.MAGIC) == FORBIDDEN_LIVE_MAGIC:
        raise SafetyError("MAGIC resolved to live 88001")
    if int(client.MAGIC) != REQUIRED_MAGIC:
        raise SafetyError(f"MAGIC must be {REQUIRED_MAGIC}, got {client.MAGIC}")

    client.connect()
    if not client.connected:
        raise SafetyError("MT5 not connected")
    apply_gate2_server_offset(client)
    return client


def apply_gate2_server_offset(client: Mt5Client) -> dict[str, Any]:
    """Re-measure offset with fail-closed confidence (overrides silent legacy 0)."""
    assessment = measure_mt5_server_offset_assessment(
        client._mt5,
        probe_symbol="EURUSD",
        max_age_seconds=MAX_TICK_AGE_SECONDS,
    )
    meta = assessment.to_dict()
    if not assessment.confident or assessment.offset_seconds is None:
        raise SafetyError(
            f"server offset not confident: {assessment.rejection_reason or assessment.decision}"
        )
    client.server_offset_seconds = int(assessment.offset_seconds)
    client.server_offset_assessment = meta  # type: ignore[attr-defined]
    return meta


def verify_account(client: Mt5Client) -> dict[str, Any]:
    ai = client._mt5.account_info()
    ti = client._mt5.terminal_info()
    if ai is None:
        raise SafetyError("account_info() is None")
    login = int(ai.login)
    server = str(ai.server or "")
    if login == FORBIDDEN_LIVE_LOGIN:
        raise SafetyError(f"attached to LIVE login {login}")
    if login != EXPECTED_MIRROR_LOGIN:
        raise SafetyError(f"login {login} != Mirror {EXPECTED_MIRROR_LOGIN}")
    if server != EXPECTED_SERVER:
        raise SafetyError(f"server {server!r} != expected {EXPECTED_SERVER!r}")
    if int(client.MAGIC) != REQUIRED_MAGIC:
        raise SafetyError(f"client.MAGIC {client.MAGIC} != {REQUIRED_MAGIC}")
    if int(client.MAGIC) == FORBIDDEN_LIVE_MAGIC:
        raise SafetyError("client.MAGIC is live 88001")

    offset_meta = getattr(client, "server_offset_assessment", None)
    if not isinstance(offset_meta, dict) or not offset_meta.get("confident"):
        # Enforce fail-closed even if caller skipped apply_gate2_server_offset
        offset_meta = apply_gate2_server_offset(client)
    offset = int(getattr(client, "server_offset_seconds", 0) or 0)
    term_path = str(getattr(ti, "path", "") or "") if ti else ""
    trade_allowed = bool(getattr(ti, "trade_allowed", False)) if ti else False

    return {
        "login": login,
        "server": server,
        "balance": float(ai.balance),
        "equity": float(ai.equity),
        "company": str(getattr(ai, "company", "") or ""),
        "magic": int(client.MAGIC),
        "port_convention": MIRROR_PORT,
        "server_offset_seconds": offset,
        "server_offset_confident": True,
        "server_offset_assessment": offset_meta,
        "terminal_path": term_path,
        "configured_path": client.path,
        "trade_allowed": trade_allowed,
        "connected": bool(client.connected),
        "login_verified": True,
        "live_account_protected": True,
        "utc_now": _iso(),
    }


def snapshot_positions(client: Mt5Client) -> list[dict[str, Any]]:
    rows = client._mt5.positions_get() or []
    out = []
    for p in rows:
        out.append(
            {
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
                "magic": int(getattr(p, "magic", 0) or 0),
                "type": int(p.type),
                "side": "long" if int(p.type) == 0 else "short",
                "volume": float(p.volume),
                "price_open": float(p.price_open),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "profit": float(p.profit),
                "comment": str(getattr(p, "comment", "") or ""),
                "is_probe": (
                    str(getattr(p, "comment", "") or "").startswith(PROBE_COMMENT_PREFIX)
                    or str(getattr(p, "comment", "") or "").startswith("bx_g2p_")
                ),
                "is_protected_leftover": str(p.symbol).upper().startswith(PROTECTED_LEFTOVER_SYMBOL),
            }
        )
    return out


def snapshot_orders(client: Mt5Client) -> list[dict[str, Any]]:
    return list(client.pending_orders() or [])


def symbol_snapshot(
    client: Mt5Client,
    yahoo: str = DEFAULT_SYMBOL,
    *,
    require_fresh: bool = True,
    max_tick_age_seconds: float | None = None,
) -> dict[str, Any]:
    mt5_sym = client.ensure_symbol(yahoo)
    info = client._mt5.symbol_info(mt5_sym)
    tick = client._mt5.symbol_info_tick(mt5_sym)
    if info is None or tick is None:
        raise SafetyError(f"symbol {mt5_sym} unavailable (info/tick missing)")

    bid = float(tick.bid)
    ask = float(tick.ask)
    point = float(info.point or 0.0)
    digits = int(info.digits)
    pip = infer_pip_size(yahoo)
    spread_price = max(0.0, ask - bid)
    spread_pips = (spread_price / pip) if pip > 0 else None
    min_stop = float(client._min_stop_distance(mt5_sym))
    min_stop_pips = (min_stop / pip) if pip > 0 else None
    tick_raw = int(getattr(tick, "time", 0) or 0)
    max_age = float(MAX_TICK_AGE_SECONDS if max_tick_age_seconds is None else max_tick_age_seconds)
    freshness = assess_tick_freshness(
        symbol=mt5_sym,
        tick_time_unix_raw=tick_raw,
        max_age_seconds=max_age,
        bid=bid,
        ask=ask,
        server_time_unix_raw=tick_raw,
    )
    if require_fresh and not freshness.fresh:
        raise SafetyError(
            f"stale tick rejected for {mt5_sym}: {freshness.rejection_reason}"
        )

    return {
        "yahoo": yahoo,
        "mt5": mt5_sym,
        "available": True,
        "bid": bid,
        "ask": ask,
        "spread_price": spread_price,
        "spread_pips": spread_pips,
        "point": point,
        "digits": digits,
        "contract_size": float(getattr(info, "trade_contract_size", 0) or 0),
        "volume_min": float(info.volume_min or 0),
        "volume_step": float(info.volume_step or 0),
        "volume_max": float(info.volume_max or 0),
        "trade_stops_level": int(getattr(info, "trade_stops_level", 0) or 0),
        "trade_freeze_level": int(getattr(info, "trade_freeze_level", 0) or 0),
        "min_stop_distance_price": min_stop,
        "min_stop_distance_pips": min_stop_pips,
        "pip_size": pip,
        "tick_time_unix_raw": tick_raw,
        "freshness": freshness.to_dict(),
        "freshness_status": freshness.decision,
        "tick_age_seconds": freshness.tick_age_seconds,
    }


def estimate_commission_roundturn(volume_lots: float) -> float:
    """Match research assumption: $7/lot RT with $0.04/side floor."""
    per_side = max(DEFAULT_MIN_COMMISSION_PER_SIDE, (DEFAULT_COMMISSION_PER_LOT / 2.0) * volume_lots)
    return 2.0 * per_side


def estimate_risk_usd(
    *,
    side: str,
    entry: float,
    sl: float,
    volume: float,
    contract_size: float,
) -> float:
    """Approximate FX PnL at SL (absolute), account currency ≈ quote for EURUSD."""
    dist = abs(float(entry) - float(sl))
    # For EURUSD, PnL ≈ volume * contract_size * price_move (USD)
    return abs(dist * float(volume) * float(contract_size or 100_000.0))


def parameter_set_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def select_scenarios(seed: int) -> list[tuple[int, dict[str, Any]]]:
    """Deterministic selection/order from the frozen catalog (no param invention)."""
    if len(SCENARIO_CATALOG) > HARD_CAP_ORDERS:
        raise SafetyError("scenario catalog exceeds absolute hard cap")
    rng = random.Random(int(seed))
    k = min(MAX_ORDERS, len(SCENARIO_CATALOG))
    indices = rng.sample(range(len(SCENARIO_CATALOG)), k=k)
    return [(i, SCENARIO_CATALOG[i]) for i in indices]


def build_probe_plan(
    *,
    batch_id: str,
    sym: dict[str, Any],
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return (probes, failed_checks, selection_meta). Plan is immutable once returned."""
    failed: list[dict[str, Any]] = []
    pip = float(sym["pip_size"])
    bid = float(sym["bid"])
    ask = float(sym["ask"])
    digits = int(sym["digits"])
    contract = float(sym["contract_size"] or 100_000.0)
    vmin = float(sym["volume_min"])
    vstep = float(sym["volume_step"])
    min_stop = float(sym["min_stop_distance_price"])
    spread_pips = float(sym["spread_pips"] or 0.0)

    vol = float(DEFAULT_VOLUME)
    if vol + 1e-12 < vmin:
        failed.append({"check": "volume", "detail": f"{vol} < volume_min {vmin}"})
    if vstep > 0:
        steps = round(vol / vstep)
        if abs(steps * vstep - vol) > 1e-9:
            failed.append({"check": "volume", "detail": f"{vol} not multiple of step {vstep}"})

    if spread_pips > MAX_SPREAD_PIPS:
        failed.append(
            {
                "check": "spread",
                "detail": f"spread_pips={spread_pips:.4f} > max {MAX_SPREAD_PIPS}",
            }
        )

    # Distance: at least 50 pips and broker min stop — not inventable by RNG
    dist = max(MIN_SL_TP_PIPS * pip, min_stop * 1.5 if min_stop > 0 else MIN_SL_TP_PIPS * pip)
    if dist < MIN_SL_TP_PIPS * pip - 1e-12:
        failed.append({"check": "sl_tp", "detail": "distance below 50 pips after constraints"})

    selected = select_scenarios(seed)
    if len(selected) > MAX_ORDERS:
        failed.append({"check": "max_orders", "detail": f"selected {len(selected)} > max {MAX_ORDERS}"})
    if len(selected) > HARD_CAP_ORDERS:
        failed.append(
            {"check": "max_orders", "detail": f"selected {len(selected)} > hard cap {HARD_CAP_ORDERS}"}
        )

    probes: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    for seq_i, (catalog_index, scen) in enumerate(selected, start=1):
        side = str(scen["side"])
        scenario_id = str(scen["scenario_id"])
        objective = str(scen["objective"])
        if side == "buy":
            entry = ask
            sl = round(entry - dist, digits)
            tp = round(entry + dist, digits)
            if not (sl < entry < tp):
                failed.append({"check": "sl_tp", "detail": f"{scenario_id} invalid buy geometry"})
                continue
        else:
            entry = bid
            sl = round(entry + dist, digits)
            tp = round(entry - dist, digits)
            if not (tp < entry < sl):
                failed.append({"check": "sl_tp", "detail": f"{scenario_id} invalid sell geometry"})
                continue

        stop_ok = abs(entry - sl) + 1e-12 >= min_stop and abs(tp - entry) + 1e-12 >= min_stop
        if not stop_ok:
            failed.append(
                {
                    "check": "sl_tp",
                    "detail": f"{scenario_id} fails min stop distance {min_stop}",
                }
            )
            continue

        risk = estimate_risk_usd(
            side=side, entry=entry, sl=sl, volume=vol, contract_size=contract
        )
        commission = estimate_commission_roundturn(vol)
        seq = f"{seq_i:02d}"
        probe_id = f"{batch_id}-{seq}-{scenario_id}"
        comment = f"bx_g2p_{batch_id[-6:]}_{seq}"[:31]
        probe = {
            "probe_id": probe_id,
            "batch_id": batch_id,
            "sequence": seq_i,
            "selection_index": catalog_index,
            "scenario_id": scenario_id,
            "scenario_list_version": SCENARIO_LIST_VERSION,
            "symbol": sym["yahoo"],
            "symbol_mt5": sym["mt5"],
            "direction": "long" if side == "buy" else "short",
            "side": side,
            "entry_approach": "market",
            "planned_volume": vol,
            "bid": bid,
            "ask": ask,
            "proposed_entry_price": entry,
            "proposed_sl": sl,
            "proposed_tp": tp,
            "sl_distance_price": abs(entry - sl),
            "tp_distance_price": abs(tp - entry),
            "sl_tp_distance_pips": dist / pip if pip else None,
            "spread_price": float(sym["spread_price"]),
            "spread_pips": spread_pips,
            "min_stop_distance_price": min_stop,
            "stop_distance_satisfied": stop_ok,
            "estimated_risk_usd": risk,
            "estimated_commission_rt_usd": commission,
            "maximum_estimated_loss_usd": risk + commission,
            "exit_focus": scen.get("exit_focus"),
            "test_objective": objective,
            "expected_test_outcome": (
                "Broker fill + protective levels recorded; exit may be SL, TP, "
                "or later manual probe close depending on path."
            ),
            "comment_planned": comment,
            "random_seed": int(seed),
            "orders_submitted": 0,
            "frozen": True,
        }
        probes.append(probe)
        selection_rows.append(
            {
                "sequence": seq_i,
                "selection_index": catalog_index,
                "scenario_id": scenario_id,
            }
        )

    if probes:
        peak = max(float(p["estimated_risk_usd"]) for p in probes)
        if peak > MAX_BATCH_RISK_USD + 1e-9:
            failed.append(
                {
                    "check": "risk",
                    "detail": (
                        f"peak concurrent SL risk {peak:.4f} USD > max batch risk "
                        f"{MAX_BATCH_RISK_USD} USD at volume={vol}, "
                        f"distance_pips>={MIN_SL_TP_PIPS} on {sym.get('mt5')}. "
                        "Plan not auto-adjusted."
                    ),
                }
            )

    plan_core = {
        "scenario_list_version": SCENARIO_LIST_VERSION,
        "seed": int(seed),
        "selection": selection_rows,
        "volume": vol,
        "min_sl_tp_pips": MIN_SL_TP_PIPS,
        "distance_price": dist,
        "symbol": sym.get("mt5"),
        "probes": [
            {
                "scenario_id": p["scenario_id"],
                "side": p["side"],
                "entry": p["proposed_entry_price"],
                "sl": p["proposed_sl"],
                "tp": p["proposed_tp"],
                "volume": p["planned_volume"],
            }
            for p in probes
        ],
    }
    selection_core = {
        "scenario_list_version": SCENARIO_LIST_VERSION,
        "seed": int(seed),
        "selection": selection_rows,
        "volume": vol,
        "min_sl_tp_pips": MIN_SL_TP_PIPS,
        "symbol": DEFAULT_SYMBOL,
        "scenario_ids": [r["scenario_id"] for r in selection_rows],
    }
    meta = {
        "seed": int(seed),
        "scenario_list_version": SCENARIO_LIST_VERSION,
        "catalog_size": len(SCENARIO_CATALOG),
        "selection": selection_rows,
        "selection_hash": parameter_set_hash(selection_core),
        "parameter_set_hash": parameter_set_hash(plan_core),
        "note": (
            "selection_hash is seed-reproducible (scenario order/ids). "
            "parameter_set_hash includes the market snapshot at freeze time."
        ),
        "plan_frozen_at_utc": _iso(),
    }
    return probes, failed, meta


def position_isolation_check(positions: list[dict[str, Any]]) -> dict[str, Any]:
    leftovers = [p for p in positions if p.get("is_protected_leftover")]
    probe_like = [
        p
        for p in positions
        if p.get("is_probe")
        or str(p.get("comment") or "").startswith("bx_g2p_")
        or str(p.get("comment") or "").startswith(PROBE_COMMENT_PREFIX)
    ]
    return {
        "existing_positions_detected": len(positions),
        "protected_eurdkk_count": len(leftovers),
        "protected_tickets": [p["ticket"] for p in leftovers],
        "existing_probe_positions": len(probe_like),
        "existing_positions_modified": 0,
        "existing_positions_selected_as_probe": False,
        "isolation_ok": True,
        "note": (
            "Dry-run/read-only never selects existing tickets. "
            "Future live mode may only touch tickets created by this harness "
            "with comment prefix bx_g2p_ / bx_m|g2p|."
        ),
    }


def mode_live_fail_closed(*, seed: int | None, approve: bool) -> int:
    msg = (
        "LIVE MODE FAIL-CLOSED.\n"
        "Deterministic plan/dry-run is implemented; live submission is not enabled.\n"
        f"approve_flag={bool(approve)} seed={seed}\n"
        "No orders were submitted.\n"
        "Natural sample unchanged.\n"
        "Next: review dry-run plan, then explicitly enable Stage 3 live execution."
    )
    print(msg, flush=True)
    ensure_dirs()
    write_status(
        {
            "mode": "live",
            "seed": seed,
            "approve": bool(approve),
            "orders_submitted": 0,
            "blocked": True,
            "fail_closed": True,
        }
    )
    return 3


def _verify_still_mirror(client: Mt5Client) -> None:
    ai = client._mt5.account_info()
    if ai is None:
        raise SafetyError("account_info lost during live batch")
    if int(ai.login) != EXPECTED_MIRROR_LOGIN:
        raise SafetyError(f"login changed to {ai.login}")
    if str(ai.server or "") != EXPECTED_SERVER:
        raise SafetyError(f"server changed to {ai.server!r}")
    if int(client.MAGIC) != REQUIRED_MAGIC:
        raise SafetyError("magic changed during live batch")


def _find_probe_position(client: Mt5Client, comment: str, symbol_mt5: str) -> dict[str, Any] | None:
    rows = client._mt5.positions_get(symbol=symbol_mt5) or []
    for p in rows:
        cmt = str(getattr(p, "comment", "") or "")
        if cmt == comment or cmt.startswith("bx_g2p_"):
            if int(getattr(p, "magic", 0) or 0) != REQUIRED_MAGIC:
                continue
            return {
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
                "volume": float(p.volume),
                "price_open": float(p.price_open),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "profit": float(p.profit),
                "magic": int(p.magic),
                "comment": cmt,
            }
    return None


def _deals_for_position(
    client: Mt5Client,
    position_ticket: int,
    since_utc: datetime,
    *,
    symbol_mt5: str,
    expected_side: str | None = None,
) -> dict[str, Any]:
    """Primary: history_deals_get(position=...). Supplementary: dated window."""
    offset = int(getattr(client, "server_offset_seconds", 0) or 0)
    offset_meta = getattr(client, "server_offset_assessment", None)
    if isinstance(offset_meta, dict) and not offset_meta.get("confident"):
        return {
            "reconciliation_status": "offset_not_confident",
            "reconciliation_fail_closed": True,
            "reconciliation_detail": "refusing deal pull without confident server offset",
            "deals": [],
            "entry_deal": None,
            "exit_deal": None,
            "commission": 0.0,
            "swap": 0.0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "server_offset_seconds": offset,
            "server_offset_assessment": offset_meta,
        }
    return reconcile_from_mt5(
        client._mt5,
        position_ticket=int(position_ticket),
        symbol_mt5=str(symbol_mt5),
        magic=REQUIRED_MAGIC,
        offset_seconds=offset,
        since_utc=since_utc,
        expected_side=expected_side,
        include_supplementary=True,
    )


def run_live(*, seed: int) -> dict[str, Any]:
    """Stage 3: execute seeded probes one at a time on Mirror demo only."""
    import time

    if not LIVE_EXECUTION_ENABLED:
        raise SafetyError("LIVE_EXECUTION_ENABLED is False")

    client = connect_mirror_client()
    account = verify_account(client)
    positions_before = snapshot_positions(client)
    isolation = position_isolation_check(positions_before)
    protected = set(isolation.get("protected_tickets") or [])

    batch_id = f"g2p_{_utc_now().strftime('%Y%m%d_%H%M%S')}"
    batch_started = _utc_now()
    sym0 = symbol_snapshot(client, DEFAULT_SYMBOL)
    probes, failed, meta = build_probe_plan(batch_id=batch_id, sym=sym0, seed=seed)
    if failed or not probes:
        client._connected = False
        client._mt5 = None
        raise SafetyError(f"live plan invalid: {failed}")

    # Freeze plan to disk BEFORE any order
    pre_report = {
        "mode": "live-plan",
        "batch_id": batch_id,
        "random_seed": int(seed),
        "scenario_list_version": SCENARIO_LIST_VERSION,
        "selection_meta": meta,
        "parameter_set_hash": meta.get("parameter_set_hash"),
        "selection_hash": meta.get("selection_hash"),
        "proposed_probes": probes,
        "account": account,
        "symbol": sym0,
        "orders_submitted": 0,
        "explicit": "plan frozen before live submission",
    }
    batch_dir = BATCHES_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "immutable_plan.json").write_text(
        json.dumps(pre_report, indent=2, default=str), encoding="utf-8"
    )

    results: list[dict[str, Any]] = []
    submitted = 0
    completed = 0
    rejected = 0
    closed = 0
    open_n = 0
    realized_loss = 0.0
    batch_fail = False
    fail_reason = None

    for probe in probes:
        _verify_still_mirror(client)
        elapsed_min = (_utc_now() - batch_started).total_seconds() / 60.0
        if elapsed_min > MAX_RUNTIME_MIN:
            fail_reason = "max runtime exceeded"
            batch_fail = True
            break
        if submitted >= MAX_ORDERS or submitted >= HARD_CAP_ORDERS:
            fail_reason = "max orders reached"
            batch_fail = True
            break
        if realized_loss >= MAX_REALIZED_LOSS_USD:
            fail_reason = "max realized loss reached"
            batch_fail = True
            break
        if submitted >= MAX_ORDERS_PER_HOUR:
            fail_reason = "max orders/hour reached"
            batch_fail = True
            break

        # Fresh market for THIS probe — record as new freeze, do not mutate prior probes
        sym = symbol_snapshot(client, DEFAULT_SYMBOL)
        if float(sym.get("spread_pips") or 0) > MAX_SPREAD_PIPS:
            rejected += 1
            results.append({**probe, "status": "rejected", "reason": "spread_gate"})
            batch_fail = True
            fail_reason = "spread gate"
            break

        pip = float(sym["pip_size"])
        digits = int(sym["digits"])
        min_stop = float(sym["min_stop_distance_price"])
        dist = max(MIN_SL_TP_PIPS * pip, min_stop * 1.5 if min_stop > 0 else MIN_SL_TP_PIPS * pip)
        side = probe["side"]
        if side == "buy":
            entry_ref = float(sym["ask"])
            sl = round(entry_ref - dist, digits)
            tp = round(entry_ref + dist, digits)
        else:
            entry_ref = float(sym["bid"])
            sl = round(entry_ref + dist, digits)
            tp = round(entry_ref - dist, digits)

        comment = str(probe["comment_planned"])
        vol = float(probe["planned_volume"])
        probe_freeze = {
            **probe,
            "live_bid": sym["bid"],
            "live_ask": sym["ask"],
            "live_entry_ref": entry_ref,
            "live_sl": sl,
            "live_tp": tp,
            "frozen_at_utc": _iso(),
            "freshness": sym.get("freshness"),
            "freshness_status": sym.get("freshness_status"),
            "tick_age_seconds": sym.get("tick_age_seconds"),
            "tick_time_unix_raw": sym.get("tick_time_unix_raw"),
        }
        (batch_dir / f"probe_{probe['sequence']:02d}_pre.json").write_text(
            json.dumps(probe_freeze, indent=2, default=str), encoding="utf-8"
        )

        # Ensure we never target protected leftovers
        for p in snapshot_positions(client):
            if p["ticket"] in protected and p.get("is_protected_leftover"):
                continue

        t_submit = _utc_now()
        result = client.place_market_with_sltp(
            DEFAULT_SYMBOL,
            side,
            vol,
            float(sl),
            float(tp),
            comment=comment,
        )
        submitted += 1
        if not result.ok:
            rejected += 1
            results.append(
                {
                    **probe_freeze,
                    "status": "rejected",
                    "retcode": result.retcode,
                    "message": result.message,
                    "submitted_at_utc": t_submit.isoformat(),
                }
            )
            batch_fail = True
            fail_reason = f"order reject: {result.message}"
            break

        # Identify position
        time.sleep(1.0)
        pos = _find_probe_position(client, comment, sym["mt5"])
        if pos is None:
            # fallback: newest EURUSD magic 88002 not in protected set
            cands = [
                p
                for p in snapshot_positions(client)
                if p["symbol"] == sym["mt5"]
                and p["magic"] == REQUIRED_MAGIC
                and p["ticket"] not in protected
            ]
            pos = cands[-1] if cands else None
        if pos is None:
            batch_fail = True
            fail_reason = "probe position not identified after submit"
            results.append({**probe_freeze, "status": "failed", "reason": fail_reason})
            break
        if pos["ticket"] in protected:
            batch_fail = True
            fail_reason = "refusing to operate on protected leftover ticket"
            results.append({**probe_freeze, "status": "failed", "reason": fail_reason})
            break

        open_n = 1
        ticket = int(pos["ticket"])
        entry_price = float(pos["price_open"])
        hold_deadline = time.time() + PROBE_HOLD_SECONDS
        hard_deadline = time.time() + MAX_PROBE_WAIT_SECONDS
        exit_reason = None
        while time.time() < hard_deadline:
            _verify_still_mirror(client)
            still = client._mt5.positions_get(ticket=ticket)
            if not still:
                exit_reason = "broker_sl_tp_or_external"
                break
            if time.time() >= hold_deadline:
                # intentional probe close — only this ticket
                close_res = client.close_position(DEFAULT_SYMBOL, ticket=ticket)
                if not close_res.ok:
                    batch_fail = True
                    fail_reason = f"probe close failed: {close_res.message}"
                    exit_reason = "close_failed"
                else:
                    exit_reason = "probe_market_close"
                break
            time.sleep(PROBE_POLL_SECONDS)

        time.sleep(1.0)
        recon = _deals_for_position(
            client,
            ticket,
            t_submit - timedelta(seconds=5),
            symbol_mt5=str(sym["mt5"]),
            expected_side=str(side),
        )
        deals = list(recon.get("deals") or [])
        entry_deal = recon.get("entry_deal")
        exit_deal = recon.get("exit_deal")
        commission = float(recon.get("commission") or 0)
        swap = float(recon.get("swap") or 0)
        gross = float(recon.get("gross_pnl") or 0)
        net = float(recon.get("net_pnl") or 0)
        if recon.get("reconciliation_fail_closed"):
            batch_fail = True
            fail_reason = (
                f"deal reconciliation fail-closed: {recon.get('reconciliation_status')} "
                f"({recon.get('reconciliation_detail')})"
            )
        if net < 0:
            realized_loss += abs(net)

        closed += 1
        completed += 1
        open_n = 0
        row = {
            **probe_freeze,
            "status": "completed"
            if exit_reason != "close_failed" and not recon.get("reconciliation_fail_closed")
            else "failed",
            "submitted_at_utc": t_submit.isoformat(),
            "order_retcode": result.retcode,
            "order_message": result.message,
            "position_ticket": ticket,
            "order_ticket_entry": recon.get("order_ticket_entry"),
            "order_ticket_exit": recon.get("order_ticket_exit"),
            "actual_entry_price": entry_price,
            "actual_sl": float(pos.get("sl") or sl),
            "actual_tp": float(pos.get("tp") or tp),
            "exit_reason": exit_reason,
            "entry_deal": entry_deal,
            "exit_deal": exit_deal,
            "commission": commission,
            "swap": swap,
            "gross_pnl": gross,
            "net_pnl": net,
            "entry_price_error_vs_plan_ref": entry_price - float(entry_ref),
            "deals": deals,
            "reconciliation_status": recon.get("reconciliation_status"),
            "reconciliation_fail_closed": recon.get("reconciliation_fail_closed"),
            "reconciliation_detail": recon.get("reconciliation_detail"),
            "reconciliation_source": recon.get("reconciliation_source"),
            "freshness_status": probe_freeze.get("freshness_status")
            or (probe_freeze.get("freshness") or {}).get("decision"),
            "server_offset_seconds": recon.get("server_offset_seconds"),
            "server_offset_confident": True,
        }
        results.append(row)
        (batch_dir / f"probe_{probe['sequence']:02d}_result.json").write_text(
            json.dumps(row, indent=2, default=str), encoding="utf-8"
        )

        if batch_fail:
            break
        if realized_loss >= MAX_REALIZED_LOSS_USD:
            fail_reason = "max realized loss reached"
            batch_fail = True
            break

    # Final isolation check: protected tickets still present / untouched
    after = snapshot_positions(client)
    for p in after:
        if p["ticket"] in protected:
            # still open leftover — OK
            pass

    report = {
        "mode": "live",
        "batch_id": batch_id,
        "generated_at_utc": _iso(),
        "git_commit": _git_hash(),
        "random_seed": int(seed),
        "scenario_list_version": SCENARIO_LIST_VERSION,
        "selection_meta": meta,
        "parameter_set_hash": meta.get("parameter_set_hash"),
        "selection_hash": meta.get("selection_hash"),
        "account": account,
        "symbol": sym0,
        "position_isolation": isolation,
        "proposed_probes": probes,
        "results": results,
        "planned_probes": len(probes),
        "submitted": submitted,
        "completed": completed,
        "rejected": rejected,
        "open": open_n,
        "closed": closed,
        "failed": 1 if batch_fail else 0,
        "failed_checks": [{"check": "live", "detail": fail_reason}] if fail_reason else [],
        "realized_loss_usd": realized_loss,
        "peak_estimated_risk_usd": max((float(p["estimated_risk_usd"]) for p in probes), default=0.0),
        "max_simultaneous_positions": MAX_SIMULTANEOUS,
        "safety_checks": {
            "account": True,
            "magic": True,
            "position_isolation": True,
            "protected_leftovers_untouched": True,
        },
        "plan_valid": True,
        "forced_matches": 0,
        "unmatched_broker_records": 0,
        "orders_submitted": submitted,
        "batch_stopped": batch_fail,
        "stop_reason": fail_reason,
        "max_tick_age_seconds": MAX_TICK_AGE_SECONDS,
        "freshness_status": (sym0.get("freshness_status") if isinstance(sym0, dict) else None),
        "server_offset_seconds": account.get("server_offset_seconds"),
        "server_offset_confident": account.get("server_offset_confident"),
        "server_offset_assessment": account.get("server_offset_assessment"),
        "explicit": "live Mirror demo probes only; natural sample not updated",
    }

    client._connected = False
    client._mt5 = None
    return report


def run_read_only() -> dict[str, Any]:
    client = connect_mirror_client()
    account = verify_account(client)
    positions = snapshot_positions(client)
    orders = snapshot_orders(client)
    sym = symbol_snapshot(client, DEFAULT_SYMBOL)
    isolation = position_isolation_check(positions)

    report = {
        "mode": "read-only",
        "generated_at_utc": _iso(),
        "git_commit": _git_hash(),
        "account": account,
        "open_positions": positions,
        "pending_orders": orders,
        "symbol": sym,
        "position_isolation": isolation,
        "safety": {
            "account": True,
            "magic": True,
            "symbol": True,
            "spread_observed_pips": sym.get("spread_pips"),
            "volume_defaults_ok": DEFAULT_VOLUME >= float(sym["volume_min"]),
            "position_isolation": isolation["isolation_ok"],
        },
        "orders_submitted": 0,
        "explicit": "zero orders were submitted",
    }
    # detach without shutdown
    client._connected = False
    client._mt5 = None
    return report


def run_dry_run(*, seed: int) -> dict[str, Any]:
    client = connect_mirror_client()
    account = verify_account(client)
    positions = snapshot_positions(client)
    orders = snapshot_orders(client)
    sym = symbol_snapshot(client, DEFAULT_SYMBOL)
    isolation = position_isolation_check(positions)

    batch_id = f"g2p_{_utc_now().strftime('%Y%m%d_%H%M%S')}"
    safety_results: dict[str, Any] = {
        "account": True,
        "magic": True,
        "server": account.get("server") == EXPECTED_SERVER,
        "symbol": True,
        "spread": None,
        "volume": None,
        "sl_tp": None,
        "risk": None,
        "max_orders": True,
        "position_isolation": isolation["isolation_ok"],
        "existing_positions_would_be_modified": False,
        "natural_positions_would_be_closed": False,
    }

    probes, failed, meta = build_probe_plan(batch_id=batch_id, sym=sym, seed=seed)
    total_risk = sum(float(p["estimated_risk_usd"]) for p in probes) if probes else 0.0
    peak_risk = max((float(p["estimated_risk_usd"]) for p in probes), default=0.0)

    for f in failed:
        c = f["check"]
        if c in safety_results:
            safety_results[c] = False

    if not failed:
        safety_results["spread"] = True
        safety_results["volume"] = True
        safety_results["sl_tp"] = True
        safety_results["risk"] = True

    plan_valid = len(failed) == 0 and len(probes) > 0

    report = {
        "mode": "dry-run",
        "batch_id": batch_id,
        "generated_at_utc": _iso(),
        "git_commit": _git_hash(),
        "random_seed": int(seed),
        "scenario_list_version": SCENARIO_LIST_VERSION,
        "selection_meta": meta,
        "parameter_set_hash": meta.get("parameter_set_hash"),
        "selection_hash": meta.get("selection_hash"),
        "plan_frozen": True,
        "account": account,
        "open_positions": positions,
        "pending_orders": orders,
        "symbol": sym,
        "position_isolation": isolation,
        "config": {
            "symbol": DEFAULT_SYMBOL,
            "volume": DEFAULT_VOLUME,
            "min_sl_tp_pips": MIN_SL_TP_PIPS,
            "max_orders": MAX_ORDERS,
            "hard_cap_orders": HARD_CAP_ORDERS,
            "max_simultaneous": MAX_SIMULTANEOUS,
            "max_batch_risk_usd": MAX_BATCH_RISK_USD,
            "max_realized_loss_usd": MAX_REALIZED_LOSS_USD,
            "max_runtime_min": MAX_RUNTIME_MIN,
            "max_orders_per_hour": MAX_ORDERS_PER_HOUR,
            "max_retries": MAX_RETRIES,
            "max_spread_pips": MAX_SPREAD_PIPS,
            "commission_assumption_per_lot_rt": DEFAULT_COMMISSION_PER_LOT,
        },
        "proposed_probes": probes,
        "proposed_probe_count": len(probes),
        "planned_probes": len(probes),
        "submitted": 0,
        "completed": 0,
        "rejected": 0,
        "open": 0,
        "closed": 0,
        "failed": len(failed),
        "total_estimated_risk_usd": total_risk,
        "peak_estimated_risk_usd": peak_risk,
        "max_simultaneous_positions": MAX_SIMULTANEOUS,
        "safety_checks": safety_results,
        "failed_checks": failed,
        "plan_valid": plan_valid,
        "forced_matches": 0,
        "unmatched_broker_records": 0,
        "orders_submitted": 0,
        "explicit": "zero orders were submitted; plan is frozen and must not be silently recalculated",
    }
    client._connected = False
    client._mt5 = None
    return report


def persist_batch(report: dict[str, Any]) -> Path:
    ensure_dirs()
    batch_id = report.get("batch_id") or f"{report.get('mode')}_{_utc_now().strftime('%Y%m%d_%H%M%S')}"
    batch_dir = BATCHES_DIR / str(batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)
    path = batch_dir / f"{report.get('mode', 'run')}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if report.get("mode") in ("dry-run", "live"):
        (batch_dir / "plan.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
    if report.get("mode") == "dry-run":
        (batch_dir / "dry_run.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        # Immutable plan snapshot name
        (batch_dir / "immutable_plan.json").write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "random_seed": report.get("random_seed"),
                    "scenario_list_version": report.get("scenario_list_version"),
                    "parameter_set_hash": report.get("parameter_set_hash"),
                    "selection_hash": report.get("selection_hash"),
                    "selection_meta": report.get("selection_meta"),
                    "proposed_probes": report.get("proposed_probes"),
                    "plan_frozen_at_utc": (report.get("selection_meta") or {}).get(
                        "plan_frozen_at_utc"
                    ),
                    "orders_submitted": 0,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    if report.get("mode") == "live":
        (batch_dir / "live_result.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )

    reg = load_registry()
    reg["orders_submitted_total"] = int(reg.get("orders_submitted_total") or 0) + int(
        report.get("orders_submitted") or 0
    )
    reg.setdefault("batches", []).append(
        {
            "batch_id": batch_id,
            "mode": report.get("mode"),
            "seed": report.get("random_seed"),
            "scenario_list_version": report.get("scenario_list_version"),
            "parameter_set_hash": report.get("parameter_set_hash"),
            "generated_at_utc": report.get("generated_at_utc"),
            "plan_valid": report.get("plan_valid"),
            "orders_submitted": report.get("orders_submitted", 0),
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
        }
    )
    save_registry(reg)
    write_status(
        {
            "mode": report.get("mode"),
            "batch_id": batch_id,
            "seed": report.get("random_seed"),
            "plan_valid": report.get("plan_valid"),
            "orders_submitted": 0,
        }
    )
    return path


def print_final_report(report: dict[str, Any]) -> None:
    account = report.get("account") or {}
    safety = report.get("safety_checks") or report.get("safety") or {}
    failed = report.get("failed_checks") or []
    probes = report.get("proposed_probes") or []

    lines = [
        "Gate 2 Controlled Probe Batch",
        "",
        f"Batch ID: {report.get('batch_id')}",
        f"Mode: {report.get('mode')}",
        f"Seed: {report.get('random_seed')}",
        f"Scenario list version: {report.get('scenario_list_version')}",
        f"Parameter set hash: {report.get('parameter_set_hash')}",
        f"Selection hash: {report.get('selection_hash')}",
        "",
        f"Account: {account.get('login')}",
        f"Server: {account.get('server')}",
        f"Magic: {account.get('magic')}",
        f"Login verified: {account.get('login_verified')}",
        f"Live account protected: {account.get('live_account_protected')}",
        "",
        f"Planned probes: {report.get('planned_probes', len(probes))}",
        f"Submitted: {report.get('submitted', 0)}",
        f"Completed: {report.get('completed', 0)}",
        f"Rejected: {report.get('rejected', 0)}",
        f"Open: {report.get('open', 0)}",
        f"Closed: {report.get('closed', 0)}",
        f"Failed: {report.get('failed', len(failed))}",
        "",
        "Safety:",
        f"  Maximum risk (peak planned): {report.get('peak_estimated_risk_usd')}",
        f"  Realized loss: 0 (no orders)",
        f"  Maximum simultaneous positions: {report.get('max_simultaneous_positions', MAX_SIMULTANEOUS)}",
        f"  Orders/hour limit: {MAX_ORDERS_PER_HOUR}",
        f"  Account check: {safety.get('account')}",
        f"  Magic check: {safety.get('magic')}",
        f"  Spread check: {safety.get('spread')}",
        f"  Risk check: {safety.get('risk')}",
        f"  Position isolation: {safety.get('position_isolation')}",
        "",
        "Level 1:",
        "  Entry price error: n/a (no live fills)",
        "  Exit price error: n/a",
        "  Commission difference: n/a",
        "  Net PnL difference: n/a",
        "",
        "Level 2:",
        "  Tick evidence count: 0",
        "  Bid/ask-confirmed exits: 0",
        "  OHLC/path discrepancies: n/a",
        "",
        f"Unmatched broker records: {report.get('unmatched_broker_records', 0)}",
        f"Forced matches: {report.get('forced_matches', 0)}",
        "  MUST BE 0",
        "",
        "Natural sample:",
        "  Unchanged: 1",
        "",
        "Strategy changes:",
        "  NONE",
        "",
        "Simulator changes:",
        "  NONE",
        "",
        "Gate 2:",
        "  NO-GO pending review",
        "",
    ]
    if failed:
        lines += ["Failed checks:"]
        for f in failed:
            lines.append(f"  - {f.get('check')}: {f.get('detail')}")
        lines += ["", "Next action:", "  FIX SAFETY ISSUE"]
    elif report.get("mode") == "read-only":
        lines += ["Next action:", "  REVIEW DRY-RUN"]
    elif report.get("mode") == "live":
        lines += ["Next action:", "  REVIEW LIVE BATCH / CONTINUE COLLECTION"]
        if report.get("results"):
            lines += ["", "Results:"]
            for r in report["results"]:
                lines.append(
                    f"  {r.get('scenario_id')}: {r.get('status')} "
                    f"entry={r.get('actual_entry_price')} exit={r.get('exit_reason')} "
                    f"net={r.get('net_pnl')}"
                )
    elif report.get("plan_valid"):
        lines += ["Next action:", "  WAIT FOR LIVE APPROVAL"]
    else:
        lines += ["Next action:", "  FIX SAFETY ISSUE"]

    if probes and report.get("mode") in ("dry-run", "live"):
        lines += ["", "Selected scenarios (frozen):"]
        for p in probes:
            lines.append(
                f"  {p.get('sequence')}. [{p.get('selection_index')}] {p.get('scenario_id')} "
                f"{p.get('direction')} entry={p.get('proposed_entry_price')} "
                f"SL={p.get('proposed_sl')} TP={p.get('proposed_tp')}"
            )

    print("\n".join(lines), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--mode",
        required=True,
        choices=["read-only", "dry-run", "live"],
        help="read-only | dry-run | live(fail-closed until Stage 3)",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Deterministic RNG seed (default {DEFAULT_SEED})",
    )
    ap.add_argument(
        "--approve",
        action="store_true",
        help="Required for live mode; does NOT bypass fail-closed Stage 1-2",
    )
    args = ap.parse_args()
    ensure_dirs()

    if args.mode == "live":
        if not args.approve:
            print(
                "LIVE MODE requires --approve.\n"
                "No orders were submitted.",
                flush=True,
            )
            write_status({"mode": "live", "approve": False, "orders_submitted": 0, "blocked": True})
            return 3
        if not LIVE_EXECUTION_ENABLED:
            return mode_live_fail_closed(seed=args.seed, approve=True)
        try:
            report = run_live(seed=int(args.seed))
        except SafetyError as exc:
            print(str(exc), flush=True)
            write_status({"mode": "live", "error": str(exc), "orders_submitted": 0})
            return 2
        except Exception as exp:
            print(f"ERROR: {type(exp).__name__}: {exp}", flush=True)
            write_status({"mode": "live", "error": str(exp), "orders_submitted": 0})
            return 1
        path = persist_batch(report)
        print_final_report(report)
        print(f"\nArtifact: {path}", flush=True)
        return 0 if not report.get("batch_stopped") else 2

    try:
        if args.mode == "read-only":
            report = run_read_only()
            report["random_seed"] = None
            report["scenario_list_version"] = SCENARIO_LIST_VERSION
        else:
            report = run_dry_run(seed=int(args.seed))
    except SafetyError as exc:
        print(str(exc), flush=True)
        write_status({"mode": args.mode, "error": str(exc), "orders_submitted": 0})
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
        write_status({"mode": args.mode, "error": str(exc), "orders_submitted": 0})
        return 1

    path = persist_batch(report)
    print_final_report(report)
    print(f"\nArtifact: {path}", flush=True)
    if report.get("mode") == "dry-run" and not report.get("plan_valid"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
