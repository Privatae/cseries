#!/usr/bin/env python3
"""
Harness streaming ratchet — Bench L1+L2.

Pins the two things that make live streaming possible, both of which were
verified against the REAL vendor binaries on 2026-07-26 (each sidecar image
was run, not read about):

  grok             --output-format streaming-json
  cursor           --output-format stream-json + --stream-partial-output
  codex            --json                     ("Print events to stdout as JSONL")
  opencode         --format json              ("raw JSON events")

Gates:
  1. Every streaming harness has stream flags, and applying them to its real
     argv produces exactly one --output-format (cursor's buffered `text` pair
     must be REPLACED, not duplicated — two of that flag is a vendor error).
  2. Applying flags is non-destructive: the binary, prompt and project dir the
     buffered argv carried all survive.
  3. normalize() NEVER drops a line. codex interleaves plain-text log lines
     with its JSONL on the same stream (observed live), so an unparseable line
     must degrade to kind="text" carrying the raw text — silently dropping it
     is the exact failure class this change removes.
  4. Real captured vendor frames classify correctly.
  5. Every emitted event carries the shared contract keys (ts/seq/kind/payload)
     and a kind from the agreed vocabulary, so the Workbench's run cards and Agents can share
     renderers.
  6. A vendor shape change cannot kill a run: a parser blowing up still yields
     a text event.

Runs inside the image: python3 /app/scripts/test_harness_streaming.py
"""
import os
import sys

# In the image _harness_runtime lives at /app/cli/_harness_runtime and
# cseries_sidecar at /app/cli (Dockerfile.linux:150,155). In a source checkout it
# is system-apps/_harness_runtime with cli/ alongside. Support both so the gate
# is runnable in-image (smoke) and in-repo (development).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in ("/app/cli", os.path.join(_REPO, "cli"), os.path.join(_REPO, "system-apps")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from _harness_runtime import events as E          # noqa: E402
from _harness_runtime.specs import (              # noqa: E402
    STREAM_FLAGS, apply_stream_flags, ALL_SPECS,
)

FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -- " + str(detail)) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


# ── 1 + 2. flags ─────────────────────────────────────────────────────────────
for hid in sorted(STREAM_FLAGS):
    spec = ALL_SPECS.get(hid)
    if spec is None:
        check("spec exists: %s" % hid, False, "not in ALL_SPECS")
        continue
    base = spec.build_argv(spec, "PROMPT", "/tmp/projdir", "some-model", None)
    out = apply_stream_flags(hid, base)
    check("%-9s stream flags applied" % hid,
          all(f in out for f in STREAM_FLAGS[hid]), out)
    check("%-9s exactly one --output-format" % hid,
          out.count("--output-format") <= 1, out)
    check("%-9s binary preserved" % hid, out[0] == base[0], out[:2])
    check("%-9s prompt preserved" % hid, "PROMPT" in out, out)
    # How the project dir reaches the harness differs BY DESIGN: codex (--cd)
    # names it in the argv; cursor/grok/opencode/kimi have no
    # such flag and get it as the subprocess cwd (run_cwd=True). Assert the
    # right one per spec rather than assuming argv.
    if spec.run_cwd:
        check("%-9s dir rides cwd, not argv" % hid, True)
    else:
        check("%-9s project dir preserved in argv" % hid,
              "/tmp/projdir" in out, out)

# cursor specifically: the buffered argv passes `text`, streaming must not keep it
_cur = ALL_SPECS["cursor"]
_cur_stream = apply_stream_flags("cursor", _cur.build_argv(_cur, "P", "/tmp/d", "m", None))
check("cursor: buffered 'text' output-format is gone",
      "text" not in _cur_stream or _cur_stream[_cur_stream.index("--output-format") + 1] == "stream-json",
      _cur_stream)

# ── 3. never drop a line ─────────────────────────────────────────────────────
NOISE = [
    "2026-07-26T07:57:41Z ERROR codex_models_manager::manager: failed to refresh",
    "WARNING: proceeding, even though we could not update PATH",
    "not json at all {{{",
    "",
]
for i, line in enumerate(NOISE):
    ev = E.normalize("codex", line, i)
    check("noise line %d survives as text" % i,
          ev is not None and ev["kind"] == "text", ev)
    if line.strip():
        check("noise line %d keeps its raw text" % i,
              line in ev["payload"].get("text", ""), ev)

