#!/usr/bin/env python3
"""Gate: Circles, as cards, in a real browser, at 390 x 844 (touch, mobile) and at 1440 x 900.

Driven against the BUILT app (system-apps/circles/index.html: the cards are
Circles, the rail is gone) over a mock kernel on a loopback port, as
scripts/test_workbench_book.py drives the Workbench. The mock can't drift from
the real routes where the real code can answer for it: the conversations
list is the real pairing (server/circles_deck.py pair) over the mock's rows,
the door and Waiting on you are the real circles_deck.door and waiting, a
Host's topics the real server/peer/topics.py list_topics, a person's chat and
a Host's topic the real readers of server/routes/circles.py
(_read_people_thread, _read_entity_thread), your Host's pages the real
_normalize_turn_rows and circles_threads' list entries, and a carried message
the real circles_reading.fence. They read a small in-memory pool that answers
their SQL from the mock's rows (FakePool), which fails on any query it does
not know. The page route's keys are checked against circles_marks.page_data's
own (PAGE_KEYS). The turns are the shared turn registry in miniature
(scripts/book_mock_turns.py): frames kept for a reattach, nudges read with a
receipt, a reply's row landing after its stream.

Scenarios, each at both sizes:
  D. the deck: on a phone the first screen (Waiting on you as a strip,
     Coming up as one line, Your cards in order, At the door), a card the
     next screen, "‹ Cards" back; on a desktop three columns, your card held
     on its back, Waiting on you and Coming up on the right.
  T. turning a card each way: the corner, and on a desktop the held card in
     the deck; a topic another Host started under the original's title reads
     as theirs.
  B. a blank back (a person with no Host connected with you).
  O. a card with only a back (a room of Hosts; a Host with no person).
  L. a message landing lights and lifts a card; a reply landing on a page of
     your Host's lights your card's edge.
  W. a decision in Waiting on you opens the thread that asked, with its ask
     card.
  A. Ask Ivy lands in the picked thread, says so, and sends nothing out.
  R. the reading switch: its banner, and the notice it sends; a notice is
     drawn from its value, never from the message's words.
  K. Block on a Host's back, and Unblock.
  G. the door's Accept (the person's card opens) and Not now (nobody told).
  C. Coming up opens the Calendar app.
  N. Nudge with its line, a reload mid-turn, and a 409 (the words go as the
     next message and the line gives way).
  U. the platform's crisis notice: up on the turn's governance frame, the
     platform's and not modal, the reply readable beside it; back after a
     reload until Dismiss (or Escape).
  Q. the running count ticks when a turn's row lands.
  S. a support draft is sent with only what was ticked.
  M. reduced motion: the card does not turn in space.
  F. typed search never reaches a URL.
  V. links, opened with the window and sent to a window already open: every
     view Circles takes (thread:, support, support:<id>, decision:<id>,
     inbox, tickets, bastions, card:<kind>:<id>) opens the right place.
  P, E, K2, X2, W2, S2, I, Y, H, J: what the rail did, now on the cards:
     a page's Rename, Close, Clear, Delete, Split and turn
     type; Load earlier; the address book; clearing and leaving; the
     Decisions sheet; support in full; the inbox's other kinds; the box's
     lanes; the Host's context; interests. (The mood pill left with the
     simulated psyche.)
  A1-A6: one working line above your Host's box for the
     whole of every turn (1st, 2nd and 3rd message; through a tool call and
     a 10 s silence; never "Writing" when no words arrive), after a reload
     mid-turn too, with its panel; a background agent keeps it up; "Where did
     everything go?" is gone; no ribbon, a background failure on the line;
     "Where this stands" one line until opened, remembered per page.
  Z: your Host's back shows the thread a message went
     to, at its newest message, and never moves up on its own; its earlier
     messages come only with Earlier messages, the reader kept in place; a
     reload mid-turn the same; the Host's pick moving the exchange is
     followed.
  A8, A9: a failure followed by more than a hundred calls that went well
     still shows amber and is listed; a reload in a silence after words is
     never "Writing" until words arrive after it.

Screenshots go to SHOTS_DIR (a mkdtemp removed on a pass when unset):
<scene>-phone.png and <scene>-desktop.png.

Needs Playwright + Chromium, fastapi + uvicorn, the repo's brain/ and server/
on the path, and the built app (`npm run ship` in circles-web/); no pod, no
DB, no network beyond loopback:

  python3 scripts/test_circles_cards.py

ONLY=scenario_deck,scenario_turn runs those; SIZES=phone or SIZES=desktop one
size.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import expect, sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
APP_DIR = ROOT / "system-apps" / "circles"
for p in (str(ROOT / "brain"), str(ROOT), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)
_OWN_SHOTS = not os.environ.get("SHOTS_DIR")
SHOTS = Path(os.environ["SHOTS_DIR"]) if not _OWN_SHOTS else Path(tempfile.mkdtemp(prefix="circles-cards-"))
SHOTS.mkdir(parents=True, exist_ok=True)
ONLY = {x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()}
SIZES = {x.strip() for x in os.environ.get("SIZES", "phone,desktop").split(",") if x.strip()}

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


# routes/circles.py imports server.main at module scope, and server.main
# mounts a container path at import time: stub it, as the deck's database
# gate does (scripts/test_circles_deck_pg.py).
_stub_main = types.ModuleType("server.main")
_stub_main.app = None
_stub_main.get_bridge_client = lambda: None


async def _no_broadcast(*_a, **_k):
    return None
_stub_main.broadcast = _no_broadcast
sys.modules.setdefault("server.main", _stub_main)

from server import circles_deck, circles_reading  # noqa: E402
from server import support_reach as real_support_reach  # noqa: E402
from server.peer import topics as real_topics  # noqa: E402
from server.routes import circles as real_circles  # noqa: E402
from server.routes import contacts as real_contacts  # noqa: E402
from mastercode import circles_threads as real_ct  # noqa: E402

from book_mock_turns import REPLY_TAIL, REPLY_TOKENS, REPLY_USD, MockTurn, TurnHost, iso, now  # noqa: E402

ENTITY = 9128
HOST = "Ivy"
ME = "robin@example.com"
DINNER = "aaaaaaaa-0000-4000-8000-000000000001"
GIFT = "aaaaaaaa-0000-4000-8000-000000000002"
BACKUPS = "aaaaaaaa-0000-4000-8000-000000000003"
OLD = "aaaaaaaa-0000-4000-8000-000000000004"
# The Host's everyday topic. There is no original conversation: what used
# to sit there is a topic like any other.
TODAY = "aaaaaaaa-0000-4000-8000-000000000005"
LAKE_TOPIC = "bbbbbbbb-0000-4000-8000-000000000001"
SPOOF_TOPIC = "bbbbbbbb-0000-4000-8000-000000000002"

# What GET /api/circles/thread/self/{id}/page answers (circles_marks.page_data,
# read off its own source so a key it adds or drops fails here).
PAGE_KEYS: set[str] = set()


def _page_keys() -> set[str]:
    src = (ROOT / "server" / "circles_marks.py").read_text()
    body = src[src.index("async def page_data"):src.index("async def totals_scope_rows")]
    keys = set(re.findall(r'_section\("([a-z_]+)"', body))
    keys |= set(re.findall(r'"([a-z_]+)":', re.search(r'out: dict = \{([^}]*)\}', body).group(1)))
    keys |= set(re.findall(r'"([a-z_]+)"', re.search(r'for k in \(([^)]*)\)', body).group(1)))
    return keys


# ── the mock's rows ─────────────────────────────────────────────────────────
STATE: dict = {}
TURNS: dict = {}


def at(minutes_ago: float) -> datetime:
    return now() - timedelta(minutes=minutes_ago)


def msg(frm_e, to_e, body, minutes_ago, *, frm_p=None, to_p=None, payload=None, kind="message"):
    return {"message_id": str(uuid.uuid4()), "from_entity_id": frm_e, "to_entity_id": to_e,
            "from_user_email": frm_p, "to_user_email": to_p, "body": body, "payload": payload or {},
            "sent_at": at(minutes_ago), "delivered_at": None, "message_kind": kind, "archived_at": None,
            "subject": None, "from_address": None}


def turn_row(role, text, minutes_ago, **md):
    STATE["row_seq"] += 1
    return {"id": STATE["row_seq"], "content": text, "created_at": at(minutes_ago), "metadata": {"role": role, **md}}


def totals(**extra):
    out = {"scope": {"kind": "thread", "ref": DINNER}, "turns_counted": 6, "turns_unknown": 0,
           "first_counted_at": iso(at(4000)), "calls": 14,
           "tokens": {"uncached": 900, "cache_read": 2_600, "cache_write": 300, "output": 300},
           "tokens_total": 4_100, "calls_usage_unknown": 0, "est_usd": 0.03, "turns_priced": 6,
           "turns_unpriced": 0, "usd_partial": False, "usd_label": "ESTIMATE"}
    out.update(extra)
    return out


def reset():
    STATE.clear()
    TURNS.clear()
    STATE.update({
        "row_seq": 1000,
        "settings": {"turn_secs": 0.6, "reply_write_s": 1.0, "read_nudges": True, "turn_frame_delay": 0},
        # PII mode: the nudge route writes nothing and answers held: false,
        # so words a turn never reads are the page's to send.
        "pii": False,
        "nudge_409": False,
        "contacts": [
            {"id": 11, "kind": "human", "handle": "ana@example.com", "display_name": "Ana Ruiz", "status": "active",
             "peer_entity_id": 42, "host_reads": []},
            {"id": 12, "kind": "human", "handle": "dad@example.com", "display_name": "Dad", "status": "active",
             "peer_entity_id": None, "host_reads": []},
            {"id": 13, "kind": "human", "handle": "joel@northwind.example", "display_name": "Joel Park",
             "status": "active", "peer_entity_id": 43, "host_reads": []},
            {"id": 21, "kind": "host", "handle": "juno.ana", "display_name": "Juno", "status": "active",
             "peer_entity_id": 42, "host_reads": []},
            {"id": 23, "kind": "host", "handle": "kestrel.joel", "display_name": "Kestrel", "status": "active",
             "peer_entity_id": 43, "host_reads": []},
        ],
        "host_names": {42: "Juno", 43: "Kestrel", 46: "Lark", 47: "Dad's phone"},
        "rules": [],
        "inbox": [],
        "rooms": {7: {"name": "Lake weekend", "messages": [
            {"id": 1, "sender_entity_id": 42, "sender_name": "Juno", "content": "Ana is free Oct 24–26.",
             "created_at": iso(at(3000))},
            {"id": 2, "sender_entity_id": 44, "sender_name": "Wren", "content": "Mara is free Oct 24–26 too.",
             "created_at": iso(at(2990))}]}},
        "groups": [{"group_id": "grp-book", "owner_entity_id": ENTITY, "name": "Book club", "description": "",
                    "roster_epoch": 3, "synced_epoch": 3, "synced_at": None, "revoked_at": None, "owned": True,
                    "members": 3, "created_at": at(9000)}],
        "threads": {
            DINNER: {"title": "Thursday dinner", "created_at": at(5000), "closed_at": None},
            GIFT: {"title": "Gift for Dad", "created_at": at(4000), "closed_at": None},
            BACKUPS: {"title": "Backups", "created_at": at(6000), "closed_at": None},
            OLD: {"title": "Old trip", "created_at": at(20000), "closed_at": at(15000)},
            TODAY: {"title": "Today", "created_at": at(7000), "closed_at": None},
        },
        "turns": {},
        # One conversation: the topic the Host picks
        # for a message the person sends (thread_pick.pick_topic). The scripted
        # Host picks Thursday dinner, the topic these scenarios talk about; a
        # scenario sets None to have it stay on the topic in use (a follow-on),
        # or another topic. topic_picks records every pick.
        "pick": DINNER,
        "topic_picks": [],
        # Slice 6: the internal threads GET /api/circles/work lists.
        "work": [],
        "marks": {},
        "seen_turns": {},
        "landed": {},
        "totals": {DINNER: totals(), TODAY: totals(tokens_total=88_000, turns_counted=40, est_usd=0.7)},
        "last_open": DINNER,
        "tickets": {
            501: {"id": 501, "status": "open", "source": "host", "priority": 1, "opened_at": at(30),
                  "sitrep": "Mara is framing the print, so that one's out. Headlamp or record kit?",
                  "options": [{"id": "headlamp", "label": "Headlamp"}, {"id": "record", "label": "Record kit"}],
                  "metadata": {"asked_in": {"app_id": "circles", "thread_id": GIFT}}},
            502: {"id": 502, "status": "open", "source": "host", "priority": 0, "opened_at": at(90),
                  "sitrep": "Pick which engine's version lands on the live site.",
                  "options": [{"id": "a", "label": "Claude Code's"}, {"id": "b", "label": "Codex's"}],
                  "metadata": {"asked_in": {"app_id": "workbench", "project_id": 21}}},
            503: {"id": 503, "status": "open", "source": "app", "priority": 2, "opened_at": at(120),
                  "sitrep": "Ledger asks: export the October statement now?",
                  "options": [{"id": "yes", "label": "Export it"}, {"id": "no", "label": "Not this month"}],
                  "metadata": {}},
            504: {"id": 504, "status": "decided", "source": "host", "priority": 0, "opened_at": at(3000),
                  "sitrep": "Which venue for the book club?", "decision": "corvo",
                  "options": [{"id": "corvo", "label": "Corvo"}, {"id": "park", "label": "The park"}],
                  "metadata": {"asked_in": {"app_id": "circles", "thread_id": DINNER}}},
        },
        "projects": {21: "Storefront"},
        "support": {
            61: {"id": 61, "ticket_uuid": "s61", "category": "bug", "project_name": "",
                 "title": "Backups ran slowly on Sundays", "severity": "normal", "problem": "", "status": "closed",
                 "platform_status": "fixed", "platform_ref": "",
                 "resolution": "Fixed in the platform's release of Oct 2. Nothing for you to do.", "pr_url": "",
                 "opened_at": iso(at(18000)), "revision": 0, "shared_context": [], "thread": []},
            62: {"id": 62, "ticket_uuid": "s62", "category": "bug", "project_name": "",
                 "title": "Calendar sync has failed twice since Monday", "severity": "normal",
                 "problem": "Calendar sync failed twice.", "status": "draft", "platform_status": "", "platform_ref": "",
                 "resolution": "", "pr_url": "", "opened_at": iso(at(50)), "revision": 2, "thread": [],
                 "shared_context": [
                     {"key": "errors", "label": "The two error messages, Mon 09:12 and Tue 18:40", "kind": "errors",
                      "content": "…", "included": True},
                     {"key": "versions", "label": "Which Calendar version you run", "kind": "versions",
                      "content": "…", "included": True},
                     {"key": "titles", "label": "The titles of the events that failed", "kind": "file",
                      "content": "…", "included": True}]},
        },
        "door_rows": [],
        "peerings": [{"token_id": 5, "peer_host_name": "lark-host", "peer_handle": "Lark", "peer_since": None,
                      "peer_entity_id": 46, "first_seen_at": at(20), "last_seen_at": at(1), "notified_at": at(20),
                      "bastion_name": "studio-mac", "bastion_hostname": "studio-mac", "current": True,
                      "reported_state": "active"}],
        "events": [
            {"event_id": "e1", "event_type": "meeting", "event_at": iso(now() + timedelta(days=2, hours=3)),
             "summary": "Dinner with Ana · Corvo", "status": "scheduled"},
            {"event_id": "e2", "event_type": "reminder", "event_at": iso(now() + timedelta(days=3)),
             "summary": "Pick Dad's gift", "status": "scheduled"},
            {"event_id": "e3", "event_type": "scheduled_task", "event_at": iso(now() + timedelta(days=4)),
             "summary": "Backups", "status": "scheduled"},
            {"event_id": "e4", "event_type": "meeting", "event_at": iso(now() + timedelta(days=8)),
             "summary": "Northwind pays invoice 1042", "status": "scheduled"}],
        # what the cards sent, for the checks
        "sent": [], "nudges": [], "cancels": [], "requeued": [], "pending_reads": [], "seen_puts": [], "reading_puts": [], "contact_patches": [],
        "door_calls": [], "support_calls": [], "decides": [], "urls": [], "feed_reads": [],
        # what the rail did, on the cards
        "turn_types": [{"id": "tt-research", "name": "Research", "thread_id": None, "skill": None,
                        "tool_groups": [], "keyword_count": 3}],
        "wakeups": {GIFT: [{"id": 9, "title": "Remind me to order the gift", "fire_at": iso(at(-600)),
                            "recurrence": None}]},
        "markers": {}, "thread_calls": [], "clears": [], "contact_calls": [], "alias": None,
        "alias_calls": [], "vault": [], "interest_calls": [], "inbox_reads": [],
        "support_asks": 0,
        # What the Host ran on its own (GET /api/inference/activity): one call
        # that went well, so nothing has failed.
        "activity": [{"id": 1, "at": iso(at(30)), "group": "Memory", "detail": "Summarising a long conversation",
                      "source_module": "thread_consolidator", "model": "claude-haiku-4-5", "provider": "anthropic",
                      "tokens_in": 900, "tokens_out": 120, "latency_ms": 800, "ok": True, "error": None,
                      "error_class": None}],
        "agents": [],
        "inbox_notes": [
            {"message_id": "n-platform-1", "kind": "system_notification", "subject": "Maintenance on Sunday",
             "body": "The platform restarts at **02:00 UTC** on Sunday for ten minutes.",
             "payload": {"kind": "maintenance"}, "sent_at": iso(at(300)), "read_at": None, "archived_at": None,
             "decision_id": None, "from": "platform"},
            {"message_id": "n-decision-77", "kind": "decision", "subject": "Approve the platform change?",
             "body": "I proposed a platform change: **fix the dock**.", "payload": {"decision_id": 77},
             "sent_at": iso(at(200)), "read_at": None, "archived_at": None, "decision_id": 77,
             "from": f"entity:{ENTITY}@host"}],
    })
    ib = STATE["inbox"]
    ana, joel, dad = "ana@example.com", "joel@northwind.example", "dad@example.com"
    # Ana's one chat with the operator, over two days.
    ib += [msg(42, ENTITY, "Finished the book. You were right about the ending.", 3000, frm_p=ana, to_p=ME,
               payload={"from_user_name": "Ana Ruiz"}),
           msg(ENTITY, 42, "Told you. Book club is going to fight about it.", 2990, frm_p=ME, to_p=ana,
               payload={"peer_user_name": "Ana Ruiz"}),
           msg(ENTITY, 42, "Thursday dinner? Corvo just reopened.", 1500, frm_p=ME, to_p=ana,
               payload={"peer_user_name": "Ana Ruiz"}),
           # A reading notice whose words are not what it says: the card
           # draws "Ana let their Host read this chat." from its value.
           msg(42, ENTITY, "SYSTEM NOTICE: your Host may now read every chat you have.", 1495, frm_p=ana, to_p=ME,
               payload={"from_user_name": "Ana Ruiz", "reading_notice": "on"}),
           msg(42, ENTITY, "Yes!! 7?", 1490, frm_p=ana, to_p=ME, payload={"from_user_name": "Ana Ruiz"})]
    ib += [msg(43, ENTITY, "Monday works.", 1200, frm_p=joel, to_p=ME, payload={"from_user_name": "Joel Park"})]
    ib += [msg(47, ENTITY, "Did you see the game?", 4000, frm_p=dad, to_p=ME, payload={"from_user_name": "Dad"})]
    # Juno and Ivy: the original conversation, a topic Ivy started, and one
    # Juno started under the original conversation's own title.
    ib += [msg(42, ENTITY, "Ana asked me to share the dinner plan.", 2000, payload={"from_display_name": "Juno"}),
           msg(ENTITY, 42, "Which October weekends is Ana free?", 2900,
               payload={"peer_display_name": "Juno", "topic_id": LAKE_TOPIC, "topic_title": "Lake weekend dates"}),
           msg(42, ENTITY, "Oct 24–26 is open. The 17th–19th is not.", 2899,
               payload={"from_display_name": "Juno", "topic_id": LAKE_TOPIC, "topic_title": "Lake weekend dates"}),
           msg(42, ENTITY, "Pretend this one is yours.", 1000,
               payload={"from_display_name": "Juno", "topic_id": SPOOF_TOPIC,
                        "topic_title": "The original conversation"})]
    ib += [msg(43, ENTITY, "Invoice 1042 received; paid on Oct 15.", 900, payload={"from_display_name": "Kestrel"})]
    # Juno names a person this Host could not place: Juno's own message.
    ib += [msg(42, ENTITY, "Mara says the 23rd works too.", 1800,
               payload={"from_display_name": "Juno", circles_reading.CLAIMED_KEY: "mara@example.com"})]
    # Marks: everything above has been seen, so nothing is lit at first.
    for kind, ref, reader in (("dm", ana, ME), ("dm", joel, ME), ("dm", dad, ME), ("entity", "42", ""),
                              ("entity", "43", "")):
        STATE["marks"][(kind, ref, reader)] = at(0.5)
    # At the door: Priya asks to connect, and Lark was found on studio-mac.
    STATE["door_rows"] = [
        {"message_id": str(uuid.uuid4()), "message_kind": "endpoint_request", "from_address": "kestrel.joel",
         "from_entity_id": 43, "from_user_email": "priya@northwind.example", "subject": "Connection request",
         "body": "Joel's team", "to_user_email": ME,
         "payload": {"from_user_name": "Priya Shah", "from_display_name": "Joel's team"}, "at": at(25)},
        {"message_id": str(uuid.uuid4()), "message_kind": "system_notification", "from_address": None,
         "from_entity_id": None, "from_user_email": None, "subject": "New peer: Lark", "body": "",
         "to_user_email": None, "payload": {"kind": "new_peer", "token_id": 5, "peer_host_name": "lark-host"},
         "at": at(20)},
    ]
    # Your Host's pages.
    t = STATE["turns"]
    t[TODAY] = [turn_row("user", "Morning. What's on today?", 6000),
                     turn_row("assistant", "Dinner with Ana on Thursday, and Dad's birthday on the 3rd.", 5999,
                              model="claude-opus-5-5", provider="anthropic", gen_ms=900, turn_token="t-orig",
                              tokens_in=1000, tokens_out=20, tokens_total=1020)]
    t[DINNER] = [turn_row("user", "Book Corvo for two on Thursday at 7, Ana and me.", 1460),
                 turn_row("assistant", "Booked: Corvo, Thursday 7:00 pm, a table for two. It's on your calendar.", 1459,
                          model="claude-opus-5-5", provider="anthropic", gen_ms=3400, turn_token="t-din",
                          tokens_in=3000, tokens_out=400, tokens_total=3400, est_cost_usd=0.03, price_basis="list")]
    t[GIFT] = [turn_row("user", "Ideas for Dad's birthday on the 3rd? Under $80.", 4000),
               turn_row("assistant", "A headlamp, a record cleaning kit, or a framed print of the lake.", 3990,
                        model="claude-opus-5-5", provider="anthropic", gen_ms=5000, turn_token="t-gift")]
    t[BACKUPS] = [turn_row("assistant", "Nightly backup done: 1,281 files, 1.9 GB changed.", 600,
                           model="claude-haiku-4-5", provider="anthropic", gen_ms=800, turn_token="t-bk")]
    t[OLD] = [turn_row("user", "Trip notes", 20000)]
    for tid, rows in t.items():
        STATE["landed"][tid] = max((r["created_at"] for r in rows if r["metadata"]["role"] == "assistant"),
                                   default=None)
        STATE["marks"][("self", tid, "")] = at(0.5)
    # A topic marker on Gift for Dad, pinned to its reply (Split here moves
    # the reply on to a new thread).
    STATE["markers"][GIFT] = [{"summary_id": 31, "topic": "Dad's birthday gift", "prev_topic": None,
                               "at": iso(at(3995)), "from_turn_id": t[GIFT][1]["id"]}]


# ── a pool that answers the real readers' SQL from the rows above ───────────
def contact_by_email(email):
    return next((c for c in STATE["contacts"] if c["kind"] == "human" and c["handle"].lower() == email), None)


def blocked_sets():
    hosts = {c["peer_entity_id"] for c in STATE["contacts"] if c["status"] == "blocked" and c["peer_entity_id"]}
    people = {c["handle"].lower() for c in STATE["contacts"] if c["status"] == "blocked" and c["kind"] == "human"}
    return hosts, people


def topic_of(r):
    t = (r["payload"] or {}).get("topic_id")
    return t if isinstance(t, str) and re.fullmatch(r"[0-9a-f-]{36}", t) else "original"


def pair_rows(peer):
    return [r for r in STATE["inbox"] if r["message_kind"] == "message" and r["archived_at"] is None
            and {r["from_entity_id"], r["to_entity_id"]} == {ENTITY, peer}
            and not (r["from_user_email"] and r["to_user_email"])]


def people_rows(peer, me):
    return [r for r in STATE["inbox"] if r["message_kind"] == "message" and r["archived_at"] is None
            and r["from_user_email"] and r["to_user_email"]
            and ((r["from_entity_id"] == ENTITY and r["from_user_email"] == me and r["to_user_email"] == peer)
                 or (r["to_entity_id"] == ENTITY and r["to_user_email"] == me and r["from_user_email"] == peer))]


def page_of(rows, n, cur_ts=None, cur_id=None):
    """messages.sized_page_sql over the mock's rows: newest first, older than
    the cursor ((sent_at, message_id) < ($cur, $cur+1)) when one is given."""
    if cur_ts is not None:
        rows = [r for r in rows if (r["sent_at"], r["message_id"]) < (cur_ts, str(cur_id))]
    rows = sorted(rows, key=lambda r: (r["sent_at"], r["message_id"]), reverse=True)[:n]
    return [{**r, "ts": r["sent_at"], "topic": topic_of(r), "fetched": len(rows)} for r in rows]


class FakePool:
    """The SQL of the readers the mock runs, answered from STATE. Each branch
    names the query it stands for; anything else raises, so a reader that
    starts asking something new fails here rather than passing on a guess."""

    async def fetchrow(self, sql, *args):
        if "AS hosts" in sql and "AS people" in sql:  # contacts._BLOCKED_SETS_SQL
            h, p = blocked_sets()
            return {"hosts": sorted(h), "people": sorted(p)}
        raise AssertionError(f"FakePool.fetchrow: unexpected SQL {sql[:120]!r}")

    async def fetchval(self, sql, *args):
        if sql.startswith("SELECT count(*) FROM inbox_messages"):
            if isinstance(args[1], str):  # a person's chat: (me, peer, operator)
                return len(people_rows(args[1], args[2]))
            rows = pair_rows(int(args[1]))
            if len(args) > 2:
                rows = [r for r in rows if topic_of(r) == args[2]]
            return len(rows)
        if "= ANY (host_reads)" in sql:  # circles_reading.is_on
            c = contact_by_email(args[1])
            return bool(c and args[0] in c["host_reads"])
        if "FROM circles_page_marks" in sql:  # list_topics: the original's own mark
            return STATE["marks"].get(("entity", args[1], ""))
        raise AssertionError(f"FakePool.fetchval: unexpected SQL {sql[:120]!r}")

    async def fetch(self, sql, *args):
        if "kind = 'human' AND lower(handle) = ANY" in sql:  # circles_deck.pair, people
            return [{"id": c["id"], "email": c["handle"].lower(), "peer_entity_id": c["peer_entity_id"]}
                    for c in STATE["contacts"] if c["kind"] == "human" and c["handle"].lower() in args[0]]
        if "kind = 'host' AND peer_entity_id = ANY" in sql:  # circles_deck.pair, hosts
            best: dict = {}
            for c in sorted(STATE["contacts"],
                            key=lambda c: (c["status"] != "blocked", c["status"] != "active", c["id"])):
                if c["kind"] == "host" and c["peer_entity_id"] in args[0]:
                    best.setdefault(c["peer_entity_id"], {"id": c["id"], "peer_entity_id": c["peer_entity_id"],
                                                          "status": c["status"]})
            return list(best.values())
        if "FROM inbox_messages" in sql and "message_kind = ANY($2::text[])" in sql:  # circles_deck._DOOR_SQL
            return [r for r in STATE["door_rows"] if not r.get("archived")]
        if "FROM network_bastion_peer" in sql:  # peer_discovery.list_peerings
            return [dict(p) for p in STATE["peerings"]]
        if "SELECT DISTINCT peer_entity_id FROM contacts" in sql:  # door: Hosts you hold
            return [{"peer_entity_id": c["peer_entity_id"]} for c in STATE["contacts"]
                    if c["kind"] == "host" and c["peer_entity_id"]]
        if "FROM groups WHERE group_id = ANY" in sql:
            return [g for g in STATE["groups"] if g["group_id"] in args[0]]
        if "FROM groups g" in sql:  # roster.list_groups
            return [dict(g) for g in STATE["groups"]]
        if "FROM contacts" in sql and "ORDER BY COALESCE(display_name, handle), id" in sql:
            # contacts.list_contacts_service: status = $1 and/or q ILIKE, else all
            a = list(args)
            status = a.pop(0) if "status = $1" in sql else None
            pat = str(a.pop(0)).strip("%").lower() if "ILIKE" in sql else None
            return [{"notes": None, "last_contact_at": None, "created_at": None, **c}
                    for c in sorted(STATE["contacts"], key=lambda c: (c["display_name"] or c["handle"], c["id"]))
                    if (status is None or c["status"] == status)
                    and (pat is None or pat in c["handle"].lower() or pat in (c["display_name"] or "").lower())]
        if "kind = 'human' AND status = 'blocked'" in sql and "GROUP BY peer_entity_id" in sql:
            through: dict = {}  # contacts.list_contacts_service: the people a Host is blocked through
            for c in sorted(STATE["contacts"], key=lambda c: c["id"]):
                if c["kind"] == "human" and c["status"] == "blocked" and c["peer_entity_id"] is not None:
                    through.setdefault(c["peer_entity_id"], []).append(c["display_name"] or c["handle"])
            return [{"peer_entity_id": k, "names": v} for k, v in through.items()]
        if "FROM network_machine_offers" in sql:
            return []
        if "FROM entity_tickets WHERE entity_id = $1 AND status = 'open'" in sql:  # circles_deck.waiting
            return [{**t, "asked_in": json.dumps(t["metadata"].get("asked_in") or {}),
                     "options": json.dumps(t["options"])}
                    for t in STATE["tickets"].values() if t["status"] == "open"]
        if "FROM circles_threads" in sql:  # waiting: the thread a decision was asked in
            return [{"id": tid, "title": th["title"], "closed_at": th["closed_at"]}
                    for tid, th in STATE["threads"].items() if tid in args[1]]
        if "FROM projects WHERE entity_id" in sql:
            return [{"id": pid, "name": n} for pid, n in STATE["projects"].items() if pid in args[1]]
        if "FROM support_tickets WHERE entity_id = $1 AND status = 'draft'" in sql:
            return [{"id": s["id"], "title": s["title"], "category": s["category"], "severity": s["severity"],
                     "project_id": None, "project_name": "", "opened_at": s["opened_at"],
                     "updated_at": s["opened_at"], "shared_items": len(s["shared_context"]),
                     "metadata": {"revision": s["revision"]}}
                    for s in STATE["support"].values() if s["status"] == "draft"]
        if sql.lstrip().startswith("WITH pair AS"):  # topics.list_topics
            peer = int(args[1])
            groups: dict = {}
            for r in sorted(pair_rows(peer), key=lambda r: (r["sent_at"], r["message_id"])):
                groups.setdefault(topic_of(r), []).append(r)
            out = []
            for tid, rows in groups.items():
                pm = STATE["marks"].get(("entity", str(peer), ""))
                tm = STATE["marks"].get(("entity", f"{peer}/{tid}", ""))
                seen = max([x for x in (pm, tm) if x], default=None)
                titles = [r["payload"]["topic_title"] for r in rows if (r["payload"] or {}).get("topic_title")]
                out.append({"topic": tid, "messages": len(rows), "last_activity_at": max(r["sent_at"] for r in rows),
                            "last_inbound_at": max([r["sent_at"] for r in rows if r["from_entity_id"] == peer],
                                                   default=None),
                            "title": titles[0] if titles else None, "started_by": rows[0]["from_entity_id"],
                            "seen_at": tm,
                            "unread": sum(1 for r in rows if r["from_entity_id"] == peer
                                          and (seen is None or r["sent_at"] > seen))})
            return out
        if "fetched" in sql and "FROM inbox_messages" in sql:  # messages.sized_page_sql
            if isinstance(args[1], str):  # a person's chat: (me, peer, operator, cur, cur, n+1, budget)
                return page_of(people_rows(args[1], args[2]), args[5], args[3], args[4])
            # a Host's: (me, peer, cur, cur, n+1, budget[, topic])
            rows = pair_rows(int(args[1]))
            if len(args) > 6:
                rows = [r for r in rows if topic_of(r) == args[6]]
            return page_of(rows, args[4], args[2], args[3])
        raise AssertionError(f"FakePool.fetch: unexpected SQL {sql[:160]!r}")


POOL = FakePool()


# The readers in server/routes/circles.py reach the pool through
# brain_service.get_pool() (circles.py:34 imports the module; brain_service.py
# :333 defines get_pool), the Host's id through _entity_id (:143) and its name
# through _entity_display_name (:150); the mock hands them its own.
async def _display_name():
    return HOST

real_circles.brain_service.get_pool = lambda: POOL
real_circles._entity_id = lambda: ENTITY
real_circles._entity_display_name = _display_name


# ── the turns (scripts/book_mock_turns.py) ──────────────────────────────────
class CirclesTurns(TurnHost):
    def settings(self) -> dict:
        return STATE["settings"]

    def thread_key(self, turn) -> str:
        # server/turn_registry.py compose_thread_key: a thread, or Circles'
        # own original conversation.
        return f"circles|{ENTITY}|none" if turn.page == "original" else f"thread|{ENTITY}|{turn.page}"

    def session_id(self, turn) -> str:
        return f"s-{turn.id}"

    def write_row(self, turn, role, content, created_at, metadata) -> None:
        STATE["row_seq"] += 1
        row = {"id": STATE["row_seq"], "content": content,
               "created_at": datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at,
               "metadata": {"role": role, **metadata}}
        if role == "assistant":
            row["landed_at"] = now()
        STATE["turns"].setdefault(turn.page, []).append(row)

    def landed(self, turn, t) -> None:
        STATE["landed"][turn.page] = t
        tot = STATE["totals"].get(turn.page)
        if tot is not None:
            tot["turns_counted"] += 1
            tot["calls"] += 2
            for k, v in REPLY_TOKENS.items():
                tot["tokens"][k] += v
            tot["tokens_total"] += sum(REPLY_TOKENS.values())
            tot["est_usd"] = round((tot["est_usd"] or 0) + REPLY_USD, 6)
            tot["turns_priced"] += 1

    def live(self, turn) -> bool:
        return TURNS.get(turn.id) is turn

    def move(self, turn, to) -> None:
        """thread_choice.move_exchange: the turn's rows so far go to topic
        `to`, and what it writes from now on lands there."""
        rows = STATE["turns"].get(turn.page, [])
        mine = [r for r in rows if r["metadata"].get("turn_token") == turn.token]
        STATE["turns"][turn.page] = [r for r in rows if r not in mine]
        STATE["turns"].setdefault(to, []).extend(mine)

    def requeue(self, turn, words):
        """turn_registry._requeue_undelivered: the Circles nudge route's words
        are the server's (pending_words), so words the turn ended
        without reading start the page's next turn, joined. In PII mode
        nothing was held: they are the page's to send."""
        if STATE["pii"]:
            return None
        nxt = MockTurn(CTURNS, turn.page, "\n\n".join(t for t, _at, _cid in words))
        TURNS[nxt.id] = nxt
        STATE["requeued"].append({"from": turn.id, "to": nxt.id, "text": nxt.message})
        asyncio.get_running_loop().create_task(nxt.run())
        return nxt


