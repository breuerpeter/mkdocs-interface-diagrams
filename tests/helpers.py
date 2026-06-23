"""Shared fixtures for the diagram-pipeline tests.

Every test module imports the pipeline the same way (scripts/ on sys.path) and
most need: a small two-subsystem System, silenced+counted validation warnings,
and temp interface docs. Centralised here so fixtures can't drift apart the way
the original Flow(title=...) fixture did."""

from __future__ import annotations

import contextlib
import io
import sys  # noqa: F401
import unittest

import interface_diagrams.generate  # noqa: F401  (ensures import works)
SCRIPTS = None  # retained name; tests that referenced files under SCRIPTS use fixtures now

from interface_diagrams import generate  # noqa: E402
from interface_diagrams.generate import (  # noqa: E402
    Component,
    Device,
    Flow,
    Interface,
    InterfaceRef,
    System,
)


def make_system() -> System:
    """Two subsystems, exercising every modelling shape:

    SubA / DevA:  device interface 'eth0', component 'proc' with 'udp:1'
    SubA / Hub :  two bare device interfaces 'in' and 'out' (no components)
    SubB / DevB:  device interface 'eth0', component 'srv' with 'tcp:2'
    """
    deva = Device(
        name="DevA",
        subsystem="SubA",
        interfaces=[Interface("eth0")],
        components=[Component(name="proc", interfaces=[Interface("udp:1")])],
    )
    hub = Device(name="Hub", subsystem="SubA", interfaces=[Interface("in"), Interface("out")])
    devb = Device(
        name="DevB",
        subsystem="SubB",
        interfaces=[Interface("eth0")],
        components=[Component(name="srv", interfaces=[Interface("tcp:2")])],
    )
    return System(name="Sys", devices=[deva, hub, devb])


def make_flow(
    payload="MAVLink",
    label="telemetry",
    source=("SubA", "DevA > proc > udp:1"),
    waypoints=(("SubA", "DevA > eth0"), ("SubB", "DevB > eth0"), ("SubB", "DevB > srv > tcp:2")),
) -> Flow:
    """proc(udp:1) -> DevA(eth0) -> DevB(eth0) -> srv(tcp:2) by default."""
    return Flow(
        payload=payload,
        subsystem=source[0],
        label=label,
        source=source,
        waypoints=[InterfaceRef(s, h) for s, h in waypoints],
    )


class PipelineTestCase(unittest.TestCase):
    """Resets the module-level validation counters (a --check artifact shared
    across the process) and silences warning prints, so tests can assert on
    warning DELTAS without polluting each other or the test output."""

    def setUp(self):
        generate._VALIDATION_WARNINGS = 0
        generate._VALIDATION_SOFT_WARNINGS = 0
        self._stderr = contextlib.redirect_stderr(io.StringIO())
        self._stderr.__enter__()
        self.addCleanup(self._stderr.__exit__, None, None, None)

    @property
    def warnings(self) -> int:
        return generate._VALIDATION_WARNINGS

    @property
    def soft_warnings(self) -> int:
        return generate._VALIDATION_SOFT_WARNINGS

    @property
    def stderr_text(self) -> str:
        return self._stderr._new_target.getvalue()