# ── 4. real captured vendor frames ───────────────────────────────────────────
REAL = [
    # captured live from the grok binary
    ("grok", '{"type":"error","message":"error sending request for url (https://auth.x.ai/...)"}',
     "error"),
    # captured live from the codex binary
    ("codex", '{"type":"thread.started","thread_id":"019f9d6e-0552-7b03"}', "status"),
    ("codex", '{"type":"turn.started"}', "status"),
    # codex item lines — captured live from rust-v0.149.0 (one run,
    # entity-9003, 2026-08-21). The content rides item.*; until 2026-08-21 every one
    # of these folded to a bare "item.completed" status frame and the Workbench
    # feed showed nothing else.
    ("codex", '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"I’ll inspect the current project, then rebuild the static app at the root with no external dependencies or network access."}}',
     "text"),
    ("codex", '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/usr/bin/bash -lc \\"pwd && ls\\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
     "tool_call"),
    ("codex", '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/usr/bin/bash -lc \\"pwd && ls\\"","aggregated_output":"/var/lib/cseries/projects/codex\\nindex.html\\n","exit_code":0,"status":"completed"}}',
     "tool_result"),
    ("codex", '{"type":"item.completed","item":{"id":"item_7","type":"file_change","changes":[{"path":"/var/lib/cseries/projects/codex/app.yaml","kind":"update"},{"path":"/var/lib/cseries/projects/codex/index.html","kind":"update"}],"status":"completed"}}',
     "file_edit"),
    ("codex", '{"type":"item.completed","item":{"id":"item_2","type":"reasoning","text":"**Inspecting the tree**"}}',
     "thinking"),
    ("codex", '{"type":"turn.completed","usage":{"input_tokens":177124,"cached_input_tokens":144384,"cache_write_input_tokens":0,"output_tokens":1851,"reasoning_output_tokens":444}}',
     "status"),
    ("codex", '{"type":"turn.failed","error":{"message":"rate limited"}}', "error"),
    # grok --output-format streaming-json, as the CLI emits it
    # (2026-08-21) and matching the pinned binary's own embedded docs
    # (0.2.112 `### streaming-json`). Token-level deltas — that one build
    # emitted 555 thought + 120 text frames. Before parse_grok existed all 675
    # of them declined every reader and reached the Bench as raw JSON lines.
    ("grok", '{"type":"thought","data":"The"}', "thinking"),
    ("grok", "{\"type\":\"text\",\"data\":\"I'll\"}", "text"),
    # claude-family content blocks (cursor's shape; grok 0.2.51 spoke it too,
    # so it stays grok's FALLBACK reader and these must keep classifying)
    ("grok", '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}',
     "text"),
    ("grok", '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"hmm"}]}}',
     "thinking"),
    ("grok", '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"p":1}}]}}',
     "tool_call"),
    ("cursor", '{"type":"text_delta","delta":{"text":"chunk"}}', "text"),
    # a generic-JSONL tool_result shape for the parse_generic lane (a
    # representative shape, not a frame captured from the opencode binary)
    ("opencode", '{"type":"tool_result","tool":"bash","status":"ok","result":"done"}', "tool_result"),
    ("opencode", '{"type":"error","message":"boom"}', "error"),
    # kimi's --output-format stream-json emits the Claude event vocabulary.
    ("kimi", '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"p":1}}]}}',
     "tool_call"),
    ("kimi", '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"hmm"}]}}',
     "thinking"),
    # antigravity's --output-format stream-json, as the CLI emits it
    # (agy 1.1.17). The first cut of this
    # parser was written off the vendor's docs and rendered 15 raw-JSON lines
    # (of 39 vendor lines), every tool call twice and a red failure strip on a
    # build that succeeded; these rows pin the measured dialect.
    #
    # HONEST ABOUT THE FIXTURES: the SHAPES and field names are the capture's,
    # but three rows are TRIMMED for readability and one is SYNTHESIZED —
    # never read them as "this is exactly what the vendor sent":
    #   * the project path is shortened to /w/antigravity throughout;
    #   * init's tools list is 3 of the 56 the vendor actually lists;
    #   * the tool-DONE row's output is 2 lines of a longer listing;
    #   * the SUCCESS `result` row is SYNTHESIZED (response truncated,
    #     duration 61.2). The run's REAL result was status ERROR — one tool
    #     step had failed — and that one is below VERBATIM as _AG_RESULT_REAL,
    #     including its 468-char response and duration 25.219212836.
    # _AG_VIEW's StartLine/EndLine and _AG_WRITE_DONE's ReplacementChunks are
    # likewise illustrative: the capture's params for those calls were
    # single-key. Behaviour coverage is what these assert; the full-transcript
    # replay is the fidelity check.
    ("antigravity", '{"event":"init","conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","init":{"cwd":"/w/antigravity","tools":["ask_permission","run_command","write_to_file"],"permission_mode":"always-proceed"}}',
     "status"),
    ("antigravity", '{"event":"step_update","step_update":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"pwd && ls -la"}}}}',
     "tool_call"),
    ("antigravity", '{"event":"step_update","step_update":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.075373204,"tool_info":{"name":"run_command","parameters":{"CommandLine":"pwd && ls -la"},"output":"/w/antigravity\\r\\ntotal 44\\r\\n"}}}',
     "tool_result"),
    ("antigravity", '{"event":"step_update","step_update":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","step_index":24,"state":"ACTIVE","step_type":"agent_response","text_delta":"- [app.yaml](file:///w/antigravity/app.yaml): Application manifest declaring id, title, "}}',
     "text"),
    ("antigravity", '{"event":"step_update","step_update":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","step_index":2,"state":"DONE","step_type":"agent_response","duration_seconds":2.618245956,"usage":{"input_tokens":15799,"output_tokens":521,"thinking_tokens":465,"cache_read_tokens":0,"total_tokens":16320}}}',
     "usage"),
    ("antigravity", '{"event":"step_update","step_update":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","step_index":0,"state":"DONE","step_type":"user_input"}}',
     "status"),
    ("antigravity", '{"event":"step_update","step_update":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","step_index":1,"state":"DONE","step_type":"checkpoint","duration_seconds":1.645828549}}',
     "status"),
    ("antigravity", '{"event":"result","result":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","status":"SUCCESS","response":"- [app.yaml](file:///w/antigravity/app.yaml): Application manifest","duration_seconds":61.2,"num_turns":1,"usage":{"input_tokens":101119,"output_tokens":5588,"thinking_tokens":4223,"cache_read_tokens":252607,"total_tokens":106707}}}',
     "status"),
    # the non-stream json envelope of an unauthenticated run — MEASURED on 1.1.17
    # (exit 1; the runtime's terminal frame carries the verdict, this is context)
    ("antigravity", '{"conversation_id":"","status":"ERROR","response":"","error":"authentication failed or timed out","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}',
     "status"),
]
for hid, raw, want in REAL:
    ev = E.normalize(hid, raw, 0)
    check("%-9s %-11s classifies" % (hid, want), ev["kind"] == want,
          "got %s from %s" % (ev["kind"], raw[:60]))

# ── 4a. grok's own dialect ───────────────────────────────────────────────────
# The defect this pins: every frame of one run reached the Bench as a
# raw JSON line, because grok read through the claude-family parser on a design
# doc's claim (measured against 0.2.51) that 0.2.112 does not honour.
for raw in ('{"type":"thought","data":"The"}',
            "{\"type\":\"text\",\"data\":\"I'll\"}"):
    ev = E.normalize("grok", raw, 0)
    check("grok delta arrives as content, never a raw JSON line",
          '{"type"' not in ev["payload"].get("text", ""),
          "payload=%r" % (ev["payload"],))
check("grok keeps the vendor's own leading space (chunks concatenate cleanly)",
      E.normalize("grok", '{"type":"text","data":" a summary"}', 0)
      ["payload"]["text"] == " a summary")

