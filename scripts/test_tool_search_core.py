#!/usr/bin/env python3
"""Search mode's core.

The fixed core per surface, the history notes, the prefetch, the exits and
the mode flag (runtime/tool_core.py, wired in session.py). One section per
check:

  1. across a turn with searches, the tool list's hash is identical on every
     call except the documented exits (the spend stop keeps the list with
     tool_choice "none"; only `escalate` alone differs)
  2. a tool called in turn N appears in turn N+1's history once, with
     identical bytes in turn N+2 — the bytes find_tools returns
  3. note characters are counted in the history size
  4. a note leaves with its turn in the walk and in the pre-send trim, and
     never starts the window
  5. notes render in PII mode
  6. a mid-history note goes to Kimi K3 as the system message Kimi K3
     already accepts after history on every turn
  7. the core: its contents per surface and its extras (CORE_MAX itself is
     in scripts/test_registry_slim.py, beside WIRE_MAX)

Container-free: APP_ROOT=<repo> python3 scripts/test_tool_search_core.py.
No pod, no DB, no model: real turns through EntityRuntime.send_message with
a scripted model and a stub pool, the way scripts/test_tool_search_doors.py
runs them.
"""
import asyncio
import copy
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

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
        print(f"[tool-core] FAIL {name} {extra}")


from mastercode.runtime import session as S  # noqa: E402
from mastercode.runtime import tool_core as TC  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from mastercode import memory_engine as ME  # noqa: E402
from mastercode import context_budget as CB  # noqa: E402
from mastercode import pii_queue as PQ  # noqa: E402
from mastercode.runtime import direct_ops as DO  # noqa: E402
from mastercode.runtime import tool_doors as TD  # noqa: E402
from mastercode.runtime import turn_allowance as TA  # noqa: E402
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402


def _wire(name):
    s = copy.deepcopy(P_REG[name])
    s.pop("_meta", None)
    return s


CATALOG_NAMES = ("web_search", "read_url", "research", "local_grep", "workspace_file",
                 "contacts_manage", "system.restart_app", "revoke_nac_rule", "build",
                 "review.compare", "capabilities")
CLOSE = {"topic": "tool search", "handoff": {"next_step": "nothing"}}
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)


def row(i, role, content, calls=None, *, meta=None):
    md = {"role": role, "source_context": {"kind": "app", "ref": "circles"}, "app_id": "circles"}
    if calls is not None:
        md["tool_calls"] = calls
    md.update(meta or {})
    return {"id": i, "content": content, "metadata": md,
            "created_at": T0 + timedelta(minutes=i), "project_id": None, "source_session": "s"}


def chip(tool, **k):
    return {"tool": tool, "status": "ok", **k}


class Pool:
    """Answers the reads a turn makes: its history rows (recall_recent_turns),
    the tool trail (recall_tool_trail), the core's use counts (tool_core's
    query); everything else is empty."""

    def __init__(self, rows=(), use_rows=()):
        self.rows = list(rows)
        self.use_rows = list(use_rows)
        self.use_queries = 0

    async def fetch(self, q, *a):
        if "SELECT id, content, metadata, created_at" in q:
            return list(reversed(self.rows))
        if "SELECT created_at, metadata" in q and "tool_calls" in q:
            return [{"created_at": r["created_at"], "metadata": r["metadata"]}
                    for r in reversed(self.rows)
                    if r["metadata"].get("role") == "assistant" and r["metadata"].get("tool_calls")]
        if "AS chips" in q:
            self.use_queries += 1
            return list(self.use_rows)
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


