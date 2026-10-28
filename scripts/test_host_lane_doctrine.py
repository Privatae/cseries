#!/usr/bin/env python3
# gate-scope: whole-tree — every public tool schema
"""Gate: the Host calls an agent ONLY for untrusted work.

The Host calls an agent only for untrusted work outside
the platform (the open web, foreign repos, other people's and Hosts' content).
Everything in-platform it does itself, directly.

Measured over 14 days: 23 dispatches, 17 of them in-platform work, and 187
agent code reads. Two mechanisms made that the default, and this gate pins both
closed:

  S1  tool_lanes.lane_for sent every tool it did not recognise to the agent
      lane: 19 registry tools, one of them untrusted work. The agent lane is
      now the closed list UNTRUSTED_WORK_TOOLS; an unlisted tool is a `gap`
      (check_tool_lanes fails on it), the in-platform tools are operate ops,
      app_probe is a read and escalate a verb, the six legacy controller ship
      tools are retired, and the capability cards carry lane_for's lane.
      The walls that moved with them: keys_manage `use` and a mgmt_run write
      are refused on a turn that read untrusted content, none of the moved
      tools is schedulable, and a credential carried inside operate(...) is
      redacted in the call ledger.
  S2  the dispatch door never asked whether the work was untrusted. `work` is
      required, a scope that reads nothing untrusted is refused, and a scope
      holding the Host's own code reads is refused whatever else it holds.

Pure imports over the registry and the door; no DB, no container, no network.
Run: APP_ROOT=<repo> python3 scripts/test_host_lane_doctrine.py
"""
import asyncio
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
sys.path.insert(0, os.path.join(APP_ROOT, "scripts"))

FAILS: list[str] = []


def check(label: str, cond, detail="") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"\n        {str(detail)[:600]}" if detail != "" else ""))


def part(title: str, fn) -> None:
    print(title)
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — a part that cannot run fails
        check(f"{title} ran", False, repr(e))


def _schemas() -> dict:
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS
    from mastercode.runtime.triage import _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS
    out = {}
    for src in (PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS, _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS):
        for sc in src.values():
            n = ((sc or {}).get("function") or {}).get("name")
            if n:
                out[n] = sc
    return out


IN_PLATFORM = ("context", "mgmt_run", "image_generate", "keys_manage", "connect_compute",
               "reachout_channel", "propose_capability", "community_board", "state_store", "dcn")
RETIRED_SHIP = ("docker_build", "docker_push", "git_tag", "k8s_set_image", "k8s_set_env",
                "systemctl_run")


def s1_lanes():
    from mastercode.runtime.tool_lanes import lane_for
    sc = _schemas()
    for t in IN_PLATFORM:
        check(f"{t} -> operate (in-platform: the Host runs it itself)",
              t in sc and lane_for(t, sc[t]) == "operate", lane_for(t, sc.get(t)))
    check("app_probe -> read (it reads the Host's own app)",
          lane_for("app_probe", sc.get("app_probe")) == "read", lane_for("app_probe", sc.get("app_probe")))
    check("escalate -> verb (it rides every turn)",
          lane_for("escalate", sc.get("escalate")) == "verb", lane_for("escalate", sc.get("escalate")))
    dispatch = sorted(n for n, s in sc.items() if lane_for(n, s) == "dispatch")
    check("the agent lane over every registry tool is exactly {'github'}", dispatch == ["github"], dispatch)
    unknown = {"type": "function", "function": {"name": "brand_new_tool", "parameters": {
        "type": "object", "properties": {"op": {"type": "string", "enum": ["create", "delete"]}}}}}
    check("an unclassified action is a 'gap', never the agent lane",
          lane_for("brand_new_tool", unknown) == "gap", lane_for("brand_new_tool", unknown))
    check("an empty name is a 'gap'", lane_for("") == "gap", lane_for(""))
    gaps = sorted(n for n, s in sc.items() if lane_for(n, s) == "gap")
    check("no registry tool lands in 'gap'", not gaps, gaps)


