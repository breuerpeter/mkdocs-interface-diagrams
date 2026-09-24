"""Discover the subsystem interface docs in a system folder.

The diagram tool runs on a folder (e.g. docs/drone-system): every `<name>.md`
in it is a subsystem interface doc, except `index.md` — the landing page that
hosts the system-level diagram. The folder membership IS the subsystem list, so
there is no manifest file.

The system's display name is declared in the landing page's YAML frontmatter
(`system: <Name>`) — so adding a new system diagram is just a new folder with an
`index.md` carrying that key (e.g. `system: Simulation System`). It is required.
The same frontmatter may declare the section's targets (`targets: [x_1, x_2]`),
the names a flow's target tag may match; each target gets its own view.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _frontmatter(path: Path) -> dict:
    """A doc's YAML frontmatter, read as YAML like mkdocs reads it; {} when the
    doc has none or it is not a YAML mapping."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def landing_system_name(path: Path) -> str | None:
    """The `system:` name from a doc's YAML frontmatter, or None. A page
    carrying this key is the system landing/overview page."""
    name = _frontmatter(path).get("system")
    return str(name).strip() if name is not None else None


def landing_targets(path: Path) -> list[str]:
    """The `targets:` names from a landing page's frontmatter, a YAML list such
    as `targets: [x_1, x_2]`; [] when the page declares none."""
    targets = _frontmatter(path).get("targets") or []
    return [str(t) for t in targets] if isinstance(targets, list) else [str(targets)]


def system_name(folder: Path) -> str:
    """Display name from the system folder's `index.md` `system:` frontmatter."""
    name = landing_system_name(folder / "index.md")
    if name is None:
        raise ValueError(f"{folder}/index.md must declare the system name in frontmatter:\n---\nsystem: <Name>\n---")
    return name


def parse_section(folder: Path) -> tuple[str, list[Path]]:
    """Return (system_name, [subsystem doc Path, ...]) for a system folder."""
    docs = sorted(p for p in folder.glob("*.md") if p.stem.lower() != "index")
    return system_name(folder), docs
