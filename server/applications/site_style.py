"""A website's look, read deterministically — the palette and type a desktop
can be built in.

The old Designs app's "turn any website into a template" lane becomes a
tool. The old lane ran a model over the site through the
in-pod SDK harness and wrote a brand template into the catalog; its window is
gone with the brand gallery. This is the tool: `artifact op='desktop_from_url'`
fetches the page and its stylesheets (brain/mastercode/runtime/url_reader.py —
the governed egress path, internal hosts refused) and this module reads the
colours and fonts out of the CSS with no model in the loop. What comes back is
a COMPLETE token set that already passes the desktop floor's readability
rules (desktop_contract.check_palette), so the Host starts from a desktop in
that site's palette and refines the design with the user, instead of guessing
hex values from a page it read as text.

Pure: no I/O, no server imports beyond the contract's colour maths.

What it reads, in order of how much it trusts it:
  - backgrounds and text colours set on html / body / :root (and the page's
    main landmarks), including var() references resolved against the page's
    own custom properties;
  - accents: custom properties named primary / accent / brand / link / cta,
    the colours on links and buttons, and <meta name="theme-color">;
  - fonts: font-family on html / body / :root (body type) and h1–h3 (display).
Frequency across the whole sheet breaks ties. A page that declares none of it
still yields a readable palette (the neutral fallbacks below), and says so in
`observed`.
"""
from __future__ import annotations

import colorsys
import re
from collections import Counter

from server.applications.desktop_contract import (
    CSS_NAMED_COLORS, REQUIRED_TOKENS, check_palette, contrast_ratio, parse_color)

_NAME_OK = re.compile(r"-{0,2}[a-zA-Z][-\w]{0,60}")
# Every repeated group is BOUNDED: an unbounded [^)]* after `rgb(` or a
# var() fallback scans to the end of the value whenever `)` is missing, which
# a hostile page can repeat (review of the tool, 37 s from 600 KB).
_COLOR_RE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|(?:rgba?|hsla?)\([^)]{0,64}\)", re.IGNORECASE)
_VAR_RE = re.compile(r"var\(\s*(--[-\w]{1,80})\s*(?:,\s*([^)]{0,200}))?\)")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_THEME_RE = re.compile(
    r"""<meta[^>]+name\s*=\s*["']theme-color["'][^>]*content\s*=\s*["']([^"']+)["']"""
    r"""|<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]*name\s*=\s*["']theme-color["']""",
    re.IGNORECASE)
_STYLE_ATTR_RE = re.compile(r"""\sstyle\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_TAG_NAME_RE = re.compile(r"<\s*(/?)([a-zA-Z][a-zA-Z0-9-]{0,20})")
MAX_HTML_SCAN = 1_000_000        # the part of the page read for tags
MAX_TAG = 2_000

_ROOT_SEL = re.compile(r"(^|[\s,>])(html|body|:root)(?=$|[\s,.:#\[>])", re.IGNORECASE)
_LANDMARK_SEL = re.compile(r"(^|[\s,>])(main|header|nav|footer|#app|\.app|#root)(?=$|[\s,.:#\[>])", re.IGNORECASE)
_ACCENT_SEL = re.compile(r"(^|[\s,>])(a|button)(?=$|[\s,.:#\[>])|\.btn|\[type=.?submit|primary|accent|brand|cta", re.IGNORECASE)
_ACCENT_VAR = re.compile(r"primary|accent|brand|link|cta|highlight", re.IGNORECASE)
_HEAD_SEL = re.compile(r"(^|[\s,>])h[123](?=$|[\s,.:#\[>])", re.IGNORECASE)
_GENERIC_FONTS = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
                  "ui-sans-serif", "ui-serif", "ui-monospace", "inherit", "initial",
                  "-apple-system", "blinkmacsystemfont", "emoji", "math", "unset"}
_FONT_OK = re.compile(r"^[A-Za-z][A-Za-z0-9 -]{0,38}$")

# How many linked stylesheets one page may cost in FETCHES. The bytes read are
# bounded by MAX_TOTAL_CSS below (the caller stops fetching once it has that
# much), so this only stops a page that links endless tiny sheets, each one a
# fetch that waits out url_reader's per-host courtesy gap. It was 4, a quality
# cut that left a palette out of a site's fifth sheet without a word; the sheets not read are now named in the answer.
MAX_STYLESHEETS = 32
MAX_CSS_BYTES = 400_000          # per stylesheet
# EVERY cost is bounded (review of the tool, reproduced: var() chains grew N^4
# in memory — 2 KB of CSS reached 456 MB — and a regex over brace-less text
# was quadratic, 80 KB in 31 s). A hostile page must never freeze or OOM the
# Host, so: a linear scanner, capped inputs, capped expansions.
MAX_TOTAL_CSS = 600_000          # everything read, page <style> included
MAX_RULES = 20_000
MAX_SELECTOR = 300
MAX_BODY = 4_000
MAX_VALUE = 1_000                # a declaration value, before and after var()
MAX_PROPS = 2_000                # custom properties kept
MAX_COLORS_PER_VALUE = 8         # colours read out of one declaration
MAX_DISTINCT_VALUES = 5_000      # values worked out per page (each once)
# One budget over everything var() resolution may scan on a page, whatever
# route a value takes — the backstop that makes the total bounded even for a
# shape nobody has thought of yet. Past it, values are read as written.
RESOLVE_BUDGET_CHARS = 1_500_000


# ── colour helpers ───────────────────────────────────────────────────────────

def _rgb(value: str):
    c = parse_color(value)
    if c is None or c[3] < 0.9:          # translucent: what shows through is unknown
        return None
    return c[:3]


def _hex(rgb) -> str:
    """0..1 channels → #rrggbb."""
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v * 255))) for v in rgb)


