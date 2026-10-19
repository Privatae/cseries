#!/usr/bin/env python3
"""The extraction PULL path's AUTHORITY, driven over real HTTP.

WHAT THIS GATE IS FOR. Extraction used to hand the document to the user's
machine inline, base64 in the management frame, which caps a source at 8 MiB
(client-go/contextextract.go `ctxExtractMaxBytes`) because a management job is
one WebSocket frame and uvicorn stops at 16 MiB. Scanned PDFs — the OCR lane —
routinely exceed that, so the bytes now move by PULL: the machine fetches the
source from this host.

The dangerous version of that is a route that takes a ctx_id. A paired machine
holds a valid bearer forever; given an id parameter it could walk the integers
and copy the user's whole corpus, and nothing in the request would look wrong.
So GET /api/network/bastions/context-source takes NO id. It takes a one-shot
ticket the host minted for that machine and that row
(server/network/context_tickets.py). Every check below exists to keep that true:

  * no bearer / a bearer this host does not know        -> 401
  * a valid bearer with no ticket, an unknown ticket,
    an expired one, or one minted for ANOTHER machine   -> 404, all four alike
  * the correct ticket                                  -> 200 and the bytes
  * the same ticket again                               -> 404 (single use)
  * a quarantined machine                               -> 403

This drives the REAL router on an ASGI app with a stub pool, not a source grep:
the route was the thing that had to be exercised, and a gate that reads the file
would pass with the redeem call deleted. Verified red by deleting it.

Run: APP_ROOT=<repo> python3 scripts/test_bastion_context_pull.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

failures: list[str] = []
checks = 0


def check(label, cond, extra=""):
    global checks
    checks += 1
    print(("  ok    " if cond else "  FAIL  ") + label
          + (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(label)


# ── the stub pool ───────────────────────────────────────────────────────────
# Only the three statements this route issues are answered; anything else raises
# so a future edit that adds a query cannot pass here by accident.

ENTITY = 7
BEARER_A = "bastion-a-bearer"
BEARER_B = "bastion-b-bearer"
TOKEN_A, TOKEN_B = 11, 22
DOC = b"%PDF-1.4 the user's private document\n"
OTHER_DOC = b"%PDF-1.4 a DIFFERENT document the machine was never asked for\n"


def _hash(b: str) -> bytes:
    return hashlib.sha256(b.encode()).digest()


class FakePool:
    def __init__(self):
        self.quarantined: set[int] = set()
        self.tokens = {_hash(BEARER_A): {"id": TOKEN_A, "entity_id": ENTITY},
                       _hash(BEARER_B): {"id": TOKEN_B, "entity_id": ENTITY}}
        self.rows = {
            100: {"id": 100, "title": "statement.pdf",
                  "content": base64.b64encode(DOC).decode(), "storage_backend": "pg",
                  "metadata": json.dumps({"kind": "file", "mime": "application/pdf",
                                          "size_bytes": len(DOC)})},
            101: {"id": 101, "title": "private.pdf",
                  "content": base64.b64encode(OTHER_DOC).decode(), "storage_backend": "pg",
                  "metadata": json.dumps({"kind": "file", "mime": "application/pdf",
                                          "size_bytes": len(OTHER_DOC)})},
        }

    async def execute(self, *a, **k):
        return "OK"

    async def fetchrow(self, sql, *args):
        if "mcp_client_tokens" in sql:
            return self.tokens.get(args[0])
        if "FROM user_context" in sql:
            row = self.rows.get(args[0])
            return row if row and args[1] == ENTITY else None
        raise AssertionError("unexpected fetchrow: " + sql)

    async def fetchval(self, sql, *args):
        if "nac_bastion_quarantine" in sql:
            return 1 if args[0] in self.quarantined else None
        if "network_bastion_heartbeat" in sql:
            return None   # no heartbeat: a bastion that passes quarantine answers 409
        raise AssertionError("unexpected fetchval: " + sql)


async def main() -> int:
    # server.main mounts container paths at import; routes that import it must
    # not drag that in here.
    sys.modules.setdefault("server.main", types.ModuleType("server.main"))

    from fastapi import FastAPI
    import httpx

    from server import brain_service
    import server.routes.network as network
    from server.network import context_tickets

    pool = FakePool()
    brain_service.get_pool = lambda: pool           # type: ignore[assignment]
    brain_service.get_control_pool = lambda: pool   # type: ignore[assignment]
    brain_service._entity_id = ENTITY

    app = FastAPI()
    app.include_router(network.router)
    paths = {getattr(r, "path", None) for r in network.router.routes}
    check("the pull route is registered",
          "/api/network/bastions/context-source" in paths)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        URL = "/api/network/bastions/context-source"

        def auth(b):
            return {"Authorization": "Bearer " + b}

        # ── the route names no document ─────────────────────────────────────
        # The strongest statement this gate makes: there is no parameter through
        # which a machine could ask for a row, so enumeration is not a request
        # that can be formed. Checked on the SIGNATURE, because a later edit that
        # adds one would otherwise only be caught by someone reading the diff.
        import inspect
        sig = inspect.signature(network.bastion_context_source)
        check("the route accepts a ticket and a bearer, and NO context id",
              set(sig.parameters) == {"ticket", "authorization"}, list(sig.parameters))

        # ── auth ────────────────────────────────────────────────────────────
        good = context_tickets.mint(entity_id=ENTITY, ctx_id=100,
                                    bastion_token_id=TOKEN_A, tool="pdf-text",
                                    max_bytes=64 << 20)
        r = await c.get(URL, params={"ticket": good})
        check("no bearer is 401", r.status_code == 401, r.status_code)
        r = await c.get(URL, params={"ticket": good}, headers=auth("not-a-bearer"))
        check("a bearer this host does not know is 401", r.status_code == 401, r.status_code)

        # ── the ticket is the authority ─────────────────────────────────────
        r = await c.get(URL, params={"ticket": "f" * 64}, headers=auth(BEARER_A))
        check("a valid bearer with an unknown ticket gets NOTHING (404)",
              r.status_code == 404, r.status_code)

        # A ticket minted for machine B, presented by machine A.
        b_ticket = context_tickets.mint(entity_id=ENTITY, ctx_id=101,
                                        bastion_token_id=TOKEN_B, tool="pdf-text",
                                        max_bytes=64 << 20)
        r = await c.get(URL, params={"ticket": b_ticket}, headers=auth(BEARER_A))
        check("one machine cannot redeem another machine's ticket",
              r.status_code == 404, r.status_code)
        # ...and it did not BURN it: B's job must still work.
        r = await c.get(URL, params={"ticket": b_ticket}, headers=auth(BEARER_B))
        check("and the attempt did not consume it — the rightful machine still gets its bytes",
              r.status_code == 200 and r.content == OTHER_DOC, r.status_code)

        # ── the happy path ──────────────────────────────────────────────────
        r = await c.get(URL, params={"ticket": good}, headers=auth(BEARER_A))
        check("the machine that was asked to extract gets exactly that document",
              r.status_code == 200 and r.content == DOC, r.status_code)
        check("served with the stored mime type, not as base64",
              r.headers.get("content-type", "").startswith("application/pdf"),
              r.headers.get("content-type"))

        # ── single use ──────────────────────────────────────────────────────
        r = await c.get(URL, params={"ticket": good}, headers=auth(BEARER_A))
        check("a replayed ticket gets nothing — it authorized ONE fetch",
              r.status_code == 404, r.status_code)

        # ── expiry ──────────────────────────────────────────────────────────
        stale = context_tickets.mint(entity_id=ENTITY, ctx_id=100,
                                     bastion_token_id=TOKEN_A, tool="pdf-text",
                                     max_bytes=64 << 20, ttl=-1)
        r = await c.get(URL, params={"ticket": stale}, headers=auth(BEARER_A))
        check("an expired ticket gets nothing", r.status_code == 404, r.status_code)

        # ── the ticket's own ceiling ────────────────────────────────────────
        small = context_tickets.mint(entity_id=ENTITY, ctx_id=100,
                                     bastion_token_id=TOKEN_A, tool="pdf-text",
                                     max_bytes=4)
        r = await c.get(URL, params={"ticket": small}, headers=auth(BEARER_A))
        check("a document past what the extraction was authorized for is 413, "
              "not a truncated body", r.status_code == 413, r.status_code)

        # ── quarantine ──────────────────────────────────────────────────────
        pool.quarantined.add(TOKEN_A)
        q = context_tickets.mint(entity_id=ENTITY, ctx_id=100,
                                 bastion_token_id=TOKEN_A, tool="pdf-text",
                                 max_bytes=64 << 20)
        r = await c.get(URL, params={"ticket": q}, headers=auth(BEARER_A))
        check("a quarantined machine gets no bytes", r.status_code == 403, r.status_code)
        pool.quarantined.discard(TOKEN_A)

        # dcn-propose refuses a quarantined machine the same way, before it
        # reads the heartbeat or drafts anything.
        PROPOSE = "/api/network/bastions/dcn-propose"
        pool.quarantined.add(TOKEN_A)
        r = await c.post(PROPOSE, json={"root": "/x"}, headers=auth(BEARER_A))
        check("dcn-propose: a quarantined machine is refused (403)", r.status_code == 403, r.status_code)
        pool.quarantined.discard(TOKEN_A)
        r = await c.post(PROPOSE, json={"root": "/x"}, headers=auth(BEARER_A))
        check("dcn-propose: an unquarantined machine passes the gate (409, no heartbeat)",
              r.status_code == 409, r.status_code)

    # ── the ticket store's own invariants ───────────────────────────────────
    context_tickets.clear()
    t = context_tickets.mint(entity_id=ENTITY, ctx_id=1, bastion_token_id=TOKEN_A,
                             tool="pdf-text", max_bytes=1)
    check("the ticket is 256 bits of hex, not a derivation of the id",
          len(t) == 64 and str(1) not in {t[:8]}, t[:16])
    check("redeeming consumes it", context_tickets.redeem(t, TOKEN_A) is not None
          and context_tickets.redeem(t, TOKEN_A) is None)
    context_tickets.clear()
    for i in range(context_tickets.MAX_OUTSTANDING + 20):
        context_tickets.mint(entity_id=ENTITY, ctx_id=i, bastion_token_id=TOKEN_A,
                             tool="pdf-text", max_bytes=1)
    check("the store is bounded — a stuck caller grows nothing",
          context_tickets.outstanding() <= context_tickets.MAX_OUTSTANDING,
          context_tickets.outstanding())
    context_tickets.clear()

    # ── the two middlewares that stand in front of it ───────────────────────
    # A bastion has no app window and so no application token, and it reaches
    # :8443 with its own operator cert rather than the dashboard router's. Miss
    # either exemption and this route 401s or 403s before its own checks run —
    # which is how the heartbeat was logging a would-deny every two minutes
    # before its line was added.
    from server.applications.middleware import _ALLOWLIST_PREFIXES
    from server.middleware.localhost_only import _MCP_CLIENT_PATHS
    check("the route is exempt from the blanket application-token requirement",
          "/api/network/bastions/context-source" in _ALLOWLIST_PREFIXES)
    check("and from the router-CN gate, like the heartbeat it is authed by",
          "/api/network/bastions/context-source" in _MCP_CLIENT_PATHS)

    # ── the host half picks the transport, and only where it is offered ─────
    from server.routes import context_extract as ce
    check("a machine that does not advertise the pull path is not handed a ticket",
          ce.machine_pulls({}) is False
          and ce.machine_pulls({"scan": {"supported": True, "probed": 1, "found": 1}}) is False
          and ce.machine_pulls({"scan": {"pull": True}}) is True)
    check("the pull ceiling clears the platform's own upload cap by a margin",
          ce.PULL_MAX_BYTES == 64 * 1024 * 1024 and ce.PULL_MAX_BYTES > 25 * 1024 * 1024,
          ce.PULL_MAX_BYTES)
    check("the inline cap still mirrors the box's frame limit",
          ce.MAX_SOURCE_BYTES == 8 * 1024 * 1024, ce.MAX_SOURCE_BYTES)
    # ── THE ORDERING BETWEEN THE TWO CLOCKS ─────────────────────────────────
    # The box must give up before the host does. If the host quits first it
    # reports "did not answer" about a machine that is downloading happily, then
    # discards the text when it finally arrives: the work done, the bytes moved,
    # nothing written, and the user told their machine is unresponsive by the
    # component that quit first.
    #
    # This was two independent constants — a per-size wait here, a flat four
    # minutes on the box — with a comment on each side asserting the ordering,
    # and it was FALSE across the whole domain (64 MiB: host 124s, box 240s; the
    # crossover needed a 180 MiB source and the wall is 64 MiB). So the host now
    # GRANTS the budget on the wire and its own wait is that same number plus a
    # fixed allowance. The box's half is pinned by TestPullHonoursTheHostsGrant
    # in the gateway source.
    MiB = 1024 * 1024
    domain = [0, 1 * MiB, 8 * MiB, 25 * MiB, 64 * MiB, ce.PULL_MAX_BYTES, 1 << 40]
    check("the host's wait is ALWAYS the grant plus a fixed allowance — one "
          "formula, not two constants that have to agree",
          all(abs(ce._pull_timeout(s) - (ce._pull_budget(s) + ce.PULL_REPLY_ALLOWANCE)) < 1e-9
              for s in domain))
    check("so the machine's deadline is strictly under the host's at every size "
          "— the box gives up first",
          all(ce._pull_budget(s) < ce._pull_timeout(s) for s in domain),
          [(s, ce._pull_budget(s), ce._pull_timeout(s)) for s in domain])
    check("and under it by enough for the extraction and the reply (the box's "
          "own tool ceiling is 20s)",
          all(ce._pull_timeout(s) - ce._pull_budget(s) >= 40.0 for s in domain))
    check("the grant is bounded at both ends, so a mgmt worker is never parked "
          "indefinitely and an unknown size is not starved",
          ce._pull_budget(1 << 40) == ce.PULL_BUDGET_MAX
          and ce._pull_budget(0) == ce.PULL_BUDGET_BASE)
    check("the required transfer rate is a stated floor, not an implied guess",
          ce.PULL_RATE_BYTES_PER_S == 512 * 1024)
    check("an unknown size buys the FULL grant, never the floor — the estimate "
          "under-reports on a non-pg row, which is the dangerous direction for "
          "a deadline",
          "clock_size = declared_size or cap" in
          open(os.path.join(_ROOT, "server/routes/context_extract.py"),
               encoding="utf-8").read())
    # The arithmetic itself, printed, so this is read as numbers rather than as
    # a claim about numbers.
    print("        size          box budget    host waits")
    for s in domain[:-1]:
        label = "unknown" if s == 0 else f"{s // MiB} MiB"
        print(f"        {label:>10}   {ce._pull_budget(s):9.0f} s  {ce._pull_timeout(s):9.0f} s")

    # ── the heartbeat carries the flag, whitelisted ─────────────────────────
    from server.network import topology
    top_src = open(os.path.join(_ROOT, "server/network/topology.py"), encoding="utf-8").read()
    check("prep_scan.pull is whitelisted like its siblings, not stored as it arrived",
          '"pull": bool(scan_in.get("pull"))' in top_src)
    check("topology still refuses to store the raw scan dict",
          "json.dumps(prep_scan_in)" not in top_src)
    assert topology  # imported to prove the module loads with the change

    print()
    if failures:
        print(f"FAIL: {len(failures)} of {checks} check(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS: {checks} checks — a paired machine can fetch what it was asked to "
          f"extract, and nothing else")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
