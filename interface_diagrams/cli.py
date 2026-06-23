"""`interface-diagrams generate|check` console entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from interface_diagrams.generate import generate_section


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="interface-diagrams")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="render diagrams for a system folder")
    g.add_argument("section", type=Path)
    g.add_argument("--out", type=Path, required=True)

    c = sub.add_parser("check", help="parse and validate only, write nothing")
    c.add_argument("section", type=Path)

    args = ap.parse_args(argv)
    if args.cmd == "generate":
        return generate_section(args.section, args.out, check=False)
    return generate_section(args.section, Path("/dev/null"), check=True)
