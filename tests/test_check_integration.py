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
    generate._VALIDATION_WARNINGS = 0
    generate._VALIDATION_SOFT_WARNINGS = 0
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = generate.main([str(section), "--check"])
    return code, err.getvalue()


class DefaultPaths(unittest.TestCase):
    @unittest.skip("newton-specific default paths; not applicable to the standalone package")
    def test_defaults_point_at_the_repo_section_and_assets(self):
        # The no-argument invocation (CI, local) must target the real docs.
        root = Path(generate.__file__).resolve().parents[3]
        self.assertEqual(generate.DEFAULT_SECTION, root / "docs" / "drone-system")
        self.assertEqual(generate.DEFAULT_OUT, root / "docs" / "assets" / "diagrams" / "drone-system")
        self.assertTrue(generate.DEFAULT_SECTION.is_dir())


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


class LinkTargetInvariant(unittest.TestCase):
    """Every payload-token link in the REAL section must resolve to a diagram the
    generator actually emits, and every token must be linked. This is the
    no-dead-link / everything-clickable guarantee, locked against a future
    divergence between _payload_tokens, aggregate_diagrams and
    drawable_flow_stems (the rest of the suite would otherwise stay green — the
    multiflow-* build-guard exemption and --check both miss it)."""

    @unittest.skipUnless(
        Path(__file__).resolve().parents[3].joinpath("docs", "drone-system").is_dir(),
        "newton repo docs not present (newton-specific test)",
    )
    def test_real_section_tokens_all_link_to_a_generated_diagram(self):
        from interface_diagrams import manifest

        _name, doc_paths = manifest.parse_section(generate.DEFAULT_SECTION)
        generate._VALIDATION_WARNINGS = generate._VALIDATION_SOFT_WARNINGS = 0
        with contextlib.redirect_stderr(io.StringIO()):
            full, flows, _parsed, unresolved = generate.parse_closure(doc_paths)
            drawable = generate.drawable_flow_stems(flows, full, unresolved)
            aggs = set(generate.aggregate_diagrams(flows, full))
            edges = generate.derive_edges(flows, full, drawable_stems=drawable)
        links = [link for e in edges for _t, link in e.tokens]
        self.assertTrue(links, "expected the real section to have payload tokens")
        self.assertNotIn(None, links, "every payload token must be clickable")
        dead = sorted({lk for lk in links if lk[:-4] not in drawable and lk[:-4] not in aggs})
        self.assertEqual(dead, [], f"token links with no generated target: {dead}")


if __name__ == "__main__":
    unittest.main()
