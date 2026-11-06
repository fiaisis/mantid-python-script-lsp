"""The app object's own surface: what it reports about itself.

Small enough to state once and pin: the version in the OpenAPI document is the version that was built,
and a checkout with nothing installed still starts rather than failing on import.
"""

from __future__ import annotations

import importlib.metadata

import pytest

from script_lsp import app as app_module


def test_the_version_comes_from_the_package_metadata_not_a_second_literal() -> None:
    """Two places to remember is one too many, and the drift is invisible until a release is wrong."""
    assert importlib.metadata.version(app_module.SERVICE_NAME) == app_module.SERVICE_VERSION


def test_an_uninstalled_source_tree_reports_that_instead_of_failing_to_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Someone's first clone has the repo and no installed package; that must still boot."""

    def not_installed(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(app_module.SERVICE_NAME)

    monkeypatch.setattr(app_module.metadata, "version", not_installed)

    assert app_module.service_version() == "0.0.0+unknown"
