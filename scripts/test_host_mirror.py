#!/usr/bin/env python3
"""Gate: the host's MIRROR.

Three invariants, no DB, no network:
  1. Nothing per-turn is appended to the CACHED static system message after
     the cache split — every late block goes to the dynamic half and is logged.
  2. Every block that reaches the model is visible to prompt_log / the ledger:
     inbox, orchestrator catalog, host rules, and the five late blocks.
  3. context action='status' returns the host's own window, and window_report
     reads the ledger + prompt_log correctly.

  PYTHONPATH=brain:. python3 scripts/test_host_mirror.py
"""
import asyncio
import datetime
import json
import os
import re
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


src = turn_source(REPO)

# ── 1. static message: only the (turn-stable) orchestrator catalog is appended ─
appends = [m.start() for m in re.finditer(r'grok_messages\[0\]\["content"\] \+=', src)]
check("exactly one append to the static system message remains (the orchestrator catalog)",
      len(appends) == 1, f"{len(appends)} appends")
if appends:
    tail = src[appends[0]:appends[0] + 120]
    check("…and it is the orchestrator catalog", "_orch_block" in tail, tail)
# ('attached_image' went with image understanding: the picture
# itself reaches the Host's own model, so no block points it elsewhere.)
# (felt_state, internal_state and conversation_awareness went with
# the simulated psyche; own_skills is the late block left.)
check("late block 'own_skills' is collected, not appended",
      '_late_blocks.append(("own_skills"' in src)
check("the simulated psyche's late blocks are gone",
      not any(f'_late_blocks.append(("{n}"' in src
              for n in ("felt_state", "internal_state", "conversation_awareness")))
check("no 'attached_image' block sends the Host to a second model",
      '_late_blocks.append(("attached_image"' not in src)
flush = src.split("if _late_blocks:", 1)[1][:1400] if "if _late_blocks:" in src else ""
check("late blocks flush into the dynamic message (by index), never grok_messages[0]",
      "grok_messages[_dyn_idx]" in flush and 'grok_messages[0]' not in flush)
check("a turn with no dynamic message inserts one before the user message",
      'grok_messages.insert(_ins, {"role": "system"' in flush)
check("every late block is logged to prompt_log",
      "_lib_late(self, sess, _ln, _lt)" in flush)

# ── 2. the blocks the mirror was blind to are now logged ─────────────────────
check("inbox block logged", '"inbox", _inbox_block' in src)
check("orchestrator catalog logged at per_rebuild scope",
      '"orchestrator_catalog", _orch_block, scope="per_rebuild"' in src)
check("host rules logged at per_rebuild scope",
      '"host_rules", _host_rules, scope="per_rebuild"' in src)
check("ledger carries static_appended_chars", '"static_appended_chars": _static_appended' in src)
check("ledger carries the late blocks", '_loc.get("_late_blocks")' in src)

# ── 3. the host can read its own window ──────────────────────────────────────
reg = (REPO / "brain/mastercode/public_tool_registry.py").read_text()
check("context tool description says status returns the window",
      "your own window" in reg and "last turn's cost" in reg)
ops = (REPO / "brain/mastercode/runtime/context_ops.py").read_text()
check("context status returns window", '"window": window' in ops and "window_report" in ops)

from mastercode import context_budget as cb  # noqa: E402


class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


