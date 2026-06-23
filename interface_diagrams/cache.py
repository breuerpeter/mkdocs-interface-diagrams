"""Per-job content-hash cache for the generation pass."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_MANIFEST = ".interface-diagrams-cache.json"


def job_key(section: Path, extra: str) -> str:
    h = hashlib.sha256()
    for md in sorted(section.rglob("*.md")):
        h.update(md.relative_to(section).as_posix().encode())
        h.update(md.read_bytes())
    h.update(extra.encode())
    return h.hexdigest()


def is_fresh(out_dir: Path, key: str) -> bool:
    mf = out_dir / _MANIFEST
    if not mf.exists():
        return False
    try:
        saved = json.loads(mf.read_text()).get("key")
    except (OSError, ValueError):
        return False
    return saved == key and any(out_dir.glob("*.svg"))


def write(out_dir: Path, key: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / _MANIFEST).write_text(json.dumps({"key": key}))
