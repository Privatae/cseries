#!/usr/bin/env python3
"""
Dispatch door: unknown tool names, a toolless agent, an attached file.

Runs against the image (/app) or a source tree (APP_ROOT=<repo>); no pod, no
pool, no model — the agent loop is driven with the grant-wall gate's scripted
call_model.

WHY THIS EXISTS
Entity 9163, in a live end-to-end chat-attachment test: a docx was attached and its
text was in the user's message. gpt-4o-mini dispatched an agent to "extract
the code word from the attached document", with
tool_scope=['functions.context_ingest']. No tool has that name, so the agent
started with none, answered "I cannot access or read documents directly", and
that sentence was delivered to the user as the result.

  1. a scope name that is not a tool is refused at the door, naming the real tool
  2. real names, code-exec names and host-stated spend tools pass the name check
  3. a failed schema build leaves the door open (the run-time check still holds)
  4. an agent that asked for tools and holds none is never started, and its
     run scores 'blocked', never 'pass'
  5. an agent dispatched with an empty scope still runs (unchanged)
  6. a dispatch that names a file attached this turn is refused
  7. session.py's dispatch branch runs the door checks before the grant wall
  8. a dispatch that names a model this Host cannot run is refused at the
     door (a live test run, entity 9164: model='default', then
     'image', 'dall_e', 'midjourney' ... 21 dispatches in 50 seconds, each
     dying on OpenRouter's "not a valid model ID"); the Host's own model, a
     model served on the user's network, a catalog id and a cold catalog pass

Refusals and not-started records name their real cause: an invented name, a
code-exec tool, a spend tool, or a tool list that failed to load (the last is
'unavailable', never counted against the model). A real tool the schema build
left out is never refused at the door as invented.

Exit 0 = all pass; exit 1 + [dispatch-door] FAIL lines otherwise.
"""

