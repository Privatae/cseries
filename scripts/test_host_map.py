#!/usr/bin/env python3
"""Gate: the host's MAP + the `orient` drill. No DB, no network.

  1. The map is one screen: group names + counts, apps, machines, projects,
     repos, skills, what it may touch — never a per-tool line, every list capped.
  2. The four blocks it replaced (tool_inventory, environment, domain_map,
     permissions) are out of the schema; the map is in.
  3. `orient` is a registered read tool (schema, caps row, dispatch name, lane)
     and returns the per-tool lines — lane, grant, read ops — for a group,
     authorized through the same walk the prompt used.
  4. The orchestrator catalog carries verbs + lane counts, not per-tool lines;
     the host's own build doors are still listed by name.
  5. Host rule 2 names the three action lanes instead of "never performs
     actions itself".
  6. surface_role no longer lists "other apps" (the map does, once).

  PYTHONPATH=brain:. python3 scripts/test_host_map.py
"""
import asyncio
import inspect
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))
sys.path.insert(0, str(REPO))
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from mastercode.runtime import prompt_schema as ps  # noqa: E402
from mastercode.runtime.prompt_builder import (  # noqa: E402
    _CATEGORY_LABELS, _build_map, _build_surface_role)

# ── 1. the map renders one screen ───────────────────────────────────────────
tools = []
for i in range(30):
    tools.append({"name": f"proj_tool_{i}", "category": "projects", "priority": 1 if i < 2 else 2,
                  "purpose": "p", "requires": "read_context"})
for i in range(9):
    tools.append({"name": f"comms_tool_{i}", "category": "comms", "priority": 2, "purpose": "p",
                  "requires": None})
tools.append({"name": "schedule_task", "category": "comms", "priority": 1, "purpose": "p",
              "requires": "write:calendar"})
tools.append({"name": "brand_new_tool", "category": "brand_new_category", "priority": 1,
              "purpose": "p", "requires": None})
# The tools the map's sentences name — a sentence renders only when its tool is held.
for n in ("orient", "list_projects", "context", "gitea", "search_my_code",
          "describe_app", "open_app", "get_network"):
    tools.append({"name": n, "category": "environment", "priority": 2, "purpose": "p", "requires": None})
apps = [{"id": f"app{i}", "name": f"App {i:02d}"} for i in range(20)] + [{"id": "circles", "name": "Circles"}]
rendered = _build_map(
    tools_meta=tools, apps=apps, current_app_id="circles",
    project_count=12, repos_granted=[f"owner/r{i}" for i in range(7)],
    repos_indexed=["owner/r1"], skills=[f"s{i}" for i in range(50)],
    permissions=[{"domain": "network", "permission_key": "read"},
                 {"domain": "network", "permission_key": "propose"}],
)
check("map header", rendered.startswith("## Your Map"))
# The brief prompt: apps, skills and tools are COUNTS;
# find_tools searches them, so no names, no group lines, no drill.
check("tools: a count and the pointer at find_tools, no group lines",
      "**Tools.** 49. `find_tools` finds any tool" in rendered
      and "(`projects`)" not in rendered and "by group" not in rendered, rendered)
check("tools: NO per-tool line (no '- **name**', no dispatch grant hint, no tool names)",
      "- **" not in rendered and "dispatch grant:" not in rendered
      and "proj_tool_0" not in rendered, rendered)
check("no drill is named", "orient" not in rendered)
check("apps: a count and the app you are in, no list",
      "**Apps installed (21).** You are in Circles." in rendered
      and "App 00" not in rendered, rendered)
check("machines: one fixed sentence — nothing about the bastions themselves",
      "**Machines.** Bastions carry your traffic in and out; `get_network` shows yours" in rendered
      and "online:" not in rendered and "b0" not in rendered, rendered)
check("machines: the map takes no bastion input at all (it cannot move with them)",
      "bastions" not in inspect.signature(_build_map).parameters)
_many = _build_map(tools_meta=tools, apps=apps, current_app_id="circles", project_count=12,
                   repos_granted=[f"owner/r{i:02d}" for i in range(50)], repos_indexed=None,
                   skills=None, permissions=None)
check("repos: a full Gitea page (a moving subset) renders '50+' and names none",
      "50+ you can read on Gitea" in _many and "owner/r00" not in _many, _many)
check("no domain line: the map takes no domain input (the domain controller is retired)",
      "domains" not in inspect.signature(_build_map).parameters
      and "You may touch" not in rendered, rendered)
check("projects: a count",
      "**Projects.** 12 active." in rendered, rendered)
check("repos: granted and indexed counts, no names",
      "7 you can read on Gitea; 1 indexed for code search." in rendered
      and "owner/r0" not in rendered, rendered)
