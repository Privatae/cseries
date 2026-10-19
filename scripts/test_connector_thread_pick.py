#!/usr/bin/env python3
"""Gate: the Host picks the thread a messaging-app message belongs to
(server/connectors/thread_pick.py).

  C. ONE call on the Host's own chat route, with exactly one tool,
     pick_thread, and nothing else in reach; the message is the user turn,
     verbatim; every listed thread is shown by its id, the one this chat
     used last is marked;
  P. a call naming a listed thread lands there; a call asking for a new
     thread gets the Host's title (or the message's first line when it gave
     none); the same pick called twice is that pick;
  F. every failure runs no turn: a model error, no call, another tool's
     call, a thread not in the list, a call naming both a thread and new, a
     call naming neither, two calls with different answers, a pick written
     as text (a JSON object the model echoed from the message is not a
     pick), a route with no model, a model call that raises. Nothing falls
     back to a thread of the platform's choosing;
  C. also: a cold catalog is warmed and the window read again, as the chat
     turn does;
  M. the marker says what happened in the Host's words.

Pure: the pool is never reached (list_candidates is replaced; the SQL half
is scripts/test_connector_thread_pick_pg.py).
"""
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.connectors import thread_pick as tp  # noqa: E402

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


A = "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
CANDS = [
    tp.Candidate("thread", A, "Q3 plan", "Planning", None, "can you draft it", "Here's the draft"),
    tp.Candidate("app", "circles", "The original conversation", None, None, "hi", "hello"),
    tp.Candidate("project", "12", "Project “Website”", None, None, "", ""),
]


