#!/usr/bin/env python3
"""No platform text asks for a word, or teaches the Host to end on a go-ahead
or an offer.

A user never reads 'say go'. The Host's own replies are held to that by the
go-ahead guard (runtime/go_ahead_guard.py).
This holds the PLATFORM's words to it: every text the platform writes that the
Host or the user reads — tool results and refusals, chain-stop reasons, the
briefs a woken turn starts from, delivery rows, inbox and toast lines, gate
cards, the skills, the apps' briefings, the wiki.

Everything is DISCOVERED FROM THE TREE, never a hand list: every Python module
under brain/ and server/, every default-skills/*.md, every string an app's
app.yaml carries, every wiki page — and every TS / TSX / JS / MJS / JSX under
every *-web/src, app-web-kit, dashboard (the desktop shell), sidecars and
system-apps (built assets, tests, node_modules and dist left out). A
completeness walk proves no other such file exists outside those roots unless
it is named, with its reason, in NOT_UI. In Python a text is every string a
literal expression
can produce: a `+` chain is joined, BOTH branches of a conditional are taken,
an f-string's literal parts are kept with its holes as X, so are
%-formatting's and .format()'s, a literal list's sep.join([...]) is joined,
and a literal inside any other call (str(...)) is checked on its own — so no
literal escapes by sitting inside another expression. The TS/JS side reads
the same way through the TypeScript compiler (scripts/platform_texts_ts.cjs),
plus a JSX element's children as one sentence. Left out: docstrings, log
lines, regexes, SQL, module specifiers; and the guard module itself, which
quotes asks on purpose. A coverage check proves every remaining literal was
read, on both sides.

Rules, by who reads the text:
  W  (every text) — never tells anyone to say or type a word to make work go
     on ("say build to continue", "says build to continue", "type go to start
     the next one", "say X to continue" with any word, "say yes and I'll …").
     A quoted anti-pattern after "never"/"not" is a rule, not an ask.
  Q  (every text) — never tells the Host to ASK the user for a go-ahead or a
     pick in its reply ("ask whether to build", "ask what to do"): a choice
     that is the user's goes in the question box (escalate).
  O  (every text) — never tells the Host to OFFER ("offer to plan", "Offer the
     user a re-split", "offer next steps", "a step you offer to do"). The
     principle: do the work when the request covers it, state a fact, or, for
     a genuine choice, use the question box.
  D  (Python texts the USER reads: a delivery row's `content`, a notice's
     subject, body or toast) — the go-ahead guard's own detector. The apps'
     own UI text is held to its CORE level only (a word to say or type, an
     option to reply with, "Want me to …?"): below core it reads buttons and
     placeholders ("Choose a model", "Send redacted") as asks — measured:
     267 such readings in the apps, none core.

An exemption names its file, a substring of the text and WHY; one that no
longer matches any text fails too, so the list cannot rot.
"""
from __future__ import annotations

import ast
import glob
import itertools
import json
import os
import re
import shutil
import subprocess
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
sys.path.insert(0, APP_ROOT)

from mastercode.runtime import go_ahead_guard as G  # noqa: E402

FAILS: list[str] = []


def check(name, cond, extra=""):
    print(("  ok    " if cond else "  FAIL  ") + name)
    if not cond:
        if extra:
            print(f"        {extra}")
        FAILS.append(name)


# Modules that quote asks ON PURPOSE (the rule and its correction).
QUOTES_ASKS = {"brain/mastercode/runtime/go_ahead_guard.py"}

SQL = re.compile(r"^\s*(SELECT|UPDATE|INSERT|WITH|DELETE|CREATE|ALTER|DROP)\b|\bSELECT\b.*\bFROM\b",
                 re.I | re.S)
USER_VARS = {"content", "toast_summary", "_subject"}
USER_KEYS = {"subject", "body"}
LOGGERS = {"log", "logger", "logging", "_log", "LOG", "_logger", "re"}

# W — a word to say or type, or a reply to send.
SAY_WORD = re.compile(
    r"(?i)\b(?:say|says|saying|type|types|typing)\s+[\"“'‘]?"
    r"(?:go|build|continue|ok|okay|proceed|start|done)[\"”'’]?"
    r"(?=\s*(?:[.,;:!?)]|$)|\s+(?:to|and|when|if|once|whenever)\b)"
    r"|\b(?:say|says|type|types)\s+[\"“'‘]?[\w-]+[\"”'’]?\s+to\s+"
    r"(?:continue|start|go\s+on|proceed|build|begin|resume|run)\b"
    r"|\b(?:say|says|type|types)\s+[\"“'‘]?[\w-]+[\"”'’]?\s+and\s+I(?:'ll|\s+will)\b"
    r"|\bsays?\s+the\s+word\b|\breply\s+with\s+(?:a|the)\s+(?:number|word)\b")
