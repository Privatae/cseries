"""
Brain API Routes — Local entity brain endpoints.

These routes connect the dashboard to the embedded brain.
All brain state lives in the local PostgreSQL instance.
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from server import brain_service, secret_cut
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.brain")

router = APIRouter(prefix="/api/brain", tags=["brain"])


def _memory_audience(request: Request):
    """Derive the memory read audience from the calling app's token (C7a).

    read:brain_memory gates the endpoint; this decides WHAT the holder sees:
      - No app token  -> the in-container runtime / INTERNAL_API_SECRET loopback
        (it cleared requires_permission without a token). Full corpus.
      - Kernel-native app (NATIVE_APPS) or an app holding read:brain_memory_all:
        full corpus (the explicit full-corpus grant the trust base / memory apps
        opt into).
      - Any other app: AUDIENCE-SCOPED — it sees only memories whose
        metadata.share_scope contains `app:<app_id>` (memory_scope.filter_clause).
        Untagged memories are handler-only and never surface to an app.

    Returns None for the full-corpus case (search_memory treats None as handler /
    no filter) or a memory_scope.Audience for the scoped case.
    """
    tok = getattr(request.state, "app_token", None)
    if tok is None:
        return None
    perms = set(getattr(tok, "permissions", None) or [])
    app_id = getattr(tok, "app_id", "") or ""
    try:
        from server.applications.kernel import NATIVE_APPS
    except Exception:
        NATIVE_APPS = frozenset()
    if app_id in NATIVE_APPS or "read:brain_memory_all" in perms:
        return None
    from server.applications.memory_scope import Audience
    return Audience(kind="app", caller_entity_id=None, allowed_scopes=[f"app:{app_id}"])


@router.get("/status", dependencies=[Depends(requires_permission("read:brain_state"))])
async def brain_status():
    """Brain health check."""
    runtime = brain_service.get_runtime()
    pool = brain_service.get_pool()
    return {
        "initialized": runtime is not None,
        "pool_active": pool is not None and not pool._closed if pool else False,
        "entity_id": brain_service._entity_id,
    }


@router.get("/sessions", dependencies=[Depends(requires_permission("read:brain_sessions"))])
async def list_sessions(
    limit: int = Query(50, le=200),
    context: str | None = Query(None, description="filter by interaction_context (e.g. 'project:7')"),
    status: str | None = Query(None, description="filter by status (e.g. 'active')"),
    offset: int = Query(0, ge=0, description="rows to skip — page past the first `limit`"),
):
    """List recent chat sessions, optionally filtered.

    The workspace app calls this with `?context=project:<id>&status=active` so
    reopening a project picks up the prior conversation off server truth
    instead of relying on the browser's localStorage cache (which can be
    cleared, replaced, or out of sync across windows).
    """
    return await brain_service.list_sessions(limit=limit, context=context, status=status,
                                             offset=offset)


@router.post("/sessions", dependencies=[Depends(requires_permission("write:session"))])
async def create_session(
    request: Request,
):
    """Create a new brain chat session."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    context = body.get("context", "chat")
    channel = body.get("channel", "chat")
    user_id = body.get("user_id")

    session = await brain_service.create_session(
        context=context,
        channel=channel,
        user_id=user_id,
    )
    return session


@router.get("/sessions/{session_id}", dependencies=[Depends(requires_permission("read:brain_sessions"))])
async def get_session(session_id: str):
    """Get session details."""
    session = await brain_service.resume_session(session_id)
    if not session:
        return {"error": "Session not found"}, 404
    return session


@router.get("/sessions/{session_id}/messages", dependencies=[Depends(requires_permission("read:session"))])
async def get_session_messages(session_id: str):
    """Return the chat history for a session so the UI can reload it
    on resume / refresh. Messages are filtered to {role, content} pairs
    for user + assistant turns only (system + tool entries are pruned
    server-side).
    """
    msgs = await brain_service.get_session_messages(session_id)
    if msgs is None:
        from fastapi import HTTPException, status
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return {"session_id": session_id, "messages": msgs}


# _resolve_session_for_project is removed: it was dead once the
# no-sessions chat path (/api/brain/messages) stopped calling it — the
# kernel builds a transient context per request from entity_registry
# directly. No callers remain.


@router.post("/messages", dependencies=[Depends(requires_permission("write:session"))])
async def send_message_session_free(request: Request):
    """No-sessions write path. The Workbench and any other client should call
    this instead of /api/brain/sessions/{session_id}/messages.

    Body: {message, project_id?, attachments?, artifact_id?, effort?}
    Internally resolves the write-target session from (entity_id, project_id)
    — the session is a kernel-side grouping primitive only; the client
    never sees a session_id, never owns one, never gets a "Session not
    found" error.

    Why this exists: the old `/sessions/{session_id}/messages` route
    forced clients to lookup-or-create a session and pass its UUID in the
    URL. Stale sessions surfaced as `{'error': 'Session ... not found'}`
    every time a user reopened a project older than ~a day. Per the
    no-sessions architecture this route makes that whole class of bugs
    impossible.
    """
    body = await request.json()
    message = body.get("message", "")
    attachments = body.get("attachments")
    if not message.strip() and not attachments:
        return {"error": "Message or attachments required"}, 400

    app_token = getattr(request.state, "app_token", None)
    app_id = getattr(app_token, "app_id", None) if app_token else None
    app_permissions: list[str] | None = None
    if app_token is not None:
        perms = getattr(app_token, "permissions", None)
        if perms:
            app_permissions = list(perms)

    project_id = body.get("project_id")
    try:
        project_id = int(project_id) if project_id is not None else None
    except (TypeError, ValueError):
        project_id = None
    artifact_id = body.get("artifact_id") or None
    # The FRAME (host prompt map phase 4): what the front end knows about
    # this turn — `view` (the open view, ≤120 chars) and `pills` (references
    # the user attached from an app, ≤10). Validated to those keys; anything
    # else is dropped. Rendered into the host's per-turn "This Turn" block.
    from mastercode.runtime.frame import validate_frame
    frame = validate_frame(body.get("frame"))
    # The sender's id for these words (optional), the same on the Workbench
    # send's silent retry as on its first attempt: a retry whose words the
    # server already holds (pending_words) never delivers them a second time
    # (brain_service.send_message, pending_words.claim_resend).
    client_id = body.get("client_id")
    if client_id is not None:
        from mastercode.runtime.session import nudge_id_ok
        if not nudge_id_ok(client_id):
            raise HTTPException(400, "client_id: 1-64 of A-Z a-z 0-9 _ -")

    pool = brain_service.get_pool()
    if pool is None:
        from fastapi import HTTPException as _HE
        raise _HE(503, "Brain pool not ready")
    entity_id = brain_service._entity_id

    # A per-thread EFFORT pick from the chat box (the Workbench composer):
    # this turn only, never persisted, checked against the chat route's model
    # by the Orchestrator's own rule (inference.check_thread_effort) before
    # anything of the turn happens. It rides thread_override, the same field a
    # Circles thread's pick rides (inference_config.resolve_role_route).
    thread_override = None
    effort = str(body.get("effort") or "").strip()
    if effort:
        from server.routes.inference import check_thread_effort
        _eff_err = await check_thread_effort(effort)
        if _eff_err:
            raise HTTPException(400, _eff_err)
        thread_override = {"effort": effort}

    # No-sessions architecture: pass entity_id directly.
    # The brain builds a transient per-request chat context — no
    # `entity_sessions` row is read or written. The chat log is the
    # `entity_memory` turn rows tagged by project_id + app_id.
    #
    # Forward the Accept
    # header so brain_service picks the right SSE wire format. Clients
    # asking for `text/x-vercel-ai-data-stream` get the AI SDK Data
    # Stream Protocol (line prefixes 0:/9:/a:/d:/e:/3:); everyone else
    # gets the legacy `data: {"type":"token",...}` format. When the
    # client opted in we also surface the protocol version via the
    # `x-vercel-ai-data-stream: v1` response header.
    accept = request.headers.get("accept", "") or ""
    is_new_proto = "text/x-vercel-ai-data-stream" in accept.lower()
    response_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    if is_new_proto:
        response_headers["x-vercel-ai-data-stream"] = "v1"
    return StreamingResponse(
        brain_service.send_message(
            entity_id=entity_id,
            message=message,
            attachments=attachments,
            app_id=app_id,
            project_id=project_id,
            artifact_id=artifact_id,
            app_permissions=app_permissions,
            accept=accept,
            frame=frame,
            thread_override=thread_override,
            **({"client_id": client_id} if client_id else {}),
        ),
        media_type="text/event-stream",
        headers=response_headers,
    )


# POST /sessions/{session_id}/messages is removed: the legacy explicit-
# session chat-write route. Only the codebook A/B benchmark used it
# (migrated to POST /api/brain/messages). Workbench and Circles do not
# use it.


# ── Detached turns: the Workbench-side visibility surface ───────────────
# Same registry + wire contract as /api/circles/turns/* — see that block and
# server/turn_registry.py for the doctrine (no hidden processes; the
# connection never owns the turn). Thread identity here is the project when
# there is one (whichever app started its turn), else the calling app's own
# conversation — compose_thread_key, exactly as POST /messages composes it.

