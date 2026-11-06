"""Framework warm-up, and the readiness state it gates.

`import mantid.simpleapi` costs roughly two seconds of CPU and a quarter of a gigabyte of RSS, so it
happens once per worker at startup rather than on the first analysis request, where it would just look
like the editor had hung. The analysis itself runs elsewhere - in `script_lsp.pool`, where it can
overlap - and needs nothing from Mantid but the name list generated here.

Readiness waits for it. Liveness must never wait for it: a node that warms slowly would have its pod
killed mid-warm-up and restart into the same race, which turns a slow start into a crash loop.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from script_lsp.pool import create_pool, warm
from script_lsp.preamble import Preamble, build

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WarmupState:
    """What warm-up decided, read by two callers and trusted by neither blindly.

    `/ready` renders it, and `/analyse` reads `ready` to decide whether to answer 503 rather than stall.
    `detail` is a sentence for whoever is reading pod logs: consumers branch on the status code, never on
    this text.
    """

    ready: bool = False
    detail: str = "cold"
    seconds: float | None = None


@dataclass(frozen=True)
class Framework:
    """What warm-up produces: a proven framework, and the names it exposes.

    They arrive together because analysis needs both. A warm-up that imported Mantid but could not
    enumerate its names would turn every algorithm call in every script into an "undefined name", so
    there is no useful service to run without the preamble - and no readiness without this object.
    """

    detail: str
    preamble: Preamble


def load_framework() -> (
    Framework
):  # pragma: no cover - needs the framework, so it runs in the image, not in a unit test
    """Import the analysis surface a reduction script sees, prove it works, and learn its names.

    The proof is a real algorithm rather than an attribute check: `CreateSampleWorkspace` needs the
    algorithm registry, the property system and the analysis data service all to be up, which is
    exactly what a script needs, and it costs about 8 ms with no input files.

    `AlgorithmManager` is deliberately not used for this. Mantid 7 exposes only
    `Instance/create/getAlgorithm/size/...` on it, `size()` returns 1 whatever is registered, and
    `create("Empty")` fails even while `simpleapi` can run well over a thousand algorithms — so it
    reports nothing usefully and a readiness check built on it would be theatre.
    """
    import mantid

    simpleapi = importlib.import_module("mantid.simpleapi")
    probe = "mantid-python-script-lsp-readiness"
    simpleapi.CreateSampleWorkspace(OutputWorkspace=probe)
    simpleapi.DeleteWorkspace(Workspace=probe)

    # Generated here, from the framework that just proved itself, rather than committed as a list that
    # could disagree with the base image this container was built from.
    names = build()

    algorithms = sum(1 for name in dir(simpleapi) if name[:1].isupper())
    detail = (
        f"Mantid {mantid.__version__}, {algorithms} algorithms, {names.name_count} names bound, "
        "probe workspace created and deleted"
    )
    return Framework(detail=detail, preamble=names)


def create_and_warm_pool(preamble: Preamble) -> ProcessPoolExecutor:
    """Start the workers and prove one analysis completes in each, before readiness says yes."""
    pool = create_pool(preamble)
    warm(pool)
    return pool


class Warmup:
    """Brings up what analysis needs, once, in a worker thread, so the loop keeps answering probes.

    That is two things and not one: the framework (proved by running an algorithm, and the source of the
    name list), and a pool of analysis workers. Both are readiness - a pod with a warm framework but no
    workers has nothing to serve with, and a pod with workers but no names would report every algorithm
    in the corpus as undefined.
    """

    def __init__(self) -> None:
        self.state = WarmupState()
        self.framework: Framework | None = None
        self.pool: ProcessPoolExecutor | None = None
        # Held so the task is not garbage collected mid-import; see asyncio docs on create_task.
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def shutdown(self) -> None:
        """Stop the workers. Without this, forkserver children outlive the app on a reload."""
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None

    async def _run(self) -> None:
        started = time.perf_counter()
        try:
            framework = await asyncio.to_thread(load_framework)
            pool = await asyncio.to_thread(create_and_warm_pool, framework.preamble)
        except Exception as exc:
            # Reported through readiness rather than propagating: a pod that cannot import Mantid is
            # not ready, and the exception text in the probe response is what makes that diagnosable.
            self.state = WarmupState(False, f"{type(exc).__name__}: {exc}", None)
            logger.exception("Mantid framework warm-up failed")
            return
        self.framework = framework
        self.pool = pool
        self.state = WarmupState(True, framework.detail, round(time.perf_counter() - started, 2))
        logger.info("Mantid framework warm in %s s (%s)", self.state.seconds, framework.detail)
