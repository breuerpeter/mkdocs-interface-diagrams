from __future__ import annotations

import re
import sys
import threading

from PIL import ImageFont

from interface_diagrams import workers as _workers
from interface_diagrams.model import (
    System,
    Device,
    Component,
    RenderEdge,
    _heading_for,
)

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


# Worker pool injected by generate.py after pool creation.
# run_elk reads this module-level variable, which generate.py sets at startup.
_ELK_POOL = None


def run_elk(spec: dict) -> dict:
    # Use generate._ELK_POOL when available (the authoritative pool that tests can
    # patch); fall back to elk._ELK_POOL for callers that inject directly here.
    import interface_diagrams.generate as _gen
    pool = _gen._ELK_POOL if _gen._ELK_POOL is not None else _ELK_POOL
    if pool is None:
        raise RuntimeError("elk worker pool not initialized; call generate_section first")
    resp = pool.call(spec)
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
