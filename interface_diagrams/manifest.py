"""Discover the subsystem interface docs in a system folder.

The diagram tool runs on a folder (e.g. docs/drone-system): every `<name>.md`
in it is a subsystem interface doc, except `index.md` — the landing page that
hosts the system-level diagram. The folder membership IS the subsystem list, so
there is no manifest file.

The system's display name is declared in the landing page's YAML frontmatter
(`system: <Name>`) — so adding a new system diagram is just a new folder with an
`index.md` carrying that key (e.g. `system: Simulation System`). It is required.
"""

from __future__ import annotations

import re
from pathlib import Path

_SYSTEM_FM = re.compile(r"^\s*system\s*:\s*(.+?)\s*$")


def landing_system_name(path: Path) -> str | None:
    """The `system:` name from a doc's YAML frontmatter, or None. A page
    carrying this key is the system landing/overview page."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    for line in text[3:end].splitlines():
        m = _SYSTEM_FM.match(line)
        if m:
            return m.group(1).strip().strip("\"'")
    return None


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
