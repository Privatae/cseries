"""Which thread a messaging-app message belongs to, decided by the Host itself.

A message that points at a thread (a reply to a mapped message, a mapped
native thread, a command) is routed by the connector route in software. Every
other message comes here. The platform never continues a thread or opens a
new one on its own: a new thread, but never automatically; the host
asks itself which thread the message belongs to.

So the Host is asked, in one short call on its own chat model. It sees the
message and its open threads, newest first: the original conversation, open
Circles threads and projects that have a conversation, each with its title,
type, last activity and last exchange, and the thread this chat used last
marked. The list is cut to fit the model's context window. The call has one
tool, pick_thread, and nothing else in reach.

  pick_thread(thread="<kind>:<ref>", reason=...)      an existing thread
  pick_thread(new=true, title="...", reason=...)      a new thread

A failed pick (a model error, no call, a thread that was not in the list, a
call that names both or neither) runs no turn: the route asks the person
which thread, and never falls back to one of its own choosing.

The pick is visible. land_pick writes it into the chosen thread as a marker
turn, in the Host's words ("Telegram message, picked this thread: <reason>"),
the way adopt and split leave a line where they moved a conversation.
Nothing about the choice is hidden: the thread is the Host's chain of
thought.

THE WORKBENCH'S INDEX asks the same question about a message the person typed in the
Index box rather than on a page. Same single call and the same reading of the
answer; what differs is the list and the words:

  - the list is the pages the Index's own search ranks first for the message
    (at most INDEX_PICK_K, open or archived, with a conversation or not yet),
    and the page the person has open, marked, when the search did not rank it
    (list_index_candidates). Never every page: what the person pays for the
    call stays the same at 50 pages or 500;
  - the call's one tool is pick_page, and the instruction names the Index,
    never a messaging app (build_index_messages);
  - a new page is a new Workbench project, not a Circles thread, and the
    marker reads "Index message, picked this page: <reason>"
    (land_index_pick, index_marker_text).

routes/workbench_index.py runs the search and starts the turn on the page; an
archived pick is restored in the transaction that lands it. A failed pick, as
here, runs no turn.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("privatae.connectors.thread_pick")

PICK_TOOL_NAME = "pick_thread"

# The marker row's source_kind. Its own kind, not split's 'thread_marker':
# that one carries a `marker.thread_id` that thread deletion rewrites, and a
# pick names no thread to move to.
PICK_MARKER_KIND = "connector_pick"
# The Index's marker: the same furniture, from another surface. Left out of a
# conversation's last exchange like the connector's (_NOT_MARKER).
INDEX_PICK_MARKER_KIND = "index_pick"
INDEX_PICK_TOOL_NAME = "pick_page"
# Circles as one conversation: the
# marker a topic switch leaves in the topic the message went to, and the
# pick's one tool. See "Circles: one conversation" below.
TOPIC_MARKER_KIND = "topic_pick"
TOPIC_PICK_TOOL_NAME = "pick_topic"
# How many of the Index search's best pages the Host is shown for a message
# typed in the Index. The list, not the Host's history,
# sets the call's input: at most this many pages plus the open one, each a
# title and a clipped last exchange, so the call costs the same at 50 pages or
# 500. A page outside them cannot be picked (read_pick checks the list); the
# person can still open any page from the Index.
INDEX_PICK_K = 12
# How much of a message typed in the Index the pick's call carries. Where a
# message belongs shows in its opening; a long paste is then paid for once,
# by the turn that reads it whole on the page, not a second time by the
# pick. The turn always gets the whole message.
INDEX_PICK_MESSAGE_CHARS = 2_000

# What the route sends the person when the pick fails. No turn runs.
ASK_WHICH_THREAD = ("Which thread is this for? Send /threads to choose one, "
                    "or /new to start a new one.")

# When the model's context window is not known, the list is cut to this.
# Every model a Host can be routed to today has at least this much.
_UNKNOWN_WINDOW_TOKENS = 16_000

# How much of each side of a thread's last exchange the Host sees.
_EXCHANGE_CHARS = 280
_TITLE_CHARS = 120

PLATFORM_NAMES = {"telegram": "Telegram", "discord": "Discord", "slack": "Slack"}

PICK_TOOL = {
    "type": "function",
    "function": {
        "name": PICK_TOOL_NAME,
        "description": (
            "Say which of your threads this message belongs to: an existing "
            "thread by its id from the list, or a new thread with a title."),
        "parameters": {
            "type": "object",
            "properties": {
                "thread": {"type": "string",
                           "description": "The id in brackets from the list, e.g. 'thread:6f1c…' "
                                          "or 'project:12'."},
                "new": {"type": "boolean",
                        "description": "true to start a new thread for this message."},
                "title": {"type": "string",
                          "description": "With new=true: the new thread's title, a few words."},
                "reason": {"type": "string",
                           "description": "One short sentence: why this thread. It is written into "
                                          "the thread, where the person reads it."},
            },
            "required": ["reason"],
        },
    },
}

# The Index's tool: the same shape in the Workbench's words. A page is a
# project; a new page is a new project.
INDEX_PICK_TOOL = {
    "type": "function",
    "function": {
        "name": INDEX_PICK_TOOL_NAME,
        "description": (
            "Say which of your pages this message belongs to: an existing "
            "page by its id from the list, or a new page with a title."),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "string",
                         "description": "The id in brackets from the list, e.g. 'project:12'."},
                "new": {"type": "boolean",
                        "description": "true to start a new page (a new project) for this message."},
                "title": {"type": "string",
                          "description": "With new=true: the new page's title, a few words."},
                "reason": {"type": "string",
                           "description": "One short sentence: why this page. It is written on the "
                                          "page, where the person reads it."},
            },
            "required": ["reason"],
        },
    },
}

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


def build_messages(message: str, candidates: list[Candidate], *, platform: str,
                   current: Optional[tuple[str, str]] = None) -> list[dict]:
    cur_key = f"{current[0]}:{current[1]}" if current else None
    listing = "\n".join(_block(c, c.key == cur_key) for c in candidates)
    system = (
        f"A message just arrived from {platform_name(platform)}, sent by the person you work "
        "for. Before you answer it, decide which of your threads it belongs to.\n\n"
        "Your open threads, most recently active first:\n\n"
        f"{listing}\n\n"
        "Call pick_thread once:\n"
        "- pick_thread(thread=\"<the id in brackets>\", reason=\"...\") when the message "
        "continues one of these threads;\n"
        "- pick_thread(new=true, title=\"...\", reason=\"...\") when it starts something "
        "none of them covers.\n"
        "The reason is one short sentence. It is written into the thread you pick, "
        "where the person reads it.")
    return [{"role": "system", "content": system},
            {"role": "user", "content": str(message or "")}]


def index_pick_copy(message: str) -> tuple[str, int]:
    """The pick's copy of a message typed in the Index: the whole message
    when it has at most INDEX_PICK_MESSAGE_CHARS characters; else its opening,
    cut at the last space within the final 200 of them (at the limit when
    there is none) and ended with "…". Returns (the copy, how many characters
    of the message it leaves out; 0 when whole)."""
    text = str(message or "")
    if len(text) <= INDEX_PICK_MESSAGE_CHARS:
        return text, 0
    cut = text[:INDEX_PICK_MESSAGE_CHARS]
    space = cut.rfind(" ", INDEX_PICK_MESSAGE_CHARS - 200)
    if space > 0:
        cut = cut[:space]
    cut = cut.rstrip()
    return cut + "…", len(text) - len(cut)


def build_index_messages(message: str, candidates: list[Candidate], *,
                         current: Optional[tuple[str, str]] = None) -> list[dict]:
    """The Index's call: the person typed `message` into the Workbench's
    Index, on no page; the list is the pages the Index found for it
    (list_index_candidates). The message is the user turn, verbatim, as on a
    connector, up to INDEX_PICK_MESSAGE_CHARS (index_pick_copy); a longer one
    is its opening, and the instruction says how much more follows."""
    cur_key = f"{current[0]}:{current[1]}" if current else None
    listing = "\n".join(_block(c, c.key == cur_key, _INDEX_CURRENT) for c in candidates)
    shown, left_out = index_pick_copy(message)
    longer = (f"\n\nThe message is long: you are shown its opening, and {left_out:,} more characters "
              "follow. The page you pick gets the whole message." if left_out else "")
    system = (
        "The person you work for typed this message into the Workbench's Index, not on any "
        "page. Before you answer it, decide which of your pages it belongs to. Each page is "
        "one of your projects.\n\n"
        "The pages the Index found for this message, best match first:\n\n"
        f"{listing or '(none)'}\n\n"
        "Call pick_page once:\n"
        "- pick_page(page=\"<the id in brackets>\", reason=\"...\") when the message belongs "
        "on one of these pages; an archived page is brought back when you pick it;\n"
        "- pick_page(new=true, title=\"...\", reason=\"...\") when it starts something none "
        "of them covers. That makes a new project.\n"
        "The reason is one short sentence. It is written on the page you pick, where the "
        f"person reads it.{longer}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": shown}]


# ── reading the answer ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Shape:
    """What a pick is called on one surface: the tool, the argument that
    names an existing conversation, and what a new one is."""
    tool: str
    id_arg: str
    noun: str
    new_kind: str
    new_title: str


_THREAD = _Shape(tool=PICK_TOOL_NAME, id_arg="thread", noun="thread", new_kind="thread",
                 new_title="New thread")
_PAGE = _Shape(tool=INDEX_PICK_TOOL_NAME, id_arg="page", noun="page", new_kind="project",
               new_title="New page")


def _args_of(call: Any, tool: str = PICK_TOOL_NAME) -> Optional[dict]:
    fn = call.get("function") if isinstance(call, dict) else None
    if not isinstance(fn, dict) or fn.get("name") != tool:
        return None
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _choice(args: dict, id_arg: str = "thread") -> tuple:
    return (str(args.get(id_arg) or "").strip(), args.get("new") is True)


def read_pick(result: Any, candidates: list[Candidate], *, message: str = "",
              shape: _Shape = _THREAD) -> Pick:
    """The model's answer as a Pick, checked against the list it was shown.

    Only a structured pick_thread call is a pick. Every lane the chat turn
    runs on sends tools on the wire (provider_gateway: tools with
    tool_choice "auto" on the OpenAI wire, tool_use on Anthropic's), and the
    turn itself reads no tool call out of text. Reading one out of the text
    here also read the message: the message is the user turn, a model that
    answers it may quote it, and a quoted {"new": true} opened a thread the
    Host never picked. A reply with no call asks the person.

    `shape` is the surface's tool and words: a connector's pick_thread (the
    default), or the Index's pick_page, whose new page is a project."""
    tool, noun = shape.tool, shape.noun
    if not isinstance(result, dict):
        return Pick(ok=False, failure="no answer from the model")
    if result.get("error"):
        return Pick(ok=False, failure=f"model error: {_clip(result.get('error'), 200)}")
    picks = [a for a in (_args_of(c, tool) for c in (result.get("tool_calls") or [])) if a is not None]
    if not picks:
        return Pick(ok=False, failure=f"the model did not call {tool}")
    if len({_choice(a, shape.id_arg) for a in picks}) > 1:
        # Taking one of two different answers would be the platform's pick.
        return Pick(ok=False, failure=f"the model called {tool} with different answers")
    args = picks[0]
    reason = _clip(args.get("reason"), 300)
    named = str(args.get(shape.id_arg) or "").strip()
    new = args.get("new") is True
    if new and named:
        return Pick(ok=False, failure=f"the pick named a {noun} and asked for a new one")
    if new:
        first_line = str(message or "").strip().splitlines()[0] if str(message or "").strip() else ""
        title = _clip(args.get("title"), _TITLE_CHARS) or _clip(first_line, 60) or shape.new_title
        return Pick(ok=True, kind=shape.new_kind, title=title, new=True, reason=reason)
    if not named:
        return Pick(ok=False, failure=f"the pick named no {noun}")
    cand = {c.key: c for c in candidates}.get(named)
    if cand is None:
        return Pick(ok=False, failure=f"the pick named a {noun} that was not in the list: {named!r}")
    return Pick(ok=True, kind=cand.kind, ref=cand.ref, title=cand.title, reason=reason)


# ── the call ─────────────────────────────────────────────────────────────────

CallModel = Callable[..., Awaitable[dict]]


async def _ask_host(pool, entity_id: int, *, message: str, build, candidates, tool: dict,
                    shape: _Shape, call_model: Optional[CallModel], route: Optional[dict],
                    what: str) -> tuple[Pick, list[Candidate]]:
    """The one short call both surfaces make: the chat route's model, the
    list cut to its window, one tool. `build(cands)` makes the messages,
    `candidates(budget)` the list. Returns the Pick and the list the Host was
    shown ([] when no list was read). Never raises."""
    shown: list[Candidate] = []
    try:
        from mastercode.context_budget import est_tokens, model_context_window, reply_reserve
        from mastercode.inference_config import chosen_effort, resolve_role_route, route_effort

        route = route or await resolve_role_route(pool, entity_id, "chat")
        if not route.get("model"):
            return Pick(ok=False, failure=route.get("unconfigured") or "no chat model is set up"), shown
        window = await model_context_window(route["model"], route.get("provider"))
        if not window:
            # A cold catalog, as the chat turn reads it (runtime/session.py,
            # the route's window): fill the route's rows once, then ask
            # again, so a known model is never sized as unknown.
            try:
                from server.routes.inference import warm_route_catalog
                await warm_route_catalog(route.get("provider"))
                window = await model_context_window(route["model"], route.get("provider"))
            except Exception as e:  # noqa: BLE001 — the fallback size below still applies
                log.debug("route catalog warm failed (thread pick): %s", e)
        window = window or _UNKNOWN_WINDOW_TOKENS
        bare = build([])
        budget = window - reply_reserve(window) - sum(est_tokens(m["content"]) for m in bare)
        shown = await candidates(max(budget, 0))
        messages = build(shown)
        if call_model is None:
            from mastercode.runtime.tools import _call_entity_model as call_model
        result = await call_model(
            messages, temperature=0.3, tools=[tool],
            provider=route["provider"], provider_model=route["model"],
            base_url=route.get("base_url"),
            fallback_provider=route.get("fallback_provider"),
            fallback_model=route.get("fallback_model"),
            fallback_base_url=route.get("fallback_base_url"),
            reasoning_effort=route_effort(route),
            chosen_effort=chosen_effort(route),
            reasoning_inline=route.get("reasoning_inline") is True,
        )
        pick = read_pick(result, shown, message=message, shape=shape)
    except Exception as e:  # noqa: BLE001 — a failed pick asks the person, never guesses
        log.warning("%s failed (entity %s): %s", what, entity_id, e)
        return Pick(ok=False, failure=f"pick failed: {_clip(e, 200)}"), shown
    if not pick.ok:
        log.info("%s refused (entity %s): %s", what, entity_id, pick.failure)
    return pick, shown


async def choose_thread(pool, entity_id: int, *, message: str, platform: str,
                        current: Optional[tuple[str, str]] = None,
                        call_model: Optional[CallModel] = None,
                        route: Optional[dict] = None) -> Pick:
    """Ask the Host which thread `message` belongs to. Never raises: any
    failure is a Pick with ok=False, and the route runs no turn."""
    pick, _shown = await _ask_host(
        pool, entity_id, message=message,
        build=lambda cands: build_messages(message, cands, platform=platform, current=current),
        # Looked up when called, so the module's list_candidates is the one used.
        candidates=lambda budget: list_candidates(pool, entity_id, budget_tokens=budget, current=current),
        tool=PICK_TOOL, shape=_THREAD, call_model=call_model, route=route,
        what="connector thread pick")
    return pick


async def choose_page(pool, entity_id: int, *, message: str, pages: list,
                      current: Optional[tuple[str, str]] = None,
                      call_model: Optional[CallModel] = None,
                      route: Optional[dict] = None) -> tuple[Pick, list[Candidate]]:
    """Ask the Host which Workbench page a message typed in the Index belongs
    to: `pages` the Index's own search ranked first for the message (project
    ids, best first; the first INDEX_PICK_K are shown), `current` the page the
    person has open ("project", "<id>"), shown too. Returns the Pick and the
    pages the Host was shown, which the route hands back when the pick failed
    so the person can choose. Never raises: a failure is a Pick with
    ok=False, and no turn runs."""
    return await _ask_host(
        pool, entity_id, message=message,
        build=lambda cands: build_index_messages(message, cands, current=current),
        candidates=lambda budget: list_index_candidates(pool, entity_id, pages, budget_tokens=budget,
                                                        current=current),
        tool=INDEX_PICK_TOOL, shape=_PAGE, call_model=call_model, route=route,
        what="index page pick")


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
# thread: every message goes through the same one-call pick the connectors
# and the Index use, and the Host says which TOPIC it belongs to. A topic is
# an ordinary Circles thread (a circles_threads row, or the original
# conversation), so history, recall, turn locks and Clear all keep working
# per topic, and two topics run their turns at the same time (turn_registry:
# one running turn per thread key).
#
# The pick is CONSERVATIVE. A topic's history is what the turn reads, so
# joining the wrong one puts an unrelated conversation in front of the model.
# So, in this order:
#   1. A message sent to the root thread stays there; no pick runs
#      (runtime/root_turn). The root thread is never offered either.
#   2. A bare reply ("yes", "do that", "ok go ahead") stays on the topic the
#      person was just in (is_short_reply); no pick runs. A longer one that
#      opens like a reply ("ok go ahead with the second one", but also "also
#      remind me to call mom") is not assumed to: the Host is shown the topic
#      in use as the one it may join (is_follow_on), and unsure is new.
#   3. Otherwise the search scores every topic for the message (topic_scores)
#      and a topic may be JOINED only on a clear match: a score of at least
#      JOIN_SCORE and a lead of at least JOIN_MARGIN over the runner-up
#      (clear_match). The Host is shown that one topic and may join it or
#      start a new one; with no clear match it is shown none and can only
#      start a new one. It is never asked to choose between close calls.
#   4. A failed pick starts a new topic, titled from the message. Never a
#      best-guess join.
# The other topics the search found go to the turn's frame as one-line
# pointers (title and date, related_topics) that recall can open. Their turns
# are never put in the history. The marker says which way the pick went
# ("continuing: <topic>, <date>" or "new topic: <title>"), so a wrong pick is
# visible in the feed.

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

TOPIC_PICK_TOOL = {
    "type": "function",
    "function": {
        "name": TOPIC_PICK_TOOL_NAME,
        "description": (
            "Say which topic of your conversation with the person this message belongs "
            "to: the topic in the list when the message continues it, else a new topic "
            "with a title."),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string",
                          "description": "The id in brackets from the list, e.g. 'thread:6f1c…'."},
                "new": {"type": "boolean",
                        "description": "true when the message is not plainly a continuation of "
                                       "the listed topic."},
                "title": {"type": "string",
                          "description": "With new=true: the new topic's title, a few words."},
                "reason": {"type": "string", "description": "One short sentence: why."},
            },
            "required": ["reason"],
        },
    },
}

