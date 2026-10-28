#!/usr/bin/env python3
# gate-scope: whole-tree — every user-facing text in the tree: web sources, app manifests, skills
"""Gate: the word "entity" never reaches a user, or a Host that may repeat it.

The AI a user owns is a Host, and only a Host. "entity" is the platform's internal name for the same row — it
stays in code identifiers, tables, env vars, API paths, stored addresses and
comments — but it must not appear in anything a person sees or that the Host
reads as prose and may say back to them.

This gate fails if "entity" / "entities" (word boundary, any case) reappears in:

  1. the React apps and the shared kit — circles-web, workbench-web,
     community-web, sites-web (their src/) and app-web-kit/: string literals,
     template-literal text, and JSX text. JSX text is read with a small
     JSX-aware scanner (tags, children, {expressions} nested to any depth), so
     text beside an expression or holding `;` / `=` / `&&` is still text;
  2. the dashboard shell (dashboard/**/*.js string literals, *.html text) and
     system-apps/*/app.js string literals (comments and regex literals are
     skipped);
  3. system-apps/*/index.html and dashboard/**/*.html — visible text and the
     title / placeholder / aria-label / alt attributes;
  4. system-apps/*/app.yaml — every non-comment line;
  5. default-skills/*.md — every line, front matter included;
  6. brain/mastercode/public_tool_registry.py — every "description" and
     "purpose" string of every schema it exports (the Host's tool catalog);
  7. EVERY Python string literal under server/ and brain/mastercode/ (the
     routes, bridge handlers, tool results, memory/research prompts, and the
     prompt builders — prompt_builder.py, model_style.py, agent_rules.py,
     synaptive_handler.py are all in that set). Skipped: docstrings and other
     bare string statements, the arguments of log calls, SQL `--` comment
     lines and verbose-regex `#` comment lines inside a literal, the archived
     tool blocks in runtime/triage.py that never load, and ceigas_invariants.py
     (see _NOT_SCANNED).
  8. "synaptive" too — HOST is the only noun for the AI — in default-skills,
     the public_tool_registry descriptions, mcp.py's descriptions, and the
     prompt builders (prompt_builder.py, model_style.py).
  9. JSX: a literal rendered as a child ({"entity"}) and a literal assigned to
     what the page shows (el.textContent = '...', .title, .placeholder, ...)
     are text even when the whole value is the enum word.

`--self-test` fixtures at the bottom run first on every invocation.

Run: APP_ROOT=<repo> python3 scripts/test_no_entity_in_user_text.py
"""
import ast
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "brain"))

BANNED = re.compile(r"\bentit(?:y|ies)\b", re.I)
# "synaptive" is banned too wherever the text is the Host's own words or its
# tool catalog: skills, public_tool_registry descriptions, mcp.py descriptions,
# and the prompt builders. Module and op names (synaptive_handler,
# synaptive_identity, synaptive_* bridge ops, synaptive_handle_registry) are
# snake_case and cannot match the word boundary.
BANNED_IN_PROMPTS = re.compile(r"\bentit(?:y|ies)\b|\bsynaptive\b", re.I)
_PROMPT_BUILDERS = {"brain/mastercode/runtime/prompt_builder.py",
                    "brain/mastercode/runtime/model_style.py"}

