#!/usr/bin/env python3
"""Instructions the Host reads must name calls it can actually make.

Some instructions told the Host to make calls it could not make. Measured in
one Host's thread:

  - every pasted picture became a note telling the Host to "call
    image_generate action='understand' image_b64='last_attachment'". The op
    existed, but the Host had no door to it from its own turn: `read` refused
    it (not a read op) and `operate` excluded image_generate (direct_ops). So
    the note sent the Host into a wall, and it dispatched agents to see for it;
  - a since-retired proposal tool's own schema gave 'docs/native-bundle-note'
    as its branch_slug example; the controller refuses a slash, and an agent copied
    the example's shape. That tool is retired; the
    rule stays for every other pattern.

Three rules, each a class rather than an instance:

  A. A skill that names `<tool> action|op='<x>'` names an x the tool's own
     enum for that field lists (a removed op is a wall, not an instruction).
  B. A note the platform puts INTO the Host's own turn (a picture note, an
     app image's note, the image engine's answers) names only calls the Host
     can make from that turn: the `read` door admits the op
     (tool_access.read_safe_ops — what the door itself checks) or `operate`
     runs it (direct_ops). A mention followed by "was removed" is a
     tombstone, not an instruction.
  C. A schema example ("e.g. '<x>'") for a property that declares a pattern
     matches that pattern.

Rule B also holds for every default skill (nothing checked skill text against
the code, so skills kept teaching agent-only paths and calls no door runs). A
skill is read by the Host in its own turn, so every call it names must be one
that turn can make:

  B1. `<tool> op='<x>'` names a real tool (the registry or the bridge
      catalog), and the lane tool_lanes.lane_for gives it runs that op from
      the Host's turn: a read the `read` door admits, an `operate` op, a host
      door or a turn verb. An op only an agent runs is named only as an
      agent's scope (a line about tool_scope or dispatch). A retired tool is
      never named as a call.
  B2. A call written by bare name, `tool(...)`, is one the Host's turn runs by
      that name: a held read (what tool_doors.read_admits admits), a host door,
      a turn verb, or a root-thread tool inside the root thread. Anything else
      is refused by name at run time (tool_doors.by_name_refusal) and is
      written through its door: `operate(tool='x', args={…})`. Measured: 66
      such calls in 31 skills, connectors-telegram's
      `propose_nac_policy(kind='connector', …)` among them.
  B3. A `tool_scope` a skill writes out is one the dispatch door admits
      (scoped_agent.scope_work_refusal): the skill never teaches a scope the
      door refuses.
  B4. A call written through a door, `read(tool='x', args={op:'y'})` or
      `operate(tool='x', …)`, names a real tool, an op its enum lists, and the
      door that runs it: `read` only for a read, `operate` only for an
      operate op (or a connected server's mcp:<server>/<tool>).
  B5. Every tool a default skill writes as a call (B1's `tool op='x'`, B2's
      `tool(...)`, B4's door target) is on the skill's `allowed-tools`, the
      record skill_standard's `tools-declared` rule keeps for a Host's own
      skills (network.md once called dcn, keys_manage and gitea without
      listing them).

Two tightenings: B1 refuses a name no registry knows when it is written as a
call (`foo(op='x')`), even with no `_` or `.` in it (prose "with op='x'" still
passes); and B2 checks a root-only tool (root_turn.ROOT_ONLY_TOOLS,
machine_exec) is called only in root-thread context: the section it sits in
(from the heading above it to the next one) or the skill's own `description`
names the root thread.

Each rule is proved to bite on the measured defect (the old note, the old
example) before it is run on the tree.

  PYTHONPATH=brain python3 scripts/test_instructions_name_real_calls.py
"""
import ast
import glob
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))
sys.path.insert(0, str(REPO))