_TOPIC = _Shape(tool=TOPIC_PICK_TOOL_NAME, id_arg="topic", noun="topic", new_kind="thread",
                new_title=_TOPIC_NEW_TITLE)
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


def build_topic_messages(message: str, candidates: list[Candidate], *,
                         continuing: bool = False) -> list[dict]:
    """The pick's call for a message typed in Circles: the one topic the
    search found a clear match in, if any, and the message (its opening when
    long, index_pick_copy). `continuing`: the topic shown is the one the
    person was just in, offered because the message opens like a reply to
    it (is_follow_on), not because the search matched it."""
    listing = "\n".join(_block(c, False) for c in candidates)
    shown, left_out = index_pick_copy(message)
    longer = (f"\n\nThe message is long: you are shown its opening, and {left_out:,} more characters "
              "follow. The topic you pick gets the whole message." if left_out else "")
    if candidates and continuing:
        what = ("The person's last messages were in this topic, and this message opens like a "
                f"reply to it:\n\n{listing}\n\n"
                "Call pick_topic once:\n"
                "- pick_topic(topic=\"<the id in brackets>\", reason=\"...\") only when the message "
                "answers or continues what was just said there;\n"
                "- pick_topic(new=true, title=\"...\", reason=\"...\") when it brings up something "
                "else. When unsure, start a new topic.")
    elif candidates:
        what = ("The search found one earlier topic that matches this message clearly:\n\n"
                f"{listing}\n\n"
                "Call pick_topic once:\n"
                "- pick_topic(topic=\"<the id in brackets>\", reason=\"...\") only when the message "
                "plainly continues that topic;\n"
                "- pick_topic(new=true, title=\"...\", reason=\"...\") otherwise, including when it is "
                "only in the same broad area. When unsure, start a new topic.")
    else:
        what = ("No earlier topic matches this message clearly, so it starts a new topic.\n\n"
                "Call pick_topic(new=true, title=\"...\", reason=\"...\") once, with a title of a few "
                "words for it.")
    system = (
        "The person you work for wrote this message in Circles, your one conversation with "
        "them. They never choose a topic; you do. The topic's earlier messages are what you "
        f"will read with it, so only a clear continuation joins an earlier topic.\n\n{what}\n"
        f"The reason is one short sentence.{longer}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": shown}]


