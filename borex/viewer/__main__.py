from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from borex.alexg import AlexG2Strategy, AlexG3Strategy, AlexGMethodStrategy
from borex.alexg.multi_market import default_forex_universe, pick_master_symbol
from borex.backtest import BacktestConfig, BacktestEngine, MultiMarketEngine
from borex.data import build_full_mtf_context, load_csv, load_market_data
from borex.institutional import InstitutionalFlowStrategy
from borex.strategy import CandlePatternStrategy
from borex.strategy.base import Strategy
from borex.viewer.context import ViewerSession
from borex.viewer.server import create_app, set_session

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
        choices=["candles", "alexg", "alexg2", "alexg3", "institutional"],
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
    )
    if args.strategy in ("alexg", "alexg2", "alexg3", "institutional"):
        return BacktestConfig(**base, stop_loss_pct=None, take_profit_pct=None)
    return BacktestConfig(**base)


def run_session(args: argparse.Namespace) -> ViewerSession:
    cache_mode = _cache_mode(args)

    if args.strategy == "alexg3":
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
        engine = MultiMarketEngine(strategy, config, max_positions=args.max_positions)
        result = engine.run(
            candles_by_symbol, timeframe=args.interval, master_symbol=master
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
    print(f"Trades to inspect: {session.total_trades}", flush=True)

    if not args.no_browser:
        webbrowser.open(url)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
