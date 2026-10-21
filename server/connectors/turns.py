"""What the connector message route does with a message, and what it answers.

server/routes/connector.py is the HTTP side; this is the rest:

  WHERE A MESSAGE GOES (4.2), first answer wins:
    1. a command, handled here and never sent to the Host: /new, /threads,
       /thread <n>;
    2. a reply to a message the map knows (message_map), to that thread;
    3. a message in a platform thread the map knows, to that thread;
    4. everything else is placed in software (thread_pick.place_connector):
       a clear score-based match joins its thread; otherwise the message
       lands in the thread this chat used last (a new one for a new chat)
       and the Host's own turn may move it to one of the open threads its
       frame lists (runtime/thread_choice); the reply follows the move
       (_follow_move). No model call places a message. A thread that cannot
       be written runs no turn and asks the person (ASK_WHICH_THREAD).
    A thread the person deleted or closed is never a landing place: 2 and 3
    pointing at one fall through to 4.

  THE SEND. The turn runs as the person's OWN chat turn, the way Circles and
  the Workbench start theirs: app_id "circles" with the Circles thread's id,
  or with the project's id, or with neither for the original conversation
  (routes/circles.py send_message passes exactly that). Never the connector's
  app id, never history_context, so never an app:<connector> scope: the
  original conversation renders only rows tagged circles or untagged
  (memory_engine._turn_row_where's app_id arm), and a card's answer and a
  continuation start their turn from the room the asking turn recorded, which
  carries app_id/project_id/thread_id and no history_context
  (session.py's escalate: wakeup_delivery_room). The lock is the
  conversation's (turn_registry.compose_thread_key), so a message while that
  conversation's turn runs is a nudge into it, from whichever surface.

  THE MESSAGE the Host reads is the person's text under a short visible tag
  naming the platform, followed by a visible note on what a picture here can
  be (the measured subset, connectors/picture.py), the way the VS
  Code surface puts its editor context on the message. Nothing about it is
  hidden.

  WHAT GOES BACK (4.5): NDJSON, one part per line. The thread and the turn at
  once; when the turn ends, the reply as parts (reply_parts.build_reply_parts,
  pictures by Takumi in its confined child, never a browser); then done.
  A connector gets the reply only of a turn IT started (4.8): a turn the
  person, a card's answer or a timer started in the same thread is never
  handed to it.

  A TURN THAT HOLDS ON A CARD (4.8) sends what it has written so far and the
  card's notice the moment it starts holding (session.py's _hold_at_exit
  shows its 'waiting' card then), not after the hold: the hold lasts up to
  session.HOLD_S, and a person in a messaging app must not wait that long to
  learn a card is waiting on their own device. What the turn writes after
  the hold follows when it ends, numbered on from there.

  A NUDGE NOBODY COULD READ. A message the connector nudged into its own turn
  (4.5) is read by that turn, so its reply comes on the first stream. When
  that turn ended before it ever had a session, or ended without reading it
  (the allowance's stop, a model error: the connector
  keeps no copy to send again, session.end_turn_input), the registry sends
  the nudge on as the next turn, once (turn_registry._requeue_undelivered);
  the first stream follows that turn too, because it carries this
  connector's words.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from server import secret_cut
from server.connectors import message_map as mm
from server.connectors import thread_pick as tp

log = logging.getLogger("privatae.connectors.turns")

# How often a stream that is waiting says it is alive, so no client or proxy
# between the connector and its Host reads a long turn as a dead connection.
HEARTBEAT_S = 10.0

_COMMANDS = ("/new", "/threads", "/thread")
LIST_MAX = 30          # threads shown by /threads


# ── the header the Host reads ────────────────────────────────────────────────
#
# What a picture may hold, as measured for connectors/picture.py's allowlist
# (ALLOWED_TAGS, ALLOWED_PROPERTIES, _SELECTOR_RE, _ALLOWED_ATTRS): block and
# inline text tags, styled from a <style> block by one tag, one .class or one
# #id (a style= attribute and a compound selector are refused), flexbox,
# background-color and a linear-gradient background-image (the `background`
# shorthand is refused), colour, border and border-radius in px or em. Lists,
# tables, line breaks, italics, line-height, a named font, SVG, grid, images
# and script are off the list, so a visual that uses them goes as a link to
# the thread instead. The spellings are named exactly because the natural
# ones (inline style, `background:`) are refused.
# scripts/test_connector_route.py checks every word here against those lists.
PICTURE_GUIDANCE = (
    "A ```visual in your reply reaches them as a picture only if it is a simple card: "
    "div, p, span, h1-h6, strong and b, styled from one <style> block by tag, .class or "
    "#id alone (no style= attributes); display:flex, background-color, a linear-gradient "
    "background-image, color, border and border-radius in px or em. No lists, tables, SVG, "
    "grid, images, italics, line-height, font-family or script: anything else goes as a "
    "link to this thread.")


# The message reads: the platform, the person's words, then the picture note,
# all visible. The words come before the note because a message that lands in
# a running turn is drawn in every other open client from the registry's
# nudge frame, which keeps the first 500 characters (turn_registry._nudge):
# with the note first, the person's own words were cut from that bubble.

def message_header(platform: str, origin: "Optional[Origin]" = None) -> str:
    if origin is None:
        return f"[{tp.platform_name(platform)} message]"
    return f"[{tp.platform_name(platform)} message, {origin_label(origin)}]"


def message_for_host(platform: str, text: str, origin: "Optional[Origin]" = None) -> str:
    body = text if origin is None else quoted(text)
    return f"{message_header(platform, origin)}\n\n{body}\n\n[{PICTURE_GUIDANCE}]"


def sent_words(text: str) -> Optional[str]:
    """A connector's message as message_for_host built it, without the picture
    guidance (its header and the person's words), or None when `text` is not
    one. How the turn registry knows words it could not deliver came from a
    connector chat, which is then told in plain words
    (turn_registry._say_undelivered): only a
    forward carries connector_origin."""
    tail = f"\n\n[{PICTURE_GUIDANCE}]"
    t = (text or "").rstrip()
    return t[:-len(tail)].rstrip() if t.endswith(tail) else None


def quoted(text: str) -> str:
    """Someone else's words with every line quoted ("> "). The label sits
    above them, and the words are the original author's to write: unquoted,
    a line of them reading "[Telegram message]" started what looked like the
    person's own message after the forward. Every line break str.splitlines
    knows (\\r, \\v, \\f, \\x1c-\\x1e, \\x85, U+2028 and U+2029 as well as \\n)
    starts a quoted line, so none of them ends the quote."""
    return "\n".join(("> " + ln).rstrip() for ln in text.splitlines()) or ">"


# ── where a forwarded message came from ─────────────────────────────────────
#
# A message the person forwards (Telegram forward_origin, a Discord forward's
# message_snapshots, a Slack share) or sends through an inline bot (Telegram
# via_bot) was written by someone else: another person, a group, a channel, or
# another agent's bot. The connector reports that as `origin`, and the KERNEL
# writes the label: it is the message's header (what the Host reads and what
# the thread shows, since the header is the row's text) and the user row's
# metadata (connector_origin). The connector never writes the words itself,
# and nothing in the prompt is told how to treat it: the label says who wrote
# the text, structurally, the way the header already says which app it came
# from.
ORIGIN_KINDS = ("forward", "via_bot")
ORIGIN_SOURCES = ("user", "bot", "chat", "channel", "hidden")
_ORIGIN_SOURCE_WORDS = {"user": "a person", "bot": "a bot", "chat": "a group",
                        "channel": "a channel", "hidden": "an unnamed source"}


@dataclass(frozen=True)
class Origin:
    kind: str                  # forward | via_bot
    source: str                # user | bot | chat | channel | hidden
    name: str = ""             # cleaned as check_account cleans a display name
    id: Optional[str] = None   # the platform's id for the source, when it gave one

    def as_meta(self) -> dict:
        out = {"kind": self.kind, "source": self.source}
        if self.name:
            out["name"] = self.name
        if self.id:
            out["id"] = self.id
        return out


def parse_origin(raw: Any) -> Optional["Origin"]:
    """The connector's `origin`, validated, or None when it sent none.
    Refused (400) when it is anything but {kind, source, name?, id?} with a
    kind and a source from the lists above.

    The name and the id are cut of their secret shapes here, as the message's
    text is (parse_message): both are words of the header the pick, the turn
    and a nudge read (origin_label), and of the row's connector_origin."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise Refused(400, "origin must be an object: {kind, source, name?, id?}")
    kind, source = raw.get("kind"), raw.get("source")
    if kind not in ORIGIN_KINDS:
        raise Refused(400, f"origin.kind must be one of {', '.join(ORIGIN_KINDS)}")
    if source not in ORIGIN_SOURCES:
        raise Refused(400, f"origin.source must be one of {', '.join(ORIGIN_SOURCES)}")
    from server.network.nac_connector import clean_name
    oid = _id(raw.get("id"), "origin.id", False)
    return Origin(kind=kind, source=source,
                  name=secret_cut.redact_words(clean_name(raw.get("name")), door="connector", log=log),
                  id=secret_cut.redact_words(oid, door="connector", log=log) if oid else None)


def origin_label(origin: "Origin") -> str:
    """'forwarded from a bot “Hermes” (id 123)'. The name is the platform's
    report, so the characters that would end the header or open a quote of
    its own are taken out: every opening and closing bracket and quotation
    mark Unicode has (categories Ps, Pe, Pi, Pf: a fullwidth ］ closes the
    header to the eye as ] does), and the ASCII double quote."""
    verb = "forwarded from" if origin.kind == "forward" else "sent through"
    out = f"{verb} {_ORIGIN_SOURCE_WORDS[origin.source]}"
    name = "".join(c for c in origin.name
                   if c != '"' and unicodedata.category(c) not in ("Ps", "Pe", "Pi", "Pf"))
    name = re.sub(r"\s+", " ", name).strip()
    # parse_origin cut the name as it came. Taking characters out can join
    # what stood either side of them into a shape, so the name as shown is
    # cut again (cutting cut text changes nothing).
    name = secret_cut.redact_words(name, door="connector", log=log)
    if name:
        out += f" “{name}”"
    if origin.id:
        out += f" (id {origin.id})"
    return out


# ── the replies this route started ───────────────────────────────────────────

@dataclass
class Reply:
    """One answer of the route that a connector may reattach to and report
    sends against: a Host turn it started (state 'started'), or an answer
    the route wrote itself ('command', 'ask', 'refused', 'duplicate')."""
    reply_id: str
    app_id: str
    grant_id: Optional[int]
    platform: str
    chat_id: str
    state: str
    kind: Optional[str] = None
    ref: Optional[str] = None
    title: str = ""
    changed: bool = False
    lead: Optional[str] = None
    # Every part of the reply, numbered, once the reply is complete. The parts
    # sent before that (`early`, a turn holding on a card) are its first ones,
    # with the same seqs.
    parts: "asyncio.Future" = field(
        default_factory=lambda: asyncio.get_running_loop().create_future())
    early: list = field(default_factory=list)
    # Set, and replaced by a fresh one, each time `early` grows.
    more: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)   # revoked: say no more
    revoked: bool = False
    rec: Any = None                 # the turn registry's record, for a started turn
    builder: Optional[asyncio.Task] = None
    # The texts this connector nudged into the reply's turn (start_turn), and
    # the turns the reply followed after its first: a nudge sent on as a turn
    # of its own (_followed_turn). Each followed turn id answers for this
    # reply too (_REPLIES), and revoke stops it like the first.
    nudges: list = field(default_factory=list)
    followed: list = field(default_factory=list)
    # Turns another surface started that took this connector's words as a
    # nudge (_followed_turn): the reply follows them, but they are not this
    # connector's turns, so a revoke never stops them.
    joined: list = field(default_factory=list)

    def turn_ids(self) -> list[str]:
        return [self.reply_id, *self.followed]

    def thread_line(self) -> Optional[dict]:
        if not self.kind:
            return None
        return {"type": "thread", "kind": self.kind, "ref": self.ref, "title": self.title,
                "changed": self.changed}