async def choose_topic(pool, entity_id: int, *, message: str, join: Optional[str],
                       call_model: Optional[CallModel] = None,
                       route: Optional[dict] = None,
                       continuing: bool = False) -> tuple[Pick, list[Candidate]]:
    """Ask the Host which topic a Circles message belongs to: the clear match
    `join` (a list key) or a new topic. Returns the Pick and what it was
    shown. Never raises."""
    async def cands(budget):
        if not join:
            return []
        scopes = await _topic_scopes(pool, entity_id, [join])
        return await _fit(pool, entity_id, [scopes[join]] if join in scopes else [],
                          budget_tokens=budget, current=None, current_note=_TOPIC_CURRENT)
    return await _ask_host(
        pool, entity_id, message=message,
        build=lambda c: build_topic_messages(message, c, continuing=continuing),
        candidates=cands, tool=TOPIC_PICK_TOOL, shape=_TOPIC, call_model=call_model, route=route,
        what="circles topic pick")


def _day(ts: Optional[datetime]) -> str:
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return f"{ts:%b} {ts.day}"


def topic_marker_text(*, new: bool, title: str, last_activity: Optional[datetime] = None) -> str:
    """The one line a topic switch leaves in the feed: which way the pick
    went, so a wrong one is seen."""
    if new:
        return f"new topic: {title}"
    day = _day(last_activity)
    return f"continuing: {title}" + (f", {day}" if day else "")


