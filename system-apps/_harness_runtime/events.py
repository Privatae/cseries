"""events — normalize the vendor stream dialects into ONE event shape.

Every harness can already stream (verified 2026-07-26 by running each sidecar
image's binary):

  grok             --output-format streaming-json   {"type": ...}
  cursor           --output-format stream-json + --stream-partial-output
  codex            --json                           {"type": "thread.started"}
  opencode         --format json                    raw JSON events
  claude-code      SDK async generator (its own bin, not this runtime)

Without this module the Bench UI would need a renderer per vendor. With it
there is ONE contract:

    {"ts": float, "seq": int, "kind": str, "payload": dict}

    kind ∈ text | thinking | tool_call | tool_result | file_edit
           | usage | error | status

DESIGN RULE — never drop a line. codex interleaves plain-text log lines with
its JSONL on the same stream (observed live: `ERROR codex_models_manager...`
between `{"type":"thread.started"}` frames). A parser that ignored
unparseable lines would silently blank part of the transcript, which is the
exact failure class this whole change exists to remove. Anything we cannot
classify becomes kind="text" with the raw line as payload.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict

KINDS = ("text", "thinking", "tool_call", "tool_result", "file_edit",
         "usage", "error", "status")


def _ev(kind: str, **payload) -> dict:
    return {"kind": kind if kind in KINDS else "text", "payload": payload}


def _txt(v) -> str:
    """Vendors put prose in a string, a {text:...} block, or a list of blocks."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("text") or v.get("content") or "")
    if isinstance(v, list):
        return "".join(_txt(x) for x in v)
    return str(v)


# ── Claude-Code-family (grok, cursor) ────────────────────────────────────────
# The grok CLI and cursor-agent both mirror Claude Code's stream-json:
# a `type` discriminator with assistant/tool/result frames.
def parse_claude_family(o: dict) -> dict | None:
    t = str(o.get("type") or o.get("event") or "").lower()

    if t in ("error",):
        return _ev("error", message=_txt(o.get("message") or o.get("error")))
    if t in ("system", "thread.started", "session", "init"):
        return _ev("status", state=t, detail=_txt(o.get("message")))
    if t in ("result", "turn.completed", "done"):
        return _ev("status", state="done", detail=_txt(o.get("result")))

    # assistant / message frames carry content blocks
    msg = o.get("message") if isinstance(o.get("message"), dict) else o
    blocks = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(blocks, list):
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = str(b.get("type") or "").lower()
            if bt == "thinking":
                return _ev("thinking", text=_txt(b.get("thinking") or b.get("text")))
            if bt in ("tool_use", "tool_call"):
                return _ev("tool_call", tool=str(b.get("name") or ""),
                           args=b.get("input") or b.get("arguments") or {})
            if bt in ("tool_result",):
                return _ev("tool_result", tool=str(b.get("name") or ""),
                           status="error" if b.get("is_error") else "ok",
                           result=_txt(b.get("content")))
            if bt in ("text", "output_text"):
                return _ev("text", text=_txt(b.get("text")))

    # partial deltas (cursor --stream-partial-output). delta=True for the same
    # reason grok's chunks carry it: these are fragments of one message, and a
    # renderer has to be able to tell them from a whole-message frame.
    if t in ("text_delta", "content_block_delta", "delta"):
        d = o.get("delta") if isinstance(o.get("delta"), dict) else o
        return _ev("text", text=_txt(d.get("text")), delta=True)

    if o.get("usage"):
        return _ev("usage", **{k: v for k, v in dict(o["usage"]).items()
                               if isinstance(v, (int, float))})
    return None


# ── grok ─────────────────────────────────────────────────────────────────────
# `grok --output-format streaming-json` — vocabulary read from the PINNED
# binary's OWN embedded documentation (0.2.112, section `### streaming-json`).
# `text` and `thought` are additionally MEASURED: all 675 frames of a
# live run (2026-08-21) are those two and nothing else.
# `end`, `error`, max_turns_reached and auto_compact_* rest on the vendor doc
# ALONE — that capture has none of them, because the entity pod was rolled
# mid-run and the stream was cut, not because grok
# declined to send them. The first uninterrupted grok run is that check; until
# then the never-drop rule keeps anything misread here legible as a raw line.
#
#   {"type":"text","data":"Here's"}          a chunk of the agent's prose
#   {"type":"thought","data":"Analyzing…"}   internal reasoning (thinking tokens)
#   {"type":"end","stopReason":"EndTurn","sessionId":…,"requestId":…,
#    "usage":{…},"num_turns":7,"modelUsage":{…}}      always the LAST event
#   {"type":"error","message":…}             (+ spend fields when recorded)
#
# `text` and `thought` are TOKEN-LEVEL deltas: that one build emitted 555
# thought + 120 text frames, each carrying a word or two, and the vendor keeps
# any leading space INSIDE `data` — so consumers join the pieces with no
# separator (system-apps/bench/app.js groups adjacent same-kind frames).
#
# The vendor's own docs call the type list NON-EXHAUSTIVE ("Grok may also emit
# max_turns_reached and auto_compact_* events; treat the list as non-exhaustive
# and switch on `type`"), so those two are read by name and anything else
# returns None — it then tries the claude-family reader and, failing that,
# lands as a raw text line under the never-drop rule. Guessing a meaning for an
# unknown frame is worse than showing it verbatim.
#
# WHY THIS EXISTS: until 2026-08-21 grok read through parse_claude_family, on
# the design doc's claim that "the grok CLI mirrors Claude Code's stream-json"
# — measured against 0.2.51. 0.2.112 does not. Every one of that run's
# 675 lines declined, hit the never-drop fallback, and reached the Bench as a
# raw JSON line: a live feed of `{"type":"thought","data":"The"}` instead of
# the model's reasoning. The claude-family reader stays as grok's FALLBACK
# (PARSERS["grok"]) so a build that does speak that dialect still reads.
#
# 1.0.46 (pinned 2026-10-06; xAI answers 426 Upgrade Required to any CLI
# below 1.0.13) keeps every type above and ADDS five, read off that binary's
# own `### streaming-json` section — no captured 1.0.x run yet, so these rest
# on the vendor doc alone, exactly as `end` did at 0.2.112:
#
#   {"type":"tool_call","toolCallId":"call_1","title":"Read","kind":"read",
#    "status":"in_progress","toolName":"read_file","rawInput":{"path":…},
#    "content":[],"locations":[]}
#   {"type":"tool_call_update","toolCallId":"call_1","status":"completed",
#    "content":[],"rawOutput":{…},"locations":[]}
#   {"type":"usage","messageId":…,"stopReason":"end_turn","usage":{…}}
#   {"type":"plan","entries":[…]}      {"type":"available_commands",…}
#
# Leaf names follow ACP (the Agent Client Protocol): `kind` is the tool's
# category (read, edit, delete, move, search, execute, think, fetch, other),
# `content` may hold {"type":"diff","path","oldText","newText"} blocks, and an
# update need not repeat the call's name — so the call is remembered by
# toolCallId in the run's parse state and its result names it.
_GROK_STATE_KEY = "grok_calls"
_GROK_USAGE_KEY = "grok_usage"
_GROK_PATH_KEYS = ("path", "file_path", "filePath", "target_file", "file")
_GROK_CMD_KEYS = ("command", "cmd")