@dataclass
class Nudge:
    """The message went into a turn already running in that thread (4.4): no
    second turn, no parts. `mine`: that turn is this connector's own, so its
    reply comes on the stream that started it. Otherwise `note` is what the
    connector tells the person (the reply is read in the Host, 4.8)."""
    kind: str
    ref: str
    title: str
    changed: bool
    turn_id: str
    mine: bool
    note: Optional[str] = None


@dataclass
class Again:
    """A message the route already took, whose reply is still held: it is
    streamed the way a reattach is (state running or done, never started),
    because no turn was started for it this time."""
    reply: Reply


_REPLIES: dict[str, Reply] = {}
_INFLIGHT: dict[tuple, asyncio.Task] = {}
# /threads as last shown to a chat, so /thread <n> means what the person saw.
_LISTINGS: dict[tuple, list[tuple[str, str, str]]] = {}
# A bare /new: the chat's next message starts a new thread.
_PENDING_NEW: set[tuple] = set()


def _ttl_s() -> float:
    from server.turn_registry import DONE_TTL_S
    return float(DONE_TTL_S)


def _keep(reply: Reply) -> None:
    """Hold the reply while a connector may reattach to it: until its parts
    exist, then as long as the turn registry keeps a finished turn."""
    _REPLIES[reply.reply_id] = reply

    def _drop() -> None:
        for tid in [*reply.turn_ids(), *reply.joined]:
            if _REPLIES.get(tid) is reply:
                _REPLIES.pop(tid, None)

    def _done(_f) -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().call_later(_ttl_s(), _drop)
    reply.parts.add_done_callback(_done)


