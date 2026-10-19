"""Converge the pod-Hub's peer set to THIS host's enrolled bastions.

The Hub sidecar terminates each bastion's WireGuard end-to-end (the relay only
forwards opaque datagrams), so the Hub's in-memory device+registry must hold one
peer per active bastion or it drops the bastion's handshake on the floor. That
state dies with the Hub (pod roll, sidecar restart) and nothing else can rebuild
it: the control API is loopback-only by design, so only this server — same pod —
can program it.

Two writers, one truth (mcp_client_tokens, operator_kind='bastion'):
  - redeem() pushes the new peer immediately, so `pair` works the moment it returns;
  - reconcile_hub_peers_loop() converges every tick — re-pushes after a Hub
    restart, removes peers whose bearer was revoked/expired, survives races.

This module only manages peers whose Name carries the "bastion-" prefix; peers
pushed by other flows (embed nodes, teammate hosts) are never touched.
"""

import asyncio
import base64
import logging
import os
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

# The Hub sidecar's loopback control API (hub/control/control.go). Same pod.
HUB_CONTROL = os.environ.get("HUB_CONTROL", "http://[::1]:8650")
RECONCILE_INTERVAL_S = 15
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=3)

_MANAGED_PREFIX = "bastion-"

_ACTIVE_BASTIONS_SQL = """
    SELECT id,
           notes->>'wg_bastion_pubkey' AS pubkey,
           notes->>'wg_bastion_ip'     AS tunnel_ip
    FROM mcp_client_tokens
    WHERE operator_kind = 'bastion'
      AND revoked_at IS NULL
      AND (expires_at IS NULL OR expires_at > now())
      AND notes ? 'wg_bastion_pubkey'
      AND notes ? 'wg_bastion_ip'
"""


def _peer_body(pubkey: str, tunnel_ip: str, name: str) -> dict:
    # Endpoint stays empty: the bastion is behind NAT and dials IN through the
    # relay; the Hub learns the real endpoint from the first handshake.
    ip = tunnel_ip.split("/")[0]
    return {"PublicKey": pubkey, "Name": name,
            "AllowedIPs": [f"{ip}/128"], "Endpoint": ""}


async def push_bastion_peer(pubkey: str, tunnel_ip: str, name: str) -> bool:
    """POST one peer to the Hub control API. Best-effort: False (never raise)
    when the Hub is absent/unreachable — the reconcile loop retries forever."""
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as s:
            async with s.post(f"{HUB_CONTROL}/peers",
                              json=_peer_body(pubkey, tunnel_ip, name)) as r:
                if r.status // 100 == 2:
                    logger.info("hub peer pushed: %s (%s…)", name, pubkey[:12])
                    return True
                logger.warning("hub peer push %s rejected: HTTP %s %s",
                               name, r.status, (await r.text())[:200])
    except Exception as e:
        logger.debug("hub peer push %s skipped (hub unreachable?): %s", name, e)
    return False


# Arming runs every 15s forever, and its failure states are STEADY states (a hub
# image predating /verifykey stays that way until it rolls). Logging each tick
# would put ~5.7k identical lines per pod per day into the record and bury the
# transition that actually matters, so each distinct condition speaks once and
# goes quiet until it changes.
_arm_said: set[str] = set()


def _say_once(key: str, level: int, msg: str, *args) -> None:
    if key in _arm_said:
        return
    _arm_said.add(key)
    logger.log(level, msg, *args)


def _reset_say_once() -> None:
    """Arming succeeded: let every condition speak again if it recurs, so a
    hub that rolls back to an image without arming is reported a second time."""
    _arm_said.clear()


async def arm_hub_pdp(pool, entity_id: int) -> bool:
    """Arm the Hub's PDP with this host's Ed25519 verify key.

    The Hub starts UNARMED and admits unsigned grants and unverified policy. The
    key is derived in-pod from kernel_secrets on first boot, so the operator has
    nothing to put in HUB_NAC_VERIFY_KEY at render time — only this server, in
    this pod, can supply it.

    Called on every converge tick rather than once at startup, for the reason in
    this module's docstring: the Hub's state dies with the sidecar, so a restarted
    Hub must be re-armed without restarting the host. Arming is idempotent for the
    same key (204) and refused for a different one (409).

    Best-effort like the peer push — never raises. A Hub we cannot reach is a Hub
    that is not carrying traffic either.
    """
    from server.network import nac_stepup
    try:
        pub = await nac_stepup.entity_verify_pubkey(pool, entity_id)
    except Exception as e:
        _say_once("nokey", logging.WARNING,
                  "hub PDP arm skipped: no host verify key yet (%s)", e)
        return False
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as s:
            async with s.post(f"{HUB_CONTROL}/verifykey",
                              json={"key": base64.b64encode(pub).decode()}) as r:
                if r.status // 100 == 2:
                    _reset_say_once()
                    return True
                if r.status == 409:
                    # Armed with a DIFFERENT key. Never benign: entity_verify_pubkey
                    # derives from the write-once entity_sign row, so this host's key
                    # does not legitimately change — something else armed this Hub.
                    # Loud, because it is now enforcing against a key we cannot sign
                    # for, so every widen this host approves will fail.
                    #
                    # Recovery needs the sidecar restarted, which this pod cannot do
                    # (no service account, by design). So this is a report,
                    # not a self-heal: the alert lane and /status carry it out.
                    _say_once("mismatch", logging.ERROR,
                              "hub PDP is armed with a DIFFERENT key — this host's "
                              "approvals will not verify until the hub sidecar is "
                              "restarted (the pod cannot do this itself)")
                    return False
                if r.status == 404:
                    # Hub image predates /verifykey; it stays unarmed until the
                    # sidecar rolls. This is the expected state on every pod between
                    # shipping this and rolling the hub image, so it must not spam:
                    # at 15s a tick that would be ~5.7k lines/pod/day.
                    _say_once("pre_r144", logging.WARNING,
                              "hub PDP cannot be armed: this hub image has no "
                              "/verifykey endpoint (an older hub image) — running UNARMED")
                    return False
                _say_once(f"http{r.status}", logging.WARNING,
                          "hub PDP arm rejected: HTTP %s %s",
                          r.status, (await r.text())[:200])
    except Exception as e:
        logger.debug("hub PDP arm skipped (hub unreachable?): %s", e)
    return False


