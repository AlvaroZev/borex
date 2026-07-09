from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from borex.alexg import (
    AlexG2Strategy,
    AlexG3Strategy,
    AlexG4Strategy,
    AlexG5Strategy,
    AlexGMethodStrategy,
)
from borex.alexg.multi_market import default_forex_universe, pick_master_symbol
from borex.backtest import BacktestConfig, BacktestEngine, MultiMarketEngine
from borex.data import build_full_mtf_context, load_csv, load_market_data
from borex.institutional import InstitutionalFlowStrategy
from borex.strategy import CandlePatternStrategy
from borex.strategy.base import Strategy
from borex.viewer.analysis import MarketAnalysis, scan_alexg3_decisions
from borex.viewer.analysis_store import (
    load_analysis_bundle,
    resolve_run_dir,
    save_analysis_bundle,
)
from borex.viewer.context import ViewerSession
from borex.viewer.server import create_app, set_session
from borex.viewer.trade_store import TRADES_FILE, save_trades_csv

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _parse_leverage(value: str) -> float:
    leverage = float(value)
    if not 1 <= leverage <= 5000:
        raise argparse.ArgumentTypeError("leverage debe estar entre 1 y 5000")
    return leverage


def _parse_risk_pct(value: str) -> float:
    risk = float(value)
    if not 0 < risk <= 1:
        raise argparse.ArgumentTypeError("risk-per-trade debe estar entre 0 y 1")
    return risk


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Borex trade viewer — backtest + chart UI"
    )
    parser.add_argument(
        "--strategy",
        choices=["candles", "alexg", "alexg2", "alexg3", "alexg4", "alexg5", "institutional"],
        default="alexg2",
    )
    parser.add_argument("--symbol", "-s", default="EURUSD=X")
    parser.add_argument("--period", "-p", default="60d")
    parser.add_argument("--interval", "-i", default="1h")
    parser.add_argument("--csv", help="CSV en lugar de yfinance")
    parser.add_argument("--capital", type=float, default=10_000)
    parser.add_argument("--leverage", "-l", type=_parse_leverage, default=500.0)
    parser.add_argument("--min-score", type=float, default=70.0)
    parser.add_argument("--min-rr", type=float, default=2.0)
    parser.add_argument(
        "--rr-factor",
        type=float,
        default=1.0,
        help="AlexG5: multiply dynamic RR (1/winrate). E.g. 1.1 = 10%% wider TP",
    )
    parser.add_argument(
        "--tp-fraction",
        type=float,
        default=1.0,
        help="TP a fracción del camino al AOI (1.0=completo, 0.7=70%%)",
    )
    parser.add_argument("--sl-mult", type=float, default=1.0)
    parser.add_argument("--risk-per-trade", type=_parse_risk_pct, default=0.01)
    parser.add_argument(
        "--size-mode",
        choices=["fixed_risk", "margin"],
        default="margin",
        help="margin: margen = cash libre × position-size; RR dinámico = 1/winrate",
    )
    parser.add_argument(
        "--position-size",
        type=float,
        default=0.01,
        help="Fracción del cash libre como margen (default: 0.01 = 1%%). Usa 0.01 para arriesgar 1%% por trade.",
    )
    parser.add_argument(
        "--close-on-opposite",
        action="store_true",
        help="Cerrar posición si llega señal opuesta (default: off)",
    )
    parser.add_argument(
        "--true-sl",
        action="store_true",
        help="SL al wipe del margen; TP a min-rr× ese riesgo (solo margin mode)",
    )
    parser.add_argument(
        "--allow-false-positives",
        action="store_true",
        help="Desactiva filtro de calidad por patrón (default: filtro activo)",
    )
    parser.add_argument(
        "--no-momentum",
        action="store_true",
        help="Desactiva señales de confirmación momentum (alexg2/alexg3)",
    )
    parser.add_argument(
        "--inversed",
        action="store_true",
        help="Invertir trades: buy ↔ sell (y swap SL/TP)",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="AlexG3: pares multi-mercado (default: universo FX)",
    )
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--strength-lookback", type=int, default=24)
    parser.add_argument("--min-currency-edge", type=float, default=0.00005)
    parser.add_argument("--min-confirming-pairs", type=int, default=2)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--save-analysis",
        metavar="DIR",
        nargs="?",
        const="",
        help="Save analysis CSV bundle (decisions, candles, AOI). "
        "Default: data/runs/{strategy}_{period}_{interval}/",
    )
    parser.add_argument(
        "--save-trades",
        metavar="DIR",
        nargs="?",
        const="",
        help="Save backtest trades CSV. Uses same folder as --save-analysis when both set. "
        "Default: data/runs/{strategy}_{period}_{interval}/trades.csv",
    )
    parser.add_argument(
        "--load-analysis",
        metavar="DIR",
        help="Load analysis from saved CSV bundle (skip signal scan)",
    )
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help="With --load-analysis: skip backtest, serve /analysis only",
    )
    return parser.parse_args(argv)


