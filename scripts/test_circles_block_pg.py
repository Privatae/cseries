#!/usr/bin/env python3
"""Gate — Block in Circles, against a REAL Postgres, on every way in.

THE BUG. Block set a contact's status and wrote a domain-policy deny rule, and
neither reached a message: an ordinary Host-to-Host message is admitted by its
permission slip alone (memory_handlers.handle_inbox_append), a live slip
outranks a domain-policy deny for the other kinds, and the platform renews the
slip until it is revoked. So a blocked Host kept messaging you, and a blocked
person's messages were delivered too.

WHAT BLOCK MEANS NOW:
  * Block a Host: what the Host itself sends is refused. A message from a person
    on it still arrives.
  * Block a person: their messages are refused, and their Host is blocked too.
    Unblocking the person lifts that Host block; a Host blocked directly stays
    blocked.
  * On the direct lane the person on a message is the sending Host's own word,
    so under a Host block it counts only for a person our contacts place on that
    Host; any other claim is the Host's message.

The rule is SQL over `contacts` (contacts.block_refuses), so this drives the real
handlers over a real kernel schema: the direct lane through PeerServer._serve
with real signed slips, the relay through handle_inbox_append with the payload
the bridge builds, and the context read through handle_context_fulfill. The app
registry is stubbed (an app being installed is not under test), and so is the
dashboard broadcast.

  A  nothing blocked: the control.
  B  a blocked Host: its own message, knock, reciprocal slip, app grant and
     delivery, machine token and context read are refused on every way in; its
     person's message still arrives; a person the receiver cannot place on it is
     the Host's; a group-scoped app delivery is left to the group's roster; our
     own outbound copy is untouched; the refusal reads as today's.
  C  a blocked person: refused from any Host, on both lanes; their Host is
     blocked too, and the contacts list says so on the Host's row; another
     person on that Host still arrives.
  D  unblocking the person lifts the Host block that came from them.
  E  a Host blocked directly stays blocked when the person is unblocked.
  F  two blocked people on one Host: it stays blocked until both are lifted.
  G  a person whose Host is unknown: refused by address on any Host; no Host
     is blocked with them.
  H  a check that cannot run (the pool is closed) refuses, and says try again.
  I  the Host's model cannot name its own person through a tool (inbox_send),
     so its words do not pass its Host's block as that person's.
  U  the Address book's line under a contact says which of the above Block
     does (circles-web/test/block-note.test.ts, under node --test).

Run (needs docker for a throwaway cseries-postgres, or TEST_PG_DSN):
    APP_ROOT=<repo> python3 scripts/test_circles_block_pg.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types
import uuid

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]

import asyncpg  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove Block.")
    sys.exit(2)

FAILS: list[str] = []
CHECKS = 0


def check(label: str, cond: bool, extra: object = "") -> None:
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("\n        " + str(extra)[:600]) if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


ME, JUNO, ZED = 1, 122, 130
ANA, BOB, CY, ZOE = "ana@example.com", "bob@example.com", "cy@example.com", "zoe@example.com"
MY_PERSON = "me@example.com"
#: The words today's domain-policy deny answers with (memory_handlers.py). A
#: block must say nothing more than that.
BLOCKED = "blocked by domain policy"

KEY = Ed25519PrivateKey.generate()
KID = "test-key-1"


def _iso(t: dt.datetime) -> str:
    return t.isoformat()


def slip(grantee: int, ops: list[str], resource: str, issuer: int = ME) -> dict:
    from server.peer.slip_verify import canonical_terms
    now = dt.datetime.now(dt.timezone.utc)
    cid = "c-" + uuid.uuid4().hex[:12]
    terms = {"contract_id": cid, "key_id": KID, "issuer": issuer, "grantee": grantee,
             "resource": resource, "allowed_ops": ops,
             "issued_at": _iso(now - dt.timedelta(minutes=1)),
             "expires_at": _iso(now + dt.timedelta(hours=1))}
    sig = base64.b64encode(KEY.sign(canonical_terms(terms))).decode()
    return {"contract_id": cid, "key_id": KID, "terms": terms, "signature": sig}


class Trust:
    """The pinned bridge key and an empty revocation list — what PeerTrust holds
    once it has synced."""
    ready = True

    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        self.keys = {KID: KEY.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)}
        self.revoked: set = set()

    async def ensure_key(self, _k):
        return False

    async def refresh(self):
        return None


async def main(dsn: str) -> int:
    trust = Trust()

    # server.main is the FastAPI app; only three names on it are read on these
    # paths: the dashboard push, the bridge client (none here), and the slip
    # trust the relay gate shares with the peer lane.
    fake_main = types.ModuleType("server.main")

    async def broadcast(_msg):
        return None

    fake_main.broadcast = broadcast
    fake_main.get_bridge_client = lambda: None
    fake_main._peer_trust_ref = trust
    sys.modules["server.main"] = fake_main
    import server
    server.main = fake_main

    from server import brain_service
    from server.bridge import memory_handlers as MH
    from server.peer import handlers as H
    from server.peer.peer_server import PeerServer
    from server.routes import contacts as C
    from server.applications import registry as REG

    class App:
        status = "active"
        permissions = ["read:peer_app", "write:peer_app"]

    async def fake_get(_pool, app_id):
        return App() if app_id == "veterans" else None

    REG.get = fake_get
    os.environ["ENTITY_OPERATOR_EMAILS"] = MY_PERSON

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    brain_service._pool, brain_service._entity_id = pool, ME
    srv = PeerServer(trust=trust, handlers=H.PEER_HANDLERS, my_entity_id=ME)
    msg_slip = {JUNO: slip(JUNO, ["message"], "dm:contact"),
                ZED: slip(ZED, ["message"], "dm:contact")}

    # ── the address book ───────────────────────────────────────────────────
    async def add(handle, kind, peer, name=None):
        return await pool.fetchval(
            "INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id) "
            "VALUES ($1, $2, $3, 'active', $4) RETURNING id", handle, name, kind, peer)

    juno_id = await add("juno", "host", JUNO, "Juno")
    await add("zed", "host", ZED, "Zed")
    ana_id = await add(ANA, "human", JUNO, "Ana")
    cy_id = await add(CY, "human", None, "Cy")          # a person whose Host is unknown
    await add(ZOE, "human", ZED, "Zoe")                  # a person our records place on Zed

    async def set_status(cid, status):
        r = await C.update_contact_service(pool, contact_id=cid, status=status)
        assert r["ok"], r

    # ── the ways in ────────────────────────────────────────────────────────
    async def stored(mid):
        return await pool.fetchrow(
            "SELECT from_entity_id, from_user_email, message_kind FROM inbox_messages "
            "WHERE message_id = $1::uuid", mid)

    async def direct(peer, person=None, *, to_person=None):
        """inbox.deliver over the direct lane, with the slip we issued `peer`."""
        mid = str(uuid.uuid4())
        params = {"subject": "Direct message", "body": f"hello {mid}", "message_id": mid}
        if person:
            params["from_user_email"] = person
        if to_person:
            params["to_user_email"] = to_person
        r = await srv._serve(peer, {"op": "inbox.deliver", "slip": msg_slip[peer], "params": params})
        return r, await stored(mid)

    async def relay(peer, person=None, *, kind="message", with_slip=True):
        """inbox.append as the bridge hands it over (adapter op_circles_send_entity_dm
        / op_inbox_send): sender stamped, person checked by the platform, the
        slip forwarded for this Host to verify itself."""
        mid = str(uuid.uuid4())
        p = {"subject": "Direct message", "body": f"hello {mid}", "body_format": "text/plain",
             "message_kind": kind, "message_id": mid, "payload": {"from_entity_id": peer},
             "from_entity_id": peer, "sender_verified": True,
             "from_address": f"{peer}@entity.localhost"}
        if with_slip:
            p["slip"] = msg_slip[peer]
        if person:
            p["from_user_email"] = person
            p["payload"]["from_user_email"] = person
        r = await MH.handle_inbox_append(p)
        return r, await stored(mid)

    def refused(r) -> bool:
        """The refusal reads as today's domain-policy deny, on either lane."""
        if r.get("ok") is not False:
            return False
        inner = r.get("data") if isinstance(r.get("data"), dict) else r
        return inner.get("blocked") is True and r.get("error") == BLOCKED

    def admitted(r) -> bool:
        return r.get("ok") is True

    # ── A. nothing blocked ─────────────────────────────────────────────────
    print("\n── A. nothing blocked (the control) ──")
    r, row = await direct(JUNO)
    check("A1 direct: Juno's own message is admitted", admitted(r) and row is not None, r)
    r, row = await relay(JUNO)
    check("A2 relay: Juno's own message is admitted", admitted(r) and row is not None, r)

    # ── B. block the Host ──────────────────────────────────────────────────
    print("\n── B. a blocked Host ──")
    await set_status(juno_id, "blocked")
    r, row = await direct(JUNO)
    check("B1 direct: the blocked Host's own message is refused, as today's deny reads",
          refused(r) and row is None, r)
    r, row = await relay(JUNO)
    check("B2 relay: the blocked Host's own message is refused, live slip and all",
          refused(r) and row is None, r)
    r, row = await direct(JUNO, ANA)
    check("B3 direct: Ana's message (a person our contacts place on Juno) still arrives",
          admitted(r) and row is not None and row["from_user_email"] == ANA, (r, row))
    r, row = await direct(JUNO, ANA, to_person=MY_PERSON)
    check("B4 direct: Ana's message to our person (People) still arrives",
          admitted(r) and row is not None and row["from_user_email"] == ANA, (r, row))
    r, row = await relay(JUNO, BOB)
    check("B5 relay: a person the platform checked runs Juno still arrives, contact or not",
          admitted(r) and row is not None and row["from_user_email"] == BOB, (r, row))
    r, row = await direct(JUNO, BOB)
    check("B6 direct: a person the receiver cannot place on Juno is the Host's message — refused",
          refused(r) and row is None, r)
    r, row = await direct(JUNO, CY)
    check("B7 direct: a person whose Host we do not know is not placed on Juno — refused",
          refused(r) and row is None, r)
    r, row = await direct(JUNO, ZOE)
    check("B8 direct: a person our records place on ANOTHER Host is not Juno's to name — refused",
          refused(r) and row is None, r)

    knock = await srv._serve(JUNO, {"op": "inbox.request", "params": {
        "message_kind": "endpoint_request", "subject": "Connection request", "body": "hi"}})
    n_knock = await pool.fetchval(
        "SELECT count(*) FROM inbox_messages WHERE from_entity_id = $1 AND message_kind = 'endpoint_request'",
        JUNO)
    check("B9 direct: the blocked Host's connection request is refused and no card is written",
          refused(knock) and n_knock == 0, knock)
    r, row = await relay(JUNO, kind="endpoint_request", with_slip=False)
    check("B10 relay: the blocked Host's connection request is refused", refused(r) and row is None, r)

    carried = {"contract_id": "c-back", "terms": {"issuer": JUNO, "grantee": ME,
                                                   "resource": "dm:contact", "allowed_ops": ["message"]}}
    r = await srv._serve(JUNO, {"op": "contact.slip", "slip": msg_slip[JUNO],
                                "params": {"contact_slip": carried, "message_id": str(uuid.uuid4())}})
    held = await pool.fetchval("SELECT count(*) FROM held_slips WHERE peer_entity_id = $1", JUNO)
    check("B11 direct: the blocked Host's reciprocal slip is refused and not kept",
          refused(r) and held == 0, (r, held))

    app_slip = slip(JUNO, ["app:veterans"], "app:veterans")
    mid = str(uuid.uuid4())
    r = await srv._serve(JUNO, {"op": "app.deliver", "slip": app_slip, "params": {
        "app_id": "veterans", "action": "msg", "payload": {"x": 1}, "message_id": mid}})
    got = await pool.fetchval("SELECT count(*) FROM peer_app_inbox WHERE message_id = $1::uuid", mid)
    check("B12 direct: the blocked Host's app delivery is refused and not stored",
          refused(r) and got == 0, (r, got))
    grant = {"contract_id": "c-app", "terms": {"issuer": JUNO, "grantee": ME, "resource": "app:veterans",
                                               "allowed_ops": ["app:veterans"]}}
    r = await srv._serve(JUNO, {"op": "app.grant", "slip": msg_slip[JUNO],
                                "params": {"app_id": "veterans", "app_slip": grant}})
    kept = await pool.fetchval("SELECT count(*) FROM peer_app_slips WHERE peer_entity_id = $1", JUNO)
    check("B13 direct: the blocked Host's app grant is refused and not kept",
          refused(r) and kept == 0, (r, kept))

    # A group Juno and this Host are both in: its traffic is the roster's.
    gid = f"grp-{JUNO}-" + uuid.uuid4().hex
    await pool.execute("INSERT INTO groups (group_id, owner_entity_id, name) VALUES ($1, $2, 'g')", gid, JUNO)
    for m in (JUNO, ME):
        await pool.execute("INSERT INTO group_members (group_id, member_entity_id, role) "
                           "VALUES ($1, $2, 'member')", gid, m)
    gslip = slip(JUNO, ["app:veterans"], f"group:{gid}:app:veterans")
    mid = str(uuid.uuid4())
    r = await srv._serve(JUNO, {"op": "app.deliver", "slip": gslip, "params": {
        "app_id": "veterans", "action": "msg", "payload": {"x": 2}, "message_id": mid}})
    check("B14 direct: a group-scoped app delivery is left to the group's roster (still stored)",
          admitted(r), r)

    # An app grant's carried terms are the sender's word (app_lane.app_grant
    # checks shape, not signature), so a group in them counts only when the
    # roster holds the sender.
    def group_grant(group_id):
        return {"contract_id": "c-g-" + uuid.uuid4().hex[:8], "terms": {
            "issuer": JUNO, "grantee": ME, "resource": f"group:{group_id}:app:veterans",
            "allowed_ops": ["app:veterans"]}}

    not_ours = f"grp-{ZED}-" + uuid.uuid4().hex
    r = await srv._serve(JUNO, {"op": "app.grant", "slip": msg_slip[JUNO],
                                "params": {"app_id": "veterans", "app_slip": group_grant(not_ours)}})
    kept = await pool.fetchval("SELECT count(*) FROM peer_app_slips WHERE peer_entity_id = $1", JUNO)
    check("B14a direct: the blocked Host's app grant naming a group it is not in is refused and not kept",
          refused(r) and kept == 0, (r, kept))
    r = await srv._serve(JUNO, {"op": "app.grant", "slip": msg_slip[JUNO],
                                "params": {"app_id": "veterans", "app_slip": group_grant(gid)}})
    check("B14b direct: its grant for a group the roster holds it in is left to the roster (stored)",
          admitted(r), r)
    await pool.execute("DELETE FROM peer_app_slips WHERE peer_entity_id = $1", JUNO)

    await pool.execute(
        "INSERT INTO network_machine_offers (direction, peer_entity_id, state) VALUES ('out', $1, 'open')",
        JUNO)
    r = await srv._serve(JUNO, {"op": "machine.token", "params": {}})
    left = await pool.fetchval("SELECT state FROM network_machine_offers WHERE peer_entity_id = $1", JUNO)
    check("B15 direct: the blocked Host is minted no machine token, and our open request is untouched",
          refused(r) and left == "open", (r, left))

    await pool.execute(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata) "
        "VALUES ($1, 'shared context', 'cited_fact', 'active', $2::jsonb)",
        ME, json.dumps({"source_context": {"kind": "thread", "ref": "t-shared"}}))
    r = await MH.handle_context_fulfill({"resource": "thread:t-shared", "action": "read", "grantee": JUNO})
    check("B16 relay: the blocked Host reads nothing through context.fulfill",
          r.get("ok") is False and r.get("blocked") is True and not r.get("entries"), r)
    r = await MH.handle_context_fulfill({"resource": "thread:t-shared", "action": "read", "grantee": ZED})
    check("B17 relay: a Host that is not blocked still reads what it was granted",
          r.get("ok") is True and r.get("count") == 1, r)

    r = await MH.handle_inbox_append({
        "direction": "outbound", "outbound_to_entity_id": JUNO, "from_entity_id": ME,
        "subject": "Direct message", "body": "our own copy", "message_kind": "message"})
    check("B18 our own outbound copy to a blocked Host is still written", admitted(r), r)
    r, row = await direct(ZED)
    check("B19 direct: another Host is untouched by Juno's block", admitted(r) and row is not None, r)

    # ── C. block the person ────────────────────────────────────────────────
    print("\n── C. a blocked person ──")
    await set_status(juno_id, "active")
    await set_status(ana_id, "blocked")
    r, row = await direct(JUNO, ANA)
    check("C1 direct: the blocked person's message is refused", refused(r) and row is None, r)
    r, row = await relay(JUNO, ANA)
    check("C2 relay: the blocked person's message is refused", refused(r) and row is None, r)
    r, row = await direct(ZED, ANA)
    check("C3 direct: refused whichever Host carries it", refused(r) and row is None, r)
    r, row = await relay(ZED, ANA)
    check("C4 relay: refused whichever Host carries it", refused(r) and row is None, r)
    r, row = await direct(JUNO)
    check("C5 direct: blocking Ana blocks her Host — Juno's own message is refused",
          refused(r) and row is None, r)
    r, row = await relay(JUNO)
    check("C6 relay: …and on the relay", refused(r) and row is None, r)
    r, row = await relay(JUNO, BOB)
    check("C7 relay: another person the platform checked runs Juno still arrives",
          admitted(r) and row is not None and row["from_user_email"] == BOB, (r, row))
    listed = await C.list_contacts_service(pool)
    jrow = next(c for c in listed["contacts"] if c["handle"] == "juno")
    check("C8 the contacts list names Ana on Juno's row, whose own status is unchanged",
          jrow.get("blocked_through") == ["Ana"] and jrow["status"] == "active", jrow)
    zrow = next(c for c in listed["contacts"] if c["handle"] == "zed")
    check("C9 …and nothing on a Host no blocked person is reached through",
          zrow.get("blocked_through") == [], zrow)

    # ── D. unblock the person ──────────────────────────────────────────────
    print("\n── D. unblocking the person lifts the Host block that came from them ──")
    await set_status(ana_id, "active")
    r, row = await direct(JUNO)
    check("D1 direct: Juno's own message is admitted again", admitted(r) and row is not None, r)
    r, row = await relay(JUNO)
    check("D2 relay: …and on the relay", admitted(r) and row is not None, r)
    r, row = await direct(JUNO, ANA)
    check("D3 direct: Ana's message is admitted again", admitted(r) and row is not None, r)

    # ── E. a Host blocked directly stays blocked ───────────────────────────
    print("\n── E. a Host blocked directly stays blocked when the person is unblocked ──")
    await set_status(juno_id, "blocked")
    await set_status(ana_id, "blocked")
    await set_status(ana_id, "active")
    r, row = await direct(JUNO)
    check("E1 direct: Juno, blocked directly, is still refused after Ana is unblocked",
          refused(r) and row is None, r)
    r, row = await relay(JUNO)
    check("E2 relay: …and on the relay", refused(r) and row is None, r)
    r, row = await direct(JUNO, ANA)
    check("E3 direct: Ana, unblocked, arrives through her blocked Host",
          admitted(r) and row is not None, r)
    await set_status(juno_id, "active")
    r, row = await direct(JUNO)
    check("E4 direct: unblocking Juno itself admits it", admitted(r) and row is not None, r)

    # ── F. two blocked people on one Host ──────────────────────────────────
    print("\n── F. two blocked people on one Host ──")
    bob_id = await add(BOB, "human", JUNO, "Bob")
    await set_status(ana_id, "blocked")
    await set_status(bob_id, "blocked")
    await set_status(ana_id, "active")
    r, row = await direct(JUNO)
    check("F1 Juno stays blocked while Bob is", refused(r) and row is None, r)
    await set_status(bob_id, "active")
    r, row = await direct(JUNO)
    check("F2 …and opens once both are lifted", admitted(r) and row is not None, r)

    # ── G. a person whose Host is unknown ──────────────────────────────────
    print("\n── G. a person whose Host is unknown ──")
    await set_status(cy_id, "blocked")
    r, row = await direct(ZED, CY)
    check("G1 direct: Cy's message is refused by address", refused(r) and row is None, r)
    r, row = await relay(JUNO, CY)
    check("G2 relay: refused by address on any Host", refused(r) and row is None, r)
    r, row = await direct(ZED)
    check("G3 no Host is blocked with them: Zed's own message still arrives",
          admitted(r) and row is not None, r)
    await set_status(cy_id, "active")

    # ── H. a check that cannot run ─────────────────────────────────────────
    print("\n── H. a check that cannot run refuses, and says try again ──")
    # Juno blocked, and the Host's pool closed under it: every read raises, as
    # it would with the database gone. `stored` still reads through the live one.
    await set_status(juno_id, "blocked")
    dead = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    await dead.close()
    brain_service._pool = dead
    try:
        r, row = await direct(JUNO)
        check("H1 direct: refused as retryable, nothing stored",
              r.get("ok") is False and r.get("retryable") is True and row is None, r)
        r, row = await relay(JUNO)
        check("H2 relay: refused as retryable, nothing stored",
              r.get("ok") is False and r.get("retryable") is True and row is None, r)
    finally:
        brain_service._pool = pool

    # ── I. a blocked Host's own model naming its person ────────────────────
    print("\n── I. the Host's model cannot carry its words past a block as its person ──")
    # The relay admits a person the platform checked runs the Host (B5), and
    # the bridge's check is only that: inbox.send keeps a from_user_email that
    # is an operator of the sending Host (bridge adapter
    # _enrich_and_verify_cross_host, _INBOX_SENDER_FIELDS). The inbox_send tool
    # hands the bridge the model's arguments. So the model on Juno naming Ana
    # (Juno's own person) would reach us through Juno's block. Drive the REAL
    # tool dispatcher with a model's call and capture what it gives the bridge.
    import mastercode.runtime.tools as T
    sent: list[tuple[str, dict]] = []

    class Bridge:
        _connected = True

        async def request_full(self, op, args):
            sent.append((op, dict(args)))
            return {"ok": True, "data": {"delivered": True}}

    async def allow(*_a, **_k):
        return True, None

    real_authorize, T._authorize_meta_tool = T._authorize_meta_tool, allow
    fake_main.get_bridge_client = lambda: Bridge()
    try:
        await T._execute_meta_tool("inbox_send", {
            "subject": "Connection request", "body": "the Host's model wrote this",
            "message_kind": "endpoint_request", "to_entity_id": ME,
            "from_user_email": ANA, "to_user_email": MY_PERSON}, JUNO, pool, None)
    finally:
        T._authorize_meta_tool = real_authorize
        fake_main.get_bridge_client = lambda: None
    op, handed = sent[-1] if sent else (None, {})
    check("I1 the inbox_send tool hands the bridge no person: from_user_email and "
          "to_user_email are dropped, the rest of the call is kept",
          op == "inbox.send" and "from_user_email" not in handed and "to_user_email" not in handed
          and handed.get("to_entity_id") == ME and handed.get("body") == "the Host's model wrote this",
          (op, handed))

    # What we receive: the bridge relays exactly what it was handed, the person
    # kept as it keeps a verified operator of the sender (Ana runs Juno).
    mid = str(uuid.uuid4())
    relayed = {"subject": handed.get("subject"), "body": handed.get("body"),
               "body_format": "text/plain", "message_kind": handed.get("message_kind"),
               "message_id": mid, "payload": {"from_entity_id": JUNO},
               "from_entity_id": JUNO, "sender_verified": True,
               "from_address": f"{JUNO}@entity.localhost"}
    if handed.get("from_user_email"):
        relayed["from_user_email"] = handed["from_user_email"]
        relayed["payload"]["from_user_email"] = handed["from_user_email"]
    r = await MH.handle_inbox_append(relayed)
    check("I2 Juno blocked: what its model sent through the tool is refused as Juno's own, "
          "not admitted as Ana's", refused(r) and await stored(mid) is None, r)

    brain_service._pool = None
    await pool.close()
    return 1 if FAILS else 0


