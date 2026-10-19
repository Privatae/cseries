"""The turn token's server half: every row of a live turn carries the token
the client saw on that turn's session frame, and both history routes hand it
back. The chat windows swap their live copy of a turn for its saved rows by
it (workbench-web / circles-web lib/history-merge.ts); matching on text left
a second copy on screen whenever the saved reply differed from the streamed
one (project 9239, 2026-09-24).

Pure source + AST checks, so this runs outside the image.
"""
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def src(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


# 1. session.py: the turn tags (written on the user row, the assistant row and
#    the stopped-turn trace) carry the SAME token the session frame sends.
session = ast.parse(src("brain/mastercode/runtime/session.py"))
send = next(n for n in ast.walk(session)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_message_core")
tags = [n for n in ast.walk(send)
        if isinstance(n, ast.Assign) and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "_turn_tags"
        and isinstance(n.value, ast.Dict)]
# (send_message is a thin wrapper that settles the identity-hold takeover;
# the turn itself is _send_message_core.)
check("send_message builds _turn_tags once, as a dict literal", len(tags) == 1, str(len(tags)))
if tags:
    pairs = {k.value: v for k, v in zip(tags[0].value.keys, tags[0].value.values)
             if isinstance(k, ast.Constant)}
    v = pairs.get("turn_token")
    check("_turn_tags['turn_token'] is _turn_token",
          isinstance(v, ast.Name) and v.id == "_turn_token", ast.dump(v) if v else "missing")
frame = [n for n in ast.walk(send)
         if isinstance(n, ast.Dict)
         and any(isinstance(k, ast.Constant) and k.value == "event" for k in n.keys)
         and any(isinstance(v, ast.Constant) and v.value == "session_started" for v in n.values)]
check("the session_started event carries turn_token=_turn_token",
      any(any(isinstance(k, ast.Constant) and k.value == "turn_token"
              and isinstance(v, ast.Name) and v.id == "_turn_token"
              for k, v in zip(d.keys, d.values)) for d in frame))

# 2. PII mode keeps it: scrub_metadata is an allowlist.
pii = ast.parse(src("brain/mastercode/pii_queue.py"))
keys = next(n for n in pii.body if isinstance(n, ast.Assign)
            and getattr(n.targets[0], "id", "") == "_STRUCTURAL_META_KEYS")
kept = {e.value for e in keys.value.args[0].elts}
check("pii_queue keeps turn_token through the metadata scrub", "turn_token" in kept)

# 3. Circles history hands it back (the Workbench route returns metadata whole).
circles = ast.parse(src("server/routes/circles.py"))
fn = next(n for n in circles.body
          if isinstance(n, ast.FunctionDef) and n.name == "_normalize_turn_rows")
ns = {"json": json}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "circles.py", "exec"), ns)
out = ns["_normalize_turn_rows"]([
    {"id": 1, "content": "hi", "created_at": "t1", "metadata": {"role": "user", "turn_token": "ab"}},
    {"id": 2, "content": "yo", "created_at": "t2",
     "metadata": json.dumps({"role": "assistant", "turn_token": "ab"})},
    {"id": 3, "content": "old", "created_at": "t0", "metadata": {"role": "assistant"}},
], "Host")
check("_normalize_turn_rows passes turn_token through, absent when unsaved",
      [m.get("turn_token") for m in out] == ["ab", "ab", None], str(out))
brain = src("server/routes/brain.py")
check("/api/brain/chat returns the row's metadata whole",
      '"metadata": _meta(r["metadata"])' in brain)

print("all gates passed" if not failures else f"{len(failures)} failing")
sys.exit(1 if failures else 0)
