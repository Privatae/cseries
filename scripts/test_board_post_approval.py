#!/usr/bin/env python3
"""
Gate — a post on the members' board goes out only on the owner's click on the
card that shows its exact text (brain/mastercode/runtime/board_post_approval.py,
community_board action='post', server/routes/tickets.decide_ticket_service).

  1. community_board action='post' (a new thread and a reply) sends NOTHING to
     the network: it files one card, locked to the person (user_only,
     no_pattern), whose words hold the title and body exactly.
  2. The Host deciding that card is refused, and nothing is sent.
  3. The person's 'Post it' sends the request stored on the card, exactly:
     the same title, body, category or thread, and op_id. The Host's later
     words cannot change it.
  4. 'Don't post' sends nothing; a second click on a decided card posts
     nothing again.
  5. POST /api/tickets cannot forge such a card (its kind and payload are
     stripped).
  6. The tool, the support note and the skill say the post waits on the card.
  7. 'Post it' takes the person's authenticator code on the card, checked by
     the real verifier (server/network/nac_stepup.verify_stepup): no code, a
     wrong one, a stale one, a replayed one, or no authenticator enrolled
     sends nothing and leaves the card open; a fresh one sends exactly the
     stored text. 'Don't post' takes none.
  8. propose_capability posts nothing itself: it files the same card, and
     the proposal ladder runs only after the person's Post it sent the thread.

Pure: the pool, the bridge and the turn are fakes; invented values only.
  python3 scripts/test_board_post_approval.py
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "brain"))

_failures = 0


def check(label, cond, detail=""):
    global _failures
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


def run(coro):
    return asyncio.run(coro)


class Bridge:
    """A connected network client that records every op it is asked."""
    network_member = True

    def __init__(self):
        self._connected = True
        self.connected = True
        self.calls = []

    def offers(self, op):
        return op.startswith("community.")

    async def request_full(self, op, params=None):
        self.calls.append((op, json.loads(json.dumps(params or {}))))
        if op == "community.post":
            return {"ok": True, "data": {"id": 4101}}
        return {"ok": True, "data": {}}


BRIDGE = Bridge()
_stub_main = types.ModuleType("server.main")
_stub_main.get_bridge_client = lambda: BRIDGE
sys.modules["server.main"] = _stub_main


class Secrets:
    """kernel_secrets in memory: the statements server/network/nac_stepup
    makes for the step-up secret, its burned counter and its wrong tries."""

    def __init__(self):
        self.rows = {}

    def acquire(self):
        conn = self

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Acq()

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False
        return _Tx()

    async def fetchrow(self, sql, *p):
        if "FROM kernel_secrets WHERE id=$1" in sql:
            v = self.rows.get(p[0])
            return None if v is None else {"secret": v}
        raise AssertionError(f"unexpected secrets fetchrow: {sql[:80]}")

    async def execute(self, sql, *p):
        from server.applications.tokens import _SECRET_SCHEMA
        if "pg_advisory_xact_lock" in sql or sql == _SECRET_SCHEMA:
            return "OK"
        if sql.startswith("INSERT INTO kernel_secrets") and "DO NOTHING" in sql:
            self.rows.setdefault(p[0], bytes(p[1]))
            return "INSERT 0 1"
        if sql.startswith("INSERT INTO kernel_secrets"):
            self.rows[p[0]] = bytes(p[1])
            return "INSERT 0 1"
        if sql.startswith("DELETE FROM kernel_secrets"):
            self.rows.pop(p[0], None)
            return "DELETE 1"
        raise AssertionError(f"unexpected secrets execute: {sql[:80]}")


class TicketPool:
    """entity_tickets in memory: the INSERT create_ticket_service makes, the
    SELECT and the compare-and-set decide_ticket_service makes."""

    def __init__(self):
        self.rows = {}
        self.next_id = 300
        self.secrets = Secrets()

    def acquire(self):
        return self.secrets.acquire()

    async def fetchrow(self, sql, *p):
        if "INSERT INTO entity_tickets" in sql:
            self.next_id += 1
            eid, source, source_ref, circle_id, requester, sitrep, options, context, prio, exp, md = p
            self.rows[self.next_id] = {"id": self.next_id, "entity_id": eid, "status": "open",
                                       "circle_id": circle_id, "sitrep": sitrep,
                                       "options": options, "metadata": md}
            return {"id": self.next_id, "ticket_uuid": "00000000-0000-0000-0000-000000000001",
                    "opened_at": _dt.datetime(2026, 1, 2, 3, 4, 5)}
        if "FROM entity_tickets WHERE id = $1 AND entity_id = $2" in sql:
            r = self.rows.get(p[0])
            return dict(r) if r and r["entity_id"] == p[1] else None
        raise AssertionError(f"unexpected fetchrow: {sql[:80]}")

    async def fetchval(self, sql, *p):
        return None

    async def execute(self, sql, *p):
        if "SET status = 'decided'" in sql:
            r = self.rows.get(p[2])
            if r and r["status"] == "open":
                r["status"] = "decided"
                return "UPDATE 1"
            return "UPDATE 0"
        if "board_post_apply" in sql:
            r = self.rows.get(p[1])
            if r:
                md = json.loads(r["metadata"])
                md["board_post_apply"] = json.loads(p[0])
                r["metadata"] = json.dumps(md)
            return "UPDATE 1"
        return "UPDATE 0"


POOL = TicketPool()
EID = 7

from server import brain_service  # noqa: E402
from server import host_events  # noqa: E402
from server.routes import tickets as TK  # noqa: E402

brain_service.get_pool = lambda: POOL
brain_service._entity_id = EID


async def _no_emit(*a, **k):
    return 0


host_events.emit = _no_emit

from mastercode.runtime import board_post_approval as BPA  # noqa: E402
from mastercode.runtime import tools_community as TC  # noqa: E402

TITLE = "Calendar sync stops after an update"
BODY = ("### What happens\n\nSync stops after the update.\n\n"
        "### Steps to reproduce\n\n1. Press Sync\n2. Wait a minute")


def decide(tid, option, actor="user", note="", code=None):
    """decide_ticket_service's answer, or {'error': status} when it refuses."""
    try:
        return run(TK.decide_ticket_service(POOL, EID, tid, option_id=option, actor=actor,
                                            note=note, stepup_code=code))
    except TK.TicketError as e:
        return {"error": e.status, "detail": e.detail}


