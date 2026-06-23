import shutil
from pathlib import Path

import pytest

from interface_diagrams.cli import main

FIX = Path(__file__).resolve().parent / "fixtures" / "parity"


def test_check_subcommand_validates_without_writing(capsys):
    rc = main(["check", str(FIX)])
    assert rc == 0


def test_generate_section_resets_validation_counters():
    from interface_diagrams import generate
    generate._VALIDATION_WARNINGS = 5
    generate._VALIDATION_SOFT_WARNINGS = 3
    rc = generate.generate_section(FIX, FIX / "unused", check=True)
    assert rc == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_generate_subcommand_writes_svgs(tmp_path):
    out = tmp_path / "out"
    rc = main(["generate", str(FIX), "--out", str(out)])
    assert rc == 0
    assert list(out.glob("*.svg"))
