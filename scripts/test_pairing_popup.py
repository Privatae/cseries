#!/usr/bin/env python3
"""The pop-up that asks the person to let another Host use their machine.

When a heartbeat from a machine this Host anchors carries a pending pairing,
the Host files the ask card every chat already shows (a decision ticket of
kind `bastion_pairing`, server/network/pairing_grants.py _raise_card):
"<Host> wants to use <machine>. Let it in?", what it may do ticked, 'Let it
in' with the authenticator code, 'Never', and the card's own 'Not now'.

Pinned here, container-free (a recording pool, the ticket writer and the
grant calls stubbed):
  1. first sight files ONE card (the platform's kind, the person's room, the
     default ceiling) and tells the Host there is nothing for it to approve
  2. asked once: no second card while one is up and the day has not passed
  3. unanswered a day ("Not now", or nobody looked): the old card goes and a
     fresh one comes
  4. Never (denied) stops it and takes a card down; a pairing settled
     elsewhere (active, gone) takes its card down
  5. a pairing the box says waits for ANOTHER Host is not asked here
  6. the card cannot be filed: the inbox note is the fallback, the card is
     tried again on the next beat; asked by an older build (inbox only): the
     card comes now
  7. a member Host is told once, never asked
  8. the words: plain, never "the Network app"
  9. the answer: Let it in = approve_pairing with the ticked ceiling and the
     card's code; Never = deny_pairing; only the person; a wrong code says so
 10. tickets.decide_ticket_service runs the answer BEFORE the card is decided,
     so a refusal leaves it up; POST /api/tickets cannot forge the kind

Runs as pytest and as a plain script (APP_ROOT=<repo> python3 scripts/<this>.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]
os.environ["CSERIES_URL"] = "http://home.cseries.localhost:8800"

from server.network import pairing_grants as pg  # noqa: E402

EID, TOKEN, SERVER = 3, 7, "box-a"
SUBJECT, HANDLE = "a1b2c3d4e5f60718.zone.example.test", "a1b2c3d4e5f60718"
OWN = ("home" + ".cseries.localhost",)
NOW = dt.datetime.now(dt.timezone.utc)


def _run(coro):
    return asyncio.run(coro)


class Pool:
    """network_bastion_pairing rows for the scan, entity_tickets rows for the
    cards; records every statement."""

    def __init__(self, row=None, cards=None):
        self.sql: list = []
        self.row = row
        self.cards = cards or []   # [{id, status, subject}]

    async def execute(self, q, *a):
        self.sql.append((q, a))
        if "UPDATE entity_tickets SET status='dismissed'" in q:
            for c in self.cards:
                if c["id"] == a[0] and c["status"] == "open":
                    c["status"] = "dismissed"
                    return "UPDATE 1"
            return "UPDATE 0"
        return "UPDATE 1"

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        if "FROM network_bastion_pairing" in q:
            return [self.row] if self.row else []
        if "FROM entity_tickets" in q:
            assert a[1] == pg.PAIRING_CARD_KIND and a[2] == str(TOKEN), a
            return [dict(c) for c in sorted(self.cards, key=lambda c: -c["id"])]
        return []

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        return None

    async def fetchval(self, q, *a):
        return None


def row(**kw):
    base = {"subject": SUBJECT, "subject_handle": HANDLE, "since": "",
            "pending_notified_at": None, "unapproved_raised_at": None, "denied_at": None}
    return {**base, **kw}


PENDING = [{"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": "",
            "transport": "tunnel", "origin": "paired", "waiting_for": OWN[0]}]


class Wire:
    """The ticket writer, the inbox, the Host's message list and the anchor's
    own names, stubbed and recorded."""

    def __init__(self, monkey, *, file_ok=True, inbox_ok=True):
        self.filed: list = []
        self.inbox: list = []
        self.host: list = []
        from server.routes import tickets as tk

        async def _create(pool, eid, **kw):
            if not file_ok:
                raise tk.TicketError(503, "brain pool down")
            self.filed.append(kw)
            return {"id": 100 + len(self.filed)}

        async def _inbox(subject, body, payload):
            self.inbox.append(payload)
            return inbox_ok

        async def _msg(content, action):
            self.host.append((action, content))

        async def _own(eid):
            return OWN

        async def _emit(*a, **k):
            pass
        monkey.setattr(tk, "create_ticket_service", _create)
        monkey.setattr(pg, "_inbox", _inbox)
        monkey.setattr(pg, "_host_message", _msg)
        monkey.setattr(pg, "_own_host_names", _own)
        from server import host_events
        monkey.setattr(host_events, "emit", _emit)


def _beat(pool, pairings=PENDING, *, is_anchor=True):
    return _run(pg.record_and_notify(pool, EID, TOKEN, SERVER, pairings, None, is_anchor=is_anchor))


# ── 1-3. asked once; asked again after a day ────────────────────────────────

def test_first_sight_files_one_card_in_the_persons_room(monkeypatch):
    w = Wire(monkeypatch)
    pool = Pool(row())
    out = _beat(pool)
    assert out["pending_raised"] == 1 and len(w.filed) == 1, (out, w.filed)
    kw = w.filed[0]
    md = kw["metadata"]
    assert md["kind"] == pg.PAIRING_CARD_KIND and md["user_only"] and md["no_pattern"]
    assert md["asked_in"] == {"app_id": "circles"}, "the card goes to the person's own chat"
    bp = md[pg.PAIRING_CARD_KIND]
    assert (bp["token_id"], bp["server"], bp["subject"]) == (TOKEN, SERVER, SUBJECT)
    assert bp["stepup_option"] == pg.CARD_ADMIT
    assert [c["id"] for c in bp["ceiling"] if c["on"]] == list(pg.DEFAULT_CEILING)
    assert [c["id"] for c in bp["ceiling"]] == list(pg.CAPS), "every family is offered"
    assert kw["sitrep"].startswith(f"{HANDLE} wants to use {SERVER}. Let it in?")
    assert [o["id"] for o in kw["options"]] == [pg.CARD_ADMIT, pg.CARD_NEVER]
    # marked asked; the Host is told once that there is nothing for it to do
    assert any("SET pending_notified_at = COALESCE(pending_notified_at, now())" in q
               for q, _ in pool.sql)
    assert [a for a, _ in w.host] == ["pairing_pending"]
    assert "nothing for you to approve" in w.host[0][1]
    assert w.inbox == [], "the pop-up is the ask; no second note in the inbox"


def test_asked_once_no_second_card_within_the_day(monkeypatch):
    w = Wire(monkeypatch)
    pool = Pool(row(pending_notified_at=NOW - dt.timedelta(hours=3),
                    unapproved_raised_at=NOW - dt.timedelta(hours=3)),
                cards=[{"id": 101, "status": "open", "subject": SUBJECT}])
    out = _beat(pool)
    assert out == {"pending_raised": 0, "unapproved_raised": 0, "resynced": 0}
    assert w.filed == [] and w.host == [] and w.inbox == []
    # "Not now" on the device: the card is set aside there, still open here,
    # and nothing comes back before the day is out
    pool = Pool(row(pending_notified_at=NOW - dt.timedelta(hours=23),
                    unapproved_raised_at=NOW - dt.timedelta(hours=23)),
                cards=[{"id": 101, "status": "open", "subject": SUBJECT}])
    assert _beat(pool)["unapproved_raised"] == 0 and w.filed == []


def test_unanswered_for_a_day_the_card_comes_again_fresh(monkeypatch):
    w = Wire(monkeypatch)
    cards = [{"id": 101, "status": "open", "subject": SUBJECT}]
    pool = Pool(row(pending_notified_at=NOW - dt.timedelta(hours=25),
                    unapproved_raised_at=NOW - dt.timedelta(hours=25)), cards=cards)
    out = _beat(pool)
    assert out["unapproved_raised"] == 1 and len(w.filed) == 1
    assert cards[0]["status"] == "dismissed", "the old card goes before the new one comes"
    assert w.host == [], "the Host is told once, on the first ask"
    # a card the person set aside in Tickets (dismissed) is asked again too
    w2 = Wire(monkeypatch)
    pool = Pool(row(pending_notified_at=NOW - dt.timedelta(days=3),
                    unapproved_raised_at=NOW - dt.timedelta(days=2)),
                cards=[{"id": 101, "status": "dismissed", "subject": SUBJECT}])
    assert _beat(pool)["unapproved_raised"] == 1 and len(w2.filed) == 1


# ── 4-5. Never, settled elsewhere, another Host's to admit ──────────────────

def test_never_stops_the_asking_and_takes_the_card_down(monkeypatch):
    w = Wire(monkeypatch)
    cards = [{"id": 101, "status": "open", "subject": SUBJECT}]
    pool = Pool(row(pending_notified_at=NOW - dt.timedelta(days=5),
                    unapproved_raised_at=NOW - dt.timedelta(days=5), denied_at=NOW), cards=cards)
    out = _beat(pool)
    assert out["pending_raised"] == out["unapproved_raised"] == 0 and w.filed == []
    assert cards[0]["status"] == "dismissed"


def test_a_pairing_settled_elsewhere_takes_its_card_down(monkeypatch):
    w = Wire(monkeypatch)
    cards = [{"id": 101, "status": "open", "subject": SUBJECT},
             {"id": 99, "status": "decided", "subject": SUBJECT}]
    active = [{"host_name": SUBJECT, "handle": HANDLE, "state": "active", "since": ""}]
    _beat(Pool(None, cards=cards), active)
    assert cards[0]["status"] == "dismissed" and cards[1]["status"] == "decided"
    assert w.filed == []
    # gone from the box altogether: the same
    cards = [{"id": 102, "status": "open", "subject": SUBJECT}]
    _beat(Pool(None, cards=cards), [])
    assert cards[0]["status"] == "dismissed"


def test_a_pairing_that_waits_for_another_host_is_not_asked_here(monkeypatch):
    w = Wire(monkeypatch)
    other = [{**PENDING[0], "waiting_for": "someone-else.zone.example.test"}]
    out = _beat(Pool(row()), other)
    assert out["pending_raised"] == 0 and w.filed == [] and w.inbox == []
    # an older bastion says nothing of who admits it: this anchor asks
    old = [{"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": ""}]
    assert _beat(Pool(row()), old)["pending_raised"] == 1 and len(w.filed) == 1


# ── 6. fallbacks ────────────────────────────────────────────────────────────

def test_card_not_filed_inbox_note_then_the_card_next_beat(monkeypatch):
    w = Wire(monkeypatch, file_ok=False)
    out = _beat(Pool(row()))
    assert out["pending_raised"] == 1 and [p["event"] for p in w.inbox] == ["pairing_pending"]
    assert "Network" not in json.dumps(w.inbox), "never sends the person to an app"
    # next beat: asked (the inbox note), never with a card → the card now
    w = Wire(monkeypatch)
    pool = Pool(row(pending_notified_at=NOW - dt.timedelta(minutes=1),
                    unapproved_raised_at=NOW - dt.timedelta(minutes=1)))
    out = _beat(pool)
    assert len(w.filed) == 1 and w.inbox == [] and out["unapproved_raised"] == 1


def test_asked_by_an_older_build_gets_the_card_now(monkeypatch):
    w = Wire(monkeypatch)
    pool = Pool(row(pending_notified_at=NOW - dt.timedelta(days=4),
                    unapproved_raised_at=NOW - dt.timedelta(hours=2)))
    assert _beat(pool)["unapproved_raised"] == 1 and len(w.filed) == 1


# ── 7. a member Host ────────────────────────────────────────────────────────

def test_a_member_host_is_told_once_and_never_asked(monkeypatch):
    w = Wire(monkeypatch)
    out = _beat(Pool(row()), is_anchor=False)
    assert out["pending_raised"] == 1 and w.filed == []
    assert [p["event"] for p in w.inbox] == ["pairing_pending"] and w.inbox[0]["is_anchor"] is False
    w = Wire(monkeypatch)
    out = _beat(Pool(row(pending_notified_at=NOW - dt.timedelta(days=3))), is_anchor=False)
    assert out["pending_raised"] == out["unapproved_raised"] == 0 and w.filed == w.inbox == []
    assert not any("FROM entity_tickets" in q for q, _ in Pool().sql)


# ── 8. the words ────────────────────────────────────────────────────────────

def test_the_card_words_are_plain_and_name_no_app():
    s, opts, ceiling = pg.card_words(HANDLE, SERVER, {"origin": "paired", "transport": "tunnel"})
    assert s.startswith(f"{HANDLE} wants to use {SERVER}. Let it in?")
    assert "pairing code" in s and "in the cloud" in s and "choose Never" in s
    s2, _, _ = pg.card_words(HANDLE, SERVER, {"origin": "former_anchor", "same_machine": True})
    assert "in charge of box-a" in s2 and "runs on box-a itself" in s2
    s3, _, _ = pg.card_words(HANDLE, SERVER, {"origin": "carried"})
    assert "already set up on box-a" in s3
    every = " ".join([s, s2, s3] + [o["label"] + o.get("consequence", "") for o in opts]
                     + [c["label"] for c in ceiling])
    for word in ("Network", "anchor", "pending", "pairing grant", "caps", "bastion"):
        assert word not in every, word
    assert {c["id"] for c in ceiling} == set(pg.CAPS) == set(pg.CAP_WORDS)
    # a control character in the box's strings never reaches the card
    s4, _, _ = pg.card_words("evil\nname", "box\x07-a", {})
    assert "\n" not in s4.split("\n\n")[0] and "\x07" not in s4


# ── 9. the answer ───────────────────────────────────────────────────────────

MD = {"kind": pg.PAIRING_CARD_KIND, pg.PAIRING_CARD_KIND: {
    "token_id": TOKEN, "server": SERVER, "subject": SUBJECT, "handle": HANDLE,
    "stepup_option": pg.CARD_ADMIT, "ceiling": pg.card_words(HANDLE, SERVER)[2]}}


def _grant_calls(monkey, *, approve_error=None, approve_result=None):
    calls: list = []

    async def _approve(pool, eid, server, subject, caps, **kw):
        calls.append(("approve", server, subject, list(caps), kw))
        if approve_error:
            raise approve_error
        return approve_result or {"caps": caps, "delivered": True, "state": "active", "note": None}

    async def _deny(pool, eid, server, subject, **kw):
        calls.append(("deny", server, subject, kw))
        return {"denied": True}
    monkey.setattr(pg, "approve_pairing", _approve)
    monkey.setattr(pg, "deny_pairing", _deny)
    return calls


def test_let_it_in_admits_with_the_ticked_ceiling_and_the_cards_code(monkeypatch):
    calls = _grant_calls(monkeypatch)
    res = _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="123456",
                              actor="user", choices=["inference", "egress"]))
    assert res["admitted"] and calls[0][:4] == ("approve", SERVER, SUBJECT, ["egress", "inference"])
    assert calls[0][4] == {"stepup_code": "123456", "via": "ui", "passkey_purpose": None}, \
        "the person's own act"
    # nothing ticked on the card's answer → the card's defaults
    calls.clear()
    _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="1", actor="user"))
    assert calls[0][3] == list(pg.DEFAULT_CEILING)


def test_let_it_in_refuses_an_empty_or_unknown_ceiling(monkeypatch):
    calls = _grant_calls(monkeypatch)
    for choices, why in (([], "Tick at least one"), (["root"], "root"), ("observe", "list")):
        try:
            _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="1",
                                actor="user", choices=choices))
            assert False, choices
        except pg.PairingError as e:
            assert e.status == 400 and why in e.detail, (choices, e.detail)
    # a family the card never offered is refused even if it is a real one
    md = {**MD, pg.PAIRING_CARD_KIND: {**MD[pg.PAIRING_CARD_KIND],
                                       "ceiling": [{"id": "observe", "on": True}]}}
    try:
        _run(pg.decide_card(None, EID, md, option_id=pg.CARD_ADMIT, stepup_code="1",
                            actor="user", choices=["session"]))
        assert False
    except pg.PairingError as e:
        assert e.status == 400
    assert calls == []


def test_no_authenticator_says_so_instead_of_wrong_code(monkeypatch):
    _grant_calls(monkeypatch, approve_error=pg.PairingError(
        403, "admitting a host on your machine widens access — it needs a valid step-up code"))

    async def _not_enrolled(pool):
        return False
    monkeypatch.setattr(pg, "_stepup_enrolled", _not_enrolled)
    try:
        _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="000000",
                            actor="user"))
        assert False
    except pg.PairingError as e:
        assert e.status == 403 and "no passkey or authenticator set up" in e.detail, e.detail


def test_an_older_box_that_lists_attempts_only_takes_no_card_down(monkeypatch):
    Wire(monkeypatch)
    cards = [{"id": 101, "status": "open", "subject": SUBJECT}]
    pool = Pool(None, cards=cards)
    _run(pg.record_and_notify(pool, EID, TOKEN, SERVER, None, [], is_anchor=True))
    assert cards[0]["status"] == "open", "no pairings list: nothing is known to have settled"


def test_a_wrong_code_says_so_in_plain_words(monkeypatch):
    _grant_calls(monkeypatch, approve_error=pg.PairingError(
        403, "admitting a host on your machine widens access — it needs a valid step-up code"))
    try:
        _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="000000",
                            actor="user"))
        assert False
    except pg.PairingError as e:
        assert e.status == 403 and e.detail.startswith("That code didn't work.")
        assert "step-up" not in e.detail


def test_a_grant_the_machine_refused_leaves_the_card_up(monkeypatch):
    # Signed and sent, and the box said no (approve_pairing returns, it does
    # not raise): nobody was let in, so the card must not close on it.
    _grant_calls(monkeypatch, approve_result={"caps": ["observe"], "delivered": False,
                                              "state": "refused", "detail": "bad signature"})
    try:
        _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="123456",
                            actor="user"))
        assert False, "a refused grant must not read as admitted"
    except pg.PairingError as e:
        assert "still not let in" in e.detail, e.detail
    # offline is different: the grant is kept and delivered on the next check-in
    _grant_calls(monkeypatch, approve_result={"caps": ["observe"], "delivered": False,
                                              "state": "offline", "note": "later"})
    res = _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="123456",
                              actor="user"))
    assert res["admitted"] and res["state"] == "offline"


def test_other_refusals_reach_the_person_in_plain_words(monkeypatch):
    for err in (pg.PairingError(404, f"'{SUBJECT}' is not paired on '{SERVER}' — the box does "
                                     f"not report it now; nothing to approve"),
                pg.PairingError(403, f"'{SERVER}' is anchored to x, not to this host — only "
                                     f"its anchor may admit or remove pairings"),
                pg.PairingError(409, f"'{SERVER}' is not anchored — run `privatae-bastion "
                                     f"claim-anchor` on that machine"),
                pg.PairingError(409, "this host's own handle could not be resolved right now"),
                pg.PairingError(503, "brain pool not ready")):
        _grant_calls(monkeypatch, approve_error=err)
        try:
            _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="1",
                                actor="user"))
            assert False
        except pg.PairingError as e:
            low = e.detail.lower().replace(SERVER, "<machine>")
            assert "nothing was changed" in low, e.detail
            for word in ("anchor", "privatae-bastion", "paired", "box", "bastion", "network app"):
                assert word not in low, (word, e.detail)


def test_never_denies_and_only_the_person_answers(monkeypatch):
    calls = _grant_calls(monkeypatch)
    res = _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_NEVER, stepup_code=None, actor="user"))
    assert res["denied"] and calls == [("deny", SERVER, SUBJECT, {"via": "ui"})]
    for actor in ("host", "app:circles-bot"):
        try:
            _run(pg.decide_card(None, EID, MD, option_id=pg.CARD_ADMIT, stepup_code="1", actor=actor))
            assert False
        except pg.PairingError as e:
            assert e.status == 403
    try:
        _run(pg.decide_card(None, EID, {"kind": pg.PAIRING_CARD_KIND}, option_id=pg.CARD_NEVER,
                            stepup_code=None, actor="user"))
        assert False
    except pg.PairingError as e:
        assert e.status == 400
    assert len(calls) == 1


# ── 10. through the ticket decide ───────────────────────────────────────────

class TicketPool:
    def __init__(self):
        self.sql: list = []
        self.status = "open"

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        if "FROM entity_tickets" in q:
            return {"id": 12, "circle_id": None, "status": self.status,
                    "options": json.dumps(pg.card_words(HANDLE, SERVER)[1]),
                    "metadata": json.dumps({**MD, "user_only": True, "no_pattern": True})}
        return None

    async def execute(self, q, *a):
        self.sql.append((q, a))
        if "SET status = 'decided'" in q:
            self.status = "decided"
            return "UPDATE 1"
        return "UPDATE 1"

    async def fetchval(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []


def test_the_ticket_decide_answers_first_and_a_refusal_leaves_the_card_up(monkeypatch):
    from server.routes import tickets as tk
    seen: list = []

    async def _decide(pool, eid, md, **kw):
        seen.append(kw)
        if kw["stepup_code"] != "123456":
            raise pg.PairingError(403, "That code didn't work.")
        return {"admitted": True, "caps": ["observe"]}

    async def _answer(*a, **k):
        return {}
    monkeypatch.setattr(pg, "decide_card", _decide)
    monkeypatch.setattr(tk, "_answer_to_turn", _answer)
    pool = TicketPool()
    try:
        _run(tk.decide_ticket_service(pool, EID, 12, option_id=pg.CARD_ADMIT, stepup_code="000000",
                                      choices=["observe"]))
        assert False
    except tk.TicketError as e:
        assert e.status == 403
    assert pool.status == "open" and not any("SET status = 'decided'" in q for q, _ in pool.sql)
    out = _run(tk.decide_ticket_service(pool, EID, 12, option_id=pg.CARD_ADMIT,
                                        stepup_code="123456", choices=["observe"]))
    assert pool.status == "decided" and out["pairing"]["admitted"]
    purpose = seen[-1].pop("passkey_purpose")
    assert seen[-1] == {"option_id": pg.CARD_ADMIT, "stepup_code": "123456", "actor": "user",
                        "choices": ["observe"]}
    # The person's answer carries what a passkey tap on it approves: this
    # ticket, this option, the card's words and the boxes ticked.
    assert isinstance(purpose, str) and purpose.startswith(f"ticket:12:{pg.CARD_ADMIT}:"), purpose
    assert out["decision"]["and_similar"] is False, "a pairing card never becomes a pattern"
    assert any("bastion_pairing_result" in q for q, _ in pool.sql), "the outcome is on the ticket"


def test_an_answer_applied_while_the_heartbeat_closed_the_card_still_decides_it(monkeypatch):
    # The person's Let it in was applied; between that and the status flip the
    # heartbeat took the card down (_close_card). The answer stands: the card
    # is decided as theirs, not refused as 'decided by another request'.
    from server.routes import tickets as tk

    class SweptPool(TicketPool):
        async def execute(self, q, *a):
            self.sql.append((q, a))
            if "SET status = 'decided'" in q and "status = 'open'" in q:
                self.status = "dismissed"     # the sweep won the race
                return "UPDATE 0"
            if "SET status = 'decided'" in q and "status = 'dismissed'" in q:
                assert a[3] == pg.CARD_SETTLED_NOTE, a
                if self.status == "dismissed":
                    self.status = "decided"
                    return "UPDATE 1"
                return "UPDATE 0"
            return "UPDATE 1"

    async def _decide(pool, eid, md, **kw):
        return {"admitted": True, "caps": ["observe"]}

    async def _answer(*a, **k):
        return {}
    monkeypatch.setattr(pg, "decide_card", _decide)
    monkeypatch.setattr(tk, "_answer_to_turn", _answer)
    pool = SweptPool()
    out = _run(tk.decide_ticket_service(pool, EID, 12, option_id=pg.CARD_ADMIT,
                                        stepup_code="123456", choices=["observe"]))
    assert pool.status == "decided" and out["pairing"]["admitted"]


def test_post_api_tickets_cannot_forge_the_card():
    from server.routes import tickets as tk
    assert tk.BASTION_PAIRING_KIND == pg.PAIRING_CARD_KIND
    forged = tk._strip_platform_locks({"kind": pg.PAIRING_CARD_KIND,
                                       pg.PAIRING_CARD_KIND: {"server": SERVER},
                                       "bastion_pairing_result": {}, "x": 1})
    assert forged == {"x": 1}, forged


def test_the_heartbeat_keeps_only_known_pop_up_fields():
    got = pg.narrow_pairings([{"host_name": SUBJECT, "handle": HANDLE, "state": "pending",
                               "waiting_for": OWN[0], "transport": "tunnel", "same_machine": True,
                               "origin": "former_anchor"},
                              {"host_name": "b.zone.example.test", "state": "pending",
                               "transport": "carrier-pigeon", "origin": "stolen",
                               "same_machine": "yes"}])
    assert got[0]["waiting_for"] == OWN[0] and got[0]["transport"] == "tunnel"
    assert got[0]["same_machine"] is True and got[0]["origin"] == "former_anchor"
    assert not {"transport", "origin", "same_machine", "waiting_for"} & set(got[1])


# ── script entry (the gate lane has no pytest) ──────────────────────────────

class _Monkey:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def main() -> int:
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        mk = _Monkey()
        try:
            fn(mk) if "monkeypatch" in inspect.signature(fn).parameters else fn()
            print("OK   " + name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name} -- {type(e).__name__}: {e}")
        finally:
            mk.undo()
    print(f"\nFAILURES: {failed} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
