"""A standalone, script-free render of this host's SHELL — the tile picture.

WHY THIS EXISTS. The Host Manager grid first shipped drawing the desktop
*document* — the artifact `set_desktop` points at. That showed only the bottom
layer: that document is only the wallpaper surface. The taskbar, the
dock, the app icons and the windows all live in the SHELL
(`dashboard/base/index.html`), which renders ON TOP of it. A picture of the
document is a picture of the bottom layer of the machine.

He asked for "a snapshot of the HTML desktop UI so people can visually
differentiate hosts based on the ui" — the machine as it looks. This module
produces that: the real shell markup, the real stylesheets, the host's real
installed apps, over the host's real wallpaper.

TWO PROPERTIES, AND BOTH ARE LOAD-BEARING:

1. **No scripts.** Every `<script>` is stripped and everything the shell's JS
   would have populated is filled in here instead. That is what lets the SAME
   document render in three places: the manager's empty-sandbox iframe (which
   forbids script by design — it is showing another host's document), a
   headless Chromium capturing a PNG, and a plain browser. A document that
   needed JS would render as an empty frame in the one place it matters most.

2. **No user content.** The window layer, the widget layer, the presence line
   and the boot screen are removed, and no conversation, file, project or
   notification is ever read. This is the machine's FACE, not its contents.
   A screenshot of a clean shell was chosen over a live capture for exactly
   that reason, so the cleanliness has to be structural rather than a
   promise about what happens to be open at the time.

WHAT IT IS NOT: a live screenshot. Nothing here observes the running session.
Two hosts with identical apps and identical wallpaper produce identical
documents, which is correct — the tile answers "which machine is this", not
"what is happening on it".
"""
from __future__ import annotations

import html as _html
from base64 import b64encode as _b64encode
import logging
import re
from pathlib import Path

log = logging.getLogger("privatae.pc.shell_snapshot")

DASHBOARD_BASE = Path("/app/dashboard/base")

# The reference stage every tile is scaled from (the dashboard frontend's
# lib/snapshot.ts STAGE_W/STAGE_H). Pinned here so the composed document has
# the same aspect as the frame that will hold it.
STAGE_W = 1440
STAGE_H = 900

# Elements the shell treats as chrome-with-content, removed so a snapshot
# cannot carry a session. `boot-screen` goes because it is an opaque overlay —
# leaving it in produces a tile that is one flat colour.
_STRIP_IDS = (
    "boot-screen",
    "window-layer",     # open app windows == user content
    "widget-layer",     # widget state == user content
    "presence-line",    # live status text
    "dock-backdrop",
    "dock-menu",        # the launcher popup; closed in a clean shell
)

_MAX_BYTES = 256 * 1024   # matches MAX_TILE_HTML on both the bridge and reader

# What the last compose did, in plain words, for the door that set the desktop
# to put in front of the person: {"published", "bytes", "left_out", "note"}.
LAST_TILE: dict = {}

