"""Job catalog builders for massive_lab worker groups."""

from __future__ import annotations

import itertools
from typing import Any, Iterator

from borex.gate2.exhaustive.search import config_hash


def _exp(group: str, strategy: str, family: str, params: dict, *, category: str = "DISCOVERY_ONLY") -> dict:
    payload = {"group": group, "strategy": strategy, "family": family, **params}
    h = config_hash(payload)
    return {
        "experiment_id": f"{group}:{h}",
        "group_id": group,
        "strategy": strategy,
        "family": family,
        "config_hash": h,
        "params": params,
        "category": category,
        "data_range": "2026-06..2026-08",
        "execution_model": "n1_frozen_h1_1m",
    }


def group_A_baselines() -> list[dict]:
    """Locked refs + generation labels (tape/control path)."""
    out = []
    out.append(
        _exp(
            "A_BASELINE",
            "alexg8",
            "n1_control",
            {"kind": "n1_tape", "thursday_gate": False, "rr_factor": 1.0, "rr_min": 2.0, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
            category="LOCKED_EXISTING_CANDIDATE",
        )
    )
    out.append(
        _exp(
            "A_BASELINE",
            "alexg8",
            "n1_thursday",
            {"kind": "n1_tape", "thursday_gate": True, "rr_factor": 1.0, "rr_min": 2.0, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
            category="LOCKED_EXISTING_CANDIDATE",
        )
    )
    return out


def group_C_rr_surface() -> list[dict]:
    rr_factors = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0]
    rr_mins = [1.0, 1.5, 2.0, 2.25, 2.5, 3.0, 3.5, 4.0, 5.0]
    out = []
    for rf in rr_factors:
        out.append(
            _exp(
                "C_RR",
                "alexg8",
                "rr_factor",
                {"kind": "n1_tape", "rr_factor": rf, "rr_min": 2.0, "thursday_gate": False, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
            )
        )
    for rm in rr_mins:
        out.append(
            _exp(
                "C_RR",
                "alexg8",
                "rr_min",
                {"kind": "n1_tape", "rr_factor": 1.0, "rr_min": rm, "thursday_gate": False, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
            )
        )
    # pairwise floor × factor (bounded)
    for rf, rm in itertools.product([0.8, 1.0, 1.2, 1.5], [2.0, 2.5, 3.0]):
        out.append(
            _exp(
                "C_RR",
                "alexg8",
                "rr_pair",
                {"kind": "n1_tape", "rr_factor": rf, "rr_min": rm, "thursday_gate": False, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
            )
        )
    return out


def group_F_weekday() -> list[dict]:
    out = []
    for dow in range(5):  # Mon=0 .. Fri=4
        out.append(
            _exp(
                "F_WEEKDAY",
                "alexg8",
                "exclude_dow",
                {"kind": "n1_tape", "exclude_dow": dow, "rr_factor": 1.0, "rr_min": 2.0, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
            )
        )
    out.append(
        _exp(
            "F_WEEKDAY",
            "alexg8",
            "thursday_gate",
            {"kind": "n1_tape", "thursday_gate": True, "rr_factor": 1.0, "rr_min": 2.0, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
        )
    )
    # RR × Thursday
    for rf, rm in itertools.product([0.9, 1.0, 1.1], [2.0, 2.5]):
        out.append(
            _exp(
                "F_WEEKDAY",
                "alexg8",
                "thu_rr",
                {"kind": "n1_tape", "thursday_gate": True, "rr_factor": rf, "rr_min": rm, "session": "all", "sides": "both", "ghost_sl_mult": 4.0},
            )
        )
    return out


def group_G_session() -> list[dict]:
    out = []
    for sess in ("all", "asia", "london", "newyork", "overlap"):
        for thu in (False, True):
            out.append(
                _exp(
                    "G_SESSION",
                    "alexg8",
                    "session",
                    {"kind": "n1_tape", "session": sess, "thursday_gate": thu, "rr_factor": 1.0, "rr_min": 2.0, "sides": "both", "ghost_sl_mult": 4.0},
                )
            )
    for sides in ("long_only", "short_only"):
        out.append(
            _exp(
                "G_SESSION",
                "alexg8",
                "sides",
                {"kind": "n1_tape", "sides": sides, "session": "all", "thursday_gate": False, "rr_factor": 1.0, "rr_min": 2.0, "ghost_sl_mult": 4.0},
            )
        )
    return out


def group_B_ghost_surface() -> list[dict]:
    out = []
    for g in [0.6, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]:
        for thu in (False, True):
            out.append(
                _exp(
                    "B_GHOST",
                    "alexg8",
                    "ghost_sl_mult",
                    {"kind": "n1_tape", "ghost_sl_mult": g, "thursday_gate": thu, "rr_factor": 1.0, "rr_min": 2.0, "session": "all", "sides": "both"},
                )
            )
    return out


def group_I_internet() -> list[dict]:
    out = []
    for i in range(1, 21):
        name = f"internet{i}"
        for ghostsl in (False, True):
            for thu in (False, True):
                out.append(
                    _exp(
                        "I_INTERNET",
                        name,
                        "internet_base",
                        {"kind": "internet", "name": name, "ghostsl": ghostsl, "thursday_gate": thu, "rr": 2.0},
                    )
                )
    return out


def group_K_donchian() -> list[dict]:
    out = []
    for lb in [5, 10, 15, 20, 25, 30, 40, 50, 55, 60, 75, 100]:
        for ghostsl in (False, True):
            for thu in (False, True):
                out.append(
                    _exp(
                        "K_DONCHIAN",
                        "donchian",
                        "lookback",
                        {"kind": "donchian", "lookback": lb, "ghostsl": ghostsl, "thursday_gate": thu, "rr": 2.0, "atr_mult": 2.0},
                    )
                )
    return out


def group_D_ghostsl_causal() -> list[dict]:
    """Causal controls for GhostSL on internet1/2 (matched delayed entry)."""
    out = []
    for name in ("internet1", "internet2"):
        for mode in ("ghostsl", "delayed_equiv", "base"):
            out.append(
                _exp(
                    "D_GHOSTSL_CAUSAL",
                    name,
                    mode,
                    {"kind": "ghostsl_causal", "name": name, "mode": mode, "thursday_gate": False, "rr": 2.0},
                )
            )
    return out


def group_H_liqfilter() -> list[dict]:
    out = []
    for thr in (0.5, 0.8, 1.0, 1.2, 1.5):
        for thu in (False, True):
            out.append(
                _exp(
                    "H_LIQFILTER",
                    "alexg8",
                    "disp_filter",
                    {"kind": "n1_disp_filter", "min_body_atr": thr, "thursday_gate": thu, "rr_factor": 1.0, "rr_min": 2.0},
                )
            )
    return out


def group_L_trend() -> list[dict]:
    out = []
    for ghostsl in (False, True):
        for thu in (False, True):
            out.append(
                _exp(
                    "L_TREND",
                    "internet4",
                    "ema_trend",
                    {"kind": "internet", "name": "internet4", "ghostsl": ghostsl, "thursday_gate": thu, "rr": 2.0},
                )
            )
    return out


def group_M_meanrev() -> list[dict]:
    out = []
    for name in ("internet6", "internet7", "internet17"):
        for ghostsl in (False, True):
            for thu in (False, True):
                out.append(
                    _exp(
                        "M_MEANREV",
                        name,
                        "mr",
                        {"kind": "internet", "name": name, "ghostsl": ghostsl, "thursday_gate": thu, "rr": 1.5},
                    )
                )
    return out


def all_stage1_jobs() -> list[dict]:
    jobs: list[dict] = []
    for builder in (
        group_A_baselines,
        group_C_rr_surface,
        group_F_weekday,
        group_G_session,
        group_B_ghost_surface,
        group_I_internet,
        group_K_donchian,
        group_D_ghostsl_causal,
        group_H_liqfilter,
        group_L_trend,
        group_M_meanrev,
    ):
        jobs.extend(builder())
    # dedupe by config_hash
    seen = set()
    uniq = []
    for j in jobs:
        if j["config_hash"] in seen:
            continue
        seen.add(j["config_hash"])
        uniq.append(j)
    return uniq


def chunk_jobs(jobs: list[dict], n_chunks: int) -> list[list[dict]]:
    n_chunks = max(1, n_chunks)
    chunks: list[list[dict]] = [[] for _ in range(n_chunks)]
    for i, j in enumerate(jobs):
        chunks[i % n_chunks].append(j)
    return [c for c in chunks if c]
