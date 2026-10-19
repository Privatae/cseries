"""Pictures for the messaging connectors: a page the Host wrote, drawn to a PNG
by Takumi (through its Python binding, pytakumi) in a confined child process,
or refused so the caller sends a link.

The browser sidecar has no part in this, not even as
a fallback: nothing here imports or calls it.

What a caller gets is a `Picture`: PNG bytes, or None with the reason. None is
an ordinary answer, not an error: the caller sends a link instead.

Takumi draws what it understands and passes over the rest without a word,
the way a browser does, and it has no warning channel at all. So the gate
has two lines:

1. Before anything is drawn, the page is parsed here and checked against
   allowlists that were MEASURED in the kernel image, tag by tag, property by
   property and value by value (the measurement and its method are below,
   next to the lists; scripts/test_connector_picture_image.py measures every
   listed entry again with Takumi on each build). What Takumi is handed is
   re-serialised from that parse, so it never sees markup this module did
   not check.
2. In the child, before and after the render: the page names no font but the
   shipped one, every character of its text has a glyph in that font (a
   missing glyph draws as an empty box with no warning at all), no Python
   warning was raised, and the image is not a single colour.

The render itself runs in a child process (_picture_child.py) with a time
limit, a memory limit and output limits, and it is the first thing the
out-of-memory killer takes. It forbids itself every write, every exec, every
new process, every socket and every signal outside itself before it imports
anything, imports pytakumi, and then limits its reads to the interpreter's
directories (where pytakumi lives) and the shipped font's directory before it
reads the page. Both steps are Landlock, applied by the child to itself;
without Landlock ABI 6 it refuses to draw at all. A crash in native drawing
code kills the child, never the kernel process, and the kernel only ever
signals the one child it started, through the handle it holds.

Nothing is saved. A picture is made for one send and returned inline; no row
and no file is written anywhere (a visual is never saved
unless the person clicks).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from typing import Mapping

log = logging.getLogger("privatae.connectors.picture")

# The ONE font a picture is drawn in: Inter, the desktop's own font
# (dashboard/base/css/tokens.css: --font-body is "Inter", one upright variable
# face). The file is InterVariable.ttf from the Inter 4.1 release, which
# Dockerfile.linux installs (_picture_child.FONT_FILE), so the kernel image
# carries no font package and no fontconfig for this. It is the root
# font-family of every picture, every var(--font-…) resolves to it, and it is
# the only font-family a page may name; check_page writes it exactly as
# SHIPPED_FONT, the child registers the file under that one name, and the
# child refuses a page that names anything else (_picture_child.other_fonts).
# Takumi loads fonts only from the bytes it is handed, never from a path or
# from the system, so a font-family can never name a file.
SHIPPED_FONT = "Inter"

_CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_picture_child.py")

# Limits. A small card renders in under a millisecond, and a whole draw,
# from starting the child (importing pytakumi, reading the font, drawing the
# warm-up card) to the PNG, takes about 40 ms (measured in the image); the
# time limit covers a loaded node.
TIME_LIMIT_S = 20.0
CPU_LIMIT_S = 10
# Address space, not resident memory. Measured in the image with one drawing
# thread (DRAW_THREADS): a 2048 x 4040 picture, the largest shape under
# MAX_SIDE_PX, peaks at 209852 kB of address space (VmPeak) and 129332 kB
# resident (VmHWM), and draws in about 210 ms. 1 GiB leaves room and still
# stops a runaway.
MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
MAX_PAGE_CHARS = 128 * 1024
# How deep elements may nest. The checks and the serialiser here walk the tree
# recursively; 3000 nested divs (a 33 KB page) raised RecursionError out of
# draw_picture (measured). Takumi's HTML reader walks it recursively too, so
# the child hands it an explicit cap (CHILD_MAX_DEPTH) as pytakumi's own
# notes ask, rather than its default of 512. A card nests a handful of
# levels; a page deeper than this goes as a link.
MAX_DEPTH = 64
# The child's cap: the page's own levels plus the wrapper, with room to spare.
CHILD_MAX_DEPTH = MAX_DEPTH + 8
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_SIDE_PX = 4096
MIN_WIDTH, MAX_WIDTH = 120, 2048
# At most this many children at once; a burst queues instead of forking a
# child per message.
MAX_CONCURRENT = 2
# Takumi's drawing threads per child: the Rayon pool pytakumi starts when it
# is imported, one thread per CPU unless RAYON_NUM_THREADS says otherwise.
# Measured on a 12-CPU machine: one thread, two, or twelve drew the largest
# picture in the same ~210 ms, and each thread costs an 8 MiB stack and its
# own allocator arena of address space (VmPeak 209852 kB with one, 300084 kB
# with twelve), so one is enough and keeps a big node's child far from its
# limit.
DRAW_THREADS = 1

# Where the Host keeps its data in the kernel container (Dockerfile.linux: the
# /app tree and its mounted volumes, /var/lib/cseries for apps and app data, the
# privatae home, the Postgres socket directory, /etc/privatae certs, /etc/cseries
# keys) plus the scratch and runtime trees. The child refuses to draw if any
# path it was going to allow for reading is one of these, or lies above or
# below one.
HOST_DATA_PATHS = (
    "/app", "/var/lib/cseries", "/home", "/root", "/var/run", "/run",
    "/etc/privatae", "/etc/cseries", "/tmp", "/dev/shm", "/var/tmp", "/proc", "/sys",
)


@dataclass(frozen=True)
class Picture:
    png: bytes | None  # None = no picture; the caller sends a link instead
    reason: str        # why it was refused; "" when drawn


# ── Token values ───────────────────────────────────────────────────────────

# The three token families a Host writes in a visual (default-skills/
# artifacts.md "Every artifact wears the desktop"): --cseries-*, --font-*,
# --radius-*.
_TOKEN_PREFIXES = ("--cseries-", "--font-", "--radius-")
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_ROOT_BLOCK_RE = re.compile(r":root\s*\{([^{}]*)\}")


def _is_font_token(name: str) -> bool:
    return name.startswith("--font-") or name.startswith("--cseries-font-")


def tokens_from_sheet(sheet: str) -> dict[str, str]:
    """Every --cseries-*, --font-* and --radius-* value a token sheet declares
    in its :root blocks, later blocks winning (the live desktop's override is
    appended after the stock block, server/routes/appearance.py token_sheet),
    with the sheet's own var() references resolved. A value that still
    names a variable after that is left out, so a page using it is refused."""
    raw: dict[str, str] = {}
    for block in _ROOT_BLOCK_RE.findall(_COMMENT_RE.sub("", sheet or "")):
        for decl in _split_top(block, ";"):
            if ":" not in decl:
                continue
            name, value = decl.split(":", 1)
            name = name.strip()
            if name.startswith("--"):
                raw[name] = " ".join(value.split())
    out: dict[str, str] = {}
    for name in raw:
        if not name.startswith(_TOKEN_PREFIXES):
            continue
        value = _resolve_vars(raw[name], raw, depth=0, fonts_shipped=False)
        if "var(" not in value:
            out[name] = value
    return out


async def desktop_tokens() -> dict[str, str]:
    """The current desktop's token values, read from the same sheet the
    Artifacts window wears (server/routes/appearance.py token_sheet, used by
    server/routes/artifacts.py _wearing_desktop). Async because that sheet
    includes the live desktop's override, which is read from the database."""
    from server.routes.appearance import token_sheet
    return tokens_from_sheet(await token_sheet())


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` outside parentheses and quotes."""
    parts, depth, quote, cur = [], 0, "", []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def _resolve_vars(text: str, tokens: Mapping[str, str], *, depth: int, fonts_shipped: bool) -> str:
    """Replace var(--name[, fallback]) for the three token families. A font
    token becomes the shipped family when `fonts_shipped`; a name the tokens
    do not define takes its fallback; anything else is left as written (and
    then refused by the check)."""
    if depth > 8 or "var(" not in text:
        return text
    out, i = [], 0
    while True:
        j = text.find("var(", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        # Find the matching close paren.
        k, level = j + 4, 1
        while k < len(text) and level:
            if text[k] == "(":
                level += 1
            elif text[k] == ")":
                level -= 1
            k += 1
        if level:  # unbalanced: leave the rest as written
            out.append(text[j:])
            break
        inner = text[j + 4:k - 1]
        name, _, fallback = inner.partition(",")
        name = name.strip()
        rep = None
        if name.startswith(_TOKEN_PREFIXES) or name.startswith("--cseries-font-"):
            if fonts_shipped and _is_font_token(name):
                rep = f'"{SHIPPED_FONT}"'
            elif name in tokens:
                rep = _resolve_vars(tokens[name], tokens, depth=depth + 1, fonts_shipped=fonts_shipped)
            elif fallback.strip():
                rep = _resolve_vars(fallback.strip(), tokens, depth=depth + 1, fonts_shipped=fonts_shipped)
        out.append(rep if rep is not None else text[j:k])
        i = k
    return "".join(out)


def resolve_tokens(css: str, tokens: Mapping[str, str]) -> str:
    """The page's CSS with every var(--cseries-…), var(--font-…) and
    var(--radius-…) replaced by the desktop's value; var(--font-…) is always
    the shipped family, never a family the pod may not have (4.6)."""
    return _resolve_vars(css, tokens, depth=0, fonts_shipped=True)


# ── The allowlists, as measured ────────────────────────────────────────────
#
# MEASURED in the kernel image with Takumi (pytakumi 0.1.5: Takumi 2.14.0;
# Inter 4.1) by drawing a baseline card and the same card with one candidate
# added. A candidate counts as drawn only with no exception and CHANGED
# pixels, and where the candidate moves or sizes a box, the boxes were also
# checked against where a browser puts them. scripts/
# test_connector_picture_image.py measures every entry again on each build.
# Takumi has no warning channel: what it does not understand it passes over
# without a word, as a browser does. So these lists are the whole of what
# keeps a picture to what was measured.
#
# The lists were first measured against html2pic (0.2.2, drawing with Skia),
# which drew much less than Takumi does, and were carried over as they
# stood when the renderer changed: every entry still draws with Takumi, so
# none was dropped, and nothing was added beyond what the change needed. The
# notes say what is off the lists and what Takumi does with it (measured
# 2026-10-19); off the list stays off until it is measured here.
#
# Tags. Takumi gives tags the browser's defaults (Chromium's user-agent
# styles, takumi-html DEFAULT_PRESETS): h1-h6 larger and bold with their
# margins, p with its margins, strong and b one step bolder than their parent
# (measured, against the same tags styled by hand). So:
#   - div, p, span are allowed as the containers they are;
#   - h1-h6, strong, b are allowed and drawn with those defaults;
#   - text and inline tags side by side flow on one line, as in a browser
#     (measured: "Total: <b>$42</b> today" in a <p> is one line, and two
#     spans side by side draw exactly like their text run together);
#   - em and i are not on the list. Takumi slants the shipped font, which
#     has no italic, as a browser does (measured), but they were not on it;
#   - ul, ol, li, br, hr, table, tr, td, th are not on the list. Takumi lays
#     each out with its browser defaults (bullets, numbers, the break, the
#     rule, table cells), but none was on it; svg and canvas are not either
#     (Takumi draws an inline SVG's shapes).
BLOCK_TAGS = frozenset({"div", "p", "h1", "h2", "h3", "h4", "h5", "h6"})
INLINE_TAGS = frozenset({"span", "strong", "b"})
ALLOWED_TAGS = BLOCK_TAGS | INLINE_TAGS
# Structure a page arrives in, dropped rather than drawn: a browser draws none
# of it either. <title>'s text is dropped with it. An attribute on <html>,
# <head> or <body> would style or lay out the page in a browser (style=,
# class=, dir=, bgcolor=, background=, …) and would be dropped here with no
# warning, so only <html lang> passes; <meta> and <title> draw nothing in a
# browser either, whatever they carry.
_STRUCTURE_TAGS = frozenset({"html", "head", "body", "meta", "title"})
_STRUCTURE_ATTRS = {"html": frozenset({"lang"}), "head": frozenset(), "body": frozenset()}
_ALLOWED_ATTRS = frozenset({"class", "id"})

# The wrapper the page is drawn in: a tag a page may not use, so no rule of
# the page's can reach it. It wears the desktop the way an artifact does
# (server/applications/artifact_style.py _BASE_CSS: the desktop's background
# and text colour), sets the shipped font, and fixes the width.
_ROOT_TAG = "section"

# Takumi sizes every box border-box, where a browser's default is
# content-box (measured: width:200px with padding:10px drew 200 px wide, where
# a browser draws 220). So every tag a page may use is put back to
# content-box, and the wrapper alone stays border-box: it plays the body,
# which fills the caller's width with any padding the page gives it inside
# that width, as a browser's body fills the viewport. box-sizing itself is
# off the list, so no rule of the page's can change this.
_BOX_SHEET = (f"{_ROOT_TAG}{{box-sizing:border-box}}"
              + ",".join(sorted(ALLOWED_TAGS)) + "{box-sizing:content-box}")

_LEN = r"(?:0|\d+(?:\.\d+)?(?:px|em))"
_LEN_PCT = r"(?:0|\d+(?:\.\d+)?(?:px|em|%))"
_SIGNED_LEN = r"(?:-?" + _LEN + r")"
_NUM = r"\d+(?:\.\d+)?"
_HEX = r"#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})"
_RGB = r"rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*(?:0|1|0?\.\d+)\s*)?\)"
# Named colours: the 148 CSS Color 4 keywords and transparent, nothing else.
# Takumi draws every one of them as its sRGB value (measured: each, against
# Pillow's table of the same names), and passes over a name it does not know
# with no warning, so an unknown name is refused here.
NAMED_COLOURS = frozenset({
    "aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige", "bisque", "black",
    "blanchedalmond", "blue", "blueviolet", "brown", "burlywood", "cadetblue", "chartreuse",
    "chocolate", "coral", "cornflowerblue", "cornsilk", "crimson", "cyan", "darkblue",
    "darkcyan", "darkgoldenrod", "darkgray", "darkgreen", "darkgrey", "darkkhaki",
    "darkmagenta", "darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon",
    "darkseagreen", "darkslateblue", "darkslategray", "darkslategrey", "darkturquoise",
    "darkviolet", "deeppink", "deepskyblue", "dimgray", "dimgrey", "dodgerblue", "firebrick",
    "floralwhite", "forestgreen", "fuchsia", "gainsboro", "ghostwhite", "gold", "goldenrod",
    "gray", "green", "greenyellow", "grey", "honeydew", "hotpink", "indianred", "indigo",
    "ivory", "khaki", "lavender", "lavenderblush", "lawngreen", "lemonchiffon", "lightblue",
    "lightcoral", "lightcyan", "lightgoldenrodyellow", "lightgray", "lightgreen", "lightgrey",
    "lightpink", "lightsalmon", "lightseagreen", "lightskyblue", "lightslategray",
    "lightslategrey", "lightsteelblue", "lightyellow", "lime", "limegreen", "linen", "magenta",
    "maroon", "mediumaquamarine", "mediumblue", "mediumorchid", "mediumpurple",
    "mediumseagreen", "mediumslateblue", "mediumspringgreen", "mediumturquoise",
    "mediumvioletred", "midnightblue", "mintcream", "mistyrose", "moccasin", "navajowhite",
    "navy", "oldlace", "olive", "olivedrab", "orange", "orangered", "orchid", "palegoldenrod",
    "palegreen", "paleturquoise", "palevioletred", "papayawhip", "peachpuff", "peru", "pink",
    "plum", "powderblue", "purple", "rebeccapurple", "red", "rosybrown", "royalblue",
    "saddlebrown", "salmon", "sandybrown", "seagreen", "seashell", "sienna", "silver",
    "skyblue", "slateblue", "slategray", "slategrey", "snow", "springgreen", "steelblue",
    "tan", "teal", "thistle", "tomato", "turquoise", "violet", "wheat", "white", "whitesmoke",
    "yellow", "yellowgreen",
})


def _one_of(*words: str) -> str:
    return "(?:" + "|".join(re.escape(w) for w in words) + ")"


# Longest first, so a name is never matched by a shorter name it starts with.
_NAMED = _one_of(*sorted(NAMED_COLOURS | {"transparent"}, key=lambda w: (-len(w), w)))
_COLOR = rf"(?:{_HEX}|{_RGB}|{_NAMED})"
# Gradient stops are hex or named; rgb()/rgba() stops are off the list
# (html2pic warned on them; Takumi draws them).
_STOP = rf"(?:{_HEX}|{_NAMED})(?:\s+\d{{1,3}}%)?"
_SIDE = r"(?:top|bottom|left|right)"
_GRADIENT = (rf"linear-gradient\(\s*(?:(?:\d{{1,3}}deg|to\s+{_SIDE}(?:\s+{_SIDE})?)\s*,\s*)?"
             rf"{_STOP}(?:\s*,\s*{_STOP})+\s*\)")
_SHADOW = rf"{_SIGNED_LEN}\s+{_SIGNED_LEN}(?:\s+{_LEN})?\s+{_COLOR}"


def _repeat(unit: str, lo: int, hi: int) -> str:
    return rf"{unit}(?:\s+{unit}){{{lo - 1},{hi - 1}}}"


# property -> the full value grammar that was measured to draw. The notes
# name what is off the list for that property and what Takumi does with it.
ALLOWED_PROPERTIES: dict[str, str] = {
    # Off the list: block, inline, inline-block and inline-flex (Takumi lays
    # each out as a browser does) and grid.
    "display": _one_of("flex", "none"),
    # Off the list: row-reverse (Takumi reverses the row).
    "flex-direction": _one_of("row", "column", "column-reverse"),
    # Off the list: start, left and right (Takumi draws them as flex-start).
    "justify-content": _one_of("flex-start", "flex-end", "center", "space-between",
                               "space-around", "space-evenly"),
    # Off the list: baseline (Takumi lines the baselines up).
    "align-items": _one_of("stretch", "flex-start", "flex-end", "center"),
    "align-self": _one_of("auto", "stretch", "flex-start", "flex-end", "center"),
    "flex-wrap": _one_of("nowrap", "wrap", "wrap-reverse"),
    # One length. Off the list: a row and a column gap (Takumi draws both).
    "gap": _LEN,
    # Off the list: the flex shorthand (Takumi draws it).
    "flex-grow": _NUM,
    "flex-shrink": _NUM,
    # Off the list: fit-content, max-content, calc() and rem (Takumi draws
    # each).
    "width": rf"(?:auto|{_LEN_PCT})",
    "height": rf"(?:auto|{_LEN_PCT})",
    "min-width": _LEN_PCT,
    "max-width": _LEN_PCT,
    "min-height": _LEN_PCT,
    "max-height": _LEN_PCT,
    "aspect-ratio": _NUM,
    "padding": _repeat(_LEN, 1, 4),
    "padding-top": _LEN,
    "padding-right": _LEN,
    "padding-bottom": _LEN,
    "padding-left": _LEN,
    # Off the list: negative and % margins, and auto (Takumi draws each;
    # auto centres a box of fixed width).
    "margin": _repeat(_LEN, 1, 4),
    "margin-top": _LEN,
    "margin-right": _LEN,
    "margin-bottom": _LEN,
    "margin-left": _LEN,
    # As in a browser, border-width and border-color draw only with a
    # border-style (the default style is none), and the default width is
    # medium, 3px (measured). Off the list: the border-top/-right/…
    # shorthands and the styles double, groove, hidden and none (Takumi draws
    # each).
    "border": rf"{_LEN}\s+{_one_of('solid', 'dashed', 'dotted')}\s+{_COLOR}",
    "border-width": _LEN,
    "border-style": _one_of("solid", "dashed", "dotted"),
    "border-color": _COLOR,
    # Off the list: 50% and two values (Takumi draws both).
    "border-radius": _LEN,
    "border-top-left-radius": _LEN,
    "border-top-right-radius": _LEN,
    "border-bottom-left-radius": _LEN,
    "border-bottom-right-radius": _LEN,
    # Off the list: the background shorthand, hsl()/hsla() and currentColor
    # (Takumi draws each).
    "background-color": _COLOR,
    # Off the list: radial- and repeating-linear-gradient (Takumi draws both).
    "background-image": _GRADIENT,
    # Off the list: a fourth (spread) length and inset (Takumi draws both).
    "box-shadow": rf"{_SHADOW}(?:\s*,\s*{_SHADOW})*",
    "text-shadow": _SHADOW,
    "color": _COLOR,
    # Written by check_page as exactly "<SHIPPED_FONT>", whichever spelling
    # passed here: the one name the child registers the font under, and the
    # only one it draws (_picture_child.other_fonts).
    "font-family": rf"(?:\"{re.escape(SHIPPED_FONT)}\"|'{re.escape(SHIPPED_FONT)}'|{re.escape(SHIPPED_FONT)})",
    # Off the list: the keywords (large, x-large, …; Takumi draws them).
    "font-size": _LEN_PCT,
    # Inter's wght axis draws every weight from 100 to 900 (measured). Off the
    # list: bolder and lighter (Takumi steps from the parent's weight as a
    # browser does; html2pic did not).
    "font-weight": _one_of("normal", "bold",
                           "100", "200", "300", "400", "500", "600", "700", "800", "900"),
    # font-style is off the list (see em and i in the tag notes above).
    # Off the list: justify, start and end.
    "text-align": _one_of("left", "right", "center"),
    "text-wrap": _one_of("wrap", "nowrap"),
}
# Off the list, beyond the notes above: line-height, text-decoration,
# background-size, transform, opacity, letter-spacing, text-transform,
# overflow, white-space and position/top/left/right/bottom (Takumi draws each
# of them: measured, the picture changes) and the font shorthand (Takumi
# passes over it).
_PROPERTY_RE = {p: re.compile(rf"^(?:{v})$", re.I) for p, v in ALLOWED_PROPERTIES.items()}

# Selectors: one tag, one .class or one #id, the shapes the lists were
# measured with. Takumi's selector engine (Servo's selectors crate) also
# matches descendant, child, compound, attribute and pseudo-class selectors
# (measured), but they are off the list. The universal selector is refused
# because it would reach the wrapper.
_CLASS_NAME = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
_SELECTOR_RE = re.compile(rf"^(?:[a-z][a-z0-9]*|\.{_CLASS_NAME}|#{_CLASS_NAME})$")
_CLASS_ATTR_RE = re.compile(rf"^\s*{_CLASS_NAME}(?:\s+{_CLASS_NAME})*\s*$")
_ID_ATTR_RE = re.compile(rf"^{_CLASS_NAME}$")
# What a checked value may contain at all. No quotes outside font-family, no
# escapes, no braces, no semicolons, no "!" (so no !important).
_VALUE_CHARS_RE = re.compile(r"^[A-Za-z0-9#%.,()\s\"'-]*$")


class Refused(Exception):
    """The page is not one the renderer can be trusted to draw."""


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list  # of _Node | str


class _PageParser(HTMLParser):
    """Parse the page into a tree of allowed elements and text, pulling the
    <style> blocks out as the CSS argument and dropping the structure tags.
    Raises Refused at the first thing that is not allowed."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(_ROOT_TAG, {}, [])
        self.stack: list[_Node] = [self.root]
        self.css: list[str] = []
        self._in_style = False
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if self._in_title:
            raise Refused(f"<{tag}> inside <title>")
        if tag == "style":
            for k, v in attrs:
                if not (k == "type" and (v or "").strip().lower() == "text/css"):
                    raise Refused(f"<style {k}> (a style block with attributes) is not drawn")
            self._in_style = True
            self.css.append("")
            return
        if tag in _STRUCTURE_ATTRS:
            for k, _ in attrs:
                if k not in _STRUCTURE_ATTRS[tag]:
                    raise Refused(f"the {k}= attribute on <{tag}> would change the page "
                                  "and is not drawn")
            return
        if tag == "meta":
            return
        if tag == "title":
            self._in_title = True
            return
        if tag not in ALLOWED_TAGS:
            raise Refused(f"<{tag}> is not drawn")
        if len(self.stack) > MAX_DEPTH:
            raise Refused(f"elements nest deeper than {MAX_DEPTH} levels")
        node = _Node(tag, {}, [])
        for k, v in attrs:
            if k not in _ALLOWED_ATTRS:
                raise Refused(f"the {k}= attribute on <{tag}> is not drawn")
            v = v or ""
            if k == "class" and not _CLASS_ATTR_RE.match(v):
                raise Refused(f"class={v!r} is not a plain class list")
            if k == "id" and not _ID_ATTR_RE.match(v):
                raise Refused(f"id={v!r} is not a plain id")
            node.attrs[k] = " ".join(v.split())
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if tag in _STRUCTURE_TAGS:
            if tag in _STRUCTURE_ATTRS:
                self.handle_starttag(tag, attrs)  # the same attribute check
            return
        if tag == "style":  # <style/> carries no CSS; the parser never enters it
            self.handle_starttag(tag, attrs)
            self._in_style = False
            return
        self.handle_starttag(tag, attrs)
        if tag in ALLOWED_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in ("html", "head", "body", "meta"):
            return
        if len(self.stack) < 2 or self.stack[-1].tag != tag:
            raise Refused(f"</{tag}> does not close the open element; the markup is not well formed")
        self.stack.pop()

    def handle_data(self, data):
        if self._in_style:
            self.css[-1] += data
            return
        if self._in_title:
            return
        if data.strip():
            self.stack[-1].children.append(" ".join(data.split()))

    def handle_comment(self, data):
        return

    def handle_decl(self, decl):
        if not decl.strip().lower().startswith("doctype"):
            raise Refused(f"<!{decl[:20]}> is not drawn")

    def unknown_decl(self, data):
        raise Refused("a CDATA or marked section is not drawn")

    def handle_pi(self, data):
        raise Refused("a processing instruction is not drawn")


def _parse_css(css: str) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Parse already-resolved CSS into (selectors, declarations) rules, and
    refuse anything that is not on the measured lists."""
    if "/*" in css or "*/" in css:
        raise Refused("an unterminated CSS comment")
    if "@" in css:
        m = re.search(r"@[A-Za-z-]*", css)
        raise Refused(f"the CSS at-rule {m.group(0) if m else '@'} is not drawn")
    if "\\" in css:
        raise Refused("CSS escapes are not drawn")
    rules = []
    chunks = css.split("}")
    if chunks[-1].strip():
        raise Refused("CSS text outside a rule")
    for chunk in chunks[:-1]:
        if chunk.count("{") != 1:
            raise Refused("nested or unbalanced CSS braces")
        prelude, body = chunk.split("{")
        selectors = []
        for s in (s.strip() for s in prelude.split(",")):
            if s.lower() in ("body", "html"):
                # The wrapper plays the page's body: body/html rules style it.
                selectors.append(_ROOT_TAG)
                continue
            if not _SELECTOR_RE.match(s):
                raise Refused(f"the CSS selector {s!r} is not one the renderer was measured with "
                              "(one tag, one .class or one #id)")
            if s[0] not in ".#" and s not in ALLOWED_TAGS:
                raise Refused(f"the CSS selector {s!r} names a tag that is not drawn")
            selectors.append(s)
        decls = []
        for decl in body.split(";"):
            if not decl.strip():
                continue
            if ":" not in decl:
                raise Refused(f"{decl.strip()[:40]!r} is not a CSS declaration")
            prop, value = decl.split(":", 1)
            # The value goes on in lower case. CSS reads every value on these
            # lists without regard to case, Takumi does not always (measured:
            # handed linear-gradient(90DEG, …) it drops the whole gradient,
            # without a word), and the lists were measured in lower case.
            prop, value = prop.strip().lower(), " ".join(value.split()).lower()
            low = value
            if "var(" in low:
                m = re.search(r"var\(\s*([^,)]*)", value)
                raise Refused(f"var({m.group(1).strip() if m else ''}) is not a desktop token "
                              "this renderer can resolve")
            if "url(" in low:
                raise Refused("url() is not drawn")
            if prop.startswith("--"):
                raise Refused(f"the custom property {prop} is not drawn")
            if prop not in _PROPERTY_RE:
                raise Refused(f"the CSS property {prop!r} is not one the renderer was measured to draw")
            if not _VALUE_CHARS_RE.match(value) or (("\"" in value or "'" in value)
                                                     and prop != "font-family"):
                raise Refused(f"{prop}: {value[:60]!r} has characters the renderer is not given")
            if not _PROPERTY_RE[prop].match(value):
                if prop == "font-family":
                    raise Refused(f"font-family {value[:60]!r}: the only font drawn is "
                                  f"{SHIPPED_FONT!r} (write var(--font-body))")
                raise Refused(f"{prop}: {value[:60]!r} is not a value the renderer was measured to draw")
            if prop == "font-family":
                value = f'"{SHIPPED_FONT}"'  # one spelling: the name the child registers the font under
            decls.append((prop, value))
        rules.append((selectors, decls))
    return rules


def _serialize(node: _Node) -> str:
    if node.tag == _ROOT_TAG:
        inner = "".join(_serialize(c) if isinstance(c, _Node) else escape(c, quote=False)
                        for c in node.children)
        return f"<{_ROOT_TAG}>{inner}</{_ROOT_TAG}>"
    attrs = "".join(f' {k}="{escape(v)}"' for k, v in sorted(node.attrs.items()))
    inner = "".join(_serialize(c) if isinstance(c, _Node) else escape(c, quote=False)
                    for c in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


def _serialize_css(rules) -> str:
    return "".join(", ".join(sels) + "{" + "".join(f"{p}:{v};" for p, v in decls) + "}"
                   for sels, decls in rules)


@dataclass(frozen=True)
class CheckedPage:
    """What Takumi is handed: markup and CSS re-serialised from the parse
    that was checked, never the page as written."""
    html: str
    css: str


def check_page(page: str, tokens: Mapping[str, str], width: int = 720) -> CheckedPage:
    """The first line of the gate. Returns the page to draw, or raises
    Refused with the reason. Tokens are resolved before anything is checked."""
    if not isinstance(page, str) or not page.strip():
        raise Refused("the page is empty")
    if len(page) > MAX_PAGE_CHARS:
        raise Refused(f"the page is longer than {MAX_PAGE_CHARS} characters")
    parser = _PageParser()
    parser.feed(page)
    parser.close()
    if parser._in_style:
        raise Refused("a <style> block is not closed")
    if len(parser.stack) > 1:
        raise Refused(f"<{parser.stack[-1].tag}> is not closed; the markup is not well formed")
    if not parser.root.children:
        raise Refused("the page has nothing to draw")
    css = _COMMENT_RE.sub("", "\n".join(parser.css))
    css = resolve_tokens(css, tokens)
    rules = _parse_css(css)
    root_decls = [f"width:{int(width)}px", f'font-family:"{SHIPPED_FONT}"']
    # In lower case, as _parse_css hands every value on.
    bg = (tokens.get("--cseries-ink") or "").lower()
    fg = (tokens.get("--cseries-paper") or "").lower()
    if bg and _PROPERTY_RE["background-color"].match(bg):
        root_decls.append(f"background-color:{bg}")
    if fg and _PROPERTY_RE["color"].match(fg):
        root_decls.append(f"color:{fg}")
    root_css = f"{_ROOT_TAG}{{" + ";".join(root_decls) + "}"
    # Last, so the caller's width holds whatever the page says about body.
    width_css = f"{_ROOT_TAG}{{width:{int(width)}px}}"
    # The tags' browser defaults (h1 sizes, p margins, strong bolder) are
    # Takumi's own (the child draws with use_presets), beneath every rule here.
    return CheckedPage(html=_serialize(parser.root),
                       css=root_css + _BOX_SHEET + _serialize_css(rules) + width_css)


# ── The child process ──────────────────────────────────────────────────────

_SLOTS: asyncio.Semaphore | None = None


def _slots() -> asyncio.Semaphore:
    global _SLOTS
    if _SLOTS is None:
        _SLOTS = asyncio.Semaphore(MAX_CONCURRENT)
    return _SLOTS


def _child_argv() -> list[str]:
    """-I: no PYTHONPATH, no user site, no script directory on the path; the
    child imports only the stdlib and pytakumi. -B: it writes no bytecode
    (it cannot write anything anyway)."""
    cfg = {"font_family": SHIPPED_FONT,
           "deny": list(HOST_DATA_PATHS), "mem": MEMORY_LIMIT_BYTES,
           "cpu": CPU_LIMIT_S, "max_side": MAX_SIDE_PX, "max_depth": CHILD_MAX_DEPTH}
    return [sys.executable, "-I", "-B", _CHILD, json.dumps(cfg)]


def _child_env() -> dict[str, str]:
    """Nothing of the kernel's environment (it holds the Host's secrets),
    and Takumi's drawing threads held to DRAW_THREADS."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": "/nonexistent", "XDG_CACHE_HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent", "RAYON_NUM_THREADS": str(DRAW_THREADS)}


async def _drain(stream, cap: int, on_over=None) -> tuple[bytes, bool]:
    """Read `stream` to its end, keeping at most `cap` bytes. It never stops
    reading at the cap: a pipe nobody reads stalls the child on a full pipe,
    and asyncio's Process.wait() returns only once the process has exited AND
    every pipe has reached its end (BaseSubprocessTransport._try_finish), so a
    reader that stopped early left wait() pending forever and the slot held
    (measured: a child writing 40 MiB, or 1 MiB of stderr, never
    returned)."""
    buf, over = bytearray(), False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf), over
        if not over and len(buf) + len(chunk) > cap:
            over = True
            buf.clear()
            if on_over is not None:
                on_over()
        if not over:
            buf += chunk


# After the child is stopped, how long its pipes may take to close before the
# slot is given back anyway. The child cannot fork (its seccomp filter), so
# its pipes close when it dies; this bounds the wait if that ever fails.
SETTLE_S = 5.0


async def _run_child(payload: bytes, *, argv: list[str] | None = None,
                     time_limit_s: float | None = None) -> tuple[dict | None, str]:
    """Run one child, feed it the page, and return (its JSON reply, "") or
    (None, why). The child is killed through the handle this function holds,
    and only that child; nothing else is ever signalled."""
    limit = TIME_LIMIT_S if time_limit_s is None else time_limit_s
    async with _slots():
        try:
            proc = await asyncio.create_subprocess_exec(
                *(argv or _child_argv()), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=_child_env(), close_fds=True, cwd="/")
        except OSError as e:
            return None, f"the renderer could not start ({e.strerror or e})"

        def stop():
            if proc.returncode is None:
                try:
                    proc.kill()  # the handle's own pid: this child and nothing else
                except ProcessLookupError:
                    pass

        out_task = asyncio.ensure_future(_drain(proc.stdout, MAX_PNG_BYTES * 2, on_over=stop))
        err_task = asyncio.ensure_future(_drain(proc.stderr, 64 * 1024))
        try:
            async def feed():
                try:
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

            async def talk():
                # Fed while both outputs are read, so a child that writes
                # before it reads can never stall on a full pipe.
                await feed()
                (out, over), (err, _) = await asyncio.gather(
                    asyncio.shield(out_task), asyncio.shield(err_task))
                rc = await proc.wait()
                if over:
                    return None, "the renderer's answer was larger than a picture may be"
                return (out, err, rc), ""
            try:
                got, why = await asyncio.wait_for(talk(), timeout=limit)
            except asyncio.TimeoutError:
                return None, f"the renderer took longer than {limit:g} s and was stopped"
            if got is None:
                return None, why
            out, err, rc = got
            if rc < 0:
                return None, f"the renderer crashed (signal {-rc}) and nothing was drawn"
            line = out.split(b"\n", 1)[0]
            try:
                reply = json.loads(line)
            except ValueError:
                tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
                return None, f"the renderer failed (exit {rc}): {tail[0][:160]}"
            if not isinstance(reply, dict):
                return None, "the renderer's answer was not understood"
            return reply, ""
        finally:
            stop()

            async def settle():
                # The drains keep reading after a time-out, so the pipes reach
                # their end and wait() can return (see _drain).
                await asyncio.gather(out_task, err_task, return_exceptions=True)
                await proc.wait()
            done = asyncio.ensure_future(settle())
            try:
                await asyncio.wait_for(asyncio.shield(done), timeout=SETTLE_S)
            except asyncio.TimeoutError:
                # Something still holds a pipe open; the slot goes back and
                # the drains finish whenever it closes.
                log.warning("picture renderer: the child's pipes stayed open %gs after it "
                            "was stopped", SETTLE_S)


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


async def draw_picture(html: str, *, tokens: Mapping[str, str] | None = None,
                       width: int = 720) -> Picture:
    """Draw a Host's page (a ```visual fence body, or an artifact's rendered
    page) as a PNG, or say why not. Never raises for a page it cannot draw."""
    if isinstance(width, bool) or not isinstance(width, int) or not MIN_WIDTH <= width <= MAX_WIDTH:
        return Picture(None, f"width must be a whole number from {MIN_WIDTH} to {MAX_WIDTH}")
    if tokens is None:
        try:
            tokens = await desktop_tokens()
        except Exception as e:  # noqa: BLE001 — a page with no tokens can still draw
            log.debug("desktop tokens unavailable: %s", e)
            tokens = {}
    try:
        page = check_page(html, tokens, width)
    except Refused as e:
        return Picture(None, str(e))
    payload = json.dumps({"html": page.html, "css": page.css, "width": width}).encode()
    reply, why = await _run_child(payload)
    if reply is None:
        return Picture(None, why)
    if not reply.get("ok"):
        return Picture(None, str(reply.get("reason") or "the renderer refused")[:300])
    warns = [str(w) for w in (reply.get("warnings") or [])]
    if warns:
        return Picture(None, "the renderer warned: " + "; ".join(warns[:3])[:300])
    if int(reply.get("colours") or 0) < 2:
        return Picture(None, "the picture came out a single colour")
    try:
        png = base64.b64decode(reply.get("png") or "", validate=True)
    except ValueError:
        return Picture(None, "the renderer's picture was not understood")
    if not png.startswith(_PNG_SIG) or len(png) > MAX_PNG_BYTES:
        return Picture(None, "the renderer's picture was not a PNG of an allowed size")
    return Picture(png, "")
