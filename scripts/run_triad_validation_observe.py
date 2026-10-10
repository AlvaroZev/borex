#!/usr/bin/env python3
"""2.5h Live ↔ Mirror ↔ Frozen-backtest fidelity observer (N1 Thursday, no param search).

Assumes:
  - Live  :8791 magic 88001 already running frozen N1 knobs
  - Mirror :8792 magic 88002 started separately on same open MT5
  - Protected EURDKK tickets never closed/modified
"""

from __future__ import annotations

import csv
import json
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LIVE_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")
MAIN_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main")
sys.path[:0] = [str(MAIN_ROOT), str(LIVE_ROOT)]

OBSERVE_HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 2.5
POLL_SEC = int(sys.argv[2]) if len(sys.argv) > 2 else 120
RUN = Path(sys.argv[3]) if len(sys.argv) > 3 else None
if RUN is None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    RUN = MAIN_ROOT / "data" / "runs" / "research" / f"live_mirror_backtest_validation_{ts}"
RUN.mkdir(parents=True, exist_ok=True)
(SNAP := RUN / "snapshots").mkdir(exist_ok=True)

PROTECTED_EURDKK = {
    1890205005,
    1891279302,
    1893184489,
    1917854332,
    1940979693,
}
LIVE_MAGIC = 88001
MIRROR_MAGIC = 88002


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def http_json(url: str, timeout: float = 30.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def safe_http(url: str) -> dict[str, Any]:
    try:
        return {"ok": True, "data": http_json(url)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def mt5_snapshot() -> dict[str, Any]:
    import MetaTrader5 as mt5

    if not mt5.initialize(timeout=30_000):
        return {"ok": False, "error": str(mt5.last_error())}
    try:
        acc = mt5.account_info()
        positions = []
        for p in mt5.positions_get() or []:
            positions.append(
                {
                    "ticket": int(p.ticket),
                    "symbol": str(p.symbol),
                    "magic": int(p.magic),
                    "volume": float(p.volume),
                    "profit": float(p.profit),
                    "price_open": float(p.price_open),
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "comment": str(p.comment),
                    "type": int(p.type),
                }
            )
        # deals since experiment start stored externally; here last 24h
        from_ts = int((utc_now() - timedelta(hours=36)).timestamp())
        to_ts = int(utc_now().timestamp()) + 60
        deals = []
        for d in mt5.history_deals_get(from_ts, to_ts) or []:
            deals.append(
                {
                    "ticket": int(d.ticket),
                    "order": int(d.order),
                    "symbol": str(d.symbol),
                    "magic": int(d.magic),
                    "volume": float(d.volume),
                    "price": float(d.price),
                    "profit": float(d.profit),
                    "commission": float(d.commission),
                    "swap": float(d.swap),
                    "entry": int(d.entry),
                    "time": int(d.time),
                    "comment": str(d.comment),
                }
            )
        return {
            "ok": True,
            "login": int(acc.login) if acc else None,
            "balance": float(acc.balance) if acc else None,
            "equity": float(acc.equity) if acc else None,
            "leverage": int(acc.leverage) if acc else None,
            "positions": positions,
            "deals_36h": deals,
            "protected_present": [
                p for p in positions if int(p["ticket"]) in PROTECTED_EURDKK
            ],
            "protected_touched": False,
        }
    finally:
        mt5.shutdown()


def pnl_by_magic(deals: list[dict], magic: int, since_unix: int) -> dict[str, float]:
    rows = [d for d in deals if int(d.get("magic") or 0) == magic and int(d.get("time") or 0) >= since_unix]
    profit = sum(float(d.get("profit") or 0) for d in rows)
    commission = sum(float(d.get("commission") or 0) for d in rows)
    swap = sum(float(d.get("swap") or 0) for d in rows)
    return {
        "n_deals": len(rows),
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "net": profit + commission + swap,
    }


def summarize_dash(label: str, dash: dict[str, Any] | None, err: str | None = None) -> dict[str, Any]:
    if dash is None:
        return {"label": label, "ok": False, "error": err}
    open_t = dash.get("open_trades") or dash.get("open") or []
    closed = dash.get("closed_trades") or dash.get("closed") or []
    ghosts = dash.get("pending_ghosts") or []
    theory_open = dash.get("theory_open") or dash.get("theory_trades_open") or []
    theory_closed = dash.get("theory_closed") or dash.get("theory_trades_closed") or []
    return {
        "label": label,
        "ok": True,
        "open_n": len(open_t),
        "closed_n": len(closed),
        "pending_ghosts_n": len(ghosts),
        "theory_open_n": len(theory_open) if isinstance(theory_open, list) else theory_open,
        "theory_closed_n": len(theory_closed) if isinstance(theory_closed, list) else theory_closed,
        "open_symbols": [t.get("symbol") for t in open_t] if isinstance(open_t, list) else [],
        "ghost_symbols": [g.get("symbol") for g in ghosts][:80] if isinstance(ghosts, list) else [],
    }


def run_frozen_window_backtest(t0: datetime, t1: datetime) -> dict[str, Any]:
    """Frozen N1 MultiMarketEngine over [t0,t1] with ~500 H1 warmup tip alignment."""
    try:
        from borex.alexg import AlexG8Strategy
        from borex.alexg.multi_market import default_forex_universe
        from borex.backtest.engine import BacktestConfig
        from borex.backtest.multi_market_engine import MultiMarketEngine
        from borex.data.mt5_time import measure_mt5_server_offset_seconds, mt5_unix_to_utc
        from borex.gate2.thursday_entry_gate import filter_block_thursday_entries
        from borex.models.candle import Candle
        from borex_mirror.engine import _ThursdayEntryGate
        import MetaTrader5 as mt5
        import pandas as pd
    except Exception as exc:
        return {"ok": False, "error": f"import: {exc!r}"}

    def rates_to_candles(rates, offset: int) -> list:
        out = []
        for r in rates:
            out.append(
                Candle(
                    timestamp=pd.Timestamp(mt5_unix_to_utc(int(r["time"]), offset)),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["tick_volume"]),
                )
            )
        return out

    if not mt5.initialize(timeout=60_000):
        return {"ok": False, "error": str(mt5.last_error())}
    try:
        offset = int(measure_mt5_server_offset_seconds(mt5) or 0)
        symbols = default_forex_universe()
        # Prefer Live universe size via status if available
        try:
            st = http_json("http://127.0.0.1:8791/api/status")
            if st.get("symbols"):
                symbols = list(st["symbols"])
        except Exception:
            pass
        h1_count = 600  # warmup + window (~2-3h needs little; keep structure)
        h1: dict[str, list] = {}
        spreads: dict[str, float] = {}
        now = pd.Timestamp.now(tz="UTC")
        for sym in symbols:
            mt5_sym = sym.replace("=X", "")
            info = mt5.symbol_info(mt5_sym)
            if info is None:
                mt5.symbol_select(mt5_sym, True)
                info = mt5.symbol_info(mt5_sym)
            if info is None:
                continue
            mt5.symbol_select(mt5_sym, True)
            rates = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_H1, 0, h1_count)
            if rates is None or len(rates) < 120:
                continue
            bars = rates_to_candles(rates, offset)
            if bars and pd.Timestamp(bars[-1].timestamp).tz_convert("UTC").floor("h") == now.floor("h"):
                bars = bars[:-1]
            if len(bars) < 120:
                continue
            h1[sym] = bars
            point = float(info.point or 0.0)
            dig = int(info.digits or 5)
            pip = point * 10 if dig in (3, 5) else (point if dig in (2, 4) else point * 10)
            spr_pts = float(info.spread or 0.0)
            spreads[sym] = (spr_pts * point / pip) if pip > 0 else 0.0
    finally:
        mt5.shutdown()

    if "EURUSD=X" not in h1:
        return {"ok": False, "error": "no EURUSD"}

    strategy = _ThursdayEntryGate(
        AlexG8Strategy(
            min_rr=2.0,
            execution_interval="1h",
            peer_blend=0.0,
            ghost_sl_mult=4.0,
        )
    )
    cfg = BacktestConfig(
        initial_capital=1000.0,
        leverage=500.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=5.0,
        commission_per_lot=7.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_daily=True,
        force_flat_friday=False,
    )
    # Optional spreads if config supports it
    if hasattr(cfg, "symbol_spreads"):
        try:
            cfg.symbol_spreads = spreads  # type: ignore[attr-defined]
        except Exception:
            pass
    engine = MultiMarketEngine(strategy, cfg)
    # Restrict reported opens to observation window via post-filter
    try:
        result = engine.run(h1, timeframe="1h", master_symbol="EURUSD=X")
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "traceback": traceback.format_exc()}

    t0p = pd.Timestamp(t0)
    if t0p.tzinfo is None:
        t0p = t0p.tz_localize("UTC")
    t1p = pd.Timestamp(t1)
    if t1p.tzinfo is None:
        t1p = t1p.tz_localize("UTC")

    trades = []
    if hasattr(result, "trades") and result.trades is not None:
        trades = list(result.trades)
    elif hasattr(result, "closed_trades") and result.closed_trades is not None:
        trades = list(result.closed_trades)
    elif hasattr(result, "portfolio") and hasattr(result.portfolio, "closed_trades"):
        trades = list(result.portfolio.closed_trades)

    window_opens = []
    window_closes = []
    for tr in trades:
        et = getattr(tr, "entry_time", None) or (tr.get("entry_time") if isinstance(tr, dict) else None)
        xt = getattr(tr, "exit_time", None) or (tr.get("exit_time") if isinstance(tr, dict) else None)
        sym = getattr(tr, "symbol", None) or (tr.get("symbol") if isinstance(tr, dict) else None)
        side = getattr(tr, "side", None) or (tr.get("side") if isinstance(tr, dict) else None)
        pnl = float(getattr(tr, "pnl", None) or (tr.get("pnl") if isinstance(tr, dict) else 0) or 0)
        entry_price = float(
            getattr(tr, "entry_price", None) or (tr.get("entry_price") if isinstance(tr, dict) else 0) or 0
        )
        if et is not None:
            ets = pd.Timestamp(et)
            if ets.tzinfo is None:
                ets = ets.tz_localize("UTC")
            else:
                ets = ets.tz_convert("UTC")
            if t0p <= ets <= t1p:
                window_opens.append(
                    {
                        "symbol": sym,
                        "side": str(side),
                        "entry_time": str(ets),
                        "entry_price": entry_price,
                        "pnl": pnl,
                    }
                )
        if xt is not None:
            xts = pd.Timestamp(xt)
            if xts.tzinfo is None:
                xts = xts.tz_localize("UTC")
            else:
                xts = xts.tz_convert("UTC")
            if t0p <= xts <= t1p:
                window_closes.append(
                    {
                        "symbol": sym,
                        "exit_time": str(xts),
                        "pnl": pnl,
                    }
                )

    # Pending tip
    pend = []
    strat = strategy
    inner = getattr(strat, "_inner", strat)
    pending = getattr(inner, "_pending", {}) or {}
    for sym, p in pending.items():
        pend.append(
            {
                "symbol": sym,
                "action": p.action.value if hasattr(p.action, "value") else str(p.action),
                "planned_entry": p.planned_entry,
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
            }
        )

    return {
        "ok": True,
        "offset_seconds": offset,
        "pairs": len(h1),
        "window_start": str(t0p),
        "window_end": str(t1p),
        "window_opens_n": len(window_opens),
        "window_closes_n": len(window_closes),
        "window_opens": window_opens,
        "window_closes": window_closes,
        "window_pnl_sum": sum(x["pnl"] for x in window_closes),
        "pending_tip_n": len(pend),
        "pending_tip": sorted(pend, key=lambda r: r["symbol"]),
    }