def s1_retired():
    from mastercode.runtime.tool_lanes import lane_for
    from mastercode.runtime.triage import _BRIDGE_TOOL_NAME_TO_OP
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    sc = _schemas()
    for t in RETIRED_SHIP:
        check(f"{t}: retired, no schema, no bridge op, no caps entry",
              lane_for(t) == "retired" and t not in sc and t not in _BRIDGE_TOOL_NAME_TO_OP
              and t not in META_TOOL_CAPS,
              (lane_for(t), t in sc, t in _BRIDGE_TOOL_NAME_TO_OP, t in META_TOOL_CAPS))
    check("k8s_rollout_status stays (a read of a rollout)",
          lane_for("k8s_rollout_status", sc.get("k8s_rollout_status")) == "read")


def s1_direct_ops():
    from mastercode.runtime.direct_ops import (
        DIRECT_OP_EXCLUDED_OPS, UNATTENDED_OP_TOOLS, excluded_op_refusal, not_an_op_refusal)
    check("dcn run_plan is no longer excluded from operate", "dcn" not in DIRECT_OP_EXCLUDED_OPS,
          DIRECT_OP_EXCLUDED_OPS.get("dcn"))
    check("operate(dcn, op='run_plan') is not refused by the per-op wall",
          excluded_op_refusal("dcn", {"op": "run_plan"}) is None)
    stray = sorted(UNATTENDED_OP_TOOLS & {"keys_manage", "connect_compute", "reachout_channel",
                                          "image_generate", "mgmt_run"})
    check("none of the moved credential / spend / machine tools is schedulable", not stray, stray)
    gap_msg = not_an_op_refusal("brand_new_tool")
    check("operate's refusal for an unclassified name says it is a gap, and offers no agent",
          "gap" in gap_msg and "dispatch a governed agent" not in gap_msg, gap_msg)
    read_msg = not_an_op_refusal("check_page")
    check("operate's refusal for a read names read(...)", "read(tool='check_page'" in read_msg, read_msg)


def s1_cards():
    import generate_capabilities as gc
    from mastercode.runtime.tool_lanes import lane_for
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    f = getattr(gc, "host_op_lane", None)
    check("generate_capabilities exposes host_op_lane (the card lane)", callable(f))
    if not callable(f):
        return
    drift = []
    for sc in PUBLIC_TOOL_SCHEMAS.values():
        fn = (sc or {}).get("function") or {}
        n = fn.get("name")
        if n and f(n, {"function": fn}, "public") != lane_for(n, sc):
            drift.append((n, f(n, {"function": fn}, "public"), lane_for(n, sc)))
    check("every public tool's card lane equals lane_for", not drift, drift[:10])
    for t in ("check_page", "diagnose", "close_turn"):
        sc = PUBLIC_TOOL_SCHEMAS.get(t) or {}
        got = f(t, {"function": sc.get("function") or {}}, "public")
        check(f"the {t} card does not say dispatch", got != "dispatch", got)


def _run(coro):
    return asyncio.run(coro)


