"""Lines a turn adds when a reply comes back empty, and the crisis notice.

Moved out of runtime/session.py, which re-exports every name defined here."""

import logging

log = logging.getLogger("cseries.mastercode.entity_runtime")


# The exits past the turn's loop (send_message): the forced text
# and the retry carry `escalate` alone, and are told so.
_EXIT_BOX_LINE = (
    "The one exception is `escalate`, the question box: if a real choice remains "
    "that only the user can make, put it there with the options. Never ask the "
    "user in your reply whether to go on.")

# The ask after a call that came back with no reply. Inside the tool loop it
# goes with the turn's own tools; past the loop (after an allowance stop or the
# round backstop) with the box alone, plus _EXIT_BOX_LINE.
_EMPTY_RETRY_LINE = ("Please respond to the conversation above. "
                     "Do not repeat any working memory blocks.")


def _empty_shape(res) -> dict:
    """What an empty completion returned, for empty_reply_notice."""
    res = res if isinstance(res, dict) else {}
    return {"reasoning": bool(res.get("reasoning")), "usage_reported": res.get("usage_reported")}


def empty_reply_notice(shapes: list) -> str:
    """The stall notice, worded by what the empty calls actually returned:
    `shapes` is one {reasoning, usage_reported} per empty completion. A user
    was once told "the model returned its reasoning" when
    both calls came back with no reasoning and no usage at all."""
    tail = " twice in a row. Nothing was lost and no work was undone."
    if any(s.get("reasoning") for s in shapes):
        return "I hit a snag — the model returned its reasoning but no actual reply," + tail
    if shapes and all(s.get("usage_reported") is False for s in shapes):
        return ("I hit a snag — the model's call came back empty, with no reply and no "
                "usage reported," + tail)
    return "I hit a snag — the model's call came back with no reply," + tail


# Governance floor (SB 243 §22602(b)(1)): what BOTH crisis checks
# do on a hit (entry-side, and the mid-loop nudge drain). The check once
# answered "remove this thread completely" with the 988 referral in place of
# the Host's reply, so: a hit never gates the model and never
# writes or streams text as the Host. The model is told, in that turn's
# context (frame.render_crisis_check), and interprets it; the person's app
# gets the platform's own dismissable notice with the crisis resources,
# through the `governance` progress frame (kind "crisis"); the content-free
# governance event row stays for the audit.

# The surfaces whose app draws the platform's crisis notice
# (app-web-kit/ui/crisis-notice.tsx): Circles' chat and the Workbench.
CRISIS_NOTICE_SURFACES = frozenset({"circles", "workbench"})


def _crisis_notice_shown(sess, progress_cb, words: list[str]) -> bool:
    """Whether the platform's notice reaches a screen that draws it: a turn
    with a progress stream, on a surface whose app draws the `governance`
    frame. Words a messaging connector (Telegram, Slack, Discord) carried in
    run in the person's Circles thread, but the person reads the reply in the
    messaging app, which posts text only — nothing is shown there."""
    if progress_cb is None:
        return False
    try:
        from server.connectors.turns import sent_words
        if any(sent_words(w) is not None for w in words):
            return False
    except Exception as _sw_err:  # noqa: BLE001 — unknown origin: the surface decides
        log.debug("connector words check failed (non-fatal): %s", _sw_err)
    from mastercode.runtime.prompt_builder import _classify_surface
    return _classify_surface(getattr(sess, "interaction_context", None)) in CRISIS_NOTICE_SURFACES


async def _crisis_flagged(runtime, sess, progress_cb, path, words: list[str], *, nudge: bool) -> str:
    """One crisis-check hit: the content-free audit row, the person's notice
    frame, and the fact block for the model (returned; the caller puts it in
    the turn's context). Each step is fail-soft on its own: a failed audit
    write or frame never costs the model its fact."""
    try:
        from mastercode.memory_engine import write_governance_event
        await write_governance_event(runtime.pool, sess.entity_id, sess.session_id, "crisis_gate")
    except Exception as _gov_err:  # noqa: BLE001
        log.error("governance event write failed (non-fatal): %s", _gov_err)
    if progress_cb is not None:
        try:
            await progress_cb({"event": "governance", "kind": "crisis"})
        except Exception as _cb_err:  # noqa: BLE001
            log.debug("progress_cb governance raised (non-fatal): %s", _cb_err)
    from mastercode.runtime.frame import render_crisis_check
    block = render_crisis_check(
        path, notice_shown=_crisis_notice_shown(sess, progress_cb, words), nudge=nudge)
    try:
        from mastercode.runtime.prompt_schema import log_injected_block
        await log_injected_block(runtime, sess, "crisis_check", block)
    except Exception as _lib_err:  # noqa: BLE001
        log.debug("crisis check provenance log failed (non-fatal): %s", _lib_err)
    return block