class _Pool:
    async def fetchrow(self, q, *a):
        return _Row(created_at=datetime.datetime(2026, 9, 6, 16, 9, tzinfo=datetime.timezone.utc),
                    ledger=json.dumps({
                        "calls": 2,
                        "per_call": [{"in": 107377, "out": 563, "cached": 0},
                                     {"in": 107861, "out": 105, "cached": 102865}],
                        "system_chars": 77849, "static_appended_chars": 11800,
                        "tools": {"count": 87, "chars": 186560},
                        "history": {"turns": 3, "chars": 6682, "summary": True},
                        "per_turn_blocks": {"memory_tasks": 11040, "own_skills": 300},
                        "tool_results": [{"tool": "code_tree", "chars": 76502},
                                         {"tool": "gitea", "chars": 416}]}))

    async def fetch(self, q, *a):
        if "scope='per_rebuild'" in q:
            return [{"blocks": json.dumps([{"name": "orchestrator_catalog", "chars": 9000, "freshness": "per_rebuild"}])},
                    {"blocks": json.dumps([{"name": "host_rules", "chars": 2800, "freshness": "per_rebuild"}])}]
        # Newest FIRST, the way `ORDER BY log_id DESC` returns them. The
        # per-turn build is NEWER than the static one (the static row is
        # written by build_prompt at session start), and that is the ordering
        # latest_prompt_blocks must walk PAST to reach the static half: it
        # used to `break` the moment it found a per-turn build, so the mirror
        # silently lost the entire cached system build.
        return [
            {"scope": "per_turn", "blocks": json.dumps([
                {"name": "memory", "chars": 11040, "freshness": "per_turn"},
                {"name": "memory_prefire", "chars": 60, "freshness": "per_turn",
                 "items": ["a fact from the last turn"]}])},
            {"scope": "per_rebuild+static", "blocks": json.dumps([
                {"name": "tool_inventory", "chars": 23876, "freshness": "per_rebuild"},
                {"name": "identity", "chars": 3667, "freshness": "per_rebuild"},
                {"name": "memory_prefire", "chars": 0, "items": []}])},
        ]


class _EmptyPool:
    async def fetchrow(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []


w = asyncio.run(cb.window_report(_Pool(), 9003))
check("window: tokens in summed across calls", w["last_turn"]["tokens_in_total"] == 215238, repr(w["last_turn"]))
check("window: cached summed", w["last_turn"]["tokens_cached_total"] == 102865)
check("window: largest tool result first", w["last_turn"]["largest_tool_results"][0]["tool"] == "code_tree")
check("window: late blocks visible", w["last_turn"]["per_turn_blocks"].get("own_skills") == 300)
check("window: prompt blocks largest first, prefire entry excluded, catalog + rules included",
      [b["name"] for b in w["prompt_blocks"]] == ["tool_inventory", "memory", "orchestrator_catalog", "identity", "host_rules"],
      repr([b["name"] for b in w["prompt_blocks"]]))
check("window: prompt total counts every block the model saw", w["prompt_total_chars"] == 50383)
# The mirror reports the STATIC build alongside the per-turn one. It did not
# once: latest_prompt_blocks stopped walking at the newest
# per-turn row, so `context action='status'` (runtime/context_ops.py) answered
# "my prompt is 11040 chars" for a ~50k prompt. Asserted by name, not just by
# the total, so a future change that drops the static half fails HERE.
check("window: the cached static build is counted, not just the per-turn half",
      {"tool_inventory", "identity"} <= {b["name"] for b in w["prompt_blocks"]}
      and w["prompt_total_chars"] > 39_000,
      repr(w["prompt_total_chars"]))
ps = (REPO / "brain/mastercode/runtime/prompt_schema.py").read_text()
check("log_injected_block stamps freshness from scope",
      'freshness = "per_rebuild" if scope == "per_rebuild" else "per_turn"' in ps)
cbs = (REPO / "brain/mastercode/context_budget.py").read_text()
check("window_report reads prompt_log ENTITY-wide — the per-thread filter "
      "latest_prompt_blocks grew for the panel (2026-09-15) must not scope "
      "the host's mirror of its own last turn",
      "latest_prompt_blocks(pool, entity_id)\n" in cbs, )
e = asyncio.run(cb.window_report(_EmptyPool(), 9003))
check("window: a host with no ledger yet reads null, not an error",
      e["last_turn"] is None and e["prompt_blocks"] == [])

print("\nPASS" if not fails else f"\nFAIL: {fails} check(s)")
sys.exit(1 if fails else 0)