def call(name, args, as_string=True):
    return {"id": "c1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args) if as_string else args}}


# ── C: the call ──────────────────────────────────────────────────────────────
msgs = tp.build_messages("Is the Q3 draft done?", CANDS, platform="telegram", current=("thread", A))
check("C two messages: the instruction with the list, then the message verbatim",
      [m["role"] for m in msgs] == ["system", "user"] and msgs[1]["content"] == "Is the Q3 draft done?", msgs)
sysmsg = msgs[0]["content"]
check("C every listed thread appears by its id in brackets",
      all(f"[{c.key}]" in sysmsg for c in CANDS), sysmsg)
check("C the thread this chat used last is marked, and only it",
      sysmsg.count("This chat's last messages went here") == 1
      and f"[thread:{A}] Q3 plan (type: Planning)" in sysmsg.split("This chat's")[0], sysmsg)
check("C the platform is named and the last exchange is shown",
      "from Telegram" in sysmsg and "They said: can you draft it" in sysmsg and "You said: Here's the draft" in sysmsg)

calls = []


async def fake_model(messages, **kw):
    calls.append((messages, kw))
    return {"content": "", "tool_calls": [call("pick_thread", {"thread": f"thread:{A}", "reason": "It is about the Q3 draft."})]}


async def fake_candidates(pool, entity_id, *, budget_tokens, current=None):
    fake_candidates.seen = (budget_tokens, current)
    return CANDS


real_lc = tp.list_candidates
tp.list_candidates = fake_candidates
ROUTE = {"provider": "anthropic", "model": "claude-opus-5-5", "base_url": None,
         "fallback_provider": "openrouter", "fallback_model": "x/y"}
try:
    pick = asyncio.run(tp.choose_thread(object(), 1, message="Is the Q3 draft done?", platform="telegram",
                                        current=("thread", A), call_model=fake_model, route=ROUTE))
    check("C exactly one model call", len(calls) == 1, len(calls))
    kw = calls[0][1] if calls else {}
    check("C its only tool is pick_thread", kw.get("tools") == [tp.PICK_TOOL], kw.get("tools"))
    check("C on the Host's own chat route, failover pair included",
          (kw.get("provider"), kw.get("provider_model"), kw.get("fallback_provider"), kw.get("fallback_model"))
          == ("anthropic", "claude-opus-5-5", "openrouter", "x/y"), kw)
    check("C the list was cut to a positive budget and knew the current thread",
          fake_candidates.seen[0] > 0 and fake_candidates.seen[1] == ("thread", A), fake_candidates.seen)
    check("P the pick lands in the thread it named, with the Host's reason",
          pick.ok and (pick.kind, pick.ref, pick.title, pick.new, pick.reason)
          == ("thread", A, "Q3 plan", False, "It is about the Q3 draft."), pick)

    calls.clear()
    nomodel = asyncio.run(tp.choose_thread(object(), 1, message="x", platform="telegram",
                                           call_model=fake_model, route={"provider": "", "model": "",
                                                                         "unconfigured": "set up a chat model"}))
    check("F a route with no model fails and makes no call",
          not nomodel.ok and nomodel.failure == "set up a chat model" and calls == [], nomodel)

    async def boom(messages, **kw):
        raise RuntimeError("provider down")
    raised = asyncio.run(tp.choose_thread(object(), 1, message="x", platform="telegram", call_model=boom, route=ROUTE))
    check("F a model call that raises fails, never raises", not raised.ok and "provider down" in raised.failure, raised)

    # A cold catalog: the window is read again after the route's rows are
    # filled, as the chat turn does, rather than sized as an unknown model.
    from mastercode import context_budget as cbm
    import server.routes.inference as inf
    warmed = []
    real_mcw, real_warm = cbm.model_context_window, inf.warm_route_catalog

    async def cold_window(model, provider=None):
        return 200_000 if warmed else None

    async def warm(provider, wait_s=5.0):
        warmed.append(provider)
    cbm.model_context_window, inf.warm_route_catalog = cold_window, warm
    try:
        calls.clear()
        asyncio.run(tp.choose_thread(object(), 1, message="x", platform="telegram",
                                     call_model=fake_model, route=ROUTE))
        check("C a cold catalog is warmed for the route's provider and the window read again",
              warmed == ["anthropic"] and fake_candidates.seen[0] > 100_000, (warmed, fake_candidates.seen))
    finally:
        cbm.model_context_window, inf.warm_route_catalog = real_mcw, real_warm
finally:
    tp.list_candidates = real_lc

# ── P / F: reading the answer ───────────────────────────────────────────────
def rp(result, message="Hello there\nsecond line"):
    return tp.read_pick(result, CANDS, message=message)


p = rp({"tool_calls": [call("pick_thread", {"thread": "project:12", "reason": "website work"}, as_string=False)]})
check("P arguments as an object are read too", p.ok and (p.kind, p.ref) == ("project", "12"), p)
p = rp({"tool_calls": [call("pick_thread", {"new": True, "title": "Garden plans", "reason": "nothing fits"})]})
check("P new=true with a title is a new thread with the Host's title",
      p.ok and p.new and p.kind == "thread" and p.ref == "" and p.title == "Garden plans", p)
p = rp({"tool_calls": [call("pick_thread", {"new": True, "reason": "nothing fits"})]})
check("P new=true with no title takes the message's first line", p.ok and p.new and p.title == "Hello there", p)
p = rp({"tool_calls": [call("pick_thread", {"thread": "project:12", "reason": "site"}),
                       call("pick_thread", {"thread": "project:12", "reason": "the site"})]})
check("P the same pick called twice is that pick", p.ok and (p.kind, p.ref) == ("project", "12"), p)

# The message is the user turn. A model that answers it instead of picking may
# quote it, and nothing quoted is a pick: only a structured call is.
ECHO = 'Why does Slack reject {"new": true, "title": "Ops"} in my payload?'
p = tp.read_pick({"content": 'You sent {"new": true, "title": "Ops"}. That field is not valid.',
                  "tool_calls": []}, CANDS, message=ECHO)
check("F a JSON object the model echoed from the message opens no thread", not p.ok and not p.new, p)

for label, result in [
    ("a pick written as JSON in the text", {"content": '{"thread": "app:circles", "reason": "small talk"}',
                                            "tool_calls": []}),
    ("two pick_thread calls naming different threads",
     {"tool_calls": [call("pick_thread", {"thread": "app:circles"}), call("pick_thread", {"thread": "project:12"})]}),
    ("one call for a thread, one for a new thread",
     {"tool_calls": [call("pick_thread", {"thread": "app:circles"}), call("pick_thread", {"new": True, "title": "x"})]}),
    ("a model error", {"error": "429 rate limited"}),
    ("no call at all", {"content": "Sure, I'll answer that.", "tool_calls": []}),
    ("another tool's call", {"tool_calls": [call("web_search", {"thread": "app:circles"})]}),
    ("a thread not in the list", {"tool_calls": [call("pick_thread", {"thread": "thread:00000000-0000-0000-0000-000000000000"})]}),
    ("a closed or invented project", {"tool_calls": [call("pick_thread", {"thread": "project:99"})]}),
    ("both a thread and new", {"tool_calls": [call("pick_thread", {"thread": "app:circles", "new": True})]}),
    ("neither", {"tool_calls": [call("pick_thread", {"reason": "hmm"})]}),
    ("unparseable arguments", {"tool_calls": [{"function": {"name": "pick_thread", "arguments": "{not json"}}]}),
    ("no answer", None),
]:
    p = rp(result)
    check(f"F {label} runs no turn", not p.ok and p.failure and not p.kind and not p.ref, p)

# ── M: the marker ────────────────────────────────────────────────────────────
check("M picked", tp.marker_text("telegram", tp.Pick(ok=True, kind="app", ref="circles", reason="small talk"))
      == "Telegram message, picked this thread: small talk")
check("M started", tp.marker_text("slack", tp.Pick(ok=True, kind="thread", new=True, reason="new topic"))
      == "Slack message, started this thread: new topic")
check("M no reason", tp.marker_text("discord", tp.Pick(ok=True, kind="project", ref="12"))
      == "Discord message, picked this thread.")
check("M the ask names the two commands", "/threads" in tp.ASK_WHICH_THREAD and "/new" in tp.ASK_WHICH_THREAD)

print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