async def active_bastions(pool) -> dict[str, tuple[str, str]]:
    """{machine pubkey: (tunnel ip, peer name)} — every machine the Hub must
    serve. The converge pushes exactly this set, and hub_identity retires the
    platform's key only once every one of these is on the Host's."""
    rows = await pool.fetch(_ACTIVE_BASTIONS_SQL)
    # Newest row wins per pubkey (re-pairs mint new rows; old ones age out).
    return {r["pubkey"]: (r["tunnel_ip"].split("/")[0], f"{_MANAGED_PREFIX}{r['id']}")
            for r in sorted(rows, key=lambda r: r["id"])}


async def _converge_once(pool) -> None:
    want = await active_bastions(pool)

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as s:
        async with s.get(f"{HUB_CONTROL}/peers") as r:
            r.raise_for_status()
            have = {p.get("PublicKey"): p for p in (await r.json() or [])}

        for pubkey, (ip, name) in want.items():
            existing = have.get(pubkey)
            if existing and set(existing.get("AllowedIPs") or []) == {f"{ip}/128"}:
                continue
            async with s.post(f"{HUB_CONTROL}/peers",
                              json=_peer_body(pubkey, ip, name)) as pr:
                if pr.status // 100 == 2:
                    logger.info("hub peer converged: %s (%s…)", name, pubkey[:12])
                else:
                    logger.warning("hub peer converge %s rejected: HTTP %s",
                                   name, pr.status)

        # Retire managed peers whose bearer is gone (revoked/expired) — and ONLY
        # managed ones; never touch peers other flows pushed.
        for pubkey, peer in have.items():
            if pubkey in want:
                continue
            if not str(peer.get("Name") or "").startswith(_MANAGED_PREFIX):
                continue
            async with s.delete(f"{HUB_CONTROL}/peers?pubkey={quote(pubkey, safe='')}") as dr:
                logger.info("hub peer retired: %s (%s…) HTTP %s",
                            peer.get("Name"), pubkey[:12], dr.status)


def _pod_entity_id() -> int | None:
    """This pod's entity id, from the env the operator sets on the entity
    container. None off-platform (genesis, personal install) — do NOT fall back to
    brain_service._entity_id, which is a hardcoded 1 for the personal-computer
    case and would arm the Hub with the wrong host's key."""
    eid = os.environ.get("ENTITY_ID", "")
    return int(eid) if eid.isdigit() else None


async def reconcile_hub_peers_loop(pool) -> None:
    """Forever-loop: converge the Hub's peers to the active bastion set, and keep
    it holding this Host's WireGuard key and its PDP armed. Quiet when the Hub is
    unreachable (genesis / no sidecar) —
    debug-logs and retries; a Hub that appears later (roll to a real WG identity)
    converges on the next tick without a restart."""
    from server.network import hub_identity
    reachable = None
    armed = False
    keyed = False
    entity_id = _pod_entity_id()
    # Work-first loop: without this the first converge lands in the same instant
    # as every other worker the lifespan just dispatched. Phase only — the 15s
    # cadence is unchanged.
    from server.loop_splay import splay
    await splay(RECONCILE_INTERVAL_S, "hub_peers")
    while True:
        try:
            # Arm EVERY tick, not once: the Hub's state dies with the sidecar, so
            # a latched "already armed" would leave a restarted Hub unarmed for
            # the rest of this process's life — the exact failure this converges
            # against. The call is idempotent server-side; `armed` only tracks
            # transitions so the log says something on change and stays quiet
            # otherwise. Arm before converging peers, so a Hub that just came back
            # verifies before it carries traffic rather than a tick later.
            #
            # The WireGuard key goes first of all: a Hub without it
            # terminates no tunnel, so nothing behind it carries anything yet.
            # Same every-tick reason, and the same tick retires the platform's
            # old key once every machine has moved off it (hub_identity).
            if entity_id is not None:
                now_keyed = await hub_identity.converge_identity(pool)
                if now_keyed != keyed:
                    logger.info("hub WireGuard identity %s",
                                "is this Host's own key" if now_keyed
                                else "is NOT this Host's key")
                    keyed = now_keyed
                now_armed = await arm_hub_pdp(pool, entity_id)
                if now_armed != armed:
                    logger.info("hub PDP %s",
                                "armed with this host's verify key" if now_armed
                                else "NOT armed — grants are admitted unverified")
                    armed = now_armed
            await _converge_once(pool)
            if reachable is not True:
                logger.info("hub peer reconcile: hub reachable, converging every %ss",
                            RECONCILE_INTERVAL_S)
                reachable = True
        except Exception as e:
            if reachable is not False:
                logger.info("hub peer reconcile idle (hub unreachable: %s)", e)
                reachable = False
            else:
                logger.debug("hub peer reconcile skipped: %s", e)
        await asyncio.sleep(RECONCILE_INTERVAL_S)
