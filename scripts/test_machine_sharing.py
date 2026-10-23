#!/usr/bin/env python3
"""Sharing a machine's MODELS with another host (host side).

Runs as pytest (`pytest scripts/test_machine_sharing.py`) AND as a plain script
in the container-free gate lane (APP_ROOT=<repo> python3 scripts/…). No DB, no
network: the pool is a recording stub, the mgmt relay is a recording stub, the
direct peer lane is a recording stub, and the entity key is the same FIXED
ed25519 seed scripts/test_pairing_grants.py uses, so the signed bytes are
pinned here too.

THE SENTENCE UNDER TEST: a friend's host approved
for one model must be able to say truthfully "the only thing we can do with this
machine is use the qwen4 model." On this side that means three things and they
are what the file checks: the model scope reaches the SIGNED terms; an approval
costs the other side nothing until the user's second factor has been checked;
and an enrolment token never touches a lane the platform can read.

The matching half — the box refusing every other model, refusing the sterilizer
unless it was named, refusing the store-mutating half of ollama's API, and
advertising one model on its beat — is the gateway's client-go/modelscope_test.go.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey, Ed25519PublicKey)

from server.network import machine_offers as mo  # noqa: E402
from server.network import pairing_grants as pg  # noqa: E402

SEED = bytes(range(1, 33))
KEY = Ed25519PrivateKey.from_private_bytes(SEED)
PUB_B64 = "ebVWLo/mVPlAeLES6KmLp5AfhTrmlb7X4OORC60ElmQ="

ANCHOR = "entity-1.zone.example.test"
SERVER = "box-a"
SUBJECT = "a1b2c3d4e5f60718.zone.example.test"
HANDLE = "a1b2c3d4e5f60718"
PEER = 9122
MODEL = "qwen4-embed"

# Fixed vector — a grant with a MODEL SCOPE. The box parses these exact bytes
# (the gateway's client-go/anchor.go pairingTerms.Models), so the key name and
# the sort position are part of the contract, not an implementation detail.
SCOPED_CANON = ('{"anchor":"entity-1.zone.example.test","caps":["inference"],'
                '"expires":"2026-10-14T12:00:00Z","issued_at":"2026-09-14T12:00:00Z",'
                '"kind":"pairing_grant","models":["qwen4-embed"],"server":"box-a",'
                '"subject":"a1b2c3d4e5f60718.zone.example.test","subject_handle":"a1b2c3d4e5f60718"}')


# ── stubs ────────────────────────────────────────────────────────────────────

class Pool:
    """Enough asyncpg surface for both services. Records every statement, and
    keeps ONE machine-offer row so the accept/mint paths have something real to
    read and update."""

    def __init__(self, *, anchor_host=ANCHOR, offer=None, redeem_out=None,
                 redeem_exit=0, redeem_err="", gateway_version="0.29.0", pairings=None):
        self.sql: list[tuple[str, tuple]] = []
        self.anchor_host = anchor_host
        self.offer = offer
        self.redeem_out = redeem_out
        self.redeem_exit, self.redeem_err = redeem_exit, redeem_err
        self.gateway_version = gateway_version
        self.pairings = pairings if pairings is not None else []
        self.next_id = 41

    async def execute(self, q, *a):
        self.sql.append((q, a))

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        if "FROM network_bastion_heartbeat" in q:
            # `pairings` is EMPTY on purpose. The box beats on a 60-second
            # ticker and a redeem triggers no beat, so a pairing created
            # seconds ago is in no report yet — a stub that listed it
            # unconditionally is what let the accept path look like it worked
            # when on a real pod it 404s at "the box has not reported it".
            return {"token_id": 7, "hostname": SERVER,
                    "anchor": json.dumps({"anchor_host": self.anchor_host, "owner": "Alex"})
                              if self.anchor_host else None,
                    "pairings": json.dumps(self.pairings),
                    "gateway_version": self.gateway_version}
        if "FROM network_machine_offers" in q:
            return self.offer
        return None

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        if "INSERT INTO" in q:
            self.next_id += 1
            return self.next_id
        return None

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        return []

    def acquire(self):
        pool = self

        class _Cx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False
        return _Cx()


class Relay:
    """The mgmt lane. Answers pairing_redeem with the box's JSON on stdout."""

    def __init__(self, *, exit_code=0, offline=False, stdout=None, stderr=""):
        self.calls: list[tuple[str, str, list]] = []
        self.timeouts: list = []
        self.exit_code, self.offline = exit_code, offline
        self.stdout = stdout if stdout is not None else json.dumps(
            {"state": "ok", "subject": SUBJECT, "handle": HANDLE, "pairing_state": "pending"})
        self.stderr = stderr

    async def __call__(self, pool, eid, server, op, args, timeout=None):
        self.calls.append((server, op, list(args)))
        self.timeouts.append(timeout)
        if self.offline:
            return {"exit": None, "stdout": "", "stderr": "no worker", "_offline": True}
        if self.exit_code != 0:
            return {"exit": self.exit_code, "stdout": "", "stderr": self.stderr or "refused"}
        return {"exit": 0, "stdout": self.stdout if op == pg.OP_REDEEM else "", "stderr": ""}


