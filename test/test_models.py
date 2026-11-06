"""Tests for the wire contract.

The point of these is not coverage of pydantic; it is that the guarantees the frontend is written
against - 1-based UTF-16 positions, a non-empty message, ordering, the cap, snake_case keys - hold,
because every one of them is something a user would otherwise experience as a squiggle in the wrong
place or a missing explanation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from script_lsp.models import (
    DEFAULT_DIAGNOSTIC_LIMIT,
    DEFAULT_MAX_SCRIPT_BYTES,
    DIAGNOSTIC_LIMIT_ENV,
    MAX_SCRIPT_BYTES,
    MAX_SCRIPT_BYTES_ENV,
    AnalyseRequest,
    AnalyseResponse,
    Diagnostic,
    _positive_int,
)


def diagnostic(**overrides: object) -> Diagnostic:
    """A plausible diagnostic, with fields overridable per test."""
    fields: dict[str, object] = {
        "message": "expected ':'",
        "severity": "error",
        "source": "compile",
        "start_line": 12,
        "start_column": 8,
        "end_line": 12,
        "end_column": 9,
    }
    fields.update(overrides)
    return Diagnostic(**fields)


def test_positions_are_one_based_because_monaco_says_so() -> None:
    """Python reports 0-based columns; a diagnostic that leaks that through sits off by one."""
    with pytest.raises(ValidationError, match="start_column"):
        diagnostic(start_column=0)
    with pytest.raises(ValidationError, match="start_line"):
        diagnostic(start_line=0)


def test_a_range_cannot_point_backwards() -> None:
    with pytest.raises(ValidationError, match="before start"):
        diagnostic(start_line=3, start_column=5, end_line=3, end_column=2)


def test_a_diagnostic_without_an_explanation_is_rejected() -> None:
    """jedi's text is often only 'invalid syntax', but *nothing* is worse than that."""
    with pytest.raises(ValidationError, match="string"):
        diagnostic(message="")


def test_the_size_cap_is_bytes_not_characters() -> None:
    """A 200_000-character script of 4-byte emoji is far over 200 KB, and must not be parsed."""
    over_cap = "\U0001f600" * (MAX_SCRIPT_BYTES // 4 + 1)

    with pytest.raises(ValidationError, match="over the"):
        AnalyseRequest(content=over_cap, content_hash="abc123")

    # The same count of characters under the cap in bytes is accepted: the check is size, not length.
    AnalyseRequest(content="\U0001f600" * 10, content_hash="abc123")


def test_a_missing_hash_is_an_error_not_an_empty_string() -> None:
    """Without a hash the caller cannot tell a stale reply from a current one."""
    with pytest.raises(ValidationError, match="content_hash"):
        AnalyseRequest(content="print(1)", content_hash="")


def test_build_orders_diagnostics_by_position() -> None:
    """The frontend maps these straight onto markers; unsorted input would flicker between saves."""
    response = AnalyseResponse.build(
        content_hash="h",
        parses=True,
        diagnostics=[
            diagnostic(start_line=20, start_column=1, end_line=20, end_column=2),
            diagnostic(start_line=3, start_column=10, end_line=3, end_column=11),
            diagnostic(start_line=3, start_column=2, end_line=3, end_column=3),
        ],
    )

    assert [(item.start_line, item.start_column) for item in response.diagnostics] == [(3, 2), (3, 10), (20, 1)]
    assert response.truncated is False


def test_build_applies_the_cap_and_says_so() -> None:
    """A bad dedent yields one error per line; the response must not grow with the file."""
    many = [diagnostic(start_line=line, end_line=line) for line in range(1, 251)]

    response = AnalyseResponse.build(content_hash="h", parses=False, diagnostics=many, limit=100)

    assert len(response.diagnostics) == 100
    assert response.truncated is True
    assert response.diagnostics[-1].start_line == 100


def test_json_field_names_are_snake_case() -> None:
    """The frontend's TS types mirror these names exactly; camelCase conversion happens nowhere."""
    payload = AnalyseResponse.build(content_hash="h", parses=False, diagnostics=[diagnostic()]).model_dump()

    assert set(payload) == {"content_hash", "parses", "diagnostics", "truncated"}
    assert set(payload["diagnostics"][0]) == {
        "message",
        "severity",
        "source",
        "start_line",
        "start_column",
        "end_line",
        "end_column",
    }


def test_parses_is_separate_from_the_diagnostic_count() -> None:
    """'Will the runner reject this' is not derivable from error counts, so the contract keeps it."""
    response = AnalyseResponse.build(content_hash="h", parses=False, diagnostics=[])

    assert response.model_dump() == {"content_hash": "h", "parses": False, "diagnostics": [], "truncated": False}


def test_info_is_not_a_severity_the_contract_offers() -> None:
    """Three severities would mean a third squiggle colour, and nothing here can justify one.

    `error` means the runner will not take the script and `warning` means a name is missing; an `info`
    would render as a grey underline for something the user cannot act on, in a service whose whole value
    depends on being believed.
    """
    with pytest.raises(ValidationError):
        diagnostic(severity="info")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_MAX_SCRIPT_BYTES),  # unset: the shipped default
        ("100000", 100000),
        ("", DEFAULT_MAX_SCRIPT_BYTES),  # set-but-empty is what an unset env var looks like from a manifest
        ("two hundred thousand", DEFAULT_MAX_SCRIPT_BYTES),
        ("0", DEFAULT_MAX_SCRIPT_BYTES),  # a cap of zero would reject every script, including an empty one
        ("-1", DEFAULT_MAX_SCRIPT_BYTES),
    ],
)
def test_a_setting_falls_back_to_the_shipped_default_rather_than_to_a_broken_pod(
    raw: str | None, expected: int
) -> None:
    """A typo in a Deployment must not produce a service that refuses everything.

    The cap has to be tunable - a pod with less memory should be able to lower it - but honouring a bad
    value fails worse than ignoring it.
    """
    environ = {} if raw is None else {MAX_SCRIPT_BYTES_ENV: raw}

    assert _positive_int(MAX_SCRIPT_BYTES_ENV, DEFAULT_MAX_SCRIPT_BYTES, environ) == expected


def test_the_diagnostic_cap_is_a_setting_with_the_same_fallback() -> None:
    """Both limits come from the environment, so both need the same protection against nonsense."""
    assert _positive_int(DIAGNOSTIC_LIMIT_ENV, DEFAULT_DIAGNOSTIC_LIMIT, {DIAGNOSTIC_LIMIT_ENV: "25"}) == 25
    assert _positive_int(DIAGNOSTIC_LIMIT_ENV, DEFAULT_DIAGNOSTIC_LIMIT, {DIAGNOSTIC_LIMIT_ENV: "lots"}) == (
        DEFAULT_DIAGNOSTIC_LIMIT
    )
