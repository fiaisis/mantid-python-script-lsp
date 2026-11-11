"""What does a Mantid runner image actually cost to run a script-checking service?

Run it inside the built image (see README) against a corpus from `make_corpus.py`. Each measurement
exists to decide something:

* warm-up (`import mantid` / `simpleapi` / `AlgorithmManager`) -> readiness probe budget, CPU
  request, and whether warm-up needs a startupProbe rather than a generous liveness timeout.
* per-request `compile()` / `ast.parse()` latency              -> the editor's debounce interval and
  the FIA-API upstream timeout.
* jedi first-call vs warm latency and RSS growth               -> whether completion can share the
  analyse worker pool, or needs its own warm subprocess.
* RSS per stage, and the cost of `fork()` when warm            -> memory request, and whether N
  workers cost N x warm-up (spawn) or ~1 x warm-up plus copy-on-write (fork).

Set SPIKE_SCRIPTS to the corpus directory and SPIKE_REPEATS to change the sampling count.
"""

from __future__ import annotations

import contextlib
import gc
import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS = Path(os.environ.get("SPIKE_SCRIPTS", "/spike/corpus"))
REPEATS = int(os.environ.get("SPIKE_REPEATS", "15"))

rows: list[dict[str, Any]] = []


def rss_mb() -> float:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024.0
    return float("nan")


def measure(name: str, fn: Any, note: str = "") -> Any:
    """Time one call of `fn` and record wall time plus resident set size afterwards."""
    gc.collect()
    started = time.perf_counter()
    value = fn()
    rows.append(
        {
            "name": name,
            "wall_ms": round((time.perf_counter() - started) * 1000, 1),
            "rss_mb": round(rss_mb(), 1),
            "note": note,
        }
    )
    # Printed as we go: an import blowing up later must not take the measurements with it.
    print(f"::stage {rows[-1]}", flush=True)
    return value


def latency_over_corpus() -> dict[str, Any]:
    """Per-script compile() and ast.parse() latency, sampled warm, as median/p95/max in ms."""
    import ast  # deferred: this runs before mantid is imported, which is the point of the measurement

    results: dict[str, Any] = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        # Bound as defaults so each lambda samples the file it was created for; the loop variables
        # would otherwise be read at call time, and a measurement of the wrong file looks plausible.
        per_function = {
            "compile": _sample(lambda source=source, name=path.name: compile(source, name, "exec")),
            "ast.parse": _sample(lambda source=source, name=path.name: ast.parse(source, name, "exec")),
        }
        results[path.name] = {
            "bytes": len(source.encode()),
            "lines": len(source.splitlines()),
            **per_function,
        }
    return results


def _sample(fn: Any, repeats: int = REPEATS) -> dict[str, float]:
    timings = []
    for _ in range(repeats):
        started = time.perf_counter()
        with contextlib.suppress(SyntaxError):
            fn()  # the deliberately broken corpus entry is still a valid latency sample
        timings.append((time.perf_counter() - started) * 1000)
    ordered = sorted(timings)
    return {
        "median_ms": round(statistics.median(timings), 3),
        "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1], 3),
        "max_ms": round(max(timings), 3),
    }