def _grok_args(o: dict) -> dict:
    """The vendor's rawInput plus the renderer's keys (path/command —
    workbench-web/src/lib/run-frames.ts toolCallLines), vendor keys untouched."""
    raw = o.get("rawInput")
    out = dict(raw) if isinstance(raw, dict) else {}
    if "path" not in out:
        for k in _GROK_PATH_KEYS:
            if isinstance(out.get(k), str) and out[k]:
                out["path"] = out[k]
                break
    if "path" not in out:
        locs = o.get("locations")
        if isinstance(locs, list) and locs and isinstance(locs[0], dict) and locs[0].get("path"):
            out["path"] = str(locs[0]["path"])
    if "command" not in out:
        for k in _GROK_CMD_KEYS:
            if isinstance(out.get(k), str) and out[k]:
                out["command"] = out[k]
                break
    return out


def _grok_result_text(o: dict) -> str:
    """A tool result's body: its text content blocks, else rawOutput."""
    parts = []
    for b in o.get("content") or []:
        if not isinstance(b, dict):
            continue
        inner = b.get("content") if isinstance(b.get("content"), dict) else b
        if str(inner.get("type") or "").lower() == "text":
            parts.append(_txt(inner.get("text")))
    if parts:
        return "".join(parts)
    ro = o.get("rawOutput")
    if ro is None:
        return ""
    if isinstance(ro, str):
        return ro
    try:
        return json.dumps(ro, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(ro)


def _grok_tool_events(o: dict, t: str, state: dict | None) -> list[dict] | None:
    calls = state.setdefault(_GROK_STATE_KEY, {}) if isinstance(state, dict) else {}
    cid = str(o.get("toolCallId") or "")
    known = calls.get(cid) or {}
    # The remembered call name outranks an update's `title`: ACP lets an
    # update retitle the step ("Read a.txt (3 lines)"), which is not its name.
    name = str(o.get("toolName") or known.get("tool") or o.get("title")
               or o.get("kind") or "tool")
    kind = str(o.get("kind") or known.get("kind") or "").lower()
    status = str(o.get("status") or "").lower()
    args = _grok_args(o)
    out: list[dict] = []
    if t == "tool_call":
        calls[cid] = {"tool": name, "kind": kind, "path": args.get("path")}
        out.append(_ev("tool_call", tool=name, args=args, id=cid))
    elif status not in ("completed", "failed"):
        # A mid-step update. MEASURED on one grok 1.0.46 run:
        # 138 of these, every one {"status": null | "in_progress",
        # "content": [], "rawOutput": null, "locations": [...]} — nothing for
        # the step to show. Declining them printed 138 raw JSON rows into the
        # feed. A lifecycle status ("tool_update", skipped by run-steps.ts
        # LIFECYCLE and bench NOISE) keeps the line in Raw without a row; the
        # completed/failed update below still closes the call.
        return [_ev("status", state="tool_update", detail=status or "pending",
                    id=cid)]
    if status in ("completed", "failed"):
        failed = status == "failed"
        body = _grok_result_text(o) or (status if failed else "")
        out.append(_ev("tool_result", tool=name,
                       status="error" if failed else "ok", result=body, id=cid))
        if not failed:
            # No `tool` on these: they follow the result, and both views read a
            # file_edit WITH a tool as a new edit call left open ("run") that
            # nothing closes. Without one they fold it onto the call's own row
            # (bench/app.js and run-steps.ts `file_edit`, antigravity's shape).
            diffs = [b for b in (o.get("content") or [])
                     if isinstance(b, dict) and str(b.get("type") or "").lower() == "diff"
                     and b.get("path")]
            for b in diffs:
                out.append(_ev("file_edit", path=str(b["path"]), id=cid,
                               change="write" if not b.get("oldText") else "edit"))
            path = args.get("path") or known.get("path")
            if not diffs and kind == "edit" and path:
                out.append(_ev("file_edit", path=str(path), id=cid, change="edit"))
        calls.pop(cid, None)
    return out


def _grok_usage(o: dict) -> dict | None:
    """The numbers of an `end` / `error` frame's usage, plus the run's dollars.

    grok reports its spend BESIDE the usage object, not inside it — a run's
    `end` frame: "usage": {...token counts...}, "total_cost_usd": 1.97625204,
    "modelUsage": {...}. Copying the usage object alone dropped the cost, so
    that run's harness_usage_event row said cost 0 while the stream said
    $1.98. compute._USAGE_COST_KEYS reads total_cost_usd."""
    u = o.get("usage")
    if not isinstance(u, dict):
        return None
    nums = {k: v for k, v in u.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}
    cost = o.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        nums["total_cost_usd"] = cost
    return nums


def parse_grok(o: dict, state: dict | None = None) -> list[dict] | None:
    t = str(o.get("type") or "").lower()
    if t in ("text", "thought"):
        # `data` is the field 0.2.112 uses, and the only one seen in the
        # capture. Read `text` as an alternate rather than claiming the line
        # with an empty string: field-name drift is exactly the class of change
        # that produced this bug (0.2.51 -> 0.2.112), and a claimed-but-empty
        # frame would render as NOTHING — strictly worse than the raw line this
        # replaces. With neither key we decline, so the claude-family reader
        # and then the never-drop rule still get their turn.
        if "data" not in o and "text" not in o:
            return None
        body = _txt(o.get("data") if "data" in o else o.get("text"))
        # delta=True: token-level chunk. Renderers join adjacent deltas into
        # one block (system-apps/bench/app.js); a whole-message frame from
        # another vendor must never be glued to its neighbour.
        return [_ev("thinking" if t == "thought" else "text",
                    text=body, delta=True)]
    if t == "error":
        out = [_ev("error", message=_txt(o.get("message") or o.get("error")))]
        u = _grok_usage(o)
        if u is not None:
            out.append(_ev("usage", **u))
        return out
    if t == "end":
        out = [_ev("status", state="done", detail=_txt(o.get("stopReason")))]
        u = _grok_usage(o)
        if u is not None:
            out.append(_ev("usage", **u))
        return out
    if t == "max_turns_reached" or t.startswith("auto_compact"):
        return [_ev("status", state=t, detail=_txt(o.get("message")))]
    if t in ("tool_call", "tool_call_update"):
        return _grok_tool_events(o, t, state)
    if t == "usage":
        # One per model response. grok's usage is aggregated "last"
        # (server/routes/compute.py _USAGE_AGG) and `end`, always the last
        # event, carries the run's totals — so these never double the count.
        # A run cut before `end` (a watchdog stop, a timeout) has no totals
        # line, and "last" would then take ONE response's tokens as the run's.
        # So with parse state each frame carries the running total instead.
        u = o.get("usage")
        if isinstance(u, dict):
            nums = {k: v for k, v in u.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)}
            if isinstance(state, dict):
                run = state.setdefault(_GROK_USAGE_KEY, {})
                for k, v in nums.items():
                    run[k] = run.get(k, 0) + v
                nums = dict(run)
            return [_ev("usage", **nums)]
        return None
    if t == "plan":
        # The same shape codex's todo_list takes, so both views print it alike.
        entries = [e for e in (o.get("entries") or []) if isinstance(e, dict)]
        marks = " · ".join(("☑ " if str(e.get("status") or "").lower() == "completed" else "☐ ")
                           + _txt(e.get("content")) for e in entries)
        return [_ev("status", state="todo_list", detail=marks)]
    if t == "available_commands":
        # Session-start inventory. "init" is a lifecycle state both views
        # already skip (run-steps.ts LIFECYCLE, bench/app.js NOISE).
        tools = o.get("tools") if isinstance(o.get("tools"), list) else []
        cmds = o.get("commands") if isinstance(o.get("commands"), list) else []
        return [_ev("status", state="init",
                    detail=f"{len(tools)} tools, {len(cmds)} commands")]
    return None


