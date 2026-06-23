"""End-to-end `main --check` runs against fixture sections: the full
parse → derive → validate path in one process, no node needed.
This is the regression net for everything between the CLI and the renderer."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from interface_diagrams import generate

from .helpers import SCRIPTS  # noqa: F401

VALID = {
    "index.md": "---\nsystem: Drone System\n---\n# Drone System\n",
    "SubA": """\
## DevA
### Interfaces
#### eth0
### Components
#### proc
##### udp:1
###### MAVLink
**telemetry**
1. [[#DevA > eth0]]
2. [[SubB#DevB > eth0]]
3. [[SubB#DevB > srv > tcp:2]]
""",
    "SubB": """\
## DevB
### Interfaces
#### eth0
### Components
#### srv
##### tcp:2
""",
}


def write_section(td: str, docs: dict) -> Path:
    section = Path(td) / "section"
    section.mkdir()
    for stem, text in docs.items():
        name = stem if stem.endswith(".md") else f"{stem}.md"
        (section / name).write_text(text, encoding="utf-8")
    return section


def run_check(section: Path) -> tuple[int, str]:
    import interface_diagrams.edges as _edges_mod
    _edges_mod._VALIDATION_WARNINGS = 0
    _edges_mod._VALIDATION_SOFT_WARNINGS = 0
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = generate.main([str(section), "--check"])
    return code, err.getvalue()



class CheckIntegration(unittest.TestCase):
    def test_valid_section_passes(self):
        with tempfile.TemporaryDirectory() as td:
            code, err = run_check(write_section(td, VALID))
        self.assertEqual(code, 0)
        self.assertIn("check passed", err)
        self.assertIn("1 flows", err)

    def test_dangling_waypoint_fails_check(self):
        docs = dict(VALID)
        docs["SubA"] = docs["SubA"].replace("[[SubB#DevB > eth0]]", "[[SubB#DevB > nope]]")
        with tempfile.TemporaryDirectory() as td:
            code, err = run_check(write_section(td, docs))
        self.assertEqual(code, 1)
        self.assertIn("check failed", err)
        self.assertIn("dangling waypoint", err)

    def test_duplicate_flow_fails_check(self):
        docs = dict(VALID)
        docs["SubA"] += "**telemetry**\n1. [[#DevA > eth0]]\n"
        with tempfile.TemporaryDirectory() as td:
            code, err = run_check(write_section(td, docs))
        self.assertEqual(code, 1)
        self.assertIn("duplicate flow", err)

    def test_unresolved_cross_reference_is_advisory_only(self):
        # A waypoint into a doc-less subsystem renders as a stub — check passes.
        docs = dict(VALID)
        docs["SubA"] = docs["SubA"].replace("[[SubB#DevB > srv > tcp:2]]", "[[Ghost#Dev > if]]")
        with tempfile.TemporaryDirectory() as td:
            code, err = run_check(write_section(td, docs))
        self.assertEqual(code, 0)
        self.assertIn("no doc found", err)

    def test_soft_warnings_do_not_fail_check(self):
        # DevA has components, so a flow ENDING on its bare eth0 is advisory.
        docs = dict(VALID)
        docs["SubB"] = (
            docs["SubB"]
            + """\
###### Status
**heartbeat**
1. [[SubB#DevB > eth0]]
2. [[SubA#DevA > eth0]]
"""
        )
        with tempfile.TemporaryDirectory() as td:
            code, err = run_check(write_section(td, docs))
        self.assertEqual(code, 0)
        self.assertIn("advisory", err)

    def test_empty_section_errors(self):
        with tempfile.TemporaryDirectory() as td:
            code, _ = run_check(write_section(td, {"index.md": "---\nsystem: T\n---\n# T\n"}))
        self.assertEqual(code, 2)

    def test_missing_folder_errors(self):
        code, _ = run_check(Path("/nonexistent/section"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