check("skills: a count, no names",
      "**Skills.** 50." in rendered and "s10" not in rendered, rendered)
check("permissions by domain, then the one sentence about what lies outside them",
      "**Permissions.** network: read, propose.\nOutside these, say what you can do" in rendered, rendered)
check("network authority line kept", "2FA" in rendered and "Network app" in rendered)
check("brief: under 1,200 chars with 49 tools, 21 apps, 50 skills",
      len(rendered) < 1200, str(len(rendered)))
check("every registry category has a label",
      all(k in _CATEGORY_LABELS for k in ("apps", "memory", "vault", "brain", "governance",
                                            "community", "files", "settings", "context")))

empty = _build_map(tools_meta=[], apps=[], current_app_id=None, project_count=None,
                   repos_granted=None, repos_indexed=None, skills=None, permissions=None)
check("degraded: every source unavailable still renders the primer + a zero tools count",
      "## Your Map" in empty and "**Tools.** 0." in empty
      and "**Machines.** Bastions carry your traffic in and out" in empty, empty)

# ── 2. schema ───────────────────────────────────────────────────────────────
names = [b.name for b in ps.PROMPT_SCHEMA]
check("schema has the map block, per_rebuild, after surface_role",
      "map" in names and names.index("map") == names.index("surface_role") + 1
      and [b for b in ps.PROMPT_SCHEMA if b.name == "map"][0].freshness is ps.Freshness.PER_REBUILD)
for gone in ("tool_inventory", "environment", "domain_map", "permissions"):
    check(f"schema no longer carries {gone!r}", gone not in names)
facts_src = inspect.getsource(ps._map_facts)
check("_map_facts reads every store independently (one try per source)",
      facts_src.count("except Exception as e:") >= 7, str(facts_src.count("except Exception as e:")))
repos_src = inspect.getsource(ps._granted_repos)
check("the one remote (Gitea) read is bounded AND cached per process (the prompt rebuilds every turn)",
      "wait_for(" in repos_src and "_MAP_REMOTE_TIMEOUT" in repos_src
      and "_MAP_REPOS_CACHE" in repos_src and "_granted_repos(eid)" in facts_src)


