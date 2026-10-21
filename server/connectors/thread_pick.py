"""Which thread a message belongs to: placed in software, chosen by the Host in
its own turn when that is unclear.

A message that points at a thread (a reply to a mapped message, a mapped
native thread, a command) is routed by the connector route in software. Every
other message comes here, and no model is called here:

  - a CLEAR score-based match joins its thread (topic_scores / page_scores,
    then clear_match: a score of at least JOIN_SCORE and a lead of at least
    JOIN_MARGIN over the runner-up);
  - otherwise the message lands somewhere deterministic and the Host's own
    turn gets the candidate threads in its frame, with one tool to move the
    exchange to one of them or to a thread of its own
    (runtime/thread_choice.py, pick_thread). A messaging app's message lands
    in the thread the chat used last (a new thread for a chat the map has
    never seen); a message typed in the Workbench's Index lands on the page
    the person has open; a Circles message starts a topic of its own.

The pick is visible: the landing writes a marker in the thread it lands in,
and the Host's move writes another where it moved the exchange, the way adopt
and split leave a line where they moved a conversation.

This module once asked the Host which thread in one short call on its chat
model, before the turn (_ask_host). That call decided which history, thread
preferences, goal and summary the turn read, outside the turn, and its spend
was hidden from the activity feed.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("privatae.connectors.thread_pick")

# The marker row's source_kind. Its own kind, not split's 'thread_marker':
# that one carries a `marker.thread_id` that thread deletion rewrites, and a
# pick names no thread to move to.
PICK_MARKER_KIND = "connector_pick"
# The Index's marker: the same furniture, from another surface. Left out of a
# conversation's last exchange like the connector's (_NOT_MARKER).
INDEX_PICK_MARKER_KIND = "index_pick"
# Circles as one conversation: the
# marker a topic switch leaves in the topic the message went to. See
# "Circles: one conversation" below.
TOPIC_MARKER_KIND = "topic_pick"
# How many of the Index search's best pages are scored and offered for a
# message typed in the Index: at most this many pages plus the open one, so
# the work is the same at 50 pages or 500. The person can still open any page
# from the Index.
INDEX_PICK_K = 12
# How much of a message the search reads to score threads: where a message
# belongs shows in its opening. The turn always gets the whole message.
INDEX_PICK_MESSAGE_CHARS = 2_000
# What the turn's frame lists when the Host is to pick (runtime/thread_choice):
# the candidates' titles and last exchanges, at most this many tokens and
# thread_choice.CHOICE_MAX candidates.
CHOICE_BUDGET_TOKENS = 2_400

# What the route sends the person when the pick fails. No turn runs.
ASK_WHICH_THREAD = ("Which thread is this for? Send /threads to choose one, "
                    "or /new to start a new one.")

# How much of each side of a thread's last exchange the Host sees.
_EXCHANGE_CHARS = 280
_TITLE_CHARS = 120

PLATFORM_NAMES = {"telegram": "Telegram", "discord": "Discord", "slack": "Slack"}

# How the list marks the conversation the person is in.
_CHAT_CURRENT = "This chat's last messages went here"
_INDEX_CURRENT = "The person has this page open"


@dataclass(frozen=True)
class Candidate:
    """One thread the Host may pick."""
    kind: str                 # "thread" | "project" | "app"
    ref: str                  # thread uuid | project id | "circles"
    title: str
    type_name: Optional[str]
    last_activity: Optional[datetime]
    last_user: str
    last_host: str
    # An archived Workbench page (the Index lists them; a connector never
    # does). Picking one brings it back first.
    archived: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.ref}"


@dataclass(frozen=True)
class Pick:
    """The Host's answer. `ok` False means no turn runs; `failure` says why
    (for the log; the person gets ASK_WHICH_THREAD)."""
    ok: bool
    kind: str = ""
    ref: str = ""
    title: str = ""
    new: bool = False
    reason: str = ""
    failure: str = ""


@dataclass(frozen=True)
class Landing:
    kind: str
    ref: str
    title: str
    new: bool
    # The Index's pick only: an archived page brought back in the landing's
    # transaction (land_index_pick).
    restored: bool = False
    # The Index's pick under a request id only: the marker's row id
    # (write_index_marker), for mark_index_send_reattached.
    marker_id: Optional[int] = None


def platform_name(platform: str) -> str:
    p = str(platform or "").strip().lower()
    return PLATFORM_NAMES.get(p, p.capitalize() or "Messaging app")


def _clip(text: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _when(ts: Optional[datetime]) -> str:
    if ts is None:
        return "no messages yet"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return "last active " + ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── the Host's open threads ──────────────────────────────────────────────────

_PROJECT_IS_CONVERSATION = (
    "COALESCE(p.kind, 'project') = 'project' AND p.status <> 'archived' "
    "AND NOT (COALESCE(p.config, '{}'::jsonb) ? 'ticket' "
    "OR COALESCE(p.config, '{}'::jsonb) ? 'support_ticket')")

_NOT_MARKER = ("COALESCE(metadata->>'source_kind', '') NOT IN "
               f"('thread_marker', '{PICK_MARKER_KIND}', '{INDEX_PICK_MARKER_KIND}', "
               f"'{TOPIC_MARKER_KIND}')")

# The newest row of a set as one comparable value: the max of ARRAY[epoch,
# id] is the row `ORDER BY created_at DESC, id DESC` puts first, found by
# the aggregate of the pass that already reads the rows. A DISTINCT ON per
# thread read the whole turn heap once per thread (no index reaches a
# thread's rows): 151 threads over 291,000 turn rows made 155 queries and
# took about 18.5 s per message, measured on the kernel schema.
# Grouped, the list is three passes over the turn rows whatever the thread
# count: 5 queries, about 0.4 s on the same rows.
_NEWEST = "max(ARRAY[extract(epoch FROM created_at)::numeric, id::numeric])"


def _newest_by_role(also: str = "") -> str:
    """The newest person row and the newest Host row of each group, markers
    left out, as `newest_user` / `newest_host` ([epoch, id] or NULL). `also`
    narrows both to what the transcript shows when the group's own WHERE is
    wider than that."""
    cond = _NOT_MARKER + (f" AND {also}" if also else "")
    return (f"{_NEWEST} FILTER (WHERE metadata->>'role' = 'user' AND {cond}) AS newest_user, "
            f"{_NEWEST} FILTER (WHERE metadata->>'role' = 'assistant' AND {cond}) AS newest_host")


def _row_id(newest) -> Optional[int]:
    return int(newest[1]) if newest else None


async def _scopes(pool, entity_id: int) -> list[tuple]:
    """(kind, ref, title, type name, last activity, newest person row id,
    newest Host row id) for every open thread: every open Circles thread and
    every project with a conversation. Unsorted.

    One pass over the turn rows per kind of conversation. The newest
    exchange is read under each conversation's own transcript clause: a
    thread's is shown_where's thread arm with the ref lifted into the GROUP BY, the same
    lift circles_threads._turn_counts_by_thread makes (and the pg gate
    checks it against shown_where thread by thread); a project's is its
    project_id, as the Workbench reads it (routes/brain.py brain_chat)."""
    from mastercode import circles_threads as ct

    # No original conversation: the legacy app:circles
    # scope is never offered; a message lands in a thread or a project.
    out: list = []

    by_thread = {
        str(r["ref"]): r
        for r in await pool.fetch(
            "SELECT metadata->'source_context'->>'ref' AS ref, max(created_at) AS last_activity,"
            f"      {_newest_by_role(ct.NOT_REPLAY_ROW)}"
            "  FROM entity_memory"
            " WHERE entity_id = $1 AND memory_type = 'turn' AND status = 'active'"
            "   AND project_id IS NULL AND metadata->'source_context'->>'kind' = 'thread'"
            " GROUP BY 1", entity_id)
    }
    for r in await pool.fetch(
            "SELECT c.id, c.title, c.created_at, t.name AS type_name"
            "  FROM circles_threads c"
            "  LEFT JOIN turn_types t ON t.id = c.type_id AND t.entity_id = c.entity_id"
            " WHERE c.entity_id = $1 AND c.deleted_at IS NULL AND c.closed_at IS NULL",
            entity_id):
        tid = str(r["id"])
        g = by_thread.get(tid)
        out.append(("thread", tid, r["title"] or "Untitled thread", r["type_name"],
                    (g["last_activity"] if g else None) or r["created_at"],
                    _row_id(g["newest_user"]) if g else None,
                    _row_id(g["newest_host"]) if g else None))

    for r in await pool.fetch(
            "SELECT p.id, p.name, m.last_activity, m.newest_user, m.newest_host"
            "  FROM projects p"
            f"  JOIN ({_PROJECT_TURNS_GROUPED}) m ON m.project_id = p.id"
            f" WHERE p.entity_id = $1 AND {_PROJECT_IS_CONVERSATION}", entity_id):
        out.append(("project", str(r["id"]), f"Project “{r['name']}”", None, r["last_activity"],
                    _row_id(r["newest_user"]), _row_id(r["newest_host"])))
    return out


# Every project's conversation in one grouped pass over the turn rows: when it
# was last active and its newest exchange, read by project_id as the Workbench
# reads a project's thread (routes/brain.py brain_chat). $1 is the entity.
_PROJECT_TURNS_GROUPED = (
    "SELECT project_id, max(created_at) AS last_activity,"
    f"      {_newest_by_role()}"
    "  FROM entity_memory"
    " WHERE entity_id = $1 AND memory_type = 'turn' AND status = 'active'"
    "   AND project_id IS NOT NULL"
    " GROUP BY project_id")


def _page_newest_sql(cond: str = "", at: str = "em.created_at") -> str:
    """A page's newest live turn row (`cond` narrows it) by `at`, then id.
    Read backwards through one of v216's indexes, whose order `at` must be:
    idx_entity_memory_project_turns (created_at) for any row, and
    idx_entity_memory_project_replies (COALESCE(landed_at, created_at)) for
    the Host's replies. A page then costs a few index entries however long
    its conversation is; ordered otherwise, the planner sorts every reply of
    the page. `p` is the page."""
    return ("SELECT em.id, em.created_at FROM entity_memory em"
            " WHERE em.entity_id = $1 AND em.memory_type = 'turn' AND em.project_id = p.id"
            f"   AND em.status = 'active'{cond}"
            f" ORDER BY {at} DESC, em.id DESC LIMIT 1")


# The two sides of a page's last exchange, markers left out: the person's
# newest row (as _newest_by_role reads it for the grouped passes), and the
# Host's last landed reply (as the projects list reads it, _PAGE_MARKS_JOINS).
_PERSON_ROW = f" AND em.metadata->>'role' = 'user' AND {_NOT_MARKER}"
_HOST_ROW = f" AND em.metadata->>'role' = 'assistant' AND {_NOT_MARKER}"
_REPLY_AT = "COALESCE(em.landed_at, em.created_at)"


def _index_pages_sql() -> str:
    """The listed pages' rows: name, status, newest turn and the newest row
    of each side of the exchange. $1 entity, $2 project ids."""
    from server.routes.projects import _PROJECTS_ONLY_SQL

    return ("SELECT p.id, p.name, p.status, t.created_at AS last_activity,"
            "       u.id AS newest_user, h.id AS newest_host"
            "  FROM projects p"
            f"  LEFT JOIN LATERAL ({_page_newest_sql()}) t ON true"
            f"  LEFT JOIN LATERAL ({_page_newest_sql(_PERSON_ROW)}) u ON true"
            f"  LEFT JOIN LATERAL ({_page_newest_sql(_HOST_ROW, _REPLY_AT)}) h ON true"
            f" WHERE p.entity_id = $1 AND {_PROJECTS_ONLY_SQL} AND p.id = ANY($2::int[])")


async def _index_scopes(pool, entity_id: int, ids: list[int]) -> list[tuple]:
    """(kind, ref, title, type name, last activity, newest person row id,
    newest Host row id, archived) for each of `ids` that is one of the
    entity's Workbench pages (artifacts and other entities' projects are
    not), open or archived, with a conversation or without one yet, in the
    order of `ids`.

    Ticket projects are pages too: the book shows them, and the Index's
    search ranks them, so the Host may pick what the person sees there. (The
    connector's list leaves them out, _PROJECT_IS_CONVERSATION.)

    Each page's last activity and newest exchange are its own reads
    (_index_pages_sql), never a pass over every turn row of the Host: the
    list is a dozen pages, and its cost must not follow the Host's history."""
    if not ids:
        return []
    rows = await pool.fetch(_index_pages_sql(), entity_id, ids)
    by_id = {int(r["id"]): r for r in rows}
    out: list = []
    for pid in ids:
        r = by_id.get(pid)
        if r is None:
            continue
        out.append(("project", str(pid), r["name"], None, r["last_activity"],
                    r["newest_user"], r["newest_host"], r["status"] == "archived"))
    return out


async def _contents(pool, entity_id: int, ids: list[int]) -> dict[int, str]:
    """The text of the given rows, in one primary-key read."""
    if not ids:
        return {}
    rows = await pool.fetch(
        "SELECT id, content FROM entity_memory WHERE entity_id = $1 AND id = ANY($2::int[])",
        entity_id, ids)
    return {int(r["id"]): r["content"] for r in rows}


def _sort_key(scope) -> tuple:
    last = scope[4]
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (last is not None, last or datetime.min.replace(tzinfo=timezone.utc))


def _block(c: Candidate, current: bool, current_note: str = _CHAT_CURRENT) -> str:
    head = f"[{c.key}] {c.title}"
    if c.type_name:
        head += f" (type: {c.type_name})"
    if c.archived:
        head += " (archived)"
    head += f", {_when(c.last_activity)}"
    if current:
        head += f". {current_note}"
    lines = [head]
    if c.last_user:
        lines.append(f"  They said: {c.last_user}")
    if c.last_host:
        lines.append(f"  You said: {c.last_host}")
    return "\n".join(lines)


async def _fit(pool, entity_id: int, scopes: list[tuple], *, budget_tokens: int,
               current: Optional[tuple[str, str]], current_note: str) -> list[Candidate]:
    """`scopes` (sorted, newest first) as Candidates with their last
    exchange, as many as fit in `budget_tokens`; the current one is always
    kept, whatever the budget. A scope's eighth field, when it has one, says
    the page is archived."""
    from mastercode.context_budget import est_tokens

    text = await _contents(pool, entity_id, [i for s in scopes for i in s[5:7] if i is not None])
    cur_key = f"{current[0]}:{current[1]}" if current else None
    out: list[Candidate] = []
    used = 0
    for s in scopes:
        kind, ref, title, type_name, last, user_id, host_id = s[:7]
        is_current = f"{kind}:{ref}" == cur_key
        user = _clip(text.get(user_id), _EXCHANGE_CHARS) if user_id else ""
        host = _clip(text.get(host_id), _EXCHANGE_CHARS) if host_id else ""
        cand = Candidate(kind, ref, _clip(title, _TITLE_CHARS), type_name, last, user, host,
                         bool(s[7]) if len(s) > 7 else False)
        cost = est_tokens(_block(cand, is_current, current_note)) + 1
        if used + cost > budget_tokens and not is_current:
            continue
        out.append(cand)
        used += cost
    return out


async def list_candidates(pool, entity_id: int, *, budget_tokens: int,
                          current: Optional[tuple[str, str]] = None) -> list[Candidate]:
    """The open threads the Host is shown, newest first, as many as fit in
    `budget_tokens`. The thread this chat used last is always in the list
    while it is open, whatever the budget."""
    scopes = sorted(await _scopes(pool, entity_id), key=_sort_key, reverse=True)
    return await _fit(pool, entity_id, scopes, budget_tokens=budget_tokens, current=current,
                      current_note=_CHAT_CURRENT)


async def list_index_candidates(pool, entity_id: int, pages: list, *, budget_tokens: int,
                                current: Optional[tuple[str, str]] = None) -> list[Candidate]:
    """The pages the Host is shown for a message typed in the Index: the
    first INDEX_PICK_K of `pages` (project ids, best first: the Index's own
    search for the message, routes/workbench_index.py), then the page the
    person has open when it is not among them, each with its last exchange,
    as many as fit in `budget_tokens`. The open page is always kept, whatever
    the budget. An id that is not one of the entity's pages is dropped."""
    ids: list[int] = []
    for pid in list(pages or [])[:INDEX_PICK_K]:
        if int(pid) not in ids:
            ids.append(int(pid))
    if current and current[0] == "project":
        try:
            cur = int(current[1])
        except (TypeError, ValueError):
            cur = None
        if cur is not None and cur not in ids:
            ids.append(cur)
    scopes = await _index_scopes(pool, entity_id, ids)
    return await _fit(pool, entity_id, scopes, budget_tokens=budget_tokens, current=current,
                      current_note=_INDEX_CURRENT)


# ── landing it ───────────────────────────────────────────────────────────────

def marker_text(platform: str, pick: Pick) -> str:
    verb = "started this thread" if pick.new else "picked this thread"
    line = f"{platform_name(platform)} message, {verb}"
    return f"{line}: {pick.reason}" if pick.reason else f"{line}."


async def write_pick_marker(pool, entity_id: int, kind: str, ref: str, text: str, *,
                            platform: str) -> None:
    """The pick, written into the chosen thread as a marker turn the person
    and the Host both read. Stamped like split's marker: no embedding."""
    from mastercode import circles_threads as ct
    from mastercode import memory_engine

    project_id: Optional[int] = None
    md: dict = {"role": "assistant", "source_kind": PICK_MARKER_KIND,
                "connector": {"platform": str(platform or "")}}
    if kind == "thread":
        md["source_context"] = ct.descriptor_for(ref)
    elif kind == "project":
        project_id = int(ref)
        md["source_context"] = memory_engine.derive_source_context("chat", project_id=project_id)
    else:
        # app:circles included: there is no original conversation to mark.
        raise ValueError(f"not a connector thread: {kind}:{ref}")
    await pool.execute(
        "INSERT INTO entity_memory"
        "  (entity_id, content, memory_type, importance, status, source_session,"
        "   project_id, metadata)"
        " VALUES ($1, $2, 'turn', 0.1, 'active', $3, $4, $5::jsonb)",
        entity_id, text, f"connector:{platform}", project_id, json.dumps(md))


class PickGone(ValueError):
    """The thread the Host picked was closed, deleted or archived while it
    was choosing. Nothing was written; the route asks the person, as for a
    failed pick."""


async def _still_open(conn, entity_id: int, kind: str, ref: str) -> bool:
    """True when the picked conversation is still one the list would show,
    read FOR SHARE so a close, delete or archive (each an UPDATE of that
    row) waits for the marker's transaction instead of slipping between the
    check and the write."""
    if kind == "thread":
        return await conn.fetchval(
            "SELECT 1 FROM circles_threads WHERE id = $1::uuid AND entity_id = $2"
            " AND deleted_at IS NULL AND closed_at IS NULL FOR SHARE", ref, entity_id) is not None
    if kind == "project":
        return await conn.fetchval(
            f"SELECT 1 FROM projects p WHERE p.id = $1 AND p.entity_id = $2 AND {_PROJECT_IS_CONVERSATION}"
            " FOR SHARE", int(ref), entity_id) is not None
    return False


async def land_pick(pool, entity_id: int, pick: Pick, *, platform: str) -> Landing:
    """Make the Host's pick real: a new thread is created (a circles_threads
    row, origin 'user', the Host's title), then the marker is written into
    the thread picked. Only an ok Pick may land.

    One transaction: a marker that cannot be written leaves no new thread
    behind, and a thread the person closed or deleted (a project archived)
    during the pick is never landed in (design 4.2: a closed or deleted
    thread is never a landing place). That raises PickGone with nothing
    written."""
    if not pick.ok:
        raise ValueError("a failed pick lands nowhere")
    from mastercode import circles_threads as ct

    kind, ref, title = pick.kind, pick.ref, pick.title
    async with pool.acquire() as conn:
        async with conn.transaction():
            if pick.new:
                row = await ct.create_thread(conn, entity_id, title=pick.title, origin="user")
                kind, ref, title = "thread", str(row["id"]), row.get("title") or pick.title
            elif not await _still_open(conn, entity_id, kind, ref):
                raise PickGone(f"the picked thread is no longer open: {kind}:{ref}")
            await write_pick_marker(conn, entity_id, kind, ref, marker_text(platform, pick), platform=platform)
    return Landing(kind=kind, ref=ref, title=title, new=pick.new)


# ── landing the Index's pick ─────────────────────────────────────────────────

def index_marker_text(pick: Pick) -> str:
    """What the Index's pick writes on the page, in the Host's words."""
    verb = "started this page" if pick.new else "picked this page"
    line = f"Index message, {verb}"
    return f"{line}: {pick.reason}" if pick.reason else f"{line}."


_INDEX_MARKER_SQL = (
    "INSERT INTO entity_memory"
    "  (entity_id, content, memory_type, importance, status, source_session,"
    "   project_id, metadata)"
    " VALUES ($1, $2, 'turn', 0.1, 'active', $3, $4, $5::jsonb)")


async def write_index_marker(conn, entity_id: int, project_id: int, text: str, *,
                             request_id: Optional[str] = None, send: Optional[dict] = None) -> Optional[int]:
    """The Index's pick, written on the page it chose: a marker turn in the
    project's thread, which the Workbench reads by project_id. The same
    furniture as the connector's marker: no embedding, and left out of a page's last exchange
    (_NOT_MARKER).

    `request_id`: the book's id for the message this pick is for, stored as
    metadata.client_id: the key a nudge's user row keeps its sender's id under
    (session.push_nudge), so one index (v218) finds the marker, the user row
    that delivered the words and a nudge row alike; in PII mode its digest,
    under metadata.client_id_digest, as every row keeps it then
    (pii_queue.client_id_digest). `send`: what the answer to that send named,
    kept as metadata.index_send so a retry that finds the marker can give the
    same answer (find_index_send). Returns the marker's id when it is
    written for a request id (mark_index_send_reattached), else None."""
    from mastercode import memory_engine

    md = {"role": "assistant", "source_kind": INDEX_PICK_MARKER_KIND, "app_id": "workbench",
          "source_context": memory_engine.derive_source_context("chat", project_id=project_id)}
    if not request_id:
        await conn.execute(_INDEX_MARKER_SQL, entity_id, text, f"index:{project_id}", project_id, json.dumps(md))
        return None
    from mastercode import pii_queue
    if pii_queue.enabled():
        md[pii_queue.CLIENT_ID_DIGEST_KEY] = pii_queue.client_id_digest(request_id)
    else:
        md["client_id"] = request_id
    md["index_send"] = dict(send or {})
    return await conn.fetchval(_INDEX_MARKER_SQL + " RETURNING id",
                               entity_id, text, f"index:{project_id}", project_id, json.dumps(md))


async def land_index_pick(pool, entity_id: int, pick: Pick, *, new_project, restore=None,
                          request_id: Optional[str] = None, digest: Optional[str] = None) -> Landing:
    """Make the Index's pick real, in one transaction. A new page is a new
    Workbench project: `new_project(conn, title)` inserts it on this
    transaction's connection and returns its row. A page picked from the list
    is read FOR NO KEY UPDATE, so an archive or a delete cannot slip between
    the check and the marker; an archived one is brought back by
    `restore(conn, project_id)` on this transaction's connection (whatever it
    raises, a refusal included, leaves the page archived and nothing
    written). Then the marker is written on the page.

    One transaction, so nothing is left half-done: a page gone meanwhile
    raises PickGone with nothing written, and a marker that cannot be written
    leaves no new project and no restored page behind.

    `request_id` (the book's id for the message) and `digest` (of the words
    under it) go into the marker, in the same transaction as the landing, so
    a send that landed can be told from one that did not by what the database
    holds, whatever became of the process that made it (find_index_send)."""
    if not pick.ok:
        raise ValueError("a failed pick lands nowhere")
    from server.routes.projects import _PROJECTS_ONLY_SQL

    ref = pick.ref
    restored = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            if pick.new:
                row = await new_project(conn, pick.title)
                ref = str(row["id"])
            else:
                status = None
                if pick.kind == "project":
                    status = await conn.fetchval(
                        f"SELECT p.status FROM projects p WHERE p.id = $1 AND p.entity_id = $2"
                        f" AND {_PROJECTS_ONLY_SQL} FOR NO KEY UPDATE", int(ref), entity_id)
                if status is None:
                    raise PickGone(f"the picked page is gone: {pick.kind}:{ref}")
                if status == "archived":
                    if restore is None:
                        raise PickGone(f"the picked page is archived: {pick.kind}:{ref}")
                    await restore(conn, int(ref))
                    restored = True
            send = ({"title": pick.title, "restored": restored, "created": bool(pick.new),
                     "reason": pick.reason, "digest": digest} if request_id else None)
            marker_id = await write_index_marker(conn, entity_id, int(ref), index_marker_text(pick),
                                                 request_id=request_id, send=send)
    return Landing(kind="project", ref=ref, title=pick.title, new=pick.new, restored=restored,
                   marker_id=marker_id)


async def mark_index_send_reattached(pool, marker_id: int) -> None:
    """The send this marker is for attached to the turn already running on
    its page on the same words (turn_registry.start_or_attach, "reattached"):
    the words are that turn's, delivered by it, and no row of this send
    carries the request id. Kept on the marker (index_send.reattached), so a
    retry is answered from it (find_index_send) and never starts the words
    again once that turn is over."""
    await pool.execute(
        "UPDATE entity_memory SET metadata = jsonb_set(metadata, '{index_send,reattached}', 'true'::jsonb)"
        " WHERE id = $1 AND jsonb_typeof(metadata->'index_send') = 'object'", int(marker_id))


def _md(raw) -> dict:
    """A row's metadata as a dict, whether the pool's connection decodes jsonb
    or hands it back as text."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


# The rows that carry a request id. $1 entity, $2 the id (in PII mode its
# digest: pii_queue.client_id_key). A row keeps one id as itself and the ids of
# several messages joined into one turn as a list (session.row_client_id), under
# client_id, or in PII mode their digests under client_id_digest; containment
# of the one id matches either shape (a JSON array contains a string it holds).
# The expression is the one idx_entity_memory_client_id (v218) is on;
# scripts/test_workbench_index_pg.py holds the plan to it.
INDEX_SEND_ROWS_SQL = (
    "SELECT id, project_id, content, metadata FROM entity_memory"
    " WHERE entity_id = $1"
    " AND COALESCE(metadata->'client_id', metadata->'client_id_digest') @> to_jsonb($2::text)"
    " ORDER BY id")


async def _held_for_id(pool, entity_id: int, request_id: str, app_id=None) -> bool:
    """Whether the server holds words the Index's app (`app_id`) sent under
    `request_id` (pending_words: in a running turn, not read yet); another
    app's words under the same id are not the Index's (pending_words
    .sender_of). Never raises: False when unknown."""
    from mastercode.pending_words import sender_of
    try:
        return bool(await pool.fetchval(
            "SELECT 1 FROM pending_words WHERE entity_id = $1 AND client_id = $2"
            " AND client_app = $3 LIMIT 1",
            int(entity_id), request_id, sender_of(app_id)))
    except Exception as e:  # noqa: BLE001
        log.debug("index send: held words not read (non-fatal): %s", e)
        return False


async def find_index_send(pool, entity_id: int, request_id: str,
                          app_id: Optional[str] = None) -> Optional[dict]:
    """The Index send made under `request_id`, as the database holds it, when
    its words were delivered; else None (nothing landed, or the pick landed
    and the words never reached the page: that send runs again).

    The rows carrying the id (metadata.client_id, idx_entity_memory_client_id)
    are the marker land_index_pick wrote and the user row that delivered the
    words: the new turn's own (session.py's turn row) or, when the send went
    into a turn already running, the nudge's row (session.py's drain). The
    latest marker is the one the delivery followed: a send that landed and
    delivered nothing is run again, and its next landing writes another.
    Words a running turn ended without reading went on as the thread's next
    turn, whose user row keeps the id (alone, or in the list of the messages
    joined into it). A send that attached to the turn running on the page on
    the same words wrote no row: its marker says so (index_send.reattached,
    mark_index_send_reattached), and that answers too.

    Returns {project_id, marker (the marker's text), nudge (the words went
    into a running turn), reattached, title, restored, created, reason,
    digest}."""
    from mastercode import pii_queue
    rows = await pool.fetch(INDEX_SEND_ROWS_SQL, entity_id, pii_queue.client_id_key(request_id))
    marker = None
    delivery = None
    for r in rows:
        md = _md(r["metadata"])
        if md.get("source_kind") == INDEX_PICK_MARKER_KIND:
            marker = (r, md)
        elif md.get("role") == "user" and delivery is None:
            delivery = md
    if marker is None:
        return None
    row, md = marker
    send = md.get("index_send") if isinstance(md.get("index_send"), dict) else {}
    reattached = send.get("reattached") is True
    if delivery is None and not reattached:
        # The words went into the turn running on the page and the server
        # holds them (pending_words) until a turn has them, a restart
        # included: delivered as a nudge, so a retry never sends them again.
        if not await _held_for_id(pool, entity_id, request_id, app_id):
            return None
        delivery = {"nudge": True}
    return {"project_id": int(row["project_id"]), "marker": row["content"],
            "nudge": not reattached and (delivery or {}).get("nudge") is True, "reattached": reattached,
            "title": str(send.get("title") or ""),
            "restored": send.get("restored") is True, "created": send.get("created") is True,
            "reason": str(send.get("reason") or ""), "digest": send.get("digest")}


async def mark_index_ids_reattached(pool, entity_id: int, request_ids) -> None:
    """The sends made under `request_ids` (one id, or the ids of several
    messages joined into one turn, session.row_client_id) had their words go
    on as the thread's next turn, and those words were the same words as the
    turn already waiting there, so they attached to it (turn_registry
    ._requeue_undelivered, "reattached"): that turn delivers them, and no row
    of these sends carries their ids. The marker of each id says so, as
    mark_index_send_reattached does for a send that attached itself, so a late
    retry is answered from it (find_index_send) and never starts the words
    again. An id that has no marker changes nothing."""
    from mastercode import pii_queue
    for rid in ([request_ids] if isinstance(request_ids, str) else list(request_ids or [])):
        for r in await pool.fetch(INDEX_SEND_ROWS_SQL, entity_id, pii_queue.client_id_key(rid)):
            if _md(r["metadata"]).get("source_kind") == INDEX_PICK_MARKER_KIND:
                await mark_index_send_reattached(pool, r["id"])


# ── Circles: one conversation ────────────────────────────────────────────────
#
# Circles is one conversation. The person never opens, names or switches a
# thread: every message is placed by the same rules the connectors and the
# Index use, and the TOPIC it belongs to is a clear match or the Host's own
# pick in its turn. A topic is an ordinary Circles thread (a circles_threads
# row), so history, recall, turn locks and Clear all keep working per topic,
# and two topics run their turns at the same time (turn_registry: one running
# turn per thread key).
#
# The placement is CONSERVATIVE. A topic's history is what the turn reads, so
# joining the wrong one puts an unrelated conversation in front of the model.
# So, in this order, with no model call:
#   1. A message sent to the root thread stays there (runtime/root_turn). The
#      root thread is never offered either.
#   2. A bare reply ("yes", "do that", "ok go ahead") stays on the topic the
#      person was just in (is_short_reply).
#   3. The search scores every topic for the message (topic_scores) and a
#      topic is JOINED on a clear match: a score of at least JOIN_SCORE and a
#      lead of at least JOIN_MARGIN over the runner-up (clear_match).
#   4. Otherwise the message starts a topic of its own, untitled until the
#      Host names it (close_turn's title), and the topics it may still
#      continue go to the turn's frame as a thread choice
#      (runtime/thread_choice): the topic the person was just in when the
#      message opens like a reply to it (is_follow_on), and every topic that
#      scored at least JOIN_SCORE without a clear lead. The Host moves the
#      exchange to one of them with pick_thread, in its turn, or leaves it.
# The other topics the search found go to the turn's frame as one-line
# pointers (title and date, related_topics) that recall can open. Their turns
# are never put in the history. The marker says which way the placement went
# ("continuing: <topic>, <date>" or "new topic"), so a wrong one is visible in
# the feed.

TOPIC_RELATED_MAX = 5
_TOPIC_NEW_TITLE = "New topic"

# How a topic is scored for a message (topic_scores): the best of its turn
# rows among the search's hits (memory_retrieval.recall_memories over turn
# rows, the search `recall` and the pre-fire use). A row the lexical arm found
# scores 1.0: its query is plainto_tsquery, which needs EVERY content word of
# the message in that one row. A row only the semantic arm found scores its
# cosine relevance. Nothing else: rank alone is not evidence of a match.
#
# JOIN_SCORE: recall's own floor (memory_retrieval.RELEVANCE_FLOOR = 0.3) is
# where a semantic hit stops being noise, i.e. "may be relevant". Joining a
# topic loads its whole history into the turn, which needs far more than
# "may be relevant", so the bar is twice that floor. JOIN_MARGIN: two topics
# whose best rows are within 0.1 cosine of each other are the same broad area
# to the embedder, and the message is not clearly about either; it then
# starts a new topic. Neither number is measured on topic picks yet; a replay
# of real picks would measure them, and they are the two to move.
JOIN_SCORE = 0.6
JOIN_MARGIN = 0.1

# A follow-on reply (is_follow_on): at most FOLLOW_ON_SHORT_WORDS words, or
# at most FOLLOW_ON_MAX_WORDS opening with one of _FOLLOW_ON_OPENERS. Such a
# message carries no subject of its own, so the search has nothing to score;
# it answers whatever was just said.
FOLLOW_ON_SHORT_WORDS = 3
FOLLOW_ON_MAX_WORDS = 8
_FOLLOW_ON_OPENERS = frozenset({
    "yes", "yeah", "yep", "yup", "no", "nope", "ok", "okay", "k", "sure", "fine", "do", "don't",
    "dont", "go", "please", "thanks", "thank", "that", "that's", "thats", "this", "it", "it's",
    "and", "also", "but", "then", "sounds", "great", "perfect", "agreed", "right", "exactly",
    "correct", "same", "both", "neither", "option", "try", "keep", "stop", "continue", "again"})

_TOPIC_CURRENT = "The person's last messages were in this topic"


def _topic_key(kind: str, ref: str) -> str:
    return f"{kind}:{ref}"


def _current_key(current_thread_id: Optional[str]) -> str:
    """The list key of a topic: a thread's, or "" for none. There is no
    original conversation to stand in for a missing one."""
    from mastercode import circles_threads as ct
    tid = ct.canonical_id(current_thread_id)
    return _topic_key("thread", tid) if tid else ""


def is_short_reply(message: str) -> bool:
    """A bare reply ("yes", "do that"): too short to carry a subject of its
    own, so it stays on the topic just in use with no pick."""
    return len(re.findall(r"[\w']+", str(message or "").lower())) <= FOLLOW_ON_SHORT_WORDS


def is_follow_on(message: str) -> bool:
    """A short reply that carries no subject of its own ("yes", "do that",
    "ok go ahead with the second one"): it stays on the topic just in use."""
    words = re.findall(r"[\w']+", str(message or "").lower())
    if not words:
        return True
    if len(words) <= FOLLOW_ON_SHORT_WORDS:
        return True
    return len(words) <= FOLLOW_ON_MAX_WORDS and words[0] in _FOLLOW_ON_OPENERS


def _hit_strength(h: dict) -> float:
    if "lexical" in (h.get("matched") or []) or h.get("lex_rank") is not None:
        return 1.0
    try:
        return float(h.get("relevance") or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def topic_scores(pool, entity_id: int, message: str) -> list[tuple[str, float]]:
    """Every topic the search finds for `message`, as (list key, score), best
    first (the scoring is described at JOIN_SCORE). A search that cannot run
    finds nothing: the message then starts a new topic."""
    from mastercode.memory_retrieval import recall_memories

    text = str(message or "").strip()
    if not text:
        return []
    try:
        out = await recall_memories(pool, entity_id, text[:INDEX_PICK_MESSAGE_CHARS],
                                    limit=60, memory_types=["turn"])
    except Exception as e:  # noqa: BLE001 — no evidence, no join
        log.info("topic search unavailable (entity %s): %s", entity_id, e)
        return []
    best: dict[str, float] = {}
    for h in out.get("hits") or []:
        if h.get("project_id") is not None:
            continue
        md = _md(h.get("metadata"))
        if md.get("source_kind") in (TOPIC_MARKER_KIND, PICK_MARKER_KIND, "thread_marker"):
            continue
        kind, ref = h.get("sctx_kind"), h.get("sctx_ref")
        if not kind:
            sc = md.get("source_context") or {}
            kind, ref = sc.get("kind"), sc.get("ref")
        # Only topics score. The legacy app:circles scope (the old original
        # conversation) is no topic: a message is never joined to it.
        if kind == "thread" and ref:
            key = _topic_key("thread", str(ref))
        else:
            continue
        best[key] = max(best.get(key, 0.0), _hit_strength(h))
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))


def clear_match(scores: list[tuple[str, float]], live: Optional[set] = None) -> Optional[str]:
    """The one topic the message clearly belongs to, or None: the best score
    at least JOIN_SCORE and at least JOIN_MARGIN over the runner-up. `live`:
    the keys a pick may land in (a deleted or root thread's rows are not)."""
    if live is not None:
        scores = [kv for kv in scores if kv[0] in live]
    if not scores:
        return None
    top_key, top = scores[0]
    second = scores[1][1] if len(scores) > 1 else 0.0
    if top >= JOIN_SCORE and top - second >= JOIN_MARGIN:
        return top_key
    return None


def _topic_thread_sql() -> str:
    """The Host's live, non-root threads among $2 as topics: title, type,
    last activity and the newest row of each side, markers left out. Each a
    few index reads (v219's thread indexes, as circles_threads.list_threads
    reads them). $1 entity."""
    from mastercode import circles_threads as ct

    def newest(role: str) -> str:
        return ("SELECT em.id FROM entity_memory em WHERE " + ct._THREAD_ROWS
                + " AND (em.metadata->'source_context'->>'ref') = c.id::text"
                + f" AND (em.metadata->>'role') = '{role}' AND " + ct._NOT_MARKER
                + " ORDER BY em.created_at DESC, em.id DESC LIMIT 1")
    return (
        "SELECT c.id, c.title, c.closed_at, t.name AS type_name,"
        "       GREATEST(c.created_at, la.at) AS last_activity, u.id AS newest_user, h.id AS newest_host"
        "  FROM circles_threads c"
        "  LEFT JOIN turn_types t ON t.id = c.type_id AND t.entity_id = c.entity_id"
        f" LEFT JOIN LATERAL (SELECT max({ct._LANDED}) AS at FROM entity_memory em WHERE {ct._THREAD_ROWS}"
        "      AND (em.metadata->'source_context'->>'ref') = c.id::text) la ON true"
        f" LEFT JOIN LATERAL ({newest('user')}) u ON true"
        f" LEFT JOIN LATERAL ({newest('assistant')}) h ON true"
        " WHERE c.entity_id = $1 AND c.deleted_at IS NULL"
        "   AND c.builtin_type IS DISTINCT FROM 'root'"
        "   AND c.id = ANY($2::uuid[])")


async def _topic_scopes(pool, entity_id: int, keys: list[str]) -> dict[str, tuple]:
    """Scope tuples (as _fit reads them) for the live topics among `keys`;
    a deleted or root thread is absent."""
    ids = [k.split(":", 1)[1] for k in keys if k.startswith("thread:")]
    out: dict[str, tuple] = {}
    if ids:
        for r in await pool.fetch(_topic_thread_sql(), entity_id, ids):
            out[_topic_key("thread", str(r["id"]))] = (
                "thread", str(r["id"]), r["title"] or "Untitled topic", r["type_name"],
                r["last_activity"], r["newest_user"], r["newest_host"])
    return out


def _day(ts: Optional[datetime]) -> str:
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return f"{ts:%b} {ts.day}"


def topic_marker_text(*, new: bool, title: str, last_activity: Optional[datetime] = None) -> str:
    """The one line a topic switch leaves in the feed: which way the
    placement went, so a wrong one is seen."""
    if new:
        return f"new topic: {title}" if title else "new topic"
    day = _day(last_activity)
    return f"continuing: {title}" + (f", {day}" if day else "")


async def write_topic_marker(conn, entity_id: int, thread_id: Optional[str], text: str,
                             choice: Optional[list] = None) -> int:
    """The switch, written into the topic the message went to, just before the
    message's own row: a marker turn the person and the Host both read, kept
    out of mining and of a topic's last exchange like the other markers.
    `choice`: the topics the Host may still move the message to in its turn
    (thread_choice), kept on the marker so a send that asked for the topic
    first (POST /topic, then topic_picked) finds them (pending_choice)."""
    from mastercode import circles_threads as ct
    md = {"role": "assistant", "source_kind": TOPIC_MARKER_KIND, "app_id": "circles",
          "source_context": ct.descriptor_for(thread_id)}
    if choice:
        md["choice"] = list(choice)
    return await conn.fetchval(
        "INSERT INTO entity_memory"
        "  (entity_id, content, memory_type, importance, status, source_session,"
        "   project_id, metadata)"
        " VALUES ($1, $2, 'turn', 0.1, 'active', 'circles:topic', NULL, $3::jsonb)"
        " RETURNING id", entity_id, text, json.dumps(md))


@dataclass(frozen=True)
class TopicLanding:
    """Where a Circles message goes. `thread_id` None is the original
    conversation. `marker` is the line written for a switch ('' when the
    message stays in the topic the person was last in). `picked`: the Host
    decided (a join, a new topic, a follow-on kept); False only for the root
    thread, where nothing is decided, or when nothing at all could be
    written (the route keeps the message where it was)."""
    thread_id: Optional[str]
    title: str
    new: bool = False
    moved: bool = False
    marker: str = ""
    picked: bool = False
    root: bool = False
    follow_on: bool = False
    failure: str = ""
    related: tuple = ()
    # The topics the Host may still move the message to in its turn
    # (runtime/thread_choice), as frame candidates; () when the placement
    # was settled (a clear match, a follow-on, the root thread).
    choice: tuple = ()


async def land_topic(pool, entity_id: int, pick: Pick, *, current_key: Optional[str],
                     last_activity: Optional[datetime] = None,
                     choice: Optional[list] = None) -> TopicLanding:
    """Make the Host's topic pick real, in one transaction: a new topic is a
    new circles_threads row with the Host's title; a closed topic picked is
    opened again; a deleted one is never landed in (PickGone, nothing
    written). The marker is written when the topic changes (`current_key`
    None: the person has no topic yet this visit, so it always is)."""
    if not pick.ok:
        raise ValueError("a failed pick lands nowhere")
    from mastercode import circles_threads as ct

    async with pool.acquire() as conn:
        async with conn.transaction():
            if pick.new:
                row = await ct.create_thread(conn, entity_id, title=pick.title or None, origin="user")
                tid, title = str(row["id"]), row.get("title") or pick.title
            elif pick.kind == "app":
                # No topic is offered as app:circles any more; a pick naming
                # it lands like any gone topic: in a new one.
                raise PickGone("the original conversation is no topic")
            else:
                st = await conn.fetchrow(
                    "SELECT closed_at, builtin_type FROM circles_threads WHERE id = $1::uuid"
                    " AND entity_id = $2 AND deleted_at IS NULL FOR NO KEY UPDATE",
                    pick.ref, entity_id)
                if st is None or st["builtin_type"] is not None:
                    raise PickGone(f"the picked topic is gone: {pick.kind}:{pick.ref}")
                if st["closed_at"] is not None:
                    await conn.execute(
                        "UPDATE circles_threads SET closed_at = NULL WHERE id = $1::uuid AND entity_id = $2",
                        pick.ref, entity_id)
                tid, title = pick.ref, pick.title
            key = _current_key(tid)
            moved = key != current_key
            marker = ""
            if moved or pick.new:
                marker = topic_marker_text(new=pick.new, title=title, last_activity=last_activity)
                await write_topic_marker(conn, entity_id, tid, marker, choice=choice)
    return TopicLanding(thread_id=tid, title=title, new=pick.new, moved=moved, marker=marker, picked=True,
                        choice=tuple(choice or ()))


async def _related(pool, entity_id: int, scores: list[tuple[str, float]], chosen_key: str) -> tuple:
    """The other topics the search found, best first, as one-line pointers:
    {id, title, last_active}. Never their turns."""
    keys = [k for k, sc in scores if k != chosen_key and sc > 0][:TOPIC_RELATED_MAX * 2]
    if not keys:
        return ()
    scopes = await _topic_scopes(pool, entity_id, keys)
    out = []
    for k in keys:
        s = scopes.get(k)
        if s is None:
            continue
        out.append({"id": s[1], "title": _clip(s[2], _TITLE_CHARS),
                    "last_active": s[4].isoformat() if s[4] else None})
        if len(out) >= TOPIC_RELATED_MAX:
            break
    return tuple(out)


async def _title_of(pool, entity_id: int, key: str) -> str:
    scopes = await _topic_scopes(pool, entity_id, [key])
    return scopes[key][2] if key in scopes else ""


def candidate_dicts(cands: list) -> list[dict]:
    """Candidates as the turn's frame carries them (runtime/thread_choice)."""
    return [{"kind": c.kind, "ref": c.ref, "title": c.title,
             "last_activity": c.last_activity.isoformat() if c.last_activity else None,
             "last_user": c.last_user, "last_host": c.last_host,
             **({"archived": True} if c.archived else {})} for c in cands]


async def pick_topic(pool, entity_id: int, message: str, *, current_thread_id: Optional[str] = None,
                     current_known: bool = True) -> TopicLanding:
    """Where a message typed in Circles goes (the one-conversation send); the
    rules are at the top of this section. `current_thread_id`: the topic the
    person was last in; `current_known` False: they have been in none this
    visit (a blank page), so nothing is a follow-on and any landing is a
    switch. No model call: an unclear message starts a topic of its own and
    carries its candidate topics (TopicLanding.choice) for the Host's pick in
    its turn. A database error propagates to the route, which keeps the
    message where it was."""
    from mastercode import circles_threads as ct
    from mastercode.runtime import root_turn
    from mastercode.runtime.thread_choice import CHOICE_MAX

    cur = ct.canonical_id(current_thread_id)
    if cur and await root_turn.is_root_thread(pool, entity_id, cur):
        return TopicLanding(thread_id=cur, title=root_turn.ROOT_TITLE, root=True)
    cur_key = _current_key(cur) if current_known else None
    scores = await topic_scores(pool, entity_id, message)
    replying = None
    if cur_key and is_follow_on(message):
        title = await _title_of(pool, entity_id, cur_key)
        if title and is_short_reply(message):
            return TopicLanding(thread_id=cur, title=title, picked=True, follow_on=True,
                                related=await _related(pool, entity_id, scores, cur_key))
        # Opens like a reply but may carry a subject of its own: the topic in
        # use is offered to the Host, unless another one clearly matches.
        replying = cur_key if title else None
    scopes = await _topic_scopes(pool, entity_id, [k for k, _ in scores] + ([replying] if replying else []))
    live = set(scopes.keys())
    join = clear_match(scores, live)
    if join:
        s = scopes[join]
        landing = await land_topic(pool, entity_id, Pick(ok=True, kind="thread", ref=s[1], title=s[2]),
                                   current_key=cur_key, last_activity=s[4])
        return replace(landing, related=await _related(pool, entity_id, scores, join))
    keys: list[str] = [replying] if replying else []
    keys += [k for k, sc in scores if k in live and sc >= JOIN_SCORE and k not in keys]
    cands = await _fit(pool, entity_id, [scopes[k] for k in keys[:CHOICE_MAX] if k in scopes],
                       budget_tokens=CHOICE_BUDGET_TOKENS, current=None, current_note=_TOPIC_CURRENT)
    landing = await land_topic(pool, entity_id, Pick(ok=True, kind="thread", new=True, title=""),
                               current_key=cur_key, choice=candidate_dicts(cands))
    return replace(landing, related=await _related(pool, entity_id, scores, _current_key(landing.thread_id)))


async def pending_choice(pool, entity_id: int, thread_id: Optional[str]) -> list[dict]:
    """The candidate topics a topic made for one message still carries for the
    Host's pick (write_topic_marker `choice`), while nobody has written in it
    yet: what a send after POST /topic gives its turn. [] otherwise."""
    from mastercode import circles_threads as ct
    tid = ct.canonical_id(thread_id)
    if not tid:
        return []
    row = await pool.fetchrow(
        "SELECT metadata->'choice' AS choice FROM entity_memory"
        " WHERE entity_id = $1 AND memory_type = 'turn' AND status = 'active'"
        "   AND metadata->>'source_kind' = $2"
        "   AND metadata->'source_context'->>'kind' = 'thread' AND metadata->'source_context'->>'ref' = $3"
        "   AND NOT EXISTS (SELECT 1 FROM entity_memory u WHERE u.entity_id = $1 AND u.memory_type = 'turn'"
        "       AND u.metadata->>'role' = 'user' AND u.metadata->'source_context'->>'ref' = $3)"
        " ORDER BY id DESC LIMIT 1", entity_id, TOPIC_MARKER_KIND, tid)
    choice = row["choice"] if row is not None else None
    if isinstance(choice, str):
        try:
            choice = json.loads(choice)
        except ValueError:
            choice = None
    return list(choice) if isinstance(choice, list) else []


# ── messaging apps and the Index: placed in software ─────────────────────────

async def page_scores(pool, entity_id: int, message: str,
                      pages: Optional[list] = None) -> list[tuple[str, float]]:
    """Every Workbench page the search finds for `message`, as
    ("project:<id>", score), best first: the best of its turn rows among the
    hits, scored as topic_scores scores a topic (a lexical hit 1.0, else its
    cosine). `pages`: only these project ids (the Index's own list). A search
    that cannot run finds nothing."""
    from mastercode.memory_retrieval import recall_memories

    text = str(message or "").strip()
    if not text:
        return []
    try:
        out = await recall_memories(pool, entity_id, text[:INDEX_PICK_MESSAGE_CHARS],
                                    limit=60, memory_types=["turn"])
    except Exception as e:  # noqa: BLE001 — no evidence, no join
        log.info("page search unavailable (entity %s): %s", entity_id, e)
        return []
    allowed = {int(p) for p in pages} if pages is not None else None
    best: dict[str, float] = {}
    for h in out.get("hits") or []:
        pid = h.get("project_id")
        if pid is None or (allowed is not None and int(pid) not in allowed):
            continue
        md = _md(h.get("metadata"))
        if md.get("source_kind") in (INDEX_PICK_MARKER_KIND, PICK_MARKER_KIND, "thread_marker"):
            continue
        key = f"project:{int(pid)}"
        best[key] = max(best.get(key, 0.0), _hit_strength(h))
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))


