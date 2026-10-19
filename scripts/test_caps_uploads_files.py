#!/usr/bin/env python3
"""Routes: the person's words, uploads,
attachments, embeddings, the file and codespace routes, and context tags.

The rule: a Host handles large data — paged or streamed, always saying
when there is more; nothing cut silently; a limit stays only where it protects
the platform, and says so when hit. Each block below pins one of these:

  - a Circles nudge into a running turn is queued WHOLE (was text[:2000])
  - one upload bound for both lanes (text was 10 MiB under the file lane's
    25 MiB); over it, a 413 before the body is read (was 200 + error)
  - no count cap of the route's own on chat attachments (was 20)
  - one file's size: the same bound the keep uses (was 30 MB vs 25 MB)
  - every character of a long text is embedded (windows combined into
    one vector), never text[:8000] / query[:2000]
  - codespace /tree: max_files is honoured, total_files is the true
    count, truncated is exact, and `path` opens one directory
  - codespace /file: a file over 512 KB reads in pages (was too_large)
  - codespace save: no 1 MB refusal
  - /api/files/read: any size, in pages with offset/length (was 413 >10MB)
  - /api/files/list + /index: the TRUE total and next_offset (total was
    the cut count, 500)
  - context tags: an upload follows the row's rule (50, was 24) and a
    trim is said

Run against the tree:   APP_ROOT=<repo> python3 scripts/test_caps_uploads_files.py
No cluster, ssh, live DB, docker or network: fake pools, fake requests and
temp directories under CSERIES_TEST_SCRATCH (or the repo's parent scratch).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import tempfile

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.basicConfig(level=logging.ERROR)

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[caps-g2] FAIL {name} {str(extra)[:400]}")


def run(coro):
    return asyncio.run(coro)


SCRATCH = os.environ.get("CSERIES_TEST_SCRATCH") or os.environ.get("TMPDIR") or None
_WORK = tempfile.TemporaryDirectory(prefix="caps-g2-", dir=SCRATCH)
WORK = _WORK.name


async def _none():
    return None

from fastapi import HTTPException  # noqa: E402


class FakeHeaders(dict):
    def get(self, k, d=None):
        for kk, v in self.items():
            if kk.lower() == k.lower():
                return v
        return d


class FakeRequest:
    """A Starlette-shaped request: headers, body(), stream(), json()."""

    def __init__(self, body: bytes = b"", headers: dict | None = None, js=None):
        self._body = body
        self.headers = FakeHeaders(headers or {})
        self._js = js
        self.streamed = 0
        self.body_called = False

    async def body(self):
        self.body_called = True
        return self._body

    async def stream(self):
        step = 1 << 16
        for i in range(0, len(self._body), step):
            self.streamed += 1
            yield self._body[i:i + step]

    async def json(self):
        return self._js


# server.main is the whole app (it needs /app/dashboard); the route modules
# import it only for broadcast and the bridge client. A stand-in module, the
# way test_chat_attachments.py and test_go_ahead_guard.py load circles.
import types  # noqa: E402
sys.modules.setdefault("server.main", types.ModuleType("server.main"))
import server.routes.circles as CR  # noqa: E402

# ── the nudge is the person's words, whole ─────────────────────────
print("nudge")
try:
    from mastercode.runtime import session as S
    saved = (CR._entity_id, CR._audit)
    CR._entity_id = lambda: 3

    async def _no_audit(**kw):
        return None
    CR._audit = _no_audit
    long_text = ("word " * 900).strip()          # 4,499 characters
    try:
        res = run(CR.nudge(FakeRequest(js={"session_id": "caps-g2-nudge", "text": long_text})))
    finally:
        CR._entity_id, CR._audit = saved
    queued = S.consume_nudges("caps-g2-nudge")
    check("a 4,499-character nudge is queued whole (queued_chars and the queue)",
          res.get("queued_chars") == len(long_text) and queued == [long_text],
          (res, [len(q) for q in queued]))
except Exception as e:  # noqa: BLE001
    check("nudge route importable and callable", False, repr(e))


# ── uploads ───────────────────────────────────────────────
print("uploads")
import server.routes.context as CTX  # noqa: E402


class FakePool:
    def __init__(self):
        self.inserts = []

    async def fetchval(self, sql, *args):
        if "INSERT INTO user_context" in sql:
            self.inserts.append(args)
            return len(self.inserts)
        return None

    async def execute(self, sql, *args):
        return "UPDATE 0"

    async def fetch(self, sql, *args):
        return []


def _patch_upload_env(pool):
    import server.routes.embed as EMB
    saved = {
        "get_pool": CTX.brain_service.get_pool, "eid": CTX.brain_service._entity_id,
        "sup": CTX._supersede_prior_copies, "enq": EMB.enqueue_job,
    }
    CTX.brain_service.get_pool = lambda: pool
    CTX.brain_service._entity_id = 9003

    async def _sup(*a, **k):
        return 0

    async def _enq(*a, **k):
        return {"id": 77}
    CTX._supersede_prior_copies = _sup
    EMB.enqueue_job = _enq

    def restore():
        CTX.brain_service.get_pool = saved["get_pool"]
        CTX.brain_service._entity_id = saved["eid"]
        CTX._supersede_prior_copies = saved["sup"]
        EMB.enqueue_job = saved["enq"]
    return restore


cap = getattr(CTX, "MAX_FILE_UPLOAD_BYTES", 25 << 20)
# A 12 MiB text file (a bank-statement CSV's size) is taken: it is under the
# file lane's bound, and the text lane no longer has a lower one of its own.
pool = FakePool()
restore = _patch_upload_env(pool)
saved_chunk = CTX._chunk_text
CTX._chunk_text = lambda t: ["the whole statement, read"]
try:
    body = b"a" * (12 << 20)
    req = FakeRequest(body, {"content-length": str(len(body)), "content-type": "text/plain"})
    try:
        res = run(CTX.upload_context(req, x_filename="statement.txt", x_tags=None))
        err = None
    except HTTPException as e:
        res, err = None, e
finally:
    CTX._chunk_text = saved_chunk
    restore()
check("a 12 MiB text upload is taken (the text lane's own 10 MiB is gone)",
      err is None and isinstance(res, dict) and res.get("imported") == 1 and "error" not in res,
      (err, res))
check("the text lane and the file lane share ONE bound",
      getattr(CTX, "MAX_UPLOAD_BYTES", None) == cap, (getattr(CTX, "MAX_UPLOAD_BYTES", None), cap))

# Over the bound: a 413 naming why, BEFORE the body is read.
pool = FakePool()
restore = _patch_upload_env(pool)
try:
    over = cap + 1
    req = FakeRequest(b"x" * over, {"content-length": str(over), "content-type": "application/pdf"})
    try:
        res = run(CTX.upload_context(req, x_filename="scan.pdf", x_tags=None))
        err = None
    except HTTPException as e:
        res, err = None, e
finally:
    restore()
check("an upload over the bound is a 413 (not a 200 with an error inside)",
      err is not None and err.status_code == 413, (err, res))
check("...refused on Content-Length before a byte of the body is read",
      req.streamed == 0 and not req.body_called, (req.streamed, req.body_called))
check("...and the refusal says the size and why the bound exists",
      err is not None and "MB" in str(err.detail) and "memory" in str(err.detail), err and err.detail)

# An upload's collections follow the row's rule (50), and a trim is said.
pool = FakePool()
restore = _patch_upload_env(pool)
CTX._chunk_text = lambda t: ["one chunk"]
try:
    tags30 = ",".join(f"col{i}" for i in range(30))
    req = FakeRequest(b"hello", {"content-length": "5", "content-type": "text/plain"})
    res = run(CTX.upload_context(req, x_filename="n.txt", x_tags=tags30))
    applied = list(pool.inserts[0][5]) if pool.inserts else []
    tags60 = ",".join(f"c{i}" for i in range(60))
    pool2 = FakePool()
    CTX.brain_service.get_pool = lambda: pool2
    req2 = FakeRequest(b"hello", {"content-length": "5", "content-type": "text/plain"})
    res2 = run(CTX.upload_context(req2, x_filename="m.txt", x_tags=tags60))
    applied2 = list(pool2.inserts[0][5]) if pool2.inserts else []
finally:
    CTX._chunk_text = saved_chunk
    restore()
check("an upload applies 30 collections (it stopped at 24, a row takes 50)",
      len(applied) == 30, len(applied))
check("past the row's 50, the answer says how many were left out",
      len(applied2) == 50 and "10 collection" in str((res2 or {}).get("tags_note")),
      (len(applied2), (res2 or {}).get("tags_note")))
noted = getattr(CTX, "_clean_tags_noted", None)
tags, note = noted(["x" * 80]) if noted else (None, None)
check("a collection name over 64 characters is shortened AND said",
      tags == ["x" * 64] and note and "shortened" in note, (tags, note))


# ── chat attachments ──────────────────────────────────────
print("attachments")
checker = getattr(CR, "_check_attachments", None)
small = {"content_block": {"type": "image", "source": {
    "type": "base64", "media_type": "image/png", "data": base64.b64encode(b"\x89PNG").decode()}}}
if checker is None:
    check("the route's attachment check is one helper (_check_attachments)", False, "missing")
else:
    try:
        checker([small] * 25, "rid")
        err = None
    except HTTPException as e:
        err = e
    check("25 attachments are not refused by a count cap of the route's own (was 20)",
          err is None, err and err.detail)
    at_cap = {"content_block": {"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf", "data": "A" * (cap * 4 // 3)}}}
    over_cap = {"content_block": {"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf", "data": "A" * ((cap + 4096) * 4 // 3)}}}
    try:
        checker([at_cap], "rid")
        err_at = None
    except HTTPException as e:
        err_at = e
    try:
        checker([over_cap], "rid")
        err_over = None
    except HTTPException as e:
        err_over = e
    check("a file at the Host's one file bound passes the door", err_at is None, err_at)
    check("past it, a 413 that says why — the SAME bound the keep uses",
          err_over is not None and err_over.status_code == 413
          and "memory" in json.dumps(err_over.detail), err_over and err_over.detail)
src_circles = open(os.path.join(APP_ROOT, "server", "routes", "circles.py")).read()
check("no 30 MB or 20-file literal of the route's own remains",
      "30 * 1024 * 1024" not in src_circles and "len(attachments) > 20" not in src_circles)
# The composer is the other half: it refused a 21st file and anything over
# 30 MB before the route ever saw it. Circles' box is the cards': the bound and the refusal live in
# lib/attachments.ts, the box names what it refuses (cards/composer.tsx).
src_att = open(os.path.join(APP_ROOT, "circles-web", "src", "lib", "attachments.ts")).read()
src_comp = open(os.path.join(APP_ROOT, "circles-web", "src", "cards", "composer.tsx")).read()
check("the composer has no file count of its own and holds one file to the Host's 25 MiB",
      "export const MAX_FILE_BYTES = 25 * 1024 * 1024;" in src_att and "maxFiles" not in src_comp
      and "sortFiles(list)" in src_comp)
check("a file refused for size is refused naming the size",
      "Too large to attach (over ${Math.round(MAX_FILE_BYTES / (1024 * 1024))} MB each)" in src_comp)


# ── embeddings take the whole text ───────────────────────────────
print("embeddings")
try:
    from mastercode import embed_windows as EW
    ok_windows = True
except Exception as e:  # noqa: BLE001
    EW, ok_windows = None, False
    check("mastercode.embed_windows exists", False, repr(e))

import mastercode.memory_engine as ME  # noqa: E402

seen_inputs: list[str] = []


async def _fake_embed(text, model, *, entity_id=None, pool=None, retries=3, byo=None):
    seen_inputs.append(text)
    return [float(len(text)), 1.0, 0.5]


async def _fake_cfg(pool, eid):
    return ("bridge", "test-model")

saved_me = (ME._embed_text_config_driven, ME._entity_embedding_config, ME._byo_embedding_config)
ME._embed_text_config_driven = _fake_embed
ME._entity_embedding_config = _fake_cfg
saved_bs = (CTX.brain_service.get_pool, CTX.brain_service._entity_id)
CTX.brain_service.get_pool = lambda: object()
CTX.brain_service._entity_id = 9003
long_row = "".join(f"sentence {i} of a long row. " for i in range(1200))   # ~30k chars
tail = long_row[-500:]
try:
    seen_inputs.clear()
    lit = run(CTX._generate_embedding(long_row))
    row_inputs = list(seen_inputs)
    seen_inputs.clear()
    short_lit = run(CTX._generate_embedding("a short row"))
    short_inputs = list(seen_inputs)
    seen_inputs.clear()
    long_query = "q " * 3000                                               # 6,000 chars
    run(CTX._generate_query_embedding(long_query))
    query_inputs = list(seen_inputs)
finally:
    CTX.brain_service.get_pool, CTX.brain_service._entity_id = saved_bs
check("a ~30k-character row is embedded in full — its END is sent",
      any(tail in x for x in row_inputs) and "".join(row_inputs) == long_row,
      [len(x) for x in row_inputs])
check("...in windows of at most 8,000 characters, combined into ONE vector",
      lit is not None and all(len(x) <= 8000 for x in row_inputs) and len(row_inputs) >= 4
      and lit.startswith("[") and len(json.loads(lit)) == 3, (lit, [len(x) for x in row_inputs]))
check("a row inside one window is ONE call with the text unchanged (no vector moves)",
      short_inputs == ["a short row"] and short_lit == "[11.0,1.0,0.5]", (short_inputs, short_lit))
check("a 6,000-character query is embedded whole (was cut at 2,000)",
      "".join(query_inputs) == long_query, [len(x) for x in query_inputs])

# The durable job lanes: the bridge batch and the BYO loop.
import server.routes.embed as EMB  # noqa: E402


class FakeBridge:
    def __init__(self):
        self.calls = []

    async def request_full(self, op, params):
        self.calls.append(list(params["inputs"]))
        return {"ok": True, "data": {"vectors": [[float(len(t)), 2.0] for t in params["inputs"]]}}


# The batch reaches the bridge client through server.main; a stand-in module
# keeps the gate from importing the whole app.
import types  # noqa: E402
import server as _server_pkg  # noqa: E402
fb = FakeBridge()
_had_main = sys.modules.get("server.main")
_stub = _had_main or types.ModuleType("server.main")
saved_bc = getattr(_stub, "get_bridge_client", None)
_stub.get_bridge_client = lambda: fb
sys.modules["server.main"] = _stub
_server_pkg.main = _stub
try:
    many = [long_row] * 70 + ["short"]            # > 256 windows in one job batch
    out = run(EMB._embed_batch_via_bridge("test-model", many))
finally:
    if _had_main is None:
        sys.modules.pop("server.main", None)
        try:
            delattr(_server_pkg, "main")
        except AttributeError:
            pass
    elif saved_bc is not None:
        _had_main.get_bridge_client = saved_bc
sent = [t for c in fb.calls for t in c]
check("the bridge batch sends every character of every row (no t[:8000])",
      "".join(sent[:len(EW.windows(long_row))]) == long_row if ok_windows else False,
      [len(c) for c in fb.calls])
check("...in slices within the bridge's own 256-input limit, one vector per row",
      all(len(c) <= 256 for c in fb.calls) and len(fb.calls) >= 2 and len(out) == 71
      and all(o is not None for o in out) and out[-1] == "[5.0,2.0]",
      ([len(c) for c in fb.calls], len(out)))


async def _fake_byo(pool, eid):
    return {"endpoint": "x"}
ME._byo_embedding_config = _fake_byo
try:
    seen_inputs.clear()
    out = run(EMB._embed_batch_config_driven("test-model", [long_row, "tiny"], entity_id=9003, pool=object()))
    byo_inputs = list(seen_inputs)
finally:
    ME._embed_text_config_driven, ME._entity_embedding_config, ME._byo_embedding_config = saved_me
check("the BYO/bastion lane embeds every character too (no [:8000])",
      "".join(byo_inputs[:-1]) == long_row and byo_inputs[-1] == "tiny"
      and all(o is not None for o in out), [len(x) for x in byo_inputs])
if ok_windows:
    v = EW.combine([[1.0, 0.0], [0.0, 1.0]], [3, 1])
    check("windows combine weighted by their length", v is not None and v[0] > v[1] > 0, v)
    check("one window that fails fails the whole text (no vector over part of it)",
          run(EW.embed_whole("a" * 20000, lambda t: _none())) is None)


# ── codespace ─────────────────────────────────────
print("codespace")
import server.routes.codespace as CS  # noqa: E402
from pathlib import Path  # noqa: E402

cs_ws = Path(WORK) / "cs-workspace"
(cs_ws / "sub").mkdir(parents=True)
for i in range(20):
    (cs_ws / f"f{i:02d}.py").write_text("x = 1\n")
for i in range(10):
    (cs_ws / "sub" / f"g{i:02d}.py").write_text("y = 2\n")
saved_cs = (CS.WORKSPACE, CS.MAX_FILES)
CS.WORKSPACE = cs_ws
CS.MAX_FILES = 5          # stands in for the 5,000 default on a large repo
try:
    import inspect
    _kw = {"path": ""} if "path" in inspect.signature(CS.workspace_tree).parameters else {}
    t_big = run(CS.workspace_tree(project="", max_files=40, **_kw))
    t_small = run(CS.workspace_tree(project="", max_files=10, **_kw))
    try:
        t_sub = run(CS.workspace_tree(project="", max_files=40, path="sub"))
    except TypeError:
        t_sub = {}
finally:
    CS.MAX_FILES = saved_cs[1]
check("max_files is honoured past the default (30 files, asked 40: all listed, not truncated)",
      t_big.get("total_files") == 30 and t_big.get("listed_files") == 30 and t_big.get("truncated") is False,
      {k: t_big.get(k) for k in ("total_files", "listed_files", "truncated")})
check("asked for 10 of 30: 10 listed, total_files 30, truncated true",
      t_small.get("total_files") == 30 and t_small.get("listed_files") == 10 and t_small.get("truncated") is True,
      {k: t_small.get(k) for k in ("total_files", "listed_files", "truncated")})
check("`path` opens one directory (lazy loading)",
      t_sub.get("total_files") == 10 and (t_sub.get("tree") or {}).get("path") == "sub",
      {k: t_sub.get(k) for k in ("total_files", "listed_files")})

# ── the walk never follows a link, runs off the event loop, pages ──
# `a -> .` made the walk recurse through the link; two
# such links made it exponential (still running when killed at 60 s), and it
# ran synchronously on the event loop inside the async route.
import threading  # noqa: E402
import time as _time  # noqa: E402
loop_ws = Path(WORK) / "cs-loop"
(loop_ws / "src").mkdir(parents=True)
(loop_ws / "src" / "m.py").write_text("x = 1\n")
(loop_ws / "README.md").write_text("hi\n")
os.symlink(".", loop_ws / "a")
os.symlink(".", loop_ws / "b")
os.symlink("..", loop_ws / "src" / "up")
_loop_out: dict = {}


def _walk_loop():
    try:
        CS.WORKSPACE = loop_ws
        _loop_out["r"] = asyncio.run(CS.workspace_tree(project="", max_files=50, path=""))
    except Exception as e:  # noqa: BLE001
        _loop_out["err"] = repr(e)


_saved_ws = CS.WORKSPACE
_t0 = _time.time()
_th = threading.Thread(target=_walk_loop, daemon=True)
_th.start()
_th.join(10)
_took = _time.time() - _t0
CS.WORKSPACE = _saved_ws
lr = _loop_out.get("r") or {}


def _nodes(n):
    yield n
    for c in n.get("children") or []:
        yield from _nodes(c)


_all = list(_nodes(lr.get("tree") or {}))
_links = sorted(c["path"] for c in _all if c.get("type") == "link")
check("a tree with `a -> .` and `b -> .` is walked in under 10 s (it never finished)",
      not _th.is_alive() and "r" in _loop_out, f"alive={_th.is_alive()} took={_took:.1f}s {_loop_out.get('err')}")
check("each link is listed once as a link and never descended",
      _links == ["a", "b", "src/up"] and lr.get("total_files") == 2
      and lr.get("total_links") == 3, (_links, lr.get("total_files"), lr.get("total_links")))

_off_loop = []
_real_to_thread = asyncio.to_thread


async def _spy_to_thread(fn, *a, **kw):
    _off_loop.append(getattr(fn, "__name__", str(fn)))
    return await _real_to_thread(fn, *a, **kw)


asyncio.to_thread = _spy_to_thread
CS.WORKSPACE = cs_ws
try:
    run(CS.workspace_tree(project="", max_files=10, path=""))
finally:
    asyncio.to_thread = _real_to_thread
check("the walk runs on a worker thread, not the event loop",
      bool(_off_loop), _off_loop)

_pages, _off, _guard = [], 0, 0
while _off is not None and _guard < 10:
    try:
        pr = run(CS.workspace_tree(project="", max_files=12, path="", offset=_off))
    except TypeError as e:
        pr = {"error": repr(e)}
    _pages.append(pr)
    _off = pr.get("next_offset") if "next_offset" in pr else None
    _guard += 1
_paged = [c["path"] for p_ in _pages for c in _nodes(p_.get("tree") or {}) if c.get("type") == "file"]
check("offset + next_offset page through every file exactly once (30 files, pages of 12)",
      len(_paged) == 30 and len(set(_paged)) == 30 and len(_pages) == 3
      and _pages[-1].get("next_offset") is None and _pages[0].get("total_files") == 30,
      (len(_paged), len(_pages), [p_.get("next_offset") for p_ in _pages]))
_saved_page = getattr(CS, "_PAGE_MAX", None)
if _saved_page is not None:
    CS._PAGE_MAX = 7
try:
    cl = run(CS.workspace_tree(project="", max_files=1_000_000, path=""))
finally:
    if _saved_page is not None:
        CS._PAGE_MAX = _saved_page
check("one answer is a bounded page, said plainly (max_files_clamped, next_offset)",
      _saved_page is not None and cl.get("listed_files") == 7 and cl.get("max_files_clamped") is True
      and cl.get("next_offset") == 7 and cl.get("total_files") == 30,
      {k: cl.get(k) for k in ("listed_files", "max_files_clamped", "next_offset", "total_files")})
_saved_budget = getattr(CS, "_VISIT_BUDGET", None)
if _saved_budget is not None:
    CS._VISIT_BUDGET = 5
try:
    fl = run(CS.workspace_tree(project="", max_files=100, path=""))
finally:
    if _saved_budget is not None:
        CS._VISIT_BUDGET = _saved_budget
    CS.WORKSPACE = _saved_ws
check("a count stopped by the visit budget says it is a floor",
      _saved_budget is not None and fl.get("total_is_floor") is True and "at least" in (fl.get("note") or ""),
      {k: fl.get(k) for k in ("total_is_floor", "total_files", "note")})

big = "".join(f"line {i:06d} — naïve café ✓\n" for i in range(25000))   # ~800 KB, multibyte
(cs_ws / "package-lock.json").write_text(big, encoding="utf-8")
try:
    first = run(CS.workspace_file(path="package-lock.json", project="", offset=0, length=None))
    pieces, r, guard = [], first, 0
    while r.get("ok") and guard < 50:
        pieces.append(r.get("content") or "")
        if not r.get("next_offset"):
            break
        r = run(CS.workspace_file(path="package-lock.json", project="", offset=r["next_offset"], length=None))
        guard += 1
except TypeError as e:
    first, pieces = {"ok": False, "error": repr(e)}, []
check("a ~800 KB file opens (first page, truncated, next_offset) instead of too_large",
      first.get("ok") is True and first.get("truncated") is True and first.get("next_offset")
      and first.get("size") == len(big.encode("utf-8")), {k: first.get(k) for k in ("ok", "error", "truncated", "next_offset", "size")})
check("paging by next_offset returns the whole file, every character intact",
      "".join(pieces) == big, (len("".join(pieces)), len(big)))

try:
    w = run(CS.write_workspace_file(CS.WriteFileReq(path="big.json", content="z" * (2 << 20))))
finally:
    CS.WORKSPACE = saved_cs[0]
check("a 2 MB save is written (the 1 MB refusal is gone)",
      w.get("ok") is True and (cs_ws / "big.json").stat().st_size == 2 << 20, w)


# ── /api/files ──────────────────────────────────────────
print("files")
import server.routes.files as FR  # noqa: E402

fs_ws = Path(WORK) / "fs-workspace"
(fs_ws / "many").mkdir(parents=True)
for i in range(600):
    (fs_ws / "many" / f"e{i:04d}.txt").write_text("e")
blob = os.urandom(11 << 20)
(fs_ws / "big.bin").write_bytes(blob)
text_mb = "".join(f"row {i} — ünïcödé ✓\n" for i in range(200))
(fs_ws / "u.txt").write_text(text_mb, encoding="utf-8")
saved_fs = (FR.WORKSPACE, FR.USER_FILES)
FR.WORKSPACE = fs_ws
FR.USER_FILES = fs_ws / "no-user-files"
try:
    try:
        r1 = run(FR.read_file(path="big.bin", encoding="binary"))
        err = None
    except HTTPException as e:
        r1, err = {}, e
    got = b""
    r = r1
    guard = 0
    while r and guard < 10:
        got += base64.b64decode(r.get("content") or "")
        if not r.get("next_offset"):
            break
        r = run(FR.read_file(path="big.bin", encoding="binary", offset=r["next_offset"]))
        guard += 1
    check("an 11 MiB file is read (a page + next_offset), not refused with a 413",
          err is None and r1.get("truncated") is True and r1.get("size") == len(blob), (err, {k: r1.get(k) for k in ("truncated", "size", "next_offset")}))
    check("paging returns every byte", got == blob, (len(got), len(blob)))
    # utf-8 pages of 7 bytes land mid-character and must still join exactly
    pieces, guard = [], 0
    try:
        r = run(FR.read_file(path="u.txt", offset=0, length=7))
        while guard < 5000:
            pieces.append(r.get("content") or "")
            if not r.get("next_offset"):
                break
            r = run(FR.read_file(path="u.txt", offset=r["next_offset"], length=7))
            guard += 1
    except TypeError:
        pieces = []
    check("small utf-8 pages start and end on whole characters and join to the file",
          "".join(pieces) == text_mb, (len("".join(pieces)), len(text_mb)))
    l1 = run(FR.list_files(path="many"))
    try:
        l2 = run(FR.list_files(path="many", offset=l1.get("next_offset") or 500))
    except TypeError:
        l2 = {}
    check("a 600-entry directory reports total 600 (was the cut count, 500)",
          l1.get("total") == 600 and len(l1.get("entries") or []) == 500 and l1.get("next_offset") == 500,
          {k: l1.get(k) for k in ("total", "next_offset")})
    check("next_offset reaches entries 501-600",
          len(l2.get("entries") or []) == 100 and (l2.get("entries") or [{}])[-1].get("name") == "e0599.txt",
          {k: l2.get(k) for k in ("total", "returned")})
    ix = run(FR.file_index())
    check("/index reports every file in the workspace (602), paged",
          ix.get("total") == 602 and len(ix.get("files") or []) == 500 and ix.get("next_offset") == 500,
          {k: ix.get(k) for k in ("total", "next_offset")})
finally:
    FR.WORKSPACE, FR.USER_FILES = saved_fs


_WORK.cleanup()
print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s): " + "; ".join(FAILS))
    sys.exit(1)
print("OK — route caps: whole words, one upload bound, whole embeddings, paged files and trees")