# Q — an instruction to ask the user for a go-ahead or a pick.
ASK_GO = re.compile(
    r"(?i)(?<![\w'-])ask\s+(?:(?:the\s+user|them|the\s+person)\s+)?"
    r"(?:whether|what\s+to\s+do|how\s+(?:they|you)\s+want|which\s+(?:one|phase|option|to)"
    r"|if\s+(?:they|you)\s+want|(?:them|the\s+user)\s+to\s+(?:confirm|approve|choose|pick))")
# O — an instruction to offer.
OFFER = re.compile(
    r"(?i)(?:^|[.;:!?\n—–-]\s*|\b(?:and|then|or|just|instead)\s+|,\s*)offer\s+\w+"
    r"|\b(?:you|host|it)\s+(?:should|can|may|could|might)\s+offer\b"
    r"|\b(?:you|the\s+host|your\s+host)\s+(?:(?:then|also|always|just|will|would)\s+)?"
    r"offers?\s+(?:to\s+\w+|the\s+user\b|them\b|it\b)"
    r"|\b(?:propose|suggest)\s+(?:the|a|one)\s+(?:\w+\s+)?next\s+step\b")
# A negator governing the match: within three words before it.
NEG = re.compile(r"(?i)\b(?:not|never|don't|do not|no need to|instead of|rather than|nor)\s+"
                 r"(?:[\w'\"“”‘’-]+\s+){0,3}[\"“'‘]?$")
THIRD_PERSON = re.compile(r"(?i)\b(?:user|they|he|she|person|someone|build|it)\s+$")
POINTS_AT_BOX = re.compile(r"(?i)question box|\bescalate\b")


# "never show it … or ask them to approve it": a negation carried across "or".
OR_TAIL = re.compile(r"(?i)\b(?:or|nor)\s+$")
CLAUSE_NEG = re.compile(r"(?i)\b(?:never|not|don't|do not|no)\b")


def negated(t: str, start: int) -> bool:
    pre = t[max(0, start - 60):start]
    if NEG.search(pre):
        return True
    clause = re.split(r"[.;:!?\n]", t[:start])[-1]
    return bool(OR_TAIL.search(pre) and CLAUSE_NEG.search(clause))


def rule_hits(t: str) -> list[str]:
    why = []
    for m in SAY_WORD.finditer(t):
        if not negated(t, m.start()):
            why.append("W: a word to say or type")
            break
    if not POINTS_AT_BOX.search(t):
        for m in ASK_GO.finditer(t):
            pre = t[max(0, m.start() - 60):m.start()]
            if not negated(t, m.start()) and not THIRD_PERSON.search(pre):
                why.append("Q: tells the Host to ask for a go-ahead outside the question box")
                break
    # An instruction to offer is wrong wherever it sits — naming the question
    # box elsewhere in the same text does not excuse it.
    for m in OFFER.finditer(t):
        if not negated(t, m.start() + 1):
            why.append("O: tells the Host to offer")
            break
    return why


# ── Python: every text a literal expression can produce ─────────────────────
_PCT = re.compile(r"%(?:\([^)]*\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?[diouxXeEfFgGcrsa]")
_BRACE = re.compile(r"\{[^{}]*\}")


def _pct(t: str) -> str:
    """'%s' / '%(name)d' / '%.2f' holes as X; '%%' is a percent sign."""
    return _PCT.sub("X", t.replace("%%", "\x00")).replace("\x00", "%")


def _fmt(t: str) -> str:
    """str.format holes ('{}', '{0}', '{name:>4}') as X; '{{' '}}' are braces."""
    t = t.replace("{{", "\x00").replace("}}", "\x01")
    return _BRACE.sub("X", t).replace("\x00", "{").replace("\x01", "}")


def _method(n, name):
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == name)


def alts(n, cap=32):
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        return [n.value]
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
        a = alts(n.left)
        return None if a is None else [_pct(x) for x in a]
    if _method(n, "format"):
        a = alts(n.func.value)
        return None if a is None else [_fmt(x) for x in a]
    if _method(n, "join") and len(n.args) == 1 and isinstance(n.args[0], (ast.List, ast.Tuple)):
        sep = alts(n.func.value)
        elems = [alts(e) for e in n.args[0].elts]
        if sep is None or all(e is None for e in elems):
            return None
        parts = [""]
        for i, e in enumerate(elems):
            parts = [p + (sep[0] if i else "") + x for p in parts for x in (e or ["X"])][:cap]
        return parts
    if isinstance(n, ast.JoinedStr):
        return ["".join(v.value if isinstance(v, ast.Constant) else "X" for v in n.values)]
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
        a, b = alts(n.left), alts(n.right)
        if a is None and b is None:
            return None
        return [x + y for x, y in itertools.product(a or ["X"], b or ["X"])][:cap]
    if isinstance(n, ast.IfExp):
        a, b = alts(n.body), alts(n.orelse)
        if a is None and b is None:
            return None
        return ((a or ["X"]) + (b or ["X"]))[:cap]
    return None


