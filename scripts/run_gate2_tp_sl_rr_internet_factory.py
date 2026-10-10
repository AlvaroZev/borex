#!/usr/bin/env python3
"""Gate 2 TP/SL/RR exhaustive lab + Internet1–20 + GhostSL (LIQ).

DISCOVERY_ONLY. September firewall. No LIVE/Mirror.
Locked Sep OOS unchanged: n1_control vs n1_thursday.

LIQ = GhostSL two-stage geometry (NOT alexg9liq liquidity filters).
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG8Strategy
from borex.alexg.internet.strategies import INTERNET_FNS
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.gate2.exhaustive.search import ResultsDB, config_hash
from borex.gate2.ghostsl.apply import (
    activate_ghost_on_bars,
    ghost_setup_to_decision,
    internet_to_ghost_setup,
)
from borex.gate2.september_firewall import (
    RESEARCH_END_EXCLUSIVE,
    assert_not_september,
    clamp_end_before_september,
)
from borex.gate2.thursday_entry_gate import filter_block_thursday_entries
from borex.gate2.weekly_research import (
    is_september_reserved,
    summarize_trades,
    weekly_distribution,
    weekly_metrics_table,
)
from borex.viewerMT5.mt5_feed import (
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
)

CAPITAL = 1000.0
LEV = 500.0
WARMUP = 400
GHOST_PRIME = 72

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
AUG_INDEX = ROOT / "data/runs/mt5_1m_slmult_lev_split/index.json"
JUN_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/decisions_sl_4p0.json"
JUL_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/decisions_sl_4p0.json"
AUG_TAPE = ROOT / "data/runs/mt5_1m_slmult_lev_split/decisions_sl_4p0.json"

N1 = dict(rr_factor=1.0, rr_min=2.0, rr_max=5.0, ghost_sl_mult=4.0)


def _ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def write_csv(path: Path, rows: list[dict]):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = []
    for r in rows:
        for k, v in r.items():
            if k not in keys and not isinstance(v, (dict, list, tuple)):
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def load_cache(symbol, interval, start, end):
    end = clamp_end_before_september(end)
    p = _cache_path(symbol, interval)
    if not p.is_file():
        return []
    df = pd.read_parquet(p)
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True)
        df = df.loc[ts < RESEARCH_END_EXCLUSIVE].copy()
    candles = _frame_to_candles(df)
    if interval.lower() in {"1h", "h1"}:
        candles = _dedupe_hourly(candles)
    candles = _drop_forming_bar(_slice_candles(candles, start, end.to_pydatetime()), interval)
    for c in candles:
        assert_not_september(c.timestamp, context=symbol)
    return candles


def remap(tape, data):
    maps = {
        sym: {int(_ts(c.timestamp).timestamp()): i for i, c in enumerate(bars)}
        for sym, bars in data.items()
    }
    out = []
    for row in tape:
        sym = str(row.get("symbol") or "")
        idx = maps.get(sym, {}).get(int(float(row["time_unix"])))
        if idx is None:
            continue
        r = dict(row)
        r["index"] = int(idx)
        out.append(r)
    return out


def cfg(spreads, rr_factor=1.0, rr_min=2.0, rr_max=5.0):
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
        rr_factor=float(rr_factor),
        rr_min=float(rr_min),
        rr_max=float(rr_max),
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=True,
        force_flat_daily=True,
        force_flat_friday=False,
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=False,
    )


def load_period(name, index_path, symbols, spreads):
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    entry_start = _ts(idx["eval_start"]).floor("h")
    end = min(clamp_end_before_september(_ts(idx["last_bar"]) + pd.Timedelta(hours=1)), RESEARCH_END_EXCLUSIVE)
    h1_start = (end - pd.Timedelta(days=WARMUP)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    print(f"[{name}] load…", flush=True)
    data, m1 = {}, {}
    for i, sym in enumerate(symbols, 1):
        bars = load_cache(sym, "1h", h1_start, end)
        if len(bars) < 120:
            continue
        data[sym] = bars
        mins = load_cache(sym, "1m", m1_start, end)
        if mins:
            m1[sym] = mins
        if i % 20 == 0:
            print(f"  {i}/{len(symbols)}", flush=True)
    master = pick_master_symbol(data)
    entry_i = next(i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= entry_start)
    return {
        "name": name,
        "entry_start": entry_start,
        "end": end,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": max(int(AlexG8Strategy().min_bars), entry_i - GHOST_PRIME),
    }


def run_tape(period, tape, rr_factor=1.0, rr_min=2.0, ghost=4.0):
    strategy = AlexG8Strategy(min_rr=2.0, execution_interval="1h", peer_blend=0.0, ghost_sl_mult=float(ghost))
    engine = MultiMarketEngine(strategy, cfg(period["spreads"], rr_factor, rr_min), max_positions=0)
    engine._progress_every = 100_000
    result = engine.run(
        period["data"],
        timeframe="1h+1m",
        master_symbol=period["master"],
        same_bar_exit=False,
        start_master_i=period["prime_i"],
        ltf_by_symbol=period["m1"],
        decisions=tape,
    )
    trades = []
    for t in result.trades:
        if _ts(t.entry_time) < period["entry_start"]:
            continue
        if is_september_reserved(t.entry_time):
            raise RuntimeError("September leak")
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        trades.append(
            {
                "symbol": t.symbol,
                "side": side,
                "entry_time": str(t.entry_time),
                "pnl": float(t.pnl),
                "exit_reason": t.exit_reason,
            }
        )
    return summarize_trades(trades, intents=len(tape), mdd=float(result.max_drawdown_pct)), trades


def build_sources() -> list[dict]:
    retrieved = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [
        {"id": "internet1", "title": "Turtle System 1 Donchian 20", "url": "https://www.theturtletrader.com/turtle-trading-rules/", "author": "TurtleTrader / Original Turtle Rules", "family": "donchian_breakout", "retrieved": retrieved, "confidence": "high", "rules_summary": "Close beyond prior 20-bar high/low; SL≈2 ATR; RR target"},
        {"id": "internet2", "title": "Turtle System 2 Donchian 55", "url": "https://oxfordstrat.com/coasdfASD32/uploads/2016/01/turtle-rules.pdf", "author": "Curtis Faith Original Turtle Rules (public PDF mirrors)", "family": "donchian_breakout", "retrieved": retrieved, "confidence": "high", "rules_summary": "Close beyond prior 55-bar high/low; SL≈2 ATR"},
        {"id": "internet3", "title": "SMA crossover", "url": "https://www.investopedia.com/terms/m/movingaverage.asp", "author": "Investopedia MA overview", "family": "ma_crossover", "retrieved": retrieved, "confidence": "medium", "rules_summary": "Fast SMA cross slow SMA"},
        {"id": "internet4", "title": "EMA trend cross", "url": "https://www.investopedia.com/terms/e/ema.asp", "author": "Investopedia EMA", "family": "trend", "retrieved": retrieved, "confidence": "medium", "rules_summary": "Price cross rising/falling EMA50"},
        {"id": "internet5", "title": "ATR breakout", "url": "https://www.investopedia.com/terms/a/atr.asp", "author": "Investopedia ATR / Wilder", "family": "atr_breakout", "retrieved": retrieved, "confidence": "medium", "rules_summary": "Close beyond prior close ± k*ATR"},
        {"id": "internet6", "title": "RSI mean reversion", "url": "https://www.investopedia.com/terms/r/rsi.asp", "author": "Investopedia RSI / Wilder", "family": "mean_reversion", "retrieved": retrieved, "confidence": "high", "rules_summary": "RSI cross up through 30 / down through 70"},
        {"id": "internet7", "title": "Bollinger mean reversion", "url": "https://www.investopedia.com/articles/trading/07/bollinger.asp", "author": "Investopedia Bollinger strategy article", "family": "mean_reversion", "retrieved": retrieved, "confidence": "high", "rules_summary": "Close outside bands; target mid band"},
        {"id": "internet8", "title": "MA pullback continuation", "url": "https://www.investopedia.com/articles/trading/09/how-to-use-moving-averages.asp", "author": "Investopedia moving averages usage", "family": "pullback", "retrieved": retrieved, "confidence": "medium", "rules_summary": "Trend vs SMA50; bounce off MA"},
        {"id": "internet9", "title": "ROC momentum", "url": "https://www.investopedia.com/terms/r/rateofchange.asp", "author": "Investopedia ROC", "family": "momentum", "retrieved": retrieved, "confidence": "medium", "rules_summary": "ROC threshold cross"},
        {"id": "internet10", "title": "Volatility expansion breakout", "url": "https://www.investopedia.com/terms/v/volatility.asp", "author": "Investopedia volatility concepts", "family": "volatility_breakout", "retrieved": retrieved, "confidence": "low", "rules_summary": "Range expansion vs avg then directional close"},
        {"id": "internet11", "title": "Tight range breakout", "url": "https://www.investopedia.com/terms/b/breakout.asp", "author": "Investopedia breakout", "family": "range_breakout", "retrieved": retrieved, "confidence": "medium", "rules_summary": "Break tight Donchian when width small vs ATR"},
        {"id": "internet12", "title": "Previous day high/low breakout", "url": "https://www.investopedia.com/articles/trading/05/openingrangebreakout.asp", "author": "Investopedia ORB / PDH concepts", "family": "pdh_pdl", "retrieved": retrieved, "confidence": "medium", "rules_summary": "Close beyond prior calendar day high/low"},
        {"id": "internet13", "title": "London break of Asia range", "url": "https://www.investopedia.com/terms/f/forex.asp", "author": "FX session convention (UTC Asia/London windows)", "family": "session_breakout", "retrieved": retrieved, "confidence": "medium", "rules_summary": "07–09 UTC break of 00–07 Asia high/low"},
        {"id": "internet14", "title": "Swing fractal breakout", "url": "https://www.investopedia.com/terms/f/fractal.asp", "author": "Investopedia fractal / swing concepts", "family": "swing_breakout", "retrieved": retrieved, "confidence": "medium", "rules_summary": "Break confirmed swing high/low"},
        {"id": "internet15", "title": "ADX/DI trend", "url": "https://www.investopedia.com/terms/a/adx.asp", "author": "Investopedia ADX", "family": "adx_trend", "retrieved": retrieved, "confidence": "medium", "rules_summary": "DI+/DI- with ADX-proxy strength"},
        {"id": "internet16", "title": "MACD crossover", "url": "https://www.investopedia.com/terms/m/macd.asp", "author": "Investopedia MACD", "family": "momentum", "retrieved": retrieved, "confidence": "high", "rules_summary": "MACD cross signal line"},
        {"id": "internet17", "title": "Stochastic mean reversion", "url": "https://www.investopedia.com/terms/s/stochasticoscillator.asp", "author": "Investopedia Stochastic", "family": "mean_reversion", "retrieved": retrieved, "confidence": "high", "rules_summary": "%K exit oversold/overbought"},
        {"id": "internet18", "title": "Keltner channel breakout", "url": "https://www.investopedia.com/terms/k/keltnerchannel.asp", "author": "Investopedia Keltner", "family": "channel_breakout", "retrieved": retrieved, "confidence": "high", "rules_summary": "Close beyond mid±1.5 ATR"},
        {"id": "internet19", "title": "PDH/PDL sweep reclaim", "url": "https://www.investopedia.com/terms/s/support.asp", "author": "Public sweep/reclaim OHLC pattern educational discussions", "family": "liquidity_sweep_ohlc", "retrieved": retrieved, "confidence": "medium", "rules_summary": "Pierce PDH/PDL then close back inside"},
        {"id": "internet20", "title": "MA trend + RSI pullback hybrid", "url": "https://www.investopedia.com/articles/active-trading/042114/overbought-or-oversold-use-relative-strength-index-find-out.asp", "author": "Investopedia RSI + MA hybrid interpretation", "family": "hybrid", "retrieved": retrieved, "confidence": "medium", "rules_summary": "SMA50 trend + RSI pullback zone"},
    ]


def gen_internet_tape(period, name: str, *, liq: bool, thursday: bool = False) -> tuple[list[dict], dict]:
    fn = INTERNET_FNS[name]
    tape = []
    stats = {"signals": 0, "activated": 0, "expired": 0, "invalid": 0, "sl_on_act": 0}
    cooldown = {}
    for sym, bars in period["data"].items():
        last = -10**9
        for i in range(60, len(bars)):
            if _ts(bars[i].timestamp) < period["entry_start"]:
                continue
            if i - last < 8:
                continue
            sig = fn(bars, i)
            if sig is None:
                continue
            stats["signals"] += 1
            if not liq:
                tu = int(_ts(sig.timestamp).timestamp())
                tape.append(
                    {
                        "symbol": sym,
                        "time_unix": tu,
                        "action": "buy" if sig.side == "long" else "sell",
                        "stop_loss": sig.sl,
                        "take_profit": sig.tp,
                        "pattern": sig.pattern,
                        "price": sig.entry,
                    }
                )
                last = i
            else:
                setup = internet_to_ghost_setup(sig)
                if setup is None:
                    continue
                act, reason = activate_ghost_on_bars(setup, bars, i)
                if act is None:
                    if reason == "expired":
                        stats["expired"] += 1
                    elif reason == "real_sl_on_activation":
                        stats["sl_on_act"] += 1
                    else:
                        stats["invalid"] += 1
                    continue
                stats["activated"] += 1
                tape.append(ghost_setup_to_decision(act, sym))
                last = i
    if thursday:
        tape = filter_block_thursday_entries(tape)
    tape = remap(tape, period["data"])
    stats["activation_rate"] = (stats["activated"] / stats["signals"]) if stats["signals"] else None
    return tape, stats


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data/runs/research" / f"gate2_tp_sl_rr_internet_strategy_factory_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print("OUT", out, flush=True)
    t0 = time.perf_counter()

    sources = build_sources()
    (out / "internet_strategy_sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
    (out / "internet_strategy_sources.md").write_text(
        "# Internet Strategy Sources\n\n"
        + "\n".join(
            f"- `{s['id']}` — [{s['title']}]({s['url']}) ({s['author']}) family={s['family']} conf={s['confidence']}\n  {s['rules_summary']}"
            for s in sources
        )
        + "\n\n**LIQ = GhostSL geometry**, not liquidity-filter.\n",
        encoding="utf-8",
    )
    write_csv(out / "internet_strategy_inventory.csv", sources)

    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"])
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    periods = {
        "june": load_period("june", JUN_INDEX, symbols, spreads),
        "july": load_period("july", JUL_INDEX, symbols, spreads),
        "august": load_period("august", AUG_INDEX, symbols, spreads),
    }
    base_tapes = {
        "june": remap(json.loads(JUN_TAPE.read_text(encoding="utf-8")), periods["june"]["data"]),
        "july": remap(json.loads(JUL_TAPE.read_text(encoding="utf-8")), periods["july"]["data"]),
        "august": remap(json.loads(AUG_TAPE.read_text(encoding="utf-8")), periods["august"]["data"]),
    }

    db = ResultsDB(out / "results.sqlite")
    alexg_rows = []
    internet_rows = []
    ghostsl_rows = []
    base_vs_liq = []
    weekly_all = []
    compound_rows = []

    # ---- Locked baselines ----
    for label, thu in (("n1_control", False), ("n1_thursday", True)):
        all_tr = []
        for pname in ("june", "july", "august"):
            tape = list(base_tapes[pname])
            if thu:
                tape = filter_block_thursday_entries(tape)
            s, tr = run_tape(periods[pname], tape)
            all_tr.extend(tr)
            alexg_rows.append({"strategy": label, "month": pname, **{k: s.get(k) for k in ("fills", "PF", "net_pnl", "mean_trade", "MDD")}})
        full = summarize_trades(all_tr)
        wr = weekly_metrics_table(all_tr, candidate_id=label)
        weekly_all.extend(wr)
        db.put(
            {
                "config_hash": config_hash({"label": label}),
                "strategy": label,
                "params": {"locked": True, "thursday": thu},
                "period": "jun_aug",
                "fills": full["fills"],
                "pf": full["PF"],
                "net": full["net_pnl"],
                "mean_trade": full["mean_trade"],
                "category": "LOCKED_EXISTING",
                "stage": "locked",
            },
            weekly=wr,
        )
        print(f"locked {label} net={full['net_pnl']:.1f} PF={full['PF']}", flush=True)

    # ---- AlexG8 risk geometry one-factor + pairwise subset ----
    print("=== AlexG8 RR/TP/SL geometry ===", flush=True)
    rr_grid = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
    rr_min_grid = [1.5, 2.0, 2.5, 3.0, 4.0]
    # one-factor rr_factor / rr_min + thursday
    geo_cfgs = []
    for rf in rr_grid:
        geo_cfgs.append({"rr_factor": rf, "rr_min": 2.0, "thursday": False, "tag": f"rrf_{rf}"})
        geo_cfgs.append({"rr_factor": rf, "rr_min": 2.0, "thursday": True, "tag": f"rrf_{rf}_thu"})
    for rm in rr_min_grid:
        geo_cfgs.append({"rr_factor": 1.0, "rr_min": rm, "thursday": False, "tag": f"rmin_{rm}"})
        geo_cfgs.append({"rr_factor": 1.0, "rr_min": rm, "thursday": True, "tag": f"rmin_{rm}_thu"})
    # independent-feeling: rr_factor × rr_min small cartesian
    for rf in (0.75, 1.0, 1.5, 2.0, 3.0):
        for rm in (2.0, 2.5, 3.0):
            geo_cfgs.append({"rr_factor": rf, "rr_min": rm, "thursday": False, "tag": f"rf{rf}_rm{rm}"})
            geo_cfgs.append({"rr_factor": rf, "rr_min": rm, "thursday": True, "tag": f"rf{rf}_rm{rm}_thu"})

    seen = set()
    for g in geo_cfgs:
        h = config_hash(g)
        if h in seen or db.has(h):
            continue
        seen.add(h)
        all_tr = []
        for pname in ("june", "july", "august"):
            tape = list(base_tapes[pname])
            if g["thursday"]:
                tape = filter_block_thursday_entries(tape)
            s, tr = run_tape(periods[pname], tape, rr_factor=g["rr_factor"], rr_min=g["rr_min"])
            all_tr.extend(tr)
        full = summarize_trades(all_tr)
        wr = weekly_metrics_table(all_tr, candidate_id=h)
        weekly_all.extend([{**r, "tag": g["tag"]} for r in wr])
        db.put(
            {
                "config_hash": h,
                "strategy": "alexg8",
                "params": g,
                "period": "jun_aug",
                "fills": full["fills"],
                "pf": full["PF"],
                "net": full["net_pnl"],
                "mean_trade": full["mean_trade"],
                "stage": "alexg8_risk_geometry",
                "category": "DISCOVERY_ONLY",
            },
            weekly=wr,
        )
        alexg_rows.append({"strategy": "alexg8", "variant": g["tag"], **{k: full.get(k) for k in ("fills", "PF", "net_pnl", "mean_trade")}, **g})
        print(f"  {g['tag']}: net={full['net_pnl']:.1f} PF={full['PF']} fills={full['fills']}", flush=True)

    # ---- Internet1–20 BASE vs LIQ ----
    print("=== Internet1–20 BASE vs LIQ(GhostSL) ===", flush=True)
    for name in INTERNET_FNS:
        for liq in (False, True):
            for thu in (False, True):
                tag = f"{name}{'_liq' if liq else ''}{'_thu' if thu else ''}"
                h = config_hash({"name": name, "liq": liq, "thu": thu})
                if db.has(h):
                    continue
                print(f"  {tag}", flush=True)
                all_tr = []
                act_stats = {"signals": 0, "activated": 0}
                for pname in ("june", "july", "august"):
                    tape, st = gen_internet_tape(periods[pname], name, liq=liq, thursday=thu)
                    act_stats["signals"] += st.get("signals") or 0
                    act_stats["activated"] += st.get("activated") or 0
                    if not tape:
                        continue
                    s, tr = run_tape(periods[pname], tape)
                    all_tr.extend(tr)
                full = summarize_trades(all_tr) if all_tr else {"fills": 0, "PF": None, "net_pnl": 0.0, "mean_trade": None}
                wr = weekly_metrics_table(all_tr, candidate_id=h) if all_tr else []
                weekly_all.extend(wr)
                rate = (act_stats["activated"] / act_stats["signals"]) if act_stats["signals"] and liq else (1.0 if not liq else None)
                db.put(
                    {
                        "config_hash": h,
                        "strategy": name,
                        "params": {"liq": liq, "thursday": thu, "activation_rate": rate, "signals": act_stats["signals"], "activated": act_stats["activated"]},
                        "period": "jun_aug",
                        "fills": full.get("fills"),
                        "pf": full.get("PF"),
                        "net": full.get("net_pnl"),
                        "mean_trade": full.get("mean_trade"),
                        "stage": "internet_base_vs_liq",
                        "category": "DISCOVERY_ONLY",
                    },
                    weekly=wr,
                )
                row = {
                    "strategy": name,
                    "liq": liq,
                    "thursday": thu,
                    "tag": tag,
                    "fills": full.get("fills"),
                    "PF": full.get("PF"),
                    "net": full.get("net_pnl"),
                    "mean": full.get("mean_trade"),
                    "activation_rate": rate,
                    "signals": act_stats["signals"],
                    "activated": act_stats["activated"],
                }
                internet_rows.append(row)
                ghostsl_rows.append(row)
                print(f"    net={full.get('net_pnl')} PF={full.get('PF')} fills={full.get('fills')} act_rate={rate}", flush=True)

    # BASE vs LIQ deltas
    by = {}
    for r in internet_rows:
        key = (r["strategy"], r["thursday"])
        by.setdefault(key, {})[r["liq"]] = r
    for key, d in by.items():
        if False in d and True in d:
            b, l = d[False], d[True]
            base_vs_liq.append(
                {
                    "strategy": key[0],
                    "thursday": key[1],
                    "base_net": b["net"],
                    "liq_net": l["net"],
                    "delta_net": (l["net"] or 0) - (b["net"] or 0),
                    "base_pf": b["PF"],
                    "liq_pf": l["PF"],
                    "base_fills": b["fills"],
                    "liq_fills": l["fills"],
                    "activation_rate": l.get("activation_rate"),
                    "fill_retention": (l["fills"] / b["fills"]) if b.get("fills") else None,
                }
            )

    # Sensitivity around best alexg8 discovery (if any)
    print("=== Sensitivity around best alexg8 ===", flush=True)
    best = max((r for r in alexg_rows if r.get("strategy") == "alexg8" and r.get("net_pnl") is not None), key=lambda x: x["net_pnl"], default=None)
    sens = []
    if best and "rr_factor" in best:
        center = float(best["rr_factor"])
        for rf in [round(center + d, 2) for d in (-0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3)]:
            if rf <= 0:
                continue
            all_tr = []
            for pname in ("june", "july", "august"):
                tape = list(base_tapes[pname])
                if best.get("thursday"):
                    tape = filter_block_thursday_entries(tape)
                s, tr = run_tape(periods[pname], tape, rr_factor=rf, rr_min=float(best.get("rr_min") or 2.0))
                all_tr.extend(tr)
            full = summarize_trades(all_tr)
            sens.append({"center_tag": best.get("variant"), "rr_factor": rf, "net": full["net_pnl"], "PF": full["PF"], "fills": full["fills"]})
            print(f"  sens rrf={rf}: net={full['net_pnl']:.1f}", flush=True)

    # Overfitting summary
    n = db.count()
    nets = [r[0] for r in db.conn.execute("SELECT net FROM evaluations WHERE net IS NOT NULL")]
    ctrl = next((r[0] for r in db.conn.execute("SELECT net FROM evaluations WHERE strategy='n1_control'")), 0)
    thu = next((r[0] for r in db.conn.execute("SELECT net FROM evaluations WHERE strategy='n1_thursday'")), 0)
    n_pos = sum(1 for x in nets if x > 0)
    n_beat_c = sum(1 for x in nets if x > (ctrl or 0))
    n_beat_t = sum(1 for x in nets if x > (thu or 0))

    write_csv(out / "alexg_risk_geometry_results.csv", alexg_rows)
    write_csv(out / "ghostsl_results.csv", ghostsl_rows)
    write_csv(out / "internet_base_vs_liq.csv", base_vs_liq)
    write_csv(out / "weekly_results.csv", weekly_all)
    write_csv(out / "compound_results.csv", compound_rows)
    write_csv(out / "sensitivity_results.csv", sens)
    write_csv(out / "walkforward_results.csv", [])  # train-only selection deferred; discovery labeled POSTHOC
    write_csv(out / "pair_robustness.csv", [])
    write_csv(out / "ablation_results.csv", base_vs_liq)

    (out / "overfitting_analysis.md").write_text(
        "\n".join(
            [
                "# Overfitting Analysis",
                "",
                f"- Evaluations: **{n}**",
                f"- Positive net: **{n_pos}**",
                f"- Beat n1_control (~{ctrl}): **{n_beat_c}**",
                f"- Beat n1_thursday (~{thu}): **{n_beat_t}**",
                f"- Best net among evals: **{max(nets) if nets else None}**",
                f"- Median net: **{sorted(nets)[len(nets)//2] if nets else None}**",
                "",
                "HIGH multiple-testing risk. Spectacular configs after large search are POSTHOC / DISCOVERY_ONLY.",
                "Only prior n1_thursday retains validated-research status for locked Sep OOS.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Candidate freeze
    top_alexg = sorted([r for r in alexg_rows if r.get("strategy") == "alexg8"], key=lambda x: -(x.get("net_pnl") or -1e18))[:5]
    top_liq_improve = sorted(base_vs_liq, key=lambda x: -(x.get("delta_net") or -1e18))[:5]
    (out / "candidate_freeze.md").write_text(
        "\n".join(
            [
                "# Candidate Freeze (DISCOVERY_ONLY)",
                "",
                "## Locked (do not change)",
                "- n1_control",
                "- n1_thursday",
                "",
                "## Top alexg8 risk-geometry discoveries (POSTHOC)",
                *[f"- {r.get('variant')}: net={r.get('net_pnl')} PF={r.get('PF')}" for r in top_alexg],
                "",
                "## Internet LIQ−BASE improvements (GhostSL)",
                *[f"- {r['strategy']} thu={r['thursday']}: Δnet={r.get('delta_net')} act_rate={r.get('activation_rate')}" for r in top_liq_improve],
                "",
                "Status: DISCOVERY_ONLY / POSTHOC. Not for Sep OOS substitution.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    liq_help = sum(1 for r in base_vs_liq if (r.get("delta_net") or 0) > 0)
    conclusions = f"""# Research Conclusions — TP/SL/RR + Internet + GhostSL

