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
import itertools
import json
import os
import queue
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from interface_diagrams import embed, manifest
from interface_diagrams import workers as _workers

from interface_diagrams.model import (
    InterfaceRef,
    Flow,
    Interface,
    Component,
    Device,
    System,
    Edge,
    RenderEdge,
    _fmt_key,
    _heading_for,
)


from interface_diagrams.parse import FLOW_ITEM_RE, BOLD_RE, parse_wikilink, parse_subsystem, parse_closure


from interface_diagrams.edges import (
    _warn,
    _soft_warn,
    merge_labels,
    _flow_name,
    _flow_stem,
    _aggregate_stem,
    _payload_tokens,
    _resolve_chain,
    trace_flows,
    single_flow_edges,
    derive_edges,
    aggregate_diagrams,
    drawable_flow_stems,
    port_keys_for,
    classify_edges,
)




from interface_diagrams.views import chain_view, filter_system, collapse_system


from interface_diagrams.elk import (
    SUBSYSTEM_PALETTE_BRIGHT,
    SUBSYSTEM_PALETTE_DARK,
    COMPONENT_PALETTE,
    _slug,
    _sub_id,
    _dev_id,
    _comp_id,
    _port_id_from_key,
    _stub_id,
    _stub_label,
    _stub_keys,
    _FONT_PATH,
    _FONT_CACHE,
    _FONT_LOCK,
    _font,
    _estimate_text_width,
    _glyph_advance,
    _font_height,
    PORT_LABEL_OFFSET,
    EDGE_LABEL_GAP,
    EDGE_LABEL_END_CLEAR,
    STUB_ARROW_GAP,
    PORT_SPACING_OPTS,
    emit_elk_spec,
    _port_node,
    _all_port_ids,
    WRAP_OVERSHOOT_PX,
    _abs_positions,
    _wrapped_back_edges,
    layout,
    run_elk,
)

from interface_diagrams.excalidraw import (
    EXCALIDRAW_FONT,
    _next_seed,
    _base,
    _rect,
    _normalize_luminance,
    _device_fill,
    _component_fill,
    _text,
    _arrow,
    _trim_polyline,
    subsystem_color,
    device_fill_color,
    component_fill_color,
    build_excalidraw,
)


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




def render_svg(elements: list[dict]) -> str:
    """Render Excalidraw elements to a self-contained SVG via the render worker."""
    resp = _RENDER_POOL.call({"elements": elements})
    if not resp.get("ok"):
        raise RuntimeError(f"render_svg.mjs failed: {resp.get('error', '').strip()}")
    return resp["svg"]



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


def generate_section(section: Path, out: Path, check: bool = False) -> int:
    """Generate (or, with check=True, only validate) one system folder. Returns exit code."""
    import interface_diagrams.edges as _edges_mod
    _edges_mod._VALIDATION_WARNINGS = 0
    _edges_mod._VALIDATION_SOFT_WARNINGS = 0
    if not section.is_dir():
        print(f"error: {section} is not a folder", file=sys.stderr)
        return 2

    system_name, doc_paths = manifest.parse_section(section)
    if not doc_paths:
        print(f"error: no subsystem docs found in {section}", file=sys.stderr)
        return 2
    overview = section / "index.md"
    if not check and not overview.is_file():
        print(
            f"error: {overview} not found — the section needs an index.md landing page for the system diagram",
            file=sys.stderr,
        )
        return 2

    full, flows, parsed_docs, unresolved = parse_closure(doc_paths)
    all_subs = sorted({d.subsystem for d in full.devices})

    if check:
        derive_edges(flows, full)
        if _edges_mod._VALIDATION_WARNINGS:
            print(
                f"check failed: {_edges_mod._VALIDATION_WARNINGS} issue(s) across {len(parsed_docs)} parsed doc(s)",
                file=sys.stderr,
            )
            return 1
        advisory = (
            f" ({_edges_mod._VALIDATION_SOFT_WARNINGS} advisory warning(s))"
            if _edges_mod._VALIDATION_SOFT_WARNINGS
            else ""
        )
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
    out_dir: Path = out
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
    import interface_diagrams.elk as _elk_mod
    _elk_mod._ELK_POOL = _ELK_POOL

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
    return generate_section(args.section, args.out, args.check)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
