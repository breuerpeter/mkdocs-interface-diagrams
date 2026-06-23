"""ELK spec generation: graph structure, ids, ports, stubs, edges, and the
font-metric sizing helpers the spec depends on."""

import unittest

from interface_diagrams.generate import (
    Edge,
    RenderEdge,
    _all_port_ids,
    _comp_id,
    _dev_id,
    _estimate_text_width,
    _font_height,
    _port_id_from_key,
    _slug,
    _stub_id,
    _stub_label,
    _sub_id,
    emit_elk_spec,
)

from .helpers import PipelineTestCase, make_system


class Ids(unittest.TestCase):
    def test_slug_replaces_non_identifier_chars(self):
        self.assertEqual(_slug("udp:1/x y"), "udp_1_x_y")

    def test_id_namespaces_are_disjoint(self):
        sys_ = make_system()
        deva = sys_.devices[0]
        ids = {
            _sub_id("SubA"),
            _dev_id(deva),
            _comp_id(deva, deva.components[0]),
            _port_id_from_key(("SubA", "DevA > eth0")),
            _stub_id(("SubA", "DevA > eth0")),
        }
        self.assertEqual(len(ids), 5)
        prefixes = {i.split("__")[0] for i in ids}
        self.assertEqual(prefixes, {"sub", "dev", "comp", "port", "stub"})

    def test_stub_label_is_fully_qualified(self):
        self.assertEqual(_stub_label(("pilot_pro", "Tablet > USB")), "pilot_pro > Tablet > USB")

    def test_stub_label_skips_redundant_subsystem_prefix(self):
        self.assertEqual(_stub_label(("Dev machine", "Dev machine > ssh")), "Dev machine > ssh")


class FontMetrics(unittest.TestCase):
    def test_empty_text_has_floor_width(self):
        self.assertEqual(_estimate_text_width(""), 24)

    def test_longer_text_is_wider(self):
        self.assertGreater(_estimate_text_width("a much longer label", 12), _estimate_text_width("ab", 12))

    def test_larger_font_is_wider_and_taller(self):
        self.assertGreater(_estimate_text_width("label", 16), _estimate_text_width("label", 10))
        self.assertGreater(_font_height(16), _font_height(10))


def _edge(src=("SubA", "DevA > eth0"), dst=("SubB", "DevB > eth0"), payload="MAVLink"):
    return Edge(src_key=src, dst_key=dst, direction="out", payload=payload)


class EmitElkSpec(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.sys = make_system()

    def spec(self, redges=()):
        return emit_elk_spec(self.sys, list(redges))

    def test_root_layout_is_layered_left_to_right(self):
        spec = self.spec()
        self.assertEqual(spec["id"], "root")
        self.assertEqual(spec["layoutOptions"]["elk.algorithm"], "layered")
        self.assertEqual(spec["layoutOptions"]["elk.direction"], "RIGHT")

    def test_subsystem_containers_are_sorted_and_labeled(self):
        children = self.spec()["children"]
        self.assertEqual([c["id"] for c in children], [_sub_id("SubA"), _sub_id("SubB")])
        self.assertEqual(children[0]["labels"][0]["text"], "SubA")

    def test_devices_nest_in_their_subsystem_with_ports(self):
        suba = self.spec()["children"][0]
        dev_ids = [d["id"] for d in suba["children"]]
        self.assertEqual(dev_ids, [_dev_id(self.sys.devices[0]), _dev_id(self.sys.devices[1])])
        deva = suba["children"][0]
        self.assertEqual([p["id"] for p in deva["ports"]], [_port_id_from_key(("SubA", "DevA > eth0"))])

    def test_components_nest_inside_their_device_with_their_ports(self):
        deva = self.spec()["children"][0]["children"][0]
        comps = deva["children"]
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0]["labels"][0]["text"], "proc")
        self.assertEqual([p["id"] for p in comps[0]["ports"]], [_port_id_from_key(("SubA", "DevA > proc > udp:1"))])

    def test_all_port_ids_recurses_into_components(self):
        ids = _all_port_ids(self.spec()["children"])
        self.assertIn(_port_id_from_key(("SubA", "DevA > proc > udp:1")), ids)
        self.assertIn(_port_id_from_key(("SubB", "DevB > srv > tcp:2")), ids)

    def test_edge_connects_port_ids_and_carries_label(self):
        spec = self.spec([RenderEdge(_edge())])
        self.assertEqual(len(spec["edges"]), 1)
        e = spec["edges"][0]
        self.assertEqual(e["sources"], [_port_id_from_key(("SubA", "DevA > eth0"))])
        self.assertEqual(e["targets"], [_port_id_from_key(("SubB", "DevB > eth0"))])
        self.assertEqual(e["labels"][0]["text"], "MAVLink")

    def test_empty_payload_means_no_edge_label(self):
        spec = self.spec([RenderEdge(_edge(payload=""))])
        self.assertEqual(spec["edges"][0]["labels"], [])

    def test_stub_endpoint_becomes_root_level_text_node(self):
        key = ("Ghost", "Dev > if")
        spec = self.spec([RenderEdge(_edge(dst=key), stub_key=key)])
        stubs = [c for c in spec["children"] if c["id"].startswith("stub__")]
        self.assertEqual(len(stubs), 1)
        self.assertEqual(stubs[0]["labels"][0]["text"], _stub_label(key))
        self.assertEqual(spec["edges"][0]["targets"], [_stub_id(key)])

    def test_shared_stub_endpoint_is_deduplicated(self):
        key = ("Ghost", "Dev > if")
        redges = [
            RenderEdge(_edge(dst=key), stub_key=key),
            RenderEdge(_edge(src=("SubB", "DevB > eth0"), dst=key), stub_key=key),
        ]
        spec = self.spec(redges)
        stubs = [c for c in spec["children"] if c["id"].startswith("stub__")]
        self.assertEqual(len(stubs), 1)
        self.assertEqual(len(spec["edges"]), 2)

    def test_edge_to_unrendered_port_is_dropped(self):
        # dst port isn't in this System and isn't stubbed → edge must not emit.
        spec = self.spec([RenderEdge(_edge(dst=("SubB", "DevB > nope")))])
        self.assertEqual(spec["edges"], [])


if __name__ == "__main__":
    unittest.main()
