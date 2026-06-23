#!/usr/bin/env python3
"""Generate diagrams for a system from a folder of interface docs.

Input is the data-flows FOLDER: every '<name>.md' in it is a subsystem interface
doc, except 'index.md' (the landing page). One run produces a fixed set of SVGs
into the --out directory:

  - one system diagram   (all subsystems collapsed)
  - one per subsystem     (that subsystem expanded)
  - one per device        (flow trace through the device)
  - one per component     (flow trace through the component)
  - one per interface     (flow trace through the interface)
  - one per flow          (that flow's end-to-end path)

Each diagram is embedded back into its source doc via an idempotent managed
block (system -> index.md bottom; subsystem -> doc top; device -> '## <Device>';
component -> '#### <Component>'; interface -> the interface heading; flow path ->
the flow's '**bold label**'). Parsing/flow rules live in the sibling
interface-docs skill's spec.md."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from interface_diagrams import embed, manifest
from interface_diagrams import workers as _workers
from PIL import ImageFont

# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceRef:
    subsystem: str
    heading: str


@dataclass
class Flow:
    payload: str  # base label, from the payload-base heading (clean, no parenthetical)
    subsystem: str  # doc where declared
    source: tuple[str, str]  # canonical (subsystem, heading) of source interface
    label: str | None = None  # bold distinguisher under the base heading; None = sole/unlabeled flow
    waypoints: list[InterfaceRef] = field(default_factory=list)  # in order


@dataclass
class Interface:
    name: str


@dataclass
class Component:
    name: str  # used for matching, port ids, width estimates, and the label
    interfaces: list[Interface] = field(default_factory=list)


@dataclass
class Device:
    name: str
    interfaces: list[Interface] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    subsystem: str = ""


@dataclass
class System:
    name: str
    devices: list[Device] = field(default_factory=list)
    # Maps file stem (subsystem key) → H1 title for diagram labels.
    # Falls back to the stem when no H1 is present.
    display_names: dict = field(default_factory=dict)

    def by_interface(self) -> dict[tuple[str, str], tuple[Device, Component | None, Interface]]:
        out: dict[tuple[str, str], tuple[Device, Component | None, Interface]] = {}
        for d in self.devices:
            for i in d.interfaces:
                out[(d.subsystem, i.name)] = (d, None, i)
                out[(d.subsystem, f"{d.name} > {i.name}")] = (d, None, i)
            for c in d.components:
                for i in c.interfaces:
                    out[(d.subsystem, i.name)] = (d, c, i)
                    out[(d.subsystem, f"{c.name} > {i.name}")] = (d, c, i)
                    out[(d.subsystem, f"{d.name} > {c.name} > {i.name}")] = (d, c, i)
        return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

FLOW_ITEM_RE = re.compile(r"^\d+\.\s*\[\[(?P<link>[^\]]+)\]\]\s*$")
# A flow's bold label under a payload-base heading — the whole (stripped) line is
# `**<label>**`. Distinguishes one flow from its base-mates (e.g. several MAVLink
# flows under one `###### MAVLink`). Mid-sentence bold in prose won't match.
BOLD_RE = re.compile(r"^\*\*(?P<label>.+?)\*\*$")

# Interface docs are named "<subsystem title>.md" — the file stem IS the
# subsystem title (used for matching, labels, colors, and cross-doc references).


def parse_wikilink(link: str, default_subsystem: str) -> InterfaceRef:
    if "#" in link:
        sub, heading = link.split("#", 1)
        sub, heading = sub.strip(), heading.strip()
    else:
        sub, heading = "", link.strip()
    return InterfaceRef(subsystem=sub or default_subsystem, heading=heading)


def parse_subsystem(path: Path) -> tuple[list[Device], list[Flow], str]:
    subsystem = path.stem
    display_name = subsystem
    devices: list[Device] = []
    flows: list[Flow] = []
    cur_device: Device | None = None
    cur_section: str | None = None
    cur_component: Component | None = None
    cur_interface: Interface | None = None  # device interface
    cur_comp_interface: Interface | None = None  # component interface
    # A payload-base section (`##### <base>` under a device interface, or
    # `###### <base>` under a component interface) groups every flow of one
    # payload. cur_payload is that base; cur_source is the enclosing interface
    # (the flows' implicit first waypoint). Individual flows under it are bold
    # labels; cur_flow is the one currently accumulating waypoints.
    cur_payload: str | None = None
    cur_source: tuple[str, str] | None = None
    cur_flow: Flow | None = None

    def end_payload():
        nonlocal cur_payload, cur_source, cur_flow
        cur_payload = cur_source = cur_flow = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()

        if line.startswith("# ") and not line.startswith("## ") and display_name == subsystem:
            display_name = line[2:].strip()
            continue

        if line.startswith("## ") and not line.startswith("###"):
            cur_device = Device(name=line[3:].strip(), subsystem=subsystem)
            devices.append(cur_device)
            cur_section = cur_component = cur_interface = None
            cur_comp_interface = None
            end_payload()
            continue

        if line.startswith("### ") and not line.startswith("#### "):
            head = line[4:].strip()
            cur_section = head if head in ("Interfaces", "Components") else None
            cur_component = cur_interface = cur_comp_interface = None
            end_payload()
            continue

        if line.startswith("#### ") and not line.startswith("##### "):
            head = line[5:].strip()
            end_payload()
            if cur_section == "Interfaces" and cur_device:
                cur_interface = Interface(name=head)
                cur_device.interfaces.append(cur_interface)
                cur_component = cur_comp_interface = None
            elif cur_section == "Components" and cur_device:
                cur_component = Component(name=head)
                cur_device.components.append(cur_component)
                cur_interface = cur_comp_interface = None
            continue

        if line.startswith("##### ") and not line.startswith("###### "):
            head = line[6:].strip()
            end_payload()
            if cur_section == "Interfaces" and cur_interface is not None:
                # payload-base section for a device interface
                cur_payload = head
                cur_source = (subsystem, _heading_for(cur_device, None, cur_interface))
            elif cur_section == "Components" and cur_component is not None:
                cur_comp_interface = Interface(name=head)
                cur_component.interfaces.append(cur_comp_interface)
            continue

        if line.startswith("###### "):
            head = line[7:].strip()
            end_payload()
            if cur_section == "Components" and cur_comp_interface is not None:
                # payload-base section for a component interface
                cur_payload = head
                cur_source = (subsystem, _heading_for(cur_device, cur_component, cur_comp_interface))
            continue

        if cur_payload is not None and cur_source is not None:
            m = BOLD_RE.match(line.strip())
            if m:
                # A new flow: its bold label distinguishes it from base-mates.
                cur_flow = Flow(
                    payload=cur_payload, subsystem=subsystem, source=cur_source, label=m.group("label").strip()
                )
                flows.append(cur_flow)
                continue
            m = FLOW_ITEM_RE.match(line)
            if m:
                if cur_flow is None:
                    # Numbered list with no preceding bold label — malformed
                    # (every flow needs a `**label**`). Keep it as a label-less
                    # flow rather than dropping it silently; derive_edges flags it.
                    cur_flow = Flow(payload=cur_payload, subsystem=subsystem, source=cur_source, label=None)
                    flows.append(cur_flow)
                cur_flow.waypoints.append(parse_wikilink(m.group("link"), subsystem))
                continue

    return devices, flows, display_name


def parse_closure(doc_paths: list[Path]) -> tuple[System, list[Flow], dict[str, Path], set[str]]:
    """Parse the given interface docs plus every doc reachable through their
    cross-subsystem flow waypoints, resolving referenced docs by filename
    ("<title>.md") in the given docs' directories.

    Returns (system, flows, title → doc path for everything parsed, titles
    referenced but unresolvable). Unresolvable references later render as text
    stubs.
    """
    dirs: list[Path] = []
    for p in doc_paths:
        if p.parent not in dirs:
            dirs.append(p.parent)

    pending: dict[str, Path] = {p.stem: p for p in doc_paths}
    parsed: dict[str, Path] = {}
    missing: set[str] = set()
    sys_ = System(name="view")
    all_flows: list[Flow] = []

    while pending:
        title, path = pending.popitem()
        parsed[title] = path
        devices, flows, display_name = parse_subsystem(path)
        sys_.display_names[title] = display_name
        sys_.devices.extend(devices)
        all_flows.extend(flows)
        for f in flows:
            for ref in f.waypoints:
                ref_sub = ref.subsystem
                if ref_sub in parsed or ref_sub in pending or ref_sub in missing:
                    continue
                cand = next((d / f"{ref_sub}.md" for d in dirs if (d / f"{ref_sub}.md").is_file()), None)
                if cand is not None:
                    pending[ref_sub] = cand
                else:
                    missing.add(ref_sub)
                    print(
                        f"warning: no doc found for referenced subsystem "
                        f"'{ref_sub}' (looked for '{ref_sub}.md') — its "
                        f"endpoints will render as stubs",
                        file=sys.stderr,
                    )
    return sys_, all_flows, parsed, missing


# ---------------------------------------------------------------------------
# Edge derivation from flows
# ---------------------------------------------------------------------------

_VALIDATION_WARNINGS = 0
_VALIDATION_SOFT_WARNINGS = 0


def _warn(msg: str) -> None:
    """Doc-validation error (dangling reference, duplicate/degenerate flow, bad
    cross-device hop, …). Counted so --check fails the run."""
    global _VALIDATION_WARNINGS
    _VALIDATION_WARNINGS += 1
    print(f"warning: {msg}", file=sys.stderr)


def _soft_warn(msg: str) -> None:
    """Advisory lint that does NOT fail --check — e.g. a flow whose source or
    final waypoint is a device interface, which the spec deems acceptable for
    deliberately shallow peripheral docs. Printed for visibility only."""
    global _VALIDATION_SOFT_WARNINGS
    _VALIDATION_SOFT_WARNINGS += 1
    print(f"warning: {msg}", file=sys.stderr)


@dataclass(frozen=True)
class Edge:
    src_key: tuple[str, str]
    dst_key: tuple[str, str]
    direction: str
    payload: str
    # One (token_text, link_or_None) per comma-separated token in `payload`, in
    # the same order. `link` is a flow's path-diagram href ('<stem>.svg') when
    # EXACTLY ONE drawable flow of that payload base crosses this segment, else
    # None (deduped multi-flow tokens, and a path diagram's own self-edges, stay
    # plain text). Empty when no token carries a link — render the label as one
    # element, unchanged. Excluded from equality/hash so it stays a pure render
    # annotation: views/classification compare edges by topology + payload.
    tokens: tuple = field(default=(), compare=False)


def _fmt_key(key: tuple[str, str]) -> str:
    return f"{key[0]} > {key[1]}"


def _heading_for(device, component, iface):
    if component is None:
        return f"{device.name} > {iface.name}"
    return f"{device.name} > {component.name} > {iface.name}"


def merge_labels(payloads: list[str]) -> str:
    """Deduplicate payload bases into one arrow label, preserving first-seen
    order: ['MAVLink', 'MAVLink', 'SBus RC'] -> 'MAVLink, SBus RC'. Payload bases
    are already clean — the parenthetical detail lives in each flow's bold label
    (doc text only, never a diagram), so there is nothing to strip or merge."""
    seen: list[str] = []
    for raw in payloads:
        p = raw.strip()
        if p and p not in seen:
            seen.append(p)
    return ", ".join(seen)


def _flow_name(f: Flow) -> str:
    """Human-readable flow identity for warnings: '<payload> (<label>)', or just
    '<payload>' for the sole unlabeled flow of a base."""
    return f"{f.payload} ({f.label})" if f.label else f.payload


def _flow_stem(f: Flow) -> str:
    """The stem of a flow's path-diagram SVG — the same name the flow-render task
    and hooks.py derive, so an edge-label link resolves to the file that flow
    actually emits."""
    return embed.qualified_name(f.subsystem, *f.source[1].split(" > "), f.payload, f.label)


def _aggregate_stem(payload: str, flow_stems) -> str:
    """Deterministic stem for the aggregate diagram of a multi-flow token — the
    diagram showing every flow that token represents. Keyed by the flow SET, so
    identical sets across segments share one diagram; the 'multiflow' marker lets
    hooks.py recognise these synthetic, edge-label-only diagrams (no doc heading
    places them)."""
    digest = hashlib.sha1("\n".join(sorted(flow_stems)).encode()).hexdigest()[:10]
    return embed.qualified_name("multiflow", payload, digest)


def _payload_tokens(payloads: list[str], stems: dict[str, set[str]], drawable_stems) -> tuple:
    """One (text, link) per deduplicated payload token, in label order. EVERY
    token is clickable: a single-flow token links to that flow's path diagram
    ('<stem>.svg'); a token deduplicated from several flows links to a synthetic
    aggregate diagram showing them all ('<aggregate-stem>.svg')."""
    out = []
    for p in payloads:
        flows = stems.get(p, set())
        link = None
        if len(flows) == 1:
            (stem,) = tuple(flows)
            if stem in drawable_stems:
                link = f"{stem}.svg"
        elif len(flows) > 1:
            link = f"{_aggregate_stem(p, flows)}.svg"
        out.append((p, link))
    return tuple(out)


def _resolve_chain(flow: Flow, index, parsed_subs: set[str]) -> list[tuple[tuple[str, str], tuple[str, str]]] | None:
    """Resolve source + waypoints to [(canonical_key, device_identity), ...].
    Unresolvable waypoints (subsystem without a doc) keep their raw key and
    act as their own device identity (they become stub endpoints downstream).
    Returns None (after warning) when a waypoint dangles into a parsed subsystem."""
    chain = []
    src = index.get(flow.source)
    if src is None:
        chain.append((flow.source, flow.source))
    else:
        chain.append((flow.source, (src[0].subsystem, src[0].name)))
    for ref in flow.waypoints:
        hit = index.get((ref.subsystem, ref.heading))
        if hit is None:
            if ref.subsystem in parsed_subs:
                _warn(
                    f"dangling waypoint in flow '{_flow_name(flow)}' "
                    f"(source '{_fmt_key(flow.source)}'): '{ref.heading}' not "
                    f"found in '{ref.subsystem}' — flow dropped"
                )
                return None
            chain.append(((ref.subsystem, ref.heading), (ref.subsystem, ref.heading)))
            continue
        dev, comp, iface = hit
        chain.append(((dev.subsystem, _heading_for(dev, comp, iface)), (dev.subsystem, dev.name)))
    return chain


def trace_flows(
    flows: list[Flow],
    sys_: System,
    device: str | None,
    component: str | None,
    parsed_subs: set[str],
    iface_key: tuple[str, str] | None = None,
    token_links: dict | None = None,
) -> tuple[list[Flow], list[Edge]]:
    """Flows touching the focus (any waypoint on the device/component), plus
    their chains as edges. A segment's labels are merged into one (grouped by
    payload base, like the subsystem diagrams), not stacked. Focus on a
    component matches its interfaces only; focus on a device matches both its
    device-level and component-level interfaces. When both `device` and
    `component` are given, the component is qualified to that device, so
    same-named components on different devices trace separately. When
    `iface_key` is given it overrides device/component focus: a flow hits iff
    its resolved chain contains exactly that interface key (the source,
    intermediate, or final waypoint — all count), and the whole chain is still
    rendered, so the diagram shows each passing flow end-to-end."""
    index = sys_.by_interface()
    # Resolve each flow once to avoid double-warn from _resolve_chain.
    resolved: list[tuple[Flow, list]] = []
    for f in sorted(flows, key=lambda f: (f.subsystem, f.source)):
        chain = _resolve_chain(f, index, parsed_subs)
        if chain is not None:
            resolved.append((f, chain))

    hits: list[Flow] = []
    hit_chains: list[list] = []
    for f, chain in resolved:
        for key, _dev in chain:
            if iface_key is not None:
                if key == iface_key:
                    hits.append(f)
                    hit_chains.append(chain)
                    break
                continue
            hit = index.get(key)
            if hit is None:
                continue
            dev, comp, _ = hit
            if component is not None:
                if comp is not None and comp.name == component and (device is None or dev.name == device):
                    hits.append(f)
                    hit_chains.append(chain)
                    break
            elif device is not None and dev.name == device:
                hits.append(f)
                hit_chains.append(chain)
                break

    segs: dict[frozenset, dict] = {}
    order: list[frozenset] = []
    for f, chain in zip(hits, hit_chains, strict=False):
        for (a_key, a_dev), (b_key, b_dev) in itertools.pairwise(chain):
            if a_key == b_key:
                continue
            # Same rule as derive_edges: a traversal whose endpoints share a
            # drawable box — both bare-device interfaces of one device, or two
            # interfaces of one component (a process routing in→out) — is
            # implied by that box; drawing it just adds a port-to-port loop.
            # Traversals between different components, or device↔component, draw.
            if a_dev == b_dev:
                ha, hb = index.get(a_key), index.get(b_key)
                if ha is not None and hb is not None and ha[1] is hb[1]:
                    continue
            pair = frozenset({a_key, b_key})
            if pair not in segs:
                segs[pair] = {"src": a_key, "dst": b_key, "dir": "out", "labels": []}
                order.append(pair)
            s = segs[pair]
            if s["src"] != a_key:
                s["dir"] = "both"
            if f.payload not in s["labels"]:
                s["labels"].append(f.payload)
    # Merge a segment's payload bases into one deduplicated label, like the
    # subsystem diagrams. A token links to the FULL-graph diagram for that
    # (segment, payload) — looked up in token_links — so a focused view and the
    # system view open the same diagram (and never a never-generated subset).
    links = token_links or {}
    edges = [
        Edge(
            src_key=s["src"],
            dst_key=s["dst"],
            direction=s["dir"],
            payload=merge_labels(s["labels"]),
            tokens=tuple((p, links.get((frozenset({s["src"], s["dst"]}), p))) for p in s["labels"]),
        )
        for s in (segs[p] for p in order)
    ]
    return hits, edges


def single_flow_edges(flow: Flow, chain: list, index: dict) -> list[Edge]:
    """Edges for ONE flow's chain (a path diagram). Same per-segment rules as
    trace_flows/derive_edges: skip same-box internal traversals; label each
    segment with the flow's payload base (no hop number)."""
    title = flow.payload
    segs: dict[frozenset, dict] = {}
    order: list[frozenset] = []
    for (a_key, a_dev), (b_key, b_dev) in itertools.pairwise(chain):
        if a_key == b_key:
            continue
        if a_dev == b_dev:
            ha, hb = index.get(a_key), index.get(b_key)
            if ha is not None and hb is not None and ha[1] is hb[1]:
                continue
        pair = frozenset({a_key, b_key})
        if pair not in segs:
            segs[pair] = {"src": a_key, "dst": b_key, "dir": "out"}
            order.append(pair)
        elif segs[pair]["src"] != a_key:
            segs[pair]["dir"] = "both"
    return [
        Edge(src_key=segs[p]["src"], dst_key=segs[p]["dst"], direction=segs[p]["dir"], payload=title) for p in order
    ]


