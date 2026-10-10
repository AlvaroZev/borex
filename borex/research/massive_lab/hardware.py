"""Hardware probing and safe worker sizing for massive_lab."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


@dataclass
class HardwareProfile:
    cpus_logical: int
    ram_total_gb: float
    ram_avail_gb: float
    disk_free_gb: float
    recommended_workers: int
    notes: str


def _ram_gb() -> tuple[float, float]:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 1e9, m.ullAvailPhys / 1e9
    except Exception:
        return 8.0, 2.0


def probe_hardware(*, max_workers_cap: int = 8) -> HardwareProfile:
    cpus = os.cpu_count() or 4
    total, avail = _ram_gb()
    disk = shutil.disk_usage(".").free / 1e9
    # Each MultiMarketEngine period load is memory-heavy (~1–3GB peak).
    # Bound by available RAM first, then CPU.
    by_ram = max(1, int(avail // 2.5))  # ~2.5GB headroom per worker
    by_cpu = max(1, int(cpus * 0.75))
    workers = min(by_ram, by_cpu, max_workers_cap)
    if avail < 3.0:
        workers = min(workers, 3)
    if avail < 2.0:
        workers = min(workers, 2)
    notes = (
        f"cpus={cpus} ram_avail={avail:.1f}GB → workers={workers} "
        f"(cap={max_workers_cap}; prefer avoid swap thrash)"
    )
    return HardwareProfile(
        cpus_logical=cpus,
        ram_total_gb=total,
        ram_avail_gb=avail,
        disk_free_gb=disk,
        recommended_workers=workers,
        notes=notes,
    )
