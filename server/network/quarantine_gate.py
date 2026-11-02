"""The one quarantine check every lane a paired bastion uses consults.

A quarantine (nac_commit.quarantine_bastion, the Network app's Quarantine button)
holds a machine: nothing it holds a bearer or a client certificate for may reach
this Host, until the owner releases it with their step-up code. Before this
module each lane asked on its own, and most never asked: the egress, embedding,
memory-mirror, management and harness worker sockets admitted a quarantined
bastion, /mcp consulted only the NAC policy engine (a different thing: the
rules the owner writes, nac_controller), and the heartbeat ingested whatever
the machine reported. A quarantine closed the gate in front of management
jobs and, on the platform, dropped the machine's WireGuard peer; a Host
without the platform's tunnel (a standalone C-Series Host) had no network cut
at all.

Every lane now goes through here:

  is_quarantined(pool, token_id)   the read. FAILS CLOSED: no pool, a read that
                                   raises, or anything else that stops the
                                   answer being "no" reads as quarantined.
  refuse_http(pool, token_id)      raises 403 for the HTTP routes.
  run_lane(pool, token_id, lane, body)
                                   the worker sockets: registers the socket,
                                   asks, runs `body` only when the answer is
                                   no, and stops it the moment cut() is called.
  cut(token_id)                    called by quarantine_bastion: every socket
                                   that machine holds is stopped now, rather
                                   than living until it next reconnects.
  held_beat(pool, token_id)        the heartbeat of a quarantined machine:
                                   last_seen_at is recorded and nothing else.

Keyed by the bearer's token id, and also by the hostname that token last
reported, because the quarantine row is keyed by server (v069) and the
management gate matches on it; a match on either holds the machine.

On a standalone Host the bastion reaches all of this through the lane port
(server/network/local_bastion.py: lane.sock and :8811), which checks only
that the client certificate is one this Host issued and forwards to the same
sockets and routes, so the same checks hold behind it. quarantine_bastion
also closes the lane-port connections the machine holds (close_lanes), and
the door keeps admitting the certificate so the heartbeat can be recorded.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger("privatae.network.quarantine")

# The row by token id, or by the hostname this token's machine reported. One
# argument ($1, the token id), so a test's stand-in pool can answer it from the
# table name and the first argument the way the older per-route reads were.
_QUARANTINE_SQL = (
    "SELECT 1 FROM nac_bastion_quarantine q WHERE q.token_id = $1 "
    "OR q.server IN (SELECT h.hostname FROM network_bastion_heartbeat h "
    "WHERE h.token_id = $1 AND h.hostname IS NOT NULL) LIMIT 1")

# The WebSocket close code a held lane gets (the /bastion-git lane's code before
# this module; the bastion reads 4403 as "refused, retry later").
CLOSE_CODE = 4403


async def is_quarantined(pool, token_id) -> bool:
    """Whether the machine behind bastion token `token_id` is quarantined.

    Fails closed: a quarantine is a tighten, and a tighten has to hold when the
    Host cannot read it, so no pool or a read that raises answers True."""
    if pool is None:
        log.warning("quarantine check: no database pool, so bastion token %s is held", token_id)
        return True
    try:
        tid = int(token_id)
    except (TypeError, ValueError):
        log.warning("quarantine check: token id %r is not a number, so it is held", token_id)
        return True
    try:
        hit = await pool.fetchval(_QUARANTINE_SQL, tid)
    except Exception as e:
        log.warning("quarantine check unavailable for bastion token %s (%r), so it is held",
                    tid, e)
        return True
    return bool(hit)


async def refuse_http(pool, token_id) -> None:
    """Raise 403 when the machine behind `token_id` is quarantined (or its state
    cannot be read). For the bastion's HTTP routes."""
    if await is_quarantined(pool, token_id):
        from fastapi import HTTPException
        raise HTTPException(403, "this bastion is quarantined")


# ── the worker sockets ──────────────────────────────────────────────────────

class _Hold:
    __slots__ = ("token_id", "lane", "cut")

    def __init__(self, token_id: int, lane: str):
        self.token_id = token_id
        self.lane = lane
        self.cut = asyncio.Event()


# token_id -> the lanes that machine holds open now.
_held: dict[int, set] = {}

# A refused worker socket is held this long before the caller closes it. The
# bastion parks a pool of workers on every lane and dials again about a second
# after a close (client-go egress_relay.go egressRefusedWait, servelane.go
# serveRefusedWait); it does not read the heartbeat's "quarantined", so a
# machine held on a standalone Host redialled 30 to 80 times a second, each one
# a TLS handshake and a log line here. Holding the refused socket open, with
# nothing passing on it, is the tarpit that slows that to one dial per worker
# per hold. Nothing runs on a held socket; a release is seen at the next dial.
REFUSED_HOLD_S = 30.0

# A refusal is logged once per machine and lane a minute, with how many were
# not: the redial loop above would otherwise write a line per dial.
REFUSAL_LOG_EVERY_S = 60.0
_refusal_log: dict[tuple, list] = {}


