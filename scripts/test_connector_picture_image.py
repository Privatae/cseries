#!/usr/bin/env python3
"""The connector picture renderer, drawing for real inside the kernel image.

Smoke runs this in a fresh container from the image under the Host
container's own restrictions: seccomp-personal.json, a read-only root, every
capability dropped, no-new-privileges, uid 1000 (scripts/smoke_test.sh). What
it pins:

  0. The image carries no GL library, no Mesa, no font package and no
     fontconfig; the one font is InterVariable.ttf from the Inter 4.1
     release, at its pinned hash, with its licence beside it.
  1. A card in the subset is drawn, at the caller's width, and a card written
     in var(--cseries-…) tokens draws in the desktop's colours, in Inter.
  2. A page the gate refuses is never drawn, and the second line holds by
     itself: handed to the child directly, a page that names another font
     (the Geist the pytakumi wheel embeds, or a name Takumi does not know,
     which it would draw in Inter) gives no picture.
  3. An inline SVG shape gives no picture, and the reason is the tag
     allowlist: drawn directly it comes out with ZERO warnings and many
     colours (Takumi has no warning channel at all).
  4. A single-colour image gives no picture, and neither does text with a
     character the font has no glyph for (it draws as an empty box, silently).
  5. The child runs under Landlock (ABI 6 or later), first in line for the
     OOM killer, with exactly the read roots listed below. Takumi reads no
     file for a page (an <img src>, a CSS url() and a font-family naming a
     real file draw exactly as the page without them, confined or not).
     Locked as the child locks itself, a stand-in that has imported pytakumi
     (and so started Takumi's drawing threads) cannot read, write, exec,
     fork, open a socket, signal its parent, read its parent's memory or
     environment, or copy its parent's descriptors.
  6. Confinement changes nothing in the output, the renderer writes no file,
     and it loads no browser or database module.
  7. Every entry on the measured allowlists still draws with this Takumi:
     each allowed property and keyword value changes the picture, with no
     exception, and the boxes land where a browser puts them for the
     measurements that place one. Every named colour draws as its sRGB value,
     boxes are content-box as in a browser, the tags get the browser's
     defaults, and text beside an inline tag stays on one line. Each value
     written in upper case draws, through the gate, as in lower case. A
     property added to the list without a measurement here fails.

Run in the image: python3 /app/scripts/test_connector_picture_image.py
"""
import asyncio
import base64
import io
import json
import os
import sys
import sysconfig
import tempfile
import warnings

_ROOT = os.environ.get("APP_ROOT", "/app")
sys.path[:0] = [_ROOT]

from server.connectors import picture as P  # noqa: E402

sys.path.insert(0, os.path.dirname(P._CHILD))
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
        print(f"  FAIL  {name}" + (f"\n        {str(detail)[:400]}" if detail != "" else ""))


# Where Dockerfile.linux installs the shipped font, and its sha256: the
# InterVariable.ttf of the Inter 4.1 release (github.com/rsms/inter, tag v4.1).
FONT_PATH = "/usr/local/share/cseries/fonts/InterVariable.ttf"
FONT_SHA256 = "4989b125924991b90d05b2d16e0e388c48f7d5bb8b30539bbf9c755278d0ccaf"

TOKENS = P.tokens_from_sheet(open(os.path.join(_ROOT, "dashboard/base/css/tokens.css"),
                                  encoding="utf-8").read())

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


def _rgb(hexcol: str) -> tuple:
    h = hexcol.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _decode(png: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(png)).convert("RGBA")


_RENDERER = None


def _renderer():
    """A Takumi renderer set up as the child sets up its own: the shipped
    font registered under the one family name."""
    global _RENDERER
    if _RENDERER is None:
        import pytakumi
        r = pytakumi.Renderer()
        with open(C.FONT_FILE, "rb") as f:
            r.register_font(f.read(), name=P.SHIPPED_FONT)
        _RENDERER = r
    return _RENDERER


def _unconfined(html: str, css: str):
    """Draw in THIS process, with no confinement: the negative control, and
    the measurement of the allowlists. The same calls the child makes."""
    import pytakumi
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            tree = pytakumi.from_html(html, max_depth=P.CHILD_MAX_DEPTH, use_presets=True)
            png = _renderer().render(tree, format="png", stylesheets=[css],
                                     font_families=[P.SHIPPED_FONT])
        except Exception as e:  # noqa: BLE001
            return None, [f"{type(e).__name__}: {e}"]
    return _decode(png), [str(x.message) for x in caught]


def _same(a, b) -> bool:
    return a is not None and b is not None and a.size == b.size and a.tobytes() == b.tobytes()


def _boxes(img, *colours) -> list:
    """The bounding box of each colour's pixels, or None where it is absent."""
    px, (w, h) = img.load(), img.size
    found = {c: [w, h, -1, -1] for c in colours}
    for y in range(h):
        for x in range(w):
            c = px[x, y][:3]
            if c in found:
                b = found[c]
                b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x + 1), max(b[3], y + 1)
    return [tuple(found[c]) if found[c][2] >= 0 else None for c in colours]


