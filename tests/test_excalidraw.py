"""build_excalidraw: ELK layout → Excalidraw scene. Runs on hand-built
laid-out graphs so no node/elkjs is needed — geometry helpers, palette
stability, determinism, element links, and arrow semantics."""

import json
import unittest

from interface_diagrams.generate import (
    COMPONENT_PALETTE,
    SUBSYSTEM_PALETTE_BRIGHT,
    SUBSYSTEM_PALETTE_DARK,
    Edge,
    RenderEdge,
    System,
    _component_fill,
    _normalize_luminance,
    _port_id_from_key,
    _stub_id,
    _trim_polyline,
    _wrapped_back_edges,
    build_excalidraw,
    component_fill_color,
    device_fill_color,
    subsystem_color,
)

from .helpers import PipelineTestCase

ALL_SUBS = ["SubA", "SubB"]
PORT_A = _port_id_from_key(("SubA", "DevA > eth0"))
PORT_B = _port_id_from_key(("SubB", "DevB > eth0"))


class GeometryHelpers(unittest.TestCase):
    def test_trim_pulls_end_back_along_final_segment(self):
        pts = [(0.0, 0.0), (10.0, 0.0)]
        _trim_polyline(pts, -1, 4)
        self.assertEqual(pts, [(0.0, 0.0), (6.0, 0.0)])

    def test_trim_pulls_start_forward(self):
        pts = [(0.0, 0.0), (10.0, 0.0)]
        _trim_polyline(pts, 0, 4)
        self.assertEqual(pts, [(4.0, 0.0), (10.0, 0.0)])

    def test_trim_is_noop_when_segment_too_short(self):
        pts = [(0.0, 0.0), (3.0, 0.0)]
        _trim_polyline(pts, -1, 4)
        self.assertEqual(pts, [(0.0, 0.0), (3.0, 0.0)])


class Palettes(unittest.TestCase):
    def test_colors_are_keyed_by_sorted_position(self):
        # 'all_subs' is the FULL system list so colors agree across diagrams.
        self.assertEqual(subsystem_color("SubA", ALL_SUBS), SUBSYSTEM_PALETTE_BRIGHT[0])
        self.assertEqual(subsystem_color("SubB", ALL_SUBS), SUBSYSTEM_PALETTE_BRIGHT[1])
        self.assertEqual(device_fill_color("SubB", ALL_SUBS), SUBSYSTEM_PALETTE_DARK[1])
        self.assertEqual(component_fill_color("SubA", ALL_SUBS), COMPONENT_PALETTE[0])

    def test_palette_wraps_modulo(self):
        subs = [f"S{i:02d}" for i in range(len(SUBSYSTEM_PALETTE_BRIGHT) + 1)]
        self.assertEqual(subsystem_color(subs[-1], subs), SUBSYSTEM_PALETTE_BRIGHT[0])


def device(sub, dev, port_id, x, port_x):
    """A laid-out device with one W- or E-side port (port_x relative)."""
    return {
        "id": f"dev__{sub}__{dev}",
        "x": 20,
        "y": 40,
        "width": 120,
        "height": 80,
        "labels": [{"text": dev, "x": 8, "y": 8, "width": 50, "height": 18}],
        "ports": [
            {
                "id": port_id,
                "x": port_x,
                "y": 36,
                "width": 8,
                "height": 8,
                "labels": [{"text": "eth0", "x": -34 if port_x < 0 else 12, "y": 10, "width": 30, "height": 14}],
            }
        ],
    }


def laid_out(edges=(), extra_children=()):
    """Two laid-out subsystems: SubA/DevA (W port), SubB/DevB (E port)."""
    return {
        "id": "root",
        "children": [
            {
                "id": "sub__SubA",
                "x": 0,
                "y": 0,
                "width": 300,
                "height": 200,
                "labels": [{"text": "SubA"}],
                "children": [device("SubA", "DevA", PORT_A, 20, -4)],
            },
            {
                "id": "sub__SubB",
                "x": 400,
                "y": 0,
                "width": 300,
                "height": 200,
                "labels": [{"text": "SubB"}],
                "children": [device("SubB", "DevB", PORT_B, 20, 116)],
            },
            *extra_children,
        ],
        "edges": list(edges),
    }