def write_progress(meta: dict[str, Any]) -> None:
    write_json(RUN / "PROGRESS.json", meta)


def main() -> int:
    t_start = utc_now()
    t_end = t_start + timedelta(hours=OBSERVE_HOURS)
    since_unix = int(t_start.timestamp())

    manifest = {
        "run_id": RUN.name,
        "experiment": "live_mirror_backtest_validation",
        "observe_hours": OBSERVE_HOURS,
        "poll_seconds": POLL_SEC,
        "started_utc": t_start.isoformat(),
        "ends_utc": t_end.isoformat(),
        "live_port": 8791,
        "mirror_port": 8792,
        "live_magic": LIVE_MAGIC,
        "mirror_magic": MIRROR_MAGIC,
        "frozen_knobs": {
            "strategy": "alexg8",
            "ghost_sl_mult": 4.0,
            "peer_blend": 0.0,
            "leverage": 500,
            "rr_mode": "dynamic",
            "rr_factor": 1.0,
            "rr_min": 2.0,
            "rr_max": 5.0,
            "force_flat_daily": True,
            "force_flat_friday": False,
            "thursday_gate": True,
            "commission_per_lot_rt": 7.0,
            "risk_include_commission": True,
        },
        "protected_eurdkk": sorted(PROTECTED_EURDKK),
        "notes": [
            "Fidelity only — no param search / no Sep OOS optimization.",
            "Live may already hold positions at T0; observed not force-closed.",
            "Mirror SEKJPY/88002 activity must not contaminate Live N1 PnL.",
        ],
    }
    write_json(RUN / "EXPERIMENT_MANIFEST.json", manifest)

    rows: list[dict[str, Any]] = []
    print(f"START observe until {t_end.isoformat()} run={RUN}", flush=True)

    i = 0
    while utc_now() < t_end:
        i += 1
        now = utc_now()
        live_st = safe_http("http://127.0.0.1:8791/api/status")
        mir_st = safe_http("http://127.0.0.1:8792/api/status")
        live_dash = safe_http("http://127.0.0.1:8791/api/dashboard")
        mir_dash = safe_http("http://127.0.0.1:8792/api/dashboard")
        mt5 = mt5_snapshot()

        # Never touch protected tickets — detect only
        protected_hit = []
        if mt5.get("ok"):
            for p in mt5.get("positions") or []:
                if int(p["ticket"]) in PROTECTED_EURDKK:
                    protected_hit.append(p["ticket"])

        live_sum = summarize_dash(
            "live",
            live_dash.get("data") if live_dash.get("ok") else None,
            live_dash.get("error"),
        )
        mir_sum = summarize_dash(
            "mirror",
            mir_dash.get("data") if mir_dash.get("ok") else None,
            mir_dash.get("error"),
        )

        deals = (mt5.get("deals_36h") or []) if mt5.get("ok") else []
        live_pnl = pnl_by_magic(deals, LIVE_MAGIC, since_unix)
        mir_pnl = pnl_by_magic(deals, MIRROR_MAGIC, since_unix)

        snap = {
            "i": i,
            "utc": now.isoformat(),
            "elapsed_min": round((now - t_start).total_seconds() / 60.0, 2),
            "remaining_min": round((t_end - now).total_seconds() / 60.0, 2),
            "live_status": live_st,
            "mirror_status": mir_st,
            "live_summary": live_sum,
            "mirror_summary": mir_sum,
            "mt5_ok": mt5.get("ok"),
            "mt5_balance": mt5.get("balance"),
            "mt5_equity": mt5.get("equity"),
            "mt5_positions": mt5.get("positions") if mt5.get("ok") else [],
            "live_pnl_window": live_pnl,
            "mirror_pnl_window": mir_pnl,
            "protected_tickets_open": protected_hit,
        }
        write_json(SNAP / f"snap_{i:04d}.json", snap)
        # compact row
        row = {
            "i": i,
            "utc": now.isoformat(),
            "elapsed_min": snap["elapsed_min"],
            "live_ok": live_st.get("ok"),
            "mirror_ok": mir_st.get("ok"),
            "live_open": live_sum.get("open_n"),
            "mirror_open": mir_sum.get("open_n"),
            "live_ghosts": live_sum.get("pending_ghosts_n"),
            "mirror_theory_open": mir_sum.get("theory_open_n"),
            "mt5_pos_n": len(snap["mt5_positions"] or []),
            "live_net_pnl": live_pnl["net"],
            "mirror_net_pnl": mir_pnl["net"],
            "live_deals": live_pnl["n_deals"],
            "mirror_deals": mir_pnl["n_deals"],
        }
        rows.append(row)
        with (RUN / "OBSERVATION_LOG.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            w.writeheader()
            w.writerows(rows)

        write_progress(
            {
                "phase": "observing",
                "i": i,
                "utc": now.isoformat(),
                "elapsed_min": snap["elapsed_min"],
                "remaining_min": snap["remaining_min"],
                "live_open": row["live_open"],
                "mirror_ok": row["mirror_ok"],
                "live_net_pnl": row["live_net_pnl"],
                "mirror_net_pnl": row["mirror_net_pnl"],
            }
        )
        print(
            f"[{i}] +{row['elapsed_min']}m live_open={row['live_open']} "
            f"mir_ok={row['mirror_ok']} live_net={row['live_net_pnl']:.2f} "
            f"mir_net={row['mirror_net_pnl']:.2f}",
            flush=True,
        )

        # sleep in chunks so we can end near t_end
        sleep_left = min(POLL_SEC, max(1, int((t_end - utc_now()).total_seconds())))
        time.sleep(sleep_left)

    # Final reconciliation
    print("FINALIZING", flush=True)
    t_finish = utc_now()
    live_dash = safe_http("http://127.0.0.1:8791/api/dashboard")
    mir_dash = safe_http("http://127.0.0.1:8792/api/dashboard")
    mt5 = mt5_snapshot()
    deals = (mt5.get("deals_36h") or []) if mt5.get("ok") else []
    live_pnl = pnl_by_magic(deals, LIVE_MAGIC, since_unix)
    mir_pnl = pnl_by_magic(deals, MIRROR_MAGIC, since_unix)

    frozen = run_frozen_window_backtest(t_start, t_finish)
    write_json(RUN / "FROZEN_WINDOW_BACKTEST.json", frozen)

    # Live vs Mirror open symbol overlap
    live_open = []
    mir_open = []
    if live_dash.get("ok"):
        live_open = [
            t.get("symbol") for t in (live_dash["data"].get("open_trades") or [])
        ]
    if mir_dash.get("ok"):
        # Mirror dashboard may use different keys
        d = mir_dash["data"]
        mir_open = [t.get("symbol") for t in (d.get("open_trades") or d.get("mirror_open") or [])]

    live_deals = [d for d in deals if int(d.get("magic") or 0) == LIVE_MAGIC and int(d.get("time") or 0) >= since_unix]
    mir_deals = [d for d in deals if int(d.get("magic") or 0) == MIRROR_MAGIC and int(d.get("time") or 0) >= since_unix]
    write_json(RUN / "LIVE_DEALS_WINDOW.json", live_deals)
    write_json(RUN / "MIRROR_DEALS_WINDOW.json", mir_deals)
    write_json(RUN / "MT5_FINAL_SNAPSHOT.json", mt5)

    final = {
        "run_id": RUN.name,
        "started_utc": t_start.isoformat(),
        "finished_utc": t_finish.isoformat(),
        "observe_hours_requested": OBSERVE_HOURS,
        "observe_hours_actual": round((t_finish - t_start).total_seconds() / 3600.0, 3),
        "snapshots": i,
        "live_pnl_window": live_pnl,
        "mirror_pnl_window": mir_pnl,
        "frozen_window": {
            "ok": frozen.get("ok"),
            "opens_n": frozen.get("window_opens_n"),
            "closes_n": frozen.get("window_closes_n"),
            "pnl_sum": frozen.get("window_pnl_sum"),
            "pending_tip_n": frozen.get("pending_tip_n"),
            "error": frozen.get("error"),
        },
        "live_open_symbols": live_open,
        "mirror_open_symbols": mir_open,
        "protected_eurdkk_still_untouched": True,
        "protected_present_on_account": mt5.get("protected_present") if mt5.get("ok") else [],
        "comparability": {
            "live_vs_mirror_net_pnl_delta": live_pnl["net"] - mir_pnl["net"],
            "note": "Economic equality not required; classify divergences in FINAL_RESULTS.md",
        },
    }
    write_json(RUN / "FINAL_RESULTS.json", final)

    md = f"""# FINAL_RESULTS — Live / Mirror / Frozen validation

**Run:** `{RUN.name}`  
**Window:** {t_start.isoformat()} → {t_finish.isoformat()} (~{final['observe_hours_actual']} h)  
**Knobs:** frozen N1 Thursday (unchanged)

## Broker PnL in window (MT5 deals)

| Path | Magic | Deals | Net (profit+comm+swap) |
|---|---:|---:|---:|
| Live | {LIVE_MAGIC} | {live_pnl['n_deals']} | {live_pnl['net']:.4f} |
| Mirror | {MIRROR_MAGIC} | {mir_pnl['n_deals']} | {mir_pnl['net']:.4f} |

## Frozen window backtest

- ok: `{frozen.get('ok')}`
- window opens: `{frozen.get('window_opens_n')}`
- window closes: `{frozen.get('window_closes_n')}`
- window closed PnL sum: `{frozen.get('window_pnl_sum')}`
- pending tip N: `{frozen.get('pending_tip_n')}`
- error: `{frozen.get('error')}`

## Open symbols at end

- Live: {live_open}
- Mirror: {mir_open}

## Protected EURDKK

Tickets `{sorted(PROTECTED_EURDKK)}` were **not** modified by this harness.
Present on account at end: `{final['protected_present_on_account']}`

## Verdict draft

Compare nets and fill counts above. Structural divergence vs execution noise should be classified in follow-up if nets differ materially.
"""
    (RUN / "FINAL_RESULTS.md").write_text(md, encoding="utf-8")
    write_progress({"phase": "done", "finished_utc": t_finish.isoformat(), "final": final})
    print("DONE", RUN, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
