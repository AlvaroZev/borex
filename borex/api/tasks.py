from __future__ import annotations

import threading

from borex.data.downloader import download_all
from borex.data.dukascopy_download import DEFAULT_END, DEFAULT_START, count_dukascopy_jobs, download_all_dukascopy
from borex.data.symbols import FOREX_PAIRS
from borex.data.timeframes import SUPPORTED_TIMEFRAMES
from borex.runner.live import dukascopy_callbacks, progress_callback, runs
from borex.runner.mass import MassRunConfig, build_mass_jobs, run_mass


def _run_in_thread(run_id: str, target, *args, **kwargs) -> None:
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    runs.register_worker(run_id, thread)
    thread.start()


def start_mass_run(cfg: MassRunConfig) -> str:
    jobs, run_group = build_mass_jobs(cfg)
    run_id = runs.create("mass", total=len(jobs), run_group=run_group)

    def _work() -> None:
        runs.mark_running(run_id)
        try:
            run_mass(
                cfg,
                on_progress=progress_callback(run_id),
                run_group=run_group,
            )
            runs.mark_done(run_id)
        except Exception as exc:
            runs.mark_error(run_id, str(exc))

    _run_in_thread(run_id, _work)
    return run_id


def start_download_run(*, force: bool = False) -> str:
    total = len(FOREX_PAIRS) * len(SUPPORTED_TIMEFRAMES)
    run_id = runs.create("download", total=total)

    def _work() -> None:
        runs.mark_running(run_id)
        try:
            download_all(force=force, on_progress=progress_callback(run_id))
            runs.mark_done(run_id)
        except Exception as exc:
            runs.mark_error(run_id, str(exc))

    _run_in_thread(run_id, _work)
    return run_id


def start_dukascopy_run(
    *,
    start: str = DEFAULT_START,
    end: str | None = DEFAULT_END,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    force: bool = False,
) -> str:
    total = count_dukascopy_jobs(
        symbols=symbols,
        timeframes=timeframes,
        start=start,
        end=end,
        force=force,
    )
    run_id = runs.create("dukascopy", total=total)
    on_job_start, on_progress, on_activity = dukascopy_callbacks(run_id)

    def _work() -> None:
        runs.mark_running(run_id)
        try:
            download_all_dukascopy(
                symbols=symbols,
                timeframes=timeframes,
                start=start,
                end=end,
                force=force,
                on_job_start=on_job_start,
                on_progress=on_progress,
                on_activity=on_activity,
            )
            runs.mark_done(run_id)
        except Exception as exc:
            runs.mark_error(run_id, str(exc))

    _run_in_thread(run_id, _work)
    return run_id