def _norm(rgb):
    """parse_color's 0..255 channels → 0..1 floats."""
    return tuple(v / 255.0 for v in rgb)


def _mix(a, b, t: float):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _sat_light(rgb):
    h, l, s = colorsys.rgb_to_hls(*rgb)
    return s, l


def _is_accent(rgb) -> bool:
    s, l = _sat_light(rgb)
    return s >= 0.35 and 0.18 <= l <= 0.82


def _cr(a, b) -> float:
    return contrast_ratio(_hex(a), _hex(b)) or 1.0


# ── reading the page ─────────────────────────────────────────────────────────

def _tags(html: str):
    """(name, tag_text, end_index) for each start tag in the first
    MAX_HTML_SCAN chars, in ONE linear pass (str.find, never a backtracking
    pattern over the page — a page of unclosed tags must stay cheap)."""
    html = html[:MAX_HTML_SCAN]
    pos = 0
    while True:
        lt = html.find("<", pos)
        if lt < 0:
            return
        gt = html.find(">", lt)
        if gt < 0:
            return
        tag = html[lt:gt + 1]
        pos = gt + 1
        m = _TAG_NAME_RE.match(tag[:40])
        if m and not m.group(1):
            yield m.group(2).lower(), tag[:MAX_TAG], gt + 1


def _page_parts(html: str) -> dict:
    """Everything this module reads from the markup, from one pass:
    stylesheet hrefs, <style> contents, <title>, theme-color, and inline style
    on <html>/<body>."""
    html = str(html or "")[:MAX_HTML_SCAN]
    low = html.lower()
    out = {"links": [], "styles": [], "title": "", "theme": None, "inline": []}
    # Once a search for a closing tag has failed, no later one can succeed —
    # stop searching (otherwise each unclosed tag rescans the rest of the page).
    no_style_close = no_title_close = False
    tags_seen = 0
    for name, tag, end in _tags(html):
        tags_seen += 1
        if tags_seen > 50_000:
            break
        if name == "link" and len(out["links"]) < 1_000:
            if re.search(r"""rel\s*=\s*["'][^"'>]*stylesheet""", tag, re.IGNORECASE):
                m = _HREF_RE.search(tag)
                if m and m.group(1) not in out["links"]:
                    out["links"].append(m.group(1))
        elif name == "style" and len(out["styles"]) < 20 and not no_style_close:
            close = low.find("</style", end)
            if close < 0:
                no_style_close = True
            elif close > end:
                out["styles"].append(html[end:close][:MAX_CSS_BYTES])
        elif name == "title" and not out["title"] and not no_title_close:
            close = low.find("</title", end)
            if close < 0:
                no_title_close = True
            elif close > end:
                out["title"] = re.sub(r"\s+", " ", html[end:min(close, end + 400)]).strip()[:80]
        elif name == "meta" and out["theme"] is None and "theme-color" in tag.lower():
            m = _THEME_RE.search(tag)
            if m:
                out["theme"] = m.group(1) or m.group(2)
        elif name in ("html", "body"):
            m = _STYLE_ATTR_RE.search(tag)
            if m:
                out["inline"].append(m.group(1)[:MAX_BODY])
    return out