async def write_topic_marker(conn, entity_id: int, thread_id: Optional[str], text: str) -> int:
    """The switch, written into the topic the message went to, just before the
    message's own row: a marker turn the person and the Host both read, kept
    out of mining and of a topic's last exchange like the other markers."""
    from mastercode import circles_threads as ct
    md = {"role": "assistant", "source_kind": TOPIC_MARKER_KIND, "app_id": "circles",
          "source_context": ct.descriptor_for(thread_id)}
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


async def land_topic(pool, entity_id: int, pick: Pick, *, current_key: Optional[str],
                     last_activity: Optional[datetime] = None) -> TopicLanding:
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
                row = await ct.create_thread(conn, entity_id, title=pick.title, origin="user")
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
                await write_topic_marker(conn, entity_id, tid, marker)
    return TopicLanding(thread_id=tid, title=title, new=pick.new, moved=moved, marker=marker, picked=True)


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


async def pick_topic(pool, entity_id: int, message: str, *, current_thread_id: Optional[str] = None,
                     current_known: bool = True,
                     call_model: Optional[CallModel] = None,
                     route: Optional[dict] = None) -> TopicLanding:
    """Where a message typed in Circles goes (the one-conversation send); the
    rules are at the top of this section. `current_thread_id`: the topic the
    person was last in (None for the original conversation);
    `current_known` False: they have been in none this visit (a blank page),
    so nothing is a follow-on and any landing is a switch. Never raises for
    the pick; a database error propagates to the route, which keeps the
    message where it was."""
    from mastercode import circles_threads as ct
    from mastercode.runtime import root_turn

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
        # Opens like a reply but may carry a subject of its own: the Host
        # is shown the topic in use, unless another one clearly matches.
        replying = cur_key if title else None
    live = set((await _topic_scopes(pool, entity_id, [k for k, _ in scores])).keys())
    join = clear_match(scores, live)
    continuing = join is None and replying is not None
    if continuing:
        join = replying
    pick, shown = await choose_topic(pool, entity_id, message=message, join=join,
                                     call_model=call_model, route=route, continuing=continuing)
    if not pick.ok:
        # Unclear is a new topic, titled from the message: never a guess.
        first = str(message or "").strip().splitlines()[0] if str(message or "").strip() else ""
        pick = Pick(ok=True, kind="thread", new=True, title=_clip(first, 60) or _TOPIC_NEW_TITLE,
                    reason="", failure=pick.failure)
    chosen = next((c for c in shown if c.kind == pick.kind and c.ref == pick.ref), None)
    try:
        landing = await land_topic(pool, entity_id, pick, current_key=cur_key,
                                   last_activity=chosen.last_activity if chosen else None)
    except PickGone as e:
        log.info("circles topic pick landed nowhere (entity %s): %s", entity_id, e)
        landing = await land_topic(pool, entity_id, Pick(ok=True, kind="thread", new=True,
                                                         title=_clip(message, 60) or _TOPIC_NEW_TITLE),
                                   current_key=cur_key)
    key = _current_key(landing.thread_id)
    return replace(landing, failure=pick.failure,
                   related=await _related(pool, entity_id, scores, key))


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
