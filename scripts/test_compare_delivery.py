"""A finished compare is reported by _compare_delivery (session.py): fixed text
from per_harness, no phrasing model. The case that motivated it is entity 9003,
project 9239: four codex variants landed and the
phrased report told the user to "Run review.compare on this session" and to
land the pick "with review.adopt". The record below is that run's outcome.

AST-extracted so it runs outside the image.
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


src = open(os.path.join(ROOT, "brain/mastercode/runtime/session.py")).read()
tree = ast.parse(src)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_compare_delivery")
ns: dict = {"json": json}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "session.py", "exec"), ns)
deliver = ns["_compare_delivery"]

# Tool syntax a person cannot act on: dotted tool names, op=/action= args.
TOOLISH = re.compile(r"\b(review|build|bug_review)\.[a-z_]+|\b(op|action)=")

task_406 = {"project_id": 9239, "origin": json.dumps({"app_id": "workbench", "project_id": 9239}),
            "per_harness": {
    "codex": {"state": "done", "model": "gpt-6-astra", "elapsed_seconds": 692.8, "diff_bytes": 42223},
    "codex-s2": {"state": "done", "model": "gpt-5.6-sol", "elapsed_seconds": 431.4, "diff_bytes": 13662},
    "codex-s3": {"state": "done", "model": "gpt-5.6-terra", "elapsed_seconds": 166.0, "diff_bytes": 9239},
    "codex-s4": {"state": "done", "model": "gpt-5.6-luna", "elapsed_seconds": 338.7, "diff_bytes": 14750},
}}
text, toast, level = deliver(task_406)
print("---\n" + text + "\n---")
check("all four landed: says so, level success", text.startswith("All 4 versions finished.") and level == "success")
check("each variant named by model and variant id",
      all(f"**{m}** ({v})" in text for v, m in [("codex", "gpt-6-astra"), ("codex-s2", "gpt-5.6-sol"),
                                               ("codex-s3", "gpt-5.6-terra"), ("codex-s4", "gpt-5.6-luna")]))
check("time and size in human units", "12 min" in text and "41 KB of changes" in text and "3 min" in text)
# 2026-09-30: the platform's text asks nothing. Which version is used is the
# person's choice, and it goes to the question box through the Host, which is
# handed this result (deliver_agent_result) — never "Tell me which one".
ASKS = re.compile(r"(?i)\b(tell me|let me know|say|reply|which one)\b|\?")
check("points at the button on screen and asks for nothing",
      "**open version**" in text and not ASKS.search(text), text)
check("no tool syntax", not TOOLISH.search(text), str(TOOLISH.search(text)))

mixed = {"origin": {"app_id": "workbench", "project_id": 5}, "per_harness": {
    "claude-code": {"state": "done", "model": "claude-opus-5", "elapsed_seconds": 60, "diff_bytes": 300,
                    "checks": {"ok": False}},
    "codex": {"state": "failed", "error": "codex produced no output for 10 minutes and was stopped as stuck."},
}}
text, _, level = deliver(mixed)
check("one of two: counts, keeps the failure's own words, warning",
      text.startswith("1 of 2 versions finished.") and "stopped as stuck" in text and level == "warning")
check("small diffs and failed checks read plainly",
      "under 1 KB of changes" in text and "its checks failed" in text)
check("a single survivor: its button named, nothing asked",
      "It has an **open version** button" in text and not ASKS.search(text), text)

none = {"origin": {"app_id": "workbench"}, "per_harness": {"a": {"state": "failed"}, "b": {"state": "failed", "error": "boom"}}}
text, _, level = deliver(none)
check("none landed: says nothing changed, no pick offered, nothing asked, error level",
      text.startswith("None of the 2 versions finished.") and "open version" not in text
      and "No error was recorded." in text and level == "error" and not ASKS.search(text))

circ = {"origin": {"app_id": "circles"}, "per_harness": task_406["per_harness"]}
text, _, _ = deliver(circ)
check("a compare asked in Circles names no Workbench button and asks nothing",
      "open version" not in text and not ASKS.search(text), text)

# The phrasing prompt no longer carries the compare wording that named tools.
check("session.py no longer tells the phrasing model about review.compare/adopt",
      "side by side (review.compare" not in src and "want landed (review.adopt)" not in src)
check("deliver_agent_result routes a finished compare to _compare_delivery",
      'elif status == "completed" and _is_fanout and _several:' in src
      and "content, toast_summary, toast_level = _compare_delivery(record)" in src)
_cmp = src[src.index("content, toast_summary, toast_level = _compare_delivery(record)"):]
_cmp = _cmp[:_cmp.index("elif status ==")]
check("...and hands the Host the pick for the question box (it lands the answer)",
      "_event = (" in _cmp and "question box" in _cmp and "review.adopt" in _cmp
      and "model judges" in _cmp)

# The other fixed sentences in the delivery path carry no tool syntax either.
fixed = []
for name in ("_verify_delivery", "_compare_delivery"):
    f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    doc = f.body[0].value if isinstance(f.body[0], ast.Expr) else None
    fixed += [c.value for c in ast.walk(f)
              if isinstance(c, ast.Constant) and isinstance(c.value, str) and c is not doc]
bad = [t for t in fixed if TOOLISH.search(t)]
check("no fixed delivery sentence names a tool", not bad, str(bad))
check("the bug-verdict sentence is plain", "bug_review op='validated'" not in src)

print("all gates passed" if not failures else f"{len(failures)} failing")
sys.exit(1 if failures else 0)
