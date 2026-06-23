"""Build-time diagram placement: hooks._walk derives, from a doc's heading
structure + the filename convention, the same slugs the generator names its
SVGs. These tests pin the per-kind derivation and guard the two expressions of
the convention (generate.planned_stems vs hooks._walk) against drift.

Pure Python — no node, no rendering — so it runs in CI's unit-test step."""

import pytest
pytest.importorskip("interface_diagrams.plugin")  # Task 6 ports the plugin; skip until then

import importlib.util
import sys
import unittest
from pathlib import Path

HAVE_MARKDOWN = importlib.util.find_spec("markdown") is not None

from interface_diagrams import generate  # noqa: E402
from interface_diagrams import manifest  # noqa: E402

from interface_diagrams import plugin as hooks  # noqa: E402

# One subsystem doc exercising every diagram-bearing location.
DOC = """\
# Subsys

## DevA

### Interfaces

#### eth0

##### MAVLink
**telemetry**
1. [[#DevB > eth0]]

### Components

#### proc

##### unix:/x.sock

###### SBus
**RC**
1. [[#DevA > eth0]]
"""


def walk_slugs(doc_text, doc_stem):
    """{title text/label -> derived diagram slug} for one doc."""
    out = {}
    for rec in hooks._walk(doc_text.split("\n"), doc_stem):
        if rec[0] == "heading":
            _, _i, _lvl, text, _disp, slug, _paths = rec
            if slug:
                out[text] = slug
        else:
            _, _i, label, slug = rec
            out[label] = slug
    return out


class Derivation(unittest.TestCase):
    def setUp(self):
        self.slugs = walk_slugs(DOC, "subsys")

    def test_subsystem_h1(self):
        self.assertEqual(self.slugs["Subsys"], "subsys")

    def test_device(self):
        self.assertEqual(self.slugs["DevA"], "subsys-deva")

    def test_device_interface(self):
        self.assertEqual(self.slugs["eth0"], "subsys-deva-eth0")

    def test_component(self):
        self.assertEqual(self.slugs["proc"], "subsys-deva-proc")

    def test_component_interface(self):
        self.assertEqual(self.slugs["unix:/x.sock"], "subsys-deva-proc-unix_x_sock")

    def test_device_interface_flow(self):
        self.assertEqual(self.slugs["telemetry"], "subsys-deva-eth0-mavlink-telemetry")

    def test_component_interface_flow(self):
        self.assertEqual(self.slugs["RC"], "subsys-deva-proc-unix_x_sock-sbus-rc")

    def test_payload_base_headings_own_no_diagram(self):
        # A '##### MAVLink' / '###### SBus' groups flows; it isn't itself a
        # diagram location (only its bold labels are).
        self.assertNotIn("MAVLink", self.slugs)
        self.assertNotIn("SBus", self.slugs)


@unittest.skipUnless(HAVE_MARKDOWN, "markdown package not installed")
class DriftGuardRealDocs(unittest.TestCase):
    """Every diagram the generator could name for the real docs must be
    placeable by the hook's derivation. A divergence (renamed qualifier, a
    missed flow-label) breaks this — the other direction (a rendered SVG the
    hook can't place) is caught at build time by hooks._scan's orphan check."""

    def test_planned_stems_are_all_hook_derivable(self):
        section = REPO_ROOT / "docs" / "drone-system"
        _name, doc_paths = manifest.parse_section(section)
        planned = generate.planned_stems(doc_paths)

        derivable = {hooks.qualified_name(manifest.system_name(section))}
        for p in [*doc_paths, section / "index.md"]:
            for rec in hooks._walk(p.read_text(encoding="utf-8").split("\n"), p.stem):
                slug = rec[5] if rec[0] == "heading" else rec[3]
                if slug:
                    derivable.add(slug)

        missing = planned - derivable
        self.assertEqual(missing, set(), f"generator names diagrams the hook can't place: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
