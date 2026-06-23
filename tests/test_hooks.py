"""hooks.py (the repo-root mkdocs build hook): doc scanning — anchors, the
diagram index, and the system-diagram path (the home-button regression) — plus
standalone-SVG link rewriting.

Needs the docs venv (`markdown` for _scan, `mkdocs` for the rewrites); skipped
where those aren't installed. CI runs the suite via `uv run`, where they are."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HAVE_MARKDOWN = importlib.util.find_spec("markdown") is not None
HAVE_MKDOCS = importlib.util.find_spec("mkdocs") is not None

from interface_diagrams import _hooklogic as hooks

SUB_DOC = """\
# Sub A

## DevA

### Interfaces

#### UDP:14550

##### MAVLink
**telemetry**
1. [[SubB#DevB > UDP:14550]]
2. [[#DevA > UDP:14550]]

### Components

#### proc

##### unix:/x.sock

## DevB

### Interfaces

#### UDP:14550
"""

# No managed blocks: the landing page is plain, and the build derives that the
# system overview belongs here from index.md being the landing page + the SVG
# existing (see hooks._scan).
INDEX_DOC = """\
---
system: Drone System
---
# Drone System

Intro prose.
"""


def docs_tree(td: str, svgs=()) -> Path:
    """docs/<section>/ layout matching the real repo. `svgs` are the diagram
    stems to materialise under assets/diagrams/<section>/ — placement is derived
    from these existing, so tests create exactly the ones they exercise."""
    docs = Path(td) / "docs"
    section = docs / "drone-system"
    section.mkdir(parents=True)
    (section / "index.md").write_text(INDEX_DOC, encoding="utf-8")
    (section / "SubA.md").write_text(SUB_DOC, encoding="utf-8")
    dd = docs / "assets" / "diagrams" / "drone-system"
    dd.mkdir(parents=True)
    for s in svgs:
        (dd / f"{s}.svg").write_text("<svg/>", encoding="utf-8")
    return docs


@unittest.skipUnless(HAVE_MARKDOWN, "markdown package not installed")
class Scan(unittest.TestCase):
    def make(self, svgs=()):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return docs_tree(td.name, svgs)

    def scan(self, docs):
        # The hook caches its scan in module globals (built once per mkdocs
        # run); reset so each test sees its own tree.
        hooks._DIAGRAMS = hooks._PATHS = hooks._DOCS = hooks._TITLES = None
        hooks._SYSTEM_SVG = hooks._SECTION_STEMS = None
        return hooks._scan(str(docs))

    def test_system_svg_path_is_normalized(self):
        # THE home-button regression: index.md sits in drone-system/, so its
        # diagram reaches assets through '../'; the derived path must be the
        # normalized 'assets/diagrams/...' that matches an mkdocs Files key
        # (un-normalized, the lightbox home button vanished).
        self.scan(self.make(svgs=["drone_system"]))
        self.assertEqual(hooks._SYSTEM_SVG["drone-system"], "assets/diagrams/drone-system/drone_system.svg")

    def test_system_diagram_indexed_under_the_landing_page(self):
        diagrams, _ = self.scan(self.make(svgs=["drone_system"]))
        self.assertIn(("drone-system", "drone_system"), diagrams)
        self.assertEqual(diagrams[("drone-system", "drone_system")][0], "drone-system/index.md")

    def test_device_diagram_indexed_at_its_heading_anchor(self):
        diagrams, _ = self.scan(self.make(svgs=["suba-deva"]))
        self.assertEqual(diagrams[("drone-system", "suba-deva")], ("drone-system/SubA.md", "deva"))

    def test_flow_diagram_indexed_under_its_doc(self):
        diagrams, _ = self.scan(self.make(svgs=["suba-deva-udp_14550-mavlink-telemetry"]))
        self.assertEqual(diagrams[("drone-system", "suba-deva-udp_14550-mavlink-telemetry")][0], "drone-system/SubA.md")

    def test_unplaceable_svg_fails_the_build(self):
        # A rendered SVG whose name matches no heading/flow-label means the
        # generator's naming and the hook's derivation have drifted.
        with self.assertRaises(RuntimeError):
            self.scan(self.make(svgs=["suba-ghost-device"]))

    def test_aggregate_diagram_svg_is_exempt_from_the_build_guard(self):
        # Aggregate diagrams (multiflow-*) are synthetic, referenced only from
        # edge labels — they map to no heading/flow, so the guard must not reject
        # them.
        diagrams, _ = self.scan(self.make(svgs=["suba-deva", "multiflow-mavlink-abc1234567"]))
        self.assertIn(("drone-system", "suba-deva"), diagrams)

    def test_interface_paths_resolve_in_every_waypoint_spelling(self):
        _, paths = self.scan(self.make())
        doc = "drone-system/SubA.md"
        # Bare, device-qualified, and component-qualified forms all index.
        self.assertIn((doc, "UDP:14550"), paths)
        self.assertIn((doc, "DevA > UDP:14550"), paths)
        self.assertIn((doc, "proc > unix:/x.sock"), paths)
        self.assertIn((doc, "DevA > proc > unix:/x.sock"), paths)

    def test_duplicate_headings_get_mkdocs_suffixed_anchors(self):
        _, paths = self.scan(self.make())
        doc = "drone-system/SubA.md"
        self.assertEqual(paths[(doc, "DevA > UDP:14550")], "udp14550")
        self.assertEqual(paths[(doc, "DevB > UDP:14550")], "udp14550_1")

    def test_h1_title_indexed_for_waypoint_labels(self):
        # Waypoint labels show the referenced doc's H1 title, not its filename —
        # so '[[SubA#…]]' reads "Sub A > …", not "SubA > …".
        self.scan(self.make())
        self.assertEqual(hooks._TITLES["drone-system/SubA.md"], "Sub A")


# A standalone diagram SVG carries interface (port) hrefs as '<Doc>.md#<path>'
# and title hrefs as a bare sibling '<stem>.svg'. Opened raw (not inlined), the
# '.md#' hrefs 404 unless rewritten relative to the SVG's own served location.
SVG = (
    "<svg>"
    '<a href="skynode.md#FMU (NuttX) > UART:/dev/ttyS4"><text>UART</text></a>'
    '<a href="skynode-fmu_nuttx.svg"><text>FMU</text></a>'
    '<a href="doodle_radio-radio_air.svg"><text>Radio</text></a>'
    '<a href="pilot_pro.md#USB 3.0 Hub > USB:Host"><text>USB</text></a>'
    "</svg>"
)
SVG_URL = "diagrams/skynode-som-mavlink_routerd-tcp_5790.svg"
DOC_URLS = {"skynode.md": "skynode/", "pilot_pro.md": "pilot_pro/"}
PATHS = {("skynode.md", "FMU (NuttX) > UART:/dev/ttyS4"): "fmu-nuttx-uart-dev-ttys4"}
# 'skynode-fmu_nuttx' is embedded under the FMU section; the doodle_radio stem is
# not embedded anywhere (cross-referenced subsystem outside the manifest).
DIAGRAMS = {("", "skynode-fmu_nuttx"): ("skynode.md", "fmu-nuttx")}


def fix(svg, doc_urls=DOC_URLS, paths=PATHS, diagrams=DIAGRAMS):
    return hooks._fix_standalone_svg(svg, SVG_URL, doc_urls, paths, diagrams)


@unittest.skipUnless(HAVE_MKDOCS, "mkdocs not installed")
class FixStandaloneSvg(unittest.TestCase):
    def test_iface_href_rewritten_relative_to_svg_with_anchor(self):
        self.assertIn('href="../skynode/#fmu-nuttx-uart-dev-ttys4"', fix(SVG))

    def test_iface_href_without_anchor_falls_back_to_doc_page(self):
        # 'USB 3.0 Hub > USB:Host' is not in PATHS -> link to the doc page, not 404.
        self.assertIn('href="../pilot_pro/"', fix(SVG))

    def test_title_href_rewritten_to_its_section(self):
        # An embedded diagram's title link -> the section that embeds it.
        self.assertIn('href="../skynode/#fmu-nuttx"', fix(SVG))

    def test_unembedded_title_href_left_as_svg(self):
        # No section to link to -> leave the .svg so the lightbox opens it directly.
        self.assertIn('href="doodle_radio-radio_air.svg"', fix(SVG))

    def test_payload_token_href_left_as_svg_for_lightbox(self):
        # A payload-token target (flow or aggregate) is indexed (doc, None) or
        # unplaced: its link must stay a .svg so the lightbox opens it in place,
        # NOT rewrite to a section (payload tokens never navigate the page).
        stem = "skynode-fmu_nuttx-uart-mavlink-telemetry"
        svg = f'<svg><a href="{stem}.svg"><text>MAVLink</text></a></svg>'
        diagrams = {stem: ("skynode.md", None)}
        out = hooks._fix_standalone_svg(svg, SVG_URL, DOC_URLS, PATHS, diagrams)
        self.assertIn(f'href="{stem}.svg"', out)

    def test_unknown_doc_left_unchanged(self):
        self.assertIn('href="skynode.md#FMU (NuttX) > UART:/dev/ttyS4"', fix(SVG, doc_urls={}))


def _reset():
    hooks._DIAGRAMS = hooks._PATHS = hooks._DOCS = hooks._TITLES = None
    hooks._SYSTEM_SVG = hooks._SECTION_STEMS = hooks._FILES = None


class _Page:
    def __init__(self, file):
        self.file = file


@unittest.skipUnless(HAVE_MKDOCS, "mkdocs not installed")
class PageTransform(unittest.TestCase):
    """on_page_markdown: the actual page rewrite — collapsing diagram-bearing
    titles onto their lightbox links and inlining the system overview — driven
    entirely by the derived placement (no managed blocks in the source)."""

    def render(self, src_path, svgs, system_svg="<svg></svg>"):
        from mkdocs.structure.files import File, Files

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        docs = docs_tree(td.name, svgs)
        # Give the system overview real content so inlining + href rewriting run.
        (docs / "assets" / "diagrams" / "drone-system" / "drone_system.svg").write_text(system_svg, encoding="utf-8")
        files = Files(
            [
                File(str(p.relative_to(docs)).replace(os.sep, "/"), str(docs), str(Path(td.name) / "site"), True)
                for p in docs.rglob("*")
                if p.suffix in (".md", ".svg")
            ]
        )
        _reset()
        page = _Page(files.get_file_from_path(src_path))
        md = (docs / src_path).read_text(encoding="utf-8")
        return hooks.apply_page_markdown(md, page, {"docs_dir": str(docs)}, files)

    def test_landing_page_inlines_the_system_overview(self):
        out = self.render("drone-system/index.md", ["drone_system"])
        self.assertIn('class="interface-diagram"', out)
        # H1 of the landing page stays a plain heading (not collapsed to a link).
        self.assertIn("# Drone System", out)

    def test_inlined_system_svg_port_href_rewritten_to_section(self):
        svg = '<svg><a href="SubA.md#DevA &gt; UDP:14550"><text>p</text></a></svg>'
        out = self.render("drone-system/index.md", ["drone_system"], system_svg=svg)
        # '<Doc>.md#<path>' -> the interface heading's anchor on the SubA page.
        self.assertIn("SubA/#udp14550", out)
        self.assertNotIn("SubA.md#DevA", out)

    def test_subsystem_h1_collapses_to_its_diagram_link(self):
        out = self.render("drone-system/SubA.md", ["drone_system", "suba"])
        self.assertRegex(out, r'#\s*<a [^>]*class="diagram-link"[^>]*suba\.svg[^>]*>Sub A</a>')

    def test_device_heading_collapses_to_its_diagram_link(self):
        out = self.render("drone-system/SubA.md", ["drone_system", "suba-deva"])
        self.assertIn('class="diagram-link"', out)
        self.assertIn("suba-deva.svg", out)
        self.assertRegex(out, r">DevA</a>")

    def test_flow_label_collapses_to_its_path_diagram_link(self):
        out = self.render("drone-system/SubA.md", ["drone_system", "suba-deva-udp_14550-mavlink-telemetry"])
        self.assertRegex(
            out,
            r'<a [^>]*class="diagram-link"[^>]*mavlink-telemetry\.svg[^>]*>'
            r"\*\*telemetry\*\*</a>",
        )

    def test_blank_line_separates_flow_label_from_its_waypoint_list(self):
        # python-markdown won't render a list abutting the (inline-HTML) flow
        # link; the collapse must leave a blank line before the numbered list.
        out = self.render("drone-system/SubA.md", ["drone_system", "suba-deva-udp_14550-mavlink-telemetry"])
        lines = out.split("\n")
        link = next(i for i, ln in enumerate(lines) if "diagram-link" in ln and "**telemetry**" in ln)
        self.assertEqual(lines[link + 1], "")
        self.assertTrue(lines[link + 2].lstrip().startswith("1."))

    def test_inlined_system_svg_payload_token_link_resolves_to_its_svg(self):
        # A payload-token link inside the inlined system overview resolves to the
        # diagram's .svg asset (the lightbox opens it), with NO page navigation.
        stem = "suba-deva-udp_14550-mavlink-telemetry"
        svg = f'<svg><a href="{stem}.svg"><text>MAVLink</text></a></svg>'
        out = self.render("drone-system/index.md", ["drone_system", stem], system_svg=svg)
        self.assertIn(f"{stem}.svg", out)
        self.assertNotIn(f"SubA/#{stem}", out)

    def test_heading_without_a_diagram_is_left_plain(self):
        # No suba-devb.svg rendered -> the DevB heading stays a plain heading.
        out = self.render("drone-system/SubA.md", ["drone_system"])
        self.assertIn("## DevB", out)

    def test_waypoint_label_uses_the_h1_title_not_the_filename(self):
        # The same-doc waypoint '[[#DevA > UDP:14550]]' is labelled with the
        # page's H1 title ("Sub A"), not its filename stem ("SubA").
        out = self.render("drone-system/SubA.md", ["drone_system"])
        self.assertIn(">Sub A > DevA > UDP:14550</a>", out)
        self.assertNotIn(">SubA > DevA > UDP:14550</a>", out)


if __name__ == "__main__":
    unittest.main()
