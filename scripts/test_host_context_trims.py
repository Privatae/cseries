#!/usr/bin/env python3
"""Host context trims after a harness test on one Host:
condensed compare and code-search results, the empty-reply retry inside the
loop with the turn's own tools, honest usage on a call that reported none,
a self turn decided only by the person's words (the inventory itself is gone
now; the decision widens the tool trail), and the provider's request
id and cache-pin hash on each per-call ledger entry.

Measured on that Host:
  - review_session returned the session service whole: 258,351 chars, carried
    at first 96k-119k tokens, three times;
  - one code_grep (max_results 40) returned 173,784 chars, 167,451 of them two
    lines of a built circles bundle (replayed against the same tree);
  - in one turn call 1 came back empty, the retry ran past the loop with `escalate`
    alone; the model wrote "I am checking whether the site is actually
    serving", could not check, and the changed list cost the cache;
  - in another, two calls came back with no usage object; stored as 0/0 and
    the notice said "the model returned its reasoning";
  - that same empty-call turn (a continuation) carried the 25,409-char self inventory;
  - 5 cold misses with an unchanged prefix had nothing to take to xAI.

Every part drives the real code: the tool executors through
tools._execute_meta_tool / their executor, provider_gateway._call_remote on a
fake wire, and EntityRuntime.send_message with a scripted model (the harness
of scripts/test_condensed_tool_results.py).

Run: APP_ROOT=<repo> PYTHONPATH=brain:. python3 scripts/test_host_context_trims.py
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))
logging.disable(logging.CRITICAL)

failures: list = []
SIZES: list = []


def check(label, ok, detail=""):
    print(f"[trims] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {str(detail)[:400]}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def size(x) -> int:
    return len(json.dumps(x, default=str))


def run(coro):
    return asyncio.run(coro)


class _Pool:
    async def fetch(self, *a):
        return []

    async def fetchrow(self, *a):
        return None

    async def fetchval(self, *a):
        return None

    async def execute(self, *a):
        return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


# ── 1. review_session ───────────────────────────────────────────────────────
# Diff sizes shaped like the seven variants of the measured compare (files
# landed: 41.6 / 0 / 89 / 54 / 51 / 48 / 74 KB); synthetic content.
VARIANTS = [("cursor", "composer-2.5", 41_600), ("opencode", "glm-5.3-prime", 0),
            ("antigravity", "gemini-3.8-flash-high", 89_000), ("claude-code", "opus-5.5", 54_000),
            ("codex", "gpt-6-astra", 51_000), ("kimi", "k3", 48_000), ("grok", "grok-4.7", 74_000)]


def _diff(n_bytes: int, tag: str) -> str:
    out, i, total = [], 0, 0
    while total < n_bytes:
        body = "".join(f"+line {j} of {tag} file {i}\n" for j in range(120))
        part = (f"diff --git a/src/{tag}/f{i}.js b/src/{tag}/f{i}.js\nnew file mode 100644\n"
                f"--- /dev/null\n+++ b/src/{tag}/f{i}.js\n@@ -0,0 +1,120 @@\n{body}")
        out.append(part)
        total += len(part)
        i += 1
    return "".join(out)


def session_fixture() -> dict:
    vids = [v for v, _m, _n in VARIANTS]
    return {
        "session_id": "c1b84a99c98841e7a7742e016a2f4648",
        "manifest": {"prompt": "Build a tile game. " * 80, "project_id": 9423, "project_dir": "/p/9423",
                     "variants": [{"vid": v, "harness": v, "model": m, "effort": "high"}
                                  for v, m, _n in VARIANTS],
                     "created_at": 1.0, "completed_at": 2.0,
                     "results": {v: {"ok": v != "claude-code", "harness": v,
                                     "transcript_tail": ("worked on the board renderer. " * 40) + f"{v} done.",
                                     "error": "stdout buffer exceeded" if v == "claude-code" else None}
                                 for v in vids}},
        "diffs": {v: _diff(n, v) for v, _m, n in VARIANTS},
        "picks": {},
        # A session stored before 2026-10-20 can still carry the removed diff
        # reviewer's verdicts; nothing reads them any more.
        "review": {"codex": {"ran": True, "ok": True, "score": 8, "non_blocking": ["a"], "blocking": [],
                             "model": "claude-opus-5"}},
        "per_harness": {v: {"state": "failed" if v == "claude-code" else "done", "model": m,
                            "elapsed_seconds": 200.0 + i, "diff_bytes": n,
                            "error": "stdout buffer exceeded" if v == "claude-code" else None}
                        for i, (v, m, n) in enumerate(VARIANTS)},
        "status": "completed",
        "entrypoints": {v: True for v in vids},
        "app": {"installed": False},
    }


def part_review_session():
    from server import brain_service
    from server.routes import review as RV
    from mastercode.runtime import tools as T
    fx = session_fixture()

    async def _svc(pool=None, *, session_id):
        return copy.deepcopy(fx)
    saved = (RV.get_session_service, brain_service.get_pool)
    RV.get_session_service, brain_service.get_pool = _svc, (lambda: None)
    try:
        r = run(T._exec_review_session({"session_id": fx["session_id"]}))
        before, after = size({"status": "ok", "result": fx}), size(r)
        SIZES.append(("review_session (7 variants)", before, after))
        res = r.get("result") or {}
        rows = res.get("variants") or []
        check("review_session default: a line per variant, in the manifest's order",
              r.get("status") == "ok" and [x.get("variant") for x in rows] == [v for v, _m, _n in VARIANTS],
              repr(r)[:300])
        check("review_session default: harness, model, state, elapsed, diff size, files and an outcome per "
              "variant, and no diff",
              len(rows) == 7 and all({"harness", "model", "state", "elapsed_seconds", "diff_bytes", "files",
                                      "outcome"} <= set(x) and "diff" not in x for x in rows)
              and rows[2]["diff_bytes"] == 89_000 and rows[2]["files"][0].startswith("src/antigravity/"),
              repr(rows[:1])[:300])
        check("review_session default: the failed run says why, the rest the end of what the harness said "
              "(a stored reviewer verdict is not read: the reviewer is gone)",
              len(rows) == 7 and rows[3]["state"] == "failed" and rows[3]["outcome"] == "stdout buffer exceeded"
              and str(rows[4]["outcome"]).endswith("codex done.")
              and str(rows[0]["outcome"]).endswith("cursor done.")
              and len(str(rows[0]["outcome"])) <= 301, [x.get("outcome") for x in rows])
        man = res.get("manifest") or {}
        check("review_session default: the manifest rides without its per-variant results, the brief cut "
              "with its length",
              "results" not in man and man.get("prompt_chars") == len(fx["manifest"]["prompt"])
              and man.get("project_id") == 9423, repr(man)[:300])
        check("review_session default: the result names the variant call", "variant=" in str(res.get("detail")))
        check(f"review_session default: {before:,} -> {after:,} chars (under a twentieth)", after * 20 < before,
              f"{before} -> {after}")
        r = run(T._exec_review_session({"session_id": fx["session_id"], "variant": "grok"}))
        one = r.get("result") or {}
        check("review_session(variant='grok'): that variant whole — its diff byte-for-byte, state, result",
              one.get("variant") == "grok" and one.get("diff") == fx["diffs"]["grok"]
              and one.get("state") == fx["per_harness"]["grok"]
              and one.get("result") == fx["manifest"]["results"]["grok"], repr(r)[:300])
        r = run(T._exec_review_session({"session_id": fx["session_id"], "variant": "codex"}))
        check("review_session(variant='codex'): a stored reviewer verdict does not ride the variant view",
              (r.get("result") or {}).get("variant") == "codex" and "review" not in (r.get("result") or {}),
              repr(r)[:300])
        r = run(T._exec_review_session({"session_id": fx["session_id"], "variant": "nope"}))
        check("review_session(variant=<unknown>): refused as the model's own error, naming the variants",
              r.get("status") == "error" and r.get("input_refused") is True and "kimi" in str(r.get("result")),
              repr(r)[:300])
        # The manifest's `sent_prompt`: the whole string each harness
        # was sent, 17,217 chars on project 9423. Seven of them must not ride
        # the default view; one rides the variant view whole.
        fx["manifest"]["sent_prompt"] = {v: f"SENT-{v}-" + "b" * 17_200 for v in fx["manifest"]["results"]}
        r = run(T._exec_review_session({"session_id": fx["session_id"]}))
        man = (r.get("result") or {}).get("manifest") or {}
        check("review_session default: the sent prompts ride as their lengths, never whole",
              "SENT-" not in json.dumps(r) and man.get("sent_prompt_chars") == {
                  v: len(s) for v, s in fx["manifest"]["sent_prompt"].items()}, repr(man)[:300])
        r = run(T._exec_review_session({"session_id": fx["session_id"], "variant": "grok"}))
        check("review_session(variant='grok'): carries that variant's sent prompt whole, and no other",
              (r.get("result") or {}).get("sent_prompt") == fx["manifest"]["sent_prompt"]["grok"]
              and "SENT-kimi" not in json.dumps(r), repr(r)[:300])
    finally:
        RV.get_session_service, brain_service.get_pool = saved
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P
    fn = P["review_session"]["function"]
    check("registry: review_session takes `variant` and says the default is a line per variant",
          "variant" in fn["parameters"]["properties"] and "a line per variant" in fn["description"]
          and "every variant's unified diff" not in fn["description"])
    skill = " ".join((_ROOT / "default-skills" / "review.md").read_text().split())
    check("skill review: names the per-variant line and variant=<id>",
          "`review_session(session_id)` is a line per variant" in skill and "variant='<id or harness>'" in skill)


# ── 2. code_grep ────────────────────────────────────────────────────────────
class _BC:
    """The bridge client: answers code.grep the way the operator's /repo-grep
    does — the first max_results matches, each line whole."""
    _connected = True

    def __init__(self, matches):
        self.matches, self.asked = matches, []

    async def request_full(self, op, args):
        self.asked.append((op, dict(args)))
        n = int(args.get("max_results") or 200)
        page = self.matches[:n]
        return {"ok": True, "data": {"repo": args.get("repo"), "pattern": args.get("pattern"),
                                     "matches": copy.deepcopy(page), "truncated": len(page) >= n}}


def _grep_env(bc):
    """server.main is the pod's app module (it cannot import outside the
    image); the dispatcher reads only get_bridge_client from it. The host
    holds every entity scope."""
    from mastercode.runtime import meta_tool_caps as MTC
    mod = types.ModuleType("server.main")
    mod.get_bridge_client = lambda: bc
    saved = (sys.modules.get("server.main"), MTC._resolve_entity_scopes)

    async def _scopes(pool, eid, **kw):
        return set(MTC.ENTITY_SCOPE_TO_BITS)
    sys.modules["server.main"] = mod
    MTC._resolve_entity_scopes = _scopes
    return saved


def _grep_restore(saved):
    from mastercode.runtime import meta_tool_caps as MTC
    if saved[0] is None:
        sys.modules.pop("server.main", None)
    else:
        sys.modules["server.main"] = saved[0]
    MTC._resolve_entity_scopes = saved[1]


def _grep(args):
    from mastercode.runtime import tools as T
    return run(T._execute_meta_tool("code_grep", args, 3, _Pool()))


def part_code_grep():
    from mastercode.runtime import tools as T
    pat = "support.*draft|/api/support"
    # The measured shape: two lines of a minified bundle (98,078 and 69,373
    # chars, the match deep inside) among 38 ordinary source lines.
    bundle = [{"path": "system-apps/circles/assets/index-x.js", "line_number": 1,
               "text": "a" * 61_000 + "fetch('/api/support/drafts')" + "b" * (98_078 - 61_028)},
              {"path": "system-apps/circles/assets/index-x.js", "line_number": 2,
               "text": "c" * 30_000 + "support_draft" + "d" * (69_373 - 30_013)}]
    src = [{"path": f"server/routes/support{i % 4}.py", "line_number": 100 + i,
            "text": f"    @router.post('/api/support/{i}')  # support draft {i}"} for i in range(38)]
    bc = _BC(bundle + src)
    saved = _grep_env(bc)
    try:
        r = _grep({"repo": "owner/repo", "pattern": pat, "max_results": 40, "glob": "*.{js,py}"})
        before = size({"status": "ok", "result": {"repo": "owner/repo", "pattern": pat,
                                                  "matches": bundle + src, "truncated": True}})
        after = size(r)
        SIZES.append(("code_grep (max_results 40, 2 bundle lines)", before, after))
        res = r.get("result") or {}
        m = res.get("matches") or []
        check("code_grep: every match on the page, in the operator's order, path and line_number intact",
              r.get("status") == "ok" and len(m) == 40
              and [(x["path"], x["line_number"]) for x in m]
              == [(x["path"], x["line_number"]) for x in bundle + src], repr(r)[:300])
        check("code_grep: a bundle line comes back as the excerpt around its match, cut, with the line's "
              "length and the match column",
              len(m) > 1 and m[0].get("cut") is True and m[0].get("line_chars") == 98_078
              and m[0].get("match_col") == 61_007 and "/api/support" in m[0]["text"]
              and len(m[0]["text"]) <= 402 and m[1].get("match_col") == 30_000
              and "support_draft" in m[1]["text"], repr([{k: v for k, v in x.items() if k != "text"}
                                                         for x in m[:2]]))
        check("code_grep: ordinary lines whole, not marked",
              len(m) > 2 and all("cut" not in x for x in m[2:]) and m[2]["text"] == src[0]["text"])
        check("code_grep: says how to read a cut line whole", "code_read" in str(res.get("cut_note")))
        check(f"code_grep: {before:,} -> {after:,} chars (under a tenth)", after * 10 < before,
              f"{before} -> {after}")
        check("code_grep: the operator is asked for one past the page; offset never reaches it",
              bc.asked and bc.asked[-1][0] == "code.grep" and bc.asked[-1][1].get("max_results") == 41
              and "offset" not in bc.asked[-1][1], bc.asked[-1:])
        many = [{"path": f"a/f{i}.py", "line_number": i, "text": f"x = {i}  # support draft"} for i in range(130)]
        bc2 = _BC(many)
        _grep_restore(saved)
        saved = _grep_env(bc2)
        res = (_grep({"repo": "owner/repo", "pattern": "support draft", "max_results": 200}).get("result")
               or {})
        check("code_grep max_results 200: a page of 50, next_offset 50, a 'more' pointer and the page note",
              len(res.get("matches") or []) == 50 and res.get("next_offset") == 50
              and "offset=50" in str(res.get("more")) and "50 per page" in str(res.get("note")),
              repr(res)[:300])
        res = (_grep({"repo": "owner/repo", "pattern": "support draft", "max_results": 50, "offset": 100})
               .get("result") or {})
        check("code_grep offset=100: the rest (30), no next_offset; the operator was asked for 151",
              [x["line_number"] for x in res.get("matches") or []] == list(range(100, 130))
              and "next_offset" not in res and bc2.asked[-1][1].get("max_results") == 151, repr(res)[:300])
        # A page of long paths is held by the character bound too.
        long_rows = [{"path": "p/" + "d" * 600 + f"/f{i}.py", "line_number": i, "text": "t" * 390}
                     for i in range(50)]
        bc3 = _BC(long_rows)
        _grep_restore(saved)
        saved = _grep_env(bc3)
        res = _grep({"repo": "owner/repo", "pattern": "t"}).get("result") or {}
        got = res.get("matches") or []
        check("code_grep: a page is held to its character bound, and next_offset picks up where it stopped",
              0 < len(got) < 50 and size(got) <= getattr(T, "_CODE_GREP_PAGE_CHARS", 0) + 1_200
              and res.get("next_offset") == len(got), (len(got), size(got), res.get("next_offset")))
    finally:
        _grep_restore(saved)
    from mastercode.runtime.triage import _BRIDGE_TOOL_SCHEMAS as B
    fn = B["code_grep"]["function"]
    check("schema: code_grep takes offset and says a long line is an excerpt",
          "offset" in fn["parameters"]["properties"] and "excerpt" in fn["description"])


# ── 4a / 7a. provider_gateway on a fake wire ────────────────────────────────
class _Resp:
    def __init__(self, body, headers):
        self.status, self.headers, self._body = 200, headers, body

    async def json(self):
        return copy.deepcopy(self._body)

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


def _http(bodies, sent):
    class _S:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        def post(self, url, headers=None, json=None):
            sent.append((url, dict(headers or {}), json))
            body, hdrs = bodies.pop(0)
            return _Resp(body, hdrs)
    return _S


class _LedgerPool:
    def __init__(self):
        self.rows = []

    async def execute(self, sql, *args):
        self.rows.append((sql, args))
        return "INSERT 0 1"


def part_gateway():
    from mastercode import provider_gateway as PG
    sent: list = []
    ok_body = {"id": "gen-abc123", "model": "x-model",
               "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230,
                         "prompt_tokens_details": {"cached_tokens": 1024}}}
    bare_body = {"model": "x-model", "choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}

    async def _key():
        return "sk-test"
    led = _LedgerPool()
    saved = (PG.aiohttp.ClientSession, PG._load_user_openrouter_key, PG._inference_pool)
    PG.aiohttp.ClientSession = _http([(ok_body, {}), (bare_body, {"x-request-id": "req-hdr-9"})], sent)
    PG._load_user_openrouter_key = _key
    PG._inference_pool = led
    try:
        r1 = run(PG._call_remote([{"role": "user", "content": "hi"}], "x-model", provider="openrouter",
                                 max_tokens=64, cache_key="conv-abc"))
        r2 = run(PG._call_remote([{"role": "user", "content": "hi"}], "x-model", provider="openrouter",
                                 max_tokens=64, cache_key="conv-abc"))
    finally:
        PG.aiohttp.ClientSession, PG._load_user_openrouter_key, PG._inference_pool = saved
    pin = hashlib.sha256(b"conv-abc").hexdigest()[:12]
    check("gateway: a result carries the provider's response id and a 12-hex hash of the cache pin it sent",
          r1.get("request_id") == "gen-abc123" and r1.get("cache_key_sha") == pin
          and (sent[0][2] or {}).get("prompt_cache_key") == "conv-abc",
          repr({k: r1.get(k) for k in ("request_id", "cache_key_sha", "error")}))
    check("gateway: a response with no body id takes the header's request id",
          r2.get("request_id") == "req-hdr-9", r2.get("request_id"))
    check("gateway: a reported usage stays as it was (in 1200, cached 1024, out 30)",
          r1.get("prompt_tokens") == 1200 and r1.get("cache_read_tokens") == 1024
          and r1.get("completion_tokens") == 30 and "usage_reported" not in r1, repr(r1)[:300])
    check("gateway: NO usage object -> the counts are None (unknown), usage_reported False — never 0",
          r2.get("usage_reported") is False and r2.get("prompt_tokens") is None
          and r2.get("completion_tokens") is None and r2.get("input_total_tokens") is None
          and r2.get("cache_read_tokens") is None, repr(r2)[:300])
    rows = [a for s, a in led.rows if "INSERT INTO inference_cost_log" in s]
    check("inference_cost_log: the reported call's row has its counts",
          len(rows) == 2 and rows[0][5] == 1200 and rows[0][6] == 30 and rows[0][11] == 1024,
          [r[5:7] for r in rows])
    md2 = json.loads(rows[1][10]) if len(rows) == 2 else {}
    check("inference_cost_log: the unreported call's row is NULL in / NULL out / NULL cache, unpriced, "
          "marked usage=not_reported",
          len(rows) == 2 and rows[1][5] is None and rows[1][6] is None and rows[1][11] is None
          and rows[1][12] is None and rows[1][8] == "unpriced" and rows[1][7] == 0.0
          and md2.get("usage") == "not_reported" and md2.get("total_tokens") is None, rows[1:])
    mig = _ROOT / "migrations" / "v214_inference_usage_unknown.sql"
    mig_text = mig.read_text() if mig.exists() else ""
    check("schema: the token columns take NULL (migration v214, init-db.sql)",
          all(f"ALTER COLUMN {c} DROP NOT NULL" in mig_text
              for c in ("tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens"))
          and "tokens_in       INTEGER DEFAULT 0," in (_ROOT / "init-db.sql").read_text())

    # The stream keeps the completion id too.
    class _Content:
        def __init__(self, chunks):
            self.chunks = chunks

        async def iter_any(self):
            for c in self.chunks:
                yield c

    class _SResp:
        content = _Content([b'data: {"id":"gen-s1","model":"m","choices":[{"delta":{"content":"hi"}}]}\n',
                            b'data: {"id":"gen-s1","choices":[{"delta":{},"finish_reason":"stop"}],'
                            b'"usage":{"prompt_tokens":5,"completion_tokens":1}}\n', b"data: [DONE]\n"])

    async def _cb(_t):
        return None
    relay = run(PG._consume_openrouter_stream(_SResp(), _cb))
    check("gateway: the stream reader keeps the completion id", relay.get("id") == "gen-s1", relay)


# ── 3, 4b, 5, 7b. the turn loop ─────────────────────────────────────────────
def _harness():
    from mastercode.runtime import session as S
    from mastercode import provider_gateway as PG
    from mastercode import inference_config as IC
    import mastercode.memory_engine as ME
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P
    from server.routes import inference as INF
    INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
    INF._CATALOG_CACHE["exp"] = 1e18

    class Harness:
        def __init__(self, script, results=None):
            self.script = list(script)
            self.results = {k: list(v) for k, v in (results or {}).items()}
            self.calls, self.tools, self.runs, self.rows = [], [], [], []
            self.tool_shas = []   # each call's whole tool list, hashed as the ledger hashes it

        async def model(self, messages=None, *a, **kw):
            msgs = messages if messages is not None else (a[0] if a else [])
            self.calls.append([dict(m) for m in msgs])
            self.tools.append(sorted(((t.get("function") or {}).get("name") or "?")
                                     for t in (kw.get("tools") or [])))
            self.tool_shas.append(S._bytes_sha(kw.get("tools") or []))
            r = self.script.pop(0) if self.script else {"content": "(the script ran out)"}
            cb_ = kw.get("stream_cb")
            if cb_ and r.get("content"):
                await cb_(r["content"])
            base = {"tokens_used": 1, "prompt_tokens": 1000, "completion_tokens": 10,
                    "input_total_tokens": 1000, "tool_calls": None}
            if r.get("usage_reported") is False:
                # What provider_gateway._call_remote returns for a response
                # with no usage object (part_gateway drives that).
                base = {"tokens_used": 0, "prompt_tokens": None, "completion_tokens": None,
                        "input_total_tokens": None, "cache_read_tokens": None,
                        "cache_write_tokens": None, "tool_calls": None}
            return {**base, **r}

        async def go(self, message, **send_kw):
            S._pending_nudges.clear()
            S._turn_events.clear()
            S._task_holds.clear()
            saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                     S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                     S._execute_meta_tool, S._build_tool_schemas, ME.write_turn_row,
                     ME.write_turn_row_by_token)

            async def route(pool, eid, role, thread_override=None, **kw):
                return {"provider": "anthropic", "model": "claude-opus-5-5"}

            async def run_close_turn(pool, sess, args, *, prompt_console=None):
                return {"status": "ok", "result": "recorded"}

            async def meta(name, args, **kw):
                self.runs.append((name, dict(args or {})))
                seq = self.results.get(name)
                return copy.deepcopy(seq.pop(0) if seq else {"status": "ok", "result": {"ok": True}})

            async def schemas(pool, eid, interaction_context=None, **kw):
                return [{"type": "function", "function": copy.deepcopy(P[n]["function"])}
                        for n in ("harness_connections", "coding_bench", "get_network", "nac_status")]

            async def wtr(pool, entity_id, session_id, role, content, *, tags=None, **kw):
                self.rows.append({"role": role, "content": content, "tags": dict(tags or {})})
                return len(self.rows)

            async def wtr_tok(pool, entity_id, session_id, role, content, *, token, held, tags=None, **kw):
                self.rows.append({"role": role, "content": content, "tags": dict(tags or {})})
                return len(self.rows)
            S._call_entity_model = self.model
            PG.call_model = self.model
            IC.resolve_role_route = route
            S._AVAILABLE["ceigas_domain_controller"] = None
            S._run_close_turn = run_close_turn
            S._execute_meta_tool = meta
            S._build_tool_schemas = schemas
            ME.write_turn_row, ME.write_turn_row_by_token = wtr, wtr_tok
            rt = S.EntityRuntime(_Pool())

            async def ctx(entity_id, **kw):
                return S.EntitySession(
                    session_id="sess-trims", entity_id=entity_id,
                    entity={"id": entity_id, "name": "Nova", "behavioral_params": {},
                            "permission_overrides": {"bot_capabilities": 255}},
                    interaction_context="app:circles")
            rt._build_chat_context_transient = ctx
            try:
                self.out = await rt.send_message(entity_id=9003, user_message=message, **send_kw)
                await asyncio.sleep(0.05)
            finally:
                (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
                 S._execute_meta_tool, S._build_tool_schemas, ME.write_turn_row,
                 ME.write_turn_row_by_token) = saved
            return self

        def row(self):
            for r in reversed(self.rows):
                if r["role"] == "assistant":
                    return r
            return {}

        def ledger(self):
            return (self.row().get("tags") or {}).get("context_ledger") or {}
    return Harness


def _tc(name, args, i):
    return {"id": f"call_{name}_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def part_loop():
    Harness = _harness()
    # The measured shape: call 1 empty; the retry checks, then answers.
    h = run(Harness([
        {"content": ""},
        {"content": "", "tool_calls": [_tc("get_network", {}, 1)]},
        {"content": "The site is serving."},
    ], {"get_network": [{"status": "ok", "result": {"bastions": [], "peers": 0}}]}).go(
        "is the site serving now?"))
    check("retry: the empty reply is retried, the retry's call RUNS, and the turn answers (3 calls)",
          len(h.calls) == 3 and [n for n, _ in h.runs] == ["get_network"]
          and (h.row().get("content") or "").strip() == "The site is serving.",
          (len(h.calls), h.runs, h.row().get("content")))
    check("retry: it goes with the turn's own tool list — the same list as call 1, not `escalate` alone",
          len(h.tools) >= 2 and h.tools[1] == h.tools[0] and len(h.tools[0]) > 1, h.tools)
    # One mode: the list is fixed for the turn, byte for byte,
    # and the ledger's first-call hash is the hash of what call 1 sent
    # (test_cache_stable_prefix pins that the ledger records it).
    check("one list per turn: every call sends the same tool list, byte for byte",
          len(h.tool_shas) == 3 and len(set(h.tool_shas)) == 1, h.tool_shas)
    check("ledger: first_call_sha is the hash of the list call 1 sent",
          (h.ledger().get("tools") or {}).get("first_call_sha") == (h.tool_shas or [None])[0],
          ((h.ledger().get("tools") or {}).get("first_call_sha"), h.tool_shas[:1]))
    check("retry: it asks once, in the platform's words",
          len(h.calls) >= 2 and h.calls[1][-1].get("role") == "user"
          and h.calls[1][-1].get("content", "").startswith("Please respond to the conversation above."))

    # Both empty, nothing reported: one retry, then the notice says what came back.
    h = run(Harness([{"content": "", "usage_reported": False},
                     {"content": "", "usage_reported": False}]).go("status?"))
    note = h.row().get("content") or ""
    check("retry: one retry per turn (2 calls when it is empty too)", len(h.calls) == 2, len(h.calls))
    check("notice: two empty calls with no usage are told as that — never 'returned its reasoning'",
          note.startswith("I hit a snag") and "no usage reported" in note and "reasoning" not in note, note)
    pc = h.ledger().get("per_call") or []
    check("ledger: a call with no usage is in None / out None / cached None, usage not_reported — not 0/0",
          len(pc) == 2 and all(c.get("in") is None and c.get("out") is None and c.get("cached") is None
                               and c.get("usage") == "not_reported" for c in pc), pc)
    tags = h.row().get("tags") or {}
    check("turn row: a turn none of whose calls reported has no token counts, and says how many calls",
          tags.get("tokens_in") is None and tags.get("tokens_out") is None
          and tags.get("tokens_unknown_calls") == 2,
          {k: tags.get(k) for k in ("tokens_in", "tokens_out", "tokens_unknown_calls")})
    h = run(Harness([{"content": "", "reasoning": "thinking..."}, {"content": ""}]).go("status?"))
    note = h.row().get("content") or ""
    check("notice: reasoning with no reply is still told as that",
          "returned its reasoning but no actual reply" in note, note)
    h = run(Harness([{"content": ""}, {"content": ""}]).go("status?"))
    note = h.row().get("content") or ""
    check("notice: an empty call with usage and no reasoning says it came back with no reply",
          "came back with no reply" in note and "reasoning" not in note, note)

    # The per-call ledger keeps the provider's id and the pin hash, ledger-only.
    h = run(Harness([{"content": "Done.", "request_id": "gen-1", "cache_key_sha": "a1b2c3d4e5f6"}]).go("hi"))
    pc = h.ledger().get("per_call") or []
    from mastercode import context_budget as cb
    check("ledger: per_call carries request_id and cache_key_sha",
          len(pc) == 1 and pc[0].get("request_id") == "gen-1" and pc[0].get("cache_key_sha") == "a1b2c3d4e5f6",
          pc)
    check("ledger: both are rig measurements, kept out of the Host's own mirror",
          {"request_id", "cache_key_sha"} <= set(cb._LEDGER_ONLY_CALL_KEYS))


def _sys_text(h) -> str:
    return "".join(str(m.get("content")) for m in (h.calls[0] if h.calls else []) if m.get("role") == "system")


def part_self_turn():
    # The self inventory is gone (brief prompt): a question about the
    # Host is answered from the fixed list and find_tools, on every turn. What
    # the self-turn decision still changes is the tool trail it reads: a turn
    # about the Host reads the thread's record (SELF_TRAIL_TURNS), not three
    # turns of it. Only the person's own words may decide that; a brief the
    # platform wrote ("your build", "what you started") must not.
    import mastercode.memory_engine as ME
    from mastercode.runtime.prefire_tools import SELF_TRAIL_TURNS
    from mastercode.runtime.tool_trail import TOOL_TRAIL_MAX_TURNS
    Harness = _harness()
    limits: list = []
    saved_rtt = ME.recall_tool_trail

    async def rtt(pool, entity_id, session_id, *, source_context=None, limit=3, before=None):
        limits.append(limit)
        return []

    def trail_limit(h_run):
        limits.clear()
        h = h_run()
        return h, (limits[-1] if limits else None)

    words = "what tools do you have and what can you do here?"
    ME.recall_tool_trail = rtt
    try:
        person, lim_person = trail_limit(
            lambda: run(Harness([{"content": "Here is what I hold."}]).go(words)))
        woken, lim_woken = trail_limit(
            lambda: run(Harness([{"content": "The compare finished."}]).go(words, system_turn=True)))
        saved_req, lim_saved = trail_limit(
            lambda: run(Harness([{"content": "Here is what I hold."}]).go(
                words, system_turn=True, saved_request=True)))
        plain, lim_plain = trail_limit(
            lambda: run(Harness([{"content": "Hello."}]).go("thanks, that's all for today")))
    finally:
        ME.recall_tool_trail = saved_rtt
    check("self turn: the person asking about the Host reads the thread's record (the control)",
          lim_person == SELF_TRAIL_TURNS, lim_person)
    check("self turn: the SAME words as a platform-started turn's brief read the normal trail",
          lim_woken == TOOL_TRAIL_MAX_TURNS and lim_woken != SELF_TRAIL_TURNS, lim_woken)
    check("self turn: a saved request is the person's own words, so it still decides",
          lim_saved == SELF_TRAIL_TURNS, lim_saved)
    check("self turn: a turn not about the Host reads the normal trail",
          lim_plain == TOOL_TRAIL_MAX_TURNS, lim_plain)
    for label, h in (("person", person), ("platform-started", woken), ("saved request", saved_req)):
        blocks = [b.get("name") if isinstance(b, dict) else b
                  for b in (h.ledger().get("per_turn_blocks") or [])]
        check(f"self turn ({label}): no inventory rides the prompt; the fixed list answers it",
              h.calls and "Everything you hold" not in _sys_text(h) and "self_inventory" not in blocks,
              blocks)
    check("self turn: asking about the Host does not change the tool list",
          person.tools and plain.tools and person.tools[0] == plain.tools[0],
          (person.tools[:1], plain.tools[:1]))


def main() -> int:
    for part in (part_review_session, part_code_grep, part_gateway, part_loop, part_self_turn):
        try:
            part()
        except Exception as e:  # noqa: BLE001 — a part that cannot run is a failure
            import traceback
            traceback.print_exc()
            check(f"{part.__name__} ran", False, repr(e))
    print("\n[trims] default sizes on the fixtures (chars, before -> after):")
    for name, b, a in SIZES:
        print(f"[trims]   {name}: {b:,} -> {a:,}")
    print(f"[trims] {'all passed' if not failures else f'{len(failures)} FAIL(s): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
