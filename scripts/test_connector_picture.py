#!/usr/bin/env python3
"""The connector picture renderer's gate and child, from the source tree.

server/connectors/picture.py turns a page a Host wrote (a ```visual fence body,
or an artifact's rendered page) into a PNG with Takumi (pytakumi) in a
confined child, or refuses with a reason so the caller sends a link. What
this file pins, with no pytakumi installed:

  1. The first line, the page gate: a card in the measured subset is accepted
     and re-serialised, with every tag it may use put back to content-box; a
     typical visual fence body (a <style> block and a flex card in
     var(--cseries-…) tokens) is split into markup and CSS, with the
     <html>/<head>/<body>/<meta> structure dropped; grid, a property off the
     list, an inline SVG shape, <img src>, CSS url(), @import, a leftover
     var(, a font other than the shipped one, a colour name CSS does not
     have, italic (em, i, font-style), bolder/lighter, any attribute on
     <body> or <head> (or on <html> but lang) and nesting deeper than
     MAX_DEPTH (it raised RecursionError out of draw_picture) all give no
     picture, while text beside an inline tag is drawn. Every value is
     handed on in lower case. The check costs under a second on the largest
     page allowed.
  2. Token values: var(--cseries-…)/var(--radius-…) take the desktop's values
     from the same token sheet the Artifacts window wears, and var(--font-…)
     is always the shipped family.
  3. The second line, on the child's answer: any warning, a single-colour
     image, or something that is not a PNG gives no picture; in the child, a
     page naming any font but the shipped one is refused, and the glyph
     table is read from the font's cmap (formats 4 and 12, checked here on a
     table built by hand), and a table whose groups or segments overlap or
     are out of order is refused rather than read for seconds.
  4. The child refuses to draw when Landlock is unavailable or below ABI 6
     (no signal scoping), before it imports anything; the paths it would
     allow for reading never touch where the Host keeps its data; its seccomp
     program, run here by a small BPF interpreter, refuses sockets, fork,
     vfork and every clone that is not a thread; it is handed only its three
     pipes and a scrubbed environment.
  5. Nothing in the renderer writes a row or a file, or touches the browser
     sidecar.
  6. REAL SIGNALS, in a private PID namespace only: a child that hangs is
     killed at the time limit and one that crashes is reported, an answer
     past the size cap is stopped at once, stderr past its cap is drained,
     a pipe held open by a left-behind process costs a bounded wait, and
     the caller keeps serving with no slot held. Where no namespace can be
     made this part is SKIPPED, loudly; it never runs on the host.

scripts/test_connector_picture_image.py draws for real inside the kernel image
under the Host container's restrictions.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_connector_picture.py
"""
import ast
import asyncio
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import types
import zlib
from pathlib import Path

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [_ROOT]

from server.connectors import picture as P  # noqa: E402

_CHILD_DIR = os.path.join(_ROOT, "server", "connectors")
sys.path.insert(0, _CHILD_DIR)
import _picture_child as C  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(name: str, ok: bool, detail="") -> None:
    global PASSES
    if ok:
        PASSES += 1
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail != "" else ""))


def _stock_sheet() -> str:
    return Path(_ROOT, "dashboard/base/css/tokens.css").read_text(encoding="utf-8")


TOKENS = P.tokens_from_sheet(_stock_sheet())

# What a Host writes in a ```visual fence: a <style> block and a flex card in
# the desktop's tokens (default-skills/artifacts.md: "write colours, type and
# radii as var(--cseries-…) / var(--font-body)").
VISUAL = """<style>
  .card { display: flex; flex-direction: column; gap: 8px; padding: 20px;
          background-color: var(--cseries-silk); color: var(--cseries-paper);
          border-radius: var(--radius-lg); font-family: var(--font-body); }
  .badge { background-color: var(--cseries-ember); padding: 4px 8px;
           border-radius: 6px; align-self: flex-start; }
  h1 { font-size: 22px; margin: 0; }
</style>
<div class="card">
  <h1>Weekly spend</h1>
  <p>Total: $42</p>
  <span class="badge">on budget</span>
</div>"""

CARD_CSS = ".c{display:flex;flex-direction:column;padding:20px;background-color:#1e293b;color:#ffffff}"


def _refused(page: str, tokens=None) -> str:
    try:
        P.check_page(page, TOKENS if tokens is None else tokens)
    except P.Refused as e:
        return str(e)
    return ""


def _accept(page: str, tokens) -> "P.CheckedPage":
    """check_page, but a refusal is a failed check here, never a crash."""
    try:
        return P.check_page(page, tokens)
    except P.Refused as e:
        check(f"accepted: {page[:50]!r}…", False, f"refused: {e}")
        return P.CheckedPage(html="", css="")


