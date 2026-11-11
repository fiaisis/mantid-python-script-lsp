"""The analysis pool: how big it should be, and whether work survives leaving the process.

The reason this service has a pool at all is measured - 24 concurrent analyses on
Starlette's threadpool cost 1,534 ms each, worse than running them one at a time - so these tests cover
both the sizing rule and the thing that could silently break when work moves to another process: the
positions in the reply.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from script_lsp import pool as pool_module
from script_lsp.pool import (
    _divide,
    analyse_in_pool,
    analyse_script,
    bind_preamble,
    cpu_quota,
    create_pool,
    warm,
    worker_count,
)
from script_lsp.preamble import Preamble

PREAMBLE = Preamble.from_names(["Load", "MergeMD", "mtd"])


def test_one_worker_per_allowed_core() -> None:
    """More workers than the pod's CPU quota buys throttling, not throughput.

    CFS hands out quota in ~100 ms periods; two workers against a 1 CPU limit both run, then the whole
    group stalls until the period rolls over, which turns a throughput problem into a latency spike.
    """
    assert worker_count(environ={}, quota=1.0) == 1
    assert worker_count(environ={}, quota=2.0) == 2
    assert worker_count(environ={}, quota=3.6) == 4
    assert worker_count(environ={}, quota=0.25) == 1  # never zero: a pool with no workers never replies


def test_with_no_quota_to_read_the_pool_stays_small_rather_than_matching_the_host() -> None:
    """`os.process_cpu_count()` counts the host's cores, which on a shared node is not an invitation.

    A laptop, or a pod on a node that reports no limit, gets a bounded pool: enough to overlap a burst of
    edits, few enough that one pod cannot take the node's cores from everything else on it.
    """
    assert 1 <= worker_count(environ={}, quota=None) <= 4


def test_an_explicit_count_wins_over_the_quota() -> None:
    """The deployment sometimes knows better than the cgroup - and the escape hatch has to work."""
    assert worker_count(environ={"ANALYSIS_WORKERS": "6"}, quota=1.0) == 6


@pytest.mark.parametrize("value", ["", "nonsense", "0", "-3"])
def test_a_bad_override_falls_back_to_the_quota_rather_than_to_zero(value: str) -> None:
    """A typo in a manifest must not produce a service that answers nothing."""
    assert worker_count(environ={"ANALYSIS_WORKERS": value}, quota=4.0) == 4


def test_quota_reading_tolerates_a_missing_cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Containers, CI runners and laptops all differ here; none of them is an error."""
    monkeypatch.setattr("script_lsp.pool.CGROUP_V2_QUOTA", str(tmp_path / "absent"))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_QUOTA", str(tmp_path / "absent"))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_PERIOD", str(tmp_path / "absent"))

    assert cpu_quota() is None


