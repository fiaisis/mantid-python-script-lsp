"""Shared fixtures.

Mantid is not installed on a dev machine or in CI's plain Python job, so the framework load is
replaced before the app starts in every test that needs one. What is under test is the service, not
the import of a 2 GB framework.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from script_lsp import warmup as warmup_module
from script_lsp.app import app
from script_lsp.preamble import Preamble
from script_lsp.warmup import Framework

# One analysis worker per test app rather than one per core. Correctness across the process boundary is
# the same either way, and each child pays an import cost that would otherwise put seconds on every test
# that starts an app. Pool sizing has its own tests in test_pool.py. Also set for CI, where the number of
# cores belongs to the runner rather than the test.
os.environ.setdefault("ANALYSIS_WORKERS", "1")

WARM_TIMEOUT_SECONDS = 5.0

# What the stub framework "exposes". Scripts in tests are written against these: the real service binds
# roughly 1,800 names from the live framework, and the point of the stub is that a test states which
# names exist rather than depending on a 2 GB package being installed.
STUB_FRAMEWORK_NAMES = ("Load", "CreateSampleWorkspace", "AnalysisDataService", "MergeMD", "mtd", "np")


@pytest.fixture
def stub_framework() -> Framework:
    """A `Framework` as `load_framework` would return it, with no Mantid involved."""
    return Framework(detail="3 algorithms registered", preamble=Preamble.from_names(STUB_FRAMEWORK_NAMES))


def _poll_until_ready(client: TestClient, timeout: float = WARM_TIMEOUT_SECONDS) -> str:
    """Wait for the warm-up thread to finish, returning the last readiness detail."""
    deadline = time.monotonic() + timeout
    detail = ""
    while time.monotonic() < deadline:
        response = client.get("/ready")
        detail = str(response.json().get("detail", ""))
        if response.status_code == 200:
            return detail
        time.sleep(0.02)
    return detail


@pytest.fixture
def warm_client(monkeypatch: pytest.MonkeyPatch, stub_framework: Framework) -> Iterator[TestClient]:
    """A started app whose warm-up succeeded.

    The assertion after polling is deliberate: a fixture that quietly yields a cold client turns every
    downstream test into a confusing 503 failure.
    """
    monkeypatch.setattr(warmup_module, "load_framework", lambda: stub_framework)

    with TestClient(app) as client:
        detail = _poll_until_ready(client)
        assert client.get("/ready").status_code == 200, f"warm-up never finished: {detail}"
        yield client


@pytest.fixture
def cold_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A started app whose warm-up is still failing, i.e. what a pod looks like before it is ready."""
    monkeypatch.setattr(warmup_module, "load_framework", _fail)

    with TestClient(app) as client:
        yield client


def _fail() -> str:
    msg = "mantid is not installed"
    raise RuntimeError(msg)
