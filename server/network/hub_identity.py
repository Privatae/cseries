"""The Hub's WireGuard identity is this Host's own key.

Until this change the operator generated every Host's Hub private key and kept
it in a Kubernetes Secret, so
anyone with cluster admin could read the key that terminates the user's machine
tunnels, and the bridge handed its public half to every machine that paired.
The key now lives where the Host's other keys live: `kernel_secrets`, generated
in this pod on first use and written nowhere else. The Hub gets it the way it
gets the PDP verify key (hub_peers.arm_hub_pdp) — over its loopback control API
on every reconcile tick, because the Hub's state dies with the sidecar.

The platform's old key keeps serving for a window. A Hub that booted with
HUB_WG_PRIVATE_KEY in its env runs it as a LEGACY identity beside the Host's.
Machines move to the Host's key in band: the heartbeat answer carries the
rotate directive (topology._rotate_directive) and the client proves a real
handshake with the new key before it commits to it (client-go rotate.go). Once
every machine the Hub serves is on the Host's key, the reconcile tick tells the
Hub to retire the legacy identity.

Contract: hub/control/control.go POST /identity and GET /identity (hub v0.2.8).
Every function here is best-effort and never raises, like the peer push: a Hub
we cannot reach is a Hub that is not carrying traffic either.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from server.applications.tokens import _SECRET_SCHEMA
from server.network import hub_peers

logger = logging.getLogger(__name__)

# kernel_secrets id. The value is the raw 32-byte X25519 private key, which is
# what WireGuard calls a private key; the Hub takes it base64-encoded.
KEY_ID = "hub_wg"

# The reports below run on every 15 s tick and their failure states are steady
# (an old hub image stays old until it rolls), so each condition speaks once,
# like hub_peers' PDP reports. Kept apart from hub_peers._arm_said on purpose:
# the PDP arm clears that set every time it succeeds, which is every tick on a
# healthy Hub, and it runs right after this one, so sharing it made these
# reports speak every tick.
_said: set[str] = set()


def _say_once(key: str, level: int, msg: str, *args) -> None:
    if key in _said:
        return
    _said.add(key)
    logger.log(level, msg, *args)


def _generate_private() -> bytes:
    return X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())


def public_from_private(raw: bytes) -> str:
    """The base64 WireGuard public key for a raw 32-byte private key."""
    pub = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub).decode()


async def ensure_hub_wg_key(pool) -> bytes:
    """Generate-or-fetch this Host's Hub private key. Write-once.

    Same shape as entity_signing.ensure_entity_signing_key: two callers racing
    on a fresh pod (the reconcile tick and a pairing) can both generate, but
    the INSERT keeps only the first and both return what was stored, so the
    Hub is never armed with one key while a machine is handed another."""
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id = $1", KEY_ID)
        if row is None:
            await conn.execute(
                "INSERT INTO kernel_secrets (id, secret) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                KEY_ID, _generate_private())
            row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id = $1", KEY_ID)
    return bytes(row["secret"])


async def hub_wg_pubkey(pool) -> str:
    return public_from_private(await ensure_hub_wg_key(pool))


# ── the Hub's view: GET /identity ───────────────────────────────────────────
#
# Read on every reconcile tick (to decide retirement) and by the heartbeat
# handler (to decide whether a machine still has to move). Heartbeats arrive
# once a minute per machine, so the handler reads the tick's copy rather than
# asking the Hub per beat; it fetches itself only when the copy is older than
# STATUS_MAX_AGE_S (no reconcile loop in this process, or the loop stalled), and
# before it tells a machine to change keys (pubkey_for_machine).
# No lock around that refetch: at worst a few beats landing in the same instant
# each read it once, which costs the loopback less than a lock bound to one
# event loop would cost the tests that run several.
STATUS_MAX_AGE_S = 2 * hub_peers.RECONCILE_INTERVAL_S

# GET /identity has three kinds of answer, and they must stay apart:
#  - the Hub's view (a dict): it holds the keys it names, and no others;
#  - PRE_IDENTITY: it answered 404, so its image predates the route and it
#    serves only the operator-made key, which is what the bridge reports;
#  - None: no answer we can read (unreachable, timed out, an error). That says
#    nothing about which key the Hub serves. Once the platform's key is retired
#    the bridge still reports it (its hub_pubkey column is never cleared), so
#    treating silence as an old Hub would pin a machine to a key nothing answers,
#    and the heartbeat that could correct it rides that dead tunnel.
PRE_IDENTITY = "pre-identity"
_status: tuple[float, object] = (0.0, None)


async def fetch_status():
    """GET /identity: the Hub's view (dict), PRE_IDENTITY, or None (above)."""
    global _status
    got: object = None
    try:
        async with aiohttp.ClientSession(timeout=hub_peers._HTTP_TIMEOUT) as s:
            async with s.get(f"{hub_peers.HUB_CONTROL}/identity") as r:
                if r.status == 200:
                    body = await r.json()
                    got = body if isinstance(body, dict) else None
                elif r.status == 404:
                    got = PRE_IDENTITY
    except Exception as e:
        logger.debug("hub identity read skipped (hub unreachable?): %s", e)
    _status = (time.monotonic(), got)
    return got


