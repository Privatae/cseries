#!/usr/bin/env python3
"""Host parity: the Host reads
code as well as Claude Code does — search first, then grep, then read.

Measured before this change:
  - search_my_code returned whole chunks: 12 of 12 results in 14 days went to
    the result store (median 179,964 characters), and the 2,000-character
    preview held 1 or 2 of the 6 to 15 hits;
  - files over 1 MB were never indexed: brain/mastercode/runtime/tools.py
    (1.14 MB) had 0 chunks;
  - the chunker kept only top-level definitions, so module-level tables were
    unsearchable: tool_lanes.py had 2 chunks and LANE_OVERRIDES none;
    chunks ran to 622,267 characters and an oversized unit's
    sub-chunks claimed one line past their end;
  - a search the embedding lane did not answer was keyword-only and said
    nothing;
  - code_grep needed `repo` (two refused calls), and
    code_read / code_grep / code_tree refused the forge names a search hit
    carries;
  - code_tree dropped `path`, so a directory past the controller's 6,000-file
    walk could not be listed;
  - a code_read of a line range the Host asked for was cut to a 2,000-char
    preview like any big result.

  A  search_my_code returns locators; ten hits arrive under STORE_OVER_CHARS;
     a size the caller chose is kept; a keyword-only search says so
  B  the chunker indexes module-level code, holds every chunk to
     MAX_CHUNK_CHARS, and gives line ranges that match the text and never
     overlap (on a stdlib parser standing in for tree-sitter, and on
     tree-sitter itself where it is installed)
  C  a source file over 1 MB is indexed
  D  code_grep / code_read / code_tree default to CODE_DEFAULT_REPO, read a repo
     the controller does not hold from the forge, forward `path`, and fall
     back to the forge for a directory past the controller's walk
  E  a read whose size the Host chose is not stored

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_host_code_reading_parity.py
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import sys
import tempfile
import traceback
import types
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
# The platform repo is deployment config with no default; give the code
# tools one so the no-repo path has somewhere to go.
os.environ.setdefault("CSERIES_PLATFORM_REPO", "owner/repo")
logging.disable(logging.CRITICAL)

FAILS: list[str] = []
PASSED = 0


def check(label, ok, detail=""):
    global PASSED
    print(f"[code-reading] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {str(detail)[:400]}" if detail and not ok else ""))
    if ok:
        PASSED += 1
    else:
        FAILS.append(label)


def part(fn):
    """A part that crashes is a failed part, and the others still run."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        check(f"{fn.__name__} ran", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")


def run(coro):
    return asyncio.run(coro)


