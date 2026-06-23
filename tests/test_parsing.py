"""Parsing layer: wikilinks, the interface-doc heading grammar, the multi-doc
closure, and section discovery (manifest)."""

import tempfile
import unittest
from pathlib import Path

from interface_diagrams import manifest
from interface_diagrams.generate import InterfaceRef, parse_closure, parse_subsystem, parse_wikilink

from .helpers import PipelineTestCase  # also puts scripts/ on sys.path


class ParseWikilink(unittest.TestCase):
    def test_cross_doc_reference(self):
        ref = parse_wikilink("SubB#DevB > eth0", "SubA")
        self.assertEqual(ref, InterfaceRef("SubB", "DevB > eth0"))

    def test_same_doc_reference_uses_default_subsystem(self):
        ref = parse_wikilink("DevA > eth0", "SubA")
        self.assertEqual(ref, InterfaceRef("SubA", "DevA > eth0"))

    def test_hash_with_empty_doc_part_uses_default(self):
        ref = parse_wikilink("#DevA > eth0", "SubA")
        self.assertEqual(ref, InterfaceRef("SubA", "DevA > eth0"))

    def test_whitespace_is_stripped(self):
        ref = parse_wikilink("  SubB  #  DevB > eth0  ", "SubA")
        self.assertEqual(ref, InterfaceRef("SubB", "DevB > eth0"))


DOC = """\
# SubA

Intro prose, ignored.

## DevA

### Interfaces

#### eth0

##### MAVLink

**telemetry**
1. [[#Hub > in]]
2. [[SubB#DevB > eth0]]

**commands**
1. [[SubB#DevB > eth0]]

### Components

#### proc

##### udp:1

###### SBus

**RC channel**
1. [[#DevA > eth0]]

## Hub

### Interfaces

#### in
#### out
"""


class ParseSubsystem(PipelineTestCase):
    def _parse(self, text=DOC, stem="SubA"):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / f"{stem}.md"
            p.write_text(text, encoding="utf-8")
            return parse_subsystem(p)

    def test_devices_and_interfaces(self):
        devices, _, _dn = self._parse()
        self.assertEqual([d.name for d in devices], ["DevA", "Hub"])
        deva, hub = devices
        self.assertEqual([i.name for i in deva.interfaces], ["eth0"])
        self.assertEqual([i.name for i in hub.interfaces], ["in", "out"])
        self.assertEqual(deva.subsystem, "SubA")

    def test_components_and_their_interfaces(self):
        devices, _, _dn = self._parse()
        deva = devices[0]
        self.assertEqual([c.name for c in deva.components], ["proc"])
        self.assertEqual([i.name for i in deva.components[0].interfaces], ["udp:1"])

    def test_flows_under_device_interface(self):
        _, flows, _dn = self._parse()
        tele = [f for f in flows if f.label == "telemetry"]
        self.assertEqual(len(tele), 1)
        f = tele[0]
        self.assertEqual(f.payload, "MAVLink")
        self.assertEqual(f.source, ("SubA", "DevA > eth0"))
        self.assertEqual(f.waypoints, [InterfaceRef("SubA", "Hub > in"), InterfaceRef("SubB", "DevB > eth0")])

    def test_two_flows_share_one_payload_base(self):
        _, flows, _dn = self._parse()
        mav = [f for f in flows if f.payload == "MAVLink"]
        self.assertEqual({f.label for f in mav}, {"telemetry", "commands"})

    def test_flow_under_component_interface(self):
        _, flows, _dn = self._parse()
        rc = [f for f in flows if f.label == "RC channel"]
        self.assertEqual(len(rc), 1)
        self.assertEqual(rc[0].payload, "SBus")
        self.assertEqual(rc[0].source, ("SubA", "DevA > proc > udp:1"))

    def test_labelless_numbered_list_kept_as_flow_without_label(self):
        # Malformed (no '**label**'), but kept so derive_edges can flag it.
        doc = DOC.replace("**telemetry**\n", "")
        _, flows, _dn = self._parse(doc)
        unlabeled = [f for f in flows if f.label is None]
        self.assertEqual(len(unlabeled), 1)
        self.assertEqual(unlabeled[0].payload, "MAVLink")

    def test_headings_outside_known_sections_are_ignored(self):
        doc = "## Dev\n### Notes\n#### not-an-interface\n"
        devices, flows, _dn = self._parse(doc)
        self.assertEqual(devices[0].interfaces, [])
        self.assertEqual(devices[0].components, [])
        self.assertEqual(flows, [])

    def test_subsystem_is_the_file_stem(self):
        devices, _, _dn = self._parse(stem="Other Name")
        self.assertEqual(devices[0].subsystem, "Other Name")

    def test_display_name_is_h1_title_when_present(self):
        # H1 differs from stem (e.g. file renamed to kebab-case).
        doc = "# My Subsystem\n## Dev\n### Interfaces\n#### eth0\n"
        _, _, display_name = self._parse(doc, stem="my-subsystem")
        self.assertEqual(display_name, "My Subsystem")

    def test_display_name_falls_back_to_stem_when_no_h1(self):
        doc = "## Dev\n### Interfaces\n#### eth0\n"
        _, _, display_name = self._parse(doc, stem="my-subsystem")
        self.assertEqual(display_name, "my-subsystem")