from server.network import nac_stepup as STEPUP  # noqa: E402

# The person's authenticator, enrolled as Network enrols it.
run(STEPUP.mark_enrolled(POOL))
SECRET = run(STEPUP.ensure_stepup_secret(POOL))


def fresh_code():
    """The authenticator's code in a window no code was accepted in yet: the
    burned counter is cleared, as the next 30-second window would."""
    POOL.secrets.rows.pop("nac_stepup_last_counter", None)
    return STEPUP.current_code(SECRET)


def posts():
    return [p for op, p in BRIDGE.calls if op == "community.post"]


# ── 1. the call files a card and sends nothing ──────────────────────────────

res = run(TC._exec_community_board(EID, {"action": "post", "category": "help",
                                         "title": TITLE, "body": BODY}))
r = res.get("result") or {}
check("post: nothing is sent to the network when the Host calls it", posts() == [], repr(BRIDGE.calls))
check("post: it answers not posted, waiting on the owner's card",
      res.get("status") == "ok" and r.get("posted") is False and r.get("awaiting_approval") is True,
      json.dumps(res)[:400])
tid = r.get("ticket_id")
row = POOL.rows.get(tid) or {}
md = json.loads(row.get("metadata") or "{}")
check("post: one card, locked to the person (user_only, no_pattern)",
      len(POOL.rows) == 1 and md.get("kind") == BPA.PROPOSAL_KIND and md.get("user_only") is True
      and md.get("no_pattern") is True, repr(md)[:300])
check("post: the card shows the title and the body exactly as they will go",
      f"Title: {TITLE}\n\n{BODY}" in row.get("sitrep", "") and "Every member of the network can read it"
      in row.get("sitrep", ""), row.get("sitrep", "")[:400])
stored = md.get(BPA.PAYLOAD_KEY) or {}
check("post: the card stores the request it will send",
      stored.get("title") == TITLE and stored.get("body") == BODY and stored.get("category") == "help"
      and str(stored.get("op_id") or "").startswith("host-"), repr(stored))
