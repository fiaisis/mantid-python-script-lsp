"""Corpus tests: clean reduction-shaped scripts must produce zero diagnostics.

Real facility reduction scripts (`spike/corpus/`) are gitignored because they contain instrument paths
and configurations that do not belong in a public repository. In CI, we run synthetic scripts written
in the exact shapes that real FIA autoreduction and live-data scripts take:

- module-level `from mantid.simpleapi import *` with `mtd`, `config`, `ConfigService` and chained
  algorithm calls;
- `def execute():` wrappers importing `reduction_utils` or `isis_powder` helpers alongside explicit
  `from mantid.simpleapi import (...)` blocks;
- scripts that star-import unmodellable facility modules (`from isis_powder.routines.common import *`).

When `spike/corpus/` has been generated locally from a live FIA API, the local corpus test also runs
against every clean variant (`MERLIN.py`, `MERLIN_star.py`, `MERLIN_x2.py`, `MERLIN_x8.py`) and checks
the deliberate error variants (`MERLIN_broken.py`, `MERLIN_small.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

CORPUS_DIR = Path(__file__).resolve().parent.parent / "spike" / "corpus"

STAR_IMPORT_REDUCTION_SCRIPT = """\
import os
import sys
from mantid.simpleapi import *
import numpy as np

config["default.facility"] = "ISIS"
ConfigService.Instance().setString("default.instrument", "GENERIC")


def reduce_run(run_number: int, output_dir: str) -> str:
    raw_ws = Load(Filename=f"GENERIC{run_number}.nxs", OutputWorkspace="raw")
    converted = ConvertUnits(InputWorkspace=raw_ws, Target="DeltaE", EMode="Direct")
    rebinned = Rebin(InputWorkspace=converted, Params="-10,0.1,10")
    scaled = Scale(InputWorkspace=rebinned, Factor=float(np.mean([1.0, 2.0])))
    mtd.addOrReplace("reduced", scaled)
    binned = BinMD(
        InputWorkspace="reduced",
        AlignedDim0="DeltaE,-10,10,100",
    )
    merged = MergeMD(InputWorkspaces=[binned])
    out_path = os.path.join(output_dir, f"GENERIC{run_number}_reduced.nxs")
    SaveNexus(InputWorkspace=merged, Filename=out_path)
    return out_path
"""

EXECUTE_WRAPPER_SCRIPT = """\
import importlib
import os
import time
from datetime import datetime


def execute() -> None:
    import reduction_utils
    from isis_powder.routines.common import load_current_variables

    importlib.reload(reduction_utils)
    from mantid.simpleapi import (
        AnalysisDataService,
        BinMD,
        ConvertUnits,
        Load,
        MergeMD,
        Rebin,
        SaveNexus,
        mtd,
    )

    variables = load_current_variables()
    ws = Load(Filename=str(variables.get("run_path", "sample.nxs")))
    ws = ConvertUnits(InputWorkspace=ws, Target="Wavelength")
    ws = Rebin(InputWorkspace=ws, Params="0.5,0.01,5.0")
    md = BinMD(InputWorkspace=ws, AlignedDim0="Wavelength,0.5,5.0,100")
    merged = MergeMD(InputWorkspaces=[md])
    mtd.addOrReplace("live_reduced", merged)
    if AnalysisDataService.doesExist("live_reduced"):
        SaveNexus(InputWorkspace="live_reduced", Filename=os.path.join("/tmp", f"out_{int(time.time())}.nxs"))
"""

FACILITY_STAR_IMPORT_SCRIPT = """\
from mantid.simpleapi import *
from isis_powder.routines.common import *


def execute() -> object:
    grouping = load_grouping_file("grouping.xml")
    ws = Load(Filename="run.nxs")
    mtd.addOrReplace("reduced", MergeMD(InputWorkspaces=[ws]))
    return write_auto_reduction_output(ws, grouping)
"""


@pytest.mark.parametrize(
    ("name", "script"),
    [
        ("star_import_reduction", STAR_IMPORT_REDUCTION_SCRIPT),
        ("execute_wrapper", EXECUTE_WRAPPER_SCRIPT),
        ("facility_star_import", FACILITY_STAR_IMPORT_SCRIPT),
        ("repeated_at_scale", STAR_IMPORT_REDUCTION_SCRIPT * 6),
    ],
)
def test_clean_synthetic_corpus_scripts_produce_zero_diagnostics(
    warm_client: TestClient,
    name: str,
    script: str,
) -> None:
    """Every clean reduction-script shape in CI must come back with zero diagnostics."""
    response = warm_client.post("/analyse", json={"content": script, "content_hash": name})

    assert response.status_code == 200
    body = response.json()
    assert body["content_hash"] == name
    assert body["parses"] is True
    assert body["diagnostics"] == []
    assert body["truncated"] is False


def test_a_typo_in_a_corpus_script_is_still_caught_at_its_exact_call_site(warm_client: TestClient) -> None:
    """Silence on clean scripts must not be achieved by muting the linter."""
    broken = STAR_IMPORT_REDUCTION_SCRIPT.replace("BinMD(", "BinMQ(", 1)
    expected_line = next(number for number, line in enumerate(broken.split("\n"), start=1) if "BinMQ(" in line)

    response = warm_client.post("/analyse", json={"content": broken, "content_hash": "typo"})

    assert response.status_code == 200
    body = response.json()
    assert body["parses"] is True
    assert [(item["message"], item["start_line"], item["start_column"]) for item in body["diagnostics"]] == [
        ("undefined name 'BinMQ'", expected_line, 14)
    ]


@pytest.mark.skipif(not CORPUS_DIR.is_dir(), reason="spike/corpus is generated locally and gitignored")
@pytest.mark.parametrize("filename", ["MERLIN.py", "MERLIN_star.py", "MERLIN_x2.py", "MERLIN_x8.py"])
def test_local_real_corpus_clean_variants_parse_cleanly(warm_client: TestClient, filename: str) -> None:
    """When a local corpus is present, every clean variant must parse with no syntax errors."""
    path = CORPUS_DIR / filename
    if not path.is_file():
        pytest.skip(f"{filename} not generated")

    script = path.read_text(encoding="utf-8")
    response = warm_client.post("/analyse", json={"content": script, "content_hash": filename})

    assert response.status_code == 200
    body = response.json()
    assert body["parses"] is True
    assert [item for item in body["diagnostics"] if item["source"] != "pyflakes"] == []
