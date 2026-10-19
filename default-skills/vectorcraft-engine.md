---
name: vectorcraft-engine
description: How to draw vector art with the VectorCraft Engine app — shapes, paths, gradients, type and effects, previewing, and saving to SVG, PDF, PNG and VectorCraft's own file. Present when the person has installed VectorCraft Engine.
present_if_app_installed: vectorcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context, artifact
---

# VectorCraft Engine

VectorCraft Engine runs VectorCraft, an open-source vector illustration
program, headless in the person's own space, for you. It is an MCP server
named `vectorcraft-engine` (skill 'mcp', "An installed app's own server"):
connected when the app was installed, nothing to approve. The open drawing
lives in the app between calls, like a program left open.

## When to use

Wallpapers, posters, logos, icons, diagrams and illustrations: anything that
should stay sharp at any size. Also opening and editing the person's SVG, PDF,
Illustrator (.ai) and EPS files, or turning them into other formats.

## First time

1. `mcp_connections(op='tools', name='vectorcraft-engine')`: it wakes the app.
   The tools are VectorCraft's own (`draw_shape`, `draw_path`, `set_paint`,
   `add_text`, `apply_effect`, `transform`, `pathfinder`, `command_list`,
   `command_run`, `command_batch`, `doc_inspect`, `render_preview`,
   `open_file`, `save_file`, `export`, …) and the app's `file_put`,
   `file_get`, `file_list`.
2. Write `skills/mcp-vectorcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-vectorcraft-engine', content:…})`.

## Steps

1. **Start a drawing:** `command_run(id='file.new', params={name: 'Dusk',
   width: 1920, height: 1080, units: 'Pixels'})`. Without it the drawing is a
   612×792 pt page. To edit a file instead, `file_put` it (`path:
   'in/<name>.svg'`, `data: '/api/images/…'` or base64), then
   `open_file(path)`.
2. **Draw:** coordinates are points, y down, from the artboard's top-left.
   - `draw_shape(shape='rectangle', x, y, width, height, radius?)`; `ellipse`
     takes the same box; `polygon(cx, cy, radius, sides)`;
     `star(cx, cy, radius1, radius2, points)`; `line(x1, y1, x2, y2)`.
   - `draw_path(d='M0 1080 L0 720 L760 470 L1920 580 L1920 1080 Z')`, or
     `points` as `[x, y]` pairs or `{x, y, in: [x, y], out: [x, y]}` for curves,
     with `closed: true`.
   - `fill` and `stroke` on any of them: `'#rrggbb'`, `'none'`, or a gradient,
     `{gradient: {kind: 'linear', start: [960, 0], end: [960, 1080], stops:
     [{offset: 0, color: '#141a46'}, {offset: 1, color: '#f7b267'}]}}`.
     `kind: 'radial'` with no start and end fits the object.
   - Text: `add_text(text, x, y, size, color, font?)`, where y is the first
     baseline; give `width` and `height` for a text box. The fonts are Source
     Sans 3 (used when none is named), Inter, Source Serif 4, JetBrains Mono
     and three Japanese faces; `command_run(id='text.fontList')` lists them.
   - Effects: `apply_effect(effect='stylize.dropShadow', params={x: 6, y: 8,
     blur: 12, opacity: 60})`; with no `effect` it lists them all.
   - A new object becomes the selection and its `id` comes back. Most tools act
     on the selection, or on `ids`.
   - Everything else is a command: `command_list(filter='<word>')` gives ids
     and parameters, `command_run(id, params)` runs one and
     `command_batch(steps)` runs several. `select.all` selects everything.
3. **Look:** `render_preview(max_side: 1024)` returns the first artboard as an
   image, kept in your gallery. You see it
   right after the tool results and judge it yourself (later:
   `image_generate(action='look', images=['/api/images/…'])`).
4. **Save:** `save_file(path: '<name>.vectorcraft')` keeps everything
   editable; the folder a file goes into is made for you. `export(path: 'exports/<name>.svg')` writes the format its
   extension names: .svg, .pdf, .png, .jpg, .webp, .eps, .psd and more. For
   images, `scale` is pixels per point, so `scale: 2` on a 1920×1080 drawing
   gives 3840×2160. `file_get(path)` brings a PNG or JPEG back to you as an
   image, kept in your gallery.

## Checks

- `doc_inspect` shows the artboards, the layer tree (ids, bounds, fill and
  stroke), the selection and the history.
- An SVG names its fonts, so it looks different on a machine without them.
  `export(path, outlineText: true)` turns the text into shapes.
- An export's `warnings` say what that format left out.

## When it fails

- **"automation path rejected"**: paths are relative, inside the app's files.
- **"needs the desktop app"**: `screenshot(window: true)` shows a desktop
  app's window, and there is none here. Use `render_preview`, or `screenshot`
  without `window`.
- **"unknown command"**: find the id with `command_list(filter)`.
- **A very large image export** needs memory in proportion: 7680×4320 took
  about 0.5 GB when measured, 15360×8640 up to 4.3 GB. Past the app's memory
  the program restarts and the open drawing is lost, so save before a big
  export.

## Reference

VectorCraft is MIT or Apache-2.0 (github.com/storytold/vectorcraft). It reads
.vectorcraft, .svg, .svgz, .pdf, .ai, .ait, .eps, .dxf, .emf, .wmf, .psd and
.psb (as their merged image), Affinity files (.af, .afdesign, .afpub) and
images (.png, .jpg, .gif, .webp, .tif, .bmp). It writes .vectorcraft, .svg,
.svgz, .pdf, .ai, .eps, .dxf, .emf, .wmf, .png, .jpg, .gif, .webp, .tif,
.bmp, .tga, .psd and .txt. It has no generative features of its own: generate
with `image_generate`, then place or trace here. What you make is the person's
wallpaper with `operate(tool='artifact', args={op:'set_wallpaper', image_url:'/api/images/…'})`, and
goes into any design or artifact by its `/api/images/…` name (skill
'desktop', rule 4).
