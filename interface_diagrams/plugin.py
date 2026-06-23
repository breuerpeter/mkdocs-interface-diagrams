"""mkdocs plugin entry point. The ONLY module that imports mkdocs."""

from __future__ import annotations

from mkdocs.config import config_options as c
from mkdocs.plugins import BasePlugin

from interface_diagrams import _hooklogic


class DiagramsPlugin(BasePlugin):
    config_scheme = (
        ("docs_dir", c.Type(str, default="")),          # "" => use mkdocs docs_dir
        ("out_root", c.Type(str, default="assets/diagrams")),
        ("generate", c.Type(bool, default=True)),
        ("cache", c.Type(bool, default=True)),
        ("node_path", c.Optional(c.Type(str))),
        ("exclude", c.Type(list, default=[])),
    )

    def on_page_markdown(self, markdown, page, config, files):
        return _hooklogic.apply_page_markdown(markdown, page, config, files)

    def on_post_build(self, config):
        return _hooklogic.fix_built_svgs(config)