# ── Allow-list ────────────────────────────────────────────────────────────
# Each pattern is removed from a text before BANNED is applied. Every entry is
# an EXACT identifier or stored value — never a broad shape, and never prose.
# Snake_case names (entity_memory, ENTITY_ID, ...) cannot match BANNED's word
# boundary today; they are listed anyway so that widening BANNED later does not
# turn them into failures nobody should "fix".
_ALLOW = [
    # Tool names and embed_jobs targets — the Host calls these by name.
    r"\bentity_(?:memory(?:_reembed)?|context|tasks|tickets)\b",
    # Env var and id fields (JSON keys, route params).
    r"\bENTITY_ID\b",
    r"\b(?:peer|from|to|owner|member|source)_entity_id\b",
    r"\bentity_id\b",
    r"\bentity_display_name\b",
    # The manifest key that scopes an app to certain Hosts (app.yaml).
    r"^\s*target_entities:",
    # A runbook named by its file name, so the Host can open it; its own words
    # for what the runbook does already say "Host pods".
    r"(?<![\w-])entity-roll(?![\w-])",
    # The Host's gitea username / branch prefix, as a TEMPLATE: entity-<id>,
    # entity-{id}, entity-${...}. A literal number (entity-9003) is prose about a
    # Host and is NOT allowed — write "Host #9003".
    r"\bentity-(?:<id>|\{id\}|\$\{[^}]*\})",
    # Mesh / mail / cert addresses and files: <id>@entity.localhost,
    # entity:N@..., /etc/.../entity.crt.
    r"@entity\.localhost\b",
    r"\bentity:\S*@",
    r"\bentity\.crt\b",
    # The one API path a string names: /api/entity (server/routes/entity.py),
    # and that module's logger name.
    r"/api/entity\b",
    r"\bprivatae\.pc\.routes\.entity\b",
    # The CEIGAS domain name and bridge op / field-path names.
    r"\bcore\.entity\b",
    r"\bmemory\.entity\.distill\b",
    r"\bentity\.(?:busy|behavioral_params)\b",
    # The operator CRD field the kernel matches in an operator error.
    r"\bEntity\.spec\.harnesses\b",
    # Wire header and the vendor user-agent string (sent to Anthropic, not
    # shown to anybody).
    r"\bX-Peer-Entity-Id\b",
    r"\(cseries entity\)",
    # The pod's postgres role, as a ps title and in the socket DSN.
    r"\bpostgres: entity\b",
    r"postgresql://entity@",
    # The circles thread-kind enum value, quoted inside a description or an
    # error message, and in the one SQL COALESCE that defaults it.
    r"kind 'entity'",
    r"'self'\|'entity'",
    r"\bself, entity, circle\b",
    r"COALESCE\(\$6::varchar, 'entity'\)",
    # DOM ids looked up by getElementById — markup, not text: the Settings
    # hero (about-entity-name), the taskbar name (entity-name), the dock label
    # (dock-entity).
    r"\babout-entity-name\b",
    r"^entity-name$",
    r"^dock-entity$",
    # Prefix literals that BUILD a stored name (entity-<id>, <n>-0.entity-<id>,
    # the signing key's "Entity-<id>" CN): the whole literal is the prefix.
    r"^[./\-0-9]*entity-$",
    # The key / address prefix literal "entity:" that f-strings build stored
    # keys from (f"entity:{id}@host", f"entity:{id}:memory", f"entity:{ref}").
    r"^entity:$",
    r"^Entity-$",
    # The view literal Network's Message builds a link from ('card:entity:' +
    # id): a desktop deep link Circles parses (circles-web/src/lib/app-view.ts,
    # card:<kind>:<id>), never shown.
    r"^card:entity:$",
    # The "named entities" sense (people / organisations / places extracted
    # from a file) in the bastion's file cards — not the Host.
    r"\bentity counts\b",
    r"\bkeywords(?:,| and) entities\b",
    # ... and in the memory knowledge graph (memory_graph.py,
    # batch_consolidator.py): the Cypher node label, the JSON key the
    # extraction prompt asks for, and the prompt's own words for them.
    r":Entity\b",
    r"\"entities\": \[",
    r"\{\{\"entities\": \[",
    r"\bnamed entities\b",
    r"\bentities/relations\b",
    r"\bthe entities you extracted\b",
    r"\bthe extracted entity names\b",
]
_ALLOW_RE = re.compile("|".join(f"(?:{p})" for p in _ALLOW), re.M)
# A literal whose WHOLE value is the enum word ("entity" thread kind /
# author_kind / audit actor, the `entities` list key, the decisions "entity:"
# ref prefix) is an identifier. This applies to string LITERALS only — never
# to JSX text, where a lone "entities" left after {n} is prose.
_ENUM_EXACT = {"entity", "entities", "entity:"}
# File-scoped exact literals: (file, literal). The placeholder names the code
# must still RECOGNISE on pods initialised before the user-facing noun was Host.
_FILE_EXACT = {
    ("server/bridge/identity_seed.py", "Entity"),
    ("server/host_display.py", r"^(?:entity|host|entity \d+|host #\d+|entity-\d+|—)$"),
}

# Python files that are not read by a user or the Host as prose:
#   ceigas_invariants.py — the architecture spec text, where
#   "entity" is the LEGAL term the claims are written in (the same carve-out as
#   the legal "individuals and entities" sense). It is checked against, never
#   shown or prompted.
_NOT_SCANNED = {"brain/mastercode/ceigas_invariants.py"}

fails: list[str] = []