opts = [o["id"] for o in json.loads(row.get("options") or "[]")]
check("post: the card offers Post it and Don't post", opts == [BPA.OPT_POST, BPA.OPT_KEEP], repr(opts))
check("post: the Host is told it is not posted and cannot decide the card",
      "NOT POSTED YET" in r.get("note", "") and "You cannot decide the card" in r.get("note", ""))

# ── 2. the Host cannot decide it ────────────────────────────────────────────

host_refused = decide(tid, BPA.OPT_POST, actor="host").get("error") == 403
check("decide by the Host: refused, and the card stays open",
      host_refused and (POOL.rows.get(tid) or {}).get("status") == "open")
# Through its own tool, as it would try it.
from mastercode.runtime import tools_comms as TCM  # noqa: E402

tres = run(TCM._exec_tickets({"op": "decide", "ticket_id": tid or 0, "option_id": BPA.OPT_POST}))
check("decide by the Host's tickets tool: refused as the user's to decide", tres.get("status") != "ok" and "the user's to decide" in json.dumps(tres)
      and (POOL.rows.get(tid) or {}).get("status") == "open", json.dumps(tres)[:300])
check("decide by the Host: nothing was sent", posts() == [], repr(BRIDGE.calls))

# ── 3. the person's 'Post it' sends exactly the stored request ──────────────

before = len(posts())
out = decide(tid, BPA.OPT_POST, code=fresh_code())
sent = posts()[before:]
check("decide by the person: one post goes", len(sent) == 1, repr(sent))
check("decide by the person: what goes is exactly what the card showed and stored",
      sent[:1] == [stored], f"{sent!r} vs {stored!r}")
check("decide by the person: the outcome is on the ticket",
      (out.get("board_post_apply") or {}).get("posted") is True
      and json.loads((POOL.rows.get(tid) or {}).get("metadata") or "{}").get("board_post_apply", {}).get("posted") is True,
      json.dumps(out)[:300])

# ── 4. a second click, and Don't post ───────────────────────────────────────

n = len(posts())
again = decide(tid, BPA.OPT_POST, code=fresh_code()).get("error")
check("a second click on a decided card: refused, nothing posted again", again == 409 and len(posts()) == n,
      repr(again))

res = run(TC._exec_community_board(EID, {"action": "post", "thread_id": 88, "parent_id": 912,
                                         "body": "It happens on the second Host too."}))
tid2 = (res.get("result") or {}).get("ticket_id")
row2 = POOL.rows.get(tid2) or {"sitrep": ""}
check("reply: a card too, showing the reply's exact text, nothing sent",
      len(posts()) == n and "It happens on the second Host too." in row2["sitrep"]
      and "thread 88" in row2["sitrep"], row2["sitrep"][:300] or json.dumps(res)[:300])
n = len(posts())
out = decide(tid2, BPA.OPT_KEEP, note="not yet")
check("Don't post: nothing is sent", len(posts()) == n and (out.get("board_post_apply") or {}).get("posted") is False,
      json.dumps(out)[:300])

res = run(TC._exec_community_board(EID, {"action": "post", "thread_id": 88, "body": "First words."}))
tid3 = (res.get("result") or {}).get("ticket_id")
stored3 = json.loads((POOL.rows.get(tid3) or {}).get("metadata") or "{}").get(BPA.PAYLOAD_KEY)
# The Host changes its mind after the card is up: the card's words are what go.
run(TC._exec_community_board(EID, {"action": "post", "thread_id": 88, "body": "Other words."}))
n = len(posts())
decide(tid3, BPA.OPT_POST, code=fresh_code())
check("the text that goes is the one on the card the person pressed, never a later one",
      posts()[n:] == [stored3] and stored3["body"] == "First words.", repr(posts()))

cards = len(POOL.rows)
res = run(TC._exec_community_board(EID, {"action": "post", "category": "help", "title": "", "body": "x"}))
check("a thread with no title is refused, and no card is filed", res.get("status") != "ok"
      and len(POOL.rows) == cards, json.dumps(res)[:200])

# ── 5. no forged card ───────────────────────────────────────────────────────

