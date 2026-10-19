#!/usr/bin/env python3
"""
Codesearch rerank-lane ratchet.

The bug this exists to prevent (found in a security sweep):
`brain/codesearch/bridge_embed.rerank` did not resolve the entity's configured
embed lane. `embed`/`embed_many` did — they call `_resolve_lane()` and honour a
BYO/bastion backend with no cross-lane fallback — but `rerank` went straight to
`_client()` and POSTed {"query", "docs"} to the platform bridge. `docs` is
`[h["content"] for h in fused]` (searcher.py), i.e. up to
CODESEARCH_RERANK_CANDIDATES chunks of the host's RAW SOURCE. So on a
BYO/bastion host the query embedding correctly rode the private lane and then
the rerank step shipped the query plus ~30 documents of plaintext to platform
compute — strictly worse than the vector leak the no-cross-lane-fallback rule
was written for, and a direct contradiction of the posture that a BYO host's
context never touches the platform.

There is no "send it to the bastion instead": bastions advertise `embed` /
`embed-relay` only (mastercode.embed_backends), and the bridge op is a TEI
cross-encoder that REJECTS a docs-less call (op_embeddings_rerank: "query +
non-empty docs required", 400), so a query-only rerank is not a shape the
contract can take. The fix is therefore: BYO/bastion configured → return None.
That is a SAFE degradation, not a feature loss — searcher.search guards on
`scores is not None` and keeps the RRF order, so search still works, it just
ranks by fusion instead of by cross-encoder. Gate 9 proves that property
rather than asserting it.

Same lane, same direction: lane SELECTION used to fail OPEN. A brain
pool that was not up, or a transient behavioral_params read error, returned
"no BYO configured" — which means "the bridge is legitimate" — so one pod-DB
blip was enough to route a bastion host's content to platform compute. Both
sites now report a DOWN backend instead (fail closed).

Gates:
  1. BYO configured → rerank() returns None and ZERO bridge calls are
     made (no document content leaves the pod).
  2. NEGATIVE CONTROL: the PRE-fix rerank, frozen verbatim from the old tree and
     run through this exact harness, DOES reach the bridge and DOES carry the
     document text — proving gate 1 can actually detect the leak.
  3. Bastion configured but down → rerank() None, zero bridge calls.
  4. behavioral_params read raises → lane undetermined → rerank() and
     embed() both None, zero bridge calls.
  5. no brain pool → same, closed.
  6. memory_engine._byo_embedding_config reports backend_down (not {})
     when the config read fails.
  7. NO REGRESSION: no BYO configured → the bridge IS the chosen backend;
     rerank() makes exactly one embeddings.rerank call and returns scores in
     INPUT order (None for an omitted index); embed() uses embeddings.embed.
  8. NO REGRESSION: BYO configured → embed() still embeds via the BYO lane and
     makes zero bridge calls.
  9. Degradation is safe: with rerank returning None, searcher.search still
     returns results, in RRF order, without raising.

Runs inside the image (all deps present):
  python3 /app/scripts/test_codesearch_rerank_lane.py
"""

import asyncio
import logging
import os
import sys
import types