from mastercode.public_tool_registry import (  # noqa: E402
    OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS,
)
from mastercode.runtime import direct_ops, tool_access  # noqa: E402
from mastercode.runtime import root_turn, scoped_agent, tool_doors, tool_lanes, triage  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail="") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {detail}"))
    if not ok:
        FAILS.append(name)


REG = {**OPERATOR_PLATFORM_TOOL_SCHEMAS, **PUBLIC_TOOL_SCHEMAS}
# `tool(action='x'` and `tool action='x'`, with or without backticks.
MENTION = re.compile(r"\b([a-z][a-z0-9_.]*)`?\s*\(?\s*(action|op)\s*=\s*['\"]([a-z_]+)['\"]")


def _props(schema: dict) -> dict:
    return ((schema.get("function") or {}).get("parameters") or {}).get("properties") or {}


def enum_violations(text: str, reg: dict) -> list[str]:
    """Rule A on one text: mentions of a registry tool's enumerated field whose
    value the enum does not list."""
    out = []
    for m in MENTION.finditer(text):
        tool, key, op = m.groups()
        if tool not in reg:
            continue
        spec = _props(reg[tool]).get(key)
        if not isinstance(spec, dict):
            out.append(f"{tool} has no `{key}` field ({m.group(0)})")
            continue
        if isinstance(spec.get("enum"), list) and op not in spec["enum"]:
            out.append(f"{tool} {key}='{op}' is not one of {spec['enum']}")
    return out


def _tombstone(text: str, end: int) -> bool:
    return bool(re.match(r"[^.\]]{0,40}\bwas removed\b", text[end:end + 60]))


def host_turn_violations(text: str, reg: dict, direct=None) -> list[str]:
    """Rule B on one text shown inside the Host's own turn. `direct` is the
    set of operate tools to judge against; None = today's DIRECT_OP_TOOLS."""
    is_direct = (direct_ops.is_direct_op_tool if direct is None
                 else (lambda t: t in direct))
    out = []
    for m in MENTION.finditer(text):
        if _tombstone(text, m.end()):
            continue
        tool, key, op = m.groups()
        if tool not in reg:
            # "Use action='look'" / "with op='tools'": prose naming the op of
            # the tool the note is about, not a tool. A word shaped like a
            # tool id (snake_case or dotted) that the registry does not hold
            # IS a bad instruction.
            if "_" in tool or "." in tool:
                out.append(f"{m.group(0)}: no tool named {tool!r}")
            continue
        reads = tool_access.read_safe_ops(tool, reg[tool])
        operate_ok = (is_direct(tool)
                      and op not in direct_ops.direct_op_excluded_ops(tool))
        if not (tool_access.is_read_safe_tool(tool, reg[tool]) or op in reads or operate_ok):
            out.append(f"{tool} {key}='{op}': neither the read door (reads {sorted(reads)}) "
                       f"nor operate runs it from the Host's own turn")
    return out


def strings_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append("".join(v.value for v in node.values
                               if isinstance(v, ast.Constant) and isinstance(v.value, str)))
    return out


# ── The rules bite on the measured defects ──────────────────────────────────
OLD_NOTE = ("[The person attached image.png (image/png); the model answering this turn does "
            "not read pictures. To look at it, call image_generate action='understand' "
            "image_b64='last_attachment'.]")
old_reg = {**REG, "image_generate": {"function": {"name": "image_generate", "parameters": {
    "type": "object", "properties": {"action": {
        "type": "string", "enum": ["generate", "understand", "gallery"]}}}}}}
# Judged with the lanes of its day: image_generate later became an operate op
# (the Host calls an agent only for untrusted work), and until then
# `understand` was reachable from no door of the Host's turn.
old_direct = set(direct_ops.DIRECT_OP_TOOLS) - {"image_generate"}
check("B bites: the old picture note named a call the Host's turn could not make",
      bool(host_turn_violations(OLD_NOTE, old_reg, old_direct)),
      host_turn_violations(OLD_NOTE, old_reg, old_direct))