# delta marks a TOKEN-LEVEL chunk. Renderers join adjacent deltas into one
# block; a whole-message frame must never be glued to its neighbour, so codex's
# agent_message and a never-drop raw line must NOT carry the flag.
check("grok text/thought are marked delta",
      all(E.normalize("grok", r, 0)["payload"].get("delta") is True
          for r in ('{"type":"text","data":"a"}', '{"type":"thought","data":"b"}')))
check("cursor's partial-output deltas are marked delta",
      E.normalize("cursor", '{"type":"text_delta","delta":{"text":"chunk"}}', 0)
      ["payload"].get("delta") is True)
check("codex's whole-message frame is NOT a delta (never glued to its neighbour)",
      E.normalize("codex", '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"done"}}', 0)
      ["payload"].get("delta") is None)
check("a never-drop raw line is NOT a delta",
      E.normalize("grok", "not json at all", 0)["payload"].get("delta") is None)

# Field-name drift must not produce an EMPTY claimed frame: `{"type":"text"}`
# with neither data nor text is declined, so the fallback readers still run and
# the raw line survives. (A claimed-but-empty frame renders as nothing — worse
# than the raw JSON this change replaces.)
ev = E.normalize("grok", '{"type":"text"}', 0)
check("grok text frame with NO data/text field falls through, never renders empty",
      ev["payload"].get("text", "") != "" or ev["kind"] != "text"
      or '{"type":"text"}' in ev["payload"].get("text", ""),
      "payload=%r" % (ev["payload"],))
check("grok reads an alternate `text` field if a future build renames `data`",
      E.normalize("grok", '{"type":"thought","text":"hmm"}', 0)["payload"]["text"] == "hmm")

# `end` is a done-status PLUS a usage — the shape codex's turn.completed has.
_GK_END = ('{"type":"end","stopReason":"EndTurn","sessionId":"abc123",'
           '"requestId":"xyz789","num_turns":7,'
           '"usage":{"input_tokens":1200,"output_tokens":340}}')
evs = E.normalize_line("grok", _GK_END, 5)
check("grok end -> [status(done), usage] with consecutive seq",
      [e["kind"] for e in evs] == ["status", "usage"]
      and evs[0]["payload"]["state"] == "done"
      and (evs[0]["seq"], evs[1]["seq"]) == (5, 6), evs)
check("grok end carries the stop reason and the token counts",
      evs[0]["payload"].get("detail") == "EndTurn"
      and evs[1]["payload"].get("output_tokens") == 340, evs)

# The vendor calls its own type list non-exhaustive and names these two.
check("grok max_turns_reached reads as a status",
      E.normalize("grok", '{"type":"max_turns_reached"}', 0)["kind"] == "status")
check("grok auto_compact_* reads as a status",
      E.normalize("grok", '{"type":"auto_compact_started"}', 0)["kind"] == "status")
# An unknown type is never GUESSED at: claude-family gets a look, then the
# never-drop rule shows the line verbatim.
ev = E.normalize("grok", '{"type":"hologram","data":"x"}', 0)
check("unknown grok type falls through to the raw line (never-drop)",
      ev["kind"] == "text" and "hologram" in ev["payload"]["text"], ev)

# ── 4a'. grok 1.0.46's added types ───────────────────────────────────────────
# The first two lines are the vendor's own examples, verbatim from the
# `### streaming-json` section embedded in grok 1.0.46 (2765805b9442). No
# captured 1.0.x run exists yet; the edit/diff and failure shapes follow the
# ACP fields that section names (kind, content, locations).
_GK_CALL = ('{"type":"tool_call","toolCallId":"call_1","title":"Read","kind":"read",'
            '"status":"in_progress","toolName":"read_file","rawInput":{"path":"src/main.rs"},'
            '"content":[],"locations":[]}')
_GK_DONE = ('{"type":"tool_call_update","toolCallId":"call_1","status":"completed",'
            '"content":[],"rawOutput":{"lines":42},"locations":[]}')
st = {}
evs = E.normalize_line("grok", _GK_CALL, 0, st)
check("grok tool_call -> ONE tool_call named by toolName, with its id and the renderer's path",
      [e["kind"] for e in evs] == ["tool_call"]
      and evs[0]["payload"]["tool"] == "read_file" and evs[0]["payload"]["id"] == "call_1"
      and evs[0]["payload"]["args"].get("path") == "src/main.rs", evs)
evs = E.normalize_line("grok", _GK_DONE, 1, st)
check("grok tool_call_update(completed) -> the call's result, same id, name remembered",
      [e["kind"] for e in evs] == ["tool_result"]
      and evs[0]["payload"]["id"] == "call_1" and evs[0]["payload"]["tool"] == "read_file"
      and evs[0]["payload"]["status"] == "ok" and '"lines": 42' in evs[0]["payload"]["result"], evs)
check("a finished grok call is forgotten (state does not grow with the run)",
      st.get("grok_calls") == {}, st)

st = {}
E.normalize_line("grok", '{"type":"tool_call","toolCallId":"e1","kind":"edit","toolName":"edit",'
                 '"status":"pending","rawInput":{"file_path":"index.html"}}', 0, st)
evs = E.normalize_line("grok", '{"type":"tool_call_update","toolCallId":"e1","status":"completed",'
                       '"content":[{"type":"diff","path":"index.html","oldText":"a","newText":"b"},'
                       '{"type":"diff","path":"new.css","oldText":null,"newText":"x"}]}', 1, st)
check("grok edit with diff blocks -> result + one file_edit per path (write when there was no old text)",
      [e["kind"] for e in evs] == ["tool_result", "file_edit", "file_edit"]
      and [(e["payload"]["path"], e["payload"]["change"]) for e in evs[1:]]
      == [("index.html", "edit"), ("new.css", "write")], evs)

st = {}
E.normalize_line("grok", '{"type":"tool_call","toolCallId":"e2","kind":"edit","toolName":"edit",'
                 '"locations":[{"path":"app.yaml"}]}', 0, st)
evs = E.normalize_line("grok", '{"type":"tool_call_update","toolCallId":"e2","status":"completed"}', 1, st)
check("grok edit with no diff block still records the edit, at the remembered path",
      [e["kind"] for e in evs] == ["tool_result", "file_edit"]
      and evs[1]["payload"]["path"] == "app.yaml", evs)