# ── codex ────────────────────────────────────────────────────────────────────
# `codex exec --json` — schema read from the binary's source
# (rust-v0.149.0, codex-rs/exec/src/exec_events.rs) and checked line-for-line
# against a live run transcript (2026-08-21).
# RE-CHECKED at the rust-v0.154.0 pin (2026-09-17) from inside the built image,
# unauthenticated: `--json` emits `thread.started` (with thread_id),
# `turn.started`, `error` (with message) and `item.completed` (item:{id,type,…})
# — the shapes below, unchanged. RE-CHECKED at rust-v0.160.1 (2026-10-06) the
# same way, same frames; in source exec_events.rs gained only an optional
# `results` list on web_search items (read as before: the call, then an ok
# result). An unauthenticated run cannot reach
# `turn.completed`, so its `usage` FIELD NAMES (input_tokens, cached_input_tokens,
# cache_write_input_tokens, output_tokens, reasoning_output_tokens) are the only
# part still resting on the 0.149.0 reading; `item.started`/`item.updated` were
# likewise not observed, though `item.completed` carrying the same item shape is
# good evidence for them. If token accounting reads zero after the 0.154.0 image
# ships, start here:
#
#   {"type":"thread.started","thread_id":…}          {"type":"turn.started"}
#   {"type":"turn.completed","usage":{input_tokens, cached_input_tokens,
#                 cache_write_input_tokens, output_tokens, reasoning_output_tokens}}
#   {"type":"turn.failed","error":{"message":…}}     {"type":"error","message":…}
#   {"type":"item.started|item.updated|item.completed",
#    "item":{"id":…,"type":<item type>, …}}
#
#   item type          fields                                      arrives as
#   agent_message      text                                        completed
#   reasoning          text                                        completed
#   command_execution  command, aggregated_output, exit_code,      started → completed
#                      status (in_progress|completed|failed|declined)
#   file_change        changes:[{path,kind}], status               started → completed
#   mcp_tool_call      server, tool, arguments, result, error,     started → completed
#                      status
#   web_search         query                                       started → completed
#   collab_tool_call   tool, sender_thread_id, receiver_thread_ids, started → completed
#                      prompt, agents_states, status
#   todo_list          items:[{text,completed}]                    started/updated/completed
#   error              message  — NON-FATAL by the schema's own    completed
#                      words: the CLI emits it for config warnings,
#                      deprecation notices and model reroutes while
#                      the run carries on (turn.failed / top-level
#                      error are the fatal ones)
#
# Until 2026-08-21 a `.started/.completed` SUFFIX test ran before the item
# branch, so every item.* line collapsed into a bare {"state":"item.completed"}
# status frame: one run's Workbench feed was 19 lines of
# "· item.completed" while the raw transcript held the prose, six shell
# commands with their output and a two-file patch. The suffix test is gone;
# thread/turn frames are matched by exact name and items by their own type.
# One line maps to ONE OR MORE events (a patch is one file_edit per path,
# turn.completed is a status plus a usage), so codex registers in
# MULTI_PARSERS like kimi. Unknown item types / phases return None so the
# raw line lands as text (never-drop).
_CODEX_SHELL = "shell"


def _codex_item_events(phase: str, item: dict) -> list[dict] | None:
    it = str(item.get("type") or "").lower()
    iid = str(item.get("id") or "")
    status = str(item.get("status") or "").lower()
    if it == "agent_message":
        return [_ev("text", text=_txt(item.get("text")))]
    if it == "reasoning":
        return [_ev("thinking", text=_txt(item.get("text")))]
    if it == "error":
        # Non-fatal (see the table): a status frame, not kind=error — the run
        # block paints an error frame's message as the run's failure line
        # even under a clean exit (run-frames.ts foldSummary), which would
        # turn "model rerouted a -> b" into a red strip on a successful build.
        return [_ev("status", state="warning", detail=_txt(item.get("message")),
                    id=iid)]
    if it == "todo_list":
        todos = [x for x in (item.get("items") or []) if isinstance(x, dict)]
        marks = " · ".join(("☑ " if x.get("completed") else "☐ ")
                           + str(x.get("text") or "") for x in todos)
        return [_ev("status", state="todo_list", detail=marks)]
    if it == "command_execution":
        cmd = str(item.get("command") or "")
        if phase == "started":
            return [_ev("tool_call", tool=_CODEX_SHELL,
                        args={"command": cmd}, id=iid)]
        if phase == "completed":
            rc = item.get("exit_code")
            ok = status in ("completed", "") and (rc is None or rc == 0)
            out = _txt(item.get("aggregated_output"))
            if not ok and not out.strip():
                out = (status or "failed") + (f" (exit {rc})" if rc is not None else "")
            return [_ev("tool_result", tool=_CODEX_SHELL,
                        status="ok" if ok else "error",
                        result=out, exit_code=rc, id=iid)]
        return None
    if it == "file_change":
        changes = [c for c in (item.get("changes") or []) if isinstance(c, dict)]
        paths = [str(c.get("path") or "") for c in changes]
        if phase == "started":
            # description is what the run block prints after the tool name
            # ("→ apply_patch — app.yaml, index.html"); paths stay verbatim.
            return [_ev("tool_call", tool="apply_patch",
                        args={"description": ", ".join(p.rsplit("/", 1)[-1] for p in paths),
                              "paths": paths}, id=iid)]
        if phase == "completed":
            if status == "failed":
                # ends_tool: this error frame CLOSES the apply_patch tool_call
                # the started phase opened — stated outright rather than
                # inferred from `id`, which is empty when the vendor omits it
                # The watchdog reads it.
                return [_ev("error", message="patch failed: " + ", ".join(paths),
                            id=iid, ends_tool=True)]
            # codex calls the add/update/delete flag "kind"; that word is the
            # frame's own discriminator (_ev's first argument), so it rides
            # the payload as "change".
            return [_ev("file_edit", path=p, change=str(c.get("kind") or ""),
                        id=iid) for p, c in zip(paths, changes)] or None
        return None
    if it == "mcp_tool_call":
        name = ".".join(x for x in (str(item.get("server") or ""),
                                    str(item.get("tool") or "")) if x) or "mcp_tool"
        if phase == "started":
            args = item.get("arguments")
            return [_ev("tool_call", tool=name,
                        args=args if isinstance(args, dict) else {"arguments": args},
                        id=iid)]
        if phase == "completed":
            err = item.get("error")
            if isinstance(err, dict):
                err = err.get("message")
            res = item.get("result")
            if isinstance(res, dict):
                body = _txt(res.get("content"))
                if not body and res.get("structured_content") is not None:
                    body = json.dumps(res.get("structured_content"))
            else:
                body = _txt(res)
            is_err = bool(err) or status == "failed"
            return [_ev("tool_result", tool=name,
                        status="error" if is_err else "ok",
                        result=str(err) if (is_err and err) else body, id=iid)]
        return None
    if it == "web_search":
        if phase == "started":
            return [_ev("tool_call", tool="web_search",
                        args={"query": str(item.get("query") or "")}, id=iid)]
        if phase == "completed":
            return [_ev("tool_result", tool="web_search", status="ok",
                        result="", id=iid)]
        return None
    if it == "collab_tool_call":
        name = "collab." + (str(item.get("tool") or "") or "tool")
        if phase == "started":
            return [_ev("tool_call", tool=name,
                        args={"prompt": _txt(item.get("prompt")),
                              "receivers": item.get("receiver_thread_ids") or []},
                        id=iid)]
        if phase == "completed":
            states = item.get("agents_states")
            return [_ev("tool_result", tool=name,
                        status="error" if status == "failed" else "ok",
                        result=json.dumps(states) if states else "", id=iid)]
        return None
    return None