print("\n── U. the Address book's line ──")
_node = shutil.which("node")
if _node:
    _r = subprocess.run([_node, "--test", "test/block-note.test.ts"], cwd=ROOT / "circles-web",
                        capture_output=True, text=True, timeout=120)
    check("U1 circles-web/test/block-note.test.ts passes", _r.returncode == 0 and "# fail 0" in _r.stdout,
          (_r.stdout + _r.stderr)[-600:])
elif os.environ.get("CSERIES_REQUIRE_NODE") == "1":
    # The gate lane has node (scripts/pre_merge_gates.sh sets CSERIES_REQUIRE_NODE),
    # so its absence there is a failure, the rule scripts/gate_toolchain.py keeps.
    check("U1 node on PATH to run circles-web/test/block-note.test.ts", False, "no node on PATH")
else:
    # Elsewhere a missing node is a SKIP, said loudly and never counted as a pass.
    print("  SKIP U1 circles-web/test/block-note.test.ts: no node on PATH")

with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
rc = rc or (1 if FAILS else 0)
print(f"\n{CHECKS} checks, {len(FAILS)} failed")
for f in FAILS:
    print("  - " + f)
print("RESULT: ALL PASS" if not FAILS else f"RESULT: {len(FAILS)} FAILED")
sys.exit(rc)
