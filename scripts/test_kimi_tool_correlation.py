#!/usr/bin/env python3
"""Gate: a kimi tool RESULT frame knows which tool it came from.

2026-08-22: kimi's live feed was unreadable in the run terminal.

kimi's `role:"tool"` line carries the result and nothing that names the tool —
its keys are exactly {content, role, tool_call_id}. The parser read
o.get("name"), which is never present, so every result frame carried tool="":
38 of 38 in one factory build. The run terminal could not label or pair
a single tool result.

The name lives on the assistant line that opened the call, as
tool_calls[].function.name beside tool_calls[].id, and tool_call_id refers back
to that id — so correlating needs memory ACROSS lines.

Every line below has the exact wire shape of a raw kimi transcript
(2026-08-22), with the ids and output replaced, so the gate is pinned to the
shape the vendor actually emits.

Run: APP_ROOT=<repo> python3 scripts/test_kimi_tool_correlation.py
"""
import json
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "system-apps"))

from _harness_runtime import events as EV  # noqa: E402

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


# ── in the shape of a captured harness run ──────────────────────────────────
# ONE assistant line carrying FOUR tool calls: three Bash and one Read. This
# shape is the whole reason a "use the last tool_call seen" heuristic is wrong —
# it would label all four results "Read".
ASSISTANT = json.dumps({
    "role": "assistant",
    "content": "Let me check the environment.",
    "tool_calls": [
        {"id": "tool_call_0001", "type": "function",
         "function": {"name": "Bash", "arguments": '{"command":"node -v"}'}},
        {"id": "tool_call_0002", "type": "function",
         "function": {"name": "Bash", "arguments": '{"command":"curl -sI x"}'}},
        {"id": "tool_call_0003", "type": "function",
         "function": {"name": "Bash", "arguments": '{"command":"ls"}'}},
        {"id": "tool_call_0004", "type": "function",
         "function": {"name": "Read", "arguments": '{"path":"index.html"}'}},
    ],
})
# The result lines, verbatim in shape: content + role + tool_call_id, no name.
RESULTS = [
    json.dumps({"role": "tool", "tool_call_id": tid, "content": body})
    for tid, body in (
        ("tool_call_0001",
         "/bin/bash: line 1: node: command not found\nPython 3.14.6\n"),
        ("tool_call_0002", "HTTP/1.1 200 OK\r\n"),
        ("tool_call_0003", "index.html\n"),
        ("tool_call_0004", "<!doctype html>\n"),
    )
]
WANT = ["Bash", "Bash", "Bash", "Read"]


def _drive(lines, state):
    """Feed lines through the real normalize_line, as harness_core does."""
    evs, seq = [], 0
    for ln in lines:
        got = EV.normalize_line("kimi", ln, seq, state)
        seq += len(got)
        evs.extend(got)
    return evs


# ── 1. the result frames resolve their real tool names ──────────────────────
state: dict = {}
evs = _drive([ASSISTANT] + RESULTS, state)
results = [e for e in evs if e["kind"] == "tool_result"]
check("all four results are parsed", len(results) == 4, f"got {len(results)}")
check("no result frame carries an EMPTY tool name — the shipped defect",
      all(r["payload"]["tool"] for r in results),
      f'{[r["payload"]["tool"] for r in results]}')
check("each result resolves the name of ITS OWN call, not the last one seen",
      [r["payload"]["tool"] for r in results] == WANT,
      f'got {[r["payload"]["tool"] for r in results]} want {WANT}')

# ── 2. red/green — the pre-fix read really does produce "" ──────────────────
raw_tool_line = json.loads(RESULTS[0])
check("red/green — the old o.get('name') read yields nothing on a real line",
      not raw_tool_line.get("name"), f"{raw_tool_line.get('name')!r}")
check("...and the line genuinely has no name-ish key",
      sorted(raw_tool_line.keys()) == ["content", "role", "tool_call_id"],
      f"{sorted(raw_tool_line.keys())}")

# ── 3. results still parse without state (old callers, other harnesses) ─────
evs_nostate = [
    e for ln in [ASSISTANT] + RESULTS for e in EV.normalize_line("kimi", ln, 0)
]
nores = [e for e in evs_nostate if e["kind"] == "tool_result"]
check("without state a result still parses and is labelled, never blank",
      len(nores) == 4 and all(r["payload"]["tool"] == "tool" for r in nores),
      f'{[r["payload"]["tool"] for r in nores]}')

# ── 4. an unknown id is labelled generically, never mislabelled ─────────────
state2: dict = {}
_drive([ASSISTANT], state2)
orphan = EV.normalize_line(
    "kimi", json.dumps({"role": "tool", "tool_call_id": "tool_UNSEEN",
                        "content": "x"}), 0, state2)
check("a result whose id was never announced is 'tool', not a wrong name",
      orphan[0]["payload"]["tool"] == "tool", f'{orphan[0]["payload"]["tool"]}')

# ── 5. the correlation map does not grow without bound ──────────────────────
state3: dict = {}
for i in range(EV._KIMI_TOOL_NAME_CAP + 250):
    EV.normalize_line("kimi", json.dumps({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": f"tool_{i}", "type": "function",
                        "function": {"name": "Bash", "arguments": "{}"}}],
    }), 0, state3)
check("the id->name map stays bounded on a long run",
      len(state3[EV._KIMI_TOOL_NAMES]) <= EV._KIMI_TOOL_NAME_CAP,
      f"{len(state3[EV._KIMI_TOOL_NAMES])}")

# ── 6. two concurrent runs cannot cross-label ───────────────────────────────
# The state is caller-owned per run, so a second run's ids are invisible to the
# first. Module-level memory would have made this cross-talk.
a: dict = {}
b: dict = {}
_drive([ASSISTANT], a)
res_b = EV.normalize_line(
    "kimi", RESULTS[0], 0, b)          # run B has never seen run A's ids
check("a second run does not resolve the first run's tool ids",
      res_b[0]["payload"]["tool"] == "tool", f'{res_b[0]["payload"]["tool"]}')

# ── 7. non-kimi harnesses are untouched by the signature change ─────────────
grok = EV.normalize_line("grok", json.dumps({"type": "text", "text": "hi"}), 0)
check("another harness still parses with no state argument", len(grok) == 1)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
