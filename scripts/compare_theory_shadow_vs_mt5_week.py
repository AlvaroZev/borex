#!/usr/bin/env python3
"""Compare offline MT5 alexg8 week backtest vs the live theory shadow book.

Mirrors ShadowEngine mechanics:
  - warm strategy state on prior MT5 H1 bars (no portfolio)
  - fresh portfolio at eval start with live capital/params
  - same MultiMarketEngine open/exit path, same_bar_exit=False
  - do NOT force end_of_data closes (shadow leaves opens open)
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
load_dotenv(LIVE / ".env")

# Theory shadow epoch began 2026-08-14 17:00 UTC (first live attach).
EPOCH_START = pd.Timestamp("2026-08-14T17:00:00+00:00")
# Trading week used for the live review.
WEEK_START = pd.Timestamp("2026-08-17T00:00:00+00:00")
WEEK_END = pd.Timestamp("2026-08-22T00:00:00+00:00")
EVAL_START = EPOCH_START
EVAL_END = WEEK_END
# Use deep on-the-hour cache for geometry; live_candles covers the week.
WARMUP_DAYS = 400
CAPITAL = 889.69
OUT = ROOT / "data" / "runs" / "theory_shadow_vs_mt5_week_aug17"


def _pip_size(symbol: str) -> float:
    sym = symbol.upper().replace("=X", "")
    return 0.01 if "JPY" in sym else 0.0001


def prequantize(candles, symbol: str, pips: float):
    from borex.models.candle import Candle

    step = pips * _pip_size(symbol)
    if step <= 0:
        return candles

    def snap(x: float) -> float:
        return round(x / step) * step

    out = []
    for c in candles:
        o, h, l, cl = snap(c.open), snap(c.high), snap(c.low), snap(c.close)
        hi = max(h, o, cl)
        lo = min(l, o, cl)
        out.append(
            Candle(
                timestamp=c.timestamp,
                open=o,
                high=hi,
                low=lo,
                close=cl,
                volume=c.volume,
            )
        )
    return out


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def side_norm(s: object) -> str:
    s = str(s or "").lower()
    if s in ("buy", "long"):
        return "buy"
    if s in ("sell", "short"):
        return "sell"
    return s


def hour_key(ts) -> str:
    return str(_ts(ts).floor("h"))


def trade_row(t) -> dict:
    return {
        "symbol": t.symbol,
        "side": side_norm(t.side.value if hasattr(t.side, "value") else t.side),
        "entry_time": str(t.entry_time),
        "entry_price": float(t.entry_price),
        "stop_loss": t.stop_loss,
        "take_profit": t.take_profit,
        "margin": float(t.margin),
        "rr_used": float(t.score),
        "commission": float(getattr(t, "commission", 0) or 0),
        "status": "open" if t.is_open else "closed",
        "exit_time": str(t.exit_time) if t.exit_time is not None else None,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason or None,
        "pnl": float(t.pnl or 0),
        "pattern": str(t.pattern or ""),
    }


def load_theory_trades(start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with eng.connect() as c:
        rows = c.execute(
            text(
                """
                select symbol, side, pattern, entry_price, stop_loss, take_profit,
                       margin, rr_used, commission, status, entry_time, exit_price,
                       exit_time, exit_reason, pnl
                from theory_trades
                where entry_time::timestamptz >= :s
                  and entry_time::timestamptz <  :e
                order by entry_time, symbol
                """
            ),
            {"s": start.to_pydatetime(), "e": end.to_pydatetime()},
        ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["side"] = side_norm(d.get("side"))
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif hasattr(v, "__float__") and not isinstance(v, (int, float, bool, type(None))):
                d[k] = float(v)
        out.append(d)
    return out


def filter_week(rows: list[dict]) -> list[dict]:
    return [r for r in rows if WEEK_START <= _ts(r["entry_time"]) < WEEK_END]


def summarize(rows: list[dict]) -> dict:
    closed = [r for r in rows if str(r.get("status")) == "closed"]
    opens = [r for r in rows if str(r.get("status")) == "open"]
    pnls = [float(r.get("pnl") or 0) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by_day: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for r in closed:
        d = str(_ts(r["entry_time"]).date())
        by_day[d]["n"] += 1
        by_day[d]["pnl"] += float(r.get("pnl") or 0)
        by_day[d]["wins"] += int(float(r.get("pnl") or 0) > 0)
    return {
        "total": len(rows),
        "open": len(opens),
        "closed": len(closed),
        "pnl": sum(pnls),
        "wr_pct": (len(wins) / len(closed) * 100 if closed else None),
        "avg_win": (sum(wins) / len(wins) if wins else None),
        "avg_loss": (sum(losses) / len(losses) if losses else None),
        "pf": (sum(wins) / abs(sum(losses)) if losses and sum(losses) else None),
        "exits": dict(Counter(r.get("exit_reason") or "?" for r in closed)),
        "by_day": dict(sorted(by_day.items())),
    }


def match_books(offline: list[dict], shadow: list[dict]) -> dict:
    used: set[int] = set()
    matched = []
    offline_only = []
    for o in offline:
        key = (o["symbol"], side_norm(o["side"]), hour_key(o["entry_time"]))
        mi = None
        for i, s in enumerate(shadow):
            if i in used:
                continue
            if (s["symbol"], side_norm(s["side"]), hour_key(s["entry_time"])) == key:
                mi = i
                break
        if mi is None:
            offline_only.append(o)
        else:
            used.add(mi)
            matched.append((o, shadow[mi]))
    shadow_only = [shadow[i] for i in range(len(shadow)) if i not in used]

    entry_deltas = []
    pnl_deltas = []
    same_sign = 0
    opp_sign = 0
    for o, s in matched:
        try:
            entry_deltas.append(float(o["entry_price"]) - float(s["entry_price"]))
        except Exception:
            pass
        if str(o.get("status")) == "closed" and str(s.get("status")) == "closed":
            d = float(o.get("pnl") or 0) - float(s.get("pnl") or 0)
            pnl_deltas.append(d)
            op = float(o.get("pnl") or 0)
            sp = float(s.get("pnl") or 0)
            if op != 0 and sp != 0:
                if (op > 0) == (sp > 0):
                    same_sign += 1
                else:
                    opp_sign += 1

    top = []
    for o, s in matched:
        if str(o.get("status")) != "closed" or str(s.get("status")) != "closed":
            continue
        d = float(o.get("pnl") or 0) - float(s.get("pnl") or 0)
        top.append(
            {
                "symbol": o["symbol"],
                "side": o["side"],
                "hour": hour_key(o["entry_time"]),
                "offline_pnl": float(o.get("pnl") or 0),
                "shadow_pnl": float(s.get("pnl") or 0),
                "delta": d,
                "offline_exit": o.get("exit_reason"),
                "shadow_exit": s.get("exit_reason"),
                "entry_delta": float(o["entry_price"]) - float(s["entry_price"]),
            }
        )
    top.sort(key=lambda x: abs(x["delta"]), reverse=True)

    return {
        "matched": len(matched),
        "offline_only": len(offline_only),
        "shadow_only": len(shadow_only),
        "match_rate_of_offline_pct": (len(matched) / len(offline) * 100 if offline else None),
        "match_rate_of_shadow_pct": (len(matched) / len(shadow) * 100 if shadow else None),
        "same_sign": same_sign,
        "opposite_sign": opp_sign,
        "mean_entry_delta": (sum(entry_deltas) / len(entry_deltas) if entry_deltas else None),
        "median_entry_delta": (
            float(pd.Series(entry_deltas).median()) if entry_deltas else None
        ),
        "mean_pnl_delta": (sum(pnl_deltas) / len(pnl_deltas) if pnl_deltas else None),
        "sum_pnl_delta_matched": (sum(pnl_deltas) if pnl_deltas else None),
        "top_divergences": top[:15],
        "offline_only_sample": [
            {
                "symbol": r["symbol"],
                "side": r["side"],
                "entry": r["entry_time"],
                "status": r["status"],
                "pnl": r.get("pnl"),
                "exit": r.get("exit_reason"),
            }
            for r in offline_only[:20]
        ],
        "shadow_only_sample": [
            {
                "symbol": r["symbol"],
                "side": r["side"],
                "entry": r["entry_time"],
                "status": r["status"],
                "pnl": r.get("pnl"),
                "exit": r.get("exit_reason"),
            }
            for r in shadow_only[:20]
        ],
    }


def run_offline(data: dict, master: str):
    from borex.alexg import AlexG8Strategy
    from borex.alexg.multi_market import MultiMarketContext, align_symbols_to_timeline
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine
    from borex.backtest.multi_portfolio import MultiMarketPortfolio

    strategy = AlexG8Strategy(min_rr=3.0, execution_interval="1h", peer_blend=0.0)
    q = strategy.ohlc_quantize_pips
    data = {sym: prequantize(bars, sym, q) for sym, bars in data.items()}
    strategy.ohlc_quantize_pips = 0.0

    cfg = BacktestConfig(
        initial_capital=CAPITAL,
        leverage=5000.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=3.0,
        rr_mode="dynamic",
        rr_factor=1.88,
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        risk_include_commission=True,
        winrate_min_trades=20,
        spread_pips=0.0,
        slippage_pips=0.0,
    )
    engine = MultiMarketEngine(strategy, cfg, max_positions=60)
    master_bars = data[master]
    min_bars = int(getattr(strategy, "min_bars", 120))
    ts_maps = align_symbols_to_timeline(master_bars, data)

    # Locate eval window on master timeline.
    start_i = None
    stop_i = len(master_bars)
    for i, c in enumerate(master_bars):
        ts = _ts(c.timestamp)
        if start_i is None and ts >= EVAL_START:
            start_i = i
        if ts >= EVAL_END:
            stop_i = i
            break
    if start_i is None:
        raise RuntimeError("No master bars inside eval window")

    # Match live --warmup-bars 10000 for AOI/HTF geometry before the epoch.
    warm_from = max(min_bars, start_i - 10_000)
    print(
        f"Warming strategy {warm_from}→{start_i - 1} ({start_i - warm_from} bars); "
        f"trading {start_i}→{stop_i - 1}",
        flush=True,
    )
    for mi in range(warm_from, start_i):
        ctx = MultiMarketContext.at_master_bar(
            mi,
            master_bars,
            data,
            ts_maps,
            strength_lookback=strategy.strength_lookback,
            min_currency_edge=strategy.min_currency_edge,
            min_confirming_pairs=strategy.min_confirming_pairs,
        )
        for symbol, index in ctx.indices.items():
            strategy.set_context(symbol, ctx)
            strategy.on_bar(index, data[symbol], None)

    portfolio = MultiMarketPortfolio(
        initial_capital=cfg.initial_capital,
        position_size_pct=cfg.position_size_pct,
        leverage=cfg.leverage,
        maintenance_margin_ratio=cfg.maintenance_margin_ratio,
        size_mode=cfg.size_mode,
        max_positions=60,
        commission_per_lot=cfg.commission_per_lot,
        commission_per_trade=cfg.commission_per_trade,
        min_commission_per_side=cfg.min_commission_per_side,
        lot_notional=cfg.lot_notional,
        risk_include_commission=cfg.risk_include_commission,
    )

    for mi in range(start_i, stop_i):
        ctx = MultiMarketContext.at_master_bar(
            mi,
            master_bars,
            data,
            ts_maps,
            strength_lookback=strategy.strength_lookback,
            min_currency_edge=strategy.min_currency_edge,
            min_confirming_pairs=strategy.min_confirming_pairs,
        )
        prices = {
            symbol: data[symbol][index].close for symbol, index in ctx.indices.items()
        }
        for symbol in list(portfolio.open_trades):
            index = ctx.indices.get(symbol)
            if index is not None:
                engine._check_exit(portfolio, symbol, index, data[symbol][index])

        candidates = []
        for symbol, index in ctx.indices.items():
            if index < min_bars or symbol in portfolio.open_trades:
                continue
            strategy.set_context(symbol, ctx)
            signal = strategy.on_bar(index, data[symbol], None)
            if signal is not None:
                candidates.append((symbol, signal, index))
        candidates.sort(key=lambda item: item[1].score, reverse=True)
        for symbol, signal, index in candidates:
            if not portfolio.can_open(symbol):
                break
            engine._open_signal(portfolio, symbol, signal, index, data[symbol])

        if mi % 24 == 0:
            print(
                f"  master {master_bars[mi].timestamp} opens={len(portfolio.open_trades)} "
                f"closed={len(portfolio.closed_trades)} cash={portfolio.cash:.2f}",
                flush=True,
            )

    prices = {sym: bars[-1].close for sym, bars in data.items() if bars}
    equity = portfolio.equity_at_prices(prices)
    trades = [trade_row(t) for t in portfolio.closed_trades] + [
        trade_row(t) for t in portfolio.open_trades.values()
    ]
    # Keep only entries inside the eval window (should already be).
    trades = [
        t
        for t in trades
        if EVAL_START <= _ts(t["entry_time"]) < EVAL_END
    ]
    return {
        "final_cash": portfolio.cash,
        "final_equity": equity,
        "open_n": len(portfolio.open_trades),
        "closed_n": len(portfolio.closed_trades),
        "trades": trades,
        "master_start": str(master_bars[start_i].timestamp),
        "master_end": str(master_bars[stop_i - 1].timestamp),
        "master_bars_traded": stop_i - start_i,
    }


def _load_cache_candles(symbol: str, interval: str, start, end):
    """Read MT5 parquet cache without the feed's coverage gate (can be stale)."""
    from borex.viewerMT5.mt5_feed import (
        _cache_path,
        _frame_to_candles,
        _slice_candles,
    )

    cache_file = _cache_path(symbol, interval)
    if not cache_file.is_file():
        return []
    try:
        df = pd.read_parquet(cache_file)
        # Some refreshes polluted the utc cache with :45 stamps. Live H1 is :00.
        candles = [
            c
            for c in _frame_to_candles(df)
            if pd.Timestamp(c.timestamp).minute == 0
            and pd.Timestamp(c.timestamp).second == 0
        ]
        return _slice_candles(candles, start, end)
    except Exception:
        return []