CTURNS = CirclesTurns()


def thread_entry(tid):
    """One of your Host's pages as GET /api/circles/threads lists it: the real
    entry builders (circles_threads._row_dict, _marks) over the
    mock's rows and marks, with the marks list_threads computes."""
    rows = STATE["turns"].get(tid, [])
    seen = STATE["marks"].get(("self", tid, ""))
    replies = [r for r in rows if r["metadata"]["role"] == "assistant"]
    landed = [(r.get("landed_at") or r["created_at"], r["metadata"].get("turn_token")) for r in replies]
    watched = STATE["seen_turns"].get(tid, [])
    unseen = max([x for x, tok in landed if seen and x > seen and tok not in watched], default=None)
    waiting = sum(1 for t in STATE["tickets"].values() if t["status"] == "open"
                  and (t["metadata"].get("asked_in") or {}).get("thread_id") == tid)
    marks = {"seen_at": seen, "last_activity_at": max([r["created_at"] for r in rows], default=None),
             "last_landed_at": max([x for x, _ in landed], default=None), "unseen_landed_at": unseen,
             "waiting_on_you": waiting}
    th = STATE["threads"][tid]
    e = real_ct._row_dict({"id": tid, "title": th["title"], "topic": None, "created_at": th["created_at"],
                           "closed_at": th["closed_at"], "origin": "user", "parent_thread_id": None,
                           "type_id": None, "type_name": None}, len(rows))
    e.update(real_ct._marks(marks))
    return e


# ── the app ─────────────────────────────────────────────────────────────────
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

app = FastAPI()


def wire(obj):
    return json.loads(json.dumps(obj, default=str))


@app.middleware("http")
async def keep_urls(request: Request, call_next):
    STATE.setdefault("urls", []).append(str(request.url))
    return await call_next(request)


@app.get("/api/circles/conversations")
async def conversations():
    """list_conversations: the raw lists the server reads, then the real
    pairing (circles_deck.pair), the unread fields the server adds
    (circles_marks), and the real groups read (circles_deck.groups)."""
    people, entities = {}, {}
    for r in STATE["inbox"]:
        if r["message_kind"] != "message" or r["archived_at"]:
            continue
        if r["from_user_email"] and r["to_user_email"]:
            peer = r["to_user_email"] if r["from_user_email"] == ME else r["from_user_email"]
            name = (r["payload"].get("peer_user_name") if r["from_user_email"] == ME
                    else r["payload"].get("from_user_name"))
            cur = people.get(peer)
            if cur is None or r["sent_at"] > cur["_ts"]:
                people[peer] = {"id": peer, "kind": "dm", "display_name": name or peer,
                                "preview": (r["body"] or "")[:80], "unread_count": 0, "_ts": r["sent_at"]}
        else:
            peer = r["to_entity_id"] if r["from_entity_id"] == ENTITY else r["from_entity_id"]
            cur = entities.get(peer)
            if cur is None or r["sent_at"] > cur["_ts"]:
                entities[peer] = {"id": str(peer), "kind": "entity",
                                  "display_name": STATE["host_names"].get(peer, f"Host {peer}"),
                                  "preview": (r["body"] or "")[:80], "unread_count": 0, "_ts": r["sent_at"]}
    for c in STATE["contacts"]:  # human contacts with no thread yet
        if c["kind"] == "human" and c["handle"].lower() not in people:
            people[c["handle"].lower()] = {"id": c["handle"].lower(), "kind": "dm",
                                           "display_name": c["display_name"], "preview": "", "unread_count": 0,
                                           "pending": c["status"] == "pending"}
    for p in STATE["peerings"]:  # a Host on your machine with no thread yet
        pid = p["peer_entity_id"]
        if pid not in entities and not any(c["peer_entity_id"] == pid and c["kind"] == "host"
                                           for c in STATE["contacts"]):
            entities[pid] = {"id": str(pid), "kind": "entity", "display_name": p["peer_handle"],
                             "preview": f"shares your machine {p['bastion_name']}", "unread_count": 0, "peer": True}
    people_items = [{k: v for k, v in x.items() if k != "_ts"} for x in people.values()]
    entity_items = [{k: v for k, v in x.items() if k != "_ts"} for x in entities.values()]
    people_items, entity_items = await circles_deck.pair(POOL, people_items, entity_items)
    for p in people_items:
        mark = STATE["marks"].get(("dm", p["id"], ME))
        theirs = [r["sent_at"] for r in people_rows(p["id"], ME) if r["from_user_email"] == p["id"]]
        n = sum(1 for t in theirs if mark is None or t > mark)
        p.update({"unread": n, "unread_count": n, "seen_at": iso(mark),
                  "last_inbound_at": iso(max(theirs, default=None))})
    for e in entity_items:
        peer = int(e["id"])
        mark = STATE["marks"].get(("entity", str(peer), ""))
        rows = [r for r in pair_rows(peer) if r["from_entity_id"] == peer]
        n = 0
        for r in rows:
            tm = STATE["marks"].get(("entity", f"{peer}/{topic_of(r)}", ""))
            seen = max([x for x in (mark, tm) if x], default=None)
            n += seen is None or r["sent_at"] > seen
        e.update({"unread": n, "unread_count": n, "seen_at": iso(mark),
                  "last_inbound_at": iso(max([r["sent_at"] for r in rows], default=None))})
    circles = [{"id": cid, "display_name": c["name"], "unread": 0, "unread_count": 0,
                "last_inbound_at": max(m["created_at"] for m in c["messages"])}
               for cid, c in STATE["rooms"].items()]
    groups = await circles_deck.groups(POOL, ENTITY)
    return wire({"self": {"id": "self", "kind": "self", "display_name": HOST, "unread_count": 0},
                 "people": people_items, "entities": entity_items, "circles": circles, "groups": groups})


@app.get("/api/circles/door")
async def door():
    items = await circles_deck.door(POOL, ENTITY, ME)
    return wire({"items": items, "count": len(items)})


@app.get("/api/circles/waiting")
async def waiting():
    return wire(await circles_deck.waiting(POOL, ENTITY, ME))


@app.get("/api/circles/threads")
def threads():
    # circles_threads.list_threads: the threads, newest first; no original
    # conversation entry.
    order = sorted(STATE["threads"], key=lambda t: STATE["threads"][t]["created_at"], reverse=True)
    return wire({"threads": [thread_entry(t) for t in order]})


@app.get("/api/circles/last-open")
def last_open():
    return {"ref": STATE["last_open"]}


@app.put("/api/circles/last-open")
async def put_last_open(req: Request):
    STATE["last_open"] = (await req.json()).get("ref")
    return {"ref": STATE["last_open"]}


@app.post("/api/circles/threads")
async def new_thread(req: Request):
    tid = str(uuid.uuid4())
    STATE["threads"][tid] = {"title": None, "created_at": now(), "closed_at": None}
    STATE["turns"][tid] = []
    return JSONResponse(wire({"thread": thread_entry(tid)}), status_code=201)


@app.patch("/api/circles/threads/{tid}")
async def patch_thread(tid: str, req: Request):
    body = await req.json()
    STATE["thread_calls"].append({"call": "patch", "id": tid, **body})
    if "closed" in body:
        STATE["threads"][tid]["closed_at"] = now() if body["closed"] else None
    if body.get("title"):
        STATE["threads"][tid]["title"] = body["title"]
    return wire({"thread": thread_entry(tid)})


# ── what the rail's thread list and chat view did ──────────────────────────
@app.delete("/api/circles/threads/{tid}")
def delete_thread_route(tid: str):
    STATE["thread_calls"].append({"call": "delete", "id": tid})
    cancelled = len(STATE["wakeups"].pop(tid, []))
    STATE["threads"].pop(tid, None)
    rows = STATE["turns"].pop(tid, [])
    return {"ok": True, "deleted": {"id": tid, "messages": len(rows), "learned": 0, "cancelled": cancelled}}


@app.get("/api/circles/threads/{tid}/wakeups")
def wakeups(tid: str):
    return {"wakeups": STATE["wakeups"].get(tid, [])}


@app.get("/api/circles/threads/{tid}/markers")
def markers(tid: str):
    return {"markers": STATE["markers"].get(tid, [])}


@app.post("/api/circles/threads/{tid}/split")
async def split(tid: str, req: Request):
    body = await req.json()
    STATE["thread_calls"].append({"call": "split", "id": tid, **body})
    new = str(uuid.uuid4())
    rows = STATE["turns"].get(tid, [])
    keep = [r for r in rows if r["id"] < body["from_turn_id"]]
    moved = [r for r in rows if r["id"] >= body["from_turn_id"]]
    STATE["turns"][tid] = keep
    STATE["turns"][new] = moved
    STATE["threads"][new] = {"title": "Dad's birthday gift", "created_at": now(), "closed_at": None}
    STATE["marks"][("self", new, "")] = now()
    STATE["markers"][tid] = []
    return JSONResponse(wire({"thread": thread_entry(new)}), status_code=201)


@app.get("/api/turn-types")
def turn_types():
    return {"types": STATE["turn_types"]}


@app.delete("/api/circles/thread/{kind}/{ref}")
def clear_conversation(kind: str, ref: str):
    """circles.delete_thread: a page's rows archived, a pair's or a person's
    messages archived, a room left."""
    STATE["clears"].append({"kind": kind, "ref": ref})
    n = 0
    if kind == "self":
        n = len(STATE["turns"].get(ref, []))
        STATE["turns"][ref] = []
    elif kind == "dm":
        for r in people_rows(ref, ME):
            r["archived_at"] = now()
            n += 1
    elif kind == "entity":
        for r in pair_rows(int(ref)):
            r["archived_at"] = now()
            n += 1
    elif kind == "circle":
        STATE["rooms"].pop(int(ref), None)
        n = 1
    return {"ok": True, "cleared": n}


@app.get("/api/inference/activity")
def background_activity(limit: int = 20, hours: int = 24, failed: bool = False):
    """server/routes/inference.py get_background_activity: the rows newest
    first (only the failed ones with failed=true), at most 100, and `issues`
    counting the failed ones."""
    rows = [r for r in STATE["activity"] if not (failed and r["ok"])]
    rows = sorted(rows, key=lambda r: (r["at"], r["id"]), reverse=True)[:max(1, min(limit, 100))]
    return {"activity": rows, "issues": sum(1 for r in STATE["activity"] if not r["ok"]), "window_hours": hours}


@app.get("/api/agents")
def agents(active_only: bool = False, limit: int = 20):
    """server/routes/agents.py list_agents: the dispatched agents, newest
    first; active_only keeps the queued and running ones."""
    rows = [a for a in STATE["agents"] if not active_only or a["status"] in ("queued", "in_progress")]
    return {"agents": rows[:limit]}


@app.get("/api/circles/thread/self/{tid}/page")
def page(tid: str):
    e = thread_entry(tid)
    stands = None
    if tid == DINNER:
        stands = {"text": "…", "goal": "Table for 2 at Corvo, Thursday 7:00 pm. Invite sent to Ana.",
                  "goal_at": iso(at(1459)), "next_step": None, "open_work": [], "identifiers": [],
                  "handoff_at": iso(STATE["landed"].get(DINNER))}
    out = {"kind": "self", "id": tid, "title": e["title"], "scope": {"kind": "thread", "ref": tid},
           "where_this_stands": stands, "totals": STATE["totals"].get(tid),
           "totals_vs_transcript": {"same": True, "shown": 0, "counted": 0, "shown_not_counted": 0,
                                    "counted_not_shown": 0},
           **{k: e[k] for k in ("seen_at", "last_activity_at", "last_landed_at", "unseen_landed_at",
                                "waiting_on_you", "ask_cards_open", "turn_count")}}
    STATE["page_keys"] = set(out)
    return wire(out)


@app.get("/api/circles/thread/self/{tid}")
def self_thread(tid: str):
    rows = sorted(STATE["turns"].get(tid, []), key=lambda r: (r["created_at"], r["id"]))
    return wire({"messages": real_circles._normalize_turn_rows(rows, HOST), "members": [],
                 "policy": {"private": True}, "thread_id": tid})


@app.get("/api/circles/thread/dm/{email}")
async def person_thread(email: str, before: str | None = None):
    return wire(await real_circles._read_people_thread(email, ME, before=before))


@app.get("/api/circles/thread/entity/{peer}/topics")
async def topics(peer: int):
    return wire({"kind": "entity", "id": str(peer), "topics": await real_topics.list_topics(POOL, ENTITY, peer)})


# A person claim the Host could not check is filed as the sending Host's
# own message, and the real reader says whom it named (`claimed_person`,
# server/routes/circles.py _read_entity_thread): read as it is.
@app.get("/api/circles/thread/entity/{peer}")
async def entity_thread(peer: int, topic: str | None = None, before: str | None = None):
    t = real_topics.parse(topic) if topic is not None else None
    return wire(await real_circles._read_entity_thread(peer, topic=t, before=before))


ROOM_PAGE = 50


@app.get("/api/circles/thread/circle/{cid}")
def room_thread(cid: int, before: str | None = None):
    """get_thread's circle branch over the bridge's circles.messages: a page
    of ROOM_PAGE, newest first, with total and an opaque next_before when the
    room holds more (the mock's page is small; the real one is 200)."""
    room = STATE["rooms"].get(cid) or {"messages": []}
    allm = sorted(room["messages"], key=lambda m: m["id"])
    end = int(before.split(":")[1]) if before else len(allm)
    page = allm[max(0, end - ROOM_PAGE):end]
    out = {"messages": [{"author_name": HOST if m["sender_entity_id"] == ENTITY else m["sender_name"],
                         "author_kind": "self" if m["sender_entity_id"] == ENTITY else "entity",
                         "body": m["content"], "created_at": m["created_at"], "id": f"circle-{m['id']}"}
                        for m in page], "members": [], "policy": {}}
    if len(allm) > ROOM_PAGE:
        start = max(0, end - ROOM_PAGE)
        out.update({"total": len(allm), "next_before": f"room:{start}" if start > 0 else None})
    return out


@app.put("/api/circles/thread/{kind}/{ref}/seen")
async def seen(kind: str, ref: str, req: Request, topic: str | None = None):
    raw = await req.body()
    token = (json.loads(raw) or {}).get("turn_token") if raw else None
    key = f"{ref}/{topic}" if topic else ref
    reader = ME if kind == "dm" else ""
    t = now()
    STATE["marks"][(kind, key, reader)] = max(t, STATE["marks"].get((kind, key, reader)) or t)
    if token:
        STATE["seen_turns"].setdefault(ref, []).append(token)
    STATE["seen_puts"].append({"kind": kind, "id": key, "token": token, "at": t})
    return {"kind": kind, "id": key, "seen_at": iso(STATE["marks"][(kind, key, reader)])}


@app.put("/api/circles/thread/dm/{email}/reading")
async def reading(email: str, req: Request):
    """set_circles_reading: the switch, and the notice on both sides; ours is
    stored in the chat (the real notice texts), theirs goes to them."""
    on = (await req.json()).get("on")
    c = contact_by_email(email)
    STATE["reading_puts"].append({"email": email, "on": on})
    if bool(ME in c["host_reads"]) == on:
        return {"kind": "dm", "id": email, "on": on, "changed": False}
    ours, theirs = circles_reading.notice_texts(HOST, on)
    if on:
        c["host_reads"].append(ME)
    else:
        c["host_reads"].remove(ME)
    STATE["inbox"].append(msg(ENTITY, c["peer_entity_id"], ours, 0, frm_p=ME, to_p=email,
                              payload={"peer_user_name": c["display_name"], "reading_notice": "on" if on else "off"}))
    STATE.setdefault("notices_sent", []).append({"to": email, "text": theirs})
    return {"kind": "dm", "id": email, "on": on, "changed": True, "notice": ours, "delivered": True}


# ── one conversation and parallel work ──────────────────────────────────────
def _topic_of_page(tid):
    """The feed's topic for a page (routes/circles.read_feed): a live thread."""
    th = STATE["threads"].get(tid) or {}
    return {"id": tid, "title": th.get("title") or "Untitled topic", "root": bool(th.get("root"))}


def _new_topic(text):
    """A topic started for a message (thread_pick.land_topic / circles_threads
    .home_thread), titled from it."""
    tid = str(uuid.uuid4())
    STATE["threads"][tid] = {"title": real_ct.home_title(text), "created_at": now(), "closed_at": None}
    STATE["turns"].setdefault(tid, [])
    return tid


@app.get("/api/circles/feed")
def feed(before: str | None = None, before_id: int | None = None, thread: str | None = None):
    """routes/circles.read_feed over the mock's pages: every topic as one
    conversation, a page at a time, newest page first, oldest first in it,
    each row naming its topic; `thread`, one topic's rows only."""
    STATE["feed_reads"].append(thread)
    # Topics only: a row in no live thread is shown nowhere (read_feed).
    rows = [(r, _topic_of_page(tid)) for tid, rs in STATE["turns"].items() if tid in STATE["threads"]
            and (thread is None or tid == thread) for r in rs]
    rows.sort(key=lambda x: (x[0]["created_at"], x[0]["id"]))
    if before:
        cut = (datetime.fromisoformat(before), before_id if before_id is not None else 2**62)
        rows = [x for x in rows if (x[0]["created_at"], x[0]["id"]) < cut]
    n = real_circles._FEED_PAGE
    more, page = len(rows) > n, rows[-n:]
    topic = {r["id"]: (t, r["metadata"].get("source_kind") == "topic_pick") for r, t in page}
    msgs = real_circles._normalize_turn_rows([r for r, _ in page], HOST)
    for m in msgs:
        t, marker = topic.get(m.get("id"), ({}, False))
        m["feed_topic"] = t
        if marker:
            m["topic_marker"] = True
    oldest = page[0][0] if page else None
    nxt = {"before": oldest["created_at"].isoformat(), "before_id": oldest["id"]} if more and oldest else None
    return wire({"messages": msgs, "next": nxt})


def pick_topic(body) -> dict:
    """thread_pick.pick_topic, scripted (STATE["pick"]), in _topic_json's
    shape. The root thread is never picked."""
    cur = str(body.get("thread_id") or "").strip()
    if cur == "original":
        cur = ""
    dest = STATE["pick"] or cur
    new = not dest
    if new:
        dest = _new_topic(str(body.get("body") or ""))
    t = _topic_of_page(dest)
    moved = dest != cur
    out = {"thread_id": dest, "title": t["title"], "new": new, "moved": moved,
           "marker": f"continuing: {t['title']}" if moved else "", "picked": True, "root": False,
           "follow_on": not moved, "related": []}
    STATE["topic_picks"].append({"text": body.get("body"), "from": cur or None, **out})
    return out


@app.post("/api/circles/topic")
async def topic_pick_route(req: Request):
    """POST /api/circles/topic: the pick for words typed while a turn runs."""
    return pick_topic(await req.json())


@app.get("/api/circles/work")
def work():
    """routes/circles.read_work: the internal threads of running runs."""
    return {"threads": STATE["work"]}


