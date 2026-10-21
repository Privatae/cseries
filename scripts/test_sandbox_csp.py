#!/usr/bin/env python3
"""Every host-authored frame is mounted under the sandbox CSP — measured.

These are the surfaces that render HTML the host (or a coding harness) wrote,
in a srcdoc frame with sandbox="allow-scripts":

    dashboard/base/js/desktop-surface.js   the HTML desktop
    system-apps/artifacts/app.js           the artifact preview, and its history view
    system-apps/designs/app.js             design thumbnails and previews
    system-apps/network/app.js             the host-authored network map
    workbench-web/…/version-view.tsx       a run's or module version's page (its
                                           behaviour is driven in the
                                           BUILT app by scripts/test_workbench_version_view.py)
    app-web-kit/visuals/sealed-frame.tsx   a visual in a Host's reply and an artifact
                                           card, in the Circles and Workbench chats
                                           (driven in both BUILT apps
                                           by scripts/test_chat_visuals.py)
    workbench-web/…/run-card.tsx           a landed run's version, inline in its run
                                           card: the SAME
                                           document the version view mounts, from its
                                           own hook (useVersionDoc); driven in the BUILT
                                           app by test_workbench_version_view.py (Z) and
                                           test_workbench_panes.py (C)

The sandbox attribute gives a frame an opaque origin; it does not take away the
network (measured in Chromium: fetch() and new Image().src reached an
external server from a CSP-less srcdoc frame).

WHY THIS FILE WAS REWRITTEN. Its surface checks matched source
LINES: "no `.srcdoc =` line without `sealed(` on it", "the text `did not load`
appears". Correct code failed them — Designs' refusal is split across two
string literals ("did not " + "load"), a ternary puts `sealed(` on the next line,
Artifacts' static placeholder is a string on its own — and the payload check
sliced a function with a regex and ran it without a helper it had grown
(`barOwned is not defined`). Four failures on main, all on correct code: a gate
that fails on correct code is ignored, and then it catches nothing.

What it checks now:

  1. THE POLICY, in a real browser: every lane a document could leak through
     (fetch, XHR, Image, <img>, sendBeacon, WebSocket, CSS url(), prefetch,
     <iframe src>, window.open, form POST) is closed by the ONE definition in
     dashboard/base/js/sandbox-csp.js, and the same document WITHOUT it leaks.

  2. EACH SURFACE'S OWN SEALING CODE, executed in that browser. The sealing
     function (`sealed`; for the Network map, the span of its render that
     builds the frame document) is cut out of the shipped file by a
     token-aware scan — strings, template literals, regex literals and
     comments are understood, so layout, quoting and prose never matter — and
     run on a host-authored document that tries every lane:
       - with the policy loaded: the document's own script still runs, the
         frame carries the policy, and nothing reaches the probe;
       - with the policy MISSING: the host document does not run at all, and
         the frame says the policy did not load (FAIL CLOSED).
  3. EVERY SINK, classified on the token stream: each `.srcdoc =`,
     setAttribute('srcdoc', …) and srcdoc="${…}" is a call to the surface's
     sealing function, a static literal, or a ternary of those. And EVERY WAY
     a document can reach a frame is accounted for, found by capability rather
     than by syntax: every mention of srcdoc (any spelling or form — property,
     bracket, object key, attribute text, JSX), createObjectURL, a
     data:text/html URL, or a frame's document (contentDocument, document
     .write/open), in every frontend source (.js .mjs .cjs .jsx .ts .tsx .html
     under dashboard/, system-apps/, app-web-kit/ and EVERY top-level
     <name>-web/src, derived from the tree so a new one is read the day it
     lands — circles-web, community-web and sites-web once were not), must be
     a sealed sink of a registered surface, an unmount, the one srcDoc of a
     pinned TSX frame (TSX_SINKS), or an ALLOWED use with its count and why.
     A form the sink reader
     does not know is therefore a failure, not a pass (bracket access,
     Object.assign, unquoted or
     concatenated attribute text, .mjs, another .tsx and a blob: frame each
     passed the syntax list).
  4. THE NOTICE: where a surface names what the policy blocked (the Artifacts
     viewer, the Workbench window and run card), the notice is executed
     (Artifacts) or pinned (the rest), and the viewer calls it.
  5. The desktop's outbound payload is scoped to the declared mode — the
     shipped _postState run under node with its closures stubbed and the
     helpers it calls taken from the shipped file.
  6. THE TWO LANES NOTHING HERE CLOSES, pinned: WebRTC (STUN) and
     self-navigation still leave a sealed frame — with `webrtc 'block'` and
     `navigate-to 'none'` added, and from a nested srcdoc realm. These PASS
     while the lanes stay open and FAIL the day Chromium closes one, saying
     what to change; sandbox-csp.js documents both with the same evidence.

Mutation evidence: scripts/test_sandbox_csp.py fails on a surface that skips the
policy, on one that does not fail closed, and on one that drops its notice
(each of the five surfaces; see the commit that rewrote this file).

Run:  python3 scripts/test_sandbox_csp.py   (the probe takes a free port;
      SANDBOX_CSP_PORT pins one)
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socketserver
import sys
import threading

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")

# 0 = a free port, read back once the probe is bound (main): this gate runs in
# the pre-merge pool beside other gates, and CI runs several jobs on one host
# network, so a fixed port would collide.
PORT = int(os.environ.get("SANDBOX_CSP_PORT", "0"))
hits: list[str] = []
failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/parent-origin"):
            body = b"<!DOCTYPE html><html><body>parent</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        hits.append(self.path)
        self.send_response(204)
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *a):
        pass


class _ProbeServer(socketserver.ThreadingTCPServer):
    # Set on the CLASS: TCPServer binds in its constructor, so setting it on
    # the instance afterwards (as this gate used to) never applied, and a
    # second run inside TIME_WAIT died on "Address already in use".
    allow_reuse_address = True
    daemon_threads = True


def read(rel: str) -> str:
    return open(os.path.join(_ROOT, rel), encoding="utf-8").read()


# ── A token-aware view of JavaScript source ──────────────────────────────────
# mask(src) returns a string of the SAME length in which every comment is
# blanked, and the inside of every string literal, template-literal text part
# and regex literal is replaced by 'x' (newlines kept). Code — including the
# expressions inside ${…} — is untouched. Everything structural below
# (function spans, statement ends, sinks, ternaries) is read from the mask, so
# a brace, quote, `//` or keyword inside a string or regex can never be
# mistaken for code, and a line break is only whitespace.

_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_KW = {"return", "typeof", "instanceof", "in", "of", "new", "delete",
                   "void", "throw", "case", "do", "else", "yield", "await"}


def mask(src: str) -> str:
    out = list(src)
    n = len(src)

    def x(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = "x"

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    i = 0
    last = ""               # last significant token: a punctuator char or a word
    braces = 0
    subst: list[int] = []   # brace depth at which each open ${ … } began

    def template_text(i):
        """Scan template TEXT from i (just after ` or after a closing }).
        Returns (next_index, closed_template)."""
        j = i
        while j < n:
            ch = src[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                x(i, j)
                return j + 1, True
            if ch == "$" and j + 1 < n and src[j + 1] == "{":
                x(i, j)
                return j + 2, False
            j += 1
        x(i, n)
        return n, True

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            x(i + 1, min(j, n))
            i = j + 1
            last = "str"
            continue
        if c == "`":
            i, closed = template_text(i + 1)
            if not closed:
                braces += 1
                subst.append(braces)
            last = "str" if closed else "{"
            continue
        if c == "/" and (last == "" or last in _REGEX_AFTER or last in _REGEX_AFTER_KW):
            j, klass = i + 1, False
            while j < n and src[j] != "\n":
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    klass = True
                elif ch == "]":
                    klass = False
                elif ch == "/" and not klass:
                    break
                j += 1
            x(i + 1, j)
            j += 1
            while j < n and (src[j].isalpha()):
                j += 1
            i = j
            last = "re"
            continue
        if c == "{":
            braces += 1
        elif c == "}":
            if subst and subst[-1] == braces:
                subst.pop()
                braces -= 1
                i, closed = template_text(i + 1)
                if not closed:
                    braces += 1
                    subst.append(braces)
                last = "str" if closed else "{"
                continue
            braces -= 1
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            last = src[i:j]
            i = j
            continue
        last = c
        i += 1
    return "".join(out)


_OPEN, _CLOSE = "([{", ")]}"


def match_close(m: str, i: int) -> int:
    """Index of the bracket closing the one at m[i] (on the mask)."""
    depth = 0
    for k in range(i, len(m)):
        if m[k] in _OPEN:
            depth += 1
        elif m[k] in _CLOSE:
            depth -= 1
            if depth == 0:
                return k
    return -1


def statement_end(m: str, i: int) -> int:
    """Index just past the expression starting at i: a `;` at depth 0, or the
    bracket that closes the enclosing block."""
    depth = 0
    for k in range(i, len(m)):
        ch = m[k]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            if depth == 0:
                return k
            depth -= 1
        elif ch == ";" and depth == 0:
            return k
    return len(m)


def function_span(src: str, m: str, name: str):
    """(start, end) of `function name(…) {…}` (async or not), or None."""
    fm = re.search(r"\b(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", m)
    if not fm:
        return None
    p = match_close(m, fm.end() - 1)
    b = m.find("{", p)
    e = match_close(m, b) if b >= 0 else -1
    return (fm.start(), e + 1) if e > 0 else None


def function_source(src: str, m: str, name: str):
    sp = function_span(src, m, name)
    return src[sp[0]:sp[1]] if sp else None


def _strip_parens(e: str) -> str:
    e = e.strip()
    while e.startswith("(") and match_close(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    return e


def _top_ternary(e: str):
    """(cond, a, b) when e is a top-level ternary, else None (on the mask)."""
    depth, q = 0, -1
    k = 0
    while k < len(e):
        ch = e[k]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
        elif ch == "?" and depth == 0:
            if e[k + 1:k + 2] in ("?", ".") or e[k - 1:k] == "?":
                k += 2
                continue
            q = k
            break
        k += 1
    if q < 0:
        return None
    depth, nest = 0, 0
    for k in range(q + 1, len(e)):
        ch = e[k]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
        elif depth == 0 and ch == "?" and e[k + 1:k + 2] not in ("?", "."):
            nest += 1
        elif depth == 0 and ch == ":":
            if nest == 0:
                return e[:q], e[q + 1:k], e[k + 1:]
            nest -= 1
    return None


_STATIC = re.compile(r"\s*(?:(?:'x*'|\"x*\"|`[x\n]*`)\s*(?:\+\s*)?)+\s*")


def sealed_expr(e: str, sealers: set[str]) -> bool:
    """On the MASK of an expression: is every value it can produce either a
    call to a sealing function or a static literal (no host content)?"""
    e = _strip_parens(e)
    t = _top_ternary(e)
    if t:
        return sealed_expr(t[1], sealers) and sealed_expr(t[2], sealers)
    if _STATIC.fullmatch(e):
        return True
    cm = re.match(r"(?:window\.)?([A-Za-z_$][\w$]*)\s*\(", e)
    return bool(cm and cm.group(1) in sealers and match_close(e, cm.end() - 1) == len(e) - 1)


def sinks(src: str, m: str) -> list[tuple[str, str, int, int]]:
    """Every place a document is handed to a frame, in the forms this reader
    knows: (kind, expression MASK, line, offset of the `srcdoc` it uses)."""
    out = []
    for fm in re.finditer(r"\.srcdoc\s*=(?![=+])", m):
        a = fm.end()
        out.append(("srcdoc =", m[a:statement_end(m, a)], src.count("\n", 0, a) + 1, fm.start() + 1))
    for fm in re.finditer(r"\bsetAttribute\s*\(", m):
        close = match_close(m, fm.end() - 1)
        at = re.match(r"\s*['\"](srcdoc)['\"]\s*,", src[fm.end():close], re.I)
        if at:
            comma = m.find(",", fm.end())
            out.append(("setAttribute(srcdoc)", m[comma + 1:close], src.count("\n", 0, fm.end()) + 1,
                        fm.end() + at.start(1)))
    # srcdoc="${…}" in template text: the attribute sits in masked text, the
    # expression right after it is code.
    for fm in re.finditer(r"srcdoc\s*=\s*[\"']\$\{", src):
        a = fm.end()
        if m[fm.start()] != "x":        # not inside template text: not this form
            continue
        e = match_close(m, a - 1)
        # the attribute must close right after the expression: "${x}" — not "${x}…more"
        if src[e + 1:e + 2] != src[a - 3]:
            out.append(('srcdoc="${…}…" (more text in the attribute)', "<not one expression>",
                        src.count("\n", 0, a) + 1, fm.start()))
            continue
        out.append(('srcdoc="${…}"', m[a:e], src.count("\n", 0, a) + 1, fm.start()))
    return out


# ── every way a document can reach a frame ───────────────────────────────────
def _web_source_roots() -> list[str]:
    """Every top-level <name>-web/src: a React app whose build lands in
    system-apps/<id>/assets, which the walk skips as build output, so its
    SOURCE is where a frame sink would be read."""
    return [f"{n}/src" for n in sorted(os.listdir(_ROOT))
            if n.endswith("-web") and os.path.isdir(os.path.join(_ROOT, n, "src"))]


FRONTEND_ROOTS = ["dashboard", "system-apps", "app-web-kit", *_web_source_roots()]
_FRONTEND_FILE = re.compile(r"\.(m?js|cjs|jsx|ts|tsx|html)$")
_SKIP_DIRS = {"node_modules", "assets", "dist", ".git", "vendor"}
CAPABILITIES = {
    "srcdoc": r"srcdoc",
    "blob URL": r"createObjectURL",
    "data:text/html URL": r"data:\s*text/html",
    "a frame's document": r"contentDocument|contentWindow\s*\.\s*document\b|\bdocument\s*\.\s*(?:write|writeln|open)\s*\(",
}
_UNMOUNT = re.compile(r"\.removeAttribute\s*\(\s*['\"](srcdoc)['\"]\s*\)", re.I)
# (file, capability) -> (how many, why it is not a document handed to a frame).
# A count that changes, or an entry that no longer matches, fails: a new use is
# read before it is allowed.
ALLOWED_USES = {
    ("dashboard/base/js/app-runner.js", "a frame's document"):
        (1, "deliverPromptWhenReady READS the composer out of an app window it opened, to hand it a "
            "prompt; it writes no document, and a cross-origin frame throws (caught: never delivered)"),
    ("dashboard/base/js/desktop-surface.js", "a frame's document"):
        (1, "appFrameRoots() READS same-origin app frames' root elements to restyle them on a theme "
            "change; an isolated app throws (caught) and restyles itself"),
    ("dashboard/base/js/model-picker.js", "blob URL"):
        (1, "a coding-protocol export: JSON handed to an <a download> and revoked; nothing renders it"),
    ("workbench-web/src/components/ai-elements/conversation.tsx", "blob URL"):
        (1, "the conversation download: markdown handed to an <a download> and revoked"),
    ("workbench-web/src/components/ai-elements/prompt-input.tsx", "blob URL"):
        (2, "the person's own attached files, held as blob: URLs until sent (turned into data: URLs at "
            "submit) and revoked on removal; workbench-web mounts no frame but version-view's"),
    ("system-apps/backups/app.js", "blob URL"):
        (1, "the recovery key file the owner saves: text/plain made in the page, handed to an <a download> "
            "and revoked; the page renders nothing from it, and the key is never sent"),
    ("app-web-kit/visuals/visual-block.tsx", "blob URL"):
        (1, "a visual's Download: the Host's page handed to an <a download> as application/octet-stream "
            "and revoked — never text/html, so even a navigation to the blob: URL could only save it"),
}


def frontend_files():
    for d in FRONTEND_ROOTS:
        for root, dirs, files in os.walk(os.path.join(_ROOT, d)):
            dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
            for fn in files:
                if _FRONTEND_FILE.search(fn) and ".min." not in fn:
                    yield os.path.normpath(os.path.relpath(os.path.join(root, fn), _ROOT))


def mentions(rel: str, src: str):
    """(capability, offset, line) for every mention outside a comment."""
    m = mask(src) if not rel.endswith(".html") else re.sub(
        r"<!--.*?-->", lambda c: " " * len(c.group(0)), src, flags=re.S)
    for cap, rx in CAPABILITIES.items():
        for mm in re.finditer(rx, src, re.I):
            p = mm.start()
            if m[p] == " " and not src[p].isspace():
                continue                        # a comment
            yield cap, p, src.count("\n", 0, p) + 1


def unaccounted(registered: set[str]) -> tuple[list[str], dict]:
    """Every mention that is not a recognised sink of a registered surface, an
    unmount, a pinned TSX frame's one srcDoc or an allowed use; and the
    per-entry counts of the allowed uses found."""
    bad, counts = [], {k: 0 for k in ALLOWED_USES}
    for rel in frontend_files():
        try:
            src = read(rel)
        except (OSError, UnicodeDecodeError):
            continue
        found = list(mentions(rel, src))
        if not found:
            continue
        m = mask(src) if not rel.endswith(".html") else src
        sink_at = {off for *_r, off in sinks(src, m)} if rel in registered else set()
        unmount_at = {u.start(1) for u in _UNMOUNT.finditer(src)}
        for cap, p, line in found:
            if cap == "srcdoc" and (p in sink_at or p in unmount_at):
                continue
            tsx_sink = TSX_SINKS.get(rel)
            if tsx_sink and cap == "srcdoc" and src.startswith(tsx_sink, p) \
                    and re.sub(r"(?m)^\s*//.*$", "", src).count("srcDoc={") == 1:
                continue
            if (rel, cap) in ALLOWED_USES:
                counts[(rel, cap)] += 1
                continue
            snippet = " ".join(src[max(0, p - 30):p + 40].split())
            bad.append(f"{rel}:{line} {cap} [{snippet}]")
    return bad, counts


# ── the surfaces ─────────────────────────────────────────────────────────────
JS_SURFACES = {
    "the HTML desktop": {"file": "dashboard/base/js/desktop-surface.js", "sealers": {"sealed"}},
    "the artifacts window": {"file": "system-apps/artifacts/app.js", "sealers": {"sealed"},
                             "notice": ("noteExternalRefs", "renderCurrent")},
    "the designs previews": {"file": "system-apps/designs/app.js", "sealers": {"sealed"},
                             "prelude": "var state = { apps: [{ id: 'circles', name: 'Circles' }], host: 'Sage' };"},
    # The map inlines the policy while it builds its frame document; its sink
    # is srcdoc="${attr(withBridge)}" and `withBridge` is what gets executed.
    "the network map": {"file": "system-apps/network/app.js", "sealers": set(),
                        "sink_ok": r"\s*attr\s*\(\s*withBridge\s*\)\s*"},
}
TSX_SURFACE = "workbench-web/src/components/workbench/version-view.tsx"
# Visuals and artifact cards in the Circles and Workbench chats: one
# sealed frame for both apps. Its behaviour — sealed, fail closed, the notice —
# is driven in both built apps by scripts/test_chat_visuals.py.
KIT_SURFACE = "app-web-kit/visuals/sealed-frame.tsx"
KIT_SEALER = "app-web-kit/visuals/visual.ts"
# A landed run's version inline in its run card (6f23efe33): the version
# view's own document, from its own hook (pinned in structure()).
RUN_CARD_SURFACE = "workbench-web/src/components/workbench/run-card.tsx"
# Each TSX frame's ONE srcDoc, the only srcDoc its file may hold (JSX is not
# JavaScript, so the sink reader does not apply); each is pinned in structure().
TSX_SINKS = {os.path.normpath(rel): sink for rel, sink in (
    (TSX_SURFACE, "srcDoc={html}"), (KIT_SURFACE, "srcDoc={doc}"), (RUN_CARD_SURFACE, "srcDoc={doc.html}"))}
SURFACE_PAGES = {"system-apps/artifacts/index.html": "app.js", "system-apps/designs/index.html": "app.js",
                 "system-apps/network/index.html": "app.js",
                 "dashboard/base/index.html": None,
                 "workbench-web/index.html": "/src/main.tsx", "circles-web/index.html": "/src/main.tsx"}
SINK_SCAN_DIRS = ["dashboard/base/js", "system-apps"]


def _network_render_code(src: str, m: str):
    """The Network map's own code that turns the Host's document into the
    frame document: hostedCsp(), HOSTED_BRIDGE, and the span of the render
    from `const csp = hostedCsp()` through `const withBridge = …;` — wrapped as
    a function of `doc`. None when any piece is missing."""
    hc = function_source(src, m, "hostedCsp")
    bm = re.search(r"\bconst\s+HOSTED_BRIDGE\s*=", m)
    cm = re.search(r"\bconst\s+csp\s*=\s*hostedCsp\s*\(\s*\)", m)
    wm = re.search(r"\bconst\s+withBridge\s*=", m)
    if not (hc and bm and cm and wm) or wm.start() < cm.start():
        return None
    bridge = src[bm.start():statement_end(m, bm.end()) + 1]
    span = src[cm.start():statement_end(m, wm.end()) + 1]
    return ("(function () {\n" + hc + "\n" + bridge + "\n"
            "function empty(t, b) { return '<h2>' + t + '</h2><p>' + b + '</p>'; }\n"
            "return function (doc) {\n" + span + "\nreturn withBridge;\n};\n})()")


def sandbox_only_by_its_frame(rel: str) -> bool:
    """A TSX surface's sandbox is set in exactly one place, its one <iframe>'s
    own attribute. Attribute-text pins alone miss a ref that calls
    setAttribute("sandbox", …) or a prop spread on the <iframe>: both widened
    the frame to allow-same-origin with every other check green (measured by
    mutation). The `sandbox` name is counted in code
    (comments and strings masked — a fail-closed notice may say "sandbox");
    the rest is read with comments blanked and strings kept."""
    src = read(rel)
    m = mask(src)
    code = "".join(" " if (mc == " " and not c.isspace()) else c for c, mc in zip(src, m))
    frames = re.findall(r"<iframe\b(.*?)/>", code, re.S)
    return ("allow-same-origin" not in code and len(re.findall(r"(?<![\w-])sandbox(?![\w-])", m)) == 1
            and "setAttribute" not in code and len(frames) == 1 and "{..." not in frames[0])


def structure() -> dict:
    """Section 3 + 4 (static half) + the executable pieces for sections 2 and 4."""
    runnable: dict = {}
    for label, cfg in JS_SURFACES.items():
        src = read(cfg["file"])
        m = mask(src)
        check(m.count("{") - m.count("}") == 0,
              f"{label}: the scanner reads the file as balanced code (sanity)")
        check("CSeriesSandbox" in m, f"{label} mounts through the shared definition, not its own copy")
        found = sinks(src, m)
        check(bool(found), f"{label}: its frame sinks are found ({len(found)})")
        for kind, expr, line, _at in found:
            ok = (re.fullmatch(cfg["sink_ok"], expr) is not None) if cfg.get("sink_ok") else sealed_expr(expr, cfg["sealers"])
            check(ok, f"{label}: the {kind} sink at line {line} is sealed or static "
                      f"[{' '.join(expr.split())[:80]}]")
        if cfg["sealers"]:
            fn = function_source(src, m, "sealed")
            check(fn is not None, f"{label}: its sealing function is found")
            if fn:
                runnable[label] = ("(function () {\n" + cfg.get("prelude", "") + "\n" + fn
                                   + "\nreturn sealed;\n})()")
        else:
            # Its own render code, cut out as a function of the document: the
            # surface's builder, else the Network map's span.
            code = cfg.get("runnable", _network_render_code)(src, m)
            check(code is not None, f"{label}: the render code that builds the frame document is found")
            if code:
                runnable[label] = code
        if cfg.get("notice"):
            fname, caller = cfg["notice"]
            fn = function_source(src, m, fname)
            check(fn is not None, f"{label}: its blocked-assets notice ({fname}) is found")
            cs = function_span(src, m, caller)
            body = m[cs[0]:cs[1]] if cs else ""
            check(bool(re.search(r"\.srcdoc\s*=", body)) and bool(re.search(r"\b" + fname + r"\s*\(", body)),
                  f"{label}: the viewer that mounts the document ({caller}) also calls {fname}()")
            if fn:
                runnable[label + " notice"] = "(function () {\n" + fn + "\nreturn " + fname + ";\n})()"

    # Every way a document can reach a frame, in any frontend file, is a sealed
    # sink of a registered surface, an unmount, the Workbench's one srcDoc, or
    # an allowed use with its count and why — found by capability, so a form
    # the sink reader does not know fails instead of passing.
    registered = {os.path.normpath(c["file"]) for c in JS_SURFACES.values()}
    want_roots = {"workbench-web/src", "circles-web/src", "community-web/src", "sites-web/src"}
    check(want_roots <= set(FRONTEND_ROOTS),
          f"every <name>-web source tree is read for frame sinks (missing: {sorted(want_roots - set(FRONTEND_ROOTS))})")
    bad, counts = unaccounted(registered)
    check(not bad, "every srcdoc, blob URL, data:text/html URL and frame document in the frontend is a "
                   "sealed sink of a registered surface, an unmount, a pinned TSX frame's srcDoc or an allowed use"
                   + ("".join("\n          " + b for b in bad) if bad else ""))

    for (rel, cap), (n, _why) in ALLOWED_USES.items():
        check(counts[(rel, cap)] == n, f"allowed use {rel} {cap}: still exactly {n} "
                                       f"(found {counts[(rel, cap)]}) — a new use is read before it is allowed")

    # The Workbench window is TSX (JSX text is not JavaScript, so the scanner
    # above does not apply). Pinned on whitespace-normalised code with line
    # comments removed; its behaviour — sealed, fail closed, the notice — is
    # driven in the built app by test_workbench_version_view (S, N, X, Z, B).
    tsx = " ".join(re.sub(r"(?m)^\s*//.*$", "", read(TSX_SURFACE)).split())
    check(tsx.count("srcDoc={") == 1 and "srcDoc={html}" in tsx,
          "the workbench window: one frame sink, fed from the html state")
    sets = re.findall(r"setHtml\(([^()]*)\)", tsx)
    check(bool(sets) and all(a.strip() in ("null", "mounted.doc") for a in sets),
          f"the workbench window only ever mounts what mountable() returned: setHtml({sets})")
    mbody = tsx[tsx.find("async function mountable("):tsx.find("export function VersionView")]
    check("return { doc: sb.withSandboxCsp(" in mbody,
          "the workbench window: mountable() seals through the shared withSandboxCsp")
    check(re.search(r"if \(!sb\) return null;", mbody) is not None
          and "setErr(SANDBOX_MISSING)" in tsx and "did not load" in tsx,
          "the workbench window FAILS CLOSED: no policy, nothing mounted, and it says so")
    check("data-blocked-refs" in tsx and "{blockedNotice(blocked)}" in tsx
          and "setBlocked(mounted.blocked)" in tsx and "sb.externalRefs(html)" in mbody,
          "the workbench window names what the policy blocked (the notice)")
    check(sandbox_only_by_its_frame(TSX_SURFACE),
          "the workbench window: the sandbox is set only by its one <iframe>'s attribute — no "
          "allow-same-origin, no setAttribute, no prop spread on the frame (the pins "
          "above read only `srcDoc`, so a widened sandbox would pass them)")

    # The run card's inline version: the window's own document, from the
    # window's own hook, so the two can never seal differently. useVersionDoc
    # hands out its html state, which the pins above hold to null or
    # mountable()'s sealed doc. Its behaviour in the built app: fails closed
    # (test_workbench_version_view Z), mounts sandboxed (test_workbench_panes C).
    card = " ".join(re.sub(r"(?m)^\s*//.*$", "", read(RUN_CARD_SURFACE)).split())
    check(card.count("srcDoc={") == 1 and "srcDoc={doc.html}" in card
          and set(re.findall(r"\bsandbox=(\S+)", card)) == {'"allow-scripts"'},
          "the run card's inline version: one frame sink, sandbox allow-scripts only, fed from doc.html")
    hook = tsx[tsx.find("export function useVersionDoc("):tsx.find("export function VersionView")]
    check(re.search(r'import \{[^}]*\buseVersionDoc\b[^}]*\} from "@/components/workbench/version-view";', card)
          is not None and len(re.findall(r"\bconst doc\b", card)) == 1
          and "const doc = useVersionDoc(" in card and "return { html, err," in hook,
          "the run card's doc is the version window's own (useVersionDoc), whose html is only what "
          "mountable() sealed — never a document of its own")
    check("{doc.html != null && (" in card and "doc.err ? (" in card and "{doc.err}" in card,
          "the run card FAILS CLOSED: no policy, the hook's SANDBOX_MISSING is shown and no frame mounts")
    check("data-blocked-refs" in card and "blockedNotice(doc.blocked)" in card,
          "the run card names what the policy blocked (the notice)")
    check(sandbox_only_by_its_frame(RUN_CARD_SURFACE),
          "the run card: the sandbox is set only by its one <iframe>'s attribute — no "
          "allow-same-origin, no setAttribute, no prop spread on the frame")

    # The chat visuals frame (app-web-kit/visuals), pinned the same way.
    kit = " ".join(re.sub(r"(?m)^\s*//.*$", "", read(KIT_SURFACE)).split())
    sealer = " ".join(re.sub(r"(?m)^\s*//.*$", "", read(KIT_SEALER)).split())
    check(kit.count("srcDoc={") == 1 and "srcDoc={doc}" in kit
          and set(re.findall(r"\bsandbox=(\S+)", kit)) == {'"allow-scripts"'},
          "the chat visuals frame: one frame sink, sandbox allow-scripts only, fed from the doc state")
    dsets = re.findall(r"setDoc\(([^()]*)\)", kit)
    check(bool(dsets) and all(a.strip() == "out ? out.doc : null" for a in dsets)
          and "const out = frameDoc(page, window.CSeriesSandbox, sheet);" in kit,
          f"the chat visuals frame only ever mounts what frameDoc() returned: setDoc({dsets})")
    fbody = sealer[sealer.find("export function frameDoc("):sealer.find("// ── Naming")]
    check("if (!sandbox) return null;" in fbody and "doc: sandbox.withSandboxCsp(" in fbody,
          "the chat visuals frame: frameDoc() seals through the shared withSandboxCsp, and returns "
          "null without it")
    check("if (doc === null) {" in kit and "{SANDBOX_MISSING}" in kit and "did not load" in kit,
          "the chat visuals frame FAILS CLOSED: no policy, nothing mounted, and it says so")
    check("onBlocked?.(out.blocked)" in kit and "blocked: sandbox.externalRefs(html)" in fbody,
          "the chat visuals frame names what the policy blocked (the notice)")
    check(sandbox_only_by_its_frame(KIT_SURFACE),
          "the chat visuals frame: the sandbox is set only by its one <iframe>'s attribute — no "
          "allow-same-origin, no setAttribute, no prop spread on the frame")

    for rel, app in SURFACE_PAGES.items():
        html = read(rel)
        i = html.find("/js/sandbox-csp.js")
        check(i >= 0, f"{rel} loads the shared policy")
        if app:
            check(i < html.rfind(app), f"{rel} loads it BEFORE the code that uses it")
    return runnable


def _payload_lanes():
    """Build the real outbound payload in each mode and read the object back.

    The body of `_postState` and the helper it calls (`barOwned`) are cut out of
    the shipped bridge by the token-aware scan and run under node with the
    shell state they close over stubbed. Nothing is re-implemented, so
    reverting the scoping fails these, and reflowing the source does not.
    Yields (ok, label, detail)."""
    import shutil as _shutil
    import subprocess as _subprocess
    import tempfile as _tempfile

    src = read("dashboard/base/js/desktop-surface.js")
    m = mask(src)
    sp = function_span(src, m, "_postState")
    bo = function_source(src, m, "barOwned")
    if not sp or not bo:
        yield (False, "the payload builder and barOwned are found in the bridge",
               "_postState or barOwned is gone — update this lane with the source")
        return
    body = src[m.find("{", match_close(m, m.find("(", sp[0]))) + 1:sp[1] - 1]
    node = _shutil.which("node")
    if not node:
        yield (True, "payload lanes SKIPPED — node is not on PATH", "structure only")
        return

    harness = """
const OUT = [];
async function run(frameMode, stateMode, declaredBar) {
  const posted = {};
  const frame = { _skMode: frameMode, _skBar: declaredBar, _skBarFailed: false,
                  _skPort: { postMessage: (o) => Object.assign(posted, o) } };
  const state = {
    frame, mode: stateMode, host: 'Sage',
    appList: [{ id: 'circles', name: 'Circles' }, { id: 'network', name: 'Network' }],
  };
  const desktopApps = async () => [{ id: 'circles', name: 'Circles', running: true }];
  const presenceText = () => 'here, and listening';
  const isPhone = () => false;
  const barState = async () => ({ dock: [{ id: 'circles' }], windows: [{ id: 'w1' }],
                                  desktop_shown: false, machine: '1.2G of 4G' });
  const window = { matchMedia: () => ({ matches: false }) };
  __BAROWNED__
  __BODY__
  OUT.push({ frameMode, stateMode, bar: !!declaredBar, posted });
}
(async () => {
  await run('desktop', 'desktop', null);
  await run('wallpaper', 'wallpaper', null);
  await run('wallpaper', 'desktop', null);
  await run('desktop', 'desktop', { edge: 'bottom', size: 52 });
  await run('wallpaper', 'wallpaper', { edge: 'bottom', size: 52 });
  console.log(JSON.stringify(OUT));
})();
""".replace("__BAROWNED__", bo).replace("__BODY__", body)

    with _tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(harness)
        path = fh.name
    try:
        r = _subprocess.run([node, path], capture_output=True, text=True, timeout=30)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if r.returncode != 0:
        yield (False, "the payload builder runs standalone", r.stderr.strip()[:200])
        return
    lanes = {(o["frameMode"], o["stateMode"], o["bar"]): o["posted"] for o in json.loads(r.stdout)}
    desk, wall = lanes[("desktop", "desktop", False)], lanes[("wallpaper", "wallpaper", False)]
    cross = lanes[("wallpaper", "desktop", False)]
    dbar, wbar = lanes[("desktop", "desktop", True)], lanes[("wallpaper", "wallpaper", True)]

    yield (bool(desk["apps"]), "a desktop document IS handed the app set it draws", "")
    yield (len(desk["all"]) == 2, "a desktop document IS handed every installed app", "")
    yield (bool(desk["presence"]), "a desktop document IS handed the presence sentence", "")
    yield (wall["apps"] == [], "a wallpaper is not handed the app set", repr(wall["apps"])[:60])
    yield (wall["all"] == [], "a wallpaper is not handed the installed-app inventory", repr(wall["all"])[:60])
    yield (wall["presence"] == "", "a wallpaper is not handed the presence sentence", repr(wall["presence"])[:60])
    yield (desk["host"] == wall["host"] == "Sage" and wall["phone"] is False
           and wall["reduced_motion"] is False,
           "both modes still get the host name and the viewport flags", "")
    # The crossfade: state.mode is already the INCOMING document's while the old
    # frame is still live with an open port for ~400 ms.
    yield (cross["apps"] == [] and cross["all"] == [] and cross["presence"] == "",
           "during a crossfade the OUTGOING wallpaper still gets nothing",
           f"apps={len(cross['apps'])} all={len(cross['all'])} presence={cross['presence']!r}")
    # The bar's data goes only to a desktop that DECLARED a bar.
    yield (desk["owns_bar"] is False and desk["dock"] == [] and desk["windows"] == [],
           "a desktop with no bar declared is handed no dock or window list", repr(desk.get("dock")))
    yield (dbar["owns_bar"] is True and dbar["dock"] and dbar["windows"] and dbar["machine"],
           "a desktop that declared a bar owns it and is handed its dock, windows and machine line",
           repr({k: dbar.get(k) for k in ("owns_bar", "dock", "windows", "machine")}))
    yield (wbar["owns_bar"] is False and wbar["dock"] == [] and wbar["windows"] == [],
           "a wallpaper is never handed the bar, even with a bar stamped on its frame",
           repr({k: wbar.get(k) for k in ("owns_bar", "dock", "windows")}))


def _leaky(base: str, tag: str, csp_meta: str = "") -> str:
    """A host-authored document that tries every lane, each request tagged."""
    u = f"{base}/{tag}"
    return (
        "<!DOCTYPE html><html><head>" + csp_meta + "</head><body>"
        "<div id='ctl'>script-did-not-run</div>"
        "<style>#x{background:url('" + u + "/css')}</style>"
        "<link rel='prefetch' href='" + u + "/prefetch'>"
        "<img src='" + u + "/imgtag'>"
        "<iframe src='" + u + "/iframesrc'></iframe>"
        "<form id='f' method='POST' action='" + u + "/form'></form>"
        "<script>"
        "document.getElementById('ctl').textContent='SCRIPT RAN';"
        "try{fetch('" + u + "/fetch')}catch(e){}"
        "try{var x=new XMLHttpRequest();x.open('GET','" + u + "/xhr');x.send()}catch(e){}"
        "try{new Image().src='" + u + "/img'}catch(e){}"
        "try{navigator.sendBeacon('" + u + "/beacon','x')}catch(e){}"
        "try{new WebSocket('ws://127.0.0.1:" + str(PORT) + "/" + tag + "/ws')}catch(e){}"
        "try{window.open('" + u + "/open')}catch(e){}"
        "try{document.getElementById('f').submit()}catch(e){}"
        "</script></body></html>"
    )


LANES = ("fetch", "xhr", "img", "imgtag", "beacon", "css", "prefetch", "iframesrc", "open", "form")

# Characters JavaScript's \s (and Python's re \s) match that the HTML parser
# does NOT skip as whitespace before a doctype — HTML's set is only tab, LF,
# FF, CR and space. Each one ahead of <!DOCTYPE> used to move the policy into
# <body>, where it is ignored.
# "vtab" (U+000B) is in JavaScript's \s and in the C/Python "ASCII whitespace"
# set [\t\n\v\f\r ], and it is TEXT to the HTML parser: a regression to that
# set leaks through it alone (measured in Chromium).
# "bom" cannot fail on its own: Chromium drops ONE leading U+FEFF from a
# srcdoc before parsing (measured: the old \s code sealed it);
# it stays so a BOM never starts moving the policy.
LEADS = {"nbsp": " ", "bom": "﻿", "line-sep": " ", "ideographic-space": "　",
         "vtab": "\x0b"}


class _Udp:
    """A loopback UDP port that counts what arrives — a STUN server stand-in."""

    def __init__(self):
        import socket as _socket
        self.sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.count = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        import socket as _socket
        while True:
            try:
                self.sock.recvfrom(2048)
                self.count += 1
            except _socket.timeout:
                pass
            except OSError:
                return


def _rtc_script(port: int) -> str:
    return ("try{var pc=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:" + str(port) + "'}]});"
            "pc.createDataChannel('x');pc.createOffer().then(function(o){return pc.setLocalDescription(o)})}catch(e){}")


def _open_lanes(base: str, csp: str) -> dict:
    """The pinned open lanes: {name: (document, probe)} — each a sealed frame."""
    def meta(c):
        return '<meta http-equiv="Content-Security-Policy" content="' + c + '">'
    u = {k: _Udp() for k in ("rtc", "rtc_block", "rtc_nested")}
    nested = ("var i=document.createElement('iframe');i.srcdoc='<script>'+"
              + json.dumps(_rtc_script(u["rtc_nested"].port)) + "+'<\\/script>';document.body.appendChild(i);")
    nav = "setTimeout(function(){location.href='" + base + "/{tag}/nav?apps=LEAK'},100)"
    return {
        "rtc": ("<!DOCTYPE html>" + meta(csp) + "<script>" + _rtc_script(u["rtc"].port) + "</script>", u["rtc"]),
        "rtc_block": ("<!DOCTYPE html>" + meta(csp + "; webrtc 'block'") + "<script>"
                      + _rtc_script(u["rtc_block"].port) + "</script>", u["rtc_block"]),
        "rtc_nested": ("<!DOCTYPE html>" + meta(csp) + "<body><script>" + nested + "</script></body>", u["rtc_nested"]),
        "navpin": ("<!DOCTYPE html>" + meta(csp) + "<script>" + nav.replace("{tag}", "navpin") + "</script>", None),
        "navto": ("<!DOCTYPE html>" + meta(csp + "; navigate-to 'none'") + "<script>"
                  + nav.replace("{tag}", "navto") + "</script>", None),
    }


def _leaked(tag: str) -> set[str]:
    return {h.split("?")[0].split("/")[-1] for h in hits if h.lstrip("/").startswith(tag + "/")}


def main() -> int:
    global PORT
    # ── 1. the policy's one definition ─────────────────────────────────────
    shared = read("dashboard/base/js/sandbox-csp.js")
    pm = re.search(r'var CSP = (.*?);\n', shared, re.S)
    check(bool(pm), "sandbox-csp.js declares the policy in one place")
    csp = "".join(a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', pm.group(1))) if pm else ""
    for directive in ("default-src 'none'", "connect-src 'none'",
                      "form-action 'none'", "base-uri 'none'"):
        check(directive in csp, f"the shared policy sets {directive}")

    # ── 3/4. every sink, every surface page, the notice (static half) ──────
    runnable = structure()

    # ── 5. the desktop's outbound payload, run ─────────────────────────────
    for ok, label, detail in _payload_lanes():
        check(ok, label + (f"  [{detail}]" if detail and not ok else ""))

    # ── 1/2/4. behaviour, in a real browser ────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"SKIP — playwright unavailable, structure checked only: {e}")
        for line in notes:
            print(line)
        return 2 if not failures else 1

    srv = _ProbeServer(("127.0.0.1", PORT), _H)
    PORT = srv.server_address[1]       # the port taken; _leaky's WebSocket lane reads it
    base = f"http://127.0.0.1:{PORT}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    meta = '<meta http-equiv="Content-Security-Policy" content="' + csp + '">'
    policy_js = shared
    preview_js = read("dashboard/base/js/desktop-preview.js")

    def mount(pg, fid, doc):
        pg.evaluate("""([id, d]) => { const f = document.createElement('iframe');
                 f.id = id; f.setAttribute('sandbox', 'allow-scripts'); f.srcdoc = d;
                 document.body.appendChild(f); }""", [fid, doc])

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        # A REAL http parent. From about:blank the answers differ — measured,
        # and it is how a first probe of this can report "no leak" and be wrong.
        pg.goto(f"{base}/parent-origin", wait_until="domcontentloaded")
        pg.add_script_tag(content=policy_js)
        pg.add_script_tag(content=preview_js)

        # 1. the policy itself, and the control without it
        mount(pg, "policy", _leaky(base, "policy", meta))
        mount(pg, "nopolicy", _leaky(base, "nopolicy"))
        # 1b. A document that OPENS with a character JavaScript's \s matches and
        # HTML does not treat as whitespace. withSandboxCsp
        # found the doctype with /^\s*<!DOCTYPE/, so the policy went in after
        # it — and after that leading text the parser is in <body>, where a
        # CSP <meta> is ignored. Sealed by the shared function, as every
        # surface seals.
        for name, ch in LEADS.items():
            mount(pg, "lead" + name, pg.evaluate("d => window.CSeriesSandbox.withSandboxCsp(d)",
                                                 ch + _leaky(base, "lead" + name)))

        # 6. the lanes nothing here closes (pinned)
        open_lanes = _open_lanes(base, csp)
        for name, (doc, _udp) in open_lanes.items():
            mount(pg, "open-" + name, doc)

        # 2. each surface's own sealing code, with the policy and without it
        outs = {}
        for i, label in enumerate(JS_SURFACES):
            code = runnable.get(label)
            if not code:
                continue
            for mode in ("with", "without", "lead"):
                tag = f"s{i}{mode}"
                doc = (LEADS["nbsp"] if mode == "lead" else "") + _leaky(base, tag)
                try:
                    out = pg.evaluate(
                        """([code, doc, drop]) => {
                             const saved = window.CSeriesSandbox;
                             if (drop) delete window.CSeriesSandbox;
                             try { return String((0, eval)(code)(doc)); }
                             finally { if (drop) window.CSeriesSandbox = saved; } }""",
                        [code, doc, mode == "without"])
                except Exception as e:  # noqa: BLE001
                    check(False, f"{label}: its sealing code runs ({mode} the policy): {e}")
                    continue
                outs[(label, mode)] = (tag, out)
                mount(pg, tag, out)

        # 4. the Artifacts notice, executed
        note = runnable.get("the artifacts window notice")
        notice = None
        if note:
            notice = pg.evaluate(
                """([code, ext, clean]) => {
                     const host = document.createElement('div');
                     host.innerHTML = '<div id="viewer-status"></div>';
                     document.body.appendChild(host);
                     const f = (0, eval)(code);
                     f(ext);
                     const box = document.getElementById('external-refs');
                     const shown = box && !box.hidden ? box.textContent : '';
                     f(clean);
                     const after = document.getElementById('external-refs');
                     return { shown, cleared: !after || after.hidden }; }""",
                [note, "<!DOCTYPE html><script src='https://cdn.example/lib.js'></script>",
                 "<!DOCTYPE html><p>self-contained</p>"])

        pg.wait_for_timeout(3200)

        def ctl(fid):
            try:
                return pg.frame_locator("#" + fid).locator("#ctl").inner_text(timeout=1500)
            except Exception:  # noqa: BLE001
                return None

        check(ctl("policy") == "SCRIPT RAN",
              "POSITIVE CONTROL — the document's script executed under the policy; without "
              "this, 'nothing leaked' cannot be told apart from 'nothing ran'")
        leaked = _leaked("policy")
        for lane in LANES:
            check(lane not in leaked, f"the policy blocks: {lane}")
        without = _leaked("nopolicy")
        check("fetch" in without and "img" in without,
              f"CONTROL — the same frame WITHOUT the policy does leak (got {sorted(without)}); "
              "if this ever passes, the sandbox attribute changed and the whole rationale "
              "needs re-reading")

        for name in LEADS:
            tag = "lead" + name
            check(ctl(tag) == "SCRIPT RAN",
                  f"a document opening with {name} still runs under the policy (positive control)")
            got = _leaked(tag)
            check(not got, f"a document opening with {name} (JS \\s, not HTML whitespace) is still "
                           f"sealed — nothing reaches the probe (got {sorted(got)})")

        for (label, mode), (tag, out) in outs.items():
            got = _leaked(tag)
            if mode == "lead":
                check(ctl(tag) == "SCRIPT RAN",
                      f"{label}: a document opening with a non-breaking space still runs (positive control)")
                check(not got, f"{label}: ...and is still sealed — nothing reaches the probe (got {sorted(got)})")
            elif mode == "with":
                check(ctl(tag) == "SCRIPT RAN",
                      f"{label}: its sealed document still runs (positive control)")
                check("Content-Security-Policy" in out and out.find("Content-Security-Policy") < out.find("<script"),
                      f"{label}: the policy is in the document ahead of any script")
                check(not got, f"{label}: nothing reaches the probe from its sealed frame (got {sorted(got)})")
            else:
                check(ctl(tag) is None and "SCRIPT RAN" not in out,
                      f"{label}: with the policy MISSING, the host document is not mounted (fail closed)")
                check(not got, f"{label}: ...and nothing reaches the probe (got {sorted(got)})")
                check("did not load" in re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", out)),
                      f"{label}: ...and the frame says the policy did not load")

        # 6. KNOWN OPEN, pinned. Each passes while the lane is open; the day
        # Chromium closes one, it fails and says what to change.
        note_src = read("dashboard/base/js/sandbox-csp.js")
        check("SELF-NAVIGATION" in note_src and "WEBRTC" in note_src,
              "sandbox-csp.js documents both lanes it cannot close")
        pin = {n: (u.count if u else None) for n, (_d, u) in open_lanes.items()}
        check(pin["rtc"] > 0,
              f"KNOWN OPEN (pinned): WebRTC STUN leaves a sealed frame ({pin['rtc']} packets). If this "
              "fails, Chromium closed it — update sandbox-csp.js's note and this pin")
        check(pin["rtc_block"] > 0,
              f"KNOWN OPEN (pinned): CSP `webrtc 'block'` is NOT enforced ({pin['rtc_block']} packets). If "
              "this fails, Chromium now enforces it — ADD `webrtc 'block'` to the shared policy")
        check(pin["rtc_nested"] > 0,
              f"KNOWN OPEN (pinned): WebRTC from a nested srcdoc realm ({pin['rtc_nested']} packets) — why a "
              "script that deletes RTCPeerConnection is not a wall")
        navs = {h.split("?")[0] for h in hits}
        check("/navpin/nav" in navs,
              "KNOWN OPEN (pinned): self-navigation carries data out of a sealed frame. If this fails, "
              "Chromium blocks it — update sandbox-csp.js's note and this pin")
        check("/navto/nav" in navs,
              "KNOWN OPEN (pinned): CSP `navigate-to 'none'` is NOT enforced. If this fails, Chromium now "
              "enforces it — ADD it to the shared policy")

        if note:
            check(bool(notice) and "cdn.example/lib.js" in notice["shown"],
                  f"the artifacts window notice names the blocked reference ({(notice or {}).get('shown', '')[:80]!r})")
            check(bool(notice) and notice["cleared"],
                  "the artifacts window notice goes away for a self-contained page")
        b.close()

    srv.shutdown()

    for line in notes:
        print(line)
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"\nok — {len(notes)} checks ({len(JS_SURFACES) + len(TSX_SINKS)} surfaces; the policy and "
          "each surface's own sealing code measured in a real browser)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