def _hit(where: str, text: str, enum_ok: bool = True, banned=BANNED) -> None:
    if not text or (enum_ok and text.strip() in _ENUM_EXACT):
        return
    cleaned = _ALLOW_RE.sub(" ", text)
    m = banned.search(cleaned)
    if m:
        a = max(0, m.start() - 50)
        fails.append(f"{where}: …{cleaned[a:m.end() + 50]}…".replace("\n", " "))


# ── JS / TS lexer ─────────────────────────────────────────────────────────
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KW = {"return", "typeof", "case", "do", "else", "in", "of", "void", "yield", "await"}


def lex_js(src: str):
    """Yield (kind, lineno, text) for kind in {"str", "code"}. "str" = string
    literal / template text; "code" = everything outside strings, comments and
    regex literals (used for the JSX-text heuristic)."""
    i, n, line = 0, len(src), 1
    code_buf, code_line = [], 1
    last_sig = ""  # last significant code char/word, for regex detection

    def flush_code():
        nonlocal code_buf
        if code_buf:
            yield ("code", code_line, "".join(code_buf))
            code_buf = []

    stack = []  # template nesting: brace depth per open ${
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "\n":
            line += 1
        if stack and c == "}" and stack[-1] == 0:
            # back into template text
            stack.pop()
            i += 1
            yield from flush_code()
            i, line = yield from _template(src, i, line, stack)
            last_sig = "`"
            continue
        if stack and c == "{":
            stack[-1] += 1
        elif stack and c == "}":
            stack[-1] -= 1
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
            continue
        if c in "'\"":
            start_line = line
            j, buf = i + 1, []
            while j < n and src[j] != c and src[j] != "\n":
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j + 1])
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            yield from flush_code()
            yield ("str", start_line, "".join(buf))
            # An unterminated quote stops at the newline (JSX text with an
            # apostrophe does this); leave the newline to be counted.
            i = j if (j < n and src[j] == "\n") else j + 1
            last_sig = c
            continue
        if c == "`":
            yield from flush_code()
            i, line = yield from _template(src, i + 1, line, stack)
            last_sig = "`"
            continue
        if c == "/" and (last_sig in _REGEX_PREV or last_sig in _REGEX_KW or last_sig == ""):
            j, in_cls = i + 1, False
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_cls = True
                elif src[j] == "]":
                    in_cls = False
                elif src[j] == "/" and not in_cls:
                    break
                j += 1
            i = j + 1
            last_sig = "/re"
            continue
        if not code_buf:
            code_line = line
        code_buf.append(c)
        if not c.isspace():
            if c.isalnum() or c in "_$":
                m = re.match(r"[\w$]+", src[i:])
                word = m.group(0)
                code_buf.extend(word[1:])
                i += len(word)
                last_sig = word
                continue
            last_sig = c
        i += 1
    yield from flush_code()


def _template(src, i, line, stack):
    """Read template text from i (just past ` or }) to the closing ` or ${.
    Yields the text; returns (new_i, new_line)."""
    n, buf, start_line = len(src), [], line
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n:
            buf.append(src[i + 1])
            i += 2
            continue
        if c == "`":
            yield ("str", start_line, "".join(buf))
            return i + 1, line
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            yield ("str", start_line, "".join(buf))
            stack.append(0)
            return i + 2, line
        if c == "\n":
            line += 1
        buf.append(c)
        i += 1
    yield ("str", start_line, "".join(buf))
    return i, line






# ── JSX text ──────────────────────────────────────────────────────────────
# A small JSX-aware scanner over .tsx source. Modes: CODE (strings, template
# literals, comments and regexes are skipped; a `<` opens a tag only where an
# expression can start), TAG (attributes, {expressions} skipped), and
# CHILDREN (everything up to the next `<` or `{` is text — quotes, `;`, `=`
# and `&&` included). `{` in children enters CODE until its matching `}`, so
# JSX nested inside an expression is read too. Each text run is reported as
# text; nothing inside it is treated as an enum.
_TAG_AFTER = set("(,=:?&|[{};!>") | {""}
_TAG_AFTER_WORDS = {"return", "yield", "default", "case", "else", "do", "in", "of", "await"}


def _skip_string(src, i):
    q, n = src[i], len(src)
    i += 1
    while i < n and src[i] != q:
        if src[i] == "\\":
            i += 1
        elif src[i] == "\n" and q != "`":
            break
        elif q == "`" and src[i] == "$" and i + 1 < n and src[i + 1] == "{":
            i = _skip_braces(src, i + 1)
            continue
        i += 1
    return i + 1