class ParseClosure(PipelineTestCase):
    def _section(self, td, docs: dict):
        for stem, text in docs.items():
            (Path(td) / f"{stem}.md").write_text(text, encoding="utf-8")
        return [Path(td) / f"{stem}.md" for stem in docs]

    def test_referenced_doc_is_pulled_into_the_closure(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._section(
                td,
                {
                    "SubA": "## DevA\n### Interfaces\n#### eth0\n##### P\n**f**\n1. [[SubB#DevB > eth0]]\n",
                    "SubB": "## DevB\n### Interfaces\n#### eth0\n",
                },
            )
            sys_, flows, parsed, missing = parse_closure([paths[0]])  # only SubA given
        self.assertEqual(set(parsed), {"SubA", "SubB"})
        self.assertEqual(missing, set())
        self.assertEqual({d.name for d in sys_.devices}, {"DevA", "DevB"})
        self.assertEqual(len(flows), 1)

    def test_unresolvable_reference_is_reported_missing(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._section(
                td,
                {
                    "SubA": "## DevA\n### Interfaces\n#### eth0\n##### P\n**f**\n1. [[Ghost#Dev > if]]\n",
                },
            )
            _, _, parsed, missing = parse_closure(paths)
        self.assertEqual(set(parsed), {"SubA"})
        self.assertEqual(missing, {"Ghost"})
        self.assertIn("Ghost", self.stderr_text)

    def test_display_names_stored_on_system(self):
        # H1 titles are stored in sys_.display_names keyed by stem.
        with tempfile.TemporaryDirectory() as td:
            paths = self._section(
                td,
                {
                    "kebab-sub": "# Human Title\n## Dev\n### Interfaces\n#### eth0\n",
                },
            )
            sys_, _, _, _ = parse_closure(paths)
        self.assertEqual(sys_.display_names, {"kebab-sub": "Human Title"})


class ManifestSection(unittest.TestCase):
    def test_index_is_excluded_and_docs_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ("Zeta.md", "Alpha.md", "notes.txt"):
                (Path(td) / name).write_text("", encoding="utf-8")
            (Path(td) / "index.md").write_text("---\nsystem: Test System\n---\n", encoding="utf-8")
            name, docs = manifest.parse_section(Path(td))
        self.assertEqual(name, "Test System")
        self.assertEqual([p.name for p in docs], ["Alpha.md", "Zeta.md"])


if __name__ == "__main__":
    unittest.main()
