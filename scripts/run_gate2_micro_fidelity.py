"""Gate 2 Level-4 micro fidelity runner — Mirror Theory→broker continuous probe.

Modes:
  --mode read-only     account/symbol/safety inspect; zero orders
  --mode dry-run       load 1m bars, evaluate signals, freeze Theory events; zero orders
  --mode live --approve
                       FAIL-CLOSED until LIVE_EXECUTION_ENABLED is flipped after approval

NEVER touches Live (8791 / 88001 / 52958398).
NEVER touches protected EURDKK leftovers.
NEVER imports alexg / strategy9.

Usage (PowerShell):
  ..\\borex_live\\.venv311\\Scripts\\python.exe scripts\\run_gate2_micro_fidelity.py --mode read-only
  ..\\borex_live\\.venv311\\Scripts\\python.exe scripts\\run_gate2_micro_fidelity.py --mode dry-run
  ..\\borex_live\\.venv311\\Scripts\\python.exe scripts\\run_gate2_micro_fidelity.py --mode live --approve
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path[:0] = [str(ROOT), str(LIVE), str(ROOT / "scripts")]

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")

import run_gate2_controlled_probe as g2  # noqa: E402
from borex.gate2.micro_fidelity.config import (  # noqa: E402
    FROZEN_CONFIG,
    LIVE_EXECUTION_ENABLED,
    MicroFidelityConfig,
)
from borex.gate2.micro_fidelity.m1_archive import ContinuousM1Archive  # noqa: E402
from borex.gate2.micro_fidelity.report import (  # noqa: E402
    build_reconciliation_row,
    classify_micro_exit,
    early_diagnostic,
    write_fidelity_report,
)
from borex.gate2.micro_fidelity.safety import (  # noqa: E402
    MicroSafetyError,
    assert_mirror_identity,
    assert_protected_eurdkk_intact,
    filter_experiment_positions,
    reject_if_duplicate_theory,
    snapshot_eurdkk_tickets,
    spread_gate,
)
from borex.gate2.micro_fidelity.strategy import (  # noqa: E402
    choose_sl_tp_distance,
    evaluate_completed_bar,
    plan_symmetric_sl_tp,
)
from borex.gate2.micro_fidelity.theory import (  # noqa: E402
    ImmutableJsonlStore,
    build_theory_event,
    link_execution_record,
    new_execution_id,
    new_theory_event_id,
)
from borex.gate2.probe_reconcile import reconcile_from_mt5  # noqa: E402

ARTIFACT_ROOT = ROOT / "data" / "runs" / "gate2_micro_fidelity"
RUNS_DIR = ARTIFACT_ROOT / "runs"


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


def new_run_id() -> str:
    return "g2m_" + _utc_now().strftime("%Y%m%d_%H%M%S")


def run_dir_for(run_id: str) -> Path:
    p = RUNS_DIR / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_manifest(run_dir: Path, payload: dict[str, Any]) -> None:
    (run_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def append_heartbeat(run_dir: Path, row: dict[str, Any]) -> None:
    path = run_dir / "heartbeat.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def load_m1_candles(
    client: Any, yahoo: str, *, bars: int = 120
) -> tuple[list[Any], bool]:
    """Load completed 1m candles via Mt5Client.fetch_bars (UTC-normalized).

    Returns (candles, fetch_ok). Forming/current bar is dropped. Empty + fetch_ok=False
    means provider failure (fail closed for archive); empty + fetch_ok=True is rare.
    """
    from borex.gate2.micro_fidelity.candles import MinuteCandle, floor_minute_utc

    try:
        raw = client.fetch_bars(yahoo, "1m", count=bars)
    except Exception:
        return [], False
    out: list[Any] = []
    for c in raw:
        ts = c.timestamp
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(
            MinuteCandle(
                timestamp_utc=floor_minute_utc(ts),
                open=float(c.open),
                high=float(c.high),
                low=float(c.low),
                close=float(c.close),
                tick_count=int(getattr(c, "volume", 0) or 0),
                completed=True,
            )
        )
    out.sort(key=lambda x: x.timestamp_utc)
    if out:
        return (out[:-1] if len(out) > 1 else out), True
    return [], True


def poll_m1_archive(
    client: Any,
    cfg: MicroFidelityConfig,
    archive: ContinuousM1Archive,
    *,
    bars: int = 180,
) -> dict[str, Any]:
    """Continuously ingest completed M1 for all symbols (independent of trades)."""
    appended = 0
    gaps = 0
    fetch_failures = 0
    for yahoo in cfg.symbols_yahoo:
        quote: dict[str, Any] | None = None
        try:
            quote = snapshot_quote(client, yahoo, cfg)
        except Exception:
            quote = None
        completed, ok = load_m1_candles(client, yahoo, bars=bars)
        if not ok:
            fetch_failures += 1
            st = archive.ingest_completed(yahoo, [], fetch_failed=True)
            gaps += st.gaps_recorded
            continue
        st = archive.ingest_completed(yahoo, completed, quote=quote, fetch_failed=False)
        appended += st.candles_appended
        gaps += st.gaps_recorded
    return {
        "candles_appended": appended,
        "gaps_recorded": gaps,
        "fetch_failures": fetch_failures,
    }


def snapshot_quote(client: Any, yahoo: str, cfg: MicroFidelityConfig) -> dict[str, Any]:
    return g2.symbol_snapshot(
        client,
        yahoo,
        require_fresh=True,
        max_tick_age_seconds=cfg.max_tick_age_seconds,
    )


def build_theory_from_signal(
    *,
    run_id: str,
    git_commit: str,
    signal: Any,
    quote: dict[str, Any],
    cfg: MicroFidelityConfig,
    candle_seq: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (theory_event, rejection). Theory is built before any order."""
    sg = spread_gate(quote.get("spread_pips"), cfg.max_spread_pips)
    if not sg["ok"]:
        return None, {"stage": "spread_gate", **sg}

    freeze_px = float(quote.get("point") or 0) * float(quote.get("trade_freeze_level") or 0)
    dist = choose_sl_tp_distance(
        pip_size=float(quote["pip_size"]),
        min_stop_distance_price=float(quote.get("min_stop_distance_price") or 0),
        spread_price=float(quote.get("spread_price") or 0),
        freeze_level_price=freeze_px,
        target_pips=cfg.sl_tp_target_pips,
        max_pips=cfg.max_sl_tp_pips,
    )
    if not dist.get("ok"):
        return None, {"stage": "stop_distance", **dist}

    side = signal.side
    entry_ref = float(quote["ask"]) if side == "buy" else float(quote["bid"])
    levels = plan_symmetric_sl_tp(
        side=side,
        entry_ref=entry_ref,
        distance_price=float(dist["distance_price"]),
        digits=int(quote["digits"]),
        rr=cfg.rr,
    )
    risk = g2.estimate_risk_usd(
        side=side,
        entry=entry_ref,
        sl=float(levels["sl"]),
        volume=cfg.volume_lots,
        contract_size=float(quote.get("contract_size") or 100000),
    )
    distance_meta = {
        **levels,
        "distance_selection": dist,
    }
    tev = build_theory_event(
        run_id=run_id,
        git_commit=git_commit,
        signal=signal.to_dict(),
        quote=quote,
        expected_entry=entry_ref,
        expected_sl=float(levels["sl"]),
        expected_tp=float(levels["tp"]),
        expected_risk_usd=risk,
        distance_meta=distance_meta,
        config=cfg.to_dict(),
        candle_sequence=candle_seq,
        theory_event_id=new_theory_event_id(),
    )
    return tev, None