def parse_codex_events(o: dict) -> list[dict] | None:
    """One codex JSONL object -> events (MULTI_PARSERS entry). None — never
    [] — when nothing is recognized, so normalize_line's raw-line fallback
    keeps the never-drop rule."""
    t = str(o.get("type") or "").lower()
    if t == "thread.started":
        return [_ev("status", state=t, detail=str(o.get("thread_id") or ""))]
    if t == "turn.started":
        return [_ev("status", state=t, detail="")]
    if t == "turn.completed":
        out = [_ev("status", state=t, detail="")]
        u = o.get("usage")
        if isinstance(u, dict):
            out.append(_ev("usage", **{k: v for k, v in u.items()
                                       if isinstance(v, (int, float))}))
        return out
    if t == "turn.failed":
        err = o.get("error")
        msg = _txt(err.get("message") if isinstance(err, dict) else err)
        return [_ev("error", message=msg or "turn failed")]
    if t == "error":
        return [_ev("error", message=_txt(o.get("message") or o.get("error")))]
    if t.startswith("item.") and isinstance(o.get("item"), dict):
        return _codex_item_events(t[len("item."):], o["item"])
    return None


def parse_codex(o: dict) -> dict | None:
    """Single-event view (the PARSERS fallback + the normalize() shim): the
    first event of parse_codex_events, else the claude-family reader for a
    line that is none of the shapes above."""
    evs = parse_codex_events(o)
    if evs:
        return evs[0]
    return parse_claude_family(o)


# ── kimi >= 0.36 — role-based JSONL (OpenAI chat-message shape) ──────────────
# Observed LIVE on the 0.36.1 binary (2026-08-20). The
# 0.32.0 writer spoke the Claude event vocabulary (parse_claude_family still
# reads it — role-less lines fall through to it), but 0.36.x streams chat
# messages:
#   {"role":"meta","type":"system.version","version":"0.36.1"}
#   {"role":"assistant","content":"...","tool_calls":[{"function":
#       {"name":"Read","arguments":"{\"path\":\"index.html\"}"}}]}
#   {"role":"tool","tool_call_id":"...","content":"<result body>"}
#   {"role":"meta","type":"session.resume_hint","command":"kimi -r ..."}
# One line can carry BOTH assistant prose and several tool calls, so this
# parser returns a LIST of events (normalize_line assigns rising seqs). It
# returns None — never [] — when it recognizes nothing, so the raw-line
# fallback keeps the never-drop rule.
#
# TOOL-NAME CORRELATION (2026-08-22). The `role:"tool"` line carries the result
# and NOTHING that names the tool — verified against a raw run
# transcript, where every such line's keys are exactly
# {content, role, tool_call_id}. The name lives on the assistant line that
# opened the call, as tool_calls[].function.name beside tool_calls[].id, and
# that id is what tool_call_id refers back to (e.g. id
# "tool_abc123" -> name "Bash").
#
# Reading o.get("name") therefore produced "" on EVERY result frame — 38 of 38
# on one measured run — so the run terminal could not label or pair a single
# tool result, and the live feed was unreadable.
#
# Correlation needs cross-line memory, so the caller owns a `state` dict (see
# normalize_line). A "last tool_call seen" heuristic is NOT enough: one
# assistant line in one run carried FOUR tool_calls, so three of the four
# results would have been mislabelled with the fourth's name — worse than
# blank, because a wrong name reads as truth.
_KIMI_TOOL_NAMES = "kimi_tool_names"
# Bounded so a long run cannot grow this without limit; far above the number of
# calls that can be outstanding at once (4 in the measured worst case).
_KIMI_TOOL_NAME_CAP = 512


def parse_kimi_roles(o: dict, state: dict | None = None) -> list[dict] | None:
    role = str(o.get("role") or "").lower()
    if not role:
        return None
    names = state.setdefault(_KIMI_TOOL_NAMES, OrderedDict()) \
        if state is not None else None
    out: list[dict] = []
    if role == "meta":
        u = o.get("usage")
        if isinstance(u, dict):
            out.append(_ev("usage", **{k: v for k, v in u.items()
                                       if isinstance(v, (int, float))}))
        t = str(o.get("type") or "meta")
        detail = _txt(o.get("version") or o.get("command") or o.get("content"))
        out.append(_ev("status", state=t, detail=detail))
        return out
    if role == "assistant":
        txt = _txt(o.get("content"))
        if txt:
            out.append(_ev("text", text=txt))
        for tc in (o.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "")
            # Remember it for the `role:"tool"` line that will carry only this
            # call's id (see the block comment above).
            call_id = str(tc.get("id") or tc.get("tool_call_id") or "")
            if names is not None and call_id and name:
                names[call_id] = name
                names.move_to_end(call_id)
                while len(names) > _KIMI_TOOL_NAME_CAP:
                    names.popitem(last=False)
            raw_args = fn.get("arguments") or tc.get("arguments") or {}
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (ValueError, TypeError):
                    args = {"raw": raw_args}
            else:
                args = raw_args if isinstance(raw_args, dict) else {}
            # A write-shaped call IS the file edit — surface it as one so the
            # run block's files fold counts it (payload mirrors parse_codex).
            if (name.lower() in ("write", "edit", "multiedit", "write_file",
                                 "create_file", "notebookedit")
                    and str(args.get("path") or "")):
                out.append(_ev("file_edit", path=str(args.get("path")),
                               tool=name, args=args))
            else:
                out.append(_ev("tool_call", tool=name, args=args))
        return out or None
    if role == "tool":
        is_err = bool(o.get("is_error") or o.get("error"))
        call_id = str(o.get("tool_call_id") or o.get("id") or "")
        # Resolution order: the id this result names → a `name` field if a
        # future kimi adds one → "tool". NEVER "": an unlabelled result is
        # unreadable, and a wrong label is worse than a generic one, so there is
        # no guess-from-the-last-call step here.
        tool = ""
        if names is not None and call_id:
            tool = names.pop(call_id, "")
        if not tool:
            tool = str(o.get("name") or "") or "tool"
        return [_ev("tool_result", tool=tool,
                    status="error" if is_err else "ok",
                    result=_txt(o.get("content")))]
    if role in ("user", "system"):
        txt = _txt(o.get("content"))
        return [_ev("text", text=txt)] if txt else None
    return None