def _log_refusal(lane: str, tid: int) -> None:
    import time
    now = time.monotonic()
    key = (tid, lane)
    seen = _refusal_log.get(key)
    if seen is not None and now - seen[0] < REFUSAL_LOG_EVERY_S:
        seen[1] += 1
        return
    more = seen[1] if seen is not None else 0
    _refusal_log[key] = [now, 0]
    if len(_refusal_log) > 4096:
        for k in [k for k, v in _refusal_log.items() if now - v[0] >= REFUSAL_LOG_EVERY_S]:
            del _refusal_log[k]
    log.info("%s lane REFUSED: bastion token %s is quarantined%s", lane, tid,
             f" ({more} more refused in the last minute)" if more else "")


def cut(token_id) -> int:
    """Stop every lane bastion token `token_id` holds open on this Host. Returns
    how many. Synchronous, so quarantine_bastion can call it with no await between
    writing the row and cutting the sockets."""
    try:
        tid = int(token_id)
    except (TypeError, ValueError):
        return 0
    holds = list(_held.get(tid, ()))
    for h in holds:
        h.cut.set()
    if holds:
        log.info("quarantine: cut %d open lane(s) of bastion token %s (%s)", len(holds), tid,
                 ", ".join(sorted({h.lane for h in holds})))
    return len(holds)


def open_lanes(token_id) -> list[str]:
    """The lanes bastion token `token_id` holds open now (for tests and the
    health view)."""
    return sorted(h.lane for h in _held.get(int(token_id), ()))


async def run_lane(pool, token_id, lane: str, body: Callable[[], Awaitable[None]]) -> bool:
    """Admit one worker socket of bastion token `token_id` on `lane`, run
    `body()` and return True when it ends on its own; return False when the
    machine is quarantined (the caller closes with CLOSE_CODE), either at the
    door or because cut() stopped it while it ran.

    The socket is registered BEFORE the database is asked, so a quarantine
    written between the read and the registration is still cut: either the
    read sees the row, or cut() sees the registration."""
    tid = int(token_id)
    hold = _Hold(tid, lane)
    _held.setdefault(tid, set()).add(hold)
    try:
        if await is_quarantined(pool, tid):
            _log_refusal(lane, tid)
            if REFUSED_HOLD_S > 0:
                await asyncio.sleep(REFUSED_HOLD_S)
            return False
        work = asyncio.ensure_future(body())
        stop = asyncio.ensure_future(hold.cut.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            work.cancel()
            stop.cancel()
            raise
        if work.done():
            stop.cancel()
            work.result()        # the body's own error, to the endpoint's handler
            return True
        work.cancel()
        try:
            await work
        except BaseException:
            # The body's end after the cut: CancelledError, or whatever its own
            # teardown raised. The lane is closed either way.
            pass
        log.info("%s lane CUT: bastion token %s was quarantined while it was open", lane, tid)
        return False
    finally:
        s = _held.get(tid)
        if s is not None:
            s.discard(hold)
            if not s:
                _held.pop(tid, None)


# ── the heartbeat ───────────────────────────────────────────────────────────

async def held_beat(pool, token_id) -> dict:
    """The heartbeat of a quarantined machine: ACCEPTED BUT RECORDED ONLY.

    The owner has to see that a quarantined machine is still alive (it is
    their evidence about it, and the Release button is theirs to press), so
    its last_seen_at moves. Nothing else from the beat is believed or acted
    on: no reported state is written, no agents ingested, no inbox notices,
    no embedding wiring, no drafts, and the reply carries no directive, grant,
    update, mirror instruction or approval. The bearer's expiry is not slid
    either, so a machine quarantined for good ages out the way an unused
    bearer does."""
    seen = None
    try:
        seen = await pool.fetchval(
            "UPDATE network_bastion_heartbeat SET last_seen_at = now() WHERE token_id = $1 "
            "RETURNING last_seen_at", int(token_id))
    except Exception as e:
        log.info("held heartbeat for bastion token %s not recorded: %r", token_id, e)
    return {
        "token_id": int(token_id),
        "last_seen_at": seen.isoformat() if hasattr(seen, "isoformat") else None,
        "ok": True,
        "quarantined": True,
    }


async def mcp_refusal(auth: dict) -> Optional[dict]:
    """/mcp's half: a bastion token (operator_kind 'bastion', or any device-bound
    token) whose machine is quarantined is refused before the NAC policy engine
    is asked. None to go on."""
    if not auth or auth.get("_internal") or auth.get("_agent") or auth.get("_harness"):
        return None
    if auth.get("operator_kind") != "bastion" and not auth.get("hardware_fingerprint"):
        return None
    tid = auth.get("id")
    if not tid:
        return None
    try:
        from server import brain_service
        pool = brain_service.get_pool()
    except Exception:
        pool = None
    if await is_quarantined(pool, tid):
        return {"allowed": False, "profile": None, "quarantined": True,
                "why": "this machine is quarantined on this Host; its owner can release it "
                       "in the Network app"}
    return None