def run_read_only(cfg: MicroFidelityConfig = FROZEN_CONFIG) -> dict[str, Any]:
    ensure_dirs()
    run_id = new_run_id()
    run_dir = run_dir_for(run_id)
    client = g2.connect_mirror_client()
    account = g2.verify_account(client)
    assert_mirror_identity(account, magic=cfg.magic)
    positions = g2.snapshot_positions(client)
    eurdkk_baseline = snapshot_eurdkk_tickets(positions)
    eurdkk = assert_protected_eurdkk_intact(positions, baseline=eurdkk_baseline)
    exp_pos = filter_experiment_positions(
        positions, comment_prefix=cfg.comment_prefix, magic=cfg.magic
    )
    symbols: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for yahoo in cfg.symbols_yahoo:
        try:
            sym = snapshot_quote(client, yahoo, cfg)
            symbols.append(
                {
                    "yahoo": yahoo,
                    "mt5": sym["mt5"],
                    "bid": sym["bid"],
                    "ask": sym["ask"],
                    "spread_pips": sym.get("spread_pips"),
                    "freshness_status": sym.get("freshness_status"),
                    "min_stop_distance_pips": sym.get("min_stop_distance_pips"),
                    "spread_gate": spread_gate(sym.get("spread_pips"), cfg.max_spread_pips),
                }
            )
        except Exception as exc:  # noqa: BLE001 — per-symbol isolation
            errors.append({"yahoo": yahoo, "error": str(exc)})

    deadline = _utc_now() + timedelta(seconds=cfg.experiment_duration_seconds)
    manifest = {
        "run_id": run_id,
        "mode": "read-only",
        "generated_at_utc": _iso(),
        "git_commit": _git_hash(),
        "strategy_name": cfg.strategy_name,
        "strategy_version": cfg.strategy_version,
        "strategy_parameters": cfg.to_dict(),
        "symbols": list(cfg.symbols_yahoo),
        "timeframe": cfg.timeframe,
        "account": account,
        "magic": cfg.magic,
        "comment_prefix": cfg.comment_prefix,
        "server_offset_seconds": account.get("server_offset_seconds"),
        "start_utc": _iso(),
        "deadline_utc": deadline.isoformat(),
        "orders_submitted": 0,
        "live_execution_enabled": LIVE_EXECUTION_ENABLED,
        "protected_eurdkk": eurdkk,
        "open_experiment_positions": exp_pos,
        "symbol_snapshots": symbols,
        "symbol_errors": errors,
        "safety": {
            "live_8791_untouched": True,
            "magic_88001_untouched": True,
            "eurdkk_untouched": True,
        },
    }
    write_manifest(run_dir, manifest)
    append_heartbeat(
        run_dir,
        {
            "ts": _iso(),
            "alive": True,
            "mode": "read-only",
            "connection": bool(account.get("connected")),
            "account": account.get("login"),
            "open_experiment_positions": len(exp_pos),
            "theory_events": 0,
            "broker_orders": 0,
            "completed_trades": 0,
            "last_error": errors[-1] if errors else None,
        },
    )
    # Detach without shutdown
    client._connected = False
    client._mt5 = None
    print(
        json.dumps(
            {
                "mode": "read-only",
                "run_id": run_id,
                "account": account.get("login"),
                "symbols_ok": len(symbols),
                "symbol_errors": len(errors),
                "open_experiment_positions": len(exp_pos),
                "artifact": str(run_dir),
            },
            indent=2,
        ),
        flush=True,
    )
    return manifest