# ── antigravity ──────────────────────────────────────────────────────────────
# `agy -p … --output-format stream-json` (Google's Antigravity CLI): one NDJSON
# object per line keyed by `event`. Schema READ OFF A LIVE RUN — agy 1.1.17, 2026-08-21 (39 vendor lines, which the old
# parser turned into 53 persisted frames) — not off the docs (the first cut
# was, and it rendered 15 raw-JSON lines, every tool call twice, and a red
# failure strip on a build that succeeded):
#
#   {"event":"init","conversation_id":…,"init":{"cwd":…,"tools":[…],
#                                               "permission_mode":"always-proceed"}}
#   {"event":"step_update","step_update":{"conversation_id":…,"step_index":N,
#    "state":"ACTIVE"|"DONE"|"ERROR","step_type":<type>, …}}
#     step_type      state   extra fields                             arrives as
#     user_input     DONE    —                                        once
#     checkpoint     DONE    duration_seconds                         once
#     agent_response ACTIVE  text_delta (the model's prose, chunked)  per chunk
#     agent_response DONE    duration_seconds, usage{…}, text_delta?  per turn
#     tool           ACTIVE  tool_name, tool_info{name, parameters}   call
#     tool           DONE    + duration_seconds, tool_info.output     result
#     tool           ERROR   + tool_info.error{type, message}         result
#   {"event":"result","result":{"conversation_id":…,"status":"SUCCESS"|"ERROR",
#    "response":…,"error"?:…,"duration_seconds":…,"num_turns":…,"usage":{…}}}
#
# Tool parameter names are the vendor's CamelCase — run_command{CommandLine},
# view_file{AbsolutePath,StartLine,EndLine}, list_dir{DirectoryPath},
# find_by_name{SearchDirectory,Pattern}, grep_search{Query,SearchPath},
# write_to_file{TargetFile,CodeContent,…}, replace_file_content /
# multi_replace_file_content{TargetFile,ReplacementChunks,…},
# read_url_content{Url}, search_web{query} — so _agy_args adds the renderer's
# keys (command / path / query / pattern / url / content / diff:
# workbench-web/src/lib/run-frames.ts toolCallLines) beside the originals.
#
# Mapping (every line maps or falls through as raw text, never dropped):
#   tool ACTIVE            → tool_call (the call printed ONCE)
#   tool DONE              → tool_result ok (+ file_edit for the write tools)
#   tool ERROR             → tool_result error carrying tool_info.error.message
#   agent_response ACTIVE/DONE with text_delta → text; DONE also → usage
#   user_input / checkpoint → a one-line status (state = the step type)
#   init                   → status init (cwd); result → status done + usage;
#                            a result with status != SUCCESS → status WARNING
#                            (not kind=error: the run block paints an error
#                            frame as the run's failure line even on a clean
#                            exit, and agy reports ERROR for a run in which one
#                            tool step failed while the work landed — exactly
#                            the live run. The process exit code is the
#                            verdict, via the runtime's terminal frame.)
# The --output-format json envelope (non-stream: {"status":"ERROR","error":…},
# measured on 1.1.17) reads through the same result branch.
_AGY_WRITE_TOOLS = frozenset({"write_to_file", "replace_file_content",
                              "multi_replace_file_content", "sed_file",
                              "notebook_edit"})
# vendor parameter → the renderer's key (first match wins per key)
_AGY_PATH_KEYS = ("TargetFile", "AbsolutePath", "DirectoryPath",
                  "SearchDirectory", "SearchPath", "Path", "FilePath")
_AGY_ARG_ALIASES = (
    ("command", ("CommandLine", "Command", "command")),
    ("query",   ("Query", "query", "SearchQuery")),
    ("pattern", ("Pattern", "pattern", "Regex")),
    ("url",     ("Url", "URL", "url")),
    ("content", ("CodeContent", "Content", "content", "Text")),
    ("prompt",  ("Prompt", "prompt", "Query")),
)


def _agy_args(tool: str, params) -> dict:
    """The vendor's parameters plus the keys the run terminal renders from
    (it prints `command` verbatim under a `$`, `path` in the header, sizes
    `content`/`diff`, else a one-line JSON of the rest). Every vendor key is
    kept untouched and a synthesized alias never overwrites one, so nothing
    the model sent is lost — the raw toggle shows the vendor's parameters
    alongside these aliases (it stringifies the NORMALIZED frame, not the
    vendor's line)."""
    if not isinstance(params, dict):
        return {}
    out = dict(params)
    for k in _AGY_PATH_KEYS:
        v = params.get(k)
        if isinstance(v, str) and v and "path" not in out:
            out["path"] = v
            break
    for alias, keys in _AGY_ARG_ALIASES:
        if alias in out:
            continue
        if alias == "prompt" and tool != "search_web":
            continue   # Query means a search, not a prompt, everywhere else
        for k in keys:
            v = params.get(k)
            if isinstance(v, str) and v:
                out[alias] = v
                break
    chunks = params.get("ReplacementChunks")
    if chunks is not None and "diff" not in out:
        try:
            out["diff"] = json.dumps(chunks)
        except (TypeError, ValueError):
            out["diff"] = str(chunks)
    return out


def _agy_error_text(err) -> str:
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err.get("type") or "")
    return _txt(err)


def parse_antigravity(o: dict) -> list[dict] | None:
    ev = str(o.get("event") or "").lower()
    if ev == "init":
        init = o.get("init") if isinstance(o.get("init"), dict) else {}
        return [_ev("status", state="init", detail=str(init.get("cwd") or ""))]
    if ev == "step_update":
        su = o.get("step_update") if isinstance(o.get("step_update"), dict) else o
        stype = str(su.get("step_type") or "").lower()
        state = str(su.get("state") or "").upper()
        dur = su.get("duration_seconds")
        if stype == "tool":
            info = su.get("tool_info") if isinstance(su.get("tool_info"), dict) else {}
            name = str(su.get("tool_name") or info.get("name") or "")
            args = _agy_args(name, info.get("parameters"))
            if state == "ACTIVE":
                return [_ev("tool_call", tool=name, args=args)]
            if state in ("DONE", "ERROR", "FAILED", "CANCELED", "CANCELLED"):
                err = info.get("error")
                failed = state != "DONE" or bool(err)
                body = _agy_error_text(err) if failed else _txt(info.get("output"))
                if failed and not body:
                    body = state.lower()
                out = [_ev("tool_result", tool=name,
                           status="error" if failed else "ok", result=body)]
                path = args.get("path")
                if not failed and name in _AGY_WRITE_TOOLS and path:
                    out.append(_ev("file_edit", path=str(path),
                                   change="write" if name == "write_to_file" else "edit"))
                return out
            # An unknown tool state: show the call rather than hide it.
            return [_ev("tool_call", tool=name, args=args)]
        if stype == "agent_response":
            out: list[dict] = []
            delta = _txt(su.get("text_delta") or su.get("text") or su.get("delta"))
            if delta:
                # TOKEN-LEVEL chunk, exactly like grok's `data` and cursor's
                # partial output: MEASURED in the capture, step_index 24 arrives
                # ACTIVE with 109 chars ending "…declaring id, title, " and its
                # DONE frame CONTINUES "entrypoint, zero required permissions…".
                # delta=True is what lets a renderer join adjacent chunks into
                # one block instead of printing a line per fragment.
                out.append(_ev("text", text=delta, delta=True))
            u = su.get("usage")
            if state == "DONE" and isinstance(u, dict):
                usage = {k: v for k, v in u.items() if isinstance(v, (int, float))}
                if isinstance(dur, (int, float)):
                    usage["duration_seconds"] = dur
                out.append(_ev("usage", **usage))
            return out or None
        if stype in ("thinking", "reasoning", "thought"):
            txt = _txt(su.get("text_delta") or su.get("text") or su.get("delta"))
            # Same chunking contract as the prose above (this step type was not
            # in the capture — the run's thinking tokens were counted in usage,
            # never streamed — so it is read by name and marked the same way).
            return [_ev("thinking", text=txt, delta=True)] if txt else None
        if stype in ("user_input", "checkpoint"):
            detail = "prompt received" if stype == "user_input" else ""
            if isinstance(dur, (int, float)):
                detail = (detail + " " if detail else "") + f"{dur:.1f}s"
            return [_ev("status", state=stype, detail=detail.strip())]
        if stype == "error_message":
            # MEASURED on a live fan-out (agy 1.1.17, 2026-08-21):
            # three of these arrived 15 s apart while the vendor retried a
            # model call, each as {"step_type":"error_message","state":"DONE"}
            # with NO text — the words ("There was a network issue connecting
            # to the server, please try again.") came only on the final result
            # frame. Name the step instead of leaking the raw line; a warning,
            # not kind=error, for the same reason the result branch below is:
            # the run's verdict is the exit code.
            txt = _txt(su.get("text_delta") or su.get("text") or su.get("delta")
                       or su.get("content") or su.get("message") or su.get("error"))
            return [_ev("status", state="warning",
                        detail=("vendor error step: " + txt) if txt
                        else "vendor error step (no text — the reason arrives on the result)")]
        txt = _txt(su.get("text_delta") or su.get("text") or su.get("delta")
                   or su.get("content") or su.get("message"))
        if txt:
            return [_ev("text", text=txt)]
        return None
    if ev == "result" or (not ev and "status" in o and "usage" in o):
        r = o.get("result") if isinstance(o.get("result"), dict) else o
        status = str(r.get("status") or "").upper()
        out = []
        if status and status != "SUCCESS":
            out.append(_ev("status", state="warning",
                           detail=f"{status.lower()}: {_agy_error_text(r.get('error')) or 'no detail'}"))
        out.append(_ev("status", state="done", detail=_txt(r.get("response"))))
        u = r.get("usage")
        if isinstance(u, dict):
            out.append(_ev("usage", **{k: v for k, v in u.items()
                                       if isinstance(v, (int, float))}))
        return out
    if ev == "error" or o.get("error"):
        return [_ev("error", message=_agy_error_text(o.get("message") or o.get("error")))]
    return None