def edge(src=PORT_A, dst=PORT_B, label="MAVLink"):
    return {
        "id": "e_0",
        "sources": [src],
        "targets": [dst],
        "sections": [{"startPoint": {"x": 16, "y": 80}, "endPoint": {"x": 556, "y": 80}, "bendPoints": []}],
        "labels": [{"text": label, "x": 250, "y": 60, "width": 50, "height": 14}],
    }


def redge(direction="out", stub_key=None, dst=("SubB", "DevB > eth0")):
    return RenderEdge(
        Edge(src_key=("SubA", "DevA > eth0"), dst_key=dst, direction=direction, payload="MAVLink"), stub_key=stub_key
    )


def by_type(scene, t):
    return [e for e in scene["elements"] if e["type"] == t]


def texts(scene):
    return {e["text"]: e for e in by_type(scene, "text")}


class BuildScene(PipelineTestCase):
    def scene(self, **kw):
        return build_excalidraw(laid_out([edge()]), None, [redge()], ALL_SUBS, **kw)

    def test_deterministic_output(self):
        a, b = self.scene(), self.scene()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_subsystem_box_is_dotted_with_its_accent(self):
        rects = by_type(self.scene(), "rectangle")
        sub_rects = [r for r in rects if r["strokeStyle"] == "dotted"]
        self.assertEqual(
            {r["strokeColor"] for r in sub_rects}, {SUBSYSTEM_PALETTE_BRIGHT[0], SUBSYSTEM_PALETTE_BRIGHT[1]}
        )

    def test_device_body_filled_with_normalized_accent_and_no_outline(self):
        # The whole device body is filled with the subsystem accent normalised to
        # a uniform luminance (not the pale pastel, which would invert to a
        # near-black block in dark mode) and has NO outline, so an edge routed
        # across it doesn't read as crossing a hard box.
        rects = by_type(self.scene(), "rectangle")
        fills = {r["backgroundColor"] for r in rects}
        self.assertNotIn(SUBSYSTEM_PALETTE_DARK[0], fills)
        self.assertNotIn(SUBSYSTEM_PALETTE_DARK[1], fills)
        dev_fill_a = _normalize_luminance(SUBSYSTEM_PALETTE_BRIGHT[0])
        self.assertIn(dev_fill_a, fills)
        self.assertIn(_normalize_luminance(SUBSYSTEM_PALETTE_BRIGHT[1]), fills)
        dev_rects = [r for r in rects if r["backgroundColor"] == dev_fill_a]
        self.assertTrue(dev_rects and all(r["strokeColor"] == "transparent" for r in dev_rects))

    def test_normalized_chip_fills_share_one_luminance(self):
        # The whole point: every device chip lands on the same luminance, so a
        # single dark label-text colour reads consistently on all of them.
        def lum(h):
            c = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]

        a = lum(_normalize_luminance(SUBSYSTEM_PALETTE_BRIGHT[0]))
        b = lum(_normalize_luminance(SUBSYSTEM_PALETTE_BRIGHT[3]))
        self.assertAlmostEqual(a, b, places=2)

    def test_device_label_text_is_uniformly_dark(self):
        t = texts(self.scene())
        self.assertEqual(t["DevA"]["strokeColor"], "#1e1e1e")
        self.assertEqual(t["DevB"]["strokeColor"], "#1e1e1e")

    def test_device_title_links_to_its_detail_diagram(self):
        t = texts(self.scene())
        self.assertEqual(t["DevA"]["link"], "suba-deva.svg")
        self.assertEqual(t["DevB"]["link"], "subb-devb.svg")

    def test_port_label_links_to_its_documented_section(self):
        labels = [e for e in by_type(self.scene(), "text") if e["text"] == "eth0"]
        self.assertEqual({lbl["link"] for lbl in labels}, {"SubA.md#DevA > eth0", "SubB.md#DevB > eth0"})

    def test_subsystem_title_renders_display_name_but_links_by_stem(self):
        # Subsystem keys are lowercase file stems; the title shows the H1
        # display name while the link still targets the stem-named .svg.
        sys_ = System(name="view", display_names={"SubA": "Sub A (display)"})
        scene = build_excalidraw(laid_out([edge()]), sys_, [redge()], ALL_SUBS, link_subs={"SubA"})
        t = texts(scene)
        self.assertIn("Sub A (display)", t)
        self.assertEqual(t["Sub A (display)"]["link"], "suba.svg")

    def test_subsystem_titles_link_only_when_in_link_subs(self):
        t = texts(self.scene(link_subs={"SubB"}))
        self.assertIsNone(t["SubA"]["link"])
        self.assertEqual(t["SubB"]["link"], "subb.svg")
        # Default (link_subs=None) falls back to `collapsed`.
        t = texts(build_excalidraw(laid_out([edge()]), None, [redge()], ALL_SUBS, collapsed={"SubA"}))
        self.assertEqual(t["SubA"]["link"], "suba.svg")
        self.assertIsNone(t["SubB"]["link"])

    def test_unidirectional_arrow_docks_at_port_centre(self):
        scene = self.scene()
        (arrow,) = by_type(scene, "arrow")
        self.assertIsNone(arrow["startArrowhead"])
        self.assertEqual(arrow["endArrowhead"], "triangle")
        # Both ends dock at the centre of the port box, not an edge midpoint.
        self.assertEqual(arrow["startBinding"]["fixedPoint"], [0.5, 0.5])
        self.assertEqual(arrow["endBinding"]["fixedPoint"], [0.5, 0.5])
        # The drawn polyline endpoints are snapped to the port-box centres too
        # (the binding fixedPoint alone doesn't move the static geometry): DevA's
        # port box [16,24]→20, DevB's [536,544]→540.
        self.assertEqual(arrow["x"] + arrow["points"][0][0], 20)
        self.assertEqual(arrow["x"] + arrow["points"][-1][0], 540)
        # Head color = dst subsystem accent.
        self.assertEqual(arrow["strokeColor"], SUBSYSTEM_PALETTE_BRIGHT[1])

    def test_bidirectional_cross_subsystem_arrow_is_neutral_with_two_heads(self):
        scene = build_excalidraw(laid_out([edge()]), None, [redge(direction="both")], ALL_SUBS)
        (arrow,) = by_type(scene, "arrow")
        self.assertEqual(arrow["startArrowhead"], "triangle")
        self.assertEqual(arrow["strokeColor"], "#1e1e1e")

    def test_edge_label_text_matches_arrow_color(self):
        scene = self.scene()
        (arrow,) = by_type(scene, "arrow")
        self.assertEqual(texts(scene)["MAVLink"]["strokeColor"], arrow["strokeColor"])

    def test_stub_edge_is_dotted_and_trimmed_back_from_the_text(self):
        key = ("Ghost", "Dev > if")
        stub = {
            "id": _stub_id(key),
            "x": 560,
            "y": 70,
            "width": 80,
            "height": 14,
            "labels": [{"text": "Ghost > Dev > if"}],
        }
        scene = build_excalidraw(
            laid_out([edge(dst=_stub_id(key))], extra_children=[stub]), None, [redge(stub_key=key, dst=key)], ALL_SUBS
        )
        (arrow,) = by_type(scene, "arrow")
        self.assertEqual(arrow["strokeStyle"], "dotted")
        self.assertIn("Ghost > Dev > if", texts(scene))
        # Endpoint pulled back from x=556 by STUB_ARROW_GAP=8.
        end_x = arrow["x"] + arrow["points"][-1][0]
        self.assertAlmostEqual(end_x, 548.0)

    def test_reversed_edge_renders_in_its_semantic_direction(self):
        # `layout` feeds a wrapped back-edge to ELK with source/target swapped
        # so it routes straight; the laid-out edge therefore runs dst→src
        # (PORT_B → PORT_A, section 556→16). Listing it in `reversed_edges`
        # must reproduce exactly the arrow a normal PORT_A → PORT_B edge draws:
        # head on DevB's E face, tail bound to DevA's W face, dst-accent color.
        rev_in = {
            "id": "e_0",
            "sources": [PORT_B],
            "targets": [PORT_A],
            "sections": [{"startPoint": {"x": 556, "y": 80}, "endPoint": {"x": 16, "y": 80}, "bendPoints": []}],
            "labels": [{"text": "MAVLink", "x": 250, "y": 60, "width": 50, "height": 14}],
        }
        scene = build_excalidraw(laid_out([rev_in]), None, [redge()], ALL_SUBS, reversed_edges={"e_0"})
        (arrow,) = by_type(scene, "arrow")
        self.assertIsNone(arrow["startArrowhead"])
        self.assertEqual(arrow["endArrowhead"], "triangle")
        self.assertEqual(arrow["startBinding"]["fixedPoint"], [0.5, 0.5])  # port centre
        self.assertEqual(arrow["endBinding"]["fixedPoint"], [0.5, 0.5])
        self.assertEqual(arrow["strokeColor"], SUBSYSTEM_PALETTE_BRIGHT[1])
        # Polyline restored to src→dst order and snapped to the port-box centres:
        # starts at DevA's port centre (box [16,24] → 20), ends at DevB's
        # (box [536,544] → 540).
        self.assertEqual(arrow["x"] + arrow["points"][0][0], 20)
        self.assertEqual(arrow["x"] + arrow["points"][-1][0], 540)