# The detached-turn registry. A turn's frames ARE the conversation —
# the live form of what GET /chat returns from the log — so reading them takes the
# same scope /chat does (read:brain_memory). Stopping one takes write:session,
# the scope that starts one (POST /messages). All three were once
# unscoped: any app could list every running turn and tail its output.
@router.get("/turns/active", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def brain_turn_active(request: Request, project_id: int | None = None) -> dict:
    from server.turn_registry import compose_thread_key, get_turn_registry
    app_token = getattr(request.state, "app_token", None)
    app_id = getattr(app_token, "app_id", None) if app_token else None
    eid = brain_service._entity_id
    reg = get_turn_registry()
    rec = reg.active_for(compose_thread_key(eid, app_id, project_id))
    return {"ok": True, "turn": rec.describe() if rec else None,
            "turns": reg.list_turns(eid)}


@router.get("/turns/{turn_id}/stream", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def brain_turn_stream(turn_id: str, from_seq: int = 0):
    from server.brain_service import _never_silent
    from server.turn_registry import get_turn_registry
    rec = get_turn_registry().get(turn_id)
    if rec is None:
        raise HTTPException(404, "unknown turn (finished more than ~15min ago?)")
    return StreamingResponse(
        _never_silent(get_turn_registry().tail(
            rec, mode="reattached", from_seq=max(0, from_seq))),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-data-stream": "v1",
        },
    )


@router.post("/turns/{turn_id}/nudge", dependencies=[Depends(requires_permission("write:session"))])
async def brain_turn_nudge(turn_id: str, request: Request) -> dict:
    """The person's words into the running turn — the composer's Send while
    the turn holds. The words go in
    whole, like every nudge. 409 when the turn has ended, or has stopped
    reading: the client sends it as the next message instead, which starts a
    turn of its own once that one is done (turn_registry.start_or_attach).

    The server owns the words from here, as the Circles nudge route does:
    they are written to the Host's database
    (pending_words) BEFORE this answers `held: true`, and from then on the
    turn reads them, or they start the thread's next turn when it ends
    without reading them, or, after a restart, when the Host is back. The
    page keeps no copy; it reads what is still held (GET /api/brain/pending).
    A write that fails answers 503 and the page keeps its words. In PII mode
    nothing is written (raw text at rest is what that mode forbids): the
    answer says `held: false`, and the page keeps its copy until the turn's
    nudge_read receipt and sends it as the next message when none comes."""
    from mastercode import pending_words as _pw
    from mastercode.runtime.session import nudge_words
    from server.turn_registry import get_turn_registry
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    # Whole: the person's words are the
    # input; this route once cut them at 2,000 characters with nothing said.
    text = str((body or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    # The sender's id for these words (optional): the nudge frame, the turn's
    # nudge_read receipt and the saved row echo it, so the sender matches its
    # own words by it, never by their text.
    client_id = (body or {}).get("client_id") if isinstance(body, dict) else None
    if client_id is not None:
        from mastercode.runtime.session import nudge_id_ok
        if not nudge_id_ok(client_id):
            raise HTTPException(400, "client_id: 1-64 of A-Z a-z 0-9 _ -")
        # Marked with the app that sent it (the window's token): the id says
        # the words were delivered for that app only (pending_words.SentId).
        _tok = getattr(getattr(request, "state", None), "app_token", None)
        client_id = _pw.sent_ids(client_id, getattr(_tok, "app_id", None) if _tok else None)
    reg = get_turn_registry()
    rec = reg.nudgeable(turn_id)
    if rec is None:
        raise HTTPException(409, "the turn has ended")
    # A running root turn takes only the person's own words
    # (runtime/root_turn.words_refusal): write:session alone is not the
    # person, an app may hold it. Refused before anything is held, so no
    # pending_words row carries words the root thread will not take.
    if isinstance(rec.room, dict) and rec.room.get("thread_id"):
        from mastercode.runtime import root_turn as _rt
        _why = await _rt.words_refusal(brain_service.get_pool(), rec.entity_id,
                                       rec.room["thread_id"],
                                       person=_rt.person_request(request))
        if _why:
            raise HTTPException(403, _why)
    # Held before anything answers: the words as the turn reads them (secret
    # shapes cut), in the room of the turn they were typed into, so a turn of
    # their own can run there if that turn never reads them or the Host
    # restarts first.
    key = None
    try:
        key = await _pw.hold(
            brain_service.get_pool(), rec.entity_id, rec.session_id or f"turn|{rec.turn_id}",
            nudge_words(text), client_id=client_id,
            room=dict(rec.room) if isinstance(rec.room, dict) else None, thread_key=rec.thread_key)
    except Exception as e:  # noqa: BLE001
        logger.error("nudge: the words could not be held (turn %s): %s", turn_id[:8], e)
        raise HTTPException(503, "Your words could not be saved on your Host; "
                                 "send them again as a message.") from None
    if not reg.nudge(turn_id, text, client_id=client_id, **({"pending_key": key} if key else {})):
        # The turn stopped reading while the words were written: the page
        # sends them as its next message, so nothing holds them here.
        if key:
            try:
                await _pw.settle(brain_service.get_pool(), key)
            except Exception as e:  # noqa: BLE001
                logger.error("nudge: a refused nudge's row was not removed: %s", e)
        raise HTTPException(409, "the turn has ended")
    return {"ok": True, "turn_id": turn_id, "queued_chars": len(text),
            # The server holds the words (pending_words): the page keeps no
            # copy. False where nothing is written (PII mode).
            "held": bool(key)}


@router.get("/pending", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def brain_pending(request: Request, project_id: int | None = None) -> dict:
    """The words the server holds for one Workbench page (pending_words): sent
    into a running turn and not yet read by a turn. The page draws them as
    sent and waiting, after a reload too; they leave this list when a turn
    has them. The page is the conversation /turns/active names
    (compose_thread_key, as POST /messages composes it)."""
    from mastercode import pending_words as _pw
    from server.turn_registry import compose_thread_key
    app_token = getattr(request.state, "app_token", None)
    app_id = getattr(app_token, "app_id", None) if app_token else None
    eid = brain_service._entity_id
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": True, "words": []}
    return {"ok": True, "words": await _pw.held_for(pool, eid, compose_thread_key(eid, app_id, project_id))}


@router.post("/turns/{turn_id}/cancel", dependencies=[Depends(requires_permission("write:session"))])
async def brain_turn_cancel(turn_id: str) -> dict:
    from server.turn_registry import get_turn_registry
    out = await get_turn_registry().cancel(turn_id)
    if not out.get("ok"):
        raise HTTPException(404, out.get("error", "unknown turn"))
    return out


# ── Chat surfacing — read-side composition over existing tags ────────
#
# The chat log lives in entity_memory.turn. Every surface (Workbench,
# Circles, future apps) writes through the existing pipeline; per-row
# tags (project_id on the column; app_id + cc_session_id + artifact_id
# in the metadata JSONB or via the chat_events audit join) carry the
# context. This endpoint composes filtered slices from those existing
# tags — no schema changes, no new tables, no "session" word in the
# new code path.
#
# Each app's chat bubble queries this endpoint on context change
# (e.g. when the user clicks a different project in the Workbench) and
# rebuilds its local cache from the result. The backend doesn't
# partition; the UI does.

@router.get("/chat",
            dependencies=[Depends(requires_permission("read:brain_memory"))])
async def chat_history(
    project_id: int | None = Query(None, description="filter by project_id"),
    app_id: str | None = Query(None, description="filter by app id (joins chat_events)"),
    cc_thread_id: str | None = Query(None, description="filter by SDK conversation handle"),
    artifact_id: str | None = Query(None, description="filter by artifact id in metadata"),
    sctx_kind: str | None = Query(None, description="filter by source_context kind (project|app|peer|session)"),
    sctx_ref: str | None = Query(None, description="filter by source_context ref (the thread id within kind)"),
    since: str | None = Query(None, description="ISO timestamp lower bound"),
    until: str | None = Query(None, description="ISO timestamp upper bound (inclusive) — with `since`, slices one conversation episode out of a thread"),
    limit: int = Query(50, ge=1, le=200),
    order: str = Query("asc", regex="^(asc|desc)$",
                       description="asc for chat playback, desc for newest-first"),
):
    """Return filtered turn rows from the unified chat log.

    Joins entity_memory.turn ↔ entity_sessions ↔ chat_events to compose
    per-row filters out of the existing tag substrate. The api avoids
    the word 'session' in its parameters (chat_events.cc_session_id is
    surfaced as cc_thread_id, the SDK conversation handle).

    The underlying join uses the legacy session_id linkage; the new
    code path never names the result a 'chat session'.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"turns": [], "error": "pool not ready"}, 503

    where = ["em.entity_id = $1", "em.memory_type = 'turn'"]
    args: list = [brain_service._entity_id]

    if project_id is not None:
        args.append(project_id)
        where.append(
            f"(em.project_id = ${len(args)} "
            f"OR es.interaction_context = 'project:' || ${len(args)})"
        )
    if app_id is not None:
        args.append(app_id)
        clause = f"(ce.app_id = ${len(args)} OR em.metadata->>'app_id' = ${len(args)})"
        if project_id is None:
            # The GENERAL (no-project) app thread must not sweep in turns that
            # belong to specific projects — a project conversation in this app
            # is tagged app_id=<app> too, so without this the general thread
            # showed every project's messages. Mirror the
            # host-side recall scope (recall_recent_turns app branch requires
            # project_id IS NULL, memory_engine.py) so display == context.
            # Also exclude LEGACY project turns that carry no
            # project_id column and are tied to a project only via the session's
            # interaction_context='project:N'.
            clause = (
                f"({clause} AND em.project_id IS NULL "
                f"AND (es.interaction_context IS NULL "
                f"OR es.interaction_context NOT LIKE 'project:%'))"
            )
        if sctx_kind != "thread":
            # A Circles thread's turns carry app_id='circles' as well as their
            # own {kind:thread, ref} descriptor (session.py stamps the app_id
            # the caller passed whether or not a thread rode the body), so a
            # bare app_id match sweeps every thread into the app's stream.
            # This was the one reader left without the exclusion both arms of
            # memory_engine._turn_row_where carry (its app branch,
            # memory_engine.py:1146-1151, and its app_id branch, :1211);
            # only split-moved rows were protected, by the app_id strip.
            # Skipped when the caller ASKED for thread rows by descriptor —
            # then sctx_kind/sctx_ref below say which thread, and excluding
            # threads here would answer nothing.
            where.append(
                "em.metadata->'source_context'->>'kind' IS DISTINCT FROM 'thread'")
        where.append(clause)
    if cc_thread_id is not None:
        args.append(cc_thread_id)
        where.append(
            f"(ce.cc_session_id = ${len(args)} OR em.metadata->>'cc_thread_id' = ${len(args)})"
        )
    if artifact_id is not None:
        args.append(artifact_id)
        where.append(f"em.metadata->>'artifact_id' = ${len(args)}")
    if sctx_kind is not None:
        args.append(sctx_kind)
        where.append(f"em.metadata->'source_context'->>'kind' = ${len(args)}")
    if sctx_ref is not None:
        args.append(sctx_ref)
        where.append(f"em.metadata->'source_context'->>'ref' = ${len(args)}")
    # asyncpg types the $n::timestamptz slots as timestamptz and REQUIRES
    # datetime objects — a raw ISO string raises DataError (500). `since`
    # shipped with that latent bug (no caller ever passed it); the episode
    # slice is the first real user of these bounds.
    def _parse_ts(v: str):
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    _since_dt = _parse_ts(since) if since else None
    _until_dt = _parse_ts(until) if until else None
    if _since_dt is not None:
        args.append(_since_dt)
        where.append(f"em.created_at >= ${len(args)}")
    if _until_dt is not None:
        args.append(_until_dt)
        where.append(f"em.created_at <= ${len(args)}")

    args.append(limit)
    order_sql = "ASC" if order == "asc" else "DESC"
    # WINDOW = the NEWEST N turns, ALWAYS; `order` controls display direction
    # only. The previous single-level query wrote `ORDER BY em.id,
    # em.created_at {order_sql}` — with DISTINCT ON the direction bound to the
    # created_at TIEBREAKER, so output stayed em.id ASC and LIMIT took the
    # OLDEST N regardless of order=desc. On any thread longer than the limit
    # that HID the recent conversation (the "my conversation disappeared"
    # bug; reproduced on PG16: 100 rows, limit 5, order=desc → ids 1-5).
    # The inner subquery takes the newest N (em.id DESC, which DISTINCT ON
    # accepts as the leading key); the outer re-orders for display.
    sql = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (em.id)
                   em.id, em.content, em.created_at,
                   em.project_id, em.source_session,
                   em.metadata,
                   (em.metadata->>'role') AS role,
                   COALESCE(em.metadata->>'app_id', ce.app_id) AS app_id,
                   COALESCE(em.metadata->>'cc_thread_id', ce.cc_session_id) AS cc_thread_id,
                   em.metadata->>'artifact_id' AS artifact_id
              FROM entity_memory em
              LEFT JOIN entity_sessions es ON es.session_id = em.source_session
              LEFT JOIN chat_events     ce ON ce.session_id::text = em.source_session
             WHERE {' AND '.join(where)}
             ORDER BY em.id DESC
             LIMIT ${len(args)}
        ) newest
        ORDER BY newest.id {order_sql}
    """
    rows = await pool.fetch(sql, *args)

    def _meta(raw):
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    turns = [
        {
            "id": r["id"],
            "role": r["role"] or "user",
            "content": r["content"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "project_id": r["project_id"],
            "app_id": r["app_id"],
            "cc_thread_id": r["cc_thread_id"],
            "artifact_id": r["artifact_id"],
            "metadata": _meta(r["metadata"]),
        }
        for r in rows
    ]
    # DISTINCT ON re-sorts by id; restore the requested order client-side.
    turns.sort(key=lambda t: t["created_at"] or "", reverse=(order == "desc"))

    return {
        "turns": turns,
        "filter": {
            "project_id": project_id, "app_id": app_id,
            "cc_thread_id": cc_thread_id, "artifact_id": artifact_id,
            "since": since, "until": until, "order": order,
        },
        "limit": limit,
        "has_more": len(turns) == limit,
    }


# ── Thread context (conversation-depth) ──────────────────────────────────
# The View Context / budget-dial surface. One thread = one source_context
# {kind,ref}, derived EXACTLY the way the turn path derives it (a project
# chat is project:<id>; anything else is app:<app_id>) so what this panel
# shows is what the next turn will actually ship.

def _thread_src_ctx(app_id: str | None, project_id: int | None) -> dict:
    from mastercode.memory_engine import derive_source_context
    ic = f"project:{project_id}" if project_id is not None else (app_id or "chat")
    return derive_source_context(ic)


@router.get("/thread-context",
            dependencies=[Depends(requires_permission("read:brain_memory"))])
async def get_thread_context(
    app_id: str | None = Query(None),
    project_id: int | None = Query(None),
):
    """What the next turn on this thread will fire with: the model window,
    the budget in force (per-thread override or derived), the prompt-block
    composition from the latest prompt_log rows, the verbatim history
    preview, the rolling-summary state, and last turn's prefired memories."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"error": "pool not ready"}, 503
    from mastercode.context_budget import build_thread_context
    return await build_thread_context(
        pool, brain_service._entity_id,
        _thread_src_ctx(app_id, project_id),
    )


@router.put("/thread-context",
            dependencies=[Depends(requires_permission("write:session"))])
async def put_thread_context(request: Request):
    """Set (or clear with null) the per-thread context budget and the per-turn
    spend allowance. Persisted in
    behavioral_params.context_budgets["<kind>:<ref>"] and
    behavioral_params.turn_allowances["<kind>:<ref>"] — the same store and
    lifecycle as the per-thread model pick. The turn path reads them back via
    context_budget.read_thread_budget (:217, caps the derived budget) and
    context_budget.read_thread_allowance (governs the tool loop's spend).

    Each field is keyed on PRESENCE, not on value: a body carrying only one of
    them leaves the other untouched, and `null` is how you clear one. Without
    that the two dials would clobber each other — the panel saves them
    independently, and either save would wipe the other's value.

    No migration: behavioral_params is JSONB, so a new key is a write."""
    body = await request.json()
    budget = body.get("budget", None)
    if budget is not None:
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            raise HTTPException(400, "budget must be an int or null")
        if budget <= 0:
            raise HTTPException(400, "budget must be positive")
    allowance = body.get("turn_allowance_usd", None)
    if allowance is not None:
        try:
            allowance = float(allowance)
        except (TypeError, ValueError):
            raise HTTPException(400, "turn_allowance_usd must be a number or null")
        # Zero or less cannot pay for even the first model call, so it could
        # only ever mean "refuse to answer" — never what someone means by
        # typing a budget. Refused here rather than silently read as "unset"
        # later (read_thread_allowance), so the field can say so instead of
        # appearing to accept it.
        if allowance <= 0:
            raise HTTPException(400, "turn_allowance_usd must be positive "
                                     "(send null to clear it)")
    project_id = body.get("project_id")
    try:
        project_id = int(project_id) if project_id is not None else None
    except (TypeError, ValueError):
        project_id = None
    src_ctx = _thread_src_ctx(body.get("app_id"), project_id)
    scope = f"{src_ctx.get('kind')}:{src_ctx.get('ref')}"

    pool = brain_service.get_pool()
    if pool is None:
        return {"error": "pool not ready"}, 503
    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id=$1", eid)
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        bp = json.loads(bp)
    if not isinstance(bp, dict):
        bp = {}
    if "budget" in body:
        budgets = dict(bp.get("context_budgets") or {})
        if budget is None:
            budgets.pop(scope, None)
        else:
            budgets[scope] = budget
        bp["context_budgets"] = budgets
    if "turn_allowance_usd" in body:
        allowances = dict(bp.get("turn_allowances") or {})
        if allowance is None:
            allowances.pop(scope, None)
        else:
            allowances[scope] = allowance
        bp["turn_allowances"] = allowances
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params=$2::jsonb WHERE id=$1",
        eid, json.dumps(bp),
    )
    return {"ok": True, "scope": scope, "budget": budget,
            "turn_allowance_usd": allowance}


# The Host-wide bounded defaults (context_budget.read_turn_defaults): what a
# turn gets when no per-thread dial says otherwise. Each key is a decision the
# owner can change; null clears it back to the built-in.
_TURN_DEFAULT_KEYS = {
    "history_ceiling_tokens": int,          # 0 = fill the window (no ceiling)
    "turn_allowance_usd": float,            # dollars per turn
    "scheduled_task_allowance_usd": float,  # dollars per scheduled run
    "turn_rounds": int,                     # built-in default, full-context rounds; 0 = none
    "scheduled_rounds": int,
}


@router.get("/turn-defaults",
            dependencies=[Depends(requires_permission("read:brain_memory"))])
async def get_turn_defaults():
    """The Host's bounded defaults in force, what the owner set, and the
    built-in values."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"error": "pool not ready"}, 503
    from mastercode.context_budget import DEFAULT_HISTORY_CEILING_TOKENS, read_turn_defaults
    from mastercode.runtime.turn_allowance import DEFAULT_SCHEDULED_ROUNDS, DEFAULT_TURN_ROUNDS
    eid = brain_service._entity_id
    row = await pool.fetchrow("SELECT behavioral_params FROM entity_registry WHERE id=$1", eid)
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        bp = json.loads(bp)
    return {
        "in_force": await read_turn_defaults(pool, eid),
        "set": {k: bp.get(k) for k in _TURN_DEFAULT_KEYS if isinstance(bp, dict) and bp.get(k) is not None},
        "built_in": {"history_ceiling_tokens": DEFAULT_HISTORY_CEILING_TOKENS,
                     "turn_allowance_usd": None, "scheduled_task_allowance_usd": None,
                     "turn_rounds": DEFAULT_TURN_ROUNDS, "scheduled_rounds": DEFAULT_SCHEDULED_ROUNDS},
    }


@router.put("/turn-defaults",
            dependencies=[Depends(requires_permission("write:session"))])
async def put_turn_defaults(request: Request):
    """Set (or clear with null) the Host's bounded defaults. Keyed on
    presence, like the per-thread dial: a body naming one key leaves the
    others as they are."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    unknown = [k for k in body if k not in _TURN_DEFAULT_KEYS]
    if unknown:
        raise HTTPException(400, f"unknown keys: {unknown}; known: {sorted(_TURN_DEFAULT_KEYS)}")
    patch: dict = {}
    for key, cast in _TURN_DEFAULT_KEYS.items():
        if key not in body:
            continue
        if body[key] is None:
            patch[key] = None
            continue
        try:
            v = cast(body[key])
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a number or null")
        if v < 0 or (cast is float and v == 0):
            raise HTTPException(400, f"{key} must be positive"
                                + (" (0 is allowed: it turns that default off)" if cast is int else
                                   " (send null to clear it)"))
        patch[key] = v
    pool = brain_service.get_pool()
    if pool is None:
        return {"error": "pool not ready"}, 503
    eid = brain_service._entity_id
    row = await pool.fetchrow("SELECT behavioral_params FROM entity_registry WHERE id=$1", eid)
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        bp = json.loads(bp)
    if not isinstance(bp, dict):
        bp = {}
    for k, v in patch.items():
        if v is None:
            bp.pop(k, None)
        else:
            bp[k] = v
    await pool.execute("UPDATE entity_registry SET behavioral_params=$2::jsonb WHERE id=$1",
                       eid, json.dumps(bp))
    from mastercode.context_budget import read_turn_defaults
    return {"ok": True, "in_force": await read_turn_defaults(pool, eid)}


@router.get("/threads",
            dependencies=[Depends(requires_permission("read:brain_memory"))])
async def threads(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, description="rows to skip — page past the first `limit`"),
):
    """Source-driven threading: list this entity's memory threads.

    A "thread" is one source_context {kind,ref} — the app a conversation
    happened in decides what's first-class: a Workbench project, the
    circles self-thread, a peer host. Returns one row per thread with how
    much lives in it (turns + derived memories) and when it was last active,
    newest first. The memory app renders these as the top-level Threads view;
    drilling into one filters /brain/chat (turns) by that source_context.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"threads": [], "error": "pool not ready"}, 503

    sql = """
        SELECT
            metadata->'source_context'->>'kind' AS kind,
            metadata->'source_context'->>'ref'  AS ref,
            count(*) FILTER (WHERE memory_type = 'turn')  AS turn_count,
            count(*) FILTER (WHERE memory_type <> 'turn') AS memory_count,
            max(created_at) AS last_activity
          FROM entity_memory
         WHERE entity_id = $1
           AND status = 'active'
           AND (metadata->'source_context'->>'kind') IS NOT NULL
         GROUP BY 1, 2
         ORDER BY max(created_at) DESC
         LIMIT $2 OFFSET $3
    """
    rows = await pool.fetch(sql, brain_service._entity_id, limit, offset)
    threads = [
        {
            "kind": r["kind"],
            "ref": r["ref"],
            "turn_count": r["turn_count"],
            "memory_count": r["memory_count"],
            "last_activity": r["last_activity"].isoformat() if r["last_activity"] else None,
        }
        for r in rows
    ]
    return {"threads": threads, "count": len(threads), "offset": offset,
            "next_offset": (offset + len(threads)) if len(threads) == limit else None}


@router.get("/threads/episodes",
            dependencies=[Depends(requires_permission("read:brain_memory"))])
async def thread_episodes(
    sctx_kind: str = Query(..., description="source_context kind (project|app|peer|session)"),
    sctx_ref: str = Query(..., description="source_context ref (the thread id within kind)"),
    gap_minutes: int = Query(30, ge=5, le=1440,
                             description="dormancy gap that starts a new conversation "
                                         "(default 30 — the standard inactivity window)"),
    limit: int = Query(100, ge=1, le=500),
):
    """Split one thread into conversation episodes by dormancy gap.

    Derived at READ time (LAG window over created_at) — no episode id is
    stamped on rows, so the threshold can change without a backfill and the
    segmentation applies retroactively to every existing turn. Time is a
    display subdivision WITHIN a source_context; the source-driven threading
    model (the app decides what's first-class) is untouched, and so is
    recall scoping.

    Why: a busy surface accumulates one monolithic thread (the circles
    self-thread had 342 turns). On live data the gaps cluster hard — 30 vs
    60 min dormancy differed by only 9 boundaries out of 44 — so any
    threshold in that band yields the same conversations a human would draw.

    Returns episodes newest-first: {started_at, ended_at, turn_count,
    preview} where preview is the episode's first user message (falls back
    to its first turn). The client slices turns out of one episode via
    /brain/chat?sctx_kind=&sctx_ref=&since=<started_at>&until=<ended_at>.
    """
    pool = brain_service.get_pool()
    if pool is None:
        return {"episodes": [], "error": "pool not ready"}, 503

    sql = """
        WITH t AS (
            SELECT created_at, content, metadata->>'role' AS role,
                   CASE WHEN created_at - LAG(created_at) OVER (ORDER BY created_at)
                             > make_interval(mins => $4)
                        THEN 1 ELSE 0 END AS brk
              FROM entity_memory
             WHERE entity_id = $1
               AND memory_type = 'turn'
               AND status = 'active'
               AND metadata->'source_context'->>'kind' = $2
               AND metadata->'source_context'->>'ref'  = $3
        ), seg AS (
            SELECT *, SUM(brk) OVER (ORDER BY created_at) AS ep
              FROM t
        )
        SELECT min(created_at) AS started_at,
               max(created_at) AS ended_at,
               count(*)        AS turn_count,
               COALESCE(
                   (array_agg(content ORDER BY created_at)
                        FILTER (WHERE role = 'user'))[1],
                   (array_agg(content ORDER BY created_at))[1]
               ) AS preview
          FROM seg
         GROUP BY ep
         ORDER BY max(created_at) DESC
         LIMIT $5
    """
    rows = await pool.fetch(sql, brain_service._entity_id, sctx_kind, sctx_ref,
                            gap_minutes, limit)
    episodes = [
        {
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
            "turn_count": r["turn_count"],
            "preview": (r["preview"] or "")[:160],
        }
        for r in rows
    ]
    return {"episodes": episodes, "count": len(episodes), "gap_minutes": gap_minutes}


@router.get("/memory", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def search_memory(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(10, le=100),
):
    """Search entity memory. Audience-scoped per the caller's app token (C7a).
    The typed query is cut of its secret shapes first (server/secret_cut.py):
    recall embeds it on the Host's embedding endpoint, which can be a keyed
    outside provider."""
    return await brain_service.search_memory(
        query=secret_cut.search_words(q, door="memory search", log=logger),
        limit=limit, audience=_memory_audience(request),
    )


@router.get("/memory/semantic", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def semantic_search(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(20, le=100),
    memory_type: Optional[str] = Query(
        None,
        description=(
            "Filter to one memory_type, or comma-separated for multiple "
            "(e.g. 'preference,decision'). Omit to search all types."
        ),
    ),
    include_superseded: bool = Query(
        False,
        description=(
            "Include memories that have a t_valid_until set (superseded by "
            "a newer canonical version). Default false — only live memories."
        ),
    ),
):
    """Semantic similarity search using embeddings.

    Ship C: filters by `memory_type` (single or comma-separated) and enforces
    bi-temporal validity windows. `t_valid_until IS NULL` is the live row.
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"results": [], "error": "Brain not initialized"}

    # Route through THE recall
    # engine — canonical filters + hybrid arms live in exactly one place.
    # Response shape unchanged (byte-compatible); `arms` and
    # per-hit `matched` are additive. With the embed lane down this route now
    # returns lexical hits (similarity=null; the Memory app null-guards)
    # instead of failing outright.
    from mastercode.memory_retrieval import recall_memories, AudienceScopeError
    from mastercode.memory_engine import (
        RecallUnavailable, last_embed_model as _last_embed_model,
    )

    types = [t.strip() for t in (memory_type or "").split(",") if t.strip()] or None
    _audience = _memory_audience(request)
    # The typed query, cut of its secret shapes before recall embeds it on the
    # Host's embedding endpoint (server/secret_cut.py).
    q = secret_cut.search_words(q, door="memory search", log=logger)
    try:
        res = await recall_memories(
            pool, brain_service._entity_id, q, limit=limit,
            memory_types=types, include_superseded=include_superseded,
            audience=_audience)
    except RecallUnavailable as e:
        return {"results": [], "error": str(e)}
    except AudienceScopeError as e:
        logger.warning("memory/semantic audience scope failed closed: %s", e)
        return {"results": [], "error": "audience scope unavailable"}

    return {
        "results": [
            {
                "id": h["id"],
                "content": (h["content"] or "")[:500],
                "memory_type": h["memory_type"],
                "importance": float(h["importance"]) if h["importance"] else None,
                "confidence": float(h["confidence"]) if h["confidence"] else None,
                "access_count": h["access_count"],
                "similarity": h.get("relevance"),
                "matched": h.get("matched"),
                "metadata": h["metadata"],
                "created_at": h["created_at"].isoformat() if h["created_at"] else None,
            }
            for h in res["hits"]
        ],
        "query": q,
        "memory_type": memory_type,
        "include_superseded": include_superseded,
        "arms": res["arms"],
        "model": _last_embed_model() or None,
    }


@router.get("/memories", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def list_memories(
    request: Request,
    memory_type: Optional[str] = None,
    status: str = Query(
        "active",
        description="Row status filter; 'all' disables it (the correction "
                    "lens needs status='corrected' rows, which the default "
                    "silently excluded).",
    ),
    has_embedding: Optional[bool] = None,
    include_superseded: bool = Query(
        False,
        description=(
            "Include memories with t_valid_until set (older canonical "
            "versions superseded by Mem0 consolidation). Default false."
        ),
    ),
    lineage_only: bool = Query(
        False,
        description=(
            "Only rows that are part of a correction/supersession chain "
            "(t_valid_until, correction_of, corrected_by, resolution_pointer "
            "or status='corrected'). The memory app's Corrections lens uses "
            "this — client-side filtering over the newest N rows missed every "
            "chain buried under raw turn rows."
        ),
    ),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    as_of: Optional[str] = Query(
        None,
        description=(
            "Bitemporal time-travel: return memories VALID at this ISO instant "
            "(t_valid_from <= as_of < t_valid_until). Overrides the default "
            "'currently valid' filter."
        ),
    ),
):
    """Browse entity memories with filters.

    Ship C: enforces bi-temporal validity by default. memory_type accepts
    comma-separated for multi-type queries.
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"memories": [], "total": 0}

    eid = brain_service._entity_id
    where = "WHERE entity_id = $1"
    params: list = [eid]

    if status and status != "all":
        params.append(status)
        where += f" AND status = ${len(params)}"
    if lineage_only:
        where += (" AND (t_valid_until IS NOT NULL OR correction_of IS NOT NULL"
                  " OR corrected_by IS NOT NULL OR resolution_pointer IS NOT NULL"
                  " OR status = 'corrected')")
    if memory_type:
        types = [t.strip() for t in memory_type.split(",") if t.strip()]
        if types:
            params.append(types)
            where += f" AND memory_type = ANY(${len(params)}::text[])"
    _as_of_dt = None
    if as_of:
        from datetime import datetime, timezone
        try:
            _as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if _as_of_dt.tzinfo is None:
                _as_of_dt = _as_of_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            _as_of_dt = None
    if _as_of_dt is not None:
        # Bitemporal as-of: what the entity believed at this instant (time-travel).
        # created_at is the valid-from; t_valid_until is the supersession time.
        params.append(_as_of_dt)
        where += (f" AND created_at <= ${len(params)}"
                  f" AND (t_valid_until IS NULL OR t_valid_until > ${len(params)})")
    elif not include_superseded:
        where += " AND t_valid_until IS NULL"
    if has_embedding is not None:
        # Ask about the entity's ACTIVE dim column, not the 1024d `embedding`
        # one. On a non-1024 entity the hardcoded column is permanently NULL,
        # so has_embedding=True returned nothing and has_embedding=False
        # returned everything. Whitelisted name — safe to embed.
        try:
            from mastercode.memory_engine import active_embedding_column
            _emb_col, _ = await active_embedding_column(pool, eid)
        except Exception:
            _emb_col = "embedding"
        where += f" AND {_emb_col} IS {'NOT NULL' if has_embedding else 'NULL'}"

    # C7a — audience-scope the listing by the caller's app token (applied to BOTH
    # the count and the rows so paging totals match what the app can see).
    _audience = _memory_audience(request)
    if _audience is not None:
        from server.applications.memory_scope import filter_clause
        _clause, _extra = filter_clause(_audience, next_param_index=len(params) + 1)
        if _clause:
            where += " AND " + _clause
            params.extend(_extra)

    total = await pool.fetchval(
        f"SELECT COUNT(*) FROM entity_memory {where}", *params
    )

    rows = await pool.fetch(f"""
        SELECT id, content, memory_type, source_session,
               importance, confidence, access_count, last_accessed_at,
               status, status_reason, created_at, updated_at, t_valid_until,
               correction_of, corrected_by, resolution_pointer, resolved_at,
               (embedding IS NOT NULL) AS has_embedding,
               metadata, slot_topic, slot_scope, turn_type
        FROM entity_memory {where}
        ORDER BY created_at DESC
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """, *params, limit, offset)

    return {
        "memories": [
            {
                "id": r["id"],
                "content": r["content"][:300] if r["content"] else "",
                "memory_type": r["memory_type"],
                "source_session": r["source_session"],
                "importance": float(r["importance"]) if r["importance"] else 1.0,
                "confidence": float(r["confidence"]) if r["confidence"] else 1.0,
                "access_count": r["access_count"] or 0,
                "last_accessed_at": r["last_accessed_at"].isoformat() if r["last_accessed_at"] else None,
                "status": r["status"],
                "status_reason": r["status_reason"],
                "has_embedding": r["has_embedding"],
                "metadata": json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {}),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "t_valid_from": r["created_at"].isoformat() if r["created_at"] else None,
                "t_valid_until": r["t_valid_until"].isoformat() if r["t_valid_until"] else None,
                # Lineage — the correction lens renders chains from these;
                # they were detail-endpoint-only, which left it blind.
                "correction_of": r["correction_of"],
                "corrected_by": r["corrected_by"],
                "resolution_pointer": r["resolution_pointer"],
                "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
                # The slot (memory_slots): what it is about, where it is read,
                # and the turn type it was written in. No topic = outside the
                # one-live-entry rule.
                "topic": r["slot_topic"],
                "slot_scope": r["slot_scope"],
                "turn_type": r["turn_type"],
            }
            for r in rows
        ],
        "total": total or 0,
    }


@router.get("/memories/stats", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def memory_stats():
    """Memory store health and statistics."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    eid = brain_service._entity_id

    # Total counts by type
    type_rows = await pool.fetch("""
        SELECT memory_type, COUNT(*) AS cnt
        FROM entity_memory
        WHERE entity_id = $1 AND status = 'active'
        GROUP BY memory_type
        ORDER BY cnt DESC
    """, eid)

    # Status breakdown
    status_rows = await pool.fetch("""
        SELECT status, COUNT(*) AS cnt
        FROM entity_memory
        WHERE entity_id = $1
        GROUP BY status
        ORDER BY cnt DESC
    """, eid)

    # Embedding coverage, measured against the ACTIVE dim column — the one the
    # entity's CURRENT model writes and the semantic arm searches.
    #
    # This hardcoded the legacy 1024d `embedding` column, so any entity on a
    # non-1024 model had its coverage under-reported by whatever fraction of
    # its rows predated the switch. Measured on one Host (qwen3-embedding:4b,
    # 2560d) by calling this route from inside the pod: it
    # returned 1158/2458 = 47.1% while TRUE coverage was 2458/2458 = 100%.
    # Every row was searchable and the health surface said half of them were
    # not. /api/brain/embed/coverage already routed correctly via
    # active_embedding_column (embed.py); this endpoint never got the fix.
    # Column name comes from the EMBEDDING_COLUMN_BY_DIM whitelist — safe to
    # interpolate (memory_engine.active_embedding_column).
    from mastercode.memory_engine import active_embedding_column
    _content_col, _ = await active_embedding_column(pool, eid)
    embed_row = await pool.fetchrow(f"""
        SELECT COUNT(*) AS total,
               COUNT({_content_col}) AS embedded
        FROM entity_memory
        WHERE entity_id = $1 AND status = 'active'
    """, eid)

    # Age distribution
    age_row = await pool.fetchrow("""
        SELECT MIN(created_at) AS oldest,
               MAX(created_at) AS newest,
               AVG(importance) AS avg_importance,
               AVG(confidence) AS avg_confidence,
               SUM(access_count) AS total_accesses
        FROM entity_memory
        WHERE entity_id = $1 AND status = 'active'
    """, eid)

    # Lineage + resolution counters — the memory app's totals strip shows
    # these; counting client-side over one loaded page was always 0/wrong.
    lineage_row = await pool.fetchrow("""
        SELECT COUNT(*) FILTER (WHERE t_valid_until IS NOT NULL) AS superseded,
               COUNT(*) FILTER (WHERE memory_type = 'goal'
                                AND resolved_at IS NULL
                                AND status = 'active'
                                AND t_valid_until IS NULL) AS open_goals
        FROM entity_memory
        WHERE entity_id = $1
    """, eid)

    total = embed_row["total"] if embed_row else 0
    embedded = embed_row["embedded"] if embed_row else 0

    # ── Is memory still FORMING? (v117) ────────────────────────────────────
    # Everything above describes the store's CONTENTS. None of it answers the
    # question that matters when something breaks: is the pipeline that fills
    # the store still running? Once fact derivation died and every
    # number above stayed perfectly plausible for 16 days.
    #
    # Staleness is computed HERE, by the reader, from max(finished_at) — never
    # self-reported by the worker. A consolidator loop that died silently
    # cannot file an "I stopped" report; the ABSENCE of recent rows is the
    # signal, and only a reader can observe an absence.
    #
    # Queue depths come from the workers' OWN exported predicates, never
    # restated here: the naive graph predicate counted 1220 rows on one Host
    # where the true queue is 0 (turn rows keep graph_extracted_at NULL by
    # design), so a health surface that invents its own filter reports a
    # permanent backlog that does not exist.
    from datetime import datetime, timezone
    # Memory forms in the turn (close_turn, memory_curate); there is no
    # background pass to be late.
    formation = {"available": True, "in_turn": True, "stale": False,
                 "warming_up": False}

    # Unconfigured embeddings are a SETUP state, never an outage. There is no
    # platform embedder — the user provides the endpoint — so "0 of 13 embedded"
    # on a host that has not chosen a model yet is nothing broken, and the
    # coverage block must say so rather than let a reader infer a dead pipeline
    # (a fresh host reported "embedding lane down system-wide").
    try:
        from server.routes.embedding import current as _embedding_current
        _emb = await _embedding_current()
    except Exception as _emb_err:
        logger.info("embedding config read failed for coverage: %s", _emb_err)
        _emb = {"configured": None, "model": None}
    _emb_configured = _emb.get("configured")

    return {
        "total_active": total,
        "formation": formation,
        "by_type": {r["memory_type"]: r["cnt"] for r in type_rows},
        "by_status": {r["status"]: r["cnt"] for r in status_rows},
        "embedding_coverage": {
            "total": total,
            "embedded": embedded,
            "coverage_pct": round(embedded / total * 100, 1) if total > 0 else 0,
            # configured=False is the setup state; the note is user-safe copy.
            # None means the config could not be READ this call — say that,
            # never "not set up" (a configured host with a transient DB error
            # must not be told nothing is broken).
            "configured": _emb_configured,
            "model": _emb.get("model"),
            "note": (None if _emb_configured else
                     ("Embeddings aren't set up yet for this host — nothing is broken. "
                      "Pick a machine you run or a provider you hold a key for in "
                      "Orchestrator → Memory, and search gains meaning-based recall.")
                     if _emb_configured is False else
                     "The embedding configuration could not be read just now; retry."),
        },
        "oldest": age_row["oldest"].isoformat() if age_row and age_row["oldest"] else None,
        "newest": age_row["newest"].isoformat() if age_row and age_row["newest"] else None,
        "avg_importance": round(float(age_row["avg_importance"]), 2) if age_row and age_row["avg_importance"] else 0,
        "avg_confidence": round(float(age_row["avg_confidence"]), 2) if age_row and age_row["avg_confidence"] else 0,
        "total_accesses": age_row["total_accesses"] if age_row else 0,
        "superseded": lineage_row["superseded"] if lineage_row else 0,
        "open_goals": lineage_row["open_goals"] if lineage_row else 0,
    }


@router.get("/memories/{memory_id}", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def get_memory_detail(memory_id: int, request: Request):
    """Single memory with full content and metadata.

    The same audience as the list routes (_memory_audience): an app that is
    neither native nor holding read:brain_memory_all sees a memory only when it
    is tagged for that app. This route once filtered by entity
    alone, so any holder of read:brain_memory could read any memory by id."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    where, params = "WHERE id = $1 AND entity_id = $2", [memory_id, brain_service._entity_id]
    _audience = _memory_audience(request)
    if _audience is not None:
        from server.applications.memory_scope import filter_clause
        _clause, _extra = filter_clause(_audience, next_param_index=len(params) + 1)
        if _clause:
            where += " AND " + _clause
            params.extend(_extra)
    row = await pool.fetchrow(f"""
        SELECT id, content, memory_type, source_session, source_message_idx,
               importance, confidence, access_count, last_accessed_at,
               status, status_reason, status_changed_at, t_valid_until,
               correction_of, corrected_by, resolution_pointer, resolved_at,
               project_id,
               visibility, metadata,
               (embedding IS NOT NULL) AS has_embedding,
               created_at, updated_at, slot_topic, slot_scope, turn_type
        FROM entity_memory
        {where}
    """, *params)

    if not row:
        return {"error": "Memory not found"}

    # A rule (a preference marked rule_kind: taught or learned) is labelled
    # with its kind; a skill_memory row the boot pass left is a stored skill,
    # not a rule (default-skills/memory.md). A live plain preference in a
    # rule's own words is held by that rule (memory_engine.same_words_key) —
    # kept where the rule is not read, never shown beside it in a turn.
    from mastercode.memory_engine import rules_holding, same_words_key
    _md = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {})
    _rule_kind = (_md.get("rule_kind") or None) if row["memory_type"] == "preference" else None
    _held_by = None
    if (_rule_kind is None and row["memory_type"] == "preference" and row["status"] == "active"
            and row["t_valid_until"] is None):
        _held_by = (await rules_holding(pool, brain_service._entity_id)).get(
            same_words_key(row["content"]))
    return {
        "id": row["id"],
        "content": row["content"],
        "memory_type": row["memory_type"],
        "rule_kind": _rule_kind,
        "stored_skill": row["memory_type"] == "skill_memory",
        "held_by_rule": _held_by,
        "source_session": row["source_session"],
        "source_message_idx": row["source_message_idx"],
        "importance": float(row["importance"]) if row["importance"] else 1.0,
        "confidence": float(row["confidence"]) if row["confidence"] else 1.0,
        "access_count": row["access_count"] or 0,
        "last_accessed_at": row["last_accessed_at"].isoformat() if row["last_accessed_at"] else None,
        "status": row["status"],
        "status_reason": row["status_reason"],
        "status_changed_at": row["status_changed_at"].isoformat() if row["status_changed_at"] else None,
        "t_valid_from": row["created_at"].isoformat() if row["created_at"] else None,
        "t_valid_until": row["t_valid_until"].isoformat() if row["t_valid_until"] else None,
        # The real column — this used to alias corrected_by, which is NULL on
        # every consolidation supersession (only resolution_pointer is set
        # there), so those chains never surfaced in the inspector.
        "resolution_pointer": row["resolution_pointer"],
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
        "correction_of": row["correction_of"],
        "corrected_by": row["corrected_by"],
        "project_id": row["project_id"],
        "visibility": row["visibility"],
        "has_embedding": row["has_embedding"],
        "metadata": json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {}),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        # The slot and its replacement chain (memory_slots): the rows this one
        # replaced, newest first, each with when and why it left.
        "topic": row["slot_topic"],
        "slot_scope": row["slot_scope"],
        "turn_type": row["turn_type"],
        "replaced_by": row["resolution_pointer"],
        "chain": await _slot_chain(pool, row["id"]),
    }


async def _slot_chain(pool, row_id: int) -> list:
    try:
        from mastercode.memory_slots import slot_chain
        return await slot_chain(pool, brain_service._entity_id, int(row_id))
    except Exception as e:  # noqa: BLE001 — the row reads without its chain
        logger.info("memory chain unavailable for %s: %s", row_id, e)
        return []


@router.get("/memory-slots", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def list_memory_slots(request: Request, limit: int = Query(200, ge=1, le=500)):
    """Every live preference and relationship note by slot (memory_slots:
    type, scope, topic), each with its replacement chain, and the live rows
    with no topic yet (outside the one-live-entry rule until one is given:
    POST /manage/memories/{id}/topic). The Memory app's Slots lens."""
    pool = brain_service.get_pool()
    if not pool:
        return {"slots": [], "no_topic": []}
    if _memory_audience(request) is not None:
        # An app sees memories tagged for it, not the Host's whole slot table.
        raise HTTPException(status_code=403, detail="the slot table is the owner's view")
    eid = brain_service._entity_id
    rows = await pool.fetch(
        "SELECT id, content, memory_type, slot_topic, slot_scope, turn_type, created_at,"
        "       metadata ? 'rule_kind' AS is_rule, metadata->>'written_by' AS written_by"
        "  FROM entity_memory WHERE entity_id = $1 AND memory_type IN ('preference', 'relationship')"
        "   AND status = 'active' AND t_valid_until IS NULL AND slot_topic IS NOT NULL"
        " ORDER BY memory_type, slot_scope, slot_topic LIMIT $2", eid, limit)
    from mastercode.memory_slots import slot_chain
    slots = []
    for r in rows:
        slots.append({
            "type": r["memory_type"], "scope": r["slot_scope"], "topic": r["slot_topic"],
            "rule": bool(r["is_rule"]),
            "live": {"id": r["id"], "content": r["content"], "turn_type": r["turn_type"],
                     "written_by": r["written_by"],
                     "created_at": r["created_at"].isoformat() if r["created_at"] else None},
            "chain": await slot_chain(pool, eid, int(r["id"])),
        })
    loose = await pool.fetch(
        "SELECT id, content, memory_type, slot_scope, turn_type, created_at,"
        "       metadata ? 'rule_kind' AS is_rule"
        "  FROM entity_memory WHERE entity_id = $1 AND memory_type IN ('preference', 'relationship')"
        "   AND status = 'active' AND t_valid_until IS NULL AND slot_topic IS NULL"
        " ORDER BY created_at DESC, id DESC LIMIT $2", eid, limit)
    return {
        "slots": slots,
        "no_topic": [{"id": r["id"], "content": r["content"], "type": r["memory_type"],
                      "scope": r["slot_scope"], "turn_type": r["turn_type"], "rule": bool(r["is_rule"]),
                      "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                     for r in loose],
    }


@router.post("/manage/memories/{memory_id}/topic", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def set_memory_topic(memory_id: int, request: Request):
    """The owner gives a live preference or relationship note a topic, or
    renames it (memory_slots.key_row). When another live row holds that
    slot, a rule outranks a preference, else the newer row stays; the other
    is retired into it. Body: {"topic": "<a few words>"}."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Brain not initialized")
    body = await request.json()
    from mastercode.memory_slots import key_row, normalize_topic
    topic = normalize_topic((body or {}).get("topic"))
    if topic is None:
        raise HTTPException(status_code=400, detail="topic: a few words naming what the entry is about")
    res = await key_row(pool, brain_service._entity_id, memory_id, topic,
                        stamp={"kind": "app", "ref": "memory"})
    if res.get("status") in ("not_live", "not_slotted"):
        raise HTTPException(status_code=404, detail="no live preference or relationship note with that id")
    return {"ok": True, **res}


# ── Ollama Model Management ───────────────────────────

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


@router.get("/models", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def list_models():
    """List embedding models available in the entity's Ollama instance."""
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{OLLAMA_URL}/api/tags") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = data.get("models", [])
                    return {
                        "models": [
                            {
                                "name": m.get("name", ""),
                                "size": m.get("size", 0),
                                "modified_at": m.get("modified_at", ""),
                                "digest": m.get("digest", "")[:12],
                                "details": m.get("details", {}),
                            }
                            for m in models
                        ],
                        "ollama_url": OLLAMA_URL,
                    }
                return {"models": [], "error": f"Ollama returned {resp.status}"}
    except Exception as e:
        return {"models": [], "error": str(e)}


@router.post("/models/pull", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def pull_model(request: Request):
    """Pull a model into the entity's Ollama instance."""
    import aiohttp
    body = await request.json()
    model_name = body.get("model", "").strip()
    if not model_name:
        return {"ok": False, "error": "model name required"}

    try:
        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{OLLAMA_URL}/api/pull", json={
                "name": model_name,
                "stream": False,
            }) as resp:
                if resp.status == 200:
                    return {"ok": True, "model": model_name}
                body_text = await resp.text()
                return {"ok": False, "error": f"{resp.status}: {body_text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/memories/reembed-job", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def reembed_memories_job(request: Request):
    """Enqueue a durable embed_job for unembedded entity_memory rows.

    Mirrors context.py's /embed: returns a real job_id whose progress is
    broadcast on the dashboard WS as {type: "embed_job", ...} events and
    which survives container restarts. Body: { "force": false }.
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    eid = brain_service._entity_id
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    force = body.get("force", False)

    # Config-driven: the job embeds with the entity's CONFIGURED model
    # (resolved inside enqueue_job) — never an env/literal default.
    from server.routes import embed as _embed
    try:
        job = await _embed.enqueue_job(
            entity_id=eid,
            job_type="reembed_memory",
            target="entity_memory",
            metadata={"force": bool(force)},
        )
    except ValueError as e:
        return {"error": str(e)}
    if job is None:
        return {"error": "Brain not initialized"}

    if (job.get("total") or 0) == 0:
        return {"queued": 0, "job_id": job["id"], "message": "All memories already embedded"}

    return {
        "queued": job["total"],
        "job_id": job["id"],
        "model": job["model"],
        "force": bool(force),
    }


@router.get("/personality", dependencies=[Depends(requires_permission("read:brain_personality"))])
async def get_personality():
    """Get current personality weights and configuration."""
    return await get_personality_service(brain_service.get_pool(), brain_service._entity_id)


@router.put("/personality/weights", dependencies=[Depends(requires_permission("write:brain_personality"))])
async def put_personality_weights(body: dict):
    """The user's direct personality sliders (Settings → Response Shaping).
    Sets the 10D weights."""
    return await set_personality_weights_service(
        brain_service.get_pool(), brain_service._entity_id,
        (body or {}).get("weights"),
    )


@router.put("/personality/explanation", dependencies=[Depends(requires_permission("write:brain_personality"))])
async def put_explanation_level(body: dict):
    """The user's explanation level (Settings → Response Shaping, or the host's
    `explanation_level` tool when the user asks in words). Three positions,
    named by what the host does: 'full' (define every term), 'new' (define
    a term the first time it appears — the default), 'terms' (use the
    field's terms, lead with the result). Never inferred."""
    return await set_explanation_level_service(
        brain_service.get_pool(), brain_service._entity_id,
        (body or {}).get("level"),
    )


@router.get("/manage/duplicates", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def scan_duplicates(limit: int = Query(100, le=500),
                          offset: int = Query(0, ge=0, description="groups to skip — page past the first `limit`")):
    """Find duplicate memories by content hash. `total_groups` / `total_excess`
    count EVERY duplicate group, not the page."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    eid = brain_service._entity_id
    rows = await pool.fetch("""
        WITH dupes AS (
            SELECT md5(content) as hash, array_agg(id ORDER BY access_count DESC, created_at ASC) as ids,
                   COUNT(*) as copies, MIN(content) as content, MIN(memory_type) as memory_type
            FROM entity_memory
            WHERE entity_id = $1 AND status = 'active'
            GROUP BY md5(content) HAVING COUNT(*) > 1
        )
        SELECT hash, ids, copies, LEFT(content, 200) as content, memory_type,
               COUNT(*) OVER () AS all_groups, SUM(copies - 1) OVER () AS all_excess
        FROM dupes ORDER BY copies DESC, hash LIMIT $2 OFFSET $3
    """, eid, limit, offset)

    total_dupes = int(rows[0]["all_excess"]) if rows else 0
    all_groups = int(rows[0]["all_groups"]) if rows else 0
    return {
        "groups": [
            {
                "hash": r["hash"],
                "ids": list(r["ids"]),
                "copies": r["copies"],
                "keep_id": r["ids"][0],
                "remove_ids": list(r["ids"][1:]),
                "content": r["content"],
                "memory_type": r["memory_type"],
            }
            for r in rows
        ],
        "total_groups": all_groups,
        "total_excess": total_dupes,
        "offset": offset,
        "next_offset": (offset + len(rows)) if offset + len(rows) < all_groups else None,
    }


@router.post("/manage/dedup", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def merge_duplicates(request: Request):
    """Merge duplicate memories. Keeps highest-access copy, archives the rest."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    eid = brain_service._entity_id
    merge_all = body.get("all", False)

    if merge_all:
        result = await pool.fetch("""
            WITH dupes AS (
                SELECT md5(content) as hash,
                       array_agg(id ORDER BY access_count DESC, created_at ASC) as ids
                FROM entity_memory
                WHERE entity_id = $1 AND status = 'active'
                GROUP BY md5(content) HAVING COUNT(*) > 1
            ),
            to_archive AS (
                SELECT unnest(ids[2:]) as id FROM dupes
            )
            UPDATE entity_memory SET status = 'archived', status_reason = 'dedup_merge'
            WHERE id IN (SELECT id FROM to_archive)
            RETURNING id
        """, eid)
        return {"archived": len(result), "action": "dedup_all"}

    ids = body.get("ids", [])
    if not ids:
        return {"error": "provide ids to archive or set all=true"}

    result = await pool.fetch("""
        UPDATE entity_memory SET status = 'archived', status_reason = 'dedup_merge'
        WHERE id = ANY($1) AND entity_id = $2
        RETURNING id
    """, ids, eid)
    return {"archived": len(result), "ids": [r["id"] for r in result]}


@router.get("/manage/tasks", dependencies=[Depends(requires_permission("read:brain_tasks"))])
async def list_tasks(
    status: Optional[str] = None,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0, description="rows to skip — page past the first `limit`"),
):
    """List entity tasks for review."""
    return await list_tasks_service(
        brain_service.get_pool(), brain_service._entity_id, status=status, limit=limit,
        offset=offset)


@router.post("/manage/tasks/purge", dependencies=[Depends(requires_permission("write:brain_tasks"))])
async def purge_tasks(request: Request):
    """Cancel or delete tasks by status."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return await purge_tasks_service(
        brain_service.get_pool(), brain_service._entity_id,
        status=body.get("status", "queued"), action=body.get("action", "cancel"))


@router.post("/manage/tasks/{task_id}/cancel", dependencies=[Depends(requires_permission("write:brain_tasks"))])
async def cancel_task(task_id: int):
    """Cancel a single task."""
    return await cancel_task_service(brain_service.get_pool(), brain_service._entity_id, task_id)


@router.post("/manage/tasks/{task_id}/delete", dependencies=[Depends(requires_permission("write:brain_tasks"))])
async def delete_task(task_id: int):
    """Delete a single task."""
    return await delete_task_service(brain_service.get_pool(), brain_service._entity_id, task_id)


@router.get("/manage/unretrieved", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def unretrieved_stats():
    """Stats on never-retrieved memories by type."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    eid = brain_service._entity_id
    rows = await pool.fetch("""
        SELECT memory_type, COUNT(*) as total,
               COUNT(*) FILTER (WHERE access_count = 0) as never_retrieved,
               COUNT(*) FILTER (WHERE access_count > 0) as retrieved
        FROM entity_memory
        WHERE entity_id = $1 AND status = 'active'
        GROUP BY memory_type ORDER BY never_retrieved DESC
    """, eid)

    return {
        "types": [
            {
                "type": r["memory_type"],
                "total": r["total"],
                "never_retrieved": r["never_retrieved"],
                "retrieved": r["retrieved"],
                "pct_wasted": round(100 * r["never_retrieved"] / r["total"], 1) if r["total"] > 0 else 0,
            }
            for r in rows
        ],
        "total_never_retrieved": sum(r["never_retrieved"] for r in rows),
        "total_active": sum(r["total"] for r in rows),
    }


@router.post("/manage/memories/archive", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def archive_memories(request: Request):
    """Archive memories by criteria."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    body = await request.json()
    eid = brain_service._entity_id
    ids = body.get("ids", [])
    memory_type = body.get("memory_type")
    unretrieved_only = body.get("unretrieved_only", False)
    reason = body.get("reason", "manual_archive")

    if ids:
        result = await pool.fetch("""
            UPDATE entity_memory SET status = 'archived', status_reason = $3
            WHERE id = ANY($1) AND entity_id = $2 AND status = 'active'
            RETURNING id
        """, ids, eid, reason)
        return {"archived": len(result)}

    if memory_type and unretrieved_only:
        # Never a rule-marked preference (memory_engine.RULE_KINDS): nothing
        # marks a rule read, so "unretrieved" would be every rule. The reason
        # was once bound as $4 of three arguments, so this sweep failed on every
        # call; nothing in the tree calls it.
        from mastercode.memory_engine import not_rule_sql
        result = await pool.fetch(f"""
            UPDATE entity_memory SET status = 'archived', status_reason = $3
            WHERE entity_id = $1 AND memory_type = $2 AND access_count = 0 AND status = 'active'
              AND {not_rule_sql()}
            RETURNING id
        """, eid, memory_type, reason)
        return {"archived": len(result), "type": memory_type}

    return {"error": "provide ids or memory_type+unretrieved_only"}


@router.post("/manage/memories/{memory_id}/archive", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def archive_single_memory(memory_id: int):
    """Archive a single memory."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    await pool.execute("""
        UPDATE entity_memory SET status = 'archived', status_reason = 'manual_archive'
        WHERE id = $1 AND entity_id = $2
    """, memory_id, brain_service._entity_id)
    return {"ok": True, "id": memory_id}


@router.post("/manage/memories/{memory_id}/delete", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def delete_single_memory(memory_id: int):
    """Permanently delete a single memory.

    C8: guarded against orphaning the correction chain. Any row pointing at
    this one (correction_of / corrected_by / resolution_pointer) is unlinked
    first, in one transaction, so the FK constraints can't block the delete or
    leave dangling references. Audited; 404 if the row isn't ours/found.
    """
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    eid = brain_service._entity_id
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE entity_memory SET correction_of = NULL "
                "WHERE entity_id = $1 AND correction_of = $2", eid, memory_id)
            await conn.execute(
                "UPDATE entity_memory SET corrected_by = NULL "
                "WHERE entity_id = $1 AND corrected_by = $2", eid, memory_id)
            await conn.execute(
                "UPDATE entity_memory SET resolution_pointer = NULL "
                "WHERE entity_id = $1 AND resolution_pointer = $2", eid, memory_id)
            deleted = await conn.fetchval(
                "DELETE FROM entity_memory WHERE id = $1 AND entity_id = $2 "
                "RETURNING id", memory_id, eid)
    if deleted is None:
        raise HTTPException(status_code=404, detail="memory not found")

    return {"ok": True, "id": memory_id, "deleted": True}


@router.post("/manage/memories/{memory_id}/resolve", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def resolve_single_memory(memory_id: int):
    """Mark a memory resolved (goal / unresolved → done) — C8.

    Writes resolved_at=NOW() so the row stops polluting prompt context and
    decays faster (memory_consolidation honors resolved_at). This is the
    user-facing setter; previously only internal consolidation could set it.
    """
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    eid = brain_service._entity_id
    row = await pool.fetchrow("""
        UPDATE entity_memory
        SET resolved_at = NOW(), updated_at = NOW()
        WHERE id = $1 AND entity_id = $2 AND resolved_at IS NULL
        RETURNING id
    """, memory_id, eid)
    if row is None:
        exists = await pool.fetchval(
            "SELECT 1 FROM entity_memory WHERE id = $1 AND entity_id = $2",
            memory_id, eid)
        if not exists:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"ok": True, "id": memory_id, "already_resolved": True}

    return {"ok": True, "id": memory_id, "resolved": True}


@router.post("/manage/memories/{memory_id}/edit", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def edit_single_memory(memory_id: int, request: Request):
    """Edit a memory by appending a NEW VERSION row — never mutate in place (C8).

    Append-correct model: the original row is closed (status='corrected',
    t_valid_until=NOW(), corrected_by → new id) and a new row carrying the
    edited content is inserted, linked via correction_of → old id and
    re-embedded at the entity's active model/dimension. History is preserved;
    default recall (which filters t_valid_until IS NULL) sees only the new
    version. Body: {"content": "<new text>"}.

    The new text is kept as the turn keeps a message: every hard secret shape
    cut to its [REDACTED:<id>] marker (server/secret_cut.py) before the row
    stores it, the Host's embedding endpoint (which can be a keyed outside
    provider) reads it, or recall hands it to the chat model. An edit that only
    re-types a key the row already holds cut is unchanged.
    """
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    body = await request.json()
    new_content = (body.get("content") or "").strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="content is required")
    new_content = secret_cut.redact_words(new_content, door="memory edit", log=logger)

    eid = brain_service._entity_id
    old = await pool.fetchrow("""
        SELECT content, memory_type, importance, confidence, visibility,
               project_id, metadata
        FROM entity_memory
        WHERE id = $1 AND entity_id = $2 AND status = 'active'
          AND t_valid_until IS NULL
    """, memory_id, eid)
    if old is None:
        raise HTTPException(status_code=404, detail="memory not found or not editable")

    if new_content == (old["content"] or "").strip():
        return {"ok": True, "id": memory_id, "unchanged": True}

    # Re-embed the new content. On embed-backend failure we still write the row
    # (without a content vector) so the edit isn't lost — it becomes searchable
    # after the next backfill, matching the chat write path.
    from mastercode.memory_engine import _get_memory_embedding, active_embedding_column
    try:
        embedding_str = await _get_memory_embedding(new_content)
    except Exception as e:
        logger.warning("edit re-embed failed (writing row without vector): %s", e)
        embedding_str = None

    meta = old["metadata"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    meta = dict(meta or {})
    meta["edited_from"] = memory_id

    async with pool.acquire() as conn:
        async with conn.transaction():
            if embedding_str:
                # Whitelisted column name from the active-model router.
                content_col, _ = await active_embedding_column(pool, eid)
                new_row = await conn.fetchrow(
                    f"""
                    INSERT INTO entity_memory (
                        entity_id, content, memory_type,
                        importance, confidence, visibility,
                        project_id, metadata, correction_of, {content_col}
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10::vector)
                    RETURNING id
                    """,
                    eid, new_content, old["memory_type"],
                    old["importance"], old["confidence"], old["visibility"],
                    old["project_id"], json.dumps(meta), memory_id,
                    embedding_str,
                )
            else:
                new_row = await conn.fetchrow(
                    """
                    INSERT INTO entity_memory (
                        entity_id, content, memory_type,
                        importance, confidence, visibility,
                        project_id, metadata, correction_of
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)
                    RETURNING id
                    """,
                    eid, new_content, old["memory_type"],
                    old["importance"], old["confidence"], old["visibility"],
                    old["project_id"], json.dumps(meta), memory_id,
                )
            new_id = new_row["id"]
            await conn.execute("""
                UPDATE entity_memory
                SET status = 'corrected', status_reason = 'edited',
                    corrected_by = $3, resolution_pointer = $3,
                    t_valid_until = NOW(), status_changed_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1 AND entity_id = $2
            """, memory_id, eid, new_id)
            # The new version takes the old one's slot (memory_slots), now
            # that the old one has left it, with the owner's edit as its
            # provenance.
            await conn.execute("""
                UPDATE entity_memory n
                   SET slot_topic = o.slot_topic, slot_scope = o.slot_scope, turn_type = 'owner edit'
                  FROM entity_memory o
                 WHERE n.id = $3 AND n.entity_id = $2 AND o.id = $1 AND o.entity_id = $2
            """, memory_id, eid, new_id)

    return {"ok": True, "old_id": memory_id, "new_id": new_id,
            "embedded": bool(embedding_str)}


# ── Cortex: Unified Chat Endpoint ────────────────────────────────────────
#
# Single endpoint for Cortex sensory interface. Handles:
#   - Text messages + conversation history
#   - Images as native content blocks (direct to inference — no summarization)
#   - Preloaded context injection
#   - Personality system prompt
#   - Model selection via OpenRouter (any model, one API)
#   - SSE streaming
#
# Inference routing:
#   - OpenRouter (default): OPENROUTER_API_KEY env or from entity config
#   - Direct Anthropic: ANTHROPIC_API_KEY (fallback for platform-managed)
#   - Direct xAI: XAI_API_KEY (fallback for platform-managed)
#
# Users choose at provisioning: turnkey (platform key) or BYO key.


def _load_inference_key() -> tuple[str, str]:
    """Load inference API key. Returns (key, provider).

    Priority: OPENROUTER_API_KEY > ANTHROPIC_API_KEY > XAI_API_KEY.
    """
    # OpenRouter (preferred — unified, model-agnostic)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key, "openrouter"

    # Direct Anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key, "anthropic"

    # Direct xAI
    key = os.environ.get("XAI_API_KEY", "")
    if key:
        return key, "xai"

    return "", "none"


@router.post("/chat", dependencies=[Depends(requires_permission("execute:inference"))])
async def cortex_chat(request: Request):
    """DEPRECATED — cortex chat surface is being retired.

    Primary chat lives in the cseries Circles app (Conversations pane).
    the Workbench handles app-dev chat. The handler body below is preserved
    until the next image cut so that any unexpected caller surfaces a
    clear 410 rather than silently swapping providers.
    """
    raise HTTPException(
        status_code=410,
        detail={
            "code": "cortex_chat_deprecated",
            "message": (
                "POST /api/brain/chat (cortex) is deprecated. "
                "Use the Circles app's Conversations pane (POST /api/circles/messages with target_kind=self) "
                "for primary chat, or the Workbench for build work."
            ),
            "replacement": "/api/circles/messages",
        },
    )


# ── Cortex: WebSearch ─────────────────────────────────────────────────────

@router.get("/websearch", dependencies=[Depends(requires_permission("execute:websearch"))])
async def cortex_websearch(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, le=20),
):
    """GONE (410) — superseded by the metered research path (runtime/web_research.py).

    This route called api.x.ai directly with the in-pod XAI_API_KEY and
    booked NO wallet debit — an unmetered platform-cost xAI egress. It is
    confirmed dead: no system-app manifest grants `execute:websearch`,
    no app.js calls `/api/brain/websearch`, and the "Cortex TokenSaver panel"
    it served does not exist (no `cortex` app). Web research now flows through
    the metered, keys-never-in-pod path: the chat `research` / `web_search`
    meta-tools (runtime/tools.py, runtime/web_research.py — the routed model's
    provider-native search). Rather than wire a wallet gate + bridge debit
    onto an endpoint with zero live callers, the route is retired so it can't
    resurface as an unmetered, in-pod-key egress.
    """
    raise HTTPException(
        status_code=410,
        detail="GET /api/brain/websearch is retired. Use the chat `research` / "
               "`web_search` tools (metered, keys never leave the platform).",
    )


# ── Cortex: Episodes Timeline ────────────────────────────────────────────

async def get_personality_service(pool, entity_id: int) -> dict:
    """Current personality weights + configuration (10D current/seed weights,
    envelope bounds, personality mode, skill weights, directive).
    brain_loop_mode is dropped — retired-UnifiedBrain vestige with
    zero writers; the genesis-only column crashed every pre-existing pod."""
    if not pool:
        return {"error": "Brain not initialized"}

    row = await pool.fetchrow("""
        SELECT display_name, current_weights, seed_weights,
               envelope_bounds, personality_mode,
               skill_weights, master_directive, behavioral_params
        FROM entity_registry WHERE id = $1
    """, entity_id)

    if not row:
        return {"error": "Host not found"}

    # The explanation level: a user-owned setting in
    # behavioral_params, read the same way the time zone is. Never inferred.
    from mastercode.runtime.prompt_builder import explanation_level_of
    from server.host_display import display_name_or_host
    result = {
        "display_name": display_name_or_host(row["display_name"], entity_id),
        "current_weights": json.loads(row["current_weights"]) if isinstance(row["current_weights"], str) else row["current_weights"],
        "seed_weights": json.loads(row["seed_weights"]) if isinstance(row["seed_weights"], str) else row["seed_weights"],
        "envelope_bounds": json.loads(row["envelope_bounds"]) if isinstance(row["envelope_bounds"], str) else row["envelope_bounds"],
        "personality_mode": row["personality_mode"],
        "skill_weights": json.loads(row["skill_weights"]) if isinstance(row["skill_weights"], str) else row["skill_weights"],
        "directive": row["master_directive"],
        "explanation_level": explanation_level_of(row["behavioral_params"]),
    }

    return result


async def set_personality_weights_service(pool, entity_id: int, weights) -> dict:
    """The user's direct sliders (PUT /personality/weights). Personality
    is granular user control, not secret code: the user sets the 10D
    weights directly.

    Semantics: a user set writes BOTH seed_weights and current_weights —
    it is the user-authored baseline. Past the genesis seed, this is the
    only registered writer (personality_validate.REGISTERED_WRITER_SOURCES).

    Governance unchanged in kind: the write passes guard_personality_write
    (Invariant 9 shape — exactly the 10 named dimensions, each 0.0-2.0 — plus
    fail-closed writer identity) and lands an audit row per column. Harm
    is impossible by construction — guardrails live in the constitution,
    which personality settings cannot touch (see
    scripts/check_system_separation.py).
    """
    if not pool:
        return {"error": "Brain not initialized"}
    if not isinstance(weights, dict) or not weights:
        return {"error": "body must be {\"weights\": {<dimension>: <0.0-2.0>, ...}}"}

    import sys as _sys
    if "/app/brain" not in _sys.path:
        _sys.path.insert(0, "/app/brain")
    from mastercode.runtime.personality_validate import guard_personality_write

    # Clamp to the scale; the Invariant 9 gate validates shape + range after.
    try:
        cleaned = {k: max(0.0, min(2.0, float(v))) for k, v in weights.items()}
    except (TypeError, ValueError):
        return {"error": "weights must be numeric"}

    for col in ("seed_weights", "current_weights"):
        ok, vios = await guard_personality_write(
            pool, entity_id,
            source="routes.brain.user_weights_set",
            column=col,
            weights=cleaned,
            detail="user slider set",
        )
        if not ok:
            return {"error": "weights rejected", "violations": vios}

    await pool.execute(
        """
        UPDATE entity_registry
        SET seed_weights = $2, current_weights = $2
        WHERE id = $1
        """,
        entity_id, json.dumps(cleaned),
    )
    return {"ok": True, "weights": cleaned}


async def set_explanation_level_service(pool, entity_id: int, level) -> dict:
    """Write behavioral_params.explanation_level — the user-owned setting
    for how much the host explains.

    Same read-modify-write as the time zone (routes/appearance.set_timezone):
    the blob is re-read, one key changes, the rest is written back exactly.
    The identity block renders the level per rebuild
    (prompt_builder._build_explanation_block), so the next chat request
    carries it. None/"" clears the key → the default ('new')."""
    if not pool:
        return {"error": "Brain not initialized"}
    import sys as _sys
    if "/app/brain" not in _sys.path:
        _sys.path.insert(0, "/app/brain")
    from mastercode.runtime.prompt_builder import EXPLANATION_LEVELS, EXPLANATION_DEFAULT

    lvl = (str(level).strip().lower() if level is not None else "") or None
    if lvl is not None and lvl not in EXPLANATION_LEVELS:
        return {"error": f"level must be one of {sorted(EXPLANATION_LEVELS)} (or null to reset)"}

    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
    if not row:
        return {"error": "Host not found"}
    bp = row["behavioral_params"] or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = {}
    bp = dict(bp or {})
    if lvl:
        bp["explanation_level"] = lvl
    else:
        bp.pop("explanation_level", None)
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $2::jsonb WHERE id = $1",
        entity_id, json.dumps(bp),
    )
    return {"ok": True, "level": lvl or EXPLANATION_DEFAULT}


async def list_tasks_service(pool, entity_id: int, status=None, limit: int = 100,
                             offset: int = 0, project_id: Optional[int] = None) -> dict:
    """List entity tasks for review + per-status counts. Shape unchanged from
    GET /manage/tasks.

    `project_id` narrows the list AND the counts to one project. A factory
    build and a compare stamp the project at the top of `result`
    (tools._factory_build_task_start, tools._fan_out_task_start); a
    dispatched agent carries it only in `result.origin`
    (agent_dispatch._row_to_agent). Compared as text, so a row holding
    something else there never fails the query."""
    if not pool:
        return {"error": "Brain not initialized"}

    eid = entity_id
    scope = "WHERE entity_id = $1"
    params: list = [eid]
    if project_id is not None:
        params.append(str(int(project_id)))
        n = len(params)
        scope += (f" AND (result->>'project_id' = ${n}"
                  f" OR result->'origin'->>'project_id' = ${n})")
    scope_params = list(params)
    where = scope

    if status:
        where += f" AND status = ${len(params) + 1}"
        params.append(status)

    rows = await pool.fetch(f"""
        SELECT task_id, title, description, task_type, priority, status,
               created_by, created_at, completed_at
        FROM entity_tasks {where}
        ORDER BY created_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """, *params, limit, max(0, int(offset or 0)))

    stats = await pool.fetch(f"""
        SELECT status, COUNT(*) as cnt FROM entity_tasks {scope} GROUP BY status
    """, *scope_params)

    out = {
        "tasks": [
            {
                "task_id": r["task_id"],
                "title": r["title"],
                "description": r["description"][:200] if r["description"] else None,
                "task_type": r["task_type"],
                "priority": r["priority"],
                "status": r["status"],
                "created_by": r["created_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ],
        "stats": {r["status"]: r["cnt"] for r in stats},
        "total": sum(r["cnt"] for r in stats),
        "offset": max(0, int(offset or 0)),
        "next_offset": (max(0, int(offset or 0)) + len(rows)) if len(rows) == limit else None,
    }
    if project_id is not None:
        out["project_id"] = int(project_id)
    return out


def _agent_dispatch():
    """The runtime's AgentDispatch, or None before the brain is wired. Live
    dispatched-agent rows must cancel through it — a bare row-flip leaves the
    running asyncio task alive and unattributed."""
    runtime = getattr(brain_service, "_runtime", None)
    return getattr(runtime, "agent_dispatch", None)


async def purge_tasks_service(pool, entity_id: int, status: str = "queued",
                              action: str = "cancel",
                              actor: str = "task_manage_api") -> dict:
    """Cancel or delete the entity's own tasks by status. action='delete' hard-
    deletes; anything else cancels (status='cancelled'). Shape unchanged from
    POST /manage/tasks/purge."""
    if not pool:
        return {"error": "Brain not initialized"}

    eid = entity_id
    target_status = status or "queued"

    if action == "delete":
        result = await pool.fetch("""
            DELETE FROM entity_tasks WHERE entity_id = $1 AND status = $2 RETURNING task_id
        """, eid, target_status)
        return {"deleted": len(result), "status": target_status}
    else:
        # Live dispatched-agent rows go through AgentDispatch.cancel so the
        # running task actually stops and the actor lands on the record.
        agent_cancelled = 0
        if target_status in ("queued", "in_progress"):
            dispatch = _agent_dispatch()
            if dispatch is not None:
                # A watched MCP call runs in-process like an agent
                # (runtime/mcp_watch.py), so it is stopped the same way.
                rows = await pool.fetch(
                    "SELECT task_id FROM entity_tasks WHERE entity_id = $1 "
                    "AND status = $2 AND task_type IN ('agent', 'mcp_watch')",
                    eid, target_status)
                for r in rows:
                    await dispatch.cancel(eid, r["task_id"], actor=actor)
                    agent_cancelled += 1
        result = await pool.fetch("""
            UPDATE entity_tasks SET status = 'cancelled'
            WHERE entity_id = $1 AND status = $2 RETURNING task_id
        """, eid, target_status)
        return {"cancelled": len(result) + agent_cancelled,
                "status": target_status}


async def cancel_task_service(pool, entity_id: int, task_id: int,
                              actor: str = "task_manage_api") -> dict:
    """Cancel a single task of the entity's own. Shape unchanged from
    POST /manage/tasks/{task_id}/cancel."""
    if not pool:
        return {"error": "Brain not initialized"}

    # A live dispatched-agent row is a RUNNING agent, not a queue item: route
    # through AgentDispatch.cancel (stops the asyncio task, records actor).
    row = await pool.fetchrow(
        "SELECT task_type, status FROM entity_tasks "
        "WHERE task_id = $1 AND entity_id = $2",
        task_id, entity_id)
    if (row and row["task_type"] in ("agent", "mcp_watch")
            and row["status"] in ("queued", "in_progress")):
        dispatch = _agent_dispatch()
        if dispatch is not None:
            return await dispatch.cancel(entity_id, task_id, actor=actor)

    await pool.execute("""
        UPDATE entity_tasks SET status = 'cancelled'
        WHERE task_id = $1 AND entity_id = $2
    """, task_id, entity_id)
    return {"ok": True, "task_id": task_id}


async def delete_task_service(pool, entity_id: int, task_id: int) -> dict:
    """Delete a single task of the entity's own. Shape unchanged from
    POST /manage/tasks/{task_id}/delete."""
    if not pool:
        return {"error": "Brain not initialized"}

    await pool.execute("""
        DELETE FROM entity_tasks WHERE task_id = $1 AND entity_id = $2
    """, task_id, entity_id)
    return {"ok": True, "task_id": task_id}
