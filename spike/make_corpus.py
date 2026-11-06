"""Generate `spike/corpus/` — the scripts the benchmark measures against.

Give it one real script (from a FIA API or a runner) and it derives the sizes an editor actually
sees, because latency measured on one file tells you nothing about the endpoint:

    python spike/make_corpus.py /path/to/MERLIN.py

* `<name>_small.py`   the first screenful — what is on screen while someone is typing
* `<name>_x2/_x8.py`  the same script repeated, ~2x and ~8x, approaching the 200 KB request cap, so
                      the scaling of compile()/ast.parse is measured rather than assumed
* `<name>_broken.py`  one line replaced by `def reduce_run(:` — the syntax-error path is the common
                      case mid-typing, and it is also the example the plan's verification names
* `<name>_star.py`    the same script with the framework imported the way most reduction scripts
                      import it: `from mantid.simpleapi import *` at module level. The original import
                      site is blanked out, so everything below the one- or two-line header keeps its
                      line number. This is the form that defeats star-import-unaware analysis, and the
                      explicit form - the one that makes analysis easy - is not what users type.

The corpus is gitignored: real reduction scripts are facility code and do not belong in here.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BROKEN_MARKER = "def reduce_run(:"
SMALL_LINES = 60
SCALES = (2, 8)
EXPECTED_ARGC = 2

# The modules reduction scripts pull the framework from. Rewriting these to `import *` reproduces the
# form that defeats star-import-unaware analysis, which is the form being tested for.
STAR_MODULES = {"mantid.simpleapi", "mantid.api", "mantid.kernel", "mantid.dataobjects"}


def with_star_imports(source: str) -> str:
    """Rewrite explicit framework imports as a module-level `import *`.

    The scripts FIA holds are usually wrapped in `def execute():` by the reduction service, and
    `import *` is only legal at module level - so the framework import is hoisted to the top and the
    original site is blanked rather than rewritten in place. Line numbers below the header are
    unchanged, which keeps every expected position in the tests a fact rather than a guess.
    """
    tree = ast.parse(source)
    lines = source.split("\n")
    modules: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in STAR_MODULES:
            continue
        if any(alias.name == "*" for alias in node.names):
            modules.append(node.module)
            continue
        modules.append(node.module)
        span = range(node.lineno - 1, node.end_lineno or node.lineno)
        positions = list(span)
        lines[positions[0]] = f"# {node.module} is imported at module level in this variant"
        for extra in positions[1:]:
            lines[extra] = ""

    header = "\n".join(f"from {module} import *" for module in dict.fromkeys(modules))
    return f"{header}\n" + "\n".join(lines) if header else source


def build(source_path: Path, output_dir: Path) -> list[Path]:
    source = source_path.read_text(encoding="utf-8")
    lines = source.splitlines()
    stem = source_path.stem
    written: list[Path] = []

    def write(name: str, text: str) -> None:
        path = output_dir / f"{name}.py"
        path.write_text(text, encoding="utf-8")
        written.append(path)

    write(stem, source)
    write(f"{stem}_small", "\n".join(lines[:SMALL_LINES]) + "\n")
    for scale in SCALES:
        write(f"{stem}_x{scale}", "\n\n".join([source] * scale))

    # Put the damage somewhere in the middle: a syntax error on line 1 is found faster than one at the
    # end, and typing produces both.
    broken = list(lines)
    broken[len(broken) // 2] = BROKEN_MARKER
    write(f"{stem}_broken", "\n".join(broken) + "\n")

    star = with_star_imports(source)
    write(f"{stem}_star", star)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != EXPECTED_ARGC:
        print(__doc__)
        return 2

    source_path = Path(argv[1]).expanduser().resolve()
    output_dir = Path(__file__).resolve().parent / "corpus"
    output_dir.mkdir(parents=True, exist_ok=True)

    for path in build(source_path, output_dir):
        print(f"{path.relative_to(output_dir)}: {path.stat().st_size:>7} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
