"""The analysis preamble: framework names made visible to static analysis.

Mantid's algorithm functions do not exist until the framework is running. `mantid.simpleapi` mints them
as attributes at import time, which is why neither pyflakes nor jedi can see them: probed statically,
`Load` resolves while `CreateSampleWorkspace` resolves to nothing, so a name-based check built on either
would call a working script broken. Measured on a real reduction script, four ways:

With nothing done, pyflakes reports fourteen "may be undefined, or defined from star imports" and stays
silent about a misspelled algorithm, because that is one of the fourteen. Binding the framework names but
leaving the star import in place leaves one of those hedges, and the typo is still hidden behind it.
Blanking the star import without binding anything invents fourteen lies - `undefined name 'Load'`,
`'mtd'`, and so on. Bindings *and* blanking is the only combination that is silent on working code and
still reports `undefined name 'BinMQ'` at the line and column where it was typed.

So the preamble does two things and needs both: it binds every name the live framework exposes
(`simpleapi`, `api`, `kernel`, `dataobjects`, `geometry` - 1,777 of them), and it blanks the script's own
framework `import *` lines so pyflakes is allowed to have an opinion about names again. Star imports of
anything else - facility code, which is not installed here - are left in place, because blanking one is
a promise that its names were supplied, and that promise cannot be kept.

The bindings are **generated at warm-up, not committed**. The running interpreter is the only authority
on which algorithms exist, so generating them means the list cannot drift from the pinned base image -
and it is what makes a 6.x-era script calling `Configurator()` light up, because `Configurator` is not
a name in Mantid 7.

The preamble is ~90 lines, so every diagnostic pyflakes reports needs its line number moved back by
exactly that much, and anything reported inside the preamble itself is discarded.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import itertools
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import Final

# The modules a reduction script can pull the framework from. Bound together because scripts star-import
# from several of them, and a missing binding is a false positive - the one failure mode this feature
# cannot afford.
FRAMEWORK_MODULES: Final = (
    "mantid.simpleapi",
    "mantid.api",
    "mantid.kernel",
    "mantid.dataobjects",
    "mantid.geometry",
)

# Names bound `a = b = c = None`, this many per line: fewer lines to offset, a trivial parse, and still
# readable when it is dumped for debugging.
NAMES_PER_LINE: Final = 20


def _comment(indent: str) -> str:
    return f"{indent}# framework import: these names are supplied by the analysis preamble"


@dataclass(frozen=True)
class Preamble:
    """The text to prepend, and what it costs in line numbers.

    `modelled_modules` is the set of modules whose names the bindings actually cover. It matters because
    it is also the set of star imports that may be rewritten away: blanking a star import is a claim that
    its names have been supplied elsewhere, and for `from isis_powder import *` that claim is false -
    facility code is deliberately not installed here.
    """

    text: str
    line_count: int
    name_count: int
    modelled_modules: frozenset[str] = frozenset(FRAMEWORK_MODULES)

    @classmethod
    def from_names(cls, names: Iterable[str], modelled: frozenset[str] = frozenset(FRAMEWORK_MODULES)) -> Preamble:
        """Render a name list. Sorted, so the same framework always produces byte-identical output."""
        ordered = sorted({name for name in names if name.isidentifier()})
        lines = ["# framework names, generated from the running framework"]
        lines += [
            " = ".join(group) + " = None"
            for group in itertools.batched(ordered, NAMES_PER_LINE, strict=False)  # the last group is short
        ]
        text = "\n".join(lines) + "\n"
        return cls(text=text, line_count=text.count("\n"), name_count=len(ordered), modelled_modules=modelled)

    @classmethod
    def from_modules(cls, modules: list[ModuleType]) -> Preamble:
        """Everything the given modules expose, minus builtins (pyflakes knows those already)."""
        exported = {name for module in modules for name in dir(module) if not name.startswith("_")}
        return cls.from_names(
            exported - set(dir(builtins)),
            frozenset(module.__name__ for module in modules),
        )

    def to_user_line(self, analysed_line: int) -> int | None:
        """The script's own line number for a line reported in the analysed source.

        None means the report is about the preamble rather than the user's script, which happens when a
        binding collides with something in it; such a report is dropped rather than pointed at a line
        that does not exist.
        """
        if analysed_line <= self.line_count:
            return None
        return analysed_line - self.line_count


def build() -> Preamble:  # pragma: no cover - needs the framework, so it runs in the image, not in a unit test
    """Generate the preamble from the framework in this process.

    Imports are deferred: this runs from warm-up inside the image, and the modules do not exist anywhere
    else. Failing to build is a warm-up failure, not a degraded analysis - without the bindings, every
    algorithm call in every script becomes an "undefined name", which is worse than saying nothing.
    """
    return Preamble.from_modules([importlib.import_module(name) for name in FRAMEWORK_MODULES])


def neutralise_star_imports(source: str, modelled: Collection[str] = FRAMEWORK_MODULES) -> str:
    """Replace `from x import *` lines with comments, preserving line numbers.

    Only star imports of modules the preamble binds are rewritten, and the asymmetry is the whole
    design: rewriting a star import tells pyflakes those names are accounted for, which is true for
    `mantid.simpleapi` and untrue for `isis_powder`, whose code is not installed. Measured on a script
    that star-imports facility helpers, rewriting both produces `undefined name 'load_grouping_file'`
    on a script that runs perfectly. Left alone, pyflakes hedges every unresolved name as
    `ImportStarUsage` instead - a class the lint pass does not report - so the service goes quiet about
    that script rather than inventing faults in it. Being silent about what cannot be known beats being
    confidently wrong.

    Located through the AST rather than by matching text: a script's docstring can contain a line that
    reads like an import, and rewriting inside a string literal would change the code the user wrote
    rather than the view the analyser gets. Line numbers are preserved because every diagnostic is
    reported against them, and continuation lines of a parenthesised import are blanked so nothing after
    them shifts either.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # Analysis of a script that does not parse is the syntax pass's business, not the linter's.
        return source

    lines = source.split("\n")
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not any(alias.name == "*" for alias in node.names):
            continue
        if node.module not in modelled:
            continue  # names we cannot supply, so the import has to stay visible to pyflakes
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
        lines[start] = _comment(indent)
        for continuation in range(start + 1, end):
            lines[continuation] = ""
    return "\n".join(lines)