# An inline image or font (src="data:…", url(data:…)). Replaced, largest first,
# by _BLANK_IMG (a 1x1 transparent GIF, valid in <img> and in CSS) when a
# desktop is over the tile limit.
_DATA_URI_RE = re.compile(r"data:[\w.+/-]+(?:;[\w=.+-]+)*,[^\"')\s<>]+", re.I)
_BLANK_IMG = ("data:image/gif;base64,"
              "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _strip_inert(wallpaper: str) -> str:
    """The desktop document without what can never show in a tile: its
    scripts (the tile frame is sandbox="", so none of them runs) and HTML
    comments. The picture is unchanged."""
    out = _strip_scripts(wallpaper)
    return re.sub(r"<!--.*?-->", "", out, flags=re.S)


def _strip_scripts(doc: str) -> str:
    """Remove every <script>, inline or sourced.

    Deliberately not a full parser: index.html is ours and its shape is stable,
    and a regex that removes too much fails VISIBLY (a missing widget) rather
    than silently leaving script in a document we promised has none.
    """
    return re.sub(r"<script\b[^>]*>.*?</script\s*>", "", doc, flags=re.S | re.I)


def _strip_element_by_id(doc: str, el_id: str) -> str:
    """Remove one element and everything inside it, matched on its id.

    Walks nesting depth rather than matching to the first closing tag: a
    `<div id="window-layer">` full of `<div>`s would otherwise lose only its
    head and leave the tail behind, unbalancing the document.
    """
    open_m = re.search(
        rf'<(\w+)\b[^>]*\bid=["\']{re.escape(el_id)}["\'][^>]*>', doc, flags=re.I)
    if not open_m:
        return doc
    tag = open_m.group(1)
    if open_m.group(0).rstrip().endswith("/>"):        # self-closed: nothing inside
        return doc[:open_m.start()] + doc[open_m.end():]

    depth = 1
    pos = open_m.end()
    token = re.compile(rf"<(/?){tag}\b[^>]*>", flags=re.I)
    while depth > 0:
        m = token.search(doc, pos)
        if m is None:                                   # unbalanced: leave it alone
            return doc
        depth += -1 if m.group(1) == "/" else 1
        pos = m.end()
    return doc[:open_m.start()] + doc[pos:]


def _inline_stylesheets(doc: str) -> str:
    """Replace every local <link rel=stylesheet> with the file's contents.

    The document has to be SELF-CONTAINED: it renders from a `data:` URL in the
    capture browser and from `srcdoc` in the manager, and in neither case can a
    relative `/css/...` resolve to this pod. An unresolvable stylesheet is the
    difference between the machine and a pile of unstyled text.
    """
    def repl(m: re.Match) -> str:
        href = m.group(1).split("?", 1)[0]
        if not href.startswith("/css/"):
            return ""          # external or unknown — drop, never a dead link
        try:
            return "<style>" + (DASHBOARD_BASE / href.lstrip("/")).read_text(
                encoding="utf-8") + "</style>"
        except OSError as e:
            log.warning("shell snapshot: stylesheet %s unreadable: %s", href, e)
            return ""

    return re.sub(
        r'<link\b[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\'][^>]*>',
        repl, doc, flags=re.I)


_MAX_ICON_BYTES = 24 * 1024      # per icon; a tile is 40px, nothing needs more
_MAX_ICONS = 8                   # a tile is a thumbnail; more than this wraps
                                 # into a second column and reads as clutter

_ICON_MIME = {".png": "image/png", ".svg": "image/svg+xml",
              ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif"}


def _svg_for_img(raw: bytes) -> bytes | None:
    """Make an SVG loadable as an `<img src="data:image/svg+xml;base64,…">`,
    or None when it has no root element.

    TWO THINGS BREAK AN OTHERWISE-VALID SVG in that position, and the first one
    is the surprise:

    1. **Anything before the root element.** An icon that opens with a licence
       or explanation comment fails to decode — seen with circles/icon.svg,
       which starts `<!-- Circles — the platform's "linked rings" mark…`:
       as-is `naturalWidth` is 0, comment stripped it is 64. It renders fine
       everywhere else, which is exactly why it survived: the shell uses these
       icons INLINE, where the HTML parser is forgiving.
    2. **A missing namespace.** Inline SVG inherits it from the HTML parser; a
       standalone document must declare it.

    A broken-image glyph in a tile looks like a broken feature, so anything
    this cannot repair returns None and the caller draws a lettermark instead.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    m = re.search(r"<svg\b", text, flags=re.I)
    if m is None:
        return None
    text = text[m.start():]                      # drop comments / prolog / BOM
    end = text.find(">")
    if end == -1:
        return None
    if "xmlns" not in text[:end]:
        text = text[:4] + ' xmlns="http://www.w3.org/2000/svg"' + text[4:]
    return text.encode("utf-8")


def _icon_data_uri(icon_path: Path | None) -> str | None:
    """An app icon as a self-contained data: URI, or None.

    THE ICONS MUST BE INLINED. `_serialize` hands out `icon_url` as the
    RELATIVE path `/system-apps/<id>/<file>` (routes/applications.py:70), which
    resolves against whatever origin the document happens to be rendered in —
    and this document is rendered from `srcdoc` in the manager and a `data:`
    URL in the capture browser, where it resolves to nothing. Left as-is every
    icon renders as a broken-image glyph, which looks far worse than no icon.
    A typical Host has dozens of apps, most with a real icon.png or icon.svg.
    """
    if icon_path is None:
        return None
    try:
        mime = _ICON_MIME.get(icon_path.suffix.lower())
        if mime is None:
            return None
        raw = icon_path.read_bytes()
        if len(raw) > _MAX_ICON_BYTES:
            return None          # oversized: fall back to the lettermark
        if mime == "image/svg+xml":
            raw = _svg_for_img(raw)
            if raw is None:
                return None      # unfixable: lettermark beats a broken glyph
        return f"data:{mime};base64," + _b64encode(raw).decode("ascii")
    except OSError:
        return None


def _icon_markup(name: str, data_uri: str | None) -> str:
    """One desktop icon's visual — the inlined image when there is one,
    otherwise a lettermark. The fallback is deliberate: an app with no icon,
    or one too big to inline, must still draw as something rather than as a
    missing-image glyph."""
    if data_uri:
        return (f'<img src="{data_uri}" alt="" '
                'style="width:40px;height:40px;object-fit:contain;border-radius:9px">')
    return ('<span style="display:flex;align-items:center;justify-content:center;'
            'width:40px;height:40px;border-radius:9px;background:var(--cseries-veil);'
            'color:var(--cseries-bright);font:600 18px/1 var(--font-body)">'
            + _html.escape((name or "?")[:1].upper()) + "</span>")


def _desktop_icons_html(icons: list[tuple[str, str, str | None]]) -> str:
    """The icon grid, in the same markup the shell's own seedDesktopIcons emits
    (dock-menu.js:158-163), so the shipped desktop.css lays it out unchanged.
    Inventing markup here would mean maintaining a second layout that drifts
    from the real one the first time either moves.

    Takes (app_id, name, data_uri) triples — already filtered and capped by the
    caller, because "which icons are on this desktop" is the host's layout
    decision and not this renderer's.
    """
    parts = []
    for app_id, name, data_uri in icons:
        parts.append(
            '<button class="desktop-icon" data-app-id="'
            + _html.escape(app_id, quote=True) + '">'
            '<div class="icon-image">' + _icon_markup(name, data_uri) + "</div>"
            '<div class="icon-label">' + _html.escape(name) + "</div>"
            "</button>"
        )
    return "".join(parts)


def _fill_apps_region(wallpaper: str, icons: list[tuple[str, str, str | None]]) -> str:
    """Fill a desktop-mode document's `<div data-cseries-apps>` with the real
    apps, the way the shell's bridge would at runtime.

    A desktop-mode document draws its own tiles and gets the list over the
    MessageChannel bridge (desktop-surface.js). With scripts stripped that
    bridge never runs, so the document renders whatever empty state it wrote —
    on one Host, the words "No apps installed yet" across a machine with 28 apps.
    A tile that misreports the machine is worse than a tile with no icons.

    Styling is inline and minimal on purpose: this is someone else's design and
    we are filling a slot they defined, not restyling their desktop.
    """
    if not wallpaper:
        return wallpaper
    m = re.search(r'<div\b[^>]*\bdata-cseries-apps\b[^>]*>', wallpaper, flags=re.I)
    if m is None:
        return wallpaper
    end = wallpaper.find("</div>", m.end())
    if end == -1:
        return wallpaper
    tiles = []
    for app_id, name, data_uri in icons:
        art = (f'<img src="{data_uri}" alt="" style="width:40px;height:40px;'
               'object-fit:contain;border-radius:9px">') if data_uri else ""
        tiles.append(
            f'<div data-cseries-app="{_html.escape(app_id, quote=True)}" '
            'style="display:flex;flex-direction:column;align-items:center;'
            'gap:8px;width:96px">' + art
            + '<span style="font:600 11px/1.2 system-ui,sans-serif;'
              'letter-spacing:.08em;text-transform:uppercase;opacity:.85;'
              'text-align:center">' + _html.escape(name) + "</span></div>"
        )
    inner = ('<div style="display:flex;flex-wrap:wrap;gap:22px">'
             + "".join(tiles) + "</div>")
    return wallpaper[:m.end()] + inner + wallpaper[end:]


def _fill_by_id(doc: str, el_id: str, inner: str) -> str:
    """Put `inner` inside the element with this id, replacing the placeholder
    the shell shipped for its JS to fill."""
    m = re.search(
        rf'(<(\w+)\b[^>]*\bid=["\']{re.escape(el_id)}["\'][^>]*>)(.*?)(</\2\s*>)',
        doc, flags=re.S | re.I)
    if not m:
        return doc
    return doc[:m.start()] + m.group(1) + inner + m.group(4) + doc[m.end():]


def _assemble(doc: str, icons: list, wallpaper_html: str | None,
              host_name: str | None, entity_id: int) -> str:
    """The tile document from the prepared shell `doc`, these icons and this
    wallpaper. Pure: compose_clean_shell calls it again with less when the
    result is over the tile limit."""
    # WHICH LAYER DRAWS THE ICONS IS THE DOCUMENT'S CALL, not ours.
    # desktop-surface.js:16-19 — a document declaring
    # `<meta name="cseries-desktop" content="desktop">` draws its own tiles and
    # the shell HIDES #desktop-icons; a `wallpaper` document is a backdrop and
    # the shell keeps its column. Ignoring that drew BOTH, and on a desktop-mode
    # clock the shell's column landed straight across the time.
    mode = "wallpaper"
    if wallpaper_html:
        mm = re.search(
            r'<meta[^>]*name=["\']cseries-desktop["\'][^>]*content=["\'](wallpaper|desktop)["\']'
            r'|<meta[^>]*content=["\'](wallpaper|desktop)["\'][^>]*name=["\']cseries-desktop["\']',
            wallpaper_html, flags=re.I)
        if mm:
            mode = mm.group(1) or mm.group(2) or "wallpaper"

    if mode == "desktop":
        # The document owns the icons. Give it the app list it would have got
        # from the shell's bridge — without it the tile shows the document's
        # own empty state ("No apps installed yet"), which is a lie about a
        # machine with 28 apps installed.
        doc = _fill_by_id(doc, "desktop-icons", "")
        wallpaper_html = _fill_apps_region(wallpaper_html, icons)
    else:
        doc = _fill_by_id(doc, "desktop-icons", _desktop_icons_html(icons))

    # index.html ships the literal placeholder "Starting up..." for its JS to
    # replace at runtime. With scripts stripped it would sit there forever — a
    # grid of tiles all announcing they are booting. That is the same defect
    # seen in the live taskbar; it must not be baked into a picture.
    doc = _fill_by_id(doc, "entity-name",
                      _html.escape(host_name or f"Host {entity_id}"))

    # The host's own desktop document, underneath everything, in its own
    # sandboxed frame. srcdoc because this document must stand alone; the EMPTY
    # sandbox because host-authored wallpaper gets no more authority here than
    # it does in the manager's tile.
    wallpaper_css = ""
    if wallpaper_html:
        layer = ('<iframe sandbox="" srcdoc="'
                 + _html.escape(wallpaper_html, quote=True)
                 + '" style="position:fixed;inset:0;width:100%;height:100%;'
                   'border:0;z-index:-1;pointer-events:none" aria-hidden="true"></iframe>')
        doc = doc.replace("<body", "<body", 1)
        # Inserted FIRST in the body so it sits under the shell in paint order.
        m = re.search(r"<body\b[^>]*>", doc, flags=re.I)
        doc = (doc[:m.end()] + layer + doc[m.end():]) if m else doc.replace(
            "</body>", layer + "</body>", 1)
        # THE BUG THIS FIXES: html/body and #desktop carry an OPAQUE background
        # from theme.css/desktop.css (measured: rgb(10,14,15)). A wallpaper
        # frame behind them is invisible, so every tile drew the same default
        # dark surface and the hosts were indistinguishable — which is the one
        # thing the grid exists to do. Make the surfaces transparent and give
        # the shell its own stacking context above the frame.
        #
        # TRANSPARENCY ONLY — do NOT touch position or z-index on the shell's
        # own elements. A first attempt added `#taskbar,#desktop-icons{
        # position:relative;z-index:1}` to force them above the frame; that
        # overrode the taskbar's own `position:fixed;bottom:0` and moved it to
        # the TOP of the tile, and re-flowed the icon grid into overlapping
        # columns. The shell already stacks correctly above a z-index:-1 child
        # once nothing paints an opaque background over it.
        wallpaper_css = (
            "html,body{background:transparent!important}"
            "#desktop{background:transparent!important}"
        )

    # Pin the stage to the aspect the tile scales from, and kill motion — a
    # snapshot must not animate in a grid of twelve.
    doc = doc.replace(
        "</head>",
        f"<style>html,body{{width:{STAGE_W}px;height:{STAGE_H}px;"
        "overflow:hidden;margin:0}"
        "*,*::before,*::after{animation:none!important;transition:none!important}"
        + wallpaper_css +
        "</style></head>", 1)

    return doc


def _prepare_shell() -> str:
    """The shell's index.html, stylesheets inlined, scripts and the live-only
    elements stripped. File reads and regex work: run off the event loop."""
    doc = (DASHBOARD_BASE / "index.html").read_text(encoding="utf-8")
    doc = _inline_stylesheets(doc)
    doc = _strip_scripts(doc)
    for el_id in _STRIP_IDS:
        doc = _strip_element_by_id(doc, el_id)
    return doc


def _fits(cand: str) -> bool:
    return len(cand.encode("utf-8")) <= _MAX_BYTES


def _reduce_to_tile(doc: str, icons: list, wallpaper_html: str | None,
                    host_name: str | None, entity_id: int) -> tuple[str | None, str | None]:
    """(the tile document, what it left out) — or (None, None) when even the
    bare shell is over the tile limit. At most six assemblies whatever the
    desktop holds: the inline images are dropped LARGEST FIRST by arithmetic
    (each one's escaped length is exactly what removing it saves, since a data
    URI is ASCII), then one assembly checks the result, and only if that
    projection fell short are the rest dropped in one more."""
    plain = _strip_inert(wallpaper_html) if wallpaper_html else wallpaper_html
    cand = _assemble(doc, icons, wallpaper_html, host_name, entity_id)
    if _fits(cand):
        return cand, None
    cand = _assemble(doc, icons, plain, host_name, entity_id)
    if _fits(cand):
        return cand, None
    wp = plain
    if plain:
        over = len(cand.encode("utf-8")) - _MAX_BYTES
        blank = len(_html.escape(_BLANK_IMG, quote=True))
        uris = []
        for uri in set(_DATA_URI_RE.findall(plain)):
            saving = (len(_html.escape(uri, quote=True)) - blank) * plain.count(uri)
            if saving > 0:
                uris.append((saving, uri))
        uris.sort(key=lambda t: t[0], reverse=True)
        chosen, saved = [], 0
        for saving, uri in uris:
            if saved >= over:
                break
            chosen.append(uri)
            saved += saving
        if chosen:
            for uri in chosen:
                wp = wp.replace(uri, _BLANK_IMG)
            cand = _assemble(doc, icons, wp, host_name, entity_id)
            if _fits(cand):
                return cand, f"{len(chosen)} of the desktop's inline image(s), the largest"
            rest = [u for _s, u in uris if u not in chosen]
            if rest:
                for uri in rest:
                    wp = wp.replace(uri, _BLANK_IMG)
                cand = _assemble(doc, icons, wp, host_name, entity_id)
                if _fits(cand):
                    return cand, f"all {len(uris)} of the desktop's inline images"
        lettered = [(a, n, None) for a, n, _u in icons]
        cand = _assemble(doc, lettered, wp, host_name, entity_id)
        if _fits(cand):
            return cand, "every inline image, the app icons included"
    if wallpaper_html:
        cand = _assemble(doc, [(a, n, None) for a, n, _u in icons], None,
                         host_name, entity_id)
        if _fits(cand):
            return cand, "the desktop document itself (the tile shows the shell alone)"
    return None, None


async def compose_clean_shell(pool, entity_id: int, *,
                              host_name: str | None = None,
                              wallpaper_html: str | None = None) -> str | None:
    """Render this host's shell as one self-contained, script-free document.

    Over the tile limit, the desktop's inert parts, then its largest inline
    images, then every inline image, then the desktop document itself are
    left out, in that order, until it fits; LAST_TILE says what was. Returns
    None only when the shell cannot be read or even the bare shell is over —
    the caller then publishes nothing rather than a broken picture, and the
    manager keeps the host's stable fallback mark.
    """
    import asyncio
    try:
        doc = await asyncio.to_thread(_prepare_shell)
    except OSError as e:
        log.warning("shell snapshot: index.html unreadable: %s", e)
        return None

    # The host's real installed apps. A failure here costs the ICONS, not the
    # picture — a shell with this host's taskbar and wallpaper is still
    # recognisably this machine, so it is worth publishing without them.
    icons: list[tuple[str, str, str | None]] = []
    try:
        from server.applications import registry
        from server.applications.manifest import load as load_manifest
        from server.routes.applications import _serialize
        for app in await registry.list_active(pool):
            if _serialize(app).get("operator_only"):
                continue          # operator apps are on nobody's desktop
            name, icon_path = app.id, None
            try:
                mf = load_manifest(Path(app.source_dir) / "app.yaml")
                name = mf.name or app.id
                if mf.icon:
                    icon_path = Path(app.source_dir) / mf.icon
            except Exception:
                pass              # no manifest -> lettermark, still an icon
            icons.append((app.id, name, _icon_data_uri(icon_path)))
            if len(icons) >= _MAX_ICONS:
                break
    except Exception as e:
        log.warning("shell snapshot: app list unavailable: %s", e)
    # THE TILE LIMIT IS THE PLATFORM'S (the bridge's desktop.publish and the
    # manager's reader both take 256 KB), so a desktop that is over it is
    # brought under it rather than left without a tile: a rich desktop
    # inlines its images, fonts and scripts, and used to get NO tile, with
    # only a log line. Each step loses less than the next, and the tile says
    # what it left out (LAST_TILE, read by the set door into the answer the
    # person sees). The reduction is string work on a document of any size,
    # so it runs off the event loop, and it assembles a bounded number of
    # candidates whatever the number of images.
    global LAST_TILE
    doc_out, left_out = await asyncio.to_thread(
        _reduce_to_tile, doc, icons, wallpaper_html, host_name, entity_id)
    if doc_out is None:
        LAST_TILE = {"published": False, "bytes": None,
                     "note": (f"no tile: this desktop is over the {_MAX_BYTES // 1024} KB "
                              "tile limit even without its images, so the Host Manager "
                              "shows this host's mark instead")}
        log.warning("shell snapshot: over the %d tile cap even reduced — not published",
                    _MAX_BYTES)
        return None
    size = len(doc_out.encode("utf-8"))
    LAST_TILE = {"published": True, "bytes": size, "left_out": left_out,
                 "note": (f"the Host Manager tile left out {left_out} to fit its "
                          f"{_MAX_BYTES // 1024} KB limit; the desktop itself is unchanged")
                 if left_out else None}
    if left_out:
        log.info("shell snapshot: %s", LAST_TILE["note"])
    return doc_out
