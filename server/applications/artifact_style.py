"""Host-made artifacts wear the desktop.

There is no per-project style: every artifact defaults to the current UI and
takes the shape of the desktop. Apps keep their own look; this is only for the artifacts
a Host writes.

The mechanism is one stylesheet, added when an artifact is SHOWN, never
written into the artifact's files:

- the same token sheet the shell and every system app load (`/css/tokens.css`
  — the stock palette, fonts and radii, with the live desktop's `cseries-tokens`
  appended when a Host design is worn), so every `var(--cseries-*)` an artifact
  names resolves to what is on screen right now;
- a zero-specificity base (`:where(...)`) giving the page the desktop's
  background, text colour and type. Anything the artifact styles itself wins
  over it, so a Host's own choices are never overridden; an artifact that sets
  nothing still looks like the machine it lives on.

Because it is added at display time, an artifact made last month follows the
desktop the user switched to today. The one place that has to FREEZE it is a
published site (a public page must not change with the owner's desktop), so
the publish path bakes the same block into the zipped pages.

A desktop document is left alone: it declares its own tokens, and giving it
the current desktop's values would make a new design inherit the old one.
"""
from __future__ import annotations

import re

# Marks the block so a second pass (a publish of an already-baked page, a
# render of a render) never stacks two copies.
STYLE_MARK = "data-cseries-artifact-style"

_BASE_CSS = """
:where(html) { background: var(--cseries-ink); color: var(--cseries-paper);
  font-family: var(--font-body, var(--cseries-font-body, system-ui, sans-serif)); }
:where(body) { margin: 0; }
:where(a) { color: var(--cseries-dusk); }
"""

# The desktop document's mode meta.
# Read from the static head only, same as the contract.
_DESKTOP_META_RE = re.compile(r"<meta[^>]+name\s*=\s*[\"']cseries-desktop[\"']", re.I)
_HEAD_BYTES = 64 * 1024
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.I)
_DOCTYPE_RE = re.compile(r"^[\t\n\f\r ]*<!doctype[^>]*>", re.I)
_FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}", re.I)
_CHARSET_RE = re.compile(r"<meta\s[^>]*charset\s*=[^>]*>", re.I)
# Anything that styles the page — the block must come before all of it.
_STYLING_RE = re.compile(r"<style\b|<link\b", re.I)
_BOUND_RE = re.compile(r"<script\b|</head\s*>", re.I)
_STYLE_END_RE = re.compile(r"</(style)", re.I)


def is_desktop_document(html: str) -> bool:
    return bool(_DESKTOP_META_RE.search(str(html or "")[:_HEAD_BYTES]))


def style_block(token_sheet: str) -> str:
    """The block to add, or '' when there is no sheet to add."""
    if not token_sheet or not token_sheet.strip():
        return ""
    # The sheet's @font-face rules point at the shell's /fonts/ files, which a
    # sandboxed frame cannot fetch; dropping them saves a blocked request per
    # font. The families still name Inter first and fall back to system type.
    sheet = _FONT_FACE_RE.sub("", token_sheet)
    # A literal </style> inside the sheet would close the element early.
    # The HTML parser matches the end tag case-insensitively, so this does too.
    sheet = _STYLE_END_RE.sub(r"<\\/\1", sheet)
    return f"<style {STYLE_MARK}>\n{sheet}\n{_BASE_CSS}</style>"


def wear_desktop(html: str, token_sheet: str) -> str:
    """Return `html` with the desktop's style block placed FIRST in its head,
    so every rule the artifact declares comes later and wins.

    Unchanged when: there is no sheet, the document is a desktop design, or
    the block is already there."""
    html = str(html or "")
    block = style_block(token_sheet)
    if not block or is_desktop_document(html) or STYLE_MARK in html[:_HEAD_BYTES]:
        return html
    # A <meta charset> stays first: the encoding hint must sit in the first
    # 1024 bytes of a PUBLISHED page, and the block is ~9 KB. Everything the
    # artifact styles still comes after the block.
    # Every match is bounded to the markup BEFORE the first <script> (and
    # </head>): a script may carry "<head>" or "<meta charset>" inside a string
    # it builds, and splicing ~9 KB of CSS with newlines into a JS string kills
    # the artifact's whole script (reproduced).
    bound = _BOUND_RE.search(html[:_HEAD_BYTES])
    limit = bound.start() if bound else min(len(html), _HEAD_BYTES)
    head = html[:limit]
    c = _CHARSET_RE.search(head)
    if c and not _STYLING_RE.search(html[:c.start()]):
        return html[:c.end()] + block + html[c.end():]
    m = _HEAD_OPEN_RE.search(head)
    if m:
        return html[:m.end()] + block + html[m.end():]
    d = _DOCTYPE_RE.match(html)
    if d:
        return html[:d.end()] + block + html[d.end():]
    return block + html
