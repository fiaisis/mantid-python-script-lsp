"""Syntax diagnostics, from the two parsers worth querying.

Why both, measured rather than guessed:

| parser | verdict | message quality | coverage |
| --- | --- | --- | --- |
| `compile()` | authoritative - the runner's own verdict | exact, and often specifically good | **one error per file** |
| jedi | error-recovering, so never the verdict | usually generic ("invalid syntax") | **every position** |

So neither is dropped and neither leads. `compile()` decides `parses` and supplies its message for its
own position. jedi then reports the rest of the file, and where it shows an error `compile()` never
reached, the fragment holding that error is re-compiled on its own to see whether CPython will give a
better message for it than jedi will - which it does most of the time, and lies about the rest of the
time, hence the blocklist below.

Two errors for one typo is not a bug of this arrangement: for an unclosed bracket, jedi puts a
squiggle on the bracket (the cause) and `compile()` puts one where the parser gave up (the effect).
Both are true and the first is the useful one.

Nothing here touches Mantid, and nothing here should block: the caller decides severity and the
editor decides what to draw. The jedi calls are not cheap, so the route runs this in a worker rather
than on the event loop - see `ANALYSIS.md` when the executor lands.
"""

from __future__ import annotations

import logging
from typing import cast

import jedi

from .models import Diagnostic
from .positions import (
    JediSyntaxError,
    LineRange,
    jedi_error_range,
    source_lines,
    syntax_error_range,
)

logger = logging.getLogger(__name__)

# What a single-line fragment "discovers" about itself that is an artefact of the extraction rather
# than a property of the script: a fragment has no enclosing function or loop, so `return`, `break`,
# `continue` and `yield` appear to be outside one, and any dedent looks like a missing body once its
# `def` line has been cut off. Measured against real reduction scripts, these are the four or five
# lies the technique tells, and a confidently wrong message is worse than jedi's vague one.
FRAGMENT_ARTEFACTS = (
    "'return' outside function",
    "'break' outside loop",
    "'continue' outside loop",
    "'yield' outside function",
    "expected an indented block",
)

# Indentation complaints in jedi's output, dropped when `compile()` did not report one itself.
# An indentation error is a claim about a line's leading whitespace *and* about the block structure
# above it, and the block structure is exactly what a syntax error earlier in the file has already
# broken. When CPython is reading the same file and stays quiet about indentation, its silence is the
# better evidence. The cost is a real indentation error hidden behind an earlier one; it reappears on
# the next save, once the first mistake is fixed.
JEDI_INDENTATION_CASCADES = (
    "unexpected indent",
    "unindent does not match",
    "expected an indented block",
    "unexpected de-indent",
)


def syntax_diagnostics(source: str, *, module_name: str = "script.py") -> tuple[bool, list[Diagnostic]]:
    """Return `(parses, diagnostics)` for a whole script.

    `parses` is whether CPython would accept the file at all - what the runner will do with it. When it
    is True no syntax diagnostics are returned even if jedi disagrees: the runner's parser is the one
    that decides whether the user's reduction runs, and telling someone their script is broken when
    it will run fine is the one thing this feature must never do.
    """
    error = _syntax_error(source, module_name)
    if error is None:
        return True, []

    primary = _from_compile(error, source)
    return False, [primary, *_beyond_compile(source, primary)]


def _syntax_error(source: str, module_name: str) -> SyntaxError | None:
    """`compile()`'s verdict, or None when the script parses.

    The non-`SyntaxError` exceptions are not theoretical, and were checked on the interpreter that
    ships. A source string holding a lone surrogate raises `UnicodeEncodeError`, which is a `ValueError`;
    enough nesting raises `RecursionError`. A NUL byte, which used to be a `ValueError`, is reported as a
    plain `SyntaxError` by CPython 3.13 and needs no special handling. All of them become a diagnostic at
    the top of the file rather than a 500, because losing the whole feature over a pathological script is
    a bad trade.
    """
    try:
        compile(source, module_name, "exec")
    except SyntaxError as error:
        return error
    except (ValueError, RecursionError, MemoryError) as error:
        return SyntaxError(f"{type(error).__name__}: {error}", (module_name, 1, 1, None))
    return None


def _from_compile(error: SyntaxError, source: str) -> Diagnostic:
    start_line, start_column, end_line, end_column = syntax_error_range(error, source)
    return Diagnostic(
        message=error.msg or "invalid syntax",
        severity="error",
        source="compile",
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
    )


def _beyond_compile(source: str, primary: Diagnostic) -> list[Diagnostic]:
    """Errors jedi can see that `compile()` could not reach, and are not consequences of its error.

    Two filters, both learned from the measurements:

    * **overlapping** - jedi usually reports the same mistake at the same spot, and drawing two
      squiggles on one token with two different messages looks like a bug in the editor.
    * **after** the primary error - everything the parser found past the point it gave up is downstream
      of it. A missing colon on line 1 also produces "unexpected indent" on line 2, and reporting both
      teaches the user nothing while pushing a real error off the end of the cap.

    What survives is what jedi is actually for: the cause that sits *before* where CPython stopped -
    the unclosed bracket three lines earlier, which CPython only notices when it runs out of file.
    """
    primary_start = (primary.start_line, primary.start_column)
    primary_range = (primary.start_line, primary.start_column, primary.end_line, primary.end_column)

    try:
        # path=None keeps jedi from resolving a project directory it has no use for here.
        errors = cast("list[JediSyntaxError]", jedi.Script(code=source, path=None).get_syntax_errors())
    except Exception as failure:  # jedi's own failures must not cost the user compile()'s verdict
        logger.warning("jedi syntax analysis failed, reporting compile() only: %r", failure)
        return []

    diagnostics = []
    for error in errors:
        found = jedi_error_range(error, source)
        if (found[0], found[1]) >= primary_start or _overlaps(found, primary_range):
            continue
        if _is_indentation_cascade(error.get_message()):
            continue
        start_line, start_column, end_line, end_column = found
        diagnostics.append(
            Diagnostic(
                message=_message_for(error, source, found),
                severity="error",
                source="jedi",
                start_line=start_line,
                start_column=start_column,
                end_line=end_line,
                end_column=end_column,
            )
        )
    return diagnostics


def _message_for(error: JediSyntaxError, source: str, found: LineRange) -> str:
    return _fragment_message(source, found) or error.get_message()


def _fragment_message(source: str, found: LineRange) -> str | None:
    """What CPython says about just the lines jedi flagged, when it says something specific.

    Blank lines are prepended so the fragment's own line numbers still match the script's; the
    position is thrown away and only the message kept, but keeping the numbers aligned makes this
    function's behaviour legible when it has to be debugged.
    """
    start_line, _, end_line, _ = found
    lines = source_lines(source)
    fragment = "\n".join([""] * (start_line - 1) + lines[start_line - 1 : max(start_line, end_line)])

    try:
        compile(fragment, "<fragment>", "exec")
    except SyntaxError as error:
        message = error.msg or ""
        if not message or message.startswith(FRAGMENT_ARTEFACTS):
            return None
        return message
    except Exception:  # a fragment too strange to compile just means no message to offer
        return None
    return None


def _is_indentation_cascade(message: str) -> bool:
    """Whether jedi is complaining about indentation that `compile()` found acceptable."""
    return any(pattern in message for pattern in JEDI_INDENTATION_CASCADES)


def _overlaps(first: LineRange, second: LineRange) -> bool:
    """Whether two half-open `(line, column)` ranges intersect."""
    return (first[0], first[1]) < (second[2], second[3]) and (second[0], second[1]) < (first[2], first[3])
