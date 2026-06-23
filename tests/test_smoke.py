import importlib


def test_package_imports_and_reports_a_version():
    mod = importlib.import_module("interface_diagrams")
    # Version is derived from installed metadata (single source of truth in
    # pyproject.toml, bumped by release-please) — assert it's a real string,
    # not a hardcoded literal that would break on every release bump.
    assert isinstance(mod.__version__, str) and mod.__version__


def test_generator_core_imports_without_mkdocs():
    # The generator core must not depend on mkdocs (mkdocs is an optional extra;
    # only the plugin/_hooklogic may import it). Importing these must not pull in
    # mkdocs as a hard requirement.
    for name in (
        "interface_diagrams.generate",
        "interface_diagrams.cli",
        "interface_diagrams.cache",
        "interface_diagrams.workers",
        "interface_diagrams.model",
        "interface_diagrams.parse",
        "interface_diagrams.edges",
        "interface_diagrams.views",
        "interface_diagrams.elk",
        "interface_diagrams.excalidraw",
    ):
        importlib.import_module(name)
