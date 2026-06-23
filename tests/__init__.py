"""Diagram-pipeline test package.

Put scripts/ on sys.path here — at package-import time, before unittest imports
any test module — so test modules can import the pipeline (``generate``,
``embed``) regardless of their own import order (e.g. after ruff/isort sorts
imports alphabetically, which would otherwise place ``generate`` before the
``helpers`` import that used to bootstrap the path)."""

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