class PeerLane:
    """The direct host↔host lane. Records what was asked of the peer."""

    def __init__(self, *, token="skb_token", ok=True, error="nope", unavailable=False):
        self.calls: list[tuple[int, str]] = []
        self.token, self.ok, self.error, self.unavailable = token, ok, error, unavailable

    async def send_to_peer(self, peer_entity_id, op, slip, params, timeout=10.0):
        self.calls.append((int(peer_entity_id), op))
        if self.unavailable:
            raise _peer_client().PeerUnavailable("no route")
        if not self.ok:
            return {"ok": False, "error": self.error}
        return {"ok": True, "data": {"token": self.token, "expires_in_s": 900}}


def _peer_client():
    from server.peer import peer_client
    return peer_client


async def _fixed_sign(pool, eid, terms):
    canon = pg.canonical(terms)
    return base64.b64encode(canon).decode(), base64.b64encode(KEY.sign(canon)).decode()


def _wire(monkey, relay, *, stepup_ok=True, own=ANCHOR, peer=None):
    # BURN-ON-USE, like the real one (nac_stepup.consume_counter records the
    # code's counter and refuses anything at or below it). The first stub here
    # was a pure string compare, and that is precisely what hid the bug this
    # models: accept_inbound verified the code and then called a function that
    # verified it again, so on any host with an enrolled factor the second
    # check refused — AFTER the other host had minted and this box had
    # redeemed. A stub that says yes where the pod says no is not a test.
    burned: set[str] = set()

    async def _stepup(pool, code):
        if not (stepup_ok and code == "123456") or code in burned:
            return False
        burned.add(code)
        return True

    async def _own(eid):
        return () if own is None else (own,)

    async def _audit(pool, eid, server, op, *, exit_code, decision):
        pass

    async def _vk(pool, eid):
        return PUB_B64

    monkey.setattr(pg, "_verify_pubkey_b64", _vk)
    monkey.setattr(pg, "_sign", _fixed_sign)
    monkey.setattr(pg, "_verify_stepup", _stepup)
    monkey.setattr(pg, "_own_host_names", _own)
    monkey.setattr(pg, "_dispatch", relay)
    import server.network.nac_mgmt as nm
    monkey.setattr(nm, "audit_mgmt", _audit)
    if peer is not None:
        from server.peer import peer_client
        monkey.setattr(peer_client, "send_to_peer", peer.send_to_peer)

    async def _tell(*a, **k):
        return None
    monkey.setattr(mo, "_tell_them", _tell)


def _run(coro):
    return asyncio.run(coro)


#: What a beat looks like AFTER the box has reported the pairing — what
#: approve_pairing needs and accept_inbound must not.
_REPORTED = [{"host_name": SUBJECT, "handle": HANDLE, "state": "pending", "since": ""}]


def _offer_row(**over):
    row = {"id": 9, "direction": "in", "peer_entity_id": PEER, "peer_handle": "alice",
           "peer_display": "Alice", "purpose": "embeddings for my notes",
           "models_asked": json.dumps([MODEL]), "state": "open", "server": None,
           "subject": None, "granted_caps": None, "granted_models": None,
           "tokens_issued": 0, "last_token_at": None, "created_at": None,
           "decided_at": None, "decided_by": None, "detail": None}
    row.update(over)
    return row


# ── 1. the model scope reaches the signed bytes ──────────────────────────────

def test_a_scoped_grant_is_the_fixed_vector():
    terms = pg.grant_terms(anchor=ANCHOR, server=SERVER, subject=SUBJECT, subject_handle=HANDLE,
                           caps=["inference"], issued_at="2026-09-14T12:00:00Z",
                           expires="2026-10-14T12:00:00Z", models=[MODEL])
    canon = pg.canonical(terms)
    assert canon.decode() == SCOPED_CANON
    Ed25519PublicKey.from_public_bytes(base64.b64decode(PUB_B64)).verify(KEY.sign(canon), canon)