# ── A. search_my_code locators ──────────────────────────────────────────────
def part_a_search_locators():
    from codesearch import repo_sync, searcher
    from mastercode.runtime import tools as T
    from mastercode.runtime.result_store import STORE_OVER_CHARS

    body = "".join(f'    x_{i} = compute("{i}", value * {i})  # a line of ordinary code\n'
                   for i in range(80))[:4000]
    hits = [{"project": "owner/repo", "file_path": f"brain/mastercode/runtime/mod{i}.py",
             "chunk_index": i, "content": body, "language": "python", "chunk_kind": "function",
             "symbol": f"handler_{i}", "start_line": 100 * i + 1, "end_line": 100 * i + 60,
             "relevance": 0.71234567, "rrf_score": 0.0321, "lex_rank": 0.5}
            for i in range(10)]
    state = {"semantic": True}

    async def _search(db, *, project, query, limit=10, meta=None):
        if meta is not None:
            meta.update(semantic=state["semantic"], reranked=False)
        return [dict(h) for h in hits[:limit]]

    async def _gaps(db):
        return []
    saved = (searcher.search, repo_sync.not_searchable, T._get_codesearch_db)
    searcher.search, repo_sync.not_searchable, T._get_codesearch_db = _search, _gaps, (lambda: object())
    try:
        r = run(T._exec_search_my_code({"query": "lane overrides", "project": "owner/repo"}))
        res = r.get("result") or {}
        size = len(json.dumps(r, default=str))
        whole = len(json.dumps({"status": "ok", "result": {"results": hits}}, default=str))
        print(f"[code-reading]   search_my_code, 10 hits of 4,000-char chunks: {whole:,} -> {size:,} chars")
        check(f"A1 a default search with 10 hits is under STORE_OVER_CHARS ({STORE_OVER_CHARS:,}): {size:,}",
              r.get("status") == "ok" and res.get("count") == 10 and size <= STORE_OVER_CHARS, size)
        rows = res.get("results") or []
        check("A2 each hit is a locator: repo, file, line range, snippet, truncated — never the whole chunk",
              len(rows) == 10 and all(
                  "content" not in h and h.get("project") == "owner/repo" and h.get("file_path")
                  and h.get("start_line") and h.get("end_line") and h.get("snippet")
                  and h.get("truncated") is True and body.startswith(h["snippet"]) for h in rows),
              rows[:1])
        check("A2 ...and says how to read a hit", "gitea op='read'" in str(res.get("read")), res.get("read"))
        r = run(T._exec_search_my_code({"query": "lane overrides", "max_snippet_chars": 2000}))
        rows = (r.get("result") or {}).get("results") or []
        check("A3 a snippet size the caller chose is kept (2,000), not shrunk to fit",
              rows and all(1000 < len(h["snippet"]) <= 2000 for h in rows),
              [len(h.get("snippet") or "") for h in rows])
        check("A4 a semantic search carries no degraded flag", "degraded" not in (r.get("result") or {}))
        state["semantic"] = False
        r = run(T._exec_search_my_code({"query": "lane overrides"}))
        check("A4 a search the embedding lane did not answer says it matched words only",
              "keyword only" in str((r.get("result") or {}).get("degraded")), r.get("result", {}).get("degraded"))
    finally:
        searcher.search, repo_sync.not_searchable, T._get_codesearch_db = saved

    # The searcher itself fills meta: embed down, rerank refused.
    from codesearch import bridge_embed, store
    saved = (bridge_embed.embed, bridge_embed.rerank, store.search_lexical, store.search_vector,
             bridge_embed.active_space)

    async def _none(*a, **k):
        return None

    async def _lex(db, *, project, query, limit=30):
        return [dict(hits[0], lex_rank=1.0)]

    async def _space():     # a model is set up; its lane is what is down
        return store.Space(model="nomic-embed-text", dim=768), ""
    bridge_embed.embed, bridge_embed.rerank, store.search_lexical = _none, _none, _lex
    bridge_embed.active_space = _space
    try:
        meta: dict = {}
        out = run(searcher.search(None, project=None, query="x", limit=5, meta=meta))
        check("A5 searcher.search fills meta: not semantic, not reranked; its return is still the hit list",
              meta == {"semantic": False, "reranked": False} and isinstance(out, list) and len(out) == 1,
              meta)
    finally:
        (bridge_embed.embed, bridge_embed.rerank, store.search_lexical, store.search_vector,
         bridge_embed.active_space) = saved


# ── B. the chunker ──────────────────────────────────────────────────────────
class _Node:
    """The parts of a tree-sitter node the chunker reads."""

    def __init__(self, type_, start_byte, end_byte, start_point, end_point,
                 children=(), name=None):
        self.type, self.start_byte, self.end_byte = type_, start_byte, end_byte
        self.start_point, self.end_point = start_point, end_point
        self.children = list(children)
        self.named_children = self.children
        self._name = name

    def child_by_field_name(self, field):
        return self._name if field == "name" else None


class _AstParser:
    """Python's own parser standing in for tree-sitter's python grammar: the
    module's top-level statements as nodes of the same types and byte
    spans, so the chunker can be gated where tree-sitter is not installed."""

    _KIND = {ast.FunctionDef: "function_definition", ast.AsyncFunctionDef: "function_definition",
             ast.ClassDef: "class_definition", ast.Import: "import_statement",
             ast.ImportFrom: "import_from_statement"}

    def parse(self, source: bytes):
        starts = [0]
        for i, b in enumerate(source):
            if b == 0x0A:
                starts.append(i + 1)

        def off(line, col):
            return starts[line - 1] + col

        def name_node(stmt):
            line = source[starts[stmt.lineno - 1]:].split(b"\n", 1)[0]
            key = stmt.name.encode()
            col = line.find(b" " + key) + 1
            b = off(stmt.lineno, col)
            return _Node("identifier", b, b + len(key), (stmt.lineno - 1, col),
                         (stmt.lineno - 1, col + len(key)))

        kids = []
        for st in ast.parse(source).body:
            kind = self._KIND.get(type(st), "expression_statement")
            first = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])])
            first_col = 0 if first != st.lineno else st.col_offset
            sb, eb = off(first, first_col), off(st.end_lineno, st.end_col_offset)
            nm = name_node(st) if kind in ("function_definition", "class_definition") else None
            node = _Node(kind, off(st.lineno, st.col_offset), eb, (st.lineno - 1, st.col_offset),
                         (st.end_lineno - 1, st.end_col_offset), name=nm)
            if getattr(st, "decorator_list", None):
                node = _Node("decorated_definition", sb, eb, (first - 1, first_col),
                             (st.end_lineno - 1, st.end_col_offset), children=[node])
            kids.append(node)
        return types.SimpleNamespace(root_node=_Node("module", 0, len(source), (0, 0), (0, 0), kids))


