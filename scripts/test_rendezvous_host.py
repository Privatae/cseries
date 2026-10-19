#!/usr/bin/env python3
"""Gate — rendezvous, the Host half.

Container-free: a recording fake pool, no DB, no network, no docker. It drives
the REAL functions in server/groups/rendezvous.py and reads what they produced;
it never supplies the value it then asserts on.

What it pins, and why each one is here rather than assumed:

  1. NORMALISATION IS A FIXED POINT. normalise(normalise(x)) == normalise(x).
     This is the property that makes it safe to store normalised and merely
     RE-VALIDATE on the platform instead of re-transforming there. If it were
     not a fixed point the two ends would disagree about what a tag is, and
     that disagreement is invisible from either end.
  2. NORMALISATION TARGETS THE FAR SIDE'S RULE. Every tag this module accepts
     must satisfy communityd's `validSlug` (community/forum/groups.go:348-359 in
     the platform source — a-z, 0-9, '-', '_', length 1..64), re-implemented here as
     an INDEPENDENT predicate rather than imported, because the point is to
     catch the host's normaliser drifting away from the Go rule. A tag that
     normalises to something validSlug refuses would be listed successfully and
     then refused at group-creation time, which nobody can see from either end.
  3. Inputs differing only in case or whitespace collapse to ONE tag, not two.
  4. THE SIGNED TERMS CARRY SORTED TAGS. One set is one signature — so a user
     who re-lists the same interests in a different order does not produce
     different bytes and does not look to the platform like a change.
  5. THE SIGNATURE IS MADE AND CHECKED THROUGH THE PRODUCTION PATH. A real
     Ed25519 key, the real nac_stepup canonical form, the real signer. Then
     EVERY signed field is mutated in turn and every mutation must fail
     verification. A test that hands a verifier bytes it built itself cannot
     test the signer.
  6. seq IS MONOTONIC AND NEVER RESET. It is the anti-replay: without it an old
     signed listing could be replayed to resurrect a tag the user withdrew.
  7. SET IS REPLACE, NOT MERGE. A tag dropped from the list is stamped
     withdrawn — the same rule roster.apply_roster_snapshot records, because
     under a merge REMOVAL is the one change that can never travel, and here
     removal is the change the user cares about most.
  8. THE INTRODUCTION PAYLOAD NAMES NO IDENTITIES. `others` is a count and the
     reader exposes no field that could carry who they are. Asserted on the key
     set, structurally, so a later field cannot be added quietly.
  9. accept_intro TAKES THE GROUP FROM THE STORED CARD, NEVER THE CALLER, and
     sends the integer group_id community.group.join actually requires
     (bridge/adapter.py:8957-8963 parses params["group_id"] as an int).
 10. THE message_kind VOCABULARY ADMITS 'rendezvous_intro' IN BOTH
     migrations/v155_*.sql AND init-db.sql. A divergence there is invisible on a
     fresh pod and fatal on an existing one.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


#: communityd's validSlug, re-stated INDEPENDENTLY. Deliberately not imported
#: from the module under test: importing its own rule back into the assertion
#: would make this check tautological, and the thing being guarded against is
#: precisely the host rule drifting from the Go one.
_GO_VALID_SLUG = re.compile(r"^[a-z0-9_-]{1,64}$")


class RvPool:
    """A fake pool holding the interest rows and the seq counter, answering on
    the SQL the production code actually writes — so a query that changes shape
    stops being answered rather than silently passing."""

    def __init__(self, tags=None, seq=0, cards=None):
        self.tags = {t: {"tag": t, "listed_at": None, "last_error": None,
                         "created_at": _now(), "withdrawn_at": None}
                     for t in (tags or [])}
        self.seq = seq
        self.cards = cards or []
        self.execs = []

    def _live(self):
        return sorted(t for t, r in self.tags.items() if r["withdrawn_at"] is None)

    async def fetch(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "FROM rendezvous_interests" in s:
            rows = [dict(r) for r in self.tags.values()]
            if "withdrawn_at IS NULL" in s:
                rows = [r for r in rows if r["withdrawn_at"] is None]
            return sorted(rows, key=lambda r: r["tag"])
        if "FROM inbox_messages" in s and "rendezvous_intro" in s:
            return [dict(c) for c in self.cards]
        return []

    async def fetchrow(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "FROM inbox_messages" in s and "rendezvous_intro" in s:
            for c in self.cards:
                if str(c["message_id"]) != str(a[0]):
                    continue
                # Honoured only when the REAL query asks for it, so dropping the
                # predicate from production stops this row being filtered here
                # rather than silently passing.
                if "archived_at IS NULL" in s and c.get("archived_at") is not None:
                    return None
                return {"payload": c["payload"]}
            return None
        return None

    async def fetchval(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "INSERT INTO rendezvous_state" in s:
            self.seq += 1
            return self.seq
        return None

    async def execute(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "INSERT INTO rendezvous_interests" in s:
            # The real statement is a set insert (unnest of a text[]), so the
            # arg is a LIST. Matched on that shape deliberately: a revert to
            # one-statement-per-tag would bind a bare string and stop being
            # answered here rather than quietly passing.
            for t in (a[0] if isinstance(a[0], (list, tuple)) else [a[0]]):
                if t in self.tags:
                    self.tags[t]["withdrawn_at"] = None
                else:
                    self.tags[t] = {"tag": t, "listed_at": None, "last_error": None,
                                    "created_at": _now(), "withdrawn_at": None}
            return "INSERT 0 1"
        if "UPDATE rendezvous_interests SET withdrawn_at = now()" in s:
            if "NOT (tag = ANY" in s:
                keep = set(a[0])
                for t, r in self.tags.items():
                    if t not in keep and r["withdrawn_at"] is None:
                        r["withdrawn_at"] = _now()
            elif a:
                r = self.tags.get(a[0])
                if r and r["withdrawn_at"] is None:
                    r["withdrawn_at"] = _now()
            else:
                for r in self.tags.values():
                    if r["withdrawn_at"] is None:
                        r["withdrawn_at"] = _now()
            return "UPDATE 1"
        if "UPDATE rendezvous_interests SET listed_at" in s:
            for r in self.tags.values():
                if r["withdrawn_at"] is None:
                    r["listed_at"] = _now()
                    r["last_error"] = None
            return "UPDATE 1"
        return "UPDATE 0"


async def main() -> int:
    from server.groups import rendezvous as RZ

    # ── 1-3. Normalisation ────────────────────────────────────────────────
    print("\n-- normalisation --")
    cases = ["Bonsai", "  home lab  ", "Home   Lab", "HOME-LAB", "3d printing",
             "rust", "machine_learning", "café", "a" * 80, "", "---", "!!!"]
    for raw in cases:
        n = RZ.normalise(raw)
        if n is not None:
            check(RZ.normalise(n) == n,
                  f"normalise is a fixed point for {raw!r}", f"-> {n!r} -> {RZ.normalise(n)!r}")
            check(bool(_GO_VALID_SLUG.match(n)),
                  f"normalise({raw!r}) satisfies communityd validSlug", f"-> {n!r}")

    check(RZ.normalise("Home   Lab") == RZ.normalise("home-lab") == "home-lab",
          "case and whitespace collapse to ONE tag",
          f"{RZ.normalise('Home   Lab')!r} / {RZ.normalise('home-lab')!r}")
    check(RZ.normalise("a" * 80) is None,
          "a tag longer than validSlug allows is refused, not truncated")
    check(RZ.normalise("!!!") is None, "a tag with nothing usable in it is refused")
    check(RZ.normalise_all(["Bonsai", "bonsai", "BONSAI"]) == ["bonsai"],
          "normalise_all deduplicates after normalising")
    check(RZ.normalise_all(["zebra", "apple"]) == ["apple", "zebra"],
          "normalise_all SORTS — one set is one signature")

    # ── 4-6. Signing through the production path ──────────────────────────
    print("\n-- signing --")
    import base64
    import json as _json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)

    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())

    # Supply ONLY the key material. The canonical form, the terms and the
    # signature all come from production code.
    from server.network import nac_stepup
    import server.network.nac_stepup as _ns
    _ns.ensure_entity_signing_key = lambda pool, eid: asyncio.sleep(0, result=priv_pem)

    pool = RvPool(tags=["bonsai", "home-lab"], seq=4)
    signed = await RZ.sign_listing(pool, 9122, await RZ.live_tags(pool))
    terms = signed["terms"]
    check(terms["tags"] == ["bonsai", "home-lab"], "signed terms carry sorted tags", terms["tags"])
    check(terms["seq"] == 5, "seq bumped past the stored value", terms["seq"])
    check(terms["entity"] == 9122 and terms["kind"] == RZ.LISTING_KIND,
          "signed terms name this entity and the listing kind")

    canon = nac_stepup._canonical(terms)
    sig = base64.b64decode(signed["signature"])
    ok = True
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, canon)
    except Exception:
        ok = False
    check(ok, "a listing signed by the production path verifies under the real key")

    # THE FROZEN VECTOR. The literal below is the canonical JSON every
    # implementation of the listing terms must produce, byte for byte. Canonical
    # JSON written in more than one language drifts (see pairing_grants.py), so
    # each implementation freezes the SAME bytes and any one of them moving turns
    # its own gate red.
    FROZEN = (
        b'{"entity":9122,"kind":"rendezvous_listing",'
        b'"nonce":"0123456789abcdef0123456789abcdef","seq":7,'
        b'"signed_at":"2026-09-17T12:00:00+00:00",'
        b'"tags":["bonsai","home-lab"],"v":1}'
    )
    frozen_terms = RZ.listing_terms(
        entity_id=9122, tags=["bonsai", "home-lab"], seq=7,
        nonce="0123456789abcdef0123456789abcdef",
        signed_at="2026-09-17T12:00:00+00:00")
    check(nac_stepup._canonical(frozen_terms) == FROZEN,
          "the canonical bytes for the fixed vector match the frozen literal",
          nac_stepup._canonical(frozen_terms))

    # Mutate EVERY signed field in turn; every mutation must fail.
    for field, bad in [("v", 2), ("kind", "something_else"), ("entity", 9123),
                       ("tags", ["bonsai"]), ("seq", 6), ("nonce", "00" * 16),
                       ("signed_at", _now().isoformat())]:
        mutated = {**terms, field: bad}
        if mutated[field] == terms[field]:
            continue
        bad_ok = True
        try:
            Ed25519PublicKey.from_public_bytes(pub_raw).verify(
                sig, nac_stepup._canonical(mutated))
        except Exception:
            bad_ok = False
        check(not bad_ok, f"mutating the signed field {field!r} breaks the signature")

    again = await RZ.sign_listing(pool, 9122, ["bonsai"])
    check(again["terms"]["seq"] == 6, "seq is monotonic across listings",
          again["terms"]["seq"])
    check(again["terms"]["nonce"] != terms["nonce"],
          "two listings never share a nonce")

    # ── 7. Replace, not merge ─────────────────────────────────────────────
    print("\n-- set_interests replaces --")
    pool = RvPool(tags=["bonsai", "home-lab", "rust"])
    out = await RZ.set_interests(pool, ["bonsai", "Rust", "welding"])
    check(out["tags"] == ["bonsai", "rust", "welding"],
          "set_interests returns the normalised, sorted set", out["tags"])
    check(pool.tags["home-lab"]["withdrawn_at"] is not None,
          "a tag dropped from the list is stamped withdrawn")
    check(pool.tags["bonsai"]["withdrawn_at"] is None, "a kept tag stays live")
    check("welding" in pool.tags and pool.tags["welding"]["withdrawn_at"] is None,
          "a newly named tag is added live")
    check(await RZ.live_tags(pool) == ["bonsai", "rust", "welding"],
          "the live set equals what was asked for")

    re_added = await RZ.set_interests(pool, ["bonsai", "home-lab", "rust", "welding"])
    check(pool.tags["home-lab"]["withdrawn_at"] is None,
          "re-adding a withdrawn tag revives the SAME row, not a new one")
    check(re_added["tags"].count("home-lab") == 1, "and it appears exactly once")

    pool2 = RvPool(tags=["a", "b"])
    await RZ.withdraw(pool2, None)
    check(all(r["withdrawn_at"] is not None for r in pool2.tags.values()),
          "withdraw(None) withdraws every tag")

    # BOTH ENDS MUST BE TOLD ABOUT THE SAME TAG. The first cut had the route
    # hand withdraw_remote the caller's RAW query string while the local
    # withdraw normalised it, so `?tag=Bonsai` withdrew "bonsai" here and asked
    # the platform for "Bonsai" — which the platform refuses with a 400,
    # correctly, because that end validates and never transforms. Local and
    # platform state then disagreed with nothing on screen to say so.
    asked = {}

    async def _capture(op, params):
        asked["op"] = op
        asked["params"] = params
        return {"ok": True}

    real_bridge = RZ._bridge
    RZ._bridge = _capture
    pool2b = RvPool(tags=["bonsai"], seq=0)
    await RZ.withdraw_remote(pool2b, 9122, "Bonsai")
    check(asked["params"]["tag"] == "bonsai",
          "withdraw_remote NORMALISES the tag it sends — a raw 'Bonsai' would "
          "be refused by the platform while the local row was already gone",
          asked["params"]["tag"])
    check(asked["params"]["terms"]["tags"] == ["bonsai"],
          "and the SIGNED terms carry the normalised tag too, so the signature "
          "covers what the platform will match on",
          asked["params"]["terms"]["tags"])
    rejected = False
    try:
        await RZ.withdraw_remote(pool2b, 9122, "!!!")
    except RZ.RendezvousError:
        rejected = True
    check(rejected, "a tag that normalises to nothing is refused, not sent blank")
    RZ._bridge = real_bridge

    # A DROPPED TAG IS REPORTED. When at least one tag survives, the call
    # succeeds — so a 70-character tag that normalises to nothing would vanish
    # and the user would be told "listed", discoverable only by diffing the
    # returned list against what they typed.
    pool2c = RvPool()
    out2 = await RZ.set_interests(pool2c, ["bonsai", "a" * 70, "!!!"])
    check(out2["tags"] == ["bonsai"], "only the usable tag is stored", out2["tags"])
    check(len(out2.get("dropped") or []) == 2,
          "and BOTH unusable tags are reported back rather than swallowed",
          out2.get("dropped"))

    # ONE statement, not one per tag. The tag count is uncapped by design, so
    # the bound that protects the pod has to be on the work.
    pool2d = RvPool()
    await RZ.set_interests(pool2d, [f"tag{i}" for i in range(50)])
    inserts = [e for e in pool2d.execs if "INSERT INTO rendezvous_interests" in " ".join(e[0].split())]
    check(len(inserts) == 1,
          "50 tags are one INSERT, not 50 round trips", len(inserts))

    # ── 8-9. Introductions leak no identities ─────────────────────────────
    print("\n-- introductions --")
    import uuid
    mid = uuid.uuid4()
    card_payload = {"group_id": 41, "group_slug": "bonsai",
                    "group_title": "bonsai", "tag": "bonsai", "others": 4}
    pool3 = RvPool(cards=[{"message_id": mid, "subject": "", "body": "",
                           "payload": card_payload, "sent_at": _now()}])
    intros = await RZ.introductions(pool3)
    check(len(intros) == 1, "the introduction is read off inbox_messages")
    allowed = {"message_id", "group_id", "group_slug", "group_title", "tag",
               "arrived_at", "others"}
    check(set(intros[0]) == allowed,
          "the introduction reader exposes EXACTLY the no-identity key set",
          sorted(set(intros[0]) ^ allowed))
    check(isinstance(intros[0]["others"], int),
          "`others` is a count, not a collection", type(intros[0]["others"]).__name__)

    sent = {}

    async def _fake_bridge(op, params):
        sent["op"] = op
        sent["params"] = params
        return {"ok": True}

    RZ._bridge = _fake_bridge
    import server.bridge.memory_handlers as _mh
    _mh.handle_inbox_archive = lambda p: asyncio.sleep(0, result={"ok": True})
    import server.brain_service as _bs
    _bs._entity_id = 9122
    await RZ.accept_intro(pool3, str(mid))
    check(sent.get("op") == "community.group.join",
          "accepting an introduction joins through the EXISTING community op",
          sent.get("op"))
    check(sent["params"].get("group_id") == 41,
          "it sends the integer group_id community.group.join requires",
          sent["params"].get("group_id"))
    check(isinstance(sent["params"].get("group_id"), int),
          "and it is an int, not the slug")
    check("verify_pubkey" in sent["params"],
          "the verify key rides, so the joiner is never a member that cannot vote")

    # An ACCEPTED introduction cannot be accepted again: accepting archives the
    # card, and without `archived_at IS NULL` each re-accept is another
    # community.group.join for a group this host is already in.
    pool3b = RvPool(cards=[{"message_id": mid, "subject": "", "body": "",
                            "payload": card_payload, "sent_at": _now(),
                            "archived_at": _now()}])
    refused_archived = False
    try:
        await RZ.accept_intro(pool3b, str(mid))
    except RZ.RendezvousError:
        refused_archived = True
    check(refused_archived,
          "an already-answered introduction is refused, not joined a second time")

    bad = uuid.uuid4()
    pool4 = RvPool(cards=[{"message_id": bad, "subject": "", "body": "",
                           "payload": {"group_slug": "x"}, "sent_at": _now()}])
    refused = False
    try:
        await RZ.accept_intro(pool4, str(bad))
    except RZ.RendezvousError:
        refused = True
    check(refused, "an introduction naming no integer group id is refused, not guessed")

    # ── 9b. Route declaration ORDER ───────────────────────────────────────
    # FastAPI matches in declaration order, so every LITERAL segment under
    # /api/groups has to be registered ahead of the `/{group_id}` template that
    # would otherwise swallow it — `/api/groups/rendezvous` would answer 400
    # "malformed group id". Two route files already carry that reasoning as a
    # comment; a comment does not survive someone tidying the file, so it is
    # asserted here. Read structurally off the decorators rather than by
    # importing the router, which would drag in the whole app.
    print("\n-- route declaration order --")
    import ast
    tree = ast.parse(open(os.path.join(_ROOT, "server", "routes", "groups.py")).read())
    decls = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr in ("get", "post", "delete", "put", "patch")
                        and d.args and isinstance(d.args[0], ast.Constant)):
                    decls.append((node.lineno, d.args[0].value))
    decls.sort()
    templates = [ln for ln, path in decls if path.startswith("/{group_id}")]
    check(bool(templates), "the /{group_id} template routes are still there")
    first_template = min(templates)
    shadowed = [(ln, p) for ln, p in decls
                if ln > first_template and p.startswith("/")
                and not p.startswith("/{")]
    check(not shadowed,
          "every literal route under /api/groups is declared BEFORE the "
          "/{group_id} template that would shadow it",
          shadowed)
    literals = [p for ln, p in decls if p.startswith("/rendezvous")]
    check(len(literals) >= 4,
          "the rendezvous routes are among them", literals)

    # ── 9c. A STRANGER'S CARD SURVIVES A DENY POSTURE ─────────────────────
    # The defect this pins was live in the first cut of this branch and would
    # have shipped a feature that does not work for its only case.
    # domain_policy.check_inbound_allowed returns True early for kinds in
    # _REQUEST_KINDS and otherwise falls through to the per-peer rule, then the
    # active-contact check, then the `*` default. A group_join comes from
    # someone who is BY DEFINITION not a contact and has no rule, so under a
    # `*` row with allow_inbox = FALSE it was denied: the card never reached the
    # inbox and the owner never learned they had been asked. Same reasoning for
    # the platform's rendezvous introduction.
    #
    # Driven against the REAL check_inbound_allowed with a fake pool that
    # answers exactly the deny posture, rather than asserting on set membership
    # — a membership check would pass even if the function stopped consulting
    # the set.
    print("\n-- a stranger's card under a deny posture --")
    from server.routes import domain_policy as DP

    class DenyAllPool:
        """`*` says no, no per-peer rule, no active contact — the posture a
        stranger actually meets."""
        async def fetchrow(self, sql, *a):
            s2 = " ".join(sql.split())
            if "WHERE peer = '*'" in s2:
                return {"allow_inbox": False}
            return None            # no per-peer rule
        async def fetchval(self, sql, *a):
            return None            # not an active contact

    deny = DenyAllPool()
    for kind, want in [("group_join", True), ("rendezvous_intro", True),
                       ("endpoint_request", True), ("message", False)]:
        got = await DP.check_inbound_allowed(
            deny, "9122@entity.localhost", 9122, 3, kind)
        check(got is want,
              f"under a '*' deny posture a {kind!r} from a stranger is "
              f"{'admitted' if want else 'refused'}", got)
    check("machine_request" not in DP._REQUEST_KINDS,
          "machine_request is NOT given the stranger allowance — it already "
          "requires an accepted contact before its card is written")

    # ── 10. Schema parity ─────────────────────────────────────────────────
    print("\n-- message_kind vocabulary parity --")
    mig = open(os.path.join(_ROOT, "migrations",
                            "v155_group_join_and_rendezvous.sql")).read()
    init = open(os.path.join(_ROOT, "init-db.sql")).read()
    for kind in ("machine_request", "group_join", "rendezvous_intro"):
        check(f"'{kind}'" in mig, f"v155 admits message_kind {kind!r}")
        check(f"'{kind}'" in init, f"init-db.sql admits message_kind {kind!r}")

    print(f"\n{CHECKS} checks, {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  FAILED: " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