# The cache, driven: one fetch, then hits; a failed refresh returns the stale list.
async def _drive_cache():
    calls = {"n": 0}

    async def fake_request(method, path, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("gitea down")
        return [{"full_name": "owner/repo"}, {"full_name": "owner/site"}]

    import server.routes.gitea_client as gc
    saved = (gc.gitea_request, gc.gitea_creds)
    gc.gitea_request, gc.gitea_creds = fake_request, (lambda: ("entity-9003", "t", "forge.example"))
    try:
        ps._MAP_REPOS_CACHE.clear()
        a = await ps._granted_repos(3)
        b = await ps._granted_repos(3)
        n_after_two = calls["n"]
        ps._MAP_REPOS_CACHE[3] = (0.0, ps._MAP_REPOS_CACHE[3][1])  # force stale
        c = await ps._granted_repos(3)
        return a, b, c, n_after_two, calls["n"]
    finally:
        gc.gitea_request, gc.gitea_creds = saved
        ps._MAP_REPOS_CACHE.clear()


_a, _b, _c, _n2, _n3 = asyncio.run(_drive_cache())
check("repo cache: first read fetches, second is a hit (one request for two builds)",
      _a == ["owner/repo", "owner/site"] and _b == _a and _n2 == 1, f"{_a} {_b} n={_n2}")
check("repo cache: a failed refresh returns the stale list, never None", _c == _a and _n3 == 2, f"{_c} n={_n3}")
check("_b_map skips voice/document/tool-call surfaces",
      '"voice_call", "document", "tool_call"' in inspect.getsource(ps._b_map))

# ── 3. orient is gone ──────────────────────────────────────────────────────
# The groups-mode drill. Search mode's find_tools replaced it (it came off
# every tool list first); its registry entry, caps row, lane and executor went
# after, so no Host can be offered a tool that no longer has a door.
from mastercode.public_tool_registry import (  # noqa: E402
    PUBLIC_META_TOOL_NAMES, PUBLIC_TOOL_SCHEMAS)
from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS  # noqa: E402
from mastercode.runtime import tool_lanes as _TL  # noqa: E402
from mastercode.runtime import tools as T  # noqa: E402

check("orient: no schema, no executor-set entry, no caps row, no lane",
      "orient" not in PUBLIC_TOOL_SCHEMAS and "orient" not in PUBLIC_META_TOOL_NAMES
      and "orient" not in META_TOOL_CAPS and "orient" not in inspect.getsource(_TL))
check("orient: the executor neither routes nor defines it",
      'elif name == "orient":' not in inspect.getsource(T._execute_meta_tool)
      and not hasattr(T, "_exec_orient"))
check("skill 'explore' teaches find_tools, not orient",
      "## find_tools — what you can use" in (REPO / "default-skills/explore.md").read_text()
      and "orient(" not in (REPO / "default-skills/explore.md").read_text())

# The map names a tool only when THIS surface holds it (a surface
# holding none of the circles/workbench tools still had the sentences rendered).
_other = _build_map(tools_meta=[{"name": "notes", "category": "notes", "priority": 1, "purpose": "p", "requires": None}],
                     apps=[{"id": "a", "name": "A"}], current_app_id=None,
                     project_count=1, repos_granted=["x/y"], repos_indexed=["x/y"], skills=["apps"], permissions=[])
check("surface: a surface holding only `notes` gets no orient/get_network/describe_app/open_app/list_projects/gitea/search_my_code/context sentence",
      "**Tools.** 1." in _other and not any(
          s in _other for s in ("`orient", "`get_network`", "`describe_app", "`open_app`", "`list_projects`",
                                "`gitea`", "`search_my_code`", "context(action")), _other)

# ── 4. the orchestrator catalog: verbs + counts, no per-tool lines ──────────
import mastercode.runtime.session as S  # noqa: E402


def _schema(name, **props):
    return {"type": "function", "function": {"name": name, "description": f"{name} desc",
                                              "parameters": {"type": "object", "properties": props}}}


# The sample needs one tool per lane. `contacts_manage` played the dispatch
# part until the operate sweep moved it into the no-model lane;
# `github` takes that seat now — mixed the same way (repo/code/tree read,
# import writes) and still an agent's job, because the model is what decides
# what to look for.
cat = [PUBLIC_TOOL_SCHEMAS["build"], _schema("list_projects"), _schema("inbox_send"),
       PUBLIC_TOOL_SCHEMAS["github"], PUBLIC_TOOL_SCHEMAS["schedule_task"]]
p = S._build_orchestrator_prompt(cat)
# The brief prompt: the block is the facts no tool
# returns — the lane counts, the build doors by name, the turn's end. The
# verb rules live in the verb schemas only.
check("catalog: lane counts, not per-tool lines",
      "## Your tools" in p and "`list_projects`" not in p and "`inbox_send`" not in p
      and "schedule_task" not in p, p)
# list_projects: read. github + schedule_task both carry read-safe ops ⇒ 2
# mixed. inbox_send + schedule_task: operate lane. github: dispatch, and the
# one tool here that reads untrusted content (what an agent may hold). build: host.
check("catalog: counts are right (1 read + 2 mixed read ops, 2 operate, 1 agent reader)",
      "1 read tools and the read ops of 2 more" in p
      and "2 operations (run with `operate`)" in p
      and "1 of these read untrusted content" in p, p)
check("catalog: points at find_tools, never at orient or a map of groups",
      "`find_tools`" in p and "orient" not in p)
check("catalog: own build doors named, called by name",
      "Your own build tools" in p and "`build`" in p and "never dispatched or operated" in p)
check("catalog: the verb rules are NOT restated (they live in the verb schemas)",
      "`operate` runs ONE" not in p and "`dispatch` spawns" not in p and "`recall` searches ALL" not in p)
check("catalog: under 1,500 chars regardless of tool count", len(p) < 1500, str(len(p)))

# ── 5. the rulebook is the person's own rules only ──────────────────────────
from mastercode.runtime import agent_rules as _ar  # noqa: E402


class _NoOverridePool:
    async def fetchval(self, *a, **k):
        return None


class _OverridePool:
    async def fetchval(self, *a, **k):
        return {"host_rules": "Always answer in French."}


check("no override: no rulebook rides the prompt",
      asyncio.run(_ar.build_host_rules(_NoOverridePool(), 1)) == "")
_ov = asyncio.run(_ar.build_host_rules(_OverridePool(), 1))
check("the person's own rules ride, headed",
      _ov.startswith("## Rules the person set for you") and "Always answer in French." in _ov, _ov)
check("the platform rulebook is gone",
      not hasattr(_ar, "GLOBAL_RULES") and not hasattr(_ar, "HOST_RULES"))
_sess_src = turn_source(REPO)
check("the host prompt reads the person's rules",
      'build_host_rules(self.pool, sess.entity["id"])' in _sess_src)

# ── 6. surface_role: other apps moved to the map ────────────────────────────
sr = _build_surface_role("chat", installed_apps=[
    {"id": "circles", "name": "Circles"}, {"id": "network", "name": "Network"}]) or ""
check("surface_role no longer lists other apps", "Other apps installed" not in sr and "**Network**" not in sr, sr)
check("surface_role still frames the current app", "You are currently in the **Circles** app." in sr, sr)

print("\nPASS" if not fails else f"\nFAIL: {fails} check(s)")
sys.exit(1 if fails else 0)
