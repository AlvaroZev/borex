"""Constants for Gate 2 E fidelity assessment."""

from __future__ import annotations

OHLC_PATH_UNRESOLVABLE = "OHLC_PATH_UNRESOLVABLE"

# Explicit exclusion from simulator-fidelity disagreement counts.
EXCLUDED_SIM_FIDELITY_TICKET = "1959571094"
EXCLUDED_SIM_FIDELITY_SOURCE_RUN = "g2m_20260924_043542"
EXCLUDED_SIM_FIDELITY_REASON = OHLC_PATH_UNRESOLVABLE
EXCLUDED_SIM_FIDELITY_NOTE = (
    "Continuous M1 archive exists; 72 M1 bars present; no archive gap; "
    "broker exited BROKER_SL; OHLC wick replay never touches SL/TP. "
    "Inherent M1 OHLC path limitation — not a simulator failure and not missing data."
)

NATURAL_MICRO_RUNS = (
    "g2m_20260924_001929",  # Level 4 — stitched M1 (medium weight for sim exit)
    "g2m_20260924_043542",  # Level 5b — continuous M1 (highest weight for sim exit)
)

REPLAY_RUNS = {
    "g2m_20260924_001929": "g2r_20260924_032600",
    "g2m_20260924_043542": "g2r_20260924_055920",
}

CONTROLLED_RUNS = {
    "g2p_20260922_162842": {
        "kind": "controlled_probe",
        "path": "data/runs/gate2_controlled_probe/batches/g2p_20260922_162842",
        "established": [
            "market entry execution",
            "deal reconciliation",
            "execution reporting",
            "market-close handling",
        ],
    },
    "g2e_20260923_170458": {
        "kind": "controlled_broker_exit_probe",
        "path": "data/runs/gate2_broker_exit_probe/runs/g2e_20260923_170458",
        "established": [
            "broker SL (DEAL_REASON_SL)",
            "broker TP (DEAL_REASON_TP)",
            "entry reconciliation",
            "exit reconciliation",
            "actual broker exit reasons",
        ],
    },
    "g2f_20260923_181609": {
        "kind": "fidelity_reconciliation",
        "path": "data/runs/gate2_fidelity_reconciliation/runs/g2f_20260923_181609",
        "established": [
            "mapping architecture",
            "natural vs controlled separation",
            "reconciliation taxonomy",
        ],
    },
}

NATURAL_SAMPLE_REGISTRY = "data/runs/gate2_natural_sample/registry.json"

EVIDENCE_WEIGHT = {
    "g2m_20260924_043542": "highest_sim_broker",  # continuous M1 + exit timestamps
    "g2m_20260924_001929": "medium_sim_broker",  # stitched M1
    "controlled": "plumbing_only",
    "natural_sample_registry": "legacy_context",
}

# Forbidden keys in fidelity assessment payloads (profitability is not fidelity).
FORBIDDEN_PNL_METRIC_KEYS = frozenset(
    {
        "pf",
        "profit_factor",
        "wr",
        "win_rate",
        "mdd",
        "max_drawdown",
        "expectancy",
        "net_pnl_total",
        "return",
        "returns",
        "optimization",
        "sharpe",
    }
)
