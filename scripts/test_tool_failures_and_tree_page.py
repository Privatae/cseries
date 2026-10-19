#!/usr/bin/env python3
"""test_tool_failures_and_tree_page.py — the items that live in the tool loop:

  1. A tool that fails the SAME way twice in one turn is DEAD for the turn:
     the recovery message names it, carries its error verbatim, and says
     "do not retry" — never "try again" (runtime/tool_failures.note_round,
     fed ONE round at a time by session.py).
  2. The mid-turn working-memory synthesizer is GONE — with it,
     the paraphrasing that item 2 originally worked around. Raw tool results
     now stay in the message list for the life of the turn.
  3. code_tree is PAGED — one subtree, one depth, capped, with counts
     (runtime/code_tree_page.page_tree; the page args stay kernel-side, path
     and depth also reach the operator).
  4. The loaded-tools line names each loaded group's skill, and the
     Workbench manifest points at build-workflow instead of restating it.

Container-free: pure functions + static checks. APP_ROOT=<repo> python3 scripts/<this>.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. the failure ledger ────────────────────────────────────────────────
from mastercode.runtime.tool_failures import note_round, DEAD_AFTER  # noqa: E402

ERR = 'grep 500: {"detail":"ripgrep (rg) not on operator PATH"}'
fail = {"tool": "code_grep", "result_status": "error", "error_detail": ERR}
ok = {"tool": "context", "result_status": "ok", "error_detail": ""}

ledger: dict = {}
r1 = note_round(ledger, [fail, ok])
check("first failure: a hint, not dead", bool(r1["hints"]) and not r1["dead"])
check("first failure: header asks for the tool's own words (never 'do NOT give up')",
      r1["message"].startswith("One or more tool calls failed") and "give up" not in r1["message"])
check("first failure: the error text rides verbatim", ERR in r1["message"])
r2 = note_round(ledger, [fail])
check(f"second identical failure: dead after {DEAD_AFTER}", r2["dead_tools"] == ["code_grep"])
check("dead message: names the tool as dead this turn", "TOOL 'code_grep' IS DEAD THIS TURN" in r2["message"])
check("dead message: error verbatim", ERR in r2["message"])
check("dead message: says do not call it again", "Do not call it again this turn" in r2["message"])
check("dead message: no 'try again' header",
      "One or more tool calls failed" not in r2["message"] and "retry only" not in r2["message"].lower())
r3 = note_round(ledger, [{"tool": "code_grep", "result_status": "error", "error_detail": "timeout"}])
check("a DIFFERENT error on the same tool is a fresh failure, not dead", bool(r3["hints"]) and not r3["dead"])
r4 = note_round(ledger, [fail, {"tool": "memory", "result_status": "error", "error_detail": "boom"}])
check("mixed round: dead line AND a hint for the live tool", bool(r4["dead"]) and bool(r4["hints"]))
check("mixed round: header is the failure header, dead lines appended",
      r4["message"].startswith("One or more tool calls failed") and "IS DEAD THIS TURN" in r4["message"])
check("memory keeps its specific hint", any(h.startswith("MEMORY RECOVERY") for h in r4["hints"]))
check("no failures → no message", note_round({}, [ok])["message"] == "")
check("blocked/denied count as failures",
      bool(note_round({}, [{"tool": "x", "result_status": "denied", "error_detail": "no"}])["hints"]))

# The whole-turn bug: rounds fed as one slice each count once per round.
led2: dict = {}
note_round(led2, [fail])
note_round(led2, [ok])
check("a clean round does not re-count earlier failures", led2[("code_grep", ERR)] == 1)

# verbatim_errors() is gone with the synthesis it fed. note_round
# is what carries a tool's own words to the host now, so that is what gets
# ratcheted: long errors clipped, never paraphrased.
import mastercode.runtime.tool_failures as _tf  # noqa: E402
check("verbatim_errors is gone (its only consumer was the synthesizer)",
      not hasattr(_tf, "verbatim_errors"))
long = "x" * 1000
_long_msg = note_round({}, [{"tool": "t", "result_status": "error", "error_detail": long}])["message"]
check("note_round clips a very long error rather than dropping it",
      "x" * 100 in _long_msg and len(_long_msg) < 800)

# ── 2. the synthesizer is GONE ───────────────────────────────────────────
# Item 2 above was the narrow fix that kept FAILED tool results out of the
# mid-turn synthesizer's input, because it paraphrased them. The whole lane was
# removed instead: the host now reads every tool result verbatim for the
# life of the turn, so there is no paraphrase to keep anything out of. These
# checks ratchet the removal.
from mastercode.runtime import context as _ctx  # noqa: E402

src_ctx = (ROOT / "brain/mastercode/runtime/context.py").read_text()
for _gone in ("_brain_synthesize_context", "_compress_to_working_memory",
              "_check_context_pressure", "_estimate_token_count",
              "_is_error_envelope", "_errors_block"):
    check(f"context.py: {_gone} is gone", not hasattr(_ctx, _gone))
check("context.py: no synthesis prompt left behind",
      "Synthesize into this JSON format" not in src_ctx
      and "keep_searching" not in src_ctx
      and "## Working Memory" not in src_ctx)
check("context.py: records WHY the lane went", "REMOVED 2026-09-15" in src_ctx)
src_sess_syn = (ROOT / "brain/mastercode/runtime/session.py").read_text()
check("session: nothing calls the synthesizer or the compressor",
      "_brain_synthesize_context(" not in src_sess_syn
      and "_compress_to_working_memory(" not in src_sess_syn)
check("session: the hardcoded-180000 pressure sensor is not consulted",
      "_check_context_pressure" not in src_sess_syn)

# ── 3. code_tree paging ──────────────────────────────────────────────────
from mastercode.runtime.code_tree_page import page_tree, DEFAULT_MAX_ENTRIES  # noqa: E402


def d(name, path, children):
    return {"name": name, "path": path, "type": "directory", "children": children,
            "size": sum(c["size"] for c in children),
            "file_count": sum(c.get("file_count", 1) for c in children)}


def f(name, path, size=10):
    return {"name": name, "path": path, "type": "file", "size": size, "language": "python", "modified": 0}


deep = d("runtime", "brain/mastercode/runtime", [f("session.py", "brain/mastercode/runtime/session.py", 700000)])
mc = d("mastercode", "brain/mastercode", [deep, f("memory_engine.py", "brain/mastercode/memory_engine.py")])
brain = d("brain", "brain", [mc])
scripts = d("scripts", "scripts", [f(f"t{i}.py", f"scripts/t{i}.py") for i in range(400)])
tree = {"repo": "alex/project", "tree": d("cseries", "/", [brain, scripts, f("README.md", "README.md")]),
        "total_files": 403, "truncated": False}

p = page_tree(tree)
check("default page: depth 2 stops above level 3",
      any(e["path"] == "brain/mastercode" for e in p["entries"])
      and not any(e["path"].endswith("session.py") for e in p["entries"]))
check("default page: capped at DEFAULT_MAX_ENTRIES and says so",
      p["shown"] == DEFAULT_MAX_ENTRIES and p["truncated"] and "narrow with path=" in p["note"])
check("default page: entries_total counts what was walked", p["entries_total"] > p["shown"])
check("default page is far smaller than the whole tree", len(json.dumps(p)) < len(json.dumps(tree)) / 2)
sub = page_tree(tree, path="brain/mastercode", depth=1)
check("path page: lists that subtree only",
      {e["path"] for e in sub["entries"]} == {"brain/mastercode/runtime", "brain/mastercode/memory_engine.py"})
check("path page: directory entries carry file counts", any("files" in e for e in sub["entries"]))
check("path page: not truncated", not sub["truncated"] and sub["shown"] == 2)
check("unknown path → error, not an empty page", "error" in page_tree(tree, path="nope"))
check("bad depth/max fall back to defaults", page_tree(tree, depth="x", max_entries=None)["depth"] == 2)
src_tools = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
# `path` and `depth` also go to the operator, so
# its walk can start at the directory asked for instead of stopping at its
# 6,000-file cap first; max_entries and the page itself stay on this side, so
# an operator that ignores path still yields the same page.
check("tools.py: code_tree sends repo, path and depth to the bridge; the page is still cut kernel-side",
      '(("repo", args.get("repo")), ("path", args.get("path")),' in src_tools
      and '("depth", args.get("depth")))' in src_tools
      and "page_tree(data" in src_tools)
from mastercode.runtime.triage import _BRIDGE_TOOL_SCHEMAS  # noqa: E402
props = _BRIDGE_TOOL_SCHEMAS["code_tree"]["function"]["parameters"]["properties"]
check("code_tree schema exposes path / depth / max_entries", {"path", "depth", "max_entries"} <= set(props))

# ── 4. the skill pointer ─────────────────────────────────────────────────
# The loaded-tools line that named a group's skill is gone; find_tools names a
# skill and how to read it. The path that reaches a Host's own skill first is
# `skills/<name>` (context_ops).
from mastercode.runtime import context_ops as _cops, host_skills as _hs  # noqa: E402
_ptr = "skills/build-workflow"
check("`skills/<name>` reaches the Host's own skill first",
      _hs.name_from_path(_ptr) == "build-workflow", _ptr)
_real_dir = _cops._skills_dir
_cops._skills_dir = lambda: ROOT / "default-skills"     # the platform's skills, as seeded
try:
    _disk = _cops._skill_file(_ptr)
finally:
    _cops._skills_dir = _real_dir
check("...then the platform's file of that name", _disk is not None and _disk.name == "build-workflow.md",
      f"{_ptr} -> {_disk}")
src_sess = (ROOT / "brain/mastercode/runtime/session.py").read_text()
check("session: hints come from tool_failures over THIS round (round-start index, never a tail slice)",
      "_note_round(_fail_ledger, tool_actions[_round_start:] + _round_extra)" in src_sess
      and "_round_start = len(tool_actions)" in src_sess
      and "tool_actions[-len(tool_calls):]" not in src_sess)
check("session: a cached (dedup-served) error feeds the ledger as the same failure",
      '_round_extra.append({' in src_sess and '"error_detail": tool_result.get("result", "")' in src_sess)
check("session: blocked/denied carry their reason into error_detail",
      'tool_result.get("status") in ("error", "blocked", "denied") else ""' in src_sess)
check("session: the old whole-turn hint builder is gone", "_build_error_recovery_hints(tool_actions)" not in src_sess)
check("session: ledger records dead tools", '"dead": list(_loc.get("_dead_tools")' in src_sess)
check("tools meta carries the skill", '"skill": m.get("skill") or None' in src_tools)

import yaml  # noqa: E402
wb = yaml.safe_load((ROOT / "system-apps/workbench/app.yaml").read_text())["prompt_behavior"]
from server.applications.manifest import WHAT_IT_IS_MAX  # noqa: E402
wb_chars = len(wb["what_it_is"])
check("workbench prompt_behavior is one paragraph, not a second skill", wb_chars <= WHAT_IT_IS_MAX, f"{wb_chars} chars")
check("workbench prompt_behavior carries no coaching list", "do_when_active" not in wb)
for phrase in ("ALIGNMENT IS A HARD GATE", "NO INVISIBLE BUILDS", "DESKTOPS AND DESIGNS",
               "DELIVERY IS PROVEN", "Economical mode"):
    check(f"workbench no longer restates: {phrase}", phrase not in wb["what_it_is"])
skill = (ROOT / "default-skills/build-workflow.md").read_text()
for phrase in ("No invisible builds", "Desktops and designs are not app builds", "installed, being proven",
               "Economical mode means climb", "NEVER put a build tool in an agent-dispatch scope"):
    check(f"build-workflow carries: {phrase}", phrase in skill)
check("build-workflow no longer defers delivery wording to the briefing", "Your briefing's" not in skill)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