def test_an_unscoped_grant_carries_no_models_key_at_all():
    # Every grant signed before model scoping has no `models` key, and the box reads
    # the ABSENCE as unscoped. An empty list would canonicalize to different
    # bytes for a grant that means exactly the same thing.
    for models in (None, [], ()):
        terms = pg.grant_terms(anchor=ANCHOR, server=SERVER, subject=SUBJECT, subject_handle=HANDLE,
                               caps=["inference"], issued_at="2026-09-14T12:00:00Z", models=models)
        assert "models" not in terms
        assert b'"models"' not in pg.canonical(terms)


def test_models_normalize_to_a_fixed_order_and_refuse_a_shape_the_box_cannot_match():
    assert pg.normalize_models(["Qwen4-Embed", "qwen4-embed", "pii"]) == ["pii", "qwen4-embed"]
    assert pg.normalize_models("a,b") == ["a", "b"]
    assert pg.normalize_models(None) == [] and pg.normalize_models([]) == []
    # The string is signed into a grant the box matches live requests against, so
    # a name that would not mean on the box what it read like on the card is
    # refused HERE rather than silently mismatching there.
    for bad in ["two words", "a,b" if False else "a\tb", 'q"x', "back\\slash", "x" * 129]:
        try:
            pg.normalize_models([bad])
            assert False, f"accepted {bad!r}"
        except ValueError:
            pass


def test_describe_models_says_what_the_scope_means():
    assert pg.describe_models([]) == "every model this machine serves"
    assert pg.describe_models([MODEL]) == MODEL
    assert "sterilizer" in pg.describe_models([MODEL, "pii"])
    assert pg.PII_LANE_ENTRY == "pii"


# ── 2. approve carries the scope, and refuses a scope that means nothing ─────

def test_approve_signs_the_model_scope_and_stores_it(monkeypatch):
    relay, pool = Relay(), Pool(pairings=_REPORTED)
    _wire(monkeypatch, relay)
    res = _run(pg.approve_pairing(pool, 9003, SERVER, SUBJECT, ["inference"],
                                  stepup_code="123456", models=["Qwen4-Embed"]))
    assert res["approved"] and res["models"] == [MODEL]
    assert res["may_use"] == MODEL
    _server, op, args = relay.calls[0]
    assert op == pg.OP_GRANT_SYNC
    terms = json.loads(base64.b64decode(args[0]))
    assert terms["models"] == [MODEL], "the scope must be in the SIGNED bytes, not just the row"
    Ed25519PublicKey.from_public_bytes(base64.b64decode(PUB_B64)).verify(
        base64.b64decode(args[1]), base64.b64decode(args[0]))
    joined = "\n".join(q for q, _ in pool.sql)
    assert "models" in joined and "INSERT INTO nac_pairing_grants" in joined


def test_approve_refuses_models_without_the_inference_cap(monkeypatch):
    relay, pool = Relay(), Pool(pairings=_REPORTED)
    _wire(monkeypatch, relay)
    try:
        _run(pg.approve_pairing(pool, 9003, SERVER, SUBJECT, ["observe"],
                                stepup_code="123456", models=[MODEL]))
        assert False, "a model scope on a grant with no inference lane was accepted"
    except pg.PairingError as e:
        assert "inference" in e.detail
    assert relay.calls == [], "nothing may reach the box on a refusal"


def test_approve_with_no_models_is_unchanged(monkeypatch):
    relay, pool = Relay(), Pool(pairings=_REPORTED)
    _wire(monkeypatch, relay)
    res = _run(pg.approve_pairing(pool, 9003, SERVER, SUBJECT, ["inference"], stepup_code="123456"))
    assert res["models"] == []
    terms = json.loads(base64.b64decode(relay.calls[0][2][0]))
    assert "models" not in terms


# ── 3. the remote redeem ─────────────────────────────────────────────────────

def test_redeem_dispatches_the_anchor_only_op_and_returns_the_subject(monkeypatch):
    relay, pool = Relay(), Pool()
    _wire(monkeypatch, relay)
    res = _run(pg.redeem_on_box(pool, 9003, SERVER, "skb_abc"))
    assert res["subject"] == SUBJECT and res["state"] == "pending"
    server, op, args = relay.calls[0]
    assert (server, op) == (SERVER, "pairing_redeem"), \
        "the op id must stay inside the anchor-only 'pairing_' prefix the box gates on"
    assert args == ["skb_abc"]
    # The box waits on the platform's enrolment ingress inside this op, so the
    # relay must outlast that call rather than racing its deadline.
    assert relay.timeouts[0] and relay.timeouts[0] > 30.0


