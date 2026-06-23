"""Graph derivation: chain resolution, edge derivation + validation lints,
per-diagram edge classification, and the view filters."""

import unittest
from typing import ClassVar

from interface_diagrams import embed
from interface_diagrams.generate import (
    Device,
    Edge,
    Interface,
    System,
    _resolve_chain,
    aggregate_diagrams,
    chain_view,
    classify_edges,
    collapse_system,
    derive_edges,
    drawable_flow_stems,
    filter_system,
    merge_labels,
    port_keys_for,
    single_flow_edges,
    trace_flows,
)

from .helpers import PipelineTestCase, make_flow, make_system


def _stem(payload, label, source, subsystem="SubA"):
    """The path-diagram stem a flow gets — the naming contract embed/hooks share."""
    return embed.qualified_name(subsystem, *source[1].split(" > "), payload, label)


class MergeLabels(unittest.TestCase):
    def test_dedups_preserving_first_seen_order(self):
        self.assertEqual(merge_labels(["MAVLink", "SBus", "MAVLink"]), "MAVLink, SBus")

    def test_strips_and_drops_blanks(self):
        self.assertEqual(merge_labels(["  RTCM3 ", "", "RTCM3"]), "RTCM3")


class ResolveChain(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.sys = make_system()
        self.index = self.sys.by_interface()
        self.parsed = {d.subsystem for d in self.sys.devices}

    def test_resolves_to_canonical_keys_and_device_identity(self):
        chain = _resolve_chain(make_flow(), self.index, self.parsed)
        self.assertEqual(chain[0], (("SubA", "DevA > proc > udp:1"), ("SubA", "DevA")))
        self.assertEqual(chain[-1], (("SubB", "DevB > srv > tcp:2"), ("SubB", "DevB")))
        self.assertEqual(self.warnings, 0)

    def test_unparsed_subsystem_passes_through_as_stub_identity(self):
        f = make_flow(waypoints=(("Ghost", "Dev > if"),))
        chain = _resolve_chain(f, self.index, self.parsed)
        self.assertEqual(chain[1], (("Ghost", "Dev > if"), ("Ghost", "Dev > if")))
        self.assertEqual(self.warnings, 0)

    def test_dangling_waypoint_in_parsed_subsystem_drops_flow(self):
        f = make_flow(waypoints=(("SubB", "DevB > nope"),))
        self.assertIsNone(_resolve_chain(f, self.index, self.parsed))
        self.assertEqual(self.warnings, 1)


class DeriveEdges(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.sys = make_system()

    def test_basic_chain_yields_segment_edges(self):
        edges = derive_edges([make_flow()], self.sys)
        pairs = [(e.src_key, e.dst_key) for e in edges]
        self.assertIn((("SubA", "DevA > proc > udp:1"), ("SubA", "DevA > eth0")), pairs)
        self.assertIn((("SubA", "DevA > eth0"), ("SubB", "DevB > eth0")), pairs)
        self.assertIn((("SubB", "DevB > eth0"), ("SubB", "DevB > srv > tcp:2")), pairs)
        self.assertTrue(all(e.direction == "out" for e in edges))
        self.assertEqual(self.warnings, 0)

    def test_opposite_traversals_merge_to_both(self):
        out = make_flow(label="tx", source=("SubA", "DevA > eth0"), waypoints=(("SubB", "DevB > eth0"),))
        back = make_flow(label="rx", source=("SubB", "DevB > eth0"), waypoints=(("SubA", "DevA > eth0"),))
        edges = derive_edges([out, back], self.sys)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].direction, "both")

    def test_shared_segment_merges_payload_labels(self):
        a = make_flow(
            payload="MAVLink", label="a", source=("SubA", "DevA > eth0"), waypoints=(("SubB", "DevB > eth0"),)
        )
        b = make_flow(payload="RTCM3", label="b", source=("SubA", "DevA > eth0"), waypoints=(("SubB", "DevB > eth0"),))
        edges = derive_edges([a, b], self.sys)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].payload, "MAVLink, RTCM3")

    def test_bare_device_forwarding_traversal_is_implied_not_drawn(self):
        # Hub has two bare device interfaces; in -> out is implied by the box.
        f = make_flow(
            source=("SubA", "DevA > eth0"),
            waypoints=(("SubA", "Hub > in"), ("SubA", "Hub > out"), ("SubB", "DevB > eth0")),
        )
        edges = derive_edges([f], self.sys)
        pairs = [(e.src_key, e.dst_key) for e in edges]
        self.assertNotIn((("SubA", "Hub > in"), ("SubA", "Hub > out")), pairs)
        # ...but the wires in and out of the hub still draw.
        self.assertIn((("SubA", "DevA > eth0"), ("SubA", "Hub > in")), pairs)
        self.assertIn((("SubA", "Hub > out"), ("SubB", "DevB > eth0")), pairs)

    def test_device_to_component_traversal_draws(self):
        f = make_flow(source=("SubA", "DevA > proc > udp:1"), waypoints=(("SubA", "DevA > eth0"),))
        edges = derive_edges([f], self.sys)
        self.assertEqual(len(edges), 1)

    def test_cross_device_hop_onto_component_port_is_flagged(self):
        f = make_flow(source=("SubA", "DevA > eth0"), waypoints=(("SubB", "DevB > srv > tcp:2"),))
        derive_edges([f], self.sys)
        self.assertEqual(self.warnings, 1)
        self.assertIn("cross-device", self.stderr_text)

    def test_duplicate_flow_is_flagged(self):
        derive_edges([make_flow(), make_flow()], self.sys)
        self.assertEqual(self.warnings, 1)
        self.assertIn("duplicate flow", self.stderr_text)

    def test_empty_chain_is_degenerate(self):
        derive_edges([make_flow(waypoints=())], self.sys)
        self.assertEqual(self.warnings, 1)
        self.assertIn("degenerate", self.stderr_text)

    def test_consecutive_identical_waypoints_flagged(self):
        f = make_flow(source=("SubA", "DevA > eth0"), waypoints=(("SubA", "DevA > eth0"),))
        derive_edges([f], self.sys)
        self.assertEqual(self.warnings, 1)

    def test_labelless_flow_flagged(self):
        derive_edges([make_flow(label=None)], self.sys)
        self.assertEqual(self.warnings, 1)
        self.assertIn("bold label", self.stderr_text)

    def test_device_interface_endpoint_soft_warns_when_components_exist(self):
        # DevA has components, so sourcing at its bare eth0 is advisory-flagged.
        f = make_flow(
            source=("SubA", "DevA > eth0"), waypoints=(("SubB", "DevB > srv > tcp:2"), ("SubB", "DevB > eth0"))
        )
        derive_edges([f], self.sys)
        self.assertGreaterEqual(self.soft_warnings, 1)

    def test_componentless_device_endpoint_does_not_soft_warn(self):
        # Hub has no components — a microcontroller IS its firmware.
        f = make_flow(source=("SubA", "DevA > proc > udp:1"), waypoints=(("SubA", "DevA > eth0"), ("SubA", "Hub > in")))
        derive_edges([f], self.sys)
        self.assertEqual(self.soft_warnings, 0)