def reply_for(caller, reply_id: str) -> Optional[Reply]:
    """This connector's own reply, or None: another app's, or another grant's
    of the same app (a re-pair), is not its."""
    r = _REPLIES.get(str(reply_id or ""))
    if r is None or r.app_id != caller.app_id or r.grant_id != caller.grant_id:
        return None
    return r


def _numbered(parts: list[dict]) -> list[dict]:
    out = []
    for i, p in enumerate(parts):
        q = {k: v for k, v in p.items() if k not in ("type", "seq")}
        out.append({"type": p["type"], "seq": i, **q})
    return out


def _immediate(caller, msg, state: str, texts: list, *, kind=None, ref=None, title="",
               changed=False, link: Optional[dict] = None) -> Reply:
    r = Reply(reply_id=uuid.uuid4().hex, app_id=caller.app_id, grant_id=caller.grant_id,
              platform=caller.platform, chat_id=msg.chat_id, state=state, kind=kind, ref=ref,
              title=title, changed=changed)
    parts = [{"type": "text", "text": t} for t in texts if t]
    if link:
        parts.append(link)
    r.parts.set_result(_numbered(parts))
    _keep(r)
    return r


# ── the message ──────────────────────────────────────────────────────────────

class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class Incoming:
    chat_id: str
    message_id: str
    text: str
    reply_to: Optional[str]
    native_thread_id: Optional[str]
    display_name: str
    origin: Optional[Origin] = None


_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
TEXT_MAX = 32 * 1024


