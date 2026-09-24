"""Target views (#5): a section that declares targets renders, beside its
all-targets diagrams, one folder of diagrams per target, drawn from that
target's flows alone. One render of the section below serves every test."""

import re
import shutil

import pytest

from interface_diagrams.cli import main

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

TARGETS = ("x_1", "x_2", "y_1")

INDEX = """\
---
system: Fleet
targets: [x_1, x_2, y_1]
---
# Fleet
"""

# alpha (x_1) and beta (x_2) cross the same interfaces with the same payload;
# gamma is tagged with a glob; DevSpare has no flow.
SUB_A = """\
# SubA

## DevA
### Interfaces
#### eth0
##### MAVLink

**alpha**
`x_1`
1. [[SubB#DevB > eth0]]
2. [[SubB#DevB > srv > tcp:2]]

**beta**
`x_2`
1. [[SubB#DevB > eth0]]
2. [[SubB#DevB > srv > tcp:2]]

#### eth1
##### Video

**gamma**
`x_*`
1. [[SubB#DevC > eth0]]

## DevSpare
### Interfaces
#### eth0
"""

# delta has no tag; epsilon (x_1) is the only flow that reaches DevE.
SUB_B = """\
# SubB

## DevB
### Interfaces
#### eth0
### Components
#### srv
##### tcp:2

## DevC
### Interfaces
#### eth0
##### Status

**delta**
1. [[SubA#DevA > eth1]]

## DevE
### Interfaces
#### eth0
##### Log

**epsilon**
`x_1`
1. [[SubA#DevA > eth1]]
"""

ALPHA = "suba-deva-eth0-mavlink-alpha.svg"
BETA = "suba-deva-eth0-mavlink-beta.svg"
GAMMA = "suba-deva-eth1-video-gamma.svg"
DELTA = "subb-devc-eth0-status-delta.svg"

HREF = re.compile(r'href="([^"]+)"')


@pytest.fixture(scope="module")
def views(tmp_path_factory):
    """The render's output folder; each target's view is the folder named after it."""
    root = tmp_path_factory.mktemp("fleet")
    section = root / "fleet"
    section.mkdir()
    for name, text in (("index.md", INDEX), ("SubA.md", SUB_A), ("SubB.md", SUB_B)):
        (section / name).write_text(text, encoding="utf-8")
    out = root / "out"
    assert main(["generate", str(section), "--out", str(out)]) == 0
    return out


def links(view):
    """The file name of every link in every diagram of one target's view."""
    return {h.rsplit("/", 1)[-1] for p in view.glob("*.svg") for h in HREF.findall(p.read_text(encoding="utf-8"))}


def test_a_flow_tagged_x_1_shows_in_x_1s_view_and_not_in_x_2s(views):
    """A flow tagged `x_1` shows in `x_1`'s view and not in `x_2`'s."""
    assert {t: (views / t / ALPHA).is_file() for t in ("x_1", "x_2")} == {"x_1": True, "x_2": False}


def test_a_flow_with_no_tag_shows_in_every_targets_view(views):
    """A flow with no tag shows in every target's view."""
    assert {t: (views / t / DELTA).is_file() for t in TARGETS} == {"x_1": True, "x_2": True, "y_1": True}


def test_a_glob_tag_puts_the_flow_in_each_declared_target_it_matches(views):
    """A glob in a tag, such as `x_*`, puts the flow in the view of each declared target it matches."""
    assert {t: (views / t / GAMMA).is_file() for t in TARGETS} == {"x_1": True, "x_2": True, "y_1": False}


def test_a_shared_edge_shows_only_each_targets_own_flows(views):
    """An edge that carries flows of different targets shows only that target's flows in each target's view."""

    def flow_links(target):
        return {n for n in links(views / target) if n in (ALPHA, BETA) or n.startswith("multiflow-")}

    assert {t: flow_links(t) for t in ("x_1", "x_2")} == {"x_1": {ALPHA}, "x_2": {BETA}}


def test_a_box_shows_in_a_targets_view_only_when_that_targets_flows_reach_it(views):
    """A box shows in a target's view only when a flow of that target reaches it, so a box no flow reaches shows in no target's view."""
    drawn = {t: ("subb-deve.svg" in links(views / t), "suba-devspare.svg" in links(views / t)) for t in ("x_1", "x_2")}
    assert drawn == {"x_1": (True, False), "x_2": (False, False)}


def test_a_targets_view_holds_each_diagram_kind_its_flows_reach(views):
    """A target's view holds each diagram kind, from system down to flow, drawn from that target's flows."""
    assert sorted(p.name for p in (views / "x_1").glob("*.svg")) == [
        "fleet.svg",
        "suba-deva-eth0-mavlink-alpha.svg",
        "suba-deva-eth0.svg",
        "suba-deva-eth1-video-gamma.svg",
        "suba-deva-eth1.svg",
        "suba-deva.svg",
        "suba.svg",
        "subb-devb-eth0.svg",
        "subb-devb-srv-tcp_2.svg",
        "subb-devb-srv.svg",
        "subb-devb.svg",
        "subb-devc-eth0-status-delta.svg",
        "subb-devc-eth0.svg",
        "subb-devc.svg",
        "subb-deve-eth0-log-epsilon.svg",
        "subb-deve-eth0.svg",
        "subb-deve.svg",
        "subb.svg",
    ]