def test_gate():
    print("the page gate (first line)")
    page = _accept(VISUAL, TOKENS)
    check("a typical visual fence body (<style> + flex card in tokens) is accepted",
          page.html.startswith("<section>") and '<div class="card">' in page.html, page)
    check("its <style> block became the CSS argument, not markup",
          "<style" not in page.html and ".card{display:flex;" in page.css, page.css[-300:])
    check("var(--cseries-silk) and var(--radius-lg) took the desktop's values",
          f"background-color:{TOKENS['--cseries-silk'].lower()}" in page.css
          and f"border-radius:{TOKENS['--radius-lg']}" in page.css, page.css[-300:])
    check("var(--font-body) is the shipped family, never the sheet's font stack",
          f'font-family:"{P.SHIPPED_FONT}"' in page.css and "sans-serif" not in page.css
          and "apple-system" not in page.css, page.css[-300:])
    check("the shipped family is the root font-family, and the root wears the desktop",
          page.css.startswith(f'section{{width:720px;font-family:"{P.SHIPPED_FONT}";'
                              f"background-color:{TOKENS['--cseries-ink'].lower()};"
                              f"color:{TOKENS['--cseries-paper'].lower()}}}"), page.css[:160])

    doc = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Plan</title>'
           '<style>body{margin:0}.c{color:#ffffff}</style></head><body>'
           '<div class="c">Plan</div></body></html>')
    page = _accept(doc, TOKENS)
    check("<html>/<head>/<body>/<meta> are dropped as structure, <title> with its text",
          page.html == '<section><div class="c">Plan</div></section>', page.html)
    check("a body rule styles the wrapper that plays the body",
          "section{margin:0;}" in page.css, page.css[-200:])
    check("the caller's width is the last word on the wrapper",
          page.css.endswith("section{width:720px}"))

    page = _accept(f'<style>{CARD_CSS}</style><div class="c"><h1>Hi</h1><p>x</p></div>', {})
    check("the markup Takumi gets is re-serialised from the checked parse",
          page.html == '<section><div class="c"><h1>Hi</h1><p>x</p></div></section>', page.html)

    cases = {
        "display:grid": ('<style>.a{display:grid}</style><div class="a">x</div>', "display"),
        "a dropped property (line-height)": ('<style>.a{line-height:2}</style><div class="a">x</div>', "line-height"),
        "a dropped property (opacity)": ('<style>.a{opacity:0.5}</style><div class="a">x</div>', "opacity"),
        "an inline SVG shape": ('<div class="c"><p>Total</p><svg width="80" height="40">'
                                '<rect width="80" height="40" fill="red"/></svg></div>', "<svg>"),
        "a <style> inside an svg": ('<svg><style>.a{color:red}</style></svg><div class="a">x</div>', "<svg>"),
        "<img src>": ('<div><img src="/etc/hostname"><p>x</p></div>', "<img>"),
        "CSS url(": ('<style>.a{background-image:url(/etc/hostname)}</style><div class="a">x</div>', "url()"),
        "@import": ('<style>@import url("x.css"); .a{color:red}</style><div class="a">x</div>', "@import"),
        "@font-face": ('<style>@font-face{font-family:x;src:url(/etc/x.ttf)}</style><div>x</div>', "@font-face"),
        "a leftover var(": ('<style>.a{color:var(--my-colour)}</style><div class="a">x</div>', "var(--my-colour)"),
        "a --cseries- token the desktop does not define": (
            '<style>.a{color:var(--cseries-no-such)}</style><div class="a">x</div>', "var(--cseries-no-such)"),
        "a font other than the shipped one": ('<style>.a{font-family:Arial}</style><div class="a">x</div>', "font-family"),
        "the old shipped font, DejaVu Sans": ('<style>.a{font-family:"DejaVu Sans"}</style><div class="a">x</div>', "font-family"),
        "a font-family that is a path": ('<style>.a{font-family:"/etc/passwd"}</style><div class="a">x</div>', "font-family"),
        "a font-family that is the shipped font's own path": (
            '<style>.a{font-family:"/usr/local/share/cseries/fonts/InterVariable.ttf"}'
            '</style><div class="a">x</div>', "font-family"),
        "the font the pytakumi wheel embeds, Geist": ('<style>.a{font-family:Geist}</style><div class="a">x</div>',
                                                      "font-family"),
        "a colour name CSS does not have": ('<style>.a{color:blurple}</style><div class="a">x</div>', "color"),
        "a gradient stop named outside CSS": (
            '<style>.a{background-image:linear-gradient(red, blurple)}</style><div class="a">x</div>',
            "background-image"),
        "a font-family list": ('<style>.a{font-family:Inter, sans-serif}</style><div class="a">x</div>', "font-family"),
        "<em> (off the list)": ("<div><em>a</em></div>", "<em>"),
        "<i> (off the list)": ("<div><i>a</i></div>", "<i>"),
        "font-style:italic": ('<style>.a{font-style:italic}</style><div class="a">x</div>', "font-style"),
        "font-weight:lighter (off the list)": (
            '<style>.a{font-weight:lighter}</style><div class="a">x</div>', "font-weight"),
        "font-weight:bolder": ('<style>.a{font-weight:bolder}</style><div class="a">x</div>', "font-weight"),
        "<body style=…> (would style the page, was silently dropped)": (
            '<body style="background-color:red"><div>x</div></body>', "style="),
        "<body class=…> (its rules would not reach the wrapper)": (
            '<style>.dark{background-color:#000000}</style><body class="dark"><div>x</div></body>', "class="),
        "<html dir=rtl>": ('<html dir="rtl"><body><div>x</div></body></html>', "dir="),
        "<body bgcolor=…/> self-closed": ('<body bgcolor="red"/><div>x</div>', "bgcolor="),
        f"elements nested deeper than {P.MAX_DEPTH}": (
            "<div>" * (P.MAX_DEPTH + 1) + "x" + "</div>" * (P.MAX_DEPTH + 1), "deeper"),
        "a CSS escape": ('<style>.a{background-image:\\75 rl(x)}</style><div class="a">x</div>', "escape"),
        "!important": ('<style>.a{color:red !important}</style><div class="a">x</div>', "color"),
        "a descendant selector (off the list)": (
            '<style>.c .a{color:red}</style><div class="c"><div class="a">x</div></div>', "selector"),
        "a style= attribute": ('<div style="color:red">x</div>', "style="),
        "an event attribute": ('<div onclick="x()">x</div>', "onclick="),
        "<script>": ('<div>x</div><script>1</script>', "<script>"),
        "<link>": ('<link rel="stylesheet" href="x.css"><div>x</div>', "<link>"),
        "<canvas>": ('<canvas></canvas><div>x</div>', "<canvas>"),
        "<iframe>": ('<iframe src="x"></iframe><div>x</div>', "<iframe>"),
        "<ul>/<li> (off the list)": ("<ul><li>a</li></ul>", "<ul>"),
        "<br> (off the list)": ("<div>a<br>b</div>", "<br>"),
        "<table> (off the list)": ("<table><tr><td>a</td></tr></table>", "<table>"),
        "box-sizing (the gate sets it)": ('<style>.a{box-sizing:border-box}</style><div class="a">x</div>',
                                          "box-sizing"),
        "unbalanced markup": ("<div><p>x</div>", "well formed"),
        "a <style media=…> block": ('<style media="print">.a{color:red}</style><div>x</div>', "<style"),
    }
    for name, (pg, word) in cases.items():
        why = _refused(pg)
        check(f"no picture: {name}", bool(why) and word.lower() in why.lower(), why or "ACCEPTED")
    why = _refused('<style/><div class="c">Hello</div>')
    check("text after a self-closed <style/> is markup, not CSS", why == "", why)
    check("text and inline tags are fine inside display:flex",
          not _refused('<style>.r{display:flex}</style><div class="r">Total: <b>$42</b></div>'))
    for pg in ("<p>Total: <b>$42</b> today</p>", "<div><span>A</span><span>B</span></div>",
               "Total: <strong>$42</strong>"):
        check(f"text beside an inline tag is drawn, not refused (Takumi flows it on one line): {pg!r}",
              not _refused(pg), _refused(pg))
    page = _accept("<p>x</p>", {})
    check("every tag a page may use is put back to content-box, and the wrapper alone is border-box",
          P._BOX_SHEET in page.css and page.css.index(P._BOX_SHEET) < page.css.index("section{width:720px}", 1)
          and all(t in P._BOX_SHEET.split("{box-sizing:content-box}")[0] for t in P.ALLOWED_TAGS)
          and P._BOX_SHEET.startswith("section{box-sizing:border-box}"), page.css[:240])
    check("every named colour CSS has is on the list, and nothing else by name",
          len(P.NAMED_COLOURS) == 148 and {"rebeccapurple", "transparent", "navy"} <= P.NAMED_COLOURS | {"transparent"}
          and all(P._PROPERTY_RE["color"].match(n) for n in P.NAMED_COLOURS)
          and not P._PROPERTY_RE["color"].match("redd") and not P._PROPERTY_RE["color"].match("currentcolor"))
    check("<html lang> is allowed on the structure", not _refused(
        '<html lang="en"><head><meta charset="utf-8"></head><body><div>x</div></body></html>'))
    page = _accept('<style>.a{font-family:inter;font-weight:300}</style><div class="a">x</div>', {})
    check("any accepted spelling of the font is written as exactly \"Inter\" (the name the child registers)",
          '.a{font-family:"Inter";font-weight:300;}' in page.css, page.css[-120:])
    page = _accept('<style>.a{background-image:LINEAR-GRADIENT(90DEG, #FF0000, NAVY);'
                   'border:2PX SOLID Red;font-family:"INTER"}</style><div class="a">x</div>', {})
    check("every value is handed on in lower case, the spelling the lists were measured in "
          "(Takumi drops linear-gradient(90DEG, …) without a word)",
          '.a{background-image:linear-gradient(90deg, #ff0000, navy);border:2px solid red;'
          'font-family:"Inter";}' in page.css, page.css[-160:])
    check(f"{P.MAX_DEPTH} levels of nesting are drawn",
          not _refused("<div>" * P.MAX_DEPTH + "x" + "</div>" * P.MAX_DEPTH))
    deep = "<div>" * 3000 + "x" + "</div>" * 3000
    try:
        pic = asyncio.run(P.draw_picture(deep, tokens={}))
        check("3000 nested divs (33 KB) give no picture instead of raising out of draw_picture",
              pic.png is None and "deeper" in pic.reason, pic.reason)
    except RecursionError as e:
        check("3000 nested divs (33 KB) give no picture instead of raising out of draw_picture", False, repr(e))

    # The first line runs on the caller's event loop: its cost must not grow
    # with rules x nodes. A 128 KiB page of 4096 display rules and 9357 nodes
    # once took 7.3 s here (measured).
    rule = ".a{display:flex}"
    style = "<style>" + rule * (P.MAX_PAGE_CHARS // 2 // len(rule)) + "</style>"
    page = style + "<div>" + "<p></p>" * ((P.MAX_PAGE_CHARS - len(style) - 20) // 7) + "</div>"
    t0 = time.monotonic()
    _refused(page)
    took = time.monotonic() - t0
    check(f"a {len(page) // 1024} KiB page of {style.count('{')} rules and {page.count('<p>')} nodes "
          f"is checked in under 1 s ({took:.2f} s)", took < 1.0, took)


def test_tokens():
    print("token values")
    stock = _stock_sheet()
    check("the stock sheet's --cseries-ink is read", TOKENS.get("--cseries-ink") == "#0A0E0F", TOKENS.get("--cseries-ink"))
    check("a token defined through another (--radius-lg: var(--cseries-radius-lg, 10px)) resolves",
          TOKENS.get("--radius-lg") == "10px", TOKENS.get("--radius-lg"))
    check("only the three token families are kept", all(k.startswith(("--cseries-", "--font-", "--radius-"))
                                                        for k in TOKENS), sorted(TOKENS)[:5])

    # The live desktop's override is appended to the stock sheet by
    # appearance.token_sheet (design_tokens_css -> desktop_tokens.tokens_css);
    # build it with that same function.
    from server.applications.desktop_tokens import tokens_css
    override = tokens_css({"--cseries-ink": "#112233", "--cseries-radius-lg": "4px"})
    sheet = stock + override
    calls = []

    async def token_sheet():
        calls.append(1)
        return sheet

    fake = types.ModuleType("server.routes.appearance")
    fake.token_sheet = token_sheet
    saved = sys.modules.get("server.routes.appearance")
    sys.modules["server.routes.appearance"] = fake
    try:
        live = asyncio.run(P.desktop_tokens())
    finally:
        if saved is None:
            sys.modules.pop("server.routes.appearance", None)
        else:
            sys.modules["server.routes.appearance"] = saved
    check("desktop_tokens() reads appearance.token_sheet", calls == [1])
    check("the live desktop's override wins over the stock value",
          live.get("--cseries-ink") == "#112233" and live.get("--radius-lg") == "4px",
          (live.get("--cseries-ink"), live.get("--radius-lg")))
    page = _accept('<style>.a{background-color:var(--cseries-ink);border-radius:var(--radius-lg);'
                   'font-family:var(--font-display)}</style><div class="a">x</div>', live)
    check("a card in var(--cseries-ink) is drawn in the desktop's colour",
          ".a{background-color:#112233;border-radius:4px;" in page.css, page.css[-160:])
    check("var(--font-display) is the shipped family too", f'.a{{background-color:#112233;border-radius:4px;'
          f'font-family:"{P.SHIPPED_FONT}";}}' in page.css)
    art = Path(_ROOT, "server/routes/artifacts.py").read_text(encoding="utf-8")
    check("the Artifacts window still wears appearance.token_sheet (the same sheet)",
          "from server.routes.appearance import token_sheet" in art)


# ── The child's answer, through draw_picture, with a stand-in child ────────

def _png(w=4, h=2, rgba=(10, 20, 30, 255)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgba) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _stub(reply: dict, extra: str = "") -> list[str]:
    body = json.dumps(reply)
    code = (f"import sys\nsys.stdin.buffer.read()\n{extra}\n"
            f"sys.stdout.write({body!r} + '\\n')\n")
    return [sys.executable, "-I", "-c", code]


def _draw_with(argv, page=None, **kw):
    saved = P._child_argv
    P._child_argv = lambda: argv
    try:
        return asyncio.run(P.draw_picture(page or f'<style>{CARD_CSS}</style><div class="c">Hi</div>',
                                          tokens={}, **kw))
    finally:
        P._child_argv = saved


def test_second_line():
    print("the second line, on the child's answer")
    good = {"ok": True, "png": base64.b64encode(_png()).decode(), "warnings": [], "colours": 2}
    pic = _draw_with(_stub(good))
    check("a clean answer is a picture", pic.png == _png() and pic.reason == "", pic.reason)
    pic = _draw_with(_stub({**good, "warnings": ["DeprecationWarning: something changed"]}))
    check("a Python warning in the answer means no picture", pic.png is None and "DeprecationWarning" in pic.reason,
          pic)
    pic = _draw_with(_stub({**good, "colours": 1}))
    check("a single-colour image means no picture", pic.png is None and "single colour" in pic.reason, pic)
    pic = _draw_with(_stub({**good, "png": base64.b64encode(b"GIF89a....").decode()}))
    check("an answer that is not a PNG means no picture", pic.png is None, pic)
    pic = _draw_with(_stub({"ok": False, "reason": "Takumi could not draw it: PanicException: x"}))
    check("the child's own refusal is passed on", pic.png is None and "PanicException" in pic.reason, pic)
    pic = _draw_with([sys.executable, "-I", "-c", "import sys; sys.stdin.read(); print('not json')"])
    check("an answer that is not JSON means no picture", pic.png is None and "failed" in pic.reason, pic)
    pic = _draw_with(_stub(good), width=50)
    check("a width out of range is refused before any child starts", pic.png is None and "width" in pic.reason)
    pic = _draw_with(_stub(good), page='<div><img src="/etc/hostname"><p>x</p></div>')
    check("a refused page never reaches a child", pic.png is None and "<img>" in pic.reason)


def test_child_static():
    print("the child, without drawing")
    # Landlock unavailable: the child refuses before importing anything.
    code = ("import sys, json\n"
            f"sys.path.insert(0, {_CHILD_DIR!r})\n"
            "import _picture_child as C\n"
            "C.landlock_abi = lambda: 0\n"
            "sys.argv = ['x', sys.argv[1]]\n"
            "sys.exit(C.main())\n")
    argv = [sys.executable, "-I", "-c", code, P._child_argv()[-1]]
    pic = _draw_with(argv)
    check("with Landlock unavailable the renderer refuses to draw at all",
          pic.png is None and "Landlock is not available" in pic.reason, pic)
    src = Path(_CHILD_DIR, "_picture_child.py").read_text(encoding="utf-8")
    main_src = src[src.index("def main()"):]
    check("the child checks Landlock and locks writes BEFORE importing pytakumi",
          main_src.index("landlock_abi()") < main_src.index("lock_writes(abi)")
          < main_src.index("import pytakumi"))
    check("…and locks reads BEFORE reading the page",
          main_src.index("lock_reads(abi, roots)") < main_src.index("sys.stdin.buffer.read()"))

    # Landlock below ABI 6 cannot keep the child's signals inside it, and the
    # child runs as the kernel's own uid: it could kill the kernel. So 4 and 5
    # are refused like no Landlock at all.
    code5 = code.replace("C.landlock_abi = lambda: 0", "C.landlock_abi = lambda: 5")
    pic = _draw_with([sys.executable, "-I", "-c", code5, P._child_argv()[-1]])
    check("with Landlock ABI 5 (no signal scoping) the renderer refuses to draw",
          pic.png is None and "Landlock is not available" in pic.reason and "ABI 5" in pic.reason, pic)

    roots = C.read_roots(_ROOT + "/dashboard", [json, ast])
    check("the read roots computed here touch no Host data path",
          C.check_roots(roots, P.HOST_DATA_PATHS) == "", (roots, C.check_roots(roots, P.HOST_DATA_PATHS)))
    for bad in ("/", "/app/projects", "/var/lib/cseries", "/var/run/postgresql", "/home/user/x"):
        check(f"a read root of {bad} is refused", bool(C.check_roots([bad], P.HOST_DATA_PATHS)))
    for p in ("/app", "/var/lib/cseries", "/home", "/var/run", "/etc/privatae", "/tmp"):
        check(f"{p} is on the Host-data list", p in P.HOST_DATA_PATHS)

    # The one font: only the spelling check_page writes is drawn.
    css = P.check_page('<style>.a{font-family:var(--font-body)}</style><div class="a">x</div>', TOKENS).css
    check("the child finds nothing to refuse in a font-family the gate wrote",
          C.other_fonts(css, P.SHIPPED_FONT) == [] and css.count(f'font-family:"{P.SHIPPED_FONT}"') == 2, css[:200])
    for bad in ('.a{font-family:"Arial"}', '.a{font-family:Geist}', ".a{font-family:Inter}",
                '.a{font:bold 12px "Inter"}', '.a{color:#ffffff;FONT-FAMILY : "Geist" }'):
        check(f"…and refuses {bad!r} (Takumi would draw it without a word)",
              bool(C.other_fonts(bad, P.SHIPPED_FONT)), C.other_fonts(bad, P.SHIPPED_FONT))
    check("…but not font-weight or font-size", C.other_fonts(".a{font-weight:700;font-size:12px}",
                                                            P.SHIPPED_FONT) == [])
    check("the shipped font lives under the interpreter's prefix, outside every Host data path",
          C.FONT_FILE == os.path.join(sys.prefix, "share", "cseries", "fonts", "InterVariable.ttf")
          and C.check_roots([os.path.dirname(C.FONT_FILE)], P.HOST_DATA_PATHS) == "")
    check("the child is handed an explicit nesting cap for Takumi's HTML reader",
          json.loads(P._child_argv()[-1]).get("max_depth") == P.CHILD_MAX_DEPTH > P.MAX_DEPTH)

    font = _font_with_cmap()
    got = C.cmap_codepoints(font)
    check("the cmap reader takes format 12 when the font has it (every group; a group that starts at "
          ".notdef keeps the characters after it)",
          got == frozenset({0x21, 0x41, 0x42, 0x43, 0x1F600, 0x1F601}), sorted(got))
    got4 = C.cmap_codepoints(_font_with_cmap(with_format12=False))
    check("…and format 4 otherwise (deltas, a glyph array, a 0 in it, the 0xFFFF end)",
          got4 == frozenset({0x41, 0x42, 0x43, 0x61, 0x63}), sorted(got4))
    try:
        C.cmap_codepoints(_font_with_cmap(with_format12=False, with_format4=False))
        check("a font with no Unicode cmap subtable raises (no glyph table, no picture)", False)
    except ValueError:
        check("a font with no Unicode cmap subtable raises (no glyph table, no picture)", True)
    # A malformed table costs a refusal, never the child's CPU limit: groups or
    # segments that overlap made the reader repeat Unicode once per entry
    # (measured: 2000 overlapping groups in a 24 KB table took 15 s, past
    # CPU_LIMIT_S, and the kernel saw a crash). Kept small here, so the reader
    # without the order check returns its set in about a second instead.
    for name, font in (
            ("format 12 groups that overlap", _font_with_subtables([(3, 10, _format12([(0, 0x10FFFF, 1)] * 40))])),
            ("format 12 groups out of order", _font_with_subtables([(3, 10, _format12([(0x41, 0x43, 1), (0x20, 0x21, 1)]))])),
            ("a format 12 group that ends before it starts", _font_with_subtables([(3, 10, _format12([(0x43, 0x41, 1)]))])),
            ("a format 12 group count past the end of the font",
             _font_with_subtables([(3, 10, _format12([(0x41, 0x43, 1)])[:12] + struct.pack(">I", 0xFFFFFFF)
                                    + struct.pack(">III", 0x41, 0x43, 1))])),
            ("format 4 segments that overlap",
             _font_with_subtables([(3, 1, _format4([(0, 0xFFFE)] * 20 + [(0xFFFF, 0xFFFF)]))])),
            ("a format 4 segment that ends before it starts",
             _font_with_subtables([(3, 1, _format4([(0x43, 0x41), (0xFFFF, 0xFFFF)]))]))):
        try:
            C.cmap_codepoints(font)
            raised = "returned"
        except ValueError as e:
            raised = "ValueError" if "order" in str(e) or "fit" in str(e) or "past the end" in str(e) else repr(e)
        except Exception as e:  # noqa: BLE001
            raised = repr(e)
        check(f"the cmap reader refuses {name} (ValueError: no glyph table, no picture)",
              raised == "ValueError", raised)
    check("missing_glyphs names every character outside the table, tags and entities aside",
          C.missing_glyphs(got4, "<p>Ab&amp;c</p>") == ["&", "b"] and C.missing_glyphs(got4, "<b>ac</b>") == [],
          C.missing_glyphs(got4, "<p>Ab&amp;c</p>"))


def _font_with_cmap(with_format12: bool = True, with_format4: bool = True) -> bytes:
    """A font file holding only a cmap table, built by hand: format 4 maps
    A-C by delta and a-c through the glyph array (b to glyph 0, so absent);
    format 12 maps A-C and U+1F600-1F601, plus a group starting at glyph 0:
    its first character (space) is .notdef and its second (!) is glyph 1."""
    seg = [(0x41, 0x43, 10, 0), (0x61, 0x63, 0, 1), (0xFFFF, 0xFFFF, 1, 0)]  # start, end, delta, uses array
    n = len(seg)
    array = [20, 0, 22]
    head = struct.pack(">HHHHHHH", 4, 0, 0, 2 * n, 0, 0, 0)
    ends = struct.pack(f">{n}H", *[e for _, e, _, _ in seg]) + b"\0\0"
    starts = struct.pack(f">{n}H", *[s for s, _, _, _ in seg])
    deltas = struct.pack(f">{n}H", *[d & 0xFFFF for _, _, d, _ in seg])
    # idRangeOffset for segment 1 points from its own slot to the array start.
    offs = [0] * n
    offs[1] = 2 * (n - 1)
    ranges = struct.pack(f">{n}H", *offs)
    f4 = head + ends + starts + deltas + ranges + struct.pack(f">{len(array)}H", *array)
    f4 = f4[:2] + struct.pack(">H", len(f4)) + f4[4:]
    groups = [(0x20, 0x21, 0), (0x41, 0x43, 5), (0x1F600, 0x1F601, 30)]  # ascending, as format 12 requires
    f12 = _format12(groups)
    subs = ([(3, 1, f4)] if with_format4 else []) + ([(3, 10, f12)] if with_format12 else [])
    if not subs:
        subs = [(1, 0, struct.pack(">HHH", 0, 262, 0) + bytes(256))]  # a Macintosh format 0 table only
    return _font_with_subtables(subs)


def _format12(groups) -> bytes:
    return struct.pack(">HHIII", 12, 0, 16 + 12 * len(groups), 0, len(groups)) + b"".join(
        struct.pack(">III", *g) for g in groups)


def _format4(segs) -> bytes:
    """Segments (start, end), each mapped by a delta of 1 (no glyph array)."""
    n = len(segs)
    t = (struct.pack(">HHHHHHH", 4, 0, 0, 2 * n, 0, 0, 0) + struct.pack(f">{n}H", *[e for _, e in segs])
         + b"\0\0" + struct.pack(f">{n}H", *[s for s, _ in segs]) + struct.pack(f">{n}H", *([1] * n))
         + struct.pack(f">{n}H", *([0] * n)))
    return t[:2] + struct.pack(">H", len(t)) + t[4:]


def _font_with_subtables(subs) -> bytes:
    cmap = struct.pack(">HH", 0, len(subs))
    at = 4 + 8 * len(subs)
    body = b""
    for pid, eid, t in subs:
        cmap += struct.pack(">HHI", pid, eid, at + len(body))
        body += t
    cmap += body
    directory = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0) + struct.pack(">4sIII", b"cmap", 0, 28, len(cmap))
    return directory + cmap


# A seccomp program is checked here by running it: the five BPF instructions
# it uses, over seccomp_data (nr, arch, ip, args[6]) for a little-endian
# machine.
def _bpf(prog, nr: int, arch: int, arg0: int = 0) -> int:
    data = struct.pack("<iIQ6Q", nr, arch, 0, arg0, 0, 0, 0, 0, 0)
    a, pc = 0, 0
    while True:
        code, jt, jf, k = prog[pc]
        if code == 0x20:
            a = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == 0x06:
            return k
        else:
            hit = {0x15: a == k, 0x35: a >= k, 0x45: bool(a & k)}[code]
            pc += 1 + (jt if hit else jf)


def test_seccomp_program():
    print("the child's seccomp program")
    allow, eperm, enosys = 0x7FFF0000, 0x00050000 | 1, 0x00050000 | 38
    clone_thread = 0x3D0F00  # glibc's pthread_create flags: CLONE_VM|FS|FILES|SIGHAND|THREAD|SYSVSEM|…
    clone_fork = 0x01200011  # glibc's fork(): CLONE_CHILD_SETTID|CLONE_CHILD_CLEARTID|SIGCHLD
    for machine, arch, nrs in (("x86_64", 0xC000003E, {"read": 0, "socket": 41, "clone": 56, "fork": 57,
                                                       "vfork": 58, "clone3": 435, "io_uring_setup": 425}),
                               ("aarch64", 0xC00000B7, {"read": 63, "socket": 198, "clone": 220,
                                                        "clone3": 435, "io_uring_setup": 425})):
        prog = C.filter_program(machine)
        want = {"read": allow, "socket": eperm, "fork": eperm, "vfork": eperm, "io_uring_setup": eperm,
                "clone3": enosys}
        for name, nr in nrs.items():
            if name == "clone":
                continue
            got = _bpf(prog, nr, arch)
            check(f"{machine}: {name} -> {hex(want[name])}", got == want[name], hex(got))
        check(f"{machine}: clone() of a thread is allowed",
              _bpf(prog, nrs["clone"], arch, clone_thread) == allow)
        check(f"{machine}: clone() of a new process (fork) is refused",
              _bpf(prog, nrs["clone"], arch, clone_fork) == eperm)
        check(f"{machine}: a syscall through another ABI is refused", _bpf(prog, 0, 0x40000003) == eperm)
    check("x86_64: an x32 syscall number is refused", _bpf(C.filter_program("x86_64"), 0x40000000, 0xC000003E) == eperm)


def test_child_inherits_nothing():
    print("what the child is handed")
    # An inheritable descriptor in the caller (a DB socket would be one) and
    # secrets in the caller's environment must not reach the child.
    r, w = os.pipe()
    os.set_inheritable(r, True)
    os.set_inheritable(w, True)
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "CSERIES_VAULT_KEY", "PGPASSWORD")}
    os.environ.update({"DATABASE_URL": "postgresql://u:secret@/db", "CSERIES_VAULT_KEY": "k",
                       "PGPASSWORD": "secret"})
    try:
        code = ("import json, os, sys; sys.stdin.buffer.read()\n"
                "fds = sorted(int(f) for f in os.listdir('/proc/self/fd'))\n"
                "print(json.dumps({'ok': False, 'reason': 'probe', 'fds': fds, 'env': dict(os.environ),"
                " 'cwd': os.getcwd()}))\n")
        reply, why = asyncio.run(P._run_child(b"{}", argv=[sys.executable, "-I", "-c", code]))
    finally:
        os.close(r)
        os.close(w)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    reply = reply or {}
    # listdir itself holds one descriptor open while it reads the directory.
    check("the child holds only its three pipes (an inheritable descriptor in the caller is not passed)",
          len(reply.get("fds", [])) == 4 and reply.get("fds", [])[:3] == [0, 1, 2], reply.get("fds") or why)
    env = reply.get("env", {})
    check("the child's environment is the fixed scrubbed one, nothing of the caller's",
          env and set(env) <= set(P._child_env()) | {"LC_CTYPE"} and "DATABASE_URL" not in env
          and not any("secret" in v for v in env.values()), sorted(env))
    check("the child starts in /", reply.get("cwd") == "/", reply.get("cwd"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_no_row_no_file_no_browser():
    print("no row, no file, no browser")
    files = [Path(_CHILD_DIR, "picture.py"), Path(_CHILD_DIR, "_picture_child.py")]
    imports = set().union(*(_imports(f) for f in files))
    check("the renderer imports only the token sheet from the server",
          {m for m in imports if m.startswith("server")} == {"server.routes.appearance"}, imports)
    check("…and nothing that holds a database", not imports & {"asyncpg", "psycopg", "sqlite3"}, imports)
    text = "\n".join(f.read_text(encoding="utf-8") for f in files)
    code_only = re.sub(r'"""(?:.|\n)*?"""', "", text)
    code_only = "\n".join(l.split("#", 1)[0] for l in code_only.splitlines())
    for word in ("browser_sidecar", "app_verify", "/v1/verify", "desktop_render", "sidecars",
                 "generated_images", "get_pool", "INSERT", "UPDATE ", "execute(", "fetchrow"):
        check(f"no {word!r} in the renderer's code", word not in code_only)
    writes = re.findall(r"open\(([^)]*)['\"][wax]", code_only)
    check("no file is opened for writing (only the child's own oom_score_adj, a /proc knob)",
          writes == ['"/proc/self/oom_score_adj", '], writes)
    before = set(sys.modules)
    _draw_with(_stub({"ok": True, "png": base64.b64encode(_png()).decode(), "warnings": [], "colours": 2}))
    new = {m for m in set(sys.modules) - before if "browser" in m or "app_verify" in m or "asyncpg" in m}
    check("a draw loads no browser or database module", not new, new)


# ── Real signals: only inside a private PID namespace ──────────────────────

def _in_private_pid_namespace() -> bool:
    try:
        return (Path("/proc/1/comm").read_text().strip() == "sh" and os.getppid() == 1
                and len([e for e in os.listdir("/proc") if e.isdigit()]) < 50)
    except OSError:
        return False


def _procs() -> set[int]:
    return {int(e) for e in os.listdir("/proc") if e.isdigit()}


def signal_part() -> None:
    """REAL SIGNALS. Runs only after _in_private_pid_namespace() said yes:
    the only processes that exist are pid 1 (sh), this test and its children."""
    print("real signals, inside a private PID namespace")
    good = {"ok": True, "png": base64.b64encode(_png()).decode(), "warnings": [], "colours": 2}
    P.TIME_LIMIT_S = 1.5
    ticks = []

    async def hang_and_tick(argv):
        saved = P._child_argv
        P._child_argv = lambda: argv

        async def ticker():
            for _ in range(200):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)
        try:
            t = asyncio.create_task(ticker())
            t0 = time.monotonic()
            pic = await P.draw_picture(f'<style>{CARD_CSS}</style><div class="c">Hi</div>', tokens={})
            took = time.monotonic() - t0
            t.cancel()
            return pic, took
        finally:
            P._child_argv = saved

    before = _procs()
    hang = [sys.executable, "-I", "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(600)"]
    pic, took = asyncio.run(hang_and_tick(hang))
    check("a renderer that hangs gives no picture, at the time limit",
          pic.png is None and "longer than" in pic.reason and took < 5, (pic, took))
    check("the caller kept serving while it hung (its event loop ran throughout)",
          len(ticks) >= 40, len(ticks))
    check("the hung child is gone, reaped, and nothing else was touched",
          _procs() <= before, (before, _procs()))

    crash = [sys.executable, "-I", "-c",
             "import sys, ctypes; sys.stdin.buffer.read(); ctypes.string_at(0)"]
    pic = _draw_with(crash)
    check("a renderer that crashes in native code gives no picture and names the signal",
          pic.png is None and "crashed (signal 11)" in pic.reason, pic)
    pic = _draw_with([sys.executable, "-I", "-c", "import os, sys; sys.stdin.buffer.read(); os.abort()"])
    check("…and an abort", pic.png is None and "crashed (signal 6)" in pic.reason, pic)
    pic = _draw_with(_stub(good))
    check("the next picture after a hang and two crashes is drawn", pic.png == _png(), pic.reason)
    check("no child is left behind", _procs() <= before, (before, _procs()))

    # Output past the caps. asyncio's wait() returns only when every pipe has
    # reached its end, so a reader that stops at its cap left draw_picture
    # pending forever with the slot held (measured: neither returned in 25 s
    # with a 4 s limit). Each draw runs under an outer bound so a regression
    # fails here instead of hanging the gate.
    P.TIME_LIMIT_S = 4.0
    P.SETTLE_S = 1.0

    def bounded(argv, bound=12.0):
        async def go():
            saved = P._child_argv
            P._child_argv = lambda: argv
            try:
                return await asyncio.wait_for(
                    P.draw_picture(f'<style>{CARD_CSS}</style><div class="c">Hi</div>', tokens={}), bound)
            finally:
                P._child_argv = saved
        t0 = time.monotonic()
        try:
            pic = asyncio.run(go())
        except (asyncio.TimeoutError, TimeoutError):
            pic = None
        return pic, time.monotonic() - t0

    flood = [sys.executable, "-I", "-c",
             "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'x' * (40 << 20))"]
    pic, took = bounded(flood)
    check("an answer past the size cap gives no picture at once, the child stopped",
          pic is not None and pic.png is None and "larger than a picture" in pic.reason and took < 3,
          (pic, round(took, 2)))
    body = json.dumps(good)
    noisy = [sys.executable, "-I", "-c",
             f"import sys; sys.stdin.buffer.read(); sys.stderr.buffer.write(b'e' * (1 << 20)); "
             f"sys.stdout.write({body!r} + '\\n')"]
    pic, took = bounded(noisy)
    check("1 MiB on stderr is drained past its cap and the picture still arrives",
          pic is not None and pic.png == _png() and took < 3, (pic and pic.reason, round(took, 2)))

    # A process the child started that keeps its stdout open after the child
    # is gone (the real child cannot fork; this is the parent's own bound).
    # This test becomes the subreaper, so the left-behind process is its own
    # to kill and reap, inside this namespace.
    import ctypes
    ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    pidfile = os.path.join(tempfile.gettempdir(), f"picture-grandchild-{os.getpid()}")
    holder = [sys.executable, "-I", "-c",
              "import os, sys, time; sys.stdin.buffer.read()\n"
              "pid = os.fork()\n"
              "if pid == 0:\n"
              "    time.sleep(600); os._exit(0)\n"
              f"open({pidfile!r}, 'w').write(str(pid))\n"
              f"sys.stdout.write({body!r} + '\\n'); sys.stdout.flush(); os._exit(0)\n"]
    pic, took = bounded(holder)
    check("a pipe held open by a process the child left behind costs the time limit and the settle "
          "bound, then gives no picture and frees the slot",
          pic is not None and pic.png is None and "longer than" in pic.reason
          and took < P.TIME_LIMIT_S + P.SETTLE_S + 2, (pic, round(took, 2)))
    try:
        gpid = int(open(pidfile).read())
        os.unlink(pidfile)
        os.kill(gpid, 9)  # inside this private PID namespace only
        os.waitpid(gpid, 0)
    except (OSError, ValueError):
        pass
    pic, took = bounded(_stub(good))
    check("…and the next picture is drawn (no slot is left held)", pic is not None and pic.png == _png(),
          pic and pic.reason)
    time.sleep(0.2)
    check("no process is left behind after the floods", _procs() <= before, (before, _procs()))


def run_signal_part() -> str:
    """'' when the part ran and passed, 'FAIL…' when it ran and failed, and
    'SKIP…' when no private PID namespace could be made (never run here)."""
    cmd = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
           "sh", "-c", f'"{sys.executable}" "{os.path.abspath(__file__)}" --signal-part; echo "rc=$?"']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           env={**os.environ, "APP_ROOT": _ROOT})
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"SKIP: no private PID namespace here ({e!r}); the real-signal part did not run"
    m = re.search(r"rc=(\d+)\s*\Z", r.stdout)
    if not m:
        return ("SKIP: no private PID namespace here; the real-signal part did not run. "
                + (r.stdout + r.stderr)[-300:].replace("\n", " "))
    print(r.stdout[:m.start()].rstrip())
    return "" if m.group(1) == "0" else "FAIL: the real-signal part failed (above)"


def main() -> int:
    if "--signal-part" in sys.argv:
        if not _in_private_pid_namespace():
            print("refusing: --signal-part runs only inside a private PID namespace", file=sys.stderr)
            return 2
        signal_part()
        print(f"\n{PASSES} passed, {len(FAILS)} failed")
        return 1 if FAILS else 0
    test_gate()
    test_tokens()
    test_second_line()
    test_child_static()
    test_seccomp_program()
    test_child_inherits_nothing()
    test_no_row_no_file_no_browser()
    sig = run_signal_part()
    if sig.startswith("FAIL"):
        FAILS.append("real-signal part")
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} failed, {PASSES} passed: {FAILS}")
        return 1
    print(f"OK connector picture: {PASSES} passed" + (f" ({sig})" if sig else
                                                      ", real-signal part included"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