def _id(v: Any, name: str, required: bool = True) -> Optional[str]:
    if v is None or v == "":
        if required:
            raise Refused(400, f"{name} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise Refused(400, f"{name} must be a string or a number")
    s = str(v).strip()
    if not _ID_RE.match(s):
        raise Refused(400, f"{name} must be the platform's id, as the platform sent it")
    return s


def parse_message(caller, body: dict) -> Incoming:
    """The connector's report of one message. The sender check comes FIRST:
    an account that is not the one the person approved writes nothing and
    runs nothing (403). The account is the grant's, from its signed terms.

    The message's text is cut of its secret shapes here, where it arrives, as
    the turn would cut it (server/secret_cut.py): the placement's search
    embeds it before any turn starts (thread_pick.place_connector), a thread
    made for it or by /new is named after its first line, and a nudge carries
    it into a running turn, so nothing downstream reads the words before they
    are cut."""
    want = str(caller.account_id or "")
    got = body.get("account_id")
    got = str(got).strip() if isinstance(got, (str, int)) and not isinstance(got, bool) else ""
    if not want or got != want:
        raise Refused(403, "this message is not from the account paired with this connector")
    chat_id = _id(body.get("chat_id"), "chat_id")
    message_id = _id(body.get("message_id"), "message_id")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refused(400, "text is required (a message with only a file sends its caption, "
                           "or says what it was)")
    if len(text) > TEXT_MAX:
        raise Refused(413, f"text is longer than {TEXT_MAX} characters")
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", str(body.get("display_name") or "")).strip()[:80]
    text = secret_cut.redact_words(text.strip(), door="connector", log=log)
    return Incoming(chat_id=chat_id, message_id=message_id, text=text,
                    reply_to=_id(body.get("reply_to_message_id"), "reply_to_message_id", False),
                    native_thread_id=_id(body.get("native_thread_id"), "native_thread_id", False),
                    display_name=name, origin=parse_origin(body.get("origin")))


def parse_command(text: str) -> Optional[tuple[str, str]]:
    """(command, the rest) for /new, /threads and /thread, else None. A
    Telegram group command can carry the bot's name (/new@my_bot)."""
    m = re.match(r"^(/[a-z]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", text.strip(), re.S)
    if not m or m.group(1) not in _COMMANDS:
        return None
    return m.group(1), (m.group(2) or "").strip()


# ── where things are ─────────────────────────────────────────────────────────

def _eid() -> int:
    from server import brain_service
    return int(brain_service._entity_id)


async def _open(pool, eid: int, kind: str, ref: str) -> bool:
    if not mm.valid_scope(kind, ref):
        return False
    try:
        async with pool.acquire() as conn:
            return await tp._still_open(conn, eid, kind, ref)
    except Exception as e:  # noqa: BLE001 — an unreadable thread is not a landing place
        log.info("connector: %s:%s not readable as open (%s)", kind, ref, e)
        return False


async def _title(pool, eid: int, kind: str, ref: str) -> str:
    from mastercode import circles_threads as ct
    if kind == "app":
        return ct.LEGACY_TITLE
    if kind == "thread":
        t = await pool.fetchval("SELECT title FROM circles_threads WHERE id = $1::uuid "
                                "AND entity_id = $2", ref, eid)
        return t or "Untitled thread"
    name = await pool.fetchval("SELECT name FROM projects WHERE id = $1 AND entity_id = $2",
                               int(ref), eid)
    return f"Project “{name}”" if name else f"Project {ref}"


async def _thread_url(kind: Optional[str], ref: Optional[str]) -> str:
    from server.connectors.reply_parts import _absolute, thread_link_path
    from server.reachout.service import resolve_host_origin
    if not kind:
        return ""
    return _absolute(thread_link_path(kind, ref), await resolve_host_origin(_eid()))


async def _thread_link(kind, ref, label=None) -> Optional[dict]:
    from server.connectors.reply_parts import THREAD_LINK_LABEL
    url = await _thread_url(kind, ref)
    return {"type": "link", "url": url, "label": label or THREAD_LINK_LABEL} if url else None


# ── reading a finished turn ──────────────────────────────────────────────────

def read_frames(frames: list[str]) -> tuple[str, list[dict], list[str]]:
    """(the reply text, the tool calls as live records, the error lines) of a
    turn, from the frames the turn registry buffered: exactly what the Host's
    own chat drew live. A call is {tool, args, result, result_status}, the
    shape reply_parts reads."""
    text: list[str] = []
    calls: dict[str, dict] = {}
    order: list[str] = []
    errors: list[str] = []
    for f in frames:
        if not isinstance(f, str) or len(f) < 2 or f[1] != ":":
            continue
        tag, raw = f[0], f[2:]
        try:
            val = json.loads(raw)
        except ValueError:
            continue
        if tag == "0" and isinstance(val, str):
            text.append(val)
        elif tag == "9" and isinstance(val, dict) and val.get("toolCallId"):
            cid = str(val["toolCallId"])
            if cid not in calls:
                order.append(cid)
            calls[cid] = {"id": cid, "tool": val.get("toolName"), "args": val.get("args") or {},
                          "result": None, "result_status": ""}
        elif tag == "a" and isinstance(val, dict) and val.get("toolCallId"):
            c = calls.get(str(val["toolCallId"]))
            if c is not None:
                res = val.get("result")
                c["result"] = res
                c["result_status"] = (str(res.get("status") or "unknown")
                                      if isinstance(res, dict) else "ok")
        elif tag == "3" and isinstance(val, str) and val.strip():
            errors.append(val.strip())
    return "".join(text), [calls[c] for c in order], errors


# The platform tools whose RESULT carries the card the platform filed
# (app-web-kit/ask.ts RESULT_ASK_TOOLS), named, never "any result with an ask".
_RESULT_ASK_TOOLS = frozenset({"pipeline", "mgmt_run", "context_ingest", "turn_type"})


def _rec(v: Any) -> Optional[dict]:
    return v if isinstance(v, dict) else None


def asks_of_turn(calls: list[dict]) -> list[dict]:
    """The cards a turn put on the person's screen: a port of
    app-web-kit/ask.ts askFromTool, so this names exactly the cards the
    Host's own chat draws. [{ticket_id, sitrep, options: [label…]}]."""
    out: list[dict] = []
    for c in calls:
        env = _rec(c.get("result"))
        if not env or env.get("status") != "ok":
            continue
        res = _rec(env.get("result"))
        if not res:
            continue
        name = c.get("tool")
        if name == "operate":
            name = str((_rec(c.get("args")) or {}).get("tool") or "").strip()
        if name == "escalate":
            if res.get("status") != "open" or res.get("auto_resolved") is True:
                continue
            src = _rec(c.get("args"))
            src = {**src, "ticket_id": res.get("ticket_id")} if src is not None else None
        elif name in _RESULT_ASK_TOOLS:
            src = _rec(res.get("ask"))
        else:
            continue
        if not src:
            continue
        try:
            tid = int(src.get("ticket_id"))
        except (TypeError, ValueError):
            continue
        labels = []
        for o in src.get("options") or []:
            o = _rec(o)
            if o and isinstance(o.get("id"), str) and o["id"]:
                labels.append(str(o.get("label") or o["id"]))
        if tid > 0 and labels:
            out.append({"ticket_id": tid, "sitrep": str(src.get("sitrep") or "").strip(),
                        "options": labels})
    return out


async def waiting_cards(pool, calls: list[dict]) -> list[dict]:
    """The turn's cards still open now, at its end: they wait on the person's
    own device (4.8)."""
    asks = asks_of_turn(calls)
    if not asks:
        return []
    ids = [a["ticket_id"] for a in asks]
    try:
        open_ids = {int(r["id"]) for r in await pool.fetch(
            "SELECT id FROM entity_tickets WHERE id = ANY($1::bigint[]) AND status = 'open'", ids)}
    except Exception as e:  # noqa: BLE001 — unreadable: say it may be waiting
        log.info("connector: card state unreadable (%s); naming every card", e)
        open_ids = set(ids)
    return [a for a in asks if a["ticket_id"] in open_ids]


def card_text(card: dict, platform: str) -> str:
    what = tp._clip(card["sitrep"].splitlines()[0] if card["sitrep"] else "", 300) or "a decision"
    return (f"A card is waiting for you in your Host: “{what}” "
            f"(choices: {' / '.join(card['options'][:6])}). Answer it there; it cannot be "
            f"answered from {tp.platform_name(platform)}.")


CARD_LINK_LABEL = "Answer it in your Host"
TRUNCATED_NOTE = ("Part of this reply was too long to keep for this chat; the whole reply is "
                  "in your Host.")


NOTHING_TO_SEND = "(the turn ended with nothing to send)"
# The card a holding turn shows (session.py _waiting_card): its tool name.
WAITING_TOOL = "waiting"
# How many turns one reply follows past its first (a nudge sent on as a turn
# of its own, whose nudges were sent on again, …): a bound on a chain that
# only a turn failing before its session at every hop could build.
FOLLOW_MAX = 4


def _is_hold(frame: Any) -> bool:
    """The frame a turn sends as it starts holding at its exit: the
    'waiting' card (session.py _hold_at_exit → _waiting_card)."""
    if not (isinstance(frame, str) and frame.startswith("9:") and WAITING_TOOL in frame):
        return False
    try:
        val = json.loads(frame[2:])
    except ValueError:
        return False
    return isinstance(val, dict) and val.get("toolName") == WAITING_TOOL


def _requeued_to(frames: list) -> Optional[str]:
    """The turn a nudge nobody could read went on as, from the frame the
    registry leaves on the turn that ended (turn_registry._requeue_undelivered:
    {"type": "nudge_requeued", "turn_id"})."""
    for f in reversed(frames):
        if not (isinstance(f, str) and f.startswith("2:") and "nudge_requeued" in f):
            continue
        try:
            items = json.loads(f[2:])
        except ValueError:
            continue
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("type") == "nudge_requeued" and it.get("turn_id"):
                return str(it["turn_id"])
    return None


async def _segment_parts(reply: Reply, frames: list, *, cards: list, noticed: set,
                         final: bool, truncated: bool = False) -> list[dict]:
    """The parts of one stretch of a turn, unnumbered: the reply's text and
    what it made in that stretch, in order; at the turn's end any error it
    ended on; then each open card not named yet (4.8) and a link to answer
    it in the Host."""
    from server.connectors.picture import desktop_tokens, draw_picture
    from server.connectors.reply_parts import build_reply_parts
    from server.reachout.service import resolve_host_origin

    text, calls, errors = read_frames(frames)
    try:
        tokens = await desktop_tokens()
    except Exception as e:  # noqa: BLE001 — a card with no token resolved can still draw
        log.info("connector: desktop tokens unavailable (%s)", e)
        tokens = {}

    async def draw(html: str):
        return await draw_picture(html, tokens=tokens)

    origin = await resolve_host_origin(_eid())
    parts = await build_reply_parts(text, calls, scope_kind=reply.kind, scope_ref=reply.ref,
                                    host_origin=origin, draw=draw)
    if final:
        for e in errors:
            parts.append({"type": "text", "text": e})
        if truncated:
            parts.append({"type": "text", "text": TRUNCATED_NOTE})
    fresh = [c for c in cards if c["ticket_id"] not in noticed]
    for card in fresh:
        noticed.add(card["ticket_id"])
        parts.append({"type": "text", "text": card_text(card, reply.platform)})
    if fresh or truncated:
        link = await _thread_link(reply.kind, reply.ref, CARD_LINK_LABEL if fresh else None)
        if link:
            parts.append(link)
    return parts


class _Out:
    """A reply's parts as they become ready: numbered on from the last one,
    the first carrying the line that names the thread, each batch published
    to the streams at once (Reply.early, Reply.more)."""

    def __init__(self, reply: Reply):
        self.reply = reply
        self.parts: list[dict] = []

    def publish(self, batch: list[dict]) -> None:
        if not batch:
            return
        batch = list(batch)
        if not self.parts and self.reply.lead:
            if batch[0].get("type") == "text":
                batch[0] = {**batch[0], "text": f"{self.reply.lead}\n\n{batch[0]['text']}"}
            else:
                batch.insert(0, {"type": "text", "text": self.reply.lead})
        for p in batch:
            q = {k: v for k, v in p.items() if k not in ("type", "seq")}
            self.parts.append({"type": p["type"], "seq": len(self.parts), **q})
        self.reply.early = list(self.parts)
        ev, self.reply.more = self.reply.more, asyncio.Event()
        ev.set()


async def _follow_holds(reply: Reply, rec, out: _Out, noticed: set) -> int:
    """Watch one turn until it ends. Each time it starts holding while a card
    of its own is open, send what it wrote since the last batch and the
    cards not named yet (4.8): the person learns of the card when the turn
    starts waiting on it, not up to HOLD_S later. Returns the index of the
    first frame not sent yet."""
    from server import brain_service

    pool = brain_service.get_pool()
    q: asyncio.Queue = asyncio.Queue()
    rec.subscribers.add(q)          # a wake-up per frame, and one at the end
    start = idx = 0
    try:
        while True:
            frames = rec.frames
            while idx < len(frames):
                idx += 1
                if not _is_hold(frames[idx - 1]) or pool is None:
                    continue
                before = list(frames[:idx - 1])
                cards = await waiting_cards(pool, read_frames(before)[1])
                if cards:
                    out.publish(await _segment_parts(reply, before[start:], cards=cards,
                                                     noticed=noticed, final=False))
                    start = idx
            if rec.done.is_set():
                return start
            await q.get()
            while not q.empty():
                q.get_nowait()
    finally:
        rec.subscribers.discard(q)


async def _followed_turn(reply: Reply, rec):
    """The turn `rec`'s unread nudges went on to, when they carry this
    connector's words (never a turn only another surface's words went
    to), else None: a turn started for them, or the thread's next turn that
    was already running and took them as a nudge.
    It answers for this reply from now on — unless a reply of this same chat
    already answers for that turn, which sends its reply here once."""
    from server.turn_registry import get_turn_registry

    tid = _requeued_to(list(rec.frames))
    if (not tid or tid in reply.turn_ids() or tid in reply.joined
            or len(reply.followed) + len(reply.joined) >= FOLLOW_MAX):
        return None
    reg = get_turn_registry()
    nxt = reg.get(tid)
    sent_on = list(getattr(rec, "requeued_texts", None) or [])
    if nxt is None or not any(t and any(t in x for x in sent_on) for t in reply.nudges):
        return None
    other = _REPLIES.get(tid)
    if other is not None and other is not reply and (
            other.app_id, other.platform, other.chat_id) == (reply.app_id, reply.platform,
                                                             reply.chat_id):
        return None
    # A turn started for the words has them as its message (the registry
    # joins them, _requeue_undelivered): it is this connector's turn. A turn
    # another surface started and that took them as a nudge is that
    # surface's: the reply follows it, and a revoke never stops it.
    if (nxt.user_message or "") == "\n\n".join(sent_on):
        reply.followed.append(tid)
    else:
        reply.joined.append(tid)
    _REPLIES.setdefault(tid, reply)
    if reply.revoked and tid in reply.followed:
        # Revoked while that turn was being sent on: it is this connector's
        # turn in flight, so it stops too.
        await reg.cancel(tid)
    return nxt


def moved_to(frames: list) -> Optional[dict]:
    """Where the turn's exchange was moved (the thread_moved frame, sent when
    the Host picked its thread in the turn: runtime/thread_choice), as
    {kind, ref, title}, or None."""
    for f in frames:
        if not (isinstance(f, str) and f.startswith("2:") and "thread_moved" in f):
            continue
        try:
            items = json.loads(f[2:])
        except ValueError:
            continue
        for it in items if isinstance(items, list) else []:
            to = it.get("to") if isinstance(it, dict) and it.get("type") == "thread_moved" else None
            if isinstance(to, dict) and to.get("kind") and to.get("ref"):
                return {"kind": str(to["kind"]), "ref": str(to["ref"]), "title": str(it.get("to_title") or "")}
    return None


async def _follow_move(pool, reply: Reply, frames: list) -> None:
    """The Host moved this reply's exchange to another thread in its turn:
    the reply says where it is now, and the map's rows for this turn follow
    it, so the chat's next message and any reply to these continue there."""
    to = moved_to(frames)
    if to is None or pool is None or (to["kind"], to["ref"]) == (reply.kind, reply.ref):
        return
    try:
        await pool.execute(
            "UPDATE connector_messages SET scope_kind = $3, scope_ref = $4"
            " WHERE entity_id = $1 AND turn_id = $2", _eid(), reply.reply_id, to["kind"], to["ref"])
    except Exception as e:  # noqa: BLE001 — the reply still goes out
        log.warning("connector reply %s: map not moved: %s", reply.reply_id[:8], e)
    reply.kind, reply.ref = to["kind"], to["ref"]
    reply.title = to["title"] or reply.title
    reply.changed = True
    reply.lead = f"Moved to: {reply.title}" if reply.title else reply.lead


async def _builder(reply: Reply, rec) -> None:
    from server import brain_service

    out = _Out(reply)
    try:
        noticed: set = set()
        cur = rec
        while cur is not None:
            start = await _follow_holds(reply, cur, out, noticed)
            pool = brain_service.get_pool()
            frames = list(cur.frames)
            await _follow_move(pool, reply, frames)
            cards = await waiting_cards(pool, read_frames(frames)[1]) if pool is not None else []
            out.publish(await _segment_parts(reply, frames[start:], cards=cards, noticed=noticed,
                                             final=True,
                                             truncated=bool(getattr(cur, "truncated", False))))
            cur = await _followed_turn(reply, cur)
        if not out.parts:
            out.publish([{"type": "text", "text": NOTHING_TO_SEND}])
        reply.parts.set_result(list(out.parts))
    except asyncio.CancelledError:
        if not reply.parts.done():
            reply.parts.cancel()
        raise
    except Exception as e:  # noqa: BLE001 — the person still hears something
        log.exception("connector reply %s: parts not built", reply.reply_id[:8])
        if not reply.parts.done():
            out.publish([{"type": "text", "text": f"The reply could not be prepared for this chat "
                                                  f"({e}); it is in your Host."}])
            reply.parts.set_result(list(out.parts))


# ── the turn ─────────────────────────────────────────────────────────────────

async def start_turn(pool, caller, msg: Incoming, *, kind: str, ref: str, title: str,
                     changed: bool, new: bool, choice: Optional[dict] = None):
    """Write the inbound row, then start the person's own chat turn in that
    conversation on the real send path. A Reply (a turn it started, or a
    refusal) or a Nudge (the conversation's turn was already running)."""
    from server import brain_service
    from server.turn_registry import get_turn_registry

    eid = _eid()
    row_id = await mm.write_inbound(pool, eid, app_id=caller.app_id, grant_id=caller.grant_id,
                                    platform=caller.platform,
                                    chat_id=msg.chat_id, message_id=msg.message_id,
                                    native_thread_id=msg.native_thread_id, kind=kind, ref=ref)
    if row_id is None:                     # the same message, delivered twice at once
        return await duplicate(pool, caller, msg)
    send: dict = {"app_id": "circles"}
    if kind == "thread":
        send["thread_id"] = ref
    elif kind == "project":
        send["project_id"] = int(ref)
    for_host = message_for_host(caller.platform, msg.text, msg.origin)
    if msg.origin is not None:
        # Kept on the person's row as metadata (session.py puts the frame's
        # connector_origin into the user row's tags); the header carries it
        # as words.
        send["frame"] = {"connector_origin": msg.origin.as_meta()}
    if choice:
        # The threads the Host may move this exchange to in its turn
        # (runtime/thread_choice); _builder follows a move.
        send["frame"] = {**(send.get("frame") or {}), "thread_choice": choice}
    # The connector keeps no copy of the words, and needs to say nothing about
    # it: words that land in a running turn are the server's to deliver unless
    # they came through a route whose client keeps a copy, which this is not.
    # If that turn ends without reading them, the registry sends them on as the
    # thread's next turn (session.end_turn_input), and this reply follows it
    # (_followed_turn).
    meta: Optional[dict] = None
    error: Optional[str] = None
    try:
        agen = brain_service.send_message(entity_id=eid, message=for_host, **send)
        try:
            async for frame in agen:
                if frame.startswith("3:"):
                    with contextlib.suppress(ValueError):
                        error = str(json.loads(frame[2:]))
                    break
                if frame.startswith("2:") and '"turn"' in frame:
                    with contextlib.suppress(ValueError):
                        items = json.loads(frame[2:])
                        if items and isinstance(items[0], dict) and items[0].get("type") == "turn":
                            meta = items[0]
                            break
        finally:
            # Only this tail ends; the turn runs on, detached (turn_registry).
            await agen.aclose()
    except Exception as e:  # noqa: BLE001 — answered as a refusal, never a stream with no end
        log.exception("connector message %s: the send path failed", msg.message_id)
        error = f"the turn could not start ({type(e).__name__})"
    lead = (f"New thread: {title}" if new else f"Back in: {title}" if changed else None)
    if meta is None:
        # No turn ran, so the row is taken back: the same message delivered
        # again is placed, not answered as a duplicate of nothing.
        with contextlib.suppress(Exception):
            await mm.forget_inbound(pool, row_id)
        return _immediate(caller, msg, "refused", [error or "the turn could not start"],
                          kind=kind, ref=ref, title=title, changed=changed)
    turn_id = str(meta.get("turn_id"))
    if meta.get("mode") == "started":
        await mm.set_turn(pool, row_id, turn_id)
        rec = get_turn_registry().get(turn_id)
        reply = Reply(reply_id=turn_id, app_id=caller.app_id, grant_id=caller.grant_id,
                      platform=caller.platform, chat_id=msg.chat_id, state="started", kind=kind,
                      ref=ref, title=title, changed=changed, lead=lead, rec=rec)
        _keep(reply)
        reply.builder = asyncio.get_running_loop().create_task(
            _builder(reply, rec), name=f"connector-parts-{turn_id[:8]}")
        return reply
    # nudged, or the same text again while it runs: one turn, one reply
    held = reply_for(caller, turn_id)
    if held is not None:
        # Its words, so the reply follows them if that turn ends before it
        # can read them (_followed_turn). The map keeps the reply's own id,
        # the one /sent and a reattach name.
        held.nudges.append(for_host.strip())
        await mm.set_turn(pool, row_id, held.reply_id)
        return Nudge(kind, ref, title, changed, held.reply_id, True)
    url = await _thread_url(kind, ref)
    note = (f"Added to the turn already running in “{title}”. Its reply is in your Host"
            + (f": {url}" if url else "."))
    return Nudge(kind, ref, title, changed, turn_id, False, note)


async def duplicate(pool, caller, msg: Incoming):
    """A message the route has already taken (the platform delivered it
    again, or the connector posted it twice): never a second turn. Its own
    reply when that is still held, else what is known of it."""
    row = await mm.inbound(pool, _eid(), caller.app_id, caller.platform, msg.chat_id,
                           msg.message_id)
    if row is not None and row["turn_id"]:
        held = reply_for(caller, row["turn_id"])
        if held is not None:
            return Again(held)
    kind, ref = (row["scope_kind"], row["scope_ref"]) if row is not None else (None, None)
    title = await _title(pool, _eid(), kind, ref) if kind else ""
    return _immediate(caller, msg, "duplicate", [], kind=kind, ref=ref, title=title)


# ── routing (4.2) ────────────────────────────────────────────────────────────

def _chat_key(caller, msg) -> tuple:
    return (caller.app_id, caller.platform, msg.chat_id)


async def _ask(caller, msg) -> Reply:
    return _immediate(caller, msg, "ask", [tp.ASK_WHICH_THREAD])


async def _list_threads(pool, caller, msg, current) -> Reply:
    cands = (await tp.list_candidates(pool, _eid(), budget_tokens=10 ** 9,
                                      current=current))[:LIST_MAX]
    _LISTINGS[_chat_key(caller, msg)] = [(c.kind, c.ref, c.title) for c in cands]
    cur = f"{current[0]}:{current[1]}" if current else None
    lines = ["Your open threads, newest first:"]
    for i, c in enumerate(cands, 1):
        entry = f"{i}. {c.title}"
        if c.type_name:
            entry += f" ({c.type_name})"
        entry += f", {tp._when(c.last_activity)}"
        if c.key == cur:
            entry += " (you are here)"
        lines.append(entry)
    lines.append("Send /thread <number> to switch, or /new to start a new thread.")
    return _immediate(caller, msg, "command", ["\n".join(lines)])


async def _land(pool, caller, pick) -> Optional[tp.Landing]:
    try:
        return await tp.land_pick(pool, _eid(), pick, platform=caller.platform)
    except tp.PickGone:
        return None


def _new_pick(text: str) -> tp.Pick:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return tp.Pick(ok=True, kind="thread", new=True, title=tp._clip(first, 60) or "New thread",
                   reason="the person started a new thread with /new")


async def route(pool, caller, msg: Incoming):
    """Decide where the message goes and start its turn. Returns a Reply or
    a Nudge; a message it cannot place gets the question, never a guess."""
    eid = _eid()
    key = _chat_key(caller, msg)
    if await mm.inbound(pool, eid, caller.app_id, caller.platform, msg.chat_id,
                        msg.message_id) is not None:
        return await duplicate(pool, caller, msg)
    current = await mm.current_scope(pool, eid, caller.app_id, caller.platform, msg.chat_id)
    text = msg.text
    target: Optional[tuple[str, str]] = None
    new = False
    # Someone else's words are never the person's command.
    cmd = parse_command(text) if msg.origin is None else None

    # 1. commands: the route's own, never sent to the Host
    if cmd is not None:
        name, rest = cmd
        if name == "/threads":
            return await _list_threads(pool, caller, msg, current)
        if name == "/new":
            if not rest:
                _PENDING_NEW.add(key)
                return _immediate(caller, msg, "command",
                                  ["Your next message starts a new thread."])
            landing = await _land(pool, caller, _new_pick(rest))
            if landing is None:
                return await _ask(caller, msg)
            target, new, text = (landing.kind, landing.ref), True, rest
        else:   # /thread <n> [message]
            m = re.match(r"^(\d{1,3})(?:\s+(.*))?$", rest, re.S)
            if not m:
                return _immediate(caller, msg, "command", [
                    "Send /threads to see your threads, then /thread <number>."])
            listing = _LISTINGS.get(key)
            if listing is None:
                cands = await tp.list_candidates(pool, eid, budget_tokens=10 ** 9, current=current)
                listing = [(c.kind, c.ref, c.title) for c in cands[:LIST_MAX]]
            n = int(m.group(1))
            if not 1 <= n <= len(listing) or not await _open(pool, eid, *listing[n - 1][:2]):
                return _immediate(caller, msg, "command", [
                    f"There is no open thread {n}. Send /threads to see them."])
            target = (listing[n - 1][0], listing[n - 1][1])
            text = (m.group(2) or "").strip()
            if not text:
                kind, ref = target
                title = await _title(pool, eid, kind, ref)
                # The switch is a message in that thread: a reply to it, or to
                # this answer, lands there, and it is now the chat's thread.
                row_id = await mm.write_inbound(
                    pool, eid, app_id=caller.app_id, grant_id=caller.grant_id,
                    platform=caller.platform,
                    chat_id=msg.chat_id, message_id=msg.message_id,
                    native_thread_id=msg.native_thread_id, kind=kind, ref=ref)
                r = _immediate(caller, msg, "command", [f"Back in: {title}"], kind=kind, ref=ref,
                               title=title, changed=(kind, ref) != current)
                if row_id is not None:
                    await mm.set_turn(pool, row_id, r.reply_id)
                return r
    elif key in _PENDING_NEW:
        _PENDING_NEW.discard(key)
        landing = await _land(pool, caller, _new_pick(text))
        if landing is None:
            return await _ask(caller, msg)
        target, new = (landing.kind, landing.ref), True

    # 2. a reply to a message the map knows; 3. a platform thread it knows
    if target is None and msg.reply_to:
        s = await mm.scope_of_message(pool, eid, caller.app_id, caller.platform, msg.chat_id,
                                      msg.reply_to)
        if s and await _open(pool, eid, *s):
            target = s
    if target is None and msg.native_thread_id:
        s = await mm.scope_of_native_thread(pool, eid, caller.app_id, caller.platform,
                                            msg.chat_id, msg.native_thread_id)
        if s and await _open(pool, eid, *s):
            target = s

    # 4. placed in software (thread_pick.place_connector): a clear match
    # joins its thread; otherwise the message lands in the chat's last
    # thread (or a new one) and the Host's own turn may move it to one of
    # the open threads it is shown (runtime/thread_choice). No model call.
    choice = None
    if target is None:
        placed = await tp.place_connector(
            pool, eid, message=(f"[{origin_label(msg.origin)}]\n{quoted(text)}" if msg.origin else text),
            platform=caller.platform, current=current)
        if placed is None:
            return await _ask(caller, msg)
        target, new = (placed.kind, placed.ref), placed.new
        if placed.choice:
            from mastercode.runtime.thread_choice import frame_value
            choice = frame_value(surface="connector", platform=caller.platform, candidates=list(placed.choice),
                                 landed={"kind": placed.kind, "ref": placed.ref,
                                         "title": await _title(pool, eid, placed.kind, placed.ref),
                                         "provisional": placed.provisional})

    kind, ref = target
    title = await _title(pool, eid, kind, ref)
    return await start_turn(pool, caller, dataclasses.replace(msg, text=text), kind=kind,
                            ref=ref, title=title, changed=new or (kind, ref) != current, new=new,
                            choice=choice)


# One chat's messages are placed one at a time, in the order they came: a
# person sends a thought as three quick messages, and each pick must see where
# the one before it landed (a new thread it opened, or its turn running there,
# which the next one then joins as a nudge). Placed concurrently, each pick
# ran blind to the others and each could open a thread of its own; a /thread
# or /new could also be overtaken by the message it was meant to steer.
# Held only while a message is placed, never while its turn runs.
_CHAT_ORDER: dict[tuple, list] = {}       # chat key -> [lock, users]


async def _route_in_order(pool, caller, msg: Incoming):
    key = _chat_key(caller, msg)
    entry = _CHAT_ORDER.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            return await route(pool, caller, msg)
    finally:
        entry[1] -= 1
        if entry[1] == 0 and _CHAT_ORDER.get(key) is entry:
            del _CHAT_ORDER[key]


def take(pool, caller, msg: Incoming) -> asyncio.Task:
    """route(), detached from the HTTP request that carried the message: a
    connector that drops the request mid-pick does not lose the message, and
    the same message posted again while it is being placed waits for the same
    answer instead of placing it twice. A chat's messages are placed in the
    order they came (_route_in_order)."""
    key = (caller.app_id, caller.platform, msg.chat_id, msg.message_id)
    task = _INFLIGHT.get(key)
    if task is None:
        task = asyncio.get_running_loop().create_task(_route_in_order(pool, caller, msg),
                                                      name=f"connector-route-{msg.message_id}")
        _INFLIGHT[key] = task
        task.add_done_callback(lambda _t: _INFLIGHT.pop(key, None))
    return task


# ── the stream (4.5) ─────────────────────────────────────────────────────────

def line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


async def _wait(fut: "asyncio.Future", ended: Optional[asyncio.Event] = None,
                more: Optional[asyncio.Event] = None) -> AsyncIterator[str]:
    """Wait for `fut`, yielding an alive line every HEARTBEAT_S. Stops early
    when `ended` is set (the grant was revoked) or `more` is (the reply has
    parts to send before its end). Never cancels `fut`."""
    stops = [asyncio.ensure_future(e.wait()) for e in (ended, more) if e is not None]
    try:
        while not fut.done():
            done, _ = await asyncio.wait({fut, *stops}, timeout=HEARTBEAT_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            if any(s in done for s in stops):
                return
            if not done:
                yield line({"type": "alive"})
    finally:
        for s in stops:
            s.cancel()


async def stream_reply(pool, reply: Reply, *, attached: bool) -> AsyncIterator[str]:
    """The thread and the turn at once; the parts as they are ready (a batch
    when the turn starts holding on a card, the rest at its end), without the
    ones the connector already reported as sent; then done. Ends with
    nothing more once the grant is revoked."""
    head = reply.thread_line()
    if head:
        yield line(head)
    state = reply.state
    if reply.state == "started" and attached:
        state = "done" if reply.parts.done() else "running"
    yield line({"type": "turn", "turn_id": reply.reply_id, "state": state})
    out: set[int] = set()            # the seqs this stream has passed on
    while True:
        more = reply.more            # taken BEFORE reading, so no batch is missed
        complete = reply.parts.done() and not reply.parts.cancelled()
        ready = reply.parts.result() if complete else list(reply.early)
        new = [p for p in ready if p["seq"] not in out]
        if new and not reply.revoked:
            sent = await mm.sent_seqs(pool, _eid(), reply.app_id, reply.reply_id)
            for p in new:
                if reply.revoked:
                    return
                out.add(p["seq"])
                if p["seq"] not in sent:
                    yield line(p)
        if reply.revoked or reply.ended.is_set() or reply.parts.cancelled():
            return
        if complete:
            break
        async for beat in _wait(reply.parts, reply.ended, more):
            yield beat
    yield line({"type": "done"})


def nudge_lines(n: Nudge) -> list[str]:
    t = {"type": "turn", "turn_id": n.turn_id, "state": "nudged", "mine": n.mine}
    if n.note:
        t["note"] = n.note
    return [line({"type": "thread", "kind": n.kind, "ref": n.ref, "title": n.title,
                  "changed": n.changed}), line(t), line({"type": "done"})]


async def stream_message(pool, caller, msg: Incoming) -> AsyncIterator[str]:
    task = take(pool, caller, msg)
    async for beat in _wait(task):
        yield beat
    answer = task.result()
    if isinstance(answer, Nudge):
        for s in nudge_lines(answer):
            yield s
        return
    again = isinstance(answer, Again)
    async for s in stream_reply(pool, answer.reply if again else answer, attached=again):
        yield s


async def gone_lines(pool, caller, turn_id: str) -> Optional[list[str]]:
    """Past the registry's window: the reply is in the thread, and the
    connector sends the person a link to it. None when this grant never had
    a reply by that id."""
    home = await mm.turn_home(pool, _eid(), caller.app_id, caller.grant_id, turn_id)
    if home is None:
        return None
    link = await _thread_link(home[0], home[1])
    part = _numbered([link or {"type": "text", "text": "The reply is in your Host."}])[0]
    return [line({"type": "turn", "turn_id": turn_id, "state": "gone"}), line(part),
            line({"type": "done"})]


# ── revoke (revoking cancels the turn in flight) ─────────────────────────────

async def cancel_for_revoke(*, app_id: str, grant_id: Optional[int], reason: str) -> int:
    """The canceller nac_connector.revoke_connector runs after its commit:
    every reply of that grant ends its stream with nothing more, and each
    turn it started that is still running is stopped. The thread keeps what
    was done (the registry's stopped-turn row)."""
    from server.turn_registry import get_turn_registry
    reg = get_turn_registry()
    n = 0
    seen: set[int] = set()
    for r in list(_REPLIES.values()):       # a followed turn's id is the same reply
        if id(r) in seen or r.app_id != app_id or (grant_id is not None
                                                   and r.grant_id != grant_id):
            continue
        seen.add(id(r))
        r.revoked = True
        r.ended.set()
        if r.state != "started":
            continue
        for tid in r.turn_ids():
            rec = reg.get(tid)
            if rec is not None and not rec.done.is_set():
                await reg.cancel(tid)
                n += 1
    if n:
        log.info("connector %s: revoke (%s) stopped %d running turn(s)", app_id, reason, n)
    return n
