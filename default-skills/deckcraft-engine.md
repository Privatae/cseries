---
name: deckcraft-engine
description: How to make slide decks with the DeckCraft Engine app — themes, slide layouts, titles and bullets, shapes, pictures, previewing, and saving PowerPoint (.pptx), PDF and PNG. Present when the person has installed DeckCraft Engine.
present_if_app_installed: deckcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# DeckCraft Engine

DeckCraft Engine runs DeckCraft, an open-source presentation program,
headless in the person's own space, for you. It is an MCP server named
`deckcraft-engine` (skill 'mcp', "An installed app's own server"):
connected when the app was installed, nothing to approve. The open deck lives
in the app between calls, like a program left open.

## When to use

Slide decks: talks, pitches, lessons, reports, photo stories. Also opening
the person's PowerPoint files to change, check or convert them.

## First time

1. `mcp_connections(op='tools', name='deckcraft-engine')`: it wakes the app.
   The tools are DeckCraft's own (`new_presentation`, `open_presentation`,
   `add_slide`, `go_to_slide`, `add_shape`, `add_text_box`, `set_text`,
   `insert_picture`, `insert_table`, `insert_chart`, `list_commands`,
   `run_command`, `batch`, `inspect_document`, `inspect_slide`,
   `render_slide`, `save_presentation`, `export`, …) and the app's
   `file_put`, `file_get`, `file_list`.
2. Write `skills/mcp-deckcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-deckcraft-engine', content:…})`.

## Steps

1. **Start a deck:** `new_presentation(theme: 'Nocturne')` makes one title
   slide. The themes are DeckCraft, Harbor, Ember, Meadow, Nocturne, Paper,
   Coral Reef and Slate. To edit a file instead, `file_put` it, then
   `open_presentation(path)` (.pptx or .deckcraft).
2. **Fill the title slide:** `inspect_slide(index: 0)` lists its placeholders
   with their `id`s. Then `set_text(id, text)` for the title and the subtitle.
3. **Add slides:** `add_slide(layout: 'titleAndContent', title, body)`. The
   body is one bullet per line. Layouts: title, titleAndContent,
   sectionHeader, twoContent, comparison, titleOnly, blank,
   contentWithCaption, pictureWithCaption. A new slide becomes the current
   one, and shapes go on the current slide (`go_to_slide(index)` to move).
   For a second-level bullet, read the body's `id` with `inspect_slide`, then
   `set_text(id, 'First\nSecond\n\tUnder the second\nThird')`: a leading tab
   in `set_text` indents.
4. **Shapes and pictures:** coordinates are points on a 960×540 slide, from
   the top-left. `add_shape(preset: 'roundRect', x, y, w, h, fill: 'accent2',
   line: 'none', text?)` returns its `id`. A gradient:
   `run_command(command: 'shape.fill', params: {ids: [<id>], gradient:
   {stops: [[0, '#b8467e'], [1, '#f7b267']], angle: 90, kind: 'linear'}})`.
   For a picture, `file_put` it (`path: 'in/<name>.png'`, `data:
   '/api/images/…'`), then `insert_picture(path, x, y, w, h)`. Every other
   action is a command: `list_commands(filter: '<word>')` gives the ids and
   their parameters.
5. **Look:** `render_slide(slide: 1)` returns that slide as an image, kept in
   your gallery (slides count from 0). You see it
   right after the tool results and judge it yourself (later:
   `image_generate(action='look', images=['/api/images/…'])`).
6. **Save and export** (the folder a file goes into is made for you):
   `save_presentation(path: '<name>.deckcraft')`, then
   `export(path: 'exports/<name>.pptx')` for PowerPoint,
   `export(path: 'exports/<name>.pdf')`, and
   `export(path: 'exports/slides.png', all: true, scale: 2)` for one
   1920×1080 PNG per slide (slides1.png, slides2.png, …). `file_get(path)`
   brings a PNG back to you as an image.

## Checks

- `inspect_slide` gives each shape's id, kind, box [x, y, w, h], text and
  fill. `inspect_document` lists the slides, layouts, theme and fonts.
- After writing a .pptx, `open_presentation` on it shows it reads back.
- In the PDF each slide is a picture, at `dpi` (200 unless you give one),
  with its text searchable. A 60-slide deck at `dpi: 600` is about 95 MB and
  took 30 s on one CPU; the default suits the screen, 300 to 600 suits print.

## When it fails

- **A tab shows as a gap after the bullet**: it was in `add_slide`'s body.
  Set that text with `set_text` instead.

## Reference

DeckCraft is MIT or Apache-2.0 (github.com/storytold/deckcraft). It opens
.pptx and .deckcraft files. It writes .deckcraft and .pptx, PDF (slides,
notes pages or handouts), PNG or JPEG slides, and an outline as text. Its
themes, layouts and shapes are its own; it uses no Microsoft artwork or fonts.
It has no generative features of its own: make pictures with
`image_generate` or VectorCraft Engine, then insert them here.