def _skip_braces(src, i):
    """i at '{' — return the index after the matching '}' (strings skipped)."""
    depth, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            i = _skip_string(src, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _read_tag(src, i):
    """i at '<'. Return (end_index_after_gt, kind) with kind in
    {"open", "close", "self"}."""
    n = len(src)
    closing = i + 1 < n and src[i + 1] == "/"
    j = i + 1
    while j < n and src[j] != ">":
        if src[j] == "{":
            j = _skip_braces(src, j)
            continue
        if src[j] in "'\"":
            j = _skip_string(src, j)
            continue
        j += 1
    if closing:
        return j + 1, "close"
    return j + 1, ("self" if j > 0 and src[j - 1] == "/" else "open")


_LITERAL_CHILD = re.compile(r"\{\s*([\"'`])((?:(?!\1)[^\\\n]|\\.)*)\1\s*\}")


def jsx_texts(src: str):
    """Yield (lineno, text) for every JSX text run in a .tsx source."""
    n = len(src)
    stack = [["code", 0]]  # CODE frames carry their brace depth
    i, last = 0, ""
    buf, buf_at = [], 0

    def flush():
        nonlocal buf
        if buf:
            t = "".join(buf)
            buf = []
            if t.strip():
                yield (src.count("\n", 0, buf_at) + 1, t)

    while i < n:
        mode = stack[-1][0]
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if mode == "children":
            if c == "<" and (nxt.isalpha() or nxt in "/>"):
                yield from flush()
                end, kind = _read_tag(src, i)
                if kind == "close":
                    stack.pop()
                    last = ">"
                elif kind == "open":
                    stack.append(["children"])
                i = end
                continue
            if c == "{":
                yield from flush()
                # {"text"} / {'text'} / {`text`} is text too — a literal
                # rendered as a child. Read it as text, never as an enum.
                lit = _LITERAL_CHILD.match(src, i)
                if lit:
                    yield (src.count("\n", 0, i) + 1, lit.group(2))
                    i = lit.end()
                    continue
                stack.append(["code", 1])
                i += 1
                last = "{"
                continue
            if not buf:
                buf_at = i
            buf.append(c)
            i += 1
            continue
        # CODE
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"`":
            i = _skip_string(src, i)
            last = "str"
            continue
        if c == "<" and (nxt.isalpha() or nxt == ">") and (last in _TAG_AFTER or last in _TAG_AFTER_WORDS):
            end, kind = _read_tag(src, i)
            if kind == "open":
                stack.append(["children"])
            i = end
            last = ">"
            continue
        if c == "{":
            stack[-1][1] += 1
        elif c == "}":
            stack[-1][1] -= 1
            if stack[-1][1] <= 0 and len(stack) > 1:
                stack.pop()
                i += 1
                continue
        if c.isalnum() or c in "_$":
            m = re.match(r"[\w$]+", src[i:])
            last = m.group(0)
            i += len(last)
            continue
        if c == "=" and nxt == ">":
            last = ">"
            i += 2
            continue
        if not c.isspace():
            last = c
        i += 1
    yield from flush()


_SHOWN_ASSIGN = re.compile(
    r"\.(?:textContent|innerText|innerHTML|title|placeholder|alt)\s*\+?=\s*$")


def scan_js(src: str, rel, jsx: bool) -> None:
    prev_code = ""
    for kind, ln, text in lex_js(src):
        if kind == "str":
            # A literal assigned to what the page shows is text even when its
            # whole value is the enum word: el.textContent = 'entity'.
            shown = bool(_SHOWN_ASSIGN.search(prev_code))
            _hit(f"{rel}:{ln}", text, enum_ok=not shown)
        else:
            prev_code = text
    if jsx:
        for ln, text in jsx_texts(src):
            _hit(f"{rel}:{ln} (jsx text)", text, enum_ok=False)


def check_js(path: Path, jsx: bool) -> None:
    scan_js(path.read_text(), path.relative_to(APP_ROOT), jsx)


# ── HTML visible text ─────────────────────────────────────────────────────
class _Text(HTMLParser):
    _ATTRS = {"title", "placeholder", "aria-label", "alt"}

    def __init__(self, rel):
        super().__init__(convert_charrefs=True)
        self.rel, self.skip = rel, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in self._ATTRS and v:
                _hit(f"{self.rel}:{self.getpos()[0]} [{tag} {k}]", v)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            _hit(f"{self.rel}:{self.getpos()[0]}", data)


def check_html(path: Path) -> None:
    t = _Text(str(path.relative_to(APP_ROOT)))
    t.feed(path.read_text())
    t.close()




def check_html(path: Path) -> None:
    t = _Text(str(path.relative_to(APP_ROOT)))
    t.feed(path.read_text())
    t.close()


# ── Python string literals ────────────────────────────────────────────────
_LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_LOG_NAMES = {"log", "logger", "_log", "LOG", "logging", "_logger", "context"}
# SQL `--` comment lines, and INDENTED `#` lines (verbose-regex comments).
# An unindented "# ..." / "## ..." line is a markdown heading in a prompt and
# is scanned.
_COMMENT_LINE = re.compile(r"^[ \t]*--[^\n]*$|^[ \t]+#[^\n]*$", re.M)


def _skipped(tree: ast.AST, rel: str) -> set[int]:
    skip: set[int] = set()
    for n in ast.walk(tree):
        # docstrings, and any other bare string statement (a stray module
        # docstring mid-file is still a comment)
        if isinstance(n, ast.Expr) and isinstance(n.value, (ast.Constant, ast.JoinedStr)):
            for sub in ast.walk(n.value):
                skip.add(id(sub))
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in _LOG_ATTRS and isinstance(n.func.value, ast.Name)
                and n.func.value.id in _LOG_NAMES):
            for arg in n.args:
                for sub in ast.walk(arg):
                    skip.add(id(sub))
        # runtime/triage.py's archived tool blocks never load
        if (rel == "brain/mastercode/runtime/triage.py" and isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id.startswith("_ARCHIVED") for t in n.targets)):
            for sub in ast.walk(n.value):
                skip.add(id(sub))
    return skip


