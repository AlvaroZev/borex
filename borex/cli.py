from __future__ import annotations

import argparse
import json
import sys

from borex import __version__
from borex.config import BacktestConfig, LiveConfig, IterationConfig, make_backtest_config, make_iteration_config, make_live_config
from borex.data import download_all, download_symbol, list_cached
from borex.data.audit import audit_all, audit_dataset, repair_manifests
from borex.data.repair import repair_all as repair_all_data
from borex.data.symbols import FOREX_PAIRS
from borex.data.timeframes import SUPPORTED_TIMEFRAMES
from borex.runner.mass import MassRunConfig, run_mass
from borex.runner.parallel import default_workers
from borex.runner.results_db import leaderboard, list_results
from borex.optimize import SweepConfig, run_param_sweep
from borex.runner.walk_forward import RollingWalkForwardConfig, run_rolling_walk_forward, run_walk_forward
from borex.strategy.registry import get_strategy, list_strategies


from borex.data.dukascopy import import_dukascopy_csv


def _add_cost_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--commission", type=float, default=0.0, help="Commission per side (fraction of notional)")
    p.add_argument("--slippage", type=float, default=0.0, help="Slippage per side (fraction of price)")


def _add_realism_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--spread",
        type=float,
        default=0.0,
        help="Half-spread per side as price fraction (e.g. 0.00005 ~ 0.5 pip)",
    )
    p.add_argument(
        "--slippage-mode",
        choices=("fixed", "atr"),
        default="fixed",
        help="Slippage model: fixed pct or ATR-scaled",
    )
    p.add_argument(
        "--fill-mode",
        choices=("close", "next_open"),
        default="close",
        help="Fill at signal bar close or next bar open",
    )
    p.add_argument(
        "--entry-delay",
        type=int,
        default=0,
        help="Bars of latency before entry fill",
    )


def _add_risk_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--size-mode",
        choices=("fixed", "atr_risk", "kelly"),
        default="fixed",
        help="Position sizing: fixed, atr_risk, or kelly",
    )
    p.add_argument(
        "--risk-per-trade",
        type=float,
        default=0.01,
        help="Equity fraction risked to stop (atr_risk mode)",
    )
    p.add_argument(
        "--max-daily-loss",
        type=float,
        default=None,
        help="Halt new entries after this daily loss fraction (e.g. 0.05)",
    )
    p.add_argument(
        "--max-drawdown",
        type=float,
        default=None,
        help="Halt new entries after this peak drawdown fraction (e.g. 0.2)",
    )
    p.add_argument(
        "--max-currency-exposure",
        type=int,
        default=1,
        help="Max net same-direction bets per currency",
    )
    p.add_argument(
        "--no-correlation-limit",
        action="store_true",
        help="Disable currency exposure limits",
    )


def _backtest_config_from_args(args: argparse.Namespace) -> BacktestConfig:
    return make_backtest_config(
        leverage=args.leverage,
        initial_capital=args.capital,
        commission_pct=args.commission,
        slippage_pct=args.slippage,
        size_mode=getattr(args, "size_mode", "fixed"),
        risk_per_trade_pct=getattr(args, "risk_per_trade", 0.01),
        max_daily_loss_pct=getattr(args, "max_daily_loss", None),
        max_drawdown_pct=getattr(args, "max_drawdown", None),
        max_currency_exposure=getattr(args, "max_currency_exposure", 1),
        correlation_limit=not getattr(args, "no_correlation_limit", False),
        spread_pct=getattr(args, "spread", 0.0),
        slippage_mode=getattr(args, "slippage_mode", "fixed"),
        fill_mode=getattr(args, "fill_mode", "close"),
        entry_delay_bars=getattr(args, "entry_delay", 0),
    )


def _add_live_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--max-errors",
        type=int,
        default=3,
        help="Kill session after this many consecutive tick errors",
    )
    p.add_argument(
        "--stale-minutes",
        type=int,
        default=180,
        help="Kill if data older than this many minutes",
    )
    p.add_argument(
        "--divergence-warn",
        type=float,
        default=0.20,
        help="Alert when live return diverges this fraction from baseline (0.2 = 20%%)",
    )
    p.add_argument(
        "--no-kill-on-liquidation",
        action="store_true",
        help="Do not auto-kill on liquidation",
    )
    p.add_argument(
        "--no-kill-on-halt",
        action="store_true",
        help="Do not auto-kill on risk circuit breaker",
    )


def _live_config_from_args(args: argparse.Namespace) -> LiveConfig:
    return make_live_config(
        max_consecutive_errors=getattr(args, "max_errors", 3),
        stale_data_minutes=getattr(args, "stale_minutes", 180),
        divergence_warn_pct=getattr(args, "divergence_warn", 0.20),
        kill_on_liquidation=not getattr(args, "no_kill_on_liquidation", False),
        kill_on_halt=not getattr(args, "no_kill_on_halt", False),
    )