async def cached_status():
    if time.monotonic() - _status[0] < STATUS_MAX_AGE_S:
        return _status[1]
    return await fetch_status()


# ── what the platform is told: the heartbeat's hub_key ─────────────────────
#
# The platform used to learn which key each Hub runs by exec-ing into every
# Host. The Host now says so on
# the bridge heartbeat it already sends (bridge_client._heartbeat_loop), and the
# operator keeps the last answer per Host. It decides when the operator may stop
# reading and referencing the old entity-<id>-hub-wg Secret for this Host, so the
# one state that permits that, "own", means the Hub holds a Host key and runs no
# legacy identity, i.e. no machine can still need the platform's key.
#   own         Host key armed, legacy identity retired or never given
#   own+legacy  Host key armed, platform's key still served (machines moving)
#   legacy      only the platform's key (an unarmed Hub, or one predating /identity)
#   none        the Hub holds no key at all
#   unknown     no fresh answer from the Hub (unreachable, or no tick has run)
# Public facts only: no key, public or private, leaves the pod in this report.
HUB_KEY_STATES = ("own", "own+legacy", "legacy", "none", "unknown")


def report_state() -> dict:
    """{"state": one of HUB_KEY_STATES, "machines": {"host": n, "legacy": n,
    "none": n}} from the reconcile tick's copy of GET /identity. No I/O: the
    heartbeat that carries it is the bridge connection's liveness signal and
    must never wait on the Hub."""
    at, status = _status
    if not at or time.monotonic() - at >= STATUS_MAX_AGE_S:
        return {"state": "unknown", "machines": {}}
    if status == PRE_IDENTITY:
        return {"state": "legacy", "machines": {}}
    if not isinstance(status, dict):
        return {"state": "unknown", "machines": {}}
    machines = {"host": 0, "legacy": 0, "none": 0}
    for p in status.get("peers") or []:
        on = p.get("on") if isinstance(p, dict) else None
        machines[on if on in ("host", "legacy") else "none"] += 1
    own, legacy = bool(status.get("public_key")), bool(status.get("legacy_public_key"))
    state = ("own+legacy" if own and legacy else "own" if own
             else "legacy" if legacy else "none")
    return {"state": state, "machines": machines}


def peer_on(status, machine_pubkey: str) -> Optional[str]:
    """Which identity the Hub last handshook with this machine on: "host",
    "legacy", or None (not a peer, or no handshake yet)."""
    for p in (status if isinstance(status, dict) else {}).get("peers") or []:
        if isinstance(p, dict) and p.get("public_key") == machine_pubkey:
            return p.get("on")
    return None


# What each machine's latest heartbeat said about saving its move: True = it
# runs on a key its config file does not hold yet, False = saved, None = a
# client too old to say. A machine absent here has not beaten since this
# process started, or was told to move since its last beat (forget_save_state).
#
# Why the Hub's handshake view is not enough: it shows the key a machine RUNS
# on, never the one on its disk. A machine whose config save keeps failing runs
# on our key and restarts onto the platform's; retire that key under it and it
# is stranded, because the heartbeat that could correct it rides the dead
# tunnel. Clients from 0.47.2 report it on every beat.
_beat_save_pending: dict[str, Optional[bool]] = {}

# The machines the Hub serves, each with the bastion tokens it is served under,
# as of the last tick that judged retirement (the rows hub_peers.active_bastions
# reads). A beat is recorded only for one of these and only under one of its own
# tokens. The token is the bearer that sent the beat (routes/network.py sets it
# from the bearer); wg_pubkey is the machine's own word for which key it is, so
# without this check one paired machine could report another's save as done.
# It also keeps the record to the size of the fleet.
_served: dict[str, set[int]] = {}

# Machines a WARNING has named for holding the platform's key with a move they
# cannot save. A save that keeps failing (a read-only config directory) holds
# the key for as long as it fails, and the machine says so only in its own log.
# Named once per episode, not per tick, and kept apart from _said, which every
# successful arm clears.
_unsaved_said: set[str] = set()


def note_heartbeat(machine_pubkey: str, payload: dict) -> None:
    """Record what a machine's heartbeat says about saving its key move."""
    if not machine_pubkey:
        return
    try:
        token = int(payload.get("token_id"))
    except (TypeError, ValueError):
        return
    if token not in _served.get(machine_pubkey, ()):
        return
    v = payload.get("endpoint_save_pending")
    _beat_save_pending[machine_pubkey] = None if v is None else bool(v)