def linked_redge(payload, tokens, dst=("SubB", "DevB > eth0")):
    return RenderEdge(
        Edge(src_key=("SubA", "DevA > eth0"), dst_key=dst, direction="out", payload=payload, tokens=tokens)
    )


class EdgeLabelLinks(PipelineTestCase):
    """An edge's payload label splits into per-token text elements so that a
    single-flow token becomes a clickable link to its path diagram, while
    deduped multi-flow tokens (and tokenless labels) stay a plain, single
    centered element."""

    def _scene(self, payload, tokens):
        return build_excalidraw(laid_out([edge(label=payload)]), None, [linked_redge(payload, tokens)], ALL_SUBS)

    def _frags(self, scene):
        # The visible fragments — excluding the transparent full-text backing
        # that carries the single readability halo behind the whole label.
        return [e for e in by_type(scene, "text") if e["id"].startswith("elabel") and e["strokeColor"] != "transparent"]

    def test_single_flow_label_links_to_its_path_diagram(self):
        frags = self._frags(self._scene("MAVLink", (("MAVLink", "flow.svg"),)))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["text"], "MAVLink")
        self.assertEqual(frags[0]["link"], "flow.svg")

    def test_mixed_label_links_only_the_single_flow_token(self):
        scene = self._scene("MAVLink, RTSP", (("MAVLink", None), ("RTSP", "rtsp.svg")))
        t = texts(scene)
        self.assertIsNone(t["MAVLink"]["link"])
        self.assertEqual(t["RTSP"]["link"], "rtsp.svg")
        # The fragments re-concatenate to the original label, comma included...
        frags = self._frags(scene)
        self.assertEqual("".join(f["text"] for f in frags), "MAVLink, RTSP")
        # ...and the separator itself is never a link.
        self.assertIsNone(t[", "]["link"])

    def test_label_with_no_linkable_token_stays_one_centered_element(self):
        # A token deduped from several flows (all unlinked) renders unchanged.
        frags = self._frags(self._scene("MAVLink", (("MAVLink", None),)))
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["text"], "MAVLink")
        self.assertIsNone(frags[0]["link"])
        self.assertEqual(frags[0]["textAlign"], "center")

    def test_tokenless_edge_label_is_unchanged(self):
        # Edges built without tokens (path diagrams) keep the single centered,
        # unlinked label exactly as before.
        scene = build_excalidraw(laid_out([edge()]), None, [redge()], ALL_SUBS)
        frags = self._frags(scene)
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["text"], "MAVLink")
        self.assertIsNone(frags[0]["link"])
        self.assertEqual(frags[0]["textAlign"], "center")


