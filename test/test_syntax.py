"""Syntax diagnostic tests, against the mistakes users actually make.

The sources are taken from the measurements in `spike/` and from the real reduction scripts in the corpus:
missing colons, unclosed brackets in long algorithm argument lists, `print` left over from Python 2,
and over-indented lines. The numbers in the assertions are measured, not derived - which is the point
of writing them down.
"""

from __future__ import annotations

import jedi
import pytest

from script_lsp.models import AnalyseResponse, Diagnostic
from script_lsp.syntax import _fragment_message, syntax_diagnostics

# The shape of a real reduction script's opening, with the facility specifics removed. If this reports
# anything, the feature will tell a working scientist that their working script is broken.
WORKING = """\
from mantid.simpleapi import *
import matplotlib.pyplot as plt
import numpy as np

def reduce_run(runno, grp, use_opt=False, riavgs=5):
    config.setAutomaticFileSearch("{'/path/to/instrument/config/'}")
    ws = Load(Filename=f"{runno}.nxs", GroupName=grp)
    return ws
"""

# An unclosed bracket five lines into an argument list - how a real reduction script actually broke.
UNCLOSED_BRACKET = """\
def reduce_run(runno):
    ws = ScaleX(
        InputWorkspace="mantiw",
        XMin=2.3038,
        XMax=5.0
    return ws
"""

# Two independent mistakes: a missing colon, and an unterminated string on the next line. CPython stops
# at the string; the missing colon is only visible to an error-recovering parser.
COLON_AND_STRING = "def reduce_run(runno)\n    ws = Mtd('a\n    return ws\n"


def test_a_working_script_is_left_alone() -> None:
    parses, diagnostics = syntax_diagnostics(WORKING)

    assert parses is True
    assert diagnostics == []


def test_a_script_that_will_run_is_not_reported_because_jedi_disagrees(monkeypatch: pytest.MonkeyPatch) -> None:
    """jedi's parser is not the runner's. When CPython accepts the file, nothing may call it broken."""

    def explode(*args: object, **kwargs: object) -> None:
        msg = "jedi is having a bad day"
        raise RuntimeError(msg)

    monkeypatch.setattr(jedi, "Script", explode)

    parses, diagnostics = syntax_diagnostics(WORKING)

    assert parses is True
    assert diagnostics == []


def test_a_broken_script_still_gets_cpythons_verdict_when_jedi_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """jedi is a bonus. Its failure must not take the authoritative verdict down with it."""

    def explode(*args: object, **kwargs: object) -> None:
        msg = "jedi is having a bad day"
        raise RuntimeError(msg)

    monkeypatch.setattr(jedi, "Script", explode)

    parses, diagnostics = syntax_diagnostics(UNCLOSED_BRACKET)

    assert parses is False
    assert [item.source for item in diagnostics] == ["compile"]


def test_a_missing_colon_is_reported_once_at_the_end_of_the_line() -> None:
    """One mistake, one squiggle: jedi's knock-on "unexpected indent" on line 2 must not be shown."""
    parses, diagnostics = syntax_diagnostics("def reduce_run(runno)\n    return runno\n")

    assert parses is False
    assert [(item.source, item.start_line, item.start_column) for item in diagnostics] == [("compile", 1, 22)]
    assert ":" in diagnostics[0].message


def test_an_unclosed_bracket_is_reported_at_the_bracket() -> None:
    """Python 3.12+ finds this precisely, so the useful position needs no help from jedi."""
    parses, diagnostics = syntax_diagnostics(UNCLOSED_BRACKET)

    assert parses is False
    assert diagnostics[0].message == "'(' was never closed"
    assert (diagnostics[0].start_line, diagnostics[0].start_column) == (2, 16)


def test_an_error_before_the_one_cpython_stopped_at_is_recovered() -> None:
    """`compile()` gives up at the string on line 2; the missing colon on line 1 is the one to fix first."""
    parses, diagnostics = syntax_diagnostics(COLON_AND_STRING)

    assert parses is False
    recovered = [item for item in diagnostics if item.source == "jedi"]
    assert [(item.start_line, item.message) for item in recovered] == [(1, "expected ':'")]


def test_a_recovered_error_is_ordered_with_the_others_by_the_response() -> None:
    """`syntax_diagnostics` returns CPython's verdict first; the wire format is ordered by position."""
    _, diagnostics = syntax_diagnostics(COLON_AND_STRING)

    ordered = AnalyseResponse.build(content_hash="h", parses=False, diagnostics=diagnostics).diagnostics

    assert [(item.start_line, item.source) for item in ordered] == [(1, "jedi"), (2, "compile")]