def stylesheet_urls(html: str) -> list[str]:
    """Every stylesheet the page links, in document order (the first 1,000 of
    a page's tags that name one). The caller fetches them in order until it
    has MAX_TOTAL_CSS bytes or MAX_STYLESHEETS fetches, and names the rest.
    Resolution against the page URL and every fetch safety rule are the
    caller's (url_reader.normalize_url)."""
    return _page_parts(html)["links"]


def _strip_comments(css: str) -> str:
    """CSS without /* … */ — linear (an unclosed comment ends the sheet, as a
    browser reads it)."""
    out, pos = [], 0
    while True:
        a = css.find("/*", pos)
        if a < 0:
            out.append(css[pos:])
            return "".join(out)
        out.append(css[pos:a])
        b = css.find("*/", a + 2)
        if b < 0:
            return "".join(out)
        pos = b + 2


def _decls(body: str):
    """(name, value) pairs of a rule body — split on ';' and the first ':',
    linear, never a backtracking pattern."""
    for decl in body.split(";"):
        name, sep, value = decl.partition(":")
        name = name.strip()
        if sep and _NAME_OK.fullmatch(name):
            yield name, value.strip()[:MAX_VALUE]


def rules(css: str):
    """(selector, body) for every innermost rule, in ONE linear pass — nested
    blocks (@media, @supports) are opened and their inner rules read. Selector
    and body are clipped; at most MAX_RULES are yielded."""
    start = 0          # where the current selector began
    open_at = -1       # index of the '{' of the rule being read, if any
    n = 0
    for i, ch in enumerate(css):
        if ch == "{":
            # A '{' while one is already open means the earlier one opened a
            # block (an at-rule); its inner rule starts here.
            open_at = i
            sel_from = start
            start = i + 1
            selector = css[sel_from:i]
        elif ch == "}":
            if open_at >= 0:
                yield selector.strip()[-MAX_SELECTOR:], css[open_at + 1:i][:MAX_BODY]
                n += 1
                if n >= MAX_RULES:
                    return
            open_at = -1
            start = i + 1


def _custom_props(rule_list) -> dict[str, str]:
    props: dict[str, str] = {}
    for sel, body in rule_list:
        for name, value in _decls(body):
            if name.startswith("--") and name not in props:
                props[name] = value.strip()[:MAX_VALUE]
                if len(props) >= MAX_PROPS:
                    return props
    return props


class _Budget:
    def __init__(self, chars: int):
        self.left = chars


def _resolve(value: str, props: dict[str, str], budget: "_Budget | None" = None) -> str:
    """var() substituted, four levels at most, and the result never grows past
    MAX_VALUE — a chain of variables each referencing the next many times
    cannot blow up memory."""
    value = value[:MAX_VALUE]
    for _ in range(4):
        if "var(" not in value:
            break
        if budget is not None:
            if budget.left <= 0:
                break
            budget.left -= len(value)
        matched = False
        # Substitute left to right and STOP at MAX_VALUE: the expansion never
        # builds more than it keeps (a value of 110 var()s each worth 1 KB
        # used to build 110 KB per pass before clipping).
        out, size, pos = [], 0, 0
        for m in _VAR_RE.finditer(value):
            matched = True
            piece = value[pos:m.start()] + props.get(m.group(1), m.group(2) or "")
            out.append(piece)
            size += len(piece)
            pos = m.end()
            if size >= MAX_VALUE:
                break
        else:
            out.append(value[pos:])
        if not matched:
            break               # a pass that substitutes nothing changes nothing
        value = "".join(out)[:MAX_VALUE]
    return value


