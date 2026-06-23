import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "interface_diagrams" / "_js"
FONTS = ROOT / "interface_diagrams" / "_fonts"
FIX = Path(__file__).resolve().parent / "fixtures"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def _run(bundle: str, payload: dict) -> str:
    env = {**os.environ, "INTERFACE_DIAGRAMS_FONTS": str(FONTS)}
    proc = subprocess.run(
        ["node", str(JS / bundle)],
        input=json.dumps(payload) + "\n",
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_elk_bundle_lays_out_a_spec():
    spec = json.loads((FIX / "elk_spec.json").read_text())
    out = _run("elk_layout.bundle.mjs", spec)
    result = json.loads(out.splitlines()[-1])
    # Workers return {ok, result: <graph>}; unwrap the envelope.
    assert "children" in result.get("result", result)  # laid-out graph has positioned children


def test_render_bundle_emits_svg():
    elements = json.loads((FIX / "excalidraw_elements.json").read_text())
    out = _run("render_svg.bundle.mjs", elements)
    assert "<svg" in out
