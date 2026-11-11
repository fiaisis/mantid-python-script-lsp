"""`POST /analyse` over HTTP, including the parts only visible at the boundary.

Unit tests prove the analysis is right; these prove the service hands it over correctly - field names,
status codes, and a position that has survived JSON, a proxy and a Unicode string without moving.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from script_lsp.models import MAX_SCRIPT_BYTES

CLEAN = 'from mantid.simpleapi import *\n\ndef reduce_run(runno):\n    return Load(Filename=f"ISIS_{runno}.nxs")\n'
BROKEN = "def reduce_run(runno)\n    return runno\n"


def analyse(client: TestClient, content: str, content_hash: str = "sha256:abc") -> dict:
    response = client.post("/analyse", json={"content": content, "content_hash": content_hash})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_clean_script_comes_back_clean(warm_client: TestClient) -> None:
    body = analyse(warm_client, CLEAN)

    assert body["parses"] is True
    assert body["diagnostics"] == []
    assert body["truncated"] is False


def test_the_hash_is_echoed_so_a_stale_reply_can_be_discarded(warm_client: TestClient) -> None:
    """The caller's identifier for the script version is the only way to spot an out-of-order reply."""
    assert analyse(warm_client, BROKEN, "sha256:deadbeef")["content_hash"] == "sha256:deadbeef"


def test_the_verdict_and_a_position_survive_the_wire(warm_client: TestClient) -> None:
    body = analyse(warm_client, BROKEN)

    assert body["parses"] is False
    assert body["diagnostics"][0]["source"] == "compile"
    assert body["diagnostics"][0]["start_line"] == 1
    assert body["diagnostics"][0]["start_column"] == 22


def test_a_non_ascii_line_arrives_at_the_same_column_it_left_in(warm_client: TestClient) -> None:
    """The whole point of converting server-side: the editor must draw this without arithmetic.

    `class` starts after 19 UTF-16 code units - 10 for `comment = `, a quote, two emoji counting as two
    units each, a quote, and ` + ` - so column 20, and five columns later at 25. Character counting
    would have said 18 and 23.
    """
    body = analyse(warm_client, 'comment = "\U0001f600\U0001f600" + class\n')

    diagnostic = body["diagnostics"][0]
    assert (diagnostic["start_column"], diagnostic["end_column"]) == (20, 25)


def test_the_response_uses_the_field_names_the_frontend_declares(warm_client: TestClient) -> None:
    """FIA-API forwards this body unchanged, so these keys are the frontend's interface."""
    body = analyse(warm_client, BROKEN)

    assert set(body) == {"content_hash", "parses", "diagnostics", "truncated"}
    assert set(body["diagnostics"][0]) == {
        "message",
        "severity",
        "source",
        "start_line",
        "start_column",
        "end_line",
        "end_column",
    }


def test_an_oversized_script_is_refused_before_anything_parses_it(warm_client: TestClient) -> None:
    """The cap exists so one user cannot buy CPU with a paste; rejection is 422, not a slow 200."""
    response = warm_client.post(
        "/analyse",
        json={"content": "x = 1\n" * (MAX_SCRIPT_BYTES // 6 + 1), "content_hash": "sha256:big"},
    )

    assert response.status_code == 422
    assert "over the" in response.text


def test_a_misspelled_algorithm_is_reported_without_claiming_the_script_is_broken(warm_client: TestClient) -> None:
    """The two passes in one response: it parses, and one name is not in the framework."""
    body = analyse(warm_client, "from mantid.simpleapi import *\n\nws = Lod(Filename='x.nxs')\n")

    assert body["parses"] is True
    assert [(item["source"], item["severity"]) for item in body["diagnostics"]] == [("pyflakes", "warning")]
    assert body["diagnostics"][0]["message"] == "undefined name 'Lod'"
    assert body["diagnostics"][0]["start_line"] == 3
    assert body["diagnostics"][0]["start_column"] == 6


def test_no_warnings_are_offered_for_code_that_does_not_parse(warm_client: TestClient) -> None:
    """Half-typed syntax produces an undefined name for every name in the file; one true error is enough."""
    body = analyse(warm_client, "def reduce_run(runno:\n    return nowhere_to_be_found(\n")

    assert body["parses"] is False
    assert {item["source"] for item in body["diagnostics"]} == {"compile"}


def test_a_request_without_a_hash_is_refused(warm_client: TestClient) -> None:
    """No hash means the caller cannot tell a stale reply from a current one, so it is not accepted."""
    assert warm_client.post("/analyse", json={"content": CLEAN}).status_code == 422


def test_analysis_is_refused_until_the_framework_is_warm(cold_client: TestClient) -> None:
    """A 200 here claims the script is clean; a twenty-second-old pod is not entitled to make it."""
    response = cold_client.post("/analyse", json={"content": CLEAN, "content_hash": "sha256:abc"})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason"] == "warming_up"
    assert "mantid is not installed" in detail["detail"]