def test_a_v2_quota_is_read_as_cores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cpu.max` of "50000 100000" means half a core."""
    quota_file = tmp_path / "cpu.max"
    quota_file.write_text("50000 100000\n", encoding="utf-8")
    monkeypatch.setattr("script_lsp.pool.CGROUP_V2_QUOTA", str(quota_file))

    assert cpu_quota() == pytest.approx(0.5)


def test_a_v2_quota_of_max_means_unlimited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quota_file = tmp_path / "cpu.max"
    quota_file.write_text("max 100000\n", encoding="utf-8")
    monkeypatch.setattr("script_lsp.pool.CGROUP_V2_QUOTA", str(quota_file))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_QUOTA", str(tmp_path / "absent"))

    assert cpu_quota() is None


def test_a_malformed_quota_file_is_treated_as_unlimited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nodes have been observed to contain surprises here; the fallback must still give a working pool."""
    quota_file = tmp_path / "cpu.max"
    quota_file.write_text("garbage\n", encoding="utf-8")
    monkeypatch.setattr("script_lsp.pool.CGROUP_V2_QUOTA", str(quota_file))

    assert cpu_quota() is None


def test_a_v1_quota_of_minus_one_means_unlimited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cgroup v1 spells "no limit" as a quota of -1, which is not a pool of negative size."""
    (tmp_path / "quota").write_text("-1\n", encoding="utf-8")
    (tmp_path / "period").write_text("100000\n", encoding="utf-8")
    monkeypatch.setattr("script_lsp.pool.CGROUP_V2_QUOTA", str(tmp_path / "absent"))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_QUOTA", str(tmp_path / "quota"))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_PERIOD", str(tmp_path / "period"))

    assert cpu_quota() is None


def test_positions_survive_the_trip_into_a_worker_process() -> None:
    """The point of the change, and the way it could go wrong quietly.

    The preamble is handed to the child by an initializer and the offsets are undone there; if the child
    were left with no bindings, every framework name would come back "undefined" - plausible output,
    wrong story. So: a script whose one unbound name is deliberate must come back as exactly that one,
    at the script's own line and column, from a different process.
    """
    script = "from mantid.simpleapi import *\n\nws = Load(Filename='x.nxs')\nws = Lod(Filename='y.nxs')\n"
    pool = create_pool(PREAMBLE)

    try:
        parses, diagnostics = asyncio.run(analyse_in_pool(pool, script))
    finally:
        pool.shutdown(wait=True)

    assert parses is True
    assert [(item.message, item.start_line, item.start_column) for item in diagnostics] == [
        ("undefined name 'Lod'", 4, 6)
    ]


def test_a_worker_reports_a_script_that_does_not_parse_without_linting_it() -> None:
    """Same rule as in-process: half-typed code yields one true error, not an undefined name per name."""
    pool = create_pool(PREAMBLE)

    try:
        parses, diagnostics = asyncio.run(analyse_in_pool(pool, "def reduce_run(runno:\n    return nowhere\n"))
    finally:
        pool.shutdown(wait=True)

    assert parses is False
    assert {item.source for item in diagnostics} == {"compile"}


def test_a_shut_down_pool_fails_rather_than_running_the_work_in_process() -> None:
    """If the pool is gone, analysis is unavailable - not silently moved onto the event loop.

    Running it inline would answer 200 with diagnostics from a process that shares the loop, which is the
    exact behaviour the pool exists to remove.
    """
    pool = create_pool(PREAMBLE)
    pool.shutdown(wait=True)

    with pytest.raises(RuntimeError):
        asyncio.run(analyse_in_pool(pool, "x = 1\n"))


def test_the_pool_honours_the_configured_worker_count() -> None:
    """A manifest says `ANALYSIS_WORKERS: 3` and the pool must have three processes, not the host's cores."""
    os.environ["ANALYSIS_WORKERS"] = "2"

    try:
        pool = create_pool(PREAMBLE)
        try:
            assert pool._max_workers == 2
        finally:
            pool.shutdown(wait=True)
    finally:
        os.environ.pop("ANALYSIS_WORKERS", None)


def test_warming_starts_every_worker_so_the_first_burst_is_not_the_startup_cost() -> None:
    """A lazily spawned pool pays jedi's import inside a user's request.

    Measured: the first 24-request burst against a cold pool took 670 ms and the same burst afterwards
    took 211 ms. Readiness should not be reported until that has already happened.
    """
    os.environ["ANALYSIS_WORKERS"] = "3"
    pool = create_pool(PREAMBLE)

    try:
        assert len(pool._processes) == 0
        warm(pool)
        assert len(pool._processes) == 3
    finally:
        pool.shutdown(wait=True)
        os.environ.pop("ANALYSIS_WORKERS", None)


def test_creating_a_pool_does_not_add_another_startup_pause() -> None:
    """Warm-up already pays for Mantid; the pool must not add seconds to the startup probe budget."""
    started = time.perf_counter()
    pool = create_pool(PREAMBLE)
    elapsed = time.perf_counter() - started
    pool.shutdown(wait=True)

    assert elapsed < 1.0


def test_a_worker_without_bound_names_reports_syntax_only() -> None:
    """The gate that keeps an unwarmed pool from telling users their framework names are undefined.

    Called directly, because in production this runs in a child process where coverage cannot see it - and
    the branch is the difference between "no lint yet" and "every algorithm is a typo".
    """
    parses, diagnostics = analyse_script("x = Load()\n")

    assert (parses, diagnostics) == (True, [])


def test_a_worker_with_bound_names_lints_in_whatever_process_it_runs_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pool_module, "_BOUND_PREAMBLE", PREAMBLE)

    parses, diagnostics = analyse_script("x = Load()\ny = Lod()\n")

    assert parses is True
    assert [(item.message, item.start_line) for item in diagnostics] == [("undefined name 'Lod'", 2)]


def test_the_initializer_binds_the_names_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the pool hands each worker, and the reason workers can start without Mantid."""
    monkeypatch.setattr(pool_module, "_BOUND_PREAMBLE", None)

    bind_preamble(PREAMBLE)

    assert pool_module._BOUND_PREAMBLE is PREAMBLE


def test_a_quota_division_that_cannot_be_done_is_no_quota_at_all() -> None:
    """Neither a malformed cgroup file nor a zero period should size the pool by accident."""
    assert _divide("not-a-number", "100000") is None
    assert _divide("100000", "0") is None


def test_a_v1_quota_is_read_as_cores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The older cgroup layout splits the answer across two files."""
    (tmp_path / "quota").write_text("200000\n", encoding="utf-8")
    (tmp_path / "period").write_text("100000\n", encoding="utf-8")
    monkeypatch.setattr("script_lsp.pool.CGROUP_V2_QUOTA", str(tmp_path / "absent"))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_QUOTA", str(tmp_path / "quota"))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_PERIOD", str(tmp_path / "period"))

    assert cpu_quota() == pytest.approx(2.0)


def test_a_v1_quota_file_that_is_not_a_number_is_treated_as_unlimited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "quota").write_text("unlimited\n", encoding="utf-8")
    (tmp_path / "period").write_text("100000\n", encoding="utf-8")
    monkeypatch.setattr("script_lsp.pool.CGROUP_V2_QUOTA", str(tmp_path / "absent"))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_QUOTA", str(tmp_path / "quota"))
    monkeypatch.setattr("script_lsp.pool.CGROUP_V1_PERIOD", str(tmp_path / "period"))

    assert cpu_quota() is None