def s1_executor_walls():
    from mastercode.runtime import turn_taint
    from mastercode.runtime import tools as T

    def in_turn(coro_fn, *, taint=None, agent=False):
        async def go():
            tok = turn_taint.begin_agent() if agent else turn_taint.begin_turn()
            try:
                if taint:
                    turn_taint.mark(taint, "test")
                return await coro_fn()
            finally:
                turn_taint.end_turn(tok)
        return _run(go())

    w = in_turn(lambda: T._exec_mgmt_run({"server": "box", "op": "contain"}), taint=turn_taint.WEB)
    check("mgmt_run: a write on a marked turn is refused before anything runs",
          w.get("status") == "denied" and w.get("reason") == "untrusted_turn", w)
    w = in_turn(lambda: T._exec_mgmt_run({"server": "box", "op": "contain"}), agent=True)
    check("mgmt_run: a write in an agent's run is refused",
          w.get("status") == "denied" and w.get("reason") == "untrusted_turn", w)
    r = in_turn(lambda: T._exec_mgmt_run({"server": "box", "op": "uptime"}), taint=turn_taint.WEB)
    check("mgmt_run: a read on a marked turn is not held back by the taint wall",
          r.get("reason") != "untrusted_turn", r)
    c = in_turn(lambda: T._exec_mgmt_run({"server": "box", "op": "contain"}))
    check("mgmt_run: a write on a clean turn passes the taint wall",
          c.get("reason") != "untrusted_turn", c)

    u = in_turn(lambda: T._exec_keys_manage({"op": "use", "name": "k", "action": "http_call",
                                             "request": {"url": "https://x.test/"}}),
                taint=turn_taint.MESSAGE)
    check("keys_manage: use on a marked turn is refused",
          u.get("status") == "denied" and u.get("reason") == "untrusted_turn", u)
    u2 = in_turn(lambda: T._exec_keys_manage({"op": "use", "name": "k", "action": "http_call",
                                              "request": {"url": "https://x.test/"}}))
    check("keys_manage: use on a clean turn passes the taint wall",
          u2.get("reason") != "untrusted_turn", u2)
    lst = in_turn(lambda: T._exec_keys_manage({"op": "list"}), taint=turn_taint.WEB)
    check("keys_manage: list is not held back on a marked turn", lst.get("reason") != "untrusted_turn", lst)


def s1_ledger_redaction():
    from mastercode.runtime import tool_call_ledger as L
    p = L._preview({"tool": "keys_manage", "args": {"op": "add", "name": "t", "value": "S3CRET-VALUE"},
                    "intent": "store it"}, "operate")
    check("a credential carried inside operate(...) is redacted in the call ledger",
          "S3CRET-VALUE" not in p, p)
    p2 = L._preview({"tool": "preference", "args": {"op": "set", "value": "warmer"}}, "operate")
    check("an ordinary `value` inside operate(...) is kept", "warmer" in p2 or "args withheld" in p2, p2)


def s2_door():
    from mastercode.runtime import scoped_agent as sa
    from mastercode import agent_work as aw
    from mastercode.runtime import turn_taint
    src = turn_taint.untrusted_source_tools()
    check("turn_taint names the untrusted-source tools (web, foreign repo, messages, validator)",
          {"web_search", "read_url", "research", "github", "gitea", "bug_review",
           "community_board", "host_message"} <= set(src), sorted(src))
    check("…and none of the Host's own reads is one",
          not ({"context", "code_grep", "code_read", "search_my_code", "diagnose"} & set(src)))
    m = sa.dispatch_precheck(["code_grep"], ["read_context"])
    check("dispatch scope [code_grep] -> refused, naming read(tool='code_grep')",
          m is not None and "read(tool='code_grep'" in m, m)
    m = sa.dispatch_precheck(["code_grep", "web_search"], ["read_context", "external_api"])
    check("dispatch scope [code_grep, web_search] -> refused (own code reads, whatever else)",
          m is not None and "code_grep" in m, m)
    for own in ("code_read", "code_tree", "search_my_code", "index_my_code"):
        check(f"dispatch scope holding {own} -> refused",
              sa.dispatch_precheck([own, "read_url"], ["read_context", "external_api",
                                                        "write_context", "execute_tool"]) is not None)
    m = sa.dispatch_precheck(["context"], ["read_context", "read_memory", "write_context"])
    check("dispatch scope [context] -> refused (its own files are its own work)", m is not None, m)
    m = sa.dispatch_precheck(["diagnose", "machine_map"], ["read_context"])
    check("dispatch scope [diagnose, machine_map] -> refused (nothing untrusted to read)", m is not None, m)
    m = sa.dispatch_precheck(["web_search"], ["external_api"])
    check("dispatch scope [web_search] -> accepted", m is None, m)
    check("…with work='web'", aw.work_refusal("web") is None)
    m = sa.dispatch_precheck(["bug_review"], ["external_api"])
    check("dispatch scope [bug_review] -> accepted (the validator reads another Host's report)", m is None, m)
    check("…with work='message'", aw.work_refusal("message") is None)
    check("any dispatch without `work` -> refused", aw.work_refusal(None) is not None
          and aw.work_refusal("  ") is not None)