def laid_out_intra_device():
    """One subsystem, one device, TWO components inside it, with an edge between
    the two component ports — a path routed entirely inside the device."""
    p1 = "port__SubA__DevA__C1__p"
    p2 = "port__SubA__DevA__C2__p"
    return {
        "id": "root",
        "children": [
            {
                "id": "sub__SubA",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 200,
                "labels": [{"text": "SubA"}],
                "children": [
                    {
                        "id": "dev__SubA__DevA",
                        "x": 20,
                        "y": 40,
                        "width": 300,
                        "height": 120,
                        "labels": [{"text": "DevA", "x": 8, "y": 8, "width": 50, "height": 18}],
                        "children": [
                            {
                                "id": "comp__SubA__DevA__C1",
                                "x": 10,
                                "y": 30,
                                "width": 80,
                                "height": 60,
                                "labels": [{"text": "C1"}],
                                "ports": [{"id": p1, "x": 80, "y": 26, "width": 8, "height": 8}],
                            },
                            {
                                "id": "comp__SubA__DevA__C2",
                                "x": 200,
                                "y": 30,
                                "width": 80,
                                "height": 60,
                                "labels": [{"text": "C2"}],
                                "ports": [{"id": p2, "x": -8, "y": 26, "width": 8, "height": 8}],
                            },
                        ],
                    }
                ],
            }
        ],
        "edges": [
            {
                "id": "e_0",
                "sources": [p1],
                "targets": [p2],
                "sections": [{"startPoint": {"x": 110, "y": 106}, "endPoint": {"x": 230, "y": 106}, "bendPoints": []}],
            }
        ],
    }