logging.disable(logging.CRITICAL)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("APP_ROOT") or os.path.join(HERE, "..")
for _p in (os.path.join(ROOT, "brain"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Entity 72 is the sanctioned API/DB test entity, never a live Host.
EID = 72
# The configured model's size, and vectors of that size from each lane.
DIM = 768
BRIDGE_VEC = [0.1, 0.2, 0.3] + [0.0] * (DIM - 3)
BYO_VEC = [0.4, 0.5, 0.6] + [0.0] * (DIM - 3)

FAILS = []


def check(name: str, ok: bool, detail: object = ""):
    print(f"[rerank-lane] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── Harness ───────────────────────────────────────────────────────────────────
# The only things stubbed are the two `server.*` modules that need a live pod.
# Lane resolution itself is the REAL code path: real _resolve_lane, real
# memory_engine._byo_embedding_config, reading behavioral_params off a fake pool.

SECRET = "class AcmeLicenceKey:  # PROPRIETARY-DO-NOT-LEAK-7f3a\n    pass"
DOCS = [SECRET, "def helper(): return 1", "README: internal roadmap"]
QUERY = "licence key"


class RecordingClient:
    """Stands in for the in-pod bridge client. Records every op that reaches the
    platform, so a leak is an assertion about calls, not about a return value."""

    def __init__(self, results=None):
        self.calls = []
        self._results = results if results is not None else [
            {"index": 0, "score": 0.9}, {"index": 2, "score": 0.7}]

    async def request_full(self, op, params):
        self.calls.append((op, params))
        if op == "embeddings.rerank":
            return {"ok": True, "data": {"results": list(self._results)}}
        if op == "embeddings.embed":
            return {"ok": True, "data": {"vector": list(BRIDGE_VEC)}}
        return {"ok": False, "error": "unexpected op"}

    def leaked(self) -> bool:
        """Did any recorded call carry document content off the pod? Walks the
        param values rather than repr()-ing them — repr escapes the newlines in
        a real source chunk, which would make this detector silently blind."""
        def _walk(v):
            if isinstance(v, str):
                return SECRET in v
            if isinstance(v, dict):
                return any(_walk(x) for x in v.values())
            if isinstance(v, (list, tuple)):
                return any(_walk(x) for x in v)
            return False

        return any(_walk(params) for _op, params in self.calls)


class FakePool:
    """behavioral_params source. `raises=True` simulates the pod-DB blip."""

    def __init__(self, bp=None, raises=False):
        self._bp = bp if bp is not None else {}
        self._raises = raises

    async def fetchrow(self, _sql, _eid):
        if self._raises:
            raise RuntimeError("connection reset by peer")
        return {"behavioral_params": dict(self._bp)}


def install_server_stubs():
    """Fake `server.brain_service` + `server.main` in sys.modules."""
    pkg = sys.modules.get("server")
    if pkg is None or not hasattr(pkg, "__path__"):
        pkg = types.ModuleType("server")
        pkg.__path__ = []  # make it a package so `from server import x` works
        sys.modules["server"] = pkg
    bs = types.ModuleType("server.brain_service")
    bs._entity_id = EID
    bs._pool = None
    bs.get_pool = lambda: bs._pool
    mn = types.ModuleType("server.main")
    mn._client = None
    mn.get_bridge_client = lambda: mn._client
    sys.modules["server.brain_service"] = bs
    sys.modules["server.main"] = mn
    pkg.brain_service = bs
    pkg.main = mn
    return bs, mn


BS, MN = install_server_stubs()

from codesearch import bridge_embed, searcher            # noqa: E402
from mastercode import memory_engine                     # noqa: E402


def arrange(*, bp=None, raises=False, no_pool=False, client=None):
    """Point the harness at one scenario. Returns the recording client."""
    BS._pool = None if no_pool else FakePool(bp, raises=raises)
    MN._client = client if client is not None else RecordingClient()
    return MN._client


# ── The PRE-fix rerank, frozen verbatim from the old tree ─────────────────────
# This is a historical artifact and must NOT be "kept in sync" with the module.
# Its only job is to prove this harness detects the leak it was written for.

async def rerank_PREFIX(query, docs):
    if not docs:
        return None
    client = bridge_embed._client()
    if client is None:
        return None
    try:
        env = await client.request_full("embeddings.rerank",
                                        {"query": query, "docs": docs})
    except Exception:
        return None
    if not (env and env.get("ok")):
        return None
    scores = [None] * len(docs)
    for r in (env.get("data") or {}).get("results") or []:
        i, s = r.get("index"), r.get("score")
        if isinstance(i, int) and 0 <= i < len(docs) and s is not None:
            scores[i] = float(s)
    return scores


# ── Gates ─────────────────────────────────────────────────────────────────────

async def main() -> int:
    BYO = {"embedding_base_url": "http://host.internal:11434", "embedding_model": "m", "embedding_dim": DIM}

    # 1. BYO configured: no rerank, and nothing reaches the platform.
    c = arrange(bp=BYO)
    out = await bridge_embed.rerank(QUERY, DOCS)
    check("BYO configured → rerank returns None", out is None, out)
    check("BYO configured → ZERO bridge calls", c.calls == [], c.calls)
    check("BYO configured → no document content left the pod",
          not c.leaked(), c.calls)

    # 2. NEGATIVE CONTROL — same harness, pre-fix code: the leak is visible.
    c = arrange(bp=BYO)
    out = await rerank_PREFIX(QUERY, DOCS)
    check("negative control: PRE-fix rerank DID call the bridge",
          [op for op, _ in c.calls] == ["embeddings.rerank"], c.calls)
    check("negative control: PRE-fix rerank DID ship document content",
          c.leaked(), "harness cannot see the leak — gate 1 is worthless")
    check("negative control: PRE-fix rerank returned scores (it 'worked')",
          out == [0.9, None, 0.7], out)

    # 3. Bastion configured but with no live endpoint → down, not bridge. This
    #    drives the REAL _byo_embedding_config bastion branch; only the heartbeat
    #    lookup is stubbed.
    import mastercode.embed_backends as eb
    _eb_orig = eb.resolve_endpoint_for_hostname

    async def _no_endpoint(_pool, _eid, _host):
        return None

    eb.resolve_endpoint_for_hostname = _no_endpoint
    try:
        c = arrange(bp={"embedding_bastion": "bastion-a", "embedding_model": "m", "embedding_dim": DIM})
        out = await bridge_embed.rerank(QUERY, DOCS)
        check("bastion down → rerank returns None", out is None, out)
        check("bastion down → ZERO bridge calls (no fallback)", c.calls == [], c.calls)
    finally:
        eb.resolve_endpoint_for_hostname = _eb_orig

    # 4. behavioral_params read raises: lane undetermined → closed.
    c = arrange(bp=BYO, raises=True)
    out = await bridge_embed.rerank(QUERY, DOCS)
    check("config read error → rerank returns None", out is None, out)
    check("config read error → ZERO bridge calls", c.calls == [], c.calls)
    c = arrange(bp=BYO, raises=True)
    vec = await bridge_embed.embed("some private query")
    check("config read error → embed returns None", vec is None, vec)
    check("config read error → embed made ZERO bridge calls",
          c.calls == [], c.calls)

    # 5. no brain pool at all: still closed.
    c = arrange(no_pool=True)
    out = await bridge_embed.rerank(QUERY, DOCS)
    check("no brain pool → rerank returns None", out is None, out)
    check("no brain pool → ZERO bridge calls", c.calls == [], c.calls)
    c = arrange(no_pool=True)
    vec = await bridge_embed.embed("some private query")
    check("no brain pool → embed returns None", vec is None, vec)
    check("no brain pool → embed made ZERO bridge calls", c.calls == [], c.calls)

    # 6. the shared resolver reports DOWN, not "no BYO", on a read error.
    cfg = await memory_engine._byo_embedding_config(FakePool(raises=True), EID)
    check("_byo_embedding_config read error → backend_down (not {})",
          isinstance(cfg, dict) and cfg.get("backend_down") is True, cfg)
    cfg_ok = await memory_engine._byo_embedding_config(FakePool({}), EID)
    check("genuinely-unconfigured still returns {} (bridge legitimate)",
          cfg_ok == {}, cfg_ok)

    # 7. NO REGRESSION — no BYO: the bridge IS the chosen backend.
    c = arrange(bp={"embedding_model": "m", "embedding_dim": DIM})
    out = await bridge_embed.rerank(QUERY, DOCS)
    check("no-BYO: rerank makes exactly one embeddings.rerank call",
          [op for op, _ in c.calls] == ["embeddings.rerank"], c.calls)
    check("no-BYO: rerank sends the query + all docs",
          c.calls and c.calls[0][1] == {"query": QUERY, "docs": DOCS}, c.calls)
    check("no-BYO: scores come back in INPUT order, None for an omitted index",
          out == [0.9, None, 0.7], out)
    c = arrange(bp={"embedding_model": "m", "embedding_dim": DIM})
    vec = await bridge_embed.embed("q")
    check("no-BYO: embed still uses the bridge",
          [op for op, _ in c.calls] == ["embeddings.embed"] and vec == BRIDGE_VEC,
          (c.calls, vec))
    check("no-BYO: the bridge is asked for the CONFIGURED model (no model of code search's own)",
          c.calls and c.calls[0][1].get("model") == "m", c.calls)

    # 7b. No embedding model set up: embed asks nobody and answers None — a
    # setup step, never a call to some default model.
    c = arrange(bp={})
    vec = await bridge_embed.embed("q")
    check("no model set up: embed answers None and makes ZERO calls", vec is None and c.calls == [],
          (vec, c.calls))
    space, why = await bridge_embed.active_space()
    check("no model set up: no space, and the reason says embeddings are not set up yet",
          space is None and "not set up yet" in why, (space, why))
    c = arrange(bp={"embedding_model": "m", "embedding_dim": 1024})
    vec = await bridge_embed.embed("q")
    check("a vector of another size than the configured one is not used",
          vec is None and [op for op, _ in c.calls] == ["embeddings.embed"], (vec, c.calls))

    # 8. NO REGRESSION — BYO configured: embed still rides the BYO lane.
    seen = []

    async def _fake_byo(text, model, cfg, *, entity_id=None, pool=None):
        seen.append((text, model, cfg.get("base_url")))
        return list(BYO_VEC)

    _orig_byo = memory_engine._embed_via_byo
    memory_engine._embed_via_byo = _fake_byo
    try:
        c = arrange(bp=BYO)
        vec = await bridge_embed.embed("private query text")
        check("BYO: embed returns the BYO vector", vec == BYO_VEC, vec)
        check("BYO: embed asked the endpoint for the CONFIGURED model",
              seen and seen[-1][1] == "m", seen)
        check("BYO: embed made ZERO bridge calls", c.calls == [], c.calls)
        check("BYO: embed hit the configured endpoint",
              seen and seen[-1][2] == BYO["embedding_base_url"], seen)

        # 9. Degradation is SAFE — search still works, just in RRF order.
        hits = [{"file_path": f"f{i}.py", "chunk_index": 0, "content": DOCS[i % len(DOCS)]}
                for i in range(3)]

        class _StubStore:
            @staticmethod
            async def search_vector(_db, **_kw):
                return hits

            @staticmethod
            async def search_lexical(_db, **_kw):
                return list(reversed(hits))

        _orig_store = searcher.store
        searcher.store = _StubStore
        try:
            c = arrange(bp=BYO)
            res = await searcher.search(None, project="p", query=QUERY, limit=10)
            check("degradation: search still returns results on a BYO host",
                  len(res) == 3, res)
            check("degradation: results keep the RRF order (no rerank_score)",
                  all("rerank_score" not in h for h in res)
                  and [h["rrf_score"] for h in res] == sorted(
                      (h["rrf_score"] for h in res), reverse=True), res)
            check("degradation: the whole search made ZERO bridge calls",
                  c.calls == [], c.calls)
        finally:
            searcher.store = _orig_store
    finally:
        memory_engine._embed_via_byo = _orig_byo

    # 10. THE LOG NEVER HOLDS THE WORDS. An exception, or a not-ok envelope,
    # can carry the text that was sent (a provider echoing its input; asyncpg
    # quoting an argument it cannot encode). The pod keeps this log, so each
    # line names the exception's type, or the envelope's short `code`, and
    # nothing of the text.
    WORDS = "hunter2hunter2 private ledger words"

    class _RaisingClient(RecordingClient):
        async def request_full(self, op, params):
            self.calls.append((op, params))
            raise RuntimeError(f"bridge refused input {params.get('input')!r}")

    class _NotOkClient(RecordingClient):
        def __init__(self, code):
            super().__init__()
            self._code = code

        async def request_full(self, op, params):
            self.calls.append((op, params))
            env = {"ok": False, "error": f"model rejected input: {params.get('input')}"}
            if self._code is not None:
                env["code"] = self._code
            return env

    async def _raising_byo(text, model, cfg, *, entity_id=None, pool=None):
        raise ValueError(f"endpoint answered 400: input {text!r} too long")

    caught: list = []

    class _Keep(logging.Handler):
        def emit(self, record):
            caught.append(record.getMessage())

    be_log = logging.getLogger(bridge_embed.log.name)
    keep, old_level = _Keep(level=logging.DEBUG), be_log.level
    be_log.addHandler(keep)
    be_log.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)  # this file silences logging (top); this section reads it
    memory_engine._embed_via_byo = _raising_byo
    try:
        lines = {}
        for label, client, bp in (
                ("a raising bridge", _RaisingClient(), {"embedding_model": "m", "embedding_dim": DIM}),
                ("a not-ok envelope with a code", _NotOkClient("input_rejected"), {"embedding_model": "m", "embedding_dim": DIM}),
                ("a not-ok envelope whose code is a sentence", _NotOkClient(f"rejected: {WORDS}"),
                 {"embedding_model": "m", "embedding_dim": DIM}),
                ("a raising BYO lane", None, BYO)):
            start = len(caught)
            arrange(bp=bp, client=client)
            vec = await bridge_embed.embed(WORDS)
            lines[label] = caught[start:]
            check(f"log: {label}: embed answers None and logs a line", vec is None and lines[label], lines[label])
            check(f"log: {label}: no line holds the words",
                  not any("hunter2" in m or "ledger words" in m for m in lines[label]), lines[label])
        check("log: the raising bridge's line names the exception's type",
              any("RuntimeError" in m for m in lines["a raising bridge"]), lines["a raising bridge"])
        check("log: a not-ok envelope's line carries its short code",
              any("(input_rejected)" in m for m in lines["a not-ok envelope with a code"]),
              lines["a not-ok envelope with a code"])
        check("log: the BYO lane's line names the exception's type",
              any("ValueError" in m for m in lines["a raising BYO lane"]), lines["a raising BYO lane"])
    finally:
        be_log.removeHandler(keep)
        be_log.setLevel(old_level)
        logging.disable(logging.CRITICAL)
        memory_engine._embed_via_byo = _orig_byo

    print()
    if FAILS:
        print(f"[rerank-lane] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("[rerank-lane] PASS — rerank obeys the configured lane; document "
          "content never reaches the bridge from a BYO/bastion host, and an "
          "unreadable lane fails closed")
    return 0


sys.exit(asyncio.run(main()))