def forget_save_state(machine_pubkey: str) -> None:
    """The machine is being told to move to our key on this beat.

    What this beat said about its save was built before the move, so it says
    nothing about whether the move will be saved. The Hub sees the machine on
    our key as soon as its probe handshakes, and the beat that reports the
    move's save comes a minute later; until then the machine counts as one
    whose save is unknown."""
    _beat_save_pending.pop(machine_pubkey, None)


async def _served_machines(pool) -> dict[str, set[int]]:
    """{machine pubkey: its bastion token ids}, from the same rows
    hub_peers.active_bastions reads, so its keys are the set the converge
    pushes."""
    out: dict[str, set[int]] = {}
    for r in await pool.fetch(hub_peers._ACTIVE_BASTIONS_SQL):
        out.setdefault(r["pubkey"], set()).add(int(r["id"]))
    return out


def should_retire_legacy(status, own_pubkey: str, machines: set[str]) -> bool:
    """Retire the platform's key once nothing still needs it.

    `machines` is the set the Hub must serve: hub_peers.active_bastions, the
    same set the converge pushes. A machine whose bearer was revoked or expired
    is not in it; it has to pair again whatever key the Hub holds. The Hub must
    already hold OUR key, or retiring would leave it holding nothing we gave
    it. An empty set retires at once: no machine is pinned to the old key.

    Every machine must also have beaten since this process started and since
    it was last told to move, under its own token, and none may report a move
    it has not saved (_beat_save_pending): a key retired
    under a machine whose disk still names it strands that machine at its next
    restart. A client too old to report it is judged on its handshake alone.
    """
    if not isinstance(status, dict) or not status.get("legacy_public_key"):
        return False
    if status.get("public_key") != own_pubkey:
        return False
    for m in machines:
        if peer_on(status, m) != "host":
            return False
        if m not in _beat_save_pending or _beat_save_pending[m] is True:
            return False
    return True


# ── arming: POST /identity ──────────────────────────────────────────────────

async def arm_hub_identity(pool, retire_legacy: bool = False) -> bool:
    """Give the Hub this Host's key. True on 204.

    Idempotent for the same key. 409 means the Hub already holds a DIFFERENT
    Host key (or was handed the platform's), which is never benign: this
    Host's key is write-once, so something else armed the Hub, and every
    machine we pair is handed a key that Hub will not answer. Recovery needs
    the sidecar restarted, which this pod cannot do (no service account,
    by design), so it is reported loudly once and left to the alert lane, the same
    as a PDP key mismatch."""
    try:
        raw = await ensure_hub_wg_key(pool)
    except Exception as e:
        _say_once("idkey", logging.WARNING,
                  "hub identity arm skipped: no Hub key yet (%s)", e)
        return False
    body = {"key": base64.b64encode(raw).decode(), "retire_legacy": bool(retire_legacy)}
    try:
        async with aiohttp.ClientSession(timeout=hub_peers._HTTP_TIMEOUT) as s:
            async with s.post(f"{hub_peers.HUB_CONTROL}/identity", json=body) as r:
                if r.status // 100 == 2:
                    # Armed: each condition may speak again if it comes back (a
                    # hub rolled back to an image without /identity).
                    _said.clear()
                    return True
                if r.status == 409:
                    _say_once(
                        "idmismatch", logging.ERROR,
                        "hub holds a DIFFERENT WireGuard key than this Host's — "
                        "machines paired to this Host will not connect until the hub "
                        "sidecar is restarted (the pod cannot do this itself)")
                    return False
                if r.status == 404:
                    # Hub image predates /identity: it serves the operator's key
                    # until the sidecar rolls. Expected on every pod between this
                    # ship and the hub roll, so it speaks once.
                    _say_once(
                        "pre_identity", logging.WARNING,
                        "hub cannot take this Host's WireGuard key: this hub image has "
                        "no /identity endpoint — it serves the platform-made key")
                    return False
                _say_once(f"idhttp{r.status}", logging.WARNING,
                          "hub identity arm rejected: HTTP %s %s",
                          r.status, (await r.text())[:200])
    except Exception as e:
        logger.debug("hub identity arm skipped (hub unreachable?): %s", e)
    return False


