#!/usr/bin/env python3
"""Gate: every model call the Host makes outside its own turn names itself, so
the activity feed shows it with its cost.

The activity feed (GET /api/inference/activity) leaves out the chat turn's own
source module, entity_runtime, which is also what a call that names no source
is logged under: the rolling summary and the thread pick once spent under
that label, hidden from the owner. Here:

  L. every call_model / _call_entity_model call outside runtime/session.py
     (the turn itself) passes `_source` with a name of its own: a literal
     keyword, or a kwargs dict the same function fills with `_source`;
  C. the feed returns each call's cost (cost_usd, and cost_basis saying what
     kind of figure it is) and can include the turn's own calls;
  T. Training Memory's classifier batch, which calls Anthropic directly, is
     logged to the same ledger under its own source.

Container-free: reads the source. APP_ROOT=<repo> python3 scripts/<this>.py
"""
import ast
import inspect
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]
FAILED = 0
CHAT = "entity_runtime"


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:900]}"))
    if not cond:
        FAILED += 1


def _enclosing(tree):
    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[c] = n
    return parent


unlabelled = []
for base in ("brain", "server"):
    for f in sorted((ROOT / base).rglob("*.py")):
        rel = str(f.relative_to(ROOT))
        if rel == "brain/mastercode/runtime/session.py":
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        if "call_model" not in src and "_call_entity_model" not in src:
            continue
        tree = ast.parse(src)
        parent = _enclosing(tree)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
            if name not in ("call_model", "_call_entity_model", "_call_model"):
                continue
            if name == "_call_model" and rel.endswith("provider_gateway.py"):
                continue    # the gateway's own inner call forwards **kwargs
            kws = {k.arg: k.value for k in n.keywords if k.arg}
            ok = False
            if "_source" in kws:
                v = kws["_source"]
                ok = not (isinstance(v, ast.Constant) and v.value == CHAT)
            elif any(k.arg is None for k in n.keywords):
                fnode = n
                while fnode in parent and not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fnode = parent[fnode]
                ok = "_source" in ast.unparse(fnode)
            if not ok:
                unlabelled.append(f"{rel}:{n.lineno}")
check("L every model call outside the turn names its source", not unlabelled, unlabelled)

from server.routes import inference as inf  # noqa: E402
p = inspect.signature(inf.get_background_activity).parameters
src_inf = inspect.getsource(inf.get_background_activity)
check("C the feed returns each call's cost and what kind of figure it is",
      '"cost_usd"' in src_inf and '"cost_basis"' in src_inf, "")
check("C the feed can include the turn's own calls", p.get("include_turns") is not None
      and p["include_turns"].default is False, p.get("include_turns"))
check("C no label names the retired summary writer",
      "thread_consolidator" not in inf._ACTIVITY_LABELS, list(inf._ACTIVITY_LABELS))
wk = (ROOT / "brain/training_memory/worker.py").read_text()
check("T the classifier batch is logged under its own source",
      '_log_inference("training_memory"' in wk and "await _log_batch_spend(results_text" in wk)

print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