def tc(name, args, i):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class Harness:
    """One turn through EntityRuntime.send_message with a scripted model.
    Records every tools= list and tool_choice the model was sent, every
    message list, and the turn's ledger."""

    def __init__(self, script, *, behavioral=None, interaction_context="chat",
                 rows=(), use_rows=(), catalog=CATALOG_NAMES, stop_after=None):
        self.script = list(script)
        self.behavioral = behavioral if behavioral is not None else {"tool_surface": "search"}
        self.interaction_context = interaction_context
        self.catalog = [_wire(n) for n in catalog]
        self.pool = Pool(rows, use_rows)
        self.stop_after = stop_after
        self.tools: list[list[dict]] = []
        self.choice: list = []
        self.calls: list[list[dict]] = []
        self.direct: list = []
        self.meta_calls: list = []
        self.out = None

    async def model(self, messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        self.calls.append([dict(m) for m in msgs])
        self.tools.append(copy.deepcopy(kw.get("tools") or []))
        self.choice.append(kw.get("tool_choice"))
        r = self.script.pop(0) if self.script else {
            "content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 999)]}
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1, "tool_calls": None, **r}

    def results(self) -> dict:
        out = {}
        for msgs in self.calls:
            for m in msgs:
                if m.get("role") == "tool" and m.get("tool_call_id"):
                    try:
                        out[m["tool_call_id"]] = json.loads(m.get("content") or "null")
                    except (TypeError, ValueError):
                        out[m["tool_call_id"]] = m.get("content")
        return out

    def names(self, i=0) -> list[str]:
        return [(t.get("function") or {}).get("name") for t in self.tools[i]]

    def run(self, message="Look into it for me."):
        saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                 S._execute_meta_tool, S._build_tool_schemas, DO.run_direct_op,
                 TA.TurnSpend.check)

        async def route(pool, eid, role, thread_override=None, **k):
            return {"provider": "anthropic", "model": "fake-model"}

        async def run_close_turn(pool, sess, args, *, prompt_console=None):
            return {"status": "ok", "result": "recorded"}

        async def meta(name, args, **k):
            self.meta_calls.append((name, copy.deepcopy(args)))
            # orient answers with the groups it opened, as the real one does,
            # so a groups-mode loop would load their schemas mid-turn.
            if name == "orient":
                return {"status": "ok", "result": {"groups": [
                    {"group": g, "tools": []} for g in (args or {}).get("open") or []]}}
            return {"status": "ok", "result": f"{name} ran"}

        async def direct(name, args, **k):
            self.direct.append((name, copy.deepcopy(args)))
            return {"status": "ok", "result": f"{name} operated"}

        async def schemas(pool, eid, interaction_context=None, **k):
            return copy.deepcopy(self.catalog)

        _orig_check = TA.TurnSpend.check
        h = self

        def check_spend(spend, new_tok, reserve):
            if h.stop_after is not None and len(h.tools) >= h.stop_after:
                return "stop", "STOP: the turn's allowance is spent."
            return _orig_check(spend, new_tok, reserve)

        S._call_entity_model = self.model
        PG.call_model = self.model
        IC.resolve_role_route = route
        S._AVAILABLE["ceigas_domain_controller"] = None
        S._run_close_turn = run_close_turn
        S._execute_meta_tool = meta
        S._build_tool_schemas = schemas
        DO.run_direct_op = direct
        TA.TurnSpend.check = check_spend
        TC._USE_CACHE.clear()
        rt = S.EntityRuntime(self.pool)

        async def ctx(entity_id, **k):
            return S.EntitySession(
                session_id="sess-core", entity_id=entity_id,
                entity={"id": entity_id, "name": "Host", "behavioral_params": self.behavioral,
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context=self.interaction_context)
        rt._build_chat_context_transient = ctx
        try:
            self.out = asyncio.run(rt.send_message(entity_id=7, user_message=message))
        finally:
            (S._call_entity_model, PG.call_model, IC.resolve_role_route,
             S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
             S._execute_meta_tool, S._build_tool_schemas, DO.run_direct_op,
             TA.TurnSpend.check) = saved
        return self

    def ledger(self) -> dict:
        return ((self.out or {}).get("context") or {}).get("ledger") or {}


def notes_in(msgs) -> list[dict]:
    return [m for m in msgs if TC.is_note(m)]


def note_entries(note: dict) -> list[dict]:
    return json.loads(note["content"].split("\n", 1)[1])


# ── 1. one list for the whole turn ─────────────────────────────────────────
print("1. the tool list is byte-identical on every call but the documented exits")
SCRIPT1 = [
    {"content": "", "tool_calls": [tc("find_tools", {"query": "search the web"}, 1)]},
    {"content": "", "tool_calls": [tc("read", {"tool": "web_search", "args": {"query": "pgvector"}}, 2)]},
    # An orient named anyway: in groups mode its open loads schemas into the
    # list mid-turn, the change that rewrites the provider's cache.
    {"content": "", "tool_calls": [tc("orient", {"open": ["research", "comms"]}, 3)]},
    {"content": "", "tool_calls": [tc("operate", {"intent": "Restarting it.", "tool": "system.restart_app",
                                                  "args": {"app_id": "x"}}, 4)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 5)]},
]
h1 = Harness(SCRIPT1).run(message="search the web for pgvector, then restart the app")
shas = [S._bytes_sha(t) for t in h1.tools]
check("a 5-call turn with a search, a door read, an orient and an operate: one hash on every call",
      len(shas) == 5 and len(set(shas)) == 1, shas)