def folded(n):
    """The nodes whose words alts(n) folded in. A call, a name, a subscript
    is a hole: the literals inside it are checked on their own."""
    out = [n]
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
        out += folded(n.left)
    elif _method(n, "format"):
        out += folded(n.func.value)
    elif _method(n, "join"):
        out += folded(n.func.value)
        for c in n.args[0].elts:
            if alts(c) is not None:
                out += folded(c)
    elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
        for c in (n.left, n.right):
            if alts(c) is not None:
                out += folded(c)
    elif isinstance(n, ast.IfExp):
        for c in (n.body, n.orelse):
            if alts(c) is not None:
                out += folded(c)
    elif isinstance(n, ast.JoinedStr):
        out += [v for v in n.values if isinstance(v, ast.Constant)]
    return out


def py_texts(src: str):
    """[(line, text, the user reads it)], and the literals no text read."""
    tree = ast.parse(src)
    skip, user = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant):
                skip.add(id(b[0].value))
        if isinstance(n, ast.Call):
            f = n.func
            base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
            if base in LOGGERS:
                for s in ast.walk(n):
                    skip.add(id(s))
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            names = {e.id for t in targets for e in (t.elts if isinstance(t, ast.Tuple) else [t])
                     if isinstance(e, ast.Name)}
            if names & USER_VARS:
                for s in ast.walk(n.value):
                    user.add(id(s))
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in USER_KEYS and v is not None:
                    for s in ast.walk(v):
                        user.add(id(s))
    seen, out = set(), []
    for n in ast.walk(tree):
        if id(n) in skip or id(n) in seen:
            continue
        a = alts(n)
        if a is None:
            continue
        for c in folded(n):
            seen.add(id(c))
        for t in a:
            out.append((n.lineno, t, id(n) in user))
    unread = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and id(n) not in seen and id(n) not in skip]
    return out, unread


def md_texts(src: str):
    """Each paragraph and list item of Markdown the Host or the user reads
    (fenced code kept out)."""
    out, buf, fence = [], [], False
    for i, line in enumerate(src.splitlines(), 1):
        if line.strip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if (not line.strip() or line.lstrip().startswith(("- ", "* ", "#"))
                or re.match(r"^\s*\d+\.\s", line)):
            if buf:
                out.append((buf[0][0], " ".join(t for _, t in buf)))
            buf = [(i, line.strip())] if line.strip() else []
            continue
        buf.append((i, line.strip()))
    if buf:
        out.append((buf[0][0], " ".join(t for _, t in buf)))
    return out


def yaml_texts(src: str):
    import yaml
    out = []

    def walk(v, path):
        if isinstance(v, str):
            out.append((path or ".", v))
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for j, x in enumerate(v):
                walk(x, f"{path}[{j}]")
    walk(yaml.safe_load(src), "")
    return out


def sources():
    """Every place the platform writes words the Host or the user reads."""
    for top in ("brain", "server"):
        for dp, dn, fn in os.walk(os.path.join(APP_ROOT, top)):
            dn[:] = [d for d in dn if d not in ("__pycache__", "node_modules")]
            for f in sorted(fn):
                if f.endswith(".py"):
                    yield "py", os.path.relpath(os.path.join(dp, f), APP_ROOT)
    for p in sorted(glob.glob(os.path.join(APP_ROOT, "default-skills", "*.md"))):
        yield "md", os.path.relpath(p, APP_ROOT)
    for p in sorted(glob.glob(os.path.join(APP_ROOT, "docs", "wiki", "pages", "*.md"))):
        yield "md", os.path.relpath(p, APP_ROOT)
    for p in sorted(glob.glob(os.path.join(APP_ROOT, "system-apps", "*", "app.yaml"))):
        yield "yaml", os.path.relpath(p, APP_ROOT)


# ── TypeScript / TSX / JS: the apps' own words ──────────────────────────────
TS_EXT = (".ts", ".tsx", ".js", ".mjs", ".jsx", ".cjs")
TS_SKIP_DIRS = {"node_modules", "dist", "build", "test", "tests", "__tests__", "assets", ".git",
                "__pycache__", ".venv", "venv"}
