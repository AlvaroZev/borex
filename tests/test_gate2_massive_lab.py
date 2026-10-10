"""Tests for massive_lab orchestrator primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from borex.gate2.september_firewall import SeptemberFirewallError, assert_not_september
from borex.research.massive_lab.hardware import probe_hardware
from borex.research.massive_lab.jobs import (
    all_stage1_jobs,
    chunk_jobs,
    group_C_rr_surface,
    group_I_internet,
)
from borex.research.massive_lab.registry import ExperimentRegistry


def test_september_firewall_still_hard():
    with pytest.raises(SeptemberFirewallError):
        assert_not_september("2026-09-01T00:00:00+00:00")


def test_hardware_workers_bounded():
    hw = probe_hardware(max_workers_cap=4)
    assert 1 <= hw.recommended_workers <= 4


def test_job_catalog_unique_hashes():
    jobs = all_stage1_jobs()
    hashes = [j["config_hash"] for j in jobs]
    assert len(hashes) == len(set(hashes))
    assert len(jobs) > 100
    assert any(j["group_id"] == "I_INTERNET" for j in jobs)
    assert any(j["group_id"] == "C_RR" for j in jobs)


def test_chunk_jobs():
    jobs = group_C_rr_surface()[:10]
    chunks = chunk_jobs(jobs, 3)
    assert sum(len(c) for c in chunks) == 10


def test_registry_resume_and_cache(tmp_path: Path):
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    job = group_I_internet()[0]
    assert reg.register(job) is True
    assert reg.register(job) is False  # duplicate
    assert not reg.is_done(job["config_hash"])
    reg.mark_running(job["config_hash"], "W0")
    reg.mark_done(job["config_hash"], {"net": 1.0, "fills": 10}, 0.5)
    assert reg.is_done(job["config_hash"])
    assert reg.counts().get("done") == 1
    reg.close()
