#!/usr/bin/env python3
"""CLI para ejecutar backtests con patrones de velas."""

from __future__ import annotations

import argparse
import sys

from borex.alexg import AlexG2Strategy, AlexGMethodStrategy
from borex.backtest import BacktestConfig, BacktestEngine
from borex.institutional import InstitutionalFlowStrategy
from borex.data import build_full_mtf_context, load_csv, load_market_data
from borex.strategy import CandlePatternStrategy
from borex.strategy.base import Strategy


def _parse_leverage(value: str) -> float:
    leverage = float(value)
    if not 1 <= leverage <= 5000:
        raise argparse.ArgumentTypeError("leverage debe estar entre 1 y 5000")
    return leverage


def _parse_sl_mult(value: str) -> float:
    mult = float(value)
    if mult <= 0:
        raise argparse.ArgumentTypeError("sl-mult debe ser > 0")
    return mult


def _parse_risk_pct(value: str) -> float:
    risk = float(value)
    if not 0 < risk <= 1:
        raise argparse.ArgumentTypeError("risk-per-trade debe estar entre 0 y 1 (ej. 0.01 = 1%)")
    return risk


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Borex — backtesting con patrones, AlexG Method e Institutional Flow"
    )
    parser.add_argument(
        "--strategy",
        choices=["candles", "alexg", "alexg2", "institutional"],
        default="candles",
        help="Estrategia: candles, alexg, alexg2 o institutional",
    )
    parser.add_argument(
        "--symbol", "-s", default="EURUSD=X", help="Símbolo (yfinance)"
    )
    parser.add_argument(
        "--period", "-p", default="30d", help="Periodo histórico (yfinance)"
    )
    parser.add_argument(
        "--interval", "-i", default="1h", help="Timeframe de ejecución (mín. 15m con MTF)"
    )
    parser.add_argument(
        "--mtf",
        "-f",
        action="store_true",
        help=(
            "Multi-timeframe: exige alineación en TODOS los TF superiores "
            "(30m, 1h, 4h, 1d, 1wk según -i). AlexG lo activa por defecto."
        ),
    )
    parser.add_argument(
        "--filter-mode",
        choices=["trend", "off"],
        default="trend",
        help="Modo de filtro MTF (solo estrategia candles)",
    )
    parser.add_argument(
        "--csv", help="Ruta a CSV en lugar de yfinance (columnas Date,OHLCV)"
    )
    parser.add_argument(
        "--capital", type=float, default=10_000, help="Capital inicial"
    )
    parser.add_argument(
        "--leverage",
        "-l",
        type=_parse_leverage,
        default=1.0,
        help="Apalancamiento de 1 a 1000 (default: 1)",
    )
    parser.add_argument(
        "--maintenance-margin",
        type=float,
        default=0.0,
        help="Stop-out: liquida si equity <= margen × ratio (0 = cuenta a cero)",
    )
    parser.add_argument(
        "--stop-loss", type=float, default=0.02, help="Stop loss %% (solo candles; alexg usa estructura)"
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=0.04,
        help="Take profit %% (solo candles; alexg usa estructura)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=70.0,
        help="AlexG: score mínimo de confluencia (70=valid, 100=A+)",
    )
    parser.add_argument(
        "--min-rr",
        type=float,
        default=3.0,
        help="AlexG: risk/reward mínimo (ej. 3 = TP/SL 3:1)",
    )
    parser.add_argument(
        "--tp-fraction",
        type=float,
        default=1.0,
        help="AlexG2: TP a fracción del camino al siguiente AOI (1.0=completo, 0.7=70%%)",
    )
    parser.add_argument(
        "--max-tp-pct",
        type=float,
        default=None,
        help="AlexG: TP máximo como %% del entry (ej. 0.01 = 1%%). Limita reward a RR×SL.",
    )
    parser.add_argument(
        "--sl-mult",
        type=_parse_sl_mult,
        default=1.0,
        help="AlexG/institutional: multiplicador del SL estructural (>1 = más ancho, ej. 1.25)",
    )
    parser.add_argument(
        "--risk-per-trade",
        type=_parse_risk_pct,
        default=None,
        help="Riesgo fijo por trade si toca SL (ej. 0.01 = 1%% del equity). AlexG: 1-2%%",
    )
    parser.add_argument(
        "--size-mode",
        choices=["fixed_risk", "margin"],
        default="fixed_risk",
        help=(
            "fixed_risk: riesgo fijo en $ al SL (leverage no cambia PnL). "
            "margin: margen = cash libre × position-size; nocional = margen × leverage."
        ),
    )
    parser.add_argument(
        "--position-size",
        type=float,
        default=1.0,
        help="Fracción del cash libre (no invertido) usada como margen (default: 0.01 = 1%%)",
    )
    parser.add_argument(
        "--patterns",
        nargs="*",
        help="Patrones a usar (solo candles). Ej: hammer bullish_engulfing",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Mostrar cada trade"
    )
    parser.add_argument(
        "--inversed",
        action="store_true",
        help="Invertir trades: compras pasan a ventas y viceversa",
    )
    parser.add_argument(
        "--close-on-opposite",
        action="store_true",
        help="Cerrar posición abierta si llega una señal en dirección opuesta (default: off)",
    )
    parser.add_argument(
        "--true-sl",
        action="store_true",
        help=(
            "Margin mode: SL al wipe del margen (pierde solo el margen apostado); "
            "TP a min-rr desde ese SL. Sin esto, el margen stop sigue activo pero "
            "el SL estructural de la estrategia puede ser más lejano."
        ),
    )
    parser.add_argument(
        "--spread-pips",
        type=float,
        default=0.0,
        help="Spread en pips (round-trip: mitad en entry, mitad en exit)",
    )
    parser.add_argument(
        "--slippage-pips",
        type=float,
        default=0.0,
        help="Slippage adverso en pips por fill (entry y exit)",
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=0.0,
        help="Comisión fija USD por trade cerrado",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="Solo datos locales (Dukascopy parquet en data/cache, luego Yahoo CSV).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Forzar descarga Yahoo (ignorar cache)",
    )
    return parser.parse_args()


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
            max_tp_pct=args.max_tp_pct,
            sl_mult=args.sl_mult,
        )
    if args.strategy == "alexg2":
        return AlexG2Strategy(min_rr=args.min_rr, tp_fraction=args.tp_fraction)
    if args.strategy == "institutional":
        return InstitutionalFlowStrategy(
            min_score=args.min_score,
            min_rr=args.min_rr,
            atr_sl_mult=args.sl_mult,
        )
    strategy = CandlePatternStrategy(filter_mode=args.filter_mode)
    if args.patterns:
        strategy.enabled_patterns = set(args.patterns)
    return strategy


