"""python -m borex.research.massive_lab

Massive parallel Gate 2 discovery orchestrator.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from borex.research.massive_lab.aggregate import aggregate
from borex.research.massive_lab.hardware import probe_hardware
from borex.research.massive_lab.jobs import all_stage1_jobs, chunk_jobs
from borex.research.massive_lab.registry import ExperimentRegistry
from borex.research.massive_lab.worker import run_chunk


def _git() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    except Exception:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Gate 2 massive parallel discovery lab")
    p.add_argument("--workers", type=int, default=0, help="0=auto from hardware")
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--register-only", action="store_true")
    p.add_argument("--aggregate-only", action="store_true")
    p.add_argument("--out", type=str, default="")
    args = p.parse_args(argv)

    hw = probe_hardware(max_workers_cap=args.max_workers)
    n_workers = args.workers or hw.recommended_workers
    n_workers = max(1, min(n_workers, args.max_workers))

    if args.out:
        out = Path(args.out)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = ROOT / "data" / "runs" / "research" / f"gate2_massive_lab_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    reg_path = out / "results.sqlite"
    reg = ExperimentRegistry(reg_path)

    print(f"OUT {out}", flush=True)
    print(f"HARDWARE {hw.notes}", flush=True)
    print(f"WORKERS {n_workers}", flush=True)

    reg.set_meta("hardware", hw.__dict__)
    reg.set_meta("git", _git())
    reg.set_meta("september_firewall", True)
    reg.set_meta("locked_oos", ["n1_control", "n1_thursday"])
    reg.set_meta("live_mirror_untouched", True)

    (out / "massive_lab_inventory.md").write_text(
        "\n".join(
            [
                "# Massive Lab Inventory",
                "",
                "Stage-1 groups: A_BASELINE, B_GHOST, C_RR, D_GHOSTSL_CAUSAL, F_WEEKDAY,",
                "G_SESSION, H_LIQFILTER, I_INTERNET, K_DONCHIAN, L_TREND, M_MEANREV",
                "",
                "Data: Jun–Aug 2026 H1+M1 utc_v2, lev=500 N1 geometry.",
                "September reserved. LIVE/Mirror untouched.",
                "",
                f"Git: `{_git()}`",
                f"Hardware: {hw.notes}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out / "september_lock.md").write_text(
        "September unread. Locked OOS: n1_control vs n1_thursday. Unchanged.\n",
        encoding="utf-8",
    )

    if args.aggregate_only:
        summary = aggregate(out, reg)
        print("AGGREGATED", summary["n_done"], flush=True)
        reg.close()
        return 0

    jobs = all_stage1_jobs()
    new = 0
    for j in jobs:
        if reg.register(j):
            new += 1
    print(f"Registered jobs={len(jobs)} new={new} status={reg.counts()}", flush=True)
    (out / "job_catalog.json").write_text(
        json.dumps({"n": len(jobs), "groups": sorted({j["group_id"] for j in jobs})}, indent=2),
        encoding="utf-8",
    )

    if args.register_only:
        reg.close()
        return 0

    # Only pending/failed
    pending = reg.pending()
    print(f"Pending/failed to run: {len(pending)}", flush=True)
    if not pending:
        summary = aggregate(out, reg)
        print("Nothing pending; aggregated", summary["n_done"], flush=True)
        reg.close()
        return 0

    chunks = chunk_jobs(pending, n_workers)
    print(f"Launching {len(chunks)} worker chunks…", flush=True)
    t0 = time.perf_counter()
    # Use spawn for Windows safety
    ctx = mp.get_context("spawn")
    payload = [(f"W{i}", str(reg_path), ch) for i, ch in enumerate(chunks)]
    with ctx.Pool(processes=len(chunks)) as pool:
        stats = pool.map(run_chunk, payload)
    elapsed = (time.perf_counter() - t0) / 60.0
    print(f"Workers finished in {elapsed:.1f} min: {stats}", flush=True)
    reg.set_meta("worker_stats", stats)
    reg.set_meta("elapsed_min", elapsed)

    summary = aggregate(out, reg)
    (out / "README.md").write_text(
        f"# Gate 2 Massive Lab\n\nDISCOVERY_ONLY. Workers={n_workers}. Done={summary['n_done']}.\n"
        f"See massive_lab_summary.md\n",
        encoding="utf-8",
    )
    print("DONE", out, "evals", summary["n_done"], flush=True)
    reg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