class EdgeLabelTokens(PipelineTestCase):
    """derive_edges/trace_flows annotate each payload token with a link to its
    flow's path diagram — but ONLY when exactly one drawable flow of that
    payload base crosses the segment. A token deduped from several flows can't
    pick a single diagram, so it stays unlinked."""

    SRC = ("SubA", "DevA > eth0")

    def setUp(self):
        super().setUp()
        self.sys = make_system()
        self.parsed = {d.subsystem for d in self.sys.devices}

    def _wire(self, payload, label):
        """A single one-segment flow DevA(eth0) -> DevB(eth0)."""
        return make_flow(payload=payload, label=label, source=self.SRC, waypoints=(("SubB", "DevB > eth0"),))

    def test_single_flow_token_links_to_its_path_diagram(self):
        f = self._wire("MAVLink", "telemetry")
        stem = _stem("MAVLink", "telemetry", self.SRC)
        edges = derive_edges([f], self.sys, drawable_stems={stem})
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].payload, "MAVLink")
        self.assertEqual(edges[0].tokens, (("MAVLink", f"{stem}.svg"),))

    def test_deduped_multi_flow_token_links_to_aggregate(self):
        # A token deduped from several flows links to a synthetic aggregate
        # diagram (showing all those flows), keyed deterministically by the set.
        a, b = self._wire("MAVLink", "primary"), self._wire("MAVLink", "backup")
        drawable = {_stem("MAVLink", "primary", self.SRC), _stem("MAVLink", "backup", self.SRC)}
        edges = derive_edges([a, b], self.sys, drawable_stems=drawable)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].payload, "MAVLink")
        ((text, link),) = edges[0].tokens
        self.assertEqual(text, "MAVLink")
        self.assertTrue(link.startswith("multiflow-mavlink-"))
        self.assertTrue(link.endswith(".svg"))

    def test_aggregate_stem_is_deterministic_and_set_keyed(self):
        # Same flow set -> same stem (so links match the generated file and
        # identical sets across segments dedup); order-independent.
        from interface_diagrams.generate import _aggregate_stem

        s = {"a-b-c", "x-y-z"}
        self.assertEqual(_aggregate_stem("MAVLink", s), _aggregate_stem("MAVLink", set(s)))
        self.assertNotEqual(_aggregate_stem("MAVLink", s), _aggregate_stem("MAVLink", {"a-b-c"}))

    def test_mixed_label_links_each_token_to_its_diagram(self):
        flows = [self._wire("MAVLink", "a"), self._wire("MAVLink", "b"), self._wire("RTSP", "c")]
        rtsp_stem = _stem("RTSP", "c", self.SRC)
        drawable = {rtsp_stem, _stem("MAVLink", "a", self.SRC), _stem("MAVLink", "b", self.SRC)}
        edges = derive_edges(flows, self.sys, drawable_stems=drawable)
        self.assertEqual(edges[0].payload, "MAVLink, RTSP")
        toks = dict(edges[0].tokens)
        self.assertTrue(toks["MAVLink"].startswith("multiflow-mavlink-"))  # 2 flows
        self.assertEqual(toks["RTSP"], f"{rtsp_stem}.svg")  # 1 flow

    def test_single_flow_token_unlinked_when_its_diagram_was_not_emitted(self):
        f = self._wire("MAVLink", "telemetry")
        edges = derive_edges([f], self.sys, drawable_stems=set())
        self.assertEqual(edges[0].tokens, (("MAVLink", None),))

    def test_default_no_drawable_stems_links_nothing(self):
        # The --check call passes no drawable set; tokens carry text, no links.
        f = self._wire("MAVLink", "telemetry")
        edges = derive_edges([f], self.sys)
        self.assertEqual(edges[0].tokens, (("MAVLink", None),))

    def test_trace_flows_links_token_via_token_links(self):
        # A focused view links each token to the full-graph diagram for that
        # (segment, payload), supplied via token_links.
        f = self._wire("MAVLink", "telemetry")
        stem = _stem("MAVLink", "telemetry", self.SRC)
        pair = frozenset({("SubA", "DevA > eth0"), ("SubB", "DevB > eth0")})
        _hits, edges = trace_flows(
            [f], self.sys, "DevB", None, self.parsed, token_links={(pair, "MAVLink"): f"{stem}.svg"}
        )
        self.assertTrue(edges)
        self.assertEqual(edges[0].tokens, (("MAVLink", f"{stem}.svg"),))

    def test_derive_edges_uses_token_links_when_given(self):
        # Aggregate diagrams pass token_links so their own labels stay clickable,
        # pointing at the same full-graph diagram for each (segment, payload).
        f = self._wire("MAVLink", "telemetry")
        pair = frozenset({("SubA", "DevA > eth0"), ("SubB", "DevB > eth0")})
        edges = derive_edges([f], self.sys, token_links={(pair, "MAVLink"): "agg.svg"})
        self.assertEqual(edges[0].tokens, (("MAVLink", "agg.svg"),))

    def test_multi_flow_token_link_matches_aggregate_registry_key(self):
        # The link a multi-flow token gets MUST equal the file aggregate_diagrams
        # emits — both compute the aggregate stem independently from the same flow
        # set, so a divergence would silently dead-link every multi-flow token.
        a, b = self._wire("MAVLink", "primary"), self._wire("MAVLink", "backup")
        drawable = drawable_flow_stems([a, b], self.sys, set())
        edges = derive_edges([a, b], self.sys, drawable_stems=drawable)
        ((_text, link),) = edges[0].tokens
        (agg_stem,) = aggregate_diagrams([a, b], self.sys).keys()
        self.assertEqual(link, f"{agg_stem}.svg")

    def test_every_token_link_resolves_to_a_generated_target(self):
        # The no-dead-link invariant across the three functions that must agree:
        # every link derive_edges emits is a drawable per-flow stem OR an
        # aggregate_diagrams key. (Single-flow -> per-flow; multi -> aggregate.)
        flows = [self._wire("MAVLink", "a"), self._wire("MAVLink", "b"), self._wire("RTSP", "c")]
        drawable = drawable_flow_stems(flows, self.sys, set())
        aggs = set(aggregate_diagrams(flows, self.sys))
        edges = derive_edges(flows, self.sys, drawable_stems=drawable)
        targets = {link[:-4] for e in edges for _t, link in e.tokens if link}
        self.assertTrue(targets)
        for t in targets:
            self.assertTrue(t in drawable or t in aggs, f"dead link target: {t}")

    def test_aggregate_diagrams_collects_multi_flow_token_flows(self):
        a, b = self._wire("MAVLink", "primary"), self._wire("MAVLink", "backup")
        aggs = aggregate_diagrams([a, b], self.sys)
        self.assertEqual(len(aggs), 1)
        stem, agg_flows = next(iter(aggs.items()))
        self.assertTrue(stem.startswith("multiflow-mavlink-"))
        self.assertEqual({f.label for f in agg_flows}, {"primary", "backup"})

    def test_aggregate_diagrams_skips_single_flow_tokens(self):
        self.assertEqual(aggregate_diagrams([self._wire("MAVLink", "x")], self.sys), {})

    def test_path_diagram_edges_carry_no_token_links(self):
        # A flow's OWN path diagram must not self-link its labels.
        f = self._wire("MAVLink", "telemetry")
        index = self.sys.by_interface()
        chain = _resolve_chain(f, index, self.parsed)
        edges = single_flow_edges(f, chain, index)
        self.assertTrue(edges)
        self.assertTrue(all(e.tokens == () for e in edges))


