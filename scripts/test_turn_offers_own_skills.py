#!/usr/bin/env python3
"""Gate: a Host's turn is offered the Host's OWN skills that match the message.

THE BUG (measured 2026-10-16, benchmark run 2, two attempts). A Host wrote its
own skill `skills/port` (host_skills) after a porting job, then got the same
kind of job again. Its lead turn called find_tools by exact names (task_record,
workspace) and planned at once. It never called `recall`, which on this branch
has a skills store and would have listed the skill. The store existed; the Host
did not search it before planning.

THE FIX. Beside the memories the pre-fire pushes into every turn, the turn's
context carries at most two lines for the Host's own skills that match the
person's message (memory_retrieval.turn_own_skills / format_turn_skills, wired
in session.py once the turn's DoorCatalog exists). Each line is the name, its
one line and the call that opens it, never the skill's text.

What this ratchets:
  A. the benchmark's own message, on a Host whose own skill `port` carries the
     benchmark's own description, offers the port line with its open call,
     and never the skill's text;
  B. an unrelated message ("what's the weather like") offers nothing, and so
     does a message that shares one incidental word with the description
     (the floor: TURN_SKILLS_FLOOR and the name rule);
  C. a retired skill is never offered (control: the same row active is);
  D. the platform's skills are never offered, even one the message names;
     a skill the person wrote is (author 'person');
  E. at most TURN_SKILLS_MAX lines, however many skills match;
  F. a typed turn that cannot read skills gets none: a type that narrows
     memory or projects (recall's rule, _type_reads_store), and a type whose
     groups cannot open `context` (the turn's DoorCatalog.admitted_step);
     controls: a type holding `context`'s group, and an untyped turn, get it;
  G. session.py offers them from the turn's own catalog and filter, into the
     dynamic (per-turn) message, logged and counted with the late blocks, and
     not on the root turn, under a 2 s cap inside a try whose except only
     logs;
  H. the Host's own skills unreadable: the offer is [] and nothing raises;
  I. the storage skill and the Skills wiki page say what the turn carries,
     with TURN_SKILLS_MAX's number.

The Host's skills come from a stand-in pool that answers host_skills.list_skills'
query; the platform's are the real default-skills/ folder.

Container-free:
  python3 scripts/test_turn_offers_own_skills.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT), str(ROOT / "brain"), str(ROOT / "server")):
    if p not in sys.path:
        sys.path.insert(0, p)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from mastercode import memory_retrieval as mr  # noqa: E402
from mastercode.runtime import skill_loader  # noqa: E402
from mastercode.runtime.turn_types import context_filter  # noqa: E402

E = 7
BODY_MARK = "BODY-TEXT-NEVER-IN-THE-TURN"
BENCH_MSG = ("Port the Python library markdown-it-py to Go. For this task, finishing sooner "
             "matters more than cost.")
# The benchmark Host's own skill, as it wrote it (description and allowed tools).
PORT_DESC = ("Porting or rewriting a codebase into another language. Decide first between one "
             "thread and many (one is the default and was cheaper on the one measured job), then "
             "build a compiling skeleton, fill it in from the leaves up, check each layer against "
             "the original implementation, and finish with the original's tests and the "
             "conformance runner.")
PORT_OPEN = 'read(tool=\'context\', args={"action":"read","path":"skills/port"})'


def _row(name, status, description, *, author="host", version=1,
         tools=("workspace", "task_record", "context", "recall")):
    return {"id": version, "entity_id": E, "name": name, "version": version,
            "body": f"## Steps\n{BODY_MARK} {name}", "description": description,
            "allowed_tools": list(tools), "mcp_server": None, "sources": "[]", "author": author,
            "thread_id": None, "session_id": None, "edited_from": None, "status": status,
            "created_at": None, "superseded_at": None, "versions": version}


PORT = _row("port", "active", PORT_DESC, version=2)
# Retired, and the benchmark message names it (port, go).
PORT_GO = _row("port-go", "retired", "Port a Python package to Go with goroutines.", version=3)


class Pool:
    def __init__(self, rows):
        self.rows, self.asked = rows, []

    async def fetch(self, sql, *args):
        self.asked.append((sql, args))
        if "FROM host_skills" not in sql:
            raise AssertionError(f"unexpected query: {sql[:120]}")
        return [dict(r) for r in self.rows]


skill_loader.get_location = lambda: ROOT / "default-skills"


def _door_catalog(turn_type):
    """The turn's DoorCatalog as session.py builds it, over the public
    registry (scripts/test_recall_finds_own_skills.py's construction)."""
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG
    from mastercode.runtime import tool_doors as TD
    from mastercode.runtime.tool_access import read_safe_ops
    from mastercode.runtime.tool_lanes import lane_for
    caps = [{"type": "function", "function": P_REG[n]["function"]} for n in P_REG
            if not n.startswith("_meta_") and lane_for(n, P_REG[n]) != "retired"]
    rst = {s["function"]["name"] for s in caps if lane_for(s["function"]["name"], s) == "read"}
    rso = {}
    for s in caps:
        n = s["function"]["name"]
        if n not in rst and read_safe_ops(n, s):
            rso[n] = read_safe_ops(n, s)
    return TD.DoorCatalog(caps, read_safe_tools=rst, read_safe_ops=rso, turn_type=turn_type)


_offer = getattr(mr, "turn_own_skills", None)
_render = getattr(mr, "format_turn_skills", None)


async def turn_context(rows, message, *, turn_type=None):
    """What session.py adds to the turn's context for this message: the
    block format_turn_skills renders from turn_own_skills, asked with the
    turn's catalog and the turn type's filter. None on code without them."""
    if _offer is None or _render is None:
        return None
    cat = _door_catalog(turn_type)
    hits = await _offer(Pool(rows), E, message, context=context_filter(turn_type),
                        skill_step=cat.admitted_step)
    return _render(hits)


def _lines(block):
    return [ln for ln in (block or "").splitlines() if ln.startswith("- ")]


async def _run():
    check("memory_retrieval offers the turn its own skills (turn_own_skills, format_turn_skills)",
          _offer is not None and _render is not None)

    # A: the benchmark's message.
    block = await turn_context([PORT, PORT_GO], BENCH_MSG)
    check("A. the benchmark's message offers the Host's own `port` line, with its open call",
          "- port (yours, written by you): Porting or rewriting a codebase" in (block or "")
          and PORT_OPEN in (block or ""), repr(block))
    check("A. ...one line per skill, under one heading, and never the skill's text",
          bool(block) and block.startswith("## ") and len(block.splitlines()) == 1 + len(_lines(block))
          and BODY_MARK not in block, repr(block))

    # B: unrelated messages get none.
    for msg in ("what's the weather like",
                # One word ("between") in common with the port skill's line.
                "Does Privatae see messages between Hosts?"):
        b = await turn_context([PORT], msg)
        check(f"B. {msg!r} offers no skill", b == "", repr(b))

    # C: a retired skill is never offered.
    check("C. a retired skill the message names (`port-go`) is not offered",
          block is not None and "port-go" not in block, repr(block))
    live = await turn_context([PORT, {**PORT_GO, "status": "active"}], BENCH_MSG)
    check("C. control: the same row active IS offered (the check above is not vacuous)",
          "- port-go (yours" in (live or ""), repr(live))

    # D: only the Host's own skills.
    plat = await turn_context([PORT], "run the benchmark on my new model")
    check("D. a platform skill the message names (`benchmark`) is not offered",
          plat == "", repr(plat))
    own_bench = await turn_context([PORT, _row("benchmark", "active", "How I run a model benchmark.")],
                                   "run the benchmark on my new model")
    check("D. control: a `benchmark` the Host wrote itself IS offered for the same message",
          "- benchmark (yours, written by you)" in (own_bench or ""), repr(own_bench))
    by_person = await turn_context([{**PORT, "author": "person"}], BENCH_MSG)
    check("D. a skill the person wrote is offered, said as theirs",
          "- port (yours, written by the user)" in (by_person or ""), repr(by_person))

    # E: at most TURN_SKILLS_MAX.
    many = [PORT] + [_row(f"port-{x}", "active", f"Port a library to {x}.") for x in ("go", "py", "md")]
    mb = await turn_context(many, BENCH_MSG)
    check("E. four matching skills: at most TURN_SKILLS_MAX lines",
          getattr(mr, "TURN_SKILLS_MAX", None) == 2 and len(_lines(mb)) == 2, repr(mb))

    # F: typed turns.
    def _tt(groups, context=None):
        return {"id": "t-f", "name": "Typed", "status": "active", "tool_groups": groups,
                "grant_names": [], "keys": [], "bastions": [], "context": context or {}}
    for label, tt, want in (
            ("a type whose groups cannot open `context`", _tt(["research"]), False),
            ("a type that reads only project 5", _tt(["research", "memory"], {"projects": [5]}), False),
            ("a type that reads no memory", _tt(["research", "memory"], {"memory": "none"}), False),
            ("a closed type", {**_tt(["research", "memory"]), "status": "closed"}, False),
            ("control: a type holding `memory` (context's group)", _tt(["research", "memory"]), True),
            ("control: an untyped turn", None, True)):
        b = await turn_context([PORT], BENCH_MSG, turn_type=tt)
        if want:
            check(f"F. {label}: `port` is offered", PORT_OPEN in (b or ""), repr(b))
        else:
            check(f"F. {label}: no skill is offered", b == "", repr(b))

    # H: the Host's skills unreadable. The offer is []: nothing reaches the
    # turn and nothing raises into it (the platform's half still answers and
    # is filtered out, so this is the authors filter as well as the catch).
    class _DownPool:
        async def fetch(self, sql, *args):
            raise ConnectionError("host_skills unreachable")
    down = None
    try:
        if _offer is not None:
            down = await _offer(_DownPool(), E, BENCH_MSG, context=None,
                                skill_step=_door_catalog(None).admitted_step)
    except Exception as e:  # noqa: BLE001 - the check below names it
        down = e
    check("H. the Host's own skills unreadable: no line, and no error reaches the turn",
          down == [], repr(down))


asyncio.run(_run())

# G: the wiring in the turn.
_src = turn_source(ROOT)
_at = _src.find("turn_own_skills(")
_seg = _src[max(0, _at - 1500):_at + 2200] if _at >= 0 else ""
check("G. session.py offers the turn its own skills (turn_own_skills)", _at >= 0)
# add_discovery runs in EntityRuntime._build_turn_tools, which sits above the
# turn in the file, so its position says nothing about order: the turn's call
# of that method is what has to come before the lookup.
_bt_at = _src.find("    async def _build_turn_tools(")
_bt_end = _src.find("\n    async def ", _bt_at + 1) if _bt_at >= 0 else -1
check("G. ...after the turn's catalog holds the skills (add_discovery comes first)",
      0 <= _bt_at < _src.find("_door_catalog.add_discovery(await discovery_entries(", _bt_at) < _bt_end
      and 0 <= _src.find("= await self._build_turn_tools(") < _at,
      f"build_turn_tools={_bt_at} call={_src.find('= await self._build_turn_tools(')} lookup={_at}")
check("G. ...with the turn's own catalog and turn-type filter, as recall is asked",
      "skill_step=_door_catalog.admitted_step" in _seg
      and 'context=_tt_context_filter(getattr(sess, "turn_type", None))' in _seg)
check("G. ...never on the root turn", "if _door_catalog is not None and not _root_turn:" in _seg)
check("G. ...into the dynamic (per-turn) message, never the cached static one",
      'grok_messages[_dyn_idx]["content"] += "\\n\\n" + _own_skills_block' in _seg
      and "grok_messages[0]" not in _seg[_seg.find("turn_own_skills("):])
check("G. ...logged to prompt_log and counted in the ledger with the late blocks",
      'log_injected_block(self, sess, "own_skills", _own_skills_block)' in _seg
      and '_late_blocks.append(("own_skills", _own_skills_block))' in _seg)
# The lookup runs after the memory pre-fire has built the per-turn half, under
# a 2 s cap, inside a try whose except only logs: a slow or raising lookup
# costs these lines and nothing else.
_call = _seg[_seg.find("_own_skills_block = format_turn_skills("):]
_try = _seg.rfind("try:", 0, _seg.find("_own_skills_block = format_turn_skills("))
_exc = _seg.find("except Exception as _os_err")
check("G. ...under a 2 s cap, and a slow or raising lookup costs only these lines",
      _call.startswith("_own_skills_block = format_turn_skills(await asyncio.wait_for(")
      and "timeout=2.0))" in _call[:600]
      and 0 <= _try < _seg.find("turn_own_skills(", 1500) < _exc
      and _seg[_exc:_seg.find("if _own_skills_block:", _exc)].count("\n") == 2,
      _seg[max(0, _try):_try + 700])

# I: what the turn carries is said where the Host and the person read about
# skills: the context tool's skill (default-skills/storage.md) and the Skills
# wiki page, with the number the code uses.
_max = getattr(mr, "TURN_SKILLS_MAX", None)
_words = {2: "two"}.get(_max, str(_max))
_storage = " ".join((ROOT / "default-skills" / "storage.md").read_text().split())
check("I. the storage skill says a message naming the Host's skill carries one line for it",
      "every word of your skill's name" in _storage and "never its text" in _storage
      and f"at most {_words} skills" in _storage)
_wiki = " ".join((ROOT / "docs" / "wiki" / "pages" / "skills-app.md").read_text().split())
check("I. the Skills wiki page says so, with the same number, and only the Host's or the person's own",
      "every word of the name of one of your Host's own skills" in _wiki
      and f"At most {_words} skills are shown" in _wiki and "only ones your Host or you wrote" in _wiki)

print("\nPASS turn_offers_own_skills" if not fails else f"\nFAIL turn_offers_own_skills: {fails} check(s)")
sys.exit(1 if fails else 0)