forged = TK._strip_platform_locks({"kind": "board_post_proposal", "user_only": True,
                                   "board_post": {"op_id": "x", "body": "y", "thread_id": 1}})
check("POST /api/tickets cannot forge a card that posts on the board",
      "kind" not in forged and "board_post" not in forged and "user_only" not in forged, repr(forged))

# ── 6. the words ────────────────────────────────────────────────────────────

from mastercode import public_tool_registry as PTR  # noqa: E402

desc = PTR._COMMUNITY_BOARD_SCHEMAS["community_board"]["function"]["description"]
check("tool description: a post waits on the owner's card", "only your user's Post it there posts it" in desc, desc)
skill = open(os.path.join(_ROOT, "default-skills", "tickets.md"), encoding="utf-8").read()
check("tickets skill: the board post goes out only when they press Post it on the card",
      "the post goes out only when they press Post it there" in skill)
from server import support_reach as SR  # noqa: E402

check("support board offer: the post goes out only on the press on the card",
      "it goes out only when you press Post it there" in SR.BOARD_OFFER, SR.BOARD_OFFER)
check("support board offer: the press takes a code from the authenticator",
      "with a code from your authenticator" in SR.BOARD_OFFER, SR.BOARD_OFFER)
check("the Host's answer: it never sees or asks for the code",
      "never ask them for the code" in (r.get("note") or ""), r.get("note"))
check("tickets skill: the code is typed on the card, never asked for",
      "code from their authenticator typed on that card" in skill)

# ── 7. Post it takes the authenticator code ─────────────────────────────────


def new_card(body):
    r = run(TC._exec_community_board(EID, {"action": "post", "thread_id": 88, "body": body}))
    return (r.get("result") or {}).get("ticket_id")


def is_open(t):
    return (POOL.rows.get(t) or {}).get("status") == "open"


row7 = POOL.rows.get(new_card("Seven.")) or {}
md7 = json.loads(row7.get("metadata") or "{}")
opts7 = {o["id"]: o for o in json.loads(row7.get("options") or "[]")}
check("card words: Post it says it needs a code from the authenticator",
      "with a code from your authenticator" in row7.get("sitrep", "")
      and "It needs a current code from your authenticator." in opts7.get(BPA.OPT_POST, {}).get("consequence", ""),
      row7.get("sitrep", "")[:300])

t7 = new_card("No code given.")
n = len(posts())
r7 = decide(t7, BPA.OPT_POST)
check("Post it with no code: refused, nothing sent, the card stays open",
      r7.get("error") == 403 and len(posts()) == n and is_open(t7), repr(r7))
check("the refusal says what to do, and that nothing was posted",
      "code from your authenticator" in str(r7.get("detail")) and "Nothing was posted" in str(r7.get("detail")),
      repr(r7))
good = STEPUP.current_code(SECRET)
wrong = f"{(int(good) + 1) % 1_000_000:06d}"
r7 = decide(t7, BPA.OPT_POST, code=wrong)
check("Post it with a wrong code: refused, nothing sent, the card stays open",
      r7.get("error") == 403 and len(posts()) == n and is_open(t7), repr(r7))
import time as _time  # noqa: E402

stale = STEPUP.current_code(SECRET, at=_time.time() - 600)
r7 = decide(t7, BPA.OPT_POST, code=stale)
check("Post it with a stale code (ten minutes old): refused, nothing sent",
      r7.get("error") == 403 and len(posts()) == n and is_open(t7), repr(r7))
r7 = decide(t7, BPA.OPT_POST, actor="host", code=fresh_code())
check("the Host passing a right code: refused, nothing sent", r7.get("error") == 403
      and len(posts()) == n and is_open(t7), repr(r7))
code = fresh_code()
r7 = decide(t7, BPA.OPT_POST, code=code)
stored7 = json.loads((POOL.rows.get(t7) or {}).get("metadata") or "{}").get(BPA.PAYLOAD_KEY)
check("Post it with a fresh code: exactly the stored text goes, once",
      posts()[n:] == [stored7] and stored7["body"] == "No code given."
      and (r7.get("board_post_apply") or {}).get("posted") is True, repr(r7)[:300])