h1o = Harness([{"content": "", "tool_calls": [tc("orient", {"open": ["files", "codespace"]}, 1)]},
               {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 2)]}],
              behavioral={}).run(message="hello there")
check("a Host with no tool_surface setting runs the same fixed list (groups mode is gone)",
      len({S._bytes_sha(t) for t in h1o.tools}) == 1 and h1o.names(0) == h1.names(0),
      [S._bytes_sha(t) for t in h1o.tools])
check("orient is not in the list",
      "orient" not in h1.names(0), h1.names(0))
check("the core: the six verbs, find_tools, read — then this surface's doors",
      h1.names(0)[:8] == ["dispatch", "operate", "recall", "agent_status", "escalate", "close_turn",
                          "find_tools", "read"]
      and S._bytes_sha(h1.tools[0][:8]) == S._bytes_sha(
          S._build_host_orchestration_tools() + S._build_search_door_tools()))
check("Circles' list is the verbs and the doors only: its build doors are found, not listed",
      h1.names(0)[8:] == [], h1.names(0)[8:])
check("no tool_choice is sent on an ordinary call", set(h1.choice) == {None}, h1.choice)
check("the ledger says search, with no group or orient record",
      h1.ledger().get("tools", {}).get("surface") == "search"
      and "groups" not in h1.ledger().get("tools", {})
      and "orient_opens" not in h1.ledger().get("tools", {}), h1.ledger().get("tools"))
_esc = next(t for t in h1.tools[0] if t["function"]["name"] == "escalate")
_esc_p = TD._strip_descriptions(_esc["function"]["parameters"])
_esc_item = _esc_p["properties"]["options"]["items"]
check("escalate is trimmed (<= 2,400 characters) and its contract is the registry's, unchanged",
      len(json.dumps(_esc)) <= 2400
      and _esc_p["required"] == ["sitrep", "options"]
      and set(_esc_p["properties"]) == {"sitrep", "options", "project_id", "phase_id", "action_class",
                                        "circle_id", "requester", "context", "priority", "source_ref"}
      and _esc_item["required"] == ["id", "label"]
      and _esc_item["properties"]["credential_request"]["required"] == ["name", "cred_type"]
      and _esc_item["properties"]["credential_request"]["properties"]["cred_type"]["enum"] == ["api_key", "opaque"]
      and "never whether to go on" in _esc["function"]["description"]
      and "NEW name" in _esc["function"]["description"], len(json.dumps(_esc)))
_dispatch = h1.tools[0][0]["function"]
check("dispatch names find_tools, never orient",
      "orient" not in json.dumps(_dispatch) and "find_tools" in json.dumps(_dispatch))

# The spend stop: the same tools, no call allowed.
h1s = Harness([
    {"content": "", "tool_calls": [tc("find_tools", {"query": "web"}, 1)]},
    {"content": "", "tool_calls": [tc("read", {"tool": "web_search", "args": {"query": "q"}}, 2)]},
    {"content": "Here is what I found."},
], stop_after=2).run(message="search the web")
check("the spend stop keeps the same list (one hash) and sends tool_choice 'none'",
      len(h1s.tools) >= 3 and len({S._bytes_sha(t) for t in h1s.tools}) == 1
      and h1s.choice[-1] == "none" and set(h1s.choice[:-1]) == {None},
      ([S._bytes_sha(t) for t in h1s.tools], h1s.choice))
h1g = Harness([
    {"content": "", "tool_calls": [tc("web_search", {"query": "q"}, 1)]},
    {"content": "Here is what I found."},
], behavioral={}, stop_after=1).run(message="search the web")
check("a Host with no setting stops the same way (one list, tool_choice 'none')",
      len({S._bytes_sha(t) for t in h1g.tools}) == 1 and h1g.choice[-1] == "none",
      ([S._bytes_sha(t) for t in h1g.tools], h1g.choice))

# The forced final call past the loop (the backstop): escalate alone is the
# one documented exit.
_bs = TA.backstop_rounds
TA.backstop_rounds = lambda *a, **k: 2
try:
    h1b = Harness([
        {"content": "", "tool_calls": [tc("find_tools", {"query": "web"}, 1)]},
        {"content": "", "tool_calls": [tc("find_tools", {"query": "mail"}, 2)]},
        {"content": "", "tool_calls": [tc("find_tools", {"query": "files"}, 3)]},
        {"content": "Summary."},
    ]).run(message="look around")
finally:
    TA.backstop_rounds = _bs