check("A bites: a skill naming an op its tool does not list",
      bool(enum_violations("judge it with `image_generate(action='understand')`", REG)))
check("B passes a tombstone ('... was removed')",
      not host_turn_violations("action='understand' was removed 2026-10-12", REG))
check("B passes the call that replaced it",
      not host_turn_violations("look with image_generate(action='look', images=[...])", REG),
      host_turn_violations("look with image_generate(action='look', images=[...])", REG))

# ── A: every skill ──────────────────────────────────────────────────────────
skill_bad = []
for f in sorted(glob.glob(str(REPO / "default-skills" / "*.md"))):
    for i, line in enumerate(open(f), 1):
        for v in enum_violations(line, REG):
            skill_bad.append(f"{Path(f).name}:{i}: {v}")
check("A: every tool op a skill names is one its tool lists", not skill_bad, skill_bad)

# ── B: what the platform writes into the Host's own turn ────────────────────
HOST_TURN_SOURCES = [
    "brain/mastercode/attachment_parts.py",   # picture and document notes
    "server/mcp_connections.py",              # an app image's note
    "brain/mastercode/image_engine.py",       # look / gallery answers
    "brain/mastercode/runtime/turn_pictures.py",
    "brain/mastercode/runtime/diagnostics.py",  # the "needs attention" block
]
turn_bad = []
for rel in HOST_TURN_SOURCES:
    for s in strings_of(REPO / rel):
        for v in host_turn_violations(s, REG):
            turn_bad.append(f"{rel}: {v}")
check("B: every call a note in the Host's turn names is one it can make there",
      not turn_bad, turn_bad)

# ── B for skills: every call a skill names is one the Host's turn can make ──
CATALOG = {**REG}
for _d in (getattr(triage, "_BRIDGE_TOOL_SCHEMAS", {}), getattr(triage, "_BOT_TOOL_SCHEMAS", {})):
    for _k, _v in _d.items():
        CATALOG.setdefault(_k, _v)
# `tool op='x'` / `tool(op='x'`: the tool name ends in a letter or digit and
# is set off by a space or a paren, so `service_op='up'` (an argument) and
# `window. op='x'` (prose) are not read as tools.
SKILL_MENTION = re.compile(
    r"(?<![\w.'\"=])([a-z][a-z0-9_.]*[a-z0-9])`?(?:\s+|\s*\(\s*)(action|op)\s*=\s*['\"]([a-z_]+)['\"]")
BARE_CALL = re.compile(r"(?<![\w.'\"=])([a-z][a-z0-9_.]*[a-z0-9])\(")
CALL_SELECTOR = re.compile(r"\b(action|op|operation|method|command|mode)\s*[=:]\s*['\"]?([A-Za-z_]+)")
DOOR_CALL = re.compile(r"\b(read|operate)\((?:\s*intent=(?:'[^']*'|\"[^\"]*\"|[^,]*),)?\s*tool='([^']+)'")
SCOPE = re.compile(r"tool_scope`?\s*[=:]\s*`?\[([^\]]*)\]")
AGENT_LINE = re.compile(r"tool_scope|dispatch|\bagent")
# Called by name in a Host turn without being registry tools: the turn's own
# verbs and doors (session.py runs them; turn_types.ALWAYS_ALLOWED) and the
# search over the catalog.
from mastercode.runtime.turn_types import ALWAYS_ALLOWED as _ALWAYS  # noqa: E402
TURN_VERBS = frozenset(_ALWAYS) | {"read", "find_tools"}
ROOT_CONTEXT = re.compile(r"root thread", re.I)
HEADING = re.compile(r"^#{1,6}\s", re.M)


def _description(text: str) -> str:
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    d = re.search(r"^description:\s*(.*)$", m.group(1), re.M)
    return d.group(1) if d else ""