def _covering(chunks, line):
    return [c for c in chunks if c.start_line <= line <= c.end_line]


def _line_of(src: str, needle: str) -> int:
    for i, ln in enumerate(src.split("\n"), 1):
        if needle in ln:
            return i
    raise AssertionError(f"{needle!r} not found")


def _chunker_checks(chunker, tag):
    lanes_src = (ROOT / "brain/mastercode/runtime/tool_lanes.py").read_text()
    store_src = (ROOT / "brain/mastercode/runtime/result_store.py").read_text()
    lanes = chunker.chunk_file("brain/mastercode/runtime/tool_lanes.py", lanes_src)
    store_c = chunker.chunk_file("brain/mastercode/runtime/result_store.py", store_src)
    ln = _line_of(lanes_src, "LANE_OVERRIDES: dict")
    ln2 = _line_of(lanes_src, '"search_my_code": "read"')
    check(f"B1 [{tag}] tool_lanes.py: the LANE_OVERRIDES lines are in a chunk (lines {ln}, {ln2})",
          bool(_covering(lanes, ln)) and any("LANE_OVERRIDES" in c.content for c in _covering(lanes, ln))
          and any('"search_my_code": "read"' in c.content for c in _covering(lanes, ln2)),
          [(c.chunk_kind, c.start_line, c.end_line) for c in lanes])
    ln = _line_of(store_src, "STORE_OVER_CHARS = ")
    check(f"B1 [{tag}] result_store.py: STORE_OVER_CHARS (line {ln}) is in a chunk",
          any("STORE_OVER_CHARS = " in c.content for c in _covering(store_c, ln)),
          [(c.chunk_kind, c.start_line, c.end_line) for c in store_c])
    check(f"B1 [{tag}] module-level code is a 'module' chunk",
          any(c.chunk_kind == "module" for c in lanes), [c.chunk_kind for c in lanes])

    # An oversized function, a huge one-line literal, and a 120-line table.
    big_fn = "def big():\n" + "".join(f"    v{i} = call_something({i}, 'a moderately long argument')\n"
                                      for i in range(400))
    literal = "BUNDLE = '" + "x" * 50_000 + "'\n"
    table = "TABLE = {\n" + "".join(f"    'key_{i}': 'value number {i}',\n" for i in range(120)) + "}\n"
    src = ('"""A module docstring, long enough to stand as a chunk of its own here."""\n'
           "import os\n\n" + big_fn + "\n\n" + literal + "\n" + table
           + "\n\ndef small_after():\n    return os.getcwd()  # long enough to be a chunk\n")
    cs = chunker.chunk_file("probe/big.py", src)
    lines = src.split("\n")
    check(f"B2 [{tag}] no chunk is over MAX_CHUNK_CHARS ({chunker.MAX_CHUNK_CHARS})",
          cs and max(len(c.content) for c in cs) <= chunker.MAX_CHUNK_CHARS,
          max((len(c.content) for c in cs), default=0))
    check(f"B2 [{tag}] a line over MAX_CHUNK_CHARS is cut into windows marked long_line, each on its line",
          any(c.chunk_kind == "long_line" for c in cs)
          and all(c.start_line == c.end_line == _line_of(src, "BUNDLE = ")
                  for c in cs if c.chunk_kind == "long_line"))
    ordered = sorted((c for c in cs if c.chunk_kind != "long_line"), key=lambda c: c.start_line)
    check(f"B3 [{tag}] no two chunks claim the same line (end_line is inclusive)",
          all(b.start_line > a.end_line for a, b in zip(ordered, ordered[1:])),
          [(c.chunk_kind, c.start_line, c.end_line) for c in ordered[:8]])
    check(f"B3 [{tag}] every chunk's text is exactly its lines",
          all(c.content.rstrip("\n") == "\n".join(lines[c.start_line - 1:c.end_line]).rstrip("\n")
              for c in ordered),
          next(((c.start_line, c.end_line, c.content[:60]) for c in ordered
                if c.content.rstrip("\n") != "\n".join(lines[c.start_line - 1:c.end_line]).rstrip("\n")),
               None))
    check(f"B3 [{tag}] an oversized function keeps its kind and name on every sub-chunk",
          {(c.chunk_kind, c.symbol) for c in cs if c.symbol == "big"} == {("function", "big")})
    covered = set()
    for c in cs:
        covered.update(range(c.start_line, c.end_line + 1))
    missing = [i for i, t in enumerate(lines, 1) if t.strip() and i not in covered]
    check(f"B4 [{tag}] every non-blank line of the file is in a chunk", not missing, missing[:10])


