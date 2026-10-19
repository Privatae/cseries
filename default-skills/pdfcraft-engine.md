---
name: pdfcraft-engine
description: How to make and edit PDFs with the PdfCraft Engine app — creating documents from text or images, adding text, comments, stamps, headers and footers, combining, splitting and extracting pages, forms, redaction, text recognition on scans, previews, and saving. Present when the person has installed PdfCraft Engine.
present_if_app_installed: pdfcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# PdfCraft Engine

PdfCraft Engine runs PdfCraft, an open-source PDF editor, headless in the
person's own space, for you. It is an MCP server named `pdfcraft-engine`
(skill 'mcp', "An installed app's own server"): connected when the app was
installed, nothing to approve. Open documents stay open in the app between
calls, like an editor left open, and edits stay in memory until you save.

## When to use

Anything a person would do in a PDF editor: make a PDF from text or images,
add text, comments, stamps, page numbers, headers, footers and watermarks,
combine files, split them or pull pages out, fill and build forms, redact,
make scanned pages searchable, and export a PDF to Word, HTML or RTF.
To write a document from scratch with styles and tables, use WordCraft Engine
and save it as PDF.

## First time

1. `mcp_connections(op='tools', name='pdfcraft-engine')`: it wakes the app.
   The tools are PdfCraft's own (`doc_open`, `doc_create`, `doc_save`,
   `page_add_text`, `comment_add`, `doc_header_footer`, `doc_combine`,
   `doc_split`, `page_extract`, `render_preview`, `text_extract`, `form_fill`,
   `redact_mark`, `ocr_recognize`, …) and the app's `file_put`, `file_get`,
   `file_list`.
2. Write `skills/mcp-pdfcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-pdfcraft-engine', content:…})`.

## Steps

1. **Open or make a document:** a file the person gave you goes in with
   `file_put(path: 'in/<name>.pdf', data: …)`, then `doc_open(path:
   'in/<name>.pdf')`. Or `doc_create(from: 'blank', pages: 2)`,
   `doc_create(from: 'text', text: '…')` or `doc_create(from: 'images',
   paths: ['in/a.png', …])` (PNG, JPEG, TIFF, GIF or BMP, a page each). Each
   answer gives the document's number in `doc`; every other tool takes it.
2. **Add to it:** `page_add_text(doc, page, text, at: [72, 72], width: 460,
   size: 28, bold: true)` places text in points from the top left of the
   page; it wraps at `width` (default 200), so give the width you want.
   `comment_add(doc, page, type: 'highlight', find: '<text on the page>')`,
   `type: 'note'` with `at` and `contents`, or `type: 'stamp'` with `stamp:
   'draft'` and `at`. `doc_header_footer(doc, footer_center: '<<Page 1 of
   n>>')` numbers the pages.
3. **Pages and files:** `doc_combine(paths: [...], out: 'out/all.pdf')`
   merges files in order. `doc_split(doc, every: 1, out_dir: 'out/parts')`
   writes `<name>-partK.pdf` files. `page_extract`, `page_delete`,
   `page_move`, `page_rotate` and `page_insert_blank` work on one document;
   page numbers start at 1.
4. **Look:** `render_preview(doc, page, max_side: 1024)` returns the page as
   an image, kept in your gallery. `text_extract(doc)` returns the text,
   `comment_list(doc)` the comments, `doc_info(doc)` the summary.
5. **Save:** `doc_save(doc, path: 'out/<name>.pdf')`. A page as a picture
   file: `doc_export_images(doc, folder: 'out/pages', format: 'png', dpi:
   150, pages: [1])`. To Word, HTML or RTF: `doc_export_office(doc, path:
   'out/<name>.docx')`. `file_get(path)` brings a picture back to you.

`command_list` lists every command with its parameters; `command_run` runs
one and `command_batch` several.

## Checks

- Every editing answer shows the document's `pages`, whether it is unsaved
  (`dirty`) and what `edit_undo` would undo.
- `doc_save` answers with the bytes written. Paths in answers are full paths
  inside the app; you always give the short relative one.

## When it fails

- **Text recognition on many pages:** `ocr_recognize` reads about one page
  every 2 to 5 seconds. Over about 150 pages, run it on page ranges with
  `pages`, one call each, and save between them.
- **Printing and digital IDs:** there is no printer here, and no Windows or
  Mac keychain; `doc_print(path: 'out/<name>.pdf')` saves the print-ready PDF
  instead. Signing needs a `.p12` file the person gives you.
- **A path is refused:** paths are relative, inside the app's files, with no
  `..`.

## Reference

PdfCraft is MIT or Apache-2.0 (github.com/storytold/pdfcraft). Text
recognition reads English.