def test_redeem_refuses_on_a_machine_anchored_elsewhere(monkeypatch):
    relay, pool = Relay(), Pool()
    _wire(monkeypatch, relay, own="someone-else.zone.example.test")
    try:
        _run(pg.redeem_on_box(pool, 9003, SERVER, "skb_abc"))
        assert False, "redeemed on a machine this host does not anchor"
    except pg.PairingError as e:
        assert e.status == 403
    assert relay.calls == [], "the token must not be spent proving something we could check first"


def test_redeem_on_an_offline_box_says_so_rather_than_queueing(monkeypatch):
    relay, pool = Relay(offline=True), Pool()
    _wire(monkeypatch, relay)
    try:
        _run(pg.redeem_on_box(pool, 9003, SERVER, "skb_abc"))
        assert False, "an offline box reported success"
    except pg.PairingError as e:
        # Unlike a grant sync there is nothing to retry later: the token is
        # single-use and short-lived, and saying "it will land on the next
        # check-in" would be false.
        assert e.status == 503 and "again" in e.detail


def test_redeem_surfaces_the_boxs_refusal(monkeypatch):
    relay, pool = Relay(exit_code=126, stderr="refused: op is answered only on the anchor host's pairing"), Pool()
    _wire(monkeypatch, relay)
    try:
        _run(pg.redeem_on_box(pool, 9003, SERVER, "skb_abc"))
        assert False, "a refusal read as success"
    except pg.PairingError as e:
        assert "anchor" in e.detail


# ── 4. accept: the order is the security property ────────────────────────────

def test_accept_checks_the_second_factor_before_asking_anyone_to_mint(monkeypatch):
    relay, peer = Relay(), PeerLane()
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    try:
        _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                               models=[MODEL], stepup_code="000000"))
        assert False, "a wrong step-up code was accepted"
    except mo.OfferError as e:
        assert e.status == 403
    assert peer.calls == [], "a typo in the 2FA code must not cost the other side a single-use token"
    assert relay.calls == []


def test_accept_fetches_a_token_redeems_it_and_signs_the_scope(monkeypatch):
    relay, peer = Relay(), PeerLane(token="skb_fresh")
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    res = _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                                 models=[MODEL], stepup_code="123456"))
    assert res["accepted"] and res["subject"] == SUBJECT and res["models"] == [MODEL]
    assert res["may_use"] == MODEL
    # the token came from the DIRECT lane, and it went straight to the box
    assert peer.calls == [(PEER, mo.OP_TOKEN)]
    assert relay.calls[0][1] == pg.OP_REDEEM and relay.calls[0][2] == ["skb_fresh"]
    assert relay.calls[1][1] == pg.OP_GRANT_SYNC
    terms = json.loads(base64.b64decode(relay.calls[1][2][0]))
    assert terms["caps"] == ["inference"] and terms["models"] == [MODEL]
    # the offer is closed with what was actually granted
    assert any("state='accepted'" in q for q, _ in pool.sql)


def test_accept_refuses_an_approval_that_names_no_models(monkeypatch):
    relay, peer = Relay(), PeerLane()
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    try:
        _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                               models=[], stepup_code="123456"))
        assert False, "an unscoped approval went through the sharing path"
    except mo.OfferError as e:
        assert "EVERY model" in e.message
    assert peer.calls == []


def test_accept_refuses_a_ceiling_wider_than_the_inference_lane(monkeypatch):
    relay, peer = Relay(), PeerLane()
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    try:
        _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["dcn"],
                               models=[MODEL], stepup_code="123456"))
        assert False, "a data family rode in on a model-sharing card"
    except mo.OfferError as e:
        assert "inference" in e.message
    assert peer.calls == []


def test_accept_never_falls_back_to_the_platform_relay(monkeypatch):
    relay, peer = Relay(), PeerLane(unavailable=True)
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    try:
        _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                               models=[MODEL], stepup_code="123456"))
        assert False, "the direct lane was down and something still happened"
    except mo.OfferError as e:
        assert e.status == 503 and "relay" in e.message
    assert relay.calls == [], "nothing reached the machine"


def test_decline_touches_nothing_near_the_machine(monkeypatch):
    relay, peer = Relay(), PeerLane()
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    res = _run(mo.decline_inbound(pool, 9))
    assert res["declined"] and "nothing was written" in res["note"]
    assert relay.calls == [] and peer.calls == []
    assert any("state='declined'" in q for q, _ in pool.sql)


def test_an_already_answered_offer_cannot_be_answered_again(monkeypatch):
    pool = Pool(offer=_offer_row(state="accepted"))
    try:
        _run(mo.decline_inbound(pool, 9))
        assert False, "a decided offer was decided again"
    except mo.OfferError as e:
        assert e.status == 409