def jedi_probe() -> dict[str, Any]:
    """jedi's first call in a process costs far more than later ones; measure both."""
    try:
        import jedi
    except ImportError:
        return {"available": False}

    source = (SCRIPTS / sorted(SCRIPTS.glob("*.py"))[0].name).read_text(encoding="utf-8")
    # Complete at the end of an `import x as y` line: cheap to find, and the shape a user reaches for
    # when they want to know what a Mantid object gives them.
    line, col = 1, 1
    for index, text in enumerate(source.splitlines(), start=1):
        stripped = text.strip()
        if stripped.startswith("import ") and " as " in stripped:
            line, col = index, len(stripped)
            break

    started = time.perf_counter()
    script = jedi.Script(code=source)
    completions = script.complete(line, col)
    first_ms = (time.perf_counter() - started) * 1000
    rss_after_first = rss_mb()

    warm = [
        _sample(lambda offset=offset: script.complete(line, max(1, col - offset)))["median_ms"] for offset in range(6)
    ]

    started = time.perf_counter()
    script.infer(line, col)
    return {
        "available": True,
        "jedi_version": getattr(jedi, "__version__", "?"),
        "first_complete_ms": round(first_ms, 1),
        "warm_complete_median_ms": round(statistics.median(warm), 2),
        "warm_complete_max_ms": round(max(warm), 2),
        "first_infer_ms": round((time.perf_counter() - started) * 1000, 1),
        "completions": len(completions),
        "rss_after_first_mb": round(rss_after_first, 1),
    }


def fork_cost() -> dict[str, Any]:
    """What adding another worker costs once warm (uvicorn --workers / gunicorn fork on POSIX)."""
    read_fd, write_fd = os.pipe()
    started = time.perf_counter()
    pid = os.fork()
    fork_ms = (time.perf_counter() - started) * 1000

    if pid == 0:
        os.close(read_fd)
        with os.fdopen(write_fd, "w") as pipe:
            pipe.write(str(rss_mb()))
        os._exit(0)  # do not run atexit handlers in the probe child

    os.close(write_fd)
    with os.fdopen(read_fd) as pipe:
        child_rss = float(pipe.read() or "nan")
    os.waitpid(pid, 0)

    parent_rss = rss_mb()
    return {
        "fork_ms": round(fork_ms, 2),
        "parent_rss_mb": round(parent_rss, 1),
        "child_rss_mb": round(child_rss, 1),
        "child_extra_rss_mb": round(child_rss - parent_rss, 1),
    }


def init_workflow_manager() -> str:
    """The explicit framework warm-up older reduction scripts call, wherever this Mantid keeps it."""
    import importlib

    for module_name in ("mantid.api", "mantid.kernel", "mantid.utils"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        manager = getattr(module, "WorkflowManager", None)
        if manager is not None:
            manager().initialize()
            return f"{module_name}.WorkflowManager().initialize()"
    return "not found: simpleapi already initialised the framework"


print(f"::env python={sys.version.split()[0]} exe={sys.executable} scripts={SCRIPTS}", flush=True)
rows.append({"name": "python baseline", "wall_ms": 0.0, "rss_mb": round(rss_mb(), 1), "note": "before any import"})

# 1. The floor: syntax checking with no Mantid in the picture at all.
syntax_before = measure("syntax pass (before mantid)", latency_over_corpus, "compile+ast.parse over the corpus")

# 2. Warm-up: what the readiness gate is protecting.
measure("import mantid", lambda: __import__("mantid"))
measure("from mantid.simpleapi import *", lambda: __import__("mantid.simpleapi", fromlist=["*"]))
measure(
    "AlgorithmManager.Instance()",
    lambda: __import__("mantid.api", fromlist=["AlgorithmManager"]).AlgorithmManager.Instance().size(),
    "registers every algorithm",
)
measure("WorkflowManager().initialize()", init_workflow_manager, "explicit warm-up some scripts still call")

# 3. Per-request cost once warm: the numbers behind the debounce and the upstream timeout.
syntax_warm = measure("syntax pass (warm)", latency_over_corpus, "same corpus after warm-up")

jedi_result = jedi_probe()
fork_result = fork_cost() if "SPIKE_NO_FORK" not in os.environ else {"skipped": True}

json.dump(
    {
        "stages": rows,
        "syntax_before_mantid": syntax_before,
        "syntax_warm": syntax_warm,
        "jedi": jedi_result,
        "fork": fork_result,
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
        "cpu_count": os.cpu_count(),
        "load_avg": os.getloadavg(),
    },
    sys.stdout,
    indent=2,
)
print()
