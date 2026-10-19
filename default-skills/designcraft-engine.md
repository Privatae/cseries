---
name: designcraft-engine
description: How to lay out pages with the DesignCraft Engine app — documents and pages, text frames, paragraph styles, placed pictures, colour and gradients, previewing, and exporting PDF, PNG, IDML and EPUB. Present when the person has installed DesignCraft Engine.
present_if_app_installed: designcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# DesignCraft Engine

DesignCraft Engine runs DesignCraft, an open-source page-layout program,
headless in the person's own space, for you. It is an MCP server named
`designcraft-engine` (skill 'mcp', "An installed app's own server"):
connected when the app was installed, nothing to approve. The open document
lives in the app between calls, like a program left open.

## When to use

Anything printed or paged: flyers, posters, zines, magazines, reports,
menus, invitations, books. Use it when text has to flow in frames and columns
around pictures, with styles kept across pages.

## First time

1. `mcp_connections(op='tools', name='designcraft-engine')`: it wakes the app.
   The tools are DesignCraft's own (`new_document`, `open_document`,
   `list_commands`, `execute`, `batch`, `inspect_document`, `get_story`,
   `set_story_text`, `place_image`, `render_page`, `export_png`,
   `save_document`, …) and the app's `file_put`, `file_get`, `file_list`.
2. Write `skills/mcp-designcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-designcraft-engine', content:…})`.

## Steps

1. **Start a document:** `new_document(preset: 'Letter', pages: 2,
   facingPages: false, margins: 54, title)`. Sizes are in points (72 pt = 1
   in); `preset` also takes 'A4', 'Tabloid', …, or give `width` and `height`.
   Each page of a document without facing pages is its own spread: page 2 is
   `spread: 1`. To edit a file instead, `file_put` it, then
   `open_document(path)`.
2. **Text frames:** `execute(command: 'frame.create', params: {spread: 0,
   rect: [54, 54, 558, 110], content: 'text', text: '…'})` returns the frame
   `id` and its `story`. `rect` is [left, top, right, bottom] in points. Use
   `\n` between paragraphs. `set_story_text(story, text)` replaces the text
   later.
3. **Styles:** make them once with `execute(command: 'style.paragraph.create', params: {name:
   'Body', chars: {size: 11}, para: {spaceAfter: 6}})`. To apply one, select
   the story's text with `execute(command: 'text.select', params: {story, anchor: 0, focus:
   <length>})` (lengths count UTF-8 bytes; `get_story` gives them), then
   `execute(command: 'style.paragraph.apply', params: {name: 'Body'})`. The body font is
   Source Serif 4.
4. **Pictures:** `file_put` the image (`path: 'in/<name>.png'`, `data:
   '/api/images/…'`), then `place_image(path: 'in/<name>.png', x, y, width)`.
   It makes a frame at the picture's own proportions. PNG, JPEG, WebP, GIF and
   TIFF work.
5. **Layout:** a new frame is not selected, so name it with `ids`, as in
   `execute(command: 'object.textFrameOptions', params: {ids: [<frame id>], columns: 2,
   gutter: 18})`. For a gradient, use `execute(command: 'object.gradient', params: {ids: […],
   kind: 'linear', angle: 0, stops: [{location: 0, color: '#141a46'},
   {location: 100, color: '#f7b267'}]})`. Pages:
   `execute(command: 'layout.pages.insert', params: {count})`. Every other action is a
   command: `list_commands(filter: '<word>')` gives the ids and their
   parameters, and `batch` runs several in a row.
6. **Look:** `render_page(page: 0)` returns that page as an image, kept in
   your gallery (pages count from 0). You see it
   right after the tool results and judge it yourself (later:
   `image_generate(action='look', images=['/api/images/…'])`).
7. **Save and export** (the folder a file goes into is made for you):
   `save_document(path: '<name>.designcraft')`;
   `export_png(path: 'exports/page-1.png', page: 0, scale: 2)`;
   `execute(command: 'file.exportPdf', params: {path: 'exports/<name>.pdf'})`;
   `execute(command: 'file.exportIdml', params: {path: 'exports/<name>.idml'})` for InDesign.
   `file_get(path)` brings a PNG back to you as an image.

## Checks

- `inspect_document` lists the pages, every item (id, kind, bounds), the
  stories with `overset` (text that does not fit its frame) and the styles.
- `get_story` gives a story's text, its length and where it overflows.

## When it fails

- **"is not available right now: nothing selected"**: give the command the
  frame's `ids`.
- **"no text insertion point"** from `type_text`: use `set_story_text` or
  `frame.create` with `text`.

## Reference

DesignCraft is MIT or Apache-2.0 (github.com/storytold/designcraft). It opens
.designcraft files (`open_document`), and .idml through
`execute(command: 'file.open', params: {path})`. It places PNG, JPEG, WebP, GIF, TIFF and PDF
pages as pictures, and .txt, .docx, .rtf, .md and .xlsx as text
(`execute(command: 'file.place', params: …)`). It writes .designcraft, PDF (`file.exportPdf`
takes `standard: 'x4'` or `'a2b'` for PDF/X-4 or PDF/A-2b), PNG or JPEG pages,
IDML, EPUB (`file.exportEpub`), HTML, text and XML. It has no
generative features of its own: make pictures with `image_generate` or
VectorCraft Engine, then place them here.
