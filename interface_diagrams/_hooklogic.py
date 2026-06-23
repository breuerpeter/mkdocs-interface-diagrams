"""MkDocs build hook for interface-diagram docs.

The interface-diagrams tool (tools/diagrams) renders one SVG per
subsystem/device/component/interface/flow into docs/assets/diagrams/<section>/.
Each SVG's filename is the slug of its qualifier chain (see embed.qualified_name)
— e.g. 'skynode-fmu_nuttx-uart_dev_ttys4.svg'. The docs themselves carry NO
generated markup: this hook derives, purely from each doc's heading structure
plus that filename convention, where every diagram belongs. Then per page it:

  * inlines the system overview diagram on the landing page and rewrites its
    internal links to the sections they target;
  * makes every other diagram's title (a heading or `**flow label**`) the link
    that opens it in the lightbox — collapsing the title onto the link — and
    rewrites each interface (port) label's href to its interface heading;
  * turns each `[[...]]` flow waypoint into a link to the referenced
    interface's heading.

The placement derivation here is the mirror of the tool's diagram naming
(generate.py builds the same slugs from its parsed model); test_derivation.py
guards the two against drift.
"""

import functools
import html
import os
import posixpath
import re
from pathlib import Path

from mkdocs.utils import get_relative_url as _get_relative_url

# The diagram tool owns the slug contract (qualified_name) and the system
# diagram's name (manifest.SYSTEM_NAME). Import them so placement derivation and
# rendering use the exact same naming the generator does — one source of truth.
from interface_diagrams import manifest
from interface_diagrams.embed import qualified_name

_WIKILINK = re.compile(r"(?<!!)\[\[([^\]]+?)\]\]")
_SVG_HREF = re.compile(r'(href|xlink:href)="([^"/]+?\.svg)"')
# Interface (port) labels carry a doc-relative href ('<Doc>.md#Device > Iface')
# instead of a '.svg' detail target; rewritten to the interface heading's anchor
# below. '>' etc. may arrive HTML-escaped from the SVG serializer (html.unescape).
_SVG_DOC_HREF = re.compile(r'(href|xlink:href)="([^"#]+?\.md)#([^"]+?)"')
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_BOLD = re.compile(r"^\*\*(?P<label>.+?)\*\*$")

# The system overview slug is derived per-section from the landing page's
# `system:` frontmatter (see _system_slug); inlined on the landing page (index.md).
_LANDING_STEM = "index"

# Caches (built once): diagram stem -> (doc, anchor|None);
#                      (doc, "A > B[ > C]") -> anchor;
#                      doc stem -> docs-relative path (docs may live in a
#                      subfolder like drone-system/, so references by name
#                      resolve through this); docs-relative path -> the doc's H1
#                      title (the subsystem label shown in waypoints, not its
#                      filename); docs-relative path of the system diagram;
#                      section -> set of existing diagram stems.
_DIAGRAMS = None
_PATHS = None
_DOCS = None
_TITLES = None
_SYSTEM_SVG = {}  # section -> docs-relative path of its system svg
_SECTION_STEMS = None


def _doc_path(name: str, section: str = "") -> str:
    """Resolve a referenced doc — a subsystem name or '<name>.md' — to its real
    docs-relative path, **preferring the given section**: different systems can
    share a stem (drone-system/autopilot.md vs simulation-system/autopilot.md),
    so an intra-system reference must resolve within its own system."""
    stem = name[:-3] if name.endswith(".md") else name
    docs = _DOCS or {}
    if (section, stem) in docs:
        return docs[(section, stem)]
    for (_sec, _st), path in docs.items():  # fallback: any section
        if _st == stem:
            return path
    return f"{stem}.md"


def _heading_alias(text: str) -> str:
    m = re.match(r"\[\[([^\]]+)\]\]\s*$", text.strip())
    if m:
        return m.group(1).split("|", 1)[-1].strip()
    return text.strip()


def _norm_path(p: str) -> str:
    return re.sub(r"\s*>\s*", " > ", p).strip()


def _section_of(doc: str) -> str:
    """The section folder of a docs-relative doc path: its first path component
    (e.g. 'drone-system/skynode.md' -> 'drone-system'). Diagrams for a section
    live at 'assets/diagrams/<section>/', matching the generator's output."""
    head = doc.split("/", 1)[0]
    return head if "/" in doc else ""