def cmd_import(args: argparse.Namespace) -> int:
    from borex.data.store import load_ohlcv

    path = import_dukascopy_csv(
        args.file,
        symbol=args.symbol,
        timeframe=args.timeframe,
    )
    sym = args.symbol
    tf = args.timeframe
    if not sym or not tf:
        from borex.data.dukascopy import parse_dukascopy_filename
        from pathlib import Path
        parsed = parse_dukascopy_filename(Path(args.file))
        if parsed:
            sym, tf = parsed
    if sym and tf:
        df = load_ohlcv(sym, tf)
        print(f"Imported -> {path}")
        print(f"  {sym} {tf}: {len(df)} bars, {df.index.min()} .. {df.index.max()}")
    else:
        print(f"Imported -> {path}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    if args.all:
        results = download_all(force=args.force, delay=args.delay)
        ok = sum(1 for r in results if r.get("status") == "ok")
        print(f"Downloaded {ok}/{len(results)} datasets")
        for r in results:
            if r.get("status") == "ok":
                print(f"  OK  {r['symbol']} {r['timeframe']} -> {r['bars']} bars")
            else:
                print(f"  ERR {r['symbol']} {r['timeframe']}: {r.get('error')}")
        return 0 if ok == len(results) else 1

    if not args.symbol or not args.timeframe:
        print("Provide --symbol and --timeframe, or use --all", file=sys.stderr)
        return 1
    path = download_symbol(args.symbol, args.timeframe, force=args.force)
    print(f"Saved: {path}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from borex.backtest.engine import BacktestEngine
    from borex.data.mtf import load_bias_dfs
    from borex.data.store import load_ohlcv
    from borex.strategy.mtf import is_mtf_strategy, validate_mtf_entry

    params = json.loads(args.params) if args.params else None
    strategy = get_strategy(args.strategy, params)
    validate_mtf_entry(strategy, args.timeframe)
    df = load_ohlcv(args.symbol, args.timeframe)
    htf_dfs = None
    if is_mtf_strategy(type(strategy)):
        htf_dfs = load_bias_dfs(args.symbol, strategy.mtf_spec().bias_timeframes)
    engine = BacktestEngine(_backtest_config_from_args(args))
    result = engine.run(
        strategy, df, symbol=args.symbol, timeframe=args.timeframe, htf_dfs=htf_dfs
    )
    from borex.backtest.regime import analyze_trades_by_regime
    from borex.data.manifest import get_dataset_hash

    out = result.to_dict()
    out["dataset_hash"] = get_dataset_hash(args.symbol, args.timeframe)
    if args.regimes:
        out["regimes"] = analyze_trades_by_regime(result, df)
    print(json.dumps(out, indent=2))
    return 0


def cmd_mass(args: argparse.Namespace) -> int:
    bt = _backtest_config_from_args(args)
    cfg = MassRunConfig(
        strategies=args.strategies.split(",") if args.strategies else None,
        symbols=args.symbols.split(",") if args.symbols else None,
        timeframes=args.timeframes.split(",") if args.timeframes else None,
        workers=args.workers,
        backtest_config=bt,
    )
    results = run_mass(cfg)
    ok = [r for r in results if "error" not in r]
    print(f"Completed {len(ok)}/{len(results)} backtests")
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    from borex.strategy.mtf import validate_mtf_entry

    params = json.loads(args.params) if args.params else None
    strategy = get_strategy(args.strategy, params)
    validate_mtf_entry(strategy, args.timeframe)
    bt = _backtest_config_from_args(args)

    if args.rolling:
        sweep_params = args.sweep_params.split(",") if args.sweep_params else None
        summary = run_rolling_walk_forward(
            strategy,
            args.symbol,
            args.timeframe,
            config=bt,
            wf=RollingWalkForwardConfig(
                train_months=args.train_months,
                test_months=args.test_months,
                step_months=args.step_months,
                optimize_on_train=args.optimize,
                sweep_params=sweep_params,
                max_points=args.max_points,
                max_combos=args.max_combos,
                min_reward_risk_ratio=getattr(args, "min_rr", 0.0),
                train_sweep_workers=getattr(args, "train_workers", 0),
                fold_workers=getattr(args, "fold_workers", 0),
            ),
            save=args.save,
            include_regimes=not args.no_regimes,
        )
        print(json.dumps(summary.to_dict(), indent=2))
        return 0

    train, test = run_walk_forward(strategy, args.symbol, args.timeframe, config=bt)
    print(json.dumps({"train": train.to_dict(), "test": test.to_dict()}, indent=2))
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    from borex.strategy.mtf import validate_mtf_entry

    validate_mtf_entry(get_strategy(args.strategy), args.timeframe)
    sweep_params = args.sweep_params.split(",") if args.sweep_params else None
    bt = _backtest_config_from_args(args)
    cfg = SweepConfig(
        strategy=args.strategy,
        symbol=args.symbol,
        timeframe=args.timeframe,
        sweep_params=sweep_params,
        max_points=args.max_points,
        max_combos=args.max_combos,
        workers=args.workers,
        backtest_config=bt,
        metric=args.metric,
        min_reward_risk_ratio=getattr(args, "min_rr", 0.0),
    )
    results, best = run_param_sweep(cfg, save=not args.no_save)
    ok = [r for r in results if "error" not in r]
    print(f"Sweep completed {len(ok)}/{len(results)}")
    if best:
        print(f"Best ({args.metric}): {json.dumps(best, indent=2)}")
    elif args.json:
        print(json.dumps(results[:20], indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.cached:
        print(json.dumps(list_cached(), indent=2))
        return 0
    if args.strategies:
        print(json.dumps(list_strategies(), indent=2))
        return 0
    if args.results:
        print(json.dumps(list_results(limit=args.limit), indent=2))
        return 0
    if args.leaderboard:
        print(json.dumps(leaderboard(metric=args.metric, limit=args.limit), indent=2))
        return 0
    print("Use --cached, --strategies, --results, or --leaderboard")
    return 1


def cmd_audit(args: argparse.Namespace) -> int:
    if args.fix:
        results = repair_all_data()
        ok = sum(1 for r in results if r.get("status") == "ok")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        print(f"Repaired {ok} dataset(s), skipped {skipped}")
        if args.json:
            print(json.dumps(results, indent=2))
        reports = audit_all()
        ok_a = sum(1 for r in reports if r.status == "ok")
        warn = sum(1 for r in reports if r.status == "warn")
        fail = sum(1 for r in reports if r.status == "fail")
        print(f"Post-repair audit: {ok_a} ok, {warn} warn, {fail} fail")
        if args.strict and fail:
            return 1
        return 0

    if args.repair_manifests:
        repaired = repair_manifests()
        print(f"Repaired {len(repaired)} manifest(s)")
        if args.json:
            print(json.dumps(repaired, indent=2))
        return 0

    if args.all:
        reports = audit_all()
    elif args.symbol and args.timeframe:
        reports = [audit_dataset(args.symbol, args.timeframe)]
    else:
        print("Provide --symbol and --timeframe, or use --all", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        ok = sum(1 for r in reports if r.status == "ok")
        warn = sum(1 for r in reports if r.status == "warn")
        fail = sum(1 for r in reports if r.status == "fail")
        print(f"Audit: {ok} ok, {warn} warn, {fail} fail ({len(reports)} datasets)")
        for r in sorted(reports, key=lambda x: (x.status != "fail", x.status != "warn", x.symbol)):
            icon = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}[r.status]
            line = f"  [{icon}] {r.symbol} {r.timeframe}: {r.bars} bars"
            if r.completeness_pct is not None:
                line += f", {r.completeness_pct}% complete"
            if r.manifest_hash:
                line += f", hash={r.manifest_hash[:12]}..."
            print(line)
            for issue in r.issues:
                print(f"         - {issue}")

    if args.strict and any(r.status == "fail" for r in reports):
        return 1
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    from borex.runner.decision_log import list_decisions
    from borex.runner.paper import (
        create_session,
        get_monitor_status,
        kill_session,
        list_sessions,
        paper_tick,
        resume_session,
        run_paper_loop,
    )

    if args.list:
        print(json.dumps(list_sessions(limit=args.limit), indent=2))
        return 0

    if args.session and args.decisions:
        print(json.dumps(list_decisions(args.session, limit=args.limit), indent=2))
        return 0

    if args.session and args.monitor:
        print(json.dumps(get_monitor_status(args.session), indent=2))
        return 0

    if args.session and args.kill:
        session = kill_session(args.session, reason=args.kill_reason or "manual")
        print(json.dumps({"session_id": session.id, "status": session.status, "reason": session.kill_switch.state.reason}, indent=2))
        return 0

    if args.session and args.resume:
        session = resume_session(args.session)
        print(json.dumps({"session_id": session.id, "status": session.status}, indent=2))
        return 0

    if args.session:
        if args.loop:
            run_paper_loop(args.session, poll_seconds=args.poll, max_ticks=args.max_ticks)
            return 0
        out = paper_tick(args.session, refresh_data=not args.no_refresh)
        print(json.dumps(out, indent=2))
        return 0

    if not args.strategy or not args.symbol:
        print("Provide --strategy and --symbol to create a session, or --session to tick", file=sys.stderr)
        return 1

    session = create_session(
        args.strategy,
        args.symbol,
        args.timeframe,
        params=json.loads(args.params) if args.params else None,
        config=_backtest_config_from_args(args),
        live_config=_live_config_from_args(args),
        refresh_data=not args.no_refresh,
    )
    out = {
        "session_id": session.id,
        "last_bar_ts": session.last_bar_ts,
        "baseline_metrics": session.baseline_metrics,
    }
    print(json.dumps(out, indent=2))
    if args.loop:
        run_paper_loop(session.id, poll_seconds=args.poll, max_ticks=args.max_ticks)
    return 0


def _iteration_config_from_args(args: argparse.Namespace) -> IterationConfig:
    return make_iteration_config(
        recent_months=getattr(args, "recent_months", 3),
        baseline_months=getattr(args, "baseline_months", None),
        decay_sharpe_delta=getattr(args, "decay_sharpe", 0.5),
        decay_return_pct=getattr(args, "decay_return", 15.0),
        min_recent_trades=getattr(args, "min_recent_trades", 10),
        min_paper_days=getattr(args, "min_paper_days", 14),
        min_paper_trades=getattr(args, "min_paper_trades", 20),
        scale_up_factor=getattr(args, "scale_factor", 1.5),
        max_capital_multiplier=getattr(args, "max_capital_mult", 5.0),
    )


def _add_iteration_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-months", type=int, default=3, help="Recent window for decay check")
    p.add_argument("--baseline-months", type=int, default=None, help="Baseline window (default: all before recent)")
    p.add_argument("--decay-sharpe", type=float, default=0.5, help="Sharpe drop threshold for decay verdict")
    p.add_argument("--decay-return", type=float, default=15.0, help="Return lag threshold (pct points)")
    p.add_argument("--min-recent-trades", type=int, default=10)
    p.add_argument("--min-paper-days", type=int, default=14, help="Min paper days before capital scale-up")
    p.add_argument("--min-paper-trades", type=int, default=20)
    p.add_argument("--scale-factor", type=float, default=1.5, help="Capital scale-up multiplier")
    p.add_argument("--max-capital-mult", type=float, default=5.0, help="Max capital vs initial")


def cmd_revalidate(args: argparse.Namespace) -> int:
    from borex.runner.revalidate import get_revalidation, list_revalidations, run_revalidation
    from borex.strategy.mtf import validate_mtf_entry

    if args.list:
        print(json.dumps(
            list_revalidations(strategy=args.strategy, symbol=args.symbol, limit=args.limit),
            indent=2,
        ))
        return 0

    if args.id:
        row = get_revalidation(args.id)
        if not row:
            print(f"Revalidation not found: {args.id}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2))
        return 0

    if not args.strategy or not args.symbol:
        print("Provide --strategy and --symbol, or --list / --id", file=sys.stderr)
        return 1

    validate_mtf_entry(get_strategy(args.strategy), args.timeframe)
    report = run_revalidation(
        args.strategy,
        args.symbol,
        args.timeframe,
        params=json.loads(args.params) if args.params else None,
        config=_backtest_config_from_args(args),
        iteration=_iteration_config_from_args(args),
        paper_session_id=args.session,
        save=not args.no_save,
    )
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.verdict in ("healthy", "warning") else 1


def _screen_gates_from_args(args: argparse.Namespace):
    from borex.runner.screen import ScreenGates

    return ScreenGates(
        min_oos_sharpe=args.min_oos_sharpe,
        min_oos_trades=args.min_oos_trades,
        max_oos_drawdown_pct=args.max_oos_drawdown,
        min_oos_return_pct=args.min_oos_return,
        min_positive_fold_ratio=args.min_positive_folds,
        allow_liquidation=args.allow_liquidation,
        min_reward_risk_ratio=getattr(args, "min_rr", 0.0),
    )


def _screen_config_from_args(args: argparse.Namespace, *, create_paper_default: bool = False) -> "ScreenConfig":
    from borex.runner.screen import ScreenConfig

    screen_kw: dict = {
        "strategies": args.strategies.split(",") if getattr(args, "strategies", None) else None,
        "symbols": args.symbols.split(",") if getattr(args, "symbols", None) else None,
        "sweep_params": args.sweep_params.split(",") if getattr(args, "sweep_params", None) else None,
        "max_points": args.max_points,
        "max_combos": args.max_combos,
        "workers": args.workers,
        "train_months": args.train_months,
        "test_months": args.test_months,
        "step_months": args.step_months,
        "optimize_metric": args.metric,
        "backtest_config": _backtest_config_from_args(args),
        "gates": _screen_gates_from_args(args),
        "create_paper": getattr(args, "create_paper", False) or create_paper_default,
        "top_n_paper": args.top_n_paper,
        "save": not args.no_save,
    }
    if getattr(args, "timeframes", None):
        screen_kw["timeframes"] = args.timeframes.split(",")
    return ScreenConfig(**screen_kw)


def cmd_screen(args: argparse.Namespace) -> int:
    from borex.runner.screen import get_screen_run, list_screen_runs, run_screen

    if args.list:
        print(json.dumps(list_screen_runs(limit=args.limit), indent=2))
        return 0

    if args.id:
        row = get_screen_run(args.id)
        if not row:
            print(f"Screen run not found: {args.id}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2))
        return 0

    summary = run_screen(_screen_config_from_args(args))
    print(json.dumps(summary.to_dict(), indent=2))
    return 0 if summary.promoted else 1


def _add_screen_args(p: argparse.ArgumentParser, *, create_paper_default: bool = False) -> None:
    p.add_argument("--strategies", help="Comma-separated strategy names (default: all)")
    p.add_argument("--symbols", help="Comma-separated symbols (default: all pairs)")
    p.add_argument("--timeframes", help="Comma-separated entry TFs (default: 1h)")
    p.add_argument("--sweep-params", help="Params to optimize on train window")
    p.add_argument("--max-points", type=int, default=6)
    p.add_argument("--max-combos", type=int, default=80)
    p.add_argument("--workers", type=int, default=default_workers(), help="Parallel screen jobs (default: CPU-2)")
    p.add_argument("--train-months", type=int, default=6)
    p.add_argument("--test-months", type=int, default=1)
    p.add_argument("--step-months", type=int, default=1)
    p.add_argument("--metric", default="sharpe", help="Optimize metric on train window")
    p.add_argument("--min-oos-sharpe", type=float, default=0.5)
    p.add_argument("--min-oos-trades", type=int, default=10)
    p.add_argument("--max-oos-drawdown", type=float, default=35.0, help="Max OOS drawdown pct")
    p.add_argument("--min-oos-return", type=float, default=0.0, help="Min avg OOS return pct")
    p.add_argument("--min-positive-folds", type=float, default=0.5, help="Min fraction of positive OOS folds")
    p.add_argument("--min-rr", type=float, default=0.0, help="Min reward:risk ratio (tp_pct/sl_pct), e.g. 2.0 for 2:1")
    p.add_argument("--allow-liquidation", action="store_true", help="Allow configs that liquidate in OOS")
    if create_paper_default:
        p.add_argument("--no-create-paper", action="store_true", help="Skip paper session creation for promoted configs")
    else:
        p.add_argument("--create-paper", action="store_true", help="Create paper sessions for top promoted configs")
    p.add_argument("--top-n-paper", type=int, default=3)
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--leverage", type=float, default=500.0)
    p.add_argument("--capital", type=float, default=1000.0)
    _add_cost_args(p)
    _add_risk_args(p)
    _add_realism_args(p)


def cmd_pipeline(args: argparse.Namespace) -> int:
    from borex.runner.pipeline import (
        PipelineConfig,
        get_pipeline_run,
        list_pipeline_runs,
        run_pipeline,
        run_pipeline_tick,
        run_pipeline_watch,
    )

    if args.pipeline_cmd == "list":
        print(json.dumps(list_pipeline_runs(limit=args.limit), indent=2))
        return 0

    if args.pipeline_cmd == "show":
        row = get_pipeline_run(args.id)
        if not row:
            print(f"Pipeline run not found: {args.id}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2))
        return 0

    if args.pipeline_cmd == "tick":
        summary = run_pipeline_tick(
            revalidate=args.revalidate,
            kill_on_decay=not args.no_kill_on_decay,
            iteration=_iteration_config_from_args(args),
            send_digest=args.digest,
            save=not args.no_save,
        )
        print(json.dumps(summary.to_dict(), indent=2))
        return 0 if summary.status == "ok" else 1

    if args.pipeline_cmd == "watch":
        run_pipeline_watch(
            poll_seconds=args.poll,
            revalidate_every=args.revalidate_every,
            kill_on_decay=not args.no_kill_on_decay,
            iteration=_iteration_config_from_args(args),
            max_cycles=args.max_cycles,
        )
        return 0

    # pipeline run
    create_paper = not getattr(args, "no_create_paper", False)
    args.create_paper = create_paper
    screen_cfg = _screen_config_from_args(args, create_paper_default=create_paper)
    cfg = PipelineConfig(
        run_audit=not args.skip_audit,
        strict_audit=not args.no_strict_audit,
        run_screen=not args.skip_screen,
        screen=screen_cfg,
        tick_paper=args.tick_paper,
        run_revalidate=not args.no_revalidate,
        kill_on_decay=not args.no_kill_on_decay,
        iteration=_iteration_config_from_args(args),
        send_digest=not args.no_digest,
        save=not args.no_save,
    )
    summary = run_pipeline(cfg)
    print(json.dumps(summary.to_dict(), indent=2))
    return 0 if summary.status == "ok" else 1


def cmd_monitor(args: argparse.Namespace) -> int:
    from borex.runner.decision_log import list_alerts, list_decisions
    from borex.runner.paper import get_monitor_status

    if not args.session:
        print("Provide --session", file=sys.stderr)
        return 1

    out: dict = {"monitor": get_monitor_status(args.session)}
    if args.decisions:
        out["decisions"] = list_decisions(args.session, limit=args.limit)
    if args.alerts:
        out["alerts"] = list_alerts(args.session, limit=args.limit)
    print(json.dumps(out, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("borex.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="borex", description="Forex backtesting platform")
    parser.add_argument("--version", action="version", version=f"borex {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dl = sub.add_parser("download", help="Download OHLCV to local cache")
    p_dl.add_argument("--all", action="store_true", help="All forex pairs x all timeframes")
    p_dl.add_argument("--symbol", "-s", choices=FOREX_PAIRS)
    p_dl.add_argument("--timeframe", "-t", choices=SUPPORTED_TIMEFRAMES)
    p_dl.add_argument("--force", action="store_true")
    p_dl.add_argument("--delay", type=float, default=0.5)
    p_dl.set_defaults(func=cmd_download)

    p_imp = sub.add_parser("import", help="Import dukascopy-node CSV into parquet cache")
    p_imp.add_argument("file", help="Path to dukascopy-node CSV")
    p_imp.add_argument("--symbol", "-s", help="Override symbol (e.g. EURUSD=X)")
    p_imp.add_argument("--timeframe", "-t", help="Override timeframe (e.g. 1h)")
    p_imp.set_defaults(func=cmd_import)

    p_bt = sub.add_parser("backtest", help="Run a single backtest")
    p_bt.add_argument("--strategy", required=True)
    p_bt.add_argument("--symbol", "-s", required=True)
    p_bt.add_argument("--timeframe", "-t", default="1h")
    p_bt.add_argument("--params", help="JSON param overrides")
    p_bt.add_argument("--leverage", type=float, default=500.0)
    p_bt.add_argument("--capital", type=float, default=1000.0)
    p_bt.add_argument("--regimes", action="store_true", help="Include per-regime trade breakdown")
    _add_cost_args(p_bt)
    _add_risk_args(p_bt)
    _add_realism_args(p_bt)
    p_bt.set_defaults(func=cmd_backtest)

    p_mass = sub.add_parser("mass", help="Mass backtest: strategies x symbols x timeframes")
    p_mass.add_argument("--strategies", help="Comma-separated strategy names")
    p_mass.add_argument("--symbols", help="Comma-separated symbols")
    p_mass.add_argument("--timeframes", help="Comma-separated timeframes")
    p_mass.add_argument("--workers", type=int, default=default_workers(), help="Parallel backtest jobs (default: CPU-2)")
    p_mass.add_argument("--leverage", type=float, default=500.0)
    p_mass.add_argument("--capital", type=float, default=1000.0)
    _add_cost_args(p_mass)
    _add_risk_args(p_mass)
    _add_realism_args(p_mass)
    p_mass.set_defaults(func=cmd_mass)

    p_wf = sub.add_parser("walkforward", help="Train/test walk-forward validation")
    p_wf.add_argument("--strategy", required=True)
    p_wf.add_argument("--symbol", "-s", required=True)
    p_wf.add_argument("--timeframe", "-t", default="1h")
    p_wf.add_argument("--params")
    p_wf.add_argument("--leverage", type=float, default=500.0)
    p_wf.add_argument("--capital", type=float, default=1000.0)
    p_wf.add_argument("--rolling", action="store_true", help="Rolling train/test windows")
    p_wf.add_argument("--train-months", type=int, default=6)
    p_wf.add_argument("--test-months", type=int, default=1)
    p_wf.add_argument("--step-months", type=int, default=1)
    p_wf.add_argument("--optimize", action="store_true", help="Optimize params on each train window")
    p_wf.add_argument("--sweep-params", help="Comma-separated params to optimize (e.g. fast,slow)")
    p_wf.add_argument("--max-points", type=int, default=6)
    p_wf.add_argument("--max-combos", type=int, default=200)
    p_wf.add_argument("--min-rr", type=float, default=0.0, help="Min reward:risk (tp/sl) during train optimization")
    p_wf.add_argument("--train-workers", type=int, default=0, help="Parallel param combos per fold (0=auto)")
    p_wf.add_argument("--fold-workers", type=int, default=0, help="Parallel OOS folds (0=auto)")
    p_wf.add_argument("--save", action="store_true", help="Persist fold results to SQLite")
    p_wf.add_argument("--no-regimes", action="store_true")
    _add_cost_args(p_wf)
    _add_risk_args(p_wf)
    _add_realism_args(p_wf)
    p_wf.set_defaults(func=cmd_walkforward)

    p_sweep = sub.add_parser("sweep", help="Parameter grid search")
    p_sweep.add_argument("--strategy", required=True)
    p_sweep.add_argument("--symbol", "-s", required=True)
    p_sweep.add_argument("--timeframe", "-t", default="1h")
    p_sweep.add_argument("--sweep-params", help="Params to sweep (default: all schema params)")
    p_sweep.add_argument("--max-points", type=int, default=8)
    p_sweep.add_argument("--max-combos", type=int, default=500)
    p_sweep.add_argument("--min-rr", type=float, default=0.0, help="Min reward:risk (tp/sl) in param grid")
    p_sweep.add_argument("--workers", type=int, default=default_workers(), help="Parallel sweep jobs (default: CPU-2)")
    p_sweep.add_argument("--metric", default="sharpe")
    p_sweep.add_argument("--no-save", action="store_true")
    p_sweep.add_argument("--json", action="store_true")
    p_sweep.add_argument("--leverage", type=float, default=500.0)
    p_sweep.add_argument("--capital", type=float, default=1000.0)
    _add_cost_args(p_sweep)
    _add_risk_args(p_sweep)
    _add_realism_args(p_sweep)
    p_sweep.set_defaults(func=cmd_sweep)

    p_list = sub.add_parser("list", help="List cached data, strategies, or results")
    p_list.add_argument("--cached", action="store_true")
    p_list.add_argument("--strategies", action="store_true")
    p_list.add_argument("--results", action="store_true")
    p_list.add_argument("--leaderboard", action="store_true")
    p_list.add_argument("--metric", default="sharpe")
    p_list.add_argument("--limit", type=int, default=100)
    p_list.set_defaults(func=cmd_list)

    p_audit = sub.add_parser("audit", help="Data integrity audit and manifest repair")
    p_audit.add_argument("--all", action="store_true", help="Audit all cached datasets")
    p_audit.add_argument("--symbol", "-s", help="Symbol to audit (e.g. EURUSD=X)")
    p_audit.add_argument("--timeframe", "-t", help="Timeframe to audit (e.g. 1h)")
    p_audit.add_argument("--json", action="store_true", help="JSON output")
    p_audit.add_argument("--strict", action="store_true", help="Exit 1 if any dataset fails")
    p_audit.add_argument(
        "--fix",
        action="store_true",
        help="Rebuild corrupt datasets from year chunks / resample, then audit",
    )
    p_audit.add_argument(
        "--repair-manifests",
        action="store_true",
        help="Backfill manifests for existing parquet files",
    )
    p_audit.set_defaults(func=cmd_audit)

    p_paper = sub.add_parser("paper", help="Paper trading on live data (simulated fills)")
    p_paper.add_argument("--strategy", help="Strategy name (required to create session)")
    p_paper.add_argument("--symbol", "-s", help="Symbol (e.g. EURUSD=X)")
    p_paper.add_argument("--timeframe", "-t", default="1h")
    p_paper.add_argument("--params", help="JSON param overrides")
    p_paper.add_argument("--session", help="Existing session id (tick or loop)")
    p_paper.add_argument("--list", action="store_true", help="List paper sessions")
    p_paper.add_argument("--limit", type=int, default=20)
    p_paper.add_argument("--poll", type=float, default=300.0, help="Poll interval seconds for --loop")
    p_paper.add_argument("--loop", action="store_true", help="Poll continuously")
    p_paper.add_argument("--max-ticks", type=int, default=None)
    p_paper.add_argument("--no-refresh", action="store_true", help="Skip Yahoo refresh on tick")
    p_paper.add_argument("--monitor", action="store_true", help="Show monitor status for session")
    p_paper.add_argument("--decisions", action="store_true", help="Show decision log for session")
    p_paper.add_argument("--kill", action="store_true", help="Kill paper session")
    p_paper.add_argument("--resume", action="store_true", help="Resume killed session")
    p_paper.add_argument("--kill-reason", default="manual", help="Reason for --kill")
    p_paper.add_argument("--leverage", type=float, default=500.0)
    p_paper.add_argument("--capital", type=float, default=1000.0)
    _add_cost_args(p_paper)
    _add_risk_args(p_paper)
    _add_realism_args(p_paper)
    _add_live_args(p_paper)
    p_paper.set_defaults(func=cmd_paper)

    p_mon = sub.add_parser("monitor", help="Live session health, divergence, alerts")
    p_mon.add_argument("--session", required=True)
    p_mon.add_argument("--decisions", action="store_true")
    p_mon.add_argument("--alerts", action="store_true")
    p_mon.add_argument("--limit", type=int, default=50)
    p_mon.set_defaults(func=cmd_monitor)

    p_rev = sub.add_parser("revalidate", help="Periodic decay check: baseline vs recent window")
    p_rev.add_argument("--strategy")
    p_rev.add_argument("--symbol", "-s")
    p_rev.add_argument("--timeframe", "-t", default="1h")
    p_rev.add_argument("--params")
    p_rev.add_argument("--session", help="Paper session id for capital scale recommendation")
    p_rev.add_argument("--list", action="store_true", help="List past revalidation runs")
    p_rev.add_argument("--id", help="Show a specific revalidation run")
    p_rev.add_argument("--limit", type=int, default=50)
    p_rev.add_argument("--no-save", action="store_true")
    p_rev.add_argument("--leverage", type=float, default=500.0)
    p_rev.add_argument("--capital", type=float, default=1000.0)
    _add_cost_args(p_rev)
    _add_risk_args(p_rev)
    _add_realism_args(p_rev)
    _add_iteration_args(p_rev)
    p_rev.set_defaults(func=cmd_revalidate)

    p_scr = sub.add_parser(
        "screen",
        help="Mass sweep + rolling walk-forward; promote configs passing OOS gates",
    )
    _add_screen_args(p_scr)
    p_scr.add_argument("--list", action="store_true", help="List past screen runs")
    p_scr.add_argument("--id", help="Show a specific screen run")
    p_scr.add_argument("--limit", type=int, default=20)
    p_scr.set_defaults(func=cmd_screen)

    p_pipe = sub.add_parser("pipeline", help="Automated testing pipeline")
    pipe_sub = p_pipe.add_subparsers(dest="pipeline_cmd", required=True)

    p_pipe_list = pipe_sub.add_parser("list", help="List past pipeline runs")
    p_pipe_list.add_argument("--limit", type=int, default=20)
    p_pipe_list.set_defaults(func=cmd_pipeline)

    p_pipe_show = pipe_sub.add_parser("show", help="Show a pipeline run report")
    p_pipe_show.add_argument("--id", required=True)
    p_pipe_show.set_defaults(func=cmd_pipeline)

    p_pipe_run = pipe_sub.add_parser(
        "run",
        help="Audit -> screen -> revalidate -> retire decayed (weekly research)",
    )
    p_pipe_run.add_argument("--skip-audit", action="store_true")
    p_pipe_run.add_argument("--no-strict-audit", action="store_true", help="Continue if audit has failures")
    p_pipe_run.add_argument("--skip-screen", action="store_true", help="Revalidate/tick only")
    p_pipe_run.add_argument("--tick-paper", action="store_true", help="Tick active paper sessions after screen")
    p_pipe_run.add_argument("--no-revalidate", action="store_true")
    p_pipe_run.add_argument("--no-kill-on-decay", action="store_true", help="Do not kill paper on decayed verdict")
    p_pipe_run.add_argument("--no-digest", action="store_true", help="Skip webhook summary")
    _add_screen_args(p_pipe_run, create_paper_default=True)
    _add_iteration_args(p_pipe_run)
    p_pipe_run.set_defaults(func=cmd_pipeline, kill_on_decay=True, pipeline_cmd="run")

    p_pipe_tick = pipe_sub.add_parser("tick", help="Tick all active paper sessions")
    p_pipe_tick.add_argument("--revalidate", action="store_true", help="Also revalidate active sessions")
    p_pipe_tick.add_argument("--no-kill-on-decay", action="store_true")
    p_pipe_tick.add_argument("--digest", action="store_true", help="Send webhook summary")
    p_pipe_tick.add_argument("--no-save", action="store_true")
    _add_iteration_args(p_pipe_tick)
    p_pipe_tick.set_defaults(func=cmd_pipeline, kill_on_decay=True, pipeline_cmd="tick")

    p_pipe_watch = pipe_sub.add_parser("watch", help="Daemon: tick active sessions on interval")
    p_pipe_watch.add_argument("--poll", type=float, default=300.0, help="Seconds between tick cycles")
    p_pipe_watch.add_argument(
        "--revalidate-every",
        type=int,
        default=288,
        help="Revalidate every N poll cycles (288 @ 5min ≈ daily; 0=never)",
    )
    p_pipe_watch.add_argument("--no-kill-on-decay", action="store_true")
    p_pipe_watch.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (default: forever)")
    _add_iteration_args(p_pipe_watch)
    p_pipe_watch.set_defaults(func=cmd_pipeline, kill_on_decay=True, pipeline_cmd="watch")

    p_serve = sub.add_parser("serve", help="Start API server for React UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
