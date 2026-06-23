from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class InterfaceRef:
    subsystem: str
    heading: str


@dataclass
class Flow:
    payload: str
    subsystem: str
    source: tuple[str, str]
    label: str | None = None
    waypoints: list[InterfaceRef] = field(default_factory=list)


@dataclass
class Interface:
    name: str


@dataclass
class Component:
    name: str
    interfaces: list[Interface] = field(default_factory=list)


@dataclass
class Device:
    name: str
    interfaces: list[Interface] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    subsystem: str = ""


@dataclass
class System:
    name: str
    devices: list[Device] = field(default_factory=list)
    display_names: dict = field(default_factory=dict)

    def by_interface(self) -> dict[tuple[str, str], tuple[Device, Component | None, Interface]]:
        out: dict[tuple[str, str], tuple[Device, Component | None, Interface]] = {}
        for d in self.devices:
            for i in d.interfaces:
                out[(d.subsystem, i.name)] = (d, None, i)
                out[(d.subsystem, f"{d.name} > {i.name}")] = (d, None, i)
            for c in d.components:
                for i in c.interfaces:
                    out[(d.subsystem, i.name)] = (d, c, i)
                    out[(d.subsystem, f"{c.name} > {i.name}")] = (d, c, i)
                    out[(d.subsystem, f"{d.name} > {c.name} > {i.name}")] = (d, c, i)
        return out


@dataclass(frozen=True)
class Edge:
    src_key: tuple[str, str]
    dst_key: tuple[str, str]
    direction: str
    payload: str
    tokens: tuple = field(default=(), compare=False)


def _fmt_key(key: tuple[str, str]) -> str:
    return f"{key[0]} > {key[1]}"


def _heading_for(device, component, iface):
    if component is None:
        return f"{device.name} > {iface.name}"
    return f"{device.name} > {component.name} > {iface.name}"


@dataclass
class RenderEdge:
    """An Edge plus how this particular diagram should draw it."""
    edge: Edge
    stub_key: tuple[str, str] | None = None