def _child(html: str, css: str) -> dict:
    """Hand a page to the real child WITHOUT the first line: the test is
    bypassing the allowlist on purpose."""
    reply, why = asyncio.run(P._run_child(json.dumps({"html": html, "css": css}).encode()))
    return reply if reply is not None else {"ok": False, "reason": why}


def test_draws():
    print("drawing")
    pic = asyncio.run(P.draw_picture(VISUAL, tokens=TOKENS))
    check("a typical visual fence body is drawn", pic.png is not None and pic.reason == "", pic.reason)
    if pic.png is None:
        return None
    img = _decode(pic.png)
    check("at the caller's width (720 px)", img.size[0] == 720, img.size)
    check("the card is in the desktop's --cseries-silk", img.getpixel((360, 6))[:3] == _rgb(TOKENS["--cseries-silk"]),
          (img.getpixel((360, 6)), TOKENS["--cseries-silk"]))
    check("its rounded corner shows the desktop's --cseries-ink behind it",
          img.getpixel((0, 0))[:3] == _rgb(TOKENS["--cseries-ink"]), img.getpixel((0, 0)))
    other = dict(TOKENS, **{"--cseries-silk": "#204060"})
    pic2 = asyncio.run(P.draw_picture(VISUAL, tokens=other))
    check("another desktop's --cseries-silk draws in that colour",
          pic2.png is not None and _decode(pic2.png).getpixel((360, 6))[:3] == (0x20, 0x40, 0x60))
    pic3 = asyncio.run(P.draw_picture(VISUAL, tokens=TOKENS, width=400))
    check("width=400 draws 400 px wide", pic3.png is not None and _decode(pic3.png).size[0] == 400)
    return pic.png


def test_refusals(tmp: str):
    print("no picture")
    for name, page in {
        "display:grid": '<style>.a{display:grid;padding:8px;background-color:#222}</style><div class="a">x</div>',
        "an inline SVG shape": ('<div><p>Total</p><svg width="80" height="40"><rect width="80" height="40" '
                                'fill="red"/></svg></div>'),
        "<img src>": f'<div><img src="{tmp}/real.png"><p>x</p></div>',
        "CSS url(": f'<style>.a{{background-image:url({tmp}/real.png)}}</style><div class="a">x</div>',
        "a leftover var(": '<style>.a{color:var(--mine)}</style><div class="a">x</div>',
        "a colour name CSS does not have": '<style>.a{color:blurple}</style><div class="a">x</div>',
    }.items():
        pic = asyncio.run(P.draw_picture(page, tokens=TOKENS))
        check(f"draw_picture gives no picture for {name}", pic.png is None and pic.reason, pic.reason)

    # Takumi draws a font-family it does not know in a registered font, and
    # the name "Geist" in the font the wheel embeds as its last resort, both
    # without a word; the child refuses either by itself.
    base = ".a{padding:8px;background-color:#222222;color:#ffffff}"
    geist_free, _ = _unconfined('<div class="a">Geist</div>', base + '.a{font-family:"Geist"}')
    inter_free, _ = _unconfined('<div class="a">Geist</div>', base)
    check("control: unconfined, font-family \"Geist\" draws in a font other than the shipped one",
          geist_free is not None and not _same(geist_free, inter_free))
    for fam in ('"Geist"', '"No Such Font"', "serif"):
        got = _child('<div class="a">x</div>', base + f".a{{font-family:{fam}}}")
        check(f"the second line alone: font-family {fam} handed straight to the child gives no picture",
              not got.get("ok") and "font other than" in got.get("reason", ""), got)

    svg = _child('<div class="c"><p>Total</p><svg width="80" height="40"><rect width="80" height="40" '
                 'fill="red"/></svg></div>', ".c{padding:20px;background-color:#1e293b;color:#ffffff}")
    check("an SVG shape drawn directly comes out with zero warnings and many colours "
          "(Takumi warns about nothing; the tag allowlist is what stops it)",
          svg.get("ok") and svg.get("warnings") == [] and svg.get("colours", 0) >= 2, svg)

    flat = asyncio.run(P.draw_picture('<style>.z{height:60px;background-color:#ff0000}</style>'
                                      '<div class="z"></div>', tokens={}))
    check("a single-colour image gives no picture", flat.png is None and "single colour" in flat.reason,
          flat.reason)

    # A character the font has no glyph for draws as an empty box with NO
    # warning (measured for 📈, Hebrew and CJK in Inter, with Takumi as with
    # html2pic before it), so the child checks the text against the font's
    # cmap table.
    card = '<style>.c{padding:12px;background-color:#1e293b;color:#ffffff}</style><div class="c">%s</div>'
    box, _ = _unconfined('<div class="c">Spend 📈 up</div>', ".c{padding:12px;background-color:#1e293b;color:#ffffff}")
    check("control: unconfined, Takumi draws 📈 (no glyph in Inter) with no exception and no warning",
          box is not None)
    for text in ("Spend 📈 up", "שלום", "日本"):
        pic = asyncio.run(P.draw_picture(card % text, tokens={}))
        check(f"no picture for {text!r}: the font has no glyph for it",
              pic.png is None and "no glyph" in pic.reason, pic.reason)
    pic = asyncio.run(P.draw_picture(card % "€ £ — “quoted” Ωμέγα Привет ✓ → ⚠", tokens={}))
    check("…while Latin, Greek, Cyrillic, quotes, ✓ → ⚠ draw", pic.png is not None, pic.reason)
    with open(C.FONT_FILE, "rb") as f:
        cps = C.cmap_codepoints(f.read())
    check("the glyph table read from Inter's cmap has its 2852 characters (2821 of them in the BMP)",
          len(cps) == 2852 and len([c for c in cps if c <= 0xFFFF]) == 2821, len(cps))