def _svg_rel(doc: str, stem: str) -> str:
    """Docs-relative path of a diagram SVG for the given doc's section."""
    return f"assets/diagrams/{_section_of(doc)}/{stem}.svg"


def _system_slug(docs_dir: str, doc: str):
    """System-overview diagram slug for a doc's section, from the section's
    index.md `system:` frontmatter — or None if the section declares no system."""
    section = _section_of(doc)
    idx = Path(docs_dir, section, "index.md") if section else Path(docs_dir, "index.md")
    name = manifest.landing_system_name(idx)
    return qualified_name(name) if name else None


def _section_stems(docs_dir: str, doc: str) -> set:
    """Set of diagram stems that actually exist for a doc's section (one
    listdir per section, cached). A heading/label only becomes a diagram link
    when its derived slug is in this set — so devices/interfaces the generator
    skipped (no flows touch them) stay plain text."""
    section = _section_of(doc)
    cache = _SECTION_STEMS if _SECTION_STEMS is not None else {}
    if section not in cache:
        d = os.path.join(docs_dir, "assets", "diagrams", section)
        try:
            cache[section] = {f[:-4] for f in os.listdir(d) if f.endswith(".svg")}
        except FileNotFoundError:
            cache[section] = set()
    return cache[section]


def _walk(lines, doc_stem):
    """Walk one doc's lines, yielding a record per heading and per flow-label.

    This mirrors the generator's doc model (parse_subsystem) and its diagram
    naming, so the slugs yielded here are exactly the SVG filenames it writes.

    Yields:
      ('heading', idx, level, text, disp, slug|None, paths)
          idx    line index; level the heading level; text the raw heading text
          (used for the collapsed link and the diagram slug, matching the
          generator's use of raw heading text for device/interface names); disp
          the alias-resolved text (used for anchors/waypoint paths); slug the
          diagram for this heading (None if the heading owns no diagram); paths
          the interface-path spellings this heading anchors (for waypoints).
      ('flow', idx, label, slug)
          a `**bold label**` under a payload-base section; slug its path diagram.
    """
    sub = doc_stem
    device = section = component = dev_iface = comp_iface = None
    payload = source_path = None
    for i, raw in enumerate(lines):
        line = raw.rstrip("\n")
        hm = _HEADING.match(line)
        if hm:
            level = len(hm.group(1))
            text = hm.group(2).strip()
            disp = _heading_alias(hm.group(2))
            payload = source_path = None
            slug, paths = None, []
            if level == 1:
                slug = qualified_name(sub)
            elif level == 2:
                device, section, component, dev_iface, comp_iface = text, None, None, None, None
                slug = qualified_name(sub, text)
                paths = [disp]
            elif level == 3:
                section, component, dev_iface, comp_iface = text, None, None, None
            elif level == 4:
                if section and section.lower().startswith("component"):
                    component, comp_iface = text, None
                    slug = qualified_name(sub, device, text)
                    paths = [f"{device} > {disp}"]
                elif section and section.lower().startswith("interface"):
                    dev_iface = text
                    slug = qualified_name(sub, device, text)
                    paths = [disp, f"{device} > {disp}"]
            elif level == 5:
                if section and section.lower().startswith("component"):
                    comp_iface = text
                    slug = qualified_name(sub, device, component, text)
                    paths = [disp, f"{component} > {disp}", f"{device} > {component} > {disp}"]
                else:  # payload base, device iface
                    payload, source_path = text, f"{device} > {dev_iface}"
            elif level == 6:  # payload base, component iface
                payload, source_path = text, f"{device} > {component} > {comp_iface}"
            yield ("heading", i, level, text, disp, slug, paths)
            continue
        bm = _BOLD.match(line.strip())
        if bm is not None and payload is not None:
            label = bm.group("label").strip()
            slug = qualified_name(sub, *source_path.split(" > "), payload, label)
            yield ("flow", i, label, slug)


def _anchor(disp, used):
    """MkDocs/Python-Markdown slug for a heading, with its `_N` de-duplication."""
    from markdown.extensions.toc import slugify

    slug = slugify(disp, "-")
    anchor, n = slug, 1
    while anchor in used:
        anchor = f"{slug}_{n}"
        n += 1
    used.add(anchor)
    return anchor


