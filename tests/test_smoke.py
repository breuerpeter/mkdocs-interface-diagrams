import importlib


def test_package_imports_without_mkdocs():
    mod = importlib.import_module("interface_diagrams")
    assert mod.__version__ == "0.0.0"
