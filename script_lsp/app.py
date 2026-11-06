"""The FastAPI app.

`/analyse` sits behind the warm-up gate on purpose: analysis is meaningless (and about 2 s slow) until
the framework is up, and the service says so with a 503 rather than take traffic and stall. The gate also
holds until the analysis worker pool exists - see `script_lsp.warmup` and `script_lsp.pool`.

Positions in anything this service returns are Monaco's - 1-based lines and 1-based columns counted in
UTF-16 code units - because converting once, here, under test, is the only way to keep them consistent.
See `script_lsp.positions`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from script_lsp.analyse import analyse
from script_lsp.warmup import Warmup

SERVICE_NAME = "mantid-python-script-lsp"

# One source of truth for the version: `pyproject.toml`, read back from the installed package. A second
# literal here is a thing to forget, and a released image advertising the wrong version is worse than one
# advertising that it was started from a bare checkout.


def service_version() -> str:
    """The installed version, or an honest marker when running from an uninstalled source tree."""
    try:
        return metadata.version(SERVICE_NAME)
    except metadata.PackageNotFoundError:  # a source tree with nothing installed, e.g. someone's first clone
        return "0.0.0+unknown"


SERVICE_VERSION = service_version()

health = APIRouter(tags=["health"])


@health.get("/healthz")
async def healthz() -> dict[str, str]:
    """Process-up only — deliberately independent of the framework, see `script_lsp.warmup`.

    Named like the rest of FIA (`../FIA-API/fia_api/routers/health.py`, and every probe in `../gitops`
    points at `/healthz`) so one probe definition works across the estate.
    """
    return {"status": "alive"}


@health.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """503 until the framework is warm and the workers have analysed, with the reason either way."""
    state: Any = request.app.state.warmup.state
    payload = {"ready": state.ready, "detail": state.detail, "warmup_seconds": state.seconds}
    return JSONResponse(payload, status_code=200 if state.ready else 503)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    warmup = Warmup()
    app.state.warmup = warmup
    warmup.start()
    yield
    warmup.shutdown()


app = FastAPI(
    title=SERVICE_NAME,
    description="Mantid-aware Python analysis for the FIA script editors",
    version=SERVICE_VERSION,
    lifespan=lifespan,
)
app.include_router(health)
app.include_router(analyse)
