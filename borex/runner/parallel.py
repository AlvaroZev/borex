from __future__ import annotations

import os


def cpu_count() -> int:
    return os.cpu_count() or 8


def default_workers(*, reserve: int = 2, minimum: int = 4, cap: int = 32) -> int:
    """Process pool size for mass/screen/sweep (leave headroom for OS)."""
    return max(minimum, min(cap, cpu_count() - reserve))


def resolve_wf_workers(
    *,
    nested_job: bool = False,
    train_sweep_workers: int = 0,
    fold_workers: int = 0,
    outer_workers: int = 1,
) -> tuple[int, int]:
    """
    Pick train-sweep and fold parallelism without oversubscribing the CPU.

    nested_job: screen/mass worker already running in a pool -> no inner pools.
    outer_workers: how many top-level jobs run at once (screen pool size).
    """
    if nested_job:
        return 1, 1

    total = cpu_count()
    budget = max(1, total // max(1, outer_workers))

    fold_w = fold_workers if fold_workers > 0 else max(1, min(8, budget // 2))
    if fold_w > 1:
        return 1, fold_w

    train_w = train_sweep_workers if train_sweep_workers > 0 else max(1, min(4, budget))
    return train_w, 1