# ── opencode >= 1.17 — `opencode run --format json` ──────────────────────────
# MEASURED on v1.17.13 (the pin then, sidecars/harness-opencode/Dockerfile)
# from a live fan-out (2026-08-21, 37 vendor lines).
# v1.18.35 (pinned 2026-10-06, source at the tag): the --format json writer
# emits the same types (cli/cmd/run.ts:724-790, tool_use only at
# completed|error) and the part schema (packages/schema/src/v1/session.ts) is
# byte-identical to v1.17.13. Not re-measured with a real (keyed) run. Every line is
# {"type": <event>, "timestamp", "sessionID", "part": {...}} — the payload is
# INSIDE `part`, keyed by part.type:
#
#   type          part.type     part fields                         arrives as
#   step_start    step-start    id, messageID                       once per model call
#   text          text          text, time{start,end}               ONE whole message
#   tool_use      tool          tool, callID, state{status, input,  ONE line per call,
#                               output, title, metadata, time}      already COMPLETED
#   step_finish   step-finish   reason (tool-calls|stop), tokens    once per model call
#                               {total,input,output,reasoning,
#                                cache{write,read}}, cost
#
# Until 2026-08-21 opencode read through parse_generic, which looks at the TOP
# level: `tool_use` became a tool_call with tool="" and args={} (CLAIMED, so
# the never-drop fallback never fired and the call rendered as "→ "), `text`
# became text="" (the model's prose VANISHED — measured: 5 text lines, 0
# characters shown), and step_start/step_finish fell to the raw line. The Bench
# feed of a SUCCESSFUL build was 22 raw-JSON lines and 10 blank tool calls.
#
# Tool names + input keys, read off the capture: write{filePath,content},
# edit{filePath,oldString,newString}, read{filePath}, bash{command},
# todowrite{todos}, glob/grep/webfetch by name. A tool line carries the call
# AND its result, so one line maps to tool_call + tool_result (+ file_edit for
# the write-shaped tools). state.status other than completed/error (pending,
# running) maps to the call alone. `reasoning`/`thinking` parts are read BY
# NAME only (the capture has none — the run did not stream thinking).
_OC_WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "patch", "apply_patch"})


def _oc_args(tool: str, inp) -> dict:
    """The vendor's input plus the renderer's keys (path/command/content —
    workbench-web/src/lib/run-frames.ts toolCallLines), vendor keys untouched."""
    if not isinstance(inp, dict):
        return {}
    out = dict(inp)
    fp = inp.get("filePath") or inp.get("path") or inp.get("file_path")
    if isinstance(fp, str) and fp and "path" not in out:
        out["path"] = fp
    return out


def parse_opencode(o: dict) -> list[dict] | None:
    t = str(o.get("type") or "").lower()
    part = o.get("part") if isinstance(o.get("part"), dict) else None
    if part is None:
        if t == "error":
            # The part-less error envelope. MEASURED on v1.17.13 and v1.18.35
            # (a signed-out run with no key, 2026-10-06):
            #   {"type":"error","timestamp":…,"sessionID":…,"error":{"name":
            #    "UnknownError","data":{"message":"Unexpected server error.
            #    Check server logs for details.","ref":"err_…"}}}
            # opencode's NamedError: `name` is the class, data.message is the
            # sentence — the one its own non-json path prints (cli/cmd/run.ts
            # :783-786 at v1.18.35). Read it the same way: data.message, else
            # the name, else the whole envelope rather than "".
            e = o.get("error")
            msg = _txt(o.get("message"))
            if not msg and isinstance(e, dict):
                data = e.get("data") if isinstance(e.get("data"), dict) else {}
                msg = _txt(e.get("message") or data.get("message") or e.get("name"))
            elif not msg:
                msg = _txt(e)
            return [_ev("error", message=msg or json.dumps(o, ensure_ascii=False)[:600])]
        return None          # not this dialect — parse_generic / claude-family get their turn
    pt = str(part.get("type") or "").lower()
    if t == "text" or pt == "text":
        if "text" not in part:
            # Field-name drift (the parse_grok rule): with no `text` key the
            # prose may sit elsewhere. The generic fallback would CLAIM this as
            # text="" (it reads top-level keys), so hand the line back legible
            # ourselves rather than decline into a blank frame.
            return [_ev("text", text=json.dumps(o, ensure_ascii=False))]
        return [_ev("text", text=_txt(part.get("text")))]
    if t in ("reasoning", "thinking") or pt in ("reasoning", "thinking"):
        body = _txt(part.get("text") or part.get("reasoning") or part.get("thinking"))
        return [_ev("thinking", text=body)] if body else None
    if t == "tool_use" or pt == "tool":
        name = str(part.get("tool") or part.get("name") or "")
        st = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = str(st.get("status") or "").lower()
        cid = str(part.get("callID") or part.get("id") or "")
        args = _oc_args(name, st.get("input"))
        out = [_ev("tool_call", tool=name, args=args, id=cid)]
        if status in ("completed", "error", "failed"):
            failed = status != "completed"
            body = _txt(st.get("error") if failed else st.get("output"))
            if failed and not body:
                body = status
            out.append(_ev("tool_result", tool=name,
                           status="error" if failed else "ok", result=body, id=cid))
            path = args.get("path")
            if not failed and name.lower() in _OC_WRITE_TOOLS and path:
                out.append(_ev("file_edit", path=str(path), tool=name,
                               change="write" if name.lower() == "write" else "edit"))
        return out
    if t == "step_start" or pt == "step-start":
        return [_ev("status", state="step_start", detail="")]
    if t == "step_finish" or pt == "step-finish":
        # stop_reason: the model step's own finish reason, read by
        # harness_core to tell a run that stopped at the model's output limit
        # ("length" — one run: 31,998 reasoning + 2 output = 32,000, then
        # nothing) from one that finished.
        out = [_ev("status", state="step_finish", detail=str(part.get("reason") or ""),
                   stop_reason=str(part.get("reason") or ""))]
        tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else None
        if tok:
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            usage = {}
            for src, dst in (("input", "input_tokens"), ("output", "output_tokens"),
                             ("reasoning", "reasoning_tokens"), ("total", "total_tokens")):
                if isinstance(tok.get(src), (int, float)):
                    usage[dst] = tok[src]
            if isinstance(cache.get("read"), (int, float)):
                usage["cache_read_tokens"] = cache["read"]
            if isinstance(cache.get("write"), (int, float)):
                usage["cache_write_tokens"] = cache["write"]
            if isinstance(part.get("cost"), (int, float)):
                usage["cost_usd"] = part["cost"]
            if usage:
                out.append(_ev("usage", **usage))
        return out
    if t == "error" or pt == "error":
        msg = _txt(part.get("message") or part.get("error") or o.get("message") or o.get("error"))
        return [_ev("error", message=msg or "opencode error")]
    return None


