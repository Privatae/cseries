"""mastercode.runtime.tool_trail — the host's own recent tool calls, carried
into the NEXT turn's context as a record instead of a memory.

WHY. Two fabrications in
one thread, both with the same mechanism. One turn said "Codex is wired — the
grant landed" with ZERO tool calls; the grant landed six minutes later when a
later turn actually called login_status. Another turn quoted the wrong task
number for a codex build. In both cases the evidence existed — the
previous turn's tool RESULT carried a next_step ("when they say they have
entered it, call op='login_status'") and the build result carried the task id
— but tool results do not survive into the next turn: history is user/
assistant prose, and `next_step` was set in eight places and consumed nowhere
durable. So on "done" the model had no pending-action record, inferred the
outcome from the user's words, and refilled the turn from whatever context was
loudest. That is a missing data lane, not a prompt problem
(cure inference with context): we cannot stop a model inferring,
we can only give it the record that makes inference unnecessary.

WHAT. Two halves, both stdlib-only:

  digest_tool_action(tool, args, result)
      The durable extra fields for one tool call — op / provider / harness,
      the result's `next_step` (the instruction the tool handed the host),
      and a small allow-listed `digest` of the result (task_id, session_id,
      state, done, url, user_code…). session.py folds these into the
      per-call chip it already persists on the assistant turn row
      (metadata.tool_calls) — the same bounded record the Circles chips
      replay from, so nothing new is stored, the existing record just
      carries enough to be a record.

  render_tool_trail(turns)
      The per-turn block: the last few assistant turns' calls in this thread
      with their digests, and every `next_step` that is still PENDING —
      i.e. no later call to the same tool (same provider/harness) has
      happened and the call itself did not come back done. Resolved steps
      are not shown; the host sees a still-open instruction, not a history
      lesson. memory_engine.recall_tool_trail reads the rows; session.py
      appends the block to the dynamic half of the system prompt (after
      history, before the user message — the cache-stable position every
      other per-turn block uses) and logs it under the name "tool_trail".
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# How many assistant turns (that made tool calls) the trail shows, newest
# last. Three covers "I started a login / a build, the user replied once or
# twice, now they say it's done" without re-narrating the whole thread.
TOOL_TRAIL_MAX_TURNS = 3
TOOL_TRAIL_MAX_CALLS = 12
_NEXT_STEP_MAX = 600
_DIGEST_STR_MAX = 240

# Result keys worth carrying forward. Ids the host will need to name later
# (task/session/project), the outcome shape the host must not guess at
# (state/done/ok/persisted/supported), and the things a login hands the user
# (url/user_code — shown to the user anyway, and the code expires in minutes).
# Never tokens, never file contents: rotated_creds and friends stay out.
_DIGEST_KEYS = (
    "task_id", "session_id", "project_id", "harness", "harnesses", "model",
    "state", "done", "ok", "persisted", "supported", "login_waiting",
    "url", "user_code", "preview_url", "files_preview_url", "exit_code",
    # Which repo a build/review touched — the knowledge table of contents
    # counts repos per thread from the trail; without this key
    # it could only recover them from result URLs.
    "repo",
    # Which artifact a create/write/promote/import/revert left behind. A
    # reloaded chat draws that artifact as a thumbnail (circles-web
    # lib/artifact-ref.ts); a write's result has no preview_url, so without
    # this key only creates survived a reload.
    "artifact_id",
)


def _clip(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def digest_tool_action(tool: str, args: dict | None, result: Any) -> dict:
    """Extra chip fields for one tool call. Empty dict when nothing is worth
    carrying (a tool that returned prose, no ids, no next step)."""
    out: dict = {}
    a = args if isinstance(args, dict) else {}
    # `operate(tool=X, args={op: …})` is the lane every write takes (an action
    # tool named directly is refused with a hint to use operate), so the chip
    # for an operate call carries the INNER tool as `operate_tool` and reads
    # its op from the inner args — otherwise the record of a desktop write
    # was `{tool: operate, status: ok}` and the refused direct call was the
    # only chip that ever said `artifact op=set_desktop`.
    if str(tool or "") == "operate":
        inner_tool = a.get("tool")
        if isinstance(inner_tool, str) and inner_tool.strip():
            out["operate_tool"] = _clip(inner_tool.strip(), 80)
        inner_args = a.get("args")
        if isinstance(inner_args, dict):
            # Inner wins: the operated tool's own provider/harness/op are the
            # facts; operate's outer args are intent/tool/args and carry none
            # of them today, so this only matters if it ever grows one.
            a = {**{k: v for k, v in a.items() if k not in ("op", "action")}, **inner_args}
    for src, dst in (("op", "op"), ("action", "op"), ("provider", "provider"),
                     ("harness", "harness")):
        v = a.get(src)
        if isinstance(v, (str, int)) and str(v).strip() and dst not in out:
            out[dst] = _clip(v, 80)

    if not isinstance(result, dict):
        return out
    inner = result.get("result") if isinstance(result.get("result"), dict) else {}

    ns = result.get("next_step") or inner.get("next_step")
    if isinstance(ns, str) and ns.strip():
        out["next_step"] = _clip(ns.strip(), _NEXT_STEP_MAX)

    digest: dict = {}
    for k in _DIGEST_KEYS:
        v = inner.get(k) if k in inner else result.get(k)
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, (dict, list)):
            try:
                v = _clip(json.dumps(v, default=str), _DIGEST_STR_MAX)
            except Exception:  # noqa: BLE001
                continue
        elif isinstance(v, str):
            v = _clip(v, _DIGEST_STR_MAX)
        digest[k] = v
    # The tool's own status word when it is not the plain "ok".
    st = result.get("status")
    if isinstance(st, str) and st and st != "ok":
        digest["status"] = st
        err = result.get("result") if st == "error" else None
        if isinstance(err, str) and err.strip():
            digest["error"] = _clip(err.strip(), _DIGEST_STR_MAX)
    if digest:
        out["digest"] = digest
    return out


def _same_target(a: dict, b: dict) -> bool:
    """Does a LATER call `b` act on the same tool + target (provider/harness)
    as `a`? Only such a call resolves (or supersedes) a's next_step. When `a`
    names a target, `b` must name the SAME one — a later provider-less call on
    the tool (harness_connections op=list / op=matrix) is a different action
    and leaves the step pending ("connect codex → what else is connected? →
    done" must still show the login step open)."""
    if (a.get("tool") or "") != (b.get("tool") or ""):
        return False
    # Two operate calls are the same action only when they operated the
    # same tool; `operate_tool` is that tool. A search-mode
    # read(tool=X) names X as `read_tool` the same way.
    for k in ("operate_tool", "read_tool", "provider", "harness"):
        av, bv = a.get(k), b.get(k)
        if av and (not bv or str(av) != str(bv)):
            return False
    return True


def _when(ts: Any) -> str:
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%H:%M UTC")
    if isinstance(ts, str) and len(ts) >= 16:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%H:%M UTC")
        except Exception:  # noqa: BLE001
            return ts[11:16] + " UTC"
    return "earlier"


def trail_from_rows(rows: list) -> list[dict]:
    """DB rows ({created_at, metadata|tool_calls}) -> [{created_at, tool_calls}]
    chronological, keeping only rows that actually carry calls."""
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        calls = r.get("tool_calls")
        if calls is None:
            md = r.get("metadata") or {}
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:  # noqa: BLE001
                    md = {}
            calls = md.get("tool_calls") if isinstance(md, dict) else None
        if not isinstance(calls, list) or not calls:
            continue
        out.append({"created_at": r.get("created_at"),
                    "tool_calls": [c for c in calls if isinstance(c, dict)]})
    return out


def _fmt_call(c: dict) -> str:
    head = str(c.get("tool") or "tool")
    for k in ("operate_tool", "read_tool", "op", "provider", "harness"):
        if c.get(k):
            head += f" {k}={c[k]}"
    status = c.get("status") or "ok"
    parts = [f"{head} → {status}"]
    if c.get("error"):
        parts.append(f"error={_clip(c['error'], 160)}")
    d = c.get("digest") or {}
    if isinstance(d, dict):
        for k, v in d.items():
            if k in ("status", "error"):
                continue
            parts.append(f"{k}={v}")
    return " · ".join(parts)


def render_tool_trail(turns: list[dict], *, max_turns: int | None = None,
                      max_calls: int | None = None) -> str:
    """The per-turn block, or "" when there is nothing to carry. `turns` is
    chronological; only the newest `max_turns` (default TOOL_TRAIL_MAX_TURNS)
    are rendered, at most `max_calls` of them (default TOOL_TRAIL_MAX_CALLS),
    and a pending next_step is judged against EVERY later call in the whole
    list. A turn about the host itself (prefire_tools.is_self_turn) passes
    wider bounds so a self-review reads the thread's record, not three turns
    of it: a host once described a dispatch refusal
    from earlier in the thread as a "grant signature mismatch" — words that
    exist nowhere in the platform — because the refusal had fallen out of the
    three-turn window and it narrated from memory."""
    n_turns = int(max_turns) if max_turns and int(max_turns) > 0 else TOOL_TRAIL_MAX_TURNS
    n_calls = int(max_calls) if max_calls and int(max_calls) > 0 else TOOL_TRAIL_MAX_CALLS
    turns = [t for t in (turns or []) if isinstance(t, dict) and t.get("tool_calls")]
    if not turns:
        return ""
    # Flatten chronologically so "a later call" is a simple index comparison.
    flat: list[tuple[int, dict]] = []
    for ti, t in enumerate(turns):
        for c in t["tool_calls"]:
            if isinstance(c, dict):
                flat.append((ti, c))
    if not flat:
        return ""
    first_shown = max(len(turns) - n_turns, 0)

    def _is_pending(pos: int, c: dict) -> bool:
        ns = c.get("next_step")
        if not (isinstance(ns, str) and ns.strip()):
            return False
        d = c.get("digest") or {}
        if isinstance(d, dict) and d.get("done") is True:
            return False            # the call itself finished the job
        if (c.get("status") or "ok") in ("error", "blocked", "denied"):
            return False            # a failed call's next_step is advice, not a promise
        # Resolved/superseded by ANY later call on the same tool+target —
        # judged against the WHOLE thread tail, not just the shown turns.
        return not any(_same_target(c, cj) for i, (_tj, cj) in enumerate(flat) if i > pos)

    # Candidate lines: every call in the shown turns, chronological, each
    # tagged with its pending verdict. The clip keeps the NEWEST calls and
    # never drops a pending one — a 100-tool turn must not push the login
    # that is still waiting, or the build that just started, out of view
    # (keeping the oldest twelve would).
    cands: list[tuple[int, dict, bool, str]] = []
    for pos, (ti, c) in enumerate(flat):
        if ti < first_shown:
            continue
        cands.append((ti, c, _is_pending(pos, c), _when(turns[ti].get("created_at"))))
    if not cands:
        return ""
    keep = cands
    elided = 0
    if len(cands) > n_calls:
        pend = [x for x in cands if x[2]]
        rest = [x for x in cands if not x[2]]
        room = max(n_calls - len(pend), 0)
        chosen = set(id(x) for x in pend[-n_calls:]) | set(id(x) for x in rest[len(rest) - room:] if room)
        keep = [x for x in cands if id(x) in chosen]
        elided = len(cands) - len(keep)

    lines = [
        "## Your recent tool calls in this thread (the record — not memory)",
        "These are calls YOU made in earlier turns and exactly what came back. "
        "A step marked PENDING has not happened yet: nothing below (and nothing "
        "the user said) counts as having done it — do it with the named tool "
        "call, or say plainly that it is still open. Quote ids (task, session) "
        "from here, never from recollection.",
    ]
    if elided:
        lines.append(f"(… {elided} earlier call{'s' if elided != 1 else ''} in these "
                     "turns not shown — none of them pending.)")
    pending_n = 0
    for _ti, c, pending, when in keep:
        lines.append(f"- [{when}] {_fmt_call(c)}")
        if pending:
            pending_n += 1
            lines.append(f"  PENDING next step: \"{_clip(str(c.get('next_step')).strip(), _NEXT_STEP_MAX)}\"")
    if pending_n:
        lines.append(f"({pending_n} pending step{'s' if pending_n != 1 else ''} above "
                     "— still open until the named call is actually made.)")
    return "\n".join(lines)
