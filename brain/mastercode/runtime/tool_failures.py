"""mastercode.runtime.tool_failures — the per-turn tool failure ledger.

A tool that fails the SAME way twice in one turn is DEAD for that turn. The
recovery nudge the host loop injects after a failed round used to say
"IMPORTANT: tool failed, try again" on every round, for every failure so far
(the hint list was built from the whole turn's actions, not the round's), so
two failures of one dead tool produced three nudges — and the nudge itself
pushed the host to re-call the dead tool once more before it stopped
(`code_grep` → "ripgrep (rg) not on operator PATH", twice, then a third
call on the nudge).

This module owns that judgement so it is testable without the loop:

  note_round(ledger, round_actions) -> {
      "hints": [per-tool recovery lines for failures that are NOT yet dead],
      "dead":  [lines naming tools dead this turn, with the error VERBATIM],
      "dead_tools": [names],
      "message": the one user-role message to inject, or "" when nothing failed,
  }

`ledger` is a dict the caller keeps for the turn: (tool, error) -> count.
Errors pass through verbatim (clipped, never paraphrased) — the host should
read the tool's own words, not a summary of them.

A FAILURE IS REPORTED, NOT ROUTED AROUND. The nudge used to open with "You MUST try a different approach
-- do NOT give up", which beat the prompt's honesty rule: refused
a read, the host tried a machine, then another lane, then ran the job in the
pod. So the message now says what a failure IS: a refusal (a blocked or
denied status, or a tool answering "refused: …") is reported as it stands and
never retried by another machine, lane, grant or tool; any other error is
retried only when the error itself says a retry can succeed.
"""
from __future__ import annotations

DEAD_AFTER = 2          # identical failures before a tool is dead for the turn
ERROR_CLIP = 300        # chars of the verbatim error carried into the message

_FAILED = ("error", "blocked", "denied")
_REFUSED = ("blocked", "denied")
_REFUSAL_PREFIXES = ("refused", "denied", "not permitted")

FAILED_HEAD = ("One or more tool calls failed. Tell the user what failed, in the "
               "tool's own words.\n\n")
DEAD_HEAD = "Dead tools this turn — do not retry them:\n"
# The one thing a refusal may lead to: a REQUEST shown to the person. A grant
# drafted with propose_mgmt_grant does nothing until they approve it, so it is
# not a way around the refusal — and it is offered only when the refusal says
# there is no grant at all, never when the user's own deny list said no.
DRAFT_GRANT_TAIL = (" The words say there is no signed grant: you may draft one for the user "
                    "to approve with propose_mgmt_grant and tell them — it does nothing until "
                    "they approve it.")


def is_refusal(status: str, error: str) -> bool:
    """A refusal is a gate saying no — not a fault to work around. Its status
    says so (blocked / denied), or the tool's own words open with it (the
    box's 'refused: …', the host gate's 'not permitted')."""
    if str(status or "") in _REFUSED:
        return True
    return str(error or "").strip().lower().startswith(_REFUSAL_PREFIXES)

# Tool-specific recovery lines (moved verbatim from session.py's inner
# _build_error_recovery_hints). These fire on the FIRST failure
# only; the second identical failure declares the tool dead instead.
_SPECIFIC_HINTS = {
    "journal": ("JOURNAL RECOVERY: The journal call failed. Try: journal(action='list') "
                "to see recent entries, or journal(action='search', query='<keyword>') "
                "to find by title. Do NOT guess journal IDs."),
    "context": ("CONTEXT RECOVERY: The context call failed. Try: context(action='list') "
                "to browse available paths, then use the exact path from the listing."),
    "memory": ("MEMORY RECOVERY: Memory search failed. Try a broader search query or "
               "memory(action='list')."),
    "research_db": ("QUERY RECOVERY: Database query failed. Try: research_db(action='databases') "
                    "to list available databases, then research_db(action='tables', "
                    "database='<name>') to see tables."),
}