def _scan_latest_signal(
    *,
    yahoo: str,
    completed: list[Any],
    pip_size: float,
    cfg: MicroFidelityConfig,
    last_signal_candle_utc: str | datetime | None,
) -> tuple[Any | None, str | None]:
    """Return (signal, source_candle_utc_iso) for newest completed bar only."""
    if len(completed) < cfg.lookback_bars + 1:
        return None, None
    curr = completed[-1]
    bars_since = None
    if last_signal_candle_utc is not None:
        if isinstance(last_signal_candle_utc, str):
            last_ts = datetime.fromisoformat(last_signal_candle_utc)
        else:
            last_ts = last_signal_candle_utc
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        bars_since = int(
            (curr.timestamp_utc.astimezone(timezone.utc) - last_ts).total_seconds() // 60
        )
    sig = evaluate_completed_bar(
        symbol=yahoo,
        completed=completed,
        pip_size=pip_size,
        config=cfg,
        bars_since_last_signal=bars_since,
    )
    if sig is None:
        return None, None
    return sig, curr.timestamp_utc.isoformat()


def _normalize_last_signal_map(raw: dict[str, Any]) -> dict[str, str]:
    """Prefer ISO candle timestamps; drop legacy rolling-window integer indices."""
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        if isinstance(v, str) and "T" in v:
            out[str(k)] = v
    return out


def run_dry_run(
    cfg: MicroFidelityConfig = FROZEN_CONFIG,
    *,
    max_loops: int = 3,
) -> dict[str, Any]:
    """Evaluate strategy on live 1m history; persist Theory events; place ZERO orders."""
    ensure_dirs()
    run_id = new_run_id()
    run_dir = run_dir_for(run_id)
    git_commit = _git_hash()
    theory_store = ImmutableJsonlStore(run_dir / "theory_events.jsonl")
    exec_store = ImmutableJsonlStore(run_dir / "broker_executions.jsonl")
    state_path = run_dir / "runner_state.json"

    client = g2.connect_mirror_client()
    account = g2.verify_account(client)
    assert_mirror_identity(account, magic=cfg.magic)
    positions = g2.snapshot_positions(client)
    eurdkk_baseline = snapshot_eurdkk_tickets(positions)
    eurdkk = assert_protected_eurdkk_intact(positions, baseline=eurdkk_baseline)

    deadline = _utc_now() + timedelta(seconds=cfg.experiment_duration_seconds)
    signals_seen = 0
    theory_written = 0
    rejects: list[dict[str, Any]] = []
    last_signal_bar: dict[str, str] = {}
    processed_candle: dict[str, str] = {}

    for loop_i in range(max_loops):
        g2._verify_still_mirror(client)
        assert_protected_eurdkk_intact(
            g2.snapshot_positions(client), baseline=eurdkk_baseline
        )
        for yahoo in cfg.symbols_yahoo:
            try:
                quote = snapshot_quote(client, yahoo, cfg)
                completed, fetch_ok = load_m1_candles(client, yahoo, bars=80)
                if not fetch_ok or len(completed) < cfg.lookback_bars + 1:
                    rejects.append(
                        {
                            "yahoo": yahoo,
                            "reason": "fetch_failed" if not fetch_ok else "insufficient_1m_bars",
                        }
                    )
                    continue
                # Estimate historical signal count (not executed) for visibility
                hist = 0
                tmp_last: str | None = None
                for end in range(cfg.lookback_bars + 1, len(completed) + 1):
                    window = completed[:end]
                    bars_since = None
                    if tmp_last is not None:
                        last_ts = datetime.fromisoformat(tmp_last)
                        bars_since = int(
                            (
                                window[-1].timestamp_utc.astimezone(timezone.utc) - last_ts
                            ).total_seconds()
                            // 60
                        )
                    sig_h = evaluate_completed_bar(
                        symbol=yahoo,
                        completed=window,
                        pip_size=float(quote["pip_size"]),
                        config=cfg,
                        bars_since_last_signal=bars_since,
                    )
                    if sig_h is not None:
                        hist += 1
                        tmp_last = window[-1].timestamp_utc.isoformat()
                if hist:
                    rejects.append(
                        {
                            "yahoo": yahoo,
                            "info": "historical_signal_estimate",
                            "count_in_window": hist,
                        }
                    )

                found, found_key = _scan_latest_signal(
                    yahoo=yahoo,
                    completed=completed,
                    pip_size=float(quote["pip_size"]),
                    cfg=cfg,
                    last_signal_candle_utc=last_signal_bar.get(yahoo),
                )
                if found is None or found_key is None:
                    continue
                candle_key = found.source_candle_timestamp_utc.isoformat()
                if processed_candle.get(yahoo) == candle_key:
                    continue
                signals_seen += 1
                seq = [c.to_dict() for c in completed[-(cfg.lookback_bars + 2) :]]
                tev, rej = build_theory_from_signal(
                    run_id=run_id,
                    git_commit=git_commit,
                    signal=found,
                    quote=quote,
                    cfg=cfg,
                    candle_seq=seq,
                )
                if rej:
                    rejects.append({"yahoo": yahoo, "signal": found.to_dict(), **rej})
                    continue
                assert tev is not None
                dup = reject_if_duplicate_theory(
                    theory_store_has=theory_store.has_id(tev["theory_event_id"]),
                    execution_store_has=False,
                    open_experiment_for_symbol=False,
                )
                if dup:
                    rejects.append({"yahoo": yahoo, "reason": dup})
                    continue
                theory_store.append(tev, id_key="theory_event_id")
                theory_written += 1
                processed_candle[yahoo] = candle_key
                last_signal_bar[yahoo] = found_key
                exe = link_execution_record(
                    theory_event_id=tev["theory_event_id"],
                    execution_id=new_execution_id(),
                    comment=f"{cfg.comment_prefix}{tev['theory_event_id'][-8:]}"[:31],
                    order_result={"ok": False, "dry_run": True, "message": "no_order_dry_run"},
                    position_ticket=None,
                    actual_sl=None,
                    actual_tp=None,
                    entry_deal=None,
                    mode="dry-run",
                )
                exec_store.append(exe, id_key="execution_id")
            except Exception as exc:  # noqa: BLE001
                rejects.append({"yahoo": yahoo, "error": str(exc)})

        append_heartbeat(
            run_dir,
            {
                "ts": _iso(),
                "alive": True,
                "mode": "dry-run",
                "loop": loop_i,
                "connection": True,
                "account": account.get("login"),
                "theory_events": theory_written,
                "broker_orders": 0,
                "signals_seen": signals_seen,
                "rejects": len(rejects),
                "deadline_utc": deadline.isoformat(),
            },
        )
        time.sleep(0.2)

    assert_protected_eurdkk_intact(
        g2.snapshot_positions(client), baseline=eurdkk_baseline
    )
    state_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "mode": "dry-run",
                "last_signal_bar": last_signal_bar,
                "processed_candle": processed_candle,
                "theory_events": theory_written,
                "updated_at": _iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    write_fidelity_report(
        run_dir=run_dir,
        run_id=run_id,
        rows=[],
        manifest={"run_id": run_id},
        early=None,
    )
    manifest = {
        "run_id": run_id,
        "mode": "dry-run",
        "generated_at_utc": _iso(),
        "git_commit": git_commit,
        "strategy_name": cfg.strategy_name,
        "strategy_version": cfg.strategy_version,
        "strategy_parameters": cfg.to_dict(),
        "symbols": list(cfg.symbols_yahoo),
        "timeframe": cfg.timeframe,
        "account": account,
        "magic": cfg.magic,
        "comment_prefix": cfg.comment_prefix,
        "server_offset_seconds": account.get("server_offset_seconds"),
        "start_utc": _iso(),
        "deadline_utc": deadline.isoformat(),
        "orders_submitted": 0,
        "theory_events_written": theory_written,
        "signals_seen": signals_seen,
        "rejects": rejects[:50],
        "live_execution_enabled": LIVE_EXECUTION_ENABLED,
        "protected_eurdkk": eurdkk,
        "safety": {
            "live_8791_untouched": True,
            "magic_88001_untouched": True,
            "eurdkk_untouched": True,
        },
    }
    write_manifest(run_dir, manifest)
    client._connected = False
    client._mt5 = None
    print(
        json.dumps(
            {
                "mode": "dry-run",
                "run_id": run_id,
                "signals_seen": signals_seen,
                "theory_events_written": theory_written,
                "orders_submitted": 0,
                "rejects": len(rejects),
                "artifact": str(run_dir),
            },
            indent=2,
        ),
        flush=True,
    )
    return manifest