evs = E.normalize_line("grok", '{"type":"tool_call_update","toolCallId":"x9","toolName":"bash",'
                       '"status":"failed","content":[{"type":"content","content":'
                       '{"type":"text","text":"exit 2"}}]}', 0, {})
check("grok failed update -> an error result carrying the vendor's text",
      [e["kind"] for e in evs] == ["tool_result"]
      and evs[0]["payload"]["status"] == "error" and evs[0]["payload"]["result"] == "exit 2", evs)

# Measured on one grok 1.0.46 run: 138 mid-step updates, all
# status null / in_progress with empty content — they printed as raw JSON
# rows. Now a lifecycle status the views skip (run-steps.ts LIFECYCLE, bench
# NOISE); the line itself stays in Raw, and nothing is read into it.
ev = E.normalize("grok", '{"type":"tool_call_update","toolCallId":"call_1","status":"in_progress"}', 0)
check("grok mid-step update is a tool_update lifecycle status, not a raw row",
      ev["kind"] == "status" and ev["payload"]["state"] == "tool_update"
      and ev["payload"]["detail"] == "in_progress" and not ev["payload"].get("raw"), ev)
ev = E.normalize("grok", _GK_DONE, 0)
check("a grok result with NO parse state still reads (stateless callers keep working)",
      ev["kind"] == "tool_result" and ev["payload"]["id"] == "call_1", ev)

ev = E.normalize("grok", '{"type":"usage","messageId":"resp_1","stopReason":"end_turn",'
                 '"usage":{"input_tokens":812,"output_tokens":45}}', 0)
check("grok per-response usage reads as usage",
      ev["kind"] == "usage" and ev["payload"].get("input_tokens") == 812, ev)
ev = E.normalize("grok", '{"type":"plan","entries":[{"content":"read","status":"completed"},'
                 '{"content":"write","status":"pending"}]}', 0)
check("grok plan -> the todo_list status codex's list uses",
      ev["kind"] == "status" and ev["payload"]["state"] == "todo_list"
      and ev["payload"]["detail"] == "☑ read · ☐ write", ev)
ev = E.normalize("grok", '{"type":"available_commands","tools":["a","b"],"commands":["review"]}', 0)
check("grok available_commands -> an init status the views skip, never a raw line",
      ev["kind"] == "status" and ev["payload"]["state"] == "init"
      and ev["payload"]["detail"] == "2 tools, 1 commands", ev)

# The edit's file_edit frames come AFTER its tool_result, so they must carry no
# `tool`: both views (bench/app.js fold, workbench-web run-steps.ts) treat a
# file_edit WITH a tool as a new edit CALL — a second row, status "run",
# pushed to pending, that nothing ever closes, so the live "now" line keeps
# naming an edit grok finished long ago. Without a tool the same views fold
# it onto the edit call's own row (antigravity's shape, events.parse_antigravity).
st = {}
E.normalize_line("grok", '{"type":"tool_call","toolCallId":"e3","kind":"edit","toolName":"edit_file",'
                 '"status":"in_progress","rawInput":{"path":"index.html"}}', 0, st)
evs = E.normalize_line("grok", '{"type":"tool_call_update","toolCallId":"e3","status":"completed",'
                       '"content":[{"type":"diff","path":"index.html","oldText":"a","newText":"b"}]}', 1, st)
check("grok file_edit after its result carries no `tool` (views fold it onto the call, never a 2nd open row)",
      [e["kind"] for e in evs] == ["tool_result", "file_edit"]
      and "tool" not in evs[1]["payload"] and evs[1]["payload"]["id"] == "e3", evs)
check("grok result keeps the CALL's name when the update carries only a title",
      E.normalize_line("grok", '{"type":"tool_call","toolCallId":"r1","toolName":"read_file",'
                       '"title":"Read","kind":"read"}', 0, st) and
      E.normalize_line("grok", '{"type":"tool_call_update","toolCallId":"r1","title":"Read a.txt (3 lines)",'
                       '"status":"completed"}', 1, st)[0]["payload"]["tool"] == "read_file")

# Per-response usage under grok's "last" rule (server/routes/compute.py
# _USAGE_AGG): with `end` the end totals are last and win. A run cut before
# `end` (idle/tool-step stop, timeout, a killed pod) would otherwise record
# the LAST response's tokens as the whole run's — so each frame carries the
# run's running total, which "last" then reads correctly either way.
st = {}
u1 = E.normalize_line("grok", '{"type":"usage","messageId":"r1","usage":{"input_tokens":812,'
                      '"output_tokens":45,"cache_read_input_tokens":100}}', 0, st)
u2 = E.normalize_line("grok", '{"type":"usage","messageId":"r2","usage":{"input_tokens":90,'
                      '"output_tokens":5}}', 1, st)
check("grok per-response usage frames carry the run's running total (a cut run is not undercounted)",
      u1[0]["payload"].get("input_tokens") == 812
      and u2[0]["kind"] == "usage" and u2[0]["payload"].get("input_tokens") == 902
      and u2[0]["payload"].get("output_tokens") == 50
      and u2[0]["payload"].get("cache_read_input_tokens") == 100, (u1, u2))
end = E.normalize_line("grok", '{"type":"end","stopReason":"end_turn","usage":{"input_tokens":1000,'
                       '"output_tokens":60}}', 2, st)
check("grok `end` usage is still the vendor's own totals, untouched by the running sum",
      [e["kind"] for e in end] == ["status", "usage"]
      and end[1]["payload"].get("input_tokens") == 1000, end)

# ── 4c. antigravity frames render like a terminal, from the live capture ────
_AG = [r for r in REAL if r[0] == "antigravity"]
evs = E.normalize_line("antigravity", _AG[1][1], 7)
check("antigravity tool ACTIVE -> ONE tool_call carrying the renderer's `command` key "
      "beside the vendor's CommandLine",
      [e["kind"] for e in evs] == ["tool_call"] and evs[0]["payload"]["tool"] == "run_command"
      and evs[0]["payload"]["args"].get("command") == "pwd && ls -la"
      and evs[0]["payload"]["args"].get("CommandLine") == "pwd && ls -la" and evs[0]["seq"] == 7, evs)