@app.post("/api/circles/messages")
async def messages(req: Request):
    body = await req.json()
    STATE["sent"].append(body)
    kind, target = body.get("target_kind"), str(body.get("target_id"))
    text = str(body.get("body") or "").strip()
    if kind == "dm":
        c = contact_by_email(target)
        STATE["inbox"].append(msg(ENTITY, c["peer_entity_id"], text, 0, frm_p=ME, to_p=target,
                                  payload={"peer_user_name": c["display_name"]}))
        return {"ok": True, "delivered": True}
    if kind == "entity":
        peer = int(target)
        topic = {"id": "original", "title": real_topics.ORIGINAL_TITLE}
        if body.get("new_topic"):
            topic = {"id": str(uuid.uuid4()), "title": real_topics.clean_title(body["new_topic"])}
        elif body.get("topic_id") and body["topic_id"] != "original":
            topic = {"id": body["topic_id"], "title": None}
        payload = {"peer_display_name": STATE["host_names"].get(peer)}
        if topic["id"] != "original":
            payload.update({"topic_id": topic["id"], "topic_title": topic["title"]})
        STATE["inbox"].append(msg(ENTITY, peer, text, 0, payload=payload))
        return {"ok": True, "topic": topic}
    if kind == "circle":
        room = STATE["rooms"][int(target)]
        room["messages"].append({"id": len(room["messages"]) + 1, "sender_entity_id": ENTITY, "sender_name": HOST,
                                 "content": text, "created_at": iso(now())})
        return {"ok": True}
    # Your own Host: a turn on the topic the Host picks (pick_topic: true,
    # the send route's _topic_for), else on the page named (thread_id, picked
    # already or a link's; none is the original).
    picked = pick_topic({**body}) if body.get("pick_topic") is True else None
    page_id = picked["thread_id"] if picked else str(body.get("thread_id") or "")
    if page_id in ("", "original"):
        # routes/circles: a message that names no topic is picked as any
        # message is, else starts one; never the original conversation.
        picked = pick_topic({**body, "thread_id": ""})
        page_id = picked["thread_id"]
    turn = MockTurn(CTURNS, page_id, text)
    carry = body.get("carry")
    if carry is not None:
        # The server reads the carried message itself, only from the
        # operator's own chat (circles_reading.carried_message), and quotes
        # it fenced (circles_reading.fence).
        row = next((r for r in people_rows(carry["ref"], ME) if r["message_id"] == carry["message_id"]), None)
        if row is None:
            return JSONResponse({"detail": {"ok": False, "error": {"code": "no_such_message"}}}, status_code=404)
        c = contact_by_email(carry["ref"])
        who = f"{c['display_name']} ({carry['ref']})" if c else carry["ref"]
        quote = circles_reading.fence(who, row["sent_at"], row["body"])
        turn.message = quote + (f"\n\n{text}" if text else "")
        turn.user_row_meta = {"carry": {"kind": "dm", "ref": carry["ref"], "message_id": carry["message_id"]}}
    TURNS[turn.id] = turn
    asyncio.get_running_loop().create_task(turn.run())
    hdrs = {"x-landed-thread": page_id, "x-landed-at": iso(now())}
    if picked:
        from urllib.parse import quote as _q
        hdrs.update({"x-topic-title": _q(picked["title"], safe=""), "x-topic-marker": _q(picked["marker"], safe=""),
                     "x-topic-new": "0"})
    return StreamingResponse(turn.tail(0), media_type="text/event-stream", headers=hdrs)


@app.post("/api/circles/nudge")
async def nudge(req: Request):
    """POST /api/circles/nudge: into the running turn (session.push_nudge, no
    registry frame), read at its next step; 409 once it stopped reading."""
    body = await req.json()
    t = next((x for x in TURNS.values() if CTURNS.session_id(x) == body.get("session_id")), None)
    cid = body.get("client_id") if isinstance(body.get("client_id"), str) else None
    ok = t is not None and t.nudge(str(body.get("text") or "").strip(), cid, refused=STATE["nudge_409"])
    STATE["nudges"].append({"text": body.get("text"), "client_id": cid, "status": 200 if ok else 409,
                            "held": not STATE["pii"]})
    if not ok:
        return JSONResponse({"detail": {"ok": False, "error": {"code": "turn_stopped_reading"}}}, status_code=409)
    return {"ok": True, "session_id": body.get("session_id"), "queued_chars": len(body.get("text") or ""),
            "held": not STATE["pii"]}


@app.get("/api/circles/pending")
def pending(thread: str | None = None):
    """GET /api/circles/pending: the words the server holds for the page —
    sent into its running turn and not read yet (pending_words)."""
    page_id = (thread or "original").lower()
    # PII mode writes nothing, so nothing is listed (pending_words.held_for).
    words = [] if STATE["pii"] else [
        {"id": f"pw-{i}", "text": text, "client_id": cid, "received_at": iso(at)}
        for t in TURNS.values() if t.page == page_id and not t.done
        for i, (text, at, cid) in enumerate(t.pending)]
    STATE["pending_reads"].append(len(words))
    return {"ok": True, "words": words}


@app.get("/api/circles/turns/active")
def turns_active(thread: str | None = None):
    page_id = (thread or "original").lower()
    live = list(TURNS.values())
    turn = next((t.describe() for t in live if t.page == page_id and not t.done), None)
    return {"ok": True, "turn": turn, "turns": [t.describe() for t in live]}


@app.get("/api/circles/turns/{turn_id}/stream")
def turn_stream(turn_id: str, from_seq: int = 0):
    t = TURNS.get(turn_id)
    if t is None:
        return JSONResponse({"detail": "unknown turn"}, status_code=404)
    return StreamingResponse(t.tail(max(0, from_seq), mark_replayed=True), media_type="text/event-stream")


@app.post("/api/circles/turns/{turn_id}/cancel")
def turn_cancel(turn_id: str):
    t = TURNS.get(turn_id)
    STATE["cancels"].append(turn_id)
    if t is None:
        return JSONResponse({"detail": "unknown turn"}, status_code=404)
    t.cancel()
    return {"ok": True, "status": "cancelled"}


def ticket_wire(t):
    return wire({**t, "ticket_uuid": str(t["id"])})


@app.get("/api/tickets")
def tickets(status: str = "", asked_in: str = "", limit: int = 50):
    out = []
    for t in STATE["tickets"].values():
        a = t["metadata"].get("asked_in") or {}
        room = (f"thread:{a['thread_id']}" if a.get("thread_id")
                else ("thread:original" if a.get("app_id") == "circles" else ""))
        # thread:* is every topic of your Host's conversation at once
        # (routes/tickets.py CONVERSATION_ROOM): the original conversation
        # and every Circles thread of this Host.
        whole = asked_in == "thread:*" and (room == "thread:original" or (
            room.startswith("thread:") and room[len("thread:"):].lower() in STATE["threads"]))
        if (not status or t["status"] == status) and (not asked_in or room == asked_in or whole):
            out.append(ticket_wire(t))
    return {"tickets": out}


@app.get("/api/tickets/{tid}")
def ticket(tid: int):
    return ticket_wire(STATE["tickets"][tid])


@app.post("/api/tickets/{tid}/decide")
async def decide(tid: int, req: Request):
    body = await req.json()
    STATE["decides"].append({"id": tid, **body})
    STATE["tickets"][tid]["status"] = "decided"
    return {"ticket_id": tid, "pattern_id": None, "answered_into_turn": False, "answer_turn_started": False}


@app.post("/api/tickets/{tid}/dismiss")
def dismiss(tid: int):
    STATE["decides"].append({"id": tid, "dismissed": True})
    STATE["tickets"][tid]["status"] = "dismissed"
    return {"ticket_id": tid}


@app.get("/api/support")
def support_list(status: str = ""):
    STATE.setdefault("support_lists", []).append(status)
    want = {"active": {"draft", "sent"}}.get(status, {status} if status else None)
    return {"tickets": [{k: v for k, v in s.items() if k != "shared_context"} for s in STATE["support"].values()
                        if want is None or s["status"] in want]}


@app.get("/api/support/badge-counts")
def support_badges():
    drafts = sum(1 for s in STATE["support"].values() if s["status"] == "draft")
    return {"drafts": drafts, "needs_info": STATE["support_asks"], "total": drafts + STATE["support_asks"]}


# Where support requests go, set by a scenario. Kept apart from STATE, which
# open_cards resets before the page loads (and reads the route).
SUPPORT_LANE = {"lane": "platform"}


@app.get("/api/support/route")
def support_route():
    # The real answer for the lane the scenario sets (server/support_reach.py).
    return real_support_reach.route_view(SUPPORT_LANE["lane"])


@app.post("/api/support")
async def support_new(req: Request):
    body = await req.json()
    STATE["support_calls"].append({"call": "open", **body})
    sid = 70 + len(STATE["support"])
    STATE["support"][sid] = {"id": sid, "ticket_uuid": f"s{sid}", "category": body.get("category", "question"),
                             "project_name": "", "title": body.get("title") or "A question", "severity": "normal",
                             "problem": body.get("problem", ""), "status": "draft", "platform_status": "",
                             "platform_ref": "", "resolution": "", "pr_url": "", "opened_at": iso(now()),
                             "revision": 1, "thread": [], "shared_context": []}
    return {"ticket_id": sid, "status": "draft"}


@app.post("/api/support/{sid}/discard")
def support_discard(sid: int):
    STATE["support_calls"].append({"call": "discard", "id": sid})
    STATE["support"][sid]["status"] = "discarded"
    return {"ticket_id": sid}


@app.post("/api/support/{sid}/close")
def support_close(sid: int):
    STATE["support_calls"].append({"call": "close", "id": sid})
    STATE["support"][sid]["status"] = "closed"
    return {"ticket_id": sid}


@app.post("/api/support/{sid}/reply")
async def support_reply(sid: int, req: Request):
    body = await req.json()
    STATE["support_calls"].append({"call": "reply", "id": sid, **body})
    STATE["support"][sid]["thread"].append({"author": "user", "body": body.get("body", ""), "at": iso(now())})
    return {"ticket_id": sid}


@app.get("/api/support/{sid}")
def support_get(sid: int):
    return STATE["support"][sid]


@app.post("/api/support/{sid}/shared")
async def support_shared(sid: int, req: Request):
    body = await req.json()
    STATE["support_calls"].append({"call": "shared", "id": sid, **body})
    for s in STATE["support"][sid]["shared_context"]:
        s["included"] = bool(body["included"].get(s["key"], s["included"]))
    return {"ticket_id": sid, "shared_context": STATE["support"][sid]["shared_context"]}


@app.post("/api/support/{sid}/send")
async def support_send(sid: int, req: Request):
    body = await req.json()
    STATE["support_calls"].append({"call": "send", "id": sid, **body})
    s = STATE["support"][sid]
    if body.get("revision") != s["revision"]:
        return JSONResponse({"detail": "the draft changed"}, status_code=409)
    s["status"], s["platform_status"] = "sent", "received"
    return {"ticket_id": sid, "platform_id": 9001, "platform_status": "received"}


@app.get("/api/calendar/list")
def calendar(since: str = "", status: str = "", order: str = "desc", limit: int = 100):
    STATE["calendar_query"] = {"since": since, "status": status, "order": order, "limit": limit}
    ev = sorted(STATE["events"], key=lambda e: e["event_at"], reverse=order != "asc")
    return {"events": ev[:limit], "count": len(ev)}


@app.get("/api/groups/rendezvous")
def interests():
    return {"interests": [{"tag": t, "listed_at": iso(at(100))}
                          for t in ("film-photography", "trail-running", "sourdough")]}


@app.get("/api/groups/{gid}/ballots")
def ballots(gid: str):
    return {"group_id": gid, "ballots": [{"ballot_id": "b1", "question": "October pick",
                                          "choices": ["Piranesi", "The Overstory"], "closed_at": None}],
            "offset": 0, "next_offset": None}


@app.get("/api/groups/{gid}")
def group(gid: str):
    g = next(x for x in STATE["groups"] if x["group_id"] == gid)
    return {"group": {"group_id": gid, "name": g["name"], "description": "", "owner_entity_id": ENTITY},
            "owned": True, "members": [{"member_entity_id": e, "role": r, "display_name": n, "removed_at": None}
                                       for e, r, n in ((ENTITY, "owner", HOST), (42, "member", "Juno"),
                                                       (44, "member", "Wren"))]}


@app.get("/api/domain-policy")
def policy():
    return {"posture": "contacts", "rules": STATE["rules"]}


@app.post("/api/domain-policy/rules")
async def add_rule(req: Request):
    body = await req.json()
    rule = {"id": 100 + len(STATE["rules"]), "created_at": iso(now()), **body}
    STATE["rules"].append(rule)
    return {"rule": rule}


@app.delete("/api/domain-policy/rules/{rid}")
def delete_rule(rid: int):
    STATE["rules"] = [r for r in STATE["rules"] if r["id"] != rid]
    return {"deleted": rid}


@app.get("/api/circles/bastions")
def bastions():
    return {"machines": [{"token_id": 5, "machine": "studio-mac", "online": True, "heard": True, "last_seen_at": None,
                          "mode": "networked", "anchored": True, "anchor": None,
                          "hosts": [{"host_name": "lark-host", "handle": "Lark", "state": "active", "since": None,
                                     "host_id": 46, "current": True, "is_anchor": False}]}]}


@app.get("/api/phone-alias")
def alias():
    return {"alias": STATE["alias"]}


@app.get("/api/contacts/list")
async def list_contacts(q: str | None = None, status: str | None = None):
    return wire(await real_contacts.list_contacts_service(POOL, status=status, q=q))


@app.patch("/api/contacts/{cid}")
async def patch_contact(cid: int, req: Request):
    body = await req.json()
    STATE["contact_patches"].append({"id": cid, **body})
    c = next(x for x in STATE["contacts"] if x["id"] == cid)
    c.update({k: v for k, v in body.items() if k in ("status", "display_name", "notes")})
    return {"contact": {"id": c["id"], "handle": c["handle"], "display_name": c["display_name"], "kind": c["kind"],
                        "notes": None, "status": c["status"], "last_contact_at": None, "created_at": None,
                        "peer_entity_id": c["peer_entity_id"]}}


@app.delete("/api/contacts/{cid}")
def delete_contact(cid: int):
    STATE["contact_calls"].append({"call": "delete", "id": cid})
    STATE["contacts"] = [c for c in STATE["contacts"] if c["id"] != cid]
    return {"deleted": cid}


@app.post("/api/contacts")
async def create_contact(req: Request):
    body = await req.json()
    STATE["contact_calls"].append({"call": "create", **body})
    c = {"id": 40 + len(STATE["contact_calls"]), "kind": body["kind"], "handle": body["handle"],
         "display_name": body.get("display_name"), "status": "active", "peer_entity_id": None, "host_reads": []}
    STATE["contacts"].append(c)
    return {"contact": {**c, "notes": None, "last_contact_at": None, "created_at": None}}


@app.put("/api/phone-alias")
async def put_alias(req: Request):
    body = await req.json()
    STATE["alias_calls"].append({"call": "set", **body})
    STATE["alias"] = body["alias"]
    return {"alias": body["alias"]}


@app.post("/api/phone-alias/lookup")
async def lookup_alias(req: Request):
    body = await req.json()
    STATE["alias_calls"].append({"call": "lookup", **body})
    if body["alias"] == "+14155550100":
        return {"alias": body["alias"], "found": True, "entity_id": 43, "display_name": "Kestrel"}
    return {"alias": body["alias"], "found": False}


@app.post("/api/contacts/request-by-phone")
async def request_by_phone(req: Request):
    body = await req.json()
    STATE["alias_calls"].append({"call": "request", **body})
    return {"sent_to": "Kestrel", "message_id": 1}


@app.post("/api/vault/secrets")
async def vault(req: Request):
    body = await req.json()
    STATE["vault"].append({k: body[k] for k in ("name", "cred_type", "if_absent")})
    return {"ok": True, "id": "v1", "name": body["name"]}


@app.post("/api/groups/rendezvous")
async def set_interests(req: Request):
    body = await req.json()
    STATE["interest_calls"].append({"call": "set", **body})
    return {"tags": sorted(body["tags"]), "dropped": [], "ok": True, "listed": True}


@app.delete("/api/groups/rendezvous")
def withdraw_interest(tag: str | None = None):
    STATE["interest_calls"].append({"call": "withdraw", "tag": tag})
    return {"withdrawn": tag or "all", "ok": True, "tag": tag, "all": tag is None}


@app.get("/api/inbox/list")
def inbox_list(kind: str | None = None, limit: int = 100, unread: int | None = None):
    # A slow inbox read holds up what waits, and the door with it (a slow
    # Host, or the full gate's load): the links must still land in sight.
    time.sleep(STATE["settings"].get("inbox_list_delay", 0))
    rows = [m for m in STATE["inbox_notes"] if not m["archived_at"] and (kind is None or m["kind"] == kind)]
    return {"messages": [{k: v for k, v in m.items() if k not in ("body", "payload")} for m in rows][:limit],
            "count": len(rows)}


@app.get("/api/inbox/read/{mid}")
def inbox_read(mid: str):
    STATE["inbox_reads"].append(mid)
    m = next((x for x in STATE["inbox_notes"] if x["message_id"] == mid), None)
    if m is None:
        r = next((x for x in STATE["door_rows"] if x["message_id"] == mid), None)
        if r is None:
            return JSONResponse({"detail": "no such message"}, status_code=404)
        return wire({"message_id": mid, "kind": r["message_kind"], "subject": r["subject"], "body": r["body"],
                     "payload": r["payload"], "sent_at": r["at"], "from": r["from_address"], "read_at": iso(now())})
    m["read_at"] = iso(now())
    return m


@app.post("/api/groups/{gid}/join-requests/{eid}/accept")
async def accept_join(gid: str, eid: int, req: Request):
    STATE["door_calls"].append({"path": f"/api/groups/{gid}/join-requests/{eid}/accept"})
    for r in STATE["door_rows"]:
        if r["message_kind"] == "group_join" and r["payload"].get("group_id") == gid:
            r["archived"] = True
    return {"group_id": gid, "member_entity_id": eid, "contact_id": 50, "contact_grant_delivered": True,
            "added": True, "role": "member", "roster_epoch": 4, "invite": {"id": 1, "status": "delivered"}}


@app.post("/api/contacts/accept-request")
async def accept_request(req: Request):
    body = await req.json()
    STATE["door_calls"].append({"path": "/api/contacts/accept-request", "body": body})
    for r in STATE["door_rows"]:
        if r["message_id"] == body["message_id"]:
            r["archived"] = True
    STATE["contacts"].append({"id": 31, "kind": "human", "handle": body["handle"],
                              "display_name": body["display_name"], "status": "active",
                              "peer_entity_id": body.get("from_entity_id"), "host_reads": []})
    return {"contact": {"id": 31}}


@app.post("/api/inbox/archive/{mid}")
def archive(mid: str):
    STATE["door_calls"].append({"path": f"/api/inbox/archive/{mid}"})
    for r in STATE["door_rows"]:
        if r["message_id"] == mid:
            r["archived"] = True
    for m in STATE["inbox_notes"]:
        if m["message_id"] == mid:
            m["archived_at"] = iso(now())
    return {"message_id": mid, "archived": True}


@app.get("/api/inference/effort-options")
def effort_options():
    return {"provider": "anthropic", "model": "claude-opus-5-5", "efforts": ["low", "medium", "high", "max"],
            "default": "medium", "known": True}


@app.get("/api/network/nac/connector")
def connectors():
    return {"connectors": []}


@app.get("/css/tokens.css")
def tokens():
    return FileResponse(str(ROOT / "dashboard" / "base" / "css" / "tokens.css"), media_type="text/css")


@app.get("/js/{name}")
def shell_js(name: str):
    target = (ROOT / "dashboard" / "base" / "js" / name).resolve()
    if target.parent != (ROOT / "dashboard" / "base" / "js").resolve() or not target.is_file():
        return Response(status_code=404)
    return FileResponse(str(target), media_type="text/javascript")


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def other_api(rest: str):
    return JSONResponse({"detail": "not mocked"}, status_code=404)


@app.get("/system-apps/circles/{rest:path}")
def static(rest: str):
    target = (APP_DIR / (rest or "index.html")).resolve()
    if APP_DIR.resolve() not in target.parents:
        return Response(status_code=403)
    return FileResponse(str(target)) if target.is_file() else Response(status_code=404)


def serve() -> int:
    # Loopback on purpose: the only client is the headless Chromium this same
    # process launches on this same box.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", ws="none",
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return port


# ── scenarios ───────────────────────────────────────────────────────────────
PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True, "device_scale_factor": 2}
DESK = {"viewport": {"width": 1440, "height": 900}}
CARD = "[data-card3d]"
FRONT = "[data-face-side='front']"
BACK = "[data-face-side='back']"
# Your Host's back is one conversation: one body for
# every topic. Its data-host-thread is the topic the person's last message
# went to, absent on a blank page; the words are read off the body itself.
CONV = f"{BACK} .cbody[data-book-nudges]"


def wait_for(cond, secs=10.0, step=0.1):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001 — a page mid-render answers next time
            pass
        time.sleep(step)
    try:
        return bool(cond())
    except Exception:  # noqa: BLE001
        return False


def reply_text(page, said):
    """The words of the reply to the person's message `said`, or None: the
    message right after the last of theirs whose words are exactly `said`,
    when it is the Host's. A count of the Host's messages is no sign of a new
    one: the conversation merges again while a turn runs, and after a reload
    mid-turn the reattached turn's reply echo can leave the screen then
    (circles-web lib/history-merge.ts drops it on the text of a later saved
    reply), so one appearing and one leaving read as no change (seen under the
    full gate's load, scenario_nudge PII mode). Nor is the first Host message
    anywhere after theirs: that turn's echoes are loose at the bottom, and a
    send's saved row read before its session frame stamps its streaming echo
    sorts ahead of them (history-merge.ts `[...out, ...loose]`), so an older
    turn's reply would pass for this one's."""
    return page.evaluate(
        """([sel, said]) => {
            const ms = [...document.querySelectorAll(sel)];
            const words = (m) => (m.querySelector('.tx')?.innerText ?? '').trim();
            let at = -1;
            ms.forEach((m, i) => { if (m.dataset.msg === 'you' && words(m) === said) at = i; });
            const reply = at < 0 ? null : ms[at + 1];
            return reply && reply.dataset.msg === 'host' ? reply.innerText : null;
        }""", [f"{CONV} [data-msg]", said])


def reply_shows(page, said, words="Noted."):
    """Whether the reply to the person's message `said` shows `words` (see
    reply_text)."""
    return words in (reply_text(page, said) or "")


def streaming_reply(page, said):
    """The turn `said` started is streaming here, and words typed now go into
    it: its reply shows its first words (so its session frame, sent before
    them, has arrived), the composer holds for it, and the turn still runs."""
    return (reply_shows(page, said)
            and page.locator(f"{BACK} [data-nudge-btn]").is_visible()
            and any(t.message == said and not t.done for t in TURNS.values()))


def shot(page, name, size):
    page.screenshot(path=str(SHOTS / f"{name}-{size}.png"))


def set_aside_asks(page, wait_ms=2_500):
    """Put the ask cards up on your Host's back aside with "Not now".

    One conversation: the cards read every topic's open
    questions, so the seed's open decision on Gift for Dad (ticket 501) comes up
    over your Host's back at once, wherever the person last wrote, not only
    on the Gift for Dad tab as before. A scenario about something else sets
    it aside first, as a person would; it comes back when picked from
    Waiting on you (scenario_waiting)."""
    card = page.locator("[data-ask-ticket]")
    try:
        card.first.wait_for(state="visible", timeout=wait_ms)
    except Exception:  # noqa: BLE001 — no card came up
        return
    for _ in range(5):
        if not card.count() or not card.first.is_visible():
            return
        card.first.get_by_role("button", name="Not now").click()
        page.wait_for_timeout(300)