@dataclass(frozen=True)
class Placement:
    """Where a messaging app's message goes: (kind, ref) and whether it is a
    new thread made for it, and the candidates the Host may move it to in its
    turn (() when settled)."""
    kind: str
    ref: str
    new: bool
    choice: tuple = ()
    provisional: bool = False


async def place_connector(pool, entity_id: int, *, message: str, platform: str,
                          current: Optional[tuple[str, str]] = None) -> Optional[Placement]:
    """A messaging app's message no reply, platform thread or command placed.
    A clear match among the open threads and projects joins it. Otherwise it
    lands in the thread this chat used last while that is open, else in a new
    thread made for it, and the open threads go to the turn's frame for the
    Host's pick (runtime/thread_choice). No model call. None only when no
    thread could be written (the route then asks the person)."""
    from mastercode.runtime.thread_choice import CHOICE_MAX
    scopes_all = await _scopes(pool, entity_id)
    open_keys = {f"{s[0]}:{s[1]}" for s in scopes_all}
    scores = sorted(await topic_scores(pool, entity_id, message) + await page_scores(pool, entity_id, message),
                    key=lambda kv: (-kv[1], kv[0]))
    join = clear_match(scores, open_keys)
    if join:
        kind, _, ref = join.partition(":")
        return Placement(kind=kind, ref=ref, new=False)
    cur_key = f"{current[0]}:{current[1]}" if current else None
    landed: Optional[tuple[str, str]] = current if cur_key in open_keys else None
    new = False
    if landed is None:
        first = str(message or "").strip().splitlines()[0] if str(message or "").strip() else ""
        try:
            landing = await land_pick(pool, entity_id, Pick(ok=True, kind="thread", new=True,
                                                            title=_clip(first, 60) or "New thread"),
                                      platform=platform)
        except PickGone:
            return None
        landed, new = (landing.kind, landing.ref), True
    # The candidates: the threads that scored at all first, then the most
    # recently active, as many as fit; never the one it landed in.
    order = {k: i for i, (k, _) in enumerate(scores)}
    landed_key = f"{landed[0]}:{landed[1]}"
    others = [s for s in scopes_all if f"{s[0]}:{s[1]}" != landed_key]
    scored = sorted((s for s in others if f"{s[0]}:{s[1]}" in order), key=lambda s: order[f"{s[0]}:{s[1]}"])
    rest = sorted((s for s in others if f"{s[0]}:{s[1]}" not in order), key=_sort_key, reverse=True)
    cands = await _fit(pool, entity_id, (scored + rest)[:CHOICE_MAX], budget_tokens=CHOICE_BUDGET_TOKENS,
                       current=None, current_note=_CHAT_CURRENT)
    return Placement(kind=landed[0], ref=str(landed[1]), new=new, choice=tuple(candidate_dicts(cands)),
                     provisional=new)


async def clear_topic_for(pool, entity_id: int, message: str) -> Optional[str]:
    """The live topic `message` clearly belongs to (clear_match over
    topic_scores, the pick's first step), as a thread id, or None. No model
    call: the net under a send that names no thread (brain_service), which
    otherwise starts a topic of its own."""
    scores = await topic_scores(pool, entity_id, message)
    live = set((await _topic_scopes(pool, entity_id, [k for k, _ in scores])).keys())
    join = clear_match(scores, live)
    return join.split(":", 1)[1] if join and join.startswith("thread:") else None


async def related_topics(pool, entity_id: int, message: str, *, thread_id: Optional[str]) -> tuple:
    """The topics the search finds for `message` besides the one it goes to
    (`thread_id`, None for the original conversation), as one-line pointers:
    what the turn's frame names when the page asked for the topic before
    sending (routes/circles.py `topic_picked`). No model call."""
    return await _related(pool, entity_id, await topic_scores(pool, entity_id, message),
                          _current_key(thread_id))
