"""
The desktop document's MANDATORY floor, the brief a designing agent works
from, and the template it starts with. It exists because a desktop once
shipped as a wallpaper with dead buttons and ten fictional apps. A design
needs a minimum set of requirements and the full capability list, and the
designing agent needs a template to work from: no mocks or stubs. A harness
agent that designs without that context stubs everything, because it never
learned what it can and must build.

So three things live here, in one pure module (no server imports —
scripts/test_desktop_contract.py runs it anywhere, and server/routes/review.py
prepends the brief to harness prompts without importing the kernel):

  check_document(html)   the floor. set_desktop REFUSES a document that fails
                         it; create/write report it so the host fixes the
                         design in the same turn instead of the user finding
                         out. It checks that the palette is COMPLETE and that
                         it is READABLE — see check_palette.
  design_brief()         the capability list + requirements, in prose, for
                         the host's skill and for every harness run that is
                         designing a desktop or shell.
  TEMPLATE_HTML          a complete, working desktop that passes the floor —
                         every widget draws real shell state, every tile
                         launches a real app. Start here, never from a blank
                         page.

The floor is deliberately closed: a document that declares nothing is not
"a wallpaper", it is rejected. What used to be "three levels, all valid" is
now one contract with two modes.
"""
from __future__ import annotations

import re

from server.applications.desktop_tokens import (  # pure module
    _META_RE as _TOKENS_META_RE, dropped_warning, extract_tokens, parse_tokens)

MAX_HEAD_BYTES = 65536


def _past_head_window(tag: str) -> str:
    """The refusal for a head declaration that is in the document but past the
    MAX_HEAD_BYTES the shell reads it from."""
    return (f"{tag} is in the document but past its first {MAX_HEAD_BYTES // 1024} KB, where "
            "the shell reads it. The rendered document is longer than your file: each of your "
            "images named /api/images/<id> becomes the image itself, and a stylesheet file "
            "is put in whole. Put <meta name=\"cseries-desktop\"> and <meta name=\"cseries-tokens\"> "
            "first in <head>, before any <style>, <link>, script or image")

# The tokens a design MUST declare. Together they carry a complete palette
# through the shell and every app: five surfaces, the hairline, three text
# colours, the two accents. A design that names fewer leaves the taskbar and
# windows on stock colours while the desktop is something else — the exact
# "wallpaper with a different taskbar" failure the floor exists to stop.
REQUIRED_TOKENS: tuple[str, ...] = (
    "--cseries-ink", "--cseries-night", "--cseries-twilight", "--cseries-silk", "--cseries-veil",
    "--cseries-mist",
    "--cseries-paper", "--cseries-linen", "--cseries-ash",
    "--cseries-ember", "--cseries-dusk",
)

# Everything a design MAY declare beyond the required set. Each one is read
# by the shell's stylesheet (dashboard/base/css/tokens.css maps the chrome
# variables onto these with fallbacks) — so this list is the honest answer to
# "what can I change": if it is not here, declaring it does nothing.
OPTIONAL_TOKENS: tuple[str, ...] = (
    "--cseries-mist-strong", "--cseries-bright", "--cseries-moss", "--cseries-clay", "--cseries-gold",
    "--cseries-ember-soft", "--cseries-ember-ring", "--cseries-seam", "--cseries-seam-active",
    "--cseries-glass", "--cseries-glass-blur", "--cseries-glow", "--cseries-tile-bg", "--cseries-tile-bg-2",
    "--cseries-font-body", "--cseries-font-display", "--cseries-font-mono",
    "--cseries-taskbar-h", "--cseries-window-head-h",
    "--cseries-radius-sm", "--cseries-radius-md", "--cseries-radius-lg", "--cseries-radius-xl",
    "--cseries-icon-size",
)

_MODE_RE = re.compile(
    r"<meta\s+(?:[^>]*?\s)?name=[\"']cseries-desktop[\"'][^>]*?content=[\"'](wallpaper|desktop)[\"']"
    r"|<meta\s+(?:[^>]*?\s)?content=[\"'](wallpaper|desktop)[\"'][^>]*?name=[\"']cseries-desktop[\"']",
    re.IGNORECASE,
)
# ── THE BAR ──────────────────────────────────────────────────────────────────
# A separate toolbar is extra noise; folding its function into the desktop UI
# template makes a better product, and the bar is a fixed part of the
# template. A `desktop` document may declare
# that IT draws the bar: <meta name="cseries-bar" content="bottom 48"> — the
# edge it takes and its thickness in px. The shell then hides its own taskbar
# and keeps windows out of that strip (maximized ones included), and hands the
# document what the bar needs: the dock, the open windows, show-desktop, help
# and the launcher. A document that
# declares nothing keeps the shell's taskbar, exactly as before.
BAR_EDGES: tuple[str, ...] = ("top", "bottom", "left", "right")
BAR_MIN_PX, BAR_MAX_PX = 28, 120
_BAR_META_RE = re.compile(
    r"<meta\s+(?:[^>]*?\s)?name=[\"']cseries-bar[\"'][^>]*?content=[\"']([^\"']*)[\"']"
    r"|<meta\s+(?:[^>]*?\s)?content=[\"']([^\"']*)[\"'][^>]*?name=[\"']cseries-bar[\"']",
    re.IGNORECASE,
)
_BAR_VALUE_RE = re.compile(r"^\s*(top|bottom|left|right)\s+([0-9]{2,3})\s*(?:px)?\s*$", re.IGNORECASE)
# What lets the user get back to an open window once the shell's dock is gone.
_BAR_SWITCH_RE = re.compile(
    r"CSeriesDesktop\.(?:dock|windows)\b|data-cseries-(?:dock|windows|window|activate)\b")
_BAR_LAUNCH_RE = re.compile(r"\.openLauncher\s*\(|data-cseries-launcher\b")
_BAR_SHOW_RE = re.compile(r"\.toggleDesktop\s*\(|data-cseries-show-desktop\b")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

