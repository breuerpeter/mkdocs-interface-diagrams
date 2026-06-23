"""mkdocs plugin entry point. The ONLY module that imports mkdocs."""

from __future__ import annotations

from importlib.resources import files as _res
from pathlib import Path

from mkdocs.config import config_options as c
from mkdocs.plugins import BasePlugin
from mkdocs.structure.files import File

from interface_diagrams import __version__, _hooklogic, cache, manifest, workers
from interface_diagrams.generate import generate_section


class DiagramsPlugin(BasePlugin):
    config_scheme = (
        ("docs_dir", c.Type(str, default="")),          # "" => use mkdocs docs_dir
        ("out_root", c.Type(str, default="assets/diagrams")),
        ("generate", c.Type(bool, default=True)),
        ("cache", c.Type(bool, default=True)),
        ("node_path", c.Optional(c.Type(str))),
        ("exclude", c.Type(list, default=[])),
    )

    def on_config(self, config):
        _hooklogic._reset_caches()
        node = self.config["node_path"] or workers.resolve_node()
        workers.check_node(node)
        self._node = node
        docs_dir = Path(self.config["docs_dir"] or config["docs_dir"])
        out_root = self.config["out_root"]
        exclude = set(self.config["exclude"])
        self._jobs = []
        for sub in sorted(p for p in docs_dir.iterdir() if p.is_dir()):
            if sub.name in exclude:
                continue
            index = sub / "index.md"
            if not index.exists():
                continue
            name = manifest.landing_system_name(index)
            if not name:
                continue
            out_dir = docs_dir / out_root / sub.name
            self._jobs.append((sub, out_dir, name))
        config["extra_javascript"].append(f"{out_root}/_assets/diagram-lightbox.js")
        config["extra_css"].append(f"{out_root}/_assets/diagram.css")
        return config

    def on_pre_build(self, config):
        if not self.config["generate"]:
            return
        bundles = workers.bundle_path("render_svg.bundle.mjs").stat().st_mtime_ns
        extra = f"{__version__}:{bundles}"
        for section, out_dir, _name in self._jobs:
            key = cache.job_key(section, extra)
            if self.config["cache"] and cache.is_fresh(out_dir, key):
                continue
            generate_section(section, out_dir, check=False)
            cache.write(out_dir, key)

    def on_files(self, files, config):
        out_root = self.config["out_root"]
        assets = _res("interface_diagrams") / "_assets"
        # Register the packaged assets (diagram-lightbox.js and diagram.css live
        # OUTSIDE docs_dir inside the installed package, so they MUST be
        # explicitly registered). The generated SVGs live inside docs_dir and are
        # auto-discovered by mkdocs — registering them here too would cause a
        # duplicate dest-path error during the real build.
        for asset in ("diagram-lightbox.js", "diagram.css"):
            files.append(File.generated(config, f"{out_root}/_assets/{asset}",
                                        abs_src_path=str(assets / asset)))
        return files

    def on_page_markdown(self, markdown, page, config, files):
        return _hooklogic.apply_page_markdown(markdown, page, config, files)

    def on_post_build(self, config):
        return _hooklogic.fix_built_svgs(config)
