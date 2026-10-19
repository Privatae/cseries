"""
The desktop document's token declaration — the one place a host-authored
design recolours the SHELL and every app.

A desktop artifact may declare, in static <head> markup:

    <meta name="cseries-tokens"
          content="--cseries-ink:#0a0e0f; --cseries-ember:#4f7f6a; --cseries-dusk:#7fcfae">

The kernel parses it here (never the document's scripts — static markup
only, the same rule as the mode meta) and serves the accepted pairs appended
to /css/tokens.css, which the shell and every system app load; the shell also
applies them live to itself. Nothing here is a security boundary the frame
depends on — the frame stays sandboxed regardless — but the values DO land in
first-party CSS, so the grammar is a whitelist: token names must be --cseries-*,
values are colours, plain numbers/lengths, or quoted font stacks, and nothing
that can reach out (url(), var(), expression(), escapes, braces, semicolons).

Pure module: no server imports, so scripts/test_desktop_tokens.py runs it
anywhere.
"""
from __future__ import annotations

import re

MAX_TOKENS = 64
MAX_HEAD_BYTES = 65536  # same static-head window the shell's mode detection reads

# The content attribute may be double- or single-quoted; the OTHER quote may
# appear inside it (a font stack like 'Iowan Old Style' in a double-quoted
# attribute), so the closing quote is a backreference, not a class.
_META_RE = re.compile(
    r"<meta\s+(?:[^>]*?\s)?name=[\"']cseries-tokens[\"'][^>]*?content=([\"'])(.*?)\1"
    r"|<meta\s+(?:[^>]*?\s)?content=([\"'])(.*?)\3[^>]*?name=[\"']cseries-tokens[\"']",
    re.IGNORECASE | re.DOTALL,
)
_NAME_RE = re.compile(r"^--cseries-[a-z0-9-]{1,40}$")
# Values: hex colours, rgb()/rgba()/hsl()/hsla() with plain numeric args,
# bare numbers with an optional unit, or a font stack of names/quoted names.
_VALUE_RE = re.compile(
    r"^(?:"
    r"#[0-9a-f]{3,8}"
    r"|(?:rgba?|hsla?)\(\s*[0-9.%\s,/-]+\)"
    r"|-?[0-9]*\.?[0-9]+(?:px|em|rem|%|ms|s)?"
    r"|(?:\"[A-Za-z0-9 -]{1,40}\"|'[A-Za-z0-9 -]{1,40}'|[A-Za-z][A-Za-z0-9-]{0,40})"
    r"(?:\s*,\s*(?:\"[A-Za-z0-9 -]{1,40}\"|'[A-Za-z0-9 -]{1,40}'|[A-Za-z][A-Za-z0-9-]{0,40}))*"
    r")$",
    re.IGNORECASE,
)
_FORBIDDEN = ("url", "var(", "expression", "\\", "{", "}", "<", ">", "@", "/*", "//")


MAX_VALUE_CHARS = 120


def extract_tokens(html: str) -> dict[str, str]:
    """The accepted --cseries-* pairs declared in the document's static head, in
    declaration order. Returns {} when the document declares nothing. What was
    NOT applied, and why, is parse_tokens()'s second half."""
    return parse_tokens(html)[0]


def parse_tokens(html: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """(accepted, dropped). A bad pair never takes the design down with it —
    it is left out — but it is never left out silently either: `dropped`
    names every declaration that was not applied, with the reason, so the
    desktop contract can tell the host which tokens did not land.

    The bounds stay: these values are appended to the CSS the shell and every
    app load, so the count (MAX_TOKENS) and each value's length
    (MAX_VALUE_CHARS) keep that file small and the grammar keeps it inert."""
    head = str(html or "")[:MAX_HEAD_BYTES]
    m = _META_RE.search(head)
    if not m:
        return {}, []
    raw = m.group(2) if m.group(2) is not None else m.group(4)
    out: dict[str, str] = {}
    dropped: list[dict[str, str]] = []
    for decl in str(raw or "").split(";"):
        if not decl.strip():
            continue
        if ":" not in decl:
            dropped.append({"name": " ".join(decl.split())[:60],
                            "why": "not a name:value declaration"})
            continue
        name, value = decl.split(":", 1)
        name = name.strip().lower()
        value = " ".join(value.strip().split())
        if not _NAME_RE.match(name):
            dropped.append({"name": name[:60], "why": "not a --cseries-* name of at most 40 "
                                                     "letters, digits and dashes"})
            continue
        if not value:
            dropped.append({"name": name, "why": "empty value"})
            continue
        if len(value) > MAX_VALUE_CHARS:
            dropped.append({"name": name, "why": f"value longer than {MAX_VALUE_CHARS} characters"})
            continue
        low = value.lower()
        if any(f in low for f in _FORBIDDEN) or not _VALUE_RE.match(value):
            dropped.append({"name": name, "why": "value is not a colour, number, length or "
                                                 "font stack (url(), var(), expression(), "
                                                 "escapes and braces are refused)"})
            continue
        if name not in out and len(out) >= MAX_TOKENS:
            dropped.append({"name": name, "why": f"past the {MAX_TOKENS}-token limit"})
            continue
        out[name] = value
    return out, dropped


def dropped_warning(dropped: list[dict[str, str]], *, show: int = 20) -> str | None:
    """One plain sentence naming the declarations that were not applied,
    grouped by reason, or None when every declaration landed. Each reason
    lists its first `show` names and states how many more there are."""
    if not dropped:
        return None
    by_why: dict[str, list[str]] = {}
    for d in dropped:
        by_why.setdefault(d["why"], []).append(d["name"])
    parts = []
    for why, names in by_why.items():
        listed = ", ".join(names[:show])
        more = f" and {len(names) - show} more" if len(names) > show else ""
        parts.append(f"{listed}{more} ({why})")
    return (f"{len(dropped)} design token declaration(s) were NOT applied: "
            + "; ".join(parts) + " — fix or remove them")


def tokens_css(tokens: dict[str, str]) -> str:
    """The override block appended to /css/tokens.css. Empty string when there
    is nothing to declare, so the base file is served byte-identical."""
    if not tokens:
        return ""
    lines = "".join(f"    {k}: {v};\n" for k, v in tokens.items())
    return (
        "\n/* ── Host design (the desktop document's cseries-tokens meta) ── */\n"
        ":root {\n" + lines + "}\n"
    )
