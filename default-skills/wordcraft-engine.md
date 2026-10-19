---
name: wordcraft-engine
description: How to write documents with the WordCraft Engine app — headings, paragraphs, lists, tables, styles and page layout, previewing pages, and saving as Word (DOCX), PDF, ODT, RTF, HTML, Markdown, LaTeX or text. Present when the person has installed WordCraft Engine.
present_if_app_installed: wordcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# WordCraft Engine

WordCraft Engine runs WordCraft, an open-source word processor, headless in
the person's own space, for you. It is an MCP server named `wordcraft-engine`
(skill 'mcp', "An installed app's own server"): connected when the app was
installed, nothing to approve. One document stays open in the app between
calls, like an editor left open.

## When to use

Letters, reports, plans, CVs and any document the person wants as a Word
file or a PDF: written fresh, from a template, or opened from a file they
gave you.

## First time

1. `mcp_connections(op='tools', name='wordcraft-engine')`: it wakes the app.
   The tools are WordCraft's own (`new_document`, `open_document`,
   `type_text`, `execute`, `batch`, `list_commands`, `select_text`,
   `inspect_document`, `get_text`, `render_page`, `save_document`, …) and the
   app's `file_put`, `file_get`, `file_list`.
2. Write `skills/mcp-wordcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-wordcraft-engine', content:…})`.

## Steps

1. **Start:** `new_document(template: 'blank')` (or `letter`, `resume`,
   `report`, `sample`), or `open_document(path: 'in/<name>.docx')` after
   `file_put`. It opens .docx, .odt, .rtf, .md, .tex, .html, .txt and .json.
2. **Write:** `type_text(text)` types at the caret. Every other action is a
   command run with `execute(command, params)`; `list_commands(query:
   '<word>')` gives the ids and parameters. The ones you need most:
   - new paragraph: `text.newParagraph`
   - headings and body: `para.heading1`, `para.heading2`, `para.normal`, or
     `para.style {style: '<name>'}`
   - bulleted list: `para.bullets`, then `type_text(text: 'one\ntwo\nthree',
     paragraphs: true)` for one item per line; `para.bullets {off: true}`
     ends it. Numbered: `para.numbering`.
   - table: `insert.table {rows: 3, cols: 3}`. Fill a cell by moving the caret
     into it, `caret.set {pos: {story: 'body', path: [<table block>, <row>,
     <col>, 0], off: 0}}`, then `type_text`; rows and columns count from 0
     and `insert.table` answers with the table's block number.
     `table.borders {kind: 'all'}` draws the grid.
   - character formatting: `list_commands(query: 'format')`; `select_text`
     selects the words a command applies to.
   `batch(commands: [{command, params}, …])` runs several in order.
3. **Look:** `render_page(page: 1, scale: 1.5)` returns the page as an image,
   kept in your gallery, and the page count. `inspect_document` lists every
   block with its style, lists and tables.
4. **Save:** `save_document(path: 'out/<name>.docx')` (the folder is made for
   you); the extension picks the
   format: docx, pdf, odt, rtf, html, md, tex, txt or png. Save the Word file
   and the PDF as two calls. `file_get(path)` brings a page picture back.

## Checks

- `get_text` returns the body as plain text: table cells separated by tabs,
  one paragraph per line.
- After saving, `open_document` on the saved file reports its paragraphs and
  words.

## When it fails

- **Text went into the wrong place:** the caret is where the last command
  left it; `inspect_document` shows the selection, and `caret.docEnd` goes
  to the end.

## Reference

WordCraft is MIT or Apache-2.0 (github.com/storytold/wordcraft).
