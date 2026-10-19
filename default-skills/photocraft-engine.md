---
name: photocraft-engine
description: How to edit images with the PhotoCraft Engine app — opening the person's images or your renders, layers, masks, adjustments, filters and text, previewing, and exporting. Present when the person has installed PhotoCraft Engine.
present_if_app_installed: photocraft-engine
allowed-tools: mcp_connections, operate, image_generate, context, artifact
---

# PhotoCraft Engine

PhotoCraft Engine runs PhotoCraft, an open-source image editor, headless in
the person's own space, for you. It is an MCP server named
`photocraft-engine` (skill 'mcp', "An installed app's own server"):
connected when the app was installed, nothing to approve. One document
session lives in the app between calls, like an editor left open.

## When to use

Compositing, retouching, colour work, adding text or shapes, resizing and
exporting images: the person's own photos, images you generated
(`image_generate`), or renders from Blender Engine.

## First time

1. `mcp_connections(op='tools', name='photocraft-engine')`: it wakes the app.
   The tools are PhotoCraft's own (`doc_new`, `doc_open`, `command_list`,
   `command_run`, `command_batch`, `render_preview`, `doc_save`, `doc_export`,
   `doc_inspect`, …) and the app's `file_put`, `file_get`, `file_list`.
2. Write `skills/mcp-photocraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-photocraft-engine', content:…})`.

## Steps

1. **Bring an image in:** `file_put` with `path: 'in/<name>.png'` and
   `data: '/api/images/…'` (one of your images by its name; it arrives as the
   image itself). Then `doc_open` with that path. Or start blank with
   `doc_new` (width, height, background).
2. **Find the command:** every edit is an engine command.
   `command_list(filter='<word>')` gives its id and the parameters it takes,
   for example `filter.blur.gaussianBlur {radius}` or
   `layer.newFillLayer.gradient {from, to, angle, style}`. Run one with
   `command_run(id, params)`, several with `command_batch`.
3. **Look:** `render_preview(max_side: 1024)` returns the flattened document
   as an image, kept in your gallery. You see it
   right after the tool results and judge it yourself (later:
   `image_generate(action='look', images=['/api/images/…'])`).
4. **Save:** `doc_save(path: 'out/<name>.jpg')` exports by the extension
   (.png, .jpg, .webp, .tif, .psd, .pcraft); `.psd` and `.pcraft` keep the
   layers. `file_get(path)` brings an exported image back to you, kept in
   your gallery.

## Checks

- A command's result names what it changed; `doc_inspect` shows the layer
  tree, the active layer and the history.
- Many filters work only on a pixel layer: one refused with "needs a pixel
  layer" means select or rasterize one first (`command_list(filter='rasterize')`).

## When it fails

- **"not available right now"**: the command needs a different active layer
  or a selection; `doc_inspect` shows what is active.
- **A path is refused**: paths are relative, inside the app's files, with no
  `..`.
- **An image did not come back whole**: one comes back up to 20 MB, the size
  your gallery keeps. For a larger one, export a JPEG or WebP copy.

## Reference

PhotoCraft is MIT or Apache-2.0 (github.com/storytold/photocraft). It has no
generative features of its own: generate with `image_generate`, then edit
here. What you make is the person's wallpaper with `operate(tool='artifact', args={op:'set_wallpaper',
image_url:'/api/images/…'})`, and goes into any design or artifact by its
`/api/images/…` name (skill 'desktop', rule 4).
