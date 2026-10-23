#!/usr/bin/env python3
"""The grant-back travels the peer lane — the other half of first contact.

inbox.request made the KNOCK deliverable. The ANSWER still went by bridge, which
holds its connections per DaemonSet pod with no forwarding between them, so the
card landed, the user accepted, and the requester was never told. The accept path
already knew: it sets grant_delivered=False and the UI shows it.

WHY THE GRANT NEEDS ITS OWN SLIPLESS OP. inbox.deliver is slip-gated and the slip
must be issued BY the receiver (peer_server verifies expected_issuer=self). At
grant time the accepter holds no slip from the requester — the requester only
knocked, and a knock grants nothing. So the reply cannot ride the existing op.

WHICH MEANS IT MUST BE GATED HARDER, and it is, on a precondition the knock
cannot have: WE MUST HAVE ASKED. Every outbound request writes a `contacts` row
with that peer_entity_id and status 'pending', so a grant is admitted only from a
peer we are waiting on. A stranger with a valid CEIGAS cert has no pending row.

Container-free: the handler is driven directly with a stubbed pool and a stubbed
handle_inbox_append, because what is under test is the ADMISSION decision, not
the storage — the storage path is the same one the bridge uses and is covered
where inbox append is tested.

Run:  python3 scripts/test_peer_lane_grant.py
"""
import asyncio
import os
import sys
import types

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


class StubPool:
    """Answers the one precondition query. `pending` is the set of peer ids we
    have an OPEN connection request with — in either direction, which is what the
    real query checks and what the schema means by 'pending' (v048:20)."""

    def __init__(self, pending=(), raises=False):
        self.pending = set(pending)
        self.raises = raises
        self.queries = []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        if self.raises:
            raise RuntimeError("pool exploded")
        return 1 if int(args[0]) in self.pending else 0