def laid_out_device_iface_to_component():
    """One device with its OWN interface and one component; an edge from the
    device interface to the component interface (still inside the device)."""
    pdev = "port__SubA__DevA__devport"
    pcomp = "port__SubA__DevA__C1__p"
    return {
        "id": "root",
        "children": [
            {
                "id": "sub__SubA",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 200,
                "labels": [{"text": "SubA"}],
                "children": [
                    {
                        "id": "dev__SubA__DevA",
                        "x": 20,
                        "y": 40,
                        "width": 300,
                        "height": 120,
                        "labels": [{"text": "DevA", "x": 8, "y": 8, "width": 50, "height": 18}],
                        "ports": [{"id": pdev, "x": -8, "y": 50, "width": 8, "height": 8}],
                        "children": [
                            {
                                "id": "comp__SubA__DevA__C1",
                                "x": 200,
                                "y": 30,
                                "width": 80,
                                "height": 60,
                                "labels": [{"text": "C1"}],
                                "ports": [{"id": pcomp, "x": -8, "y": 26, "width": 8, "height": 8}],
                            },
                        ],
                    }
                ],
            }
        ],
        "edges": [
            {
                "id": "e_0",
                "sources": [pdev],
                "targets": [pcomp],
                "sections": [{"startPoint": {"x": 12, "y": 90}, "endPoint": {"x": 212, "y": 106}, "bendPoints": []}],
            }
        ],
    }