_EXTERNAL_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//|@import\b|<link[^>]+rel=["']stylesheet["'][^>]+href=["']\s*(?:https?:)?//""",
    re.IGNORECASE,
)
_APP_ATTR_RE = re.compile(r"""data-cseries-app\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# A literal list of app-shaped objects — `{ id: "mail", name: "Mail", … }` —
# is the signature of an invented app set. Three or more is a list, not a
# lookup table for one thing.
_FAKE_APP_ITEM_RE = re.compile(
    r"""\{\s*(?:["']?id["']?\s*:\s*["'][^"']+["']\s*,\s*["']?name["']?\s*:|["']?name["']?\s*:\s*["'][^"']+["']\s*,\s*["']?id["']?\s*:)""",
    re.IGNORECASE,
)
# ── the NETWORK profile ─────────────────────────────────────────────────────
# The second surface a host-authored artifact may render on: the map in the
# Network app. Same floor, same reason — an early desktop mounted as a
# screenshot-in-HTML with ten fictional apps because nothing checked, and a map
# has the identical failure with a worse consequence: a convincing picture of
# machines that do not exist, shown to someone asking where their data goes.
#
# It is deliberately NOT a copy of the desktop rules. A desktop dresses the
# whole shell through its tokens, so the token set is required there; a map
# renders inside the app's own scene box and the shell reads nothing from it,
# so requiring tokens here would be rigor for its own sake. What IS required is
# the part that keeps the picture honest: the nodes come from the bridge.
_NETWORK_MODE_RE = re.compile(
    r"<meta\s+(?:[^>]*?\s)?name=[\"']cseries-network[\"'][^>]*?content=[\"'](map)[\"']"
    r"|<meta\s+(?:[^>]*?\s)?content=[\"'](map)[\"'][^>]*?name=[\"']cseries-network[\"']",
    re.IGNORECASE,
)
# A literal list of node-shaped objects is an invented topology — the same
# signature as a typed app list, and the thing this surface must never show.
_FAKE_NODE_ITEM_RE = re.compile(
    r"""\{\s*(?:["']?id["']?\s*:\s*["'][^"']+["']\s*,\s*["']?(?:label|name|hostname)["']?\s*:"""
    r"""|["']?(?:label|name|hostname)["']?\s*:\s*["'][^"']+["']\s*,\s*["']?id["']?\s*:)""",
    re.IGNORECASE,
)

_STUB_TEXT_RE = re.compile(
    r"\b(?:lorem ipsum|coming soon|placeholder|TODO|TBD|sample data|mock(?:ed|up)?\b|dummy)\b",
    re.IGNORECASE,
)
_DEAD_LINK_RE = re.compile(r"""href\s*=\s*["']#["']|onclick\s*=\s*["']\s*(?:return\s+false|void\(0\))\s*;?\s*["']""", re.IGNORECASE)


# ── colour math: a palette has to be READABLE, not just complete ────────────
# Origin (a real artifact). The document declared all
# eleven required tokens and passed, because the floor only counted names. The
# palette was inverted: the surfaces held the light values and the text tokens
# held the near-black ones. The shell paints window bodies from --cseries-night
# and body text from --cseries-paper (dashboard/base/css/desktop.css,
# dashboard/base/css/tokens.css), so the user got black text on black across
# the whole machine. Completeness was never the property that mattered.
#
# The maths is WCAG 2.x relative luminance and contrast ratio, written out here
# rather than pulled in: it is twenty lines, and this module is pure on purpose
# (scripts/test_desktop_contract.py runs it with nothing installed).
_HEX_RE = re.compile(r"^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$", re.IGNORECASE)
_FUNC_RE = re.compile(r"^(rgba?|hsla?)\(([^)]*)\)$", re.IGNORECASE)
_NUM_RE = re.compile(r"^[+-]?(?:[0-9]*\.)?[0-9]+(%?)$")

# Every CSS named colour (CSS Color Module Level 4, 148 keywords including
# rebeccapurple), lower-cased. Generated from the `color-name` package rather
# than typed out: one wrong digit here is a wrong contrast ratio, and nothing
# downstream would catch it. A design is free to write `--cseries-paper: white`
# — desktop_tokens.py's grammar admits bare keywords — so the floor has to
# read that as a colour. "Skipped" is a hole when the value is measurable.
CSS_NAMED_COLORS: dict[str, str] = {
    "aliceblue": "#f0f8ff", "antiquewhite": "#faebd7", "aqua": "#00ffff",
    "aquamarine": "#7fffd4", "azure": "#f0ffff", "beige": "#f5f5dc",
    "bisque": "#ffe4c4", "black": "#000000", "blanchedalmond": "#ffebcd",
    "blue": "#0000ff", "blueviolet": "#8a2be2", "brown": "#a52a2a",
    "burlywood": "#deb887", "cadetblue": "#5f9ea0", "chartreuse": "#7fff00",
    "chocolate": "#d2691e", "coral": "#ff7f50", "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc", "crimson": "#dc143c", "cyan": "#00ffff",
    "darkblue": "#00008b", "darkcyan": "#008b8b", "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9", "darkgreen": "#006400", "darkgrey": "#a9a9a9",
    "darkkhaki": "#bdb76b", "darkmagenta": "#8b008b",
    "darkolivegreen": "#556b2f", "darkorange": "#ff8c00",
    "darkorchid": "#9932cc", "darkred": "#8b0000", "darksalmon": "#e9967a",
    "darkseagreen": "#8fbc8f", "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f", "darkslategrey": "#2f4f4f",
    "darkturquoise": "#00ced1", "darkviolet": "#9400d3",
    "deeppink": "#ff1493", "deepskyblue": "#00bfff", "dimgray": "#696969",
    "dimgrey": "#696969", "dodgerblue": "#1e90ff", "firebrick": "#b22222",
    "floralwhite": "#fffaf0", "forestgreen": "#228b22", "fuchsia": "#ff00ff",
    "gainsboro": "#dcdcdc", "ghostwhite": "#f8f8ff", "gold": "#ffd700",
    "goldenrod": "#daa520", "gray": "#808080", "green": "#008000",
    "greenyellow": "#adff2f", "grey": "#808080", "honeydew": "#f0fff0",
    "hotpink": "#ff69b4", "indianred": "#cd5c5c", "indigo": "#4b0082",
    "ivory": "#fffff0", "khaki": "#f0e68c", "lavender": "#e6e6fa",
    "lavenderblush": "#fff0f5", "lawngreen": "#7cfc00",
    "lemonchiffon": "#fffacd", "lightblue": "#add8e6",
    "lightcoral": "#f08080", "lightcyan": "#e0ffff",
    "lightgoldenrodyellow": "#fafad2", "lightgray": "#d3d3d3",
    "lightgreen": "#90ee90", "lightgrey": "#d3d3d3", "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a", "lightseagreen": "#20b2aa",
    "lightskyblue": "#87cefa", "lightslategray": "#778899",
    "lightslategrey": "#778899", "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0", "lime": "#00ff00", "limegreen": "#32cd32",
    "linen": "#faf0e6", "magenta": "#ff00ff", "maroon": "#800000",
    "mediumaquamarine": "#66cdaa", "mediumblue": "#0000cd",
    "mediumorchid": "#ba55d3", "mediumpurple": "#9370db",
    "mediumseagreen": "#3cb371", "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a", "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585", "midnightblue": "#191970",
    "mintcream": "#f5fffa", "mistyrose": "#ffe4e1", "moccasin": "#ffe4b5",
    "navajowhite": "#ffdead", "navy": "#000080", "oldlace": "#fdf5e6",
    "olive": "#808000", "olivedrab": "#6b8e23", "orange": "#ffa500",
    "orangered": "#ff4500", "orchid": "#da70d6", "palegoldenrod": "#eee8aa",
    "palegreen": "#98fb98", "paleturquoise": "#afeeee",
    "palevioletred": "#db7093", "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9", "peru": "#cd853f", "pink": "#ffc0cb",
    "plum": "#dda0dd", "powderblue": "#b0e0e6", "purple": "#800080",
    "rebeccapurple": "#663399", "red": "#ff0000", "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1", "saddlebrown": "#8b4513", "salmon": "#fa8072",
    "sandybrown": "#f4a460", "seagreen": "#2e8b57", "seashell": "#fff5ee",
    "sienna": "#a0522d", "silver": "#c0c0c0", "skyblue": "#87ceeb",
    "slateblue": "#6a5acd", "slategray": "#708090", "slategrey": "#708090",
    "snow": "#fffafa", "springgreen": "#00ff7f", "steelblue": "#4682b4",
    "tan": "#d2b48c", "teal": "#008080", "thistle": "#d8bfd8",
    "tomato": "#ff6347", "turquoise": "#40e0d0", "violet": "#ee82ee",
    "wheat": "#f5deb3", "white": "#ffffff", "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00", "yellowgreen": "#9acd32",
}


def _channel(raw: str, scale: float) -> float | None:
    """One function argument as 0..scale, or None when it is not a plain number.
    A percentage is relative to that argument's own full scale."""
    m = _NUM_RE.match(raw.strip())
    if not m:
        return None
    n = float(raw.strip().rstrip("%"))
    return n / 100.0 * scale if m.group(1) else n


def _hsl_to_rgb(h: float, s: float, ll: float) -> tuple[float, float, float]:
    """CSS hsl() to sRGB 0..255. h in degrees (wrapped), s and l in 0..1."""
    h = h % 360.0
    s = max(0.0, min(1.0, s))
    ll = max(0.0, min(1.0, ll))
    c = (1.0 - abs(2.0 * ll - 1.0)) * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = ll - c / 2.0
    r, g, b = ((c, x, 0.0) if h < 60 else (x, c, 0.0) if h < 120 else
               (0.0, c, x) if h < 180 else (0.0, x, c) if h < 240 else
               (x, 0.0, c) if h < 300 else (c, 0.0, x))
    return ((r + m) * 255.0, (g + m) * 255.0, (b + m) * 255.0)


def parse_color(value: str) -> tuple[float, float, float, float] | None:
    """(r, g, b, alpha) — r/g/b in 0..255, alpha in 0..1 — for a hex,
    rgb()/rgba(), hsl()/hsla() or CSS named-colour token value. None only for
    what genuinely carries no colour: a font stack, a length, var(), a keyword
    that is not a colour name.

    Unmeasurable is never a failure — the rules below skip a value they could
    not read rather than refusing a design on a guess. That is exactly why this
    has to be WIDE: every form it cannot read is a form an inverted palette can
    hide in, and the token grammar (desktop_tokens.py) already admits hsl() and
    bare keywords. The first cut read hex and rgb() only, so that
    backwards palette would have passed verbatim in hsl().
    """
    v = " ".join(str(value or "").strip().split())
    named = CSS_NAMED_COLORS.get(v.lower())
    if named:
        v = named
    if _HEX_RE.match(v):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        a = int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0
        return (float(r), float(g), float(b), a)
    m = _FUNC_RE.match(v)
    if not m:
        return None
    fn = m.group(1).lower()
    # Both the legacy comma form and the modern space form, with the alpha
    # after either a comma or a slash.
    args = [p for p in re.split(r"[,/\s]+", m.group(2).strip()) if p]
    if len(args) not in (3, 4):
        return None
    alpha = 1.0
    if len(args) == 4:
        alpha = _channel(args[3], 1.0)
        if alpha is None:
            return None
    if fn.startswith("rgb"):
        rgb = [_channel(x, 255.0) for x in args[:3]]
        if any(c is None for c in rgb):
            return None
        out = (rgb[0], rgb[1], rgb[2])
    else:
        # hsl(): hue is an angle (bare number or 'deg'), s and l are
        # percentages. _channel's 0..1 scale reads "60%" as 0.6 and a bare
        # "0.6" as 0.6, which is what CSS Color 4 accepts for both.
        hue_raw = args[0][:-3] if args[0].lower().endswith("deg") else args[0]
        hue, sat, lig = (_channel(hue_raw, 360.0), _channel(args[1], 1.0),
                         _channel(args[2], 1.0))
        if hue is None or sat is None or lig is None:
            return None
        out = _hsl_to_rgb(hue, sat, lig)
    return (out[0], out[1], out[2], max(0.0, min(1.0, alpha)))


def relative_luminance(rgb: tuple[float, float, float, float]) -> float:
    """WCAG 2.x relative luminance, 0 (black) .. 1 (white)."""
    def lin(c: float) -> float:
        s = max(0.0, min(255.0, c)) / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


# At or above this an alpha is opaque for measuring purposes.
ALPHA_OPAQUE = 0.999
# Below this, a translucent colour is a wash rather than text and the composite
# is dominated by whatever is behind it. Between the two, compositing over a
# KNOWN opaque background is exact arithmetic, so it is measured rather than
# waved through: rgba(230,230,240,0.9) on a dark window is text, and skipping
# it left a hole an inverted palette could sit in.
ALPHA_MEASURABLE = 0.85


def _over(fg: tuple[float, float, float, float],
          bg: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Source-over compositing of fg onto an OPAQUE bg. Exact, and meaningful
    only because bg is known — which is why the caller checks bg first."""
    a = fg[3]
    return (fg[0] * a + bg[0] * (1.0 - a),
            fg[1] * a + bg[1] * (1.0 - a),
            fg[2] * a + bg[2] * (1.0 - a), 1.0)


def contrast_ratio(fg: str, bg: str) -> float | None:
    """The WCAG contrast ratio between two token VALUES, or None when it cannot
    be measured honestly.

    The BACKGROUND must be opaque: a translucent surface renders over whatever
    sits behind it, which a static read of the document cannot know. A
    near-opaque FOREGROUND over an opaque background is composited and then
    measured.
    """
    a, b = parse_color(fg), parse_color(bg)
    if a is None or b is None or b[3] < ALPHA_OPAQUE:
        return None
    if a[3] < ALPHA_OPAQUE:
        if a[3] < ALPHA_MEASURABLE:
            return None
        a = _over(a, b)
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


# The pairs the shell actually composites, with the floor each one has to
# clear. 4.5:1 is WCAG AA for body text; 3:1 is AA for large/secondary text,
# which is what --cseries-linen is used for.
CONTRAST_PAIRS: tuple[tuple[str, str, float, str], ...] = (
    ("--cseries-paper", "--cseries-ink", 4.5, "body text on the desktop surface"),
    ("--cseries-paper", "--cseries-night", 4.5, "body text on a window"),
    ("--cseries-paper", "--cseries-twilight", 4.5, "body text on a raised panel"),
    ("--cseries-linen", "--cseries-night", 3.0, "secondary text on a window"),
)
# The five surfaces are a ramp, and the shell stacks them in this order.
SURFACE_RAMP: tuple[str, ...] = (
    "--cseries-ink", "--cseries-night", "--cseries-twilight", "--cseries-silk", "--cseries-veil",
)
# A luminance step smaller than this is invisible — at the dark end of the ramp
# it is a contrast ratio of about 1.04 — so it must never decide the direction.
RAMP_EPSILON = 0.002


def check_palette(tokens: dict[str, str]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a palette that is COMPLETE but may not be usable.
    Both empty when the declared values are readable, and also when there is
    nothing legible to measure.

    ERRORS — text that does not clear its surface (CONTRAST_PAIRS). That is the
    defect that made a real artifact unusable, and it is not a matter
    of taste: the shell composites those exact pairs.

    WARNINGS — the five surfaces do not run one way (SURFACE_RAMP). This was an
    error in the first cut and that was wrong twice over. A dark UI whose
    desktop base is deliberately lighter than its chrome clears every contrast
    pair and was being refused for a choice a designer is allowed to make; and
    a genuinely unreadable palette always trips a contrast rule too, so the
    ramp never refused anything on its own. It is real signal about how the
    shell stacks its surfaces, which belongs in the host's hands, not in a
    gate.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not tokens:
        return errors, warnings

    for fg, bg, floor, what in CONTRAST_PAIRS:
        if fg not in tokens or bg not in tokens:
            continue
        ratio = contrast_ratio(tokens[fg], tokens[bg])
        if ratio is None or ratio >= floor:
            continue
        errors.append(
            f"{fg} on {bg} measures {ratio:.2f}:1 ({what}) — the floor is {floor}:1. "
            f"Declared: {fg}:{tokens[fg]}, {bg}:{tokens[bg]}. "
            "The shell composites these two, so this is unreadable on screen, "
            "not a matter of taste")

    ramp = [(name, relative_luminance(c))
            for name, c in ((n, parse_color(tokens.get(n, ""))) for n in SURFACE_RAMP)
            if c is not None and c[3] >= ALPHA_OPAQUE]
    if len(ramp) >= 3:
        falls = rises = None
        for (n1, l1), (n2, l2) in zip(ramp, ramp[1:]):
            if l2 - l1 > RAMP_EPSILON and rises is None:
                rises = (n1, l1, n2, l2)
            elif l1 - l2 > RAMP_EPSILON and falls is None:
                falls = (n1, l1, n2, l2)
        if falls and rises:
            warnings.append(
                "the surfaces are not ordered dark → light as the shell expects: "
                + " → ".join(SURFACE_RAMP) + f" falls at {falls[0]} → {falls[2]} "
                f"(luminance {falls[1]:.3f} → {falls[3]:.3f}) and rises at "
                f"{rises[0]} → {rises[2]} ({rises[1]:.3f} → {rises[3]:.3f}). "
                "Check the taskbar and a window after set_desktop — the shell "
                "stacks them in that order and will look inside out if this was "
                "not deliberate")
    return errors, warnings


def detect_mode(html: str) -> str | None:
    """'wallpaper' | 'desktop' from the static head, or None when undeclared —
    the same 64 KB window and first-tag rule the shell and the token parser
    use, so the three never disagree."""
    m = _MODE_RE.search(str(html or "")[:MAX_HEAD_BYTES])
    if not m:
        return None
    return (m.group(1) or m.group(2)).lower()


def parse_bar(html: str) -> dict | None:
    """The bar a document declares, from its static head (same 64 KB window and
    first-tag rule as the mode): {"edge", "size"} when well formed,
    {"invalid": <value>} when present but malformed, None when absent."""
    m = _BAR_META_RE.search(str(html or "")[:MAX_HEAD_BYTES])
    if not m:
        return None
    raw = m.group(1) if m.group(1) is not None else (m.group(2) or "")
    v = _BAR_VALUE_RE.match(raw)
    if not v:
        return {"invalid": raw[:40]}
    size = int(v.group(2))
    if not BAR_MIN_PX <= size <= BAR_MAX_PX:
        # Well formed, outside the range: say so, rather than "malformed".
        return {"invalid": raw[:40], "size_px": size}
    return {"edge": v.group(1).lower(), "size": size}


def check_network_document(html: str) -> dict:
    """The floor for a host-authored NETWORK MAP. Returns the same shape as
    check_document: {ok, mode, errors, warnings, tokens}.

    One rule carries this surface: the nodes come from the bridge. A map of
    your own network is the artifact where a plausible fiction does the most
    damage — a beautiful diagram of machines that do not exist, shown to
    someone asking where their data actually goes. "No mocks or stubs" is a
    safety rule here, not a quality one.

    Tokens are NOT required (unlike the desktop): the map renders inside the
    Network app's scene box and the shell reads nothing from it, so demanding a
    palette would be a rule that changes nothing.
    """
    doc = str(html or "")
    errors: list[str] = []
    warnings: list[str] = []

    if not _NETWORK_MODE_RE.search(doc[:MAX_HEAD_BYTES]):
        errors.append(
            "no mode declared — add <meta name=\"cseries-network\" content=\"map\"> to <head> "
            "so a document written for another surface cannot be mounted here by mistake")

    if _EXTERNAL_RE.search(doc):
        errors.append(
            "external reference (http(s):// src/href/url()/@import) — inline every asset "
            "(system fonts, CSS gradients, SVG, canvas). The frame's CSP blocks these at "
            "load time anyway, so they would render broken; this refuses them at write "
            "time so you find out now rather than on screen")

    uses_bridge = ("CSeriesNetwork" in doc) or ("data-cseries-nodes" in doc)
    if not uses_bridge:
        errors.append(
            "nothing draws the real network — read CSeriesNetwork.nodes (and .edges / .host) or "
            "drop in <div data-cseries-nodes></div>. A map that does not read the bridge is a "
            "picture, not your network")

    fake_items = len(_FAKE_NODE_ITEM_RE.findall(doc))
    if fake_items >= 3:
        errors.append(
            f"a literal list of {fake_items} node-shaped objects ({{id, label, …}}) — the nodes "
            "come from CSeriesNetwork at runtime, never from a list you typed. This is the one "
            "thing this surface must never do")

    stubs = sorted({m.group(0).lower() for m in _STUB_TEXT_RE.finditer(doc)})
    if stubs:
        warnings.append("stub text found (" + ", ".join(stubs) + ") — no placeholders on a map")
    dead = len(_DEAD_LINK_RE.findall(doc))
    if dead:
        warnings.append(f"{dead} dead control(s) (href=\"#\" / onclick that returns false) — "
                        "every control must do something real or not exist")
    if "CSeriesNetwork" in doc and ".on(" not in doc and "cseries:network" not in doc:
        warnings.append("CSeriesNetwork is referenced but nothing subscribes with .on(cb) — "
                        "machines go up and down while the map is open")

    return {"ok": not errors, "mode": "map" if not errors else None,
            "errors": errors, "warnings": warnings, "tokens": extract_tokens(doc)}


def check_document(html: str, *, installed_ids: set[str] | frozenset[str] | None = None) -> dict:
    """The floor. Returns {ok, mode, errors, warnings, tokens}.

    errors  — the document is refused as a desktop until these are fixed.
    warnings — it mounts, but the host should fix them in the same turn.

    Everything is decided from the static document: the frame it renders in
    has no network and no privileged state, so a static read of what the
    document declares and references IS what the user will get.

    The palette is checked two ways: every required token is DECLARED, and the
    declared values are READABLE against each other (check_palette). Unreadable
    text is an error; surface ORDER is a warning. That second half exists
    because the first one passed a desktop that rendered black on black.
    """
    doc = str(html or "")
    errors: list[str] = []
    warnings: list[str] = []

    mode = detect_mode(doc)
    if mode is None and _MODE_RE.search(doc):
        # Declared, but past the window the shell reads. In a rendered
        # document that is usually an image the platform put in (an
        # /api/images name becomes the image itself), which the Host's own
        # file does not show, so "add the meta" would send it looking for
        # something that is already there.
        errors.append(_past_head_window("<meta name=\"cseries-desktop\">"))
    elif mode is None:
        errors.append(
            "no mode declared — add <meta name=\"cseries-desktop\" content=\"desktop\"> "
            "(you draw the apps) or content=\"wallpaper\" (the shell draws them) to <head>")

    tokens, dropped = parse_tokens(doc)
    tokens_past_window = (not _TOKENS_META_RE.search(doc[:MAX_HEAD_BYTES])
                          and bool(_TOKENS_META_RE.search(doc)))
    if tokens_past_window:
        errors.append(_past_head_window("<meta name=\"cseries-tokens\">"))
    # A declaration the grammar or the bounds left out is named, never
    # dropped silently: the design mounts without it, and the host fixes it.
    _dropped_warning = dropped_warning(dropped)
    if _dropped_warning:
        warnings.append(_dropped_warning)
    missing = [t for t in REQUIRED_TOKENS if t not in tokens]
    if tokens_past_window:
        pass
    elif not tokens:
        errors.append(
            "no design tokens — add <meta name=\"cseries-tokens\" content=\"--cseries-ink:#…; …\"> "
            "declaring at least: " + ", ".join(REQUIRED_TOKENS))
    elif missing:
        errors.append("design tokens incomplete — also declare: " + ", ".join(missing))
    # Complete is not the same as usable: one real artifact declared all
    # eleven and painted black text on black. Unreadable TEXT is an error — a
    # desktop nobody can read is not a design decision. Surface ORDER is a
    # warning: a designer may legitimately want the base lighter than the
    # chrome, and that choice trips no contrast rule.
    _pal_errors, _pal_warnings = check_palette(tokens)
    errors += _pal_errors
    warnings += _pal_warnings

    if _EXTERNAL_RE.search(doc):
        errors.append(
            "external reference (http(s):// src/href/url()/@import) — the desktop frame has no "
            "network; inline every asset (system fonts, CSS gradients, SVG, canvas)")

    uses_bridge = ("CSeriesDesktop" in doc) or ("data-cseries-apps" in doc) or bool(_APP_ATTR_RE.search(doc))
    if mode == "desktop" and not uses_bridge:
        errors.append(
            "mode is 'desktop' but nothing draws the real apps — render CSeriesDesktop.apps with "
            "data-cseries-app=\"<id>\" on each tile, or drop in <div data-cseries-apps></div>")

    fake_items = len(_FAKE_APP_ITEM_RE.findall(doc))
    if fake_items >= 3:
        errors.append(
            f"a literal list of {fake_items} app-shaped objects ({{id, name, …}}) — apps come "
            "from CSeriesDesktop.apps / .all at runtime, never from a list you typed")

    if installed_ids:
        bad = sorted({a for a in _APP_ATTR_RE.findall(doc)
                      if a and not a.startswith("{") and a not in installed_ids})
        if bad:
            errors.append("data-cseries-app names apps that are not installed: " + ", ".join(bad))
    for a in _APP_ATTR_RE.findall(doc):
        if a and not a.startswith("{") and not _APP_ID_RE.match(a):
            errors.append(f"data-cseries-app id {a!r} is not a valid app id")
            break

    if mode == "wallpaper" and uses_bridge:
        warnings.append(
            "mode is 'wallpaper' (the shell draws the icon column) but the document also draws "
            "apps — declare 'desktop' or remove the tiles, otherwise there are two icon sets")

    dead = len(_DEAD_LINK_RE.findall(doc))
    if dead:
        warnings.append(f"{dead} dead control(s) (href=\"#\" / onclick that returns false) — "
                        "every control must do something real or not exist")
    stubs = sorted({m.group(0).lower() for m in _STUB_TEXT_RE.finditer(doc)})
    if stubs:
        warnings.append("stub text found (" + ", ".join(stubs) + ") — no placeholders on a desktop")

    if mode == "desktop" and "CSeriesDesktop" in doc and ".on(" not in doc and "cseries:desktop" not in doc:
        warnings.append("CSeriesDesktop is referenced but nothing subscribes with .on(cb) — "
                        "the app set, running dots and presence change while the desktop is up")

    # The bar. Declaring it takes the shell's taskbar away, so what the taskbar
    # did has to be here: a way back to every open window, and the launcher.
    bar = parse_bar(doc)
    # Read the controls from the document with its HTML comments removed — a
    # comment naming data-cseries-dock is not a dock.
    live = _COMMENT_RE.sub("", doc)
    if bar is not None and "invalid" in bar:
        if bar.get("size_px") is not None:
            errors.append(
                f"cseries-bar {bar['invalid']!r} asks for a {bar['size_px']} px bar; a bar is "
                f"{BAR_MIN_PX}–{BAR_MAX_PX} px thick — write a size in that range, e.g. "
                f"content=\"bottom 48\"")
        else:
            errors.append(
                f"cseries-bar {bar['invalid']!r} is not an edge and a size — write e.g. "
                f"content=\"bottom 48\" (edge: {', '.join(BAR_EDGES)}; size "
                f"{BAR_MIN_PX}–{BAR_MAX_PX} px)")
        bar = None
    elif bar is not None:
        if mode != "desktop":
            errors.append(
                "cseries-bar needs mode 'desktop' — a wallpaper sits under the shell's own icons "
                "and taskbar, so it cannot take the bar over")
        if not _BAR_SWITCH_RE.search(live):
            errors.append(
                "the document takes the bar but offers no way back to an open window — draw "
                "CSeriesDesktop.dock (or drop in <div data-cseries-dock></div>) and/or "
                "CSeriesDesktop.windows with data-cseries-window=\"<id>\"; without it a window you "
                "minimize is gone")
        if not _BAR_LAUNCH_RE.search(live):
            errors.append(
                "the document takes the bar but offers no launcher — a button calling "
                "CSeriesDesktop.openLauncher() or carrying data-cseries-launcher")
        if not _BAR_SHOW_RE.search(live):
            warnings.append(
                "the bar has no show-desktop control (CSeriesDesktop.toggleDesktop() or "
                "data-cseries-show-desktop) — with windows open there is no one-click way back "
                "to the desktop")

    return {"ok": not errors, "mode": mode, "errors": errors, "warnings": warnings,
            "tokens": tokens, "bar": bar}


def format_report(report: dict) -> str:
    """One readable block for a tool result or an HTTP 422 body."""
    lines: list[str] = []
    if report.get("errors"):
        lines.append("REFUSED — the document does not meet the desktop floor:")
        lines += [f"  ✗ {e}" for e in report["errors"]]
    if report.get("warnings"):
        lines.append("Fix before the user notices:" if not report.get("errors") else "Also:")
        lines += [f"  ! {w}" for w in report["warnings"]]
    if lines:
        lines.append("Start from the template (default-skills/desktop.md, section 5) — it passes as written.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# The brief — what a designing agent (the host, or a coding harness) gets
# ─────────────────────────────────────────────────────────────────────────────
DESIGN_BRIEF = """\
CSERIES DESKTOP / SHELL DESIGN — THE CONTRACT (mandatory; the platform refuses anything less)

You are designing the user's whole machine, not a picture of one. The shell — desktop,
taskbar, windows, dock, every app — takes its colours, type, radii and chrome sizes from
the design tokens YOUR document declares. One self-contained index.html is the design.

REQUIRED, or the document is refused when it is set as the desktop:
  1. <meta name="cseries-desktop" content="desktop">  (you draw the app tiles)
     or content="wallpaper"                        (a live backdrop; the shell draws its own icons)
  2. <meta name="cseries-tokens" content="…"> declaring AT LEAST:
       --cseries-ink --cseries-night --cseries-twilight --cseries-silk --cseries-veil   (surfaces, dark→light)
       --cseries-mist                                                           (hairlines)
       --cseries-paper --cseries-linen --cseries-ash                                (text, strong→muted)
       --cseries-ember --cseries-dusk                                             (accent fill, accent text)
     Values: hex, rgb()/rgba()/hsl()/hsla(), CSS colour names, numbers with units, or font
     stacks. Nothing that reaches out (url(), var()). The desktop's own palette and these
     tokens are ONE palette, and the floor MEASURES that palette in every colour form it
     accepts — declaring the names is not enough:
       REFUSED: --cseries-paper below 4.5:1 against --cseries-ink, --cseries-night or
         --cseries-twilight, or --cseries-linen below 3:1 against --cseries-night. The refusal
         carries the pair and the measured ratio. The shell paints windows from
         --cseries-night and their text from --cseries-paper, so getting this the wrong way
         round is black text on black across the machine, not a taste call.
       WARNED: the five surfaces ink → night → twilight → silk → veil not running one way.
         The shell stacks them in that order, so check the taskbar and a window after
         set_desktop; deliberate is fine, accidental looks inside out.
  3. In 'desktop' mode, apps come from the shell at runtime — never a list you typed:
       window.CSeriesDesktop.apps  [{id, name, description, icon (inline markup — insert as-is),
                                   running, focused}]  — the user's desktop set, their order
       window.CSeriesDesktop.all   [{id, name}]           — every installed app (any may launch)
       Put data-cseries-app="<id>" on whatever you draw for an app; a trusted click launches it.
       Or drop <div data-cseries-apps></div> and restyle the grid the shell fills in.
       Subscribe: CSeriesDesktop.on(function (S) { … }) fires now and on every change.
  4. No external assets of any kind. The frame has no network. System fonts, CSS gradients,
     inline SVG, canvas, and the Host's own images named /api/images/<id> (src or url()),
     which the platform puts into the document when it renders it.
  5. No mocks, no stubs, no dead controls. Every button does a real thing or does not exist.
     Do NOT draw: a context menu, fake windows, fake mail, fake music, fake notifications,
     fake balances. The shell draws the real windows and menus — restyle them with tokens.
     The BAR is yours only if you declare it (below); otherwise the shell draws its taskbar
     and you draw none.

THE BAR (the design draws it — the template does):
  <meta name="cseries-bar" content="bottom 52">  — the edge (top|bottom|left|right) and its
  thickness in px (28–120). The shell then hides its own taskbar and keeps every window out of
  that strip, maximized ones included; your document (full-screen underneath) draws the bar
  there. Needs mode 'desktop'. Declaring it means drawing what the taskbar did, all real:
    REQUIRED: a way back to every open window — CSeriesDesktop.dock [{id, name, icon, pinned,
      running, focused}] with data-cseries-activate="<id>" (click opens / focuses / minimizes;
      right-click is the shell's pin / close menu), or <div data-cseries-dock></div>; and
      CSeriesDesktop.windows [{id, app_id, title, icon, focused, minimized}] with
      data-cseries-window="<id>" for windows that belong to no app.
    REQUIRED: a launcher — data-cseries-launcher (or CSeriesDesktop.openLauncher()).
    WARNED without: show-desktop — data-cseries-show-desktop (CSeriesDesktop.desktopShown says
      whether windows are held down).
    Also yours to place: data-cseries-help (the shell's help), CSeriesDesktop.machine (the memory
      line, e.g. "1.2G of 4G"), CSeriesDesktop.host. Put data-cseries-machine on the memory line (or
      call CSeriesDesktop.openMachine()) and a click opens the shell's machine panel — memory, each
      app, every process. The shell draws that panel; the process list never enters your document.
  Draw the bar ONLY while CSeriesDesktop.ownsBar is true — on a phone the shell keeps its own
  taskbar and says so, and two bars is a bug.

OPTIONAL tokens (each one is read by the shell; nothing else is):
  --cseries-mist-strong --cseries-bright --cseries-moss --cseries-clay --cseries-gold
  --cseries-ember-soft --cseries-ember-ring --cseries-seam --cseries-seam-active
  --cseries-glass (taskbar / menu glass colour, use rgba) --cseries-glass-blur (e.g. 18px)
  --cseries-glow (accent glow colour, rgba) --cseries-tile-bg --cseries-tile-bg-2 (desktop icon tile)
  --cseries-font-body --cseries-font-display --cseries-font-mono
  --cseries-taskbar-h (e.g. 52px — the shell's own taskbar, when you do not draw the bar)
  --cseries-window-head-h (e.g. 36px)
  --cseries-radius-sm/-md/-lg/-xl  --cseries-icon-size

WHAT THE SHELL GIVES YOUR DOCUMENT (window.CSeriesDesktop, injected before your scripts run):
  Every mode: .host (display name) .phone .reducedMotion .mode  ·  .on(cb) → unsubscribe
  ·  .launch(id) / .openLauncher() (both need a real click/key in the frame within 1 s)
  ·  .render(el) draws the default grid.
  DESKTOP MODE ONLY: .apps .all .presence (one-line "what the host is doing"). In a
  wallpaper those are empty — [], [] and "" — because the shell keeps drawing the icon
  column and the presence line on top of a wallpaper, so it is never handed an inventory
  it cannot use. Do not write a wallpaper that reads them.
  Right-click anywhere = the shell's own menu. Escape closes it. Nothing else crosses.

REAL WIDGETS you can build from that, today: clock/date/greeting · reduced-motion-aware
ambient motion · phone-aware layout (read .phone) — and in DESKTOP mode also the host's
name and presence sentence · the app grid with running/focused state · a "running now"
strip · a launcher button.

THE PHONE (the same document is the phone's home screen — lay it out for one):
  On a phone CSeriesDesktop.phone is true (re-sent through .on() on rotation), the shell keeps
  its own taskbar below the document (ownsBar false — draw no bar) and windows open
  full-screen. Switch on .phone inside .on() (toggle a class); use
  @media (orientation: landscape) inside it for a phone on its side. Fit 360 px wide — no
  desktop pixel positions: a centred row of fifteen 52 px tiles runs off both sides. Every
  tile inside the width: a grid (four columns), scrolling DOWN when the apps outnumber the
  screen, never sideways. Greeting / clock / presence compact at the top. Touch, not hover:
  names visible, no hover-only controls, tap targets ≥ 44 px. Landscape (~360 px tall): the
  header beside the tiles. The render check loads the document again at 360×640 as a phone:
  REFUSED there for what refuses it at desktop size (throws, shows nothing, no tile, every
  tile covered); WARNED for tiles past the side edges of the phone screen.

QUALITY: one idea carried all the way; tiles ≥ 40 px hit targets with visible names; on a
desktop screen the whole desktop set fits without scrolling; motion is cheap (one canvas, rAF,
stops under prefers-reduced-motion); text over motion gets a plate or shadow.

DELIVERY: write index.html (and variant-*.html for alternatives). Never re-type a file to copy
it — the platform copies files as an operation. The user picks a variant with one click and
the pick becomes the desktop with no model in the loop.
"""


def design_brief() -> str:
    return DESIGN_BRIEF


def looks_like_desktop_task(text: str) -> bool:
    """Whether a build/fan-out prompt is designing the desktop or shell — the
    trigger for prepending the brief to a harness prompt (server/routes/review.py).
    Conservative on purpose: 'desktop' as in 'desktop app' would over-fire, so the
    word has to sit next to design vocabulary."""
    t = str(text or "").lower()
    if "cseries-desktop" in t or "cseries-tokens" in t or "html desktop" in t:
        return True
    if re.search(r"\b(?:wallpaper|taskbar|dock|shell (?:design|theme|skin)|design tokens?)\b", t):
        return True
    return bool(re.search(r"\bdesktop\b", t) and re.search(r"\b(?:design|theme|look|vibe|skin|concept|redesign|style)\b", t))


# ─────────────────────────────────────────────────────────────────────────────
# The template — passes check_document as written; every widget is real
# ─────────────────────────────────────────────────────────────────────────────
TEMPLATE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="cseries-desktop" content="desktop">
<meta name="cseries-bar" content="bottom 52">
<meta name="cseries-tokens" content="--cseries-ink:#0a0e0f; --cseries-night:#0e1416; --cseries-twilight:#131b1d; --cseries-silk:#1a2427; --cseries-veil:#223034; --cseries-mist:#263135; --cseries-mist-strong:#3b4c51; --cseries-paper:#e5e7eb; --cseries-linen:#9ca3af; --cseries-ash:#6b7280; --cseries-ember:#4f7f6a; --cseries-dusk:#7fcfae; --cseries-bright:#9be3c2; --cseries-glass:rgba(14,20,22,0.72); --cseries-glow:rgba(127,207,174,0.45); --cseries-font-body:Inter, system-ui, sans-serif; --cseries-font-display:Inter, system-ui, sans-serif; --cseries-radius-md:6px; --cseries-taskbar-h:52px">
<title>Threshold</title>
<style>
  /* ONE palette: these are the same values as the cseries-tokens line above. */
  :root {
    --ink:#0a0e0f; --night:#0e1416; --twilight:#131b1d; --silk:#1a2427; --veil:#223034;
    --mist:#263135; --paper:#e5e7eb; --linen:#9ca3af; --ash:#6b7280;
    --ember:#4f7f6a; --dusk:#7fcfae; --bright:#9be3c2; --glass:rgba(14,20,22,0.72);
    --cseries-dusk:#7fcfae;            /* recolours the shell's glyph accents */
    --font:Inter, system-ui, sans-serif;
  }
  html, body { margin:0; height:100%; overflow:hidden; background:var(--ink); color:var(--paper);
    font:14px/1.45 var(--font); user-select:none; -webkit-user-select:none; }
  canvas#sky { position:fixed; inset:0; width:100%; height:100%; display:block; }

  /* Header: greeting, clock, the host's presence — all live. */
  header { position:fixed; left:48px; top:40px; right:48px; display:flex; align-items:flex-end;
    justify-content:space-between; gap:24px; pointer-events:none; }
  h1 { margin:0; font-weight:500; font-size:34px; letter-spacing:-.01em; }
  #presence { margin:6px 0 0; color:var(--linen); font-size:13px; letter-spacing:.04em; }
  #clock { font-size:44px; font-weight:300; letter-spacing:-.02em; line-height:1; text-align:right; }
  #date { color:var(--linen); font-size:13px; text-align:right; margin-top:6px; letter-spacing:.04em; }

  /* The app grid — every tile is a real installed app, drawn from CSeriesDesktop.apps. */
  .apps { position:fixed; left:48px; top:150px; right:48px; bottom:120px;
    display:grid; grid-template-columns:repeat(auto-fill, 112px); grid-auto-rows:112px;
    gap:14px; align-content:start; }
  .tile { position:relative; display:flex; flex-direction:column; align-items:center; justify-content:center;
    gap:8px; border:1px solid var(--mist); border-radius:14px; cursor:pointer; color:var(--linen);
    background:linear-gradient(160deg, rgba(34,48,52,.82), rgba(14,20,22,.88));
    transition:transform .18s ease, border-color .18s ease, color .18s ease; padding:0; font:inherit; }
  .tile:hover { transform:translateY(-3px); border-color:var(--ember); color:var(--paper); }
  .tile:focus-visible { outline:2px solid var(--dusk); outline-offset:2px; }
  .tile .ic { width:44px; height:44px; display:grid; place-items:center; }
  .tile .ic svg, .tile .ic img { width:36px; height:36px; }
  .tile .ic .sk-app-letter { font-size:22px; font-weight:600; }
  .tile .nm { font-size:11px; letter-spacing:.08em; text-transform:uppercase; }
  .tile .dot { position:absolute; bottom:8px; left:50%; transform:translateX(-50%); width:5px; height:5px;
    border-radius:50%; background:transparent; }
  .tile.is-running .dot { background:var(--dusk); box-shadow:0 0 6px var(--dusk); }
  .tile.is-focused { border-color:var(--dusk); }

  /* The bar — this document draws it (cseries-bar above: bottom, 52 px) and the
     shell keeps windows out of that strip. Every control is the shell's own:
     the launcher, the dock (pinned + running apps), shell windows, the host,
     the machine, help, show-desktop. Hidden whenever the shell keeps its own
     taskbar (a phone), so there is never a second bar. */
  #bar { position:fixed; left:0; right:0; bottom:0; height:52px; display:none; align-items:center; gap:6px;
    padding:0 12px; background:var(--glass); border-top:1px solid var(--mist);
    backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px); font-size:12px; color:var(--linen); }
  body.owns-bar #bar { display:flex; }
  #bar button { font:inherit; color:inherit; background:none; border:0; cursor:pointer; border-radius:8px;
    height:40px; min-width:40px; display:inline-grid; place-items:center; padding:0 8px; }
  #bar button:hover { background:var(--silk); color:var(--paper); }
  #bar button:focus-visible { outline:2px solid var(--dusk); outline-offset:1px; }
  #launch svg { width:18px; height:22px; }
  #dock, #wins { display:flex; align-items:center; gap:2px; min-width:0; overflow:hidden; }
  #dock button { position:relative; }
  #dock button span.ic { width:26px; height:26px; display:grid; place-items:center; }
  #dock button span.ic svg, #dock button span.ic img { width:22px; height:22px; }
  #dock button .ic .sk-app-letter { font-size:14px; font-weight:600; }
  #dock button.on::after { content:""; position:absolute; left:50%; bottom:3px; width:4px; height:4px; margin-left:-2px;
    border-radius:50%; background:var(--dusk); box-shadow:0 0 6px var(--dusk); }
  #dock button.focus { background:var(--veil); color:var(--paper); }
  #wins button.min { opacity:.6; }
  .grow { flex:1; }
  #who { display:flex; align-items:center; gap:8px; padding:0 8px; white-space:nowrap; }
  #who i { width:7px; height:7px; border-radius:50%; background:var(--dusk); box-shadow:0 0 6px var(--dusk); }
  #machine { white-space:nowrap; letter-spacing:.04em; }
  #machine[hidden] { display:none; }
  #showdesk.on { color:var(--dusk); }

  /* Phone (CSeriesDesktop.phone, desktop skill §2b). The same document, laid
     out for a phone: the shell keeps its own taskbar below this frame, so no
     strip is kept for a bar. The page becomes a column — a compact header,
     then the tiles in four columns filling the width; when there are more
     apps than the screen holds, the grid scrolls down, never off the side.
     Turned on its side, the header moves beside the tiles. No hover lift:
     a touch screen has no hover. */
  body.phone { display:flex; flex-direction:column; }
  body.phone header { position:static; margin:20px 20px 14px; flex-direction:column; align-items:flex-start; gap:8px; }
  body.phone h1 { font-size:22px; }
  body.phone #clock { font-size:30px; text-align:left; }
  body.phone #date { text-align:left; margin-top:4px; }
  body.phone .apps { position:static; flex:1; min-height:0; margin:0 14px 14px; overflow-y:auto;
    grid-template-columns:repeat(4, minmax(0, 1fr)); grid-auto-rows:84px; gap:10px; }
  body.phone .tile .nm { max-width:100%; padding:0 4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    font-size:10px; letter-spacing:.04em; }
  body.phone .tile:hover { transform:none; }
  @media (orientation: landscape) {
    body.phone { flex-direction:row; }
    body.phone header { width:34%; flex:none; margin:20px 8px 20px 24px; }
    body.phone .apps { margin:14px 14px 14px 0; grid-template-columns:repeat(auto-fill, minmax(76px, 1fr)); }
  }
</style>
</head>
<body>
<canvas id="sky" aria-hidden="true"></canvas>
<header>
  <div><h1 id="greet">Good day.</h1><p id="presence"></p></div>
  <div><div id="clock">--:--</div><div id="date"></div></div>
</header>
<div class="apps" id="apps" role="list"></div>
<nav id="bar" aria-label="Taskbar">
  <button id="launch" type="button" title="All apps" aria-label="All apps" data-cseries-launcher>
    <svg viewBox="-0.5 -0.5 17 21" aria-hidden="true"><rect x="0" y="0" width="16" height="4" rx="1.5" fill="none" style="stroke:var(--ember)"/><rect x="0" y="8" width="16" height="4" rx="1.5" style="fill:var(--bright)"/><rect x="0" y="16" width="16" height="4" rx="1.5" fill="none" style="stroke:var(--ember)"/></svg>
  </button>
  <div id="dock"></div>
  <div id="wins"></div>
  <span class="grow"></span>
  <span id="who"><i></i><span id="who-name"></span></span>
  <button type="button" id="machine" title="Your machine — what is running" data-cseries-machine></button>
  <button type="button" title="Help" aria-label="Help" data-cseries-help>?</button>
  <button id="showdesk" type="button" title="Show desktop" aria-label="Show desktop" data-cseries-show-desktop>
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="1.5" y="2.5" width="13" height="9" rx="1.5"/><path d="M5 14h6"/></svg>
  </button>
</nav>
<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  // ── Clock + greeting (real time, ticks each minute) ──────────────────
  function tick() {
    var d = new Date(), h = d.getHours();
    $("clock").textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    $("date").textContent = d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
    var name = CSeriesDesktop.host ? " " + CSeriesDesktop.host + " is here." : "";
    $("greet").textContent = (h < 5 ? "Late night." : h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening.") + name;
  }
  tick(); setInterval(tick, 15000);

  // ── Apps, the bar, presence — from the shell, re-drawn on change ──────
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  // Rewrite a region only when its markup changed, and then only what changed:
  // the shell re-posts state on every window and dock event, and a button
  // replaced between its press and its release loses the click. A control that
  // is still wanted stays the same element (its class and label are updated in
  // place); only controls that came or went are added or removed.
  var drawn = {};
  function keyOf(n) {
    if (n.nodeType !== 1) return "";
    for (var i = 0; i < n.attributes.length; i++)
      if (n.attributes[i].name.indexOf("data-cseries-") === 0)
        return n.nodeName + " " + n.attributes[i].name + "=" + n.attributes[i].value;
    return "";
  }
  // Bring one wanted node into an existing one, in place: attributes, then
  // children by position. Nothing is re-parsed from text, so markup is never
  // serialized and parsed a second time; a child that is the same kind of node
  // is updated where it stands (so a press on a label survives the label being
  // rewritten) and any other is swapped.
  function mend(have, want) {
    var i, a, h, w;
    if (want.nodeType !== 1) { if (have.nodeValue !== want.nodeValue) have.nodeValue = want.nodeValue; return; }
    for (i = 0; i < want.attributes.length; i++) {
      a = want.attributes[i];
      if (have.getAttribute(a.name) !== a.value) have.setAttribute(a.name, a.value);
    }
    for (i = have.attributes.length - 1; i >= 0; i--)
      if (!want.hasAttribute(have.attributes[i].name)) have.removeAttribute(have.attributes[i].name);
    h = Array.prototype.slice.call(have.childNodes);
    w = Array.prototype.slice.call(want.childNodes);
    for (i = 0; i < w.length; i++) {
      if (i >= h.length) have.appendChild(w[i]);
      else if (h[i].nodeName === w[i].nodeName && h[i].namespaceURI === w[i].namespaceURI) mend(h[i], w[i]);
      else have.replaceChild(w[i], h[i]);
    }
    for (i = w.length; i < h.length; i++) have.removeChild(h[i]);
  }
  // A region is its controls, each matched by its first data-cseries-* attribute
  // (kind and value, so another window with the same label is another control).
  // Whatever is no control (text, an element with no such attribute) is replaced
  // each time, as it always was; a duplicate of a control is a new element.
  function put(id, html) {
    if (drawn[id] === html) return;
    drawn[id] = html;
    var box = $(id), t = document.createElement("template"), kept = {}, order = [], i, k, el;
    t.innerHTML = html;
    var next = Array.prototype.slice.call(t.content.childNodes), now = Array.prototype.slice.call(box.childNodes);
    for (i = 0; i < next.length; i++) order.push(keyOf(next[i]));
    for (i = 0; i < now.length; i++) {
      k = keyOf(now[i]);
      if (k && order.indexOf(k) >= 0 && !kept[k]) kept[k] = now[i]; else box.removeChild(now[i]);
    }
    for (i = 0; i < next.length; i++) {
      el = order[i] && kept[order[i]];
      if (el) { delete kept[order[i]]; mend(el, next[i]); } else el = next[i];
      if (box.childNodes[i] !== el) box.insertBefore(el, box.childNodes[i] || null);
    }
  }
  function draw(S) {
    document.body.classList.toggle("phone", !!S.phone);
    document.body.classList.toggle("owns-bar", !!CSeriesDesktop.ownsBar);
    $("presence").textContent = S.presence || "";
    put("apps", S.apps.map(function (a) {
      return '<button class="tile' + (a.running ? " is-running" : "") + (a.focused ? " is-focused" : "") +
        '" type="button" role="listitem" data-cseries-app="' + esc(a.id) + '" title="' + esc(a.description || a.name) + '">' +
        '<span class="ic">' + (a.icon || "") + "</span><span class=\\"nm\\">" + esc(a.name) + '</span><span class="dot"></span></button>';
    }).join(""));
    // The dock: pinned apps and running ones — a click opens, focuses or
    // minimizes; a right-click is the shell's pin / close menu.
    put("dock", S.dock.map(function (a) {
      return '<button type="button" class="' + (a.running ? "on" : "") + (a.focused ? " focus" : "") +
        '" data-cseries-activate="' + esc(a.id) + '" title="' + esc(a.name) + '"><span class="ic">' + (a.icon || "") + "</span></button>";
    }).join(""));
    // Shell windows that belong to no app (the dock covers app windows).
    put("wins", S.windows.filter(function (w) { return !w.app_id; }).map(function (w) {
      return '<button type="button" class="' + (w.minimized ? "min" : "") + '" data-cseries-window="' + esc(w.id) +
        '" title="' + esc(w.title) + '">' + esc(w.title) + "</button>";
    }).join(""));
    $("who-name").textContent = CSeriesDesktop.host || "";
    $("machine").textContent = S.machine || "";
    $("machine").hidden = !S.machine;
    $("showdesk").classList.toggle("on", !!CSeriesDesktop.desktopShown);
    tick();
  }
  CSeriesDesktop.on(draw);

  // ── Ambient sky: one canvas, few particles, honours reduced motion ─────
  // ONE loop, ever: rafId is the running guard. motion() is called on every
  // shell state post (window open/close/focus, presence, layout), so without
  // the guard each post would start another permanent parallel loop.
  var c = $("sky"), ctx = c.getContext("2d"), pts = [], W = 0, H = 0, still = false, rafId = 0;
  // The sky is drawn in the palette above (read once), so a new palette
  // recolours it with no edit here.
  var css = getComputedStyle(document.documentElement);
  function tone(name, a, fallback) {
    var v = (css.getPropertyValue(name) || fallback).trim(), m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
    if (!m) return v;
    var h = m[1].length === 3 ? m[1].replace(/./g, "$&$&") : m[1];
    return "rgba(" + parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," + parseInt(h.slice(4, 6), 16) + "," + a + ")";
  }
  var glow0 = tone("--dusk", 0.08, "#7fcfae"), glow1 = tone("--dusk", 0, "#7fcfae"), dot = tone("--paper", 0.55, "#e5e7eb");
  function size() { W = c.width = innerWidth * devicePixelRatio; H = c.height = innerHeight * devicePixelRatio; }
  function seed() { pts = []; for (var i = 0; i < 90; i++) pts.push({ x: Math.random() * W, y: Math.random() * H,
    r: (Math.random() * 1.4 + .4) * devicePixelRatio, v: (Math.random() * .08 + .02) * devicePixelRatio, a: Math.random() * Math.PI * 2 }); }
  function frame() {   // draws one frame; never schedules
    ctx.clearRect(0, 0, W, H);
    var g = ctx.createRadialGradient(W * .5, H * 1.15, 0, W * .5, H * 1.15, H * .9);
    g.addColorStop(0, glow0); g.addColorStop(1, glow1);
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = dot;
    for (var i = 0; i < pts.length; i++) { var p = pts[i];
      if (!still) { p.y -= p.v; p.a += .01; if (p.y < -4) { p.y = H + 4; p.x = Math.random() * W; } }
      ctx.globalAlpha = .35 + Math.sin(p.a) * .25; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.283); ctx.fill(); }
    ctx.globalAlpha = 1;
  }
  function loop() { rafId = 0; frame(); if (!still) rafId = requestAnimationFrame(loop); }
  function motion() {
    still = CSeriesDesktop.reducedMotion || (matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
    if (still) { if (rafId) { cancelAnimationFrame(rafId); rafId = 0; } frame(); }
    else if (!rafId) rafId = requestAnimationFrame(loop);
  }
  addEventListener("resize", function () { size(); seed(); if (still) frame(); });
  size(); seed(); motion();
  CSeriesDesktop.on(motion);
})();
</script>
</body>
</html>
"""


def template_html() -> str:
    return TEMPLATE_HTML


# The template's own short names for the palette it draws with (its :root
# block), keyed by the --cseries-* token that carries each value.
_TEMPLATE_VARS = (
    ("--ink", "--cseries-ink"), ("--night", "--cseries-night"), ("--twilight", "--cseries-twilight"),
    ("--silk", "--cseries-silk"), ("--veil", "--cseries-veil"), ("--mist", "--cseries-mist"),
    ("--paper", "--cseries-paper"), ("--linen", "--cseries-linen"), ("--ash", "--cseries-ash"),
    ("--ember", "--cseries-ember"), ("--dusk", "--cseries-dusk"), ("--bright", "--cseries-bright"),
    ("--glass", "--cseries-glass"), ("--cseries-dusk", "--cseries-dusk"),
    ("--font", "--cseries-font-body"),
)


def template_with_tokens(tokens: dict[str, str], title: str = "") -> str:
    """The template in another palette — an OPERATION, never a model retyping
    the document (switching a shell is an op). The
    cseries-tokens meta carries `tokens` (merged over the template's own, so an
    unnamed optional token keeps its template value) and the :root block the
    template draws with is rewritten from the same values: one palette.
    Values must already be token-grammar clean (desktop_tokens); `title` is
    plain text."""
    base = extract_tokens(TEMPLATE_HTML)
    merged = {**base, **{k: v for k, v in (tokens or {}).items() if k.startswith("--cseries-")}}
    meta = "; ".join(f"{k}:{v}" for k, v in merged.items())
    doc = re.sub(r'(<meta name="cseries-tokens" content=")[^"]*(")',
                 lambda m: m.group(1) + meta.replace('"', "'") + m.group(2), TEMPLATE_HTML, count=1)
    root_vars = []
    for short, token in _TEMPLATE_VARS:
        if token in merged:
            root_vars.append(f"{short}:{merged[token]};")
    root = ":root {\n    " + " ".join(root_vars) + "\n  }"
    doc = re.sub(r":root \{.*?\n  \}", lambda m: root, doc, count=1, flags=re.S)
    if title:
        safe = re.sub(r"[<>&\"]", "", str(title))[:80]
        doc = doc.replace("<title>Threshold</title>", f"<title>{safe}</title>", 1)
    return doc
