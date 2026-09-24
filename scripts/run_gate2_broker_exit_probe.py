"""Gate 2 broker-triggered SL/TP exit probe (Mirror demo only).

Validates genuine broker-side SL/TP execution — NOT market-close contamination.

Modes:
  --mode read-only
  --mode dry-run
  --mode live --approve

Live places at most 2 EURUSD probes (1 SL + 1 TP) on Mirror 53019882 / magic 88002.
Does NOT touch live 52958398 / 88001 / port 8791.
Does NOT close/modify protected EURDKK leftovers.

DO NOT run live without explicit human approval.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path[:0] = [str(ROOT), str(LIVE), str(ROOT / "scripts")]

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")

import run_gate2_controlled_probe as g2  # noqa: E402
from borex.gate2.exit_classify import (  # noqa: E402
    EXIT_BROKER_SL,
    EXIT_BROKER_TP,
    EXIT_CLEANUP_MARKET_CLOSE,
    EXIT_TIMEOUT,
    choose_trigger_distance,
    classify_broker_exit,
    plan_sl_tp_levels,
    quote_would_trigger,
)
from borex.gate2.probe_reconcile import reconcile_from_mt5  # noqa: E402

ARTIFACT_ROOT = ROOT / "data" / "runs" / "gate2_broker_exit_probe"
RUNS_DIR = ARTIFACT_ROOT / "runs"

# Experiment constants (conservative; fail-closed if distance impractical)
DEFAULT_SYMBOL = "EURUSD=X"
DEFAULT_VOLUME = 0.01
MAX_SPREAD_PIPS = 2.0
MAX_TRIGGER_PIPS = 8.0
PROTECTIVE_PIPS = 50.0
PROBE_WAIT_SECONDS = 600  # wait for broker trigger; no silent market-close success
POLL_SECONDS = 2.0
CLEANUP_ON_TIMEOUT = True
LIVE_EXECUTION_ENABLED = True
COMMENT_PREFIX = "bx_g2e_"  # Gate2 exit-probe comments


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _git_hash() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def ensure_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def assert_eurdkk_unchanged(before_tickets: set[int], client: Any) -> None:
    after = g2.snapshot_positions(client)
    after_eurdkk = {
        int(p["ticket"])
        for p in after
        if str(p.get("symbol") or "").upper().startswith("EURDKK")
    }
    if after_eurdkk != before_tickets:
        raise g2.SafetyError(
            f"EURDKK protected set changed: before={sorted(before_tickets)} after={sorted(after_eurdkk)}"
        )


def build_exit_plan(*, sym: dict[str, Any], run_id: str) -> tuple[list[dict[str, Any]], list[dict]]:
    """Freeze SL + TP probe plans from current symbol snapshot."""
    failed: list[dict] = []
    if float(sym.get("spread_pips") or 0) > MAX_SPREAD_PIPS:
        failed.append(
            {
                "check": "spread",
                "detail": f"spread_pips={sym.get('spread_pips')} > max {MAX_SPREAD_PIPS}",
            }
        )
        return [], failed

    pip = float(sym["pip_size"])
    digits = int(sym["digits"])
    freeze_px = float(sym.get("point") or 0) * float(sym.get("trade_freeze_level") or 0)
    dist = choose_trigger_distance(
        pip_size=pip,
        min_stop_distance_price=float(sym.get("min_stop_distance_price") or 0),
        spread_price=float(sym.get("spread_price") or 0),
        freeze_level_price=freeze_px,
        max_trigger_pips=MAX_TRIGGER_PIPS,
    )
    if not dist.get("ok"):
        failed.append({"check": "trigger_distance", "detail": dist.get("rejection_reason")})
        return [], failed

    protective = PROTECTIVE_PIPS * pip
    scenarios = [
        ("BROKER_EXIT_LONG_SL", "buy", "sl"),
        ("BROKER_EXIT_LONG_TP", "buy", "tp"),
    ]
    probes: list[dict[str, Any]] = []
    for i, (sid, side, intended) in enumerate(scenarios, start=1):
        entry_ref = float(sym["ask"]) if side == "buy" else float(sym["bid"])
        levels = plan_sl_tp_levels(
            side=side,
            entry_ref=entry_ref,
            distance_price=float(dist["distance_price"]),
            protective_distance_price=protective,
            digits=digits,
            intended=intended,
        )
        risk = g2.estimate_risk_usd(
            side=side,
            entry=entry_ref,
            sl=float(levels["sl"]),
            volume=DEFAULT_VOLUME,
            contract_size=float(sym.get("contract_size") or 100000),
        )
        probes.append(
            {
                "probe_id": f"{run_id}-{i:02d}-{sid}",
                "run_id": run_id,
                "sequence": i,
                "scenario_id": sid,
                "symbol": DEFAULT_SYMBOL,
                "symbol_mt5": sym["mt5"],
                "direction": "long" if side == "buy" else "short",
                "side": side,
                "intended_trigger": intended,
                "planned_volume": DEFAULT_VOLUME,
                "bid": sym["bid"],
                "ask": sym["ask"],
                "spread_price": sym.get("spread_price"),
                "spread_pips": sym.get("spread_pips"),
                "proposed_entry_price": entry_ref,
                "proposed_sl": levels["sl"],
                "proposed_tp": levels["tp"],
                "trigger_side": levels["trigger_side"],
                "trigger_distance_price": dist["distance_price"],
                "trigger_distance_pips": dist["distance_pips"],
                "protective_distance_pips": PROTECTIVE_PIPS,
                "min_stop_distance_price": sym.get("min_stop_distance_price"),
                "trade_freeze_level": sym.get("trade_freeze_level"),
                "estimated_risk_usd": risk,
                "comment_planned": f"{COMMENT_PREFIX}{run_id[-6:]}_{i:02d}"[:31],
                "distance_selection": dist,
                "freshness": sym.get("freshness"),
                "freshness_status": sym.get("freshness_status"),
                "tick_age_seconds": sym.get("tick_age_seconds"),
                "tick_time_unix_raw": sym.get("tick_time_unix_raw"),
            }
        )
    return probes, failed


def run_read_only() -> dict[str, Any]:
    client = g2.connect_mirror_client()
    account = g2.verify_account(client)
    positions = g2.snapshot_positions(client)
    isolation = g2.position_isolation_check(positions)
    sym = g2.symbol_snapshot(client, DEFAULT_SYMBOL)
    freeze_px = float(sym.get("point") or 0) * float(sym.get("trade_freeze_level") or 0)
    dist = choose_trigger_distance(
        pip_size=float(sym["pip_size"]),
        min_stop_distance_price=float(sym.get("min_stop_distance_price") or 0),
        spread_price=float(sym.get("spread_price") or 0),
        freeze_level_price=freeze_px,
        max_trigger_pips=MAX_TRIGGER_PIPS,
    )
    report = {
        "mode": "read-only",
        "generated_at_utc": _iso(),
        "git_commit": _git_hash(),
        "account": account,
        "symbol": sym,
        "position_isolation": isolation,
        "trigger_distance_preview": dist,
        "orders_submitted": 0,
        "explicit": "zero orders were submitted",
    }
    client._connected = False
    client._mt5 = None
    return report


def run_dry_run() -> dict[str, Any]:
    client = g2.connect_mirror_client()
    account = g2.verify_account(client)
    positions = g2.snapshot_positions(client)
    isolation = g2.position_isolation_check(positions)
    sym = g2.symbol_snapshot(client, DEFAULT_SYMBOL)
    run_id = f"g2e_{_utc_now().strftime('%Y%m%d_%H%M%S')}"
    probes, failed = build_exit_plan(sym=sym, run_id=run_id)
    report = {
        "mode": "dry-run",
        "run_id": run_id,
        "generated_at_utc": _iso(),
        "plan_frozen_at_utc": _iso(),
        "git_commit": _git_hash(),
        "account": account,
        "symbol": sym,
        "position_isolation": isolation,
        "proposed_probes": probes,
        "failed_checks": failed,
        "plan_valid": len(failed) == 0 and len(probes) == 2,
        "orders_submitted": 0,
        "max_spread_pips": MAX_SPREAD_PIPS,
        "max_trigger_pips": MAX_TRIGGER_PIPS,
        "probe_wait_seconds": PROBE_WAIT_SECONDS,
        "explicit": "dry-run: zero orders; plan only",
        "gate2": "NO-GO",
    }
    client._connected = False
    client._mt5 = None
    return report


def _snapshot_quote(client: Any, mt5_sym: str) -> dict[str, Any]:
    tick = client._mt5.symbol_info_tick(mt5_sym)
    if tick is None:
        raise g2.SafetyError(f"no tick for {mt5_sym}")
    return {
        "bid": float(tick.bid),
        "ask": float(tick.ask),
        "tick_time_unix_raw": int(getattr(tick, "time", 0) or 0),
        "captured_at_utc": _iso(),
    }


def _wait_for_broker_exit(
    client: Any,
    *,
    ticket: int,
    side: str,
    intended: str,
    sl: float,
    tp: float,
    mt5_sym: str,
    wait_seconds: float,
) -> dict[str, Any]:
    """Poll until position gone or timeout. Never market-closes here."""
    deadline = time.time() + float(wait_seconds)
    quotes: list[dict[str, Any]] = []
    while time.time() < deadline:
        g2._verify_still_mirror(client)
        q = _snapshot_quote(client, mt5_sym)
        q["would_trigger"] = quote_would_trigger(
            side=side, intended=intended, sl=sl, tp=tp, bid=q["bid"], ask=q["ask"]
        )
        quotes.append(q)
        still = client._mt5.positions_get(ticket=ticket)
        if not still:
            return {
                "closed": True,
                "timed_out": False,
                "quotes": quotes[-20:],
                "last_quote": q,
            }
        time.sleep(POLL_SECONDS)
    last = quotes[-1] if quotes else _snapshot_quote(client, mt5_sym)
    still = client._mt5.positions_get(ticket=ticket)
    return {
        "closed": not bool(still),
        "timed_out": bool(still),
        "quotes": quotes[-20:],
        "last_quote": last,
    }


def run_live() -> dict[str, Any]:
    if not LIVE_EXECUTION_ENABLED:
        raise g2.SafetyError("LIVE_EXECUTION_ENABLED is False")

    client = g2.connect_mirror_client()
    account = g2.verify_account(client)
    positions_before = g2.snapshot_positions(client)
    isolation = g2.position_isolation_check(positions_before)
    protected = set(isolation.get("protected_tickets") or [])
    eurdkk_before = set(protected)

    if isolation.get("existing_probe_positions", 0) not in (0, None):
        # Allow only if comments are not our exit-probe prefix — still refuse any EURUSD open probe
        eur_open = [
            p
            for p in positions_before
            if p.get("symbol") == "EURUSD" and p.get("magic") == g2.REQUIRED_MAGIC
        ]
        if eur_open:
            raise g2.SafetyError(f"unexpected open EURUSD magic positions: {eur_open}")

    run_id = f"g2e_{_utc_now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    sym0 = g2.symbol_snapshot(client, DEFAULT_SYMBOL)
    probes, failed = build_exit_plan(sym=sym0, run_id=run_id)
    if failed or len(probes) != 2:
        raise g2.SafetyError(f"live plan invalid: {failed}")

    plan = {
        "mode": "live-plan",
        "run_id": run_id,
        "generated_at_utc": _iso(),
        "plan_frozen_at_utc": _iso(),
        "git_commit": _git_hash(),
        "account": account,
        "symbol": sym0,
        "position_isolation": isolation,
        "proposed_probes": probes,
        "orders_submitted": 0,
        "explicit": "plan frozen before any order",
    }
    (run_dir / "immutable_plan.json").write_text(
        json.dumps(plan, indent=2, default=str), encoding="utf-8"
    )

    results: list[dict[str, Any]] = []
    submitted = 0
    for probe in probes:
        g2._verify_still_mirror(client)
        assert_eurdkk_unchanged(eurdkk_before, client)

        sym = g2.symbol_snapshot(client, DEFAULT_SYMBOL)
        if float(sym.get("spread_pips") or 0) > MAX_SPREAD_PIPS:
            raise g2.SafetyError("spread gate failed at submit time")

        # Rebuild levels from FRESH tick (do not use stale freeze alone)
        pip = float(sym["pip_size"])
        digits = int(sym["digits"])
        freeze_px = float(sym.get("point") or 0) * float(sym.get("trade_freeze_level") or 0)
        dist = choose_trigger_distance(
            pip_size=pip,
            min_stop_distance_price=float(sym.get("min_stop_distance_price") or 0),
            spread_price=float(sym.get("spread_price") or 0),
            freeze_level_price=freeze_px,
            max_trigger_pips=MAX_TRIGGER_PIPS,
        )
        if not dist.get("ok"):
            raise g2.SafetyError(f"trigger distance fail-closed: {dist.get('rejection_reason')}")

        side = probe["side"]
        intended = probe["intended_trigger"]
        entry_ref = float(sym["ask"]) if side == "buy" else float(sym["bid"])
        levels = plan_sl_tp_levels(
            side=side,
            entry_ref=entry_ref,
            distance_price=float(dist["distance_price"]),
            protective_distance_price=PROTECTIVE_PIPS * pip,
            digits=digits,
            intended=intended,
        )
        comment = str(probe["comment_planned"])
        freeze = {
            **probe,
            "live_bid": sym["bid"],
            "live_ask": sym["ask"],
            "live_entry_ref": entry_ref,
            "live_sl": levels["sl"],
            "live_tp": levels["tp"],
            "trigger_distance_price": dist["distance_price"],
            "trigger_distance_pips": dist["distance_pips"],
            "freshness": sym.get("freshness"),
            "freshness_status": sym.get("freshness_status"),
            "tick_age_seconds": sym.get("tick_age_seconds"),
            "tick_time_unix_raw": sym.get("tick_time_unix_raw"),
            "frozen_at_utc": _iso(),
        }
        (run_dir / f"probe_{probe['sequence']:02d}_pre.json").write_text(
            json.dumps(freeze, indent=2, default=str), encoding="utf-8"
        )

        t_submit = _utc_now()
        result = client.place_market_with_sltp(
            DEFAULT_SYMBOL,
            side,
            DEFAULT_VOLUME,
            float(levels["sl"]),
            float(levels["tp"]),
            comment=comment,
        )
        submitted += 1
        if not result.ok:
            row = {
                **freeze,
                "status": "rejected",
                "order_retcode": result.retcode,
                "order_message": result.message,
                "submitted_at_utc": t_submit.isoformat(),
                "exit_trigger_classification": EXIT_UNKNOWN,
            }
            results.append(row)
            break

        time.sleep(1.0)
        pos = None
        for p in g2.snapshot_positions(client):
            if p.get("comment") == comment and p.get("symbol") == sym["mt5"]:
                pos = p
                break
        if pos is None:
            cands = [
                p
                for p in g2.snapshot_positions(client)
                if p["symbol"] == sym["mt5"]
                and p["magic"] == g2.REQUIRED_MAGIC
                and p["ticket"] not in protected
            ]
            pos = cands[-1] if cands else None
        if pos is None or pos["ticket"] in protected:
            raise g2.SafetyError("probe position not identified or is protected leftover")

        ticket = int(pos["ticket"])
        wait = _wait_for_broker_exit(
            client,
            ticket=ticket,
            side=side,
            intended=intended,
            sl=float(levels["sl"]),
            tp=float(levels["tp"]),
            mt5_sym=str(sym["mt5"]),
            wait_seconds=PROBE_WAIT_SECONDS,
        )

        intentional_cleanup = False
        cleanup_result = None
        if wait["timed_out"]:
            if CLEANUP_ON_TIMEOUT:
                cleanup_result = client.close_position(DEFAULT_SYMBOL, ticket=ticket)
                intentional_cleanup = True
                time.sleep(1.0)

        time.sleep(1.0)
        offset = int(getattr(client, "server_offset_seconds", 0) or 0)
        recon = reconcile_from_mt5(
            client._mt5,
            position_ticket=ticket,
            symbol_mt5=str(sym["mt5"]),
            magic=g2.REQUIRED_MAGIC,
            offset_seconds=offset,
            since_utc=t_submit,
            expected_side=side,
            include_supplementary=True,
        )
        exit_deal = recon.get("exit_deal")
        classification = classify_broker_exit(
            exit_deal=exit_deal,
            intentional_probe_close=False,
            intentional_cleanup_close=intentional_cleanup,
            timed_out=bool(wait["timed_out"]) and not intentional_cleanup,
            position_still_open=bool(
                client._mt5.positions_get(ticket=ticket)
            ),
            requested_sl=float(levels["sl"]),
            requested_tp=float(levels["tp"]),
            exit_price=float((exit_deal or {}).get("price") or 0) or None,
            price_tolerance=float(sym["pip_size"]),
        )
        # If cleanup ran, force cleanup classification even if deal.reason is expert
        if intentional_cleanup:
            classification = classify_broker_exit(
                exit_deal=exit_deal,
                intentional_cleanup_close=True,
                requested_sl=float(levels["sl"]),
                requested_tp=float(levels["tp"]),
            )

        success = classification.classification in (EXIT_BROKER_SL, EXIT_BROKER_TP) and not recon.get(
            "reconciliation_fail_closed"
        )
        row = {
            **freeze,
            "status": "completed" if success else "failed",
            "submitted_at_utc": t_submit.isoformat(),
            "order_retcode": result.retcode,
            "order_message": result.message,
            "position_ticket": ticket,
            "order_ticket_entry": recon.get("order_ticket_entry"),
            "order_ticket_exit": recon.get("order_ticket_exit"),
            "actual_entry_price": float(pos.get("price_open") or entry_ref),
            "actual_sl": float(pos.get("sl") or levels["sl"]),
            "actual_tp": float(pos.get("tp") or levels["tp"]),
            "broker_sl_requested": levels["sl"],
            "broker_tp_requested": levels["tp"],
            "entry_deal": recon.get("entry_deal"),
            "exit_deal": exit_deal,
            "deals": recon.get("deals"),
            "commission": recon.get("commission"),
            "swap": recon.get("swap"),
            "gross_pnl": recon.get("gross_pnl"),
            "net_pnl": recon.get("net_pnl"),
            "reconciliation_status": recon.get("reconciliation_status"),
            "reconciliation_fail_closed": recon.get("reconciliation_fail_closed"),
            "reconciliation_source": recon.get("reconciliation_source"),
            "wait": {
                "timed_out": wait["timed_out"],
                "closed_before_timeout": wait["closed"],
                "last_quote": wait.get("last_quote"),
                "quote_samples": wait.get("quotes"),
            },
            "cleanup": {
                "performed": intentional_cleanup,
                "ok": None if cleanup_result is None else bool(cleanup_result.ok),
                "message": None if cleanup_result is None else cleanup_result.message,
            },
            "exit_trigger_classification": classification.classification,
            "exit_classification": classification.to_dict(),
            "server_offset_seconds": offset,
            "server_offset_confident": True,
        }
        results.append(row)
        (run_dir / f"probe_{probe['sequence']:02d}_result.json").write_text(
            json.dumps(row, indent=2, default=str), encoding="utf-8"
        )

        # Fail closed: stop batch if intended trigger was not broker-confirmed
        if not success:
            break

        assert_eurdkk_unchanged(eurdkk_before, client)

    assert_eurdkk_unchanged(eurdkk_before, client)
    after = g2.snapshot_positions(client)
    open_probes = [
        p
        for p in after
        if str(p.get("comment") or "").startswith(COMMENT_PREFIX)
        or (
            p.get("symbol") == "EURUSD"
            and p.get("magic") == g2.REQUIRED_MAGIC
            and p["ticket"] not in protected
        )
    ]

    report = {
        "mode": "live",
        "run_id": run_id,
        "generated_at_utc": _iso(),
        "git_commit": _git_hash(),
        "account": account,
        "symbol": sym0,
        "position_isolation": isolation,
        "proposed_probes": probes,
        "results": results,
        "submitted": submitted,
        "completed": sum(1 for r in results if r.get("status") == "completed"),
        "failed": sum(1 for r in results if r.get("status") != "completed"),
        "orders_submitted": submitted,
        "open_probe_positions_remaining": len(open_probes),
        "forced_matches": 0,
        "unmatched_broker_records": 0,
        "broker_sl_success": any(
            r.get("exit_trigger_classification") == EXIT_BROKER_SL for r in results
        ),
        "broker_tp_success": any(
            r.get("exit_trigger_classification") == EXIT_BROKER_TP for r in results
        ),
        "gate2": "NO-GO",
        "explicit": "broker-exit validation only; strategy/simulator frozen; natural sample unchanged",
    }
    (run_dir / "live_result.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    client._connected = False
    client._mt5 = None
    return report


def print_report(report: dict[str, Any]) -> None:
    lines = [
        "Gate 2 Broker Exit Probe",
        "",
        f"Run ID: {report.get('run_id')}",
        f"Mode: {report.get('mode')}",
        f"Account: {(report.get('account') or {}).get('login')}",
        f"Server: {(report.get('account') or {}).get('server')}",
        f"Magic: {(report.get('account') or {}).get('magic')}",
        f"Offset: {(report.get('account') or {}).get('server_offset_seconds')} "
        f"confident={(report.get('account') or {}).get('server_offset_confident')}",
        f"Orders submitted: {report.get('orders_submitted', 0)}",
        f"Plan valid: {report.get('plan_valid')}",
        f"Gate 2: {report.get('gate2', 'NO-GO')}",
    ]
    if report.get("trigger_distance_preview"):
        lines.append(f"Trigger distance preview: {report['trigger_distance_preview']}")
    for r in report.get("results") or []:
        lines.append(
            f"  {r.get('scenario_id')}: status={r.get('status')} "
            f"class={r.get('exit_trigger_classification')} "
            f"recon={r.get('reconciliation_status')} net={r.get('net_pnl')}"
        )
    for p in report.get("proposed_probes") or []:
        lines.append(
            f"  plan {p.get('sequence')}. {p.get('scenario_id')} "
            f"entry={p.get('proposed_entry_price')} SL={p.get('proposed_sl')} "
            f"TP={p.get('proposed_tp')} dist_pips={p.get('trigger_distance_pips')}"
        )
    print("\n".join(lines), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", required=True, choices=["read-only", "dry-run", "live"])
    ap.add_argument("--approve", action="store_true", help="Required for live")
    args = ap.parse_args()
    ensure_dirs()

    if args.mode == "live":
        if not args.approve:
            print("LIVE MODE requires --approve. No orders submitted.", flush=True)
            return 3
        try:
            report = run_live()
        except g2.SafetyError as exc:
            print(f"SAFETY FAIL: {exc}", flush=True)
            return 2
        print_report(report)
        print(f"\nArtifact: {RUNS_DIR / report['run_id'] / 'live_result.json'}", flush=True)
        ok = report.get("broker_sl_success") and report.get("broker_tp_success")
        return 0 if ok else 2

    try:
        report = run_read_only() if args.mode == "read-only" else run_dry_run()
    except g2.SafetyError as exc:
        print(f"SAFETY FAIL: {exc}", flush=True)
        return 2

    run_id = report.get("run_id") or f"{args.mode}_{_utc_now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = RUNS_DIR / str(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{args.mode}.json"
    if args.mode == "dry-run":
        (out_dir / "immutable_plan.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print_report(report)
    print(f"\nArtifact: {path}", flush=True)
    if args.mode == "dry-run" and not report.get("plan_valid"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