def derive_edges(flows: list[Flow], sys_: System, drawable_stems=frozenset(), token_links=None) -> list[Edge]:
    """Derive the point-to-point edge list from flow chains: each consecutive
    waypoint pair is a segment (a wire when the devices differ; an internal
    traversal when the same device — drawn only when its endpoints sit in
    different boxes, since a traversal within one box, device→device forwarding
    or a component routing in→out, is implied and a drawn loop just clutters
    the box). Direction is the flow's;
    opposite traversals of the same segment merge to 'both'. A segment's label
    is the deduplicated payload bases of the flows crossing it (merge_labels).
    Flows are processed in canonically sorted order so edge orientation and
    label order are first-encounter order over that sort — independent of CLI
    argument/parse order."""
    index = sys_.by_interface()
    parsed_subs = {d.subsystem for d in sys_.devices}
    segs: dict[frozenset, dict] = {}
    order: list[frozenset] = []
    seen_flows: set[tuple] = set()
    # Deliberately NOT keyed on payload: flows sharing a source keep their
    # in-doc declaration order (stable sort), so merged labels read in the order
    # the author wrote them (tested by render-label).
    for f in sorted(flows, key=lambda f: (f.subsystem, f.source)):
        if f.label is None:
            # Every flow is a '**label**' under its payload-base heading. A
            # label-less flow comes from a numbered list with no preceding bold
            # line — a malformed payload-base section.
            _warn(
                f"flow under payload '{f.payload}' on '{_fmt_key(f.source)}' has "
                f"no '**label**' — every flow needs a bold label"
            )
        fid = (f.source, f.payload, f.label)
        if fid in seen_flows:
            _warn(f"duplicate flow: '{_flow_name(f)}' declared more than once on '{_fmt_key(f.source)}'")
        seen_flows.add(fid)
        if not f.waypoints:
            _warn(f"degenerate flow '{_flow_name(f)}' on '{_fmt_key(f.source)}': empty chain")
            continue
        chain = _resolve_chain(f, index, parsed_subs)
        if chain is None:
            continue
        # endpoint lint: processes consume data, devices don't — UNLESS the
        # device has no components at all (a microcontroller is its own
        # firmware; there's no process to name). hit = (device, component, _).
        for label, key in (("source", chain[0][0]), ("final waypoint", chain[-1][0])):
            hit = index.get(key)
            if hit is not None and hit[1] is None and hit[0].components:
                _soft_warn(
                    f"flow '{_flow_name(f)}': {label} '{_fmt_key(key)}' is a device "
                    f"interface — devices aren't consumers, name the component"
                )
        for (a_key, a_dev), (b_key, b_dev) in itertools.pairwise(chain):
            if a_key == b_key:
                _warn(f"degenerate stage in flow '{_flow_name(f)}': consecutive identical waypoint '{_fmt_key(a_key)}'")
                continue
            # An internal traversal whose two endpoints belong to the SAME
            # drawable box — both bare-device interfaces of one device (a
            # hub/bridge forwarding in→out), or two interfaces of one component
            # (a process routing in→out) — is implied by that shared box;
            # drawing it just adds a port-to-port loop. Skip it (the in/out
            # wires still show the path through the box). Traversals between
            # DIFFERENT components, or device↔component, still draw.
            if a_dev == b_dev:
                ha, hb = index.get(a_key), index.get(b_key)
                if ha is not None and hb is not None and ha[1] is hb[1]:
                    continue
            # Wire segments (between different devices) must connect device
            # interfaces on both ends — components interface only within their
            # own device (spec). A cross-device hop onto a component port is a
            # modeling error (and ELK can't lay it out across subsystems).
            if a_dev != b_dev:
                for k in (a_key, b_key):
                    h = index.get(k)
                    if h is not None and h[1] is not None:
                        _warn(
                            f"flow '{_flow_name(f)}': cross-device segment touches "
                            f"component interface '{_fmt_key(k)}' — devices "
                            f"interface via device interfaces; route through a "
                            f"device interface, then traverse to the component"
                        )
            pair = frozenset({a_key, b_key})
            if pair not in segs:
                segs[pair] = {"src": a_key, "dst": b_key, "dir": "out", "payloads": [], "stems": {}}
                order.append(pair)
            s = segs[pair]
            if s["src"] != a_key:
                s["dir"] = "both"  # traversed in the opposite direction too
            s["stems"].setdefault(f.payload, set()).add(_flow_stem(f))
            if f.payload not in s["payloads"]:
                s["payloads"].append(f.payload)

    def _tokens(s):
        # The full pass (token_links=None) computes each token's link from its
        # own flow set; a derived view (an aggregate diagram) instead looks the
        # link up by (segment, payload) so it opens the SAME full-graph diagram.
        if token_links is not None:
            pair = frozenset({s["src"], s["dst"]})
            return tuple((p, token_links.get((pair, p))) for p in s["payloads"])
        return _payload_tokens(s["payloads"], s["stems"], drawable_stems)

    return [
        Edge(
            src_key=s["src"],
            dst_key=s["dst"],
            direction=s["dir"],
            payload=merge_labels(s["payloads"]),
            tokens=_tokens(s),
        )
        for s in (segs[p] for p in order)
    ]


def aggregate_diagrams(flows: list[Flow], sys_: System) -> dict[str, list[Flow]]:
    """Map each multi-flow payload token (a segment carrying >1 flow of one
    payload base) to the flows it represents, keyed by the aggregate-diagram
    stem. Identical flow sets share one entry, so the generator emits each
    aggregate once. Same per-segment rules as derive_edges."""
    index = sys_.by_interface()
    parsed_subs = {d.subsystem for d in sys_.devices}
    seg: dict[tuple, dict[str, Flow]] = {}  # (pair, payload) -> {flow_stem: flow}
    for f in flows:
        chain = _resolve_chain(f, index, parsed_subs)
        if chain is None:
            continue
        for (a_key, a_dev), (b_key, b_dev) in itertools.pairwise(chain):
            if a_key == b_key:
                continue
            if a_dev == b_dev:
                ha, hb = index.get(a_key), index.get(b_key)
                if ha is not None and hb is not None and ha[1] is hb[1]:
                    continue
            seg.setdefault((frozenset({a_key, b_key}), f.payload), {})[_flow_stem(f)] = f
    aggs: dict[str, list[Flow]] = {}
    for (_pair, payload), by_stem in seg.items():
        if len(by_stem) > 1:
            aggs[_aggregate_stem(payload, set(by_stem))] = sorted(
                by_stem.values(), key=lambda f: (f.subsystem, f.source, f.payload, f.label or "")
            )
    return aggs