def part_b_chunker():
    from codesearch import chunker
    real = chunker._try_tree_sitter
    chunker._try_tree_sitter = lambda lang: _AstParser() if lang == "python" else None
    try:
        _chunker_checks(chunker, "stdlib parser")
    finally:
        chunker._try_tree_sitter = real
    if real("python") is not None:
        _chunker_checks(chunker, "tree-sitter")
    else:
        print("[code-reading]   (tree-sitter not installed here: the image gate runs it)")
    check("B5 a file's hash carries the chunker version, so a Host's index is chunked again once",
          chunker.file_hash("x = 1\n") != hashlib.sha256(b"x = 1\n").hexdigest()
          and str(getattr(chunker, "CHUNKER_VERSION", "")) != "")


# ── C. large source files are indexed ───────────────────────────────────────
def part_c_large_files():
    from codesearch import bridge_embed, chunker, indexer, store
    size = 1_143_332
    check("C1 indexes_path: a 1.14 MB .py file is indexed", indexer.indexes_path("brain/x/tools.py", size))
    check("C1 ...a 1.14 MB .json file is not (data keeps the 1 MB limit)",
          not indexer.indexes_path("data/blob.json", size))
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        body = "".join(f"def handler_{i}(arg):\n    '''Handle case {i} of the dispatcher.'''\n"
                       f"    return dispatch_table[{i}](arg, 'padding text to make the file large')\n\n"
                       for i in range(9000))
        (root / "tools.py").write_text(body)
        check("C2 the probe file is over 1 MB", (root / "tools.py").stat().st_size > 1_000_000,
              (root / "tools.py").stat().st_size)
        # A minified bundle over 1 MB (this repo checks in two, 2.8 and
        # 3.4 MB, renamed on every build) beside a hand-written script of the
        # same size: the bundle keeps the 1 MB limit, the script does not.
        (root / "bundle.js").write_text(
            "".join(f"function f{i}(a){{return a+{i}}};" for i in range(70_000)) + "\n")
        (root / "app.js").write_text(
            "".join(f"function handler{i}(arg) {{\n  return dispatchTable[{i}](arg, 'padding');\n}}\n"
                    for i in range(17_000)))
        check("C2 the minified and hand-written probes are over 1 MB",
              (root / "bundle.js").stat().st_size > 1_000_000 and (root / "app.js").stat().st_size > 1_000_000,
              ((root / "bundle.js").stat().st_size, (root / "app.js").stat().st_size))
        written: dict = {}
        saved = (store.get_file_state, store.delete_file, store.replace_file, bridge_embed.embed_many,
                 chunker._try_tree_sitter, bridge_embed.active_space)

        async def _state(db, project, space):
            return {}

        async def _delete(db, project, file_path):
            return 0

        async def _replace(db, project, file_path, chunks, space, unembedded=False):
            written[file_path] = len(chunks)
            return len(chunks)

        async def _embed(texts, *, space=None):
            return [[0.0] for _ in texts]

        async def _space():
            return store.Space(model="nomic-embed-text", dim=768), ""
        store.get_file_state, store.delete_file, store.replace_file = _state, _delete, _replace
        bridge_embed.embed_many, bridge_embed.active_space = _embed, _space
        chunker._try_tree_sitter = lambda lang: _AstParser() if lang == "python" else None
        try:
            summary = run(indexer.index_project(None, project="owner/probe", project_root=str(root)))
        finally:
            (store.get_file_state, store.delete_file, store.replace_file, bridge_embed.embed_many,
             chunker._try_tree_sitter, bridge_embed.active_space) = saved
        check("C2 index_project chunks and writes a 1.2 MB source file",
              written.get("tools.py", 0) > 1000, f"{written} {summary}")
        check("C2 ...and a hand-written script over 1 MB",
              written.get("app.js", 0) > 0, f"{written} {summary}")
        check("C2 a minified bundle over 1 MB is not indexed (a line longer than a chunk)",
              "bundle.js" not in written, f"{written} {summary}")

        # A repo whose head has not moved is indexed again once after the
        # chunker changes (repo_sync asks chunked_by_this_version).
        class _Db:
            def __init__(self, h):
                self.h = h

            async def fetchrow(self, sql, *a):
                return {"file_path": "tools.py", "file_hash": self.h}
        old_hash = hashlib.sha256(body.encode()).hexdigest()
        probe = getattr(indexer, "chunked_by_this_version", None)
        check("C3 an index chunked by an older chunker is told apart from a current one",
              probe is not None
              and run(probe(_Db(old_hash), project="owner/probe", project_root=root)) is False
              and run(probe(_Db(chunker.file_hash(body)), project="owner/probe", project_root=root)) is True)
        src = (ROOT / "brain/codesearch/repo_sync.py").read_text()
        check("C3 repo_sync re-indexes an unmoved repo whose index is from an older chunker",
              "indexer.chunked_by_this_version(db, project=repo, project_root=dest)" in src)


