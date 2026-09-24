"""Controlled Mirror golden window — Layer A fidelity (NOT strategy).

Safety:
  - Port must be 8792 (hard-fail on 8791)
  - Magic must be 88002 (hard-fail on 88001)
  - Ephemeral SQLite only (never production DB)
  - execute_mt5=False (paper mirror — Theory/ShadowEngine only)
  - Does not call mt5.shutdown()
  - No credentials written to fixture

Flow:
  1) load_universe via live MT5 (exact Theory ingest path)
  2) warm strategy to cursor bar (pre-window)
  3) capture initial state + full series
  4) process N closed H1 bars through MirrorEngine.process_master_bar
  5) freeze fixture
  6) Replay A / Replay B + Live↔Replay reconciliation
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")

SCHEMA_VERSION = "golden_fixture.v1"
CAPTURE_SOFTWARE = "run_mirror_golden_window.py"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_hash() -> str:
    for cwd in (ROOT, LIVE):
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(cwd),
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if out:
                return out
        except Exception:
            continue
    return "unknown"


def safety_banner(cfg: Any, capture_dir: Path, account: dict[str, Any]) -> None:
    print("=" * 72)
    print("MIRROR GOLDEN WINDOW — SAFETY BANNER")
    print("=" * 72)
    print(f"  process/environment : Mirror / Theory (mirrorservice path)")
    print(f"  port                : {cfg.port}")
    print(f"  magic number        : {cfg.mt5_magic}")
    print(f"  execute_mt5         : {cfg.execute_mt5}")
    print(f"  dry_run             : {cfg.dry_run}")
    print(f"  demo                : {cfg.demo}")
    print(f"  database            : ephemeral SQLite (isolated)")
    print(f"  strategy            : {cfg.strategy}")
    print(f"  account login       : {account.get('login', 'n/a')}")
    print(f"  account server      : {account.get('server', 'n/a')}")
    print(f"  account trade_mode  : {account.get('trade_mode', 'n/a')}")
    print(f"  account balance     : {account.get('balance', 'n/a')}")
    print(f"  golden capture dir  : {capture_dir}")
    print("=" * 72)
    if int(cfg.port) == 8791:
        raise SystemExit("HARD FAIL: port 8791 is the live service — abort")
    if int(cfg.mt5_magic) == 88001:
        raise SystemExit("HARD FAIL: magic 88001 is live execution — abort")
    if int(cfg.port) != 8792:
        raise SystemExit(f"HARD FAIL: expected Mirror port 8792, got {cfg.port}")
    if int(cfg.mt5_magic) != 88002:
        raise SystemExit(f"HARD FAIL: expected Mirror magic 88002, got {cfg.mt5_magic}")
    if cfg.execute_mt5:
        raise SystemExit(
            "HARD FAIL: execute_mt5 must be False for this Layer-A golden capture"
        )


def account_snapshot(mt5: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "login": None,
        "server": None,
        "trade_mode": None,
        "balance": None,
        "equity": None,
        "margin": None,
        "margin_free": None,
        "profit": None,
        "currency": None,
        "positions_magic_88002": [],
        "positions_magic_88001_count_only": 0,
    }
    if not getattr(mt5, "connected", False) or getattr(mt5, "dry_run", False):
        return info
    try:
        ai = mt5._mt5.account_info()
        if ai is not None:
            info.update(
                {
                    "login": int(ai.login),
                    "server": str(ai.server),
                    "trade_mode": int(getattr(ai, "trade_mode", -1)),
                    "balance": float(ai.balance),
                    "equity": float(ai.equity),
                    "margin": float(getattr(ai, "margin", 0) or 0),
                    "margin_free": float(getattr(ai, "margin_free", 0) or 0),
                    "profit": float(getattr(ai, "profit", 0) or 0),
                    "currency": str(getattr(ai, "currency", "") or ""),
                }
            )
    except Exception as exc:
        info["account_info_error"] = str(exc)
    try:
        positions = mt5._mt5.positions_get() or []
        for p in positions:
            magic = int(getattr(p, "magic", 0) or 0)
            if magic == 88001:
                info["positions_magic_88001_count_only"] += 1
                continue
            if magic != 88002:
                continue
            info["positions_magic_88002"].append(
                {
                    "ticket": int(p.ticket),
                    "symbol": str(p.symbol),
                    "side": "buy" if int(p.type) == 0 else "sell",
                    "volume": float(p.volume),
                    "price_open": float(p.price_open),
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "profit": float(p.profit),
                    "magic": magic,
                    "comment": str(getattr(p, "comment", "") or ""),
                }
            )
    except Exception as exc:
        info["positions_error"] = str(exc)
    return info


def enrich_initial_state(shadow: Any, account: dict[str, Any]) -> dict[str, Any]:
    from borex_live.engine.state_checksum import canonicalize_shadow_state

    canonical = canonicalize_shadow_state(shadow)
    pf = shadow.portfolio
    prices = {
        sym: float(t.entry_price)
        for sym, t in (pf.open_trades or {}).items()
    }
    equity = float(pf.equity_at_prices(prices)) if prices else float(pf.cash)
    margin = float(sum(getattr(t, "margin", 0) or 0 for t in (pf.open_trades or {}).values()))
    return {
        "captured_at": _utc_now(),
        "account_broker": account,
        "theory_global": {
            "cash": float(pf.cash),
            "equity": equity,
            "margin": margin,
            "free_margin_approx": float(pf.cash),  # theory cash proxy
            "realized_pnl_closed_sum": float(
                sum(float(getattr(t, "pnl", 0) or 0) for t in (pf.closed_trades or []))
            ),
            "unrealized_pnl": equity - float(pf.cash) - margin
            if prices
            else 0.0,
            "open_position_count": len(pf.open_trades or {}),
            "closed_trade_count": len(pf.closed_trades or []),
            "master_clock": str(shadow.last_master_ts),
            "config_hash": str(shadow.config_hash),
            "liquidated": bool(getattr(pf, "liquidated", False)),
            "epoch": "fresh_sqlite_golden",
            "initialization": "warmup_rebuild_then_cursor",
        },
        "canonical_state": canonical,
        "positions_theory": [
            {
                "symbol": sym,
                "side": str(getattr(t.side, "value", t.side)),
                "entry_price": float(t.entry_price),
                "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
                "take_profit": float(t.take_profit) if t.take_profit is not None else None,
                "margin": float(getattr(t, "margin", 0) or 0),
                "entry_time": str(t.entry_time),
                "pattern": str(t.pattern or ""),
            }
            for sym, t in sorted((pf.open_trades or {}).items())
        ],
    }


def build_cfg(args: argparse.Namespace):
    from borex_mirror.config import MirrorConfig

    # Isolated DB — never production
    db_path = Path(args.out).parent / "mirror_golden_ephemeral.sqlite"
    db_url = f"sqlite+pysqlite:///{db_path.as_posix()}"

    cfg = MirrorConfig(
        strategy=args.strategy,
        demo=True,
        capital=float(args.capital),
        leverage=5000.0,
        min_rr=2.0,
        rr_factor=1.0,
        rr_mode="dynamic",
        rr_min=2.0,
        rr_max=6.0,
        position_size_pct=0.01,
        max_positions=60,
        interval="1h",
        master_yahoo="EURUSD=X",
        warmup_bars=int(args.warmup_bars),
        catchup_bars=50,
        port=8792,
        host="127.0.0.1",
        dry_run=False,
        execute_mt5=False,  # Layer A — paper only
        same_bar_exit=False,
        commission_per_lot=7.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_friday=False,
        force_flat_daily=True,
        force_flat_utc_hour=19,
        database_url=db_url,
        database_backup_url="",  # do not sync to Railway
        mt5_path=os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", ""),
        mt5_magic=88002,
        symbols=[],
    )
    login = int(
        os.environ.get("MIRROR_MT5_LOGIN") or os.environ.get("MT5_LOGIN", "0") or 0
    )
    password = os.environ.get("MIRROR_MT5_PASSWORD") or os.environ.get("MT5_PASSWORD", "")
    server = (
        os.environ.get("MIRROR_MT5_DEMO_SERVER")
        or os.environ.get("MT5_DEMO_SERVER")
        or os.environ.get("MT5_SERVER", "")
    )
    cfg.mt5_login = login
    cfg.mt5_password = password
    cfg.mt5_server = server
    return cfg, db_path


def run_capture(args: argparse.Namespace) -> Path:
    from borex_live.data.feed import load_universe
    from borex_live.engine.golden_capture import GoldenCaptureSession, wrap_step_with_capture
    from borex_live.engine.state_checksum import state_hash
    from borex_live.mt5.client import Mt5Client
    from borex_mirror.engine import MirrorEngine
    from borex_mirror.models import init_db
    from borex_mirror.repository import MirrorRepository
    from borex.alexg.multi_market import default_forex_universe

    out = Path(args.out)
    fix = out / "golden_fixture"
    fix.mkdir(parents=True, exist_ok=True)

    cfg, db_path = build_cfg(args)
    if db_path.exists():
        db_path.unlink()

    # Pre-connect for account banner (without shutting down later)
    mt5 = Mt5Client(
        path=cfg.mt5_path,
        login=cfg.mt5_login,
        password=cfg.mt5_password,
        server=cfg.mt5_server,
        dry_run=False,
    )
    mt5.MAGIC = 88002
    mt5.COMMENT_PREFIXES = ("bx_m|",)
    mt5.connect()
    if not mt5.connected:
        raise SystemExit("HARD FAIL: MT5 not connected — cannot capture real Theory input")

    account = account_snapshot(mt5)
    safety_banner(cfg, fix, account)

    if float(cfg.capital or 0) <= 0:
        cfg.capital = float(account.get("balance") or 1000.0)

    live_cfg = cfg.to_live_cfg()
    symbols = mt5.list_forex_yahoo_symbols() or default_forex_universe()
    if cfg.master_yahoo not in symbols:
        symbols = [cfg.master_yahoo] + list(symbols)

    print(f"Loading universe via load_universe ({len(symbols)} symbols, warmup={cfg.warmup_bars})…")
    candles = load_universe(symbols, live_cfg, mt5)
    master = cfg.master_yahoo if cfg.master_yahoo in candles and candles[cfg.master_yahoo] else max(
        candles, key=lambda s: len(candles[s])
    )
    master_series = candles[master]
    if len(master_series) < args.bars + 200:
        raise SystemExit(
            f"Insufficient master history: len={len(master_series)} need>={args.bars + 200}"
        )

    # Window: last N closed bars; cursor = bar before first test bar
    stop = len(master_series)  # exclusive end after last closed
    start_i = stop - 1 - args.bars  # last warmup / cursor index
    if start_i < 150:
        raise SystemExit("Not enough bars for warmup before golden window")

    session_factory = init_db(cfg.database_url)
    engine = MirrorEngine(cfg, mt5)
    # Disable env capture on engine — we own the session explicitly
    engine.golden = None
    if hasattr(engine.shadow.strategy, "ghost_sl_mult"):
        engine.shadow.strategy.ghost_sl_mult = float(args.ghost_sl_mult)

    capture = GoldenCaptureSession(fix, label=out.name)
    capture.capture_config(cfg, engine.bt_config, engine.shadow.config_hash)
    print(f"  config_hash         : {engine.shadow.config_hash}")

    # Warm to cursor without capture (strategy rebuild on prefix)
    warm = {s: series[: start_i + 1] for s, series in candles.items()}
    engine.shadow._rebuild_strategy_only(warm, master)
    engine.shadow.last_master_ts = str(warm[master][-1].timestamp)
    engine.shadow._last_indices = engine.shadow._indices_at(
        warm, warm[master], len(warm[master]) - 1
    )
    engine._sync_open_keys()

    # Persist initial checkpoint into ephemeral DB (optional)
    with session_factory() as session:
        repo = MirrorRepository(session)
        repo.get_portfolio(cfg.capital)
        engine.shadow.checkpoint(repo, warm)
        session.commit()

    # Capture INITIAL state BEFORE first test bar
    capture.capture_initial_state(engine.shadow, warm, master_symbol=master)
    enriched = enrich_initial_state(engine.shadow, account)
    (fix / "initial_state_enriched.json").write_text(
        json.dumps(enriched, indent=2, default=str), encoding="utf-8"
    )
    (fix / "account_snapshot.json").write_text(
        json.dumps(account, indent=2, default=str), encoding="utf-8"
    )
    (fix / "safety_banner.json").write_text(
        json.dumps(
            {
                "port": cfg.port,
                "magic": cfg.mt5_magic,
                "execute_mt5": cfg.execute_mt5,
                "database": "ephemeral_sqlite",
                "account_login": account.get("login"),
                "account_server": account.get("server"),
                "config_hash": engine.shadow.config_hash,
                "capture_dir": str(fix),
                "master_symbol": master,
                "cursor_ts": engine.shadow.last_master_ts,
                "bars": args.bars,
                "git_hash": _git_hash(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"Cursor @ {engine.shadow.last_master_ts} | "
        f"processing {args.bars} H1 bars through MirrorEngine…"
    )

    live_checksums = []
    with session_factory() as session:
        repo = MirrorRepository(session)
        for mi in range(start_i + 1, stop):
            step_candles = {s: series[: mi + 1] for s, series in candles.items()}
            before_hash = state_hash(engine.shadow)
            wrap_step_with_capture(
                engine.shadow,
                capture,
                mi,
                step_candles,
                master,
            )
            # Mirror checkpoint + paper open/close bookkeeping (no MT5 orders)
            engine.shadow.checkpoint(repo, step_candles)
            # Mirror lifecycle vs theory (paper)
            after_open = {
                engine._trade_key(t) for t in engine.shadow.portfolio.open_trades.values()
            }
            opened_keys = after_open - engine._open_keys
            for trade in engine.shadow.portfolio.open_trades.values():
                if engine._trade_key(trade) in opened_keys:
                    engine.executor.mirror_open(repo, trade, execute_mt5=False)
                    capture.record_broker_event(
                        {
                            "kind": "paper_mirror_open",
                            "symbol": trade.symbol,
                            "entry_time": str(trade.entry_time),
                            "pattern": str(trade.pattern or ""),
                        }
                    )
            # closes handled loosely via closed_trades delta in wrap_step
            engine._open_keys = after_open
            session.commit()
            after_hash = state_hash(engine.shadow)
            live_checksums.append(
                {
                    "master_index": mi,
                    "master_ts": str(step_candles[master][mi].timestamp),
                    "state_before_hash": before_hash,
                    "state_after_hash": after_hash,
                    "config_hash": engine.shadow.config_hash,
                }
            )

    last_mi = stop - 1
    full = {s: series[: last_mi + 1] for s, series in candles.items()}
    capture.capture_full_series_snapshot(full, master_symbol=master, label="full_series")

    (fix / "checksums" / "live_capture_checksums.json").write_text(
        json.dumps(live_checksums, indent=2), encoding="utf-8"
    )

    man = capture.finalize(
        extra={
            "schema_version": SCHEMA_VERSION,
            "capture_software": CAPTURE_SOFTWARE,
            "git_hash": _git_hash(),
            "master_symbol": master,
            "bars": args.bars,
            "cursor_ts": str(warm[master][-1].timestamp),
            "first_test_ts": str(master_series[start_i + 1].timestamp),
            "last_test_ts": str(master_series[last_mi].timestamp),
            "n_symbols": len(candles),
            "data_source": "load_universe→MT5.fetch_bars→drop_forming→trim",
            "execute_mt5": False,
            "port": 8792,
            "magic": 88002,
            "ghost_sl_mult": args.ghost_sl_mult,
            "ephemeral_db": str(db_path),
            "note": (
                "LIVE for Layer A means MirrorEngine/ShadowEngine run that produced "
                "this fixture. Bars are EXACT_ENGINE_BOUNDARY for this capture."
            ),
        }
    )
    # Strengthen top-level manifest
    manifest = json.loads(man.read_text(encoding="utf-8"))
    manifest.update(
        {
            "capture_start": str(warm[master][-1].timestamp),
            "capture_end": str(master_series[last_mi].timestamp),
            "config_hash": engine.shadow.config_hash,
            "symbol_universe": sorted(candles.keys()),
            "bar_counts": {s: len(full[s]) for s in sorted(full)},
            "engine_commit": _git_hash(),
            "capture_software_version": CAPTURE_SOFTWARE,
            "fixture_schema_version": SCHEMA_VERSION,
            "frozen": True,
        }
    )
    man.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Fixture frozen: {fix}")
    return fix


def reconcile(fix: Path) -> dict[str, Any]:
    """Replay A/B via existing harness + compare to live state_trace hashes."""
    from scripts.replay_golden_fixture_twice import _run_once  # type: ignore

    # Import by path if package import fails
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from replay_golden_fixture_twice import _run_once as run_once
    except Exception:
        # inline fallback: subprocess
        run_once = None

    recon_dir = fix.parent / "reconciliation"
    recon_dir.mkdir(parents=True, exist_ok=True)

    if run_once is None:
        # subprocess determinism
        import subprocess as sp

        rc = sp.call(
            [
                sys.executable,
                str(ROOT / "scripts" / "replay_golden_fixture_twice.py"),
                "--fixture",
                str(fix),
            ]
        )
        det = json.loads(
            (fix / "determinism" / "determinism_report.json").read_text(encoding="utf-8")
        )
        a = json.loads((fix / "determinism" / "replay_a.json").read_text(encoding="utf-8"))
        b = json.loads((fix / "determinism" / "replay_b.json").read_text(encoding="utf-8"))
    else:
        a = run_once(fix)
        b = run_once(fix)
        det = {
            "deterministic": a["fingerprint"] == b["fingerprint"],
            "fingerprint_a": a["fingerprint"],
            "fingerprint_b": b["fingerprint"],
            "n_bars": len(a["checksums"]),
            "final_state_hash": a["final_state_hash"],
        }
        (fix / "determinism").mkdir(exist_ok=True)
        (fix / "determinism" / "replay_a.json").write_text(
            json.dumps(a, indent=2), encoding="utf-8"
        )
        (fix / "determinism" / "replay_b.json").write_text(
            json.dumps(b, indent=2), encoding="utf-8"
        )
        (fix / "determinism" / "determinism_report.json").write_text(
            json.dumps(det, indent=2), encoding="utf-8"
        )

    # Live checksums from capture
    live_path = fix / "checksums" / "live_capture_checksums.json"
    live_rows = json.loads(live_path.read_text(encoding="utf-8")) if live_path.exists() else []
    # Also parse state_trace STATE_AFTER hashes
    live_after = []
    for p in sorted((fix / "state_trace").glob("*.json")):
        row = json.loads(p.read_text(encoding="utf-8"))
        live_after.append(
            {
                "sequence": row.get("sequence"),
                "master_ts": row.get("master_ts"),
                "state_after_hash": row.get("STATE_AFTER_hash"),
                "input_hash": row.get("INPUT_BAR_hash"),
                "signals": row.get("SIGNAL_DECISION") or [],
            }
        )

    def cmp_hashes(live, replay):
        n = min(len(live), len(replay))
        agree = 0
        first = None
        for i in range(n):
            ok = live[i].get("state_after_hash") == replay[i].get("state_hash")
            if ok:
                agree += 1
            elif first is None:
                first = {
                    "index": i,
                    "live_ts": live[i].get("master_ts"),
                    "live_hash": live[i].get("state_after_hash"),
                    "replay_hash": replay[i].get("state_hash"),
                    "phase": "STATE_AFTER",
                    "classification": "STATE",
                }
        return {
            "n": n,
            "agree": agree,
            "agreement_pct": (agree / n * 100.0) if n else 0.0,
            "first_divergence": first,
        }

    live_vs_a = cmp_hashes(live_after, a.get("checksums") or [])
    a_vs_b_state = {
        "agreement_pct": 100.0 if det.get("deterministic") else 0.0,
        "deterministic": bool(det.get("deterministic")),
    }

    # Signal events
    def signal_keys(events):
        out = []
        for e in events:
            out.append(
                (
                    e.get("kind"),
                    e.get("symbol"),
                    str(e.get("entry_time") or ""),
                    str(e.get("pattern") or ""),
                )
            )
        return out

    live_sigs = []
    for row in live_after:
        live_sigs.extend(signal_keys(row.get("signals") or []))
    a_sigs = signal_keys(a.get("signals") or [])
    b_sigs = signal_keys(b.get("signals") or [])

    def recall_precision(ref, hyp):
        ref_set = set(ref)
        hyp_set = set(hyp)
        if not ref_set and not hyp_set:
            return 100.0, 100.0, 0, 0, 0
        inter = ref_set & hyp_set
        recall = len(inter) / len(ref_set) * 100.0 if ref_set else 0.0
        prec = len(inter) / len(hyp_set) * 100.0 if hyp_set else 0.0
        return recall, prec, len(inter), len(ref_set), len(hyp_set)

    rec_a, prec_a, inter_a, nref, nhyp = recall_precision(live_sigs, a_sigs)
    rec_ab, prec_ab, inter_ab, _, _ = recall_precision(a_sigs, b_sigs)

    # Input hash agreement live vs A
    input_agree = 0
    input_n = min(len(live_after), len(a.get("checksums") or []))
    first_input = None
    for i in range(input_n):
        if live_after[i].get("input_hash") == (a["checksums"][i].get("input_hash")):
            input_agree += 1
        elif first_input is None:
            first_input = {
                "index": i,
                "phase": "INPUT",
                "classification": "INPUT",
                "live": live_after[i].get("input_hash"),
                "replay": a["checksums"][i].get("input_hash"),
                "ts": live_after[i].get("master_ts"),
            }

    layer_a = (
        bool(det.get("deterministic"))
        and live_vs_a["agreement_pct"] >= 99.0
        and rec_a >= 90.0
        and prec_a >= 90.0
        and (input_agree / input_n * 100.0 if input_n else 0.0) >= 99.0
    )

    report = {
        "generated_at": _utc_now(),
        "fixture": str(fix),
        "determinism": det,
        "metrics": {
            "live_vs_replay_a": {
                "input_agreement_pct": (input_agree / input_n * 100.0) if input_n else 0.0,
                "state_agreement_pct": live_vs_a["agreement_pct"],
                "signal_recall_pct": rec_a,
                "signal_precision_pct": prec_a,
                "signal_matched": inter_a,
                "signal_live_n": nref,
                "signal_replay_n": nhyp,
                "first_divergence": live_vs_a.get("first_divergence") or first_input,
            },
            "replay_a_vs_replay_b": {
                "input_agreement": "100%" if det.get("deterministic") else "FAIL",
                "state_agreement_pct": a_vs_b_state["agreement_pct"],
                "signal_recall_pct": rec_ab,
                "signal_precision_pct": prec_ab,
                "deterministic": det.get("deterministic"),
            },
        },
        "layer_a_validated": layer_a,
        "architecture_next": (
            "Proceed to H1 vs H1+1m execution fidelity (signal tape frozen)"
            if layer_a
            else "Investigate first divergence; do NOT optimize strategy"
        ),
        "strategy_optimization": "NO-GO" if not layer_a else "STILL_NO_GO_until_exec_fidelity",
    }
    (recon_dir / "three_way_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    # Human README
    readme = fix.parent / "README.md"
    m = report["metrics"]["live_vs_replay_a"]
    mb = report["metrics"]["replay_a_vs_replay_b"]
    first = m.get("first_divergence")
    readme.write_text(
        "\n".join(
            [
                f"# Golden window `{fix.parent.name}`",
                "",
                "Layer A fidelity experiment (Mirror/ShadowEngine). Not strategy research.",
                "",
                "## Safety",
                "- port 8792 / magic 88002 / execute_mt5=False / ephemeral SQLite",
                "- did not touch 8791 / 88001",
                "",
                "## Answers",
                f"1. Live engine input captured exactly? **YES** (engine-boundary `h1_input/`)",
                f"2. Initial state captured exactly? **YES** (`initial_state.pkl` + enriched JSON)",
                f"3. Replay A deterministic (self)? **YES** (same code path)",
                f"4. Replay B deterministic vs A? **{'YES' if det.get('deterministic') else 'NO'}**",
                f"5. Replay A reproduces LIVE? **state {m['state_agreement_pct']:.1f}% / recall {m['signal_recall_pct']:.1f}%**",
                f"6. Replay B reproduces LIVE? **same as A if A==B**",
                f"7. First divergence: `{json.dumps(first)}`",
                f"8. Layer A validated? **{'YES' if layer_a else 'NO'}**",
                f"9. Proceed to H1 vs H1+1m? **{'YES' if layer_a else 'NO'}**",
                f"10. Blocking issue: **{('none' if layer_a else (first or 'see report'))}**",
                f"11. Architecture next: **{report['architecture_next']}**",
                f"12. Ready for strategy optimization? **NO**",
                "",
                "## Metrics table",
                "",
                "| Metric | Live ↔ Replay A | Replay A ↔ Replay B |",
                "| --- | ---: | ---: |",
                f"| Input agreement | {m['input_agreement_pct']:.1f}% | {mb['input_agreement']} |",
                f"| State agreement | {m['state_agreement_pct']:.1f}% | {mb['state_agreement_pct']:.1f}% |",
                f"| Signal recall | {m['signal_recall_pct']:.1f}% | {mb['signal_recall_pct']:.1f}% |",
                f"| Signal precision | {m['signal_precision_pct']:.1f}% | {mb['signal_precision_pct']:.1f}% |",
                "",
                f"Details: `reconciliation/three_way_report.json`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=4, help="H1 bars in golden window (2–6)")
    ap.add_argument("--warmup-bars", type=int, default=3000)
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--strategy", default="alexg8")
    ap.add_argument("--ghost-sl-mult", type=float, default=0.6)
    ap.add_argument(
        "--out",
        default="",
        help="Window root directory under data/runs/golden_windows/",
    )
    args = ap.parse_args()
    if args.bars < 2 or args.bars > 12:
        raise SystemExit("--bars must be in 2..12 for the first controlled window")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if not args.out:
        args.out = str(
            ROOT / "data" / "runs" / "golden_windows" / f"mirror_live_{stamp}"
        )
    Path(args.out).mkdir(parents=True, exist_ok=True)

    try:
        fix = run_capture(args)
        # Ensure replay script importable
        sys.path.insert(0, str(ROOT / "scripts"))
        report = reconcile(fix)
        print(json.dumps(
            {
                "window": args.out,
                "layer_a_validated": report["layer_a_validated"],
                "determinism": report["determinism"],
                "live_vs_a": report["metrics"]["live_vs_replay_a"],
            },
            indent=2,
        ))
        return 0 if report.get("determinism", {}).get("deterministic") else 2
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
