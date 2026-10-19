"""mastercode.runtime.tool_result_wall — the transcript wall + two constants
that outlived the retired Claude-subscription host engine (host_sdk_engine.py,
deleted and cut from routing).

  _bound_tool_result  every byte a tool hands back into a model transcript
                      passes through this (session.py host loop, scoped_agent
                      agent loop). Bounded and HONEST: never slice serialized
                      JSON — an 8000-char dumps slice cut
                      mid-token, the model got broken JSON with no marker and
                      flailed. Oversized fields truncate INLINE with a marker,
                      the envelope stays well-formed, `truncated: true` + the
                      note tell the model to narrow the query.
  NODE_HEAP_OPTS      the heap cap every Node CLI a C-Series pod spawns runs
                      under (harness sidecars, crash surface — server/crash.py,
                      entrypoint.sh, smoke).
  HOST_ENGINE_MODELS  the floor of the Claude model ids the claude-code
                      HARNESS offers in the coding matrix: what a cold catalog
                      still lists. A warm catalog adds its newer Claude rows
                      (server/routes/inference.py _claude_code_models).

Deliberately stdlib-only.
"""
from __future__ import annotations

import contextvars
import json

NODE_HEAP_OPTS = "--max-old-space-size=1536"

HOST_ENGINE_MODELS = (
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5",
    "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-5-5",
    "claude-haiku-4-5-20251001",
)

# THE WALL IS THE MODEL'S, NOT OURS. It used to be a fixed 100 KB per result /
# 60 KB per field — about 25k tokens,
# a small fraction of a current window — and the readers it cut (workspace_file
# read, context read, read_url, recall) had no offset, so whatever it cut was
# unreachable. Now:
#   * with the active model's context window known, one result may fill a
#     QUARTER of it (window tokens × CHARS_PER_TOKEN ÷ 4). Output takes at most
#     half the window (context_budget.clamp_output_reserve), so a quarter is
#     half of what input can ever have, and the pre-send trim
#     (session.py _call_with_overflow_retry) still owns the running total.
#   * with the window unknown, the old 100 KB stays as the wall.
#   * strings are cut before lists, and every cut is NAMED in `cuts` with what
#     was kept (chars, items). A cut READER page (offset/next_offset/
#     offset_unit) has its next_offset moved to where the cut fell, counted in
#     the page's own unit and codec, and its cut names that as `resume_at` —
#     the model reads on from there. No UTF-8 count is ever offered as a file
#     offset. Nothing is cut without saying where.
CHARS_PER_TOKEN = 4          # mirrors context_budget.CHARS_PER_TOKEN (gate-checked)
_WINDOW_SHARE_DIVISOR = 4    # one result ≤ a quarter of the window
_RESULT_MAX_BYTES = 100_000  # the wall when the model's window is unknown
_FIELD_SHARE = 0.6           # first cut of one field: 60% of the wall
_FIELD_MAX_BYTES = int(_RESULT_MAX_BYTES * _FIELD_SHARE)
_LIST_MAX_ITEMS = 500        # kept for importers; lists are now cut last
_FIELD_FLOOR = 512           # strings shrink to this before any list is cut
_CUTS_SHOWN = 20             # the cut list is itself bounded, and says so


# The active model's window for the tool call running NOW, so a reader can
# size a page to what its result can carry (read_page_cap) and
# _bound_tool_result uses it when no window is passed. It is SET by the loops
# that bound results — session.py and scoped_agent.py. Unset = unknown window =
# the legacy 100 KB
# wall, and a page sized to that.
TOOL_WALL_WINDOW: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "tool_wall_window", default=None)
# Room for a reader result's own fields (path, size, offsets, notes) beside
# its content, so a page sized by read_page_cap is not cut for its envelope.
_ENVELOPE_ROOM = 4096