# ── D. code_grep / code_read / code_tree ────────────────────────────────────
class _Pool:
    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None

    async def fetchval(self, *a, **k):
        return None

    async def execute(self, *a, **k):
        return "OK"


# The controller's checkouts; part D puts the default repo in front.
KNOWN = ["team/core", "team/manifests", "team/voice"]


class _Bridge:
    """The bridge client, answering as the live controller does: a repo it
    has no checkout of is a 404 'unknown repo', and /repo-tree walks the
    whole repo to its file cap whatever path it is given."""
    _connected = True

    def __init__(self):
        self.asked: list = []

    async def request_full(self, op, args):
        self.asked.append((op, dict(args)))
        repo = args.get("repo")
        if repo not in KNOWN:
            verb = op.split(".")[1]
            return {"ok": False, "error": f'{verb} 404: unknown repo {repo!r}; known: {KNOWN}'}
        if op == "code.grep":
            return {"ok": True, "data": {"repo": repo, "pattern": args.get("pattern"),
                                         "matches": [{"path": "a.py", "line_number": 1, "text": "x = 1"}],
                                         "truncated": False}}
        if op == "code.read":
            return {"ok": True, "data": {"repo": repo, "path": args.get("path"), "content": "x = 1\n"}}
        tree = {"name": "", "path": "", "type": "directory", "file_count": 6000, "children": [
            {"name": "aaa", "path": "aaa", "type": "directory", "file_count": 6000, "children": []}]}
        return {"ok": True, "data": {"repo": repo, "tree": tree, "total_files": 6000, "truncated": True}}


def _env(bridge, gitea_calls, gitea_answer):
    from mastercode.runtime import meta_tool_caps as MTC
    from mastercode.runtime import tools as T
    mod = types.ModuleType("server.main")
    mod.get_bridge_client = lambda: bridge
    saved = (sys.modules.get("server.main"), MTC._resolve_entity_scopes, T._exec_gitea)

    async def _scopes(pool, eid, **kw):
        return set(MTC.ENTITY_SCOPE_TO_BITS)

    async def _gitea(args, entity_id=None, pool=None):
        gitea_calls.append(dict(args))
        return gitea_answer(args)
    sys.modules["server.main"] = mod
    MTC._resolve_entity_scopes = _scopes
    T._exec_gitea = _gitea
    return saved


def _restore(saved):
    from mastercode.runtime import meta_tool_caps as MTC
    from mastercode.runtime import tools as T
    if saved[0] is None:
        sys.modules.pop("server.main", None)
    else:
        sys.modules["server.main"] = saved[0]
    MTC._resolve_entity_scopes, T._exec_gitea = saved[1], saved[2]