def _card_archives(pool):
    """The inbox-card archives the answer wrote, as (kind, peer, offer) triples,
    with the statements that carried them — so a test can say WHICH card was
    archived: the peer's unarchived request of that kind, sent no later than the
    answer, not the whole inbox. The kind is the literal the peer lane stores
    (handlers.py REQUEST_KINDS), not the module's constant, so a renamed
    constant that stopped matching real cards would be caught here."""
    out = []
    for q, a in pool.sql:
        if "UPDATE inbox_messages" in q and "archived_at" in q:
            assert "from_entity_id = $1" in q and "archived_at IS NULL" in q, q
            assert "message_kind = $2" in q, q
            assert "sent_at <= (SELECT decided_at FROM network_machine_offers WHERE id = $3)" in q, q
            out.append((a[1], a[0], a[2]))
    return out


# THE ANSWER IS THE ARCHIVE. The peer lane counts UNARCHIVED machine_request
# cards to decide whether the asker may knock again (one per peer per week);
# the offer row's state is not consulted. An accept or decline that left the
# card live kept the asker locked out for a week after this host had already
# answered — measured. Both answers archive the peer's card, scoped
# to that peer and that kind, after the offer row is closed.
#
# MUTATION: drop the _archive_cards call from decline_inbound.  RESULT: red.
def test_decline_archives_the_askers_card(monkeypatch):
    relay, peer = Relay(), PeerLane()
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    _run(mo.decline_inbound(pool, 9))
    assert _card_archives(pool) == [("machine_request", PEER, 9)]
    order = [q for q, _ in pool.sql if "state='declined'" in q or "archived_at" in q]
    assert "state='declined'" in order[0] and "archived_at" in order[1], order


# MUTATION: drop the _archive_cards call from accept_inbound.  RESULT: red.
def test_accept_archives_the_askers_card(monkeypatch):
    relay, peer = Relay(), PeerLane(token="skb_fresh")
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    res = _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                                 models=[MODEL], stepup_code="123456"))
    assert res["accepted"]
    assert _card_archives(pool) == [("machine_request", PEER, 9)]
    order = [q for q, _ in pool.sql if "state='accepted'" in q or "archived_at" in q]
    assert "state='accepted'" in order[0] and "archived_at" in order[1], order


# A refused answer archives nothing: the card is still the user's to act on.
def test_a_refused_answer_leaves_the_card(monkeypatch):
    relay, peer = Relay(), PeerLane()
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    try:
        _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                               models=[MODEL], stepup_code="000000"))
        assert False, "a wrong step-up code was accepted"
    except mo.OfferError:
        pass
    assert _card_archives(pool) == []


# The archive is best-effort: a database that refuses the archive does not
# turn a recorded answer into an error the user sees.
def test_a_failed_archive_never_fails_the_answer(monkeypatch):
    relay, peer = Relay(), PeerLane()

    class _Pool(Pool):
        async def execute(self, q, *a):
            await super().execute(q, *a)
            if "UPDATE inbox_messages" in q:
                raise RuntimeError("inbox is read-only right now")
    pool = _Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    res = _run(mo.decline_inbound(pool, 9))
    assert res["declined"]


# ── 5. minting: the open offer IS the authorization, and it is bounded ───────

def test_mint_refuses_a_peer_with_no_open_offer_from_us():
    pool = Pool(offer=None)
    try:
        _run(mo.mint_for_peer(pool, 9003, PEER))
        assert False, "a host with no outstanding ask got an enrolment token"
    except mo.OfferError as e:
        assert e.status == 403


class ClaimPool(Pool):
    """A Pool that answers the claiming UPDATE the way Postgres would: the row
    is claimed once, and a caller who arrives after that finds nothing still
    'open' and gets zero rows back."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.claims: list[str] = []

    async def fetchrow(self, q, *a):
        if "UPDATE network_machine_offers SET state='spent'" in q:
            self.sql.append((q, a))
            self.claims.append(q)
            if self.offer and self.offer["state"] == "open":
                self.offer = {**self.offer, "state": "spent"}
                return {"id": self.offer["id"]}
            return None
        return await super().fetchrow(q, *a)


def _with_minter(monkey, token="skb_one", boom=False):
    """Stand in for the platform's token mint."""
    import server.network.bastion_enroll as be

    async def _handle(eid):
        return "me"

    async def _mint(pool, *, entity_id, handle, name):
        if boom:
            raise RuntimeError("no handle yet")
        return {"token": token, "expires_in_s": 900, "name": name}
    monkey.setattr(be, "resolve_self_handle", _handle)
    monkey.setattr(be, "mint_pairing_token", _mint)