def _scan(docs_dir: str):
    """One ordered pass per doc: compute each heading's exact anchor and, from
    the heading structure + filename convention, where each diagram belongs."""
    global _DIAGRAMS, _PATHS, _DOCS, _TITLES, _SYSTEM_SVG, _SECTION_STEMS
    if _DIAGRAMS is not None:
        return _DIAGRAMS, _PATHS
    _DIAGRAMS, _PATHS, _DOCS, _TITLES, _SECTION_STEMS, _SYSTEM_SVG = {}, {}, {}, {}, {}, {}
    for root, _dirs, fnames in os.walk(docs_dir):
        for fn in sorted(fnames):
            if not fn.endswith(".md"):
                continue
            doc = os.path.relpath(os.path.join(root, fn), docs_dir).replace(os.sep, "/")
            doc_stem = os.path.splitext(fn)[0]
            _DOCS.setdefault((_section_of(doc), doc_stem), doc)
            stems = _section_stems(docs_dir, doc)
            with open(os.path.join(root, fn), encoding="utf-8") as fh:
                lines = fh.read().split("\n")
            used = set()
            for rec in _walk(lines, doc_stem):
                if rec[0] == "heading":
                    _, _i, _level, _text, disp, slug, paths = rec
                    anchor = _anchor(disp, used)
                    if _level == 1:
                        _TITLES.setdefault(doc, disp)  # subsystem label
                    for p in paths:
                        _PATHS[(doc, p)] = anchor
                    if slug and slug in stems:
                        _DIAGRAMS.setdefault((_section_of(doc), slug), (doc, anchor))
                else:  # flow
                    _, _i, _label, slug = rec
                    if slug in stems:
                        # Flows live under a bold label, not a heading, so they
                        # have no precise anchor of their own; a payload-token
                        # link opens the flow's diagram in the lightbox without
                        # navigating, so an unplaced (doc, None) entry suffices.
                        _DIAGRAMS.setdefault((_section_of(doc), slug), (doc, None))
            # The system overview is inlined on the landing page; it owns no
            # heading, so record it explicitly and remember its path so pages can
            # link "home" to it (diagram-lightbox.js).
            if doc_stem == _LANDING_STEM:
                sys_slug = _system_slug(docs_dir, doc)
                if sys_slug and sys_slug in stems:
                    _DIAGRAMS.setdefault((_section_of(doc), sys_slug), (doc, None))
                    _SYSTEM_SVG[_section_of(doc)] = _svg_rel(doc, sys_slug)
    # Every rendered SVG must have been placed by the derivation above; one that
    # wasn't means its filename no longer matches any heading/flow-label — i.e.
    # the generator's naming and this hook's derivation have drifted. Fail the
    # build loudly rather than silently dropping a diagram from the docs.
    # Aggregate diagrams ('multiflow-…', generate.py:_aggregate_stem) are the one
    # exception: synthetic, referenced only from multi-flow edge labels, so they
    # map to no heading/flow and are exempt.
    placed = set(_DIAGRAMS)
    orphans = sorted(
        f"{sec}/{s}"
        for sec, stems in _SECTION_STEMS.items()
        for s in stems
        if (sec, s) not in placed and not s.startswith("multiflow-")
    )
    if orphans:
        raise RuntimeError(
            "diagram(s) rendered but not placeable from any heading/flow-label "
            f"(generator/hook naming drift): {', '.join(orphans)}"
        )
    return _DIAGRAMS, _PATHS


def _read_svg(docs_dir: str, relpath: str):
    path = os.path.join(docs_dir, relpath)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        svg = fh.read()
    svg = re.sub(r"\n\s*\n", "\n", svg).strip()
    return re.sub(r"<svg ", '<svg style="max-width:100%;height:auto" ', svg, count=1)


# Captured on each page pass so on_post_build (which gets no `files`) can rewrite
# the standalone diagram SVGs after they're copied into the site.
_FILES = None


