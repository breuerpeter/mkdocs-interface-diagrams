from __future__ import annotations

import re
import sys
from pathlib import Path

from interface_diagrams.model import (
    InterfaceRef,
    Flow,
    Interface,
    Component,
    Device,
    System,
    _heading_for,
)

FLOW_ITEM_RE = re.compile(r"^\d+\.\s*\[\[(?P<link>[^\]]+)\]\]\s*$")
# A flow's bold label under a payload-base heading — the whole (stripped) line is
# `**<label>**`. Distinguishes one flow from its base-mates (e.g. several MAVLink
# flows under one `###### MAVLink`). Mid-sentence bold in prose won't match.
BOLD_RE = re.compile(r"^\*\*(?P<label>.+?)\*\*$")

# Interface docs are named "<subsystem title>.md" — the file stem IS the
# subsystem title (used for matching, labels, colors, and cross-doc references).


def parse_wikilink(link: str, default_subsystem: str) -> InterfaceRef:
    if "#" in link:
        sub, heading = link.split("#", 1)
        sub, heading = sub.strip(), heading.strip()
    else:
        sub, heading = "", link.strip()
    return InterfaceRef(subsystem=sub or default_subsystem, heading=heading)


def parse_subsystem(path: Path) -> tuple[list[Device], list[Flow], str]:
    subsystem = path.stem
    display_name = subsystem
    devices: list[Device] = []
    flows: list[Flow] = []
    cur_device: Device | None = None
    cur_section: str | None = None
    cur_component: Component | None = None
    cur_interface: Interface | None = None  # device interface
    cur_comp_interface: Interface | None = None  # component interface
    # A payload-base section (`##### <base>` under a device interface, or
    # `###### <base>` under a component interface) groups every flow of one
    # payload. cur_payload is that base; cur_source is the enclosing interface
    # (the flows' implicit first waypoint). Individual flows under it are bold
    # labels; cur_flow is the one currently accumulating waypoints.
    cur_payload: str | None = None
    cur_source: tuple[str, str] | None = None
    cur_flow: Flow | None = None

    def end_payload():
        nonlocal cur_payload, cur_source, cur_flow
        cur_payload = cur_source = cur_flow = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()

        if line.startswith("# ") and not line.startswith("## ") and display_name == subsystem:
            display_name = line[2:].strip()
            continue

        if line.startswith("## ") and not line.startswith("###"):
            cur_device = Device(name=line[3:].strip(), subsystem=subsystem)
            devices.append(cur_device)
            cur_section = cur_component = cur_interface = None
            cur_comp_interface = None
            end_payload()
            continue

        if line.startswith("### ") and not line.startswith("#### "):
            head = line[4:].strip()
            cur_section = head if head in ("Interfaces", "Components") else None
            cur_component = cur_interface = cur_comp_interface = None
            end_payload()
            continue

        if line.startswith("#### ") and not line.startswith("##### "):
            head = line[5:].strip()
            end_payload()
            if cur_section == "Interfaces" and cur_device:
                cur_interface = Interface(name=head)
                cur_device.interfaces.append(cur_interface)
                cur_component = cur_comp_interface = None
            elif cur_section == "Components" and cur_device:
                cur_component = Component(name=head)
                cur_device.components.append(cur_component)
                cur_interface = cur_comp_interface = None
            continue

        if line.startswith("##### ") and not line.startswith("###### "):
            head = line[6:].strip()
            end_payload()
            if cur_section == "Interfaces" and cur_interface is not None:
                # payload-base section for a device interface
                cur_payload = head
                cur_source = (subsystem, _heading_for(cur_device, None, cur_interface))
            elif cur_section == "Components" and cur_component is not None:
                cur_comp_interface = Interface(name=head)
                cur_component.interfaces.append(cur_comp_interface)
            continue

        if line.startswith("###### "):
            head = line[7:].strip()
            end_payload()
            if cur_section == "Components" and cur_comp_interface is not None:
                # payload-base section for a component interface
                cur_payload = head
                cur_source = (subsystem, _heading_for(cur_device, cur_component, cur_comp_interface))
            continue

        if cur_payload is not None and cur_source is not None:
            m = BOLD_RE.match(line.strip())
            if m:
                # A new flow: its bold label distinguishes it from base-mates.
                cur_flow = Flow(
                    payload=cur_payload, subsystem=subsystem, source=cur_source, label=m.group("label").strip()
                )
                flows.append(cur_flow)
                continue
            m = FLOW_ITEM_RE.match(line)
            if m:
                if cur_flow is None:
                    # Numbered list with no preceding bold label — malformed
                    # (every flow needs a `**label**`). Keep it as a label-less
                    # flow rather than dropping it silently; derive_edges flags it.
                    cur_flow = Flow(payload=cur_payload, subsystem=subsystem, source=cur_source, label=None)
                    flows.append(cur_flow)
                cur_flow.waypoints.append(parse_wikilink(m.group("link"), subsystem))
                continue

    return devices, flows, display_name


def parse_closure(doc_paths: list[Path]) -> tuple[System, list[Flow], dict[str, Path], set[str]]:
    """Parse the given interface docs plus every doc reachable through their
    cross-subsystem flow waypoints, resolving referenced docs by filename
    ("<title>.md") in the given docs' directories.

    Returns (system, flows, title → doc path for everything parsed, titles
    referenced but unresolvable). Unresolvable references later render as text
    stubs.
    """
    dirs: list[Path] = []
    for p in doc_paths:
        if p.parent not in dirs:
            dirs.append(p.parent)

    pending: dict[str, Path] = {p.stem: p for p in doc_paths}
    parsed: dict[str, Path] = {}
    missing: set[str] = set()
    sys_ = System(name="view")
    all_flows: list[Flow] = []

    while pending:
        title, path = pending.popitem()
        parsed[title] = path
        devices, flows, display_name = parse_subsystem(path)
        sys_.display_names[title] = display_name
        sys_.devices.extend(devices)
        all_flows.extend(flows)
        for f in flows:
            for ref in f.waypoints:
                ref_sub = ref.subsystem
                if ref_sub in parsed or ref_sub in pending or ref_sub in missing:
                    continue
                cand = next((d / f"{ref_sub}.md" for d in dirs if (d / f"{ref_sub}.md").is_file()), None)
                if cand is not None:
                    pending[ref_sub] = cand
                else:
                    missing.add(ref_sub)
                    print(
                        f"warning: no doc found for referenced subsystem "
                        f"'{ref_sub}' (looked for '{ref_sub}.md') — its "
                        f"endpoints will render as stubs",
                        file=sys.stderr,
                    )
    return sys_, all_flows, parsed, missing
