#!/usr/bin/env python3
"""Approvals take a passkey tap; the authenticator code is the fallback.

The owner's own passkey (owner_passkeys, the one they sign in with) approves a
card over the chat, bound to exactly that answer (server/approval_passkeys.py),
checked by the one choke point (nac_stepup.verify_stepup) before anything acts.
Driven through the real webauthn library by a software authenticator (ES256),
the real routes' functions (routes/approvals.py) and the real decide
(tickets.decide_ticket_service -> pairing_grants.decide_card -> approve_pairing),
on a fresh clone of the kernel's schema. Only the machine is stubbed: the
anchor read and the sign-and-push of the grant.

  1. GET /api/approvals/passkey: a passkey approves on the desktop's own name;
     not on an app origin's; only the person's chat asks, signed in.
  2. The machine pairing card ("<Host> wants to use <machine>. Let it in?"):
     a tap made for Let it in with the boxes ticked admits with exactly those.
  3. Bound to the request: the same tap for other boxes, another option,
     another card, or a card whose words changed after the challenge, is
     refused; the card stays up and nothing is signed.
  4. Used once: a tap that approved is refused the second time.
  5. Short: a challenge past CHALLENGE_TTL_S is refused.
  6. Only for a caller that binds a purpose: verify_stepup with none refuses a
     tap without counting it; the Host's own decide passes none.
  7. Refused and counted like a wrong code: no user verification, another
     origin, a forged signature, a counter that went back, a passkey this Host
     does not hold, a passkey made for another address.
  8. The code still works on the same card, and a wrong one says so; with no
     passkey and no code the card says neither is set up.
  9. The machine-share card's request: a tap for one body opens no other; only
     its own path can be asked for.
 10. A messaging connector's card: the account its grant matched is part of
     what a tap approves.
 11. Adding a passkey while a code is set up takes a passkey check or the code
     (owner_passkeys._approval_factor): a recovery code or the password alone
     no longer gains one.

    python3 scripts/test_passkey_approvals_pg.py      (from the repo root)

TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN names a server; without one it starts a
throwaway one through docker, as the other _pg gates do.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

os.environ["CSERIES_URL"] = "http://home.cseries.localhost:8800"
os.environ.pop("CSERIES_REMOTE_URL", None)
ENTITY = 9001
os.environ["ENTITY_ID"] = str(ENTITY)

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove a passkey approval is checked and burned.")
    sys.exit(2)

import asyncpg  # noqa: E402
import cbor2  # noqa: E402  (the webauthn library's own dependency)
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402
from server import approval_passkeys as AP  # noqa: E402
from server import brain_service, owner_passkeys as P  # noqa: E402
from server.home_origin import HOME  # noqa: E402
from server.network import nac_stepup, pairing_grants as PG  # noqa: E402
from server.routes import approvals, tickets  # noqa: E402

FAILS: list[str] = []
CHECKS = 0
HOST = HOME.host                      # home.cseries.localhost
ORIGIN = HOME.origin                  # http://home.cseries.localhost:8800
SERVER_NAME, SUBJECT, HANDLE = "studio-desktop", "aster1a2b3c4d.example.test", "aster"


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:600]) if extra else ""))
    if not cond:
        FAILS.append(msg)


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class Authenticator:
    """A passkey on a pretend device: one ES256 key and its counter, with the
    browser's half (clientDataJSON) folded in."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.counter = 0

    def cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def get(self, options, origin=ORIGIN, rp_id=None, flags=0x05, counter=None, forge=False) -> str:
        rp = rp_id or options["rpId"]
        if counter is None:
            self.counter += 1
            counter = self.counter
        cdj = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin,
                          "crossOrigin": False}).encode()
        auth = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", counter)
        signer = ec.generate_private_key(ec.SECP256R1()) if forge else self.key
        sig = signer.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        cred = {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth),
                             "signature": b64u(sig), "userHandle": b64u(P.USER_HANDLE)},
                "clientExtensionResults": {}}
        return AP.PROOF_PREFIX + json.dumps(cred)


# ── the routes, as the person's chat calls them ─────────────────────────────

SESSION = {"live": True}


async def _live_session(scope):
    return ("owner@example.com", "tok") if SESSION["live"] else (None, None)


approvals.live_session = _live_session