def _section(text: str, pos: int) -> str:
    """The text from the heading above `pos` to the next heading."""
    starts = [h.start() for h in HEADING.finditer(text)]
    s = max([0] + [h for h in starts if h <= pos])
    e = min([len(text)] + [h for h in starts if h > pos])
    return text[s:e]


def root_context(text: str, pos: int) -> bool:
    """B2's root-thread context for a call at `pos`: its section, or the
    skill's declared description, names the root thread."""
    return bool(ROOT_CONTEXT.search(_section(text, pos)) or ROOT_CONTEXT.search(_description(text)))


def called_tools(text: str, cat: dict) -> dict[str, int]:
    """{tool: first line} for every catalog tool `text` writes as a call: B1's
    `tool op='x'` (a tombstone and an agent's scope on a dispatch line left
    out), B2's `tool(...)`, B4's door target."""
    out: dict[str, int] = {}

    def add(tool: str, pos: int) -> None:
        if tool in cat and tool not in out:
            out[tool] = text.count("\n", 0, pos) + 1

    for m in SKILL_MENTION.finditer(text):
        if _tombstone(text, m.end()):
            continue
        tool = m.group(1)
        if tool in cat and tool_lanes.lane_for(tool, cat[tool]) == "dispatch":
            s = text.rfind("\n", 0, m.start()) + 1
            e = text.find("\n", m.start())
            if AGENT_LINE.search(text[s:e if e >= 0 else len(text)]):
                continue
        add(tool, m.start())
    for m in BARE_CALL.finditer(text):
        add(m.group(1), m.start())
    for m in DOOR_CALL.finditer(text):
        add(m.group(2), m.start())
    return out