# The TypeScript compiler comes from the gates' own pinned toolchain
# (scripts/toolchain, installed by scripts/gate_toolchain.py), never from an
# app's node_modules.
sys.path.insert(0, os.path.join(APP_ROOT, "scripts"))
import gate_toolchain  # noqa: E402
_TOOLCHAIN = gate_toolchain.typescript(APP_ROOT)
# Code the platform's words are NOT in, with why. Everything else of these
# kinds outside the roots fails the completeness walk until it is swept or
# named here.
NOT_UI = {
    "scripts/": "build and gate tooling: its words are for the developer running it",
    "NOTES/": "design notes and mockups, not shipped",
}
NOT_UI_FILES = re.compile(r"(?:^|/)vite\.config\.ts$")   # an app's build config


def ts_roots(root=None):
    """Where the apps keep their words, found from the tree: every *-web/src,
    the kit, the desktop shell, the sidecars, the system apps and the npx
    installer's packages."""
    root = root or APP_ROOT
    roots = sorted(os.path.relpath(p, root) for p in glob.glob(os.path.join(root, "*-web", "src")))
    roots += ["app-web-kit", "dashboard", "sidecars", "system-apps", "packages",
              # A cloud machine's unlock page (vm/cloud/README.md, "The vault").
              "vm/cloud/overlay/usr/share/cseries-cloud/unlock"]
    return [r for r in roots if os.path.isdir(os.path.join(root, r))]


def _ts_file(f: str) -> bool:
    return (f.endswith(TS_EXT) and not f.endswith(".d.ts") and ".test." not in f
            and ".spec." not in f and not f.endswith(".min.js"))


def ts_sources():
    for top in ts_roots():
        for dp, dn, fn in os.walk(os.path.join(APP_ROOT, top)):
            dn[:] = sorted(d for d in dn if d not in TS_SKIP_DIRS)
            for f in sorted(fn):
                if _ts_file(f):
                    yield os.path.relpath(os.path.join(dp, f), APP_ROOT)


def ts_outside_roots(root=None):
    """Every TS/JS file in the tree that is in no root and not NOT_UI."""
    root = root or APP_ROOT
    roots = tuple(r.rstrip("/") + "/" for r in ts_roots(root))
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in TS_SKIP_DIRS)
        for f in sorted(fn):
            if not _ts_file(f):
                continue
            rel = os.path.relpath(os.path.join(dp, f), root)
            if rel.startswith(roots) or rel.startswith(tuple(NOT_UI)) or NOT_UI_FILES.search(rel):
                continue
            out.append(rel)
    return out