class DrawableFlowStems(PipelineTestCase):
    """The set of stems whose path diagram actually gets emitted — what an
    edge-label link may point at. Mirrors the flow-render task's skip rules."""

    def setUp(self):
        super().setUp()
        self.sys = make_system()

    def test_returns_stems_of_emittable_flows(self):
        f = make_flow()  # resolvable, multi-segment → drawn
        self.assertEqual(
            drawable_flow_stems([f], self.sys, set()), {_stem("MAVLink", "telemetry", ("SubA", "DevA > proc > udp:1"))}
        )

    def test_excludes_flows_in_unresolved_subsystems(self):
        self.assertEqual(drawable_flow_stems([make_flow()], self.sys, {"SubA"}), set())

    def test_excludes_flow_with_no_drawable_segment(self):
        # Hub's in->out is a same-box forwarding hop — implied, never drawn — so
        # the flow emits no path diagram and its stem is not linkable.
        f = make_flow(source=("SubA", "Hub > in"), waypoints=(("SubA", "Hub > out"),))
        self.assertEqual(drawable_flow_stems([f], self.sys, set()), set())

    def test_excludes_dangling_flow(self):
        f = make_flow(waypoints=(("SubB", "DevB > nope"),))
        self.assertEqual(drawable_flow_stems([f], self.sys, set()), set())
        self.assertEqual(self.warnings, 1)