# ── cursor — `cursor-agent -p --output-format stream-json --stream-partial-output`
# MEASURED on a live run (2026-10-07, 925 lines). The generic
# Claude-style reader claimed only 3 of the shapes below; 524 lines fell to
# the never-drop fallback and printed as raw JSON rows, its 397 one-word
# assistant fragments each became a row, and the result frame's usage was
# dropped by that reader's early return on `result`.
#
#   type        subtype     fields                                   seen
#   system      init        model, cwd, permissionMode               1
#   user        —           message.content[text] (the prompt)       1
#   thinking    delta       text (a word or two)                     482
#   thinking    completed   no text                                  8
#   assistant   —           message.content[text], timestamp_ms      396 (fragments)
#   assistant   —           the same, NO timestamp_ms                1 (whole message)
#   tool_call   started     call_id, tool_call{<x>ToolCall:{args}}   17
#   tool_call   completed   + tool_call{<x>ToolCall:{args, result:
#                             {success|failure: {...}}}}             17
#   result      success     result (text), is_error, duration_ms,
#                           usage{inputTokens, outputTokens,
#                                 cacheReadTokens, cacheWriteTokens} 1
#
# The whole-message assistant frame repeats, byte for byte, the fragments
# since the last non-prose frame (measured: 1,235 chars, equal), so it is a
# lifecycle status when it matches and prose only when no fragments came
# (a run without --stream-partial-output). Tool keys seen: editToolCall,
# shellToolCall, globToolCall; any `<x>ToolCall` reads by the same rule, its
# name being <x>. The result's usage keeps cache SEPARATE from input
# (cacheReadTokens 205,468 > inputTokens 57,238 on one run), which
# compute._CACHE_SEPARATE states for cursor.
_CURSOR_TEXT = "cursor_text"
# Set once any prose went out as text this run. The result frame's `result`
# repeats that prose (on one run: the first paragraph of each earlier span plus
# the WHOLE final message, 1,566 chars), and both views turn a done detail
# over 80 chars into a report step — the final message showed twice.
_CURSOR_SAID = "cursor_said"
# Fields of a tool result that are whole file bodies or patches: sized by the
# raw view, never printed as the step's result.
_CURSOR_BULK = frozenset({"beforeFullFileContent", "afterFullFileContent",
                          "diffString", "streamContent", "parsingResult"})


def _cursor_tool(o: dict) -> tuple[str, dict]:
    """(name, inner) of a tool_call frame: the first `<x>ToolCall` key."""
    tc = o.get("tool_call") if isinstance(o.get("tool_call"), dict) else {}
    for k, v in tc.items():
        if k.endswith("ToolCall") and isinstance(v, dict):
            return (k[: -len("ToolCall")] or "tool"), v
    return "", {}


def _cursor_args(name: str, inner: dict) -> dict:
    """The vendor's args plus the renderer's keys (path / command / pattern —
    run-frames.ts toolCallLines), vendor keys untouched."""
    a = inner.get("args")
    out = dict(a) if isinstance(a, dict) else {}
    if "path" not in out:
        for k in ("filePath", "targetFile", "targetDirectory", "directory"):
            if isinstance(out.get(k), str) and out[k]:
                out["path"] = out[k]
                break
    if "pattern" not in out:
        for k in ("globPattern", "query", "regex"):
            if isinstance(out.get(k), str) and out[k]:
                out["pattern"] = out[k]
                break
    if "description" not in out and isinstance(inner.get("description"), str):
        out["description"] = inner["description"]
    return out


def _cursor_result(res) -> tuple[bool, str, int | None]:
    """(ok, body, exit_code) of a completed tool's `result`."""
    if not isinstance(res, dict):
        return True, _txt(res), None
    ok = "success" in res
    body = res.get("success") if ok else (res.get("failure") or res.get("error")
                                          or res.get("rejected"))
    rc = None
    if isinstance(body, dict):
        try:
            rc = int(body["exitCode"]) if body.get("exitCode") not in (None, "") else None
        except (TypeError, ValueError):
            rc = None
        text = ""
        for k in ("interleavedOutput", "stdout", "message", "content", "error"):
            if isinstance(body.get(k), str) and body[k].strip():
                text = body[k]
                break
        if not text and isinstance(body.get("stderr"), str):
            text = body["stderr"]
        if not text:
            files = body.get("files")
            if isinstance(files, list):
                text = "\n".join(str(f) for f in files) or "no matches"
        if not text:
            rest = {k: v for k, v in body.items() if k not in _CURSOR_BULK}
            text = json.dumps(rest, ensure_ascii=False) if rest else ""
        if rc not in (None, 0):
            ok = False
        return ok, text, rc
    return ok, _txt(body), None


def parse_cursor(o: dict, state: dict | None = None) -> list[dict] | None:
    t = str(o.get("type") or "").lower()
    st = str(o.get("subtype") or "").lower()
    buf = state if isinstance(state, dict) else {}
    if t not in ("assistant", "thinking"):
        # Any non-prose frame ends the prose the next whole message repeats.
        # Thinking does not: the whole message repeats the TEXT fragments
        # only, so reasoning streamed between them must neither end the
        # span nor join it, or the match fails and the message shows twice.
        buf[_CURSOR_TEXT] = ""
    if t == "thinking":
        if st == "delta":
            return [_ev("thinking", text=_txt(o.get("text")), delta=True)]
        if st == "completed":
            # Ends a reasoning span and carries no text: an empty chunk joins
            # the open span (renderers add nothing) instead of a raw row.
            return [_ev("thinking", text="", delta=True)]
        return None
    if t == "assistant":
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        content = msg.get("content")
        if isinstance(content, list):
            # Text blocks only: a thinking block inside a fragment is not
            # prose and is not in the whole message the fragments are
            # compared against.
            content = [b for b in content
                       if not (isinstance(b, dict) and str(b.get("type") or "").lower()
                               in ("thinking", "reasoning", "redacted_thinking"))]
        text = _txt(content)
        if "timestamp_ms" in o:
            buf[_CURSOR_TEXT] = buf.get(_CURSOR_TEXT, "") + text
            if text:
                buf[_CURSOR_SAID] = True
            return [_ev("text", text=text, delta=True)]
        seen = buf.get(_CURSOR_TEXT, "")
        buf[_CURSOR_TEXT] = ""
        if seen and text == seen:
            return [_ev("status", state="message_end", detail=f"{len(text)} chars")]
        if text:
            buf[_CURSOR_SAID] = True
        return [_ev("text", text=text)] if text else None
    if t == "tool_call":
        name, inner = _cursor_tool(o)
        if not name:
            return None
        cid = str(o.get("call_id") or (o.get("tool_call") or {}).get("toolCallId") or "")
        args = _cursor_args(name, inner)
        if st == "started":
            return [_ev("tool_call", tool=name, args=args, id=cid)]
        if st == "completed":
            ok, body, rc = _cursor_result(inner.get("result"))
            out = [_ev("tool_result", tool=name, status="ok" if ok else "error",
                       result=body, exit_code=rc, id=cid)]
            res = inner.get("result") if isinstance(inner.get("result"), dict) else {}
            succ = res.get("success") if isinstance(res.get("success"), dict) else None
            path = (succ or {}).get("path") or args.get("path")
            if ok and succ is not None and name.lower() in ("edit", "write") and path:
                # No `tool`: it folds onto the call's own row (run-steps.ts
                # file_edit; the shape grok and antigravity already send).
                out.append(_ev("file_edit", path=str(path), id=cid,
                               change="edit" if "beforeFullFileContent" in succ else "write"))
            return out
        return None
    if t == "result":
        out = []
        if o.get("is_error") or (st and st != "success"):
            out.append(_ev("error", message=_txt(o.get("result") or o.get("error"))
                           or f"cursor result: {st or 'error'}"))
        # The result text is the run's closing report only when no prose
        # streamed (see _CURSOR_SAID); otherwise it is a second copy.
        out.append(_ev("status", state="done",
                       detail="" if buf.get(_CURSOR_SAID) else _txt(o.get("result"))))
        u = o.get("usage")
        if isinstance(u, dict):
            nums = {k: v for k, v in u.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)}
            if nums:
                out.append(_ev("usage", **nums))
        return out
    if t == "user":
        # The prompt, echoed back. "user_input" is a lifecycle state both
        # views skip (antigravity sends the same step).
        return [_ev("status", state="user_input", detail="prompt received")]
    if t == "system":
        return [_ev("status", state="init", detail=str(o.get("model") or ""))]
    if t == "error":
        return [_ev("error", message=_txt(o.get("message") or o.get("error")))]
    return None