def drawable_flow_stems(flows: list[Flow], sys_: System, unresolved: set[str]) -> set[str]:
    """Stems of the flows whose path diagram actually gets emitted — a
    resolvable chain with at least one drawn segment, in a parsed subsystem.
    Mirrors the flow-render task's own skip conditions (compute_path), so an
    edge-label link only ever points at a path diagram that was written."""
    index = sys_.by_interface()
    parsed_subs = {d.subsystem for d in sys_.devices}
    out: set[str] = set()
    for f in flows:
        if f.subsystem in unresolved:
            continue
        chain = _resolve_chain(f, index, parsed_subs)
        if chain is not None and single_flow_edges(f, chain, index):
            out.add(_flow_stem(f))
    return out


# ---------------------------------------------------------------------------
# Edge classification (per-diagram)
# ---------------------------------------------------------------------------


@dataclass
class RenderEdge:
    """An Edge plus how this particular diagram should draw it: normally
    (stub_key is None) or dotted to a text-only stub standing in for the
    off-graph endpoint (stub_key = that endpoint's canonical key)."""

    edge: Edge
    stub_key: tuple[str, str] | None = None


def port_keys_for(sys_: System) -> set[tuple[str, str]]:
    """Canonical (subsystem, heading) key of every interface in `sys_` — the
    set of edge endpoints this diagram can attach arrows to."""
    keys: set[tuple[str, str]] = set()
    for d in sys_.devices:
        for i in d.interfaces:
            keys.add((d.subsystem, _heading_for(d, None, i)))
        for c in d.components:
            for i in c.interfaces:
                keys.add((d.subsystem, _heading_for(d, c, i)))
    return keys


def classify_edges(
    edges: list[Edge], port_keys: set, collapsed: set[str], unresolved: set[str] = frozenset()
) -> list[RenderEdge]:
    """Decide, for one diagram, which edges draw normally, which draw dotted
    to an off-graph stub, and which drop entirely.

    `port_keys` is what THIS diagram renders; `unresolved` holds subsystems
    referenced by some doc but without a doc of their own. Stubs are reserved
    for endpoints that can't be FOUND (unresolved subsystem); endpoints that
    exist in the parsed closure but aren't rendered by this view (a collapsed
    neighbor's edge to a subsystem outside the view, a focused render's
    filtered-out peer, a collapsed subsystem's internals) drop with their
    edges — they're second-degree detail, visible in the view that renders
    them. Dangling wikilinks within a parsed doc also drop (warned at parse).
    """
    out: list[RenderEdge] = []
    for e in edges:
        src_in = e.src_key in port_keys
        dst_in = e.dst_key in port_keys
        if src_in and dst_in:
            # Both endpoints kept but the edge never leaves a collapsed
            # subsystem → internal detail, hidden by the collapse.
            if e.src_key[0] == e.dst_key[0] and e.src_key[0] in collapsed:
                continue
            out.append(RenderEdge(e))
        elif src_in or dst_in:
            missing = e.dst_key if src_in else e.src_key
            if missing[0] in unresolved:
                out.append(RenderEdge(e, stub_key=missing))
            # else: resolvable but not rendered by this view — drop
    return out


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def chain_view(sys_: System, edges: list[Edge]) -> System:
    """Sub-System containing only the devices/components/interfaces that
    appear as endpoints of the traced edges."""
    keys = {e.src_key for e in edges} | {e.dst_key for e in edges}
    out = System(name=sys_.name, display_names=sys_.display_names)
    for d in sys_.devices:
        new_dev = Device(name=d.name, subsystem=d.subsystem)
        new_dev.interfaces = [i for i in d.interfaces if (d.subsystem, _heading_for(d, None, i)) in keys]
        for c in d.components:
            nc = Component(name=c.name)
            nc.interfaces = [i for i in c.interfaces if (d.subsystem, _heading_for(d, c, i)) in keys]
            if nc.interfaces:
                new_dev.components.append(nc)
        if new_dev.interfaces or new_dev.components:
            out.devices.append(new_dev)
    return out


def filter_system(sys_, subsystems, devices, components) -> System:
    out = System(name=sys_.name, display_names=sys_.display_names)
    for d in sys_.devices:
        if subsystems and d.subsystem not in subsystems:
            continue
        if devices and d.name not in devices:
            continue
        new_d = Device(name=d.name, subsystem=d.subsystem, interfaces=list(d.interfaces))
        for c in d.components:
            if components and c.name not in components:
                continue
            new_d.components.append(c)
        out.devices.append(new_d)
    return out


def collapse_system(sys_: System, collapsed: set[str], edges: list[Edge], focus: set[str] | None = None) -> System:
    """Reduce each collapsed subsystem to its boundary-crossing surface.

    Components drop wholesale (cross-device edges only attach to device
    interfaces, per the spec), device interfaces survive only when at
    least one of their edges crosses to ANOTHER SUBSYSTEM IN THIS VIEW
    (crossings to out-of-view subsystems are second-degree detail and drop
    with their edges), and devices with no surviving interfaces disappear.
    The subsystem boundary itself stays — its label links to the subsystem's
    detail diagram.

    `focus` is the set of expanded subsystem(s) a subsystem-detail diagram is
    built around. When given, a collapsed neighbour keeps only the interfaces
    that cross to the focus; a lateral neighbour↔neighbour link (and any port
    that exists solely for it) is second-degree detail relative to the focus
    and drops. Left None for the focus-less system diagram (all subsystems
    collapsed), which keeps every link between two drawn boxes.
    """
    view_subs = {d.subsystem for d in sys_.devices}
    crossing: set[tuple[str, str]] = set()
    for e in edges:
        a, b = e.src_key[0], e.dst_key[0]
        if a == b or a not in view_subs or b not in view_subs:
            continue
        if focus is not None and a not in focus and b not in focus:
            continue
        crossing.add(e.src_key)
        crossing.add(e.dst_key)

    out = System(name=sys_.name, display_names=sys_.display_names)
    for d in sys_.devices:
        if d.subsystem not in collapsed:
            out.devices.append(d)
            continue
        kept = [i for i in d.interfaces if (d.subsystem, _heading_for(d, None, i)) in crossing]
        if kept:
            out.devices.append(Device(name=d.name, subsystem=d.subsystem, interfaces=kept))
    return out


# ---------------------------------------------------------------------------
# ELK spec generation
# ---------------------------------------------------------------------------

# Two parallel palettes — saturated (subsystem outline + label) and pastel
# (device fill, same hue). Both are picked as if drawing on a WHITE canvas; the
# mkdocs dark theme inverts the SVG (a luminance-invert + hue-rotate CSS filter,
# see extra.css), so light-native colors come out correctly in dark mode
# (saturated stays saturated; pastel becomes dark).
SUBSYSTEM_PALETTE_BRIGHT = [
    "#1c7ed6",  # saturated blue
    "#37b24d",  # saturated green
    "#f76707",  # saturated orange
    "#7048e8",  # saturated purple
    "#e03131",  # saturated red
    "#f59f00",  # saturated amber
    "#1098ad",  # saturated teal
    "#d6336c",  # saturated pink
]
SUBSYSTEM_PALETTE_DARK = [
    "#a5d8ff",  # light blue
    "#b2f2bb",  # light green
    "#ffd8a8",  # light orange
    "#d0bfff",  # light purple
    "#ffc9c9",  # light red
    "#fff3bf",  # light yellow
    "#c3fae8",  # light teal
    "#eebefa",  # light pink
]
# Even lighter than the device pastels — the dark-mode invert turns these into
# darker-than-device fills, giving nested components extra visual depth.
COMPONENT_PALETTE = [
    "#e7f5ff",  # very pale blue
    "#ebfbee",  # very pale green
    "#fff4e6",  # very pale orange
    "#f3f0ff",  # very pale purple
    "#fff5f5",  # very pale red
    "#fff9db",  # very pale yellow
    "#e6fcf5",  # very pale teal
    "#fff0f6",  # very pale pink
]


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def _sub_id(name: str) -> str:
    return f"sub__{_slug(name)}"


def _dev_id(device: Device) -> str:
    return f"dev__{_slug(device.subsystem)}__{_slug(device.name)}"


def _comp_id(device: Device, component: Component) -> str:
    return f"comp__{_slug(device.subsystem)}__{_slug(device.name)}__{_slug(component.name)}"


def _port_id_from_key(key: tuple[str, str]) -> str:
    return f"port__{_slug(key[0])}__{_slug(key[1])}"


def _stub_id(key: tuple[str, str]) -> str:
    return f"stub__{_slug(key[0])}__{_slug(key[1])}"


def _stub_label(key: tuple[str, str]) -> str:
    # Full qualification: "Subsystem > Device > Interface" (the heading part
    # already carries "Device > Interface" or "Device > Component > Interface").
    # When the device is named like its subsystem (single-device subsystems
    # such as "Developer machine"), skip the redundant prefix.
    if key[1].startswith(f"{key[0]} > "):
        return key[1]
    return f"{key[0]} > {key[1]}"


def _stub_keys(redges: list[RenderEdge]) -> dict[tuple[str, str], str]:
    """Deduped stub endpoints → stub node id. Several paths to the same
    external interface share one stub."""
    return {re_.stub_key: _stub_id(re_.stub_key) for re_ in redges if re_.stub_key}


_FONT_PATH = _workers.fonts_dir() / "Nunito-Regular.woff2"
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}
# Diagrams render in parallel (a thread per diagram, see main()), but a Pillow
# FreeTypeFont wraps a single FreeType face that is NOT safe to call
# concurrently. Measurement is cheap relative to the node render round-trip, so
# serializing every cache load + metric query behind one lock costs ~nothing and
# removes the data race.
_FONT_LOCK = threading.Lock()


def _font(size_px: int) -> ImageFont.FreeTypeFont:
    if size_px not in _FONT_CACHE:
        _FONT_CACHE[size_px] = ImageFont.truetype(str(_FONT_PATH), size_px)
    return _FONT_CACHE[size_px]


def _estimate_text_width(text: str, font_px: int = 12) -> int:
    """Measure the actual rendered width of `text` in Nunito at `font_px`.

    Uses Pillow's font metrics — the woff2 we ship is the same file the SVG
    renderer outlines with, so the measurement matches the rendered glyphs within
    a pixel. The small added constant is breathing room so the box never sits
    flush against the glyphs.
    """
    if not text:
        return 24
    with _FONT_LOCK:
        return round(_font(font_px).getlength(text)) + 4


def _glyph_advance(text: str, font_px: int) -> float:
    """Raw rendered width of `text` with no estimator margin — for tiling split
    edge-label fragments flush so they read identically to the single string."""
    if not text:
        return 0.0
    with _FONT_LOCK:
        return _font(font_px).getlength(text)


def _font_height(font_px: int) -> int:
    """Total rendered line height of Nunito at `font_px`.

    Excalidraw applies lineHeight=1.25, so a fontSize=12 glyph occupies ~15 px
    vertically. Using this for text-element height keeps autoResize from
    shrinking the box and shifting the rendered text off-center.
    """
    with _FONT_LOCK:
        ascent, descent = _font(font_px).getmetrics()
    return ascent + descent


# Half the extra vertical pitch reserved between adjacent ports so their labels
# can't collide (used only by PORT_SPACING_OPTS below — a label may sit toward
# either neighbour, so the pitch is widened by twice this). Port-label vertical
# placement itself is handled in build_excalidraw, seated EDGE_LABEL_GAP off the
# line to mirror the edge payload labels.
PORT_LABEL_OFFSET = 5

# Target clearance between an edge label and its arrow line. ELK places labels
# at spacing+1 px from the edge, so every graph scope that routes edges (root,
# subsystem containers, devices with components) sets elk.spacing.edgeLabel =
# EDGE_LABEL_GAP - 1.
EDGE_LABEL_GAP = 9

# Minimum clearance between a payload label's near edge and the END of the
# segment it sits on (a port + its arrowhead, or a bend). ELK seats inline
# labels on the wire but can place one right at a port, where its backing box
# would sit over the arrowhead; clamp the label along its segment to keep clear.
EDGE_LABEL_END_CLEAR = 18

# Clearance between an arrowhead and the stub text it binds to. Applied both
# to the static ELK-routed geometry (endpoint pulled back along the final
# segment) and to the Excalidraw binding's `gap`.
STUB_ARROW_GAP = 8

