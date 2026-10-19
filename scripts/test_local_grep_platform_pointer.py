#!/usr/bin/env python3
"""local_grep names the door to the platform source — for a Host that holds it.

A Host grepped its own codespace ten times for the shell's
source. local_grep's description said the platform source is not visible there
and named nothing else; code_grep was not loaded that turn. A Host holding the
platform-source tools (the infra.codebase gate in _build_tool_schemas) now
reads, in local_grep's own description, which tools search that source and how
to load them. A Host without them is told nothing about tools it cannot call.

Builds the real tool list with a stub pool. No pod, no DB. Exit 0 = pass.
"""
import asyncio
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from mastercode.runtime import tools as T  # noqa: E402
from mastercode.runtime.triage import _BRIDGE_TOOL_SCHEMAS  # noqa: E402

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[grep-pointer] FAIL {name} {extra}")


class _Pool:
    def __init__(self, codebase_admin):
        self.codebase_admin = codebase_admin

    async def fetchval(self, sql, *a):
        if "infra.codebase" in sql:
            return 1 if self.codebase_admin else None
        return None

    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None


def _desc(schemas, name):
    return next((s["function"]["description"] for s in schemas
                 if s["function"]["name"] == name), None)


with_grant = asyncio.run(T._build_tool_schemas(_Pool(True), 3))
without = asyncio.run(T._build_tool_schemas(_Pool(False), 55))

d_with, d_without = _desc(with_grant, "local_grep"), _desc(without, "local_grep")
check("both Hosts get local_grep", d_with is not None and d_without is not None)
check("a Host holding the platform-source tools is told code_grep / code_read and the door that runs them",
      d_with and "code_grep" in d_with and "code_read" in d_with
      and "read(tool='code_grep'" in d_with and "orient" not in d_with, d_with)
check("...and it does hold them (the pointer names tools in its own list)",
      _desc(with_grant, "code_grep") is not None and _desc(with_grant, "code_read") is not None)
check("a Host without them is told nothing about them",
      d_without and "code_grep" not in d_without and "platform_source" not in d_without, d_without)
check("the registry's own schema is untouched (the pointer is per build, on a copy)",
      "code_grep" not in T.PUBLIC_TOOL_SCHEMAS["local_grep"]["function"]["description"])
check("the group the pointer names is the one the code tools are filed under",
      _BRIDGE_TOOL_SCHEMAS["code_grep"]["_meta"]["category"] == "platform_source")

BASE = T.PUBLIC_TOOL_SCHEMAS["local_grep"]["function"]["description"]

# A surface that hides a code tool: the gate holds, the tool is not in the list,
# so the pointer must not name it. Keyed on the built list, not the gate alone.
_real_surface_allows = T._surface_allows
T._surface_allows = lambda schema, surface: (
    schema["function"]["name"] != "code_grep" and _real_surface_allows(schema, surface))
try:
    hidden = asyncio.run(T._build_tool_schemas(_Pool(True), 3))
finally:
    T._surface_allows = _real_surface_allows
check("a surface without code_grep gets no pointer, though the gate holds",
      _desc(hidden, "code_grep") is None and _desc(hidden, "local_grep") == BASE,
      _desc(hidden, "local_grep"))

# A list narrowed below the build (a typed turn's groups, an agent's scope):
# the pointer comes back out when the narrowed list lacks a tool it names, on a
# copy — the caller's schemas are never mutated.
_dupp = getattr(T, "drop_unheld_platform_pointer", lambda s: list(s))
narrowed_in = [s for s in with_grant if s["function"]["name"] != "code_read"]
narrowed = _dupp(narrowed_in)
check("a narrowed list without code_read reads local_grep's base description",
      _desc(narrowed, "local_grep") == BASE, _desc(narrowed, "local_grep"))
check("...and the list it was given still carries the pointer (copied, not mutated)",
      "read(tool='code_grep'" in (_desc(narrowed_in, "local_grep") or ""))
check("a list that keeps every tool the pointer names keeps it",
      _desc(_dupp(with_grant), "local_grep") == d_with)


# A dispatched agent: the real run_scoped_agent over the real tool build, the
# model scripted. What reaches call_model as tools= is what the agent reads.
def _agent_tools(scope):
    from mastercode.runtime import scoped_agent as sa
    import mastercode.model_backends as mb
    seen = {}

    async def fake_call_model(**k):
        seen.setdefault("tools", k.get("tools"))
        return {"content": "done", "tool_calls": None, "raw_message": {},
                "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def fake_route(pool, eid, role):
        return {"provider": "openrouter", "model": "m", "base_url": None,
                "fallback_provider": None, "fallback_model": None}

    async def no_local(*a, **k):
        return None

    saved = (sa.call_model, sa.resolve_role_route, mb.match_local_model)
    sa.call_model, sa.resolve_role_route, mb.match_local_model = fake_call_model, fake_route, no_local
    try:
        out = asyncio.run(sa.run_scoped_agent(
            _Pool(True), entity_id=9003, task_prompt="t", tool_scope=scope,
            grant=["read_context"], session_id=None, max_rounds=1))
    finally:
        sa.call_model, sa.resolve_role_route, mb.match_local_model = saved
    return seen.get("tools") or [], out


a_tools, a_out = _agent_tools(["local_grep"])
check("an agent scoped to local_grep alone is not pointed at code tools it lacks",
      _desc(a_tools, "local_grep") == BASE, f"{_desc(a_tools, 'local_grep')!r} out={a_out}")
a_tools, a_out = _agent_tools(["local_grep", "search_my_code", "code_grep", "code_read", "code_tree"])
check("an agent scoped to local_grep, search_my_code and the code tools keeps the pointer",
      _desc(a_tools, "local_grep") == d_with, f"{_desc(a_tools, 'local_grep')!r} out={a_out}")

# Search first, then grep, then read — the order
# Claude Code works the same source in. The pointer names search_my_code before
# code_grep, and search_my_code itself says the platform repos are in its index,
# only for a Host that holds the code tools.
check("the pointer sends the Host to search_my_code before code_grep and code_read",
      d_with and 0 <= d_with.find("search_my_code") < d_with.find("code_grep") < d_with.find("code_read"),
      d_with)
S_BASE = T.PUBLIC_TOOL_SCHEMAS["search_my_code"]["function"]["description"]
s_with, s_without = _desc(with_grant, "search_my_code"), _desc(without, "search_my_code")
check("search_my_code tells a Host holding the code tools the platform repos are in its index",
      s_with and s_with.startswith(S_BASE) and "platform repos" in s_with[len(S_BASE):]
      and "code_grep" in s_with[len(S_BASE):], s_with)
check("...and a Host without them reads the base description", s_without == S_BASE, s_without)
narrowed = _dupp([s for s in with_grant if s["function"]["name"] != "code_tree"])
check("a narrowed list without code_tree takes both pointers back out",
      _desc(narrowed, "local_grep") == BASE and _desc(narrowed, "search_my_code") == S_BASE)
narrowed = _dupp([s for s in with_grant if s["function"]["name"] != "search_my_code"])
check("a narrowed list without search_my_code loses local_grep's pointer (it names search_my_code)",
      _desc(narrowed, "local_grep") == BASE, _desc(narrowed, "local_grep"))

if FAILS:
    print(f"[grep-pointer] {len(FAILS)} FAIL")
    sys.exit(1)
print("[grep-pointer] ALL OK")