_ex = [i for i, t in enumerate(h1b.tools) if [x["function"]["name"] for x in t] == ["escalate"]]
check("at the backstop's forced final call only `escalate` alone differs; every other call has one hash",
      _ex == [len(h1b.tools) - 1]
      and len({S._bytes_sha(t) for i, t in enumerate(h1b.tools) if i not in _ex}) == 1,
      [[x["function"]["name"] for x in t][:3] for t in h1b.tools])

# ── 2. a called tool rides the next turns' history, once, the same bytes ──
print("2. a tool called in turn N is in turn N+1's history once, identical in N+2")
ROWS_N = [row(1, "user", "check the web for pgvector"),
          row(2, "assistant", "Searched: pgvector is a Postgres extension.",
              [chip("find_tools"), chip("read", read_tool="web_search"), chip("close_turn")])]
h2a = Harness([{"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 1)]}],
              rows=ROWS_N).run(message="thanks, and read that page")
n_a = notes_in(h2a.calls[0])
check("turn N+1's history carries ONE note, right after turn N's reply, naming web_search",
      len(n_a) == 1 and [e["name"] for e in note_entries(n_a[0])] == ["web_search"]
      and str(h2a.calls[0][h2a.calls[0].index(n_a[0]) - 1].get("content", "")).startswith("Searched"),
      [str(m.get("content", ""))[:40] for m in h2a.calls[0]])
_found = Harness([{"content": "", "tool_calls": [tc("find_tools", {"query": "web_search"}, 1)]},
                  {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 2)]}]).run()
_f_entry = next(e for e in _found.results()["call_1"]["result"]["tools"] if e["name"] == "web_search")
check("the note's entry is byte-for-byte what find_tools returns for the tool",
      bool(n_a) and json.dumps(note_entries(n_a[0])[0], default=str) == json.dumps(_f_entry, default=str))
ROWS_N2 = ROWS_N + [row(3, "user", "thanks, and read that page"),
                    row(4, "assistant", "Read it.",
                        [chip("read", read_tool="web_search"), chip("operate", operate_tool="revoke_nac_rule")])]
