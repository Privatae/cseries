#!/usr/bin/env python3
"""Gate — a person claim the Host cannot verify is the sending Host's own
message, never another person's chat, against
REAL Postgres, on three Hosts.

THE HOLE. On the direct Host-to-Host lane the receiving Host took the sending
Host's word for which person wrote a message (from_user_email), and refused
the claim only when our contacts named a DIFFERENT Host for that person. With
no contact, or a contact that names no Host, any slip-holding Host could
deliver a message "from ana@" that landed in me@'s People chat with Ana,
which is keyed on the two addresses alone. An earlier fix kept such a row out of the
Host's model (circles_reading.visible_sql); the operator's own view still
showed it as Ana's.

THE RULE NOW (circles_reading.person_checked_sql, peer/handlers.inbox_deliver):
a person's message counts as theirs when the relay carried it (the platform
checked the person runs the sending Host) or our contact places that person on
the sending Host. The direct lane files anything else as the sending Host's own
message, in our conversation with that Host, carrying the address it named.
Every People reader applies the same predicate at read time, so a row filed
before this change stops showing as the person's. Nothing stored is rewritten.

Hosts: A (1, "Ivy", me@ and carol@), B (122, "Juno", Ana's), N (150, "Nova",
Dora's, for first contact). Each has its own database; brain_service's pool and
entity id are switched to whichever Host acts, and the direct lane runs each
receiver's real PeerServer with a real signed slip. Z (140, "Zed") holds a slip
from A and has no database: it is only ever a sender. The relay is the bridge's
_enrich_and_verify_cross_host written out (the platform bridge's adapter.py):
the person checked against the sending Host's operators, the nested payload
passed through less the keys the bridge stamps, then from_display_name,
from_entity_uuid and from_entity_id stamped, delivered to the receiver's
inbox.append (memory_handlers.handle_relay_inbox_append, as server/main.py
registers it).

Every check is tagged. [new] tests behaviour this change adds and fails on main
d042b0e61. [guard] pins behaviour main already has, which this change must not
break. [refusal] pins the refusal of a person-to-person claim the receiver
cannot place on the sending Host.

  S  a spoofed claim: delivered as Z's own message in A's conversation with Z,
     saying which address it named; never in me@'s chat with Ana, nor a People
     thread of its own for an address with no contact or no Host
  V  a verified claim (the contact names that Host) lands in Ana's chat
  R  a relay-checked person lands, even from a Host no contact names, and is
     marked as checked; a relay sender cannot plant either mark
  O  rows filed before this change: an old spoofed row no longer shows as
     Ana's anywhere a person's chat is read (the thread, the list, the unread
     count, Ask Ivy, the Host's own reader), and stays stored; an old relay
     row still shows
  F  first contact: me@ asks Dora by email, Dora accepts, and her first
     message on the direct lane lands in me@'s chat with her

Run (needs docker for a throwaway cseries-postgres, or TEST_PG_DSN):
    APP_ROOT=<repo> python3 scripts/test_people_unverified_claims_pg.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import os
import pathlib
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
          "this gate needs real databases to prove what each Host files and shows.")
    sys.exit(2)

FAILS: list[str] = []
CHECKS = 0


def check(label: str, cond, extra: object = "") -> None:
    global CHECKS
    CHECKS += 1
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + label
          + (("\n        " + str(extra)[:900]) if extra and not ok else ""))
    if not ok:
        FAILS.append(label)


async def section(name, fn):
    """One lettered section; an exception fails that section and the run goes
    on, so a tree without the change shows every part it lacks."""
    print(f"\n── {name} ──")
    try:
        await fn()
    except Exception as e:  # noqa: BLE001 — reported as the section's failure
        import traceback
        traceback.print_exc()
        check(f"{name.split()[0]} ran to the end", False, f"{type(e).__name__}: {e}")


A, B, N, Z, BENS = 1, 122, 150, 140, 130
ME, CAROL, ANA, DORA, BEN = ("me@example.com", "carol@example.com", "ana@example.com",
                             "dora@example.com", "ben@example.com")
EVE, PAT = "eve@example.com", "pat@example.com"
NAMES = {A: "Ivy", B: "Juno", N: "Nova", Z: "Zed", BENS: "Cato"}
#: Who runs which Host, as the platform's operator_entities would say.
#: Pat runs on Juno too, but A's contact for Pat was added by hand with no Host.
OPERATORS = {A: [ME, CAROL], B: [ANA, PAT], N: [DORA], Z: ["zed@example.com"], BENS: [BEN]}
#: The claimed-address key, spelled out so this runs on main (which has no constant).
CLAIMED = "claimed_from_user_email"

KEY = Ed25519PrivateKey.generate()
KID = "test-key-1"


def slip(grantee: int, issuer: int, ops=("message",), resource="dm:contact") -> dict:
    from server.peer.slip_verify import canonical_terms
    now = dt.datetime.now(dt.timezone.utc)
    cid = "c-" + uuid.uuid4().hex[:12]
    terms = {"contract_id": cid, "key_id": KID, "issuer": issuer, "grantee": grantee,
             "resource": resource, "allowed_ops": list(ops),
             "issued_at": (now - dt.timedelta(minutes=1)).isoformat(),
             "expires_at": (now + dt.timedelta(hours=1)).isoformat()}
    sig = base64.b64encode(KEY.sign(canonical_terms(terms))).decode()
    return {"contract_id": cid, "key_id": KID, "terms": terms, "signature": sig}


class Trust:
    """The pinned bridge key and an empty revocation list — PeerTrust once synced."""
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


def request(body=None, headers=None, method="GET", query: str = ""):
    from starlette.requests import Request
    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": "/", "headers": hdrs,
                    "query_string": query.encode()}, receive)


def err(e) -> dict:
    d = e.detail if isinstance(e.detail, dict) else {}
    inner = d.get("error") if isinstance(d.get("error"), dict) else {}
    return {"status": e.status_code, "code": inner.get("code"),
            "message": inner.get("message") or (e.detail if isinstance(e.detail, str) else None)}


async def main(dsns: dict[int, str]) -> int:
    trust = Trust()
    fake_main = types.ModuleType("server.main")
    fake_main.app = None

    async def broadcast(_msg):
        return None
    fake_main.broadcast = broadcast
    fake_main._peer_trust_ref = trust
    sys.modules["server.main"] = fake_main
    import server
    server.main = fake_main

    from fastapi import HTTPException
    from server import brain_service as bs
    from server import circles_marks as CM
    from server import circles_reading as CR
    from server.bridge import memory_handlers as MH
    from server.peer import handlers as H
    from server.peer import peer_client as PC
    from server.peer.peer_server import PeerServer
    from server.routes import circles as C
    from server.routes import contacts as CT
    from server.routes import messages as MSG
    from server.routes.messages import dm_thread_id, people_thread_id

    pools = {h: await asyncpg.create_pool(d, min_size=1, max_size=4) for h, d in dsns.items()}
    srv = {h: PeerServer(trust=trust, handlers=H.PEER_HANDLERS, my_entity_id=h) for h in pools}

    def use(h):
        bs._pool, bs._entity_id = pools.get(h), h
        os.environ["ENTITY_OPERATOR_EMAILS"] = ",".join(OPERATORS[h])

    @contextlib.contextmanager
    def as_host(h):
        saved = (bs._pool, bs._entity_id, os.environ.get("ENTITY_OPERATOR_EMAILS"))
        use(h)
        try:
            yield pools.get(h)
        finally:
            bs._pool, bs._entity_id = saved[0], saved[1]
            os.environ["ENTITY_OPERATOR_EMAILS"] = saved[2] or ""

    for h in pools:
        for e, name in NAMES.items():
            await pools[h].execute(
                "INSERT INTO entity_registry (id, display_name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name", e, name)

    async def contact(h, handle, kind, peer, name):
        await pools[h].execute(
            "INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id) "
            "VALUES ($1, $2, $3, 'active', $4)", handle, name, kind, peer)
    await contact(A, "juno", "host", B, "Juno")
    await contact(A, "zed", "host", Z, "Zed")
    await contact(A, ANA, "human", B, "Ana Ruiz")
    await contact(A, PAT, "human", None, "Pat")          # added by hand: no Host recorded
    await contact(B, "ivy", "host", A, "Ivy")
    await contact(B, ME, "human", A, "Me")

    async def hold(h, peer):
        s = slip(grantee=h, issuer=peer)
        await pools[h].execute(
            "INSERT INTO held_slips (peer_entity_id, slip, contract_id, expires_at, updated_at) "
            "VALUES ($1, $2::jsonb, $3, $4::timestamptz, now()) ON CONFLICT (peer_entity_id) DO NOTHING",
            peer, json.dumps(s), s["contract_id"], dt.datetime.fromisoformat(s["terms"]["expires_at"]))
    await hold(A, B)
    await hold(B, A)

    # ── the direct lane, and the relay ─────────────────────────────────────
    lane = {"mode": "direct", "relayed": [], "direct": []}

    async def send_to_peer(peer, op, s, params, *, timeout=10.0):
        if lane["mode"] == "relay" or peer not in pools:
            raise PC.PeerUnavailable(f"peer {peer} unreachable (test: {lane['mode']})")
        sender = bs._entity_id
        with as_host(peer):
            resp = await srv[peer]._serve(sender, {"op": op, "slip": s, "params": params})
        # What the direct lane answered, so a check can tell "refused, then relayed"
        # from "never tried".
        lane["direct"].append((sender, peer, op, resp.get("ok"), resp.get("error")))
        return resp
    PC.send_to_peer = send_to_peer

    async def as_peer(sender: int, to: int, params: dict) -> dict:
        """`sender` delivers on the direct lane, presenting the slip `to` issued
        it. The receiver's answer, unwrapped as peer/deliver.py reads it."""
        with as_host(to):
            resp = await srv[to]._serve(sender, {"op": "inbox.deliver",
                                                 "slip": slip(grantee=sender, issuer=to),
                                                 "params": params})
        return {**(resp.get("data") or {}), "ok": resp.get("ok"), "error": resp.get("error")}

    # adapter.py _BRIDGE_STAMPED_INNER: never taken from the sender.
    STAMPED_INNER = ("from_user_email", "from_user_name", "authorized_via_slip",
                     "from_display_name", "from_entity_uuid", "from_entity_id", "from_handle")

    async def relay_deliver(sender: int, to: int, params: dict, kind: str = "message"):
        """_enrich_and_verify_cross_host + delivery to the receiver's inbox.append."""
        lane["relayed"].append((sender, to, kind))
        inner = {k: v for k, v in dict(params.get("payload") or {}).items() if k not in STAMPED_INNER}
        recip = {"subject": params.get("subject") or "Direct message", "body": params.get("body") or "",
                 "body_format": "text/plain", "message_kind": kind,
                 "message_id": params.get("message_id"), "thread_id": params.get("thread_id"),
                 "slip": params.get("slip"), "from_entity_id": sender, "sender_verified": True,
                 "from_address": f"{sender}@entity.localhost"}
        frm = (params.get("from_user_email") or "").strip().lower()
        if frm and frm in OPERATORS.get(sender, []):      # _person_on_host: else dropped
            recip["from_user_email"] = frm
            inner["from_user_email"] = frm
            inner["from_user_name"] = frm
        to_person = (params.get("to_user_email") or "").strip().lower()
        if to_person:
            if to_person not in OPERATORS.get(to, []):
                return None
            recip["to_user_email"] = to_person
        inner["from_display_name"] = NAMES[sender]
        inner["from_entity_uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"entity:{sender}"))
        inner["from_entity_id"] = sender
        recip["payload"] = inner
        if to not in pools:
            return None
        with as_host(to):
            # server/main.py registers the relay's own entry; main has none.
            r = await getattr(MH, "handle_relay_inbox_append", MH.handle_inbox_append)(recip)
            # A grant reciprocates in the background; finish it as this Host.
            while MH._background_tasks:
                await asyncio.gather(*list(MH._background_tasks), return_exceptions=True)
        return r if r.get("ok") else None

    def host_of(email: str) -> int | None:
        return next((h for h, ops in OPERATORS.items() if email in ops), None)

    async def relay(op, params):
        sender = int(bs._entity_id)
        if op == "contract.issue":
            return {"slip": slip(grantee=int(params["grantee_entity_id"]), issuer=sender)}
        if op == "contacts.request_by_email":
            target = host_of((params.get("to_user_email") or "").lower())
            if target is not None:
                await relay_deliver(sender, target, {
                    "subject": "Connection request", "body": params.get("body"),
                    "from_user_email": params.get("from_user_email"),
                    "to_user_email": params.get("to_user_email"),
                    "payload": {"kind": "human", "via": "email"}}, kind="endpoint_request")
            return {"sent": True}
        if op == "inbox.send":
            to = int(params["to_entity_id"])
            r = await relay_deliver(sender, to, params, kind=params.get("message_kind") or "message")
            return {"delivered": True, **r} if r else None
        if op in ("circles.send_dm", "circles.send_entity_dm"):
            to = int(params["target_id"])
            r = await relay_deliver(sender, to, params)
            if r is None:
                return None
            own = {"subject": "Direct message", "body": params["body"], "body_format": "text/plain",
                   "message_kind": "message", "direction": "outbound", "outbound_to_entity_id": to,
                   "from_entity_id": sender, "thread_id": params.get("thread_id"),
                   "payload": {"peer_entity_id": to, **dict(params.get("payload") or {})}}
            for k in ("from_user_email", "to_user_email"):
                if params.get(k):
                    own[k] = params[k]
            await MH.handle_inbox_append(own)
            return {"delivered": True, "to_entity_id": to, **r}
        return None

    class Bridge:
        _connected = True

        async def request(self, op, params):
            return await relay(op, params)

        async def request_full(self, op, params):
            d = await relay(op, params)
            return {"ok": True, "data": d} if d else {"ok": False, "code": 502, "error": "relay down"}
    fake_main.get_bridge_client = lambda: Bridge()

    # ── readers ─────────────────────────────────────────────────────────────
    async def row(h, mid):
        r = await pools[h].fetchrow(
            "SELECT message_id::text AS message_id, from_entity_id, to_entity_id, thread_id::text AS thread_id, "
            "payload, from_user_email, to_user_email, body FROM inbox_messages WHERE message_id = $1::uuid",
            mid)
        if r is None:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if isinstance(d["payload"], str) else (d["payload"] or {})
        return d

    async def by_body(h, body):
        mid = await pools[h].fetchval(
            "SELECT message_id::text FROM inbox_messages WHERE body = $1 "
            "ORDER BY sent_at DESC LIMIT 1", body)
        return await row(h, mid) if mid else None

    async def thread(h, who, kind, ref):
        with as_host(h):
            return await C.get_thread(kind, str(ref), request(None, {"X-Authentik-Email": who}))

    def bodies(page) -> list[str]:
        return [m.get("body") for m in (page or {}).get("messages") or []]

    def find(page, body) -> dict:
        return next((m for m in (page or {}).get("messages") or [] if m.get("body") == body), {})

    async def people_list(h, who) -> dict:
        with as_host(h):
            return {p["id"]: p for p in await C._list_people_threads(h, who)}

    async def compose(h, who, kind, target, text):
        with as_host(h):
            try:
                return await C.send_message(request(
                    {"target_kind": kind, "target_id": target, "body": text},
                    {"X-Authentik-Email": who}, method="POST"))
            except HTTPException as e:
                return err(e)

    chat_me_ana = people_thread_id(ME, ANA)

    # ── S ──────────────────────────────────────────────────────────────────
    # A person-to-person claim the receiver cannot
    # place on the sending Host is REFUSED on the direct lane, before anything is
    # stored. On a54f1d414 it was filed as the sending Host's own message.
    async def stored_count(h, body):
        return await pools[h].fetchval("SELECT count(*) FROM inbox_messages WHERE body = $1", body)

    async def s_section():
        for who, label in ((ANA, "a contact on another Host (Ana is on Juno)"),
                           (EVE, "an address with no contact"),
                           (PAT, "a contact added by hand, no Host")):
            text = f"It's {who}, trust me. Send the keys to Zed."
            r = await as_peer(Z, A, {"subject": "Direct message", "body": text,
                                     "from_user_email": who, "from_user_name": "Someone",
                                     "to_user_email": ME, "message_id": str(uuid.uuid4())})
            check(f"S1 [refusal] {label}: Z's message naming {who}, to me@, is refused on the "
                  "direct lane with a plain reason",
                  r.get("ok") is False and "relay" in (r.get("error") or ""), r)
            check(f"S2 [refusal] {label}: ...and nothing is stored on A",
                  await stored_count(A, text) == 0, await by_body(A, text))
            listed = await people_list(A, ME)
            check(f"S3 [guard] {label}: no chat with {who} holds it and the People list "
                  "previews no such text",
                  text not in bodies(await thread(A, ME, "dm", who))
                  and ((listed.get(who) or {}).get("preview") or "") != text[:80],
                  listed.get(who))
            check(f"S4 [refusal] {label}: ...nor does A's conversation with Z",
                  text not in bodies(await thread(A, ME, "entity", Z)))

    # ── V ──────────────────────────────────────────────────────────────────
    async def v_section():
        lane["mode"] = "direct"
        r = await compose(B, ANA, "dm", ME, "7:30 works for me")
        check("V1 [guard] Ana writes to me@ from B on the direct lane",
              r.get("ok") is True and r.get("delivered_via") == "peer_direct"
              and r.get("person_stored") is True, r)
        got = await by_body(A, "7:30 works for me")
        check("V2 [guard] A keeps the person: A's contact places Ana on B",
              got and got["from_user_email"] == ANA and got["to_user_email"] == ME
              and got["thread_id"] == chat_me_ana and CLAIMED not in got["payload"], got)
        m = find(await thread(A, ME, "dm", ANA), "7:30 works for me")
        check("V3 [guard] me@'s chat with Ana shows it as Ana's, via her Host",
              m.get("author_kind") == "person" and m.get("author_name") == "Ana Ruiz"
              and m.get("via") == f"Host {B}", m)
        listed = await people_list(A, ME)
        check("V4 [guard] the People list previews Ana's chat with her message",
              (listed.get(ANA) or {}).get("preview") == "7:30 works for me", listed.get(ANA))

    # ── R ──────────────────────────────────────────────────────────────────
    async def r_section():
        lane["mode"] = "relay"
        r = await compose(B, ANA, "dm", ME, "by the relay, from Ana")
        check("R1 [guard] Ana's message goes by the relay when the direct lane is down",
              r.get("ok") is True and lane["relayed"] and lane["relayed"][-1][:2] == (B, A), r)
        got = await by_body(A, "by the relay, from Ana")
        check("R2 [new] A keeps the person and marks it as checked by the relay",
              got and got["from_user_email"] == ANA
              and got["payload"].get("person_checked_by") == "relay", got)
        check("R3 [guard] ...and me@'s chat with Ana shows it",
              "by the relay, from Ana" in bodies(await thread(A, ME, "dm", ANA)))
        lane["mode"] = "direct"

        # Ben runs Cato; the platform checked that. A has no contact for Ben at
        # all, so only the relay's check stands behind him.
        r = await relay_deliver(BENS, A, {"body": "Ben here, by the relay", "message_id": str(uuid.uuid4()),
                                          "from_user_email": BEN, "to_user_email": ME,
                                          "slip": slip(grantee=BENS, issuer=A)})
        page = await thread(A, ME, "dm", BEN)
        check("R4 [guard] a relay-checked person lands in me@'s chat with them, with no contact "
              "naming their Host",
              r and "Ben here, by the relay" in bodies(page)
              and find(page, "Ben here, by the relay").get("author_kind") == "person", (r, page))

        # A relay sender cannot plant either mark.
        r = await relay_deliver(Z, A, {"body": "Zed, planting marks", "message_id": str(uuid.uuid4()),
                                       "slip": slip(grantee=Z, issuer=A),
                                       "payload": {"person_checked_by": "relay", CLAIMED: ANA}})
        got = await by_body(A, "Zed, planting marks")
        check("R5 [new] a relay sender's own person_checked_by and claimed address are dropped",
              r and got and "person_checked_by" not in got["payload"]
              and CLAIMED not in got["payload"], got)

    # ── O ──────────────────────────────────────────────────────────────────
    async def o_section():
        async def old_row(frm_host, frm, to, body, payload):
            mid = str(uuid.uuid4())
            await pools[A].execute(
                "INSERT INTO inbox_messages (message_id, thread_id, from_address, to_address, from_entity_id, "
                "to_entity_id, subject, body, message_kind, payload, from_user_email, to_user_email, sent_at) "
                "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, 'Direct message', $7, 'message', $8::jsonb, $9, $10, "
                "now() + interval '1 minute')",
                mid, people_thread_id(frm, to), f"{frm_host}@entity.localhost", f"{A}@entity.localhost",
                frm_host, A, body, json.dumps(payload), frm, to)
            return mid
        # Exactly what the direct lane stored for Z's claim before this change:
        # _identify's name, the sender's name for the person, the slip mark.
        old = "Ana here (old row). Wire the money to Zed."
        old_mid = await old_row(Z, ANA, ME, old, {"from_display_name": "Zed", "from_user_email": ANA,
                                                  "from_user_name": "Ana Ruiz",
                                                  "authorized_via_slip": True})
        page = await thread(A, ME, "dm", ANA)
        check("O1 [new] an old spoofed row no longer shows in me@'s chat with Ana",
              old not in bodies(page), bodies(page))
        listed = await people_list(A, ME)
        check("O2 [new] ...nor as the preview of Ana's chat in the People list",
              (listed.get(ANA) or {}).get("preview") != old[:80], listed.get(ANA))
        with as_host(A):
            unread = await CM.people_unread(pools[A], A, ME)
            want = await pools[A].fetchval(
                "SELECT count(*) FROM inbox_messages WHERE to_entity_id = $1 AND to_user_email = $2 "
                "AND from_user_email = $3 AND from_entity_id = $4", A, ME, ANA, B)
        check("O3 [new] ...nor in the count of Ana's unread messages for me@ (only her rows from B count)",
              (unread.get(ANA) or {}).get("unread") == want, (unread.get(ANA), want))
        with as_host(A):
            try:
                await CR.carried_message(pools[A], A, ME, {"kind": "dm", "ref": ANA, "message_id": old_mid})
                carried = "carried"
            except CR.CarryError as e:
                carried = e.status
        check("O4 [new] ...and Ask Ivy cannot hand it to the Host as Ana's (404, like any message not in "
              "the chat)", carried == 404, carried)
        with as_host(A):
            await CR.set_on(pools[A], ME, ANA, True)
            svc = await MSG.thread_service(pools[A], A, chat_me_ana)
            await CR.set_on(pools[A], ME, ANA, False)
        got = {m["body"] for m in svc["messages"]}
        check("O5 [guard] with the reading switch on, the Host's reader shows Ana's rows but not the old "
              "spoofed one (the same predicate the operator's view uses)",
              "7:30 works for me" in got and "by the relay, from Ana" in got and old not in got, got)
        check("O6 [guard] the old row is still stored, unchanged (history is not rewritten)",
              (await row(A, old_mid) or {}).get("from_user_email") == ANA)

        only = "Eve (old row), nobody you know"
        await old_row(Z, EVE, ME, only, {"from_display_name": "Zed", "from_user_email": EVE})
        listed = await people_list(A, ME)
        check("O7 [new] an old spoofed row that was a person's only message opens no People thread",
              EVE not in listed or (listed[EVE].get("preview") or "") != only[:80], listed.get(EVE))

        # An old relay row: the bridge stamped from_entity_uuid, and A has no
        # contact naming Cato for Ben. The relay checked him; it still shows.
        relay_old = "Ben (old relay row)"
        await old_row(BENS, BEN, ME, relay_old, {"from_display_name": "Cato", "from_user_email": BEN,
                                                "from_user_name": BEN, "from_entity_id": BENS,
                                                "from_entity_uuid": str(uuid.uuid4()),
                                                "authorized_via_slip": True})
        check("O8 [guard] an old row the relay carried still shows in me@'s chat with that person",
              relay_old in bodies(await thread(A, ME, "dm", BEN)))

    # ── F ──────────────────────────────────────────────────────────────────
    async def f_section():
        lane["mode"] = "direct"
        with as_host(A):
            r = await CT.request_by_email_service(pools[A], email=DORA, note="Hi Dora", my_email=ME)
        req = await pools[N].fetchrow(
            "SELECT message_id::text AS mid, from_user_email, from_entity_id FROM inbox_messages "
            "WHERE message_kind = 'endpoint_request' ORDER BY sent_at DESC LIMIT 1")
        pending = await pools[A].fetchrow("SELECT status, peer_entity_id FROM contacts WHERE handle = $1", DORA)
        check("F1 [guard] me@ asks Dora by email: the request reaches Nova by the relay with me@ checked, "
              "and A holds a pending contact with no Host yet",
              r.get("ok") and req and req["from_user_email"] == ME and req["from_entity_id"] == A
              and pending and pending["status"] == "pending" and pending["peer_entity_id"] is None,
              (r, req, pending))
        with as_host(N):
            r = await CT.accept_request_service(pools[N], handle=ME, message_id=req["mid"],
                                                from_entity_id=A, kind="human",
                                                from_user_email=ME, my_email=DORA)
        a_dora = await pools[A].fetchrow("SELECT status, peer_entity_id FROM contacts WHERE handle = $1", DORA)
        n_me = await pools[N].fetchrow("SELECT status, peer_entity_id FROM contacts WHERE handle = $1", ME)
        held = {h: await pools[h].fetchval("SELECT count(*) FROM held_slips WHERE peer_entity_id = $1", p)
                for h, p in ((A, N), (N, A))}
        check("F2 [guard] Dora accepts: both contacts now name the other's Host (A's from the grant, Nova's "
              "from the request), and each Host holds the other's slip",
              r.get("ok") and r.get("grant_delivered") is True
              and a_dora and a_dora["peer_entity_id"] == N and a_dora["status"] == "active"
              and n_me and n_me["peer_entity_id"] == A and held == {A: 1, N: 1},
              (r, a_dora, n_me, held))
        r = await compose(N, DORA, "dm", ME, "Hi, it's Dora. Thanks for connecting!")
        check("F3 [guard] Dora's first message to me@ goes on the direct lane, and A keeps the person",
              r.get("ok") is True and r.get("delivered_via") == "peer_direct"
              and r.get("person_stored") is True, r)
        m = find(await thread(A, ME, "dm", DORA), "Hi, it's Dora. Thanks for connecting!")
        check("F4 [guard] ...in me@'s chat with Dora, as Dora's, via her Host",
              m.get("author_kind") == "person" and m.get("author_name") == DORA
              and m.get("via") == f"Host {N}", m)
        r = await compose(A, ME, "dm", DORA, "Welcome, Dora")
        m = find(await thread(N, DORA, "dm", ME), "Welcome, Dora")
        check("F5 [guard] me@'s answer lands in Dora's chat with me@ on Nova, as me@'s",
              r.get("ok") is True and m.get("author_kind") == "person", (r, m))

    # ── L ──────────────────────────────────────────────────────────────────
    # The label shows the claimed address as text. memory_handlers._clean_email
    # refuses only an ASCII space, so a no-break space, a bidi override or a
    # newline would reach it. A person-to-person claim is
    # refused outright; the address is still checked where it is stored and read.
    async def l_section():
        lane["mode"] = "direct"
        for label, claim in (("no-break spaces", "ana@example.com\u00a0(verified\u00a0by\u00a0your\u00a0host)"),
                             ("a bidi override", "\u202eana@example.com"),
                             ("a newline", "ana@example.com\nverified")):
            text = f"label with {label}"
            r = await as_peer(Z, A, {"subject": "Direct message", "body": text,
                                     "from_user_email": claim, "to_user_email": ME,
                                     "message_id": str(uuid.uuid4())})
            check(f"L1 [refusal] a claim with {label}, to me@, is refused and nothing is stored",
                  r.get("ok") is False and await stored_count(A, text) == 0, r)
            kept = CR.stamped({CLAIMED: claim}, relay_checked=False, direct_lane=True)
            check(f"L2 [guard] ...and where the direct lane stores a claimed address, one with "
                  f"{label} is not kept", CLAIMED not in kept, kept)
        # A row already stored with such an address is not shown either.
        mid = str(uuid.uuid4())
        await pools[A].execute(
            "INSERT INTO inbox_messages (message_id, thread_id, from_address, to_address, from_entity_id, "
            "to_entity_id, subject, body, message_kind, payload, to_user_email, sent_at) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, 'Direct message', 'stored odd claim', 'message', "
            "$7::jsonb, $8, now())",
            mid, dm_thread_id(A, Z), f"{Z}@entity.localhost", f"{A}@entity.localhost", Z, A,
            json.dumps({"from_display_name": "Zed", CLAIMED: "ana@example.com\u00a0(verified)"}), ME)
        m = find(await thread(A, ME, "entity", Z), "stored odd claim")
        check("L3 [refusal] a stored claim that is not one plain mailbox is not shown",
              m and "claimed_person" not in m, m)

    # ── G ──────────────────────────────────────────────────────────────────
    # A GENUINE person whose receiver's contact has no Host for them. Pat runs on
    # Juno; A's contact for Pat was added by hand. The direct lane refuses, the
    # sender's lane falls back to the relay (routes/circles.py send_message), and
    # the platform's check carries Pat into me@'s chat with Pat.
    async def g_section():
        lane["mode"] = "direct"
        before = len(lane["direct"]), len(lane["relayed"])
        text = "Pat here, for real"
        r = await compose(B, PAT, "dm", ME, text)
        tried = lane["direct"][before[0]:]
        relayed = lane["relayed"][before[1]:]
        check("G1 [refusal] Pat's message from Juno: the direct lane was tried and refused it",
              any(t[:3] == (B, A, "inbox.deliver") and t[3] is False for t in tried), tried)
        check("G2 [refusal] ...and the sender's lane fell back to the relay, which delivered it",
              r.get("ok") is True and r.get("delivered_via") != "peer_direct"
              and (B, A, "message") in relayed, (r, relayed))
        got = await by_body(A, text)
        check("G3 [refusal] A stores it as Pat's, to me@, marked as checked by the relay, once",
              got and got["from_user_email"] == PAT and got["to_user_email"] == ME
              and got["payload"].get("person_checked_by") == "relay"
              and await stored_count(A, text) == 1, got)
        m = find(await thread(A, ME, "dm", PAT), text)
        check("G4 [refusal] ...and me@'s chat with Pat shows it as Pat's",
              m.get("author_kind") == "person", m)
        own = await pools[B].fetchval(
            "SELECT count(*) FROM inbox_messages WHERE body = $1 AND from_entity_id = $2", text, B)
        check("G5 [guard] Juno keeps one copy of its own send", own == 1, own)

    # ── H ──────────────────────────────────────────────────────────────────
    # A person writing to the HOST itself (no addressee) is unchanged.
    async def h_section():
        lane["mode"] = "direct"
        text = "Zed's person, writing to Ivy"
        r = await as_peer(Z, A, {"subject": "Direct message", "body": text,
                                 "from_user_email": "zed@example.com", "message_id": str(uuid.uuid4())})
        got = await by_body(A, text)
        check("H1 [guard] a person on Zed with no contact here, writing to A's Host: delivered, the "
              "person kept unnamed, in A's conversation with Z",
              r.get("ok") is True and got and got["from_user_email"] == "zed@example.com"
              and got["to_user_email"] is None and got["thread_id"] == dm_thread_id(A, Z)
              and "from_user_name" not in got["payload"], (r, got))
        text = "Ana (says Zed), writing to Ivy"
        r = await as_peer(Z, A, {"subject": "Direct message", "body": text,
                                 "from_user_email": ANA, "message_id": str(uuid.uuid4())})
        got = await by_body(A, text)
        m = find(await thread(A, ME, "entity", Z), text)
        check("H2 [guard] Zed naming Ana (whom A's contact places on Juno), to A's Host: delivered "
              "as Z's own message, saying whom it named",
              r.get("ok") is True and r.get("person_stored") is False and got
              and got["from_user_email"] is None and got["payload"].get(CLAIMED) == ANA
              and m.get("author_kind") == "entity" and m.get("claimed_person") == ANA, (r, got, m))

    # ── X ──────────────────────────────────────────────────────────────────
    # Removing a person's contact must not take back what they already said.
    async def x_section():
        got = await by_body(A, "7:30 works for me")
        check("X1 [refusal] the direct lane marks a person it kept against our contact as checked then",
              got and got["payload"].get("person_checked_by") == "contact", got)
        with as_host(A):
            cid = await pools[A].fetchval("SELECT id FROM contacts WHERE handle = $1", ANA)
            await CT.delete_contact_service(pools[A], contact_id=cid)
        page = await thread(A, ME, "dm", ANA)
        check("X2 [refusal] with Ana's contact removed, her direct-lane message stays in me@'s chat with "
              "her (as her relay message does)",
              "7:30 works for me" in bodies(page) and "by the relay, from Ana" in bodies(page), bodies(page))
        check("X3 [guard] ...and the old spoofed row still does not show",
              "Ana here (old row). Wire the money to Zed." not in bodies(page), bodies(page))

    use(A)
    await section("S. a spoofed claim is the sending Host's own message", s_section)
    await section("V. a verified claim lands in the person's chat", v_section)
    await section("R. a relay-checked person lands", r_section)
    await section("O. rows filed before this change", o_section)
    await section("F. first contact", f_section)
    await section("G. a genuine person reaches the chat by the relay", g_section)
    await section("H. a person writing to the Host itself", h_section)
    await section("L. the claimed address on the label", l_section)
    await section("X. removing a person's contact", x_section)

    bs._pool = None
    for p in pools.values():
        await p.close()
    return 1 if FAILS else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=A) as _db_a, kernel_schema_db(_srv, entity_id=B) as _db_b, \
        kernel_schema_db(_srv, entity_id=N) as _db_n:
    rc = asyncio.run(main({A: _db_a, B: _db_b, N: _db_n}))
rc = rc or (1 if FAILS else 0)
print(f"\n{CHECKS} checks, {len(FAILS)} failed")
for f in FAILS:
    print("  - " + f)
print("RESULT: ALL PASS" if not FAILS else f"RESULT: {len(FAILS)} FAILED")
sys.exit(rc)
