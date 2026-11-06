"""The wire contract between this service, FIA-API and the frontend.

This module is the canonical definition. FIA-API validates the *request* (size cap, auth, rate
limit) and forwards the response body and status untouched, so the response shape exists in exactly
two places: here, where it is produced, and `src/lib/types.ts` in `../frontend`, where it is parsed.
Changing a field here means changing that file; nothing else has to be found and updated.

Positions are Monaco's, not the LSP's: **1-based lines, 1-based columns, counted in UTF-16 code
units**, with `end_column` exclusive. The field names mirror Monaco's `IMarkerData`
(`startLineNumber`/`startColumn`/`endLineNumber`/`endColumn` in camelCase) so the marker bridge is a
field copy rather than a translation layer, and the frontend never has to do arithmetic on a
position. Python is the odd one out — 0-based columns and byte offsets — so all conversion happens
before a diagnostic leaves this service, under test, because an off-by-one here is a squiggle under
the user's cursor pointing at the wrong thing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

DEFAULT_MAX_SCRIPT_BYTES: Final = 200_000
DEFAULT_DIAGNOSTIC_LIMIT: Final = 100

# Restated from FIA-API's cap, deliberately: FIA-API rejects an oversized script, but this service is
# the thing that would spend CPU parsing it, so it does not rely on its caller to have checked. The cap
# counts bytes, not characters, and both sides must mean the same thing by it - see "The wire contract"
# in `AGENTS.md`. Tunable per deployment, but then FIA-API's own cap has to be set from the same value
# or one of the two starts rejecting scripts the other has already accepted.
MAX_SCRIPT_BYTES_ENV: Final = "MAX_SCRIPT_BYTES"
DIAGNOSTIC_LIMIT_ENV: Final = "DIAGNOSTIC_LIMIT"


def _positive_int(name: str, default: int, environ: Mapping[str, str] | None = None) -> int:
    """Read one integer setting, falling back to the default rather than to a crash.

    A typo in a manifest must not produce a pod that refuses every script or returns no diagnostics at
    all, so a bad value is logged and ignored. Read once, at import: these are deployment settings, not
    per-request ones, and a cap that changed mid-response would be worse than either value.
    """
    environment = os.environ if environ is None else environ
    raw = environment.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value < 1:
        logger.warning("%s=%r is below 1; using %s", name, raw, default)
        return default
    return value


MAX_SCRIPT_BYTES = _positive_int(MAX_SCRIPT_BYTES_ENV, DEFAULT_MAX_SCRIPT_BYTES)

# A badly broken script can produce one syntax error per line - a bad dedent does exactly that - and
# an editor cannot show, and the user cannot act on, more than a screenful. Beyond the cap the response
# says so rather than silently dropping the rest.
DIAGNOSTIC_LIMIT = _positive_int(DIAGNOSTIC_LIMIT_ENV, DEFAULT_DIAGNOSTIC_LIMIT)

# `info` is deliberately absent. Monaco would render it as a grey squiggle, and a diagnostic nobody can
# act on in the colour reserved for "nothing is wrong, but" is noise; nothing in this service emits one.
DiagnosticSeverity = Literal["error", "warning"]
DiagnosticSource = Literal["compile", "jedi", "pyflakes"]


class AnalyseRequest(BaseModel):
    """A script as it currently stands in the user's editor."""

    content: str = Field(description="The script text, exactly as edited. Not normalised in any way.")
    content_hash: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Client-computed digest of `content`, echoed back unchanged. It is what lets the caller "
            "throw away a reply that arrived after the user had already typed something else, and "
            "what FIA-API keys its cache on."
        ),
    )

    @field_validator("content")
    @classmethod
    def _within_size_cap(cls, content: str) -> str:
        # Byte length, not character count: the cap is about how much work a request can force, and
        # it has to mean the same thing here as it does in FIA-API.
        if (size := len(content.encode())) > MAX_SCRIPT_BYTES:
            msg = f"script is {size} bytes, over the {MAX_SCRIPT_BYTES} byte limit"
            raise ValueError(msg)
        return content


class Diagnostic(BaseModel):
    """One problem with the script, positioned where the editor can draw it."""

    message: str = Field(
        min_length=1,
        description=(
            "Never empty. Where CPython's own text is available it is used in preference to jedi's, "
            "because 'expected :' beats 'invalid syntax' and a confident wrong message is worse than "
            "either."
        ),
    )
    severity: DiagnosticSeverity
    source: DiagnosticSource = Field(
        description=(
            "`compile` is the runner's own verdict and the only source that means 'this will not run'; "
            "`jedi` is error-recovering coverage; `pyflakes` is lint, computed only when the script parses."
        )
    )
    start_line: int = Field(ge=1)
    start_column: int = Field(ge=1, description="1-based, counted in UTF-16 code units.")
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=1, description="Exclusive, the way Monaco reads it.")

    @model_validator(mode="after")
    def _range_is_not_backwards(self) -> Diagnostic:
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            msg = f"end {(self.end_line, self.end_column)} is before start {(self.start_line, self.start_column)}"
            raise ValueError(msg)
        return self


class AnalyseResponse(BaseModel):
    """The diagnostics for one version of one script."""

    content_hash: str
    parses: bool = Field(
        description=(
            "Whether CPython would accept the script at all - what the runner will do with it. Kept "
            "apart from the diagnostics so the editor's chip can say 'this will not run' without "
            "guessing from error counts."
        )
    )
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description=(
            f"True when more than {DIAGNOSTIC_LIMIT} were found; the first {DIAGNOSTIC_LIMIT} by position are returned."
        ),
    )

    @staticmethod
    def build(
        *,
        content_hash: str,
        parses: bool,
        diagnostics: list[Diagnostic],
        limit: int | None = None,
    ) -> AnalyseResponse:
        """Order by position and apply the cap, so the guarantees are stated once, here."""
        cap = DIAGNOSTIC_LIMIT if limit is None else limit
        ordered = sorted(diagnostics, key=lambda item: (item.start_line, item.start_column))
        return AnalyseResponse(
            content_hash=content_hash,
            parses=parses,
            diagnostics=ordered[:cap],
            truncated=len(ordered) > cap,
        )