# Port pitch knobs. ELK resolves a node's port spacing against its PARENT's
# options, so these go on every scope that contains nodes with ports
# (subsystem containers for devices, devices-with-components for components) —
# setting them on the port-owning node itself does nothing. The resulting
# pitch for edge-connected ports is portPort + port height (8) + port label
# height (14) + 1. Port labels are pushed PORT_LABEL_OFFSET px off the port
# face, so the pitch is widened by twice that (a label can be pushed toward
# either neighbour) to keep adjacent ports' labels from colliding.
# portsSurrounding keeps the first/last port inset from the node corners. The
# top inset is the largest: it pushes the topmost port (and any edge routing to
# or transiting it) down clear of the device/component label, which sits inside
# the box's top edge.
PORT_SPACING_OPTS = {
    "elk.spacing.portPort": str(1 + 2 * PORT_LABEL_OFFSET),
    "elk.spacing.portsSurrounding": "[top=15,left=10,bottom=10,right=10]",
}


def emit_elk_spec(sys_: System, redges: list[RenderEdge], collapsed: set[str] = frozenset()) -> dict:
    """Build an ELK JSON graph: root → subsystem containers → device nodes with
    ports, plus root-level text-only stub nodes for off-graph edge endpoints."""

    by_subsystem: dict[str, list[Device]] = {}
    for d in sys_.devices:
        by_subsystem.setdefault(d.subsystem, []).append(d)

    children = []
    for sub_name in sorted(by_subsystem.keys()):
        sub_children = []
        for d in by_subsystem[sub_name]:
            device_ports = [_port_node((d.subsystem, _heading_for(d, None, i)), i.name) for i in d.interfaces]

            # Components become nested sub-containers; their interfaces (if any)
            # become ports on the component, not the device. Components without
            # interfaces still render as labeled boxes inside the device.
            component_children = []
            for c in d.components:
                comp_ports = [_port_node((d.subsystem, _heading_for(d, c, i)), i.name) for i in c.interfaces]
                comp_label_w = _estimate_text_width(c.name, 12)
                component_children.append(
                    {
                        "id": _comp_id(d, c),
                        "labels": [
                            {
                                "text": c.name,
                                "width": comp_label_w,
                                "height": 16,
                            }
                        ],
                        "layoutOptions": {
                            "elk.portConstraints": "FREE",
                            # ALWAYS_SAME_SIDE + nextToPortIfPossible: see
                            # dev_layout below.
                            "elk.portLabels.placement": "OUTSIDE ALWAYS_SAME_SIDE",
                            "elk.portLabels.nextToPortIfPossible": "true",
                            # Size to label + ports: label-only sizing kept the box
                            # at a fixed ~26px height, cramming multi-port
                            # components (ports overlapped on the border). PORTS
                            # PORT_LABELS makes ELK grow the box to honour the
                            # portPort spacing, same as plain devices.
                            "elk.nodeSize.constraints": "NODE_LABELS PORTS PORT_LABELS",
                            "elk.nodeSize.options": "ASYMMETRICAL",
                            # Top-left, matching devices and subsystems.
                            "elk.nodeLabels.placement": "INSIDE V_TOP H_LEFT",
                            "elk.spacing.labelPort": "8",
                        },
                        "ports": comp_ports,
                    }
                )

            dev_layout = {
                "elk.portConstraints": "FREE",
                # ALWAYS_SAME_SIDE: without it ELK's space-efficient label
                # mode flips the first label above the port on two-port sides
                # and packs those ports at a tighter pitch than 3+-port sides
                # get — inconsistent spacing across the diagram. Forcing every
                # edge-connected port's label below its port makes all sides
                # use the same pitch. nextToPortIfPossible only kicks in for
                # ports with no edge: their label has nothing to collide with,
                # so it sits vertically centered beside the port.
                "elk.portLabels.placement": "OUTSIDE ALWAYS_SAME_SIDE",
                "elk.portLabels.nextToPortIfPossible": "true",
                # Let ELK size each device to fit its label + ports + port
                # labels. ASYMMETRICAL stops ELK from mirroring the label's
                # left padding on the right side (the default behaviour, which
                # was doubling every empty device's width).
                "elk.nodeSize.constraints": "NODE_LABELS PORTS PORT_LABELS",
                "elk.nodeSize.options": "ASYMMETRICAL",
                # Device labels always sit in the top-left so devices with and
                # without nested components read consistently.
                "elk.nodeLabels.placement": "INSIDE V_TOP H_LEFT",
                # One uniform inner padding. ELK uses spacing.labelNode (default
                # 5) to separate the label from child components, so we don't
                # have to inflate the top padding for the "has children" case.
                "elk.padding": "[top=8,left=8,bottom=8,right=8]",
            }
            if component_children:
                dev_layout["elk.spacing.nodeNode"] = "30"
                # Port pitch for the nested components (parent-resolved, see
                # PORT_SPACING_OPTS) — same values the subsystem containers
                # set for devices.
                dev_layout.update(PORT_SPACING_OPTS)
                # Same edge-label clearance as the root graph — this device's
                # internal edges lay out separately (SEPARATE_CHILDREN below).
                dev_layout["elk.spacing.edgeLabel"] = str(EDGE_LABEL_GAP - 1)
                # Each device-with-components is its own little layout problem.
                # SEPARATE_CHILDREN stops ELK from placing nested components in
                # the same global flow as the outer subsystem layout.
                dev_layout["elk.hierarchyHandling"] = "SEPARATE_CHILDREN"
                # Children + their layout determine the inner extent, so the
                # device's minimum size should ONLY honour the node label.
                dev_layout["elk.nodeSize.constraints"] = "NODE_LABELS"
                # OUTSIDE label placement — same trick subsystems use. With
                # INSIDE placement, ELK reserves Layer 0 of the device for the
                # label and pushes child components into Layer 1, which is what
                # makes AMC sit at the right edge of Galaxy Tab S5. OUTSIDE
                # tells ELK the label isn't internal content, so children pack
                # to the top-left of the device. We then render the label
                # ourselves over the top padding (cf. the subsystem-label
                # rendering path).
                dev_layout["elk.nodeLabels.placement"] = "OUTSIDE V_TOP H_LEFT"
                dev_layout["elk.padding"] = "[top=28,left=8,bottom=8,right=8]"

            dev_node = {
                "id": _dev_id(d),
                "labels": [
                    {
                        "text": d.name,
                        "width": _estimate_text_width(d.name, 14),
                        "height": 18,
                    }
                ],
                "layoutOptions": dev_layout,
                "ports": device_ports,
            }
            if component_children:
                dev_node["children"] = component_children
            sub_children.append(dev_node)

        # The OUTSIDE label never widens the container, so a subsystem whose
        # contents are narrower than its (top-left) label lets the label spill
        # past the dotted box. Floor the container width to fit the label —
        # rendered at +16 from the left edge — plus a right margin.
        sub_display = sys_.display_names.get(sub_name, sub_name)
        sub_label_w = _estimate_text_width(sub_display, 16)
        sub_min_w = sub_label_w + 16 + 16
        children.append(
            {
                "id": _sub_id(sub_name),
                "labels": [{"text": sub_name, "width": sub_label_w, "height": 22}],
                "layoutOptions": {
                    # Tighter inside-subsystem packing — the root nodeNode (100) and
                    # nodeNodeBetweenLayers (140) handle inter-subsystem breathing
                    # room; inside a subsystem we want devices to sit closer.
                    "elk.padding": "[top=40,left=16,bottom=16,right=16]",
                    "elk.spacing.nodeNode": "40",
                    "elk.layered.spacing.nodeNodeBetweenLayers": "10",
                    "elk.nodeLabels.placement": "OUTSIDE V_TOP H_LEFT",
                    "elk.spacing.edgeLabel": str(EDGE_LABEL_GAP - 1),
                    # Floor the width to the label (OUTSIDE labels don't size the
                    # node); content still wins when the devices are wider.
                    "elk.nodeSize.constraints": "MINIMUM_SIZE",
                    "elk.nodeSize.minimum": f"({sub_min_w}, 0)",
                    # Port pitch for the devices inside (parent-resolved).
                    **PORT_SPACING_OPTS,
                },
                "children": sub_children,
            }
        )

    # Off-graph endpoints become root-level stub nodes: sized to their label,
    # rendered downstream as text only. Root level (outside every subsystem
    # container) so ELK's layered layout places them at the diagram edge.
    stub_id_for = _stub_keys(redges)
    for key, sid in stub_id_for.items():
        label = _stub_label(key)
        # Explicit node size (like port nodes) — the node IS the label, and
        # arrows route to the node border, so letting ELK size it from the
        # label would add its default label padding as a gap between
        # arrowhead and text.
        w = _estimate_text_width(label, 11)
        h = _font_height(11)
        children.append(
            {
                "id": sid,
                "width": w,
                "height": h,
                "labels": [{"text": label, "width": w, "height": h}],
            }
        )

    elk_edges = []
    valid_ports = _all_port_ids(children)
    for n, re_ in enumerate(redges):
        e = re_.edge
        src = stub_id_for[e.src_key] if re_.stub_key == e.src_key else _port_id_from_key(e.src_key)
        dst = stub_id_for[e.dst_key] if re_.stub_key == e.dst_key else _port_id_from_key(e.dst_key)
        if (not src.startswith("stub__") and src not in valid_ports) or (
            not dst.startswith("stub__") and dst not in valid_ports
        ):
            continue  # endpoint dropped by a filter and not stubbed
        label_w = _estimate_text_width(e.payload, 10)
        label_h = 14
        elk_edges.append(
            {
                "id": f"e_{n}",
                "sources": [src],
                "targets": [dst],
                "labels": [
                    {
                        "text": e.payload,
                        "width": label_w,
                        "height": label_h,
                        # Seat the payload label ON the wire, not above/below it (the
                        # label's halo masks the line behind the text). Per-label option;
                        # ELK ignores it when set on the graph root.
                        "layoutOptions": {"org.eclipse.elk.edgeLabels.inline": "true"},
                    }
                ]
                if e.payload
                else [],
            }
        )

    return {
        "id": "root",
        "layoutOptions": {
            "elk.algorithm": "layered",
            "elk.direction": "RIGHT",
            "elk.hierarchyHandling": "INCLUDE_CHILDREN",
            "elk.spacing.nodeNode": "50",
            "elk.layered.spacing.nodeNodeBetweenLayers": "10",
            "elk.padding": "[top=20,left=20,bottom=20,right=20]",
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.layered.mergeEdges": "true",
            "elk.spacing.edgeLabel": str(EDGE_LABEL_GAP - 1),
        },
        "children": children,
        "edges": elk_edges,
    }


def _port_node(key, label_text):
    return {
        "id": _port_id_from_key(key),
        "labels": [
            {
                "text": label_text,
                "width": _estimate_text_width(label_text, 11),
                "height": 14,
            }
        ],
        "width": 8,
        "height": 8,
    }


def _all_port_ids(children) -> set[str]:
    """Collect every port id in the tree — ports can live at any depth now that
    components nest inside devices, so this has to recurse."""
    ids: set[str] = set()

    def walk(node):
        for p in node.get("ports", []):
            ids.add(p["id"])
        for c in node.get("children", []):
            walk(c)

    for sub in children:
        walk(sub)
    return ids