def _cache_mode(args: argparse.Namespace) -> str:
    if args.use_cache:
        return "only"
    if args.no_cache:
        return "off"
    return "auto"


def _build_strategy(args: argparse.Namespace) -> Strategy:
    if args.strategy == "alexg":
        return AlexGMethodStrategy(
            min_score=args.min_score,
            min_rr=args.min_rr,
            sl_mult=args.sl_mult,
        )
    disabled = ("momentum",) if args.no_momentum else ()
    if args.strategy == "alexg2":
        return AlexG2Strategy(
            min_rr=args.min_rr,
            tp_fraction=args.tp_fraction,
            filter_false_positives=not args.allow_false_positives,
            disabled_signals=disabled,
        )
    if args.strategy == "alexg3":
        return AlexG3Strategy(
            min_rr=args.min_rr,
            tp_fraction=args.tp_fraction,
            strength_lookback=args.strength_lookback,
            min_currency_edge=args.min_currency_edge,
            min_confirming_pairs=args.min_confirming_pairs,
            filter_false_positives=not args.allow_false_positives,
            disabled_signals=disabled,
        )
    if args.strategy == "alexg4":
        return AlexG4Strategy(
            min_rr=args.min_rr,
            tp_fraction=args.tp_fraction,
            strength_lookback=args.strength_lookback,
            min_currency_edge=args.min_currency_edge,
            min_confirming_pairs=args.min_confirming_pairs,
            filter_false_positives=not args.allow_false_positives,
            disabled_signals=disabled,
        )
    if args.strategy == "alexg5":
        return AlexG5Strategy(
            min_rr=args.min_rr,
            tp_fraction=args.tp_fraction,
            strength_lookback=args.strength_lookback,
            min_currency_edge=args.min_currency_edge,
            min_confirming_pairs=args.min_confirming_pairs,
            filter_false_positives=not args.allow_false_positives,
            disabled_signals=disabled,
        )
    if args.strategy == "institutional":
        return InstitutionalFlowStrategy(
            min_score=args.min_score,
            min_rr=args.min_rr,
            atr_sl_mult=args.sl_mult,
        )
    return CandlePatternStrategy(filter_mode=args.filter_mode)


def _build_config(args: argparse.Namespace) -> BacktestConfig:
    base = dict(
        initial_capital=args.capital,
        leverage=args.leverage,
        risk_per_trade_pct=args.risk_per_trade,
        close_on_opposite_signal=args.close_on_opposite,
        inversed=args.inversed,
        size_mode=args.size_mode,
        position_size_pct=args.position_size,
        true_sl=args.true_sl,
        true_sl_rr=args.min_rr,
        rr_factor=args.rr_factor,
    )
    if args.strategy in ("alexg", "alexg2", "alexg3", "alexg4", "alexg5", "institutional"):
        if args.strategy == "alexg5":
            # AlexG5 always uses margin stop as SL and winrate-derived RR for TP.
            base["size_mode"] = "margin"
            base["true_sl"] = True
            base["rr_factor"] = args.rr_factor
        return BacktestConfig(**base, stop_loss_pct=None, take_profit_pct=None)
    return BacktestConfig(**base)


