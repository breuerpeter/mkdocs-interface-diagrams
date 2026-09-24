"""Golden-output parity gate: proves the package generator renders the same
SVGs as the original newton tool.

Golden files under tests/fixtures/parity/golden/ were captured with:
  cd ~/code/newton && uv run --group docs python tools/diagrams/scripts/generate.py \
    ~/code/mkdocs-interface-diagrams/tests/fixtures/parity \
    --out ~/code/mkdocs-interface-diagrams/tests/fixtures/parity/golden

Un-normalised diff of golden vs package output was empty (bit-identical), so
no normalisation is applied.  The _norm stub is kept in case a future change
introduces excalidraw seed non-determinism — tighten it then rather than now.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "parity"
GOLDEN = FIX / "golden"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

# Excalidraw seeds roughjs strokes; if seeds are not pinned, normalise them out
# before comparing so the gate asserts structural+geometric equality, not noise.
# Tightened to require attribute delimiters: word boundaries for seed/versionNonce,
# quote-delimited for "id" values. Prevents spurious matches on structural SVG content.
_SEED = re.compile(r'\b(seed|versionNonce)=[0-9]+|("id":")[^",]+')


def _norm(svg: str) -> str:
    return _SEED.sub(r"\1X", svg)


def test_package_output_matches_golden(tmp_path):
    out = tmp_path / "out"
    rc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from interface_diagrams.generate import main; "
            f"raise SystemExit(main([{str(FIX)!r}, '--out', {str(out)!r}]))",
        ],
        cwd=ROOT,
    ).returncode
    assert rc == 0
    produced = sorted(p.name for p in out.glob("*.svg"))
    expected = sorted(p.name for p in GOLDEN.glob("*.svg"))
    assert len(expected) >= 5, f"golden set is degenerate: only {len(expected)} SVGs"
    assert produced == expected, "diagram filename set drifted"
    for name in expected:
        assert _norm((out / name).read_text()) == _norm((GOLDEN / name).read_text()), name


def test_tags_and_a_target_declaration_change_no_diagram_on_the_pages(tmp_path):
    """Tags and a target declaration change no diagram on the pages."""
    section = tmp_path / "parity"
    shutil.copytree(FIX, section, ignore=shutil.ignore_patterns("golden"))
    for doc, old, new in (
        ("index.md", "system: Parity Demo\n", "system: Parity Demo\ntargets: [x_1, x_2]\n"),
        ("controller.md", "**Telemetry uplink**\n", "**Telemetry uplink**\n`x_1`\n"),
        ("controller.md", "**Sensor read**\n", "**Sensor read**\n`x_*`\n"),
        ("display.md", "**Status reply**\n", "**Status reply**\n`x_2`\n"),
    ):
        path = section / doc
        path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    out = tmp_path / "out"
    rc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from interface_diagrams.generate import main; "
            f"raise SystemExit(main([{str(section)!r}, '--out', {str(out)!r}]))",
        ],
        cwd=ROOT,
    ).returncode
    changed = [
        p.name
        for p in sorted(GOLDEN.glob("*.svg"))
        if not (out / p.name).is_file() or _norm((out / p.name).read_text()) != _norm(p.read_text())
    ]
    assert (rc, changed) == (0, [])