def s2_refusals_offer_only_admitted_scopes():
    # A refusal that tells the Host to scope a tool the same door then refuses
    # sends it round the door again (gitea, host_message
    # and community_board read outside content but are direct ops, so a scope
    # of one is refused as clerk work).
    import re
    from mastercode.runtime import scoped_agent as sa
    from mastercode.runtime import turn_taint
    src = turn_taint.untrusted_source_tools()
    for scope in (["context"], ["diagnose", "machine_map"], ["gitea"], ["host_message"]):
        msg = sa.scope_work_refusal(scope) or ""
        offer = msg.split("An agent is only for", 1)[1] if "An agent is only for" in msg else ""
        check(f"the refusal for {scope} offers an agent's tools", bool(offer), msg)
        named = sorted(t for t in src if re.search(rf"(?<![\w.]){re.escape(t)}(?![\w])", offer))
        bad = [t for t in named if sa.scope_work_refusal([t]) is not None]
        check(f"…and every tool it offers is one the door admits alone ({scope})", not bad, (bad, offer))


def s2_dispatch_skill():
    raw = open(os.path.join(APP_ROOT, "default-skills", "dispatch-agent.md"), encoding="utf-8").read()
    skill = " ".join(raw.split())
    example = raw.split("## Writing the whole thing", 1)[-1].split("```", 2)[1] if "```" in raw else ""
    check("the dispatch-agent skill's worked example passes `work` (required at the door)",
          "work=" in example, example)
    check("the dispatch-agent skill no longer says dcn run_plan dispatches",
          "so it dispatches" not in skill and "naming run_plan in `operate` is refused" not in skill)
    check("the dispatch-agent skill no longer offers agents for parallel work",
          "work that runs in parallel" not in skill)
    check("the dispatch-agent skill no longer says a read in the scope never trips the wall",
          "A read in the scope never trips the wall" not in skill)


def s2_verb_and_platform_dispatch():
    from mastercode.runtime.session import _orchestration_verbs
    d = next(v for v in _orchestration_verbs() if (v.get("function") or {}).get("name") == "dispatch")
    fn = d["function"]
    check("the dispatch verb requires `work`", "work" in (fn["parameters"].get("required") or []),
          fn["parameters"].get("required"))
    check("the dispatch verb no longer offers agents for parallel work", "parallel" not in fn["description"],
          fn["description"])
    from mastercode.runtime import bug_review as br
    check("the platform-started validator dispatch passes work='message'",
          "work 'message'" in br.WAKE_PROMPT, br.WAKE_PROMPT)
    skill = open(os.path.join(APP_ROOT, "default-skills", "bug-review.md"), encoding="utf-8").read()
    check("the bug-review skill's validator dispatch names work: message", "`work`: `message`" in skill)


# ── the Host's own ops, forge comments, mesh runs, /mcp ship tools ───────────

def _in_turn(coro_fn, *, taint=None):
    from mastercode.runtime import turn_taint

    async def go():
        tok = turn_taint.begin_turn()
        try:
            if taint:
                turn_taint.mark(taint, "test")
            out = await coro_fn()
            st = turn_taint.current()
            return out, ((st.source, st.detail) if st.source else None)
        finally:
            turn_taint.end_turn(tok)
    return _run(go())


class _Patch:
    """Set attributes for one block and put them back after."""

    def __init__(self, *triples):
        self.triples, self.saved = triples, []

    def __enter__(self):
        for obj, name, val in self.triples:
            self.saved.append((obj, name, getattr(obj, name, None), hasattr(obj, name)))
            setattr(obj, name, val)
        return self

    def __exit__(self, *exc):
        for obj, name, val, had in reversed(self.saved):
            if had:
                setattr(obj, name, val)
            else:
                delattr(obj, name)