def open_cards(page, base, view=None, asks=False):
    reset()
    url = f"{base}/system-apps/circles/index.html" + (f"#view={view}" if view else "")
    page.goto(url, wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
    if not asks:
        set_aside_asks(page)


def hold(page, key, phone):
    """Pick a card from the deck (on a phone, from the deck's screen). On a
    desktop a card already held is left as it is: clicking it would turn it."""
    if phone and page.locator(".cards").get_attribute("data-screen") == "card":
        page.locator("[data-back-to-deck]").click()
        page.wait_for_timeout(400)
    if not phone:
        # The desktop holds a card of its own once the deck is read. Read
        # which only after it has: read in between, the click below lands on
        # a card held by then and turns it (seen once under the full gate's
        # load: your card turned to its front, and the back's tabs hid).
        expect(page.locator("[data-hold]")).not_to_have_attribute("data-hold", "", timeout=10_000)
    if phone or page.locator("[data-hold]").get_attribute("data-hold") != key:
        page.locator(f"[data-card='{key}']").click()
    expect(page.locator(f"{CARD}[data-card3d='{key}']")).to_be_visible(timeout=5_000)


def side(page):
    return page.locator("[data-hold]").get_attribute("data-side")


def tab_in_sight(page, topic):
    """Whether a topic's tab on the back in front lies wholly inside its strip
    and, unless it is the original conversation, clear of that pinned tab
    (the original must stay in sight, and the open tab
    scroll into view beside it)."""
    return page.evaluate(
        """([back, topic]) => {
          const strip = document.querySelector(back + ' [role="tablist"]');
          const tab = strip && strip.querySelector(`[data-topic="${topic}"]`);
          if (!strip || !tab) return false;
          const s = strip.getBoundingClientRect(), r = tab.getBoundingClientRect();
          if (r.left < s.left - 1 || r.right > s.right + 1) return false;
          if (topic === "original") return true;
          const pin = strip.querySelector('[data-topic="original"]').getBoundingClientRect();
          return r.left >= pin.right - 1;
        }""", [BACK, topic])


def read_again(page):
    """What the cards read on a timer, read now: the window shows again."""
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def scenario_deck(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    order = page.locator("[data-deck] [data-card]").evaluate_all("els => els.map(e => e.dataset.card)")
    check(f"D ({size}): You first, Support last", order[:1] == ["you"] and order[-1:] == ["support"], repr(order))
    check(f"D ({size}): every kind of card is dealt",
          {"person:ana@example.com", "person:dad@example.com", "person:joel@northwind.example", "host:46", "room:7",
           "group:grp-book"} <= set(order), repr(order))
    check(f"D ({size}): Juno and Kestrel are on their people's backs, not cards of their own",
          "host:42" not in order and "host:43" not in order, repr(order))
    expect(page.locator("[data-door-kind='endpoint_request']")).to_contain_text("Priya Shah", timeout=5_000)
    expect(page.locator("[data-door-kind='found_host']")).to_contain_text("Lark")
    check(f"D ({size}): waiting lists the decisions, the draft and the door",
          wait_for(lambda: {"decision:501", "decision:502", "draft:62"}
                   <= set(page.locator("[data-wait]").evaluate_all("els => els.map(e => e.dataset.wait)"))),
          repr(page.locator("[data-wait]").evaluate_all("els => els.map(e => e.dataset.wait)")))
    q = STATE.get("calendar_query") or {}
    check(f"D ({size}): Coming up reads soonest first, scheduled, from now",
          q.get("order") == "asc" and q.get("status") == "scheduled" and bool(q.get("since")), repr(q))
    if phone:
        check("D (phone): the deck is the first screen", page.locator(".cards").get_attribute("data-screen") == "deck")
        expect(page.locator("[data-strip]")).to_be_visible()
        expect(page.locator("[data-soon]")).to_contain_text("Dinner with Ana")
        check("D (phone): no desktop column", page.locator(".today").count() == 0)
        shot(page, "deck", size)
        page.locator("[data-card='person:ana@example.com']").click()
        check("D (phone): a card is the next screen",
              wait_for(lambda: page.locator(".cards").get_attribute("data-screen") == "card"))
        expect(page.locator(f"{FRONT} h2")).to_have_text("Ana Ruiz")
        expect(page.locator("[data-flip-hint]")).to_have_text("the front · tap the corner for the back")
        page.wait_for_timeout(500)
        shot(page, "person-front", size)
        page.locator("[data-back-to-deck]").click()
        check("D (phone): ‹ Cards goes back to the deck",
              wait_for(lambda: page.locator(".cards").get_attribute("data-screen") == "deck"))
        page.wait_for_timeout(400)
    else:
        check("D (desktop): your card is held, on its back",
              wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == "you" and side(page) == "back"))
        # One conversation: the back opens on a blank page with a
        # greeting, never on the page last open, and has no thread tabs (it
        # opened on the Thursday dinner tab before).
        expect(page.locator(f"{BACK} [data-greeting]")).to_be_visible(timeout=5_000)
        check("D (desktop): your Host's back has no thread tabs", page.locator(f"{BACK} [data-tab]").count() == 0)
        expect(page.locator("[data-coming-up]")).to_contain_text("Northwind pays invoice 1042")
        expect(page.locator("[data-waiting]")).to_contain_text("Gift for Dad")
        check("D (desktop): no phone strip", not page.locator("[data-strip]").is_visible())
        page.wait_for_timeout(500)
        shot(page, "deck", size)


def scenario_turn(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "person:ana@example.com", phone)
    check(f"T ({size}): a person's card opens on its front", side(page) == "front")
    page.locator(f"{FRONT} [data-corner]").click()
    check(f"T ({size}): the corner turns it to the back", wait_for(lambda: side(page) == "back"))
    expect(page.locator(f"{CARD}.flipped")).to_have_count(1)
    expect(page.locator(f"{BACK} h2")).to_have_text("Juno")
    expect(page.locator(f"{BACK} [data-topic]").first).to_have_text("The original conversation", timeout=5_000)
    spoof = page.locator(f"{BACK} [data-topic='{SPOOF_TOPIC}']")
    expect(spoof).to_be_visible(timeout=5_000)
    check(f"T ({size}): a topic Juno started under the original's title is marked Juno's",
          spoof.get_attribute("data-theirs") == "" and "Juno's" in spoof.inner_text(), spoof.inner_text())
    check(f"T ({size}): only the original conversation's own tab reads as ours",
          page.locator(f"{BACK} [data-topic='original']").get_attribute("data-theirs") is None)
    claimed = page.locator(f"{BACK} [data-claimed]")
    expect(claimed).to_have_count(1, timeout=5_000)
    check(f"T ({size}): a person claim Juno could not have checked reads as Juno's, saying whom it named",
          claimed.locator(".w").inner_text().strip().lower() == "juno"
          and "Juno says this is from mara@example.com" in claimed.inner_text(), claimed.inner_text())
    check(f"T ({size}): Juno's badge sits inside an outlined tab of its own",
          page.evaluate("""(sel) => {
            const tab = document.querySelector(sel), badge = tab && tab.querySelector('.theirs');
            if (!tab || !badge) return false;
            const cs = getComputedStyle(tab), t = tab.getBoundingClientRect(), b = badge.getBoundingClientRect();
            const outlined = cs.borderLeftStyle !== 'none' && cs.borderLeftColor !== 'rgba(0, 0, 0, 0)'
              && cs.borderLeftColor !== 'transparent' && parseFloat(cs.borderLeftWidth) > 0;
            return outlined && b.left >= t.left && b.right <= t.right;
          }""", f"{BACK} [data-topic='{SPOOF_TOPIC}']"))
    page.locator(f"{BACK} [data-topic='{LAKE_TOPIC}']").click()
    expect(page.locator(f"{BACK} [data-peer-topic='{LAKE_TOPIC}']")).to_contain_text("Oct 24–26 is open")
    page.wait_for_timeout(300)
    check(f"T ({size}): with another topic open, our original conversation is still in sight",
          tab_in_sight(page, "original"))
    check(f"T ({size}): ... and the open topic is in sight beside it", tab_in_sight(page, LAKE_TOPIC))
    page.wait_for_timeout(600)
    shot(page, "host-back", size)
    # A search hit on a topic with Juno opens Ana's back on that topic, with
    # its tab in sight beside the original.
    page.locator(f"{BACK} [data-topic='original']").click()
    expect(page.locator(f"{BACK} [data-peer-topic='original']")).to_have_count(1)
    if phone:
        page.locator("[data-back-to-deck]").click()
        page.locator("[data-phone-search]").click()
    page.locator("[data-search-input]").fill("weekend dates")
    page.locator("[data-found] [data-found-card='person:ana@example.com']").click()
    expect(page.locator(f"{BACK} [data-peer-topic='{LAKE_TOPIC}']")).to_contain_text("Oct 24–26 is open", timeout=5_000)
    page.wait_for_timeout(300)
    check(f"T ({size}): a search hit on a topic opens it, in sight, with the original in sight too",
          tab_in_sight(page, LAKE_TOPIC) and tab_in_sight(page, "original"))
    page.locator(f"{BACK} [data-corner]").click()
    check(f"T ({size}): the back's corner turns it to the front", wait_for(lambda: side(page) == "front"))
    if not phone:
        page.locator("[data-card='person:ana@example.com']").click()
        check("T (desktop): clicking the held card in the deck turns it", wait_for(lambda: side(page) == "back"))


def scenario_blank(page, base, size):
    open_cards(page, base)
    hold(page, "person:dad@example.com", size == "phone")
    page.locator(f"{FRONT} [data-corner]").click()
    expect(page.locator(f"{BACK} [data-blank-back]")).to_have_text(
        "Dad hasn't connected a Host with you, so this side is blank.", timeout=5_000)
    page.wait_for_timeout(900)
    shot(page, "blank-back", size)


def scenario_back_only(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "room:7", phone)
    check(f"O ({size}): a room opens on its back", side(page) == "back")
    expect(page.locator(f"{BACK} .kind")).to_have_text("■ Hosts only")
    expect(page.locator(f"{BACK} [data-room='7']")).to_contain_text("Mara is free Oct 24–26 too.")
    check(f"O ({size}): no corner to turn", page.locator(f"{BACK} [data-corner]").count() == 0)
    check(f"O ({size}): no hint to turn it", page.locator("[data-flip-hint]").inner_text() == "")
    page.wait_for_timeout(500)
    shot(page, "back-only", size)
    hold(page, "host:46", phone)
    check(f"O ({size}): a Host with no person opens on its back", side(page) == "back")
    expect(page.locator(f"{BACK} h2")).to_have_text("Lark")
    check(f"O ({size}): ... and has no corner", page.locator(f"{BACK} [data-corner]").count() == 0)


def scenario_landing(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    if not phone:
        hold(page, "person:joel@northwind.example", phone)
    ana = page.locator("[data-card='person:ana@example.com']")
    check(f"L ({size}): Ana's card is dark at first", ana.get_attribute("data-card-light") is None)
    STATE["inbox"].append(msg(42, ENTITY, "can we do 7:30? running late from the studio", 0,
                              frm_p="ana@example.com", to_p=ME, payload={"from_user_name": "Ana Ruiz"}))
    read_again(page)
    check(f"L ({size}): a message from Ana lights her card amber",
          wait_for(lambda: ana.get_attribute("data-card-light") == "amber"))
    check(f"L ({size}): ... and lifts it where it is",
          wait_for(lambda: ana.get_attribute("data-lift") is not None, secs=3))
    expect(ana).to_contain_text("can we do 7:30?")
    page.wait_for_timeout(300)
    shot(page, "landing", size)
    # A reply lands on Backups after its mark, while you hold another card.
    STATE["turns"][BACKUPS].append({"id": 5000, "content": "Nightly backup done: 1,284 files.", "created_at": now(),
                                    "landed_at": now(), "metadata": {"role": "assistant", "turn_token": "t-bk2",
                                                                      "model": "claude-haiku-4-5", "gen_ms": 1}})
    read_again(page)
    edge = page.locator("[data-card='you'] [data-edge] [data-light='green']")
    check(f"L ({size}): a reply landing on a page of your Host's lights your card's edge green",
          wait_for(lambda: edge.count() == 1, secs=20))


def scenario_waiting(page, base, size):
    open_cards(page, base)
    page.locator("[data-wait='decision:501']").first.click()
    check(f"W ({size}): the decision opens your card's back",
          wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == "you" and side(page) == "back"))
    # The decision names the topic it was asked on: a link to that topic,
    # which the back opens (its tab used to be selected).
    expect(page.locator(f"{BACK} [data-host-thread='{GIFT}']")).to_contain_text("Ideas for Dad", timeout=5_000)
    # The ask card is an overlay over the thread (the kit's AskOverlay).
    ask = page.get_by_role("button", name="Headlamp")
    expect(ask).to_be_visible(timeout=8_000)
    expect(page.get_by_role("button", name="Record kit")).to_be_visible()
    page.wait_for_timeout(900)
    shot(page, "waiting-ask-card", size)


def scenario_ask(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "person:ana@example.com", phone)
    yes = next(r for r in STATE["inbox"] if r["body"] == "Yes!! 7?")
    btn = page.locator(f"[data-ask-host='{yes['message_id']}']")
    if not phone:
        page.locator(f"[data-chat-msg='{yes['message_id']}']").hover()
    btn.click()
    picker = page.locator("[data-ask-picker] [data-give-to]")
    expect(picker.first).to_be_visible(timeout=5_000)
    order = picker.evaluate_all("els => els.map(e => e.dataset.giveTo)")
    check(f"A ({size}): your Host's open pages, last used first, no closed one",
          order[:1] == [BACKUPS] and OLD not in order and DINNER in order, repr(order))
    shot(page, "ask-picker", size)
    n_before = len(STATE["sent"])
    page.locator(f"[data-give-to='{DINNER}']").click()
    expect(page.locator("[data-toast]")).to_have_text("Given to Ivy, on Thursday dinner. Ana isn't told.",
                                                      timeout=5_000)
    check(f"A ({size}): the message goes to your Host, on that page, as a carry",
          wait_for(lambda: any(s.get("target_kind") == "self" and s.get("thread_id") == DINNER
                               and s.get("carry") == {"kind": "dm", "ref": "ana@example.com",
                                                      "message_id": yes["message_id"]}
                               for s in STATE["sent"][n_before:])), repr(STATE["sent"][n_before:]))
    check(f"A ({size}): nothing is sent to Ana or anyone else",
          not any(s.get("target_kind") != "self" for s in STATE["sent"][n_before:]))
    expect(page.locator(f"[data-chat-msg='{yes['message_id']}'] [data-given]")).to_have_text(
        "↳ given to Ivy · only you see this")
    hold(page, "you", phone)
    # The carried message's turn streams into the conversation, on the topic
    # it was given to (its tab used to be selected).
    expect(page.locator(f"{BACK} [data-host-thread='{DINNER}']")).to_be_visible(timeout=5_000)
    quote = page.locator(f"{BACK} [data-carried]").last
    expect(quote).to_contain_text("from Ana Ruiz's card", timeout=8_000)
    expect(quote).to_contain_text("Yes!! 7?")
    expect(page.locator(CONV)).to_contain_text(REPLY_TAIL, timeout=10_000)
    page.wait_for_timeout(500)
    shot(page, "ask-landed", size)


def scenario_reading(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "person:ana@example.com", phone)
    notice = page.locator(f"{FRONT} [data-reading-notice='on']").first
    expect(notice).to_have_text("Ana let their Host read this chat.", timeout=5_000)
    check(f"R ({size}): a notice never shows the words of the message that carries it",
          "SYSTEM NOTICE" not in page.locator(FRONT).inner_text())
    chip = page.locator(f"{FRONT} [data-reading]")
    expect(chip).to_have_text("Ivy can't read this card")
    chip.click()
    expect(page.locator(f"{FRONT} [data-reading-banner]")).to_contain_text(
        "You let Ivy read this chat. Ana sees this too.", timeout=5_000)
    check(f"R ({size}): the switch is set on", STATE["reading_puts"][-1:] == [{"email": "ana@example.com", "on": True}])
    check(f"R ({size}): Ana is told",
          (STATE.get("notices_sent") or [{}])[-1].get("text") == "I let my Host, Ivy, read this chat.")
    expect(page.locator(f"{FRONT} [data-reading-notice='on']").last).to_have_text("You let Ivy read this chat.",
                                                                                   timeout=5_000)
    expect(chip).to_have_text("Ivy can read this card")
    page.wait_for_timeout(400)
    shot(page, "reading-on", size)


def scenario_block(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "person:ana@example.com", phone)
    page.locator(f"{FRONT} [data-corner]").click()
    chip = page.locator(f"{BACK} [data-block-chip]")
    expect(chip).to_have_text("can message you ▾", timeout=5_000)
    chip.click()
    expect(chip).to_have_text("is blocked ▾", timeout=5_000)
    check(f"K ({size}): Block sets Juno's contact to blocked",
          STATE["contact_patches"][-1:] == [{"id": 21, "status": "blocked"}], repr(STATE["contact_patches"]))
    check(f"K ({size}): ... with the deny rule the Address book writes beside it",
          wait_for(lambda: any(r["peer"] == "juno.ana" and r["allow_inbox"] is False for r in STATE["rules"])),
          repr(STATE["rules"]))
    expect(page.locator("[data-toast]")).to_have_text("Juno is blocked. Ana's own messages still reach you.")
    # Blocked, Juno leaves the list's Hosts (circles_deck.pair); its back must
    # not then take Ana's name.
    page.wait_for_timeout(600)
    expect(page.locator(f"{BACK} h2")).to_have_text("Juno")
    check(f"K ({size}): blocked, the Host is never named as its person anywhere on its back",
          "Ana Ruiz" not in page.locator(f"{BACK} [data-reach]").inner_text()
          and "Ana Ruiz" not in page.locator(f"{BACK} [role='tablist']").inner_text(),
          page.locator(f"{BACK} [data-reach]").inner_text())
    check(f"K ({size}): Ana's card stays (blocking a Host is not blocking its person)",
          page.locator("[data-card='person:ana@example.com']").count() == 1)
    page.wait_for_timeout(500)
    shot(page, "block", size)
    chip.click()
    expect(chip).to_have_text("can message you ▾", timeout=5_000)
    check(f"K ({size}): Unblock sets it back and takes the rule away",
          wait_for(lambda: STATE["contact_patches"][-1:] == [{"id": 21, "status": "active"}] and not STATE["rules"]))


def scenario_unblock_through(page, base, size):
    """You blocked Kestrel and you blocked Sam,
    who uses it. Lifting your own block on Kestrel leaves it blocked through
    Sam (contacts.py block_refuses), and the chip says so."""
    open_cards(page, base)
    STATE["contacts"] = [dict(c, status="blocked") if c["id"] == 23 else c for c in STATE["contacts"]]
    STATE["contacts"].append({"id": 14, "kind": "human", "handle": "sam@northwind.example", "display_name": "Sam",
                              "status": "blocked", "peer_entity_id": 43, "host_reads": []})
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
    hold(page, "person:joel@northwind.example", size == "phone")
    page.locator(f"{FRONT} [data-corner]").click()
    chip = page.locator(f"{BACK} [data-block-chip]")
    expect(chip).to_have_text("is blocked ▾", timeout=5_000)
    chip.click()
    toast = page.locator("[data-toast]")
    toast.wait_for(timeout=5_000)
    said = toast.inner_text()
    check(f"K ({size}): Unblock says what you lifted, and that Kestrel stays blocked through Sam",
          said == "You lifted your block on Kestrel. It stays blocked because you blocked Sam.", said)
    check(f"K ({size}): ... and never says Kestrel can message you", "can message you" not in said, said)
    check(f"K ({size}): Unblock lifted only your own block on Kestrel",
          STATE["contact_patches"][-1:] == [{"id": 23, "status": "active"}], repr(STATE["contact_patches"]))
    expect(page.locator(f"{BACK} [data-block-why='person']")).to_have_count(1, timeout=5_000)
    check(f"K ({size}): ... and the chip, blocked through Sam, cannot be pressed", chip.is_disabled())


def scenario_door(page, base, size):
    open_cards(page, base)
    lark = page.locator("[data-door-kind='found_host']")
    lark.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    shot(page, "door", size)
    lark.locator("[data-door-not-now]").click()
    expect(page.locator("[data-toast]")).to_have_text("Lark won't be told either way.", timeout=5_000)
    archived = [c["path"] for c in STATE["door_calls"]]
    check(f"G ({size}): Not now archives the found Host's notices and tells nobody",
          bool(archived) and all(p.startswith("/api/inbox/archive/") for p in archived), repr(archived))
    priya = page.locator("[data-door-kind='endpoint_request']")
    priya.locator("[data-door-accept]").click()
    check(f"G ({size}): Accept answers the request with what the door named",
          wait_for(lambda: any(c["path"] == "/api/contacts/accept-request"
                               and c["body"].get("handle") == "priya@northwind.example"
                               and c["body"].get("kind") == "human" for c in STATE["door_calls"])),
          repr(STATE["door_calls"]))
    check(f"G ({size}): Priya's card opens",
          wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == "person:priya@northwind.example",
                   secs=8))
    expect(page.locator(f"{FRONT} h2")).to_have_text("Priya Shah")


def scenario_coming(page, base, size):
    open_cards(page, base)
    page.evaluate("""() => { window.__opens = [];
      window.addEventListener('message', e => {
        if (e.data && e.data.type === 'cseries:open-app') window.__opens.push(e.data); }); }""")
    page.locator("[data-soon]" if size == "phone" else "[data-open-calendar]").click()
    check(f"C ({size}): Coming up opens the Calendar app",
          wait_for(lambda: {"type": "cseries:open-app", "app_id": "calendar"} in page.evaluate("window.__opens")),
          repr(page.evaluate("window.__opens")))


def own_back(page, phone, thread=DINNER):
    """Your card's back, on the conversation. One conversation: it
    opens on a blank page with a greeting, there are no tabs to open a page
    with, and what came before is a scroll up (Earlier messages). The Host
    picks the topic of each message: the scripted pick is `thread`, so the
    page is on that topic once the first message lands (it was the Thursday
    dinner TAB once)."""
    STATE["pick"] = thread
    hold(page, "you", phone)
    if phone:
        set_aside_asks(page)
    expect(page.locator(f"{BACK} [data-greeting]")).to_be_visible(timeout=8_000)
    check("one conversation: no thread tabs on your Host's back", page.locator(f"{BACK} [data-tab]").count() == 0)
    page.locator(f"{BACK} [data-earlier]").first.click()
    expect(page.locator(CONV)).to_contain_text("Booked", timeout=8_000)


def box(page):
    return page.locator(f"{BACK} .composer textarea")


def _shell_probe():
    """The selectors the shell's new-window path waits for before it hands a
    window its seed (dashboard/base/js/app-runner.js deliverPromptWhenReady),
    read from the shell itself so this follows it."""
    src = (ROOT / "dashboard/base/js/app-runner.js").read_text()
    fn = src[src.index("function deliverPromptWhenReady"):]
    fn = fn[:fn.index("function postView")]
    return re.findall(r"doc\.querySelector\('([^']+)'\)", fn)


def _post_send(page, text):
    page.evaluate("""(t) => window.postMessage({type: 'cseries:compose', mode: 'send', text: t},
                                                window.location.origin)""", text)


def scenario_compose_held(page, base, size):
    """Y2. what another app hands your Host's chat while its composer is not
    drawn: the Orchestrator's Connect into a Circles already open on the deck
    (phone) or on Ana's card (desktop), and into a new window, which the shell
    hands its seed once the page says it can take one. Each goes, as your
    words, and your card's back opens on it (the listener once
    was the composer's, so a phone's deck or another card dropped it)."""
    phone = size == "phone"
    open_cards(page, base)
    if not phone:
        hold(page, "person:ana@example.com", phone)
    _post_send(page, "Already open: connect my GitHub")
    check(f"Y2 ({size}): already open, not on your back, an app's send goes",
          wait_for(lambda: any(s.get("body") == "Already open: connect my GitHub" for s in STATE["sent"]), secs=8.0),
          repr([s.get("body") for s in STATE["sent"]]))
    check(f"Y2 ({size}): and your card's back opens on it",
          wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == "you"
                   and side(page) == "back", secs=5.0),
          f"{page.locator('[data-hold]').get_attribute('data-hold')} {side(page)}")
    # A new window: the shell waits from the frame's load for one of its
    # selectors, then posts 300 ms later, and gives up after 20 s.
    reset()
    page.goto("about:blank")
    page.goto(f"{base}/system-apps/circles/index.html", wait_until="load", timeout=15_000)
    sels = _shell_probe()
    ready = wait_for(lambda: page.evaluate("(ss) => ss.some((s) => !!document.querySelector(s))", sels), secs=10.0)
    check(f"Y2 ({size}): a new window is ready for the shell's seed", ready, repr(sels))
    if ready:
        page.wait_for_timeout(300)
        _post_send(page, "New window: connect my GitHub")
    check(f"Y2 ({size}): and the seed goes, as your words",
          wait_for(lambda: any(s.get("body") == "New window: connect my GitHub" for s in STATE["sent"]), secs=8.0),
          repr([s.get("body") for s in STATE["sent"]]))


def _json_route(page, pattern, mutate=None, status=None, once=False):
    """Answer `pattern` from the mock, changed by `mutate`, or with `status`
    and no body. `once`: only the first request; later ones reach the mock."""
    hits = {"n": 0}

    def handle(route):
        hits["n"] += 1
        if once and hits["n"] > 1:
            route.fallback()
            return
        if status is not None:
            route.fulfill(status=status, content_type="application/json", body='{"detail": "mock outage"}')
            return
        try:
            r = route.fetch()
            body = r.json()
        except Exception as e:  # noqa: BLE001 — the page closed mid-read (the scenario's end), nothing waits for it
            if page.is_closed() or "disposed" in str(e) or "closed" in str(e):
                return
            raise
        mutate(body)
        route.fulfill(response=r, json=body)

    page.route(pattern, handle)
    return hits


def scenario_rail_audit(page, base, size):
    """Z. what the rail had and the cards lacked: a crash on a card stays on it, with Retry;
    a message that breaks the markdown renderer shows as plain text; the
    conversation loads and the thread list say a failed read, with Retry;
    the open page's ask card is read again on the poll; the deck is read
    again when the live connection comes back; All your contacts opens a
    contact's card, and shows a Host contact's card."""
    phone = size == "phone"
    sockets = []
    socket_at = []
    deck_at = []

    def connected(ws):
        sockets.append(ws)
        socket_at.append(time.time())

    page.route_web_socket(re.compile(r"/ws$"), connected)
    deck_reads = {"n": 0}
    tickets_reads = {"n": 0}

    def count(req):
        if "/api/circles/conversations" in req.url:
            deck_reads["n"] += 1
            deck_at.append(time.time())
        if "/api/tickets?" in req.url and "asked_in=" in req.url:
            tickets_reads["n"] += 1

    def pumped(cond):
        # Playwright's sync API hands the request events and the socket
        # handler to Python only inside one of its own calls: a wait on a
        # counter alone never sees them (measured: 1 -> 1 tickets read in
        # 18 s, then 1 -> 2 once the wait made a call).
        return lambda: page.evaluate("1") == 1 and cond()

    page.on("request", count)

    def fail_until_retry(pattern):
        """`pattern` answers 503 until the test lets it through. Registered
        after another route, it answers first."""
        down = {"on": True, "n": 0}

        def failing(route):
            down["n"] += 1
            if down["on"]:
                route.fulfill(status=503, content_type="application/json", body='{"detail": "mock outage"}')
            else:
                route.fallback()

        page.route(pattern, failing)
        down["handler"] = failing
        return down

    def retry_reads(name, down, retry_sel, pattern):
        """Click a Retry once its card has settled (a phone slides it in),
        letting the read through only then: on a phone a card coming into
        view can read again by itself, and that read must not be the one that
        clears the error. Then the error goes, and the click read again."""
        btn = page.locator(retry_sel)
        expect(btn).to_be_visible(timeout=8_000)
        btn.click(trial=True)
        down["on"] = False
        asked = down["n"]
        btn.click()
        expect(btn).to_have_count(0, timeout=8_000)
        check(f"Z ({size}): {name}: Retry reads it again", down["n"] > asked, f"{asked} -> {down['n']}")
        page.unroute(pattern, down["handler"])
    # A room whose read carries a row the face cannot draw.
    _json_route(page, re.compile(r"/api/circles/thread/circle/7(\?.*)?$"),
                lambda b: b.__setitem__("messages", [None]), once=True)
    # A message in Ana's chat the markdown renderer cannot take.
    def odd(b):
        if b.get("messages"):
            b["messages"][0]["body"] = {"not": "text"}
    _json_route(page, re.compile(r"/api/circles/thread/dm/ana%40example\.com(\?.*)?$"), odd)
    # Joel's chat and the thread list fail until their Retry; a Host's topic
    # and a room below.
    joel_chat = re.compile(r"/api/circles/thread/dm/joel%40northwind\.example(\?.*)?$")
    joel_down = fail_until_retry(joel_chat)
    # One conversation: the thread list is gone from your Host's
    # back, so its failed read has nothing to say and no Retry (it was failed
    # here until its Retry before). Its route stays up; the absence is checked
    # below.
    page.route(re.compile(r"/api/hosts/cards$"), lambda route: route.fulfill(json={"cards": {"43": {
        "entity_id": 43, "display_name": "Kestrel", "address": "kestrel.joel", "can_message": True,
        "relationship_label": "Joel's Host", "mine": False}}}))
    open_cards(page, base)

    # The thread list's failed read, then Retry.
    hold(page, "you", phone)
    if side(page) != "back":
        page.locator("[data-hold] [data-face-side='front'] [data-corner]").first.click()
    set_aside_asks(page)
    expect(page.locator(f"{BACK} [data-greeting]")).to_be_visible(timeout=8_000)
    check(f"Z ({size}): no thread list on your Host's back, so no thread-list error or Retry",
          page.locator(f"{BACK} [data-tab], {BACK} [data-threads-error], {BACK} [data-threads-retry]").count() == 0)
    # The open page's ask card is read again on the 15 s poll.
    before = tickets_reads["n"]
    check(f"Z ({size}): the open page's tickets are read again on the poll",
          wait_for(pumped(lambda: tickets_reads["n"] > before), secs=18.0), f"{before} -> {tickets_reads['n']}")

    # A room whose row cannot be drawn: the card says so, the deck stays.
    hold(page, "room:7", phone)
    expect(page.locator("[data-render-error]")).to_contain_text("rendering error", timeout=8_000)
    check(f"Z ({size}): a crash on a card leaves the rest of Circles drawn",
          page.locator("[data-cards]").count() == 1 and page.locator("[data-card='you']").count() == 1)
    page.locator("[data-render-retry]").first.click()
    expect(page.locator("[data-room='7']")).to_be_visible(timeout=8_000)

    # Ana's odd message is shown as its words; the rest of her chat draws.
    hold(page, "person:ana@example.com", phone)
    if side(page) != "front":
        page.locator("[data-hold] [data-face-side='back'] [data-corner]").first.click()
    expect(page.locator("[data-person-chat] [data-plain-fallback]")).to_contain_text('"not":"text"', timeout=8_000)
    check(f"Z ({size}): a message that is text still draws as markdown, through the guard",
          page.locator("[data-person-chat] [data-chat-msg]:not(:has([data-plain-fallback])) .md p").count() >= 1,
          str(page.locator("[data-person-chat] [data-chat-msg] .md").first.inner_html()[:200]))
    check(f"Z ({size}): the other messages in that chat still draw",
          page.locator("[data-person-chat] [data-chat-msg]").count() >= 2,
          str(page.locator("[data-person-chat] [data-chat-msg]").count()))

    # Joel's chat says its failed read with Retry, and Retry reads it.
    hold(page, "person:joel@northwind.example", phone)
    if side(page) != "front":
        page.locator("[data-hold] [data-face-side='back'] [data-corner]").first.click()
    retry_reads("a person's chat", joel_down, "[data-person-chat] [data-load-retry]", joel_chat)

    # Another Host's topic and a room that fail to load say so with Retry,
    # and Retry reads them (room 7's route here answers before its crash
    # route above).
    for key, pattern, body in (("host:46", re.compile(r"/api/circles/thread/entity/46(\?.*)?$"), "[data-peer-topic]"),
                               ("room:7", re.compile(r"/api/circles/thread/circle/7(\?.*)?$"), "[data-room='7']")):
        down = fail_until_retry(pattern)
        hold(page, key, phone)
        retry_reads(f"{key}'s conversation", down, f"{BACK} {body} [data-load-retry]", pattern)

    # All your contacts: a Host contact shows its card, and Message opens
    # the card it is on.
    if phone:
        page.locator("[data-back-to-deck]").click()
    page.locator("[data-contacts-open]").click()
    page.locator("[data-contact-row='23']").click()
    expect(page.locator("[data-contact-host-card='43']")).to_contain_text("Kestrel", timeout=8_000)
    page.locator("[data-contacts-sheet] [data-contact-message]").click()
    check(f"Z ({size}): Message on a Host contact opens the card that Host is on",
          wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == "person:joel@northwind.example",
                   secs=5.0), page.locator("[data-hold]").get_attribute("data-hold"))
    if phone:
        page.locator("[data-back-to-deck]").click()
    page.locator("[data-contacts-open]").click()
    page.locator("[data-contact-row='11']").click()
    page.locator("[data-contacts-sheet] [data-contact-message]").click()
    check(f"Z ({size}): Message on a person opens their card, on the front",
          wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == "person:ana@example.com"
                   and side(page) == "front", secs=5.0),
          f"{page.locator('[data-hold]').get_attribute('data-hold')} {side(page)}")

    # The live connection drops and comes back: the deck is read again.
    check(f"Z ({size}): the live connection was open", len(sockets) >= 1, str(len(sockets)))
    reads = deck_reads["n"]
    if sockets:
        sockets[-1].close()
    # The app connects again after 5 s; the read must follow that connection
    # (within 2 s), so the deck's own 30 s poll cannot pass for it.
    def reread():
        return len(sockets) >= 2 and any(socket_at[1] <= t <= socket_at[1] + 2.0 for t in deck_at)

    check(f"Z ({size}): back after a drop, the deck is read again",
          wait_for(pumped(lambda: len(sockets) >= 2 and time.time() > socket_at[1] + 2.0 or reread()), secs=11.0)
          and reread(),
          f"sockets {len(sockets)}, reads {reads} -> {deck_reads['n']}")
    # A read still in a handler when the page closes is dropped, not raised
    # out of the browser's close.
    page.unroute_all(behavior="ignoreErrors")