import asyncio
import os
import re
import sys
from turn_source import turn_source  # session.py and the modules split out of it

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[dispatch-door] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    sys.path.insert(0, os.path.join(app_root, "scripts"))
    import mastercode.runtime.scoped_agent as sa
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.agent_dispatch import _finalize_record
    import test_agent_grant_wall as gw

    # ── 1. The measured name, against the REAL registry. ───────────────────
    real = {s["function"]["name"] for s in PUBLIC_TOOL_SCHEMAS.values()}
    check("negative control: context_ingest is a real public tool",
          "context_ingest" in real and "functions.context_ingest" not in real)
    un = sa.unknown_scope_names(["functions.context_ingest"], real)
    check("the wire-prefixed name is unknown and points at the real tool",
          un == {"functions.context_ingest": "context_ingest"}, f"{un}")
    msg = sa.unknown_tools_refusal(un)
    check("the refusal names both the bad name and the real one",
          bool(msg) and "'functions.context_ingest'" in msg and "'context_ingest'" in msg, str(msg))
    check("the refusal says nothing was created",
          bool(msg) and "no task was created" in msg, str(msg))

    un2 = sa.unknown_scope_names(["context_ingst", "no_such_tool"], real)
    check("a near-miss gets the closest real name", un2.get("context_ingst") == "context_ingest", f"{un2}")
    check("a name like nothing gets no guess", "no_such_tool" in un2 and un2["no_such_tool"] is None, f"{un2}")

    # ── 2. What the name check must leave alone. ───────────────────────────
    check("real names pass", sa.unknown_scope_names(["context_ingest"], real) == {})
    check("a code-exec name is left to its own wall",
          sa.unknown_scope_names(["codex.run"], real) == {})
    check("a host-stated spend tool is left to its own strip",
          sa.unknown_scope_names(sorted(sa.HOST_STATED_SPEND_TOOLS), real) == {})
    check("no unknown names, no refusal", sa.unknown_tools_refusal({}) is None)

    # ── 1b. An invented name gets the closest real tools (a live test run,
    # entity 9164: tool_scope ['filesystem'], intent "Create a file in the
    # workspace", refused with no tool named, and the Host gave up). ─────────
    _all = list(PUBLIC_TOOL_SCHEMAS.values())
    _near = sa.nearest_tools("filesystem", _all, intent="Create a file in the workspace")
    check("negative control: 'filesystem' is no tool and no slip of one",
          "filesystem" not in real and sa.unknown_scope_names(["filesystem"], real) == {"filesystem": None})
    check("the search over the real registry finds the tool that writes a workspace file",
          "workspace_file" in _near and all(n in real for n in _near), f"{_near}")
    msg_n = sa.unknown_tools_refusal({"filesystem": _near})
    check("the refusal names the closest tools for an invented name",
          bool(msg_n) and "'filesystem' (closest: " in msg_n and "'workspace_file'" in msg_n, str(msg_n))
    check("a slip still names its one tool, and a name with no candidates names none",
          "(the tool is 'a')" in sa.unknown_tools_refusal({"x": "a"})
          and sa.unknown_tools_refusal({"x": None}).startswith("This dispatch names tools that do not exist: 'x'."))
    # A candidate must be a tool an agent can hold: the registry also carries
    # the Host's own build doors and verbs, and `build` offered for "build me a
    # website" sends the Host from this refusal into the code-exec wall.
    from mastercode.runtime.tool_lanes import lane_for

    def _agent_cannot_hold(n):
        return (sa._is_code_exec_tool(n) or n in sa.HOST_STATED_SPEND_TOOLS
                or n in sa.HOST_ONLY_MACHINE_TOOLS
                or lane_for(n, PUBLIC_TOOL_SCHEMAS.get(n)) in ("verb", "host", "retired"))
    check("negative control: the registry holds build doors no agent can hold",
          "build" in real and _agent_cannot_hold("build") and "ladder.run" in real)
    _held = {(nm, it): sa.nearest_tools(nm, _all, intent=it) for nm, it in (
        ("make_site", "build me a website and deploy it"),
        ("runner", "run a ladder of builds"),
        ("compare", "compare two candidate builds and adopt one"),
        ("bench", "benchmark models"), ("shell", "run a command on my machine"),
        ("finish", "close the turn"))}
    _bad = {k: [n for n in v if _agent_cannot_hold(n)] for k, v in _held.items()}
    check("the search never offers a tool no agent can hold (a build door, a verb, a spend or machine tool)",
          all(_held.values()) and not any(_bad.values()), f"{_bad} of {_held}")

    # ── 1/3. The async door against a stubbed schema build. ────────────────
    _orig_build = sa._build_tool_schemas

    async def fake_schemas(pool, eid, **kw):
        return [gw._schema("context_ingest"), gw._schema("inbox_send")]

    async def broken_schemas(pool, eid, **kw):
        raise RuntimeError("db down")

    sa._build_tool_schemas = fake_schemas
    r = asyncio.run(sa.scope_name_refusal(gw._Pool(), 163, ["functions.context_ingest"]))
    check("door refuses the measured scope", bool(r) and "'context_ingest'" in r, str(r))
    check("door passes a real scope",
          asyncio.run(sa.scope_name_refusal(gw._Pool(), 163, ["context_ingest"])) is None)
    r = asyncio.run(sa.scope_name_refusal(gw._Pool(), 163, ["staging_area"],
                                          intent="stage this material into my context"))
    check("door: an invented name is refused with the closest offered tool for its intent",
          bool(r) and "'staging_area' (closest: 'context_ingest'" in r, str(r))
    # The builder reads bridge access from the DB and a failed read looks like
    # "no access". A name some registry knows is never refused as invented.
    from mastercode.runtime.tool_call_ledger import known_tool_schema
    check("negative control: code_read is a real registry tool", known_tool_schema("code_read") is not None)
    check("door passes a real tool the schema build left out",
          asyncio.run(sa.scope_name_refusal(gw._Pool(), 163, ["code_read"])) is None)
    sa._build_tool_schemas = broken_schemas
    check("a failed schema build leaves the door open",
          asyncio.run(sa.scope_name_refusal(gw._Pool(), 163, ["functions.context_ingest"])) is None)
    sa._build_tool_schemas = _orig_build

    # ── 4. Run time: asked for tools, holds none → never started. ──────────
    executed, frames = [], []
    out = asyncio.run(gw._run(sa, scope=["functions.inbox_send"],
                              calls=[gw._tool_call("inbox_send")],
                              executed=executed, frames=frames))
    check("the toolless agent never ran a model round",
          out.get("rounds") == 0 and out.get("tokens") == 0, f"{out}")
    check("...and never executed anything", executed == [], f"{executed}")
    check("...and says it was not started", "was not started" in str(out.get("error")), f"{out}")
    check("...naming the unknown tool", "functions.inbox_send" in (out.get("denied_tools") or []), f"{out}")
    check("...with the reason on the run record",
          "not a tool this Host has: functions.inbox_send" in str(out.get("blocked_reason")), f"{out}")
    rec = _finalize_record(out, None, None)
    check("the run scores 'blocked', never 'pass'", rec["verdict"] == "blocked", f"{rec}")

    # Each cause is named for what it is.
    out_ce = asyncio.run(gw._run(sa, scope=["codex.run"], calls=[], executed=[], frames=[]))
    check("a code-exec-only scope is not started, and is told about the build tools",
          "was not started" in str(out_ce.get("error")) and "build" in str(out_ce.get("error"))
          and "not a tool this Host has" not in str(out_ce.get("error")), f"{out_ce}")
    spend = sorted(sa.HOST_STATED_SPEND_TOOLS)[0]
    out_sp = asyncio.run(gw._run(sa, scope=[spend], calls=[], executed=[], frames=[],
                                 meta_names={"inbox_send", spend}))
    rec_sp = _finalize_record(out_sp, None, None)
    check("a spend-only scope is not started and scores 'blocked'",
          "was not started" in str(out_sp.get("error")) and rec_sp["verdict"] == "blocked"
          and "operate" in rec_sp["blocked_reason"], f"{rec_sp}")

    async def _broken(pool, eid, **kw):
        raise RuntimeError("db down")

    async def _run_broken():
        # gw._run installs its own schema stub; replace it after, then call.
        await gw._run(sa, scope=["inbox_send"], calls=[], executed=[], frames=[])
        sa._build_tool_schemas = _broken
        return await sa.run_scoped_agent(gw._Pool(), entity_id=1, task_prompt="t",
                                         tool_scope=["inbox_send"], grant=["write_memory"],
                                         session_id=None, max_rounds=3)
    out_f = asyncio.run(_run_broken())
    rec_f = _finalize_record(out_f, None, None)
    check("a tool list that failed to load is 'unavailable', not unknown names",
          rec_f["verdict"] == "unavailable" and not rec_f["denied_tools"]
          and "could not be loaded" in str(out_f.get("error")), f"{out_f}")

    async def _run_broken_ce():
        await gw._run(sa, scope=["inbox_send"], calls=[], executed=[], frames=[])
        sa._build_tool_schemas = _broken
        return await sa.run_scoped_agent(gw._Pool(), entity_id=1, task_prompt="t",
                                         tool_scope=["codex.run"], grant=["write_memory"],
                                         session_id=None, max_rounds=3)
    rec_fce = _finalize_record(asyncio.run(_run_broken_ce()), None, None)
    check("a code-exec-only scope is 'blocked' for code-exec even when the tool list failed",
          rec_fce["verdict"] == "blocked" and "codex.run" in rec_fce["denied_tools"], f"{rec_fce}")
    sa._build_tool_schemas = _orig_build

    # ── 5. An empty scope is not "asked for tools". ────────────────────────
    executed5, frames5 = [], []
    out5 = asyncio.run(gw._run(sa, scope=[], calls=[], executed=executed5, frames=frames5))
    check("an empty-scope agent still runs", out5.get("rounds", 0) >= 1
          and "was not started" not in str(out5.get("error") or ""), f"{out5}")

    # ── 6. A file attached this turn is the Host's to read. ────────────────
    name = "test-attach-976164c7.docx"
    task = f"Extract the code word from the attached document named '{name}'."
    r6 = sa.attached_file_refusal(task, "You are a document reader.", [name])
    check("the measured dispatch is refused", bool(r6) and f"'{name}'" in r6, str(r6))
    check("the refusal says the content is in the message",
          bool(r6) and "already in the user's message" in r6, str(r6))
    check("...and how to hand an agent work on the content",
          bool(r6) and "copy the part it needs into the task" in r6
          and "without naming the file" in r6, str(r6))

    # The names, from attachments shaped as send_message receives them.
    atts = [
        {"name": name, "content_block": {"type": "document", "source": {"data": "x"}}},
        {"content_block": {"type": "text_file", "filename": "notes.md", "content": "hi"}},
        {"name": "shot.png", "content_block": {"type": "image", "source": {"data": "x"}}},
        {"name": "", "content_block": {"type": "document"}},
        "not-a-dict",
        {"name": "odd.bin", "content_block": "not-a-dict"},
    ]
    got = sa.attached_file_names(atts)
    check("attached_file_names: a document's stored name and a text file's filename, no images",
          got == [name, "notes.md"], f"{got}")
    check("attached_file_names: nothing attached, no names", sa.attached_file_names(None) == [])
    check("the name matches in the system prompt too, case-insensitively",
          sa.attached_file_refusal("read it", f"Read {name.upper()}.", [name]) is not None)
    check("a dispatch that does not name the file is not refused",
          sa.attached_file_refusal("Search the web for flights.", "Researcher.", [name]) is None)
    check("no attachment, no refusal", sa.attached_file_refusal(task, "x", []) is None)
    check("an empty name never matches", sa.attached_file_refusal(task, "x", [""]) is None)

    # ── 7. Wiring: the door checks run before the grant wall. ──────────────
    src = turn_source(app_root)
    # The branch ends at the next tool's branch, so nothing after it (another
    # branch, or a module split out of session.py) can stand in for it.
    _b0 = src.index('elif func_name == "dispatch":')
    _b1 = src.find("elif func_name ==", _b0 + 1)
    branch = src[_b0:_b1 if _b1 > _b0 else len(src)]
    pos = [branch.find(k) for k in ("_attached_file_refusal(", "await _scope_name_refusal(",
                                    "await _model_name_refusal(",
                                    "_grant_precheck(_scope, _grant)")]
    check("session.py dispatch branch: attachment, tool names, model, then grant wall",
          all(p >= 0 for p in pos) and pos == sorted(pos), f"{pos}")
    check("the attachment check reads this turn's attachments through attached_file_names",
          re.search(r"_attached_file_names\(attachments\)", branch[:pos[1]]) is not None)
    check("the model check reads the dispatch's own model argument",
          re.search(r"_model_name_refusal\(\s*self\.pool, sess\.entity\[\"id\"\], _agent_model",
                    branch[pos[2]:pos[3]]) is not None)

    # ── 8. An invented model is refused at the door. ───────────────────────
    catalog = {"openai/gpt-4o-mini-2024-07-18": 128000,
               "google/gemini-2.5-flash-image": 32768, "qwen/qwen3-coder": 262144}
    measured = ["default", "image", "image_generation", "dall_e", "midjourney",
                "stable-diffusion-v2", "image-gen-v2"]
    _orig8 = (sa._is_chat_route_model, sa._local_model_for, sa._model_catalog)

    async def _not_chat(_pool, _eid, _m):
        return False

    async def _not_local(_pool, _eid, _m, _tt):
        return None, []

    def refusal(model, eid=9164):
        return asyncio.run(sa.model_name_refusal(None, eid, model))

    sa._is_chat_route_model, sa._local_model_for = _not_chat, _not_local
    sa._model_catalog = lambda: dict(catalog)
    for name in measured:
        r = refusal(name)
        check(f"the measured invented model {name!r} is refused at the door",
              bool(r) and f"`{name}`" in r, str(r))
    r8 = refusal("default")
    check("...and the refusal says no agent started and how to dispatch instead",
          bool(r8) and r8.startswith("No agent was started") and "`model` left out" in r8, str(r8))
    check("a model the catalog lists passes", refusal("qwen/qwen3-coder") is None)
    check("a listed model with an OpenRouter variant suffix passes",
          refusal("qwen/qwen3-coder:nitro") is None)
    check("...but a suffix does not make an invented base id real",
          refusal("dall_e:nitro") is not None)
    check("no model passes", refusal(None) is None and refusal("  ") is None)
    check("a Claude-shaped id goes to its own provider path, not this check",
          refusal("claude-sonnet-4-5") is None)

    sa._model_catalog = lambda: {}
    check("a cold catalog refuses nothing (it is not evidence)", refusal("default") is None)
    sa._model_catalog = lambda: dict(catalog)

    async def _chat_is_k3(_pool, _eid, m):
        return m == "k3"
    sa._is_chat_route_model = _chat_is_k3
    check("the Host's own model passes though the catalog lacks it (entity 9003's 'k3')",
          refusal("k3", eid=9003) is None)
    sa._is_chat_route_model = _not_chat

    async def _served(_pool, _eid, m, _tt):
        return ({"endpoint": "http://[fd00::1]:11434"}, []) if m == "llama3.2:3b" else (None, [])
    sa._local_model_for = _served
    check("a model a machine on the user's network serves passes", refusal("llama3.2:3b") is None)

    async def _outside_type(_pool, _eid, _m, _tt):
        return None, ["box-2"]
    sa._local_model_for = _outside_type
    check("a model served only outside the turn's type is left to run_scoped_agent's refusal",
          refusal("llama3.2:3b") is None)
    sa._is_chat_route_model, sa._local_model_for, sa._model_catalog = _orig8

    # Half-warm catalog: the route's first-party rows are in, the OpenRouter
    # list is not (startup warm still sleeping, or its fetch failed). The real
    # _model_catalog must read that as cold, not refuse every OpenRouter id.
    sys.path.insert(0, app_root)
    import server.routes.inference as INF
    _saved_cat, _saved_dm = INF._CATALOG_CACHE.get("data"), dict(INF._DIRECT_MODELS_CACHE)
    try:
        INF._CATALOG_CACHE["data"] = None
        INF._DIRECT_MODELS_CACHE.clear()
        INF._DIRECT_MODELS_CACHE["kimi"] = {"data": [{"id": "k3", "context_length": 262144}]}
        check("a half-warm catalog (first-party rows, OpenRouter list cold) refuses nothing",
              not sa._model_catalog()
              and sa.unknown_model_refusal("openai/gpt-5.2", "openai/gpt-5.2",
                                           sa._model_catalog()) is None,
              sa._model_catalog())
        INF._CATALOG_CACHE["data"] = {"openrouter_models": [
            {"id": "openai/gpt-5.2", "context_length": 400000},
            # A row OpenRouter lists with no context_length: still a model.
            {"id": "acme/new-model"}]}
        cat = sa._model_catalog()
        check("once the OpenRouter list is warm, an id it lacks is refused",
              sa.unknown_model_refusal("default", "default", cat) is not None)
        check("an OpenRouter row with no context_length is still a model it lists",
              sa.unknown_model_refusal("acme/new-model", "acme/new-model", cat) is None, f"{cat}")
        check("an id in a different case is the same model",
              sa.unknown_model_refusal("OpenAI/GPT-5.2", "OpenAI/GPT-5.2", cat) is None)
        check("a first-party-only id ('k3') is judged by OpenRouter's list, which it would "
              "ride on this branch, and refused",
              sa.unknown_model_refusal("k3", "k3", cat) is not None, f"{cat}")
        check("an id the door let through in another case is sent in OpenRouter's spelling",
              sa.catalog_spelling("OpenAI/GPT-5.2", cat) == "openai/gpt-5.2", f"{cat}")
        check("...with a :variant kept as given",
              sa.catalog_spelling("OpenAI/GPT-5.2:nitro", cat) == "openai/gpt-5.2:nitro")
        check("...and an id the catalog does not list goes as typed",
              sa.catalog_spelling("Some/Other", cat) == "Some/Other"
              and sa.catalog_spelling("x", {}) == "x")
        _ra = open(os.path.join(app_root, "brain/mastercode/runtime/scoped_agent.py")).read()
        _br = _ra[_ra.index('eff_provider, eff_model = normalize_provider("openrouter", model)'):]
        _br = _br[:_br.index("\n    else:")]
        check("run_scoped_agent's bare-model OpenRouter branch sends the catalog's spelling",
              "eff_model = catalog_spelling(eff_model, _model_catalog())" in _br, _br[-400:])
    finally:
        INF._CATALOG_CACHE["data"] = _saved_cat
        INF._DIRECT_MODELS_CACHE.clear()
        INF._DIRECT_MODELS_CACHE.update(_saved_dm)

    print()
    if FAILS:
        print(f"[dispatch-door] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("[dispatch-door] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
