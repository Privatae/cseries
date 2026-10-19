#!/usr/bin/env python3
"""Gate: every HTML escape helper in the system apps and the shell escapes
both quotes.

Most hand-written apps escape with `esc()`, and many copied the
textContent→innerHTML trick, which escapes & < > but leaves " and ' alone.
Its output went into attributes (data-entry="${esc(name)}", title="…",
value="…"), so any value holding a quote could add attributes, handlers
included, to the element. The Artifacts window's variant tabs did exactly that
with a Host-chosen file name (found in review, 2026-10-01); 127 such sites
were found across the shell's model picker, Keys, Designs, Security, Skills,
Terminal, Network, Context, Publisher and Artifacts.

For every helper named esc / escapeHtml / escHtml / attr / escAttr /
escapeAttr in system-apps/*/ (hand-written JS; bundler output and vendored
code excluded) and dashboard/base/js, this gate:
  - runs it in Node on  a"b'c<d>e&f  and fails if a quote survives (an
    esc/escapeHtml must also escape < and >);
  - fails if it escapes by reading .innerHTML back.
And, because escaping cannot stop a `javascript:` URL, the data-driven
links: the App Store's "Based on" upstream link (the publisher's catalog
metadata) is run in node with javascript:/data: values and must not become
an href; the store's checkout and the Settings changelog link keep their
http(s) guard; the GitHub device-flow address the Gitea app links is passed
on only when it is https.
Needs node; SKIPS (exit 0, said loudly) only when node is absent.

Run: APP_ROOT=<repo> python3 scripts/test_escape_helpers_quote.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])

NAMES = ("esc", "escapeHtml", "escHtml", "attr", "escAttr", "escapeAttr")
TEXT_HELPERS = ("esc", "escapeHtml", "escHtml")
DECL = re.compile(r"function\s+(" + "|".join(NAMES) + r")\s*\(\s*(\w+)\s*\)\s*\{")
ARROW = re.compile(r"(?:const|let|var)\s+(" + "|".join(NAMES) + r")\s*=\s*\(?\s*(\w+)\s*\)?\s*=>")


def files():
    out = []
    for base in ("system-apps", "dashboard/base/js"):
        for p in sorted((_ROOT / base).rglob("*.js")):
            rel = p.relative_to(_ROOT).as_posix()
            if "/assets/" in rel or "/vendor/" in rel or rel.endswith(".min.js"):
                continue
            out.append(p)
    return out


_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^\n")


def scan_to(text, start, stop):
    """Index just past the end of the expression starting at `start`: for
    stop='}' the brace that closes text[start] == '{'; for stop=';' the first
    ; (or newline after a complete expression) at depth 0. Skips strings,
    template literals, regex literals (character classes included) and
    comments, so a '"' inside /[&<>"']/ is not taken for a string."""
    depth, i, n = 0, start, len(text)
    prev = "("
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i, prev = j + 1, "a"
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i = text.find("*/", i + 2)
            i = n if i < 0 else i + 2
            continue
        if c == "/" and prev in _REGEX_AFTER:
            j, in_class = i + 1, False
            while j < n:
                d = text[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "[":
                    in_class = True
                elif d == "]":
                    in_class = False
                elif d == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            i, prev = j, "a"
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if stop == "}" and depth == 0:
                return i + 1
        elif stop == ";" and depth == 0 and c == ";":
            return i
        if not c.isspace():
            prev = c
        i += 1
    return None


def body_from(text, start):
    end = scan_to(text, start, "}")
    return None if end is None else text[start:end]


def arrow_from(text, start):
    end = scan_to(text, start, ";")
    return None if end is None else text[start:end]


helpers = []   # (where, name, js source of a function expression)
for p in files():
    text = p.read_text(errors="replace")
    rel = p.relative_to(_ROOT).as_posix()
    for m in DECL.finditer(text):
        body = body_from(text, m.end() - 1)
        if body is None:
            continue
        line = text.count("\n", 0, m.start()) + 1
        helpers.append((f"{rel}:{line}", m.group(1), f"(function({m.group(2)}){body})"))
    for m in ARROW.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        start = text.index("=", m.start() + len(m.group(0).split("=")[0])) + 1
        src = arrow_from(text, start)
        if src is None:
            continue
        helpers.append((f"{rel}:{line}", m.group(1), f"({src.strip()})"))

_FAILURES = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


check("helpers found (the scan itself works)", len(helpers) >= 20, str(len(helpers)))
for where, name, src in helpers:
    check(f"{where} {name}() does not escape by reading .innerHTML back",
          "innerHTML" not in src)

node = shutil.which("node")
if not node:
    print("SKIPPED the behaviour half: node is not installed here")
else:
    def scope(where, name, src):
        """Var declarations for the other helpers of the same file that this
        one calls (skills' attr() is esc() itself), the nearest one above it."""
        f, line = where.rsplit(":", 1)
        decls = []
        for other in NAMES:
            if other == name or not re.search(r"\b" + other + r"\s*\(", src):
                continue
            cands = [(int(w.rsplit(":", 1)[1]), s) for w, n, s in helpers
                     if n == other and w.rsplit(":", 1)[0] == f and int(w.rsplit(":", 1)[1]) < int(line)]
            if cands:
                decls.append(f"var {other} = {max(cands)[1]};")
        return " ".join(decls)

    prog = ("const out = {};\n"
            + "".join(f"try {{ out[{json.dumps(w)}] = (function () {{ {scope(w, n, s)} "
                      f"return String(({s})('a\"b\\'c<d>e&f')); }})(); }}"
                      f" catch (e) {{ out[{json.dumps(w)}] = 'THREW ' + e; }}\n"
                      for w, n, s in helpers)
            + "process.stdout.write(JSON.stringify(out));")
    r = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        check("the helpers run in node", False, r.stderr[-400:])
    else:
        got = json.loads(r.stdout)
        for where, name, _s in helpers:
            o = got.get(where, "")
            ok = not o.startswith("THREW") and '"' not in o and "'" not in o
            if name in TEXT_HELPERS:
                ok = ok and "<" not in o and ">" not in o
            check(f"{where} {name}() escapes both quotes"
                  + (" and < >" if name in TEXT_HELPERS else ""), ok, repr(o))

# ── URL sinks: escaping cannot stop a `javascript:` URL ──────────────────
# An href (or window.open) runs a `javascript:` address however well it is
# escaped, so a data-driven one is linked only when it is a web address.
print("url sinks")
_store = (_ROOT / "system-apps/app-store/app.js").read_text()
_m = re.search(r"function\s+renderListingRows\s*\(\s*a\s*\)\s*\{", _store)
_listing = body_from(_store, _m.end() - 1) if _m else None
check("app-store: renderListingRows is found", _listing is not None)
if node and _listing is not None:
    _store_helpers = {n: s for w, n, s in helpers
                      if w.startswith("system-apps/app-store/app.js:") and n in ("esc", "attr")}
    _base = re.search(r"var\s+GITEA_BASE\s*=\s*('[^']*'|\"[^\"]*\")\s*;", _store)
    cases = {
        "javascript": "javascript:fetch('/api/artifacts')",
        "cased": "JavaScript:alert(1)",
        "spaced": " javascript:alert(1)",
        "data": "data:text/html,<script>alert(1)</script>",
        "https": "https://example.org/upstream",
    }
    prog = ("var esc = " + _store_helpers.get("esc", "null") + ";\n"
            "var attr = " + _store_helpers.get("attr", "null") + ";\n"
            "var GITEA_BASE = " + (_base.group(1) if _base else "'https://git.example'") + ";\n"
            "function licenceWords() { return ''; }\n"
            "var renderListingRows = function (a) " + _listing + ";\n"
            "var cases = " + json.dumps(cases) + ", out = {};\n"
            "for (var k in cases) out[k] = renderListingRows({ upstream_url: cases[k] });\n"
            "process.stdout.write(JSON.stringify(out));")
    r = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        check("app-store: renderListingRows runs in node", False, r.stderr[-300:])
    else:
        out = json.loads(r.stdout)
        hrefs = {k: re.findall(r'href="([^"]*)"', v) for k, v in out.items()}
        for k in ("javascript", "cased", "spaced", "data"):
            check(f"app-store: a {k} upstream_url never becomes an href (shown as text)",
                  hrefs.get(k) == [] and "Based on" in out.get(k, ""), f"{hrefs.get(k)} {out.get(k, '')[:120]}")
        check("app-store: an https upstream_url is still a link",
              hrefs.get("https") == [cases["https"]], str(hrefs.get("https")))
check("app-store: checkout opens only a web address",
      re.search(r"if \(!/\^https\?:\\/\\//i\.test\(String\(info\.checkout_url\)\)\) \{", _store) is not None)
_settings = (_ROOT / "system-apps/settings/app.js").read_text()
check("settings: the changelog is linked only for a web address",
      "a.changelog_url && /^https?:\\/\\//i.test(String(a.changelog_url))" in _settings)

# GitHub's device-flow address is linked by the Gitea app; the server passes
# on only an https one.
try:
    import asyncio
    sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]
    import server.routes.github_mirror as GM

    def _device(uri):
        async def fake_gh(method, url, **kw):
            return 200, {"device_code": "d", "user_code": "U", "verification_uri": uri,
                         "interval": 5, "expires_in": 900}
        GM._gh, GM._client_id = fake_gh, (lambda: "cid")
        return asyncio.run(GM.device_start())["verification_uri"]

    check("github device flow: a javascript: verification_uri is replaced by GitHub's page",
          _device("javascript:alert(1)") == "https://github.com/login/device")
    check("github device flow: an https verification_uri passes through",
          _device("https://github.com/login/device?x=1") == "https://github.com/login/device?x=1")
except Exception as e:  # noqa: BLE001
    check("github device flow: the route can be exercised", False, repr(e)[:200])

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED")
    sys.exit(1)
print(f"all gates passed ({len(helpers)} helpers)")