def test_confinement(card_png: bytes, tmp: str):
    print("the child's confinement")
    page = P.check_page(VISUAL, TOKENS)
    reply = _child(page.html, page.css)
    check("the child runs under Landlock ABI 6 or later", int(reply.get("landlock_abi") or 0) >= 6, reply.get("landlock_abi"))
    check("the child put itself first in line for the out-of-memory killer (oom_score_adj 1000)",
          reply.get("oom_score_adj") == 1000, reply.get("oom_score_adj"))
    check(f"it draws in the shipped {FONT_PATH}", reply.get("font") == FONT_PATH, reply.get("font"))
    want = [sysconfig.get_path("stdlib"), os.path.dirname(FONT_PATH)]
    check(f"its read roots are exactly {want} (pytakumi's package folds into the first)",
          reply.get("roots") == want, reply.get("roots"))
    check("none of them touches a Host data path",
          not [r for r in reply.get("roots", []) for d in P.HOST_DATA_PATHS
               if r == d or r.startswith(d + "/") or d.startswith(r + "/")])
    free, fw = _unconfined(page.html, page.css)
    if free is not None and reply.get("png"):
        conf = _decode(base64.b64decode(reply["png"]))
        check("confined and unconfined draws are the same pixels", _same(conf, free) and not fw, fw)

    # A real PNG and a real font file, outside every read root. Takumi reads
    # neither (it loads images and fonts only from bytes it is handed); the
    # second Landlock layer is what holds if that ever changes.
    real_png = os.path.join(tmp, "real.png")
    with open(real_png, "wb") as f:
        f.write(card_png)
    font_copy = os.path.join(tmp, "copy.ttf")
    with open(C.FONT_FILE, "rb") as src, open(font_copy, "wb") as dst:
        dst.write(src.read())
    css = ".c{padding:8px;background-color:#1e293b;color:#ffffff}"
    without, _ = _unconfined('<div class="c"><p>x</p></div>', css)
    for name, html, extra in (
            ("an <img src> at that PNG", f'<div class="c"><img src="{real_png}"><p>x</p></div>', ""),
            ("a CSS url() at that PNG", '<div class="c"><p>x</p></div>',
             f".c{{background-image:url({real_png})}}"),
            ("a font-family naming that font file", '<div class="c"><p>x</p></div>',
             f'.c{{font-family:"{font_copy}"}}')):
        free, w = _unconfined(html, css + extra)
        check(f"unconfined, {name} draws exactly as the page without it (Takumi read nothing)",
              _same(free, without) and not w, w)
        if "font-family" in extra:
            continue  # the child refuses any other font name before drawing (above)
        got = _child(html, css + extra)
        check(f"in the child, {name} draws exactly as the page without it (allowlist bypassed on purpose)",
              got.get("ok") and _same(_decode(base64.b64decode(got["png"])), without), got.get("reason"))


