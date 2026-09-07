#!/usr/bin/env python3
"""Replay theory shadow offline and measure parity with the live theory book.

Why this exists
---------------
Comparing offline MT5 backtests to theory shadow failed (~9% trade match) because
the hand-rolled offline runner used a *different warmup* (10k bars before eval)
than ShadowEngine at epoch init (~72 bars via ``_rebuild_strategy_only``).

This harness uses the **same ShadowEngine class** and the **same bar sources**
(MT5 on-the-hour cache + ``live_candles``) so we can iterate:

  Tier 0  ``epoch``       — cold-start at theory epoch, replay forward (golden path)
  Tier 1  ``checkpoint``  — restore ``theory_state`` blob, replay from cursor
  Tier 2  ``bars``        — OHLC diff only (cache vs live_candles), no strategy

Target: epoch replay should match ``theory_trades`` at >95% before we touch live MT5.
Checkpoint replay should be ~100% when bars haven't drifted since last save.

Usage (from borex_live, with .env DATABASE_URL set):

  .\\.venv311\\Scripts\\python.exe scripts/shadow_parity_replay.py epoch
  .\\.venv311\\Scripts\\python.exe scripts/shadow_parity_replay.py checkpoint
  .\\.venv311\\Scripts\\python.exe scripts/shadow_parity_replay.py bars
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT.parent / "borex-main"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MAIN))
load_dotenv(ROOT / ".env")

# First live theory attach after shadow shipped.
DEFAULT_EPOCH = pd.Timestamp("2026-08-14T17:00:00+00:00")
DEFAULT_END = pd.Timestamp.now(tz="UTC").floor("h") + pd.Timedelta(hours=1)
WARMUP_DAYS = 400
OUT = MAIN / "data" / "runs" / "shadow_parity"


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


def trade_row(t) -> dict[str, Any]:
    side = t.side.value if hasattr(t.side, "value") else t.side
    return {
        "symbol": t.symbol,
        "side": side_norm(side),
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


def db_trade_row(r: dict) -> dict[str, Any]:
    d = dict(r)
    d["side"] = side_norm(d.get("side"))
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif hasattr(v, "__float__") and not isinstance(v, (int, float, bool, type(None))):
            d[k] = float(v)
    return d


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
    return [db_trade_row(dict(r)) for r in rows]


def load_theory_state() -> dict[str, Any] | None:
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with eng.connect() as c:
        row = c.execute(
            text(
                """
                select strategy, config_hash, initial_capital, cash, equity,
                       last_master_ts, state_blob, updated_at
                from theory_state where id = 1
                """
            )
        ).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d["state_blob"] = bytes(d["state_blob"])
    if hasattr(d.get("updated_at"), "isoformat"):
        d["updated_at"] = d["updated_at"].isoformat()
    return d


def _load_cache_candles(symbol: str, interval: str, start, end):
    from borex.viewerMT5.mt5_feed import _cache_path, _frame_to_candles, _slice_candles

    cache_file = _cache_path(symbol, interval)
    if not cache_file.is_file():
        return []
    try:
        df = pd.read_parquet(cache_file)
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


def load_canonical_bars(
    epoch: pd.Timestamp,
    end: pd.Timestamp,
    warmup_days: int = WARMUP_DAYS,
) -> dict[str, list]:
    from borex.data.symbols import FOREX_PAIRS

    load_start = (epoch - timedelta(days=warmup_days)).to_pydatetime()
    load_end = end.to_pydatetime()
    live_db = _load_live_candles_from_db(load_start, load_end)
    symbols = sorted(set(FOREX_PAIRS) | set(live_db.keys()))
    raw: dict[str, list] = {}
    for sym in symbols:
        bars = _load_cache_candles(sym, "1h", load_start, load_end)
        if sym in live_db:
            bars = _merge_bars(bars, live_db[sym])
        if len(bars) >= 120:
            raw[sym] = bars
    return raw


def truncate_bars_at(
    candles_by_symbol: dict[str, list],
    cutoff: pd.Timestamp,
) -> dict[str, list]:
    out: dict[str, list] = {}
    for sym, bars in candles_by_symbol.items():
        out[sym] = [c for c in bars if _ts(c.timestamp) <= cutoff]
    return out


def match_books(replayed: list[dict], expected: list[dict]) -> dict[str, Any]:
    used: set[int] = set()
    matched = []
    replay_only = []
    for r in replayed:
        key = (r["symbol"], side_norm(r["side"]), hour_key(r["entry_time"]))
        mi = None
        for i, e in enumerate(expected):
            if i in used:
                continue
            if (e["symbol"], side_norm(e["side"]), hour_key(e["entry_time"])) == key:
                mi = i
                break
        if mi is None:
            replay_only.append(r)
        else:
            used.add(mi)
            matched.append((r, expected[mi]))
    expected_only = [expected[i] for i in range(len(expected)) if i not in used]

    entry_deltas = []
    pnl_deltas = []
    for r, e in matched:
        try:
            entry_deltas.append(float(r["entry_price"]) - float(e["entry_price"]))
        except Exception:
            pass
        if str(r.get("status")) == "closed" and str(e.get("status")) == "closed":
            pnl_deltas.append(float(r.get("pnl") or 0) - float(e.get("pnl") or 0))

    return {
        "matched": len(matched),
        "replay_only": len(replay_only),
        "expected_only": len(expected_only),
        "match_rate_replay_pct": (len(matched) / len(replayed) * 100 if replayed else None),
        "match_rate_expected_pct": (len(matched) / len(expected) * 100 if expected else None),
        "mean_entry_delta": (sum(entry_deltas) / len(entry_deltas) if entry_deltas else None),
        "mean_pnl_delta": (sum(pnl_deltas) / len(pnl_deltas) if pnl_deltas else None),
        "replay_only_sample": replay_only[:10],
        "expected_only_sample": expected_only[:10],
    }


def summarize(rows: list[dict]) -> dict[str, Any]:
    closed = [r for r in rows if str(r.get("status")) == "closed"]
    pnls = [float(r.get("pnl") or 0) for r in closed]
    wins = [p for p in pnls if p > 0]
    return {
        "total": len(rows),
        "closed": len(closed),
        "open": len(rows) - len(closed),
        "pnl": sum(pnls),
        "wr_pct": (len(wins) / len(closed) * 100 if closed else None),
    }


@dataclass
class _TheoryStateRow:
    strategy: str
    config_hash: str
    initial_capital: float
    cash: float
    equity: float
    last_master_ts: str
    state_blob: bytes


@dataclass
class InMemoryTheoryRepo:
    """Minimal repo for offline ShadowEngine replay (no DB writes)."""

    trades: list[Any] = field(default_factory=list)
    state: _TheoryStateRow | None = None
    seeded_state: _TheoryStateRow | None = None

    def get_theory_state(self) -> _TheoryStateRow | None:
        return self.state if self.state is not None else self.seeded_state

    def reset_theory(self) -> None:
        self.trades.clear()
        self.state = None

    def save_theory_state(
        self,
        *,
        strategy: str,
        config_hash: str,
        initial_capital: float,
        cash: float,
        equity: float,
        last_master_ts: str,
        state_blob: bytes,
    ) -> _TheoryStateRow:
        self.state = _TheoryStateRow(
            strategy=strategy,
            config_hash=config_hash,
            initial_capital=initial_capital,
            cash=cash,
            equity=equity,
            last_master_ts=last_master_ts,
            state_blob=state_blob,
        )
        return self.state

    def upsert_theory_trade(self, trade: Any) -> Any:
        key = (trade.symbol, str(trade.entry_time), str(trade.pattern or ""))
        for i, row in enumerate(self.trades):
            if (row.symbol, str(row.entry_time), str(row.pattern or "")) == key:
                self.trades[i] = trade
                return trade
        self.trades.append(trade)
        return trade


def load_latest_service_config() -> dict[str, Any] | None:
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with eng.connect() as c:
        row = c.execute(
            text(
                """
                select strategy, config_json
                from service_runs
                order by id desc
                limit 1
                """
            )
        ).mappings().first()
    return dict(row) if row else None


def build_live_config(
    capital: float,
    db_state: dict[str, Any] | None = None,
    *,
    strategy_override: str = "",
) -> Any:
    from borex_live.config import LiveServiceConfig

    run = load_latest_service_config()
    cfg = LiveServiceConfig.from_env()
    if run and isinstance(run.get("config_json"), dict):
        saved = run["config_json"]
        cfg.strategy = str(saved.get("strategy") or run.get("strategy") or "alexg8")
        cfg.leverage = float(saved.get("leverage") or cfg.leverage)
        cfg.rr_factor = float(saved.get("rr_factor") or cfg.rr_factor)
        cfg.min_rr = float(saved.get("min_rr") or cfg.min_rr)
        cfg.position_size_pct = float(saved.get("position_size_pct") or cfg.position_size_pct)
        cfg.max_positions = int(saved.get("max_positions") or cfg.max_positions)
        cfg.interval = str(saved.get("interval") or cfg.interval)
        cfg.master_yahoo = str(saved.get("master_yahoo") or cfg.master_yahoo)
        cfg.same_bar_exit = bool(saved.get("same_bar_exit", cfg.same_bar_exit))
        cfg.commission_per_lot = float(saved.get("commission_per_lot") or cfg.commission_per_lot)
        cfg.risk_include_commission = bool(
            saved.get("risk_include_commission", cfg.risk_include_commission)
        )
        cfg.winrate_min_trades = int(saved.get("winrate_min_trades") or cfg.winrate_min_trades)
        cfg.rr_mode = str(saved.get("rr_mode") or getattr(cfg, "rr_mode", "dynamic"))
        cfg.commission_at_entry = bool(
            saved.get("commission_at_entry", getattr(cfg, "commission_at_entry", False))
        )
        cfg.force_flat_friday = bool(
            saved.get("force_flat_friday", getattr(cfg, "force_flat_friday", False))
        )
        cfg.force_flat_daily = bool(
            saved.get("force_flat_daily", getattr(cfg, "force_flat_daily", False))
        )
        cfg.force_flat_utc_hour = int(
            saved.get("force_flat_utc_hour", getattr(cfg, "force_flat_utc_hour", 19))
        )
        cfg.force_flat_friday_from_hour = int(
            saved.get("force_flat_friday_from_hour", getattr(cfg, "force_flat_friday_from_hour", 19))
        )
    elif db_state:
        cfg.strategy = str(db_state.get("strategy") or "alexg8")
    else:
        cfg.strategy = "alexg8"
        cfg.leverage = 5000.0
        cfg.rr_factor = 1.88
        cfg.min_rr = 3.0
        cfg.risk_include_commission = True
        cfg.rr_mode = "dynamic"

    if db_state:
        cfg.strategy = str(db_state.get("strategy") or cfg.strategy)
    if strategy_override:
        cfg.strategy = strategy_override
    cfg.capital = capital
    cfg.warmup_bars = 10_000
    return cfg


def _make_shadow(cfg: Any) -> Any:
    from borex.backtest.engine import BacktestConfig
    from borex_live.engine.shadow_engine import ShadowEngine
    from borex_live.strategy_registry import create_strategy

    strategy, _ = create_strategy(
        cfg.strategy,
        min_rr=cfg.min_rr,
        second_signal=cfg.second_signal,
        execution_interval=cfg.interval,
    )
    bt_config = BacktestConfig(
        initial_capital=cfg.capital,
        leverage=cfg.leverage,
        position_size_pct=cfg.position_size_pct,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=cfg.min_rr,
        rr_mode=getattr(cfg, "rr_mode", "dynamic") or "dynamic",
        rr_factor=cfg.rr_factor,
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=float(cfg.commission_per_lot or 0.0),
        min_commission_per_side=float(cfg.min_commission_per_side or 0.04),
        lot_notional=float(cfg.lot_notional or 100_000.0),
        risk_include_commission=bool(cfg.risk_include_commission),
        winrate_min_trades=int(cfg.winrate_min_trades or 20),
        commission_at_entry=bool(getattr(cfg, "commission_at_entry", False)),
        force_flat_friday=bool(getattr(cfg, "force_flat_friday", False)),
        force_flat_daily=bool(getattr(cfg, "force_flat_daily", False)),
        force_flat_utc_hour=int(getattr(cfg, "force_flat_utc_hour", 19) or 19),
        force_flat_friday_from_hour=int(getattr(cfg, "force_flat_friday_from_hour", 19) or 19),
    )
    return ShadowEngine(strategy, cfg, bt_config)


def portfolio_trades(shadow: Any) -> list[dict]:
    rows = [trade_row(t) for t in shadow.portfolio.closed_trades]
    rows.extend(trade_row(t) for t in shadow.portfolio.open_trades.values())
    rows.sort(key=lambda r: (r["entry_time"], r["symbol"]))
    return rows


def replay_epoch(
    candles_full: dict[str, list],
    master: str,
    epoch: pd.Timestamp,
    cfg: Any,
) -> tuple[list[dict], dict[str, Any]]:
    from borex.alexg.multi_market import pick_master_symbol

    if master not in candles_full:
        master = pick_master_symbol(candles_full)

    candles_at_epoch = truncate_bars_at(candles_full, epoch)
    repo = InMemoryTheoryRepo()
    shadow = _make_shadow(cfg)
    start_info = shadow.start(repo, candles_at_epoch, master)

    master_bars = candles_full[master]
    cursor = shadow._master_index_for_ts(master_bars, shadow.last_master_ts)
    if cursor is None:
        raise RuntimeError(f"Epoch cursor {shadow.last_master_ts!r} missing from full timeline")
    processed = shadow.process_range(repo, candles_full, master, cursor + 1, len(master_bars))

    meta = {
        "start_mode": start_info.get("mode"),
        "start_processed": start_info.get("processed"),
        "epoch_cursor_ts": shadow.last_master_ts,
        "bars_replayed": processed,
        "final_cash": shadow.portfolio.cash,
        "final_equity": getattr(shadow, "_latest_equity", None),
    }
    return portfolio_trades(shadow), meta


def replay_checkpoint(
    candles_full: dict[str, list],
    master: str,
    db_state: dict[str, Any],
    cfg: Any,
) -> tuple[list[dict], dict[str, Any]]:
    from borex.alexg.multi_market import pick_master_symbol

    if master not in candles_full:
        master = pick_master_symbol(candles_full)

    repo = InMemoryTheoryRepo()
    repo.seeded_state = _TheoryStateRow(
        strategy=str(db_state["strategy"]),
        config_hash=str(db_state["config_hash"]),
        initial_capital=float(db_state["initial_capital"]),
        cash=float(db_state["cash"]),
        equity=float(db_state["equity"]),
        last_master_ts=str(db_state["last_master_ts"]),
        state_blob=bytes(db_state["state_blob"]),
    )
    shadow = _make_shadow(cfg)
    start_info = shadow.start(repo, candles_full, master)

    meta = {
        "start_mode": start_info.get("mode"),
        "start_processed": start_info.get("processed"),
        "checkpoint_ts": str(db_state["last_master_ts"]),
        "db_cash": float(db_state["cash"]),
        "replay_cash": shadow.portfolio.cash,
        "cash_delta": shadow.portfolio.cash - float(db_state["cash"]),
        "db_equity": float(db_state["equity"]),
    }
    return portfolio_trades(shadow), meta


def diff_bars(
    epoch: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    load_start = (epoch - timedelta(days=30)).to_pydatetime()
    load_end = end.to_pydatetime()
    live_db = _load_live_candles_from_db(load_start, load_end)

    ohlc_mismatches = 0
    cache_only = 0
    live_only = 0
    samples: list[dict] = []

    for sym in sorted(live_db.keys()):
        cache = _load_cache_candles(sym, "1h", load_start, load_end)
        cache_map = {_ts(c.timestamp).isoformat(): c for c in cache}
        live_map = {_ts(c.timestamp).isoformat(): c for c in live_db[sym]}
        keys = set(cache_map) | set(live_map)
        for k in sorted(keys):
            cc = cache_map.get(k)
            lc = live_map.get(k)
            if cc is None:
                live_only += 1
                continue
            if lc is None:
                cache_only += 1
                continue
            for field in ("open", "high", "low", "close"):
                if abs(float(getattr(cc, field)) - float(getattr(lc, field))) > 1e-9:
                    ohlc_mismatches += 1
                    if len(samples) < 20:
                        samples.append(
                            {
                                "symbol": sym,
                                "ts": k,
                                "field": field,
                                "cache": float(getattr(cc, field)),
                                "live": float(getattr(lc, field)),
                            }
                        )
                    break

    return {
        "symbols_with_live_candles": len(live_db),
        "ohlc_mismatches": ohlc_mismatches,
        "cache_only_bars": cache_only,
        "live_only_bars": live_only,
        "samples": samples,
    }


def export_fixture(out_dir: Path, epoch: pd.Timestamp, end: pd.Timestamp) -> Path:
    """Save theory checkpoint + live_candles for deterministic offline replay."""
    import pickle

    db_state = load_theory_state()
    if db_state is None:
        raise RuntimeError("No theory_state row to export")

    load_start = epoch.to_pydatetime()
    load_end = end.to_pydatetime()
    live_db = _load_live_candles_from_db(load_start, load_end)

    fixture_dir = out_dir / f"fixture_{epoch.strftime('%Y%m%d')}"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "epoch": epoch.isoformat(),
        "end": end.isoformat(),
        "config_hash": db_state["config_hash"],
        "last_master_ts": db_state["last_master_ts"],
        "initial_capital": float(db_state["initial_capital"]),
        "cash": float(db_state["cash"]),
        "equity": float(db_state["equity"]),
        "strategy": db_state["strategy"],
        "live_candle_symbols": len(live_db),
        "theory_trades": len(load_theory_trades(epoch, end)),
    }
    (fixture_dir / "manifest.json").write_text(
        json.dumps(meta, indent=2),
        encoding="utf-8",
    )
    (fixture_dir / "theory_state.pkl").write_bytes(bytes(db_state["state_blob"]))

    bars_json: dict[str, list] = {}
    for sym, bars in live_db.items():
        bars_json[sym] = [
            {
                "timestamp": str(c.timestamp),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in bars
        ]
    (fixture_dir / "live_candles.json").write_text(
        json.dumps(bars_json, indent=2),
        encoding="utf-8",
    )
    return fixture_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow parity replay harness")
    parser.add_argument(
        "mode",
        choices=("epoch", "checkpoint", "bars", "all", "export-fixture"),
        help=(
            "epoch=cold replay; checkpoint=restore blob; bars=OHLC diff; "
            "all=run all tiers; export-fixture=save golden bar+state snapshot"
        ),
    )
    parser.add_argument("--epoch", default=DEFAULT_EPOCH.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--capital", type=float, default=0.0, help="0 = read theory_state")
    parser.add_argument(
        "--strategy",
        default="",
        help="Override strategy (default: latest service_runs, else theory_state.strategy)",
    )
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL required", file=sys.stderr)
        return 1

    epoch = _ts(args.epoch)
    end = _ts(args.end)

    if args.mode == "export-fixture":
        path = export_fixture(args.out, epoch, end)
        print(f"Exported golden fixture to {path}")
        return 0

    modes = ("epoch", "checkpoint", "bars") if args.mode == "all" else (args.mode,)

    db_state = load_theory_state()
    capital = args.capital or float((db_state or {}).get("initial_capital") or 889.69)
    cfg = build_live_config(capital, db_state, strategy_override=args.strategy or "")

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "epoch": epoch.isoformat(),
        "end": end.isoformat(),
        "capital": capital,
        "strategy": cfg.strategy,
        "rr_mode": getattr(cfg, "rr_mode", None),
        "min_rr": cfg.min_rr,
        "config_hash_target": db_state.get("config_hash") if db_state else None,
    }

    expected = load_theory_trades(epoch, end)
    report["expected_theory"] = summarize(expected)

    candles_full: dict[str, list] | None = None
    master = cfg.master_yahoo

    if "bars" in modes:
        print("Tier 2: bar diff (cache vs live_candles)…", flush=True)
        report["bars"] = diff_bars(epoch, end)
        print(json.dumps(report["bars"], indent=2), flush=True)

    if "epoch" in modes or "checkpoint" in modes:
        print("Loading canonical bars…", flush=True)
        candles_full = load_canonical_bars(epoch, end)
        from borex.alexg.multi_market import pick_master_symbol

        master = pick_master_symbol(candles_full)
        report["bars_loaded"] = {
            "symbols": len(candles_full),
            "master": master,
            "master_bars": len(candles_full.get(master, [])),
        }
        print(
            f"  {len(candles_full)} symbols, master={master} "
            f"({len(candles_full.get(master, []))} bars)",
            flush=True,
        )

    if "epoch" in modes:
        print("Tier 0: epoch cold replay via ShadowEngine…", flush=True)
        replayed, meta = replay_epoch(candles_full or {}, master, epoch, cfg)
        cmp = match_books(replayed, expected)
        report["epoch_replay"] = {
            "meta": meta,
            "summary": summarize(replayed),
            "comparison": cmp,
        }
        print(
            f"  match={cmp['matched']}/{len(expected)} "
            f"({cmp['match_rate_expected_pct']:.1f}% of DB book)"
            if cmp["match_rate_expected_pct"] is not None
            else "  no expected trades",
            flush=True,
        )

    if "checkpoint" in modes:
        if db_state is None:
            print("No theory_state row; skipping checkpoint tier", flush=True)
        else:
            print("Tier 1: checkpoint restore + catch-up…", flush=True)
            replayed, meta = replay_checkpoint(candles_full or {}, master, db_state, cfg)
            cmp = match_books(replayed, expected)
            report["checkpoint_replay"] = {
                "meta": meta,
                "summary": summarize(replayed),
                "comparison": cmp,
            }
            print(
                f"  match={cmp['matched']}/{len(expected)} "
                f"({cmp['match_rate_expected_pct']:.1f}% of DB book)"
                if cmp["match_rate_expected_pct"] is not None
                else "  no expected trades",
                flush=True,
            )
            print(
                f"  cash delta vs checkpoint: {meta['cash_delta']:.4f}",
                flush=True,
            )

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"parity_{args.mode}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"

    def _json_default(o):
        if isinstance(o, (pd.Timestamp, datetime)):
            return o.isoformat()
        if isinstance(o, bytes):
            return f"<{len(o)} bytes>"
        raise TypeError(type(o))

    out_path.write_text(
        json.dumps(report, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
