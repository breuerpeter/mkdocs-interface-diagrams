import shutil
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parent / "fixtures" / "site"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def test_mkdocs_build_generates_and_wires_diagrams(tmp_path):
    out = tmp_path / "site"
    rc = subprocess.run(["mkdocs", "build", "-f", str(SITE / "mkdocs.yml"),
                         "-d", str(out)], cwd=SITE).returncode
    assert rc == 0
    assert list((out).rglob("*.svg"))
    index = (out / "index.html").read_text()
    assert "diagram-lightbox.js" in index