def test_one_ask_yields_exactly_one_token_and_the_claim_is_atomic(monkeypatch):
    """The offer row IS the permit, and claiming it IS the mint.

    The first cut counted tokens in Python between a SELECT and an UPDATE, and
    PeerServer serves every connection as its own task with no lock — so N
    simultaneous calls all read zero, all passed, and all minted. A cap you can
    step around by arriving twice at once is not a cap. One statement now moves
    the row out of 'open' and hands back its id, so exactly one caller wins."""
    _with_minter(monkeypatch)
    pool = ClaimPool(offer=_offer_row(direction="out"))
    assert _run(mo.mint_for_peer(pool, 9003, PEER))["token"] == "skb_one"
    for _ in range(3):
        try:
            _run(mo.mint_for_peer(pool, 9003, PEER))
            assert False, "a spent ask minted again"
        except mo.OfferError as e:
            assert e.status == 403
    assert len(pool.claims) == 4, "every attempt must go through the one claiming statement"
    # The claim and the mint are ONE statement, not a read followed by a write.
    assert all("FOR UPDATE SKIP LOCKED" in q for q in pool.claims)


def test_a_mint_that_fails_puts_the_permit_back(monkeypatch):
    """The permit is spent before the token exists, so a mint that raises would
    otherwise kill the user's ask with nothing to show for it."""
    _with_minter(monkeypatch, boom=True)
    pool = ClaimPool(offer=_offer_row(direction="out"))
    try:
        _run(mo.mint_for_peer(pool, 9003, PEER))
        assert False, "the mint raised and nobody noticed"
    except RuntimeError:
        pass
    assert any("SET state='open'" in q for q, _ in pool.sql), \
        "the ask was left spent after a mint that never happened"


def test_mint_returns_the_token_and_writes_it_nowhere(monkeypatch):
    _with_minter(monkeypatch, token="skb_secret")
    pool = ClaimPool(offer=_offer_row(direction="out"))
    assert _run(mo.mint_for_peer(pool, 9003, PEER))["token"] == "skb_secret"
    assert any("tokens_issued=tokens_issued+1" in q for q, _ in pool.sql)
    # the secret is in no statement this host ran
    for q, a in pool.sql:
        assert "skb_secret" not in q and "skb_secret" not in repr(a), \
            "a single-use machine credential was written into a statement"


# ── 5b. the three the stubs used to hide ─────────────────────────────────────

def test_the_step_up_code_is_spent_exactly_once(monkeypatch):
    """The code is BURN-ON-USE (nac_stepup.consume_counter). accept_inbound
    verified it and then called approve_pairing, which verified it again — so on
    any host with an enrolled factor the second check refused a code the user
    had typed correctly, AFTER the other host had minted and this box had
    redeemed. The user was told their code was wrong; the box held a pending
    pairing; the only way out was approving it by hand."""
    spent = []
    relay, peer = Relay(), PeerLane()
    pool = Pool(offer=_offer_row())
    _wire(monkeypatch, relay, peer=peer)
    real = pg._verify_stepup

    async def counting(p, code):
        spent.append(code)
        return await real(p, code)
    monkeypatch.setattr(pg, "_verify_stepup", counting)
    res = _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                                 models=[MODEL], stepup_code="123456"))
    assert res["accepted"], res
    assert spent == ["123456"], f"the code was checked {len(spent)} times; it can only work once"


def test_accept_does_not_need_a_heartbeat_that_cannot_have_arrived(monkeypatch):
    """The box beats on a 60-second ticker and a redeem triggers no beat, so the
    pairing this flow just created is in no report. approve_pairing resolves its
    subject FROM that report, which is why accept_inbound must not go through
    it — the redeem answer already names the subject."""
    relay, peer = Relay(), PeerLane()
    pool = Pool(offer=_offer_row(), pairings=[])   # the box has reported nothing
    _wire(monkeypatch, relay, peer=peer)
    res = _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                                 models=[MODEL], stepup_code="123456"))
    assert res["accepted"] and res["subject"] == SUBJECT
    terms = json.loads(base64.b64decode(relay.calls[1][2][0]))
    assert terms["subject"] == SUBJECT and terms["subject_handle"] == HANDLE, terms
    # ...and the plain approve path still DOES require it, unchanged.
    try:
        _run(pg.approve_pairing(Pool(pairings=[]), 9003, SERVER, SUBJECT, ["inference"],
                                stepup_code="123456"))
        assert False, "approve_pairing admitted a host the box never reported"
    except pg.PairingError as e:
        assert e.status == 404