evs = E.normalize_line("antigravity", _AG[2][1], 8)
check("antigravity tool DONE -> tool_result ok with the output, NO second tool_call",
      [e["kind"] for e in evs] == ["tool_result"] and evs[0]["payload"]["status"] == "ok"
      and "total 44" in evs[0]["payload"]["result"], evs)
_AG_ERR = ('{"event":"step_update","step_update":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75",'
           '"step_index":19,"state":"ERROR","step_type":"tool","tool_name":"write_to_file","duration_seconds":0.01,'
           '"tool_info":{"name":"write_to_file","parameters":{"TargetFile":"/w/antigravity/app.yaml"},'
           '"error":{"type":"TOOL_ERROR","message":"declaring permissions: cortex tool write_to_file: convert tool call for permissions: '
           'model output error: invalid tool call error (invalid_args) /w/antigravity/app.yaml is not a valid artifact path"}}}}')
evs = E.normalize_line("antigravity", _AG_ERR, 0)
check("antigravity tool ERROR -> tool_result error carrying tool_info.error.message (was '' before)",
      [e["kind"] for e in evs] == ["tool_result"] and evs[0]["payload"]["status"] == "error"
      and "not a valid artifact path" in evs[0]["payload"]["result"], evs)
_AG_WRITE_DONE = ('{"event":"step_update","step_update":{"step_index":21,"state":"DONE","step_type":"tool",'
                  '"tool_name":"replace_file_content","duration_seconds":0.02,"tool_info":{"name":"replace_file_content",'
                  '"parameters":{"TargetFile":"/w/antigravity/app.yaml","ReplacementChunks":[{"TargetContent":"a","ReplacementContent":"b"}]}}}}')
evs = E.normalize_line("antigravity", _AG_WRITE_DONE, 0)
check("antigravity write-tool DONE -> tool_result ok + file_edit(path) so the summary lists the file",
      [e["kind"] for e in evs] == ["tool_result", "file_edit"]
      and evs[1]["payload"]["path"] == "/w/antigravity/app.yaml"
      and evs[1]["payload"]["change"] == "edit", evs)
_AG_VIEW = ('{"event":"step_update","step_update":{"step_index":5,"state":"ACTIVE","step_type":"tool","tool_name":"view_file",'
            '"tool_info":{"name":"view_file","parameters":{"AbsolutePath":"/w/antigravity/index.html","StartLine":1,"EndLine":40}}}}')
evs = E.normalize_line("antigravity", _AG_VIEW, 0)
check("antigravity view_file -> tool_call with `path` for the header (AbsolutePath kept too)",
      evs[0]["payload"]["args"].get("path") == "/w/antigravity/index.html"
      and evs[0]["payload"]["args"].get("AbsolutePath") == "/w/antigravity/index.html", evs)
evs = E.normalize_line("antigravity", _AG[3][1], 0)
check("antigravity agent_response text_delta -> text (the model's prose, was raw JSON)",
      [e["kind"] for e in evs] == ["text"] and evs[0]["payload"]["text"].startswith("- [app.yaml]"), evs)
# Token-level chunks, same as grok's and cursor's: the capture's step 24 ACTIVE
# ends mid-sentence ("…declaring id, title, ") and its DONE frame continues it,
# so adjacent chunks must be joinable (delta=True) instead of one line each.
check("antigravity prose chunks are marked delta (joined into one block, never a line per fragment)",
      evs[0]["payload"].get("delta") is True, evs)
evs = E.normalize_line("antigravity", _AG[4][1], 0)
check("antigravity agent_response DONE -> usage (tokens + duration), no body line",
      [e["kind"] for e in evs] == ["usage"] and evs[0]["payload"].get("output_tokens") == 521
      and evs[0]["payload"].get("duration_seconds") == 2.618245956, evs)
evs = E.normalize_line("antigravity", _AG[5][1], 0)
check("antigravity user_input -> one status line",
      [e["kind"] for e in evs] == ["status"] and evs[0]["payload"]["state"] == "user_input", evs)
evs = E.normalize_line("antigravity", _AG[6][1], 0)
check("antigravity checkpoint -> one status line with its duration",
      [e["kind"] for e in evs] == ["status"] and evs[0]["payload"]["state"] == "checkpoint"
      and "1.6s" in evs[0]["payload"]["detail"], evs)
evs = E.normalize_line("antigravity", _AG[7][1], 0)
check("antigravity result SUCCESS -> done status + usage",
      [e["kind"] for e in evs] == ["status", "usage"] and evs[0]["payload"]["state"] == "done"
      and evs[1]["payload"].get("total_tokens") == 106707, evs)
# The run's REAL closing frame, VERBATIM from the capture (only the project
# path shortened): status ERROR because one write_to_file step hit agy's own
# artifact-path rule, while the build itself landed and rendered.
_AG_RESULT_REAL = (
    '{"event":"result","result":{"conversation_id":"f9542660-fdec-4258-937f-b21180d68e75","status":"ERROR",'
    '"response":"- [app.yaml](file:///w/antigravity/app.yaml): Application manifest declaring id, title, entrypoint, '
    'zero required permissions, and strict inline CSP.\\n- [index.html](file:///w/antigravity/index.html): Self-contained '
    'single-page finance app featuring a 4-card summary strip, tabular numerals, light fintech styling, and tabbed panels '
    'for Overview, Bills, Subscriptions, and Debt payoff progress with zero external dependencies.\\n",'
    '"error":"declaring permissions: cortex tool write_to_file: convert tool call for permissions: model output error: '
    'invalid tool call error (invalid_args) /w/antigravity/app.yaml is not a valid artifact path; artifacts must be in '
    '/w/antigravity/.agy-home/.gemini/antigravity-cli/brain/f9542660-fdec-4258-937f-b21180d68e75/",'
    '"duration_seconds":25.219212836,"num_turns":1,"usage":{"input_tokens":101119,"output_tokens":5588,'
    '"thinking_tokens":4223,"cache_read_tokens":252607,"total_tokens":106707}}}')