def _fix_standalone_svg(svg: str, svg_url: str, doc_urls: dict, paths: dict, diagrams: dict, section: str = "") -> str:
    """Rewrite a raw diagram SVG's links so they work when the .svg is opened
    directly (the "Open diagram" target), not just when inlined — all relative to
    the SVG's own served location (`svg_url`):

      * '<Doc>.md#<path>' interface (port) hrefs -> the interface heading's anchor
        (else the doc page);
      * '<stem>.svg' title hrefs (device/component/subsystem boxes) -> the section
        where that diagram is embedded, so a title click navigates there and
        opens it like an interface link. A title whose diagram isn't placed
        anywhere (e.g. a cross-referenced subsystem not in the data-flows folder)
        has no section, so it's left as the .svg and the lightbox opens it
        directly."""

    def _section_url(loc):
        doc, anchor = loc
        target = doc_urls.get(doc)
        if not target:
            return None
        u = _get_relative_url(target, svg_url)
        return u + (f"#{anchor}" if anchor else "")

    def iface(hm):
        attr, doc = hm.group(1), _doc_path(hm.group(2), section)
        path = _norm_path(html.unescape(hm.group(3)))
        u = _section_url((doc, paths.get((doc, path)))) if doc in doc_urls else None
        return f'{attr}="{u}"' if u else hm.group(0)

    def title(hm):
        attr, name = hm.group(1), hm.group(2)
        loc = diagrams.get((section, name[:-4]))  # (section, stem) -> (doc, anchor)
        # A placed title -> its heading anchor. A payload-token target (a flow or
        # aggregate diagram, indexed with no anchor / unplaced) is left as the
        # .svg so the lightbox opens it in place WITHOUT navigating the page.
        u = _section_url(loc) if (loc and loc[1] is not None) else None
        return f'{attr}="{u}"' if u else hm.group(0)

    svg = _SVG_DOC_HREF.sub(iface, svg)
    svg = _SVG_HREF.sub(title, svg)
    return svg


def fix_built_svgs(config):
    """After the build, rewrite the interface hrefs inside every standalone
    diagram SVG copied into the site, so links work when the raw .svg is opened
    via "Open diagram" (the hook only rewrites SVGs it inlines into a page)."""
    if _FILES is None:
        return
    _diagrams, paths = _scan(config["docs_dir"])
    doc_urls = {f.src_path.replace(os.sep, "/"): f.url for f in _FILES if f.src_path.endswith(".md")}
    for f in _FILES:
        if not f.src_path.endswith(".svg"):
            continue
        dest = f.abs_dest_path
        if not os.path.isfile(dest):
            continue
        svg = open(dest, encoding="utf-8").read()
        parts = f.src_path.replace(os.sep, "/").split("/")
        sec = parts[2] if parts[:2] == ["assets", "diagrams"] and len(parts) > 3 else ""
        fixed = _fix_standalone_svg(svg, f.url, doc_urls, paths, _diagrams, sec)
        if fixed != svg:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(fixed)


