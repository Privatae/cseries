#!/usr/bin/env python3
"""Gate: an all-tools-failed round NAMES the tools that failed.

Origin: a Host logged three rounds of

    Tool round 1: ALL 1 tools failed (consecutive=1)

and which tool failed was unrecoverable after the fact: there is no tool-call
table in the entity DB (only inference_cost_log), and the pod log carried no
denial or blocked line. The only record of the name was this warning — which
printed a count and nothing else. It cost a diagnosis in the session that found
it. tool_actions already held the name, the status and the error text.

session.py is far too heavy to import here (it pulls the whole runtime), so the
helper is lifted out by AST and exercised directly — which also keeps this gate
honest about testing the shipped function rather than a copy.

Run: APP_ROOT=<repo> python3 scripts/test_failed_tool_naming.py
"""
import ast
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
_SESSION = _ROOT / "brain" / "mastercode" / "runtime" / "session.py"

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


src = turn_source(_ROOT)
tree = ast.parse(src)

fns = [n for n in ast.walk(tree)
       if isinstance(n, ast.FunctionDef) and n.name == "_describe_failed_tools"]
check("_describe_failed_tools exists in session.py", len(fns) == 1)
if not fns:
    print("\n1 FAILED")
    sys.exit(1)

ns: dict = {}
exec(compile(ast.Module(body=[fns[0]], type_ignores=[]), "<lifted>", "exec"), ns)
describe = ns["_describe_failed_tools"]

# ── the shipped defect: the name must appear ────────────────────────────────
out = describe([{"tool": "web_search", "result_status": "error",
                 "error_detail": "connection refused"}])
check("the failing tool is named", "web_search" in out, out)
check("its status is included", "error" in out, out)
check("its reason is included", "connection refused" in out, out)

# ── every tool in the round, not just the first ─────────────────────────────
out = describe([
    {"tool": "alpha", "result_status": "denied", "error_detail": ""},
    {"tool": "beta", "result_status": "blocked", "error_detail": "out of grant"},
])
check("every tool in the round is named", "alpha" in out and "beta" in out, out)
check("a tool with no reason still reports its status",
      "alpha(denied)" in out, out)

# ── it is a LOG line: bounded, single-line ──────────────────────────────────
out = describe([{"tool": "t", "result_status": "error", "error_detail": "x" * 5000}])
check("a huge reason is clipped, so one tool cannot flood the log",
      len(out) < 400, f"len={len(out)}")
out = describe([{"tool": "t", "result_status": "error",
                 "error_detail": "line one\nline two\n\tindented"}])
check("a multi-line reason is flattened to one line", "\n" not in out and "\t" not in out, repr(out))

# ── never raises, whatever it is handed ─────────────────────────────────────
for label, arg in (
    ("empty list", []),
    ("non-dict entries", ["junk", None, 42]),
    ("dict with nothing in it", [{}]),
    ("missing error_detail", [{"tool": "t", "result_status": "error"}]),
    ("None fields", [{"tool": None, "result_status": None, "error_detail": None}]),
):
    try:
        got = describe(arg)
        ok = isinstance(got, str) and got != ""
    except Exception as e:      # noqa: BLE001
        ok, got = False, f"raised {e!r}"
    check(f"tolerates {label}", ok, str(got))

# ── both call sites actually pass the round's actions ───────────────────────
calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and getattr(n.func, "id", None) == "_describe_failed_tools"]
check("the helper is called at BOTH the all-failed and the breaker log",
      len(calls) == 2, f"{len(calls)} call site(s)")

# red/green: the bare count-only format must be gone from those warnings.
check("red/green — the count-only warning no longer exists",
      "ALL %d tools failed (consecutive=%d)\"" not in src)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
