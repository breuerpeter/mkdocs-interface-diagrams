# mkdocs-interface-diagrams

Interface-documentation diagram generator and mkdocs plugin (ELK + Excalidraw).

You describe devices, their interfaces and the data flows between them in plain Markdown headings. The plugin draws one SVG per subsystem, device, component, interface and flow, and puts each on the heading it belongs to.

## Install

```sh
pip install "mkdocs-interface-diagrams[mkdocs]"
```

The renderer runs on Node.js 20 or later. It must be on `PATH`, or named by the `node_path` option or the `INTERFACE_DIAGRAMS_NODE` environment variable.

```yaml
# mkdocs.yml
plugins:
  - interface-diagrams
```

| Option | Default | Effect |
|---|---|---|
| `docs_dir` | mkdocs' `docs_dir` | Where to look for sections. |
| `out_root` | `assets/diagrams` | Where the SVGs go, under `docs_dir`. Pages find no diagrams under any other value yet. |
| `generate` | `true` | Render the diagrams on each build. |
| `cache` | `true` | Skip a section whose docs have not changed since the last render. |
| `node_path` | none | The Node.js binary to use. |
| `exclude` | `[]` | Folder names under `docs_dir` that are not sections. |

The SVGs are written into the docs tree, to `<docs_dir>/<out_root>/<section>/`, so you may want to ignore that folder in git.

## Sections

A section is a top-level folder under `docs_dir` whose `index.md` names the system in its front matter. Each other `.md` file in the folder is one subsystem. Its file name is how flows refer to it.

```
docs/
  fleet/
    index.md
    controller.md
    display.md
```

```markdown
---
system: Fleet
targets: [x_1, x_2]
---
# Fleet
```

The landing page shows the system diagram under its title. `targets:` is optional; see [Targets](#targets).

## Subsystem docs

Headings carry the model. The level says what a heading is:

| Heading | Is |
|---|---|
| `#` | the subsystem |
| `##` | a device |
| `### Interfaces`, then `####` | an interface of the device |
| `### Components`, then `####` | a component of the device |
| `#####` under a component | an interface of the component |
| `#####` under a device interface, `######` under a component interface | a payload: the kind of data its flows carry |

Under a payload, each flow is a bold label on its own line and a numbered list of waypoints. The flow starts at the interface above it. Each waypoint is `[[<doc>#<device> > <interface>]]` or `[[<doc>#<device> > <component> > <interface>]]`; leave out `<doc>` for the same doc. A flow should start and end at a component interface; the generator warns otherwise.

```markdown
# Controller

## MCU
### Interfaces
#### eth0
### Components
#### app
##### udp_out
###### Commands

**Telemetry uplink**
`x_1`
1. [[#MCU > eth0]]
2. [[display#Panel > eth0]]
3. [[display#Panel > ui > udp_in]]

**Heartbeat**
1. [[#MCU > eth0]]
2. [[display#Panel > eth0]]
3. [[display#Panel > ui > udp_in]]
```

```markdown
# Display

## Panel
### Interfaces
#### eth0
### Components
#### ui
##### udp_in
```

A heading or flow label that has a diagram becomes the link that opens it. A flow's waypoints become links to the interface headings.

## Targets

A target is one variant of the system, such as one vehicle build. `targets:` on the landing page declares them. A name holds only letters, digits, `.`, `_` and `-`, and starts with a letter or a digit.

A line of code spans right under a flow's label tags the flow, like `` `x_1` `` above. Each entry is a target name or a glob such as `` `x_*` ``. A flow with no tag belongs to every target. An entry that matches no declared target stops the build.

Each target gets its own set of diagrams, drawn from its flows alone, in `<out_root>/<section>/<target>/`. A page opens a target's view with a token:

| Token | Opens |
|---|---|
| `[[target:<section>/<name>]]` | that section's view of `<name>`, from any page |
| `[[target:<name>]]` | the view of `<name>` in the page's own section |
| `[[target:<section>/<name>\|<text>]]` | the same, with `<text>` as the link text |

A token stops the build and names the page when its section does not declare the target, or when the folder it names has no rendered view: the folder declares no `system:`, or `exclude` names it.

## The lightbox

A diagram link opens its diagram in an overlay on the page. A link inside a diagram opens the next diagram. The toolbar has "Back to system diagram", which opens the system diagram of the shown diagram's section, and "Download diagram". In a target's view, closing the overlay moves the page to the section of the last diagram shown. If that is still the diagram a link on the page opened, the page stays where it is.

## Command line

```sh
interface-diagrams check docs/fleet                        # parse and validate, write nothing
interface-diagrams generate docs/fleet --out diagrams/fleet   # render one section's SVGs
```
