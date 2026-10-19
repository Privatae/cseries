---
name: creative-engines
description: Which creative engine app does which work — 3D, photos, photo development, vector art, page layout, motion graphics, video, sound, technical drawings, documents, spreadsheets, slides and PDFs — installing the one a piece of work needs from the App Store, and handing work from one engine to another.
allowed-tools: list_app_catalog, purchase_app, install_app, mcp_connections, operate, context, artifact
---

# Creative engines

The creative engines are store apps that each run one open-source program
headless in the person's own space, for you to operate through the app's own
MCP server (skill 'mcp', "An installed app's own server"). Each has its own
skill, present once the app is installed, that says how to use it.

## When to use

The person wants something made: a picture, a logo, a wallpaper, a page, a
video, a song, a drawing, a document, a sheet, a deck or a PDF. Pick the
engine for the work, and install it if it is not there yet.

| The work | The engine (app id) |
|---|---|
| 3D scenes, rendered wallpapers and artwork | Blender Engine (`blender-engine`) |
| Editing and compositing images, layers, retouching, PSD | PhotoCraft Engine (`photocraft-engine`) |
| Developing photos: light, colour, presets, crops, raw files | LightCraft Engine (`lightcraft-engine`) |
| Logos, icons, posters, illustrations, SVG | VectorCraft Engine (`vectorcraft-engine`) |
| Flyers, magazines, books, print-ready PDF, IDML | DesignCraft Engine (`designcraft-engine`) |
| Animated titles, motion graphics, effects, Lottie | EffectCraft Engine (`effectcraft-engine`) |
| Cutting and assembling video | FilmCraft Engine (`filmcraft-engine`) |
| Music, sound, mixing, bouncing audio | SoundCraft Engine (`soundcraft-engine`) |
| Plans and parts drawn to measure, DXF and DWG | CADCraft Engine (`cadcraft-engine`) |
| Letters, reports and other documents, DOCX | WordCraft Engine (`wordcraft-engine`) |
| Spreadsheets, formulas, charts, XLSX and CSV | GridCraft Engine (`gridcraft-engine`) |
| Slide decks, PPTX | DeckCraft Engine (`deckcraft-engine`) |
| Combining, splitting, marking up and reading PDFs | PdfCraft Engine (`pdfcraft-engine`) |

## Steps

1. **Is it installed?** `list_app_catalog` shows each engine with its install
   state and price.
2. **Install it:** tell the person which engine and what for. They click
   Install in the App Store, or you call `purchase_app` (a free app's
   entitlement) and then `install_app`, each through `operate` with
   `{app_id}` (skill 'apps'). Installing
   connects its MCP server to you, nothing to approve.
3. **Use it** as its own skill says: read its tools once with
   `mcp_connections(op='tools', name='<app id>')`, write
   `skills/mcp-<app id>` yourself (`context` action='write', through
   `operate`), and call it through `operate`.
4. **Hand work from one engine to another.** An image comes back to your
   gallery as `/api/images/…`; any other file stays in the engine that made
   it and is named `app://<app id>/files/<path>`. Either name, as
   `file_put`'s `data` in the next engine, puts the file there. For
   example: a Blender render composited in PhotoCraft and worn with
   `artifact` op='set_wallpaper'; a SoundCraft bounce under a FilmCraft cut
   with EffectCraft titles; a VectorCraft logo in a DeckCraft deck; a
   WordCraft report combined with others in PdfCraft.

## Checks

- `mcp_connections(op='servers')` lists the installed engines, each with
  `app` set.
- Each engine's window shows the person the files made there, to open or
  download.

## When it fails

- **The engine is not in the catalog:** it has not been published to this
  store yet; say so, and make the thing with what is installed.
- **A handed-over file is refused:** the name must be exactly
  `app://<app id>/files/<path>` of an app the person has installed, with the
  path the first engine's answer gave. One call carries up to 128 MB of
  images and files.