def _build_config(args: argparse.Namespace) -> BacktestConfig:
    base = dict(
        initial_capital=args.capital,
        leverage=args.leverage,
        maintenance_margin_ratio=args.maintenance_margin,
        inversed=args.inversed,
        spread_pips=args.spread_pips,
        slippage_pips=args.slippage_pips,
        commission_per_trade=args.commission,
        risk_per_trade_pct=args.risk_per_trade,
        close_on_opposite_signal=args.close_on_opposite,
        size_mode=args.size_mode,
        position_size_pct=args.position_size,
        true_sl=args.true_sl,
        true_sl_rr=args.min_rr,
    )
    if args.strategy in ("alexg", "alexg2", "institutional"):
        return BacktestConfig(
            **base,
            stop_loss_pct=None,
            take_profit_pct=None,
        )
    return BacktestConfig(
        **base,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
    )


def main() -> int:
    args = parse_args()
    use_mtf = args.mtf or args.strategy in ("alexg", "alexg2", "institutional")

    mtf = None
    cache_mode = _cache_mode(args)
    try:
        if args.csv:
            if use_mtf:
                print(
                    "MTF con CSV no soportado aún. Usa yfinance o quita --mtf.",
                    file=sys.stderr,
                )
                return 1
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
    except Exception as exc:
        print(f"Error cargando datos: {exc}", file=sys.stderr)
        return 1

    min_bars = (
        80
        if args.strategy in ("alexg", "alexg2")
        else 60
        if args.strategy == "institutional"
        else 20
    )
    if len(candles) < min_bars:
        print(
            f"Datos insuficientes (mínimo ~{min_bars} velas).",
            file=sys.stderr,
        )
        return 1

    strategy = _build_strategy(args)
    config = _build_config(args)

    engine = BacktestEngine(strategy, config)
    result = engine.run(candles, symbol=symbol, timeframe=timeframe, mtf=mtf)

    print(result.summary())
    print(f"Velas analizadas: {len(candles)}")
    if mtf:
        print(f"Timeframes filtro (todos deben alinear): {', '.join(mtf.filter_intervals)}")
        for interval in mtf.filter_intervals:
            print(f"  {interval}: {len(mtf.filter_candles[interval])} velas")
    print()

    if args.verbose and result.trades:
        print("Trades:")
        print("-" * 90)
        for i, t in enumerate(result.trades, 1):
            sign = "+" if t.pnl >= 0 else ""
            account_pct = t.pnl / t.entry_equity if t.entry_equity else 0.0
            margin_pct = t.pnl / t.margin if t.margin else 0.0
            lev = args.leverage
            notional = t.margin * args.leverage
            score_txt = f" score={t.score:.0f}" if t.score else ""
            sl_tp = ""
            if t.stop_loss is not None and t.take_profit is not None:
                sl_tp = f" SL={t.stop_loss:.5f} TP={t.take_profit:.5f}"
            print(
                f"  {i:3d}. {t.side.value:5s} | {t.pattern:30s} | "
                f"entry {t.entry_price:.5f} -> exit {t.exit_price:.5f}{sl_tp} | "
                f"margin ${t.margin:.2f} · notional ${notional:,.0f} ({lev:.0f}x) | "
                f"PnL {sign}{t.pnl:.2f} (acct {sign}{account_pct:.2%}, margin {sign}{margin_pct:.2%})"
                f"{score_txt} [{t.exit_reason}]"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