# What code running inside a locked child could still try. The child itself
# runs no code from the page; this stands in for a native exploit in the
# drawing stack, using the child's own lock functions in the child's order.
_ESCAPE = r"""
import ctypes, json, os, socket, sys, threading
sys.path.insert(0, %(dir)r)
import _picture_child as C
abi = C.landlock_abi()
C.lock_writes(abi)
import pytakumi, warnings
draw_threads = len(os.listdir("/proc/self/task")) - 1
roots = C.read_roots(os.path.dirname(C.FONT_FILE), [pytakumi, warnings])
C.lock_reads(abi, roots)
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
NR = {"x86_64": (434, 438, 310), "aarch64": (434, 438, 270)}[os.uname().machine]
def sc(*a):
    rc = libc.syscall(*[ctypes.c_long(x) for x in a])
    if rc < 0:
        e = ctypes.get_errno(); raise OSError(e, os.strerror(e))
    return rc
res = {}
def t(name, fn):
    try:
        fn(); res[name] = "ALLOWED"
    except Exception as e:
        res[name] = type(e).__name__
def fork():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
def steal_fd():
    # The parent holds a file open at this number; take a copy of it.
    pidfd = sc(NR[0], os.getppid(), 0)
    fd = sc(NR[1], pidfd, %(parent_fd)d, 0)
    os.read(fd, 10)
def read_parent_memory():
    buf = ctypes.create_string_buffer(16)
    local = (ctypes.c_void_p * 2)(ctypes.cast(buf, ctypes.c_void_p), 16)
    remote = (ctypes.c_void_p * 2)(0x400000, 16)
    sc(NR[2], os.getppid(), ctypes.addressof(local), 1, ctypes.addressof(remote), 1, 0)
def through_symlinks():
    # Every symlink under the read roots that points outside them, read
    # through the link: the kernel checks where it lands.
    outside = []
    for root in roots:
        for d, dirs, files in os.walk(root):
            for f in dirs + files:
                p = os.path.join(d, f)
                if os.path.islink(p):
                    real = os.path.realpath(p)
                    if not any(real == r or real.startswith(r + "/") for r in roots) and os.path.isfile(real):
                        outside.append(p)
    res["_symlinks_out"] = len(outside)
    for p in outside[:20]:
        try:
            open(p, "rb").read(1)
        except PermissionError:
            continue
        return  # one read through a link: ALLOWED
    raise PermissionError("every read through them was refused")
t("read a real PNG on disk", lambda: open(%(png)r, "rb").read())
t("read /etc/passwd", lambda: open("/etc/passwd").read())
t("read /etc/passwd through /proc/self/root", lambda: open("/proc/self/root/etc/passwd").read())
t("read the kernel's own code", lambda: open("/app/server/main.py").read())
t("read /proc/self/environ", lambda: open("/proc/self/environ", "rb").read())
t("read its parent's environment", lambda: open("/proc/%%d/environ" %% os.getppid(), "rb").read())
t("read its parent's memory through /proc", lambda: open("/proc/%%d/mem" %% os.getppid(), "rb").read(1))
t("read its parent's memory (process_vm_readv)", read_parent_memory)
t("take a copy of its parent's open descriptor (pidfd_getfd)", steal_fd)
t("write a file", lambda: open(%(tmp)r + "/x", "w").write("x"))
t("make a directory", lambda: os.mkdir(%(tmp)r + "/d"))
t("connect over TCP", lambda: socket.create_connection(("127.0.0.1", 8800), timeout=2))
t("send a UDP packet", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 53)))
t("connect to a unix socket by path", lambda: socket.socket(socket.AF_UNIX).connect(%(sock)r))
t("exec a program", lambda: os.execv("/bin/true", ["true"]))
t("start a new process", fork)
t("signal its parent", lambda: os.kill(os.getppid(), 0))
t("read a file through a symlink that leaves the read roots", through_symlinks)
th = threading.Thread(target=lambda: None); th.start(); th.join()
res["_thread_ran"] = True
res["_draw_threads"] = draw_threads
res["_stdlib_read"] = open(os.__file__).read(10) != ""
print(json.dumps(res))
"""


def test_escape_routes(tmp: str, png: str):
    print("what a compromised child could still do")
    import socket
    import subprocess
    sock_path = os.path.join(tmp, "listen.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(sock_path)
    srv.listen(1)
    # A descriptor this process holds, at a number the stand-in is told: a
    # stand-in for the kernel's open DB socket.
    held = open(png, "rb")
    code = _ESCAPE % {"dir": os.path.dirname(P._CHILD), "png": png, "tmp": tmp, "sock": sock_path,
                      "parent_fd": held.fileno()}
    r = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                       timeout=60, env=P._child_env())
    held.close()
    srv.close()
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        check("the locked stand-in ran", False, r.stderr[-400:])
        return
    check("control: it can still read the standard library", res.pop("_stdlib_read", False) is True)
    check("control: it can still start a thread (only new processes are refused)",
          res.pop("_thread_ran", False) is True)
    check(f"importing pytakumi under the seccomp filter started Takumi's {P.DRAW_THREADS} drawing "
          "thread(s) (RAYON_NUM_THREADS from the child's environment)",
          res.pop("_draw_threads", -1) == P.DRAW_THREADS)
    print(f"        ({res.pop('_symlinks_out', 0)} symlinks under the read roots point outside them)")
    for name, outcome in res.items():
        check(f"locked, it cannot {name}", outcome != "ALLOWED", outcome)