def _clip(text: str, n: int = ERROR_CLIP) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def failure_key(tool: str, error: str) -> tuple[str, str]:
    """Identity of a failure: the tool and its error text with whitespace
    collapsed. Two failures with this key are the SAME failure."""
    return (str(tool or ""), " ".join(str(error or "").split()))


def note_round(ledger: dict, round_actions: list) -> dict:
    """Record this ROUND's failures in `ledger` and build the recovery message.

    round_actions: the tool_actions entries for the calls made in this round
    only (each {tool, result_status, error_detail}). Passing the whole turn
    re-counts earlier rounds and is the bug this module replaces."""
    hints: list[str] = []
    dead: list[str] = []
    dead_tools: list[str] = []
    for ta in round_actions or []:
        if not isinstance(ta, dict):
            continue
        if ta.get("result_status") not in _FAILED:
            continue
        tool = str(ta.get("tool") or "")
        err = str(ta.get("error_detail") or "")
        key = failure_key(tool, err)
        ledger[key] = int(ledger.get(key, 0)) + 1
        refusal = is_refusal(ta.get("result_status"), err)
        if ledger[key] >= DEAD_AFTER:
            if tool not in dead_tools:
                dead_tools.append(tool)
                if refusal:
                    # A refusal that repeats is the known pattern (refused,
                    # then another route). The dead line must not hand it one.
                    dead.append(
                        f"TOOL '{tool}' WAS REFUSED AGAIN — the same refusal "
                        f"{ledger[key]} times this turn, verbatim: {_clip(err)} "
                        f"Do not call it again this turn and do not take another "
                        f"route; report the refusal to the user."
                    )
                else:
                    dead.append(
                        f"TOOL '{tool}' IS DEAD THIS TURN — it failed the same way "
                        f"{ledger[key]} times. Its error, verbatim: {_clip(err)} "
                        f"Do not call it again this turn; take another route, or tell "
                        f"the user what it could not do."
                    )
            continue
        if ta.get("use_instead"):
            # The answer is only "use this other tool" (session.py: a real
            # tool named outside its lane). The tool result already says
            # which; "tell the user what failed" on top of it made the Host
            # narrate the miss and ask leave to make the right call (a live
            # test run). Counted above, so the same redirect
            # twice still declares the tool dead.
            continue
        if refusal:
            hints.append(
                f"TOOL '{tool}' WAS REFUSED: {_clip(err) or '(no reason given)'} "
                f"Report the refusal to the user as it is. Do not try another "
                f"machine, lane, grant, tool or approach to get around it."
                + (DRAFT_GRANT_TAIL if "no signed grant" in err.lower() else "")
            )
            continue
        specific = _SPECIFIC_HINTS.get(tool)
        if specific:
            hints.append(specific)
        else:
            hints.append(
                f"TOOL '{tool}' FAILED: {_clip(err) or '(no error text)'} "
                f"Say what failed. Retry only if the error itself says a retry can "
                f"succeed (a timeout, a busy service); otherwise report it."
            )
    if not hints and not dead:
        return {"hints": [], "dead": [], "dead_tools": [], "message": ""}
    if hints:
        head = FAILED_HEAD
        body = "\n".join(hints)
        if dead:
            body += "\n\n" + "\n".join(dead)
    else:
        head = DEAD_HEAD
        body = "\n".join(dead)
    return {"hints": hints, "dead": dead, "dead_tools": dead_tools, "message": head + body}


# verbatim_errors() was removed with the working-memory synthesis
# it existed for (runtime/context._brain_synthesize_context): it built the
# verbatim error list that got appended AFTER the synthesizer's paraphrase.
# With no synthesizer there is no paraphrase to correct, and note_round above
# already carries every failed tool's own words into the injected message —
# the first-failure hint line and the dead-tool line both print _clip(err)
# verbatim. Nothing else called it.