def check_py(path: Path) -> None:
    rel = str(path.relative_to(APP_ROOT))
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError) as e:
        fails.append(f"{rel}: unparseable ({e})")
        return
    skip = _skipped(tree, rel)
    banned = BANNED_IN_PROMPTS if rel in _PROMPT_BUILDERS else BANNED
    if rel == "server/routes/mcp.py":
        # the MCP tool and prompt catalog an outside chat app shows its user
        for n in ast.walk(tree):
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value == "description":
                        for s in ast.walk(v):
                            if isinstance(s, ast.Constant) and isinstance(s.value, str):
                                _hit(f"{rel}:{s.lineno} [description]", s.value,
                                     banned=BANNED_IN_PROMPTS)
    # A piece of an f-string is never a whole enum value: f"entity {n}" is prose.
    in_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            if (rel, n.value) in _FILE_EXACT:
                continue
            _hit(f"{rel}:{n.lineno}", _COMMENT_LINE.sub(" ", n.value),
                 enum_ok=id(n) not in in_fstring, banned=banned)


# ── scope ─────────────────────────────────────────────────────────────────
_WEB_DIRS = ("circles-web/src", "workbench-web/src", "community-web/src", "sites-web/src",
             "app-web-kit")
_SKIP_PARTS = {"node_modules", "dist", "__pycache__"}


def _files(base: Path, suffixes):
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*")
                  if p.is_file() and p.suffix in suffixes
                  and not _SKIP_PARTS & set(p.relative_to(base).parts))


