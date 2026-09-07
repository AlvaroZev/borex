"""Closest broker-sim 1m path: H1 alexg8 signals, 1m SL/TP order.

Scan is independent of occupancy. ghost_sl_mult is a signal-side knob
(distance from planned ghost entry to ghost SL). Each mult gets its own
tape; each leverage is an exec-only replay of that tape.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
load_dotenv(LIVE / ".env")

OUT = ROOT / "data" / "runs" / "mt5_1m_slmult_lev_split"
CAPITAL = 1000.0
WARMUP_DAYS = 400
GHOST_PRIME_BARS = 72
LEVERAGES = (5000.0, 1000.0, 500.0, 100.0, 10.0)
SL_MULTS = (1.2, 1.6, 2.0, 2.4, 2.8, 3.2)
# 0 = no book-size cap. Still one open trade per pair at fill time.
MAX_POSITIONS = 0
KNOBS = dict(
    strategy="alexg8",
    rr_mode="dynamic",
    true_sl_rr=2.0,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=5.0,
    risk_include_commission=True,
    commission_at_entry=False,
    force_flat_friday=False,
    force_flat_daily=True,
    intra_hour_sl=False,
    path="h1_signal_1m_exit",
    max_positions=MAX_POSITIONS,
    signal_exec_split=True,
)


def _mult_tag(mult: float) -> str:
    return str(mult).replace(".", "p")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _attach_mt5():
    from borex_live.mt5.client import Mt5Client

    path = os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", "")
    client = Mt5Client(path=path)
    client.connect()
    return client


def load_mt5(
    h1_start: datetime,
    end: datetime,
    m1_start: datetime,
    *,
    symbols: list[str] | None = None,
):
    from borex.viewerMT5.mt5_feed import (
        _dedupe_hourly,
        _drop_forming_bar,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
        read_spread_pips,
    )

    print(f"Loading MT5 H1 {h1_start.date()} -> {end.date()}", flush=True)
    print(f"Loading MT5 1m {m1_start.date()} -> {end.date()}", flush=True)
    client = _attach_mt5()
    off = int(getattr(client, "server_offset_seconds", 0) or 0)
    print(f"MT5 server offset {off}s ({off / 3600:.1f}h)", flush=True)
    if off == 0:
        print(
            "WARNING: offset probe is 0 (August lab was +3h). "
            "Cached H1 is reused as-is; new 1m fetches use this probe.",
            flush=True,
        )
    h1: dict = {}
    m1: dict = {}
    spreads: dict[str, float] = {}
    tail_start = end - timedelta(days=21)
    try:
        if symbols is None:
            symbols = list_tradeable_yahoo_symbols(client=client)
        else:
            print(f"Pinned universe n={len(symbols)}", flush=True)
        for i, sym in enumerate(symbols, 1):
            try:
                hist = fetch_mt5_candles(
                    sym, "1h", h1_start, end, client=client, use_cache=True, write_cache=True
                )
            except Exception as exc:
                print(f"  H1 skip {sym}: {exc}", flush=True)
                continue
            bars = list(hist)
            try:
                tail = fetch_mt5_candles(
                    sym,
                    "1h",
                    tail_start,
                    end,
                    client=client,
                    use_cache=False,
                    write_cache=True,
                )
                bars = _dedupe_hourly(bars + list(tail))
            except Exception as exc:
                print(f"  H1 tail skip {sym}: {exc}", flush=True)
                bars = _dedupe_hourly(bars)
            bars = _drop_forming_bar(bars, "1h")
            if len(bars) < 120:
                if i % 15 == 0 or i == len(symbols):
                    print(f"  skip {sym} H1={len(bars)}", flush=True)
                continue
            h1[sym] = bars
            spreads[sym] = read_spread_pips(client, sym)
            try:
                minutes = fetch_mt5_candles(
                    sym,
                    "1m",
                    m1_start,
                    end,
                    client=client,
                    use_cache=True,
                    write_cache=True,
                )
                minutes = _drop_forming_bar(list(minutes), "1m")
            except Exception as exc:
                print(f"  1m skip {sym}: {exc}", flush=True)
                minutes = []
            if minutes:
                m1[sym] = minutes
            if i % 5 == 0 or i == len(symbols):
                last_h = bars[-1].timestamp
                last_m = minutes[-1].timestamp if minutes else "-"
                print(
                    f"  {i}/{len(symbols)} H1={len(h1)} 1m={len(m1)} "
                    f"lastH1={last_h} last1m={last_m}",
                    flush=True,
                )
    finally:
        client._connected = False
        client._mt5 = None

    minutes = {
        pd.Timestamp(c.timestamp).minute
        for bars in h1.values()
        for c in bars[-48:]
    }
    print(f"Pairs H1={len(h1)} 1m={len(m1)} H1 minutes={sorted(minutes)}", flush=True)
    if minutes - {0}:
        raise SystemExit(f"H1 grid is not on the hour: {sorted(minutes)}")
    if spreads:
        vals = sorted(spreads.values())
        mid = vals[len(vals) // 2]
        print(
            f"Spreads n={len(spreads)} median={mid:.2f} pip "
            f"min={vals[0]:.2f} max={vals[-1]:.2f}",
            flush=True,
        )
    return h1, m1, spreads


def _floats(raw: str) -> tuple[float, ...]:
    return tuple(float(x.strip()) for x in str(raw).split(",") if x.strip())


def _cfg(leverage: float, spreads: dict[str, float], *, rr_factor: float | None = None):
    from borex.backtest.engine import BacktestConfig

    factor = float(KNOBS["rr_factor"] if rr_factor is None else rr_factor)
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=float(leverage),
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(KNOBS["true_sl_rr"]),
        rr_mode=KNOBS["rr_mode"],
        rr_factor=factor,
        rr_min=float(KNOBS["rr_min"]),
        rr_max=float(KNOBS["rr_max"]),
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=bool(KNOBS["risk_include_commission"]),
        commission_at_entry=bool(KNOBS["commission_at_entry"]),
        force_flat_friday=bool(KNOBS["force_flat_friday"]),
        force_flat_daily=bool(KNOBS["force_flat_daily"]),
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=bool(KNOBS["intra_hour_sl"]),
    )


def _summarize(trades, result, capital: float) -> dict:
    net = sum(float(t.pnl) for t in trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl <= 0)
    loss_sum = sum(t.pnl for t in trades if t.pnl <= 0)
    win_sum = sum(t.pnl for t in trades if t.pnl > 0)
    return {
        "account": capital + net,
        "net_pnl": net,
        "return_pct": (net / capital) if capital else 0.0,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / len(trades)) if trades else 0.0,
        "profit_factor": (
            (win_sum / abs(loss_sum)) if loss_sum and loss_sum != 0 else 0.0
        ),
        "max_drawdown_pct": float(result.max_drawdown_pct),
        "avg_planned_rr": float(result.avg_planned_rr),
        "full_run_equity": float(result.final_equity),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
    }


def _trade_row(t) -> dict:
    return {
        "symbol": t.symbol,
        "side": t.side.value if hasattr(t.side, "value") else str(t.side),
        "entry_time": str(t.entry_time),
        "exit_time": str(t.exit_time),
        "entry": float(t.entry_price),
        "exit": float(t.exit_price or 0),
        "sl": float(t.stop_loss or 0),
        "tp": float(t.take_profit or 0),
        "pnl": float(t.pnl),
        "margin": float(t.margin or 0),
        "rr": float(t.score or 0),
        "exit_reason": t.exit_reason,
        "pattern": str(t.pattern or ""),
    }


def run_one(
    leverage: float,
    data,
    m1,
    spreads,
    master,
    prime_i,
    entry_start,
    tag: str,
    *,
    ghost_sl_mult: float,
    rr_factor: float | None = None,
    decisions=None,
    collect_decisions=None,
):
    from borex.alexg import AlexG8Strategy
    from borex.backtest.multi_market_engine import MultiMarketEngine

    factor = float(KNOBS["rr_factor"] if rr_factor is None else rr_factor)
    strategy = AlexG8Strategy(
        min_rr=float(KNOBS["true_sl_rr"]),
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(ghost_sl_mult),
    )
    config = _cfg(leverage, spreads, rr_factor=factor)
    engine = MultiMarketEngine(strategy, config, max_positions=MAX_POSITIONS)
    engine._progress_every = 100
    t0 = time.perf_counter()
    result = engine.run(
        data,
        timeframe="1h+1m",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=prime_i,
        ltf_by_symbol=m1,
        decisions=decisions,
        collect_decisions=collect_decisions,
    )
    elapsed = time.perf_counter() - t0
    window = [t for t in result.trades if _ts(t.entry_time) >= entry_start]
    knobs = {
        **KNOBS,
        "ghost_sl_mult": float(ghost_sl_mult),
        "leverage": float(leverage),
        "rr_factor": factor,
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "leverage": leverage,
        "ghost_sl_mult": float(ghost_sl_mult),
        "rr_factor": factor,
        "capital": CAPITAL,
        "entry_start": str(entry_start),
        "last_bar": str(data[master][-1].timestamp),
        "master": master,
        "h1_pairs": len(data),
        "m1_pairs": len(m1),
        "knobs": knobs,
        "same_bar_exit": False,
        "intents": len(collect_decisions or decisions or []),
        "summary": _summarize(window, result, CAPITAL),
        "warmup_trades": int(result.total_trades) - len(window),
        "trades": [_trade_row(t) for t in window],
        "elapsed_sec": elapsed,
    }
    path = OUT / f"{tag}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    s = report["summary"]
    print(
        f"[{tag}] sl_mult={ghost_sl_mult:g} lev={leverage:g} rr={factor:g} "
        f"account=${s['account']:,.2f} net=${s['net_pnl']:,.2f} "
        f"trades={s['trades']} WR={s['win_rate']*100:.1f}% "
        f"PF={s['profit_factor']:.2f} MDD={s['max_drawdown_pct']*100:.1f}% "
        f"exits={s['exit_reasons']} in {elapsed/60:.1f}m -> {path}",
        flush=True,
    )
    return {
        "tag": tag,
        "leverage": leverage,
        "ghost_sl_mult": float(ghost_sl_mult),
        "rr_factor": factor,
        "intents": report["intents"],
        "report": str(path),
        "summary": s,
        "elapsed_sec": elapsed,
    }


def _run_tag(mult: float, lev: float, rr_factor: float, *, include_rr: bool) -> str:
    tag = f"sl_{_mult_tag(mult)}_lev_{int(lev)}"
    if include_rr:
        tag = f"{tag}_rr_{_mult_tag(rr_factor)}"
    return tag


def main() -> int:
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-days", type=int, default=30)
    parser.add_argument(
        "--end",
        default="",
        help="UTC end (ISO). Default: now. Pin this to continue a grid.",
    )
    parser.add_argument(
        "--sl-mults",
        default=",".join(str(x) for x in SL_MULTS),
        help="Comma-separated ghost_sl_mult values",
    )
    parser.add_argument(
        "--leverages",
        default=",".join(str(int(x)) for x in LEVERAGES),
        help="Comma-separated leverages (exec-only)",
    )
    parser.add_argument(
        "--rr-factors",
        default=str(KNOBS["rr_factor"]),
        help="Comma-separated TP multipliers (rr_factor, exec-only)",
    )
    parser.add_argument(
        "--replay-tape",
        default="",
        help="Reuse a saved decisions_sl_*.json tape (skip scan)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output directory (default: data/runs/mt5_1m_slmult_lev_split)",
    )
    parser.add_argument("--rr-min", type=float, default=None, help="Override rr_min clamp")
    parser.add_argument("--rr-max", type=float, default=None, help="Override rr_max clamp")
    parser.add_argument(
        "--symbols-from",
        default="",
        help="JSON index with spread_pips_by_symbol — pin the same universe",
    )
    args = parser.parse_args()
    sl_mults = _floats(args.sl_mults)
    levers = _floats(args.leverages)
    rr_factors = _floats(args.rr_factors)
    include_rr = len(rr_factors) > 1 or (rr_factors and rr_factors[0] != 1.0)
    if args.rr_min is not None:
        KNOBS["rr_min"] = float(args.rr_min)
    if args.rr_max is not None:
        KNOBS["rr_max"] = float(args.rr_max)
    if args.out:
        out = Path(args.out)
        OUT = out if out.is_absolute() else ROOT / out

    if args.end:
        end = datetime.fromisoformat(str(args.end).replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        else:
            end = end.astimezone(timezone.utc)
    else:
        end = datetime.now(timezone.utc)
    eval_start = end - timedelta(days=int(args.eval_days))
    h1_start = end - timedelta(days=WARMUP_DAYS)
    m1_start = eval_start - timedelta(days=2)

    pin_symbols = None
    if args.symbols_from:
        src = Path(args.symbols_from)
        if not src.is_absolute():
            src = ROOT / src
        prev = json.loads(src.read_text(encoding="utf-8"))
        pin_symbols = sorted((prev.get("spread_pips_by_symbol") or {}).keys())
        if not pin_symbols:
            raise SystemExit(f"no symbols in {src}")
    data, m1, spreads = load_mt5(h1_start, end, m1_start, symbols=pin_symbols)
    if not data:
        raise SystemExit("no MT5 H1")
    if not m1:
        raise SystemExit("no MT5 1m")

    from borex.alexg.multi_market import pick_master_symbol

    master = pick_master_symbol(data)
    master_bars = data[master]
    entry_start = _ts(eval_start).floor("h")
    entry_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= entry_start),
        None,
    )
    if entry_i is None:
        raise SystemExit(f"no master bar on/after {entry_start}")
    from borex.alexg import AlexG8Strategy

    warmup = int(AlexG8Strategy().min_bars)
    prime_i = max(warmup, entry_i - GHOST_PRIME_BARS)
    replay_tape = None
    if args.replay_tape:
        tape_src = Path(args.replay_tape)
        if not tape_src.is_absolute():
            tape_src = ROOT / tape_src
        replay_tape = json.loads(tape_src.read_text(encoding="utf-8"))
        if not isinstance(replay_tape, list):
            raise SystemExit(f"replay tape is not a list: {tape_src}")
        print(f"Replaying tape {tape_src} n={len(replay_tape)}", flush=True)
    print(
        f"Eval {entry_start} -> {master_bars[-1].timestamp} | "
        f"engine from master {prime_i}/{len(master_bars)} "
        f"ghost={entry_i - prime_i} 1m pairs={len(m1)} | "
        f"sl_mults={list(sl_mults)} levs={list(levers)} rr={list(rr_factors)} "
        f"rr_min={KNOBS['rr_min']:g} rr_max={KNOBS['rr_max']:g}",
        flush=True,
    )

    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    tapes: dict[str, int] = {}
    for mult in sl_mults:
        mtag = _mult_tag(mult)
        tape: list[dict] | None = list(replay_tape) if replay_tape is not None else None
        if tape is not None:
            tapes[mtag] = len(tape)
        print(
            f"=== ghost_sl_mult={mult:g} {'replay' if tape is not None else 'scan'} ===",
            flush=True,
        )
        for factor in rr_factors:
            for lev in levers:
                tag = _run_tag(mult, lev, factor, include_rr=include_rr)
                if tape is None:
                    tape = []
                    rows.append(
                        run_one(
                            lev,
                            data,
                            m1,
                            spreads,
                            master,
                            prime_i,
                            entry_start,
                            tag,
                            ghost_sl_mult=mult,
                            rr_factor=factor,
                            collect_decisions=tape,
                        )
                    )
                    tape_path = OUT / f"decisions_sl_{mtag}.json"
                    tape_path.write_text(json.dumps(tape, indent=2), encoding="utf-8")
                    tapes[mtag] = len(tape)
                    print(
                        f"Signal tape sl_mult={mult:g} n={len(tape)} -> {tape_path}",
                        flush=True,
                    )
                else:
                    rows.append(
                        run_one(
                            lev,
                            data,
                            m1,
                            spreads,
                            master,
                            prime_i,
                            entry_start,
                            tag,
                            ghost_sl_mult=mult,
                            rr_factor=factor,
                            decisions=tape,
                        )
                    )
        _write_index(
            rows,
            tapes,
            sl_mults,
            levers,
            rr_factors,
            master,
            master_bars,
            entry_start,
            data,
            m1,
            spreads,
        )

    print(f"Index -> {OUT / 'index.json'}", flush=True)
    for row in rows:
        s = row["summary"]
        print(
            f"  sl={row['ghost_sl_mult']:<4g} lev={row['leverage']:>6g} "
            f"rr={row.get('rr_factor', 1):<4g} "
            f"intents={row['intents']:>5} trades={s['trades']:>4} "
            f"WR={s['win_rate']*100:5.1f}% PF={s['profit_factor']:5.2f} "
            f"net=${s['net_pnl']:>9.2f} end=${s['account']:>9.2f}",
            flush=True,
        )
    return 0


def _write_index(
    rows,
    tapes,
    sl_mults,
    levers,
    rr_factors,
    master,
    master_bars,
    entry_start,
    data,
    m1,
    spreads,
) -> None:
    prev_runs: list = []
    prev_tapes: dict = {}
    prev_mults: list = []
    prev_levers: list = []
    prev_rr: list = []
    path = OUT / "index.json"
    if path.is_file():
        prev = json.loads(path.read_text(encoding="utf-8"))
        prev_runs = list(prev.get("runs") or [])
        prev_tapes = dict(prev.get("intents_by_sl_mult") or {})
        prev_mults = list(prev.get("sl_mults") or [])
        prev_levers = list(prev.get("leverages") or [])
        prev_rr = list(prev.get("rr_factors") or [])
    by_tag = {r["tag"]: r for r in prev_runs if r.get("tag")}
    for r in rows:
        by_tag[r["tag"]] = r
    merged_tapes = {**prev_tapes, **tapes}
    merged_mults = sorted({float(x) for x in list(prev_mults) + list(sl_mults)})
    merged_levers = sorted(
        {float(x) for x in list(prev_levers) + list(levers)}, reverse=True
    )
    merged_rr = sorted({float(x) for x in list(prev_rr) + list(rr_factors)})
    merged_runs = sorted(
        by_tag.values(),
        key=lambda r: (
            float(r.get("ghost_sl_mult") or 0),
            float(r.get("rr_factor") or 0),
            float(r.get("leverage") or 0),
        ),
    )
    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "closest_sim": (
            "ghost_sl_mult is signal-side (own tape per mult). "
            "Leverage and rr_factor (TP multiplier) are exec-only replay "
            "of that tape on the 1m path. rr_min/rr_max still clamp after the factor."
        ),
        "capital": CAPITAL,
        "eval_start": str(entry_start),
        "last_bar": str(master_bars[-1].timestamp),
        "master": master,
        "h1_pairs": len(data),
        "m1_pairs": len(m1),
        "sl_mults": merged_mults,
        "leverages": merged_levers,
        "rr_factors": merged_rr,
        "intents_by_sl_mult": merged_tapes,
        "knobs": KNOBS,
        "spread_pips_by_symbol": {k: round(v, 4) for k, v in sorted(spreads.items())},
        "runs": merged_runs,
    }
    (OUT / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
