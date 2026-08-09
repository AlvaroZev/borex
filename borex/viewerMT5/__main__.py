"""
Borex trade viewer using MetaTrader 5 OHLC (broker history).

Same UI as ``python -m borex.viewer``, but bars come from MT5 for all
tradeable Forex pairs (default: last 3 years).

Requires sibling ``borex_live`` + logged-in MT5 terminal (see borex_live/.env).

Example:
  python -m borex.viewerMT5 --strategy alexg7 -i 1h -p 3y --same-bar-exit --port 8766
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from borex.alexg.multi_market import pick_master_symbol
from borex.backtest import BacktestConfig, BacktestEngine, MultiMarketEngine
from borex.viewer.analysis import (
    MarketAnalysis,
    scan_alexg3_decisions,
    strategy_params,
    warn_decision_param_mismatch,
)
from borex.viewer.analysis_store import (
    load_analysis_bundle,
    resolve_run_dir,
    save_analysis_bundle,
)
from borex.viewer.context import ViewerSession
from borex.viewer.server import create_app, set_session
from borex.viewer.trade_store import TRADES_FILE, save_trades_csv
from borex.viewer.__main__ import (
    STATIC_DIR,
    _build_config,
    _build_strategy,
    _set_terminal_title,
)
from borex.viewerMT5.mt5_feed import (
    list_tradeable_yahoo_symbols,
    load_mt5_universe,
)


def _parse_leverage(value: str) -> float:
    leverage = float(value)
    if not 1 <= leverage <= 50_000:
        raise argparse.ArgumentTypeError("leverage debe estar entre 1 y 50000")
    return leverage


def _parse_risk_pct(value: str) -> float:
    risk = float(value)
    if not 0 < risk <= 1:
        raise argparse.ArgumentTypeError("risk-per-trade debe estar entre 0 y 1")
    return risk


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Borex trade viewer — MT5 OHLC backtest + chart UI"
    )
    parser.add_argument(
        "--strategy",
        choices=[
            "candles",
            "alexg",
            "alexg2",
            "alexg3",
            "alexg4",
            "alexg5",
            "alexg6",
            "alexg6a",
            "alexg6b",
            "alexg7",
            "alexg7aligned",
            "alexg8",
            "alexg6-1m",
            "alexg-market",
            "institutional",
        ],
        default="alexg7",
    )
    parser.add_argument("--symbol", "-s", default="EURUSD=X")
    parser.add_argument(
        "--period",
        "-p",
        default="3y",
        help="History window from MT5 (default: 3y). Also: 90d, 24m, max",
    )
    parser.add_argument("--interval", "-i", default="1h")
    parser.add_argument("--capital", type=float, default=1_000)
    parser.add_argument("--leverage", "-l", type=_parse_leverage, default=5_000.0)
    parser.add_argument("--min-score", type=float, default=70.0)
    parser.add_argument("--min-rr", type=float, default=3.0)
    parser.add_argument(
        "--rr-mode",
        choices=["fixed", "dynamic"],
        default="dynamic",
        help="RR fijo o dinámico (1/winrate)",
    )
    parser.add_argument(
        "--ltf-intervals",
        nargs="+",
        default=["1m"],
        help="Unused (legacy); alexg8 has no LTF confirm",
    )
    parser.add_argument(
        "--ltf-confirm-mode",
        choices=["any", "all"],
        default="any",
        help="Unused (legacy); alexg8 has no LTF confirm",
    )
    parser.add_argument(
        "--rr-factor",
        type=float,
        default=1.0,
        help="AlexG5/6: multiply dynamic RR (1/winrate)",
    )
    parser.add_argument(
        "--second-signal",
        choices=["off", "flip", "replace"],
        default="off",
    )
    parser.add_argument("--tp-fraction", type=float, default=1.0)
    parser.add_argument("--sl-mult", type=float, default=1.0)
    parser.add_argument("--risk-per-trade", type=_parse_risk_pct, default=0.01)
    parser.add_argument(
        "--size-mode",
        choices=["fixed_risk", "margin"],
        default="margin",
    )
    parser.add_argument("--position-size", type=float, default=0.01)
    parser.add_argument(
        "--commission-per-lot",
        type=float,
        default=7.0,
        help="Round-turn commission USD per 1.0 lot (default: 7.0)",
    )
    parser.add_argument("--commission-per-trade", type=float, default=0.0)
    parser.add_argument(
        "--no-commission",
        action="store_true",
        help="Disable commission",
    )
    parser.add_argument(
        "--risk-include-commission",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Shrink margin so SL price-loss + commission ≈ 1% risk",
    )
    parser.add_argument("--close-on-opposite", action="store_true")
    parser.add_argument("--true-sl", action="store_true")
    parser.add_argument("--allow-false-positives", action="store_true")
    parser.add_argument("--no-momentum", action="store_true")
    parser.add_argument("--inversed", action="store_true")
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="Override universe (Yahoo keys). Default: all MT5 tradeable Forex",
    )
    parser.add_argument("--max-positions", type=int, default=999)
    parser.add_argument("--strength-lookback", type=int, default=24)
    parser.add_argument("--min-currency-edge", type=float, default=0.00005)
    parser.add_argument("--min-confirming-pairs", type=int, default=2)
    parser.add_argument(
        "--same-bar-exit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Check SL/TP on fill bar (default: on — closer to live MT5)",
    )
    parser.add_argument(
        "--use-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Read/write parquet under data/cache/mt5/ (default: on)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--save-analysis",
        metavar="DIR",
        nargs="?",
        const="",
        help=(
            "Save decisions CSV bundle for reuse. Default: "
            "data/runs/{strategy}_mt5_{period}_{interval}/. "
            "Re-run later with --load-analysis to skip the signal scan."
        ),
    )
    parser.add_argument(
        "--save-trades",
        metavar="DIR",
        nargs="?",
        const="",
        help="Save backtest trades CSV",
    )
    parser.add_argument(
        "--load-analysis",
        metavar="DIR",
        help=(
            "Load saved decisions and replay them for the portfolio backtest "
            "(skips strategy scan). Safe for PnL-only sweeps: capital, leverage, "
            "commission, rr-factor, same-bar-exit, position-size, max-positions. "
            "Re-scan if strategy/min-rr/filters/data change."
        ),
    )
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help="With --load-analysis: skip backtest, serve /analysis only",
    )
    return parser.parse_args(argv)


def _run_dir_period(args: argparse.Namespace) -> str:
    return f"mt5_{args.period}"


def run_session(args: argparse.Namespace) -> ViewerSession:
    multi = args.strategy in (
        "alexg3",
        "alexg4",
        "alexg5",
        "alexg6",
        "alexg6a",
        "alexg6b",
        "alexg7",
        "alexg7aligned",
        "alexg8",
        "alexg6-1m",
        "alexg-market",
    )

    if not multi:
        # Single-market strategies still use MT5 bars for --symbol.
        print(
            f"Fetching MT5 {args.interval} for {args.symbol} ({args.period})…",
            flush=True,
            file=sys.stderr,
        )
        candles_by_symbol = load_mt5_universe(
            [args.symbol],
            args.interval,
            args.period,
            use_cache=args.use_cache,
        )
        if args.symbol not in candles_by_symbol:
            raise RuntimeError(f"No MT5 bars for {args.symbol}")
        candles = candles_by_symbol[args.symbol]
        strategy = _build_strategy(args)
        config = _build_config(args)
        engine = BacktestEngine(strategy, config)
        result = engine.run(
            candles, symbol=args.symbol, timeframe=args.interval, mtf=None
        )
        if args.save_trades is not None:
            run_dir = resolve_run_dir(
                strategy=strategy.name,
                period=_run_dir_period(args),
                interval=args.interval,
                save_analysis=None,
                save_trades=args.save_trades,
            )
            trades_path = save_trades_csv(
                result.trades, run_dir, leverage=args.leverage
            )
            print(
                f"Trades saved to {trades_path} ({len(result.trades)} rows)",
                flush=True,
                file=sys.stderr,
            )
        return ViewerSession(
            symbol=args.symbol,
            timeframe=args.interval,
            strategy_name=strategy.name,
            leverage=args.leverage,
            candles=candles,
            trades=result.trades,
            summary_text=result.summary() + f"\n[data] MT5 OHLC {args.period}",
            total_return_pct=result.total_return_pct,
            win_rate=result.win_rate,
            total_trades=result.total_trades,
            inversed=args.inversed,
            tp_fraction=args.tp_fraction,
            true_sl=args.true_sl,
        )

    load_path = Path(args.load_analysis) if args.load_analysis else None
    if args.analysis_only and not load_path:
        raise RuntimeError("--analysis-only requires --load-analysis DIR")

    if args.analysis_only and load_path:
        analysis = load_analysis_bundle(load_path)
        tf = analysis.timeframe or args.interval
        return ViewerSession(
            symbol=f"analysis ({len(analysis.symbols)} pairs)",
            timeframe=tf,
            strategy_name=args.strategy,
            leverage=args.leverage,
            candles=[],
            trades=[],
            candles_by_symbol=None,
            analysis=analysis,
            summary_text=(
                f"Loaded analysis from {load_path}\n"
                f"Signals: {analysis.total_decisions} across "
                f"{len(analysis.symbols)} markets"
            ),
            total_return_pct=0.0,
            win_rate=0.0,
            total_trades=0,
            inversed=args.inversed,
            tp_fraction=args.tp_fraction,
            true_sl=args.true_sl,
        )

    if args.symbols:
        universe = list(args.symbols)
        print(
            f"Using CLI universe ({len(universe)} symbols)",
            flush=True,
            file=sys.stderr,
        )
    else:
        print("Listing MT5 tradeable Forex pairs…", flush=True, file=sys.stderr)
        universe = list_tradeable_yahoo_symbols()
        print(f"MT5 Forex tradeable: {len(universe)} pairs", flush=True, file=sys.stderr)

    if args.symbol not in universe:
        universe = [args.symbol] + list(universe)

    print(
        f"Fetching MT5 {args.interval} for {len(universe)} pairs ({args.period})…",
        flush=True,
        file=sys.stderr,
    )
    candles_by_symbol = load_mt5_universe(
        universe,
        args.interval,
        args.period,
        use_cache=args.use_cache,
    )
    if not candles_by_symbol:
        raise RuntimeError(
            "No MT5 series loaded. Is the terminal logged in? "
            "Try scrolling charts left to download history."
        )
    print(
        f"Loaded {len(candles_by_symbol)}/{len(universe)} pairs from MT5",
        flush=True,
        file=sys.stderr,
    )

    master = pick_master_symbol(candles_by_symbol, args.symbol)
    strategy = _build_strategy(args)

    config = _build_config(args)

    analysis: MarketAnalysis | None = None
    current_params = strategy_params(strategy)
    if load_path:
        print(f"Loading analysis from {load_path}…", flush=True, file=sys.stderr)
        analysis = load_analysis_bundle(load_path, candles_by_symbol)
        print(
            f"Loaded {analysis.total_decisions} signals (scan skipped)",
            flush=True,
            file=sys.stderr,
        )
        warn_decision_param_mismatch(analysis.strategy_params, current_params)
    else:
        print(
            "Scanning decisions across all markets…",
            flush=True,
            file=sys.stderr,
        )
        analysis = scan_alexg3_decisions(
            candles_by_symbol, strategy, master_symbol=master
        )
        analysis.strategy_params = current_params
        print(
            f"Analysis: {analysis.total_decisions} signals across "
            f"{len(analysis.symbols)} markets",
            flush=True,
            file=sys.stderr,
        )

    if args.save_analysis is not None:
        run_dir = resolve_run_dir(
            strategy=strategy.name,
            period=_run_dir_period(args),
            interval=args.interval,
            save_analysis=args.save_analysis,
            save_trades=args.save_trades,
        )
        saved = save_analysis_bundle(
            analysis,
            run_dir,
            timeframe=args.interval,
            strategy_name=strategy.name,
            extra_meta={
                "period": args.period,
                "data_source": "mt5",
                "same_bar_exit": args.same_bar_exit,
                "strategy_params": current_params,
                "trades_file": TRADES_FILE if args.save_trades is not None else None,
            },
        )
        print(f"Analysis saved to {saved}", flush=True, file=sys.stderr)

    engine = MultiMarketEngine(strategy, config, max_positions=args.max_positions)
    result = engine.run(
        candles_by_symbol,
        timeframe=args.interval,
        master_symbol=master,
        same_bar_exit=args.same_bar_exit,
        decisions=analysis.all_decisions,
    )

    if args.save_trades is not None:
        run_dir = resolve_run_dir(
            strategy=strategy.name,
            period=_run_dir_period(args),
            interval=args.interval,
            save_analysis=args.save_analysis,
            save_trades=args.save_trades,
        )
        trades_path = save_trades_csv(
            result.trades,
            run_dir,
            leverage=args.leverage,
        )
        print(
            f"Trades saved to {trades_path} ({len(result.trades)} rows)",
            flush=True,
            file=sys.stderr,
        )

    display_symbol = args.symbol if args.symbol in candles_by_symbol else master
    summary = (
        result.summary()
        + f"\n[data] MT5 OHLC | period={args.period} | pairs={len(candles_by_symbol)}"
        + f" | same_bar_exit={args.same_bar_exit}"
    )
    return ViewerSession(
        symbol=f"{display_symbol} (+{len(candles_by_symbol)-1} pairs)",
        timeframe=args.interval,
        strategy_name=strategy.name,
        leverage=args.leverage,
        candles=candles_by_symbol.get(display_symbol, candles_by_symbol[master]),
        trades=result.trades,
        candles_by_symbol=candles_by_symbol,
        analysis=analysis,
        summary_text=summary,
        total_return_pct=result.total_return_pct,
        win_rate=result.win_rate,
        total_trades=result.total_trades,
        inversed=args.inversed,
        tp_fraction=args.tp_fraction,
        true_sl=args.true_sl,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _set_terminal_title(f"mt5-{args.strategy}", args.port)
    try:
        session = run_session(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    set_session(session)
    app = create_app(STATIC_DIR)

    url = f"http://{args.host}:{args.port}"
    print(session.summary_text, flush=True)
    print(flush=True)
    print(f"Trade viewer (MT5): {url}", flush=True)
    print(f"Market analysis: {url}/analysis", flush=True)
    print(f"Trades to inspect: {session.total_trades}", flush=True)

    if not args.no_browser:
        open_url = f"{url}/analysis" if args.analysis_only else url
        webbrowser.open(open_url)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
