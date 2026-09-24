import re
import shutil
import subprocess
from pathlib import Path

import pytest

from interface_diagrams.cli import main

SITE = Path(__file__).resolve().parent / "fixtures" / "site"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

HREF = re.compile(r'href="([^"]+)"')

# Declares x_1 and x_2, tags one flow for each and links x_1's view from the
# landing page. Sensor read has no tag, so it is in both views.
TAGGED = [
    ("parity/index.md", "system: Parity Demo\n", "system: Parity Demo\ntargets: [x_1, x_2]\n"),
    ("parity/index.md", "# Parity Demo\n", "# Parity Demo\n\n[[target:x_1]]\n"),
    ("parity/controller.md", "**Telemetry uplink**\n", "**Telemetry uplink**\n`x_1`\n"),
    ("parity/display.md", "**Status reply**\n", "**Status reply**\n`x_2`\n"),
]


def site_copy(root, edits):
    """The fixture site copied under `root`, with each (docs file, old, new) edit applied."""
    site = root / "src"
    shutil.copytree(SITE, site)
    for doc, old, new in edits:
        path = site / "docs" / doc
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    return site


def build(site, out, *flags):
    """`mkdocs build` of `site` into `out`, with its output captured."""
    return subprocess.run(
        ["mkdocs", "build", "-f", str(site / "mkdocs.yml"), "-d", str(out), *flags],
        cwd=site,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def tagged_site(tmp_path_factory):
    """The fixture site with TAGGED applied, built once."""
    root = tmp_path_factory.mktemp("tagged")
    out = root / "site"
    build(site_copy(root, TAGGED), out)
    return out


def test_mkdocs_build_generates_and_wires_diagrams(tmp_path):
    out = tmp_path / "site"
    rc = subprocess.run(["mkdocs", "build", "-f", str(SITE / "mkdocs.yml"),
                         "-d", str(out)], cwd=SITE).returncode
    assert rc == 0
    assert list((out).rglob("*.svg"))
    index = (out / "index.html").read_text()
    assert "diagram-lightbox.js" in index


def test_a_target_token_renders_a_lightbox_link_to_the_targets_system_diagram(tagged_site):
    """`[[target:x_1]]` on a page renders a link that opens `x_1`'s system diagram in the lightbox."""
    page = tagged_site / "parity" / "index.html"
    anchors = re.findall(r"<a\b[^>]*>", page.read_text(encoding="utf-8"))
    hrefs = [m[1] for a in anchors if 'class="diagram-link"' in a and (m := HREF.search(a))]
    to_view = [h for h in hrefs if h.endswith("x_1/parity_demo.svg")]
    assert [(page.parent / h).resolve().is_file() for h in to_view] == [True]


def test_a_box_link_in_a_targets_view_opens_the_same_targets_diagram(tagged_site):
    """Inside a target's view, a box's link opens the same target's diagram of that box."""
    view = tagged_site / "assets" / "diagrams" / "parity" / "x_1"
    system = view / "parity_demo.svg"
    hrefs = HREF.findall(system.read_text(encoding="utf-8")) if system.is_file() else []
    svgs = {(view / h.split("#")[0]).resolve() for h in hrefs if h.split("#")[0].endswith(".svg")}
    subsystems = {p: p.is_file() for p in svgs if p.stem in ("controller", "display", "sensor")}
    assert subsystems == {(view / f"{s}.svg").resolve(): True for s in ("controller", "display", "sensor")}


def test_a_targets_flow_diagram_carries_the_section_of_the_flows_payload_heading(tagged_site):
    """Closing the lightbox on a target's flow diagram lands on the payload
    heading above the flow's label, the flow's section on its page."""
    view = tagged_site / "assets" / "diagrams" / "parity" / "x_1"
    flow = view / "controller-mcu-app-eth0_ctrl-commands-telemetry_uplink.svg"
    m = re.search(r'data-section="([^"]*)"', flow.read_text(encoding="utf-8")) if flow.is_file() else None
    assert (m and m[1]) == "../../../../parity/controller/#commands"


def test_a_tag_entry_that_matches_no_declared_target_fails_build_and_check(tmp_path, capsys):
    """A tag entry, name or glob, that matches no declared target fails `mkdocs build` and
    `interface-diagrams check`, and each message names the flow."""
    site = site_copy(tmp_path, [
        ("parity/index.md", "system: Parity Demo\n", "system: Parity Demo\ntargets: [x_1]\n"),
        ("parity/controller.md", "**Telemetry uplink**\n", "**Telemetry uplink**\n`x_1` `x_9`\n"),
    ])
    built = build(site, tmp_path / "site")
    checked = main(["check", str(site / "docs" / "parity")])
    check_err = capsys.readouterr().err

    def names_it(text):
        return "Telemetry uplink" in text and "x_9" in text

    assert {
        "build": (built.returncode != 0, names_it(built.stdout + built.stderr)),
        "check": (checked != 0, names_it(check_err)),
    } == {"build": (True, True), "check": (True, True)}


def test_a_target_token_for_an_undeclared_target_fails_the_build(tmp_path):
    """`[[target:x_9]]` for an undeclared target fails `mkdocs build`, and the message names the page."""
    site = site_copy(tmp_path, [
        ("parity/index.md", "system: Parity Demo\n", "system: Parity Demo\ntargets: [x_1]\n"),
        ("parity/index.md", "# Parity Demo\n", "# Parity Demo\n\n[[target:x_9]]\n"),
    ])
    built = build(site, tmp_path / "site")
    output = built.stdout + built.stderr
    assert (built.returncode != 0, "parity/index.md" in output and "x_9" in output) == (True, True)


def test_a_dangling_waypoint_still_drops_its_flow_and_passes_the_build(tmp_path):
    """Any other flow error, such as a dangling waypoint, still drops the flow and passes `mkdocs build`."""
    site = site_copy(tmp_path, [
        ("parity/controller.md", "[[display#Panel > eth0:10.0.0.2/24]]", "[[display#Panel > nope]]"),
    ])
    out = tmp_path / "site"
    built = build(site, out, "--strict")
    flow = out / "assets" / "diagrams" / "parity" / "controller-mcu-app-eth0_ctrl-commands-telemetry_uplink.svg"
    assert (built.returncode, flow.is_file()) == (0, False)