def _load_live_candles_from_db(start, end) -> dict[str, list]:
    """Recent MT5 bars persisted by the live service (works while MT5 IPC is busy)."""
    from borex.models.candle import Candle

    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    out: dict[str, list] = defaultdict(list)
    with eng.connect() as c:
        rows = c.execute(
            text(
                """
                select symbol, ts, open, high, low, close, volume
                from live_candles
                where interval = '1h'
                  and ts::timestamptz >= :s
                  and ts::timestamptz <  :e
                order by symbol, ts
                """
            ),
            {"s": start, "e": end},
        ).mappings().all()
    for r in rows:
        ts = pd.Timestamp(r["ts"])
        if ts.minute != 0 or ts.second != 0:
            continue
        out[r["symbol"]].append(
            Candle(
                timestamp=r["ts"],
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"] or 0),
            )
        )
    return dict(out)


def _merge_bars(base: list, extra: list) -> list:
    merged = {pd.Timestamp(c.timestamp).value: c for c in base}
    for c in extra:
        ts = pd.Timestamp(c.timestamp)
        if ts.minute != 0 or ts.second != 0:
            continue
        merged[ts.value] = c
    return [merged[k] for k in sorted(merged.keys())]


def main() -> int:
    from borex.alexg.multi_market import pick_master_symbol
    from borex.data.symbols import FOREX_PAIRS

    OUT.mkdir(parents=True, exist_ok=True)
    print("Loading theory shadow trades from DB…", flush=True)
    shadow_all = load_theory_trades(EPOCH_START, WEEK_END)
    shadow = filter_week(shadow_all)
    print(
        f"Shadow trades epoch={len(shadow_all)} week={len(shadow)}",
        flush=True,
    )

    load_start = (EVAL_START - timedelta(days=WARMUP_DAYS)).to_pydatetime()
    load_end = EVAL_END.to_pydatetime()
    print(f"Loading MT5 H1 {load_start.date()} → {load_end.date()}", flush=True)

    # Live owns MT5 IPC. Use on-the-hour cache + live_candles (same source as shadow).
    live_db = _load_live_candles_from_db(load_start, load_end)
    print(f"DB live_candles symbols={len(live_db)}", flush=True)

    symbols = sorted(set(FOREX_PAIRS) | set(live_db.keys()))
    raw: dict = {}
    for i, sym in enumerate(symbols, 1):
        bars = _load_cache_candles(sym, "1h", load_start, load_end)
        if sym in live_db:
            # Prefer live service bars for the overlapping recent window.
            bars = _merge_bars(bars, live_db[sym])
        if len(bars) >= 120 and any(
            EVAL_START <= _ts(c.timestamp) < EVAL_END for c in bars[-80:]
        ):
            raw[sym] = bars
        if i % 15 == 0 or i == len(symbols):
            print(f"  loaded {len(raw)}/{i}", flush=True)

    if not raw:
        raise RuntimeError("No symbol histories covering the eval window")

    master = pick_master_symbol(raw)
    last = raw[master][-1].timestamp
    print(
        f"Running offline alexg8 | pairs={len(raw)} master={master} last={last}",
        flush=True,
    )
    offline = run_offline(raw, master)

    offline_all = offline["trades"]
    offline_week = filter_week(offline_all)

    offline_sum = summarize(offline_week)
    shadow_sum = summarize(shadow)
    cmp = match_books(offline_week, shadow)
    cmp_all = match_books(offline_all, shadow_all)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "epoch_start": EPOCH_START.isoformat(),
            "week_start": WEEK_START.isoformat(),
            "week_end": WEEK_END.isoformat(),
            "warmup_days": WARMUP_DAYS,
            "offline_master_start": offline["master_start"],
            "offline_master_end": offline["master_end"],
            "master_bars_traded": offline["master_bars_traded"],
            "note": "Offline uses on-the-hour MT5 cache + live_candles (same bars as shadow).",
        },
        "params": {
            "strategy": "alexg8",
            "capital": CAPITAL,
            "leverage": 5000.0,
            "position_size_pct": 0.01,
            "rr_mode": "dynamic",
            "rr_factor": 1.88,
            "commission_per_lot": 7.0,
            "same_bar_exit": False,
            "max_positions": 60,
        },
        "offline_week": {
            "final_cash": offline["final_cash"],
            "final_equity": offline["final_equity"],
            "summary": offline_sum,
            "epoch_trade_count": len(offline_all),
        },
        "shadow_week": {
            "summary": shadow_sum,
            "epoch_trade_count": len(shadow_all),
        },
        "comparison_week": cmp,
        "comparison_epoch": {
            "matched": cmp_all["matched"],
            "offline_only": cmp_all["offline_only"],
            "shadow_only": cmp_all["shadow_only"],
            "match_rate_of_offline_pct": cmp_all["match_rate_of_offline_pct"],
            "match_rate_of_shadow_pct": cmp_all["match_rate_of_shadow_pct"],
        },
        "pnl_gap_offline_minus_shadow_week": offline_sum["pnl"] - shadow_sum["pnl"],
    }

    out_path = OUT / "report.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (OUT / "offline_trades.json").write_text(
        json.dumps(offline_week, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "shadow_trades.json").write_text(
        json.dumps(shadow, indent=2, default=str), encoding="utf-8"
    )

    print(json.dumps({
        "offline_week": offline_sum,
        "shadow_week": shadow_sum,
        "match_week": {
            k: cmp[k]
            for k in (
                "matched",
                "offline_only",
                "shadow_only",
                "match_rate_of_offline_pct",
                "match_rate_of_shadow_pct",
                "same_sign",
                "opposite_sign",
                "sum_pnl_delta_matched",
            )
        },
        "match_epoch": report["comparison_epoch"],
        "pnl_gap_offline_minus_shadow_week": report["pnl_gap_offline_minus_shadow_week"],
        "offline_equity": offline["final_equity"],
    }, indent=2))
    print(f"Wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