def test_a_scope_is_refused_for_a_box_that_would_ignore_it(monkeypatch):
    """A pre-0.29 bastion has no Models field, so json.Unmarshal drops the key:
    the signature still verifies, the grant still applies, and the friend gets
    EVERY model while the user was shown one. A ceiling nobody enforces is worse
    than no ceiling, because it is the one the user believes."""
    for version, why in (("0.28.0", "older"), ("0.9.9", "an older line"), (None, "unknown")):
        relay, peer = Relay(), PeerLane()
        pool = Pool(offer=_offer_row(), gateway_version=version)
        _wire(monkeypatch, relay, peer=peer)
        try:
            _run(mo.accept_inbound(pool, 9003, 9, server=SERVER, caps=["inference"],
                                   models=[MODEL], stepup_code="123456"))
            assert False, f"a scoped grant was signed for a {why} box"
        except pg.PairingError as e:
            assert e.status == 409, (version, e.detail)
        assert peer.calls == [] and relay.calls == [], \
            f"the {why} box case reached the wire before the version was read"
    # 0.29.0 and above is fine, and so is an UNSCOPED grant on an old box —
    # nothing to enforce there, so nothing to misrepresent.
    relay, peer = Relay(), PeerLane()
    _wire(monkeypatch, relay, peer=peer)
    assert _run(mo.accept_inbound(Pool(offer=_offer_row(), gateway_version="0.30.1"), 9003, 9,
                                  server=SERVER, caps=["inference"], models=[MODEL],
                                  stepup_code="123456"))["accepted"]
    relay2 = Relay()
    _wire(monkeypatch, relay2)
    assert _run(pg.approve_pairing(Pool(pairings=_REPORTED, gateway_version="0.28.0"), 9003,
                                   SERVER, SUBJECT, ["inference"], stepup_code="123456"))["approved"]


def test_version_compare_is_numeric_not_lexical():
    # "0.9" sorts ABOVE "0.29" as a string, which would pass every box on an
    # older line — the same shape as the bug the bastion update lane carries.
    assert pg._version_tuple("0.9.9") < pg.MODELS_MIN_BASTION
    assert pg._version_tuple("0.29.0") >= pg.MODELS_MIN_BASTION
    assert pg._version_tuple("0.30") >= pg.MODELS_MIN_BASTION
    assert pg._version_tuple("1.0.0") >= pg.MODELS_MIN_BASTION
    for junk in (None, "", "dev", "0.29.0-rc1", "v0.29.0"):
        assert pg._version_tuple(junk) is None, junk


# ── 6. what the boundary accepts from the other side ─────────────────────────

def test_the_askers_words_are_bounded_and_stripped():
    assert mo.clean_purpose("hello\nworld") == "helloworld"
    assert len(mo.clean_purpose("x" * 5000)) == mo.MAX_PURPOSE
    assert mo.clean_models_asked(["a\nb", "c"]) == ["ab", "c"]
    assert len(mo.clean_models_asked(["m"] * 50)) == mo.MAX_MODELS_ASKED
    assert mo.clean_models_asked("a,b") == ["a", "b"]
    assert mo.clean_models_asked(None) == [] and mo.clean_models_asked(7) == []


def test_the_peer_lane_classifies_the_new_op_rather_than_defaulting_it():
    # peer_server dispatches an op only if it is registered AND classified —
    # either mapped to a slip action or explicitly slipless. An op that is
    # neither would be refused, and defaulting one to "no slip needed" is how a
    # gated op silently becomes ungated. Both halves, pinned.
    from server.peer import peer_server, handlers
    assert "machine.token" in handlers.PEER_HANDLERS
    assert "machine.token" in peer_server._SLIPLESS_OPS
    assert "machine_request" in handlers.REQUEST_KINDS
    assert "machine_request" in handlers._KINDS_NEEDING_A_CONTACT, \
        "asking for someone's hardware is not a first contact — it needs a relationship"
    assert "models_asked" in handlers._ALLOWED_PAYLOAD_KEYS


def test_a_machine_request_from_a_stranger_is_refused(monkeypatch):
    from server.peer import handlers

    async def _no_contact(peer_id):
        return False

    async def _boom(*a, **k):
        raise AssertionError("a stranger's ask reached the offer table")
    monkeypatch.setattr(handlers, "_connected_with", _no_contact)
    monkeypatch.setattr(handlers, "_record_machine_offer", _boom)
    res = _run(handlers.inbox_request(PEER, None, {"message_kind": "machine_request"}))
    assert res["ok"] is False and "connect" in res["error"]