def main() -> int:
    # 1. React apps + kit
    for d in _WEB_DIRS:
        for p in _files(APP_ROOT / d, {".ts", ".tsx"}):
            check_js(p, jsx=p.suffix == ".tsx")
    # 2 + 3. dashboard shell
    for p in _files(APP_ROOT / "dashboard", {".js"}):
        check_js(p, jsx=False)
    for p in _files(APP_ROOT / "dashboard", {".html"}):
        check_html(p)
    # 2 + 3 + 4. system apps
    for d in sorted((APP_ROOT / "system-apps").iterdir()):
        if (d / "app.js").is_file():
            check_js(d / "app.js", jsx=False)
        if (d / "index.html").is_file():
            check_html(d / "index.html")
        if (d / "app.yaml").is_file():
            for i, line in enumerate((d / "app.yaml").read_text().splitlines(), 1):
                if not line.lstrip().startswith("#"):
                    _hit(f"{(d / 'app.yaml').relative_to(APP_ROOT)}:{i}", line)
    # 5. skills
    for md in sorted((APP_ROOT / "default-skills").glob("*.md")):
        for i, line in enumerate(md.read_text().splitlines(), 1):
            _hit(f"default-skills/{md.name}:{i}", line, banned=BANNED_IN_PROMPTS)
    # 6. tool descriptions
    from mastercode.public_tool_registry import (  # noqa: E402
        PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS)

    def walk(where, node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("description", "purpose") and isinstance(v, str):
                    _hit(where, v, banned=BANNED_IN_PROMPTS)
                else:
                    walk(where, v)
        elif isinstance(node, list):
            for v in node:
                walk(where, v)

    for label, src in (("public", PUBLIC_TOOL_SCHEMAS), ("operator", OPERATOR_PLATFORM_TOOL_SCHEMAS)):
        for key, sc in src.items():
            walk(f"public_tool_registry[{label}] {key}", sc)
    # 7 + 8. every Python literal under server/ and brain/mastercode/
    for base in ("server", "brain/mastercode"):
        for p in _files(APP_ROOT / base, {".py"}):
            if str(p.relative_to(APP_ROOT)) in _NOT_SCANNED:
                continue
            check_py(p)

    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        print(f"\n{len(fails)} place(s) a user or the Host would read 'entity'. The word "
              "is Host: 'host', 'Host #N', 'this host'. If the hit is an identifier or "
              "a stored address, add it to _ALLOW with a comment saying why.")
        return 1
    print("OK: no 'entity' in web-app / dashboard / system-app text, app manifests, "
          "skills, tool descriptions, or any server/ or brain/mastercode/ string "
          "literal (and no 'synaptive' in skills, tool/MCP descriptions or prompt "
          "builders)")
    return 0


# ── self-test ─────────────────────────────────────────────────────────────
# (source, must_report). Every source is scanned as .tsx. The first six cover
# plain JSX text, text beside an {expression}, a lone word left after {n}, and
# prose holding `;`, `=`, `&&`; a matcher that is not JSX-aware passes all six.
_FIXTURES = [
    ("const A = () => <p>{n} entities found</p>;", True),
    ("const B = () => <p>Your entity {name} is ready</p>;", True),
    ("const C = () => <p>This entity is private; nobody else sees it.</p>;", True),
    ("const D = () => <p>One entity = one container</p>;", True),
    ("const E = () => <span>{n} entities</span>;", True),
    ("const F = () => <p>{a && b} entity {c}</p>;", True),
    ("const G = () => <ul>{xs.map((x) => <li key={x}>{x} entity</li>)}</ul>;", True),
    ("const H = () => <p className=\"x\" title={t}>Don't lose the entity</p>;", True),
    ("const t = `Revoke? The entity loses ${x} rights`;", True),
    ("const I = () => <p>{n} hosts found</p>;", False),
    ("const J = (entity) => { return entity.id > 0 && x < 3; };", False),
    ("function f(entity: Entity) { if (a > b) { const entity = 1; } return c < d; }", False),
    ("const s = useState<Entity>(null); const k = kind === 'entity' ? 1 : 2;", False),
    ("const L = () => <Row kind={'entity'} />;", False),
    # A literal rendered as a child, and a literal assigned to
    # what the page shows, are text even when the whole value is the enum word.
    ("const M = () => <p>{\"entity\"}</p>;", True),
    ("const N = () => <p>Hello {'entities'}</p>;", True),
    ("el.textContent = 'entity';", True),
    ("el.title = 'entities';", True),
    ("const O = () => <p>{kind === 'entity' ? a : b}</p>;", False),
    ("row.kind = 'entity';", False),
    ("// the entity container\nconst t = 'host';", False),
    ("const r = /^entity\\s+\\d+$/i;", False),
]


def self_test() -> int:
    bad = 0
    for src, want in _FIXTURES:
        before = len(fails)
        scan_js(src, "fixture", jsx=True)
        got = len(fails) > before
        del fails[before:]
        ok = got == want
        bad += not ok
        print(("  ok    " if ok else "  FAIL  ") + f"{'reports' if want else 'passes '}  {src!r}")
    return 1 if bad else 0


if __name__ == "__main__":
    rc = self_test()
    if rc:
        print("self-test FAILED — the scanner no longer reads the fixtures right")
        sys.exit(rc)
    sys.exit(main())