t8 = new_card("Replay.")
n = len(posts())
r8 = decide(t8, BPA.OPT_POST, code=code)
check("the same code again, on another card: refused as replayed, nothing sent",
      r8.get("error") == 403 and len(posts()) == n and is_open(t8), repr(r8))
r8 = decide(t8, BPA.OPT_KEEP, note="no")
check("Don't post needs no code, and sends nothing", r8.get("decision", {}).get("option_id") == BPA.OPT_KEEP
      and len(posts()) == n, repr(r8)[:200])
POOL.secrets.rows.pop("nac_stepup_enrolled", None)
t9 = new_card("Not enrolled.")
r9 = decide(t9, BPA.OPT_POST, code=fresh_code())
check("no authenticator enrolled: refused, nothing sent", r9.get("error") == 403
      and len(posts()) == n and is_open(t9), repr(r9))
run(STEPUP.mark_enrolled(POOL))

# ── 8. propose_capability files the same card ───────────────────────────────

from mastercode.runtime import tools_governance as TG  # noqa: E402
from server.routes import community as COMM  # noqa: E402

LADDER = []


async def _fake_propose(thread_id, done_looks_like=""):
    LADDER.append((thread_id, done_looks_like))
    return {"proposal_ref": 77, "url": "https://forge.example/proposals/pulls/77"}


async def _fake_comm_op(op, params):
    BRIDGE.calls.append((op, json.loads(json.dumps(params or {}))))
    if op == "community.categories":
        return {"categories": [{"slug": "general", "name": "General"}, {"slug": "ideas", "name": "Ideas"}]}
    if op == "community.post":
        return {"id": 9999}
    return {}


COMM.propose = _fake_propose
COMM._op = _fake_comm_op
n = len(posts())
cap = run(TG._exec_propose_capability({"title": "Overlay windows", "body": "Apps cannot ask for one.",
                                       "done_looks_like": "An app can ask for an overlay window."}))
cr = cap.get("result") or {}
ct = cr.get("ticket_id")
crow = POOL.rows.get(ct) or {}
cmd = json.loads(crow.get("metadata") or "{}")
check("propose_capability: nothing posted, no proposal filed, a card is up",
      len(posts()) == n and LADDER == [] and cap.get("status") == "ok" and cr.get("awaiting_approval") is True
      and cmd.get("kind") == BPA.PROPOSAL_KIND and cmd.get("user_only") is True, json.dumps(cap)[:300])
check("propose_capability: the card holds the Ideas thread's exact text",
      (cmd.get(BPA.PAYLOAD_KEY) or {}).get("category") == "ideas"
      and "Title: Overlay windows\n\nApps cannot ask for one." in crow.get("sitrep", ""), repr(cmd)[:300])
check("propose_capability: the host is refused deciding it",
      decide(ct, BPA.OPT_POST, actor="host", code=fresh_code()).get("error") == 403 and LADDER == [])
check("propose_capability: Post it with no code sends nothing and files nothing",
      decide(ct, BPA.OPT_POST).get("error") == 403 and len(posts()) == n and LADDER == [])
cout = decide(ct, BPA.OPT_POST, code=fresh_code())
capp = cout.get("board_post_apply") or {}
check("propose_capability: Post it with a code posts the thread, then files the proposal on it",
      posts()[n:] == [cmd[BPA.PAYLOAD_KEY]] and LADDER == [(4101, "An app can ask for an overlay window.")]
      and (capp.get("proposal") or {}).get("proposal_ref") == 77, json.dumps(cout)[:400])
cap2 = run(TG._exec_propose_capability({"title": "Another", "body": "Gap."}))
ct2 = (cap2.get("result") or {}).get("ticket_id")
n = len(posts())
decide(ct2, BPA.OPT_KEEP, note="later")
check("propose_capability: Don't post files nothing", len(posts()) == n and len(LADDER) == 1, repr(LADDER))
forged = TK._strip_platform_locks({"board_post_then": {"propose": {}}})
check("POST /api/tickets cannot forge the follow-up", "board_post_then" not in forged, repr(forged))

print()
if _failures:
    print(f"FAIL board_post_approval: {_failures} check(s)")
    sys.exit(1)
print("PASS board_post_approval")
