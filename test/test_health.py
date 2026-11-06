"""What the kubelet's probes see.

Mantid is not installed on a dev machine or in CI's plain Python job, so `load_framework` is replaced in
every test: these tests are about the gate, not about Mantid. The replacement is the shared
`stub_framework` fixture, so the object a warm framework returns is the same shape everywhere.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from script_lsp import warmup as warmup_module
from script_lsp.app import app
from script_lsp.warmup import Framework


def _wait_until(client: TestClient, predicate: Callable[[dict], bool], timeout: float = 5.0) -> dict:
    """Poll readiness until `predicate` holds, then return the last body seen."""
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get("/ready")
        body = {**response.json(), "status_code": response.status_code}
        if predicate(body):
            return body
        time.sleep(0.02)
    return body


def _explode() -> str:
    msg = "mantid is not installed"
    raise RuntimeError(msg)


def test_liveness_is_up_before_the_framework_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule that keeps a slow warm-up from becoming a crash loop."""
    monkeypatch.setattr(warmup_module, "load_framework", _explode)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_the_probe_paths_are_the_names_every_other_fia_deployment_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming is not a detail: the probe definitions in `../gitops` are shared across services.

    Every FIA component answers `/healthz` for liveness and `/ready` for readiness
    (`../FIA-API/fia_api/routers/health.py`), so this pins the paths rather than leaving a rename to be
    discovered by a 503 in staging.
    """
    monkeypatch.setattr(warmup_module, "load_framework", _explode)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.get("/health/live").status_code == 404
        assert client.get("/health/ready").status_code == 404


def test_readiness_goes_green_once_the_framework_is_warm(
    monkeypatch: pytest.MonkeyPatch, stub_framework: Framework
) -> None:
    monkeypatch.setattr(warmup_module, "load_framework", lambda: stub_framework)

    with TestClient(app) as client:
        body = _wait_until(client, lambda payload: payload["status_code"] == 200)

    assert body["status_code"] == 200, body
    assert body["detail"] == "3 algorithms registered"
    assert body["warmup_seconds"] is not None


def test_readiness_reports_a_failed_warmup_instead_of_hanging_red(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(warmup_module, "load_framework", _explode)

    with TestClient(app) as client:
        body = _wait_until(client, lambda payload: "RuntimeError" in payload["detail"])

    assert body["status_code"] == 503, body
    assert "mantid is not installed" in body["detail"]
