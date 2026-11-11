"""Undefined names, which is the only lint worth showing a scientist.

pyflakes reports a lot more than this and almost all of it is noise on real code: measured on the real
corpus it produces 2-3 "imported but unused", 1 "assigned to but never used", and - on a script
duplicated to the size of the request cap - 35 "redefinition of unused". Style commentary on someone
else's analysis code, in the colour reserved for code that will not run. So the message types are
whitelisted down to one.

What is left is worth the round trip because it is the check nothing else in FIA can do: a name is
reported only if neither the script, nor Python, nor **the framework actually running in the runner
image** provides it. That is what catches `BinMD` typed as `BinMQ`, and what catches a 6.x-era script
calling `Configurator()`, which Mantid 7 no longer has.

Severity is `warning`, not `error`, and that is a claim about truth rather than politeness: the runner
executes scripts with names injected around them, so "this will fail" is not something static analysis
is entitled to assert. `compile()` is; it is the runner's own parser.

The same reasoning limits *which* names may be reported at all. A name is called undefined only when
every place it could have come from is known: the script, the builtins, and the framework modules the
preamble binds. A script that does `from isis_powder import *` brings in names no part of this service
can enumerate, so pyflakes hedges each unresolved name as `ImportStarUsage` - not `UndefinedName`, and
not reported. Such a script gets no undefined-name checking, which is the accepted cost of never
reporting a working script as broken.
"""

from __future__ import annotations

import ast
import logging

from pyflakes.checker import Checker
from pyflakes.messages import UndefinedName

from script_lsp.models import Diagnostic
from script_lsp.positions import column_at_byte_offset, line_at, source_lines, utf16_length
from script_lsp.preamble import Preamble, neutralise_star_imports

logger = logging.getLogger(__name__)

# The name pyflakes sees. It appears in nothing we return - messages are rebuilt from the template - but
# it shows up in anything pyflakes itself logs, so it is kept stable.
LINT_FILENAME = "script.py"


def lint_diagnostics(source: str, preamble: Preamble) -> list[Diagnostic]:
    """Warnings for one script, positioned in the user's coordinates.

    The caller has already established that the script parses; if it does not, nothing is returned,
    because a half-typed line produces undefined names by the hundred and the syntax pass is already
    saying the one true thing about it.
    """
    analysed = preamble.text + neutralise_star_imports(source, preamble.modelled_modules)

    try:
        tree = ast.parse(analysed, LINT_FILENAME)
    except (SyntaxError, ValueError):
        return []

    try:
        messages = Checker(tree, LINT_FILENAME).messages
    except Exception as failure:  # a lint crash must not cost the user their syntax diagnostics
        logger.warning("pyflakes failed, reporting syntax only: %r", failure)
        return []

    user_lines = source_lines(source)
    diagnostics = []
    for message in messages:
        if not isinstance(message, UndefinedName):
            continue
        user_line = preamble.to_user_line(message.lineno)
        if user_line is None:
            continue
        # Lines are shifted by the preamble; columns are not, because it only adds whole lines above the
        # script, so the text of a reported line is identical in both copies. pyflakes' column is a UTF-8
        # byte offset, which is why this is not the same conversion the syntax pass uses.
        line_text = line_at(user_lines, user_line)
        start_column = column_at_byte_offset(line_text, message.col)
        name = str(message.message_args[0]) if message.message_args else ""
        diagnostics.append(
            Diagnostic(
                # Rebuilt from the template rather than str(message), which prefixes "script.py:12:5: ".
                message=message.message % message.message_args,
                severity="warning",
                source="pyflakes",
                start_line=user_line,
                start_column=start_column,
                end_line=user_line,
                end_column=start_column + max(1, utf16_length(name)),
            )
        )
    return diagnostics