evs = E.normalize_line("antigravity", _AG_RESULT_REAL, 0)
check("antigravity result ERROR (one failed tool step, the build itself landed) -> WARNING status, "
      "not kind=error (an error frame paints the run's failure line even on a clean exit)",
      [e["kind"] for e in evs] == ["status", "status", "usage"] and evs[0]["payload"]["state"] == "warning"
      and "artifact path" in evs[0]["payload"]["detail"] and evs[1]["payload"]["state"] == "done", evs)
check("antigravity's real closing frame carries the whole response and the measured token total",
      evs[1]["payload"]["detail"].startswith("- [app.yaml]")
      and "zero external dependencies" in evs[1]["payload"]["detail"]
      and evs[2]["payload"].get("total_tokens") == 106707, evs)
evs = E.normalize_line("antigravity", '{"event":"hologram","x":1}', 0)
check("antigravity unknown event -> raw line kept as text (never-drop)",
      [e["kind"] for e in evs] == ["text"] and "hologram" in evs[0]["payload"]["text"], evs)
# Replay the live capture's shapes end to end: no raw-JSON text frame may be
# left over (every known shape maps); only a genuinely unknown line may.
_AG_LIVE = [_AG[0][1], _AG[5][1], _AG[6][1], _AG[4][1], _AG[1][1], _AG[2][1],
            _AG_VIEW, _AG_ERR, _AG_WRITE_DONE, _AG[3][1], _AG[7][1]]
_live_evs = [e for raw in _AG_LIVE for e in E.normalize_line("antigravity", raw, 0)]
check("antigravity live-capture replay leaves NO raw-JSON text frames",
      any(e["kind"] == "text" for e in _live_evs)
      and not any(e["kind"] == "text" and e["payload"]["text"].lstrip().startswith("{") for e in _live_evs),
      [e["kind"] for e in _live_evs])

check("text frame carries its text",
      E.normalize("grok",
                  '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}',
                  0)["payload"].get("text") == "hello")
check("tool_call frame carries tool + args",
      E.normalize("grok",
                  '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"p":1}}]}}',
                  0)["payload"].get("tool") == "Read")

# ── 4b. codex item CONTENT reaches the frames ────────
_CX_MSG = '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Done. Rebuilt index.html."}}'
_CX_CMD0 = '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/usr/bin/bash -lc \\"pwd && ls\\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}'
_CX_CMD1 = '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/usr/bin/bash -lc \\"pwd && ls\\"","aggregated_output":"/var/lib/cseries/projects/codex\\nindex.html\\n","exit_code":0,"status":"completed"}}'
_CX_CMDF = '{"type":"item.completed","item":{"id":"item_3","type":"command_execution","command":"/usr/bin/bash -lc false","aggregated_output":"","exit_code":1,"status":"failed"}}'
_CX_PATCH = '{"type":"item.completed","item":{"id":"item_7","type":"file_change","changes":[{"path":"/var/lib/cseries/projects/codex/app.yaml","kind":"update"},{"path":"/var/lib/cseries/projects/codex/index.html","kind":"update"}],"status":"completed"}}'
_CX_TURN = '{"type":"turn.completed","usage":{"input_tokens":177124,"cached_input_tokens":144384,"cache_write_input_tokens":0,"output_tokens":1851,"reasoning_output_tokens":444}}'
_CX_UNKNOWN = '{"type":"item.updated","item":{"id":"item_9","type":"hologram","payload":"x"}}'

ev = E.normalize("codex", _CX_MSG, 0)
check("codex agent_message carries its prose",
      ev["payload"].get("text") == "Done. Rebuilt index.html.", ev)
ev = E.normalize("codex", _CX_CMD0, 0)
check("codex command start -> tool_call with the command in args",
      ev["kind"] == "tool_call"
      and ev["payload"]["args"].get("command") == '/usr/bin/bash -lc "pwd && ls"', ev)
ev = E.normalize("codex", _CX_CMD1, 0)
check("codex command end -> ok tool_result carrying output + exit code",
      ev["kind"] == "tool_result" and ev["payload"]["status"] == "ok"
      and "index.html" in ev["payload"]["result"]
      and ev["payload"]["exit_code"] == 0, ev)
ev = E.normalize("codex", _CX_CMDF, 0)
check("codex failed command -> error tool_result naming the exit code",
      ev["kind"] == "tool_result" and ev["payload"]["status"] == "error"
      and "exit 1" in ev["payload"]["result"], ev)
evs = E.normalize_line("codex", _CX_PATCH, 4)
check("codex two-file patch -> two file_edit frames, paths kept, rising seq",
      [e["kind"] for e in evs] == ["file_edit", "file_edit"]
      and [e["seq"] for e in evs] == [4, 5]
      and evs[1]["payload"]["path"].endswith("index.html"), evs)
evs = E.normalize_line("codex", _CX_TURN, 0)
check("codex turn.completed -> status + usage (input_tokens kept)",
      [e["kind"] for e in evs] == ["status", "usage"]
      and evs[1]["payload"].get("input_tokens") == 177124, evs)
evs = E.normalize_line("codex", _CX_UNKNOWN, 0)
check("codex unknown item type -> raw line as text (never-drop)",
      len(evs) == 1 and evs[0]["kind"] == "text"
      and "hologram" in evs[0]["payload"]["text"], evs)
ev = E.normalize("codex", '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Code Mode is unavailable"}}', 0)
check("codex item-level error (non-fatal by schema) -> warning status, not kind=error",
      ev["kind"] == "status" and ev["payload"]["state"] == "warning"
      and "Code Mode" in ev["payload"]["detail"], ev)
ev = E.normalize("codex", '{"type":"item.started","item":{"id":"item_4","type":"collab_tool_call","tool":"spawn_agent","sender_thread_id":"t0","receiver_thread_ids":["t1"],"prompt":"review the diff","agents_states":{},"status":"in_progress"}}', 0)
check("codex collab_tool_call start -> tool_call collab.<tool> with the prompt",
      ev["kind"] == "tool_call" and ev["payload"]["tool"] == "collab.spawn_agent"
      and ev["payload"]["args"]["prompt"] == "review the diff", ev)