def extract(html: str, css_texts: list[str]) -> dict:
    """{title, background, text, accent, font_body, font_display, observed}.
    Colours are hex strings or None; `observed` lists what was seen, most
    trusted first, so the Host can say what it took from the page."""
    page = _page_parts(html)
    parts = [str(t)[:MAX_CSS_BYTES] for t in css_texts[:MAX_STYLESHEETS] if t]
    own = list(page["styles"])
    inline = " ".join(page["inline"])[:MAX_BODY]
    if inline:
        own.append("body{" + inline + "}")
    # The page's own <style> and body style come last (they win the cascade)
    # and are never the part cut: the linked sheets give up their tail first.
    # Comments go first (an unclosed one would swallow what follows), and the
    # linked part is cut at its last whole rule, never mid-block.
    room = max(0, MAX_TOTAL_CSS - sum(len(x) + 1 for x in own))
    ext = "\n".join(_strip_comments(p) for p in parts)
    if len(ext) > room:
        ext = ext[:room]
        ext = ext[:ext.rfind("}") + 1]
    css = "\n".join([ext, *own])[:MAX_TOTAL_CSS]
    css = _strip_comments(css)
    rule_list = list(rules(css))
    props = _custom_props(rule_list)

    bg, text, accent = Counter(), Counter(), Counter()
    body_fonts, head_fonts = Counter(), Counter()

    # Each DISTINCT value is resolved and parsed once (a page repeating one
    # expensive var() thousands of times costs one expansion — review of the
    # tool: 400 KB of `color:var(--x)` was 12.7 s), at most
    # MAX_DISTINCT_VALUES of them, and a value yields at most
    # MAX_COLORS_PER_VALUE colours.
    seen: dict[str, tuple[str, ...]] = {}
    fonts_seen: dict[str, str | None] = {}
    budget = _Budget(RESOLVE_BUDGET_CHARS)

    def colors_in(value):
        got = seen.get(value)
        if got is None:
            if len(seen) >= MAX_DISTINCT_VALUES:
                return ()
            resolved = _resolve(value, props, budget)
            raws = _COLOR_RE.findall(resolved)[:MAX_COLORS_PER_VALUE]
            if len(raws) < MAX_COLORS_PER_VALUE:
                raws += [w for w in re.findall(r"[a-zA-Z]+", _COLOR_RE.sub(" ", resolved))
                         if w.lower() in CSS_NAMED_COLORS][:MAX_COLORS_PER_VALUE - len(raws)]
            out = []
            for raw in raws:
                rgb = _rgb(raw)
                if rgb is not None:
                    out.append(_hex(_norm(rgb)))
            got = seen[value] = tuple(out)
        return got

    def add(counter, key, weight):
        if weight > 0:          # an unweighted sighting must never win by default
            counter[key] += weight

    for sel, body in rule_list:
        root = bool(_ROOT_SEL.search(sel)) and len(sel) < 80
        landmark = bool(_LANDMARK_SEL.search(sel))
        accent_sel = bool(_ACCENT_SEL.search(sel))
        for name, value in _decls(body):
            name = name.lower()
            if name.startswith("--"):
                if _ACCENT_VAR.search(name):
                    for c in colors_in(value):
                        accent[c] += 4
                continue
            if name in ("background", "background-color"):
                for c in colors_in(value):
                    add(bg, c, 6 if root else (2 if landmark else 0))
                    if accent_sel:
                        accent[c] += 2
            elif name == "color":
                for c in colors_in(value):
                    add(text, c, 6 if root else (1 if landmark else 0))
                    if accent_sel:
                        accent[c] += 2
                    else:
                        accent[c] += 0.25
            elif name in ("border-color", "outline-color", "fill", "stroke") and accent_sel:
                for c in colors_in(value):
                    accent[c] += 1
            elif name == "font-family":
                # Same memo, cap and budget as colours.
                if value in fonts_seen:
                    first = fonts_seen[value]
                elif len(fonts_seen) >= MAX_DISTINCT_VALUES:
                    first = None
                else:
                    first = fonts_seen[value] = _first_font(_resolve(value, props, budget))
                if first:
                    if root:
                        body_fonts[first] += 3
                    elif _HEAD_SEL.search(sel):
                        head_fonts[first] += 3
                    else:
                        body_fonts[first] += 0.2

    if page["theme"]:
        rgb = _rgb(page["theme"][:100])
        if rgb is not None:
            accent[_hex(_norm(rgb))] += 3
    title = page["title"]

    accents = [c for c, _ in accent.most_common() if _is_accent(_norm(_rgb(c)))]
    return {
        "title": title,
        "background": bg.most_common(1)[0][0] if bg else None,
        "text": text.most_common(1)[0][0] if text else None,
        "accent": accents[0] if accents else None,
        "font_body": body_fonts.most_common(1)[0][0] if body_fonts else None,
        "font_display": head_fonts.most_common(1)[0][0] if head_fonts else None,
        "observed": {
            "backgrounds": [c for c, _ in bg.most_common(5)],
            "text": [c for c, _ in text.most_common(5)],
            "accents": accents[:5],
            "fonts": [f for f, _ in (body_fonts + head_fonts).most_common(5)],
        },
    }


def _first_font(value: str) -> str | None:
    """The first real family, if the token grammar will carry it: a name the
    shell's token parser drops (one containing `url` or `expression` —
    desktop_tokens._FORBIDDEN) would reach the desktop but not the shell, so
    the machine would disagree with itself."""
    from server.applications.desktop_tokens import _FORBIDDEN
    for part in value[:MAX_VALUE].split(","):
        name = part.strip().strip("\"'").strip()
        low = name.lower()
        if (name and low not in _GENERIC_FONTS and _FONT_OK.match(name)
                and not any(f in low for f in _FORBIDDEN)):
            return name
    return None


