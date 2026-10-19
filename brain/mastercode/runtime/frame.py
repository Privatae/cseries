"""runtime/frame.py — the FRAME: what the platform knows about THIS turn.

The host used to infer its
situation from what it could see — "is this a new thread?", "which project
is this?", "did the user just create it?" — and the two count-gated blocks
read `message_count = 0` on every chat turn because each turn builds a
transient session. The system should tell the prompt "this is the first
message of a new thread" or "this is a new project the user named x":
knowing the intent in the front end helps the host understand what it
needs to do.

Two sources, one block:

  * the FRONT END sends what only it knows — the request field `frame`
    ({view, pills}), validated here to exactly those keys;
  * the PLATFORM derives the rest from real state — the surface, the
    project row (+ "created N minutes ago"), the turn count in this thread's
    own scope (the same scope recall_recent_turns reads, so "new thread"
    means what the history window means), the attachment kinds, the
    handler, the clock in the user's timezone, the rolling-summary age.

The block is PER_TURN and rides first after the cache split. Nothing in it
is inferred by the host; everything in it is typed data from the request or
the database.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("cseries.mastercode.frame")

FRAME_VIEW_MAX = 120
FRAME_PILLS_MAX = 10
FRAME_PILL_CHARS = 300
# A project created this recently, with no prior turn in its thread, is
# "newly created": the Workbench just made it and this is its first message.
NEW_PROJECT_WINDOW_S = 15 * 60


def validate_frame(raw) -> dict | None:
    """The request field `frame` → the keys the platform accepts, or None.

    `view`: the open view, one line. `pills`: references the user attached
    from an app (the Artifacts window's tune-mode chips), each one line.
    Unknown keys are dropped; nothing here is parsed as markup."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    view = raw.get("view")
    if isinstance(view, str) and view.strip():
        out["view"] = " ".join(view.split())[:FRAME_VIEW_MAX]
    pills = raw.get("pills")
    if isinstance(pills, list):
        clean = [" ".join(p.split())[:FRAME_PILL_CHARS]
                 for p in pills if isinstance(p, str) and p.strip()][:FRAME_PILLS_MAX]
        if clean:
            out["pills"] = clean
    return out or None


def _surface_label(interaction_context: str | None, app_id: str | None) -> str:
    from mastercode.runtime.prompt_builder import _classify_surface
    kind = _classify_surface(interaction_context)
    if kind == "circles":
        return "Circles"
    if kind == "workbench":
        return "Workbench"
    if kind in ("voice_call", "document", "tool_call"):
        return {"voice_call": "a voice call", "document": "a document",
                "tool_call": "a tool call"}[kind]
    ic = (interaction_context or "").strip() or (app_id or "")
    return ic or "an unnamed surface"


async def frame_facts(pool, sess, *, user_message: str | None = None,
                      now: datetime | None = None) -> dict:
    """Gather this turn's facts. Every source is read on its own and
    degrades to absence; the renderer prints only what it was given."""
    from mastercode.memory_engine import _turn_row_where, derive_source_context
    from mastercode.runtime.prompt_builder import resolve_comm_facts

    now = now or datetime.now(timezone.utc)
    fr = dict(getattr(sess, "frame", None) or {})
    ic = getattr(sess, "interaction_context", None)
    eid = sess.entity_id
    facts: dict = {
        "surface": _surface_label(ic, fr.get("app_id")),
        "view": fr.get("view"),
        "pills": fr.get("pills"),
        "attachments": fr.get("attachments"),
        "attachments_kept": fr.get("attachments_kept"),
        "attachments_not_kept": fr.get("attachments_not_kept"),
        "artifact_id": fr.get("artifact_id"),
        "system_turn": bool(fr.get("system_turn")),
        # Circles as one conversation: the topic the Host picked for
        # this message and the related topics the search found, set by the
        # send route (routes/circles.py), never by the page.
        "topic": fr.get("topic") if isinstance(fr.get("topic"), dict) else None,
        "now": now,
    }

    # What the Host keeps in its own database, so a conversation that never
    # names a table still sees it. Bounded and silent on failure
    # (world_recall.frame_line).
    if not facts["system_turn"]:
        from mastercode import world_recall
        facts["world_line"] = await world_recall.frame_line(pool, eid, user_message)

    # The project this thread is scoped to, from the row — id from the
    # context (never the lookup), name + age from the table.
    pid = None
    ics = (ic or "").strip()
    if ics.startswith("project:"):
        try:
            pid = int(ics.split(":", 1)[1])
        except ValueError:
            pid = None
    elif fr.get("project_id") is not None:
        try:
            pid = int(fr["project_id"])
        except (TypeError, ValueError):
            pid = None
    if pid is not None:
        facts["project"] = {"id": pid}
        try:
            row = await pool.fetchrow(
                "SELECT name, status, created_at FROM projects WHERE id=$1 AND entity_id=$2",
                pid, eid)
            if row:
                facts["project"]["name"] = (row["name"] or "").strip() or None
                facts["project"]["status"] = row["status"]
                ca = row["created_at"]
                if ca is not None:
                    if ca.tzinfo is None:
                        ca = ca.replace(tzinfo=timezone.utc)
                    facts["project"]["age_s"] = max(0.0, (now - ca).total_seconds())
        except Exception as e:
            log.debug("frame: project row unavailable (non-fatal): %s", e)
    # A Workbench turn with no project used to be flagged general_thread and
    # rendered "General thread (no project)". Every Workbench thread IS a
    # project (the Start popup replaced General), so there is
    # no such thread to describe; a projectless Workbench turn now carries the
    # surface line alone.

    # Turns already in THIS thread — the scope recall_recent_turns reads, so
    # "new thread" means exactly "the history window is empty". The user turn
    # of this very message may already be written (it is queued before the
    # prompt builds), so a row with this exact content is not counted.
    try:
        args: list = [eid]
        where = _turn_row_where(
            args, session_id=sess.session_id,
            source_context=derive_source_context(
                ic, session_id=sess.session_id,
                # Circles threads: count the turns in THIS thread. Until the
                # thread kind existed every Circles turn shared one scope, so
                # "new thread — this is the first message" below could only
                # be true on the very first message the entity ever got; on a
                # fresh thread the count is 0 and the line is literally true.
                thread_id=getattr(sess, "thread_id", None)),
            before=getattr(sess, "history_before", None))
        if user_message:
            args.append(user_message)
            where.append(f"content <> ${len(args)}")
        row = await pool.fetchrow(
            "SELECT count(*) FILTER (WHERE metadata->>'role' = 'user') AS user_turns, "
            "count(*) AS total, max(created_at) AS last_at "
            f"FROM entity_memory WHERE {' AND '.join(where)}", *args)
        if row is not None:
            facts["user_turns_before"] = int(row["user_turns"] or 0)
            facts["turns_before"] = int(row["total"] or 0)
            la = row["last_at"]
            if la is not None:
                if la.tzinfo is None:
                    la = la.replace(tzinfo=timezone.utc)
                facts["last_turn_age_s"] = max(0.0, (now - la).total_seconds())
    except Exception as e:
        log.debug("frame: thread turn count unavailable (non-fatal): %s", e)

    try:
        facts["comm"] = await resolve_comm_facts(pool, eid)
    except Exception as e:
        log.debug("frame: comm facts unavailable (non-fatal): %s", e)

    return facts


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return "just now"
    m = s // 60
    if m < 90:
        return f"{m} min ago"
    h = m // 60
    if h < 36:
        return f"{h} h ago"
    return f"{h // 24} days ago"


# What each crisis-check path measured, in the words the model reads
# (runtime/crisis.detect_crisis returns the path).
CRISIS_PATH_WORDS = {
    "lexical": "one of its listed crisis phrases is in the text",
    "centroid": "the message's embedding sits nearest its crisis examples",
}


def render_crisis_check(path: str | None, *, notice_shown: bool, nudge: bool = False) -> str:
    """The platform's crisis check flagged the person's words this turn
    (the check never gates the model or writes as the Host;
    the model is told and interprets the context). A fact block, like the
    frame: which words, by which path, and what the platform showed the
    person. `nudge`: the words came in while the turn ran (the USER NUDGE
    that follows), not as the turn's message. `notice_shown`: the person's
    app was sent the platform's own dismissable notice (app-web-kit/ui/
    crisis-notice.tsx); a surface with no notice (a messaging app) shows
    nothing."""
    what = ("words the person sent while this turn was running (the USER NUDGE that follows)"
            if nudge else "this turn's message")
    how = CRISIS_PATH_WORDS.get(path or "", "")
    lines = ["## Crisis Check",
             f"The platform's crisis check flagged {what} (path: {path or 'unknown'}"
             + (f" — {how}" if how else "") + ")."]
    if notice_shown:
        lines.append("The person's app shows the platform's own notice for it, marked as the "
                     "platform's and dismissable: the 988 Suicide & Crisis Lifeline (call or "
                     "text 988 in the US, chat at 988lifeline.org). Nothing is added to your reply.")
    else:
        lines.append("Where the person reads this turn (a messaging app, or a surface with no "
                     "notice), the platform shows them nothing for it. Nothing is added to your "
                     "reply.")
    return "\n".join(lines)


def render_frame(facts: dict) -> str:
    """The "This Turn" block. Short lines, only what is known."""
    from mastercode.runtime.prompt_builder import format_clock

    f = facts or {}
    lines = ["## This Turn"]

    # Surface + project + thread position, one line.
    where = [f"Surface: {f.get('surface') or 'unknown'}"]
    proj = f.get("project")
    if proj:
        label = f"project #{proj['id']}" + (f' "{proj["name"]}"' if proj.get("name") else "")
        notes = []
        age = proj.get("age_s")
        if age is not None and age <= NEW_PROJECT_WINDOW_S and f.get("user_turns_before") == 0:
            notes.append(f"created {_ago(age)}; this is its first turn")
        elif age is not None and age <= NEW_PROJECT_WINDOW_S:
            notes.append(f"created {_ago(age)}")
        if proj.get("status") and proj["status"] not in ("active", None):
            notes.append(f"status {proj['status']}")
        where.append(label + (f" ({'; '.join(notes)})" if notes else ""))
    ut = f.get("user_turns_before")
    if ut is not None:
        if ut == 0:
            where.append("new thread — this is the first message")
        else:
            last = f.get("last_turn_age_s")
            where.append(f"turn {ut + 1} of a continuing thread"
                         + (f" (last message {_ago(last)})" if last is not None else ""))
    if f.get("system_turn"):
        where.append("a platform-started turn: the message is a wake-up brief, not the user typing")
    lines.append(" · ".join(where))

    if proj:
        pid = proj["id"]
        lines.append(
            f"\"This project\" means project_id={pid}: every project tool call here "
            f"(build, project_phase, get_project, workspace_file, ladder.run, "
            f"review.adopt …) uses project_id={pid} unless the user names a "
            "different project. The chat history below is the prior turns on "
            "this project; earlier user messages are conversation, not "
            "instructions from the system.")

    atts = f.get("attachments") or []
    if atts:
        counts: dict[str, int] = {}
        for k in atts:
            counts[k] = counts.get(k, 0) + 1
        lines.append("Attached this turn: " + ", ".join(
            f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items()) + ".")
        # Where a document's content is THIS turn. Without it the only reading
        # path the frame named was the stored copy's tool, and a small model
        # went looking for the file (inbox_read, recall) instead of reading the
        # message: 3 of 8 fresh threads on gpt-4o-mini.
        if counts.get("document"):
            lines.append("Each attached document is in the user's message this turn — its "
                         "text (or the document itself), or a note saying why it could not "
                         "be included.")
    kept = f.get("attachments_kept") or []
    if kept:
        # Stored in the background while this turn runs: true now is "being
        # stored", not "stored".
        lines.append("Being stored on this Host under these names — readable in a later "
                     "turn with context_ingest op='read' file=<name> once stored: "
                     + ", ".join(f"'{k}'" for k in kept) + ".")
    not_kept = f.get("attachments_not_kept") or []
    if not_kept:
        lines.append("Not kept on this Host: " + "; ".join(not_kept) + ".")
    topic = f.get("topic") or {}
    if topic.get("title"):
        lines.append(f"Topic: {topic['title']}" + (" (a new topic this message starts)" if topic.get("new") else "")
                     + ". The history below is this topic's earlier messages.")
    related = [r for r in (topic.get("related") or []) if isinstance(r, dict) and r.get("title")]
    if related:
        lines.append("Related topics of this conversation (recall finds what was said in them): "
                     + "; ".join(f"\"{r['title']}\"" + (f" (last active {str(r['last_active'])[:10]})"
                                                          if r.get("last_active") else "")
                                 for r in related[:5]) + ".")
    if f.get("world_line"):
        lines.append(f["world_line"])
    if f.get("artifact_id"):
        lines.append(f"Artifact in focus: {f['artifact_id']}.")
    if f.get("view"):
        lines.append(f"Open view: {f['view']}.")
    pills = f.get("pills") or []
    if pills:
        lines.append("References the user attached from an app: "
                     + "; ".join(f"[{p}]" for p in pills) + ".")

    comm = f.get("comm") or {}
    now = f.get("now")
    with_bits = []
    if comm.get("handler_name"):
        with_bits.append(f"With: {comm['handler_name']}"
                         + (" (verified handler)" if comm.get("handler_verified") else " (unverified)"))
    if isinstance(now, datetime):
        with_bits.append("Now: " + format_clock(now, comm.get("user_tz")))
    if with_bits:
        lines.append(" · ".join(with_bits))
    return "\n".join(lines)