def _call_span(text: str, at: int) -> str:
    """The parenthesised argument text starting at `at` (an open paren)."""
    depth = 0
    for j in range(at, min(len(text), at + 800)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[at:j + 1]
    return text[at:at + 300]


def _runs_as_read(tool: str, args: dict, schema: dict) -> bool:
    lane = tool_lanes.lane_for(tool, schema)
    whole = {tool} if (lane == "read" or tool_access.is_read_safe_tool(tool, schema)) else set()
    return tool_doors.read_admits(tool, args, whole, {tool: tool_access.read_safe_ops(tool, schema)})


def skill_call_violations(text: str, cat: dict) -> list[str]:
    """B1-B3 on one skill's text. Each violation carries its line number."""
    out = []

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    def line_text(pos: int) -> str:
        s = text.rfind("\n", 0, pos) + 1
        e = text.find("\n", pos)
        return text[s:e if e >= 0 else len(text)]

    for m in SKILL_MENTION.finditer(text):
        if _tombstone(text, m.end()):
            continue
        tool, key, op = m.groups()
        if tool in tool_lanes.RETIRED_TOOLS:
            out.append(f"{line_of(m.start())}: {m.group(0)}: {tool} is retired")
            continue
        if tool not in cat:
            # Prose ("with op='tools'") names an op, not a tool. A name
            # shaped like a tool id, or one written as a call, is a tool.
            as_call = "(" in m.group(0)
            if ("_" in tool or "." in tool or as_call) and tool not in TURN_VERBS:
                out.append(f"{line_of(m.start())}: {m.group(0)}: no tool named {tool!r}")
            continue
        lane = tool_lanes.lane_for(tool, cat[tool])
        if lane in ("host", "verb") or _runs_as_read(tool, {key: op}, cat[tool]):
            continue
        if lane == "operate" and op not in direct_ops.direct_op_excluded_ops(tool):
            continue
        if lane == "dispatch" and AGENT_LINE.search(line_text(m.start())):
            continue
        out.append(f"{line_of(m.start())}: {m.group(0)}: lane {lane!r} — no door of the Host's own "
                   f"turn runs {key}='{op}'")

    for m in BARE_CALL.finditer(text):
        tool = m.group(1)
        if tool in tool_lanes.RETIRED_TOOLS:
            out.append(f"{line_of(m.start())}: {tool}(...): {tool} is retired")
            continue
        if tool in root_turn.ROOT_ONLY_TOOLS:
            if not root_context(text, m.start()):
                out.append(f"{line_of(m.start())}: {tool}(...) outside root-thread context — it runs "
                           "only in the root thread; say so where it is called")
            continue
        if tool not in cat:
            continue
        lane = tool_lanes.lane_for(tool, cat[tool])
        if lane in ("host", "verb"):
            continue
        args = {k: v for k, v in CALL_SELECTOR.findall(_call_span(text, m.end() - 1))}
        if _runs_as_read(tool, args, cat[tool]):
            continue
        out.append(f"{line_of(m.start())}: {tool}(...) by name — lane {lane!r} is refused by name; "
                   f"write it operate(tool='{tool}', args={{…}})")

    for m in DOOR_CALL.finditer(text):
        door, tool = m.groups()
        if tool.startswith("mcp:") or "<" in tool:
            continue
        if tool not in cat:
            out.append(f"{line_of(m.start())}: {door}(tool='{tool}'): no tool named {tool!r}")
            continue
        args = {k: v for k, v in CALL_SELECTOR.findall(_call_span(text, m.start() + len(door)))}
        props = _props(cat[tool])
        for k, v in args.items():
            enum = (props.get(k) or {}).get("enum") if isinstance(props.get(k), dict) else None
            if isinstance(enum, list) and v not in enum and v.upper() not in enum:
                out.append(f"{line_of(m.start())}: {door}(tool='{tool}') {k}='{v}' is not one of {enum}")
        lane = tool_lanes.lane_for(tool, cat[tool])
        if door == "read" and not _runs_as_read(tool, args, cat[tool]):
            out.append(f"{line_of(m.start())}: read(tool='{tool}', {args}) — the read door refuses it "
                       f"(lane {lane!r})")
        if door == "operate" and not (lane == "operate" and not (
                set(args.values()) & set(direct_ops.direct_op_excluded_ops(tool)))):
            out.append(f"{line_of(m.start())}: operate(tool='{tool}') — lane {lane!r}, operate does not run it")

    for m in SCOPE.finditer(text):
        names = re.findall(r"['\"]([A-Za-z0-9_.:/-]+)['\"]", m.group(1))
        if names and scoped_agent.scope_work_refusal(names) is not None:
            out.append(f"{line_of(m.start())}: tool_scope {names} is refused at the dispatch door")
    return out


# The rules bite on the measured skill text.
for label, old in (
        ("B2 bites: connectors-telegram's bare pairing call",
         "4. **Start the pairing.** `propose_nac_policy(kind='connector',\n"
         "   app_id='telegram', platform='telegram', token_name='telegram-bot')`."),
        ("B2 bites: mcp.md's bare skill write",
         "Write it with\n`context(action='write', path='skills/mcp-<name>', content=…)`."),
        ("B3 bites: a scope of platform-code reads",
         "- `tool_scope`: `[\"code_grep\", \"code_read\"]`"),
        ("B1 bites: an op no door of the Host's turn runs",
         "copy it with `github op='import'`, then read it"),
        ("B4 bites: an agent-only tool through operate",
         "`operate(tool='github', args={op:'import', repo:'a/b'})`"),
        ("B4 bites: a write through the read door",
         "`read(tool='context', args={action:'write', path:'skills/x'})`"),
        ("B1 bites: an unknown name written as a call, with no '_' or '.'",
         "then `frobnicate(op='run')` the result"),
        ("B2 bites: a root-only tool called outside root-thread context",
         "## Fixing a box\n\nRun `machine_exec(machine='box-a', command='ls')` to check.")):
    check(label, bool(skill_call_violations(old, CATALOG)), skill_call_violations(old, CATALOG))
for label, ok_text in (
        ("B passes the door form", "`operate(tool='context', args={action:'write', path:'skills/x'})`"),
        ("B passes a read called by name", "read its tools with `mcp_connections(op='tools', name='x')`"),
        ("B passes a host door and a root-thread tool",
         "`build(action='continue', message=m)`; in the root thread `machine_exec(machine, command)`"),
        ("B passes an agent's scope naming its reader", "dispatch with `github op='import'` in tool_scope"),
        ("B passes prose naming an op (no call)", "Use it with op='tools' to list them."),
        ("B passes a root-only tool in a root-thread section",
         "## Root\n\nIn your root thread:\n\n- run `machine_exec(machine, command)` once.")):
    check(label, not skill_call_violations(ok_text, CATALOG), skill_call_violations(ok_text, CATALOG))

skill_turn_bad = []
for f in sorted(glob.glob(str(REPO / "default-skills" / "*.md"))):
    for v in skill_call_violations(Path(f).read_text(), CATALOG):
        skill_turn_bad.append(f"{Path(f).name}:{v}")
check("B: every call a default skill names is one the Host's own turn can make",
      not skill_turn_bad, skill_turn_bad)


def undeclared_calls(text: str, cat: dict) -> list[str]:
    """B5 on one skill: the tools it calls that its allowed-tools omits."""
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    raw = re.search(r"^allowed-tools:\s*(.*)$", m.group(1), re.M) if m else None
    allowed = {t.strip() for t in (raw.group(1) if raw else "").split(",") if t.strip()}
    return [f"{ln}: {t}" for t, ln in sorted(called_tools(text, cat).items(), key=lambda kv: kv[1])
            if t not in allowed]


check("B5 bites: network.md's dcn call before dcn was declared",
      bool(undeclared_calls("---\nname: x\nallowed-tools: get_network\n---\n"
                            "run `dcn(op='scan')` first", CATALOG)))
check("B5 passes a declared call and a dispatch scope",
      not undeclared_calls("---\nname: x\nallowed-tools: dcn, dispatch\n---\n"
                           "run `dcn(op='scan')`; dispatch with `github op='import'` in tool_scope",
                           CATALOG))
undeclared_bad = []
for f in sorted(glob.glob(str(REPO / "default-skills" / "*.md"))):
    for v in undeclared_calls(Path(f).read_text(), CATALOG):
        undeclared_bad.append(f"{Path(f).name}:{v}")
check("B5: every tool a default skill calls is on its allowed-tools", not undeclared_bad, undeclared_bad)

# ── C: schema examples match the schema's own pattern ───────────────────────
EXAMPLE = re.compile(r"e\.g\.\s*'([^']+)'")


def example_violations(schemas: dict) -> list[str]:
    out = []
    for tname, schema in schemas.items():
        for pname, spec in _props(schema).items():
            if not isinstance(spec, dict) or not spec.get("pattern"):
                continue
            for ex in EXAMPLE.findall(str(spec.get("description") or "")):
                if not re.fullmatch(spec["pattern"], ex):
                    out.append(f"{tname}.{pname}: example {ex!r} fails {spec['pattern']}")
    return out


old_slug = {"retired_proposal_tool": {"function": {"parameters": {"properties": {"branch_slug": {
    "type": "string", "pattern": "^[a-z][a-z0-9-]{0,40}$",
    "description": "Branch-name suffix. e.g. 'docs/native-bundle-note'"}}}}}}
check("C bites: the old branch_slug example against the controller's pattern",
      bool(example_violations(old_slug)))
from mastercode.runtime import triage  # noqa: E402
all_schemas = {**REG}
for d in (getattr(triage, "_BRIDGE_TOOL_SCHEMAS", {}), getattr(triage, "_BOT_TOOL_SCHEMAS", {})):
    for k, v in d.items():
        all_schemas.setdefault(k, v)
ex_bad = example_violations(all_schemas)
check("C: every schema example matches its property's pattern", not ex_bad, ex_bad)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
sys.exit(1 if FAILS else 0)