def test_a_fragment_is_not_allowed_to_invent_a_context_error() -> None:
    """A single extracted line has no enclosing function, so it appears to contain a bare `return`.

    Reporting "'return' outside function" about a line inside `reduce_run` would be a confident lie,
    and users do not forgive those.
    """
    _, diagnostics = syntax_diagnostics(COLON_AND_STRING)

    assert all("'return' outside function" not in item.message for item in diagnostics)


def test_an_over_indented_line_is_reported_where_it_is() -> None:
    source = "def reduce_run(runno):\n    ws = runno\n     ws = ws + 1\n    return ws\n"

    parses, diagnostics = syntax_diagnostics(source)

    assert parses is False
    assert diagnostics[0].start_line == 3
    assert "indent" in diagnostics[0].message.lower()


def test_python_2_print_is_reported_in_plain_words() -> None:
    parses, diagnostics = syntax_diagnostics('import numpy as np\nprint "reduced"\n')

    assert parses is False
    assert diagnostics[0].start_line == 2
    assert "Missing parentheses" in diagnostics[0].message


def test_a_null_byte_is_a_diagnostic_rather_than_a_500() -> None:
    """`compile()` raises ValueError for this; an advisory feature must not fall over on it."""
    parses, diagnostics = syntax_diagnostics("x = 1\x00\n")

    assert parses is False
    assert diagnostics[0].start_line == 1
    assert "null" in diagnostics[0].message


def test_everything_returned_is_drawable() -> None:
    """1-based, non-empty, never inverted - the model enforces these, and a violation means a bug here."""
    _, diagnostics = syntax_diagnostics("def f(:\n  x = [1, 2\n  print 'x'\n")

    assert diagnostics
    for item in diagnostics:
        assert isinstance(item, Diagnostic)
        assert item.start_line >= 1
        assert item.start_column >= 1
        assert (item.end_line, item.end_column) > (item.start_line, item.start_column)
        assert item.message


def test_a_script_cpython_refuses_to_look_at_becomes_a_diagnostic_not_a_crash() -> None:
    """A lone surrogate in the buffer makes `compile()` raise UnicodeEncodeError, not SyntaxError.

    Measured on the interpreter that ships: it is a `ValueError`, so it is caught here and turned into one
    diagnostic rather than a 500 for a script the user can see is fine. (A NUL byte, checked at the same
    time, is a plain SyntaxError since CPython 3.12 and needs nothing special.)
    """
    parses, diagnostics = syntax_diagnostics('ws = Load(Filename="\ud800")\n')

    assert parses is False
    assert diagnostics[0].source == "compile"
    assert "UnicodeEncodeError" in diagnostics[0].message


def test_a_null_byte_is_reported_where_the_user_can_find_it() -> None:
    """Pinned because the exception type for this input changed under us once already."""
    parses, diagnostics = syntax_diagnostics("ws = Load(Filename='x.nxs')\n\x00\n")

    assert parses is False
    assert diagnostics[0].message == "source code string cannot contain null bytes"
    assert (diagnostics[0].start_line, diagnostics[0].start_column) == (1, 1)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("if True:", None),  # "expected an indented block" describes the cut, not the script
        ("x = 1", None),  # nothing to report about it
        ("Load(", "'(' was never closed"),  # specific, true of the line, and better than "invalid syntax"
    ],
)
def test_a_borrowed_fragment_only_lends_a_message_when_that_message_is_about_the_script(
    line: str, expected: str | None
) -> None:
    """Recovering a good message means compiling a slice of the file, and a slice lies.

    Out of its block, a fragment looks like it is missing a body, or like a `return` escaped a function.
    Those are artefacts of the technique, so they are refused and jedi's vaguer-but-true message is kept.
    """
    assert _fragment_message(f"{line}\n", (1, 1, 1, len(line) + 1)) == expected


def test_a_fragment_too_pathological_to_compile_offers_no_message_and_no_exception() -> None:
    """The fallback for the fallback: whatever went wrong here, the caller still gets jedi's message.

    A surrogate escapes `except SyntaxError` and would otherwise leave this function without a return
    value on the path that matters most.
    """
    assert _fragment_message('x = "\ud800"\n', (1, 1, 1, 6)) is None