def request(method: str, path: str, body=None, *, host=f"{HOST}:8800", app_id="circles") -> Request:
    payload = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "method": method, "path": path, "query_string": b"", "state": {},
             "headers": [(b"host", host.encode()), (b"content-type", b"application/json")]}
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}
    r = Request(scope, receive)
    r.state.app_token = SimpleNamespace(app_id=app_id)
    return r


async def route(fn, req) -> tuple[int, dict]:
    try:
        resp = await fn(req)
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}
    return resp.status_code, json.loads(resp.body)


async def ticket_options(ticket_id, option_id, choices=None, **kw) -> dict:
    body = {"ticket_id": ticket_id, "option_id": option_id}
    if choices is not None:
        body["choices"] = choices
    st, j = await route(approvals.ticket_challenge, request("POST", "/api/approvals/passkey/ticket", body, **kw))
    assert st == 200, (st, j)
    return j["options"]


# ── the machine, stubbed ────────────────────────────────────────────────────

SIGNED: list[tuple] = []


async def fake_anchored(pool, eid, server):
    return {"token_id": 7, "hostname": server, "anchor": {"anchor_host": "home.cseries.localhost"},
            "pairings": [{"host_name": SUBJECT, "handle": HANDLE}], "unapproved_pairing_attempts": []}


async def fake_sign_and_push(pool, eid, server, subject, handle, caps, models, *, row=None,
                             ttl_days=None, approved_by=None):
    SIGNED.append((server, subject, list(caps), approved_by))
    return {"state": "delivered", "delivered": True, "caps": list(caps)}


async def no_answer(*a, **k):
    return {}


PG._anchored_here = fake_anchored
PG.sign_and_push_grant = fake_sign_and_push
tickets._answer_to_turn = no_answer


async def new_card(pool) -> int:
    sitrep, options, ceiling = PG.card_words(HANDLE, SERVER_NAME, {"host_name": SUBJECT})
    t = await tickets.create_ticket_service(
        pool, ENTITY, source="runtime", sitrep=sitrep, options=options, priority=70,
        metadata={"kind": PG.PAIRING_CARD_KIND,
                  PG.PAIRING_CARD_KIND: {"token_id": 7, "server": SERVER_NAME, "subject": SUBJECT,
                                         "handle": HANDLE, "stepup_option": PG.CARD_ADMIT,
                                         "ceiling": ceiling},
                  "user_only": True, "no_pattern": True, "asked_in": {"app_id": "circles"}})
    return int(t.get("ticket_id") or t.get("id"))


async def clear_failures(pool) -> None:
    """The attempt limit is pinned in section 7 and in test_stepup_attempt_limit;
    elsewhere each refusal here starts from a clean record, so the limit's
    wait never stands in for the refusal being checked."""
    await pool.execute("DELETE FROM kernel_secrets WHERE id LIKE 'nac_stepup_failures%'")


async def decide(pool, tid, code, choices=None, actor="user"):
    try:
        out = await tickets.decide_ticket_service(pool, ENTITY, tid, option_id=PG.CARD_ADMIT,
                                                  stepup_code=code, choices=choices, actor=actor)
        return 200, out
    except tickets.TicketError as e:
        return e.status, {"detail": e.detail}
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}
    finally:
        await clear_failures(pool)


async def status_of(pool, tid) -> str:
    return await pool.fetchval("SELECT status FROM entity_tickets WHERE id=$1", tid)


async def owner_failures(pool) -> int:
    raw = await pool.fetchval("SELECT secret FROM kernel_secrets WHERE id=$1", "nac_stepup_failures")
    return int(json.loads(bytes(raw).decode())["n"]) if raw else 0


