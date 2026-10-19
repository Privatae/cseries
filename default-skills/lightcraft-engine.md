---
name: lightcraft-engine
description: How to develop photos with the LightCraft Engine app — importing the person's photos into their library, exposure, contrast and the other develop sliders, presets, crops, previewing, and exporting JPEG, PNG, TIFF, WebP, AVIF or DNG. Present when the person has installed LightCraft Engine.
present_if_app_installed: lightcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# LightCraft Engine

LightCraft Engine runs LightCraft, an open-source photo library and raw
developer, headless in the person's own space, for you. It is an MCP server
named `lightcraft-engine` (skill 'mcp', "An installed app's own server"):
connected when the app was installed, nothing to approve. The person's photo
library lives in the app and keeps every photo and its edits between calls
and between sessions. Edits never change the original file.

## When to use

Developing photos: the person's own photos and raw files, or images you
generated (`image_generate`). Light and colour corrections, presets, crops
and straightening, then exporting copies at the size and format wanted.
For layers, text or compositing, use PhotoCraft Engine instead.

## First time

1. `mcp_connections(op='tools', name='lightcraft-engine')`: it wakes the app.
   The tools are LightCraft's own (`import`, `query_photos`, `select_photos`,
   `list_controls`, `set_develop`, `get_develop`, `apply_preset`, `crop`,
   `render_preview`, `render_photo`, `export`, `doc_inspect`, `command_list`,
   `command_run`, `command_batch`, …) and the app's `file_put`, `file_get`,
   `file_list`.
2. Write `skills/mcp-lightcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-lightcraft-engine', content:…})`.

## Steps

1. **Bring a photo in:** `file_put` with `path: 'in/<name>.jpg'` and
   `data: '/api/images/…'`. Then `import` with `paths: ['in/<name>.jpg']`
   (a folder imports everything in it). The answer lists the new photo ids in
   `imported`; the first becomes the active photo. The library refers to the
   file where it is, so leave it there, or pass `mode: 'copy'` to copy it into
   the library.
2. **Find the photo:** `query_photos` lists ids, names, sizes and whether each
   is edited. Tools that take `id` make that photo active; the others act on
   the active photo (`select_photos(ids)` to choose).
3. **Develop:** `list_controls(section: 'light')` gives every slider's id,
   range and value (sections such as light, color, effects, detail). Set them
   with `set_develop(values: {'light.exposure': 0.5, 'light.contrast': 25})`.
4. **Presets:** `command_run(id: 'presets.list')` gives preset ids such as
   `lc.warm-glow`; `apply_preset(preset: 'lc.warm-glow', amount: 60)` applies
   one at that strength (0 to 200, default 100).
5. **Crop:** `crop(rect: [x0, y0, x1, y1])` in fractions of the frame, 0 to 1
   from the top left; `angle` straightens; `reset: true` clears it.
6. **Look:** `render_preview(max_side: 1024, format: 'jpeg')` returns the
   developed photo as an image, kept in your gallery. You see it
   right after the tool results and judge it yourself (later:
   `image_generate(action='look', images=['/api/images/…'])`).
7. **Export:** `export(path: 'out/<name>.jpg', longEdge: 2400, quality: 90)`
   writes one photo; the extension picks the format (.jpg, .png, .tif, .webp,
   .avif, .dng). Without a size it exports 3000 px on the long edge;
   `longEdge: 0` is full size. Several photos: `export(ids: [...], dir:
   'out/<folder>', format: 'jpeg')`. `file_get(path)` brings an exported
   image back to you, kept in your gallery.

Any other LightCraft command (ratings, flags, albums, keywords, masks, spot
removal, history, virtual copies, metadata) is reachable the same way:
`command_list(filter: '<word>')` gives its id and parameters, and
`command_run(id, params)` runs it; `command_batch(steps)` runs several in
order.

## Checks

- `export` answers with each file written: its path, width, height and bytes.
- `get_develop` shows the photo's full settings; `query_photos` marks a
  photo `edited: true` once it has edits.
- `doc_inspect` shows the selection, the active photo and what undo would undo.

## When it fails

- **A large export:** one call has about 14 minutes. A full-size export of a
  24-megapixel photo took 1 to 2 seconds, so export up to about 200 photos per
  call and split bigger sets across calls.
- **AI masks, AI denoise and face recognition:** they need models that are
  not installed in this app. Radial, linear, colour-range and
  luminance-range masks work: `command_run(id: 'mask.add', params:
  {kind: 'radial', center: [0.5, 0.5], rx: 0.3, ry: 0.3})`;
  `command_list(filter: 'mask')` gives the rest.
- **An image did not come back whole:** one comes back up to 20 MB, the size
  your gallery keeps. For a larger one, export a JPEG or WebP copy.

## Reference

LightCraft is MIT or Apache-2.0 (github.com/storytold/lightcraft). It renders
on the CPU here. It has no generative features of its own: generate with
`image_generate`, then develop here.