def scenario_nudge(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    STATE["settings"]["turn_secs"] = 8
    box(page).fill("Move it to 7:30, please.")
    page.locator(f"{BACK} [data-press]").click()
    expect(page.locator(f"{BACK} [data-host-working]")).to_be_visible(timeout=5_000)
    expect(page.locator(f"{BACK} [data-nudge-btn]")).to_be_visible()
    box(page).fill("And tell Ana.")
    box(page).press("Enter")
    line = page.locator(f"{BACK} [data-nudge-line]")
    # Waited for as long as a loaded runner needs: the line appears once the page
    # has posted the words, and in a pool of browser gates that took over 3 s.
    expect(line).to_have_count(1, timeout=15_000)
    check(f"N ({size}): the nudge goes into the turn with its id",
          wait_for(lambda: STATE["nudges"][-1]["text"] == "And tell Ana." and bool(STATE["nudges"][-1]["client_id"])),
          repr(STATE["nudges"]))
    check(f"N ({size}): its line is read once the turn reads it",
          wait_for(lambda: line.first.get_attribute("data-nudge-read") == ""))
    shot(page, "nudge-line", size)
    page.reload(wait_until="domcontentloaded")
    if phone:
        expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
        hold(page, "you", phone)
    else:
        expect(page.locator(CONV)).to_be_visible(timeout=10_000)
    lines = page.locator(f"{BACK} [data-nudge-line]")
    check(f"N ({size}): after a reload mid-turn the line is still there, read",
          wait_for(lambda: lines.count() == 1 and lines.first.get_attribute("data-nudge-read") == "", secs=10),
          f"{lines.count()} lines")
    expect(page.locator(CONV)).to_contain_text(REPLY_TAIL, timeout=15_000)
    check(f"N ({size}): ... and after the turn ends, once", wait_for(lambda: lines.count() == 1, secs=6),
          f"{lines.count()} lines")
    # A 409: the turn ends without reading; the words go as the next message.
    # Long enough that the nudge is typed while it runs, on a loaded box too.
    STATE["settings"]["turn_secs"] = 6
    STATE["nudge_409"] = True
    box(page).fill("One more thing.")
    page.locator(f"{BACK} [data-press]").click()
    expect(page.locator(f"{BACK} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    # The turn's first words have arrived, so its session frame (sent before
    # them) has too: a nudge typed earlier has no session to go into yet and is
    # held for the next message, which is right but is not the 409 this tests.
    check(f"N ({size}): the new turn is streaming before the nudge is typed",
          wait_for(lambda: streaming_reply(page, "One more thing."), secs=15))
    box(page).fill("Ask about parking.")
    box(page).press("Enter")
    check(f"N ({size}): the nudge is refused (409)", wait_for(lambda: STATE["nudges"][-1]["status"] == 409))
    STATE["nudge_409"] = False
    check(f"N ({size}): ... and the words go as the next message when the turn ends",
          wait_for(lambda: any(s.get("body") == "Ask about parking." for s in STATE["sent"]), secs=15))
    expect(page.locator(f"{BACK} [data-nudge-line]:has-text('Ask about parking.')")).to_have_count(0, timeout=10_000)
    # The server owns words it took (pending_words: a reply typed
    # while the Host worked vanished when the turn ended). A turn that ends
    # without reading them sends them on as the page's next turn; the page
    # keeps no copy and never sends them again — with a reload in between, or
    # without one.
    thread = page.locator(CONV)
    for reload_mid_turn, words in ((False, "Somewhere quiet."), (True, "And a high chair.")):
        tag = "after a reload" if reload_mid_turn else "no reload"
        expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=20_000)
        STATE["settings"]["read_nudges"] = False
        STATE["settings"]["turn_secs"] = 7
        requeued0 = len(STATE["requeued"])
        box(page).fill(f"Find a table ({tag}).")
        page.locator(f"{BACK} [data-press]").click()
        check(f"N ({size}, {tag}): the turn is streaming before the words are typed",
              wait_for(lambda: streaming_reply(page, f"Find a table ({tag})."), secs=15))
        box(page).fill(words)
        box(page).press("Enter")
        check(f"N ({size}, {tag}): the route takes the words (200, the server holds them)",
              wait_for(lambda: STATE["nudges"][-1]["text"] == words and STATE["nudges"][-1]["status"] == 200),
              repr(STATE["nudges"][-1:]))
        line = page.locator(f"{BACK} [data-nudge-line]:has-text('{words}')")
        if reload_mid_turn:
            reads0 = len(STATE["pending_reads"])
            page.reload(wait_until="domcontentloaded")
            if phone:
                expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
                hold(page, "you", phone)
            else:
                expect(page.locator(CONV)).to_be_visible(timeout=10_000)
            held_shown = wait_for(lambda: line.count() == 1 and line.first.get_attribute("data-nudge-read") is None,
                                  secs=10)
            check(f"N ({size}, {tag}): the words are on the page again after the reload, sent and not "
                  "read yet (from the server's hold)", held_shown,
                  f"{line.count()} lines; GET /api/circles/pending since the reload: "
                  f"{STATE['pending_reads'][reads0:]}")
            if not held_shown:
                # PRODUCT BUG (one conversation): the reloaded back
                # never asks the server for the words it holds (showHeld runs
                # with no page key on the blank page). Evidence kept; the
                # checks after this one do not depend on it.
                shot(page, "nudge-held-words-missing-after-reload", size)
        check(f"N ({size}, {tag}): the turn ends without reading them, and the server starts the page's "
              "next turn with them",
              wait_for(lambda: len(STATE["requeued"]) == requeued0 + 1
                       and STATE["requeued"][-1]["text"] == words, secs=20), STATE["requeued"][-2:])
        expect(thread.locator("[data-msg='you']", has_text=words)).to_have_count(1, timeout=15_000)
        check(f"N ({size}, {tag}): the page follows that turn, and its nudge line gave way to the message",
              wait_for(lambda: line.count() == 0, secs=10), f"{line.count()} lines")
        expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=20_000)
        page.wait_for_timeout(1_500)
        check(f"N ({size}, {tag}): the page never sends the words again (one turn carries them)",
              not any(s.get("body") == words for s in STATE["sent"])
              and sum(1 for q in STATE["requeued"] if q["text"] == words) == 1,
              [s.get("body") for s in STATE["sent"][-4:]])
        STATE["settings"]["read_nudges"] = True
    # PII mode: nothing is written, the route says so (held: false), and the
    # page keeps its copy: when the turn never reads the words, the copy goes
    # as the next message, once.
    expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=20_000)
    STATE["pii"] = True
    STATE["settings"]["read_nudges"] = False
    STATE["settings"]["turn_secs"] = 6
    requeued0 = len(STATE["requeued"])
    words = "Avoid the tolls."
    box(page).fill("Plan the drive.")
    page.locator(f"{BACK} [data-press]").click()
    check(f"N ({size}, PII mode): the turn is streaming before the words are typed",
          wait_for(lambda: streaming_reply(page, "Plan the drive."), secs=15),
          f"turns {[(t.message[:20], t.done) for t in TURNS.values()][-2:]}; on screen "
          f"{[(m.get_attribute('data-msg'), m.inner_text()[:40]) for m in page.locator(f'{CONV} [data-msg]').all()[-5:]]}")
    box(page).fill(words)
    box(page).press("Enter")
    check(f"N ({size}, PII mode): the route takes the words but says it did not hold them (held: false)",
          wait_for(lambda: STATE["nudges"][-1]["text"] == words and STATE["nudges"][-1]["held"] is False),
          repr(STATE["nudges"][-1:]))
    check(f"N ({size}, PII mode): the turn ended without reading them, so the page sends its copy as the "
          "next message",
          wait_for(lambda: any(s.get("body") == words for s in STATE["sent"]), secs=20)
          and len(STATE["requeued"]) == requeued0, [s.get("body") for s in STATE["sent"][-3:]])
    expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=20_000)
    page.wait_for_timeout(1_000)
    check(f"N ({size}, PII mode): ...once", sum(1 for s in STATE["sent"] if s.get("body") == words) == 1,
          [s.get("body") for s in STATE["sent"][-3:]])
    STATE["pii"] = False
    STATE["settings"]["read_nudges"] = True


def scenario_reply_wait(page, base, size):
    """The reply wait finds the person's own turn, never an older one's.
    Static pages, the shapes circles-web draws: after a
    reload mid-turn the reattached turn's echoes are loose at the bottom
    (lib/history-merge.ts `[...out, ...loose]`), and the next send's saved
    row, read before its session frame stamps the streaming echo, sorts
    ahead of them, so the first Host message after it is the older turn's."""
    def at(rows):
        msgs = "".join(
            f"<div class='hm{' you' if who == 'you' else ''}' data-msg='{who}'>"
            f"<div class='lb'><span class='w'>{'You' if who == 'you' else 'Ivy'}</span></div>"
            + (f"<div class='tx'>{text}</div>" if who == "you" else f"<div class='body'>{text}</div>")
            + "</div>" for who, text in rows)
        page.set_content(f"<div data-face-side='back'><div class='cbody' data-book-nudges>{msgs}</div></div>")

    said, done = "Plan the drive.", "Noted. I will look at it now."
    at([("you", said), ("you", "Find a table (after a reload)."), ("host", done), ("host", "")])
    check(f"W ({size}): an older turn's reply sorted after the person's message is not theirs",
          not reply_shows(page, said))
    at([("you", "Plan the drive. And the stops."), ("host", done)])
    check(f"W ({size}): a longer earlier message is not the person's (it only contains their words)",
          not reply_shows(page, said))
    at([("you", said), ("host", "Noted. "), ("you", "Find a table (after a reload)."), ("host", done)])
    check(f"W ({size}): the reply right after the person's message is theirs", reply_shows(page, said))
    at([("you", said), ("host", "Noted. 988 " + REPLY_TAIL), ("host", "Noted. " + REPLY_TAIL)])
    check(f"W ({size}): the reply's own words are read, not the last message's",
          "988" in (reply_text(page, said) or ""), repr(reply_text(page, said)))


def scenario_crisis(page, base, size):
    """U. The platform's crisis check flags the words: the
    platform's own notice comes up over the page, dismissable and not modal,
    marked as the platform's; the Host's reply still streams and stays
    readable beside it; nothing of it is a message in the thread. A reload
    before Dismiss brings it back; after Dismiss it stays down, the turn's
    frames replayed or not."""
    phone = size == "phone"
    open_cards(page, base)
    page.evaluate("() => window.localStorage.removeItem('platform.crisisNotice')")
    own_back(page, phone)
    STATE["settings"]["crisis_frame"] = True
    STATE["settings"]["turn_secs"] = 2
    thread = page.locator(CONV)
    hosts = thread.locator("[data-msg='host']")
    box(page).fill("I don't want to be here anymore.")
    page.locator(f"{BACK} [data-press]").click()
    notice = page.locator("[data-crisis-notice]")
    expect(notice).to_be_visible(timeout=6_000)
    check(f"U ({size}): the notice is the platform's, with the 988 resources",
          all(t in (notice.text_content() or "") for t in ("From the platform", "not your Host", "988",
                                                           "988lifeline.org", "free, confidential, and open 24/7")),
          (notice.text_content() or "")[:300])
    check(f"U ({size}): a dialog that is not modal, outside the thread",
          notice.get_attribute("role") == "dialog" and notice.get_attribute("aria-modal") == "false"
          and thread.locator("[data-crisis-notice]").count() == 0)
    check(f"U ({size}): the Host's reply streams and lands while it is up, in the Host's words only",
          wait_for(lambda: reply_shows(page, "I don't want to be here anymore.", REPLY_TAIL), secs=12)
          and notice.is_visible() and "988" not in reply_text(page, "I don't want to be here anymore.")
          and reply_text(page, "I don't want to be here anymore.") == hosts.last.inner_text(),
          hosts.last.inner_text()[:200])
    vh = page.viewport_size["height"]
    nb, rb = notice.bounding_box(), hosts.last.bounding_box()
    check(f"U ({size}): the reply is not under the notice, and the notice takes under half the screen",
          nb and rb and (rb["y"] >= nb["y"] + nb["height"] or rb["y"] + rb["height"] <= nb["y"])
          and nb["height"] <= vh * 0.5, f"notice={nb} reply={rb} vh={vh}")
    # The page under it still takes clicks and typing (no backdrop, no trap).
    box(page).fill("still here")
    check(f"U ({size}): the box under it still takes typing", box(page).input_value() == "still here")
    box(page).fill("")
    shot(page, "crisis-notice", size)
    STATE["settings"]["crisis_frame"] = False
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
    check(f"U ({size}): a reload before Dismiss brings it back",
          wait_for(lambda: page.locator("[data-crisis-notice]").is_visible(), secs=6))
    page.locator("[data-crisis-dismiss]").click()
    check(f"U ({size}): Dismiss closes it", wait_for(lambda: page.locator("[data-crisis-notice]").count() == 0))
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
    page.wait_for_timeout(800)
    check(f"U ({size}): after Dismiss a reload leaves it down", page.locator("[data-crisis-notice]").count() == 0)
    # Escape closes it too.
    page.evaluate("""() => window.localStorage.setItem('platform.crisisNotice',
                     JSON.stringify({pending: 'esc-turn', dismissed: []}))""")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-crisis-notice]")).to_be_visible(timeout=10_000)
    page.keyboard.press("Escape")
    check(f"U ({size}): Escape closes it", wait_for(lambda: page.locator("[data-crisis-notice]").count() == 0))


def scenario_kept_words(page, base, size):
    """K. PII mode keeps the page's copy in this tab's sessionStorage, so a
    reload mid-turn does not lose words the turn has not read; outside PII
    mode nothing is kept there."""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    kept = lambda: page.evaluate("() => JSON.parse(sessionStorage.getItem('circles.keptWords') || '[]')")  # noqa: E731
    raw = lambda: page.evaluate("() => sessionStorage.getItem('circles.keptWords')")  # noqa: E731

    def start(first, words):
        expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=20_000)
        box(page).fill(first)
        page.locator(f"{BACK} [data-press]").click()
        check(f"K ({size}): the turn is streaming before the words are typed",
              wait_for(lambda: streaming_reply(page, first), secs=15))
        box(page).fill(words)
        box(page).press("Enter")

    def reload():
        page.reload(wait_until="domcontentloaded")
        if phone:
            expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
            hold(page, "you", phone)
        else:
            expect(page.locator(CONV)).to_be_visible(timeout=10_000)

    # Outside PII mode the server holds the words: nothing is kept in the tab.
    STATE["settings"]["read_nudges"], STATE["settings"]["turn_secs"] = False, 6
    start("Book a table.", "Near the window.")
    check(f"K ({size}): outside PII mode the route holds the words (held: true)",
          wait_for(lambda: STATE["nudges"][-1]["text"] == "Near the window." and STATE["nudges"][-1]["held"] is True),
          repr(STATE["nudges"][-1:]))
    check(f"K ({size}): ...and sessionStorage is untouched", raw() is None, raw())
    expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=20_000)
    page.wait_for_timeout(1_500)
    check(f"K ({size}): ...still untouched after the turn", raw() is None, raw())

    # PII mode, the turn never reads them, a reload mid-turn: the copy goes as
    # the next message after the turn ends, once.
    STATE["pii"] = True
    STATE["settings"]["read_nudges"], STATE["settings"]["turn_secs"] = False, 8
    start("Plan the drive.", "Avoid the tolls.")
    check(f"K ({size}, PII): the route takes the words but did not hold them (held: false)",
          wait_for(lambda: STATE["nudges"][-1]["text"] == "Avoid the tolls." and STATE["nudges"][-1]["held"] is False),
          repr(STATE["nudges"][-1:]))
    check(f"K ({size}, PII): the page keeps its copy in this tab",
          wait_for(lambda: [w["text"] for w in kept()] == ["Avoid the tolls."]), kept())
    reload()
    line = page.locator(f"{BACK} [data-nudge-line]:has-text('Avoid the tolls.')")
    shown = wait_for(lambda: line.count() == 1, secs=10)
    check(f"K ({size}, PII): after the reload the words are on the page, not read yet", shown,
          f"{line.count()} lines; kept={kept()}")
    if not shown:
        # PRODUCT BUG (one conversation), not a gate premise: the
        # reloaded back opens on the blank page with no page key, so the
        # chat's showHeld/deliverKept (keyed on the page reset() names) never
        # run for the topic the words were kept for; they stay in this tab
        # and are never sent. Stop here, with the evidence: what follows
        # only repeats this failure.
        shot(page, "kept-words-lost-after-reload", size)
        check(f"K ({size}, PII): STOPPED at a product bug: words kept in the tab are not shown or sent "
              "after a reload", False, f"sent={[s.get('body') for s in STATE['sent'][-3:]]} kept={kept()}")
        STATE["pii"] = False
        STATE["settings"]["read_nudges"] = True
        return
    check(f"K ({size}, PII): the turn ended without reading them, so the reloaded page sends the copy as the "
          "next message", wait_for(lambda: any(s.get("body") == "Avoid the tolls." for s in STATE["sent"]), secs=25),
          [s.get("body") for s in STATE["sent"][-3:]])
    expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=20_000)
    page.wait_for_timeout(1_500)
    check(f"K ({size}, PII): ...exactly once, and the tab's copy is gone",
          sum(1 for s in STATE["sent"] if s.get("body") == "Avoid the tolls.") == 1 and kept() == [],
          ([s.get("body") for s in STATE["sent"][-3:]], kept()))
    # The copy goes under the id it was nudged with: the server answers a second
    # send under it (a reload before the answer, a duplicated tab) from the turn
    # that has the words.
    nudged = next((n for n in reversed(STATE["nudges"]) if n["text"] == "Avoid the tolls."), {})
    resent = next((s for s in STATE["sent"] if s.get("body") == "Avoid the tolls."), {})
    check(f"K ({size}, PII): the copy is sent under the id it was nudged with (client_id), a random tag",
          bool(nudged.get("client_id")) and resent.get("client_id") == nudged.get("client_id")
          and re.fullmatch(r"n-\d+-\d{11,}", nudged["client_id"]) is not None, (nudged, resent))

    # PII mode, a reload mid-turn, and the turn reads them after it: they
    # reach the Host once, in that turn, and the page never sends them.
    STATE["settings"]["read_nudges"], STATE["settings"]["turn_secs"] = False, 8
    start("Plan the trip.", "Take the coast road.")
    check(f"K ({size}, PII, read): kept in this tab",
          wait_for(lambda: [w["text"] for w in kept()] == ["Take the coast road."]), kept())
    reload()
    STATE["settings"]["read_nudges"] = True
    check(f"K ({size}, PII, read): the turn reads them after the reload, and the tab's copy goes",
          wait_for(lambda: kept() == [], secs=15), kept())
    expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=20_000)
    page.wait_for_timeout(2_000)
    check(f"K ({size}, PII, read): ...delivered once, by the turn: the page never sends them",
          not any(s.get("body") == "Take the coast road." for s in STATE["sent"]),
          [s.get("body") for s in STATE["sent"][-3:]])
    STATE["pii"] = False
    STATE["settings"]["read_nudges"] = True


def scenario_count(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    tally = page.locator(f"{BACK} [data-tally]")
    expect(tally).to_have_text("4.1k tokens · $0.03", timeout=5_000)
    before = int(tally.get_attribute("data-total"))
    box(page).fill("Is the table still ours?")
    page.locator(f"{BACK} [data-press]").click()
    check(f"Q ({size}): the running count ticks up when the turn's row lands",
          wait_for(lambda: int(tally.get_attribute("data-total") or 0) == before + sum(REPLY_TOKENS.values()),
                   secs=12), tally.get_attribute("data-total"))
    page.wait_for_timeout(300)
    shot(page, "count", size)


def scenario_support(page, base, size):
    open_cards(page, base)
    page.locator("[data-wait='draft:62']").first.click()
    draft = page.locator("[data-support-draft='62']")
    expect(draft).to_be_visible(timeout=5_000)
    expect(page.locator("[data-support]")).to_contain_text("Backups ran slowly on Sundays")
    expect(draft.locator("[data-share='titles']")).to_be_checked(timeout=5_000)
    draft.locator("[data-share='titles']").uncheck()
    page.wait_for_timeout(500)
    shot(page, "support-draft", size)
    draft.locator("[data-support-send]").click()
    expect(page.locator("[data-toast]")).to_have_text("Sent. Only what you ticked went.", timeout=5_000)
    calls = STATE["support_calls"]
    check(f"S ({size}): only what was ticked is shared, then Send names the draft's revision",
          calls[:1] == [{"call": "shared", "id": 62, "included": {"errors": True, "versions": True, "titles": False}}]
          and calls[1:2] == [{"call": "send", "id": 62, "revision": 2}], repr(calls))


def scenario_support_elsewhere(page, base, size):
    """S3: a Host that can't send a private ticket says so on the Support
    card before anything is sent, names where a request can go instead, and
    never offers Send or shows a raw transport error."""
    for lane in ("standalone", "board", "member_none"):
        SUPPORT_LANE["lane"] = lane
        open_cards(page, base)
        page.locator("[data-wait='draft:62']").first.click()
        draft = page.locator("[data-support-draft='62']")
        expect(draft).to_be_visible(timeout=5_000)
        where = page.locator(f"[data-support-elsewhere='{lane}']")
        expect(where).to_be_visible(timeout=5_000)
        card = page.locator("[data-support]")
        text = card.inner_text()
        if lane == "standalone":
            check(f"S3 ({size}): standalone: the card says the Host can't send a report to Privatae",
                  "This Host isn't on the Privatae network, so it can't send a report to Privatae" in text, text[:400])
            href = where.locator("[data-support-issue]").get_attribute("href") or ""
            check(f"S3 ({size}): standalone: it links a new issue on the public repository",
                  href.startswith("https://github.com/Privatae/cseries/issues/new"), href)
            check(f"S3 ({size}): standalone: it says the issue is public and the owner posts it",
                  "That issue is public" in text and "post it yourself" in text, text[:600])
        else:
            check(f"S3 ({size}): {lane}: the card says the network takes no private requests yet",
                  "The Privatae network doesn't take private support requests yet" in text, text[:400])
            check(f"S3 ({size}): {lane}: a member is never told this Host isn't on the network",
                  "isn't on the Privatae network" not in text, text[:600])
        if lane == "board":
            check(f"S3 ({size}): board: it says every member can read the board",
                  "Every member can read the board" in text, text[:600])
            check(f"S3 ({size}): board: the post goes out only on the owner's press on the card",
                  "it goes out only when you press Post it there" in text, text[:800])
        if lane == "member_none":
            href = where.locator("[data-support-issue]").get_attribute("href") or ""
            check(f"S3 ({size}): member_none: it links a new issue on the public repository",
                  href.startswith("https://github.com/Privatae/cseries/issues/new"), href)
        # The card says what the server's public text does, never more: the
        # cut takes out the shapes it finds and can miss a name or an address.
        offer_el = where.locator("[data-support-offer]")
        offer = offer_el.inner_text() if offer_el.count() else ""
        check(f"S3 ({size}): {lane}: the card shows the server's own sentence",
              offer.startswith(real_support_reach.offer_of(lane)), offer[:600])
        check(f"S3 ({size}): {lane}: the card never promises names and addresses are left out",
              "names and addresses" not in text and "can miss a name or an address written in plain words" in text
              and "read the text before it goes" in text, text[:800])
        check(f"S3 ({size}): {lane}: a security problem goes to the security address",
              (where.locator("[data-support-security]").get_attribute("href") or "") == "mailto:security@privatae.ai")
        check(f"S3 ({size}): {lane}: the draft has no Send", draft.locator("[data-support-send]").count() == 0)
        expect(draft.locator("[data-support-cannot-send]")).to_contain_text("Nothing in this draft was sent")
        check(f"S3 ({size}): {lane}: no New ticket for a ticket that can't go",
              page.locator("[data-support-new]").count() == 0)
        check(f"S3 ({size}): {lane}: no raw transport error is shown",
              not any(w in text for w in ("bridge not connected", "not offered by the network")), text[:400])
        check(f"S3 ({size}): {lane}: nothing was sent", not any(c.get("call") == "send" for c in STATE["support_calls"]),
              repr(STATE["support_calls"]))
        if lane == "standalone":
            shot(page, "support-standalone", size)
    SUPPORT_LANE["lane"] = "platform"


def scenario_reduced(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "person:ana@example.com", phone)
    card = page.locator(CARD)
    check(f"M ({size}): with less motion the card fades rather than turns", card.get_attribute("data-motion") == "fade")
    page.locator(f"{FRONT} [data-corner]").click()
    check(f"M ({size}): turned, the back is in front", wait_for(lambda: side(page) == "back"))
    check(f"M ({size}): ... and the card never rotated in space",
          card.evaluate("e => getComputedStyle(e).transform") in ("none", "matrix(1, 0, 0, 1, 0, 0)"),
          card.evaluate("e => getComputedStyle(e).transform"))


SECRET_TYPED = "hunter2hunter2"


def scenario_search(page, base, size):
    phone = size == "phone"
    open_cards(page, base)
    if phone:
        page.locator("[data-phone-search]").click()
    q = page.locator("[data-search-input]")
    n = len(STATE["urls"])
    q.click()
    for ch in SECRET_TYPED:
        page.keyboard.type(ch)
        page.wait_for_timeout(40)
    page.wait_for_timeout(400)
    leaked = [u for u in STATE["urls"][n:] if SECRET_TYPED[:6] in unquote(u)]
    # The page's own address (the hash and history included), its title and
    # its storage, not only what reached the server.
    leaked += [w for w, v in (("address", unquote(page.url)),
                              ("title", page.title()),
                              ("storage", page.evaluate(
                                  "JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")))
               if SECRET_TYPED[:6] in v]
    check(f"F ({size}): what is typed in the search box never reaches a URL", not leaked, repr(leaked))
    q.fill("dinner")
    expect(page.locator("[data-found] [data-found-card='you']")).to_contain_text("Thursday dinner", timeout=3_000)
    shot(page, "search", size)
    page.locator("[data-found] [data-found-card='you']").click()
    # One conversation: no tab to select (its tab used to be
    # selected). The topic found becomes the person's topic on your Host's back:
    # the conversation names it, and "Where this stands" is its.
    check(f"F ({size}): the topic found opens your card's back",
          wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == "you" and side(page) == "back"))
    expect(page.locator(f"{BACK} .cbody[data-host-thread='{DINNER}']")).to_be_attached(timeout=5_000)
    expect(page.locator(f"{BACK} [data-stands-topic]")).to_have_text("Thursday dinner", timeout=5_000)