def f1_keys_seal_ops_direct():
    # keys_manage op='add' and op='seal_for_user' seal a value the Host itself
    # holds (to itself, or to its user's registered key, which it can never
    # reopen), so both stay the Host's own ops. A secret the person owns
    # enters through the Keys app, which needs no Host op.
    from mastercode.runtime import direct_ops as D
    from mastercode.runtime import tools as T
    from server import brain_service
    from server.vault import api as vault_api
    check("keys_manage is a direct op", "keys_manage" in D.DIRECT_OP_TOOLS)
    for op in ("add", "seal_for_user"):
        check(f"operate(keys_manage, op='{op}') is not refused by the per-op wall",
              D.excluded_op_refusal("keys_manage", {"op": op}) is None)
    calls = []

    async def store_secret(pool, **kw):
        calls.append(("add", kw["name"], kw["plaintext"]))
        return "env-1"

    async def seal_for_user(pool, **kw):
        calls.append(("seal_for_user", kw["user_id"], kw["name"], kw["plaintext"]))
        return "env-2"

    with _Patch((brain_service, "get_pool", lambda: object()),
                (vault_api, "store_secret", store_secret),
                (vault_api, "seal_for_user", seal_for_user)):
        add, _ = _in_turn(lambda: T._exec_keys_manage(
            {"op": "add", "name": "forge:box", "cred_type": "token", "value": "tok-made-here"}))
        seal, _ = _in_turn(lambda: T._exec_keys_manage(
            {"op": "seal_for_user", "user_id": "42", "name": "box-root",
             "value": "pw-made-here"}))
    check("keys_manage add seals the Host's value through the vault",
          add.get("status") == "ok" and ("add", "forge:box", b"tok-made-here") in calls, (add, calls))
    check("keys_manage seal_for_user seals the Host's value to the user, unreadable to the Host",
          seal.get("status") == "ok" and (seal.get("result") or {}).get("host_can_read") is False
          and ("seal_for_user", "42", "box-root", b"pw-made-here") in calls, (seal, calls))
    src = open(os.path.join(APP_ROOT, "brain", "mastercode", "runtime", "direct_ops.py"),
               encoding="utf-8").read()
    check("direct_ops records why add and seal_for_user stay direct",
          "`add` and `seal_for_user` stay direct" in src)


def f3_gitea_comment_wall():
    from mastercode.runtime import tools as T
    from mastercode.runtime import turn_taint
    args = {"op": "comment", "repo": "alex/notes-app", "number": 3, "body": "LGTM"}
    marked, _ = _in_turn(lambda: T._exec_gitea(dict(args)), taint=turn_taint.WEB)
    check("gitea comment on a turn that read untrusted content is refused before anything is sent",
          marked.get("status") == "denied" and marked.get("reason") == "untrusted_turn", marked)
    sent = []

    async def gitea_comment(a):
        sent.append(a)
        return {"status": "ok", "result": {"comment_id": 1}}

    async def scopes(pool, eid):
        return {"write:gitea"}

    from mastercode.runtime import meta_tool_caps as caps
    with _Patch((T, "_gitea_comment", gitea_comment), (caps, "_resolve_entity_scopes", scopes)):
        clean, _ = _in_turn(lambda: T._exec_gitea(dict(args), entity_id=9003, pool=object()))
    check("gitea comment on a clean turn is the Host's own and is sent",
          clean.get("status") == "ok" and len(sent) == 1, (clean, sent))
    check("a dispatched agent's run never comments",
          _run(_agent_comment(T, args)).get("reason") == "untrusted_turn")


async def _agent_comment(T, args):
    from mastercode.runtime import turn_taint
    tok = turn_taint.begin_agent()
    try:
        return await T._exec_gitea(dict(args))
    finally:
        turn_taint.end_turn(tok)