class Clock:
    """nac_stepup's clock, a window on for each code instead of waiting."""

    def __init__(self):
        self.now = float(int(time.time()) // 30 * 30) + 5

    def time(self) -> float:
        return self.now


CLOCK = Clock()
nac_stepup.time = CLOCK


async def main(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    brain_service.get_pool = lambda: pool
    brain_service._entity_id = ENTITY
    dev = Authenticator()
    await pool.execute(
        "INSERT INTO owner_passkeys (credential_id, public_key, sign_count, transports, label, rp_id) "
        "VALUES ($1,$2,0,'{internal}','laptop',$3)", dev.cred_id, dev.cose(), HOST)
    secret = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    raw = nac_stepup._b32decode(secret)

    def next_code() -> str:
        CLOCK.now += 30
        return nac_stepup._hotp(raw, int(CLOCK.now) // 30)

    print("== 1. can a passkey approve here")
    st, j = await route(approvals.passkey_status, request("GET", "/api/approvals/passkey"))
    check(st == 200 and j["passkey"] is True and j["code"] is True
          and j["setup_url"] == ORIGIN + "/passkey", "on the desktop's own name: a passkey and a code", j)
    st, j = await route(approvals.passkey_status,
                        request("GET", "/api/approvals/passkey", host="apps-x1-home.cseries.localhost:8800"))
    check(st == 200 and j["passkey"] is False, "on an app origin's name: no passkey (the code alone)", j)
    st, j = await route(approvals.passkey_status, request("GET", "/api/approvals/passkey", app_id="notes"))
    check(st == 403, "another app's token is refused", (st, j))
    SESSION["live"] = False
    st, j = await route(approvals.passkey_status, request("GET", "/api/approvals/passkey"))
    SESSION["live"] = True
    check(st == 401, "without the owner's session: refused", (st, j))

    print("== 2. the machine pairing card, approved with a tap")
    tid = await new_card(pool)
    picked = ["observe", "session"]
    opts = await ticket_options(tid, PG.CARD_ADMIT, picked)
    chal = base64.urlsafe_b64decode(opts["challenge"] + "==")
    purpose = AP.ticket_purpose(tid, *(await pool.fetchrow(
        "SELECT sitrep, options, metadata FROM entity_tickets WHERE id=$1", tid)), PG.CARD_ADMIT, picked)
    check(opts["rpId"] == HOST and opts["userVerification"] == "required"
          and [c["id"] for c in opts["allowCredentials"]] == [b64u(dev.cred_id)],
          "the challenge names this Host's desktop name, asks for the person's verification, "
          "offers only the owner's passkey", opts)
    check(AP.challenge_matches(chal, purpose) and len(chal) == 32,
          "the challenge is derived from the card's purpose (ticket, option, words, boxes)")
    proof = dev.get(opts)
    st, out = await decide(pool, tid, proof, picked)
    check(st == 200 and SIGNED and SIGNED[-1][2] == ["observe", "session"] and SIGNED[-1][3] == "user",
          "Let it in with a tap admits with exactly the boxes ticked, as the person", (st, out, SIGNED))
    check(await status_of(pool, tid) == "decided", "the card is decided")

    print("== 4. used once")
    tid2 = await new_card(pool)
    opts = await ticket_options(tid2, PG.CARD_ADMIT, picked)
    proof = dev.get(opts)
    p2 = AP.ticket_purpose(tid2, *(await pool.fetchrow(
        "SELECT sitrep, options, metadata FROM entity_tickets WHERE id=$1", tid2)), PG.CARD_ADMIT, picked)
    check(await nac_stepup.verify_stepup(pool, proof, purpose=p2) is True, "a tap verifies once")
    check(await nac_stepup.verify_stepup(pool, proof, purpose=p2) is False, "the same tap again is refused")
    await clear_failures(pool)
    n = len(SIGNED)
    st, out = await decide(pool, tid2, proof, picked)
    check(st == 403 and out["detail"] == AP.REFUSED and len(SIGNED) == n
          and await status_of(pool, tid2) == "open",
          "replayed on the card: refused in plain words, nothing signed, the card stays up", (st, out))

    # A passkey that keeps no signature counter (most synced passkeys send 0):
    # only the challenge being used once stops a replay.
    flat = Authenticator()
    await pool.execute(
        "INSERT INTO owner_passkeys (credential_id, public_key, sign_count, transports, label, rp_id) "
        "VALUES ($1,$2,0,'{internal}','phone',$3)", flat.cred_id, flat.cose(), HOST)
    opts = await ticket_options(tid2, PG.CARD_ADMIT, picked)
    proof = flat.get(opts, counter=0)
    check(await nac_stepup.verify_stepup(pool, proof, purpose=p2) is True, "a passkey with no counter approves once")
    check(await nac_stepup.verify_stepup(pool, proof, purpose=p2) is False,
          "and its tap again is refused, with no counter to catch it")
    await clear_failures(pool)
    # Each binding on its own: the challenge kept for one purpose, and the
    # challenge derived from it.
    opts = await ticket_options(tid2, PG.CARD_ADMIT, picked)
    chal = base64.urlsafe_b64decode(opts["challenge"] + "==")
    other_purpose = p2 + "-other"
    AP._pending[chal] = (other_purpose,) + AP._pending[chal][1:]
    check(await nac_stepup.verify_stepup(pool, flat.get(opts, counter=0), purpose=p2) is False,
          "a challenge kept for another purpose is refused, though derived from this one")
    opts = await ticket_options(tid2, PG.CARD_ADMIT, picked)
    chal = base64.urlsafe_b64decode(opts["challenge"] + "==")
    AP._pending[chal] = (other_purpose,) + AP._pending[chal][1:]
    check(await nac_stepup.verify_stepup(pool, flat.get(opts, counter=0), purpose=other_purpose) is False,
          "a challenge not derived from the purpose is refused, though kept for it")
    await clear_failures(pool)
    await pool.execute("DELETE FROM owner_passkeys WHERE credential_id=$1", flat.cred_id)

    print("== 3. bound to the request")
    n = len(SIGNED)
    opts = await ticket_options(tid2, PG.CARD_ADMIT, picked)
    st, out = await decide(pool, tid2, dev.get(opts), ["observe", "session", "egress"])
    check(st == 403 and len(SIGNED) == n and await status_of(pool, tid2) == "open",
          "a tap made for two boxes, sent with a third ticked: refused, nothing signed", (st, out))
    opts = await ticket_options(tid2, PG.CARD_NEVER)
    st, out = await decide(pool, tid2, dev.get(opts), picked)
    check(st == 403 and len(SIGNED) == n, "a tap made for Never, sent with Let it in: refused", (st, out))
    tid3 = await new_card(pool)
    opts = await ticket_options(tid3, PG.CARD_ADMIT, picked)
    st, out = await decide(pool, tid2, dev.get(opts), picked)
    check(st == 403 and len(SIGNED) == n, "a tap made for another card: refused", (st, out))
    opts = await ticket_options(tid2, PG.CARD_ADMIT, picked)
    await pool.execute("UPDATE entity_tickets SET sitrep = sitrep || ' (and more)' WHERE id=$1", tid2)
    st, out = await decide(pool, tid2, dev.get(opts), picked)
    check(st == 403 and len(SIGNED) == n, "a card whose words changed after the challenge: refused", (st, out))
    opts = await ticket_options(tid2, PG.CARD_ADMIT, picked)
    st, out = await decide(pool, tid2, dev.get(opts), picked)
    check(st == 200 and len(SIGNED) == n + 1, "the same card, tapped for what it now says: admitted", (st, out))

    print("== 5. short")
    tid4 = await new_card(pool)
    opts = await ticket_options(tid4, PG.CARD_ADMIT, picked)
    real_now = AP._now
    AP._now = lambda: real_now() + AP.CHALLENGE_TTL_S + 1
    st, out = await decide(pool, tid4, dev.get(opts), picked)
    AP._now = real_now
    check(st == 403 and await status_of(pool, tid4) == "open",
          f"a challenge older than {int(AP.CHALLENGE_TTL_S)} s: refused", (st, out))

    print("== 6. only for a caller that binds a purpose")
    opts = await ticket_options(tid4, PG.CARD_ADMIT, picked)
    proof = dev.get(opts)
    before = await owner_failures(pool)
    check(await nac_stepup.verify_stepup(pool, proof) is False and await owner_failures(pool) == before,
          "verify_stepup with no purpose refuses a tap and does not count it")
    st, out = await decide(pool, tid4, proof, picked, actor="host")
    check(st == 403 and await status_of(pool, tid4) == "open", "the Host's own decide: refused", (st, out))
    p4 = AP.ticket_purpose(tid4, *(await pool.fetchrow(
        "SELECT sitrep, options, metadata FROM entity_tickets WHERE id=$1", tid4)), PG.CARD_ADMIT, picked)
    check(await nac_stepup.verify_stepup(pool, proof, purpose=p4) is True,
          "and the tap the Host could not spend still approves the person's own answer")

    print("== 7. refused and counted like a wrong code")
    other = Authenticator()

    async def wrong(what, make):
        opts = await ticket_options(tid4, PG.CARD_ADMIT, picked)
        before = await owner_failures(pool)
        ok = await nac_stepup.verify_stepup(pool, make(opts), purpose=p4)
        check(ok is False and await owner_failures(pool) == before + 1, what + ": refused and counted")
        await pool.execute("DELETE FROM kernel_secrets WHERE id LIKE 'nac_stepup_failures%'")
    await wrong("no user verification (presence only)", lambda o: dev.get(o, flags=0x01))
    await wrong("another origin", lambda o: dev.get(o, origin="http://apps-x1-home.cseries.localhost:8800"))
    await wrong("a forged signature", lambda o: dev.get(o, forge=True))
    await wrong("a counter that went back", lambda o: dev.get(o, counter=1))
    await wrong("a passkey this Host does not hold", lambda o: other.get(o))
    await pool.execute("UPDATE owner_passkeys SET rp_id='home.k7x2m9.remote.example' WHERE credential_id=$1",
                       dev.cred_id)
    opts_wrong_rp = None
    try:
        opts_wrong_rp = await ticket_options(tid4, PG.CARD_ADMIT, picked)
    except AssertionError as e:
        check("no passkey" in str(e), "a passkey made for another address: none is offered here", e)
    check(opts_wrong_rp is None, "and no challenge is made")
    await pool.execute("UPDATE owner_passkeys SET rp_id=$2 WHERE credential_id=$1", dev.cred_id, HOST)

    print("== 8. the code is the fallback")
    tid5 = await new_card(pool)
    st, out = await decide(pool, tid5, "000000", picked)
    check(st == 403 and "That code didn't work" in out["detail"] and await status_of(pool, tid5) == "open",
          "a wrong code says the code didn't work", (st, out))
    st, out = await decide(pool, tid5, next_code(), picked)
    check(st == 200 and SIGNED[-1][2] == picked, "the current code admits, as before", (st, out))
    await pool.execute("DELETE FROM owner_passkeys WHERE credential_id=$1", dev.cred_id)
    await pool.execute("DELETE FROM kernel_secrets WHERE id=$1", nac_stepup._TOTP_ENROLLED_ID)
    st, j = await route(approvals.passkey_status, request("GET", "/api/approvals/passkey"))
    check(st == 200 and j["passkey"] is False and j["code"] is False, "neither set up: the status says so", j)
    tid6 = await new_card(pool)
    st, j = await route(approvals.ticket_challenge, request("POST", "/api/approvals/passkey/ticket",
                                                            {"ticket_id": tid6, "option_id": PG.CARD_ADMIT}))
    check(st == 409 and j.get("no_passkey") is True, "no passkey: no challenge, and the card is told why", (st, j))
    tid6 = await new_card(pool)
    st6, out = await decide(pool, tid6, "123456", picked)
    check(st6 == 403 and "no passkey or authenticator set up" in out["detail"],
          "a card with neither set up says so, and names the passkey", out)
    await nac_stepup.mark_enrolled(pool)
    await pool.execute(
        "INSERT INTO owner_passkeys (credential_id, public_key, sign_count, transports, label, rp_id) "
        "VALUES ($1,$2,$3,'{internal}','laptop',$4)", dev.cred_id, dev.cose(), dev.counter, HOST)

    print("== 9. the machine-share card's request")
    path = "/api/network/bastions/machine-offers/accept"
    body = {"offer_id": 4, "server": SERVER_NAME, "models": ["llama-3-8b"]}
    st, j = await route(approvals.request_challenge, request("POST", "/api/approvals/passkey/request",
                                                             {"path": path, "body": body}))
    check(st == 200, "a challenge for this request", (st, j))
    proof = dev.get(j["options"])
    wider = dict(body, models=["llama-3-8b", "qwen-72b"])
    check(await nac_stepup.verify_stepup(pool, proof, purpose=AP.purpose_for_request("POST", path, wider)) is False,
          "a tap for one model list does not share another")
    await clear_failures(pool)
    st, j = await route(approvals.request_challenge, request("POST", "/api/approvals/passkey/request",
                                                             {"path": path, "body": body}))
    proof = dev.get(j["options"])
    check(await nac_stepup.verify_stepup(
        pool, proof, purpose=AP.purpose_for_request("POST", path, dict(body, stepup_code=proof))) is True,
          "the same body (the proof left out) is approved")
    st, j = await route(approvals.request_challenge, request("POST", "/api/approvals/passkey/request",
                                                             {"path": "/api/vault/reveal", "body": {}}))
    check(st == 400, "a request no card sends is not asked for", (st, j))

    print("== 10. a messaging connector's card: the account is part of it")
    md = {"kind": "connector_pairing", "connector_pairing": {"grant_id": 1}}
    a = AP.ticket_purpose(1, "s", [{"id": "approve"}], md, "approve", None,
                          {"platform": "telegram", "account": "100", "pairing": "matched"})
    b = AP.ticket_purpose(1, "s", [{"id": "approve"}], md, "approve", None,
                          {"platform": "telegram", "account": "200", "pairing": "matched"})
    check(a != b, "another account matched after the tap: another purpose")

    print("== 10b. a board post card: the request it posts is part of it")
    from mastercode.runtime import board_post_approval as BPA
    check(getattr(AP, "_PAYLOAD_KEYS", {}).get(BPA.PROPOSAL_KIND) == (BPA.PAYLOAD_KEY, BPA.THEN_KEY),
          "the keys digested are the ones the board post's act reads")
    opts = [{"id": BPA.OPT_POST}]
    def bp(text, then=None):
        md = {"kind": BPA.PROPOSAL_KIND, BPA.PAYLOAD_KEY: {"board": "members", "text": text}}
        if then is not None:
            md[BPA.THEN_KEY] = then
        return AP.ticket_purpose(1, "s", opts, md, BPA.OPT_POST)
    check(bp("hello all") != bp("something else"),
          "the posted text changed after the tap: another purpose")
    check(bp("hello all") != bp("hello all", {"propose": {"done_looks_like": "x"}}),
          "what runs after the post changed: another purpose")
    check(bp("hello all") == bp("hello all"), "the same card: the same purpose")

    print("== 11. adding a passkey while a code is set up")
    token = "session-" + os.urandom(6).hex()
    P._step_ups.clear()

    async def factor(code=None):
        try:
            await P._approval_factor(pool, token, code, "Adding a passkey")
            return None
        except P.CodeNeeded as e:
            return e
    P._note_step_up(token, P.RECOVERY)
    check(isinstance(await factor(), P.CodeNeeded), "after a recovery code alone: the code is asked for")
    P._note_step_up(token, P.PASSWORD)
    check(isinstance(await factor(), P.CodeNeeded), "after the password alone: the code is asked for")
    check(isinstance(await factor("000000"), P.CodeNeeded), "a wrong code: refused")
    # Wrong codes until the record waits: the page still gets words to show
    # (CodeNeeded, which /passkey reads as `error`), never a bare 429.
    waited = None
    for _ in range(30):
        try:
            e = await factor("000000")
        except nac_stepup.StepupLocked as locked:
            waited = locked
            break
        if e is not None and "waiting" in str(e):
            waited = e
            break
    check(isinstance(waited, P.CodeNeeded) and "try again in" in str(waited),
          "too many wrong codes: refused with the wait in words", repr(waited))
    await clear_failures(pool)
    check(await factor(next_code()) is None, "the current code: added")
    P._note_step_up(token, P.PASSKEY)
    check(await factor() is None, "after a passkey check: added, no code")
    await pool.execute("DELETE FROM kernel_secrets WHERE id=$1", nac_stepup._TOTP_ENROLLED_ID)
    P._step_ups.clear()
    P._note_step_up(token, P.PASSWORD)
    check(await factor() is None, "with no code set up: nothing more than before")
    await pool.close()


if __name__ == "__main__":
    if SERVER:
        with kernel_schema_db(SERVER, entity_id=ENTITY) as dsn:
            asyncio.run(main(dsn))
    else:
        with throwaway_server() as server_dsn:
            with kernel_schema_db(server_dsn, entity_id=ENTITY) as dsn:
                asyncio.run(main(dsn))
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} passed")
    if FAILS:
        print("FAILED:\n  " + "\n  ".join(FAILS))
        sys.exit(1)
