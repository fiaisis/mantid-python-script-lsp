"""The lint pass: one message type, in the user's coordinates.

These tests use a preamble built from a handful of names rather than the framework's ~1,800, which is
the point: the test states what the framework provides, and everything else follows.
"""

from __future__ import annotations

import pytest

from script_lsp import lint as lint_module
from script_lsp.lint import lint_diagnostics
from script_lsp.positions import utf16_length
from script_lsp.preamble import Preamble

# The framework, as far as these tests are concerned.
PREAMBLE = Preamble.from_names(["Load", "MergeMD", "mtd", "AnalysisDataService", "np"])

STAR_SCRIPT = """\
from mantid.simpleapi import *

def reduce_run(runno):
    ws = Load(Filename=f"ISIS_{runno}.nxs")
    mtd.addOrReplace("reduced", ws)
    return MergeMD(InputWorkspaces=[ws])
"""


FACILITY_SCRIPT = """\
from mantid.simpleapi import *
from isis_powder.routines.common import *

def reduce_run(runno):
    grouping = load_grouping_file(runno)
    ws = Load(Filename=f"ISIS_{runno}.nxs")
    return grouping
"""


def test_a_script_star_importing_facility_code_is_not_accused_of_anything() -> None:
    """`isis_powder` is facility code, and this service deliberately does not install it.

    Its helper names cannot be enumerated here, so they cannot be claimed undefined. Rewriting that star
    import - which is what an earlier version of this code did - reported `undefined name
    'load_grouping_file'` on a script that runs correctly, and a false positive costs the credibility of
    every real warning next to it.
    """
    assert lint_diagnostics(FACILITY_SCRIPT, PREAMBLE) == []


def test_that_silence_covers_a_typo_too_and_that_is_a_decision_not_an_oversight() -> None:
    """The cost of the rule above, pinned so nobody 'fixes' it by rewriting the import again.

    pyflakes hedges every unresolved name once a scope holds a star import it cannot see through, so a
    misspelled algorithm in such a script goes unreported as well. Deliberate: this service reports only
    what it can know, and a script that uses unmodellable star imports is one it cannot know about.
    """
    assert lint_diagnostics(FACILITY_SCRIPT.replace("Load(", "Loa(", 1), PREAMBLE) == []


def test_a_script_using_only_framework_names_is_silent() -> None:
    """The whole corpus assertion in miniature: `import *` must not produce a wall of warnings.

    Measured on the real MERLIN script in that form, plain pyflakes reports fourteen "may be undefined,
    or defined from star imports" on working code.
    """
    assert lint_diagnostics(STAR_SCRIPT, PREAMBLE) == []


def test_a_misspelled_algorithm_is_reported_once_where_it_was_typed() -> None:
    """`BinMD` as `BinMQ`: the mistake this exists to catch, and the one naive pyflakes hides."""
    source = STAR_SCRIPT.replace("MergeMD(", "MergeMDTypo(", 1)

    diagnostics = lint_diagnostics(source, PREAMBLE)

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.message == "undefined name 'MergeMDTypo'"
    assert diagnostic.severity == "warning"
    assert diagnostic.source == "pyflakes"
    # Line 6 of the script, not line 6 + len(preamble): the offset is undone before anyone sees it.
    assert diagnostic.start_line == 6
    assert diagnostic.start_column == 12
    assert diagnostic.end_column == 12 + len("MergeMDTypo")


def test_warnings_are_advisory_because_static_analysis_cannot_see_the_runner() -> None:
    """The runner injects names around a script, so "this will fail" is not ours to claim.

    `compile()` says that, and says it as an error; a name this pass cannot resolve is a strong hint,
    not a verdict.
    """
    source = "result = something_the_runner_provides()\n"

    diagnostics = lint_diagnostics(source, PREAMBLE)

    assert [item.severity for item in diagnostics] == ["warning"]


def test_style_opinions_are_not_reported() -> None:
    """Measured on real scripts: 2-3 unused imports, an unused variable, and 35 redefinitions at scale."""
    source = """\
from mantid.simpleapi import Load
import json
unused_workspace = Load(Filename="x.nxs")
unused_workspace = Load(Filename="y.nxs")
"""

    assert lint_diagnostics(source, PREAMBLE) == []


def test_positions_count_utf16_units_like_the_editor_does() -> None:
    """A misspelling after an emoji, through the lint path - all three column conventions differ here.

    The name starts after a 14-character prefix containing one astral character. That prefix is 17 UTF-8
    bytes (what pyflakes reports) and 15 UTF-16 units (what Monaco wants), so the answer is column 16:
    passing the byte offset through would say 18, and counting characters would say 15.
    """
    source = 'label = "\U0001f600" + missing_name\n'

    diagnostics = lint_diagnostics(source, PREAMBLE)

    assert len(diagnostics) == 1
    assert diagnostics[0].message == "undefined name 'missing_name'"
    assert diagnostics[0].start_column == 1 + utf16_length('label = "\U0001f600" + ') == 16


def test_a_script_that_does_not_parse_gets_no_warnings() -> None:
    """Half-typed code produces an undefined name for every name in it; the syntax pass covers that."""
    source = STAR_SCRIPT.replace("def reduce_run(runno):", "def reduce_run(runno:") + "undefined_here\n"

    assert lint_diagnostics(source, PREAMBLE) == []


def test_builtins_and_the_scripts_own_names_are_never_reported() -> None:
    source = """\
from mantid.simpleapi import *

def helper(values):
    total = sum(len(value) for value in values)
    return [str(total), dict(), list(values)]

def reduce_run(runno):
    return helper([str(runno), Load(Filename="x")])
"""

    assert lint_diagnostics(source, PREAMBLE) == []


def test_the_message_does_not_carry_pyflakes_own_prefix() -> None:
    """`str(message)` is "script.py:12:5: undefined name 'x'" - a position quoted inside a position."""
    diagnostics = lint_diagnostics("x = nowhere()\n", PREAMBLE)

    assert diagnostics[0].message == "undefined name 'nowhere'"


def test_a_pyflakes_crash_costs_the_lint_and_not_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A third-party checker falling over must not cost the user their syntax diagnostics.

    `lint_diagnostics` is called after the syntax pass and its result is appended to it, so an exception
    escaping here becomes a 500 for a script the service was perfectly able to talk about.
    """

    def explode(*args: object, **kwargs: object) -> None:
        msg = "pyflakes fell over"
        raise RuntimeError(msg)

    monkeypatch.setattr(lint_module, "Checker", explode)

    assert lint_diagnostics(STAR_SCRIPT, PREAMBLE) == []


def test_a_report_about_the_preamble_itself_is_never_pointed_at_the_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one guard between a mapped line number and a squiggle under the wrong code.

    A binding colliding with something in the script makes pyflakes report inside the preamble, where the
    line does not exist in the user's file. Emitting the number anyway would underline arbitrary code.
    """
    monkeypatch.setattr(Preamble, "to_user_line", lambda self, analysed_line: None)

    assert lint_diagnostics("x = Lod()\n", PREAMBLE) == []