def ts_texts(files):
    """{path: (items [(line, text)], unread [(line, text)])} through the
    TypeScript compiler, or None when the toolchain is not here (_TOOLCHAIN
    says why)."""
    if _TOOLCHAIN[0] != "run":
        return None
    node = shutil.which("node")
    payload = json.dumps({"ts": _TOOLCHAIN[1], "files": [{"path": p, "source": src} for p, src in files]})
    r = subprocess.run([node, os.path.join(APP_ROOT, "scripts", "platform_texts_ts.cjs")],
                       input=payload, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise SystemExit(f"FAIL: the TS text reader failed: {r.stderr[-800:]}")
    return {e["path"]: (e["items"], e["unread"]) for e in json.loads(r.stdout)}


def ts_why(t: str) -> list[str]:
    """What an app's text breaks: W/Q/O, and the detector at its core level."""
    why = rule_hits(t)
    d = G.find_go_ahead_hit(t)
    if d and d[2] == "core":
        why.append(f"D(core): {d[0]}")
    return why


def ui_text(t: str) -> str:
    """An app's text as it reads: HTML tags in a JS string dropped."""
    return re.sub(r"[ \t]+", " ", re.sub(r"<[^<>]+>", "", t)).strip()


# (file, a substring of the text) -> why it may say what it says.
EXEMPT: dict[tuple[str, str], str] = {
    ("brain/mastercode/constitution/platform_values.py", "offer the legitimate alternative"):
        "The 2026-07-21 value 'No malicious use', verbatim (it rode from an entity-0 row until "
        "v236): naming the legitimate path when the Host declines is the value, not an offer of more work",
    ("brain/mastercode/memory_engine.py", "Say \"continue\" and I'll pick up where I left off."):
        "the old stall notice as once stored, matched EXACTLY "
        "against old rows so host_part reads it as a platform note; never sent or shown",
    ("packages/cseries/src/cli.js", "Type y to go on"):
        "the installer's confirmation before uninstall --delete-data deletes the Host's data for "
        "good: a permanent deletion is the person's own act, so the terminal asks before it",
}

seen_exempt: set = set()
counts = {"py": 0, "md": 0, "yaml": 0, "user": 0, "files": 0, "unread": 0,
          "ts": 0, "ts_files": 0, "ts_unread": 0}
for kind, rel in sources():
    if rel in QUOTES_ASKS:
        continue
    counts["files"] += 1
    src = open(os.path.join(APP_ROOT, rel), encoding="utf-8").read()
    if kind == "py":
        items, unread = py_texts(src)
        counts["unread"] += len(unread)
        for n in unread:
            check(f"{rel}:{n.lineno} a literal no text read", False, repr(n.value[:120]))
        items = [(ln, t, u) for ln, t, u in items if len(t) >= 12 and " " in t and not SQL.search(t)]
    elif kind == "md":
        items = [(ln, t, False) for ln, t in md_texts(src)]
    else:
        items = [(where, t, False) for where, t in yaml_texts(src)]
    for where, t, by_user in items:
        counts[kind] += 1
        counts["user"] += by_user
        why = rule_hits(t)
        if by_user:
            d = G.ask_form(t)
            if d:
                why.append(f"D: {d}")
        if not why:
            continue
        ex = next((k for k in EXEMPT if k[0] == rel and k[1] in t), None)
        if ex is not None:
            seen_exempt.add(ex)
            continue
        check(f"{rel}:{where} asks for nothing and offers nothing", False, f"{'; '.join(why)} — {t[:220]!r}")

_ts_files = list(ts_sources())
_ts = ts_texts([(p, open(os.path.join(APP_ROOT, p), encoding="utf-8").read()) for p in _ts_files])
# In the gate lane node is there (the apps are built with it): there, a
# toolchain that cannot be had FAILS (CSERIES_REQUIRE_NODE, set by
# pre_merge_gates.sh; gate_toolchain) instead of skipping, since a skip would
# read as a pass in its summary.

_outside = ts_outside_roots()
check(f"every TS/JS file in the tree is in a swept root or named in NOT_UI ({len(_outside)} outside)",
      not _outside, _outside[:20])
if _ts is None:
    if _TOOLCHAIN[0] == "fail":
        check("the gates' TypeScript toolchain is here for the apps' TS/JS sweep (required in the "
              "gate lane)", False, _TOOLCHAIN[1])
    else:
        print(f"  SKIP  the apps' TS/JS sweep: {_TOOLCHAIN[1]} — NOT a pass; run it where node is")
else:
    for rel, (items, unread) in _ts.items():
        counts["ts_files"] += 1
        counts["ts_unread"] += len(unread)
        for ln, t in unread:
            check(f"{rel}:{ln} a literal no text read", False, repr(t))
        for ln, t in items:
            t = ui_text(t)
            if len(t) < 12 or " " not in t:
                continue
            counts["ts"] += 1
            why = ts_why(t)
            if not why:
                continue
            ex = next((k for k in EXEMPT if k[0] == rel and k[1] in t), None)
            if ex is not None:
                seen_exempt.add(ex)
                continue
            check(f"{rel}:{ln} asks for nothing and offers nothing", False, f"{'; '.join(why)} — {t[:220]!r}")
    _by_root: dict = {}
    for _p in _ts:
        _r = next((r for r in ts_roots() if _p.startswith(r.rstrip("/") + "/")), "?")
        _by_root[_r] = _by_root.get(_r, 0) + 1
    check(f"the apps' TS/JS were read ({counts['ts_files']} files, {counts['ts']} texts; "
          f"by root: {_by_root})",
          counts["ts_files"] > 230 and counts["ts"] > 2000
          and all(_by_root.get(r) for r in ("dashboard", "system-apps")), counts)
    check("every string literal and JSX text in the apps' TS/JS was read", counts["ts_unread"] == 0,
          counts["ts_unread"])

check(f"the sweep read the tree ({counts['files']} files: {counts['py']} Python texts, "
      f"{counts['md']} Markdown paragraphs, {counts['yaml']} app.yaml strings; "
      f"{counts['user']} the user reads)",
      counts["files"] > 450 and counts["py"] > 8000 and counts["md"] > 2000
      and counts["yaml"] > 400 and counts["user"] > 30, counts)
check("every string literal in brain/ and server/ was read (none hides in a call, a "
      "conditional or an f-string)", counts["unread"] == 0, counts["unread"])
for ex in EXEMPT:
    check(f"exemption still matches a text: {ex}", ex in seen_exempt)


# ── The sweep proves itself on the shapes that used to slip through ────────
def _flagged(src: str) -> list[str]:
    items, unread = py_texts(src)
    return [t for _, t, _ in items if rule_hits(t)] if not unread else ["UNREAD"]


SYNTHETIC = '''
def f(done, word):
    a = ("The phase landed. " + ("Nothing further starts; say build to continue." if done else "It is building."))
    b = "The phase landed. " + ", ".join(["then type go to start the next one"])
    c = f"The phase landed; say {word} to continue."
    return a, b, c
'''
_hits = _flagged(SYNTHETIC)
check("synthetic shapes fail: a word inside a conditional, inside a "
      "call in a + chain, and an f-string's literal parts",
      any("say build to continue" in t for t in _hits)
      and any("type go to start the next one" in t for t in _hits)
      and any("say X to continue" in t for t in _hits), _hits)
# Only the composed sentence asks: every piece alone is too short or innocent,
# so this fails only if a conditional's branches are joined into the text.
COMPOSED = '''
def h(stuck):
    return "The phase is stuck — " + ("offer to " if stuck else "say why, ") + "rebuild it."
'''
check("a conditional's branch is read joined to its sentence (an offer only the joined text shows)",
      any("offer to rebuild it" in t for t in _flagged(COMPOSED)), _flagged(COMPOSED))
check("an offer is caught even when the same text names the question box",
      bool(rule_hits("Surface it and offer to plan; when it is theirs to call, put it in the question box.")))
OFFERS = '''
def g(n, stub):
    note = f"{n} project(s) have no plan yet — surface it and offer to plan."
    resplit = ("Offer the user a re-split: make it wait." if stub else "Built.")
    hint = "nothing left to build. " + str("Offer next steps (publish, a new plan).")
    rule = "'not set up yet' is a setup step you offer to do with the user."
    return note, resplit, hint, rule
'''
_hits = _flagged(OFFERS)
check("offers fail wherever they sit: an f-string, a conditional, a call in a + chain, "
      "'a step you offer to do'", len(_hits) == 4, _hits)
# %-formatting, .format() and a phrase split across join.
FORMATS = '''
def k(word):
    a = "The phase landed; say %s to continue." % word
    b = "The phase landed; say {} to continue.".format(word)
    c = " ".join(["The phase landed; say", "build to continue."])
    d = "The phase landed; %(verb)s %(w)s to continue." % {"verb": "say", "w": word}
    return a, b, c, d
'''
_hits = _flagged(FORMATS)
check("%-formatting, .format() and join are read as their sentence",
      any("say X to continue" in t and "%" not in t for t in _hits)
      and sum("say X to continue" in t for t in _hits) >= 2
      and any("say build to continue" in t for t in _hits), _hits)
check("...and each needed the composition: no piece alone asks",
      not rule_hits("The phase landed; say %s to continue.")
      and not rule_hits("The phase landed; say {} to continue.")
      and not rule_hits("The phase landed; say") and not rule_hits("build to continue."))
check("'%%' and '{{ }}' stay literal", _pct("100%% done, %s left") == "100% done, X left"
      and _fmt("{{x}} is {}") == "{x} is X")
if _TOOLCHAIN[0] == "run":
    TSX = {
        "s.tsx": '''
export function A({ done, word }: { done: boolean; word: string }) {
  const a = "The build is done; " + (done ? "say build to continue." : "it is building.");
  const b = `The plan is ready; say ${word} to start.`;
  const c = ["The draft is saved; type", "go to publish."].join(" ");
  return (<div>
    <p>The phase landed; say {word} to continue.</p>
    <p>The phase landed; say <b>go</b> to resume.</p>
    <p title="Offer the user a re-split of the phases.">x</p>
    <p>{a}{b}{c}</p>
    <p>Elm Lantern is built. Want me to start Tin Robot?</p>
  </div>);
}
''',
        "app.js": '''
function render(el) {
  el.innerHTML = '<p class="note">The phase landed; say <b>go</b> to continue.</p>';
}
'''}
    _ts_syn = ts_texts(list(TSX.items()))
    _syn = {p: [ui_text(t) for _, t in it] for p, (it, _u) in _ts_syn.items()}
    _flag = {p: [t for t in ts_ if ts_why(t)] for p, ts_ in _syn.items()}
    check("TSX: a conditional's branch in a + chain is read as its sentence",
          "The build is done; say build to continue." in _flag["s.tsx"], _flag["s.tsx"])
    check("TSX: a template literal keeps its literal parts with its holes as X",
          "The plan is ready; say X to start." in _flag["s.tsx"], _flag["s.tsx"])
    check("TSX: [...].join(sep) is read as its sentence",
          "The draft is saved; type go to publish." in _flag["s.tsx"], _flag["s.tsx"])
    check("TSX: a JSX sentence split by an expression, and one read through an inline tag",
          "The phase landed; say X to continue." in _flag["s.tsx"]
          and "The phase landed; say go to resume." in _flag["s.tsx"], _flag["s.tsx"])
    check("TSX: an attribute's words are read (an offer in a title)",
          any("Offer the user a re-split" in t for t in _flag["s.tsx"]), _flag["s.tsx"])
    check("TSX: an app's line that is a core ask fails (D at its core level), and nothing else of it",
          any("Want me to start" in t for t in _flag["s.tsx"])
          and ts_why("Elm Lantern is built. Want me to start Tin Robot?") == [
              f"D(core): {G.find_go_ahead_hit('Elm Lantern is built. Want me to start Tin Robot?')[0]}"]
          and not ts_why("Choose a model") and not ts_why("Upload files"), _flag["s.tsx"])
    check("JS: an HTML string in a system app is read with its tags dropped",
          any("say go to continue" in t for t in _flag["app.js"]), _flag["app.js"])
    check("TS/JS: no literal of the synthetic sources went unread",
          all(not u for _it, u in _ts_syn.values()), {p: u for p, (_i, u) in _ts_syn.items()})
elif _TOOLCHAIN[0] == "fail":
    check("the gates' TypeScript toolchain is here for the TS/JS synthetic shapes (required in "
          "the gate lane)", False, _TOOLCHAIN[1])
else:
    print(f"  SKIP  the TS/JS synthetic shapes: {_TOOLCHAIN[1]} — NOT a pass")
# The toolchain helper's answers, on a copy of scripts/toolchain with nothing
# installed: no node, or node but no npm, FAILS in the gate lane and skips
# elsewhere (never installing, never a pass); here, it runs.
import tempfile  # noqa: E402
_tc_root = tempfile.mkdtemp(prefix="toolchain-")
os.makedirs(os.path.join(_tc_root, "scripts", "toolchain"))
for _f in ("package.json", "package-lock.json"):
    shutil.copy(os.path.join(APP_ROOT, "scripts", "toolchain", _f), os.path.join(_tc_root, "scripts", "toolchain", _f))
_node_only = tempfile.mkdtemp(prefix="node-only-")
os.symlink(shutil.which("node") or "/nonexistent/node", os.path.join(_node_only, "node"))
_lane, _off = {"CSERIES_REQUIRE_NODE": "1"}, {}
_v = {
    "no node, lane": gate_toolchain.typescript(_tc_root, {**_lane, "PATH": "/nonexistent"}),
    "no node, off": gate_toolchain.typescript(_tc_root, {**_off, "PATH": "/nonexistent"}),
    "no npm, lane": gate_toolchain.typescript(_tc_root, {**_lane, "PATH": _node_only}),
    "no npm, off": gate_toolchain.typescript(_tc_root, {**_off, "PATH": _node_only}),
}
check("the toolchain helper: no node or no npm FAILS in the gate lane, naming it, and skips elsewhere",
      _v["no node, lane"] == ("fail", "no node on PATH") and _v["no node, off"] == ("skip", "no node on PATH")
      and _v["no npm, lane"][0] == "fail" and "no npm on PATH" in _v["no npm, lane"][1]
      and _v["no npm, off"][0] == "skip"
      and not os.path.exists(os.path.join(_tc_root, "scripts", "toolchain", "node_modules")), _v)
# A failed install, with two gates waiting on the lock: npm runs
# ONCE; the gate that waited fails at once with the same reason, which quotes
# npm's `npm error code` line and never its stack; a gate that starts after
# the failure tries again.
import threading  # noqa: E402
_fake_bin = tempfile.mkdtemp(prefix="fake-npm-")
os.symlink(shutil.which("node") or "/nonexistent/node", os.path.join(_fake_bin, "node"))
_count = os.path.join(_fake_bin, "count")
with open(os.path.join(_fake_bin, "npm"), "w") as _f:
    _f.write("#!/bin/sh\necho x >> \"$FAKE_NPM_COUNT\"\nsleep 1\n"
             "echo 'npm error code ENOTFOUND' >&2\n"
             "echo 'npm error network request to https://registry.npmjs.org/typescript failed, "
             "reason: getaddrinfo ENOTFOUND registry.npmjs.org' >&2\n"
             "echo '    at Object.<anonymous> (/opt/node/lib/stack.js:1:1)' >&2\nexit 1\n")
os.chmod(os.path.join(_fake_bin, "npm"), 0o755)
_fenv = {"CSERIES_REQUIRE_NODE": "1", "PATH": _fake_bin + ":/usr/bin:/bin", "FAKE_NPM_COUNT": _count}
_got: list = []
_ts_ = [threading.Thread(target=lambda: _got.append(gate_toolchain.typescript(_tc_root, _fenv)))
        for _ in range(2)]
for _t in _ts_:
    _t.start()
for _t in _ts_:
    _t.join(60)
_runs = len(open(_count).read().split()) if os.path.exists(_count) else 0
check("a failed install runs npm once; the gate that waited fails at once with the same reason",
      _runs == 1 and len(_got) == 2 and _got[0] == _got[1] and _got[0][0] == "fail", (_runs, _got))
check("...the reason quotes npm's error code line, never its stack",
      len(_got) == 2 and "npm error code ENOTFOUND" in _got[0][1]
      and "getaddrinfo ENOTFOUND registry.npmjs.org" in _got[0][1] and "at Object.<anonymous>" not in _got[0][1],
      _got[:1])
_later = gate_toolchain.typescript(_tc_root, _fenv)
check("...and a gate that starts after the failure tries the install again",
      len(open(_count).read().split()) == 2 and _later[0] == "fail", _later)
check("...and here it runs from scripts/toolchain, pinned to the version the web apps lock",
      _TOOLCHAIN[0] != "run" or (_TOOLCHAIN[1].endswith("scripts/toolchain/node_modules/typescript/lib/typescript.js")
                                 and json.load(open(os.path.join(APP_ROOT, "scripts/toolchain/package-lock.json")))
                                 ["packages"]["node_modules/typescript"]["version"]
                                 == json.load(open(os.path.join(APP_ROOT, "workbench-web/package-lock.json")))
                                 ["packages"]["node_modules/typescript"]["version"]), _TOOLCHAIN)
_bad = [f for f in sorted(glob.glob(os.path.join(APP_ROOT, "scripts", "*.py"))
                         + glob.glob(os.path.join(APP_ROOT, "scripts", "*.cjs")))
        if re.search(r"(?:community|workbench|circles|sites)-web/node_modules/typescript",
                     open(f, encoding="utf-8").read())
        and not f.endswith("test_platform_texts_ask_nothing.py")]
check("no gate reads TypeScript from an app's node_modules any more",
      not _bad, [os.path.relpath(f, APP_ROOT) for f in _bad])
_ign = open(os.path.join(APP_ROOT, ".gitignore")).read()
check("the toolchain's install is ignored by git", "scripts/toolchain/node_modules/" in _ign
      and "scripts/toolchain/.install.lock" in _ign)
# The completeness walk on a made-up tree: a new UI folder outside every root
# is named; a new *-web/src is a root by itself; tooling in NOT_UI is not.
_tdir = tempfile.mkdtemp(prefix="ts-roots-")
for _f in ("newui/src/app.tsx", "shop-web/src/main.tsx", "scripts/build.js", "dashboard/base/js/x.js",
           "shop-web/vite.config.ts", "system-apps/x/assets/index-abc.js", "newui/src/a.test.ts"):
    os.makedirs(os.path.dirname(os.path.join(_tdir, _f)), exist_ok=True)
    open(os.path.join(_tdir, _f), "w").write("export const x = 1;\n")
check("the completeness walk names a UI folder no root covers, and a new *-web/src is a root",
      ts_outside_roots(_tdir) == ["newui/src/app.tsx"] and "shop-web/src" in ts_roots(_tdir),
      (ts_outside_roots(_tdir), ts_roots(_tdir)))
_pmg = open(os.path.join(APP_ROOT, "scripts", "pre_merge_gates.sh"), encoding="utf-8").read()
check("the gate lane runs every gate with CSERIES_REQUIRE_NODE=1, so a missing node fails there",
      # every gate is launched once, under hang_watch (fix-hidden-gate-failures),
      # with the flag on that one launch line
      re.search(r'APP_ROOT="\$REPO" CSERIES_REQUIRE_NODE=1 python3 "\$REPO/scripts/hang_watch\.py"', _pmg) is not None)
check("the roots are found from the tree: every *-web/src, plus the shell and the apps",
      {"circles-web/src", "workbench-web/src", "community-web/src", "sites-web/src",
       "dashboard", "system-apps"} <= set(ts_roots()), ts_roots())
check("W passes a word in a rule against it, and the words an approval surface uses",
      not rule_hits('never "say the word and I\'ll point it there"')
      and not rule_hits("Security is where the user says yes or no to what their host may reach.")
      and not rule_hits("Say exactly that; do not call it done."))
check("Q passes the same choice handed to the question box, a negated instruction, and "
      "a third person who asks",
      not rule_hits("whether to build or drop them is the user's call — put it in the question box")
      and not rule_hits("Do NOT present this as the plan and do NOT ask the user to approve it.")
      and not rule_hits("the build asks which one rather than guessing"))
check("O passes 'do not offer' and an offer named as a thing, and catches 'and offer to'",
      not rule_hits("Do it; do not offer it.") and not rule_hits("leave the offer out")
      and bool(rule_hits("Say so in one line and offer to draft the plan.")))
check("D judges the user's lines with the guard's own detector",
      G.ask_form("Done. Want me to build the next phase too?") is not None)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all gates passed")
