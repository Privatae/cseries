#!/usr/bin/env python3
"""A search hit the Host already has in front of it is not sent again
(runtime/search_dedup.py, 2026-10-04: context is worth saving).

The incident: a Host ran ten local_grep calls in one turn, each with a
DIFFERENT pattern, whose pages came back almost the same — the exact-argument
dedup never fired, and every repeated hit was paid for again as input.

Part A drives the real EntityRuntime.send_message with a scripted model and
stubbed tools (the harness shape of scripts/test_turn_cut_output.py) and reads
what the MODEL was sent each round. Part B pins the two safety rules on
TurnSearchSeen directly: a hit whose message the context trim removed is shown
again, and a result the transcript wall cut is never recorded as seen.

No pod, no DB, no model. Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import json
import logging
import os
import sys

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
        print(f"[search-dedup] FAIL {name} {extra}")


from mastercode.runtime import session as S  # noqa: E402
from mastercode.runtime.search_dedup import TurnSearchSeen  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402
from server.routes import inference as INF  # noqa: E402

INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
INF._CATALOG_CACHE["exp"] = 1e18


class Pool:
    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


def _tc(name, args, i):
    return {"id": f"call_{name}_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _round(name, args, i):
    return {"content": "", "tool_calls": [_tc(name, args, i)],
            "raw_message": {"content": "", "tool_calls": [_tc(name, args, i)]}}


def _round_many(calls):
    """One completion carrying several tool calls (a parallel round)."""
    tcs = [_tc(name, args, i) for name, args, i in calls]
    return {"content": "", "tool_calls": tcs,
            "raw_message": {"content": "", "tool_calls": tcs}}


class Harness:
    def __init__(self, script, tool_results):
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.tool_results = {k: list(v) for k, v in tool_results.items()}
        self.pool = Pool()

    async def model(self, messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        self.calls.append([dict(m) for m in msgs])
        r = self.script.pop(0) if self.script else {"content": "(the script ran out)"}
        cb = kw.get("stream_cb")
        if cb and r.get("content"):
            await cb(r["content"])
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1,
                "tool_calls": None, **r}

    async def run(self):
        S._pending_nudges.clear()
        S._turn_events.clear()
        S._task_holds.clear()
        saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE.get("ceigas_domain_controller"),
                 S._execute_meta_tool, S._build_tool_schemas)

        async def route(pool, eid, role, thread_override=None, **kw):
            return {"provider": "anthropic", "model": "claude-opus-5-5"}

        async def meta(name, args, **kw):
            return self.tool_results[name].pop(0)

        async def schemas(pool, eid, interaction_context=None, **kw):
            return [{"type": "function", "function": P_REG[n]["function"]}
                    for n in ("local_grep", "search_my_code")]

        S._call_entity_model = self.model
        PG.call_model = self.model
        IC.resolve_role_route = route
        S._AVAILABLE["ceigas_domain_controller"] = None
        S._execute_meta_tool = meta
        S._build_tool_schemas = schemas
        rt = S.EntityRuntime(self.pool)

        async def ctx(entity_id, **kw):
            return S.EntitySession(
                session_id="sess-dedup", entity_id=entity_id,
                entity={"id": entity_id, "name": "Sage", "behavioral_params": {},
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context="project:251")
        rt._build_chat_context_transient = ctx
        self.artifacts = None

        async def post(*a, **kw):
            # What the turn hands the artifact stash (thread_consolidator
            # .stash_tool_artifacts): these become user_context rows.
            self.artifacts = kw.get("tool_artifacts")
        rt._post_message_tasks = post
        try:
            self.out = await rt.send_message(entity_id=9003, user_message="find the hook",
                                             project_id=251, app_id="workbench")
            await asyncio.sleep(0.05)
        finally:
            (S._call_entity_model, PG.call_model, IC.resolve_role_route,
             S._AVAILABLE["ceigas_domain_controller"],
             S._execute_meta_tool, S._build_tool_schemas) = saved
        return self


def sent(h, i):
    """The newest tool message the model was sent on call i, parsed."""
    tm = [m for m in (h.calls[i] if len(h.calls) > i else []) if m.get("role") == "tool"]
    if not tm:
        return {}
    try:
        return json.loads(tm[-1]["content"])
    except (TypeError, ValueError):
        return {"_raw": tm[-1]["content"]}


def hit(path, line, content):
    return {"path": path, "line": line, "content": content}


def grep(pattern, hits, total=None, next_offset=None):
    r = {"pattern": pattern, "path": "/var/lib/cseries", "matches": hits,
         "truncated": next_offset is not None, "total_hits_returned": len(hits),
         "total_hits": total if total is not None else len(hits), "offset": 0}
    if next_offset is not None:
        r["next_offset"] = next_offset
    return {"status": "ok", "result": r}


P = "/var/lib/cseries/desktop-render/pins/249/index.html"
H1 = hit(P, 388, "memory line: textContent from the bridge state")
H2 = hit(P, 389, "memory line: hidden when the bridge state is empty")
H3 = hit("/var/lib/cseries/apps/x/skill.md", 12, "the bridge's memory line, e.g. 1.2G of 4G")
H4 = hit("/var/lib/cseries/apps/x/skill.md", 40, "processes are not part of the bridge")
H2_EDITED = hit(P, 389, "memory line: always shown (edited mid-turn)")

print("A. through the real Host loop")
h = asyncio.run(Harness(
    [_round("local_grep", {"pattern": "memory"}, 1),
     _round("local_grep", {"pattern": "bridge|memory", "is_regex": True}, 2),
     _round("local_grep", {"pattern": "memory line"}, 3),
     _round("local_grep", {"pattern": "shown"}, 4),
     _round("local_grep", {"pattern": "memory"}, 5),
     {"content": "The shell hands over the memory line only."}],
    {"local_grep": [grep("memory", [H1, H2, H3]),
                    grep("bridge|memory", [H1, H2, H3, H4], total=9, next_offset=4),
                    grep("memory line", [H1, H2], total=2),
                    grep("shown", [H2_EDITED]),
                    grep("memory", [H1, H2, H3])]}).run())

r1 = sent(h, 1).get("result", {})
check("A1 the first search goes to the model whole",
      len(r1.get("matches", [])) == 3 and "already_shown" not in r1, r1)

r2 = sent(h, 2).get("result", {})
check("A2 a DIFFERENT pattern's page drops the hits already above (H1-H3), keeps the new one",
      [m["line"] for m in r2.get("matches", [])] == [40], r2.get("matches"))
check("A3 ...and says how many it dropped and from which round",
      r2.get("already_shown", {}).get("count") == 3
      and r2.get("already_shown", {}).get("rounds") == [1], r2.get("already_shown"))
check("A4 ...keeping the true total and the paging cursor",
      r2.get("total_hits") == 9 and r2.get("next_offset") == 4
      and r2.get("total_hits_returned") == 1, r2)

r3 = sent(h, 3)
r3i = r3.get("result", {})
check("A5 a page of only seen hits is not refused: status ok, empty list, 'found nothing new'",
      r3.get("status") == "ok" and r3i.get("matches") == []
      and "nothing new" in r3i.get("already_shown", {}).get("note", ""), r3)

r4 = sent(h, 4).get("result", {})
check("A6 the same file:line with different text (edited mid-turn) is a new hit",
      len(r4.get("matches", [])) == 1 and "already_shown" not in r4, r4)

r5 = sent(h, 5)
check("A7 the exact-repeat note still fires on an identical call + identical raw result",
      r5.get("unchanged_since_round") == 1, list(r5)[:5])
check("A8 ...and the repeated hits inside it are not sent a second time",
      r5.get("result", {}).get("result", {}).get("matches") == [], r5.get("result"))

# The artifact stash keeps the WHOLE result. Those rows are recalled in later
# turns, where "already returned this turn and still above" is false and the
# dropped hits are in some other row, if anywhere.
_arts = [json.loads(a["content"]) for a in (h.artifacts or []) if a.get("tool") == "local_grep"]
_a2 = (_arts[1] if len(_arts) > 1 else {}).get("result", {})
check("A8b the artifact stash gets the whole result, not the thinned copy the model got",
      len(_arts) == 5 and [m["line"] for m in _a2.get("matches", [])] == [388, 389, 12, 40]
      and "already_shown" not in _a2, _a2)

# Two searches in ONE completion: the second runs after the first's result is
# appended, so its overlap is dropped and both results reach the next request.
h = asyncio.run(Harness(
    [_round_many([("local_grep", {"pattern": "memory"}, 1),
                  ("local_grep", {"pattern": "bridge"}, 2)]),
     {"content": "done"}],
    {"local_grep": [grep("memory", [H1, H2]), grep("bridge", [H2, H3])]}).run())
_tm = [json.loads(m["content"]) for m in (h.calls[1] if len(h.calls) > 1 else [])
       if m.get("role") == "tool"]
check("A8c parallel calls: the first result goes whole, the second drops the hit the first carried",
      len(_tm) == 2
      and [m["line"] for m in _tm[0]["result"]["matches"]] == [388, 389]
      and [m["line"] for m in _tm[1]["result"]["matches"]] == [12]
      and _tm[1]["result"].get("already_shown", {}).get("rounds") == [1], _tm)


# search_my_code: keyed by project + file + chunk
def chunk(f, i, c):
    return {"file_path": f, "chunk_index": i, "content": c}


C1, C2 = chunk("src/a.py", 0, "def a(): ..."), chunk("src/b.py", 3, "def b(): ...")
h = asyncio.run(Harness(
    [_round("search_my_code", {"project": "cseries-desk", "query": "memory line"}, 1),
     _round("search_my_code", {"project": "cseries-desk", "query": "memory bar"}, 2),
     _round("search_my_code", {"project": "other", "query": "memory bar"}, 3),
     {"content": "done"}],
    {"search_my_code": [
        {"status": "ok", "result": {"project": "cseries-desk", "count": 1, "results": [C1]}},
        {"status": "ok", "result": {"project": "cseries-desk", "count": 2, "results": [C1, C2]}},
        {"status": "ok", "result": {"project": "other", "count": 1, "results": [C1]}}]}).run())
s2 = sent(h, 2).get("result", {})
check("A9 search_my_code drops a chunk already above and fixes `count`",
      [c["chunk_index"] for c in s2.get("results", [])] == [3] and s2.get("count") == 1, s2)
s3 = sent(h, 3).get("result", {})
check("A10 the same file and chunk in ANOTHER project is a different hit",
      len(s3.get("results", [])) == 1 and "already_shown" not in s3, s3)

print("B. the safety rules")
seen = TurnSearchSeen()
first = grep("memory", [H1, H2])
msg = {"role": "tool", "content": json.dumps(first)}
live = [{"role": "system"}, msg]
seen.record("local_grep", {}, first, msg["content"], msg, 1)
again = grep("memory|x", [H1, H2])
check("B1 while the carrying message is in context, its hits are dropped",
      seen.filter("local_grep", {}, again, live)["result"]["matches"] == [])
check("B2 once the context trim dropped that message, the hits are shown again",
      seen.filter("local_grep", {}, again, [{"role": "system"}]) is again)
shrunk = [{"role": "system"}, {"role": "tool", "content": "{\"truncated\": true}"}]
check("B3 ...and the same when the trim replaced it with a truncation envelope (a new object)",
      seen.filter("local_grep", {}, again, shrunk) is again)

seen2 = TurnSearchSeen()
cut_msg = {"role": "tool", "content": "{\"truncated\": true, \"result\": {}}"}
seen2.record("local_grep", {}, first, cut_msg["content"], cut_msg, 1)
check("B4 a result the transcript wall cut is never recorded as seen",
      seen2.filter("local_grep", {}, again, [cut_msg]) is again)

check("B5 the raw tool result is never mutated (the loop keeps it for its exact-repeat check)",
      [m["line"] for m in again["result"]["matches"]] == [388, 389])
check("B6 a failed search and any other tool pass through untouched",
      seen.filter("local_grep", {}, {"status": "error", "result": "x"}, live) == {"status": "error", "result": "x"}
      and seen.filter("workspace_file", {}, first, live) is first)

# B7/B8 run the REAL pre-send trim (context_budget, the function
# session._call_with_overflow_retry writes back with msgs[:] = ...), not a
# hand-built list. The identity rule is only sound while the trim never edits
# a message in place; a trim that rewrote the carrier's content in place would
# leave the same object in the list and these would fail.
from mastercode import context_budget as CB  # noqa: E402

big = grep("memory", [hit(P, 500 + i, "x" * 400) for i in range(40)])


def _loop_list():
    carrier = {"role": "tool", "tool_call_id": "c1", "content": json.dumps(big)}
    msgs = [{"role": "system", "content": "IDENTITY: host."},
            {"role": "user", "content": "find the hook"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "local_grep", "arguments": "{}"}}]},
            carrier,
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c2", "type": "function",
                 "function": {"name": "workspace_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "{\"status\": \"ok\"}"}]
    return carrier, msgs


again_big = grep("memory|y", list(big["result"]["matches"]))
carrier, msgs = _loop_list()
seen3 = TurnSearchSeen()
seen3.record("local_grep", {}, big, carrier["content"], carrier, 1)
_raw_content = carrier["content"]
check("B7a (setup) with the carrier in the list, every hit is dropped",
      seen3.filter("local_grep", {}, again_big, msgs)["result"]["matches"] == [])
_t = CB.trim_messages_to_window(msgs, 2_000)
check("B7 the real trim's step 3 replaced the carrier with an envelope -> hits shown again",
      _t is not msgs and carrier["content"] == _raw_content
      and not any(m is carrier for m in _t)
      and any("truncated" in str(m.get("content")) for m in _t if m.get("role") == "tool")
      and seen3.filter("local_grep", {}, again_big, _t) is again_big,
      [str(m.get("content"))[:60] for m in _t])
carrier, msgs = _loop_list()
seen4 = TurnSearchSeen()
seen4.record("local_grep", {}, big, carrier["content"], carrier, 1)
_t = CB.trim_with_hysteresis(msgs, 300)
check("B8 the real hysteresis trim at a tight target drops the carrier's group -> hits shown again",
      _t is not msgs and not any(m is carrier for m in _t)
      and seen4.filter("local_grep", {}, again_big, _t) is again_big,
      [m.get("role") for m in _t])

if FAILS:
    print(f"[search-dedup] {len(FAILS)} FAIL")
    sys.exit(1)
print("[search-dedup] ALL OK")