def _snapshot(roots) -> dict:
    out = {}
    for root in roots:
        for d, _, files in os.walk(root):
            for f in files + [""]:
                p = os.path.join(d, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                out[p] = (st.st_size, st.st_mtime_ns)
    return out


def test_no_file_no_row(tmp: str):
    print("no file, no row, no browser")
    watch = [r for r in ("/tmp", os.path.expanduser("~"), "/var/tmp", "/dev/shm") if os.path.isdir(r)]
    before = _snapshot(watch)
    pic = asyncio.run(P.draw_picture(VISUAL, tokens=TOKENS))
    after = _snapshot(watch)
    check(f"a draw writes no file (watched: {watch})", pic.png is not None and before == after,
          sorted(set(after) ^ set(before))[:5])
    loaded = [m for m in sys.modules if m.startswith(("asyncpg", "server.brain_service", "server.runtime"))
              or "browser" in m or "app_verify" in m or "desktop_render" in m]
    check("no database, browser or render-check module was loaded", not loaded, loaded)


# ── The measured allowlists, measured again ────────────────────────────────

# Every measurement draws in the shipped font (the family the child sets) and
# with the box sizing check_page writes (_BOX_SHEET), so the boxes are sized
# as a page's are.
BASE = (P._BOX_SHEET + ".c{background-color:#203040;color:#ffffff;width:400px}"
        ".a{background-color:#aa3333}.b{background-color:#33aa33}")
DIVS = '<div class="c"><div class="a">Alpha</div><div class="b">Beta two words</div></div>'
FLEX = ".c{display:flex}.a{height:60px}"
A, B = (0xAA, 0x33, 0x33), (0x33, 0xAA, 0x33)
# property -> [(context css, candidate css)], each candidate a value the list
# allows that must change the picture against the context alone.
MEASURE = {
    "display": [("", ".c{display:flex}"), ("", ".b{display:none}")],
    "flex-direction": [(".c{display:flex}", ".c{flex-direction:column}"),
                       (".c{display:flex}", ".c{flex-direction:column-reverse}")],
    "justify-content": [(".c{display:flex}", f".c{{justify-content:{v}}}")
                        for v in ("flex-end", "center", "space-between", "space-around", "space-evenly")],
    "align-items": [(FLEX + ".b{font-size:10px}", f".c{{align-items:{v}}}")
                    for v in ("flex-start", "flex-end", "center")],
    "align-self": [(FLEX, f".b{{align-self:{v}}}") for v in ("flex-start", "flex-end", "center")],
    "flex-wrap": [(".c{display:flex;width:120px}.a{width:80px}.b{width:80px}", f".c{{flex-wrap:{v}}}")
                  for v in ("wrap", "wrap-reverse")],
    "gap": [(".c{display:flex}", ".c{gap:20px}"), (".c{display:flex}", ".c{gap:1em}")],
    "flex-grow": [(".c{display:flex}", ".a{flex-grow:1}")],
    "flex-shrink": [(".c{display:flex;width:100px}.a{width:80px}.b{width:80px}", ".a{flex-shrink:0}")],
    "width": [("", ".a{width:200px}"), ("", ".a{width:50%}"), ("", ".a{width:10em}")],
    "height": [("", ".a{height:50px}")],
    "min-width": [(".c{display:flex}", ".a{min-width:300px}")],
    "max-width": [("", ".a{max-width:50px}")],
    "min-height": [("", ".a{min-height:60px}")],
    "max-height": [(".a{height:40px}", ".a{max-height:10px}")],
    "aspect-ratio": [(".c{display:flex;align-items:flex-start}.a{width:100px}", ".a{aspect-ratio:2}")],
    "padding": [("", ".a{padding:10px}"), ("", ".a{padding:4px 20px}"), ("", ".a{padding:1em}"),
                ("", ".a{padding:2px 4px 6px}"), ("", ".a{padding:2px 4px 6px 8px}")],
    "padding-top": [("", ".a{padding-top:10px}")],
    "padding-right": [(".c{display:flex}", ".a{padding-right:30px}")],
    "padding-bottom": [("", ".a{padding-bottom:10px}")],
    "padding-left": [("", ".a{padding-left:30px}")],
    "margin": [("", ".a{margin:10px}"), ("", ".a{margin:4px 20px}"), ("", ".a{margin:2px 4px 6px 8px}")],
    "margin-top": [("", ".a{margin-top:10px}")],
    "margin-right": [(".c{display:flex}", ".a{margin-right:30px}")],
    "margin-bottom": [("", ".a{margin-bottom:10px}")],
    "margin-left": [("", ".a{margin-left:30px}"), ("", ".a{margin-left:1.5em}")],
    "border": [("", ".a{border:3px solid #ffff00}"), ("", ".a{border:3px dashed yellow}")],
    # As in a browser, a border draws only with a style, and the default
    # width is medium (3px), so the measured width is another one.
    "border-width": [(".a{border-style:solid;border-color:#ffff00}", ".a{border-width:6px}")],
    "border-style": [(".a{border:4px solid #ffff00}", f".a{{border-style:{v}}}") for v in ("dashed", "dotted")],
    "border-color": [(".a{border-style:solid;border-width:3px}", ".a{border-color:#ff00ff}")],
    "border-radius": [(".a{height:40px}", ".a{border-radius:12px}"), (".a{height:40px}", ".a{border-radius:1em}")],
    "border-top-left-radius": [(".a{height:40px}", ".a{border-top-left-radius:12px}")],
    "border-top-right-radius": [(".a{height:40px}", ".a{border-top-right-radius:12px}")],
    "border-bottom-left-radius": [(".a{height:40px}", ".a{border-bottom-left-radius:12px}")],
    "border-bottom-right-radius": [(".a{height:40px}", ".a{border-bottom-right-radius:12px}")],
    "background-color": [("", f".a{{background-color:{v}}}") for v in
                         ("#0000ff", "#00f", "#0000ff80", "rgb(0,0,255)", "rgba(0,0,255,0.5)", "navy",
                          "transparent")],
    "background-image": [("", f".a{{background-image:{v}}}") for v in
                         ("linear-gradient(90deg, #ff0000, #0000ff)", "linear-gradient(#ff0000, #0000ff)",
                          "linear-gradient(to bottom right, #ff0000 0%, #0000ff 100%)",
                          "linear-gradient(to right, red, blue, green)")],
    "box-shadow": [(".a{margin:12px}", ".a{box-shadow:0 4px 8px rgba(0,0,0,0.8)}"),
                   (".a{margin:12px}", ".a{box-shadow:0 2px 4px #000000, 0 8px 16px #000000}"),
                   (".a{margin:12px}", ".a{box-shadow:-2px -2px #ffff00}")],
    "text-shadow": [("", ".a{text-shadow:2px 2px 2px #000000}")],
    "color": [("", f".a{{color:{v}}}") for v in ("#ffff00", "#ff0", "rgb(255,255,0)", "rgba(255,255,0,0.5)",
                                                 "yellow")],
    # Measured on its own below: the shipped family is already the default.
    "font-family": [],
    "font-size": [("", ".a{font-size:24px}"), ("", ".a{font-size:1.5em}"), ("", ".a{font-size:150%}")],
    # Every weight but 400 (normal) draws differently from it.
    "font-weight": [("", f".a{{font-weight:{v}}}") for v in ("bold", "100", "200", "300", "500", "600",
                                                             "700", "800", "900")],
    "text-align": [("", f".a{{text-align:{v}}}") for v in ("right", "center")],
    "text-wrap": [(".c{width:60px}", ".b{text-wrap:nowrap}")],
}

# Where a browser puts .a and .b for the candidates that move or size a box:
# candidate -> f(L, base boxes, got boxes) -> bool, L being one line of text
# at 16px as drawn in the baseline. The browser's numbers follow from CSS
# itself (content-box sizes, block layout, flex layout), not from a render.
def _block(a, b):
    return lambda L, base, got: got == (a(L), b(L))


GEOMETRY = {
    ".a{width:200px}": _block(lambda L: (0, 0, 200, L), lambda L: (0, L, 400, 2 * L)),
    ".a{width:50%}": _block(lambda L: (0, 0, 200, L), lambda L: (0, L, 400, 2 * L)),
    ".a{width:10em}": _block(lambda L: (0, 0, 160, L), lambda L: (0, L, 400, 2 * L)),
    ".a{height:50px}": _block(lambda L: (0, 0, 400, 50), lambda L: (0, 50, 400, 50 + L)),
    ".a{max-width:50px}": _block(lambda L: (0, 0, 50, L), lambda L: (0, L, 400, 2 * L)),
    ".a{min-height:60px}": _block(lambda L: (0, 0, 400, 60), lambda L: (0, 60, 400, 60 + L)),
    ".a{max-height:10px}": _block(lambda L: (0, 0, 400, 10), lambda L: (0, 10, 400, 10 + L)),
    ".a{padding:10px}": _block(lambda L: (0, 0, 400, L + 20), lambda L: (0, L + 20, 400, 2 * L + 20)),
    ".a{padding:4px 20px}": _block(lambda L: (0, 0, 400, L + 8), lambda L: (0, L + 8, 400, 2 * L + 8)),
    ".a{padding:1em}": _block(lambda L: (0, 0, 400, L + 32), lambda L: (0, L + 32, 400, 2 * L + 32)),
    ".a{padding:2px 4px 6px}": _block(lambda L: (0, 0, 400, L + 8), lambda L: (0, L + 8, 400, 2 * L + 8)),
    ".a{padding-top:10px}": _block(lambda L: (0, 0, 400, L + 10), lambda L: (0, L + 10, 400, 2 * L + 10)),
    ".a{padding-bottom:10px}": _block(lambda L: (0, 0, 400, L + 10), lambda L: (0, L + 10, 400, 2 * L + 10)),
    ".a{margin:10px}": _block(lambda L: (10, 10, 390, 10 + L), lambda L: (0, L + 20, 400, 2 * L + 20)),
    ".a{margin:4px 20px}": _block(lambda L: (20, 4, 380, 4 + L), lambda L: (0, L + 8, 400, 2 * L + 8)),
    ".a{margin:2px 4px 6px 8px}": _block(lambda L: (8, 2, 396, 2 + L), lambda L: (0, L + 8, 400, 2 * L + 8)),
    ".a{margin-top:10px}": _block(lambda L: (0, 10, 400, 10 + L), lambda L: (0, 10 + L, 400, 10 + 2 * L)),
    ".a{margin-bottom:10px}": _block(lambda L: (0, 0, 400, L), lambda L: (0, L + 10, 400, 2 * L + 10)),
    ".a{margin-left:30px}": _block(lambda L: (30, 0, 400, L), lambda L: (0, L, 400, 2 * L)),
    ".a{margin-left:1.5em}": _block(lambda L: (24, 0, 400, L), lambda L: (0, L, 400, 2 * L)),
    ".a{border:3px solid #ffff00}": _block(lambda L: (3, 3, 397, 3 + L), lambda L: (0, L + 6, 400, 2 * L + 6)),
    # Flex: .a and .b sit side by side at their text widths.
    ".c{display:flex}": lambda L, base, got: (got[0][:2] == (0, 0) and got[1][0] == got[0][2]
                                              and got[0][3] == got[1][3] == L),
    ".c{gap:20px}": lambda L, base, got: got[1][0] - got[0][2] == 20,
    ".c{gap:1em}": lambda L, base, got: got[1][0] - got[0][2] == 16,
    ".c{justify-content:flex-end}": lambda L, base, got: got[1][2] == 400 and got[1][0] == got[0][2],
    ".c{flex-direction:column}": _block(lambda L: (0, 0, 400, L), lambda L: (0, L, 400, 2 * L)),
    ".c{flex-direction:column-reverse}": _block(lambda L: (0, L, 400, 2 * L), lambda L: (0, 0, 400, L)),
    ".a{flex-grow:1}": lambda L, base, got: got[0][2] == got[1][0] and got[1][2] == 400,
    ".a{min-width:300px}": lambda L, base, got: got[0][2] - got[0][0] == 300 and got[1][0] == 300,
    ".a{flex-shrink:0}": lambda L, base, got: got[0][2] - got[0][0] == 80,
    ".a{aspect-ratio:2}": lambda L, base, got: got[0] == (0, 0, 100, 50),
    ".b{align-self:flex-start}": lambda L, base, got: got[1][1] == 0 and got[1][3] == L,
    ".b{align-self:flex-end}": lambda L, base, got: got[1][3] == 60 and got[1][1] == 60 - L,
}


def test_allowlists_still_draw():
    print("the measured allowlists, measured again")
    check("every allowed property has a measurement here", set(MEASURE) == set(P.ALLOWED_PROPERTIES),
          sorted(set(P.ALLOWED_PROPERTIES) ^ set(MEASURE)))
    plain, _ = _unconfined(DIVS, BASE)
    L = _boxes(plain, A)[0][3] if plain is not None else 0
    for prop, rows in MEASURE.items():
        for ctx, cand in rows:
            decls = cand[cand.index("{") + 1:-1]
            value = decls.split(":", 1)[1]
            on_list = P._PROPERTY_RE[prop].match(value) is not None
            base, bw = _unconfined(DIVS, BASE + ctx)
            img, w = _unconfined(DIVS, BASE + ctx + cand)
            changed = base is not None and img is not None and not _same(base, img)
            placed = ""
            if cand in GEOMETRY and changed:
                got = tuple(_boxes(img, A, B))
                ok = all(got) and GEOMETRY[cand](L, tuple(_boxes(base, A, B)), got)
                placed = " where a browser puts it" if ok else f" MISPLACED {got} (line {L})"
            check(f"{cand} is on the list and draws{placed}",
                  on_list and not bw and not w and changed and "MISPLACED" not in placed,
                  f"on_list={on_list} warnings={(bw + w)[:2]} changed={changed}{placed}")
    check("a line of 16 px text is 20 px tall in the baseline (the unit the placements use)", L == 20, L)

    # The lists match without regard to case, as CSS reads values, and Takumi
    # does not always (handed linear-gradient(90DEG, …) it drops the whole
    # gradient, without a word). So every measured value, written in upper
    # case and passed through the gate's own parse, draws as in lower case.
    upper = []
    for prop, rows in MEASURE.items():
        for ctx, cand in rows:
            sel, body = cand.split("{", 1)
            name, value = body[:-1].split(":", 1)
            loud = f"{sel}{{{name}:{value.upper()}}}"
            lo, _ = _unconfined(DIVS, BASE + P._serialize_css(P._parse_css(ctx + cand)))
            hi, _ = _unconfined(DIVS, BASE + P._serialize_css(P._parse_css(ctx + loud)))
            if not _same(lo, hi):
                upper.append(loud)
    check("every measured value written in upper case draws, through the gate, exactly as in lower case",
          not upper, upper[:6])

    # font-family: the shipped family is the child's default, so naming it
    # draws the same pixels, and not those of the other font in the wheel.
    fam = f'.a{{font-family:"{P.SHIPPED_FONT}"}}'
    named, _ = _unconfined(DIVS, BASE + fam)
    geist, _ = _unconfined(DIVS, BASE + '.a{font-family:"Geist"}')
    check(f"{fam} is on the list and draws in the shipped font, not the wheel's embedded Geist",
          P._PROPERTY_RE["font-family"].match(f'"{P.SHIPPED_FONT}"') is not None
          and _same(named, plain) and not _same(named, geist))

    # Every named colour, exactly.
    from PIL import ImageColor
    wrong = []
    for name in sorted(P.NAMED_COLOURS | {"transparent"}):
        img, w = _unconfined(DIVS, BASE + f".a{{background-color:{name}}}")
        want = (0x20, 0x30, 0x40) if name == "transparent" else ImageColor.getrgb(name)[:3]
        got = img.getpixel((396, 2))[:3] if img is not None else None
        if got != want or w or not P._PROPERTY_RE["color"].match(name):
            wrong.append((name, got, want))
    check(f"all {len(P.NAMED_COLOURS)} named colours and transparent are on the list and draw as "
          "their sRGB value", len(P.NAMED_COLOURS) == 148 and not wrong, wrong[:6])

    print("box sizing, as in a browser")
    for css, want_w in ((".a{width:200px;padding:10px}", 220), (".a{width:200px;border:5px solid #ffff00}", 210)):
        page = P.check_page(f'<style>.a{{background-color:#aa3333}}{css}</style><div class="a">x</div>', {}, 400)
        img, _ = _unconfined(page.html, page.css)
        box = _boxes(img, A, (0xFF, 0xFF, 0x00)) if img is not None else [None, None]
        outer = [b for b in box if b]
        width = max(b[2] for b in outer) - min(b[0] for b in outer) if outer else 0
        check(f"{css} draws {want_w} px wide (content-box; Takumi's own default is border-box)",
              width == want_w, (width, box))
    page = P.check_page('<style>body{padding:20px}.a{background-color:#aa3333}</style><div class="a">x</div>', {}, 400)
    img, _ = _unconfined(page.html, page.css)
    check("padding on the body stays inside the caller's width, as a body's does in a viewport",
          img is not None and img.size[0] == 400 and _boxes(img, A)[0][::2] == (20, 380),
          img and (img.size, _boxes(img, A)))

    print("the browser defaults the renderer gives the tags")
    for tag, plain_tag in (("h1", "div"), ("h2", "div"), ("h3", "div"), ("strong", "span"), ("b", "span")):
        a = P.check_page(f"<div><{tag}>Sample text</{tag}></div>", {})
        b = P.check_page(f"<div><{plain_tag}>Sample text</{plain_tag}></div>", {})
        ia, wa = _unconfined(a.html, a.css)
        ib, _ = _unconfined(b.html, b.css)
        check(f"<{tag}> draws differently from <{plain_tag}>", ia is not None and ib is not None and not wa
              and not _same(ia, ib), wa)
    for tag, by_hand in (("h1", "font-size:2em;font-weight:bold;margin-top:0.67em;margin-bottom:0.67em"),
                         ("p", "margin-top:1em;margin-bottom:1em")):
        a = P.check_page(f"<div><{tag}>Sample text</{tag}><div>next</div></div>", {})
        b = P.check_page(f"<style>.h{{{by_hand}}}</style><div><div class=\"h\">Sample text</div>"
                         "<div>next</div></div>", {})
        check(f"<{tag}> draws exactly as a div given a browser's defaults for it by hand",
              _same(_unconfined(a.html, a.css)[0], _unconfined(b.html, b.css)[0]))
    a = P.check_page('<style>.w{font-weight:600}</style><div class="w">x <strong>Sample</strong></div>', {})
    b = P.check_page('<style>.w{font-weight:600}.x{font-weight:900}</style>'
                     '<div class="w">x <span class="x">Sample</span></div>', {})
    check("<strong> in a 600-weight box draws at 900, one step bolder, as in a browser",
          _same(_unconfined(a.html, a.css)[0], _unconfined(b.html, b.css)[0]))

    print("text beside inline tags")
    one = P.check_page("<p>Total: $42 today</p>", {})
    mixed = P.check_page("<p>Total: <b>$42</b> today</p>", {})
    i1, _ = _unconfined(one.html, one.css)
    i2, _ = _unconfined(mixed.html, mixed.css)
    check("\"Total: <b>$42</b> today\" in a <p> draws on one line, as in a browser",
          i1 is not None and i2 is not None and i1.size == i2.size and not _same(i1, i2), (i1 and i1.size, i2 and i2.size))
    spans = P.check_page("<div><span>A</span><span>B</span></div>", {})
    run = P.check_page("<div>AB</div>", {})
    check("two spans side by side draw exactly like their text run together",
          _same(_unconfined(spans.html, spans.css)[0], _unconfined(run.html, run.css)[0]))


def test_image_contents():
    print("what the image carries for this")
    import glob
    import hashlib
    libdir = "/usr/lib/" + {"x86_64": "x86_64-linux-gnu", "aarch64": "aarch64-linux-gnu"}[os.uname().machine]
    gl = sorted(os.path.basename(p) for p in glob.glob(libdir + "/lib*.so*")
                if os.path.basename(p).startswith(("libGL.", "libEGL.", "libGLX", "libGLdispatch.", "libX11.")))
    check("no GL or X11 library: nothing in the image draws with them since Skia left", not gl, gl)
    mesa = (glob.glob(libdir + "/dri") + glob.glob(libdir + "/libLLVM*") + glob.glob(libdir + "/libgallium*"))
    check("no Mesa: no DRI drivers, no LLVM", not mesa, mesa)
    fonts = glob.glob("/usr/share/fonts/*") + glob.glob("/etc/fonts")
    check("no font package and no fontconfig: the one font is the shipped Inter", not fonts, fonts)
    try:
        with open(FONT_PATH, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError as e:
        digest = repr(e)
    check(f"the shipped font is InterVariable.ttf from the Inter 4.1 release ({FONT_SHA256[:12]}…)",
          C.FONT_FILE == FONT_PATH and digest == FONT_SHA256, (C.FONT_FILE, digest))
    lic = os.path.join(os.path.dirname(FONT_PATH), "LICENSE.txt")
    check("its OFL licence is beside it", os.path.isfile(lic) and "SIL Open Font License" in open(lic).read())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="picture-gate-") as tmp:
        test_image_contents()
        card = test_draws()
        test_refusals(tmp)
        if card:
            test_confinement(card, tmp)
            test_escape_routes(tmp, os.path.join(tmp, "real.png"))
        else:
            check("a picture to test confinement with", False)
        test_no_file_no_row(tmp)
        test_allowlists_still_draw()
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} failed, {PASSES} passed: {FAILS[:6]}")
        return 1
    print(f"OK connector picture in the image: {PASSES} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