# Harnesses whose writer can pack SEVERAL events into one line — or whose one
# line is several events of ours (a codex patch is one file_edit per path).
# Consulted by normalize_line first; the single-event PARSERS entry stays as
# the fallback dialect reader (kimi 0.32-style claude-vocabulary lines are
# role-less and land there; codex lines it declines go to parse_codex, whose
# own fallback is the claude-family reader).
# Multi-parsers that take the caller's per-run `state` dict as a second
# argument. Listed explicitly rather than sniffed, so adding state to a parser
# is a deliberate two-line change and the other four keep their signature.
STATEFUL_MULTI_PARSERS = {"kimi", "grok", "cursor"}

MULTI_PARSERS = {
    "kimi":        parse_kimi_roles,
    "codex":       parse_codex_events,
    "antigravity": parse_antigravity,
    # grok's `end` frame is a done-status PLUS a usage, the same shape codex's
    # turn.completed has; its per-token text/thought frames are one event each.
    "grok":        parse_grok,
    # opencode's one tool line is the call AND its result (+ a file_edit);
    # lines without a `part` envelope decline to parse_generic below.
    "opencode":    parse_opencode,
    # cursor's completed tool line is the result AND its file_edit; its
    # result frame is a done-status plus a usage. Lines it declines still
    # reach the claude-family reader (PARSERS["cursor"]).
    "cursor":      parse_cursor,
}


# ── generic JSONL (opencode's FALLBACK since 2026-08-21, see parse_opencode) ──
# Top-level keys only; classify defensively and fall back to the claude-family
# reader before giving up. opencode 1.17's `part`-wrapped lines never reach
# this (parse_opencode claims them); a future top-level dialect would.
def parse_generic(o: dict) -> dict | None:
    t = str(o.get("type") or o.get("event") or o.get("kind") or "").lower()
    if "error" in t or o.get("error"):
        return _ev("error", message=_txt(o.get("message") or o.get("error")))
    if "tool" in t:
        if "result" in t:
            return _ev("tool_result", tool=str(o.get("tool") or o.get("name") or ""),
                       status=str(o.get("status") or "ok"),
                       result=_txt(o.get("result") or o.get("content")))
        return _ev("tool_call", tool=str(o.get("tool") or o.get("name") or ""),
                   args=o.get("args") or o.get("input") or {})
    if "think" in t or "reason" in t:
        return _ev("thinking", text=_txt(o.get("text") or o.get("content")))
    if "usage" in t or o.get("usage"):
        u = o.get("usage") if isinstance(o.get("usage"), dict) else o
        return _ev("usage", **{k: v for k, v in u.items()
                               if isinstance(v, (int, float))})
    if t in ("text", "message", "assistant", "output") or o.get("text"):
        return _ev("text", text=_txt(o.get("text") or o.get("content")
                                     or o.get("message")))
    return parse_claude_family(o)


PARSERS = {
    "grok":     parse_claude_family,
    "cursor":   parse_claude_family,
    "codex":    parse_codex,
    "opencode": parse_generic,
    # kimi 0.32.0's --output-format stream-json emitted the Claude event
    # vocabulary, so claude-family stays as its FALLBACK dialect. 0.36.x
    # switched to role-based chat-message JSONL — that reads via
    # MULTI_PARSERS["kimi"] (parse_kimi_roles) in normalize_line; this entry
    # only sees lines the role parser declined.
    "kimi":     parse_claude_family,
    # antigravity lines parse_antigravity declines (no `event` key we know)
    # fall to the generic reader, whose own fallback is claude-family.
    "antigravity": parse_generic,
}


def normalize_line(harness_id: str, line: str, seq: int,
                   state: dict | None = None) -> list[dict]:
    """One raw stdout line -> ONE OR MORE normalized events (seq, seq+1, …).

    kimi >= 0.36 packs assistant prose plus several tool calls into a single
    JSONL line; forcing one event per line either drops parts (a never-drop
    violation) or leaves raw JSON in the transcript — the exact rendering
    seen on a live run (2026-08-20). NEVER returns []: an
    unparseable or unclassifiable line becomes one kind='text' event carrying
    the raw line.

    `state` is an OPTIONAL caller-owned dict, one per run, for parsers that need
    to remember something across lines — currently only kimi, which must map a
    tool result back to the tool_call id that named it (see parse_kimi_roles).
    Omitting it keeps the old stateless behaviour, so every existing caller and
    every other harness is unaffected; a kimi run without it simply falls back
    to labelling results "tool"."""
    raw = (line or "").rstrip("\n")
    ts = time.time()
    # raw=True marks the NEVER-DROP fallbacks below: a line no parser claimed
    # (a version banner, a pip warning, a blank). The transcript keeps it
    # verbatim as before; the runtime's watchdog reads the marker so a CLI
    # that printed its banner and then wedged is still the never-started case,
    # not a model turn (harness_core).
    if not raw.strip():
        return [{"ts": ts, "seq": seq, **_ev("text", text="", raw=True)}]
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return [{"ts": ts, "seq": seq, **_ev("text", text=raw, raw=True)}]
    if not isinstance(obj, dict):
        return [{"ts": ts, "seq": seq, **_ev("text", text=raw, raw=True)}]
    evs: list[dict] | None = None
    try:
        multi = MULTI_PARSERS.get(harness_id)
        if multi is not None:
            evs = (multi(obj, state) if harness_id in STATEFUL_MULTI_PARSERS
                   else multi(obj))
        if evs is None:
            ev = PARSERS.get(harness_id, parse_generic)(obj)
            evs = [ev] if ev is not None else None
    except Exception:                      # a vendor shape change must not kill the run
        evs = None
    if not evs:
        evs = [_ev("text", text=raw, raw=True)]
    return [{"ts": ts, "seq": seq + i, **e} for i, e in enumerate(evs)]


def normalize(harness_id: str, line: str, seq: int) -> dict:
    """One raw stdout line -> one normalized event. NEVER returns None: an
    unparseable or unclassifiable line becomes kind='text' carrying the raw
    line, so nothing the harness printed can vanish from the transcript.

    COMPAT SHIM over normalize_line — multi-event lines (kimi >= 0.36) return
    only their FIRST event here; the streaming loop uses normalize_line."""
    return normalize_line(harness_id, line, seq)[0]