def result_wall_bytes(context_window: int | None = None) -> int:
    """The transcript wall for one tool result, in serialized bytes.

    Derived from the active model's context window when the caller knows it;
    the legacy 100 KB when it does not."""
    try:
        w = int(context_window or 0)
    except (TypeError, ValueError):
        w = 0
    if w <= 0:
        return _RESULT_MAX_BYTES
    return max(4_096, w * CHARS_PER_TOKEN // _WINDOW_SHARE_DIVISOR)


def current_wall_bytes() -> int:
    """The wall for the tool call running now (TOOL_WALL_WINDOW)."""
    return result_wall_bytes(TOOL_WALL_WINDOW.get())


def read_page_cap(unit: str = "bytes") -> int:
    """The most one reader page should hold so its result is never cut by
    the wall: reading more only spends memory and CPU on
    bytes the wall then throws away, and hands back a next_offset past what
    the model saw. unit 'bytes' (text), 'chars', or 'b64' (raw bytes that go
    out as base64, 4 characters per 3 bytes)."""
    room = max(1024, current_wall_bytes() - _ENVELOPE_ROOM)
    if unit == "b64":
        return room * 3 // 4
    return room


# ── Paging a reader by what its page COSTS on the wire ──
# A reader page must arrive uncut, and what it fetches should be what it
# delivers. A page's JSON cost per source byte depends on the content — about
# 1 for ASCII, up to 3 for Cyrillic/Greek/emoji (\\uXXXX escapes), 4/3 for
# base64 — so each reader remembers the cost its last page of a file actually
# had and sizes the next fetch by it. The first page of a file assumes base64's
# 4/3, which every page fits; a page still over (a turn to heavier content) is
# trimmed by fit_page with an exact next_offset, and the next fetch follows.
from collections import OrderedDict as _OrderedDict

_PAGE_COST: "_OrderedDict" = _OrderedDict()
_PAGE_COST_KEEP = 512
_FIRST_PAGE_COST = 4 / 3


def page_fetch_size(key) -> int:
    """How many source units the next page of `key` should fetch so that its
    encoded page fits one result for this call."""
    cost = _PAGE_COST.get(key, _FIRST_PAGE_COST)
    return max(1, int(read_page_cap("bytes") / max(1.0, cost)))


def note_page_cost(key, json_chars: int, units: int) -> None:
    """Remember what the page just built cost on the wire per source unit."""
    if units <= 0:
        return
    # A guess only ever costs a trim (fit_page), never a byte, so a key
    # shared or evicted by another reader is harmless.
    try:
        _PAGE_COST[key] = max(1.0, json_chars / units)
        _PAGE_COST.move_to_end(key)
        while len(_PAGE_COST) > _PAGE_COST_KEEP:
            _PAGE_COST.popitem(last=False)
    except KeyError:
        pass


def fit_page(out: dict) -> None:
    """Trim a reader page (content + offset + next_offset + offset_unit) whose
    ENCODED content is over what one result carries, so it never reaches the
    wall over size; next_offset moves to exactly where the kept content ends
    — in bytes of the page's own codec, base64 quanta, or characters."""
    content = out.get("content")
    budget = read_page_cap("bytes")
    if not isinstance(content, str) or not isinstance(out.get("offset"), int):
        return
    if len(json.dumps(content)) <= budget:
        return
    b64 = out.get("encoding") == "base64"
    lo, hi = 0, len(content)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        piece = content[:mid]
        if b64:
            piece = piece[: len(piece) // 4 * 4]
        if len(json.dumps(piece)) <= budget:
            lo = mid
        else:
            hi = mid - 1
    keep = content[:lo]
    if b64:
        keep = keep[: len(keep) // 4 * 4]
        adv = len(keep) // 4 * 3
    elif out.get("offset_unit") == "chars":
        adv = len(keep)
    else:
        codec = out.get("codec") or "utf-8"
        try:
            adv = len(keep.encode(codec))
        except (LookupError, UnicodeEncodeError):
            adv = len(keep.encode("utf-8", "surrogatepass"))
    if not keep or adv <= 0:
        return                                      # never an empty page
    out["content"] = keep
    out["next_offset"] = out["offset"] + adv
    if "end_byte" in out:
        out["end_byte"] = out["next_offset"]
    out["eof"] = False
    out["fitted"] = "trimmed to what one result carries; read on from next_offset"


def _fix_reader_offsets(orig: dict, shrunk: dict, s_budget: int):
    """A reader page the wall cut (a dict carrying content + offset +
    next_offset + offset_unit): its next_offset becomes where the cut fell,
    in the reader's own unit, so reading on from next_offset skips nothing."""
    content = orig.get("content")
    cut = shrunk.get("content")
    unit = orig.get("offset_unit")
    if not (isinstance(content, str) and isinstance(cut, str) and cut != content
            and isinstance(orig.get("offset"), int) and unit in ("bytes", "chars", "b64")):
        return None
    kept = content[:s_budget]          # a cut string keeps exactly s_budget chars
    if unit == "chars":
        adv = len(kept)
    elif unit == "b64":
        adv = (len(kept) // 4) * 3
    else:
        # Bytes in the page's OWN codec: a
        # latin-1 or utf-16 page counted as utf-8 put next_offset past text
        # the model never saw. A reader names the codec it decoded with.
        codec = orig.get("codec") or orig.get("encoding") or "utf-8"
        try:
            adv = len(kept.encode(codec))
        except (LookupError, UnicodeEncodeError):
            adv = len(kept.encode("utf-8", "replace"))
    shrunk["next_offset"] = orig["offset"] + adv
    shrunk["next_offset_note"] = ("the transcript wall cut this page; next_offset is "
                                  "where the cut fell — read on from there")
    return shrunk["next_offset"]


def _shrink_fields(obj, s_budget: int, l_budget, path: str = "result",
                   cuts: list | None = None):
    if isinstance(obj, str) and len(obj) > s_budget:
        kept = obj[:s_budget]
        if cuts is not None:
            cuts.append({"path": path, "kept_chars": s_budget,
                         "total_chars": len(obj)})
        return kept + f" …[+{len(obj) - s_budget} chars truncated]"
    if isinstance(obj, list):
        head = obj if l_budget is None else obj[:l_budget]
        out = [_shrink_fields(v, s_budget, l_budget, f"{path}[{i}]", cuts)
               for i, v in enumerate(head)]
        if len(head) < len(obj):
            if cuts is not None:
                cuts.append({"path": path, "kept_items": len(head),
                             "total_items": len(obj)})
            out.append(f"…[+{len(obj) - len(head)} more items truncated]")
        return out
    if isinstance(obj, dict):
        out = {k: _shrink_fields(v, s_budget, l_budget, f"{path}.{k}", cuts)
               for k, v in obj.items()}
        if "offset_unit" in obj:
            resume = _fix_reader_offsets(obj, out, s_budget)
            if resume is not None and cuts is not None:
                for c in reversed(cuts):
                    if c.get("path") == f"{path}.content":
                        c["resume_at"] = resume
                        break
        return out
    return obj


def _longest_list(obj) -> int:
    best = 0
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, list):
            best = max(best, len(o))
            stack.extend(o)
        elif isinstance(o, dict):
            stack.extend(o.values())
    return best


def _bound_tool_result(result, context_window: int | None = None) -> str:
    """Serialize a tool result for a model transcript, bounded and honest.

    `context_window` is the active model's window in tokens (None = unknown,
    the legacy 100 KB wall). Callers that know it pass it — the Host loop
    (session.py) and the dispatched-agent loop (scoped_agent.py)."""
    if context_window is None:
        context_window = TOOL_WALL_WINDOW.get()
    try:
        payload = json.dumps(result, default=str)
    except Exception:
        payload = json.dumps({"status": "error", "result": "unserializable tool result"})
    wall = result_wall_bytes(context_window)
    if len(payload) <= wall:
        return payload
    total = len(payload)

    def _wrap(s_budget, l_budget):
        cuts: list = []
        shrunk = _shrink_fields(result, s_budget, l_budget, "result", cuts)
        env = {
            "truncated": True,
            "total_bytes": total,
            "wall_bytes": wall,
            "note": ("result exceeded the transcript wall — cut inline, and each "
                     "cut is named in `cuts` with what was kept. A cut page from "
                     "a reader (workspace_file, context, read_url) had its "
                     "next_offset moved to where the cut fell: read on with "
                     "offset = that next_offset (the cut's resume_at). A cut "
                     "list: read on with offset = kept_items. Or narrow the "
                     "query."),
            "cuts": cuts[:_CUTS_SHOWN],
        }
        if len(cuts) > _CUTS_SHOWN:
            env["more_cuts"] = len(cuts) - _CUTS_SHOWN
        env["result"] = shrunk
        return json.dumps(env, default=str)

    # Phase 1: strings only — every record of a list is kept whole in count.
    s_budget = max(_FIELD_FLOOR, int(wall * _FIELD_SHARE))
    while True:
        payload = _wrap(s_budget, None)
        if len(payload) <= wall:
            return payload
        if s_budget <= _FIELD_FLOOR:
            break
        s_budget = max(_FIELD_FLOOR, s_budget // 2)
    # Phase 2: lists too, strings on down to a 64-char floor.
    l_budget = max(1, _longest_list(result) // 2)
    for _ in range(40):
        payload = _wrap(s_budget, l_budget)
        if len(payload) <= wall:
            return payload
        if s_budget <= 64 and l_budget <= 1:
            break
        s_budget = max(64, s_budget // 2)
        l_budget = max(1, l_budget // 2)
    # A structure this cut-resistant (e.g. tens of thousands of dict keys) is
    # a bug upstream — refuse it in one well-formed piece, never broken JSON.
    return json.dumps({
        "truncated": True, "total_bytes": total, "wall_bytes": wall,
        "status": "error",
        "result": "tool result too pathological to shrink — re-query narrower",
    })
