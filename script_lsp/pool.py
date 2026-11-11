"""Running the analysis where it can actually overlap.

Measured: 24 concurrent analyses on Starlette's threadpool take 1,534 ms each, which
is 4.6x worse than running the same 24 one after another. `ast.parse` and pyflakes never release the GIL,
so threads thrash it instead of overlapping. Eight worker processes do the same work at 247 analyses per
second - 18x the threadpool.

Workers start without importing Mantid, because the analysis needs nothing from the framework at request
time: the names arrive as a 90-line string from warm-up. That is what makes a process pool affordable
here — tens of megabytes per worker rather than the quarter-gigabyte the parent pays.

Two details that bite if ignored:

* **forkserver, not fork.** The parent is a threaded asyncio process, and forking one is how you inherit
  a lock that nobody will ever release again. forkserver forks children from a clean server process.
* **`os.cpu_count()` is a liar in a container** - it reports the host's cores, not the pod's cgroup
  quota. Worker count comes from the quota, because a pod with a 1 CPU limit and 8 workers gets CFS
  throttled in 100 ms bursts, which turns a latency problem into a p95 problem.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Final

from script_lsp.lint import lint_diagnostics
from script_lsp.models import Diagnostic
from script_lsp.preamble import Preamble
from script_lsp.syntax import syntax_diagnostics

logger = logging.getLogger(__name__)

WORKERS_ENV: Final = "ANALYSIS_WORKERS"
WORKER_STARTUP_TIMEOUT_SECONDS: Final = 120
CGROUP_V2_QUOTA: Final = "/sys/fs/cgroup/cpu.max"
CGROUP_V1_QUOTA: Final = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
CGROUP_V1_PERIOD: Final = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"

# Set in each worker by the pool initializer. The analysis must not depend on it being anything
# particular - an empty preamble means "no framework names bound", which the lint pass handles by
# reporting every framework name, so the warm-up gate is what stops that reaching a user.
_BOUND_PREAMBLE: Preamble | None = None


def analyse_script(content: str) -> tuple[bool, list[Diagnostic]]:
    """Both passes, in whatever process this runs in. Module level, because it has to be picklable."""
    parses, diagnostics = syntax_diagnostics(content)
    if parses and _BOUND_PREAMBLE is not None:
        diagnostics = [*diagnostics, *lint_diagnostics(content, _BOUND_PREAMBLE)]
    return parses, diagnostics


def bind_preamble(preamble: Preamble) -> None:
    """Pool initializer: give a worker the names before it analyses anything."""
    global _BOUND_PREAMBLE  # noqa: PLW0603 - this is what an initializer exists for
    _BOUND_PREAMBLE = preamble
    # forkserver children are forked from a clean server process, which never inherited uvicorn's logging
    # configuration; without this, a worker's warnings - including "pyflakes failed" - go to /dev/null.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info("analysis worker %s bound to %s framework names", os.getpid(), preamble.name_count)


def _first_line(path: Path) -> str | None:
    """Read a sysfs/cgroup file, or None. These files report a size of 0, so read rather than stat."""
    try:
        return path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None


def _divide(numerator: str, denominator: str) -> float | None:
    try:
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None


def cpu_quota() -> float | None:
    """How many cores this container is allowed, from the cgroup, or None if unlimited.

    Reads cgroup v2 and falls back to v1; either can be absent depending on how the node is mounted, and
    "absent" is a legitimate answer (unlimited) rather than an error - the same answer a laptop gives.
    """
    v2 = _first_line(Path(CGROUP_V2_QUOTA))
    if v2 is not None:
        fields = v2.split()
        if len(fields) != 2:  # noqa: PLR2004 - the documented format is exactly "quota period"
            return None
        quota, period = fields
        return None if quota == "max" else _divide(quota, period)

    v1_quota = _first_line(Path(CGROUP_V1_QUOTA))
    v1_period = _first_line(Path(CGROUP_V1_PERIOD))
    if v1_quota is None or v1_period is None:
        return None
    try:
        allowed = float(v1_quota)
    except ValueError:
        return None
    if allowed < 0:
        return None  # v1 spells "unlimited" as a quota of -1
    return _divide(v1_quota, v1_period)


def worker_count(environ: Mapping[str, str] | None = None, quota: float | None = None) -> int:
    """How many analysis workers to run.

    One per allowed core, rounded up: more than that gets throttled against the pod's own quota, which
    makes latency worse rather than better. `ANALYSIS_WORKERS` overrides for a pod whose limits the
    deployment knows better than the cgroup does.
    """
    environment = os.environ if environ is None else environ
    override = environment.get(WORKERS_ENV)
    if override:
        try:
            requested = int(override)
        except ValueError:
            logger.warning("%s=%r is not a number; using the CPU quota", WORKERS_ENV, override)
        else:
            if requested < 1:
                logger.warning("%s=%s is below 1; using the CPU quota", WORKERS_ENV, requested)
            else:
                return requested

    if quota is not None:
        return max(1, round(quota))
    return max(1, min(4, os.process_cpu_count() or 1))


def create_pool(preamble: Preamble) -> ProcessPoolExecutor:
    """A process pool whose workers already know the framework's names."""
    workers = worker_count(quota=cpu_quota())
    context = multiprocessing.get_context("forkserver")
    pool = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=context,
        initializer=bind_preamble,
        initargs=(preamble,),
    )
    logger.info("analysis pool started with %s worker(s) for %s names", workers, preamble.name_count)
    return pool


def warm(pool: ProcessPoolExecutor) -> None:
    """Force every worker to start, and to import what analysis needs, before any request arrives.

    `ProcessPoolExecutor` spawns workers on demand, and each one's first analysis pays for importing
    jedi and pyflakes - measured at ~0.2 s each, which lands as a 670 ms median on the first burst of 24
    requests and then vanishes. That is the same mistake as warming nothing at all, and readiness is the
    place that stops it: a pod is not ready until an analysis has actually completed in every worker.
    """
    workers = getattr(pool, "_max_workers", 1)
    probes = [pool.submit(analyse_script, "x = 1\n") for _ in range(workers)]
    for probe in probes:
        probe.result(timeout=WORKER_STARTUP_TIMEOUT_SECONDS)
    logger.info("analysis pool warmed: %s worker(s) completed a probe analysis", workers)


async def analyse_in_pool(pool: ProcessPoolExecutor, content: str) -> tuple[bool, list[Diagnostic]]:
    """Run one analysis off the event loop, in a worker process.

    Exceptions travel back with the future on purpose: a worker that dies on a pathological script should
    surface as a 500 the service logs, not as a silently empty diagnostic list.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, analyse_script, content)