def f4_mesh_harness_marks():
    # A mesh harness run is a container on the user's machine that egresses
    # directly (the bastion's client-go/harness.go driveHarnessAgent), so
    # its report marks the turn even when it coded the Host's own repo.
    from mastercode.runtime import tools as T
    from server import brain_service
    from server.network import nac_harness

    async def run_harness_op(*a, **kw):
        return {"exit": 0, "result": {"ok": True, "transcript": "fetched a page and fixed it"}}

    async def own_repo(repo):
        return None   # the forge says: this Host's own repository

    with _Patch((brain_service, "get_pool", lambda: object()),
                (nac_harness, "run_harness_op", run_harness_op),
                (nac_harness, "host_mode_refusal", lambda eid, mode: ""),
                (T, "_forge_repo_foreign", own_repo)):
        out, src = _in_turn(lambda: T._exec_mesh_harness_run(
            {"server": "box", "harness_id": "h1", "mode": "git", "repo": "alex/notes-app"},
            T.OPERATOR_ENTITY_ID))
    check("mesh_harness_run mode=git on the Host's own repo returns its report", out.get("status") == "ok", out)
    check("...and marks the turn: the run had the open web in reach",
          src is not None and "open web" in src[1], src)


def f5_mcp_ship_tools_retired():
    from server.routes import mcp as M
    gone = ("docker_build", "docker_push", "git_tag", "systemctl_run", "k8s_set_image",
            "k8s_set_env", "ship_cseries")
    names = {t.get("name") for t in M.TOOLS}
    left = sorted((set(gone) & set(M.TOOL_HANDLERS)) | (set(gone) & names))
    check("/mcp no longer offers or handles the retired ship tools", not left, left)
    check("/mcp's ship_cseries pipeline is gone", not hasattr(M, "_call_ship_cseries"))
    # The rest of the `factory` scope went too. No mint path
    # issues it (synaptive.create_token's allowlists, BASTION_SCOPES, the
    # internal and harness callers), so every tool behind it was unreachable.
    factory = ("order_software", "list_projects", "project_status", "reject_step",
               "ship_project", "code_read", "code_grep", "code_tree", "k8s_rollout_status")
    left = sorted((set(factory) & set(M.TOOL_HANDLERS)) | (set(factory) & names))
    check("/mcp no longer offers or handles the factory-scope tools", not left, left)
    check("no /mcp tool requires the `factory` scope",
          not [n for n, (_h, sc) in M.TOOL_HANDLERS.items() if sc == "factory"])
    check("/mcp's controller lane and factory helpers are gone",
          not any(hasattr(M, a) for a in ("_controller_call", "_call_code_op",
                                          "_call_order_software", "_auto_plan_project",
                                          "_require_factory_scope")))


def main() -> int:
    part("S1. the agent lane is a closed list of untrusted work", s1_lanes)
    part("S1. the legacy controller ship tools are retired", s1_retired)
    part("S1. direct ops: dcn whole, nothing schedulable that should not be", s1_direct_ops)
    part("S1. the capability cards carry lane_for's lane", s1_cards)
    part("S1. the walls that moved with the tools", s1_executor_walls)
    part("S1. the call ledger keeps a credential out of operate's preview", s1_ledger_redaction)
    part("S2. the dispatch door enforces untrusted work", s2_door)
    part("S2. the door's refusals offer only scopes it admits", s2_refusals_offer_only_admitted_scopes)
    part("S2. the dispatch skill matches the door", s2_dispatch_skill)
    part("S2. the verb and the platform's own dispatches", s2_verb_and_platform_dispatch)
    part("keys_manage add and seal_for_user are the Host's own ops", f1_keys_seal_ops_direct)
    part("a forge comment waits for a clean turn", f3_gitea_comment_wall)
    part("a mesh harness run marks the turn", f4_mesh_harness_marks)
    part("/mcp's legacy ship tools are retired", f5_mcp_ship_tools_retired)
    if FAILS:
        print(f"host-lane-doctrine: {len(FAILS)} FAIL(s)")
        return 1
    print("host-lane-doctrine: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