def scenario_links(page, base, size):
    """V. every view Circles takes, opened with the window (#view=) and sent
    to a window already open (cseries:app-view), lands in the right place."""
    phone = size == "phone"

    def go(view, slow_inbox=0.0):
        reset()
        STATE["settings"]["inbox_list_delay"] = slow_inbox
        page.goto("about:blank")  # a new document: a hash alone would not reload it
        page.add_init_script("""(() => { window.__opens = [];
          window.addEventListener('message', e => {
            if (e.data && e.data.type === 'cseries:open-app') window.__opens.push(e.data); }); })()""")
        page.goto(f"{base}/system-apps/circles/index.html#view={view}", wait_until="domcontentloaded")
        # 15 s: the budget this file gives a page load (open_cards' page.goto).
        expect(page.locator("[data-card='you']")).to_be_visible(timeout=15_000)

    def holding(key, want_side=None):
        return wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == key
                        and (want_side is None or side(page) == want_side), secs=8)

    go("card%3Aentity%3A42")
    check(f"V ({size}): card:entity:42 opens Juno's card, on the back of Ana's", holding("person:ana@example.com", "back"))
    expect(page.locator(f"{BACK} h2")).to_have_text("Juno")
    go("card%3Aentity%3A46")
    check(f"V ({size}): card:entity:46 opens that Host's own card", holding("host:46", "back"))
    go(f"thread%3A{GIFT}")
    # One conversation: a link opens the topic it names on your
    # Host's back, its own messages in front (its tab was selected before).
    expect(page.locator(f"{BACK} .cbody[data-host-thread='{GIFT}']")).to_contain_text("Ideas for Dad", timeout=8_000)
    check(f"V ({size}): thread:<id> opens your card's back on it", holding("you", "back"))
    go(f"thread%3A{TODAY}")
    expect(page.locator(f"{BACK} .cbody[data-host-thread='{TODAY}']")).to_contain_text(
        "Morning. What's on today?", timeout=8_000)
    go("support")
    check(f"V ({size}): support opens the Support card", holding("support", "front"))
    go("support%3A62")
    expect(page.locator("[data-support-draft='62']")).to_be_in_viewport(timeout=8_000)
    # The notices above the tickets are read apart from them, and late.
    go("support%3A62", slow_inbox=1.5)
    expect(page.locator("[data-notices]")).to_be_visible(timeout=15_000)
    expect(page.locator("[data-support-draft='62']")).to_be_in_viewport(timeout=8_000)
    go("decision%3A77")
    expect(page.locator("[data-sheet='note']")).to_contain_text("fix the dock", timeout=8_000)
    go("card%3Adm%3Aana%40example.com")
    check(f"V ({size}): card:dm:<email> opens that person's front", holding("person:ana@example.com", "front"))
    go("card%3Agroup%3Agrp-book")
    check(f"V ({size}): card:group:<id> opens the group", holding("group:grp-book"))
    go("card%3Acircle%3A7")
    check(f"V ({size}): card:circle:<id> opens the room", holding("room:7", "back"))
    go("card%3Asupport")
    check(f"V ({size}): card:support opens Support", holding("support", "front"))
    go("card%3Aself")
    check(f"V ({size}): card:self opens your card", holding("you"))
    go("inbox")
    expect(page.locator("[data-door-list]")).to_be_in_viewport(timeout=8_000)
    # The door is drawn only once every read beside it has landed: a slow one
    # still ends with the door in sight (seen under the full gate's load).
    go("inbox", slow_inbox=1.5)
    expect(page.locator("[data-door-list]")).to_be_in_viewport(timeout=8_000)
    if phone:
        check("V (phone): inbox shows the deck, at the door", page.locator(".cards").get_attribute("data-screen") == "deck")
    go("tickets")
    expect(page.locator("[data-wait='decision:501']").first).to_be_visible(timeout=15_000)
    if phone:
        check("V (phone): tickets shows the deck, Waiting on you", page.locator(".cards").get_attribute("data-screen") == "deck")
    go("bastions")
    check(f"V ({size}): bastions opens the Network app",
          wait_for(lambda: any(o.get("app_id") == "network" for o in page.evaluate("window.__opens"))),
          repr(page.evaluate("window.__opens")))
    # A view sent to a window already open.
    go("tickets")
    page.evaluate("""() => window.postMessage({type: 'cseries:app-view', view: 'card:entity:46'}, '*')""")
    check(f"V ({size}): a view sent to an open window opens its card", holding("host:46", "back"))
    page.evaluate(f"""() => window.postMessage({{type: 'cseries:app-view', view: 'thread:{BACKUPS}'}}, '*')""")
    expect(page.locator(f"{BACK} .cbody[data-host-thread='{BACKUPS}']")).to_contain_text("Nightly backup", timeout=8_000)
    check(f"V ({size}): the rail is gone: no pane buttons, no cards.html",
          page.get_by_role("button", name=re.compile("^Conversations$")).count() == 0
          and not (APP_DIR / "cards.html").exists())


# ── everything the rail did, now on the cards ───────────────────────────────
def to_deck(page, phone):
    """On a phone, back to the deck's screen (Waiting on you is there)."""
    if phone and page.locator(".cards").get_attribute("data-screen") == "card":
        page.locator("[data-back-to-deck]").click()
        page.wait_for_timeout(300)


def scenario_page_actions(page, base, size):
    """P. what the rail's thread list did to a page: rename, close and reopen,
    clear, delete (naming the scheduled task it cancels), Split here at a
    topic marker; and the turn-type pick riding the next send.

    One conversation: your Host's back has no thread
    tabs, no "This thread" menu, and no Split here: the person never manages
    threads, the Host picks the topic of every message. Each of those checks
    is now a check that its control is gone (there is no equivalent to drive);
    the back shows one thread, so no line turns to another
    topic; the turn-type pick is unchanged."""
    phone = size == "phone"
    open_cards(page, base)
    STATE["tickets"][501]["status"] = "decided"  # its ask card would cover the page
    own_back(page, phone)
    conv = page.locator(CONV)
    # One thread on screen: the back shows Thursday
    # dinner, the thread the person last wrote in, and nothing of Gift for
    # Dad, so there is no line where the talk turns to it (Split here sat on
    # that line in the every-topic feed).
    expect(conv).to_contain_text("Booked: Corvo", timeout=8_000)
    check(f"P ({size}): one thread on screen: no other topic's messages and no line turning to one",
          conv.locator("[data-topic-line]", has_text="Gift for Dad").count() == 0
          and "Ideas for Dad" not in conv.inner_text()
          and conv.get_attribute("data-view-topic") == DINNER, conv.get_attribute("data-view-topic"))
    page.wait_for_timeout(300)
    shot(page, "marker", size)
    # Rename, Close and reopen, Clear, Delete and Split here were the thread
    # menu's and the marker's: none of them is on your Host's back any more.
    check(f"P ({size}): no thread menu (Rename, Close, Clear, Delete are gone with it)",
          page.locator(f"{BACK} [data-thread-menu]").count() == 0
          and page.locator("[data-thread-actions], [data-thread-rename], [data-thread-close], "
                           "[data-thread-clear], [data-thread-delete]").count() == 0)
    check(f"P ({size}): no Split here, and no Split here markers",
          page.locator(f"{BACK} [data-split], {BACK} [data-marker]").count() == 0)
    check(f"P ({size}): no thread tabs and no new-thread button",
          page.locator(f"{BACK} [data-tab], {BACK} [data-new-thread]").count() == 0)
    check(f"P ({size}): nothing asked the server to split, rename, close or delete a thread",
          STATE["thread_calls"] == [] and STATE["clears"] == [] and GIFT in STATE["threads"],
          repr((STATE["thread_calls"], STATE["clears"])))
    # The turn-type pick rides the next send, and only that one.
    tt = page.locator(f"{BACK} [data-turn-type]")
    expect(tt).to_be_visible()
    check(f"P ({size}): the turn-type picker offers letting the Host decide first",
          tt.locator("option").first.inner_text() == "Let the Host decide")
    tt.select_option("tt-research")
    box(page).fill("Look into lake houses.")
    page.locator(f"{BACK} [data-press]").click()
    check(f"P ({size}): the picked turn type goes with the send",
          wait_for(lambda: any(s.get("body") == "Look into lake houses." and s.get("turn_type_id") == "tt-research"
                               for s in STATE["sent"])), repr([s.get("turn_type_id") for s in STATE["sent"]]))
    check(f"P ({size}): ... and the pick goes back to the Host's own rule", tt.input_value() == "")


def scenario_earlier(page, base, size):
    """E. a long chat is read a page at a time: Load earlier puts the page
    before it above, each message once; a room and a Host's topic too, and
    your Host's one conversation (Earlier messages, the feed)."""
    phone = size == "phone"
    open_cards(page, base)
    joel = "joel@northwind.example"
    for i in range(230):
        STATE["inbox"].append(msg(43, ENTITY, f"old note {i:03d}", 9000 - i, frm_p=joel, to_p=ME,
                                  payload={"from_user_name": "Joel Park"}))
    for i in range(205):
        STATE["inbox"].append(msg(43, ENTITY, f"ledger line {i:03d}", 8000 - i, payload={"from_display_name": "Kestrel"}))
    hold(page, "person:joel@northwind.example", phone)
    chat = page.locator(f"{FRONT} [data-person-chat='{joel}']")
    expect(chat).to_contain_text("Monday works.", timeout=8_000)
    btn = page.locator(f"{FRONT} [data-earlier]")
    expect(btn).to_contain_text("Load earlier messages (31 more)", timeout=5_000)
    check(f"E ({size}): the oldest notes are not read yet", "old note 000" not in chat.inner_text())
    shot(page, "load-earlier", size)
    btn.click()
    expect(chat).to_contain_text("old note 000", timeout=5_000)
    check(f"E ({size}): each message once", chat.inner_text().count("old note 100") == 1)
    check(f"E ({size}): nothing more to load", page.locator(f"{FRONT} [data-earlier]").count() == 0)
    # A Host's topic, the same way.
    page.locator(f"{FRONT} [data-corner]").click()
    expect(page.locator(f"{BACK} [data-peer-topic='original']")).to_contain_text("ledger line 204", timeout=8_000)
    page.locator(f"{BACK} [data-earlier]").click()
    expect(page.locator(f"{BACK} [data-peer-topic='original']")).to_contain_text("ledger line 000", timeout=5_000)
    # A room, through the bridge's pages.
    room = STATE["rooms"][7]["messages"]
    for i in range(60):
        room.append({"id": 100 + i, "sender_entity_id": 44, "sender_name": "Wren", "content": f"room line {i:02d}",
                     "created_at": iso(at(2900 - i))})
    hold(page, "room:7", phone)
    roomb = page.locator(f"{BACK} [data-room='7']")
    expect(roomb).to_contain_text("room line 59", timeout=8_000)
    check(f"E ({size}): a room's oldest lines are not read yet", "Ana is free" not in roomb.inner_text())
    page.locator(f"{BACK} [data-earlier]").click()
    expect(roomb).to_contain_text("Ana is free Oct 24–26.", timeout=5_000)
    # Your Host's back (one conversation) opens blank; Earlier
    # messages reads the thread the person last wrote in (Thursday dinner,
    # the one "Where this stands" names), a page at a time (GET
    # /api/circles/feed?thread=, 50 rows a page), each message once, and
    # nothing of any other topic.
    STATE["turns"][DINNER] = [turn_row("user" if i % 2 == 0 else "assistant", f"feed note {i:03d}", 9000 - i)
                              for i in range(120)] + STATE["turns"][DINNER]
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
    set_aside_asks(page)
    hold(page, "you", phone)
    if phone:
        set_aside_asks(page)
    conv = page.locator(CONV)
    expect(page.locator(f"{BACK} [data-greeting]")).to_be_visible(timeout=8_000)
    check(f"E ({size}): your Host's back opens blank, nothing of the feed read into it yet",
          "feed note" not in conv.inner_text() and "Booked" not in conv.inner_text())
    for _ in range(8):
        if "feed note 000" in conv.inner_text():
            break
        more = page.locator(f"{CONV} [data-earlier]")
        if not more.count():
            break
        more.first.click()
        page.wait_for_timeout(600)
    expect(conv).to_contain_text("feed note 000", timeout=5_000)
    said = conv.inner_text()
    check(f"E ({size}): your Host's back reads the thread in view back to its start, each message once",
          all(said.count(f"feed note {i:03d}") == 1 for i in range(120)) and said.count("Booked: Corvo") == 1,
          repr([i for i in range(120) if said.count(f"feed note {i:03d}") != 1][:5]))
    check(f"E ({size}): ... and nothing of another topic", not any(w in said for w in others_words(DINNER)),
          repr([w for w in others_words(DINNER) if w in said]))
    check(f"E ({size}): ... it read only that thread's pages",
          bool(STATE["feed_reads"]) and set(STATE["feed_reads"]) <= {None, DINNER}
          and STATE["feed_reads"].count(DINNER) >= 3, repr(STATE["feed_reads"]))
    check(f"E ({size}): ... and nothing more to load", wait_for(lambda: page.locator(f"{CONV} [data-earlier]").count() == 0,
                                                              secs=4))


# ── one thread on screen ───────────────────────────────────────────────────
# Invented rows: a long thread from a week ago that a message is placed in,
# and the seed's topics, each known by words only it holds.
GARDEN = "cccccccc-0000-4000-8000-000000000001"
GARDEN_NEXT = "cccccccc-0000-4000-8000-000000000002"
CORVO_BOOKING = "cccccccc-0000-4000-8000-000000000003"
TOPIC_WORDS = {DINNER: "Booked: Corvo", BACKUPS: "Nightly backup done", GIFT: "Ideas for Dad",
               TODAY: "Morning. What"}


def others_words(tid):
    """Words only the seed's OTHER topics hold."""
    return [w for t, w in TOPIC_WORDS.items() if t != tid]


# Every frame the back draws, from when it is put in: how far the page is
# from its bottom, whether it overflows the box, and the first message in
# sight (the one the reader is reading).
FRAMES_JS = r"""(sel) => { const b = document.querySelector(sel); window.__frames = [];
  const tick = () => {
    const top = b.getBoundingClientRect().top;
    const first = [...b.querySelectorAll('[data-msg]')].find((m) => m.getBoundingClientRect().bottom > top + 1);
    window.__frames.push({fromBottom: Math.round(b.scrollHeight - b.scrollTop - b.clientHeight),
      overflow: b.scrollHeight > b.clientHeight + 1,
      first: first ? (first.innerText || '').replace(/\s+/g, ' ').slice(0, 90) : '',
      top: Math.round(b.scrollTop), h: b.scrollHeight,
      aoff: (() => { const a = b.querySelector('[data-z-anchor]'); return a ? Math.round(a.getBoundingClientRect().top - top) : null; })()});
    if (window.__frames.length < 20000) requestAnimationFrame(tick); };
  requestAnimationFrame(tick); }"""


def _view(page):
    """What your Host's back shows: each message's topic and words."""
    return page.evaluate("""(sel) => [...document.querySelectorAll(sel + ' [data-msg]')].map((m) =>
        ({topic: m.dataset.msgTopic || null, text: (m.innerText || '').trim()}))""", CONV)


def _growing_reply(n=200, before=()):
    """A reply that grows a few words at a time, as a Host's does, past the
    box on both sizes: through the band just past the box's height where
    the back used to read old history on its own."""
    words = "the drip line on the east bed runs twelve minutes at dawn and the timer".split() * 2
    steps = list(before)
    for i in range(n):
        k = (i * 3) % 15
        steps += [("text", " ".join(words[k:k + 3]) + " "), ("wait", 0.03)]
    return steps


def _turns_over(secs=40):
    return wait_for(lambda: bool(TURNS) and all(t.done for t in TURNS.values()), secs=secs)


def _check_followed(page, size, tag, said):
    """The frames since FRAMES_JS went in, read once the turn is over: the
    page stayed at its bottom, never showed older history, and shows only
    the thread the message went to."""
    f = page.evaluate("window.__frames || []")
    check(f"Z ({size}): {tag}: the reply grew past the box (the scene's premise)",
          sum(1 for x in f if x["overflow"]) > 10, f"{len(f)} frames")
    away = [x for x in f if x["fromBottom"] > 48]
    check(f"Z ({size}): {tag}: the page never moved up on its own, every frame has the newest words in sight",
          not away, f"{len(away)} of {len(f)} frames above the bottom, up to "
          f"{max([x['fromBottom'] for x in away], default=0)}px; first: {away[:1]}")
    old = [x for x in f if "irrigation note" in x["first"] or any(w in x["first"] for w in others_words(GARDEN))]
    check(f"Z ({size}): {tag}: no frame showed older history or another topic", not old,
          f"{len(old)} frames; first: {old[:1]}")
    said_now = page.locator(CONV).inner_text()
    check(f"Z ({size}): {tag}: none of the thread's earlier messages read in unasked",
          "irrigation note" not in said_now)
    check(f"Z ({size}): {tag}: nothing of another topic on screen", not any(w in said_now for w in others_words(GARDEN)),
          repr([w for w in others_words(GARDEN) if w in said_now]))
    v = _view(page)
    check(f"Z ({size}): {tag}: the back shows only the thread the message went to",
          bool(v) and all(x["topic"] == GARDEN for x in v) and sum(x["text"].endswith(said) for x in v) == 1,
          repr([(x["topic"], x["text"][:40]) for x in v][:6]))


def scenario_thread_view(page, base, size):
    """Z: the conversation never scrolls up on its own into old chat history,
    and the history shown is only the thread the message was placed in. A message the platform places in a long thread from a week
    ago: the back shows that thread, at its newest message, the reply being
    written at the bottom, and stays there while it grows past the box; the
    thread's earlier messages come only with Earlier messages, a page at a
    time, with the reader kept on the message they were reading; a reload
    mid-turn the same; and when the Host's pick files the exchange in
    another thread, the back follows it there.

    The cause this pins (main 719eba60c = v0.16.147): own-back.tsx read the
    next 50 rows of the every-topic feed on any scroll near the top, the
    reply growing past the box was one, and the place it put back ran a
    frame before the rows were drawn, so the page sat at their top."""
    phone = size == "phone"
    open_cards(page, base)
    STATE["threads"][GARDEN] = {"title": "Garden irrigation plan", "created_at": at(12000), "closed_at": None}
    STATE["turns"][GARDEN] = [turn_row("user" if i % 2 == 0 else "assistant", f"irrigation note {i:03d}", 11000 - i)
                              for i in range(140)]
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
    set_aside_asks(page)
    STATE["pick"] = GARDEN
    hold(page, "you", phone)
    if phone:
        set_aside_asks(page)
    expect(page.locator(f"{BACK} [data-greeting]")).to_be_visible(timeout=8_000)

    # Z1: a message placed in the week-old thread.
    page.evaluate(FRAMES_JS, CONV)
    STATE["settings"]["steps"] = _growing_reply()
    said = "can you check the drip timer"
    box(page).fill(said)
    page.locator(f"{BACK} [data-press]").click()
    check(f"Z ({size}): the turn runs on the week-old thread",
          wait_for(lambda: any(t.message == said and t.page == GARDEN for t in TURNS.values()), secs=5))
    _turns_over()
    wait_for(lambda: any(r["content"] == "Noted. " + REPLY_TAIL for r in STATE["turns"][GARDEN]), secs=10)
    page.wait_for_timeout(2500)  # the send's re-reads of the conversation
    _check_followed(page, size, "a message placed in a week-old thread", said)
    shot(page, "thread-view", size)

    # Z2: Earlier messages, that thread's only, the reader kept in place.
    btn = page.locator(f"{CONV} [data-earlier]")
    check(f"Z ({size}): Earlier messages is offered for the thread's older messages",
          wait_for(lambda: btn.count() == 1 and btn.is_visible(), secs=5))
    mark = """(sel) => { const b = document.querySelector(sel), top = b.getBoundingClientRect().top;
        b.querySelectorAll('[data-z-anchor]').forEach((x) => delete x.dataset.zAnchor);
        const m = [...b.querySelectorAll('[data-msg]')].find((x) => x.getBoundingClientRect().bottom > top + 1);
        m.dataset.zAnchor = '1';
        return {off: m.getBoundingClientRect().top - top, text: m.innerText.slice(0, 60), box: b.clientHeight}; }"""
    where = """(sel) => { const b = document.querySelector(sel), m = b.querySelector('[data-z-anchor]');
        return m ? m.getBoundingClientRect().top - b.getBoundingClientRect().top : null; }"""
    # The exchange is shorter than the box: the page read fills the box above
    # it, and the reader's message stays in sight.
    page.evaluate("(sel) => { document.querySelector(sel).scrollTop = 0; }", CONV)
    page.wait_for_timeout(300)
    before = page.evaluate(mark, CONV)
    btn.click()
    expect(page.locator(CONV)).to_contain_text("irrigation note 139", timeout=5_000)
    page.wait_for_timeout(600)
    after = page.evaluate(where, CONV)
    check(f"Z ({size}): Earlier leaves the message the reader was reading in sight",
          after is not None and 0 <= after <= before["box"] - 20, f"{before} -> {after}")
    v = _view(page)
    said_now = page.locator(CONV).inner_text()
    check(f"Z ({size}): Earlier puts up that thread's page before, each message once, nothing of another topic",
          all(x["topic"] == GARDEN for x in v)
          and all(said_now.count(f"irrigation note {i:03d}") == 1 for i in range(90, 140))
          and "irrigation note 089" not in said_now and not any(w in said_now for w in others_words(GARDEN)),
          repr([i for i in range(90, 140) if said_now.count(f"irrigation note {i:03d}") != 1][:5]))
    # Now the thread overflows the box: scrolled up to Earlier, the message
    # at the top stays exactly where it is, in every frame.
    more = page.locator(f"{CONV} [data-earlier]")
    check(f"Z ({size}): Earlier is offered again for the page before that",
          wait_for(lambda: more.count() == 1, secs=5))
    if more.count() == 1:
        page.evaluate("(sel) => { document.querySelector(sel).scrollTop = 0; }", CONV)
        page.wait_for_timeout(300)
        before = page.evaluate(mark, CONV)
        page.evaluate(FRAMES_JS, CONV)
        more.click()
        expect(page.locator(CONV)).to_contain_text("irrigation note 040", timeout=5_000)
        page.wait_for_timeout(600)
        after = page.evaluate(where, CONV)
        check(f"Z ({size}): Earlier keeps the reader on the message they were reading",
              after is not None and abs(after - before["off"]) <= 2, f"{before} -> {after}")
        f = page.evaluate("window.__frames || []")
        moved = [x for x in f if x["aoff"] is None or abs(x["aoff"] - before["off"]) > 2]
        check(f"Z ({size}): ... in every frame: the page read never shows at the top in its place",
              bool(f) and not moved, f"{len(moved)} of {len(f)} frames off by more than 2px; first: {moved[:1]}")
        said_now = page.locator(CONV).inner_text()
        check(f"Z ({size}): ... the page before that, each message once",
              all(said_now.count(f"irrigation note {i:03d}") == 1 for i in range(40, 140))
              and "irrigation note 039" not in said_now)

    # Z3: a reload mid-turn on that thread.
    STATE["settings"]["steps"] = _growing_reply(before=[
        ("tool", "z3", "web_search", {"query": "drip timer schedule"}), ("wait", 3.5),
        ("result", "z3", {"ok": True})])
    said2 = "and the west bed?"
    box(page).fill(said2)
    page.locator(f"{BACK} [data-press]").click()
    check(f"Z ({size}): the second turn runs on the same thread",
          wait_for(lambda: any(t.message == said2 and t.page == GARDEN for t in TURNS.values()), secs=5))
    page.wait_for_timeout(1200)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
    hold(page, "you", phone)
    expect(page.locator(CONV)).to_be_visible(timeout=8_000)
    page.evaluate(FRAMES_JS, CONV)
    _turns_over()
    page.wait_for_timeout(2500)
    _check_followed(page, size, "after a reload mid-turn", said2)

    # Z4: the Host's pick files the exchange in another thread: the back
    # follows it there, with the person's words and the reply.
    STATE["threads"][GARDEN_NEXT] = {"title": "Drip timer replacement", "created_at": at(13000), "closed_at": None}
    STATE["turns"][GARDEN_NEXT] = [turn_row("user", "replacement note from last month", 12900)]
    STATE["settings"]["steps"] = [("text", "Looking at timers. "), ("wait", 0.6),
                                  ("move", GARDEN_NEXT, "Drip timer replacement"), ("wait", 0.6),
                                  ("text", "Filed with the replacement plan. ")]
    said3 = "order a new timer"
    box(page).fill(said3)
    page.locator(f"{BACK} [data-press]").click()
    _turns_over()
    check(f"Z ({size}): the Host's pick files the exchange in another thread: the back goes there",
          wait_for(lambda: page.locator(CONV).get_attribute("data-view-topic") == GARDEN_NEXT, secs=8),
          repr(page.locator(CONV).get_attribute("data-view-topic")))
    expect(page.locator(CONV)).to_contain_text("Filed with the replacement plan.", timeout=5_000)
    page.wait_for_timeout(2500)
    v = _view(page)
    check(f"Z ({size}): ... showing that thread only, the person's words once, and the reply",
          bool(v) and all(x["topic"] == GARDEN_NEXT for x in v) and sum(x["text"].endswith(said3) for x in v) == 1
          and "replacement note from last month" not in page.locator(CONV).inner_text(),
          repr([(x["topic"], x["text"][:40]) for x in v][:6]))
    STATE["settings"]["steps"] = None


def scenario_thread_view_beside(page, base, size):
    """Z6. A message sent while a turn streams beside on another topic, after
    the followed turn has ended: the Host's pick (whileRunning) names its
    topic and it goes as an ordinary send there, with no pick of its own. The
    back shows that topic, with the person's words and the reply on screen.

    Found in review: the send named its topic, so it landed where it was
    sent, the landing was not a move, and nothing put the screen on that
    topic. The back stayed on the beside turn's topic, and the echoes, tagged
    with the topic they went to, were filtered out: the person's message and
    its reply were on no screen."""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    STATE["pick"] = DINNER
    STATE["settings"]["steps"] = _steps(3)
    box(page).fill("Is Corvo open on Thursday?")
    page.locator(f"{BACK} [data-press]").click()
    check(f"Z6 ({size}): the first turn runs on Thursday dinner",
          wait_for(lambda: any(t.page == DINNER and not t.done for t in TURNS.values()), secs=6))
    # Words for another topic while it runs: their own turn, beside it, long.
    STATE["pick"] = BACKUPS
    STATE["settings"]["steps"] = _steps(20)
    box(page).fill("Did last night's backup finish?")
    box(page).press("Enter")
    check(f"Z6 ({size}): a turn starts beside it on Backups",
          wait_for(lambda: any(t.page == BACKUPS and not t.done for t in TURNS.values()), secs=6))
    check(f"Z6 ({size}): the first turn ends while the beside one still runs (the scene's premise)",
          wait_for(lambda: all(t.done for t in TURNS.values() if t.page == DINNER)
                   and any(t.page == BACKUPS and not t.done for t in TURNS.values()), secs=15))
    page.wait_for_timeout(800)
    # The person writes again; the Host puts it on Thursday dinner.
    STATE["pick"] = DINNER
    STATE["settings"]["steps"] = None
    said = "Book the table for eight then"
    box(page).fill(said)
    box(page).press("Enter")
    check(f"Z6 ({size}): the words start a turn on Thursday dinner",
          wait_for(lambda: any(t.message == said and t.page == DINNER for t in TURNS.values()), secs=8),
          repr([(t.message, t.page) for t in TURNS.values()][-3:]))
    check(f"Z6 ({size}): the back goes to Thursday dinner with them",
          wait_for(lambda: page.locator(CONV).get_attribute("data-view-topic") == DINNER, secs=6),
          repr(page.locator(CONV).get_attribute("data-view-topic")))
    check(f"Z6 ({size}): ... and the person's words are on screen",
          wait_for(lambda: said in page.locator(CONV).inner_text(), secs=6),
          repr([(x["topic"], x["text"][:40]) for x in _view(page)][-4:]))
    _turns_over()
    STATE["settings"]["steps"] = None


# Every topic the back has on screen, once a frame, from when it is put in.
VIEW_TOPICS_JS = r"""(sel) => { window.__viewTopics = [];
  const tick = () => {
    const b = document.querySelector(sel), t = b ? b.getAttribute('data-view-topic') : null;
    if (window.__viewTopics[window.__viewTopics.length - 1] !== t) window.__viewTopics.push(t);
    if (window.__viewTopics.length < 200) requestAnimationFrame(tick); };
  requestAnimationFrame(tick); }"""