h2b = Harness([{"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 1)]}],
              rows=ROWS_N2).run(message="good")
n_b = notes_in(h2b.calls[0])
_all_b = [e["name"] for n in n_b for e in note_entries(n)]
check("turn N+2: web_search still once, in turn N's note with identical bytes; turn N+1's note has "
      "only what is new (revoke_nac_rule, an operate tool)",
      _all_b.count("web_search") == 1 and len(n_b) == 2 and bool(n_a) and n_b[0]["content"] == n_a[0]["content"]
      and [e["name"] for e in note_entries(n_b[1])] == ["revoke_nac_rule"]
      and note_entries(n_b[1])[0]["door"] == "operate", _all_b)
check("no prefetch rides the turn (removed with the brief-prompt slice)",
      "prefetch" not in h2b.ledger())

# ── 3. note characters count in the history size ──────────────────────────
print("3. note characters are counted in the history size")
_hist = h2b.ledger().get("history") or {}
_turn_chars = sum(len(r["content"]) for r in ROWS_N2)
_note_chars = sum(len(n["content"]) for n in n_b)
check("history.chars = the turns' text + their notes; history.notes says what rode",
      _hist.get("chars") == _turn_chars + _note_chars
      and (_hist.get("notes") or {}).get("chars") == _note_chars
      and (_hist.get("notes") or {}).get("tools") == 2, (_hist, _turn_chars, _note_chars))


class _Cat:
    """A stand-in catalog for the pure checks: every name renders, at a size."""

    def __init__(self, sizes):
        self.sizes = sizes
        self.searchable = set(sizes)
        self.entries = dict.fromkeys(sizes, True)

    def render(self, name):
        return {"name": name, "pad": "x" * self.sizes[name]}


_cat = _Cat({"a": 300, "b": 200, "c": 100})
_nr = TC.NoteRenderer(_cat, core_names=set())
_turns = [{"role": "user", "content": "u" * 100},
          {"role": "assistant", "content": "x" * 100, "tools_called": ["a", "b"]},
          {"role": "user", "content": "u" * 100},
          {"role": "assistant", "content": "y" * 100, "tools_called": ["a", "c"]}]
_w = TC.window_chars(_turns, _nr)
_plan = TC.note_plan(_turns, _nr)
check("the walk's sizing: text + each note once (a and b on turn 1, only c on turn 2)",
      _w == 400 + len(_plan[1][0]) + len(_plan[3][0]) and _plan[1][1] == ["a", "b"] and _plan[3][1] == ["c"],
      (_w, _plan))

# ── 4. a note leaves with its turn, and never starts the window ───────────
print("4. a note leaves with its turn (walk, pre-send trim) and never starts the window")
# One character under: the chunked cut takes the first exchange (the person's
# message, then the reply with its note) — the newest exchange stays.
_kept, _wt = TC.fit_history(_turns, max_hist=_w - 101, max_msgs=100, block=1, note_fn=_nr)
_p2 = TC.note_plan(_kept, _nr)
check("the walk cuts turn 1 with its note; `a`, called again later, renders on the turn that remains",
      _kept[0] is _turns[2] and _p2[1] is not None and _p2[1][1] == ["a", "c"]
      and _wt == TC.window_chars(_kept, _nr) and _wt <= _w - 101, (_kept, _p2, _wt))
_lead = [{"role": "assistant", "content": "", "tools_called": ["a"]},
         {"role": "user", "content": "hi"},
         {"role": "assistant", "content": "ok", "tools_called": ["a"]}]
_hm = ME.history_messages(_lead, tool_note=_nr)
check("a turn with no words of its own renders no note at the front; its tool renders at the next "
      "appearance instead",
      not TC.is_note(_hm[0]) and _hm[0]["role"] == "user" and TC.is_note(_hm[-1])
      and [e["name"] for e in note_entries(_hm[-1])] == ["a"], _hm)
check("the window never opens on a note, whatever the rows",
      not any(TC.is_note(m) for m in ME.history_messages(
          [{"role": "assistant", "content": "", "tools_called": ["a"]}], tool_note=_nr))
      and ME.history_messages([{"role": "assistant", "content": "", "platform_note": ["budget"],
                                "tools_called": ["b"]},
                               {"role": "user", "content": "hi"}], tool_note=_nr)[0]["role"] == "user")
_note = {"role": "system", "content": TC.note_text([{"name": "a", "pad": "x" * 4000}])}
_msgs = [{"role": "system", "content": "sys"},
         {"role": "user", "content": "old question " * 300},
         {"role": "assistant", "content": "old answer " * 300},
         _note,
         {"role": "user", "content": "newer " * 50},
         {"role": "assistant", "content": "newer answer " * 50},
         {"role": "system", "content": "per-turn block"},
         {"role": "user", "content": "now?"}]
_tr = CB.trim_messages_to_window(_msgs, 900, protect=[_msgs[-1]])
check("the pre-send trim drops an evicted turn's note with it, and no note is left at the front",
      _msgs[2] not in _tr and _note not in _tr and _msgs[-1] in _tr and not TC.is_note(_tr[1]),
      [(m["role"], m["content"][:20]) for m in _tr])
_tr0 = CB.trim_messages_to_window([m for m in _msgs if m is not _note], 900, protect=[_msgs[-1]])
check("without notes the trim is what it was (the same messages kept)",
      [m["content"] for m in _tr] == [m["content"] for m in _tr0])
# Through the real walk: a long thread at the legacy caps (8,000 characters).
_long = [row(10 + i, "user" if i % 2 == 0 else "assistant", ("q " if i % 2 == 0 else "a ") * 900,
             None if i % 2 == 0 else [chip("read", read_tool="web_search" if i == 1 else "local_grep")])
         for i in range(8)]
h4 = Harness([{"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 1)]}],
             rows=_long).run(message="next")
_n4 = notes_in(h4.calls[0])
_h4 = [m for m in h4.calls[0][1:-1] if m.get("role") in ("user", "assistant")]
_hc = (h4.ledger().get("history") or {}).get("chars")
check("a real walk at the 8,000-character cap: the cut turn's tool (web_search) is gone, the kept "
      "turns' tool (local_grep) renders once, and turns + notes fit the cap",
      all("web_search" not in n["content"] for n in _n4) and len(_n4) == 1
      and _hc is not None and _hc <= 8000
      and _hc == sum(len(m["content"]) for m in _h4) + sum(len(n["content"]) for n in _n4),
      ((h4.ledger().get("history") or {}), len(_n4), len(_h4)))

# ── 5. notes in PII mode ───────────────────────────────────────────────────
print("5. notes render in PII mode")
_md_search = {"role": "assistant", "tool_surface": "search", "tool_calls": [
    {"tool": "read", "read_tool": "web_search", "args_summary": "Jane's tax file", "status": "ok",
     "digest": "found 3 results for Jane", "next_step": "email Jane"},
    {"tool": "read", "read_tool": "this is a sentence, not a tool name"}]}
_md_groups = {"role": "assistant", "tool_calls": [{"tool": "web_search", "args_summary": "Jane"}]}
_s = PQ.scrub_metadata(_md_search)
check("a search-mode row keeps its tool NAMES in PII mode, nothing else from a chip",
      _s.get("tool_calls") == [{"tool": "read", "read_tool": "web_search"}, {"tool": "read"}]
      and _s.get("tool_surface") == "search" and "Jane" not in json.dumps(_s), _s)
check("a groups-mode row's chips are dropped whole in PII mode, as before",
      "tool_calls" not in PQ.scrub_metadata(_md_groups))
# A person's turn on a search_platform Host runs groups mode; the wake-up
# after it runs search and renders notes for what that turn called. Its row
# names its surface so the scrub keeps the names.
_s2 = PQ.scrub_metadata({**_md_groups, "tool_surface": "groups"})
check("a search_platform Host's groups-mode row keeps its tool NAMES in PII mode",
      _s2.get("tool_calls") == [{"tool": "web_search"}] and "Jane" not in json.dumps(_s2), _s2)
import mastercode.memory_engine as _ME  # noqa: E402
_written: list = []
_orig_wtr = _ME.write_turn_row


def _cap_wtr(pool, eid, sid, role, content, **k):
    # Recorded at the call, before session.py hands the coroutine to a task.
    _written.append((role, dict(k.get("tags") or {})))

    async def _n():
        return None
    return _n()


def _row_surface(behavioral):
    _written.clear()
    Harness([{"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 1)]}],
            behavioral=behavioral).run()
    return [t.get("tool_surface", "-") for r, t in _written if r == "assistant"]


_ME.write_turn_row = _cap_wtr
try:
    _sp = _row_surface({"tool_surface": "search_platform"})
    _none = _row_surface({})
    _srch = _row_surface({"tool_surface": "search"})
finally:
    _ME.write_turn_row = _orig_wtr
check("every orchestrating Host's turn row says 'search', whatever the old setting said",
      _sp == ["search"] and _none == ["search"] and _srch == ["search"], (_sp, _none, _srch))
_pii_rows = [row(1, "user", "[PERSON_1] asked"),
             {**row(2, "assistant", "[ORG_1] done."),
              "metadata": PQ.scrub_metadata({**row(2, "assistant", "x")["metadata"], **_md_search})}]
h5 = Harness([{"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 1)]}],
             rows=_pii_rows).run(message="more")
_n5 = notes_in(h5.calls[0])
check("the next turn's history renders the note from the scrubbed row",
      len(_n5) == 1 and [e["name"] for e in note_entries(_n5[0])] == ["web_search"], _n5)

# ── 6. Kimi K3 ─────────────────────────────────────────────────────────────
print("6. a mid-history note reaches Kimi K3 as the system message it accepts after history")


class _Resp:
    status = 200
    headers: dict = {}

    async def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1}}

    async def text(self):
        return json.dumps(await self.json())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


SENT: list[dict] = []


class _Sess:
    def __init__(self, *a, **k):
        pass

    def post(self, endpoint, headers=None, json=None):
        SENT.append(copy.deepcopy(json))
        return _Resp()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class _Cred:
    secret = "sk-test"
    source = "key"


async def _cred(*a, **k):
    return _Cred()


async def _nolog(*a, **k):
    return None

import server.vault.providers as VP  # noqa: E402

_saved6 = (PG.aiohttp.ClientSession, VP.resolve_for_inference, PG._log_inference)
PG.aiohttp.ClientSession, VP.resolve_for_inference, PG._log_inference = _Sess, _cred, _nolog
_kmsgs = [{"role": "system", "content": "static"},
          {"role": "user", "content": "check the web"},
          {"role": "assistant", "content": "Searched."},
          {"role": "system", "content": TC.note_text([{"name": "web_search"}])},
          {"role": "user", "content": "thanks"},
          {"role": "assistant", "content": "ok"},
          {"role": "system", "content": "## This Turn\nper-turn block"},
          {"role": "user", "content": "now?"}]
_ktools = S._build_host_orchestration_tools() + S._build_search_door_tools()
try:
    asyncio.run(PG._call_remote(_kmsgs, "k3", max_tokens=100, tools=_ktools, provider="kimi",
                                base_url="https://api.kimi.com/coding/v1", session_id="s"))
    asyncio.run(PG._call_remote(_kmsgs, "k3", max_tokens=100, tools=_ktools, provider="kimi",
                                base_url="https://api.kimi.com/coding/v1", session_id="s",
                                tool_choice="none"))
finally:
    PG.aiohttp.ClientSession, VP.resolve_for_inference, PG._log_inference = _saved6
_roles = [m["role"] for m in (SENT[0]["messages"] if SENT else [])]
check("on the Kimi wire the note is a system message between two history turns, in place — the "
      "role and position of the per-turn block Kimi K3 is sent after history on every turn",
      bool(SENT) and _roles == ["system", "user", "assistant", "system", "user", "assistant", "system", "user"]
      and SENT[0]["messages"][3]["content"].startswith(TC.NOTE_HEAD), (_roles, SENT[:1]))
check("the spend stop on the Kimi wire: the same tools, tool_choice 'none'",
      len(SENT) == 2 and SENT[1]["tools"] == SENT[0]["tools"] and SENT[0]["tool_choice"] == "auto"
      and SENT[1]["tool_choice"] == "none", [s.get("tool_choice") for s in SENT])
_ap = PG._anthropic_messages_payload(_kmsgs, "claude", 0.3, 100, _ktools, tool_choice="none")
check("on the Anthropic wire: the same tools, tool_choice {type: none}; a note rides as a user block",
      _ap["tool_choice"] == {"type": "none"} and len(_ap["tools"]) == len(_ktools)
      and any(TC.NOTE_HEAD in b.get("text", "") for m in _ap["messages"] for b in m["content"]),
      _ap.get("tool_choice"))
check("on the Anthropic wire with no tool_choice: auto, as before",
      PG._anthropic_messages_payload(_kmsgs, "claude", 0.3, 100, _ktools)["tool_choice"] == {"type": "auto"})

# ── 7. the core and its extras ────────────────────────────────────────────
print("7. the core: its contents per surface, and its extras fixed per kernel")
# CORE_EXTRAS is 0 since the brief-prompt slice (none ride the operator Host's list); the
# machinery that picks them is still exercised here at the old setting.
check("no extras ride by default", TC.CORE_EXTRAS == 0, TC.CORE_EXTRAS)
_saved_extras = TC.CORE_EXTRAS
TC.CORE_EXTRAS = 5
_use = [{"kind": "app", "ref": "circles", "app_id": "circles",
         "chips": [chip("read", read_tool="web_search"), chip("operate", operate_tool="revoke_nac_rule")]},
        {"kind": "app", "ref": "circles", "app_id": "circles",
         "chips": json.dumps([chip("web_search"), chip("local_grep"),
                              chip("operate", operate_tool="workspace_file")])},
        {"kind": "project", "ref": "5", "app_id": "workbench", "chips": [chip("capabilities")]}]
h7 = Harness([{"content": "", "tool_calls": [tc("find_tools", {"query": "q"}, 1)]},
              {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 2)]}],
             use_rows=_use).run(message="hello")
check("the Circles core adds this Host's most-used tools a native call can run, most used first "
      "(web_search 2, then local_grep and workspace_file's read-only variant 1 each, by name); never an "
      "action (revoke_nac_rule), never a Workbench-only door",
      h7.names(0)[8:] == ["web_search", "local_grep", "workspace_file"]
      and h7.ledger()["tools"].get("core_extras") == ["web_search", "local_grep", "workspace_file"],
      h7.names(0))
check("the use counts are read once per kernel", h7.pool.use_queries == 1, h7.pool.use_queries)
_ws = next((t for t in h7.tools[0] if t["function"]["name"] == "workspace_file"), None)
_ws_ops = ((((_ws or {}).get("function") or {}).get("parameters") or {}).get("properties") or {}).get("op") or {}
check("a mixed tool joins the core as its read-only variant",
      bool(_ws_ops.get("enum")) and "write" not in _ws_ops["enum"] and "delete" not in _ws_ops["enum"], _ws_ops)
h7w = Harness([{"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 1)]}],
              interaction_context="project:5", use_rows=_use).run(message="hello")
check("the Workbench core carries the build door and its own extras",
      "build" in h7w.names(0) and h7w.names(0)[-1] == "capabilities", h7w.names(0))
check("the extras are counted only from rows written before the kernel started",
      "created_at < $3" in TC._USE_SQL and TC.KERNEL_START <= datetime.now(timezone.utc))
check("rank_by_use ties break by name, so the same rows give the same core",
      TC.rank_by_use([("chat", [chip("b"), chip("a")])]) == {"circles": [("a", 1), ("b", 1)]})
check("called_names: a door's target, once each, verbs and orient left out",
      TC.called_names([chip("operate", operate_tool="x"), chip("read", read_tool="y"), chip("x"),
                       chip("orient"), chip("dispatch"), chip("z")]) == ["x", "y", "z"])
check("row_interaction_context: a project is the Workbench, a thread or Circles is chat, another app is itself",
      [TC.row_interaction_context(k, r, a) for k, r, a in
       (("project", "5", "workbench"), ("thread", "t", "circles"), ("app", "circles", None),
        ("app", "notes", "notes"))] == ["project:5", "chat", "chat", "notes"])
# The prefetch, the map's group lines and the self inventory are gone: one
# fixed list, find_tools on demand.
check("tool_core has no prefetch", not hasattr(TC, "prefetch_block") and not hasattr(TC, "PREFETCH_HEAD"))
check("the lanes paragraph names find_tools, never orient",
      "`find_tools`" in S._build_orchestrator_prompt([])
      and "orient" not in S._build_orchestrator_prompt([]))
TC.CORE_EXTRAS = _saved_extras

# ── 7b. Circles finds its build doors; operate runs them ──────────────────
# (~3,900 characters off every Circles call). The Workbench is the build
# surface and keeps them listed.
print("7b. Circles: build doors found with find_tools and run through operate")
check("only Circles finds its build doors; the Workbench lists them",
      TC.build_doors_listed("project:5") and TC.build_doors_listed("workbench")
      and not TC.build_doors_listed("chat") and not TC.build_doors_listed("circles"))
h8 = Harness([
    {"content": "", "tool_calls": [tc("find_tools", {"name": "review.compare"}, 1)]},
    {"content": "", "tool_calls": [tc("operate", {"intent": "Comparing.", "tool": "review.compare",
                                                  "args": {}}, 2)]},
    {"content": "", "tool_calls": [tc("operate", {"intent": "Comparing.", "tool": "review.compare",
                                                  "args": {"session_id": "s1"}}, 3)]},
    {"content": "", "tool_calls": [tc("find_tools", {"query": "compare candidates"}, 4)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 5)]}]).run(message="compare them")
_r8 = h8.results()
check("no build door rides any Circles call, and the list is one hash all turn",
      all("review.compare" not in h8.names(i) for i in range(len(h8.tools)))
      and len({S._bytes_sha(t) for t in h8.tools}) == 1, h8.names(0))
_f8 = ((_r8.get("call_1") or {}).get("result") or {}).get("tools") or [{}]
check("find_tools(name=review.compare) gives its whole schema on the operate door",
      _f8[0].get("door") == "operate" and "schema" in _f8[0], _f8)
check("operate with arguments the schema refuses runs nothing and names the error",
      (_r8.get("call_2") or {}).get("reason") == "args_schema"
      and not any(n == "review.compare" and a == {} for n, a in h8.meta_calls), _r8.get("call_2"))
check("operate(tool=review.compare) runs through the host-own path, never run_direct_op",
      ("review.compare", {"session_id": "s1"}) in h8.meta_calls
      and not any(n == "review.compare" for n, _ in h8.direct), (h8.meta_calls, h8.direct))
_q8 = [t.get("name") for t in (((_r8.get("call_4") or {}).get("result") or {}).get("hits")
                               or ((_r8.get("call_4") or {}).get("result") or {}).get("tools") or [])]
check("a query finds the build door", "review.compare" in _q8, _r8.get("call_4"))
check("the tools block names them as run with operate",
      "run with operate(intent, tool, args)" in S._build_orchestrator_prompt(
          [_wire("review.compare")], build_doors_listed=False)
      and "never dispatched or operated" in S._build_orchestrator_prompt([_wire("review.compare")]))
h8w = Harness([
    {"content": "", "tool_calls": [tc("operate", {"intent": "Building.", "tool": "build",
                                                  "args": {"action": "run", "project_id": 5}}, 1)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 2)]}],
    interaction_context="project:5").run(message="build it")
check("the Workbench lists build compact, and operate(tool=build) still meets run_direct_op's wall",
      "build" in h8w.names(0)
      and next(t for t in h8w.tools[0] if t["function"]["name"] == "build")
      == TD.compact_schema(_wire("build"))
      and not any(n == "build" for n, _ in h8w.meta_calls), (h8w.names(0), h8w.meta_calls))

if FAILS:
    print(f"\ntool-core: {len(FAILS)} FAIL(s): {FAILS}")
    sys.exit(1)
print("\ntool-core: all checks pass")
