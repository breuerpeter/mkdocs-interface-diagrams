from pathlib import Path

from interface_diagrams.plugin import DiagramsPlugin

FIX = Path(__file__).resolve().parent / "fixtures"


def _cfg(docs_dir: Path) -> dict:
    return {"docs_dir": str(docs_dir), "extra_javascript": [], "extra_css": []}


def test_on_config_discovers_systems_and_injects_assets(monkeypatch):
    monkeypatch.setattr("interface_diagrams.workers.resolve_node", lambda: "node")
    monkeypatch.setattr("interface_diagrams.workers.check_node", lambda n: None)
    p = DiagramsPlugin()
    p.load_config({})
    cfg = _cfg(FIX)               # fixtures/parity has index.md with system: frontmatter
    p.on_config(cfg)
    names = {s for _, _, s in p._jobs}
    assert "Parity Demo" in names
    assert "assets/diagrams/_assets/diagram-lightbox.js" in cfg["extra_javascript"]
    assert "assets/diagrams/_assets/diagram.css" in cfg["extra_css"]
    assert len(p._jobs) == 1
