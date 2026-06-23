# Changelog

## 0.1.0 (2026-06-23)


### Features

* **cli:** generate|check entry point sharing generate_section ([724b6d8](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/724b6d8310a8eccf140d1e83ec1ff64b66fc8212))
* lift generator into interface_diagrams package ([5c890dc](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/5c890dc5b0e8040dc08534a51ecba92430db9ddb))
* **plugin:** auto-discover systems, node check, inject lightbox assets ([4ae025a](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/4ae025ad98648cb34b4d8a89770651c7233cb516))
* **plugin:** cache-gated generation in on_pre_build ([12c3118](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/12c3118e8b2c9c7bf5d70a110e8998a0c8710870))
* **plugin:** port hook into DiagramsPlugin (rewrite-only) ([d3c8bb9](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/d3c8bb9321436a9fb0da85edd94e4f9c7e253f82))


### Bug Fixes

* clear stale caches, guard unset pools, and wrap bad node path ([c534681](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/c534681815b861e12778a42dcf984add6c672177))
* portable POSIX src_uri in plugin.py + tighten discovery test assertions ([f0918ee](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/f0918ee93f43cfa4ce9d05f60c5fde2c9ec58cb8))
* reset validation counters per generate_section call ([734c2c1](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/734c2c131eaa02c9f7866f729b14da55038fdf87))
* scope require.resolve shim and decode render test envelope ([1a53170](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/1a531705379378b050756083f9df2d7663e26fdd))
* **tests:** point validation counter resets at live edges module ([93f239d](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/93f239dcd580553c6d9f7eecbfd2fb3f8a57d948))


### Build System

* fix wheel duplicate-file error; include gitignored JS bundles ([49bca60](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/49bca602e17b94e440d468dad5e14546b53ed998))
* **js:** vendored esbuild bundles + clean-checkout smoke gate ([69d9e30](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/69d9e30d54e797f3c20fbdb31cd1d2d6d65c9dc3))


### Refactors

* **generate:** DRY toolchain check, remove stale newton defaults ([9a5fae6](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/9a5fae6842b64cc76b7534e262433a7a93738e56))
* split generator into focused modules ([1009e11](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/1009e114b5de8344e9467b696fe069c7ecc4c3be))
* use mkdocs's get_relative_url instead of inlined reimplementation ([2efdbfa](https://github.com/breuerpeter/mkdocs-interface-diagrams/commit/2efdbfa42f8909035ac7681e9638a9a982176415))
