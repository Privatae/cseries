#!/usr/bin/env python3
"""Gate: the kernel reads the Host's own schema only through its executor.

A view or function the Host wrote runs with the rights of whoever reads it. If
kernel code read `world` as `entity` (the superuser the brain pool connects
as), the Host's code would run as the superuser. So:

  1. No string literal in server/ or brain/ names an object in `world`
     (`world.<name>`), except in brain/mastercode/world_db.py, the executor.
     Catalog reads (`n.nspname = 'world'`) name no object and run no Host code,
     so they are not matched.
  2. world_db.py takes no pool but the host_world lane: it never calls the
     brain, control or recall pool getters.
  3. The `data` dispatch branch in runtime/tools.py hands world_db no pool.

Container-free: an AST walk of the source.
  python3 scripts/test_host_world_static.py
"""
import ast
import os
import re
import sys
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
EXECUTOR = ROOT / "brain" / "mastercode" / "world_db.py"
OBJECT_IN_WORLD = re.compile(r"(?<![\w.])world\.[A-Za-z_\"]")

failed = 0


def check(label, cond, detail=""):
    global failed
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        failed += 1


def literals(tree):
    """Every string constant, with f-strings flattened to their literal parts."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
            yield node.lineno, text


hits = []
for base in ("server", "brain"):
    for path in sorted((ROOT / base).rglob("*.py")):
        if path == EXECUTOR or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for line, text in literals(tree):
            if OBJECT_IN_WORLD.search(text):
                hits.append(f"{path.relative_to(ROOT)}:{line}")
check("1 no kernel string names an object in `world` outside world_db.py", not hits, str(hits[:10]))

src = EXECUTOR.read_text(encoding="utf-8")
other_lanes = [g for g in ("get_pool(", "get_control_pool(", "get_recall_pool(", "_pool =", "brain_service._pool")
               if g in src]
check("2 world_db.py reaches no pool but host_world's", not other_lanes, str(other_lanes))
check("2 and it does use the host_world lane",
      "get_world_pool()" in src and "world_dsn()" in src)

tools = tools_source(ROOT)
m = re.search(r'elif name == "data":(.*?)\n    elif ', tools, re.S)
code = "\n".join(ln.split("#", 1)[0] for ln in (m.group(1) if m else "").splitlines())
check("3 the data branch exists and calls world_db.run_op with no pool",
      "world_db.run_op(args)" in code and "pool" not in code, code.strip()[:200])

print("RESULT: ALL PASS" if failed == 0 else f"RESULT: {failed} FAILED")
sys.exit(1 if failed else 0)
