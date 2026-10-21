#!/usr/bin/env python3
"""Gate: a Workbench turn lives with the project its work touched, and a
project thread never spawns a second project.

Measured on one Host (projects 9228 → 9229): the ask was typed in a
stub project's thread; the host reached `create_project` through `operate`
(which runs registry ops with no surface gate by design), made project 9229
for the same work, and the carry-forward re-tagged five of 9228's turns into
9229. From then on every reply and every "yes" stayed in 9228 (filed by
window) while every build report landed in 9229 (filed by build) — one
conversation split across two threads for two hours, with no repair op.

Asserts, container-free (fake pool, real functions):
  1. decide_turn_home: reads never move a turn; one other project written
     moves it; two different ones is ambiguous (stay); the window's own
     project never moves; Circles (no window project) never moves;
     `operate`-wrapped writes count by their inner op; a refused write does
     not count.
  2. _exec_create_project refuses inside a project thread — through the
     origin_context `operate` passes AND the direct lane's
     interaction_context — names the project, and never INSERTs.
  3. create_project moves NO turn (project 417: the
     carry-forward took the request out of its Circles thread and left the
     reply): no UPDATE of entity_memory at all, the arc detector is gone,
     and the new project's thread gets one thread_marker turn pointing back
     to the conversation (title + link). The real-schema half is
     test_create_project_keeps_exchange_pg.py.
  4. adopt_thread_core with kind='project' scopes by the project_id column
     (never IS NULL), refuses self-adoption, and files the marker in the
     source project; kind='thread' takes the thread's transcript set and
     refuses the legacy sentinels; kind='app' ref='circles' takes exactly
     what the original conversation renders.
  5. The surface block carries only its own app's manifest: no offer to
     create a project, no create_project instruction on the Workbench, no
     admin ship recipe; the frame names "this project" in a
     project thread.
  6. rehome_turn awaits the user row, retags it to the target with the
     source recorded, carries what was learned from it, and leaves a marker
     in the source project (the carry on a real database:
     test_thread_choice_pg R).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, os.path.join(_ROOT, "brain")):
    if p not in sys.path:
        sys.path.insert(0, p)

fails: list[str] = []


def _check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


# ── 1. the decision ────────────────────────────────────────────────────────
from mastercode.runtime.turn_home import decide_turn_home  # noqa: E402


def _ta(tool, args=None, status="ok", digest=None, result=None):
    d = {"tool": tool, "args": args or {}, "result_status": status}
    if digest is not None:
        d["digest"] = digest
    if result is not None:
        d["result"] = result
    return d


_check(decide_turn_home(9228, [_ta("list_projects", {"search": "prep"}),
                              _ta("get_project", {"project_id": 9229})]) is None,
       "reads must never move a turn")
_check(decide_turn_home(9228, [_ta("build", {"action": "plan", "project_id": 9229})]) == 9229,
       "one other project written → the turn moves there")
_check(decide_turn_home(9228, [_ta("build", {"action": "plan", "project_id": 9228})]) is None,
       "writing the window's own project never moves the turn")
_check(decide_turn_home(9228, [_ta("build", {"action": "build", "project_id": 9229}),
                              _ta("project_phase", {"op": "add_phase", "project_id": 230})]) is None,
       "two different projects written is ambiguous → stay")
_check(decide_turn_home(None, [_ta("build", {"action": "plan", "project_id": 9229})]) is None,
       "Circles (no window project) never re-homes")
_check(decide_turn_home(9228, [_ta("operate",
                                  {"tool": "create_project", "args": {"name": "X"}, "intent": "…"},
                                  digest={"project_id": 9229})]) == 9229,
       "an operate-wrapped create_project counts by its digest")
_check(decide_turn_home(9228, [_ta("operate",
                                  {"tool": "create_project", "args": {"name": "X"}, "intent": "…"},
                                  status="denied", digest={"project_id": 9228})]) is None,
       "a refused write moves nothing")
_check(decide_turn_home(9228, [_ta("project_phase", {"op": "list", "project_id": 9229})]) is None,
       "project_phase op=list is a read")
_check(decide_turn_home(9228, [_ta("workspace_file", {"op": "read", "project_id": 9229, "path": "a"})]) is None
       and decide_turn_home(9228, [_ta("workspace_file", {"op": "write", "project_id": 9229, "path": "a"})]) == 9229,
       "workspace_file: read stays, write moves")
_check(decide_turn_home(9228, [_ta("build", {"action": "plan", "project_id": 9229}),
                              _ta("build", {"action": "build", "project_id": 9229})]) == 9229,
       "several writes to the same other project are one target")
_check(decide_turn_home(9228, [_ta("build", {"action": "stop", "project_id": 300})]) is None,
       "build action=stop is control of a run, not work in that project")
_check(decide_turn_home(9228, [_ta("build", {"action": "continue", "project_id": 9229,
                                            "message": "fix the header"})]) == 9229,
       "build action=continue is a seeded build pass → it counts")

# The record shape the chat loop really writes (session.py ta_entry: tool,
# args, result_status, error_detail, result_chars + the tool-trail digest),
# built with the real digest function — so a key rename on either side fails
# here, not in production.
from mastercode.runtime.tool_trail import digest_tool_action  # noqa: E402
import inspect as _inspect  # noqa: E402
import mastercode.runtime.session as _session_mod  # noqa: E402

_sess_src = _inspect.getsource(_session_mod)
_ta_block = _sess_src[_sess_src.index("ta_entry = {"):_sess_src.index("tool_actions.append(ta_entry)")]
for _k in ('"tool"', '"args"', '"result_status"'):
    _check(_k in _ta_block, f"session.py ta_entry no longer writes {_k} — turn_home reads it")
_check("digest_tool_action(func_name, func_args, tool_result)" in _ta_block,
       "session.py ta_entry no longer carries the tool-trail digest — turn_home reads digest.project_id")


def _real_entry(tool, args, result):
    e = {"tool": tool, "args": args,
         "result_status": result.get("status", "unknown"),
         "error_detail": result.get("result", "") if result.get("status") in ("error", "blocked", "denied") else "",
         "result_chars": None}
    e.update(digest_tool_action(tool, args, result))
    return e


_op_args = {"tool": "create_project", "args": {"name": "Fern LCSW Test Prep"},
            "intent": "Creating the project now."}
_op_res = {"status": "ok", "repo": None,
           "result": {"project_id": 9229, "name": "Fern LCSW Test Prep", "status": "active"}}
_real = _real_entry("operate", _op_args, _op_res)
_check(_real.get("digest", {}).get("project_id") == 9229,
       f"the tool-trail digest carries the created project_id for operate/create_project: {_real.get('digest')}")
_check(decide_turn_home(9228, [_real]) == 9229,
       "a real operate→create_project record re-homes by its digest")
_denied = _real_entry("operate", _op_args,
                      {"status": "denied", "result": "This thread already is project #9228", "project_id": 9228})
_check(decide_turn_home(9228, [_denied]) is None, "a real denied create moves nothing")
_real_build = _real_entry("build", {"action": "plan", "project_id": 9229, "intent": "x"},
                          {"status": "ok", "result": {"task_id": 351, "project_id": 9229}})
_check(decide_turn_home(9228, [_real_build]) == 9229, "a real build record re-homes by its args")


# ── a fake pool that records SQL ───────────────────────────────────────────
class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


class FakePool:
    def __init__(self, *, fetchrow=None, fetchval=None, fetch=None):
        self.calls: list[tuple[str, str, tuple]] = []
        self._fetchrow = fetchrow or {}
        self._fetchval = fetchval or {}
        self._fetch = fetch or []

    def _match(self, table, sql):
        for key, val in table.items():
            if key in sql:
                return val
        return None

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._match(self._fetchrow, sql)

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self._match(self._fetchval, sql)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if "slot_scope IS DISTINCT FROM" in sql:
            # memory_slots.resync_scopes after an adoption: no row's slot is
            # out of step in this fake.
            return []
        if "jsonb_array_elements_text(e->'from_turns')" in sql or "jsonb_array_elements_text((CASE" in sql:
            # circles_threads._split_records: no record names moved and
            # staying messages in this fake.
            return []
        if "metadata->>'turn_token' AS t" in sql:
            # circles_threads._moved_tokens: no moved row carries a turn
            # token in this fake (its rows name their turns by id).
            return []
        if "JOIN entity_memory n" in sql:
            # thread_choice.turn_rows: nobody sent words into the turn.
            return []
        if "AS in_moved" in sql:
            # circles_threads.carry_learned: no decision about the source.
            return []
        if "'rehomed'" in sql and "RETURNING id" in sql:
            # turn_home.rehome_turn's retag: every row it names is there.
            return [_Row(id=int(i)) for i in args[0]]
        return list(self._fetch)

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "UPDATE 1"

    @contextlib.asynccontextmanager
    async def acquire(self):
        # One connection of the pool, recording into the same calls.
        yield _FakeConn(self)

    def sql(self, verb=None):
        return [s for v, s, _ in self.calls if verb is None or v == verb]


class _FakeConn:
    def __init__(self, pool):
        self._pool = pool

    def __getattr__(self, name):
        return getattr(self._pool, name)

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield self


# ── 2. create_project refuses inside a project thread ──────────────────────
from mastercode.runtime.tools import _exec_create_project, _project_of_thread  # noqa: E402

_check(_project_of_thread("project:9228") == 9228 and _project_of_thread("chat") is None
       and _project_of_thread(None) is None and _project_of_thread("project:x") is None,
       "_project_of_thread parses only project:<int>")


async def _t2():
    pool = FakePool(fetchval={"SELECT name FROM projects": "Test Prep"})
    out = await _exec_create_project({"name": "Fern LCSW Test Prep"}, 3, pool,
                                     origin_context="project:9228")
    _check(out.get("status") == "denied", "create_project in a project thread must be denied")
    _check(out.get("project_id") == 9228 and "project #9228" in str(out.get("result"))
           and "Test Prep" in str(out.get("result")),
           "the refusal names the thread's project (id + name)")
    _check("project_id=9228" in str(out.get("result")),
           "the refusal says where to plan instead")
    # The Workbench is the book of pages: the rail is gone,
    # so a separate project starts from the book's Index.
    _check("rail" not in str(out.get("result")) and "Index" in str(out.get("result")),
           "the refusal sends the user to the Workbench's Index, never the deleted rail")
    _check(not any("INSERT INTO projects" in s for s in pool.sql()),
           "a refused create must not INSERT a project")
    # Circles (origin 'chat') is NOT refused by this gate — it proceeds to the
    # insert (which the fake pool answers with None → the insert-failed path).
    pool2 = FakePool(fetchrow={"INSERT INTO projects": _Row(id=231, name="From Circles",
                                                            status="active", created_at=None)})
    out2 = await _exec_create_project({"name": "From Circles"}, 3, pool2, origin_context="chat")
    _check(out2.get("status") != "denied", "Circles creation is not refused by the thread gate")
    _check(any("INSERT INTO projects" in s for s in pool2.sql()),
           "Circles creation reaches the INSERT")


asyncio.run(_t2())


# ── 3. create_project moves no turn; it leaves a marker back ───────────────
# The carry-forward is removed: it read every conversation's untagged rows
# and moved the request
# out of the turn that asked, leaving the reply behind (row 9190 to
# project 417, its reply 9192 in thread a46c9283…).
import json as _json  # noqa: E402
import mastercode.runtime.tools as _tools_mod  # noqa: E402
import server.routes.projects as _projects_mod  # noqa: E402
from server.home_origin import HOME as _home  # noqa: E402


async def _t3():
    _check(not hasattr(_tools_mod, "_detect_active_arc"),
           "the arc carry-forward detector is gone")
    _saved = (_projects_mod.ensure_project_dir, _projects_mod.ensure_project_repo)

    async def _no_repo(pool, pid, name):
        return {"repo": None, "reason": "test"}
    _projects_mod.ensure_project_dir = lambda pid: None
    _projects_mod.ensure_project_repo = _no_repo
    try:
        tid = "A46C9283-D9E5-452F-8032-C5F5418AD4BC"
        pool = FakePool(fetchrow={
            "INSERT INTO projects": _Row(id=417, name="Telegram connector", status="active",
                                         created_at=None),
            "FROM circles_threads": _Row(title="Telegram connector", topic=None)})
        out = await _exec_create_project({"name": "Telegram connector"}, 3, pool,
                                         origin_context="chat", thread_id=tid)
        _check(not any("UPDATE entity_memory" in s for s in pool.sql()),
               "create_project moves no turn row (no UPDATE of entity_memory)")
        ins = [a for v, s, a in pool.calls if v == "execute" and "INSERT INTO entity_memory" in s]
        _check(len(ins) == 1, f"exactly one marker turn is written: {len(ins)}")
        if ins:
            content, project_col, md = ins[0][1], ins[0][3], _json.loads(ins[0][4])
            link = f"{_home.origin}/?open=circles&view=thread:{tid.lower()}"
            _check(project_col == 417 and md.get("source_kind") == "thread_marker"
                   and md.get("source_context") == {"kind": "project", "ref": "417"}
                   and md.get("app_id") == "workbench",
                   f"the marker is a thread_marker in the NEW project's thread: {md}")
            _check((md.get("marker") or {}).get("started_from") == {"kind": "thread", "ref": tid.lower()}
                   and (md.get("marker") or {}).get("title") == "Telegram connector"
                   and (md.get("marker") or {}).get("link") == link
                   and "thread_id" not in (md.get("marker") or {}),
                   f"the marker names the conversation it came from (canonical id, title, link): {md}")
            _check("“Telegram connector”" in content and f"({link})" in content
                   and "nothing was moved" in content,
                   f"the marker says where it was started and links back: {content!r}")
        _check((out.get("result") or {}).get("started_from", {}).get("title") == "Telegram connector"
               and "carried_turns" not in (out.get("result") or {}),
               f"the result tells the Host where it was started, and claims no carried turns: {out}")
        # The original conversation: its fixed title, its own link.
        pool2 = FakePool(fetchrow={"INSERT INTO projects": _Row(id=418, name="Orig", status="active",
                                                                created_at=None)})
        out2 = await _exec_create_project({"name": "From the original"}, 3, pool2,
                                          origin_context="chat")
        sf = (out2.get("result") or {}).get("started_from") or {}
        _check(sf.get("kind") == "app" and sf.get("ref") == "circles"
               and sf.get("title") == "The original conversation"
               and str(sf.get("link") or "").endswith("/?open=circles&view=thread:original")
               and not any("UPDATE entity_memory" in s for s in pool2.sql()),
               f"from the original conversation: nothing moves, the marker links to it: {sf}")
    finally:
        _projects_mod.ensure_project_dir, _projects_mod.ensure_project_repo = _saved


asyncio.run(_t3())


# ── 3b. the Host is never told the conversation is carried ──────────────────
# The Circles brief the Host reads while active (system-apps/circles/app.yaml,
# do_when_active) said "The recent conversation will be carried into the
# project's chat history; when the user opens the Workbench the thread will
# already be there" — false once nothing moves, and the Host repeats it to the
# person. Comment lines are the changelog (history) and are not read.
# The Circles manifest has no coaching list; what the Host reads about
# create_project is the tool's own description.
_reg = open(os.path.join(_ROOT, "brain", "mastercode", "public_tool_registry.py"), encoding="utf-8").read()
_cp = _reg[_reg.index('"name": "create_project"'):][:1500]
_circles_live = [_cp]
_check("create_project" in _cp, "the create_project schema is where the Host reads it")
_check(not any(("carried into" in ln or "already be there" in ln) for ln in _circles_live),
       f"create_project promises the conversation is carried into the project: {_cp[:300]}")
_check(any("links back" in ln and ("stays" in ln or "never moved" in ln) for ln in _circles_live),
       f"create_project says the conversation stays and the project links back: {_cp[:300]}")


# ── 4. adopt_thread_core with a PROJECT source ─────────────────────────────
from server.routes.projects import adopt_thread_core  # noqa: E402
from mastercode.circles_threads import _all_moved  # noqa: E402

# How carry_learned takes a row learned in the moved turns: every turn the
# row names (its message rows' ids, its turns' tokens) among the moved ones.
_BY_TURNS = _all_moved("metadata", 2)
_check("metadata->'from_turns'" in _BY_TURNS and "metadata->'from_turn_tokens'" in _BY_TURNS
       and "<@ $2::jsonb" in _BY_TURNS,
       "a row is taken by the turns it names, by id and by token, all among the moved ones")


async def _t4():
    pool = FakePool(fetchrow={"SELECT id, name FROM projects": _Row(id=9229, name="Fern")},
                    fetchval={"SELECT 1 FROM projects": 1},
                    fetch=[_Row(id=1, source_session="s1"), _Row(id=2, source_session="s1")])
    res = await adopt_thread_core(pool, 3, 9229, "project", "9228")
    _check(res.get("moved_turns") == 2, "project-source adoption moves the turns")
    upd = [s for s in pool.sql("fetch") if "UPDATE entity_memory" in s]
    _check(len(upd) == 1 and "project_id = $2::int" in upd[0] and "IS NULL" not in upd[0],
           "project-source adoption scopes by the project_id column, never IS NULL")
    _check("thread_marker" in upd[0], "a source marker is never carried across")
    mem = [(s, a) for v, s, a in pool.calls if v == "execute" and "memory_type <> 'turn'" in s]
    _check(len(mem) == 1 and _BY_TURNS in mem[0][0]
           and mem[0][1][1] == "[1, 2]" and mem[0][1][5] is True
           and '"ref": "9228"' in mem[0][1][3] and mem[0][1][7] == "9228",
           "what was learned from the moved turns follows them out of the source PROJECT "
           "(circles_threads.carry_learned: by the turns, and the whole conversation moving)")
    _check("project_id" not in mem[0][0].split("WHERE")[0],
           "...and is never given a project tag (a row written in the project has none)")
    _check(any("slot_scope IS DISTINCT FROM" in s for s in pool.sql("fetch")),
           "moved memories take their slot scope with them (memory_slots.resync_scopes)")
    ins = [(s, a) for v, s, a in pool.calls if v == "execute" and "INSERT INTO entity_memory" in s]
    _check(len(ins) == 1 and "project_id" in ins[0][0] and 9228 in ins[0][1],
           "the marker is filed in the SOURCE project's thread")
    # Refusals.
    for ref, why in (("9229", "self"), ("abc", "non-int")):
        try:
            await adopt_thread_core(pool, 3, 9229, "project", ref)
            fails.append(f"project-source adoption must refuse {why} ref={ref!r}")
        except ValueError:
            pass
    # The app source path is unchanged: IS NULL + source_context match.
    pool2 = FakePool(fetchrow={"SELECT id, name FROM projects": _Row(id=9229, name="Fern")},
                     fetch=[])
    await adopt_thread_core(pool2, 3, 9229, "app", "workbench")
    upd2 = [s for s in pool2.sql("fetch") if "UPDATE entity_memory" in s]
    _check(len(upd2) == 1 and "project_id IS NULL" in upd2[0]
           and "source_context'->>'kind' = $2" in upd2[0],
           "app-source adoption keeps the untagged + source_context scope")
    # The thread source path (Circles threads): a real thread id
    # scopes by the exact thread descriptor, transcript-style (no t_valid_until
    # filter) and without a regenerate's rows — the same set Split moves.
    pool3 = FakePool(fetchrow={"SELECT id, name FROM projects": _Row(id=9229, name="Fern")},
                     fetch=[])
    await adopt_thread_core(pool3, 3, 9229, "thread",
                            "6f1c2a3e-9b8d-4c7a-a1e2-0f3b4c5d6e7f")
    upd3 = [s for s in pool3.sql("fetch") if "UPDATE entity_memory" in s]
    _check(len(upd3) == 1 and "source_context'->>'kind' = 'thread'" in upd3[0]
           and "t_valid_until" not in upd3[0]
           and "circles-replay" in upd3[0],
           "thread-source adoption takes the thread's transcript set "
           "(exact descriptor, superseded rows in, regenerate rows out)")
    # The legacy sentinels name the ORIGINAL conversation; kind='thread' must
    # refuse them rather than adopt the user's whole Circles history.
    for ref in ("original", "self", "", "SELF"):
        try:
            await adopt_thread_core(pool3, 3, 9229, "thread", ref)
            fails.append(f"thread-source adoption must refuse the legacy sentinel ref={ref!r}")
        except ValueError:
            pass
    upd3_after = [s for s in pool3.sql("fetch") if "UPDATE entity_memory" in s]
    _check(len(upd3_after) == 1,
           "a refused sentinel never reaches the UPDATE")
    # The original Circles conversation (kind='app', ref='circles') adopts the
    # rows it renders: the historic app_id-or-untagged clause, transcript
    # style, regenerate rows out — the same set Split moves off it.
    pool4 = FakePool(fetchrow={"SELECT id, name FROM projects": _Row(id=9229, name="Fern")},
                     fetch=[_Row(id=1, source_session="s1")])
    await adopt_thread_core(pool4, 3, 9229, "app", "Circles")
    upd4 = [s for s in pool4.sql("fetch") if "UPDATE entity_memory" in s]
    _check(len(upd4) == 1 and "metadata->>'app_id' IS NULL" in upd4[0]
           and "IS DISTINCT FROM 'thread'" in upd4[0]
           and "circles-replay" in upd4[0] and "t_valid_until" not in upd4[0],
           "adopting the original Circles conversation takes exactly what it "
           "renders (app_id-or-untagged, no thread rows, no regenerate rows, "
           "superseded rows in; ref matched case-insensitively)")
    mem4 = [(s, a) for v, s, a in pool4.calls
            if v == "execute" and "memory_type <> 'turn'" in s and "source_context' IS NULL" in s]
    _check(len(mem4) == 1 and mem4[0][1][1] == ["s1"] and "project_id" not in mem4[0][0],
           "knowledge derived from a pre-descriptor Circles session follows "
           "its turns into the project, with no project tag")
    carried4 = [a for v, s, a in pool4.calls
                if v == "execute" and _BY_TURNS in s and "memory_type <> 'turn'" in s]
    _check(len(carried4) == 1 and carried4[0][6] == "app" and carried4[0][7] == "circles",
           "the original conversation's learned rows follow it by its own descriptor (app:circles)")
    mem2 = [(s, a) for v, s, a in pool2.calls
            if v == "execute" and "source_context' IS NULL" in s]
    _check(not mem2,
           "every other app source never sweeps in rows that name no conversation")


asyncio.run(_t4())


# ── 5. the surface block ───────────────────────────────────────────────────
from mastercode.runtime.prompt_builder import _build_surface_role  # noqa: E402

_APPS = [
    {"id": "workbench", "name": "Workbench", "tier": "core",
     "prompt_behavior": {"what_it_is": "The build surface.",
                         "project_scoped": True, "user_path": None, "step_up": None}},
    {"id": "circles", "name": "Circles", "tier": "core",
     "prompt_behavior": {"what_it_is": "The chat surface.",
                         "project_scoped": False, "user_path": None, "step_up": None}},
]
wb = _build_surface_role("project:9228", license_tier="admin", installed_apps=_APPS) or ""
ci = _build_surface_role("chat", license_tier="admin", installed_apps=_APPS) or ""
# Brief prompt: the surface block carries the current app's own
# manifest and nothing else. "Creation is not a thread move" is held in code
# (section 2: create_project refuses in a project thread, Circles reaches the
# INSERT), so neither block carries a creation instruction that could read
# as a fresh commitment, and the admin ship recipe is gone with the rest of
# the coaching. "This project" is named once, by the frame.
_check("offer to create it" not in wb and "offer to create it" not in ci,
       "no surface block offers to create a project (Circles creates; the Workbench is refused in code)")
_check("create_project" not in wb,
       "the Workbench block carries no create_project instruction (the refusal is code, section 2)")
_check("call `create_project` (or reuse" not in wb and "How you actually ship a fix" not in wb
       and "How you actually ship a fix" not in ci,
       "the admin ship recipe is gone from both blocks")
_check("The build surface." in wb and "The chat surface." in ci
       and "The chat surface." not in wb and "The build surface." not in ci,
       "each surface block carries its own app's manifest, not another app's")
from mastercode.runtime.frame import render_frame  # noqa: E402
_fr = render_frame({"surface": "Workbench", "project": {"id": 9228, "name": "Test Prep"},
                    "user_turns_before": 0})
_fc = render_frame({"surface": "Circles", "user_turns_before": 0})
_check('"This project" means project_id=9228' in _fr and "build" in _fr,
       "in a project thread the frame names THIS project for every project tool call")
_check("This project" not in _fc, "a Circles turn names no project (the control)")


# ── 6. rehome_turn ─────────────────────────────────────────────────────────
from mastercode.runtime.turn_home import rehome_turn, marker_text  # noqa: E402


async def _t6():
    pool = FakePool(fetchrow={"SELECT name FROM projects": _Row(name="Fern LCSW Test Prep")})

    async def _user_row():
        return 8191
    task = asyncio.create_task(_user_row())
    res = await rehome_turn(pool, 3, from_project=9228, to_project=9229,
                            user_row_task=task, session_id="s", turn_token="t6")
    _check(res["moved_user_row"] is True and res["to_name"] == "Fern LCSW Test Prep",
           f"rehome moves the user row and names the target: {res}")
    # The retag returns the rows it moved (the message, and the words sent
    # into the turn, thread_choice.turn_rows: none here).
    upd = [(s, a) for v, s, a in pool.calls if v == "fetch" and "UPDATE entity_memory" in s
           and "'rehomed'" in s]
    _check(len(upd) == 1 and upd[0][1][0] == [8191] and upd[0][1][1] == 9229
           and '"ref": "9228"' in upd[0][1][3],
           "the user row is retagged to the target with the source recorded")
    carried = [a for v, s, a in pool.calls if v == "execute" and _BY_TURNS in s
               and "memory_type <> 'turn'" in s]
    _check(len(carried) == 1 and carried[0][1] == "[8191]" and '"ref": "9229"' in carried[0][2],
           "what was learned from the user row is carried to the target (thread_choice._carry_learned)")
    ins = [(s, a) for v, s, a in pool.calls if v == "execute" and "INSERT INTO entity_memory" in s]
    _check(len(ins) == 1 and 9228 in ins[0][1] and marker_text(9229, "Fern LCSW Test Prep") in ins[0][1],
           "the marker lands in the SOURCE project with the target named")
    _check(upd[0][1][5] == 9228 and "AND project_id = $6" in upd[0][0],
           "the retag is fenced to a row still in the source project")
    # No user task and no turn token to find the row by: nothing moves and
    # NO marker lands — half a move is the split.
    pool2 = FakePool(fetchrow={"SELECT name FROM projects": _Row(name="T")})
    res2 = await rehome_turn(pool2, 3, from_project=1, to_project=2, user_row_task=None, session_id="s")
    _check(res2["moved_user_row"] is False and res2["marker"] is False,
           "with no user row to move, nothing moves and no marker lands")
    _check(not any("INSERT INTO entity_memory" in s for s in pool2.sql("execute")),
           "no marker INSERT when the user row did not move")


asyncio.run(_t6())


# ── 6b. session.py only follows a move that happened ───────────────────────
_blk = _sess_src[_sess_src.index("_rehome_res = None"):_sess_src.index('"event": "thread_moved"')]
_check('if _rehome_res and _rehome_res.get("moved_user_row"):' in _blk,
       "session.py gates the assistant row, the frame and the scope on moved_user_row")
_check('sess.interaction_context = f"project:{_turn_home}"' in _blk,
       "a completed move re-scopes the turn's post-tasks (consolidation, artifacts)")
_check(_blk.index('_rehome_res.get("moved_user_row")') < _blk.index("write_turn_row("),
       "the user row moves BEFORE the assistant row is written")
_check(decide_turn_home(9228, [_ta("build", {"action": "Plan ", "project_id": 9229})]) == 9229
       and decide_turn_home(9228, [_ta("build", {"project_id": 9229})]) is None,
       "the build action is normalized like _exec_build reads it; a missing action is no write")


# ── 7. the client's local marker is the kernel's marker, word for word ─────
# mergeHistory drops a local echo only when a server row with the same role
# and text arrives; a drifted string would render the marker twice.
_hook = os.path.join(_ROOT, "workbench-web", "src", "hooks", "use-workbench-chat.ts")
try:
    _hook_src = open(_hook, encoding="utf-8").read()
    _py = marker_text(9229, "Fern")
    _head, _tail = _py.split("project #9229 “Fern”")
    # The TS builds the same sentence from two literals joined at "Nothing";
    # every fragment must appear verbatim.
    _frags = [_head.strip()] + [f.strip() for f in _tail.split("Nothing") if f.strip()]
    _frags.append("Nothing was deleted;")
    _missing = [f for f in _frags if f not in _hook_src]
    _check(not _missing,
           f"use-workbench-chat.ts movedMarkerText must equal turn_home.marker_text; missing {_missing}")
except FileNotFoundError:
    fails.append(f"missing {_hook}")

if fails:
    print("test_turn_home: FAIL")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("test_turn_home: ok (decision, creation gate, no carry-forward + marker back, project/thread/circles-source adoption, "
      "surface block, rehome)")
