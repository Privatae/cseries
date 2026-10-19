"""Search hits the Host already has in front of it are not sent to it again.

Many turns cost more in input than in output, so reducing context noise is a
critical step. Measured: a 14-call turn ran ten local_grep calls, each with a DIFFERENT
pattern, and by a read-only re-run of those greps each first page came back
almost the same (the Host's own pinned desktops and the skill files). The
exact-argument dedup in the tool loop (session.py, _dedup_cache) never fired,
because no two calls had the same arguments — the repeats were in the RESULTS.
Every repeated hit was paid for again as input on every later round.

So a search result is filtered, per turn, by what it returns: a hit whose exact
text was already returned this turn, in a message that is STILL in the Host's
context, is dropped from the new result and counted instead. Nothing is ever
refused — a narrowing search, a re-check, a page with nothing new all still
run and say what they found. Three rules keep that honest:

  * A hit counts as seen only while the message that carried it is still in
    the loop's message list, as the same object. The pre-send trim
    (context_budget.trim_messages_to_window) never edits a message in place:
    it drops it, or replaces it with a new truncation envelope. Either way the
    identity test fails and the hit is shown again.
  * A result is recorded only when it went to the model whole — the
    transcript wall (tool_result_wall._bound_tool_result) may cut a long one,
    and a hit cut out of view was never seen.
  * The key is the hit's own text, not just its place: a file the Host edited
    mid-turn has new text at that line, and that is a new hit.

Applies to the searches that return a list of hits: local_grep (path + line)
and search_my_code (project + file + chunk). Scope is ONE turn — the loop
builds a fresh TurnSearchSeen per turn, so nothing here outlives the context
it describes.
"""
from __future__ import annotations

import hashlib
import json


def _digest(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:20]


def _local_grep_key(args: dict, hit: dict) -> str | None:
    if not isinstance(hit, dict) or not hit.get("path"):
        return None
    return _digest("local_grep", hit.get("path"), hit.get("line"), hit.get("content"))


def _search_my_code_key(args: dict, hit: dict) -> str | None:
    if not isinstance(hit, dict) or not hit.get("file_path"):
        return None
    # A hit names its project (a search of everything spans many); the
    # argument is the fallback for a hit that does not.
    if "content" in hit:
        text = hit.get("content")
    else:
        # A locator (tools._search_locator): its line range and the start of
        # its snippet stand for the chunk — the snippet's length follows the
        # size of the search it came in, so only its first 100 characters.
        text = (hit.get("start_line"), hit.get("end_line"), str(hit.get("snippet") or "")[:100])
    return _digest("search_my_code", hit.get("project") or (args or {}).get("project"),
                   hit.get("file_path"), hit.get("chunk_index"), text)


# tool name → (the list field inside result, the hit key, the count field to
# keep in step with the list, or None)
_SHAPES = {
    "local_grep": ("matches", _local_grep_key, "total_hits_returned"),
    "search_my_code": ("results", _search_my_code_key, "count"),
}


def _hit_list(func_name: str, tool_result):
    shape = _SHAPES.get(func_name)
    if shape is None or not isinstance(tool_result, dict) or tool_result.get("status") != "ok":
        return None, None
    inner = tool_result.get("result")
    if not isinstance(inner, dict) or not isinstance(inner.get(shape[0]), list):
        return None, None
    return shape, inner


def _rounds(rounds: list[int]) -> str:
    return ", ".join(str(r) for r in rounds)


class TurnSearchSeen:
    """The search hits this turn has put in front of the Host, each tied to
    the message that carried it."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[dict, int]] = {}

    def filter(self, func_name: str, args: dict, tool_result, live_messages: list):
        """`tool_result` with every hit the Host can still see removed, plus an
        `already_shown` note — or `tool_result` itself (the same object) when
        nothing was removed. Never mutates `tool_result`: the loop keeps the
        raw result for its own exact-repeat check and the ledger."""
        shape, inner = _hit_list(func_name, tool_result)
        if shape is None:
            return tool_result
        field, key_of, count_field = shape
        live = {id(m) for m in live_messages}
        kept, dropped_rounds = [], set()
        dropped = 0
        for hit in inner[field]:
            key = key_of(args, hit)
            prior = self._seen.get(key) if key else None
            if prior is not None and id(prior[0]) in live:
                dropped += 1
                dropped_rounds.add(prior[1])
                continue
            kept.append(hit)
        if not dropped:
            return tool_result
        rounds = sorted(dropped_rounds)
        new_inner = dict(inner)
        new_inner[field] = kept
        if count_field and count_field in inner:
            new_inner[count_field] = len(kept)
        if kept:
            note = (f"{dropped} of the hits on this page were already returned this turn "
                    f"(round {_rounds(rounds)}) and are still above; only the new ones are listed.")
        else:
            note = (f"Every hit on this page was already returned this turn "
                    f"(round {_rounds(rounds)}) and is still above: this search found nothing new.")
            if inner.get("next_offset") is not None:
                note += f" More hits follow: offset={inner['next_offset']}."
        new_inner["already_shown"] = {"count": dropped, "rounds": rounds, "note": note}
        out = dict(tool_result)
        out["result"] = new_inner
        return out

    def record(self, func_name: str, args: dict, sent_result, sent_text: str,
               message: dict, round_no: int) -> None:
        """Remember the hits of `sent_result` as carried by `message` — only
        when `sent_text` (the bytes the model got) is `sent_result` whole,
        i.e. the transcript wall did not cut it."""
        shape, inner = _hit_list(func_name, sent_result)
        if shape is None:
            return
        try:
            whole = sent_text == json.dumps(sent_result, default=str)
        except Exception:
            whole = False
        if not whole:
            return
        field, key_of, _ = shape
        for hit in inner[field]:
            key = key_of(args, hit)
            if key:
                self._seen[key] = (message, round_no)