class TraceFlows(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.sys = make_system()
        self.parsed = {d.subsystem for d in self.sys.devices}

    def test_device_focus_matches_component_level_traffic(self):
        hits, edges = trace_flows([make_flow()], self.sys, "DevB", None, self.parsed)
        self.assertEqual(len(hits), 1)
        self.assertTrue(edges)

    def test_component_focus_is_qualified_to_its_device(self):
        hits, _ = trace_flows([make_flow()], self.sys, "DevA", "srv", self.parsed)
        self.assertEqual(hits, [])  # srv lives on DevB, not DevA
        hits, _ = trace_flows([make_flow()], self.sys, "DevB", "srv", self.parsed)
        self.assertEqual(len(hits), 1)

    def test_untouched_device_traces_nothing(self):
        hits, edges = trace_flows([make_flow()], self.sys, "Hub", None, self.parsed)
        self.assertEqual((hits, edges), ([], []))


class SingleFlowEdges(PipelineTestCase):
    def test_path_edges_carry_the_flow_payload(self):
        sys_ = make_system()
        f = make_flow()
        index = sys_.by_interface()
        chain = _resolve_chain(f, index, {d.subsystem for d in sys_.devices})
        edges = single_flow_edges(f, chain, index)
        self.assertTrue(edges)
        self.assertTrue(all(e.payload == "MAVLink" for e in edges))


class ClassifyEdges(PipelineTestCase):
    E = Edge(src_key=("SubA", "DevA > eth0"), dst_key=("SubB", "DevB > eth0"), direction="out", payload="P")

    def test_both_endpoints_rendered_draws_normally(self):
        keys = {self.E.src_key, self.E.dst_key}
        out = classify_edges([self.E], keys, collapsed=set())
        self.assertEqual([r.stub_key for r in out], [None])

    def test_edge_internal_to_a_collapsed_subsystem_drops(self):
        e = Edge(("SubA", "DevA > eth0"), ("SubA", "Hub > in"), "out", "P")
        out = classify_edges([e], {e.src_key, e.dst_key}, collapsed={"SubA"})
        self.assertEqual(out, [])

    def test_unresolved_endpoint_becomes_a_stub(self):
        e = Edge(("SubA", "DevA > eth0"), ("Ghost", "Dev > if"), "out", "P")
        out = classify_edges([e], {e.src_key}, collapsed=set(), unresolved={"Ghost"})
        self.assertEqual([r.stub_key for r in out], [("Ghost", "Dev > if")])

    def test_resolvable_but_unrendered_endpoint_drops_the_edge(self):
        out = classify_edges([self.E], {self.E.src_key}, collapsed=set())
        self.assertEqual(out, [])


class Views(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.sys = make_system()

    def test_port_keys_cover_device_and_component_interfaces(self):
        keys = port_keys_for(self.sys)
        self.assertIn(("SubA", "DevA > eth0"), keys)
        self.assertIn(("SubA", "DevA > proc > udp:1"), keys)
        self.assertIn(("SubB", "DevB > srv > tcp:2"), keys)

    def test_chain_view_keeps_only_edge_endpoints(self):
        edges = [Edge(("SubA", "DevA > eth0"), ("SubB", "DevB > eth0"), "out", "P")]
        view = chain_view(self.sys, edges)
        self.assertEqual({d.name for d in view.devices}, {"DevA", "DevB"})
        deva = next(d for d in view.devices if d.name == "DevA")
        self.assertEqual([i.name for i in deva.interfaces], ["eth0"])
        self.assertEqual(deva.components, [])  # udp:1 not an endpoint

    def test_filter_system_by_subsystem(self):
        view = filter_system(self.sys, {"SubB"}, set(), set())
        self.assertEqual({d.subsystem for d in view.devices}, {"SubB"})

    def test_collapse_keeps_only_boundary_crossing_interfaces(self):
        edges = [Edge(("SubA", "DevA > eth0"), ("SubB", "DevB > eth0"), "out", "P")]
        view = collapse_system(self.sys, {"SubA"}, edges)
        names = {d.name for d in view.devices}
        self.assertIn("DevA", names)  # eth0 crosses to SubB
        self.assertNotIn("Hub", names)  # no crossing edge — drops entirely
        deva = next(d for d in view.devices if d.name == "DevA")
        self.assertEqual(deva.components, [])  # components drop wholesale

    def test_collapse_ignores_crossings_to_out_of_view_subsystems(self):
        edges = [Edge(("SubA", "DevA > eth0"), ("Ghost", "Dev > if"), "out", "P")]
        view = collapse_system(self.sys, {"SubA"}, edges)
        self.assertNotIn("DevA", {d.name for d in view.devices})

    # A subsystem-detail diagram expands one focus and collapses its neighbours.
    # Two independent neighbours of the focus may also link to EACH OTHER; that
    # lateral neighbour↔neighbour link (and any port that exists only for it) is
    # second-degree detail relative to the focus and must not be dragged in.
    LATERAL_SYS = System(
        name="Sys",
        devices=[
            Device(name="Rf", subsystem="F", interfaces=[Interface("r")]),
            Device(name="Flux", subsystem="N1", interfaces=[Interface("flux")]),
            Device(name="Gcu", subsystem="N1", interfaces=[Interface("gcu")]),
            Device(name="Fmu", subsystem="N2", interfaces=[Interface("fmu")]),
        ],
    )
    LATERAL_EDGES: ClassVar = [
        Edge(("N1", "Flux > flux"), ("F", "Rf > r"), "out", "P"),  # N1 <-> focus
        Edge(("N2", "Fmu > fmu"), ("F", "Rf > r"), "out", "P"),  # N2 <-> focus
        Edge(("N1", "Gcu > gcu"), ("N2", "Fmu > fmu"), "out", "P"),  # N1 <-> N2 lateral
    ]

    def test_collapse_with_focus_drops_lateral_neighbour_only_interfaces(self):
        view = collapse_system(self.LATERAL_SYS, {"N1", "N2"}, self.LATERAL_EDGES, focus={"F"})
        names = {d.name for d in view.devices}
        self.assertIn("Flux", names)  # crosses to the focus -> kept
        self.assertIn("Fmu", names)  # crosses to the focus -> kept
        self.assertNotIn("Gcu", names)  # only crosses to a sibling -> drops

    def test_collapse_without_focus_keeps_links_between_drawn_boxes(self):
        # The focus-less system diagram (all subsystems collapsed) must still
        # show every link between two drawn boxes — Gcu survives here.
        view = collapse_system(self.LATERAL_SYS, {"F", "N1", "N2"}, self.LATERAL_EDGES)
        self.assertIn("Gcu", {d.name for d in view.devices})

    def test_derived_views_keep_display_names(self):
        # Subsystem keys are lowercase file stems; labels render the H1 display
        # name, which every view derivation must carry through.
        self.sys.display_names["SubA"] = "Sub A (display)"
        edges = [Edge(("SubA", "DevA > eth0"), ("SubB", "DevB > eth0"), "out", "P")]
        for view in (
            chain_view(self.sys, edges),
            filter_system(self.sys, {"SubA"}, set(), set()),
            collapse_system(self.sys, {"SubA"}, edges),
        ):
            self.assertEqual(view.display_names.get("SubA"), "Sub A (display)")


if __name__ == "__main__":
    unittest.main()