def apply_page_markdown(markdown, page, config, files):
    global _FILES
    _FILES = files
    docs_dir = config["docs_dir"]
    diagrams, paths = _scan(docs_dir)
    here = page.file.src_path.replace(os.sep, "/")
    os.path.dirname(here)
    here_stem = os.path.splitext(os.path.basename(here))[0]
    stems = _section_stems(docs_dir, here)

    def url_to(doc, anchor=None):
        f = files.get_file_from_path(doc)
        if not f:
            return None
        u = _get_relative_url(f.url, page.file.url)
        return u + (f"#{anchor}" if anchor else "")

    def svg_url(stem):
        f = files.get_file_from_path(_svg_rel(here, stem))
        return _get_relative_url(f.url, page.file.url) if f else None

    def diagram_link(stem, inner):
        """Wrap a title (heading text or `**flow label**`) in the lightbox link
        that opens its diagram."""
        u = svg_url(stem)
        if not u:
            return inner
        return f'<a class="diagram-link" href="{u}" target="_blank" rel="noopener" title="Open diagram">{inner}</a>'

    def inline_system():
        """The system overview SVG, inlined with its internal links rewritten to
        the sections/headings they target (the only inlined diagram)."""
        rel = _svg_rel(here, _system_slug(docs_dir, here))
        svg = _read_svg(docs_dir, rel)
        if svg is None:
            return f"\n\n*missing diagram: {rel}*\n\n"
        folder = os.path.dirname(rel)

        def fix_href(hm):
            attr, name = hm.group(1), hm.group(2)
            hstem = name[:-4]
            loc = diagrams.get((_section_of(here), hstem))
            # A placed title -> its heading anchor; a payload-token target (flow
            # or aggregate, anchor None) falls through to its .svg asset so the
            # lightbox opens it in place without navigating.
            if loc and loc[1] is not None:
                u = url_to(*loc)
                if u:
                    return f'{attr}="{u}"'
            u = url_to(_doc_path(hstem, _section_of(here)))
            if u:
                return f'{attr}="{u}"'
            f2 = files.get_file_from_path(os.path.join(folder, name))
            if f2:
                return f'{attr}="{_get_relative_url(f2.url, page.file.url)}" target="_blank" rel="noopener"'
            return hm.group(0)

        def fix_iface_href(hm):
            attr, doc, path = (
                hm.group(1),
                _doc_path(hm.group(2), _section_of(here)),
                _norm_path(html.unescape(hm.group(3))),
            )
            anchor = paths.get((doc, path))
            u = url_to(doc, anchor) if anchor else url_to(doc)
            return f'{attr}="{u}"' if u else hm.group(0)

        svg = _SVG_HREF.sub(fix_href, svg)
        svg = _SVG_DOC_HREF.sub(fix_iface_href, svg)
        return f'\n\n<div class="interface-diagram" markdown="0">\n{svg}\n</div>\n\n'

    # Collapse each diagram-bearing title onto its lightbox link, and inline the
    # system overview on the landing page — all derived from the heading
    # structure, no source markup. Build a per-line action map, then re-emit.
    lines = markdown.split("\n")
    is_landing = here_stem == _LANDING_STEM
    actions = {}  # idx -> ('heading', level, text, stem) | ('flow', label, stem)
    inline_after = None  # idx of the landing H1 to inline the system diagram after
    for rec in _walk(lines, here_stem):
        if rec[0] == "heading":
            _, idx, level, text, _disp, slug, _paths = rec
            if is_landing and level == 1:
                _sys = _system_slug(docs_dir, here)
                if _sys and _sys in stems:
                    inline_after = idx
                continue  # landing H1 stays plain; system diagram inlined below it
            if slug and slug in stems:
                actions[idx] = ("heading", level, text, slug)
        else:
            _, idx, label, slug = rec
            if slug in stems:
                actions[idx] = ("flow", label, slug)

    out = []
    for idx, line in enumerate(lines):
        act = actions.get(idx)
        if act and act[0] == "heading":
            _, level, text, stem = act
            out.append(f"{'#' * level} {diagram_link(stem, text)}")
        elif act and act[0] == "flow":
            _, label, stem = act
            out.append(diagram_link(stem, f"**{label}**"))  # ** renders inside the link
            # The waypoint list sits directly under the label (the managed block
            # that used to separate them is gone); python-markdown won't render a
            # list that abuts the preceding (now inline-HTML) line, so re-insert
            # the blank line it needs.
            if idx + 1 < len(lines) and lines[idx + 1].strip():
                out.append("")
        else:
            out.append(line)
        if idx == inline_after:
            out.append(inline_system())
    markdown = "\n".join(out)

    def sub_wikilink(m):
        target = m.group(1).split("|", 1)[0]
        alias = m.group(1).split("|", 1)[1] if "|" in m.group(1) else None
        docpart, _, path = target.partition("#")
        docpart, path = docpart.strip(), _norm_path(path)
        doc = _doc_path(docpart, _section_of(here)) if docpart else here
        # Show the subsystem in the waypoint label: the referenced doc's H1 title
        # (the cross-doc waypoint's subsystem) or the current page's (same-doc).
        # Fall back to the file stem when a doc has no recorded H1.
        subsystem = (_TITLES or {}).get(doc) or docpart or here_stem
        disp = alias or (f"{subsystem} > {path}" if path else subsystem)
        anchor = paths.get((doc, path)) if path else None
        u = url_to(doc, anchor)
        if u and (anchor or not path):  # resolved to a section (or a page)
            return f'<a href="{u}">{disp}</a>'
        return f"`{disp}`"  # unresolved -> plain code

    markdown = _WIKILINK.sub(sub_wikilink, markdown)

    # Expose a page-relative URL to the system diagram so the lightbox's "home"
    # button can jump back to it from any page (see diagram-lightbox.js).
    home_svg = _SYSTEM_SVG.get(_section_of(here))
    if home_svg:
        sf = files.get_file_from_path(home_svg)
        if sf:
            rel = html.escape(_get_relative_url(sf.url, page.file.url), quote=True)
            markdown += f'\n\n<div class="system-diagram-link" data-url="{rel}" hidden></div>\n'
    return markdown
