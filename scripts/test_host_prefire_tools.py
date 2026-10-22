#!/usr/bin/env python3
"""Gate: what is left of the PREFIRE module after the brief prompt, and that
the rest stays gone. No DB, no network.

  1. The pick, the loaded-tools line, the skill push, the self-turn inventory
     and orient's mid-turn schema load are gone from prefire_tools and from
     session.py: the tool list is fixed for the turn and find_tools reaches
     the rest.
  2. The detector for a turn ABOUT the Host (it widens the tool trail) still
     fires on the self questions and only on them, in linear time.
  3. trail_tool_names reads an operate chip as the tool it operated.

  PYTHONPATH=brain:. python3 scripts/test_host_prefire_tools.py
"""
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


from mastercode.runtime import prefire_tools as PT  # noqa: E402

# ── 1. gone ─────────────────────────────────────────────────────────────────
for gone in ("pick_tool_groups", "render_loaded_tools", "render_skill_context",
             "render_self_inventory", "desktop_write_seen", "GROUP_KEYWORDS",
             "SURFACE_DEFAULT_GROUPS", "ALWAYS_TOOLS", "SKILL_INJECT_TOTAL"):
    check(f"prefire_tools.{gone} is gone", not hasattr(PT, gone))
_sess = turn_source(REPO)
for gone in ("pick_tool_groups", "render_skill_context", "render_self_inventory",
             "_tool_loadable", "prefetch_block", "func_name == \"orient\"",
             "_build_host_orchestration_tools(search=True)", "build_host_rules(self.pool, sess.entity[\"id\"], search="):
    check(f"session.py no longer carries {gone!r}", gone not in _sess)
check("session.py builds the one list: the verbs + find_tools + read",
      "tools = _build_host_orchestration_tools() + _build_search_door_tools()" in _sess)
check("the kept primitives are still here (turn types and live context score with them)",
      all(hasattr(PT, n) for n in ("WEAK_WORDS", "_STOP", "_tokens", "STRONG_HIT", "WEAK_HIT",
                                   "LOAD_THRESHOLD", "SKILL_INJECT_CHARS", "surface_key",
                                   "trail_tool_names", "is_self_turn")))

# ── 2. turns ABOUT the host ────────────────────────────────────────────────
# Entity 9003, a small open model on Circles, asked to review its own
# environment: it held environment + comms and answered "no Gitea tool, no
# HTTP tool". Both exist, one orient away. On such a turn the whole inventory
# rides as text and the trail widens; no extra schemas load.
for msg in ("what tools do you have?",
            "analyze what you have in front of you",
            "review your environment and tell me the gaps",
            "do you have a git tool for gitea?",
            "what can you do",
            "describe your capabilities",
            "why couldn't you read that page",
            "What's your setup — which model, what memory, what access?",
            # The motivating shapes with no kit noun.
            "list everything you can do",
            "can you tell me everything you are able to do here?",
            "do you have any way to hit an HTTP endpoint?",
            "summarize what you did in this thread so far",
            "are you allowed to post to my circle?",
            "what are you?", "who are you", "what do you have available?",
            # A trailing clause after the opener, and
            # "what went wrong" with the host before or after it.
            "what are you? also tell me about the weather",
            "what do you have? i'm curious",
            "what went wrong on your end?",
            "you tried to fetch it — what went wrong?",
            "show me all that you did in this thread"):
    check(f"self turn: {msg!r}", PT.is_self_turn(msg), msg)
import time as _time  # noqa: E402
# Whitespace runs (third review) AND repeated markers on one line (fourth
# review: "your "*40000 took 19.6 s through a two-sided `.*` alternation).
for probe in ("what are you" + " " * 80_000, "what do you have" + "\t \n" * 30_000,
              "how you work" + " " * 80_000, "x" * 200_000 + " what went wrong",
              "your " + "y" * 200_000, "your " * 40_000, "you " * 40_000 + "what went wrong",
              "what went wrong " + "your " * 40_000, ("you your " * 20_000) + " what are you"):
    _t0 = _time.perf_counter()
    PT.is_self_turn(probe)
    _dt = _time.perf_counter() - _t0
    check(f"detector is linear on a long message ({len(probe):,} chars: {_dt*1000:.0f} ms)", _dt < 0.5, f"{_dt:.2f}s")
check("the detector scans a bounded prefix, so a paste cannot buy CPU",
      PT.SELF_SCAN_CHARS <= 50_000 and not PT.is_self_turn("x " * 20_000 + "what tools do you have?"))
# An exclusion phrase straddling the scan boundary must still be struck
# whole (9 cut offsets once turned "your environment
# variables" into "your environment").
check("an exclusion phrase across the scan boundary does not fire",
      not any(PT.is_self_turn(("q " * ((PT.SELF_SCAN_CHARS - k) // 2)) + "set your environment variables here")
              for k in range(0, 40)))
# Exclusion phrases BEFORE the boundary once shortened the
# text and slid the margin's truncated phrase inside the bound. The strike
# must keep offsets, so the number of earlier strikes cannot matter.
_lead = "we set your environment variables now. "
check("earlier exclusion strikes do not move the boundary",
      not any(PT.is_self_turn(_lead * n + " " * (PT.SELF_SCAN_CHARS + s - len(_lead * n))
                              + "your environment variables here trailing words")
              for n in range(0, 8) for s in range(0, 64, 3)))
for msg in ("can you restart the app", "hey how are you", "set up my python environment",
            "what's the plan for dinner", "run the build for phase 2", "",
            "review the PR for me", "tell me about the tools in my workshop",
            # A reviewing verb with no second-person marker is
            # Workbench work, not a self turn; generic nouns need the marker.
            "review the architecture of this repo and describe the modules",
            "analyze the gaps in the test coverage",
            "describe the limitations of this approach",
            "audit the network environment on the edge server",
            "evaluate the model's capabilities for our use case",
            "inventory the parts in the garage",
            "is that what you have in mind?",
            "set up your environment variables in the container",
            # The bare openers are ordinary chat.
            "what are you doing tonight?",
            "what do you have going on tomorrow?",
            "what are you seeing in the logs for my app?",
            "who are you meeting with on friday?",
            "tell me how you work through a recipe substitution",
            "what can you do to help me with dinner?",
            "what went wrong with the deploy of my site?"):
    check(f"not a self turn: {msg!r}", not PT.is_self_turn(msg), msg)


# ── 3. the trail names the operated tool ────────────────────────────────────
_trail = [{"tool_calls": [{"tool": "operate", "operate_tool": "revoke_nac_rule", "status": "ok"},
                          {"tool": "get_network", "status": "ok"}]}]
_names = PT.trail_tool_names(_trail, max_turns=3)
check("an operate chip reads as the tool it operated", "revoke_nac_rule" in _names
      and "operate" not in _names, repr(_names))

print(f"\nprefire: {'PASS' if not fails else f'FAIL ({fails})'}")
sys.exit(1 if fails else 0)
