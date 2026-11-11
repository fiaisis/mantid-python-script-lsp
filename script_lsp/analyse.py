"""`POST /analyse` - one script in, its diagnostics out.

Two passes answer different questions and both are reported: `compile()` says whether the runner would
accept the file at all, and pyflakes - over a copy carrying the framework's real names - says whether a
name exists in the Mantid this service runs. See `script_lsp.syntax` and `script_lsp.lint`.

The handler is `async` and hands the work to a process pool, not a thread. An earlier version was a
plain `def` handler - "keep CPU work off the event loop" - and measurement says that was the wrong
instinct: 24 concurrent analyses on the threadpool cost 1,534 ms each, which is 4.6x worse than running
those 24 one at a time, because `ast.parse` and pyflakes never release the GIL. Worker processes give
18x the threadpool. See `script_lsp.pool`.

Status codes are part of the contract the frontend branches on, so they are stated rather than left to
chance:

| code | meaning | what the caller should do |
| --- | --- | --- |
| 200 | analysed; `parses` and `diagnostics` describe the script | draw them |
| 422 | request rejected - over the size cap, or no `content_hash` | stop, this is a bug in the caller |
| 503 | framework still warming up | retry shortly; keep the previous diagnostics |

FIA-API forwards the status and the body unchanged, so a `503` here is what stops the frontend from
treating a cold pod as "this script has no errors".
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

from fastapi import APIRouter, HTTPException, Request, status

from script_lsp.models import AnalyseRequest, AnalyseResponse
from script_lsp.pool import analyse_in_pool
from script_lsp.warmup import Warmup

analyse = APIRouter(tags=["analyse"])


@analyse.post("/analyse", response_model=AnalyseResponse)
async def analyse_script(body: AnalyseRequest, request: Request) -> AnalyseResponse:
    """Diagnostics for a script, advisory by construction: nothing here can stop a save.

    Syntax is measured on the script exactly as typed; lint on the analysed copy, whose positions are
    already mapped back. Lint only runs when the script parses - a half-typed line produces undefined
    names by the hundred, and the syntax pass is already saying the useful thing about it.
    """
    pool = _analysis_pool(request)

    parses, diagnostics = await analyse_in_pool(pool, body.content)

    return AnalyseResponse.build(
        content_hash=body.content_hash,
        parses=parses,
        diagnostics=diagnostics,
    )


def _analysis_pool(request: Request) -> ProcessPoolExecutor:
    """The worker pool, refusing analysis until it exists - with a reason the caller can log.

    Syntax analysis would technically work before warm-up finishes, and answering anyway is the worse
    option: a client cannot tell "the script is clean" from "the pod is twenty seconds old", and the
    first of those is a claim the editor should only make when it is true. Lint needs the warm framework
    outright - without its names bound, every algorithm call in the script reads as undefined.
    """
    warmup: Warmup = request.app.state.warmup
    if warmup.state.ready and warmup.pool is not None:
        return warmup.pool
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "reason": "warming_up",
            "detail": warmup.state.detail,
            "warmup_seconds": warmup.state.seconds,
        },
    )
