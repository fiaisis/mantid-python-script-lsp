"""Position conversion tests.

Every case here is a real failure mode: the units disagreeing, the line split disagreeing, or the
source not saying where an error ends. A mistake in this module does not raise - it draws the squiggle
a few characters away from the thing it is about, which is the kind of defect that survives review
because the diagnostic still looks plausible.
"""

from __future__ import annotations

from typing import NamedTuple

import pytest

from script_lsp.positions import (
    column_at,
    column_at_byte_offset,
    jedi_error_range,
    source_lines,
    syntax_error_range,
    utf16_length,
)


def compile_error(source: str) -> SyntaxError:
    """The `SyntaxError` CPython raises for `source`, for feeding to the converter."""
    with pytest.raises(SyntaxError) as raised:
        compile(source, "script.py", "exec")
    return raised.value


class FakeJediError(NamedTuple):
    """Stands in for jedi's `SyntaxError`, whose attributes are the ones named in the Protocol."""

    line: int
    column: int
    until_line: int
    until_column: int

    def get_message(self) -> str:
        return "fake"


def test_emoji_shift_every_column_after_them() -> None:
    """CPython counts characters, Monaco counts UTF-16 code units; an astral character is two of them.

    Measured, not reasoned: for this line CPython reports offset 12 for `class`, and the range belongs
    at columns 14 to 19. Passing the number through unchanged gives 12 to 17 - a keyword-shaped
    squiggle starting two characters early and ending three characters short.
    """
    source = 'x = "\U0001f600\U0001f600" + class'

    assert compile_error(source).offset == 12
    assert syntax_error_range(compile_error(source), source) == (1, 14, 1, 19)


def test_basic_multilingual_characters_count_once() -> None:
    """An accented letter is one UTF-16 unit; treating it like an emoji would overshoot."""
    assert utf16_length("café \U0001f600") == 7
    assert column_at("café = ", 6) == 7


def test_line_splitting_matches_the_tokenizer() -> None:
    """`str.splitlines()` also breaks on form feed, vertical tab and U+2028; CPython's tokenizer does not.

    Using it would invent a line break that the compiler does not see, and shift every line number
    below it - relocating diagnostics into a different part of the script.
    """
    source = "import os\x0c\nprint os"

    assert len(source_lines(source)) == 2
    assert len(source.splitlines()) == 3  # the trap, demonstrated
    assert syntax_error_range(compile_error(source), source)[0] == 2


def test_carriage_returns_are_not_part_of_the_line() -> None:
    """Windows line endings arrive from the editor; a trailing `\\r` must not be counted as a character."""
    assert source_lines("def reduce_run()\r\n    pass\r\n") == ["def reduce_run()", "    pass", ""]


def test_a_missing_end_position_still_draws_one_character() -> None:
    """`expected ':'` is reported with no width - the missing character has none - and width is visibility."""
    source = "def reduce_run(runno)\n"
    error = SyntaxError("expected ':'", ("script.py", 1, 22, 22))

    assert compile_error(source).offset == 22  # CPython really does report an end equal to the start
    assert syntax_error_range(error, source) == (1, 22, 1, 23)


def test_an_offset_past_the_end_of_its_line_is_clamped_to_the_line() -> None:
    """A number beyond the text would put the marker outside the editor's line, so it is pulled back."""
    error = SyntaxError("bad", ("script.py", 4, 40, None))

    assert syntax_error_range(error, "x = 1\ny = 2\nz = 3\n    oops\n") == (4, 9, 4, 10)


def test_a_line_number_past_the_end_of_the_source_does_not_raise() -> None:
    """Trusts the interpreter's numbers, which occasionally point one line beyond a truncated file."""
    assert syntax_error_range(SyntaxError("eof", ("script.py", 9, 3, None)), "x = 1\n") == (9, 1, 9, 2)


def test_pyflakes_columns_are_utf8_bytes_not_characters() -> None:
    """Measured against pyflakes: `col` comes from `tokenize` over the encoded source.

    The three differ on the same line, which is why this needs its own conversion rather than the one
    CPython and jedi use: `lábel = nowhere()` reports byte column 9 for a name starting at code point 8,
    and an astral character is four bytes where Monaco wants two columns.
    """
    assert column_at_byte_offset("lábel = nowhere", 9) == 9  # é is two bytes, one character, one column
    assert column_at_byte_offset('a = "\U0001f600\U0001f600" + nowhere', 17) == 14
    assert column_at("a = nowhere", 4) == 5  # the code-point conversion, unchanged, for comparison


def test_a_byte_offset_into_the_middle_of_a_character_does_not_raise() -> None:
    """If an analyser ever reports an offset inside a multi-byte character, land on a column anyway."""
    assert column_at_byte_offset('x = "\U0001f600" + nowhere', 6) == 6  # stops one byte into a 4-byte emoji
    assert column_at_byte_offset("x = 1", 400) == 6  # past the end, clamped


def test_jedi_columns_are_zero_based() -> None:
    """jedi's 0-based column becomes Monaco's 1-based one; measured against jedi's own output."""
    source = "import os\x0c\nprint os"

    assert jedi_error_range(FakeJediError(line=2, column=6, until_line=2, until_column=8), source) == (2, 7, 2, 9)


def test_a_jedi_range_ending_before_it_starts_is_widened_to_one_character() -> None:
    """jedi's `until_*` marks where it gave up, which is sometimes where it started."""
    assert jedi_error_range(FakeJediError(line=3, column=4, until_line=1, until_column=0), "a\nb\ncdef\n") == (
        3,
        5,
        3,
        6,
    )


def test_columns_are_exclusive_at_the_end() -> None:
    """Monaco's end column is one past the last character, so a single character spans exactly one."""
    assert column_at("abc", 1) - column_at("abc", 0) == 1
