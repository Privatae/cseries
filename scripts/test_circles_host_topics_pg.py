#!/usr/bin/env python3
"""Gate — topics between two Hosts (Circles cards), against REAL
Postgres, on both ways in.

Two Hosts share one
conversation; a topic is a named strand inside it (server/peer/topics.py): a
topic id and title in the message's nested payload, on the direct lane
(peer/handlers._ALLOWED_DELIVER_PAYLOAD_KEYS), on the relay (the bridge passes
the nested payload through) and in inbox_messages.payload. The receiver still
derives the pair's thread itself, now on the relay too, and a topic never
chooses it.

TWO HOSTS, TWO DATABASES. Host A (1) and Host B (9122) each get a fresh kernel
database, and brain_service's pool and entity id are switched to whichever
Host is acting, so a send runs A's real route, A's real direct-lane sender, and
B's real PeerServer with a real signed slip, storing into B's own database.
The relay is the bridge's op_circles_send_entity_dm and
_enrich_and_verify_cross_host written out: the sender's nested payload copied whole to the receiver and to
the sender's own copy, less the keys the bridge stamps. Hosts Z (130), OLD
(150), FRESH (160) and W (170) only send to A over its direct lane.

Every check is tagged. [new] tests behaviour this slice adds and fails on main
821180ca0. [guard] pins behaviour main already has, which a topic must not
break.

  T  a topic stays inside the derived pair thread, by the composer, on both
     lanes; a later message cannot rename it; the relay derives the thread
  P  a peer cannot use a topic to reach another pair: a topic id from another
     conversation lands in the sender's own; the model's reader is not
     reached; a send or a mark naming another pair's topic is refused;
     unchecked ids and titles are cleaned at storage
  O  a message without a topic files under the first thread ("original")
  V  an older Host's message reads normally, and what a newer Host sends an
     older one changes only by the nested payload
  K  a blocked Host's message is refused whatever topic it names, on both
     lanes; its person's still arrives, in the topic it names
  M  marks per topic: a topic's own mark, the whole conversation's mark, and
     the deck's unread as their sum
  R  the routes refuse what is not a topic
  S  the topic list's speed on a pair of 3,000 messages in 30 topics

Run (needs docker for a throwaway cseries-postgres, or TEST_PG_DSN):
    APP_ROOT=<repo> python3 scripts/test_circles_host_topics_pg.py
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
import time
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
          "this gate needs real databases to prove topics between two Hosts.")
    sys.exit(2)

FAILS: list[str] = []
CHECKS = 0


def check(label: str, cond, extra: object = "") -> None:
    global CHECKS
    CHECKS += 1
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + label
          + (("\n        " + str(extra)[:700]) if extra and not ok else ""))
    if not ok:
        FAILS.append(label)


async def section(name, fn):
    """One lettered section; an exception fails that section and the run
    goes on, so a tree without the feature shows every part it lacks."""
    print(f"\n── {name} ──")
    try:
        await fn()
    except Exception as e:  # noqa: BLE001 — reported as the section's failure
        check(f"{name.split()[0]} ran to the end", False, f"{type(e).__name__}: {e}")


A, B, Z, OLD, FRESH, W = 1, 9122, 130, 150, 160, 170
A_PERSON, B_PERSON = "me@example.com", "owner@juno.example"
ANA = "ana@example.com"           # a person A's contacts place on B
NAMES = {A: "Ivy", B: "Juno", Z: "Zed", OLD: "Old", W: "Wren"}
BLOCKED = "blocked by domain policy"

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


def request(body=None, headers=None, method="PUT", query: str = ""):
    from starlette.requests import Request
    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": "/", "headers": hdrs,
                    "query_string": query.encode()}, receive)


async def main(dsn_a: str, dsn_b: str) -> int:
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
    from server.bridge import memory_handlers as MH
    from server.peer import handlers as H
    from server.peer import peer_client as PC
    from server.peer.peer_server import PeerServer
    from server.routes import circles as C
    from server.routes import messages as MSG
    from server.routes.messages import dm_thread_id

    pools = {A: await asyncpg.create_pool(dsn_a, min_size=1, max_size=4),
             B: await asyncpg.create_pool(dsn_b, min_size=1, max_size=4)}
    operators = {A: A_PERSON, B: B_PERSON}
    srv = {h: PeerServer(trust=trust, handlers=H.PEER_HANDLERS, my_entity_id=h) for h in pools}

    def use(h):
        bs._pool, bs._entity_id = pools[h], h
        os.environ["ENTITY_OPERATOR_EMAILS"] = operators[h]

    @contextlib.contextmanager
    def as_host(h):
        saved = (bs._pool, bs._entity_id, os.environ.get("ENTITY_OPERATOR_EMAILS"))
        use(h)
        try:
            yield pools[h]
        finally:
            bs._pool, bs._entity_id = saved[0], saved[1]
            os.environ["ENTITY_OPERATOR_EMAILS"] = saved[2] or ""

    # ── each Host's records: names, contacts, the slips it holds ───────────
    for h, pool in pools.items():
        for e, name in NAMES.items():
            await pool.execute("INSERT INTO entity_registry (id, display_name) VALUES ($1, $2) "
                               "ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name",
                               e, name)

    async def contact(h, handle, kind, peer, name):
        return await pools[h].fetchval(
            "INSERT INTO contacts (handle, display_name, kind, status, peer_entity_id) "
            "VALUES ($1, $2, $3, 'active', $4) RETURNING id", handle, name, kind, peer)
    b_on_a = await contact(A, "juno", "host", B, "Juno")
    await contact(A, "zed", "host", Z, "Zed")
    await contact(A, ANA, "human", B, "Ana")
    await contact(B, "ivy", "host", A, "Ivy")

    async def hold(h, peer):
        """`h` holds the slip `peer` issued it (what h presents to message peer)."""
        s = slip(grantee=h, issuer=peer)
        await pools[h].execute(
            "INSERT INTO held_slips (peer_entity_id, slip, contract_id, expires_at, updated_at) "
            "VALUES ($1, $2::jsonb, $3, $4::timestamptz, now())",
            peer, json.dumps(s), s["contract_id"],
            dt.datetime.fromisoformat(s["terms"]["expires_at"]))
    await hold(A, B)
    await hold(B, A)
    # The slips A issued the Hosts that send to it.
    to_a = {p: slip(grantee=p, issuer=A) for p in (B, Z, OLD, FRESH, W)}

    # ── the direct lane between A and B, and the relay ─────────────────────
    lane = {"mode": "direct", "wires": []}

    async def send_to_peer(peer, op, s, params, *, timeout=10.0):
        lane["wires"].append(json.loads(json.dumps(params)))
        if lane["mode"] == "relay" or peer not in pools:
            raise PC.PeerUnavailable(f"peer {peer} unreachable (test: {lane['mode']})")
        sender = bs._entity_id
        with as_host(peer):
            return await srv[peer]._serve(sender, {"op": op, "slip": s, "params": params})
    PC.send_to_peer = send_to_peer

    # adapter.py:5949-5951, the keys the bridge stamps into the nested payload.
    STAMPED_INNER = ("from_user_email", "from_user_name", "authorized_via_slip",
                     "from_display_name", "from_entity_uuid", "from_entity_id", "from_handle")

    class Bridge:
        _connected = True

        async def request(self, op, params):
            if op != "circles.send_entity_dm":
                return None
            sender, to = int(bs._entity_id), int(params["target_id"])
            # op_circles_send_entity_dm (adapter.py:6213-6230) then
            # _enrich_and_verify_cross_host (:5976-6083), the platform's person
            # check taken as passed.
            inner = {k: v for k, v in dict(params.get("payload") or {}).items()
                     if k not in STAMPED_INNER}
            recip = {"subject": params.get("subject") or "Direct message",
                     "body": params["body"], "body_format": "text/plain",
                     "message_kind": "message", "thread_id": params.get("thread_id"),
                     "message_id": params.get("message_id"), "slip": params.get("slip"),
                     "from_entity_id": sender, "sender_verified": True,
                     "from_address": f"{sender}@entity.localhost", "slip_authorized": True}
            if params.get("from_user_email"):
                recip["from_user_email"] = params["from_user_email"]
                inner["from_user_email"] = params["from_user_email"]
                inner["from_user_name"] = params["from_user_email"]
            inner["authorized_via_slip"] = {"contract_id": (params.get("slip") or {}).get("contract_id")}
            inner["from_display_name"] = NAMES[sender]
            inner["from_entity_id"] = sender
            recip["payload"] = inner
            if to not in pools:
                return None
            with as_host(to):
                r = await MH.handle_inbox_append(recip)
            if not r.get("ok"):
                return None
            # The sender's own copy (adapter.py:6243-6261).
            own_inner = {"peer_entity_id": to, **dict(params.get("payload") or {})}
            own = {"subject": recip["subject"], "body": params["body"], "body_format": "text/plain",
                   "message_kind": "message", "direction": "outbound", "outbound_to_entity_id": to,
                   "from_entity_id": sender, "sender_verified": True,
                   "thread_id": params.get("thread_id"), "payload": own_inner}
            if params.get("from_user_email"):
                own["from_user_email"] = params["from_user_email"]
            await MH.handle_inbox_append(own)
            return {"delivered": True, "to_entity_id": to, **r}
    fake_main.get_bridge_client = lambda: Bridge()

    # ── ways in, crafted as each lane hands them over ──────────────────────
    async def direct_into_a(peer, *, payload=None, thread_id=None, person=None, body=None):
        """inbox.deliver to A over its direct lane from `peer`, with the slip A issued."""
        mid = str(uuid.uuid4())
        params = {"subject": "Direct message", "body": body or f"from {peer} {mid}",
                  "body_format": "text/plain", "thread_id": thread_id, "message_id": mid}
        if payload is not None:
            params["payload"] = payload
        if person:
            params["from_user_email"] = person
        with as_host(A):
            r = await srv[A]._serve(peer, {"op": "inbox.deliver", "slip": to_a[peer], "params": params})
        return r, await row(A, mid)

    async def relay_into_a(peer, *, payload=None, thread_id=None, person=None, body=None,
                           to_person=None):
        """inbox.append to A as the bridge hands it over (the Bridge above)."""
        mid = str(uuid.uuid4())
        inner = {k: v for k, v in dict(payload or {}).items() if k not in STAMPED_INNER}
        inner.update({"from_display_name": NAMES.get(peer, f"Host {peer}"), "from_entity_id": peer})
        p = {"subject": "Direct message", "body": body or f"relayed from {peer} {mid}",
             "body_format": "text/plain", "message_kind": "message", "message_id": mid,
             "thread_id": thread_id, "slip": to_a[peer], "from_entity_id": peer,
             "sender_verified": True, "from_address": f"{peer}@entity.localhost",
             "payload": inner}
        if person:
            p["from_user_email"] = person
            inner["from_user_email"] = person
        if to_person:
            p["to_user_email"] = to_person
        with as_host(A):
            r = await MH.handle_inbox_append(p)
        return r, await row(A, mid)

    async def row(h, mid):
        r = await pools[h].fetchrow(
            "SELECT message_id, from_entity_id, to_entity_id, thread_id::text AS thread_id, payload, "
            "from_user_email, body FROM inbox_messages WHERE message_id = $1::uuid", mid)
        if r is None:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if isinstance(d["payload"], str) else (d["payload"] or {})
        return d

    async def newest(h, frm, to):
        r = await pools[h].fetchval(
            "SELECT message_id::text FROM inbox_messages WHERE from_entity_id = $1 AND to_entity_id = $2 "
            "ORDER BY sent_at DESC, message_id DESC LIMIT 1", frm, to)
        return await row(h, r) if r else None

    async def compose(h, peer, text, **extra):
        """POST /api/circles/messages as `h`'s person at the keyboard."""
        with as_host(h):
            try:
                return await C.send_message(request(
                    {"target_kind": "entity", "target_id": peer, "body": text, **extra},
                    {"X-Authentik-Email": operators[h]}, method="POST"))
            except HTTPException as e:
                return {"status": e.status_code, "detail": e.detail}

    async def topics(h, peer):
        """GET /api/circles/thread/entity/{peer}/topics as `h`, or an error dict."""
        with as_host(h):
            try:
                return (await C.list_circles_topics("entity", str(peer)))["topics"]
            except Exception as e:  # noqa: BLE001 — a tree without the route
                return {"error": f"{type(e).__name__}: {e}"}

    def topic(items, tid):
        return next((t for t in items if t.get("id") == tid), None) if isinstance(items, list) else None

    async def read(h, peer, topic_id=None):
        """GET /api/circles/thread/entity/{peer}[?topic=] as `h`."""
        q = "" if topic_id is None else f"topic={topic_id}"
        with as_host(h):
            try:
                return await C.get_thread("entity", str(peer), request(None, {}, "GET", q))
            except HTTPException as e:
                return {"status": e.status_code, "detail": e.detail, "messages": []}

    async def mark(h, peer, topic_id=None, kind="entity"):
        q = "" if topic_id is None else f"topic={topic_id}"
        with as_host(h):
            try:
                return await C.mark_circles_seen(
                    kind, str(peer), request(None, {"X-Authentik-Email": operators[h]}, "PUT", q))
            except HTTPException as e:
                return {"status": e.status_code, "detail": e.detail}

    async def deck_entry(h, peer):
        with as_host(h):
            pl = await C.list_conversations(request(None, {"X-Authentik-Email": operators[h]}, "GET"))
        return next((i for i in pl["entities"] if str(i["id"]) == str(peer)), None)

    def bodies(page):
        return [m.get("body") for m in (page or {}).get("messages") or []]

    async def tick():
        await asyncio.sleep(0.02)

    pair_ab = dm_thread_id(A, B)
    ids: dict = {}

    # ── T ──────────────────────────────────────────────────────────────────
    async def t_section():
        lane["mode"] = "direct"
        lane["wires"].clear()
        r = await compose(A, B, "Dinner Thursday?", new_topic="  Thursday\n dinner ")
        tp = r.get("topic") or {}
        ids["t"] = tp.get("id")
        check("T1 [new] A's person starts a topic with B (\"+\" is the send of its first message): "
              "a fresh topic id, the title cleaned to one line, sent on the direct lane",
              r.get("ok") is True and r.get("delivered_via") == "peer_direct"
              and ids["t"] and str(uuid.UUID(ids["t"])) == ids["t"] and tp.get("title") == "Thursday dinner",
              r)
        wire = lane["wires"][-1] if lane["wires"] else {}
        check("T2 [new] the wire carries the topic in the nested payload and the pair's thread id",
              ids["t"] and wire.get("payload") == {"topic_id": ids["t"], "topic_title": "Thursday dinner"}
              and wire.get("thread_id") == pair_ab, wire)
        on_b = await newest(B, A, B)
        check("T3 [new] B stored it in the pair's derived thread, under the topic",
              ids["t"] and on_b and on_b["thread_id"] == pair_ab
              and on_b["payload"].get("topic_id") == ids["t"]
              and on_b["payload"].get("topic_title") == "Thursday dinner", on_b)
        ours = await newest(A, A, B)
        check("T4 [new] A's own copy carries the same topic, in the same thread",
              ids["t"] and ours and ours["thread_id"] == pair_ab
              and ours["payload"].get("topic_id") == ids["t"], ours)
        tb, ta = await topics(B, A), await topics(A, B)
        tt = topic(tb, ids["t"]) or {}
        check("T5 [new] B lists the topic with A after the original conversation, started by the peer",
              isinstance(tb, list) and tb[0]["id"] == "original"
              and tb[0]["title"] == "The original conversation"
              and tt.get("title") == "Thursday dinner" and tt.get("started_by") == "peer"
              and tt.get("messages") == 1, tb)
        check("T6 [new] ...and A lists it as started by itself",
              (topic(ta, ids["t"]) or {}).get("started_by") == "self", ta)

        # B answers in the topic, over the RELAY this time.
        lane["mode"] = "relay"
        r = await compose(B, A, "7:30 works", topic_id=ids["t"])
        lane["mode"] = "direct"
        on_a = await newest(A, B, A)
        check("T7 [new] B's answer in the topic went by the relay and A filed it in the topic, "
              "in the derived pair thread",
              r.get("ok") is True and r.get("delivered") is True and ids["t"]
              and on_a and on_a["payload"].get("topic_id") == ids["t"] and on_a["thread_id"] == pair_ab,
              (r, on_a))
        b_own = await newest(B, B, A)
        check("T8 [new] B's own copy (the bridge's) carries the topic too",
              ids["t"] and b_own and b_own["payload"].get("topic_id") == ids["t"], b_own)
        page = await read(A, B, ids["t"])
        check("T9 [new] A reads the topic: both messages, oldest first, each naming the topic",
              ids["t"] and page.get("topic_id") == ids["t"]
              and bodies(page) == ["Dinner Thursday?", "7:30 works"]
              and all(m.get("topic_id") == ids["t"] for m in page["messages"])
              and page.get("total") == 2, page)
        orig = await read(A, B, "original")
        check("T10 [new] ...and neither is in the original conversation",
              "status" not in orig and orig.get("topic_id") == "original"
              and not ({"Dinner Thursday?", "7:30 works"} & set(bodies(orig))), orig)
        whole = await read(A, B)
        check("T11 [guard] the whole conversation (no topic named) still reads every message",
              {"Dinner Thursday?", "7:30 works"} <= set(bodies(whole)), bodies(whole))

        # A later message cannot rename the topic.
        r, got = await relay_into_a(B, payload={"topic_id": ids["t"], "topic_title": "Renamed"})
        check("T12 [new] a later message carrying another title files in the topic, and the topic "
              "keeps its first title",
              ids["t"] and got and got["payload"].get("topic_id") == ids["t"]
              and (topic(await topics(A, B), ids["t"]) or {}).get("title") == "Thursday dinner", got)
        lane["wires"].clear()
        await compose(A, B, "Booked.", topic_id=ids["t"])
        wire = lane["wires"][-1] if lane["wires"] else {}
        check("T13 [new] a send naming the topic carries the topic's own title, never one the caller chose",
              (wire.get("payload") or {}).get("topic_title") == "Thursday dinner", wire)

        # The relay derives the thread, as the direct lane does.
        r, got = await relay_into_a(B, thread_id=str(uuid.uuid4()))
        check("T14 [new] relay: a thread id the sender chose is not stored; the pair's is",
              r.get("ok") is True and got and got["thread_id"] == pair_ab, (r, got))
        r, got = await direct_into_a(B, thread_id=str(uuid.uuid4()))
        check("T15 [guard] direct lane: the same, as before",
              r.get("ok") is True and got and got["thread_id"] == pair_ab, (r, got))

    # ── P ──────────────────────────────────────────────────────────────────
    async def p_section():
        x, y = str(uuid.uuid4()), str(uuid.uuid4())
        ids["x"] = x
        pair_az = dm_thread_id(A, Z)
        await direct_into_a(Z, payload={"topic_id": x, "topic_title": "Payroll"}, body="Z on payroll")
        await direct_into_a(Z, payload={"topic_id": y, "topic_title": "Taxes"}, body="Z on taxes")
        tz = await topics(A, Z)
        check("P1 [new] Z's topics with A: Payroll and Taxes, one message each",
              (topic(tz, x) or {}).get("title") == "Payroll" and (topic(tz, x) or {}).get("messages") == 1
              and (topic(tz, y) or {}).get("title") == "Taxes", tz)

        r, d = await direct_into_a(B, payload={"topic_id": x, "topic_title": "Payroll (B)"},
                                   thread_id=pair_az, body="B naming Z's topic, direct")
        check("P2 [new] direct: B naming Z's topic id and Z's thread lands in A's conversation with B, "
              "under that id",
              r.get("ok") is True and d and d["from_entity_id"] == B and d["thread_id"] == pair_ab
              and d["payload"].get("topic_id") == x, d)
        r, rl = await relay_into_a(B, payload={"topic_id": x, "topic_title": "Payroll (B)"},
                                   thread_id=pair_az, body="B naming Z's topic, relay")
        check("P3 [new] relay: the same — B's message is in the pair A–B, never in A–Z's thread",
              r.get("ok") is True and rl and rl["thread_id"] == pair_ab, rl)
        zp = await read(A, Z, x)
        check("P4 [new] A's topic Payroll with Z still holds only Z's message",
              bodies(zp) == ["Z on payroll"] and zp.get("total") == 1, zp)
        check("P5 [new] ...and keeps Z's title",
              (topic(await topics(A, Z), x) or {}).get("title") == "Payroll")
        tb = await topics(A, B)
        check("P6 [new] the id B used is a topic of A's conversation with B, under B's own title",
              (topic(tb, x) or {}).get("title") == "Payroll (B)"
              and (topic(tb, x) or {}).get("messages") == 2, tb)
        with as_host(A):
            tool = await MSG.thread_service(pools[A], A, pair_az)
            convos = await MSG.conversations_service(pools[A], A)
        check("P7 [new] the host_message tool's read of A–Z's thread holds nothing from B (relay included)",
              all(not (m.get("body") or "").startswith("B naming") for m in tool["messages"]),
              [m.get("body") for m in tool["messages"]])
        az = next((c for c in convos["conversations"] if c["thread_id"] == pair_az), None)
        check("P8 [new] ...and its conversation list still names Z as the peer of that thread",
              az and az["peer_id"] == Z, az)

        before = await pools[B].fetchval("SELECT count(*) FROM inbox_messages")
        r = await compose(A, B, "into Z's taxes?", topic_id=y)
        after = await pools[B].fetchval("SELECT count(*) FROM inbox_messages")
        code = ((r.get("detail") or {}).get("error") or {}).get("code") if isinstance(r.get("detail"), dict) else None
        check("P9 [new] A's composer cannot send to B into a topic only A–Z has: 404, nothing sent",
              r.get("status") == 404 and code == "no_such_topic" and after == before, (r, before, after))
        m = await mark(A, B, y)
        check("P10 [new] a topic mark on B's conversation naming A–Z's topic is a 404", m.get("status") == 404, m)

        r, junk = await relay_into_a(B, payload={"topic_id": "../../original; DROP", "topic_title": "x"})
        check("P11 [new] relay: a topic id that is not a uuid is not stored; the message is in the original",
              junk and "topic_id" not in junk["payload"] and "topic_title" not in junk["payload"], junk)
        long_title = "Line one\nLine two\x07 " + "x" * 500
        new_t = str(uuid.uuid4()).upper()
        r, lt = await relay_into_a(B, payload={"topic_id": new_t, "topic_title": long_title})
        t_title = ((lt or {}).get("payload") or {}).get("topic_title") or ""
        check("P12 [new] a title is one line, without control characters, at most 120 characters; "
              "an id is stored in its lowercase form",
              lt and lt["payload"].get("topic_id") == new_t.lower() and len(t_title) <= 120
              and "\n" not in t_title and "\x07" not in t_title and " " not in t_title
              and t_title.startswith("Line one Line two"), lt)
        r, pp = await relay_into_a(B, payload={"topic_id": str(uuid.uuid4()), "topic_title": "T"},
                                   person=ANA, to_person=A_PERSON, body="Ana to me, with a topic")
        check("P13 [new] a person-to-person message keeps no topic (a person is one continuous chat)",
              r.get("ok") is True and pp and "topic_id" not in pp["payload"], (r, pp))

    # ── O ──────────────────────────────────────────────────────────────────
    async def o_section():
        lane["wires"].clear()
        r = await compose(A, B, "No topic here")
        wire = lane["wires"][-1] if lane["wires"] else {}
        check("O1 [guard] a send with no topic puts no nested payload on the wire, as before topics",
              r.get("ok") is True and wire and "payload" not in wire, wire)
        check("O2 [new] ...and says it went into the original conversation",
              r.get("topic") == {"id": "original", "title": "The original conversation"}, r)
        on_b = await newest(B, A, B)
        borig = await read(B, A, "original")
        check("O3 [new] B files it under the original conversation and reads it there",
              on_b and "topic_id" not in on_b["payload"] and "No topic here" in bodies(borig)
              and all(m.get("topic_id") == "original" for m in borig["messages"]), (on_b, borig))
        def only_original(page):
            """A read of the original conversation, and nothing from a topic in it."""
            return (page.get("topic_id") == "original" and "Dinner Thursday?" not in bodies(page)
                    and all(m.get("topic_id") == "original" for m in page.get("messages") or []))
        await relay_into_a(B, body="relayed, no topic")
        page = await read(A, B, "original")
        check("O4 [new] a relayed message with no topic is in A's original conversation",
              only_original(page) and "relayed, no topic" in bodies(page), bodies(page))
        await direct_into_a(B, payload={}, body="direct, empty payload")
        page = await read(A, B, "original")
        check("O5 [new] ...and so is a direct one with an empty payload",
              only_original(page) and "direct, empty payload" in bodies(page), bodies(page))
        tf = await topics(A, FRESH)
        check("O6 [new] a Host with no message yet has one topic, the original, with nothing in it",
              isinstance(tf, list) and len(tf) == 1 and tf[0]["id"] == "original"
              and tf[0]["messages"] == 0 and tf[0]["unread"] == 0, tf)

    # ── V ──────────────────────────────────────────────────────────────────
    async def v_section():
        # An older Host's direct-lane wire (deliver.py on main): no nested payload.
        mid = str(uuid.uuid4())
        old_wire = {"subject": "Direct message", "body": "hello from an older Host",
                    "body_format": "text/plain", "thread_id": dm_thread_id(OLD, A), "message_id": mid}
        with as_host(A):
            r = await srv[A]._serve(OLD, {"op": "inbox.deliver", "slip": to_a[OLD], "params": old_wire})
        got = await row(A, mid)
        check("V1 [guard] an older Host's message (no nested payload) is admitted and stored",
              r.get("ok") is True and got is not None, r)
        whole = await read(A, OLD)
        m = (whole.get("messages") or [{}])[-1]
        check("V2 [guard] it reads as before: the whole conversation, the Host as author",
              bodies(whole) == ["hello from an older Host"] and m.get("author_kind") == "entity"
              and whole.get("total") == 1, whole)
        orig = await read(A, OLD, "original")
        to = await topics(A, OLD)
        check("V3 [new] ...and under the original conversation, the one topic it has",
              bodies(orig) == ["hello from an older Host"] and isinstance(to, list) and len(to) == 1
              and to[0]["messages"] == 1, (orig, to))
        it = await deck_entry(A, OLD)
        check("V4 [guard] the deck counts it unread", it and it.get("unread") == 1, it)

        # What a newer Host sends an older one.
        lane["wires"].clear()
        await compose(A, B, "into the topic, wire check", topic_id=ids.get("t"))
        twire = lane["wires"][-1] if lane["wires"] else {}
        lane["wires"].clear()
        await compose(A, B, "original, wire check")
        owire = lane["wires"][-1] if lane["wires"] else {}
        check("V5 [new] a topic changes the wire only by the nested payload, which holds the two topic keys",
              set(twire) - set(owire) == {"payload"}
              and set(twire.get("payload") or {}) == {"topic_id", "topic_title"},
              (sorted(twire), sorted(owire)))
        saved = H._ALLOWED_DELIVER_PAYLOAD_KEYS
        H._ALLOWED_DELIVER_PAYLOAD_KEYS = ()   # main's value (handlers.py:53 on 821180ca0)
        try:
            r = await compose(A, B, "to an older Host, in a topic", topic_id=ids.get("t"))
        finally:
            H._ALLOWED_DELIVER_PAYLOAD_KEYS = saved
        on_b = await newest(B, A, B)
        check("V6 [guard] an older receiver drops the topic and keeps the message, in its one conversation",
              r.get("ok") is True and on_b and on_b["body"] == "to an older Host, in a topic"
              and "topic_id" not in on_b["payload"] and on_b["thread_id"] == pair_ab, (r, on_b))
        # A row an older relay path stored with topic keys nothing checked.
        await pools[A].execute(
            "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, subject, "
            "body, message_kind, payload, thread_id) VALUES ($1, $2, $3, $4, 'Direct message', "
            "'stored raw by an older relay', 'message', $5::jsonb, $6::uuid)",
            f"{OLD}@entity.localhost", f"{A}@entity.localhost", OLD, A,
            json.dumps({"topic_id": "not a uuid", "topic_title": "junk\n" * 50}), dm_thread_id(OLD, A))
        orig = await read(A, OLD, "original")
        to = await topics(A, OLD)
        check("V7 [new] a stored row whose topic id is not a uuid reads under the original conversation",
              "stored raw by an older relay" in bodies(orig) and isinstance(to, list) and len(to) == 1,
              (orig, to))

    # ── K ──────────────────────────────────────────────────────────────────
    async def k_section():
        await pools[A].execute("UPDATE contacts SET status = 'blocked' WHERE id = $1", b_on_a)
        before = await pools[A].fetchval("SELECT count(*) FROM inbox_messages WHERE from_entity_id = $1", B)
        fresh = str(uuid.uuid4())
        named = {"a new topic": {"topic_id": fresh, "topic_title": "New"},
                 "B's own topic": {"topic_id": ids.get("t") or fresh, "topic_title": "Thursday dinner"},
                 "Z's topic": {"topic_id": ids.get("x") or fresh, "topic_title": "Payroll"},
                 "no topic": None}

        def refused(r):
            inner = r.get("data") if isinstance(r.get("data"), dict) else r
            return r.get("ok") is False and inner.get("blocked") is True and r.get("error") == BLOCKED

        for label, pl in named.items():
            r, got = await direct_into_a(B, payload=pl)
            check(f"K [guard] direct: blocked B naming {label} is refused, nothing stored",
                  refused(r) and got is None, r)
            r, got = await relay_into_a(B, payload=pl)
            check(f"K [guard] relay: blocked B naming {label} is refused, nothing stored",
                  refused(r) and got is None, r)
        after = await pools[A].fetchval("SELECT count(*) FROM inbox_messages WHERE from_entity_id = $1", B)
        check("K [guard] not one row from B was written while it was blocked", after == before, (before, after))
        r, got = await direct_into_a(B, payload=named["B's own topic"], person=ANA, body="Ana, in the topic")
        check("K [guard] Ana, a person A's contacts place on B, still arrives through B's block",
              r.get("ok") is True and got is not None, r)
        check("K [new] ...in the topic she named",
              ids.get("t") and got and got["payload"].get("topic_id") == ids["t"], got)
        await pools[A].execute("UPDATE contacts SET status = 'active' WHERE id = $1", b_on_a)

    # ── M ──────────────────────────────────────────────────────────────────
    async def m_section():
        t1, t2 = str(uuid.uuid4()), str(uuid.uuid4())
        for body in ("w-o-1", "w-o-2"):
            await direct_into_a(W, body=body)
        for body in ("w-t1-1", "w-t1-2"):
            await direct_into_a(W, payload={"topic_id": t1, "topic_title": "Plans"}, body=body)
        await direct_into_a(W, payload={"topic_id": t2, "topic_title": "Gifts"}, body="w-t2-1")

        async def unread_of():
            tw = await topics(A, W)
            return ({t["id"]: t["unread"] for t in tw} if isinstance(tw, list) else tw,
                    (await deck_entry(A, W) or {}).get("unread"))
        u, deck = await unread_of()
        check("M1 [new] never opened: each topic counts its own (original 2, Plans 2, Gifts 1)",
              u == {"original": 2, t1: 2, t2: 1}, u)
        check("M2 [guard] the deck's count for W is all five", deck == 5, deck)
        r = await mark(A, W, t1)
        check("M3 [new] marking one topic answers its own mark, \"<peer>/<topic>\"",
              r.get("id") == f"{W}/{t1}" and r.get("seen_at") is not None, r)
        u, deck = await unread_of()
        check("M4 [new] ...which clears that topic alone, and the deck counts the rest (3)",
              u == {"original": 2, t1: 0, t2: 1} and deck == 3, (u, deck))
        await tick()
        await direct_into_a(W, payload={"topic_id": t1, "topic_title": "Plans"}, body="w-t1-3")
        u, deck = await unread_of()
        check("M5 [new] a new message in the marked topic counts again (Plans 1, deck 4)",
              u == {"original": 2, t1: 1, t2: 1} and deck == 4, (u, deck))
        await mark(A, W, "original")
        u, deck = await unread_of()
        check("M6 [new] marking the original conversation clears it alone (deck 2)",
              u == {"original": 0, t1: 1, t2: 1} and deck == 2, (u, deck))
        tw = await topics(A, W)
        check("M7 [new] a topic's seen_at is its own mark; one never marked has none",
              (topic(tw, t1) or {}).get("seen_at") is not None
              and isinstance(topic(tw, t2), dict) and topic(tw, t2).get("seen_at") is None, tw)
        await pools[A].execute(
            "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, subject, "
            "body, message_kind, payload, thread_id) VALUES ($1, $2, $3, $4, 'Direct message', 'ours', "
            "'message', $5::jsonb, $6::uuid)", f"{A}@entity.localhost", f"{W}@entity.localhost", A, W,
            json.dumps({"topic_id": t2, "topic_title": "Gifts"}), dm_thread_id(A, W))
        u, _ = await unread_of()
        check("M8 [new] this Host's own messages never count in a topic",
              isinstance(u, dict) and u.get(t2) == 1, u)
        await tick()
        await mark(A, W)
        u, deck = await unread_of()
        check("M9 [guard] the whole conversation's mark (no topic) clears the deck's count", deck == 0, deck)
        check("M10 [new] ...and every topic", u == {"original": 0, t1: 0, t2: 0}, u)
        for label, args, want in (("a topic id that is not one", (W, "nope"), 400),
                                  ("a topic the conversation does not have", (W, str(uuid.uuid4())), 404),
                                  ("a topic on a person's conversation", (ANA, t1, "dm"), 400),
                                  ("a topic on your own Host's page", ("original", t1, "self"), 400)):
            r = await mark(A, *args)
            check(f"M11 [new] a topic mark is refused for {label} ({want})", r.get("status") == want, r)

    # ── R ──────────────────────────────────────────────────────────────────
    async def r_section():
        fn = getattr(C, "list_circles_topics", None)
        got = []
        with as_host(A):
            for k, i in (("dm", ANA), ("self", "original"), ("entity", "abc"), ("entity", str(A))):
                try:
                    await fn(k, i) if fn else None
                    got.append(200 if fn else None)
                except HTTPException as e:
                    got.append(e.status_code)
        check("R1 [new] topics are listed only for a conversation with another Host (400 otherwise)",
              got == [400, 400, 400, 400], got)
        bad = await read(A, B, "junk")
        unknown = await read(A, B, str(uuid.uuid4()))
        check("R2 [new] reading a topic: an id that is not one is a 400, an unknown one a 404",
              bad.get("status") == 400 and unknown.get("status") == 404, (bad, unknown))
        both = await compose(A, B, "x", topic_id=ids.get("t") or str(uuid.uuid4()), new_topic="Y")
        blank = await compose(A, B, "x", new_topic=" \n ")
        with as_host(A):
            try:
                await C.send_message(request({"target_kind": "dm", "target_id": ANA, "body": "x",
                                              "new_topic": "T"}, {"X-Authentik-Email": A_PERSON}, "POST"))
                dm = 200
            except HTTPException as e:
                d = e.detail if isinstance(e.detail, dict) else {}
                dm = (e.status_code, (d.get("error") or {}).get("code"))
        check("R3 [new] a send naming a topic and a new one is a 400; a new topic needs a title; "
              "a person's chat takes no topic",
              both.get("status") == 400 and blank.get("status") == 400 and dm == (400, "topic_not_here"),
              (both, blank, dm))

    # ── S ──────────────────────────────────────────────────────────────────
    async def s_section():
        peer = 180
        rows = []
        for i in range(3000):
            tid = None if i % 31 == 0 else str(uuid.UUID(int=(i % 30) + 1))
            pl = {"topic_id": tid, "topic_title": f"Topic {i % 30}"} if tid else {}
            frm, to = (peer, A) if i % 2 else (A, peer)
            rows.append((f"{frm}@entity.localhost", f"{to}@entity.localhost", frm, to,
                         f"message {i}", json.dumps(pl), dm_thread_id(A, peer)))
        await pools[A].executemany(
            "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, subject, "
            "body, message_kind, payload, thread_id) VALUES ($1, $2, $3, $4, 'Direct message', $5, 'message', "
            "$6::jsonb, $7::uuid)", rows)
        await pools[A].execute("ANALYZE inbox_messages")
        t0 = time.perf_counter()
        tl = await topics(A, peer)
        list_ms = (time.perf_counter() - t0) * 1000
        seven = str(uuid.UUID(int=7))
        t0 = time.perf_counter()
        page = await read(A, peer, seven)
        read_ms = (time.perf_counter() - t0) * 1000
        print(f"        speed: 3,000 messages in 30 topics: list {list_ms:.0f} ms, "
              f"one topic's page {read_ms:.0f} ms")
        check("S1 [new] the list has the original and the 30 topics, in well under a second and a half",
              isinstance(tl, list) and len(tl) == 31 and list_ms < 1500,
              (len(tl) if isinstance(tl, list) else tl, list_ms))
        check("S2 [new] one topic's page reads its own messages",
              page.get("total") and page["total"] < 200
              and all(m.get("topic_id") == seven for m in page["messages"]), page.get("total"))

    use(A)
    await section("T. a topic stays inside the derived pair thread", t_section)
    await section("P. a peer cannot use a topic to reach another pair", p_section)
    await section("O. a message without a topic files under the first thread", o_section)
    await section("V. an older Host's message reads normally", v_section)
    await section("K. a blocked Host is refused whatever topic it names", k_section)
    await section("M. marks per topic", m_section)
    await section("R. the routes refuse what is not a topic", r_section)
    await section("S. speed", s_section)

    bs._pool = None
    for p in pools.values():
        await p.close()
    return 1 if FAILS else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=A) as _db_a, kernel_schema_db(_srv, entity_id=B) as _db_b:
    rc = asyncio.run(main(_db_a, _db_b))
rc = rc or (1 if FAILS else 0)
print(f"\n{CHECKS} checks, {len(FAILS)} failed")
for f in FAILS:
    print("  - " + f)
print("RESULT: ALL PASS" if not FAILS else f"RESULT: {len(FAILS)} FAILED")
sys.exit(rc)
