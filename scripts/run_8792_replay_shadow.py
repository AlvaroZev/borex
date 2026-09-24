"""ReplayShadow: feed historical H1 through live ShadowEngine for 8792 Layer-A.

Compares:
  A Broker (saved)
  B Live Theory (saved)
  C MultiMarket offline (prior recon)
  D ReplayShadow (this harness — actual ShadowEngine.step_master_bar)

Does NOT call the broker, touch 8791, or tune strategy knobs.
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(LIVE / ".env")

BASE = ROOT / "data" / "runs" / "mirror_2d_8792"
RECON = BASE / "reconciliation"
OUT = RECON / "replay_shadow"
DUMP = BASE / "broker_theory_dump.json"
PRIOR_BT = BASE / "backtest.json"
MM_SHADOW = RECON / "offline_shadow_h1.json"
MM_CLOSEST = RECON / "offline_closest_h1_1m.json"
HTML = Path(r"c:\Users\azeva\OneDrive\Documentos\ReportHistory-53019882.html")

ENTRY_START = pd.Timestamp("2026-08-27 01:00:00", tz="UTC")
LIVE_END = pd.Timestamp("2026-08-28 23:00:00", tz="UTC")
WARMUP_DAYS = 400
CAPITAL = 1000.0

# CONFIRMED from 8792 recon knobs_audit
CONFIRMED = dict(
    strategy="alexg8",
    ghost_sl_mult=0.6,  # strategy default; geometry-confirmed
    leverage=5000.0,
    rr_mode="dynamic",
    true_sl_rr=2.0,
    rr_min=2.0,
    rr_max=6.0,
    force_flat_daily=True,
    force_flat_utc_hour=19,
    commission_per_lot=7.0,
)
# ASSUMED — not in dump; kept for fill accounting; signal path ignores these
ASSUMED = dict(
    rr_factor=1.0,
    force_flat_friday=False,
    risk_include_commission=True,
    commission_at_entry=False,
    position_size_pct=0.01,
    max_positions=60,
)


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _side(x: str) -> str:
    s = str(x or "").lower()
    if s in ("buy", "long"):
        return "long"
    return "short"


def _pair(x: str) -> str:
    s = str(x or "").replace("=X", "").strip().upper()
    return f"{s}=X"


def _hour(ts) -> str:
    if not ts:
        return ""
    return _ts(ts).strftime("%Y-%m-%d %H:00")


# ---------------------------------------------------------------------------
# Architecture map (written before any replay)
# ---------------------------------------------------------------------------

ARCHITECTURE_MAP = {
    "generated_at": None,  # filled at runtime
    "question": (
        "Can ReplayShadow (live ShadowEngine path) reproduce the saved 8792 Theory tape?"
    ),
    "live_8792_path": {
        "cli": "borex_live/mirrorservice.py",
        "service": "borex_mirror.service.MirrorService",
        "engine": "borex_mirror.engine.MirrorEngine.process_master_bar",
        "theory": "borex_live.engine.shadow_engine.ShadowEngine.step_master_bar",
        "order_per_bar": [
            "align timeline / MultiMarketContext.at_master_bar",
            "exits: MultiMarketEngine._check_exit (flatten before SL/TP)",
            "scan: strategy.on_bar for each symbol in ctx.indices",
            "sort candidates by signal.score desc",
            "opens: book_is_full / can_open → _open_signal",
            "MirrorExecutor.mirror_open/close (broker side; skipped in ReplayShadow)",
        ],
        "epoch_init_live_tip": (
            "ShadowEngine.start → _rebuild_strategy_only(last max(min_bars,72) bars, "
            "NO portfolio fills) → cursor at tip → future bars only"
        ),
        "bulk_init": (
            "MirrorService.process_bulk → reset → fresh MirrorEngine → "
            "last_master_ts=start_i-1 → process_range(start_i…) WITH fills; "
            "NO _rebuild_strategy_only"
        ),
    },
    "multimarket_batch_path": {
        "entry": "borex.backtest.multi_market_engine.MultiMarketEngine.run",
        "order_per_bar": [
            "exits (_check_exit_path, optional 1m)",
            "scan on_bar",
            "score-sort opens",
            "optional same_bar_exit",
        ],
        "begin": "max(min_bars, start_master_i); 8792 recon used entry_i-72 prime WITH fills",
    },
    "shared_substrate": [
        "AlexG8Strategy / GhostSLEntryMixin",
        "MultiMarketEngine._check_exit / _open_signal",
        "MultiMarketPortfolio occupancy/cash gates fills only",
    ],
    "divergence_hypotheses_to_test": {
        "A1_clock": "master index / hour recognition / forming-bar drop",
        "A2_warmup": "tip-epoch rebuild vs bulk cold vs MultiMarket fill-prime",
        "A3_pattern": "on_bar geometry (should match if same bars+state)",
        "A4_ghost_init": "rebuild-without-fills vs fill-prime vs cold",
        "A5_occupancy": "can_open / book_is_full after scan (order of symbol dict)",
        "A6_cash": "signals ignore cash; fills/sizing use cash (cascade only)",
        "A7_flatten": "exits before scan in BOTH paths — verify",
        "A8_lifecycle": "end_of_data / checkpoint / broker reconcile (mirror-only)",
        "A9_data": "spreads: live Theory BacktestConfig has NO spread_pips; MultiMarket recon used Raw",
        "A10_unknown": "residual",
    },
    "signal_depends_on_cash": False,
    "signal_depends_on_occupancy": False,
    "occupancy_gates": "fills only, after scan+score-sort",
    "flatten_vs_scan_order": "flatten/exits BEFORE scan (both Shadow and MultiMarket)",
    "replay_hook": (
        "InstrumentedShadowEngine.step_master_bar + bulk-style process_range "
        "(no broker, InMemoryTheoryRepo)"
    ),
    "knobs": {"confirmed": CONFIRMED, "assumed": ASSUMED},
}


@dataclass
class BarTrace:
    master_ts: str
    master_i: int
    cash_before: float
    equity_before: float
    open_pairs_before: list[str]
    n_open_before: int
    intents: list[dict] = field(default_factory=list)
    blocked_fills: list[dict] = field(default_factory=list)
    opens: list[dict] = field(default_factory=list)
    closes: list[dict] = field(default_factory=list)
    ghosts_created: list[dict] = field(default_factory=list)
    ghosts_filled: list[dict] = field(default_factory=list)
    ghosts_expired: list[dict] = field(default_factory=list)


@dataclass
class InMemoryTheoryRepo:
    trades: list[Any] = field(default_factory=list)
    state: Any = None

    def get_theory_state(self):
        return self.state

    def reset_theory(self) -> None:
        self.trades.clear()
        self.state = None

    def save_theory_state(self, **kwargs):
        self.state = kwargs
        return self.state

    def upsert_theory_trade(self, trade: Any) -> Any:
        key = (trade.symbol, str(trade.entry_time), str(trade.pattern or ""))
        for i, row in enumerate(self.trades):
            if (row.symbol, str(row.entry_time), str(row.pattern or "")) == key:
                self.trades[i] = trade
                return trade
        self.trades.append(trade)
        return trade


def write_architecture_map() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = dict(ARCHITECTURE_MAP)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    path = OUT / "architecture_map.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("wrote", path, flush=True)
    return path


def load_h1_from_cache(symbols: list[str], start: datetime, end: datetime) -> dict:
    from borex.viewerMT5.mt5_feed import _cache_path, _frame_to_candles, _slice_candles
    from borex.models.candle import Candle

    out: dict[str, list] = {}
    for sym in symbols:
        cache_file = _cache_path(sym, "1h")
        if not cache_file.is_file():
            print(f"  cache miss {sym}", flush=True)
            continue
        try:
            df = pd.read_parquet(cache_file)
            candles = [
                c
                for c in _frame_to_candles(df)
                if _ts(c.timestamp).minute == 0 and _ts(c.timestamp).second == 0
            ]
            sliced = _slice_candles(candles, start, end)
            if len(sliced) >= 120:
                out[sym] = sliced
        except Exception as exc:
            print(f"  cache fail {sym}: {exc}", flush=True)
    return out


def make_shadow():
    from borex.backtest.engine import BacktestConfig
    from borex_live.config import LiveServiceConfig
    from borex_live.strategy_registry import create_strategy

    InstrumentedShadowEngine = globals()["InstrumentedShadowEngine"]

    live_cfg = LiveServiceConfig(
        strategy=CONFIRMED["strategy"],
        capital=CAPITAL,
        leverage=CONFIRMED["leverage"],
        min_rr=CONFIRMED["true_sl_rr"],
        rr_factor=ASSUMED["rr_factor"],
        rr_mode=CONFIRMED["rr_mode"],
        rr_min=CONFIRMED["rr_min"],
        rr_max=CONFIRMED["rr_max"],
        position_size_pct=ASSUMED["position_size_pct"],
        max_positions=ASSUMED["max_positions"],
        interval="1h",
        master_yahoo="EURUSD=X",
        same_bar_exit=False,
        commission_per_lot=CONFIRMED["commission_per_lot"],
        risk_include_commission=ASSUMED["risk_include_commission"],
        commission_at_entry=ASSUMED["commission_at_entry"],
        force_flat_friday=ASSUMED["force_flat_friday"],
        force_flat_daily=CONFIRMED["force_flat_daily"],
        force_flat_utc_hour=CONFIRMED["force_flat_utc_hour"],
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
    )
    strategy, _ = create_strategy(
        live_cfg.strategy,
        min_rr=live_cfg.min_rr,
        second_signal="off",
        execution_interval=live_cfg.interval,
    )
    # Preserve live default ghost_sl_mult=0.6 (do not override).
    assert abs(float(getattr(strategy, "ghost_sl_mult", 1.0)) - 0.6) < 1e-9, (
        f"unexpected ghost_sl_mult={getattr(strategy, 'ghost_sl_mult', None)}"
    )
    bt = BacktestConfig(
        initial_capital=CAPITAL,
        leverage=CONFIRMED["leverage"],
        position_size_pct=ASSUMED["position_size_pct"],
        size_mode="margin",
        true_sl=True,
        true_sl_rr=CONFIRMED["true_sl_rr"],
        rr_mode=CONFIRMED["rr_mode"],
        rr_factor=ASSUMED["rr_factor"],
        rr_min=CONFIRMED["rr_min"],
        rr_max=CONFIRMED["rr_max"],
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=CONFIRMED["commission_per_lot"],
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=ASSUMED["risk_include_commission"],
        commission_at_entry=ASSUMED["commission_at_entry"],
        force_flat_friday=ASSUMED["force_flat_friday"],
        force_flat_daily=CONFIRMED["force_flat_daily"],
        force_flat_utc_hour=CONFIRMED["force_flat_utc_hour"],
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        # LIVE Theory: MirrorEngine does NOT set spreads → 0. Preserve that.
        spread_pips=0.0,
        spread_pips_by_symbol={},
        slippage_pips=0.0,
        intra_hour_sl=False,
    )
    return InstrumentedShadowEngine(strategy, live_cfg, bt)


# Inline instrumented engine (same module to keep one script runnable)
def _install_instrumented():
    """Define InstrumentedShadowEngine in this module namespace."""
    from borex.alexg.multi_market import MultiMarketContext, align_symbols_to_timeline
    from borex_live.engine.shadow_engine import ShadowEngine

    class InstrumentedShadowEngine(ShadowEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.traces: list[BarTrace] = []
            self.trade_fills: list[dict] = []
            self._pending_before: dict = {}

        def step_master_bar(
            self,
            master_index: int,
            candles_by_symbol,
            master_symbol: str,
        ) -> None:
            master = candles_by_symbol[master_symbol]
            master_ts = str(master[master_index].timestamp)
            prices_pre = {
                s: candles_by_symbol[s][-1].close
                for s, bars in candles_by_symbol.items()
                if bars
            }
            cash_before = float(self.portfolio.cash)
            equity_before = float(self.portfolio.equity_at_prices(prices_pre))
            open_before = sorted(self.portfolio.open_trades.keys())
            closed_before = len(self.portfolio.closed_trades)
            pending_before = {
                k: (v.action, v.planned_entry, v.stop_loss, v.created_index, v.expires_index)
                for k, v in getattr(self.strategy, "_pending", {}).items()
            }

            ts_maps = align_symbols_to_timeline(master, candles_by_symbol)
            ctx = MultiMarketContext.at_master_bar(
                master_index,
                master,
                candles_by_symbol,
                ts_maps,
                strength_lookback=self.strategy.strength_lookback,
                min_currency_edge=self.strategy.min_currency_edge,
                min_confirming_pairs=self.strategy.min_confirming_pairs,
            )
            prices = {
                symbol: candles_by_symbol[symbol][index].close
                for symbol, index in ctx.indices.items()
            }

            # --- exits first (preserve live order) ---
            for symbol in list(self.portfolio.open_trades):
                index = ctx.indices.get(symbol)
                if index is not None:
                    self.engine._check_exit(
                        self.portfolio,
                        symbol,
                        index,
                        candles_by_symbol[symbol][index],
                    )

            closes = []
            for t in self.portfolio.closed_trades[closed_before:]:
                closes.append(
                    {
                        "pair": t.symbol,
                        "direction": _side(t.side.value if hasattr(t.side, "value") else t.side),
                        "exit_time": str(t.exit_time),
                        "exit_price": float(t.exit_price or 0),
                        "exit_reason": t.exit_reason,
                        "pnl": float(t.pnl or 0),
                    }
                )

            # --- scan ---
            candidates = []
            intents = []
            min_bars = int(getattr(self.strategy, "min_bars", 120))
            for symbol, index in ctx.indices.items():
                if index < min_bars:
                    continue
                pending_pre = symbol in getattr(self.strategy, "_pending", {})
                self.strategy.set_context(symbol, ctx)
                signal = self.strategy.on_bar(index, candles_by_symbol[symbol], None)
                pending_post = getattr(self.strategy, "_pending", {}).get(symbol)
                if pending_post is not None and not pending_pre:
                    intents.append(
                        {
                            "kind": "ghost_created",
                            "pair": symbol,
                            "direction": _side(
                                pending_post.action.value
                                if hasattr(pending_post.action, "value")
                                else pending_post.action
                            ),
                            "ghost_entry": float(pending_post.planned_entry),
                            "ghost_sl": float(pending_post.stop_loss),
                            "bar_index": index,
                            "master_ts": master_ts,
                        }
                    )
                if signal is not None:
                    side = signal.action.value if hasattr(signal.action, "value") else str(signal.action)
                    intents.append(
                        {
                            "kind": "fill_intent",
                            "pair": symbol,
                            "direction": _side(side),
                            "score": float(signal.score or 0),
                            "pattern": str(signal.pattern or "")[:120],
                            "bar_index": index,
                            "master_ts": master_ts,
                        }
                    )
                    candidates.append((symbol, signal, index))

            candidates.sort(key=lambda item: item[1].score, reverse=True)
            blocked = []
            opens = []
            for symbol, signal, index in candidates:
                if self.portfolio.book_is_full():
                    blocked.append(
                        {
                            "pair": symbol,
                            "reason": "book_full",
                            "n_open": len(self.portfolio.open_trades),
                            "master_ts": master_ts,
                        }
                    )
                    break
                if not self.portfolio.can_open(symbol):
                    blocked.append(
                        {
                            "pair": symbol,
                            "reason": "pair_occupied_or_cash",
                            "occupied": symbol in self.portfolio.open_trades,
                            "cash": float(self.portfolio.cash),
                            "master_ts": master_ts,
                        }
                    )
                    continue
                before_n = len(self.portfolio.open_trades)
                self.engine._open_signal(
                    self.portfolio,
                    symbol,
                    signal,
                    index,
                    candles_by_symbol[symbol],
                )
                if len(self.portfolio.open_trades) > before_n or any(
                    t.symbol == symbol
                    for t in self.portfolio.closed_trades[closed_before:]
                ):
                    # opened (may have same-bar closed already — rare)
                    trade = self.portfolio.open_trades.get(symbol)
                    if trade is None:
                        # closed same bar — find latest closed for symbol
                        for t in reversed(self.portfolio.closed_trades):
                            if t.symbol == symbol:
                                trade = t
                                break
                    if trade is not None:
                        row = {
                            "pair": trade.symbol,
                            "direction": _side(
                                trade.side.value if hasattr(trade.side, "value") else trade.side
                            ),
                            "entry_time": str(trade.entry_time),
                            "entry_price": float(trade.entry_price),
                            "sl": float(trade.stop_loss or 0),
                            "tp": float(trade.take_profit or 0),
                            "margin": float(trade.margin or 0),
                            "rr": float(trade.score or 0),
                            "pattern": str(trade.pattern or ""),
                            "master_ts": master_ts,
                            "cash_after": float(self.portfolio.cash),
                        }
                        opens.append(row)
                        self.trade_fills.append(row)

            # ghost expiries / cancels
            pending_after = {
                k: (v.action, v.planned_entry, v.stop_loss, v.created_index, v.expires_index)
                for k, v in getattr(self.strategy, "_pending", {}).items()
            }
            ghosts_created = [i for i in intents if i["kind"] == "ghost_created"]
            ghosts_filled = [i for i in intents if i["kind"] == "fill_intent"]
            ghosts_expired = []
            for k, meta in pending_before.items():
                if k not in pending_after and k not in {o["pair"] for o in opens}:
                    ghosts_expired.append(
                        {"pair": k, "master_ts": master_ts, "prior": str(meta)}
                    )

            self.traces.append(
                BarTrace(
                    master_ts=master_ts,
                    master_i=master_index,
                    cash_before=cash_before,
                    equity_before=equity_before,
                    open_pairs_before=open_before,
                    n_open_before=len(open_before),
                    intents=intents,
                    blocked_fills=blocked,
                    opens=opens,
                    closes=closes,
                    ghosts_created=ghosts_created,
                    ghosts_filled=ghosts_filled,
                    ghosts_expired=ghosts_expired,
                )
            )
            self.last_master_ts = master_ts
            self._last_indices = dict(ctx.indices)
            self._latest_equity = self.portfolio.equity_at_prices(prices)

    globals()["InstrumentedShadowEngine"] = InstrumentedShadowEngine
    # also export for make_shadow import path
    import types

    mod = types.ModuleType("instrumented_shadow")
    mod.InstrumentedShadowEngine = InstrumentedShadowEngine
    sys.modules["instrumented_shadow"] = mod
    return InstrumentedShadowEngine


def trade_row_from_portfolio(t) -> dict:
    side = t.side.value if hasattr(t.side, "value") else t.side
    return {
        "symbol": t.symbol,
        "side": _side(side),
        "entry_time": str(t.entry_time),
        "exit_time": str(t.exit_time) if t.exit_time is not None else None,
        "entry_price": float(t.entry_price),
        "exit_price": float(t.exit_price or 0) if t.exit_price is not None else None,
        "stop_loss": float(t.stop_loss or 0),
        "take_profit": float(t.take_profit or 0),
        "margin": float(t.margin or 0),
        "rr_used": float(t.score or 0),
        "pnl": float(t.pnl or 0),
        "exit_reason": t.exit_reason,
        "pattern": str(t.pattern or ""),
        "status": "open" if t.is_open else "closed",
    }


def _fnum(x, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def normalize_rows(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        et = r.get("entry_time") or r.get("open_time") or r.get("signal_time")
        if source == "broker":
            entry_px = _fnum(r.get("open"))
            exit_px = _fnum(r.get("close"))
            pnl = _fnum(r.get("net"), _fnum(r.get("profit")))
        else:
            entry_px = _fnum(r.get("entry_price") or r.get("entry"))
            exit_px = _fnum(r.get("exit_price") or r.get("exit"))
            pnl = _fnum(r.get("pnl") or r.get("net"))
        out.append(
            {
                "source": source,
                "pair": _pair(r.get("symbol") or r.get("pair") or ""),
                "direction": _side(r.get("side") or r.get("direction") or ""),
                "entry_hour": _hour(et),
                "entry_time": str(et or ""),
                "entry_price": entry_px,
                "exit_time": str(r.get("exit_time") or r.get("close_time") or ""),
                "exit_price": exit_px,
                "exit_reason": r.get("exit_reason"),
                "pnl": pnl,
                "pattern": str(r.get("pattern") or r.get("comment") or ""),
            }
        )
    return out


def match_key(r: dict) -> tuple:
    return (r["pair"], r["direction"], r["entry_hour"])


def match_sets(ref: list[dict], cand: list[dict]) -> dict:
    bucket = defaultdict(list)
    for r in cand:
        bucket[match_key(r)].append(r)
    matched = []
    missing = []
    for r in ref:
        k = match_key(r)
        if bucket.get(k):
            c = bucket[k].pop(0)
            timing_delta_h = 0.0
            matched.append({"ref": r, "cand": c, "timing_delta_h": timing_delta_h})
        else:
            # ±1h tolerance
            found = None
            for sh in (-1, 1):
                if not r["entry_hour"]:
                    break
                t = pd.Timestamp(r["entry_hour"]) + pd.Timedelta(hours=sh)
                k2 = (r["pair"], r["direction"], t.strftime("%Y-%m-%d %H:00"))
                if bucket.get(k2):
                    found = bucket[k2].pop(0)
                    matched.append({"ref": r, "cand": found, "timing_delta_h": float(sh)})
                    break
            if found is None:
                missing.append(r)
    extra = [x for xs in bucket.values() for x in xs]
    timing = [m["timing_delta_h"] for m in matched]
    dir_mismatch = sum(
        1 for m in matched if m["ref"]["direction"] != m["cand"]["direction"]
    )
    n_ref = len(ref)
    n_cand = len(cand)
    n_m = len(matched)
    return {
        "n_ref": n_ref,
        "n_cand": n_cand,
        "matched": n_m,
        "missing": len(missing),
        "extra": len(extra),
        "recall": round(100 * n_m / n_ref, 2) if n_ref else None,
        "precision": round(100 * n_m / n_cand, 2) if n_cand else None,
        "overlap_vs_ref_pct": round(100 * n_m / n_ref, 2) if n_ref else None,
        "direction_mismatch_pct": round(100 * dir_mismatch / max(n_m, 1), 2),
        "median_timing_delta_h": float(pd.Series(timing).median()) if timing else None,
        "p95_timing_delta_h": float(pd.Series(timing).quantile(0.95)) if timing else None,
        "missing_rows": missing,
        "extra_rows": extra,
        "matched_rows": matched,
    }


def first_divergences(theory: list[dict], replay: list[dict]) -> dict:
    """Per-pair first chronological divergence between Theory fills and Replay fills."""
    th_by = defaultdict(list)
    rp_by = defaultdict(list)
    for r in theory:
        th_by[r["pair"]].append(r)
    for r in replay:
        rp_by[r["pair"]].append(r)
    for p in th_by:
        th_by[p].sort(key=lambda x: x["entry_hour"])
    for p in rp_by:
        rp_by[p].sort(key=lambda x: x["entry_hour"])

    pairs = sorted(set(th_by) | set(rp_by))
    rows = []
    class_counts = Counter()
    for pair in pairs:
        th = th_by.get(pair, [])
        rp = rp_by.get(pair, [])
        # walk merged hours
        i = j = 0
        first = None
        while i < len(th) or j < len(rp):
            th_h = th[i]["entry_hour"] if i < len(th) else None
            rp_h = rp[j]["entry_hour"] if j < len(rp) else None
            if th_h is not None and (rp_h is None or th_h < rp_h):
                first = {
                    "pair": pair,
                    "hour": th_h,
                    "kind": "theory_only",
                    "theory": th[i],
                    "replay": None,
                }
                break
            if rp_h is not None and (th_h is None or rp_h < th_h):
                first = {
                    "pair": pair,
                    "hour": rp_h,
                    "kind": "replay_only",
                    "theory": None,
                    "replay": rp[j],
                }
                break
            # same hour
            if th[i]["direction"] != rp[j]["direction"]:
                first = {
                    "pair": pair,
                    "hour": th_h,
                    "kind": "direction_mismatch",
                    "theory": th[i],
                    "replay": rp[j],
                }
                break
            # aligned at this step
            i += 1
            j += 1
        if first is None:
            class_counts["zero_divergence"] += 1
            rows.append({"pair": pair, "class": "zero_divergence", "first": None})
        else:
            # classify cause tentatively
            cause = "A10_unknown"
            if first["kind"] == "theory_only":
                cause = "A4_ghost_init_or_A5_occupancy_or_A3_pattern"
            elif first["kind"] == "replay_only":
                cause = "A4_ghost_init_or_A2_warmup"
            elif first["kind"] == "direction_mismatch":
                cause = "A3_pattern"
            class_counts["early_divergence"] += 1
            class_counts[first["kind"]] += 1
            rows.append(
                {
                    "pair": pair,
                    "class": "early_divergence",
                    "cause_hypothesis": cause,
                    "first": {
                        "hour": first["hour"],
                        "kind": first["kind"],
                        "theory_dir": None
                        if first["theory"] is None
                        else first["theory"]["direction"],
                        "replay_dir": None
                        if first["replay"] is None
                        else first["replay"]["direction"],
                        "theory_entry": None
                        if first["theory"] is None
                        else first["theory"]["entry_price"],
                        "replay_entry": None
                        if first["replay"] is None
                        else first["replay"]["entry_price"],
                    },
                }
            )
    return {
        "n_pairs": len(pairs),
        "zero_divergence_pairs": class_counts["zero_divergence"],
        "early_divergence_pairs": class_counts["early_divergence"],
        "kinds": dict(class_counts),
        "rows": rows,
    }


def classify_fill_cause(missing: dict, traces: list[BarTrace]) -> str:
    """Best-effort cause for a Theory trade missing from ReplayShadow."""
    hour = missing.get("entry_hour")
    pair = missing.get("pair")
    for tr in traces:
        if _hour(tr.master_ts) != hour:
            continue
        for b in tr.blocked_fills:
            if b["pair"] == pair:
                return "A5_occupancy"
        for g in tr.ghosts_created:
            if g["pair"] == pair:
                return "A4_ghost_created_but_not_filled"
        # intent fill present?
        for i in tr.intents:
            if i.get("pair") == pair and i.get("kind") == "fill_intent":
                return "A5_occupancy_or_open_failed"
        return "A3_pattern_or_A4_no_ghost"
    return "A1_clock_or_A2_warmup_bar_not_traced"


def run_mode(
    mode: str,
    candles: dict,
    master: str,
    entry_i: int,
    end_i: int,
) -> dict:
    _install_instrumented()
    shadow = make_shadow()
    repo = InMemoryTheoryRepo()
    t0 = time.perf_counter()

    if mode == "bulk":
        # MirrorService.process_bulk semantics: no rebuild; walk from entry with fills
        shadow.portfolio = shadow._new_portfolio()
        shadow.last_master_ts = (
            str(candles[master][entry_i - 1].timestamp) if entry_i > 0 else ""
        )
        start_i = entry_i
        meta_init = {"mode": "bulk", "rebuild": False, "start_i": start_i}
    elif mode == "ghost_prime":
        # Live tip-epoch style: strategy-only rebuild on the SAME candle arrays
        # (indices must stay valid), then trade from entry_i with empty portfolio.
        wait = int(getattr(shadow.strategy, "sl_wait_max_bars", 72) or 72)
        rebuild_start = max(int(getattr(shadow.strategy, "min_bars", 120)), entry_i - wait)
        shadow.portfolio = shadow._new_portfolio()
        from borex.alexg.multi_market import MultiMarketContext, align_symbols_to_timeline

        ts_maps = align_symbols_to_timeline(candles[master], candles)
        for master_i in range(rebuild_start, entry_i):
            ctx = MultiMarketContext.at_master_bar(
                master_i,
                candles[master],
                candles,
                ts_maps,
                strength_lookback=shadow.strategy.strength_lookback,
                min_currency_edge=shadow.strategy.min_currency_edge,
                min_confirming_pairs=shadow.strategy.min_confirming_pairs,
            )
            for symbol, index in ctx.indices.items():
                shadow.strategy.set_context(symbol, ctx)
                shadow.strategy.on_bar(index, candles[symbol], None)
        shadow.last_master_ts = str(candles[master][entry_i - 1].timestamp)
        n_pending = len(getattr(shadow.strategy, "_pending", {}))
        start_i = entry_i
        meta_init = {
            "mode": "ghost_prime",
            "rebuild": True,
            "rebuild_start": rebuild_start,
            "rebuild_bars": entry_i - rebuild_start,
            "pending_ghosts_after_rebuild": n_pending,
            "start_i": start_i,
            "last_master_ts": shadow.last_master_ts,
        }
    elif mode == "fill_prime":
        # MultiMarket-like: walk last 72 bars WITH fills before window
        wait = int(getattr(shadow.strategy, "sl_wait_max_bars", 72) or 72)
        prime_i = max(int(getattr(shadow.strategy, "min_bars", 120)), entry_i - wait)
        shadow.portfolio = shadow._new_portfolio()
        shadow.last_master_ts = (
            str(candles[master][prime_i - 1].timestamp) if prime_i > 0 else ""
        )
        shadow.process_range(repo, candles, master, prime_i, entry_i)
        # clear traces before window for cleaner Layer-A window analysis
        window_traces_start = len(shadow.traces)
        start_i = entry_i
        meta_init = {
            "mode": "fill_prime",
            "prime_i": prime_i,
            "pre_window_fills": len(
                [t for t in shadow.portfolio.closed_trades]
            )
            + len(shadow.portfolio.open_trades),
            "traces_during_prime": window_traces_start,
            "start_i": start_i,
            "cash_at_window": float(shadow.portfolio.cash),
        }
    else:
        raise ValueError(mode)

    # Drop traces accumulated during fill_prime before window (keep fills)
    if mode == "fill_prime":
        shadow.traces = []
        shadow.trade_fills = [
            f
            for f in shadow.trade_fills
            if _ts(f["entry_time"]) >= ENTRY_START
        ]

    processed = shadow.process_range(repo, candles, master, start_i, end_i + 1)
    elapsed = time.perf_counter() - t0

    trades = [
        trade_row_from_portfolio(t)
        for t in shadow.portfolio.closed_trades
        if _ts(t.entry_time) >= ENTRY_START and _ts(t.entry_time) <= LIVE_END
    ]
    trades += [
        trade_row_from_portfolio(t)
        for t in shadow.portfolio.open_trades.values()
        if _ts(t.entry_time) >= ENTRY_START
    ]
    trades.sort(key=lambda r: (r["entry_time"], r["symbol"]))

    return {
        "mode": mode,
        "meta_init": meta_init,
        "processed_bars": processed,
        "elapsed_sec": elapsed,
        "n_trades": len(trades),
        "net_pnl": sum(float(t["pnl"] or 0) for t in trades if t.get("status") == "closed"),
        "final_cash": float(shadow.portfolio.cash),
        "n_traces": len(shadow.traces),
        "blocked_fill_events": sum(len(t.blocked_fills) for t in shadow.traces),
        "ghost_created_events": sum(len(t.ghosts_created) for t in shadow.traces),
        "trades": trades,
        "traces": [asdict(t) for t in shadow.traces],
    }


def four_way_classes(
    broker: list[dict],
    theory: list[dict],
    mm: list[dict],
    replay: list[dict],
) -> dict:
    def keys(rows):
        return set(match_key(r) for r in rows)

    B, T, M, R = keys(broker), keys(theory), keys(mm), keys(replay)
    return {
        "common_all": len(B & T & M & R),
        "broker_theory_only": len((B & T) - M - R),
        "theory_replay_only": len((T & R) - B - M),
        "broker_replay_only": len((B & R) - T - M),
        "multimarket_theory": len(M & T),
        "theory_missing_from_multimarket": len(T - M),
        "replay_missing_from_theory": len(R - T),
        "replay_extra_vs_theory": len(R - T),
        "theory_missing_from_replay": len(T - R),
        "sizes": {"broker": len(B), "theory": len(T), "mm": len(M), "replay": len(R)},
    }


def decision_tree(best_mode: str, cmp_theory: dict, cmp_mm: dict) -> dict:
    overlap = float(cmp_theory.get("overlap_vs_ref_pct") or 0)
    mm_overlap = float(cmp_mm.get("overlap_vs_ref_pct") or 0)

    if overlap >= 90:
        case = 1
        label = "ReplayShadow ≈ Live Theory"
        next_action = (
            "MultiMarketEngine signal/state is the primary Layer-A problem. "
            "Prefer: use ShadowEngine as canonical signal generator + separate "
            "execution replay layer (option 2). Do not rewrite everything."
        )
        go_optimize = False
        arch = 2
    elif overlap >= 70:
        case = 1
        label = "ReplayShadow mostly matches Theory (materially better than MultiMarket)"
        next_action = (
            "Treat remaining gaps via first-divergence fixes, then adopt ShadowEngine "
            "as signal generator for research."
        )
        go_optimize = False
        arch = 2
    elif overlap <= mm_overlap + 10 and overlap < 50:
        case = 2
        label = "ReplayShadow still diverges materially from Live Theory"
        next_action = (
            "Discrepancy is in replayable live path inputs/state reconstruction. "
            "Investigate first divergence; do not optimize strategy."
        )
        go_optimize = False
        arch = None
    else:
        case = 2
        label = "ReplayShadow diverges; better or worse than MultiMarket needs inspection"
        next_action = "Investigate first divergence in ReplayShadow vs Theory."
        go_optimize = False
        arch = None

    # Case 3 would require Replay≈Theory but neither≈Broker — checked by caller
    return {
        "case": case,
        "label": label,
        "best_mode": best_mode,
        "theory_overlap_pct": overlap,
        "multimarket_overlap_pct": mm_overlap,
        "next_action": next_action,
        "go_strategy_optimization": go_optimize,
        "preferred_architecture_option": arch,
    }


def architecture_options_rank(case: int) -> list[dict]:
    opts = [
        {
            "id": 1,
            "name": "Fix MultiMarketEngine to match live signal/state",
            "fidelity": "high if successful",
            "cost": "medium-high",
            "determinism": "high",
            "sweeps": "excellent (already batch)",
            "live_state": "if fixed, yes",
            "signal_exec_split": "weak today",
            "maintenance": "medium",
        },
        {
            "id": 2,
            "name": "ShadowEngine as signal generator + deterministic exec layer",
            "fidelity": "highest for Layer A (same code as live)",
            "cost": "low-medium",
            "determinism": "high",
            "sweeps": "good if tape frozen then exec-replay",
            "live_state": "native",
            "signal_exec_split": "excellent",
            "maintenance": "low (reuse live)",
        },
        {
            "id": 3,
            "name": "Separate event-driven broker emulator",
            "fidelity": "high potential",
            "cost": "high",
            "determinism": "high",
            "sweeps": "good",
            "live_state": "must reimplement",
            "signal_exec_split": "excellent",
            "maintenance": "high",
        },
        {
            "id": 4,
            "name": "Move to 1m/tick replay",
            "fidelity": "high for Layer B only after A fixed",
            "cost": "high",
            "determinism": "medium",
            "sweeps": "expensive",
            "live_state": "no",
            "signal_exec_split": "exec only",
            "maintenance": "high",
        },
        {
            "id": 5,
            "name": "MT5-native historical testing",
            "fidelity": "broker-close for fills; weak for alexg8 ghost state",
            "cost": "medium",
            "determinism": "low-medium",
            "sweeps": "poor",
            "live_state": "no",
            "signal_exec_split": "poor",
            "maintenance": "medium",
        },
    ]
    # Preferred ranking for our evidence
    if case == 1:
        order = [2, 1, 3, 4, 5]
    else:
        order = [2, 1, 3, 5, 4]
    by_id = {o["id"]: o for o in opts}
    ranked = []
    for i, oid in enumerate(order, 1):
        o = dict(by_id[oid])
        o["rank"] = i
        ranked.append(o)
    return ranked


def main() -> int:
    write_architecture_map()

    prior = json.loads(PRIOR_BT.read_text(encoding="utf-8"))
    symbols = sorted((prior.get("spread_pips_by_symbol") or {}).keys())
    dump = json.loads(DUMP.read_text(encoding="utf-8"))
    theory_raw = dump.get("theory_closed") or []

    from compare_8792_books import parse_html

    html = parse_html(HTML)
    broker_raw = html["closed"]

    mm_raw = []
    if MM_SHADOW.exists():
        mm_raw = json.loads(MM_SHADOW.read_text(encoding="utf-8")).get("trades") or []

    print(f"Loading H1 cache for {len(symbols)} symbols…", flush=True)
    h1_start = (ENTRY_START - timedelta(days=WARMUP_DAYS)).to_pydatetime()
    h1_end = (LIVE_END + timedelta(hours=2)).to_pydatetime()
    candles = load_h1_from_cache(symbols, h1_start, h1_end)
    print(f"  loaded {len(candles)} pairs", flush=True)
    if len(candles) < 30:
        raise SystemExit("insufficient H1 cache")

    from borex.alexg.multi_market import pick_master_symbol

    master = pick_master_symbol(candles)
    master_bars = candles[master]
    entry_i = next(
        i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= ENTRY_START
    )
    end_i = next(
        (
            i
            for i in range(len(master_bars) - 1, -1, -1)
            if _ts(master_bars[i].timestamp) <= LIVE_END
        ),
        len(master_bars) - 1,
    )
    print(
        f"master={master} entry_i={entry_i} ({master_bars[entry_i].timestamp}) "
        f"end_i={end_i} ({master_bars[end_i].timestamp})",
        flush=True,
    )

    mode_results = {}
    for mode in ("bulk", "ghost_prime", "fill_prime"):
        cached = OUT / f"replay_{mode}.json"
        traces_path = OUT / f"traces_{mode}.json"
        if cached.exists() and traces_path.exists():
            print(f"\n=== ReplayShadow mode={mode} (cached) ===", flush=True)
            mode_results[mode] = json.loads(cached.read_text(encoding="utf-8"))
            mode_results[mode]["traces"] = json.loads(
                traces_path.read_text(encoding="utf-8")
            )
            print(
                f"  trades={mode_results[mode]['n_trades']} "
                f"net={mode_results[mode]['net_pnl']:.2f}",
                flush=True,
            )
            continue
        print(f"\n=== ReplayShadow mode={mode} ===", flush=True)
        mode_results[mode] = run_mode(mode, candles, master, entry_i, end_i)
        path = OUT / f"replay_{mode}.json"
        # traces can be large — store separately
        traces = mode_results[mode].pop("traces")
        path.write_text(
            json.dumps(mode_results[mode], indent=2, default=str), encoding="utf-8"
        )
        traces_path.write_text(
            json.dumps(traces, indent=2, default=str), encoding="utf-8"
        )
        mode_results[mode]["traces"] = traces
        print(
            f"  trades={mode_results[mode]['n_trades']} "
            f"net={mode_results[mode]['net_pnl']:.2f} "
            f"blocked={mode_results[mode]['blocked_fill_events']} "
            f"ghosts={mode_results[mode]['ghost_created_events']}",
            flush=True,
        )

    theory = normalize_rows(theory_raw, "theory")
    # filter theory to window
    theory = [
        t
        for t in theory
        if t["entry_hour"] and ENTRY_START <= _ts(t["entry_hour"]) <= LIVE_END
    ]
    broker = normalize_rows(broker_raw, "broker")
    mm = normalize_rows(mm_raw, "multimarket")

    comparisons = {}
    first_div = {}
    for mode, res in mode_results.items():
        replay = normalize_rows(res["trades"], f"replay_{mode}")
        cmp_t = match_sets(theory, replay)
        cmp_b = match_sets(broker, replay)
        cmp_mm = match_sets(theory, mm) if mode == "bulk" else None
        traces_objs = [
            BarTrace(
                master_ts=tr["master_ts"],
                master_i=tr["master_i"],
                cash_before=tr["cash_before"],
                equity_before=tr["equity_before"],
                open_pairs_before=tr["open_pairs_before"],
                n_open_before=tr["n_open_before"],
                intents=tr["intents"],
                blocked_fills=tr["blocked_fills"],
                opens=tr["opens"],
                closes=tr["closes"],
                ghosts_created=tr["ghosts_created"],
                ghosts_filled=tr["ghosts_filled"],
                ghosts_expired=tr["ghosts_expired"],
            )
            for tr in res["traces"]
        ]
        cause_counts = Counter()
        for row in cmp_t["missing_rows"]:
            c = classify_fill_cause(row, traces_objs)
            row["cause"] = c
            cause_counts[c] += 1

        fd = first_divergences(theory, replay)
        first_div[mode] = fd
        comparisons[mode] = {
            "vs_theory": {
                k: v
                for k, v in cmp_t.items()
                if k not in ("missing_rows", "extra_rows", "matched_rows")
            },
            "vs_theory_missing_sample": cmp_t["missing_rows"][:20],
            "vs_theory_extra_sample": cmp_t["extra_rows"][:20],
            "missing_cause_counts": dict(cause_counts),
            "vs_broker": {
                k: v
                for k, v in cmp_b.items()
                if k not in ("missing_rows", "extra_rows", "matched_rows")
            },
            "four_way": four_way_classes(broker, theory, mm, replay),
        }
        if cmp_mm is not None:
            comparisons[mode]["multimarket_vs_theory"] = {
                k: v
                for k, v in cmp_mm.items()
                if k not in ("missing_rows", "extra_rows", "matched_rows")
            }

        # persist matched detail
        (OUT / f"match_theory_{mode}.json").write_text(
            json.dumps(
                {
                    "summary": comparisons[mode]["vs_theory"],
                    "missing": cmp_t["missing_rows"],
                    "extra": cmp_t["extra_rows"],
                    "matched_n": cmp_t["matched"],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    # pick best mode by Theory recall
    best_mode = max(
        mode_results.keys(),
        key=lambda m: float(comparisons[m]["vs_theory"].get("recall") or 0),
    )
    mm_vs_theory = comparisons["bulk"].get("multimarket_vs_theory") or {
        "overlap_vs_ref_pct": 0
    }
    verdict = decision_tree(
        best_mode,
        comparisons[best_mode]["vs_theory"],
        mm_vs_theory,
    )

    # Case 3 check
    best_vs_broker = float(comparisons[best_mode]["vs_broker"].get("overlap_vs_ref_pct") or 0)
    if (
        float(comparisons[best_mode]["vs_theory"].get("overlap_vs_ref_pct") or 0) >= 90
        and best_vs_broker < 70
    ):
        verdict["case"] = 3
        verdict["label"] = (
            "ReplayShadow matches Theory but neither matches Broker well"
        )
        verdict["next_action"] = (
            "Signal layer validated; investigate broker execution/data/accounting (B/C/D)."
        )

    # Execution isolation ONLY if Layer A passes (case 1 with >=90)
    exec_isolation = None
    if verdict["case"] == 1 and float(verdict["theory_overlap_pct"]) >= 90:
        exec_isolation = {
            "status": "eligible",
            "note": (
                "Layer A passed threshold; execution isolation (H1 vs H1+1m on frozen "
                "ReplayShadow tape) should be run as a follow-up script — not in this pass "
                "if time-boxed. Flag for next step."
            ),
        }
    else:
        exec_isolation = {
            "status": "skipped",
            "reason": "Layer A did not pass Theory↔ReplayShadow ≥90% overlap",
        }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"entry_start": str(ENTRY_START), "live_end": str(LIVE_END)},
        "knobs_confirmed": CONFIRMED,
        "knobs_assumed": ASSUMED,
        "assumed_note": (
            "Accounting flags assumed; signal generation does not read them. "
            "Do not treat as facts."
        ),
        "master": master,
        "h1_pairs": len(candles),
        "modes": {
            m: {
                k: v
                for k, v in res.items()
                if k not in ("trades", "traces")
            }
            for m, res in mode_results.items()
        },
        "comparisons": comparisons,
        "first_divergences": {
            m: {k: v for k, v in fd.items() if k != "rows"}
            for m, fd in first_div.items()
        },
        "first_divergence_samples": {
            m: [r for r in fd["rows"] if r["class"] == "early_divergence"][:15]
            for m, fd in first_div.items()
        },
        "verdict": verdict,
        "architecture_ranking": architecture_options_rank(verdict["case"]),
        "execution_isolation": exec_isolation,
        "answers": {
            "1_replay_reproduces_theory": float(verdict["theory_overlap_pct"]) >= 90,
            "1_overlap_pct": verdict["theory_overlap_pct"],
            "1_best_mode": best_mode,
            "2_first_divergence": first_div[best_mode],
            "3_multimarket_materially_different": (
                abs(
                    float(verdict["theory_overlap_pct"])
                    - float(verdict["multimarket_overlap_pct"])
                )
                > 15
                or float(verdict["multimarket_overlap_pct"]) < 50
            ),
            "4_h1_1m_exec_isolation": exec_isolation,
            "5_canonical_instrument": verdict.get("preferred_architecture_option"),
            "6_ready_for_strategy_optimization": verdict["go_strategy_optimization"],
        },
        "policy": {
            "system_a": (
                "MultiMarket parameter sweeps are NOT validated for live transfer "
                "until Layer A passes."
            ),
            "system_b": "Do not assume System-A findings transfer to funded accounts.",
            "system_c": (
                "Older sims may be signal-quality research only — label explicitly."
            ),
        },
    }

    # Persist first-divergence full rows
    for m, fd in first_div.items():
        (OUT / f"first_divergence_{m}.json").write_text(
            json.dumps(fd, indent=2, default=str), encoding="utf-8"
        )

    (OUT / "layer_a_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )

    # Human summary
    lines = [
        "REPLAYSHADOW LAYER-A REPORT",
        f"generated: {report['generated_at']}",
        f"best_mode: {best_mode}",
        "",
        "CONFIRMED knobs: " + json.dumps(CONFIRMED),
        "ASSUMED knobs: " + json.dumps(ASSUMED),
        "",
        "Theory ↔ ReplayShadow:",
    ]
    for m in mode_results:
        vt = comparisons[m]["vs_theory"]
        lines.append(
            f"  {m}: recall={vt['recall']}% precision={vt['precision']}% "
            f"matched={vt['matched']} missing={vt['missing']} extra={vt['extra']}"
        )
    lines.append(
        f"  MultiMarket↔Theory: {mm_vs_theory.get('recall')}% recall "
        f"matched={mm_vs_theory.get('matched')}"
    )
    lines += [
        "",
        f"VERDICT Case {verdict['case']}: {verdict['label']}",
        f"next: {verdict['next_action']}",
        f"strategy optimization: {'GO' if verdict['go_strategy_optimization'] else 'NO-GO'}",
        "",
        "Answers:",
        f"  1. reproduces Theory? {report['answers']['1_replay_reproduces_theory']} "
        f"({report['answers']['1_overlap_pct']}% via {best_mode})",
        f"  3. MultiMarket materially different? "
        f"{report['answers']['3_multimarket_materially_different']}",
        f"  5. canonical arch option: {report['answers']['5_canonical_instrument']}",
        f"  6. ready to optimize? {report['answers']['6_ready_for_strategy_optimization']}",
        f"  4. exec isolation: {exec_isolation['status']}",
    ]
    # first divergence summary
    fd = first_div[best_mode]
    lines += [
        "",
        f"First divergence ({best_mode}): "
        f"zero={fd['zero_divergence_pairs']} early={fd['early_divergence_pairs']} "
        f"/ {fd['n_pairs']} pairs",
    ]
    for r in report["first_divergence_samples"][best_mode][:8]:
        fr = r.get("first") or {}
        lines.append(
            f"  {r['pair']}: {fr.get('kind')} @ {fr.get('hour')} "
            f"cause~{r.get('cause_hypothesis')}"
        )

    text = "\n".join(lines)
    (OUT / "SUMMARY.txt").write_text(text + "\n", encoding="utf-8")
    print("\n" + text, flush=True)
    print("wrote", OUT / "layer_a_report.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