class IntraDevicePathColor(unittest.TestCase):
    def test_edge_between_components_of_one_device_uses_component_colour(self):
        # Both endpoints are components of the same device → the path is painted
        # the component-box fill (lighter normalised accent), not the subsystem
        # accent it would get for cross-device traffic.
        scene = build_excalidraw(laid_out_intra_device(), None, [redge()], ALL_SUBS)
        (arrow,) = by_type(scene, "arrow")
        self.assertEqual(arrow["strokeColor"], _component_fill(SUBSYSTEM_PALETTE_BRIGHT[0]))
        self.assertNotEqual(arrow["strokeColor"], SUBSYSTEM_PALETTE_BRIGHT[0])

    def test_device_interface_to_own_component_uses_component_colour(self):
        # One endpoint is the device's OWN interface, the other a component of
        # that same device — still a path inside the device, so component colour.
        scene = build_excalidraw(laid_out_device_iface_to_component(), None, [redge()], ALL_SUBS)
        (arrow,) = by_type(scene, "arrow")
        self.assertEqual(arrow["strokeColor"], _component_fill(SUBSYSTEM_PALETTE_BRIGHT[0]))

    def test_cross_device_edge_keeps_subsystem_accent(self):
        # The existing DevA→DevB edge (different devices) is unaffected.
        scene = build_excalidraw(laid_out([edge()]), None, [redge()], ALL_SUBS)
        (arrow,) = by_type(scene, "arrow")
        self.assertEqual(arrow["strokeColor"], SUBSYSTEM_PALETTE_BRIGHT[1])


class WrappedBackEdges(unittest.TestCase):
    """`_wrapped_back_edges` flags only edges ELK routed as a long right-to-left
    perimeter detour — source port right of target AND route overshooting the
    endpoints' span — so `layout` reverses those and leaves clean edges alone."""

    def _laid(self, src_x, dst_x, section):
        sp = _port_id_from_key(("S", "A > p"))
        dp = _port_id_from_key(("S", "B > p"))
        return {
            "id": "root",
            "children": [
                {
                    "id": "dev__S__A",
                    "x": 0,
                    "y": 0,
                    "width": 8,
                    "height": 8,
                    "ports": [{"id": sp, "x": src_x, "y": 0, "width": 8, "height": 8}],
                },
                {
                    "id": "dev__S__B",
                    "x": 0,
                    "y": 0,
                    "width": 8,
                    "height": 8,
                    "ports": [{"id": dp, "x": dst_x, "y": 0, "width": 8, "height": 8}],
                },
            ],
            "edges": [{"id": "e_0", "sources": [sp], "targets": [dp], "sections": [section]}],
        }

    def test_detects_wrapping_back_edge(self):
        # source (x=500) right of target (x=0); route loops out to x=1000 and
        # x=-200 before reaching the target — overshoots the 508px endpoint span.
        wrap = {
            "startPoint": {"x": 500, "y": 0},
            "endPoint": {"x": 0, "y": 0},
            "bendPoints": [{"x": 1000, "y": 0}, {"x": 1000, "y": 100}, {"x": -200, "y": 100}, {"x": -200, "y": 0}],
        }
        self.assertEqual(_wrapped_back_edges(self._laid(500, 0, wrap)), {"e_0"})

    def test_ignores_forward_edge(self):
        straight = {"startPoint": {"x": 0, "y": 0}, "endPoint": {"x": 500, "y": 0}, "bendPoints": []}
        self.assertEqual(_wrapped_back_edges(self._laid(0, 500, straight)), set())

    def test_ignores_short_back_edge(self):
        # A back-edge (source right of target) is fine when it routes locally:
        # no perimeter overshoot, so `layout` must not churn it.
        local = {
            "startPoint": {"x": 500, "y": 0},
            "endPoint": {"x": 0, "y": 0},
            "bendPoints": [{"x": 500, "y": 50}, {"x": 0, "y": 50}],
        }
        self.assertEqual(_wrapped_back_edges(self._laid(500, 0, local)), set())


if __name__ == "__main__":
    unittest.main()
