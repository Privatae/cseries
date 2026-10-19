"""summary_policy.py — where a rolling summary of what overflowed rides.

A Circles conversation (the original conversation and every topic thread)
carries, ahead of its history, a summary of the turns that no longer fit the
window, written by the turn itself after it ends
(thread_consolidator.maybe_consolidate: only when the history is near its
budget or 60 turns have passed, never twice at once for one topic). Nothing
else writes a summary: the background batch consolidator and its loop are
gone. Never for the root thread: its context is an allowlist (runtime/root_turn).
"""


def summary_on(src_ctx: dict | None, *, root: bool = False) -> bool:
    """Does a rolling summary ride this scope's history?"""
    if root or not src_ctx:
        return False
    kind, ref = src_ctx.get("kind"), str(src_ctx.get("ref") or "")
    return kind == "thread" or (kind == "app" and ref == "circles")


def summary_written_in_turn(src_ctx: dict | None, *, root: bool = False) -> bool:
    """Whether the turn asks for this scope's summary to be rewritten after
    it ends. The same question as summary_on: the turn is the only writer."""
    return summary_on(src_ctx, root=root)
