from __future__ import annotations

from typing import Any

from borex.config import LiveConfig
from borex.runner.decision_log import create_alert
from borex.runner.killswitch import KillSwitch, KillSwitchState, check_stale_data


def compute_divergence(
    baseline_metrics: dict[str, Any],
    *,
    live_equity: float,
    initial_capital: float,
    live_bars: int,
    bars_per_year: float = 365 * 24,
) -> dict[str, Any]:
    """Compare live/paper equity path vs backtest baseline expectations."""
    baseline_return = float(baseline_metrics.get("total_return_pct", 0.0))
    baseline_sharpe = float(baseline_metrics.get("sharpe", 0.0))
    baseline_cagr = float(baseline_metrics.get("cagr_pct", 0.0))

    live_return = ((live_equity / initial_capital) - 1) * 100 if initial_capital > 0 else 0.0
    return_delta = live_return - baseline_return

    years = live_bars / bars_per_year if bars_per_year > 0 and live_bars > 0 else 0.0
    live_cagr = 0.0
    if years > 0 and live_equity > 0 and initial_capital > 0:
        live_cagr = ((live_equity / initial_capital) ** (1 / years) - 1) * 100

    cagr_delta = live_cagr - baseline_cagr if years > 0 else return_delta

    return {
        "baseline_return_pct": round(baseline_return, 4),
        "live_return_pct": round(live_return, 4),
        "return_delta_pct": round(return_delta, 4),
        "baseline_cagr_pct": round(baseline_cagr, 4),
        "live_cagr_pct": round(live_cagr, 4),
        "cagr_delta_pct": round(cagr_delta, 4),
        "baseline_sharpe": round(baseline_sharpe, 4),
        "live_bars": live_bars,
        "live_equity": round(live_equity, 2),
    }


def evaluate_live_health(
    session_id: str,
    *,
    kill: KillSwitch | KillSwitchState,
    live_config: LiveConfig,
    last_bar_ts: str | None,
    status: str,
    portfolio_liquidated: bool,
    risk_halted: bool,
    divergence: dict[str, Any],
    trip_kill: bool = False,
) -> dict[str, Any]:
    """Run health checks; optionally emit alerts and trip kill-switch."""
    if isinstance(kill, KillSwitchState):
        ks = KillSwitch(state=kill)
    else:
        ks = kill

    alerts: list[dict] = []
    health = "ok"
    stale, age_min = check_stale_data(last_bar_ts, live_config.stale_data_minutes)

    if ks.killed or status == "killed":
        health = "killed"
    elif status == "paused":
        health = "paused"
    elif stale:
        health = "stale_data"
        if trip_kill:
            ks.trip("stale_data")
        aid = create_alert(
            session_id,
            severity="critical" if trip_kill else "warn",
            code="stale_data",
            message=f"Data stale ({age_min} min since last bar)" + ("; kill-switch engaged" if trip_kill else ""),
            detail={"age_minutes": age_min, "last_bar_ts": last_bar_ts},
        )
        alerts.append({"id": aid, "code": "stale_data", "severity": "critical" if trip_kill else "warn"})
    elif portfolio_liquidated and live_config.kill_on_liquidation:
        health = "liquidated"
        if trip_kill:
            ks.trip("liquidation")
        aid = create_alert(
            session_id,
            severity="critical",
            code="liquidation",
            message="Portfolio liquidated" + ("; kill-switch engaged" if trip_kill else ""),
        )
        alerts.append({"id": aid, "code": "liquidation", "severity": "critical"})
    elif risk_halted and live_config.kill_on_halt:
        health = "risk_halt"
        if trip_kill:
            ks.trip("risk_halt")
        aid = create_alert(
            session_id,
            severity="critical",
            code="risk_halt",
            message="Risk circuit breaker tripped" + ("; kill-switch engaged" if trip_kill else ""),
        )
        alerts.append({"id": aid, "code": "risk_halt", "severity": "critical"})
    elif live_config.divergence_warn_pct is not None:
        threshold = live_config.divergence_warn_pct * 100
        delta = abs(float(divergence.get("return_delta_pct", 0.0)))
        if delta >= threshold:
            health = "divergence_warn"
            aid = create_alert(
                session_id,
                severity="warn",
                code="divergence",
                message=f"Live return diverges {delta:.1f}% from backtest baseline",
                detail=divergence,
            )
            alerts.append({"id": aid, "code": "divergence", "severity": "warn"})

    return {
        "health": health,
        "killed": ks.killed,
        "kill_reason": ks.state.reason if isinstance(ks, KillSwitch) else ks.reason,
        "consecutive_errors": ks.state.consecutive_errors if isinstance(ks, KillSwitch) else ks.consecutive_errors,
        "data_age_minutes": age_min,
        "divergence": divergence,
        "alerts": alerts,
    }