def test_a_machine_request_writes_the_row_the_card_is_answered_from(monkeypatch):
    """The card is a button; the ROW is what the button acts on. A knock that
    wrote only the card would render an Approve the user could press to no
    effect — and the row is also what bounds a peer to one open ask."""
    from server.peer import handlers
    seen, appended = [], []

    async def _connected(peer_id):
        return True

    async def _record(pool, peer_entity_id, *, purpose, models_asked, peer_handle="", peer_display=""):
        seen.append({"peer": peer_entity_id, "purpose": purpose, "models": models_asked})
        return {"offer_id": 9}

    async def _append(payload):
        appended.append(payload)
        return {"ok": True, "message_id": "m"}

    async def _ident(peer_id):
        return {"from_display_name": "Alice", "from_handle": "alice"}

    # `group_id` is keyword-only and passed on every call (handlers._append_knock
    # narrows the slot per group for a group_join). The stub mirrors the real
    # signature rather than swallowing it with **kwargs, so a future change to
    # how the slot is keyed shows up here instead of being absorbed.
    async def _slot(peer_id, kind, *, group_id=None):
        return True
    monkeypatch.setattr(handlers, "_connected_with", _connected)
    monkeypatch.setattr(mo, "record_inbound", _record)
    monkeypatch.setattr(handlers, "handle_inbox_append", _append)
    monkeypatch.setattr(handlers, "_identify", _ident)
    monkeypatch.setattr(handlers, "_knock_slot_free", _slot)
    from server import brain_service

    class _P:
        pass
    monkeypatch.setattr(brain_service, "get_pool", lambda: _P())

    res = _run(handlers.inbox_request(PEER, None, {
        "message_kind": "machine_request", "body": "embeddings\nfor notes",
        "payload": {"models_asked": ["qwen4-embed", "x\ny"], "from_display_name": "NOT ME"}}))
    assert res.get("ok") is not False
    assert seen and seen[0]["peer"] == PEER
    # the asker's words are sanitised before they are stored OR shown
    assert seen[0]["models"] == ["qwen4-embed", "xy"]
    card = appended[0]["payload"]
    assert card["models_asked"] == ["qwen4-embed", "xy"]
    # ...and the name on the card still comes from OUR records, not theirs
    assert card["from_display_name"] == "Alice"


# ── 7. an empty agent list means four things, and only one is "quiet" ───────

def test_a_machine_that_withholds_what_runs_on_it_never_reads_as_quiet():
    """What runs on a machine is gated on `observe` now, so a box can be
    watching perfectly well and simply not be telling THIS host. The Linux
    fallback in _bastion_watch_state would have called that 'watching' and then
    shown an empty list — the quiet-vs-blind confusion that function exists to
    prevent, in a third flavour."""
    from mastercode.runtime.tools import _bastion_watch_state

    def box(**over):
        b = {"online": True, "platform": "linux", "capabilities": [],
             "enforcing": {"anchored": True, "is_anchor": False,
                           "pairing_caps": ["inference"]}}
        b.update(over)
        return b

    state, why = _bastion_watch_state(box())
    assert state == "withheld", state
    assert "not reporting it to this host" in why and "never" in why
    # Granted observe, it reads as watching again.
    assert _bastion_watch_state(box(capabilities=["agent-observe"]))[0] == "watching"
    assert _bastion_watch_state(
        box(enforcing={"anchored": True, "is_anchor": False,
                       "pairing_caps": ["inference", "observe"]}))[0] == "watching"
    # The anchor, and an unanchored box, are untouched — 19 of the fleet's 22.
    assert _bastion_watch_state(
        box(enforcing={"anchored": True, "is_anchor": True, "pairing_caps": []}))[0] == "watching"
    assert _bastion_watch_state(box(enforcing={})) [0] == "watching"
    # A box that genuinely cannot look is still BLIND, not withheld — the two
    # have different answers ("update it" vs "it is not ours to see").
    assert _bastion_watch_state(box(platform="windows", enforcing={}))[0] == "blind"
    # Offline beats everything.
    assert _bastion_watch_state(box(online=False))[0] == "unknown"


# ── runner ───────────────────────────────────────────────────────────────────

class _Monkey:
    """The three-line monkeypatch pytest gives us, for the script lane."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


if __name__ == "__main__":
    import inspect
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        mk = _Monkey()
        try:
            if "monkeypatch" in inspect.signature(fn).parameters:
                fn(mk)
            else:
                fn()
            print(f"OK   {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
        finally:
            mk.undo()
    print(f"\nFAILURES: {failures} / "
          f"{len([n for n in globals() if n.startswith('test_')])}")
    sys.exit(1 if failures else 0)
