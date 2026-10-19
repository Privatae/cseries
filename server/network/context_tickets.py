"""One-shot fetch tickets for context extraction (the PULL path).

WHAT THIS IS FOR, and it is the security core of the pull lane.

Extraction used to hand the document to the machine INLINE, base64 in the
management frame. That has a hard ceiling — a management job is one WebSocket
frame and uvicorn's ws_max_size stops it at just under 12 MiB of plaintext
(client-go/backupfetch.go, measured in ctxcap_measure_test.go) — so
client-go/contextextract.go caps a source at 8 MiB. Scanned PDFs, audio and
video routinely exceed that, which is exactly the OCR lane. So the bytes have to
move by PULL: the machine fetches the source from the host over the tunnel it
already holds.

THE OBVIOUS VERSION OF THAT IS WRONG. "Let the bastion GET
/api/network/bastions/context-source?ctx_id=N with its bearer" would hand every
paired machine a general read over the user's whole corpus: one loop over the
integers and a box that was asked to read one PDF has copied everything. The
existing file route (server/routes/context.py:714 get_context_file) is gated by
requires_permission("read:memory") — an APPLICATION token, which a bastion does
not have and must not be given, for this reason.

SO THE HOST AUTHORIZES A SPECIFIC FETCH INSTEAD OF GRANTING A READ. When the
host decides to extract context row N on machine M, it mints a ticket here and
sends the machine the TICKET, not the id. The ticket:

  * carries the ctx_id — the request does not. There is no id field on the wire
    for a machine to change, so "fetch a different document" is not a request
    that can be expressed, let alone refused;
  * is bound to ONE bastion token id. Machine B redeeming machine A's ticket is
    a 403, even though B holds a perfectly valid bearer;
  * is single-use. Redeeming pops it, so a replay after the job finishes gets
    nothing;
  * expires in TICKET_TTL seconds. The machine pulls immediately after the
    dispatch frame lands, so the window is the size of one download;
  * is 256 bits from os.urandom and is never stored in the clear — the map is
    keyed by its SHA-256, so a memory dump of this process yields no usable
    ticket.

IN-PROCESS, AND WHAT THAT ACTUALLY RESTS ON.

The mint happens while dispatching a management job. The redeem is a SEPARATE
inbound HTTP request. Those two only meet if they land in the same interpreter,
so this store is correct exactly as long as this app runs ONE worker, and no
longer. It does: server/launch.py:63-67 builds
`uvicorn.Config("server.main:app", ...)` with no `workers` argument and runs it
through `uvicorn.Server(config).run(sockets=[sock])`, which never forks.

SAY IT PLAINLY: adding workers breaks this, and not loudly. A mint in worker A
redeemed against worker B is a 404 indistinguishable from an expired ticket, so
it would show up as extraction failing for roughly (workers-1)/workers of
attempts, intermittently, with nothing in any log saying why.
server/routes/sdk_auth.py:46 records the same dependency for its own session
state — "Session state is process-scope (cseries runs uvicorn --workers 1)" — so
whoever changes that has two things to fix, not one.

(The mgmt relay is also an in-process singleton keyed by token id:
server/network/nac_mgmt.py:453 MgmtRelay, get_mgmt_relay at :506. But that
establishes only that the DISPATCH happens in one process, and says nothing
about the inbound pull — citing it as the reason would be an argument that never
reaches the thing it is defending.)

A restart invalidates every outstanding ticket, which is right: one that
outlived the process would authorize a fetch for a job nobody is waiting on. The
in-flight extraction fails and is re-run, already safe because re-extraction
REPLACES rather than appends.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from typing import Optional

# How long a ticket is good for. The machine pulls the moment the dispatch frame
# lands, so this bounds a download and nothing else. Deliberately short: a longer
# window buys the user nothing and widens the replay surface for a ticket that
# leaked out of a log.
TICKET_TTL = 120.0

# Bound the map. Nothing legitimate holds hundreds of unredeemed tickets — one
# extraction is one ticket and it is consumed seconds later — so this exists so
# a stuck or hostile caller cannot grow the process instead of failing. Oldest
# expiry goes first, which is the one closest to being worthless anyway.
MAX_OUTSTANDING = 256


@dataclass
class Ticket:
    """What one ticket authorizes. `token` is NOT stored here — only its hash is
    a key in the map — so nothing that can read this object can redeem it."""
    ctx_id: int
    entity_id: int
    bastion_token_id: int
    tool: str
    max_bytes: int
    expires_at: float


_tickets: dict[bytes, Ticket] = {}


def _key(token: str) -> bytes:
    return hashlib.sha256((token or "").encode()).digest()


def _sweep(now: float) -> None:
    for k in [k for k, t in _tickets.items() if t.expires_at <= now]:
        _tickets.pop(k, None)


def mint(*, entity_id: int, ctx_id: int, bastion_token_id: int, tool: str,
         max_bytes: int, ttl: float = TICKET_TTL) -> str:
    """Authorize ONE machine to fetch ONE context row, once, for `ttl` seconds.

    Returns the ticket string to put on the wire. The caller must not log it.
    """
    now = time.monotonic()
    _sweep(now)
    if len(_tickets) >= MAX_OUTSTANDING:
        oldest = min(_tickets, key=lambda k: _tickets[k].expires_at)
        _tickets.pop(oldest, None)
    token = os.urandom(32).hex()
    _tickets[_key(token)] = Ticket(
        ctx_id=int(ctx_id), entity_id=int(entity_id),
        bastion_token_id=int(bastion_token_id), tool=str(tool),
        max_bytes=int(max_bytes), expires_at=now + float(ttl))
    return token


def redeem(token: str, bastion_token_id: int) -> Optional[Ticket]:
    """Consume a ticket for THIS bastion, or return None.

    None covers every refusal on purpose — unknown, expired, already used, or
    minted for a different machine. The caller answers all four with the same
    404, because telling a caller which one it was is telling it how to search.
    """
    now = time.monotonic()
    _sweep(now)
    k = _key(token)
    t = _tickets.get(k)
    if t is None:
        return None
    if t.bastion_token_id != int(bastion_token_id):
        # NOT consumed: machine B guessing at A's ticket must not be able to burn
        # it and break A's job. B learns nothing either way.
        return None
    # Single use — pop before returning, so a retry of the same GET gets nothing
    # even if the response never reached the machine.
    _tickets.pop(k, None)
    if t.expires_at <= now:
        return None
    return t


def outstanding() -> int:
    """How many live tickets exist. For tests and diagnostics only."""
    _sweep(time.monotonic())
    return len(_tickets)


def clear() -> None:
    """Drop every ticket. For tests."""
    _tickets.clear()