class _Worker:
    """One long-lived `node <script>` process speaking line-delimited JSON:
    write one request line, read one response line. Importing elkjs / the
    Excalidraw bundle costs ~0.2–1s, so reusing the process across many diagrams
    is what turns a multi-minute run into a short one."""

    def __init__(self, script: Path):
        self.proc = subprocess.Popen(
            [_workers.resolve_node(), str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered stdin
            env={**os.environ, "INTERFACE_DIAGRAMS_FONTS": str(_workers.fonts_dir())},
        )

    def call(self, payload: dict) -> dict:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:  # worker died (EOF on stdout)
            raise RuntimeError(f"worker {self.proc.args[1]} exited unexpectedly")
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.wait()


class WorkerPool:
    """A fixed set of identical workers leased one-at-a-time. `call` blocks for a
    free worker, so with N workers up to N requests run concurrently — the rest
    queue. Each worker handles one request at a time, so a leased worker is never
    shared across threads."""

    def __init__(self, script: Path, size: int):
        self._idle: queue.Queue[_Worker] = queue.Queue()
        self._workers = [_Worker(script) for _ in range(size)]
        for w in self._workers:
            self._idle.put(w)

    def call(self, payload: dict) -> dict:
        w = self._idle.get()
        try:
            return w.call(payload)
        finally:
            self._idle.put(w)

    def close(self):
        for w in self._workers:
            w.close()


# Worker pools, created in main() once the worker count is known. run_elk /
# render_svg (called from every compute_* / emit, across threads) route through
# them instead of spawning a fresh `node` per diagram.
_ELK_POOL: WorkerPool | None = None
_RENDER_POOL: WorkerPool | None = None


def require_render_toolchain() -> None:
    """Fail fast, with the fix, when the node side isn't ready. Rendering needs
    node 20+. Validation (--check) never calls this — it needs no node at all."""
    try:
        _workers.check_node(_workers.resolve_node())
    except RuntimeError as e:
        raise SystemExit(f"error: {e}")
    elk_bundle = _workers.bundle_path("elk_layout.bundle.mjs")
    render_bundle = _workers.bundle_path("render_svg.bundle.mjs")
    if not elk_bundle.is_file() or not render_bundle.is_file():
        raise SystemExit(
            "error: bundled JS workers not found — ensure the package was installed "
            "correctly (the bundles ship inside interface_diagrams/_js/)."
        )


def run_elk(spec: dict) -> dict:
    resp = _ELK_POOL.call(spec)
    if not resp.get("ok"):
        sys.stderr.write(resp.get("error", "elk_layout.mjs failed") + "\n")
        raise SystemExit("elk_layout.mjs failed")
    return resp["result"]


# A back-edge (one ELK placed with its source to the RIGHT of its target, i.e.
# running against the RIGHT layout flow) whose routed horizontal extent
# overshoots its two endpoints' own span by more than this many px has been
# wrapped the long way around the diagram: ELK's hierarchical orthogonal router
# can't thread a long right-to-left edge through the layer band, so it sends it
# out to the margins and around the perimeter. None of ELK's layering/routing
# options fix this (the forward router is fine; only the reverse one wraps), so
# `layout` heals it by feeding such an edge to ELK reversed. The gap between
# real wrappers (>900px overshoot in practice) and well-routed edges is wide, so
# this threshold is not delicate.
WRAP_OVERSHOOT_PX = 400


def _abs_positions(laid: dict) -> dict:
    """Absolute (x, y, w, h) for every node and port in a laid-out graph. ELK
    reports positions relative to the parent container, so sum offsets while
    walking the tree."""
    pos: dict = {}

    def walk(node, ox, oy):
        x = ox + node.get("x", 0)
        y = oy + node.get("y", 0)
        nid = node.get("id")
        if nid:
            pos[nid] = (x, y, node.get("width", 0), node.get("height", 0))
        for p in node.get("ports", []):
            pos[p["id"]] = (x + p.get("x", 0), y + p.get("y", 0), p.get("width", 0), p.get("height", 0))
        for c in node.get("children", []):
            walk(c, x, y)

    for c in laid.get("children", []):
        walk(c, 0, 0)
    return pos


def _wrapped_back_edges(laid: dict) -> set:
    """Edge ids ELK routed as a long right-to-left detour (see
    WRAP_OVERSHOOT_PX): source port right of target port AND the route's
    horizontal span overshoots the endpoints' own span."""
    pos = _abs_positions(laid)
    wrapped: set = set()

    def walk(node):
        for e in node.get("edges", []):
            secs = e.get("sections", [])
            if not secs:
                continue
            xs = [p["x"] for sec in secs for p in [sec["startPoint"], *sec.get("bendPoints", []), sec["endPoint"]]]
            route_span = max(xs) - min(xs)
            s = pos.get(e["sources"][0])
            t = pos.get(e["targets"][0])
            if not s or not t:
                continue
            endpoint_span = max(s[0], s[0] + s[2], t[0], t[0] + t[2]) - min(s[0], s[0] + s[2], t[0], t[0] + t[2])
            if s[0] > t[0] + 5 and route_span - endpoint_span > WRAP_OVERSHOOT_PX:
                wrapped.add(e["id"])
        for c in node.get("children", []):
            walk(c)

    walk(laid)
    return wrapped


def layout(spec: dict) -> tuple:
    """Lay the ELK graph out, then heal edges the hierarchical router wrapped
    around the diagram perimeter: swap each wrapped back-edge's source/target so
    ELK sees a forward edge (which it routes straight) and lay out once more.
    Returns (laid_out, reversed_edge_ids); build_excalidraw flips the arrowhead
    on the reversed edges so their drawn direction is unchanged."""
    laid = run_elk(spec)
    wrapped = _wrapped_back_edges(laid)
    if not wrapped:
        return laid, frozenset()
    for e in spec["edges"]:
        if e["id"] in wrapped:
            e["sources"], e["targets"] = e["targets"], e["sources"]
    return run_elk(spec), frozenset(wrapped)


def render_svg(elements: list[dict]) -> str:
    """Render Excalidraw elements to a self-contained SVG via the render worker."""
    resp = _RENDER_POOL.call({"elements": elements})
    if not resp.get("ok"):
        raise RuntimeError(f"render_svg.mjs failed: {resp.get('error', '').strip()}")
    return resp["svg"]


# ---------------------------------------------------------------------------
# Excalidraw output
# ---------------------------------------------------------------------------

EXCALIDRAW_FONT = 2  # Nunito (the clean, non-hand-drawn family)


def _next_seed(rng):
    return rng.randint(1, 2_000_000_000)


def _base(rng):
    return {
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        # roughness 0 = the clean "architect" style for every shape: crisp
        # rectangles, straight path lines, sharp arrowheads — no hand-drawn jitter.
        "roughness": 0,
        "opacity": 100,
        "groupIds": [],
        "roundness": {"type": 3},
        "seed": _next_seed(rng),
        "version": 1,
        "isDeleted": False,
        "boundElements": None,
        "updated": 1,
        "link": None,
        "locked": False,
    }


def _rect(
    rng, eid, x, y, w, h, fill, stroke="#1e1e1e", stroke_width=2, stroke_style="solid", square=False, group_ids=None
):
    rect = {
        "id": eid,
        "type": "rectangle",
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        **_base(rng),
        "strokeColor": stroke,
        "strokeWidth": stroke_width,
        "strokeStyle": stroke_style,
        "backgroundColor": fill,
        "groupIds": list(group_ids) if group_ids else [],
    }
    if square:
        rect["roundness"] = None
    return rect


def _normalize_luminance(hex_color, target=0.62):
    """Push a colour to a uniform luminance (blend toward white if it's too dark,
    toward black if too light) while keeping its hue. The subsystem accents span a
    wide luminance range — yellow/orange are light, blue/purple dark — which would
    force per-device black-vs-white label text. Normalising every device chip to
    the same luminance lets them all share ONE dark text colour and read
    consistently, while staying mid-tone enough not to collapse to near-black
    under the global dark-mode invert (a light-mode 0.62 inverts to ~0.40)."""
    c = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    if lum < target:
        t = (target - lum) / (1 - lum)
        c = [ch + (1 - ch) * t for ch in c]
    elif lum > target:
        t = (lum - target) / lum
        c = [ch * (1 - t) for ch in c]
    return "#{:02x}{:02x}{:02x}".format(*tuple(round(ch * 255) for ch in c))


# Device bodies and component blocks both fill with the subsystem accent
# normalised to a fixed luminance; components a lighter shade so they read as an
# inset. Shared so an intra-device edge can be painted the EXACT component-box
# colour (see the arrow-colour logic).
def _device_fill(accent):
    return _normalize_luminance(accent)


def _component_fill(accent):
    return _normalize_luminance(accent, 0.80)


def _text(
    rng,
    eid,
    x,
    y,
    w,
    h,
    text,
    font_size=14,
    align="center",
    valign="middle",
    group_ids=None,
    auto_resize=True,
    color="#1e1e1e",
    link=None,
    halo="page",
):
    return {
        "id": eid,
        "type": "text",
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        **_base(rng),
        "strokeColor": color,
        # Readability-halo colour for render_svg.mjs, picked by what the label
        # sits ON: "page" → the page background (--diagram-halo CSS var, which is
        # theme-aware); a hex → a baked box-fill colour (device/component fill),
        # which inverts together with that box in dark mode so it always matches.
        "haloColor": halo,
        "link": link,
        "text": text,
        "fontSize": font_size,
        "fontFamily": EXCALIDRAW_FONT,
        "textAlign": align,
        "verticalAlign": valign,
        "containerId": None,
        "originalText": text,
        # autoResize=True so Excalidraw can grow the element to the rendered
        # text width (the rendered font may differ slightly from Pillow measures,
        # so a fixed-size box would cause line-wrapping). Pair with right-align
        # for WEST port labels and left-align for EAST so the resize happens
        # on the away-from-device side and the near-device edge stays pinned.
        "autoResize": auto_resize,
        "lineHeight": 1.25,
        "groupIds": list(group_ids) if group_ids else [],
    }


def _arrow(rng, eid, x, y, points, stroke_width=2, stroke_color="#1e1e1e", start_arrowhead=None, stroke_style="solid"):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {
        "id": eid,
        "type": "arrow",
        "x": x,
        "y": y,
        "width": (max(xs) - min(xs)) or 1,
        "height": (max(ys) - min(ys)) or 1,
        **_base(rng),
        "strokeColor": stroke_color,
        "strokeWidth": stroke_width,
        "strokeStyle": stroke_style,
        "roundness": None,
        # elbowed=true → exportToSvg routes right-angle elbows from each
        # endpoint's binding to the port it lands on.
        "elbowed": True,
        "points": points,
        "lastCommittedPoint": None,
        "startBinding": None,
        "endBinding": None,
        "startArrowhead": start_arrowhead,
        "endArrowhead": "triangle",
    }


def _trim_polyline(pts: list[tuple[float, float]], which: int, dist: float) -> None:
    """Pull one end of a polyline back by `dist` along its terminal segment.
    `which` is 0 (start) or -1 (end). No-op if the segment is too short."""
    anchor = pts[1] if which == 0 else pts[-2]
    end = pts[which]
    seg = ((end[0] - anchor[0]) ** 2 + (end[1] - anchor[1]) ** 2) ** 0.5
    if seg <= dist:
        return
    t = (seg - dist) / seg
    pts[which] = (anchor[0] + (end[0] - anchor[0]) * t, anchor[1] + (end[1] - anchor[1]) * t)


def subsystem_color(sub_name: str, all_subs: list[str]) -> str:
    """Bright accent for the subsystem outline + label."""
    idx = sorted(all_subs).index(sub_name)
    return SUBSYSTEM_PALETTE_BRIGHT[idx % len(SUBSYSTEM_PALETTE_BRIGHT)]


def device_fill_color(sub_name: str, all_subs: list[str]) -> str:
    """Pastel hue-matched fill for devices inside the subsystem."""
    idx = sorted(all_subs).index(sub_name)
    return SUBSYSTEM_PALETTE_DARK[idx % len(SUBSYSTEM_PALETTE_DARK)]


def component_fill_color(sub_name: str, all_subs: list[str]) -> str:
    """Even paler hue-matched fill for components nested inside a device."""
    idx = sorted(all_subs).index(sub_name)
    return COMPONENT_PALETTE[idx % len(COMPONENT_PALETTE)]


def build_excalidraw(
    laid_out: dict,
    sys_: System,
    redges: list[RenderEdge],
    all_subs: list[str],
    collapsed: set[str] = frozenset(),
    link_subs: set[str] | None = None,
    reversed_edges: set[str] = frozenset(),
) -> dict:
    """Translate ELK's laid-out graph into Excalidraw-schema elements, which
    render_svg.mjs feeds to Excalidraw's exportToSvg.

    ELK reports node and port coordinates relative to their parent container;
    we sum offsets while walking to get absolute positions. Edges all live in
    `laid_out.edges` at the root level, but each edge carries a `container`
    property telling us which container its section coordinates are relative
    to (absent = root). We pre-compute every container's absolute position so
    edges can offset by the right container regardless of nesting.

    `all_subs` is the FULL system's subsystem list (not just what this diagram
    renders) so palette indices — and therefore colors — stay identical across
    the main diagram and every per-subsystem detail diagram. Collapsed subsystem
    titles and device/component titles carry a relative `<stem>.svg` element
    link (exportToSvg wraps it in an `<a href>`); stub labels are plain text.
    """
    # Which subsystem titles are clickable links to their detail diagram.
    # Defaults to `collapsed` (system/subsystem views: every collapsed peer
    # links, the expanded focus does not). Flow-trace views (device / component
    # / path / interface) have no subsystem focus — the focus is a device or
    # flow — so they pass every subsystem present, making them all navigable.
    if link_subs is None:
        link_subs = collapsed
    rng = random.Random(42)
    elements: list[dict] = []
    # Port (interface) labels are collected here and appended AFTER the edges so
    # they paint ON TOP of any edge that transits a box and crosses them — paired
    # with the render-time readability halo, the label stays legible over the
    # crossing line instead of being painted over by it. Their positions are
    # unchanged (still seated symmetric to the edge payload labels).
    deferred_port_labels: list[dict] = []
    eid_counter = [0]

    def next_id(prefix):
        eid_counter[0] += 1
        return f"{prefix}_{eid_counter[0]}"

    def detail_link(stem: str) -> str:
        """Element-`link` for a title → its detail diagram, as a relative .svg
        href that exportToSvg wraps in an `<a href>`."""
        return f"{stem}.svg"

    def iface_link(subsystem: str, heading: str) -> str:
        """Element-`link` for an interface (port) label → its documented
        section: a doc-relative href ('<Doc>.md#<Device > Interface>') that the
        mkdocs hook rewrites to the heading's real anchor when it inlines the
        diagram."""
        return f"{subsystem}.md#{heading}"

    # Pre-compute absolute positions for every container/node so edges can look
    # up their `container` field to find the correct offset.
    container_pos: dict[str, tuple[float, float]] = {"": (0.0, 0.0)}
    # Port ids owned by component nodes — used to thin out arrows that touch a
    # component interface (component-level traffic is "less prominent" than
    # device-level traffic in the hierarchy).
    component_port_ids: set[str] = set()
    # Port id → the id of the DEVICE it belongs to — for BOTH a device's own
    # interfaces and its components' interfaces. Lets us recognise an edge routed
    # entirely inside one device (any mix of the device's own interfaces and its
    # components') and paint it the component-box colour.
    port_device: dict[str, str] = {}
    # ELK port id → (Excalidraw element id, Excalidraw element dict). Lets us
    # set startBinding/endBinding on arrows so exportToSvg anchors each arrow to
    # its endpoint port.
    port_binding: dict[str, tuple[str, dict]] = {}
    # Edge id → its semantic direction ("out" or "both"). Lookup table built
    # from the deduped Edge list so the renderer knows when to draw a single
    # arrow versus a split bidirectional pair.
    edge_direction: dict[str, str] = {f"e_{n}": re_.edge.direction for n, re_ in enumerate(redges)}
    # Edge id → its payload tokens (text, link), so a label can split into
    # per-token fragments and link each single-flow token to its path diagram.
    edge_tokens: dict[str, tuple] = {f"e_{n}": re_.edge.tokens for n, re_ in enumerate(redges)}
    # Stub node id → the off-graph endpoint it stands in for. Used to render
    # the stub's text (accent-colored) and to color edges that run to it.
    stub_meta: dict[str, tuple[str, str]] = {sid: key for key, sid in _stub_keys(redges).items()}

    # Port id → arrow color, which is the SUBSYSTEM accent (the saturated
    # outline color) of the subsystem that owns the port's device/component.
    # Arrows then read as same-color travel routes across each subsystem.
    port_arrow_color: dict[str, str] = {}

    def collect_port_colors(node, accent=None):
        for child in node.get("children", []):
            cid = child.get("id", "")
            label = (child.get("labels") or [{}])[0].get("text", "")
            if cid.startswith("sub__"):
                collect_port_colors(child, subsystem_color(label, all_subs))
            elif cid.startswith("dev__") or cid.startswith("comp__"):
                for p in child.get("ports", []):
                    port_arrow_color[p["id"]] = accent or "#1e1e1e"
                collect_port_colors(child, accent)

    collect_port_colors(laid_out)
    # Stubs color like the subsystem they stand in for, so a dotted edge to a
    # "Pilot Pro > …" stub reads exactly like the real edge to Pilot Pro does
    # in the complete diagram.
    for sid, key in stub_meta.items():
        port_arrow_color[sid] = subsystem_color(key[0], all_subs) if key[0] in all_subs else "#1e1e1e"

    def index_positions(node: dict, ox: float, oy: float, dev_id: str | None = None):
        for child in node.get("children", []):
            cx = ox + child.get("x", 0)
            cy = oy + child.get("y", 0)
            cid = child.get("id", "")
            if cid:
                container_pos[cid] = (cx, cy)
            if cid.startswith("dev__"):
                # The device's OWN interfaces belong to it directly.
                for p in child.get("ports", []):
                    port_device[p["id"]] = cid
            elif cid.startswith("comp__"):
                for p in child.get("ports", []):
                    component_port_ids.add(p["id"])
                    if dev_id:
                        port_device[p["id"]] = dev_id
            # A component nests inside its device, so carry the enclosing device
            # id down; a component's own id never overrides it.
            index_positions(child, cx, cy, cid if cid.startswith("dev__") else dev_id)

    index_positions(laid_out, 0, 0)

    def render_node(
        node: dict,
        ox: float,
        oy: float,
        groups: list[str],
        subsystem_accent: str | None = None,
        component_accent: str | None = None,
        subsystem_name: str | None = None,
        device_name: str | None = None,
    ):
        for child in node.get("children", []):
            cx = ox + child.get("x", 0)
            cy = oy + child.get("y", 0)
            cw = child.get("width", 0)
            ch = child.get("height", 0)
            cid = child.get("id", "")
            label_text = child.get("labels", [{}])[0].get("text", "")
            # Each container introduces a fresh group level. Children of this
            # container inherit `groups` plus this container's own group id, so
            # selecting a subsystem in Excalidraw drags every device, port and
            # component nested inside.
            child_groups = [*groups, f"g_{cid}"] if cid else groups

            if cid.startswith("sub__"):
                accent = subsystem_color(label_text, all_subs)
                dev_fill = device_fill_color(label_text, all_subs)
                comp_fill = component_fill_color(label_text, all_subs)
                # No fill, thin dotted outline tinted with the subsystem's
                # bright accent. The lighter hue-matched fill is what every
                # device inside this subsystem will use; components nested
                # inside those devices get an even paler shade.
                elements.append(
                    _rect(
                        rng,
                        next_id("sub"),
                        cx,
                        cy,
                        cw,
                        ch,
                        "transparent",
                        stroke=accent,
                        stroke_width=1,
                        stroke_style="dotted",
                        group_ids=child_groups,
                    )
                )
                # A subsystem title links to its detail diagram whenever it's
                # navigable context (a collapsed peer, or any subsystem in a
                # flow-trace view) — but not when it's the expanded focus of its
                # own subsystem diagram. `link_subs` captures exactly that set.
                # The title text is the plain label + an element link, which
                # exportToSvg wraps in an <a href="…svg">.
                should_link = label_text in link_subs
                sub_link = detail_link(embed.qualified_name(label_text)) if should_link else None
                sub_display_text = (sys_.display_names if sys_ is not None else {}).get(label_text, label_text)
                elements.append(
                    _text(
                        rng,
                        next_id("subtxt"),
                        cx + 14,
                        cy + 10,
                        _estimate_text_width(sub_display_text, 16),
                        22,
                        sub_display_text,
                        font_size=16,
                        align="left",
                        valign="top",
                        color=accent,
                        group_ids=child_groups,
                        link=sub_link,
                    )
                )
                render_node(child, cx, cy, child_groups, dev_fill, comp_fill, subsystem_name=label_text)

            elif cid.startswith("stub__"):
                # Off-graph endpoint stub (referenced subsystem without a doc,
                # or a filtered-out peer): a single plain-text element. Arrows
                # bind directly to it — text is a bindable Excalidraw type.
                accent = port_arrow_color.get(cid, "#1e1e1e")
                stub_text_h = _font_height(11)
                stub_eid = next_id("stubtxt")
                stub_el = _text(
                    rng,
                    stub_eid,
                    cx,
                    cy + (ch - stub_text_h) / 2,
                    cw,
                    stub_text_h,
                    label_text,
                    font_size=11,
                    align="center",
                    valign="middle",
                    color=accent,
                )
                elements.append(stub_el)
                port_binding[cid] = (stub_eid, stub_el)

            elif cid.startswith("dev__") or cid.startswith("comp__"):
                is_comp = cid.startswith("comp__")
                # Fallback fills for the (rare) no-subsystem case.
                fill = (component_accent or "#f1f3f5") if is_comp else (subsystem_accent or "#ffffff")
                # Device bodies and component blocks are both filled with the
                # subsystem accent normalised to a uniform luminance (never the
                # pale pastel, which inverts to a near-black block under the global
                # dark-mode invert) and drawn with NO outline — so an edge routed
                # across a body doesn't read as crossing a hard box. Components use
                # a LIGHTER normalised shade so they read as an inset within the
                # device body without needing a border.
                accent = subsystem_color(subsystem_name, all_subs) if subsystem_name else None
                dev_fill = _device_fill(accent) if accent else fill
                if is_comp:
                    comp_fill = _component_fill(accent) if accent else fill
                    elements.append(
                        _rect(
                            rng,
                            next_id("comp"),
                            cx,
                            cy,
                            cw,
                            ch,
                            comp_fill,
                            stroke="transparent",
                            stroke_width=1,
                            square=True,
                            group_ids=child_groups,
                        )
                    )
                else:
                    elements.append(
                        _rect(
                            rng,
                            next_id("dev"),
                            cx,
                            cy,
                            cw,
                            ch,
                            dev_fill,
                            stroke="transparent",
                            stroke_width=2,
                            square=True,
                            group_ids=child_groups,
                        )
                    )

                # Components: label top-left at ELK's computed position,
                # matching devices and subsystems.
                # Devices without children: use ELK's computed label position
                # (label is INSIDE the box; ELK and elk.padding place it).
                # Devices with children: ELK placement is OUTSIDE so its label
                # position sits ABOVE the box — we render it ourselves at the
                # device's top-left over the reserved top padding.
                # One uniform dark text colour for every device label: the chip
                # fills are all normalised to the same luminance, so a single
                # colour reads consistently on all of them (and inverts to a
                # uniform light in dark mode).
                dev_text_color = "#1e1e1e"
                if is_comp:
                    comp_text_h = _font_height(12)
                    elk_lbl = (child.get("labels") or [{}])[0]
                    # The title links to the component's detail diagram via the
                    # element link (svg href).
                    comp_link = (
                        detail_link(embed.qualified_name(subsystem_name, device_name, label_text))
                        if subsystem_name and device_name
                        else None
                    )
                    w_full = _font(12).getlength(label_text)
                    elements.append(
                        _text(
                            rng,
                            next_id("comptxt"),
                            cx + elk_lbl.get("x", 5),
                            cy + elk_lbl.get("y", 5),
                            w_full,
                            comp_text_h,
                            label_text,
                            font_size=12,
                            align="left",
                            valign="top",
                            group_ids=child_groups,
                            link=comp_link,
                            halo=comp_fill,  # sits on the component box
                        )
                    )
                elif child.get("children"):
                    # Match the (5, 5) corner offset ELK uses for devices
                    # without children so both label tiers read consistently.
                    dev_link = detail_link(embed.qualified_name(subsystem_name, label_text)) if subsystem_name else None
                    lw = _estimate_text_width(label_text, 14)
                    lh = _font_height(14)
                    elements.append(
                        _text(
                            rng,
                            next_id("devtxt"),
                            cx + 5,
                            cy + 5,
                            lw,
                            lh,
                            label_text,
                            font_size=14,
                            align="left",
                            valign="top",
                            color=dev_text_color,
                            group_ids=child_groups,
                            link=dev_link,
                            halo=dev_fill,  # sits on the device box
                        )
                    )
                else:
                    elk_lbl = (child.get("labels") or [{}])[0]
                    lbl_x = cx + elk_lbl.get("x", 8)
                    lbl_y = cy + elk_lbl.get("y", 8)
                    lbl_w = elk_lbl.get("width") or _estimate_text_width(label_text, 14)
                    lbl_h = elk_lbl.get("height") or 18
                    dev_link = detail_link(embed.qualified_name(subsystem_name, label_text)) if subsystem_name else None
                    elements.append(
                        _text(
                            rng,
                            next_id("devtxt"),
                            lbl_x,
                            lbl_y,
                            lbl_w,
                            lbl_h,
                            label_text,
                            font_size=14,
                            align="left",
                            valign="top",
                            color=dev_text_color,
                            group_ids=child_groups,
                            link=dev_link,
                            halo=dev_fill,  # sits on the device box
                        )
                    )

                for port in child.get("ports", []):
                    port_rx = port.get("x", 0)
                    port_ry = port.get("y", 0)
                    pw = port.get("width", 8)
                    ph = port.get("height", 8)
                    px = cx + port_rx
                    py = cy + port_ry
                    port_eid = next_id("port")
                    port_rect = _rect(
                        rng,
                        port_eid,
                        px,
                        py,
                        pw,
                        ph,
                        "#1e1e1e",
                        stroke="#1e1e1e",
                        stroke_width=1,
                        group_ids=child_groups,
                    )
                    elements.append(port_rect)
                    if port.get("id"):
                        port_binding[port["id"]] = (port_eid, port_rect)
                    # ELK places OUTSIDE port labels flush against the port
                    # (1 px gap) and exposes no spacing option that affects
                    # that, so we shift the rendered label box out by a small
                    # uniform gap (same whether or not an arrowhead lands here).
                    label_port_gap = 4
                    port_cy = py + ph / 2
                    # The interface's documented section, qualified the same way
                    # _heading_for() and the hook's _PATHS keys are
                    # ("Device > Interface" or "Device > Component > Interface"),
                    # so the port label can link straight to that heading.
                    for lbl in port.get("labels", []):
                        iface_name = lbl.get("text", "")
                        port_link = None
                        if subsystem_name and iface_name:
                            if is_comp:
                                heading = f"{device_name} > {label_text} > {iface_name}"
                            else:
                                heading = f"{label_text} > {iface_name}"
                            port_link = iface_link(subsystem_name, heading)
                        lh = lbl.get("height", 14)
                        lx = px + lbl.get("x", 0)
                        ly = py + lbl.get("y", 0)
                        lx_rel = lbl.get("x", 0)
                        ly_rel = lbl.get("y", 0)
                        valign = "top"
                        if lx_rel < 0 or lx_rel > pw:  # W / E port
                            if lx_rel < 0:
                                text_align = "right"
                                lx -= label_port_gap
                            else:
                                text_align = "left"
                                lx += label_port_gap
                            # Seat the label EDGE_LABEL_GAP px off the line — the
                            # SAME clearance ELK gives the edge payload labels —
                            # on whichever side ELK placed it, and centre it
                            # (valign middle) like those labels. A port label and
                            # a payload label then read at symmetric heights, one
                            # the same distance above the line as the other below.
                            if ly + lh / 2 >= port_cy:  # below the line
                                ly = port_cy + EDGE_LABEL_GAP
                            else:  # above the line
                                ly = port_cy - EDGE_LABEL_GAP - lh
                            valign = "middle"
                        elif ly_rel < 0 or ly_rel > ph:
                            text_align = "center"
                        else:
                            text_align = "left"
                        deferred_port_labels.append(
                            _text(
                                rng,
                                next_id("plabel"),
                                lx,
                                ly,
                                lbl.get("width", 40),
                                lbl.get("height", 14),
                                lbl.get("text", ""),
                                font_size=10,
                                align=text_align,
                                valign=valign,
                                group_ids=child_groups,
                                link=port_link,
                                # A component interface label sits over the device
                                # body (the component is nested inside it); a device
                                # interface label sits outside, over the page.
                                halo=(dev_fill if is_comp else "page"),
                            )
                        )

                # Recurse into device children (nested components).
                if child.get("children"):
                    render_node(
                        child,
                        cx,
                        cy,
                        child_groups,
                        subsystem_accent,
                        component_accent,
                        subsystem_name=subsystem_name,
                        device_name=(label_text if not is_comp else device_name),
                    )

    render_node(laid_out, 0, 0, [], None, None)

    # Edges all live at the root. Each edge's section coordinates are relative
    # to its `container` (or root if absent); look up the container's absolute
    # position and shift accordingly.
    for edge in laid_out.get("edges", []):
        container_id = edge.get("container", "")
        cox, coy = container_pos.get(container_id, (0.0, 0.0))

        sections = edge.get("sections", [])
        if not sections:
            continue
        points: list[tuple[float, float]] = []
        for sec in sections:
            sp = sec.get("startPoint", {"x": 0, "y": 0})
            ep = sec.get("endPoint", {"x": 0, "y": 0})
            bends = sec.get("bendPoints", [])
            seg = (
                [(sp["x"] + cox, sp["y"] + coy)]
                + [(b["x"] + cox, b["y"] + coy) for b in bends]
                + [(ep["x"] + cox, ep["y"] + coy)]
            )
            if points and points[-1] == seg[0]:
                points.extend(seg[1:])
            else:
                points.extend(seg)
        # `layout` fed wrapped back-edges to ELK reversed so they'd route
        # straight; undo that here so the arrow is drawn in its semantic
        # direction. Reversing the flattened polyline and swapping the
        # source/target ids below restores src→dst order, after which every
        # downstream step (arrowhead at the end, color from dst, port bindings)
        # is identical to a normally-oriented edge.
        reversed_edge = edge.get("id", "") in reversed_edges
        if reversed_edge:
            points.reverse()
        endpoint_ids = set(edge.get("sources", [])) | set(edge.get("targets", []))
        # Uniform data-path width: every segment uses the cross-device width (2),
        # including segments that touch a component interface (previously thinned
        # to 1 to read as "less prominent").
        stroke_w = 2
        # Edges with a stub endpoint cross out of the rendered scope — dotted.
        stroke_style = "dotted" if any(i.startswith("stub__") for i in endpoint_ids) else "solid"

        src_list = edge.get("targets") if reversed_edge else edge.get("sources")
        dst_list = edge.get("sources") if reversed_edge else edge.get("targets")
        src_id = (src_list or [None])[0]
        dst_id = (dst_list or [None])[0]
        src_color = port_arrow_color.get(src_id, "#1e1e1e")
        dst_color = port_arrow_color.get(dst_id, "#1e1e1e")

        # An edge whose two endpoints both belong to the SAME device (any mix of
        # the device's own interfaces and its components' interfaces) is routed
        # entirely inside that device's body — paint it the component-box colour
        # (the lighter fill) rather than the subsystem accent, so it reads as
        # part of that device's internals.
        src_dev = port_device.get(src_id)
        intra_device = src_dev is not None and src_dev == port_device.get(dst_id)

        direction = edge_direction.get(edge.get("id", ""), "out")

        def _bind(arrow_eid, arrow_elem, end, port_id):
            """Bind one end of an arrow to a port and update the port's
            boundElements. `end` is "start" or "end" (matches Excalidraw's
            startBinding / endBinding fields). The arrow docks at the CENTRE of
            the port rect ([0.5, 0.5]) rather than an edge midpoint, so the
            line/arrowhead meets the port box at its midpoint."""
            if port_id not in port_binding:
                return
            peid, prect = port_binding[port_id]
            arrow_elem[f"{end}Binding"] = {
                "elementId": peid,
                "focus": 0,
                "gap": STUB_ARROW_GAP if port_id.startswith("stub__") else 1,
                "fixedPoint": [0.5, 0.5],
            }
            if prect.get("boundElements") in (None, []):
                prect["boundElements"] = []
            prect["boundElements"].append({"id": arrow_eid, "type": "arrow"})

        # Snap each end of the polyline to the CENTRE of its port box. ELK routes
        # to the port's outer edge, and exportToSvg draws these raw points (the
        # binding's fixedPoint only re-anchors on drag, not in the static SVG), so
        # without this the line/arrowhead stops at the port's left/right edge
        # instead of its midpoint. The port box is centred on the edge's axis, so
        # moving to the centre shifts only along that axis and keeps the segment
        # orthogonal. Stubs are text, not port boxes — leave them to the gap trim.
        def _snap_to_port_centre(idx, port_id, points=points):
            if not port_id or port_id.startswith("stub__") or port_id not in port_binding:
                return
            _eid, prect = port_binding[port_id]
            points[idx] = (prect["x"] + prect["width"] / 2, prect["y"] + prect["height"] / 2)

        _snap_to_port_centre(0, src_id)
        _snap_to_port_centre(-1, dst_id)

        # Pad arrows away from stub text in the static geometry; the binding
        # gap (set in _bind) keeps the same clearance.
        if src_id and src_id.startswith("stub__"):
            _trim_polyline(points, 0, STUB_ARROW_GAP)
        if dst_id and dst_id.startswith("stub__"):
            _trim_polyline(points, -1, STUB_ARROW_GAP)

        ax, ay = points[0]
        relative = [[x - ax, y - ay] for x, y in points]
        aid = next_id("arrow")

        if direction == "both":
            # Bidirectional: one arrow, arrowheads on BOTH ends. Color rule:
            # same subsystem on both ends → use that subsystem's accent;
            # crossing a subsystem boundary → fall back to a neutral stroke
            # so the line doesn't have to favour one side over the other.
            arrow_color = src_color if src_color == dst_color else "#1e1e1e"
            if intra_device:
                arrow_color = _component_fill(src_color)
            arrow_elem = _arrow(
                rng,
                aid,
                ax,
                ay,
                relative,
                stroke_width=stroke_w,
                stroke_color=arrow_color,
                start_arrowhead="triangle",
                stroke_style=stroke_style,
            )
        else:
            # Unidirectional: arrow goes src → dst with the head (and thus the
            # color) tied to the dst box's subsystem.
            arrow_color = _component_fill(src_color) if intra_device else dst_color
            arrow_elem = _arrow(
                rng,
                aid,
                ax,
                ay,
                relative,
                stroke_width=stroke_w,
                stroke_color=arrow_color,
                stroke_style=stroke_style,
            )

        _bind(aid, arrow_elem, "start", src_id)
        _bind(aid, arrow_elem, "end", dst_id)
        elements.append(arrow_elem)

        # Edge labels (payload text) sit ON the wire — ELK places them inline
        # (org.eclipse.elk.edgeLabels.inline, set in emit_elk_spec); the label's
        # halo masks the line behind the text and it paints on top of its own
        # arrow (appended just above). When a payload token links to its flow's
        # path diagram, the label splits into per-token fragments so only the
        # single-flow token(s) carry a clickable link (deduped multi-flow tokens
        # stay plain text); otherwise it stays one centered label.
        toks = edge_tokens.get(edge.get("id", ""), ())
        # A segment routed entirely inside one device sits over that device's
        # body; an inter-device segment sits over the page.
        halo = _device_fill(src_color) if intra_device else "page"
        for lbl in edge.get("labels", []):
            text = lbl.get("text", "")
            lw, lh = lbl.get("width", 40), lbl.get("height", 14)
            lx, ly = cox + lbl.get("x", 0), coy + lbl.get("y", 0)
            # Keep the label clear of the segment's ENDS (ports/arrowheads):
            # clamp its centre along the horizontal segment it sits on so its
            # near edge stays EDGE_LABEL_END_CLEAR px from either end.
            cxm, cym = lx + lw / 2, ly + lh / 2
            for (x1, y1), (x2, y2) in itertools.pairwise(points):
                if abs(y1 - y2) < 0.5 and abs(y1 - cym) < lh and min(x1, x2) - 2 <= cxm <= max(x1, x2) + 2:
                    xs, xe = sorted((x1, x2))
                    margin = EDGE_LABEL_END_CLEAR + lw / 2
                    if xs + margin <= xe - margin:
                        lx = min(max(cxm, xs + margin), xe - margin) - lw / 2
                    break
            if toks and text and ", ".join(t for t, _ in toks) == text and any(link for _, link in toks):
                # Tile the fragments by exact glyph advance, starting from the
                # (clamped) label's centered left edge, so the split reads
                # identically to the one label — but each single-flow token is
                # its own <a>-wrapped (render_svg.mjs gives it a full-box hit
                # area).
                frags: list[tuple[str, str | None]] = []
                for i, tok in enumerate(toks):
                    if i:
                        frags.append((", ", None))
                    frags.append(tok)
                adv = _glyph_advance(text, 10)
                run = lx + (lw - adv) / 2
                # ONE halo box spanning the whole label, drawn behind every
                # fragment: a transparent full-text backing that render_svg turns
                # into a single box. Per-fragment halos would clip each other's
                # letters at the seams (a later fragment's box over the previous
                # fragment's glyphs), so the fragments themselves carry no halo.
                elements.append(
                    _text(
                        rng,
                        next_id("elabel"),
                        run,
                        ly,
                        adv,
                        lh,
                        text,
                        font_size=10,
                        align="left",
                        valign="middle",
                        auto_resize=True,
                        color="transparent",
                        halo=halo,
                    )
                )
                for ftext, flink in frags:
                    fw = _glyph_advance(ftext, 10)
                    elements.append(
                        _text(
                            rng,
                            next_id("elabel"),
                            run,
                            ly,
                            fw,
                            lh,
                            ftext,
                            font_size=10,
                            align="left",
                            valign="middle",
                            auto_resize=True,
                            color=arrow_color,
                            link=flink,
                            halo="none",
                        )
                    )
                    run += fw
            else:
                elements.append(
                    _text(
                        rng,
                        next_id("elabel"),
                        lx,
                        ly,
                        lw,
                        lh,
                        text,
                        font_size=10,
                        align="center",
                        valign="middle",
                        auto_resize=False,
                        color=arrow_color,
                        halo=halo,
                    )
                )

    # Port labels (+ their halos) last, on top of the edges they may cross.
    elements.extend(deferred_port_labels)

    return {"elements": elements}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def planned_stems(doc_paths: list[Path]) -> set[str]:
    """Every diagram stem the generator could emit for these docs — the naming
    contract, expressed without rendering or the flow-touch filter (so it needs
    no node and is a superset of what actually gets emitted). hooks.py derives
    the same names from each doc's heading structure; tests/test_derivation.py
    asserts this set stays within what hooks.py can derive, guarding the two
    expressions of the convention against drift."""
    full, flows, _parsed, unresolved = parse_closure(doc_paths)
    section = doc_paths[0].parent if doc_paths else Path(".")
    stems = {embed.qualified_name(manifest.system_name(section))}
    for sub in {d.subsystem for d in full.devices} - unresolved:
        stems.add(embed.qualified_name(sub))
    for d in full.devices:
        if d.subsystem in unresolved:
            continue
        stems.add(embed.qualified_name(d.subsystem, d.name))
        for i in d.interfaces:
            stems.add(embed.qualified_name(d.subsystem, *_heading_for(d, None, i).split(" > ")))
        for c in d.components:
            stems.add(embed.qualified_name(d.subsystem, d.name, c.name))
            for i in c.interfaces:
                stems.add(embed.qualified_name(d.subsystem, *_heading_for(d, c, i).split(" > ")))
    for fl in flows:
        if fl.subsystem in unresolved:
            continue
        stems.add(_flow_stem(fl))
    return stems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "section",
        type=Path,
        help=(
            "the data-flows FOLDER: every '<name>.md' in it is a subsystem interface "
            "doc, except 'index.md' (the landing page that hosts the system diagram)"
        ),
    )
    ap.add_argument(
        "--out",
        type=Path,
        required=False,
        help=(
            "output DIRECTORY for the SVGs; embeds reference it relative to the "
            "section's docs (required unless --check is given)"
        ),
    )
    ap.add_argument("--check", action="store_true", help=("parse and validate only, write no diagrams"))
    args = ap.parse_args(argv)
    if not args.check and args.out is None:
        ap.error("--out is required when not using --check")
    if not args.section.is_dir():
        print(f"error: {args.section} is not a folder", file=sys.stderr)
        return 2

    system_name, doc_paths = manifest.parse_section(args.section)
    if not doc_paths:
        print(f"error: no subsystem docs found in {args.section}", file=sys.stderr)
        return 2
    overview = args.section / "index.md"
    if not args.check and not overview.is_file():
        print(
            f"error: {overview} not found — the section needs an index.md landing page for the system diagram",
            file=sys.stderr,
        )
        return 2

    full, flows, parsed_docs, unresolved = parse_closure(doc_paths)
    all_subs = sorted({d.subsystem for d in full.devices})

    if args.check:
        derive_edges(flows, full)
        if _VALIDATION_WARNINGS:
            print(
                f"check failed: {_VALIDATION_WARNINGS} issue(s) across {len(parsed_docs)} parsed doc(s)",
                file=sys.stderr,
            )
            return 1
        advisory = f" ({_VALIDATION_SOFT_WARNINGS} advisory warning(s))" if _VALIDATION_SOFT_WARNINGS else ""
        print(f"check passed: {len(parsed_docs)} doc(s), {len(flows)} flows{advisory}", file=sys.stderr)
        return 0

    # Newton fork: a single STABLE output folder (not dated) so re-rendering is
    # idempotent — embeds always point at "diagrams/…", and unchanged docs
    # produce no diff. The folder is a gitignored build artifact regenerated by
    # CI (and locally before `mkdocs serve`); it's cleared first so diagrams for
    # removed devices/flows don't linger.
    # --out is the exact output directory for the SVGs (e.g.
    # docs/assets/diagrams/drone-system). hooks.py derives each diagram's
    # docs-relative path from this layout (assets/diagrams/<section>/<stem>.svg).
    out_dir: Path = args.out
    if out_dir.exists():
        for old in out_dir.glob("*.svg"):
            old.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Spin up the node worker pools once (cold-start is ~1s each, so per-diagram
    # spawning is what made this slow). One worker per concurrent diagram; cap to
    # keep the heavy render workers' memory in check. DIAGRAMS_WORKERS overrides.
    require_render_toolchain()
    global _ELK_POOL, _RENDER_POOL
    workers = int(os.environ.get("DIAGRAMS_WORKERS", "0") or 0) or min(os.cpu_count() or 4, 8)
    _ELK_POOL = WorkerPool(_workers.bundle_path("elk_layout.bundle.mjs"), workers)
    _RENDER_POOL = WorkerPool(_workers.bundle_path("render_svg.bundle.mjs"), workers)

    # The flows that emit a path diagram (§5 below) — single-flow token targets.
    drawable_stems = drawable_flow_stems(flows, full, unresolved)
    edges = derive_edges(flows, full, drawable_stems)
    # Every (segment, payload) token's link, keyed by the full-graph edges so a
    # focused view opens the SAME diagram as the system view. Multi-flow tokens
    # link to a synthetic aggregate diagram (§7), generated here.
    token_links = {(frozenset({e.src_key, e.dst_key}), text): link for e in edges for text, link in e.tokens}
    aggregates = aggregate_diagrams(flows, full)

    def emit(laid_out, view, redges, collapsed, reversed_edges, stem, link_subs=None):
        """Write the diagram's SVG.

        `link_subs` overrides which subsystem titles are clickable (defaults to
        `collapsed`); flow-trace views pass every subsystem in the view so all
        subsystem titles navigate to their detail diagram."""
        scene = build_excalidraw(
            laid_out, view, redges, all_subs, collapsed=collapsed, link_subs=link_subs, reversed_edges=reversed_edges
        )
        (out_dir / f"{stem}.svg").write_text(render_svg(scene["elements"]), encoding="utf-8")
        print(f"wrote {out_dir / (stem + '.svg')}", file=sys.stderr)

    def compute_subsystem(expanded: set[str], all_collapsed: bool):
        # The system diagram is focus-less (every subsystem collapsed → show the
        # whole inter-subsystem mesh); a per-subsystem diagram focuses on the
        # expanded subsystem, so its collapsed neighbours keep only their
        # focus-facing surface (no lateral neighbour↔neighbour detail).
        focus = None if all_collapsed else expanded
        if all_collapsed:
            collapsed = {s for s in all_subs if s not in unresolved}
            view = System(name=full.name, devices=list(full.devices), display_names=full.display_names)
        else:
            exp_view = filter_system(full, expanded, set(), set())
            exp_keys = port_keys_for(exp_view)
            collapsed = set()
            for e in edges:
                if e.src_key in exp_keys and e.dst_key[0] not in expanded:
                    collapsed.add(e.dst_key[0])
                if e.dst_key in exp_keys and e.src_key[0] not in expanded:
                    collapsed.add(e.src_key[0])
            collapsed -= unresolved
            neigh = filter_system(full, collapsed, set(), set()).devices if collapsed else []
            view = System(name=full.name, devices=exp_view.devices + neigh, display_names=full.display_names)
        view = collapse_system(view, collapsed, edges, focus=focus)
        redges = classify_edges(edges, port_keys_for(view), collapsed, unresolved)
        laid_out, reversed_edges = layout(emit_elk_spec(view, redges, collapsed))
        return laid_out, view, redges, collapsed, reversed_edges

    def compute_trace(
        device: str | None = None, component: str | None = None, iface_key: tuple[str, str] | None = None
    ):
        """Flow-trace view for a device, component, or single interface; None if
        no flows touch it."""
        parsed_subs = {d.subsystem for d in full.devices}
        traced, tedges = trace_flows(
            flows, full, device, component, parsed_subs, iface_key=iface_key, token_links=token_links
        )
        if not traced:
            return None
        view = chain_view(full, tedges)
        redges = classify_edges(tedges, port_keys_for(view), set(), unresolved)
        laid_out, reversed_edges = layout(emit_elk_spec(view, redges))
        return laid_out, view, redges, set(), reversed_edges

    full_index = full.by_interface()
    full_parsed_subs = {d.subsystem for d in full.devices}

    def compute_aggregate(agg_flows):
        """Aggregate view: every flow a multi-flow token represents, drawn
        together. Its own labels link to the same full-graph diagrams (via
        token_links), so they stay clickable too."""
        aedges = derive_edges(agg_flows, full, token_links=token_links)
        if not aedges:
            return None
        view = chain_view(full, aedges)
        redges = classify_edges(aedges, port_keys_for(view), set(), unresolved)
        laid_out, reversed_edges = layout(emit_elk_spec(view, redges))
        return laid_out, view, redges, set(), reversed_edges

    def compute_path(flow):
        """Path view: one flow's end-to-end chain. None if it can't be drawn."""
        chain = _resolve_chain(flow, full_index, full_parsed_subs)
        if chain is None:
            return None
        pedges = single_flow_edges(flow, chain, full_index)
        if not pedges:
            return None
        view = chain_view(full, pedges)
        redges = classify_edges(pedges, port_keys_for(view), set(), unresolved)
        laid_out, reversed_edges = layout(emit_elk_spec(view, redges))
        return laid_out, view, redges, set(), reversed_edges

    # Each task renders one diagram to <stem>.svg. The diagram's placement in
    # the docs is NOT written here — it's derived at build time from the file
    # name + the doc's heading structure (hooks.py). The stems below are the
    # naming contract; planned_stems() re-expresses it for the derivation guard.
    tasks: list = []

    # 1. System diagram (all subsystems collapsed) → inlined on the landing page.
    tasks.append(lambda: emit(*compute_subsystem(set(), all_collapsed=True), embed.qualified_name(system_name)))

    # 2. One subsystem diagram per subsystem (that subsystem expanded).
    for sub in sorted(s for s in all_subs if s not in unresolved):

        def _sub_task(sub=sub):
            emit(*compute_subsystem({sub}, all_collapsed=False), embed.qualified_name(sub))

        tasks.append(_sub_task)

    # 3. One device diagram per device (flow trace).
    for d in full.devices:
        if d.subsystem in unresolved:
            continue

        def _device_task(d=d):
            res = compute_trace(d.name, None)
            if res is None:
                print(f"note: no flows touch device {d.name}; skipped", file=sys.stderr)
                return
            emit(*res, embed.qualified_name(d.subsystem, d.name), link_subs={dd.subsystem for dd in res[1].devices})

        tasks.append(_device_task)

    # 4. One component diagram per component (flow trace). Qualified to the owning
    # device, so a component name shared by two devices (e.g. sshd on both SOMs)
    # renders separately rather than merging by name.
    for d in full.devices:
        if d.subsystem in unresolved:
            continue
        for c in d.components:

            def _comp_task(d=d, c=c):
                res = compute_trace(d.name, c.name)
                if res is None:
                    print(f"note: no flows touch component {d.name} > {c.name}; skipped", file=sys.stderr)
                    return
                emit(
                    *res,
                    embed.qualified_name(d.subsystem, d.name, c.name),
                    link_subs={dd.subsystem for dd in res[1].devices},
                )

            tasks.append(_comp_task)

    # 5. One path diagram per flow (its end-to-end chain).
    for fl in flows:
        if fl.subsystem in unresolved:
            continue

        def _flow_task(fl=fl):
            res = compute_path(fl)
            if res is None:
                print(
                    f"note: flow '{_flow_name(fl)}' on '{_fmt_key(fl.source)}' has no drawable path; skipped",
                    file=sys.stderr,
                )
                return
            stem = _flow_stem(fl)
            emit(*res, stem, link_subs={dd.subsystem for dd in res[1].devices})

        tasks.append(_flow_task)

    # 6. One diagram per interface (flow trace through that single interface).
    # Iterate CANONICAL interfaces off full.devices — not by_interface(), which
    # carries several key spellings per interface and would render each one
    # multiple times. _heading_for matches the resolved-chain keys trace_flows sees.
    for d in full.devices:
        if d.subsystem in unresolved:
            continue
        ifaces = [(None, i) for i in d.interfaces] + [(c, i) for c in d.components for i in c.interfaces]
        for comp, iface in ifaces:

            def _iface_task(d=d, comp=comp, iface=iface):
                heading = _heading_for(d, comp, iface)
                res = compute_trace(iface_key=(d.subsystem, heading))
                if res is None:
                    print(f"note: no flows pass through interface {d.subsystem} > {heading}; skipped", file=sys.stderr)
                    return
                emit(
                    *res,
                    embed.qualified_name(d.subsystem, *heading.split(" > ")),
                    link_subs={dd.subsystem for dd in res[1].devices},
                )

            tasks.append(_iface_task)

    # 7. One aggregate diagram per multi-flow token — the flows it represents,
    # drawn together. Referenced only from those edge labels (no doc placement).
    for agg_stem, agg_flows in aggregates.items():

        def _agg_task(stem=agg_stem, fl=agg_flows):
            res = compute_aggregate(fl)
            if res is None:
                print(f"note: aggregate '{stem}' has no drawable path; skipped", file=sys.stderr)
                return
            emit(*res, stem, link_subs={dd.subsystem for dd in res[1].devices})

        tasks.append(_agg_task)

    # Render every diagram across the worker pool. A thread blocked on a node
    # round-trip releases the GIL, so the pool's workers stay saturated. Tasks
    # only write their own SVG (no shared state, no source-doc edits), so order
    # doesn't matter; an exception in any task propagates here and aborts the run.
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for _ in ex.map(lambda t: t(), tasks):
                pass
    finally:
        _ELK_POOL.close()
        _RENDER_POOL.close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