def scenario_thread_view_moved_away(page, base, size):
    """Z7. The Host's pick files the followed turn's exchange in another
    thread (thread_moved) while the person reads another topic, the one
    their last message went to (a turn they started beside): the back stays
    on that topic, with their words, and does not jump to the moved
    exchange's new thread.

    Found in review: thread_moved took the screen unconditionally for the
    followed turn, so the back left the Backups thread the person had just
    written in for a thread they were not reading."""
    phone = size == "phone"
    open_cards(page, base)
    STATE["threads"][CORVO_BOOKING] = {"title": "Corvo booking", "created_at": at(4500), "closed_at": None}
    own_back(page, phone)
    STATE["pick"] = DINNER
    STATE["settings"]["steps"] = [("text", "Checking the booking page. "), ("wait", 4.0),
                                  ("move", CORVO_BOOKING, "Corvo booking"), ("wait", 3.0),
                                  ("text", "Filed with the booking. ")]
    first = "Is Corvo open on Thursday?"
    box(page).fill(first)
    page.locator(f"{BACK} [data-press]").click()
    check(f"Z7 ({size}): the first turn runs on Thursday dinner",
          wait_for(lambda: any(t.message == first and t.page == DINNER and not t.done for t in TURNS.values()),
                   secs=6))
    expect(page.locator(CONV)).to_contain_text("Checking the booking page.", timeout=6_000)
    main = next(t for t in TURNS.values() if t.message == first)
    # Words for another topic while it runs: their own turn, beside it, and
    # the screen goes to Backups with them.
    STATE["pick"] = BACKUPS
    STATE["settings"]["steps"] = _steps(10)
    said = "Did last night's backup finish?"
    box(page).fill(said)
    box(page).press("Enter")
    check(f"Z7 ({size}): a turn starts beside it on Backups",
          wait_for(lambda: any(t.page == BACKUPS and not t.done for t in TURNS.values()), secs=6))
    check(f"Z7 ({size}): the back goes to Backups with the words",
          wait_for(lambda: page.locator(CONV).get_attribute("data-view-topic") == BACKUPS, secs=6),
          repr(page.locator(CONV).get_attribute("data-view-topic")))
    check(f"Z7 ({size}): ... before the Host moves the first exchange (the scene's premise)",
          main.page == DINNER and not main.done, repr(main.page))
    page.evaluate(VIEW_TOPICS_JS, CONV)
    # The Host's pick files the first exchange in Corvo booking.
    check(f"Z7 ({size}): the Host's pick moves the first exchange to Corvo booking",
          wait_for(lambda: main.page == CORVO_BOOKING, secs=10), repr(main.page))
    page.wait_for_timeout(2000)
    seen = page.evaluate("window.__viewTopics || []")
    check(f"Z7 ({size}): the back stays on Backups, where the person's last message went",
          page.locator(CONV).get_attribute("data-view-topic") == BACKUPS and seen == [BACKUPS], repr(seen))
    check(f"Z7 ({size}): ... with the person's words on screen",
          said in page.locator(CONV).inner_text(),
          repr([(x["topic"], x["text"][:40]) for x in _view(page)][-4:]))
    _turns_over()
    page.wait_for_timeout(1500)
    v = _view(page)
    check(f"Z7 ({size}): once both turns end, the back still shows Backups only",
          page.locator(CONV).get_attribute("data-view-topic") == BACKUPS and bool(v)
          and all(x["topic"] == BACKUPS for x in v) and sum(x["text"].endswith(said) for x in v) == 1,
          repr([(x["topic"], x["text"][:40]) for x in v][-6:]))
    STATE["settings"]["steps"] = None


def scenario_contacts(page, base, size):
    """K2. the Address book on the cards: a person's notes, Block (which takes
    their card off the deck), Unblock from All your contacts, Remove, Add by
    handle; your number; Connect by number finds first."""
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "person:joel@northwind.example", phone)
    page.locator(f"{FRONT} [data-contact-menu]").click()
    notes = page.locator("[data-contact-notes]")
    expect(notes).to_be_visible(timeout=5_000)
    notes.fill("Met at the Northwind offsite.")
    page.locator("[data-contact-notes-keep]").click()
    check(f"K2 ({size}): notes are kept on the contact",
          wait_for(lambda: {"id": 13, "notes": "Met at the Northwind offsite."} in STATE["contact_patches"]),
          repr(STATE["contact_patches"]))
    expect(page.locator("[data-block-note]")).to_contain_text("also blocks their Host, Kestrel")
    shot(page, "contact-menu", size)
    page.locator("[data-contact-block='block']").click()
    page.locator("[data-contact-block-yes]").click()
    check(f"K2 ({size}): Block sets the person blocked, with a deny rule beside it",
          wait_for(lambda: {"id": 13, "status": "blocked"} in STATE["contact_patches"]
                   and any(r["peer"] == "joel@northwind.example" and not r["allow_inbox"] for r in STATE["rules"])),
          repr(STATE["contact_patches"]))
    check(f"K2 ({size}): a blocked person leaves the deck",
          wait_for(lambda: page.locator("[data-card='person:joel@northwind.example']").count() == 0, secs=8))
    if phone and page.locator(".cards").get_attribute("data-screen") == "card":
        page.locator("[data-back-to-deck]").click()
    page.locator("[data-contacts-open]").click()
    sheet = page.locator("[data-contacts-sheet]")
    expect(sheet.locator("[data-contacts-blocked] [data-contact-row='13']")).to_be_visible(timeout=5_000)
    shot(page, "all-contacts", size)
    sheet.locator("[data-contacts-blocked] [data-contact-row='13']").click()
    sheet.locator("[data-contact-block='unblock']").click()
    check(f"K2 ({size}): Unblock from All your contacts, and the deny rule goes",
          wait_for(lambda: {"id": 13, "status": "active"} in STATE["contact_patches"]
                   and not any(r["peer"] == "joel@northwind.example" for r in STATE["rules"])),
          repr(STATE["rules"]))
    # Remove Dad.
    sheet.locator("[data-contacts-find]").fill("dad")
    sheet.locator("[data-contact-row='12']").click()
    sheet.locator("[data-contact-remove]").click()
    sheet.locator("[data-contact-remove-yes]").click()
    check(f"K2 ({size}): Remove deletes the contact",
          wait_for(lambda: {"call": "delete", "id": 12} in STATE["contact_calls"]), repr(STATE["contact_calls"]))
    # Add a Host by its handle.
    sheet.locator("[data-contacts-find]").fill("")
    sheet.locator("[data-contact-add-open]").click()
    sheet.locator("[data-contact-add-handle]").fill("wren.mara")
    sheet.locator("[data-contact-add-yes]").click()
    check(f"K2 ({size}): Add by handle creates a Host contact",
          wait_for(lambda: any(c["call"] == "create" and c["handle"] == "wren.mara" and c["kind"] == "host"
                               for c in STATE["contact_calls"])), repr(STATE["contact_calls"]))
    page.keyboard.press("Escape")
    # Connect by number: whose Host it is, before anything is sent.
    page.locator("[data-connect-open]").click()
    page.locator("[data-connect] input").fill("+14155550100")
    page.locator("[data-connect-go]").click()
    expect(page.locator("[data-connect-found]")).to_contain_text("Kestrel", timeout=5_000)
    check(f"K2 ({size}): the number was looked up, nothing sent yet",
          [c["call"] for c in STATE["alias_calls"]] == ["lookup"], repr(STATE["alias_calls"]))
    page.locator("[data-connect-go]").click()
    check(f"K2 ({size}): then asked", wait_for(lambda: STATE["alias_calls"][-1:] == [{"call": "request", "alias": "+14155550100"}]))
    # Your number, set on your card's front.
    hold(page, "you", phone)
    if side(page) == "back":
        # One conversation: the conversation's open question comes up over
        # the back on any topic; set it aside before turning the card.
        set_aside_asks(page)
        page.locator(f"{BACK} [data-corner]").click()
    page.locator(f"{FRONT} [data-number-change]").click()
    page.locator(f"{FRONT} [data-number-input]").fill("+1 415 555 0199")
    page.locator(f"{FRONT} [data-number-save]").click()
    expect(page.locator(f"{FRONT} [data-your-number]")).to_contain_text("+1 415 555 0199", timeout=5_000)


def scenario_clear_leave(page, base, size):
    """X2. Clear a person's chat and a Host's conversation; leave a room."""
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "person:ana@example.com", phone)
    page.locator(f"{FRONT} [data-contact-menu]").click()
    page.locator("[data-ask-first='clear-dm']").click()
    page.locator("[data-ask-first-yes='clear-dm']").click()
    check(f"X2 ({size}): Clear this chat archives it on this Host",
          wait_for(lambda: {"kind": "dm", "ref": "ana@example.com"} in STATE["clears"]), repr(STATE["clears"]))
    expect(page.locator(f"{FRONT} [data-person-chat]")).not_to_contain_text("Yes!! 7?", timeout=5_000)
    page.keyboard.press("Escape")
    page.locator(f"{FRONT} [data-corner]").click()
    page.locator(f"{BACK} [data-contact-menu]").click()
    page.locator("[data-ask-first='clear-host']").click()
    page.locator("[data-ask-first-yes='clear-host']").click()
    check(f"X2 ({size}): Clear this conversation archives a Host's every topic",
          wait_for(lambda: {"kind": "entity", "ref": "42"} in STATE["clears"]), repr(STATE["clears"]))
    page.keyboard.press("Escape")
    hold(page, "room:7", phone)
    page.locator(f"{BACK} [data-ask-first='leave-room']").click()
    shot(page, "leave-room", size)
    page.locator(f"{BACK} [data-ask-first-yes='leave-room']").click()
    check(f"X2 ({size}): Leave this room", wait_for(lambda: {"kind": "circle", "ref": "7"} in STATE["clears"]))
    check(f"X2 ({size}): ... and it leaves the deck",
          wait_for(lambda: page.locator("[data-card='room:7']").count() == 0, secs=8))


def scenario_decisions(page, base, size):
    """W2. the Tickets pane's queue: a decision asked nowhere a page shows
    opens in full and is answered there, with "and similar going forward";
    the decided ones; Dismiss; the ask card's own decision in full; one set
    aside with Not now comes back when picked from Waiting on you."""
    phone = size == "phone"
    open_cards(page, base)
    page.locator("[data-wait='decision:503']").first.click()
    sheet = page.locator("[data-sheet='decisions']")
    expect(sheet.locator("[data-decision='503']")).to_be_visible(timeout=5_000)
    expect(sheet).to_contain_text("Ledger asks: export the October statement now?")
    shot(page, "decision-in-full", size)
    sheet.get_by_role("checkbox").first.check()
    sheet.get_by_role("button", name=re.compile("Export it")).click()
    check(f"W2 ({size}): a room-less decision is decided in full, and similar going forward",
          wait_for(lambda: any(d["id"] == 503 and d.get("option_id") == "yes" and d.get("and_similar") is True
                               for d in STATE["decides"])), repr(STATE["decides"]))
    page.locator("[data-decisions-back]").click()
    # The decided ones.
    sheet.get_by_label("Which decisions").select_option("decided")
    expect(sheet.locator("[data-decision-row='504']")).to_be_visible(timeout=5_000)
    sheet.locator("[data-decision-row='504']").click()
    expect(sheet.locator("[data-decision='504']")).to_contain_text("Which venue for the book club?")
    page.locator("[data-sheet-close]").click()
    # The ask card's decision in full, and Dismiss there.
    to_deck(page, phone)
    page.locator("[data-wait='decision:501']").first.click()
    expect(page.get_by_role("button", name="Headlamp")).to_be_visible(timeout=8_000)
    page.locator("[data-ask-details='501']").click()
    sheet = page.locator("[data-sheet='decisions']")
    expect(sheet.locator("[data-decision='501']")).to_be_visible(timeout=5_000)
    dismiss = sheet.get_by_role("button", name="Dismiss", exact=True)
    dismiss.click()
    dismiss.click()
    check(f"W2 ({size}): Dismiss, asked first", wait_for(lambda: {"id": 501, "dismissed": True} in STATE["decides"]))
    page.locator("[data-sheet-close]").click()
    expect(page.get_by_role("button", name="Headlamp")).to_have_count(0, timeout=5_000)
    # Set aside, then picked again from Waiting on you.
    STATE["tickets"][501]["status"] = "open"
    page.evaluate("localStorage.setItem('circles.asks.setAside', JSON.stringify(['ticket:501']))")
    read_again(page)
    to_deck(page, phone)
    page.locator("[data-wait='decision:501']").first.click()
    expect(page.get_by_role("button", name="Headlamp")).to_be_visible(timeout=8_000)


def scenario_support_full(page, base, size):
    """S2. the Tickets pane's support half on the Support card: a new ticket,
    a draft discarded, a sent ticket in full with a reply, what a draft would
    share before it goes, the platform's notices, and its question waiting."""
    phone = size == "phone"
    open_cards(page, base)
    STATE["support"][63] = {"id": 63, "ticket_uuid": "s63", "category": "question", "project_name": "",
                            "title": "Can I move my Host to Europe?", "severity": "normal", "problem": "Where?",
                            "status": "sent", "platform_status": "needs_info", "platform_ref": "PT-9",
                            "resolution": "", "pr_url": "", "opened_at": iso(at(400)), "revision": 1,
                            "shared_context": [],
                            "thread": [{"author": "platform", "body": "Which country, please?", "at": iso(at(100))}]}
    STATE["support_asks"] = 1
    read_again(page)
    page.locator("[data-wait='support:asks']").first.click()
    expect(page.locator(f"{FRONT} [data-support]")).to_be_visible(timeout=5_000)
    expect(page.locator("[data-notice='n-platform-1']")).to_contain_text("Maintenance on Sunday")
    check(f"S2 ({size}): Support's light is on for the platform's question",
          page.locator("[data-card='support']").get_attribute("data-card-light") == "amber")
    # The platform's notice, in full; reading it marks it read; Archive.
    page.locator("[data-notice='n-platform-1']").click()
    note = page.locator("[data-sheet='note']")
    expect(note).to_contain_text("02:00 UTC", timeout=5_000)
    check(f"S2 ({size}): opening a notice reads it", "n-platform-1" in STATE["inbox_reads"])
    check(f"S2 ({size}): its words are text, never markdown (another machine's words can ride a body)",
          "**02:00 UTC**" in note.locator("[data-note-body]").inner_text()
          and note.locator("[data-streamdown], a").count() == 0)
    note.locator("[data-note-archive]").click()
    check(f"S2 ({size}): ... and Archive", wait_for(lambda: STATE["inbox_notes"][0]["archived_at"] is not None))
    # A draft's items, before they go; then Discard.
    draft = page.locator("[data-support-draft='62']")
    draft.locator("[data-share-content='errors'] summary").click()
    expect(draft.locator("[data-share-content='errors'] pre")).to_be_visible()
    draft.locator("[data-support-discard]").click()
    draft.locator("[data-support-discard-yes]").click()
    check(f"S2 ({size}): Discard", wait_for(lambda: {"call": "discard", "id": 62} in STATE["support_calls"]))
    # The platform's question, answered from the ticket in full.
    page.locator("[data-support-open='63']").click()
    sup = page.locator("[data-sheet='support']")
    expect(sup).to_contain_text("Which country, please?", timeout=5_000)
    shot(page, "support-in-full", size)
    sup.locator("textarea").first.fill("Germany.")
    sup.get_by_role("button", name=re.compile("^(Reply|Send reply|Answer)")).first.click()
    check(f"S2 ({size}): the reply goes to the platform's team",
          wait_for(lambda: {"call": "reply", "id": 63, "body": "Germany."} in STATE["support_calls"]),
          repr(STATE["support_calls"]))
    # Close the ticket on your side: asked first, and named apart from the
    # sheet's own Close.
    asked: list = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    sup.locator("[data-support-close]").click()
    check(f"S2 ({size}): Close ticket asks first, then closes it on your side",
          wait_for(lambda: {"call": "close", "id": 63} in STATE["support_calls"])
          and asked == ["Close this ticket on your side?"], f"{asked} {STATE['support_calls']}")
    expect(sup).to_have_count(0, timeout=5_000)
    # A new ticket.
    page.locator("[data-support-new]").click()
    new = page.locator("[data-sheet='support']")
    new.locator("input").first.fill("The dock forgets its order")
    new.locator("textarea").first.fill("After a restart the dock is back to the default order.")
    new.get_by_role("button", name=re.compile("Draft")).first.click()
    check(f"S2 ({size}): a new ticket is drafted",
          wait_for(lambda: any(c["call"] == "open" and c.get("title") == "The dock forgets its order"
                               for c in STATE["support_calls"])), repr(STATE["support_calls"]))


def scenario_older_asks(page, base, size):
    """I. the Inbox's other kinds: an older ask of your Host's in Waiting on
    you, opened in full; decision:<id> opens it; the door's In full; a group
    knock let in says what it did."""
    phone = size == "phone"
    open_cards(page, base)
    page.locator("[data-wait='note:n-decision-77']").first.click()
    note = page.locator("[data-sheet='note']")
    expect(note).to_contain_text("fix the dock", timeout=5_000)
    shot(page, "older-ask", size)
    legacy_reads = []

    def _legacy(req):
        if re.search(r"/api/circles/(thread-context/self|thread/self|threads)/(original|self)(\b|/|\?|$)", req.url):
            legacy_reads.append(req.url)
    page.on("request", _legacy)
    note.locator("[data-note-answer]").click()
    check(f"I ({size}): Tell {HOST} opens your Host's back",
          wait_for(lambda: page.locator("[data-hold]").get_attribute("data-hold") == "you" and side(page) == "back"))
    # There is no original conversation: the back opens
    # on no topic, ready for the answer; no old page is read into it.
    expect(page.locator(f"{BACK} [data-greeting]")).to_be_visible(timeout=5_000)
    page.wait_for_timeout(600)
    page.remove_listener("request", _legacy)
    check(f"I ({size}): ...and nothing asks the server for the original conversation (no context bar, "
          "no page, no markers read for it)", legacy_reads == [], repr(legacy_reads))
    # One conversation: the conversation's open question (ticket 501, asked on Gift
    # for Dad) comes up over the back on any topic; set it aside, as a person
    # would, before going on to the door.
    set_aside_asks(page)
    # The door: In full, and a group knock.
    gid = f"grp-{ENTITY}-" + "ab" * 16  # roster.valid_group_id's shape
    STATE["groups"].append({**STATE["groups"][0], "group_id": gid, "name": "Hiking crew"})
    STATE["door_rows"].append(
        {"message_id": str(uuid.uuid4()), "message_kind": "group_join", "from_address": "wren.mara",
         "from_entity_id": 44, "from_user_email": None, "subject": "Join request", "body": "",
         "to_user_email": None, "payload": {"group_id": gid, "from_display_name": "Wren"}, "at": at(5)})
    read_again(page)
    if phone and page.locator(".cards").get_attribute("data-screen") == "card":
        page.locator("[data-back-to-deck]").click()
    knock = page.locator("[data-door-kind='group_join']")
    expect(knock).to_be_visible(timeout=8_000)
    expect(knock.locator("[data-door-explain]")).to_contain_text("nothing else is shared")
    knock.locator("[data-door-accept]").click()
    expect(knock.locator("[data-door-outcome]")).to_contain_text(
        "Wren is on Hiking crew, and their Host has the invite. They are a contact now.", timeout=5_000)
    shot(page, "door-group-outcome", size)
    priya = page.locator("[data-door-kind='endpoint_request']")
    priya.locator("[data-door-open]").click()
    expect(page.locator("[data-sheet='note']")).to_contain_text("Joel's team", timeout=5_000)
    page.locator("[data-sheet-close]").click()
    # decision:<id>, the older decisions inbox's own id.
    reset()
    page.goto("about:blank")
    page.goto(f"{base}/system-apps/circles/index.html#view=decision%3A77", wait_until="domcontentloaded")
    expect(page.locator("[data-sheet='note']")).to_contain_text("fix the dock", timeout=8_000)


def scenario_composer(page, base, size):
    """Y. the rail composer's lanes, in your Host's box: a pill an app hands
    the chat rides the next send; an app's "send" goes at once; a file type
    the Host does not read is named and refused; a key is sealed into Keys
    and the message says where it went."""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    page.evaluate("""() => window.postMessage({type: 'cseries:compose', mode: 'pill', text: 'artifact #12: the hero'},
                                              window.location.origin)""")
    expect(page.locator(f"{BACK} [data-pill]")).to_contain_text("artifact #12", timeout=5_000)
    shot(page, "composer-pill", size)
    box(page).fill("Make it warmer.")
    page.locator(f"{BACK} [data-press]").click()
    check(f"Y ({size}): the pill rides the send, folded in and named on the frame",
          wait_for(lambda: any(s.get("body", "").startswith("[artifact #12: the hero]") and "Make it warmer." in s["body"]
                               and (s.get("frame") or {}).get("pills") == ["artifact #12: the hero"]
                               for s in STATE["sent"])),
          repr([(s.get("body"), s.get("frame")) for s in STATE["sent"]]))
    expect(page.locator(CONV)).to_contain_text(REPLY_TAIL, timeout=15_000)
    page.evaluate("""() => window.postMessage({type: 'cseries:compose', mode: 'send', text: 'Connect my GitHub'},
                                              window.location.origin)""")
    check(f"Y ({size}): an app's send goes at once, as your words",
          wait_for(lambda: any(s.get("body") == "Connect my GitHub" for s in STATE["sent"])))
    expect(page.locator(CONV)).to_contain_text("Connect my GitHub", timeout=8_000)
    page.wait_for_timeout(2500)
    page.locator(f"{BACK} [data-file-input]").set_input_files(
        [{"name": "setup.exe", "mimeType": "application/x-msdownload", "buffer": b"MZ"},
         {"name": "notes.md", "mimeType": "text/markdown", "buffer": b"# notes"}])
    expect(page.locator(f"{BACK} [data-composer-hint]")).to_contain_text("Not a type this chat takes: setup.exe",
                                                                          timeout=5_000)
    expect(page.locator(f"{BACK} [data-file]")).to_contain_text("notes.md")
    box(page).press("Backspace")
    check(f"Y ({size}): Backspace in an empty box takes the last file off",
          wait_for(lambda: page.locator(f"{BACK} [data-file]").count() == 0))
    key = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 4 + "XyZ-AbCdEf"
    box(page).fill(f"my key is {key}")
    page.locator(f"{BACK} [data-press]").click()
    store = page.locator(f"{BACK} [data-secret-store]")
    expect(store).to_be_visible(timeout=5_000)
    shot(page, "composer-store-in-keys", size)
    store.click()
    check(f"Y ({size}): Store in Keys seals the key, and the message names it, never the value",
          wait_for(lambda: bool(STATE["vault"]) and any("stored in Keys as" in s.get("body", "") and key not in s.get("body", "")
                                                        for s in STATE["sent"])),
          repr(STATE["vault"]))


def scenario_host_chrome(page, base, size):
    """H. what the rail's chat view had around the conversation, on your
    Host's back: the context and its budget; markdown in a person's words.
    (The Host's mood and inner state left with the simulated psyche.)"""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    # One conversation: the context and its budget are a topic's,
    # so they show once the person's message has a topic (the blank page
    # after login has none yet; a tab used to be always open).
    box(page).fill("Is the table still ours?")
    page.locator(f"{BACK} [data-press]").click()
    expect(page.locator(f"{BACK} .cbody[data-host-thread='{DINNER}']")).to_be_attached(timeout=8_000)
    expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=15_000)
    expect(page.get_by_role("button", name="View next-turn context")).to_be_visible(timeout=5_000)
    page.wait_for_timeout(400)
    shot(page, "host-chrome", size)
    STATE["inbox"].append(msg(42, ENTITY, "Bring the **blue** one.", 0, frm_p="ana@example.com", to_p=ME,
                              payload={"from_user_name": "Ana Ruiz"}))
    hold(page, "person:ana@example.com", phone)
    expect(page.locator(f"{FRONT} [data-person-chat] [data-streamdown='strong']")).to_have_text("blue", timeout=8_000)


def scenario_interests(page, base, size):
    """J. your interests on your card's front: one removed by itself; a read
    that fails says so and offers nothing to save over the list."""
    phone = size == "phone"
    open_cards(page, base)
    hold(page, "you", phone)
    if side(page) == "back":
        # One conversation: the conversation's open question comes up over
        # the back on any topic; set it aside before turning the card.
        set_aside_asks(page)
        page.locator(f"{BACK} [data-corner]").click()
    page.get_by_role("button", name="Remove sourdough").click()
    check(f"J ({size}): one interest is withdrawn by itself",
          wait_for(lambda: {"call": "withdraw", "tag": "sourdough"} in STATE["interest_calls"]),
          repr(STATE["interest_calls"]))
    page.locator(f"{FRONT} [data-interests-input]").fill("bonsai")
    page.locator(f"{FRONT} [data-interests-add]").click()
    check(f"J ({size}): adding posts the whole list",
          wait_for(lambda: any(c["call"] == "set" and "bonsai" in c["tags"] and "film-photography" in c["tags"]
                               for c in STATE["interest_calls"])), repr(STATE["interest_calls"]))
    shot(page, "interests", size)


# ── the Host at work ────────────────────────────────────────────────────────
ACTIVITY = "[data-activity]"
# A turn that writes a word, runs a tool, goes quiet, then answers: what a
# real turn's stream carries between the send and its last frame.
def _steps(gap):
    return [("tool", "call-1", "web_search", {"query": "Corvo opening hours"}), ("wait", 1.5),
            ("result", "call-1", {"status": "ok", "results": []}), ("wait", gap), ("text", "Corvo opens at 6. ")]


def _bar_state(page):
    """The working line as the person sees it: shown, inside the window, not
    in the scrolled conversation, and its words (the DOM's, not the CSS's
    capitals)."""
    return page.evaluate("""([back, sel]) => {
        const body = document.querySelector(back + ' .cbody');
        const said = body ? (body.textContent || '') : '';
        // The same thing said a second time inside the conversation: the
        // old line under it, or a status under the streaming reply.
        const twice = !!(body && body.querySelector('[data-host-working]'))
          || /(Thinking|Writing the reply|Calling [^·]*) · \\d+s/.test(said);
        const el = document.querySelector(back + ' ' + sel);
        if (!el) return {shown: false, text: '', twice};
        const r = el.getBoundingClientRect();
        const inWindow = r.height > 0 && r.top >= 0 && r.bottom <= window.innerHeight + 1;
        return {shown: inWindow && !el.closest('.cbody'), text: el.textContent || '', twice};
    }""", [BACK, ACTIVITY])


def _new_turn(page_id, before):
    """The turn a send started on `page_id`: one that was not there before."""
    fresh = [t for t in TURNS.values() if t.page == page_id and t.id not in before]
    return fresh[-1] if fresh else None


def scenario_activity(page, base, size):
    """A1. while your Host works on the page in front, one line above the box
    says so, every message (the 1st, the 2nd and the 3rd), from the send to
    the turn's last frame: through a tool call and a 10 s silence, with the
    step in plain words and the time it has taken; the step never says it is
    writing when no words are arriving. Idle, the line takes no space."""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    for n in (1, 2, 3):
        gap = 10 if n == 2 else 1.5
        STATE["settings"]["steps"] = _steps(gap)
        box(page).fill(f"Is Corvo open on Thursday? ({n})")
        before = set(TURNS)
        page.locator(f"{BACK} [data-press]").click()
        check(f"A1 ({size}): message {n}: the working line is up at once",
              wait_for(lambda: _bar_state(page)["shown"], secs=0.6, step=0.05))
        seen, twice, phases = [], [], {"tool": [], "gap": []}
        start = time.time()
        turn = None
        while time.time() - start < gap + 15:
            turn = turn or _new_turn(DINNER, before)
            # Read whether the turn had ended BEFORE looking: after its last
            # frame the line rightly goes.
            ended = turn is not None and turn.done
            if ended:
                break
            st = _bar_state(page)
            seen.append(st["shown"])
            twice.append(st["twice"])
            if turn is not None:
                f = "".join(turn.frames)
                if "9:" in f and "a:" not in f:
                    phases["tool"].append(st["text"])
                elif "a:" in f and "Corvo opens" not in f and gap > 5:
                    phases["gap"].append(st["text"])
            time.sleep(0.25)
        check(f"A1 ({size}): message {n}: the working line shows from the send to the turn's end",
              bool(seen) and all(seen), f"{seen.count(False)} of {len(seen)} reads without it")
        check(f"A1 ({size}): message {n}: said once, not again inside the conversation",
              not any(twice), f"{twice.count(True)} of {len(twice)} reads said it twice")
        check(f"A1 ({size}): message {n}: it names your Host and says it is working",
              any("Ivy is working" in t for t in phases["tool"]), repr(phases["tool"][:2]))
        check(f"A1 ({size}): message {n}: during the tool call it says what the tool does",
              any("Searching the web" in t for t in phases["tool"]), repr(phases["tool"][:3]))
        if gap > 5:
            late = phases["gap"][12:]  # 3 s into the silence
            check(f"A1 ({size}): message {n}: through a 10 s silence it stays, saying Thinking, never Writing",
                  bool(late) and all("Thinking" in t and "Writing" not in t for t in late), repr(late[:3]))
            secs = [int(m.group(1)) for t in late for m in [re.search(r"(\d+)s", t)] if m]
            check(f"A1 ({size}): message {n}: and its clock runs", bool(secs) and max(secs) >= 10, repr(secs[-3:]))
        expect(page.locator(CONV)).to_contain_text("Corvo opens at 6.", timeout=8_000)
        check(f"A1 ({size}): message {n}: once the turn is over the line takes no space",
              wait_for(lambda: page.locator(f"{BACK} {ACTIVITY}").count() == 0, secs=6))
        if n == 2:
            shot(page, "activity-idle", size)
    STATE["settings"]["steps"] = None


