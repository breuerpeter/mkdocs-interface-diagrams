"""Diagram file naming — the slug contract shared by the generator (which names
the SVGs) and the MkDocs hook (which derives each diagram's placement from the
same names). One source of truth for the slug format keeps the two in step."""

from __future__ import annotations

import re

# Each qualifier becomes a lowercase filename slug: runs of anything outside
# [a-z0-9] (spaces, parentheses, ':', '/', '.', …) collapse to one underscore
# (interface names like "unix:/tmp/x.sock" reach the path-diagram filenames).
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def _sanitize(name: str) -> str:
    return _NON_SLUG.sub("_", name.lower()).strip("_") or "_"


def qualified_name(subsystem: str, *parts: str | None) -> str:
    """Collision-safe stem joining a subsystem with further qualifiers, e.g.
    'skynode-amc-mavlink_router' (device/component) or a flow's full source
    path + title (path diagram). Qualifiers are slugged to [a-z0-9_] tokens,
    so the '-' separator keeps their boundaries unambiguous; names are unique
    across the vault because they carry the full qualifier chain."""
    names = [subsystem] + [p for p in parts if p]
    return "-".join(_sanitize(n) for n in names)
