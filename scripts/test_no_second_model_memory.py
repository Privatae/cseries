#!/usr/bin/env python3
"""Gate: no model outside the Host's own turn writes what the Host remembers.

Two writers once ran as a separate model call after the turn: the rolling thread summary, which rewrote itself
from its last version and led the history, and its artifact janitor, which
kept, rewrote or dismissed stored documents. Both are retired
(migrations/v241); what falls out of a window now rides as the Host's own
close_turn notes (brain/mastercode/evicted_record.py).

  W. no module that writes entity_memory, user_context or a thread's topic
     calls a model, except where the call IS the Host's turn or a tool the Host
     started in it (ALLOWED, each with its reason);
  R. the summary writer, its policy and the post-turn dispatch are gone, and
     nothing imports them;
  T. a thread's topic has one writer: close_turn (set_thread_topic with a
     topic is called from close_turn only).

Container-free: reads the source. APP_ROOT=<repo> python3 scripts/<this>.py
"""
import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:900]}"))
    if not cond:
        FAILED += 1


WRITES = re.compile(r"INSERT INTO (entity_memory|user_context)|UPDATE (entity_memory|user_context)\b"
                    r"|UPDATE circles_threads SET topic|set_thread_topic\(")
MODEL_CALL = re.compile(r"\b(call_model|_call_entity_model)\(")
# Where a model call and a memory write share a module for a reason that is
# not "a second model writes memory":
ALLOWED = {
    # The Host's own turn: its model calls ARE the turn.
    "brain/mastercode/runtime/session.py",
    # The Host's tool executors, run in its turn; their model calls are tool
    # work the Host started (web search, a picture), and their memory writes
    # are the Host's own calls (memory_curate, close_turn).
    "brain/mastercode/runtime/tools.py",
    # build(action='plan')'s research synthesis, started by the Host in its
    # turn: its rows are recall-only research records, never pre-fired.
    "brain/mastercode/skills/deep_research.py",
}

offenders = []
for base in ("brain", "server"):
    for f in sorted((ROOT / base).rglob("*.py")):
        rel = str(f.relative_to(ROOT))
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if rel in ALLOWED or not WRITES.search(text):
            continue
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        if MODEL_CALL.search(code):
            offenders.append(rel)
check("W no module that writes memory calls a model, outside the Host's turn and its tools",
      not offenders, offenders)

print("R. the summary writer is gone")
for rel in ("brain/mastercode/thread_consolidator.py", "brain/mastercode/summary_policy.py"):
    check(f"R {rel} does not ship", not (ROOT / rel).exists())
uses = []
for base in ("brain", "server"):
    for f in (ROOT / base).rglob("*.py"):
        t = f.read_text(encoding="utf-8", errors="replace")
        for needle in ("import thread_consolidator", "from mastercode.thread_consolidator",
                       "summary_policy", "dispatch_topic_summary", "maybe_consolidate(",
                       "stash_tool_artifacts("):
            for line in t.splitlines():
                if needle in line and not line.lstrip().startswith("#"):
                    uses.append(f"{f.relative_to(ROOT)}: {line.strip()[:120]}")
check("R nothing imports or calls them", not uses, uses[:10])

print("T. one writer of a thread's topic")
writers = []
for base in ("brain", "server"):
    for f in (ROOT / base).rglob("*.py"):
        rel = str(f.relative_to(ROOT))
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                if name != "set_thread_topic" or len(n.args) < 4:
                    continue
                topic = n.args[3]
                if isinstance(topic, ast.Constant) and topic.value is None:
                    continue        # a title only, never the topic
                writers.append(f"{rel}:{n.lineno}")
check("T set_thread_topic with a topic is called from close_turn only",
      writers and all(w.startswith("brain/mastercode/runtime/close_turn.py") for w in writers), writers)
raw = []
for base in ("brain", "server"):
    for f in (ROOT / base).rglob("*.py"):
        rel = str(f.relative_to(ROOT))
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(r"UPDATE circles_threads SET[^\"']*\btopic\s*=", line):
                raw.append(f"{rel}:{i}")
check("T no other code sets circles_threads.topic directly",
      all(r.startswith("brain/mastercode/circles_threads.py") for r in raw), raw)

print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
