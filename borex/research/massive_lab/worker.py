"""Worker: load Jun–Aug data once, execute assigned experiment chunk."""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Any

import pandas as pd

from borex.alexg import AlexG8Strategy
from borex.alexg.internet.indicators import atr as ind_atr
from borex.alexg.internet.indicators import donchian_high, donchian_low
from borex.alexg.internet.strategies import INTERNET_FNS, InternetSignal
from borex.alexg.liquidity.features import build_snapshots
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
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
    filter_session_hours,
    filter_sides,
    is_september_reserved,
    summarize_trades,
    weekly_distribution,
    weekly_metrics_table,
)
from borex.models.candle import Candle
from borex.research.massive_lab.registry import ExperimentRegistry
from borex.viewerMT5.mt5_feed import (
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
)

ROOT = Path(__file__).resolve().parents[3]
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


def _ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


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


def bt_cfg(spreads, rr_factor=1.0, rr_min=2.0, rr_max=5.0):
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
    end = min(
        clamp_end_before_september(_ts(idx["last_bar"]) + pd.Timedelta(hours=1)),
        RESEARCH_END_EXCLUSIVE,
    )
    h1_start = (end - pd.Timedelta(days=WARMUP)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    data, m1 = {}, {}
    for sym in symbols:
        bars = load_cache(sym, "1h", h1_start, end)
        if len(bars) < 120:
            continue
        data[sym] = bars
        mins = load_cache(sym, "1m", m1_start, end)
        if mins:
            m1[sym] = mins
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


def run_tape(period, tape, *, rr_factor=1.0, rr_min=2.0, ghost=4.0):
    strategy = AlexG8Strategy(
        min_rr=2.0, execution_interval="1h", peer_blend=0.0, ghost_sl_mult=float(ghost)
    )
    engine = MultiMarketEngine(
        strategy, bt_cfg(period["spreads"], rr_factor, rr_min), max_positions=0
    )
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


def prepare_n1_tape(base, params):
    tape = list(base)
    if params.get("exclude_dow") is not None:
        dow = int(params["exclude_dow"])
        tape = [
            r
            for r in tape
            if pd.Timestamp(float(r["time_unix"]), unit="s", tz="UTC").dayofweek != dow
        ]
    tape = filter_sides(tape, params.get("sides", "both"))
    tape = filter_session_hours(tape, params.get("session", "all"))
    if params.get("thursday_gate"):
        tape = filter_block_thursday_entries(tape)
    return tape


def gen_internet_tape(period, name: str, *, ghostsl: bool, thursday: bool, rr: float = 2.0):
    fn = INTERNET_FNS[name]
    tape = []
    cooldown = {}
    for sym, bars in period["data"].items():
        last = -10**9
        for i in range(120, len(bars)):
            if i - last < 8:
                continue
            if _ts(bars[i].timestamp) < period["entry_start"]:
                continue
            sig = fn(bars, i, rr=rr) if "rr" in fn.__code__.co_varnames else fn(bars, i)
            if sig is None:
                continue
            last = i
            if ghostsl:
                setup = internet_to_ghost_setup(sig, rr=rr)
                if setup is None:
                    continue
                act, _reason = activate_ghost_on_bars(setup, bars, i)
                if act is None:
                    continue
                row = ghost_setup_to_decision(act, sym)
            else:
                tu = int(_ts(sig.timestamp).timestamp())
                row = {
                    "symbol": sym,
                    "time_unix": tu,
                    "action": "buy" if sig.side == "long" else "sell",
                    "stop_loss": sig.sl,
                    "take_profit": sig.tp,
                    "pattern": sig.pattern,
                }
            tape.append(row)
    if thursday:
        tape = filter_block_thursday_entries(tape)
    return remap(tape, period["data"])


def gen_donchian_tape(period, lookback: int, *, ghostsl: bool, thursday: bool, rr: float, atr_mult: float):
    tape = []
    for sym, bars in period["data"].items():
        last = -10**9
        for i in range(max(120, lookback + 2), len(bars)):
            if i - last < 8:
                continue
            if _ts(bars[i].timestamp) < period["entry_start"]:
                continue
            hi = donchian_high(bars, i - 1, lookback)
            lo = donchian_low(bars, i - 1, lookback)
            a = ind_atr(bars, i, 14)
            if hi is None or lo is None or not a:
                continue
            c = bars[i]
            sig = None
            if c.close > hi:
                sl = c.close - atr_mult * a
                tp = c.close + rr * (c.close - sl)
                sig = InternetSignal("long", c.close, sl, tp, f"donchian|{lookback}", i, c.timestamp)
            elif c.close < lo:
                sl = c.close + atr_mult * a
                tp = c.close - rr * (sl - c.close)
                sig = InternetSignal("short", c.close, sl, tp, f"donchian|{lookback}", i, c.timestamp)
            if sig is None:
                continue
            last = i
            if ghostsl:
                setup = internet_to_ghost_setup(sig, rr=rr)
                if setup is None:
                    continue
                act, _ = activate_ghost_on_bars(setup, bars, i)
                if act is None:
                    continue
                tape.append(ghost_setup_to_decision(act, sym))
            else:
                tape.append(
                    {
                        "symbol": sym,
                        "time_unix": int(_ts(sig.timestamp).timestamp()),
                        "action": "buy" if sig.side == "long" else "sell",
                        "stop_loss": sig.sl,
                        "take_profit": sig.tp,
                        "pattern": sig.pattern,
                    }
                )
    if thursday:
        tape = filter_block_thursday_entries(tape)
    return remap(tape, period["data"])


def filter_disp(trades, periods, snaps_by_month_sym, min_body: float):
    out = []
    for t in trades:
        month = t.get("month")
        snaps = (snaps_by_month_sym.get(month) or {}).get(t["symbol"])
        bars = periods[month]["data"].get(t["symbol"])
        if not snaps or not bars:
            continue
        et = _ts(t["entry_time"]).floor("h")
        idx = next((j for j, c in enumerate(bars) if _ts(c.timestamp) == et), None)
        if idx is None or snaps[idx] is None:
            continue
        d = snaps[idx].displacement
        if d and d.body_atr >= min_body:
            out.append(t)
    return out


class WorkerContext:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
        self.symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
        self.spreads = dict(jun_idx["spread_pips_by_symbol"])
        print(f"[{worker_id}] loading periods…", flush=True)
        self.periods = {
            "june": load_period("june", JUN_INDEX, self.symbols, self.spreads),
            "july": load_period("july", JUL_INDEX, self.symbols, self.spreads),
            "august": load_period("august", AUG_INDEX, self.symbols, self.spreads),
        }
        self.base_tapes = {
            "june": remap(json.loads(JUN_TAPE.read_text(encoding="utf-8")), self.periods["june"]["data"]),
            "july": remap(json.loads(JUL_TAPE.read_text(encoding="utf-8")), self.periods["july"]["data"]),
            "august": remap(json.loads(AUG_TAPE.read_text(encoding="utf-8")), self.periods["august"]["data"]),
        }
        self._snaps = None

    def snaps(self):
        if self._snaps is None:
            print(f"[{self.worker_id}] building displacement snaps…", flush=True)
            self._snaps = {}
            for m, per in self.periods.items():
                self._snaps[m] = {}
                for sym, bars in per["data"].items():
                    self._snaps[m][sym] = build_snapshots(bars)
        return self._snaps

    def run_experiment(self, params: dict) -> dict:
        kind = params.get("kind")
        all_tr = []
        if kind == "n1_tape":
            for m in ("june", "july", "august"):
                tape = prepare_n1_tape(self.base_tapes[m], params)
                s, tr = run_tape(
                    self.periods[m],
                    tape,
                    rr_factor=params.get("rr_factor", 1.0),
                    rr_min=params.get("rr_min", 2.0),
                    ghost=params.get("ghost_sl_mult", 4.0),
                )
                for t in tr:
                    t = dict(t)
                    t["month"] = m
                    all_tr.append(t)
        elif kind in {"internet", "internet_param"}:
            name = params["name"]
            for m in ("june", "july", "august"):
                tape = gen_internet_tape(
                    self.periods[m],
                    name,
                    ghostsl=bool(params.get("ghostsl")),
                    thursday=bool(params.get("thursday_gate")),
                    rr=float(params.get("rr", 2.0)),
                )
                if not tape:
                    continue
                s, tr = run_tape(self.periods[m], tape)
                for t in tr:
                    t = dict(t)
                    t["month"] = m
                    all_tr.append(t)
        elif kind == "donchian":
            for m in ("june", "july", "august"):
                tape = gen_donchian_tape(
                    self.periods[m],
                    int(params["lookback"]),
                    ghostsl=bool(params.get("ghostsl")),
                    thursday=bool(params.get("thursday_gate")),
                    rr=float(params.get("rr", 2.0)),
                    atr_mult=float(params.get("atr_mult", 2.0)),
                )
                if not tape:
                    continue
                s, tr = run_tape(self.periods[m], tape)
                for t in tr:
                    t = dict(t)
                    t["month"] = m
                    all_tr.append(t)
        elif kind == "ghostsl_causal":
            # base / ghostsl / delayed_equiv (delayed_equiv == ghostsl geometry today)
            mode = params.get("mode")
            ghostsl = mode in {"ghostsl", "delayed_equiv"}
            for m in ("june", "july", "august"):
                tape = gen_internet_tape(
                    self.periods[m],
                    params["name"],
                    ghostsl=ghostsl,
                    thursday=False,
                    rr=float(params.get("rr", 2.0)),
                )
                if not tape:
                    continue
                s, tr = run_tape(self.periods[m], tape)
                for t in tr:
                    t = dict(t)
                    t["month"] = m
                    all_tr.append(t)
        elif kind == "n1_disp_filter":
            # run control then filter
            for m in ("june", "july", "august"):
                tape = prepare_n1_tape(
                    self.base_tapes[m],
                    {**params, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
                )
                if params.get("thursday_gate"):
                    tape = filter_block_thursday_entries(tape)
                s, tr = run_tape(self.periods[m], tape)
                for t in tr:
                    t = dict(t)
                    t["month"] = m
                    all_tr.append(t)
            all_tr = filter_disp(all_tr, self.periods, self.snaps(), float(params["min_body_atr"]))
        else:
            raise ValueError(f"unknown kind {kind}")

        full = summarize_trades(all_tr) if all_tr else {
            "fills": 0, "PF": None, "net_pnl": 0.0, "mean_trade": None, "WR": None,
            "TP": 0, "daily_close": 0, "margin_stop": 0, "long_count": 0, "short_count": 0,
        }
        wr = weekly_metrics_table(all_tr, candidate_id="x") if all_tr else []
        dist = weekly_distribution(wr) if wr else {}
        return {
            "fills": full.get("fills"),
            "PF": full.get("PF"),
            "net": full.get("net_pnl"),
            "mean": full.get("mean_trade"),
            "WR": full.get("WR"),
            "TP": full.get("TP"),
            "daily_close": full.get("daily_close"),
            "margin_stop": full.get("margin_stop"),
            "long_count": full.get("long_count"),
            "short_count": full.get("short_count"),
            "MDD": full.get("MDD"),
            "profitable_weeks": dist.get("profitable_weeks"),
            "losing_weeks": dist.get("negative_weeks"),
            "worst_week": dist.get("worst_week_net"),
            "best_week": dist.get("best_week_net"),
            "median_week": dist.get("median_weekly_net"),
        }


def run_chunk(args: tuple) -> dict:
    """Entry for ProcessPool — (worker_id, registry_path, jobs)."""
    worker_id, registry_path, jobs = args
    reg = ExperimentRegistry(Path(registry_path))
    ctx = WorkerContext(worker_id)
    done = failed = skipped = 0
    for job in jobs:
        h = job["config_hash"]
        if reg.is_done(h):
            skipped += 1
            continue
        reg.mark_running(h, worker_id)
        t0 = time.perf_counter()
        try:
            result = ctx.run_experiment(job["params"])
            reg.mark_done(h, result, time.perf_counter() - t0)
            done += 1
            print(
                f"[{worker_id}] OK {job['group_id']} {job['strategy']} "
                f"net={result.get('net')} fills={result.get('fills')}",
                flush=True,
            )
        except Exception as e:
            reg.mark_failed(h, traceback.format_exc())
            failed += 1
            print(f"[{worker_id}] FAIL {h}: {e}", flush=True)
    reg.close()
    return {"worker_id": worker_id, "done": done, "failed": failed, "skipped": skipped}