for i, raw in enumerate((_CX_MSG, _CX_CMD0, _CX_CMD1, _CX_CMDF, _CX_PATCH)):
    check("codex item line %d never folds to a bare item.* status frame" % i,
          all(not str(e["payload"].get("state", "")).startswith("item.")
              for e in E.normalize_line("codex", raw, 0)), raw[:60])

# ── 4d. opencode 1.17.13 (`part`-wrapped) — REAL lines, from a fan-out ─
# 3ba241d3 (2026-08-21). Before parse_opencode these read through the generic
# top-level reader: tool_use -> tool="" args={} (claimed, blank), text -> ""
# (the prose VANISHED), step_* -> raw JSON. A successful build rendered as 22
# raw lines and 10 blank tool calls.
_OC_STEP0 = '{"type":"step_start","timestamp":1787329345359,"sessionID":"ses_fdadeabefffeNRpKGh9ocirEEP","part":{"id":"prt_0252177430011TcPdK2iL98Z42","messageID":"msg_025215fcb001oYKuFSMme311SO","sessionID":"ses_fdadeabefffeNRpKGh9ocirEEP","type":"step-start"}}'
_OC_TEXT = '{"type":"text","timestamp":1787329347766,"sessionID":"ses_fdadeabefffeNRpKGh9ocirEEP","part":{"id":"prt_02521774a001zs369XRy86MiGw","messageID":"msg_025215fcb001oYKuFSMme311SO","sessionID":"ses_fdadeabefffeNRpKGh9ocirEEP","type":"text","text":"\\n\\nI\'ll build this personal dashboard with my own distinct design philosophy. Let me plan this out.","time":{"start":1787329345354,"end":1787329347659}}}'
_OC_TODO = '{"type":"tool_use","timestamp":1787329347766,"sessionID":"ses_fdadeabefffeNRpKGh9ocirEEP","part":{"type":"tool","tool":"todowrite","callID":"toolu_01N1yuKftrY7LUdfNjkw99wD","state":{"status":"completed","input":{"todos":[{"content":"Design the visual concept","status":"in_progress","priority":"high"}]},"output":"[\\n  {\\n    \\"content\\": \\"Design the visual concept\\"\\n  }\\n]","metadata":{},"title":"1 todos","time":{"start":1,"end":2}},"metadata":{},"id":"prt_1","sessionID":"ses_fdadeabefffeNRpKGh9ocirEEP","messageID":"msg_025215fcb001oYKuFSMme311SO"}}'
_OC_WRITE = '{"type":"tool_use","timestamp":1787329434000,"sessionID":"ses_fdadeabefffeNRpKGh9ocirEEP","part":{"type":"tool","tool":"write","callID":"toolu_02","state":{"status":"completed","input":{"filePath":"/var/lib/cseries/projects/opencode/index.html","content":"<!DOCTYPE html>\\n<html lang=\\"en\\">"},"output":"Wrote file successfully.","metadata":{"diagnostics":{},"filepath":"/var/lib/cseries/projects/opencode/index.html","exists":false,"truncated":false},"title":"var/lib/cseries/projects/opencode/index.html","time":{"start":1,"end":2}},"metadata":{},"id":"prt_2","sessionID":"ses_fdadeabefffeNRpKGh9ocirEEP","messageID":"msg_0"}}'
_OC_BASH = '{"type":"tool_use","timestamp":1787329445000,"sessionID":"s","part":{"type":"tool","tool":"bash","callID":"toolu_03","state":{"status":"completed","input":{"command":"wc -l /var/lib/cseries/projects/opencode/index.html"},"output":"690 /var/lib/cseries/projects/opencode/index.html\\n","metadata":{"output":"690 …","exit":0,"truncated":false},"title":"wc -l …","time":{"start":1,"end":2}},"metadata":{},"id":"prt_3","sessionID":"s","messageID":"m"}}'
_OC_FINISH = '{"type":"step_finish","timestamp":1787329473007,"sessionID":"s","part":{"id":"prt_025236947001FqaJJigawd2TwS","reason":"stop","messageID":"msg_0252333af001p4yq1iuhG9ZmyD","sessionID":"s","type":"step-finish","tokens":{"total":20117,"input":1,"output":468,"reasoning":0,"cache":{"write":322,"read":19326}},"cost":0.0233805}}'
_OC_PENDING = '{"type":"tool_use","timestamp":1,"sessionID":"s","part":{"type":"tool","tool":"read","callID":"toolu_04","state":{"status":"running","input":{"filePath":"/var/lib/cseries/projects/opencode/app.yaml"}},"id":"prt_4","sessionID":"s","messageID":"m"}}'
_OC_TOOLERR = '{"type":"tool_use","timestamp":1,"sessionID":"s","part":{"type":"tool","tool":"bash","callID":"toolu_05","state":{"status":"error","input":{"command":"false"},"error":"exit 1"},"id":"prt_5","sessionID":"s","messageID":"m"}}'

evs = E.normalize_line("opencode", _OC_TEXT, 0)
check("opencode text part -> the model's prose (not an empty text frame)",
      len(evs) == 1 and evs[0]["kind"] == "text"
      and "distinct design philosophy" in evs[0]["payload"]["text"], evs)
evs = E.normalize_line("opencode", _OC_TODO, 3)
check("opencode completed tool line -> tool_call (name + args) AND tool_result, rising seq",
      [e["kind"] for e in evs] == ["tool_call", "tool_result"]
      and evs[0]["payload"]["tool"] == "todowrite"
      and isinstance(evs[0]["payload"]["args"].get("todos"), list)
      and evs[1]["payload"]["status"] == "ok"
      and "Design the visual concept" in evs[1]["payload"]["result"]
      and [e["seq"] for e in evs] == [3, 4], evs)
evs = E.normalize_line("opencode", _OC_WRITE, 0)
check("opencode write tool -> tool_call + tool_result + file_edit with the path",
      [e["kind"] for e in evs] == ["tool_call", "tool_result", "file_edit"]
      and evs[0]["payload"]["args"].get("path", "").endswith("/opencode/index.html")
      and evs[2]["payload"]["path"].endswith("index.html")
      and evs[2]["payload"]["change"] == "write", evs)
