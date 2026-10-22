#!/usr/bin/env python3
"""Gate: the images a turn generates ride its tool-call record
(brain/mastercode/runtime/session.py, the tool loop's `ta_entry`).

The loop keeps a call's `result` on the record for one tool. The check named
`image_generator`; the tool is `image_generate` (public_tool_registry), so no
record ever kept one. Nothing
reads it from the record today: a messaging connector's reply parts find a
turn's generated images in the turn's `a:` frames (server/connectors/turns.py
read_frames, then reply_parts.generated_images_of_turn), which carry every
tool result. This gate keeps the condition naming a real tool.

The gate reads the condition out of session.py itself (the `if` whose body
sets ta_entry["result"], and the statement that prepares its arguments) and
runs exactly those lines:

  - every tool the condition names is a registered Host tool;
  - an image_generate generate call keeps its result on the record;
  - its gallery and look calls, and every other tool, do not (their
    records are unchanged by the fix);
  - arguments that did not parse as an object do not break the loop.

Pure: no DB, no model. Run: python3 scripts/test_image_result_on_record.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]
SRC = turn_source(ROOT)
FAILS: list[str] = []


def check(label, ok, detail=None):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok or detail is None else f"   [{detail!r}]"[:600]))
    if not ok:
        FAILS.append(label)


def _sets_result(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and t.value.id == "ta_entry" and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "result"):
                    return True
    return False


tree = ast.parse(SRC)
found = []
for parent in ast.walk(tree):
    body = getattr(parent, "body", None)
    if not isinstance(body, list):
        continue
    for i, st in enumerate(body):
        if isinstance(st, ast.If) and _sets_result(ast.Module(body=st.body, type_ignores=[])):
            prev = body[i - 1] if i > 0 else None
            found.append((st, prev))

check("session.py has exactly one place that keeps a call's result on its record", len(found) == 1,
      len(found))
if not found:
    print("RESULT: FAIL")
    sys.exit(1)
cond, prev = found[0]
named = {n.comparators[0].value for n in ast.walk(cond.test)
         if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "func_name"
         and n.comparators and isinstance(n.comparators[0], ast.Constant)}
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS  # noqa: E402
check("every tool it names is a registered Host tool", named and named <= set(PUBLIC_TOOL_SCHEMAS),
      sorted(named))
check("it names image_generate", "image_generate" in named, sorted(named))

stmts = [prev, cond] if (prev is not None and any(
    isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) for n in ast.walk(prev))
    and not isinstance(prev, (ast.If, ast.Expr, ast.Await))) else [cond]
code = compile(ast.fix_missing_locations(ast.Module(body=stmts, type_ignores=[])), "session.py", "exec")

GEN = {"status": "ok", "result": {"images": [{"id": "gimg_" + "a" * 22, "url": "/api/images/gimg_" + "a" * 22,
                                              "revised_prompt": ""}], "count": 1, "prompt": "a cat"}}


def run(func_name, func_args, tool_result) -> dict:
    ns = {"func_name": func_name, "func_args": func_args, "tool_result": tool_result,
          "ta_entry": {"tool": func_name}}
    exec(code, ns)
    return ns["ta_entry"]


check("an image_generate generate call keeps its result",
      run("image_generate", {"action": "generate", "prompt": "a cat"}, GEN).get("result") == GEN)
check("...with no action named, generate is the default and is kept",
      run("image_generate", {"prompt": "a cat"}, GEN).get("result") == GEN)
check("gallery keeps nothing", "result" not in run("image_generate", {"action": "gallery"},
                                                   {"status": "ok", "result": {"images": []}}))
check("look keeps nothing", "result" not in run("image_generate", {"action": "look"},
                                                {"status": "ok", "result": {"count": 1, "shown": ["x"]}}))
check("another tool keeps nothing", "result" not in run("recall", {"query": "x"}, GEN))
for bad in (["not", "an", "object"], "a string", None, 7):
    try:
        got = run("image_generate", bad, GEN)
        check(f"arguments {bad!r} (not an object) do not raise, and read as generate",
              got.get("result") == GEN, got)
    except Exception as e:  # noqa: BLE001
        check(f"arguments {bad!r} (not an object) do not raise", False, repr(e))

print()
print("RESULT: " + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAIL: {FAILS}"))
sys.exit(1 if FAILS else 0)