def _reconcile_closed(
    client: Any,
    *,
    ticket: int,
    symbol_mt5: str,
    side: str,
    since_utc: datetime,
    theory: dict[str, Any],
    execution: dict[str, Any],
    intentional_cleanup: bool,
) -> dict[str, Any]:
    offset = int(getattr(client, "server_offset_seconds", 0) or 0)
    recon = reconcile_from_mt5(
        client._mt5,
        position_ticket=ticket,
        symbol_mt5=symbol_mt5,
        magic=int(FROZEN_CONFIG.magic),
        offset_seconds=offset,
        since_utc=since_utc,
        expected_side=side,
        include_supplementary=True,
    )
    exit_class = classify_micro_exit(
        exit_deal=recon.get("exit_deal"),
        intentional_cleanup=intentional_cleanup,
        requested_sl=float(theory.get("expected_sl") or 0) or None,
        requested_tp=float(theory.get("expected_tp") or 0) or None,
        timed_out=False,
        position_still_open=bool(client._mt5.positions_get(ticket=ticket)),
    )
    return build_reconciliation_row(
        theory=theory,
        execution=execution,
        reconcile=recon,
        exit_class=exit_class,
        intentional_cleanup=intentional_cleanup,
    )


def run_live(
    cfg: MicroFidelityConfig = FROZEN_CONFIG,
    *,
    resume_run_id: str | None = None,
) -> dict[str, Any]:
    """Continuous Mirror runner. Only callable when LIVE_EXECUTION_ENABLED and --approve."""
    if not LIVE_EXECUTION_ENABLED:
        raise MicroSafetyError("LIVE_EXECUTION_ENABLED is False")

    ensure_dirs()
    run_id = resume_run_id or new_run_id()
    run_dir = run_dir_for(run_id)
    git_commit = _git_hash()
    theory_store = ImmutableJsonlStore(run_dir / "theory_events.jsonl")
    exec_store = ImmutableJsonlStore(run_dir / "broker_executions.jsonl")
    state_path = run_dir / "runner_state.json"

    client = g2.connect_mirror_client()
    account = g2.verify_account(client)
    assert_mirror_identity(account, magic=cfg.magic)
    positions = g2.snapshot_positions(client)
    eurdkk_baseline = snapshot_eurdkk_tickets(positions)
    eurdkk = assert_protected_eurdkk_intact(positions, baseline=eurdkk_baseline)

    m1_archive = ContinuousM1Archive(
        run_dir=run_dir,
        symbols=list(cfg.symbols_yahoo),
        server_offset_seconds=int(account.get("server_offset_seconds") or 0) or None,
    )
    # Seed continuous archive immediately (pre-signal buffer for all symbols).
    seed_stats = poll_m1_archive(client, cfg, m1_archive, bars=180)

    start = _utc_now()
    duration = int(cfg.experiment_duration_seconds)
    deadline = (start + timedelta(seconds=duration)) if duration > 0 else None
    target_n = int(getattr(cfg, "target_completed_trades", 20) or 20)
    last_hb = 0.0
    last_signal_bar: dict[str, str] = {}
    processed_candle: dict[str, str] = {}
    open_map: dict[int, dict[str, Any]] = {}  # ticket -> {theory, execution, since}
    rows: list[dict[str, Any]] = []
    early: dict[str, Any] | None = None
    orders_submitted = 0
    stopping_new = False
    last_error: str | None = None
    m1_poll_stats = {"candles_appended": 0, "gaps_recorded": 0, "fetch_failures": 0}

    # Resume state from same run artifacts when --resume-run is set
    if resume_run_id:
        recon_path = run_dir / "trade_reconciliation.json"
        if recon_path.exists():
            try:
                rows = list(
                    json.loads(recon_path.read_text(encoding="utf-8")).get("rows") or []
                )
            except Exception:
                rows = []
        early_path = run_dir / "early_diagnostic.json"
        if early_path.exists():
            try:
                early = json.loads(early_path.read_text(encoding="utf-8"))
            except Exception:
                early = None
        if state_path.exists():
            try:
                st = json.loads(state_path.read_text(encoding="utf-8"))
                last_signal_bar = _normalize_last_signal_map(st.get("last_signal_bar") or {})
                # Seed cooldown from last processed candle if legacy int indices were dropped
                processed_candle = dict(st.get("processed_candle") or {})
                for sym, candle_iso in processed_candle.items():
                    last_signal_bar.setdefault(str(sym), str(candle_iso))
                orders_submitted = int(st.get("orders_submitted") or 0)
            except Exception:
                pass
        theories = {str(t.get("theory_event_id")): t for t in theory_store.iter_rows()}
        still = {
            int(p["ticket"]): p
            for p in filter_experiment_positions(
                positions, comment_prefix=cfg.comment_prefix, magic=cfg.magic
            )
        }
        done = {
            int(r["broker_position"])
            for r in rows
            if r.get("broker_position") is not None
        }
        for ex in exec_store.iter_rows():
            ticket = ex.get("position_ticket")
            if ticket is None:
                continue
            ticket = int(ticket)
            tev = theories.get(str(ex.get("theory_event_id")))
            if tev is None:
                continue
            if ticket in still:
                if ticket in done:
                    continue
                open_map[ticket] = {
                    "theory": tev,
                    "execution": ex,
                    "since": _utc_now(),
                    "symbol_mt5": still[ticket]["symbol"],
                    "side": tev.get("side")
                    or ("buy" if tev.get("direction") == "long" else "sell"),
                    "cleanup": False,
                }
            elif ticket not in done:
                try:
                    mt5_sym = str(still.get(ticket, {}).get("symbol") or "")
                    if not mt5_sym:
                        mt5_sym = str(tev.get("symbol") or "").replace("=X", "")
                    row = _reconcile_closed(
                        client,
                        ticket=ticket,
                        symbol_mt5=mt5_sym,
                        side=str(
                            tev.get("side")
                            or ("buy" if tev.get("direction") == "long" else "sell")
                        ),
                        since_utc=_utc_now() - timedelta(hours=12),
                        theory=tev,
                        execution=ex,
                        intentional_cleanup=False,
                    )
                    rows.append(row)
                    done.add(ticket)
                except Exception as exc:  # noqa: BLE001
                    last_error = f"resume_reconcile:{exc}"
        if early is None:
            for r in rows:
                if r.get("mapping_status") == "EXACT_MATCH":
                    early = early_diagnostic(r)
                    break
        if not orders_submitted:
            orders_submitted = sum(
                1 for e in exec_store.iter_rows() if (e.get("order_result") or {}).get("ok")
            )

    # Ensure trade windows exist for any open/reattached positions.
    for ticket, meta in open_map.items():
        tev = meta.get("theory") or {}
        exe = meta.get("execution") or {}
        try:
            m1_archive.open_trade_window(
                theory_event_id=str(tev.get("theory_event_id")),
                execution_id=str(exe.get("execution_id") or "") or None,
                broker_ticket=int(ticket),
                symbol=str(tev.get("symbol") or ""),
                signal_time_utc=str(tev.get("signal_timestamp_utc")),
                source_candle_utc=str(tev.get("source_candle_timestamp_utc") or "") or None,
            )
        except Exception:
            pass

    manifest = {
        "run_id": run_id,
        "mode": "live",
        "resumed": bool(resume_run_id),
        "generated_at_utc": _iso(),
        "git_commit": git_commit,
        "strategy_name": cfg.strategy_name,
        "strategy_version": cfg.strategy_version,
        "strategy_parameters": cfg.to_dict(),
        "symbols": list(cfg.symbols_yahoo),
        "timeframe": cfg.timeframe,
        "account": account,
        "magic": cfg.magic,
        "comment_prefix": cfg.comment_prefix,
        "server_offset_seconds": account.get("server_offset_seconds"),
        "start_utc": start.isoformat(),
        "deadline_utc": deadline.isoformat() if deadline else None,
        "duration_seconds": duration,
        "unlimited_time": duration <= 0,
        "target_completed_trades": target_n,
        "max_open_total": cfg.max_open_total,
        "max_open_per_symbol": cfg.max_open_per_symbol,
        "sl_tp_target_pips": cfg.sl_tp_target_pips,
        "orders_submitted": orders_submitted,
        "live_execution_enabled": True,
        "protected_eurdkk": eurdkk,
        "level5b_m1_archive": True,
        "m1_archive": {
            "dir": "m1_archive",
            "data_source": m1_archive.data_source,
            "pre_signal_buffer_minutes": m1_archive.pre_signal_buffer_minutes,
            "post_exit_tail_minutes": m1_archive.post_exit_tail_minutes,
            "seed_stats": seed_stats,
        },
    }
    write_manifest(run_dir, manifest)
    print(
        json.dumps(
            {
                "event": "live_started",
                "run_id": run_id,
                "resumed": bool(resume_run_id),
                "account": account.get("login"),
                "target_completed_trades": target_n,
                "completed_so_far": len(rows),
                "open_reattached": len(open_map),
                "unlimited_time": duration <= 0,
                "sl_tp_target_pips": cfg.sl_tp_target_pips,
                "max_open_total": cfg.max_open_total,
                "m1_archive_seed": seed_stats,
                "artifact": str(run_dir),
            },
            indent=2,
        ),
        flush=True,
    )

    continuity: dict[str, Any] | None = None
    try:
        while True:
            now = _utc_now()
            # Stop new entries once completed+open reaches target (wait for natural SL/TP).
            if len(rows) + len(open_map) >= target_n:
                stopping_new = True
            if deadline is not None and now >= deadline:
                stopping_new = True

            g2._verify_still_mirror(client)
            assert_protected_eurdkk_intact(
                g2.snapshot_positions(client), baseline=eurdkk_baseline
            )

            # Continuous M1 capture EVERY loop for all symbols (trade-independent).
            try:
                m1_poll_stats = poll_m1_archive(client, cfg, m1_archive, bars=180)
            except Exception as exc:  # noqa: BLE001
                last_error = f"m1_archive:{exc}"

            # Monitor / reconcile closed experiment positions
            still_open = {
                int(p["ticket"]): p
                for p in filter_experiment_positions(
                    g2.snapshot_positions(client),
                    comment_prefix=cfg.comment_prefix,
                    magic=cfg.magic,
                )
            }
            for ticket, meta in list(open_map.items()):
                if ticket not in still_open:
                    row = _reconcile_closed(
                        client,
                        ticket=ticket,
                        symbol_mt5=str(meta["symbol_mt5"]),
                        side=str(meta["side"]),
                        since_utc=meta["since"],
                        theory=meta["theory"],
                        execution=meta["execution"],
                        intentional_cleanup=bool(meta.get("cleanup")),
                    )
                    rows.append(row)
                    exit_utc = row.get("broker_exit_utc") or _iso()
                    try:
                        m1_archive.close_trade_window(
                            theory_event_id=str(meta["theory"].get("theory_event_id")),
                            exit_observed_utc=str(exit_utc),
                            broker_ticket=ticket,
                            execution_id=str(
                                (meta.get("execution") or {}).get("execution_id") or ""
                            )
                            or None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        last_error = f"m1_window_close:{exc}"
                    print(
                        json.dumps(
                            {
                                "event": "trade_completed",
                                "n": len(rows),
                                "target": target_n,
                                "symbol": row.get("symbol"),
                                "exit_reason": row.get("broker_exit_reason"),
                                "entry_delta_pips": row.get("entry_delta_pips"),
                                "exit_delta_pips": row.get("exit_delta_pips"),
                                "mapping": row.get("mapping_status"),
                            }
                        ),
                        flush=True,
                    )
                    if early is None and row.get("mapping_status") == "EXACT_MATCH":
                        early = early_diagnostic(row)
                        (run_dir / "early_diagnostic.json").write_text(
                            json.dumps(early, indent=2, default=str), encoding="utf-8"
                        )
                        print(
                            json.dumps(
                                {
                                    "event": "early_diagnostic",
                                    "verdict": early.get("verdict"),
                                }
                            ),
                            flush=True,
                        )
                    del open_map[ticket]
                    write_fidelity_report(
                        run_dir=run_dir,
                        run_id=run_id,
                        rows=rows,
                        manifest=manifest,
                        early=early,
                    )

            # Signal evaluation MUST run every loop (not nested under open_map tickets).
            # Empty open_map previously skipped this entire block (bug).
            if not stopping_new and len(open_map) < cfg.max_open_total:
                for yahoo in cfg.symbols_yahoo:
                    try:
                        exp_open = filter_experiment_positions(
                            g2.snapshot_positions(client),
                            comment_prefix=cfg.comment_prefix,
                            magic=cfg.magic,
                        )
                        sym_open = [
                            p
                            for p in exp_open
                            if str(p.get("symbol") or "").upper()
                            == yahoo.replace("=X", "").upper()
                        ]
                        if len(sym_open) >= cfg.max_open_per_symbol:
                            continue
                        if len(exp_open) >= cfg.max_open_total:
                            break

                        quote = snapshot_quote(client, yahoo, cfg)
                        completed, fetch_ok = load_m1_candles(client, yahoo, bars=180)
                        if not fetch_ok:
                            last_error = f"m1_fetch_failed:{yahoo}"
                            continue
                        sig, sig_candle_key = _scan_latest_signal(
                            yahoo=yahoo,
                            completed=completed,
                            pip_size=float(quote["pip_size"]),
                            cfg=cfg,
                            last_signal_candle_utc=last_signal_bar.get(yahoo),
                        )
                        if sig is None or sig_candle_key is None:
                            continue
                        candle_key = sig.source_candle_timestamp_utc.isoformat()
                        if processed_candle.get(yahoo) == candle_key:
                            continue

                        seq = [c.to_dict() for c in completed[-(cfg.lookback_bars + 2) :]]
                        tev, rej = build_theory_from_signal(
                            run_id=run_id,
                            git_commit=git_commit,
                            signal=sig,
                            quote=quote,
                            cfg=cfg,
                            candle_seq=seq,
                        )
                        if rej:
                            last_error = json.dumps(rej)
                            continue
                        assert tev is not None
                        # Persist Theory BEFORE order
                        theory_store.append(tev, id_key="theory_event_id")
                        processed_candle[yahoo] = candle_key
                        last_signal_bar[yahoo] = sig_candle_key

                        comment = f"{cfg.comment_prefix}{tev['theory_event_id'][-8:]}"[:31]
                        t_submit = _utc_now()
                        result = client.place_market_with_sltp(
                            yahoo,
                            sig.side,
                            cfg.volume_lots,
                            float(tev["expected_sl"]),
                            float(tev["expected_tp"]),
                            comment=comment,
                        )
                        orders_submitted += 1
                        pos = None
                        if result.ok:
                            time.sleep(0.8)
                            for p in filter_experiment_positions(
                                g2.snapshot_positions(client),
                                comment_prefix=cfg.comment_prefix,
                                magic=cfg.magic,
                            ):
                                if p.get("comment") == comment:
                                    pos = p
                                    break
                        exe = link_execution_record(
                            theory_event_id=tev["theory_event_id"],
                            execution_id=new_execution_id(),
                            comment=comment,
                            order_result={
                                "ok": bool(result.ok),
                                "ticket": getattr(result, "ticket", None),
                                "retcode": getattr(result, "retcode", None),
                                "message": getattr(result, "message", None),
                            },
                            position_ticket=int(pos["ticket"]) if pos else None,
                            actual_sl=float(pos["sl"]) if pos else None,
                            actual_tp=float(pos["tp"]) if pos else None,
                            entry_deal=None,
                            mode="live",
                        )
                        exec_store.append(exe, id_key="execution_id")
                        if pos:
                            open_map[int(pos["ticket"])] = {
                                "theory": tev,
                                "execution": exe,
                                "since": t_submit,
                                "symbol_mt5": pos["symbol"],
                                "side": sig.side,
                                "cleanup": False,
                            }
                            try:
                                m1_archive.open_trade_window(
                                    theory_event_id=str(tev["theory_event_id"]),
                                    execution_id=str(exe.get("execution_id") or "") or None,
                                    broker_ticket=int(pos["ticket"]),
                                    symbol=str(tev.get("symbol") or yahoo),
                                    signal_time_utc=str(tev.get("signal_timestamp_utc")),
                                    source_candle_utc=str(
                                        tev.get("source_candle_timestamp_utc") or ""
                                    )
                                    or None,
                                )
                            except Exception as exc:  # noqa: BLE001
                                last_error = f"m1_window_open:{exc}"
                    except Exception as exc:  # noqa: BLE001
                        last_error = str(exc)

            # Timed-deadline cleanup only (unlimited mode waits for natural SL/TP).
            if stopping_new and open_map and deadline is not None and now >= deadline:
                for ticket, meta in list(open_map.items()):
                    try:
                        yahoo = meta["theory"]["symbol"]
                        client.close_position(yahoo, ticket=ticket)
                        meta["cleanup"] = True
                        time.sleep(0.5)
                    except Exception as exc:  # noqa: BLE001
                        last_error = f"cleanup_failed:{exc}"

            if time.time() - last_hb >= cfg.heartbeat_seconds:
                last_hb = time.time()
                append_heartbeat(
                    run_dir,
                    {
                        "ts": _iso(),
                        "alive": True,
                        "mode": "live",
                        "connection": True,
                        "account": account.get("login"),
                        "open_experiment_positions": len(open_map),
                        "theory_events": theory_written_count(theory_store),
                        "broker_orders": orders_submitted,
                        "completed_trades": len(rows),
                        "target_completed_trades": target_n,
                        "stopping_new": stopping_new,
                        "deadline_utc": deadline.isoformat() if deadline else None,
                        "last_error": last_error,
                        "m1_archive": m1_poll_stats,
                    },
                )
                state_path.write_text(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "open_tickets": list(open_map.keys()),
                            "last_signal_bar": last_signal_bar,
                            "processed_candle": processed_candle,
                            "orders_submitted": orders_submitted,
                            "completed_trades": len(rows),
                            "target_completed_trades": target_n,
                            "m1_archive": m1_poll_stats,
                            "updated_at": _iso(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            if len(rows) >= target_n and not open_map:
                break
            # Timed deadline exit after cleanup drained
            if deadline is not None and now >= deadline and stopping_new and not open_map:
                break
            time.sleep(cfg.poll_seconds)
    finally:
        assert_protected_eurdkk_intact(
            g2.snapshot_positions(client), baseline=eurdkk_baseline
        )
        # Final M1 poll + continuity report for Level 5 replay evidence.
        try:
            poll_m1_archive(client, cfg, m1_archive, bars=180)
            continuity = m1_archive.finalize_summary()
        except Exception as exc:  # noqa: BLE001
            continuity = {"error": str(exc)}
            last_error = f"m1_finalize:{exc}"
        write_fidelity_report(
            run_dir=run_dir,
            run_id=run_id,
            rows=rows,
            manifest=manifest,
            early=early,
        )
        manifest["orders_submitted"] = orders_submitted
        manifest["completed_trades"] = len(rows)
        manifest["end_utc"] = _iso()
        manifest["m1_continuity"] = continuity
        write_manifest(run_dir, manifest)
        client._connected = False
        client._mt5 = None

    print(
        json.dumps(
            {
                "mode": "live",
                "run_id": run_id,
                "orders_submitted": orders_submitted,
                "completed_trades": len(rows),
                "early_verdict": (early or {}).get("verdict"),
                "m1_continuity": continuity,
                "artifact": str(run_dir),
            },
            indent=2,
        ),
        flush=True,
    )
    return manifest


def theory_written_count(store: ImmutableJsonlStore) -> int:
    return sum(1 for _ in store.iter_rows())


def run_live_fail_closed(*, approve: bool) -> int:
    msg = (
        "LIVE MODE FAIL-CLOSED for gate2_micro_fidelity.\n"
        f"LIVE_EXECUTION_ENABLED={LIVE_EXECUTION_ENABLED} approve={bool(approve)}\n"
        "No Mirror orders were submitted.\n"
        "After explicit human approval: set LIVE_EXECUTION_ENABLED=True in\n"
        "borex/gate2/micro_fidelity/config.py, then re-run:\n"
        "  python scripts/run_gate2_micro_fidelity.py --mode live --approve\n"
    )
    print(msg, flush=True)
    ensure_dirs()
    run_id = new_run_id()
    run_dir = run_dir_for(run_id)
    write_manifest(
        run_dir,
        {
            "run_id": run_id,
            "mode": "live",
            "blocked": True,
            "reason": "LIVE_EXECUTION_ENABLED is False or missing --approve",
            "LIVE_EXECUTION_ENABLED": LIVE_EXECUTION_ENABLED,
            "approve": bool(approve),
            "orders_submitted": 0,
            "generated_at_utc": _iso(),
            "git_commit": _git_hash(),
        },
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gate 2 micro fidelity Mirror runner")
    ap.add_argument("--mode", choices=("read-only", "dry-run", "live"), required=True)
    ap.add_argument("--approve", action="store_true", help="Required for live (still fail-closed)")
    ap.add_argument("--max-loops", type=int, default=3, help="Dry-run loop count")
    ap.add_argument(
        "--target-trades",
        type=int,
        default=None,
        help="Stop after N completed trades (default from config)",
    )
    ap.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        help="Timed deadline; 0 = unlimited (default from config)",
    )
    ap.add_argument(
        "--sl-tp-pips",
        type=float,
        default=None,
        help="Symmetric SL/TP target pips (default from config)",
    )
    ap.add_argument(
        "--max-open-total",
        type=int,
        default=None,
        help="Max concurrent experiment positions (default from config)",
    )
    ap.add_argument(
        "--resume-run",
        type=str,
        default=None,
        help="Resume an existing g2m_* run id (reattach open positions)",
    )
    args = ap.parse_args(argv)

    cfg = FROZEN_CONFIG
    overrides: dict[str, Any] = {}
    if args.target_trades is not None:
        overrides["target_completed_trades"] = int(args.target_trades)
    if args.duration_seconds is not None:
        overrides["experiment_duration_seconds"] = int(args.duration_seconds)
    if args.sl_tp_pips is not None:
        overrides["sl_tp_target_pips"] = float(args.sl_tp_pips)
    if args.max_open_total is not None:
        overrides["max_open_total"] = int(args.max_open_total)
        overrides["max_open_per_symbol"] = int(args.max_open_total)
    if overrides:
        from dataclasses import replace

        cfg = replace(cfg, **overrides)

    try:
        if args.mode == "read-only":
            run_read_only(cfg)
            return 0
        if args.mode == "dry-run":
            run_dry_run(cfg, max_loops=max(1, int(args.max_loops)))
            return 0
        if args.mode == "live":
            if not LIVE_EXECUTION_ENABLED or not args.approve:
                return run_live_fail_closed(approve=args.approve)
            run_live(cfg, resume_run_id=args.resume_run)
            return 0
    except (g2.SafetyError, MicroSafetyError) as exc:
        print(str(exc), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
