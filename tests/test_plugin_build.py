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


# Beside the bare token, the parity landing page links x_1's view by
# `[[target:<section>/<name>]]` in its own section and in section `other`,
# which declares the same targets.
QUALIFIED = TAGGED + [
    ("parity/index.md", "[[target:x_1]]\n",
     "[[target:x_1|bare]]\n\n[[target:parity/x_1|qualified]]\n\n[[target:other/x_1|other view]]\n"),
]
OTHER_INDEX = "---\nsystem: Other Demo\ntargets: [x_1, x_2]\n---\n# Other Demo\n"
LIST_PAGE = "# Targets\n\n[[target:parity/x_1]]\n\n[[target:parity/x_1|All X1]]\n"
DIAGRAM_LINK = re.compile(r'<a class="diagram-link" href="([^"]+)"[^>]*>(.*?)</a>')


def write_list_page(site, text):
    """Write `text` as `reference/list.md`, a page outside every section."""
    page = site / "docs" / "reference" / "list.md"
    page.parent.mkdir(parents=True)
    page.write_text(text, encoding="utf-8")


def diagram_links(page):
    """(file it opens, link text) of each diagram link on a built page; [] when the build wrote no page."""
    if not page.is_file():
        return []
    return [((page.parent / href).resolve(), text)
            for href, text in DIAGRAM_LINK.findall(page.read_text(encoding="utf-8"))]


@pytest.fixture(scope="module")
def qualified_site(tmp_path_factory):
    """The fixture site with QUALIFIED applied, a copy of `parity` as section
    `other` and the list page, built once: (build result, output dir)."""
    root = tmp_path_factory.mktemp("qualified")
    site = site_copy(root, QUALIFIED)
    shutil.copytree(site / "docs" / "parity", site / "docs" / "other")
    (site / "docs" / "other" / "index.md").write_text(OTHER_INDEX, encoding="utf-8")
    write_list_page(site, LIST_PAGE)
    out = root / "site"
    return build(site, out), out


def test_a_section_qualified_target_token_on_a_page_outside_every_section_opens_the_view(qualified_site):
    """`[[target:<section>/<name>]]` on a page outside every section opens that target's view."""
    built, out = qualified_site
    links = diagram_links(out / "reference" / "list" / "index.html")
    view = (out / "assets" / "diagrams" / "parity" / "x_1" / "parity_demo.svg").resolve()
    assert (built.returncode, {target for target, _ in links}, view.is_file()) == (0, {view}, True)


def test_a_section_qualified_target_token_inside_its_section_opens_the_same_view_as_the_bare_token(qualified_site):
    """The qualified token on a page inside the named section opens the same view as the bare token."""
    _, out = qualified_site
    links = {text: target for target, text in diagram_links(out / "parity" / "index.html")}
    view = (out / "assets" / "diagrams" / "parity" / "x_1" / "parity_demo.svg").resolve()
    assert (links.get("bare"), links.get("qualified")) == (view, view)


def test_a_section_qualified_target_token_opens_the_named_sections_view_of_a_shared_name(qualified_site):
    """When two sections declare the same name, the qualified token opens the named section's view."""
    _, out = qualified_site
    target = {text: t for t, text in diagram_links(out / "parity" / "index.html")}.get("other view")
    opens = (target.parent, target.is_file()) if target else None
    assert opens == ((out / "assets" / "diagrams" / "other" / "x_1").resolve(), True)


def test_a_section_qualified_target_token_without_an_alias_shows_the_target_name(qualified_site):
    """With no alias, the qualified token's link text is the target name."""
    _, out = qualified_site
    texts = [text for _, text in diagram_links(out / "reference" / "list" / "index.html")]
    assert texts[:1] == ["x_1"]


def test_a_section_qualified_target_token_with_an_alias_shows_the_alias(qualified_site):
    """With an alias, the qualified token's link text is the alias."""
    _, out = qualified_site
    texts = [text for _, text in diagram_links(out / "reference" / "list" / "index.html")]
    assert texts[1:] == ["All X1"]


def build_list_page(tmp_path, token):
    """`mkdocs build` of the TAGGED site with `token` on a page outside every
    section: (whether it failed, its output)."""
    site = site_copy(tmp_path, TAGGED)
    write_list_page(site, f"# Targets\n\n{token}\n")
    built = build(site, tmp_path / "site")
    return built.returncode != 0, built.stdout + built.stderr


def test_a_bare_target_token_on_a_page_outside_every_section_fails_the_build(tmp_path):
    """A bare token on a page outside every section fails the build and names the page."""
    failed, output = build_list_page(tmp_path, "[[target:x_1]]")
    assert (failed, "reference/list.md" in output and "x_1" in output) == (True, True)


def test_a_section_qualified_target_token_for_an_undeclared_target_fails_the_build(tmp_path):
    """A qualified token for a target its section does not declare fails the build and names the page."""
    failed, output = build_list_page(tmp_path, "[[target:parity/x_9]]")
    assert (failed, "reference/list.md" in output and "parity/x_9" in output) == (True, True)


def test_a_section_qualified_target_token_whose_folder_is_no_section_fails_the_build(tmp_path):
    """A qualified token whose folder is no section fails the build and names the page."""
    failed, output = build_list_page(tmp_path, "[[target:nosuch/x_1]]")
    assert (failed, "reference/list.md" in output and "nosuch/x_1" in output) == (True, True)


def test_a_dangling_waypoint_still_drops_its_flow_and_passes_the_build(tmp_path):
    """Any other flow error, such as a dangling waypoint, still drops the flow and passes `mkdocs build`."""
    site = site_copy(tmp_path, [
        ("parity/controller.md", "[[display#Panel > eth0:10.0.0.2/24]]", "[[display#Panel > nope]]"),
    ])
    out = tmp_path / "site"
    built = build(site, out, "--strict")
    flow = out / "assets" / "diagrams" / "parity" / "controller-mcu-app-eth0_ctrl-commands-telemetry_uplink.svg"
    assert (built.returncode, flow.is_file()) == (0, False)