async def main() -> int:
    from server.peer import handlers as H
    from server import brain_service

    appended = []

    async def fake_append(payload):
        appended.append(payload)
        return {"ok": True, "message_id": payload.get("message_id") or "mid"}

    H.handle_inbox_append = fake_append

    async def fake_identify(peer_id):
        return {"from_display_name": "speed", "from_handle": "speed"}

    H._identify = fake_identify

    def use(pool):
        brain_service._pool = pool

    SLIP = {"contract_id": "c-1", "terms": {"expires_at": "2026-12-01T00:00:00+00:00"}}

    def params(**over):
        p = {"message_kind": "endpoint_grant", "subject": "Connection accepted",
             "body": "", "message_id": "mid-1",
             "payload": {"contact_slip": SLIP, "kind": "host"}}
        p.update(over)
        return p

    # ── 1. we asked them: admitted ──────────────────────────────────────────
    use(StubPool(pending={9122}))
    appended.clear()
    r = await H.inbox_grant(9122, None, params())
    check(r.get("ok"), "a grant from a peer we have a pending request with is admitted", r)
    check(len(appended) == 1, "and it reaches the inbox append path")
    if appended:
        p = appended[0]
        check(p["from_entity_id"] == 9122, "attribution comes from the CERT, not the caller")
        check(p["payload"].get("contact_slip") == SLIP,
              "the contact_slip is carried — it is the whole point of the grant")
        check(p["payload"].get("from_handle") == "speed",
              "the name comes from OUR records (_identify), never from the sender")
        check(p["message_id"] == "mid-1", "the message id is carried for idempotency")

    # ── 2. we did NOT ask them: refused ─────────────────────────────────────
    use(StubPool(pending=set()))
    appended.clear()
    r = await H.inbox_grant(999, None, params())
    check(not r.get("ok"), "a grant from a peer with no open request is REFUSED", r)
    check("open" in (r.get("error") or ""), "and the refusal says why", r)
    check(not appended, "nothing is stored for a refused grant")

    # ── 2b. the query the precondition actually runs ────────────────────────
    # The old name claimed "a peer we are waiting on", and the query checked no
    # direction — it could not, because the schema defines `pending` as BOTH
    # ("we accepted an endpoint_request / sent one",
    # v048_circles_contacts_policy.sql:20). The claim was corrected rather than
    # the query widened, and these pin the two properties that were missing:
    # the row must be recent, and the check must happen under the lock.
    pool = StubPool(pending={9122})
    use(pool)
    await H.inbox_grant(9122, None, params())
    sql = pool.queries[-1][0] if pool.queries else ""
    check("status = 'pending'" in sql, "the precondition reads a pending contacts row", sql)
    check("interval" in sql and "last_contact_at" in sql,
          "and BOUNDS it by age — a stranded row must not admit grants for ever "
          "(upsert_contact_from_grant can skip, so rows do get stranded)", sql)
    check(str(H._GRANT_WINDOW_DAYS) in [str(a) for a in pool.queries[-1][1]],
          "with the window passed as a parameter, not interpolated", pool.queries[-1][1])

    # ── 2c. the check happens INSIDE the per-peer lock ──────────────────────
    # It once did not: it sat above the lock, so two grants racing
    # from one peer both passed it and both stored. Driven concurrently here,
    # with a pool that flips the row closed the moment it is first read — the
    # shape a real accept has. Serialized, the second must lose.
    # THE YIELD MUST HAPPEN WHILE THE ROW IS STILL OPEN, and the first version of
    # this stub discarded first and yielded second — which made the whole case
    # tautological. The mutation caught it: moving the check back outside the
    # lock still passed, because the second caller never saw the row open either
    # way. Read, yield, THEN close: now an unlocked check interleaves and admits
    # twice, and a locked one cannot.
    class OneShotPool(StubPool):
        async def fetchval(self, sql, *args):
            ok = int(args[0]) in self.pending
            self.queries.append((sql, args))
            await asyncio.sleep(0)
            self.pending.discard(int(args[0]))   # the row closes once it is used
            return 1 if ok else 0

    use(OneShotPool(pending={9122}))
    appended.clear()
    rs = await asyncio.gather(H.inbox_grant(9122, None, params(message_id="m-a")),
                              H.inbox_grant(9122, None, params(message_id="m-b")))
    check(sum(1 for r in rs if r.get("ok")) == 1,
          "two grants racing from one peer: exactly ONE is admitted", rs)
    check(len(appended) == 1, "and exactly one is stored", len(appended))

    # ── 3. fails CLOSED when the precondition cannot be checked ─────────────
    # Unlike the knock's slot cap, which fails open: a knock confers nothing, a
    # grant installs a credential.
    use(StubPool(pending={9122}, raises=True))
    appended.clear()
    r = await H.inbox_grant(9122, None, params())
    check(not r.get("ok"), "a precondition that cannot run REFUSES (fails closed)", r)
    check(not appended, "and stores nothing")
    use(None)
    r = await H.inbox_grant(9122, None, params())
    check(not r.get("ok"), "no pool at all also refuses", r)

    # ── 4. only grant kinds, and only host grants ───────────────────────────
    use(StubPool(pending={9122}))
    appended.clear()
    r = await H.inbox_grant(9122, None, params(message_kind="message"))
    check(not r.get("ok"), "`message` must never arrive as a grant", r)
    r = await H.inbox_grant(9122, None, params(message_kind="endpoint_request"))
    check(not r.get("ok"), "a request is not a grant on this op", r)
    r = await H.inbox_grant(9122, None, params(payload={"contact_slip": SLIP, "kind": "human"}))
    check(not r.get("ok"), "a HUMAN grant is refused — this lane cannot attest a person", r)
    check("bridge" in (r.get("error") or ""), "and it says where a human grant does go", r)
    check(not appended, "none of those stored anything")

    # ── 5. the payload allow-list ───────────────────────────────────────────
    use(StubPool(pending={9122}))
    appended.clear()
    # The forged keys here are deliberately ones _identify does NOT set.
    #
    # The first version of this test forged from_display_name/from_handle and
    # asserted they did not survive — and they did not, but only because
    # _identify is spread AFTER the payload and overwrites them. Removing the
    # allow-list entirely left that assertion passing, so it was testing spread
    # ordering, not the allow-list. Caught by mutation, which is the only thing
    # that ever catches this.
    #
    # `display_name` and `handle` are the keys the card renderer falls back to
    # (inbox-pane reads from_display_name ?? display_name ?? from_handle ??
    # handle), so they are exactly what a sender would forge to choose the name
    # once the from_* pair is taken.
    await H.inbox_grant(9122, None, params(payload={
        "contact_slip": SLIP, "kind": "host",
        "from_display_name": "TOTALLY NOT SPEED", "from_handle": "evil",
        "display_name": "PICKED BY SENDER", "handle": "evil-handle",
        "authorized_via_slip": True, "anything_else": "x"}))
    if appended:
        got = appended[0]["payload"]
        check(got.get("from_handle") == "speed" and got.get("from_display_name") == "speed",
              "the name and handle are OURS, from _identify, never the sender's", got)
        for k in ("display_name", "handle", "authorized_via_slip", "anything_else"):
            check(k not in got,
                  f"the allow-list drops {k!r} — the card falls back to it, so a sender "
                  f"could otherwise still choose what is shown", got)
        check(set(got) <= {"contact_slip", "kind", "from_display_name", "from_handle",
                           "shares_machine", "lane"},
              "nothing outside the allow-list plus our own fields reaches the payload", got)

    # ── 6. the op is registered and classified slipless ─────────────────────
    from server.peer import peer_server as PS
    check("inbox.grant" in H.PEER_HANDLERS, "the op is registered")
    check("inbox.grant" in PS._SLIPLESS_OPS, "and classified slipless, or it would demand a slip")
    check("inbox.deliver" not in PS._SLIPLESS_OPS,
          "inbox.deliver must STAY slip-gated — widening it is the thing to never do")

    from server.peer import peer_client as PC
    check(hasattr(PC, "grant_peer"), "the client has grant_peer")

    return 0


rc = asyncio.run(main())
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"peer-lane grant: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
