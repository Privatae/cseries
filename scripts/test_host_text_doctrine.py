#!/usr/bin/env python3
"""Gate: what the Host reads at the moment of failure teaches the agent rule.

The Host calls an agent ONLY for untrusted work outside
the platform (the open web, foreign repos, other people's and Hosts' content).
Everything in-platform it does itself, directly.

The lanes and the dispatch door say that (test_host_lane_doctrine.py). The
strings the Host reads when a call is refused still said the old rule:
operate's refusal, the read door's and a by-name call's each offered "dispatch
an agent" for any tool that was not an operate op (live on one Host: app_probe,
image_generate, and agent_status, a verb), and 37 operate cards carried "Only
these ops are read-safe and run without a dispatch" (gitea's included, so a
PR read as an agent's job). This gate pins:

  a. no capability card whose lane is read, operate, host or verb has
     "dispatch" in its gotchas (an op enum that names a `dispatch` op is the
     schema's own value, not a door, and is the one exemption);
  b. the operate, read-door and by-name refusals carry no dispatch offer for
     any tool whose lane is not `dispatch`;
  c. the other Host-facing strings: the prompt's agent count
     counts what the dispatch door admits, the read-only note, the grant
     refusal, the claim-guard repair, the kernel_op refusal, the MCP skill
     note, the picture a Host can look at, and the skill_memory refusal.

Pure imports with stubbed pools; no DB, no container, no network.
Run: APP_ROOT=<repo> python3 scripts/test_host_text_doctrine.py
"""
import asyncio
import json
import os
import re
import sys
from turn_source import turn_source  # session.py and the modules split out of it
from tools_source import tools_source  # tools.py and the modules split out of it

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


