from __future__ import annotations

import hashlib
import itertools
import sys

from interface_diagrams import embed
from interface_diagrams.model import (
    Flow,
    System,
    Edge,
    RenderEdge,
    _fmt_key,
    _heading_for,
)

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
