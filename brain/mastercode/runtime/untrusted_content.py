"""runtime/untrusted_content.py — screen attacker-influenced text on its way
INTO a model's context.

THE PROBLEM (measured on a shipped release)
A coding harness's transcript reaches host and agent reasoning byte-identical.
A harness that reads a repo echoes file content into its transcript, so
"build me something from this repo" with a poisoned README is enough to put
`</system>`, `SYSTEM:` and "ignore all previous instructions" into the next
model call. Measured: `verbatim=True, payload_intact=True`.

The platform already had the answer. `input_guard.sanitize` — run against that
exact payload — flags governance_evasion, instruction_override, persona_override
and role_break, and neutralizes the structural escapes. It was simply never
wired to this path: its call sites cover the user chat door, synaptive ingest,
teaching-memory writes and skill events. The door where a TOOL returns bytes
was not one of them.

WHERE THIS BELONGS, AND WHERE IT DOES NOT
Screen at MODEL-INGRESS, not at storage and not on the way to the user.

  harness -> harness_run (RAW)          storage keeps the truth
    -> review.py run stream (RAW)       the user must see real build output
    -> tool result -> model (SCREENED)  <- here, and only here

A real build transcript legitimately contains code that looks like a role
delimiter — JSON with a "role" key, a heredoc, a chat-template string in a test
fixture. Sanitizing storage or the run view would corrupt a developer's
artifact to defend a model, which is the wrong trade and breaks the rule that
controls protect the platform without throttling the user.

CONTRACT
Strip-and-flag, never block — inherited deliberately from input_guard. Hard
denial on a build transcript would be a false-positive wall against exactly the
legitimate case above. The structural escape is neutralized; the flags ride
along so the caller can SEE it happened. A silent screen and an absent screen
look identical from the operator's chair (the Class VIII lesson), so every
caller here surfaces `injection_flags` in its result.

SCOPE — WHAT IS WIRED AND WHAT IS NOT
Wired: the two harness->model paths (`_exec_harness_transcript`, and the
`build` success summary). NOT yet wired, and each is its own finding:
tool results generally (S1), the agent->host report (G-003/S2), external
fetches (S7), project file reads (S8), DB rows an agent reads (S9). This module
is the seam for those; do not read its existence as coverage of them.
"""
from __future__ import annotations

import logging

log = logging.getLogger("cseries.mastercode.untrusted_content")

# Hard ceiling on what we hand a screen. The regex set is linear, but a
# pathological multi-megabyte transcript should not become a latency event on
# the tool path; callers already truncate to their own display budget well
# below this.
MAX_SCREEN_CHARS = 200_000


def screen_for_model(text, *, source: str) -> tuple[str, list[str]]:
    """Neutralize structural injection in untrusted text bound for a model.

    Returns (screened_text, flags). Flags is empty when nothing tripped.

    Fails OPEN by design: if the screen itself raises, the caller still gets its
    content, tagged `screen_error`. A transcript-reading tool that starts
    returning nothing because a regex module failed to import would be a worse
    outage than the exposure it guards, and the flag makes the failure visible
    rather than silent.
    """
    if not text:
        return "", []
    if not isinstance(text, str):
        text = str(text)
    if len(text) > MAX_SCREEN_CHARS:
        text = text[:MAX_SCREEN_CHARS]
    try:
        from mastercode.runtime.input_guard import sanitize
        clean, flags = sanitize(text)
        flags = sorted(set(flags or []))
        if flags:
            log.warning(
                "UNTRUSTED_CONTENT_NEUTRALIZED source=%s flags=%s bytes=%d",
                source, flags, len(text),
            )
        return clean, flags
    except Exception as e:                                     # noqa: BLE001
        log.warning("untrusted_content: screen failed for source=%s: %s", source, e)
        return text, ["screen_error"]


__all__ = ["screen_for_model", "MAX_SCREEN_CHARS"]
