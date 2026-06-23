"""Locate the Node runtime and the vendored worker bundles."""

from __future__ import annotations

import os
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

MIN_NODE_MAJOR = 20


def resolve_node() -> str:
    node = os.environ.get("INTERFACE_DIAGRAMS_NODE") or shutil.which("node")
    if not node:
        raise RuntimeError(
            "interface-diagrams needs Node >= 20 on PATH (or set "
            "INTERFACE_DIAGRAMS_NODE / the plugin's node_path)."
        )
    return node


def check_node(node: str) -> None:
    out = subprocess.run([node, "--version"], capture_output=True, text=True)
    ver = out.stdout.strip().lstrip("v")
    major = int(ver.split(".")[0]) if ver else 0
    if major < MIN_NODE_MAJOR:
        raise RuntimeError(f"interface-diagrams needs Node >= {MIN_NODE_MAJOR}; found {ver or 'unknown'}.")


def bundle_path(name: str) -> Path:
    return Path(files("interface_diagrams") / "_js" / name)


def fonts_dir() -> Path:
    return Path(files("interface_diagrams") / "_fonts")