async def converge_identity(pool) -> bool:
    """One reconcile tick's worth: arm the Hub with this Host's key, and retire
    the legacy key when every machine has moved. Returns whether the Hub holds
    this Host's key afterwards."""
    status = await fetch_status()
    retire = False
    own = None
    try:
        own = await hub_wg_pubkey(pool)
        if (isinstance(status, dict) and status.get("legacy_public_key")
                and status.get("public_key") == own):
            served = await _served_machines(pool)
            _served.clear()
            _served.update(served)
            machines = set(served)
            retire = should_retire_legacy(status, own, machines)
            for gone in set(_beat_save_pending) - machines:
                _beat_save_pending.pop(gone, None)   # unpaired or expired
            for m in machines:
                if _beat_save_pending.get(m) is not True:
                    _unsaved_said.discard(m)
                elif m not in _unsaved_said:
                    _unsaved_said.add(m)
                    logger.warning(
                        "hub keeps the platform's WireGuard key: machine %s… runs on a Hub "
                        "key or entry it cannot save to its config file, and a restart would "
                        "bring it back on the old one; kept until its save lands", str(m)[:12])
            _unsaved_said.intersection_update(machines)
    except Exception as e:
        logger.debug("hub legacy-retire check skipped: %s", e)
    armed = await arm_hub_identity(pool, retire_legacy=retire)
    if armed and retire:
        logger.info("hub legacy WireGuard key retired: every machine is on this Host's key")
    if armed and (retire or not isinstance(status, dict) or status.get("public_key") != own):
        # The arm changed what the Hub answers, and the heartbeat hands out keys
        # from this copy until the next tick. Left as it was read above, a
        # machine that handshakes on our key the moment the Hub holds it would
        # be sent the platform's key (it probes it and moves back onto it) or,
        # once that is gone, the bridge's stale copy of it.
        await fetch_status()
    return armed


# ── what a machine is told ──────────────────────────────────────────────────

async def pubkey_for_new_machine(pool, bridge_pubkey: Optional[str]) -> Optional[str]:
    """The Hub key a machine that pairs NOW must pin: the key the Hub will
    answer it on.

    This Host's key whenever the Hub holds it (arming it first if this is a
    fresh pod and the tick has not run yet). A Hub whose image predates
    /identity serves only the operator's key, which the bridge reports. A Hub
    that does not answer at all could be either, and the bridge's key may be
    one it has retired, so that pairing is refused and the token given back for
    a retry. None when there is no key the Hub is known to serve; the caller
    refuses the pairing."""
    try:
        own = await hub_wg_pubkey(pool)
    except Exception as e:
        logger.warning("pairing: this Host's Hub key could not be read: %s", e)
        own = None
    status = await fetch_status()
    if status == PRE_IDENTITY:
        return bridge_pubkey or None
    if not isinstance(status, dict):
        logger.warning("pairing refused for now: this Host's Hub did not answer GET /identity")
        return None
    if own and status.get("public_key") != own:
        await arm_hub_identity(pool)
        again = await fetch_status()
        status = again if isinstance(again, dict) else status
    if own and status.get("public_key") == own:
        return own
    if status.get("public_key"):
        # Armed with a key that is not ours: arm_hub_identity has said so loudly.
        return None
    # Reachable, unarmed, and arming failed: the only key it answers is the legacy one.
    return status.get("legacy_public_key") or None


def _answer(status, own: Optional[str], machine_pubkey: str,
            bridge_pubkey: Optional[str]) -> tuple[Optional[str], bool]:
    if status == PRE_IDENTITY:
        # Old Hub: the old answer, the bridge's key, and nobody moves.
        return (bridge_pubkey or None), False
    if not isinstance(status, dict):
        # No answer: no key we know it serves, so no directive this beat.
        return None, False
    if not own or status.get("public_key") != own:
        # Not armed with ours (yet): only a key it names, never the bridge's,
        # and nobody moves.
        return (status.get("legacy_public_key") or None), False
    moving = bool(status.get("legacy_public_key")) and peer_on(status, machine_pubkey) != "host"
    return own, moving


async def pubkey_for_machine(pool, machine_pubkey: str,
                             bridge_pubkey: Optional[str]) -> tuple[Optional[str], bool]:
    """(the Hub key this machine should pin, whether it still has to move).

    A Hub whose image predates /identity keeps the old answer: the bridge's
    key, and no move. A Hub that holds ours: ours, and the machine has to move
    while the legacy identity runs and the Hub has not seen it handshake on
    ours. A move is decided on what the Hub says NOW, not the tick's copy: a
    Hub that restarted since then holds only the platform's key until the next
    tick, and a client sent to probe ours would spend its live tunnel on it and
    mark its endpoint failed. That is one loopback read per beat, only for a
    machine that still has to move."""
    status = await cached_status()
    try:
        own = await hub_wg_pubkey(pool)
    except Exception:
        own = None
    key, moving = _answer(status, own, machine_pubkey, bridge_pubkey)
    if moving:
        key, moving = _answer(await fetch_status(), own, machine_pubkey, bridge_pubkey)
    return key, moving