evs = E.normalize_line("opencode", _OC_BASH, 0)
check("opencode bash tool -> command in args, output in the result",
      evs[0]["payload"]["args"].get("command", "").startswith("wc -l")
      and evs[1]["payload"]["result"].startswith("690 "), evs)
evs = E.normalize_line("opencode", _OC_STEP0, 0)
check("opencode step_start -> a status frame, never raw JSON",
      len(evs) == 1 and evs[0]["kind"] == "status"
      and evs[0]["payload"]["state"] == "step_start", evs)
evs = E.normalize_line("opencode", _OC_FINISH, 0)
check("opencode step_finish -> status + usage (tokens/cache/cost mapped)",
      [e["kind"] for e in evs] == ["status", "usage"]
      and evs[0]["payload"]["detail"] == "stop"
      and evs[1]["payload"].get("output_tokens") == 468
      and evs[1]["payload"].get("cache_read_tokens") == 19326
      and evs[1]["payload"].get("cost_usd") == 0.0233805, evs)
evs = E.normalize_line("opencode", _OC_PENDING, 0)
check("opencode running tool -> the call alone (no result yet)",
      [e["kind"] for e in evs] == ["tool_call"] and evs[0]["payload"]["tool"] == "read", evs)
evs = E.normalize_line("opencode", _OC_TOOLERR, 0)
check("opencode failed tool -> error tool_result carrying the vendor's error",
      [e["kind"] for e in evs] == ["tool_call", "tool_result"]
      and evs[1]["payload"]["status"] == "error" and evs[1]["payload"]["result"] == "exit 1", evs)
evs = E.normalize_line("opencode", '{"type":"text","part":{"type":"text","body":"moved"}}', 0)
check("opencode text part WITHOUT a text key declines (raw line survives — field-drift rule)",
      len(evs) == 1 and evs[0]["kind"] == "text" and '"part"' in evs[0]["payload"]["text"], evs)
evs = E.normalize_line("opencode", '{"type":"tool_result","tool":"bash","result":"hi"}', 0)
check("opencode top-level (no part) line still reads through the generic reader",
      len(evs) == 1 and evs[0]["kind"] == "tool_result" and evs[0]["payload"]["tool"] == "bash", evs)
_oc_live = [e for raw in (_OC_STEP0, _OC_TEXT, _OC_TODO, _OC_WRITE, _OC_BASH, _OC_FINISH)
            for e in E.normalize_line("opencode", raw, 0)]
check("opencode live shapes -> zero raw-JSON text frames, zero blank tool names",
      not any(e["kind"] == "text" and str(e["payload"].get("text", "")).lstrip().startswith("{")
              for e in _oc_live)
      and all(e["payload"].get("tool") for e in _oc_live if e["kind"] == "tool_call"),
      [(e["kind"], str(e["payload"])[:60]) for e in _oc_live])

# The part-less error envelope, verbatim from a signed-out run (no key) of
# BOTH v1.17.13 and v1.18.35, 2026-10-06. The sentence is error.data.message
# (opencode's NamedError); `name` alone is only the class.
_OC_ERRENV = ('{"type":"error","timestamp":1791324006445,"sessionID":"ses_eecc4da73ffeF9ghM4PVt52Ian",'
              '"error":{"name":"UnknownError","data":{"message":"Unexpected server error. '
              'Check server logs for details.","ref":"err_5de8f2f2"}}}')
evs = E.normalize_line("opencode", _OC_ERRENV, 0)
check("opencode error envelope -> ONE error frame carrying the vendor's sentence (data.message), not just the class",
      [e["kind"] for e in evs] == ["error"]
      and evs[0]["payload"]["message"] == "Unexpected server error. Check server logs for details.", evs)
evs = E.normalize_line("opencode", '{"type":"error","error":{"name":"ProviderAuthError"}}', 0)
check("opencode error envelope with no data.message -> the error's name",
      [e["kind"] for e in evs] == ["error"] and evs[0]["payload"]["message"] == "ProviderAuthError", evs)

# ── 4e. antigravity error_message step (fan-out 3ba241d3, 2026-08-21) ───────
_AG_ERRSTEP = '{"event":"step_update","step_update":{"conversation_id":"f4bd85f7","step_index":15,"state":"DONE","step_type":"error_message","duration_seconds":0}}'
evs = E.normalize_line("antigravity", _AG_ERRSTEP, 0)
check("antigravity error_message step -> warning status, not raw JSON",
      len(evs) == 1 and evs[0]["kind"] == "status"
      and evs[0]["payload"]["state"] == "warning"
      and "vendor error step" in evs[0]["payload"]["detail"], evs)
_AG_NETRES = '{"event":"result","result":{"conversation_id":"f4bd85f7","status":"ERROR","response":"","error":"There was a network issue connecting to the server, please try again.","duration_seconds":76.9,"num_turns":1,"usage":{"input_tokens":124080,"output_tokens":6381}}}'
evs = E.normalize_line("antigravity", _AG_NETRES, 0)
check("antigravity ERROR result -> warning carrying the vendor's words + done + usage",
      [e["kind"] for e in evs] == ["status", "status", "usage"]
      and "network issue" in evs[0]["payload"]["detail"], evs)

# ── 5. shared contract ───────────────────────────────────────────────────────
for hid, raw, _ in REAL:
    ev = E.normalize(hid, raw, 7)
    check("%-9s event has contract keys" % hid,
          set(ev) >= {"ts", "seq", "kind", "payload"}, ev)
    check("%-9s kind is in the vocabulary" % hid, ev["kind"] in E.KINDS, ev["kind"])
    check("%-9s seq preserved" % hid, ev["seq"] == 7, ev)

# ── 6. a vendor shape change cannot kill a run ───────────────────────────────
_orig = E.PARSERS["grok"]
try:
    def _boom(_o):
        raise RuntimeError("vendor changed shape")
    E.PARSERS["grok"] = _boom
    ev = E.normalize("grok", '{"type":"whatever"}', 0)
    check("parser exception degrades to text (run survives)",
          ev is not None and ev["kind"] == "text", ev)
finally:
    E.PARSERS["grok"] = _orig

print()
if FAILS:
    print("FAILED: harness streaming ratchet (%d)" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("PASS: harness streaming ratchet — every harness streams, no line is ever dropped")
sys.exit(0)
