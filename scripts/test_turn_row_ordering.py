#!/usr/bin/env python3
"""Gate for CHAT TURN ORDER (project 9213, 2026-08-28).

The final response landed above the message it answered, so it looked as if
nothing had happened.

The mechanism, measured on one Host that morning:

  1. session.py writes each turn fire-and-forget — asyncio.create_task(
     write_turn_row(...)) — for the user message and, ~30s later, the
     assistant reply.
  2. memory_engine._write_turn_row_landed awaits _get_memory_embedding()
     BEFORE its INSERT.
  3. that INSERT did not name created_at, so the column took its now()
     DEFAULT — stamped when the insert landed, not when the turn was said.
  4. the chat surfaces sort strictly on created_at (workbench-web
     chat-pane.tsx loadHistory; circles-web's twin).

So the stored order was "whichever embed returned first". A short assistant
reply embeds faster than a long user message, so the answer was routinely
stamped BEFORE the question and rendered above it. In PII mode the window is
wider still: the drain worker lands the row an arbitrary time later.

The fix is not to make the writes synchronous (that puts an embed on the chat
hot path). It is to stamp created_at at the CALL SITE, synchronously, so
program order becomes stored order.

Run: APP_ROOT=<repo> python3 scripts/test_turn_row_ordering.py
"""
import ast
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
_ENGINE = _ROOT / "brain" / "mastercode" / "memory_engine.py"
_SESSION = _ROOT / "brain" / "mastercode" / "runtime" / "session.py"

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


eng_src = _ENGINE.read_text()
eng_tree = ast.parse(eng_src)
ENG_FUNCS = {n.name: n for n in ast.walk(eng_tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _kwonly(fn) -> set[str]:
    return {a.arg for a in fn.args.kwonlyargs} | {a.arg for a in fn.args.args}


# ── 1. the parameter exists on both halves ──────────────────────────────────
for name in ("write_turn_row", "_write_turn_row_landed"):
    check(f"{name} is still here", name in ENG_FUNCS)

if "write_turn_row" in ENG_FUNCS and "_write_turn_row_landed" in ENG_FUNCS:
    check("write_turn_row takes created_at",
          "created_at" in _kwonly(ENG_FUNCS["write_turn_row"]))
    check("_write_turn_row_landed takes created_at",
          "created_at" in _kwonly(ENG_FUNCS["_write_turn_row_landed"]))


# ── 2. THE REGRESSION: every turn INSERT names created_at ───────────────────
# Both branches (embedded / unembedded). Leaving the column out is exactly the
# bug — the row silently takes now() at insert time.
landed_src = ast.get_source_segment(eng_src, ENG_FUNCS["_write_turn_row_landed"]) or ""
inserts = [s for s in landed_src.split("INSERT INTO entity_memory")[1:]]
check("_write_turn_row_landed still has both INSERT branches "
      "(embedded + unembedded)", len(inserts) == 2, f"found {len(inserts)}")
for i, frag in enumerate(inserts, 1):
    # Only look at the statement, not the whole tail of the function.
    stmt = frag.split("RETURNING")[0]
    check(f"turn INSERT #{i} names created_at as a column",
          "created_at" in stmt)
    check(f"turn INSERT #{i} falls back to now() when the caller passed none",
          "COALESCE" in stmt and "now()" in stmt)


# ── 3. the stamp is taken OUTSIDE the writer closure ────────────────────────
# In PII mode _writer runs on the drain worker, arbitrarily later. If the
# default were computed inside it, mode ON would reintroduce the whole bug.
wtr_src = ast.get_source_segment(eng_src, ENG_FUNCS["write_turn_row"]) or ""
stamp_at = wtr_src.find("datetime.now(timezone.utc)")
writer_at = wtr_src.find("async def _writer")
check("write_turn_row defaults created_at BEFORE defining _writer "
      "(so PII mode carries utterance time, not drain time)",
      stamp_at != -1 and writer_at != -1 and stamp_at < writer_at,
      f"stamp={stamp_at} writer={writer_at}")
check("the closure forwards the stamped value", "created_at=said_at" in wtr_src)


# ── 4. every session.py turn write passes it ────────────────────────────────
sess_src = turn_source(_ROOT)
sess_tree = ast.parse(sess_src)

calls = []
for n in ast.walk(sess_tree):
    if isinstance(n, ast.Call):
        ident = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        if ident == "write_turn_row":
            calls.append(n)

check("session.py still writes turn rows", len(calls) >= 4, f"found {len(calls)}")
missing = [c.lineno for c in calls
           if not any(k.arg == "created_at" for k in c.keywords)]
check("EVERY session.py write_turn_row call stamps created_at",
      not missing, f"unstamped at lines {missing}")

# The user turn must be stamped ahead of the assistant turn in PROGRAM order —
# that is the whole mechanism. Both live in send_message; the user write is the
# first one in the file, the final assistant write the last.
if len(calls) >= 2:
    check("the user turn is written before the assistant turn in program order",
          calls[0].lineno < calls[-1].lineno,
          f"first={calls[0].lineno} last={calls[-1].lineno}")


# ── 5. the reader still sorts on created_at (the other half of the contract) ─
# If a future change sorts by id instead, this gate's premise is stale and
# should be revisited rather than silently passing.
_PANE = _ROOT / "workbench-web" / "src" / "components" / "workbench" / "chat-pane.tsx"
if _PANE.exists():
    pane = _PANE.read_text()
    check("workbench chat still orders history by created_at",
          "created_at" in pane and ".sort(" in pane)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