def _forge(args):
    op, repo = args.get("op"), args.get("repo")
    if repo == "nobody/hidden":
        return {"status": "error", "result": f"{repo!r} was not found on the forge for this host"}
    if op == "grep":
        return {"status": "ok", "result": {
            "repo": repo, "ref": "main", "pattern": args.get("pattern"), "hits": [
                {"path": "voice_gateway/server.py", "line": 12, "text": "def handle_call(req):"}],
            "count": 1, "clipped": False, "files_scanned": 40, "files_skipped": 0, "files_unscanned": 0}}
    if op == "read":
        return {"status": "ok", "result": {"repo": repo, "ref": "main", "path": args.get("path"),
                                           "content": "line 5\n", "total_lines": 90, "lines": [5, 5]}}
    return {"status": "ok", "result": {"repo": repo, "ref": "main", "prefix": args.get("path"),
                                       "entries": [{"path": "voice_gateway/server.py", "type": "blob", "size": 10},
                                                   {"path": "voice_gateway/lib", "type": "tree", "size": 0},
                                                   {"path": "voice_gateway/lib/util.py", "type": "blob", "size": 5}],
                                       "count": 3, "clipped": False, "total_count": 7071}}


def part_d_code_tools():
    from mastercode.runtime import tools as T
    from mastercode.runtime.triage import _BRIDGE_TOOL_SCHEMAS as S
    from mastercode.runtime.triage import CODE_DEFAULT_REPO as DEFAULT
    if DEFAULT not in KNOWN:
        KNOWN.insert(0, DEFAULT)

    for n in ("code_grep", "code_read", "code_tree"):
        req = S[n]["function"]["parameters"].get("required") or []
        desc = S[n]["function"]["parameters"]["properties"]["repo"].get("description") or ""
        check(f"D0 {n}: repo is optional, and its description names the owner/name form",
              "repo" not in req and "owner/name" in desc, (req, desc))

    def call(name, args):
        return run(T._execute_meta_tool(name, args, 3, _Pool()))

    b, calls = _Bridge(), []
    saved = _env(b, calls, _forge)
    try:
        r = call("code_grep", {"pattern": "def lane_for"})
        check("D1 code_grep with no repo searches the default repo",
              r.get("status") == "ok" and b.asked and b.asked[-1][1].get("repo") == DEFAULT
              and not calls, (r, b.asked[-1:]))
        from mastercode.runtime import triage as _tr
        _tr.CODE_DEFAULT_REPO = ""
        try:
            r = call("code_grep", {"pattern": "def lane_for"})
        finally:
            _tr.CODE_DEFAULT_REPO = DEFAULT
        check("D1b code_grep with no repo and no platform repo configured is refused",
              r.get("input_refused") is True and "owner/name" in str(r.get("result")), r)
        r = call("code_grep", {"repo": "team/voice", "pattern": "voice"})
        check("D2 a repo the controller holds (team/voice) is read there, not from the forge",
              r.get("status") == "ok" and not calls and b.asked[-1][1].get("repo") == "team/voice", r)
        r = call("code_grep", {"repo": "owner/voice", "pattern": "handle_call", "path": "voice_gateway"})
        res = r.get("result") or {}
        check("D3 code_grep on a forge name the controller lacks (owner/voice) is served from the forge",
              r.get("status") == "ok" and calls and calls[-1].get("op") == "grep"
              and calls[-1].get("repo") == "owner/voice" and calls[-1].get("path") == "voice_gateway"
              and calls[-1].get("regex") is True
              and [(m.get("path"), m.get("line_number")) for m in res.get("matches") or []]
              == [("voice_gateway/server.py", 12)] and bool(res.get("served_by")),
              (r, calls[-1:]))
        # A forge grep with more hits than one page: the forge reports clipped
        # because the limit asked is one past the page; that is a next page,
        # not a bound. Past the forge's own ceiling it is a bound.
        many = [{"path": f"m{i}.py", "line": 1, "text": "hit"} for i in range(51)]
        saved_gitea = T._exec_gitea

        async def _many(args, entity_id=None, pool=None):
            calls.append(dict(args))
            lim = int(args.get("limit") or 50)
            return {"status": "ok", "result": {"repo": args.get("repo"), "ref": "main",
                                               "hits": (many * 3)[:min(lim, T._GITEA_GREP_HITS)],
                                               "clipped": True, "files_scanned": 40}}
        T._exec_gitea = _many
        try:
            r1 = call("code_grep", {"repo": "owner/voice", "pattern": "hit"})
            r3 = call("code_grep", {"repo": "owner/voice", "pattern": "hit", "offset": 100})
        finally:
            T._exec_gitea = saved_gitea
        p1, p3 = r1.get("result") or {}, r3.get("result") or {}
        check("D3 a forge grep with a next page pages on and is not called bounded",
              p1.get("next_offset") == 50 and "forge_note" not in p1 and not p1.get("clipped"), p1)
        check("D3 ...past the forge's own hit ceiling it says the grep was bounded",
              p3.get("clipped") is True and "forge_note" in p3, {k: p3.get(k) for k in ("clipped", "forge_note")})
        r = call("code_read", {"repo": "owner/controller", "path": "controller/features/repo/sources.py",
                               "start_line": 5, "end_line": 5})
        check("D4 code_read of a repo the controller lacks is served from the forge, with the range asked",
              r.get("status") == "ok" and calls[-1].get("op") == "read"
              and calls[-1].get("start_line") == 5 and calls[-1].get("end_line") == 5
              and (r.get("result") or {}).get("content") == "line 5\n"
              and "served_by" in (r.get("result") or {}), r)
        r = call("code_read", {"repo": "nobody/hidden", "path": "x.py"})
        check("D5 when the forge refuses too, the controller's own error is returned",
              r.get("status") == "error" and "unknown repo" in str(r.get("result")), r)
        n_before = len(calls)
        r = call("code_tree", {"repo": "team/voice", "path": "voice_gateway", "depth": 2})
        res = r.get("result") or {}
        check("D6 code_tree sends path and depth to the controller",
              b.asked[-1][0] == "code.tree" and b.asked[-1][1].get("path") == "voice_gateway"
              and b.asked[-1][1].get("depth") == 2, b.asked[-1:])
        check("D6 a directory past the controller's capped walk is listed from the forge",
              r.get("status") == "ok" and len(calls) == n_before + 1 and calls[-1].get("op") == "tree"
              and calls[-1].get("path") == "voice_gateway"
              and {e.get("path") for e in res.get("entries") or []}
              == {"voice_gateway/server.py", "voice_gateway/lib", "voice_gateway/lib/util.py"}
              and "served_by" in res, (r, calls[-1:]))
    finally:
        _restore(saved)


