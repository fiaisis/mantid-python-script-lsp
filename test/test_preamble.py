"""The preamble: what it binds, and the arithmetic it forces on every reported position.

The offset is the dangerous part. A preamble that adds 90 lines and a mapping that is off by one is a
warning under the wrong line, which looks exactly like an analyser that works.
"""

from __future__ import annotations

import types

import pytest

from script_lsp.preamble import Preamble, neutralise_star_imports


def test_names_are_bound_as_assignments() -> None:
    """Assignments, not imports: an import the script does not use becomes an "imported but unused".

    Measured - explicit imports in the preamble produced three spurious warnings on a script that has
    none, because the framework bindings themselves look unused to pyflakes.
    """
    preamble = Preamble.from_names(["b", "a"])

    assert preamble.text == "# framework names, generated from the running framework\na = b = None\n"
    assert preamble.name_count == 2
    assert preamble.line_count == 2


def test_duplicates_and_junk_are_dropped() -> None:
    """The list comes from `dir()` of five modules; it must not carry nonsense into the analysis."""
    preamble = Preamble.from_names(["mtd", "mtd", "not an identifier", "", "Load"])

    assert preamble.name_count == 2
    assert "mtd = Load" in preamble.text or "Load = mtd" in preamble.text


def test_module_names_are_collected_without_builtins() -> None:
    """Binding `list` or `print` would hide the mistakes users make most often."""
    module = types.ModuleType("fake_framework")
    module.Load = object()  # type: ignore[attr-defined]
    module._private = object()  # type: ignore[attr-defined]
    module.list = list  # type: ignore[attr-defined]

    preamble = Preamble.from_modules([module])

    assert preamble.name_count == 1
    assert "Load = None" in preamble.text
    assert "list" not in preamble.text


def test_the_same_names_always_render_identically() -> None:
    """Sorted output means a warm pod and a restarted one analyse a script identically."""
    first = Preamble.from_names(["z", "a", "m"])
    second = Preamble.from_names(["m", "z", "a"])

    assert first.text == second.text


@pytest.mark.parametrize("user_line", [1, 2, 57, 58, 500])
def test_every_reported_line_maps_back_to_the_script_it_came_from(user_line: int) -> None:
    """The round trip: a script line, its position in the analysed copy, and the journey home.

    The analysed source is built the way the linter builds it - string concatenation, not a join - because
    that trailing newline is exactly where the offset arithmetic comes from.
    """
    preamble = Preamble.from_names([f"name{index}" for index in range(100)])  # more than one line of bindings
    script = "\n".join(["", "", "x = 1", "y = 2"] * 200)
    analysed = preamble.text + script

    analysed_line = user_line + preamble.line_count

    assert analysed.split("\n")[analysed_line - 1] == script.split("\n")[user_line - 1]
    assert preamble.to_user_line(analysed_line) == user_line


def test_reports_about_the_preamble_itself_are_dropped() -> None:
    """A collision inside the bindings has nothing to do with the user's file."""
    preamble = Preamble.from_names([f"name{index}" for index in range(100)])

    assert preamble.to_user_line(1) is None
    assert preamble.to_user_line(preamble.line_count) is None
    assert preamble.to_user_line(preamble.line_count + 1) == 1


def test_star_imports_are_neutralised_without_moving_anything() -> None:
    source = "from mantid.simpleapi import *\n\ndef reduce_run(runno):\n    return Load(Filename=f'{runno}.nxs')\n"

    neutralised = neutralise_star_imports(source)

    assert len(neutralised.split("\n")) == len(source.split("\n"))
    assert neutralised.split("\n")[0].startswith("# framework import")
    assert "import *" not in neutralised


def test_named_imports_are_left_alone() -> None:
    """Only star imports blind the linter; rewriting anything else would change what is checked."""
    source = "from mantid.simpleapi import Load\n\nx = Load()\n"

    assert neutralise_star_imports(source) == source


def test_a_star_import_of_code_this_service_does_not_have_is_left_in_place() -> None:
    """Facility helpers come from `isis_powder`, which is deliberately not installed here.

    Blanking that import tells pyflakes its names are accounted for, and every facility helper the script
    then uses becomes `undefined name` - a working script reported as broken. Left in, pyflakes hedges
    instead, and the hedge is a message class the lint pass does not report.
    """
    source = "from mantid.simpleapi import *\nfrom isis_powder.routines.common import *\nx = 1\n"

    neutralised = neutralise_star_imports(source)

    assert neutralised.split("\n")[0].startswith("# framework import")
    assert neutralised.split("\n")[1] == "from isis_powder.routines.common import *"
    assert len(neutralised.split("\n")) == len(source.split("\n"))


def test_the_preamble_records_which_modules_its_names_came_from() -> None:
    """The set decides which star imports may be rewritten, so it has to track what was really bound."""
    module = types.ModuleType("fake_framework")
    module.Load = None

    assert Preamble.from_modules([module]).modelled_modules == frozenset({"fake_framework"})


def test_a_star_import_inside_a_docstring_is_not_touched() -> None:
    """The rewrite goes through the AST, not text matching.

    A script whose docstring shows an example import is not hypothetical, and commenting inside a string
    literal is the analyser editing the user's code rather than its own view of it.
    """
    source = '"""\nExample:\n    from mantid.simpleapi import *\n"""\n\nx = 1\n'

    assert neutralise_star_imports(source) == source


def test_indented_star_imports_are_neutralised_where_they_appear() -> None:
    """Only legal at module level at runtime, but parso and pyflakes will both see one if typed."""
    source = "if True:\n    from mantid.api import *\nx = 1\n"

    neutralised = neutralise_star_imports(source)

    assert neutralised.split("\n")[1].startswith("    # framework import")
    assert len(neutralised.split("\n")) == len(source.split("\n"))


def test_a_star_import_wrapped_over_two_lines_is_blanked_on_both() -> None:
    """The continuation line is part of the import, and leaving it would shift nothing but confuse pyflakes.

    Written the way a formatter leaves a long import, the second line is inside the AST node's range, so
    both lines have to go for the script below to stay where the user sees it.
    """
    source = "from mantid.simpleapi import \\\n    *\n\nx = 1\n"

    neutralised = neutralise_star_imports(source)

    assert neutralised.split("\n")[0].startswith("# framework import")
    assert neutralised.split("\n")[1] == ""
    assert neutralised.split("\n")[3] == "x = 1"
    assert len(neutralised.split("\n")) == len(source.split("\n"))


def test_a_script_that_does_not_parse_is_returned_unchanged() -> None:
    """Position work on broken code belongs to the syntax pass; the linter has nothing to contribute."""
    source = "from mantid.simpleapi import *\ndef broken(:"

    assert neutralise_star_imports(source) == source
