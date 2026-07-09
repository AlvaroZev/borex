from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from borex.config import IterationConfig


@dataclass
class DecayReport:
    verdict: str  # healthy | warning | decayed | insufficient_data
    baseline: dict[str, Any] = field(default_factory=dict)
    recent: dict[str, Any] = field(default_factory=dict)
    deltas: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    capital: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "baseline": self.baseline,
            "recent": self.recent,
            "deltas": self.deltas,
            "reasons": self.reasons,
            "capital": self.capital,
        }


def compare_metrics(
    baseline_metrics: dict[str, Any],
    recent_metrics: dict[str, Any],
    *,
    cfg: IterationConfig,
    baseline_period: dict[str, str] | None = None,
    recent_period: dict[str, str] | None = None,
) -> DecayReport:
    """Compare recent-window backtest vs baseline; emit decay verdict."""
    recent_trades = int(recent_metrics.get("trades", 0))
    if recent_trades < cfg.min_recent_trades:
        return DecayReport(
            verdict="insufficient_data",
            baseline={"metrics": baseline_metrics, "period": baseline_period or {}},
            recent={"metrics": recent_metrics, "period": recent_period or {}},
            reasons=[f"Recent window has {recent_trades} trades (need {cfg.min_recent_trades})"],
        )

    b_sharpe = float(baseline_metrics.get("sharpe", 0))
    r_sharpe = float(recent_metrics.get("sharpe", 0))
    b_ret = float(baseline_metrics.get("total_return_pct", 0))
    r_ret = float(recent_metrics.get("total_return_pct", 0))
    b_pf = float(baseline_metrics.get("profit_factor", 0))
    r_pf = float(recent_metrics.get("profit_factor", 0))
    b_wr = float(baseline_metrics.get("win_rate", 0))
    r_wr = float(recent_metrics.get("win_rate", 0))

    sharpe_delta = r_sharpe - b_sharpe
    return_delta = r_ret - b_ret
    pf_delta = r_pf - b_pf
    wr_delta = r_wr - b_wr

    reasons: list[str] = []
    severity = 0  # 0 healthy, 1 warning, 2 decayed

    if b_sharpe > 0 and sharpe_delta <= -cfg.decay_sharpe_delta:
        reasons.append(
            f"Sharpe dropped {abs(sharpe_delta):.2f} (recent {r_sharpe:.2f} vs baseline {b_sharpe:.2f})"
        )
        severity = max(severity, 2)
    elif b_sharpe > 0 and sharpe_delta <= -cfg.decay_sharpe_delta * 0.5:
        reasons.append(f"Sharpe weakening ({sharpe_delta:+.2f})")
        severity = max(severity, 1)

    if return_delta <= -cfg.decay_return_pct:
        reasons.append(
            f"Return lagging baseline by {abs(return_delta):.1f}% (recent {r_ret:.1f}% vs {b_ret:.1f}%)"
        )
        severity = max(severity, 2)
    elif return_delta <= -cfg.decay_return_pct * 0.5:
        reasons.append(f"Return below baseline ({return_delta:+.1f}%)")
        severity = max(severity, 1)

    if b_pf >= 1.0 and r_pf < 1.0:
        reasons.append(f"Profit factor below 1.0 in recent window ({r_pf:.2f})")
        severity = max(severity, 2)
    elif b_pf > 0 and pf_delta <= -cfg.decay_profit_factor:
        reasons.append(f"Profit factor declined ({pf_delta:+.2f})")
        severity = max(severity, 1)

    if r_ret < 0 and b_ret > 0:
        reasons.append("Recent window negative while baseline was positive")
        severity = max(severity, 2)

    if recent_metrics.get("liquidated"):
        reasons.append("Recent window ended in liquidation")
        severity = max(severity, 2)

    verdict = {0: "healthy", 1: "warning", 2: "decayed"}.get(severity, "healthy")
    if verdict == "healthy" and not reasons:
        reasons.append("Recent performance within baseline tolerance")

    return DecayReport(
        verdict=verdict,
        baseline={"metrics": baseline_metrics, "period": baseline_period or {}},
        recent={"metrics": recent_metrics, "period": recent_period or {}},
        deltas={
            "sharpe": round(sharpe_delta, 4),
            "return_pct": round(return_delta, 4),
            "profit_factor": round(pf_delta, 4),
            "win_rate": round(wr_delta, 4),
        },
        reasons=reasons,
    )


def capital_scale_recommendation(
    *,
    cfg: IterationConfig,
    decay_verdict: str,
    initial_capital: float,
    current_capital: float | None = None,
    paper_days: float = 0,
    paper_trades: int = 0,
    health: str = "ok",
    killed: bool = False,
) -> dict[str, Any]:
    """Recommend whether capital can scale up after meaningful live/paper track record."""
    cap = current_capital if current_capital is not None else initial_capital
    max_cap = initial_capital * cfg.max_capital_multiplier
    at_max = cap >= max_cap * 0.99

    blockers: list[str] = []
    if killed:
        blockers.append("kill_switch_active")
    if health not in ("ok", "running", "working"):
        blockers.append(f"health_{health}")
    if decay_verdict in ("decayed", "insufficient_data"):
        blockers.append(f"decay_{decay_verdict}")
    if paper_days < cfg.min_paper_days:
        blockers.append(f"need_{cfg.min_paper_days}_days_paper (have {paper_days:.0f})")
    if paper_trades < cfg.min_paper_trades:
        blockers.append(f"need_{cfg.min_paper_trades}_paper_trades (have {paper_trades})")
    if at_max:
        blockers.append("at_max_capital_multiplier")

    if blockers:
        return {
            "action": "hold",
            "current_capital": round(cap, 2),
            "recommended_capital": round(cap, 2),
            "max_capital": round(max_cap, 2),
            "blockers": blockers,
        }

    recommended = min(cap * cfg.scale_up_factor, max_cap)
    return {
        "action": "scale_up",
        "current_capital": round(cap, 2),
        "recommended_capital": round(recommended, 2),
        "max_capital": round(max_cap, 2),
        "scale_factor": cfg.scale_up_factor,
        "blockers": [],
    }