def run_session(args: argparse.Namespace) -> ViewerSession:
    cache_mode = _cache_mode(args)

    if args.strategy in ("alexg3", "alexg4", "alexg5"):
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

        universe = args.symbols if args.symbols else default_forex_universe()
        if args.symbol not in universe:
            universe = [args.symbol] + list(universe)
        candles_by_symbol = {}
        for sym in universe:
            try:
                candles_by_symbol[sym] = load_market_data(
                    sym, args.period, args.interval, cache_mode=cache_mode
                )
            except Exception:
                continue
        master = pick_master_symbol(candles_by_symbol, args.symbol)
        strategy = _build_strategy(args)
        config = _build_config(args)

        analysis: MarketAnalysis | None = None
        if load_path:
            print(f"Loading analysis from {load_path}…", flush=True, file=sys.stderr)
            analysis = load_analysis_bundle(load_path, candles_by_symbol)
            print(
                f"Loaded {analysis.total_decisions} signals (scan skipped)",
                flush=True,
                file=sys.stderr,
            )
        else:
            print(
                "Scanning AlexG3 decisions across all markets…",
                flush=True,
                file=sys.stderr,
            )
            analysis = scan_alexg3_decisions(
                candles_by_symbol, strategy, master_symbol=master
            )
            print(
                f"Analysis: {analysis.total_decisions} signals across "
                f"{len(analysis.symbols)} markets",
                flush=True,
                file=sys.stderr,
            )

        if args.save_analysis is not None:
            run_dir = resolve_run_dir(
                strategy=strategy.name,
                period=args.period,
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
                    "trades_file": TRADES_FILE if args.save_trades is not None else None,
                },
            )
            print(f"Analysis saved to {saved}", flush=True, file=sys.stderr)

        engine = MultiMarketEngine(strategy, config, max_positions=args.max_positions)
        result = engine.run(
            candles_by_symbol, timeframe=args.interval, master_symbol=master
        )

        if args.save_trades is not None:
            run_dir = resolve_run_dir(
                strategy=strategy.name,
                period=args.period,
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
        return ViewerSession(
            symbol=f"{display_symbol} (+{len(candles_by_symbol)-1} pairs)",
            timeframe=args.interval,
            strategy_name=strategy.name,
            leverage=args.leverage,
            candles=candles_by_symbol.get(display_symbol, candles_by_symbol[master]),
            trades=result.trades,
            candles_by_symbol=candles_by_symbol,
            analysis=analysis,
            summary_text=result.summary(),
            total_return_pct=result.total_return_pct,
            win_rate=result.win_rate,
            total_trades=result.total_trades,
            inversed=args.inversed,
            tp_fraction=args.tp_fraction,
            true_sl=args.true_sl,
        )

    use_mtf = args.strategy in ("alexg", "alexg2", "institutional")
    mtf = None

    if args.csv:
        if use_mtf:
            raise RuntimeError("MTF con CSV no soportado")
        candles = load_csv(args.csv)
        symbol = args.csv
        timeframe = "csv"
    else:
        candles = load_market_data(
            args.symbol, args.period, args.interval, cache_mode=cache_mode
        )
        symbol = args.symbol
        timeframe = args.interval
        if use_mtf:
            mtf = build_full_mtf_context(
                candles,
                args.interval,
                args.symbol,
                args.period,
                cache_mode=cache_mode,
            )

    strategy = _build_strategy(args)
    config = _build_config(args)
    engine = BacktestEngine(strategy, config)
    result = engine.run(candles, symbol=symbol, timeframe=timeframe, mtf=mtf)

    if args.save_trades is not None:
        run_dir = resolve_run_dir(
            strategy=strategy.name,
            period=args.period,
            interval=args.interval,
            save_analysis=None,
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

    return ViewerSession(
        symbol=symbol,
        timeframe=timeframe,
        strategy_name=strategy.name,
        leverage=args.leverage,
        candles=candles,
        trades=result.trades,
        summary_text=result.summary(),
        total_return_pct=result.total_return_pct,
        win_rate=result.win_rate,
        total_trades=result.total_trades,
        inversed=args.inversed,
        tp_fraction=args.tp_fraction,
        true_sl=args.true_sl,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    print(f"Trade viewer: {url}", flush=True)
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