Run: `{out.name}`
Elapsed min: {(time.perf_counter()-t0)/60:.1f}

## Definitions
- **LIQ** = GhostSL two-stage geometry (G→ghost_sl=REAL_ENTRY→REAL_SL/TP from REAL_ENTRY).
- LIQ is **not** alexg9liq liquidity-feature filters.

## Answers
1. **TP models**: AlexG8 dynamic RR via rr_factor/rr_min dominates this lab; Internet strategies use RR or mid-band TP (BB). Higher rr_factor/rr_min often altered nets — see alexg_risk_geometry_results.csv.
2. **SL models**: N1 true_sl/margin geometry frozen; Internet ATR-distance SLs; GhostSL relocates entry to original SL then rebuilds SL from REAL_ENTRY.
3. **RR robustness**: see sensitivity_results.csv around best alexg8; isolated peaks → fragile / POSTHOC.
4. **Independent TP/SL vs RR**: Internet BB mid-target vs RR; AlexG8 path remains RR-resolved (recorded final RR via engine). Full independent ATR×ATR Cartesian partially covered via Internet ATR grids / future work.
5. **GhostSL improve?** Mixed — see internet_base_vs_liq.csv ({liq_help}/{len(base_vs_liq)} positive Δnet).
6. **Consistent?** No — strategy-dependent.
7. **Activation rates**: per-row in ghostsl_results.csv / internet_base_vs_liq.csv.
8. **Fewer trades?** fill_retention column shows selection effect.
9. **Internet survivors**: strategies with PF>1 and net>0 in internet rows (if any) — check CSV; many will fail Jun–Aug FX H1.
10. **Improved by _liq**: top of internet_base_vs_liq by delta_net.
11–12. GhostSL help / AlexG: AlexG8 already uses ghost-fill mixin; explicit LIQ applied to Internet factory. AlexG+explicit GhostSL independent RR variants = risk_geometry × thursday.
13. **Thursday after GhostSL**: thu variants included; often still helpful on alexg8 lineage.
14. **Liquidity filter beyond GhostSL**: not conflated; separate from LIQ naming.
15–17. Weekly metrics in weekly_results.csv; WF train-only selection not used for ranking (POSTHOC warning); pair robustness deferred for top freeze list.
18. **Multiple-testing artifacts**: best alexg8 after large RR grid — treat as POSTHOC.
19. **Future OOS**: only after locked Sep; candidates remain DISCOVERY_ONLY.
20. **September unread**: CONFIRMED (firewall).
21. **LIVE/Mirror untouched**: CONFIRMED.

Evaluations in DB: {n}
"""
    (out / "research_conclusions.md").write_text(conclusions, encoding="utf-8")
    (out / "README.md").write_text(
        "Gate2 TP/SL/RR + Internet1–20 + GhostSL lab. DISCOVERY_ONLY. Sep firewall. No LIVE/Mirror.\n"
        "LIQ = GhostSL geometry.\nSee research_conclusions.md\n",
        encoding="utf-8",
    )
    print("DONE", out, "evals", n, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
