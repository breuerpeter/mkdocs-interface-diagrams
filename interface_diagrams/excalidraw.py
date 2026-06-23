from __future__ import annotations

import itertools
import random

from interface_diagrams import embed
from interface_diagrams.model import System, RenderEdge, _heading_for
from interface_diagrams.elk import (
    _estimate_text_width,
    _glyph_advance,
    _font_height,
    _font,
    _stub_keys,
    EDGE_LABEL_GAP,
    EDGE_LABEL_END_CLEAR,
    STUB_ARROW_GAP,
    SUBSYSTEM_PALETTE_BRIGHT,
    SUBSYSTEM_PALETTE_DARK,
    COMPONENT_PALETTE,
)

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
