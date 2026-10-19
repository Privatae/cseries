---
name: cadcraft-engine
description: How to make technical drawings with the CADCraft Engine app — lines, circles, rectangles, layers, dimensions and text by AutoCAD-style commands, looking at the drawing, and saving DXF or DWG and exporting PNG, SVG or PDF. Present when the person has installed CADCraft Engine.
present_if_app_installed: cadcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# CADCraft Engine

CADCraft Engine runs CADCraft, an open-source 2D drafting program in the
style of AutoCAD, headless in the person's own space, for you. It is an MCP
server named `cadcraft-engine` (skill 'mcp', "An installed app's own
server"): connected when the app was installed, nothing to approve. One
drawing stays open in the app between calls.

## When to use

Plans, parts and diagrams drawn to measure: a floor plan, a bracket, a
mounting plate, a site layout, with dimensions and notes, and opening or
converting the person's DXF files.

## First time

1. `mcp_connections(op='tools', name='cadcraft-engine')`: it wakes the app.
   The tools are CADCraft's own (`new_drawing`, `open`, `command_line`,
   `script`, `execute`, `list_commands`, `inspect_drawing`, `query_entities`,
   `render`, `save`, `cancel`) and the app's `file_put`, `file_get`,
   `file_list`.
2. Write `skills/mcp-cadcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-cadcraft-engine', content:…})`.

## Steps

Coordinates are drawing units, y up.

1. **A drawing:** `new_drawing(metric: true)` (imperial without it), or
   `file_put` the person's DXF into `in/` and `open(path: 'in/<name>.dxf')`.
2. **Layers:** `execute(command: 'layer.new', params: {name: 'Outline', color: 'white', current: true})`.
   New objects go on the current layer.
3. **Draw:** type at the command line as in AutoCAD:
   `command_line(text: 'rectang 0,0 100,60')`, `command_line(text: 'circle 15,15 4')`.
   Or JSON with `execute`: `line {points: [[30,30],[70,30]]}`,
   `circle {center: [50,30], radius: 15}`, `text {at: [0,70], text: 'PLATE', height: 5}`.
   Each `execute` answers the new object's `handle`. `list_commands(filter: '<word>')`
   gives ids, aliases and parameters.
4. **Dimensions:** `execute(command: 'dimlinear', params: {p1: [0,0], p2: [100,0], at: [50,-12]})`
   (`at` places the dimension line), `dimdiameter {center: [50,30], point: [60.6,40.6]}`,
   also `dimaligned`, `dimradius`, `dimangular`.
5. **Look:** `render(width: 1200, height: 800)` returns the drawing fitted to its
   extents as an image, kept in your gallery. You see it
   right after the tool results and judge it yourself (later:
   `image_generate(action='look', images=['/api/images/…'])`).
6. **Save:** `save(path: 'drawings/<name>.dxf')` (or `.dwg`). With `.png`
   (2400×1600), `.svg` or `.pdf` the same call exports instead and the drawing
   keeps its name. `file_get` brings a PNG back to you.

## Checks

- `inspect_drawing` counts the objects by type (`Circle: 3, Dimension: 3, …`),
  lists layers and extents, and says `dirty: false` after a save.
- `query_entities(layer: 'Dims')` or `(type: 'Circle')` returns objects with
  their geometry, to check a size or position.
- A `command_line` answer shows the prompt it ended at: `"prompt": "Command:"`
  means the command finished; any other prompt is waiting for input, and
  `cancel` ends it.

## When it fails

- **A command waits for more input**: answer its prompt with another
  `command_line`, or `cancel`.
- **The export framing**: PNG, SVG and PDF fit the drawing's extents.

## Reference

CADCraft is MIT or Apache-2.0 (github.com/storytold/cadcraft). Its drafting
font, hatch patterns and linetypes are its own. It reads and writes DXF and
writes DWG (AutoCAD 2004 format here).