def src(rel: str) -> str:
    with open(os.path.join(APP_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _registry() -> dict:
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS
    from mastercode.runtime.triage import _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS
    out = {}
    for s in (PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS, _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS):
        for sc in s.values():
            n = ((sc or {}).get("function") or {}).get("name")
            if n:
                out[n] = sc
    return out


def _by_lane() -> dict[str, list[str]]:
    from mastercode.runtime.tool_lanes import lane_for
    out: dict[str, list[str]] = {}
    for n, sc in _registry().items():
        out.setdefault(lane_for(n, sc), []).append(n)
    return out


def _offers_agent(text: str) -> bool:
    return "dispatch" in (text or "").lower() or "tool_scope" in (text or "")


# ── a. the capability cards ─────────────────────────────────────────────────

_ENUM_LINE = re.compile(r"^`\w+` must be exactly one of: ")


def a_cards():
    import generate_capabilities as gc
    f = getattr(gc, "host_op_cards", None)
    check("generate_capabilities.host_op_cards builds the cards without the server app", callable(f))
    if not callable(f):
        return
    _ops, cards = f()
    bad = [(c["id"], c.get("lane"), g) for c in cards for g in (c.get("gotchas") or [])
           if c.get("lane") in ("read", "operate", "host", "verb")
           and "dispatch" in g.lower() and not _ENUM_LINE.match(g)]
    check("no read/operate/host/verb card has 'dispatch' in its gotchas", not bad, bad[:6])
    by_id = {c["id"]: c for c in cards}
    gitea = " ".join(by_id.get("gitea", {}).get("gotchas") or [])
    check("the gitea card says every op runs in the Host's own turn",
          "Every op runs in your own turn" in gitea, gitea)
    gh = " ".join(by_id.get("github", {}).get("gotchas") or [])
    check("the github card (the agent lane) says its other ops are untrusted work",
          "untrusted work outside the platform" in gh, gh)
    m = re.search(r'"/api/agents": \[([^\]]*)\]', src("scripts/generate_capabilities.py"))
    check("the /api/agents area does not point the Host at dispatch",
          m is not None and '"dispatch"' not in m.group(1), m.group(0) if m else None)


# ── b. the three refusals ───────────────────────────────────────────────────

def b_operate_refusal():
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS, not_an_op_refusal
    lanes = _by_lane()
    bad = [(n, not_an_op_refusal(n)) for lane, names in lanes.items() if lane != "dispatch"
           for n in names if n not in DIRECT_OP_TOOLS and _offers_agent(not_an_op_refusal(n))]
    check("operate's refusal offers no agent for any tool whose lane is not dispatch",
          not bad, bad[:4])
    gap = not_an_op_refusal("brand_new_tool")
    check("operate's refusal for an unclassified name says gap and offers no agent",
          "gap" in gap and not _offers_agent(gap), gap)
    v = not_an_op_refusal("agent_status")
    check("operate's refusal for a verb says call it directly",
          "call `agent_status` directly" in v and not _offers_agent(v), v)
    gh = not_an_op_refusal("github")
    check("operate's refusal for the agent lane names untrusted work",
          "untrusted work outside the platform" in gh, gh)


def b_read_refusal():
    from mastercode.runtime import tool_doors as TD
    from mastercode.runtime.tool_access import read_safe_ops
    lanes = _by_lane()
    reg = _registry()
    bad = []
    for lane, names in lanes.items():
        if lane == "dispatch":
            continue
        for n in names:
            # Not held on this turn (no read set), so every name reaches the
            # not-advertised branch; a mixed tool is called with a write op.
            ops = read_safe_ops(n, reg[n])
            rso = {n: ops} if ops else {}
            r = TD.read_refusal({"tool": n, "args": {"op": "zz_write"}}, None,
                                read_safe_tools=set(), read_safe_ops=rso)
            if r and (_offers_agent(r.get("result")) or r.get("use_instead") == "dispatch"):
                bad.append((n, lane, r))
    check("the read door's refusal offers no agent for any tool whose lane is not dispatch",
          not bad, bad[:4])
    r = TD.read_refusal({"tool": "agent_status", "args": {"task_id": 442}}, None,
                        read_safe_tools=set(), read_safe_ops={})
    check("read(tool='agent_status') says call the verb directly (as seen live)",
          bool(r) and "call `agent_status` directly" in r["result"] and not _offers_agent(r["result"]), r)
    r = TD.read_refusal({"tool": "github", "args": {"op": "import"}}, None,
                        read_safe_tools=set(), read_safe_ops={"github": read_safe_ops("github", reg["github"])})
    check("read(tool='github', op='import') sends the import to an agent as untrusted work",
          bool(r) and "untrusted work" in r["result"] and r.get("use_instead") == "dispatch", r)


def b_by_name_refusal():
    from mastercode.runtime import tool_doors as TD
    f = getattr(TD, "by_name_refusal", None)
    check("tool_doors.by_name_refusal is the by-name refusal", callable(f))
    check("session.py's tool loop answers a by-name call from it",
          "by_name_refusal" in turn_source()
          and "call dispatch with it in tool_scope" not in turn_source())
    if not callable(f):
        return
    lanes = _by_lane()
    bad = [(n, f(n)) for lane, names in lanes.items() if lane != "dispatch" for n in names
           if _offers_agent(f(n).get("result")) or f(n).get("use_instead") == "dispatch"]
    check("a by-name call offers no agent for any tool whose lane is not dispatch", not bad, bad[:4])
    r = f("context", {"context": frozenset({"read", "list"})})
    check("a by-name call to an operate tool names operate and says run it yourself",
          "operate(tool='context'" in r["result"] and "yourself" in r["result"]
          and r.get("use_instead") == "operate", r)
    # The loop runs a held read, a held build door and every verb on the list
    # by name before this refusal, so a read or a verb that reaches it is one
    # the turn does not hold: naming its door again would only be refused.
    for n in ("check_page", "read", "find_tools"):
        r = f(n)
        check(f"a by-name call to an unheld `{n}` is not sent back to a door that refuses it",
              "find_tools(query=" in r["result"] and "read(tool=" not in r["result"]
              and f"call `{n}` directly" not in r["result"] and "use_instead" not in r, r)
    r = f("totally_invented_tool")
    check("an invented name is said as no tool, with find_tools, and no agent",
          r.get("reason") == "unknown_name" and "find_tools" in r["result"]
          and not _offers_agent(r["result"]), r)


# ── c. the other strings ────────────────────────────────────────────────────

def c_prompt_count():
    from mastercode.runtime.session import _build_orchestrator_prompt
    from mastercode.runtime.scoped_agent import agent_reader_tools
    reg = _registry()
    caps = [{"type": "function", "function": sc["function"]} for sc in reg.values()]
    want = len(set(agent_reader_tools()) & set(reg))
    p = _build_orchestrator_prompt(caps)
    check("the 'Your tools' block no longer counts the dispatch lane as 'tools an agent can hold'",
          "tools an agent can hold" not in p, p[:600])
    check(f"it counts what the dispatch door admits ({want} untrusted readers)",
          f"{want} of these read untrusted content from outside the platform" in p, p[:800])


def c_read_only_note():
    from mastercode.runtime.tool_access import read_only_variant
    reg = _registry()
    d = read_only_variant("github", reg["github"])["function"]["description"]
    check("github's read-only note sends its other ops to an agent as foreign_repo work",
          "dispatch those" not in d and "work='foreign_repo'" in d, d[-300:])


def c_grant_refusal():
    from mastercode.runtime.agent_grant import parse_grant, unreachable_tools
    from mastercode.runtime.scoped_agent import dispatch_precheck, unreachable_grant_refusal
    # The door refuses this mixed scope earlier (an in-platform tool beside a
    # reader); the grant text is the run-time wall's, the same
    # helper the door uses for a reader-only scope.
    _scope = {"web_search", "context"}
    _bits = parse_grant(["read_context"])[0]
    msg = unreachable_grant_refusal(unreachable_tools(_scope, _bits), _scope, _bits,
                                    at_door=False)
    check("the grant refusal leads with doing in-platform work yourself, not widening the grant",
          bool(msg) and "do that part yourself" in msg and "Fix the grant and dispatch again" not in msg,
          msg)
    # The retry line is what the Host acts on mechanically. It must not add a
    # capability for the in-platform tool the text says to drop (context:
    # read_memory, write_context), and the retried scope leaves it out.
    tail = (msg or "").split("Retry with grant: ", 1)
    grant = json.loads(tail[1].strip()) if len(tail) == 2 else None
    check("the retry line drops the in-platform tool and widens only for the untrusted reader",
          'Retry with tool_scope: ["web_search"]' in (msg or "")
          and grant == ["read_context", "external_api"], msg)
    only = dispatch_precheck(["read_url"], ["read_context"])
    tail = (only or "").split("Retry with grant: ", 1)
    check("an untrusted reader alone still gets its missing capability added",
          len(tail) == 2 and json.loads(tail[1].strip()) == ["read_context", "external_api"]
          and "Retry with tool_scope" not in (only or ""), only)
    s = src("brain/mastercode/runtime/scoped_agent.py")
    check("the run-time grant wall says the same (one helper)",
          s.count("unreachable_grant_refusal(") >= 3, s.count("unreachable_grant_refusal("))
    check("no scope refusal sends the Host to 'your map' for tool names",
          "as your map" not in s)


def c_claim_guard():
    from mastercode.runtime.claim_guard import find_unbacked_start

    class _Pool:
        async def fetchval(self, *a, **k):
            return None

    out = asyncio.run(find_unbacked_start(_Pool(), "On it — the fix goes out on kimi now.", 3))
    check("the claim-guard repair offers read/operate for platform work, dispatch only for untrusted reading",
          bool(out) and "anywhere: dispatch" not in out and "read or operate in this turn" in out
          and "untrusted" in out, (out or "")[-700:])


def c_kernel_op():
    from mastercode.runtime.kernel_ops import run_kernel_op
    r = asyncio.run(run_kernel_op({"method": "GET", "path": "/api/zz-not-a-route"}))
    check("kernel_op's not-exposed refusal offers no agent",
          r.get("status") == "denied" and not _offers_agent(r.get("result")), r)


def c_mcp_skill_note():
    from mastercode.runtime.tool_doors import DoorCatalog
    cat = DoorCatalog([], read_safe_tools=(), read_safe_ops={})
    cat.add_mcp([{"name": "blender", "state": "missing",
                  "tools": [{"name": "mcp:blender/render", "description": "Render.",
                             "parameters": {"type": "object", "properties": {}}}]}])
    e = cat.entries.get("mcp:blender/render")
    note = (e.note if e else "") or ""
    check("an MCP server without its skill names the context write the Host makes itself",
          "operate(intent, tool='context'" in note and "skills/mcp-blender" in note, note)


def c_picture():
    import server.routes.context_extract as ce
    from mastercode.image_engine import LOOKABLE_IMAGE_TYPES, _IMAGE_MAGIC, _sniff_image_type
    samples = {ct: magic + b"\0" * 16 for magic, ct in _IMAGE_MAGIC}
    samples["image/webp"] = b"RIFF\0\0\0\0WEBP" + b"\0" * 16
    check("every lookable type has bytes _sniff_image_type answers with it (pairing)",
          set(samples) == set(LOOKABLE_IMAGE_TYPES)
          and all(_sniff_image_type(b) == ct for ct, b in samples.items()), sorted(samples))

    async def _none(*a, **k):
        return []

    real = ce.machines_for
    ce.machines_for = _none
    try:
        for ct in sorted(LOOKABLE_IMAGE_TYPES):
            m = asyncio.run(ce.no_reader_message(object(), 3, "image.png", ct, 77))
            check(f"a stored {ct} is a picture the Host looks at itself",
                  "'action': 'look'" in m and "file:77" in m and not _offers_agent(m), m)
        m = asyncio.run(ce.no_reader_message(object(), 3, "logo.svg", "image/svg+xml", 78))
        check("an SVG (not lookable) is not sent to look", "'look'" not in m, m)
    finally:
        ce.machines_for = real


def c_skill_memory():
    s = tools_source()
    i = s.find("is a stored skill (skill_memory)")
    seg = s[i:i + 700]
    check("memory_curate's skill_memory refusal names the next step in this turn",
          i > 0 and "memory_curate op='add'" in seg, seg)


def c_person_only_memory():
    """Retiring a Training Memory rule and removing a stored skill
    (skill_memory) stay the person's: what the Host reads says to raise them
    through escalate, never that it can retire them itself."""
    reg = _registry()
    tm = reg["training_memory"]["function"]["description"]
    check("training_memory's description says retiring a rule is the person's, through escalate",
          "retiring one is the person's" in tm and "escalate" in tm, tm)
    skill = src("default-skills/memory.md")
    check("the memory skill sends a Training Memory rule's retirement to the person through escalate",
          "read-only to you: nothing you call changes or retires one" in skill
          and "Retiring it is the user's decision" in skill, skill[-1200:])
    check("the memory skill sends a skill_memory change to the person through escalate",
          "should change or go, raise it with the user through `escalate`" in skill)
    s = tools_source()
    i = s.find("is a stored skill (skill_memory)")
    seg = s[i:i + 900]
    check("memory_curate's skill_memory refusal says removing the row is the user's, through escalate",
          i > 0 and "raise it with them through escalate" in seg, seg)


def c_turn_verbs():
    from mastercode.runtime.session import _build_host_orchestration_tools
    from mastercode.runtime.tool_doors import search_door_tools
    from mastercode.runtime.tool_lanes import TURN_VERBS
    names = {s["function"]["name"] for s in _build_host_orchestration_tools() + search_door_tools()}
    check("TURN_VERBS covers every verb and search door on the turn's list",
          names <= TURN_VERBS, sorted(names - TURN_VERBS))


def main() -> int:
    part("a. capability cards", a_cards)
    part("b. operate's refusal", b_operate_refusal)
    part("b. the read door's refusal", b_read_refusal)
    part("b. a by-name call's refusal", b_by_name_refusal)
    part("c. the prompt's agent count", c_prompt_count)
    part("c. the read-only note", c_read_only_note)
    part("c. the grant refusal", c_grant_refusal)
    part("c. the claim-guard repair", c_claim_guard)
    part("c. kernel_op", c_kernel_op)
    part("c. the MCP skill note", c_mcp_skill_note)
    part("c. a picture the Host can see", c_picture)
    part("c. the skill_memory refusal", c_skill_memory)
    part("c. the turn's verbs", c_turn_verbs)
    part("c. person-only memory changes", c_person_only_memory)
    if FAILS:
        print(f"host-text-doctrine: {len(FAILS)} FAIL(s)")
        return 1
    print("host-text-doctrine: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