# ── E. reads the Host sized are not stored ──────────────────────────────────
def part_e_store_exemption():
    from mastercode.runtime import result_store as RS
    cs = getattr(RS, "caller_sized", None)
    check("E1 result_store.caller_sized exists", callable(cs))
    if not callable(cs):
        return
    check("E1 a code_read of a line range, a forge read of a range and a code_grep page are caller-sized",
          cs("code_read", {"repo": "owner/repo", "path": "a.py", "start_line": 1, "end_line": 300})
          and cs("gitea", {"op": "read", "repo": "owner/repo", "path": "a.py", "start_line": 1})
          and cs("code_grep", {"pattern": "x"}))
    check("E1 a whole-file read, a gitea grep and a search are not",
          not cs("code_read", {"repo": "owner/repo", "path": "a.py"})
          and not cs("gitea", {"op": "grep", "repo": "owner/repo", "start_line": 1})
          and not cs("search_my_code", {"query": "x"}))
    src = turn_source(ROOT)
    i = src.find("len(_bounded_result) > _result_store.STORE_OVER_CHARS")
    check("E2 the store's condition in the turn loop skips caller-sized reads",
          i > 0 and "_result_store.caller_sized(_eff_name, _eff_args)" in src[i:i + 300])


for _p in (part_a_search_locators, part_b_chunker, part_c_large_files, part_d_code_tools,
           part_e_store_exemption):
    part(_p)

if FAILS:
    print(f"[code-reading] FAIL {len(FAILS)} of {len(FAILS) + PASSED}: " + "; ".join(FAILS)[:2000])
    sys.exit(1)
print(f"[code-reading] OK {PASSED} checks")
