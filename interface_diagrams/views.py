from __future__ import annotations

from interface_diagrams.model import (
    System,
    Device,
    Component,
    Edge,
    _heading_for,
)


def chain_view(sys_: System, edges: list[Edge]) -> System:
    """Sub-System containing only the devices/components/interfaces that
    appear as endpoints of the traced edges."""
    keys = {e.src_key for e in edges} | {e.dst_key for e in edges}
    out = System(name=sys_.name, display_names=sys_.display_names)
    for d in sys_.devices:
        new_dev = Device(name=d.name, subsystem=d.subsystem)
        new_dev.interfaces = [i for i in d.interfaces if (d.subsystem, _heading_for(d, None, i)) in keys]
        for c in d.components:
            nc = Component(name=c.name)
            nc.interfaces = [i for i in c.interfaces if (d.subsystem, _heading_for(d, c, i)) in keys]
            if nc.interfaces:
                new_dev.components.append(nc)
        if new_dev.interfaces or new_dev.components:
            out.devices.append(new_dev)
    return out


def filter_system(sys_, subsystems, devices, components) -> System:
    out = System(name=sys_.name, display_names=sys_.display_names)
    for d in sys_.devices:
        if subsystems and d.subsystem not in subsystems:
            continue
        if devices and d.name not in devices:
            continue
        new_d = Device(name=d.name, subsystem=d.subsystem, interfaces=list(d.interfaces))
        for c in d.components:
            if components and c.name not in components:
                continue
            new_d.components.append(c)
        out.devices.append(new_d)
    return out


def collapse_system(sys_: System, collapsed: set[str], edges: list[Edge], focus: set[str] | None = None) -> System:
    """Reduce each collapsed subsystem to its boundary-crossing surface.

    Components drop wholesale (cross-device edges only attach to device
    interfaces, per the spec), device interfaces survive only when at
    least one of their edges crosses to ANOTHER SUBSYSTEM IN THIS VIEW
    (crossings to out-of-view subsystems are second-degree detail and drop
    with their edges), and devices with no surviving interfaces disappear.
    The subsystem boundary itself stays — its label links to the subsystem's
    detail diagram.

    `focus` is the set of expanded subsystem(s) a subsystem-detail diagram is
    built around. When given, a collapsed neighbour keeps only the interfaces
    that cross to the focus; a lateral neighbour↔neighbour link (and any port
    that exists solely for it) is second-degree detail relative to the focus
    and drops. Left None for the focus-less system diagram (all subsystems
    collapsed), which keeps every link between two drawn boxes.
    """
    view_subs = {d.subsystem for d in sys_.devices}
    crossing: set[tuple[str, str]] = set()
    for e in edges:
        a, b = e.src_key[0], e.dst_key[0]
        if a == b or a not in view_subs or b not in view_subs:
            continue
        if focus is not None and a not in focus and b not in focus:
            continue
        crossing.add(e.src_key)
        crossing.add(e.dst_key)

    out = System(name=sys_.name, display_names=sys_.display_names)
    for d in sys_.devices:
        if d.subsystem not in collapsed:
            out.devices.append(d)
            continue
        kept = [i for i in d.interfaces if (d.subsystem, _heading_for(d, None, i)) in crossing]
        if kept:
            out.devices.append(Device(name=d.name, subsystem=d.subsystem, interfaces=kept))
    return out
