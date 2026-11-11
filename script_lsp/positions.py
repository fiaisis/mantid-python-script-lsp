"""Coordinate conversion between CPython, jedi, pyflakes, and Monaco.

Four systems, four conventions:

| source | line | column |
| --- | --- | --- |
| CPython `SyntaxError` | 1-based | `offset`, 1-based, **code points** |
| jedi | 1-based | 0-based, **code points** |
| pyflakes | 1-based | 0-based, **UTF-8 bytes** |
| Monaco | 1-based | 1-based, **UTF-16 code units** |

The units are the trap, and pyflakes' is the easiest to miss because it goes through `tokenize` on the
encoded source: measured, `lábel = nowhere()` gives column 9 where the name starts at code point 8, and
`a = "😀😀" + nowhere` gives 17 where the name starts at code point 11. Passed straight to Monaco, that
puts the warning part-way through a word - and only in scripts carrying non-ASCII text, which is most of
them, since reduction scripts have Greek letters and accented sample descriptions in them.

Python counts characters where Monaco counts the UTF-16 code units JavaScript strings are made of, so
anything outside the basic multilingual plane - an emoji in a comment, which happens - shifts every
later column on that line by one. Measured, not assumed: for
`x = "😀😀" + class`, CPython reports `offset` 12 for the `class` keyword and the squiggle belongs at
Monaco column 14, with the range ending at 19. Getting this wrong does not crash anything; it draws
the squiggle a couple of characters off, under the wrong token, which is exactly the kind of defect
that survives review because the diagnostic itself looks correct.

Line splitting has its own trap: `str.splitlines()` also breaks on `\\r`, `\\v`, `\\f`, `\\x1c`, `\\u2028`
and friends, while CPython's tokenizer breaks only on `\\n`. A script carrying a stray form feed -
which does occur in files with a long history - would desync every line number below it if we used
`splitlines()`, putting every later diagnostic in the wrong place.
"""

from __future__ import annotations

from typing import Protocol

# start_line, start_column, end_line, end_column - all 1-based, columns in UTF-16 units, end exclusive.
LineRange = tuple[int, int, int, int]

MAX_CODE_POINT = 0xFFFF


class JediSyntaxError(Protocol):
    """The part of jedi's `SyntaxError` we read.

    jedi ships no `py.typed`, so under `--strict` its results arrive as `Any`; naming the shape here is
    what turns that into a checked attribute access instead of a typo that silently returns None.
    """

    line: int
    column: int
    until_line: int
    until_column: int

    def get_message(self) -> str: ...


def utf16_length(text: str) -> int:
    """How many UTF-16 code units a Python string occupies - what JavaScript counts."""
    return sum(2 if ord(char) > MAX_CODE_POINT else 1 for char in text)


def source_lines(source: str) -> list[str]:
    """Split the way CPython's tokenizer does: on `\\n` and nothing else.

    Do not replace this with `str.splitlines()`; see the module docstring.
    """
    return [line.removesuffix("\r") for line in source.split("\n")]


def line_at(source_lines_list: list[str], line_number: int) -> str:
    """The text of a 1-based line, or empty when the number is out of range.

    Out-of-range happens: a truncated final line, an error CPython attributes to one past the end. An
    empty line yields a column of 1, which is wrong-but-harmless, and is far better than the
    IndexError that would replace every diagnostic with a 500.
    """
    if 1 <= line_number <= len(source_lines_list):
        return source_lines_list[line_number - 1]
    return ""


def column_at(line: str, zero_based_index: int) -> int:
    """Monaco's 1-based UTF-16 column for a 0-based code-point index into `line`."""
    clamped = max(0, min(zero_based_index, len(line)))
    return 1 + utf16_length(line[:clamped])


def column_at_byte_offset(line: str, byte_offset: int) -> int:
    """Monaco's 1-based UTF-16 column for a 0-based **UTF-8 byte** offset into `line`.

    This is what pyflakes reports. Decoding a prefix that stops in the middle of a character - which
    only happens if the analyser's number is already wrong - drops the partial character rather than
    raising: a column one place off is cosmetic, a 500 out of the diagnostic path is not.
    """
    encoded = line.encode()
    cut = max(0, min(byte_offset, len(encoded)))
    return 1 + utf16_length(encoded[:cut].decode(errors="ignore"))


def _end(start_line: int, start_column: int, end: tuple[int, int] | None) -> tuple[int, int]:
    """Fall back to a one-character range when the source gives no usable end.

    `ast` nodes always carry end positions; `SyntaxError` does not, and sometimes reports an end equal
    to its start - `expected ':'` does, because the missing character has no width. Monaco draws
    nothing for a zero-width range, so a one-character range is the minimum that is visible. jedi
    occasionally reports an `until` at or before the start too.
    """
    if end is None or end <= (start_line, start_column):
        return start_line, start_column + 1
    return end


def syntax_error_range(error: SyntaxError, source: str) -> LineRange:
    """Convert a CPython `SyntaxError` into Monaco's coordinates.

    `offset` is 1-based, so the offending character sits at index `offset - 1`. Some errors have
    `offset` of None (rare, but `SyntaxError` is constructible with anything in it), which is treated
    as the start of the line rather than crashing the analysis of an otherwise useful diagnostic.
    """
    lines = source_lines(source)
    start_line = max(1, error.lineno or 1)
    text = line_at(lines, start_line)
    start_column = column_at(text, max(0, (error.offset or 1) - 1))

    end = None
    if error.end_lineno is not None and error.end_offset is not None:
        end = (error.end_lineno, column_at(line_at(lines, error.end_lineno), error.end_offset - 1))
    end_line, end_column = _end(start_line, start_column, end)
    return start_line, start_column, end_line, end_column


def jedi_error_range(error: JediSyntaxError, source: str) -> LineRange:
    """Convert a jedi `SyntaxError` into Monaco's coordinates.

    jedi's columns are 0-based, so no adjustment before conversion. Its `until_*` pair marks where the
    error was noticed rather than where it begins, and is trusted as the end of the range - which is
    why the unclosed bracket comes out highlighting the bracket rather than the rest of the file.
    """
    lines = source_lines(source)
    start_line = max(1, error.line or 1)
    start_column = column_at(line_at(lines, start_line), error.column)

    end = None
    if error.until_column:
        end = (error.until_line, column_at(line_at(lines, error.until_line), error.until_column))
    end_line, end_column = _end(start_line, start_column, end)
    return start_line, start_column, end_line, end_column