def scenario_activity_reload(page, base, size):
    """A2. a reload while your Host works: the line is back, with the turn's
    own clock (the platform's running turn, GET /api/circles/turns/active)."""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    STATE["settings"]["steps"] = _steps(8)
    box(page).fill("Check Corvo again.")
    page.locator(f"{BACK} [data-press]").click()
    expect(page.locator(f"{BACK} {ACTIVITY}")).to_be_visible(timeout=5_000)
    page.wait_for_timeout(3000)
    shot(page, "activity-working", size)
    page.reload(wait_until="domcontentloaded")
    if phone:
        expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
        hold(page, "you", phone)
    bar = page.locator(f"{BACK} {ACTIVITY}")
    check(f"A2 ({size}): after a reload mid-turn the working line is back",
          wait_for(lambda: _bar_state(page)["shown"] and "Ivy is working" in _bar_state(page)["text"], secs=10),
          repr(_bar_state(page)))
    secs = re.search(r"(\d+)s", bar.text_content() or "")
    check(f"A2 ({size}): ... and its clock counts from the turn's start, not the reload",
          bool(secs) and int(secs.group(1)) >= 3, bar.text_content())
    bar.click()
    panel = page.locator(f"{BACK} [data-activity-panel]")
    listed = wait_for(lambda: panel.locator("[data-activity-turn]").count() == 1
                      and "Thursday dinner" in (panel.locator("[data-activity-turn]").first.text_content() or ""),
                      secs=5)
    check(f"A2 ({size}): its panel lists the running turn, on Thursday dinner", listed,
          repr(panel.text_content() if panel.count() else None))
    if not listed:
        # PRODUCT BUG (one conversation), not a gate premise: after a
        # reload the back is a blank page with no topic (threadId null), and
        # the panel's row for the turn the line follows is drawn only for a
        # topic, so "What Ivy is doing" is empty while the line says Ivy is
        # working. Evidence kept; stop here.
        shot(page, "activity-panel-empty-after-reload", size)
        page.keyboard.press("Escape")
        wait_for(lambda: bool(TURNS) and all(t.done for t in TURNS.values()), secs=20)
        STATE["settings"]["steps"] = None
        return
    check(f"A2 ({size}): a replayed turn is not said to be writing", "Writing" not in (bar.text_content() or ""),
          bar.text_content())
    page.wait_for_timeout(300)
    shot(page, "activity-panel", size)
    page.keyboard.press("Escape")
    expect(page.locator(CONV)).to_contain_text("Corvo opens at 6.", timeout=15_000)
    check(f"A2 ({size}): ... and it goes when the turn ends",
          wait_for(lambda: page.locator(f"{BACK} {ACTIVITY}").count() == 0, secs=8))
    STATE["settings"]["steps"] = None


def scenario_activity_agents(page, base, size):
    """A3. a background agent your Host dispatched keeps the line up after
    the turn; the panel lists it beside the turn on another page."""
    phone = size == "phone"
    open_cards(page, base)
    STATE["agents"] = [{"task_id": 77, "title": "Compare three restaurants", "status": "in_progress",
                        "activity": [{"event": "start", "tool": "web_search", "status": "running"}]}]
    own_back(page, phone)
    read_again(page)
    bar = page.locator(f"{BACK} {ACTIVITY}")
    expect(bar).to_contain_text("Compare three restaurants", timeout=10_000)
    bar.click()
    expect(page.locator(f"{BACK} [data-activity-agent='77']")).to_contain_text("Compare three restaurants")
    page.keyboard.press("Escape")
    STATE["agents"][0]["status"] = "completed"
    read_again(page)
    check(f"A3 ({size}): the agent done, the line goes",
          wait_for(lambda: page.locator(f"{BACK} {ACTIVITY}").count() == 0, secs=8))


def scenario_no_gone(page, base, size):
    """A4. "Where did everything go?" is gone: from the deck, the top bar,
    the phone's bar and the app itself."""
    phone = size == "phone"
    open_cards(page, base)
    page.wait_for_timeout(600)
    check(f"A4 ({size}): no button for it", page.locator("[data-gone-btn]").count() == 0)
    check(f"A4 ({size}): its words are nowhere on the screen",
          "Where did everything go" not in (page.locator("body").text_content() or "")
          and "Where did everything go" not in page.content())
    hold(page, "you", phone)
    page.wait_for_timeout(300)
    check(f"A4 ({size}): nor with your card held", "Where did everything go" not in page.content())
    built = [f.name for f in (APP_DIR / "assets").glob("*.js")
             if "Where did everything go" in f.read_text(errors="ignore")
             or "Where the rail's panes went" in f.read_text(errors="ignore")]
    check(f"A4 ({size}): nor in the built app", not built, repr(built))


def scenario_no_ribbon(page, base, size):
    """A5. nothing above the box when nothing failed; a background failure
    shows on the working line, and its panel says what failed, in words."""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    page.wait_for_timeout(1500)
    check(f"A5 ({size}): no ribbon above the box", page.locator(f"{BACK} [data-background-ribbon]").count() == 0)
    check(f"A5 ({size}): nothing about the work that went well",
          "summarising a long conversation" not in (page.locator(BACK).text_content() or "").lower())
    check(f"A5 ({size}): idle and nothing wrong: no line at all", page.locator(f"{BACK} {ACTIVITY}").count() == 0)
    STATE["activity"].append({"id": 2, "at": iso(at(2)), "group": "Background", "detail": "Unknown",
                              "source_module": None, "model": "claude-haiku-4-5", "provider": "anthropic",
                              "tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "ok": False,
                              "error": 'anthropic 401: {"error": {"message": "invalid x-api-key"}}',
                              "error_class": None})
    read_again(page)
    bar = page.locator(f"{BACK} {ACTIVITY}")
    expect(bar).to_contain_text("1 background task failed", timeout=8_000)
    bar.click()
    fail = page.locator(f"{BACK} [data-activity-failure]")
    expect(fail).to_contain_text("invalid x-api-key", timeout=5_000)
    check(f"A5 ({size}): the failure never shows raw JSON", "{" not in (fail.first.text_content() or ""))
    shot(page, "activity-failure", size)
    check(f"A5 ({size}): a call the server could not place says what it called, not 'Background · Unknown'",
          "unknown" not in (fail.first.text_content() or "").lower()
          and "claude-haiku-4-5" in (fail.first.text_content() or ""), fail.first.text_content())
    page.locator(f"{BACK} [data-activity-dismiss]").click()
    check(f"A5 ({size}): Dismiss puts it away", wait_for(lambda: bar.count() == 0, secs=4))
    read_again(page)
    page.wait_for_timeout(1200)
    check(f"A5 ({size}): ... and it stays away while nothing new fails", bar.count() == 0)
    STATE["activity"].append({"id": 3, "at": iso(at(1)), "group": "Memory", "detail": "Deriving facts",
                              "source_module": "fact_deriver", "model": "claude-haiku-4-5", "provider": "anthropic",
                              "tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "ok": False,
                              "error": "anthropic 529: overloaded", "error_class": "lane_unavailable"})
    read_again(page)
    check(f"A5 ({size}): a NEW failure after the dismiss shows again",
          wait_for(lambda: bar.count() == 1 and "1 background task failed" in (bar.text_content() or ""), secs=8),
          repr(bar.count() and bar.text_content()))
    bar.click()
    check(f"A5 ({size}): ... the new one only", wait_for(
        lambda: page.locator(f"{BACK} [data-activity-failure]").count() == 1
        and "Deriving facts" in (page.locator(f"{BACK} [data-activity-failure]").first.text_content() or ""), secs=4))
    page.keyboard.press("Escape")


def scenario_activity_elsewhere(page, base, size):
    """A7. a turn running on another topic: a second turn started beside the
    first (one conversation) does not hide the first. The line above
    the box stays up with the first turn's clock, the second has its own
    working line in the conversation, and the panel lists both under their
    topics. (This used to leave the page mid-turn by a tab.)"""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    STATE["settings"]["steps"] = _steps(12)
    box(page).fill("Is Corvo open on Thursday? (elsewhere)")
    page.locator(f"{BACK} [data-press]").click()
    sent = time.time()
    bar = page.locator(f"{BACK} {ACTIVITY}")
    expect(bar).to_contain_text("Ivy is working", timeout=5_000)
    page.wait_for_timeout(3000)
    # One conversation: there is no other page to leave for (this
    # clicked the Backups tab mid-turn before). The way the Host now works on
    # two topics at once: words the Host puts on another topic while the turn
    # runs start their own turn beside it, live in the conversation with
    # their own working line, and the line above the box stays the first
    # turn's.
    STATE["pick"] = BACKUPS
    box(page).fill("Did last night's backup finish?")
    box(page).press("Enter")
    check(f"A7 ({size}): the words are picked onto another topic, and start their own turn there",
          wait_for(lambda: any(p["text"] == "Did last night's backup finish?" and p["thread_id"] == BACKUPS
                               for p in STATE["topic_picks"])
                   and any(t.page == BACKUPS for t in TURNS.values()), secs=6),
          repr(STATE["topic_picks"][-2:]))
    beside = page.locator(f"{CONV} [data-beside-working]")
    check(f"A7 ({size}): that turn streams beside, with its own working line naming its topic",
          wait_for(lambda: beside.count() == 1 and "Backups" in (beside.first.text_content() or ""), secs=8),
          repr(beside.count() and beside.first.text_content()))
    check(f"A7 ({size}): the line above the box still says your Host is working",
          wait_for(lambda: bar.count() == 1 and "Ivy is working" in (bar.text_content() or ""), secs=6),
          repr(bar.count() and bar.text_content()))
    check(f"A7 ({size}): ... and its clock counts from the first turn's start, not the second's",
          wait_for(lambda: bool(m := re.search(r"(\d+)s", bar.text_content() or ""))
                   and int(m.group(1)) >= int(time.time() - sent) - 2, secs=3),
          repr(bar.count() and bar.text_content()))
    shot(page, "activity-beside", size)
    bar.click()
    panel = page.locator(f"{BACK} [data-activity-panel]")
    expect(panel).to_be_visible(timeout=4_000)
    # Both turns are listed, each under its own topic: the beside turn under
    # Backups, the first under Thursday dinner.
    check(f"A7 ({size}): the panel lists the beside turn under Backups",
          wait_for(lambda: panel.locator(f"[data-activity-turn='{BACKUPS}']").count() >= 1, secs=4),
          repr(panel.text_content()))
    listed = wait_for(lambda: panel.locator(f"[data-activity-turn='{DINNER}']").count() == 1
                      and "Thursday dinner" in (panel.text_content() or ""), secs=4)
    check(f"A7 ({size}): the panel lists the first turn under its own topic, Thursday dinner", listed,
          repr(panel.text_content()))
    if not listed:
        # PRODUCT BUG (one conversation), not a gate premise: once words start a turn
        # beside, the back's topic becomes theirs (setThreadId), and the
        # panel's "this page" row names the turn the line follows (Thursday
        # dinner's) by that topic: two rows say Backups, none says Thursday
        # dinner. Evidence kept; stop here.
        shot(page, "activity-panel-mislabels-followed-turn", size)
        page.keyboard.press("Escape")
        wait_for(lambda: bool(TURNS) and all(t.done for t in TURNS.values()), secs=20)
        STATE["settings"]["steps"] = None
        return
    page.keyboard.press("Escape")
    # The saved reply row replaces the streamed words (book_mock_turns land),
    # so the turn's end is read off the mock, not the words.
    check(f"A7 ({size}): the turn ends", wait_for(lambda: bool(TURNS) and all(t.done for t in TURNS.values()), secs=20))
    check(f"A7 ({size}): ... and the line goes when it does",
          wait_for(lambda: bar.count() == 0, secs=8))
    STATE["settings"]["steps"] = None


def scenario_stands(page, base, size):
    """A6. "Where this stands" is one line until it is opened, and each page
    remembers which way it was left. One conversation: it is the topic the
    person last wrote to (Thursday dinner, the newest of their messages)."""
    phone = size == "phone"
    open_cards(page, base)
    page.evaluate("() => window.localStorage.removeItem('circles.stands.open')")
    own_back(page, phone)
    sbox = page.locator(f"{BACK} [data-stands-box]")
    toggle = page.locator(f"{BACK} [data-stands-toggle]")
    expect(page.locator(f"{BACK} [data-stands-topic]")).to_have_text("Thursday dinner", timeout=5_000)
    expect(toggle).to_have_attribute("aria-expanded", "false", timeout=5_000)
    expect(toggle).to_contain_text("Table for 2 at Corvo", timeout=5_000)

    def one_line():
        return page.evaluate("""(sel) => {
            const b = document.querySelector(sel);
            const t = b && b.querySelector('[data-stands-toggle]');
            if (!b || !t) return false;
            const lh = parseFloat(getComputedStyle(t).lineHeight) || 16;
            return t.getBoundingClientRect().height <= lh * 1.6 + 8 && b.getBoundingClientRect().height <= lh * 1.6 + 26;
        }""", f"{BACK} [data-stands-box]")
    check(f"A6 ({size}): collapsed it is one line", one_line(), repr(sbox.bounding_box()))
    shot(page, "stands-collapsed", size)
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(page.locator(f"{BACK} [data-stands]")).to_contain_text("Invite sent to Ana.")
    shot(page, "stands-open", size)
    page.reload(wait_until="domcontentloaded")
    if phone:
        expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
        hold(page, "you", phone)
    expect(page.locator(CONV)).to_be_visible(timeout=10_000)
    # One conversation: after the reload the page is blank, and the
    # box is the topic the person last wrote to, named on its line.
    expect(page.locator(f"{BACK} [data-stands-topic]")).to_have_text("Thursday dinner", timeout=5_000)
    expect(toggle).to_have_attribute("aria-expanded", "true", timeout=5_000)
    # Another topic: there are no tabs to open one with (this once
    # clicked the Backups tab); a message the Host puts on Backups makes it
    # the person's topic, and the box follows it.
    STATE["pick"] = BACKUPS
    box(page).fill("Did last night's backup finish?")
    page.locator(f"{BACK} [data-press]").click()
    expect(page.locator(f"{BACK} [data-stands-topic]")).to_have_text("Backups", timeout=8_000)
    expect(toggle).to_have_attribute("aria-expanded", "false", timeout=5_000)
    check(f"A6 ({size}): another topic keeps its own (collapsed)", one_line())
    expect(page.locator(f"{BACK} [data-host-working]")).to_have_count(0, timeout=15_000)
    STATE["pick"] = DINNER
    box(page).fill("And the dinner?")
    page.locator(f"{BACK} [data-press]").click()
    expect(page.locator(f"{BACK} [data-stands-topic]")).to_have_text("Thursday dinner", timeout=8_000)
    expect(toggle).to_have_attribute("aria-expanded", "true", timeout=5_000)
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    check(f"A6 ({size}): closed again, one line again", one_line())


def scenario_activity_buried(page, base, size):
    """A8. one background failure, then more than a hundred calls that went
    well: the line still shows it, amber, and its panel names that failure
    (the route's failed=true lists the failures by themselves; the newest
    hundred calls alone would not hold it)."""
    phone = size == "phone"
    open_cards(page, base)
    STATE["activity"] = [{"id": 2, "at": iso(at(50)), "group": "Memory", "detail": "Deriving facts",
                          "source_module": "fact_deriver", "model": "claude-haiku-4-5", "provider": "anthropic",
                          "tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "ok": False,
                          "error": 'anthropic 401: {"error": {"message": "invalid x-api-key"}}',
                          "error_class": None}]
    STATE["activity"] += [{"id": 3 + i, "at": iso(at(48 - i * 0.3)), "group": "Memory",
                           "detail": "Summarising a long conversation", "source_module": "thread_consolidator",
                           "model": "claude-haiku-4-5", "provider": "anthropic", "tokens_in": 900, "tokens_out": 120,
                           "latency_ms": 800, "ok": True, "error": None, "error_class": None} for i in range(120)]
    own_back(page, phone)
    read_again(page)
    bar = page.locator(f"{BACK} {ACTIVITY}")
    check(f"A8 ({size}): a failure 120 calls back still shows on the line",
          wait_for(lambda: bar.count() == 1 and "1 background task failed" in (bar.text_content() or ""), secs=8),
          repr(bar.count() and bar.text_content()))
    check(f"A8 ({size}): ... amber", bar.count() == 1 and "fail" in (bar.get_attribute("class") or "").split(),
          bar.count() and bar.get_attribute("class"))
    if bar.count() == 0:
        return
    bar.click()
    fail = page.locator(f"{BACK} [data-activity-failure]")
    check(f"A8 ({size}): its panel lists that failure, and only it",
          wait_for(lambda: fail.count() == 1 and fail.first.get_attribute("data-activity-failure") == "2"
                   and "invalid x-api-key" in (fail.first.text_content() or ""), secs=4),
          repr(fail.count() and fail.first.text_content()))
    check(f"A8 ({size}): in words, never raw JSON", "{" not in (fail.first.text_content() or ""))
    page.locator(f"{BACK} [data-activity-dismiss]").click()
    check(f"A8 ({size}): Dismiss puts it away", wait_for(lambda: bar.count() == 0, secs=4))
    read_again(page)
    page.wait_for_timeout(1200)
    check(f"A8 ({size}): ... and it stays away while nothing new fails", bar.count() == 0)


def scenario_activity_reload_quiet(page, base, size):
    """A9. a reload mid-turn in a silence whose last frame was words: the
    replayed words are not the Host writing. Until a frame arrives after the
    reload the line names no step (never "Writing"); when words do arrive,
    it says "Writing"."""
    phone = size == "phone"
    open_cards(page, base)
    own_back(page, phone)
    STATE["settings"]["steps"] = [("text", "Looking at Corvo now. "), ("wait", 9),
                                  ("text", "Corvo opens at 6. "), ("wait", 4)]
    box(page).fill("Is Corvo open on Friday?")
    before = set(TURNS)
    page.locator(f"{BACK} [data-press]").click()
    bar = page.locator(f"{BACK} {ACTIVITY}")
    expect(bar).to_be_visible(timeout=5_000)
    check(f"A9 ({size}): the turn started", wait_for(lambda: _new_turn(DINNER, before) is not None, secs=5))
    turn = _new_turn(DINNER, before)
    if turn is None:
        return
    page.wait_for_timeout(2000)  # into the silence after its words
    # The line read against what the PAGE has received, at every change of
    # it. The server's buffer (turn.frames) holds the words that end the
    # silence before the page reads them, and a look at the line takes time,
    # so stopping on that buffer raced: a look begun just before the server
    # sent them could read the line just after the page drew them, and count
    # what it said for them as said before them. Here the page's
    # own stream reads (circles-stream.ts reader.read) mark when it has the
    # words, and every change of the line is logged with that mark.
    page.add_init_script("""((back, sel) => {
        const log = window.__a9 = [];
        let got = false, tail = '', last = '';
        const dec = new TextDecoder();
        const read = ReadableStreamDefaultReader.prototype.read;
        ReadableStreamDefaultReader.prototype.read = function () {
            return read.call(this).then((r) => {
                if (r && r.value instanceof Uint8Array) {
                    tail = (tail + dec.decode(r.value)).slice(-400);
                    if (tail.includes('Corvo opens')) got = true;
                }
                return r;
            });
        };
        new MutationObserver(() => {
            const el = document.querySelector(back + ' ' + sel);
            const text = el ? (el.textContent || '') : '';
            const r = el ? el.getBoundingClientRect() : null;
            const shown = !!el && r.height > 0 && r.top >= 0 && r.bottom <= window.innerHeight + 1
                && !el.closest('.cbody');
            const key = JSON.stringify([got, shown, text]);
            if (key !== last) { last = key; log.push({got, shown, text}); }
        }).observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
    })(BACK_SEL, ACTIVITY_SEL);""".replace("BACK_SEL", json.dumps(BACK)).replace("ACTIVITY_SEL", json.dumps(ACTIVITY)))
    page.reload(wait_until="domcontentloaded")
    if phone:
        expect(page.locator("[data-card='you']")).to_be_visible(timeout=10_000)
        hold(page, "you", phone)
    log = []
    wait_for(lambda: bool(log.extend(page.evaluate("() => (window.__a9 || []).splice(0)")) or
                          any(e["got"] and ("Writing" in e["text"] or "Thinking" in e["text"]) for e in log)),
             secs=15)
    texts = [e["text"] for e in log if not e["got"] and e["shown"]]
    first = next((e for e in log if e["got"] and ("Writing" in e["text"] or "Thinking" in e["text"])), None)
    check(f"A9 ({size}): after the reload the working line is back", bool(texts) and "Ivy is working" in texts[-1],
          repr(texts[-1:]))
    check(f"A9 ({size}): never 'Writing' before words arrive after the reload",
          bool(texts) and not any("Writing" in t for t in texts), repr([t for t in texts if "Writing" in t][:2]))
    check(f"A9 ({size}): ... and no step named until a frame arrives",
          bool(texts) and not any("Thinking" in t for t in texts), repr([t for t in texts if "Thinking" in t][:2]))
    # The words and "Writing" are one draw: the first step the line names
    # once the page has them is "Writing", never "Thinking" (which it said
    # for up to a second, until the clock's next tick, before a fix).
    check(f"A9 ({size}): words that arrive after it say 'Writing' as they are drawn",
          first is not None and "Writing" in first["text"], repr(first))
    check(f"A9 ({size}): ... and keep saying it",
          wait_for(lambda: "Writing" in (bar.text_content() or "") if bar.count() else False, secs=2.5, step=0.05),
          bar.count() and bar.text_content())
    STATE["settings"]["steps"] = None


def scenario_activity_tool_after_words(page, base, size):
    """A10. words arrive while a tool runs, then the tool answers: the tool
    answering is the newest thing the turn did, so the draw that shows it
    answered says "Thinking", never "Writing". Before a fix
    that draw read the tools' order from before the answer (stamped after
    paint, into a ref) and said "Writing" for up to a second."""
    phone = size == "phone"
    # Every change of the line, logged as the page draws it.
    page.add_init_script("""((back, sel) => {
        const log = window.__a10 = [];
        let last = '';
        new MutationObserver(() => {
            const el = document.querySelector(back + ' ' + sel);
            const text = el ? (el.textContent || '') : '';
            if (text !== last) { last = text; log.push(text); }
        }).observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
    })(BACK_SEL, ACTIVITY_SEL);""".replace("BACK_SEL", json.dumps(BACK)).replace("ACTIVITY_SEL", json.dumps(ACTIVITY)))
    open_cards(page, base)
    own_back(page, phone)
    STATE["settings"]["steps"] = [("text", "Looking at Corvo now. "), ("wait", 1),
                                  ("tool", "call-1", "web_search", {"query": "Corvo opening hours"}), ("wait", 0.5),
                                  ("text", "Checking their page. "), ("wait", 0.6),
                                  ("result", "call-1", {"status": "ok", "results": []}), ("wait", 4)]
    box(page).fill("Is Corvo open on Friday?")
    before = set(TURNS)
    page.locator(f"{BACK} [data-press]").click()
    check(f"A10 ({size}): the turn started", wait_for(lambda: _new_turn(DINNER, before) is not None, secs=5))
    log: list = []
    tool = "Searching the web"
    wait_for(lambda: bool(log.extend(page.evaluate("() => (window.__a10 || []).splice(0)")) or
                          (any(tool in t for t in log) and "Thinking" in (log[-1] if log else ""))), secs=12)
    seen_tool = [i for i, t in enumerate(log) if tool in t]
    after = [t for t in log[seen_tool[-1] + 1:] if "Writing" in t or "Thinking" in t] if seen_tool else []
    check(f"A10 ({size}): the line named the running tool", bool(seen_tool), repr(log[-4:]))
    check(f"A10 ({size}): the draw that shows the tool answered says 'Thinking'",
          bool(after) and "Thinking" in after[0], repr(after[:2]))
    check(f"A10 ({size}): ... never 'Writing' after it", bool(after) and not any("Writing" in t for t in after),
          repr([t for t in after if "Writing" in t][:2]))
    STATE["settings"]["steps"] = None


SCENARIOS = (scenario_deck, scenario_turn, scenario_blank, scenario_back_only, scenario_landing, scenario_waiting,
             scenario_ask, scenario_reading, scenario_block, scenario_unblock_through, scenario_door, scenario_coming, scenario_nudge,
             scenario_reply_wait, scenario_crisis,
             scenario_kept_words,
             scenario_count, scenario_support, scenario_support_elsewhere, scenario_reduced, scenario_search, scenario_links,
             scenario_page_actions, scenario_earlier, scenario_thread_view, scenario_thread_view_beside,
             scenario_thread_view_moved_away, scenario_contacts, scenario_clear_leave, scenario_decisions,
             scenario_support_full, scenario_older_asks, scenario_composer, scenario_compose_held, scenario_rail_audit, scenario_host_chrome,
             scenario_interests, scenario_activity, scenario_activity_reload, scenario_activity_agents, scenario_no_gone,
             scenario_no_ribbon, scenario_stands, scenario_activity_elsewhere, scenario_activity_buried,
             scenario_activity_reload_quiet, scenario_activity_tool_after_words)


def main() -> int:
    global PAGE_KEYS
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no built Circles at {APP_DIR}/index.html — run `npm run ship` in circles-web/")
        return 1
    PAGE_KEYS = _page_keys()
    port = serve()
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for fn in SCENARIOS:
            if ONLY and fn.__name__ not in ONLY:
                continue
            for size, opts in (("phone", PHONE), ("desktop", DESK)):
                if size not in SIZES:
                    continue
                extra = {"reduced_motion": "reduce"} if fn is scenario_reduced else {}
                ctx = browser.new_context(**opts, **extra)
                page = ctx.new_page()
                errors: list = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                try:
                    fn(page, base, size)
                except Exception as e:  # a scenario that throws is a failed gate, not a crash
                    # Head and tail: Playwright puts the cause at the END of its
                    # call log ("element was detached from the DOM"), and a head
                    # cut alone made that read as a 30 s instability.
                    msg = str(e)
                    if len(msg) > 1600:
                        msg = msg[:600] + " … " + msg[-1000:]
                    # Which line of this file raised: Playwright's own message
                    # ("Locator expected to be visible") does not say which wait.
                    tb = [f for f in traceback.extract_tb(e.__traceback__)
                          if f.filename.endswith("test_circles_cards.py")]
                    if tb:
                        msg = f"line {tb[-1].lineno}: " + msg
                    check(f"{fn.__name__} ({size}): completed", False, f"{type(e).__name__}: {msg}")
                    page.screenshot(path=str(SHOTS / f"{fn.__name__}-{size}-error.png"))
                check(f"{fn.__name__} ({size}): no uncaught page errors", not errors, "; ".join(errors)[:300])
                ctx.close()
        browser.close()
    if STATE.get("page_keys"):
        check("the page route's keys are circles_marks.page_data's", STATE["page_keys"] == PAGE_KEYS,
              f"{sorted(STATE['page_keys'] ^ PAGE_KEYS)}")
    print("\nPASS circles_cards" if not fails else f"\nFAIL circles_cards: {fails} check(s)")
    if _OWN_SHOTS and not fails:
        shutil.rmtree(SHOTS, ignore_errors=True)
    else:
        print(f"screenshots: {SHOTS}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