# ── building the token set ───────────────────────────────────────────────────

_BLACK, _WHITE = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)


def _surfaces(base, text):
    return {
        "ink": base,
        "night": _mix(base, text, 0.04),
        "twilight": _mix(base, text, 0.08),
        "silk": _mix(base, text, 0.13),
        "veil": _mix(base, text, 0.19),
        "mist": _mix(base, text, 0.26),
        "mist_strong": _mix(base, text, 0.38),
    }


def _min_cr(fg, surfaces) -> float:
    return min(_cr(fg, s) for s in surfaces)


def tokens_for(style: dict) -> dict[str, str]:
    """A COMPLETE --cseries-* set in the page's palette that clears every
    contrast pair the floor measures. Surfaces step from the page's
    background toward its text; text colours are the page's own; the accent
    is the page's accent, lightened or darkened until it reads on the chrome.

    Every loop is bounded, and each ends on a value that is readable by
    construction: text is moved toward whichever of black and white reads
    better on the surfaces, and a mid-grey background that neither can clear
    4.5:1 on is itself moved away from the text until one can."""
    base = _norm(_rgb(style.get("background") or "") or _WHITE)
    text = _norm(_rgb(style.get("text") or "") or (0.07, 0.07, 0.08))

    for _ in range(8):
        surf = _surfaces(base, text)
        floor = (surf["ink"], surf["night"], surf["twilight"])   # the 4.5:1 pairs
        target = _BLACK if _min_cr(_BLACK, floor) >= _min_cr(_WHITE, floor) else _WHITE
        paper = text
        for _ in range(8):
            if _min_cr(paper, floor) >= 4.5:
                break
            paper = _mix(paper, target, 0.35)
        if _min_cr(paper, floor) < 4.5:
            paper = target
        if _min_cr(paper, floor) >= 4.5:
            break
        # Neither extreme reads on this background: push the background away.
        base = _mix(base, _WHITE if target == _BLACK else _BLACK, 0.25)
        text = paper

    night = surf["night"]
    linen = _mix(paper, base, 0.30)
    for _ in range(12):
        if _cr(linen, night) >= 3.2:
            break
        linen = _mix(linen, paper, 0.35)
    if _cr(linen, night) < 3.0:
        linen = paper
    ash = _mix(paper, base, 0.50)

    accent = _norm(_rgb(style.get("accent") or "") or _mix(paper, base, 0.45))
    toward = _WHITE if _cr(_WHITE, night) >= _cr(_BLACK, night) else _BLACK
    dusk = accent
    for _ in range(12):
        if _cr(dusk, night) >= 3.0:
            break
        dusk = _mix(dusk, toward, 0.18)
    bright = _mix(dusk, toward, 0.25)
    gr, gg, gb = (round(v * 255) for v in night)
    ar, ag, ab = (round(v * 255) for v in dusk)

    tokens = {
        "--cseries-ink": _hex(surf["ink"]), "--cseries-night": _hex(night),
        "--cseries-twilight": _hex(surf["twilight"]), "--cseries-silk": _hex(surf["silk"]),
        "--cseries-veil": _hex(surf["veil"]), "--cseries-mist": _hex(surf["mist"]),
        "--cseries-mist-strong": _hex(surf["mist_strong"]),
        "--cseries-paper": _hex(paper), "--cseries-linen": _hex(linen), "--cseries-ash": _hex(ash),
        "--cseries-ember": _hex(accent), "--cseries-dusk": _hex(dusk), "--cseries-bright": _hex(bright),
        "--cseries-glass": f"rgba({gr},{gg},{gb},0.78)",
        "--cseries-glow": f"rgba({ar},{ag},{ab},0.45)",
    }
    body = style.get("font_body")
    display = style.get("font_display") or body
    if body:
        tokens["--cseries-font-body"] = f"{_quote(body)}, system-ui, sans-serif"
    if display:
        tokens["--cseries-font-display"] = f"{_quote(display)}, system-ui, sans-serif"
    return tokens


def _quote(name: str) -> str:
    return f"'{name}'" if " " in name else name


def palette_is_readable(tokens: dict[str, str]) -> bool:
    return all(t in tokens for t in REQUIRED_TOKENS) and not check_palette(tokens)[0]
