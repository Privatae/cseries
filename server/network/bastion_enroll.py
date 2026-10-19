"""
Bastion enrollment — the host's "Set up a bastion" action (token-from-host model).

The user installs the generic **cseries-bastion** binary (npm / a plain binary —
no Docker) and runs `cseries-bastion pair <token>`. The bastion
generates its OWN WireGuard + mTLS keys (private keys never leave it) and pulls only
its cert + routing from THIS host using a short, single-use pairing token. Nothing
sensitive is downloaded or shipped; the only thing the user copies is the token.

Two host actions:
  mint_pairing_token  — "Set up a bastion" mints a short-lived single-use token
                        (only its sha256 is stored), shown once on the dashboard.
  redeem              — the public, token-authed endpoint the bastion hits: it
                        verifies the token, asks the bridge (control plane) to SIGN
                        the bastion's CSR + register the concentrator peer
                        (bastion.enroll, scoped to THIS host), mints the bearer in
                        this host's own brain DB, and returns the cert + routing.

The bridge is OUT of the data path after enrollment — the bastion talks to this host
directly over the WireGuard tunnel.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from typing import Optional

from fastapi import HTTPException

from server import host_names as host_names_mod
from cryptography import x509

logger = logging.getLogger("privatae.network.bastion_enroll")

BASTION_BINARY = "privatae-bastion"
PAIRING_TTL_SECONDS = 900  # 15 min — short single-use window for the pairing token
BASTION_SCOPES = ["shape", "recall", "signal"]

# Fork 4 (2026-06-20): the device fingerprint travels as a URI SAN in the
# device-signed operator CSR (urn:privatae:hwfp:<fp>), so it is bound at enrollment from
# a structure the device signed — not TOFU'd from a self-reported heartbeat field.
_FP_SAN_PREFIX = "urn:privatae:hwfp:"


def _fp_from_csr(operator_csr: str) -> Optional[str]:
    """Extract the device fingerprint from the operator CSR's URI SAN — but only from a
    CSR whose own signature verifies (the device signed it, so a fingerprint tampered
    after signing breaks the signature and is rejected). Returns None if the CSR is
    unparseable, its self-signature is invalid, or it carries no fingerprint SAN (an old
    client) — a NULL fp yields a bearer-only token (full access is the user's sovereign
    choice), never a silent device-bind."""
    if not operator_csr:
        return None
    try:
        csr = x509.load_pem_x509_csr(operator_csr.encode())
    except Exception:
        return None
    try:
        if not csr.is_signature_valid:
            logger.warning("bastion redeem: operator CSR self-signature invalid — fingerprint ignored")
            return None
    except Exception:
        return None
    try:
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return None
    for uri in san.value.get_values_for_type(x509.UniformResourceIdentifier):
        if uri.startswith(_FP_SAN_PREFIX):
            fp = uri[len(_FP_SAN_PREFIX):].strip()
            return fp or None
    return None

# bastion_pairing_tokens is declared in migrations/v145_band_b_bastion_enroll.sql
# and applied at boot by entrypoint.sh. This module does not create it.


def _host_name(handle: str) -> str:
    """The canonical name for a NEW pairing. See server/host_names.py."""
    return host_names_mod.host_name(handle)


def _host_names(handle: str) -> tuple:
    """Every name this host answers to, canonical first — what an EXTERNAL
    value (a bastion's heartbeat, a signed record) must be checked against."""
    return host_names_mod.host_names(handle)


def _mint_bearer() -> tuple[str, bytes]:
    """Plaintext bearer (returned once to the bastion) + its sha256 (stored)."""
    raw = "mcp_" + secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).digest()


async def _bridge_op(op: str, params: dict) -> dict:
    """Control-plane call to the CEIGAS bridge (mirrors contacts._bridge_op). The
    bridge derives this host's entity from its mTLS cert CN, so bastion.enroll is
    self-scoped — a host can only enroll bastions for itself."""
    from server.main import get_bridge_client

    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        logger.error("bastion %s failed: bridge client not connected", op)
        raise HTTPException(503, "bridge not connected — cannot enroll the bastion")
    envelope = await bc.request_full(op, params)
    if not envelope.get("ok"):
        code = int(envelope.get("code") or 502)
        detail = envelope.get("error") or f"{op} failed"
        # This branch used to raise the bridge's status code and log NOTHING, so
        # a failed enrolment left no trace on the host, the edge or the bridge — the
        # operator saw only "redeem rejected: HTTP 502" on the client and had nowhere to
        # look. Measured 2026-09-07: roughly 40% of a 20-machine pairing run hit it.
        # The detail is the bridge's own message; params are NOT logged (they carry the
        # bastion's pubkey and CSR).
        logger.error("bastion %s failed: bridge answered code=%s detail=%s", op, code, detail)
        raise HTTPException(code, detail)
    return envelope.get("data") or {}


_self_handle_cache: dict[int, str] = {}


async def resolve_self_handle(entity_id: int) -> Optional[str]:
    """This host's own primary synaptive handle. Tries ENTITY_HANDLE/HANDLE env
    first (cheap), else asks the bridge (synaptive.handle.self) — the pod can't read
    the platform handle registry directly (platform-only + egress-moated). Cached per
    entity for the process lifetime. Returns None if unresolvable (bridge down / no
    handle yet) so the caller can surface a clean 409."""
    env = (os.environ.get("ENTITY_HANDLE") or os.environ.get("HANDLE") or "").strip()
    if env:
        return env
    # No platform: there is no handle registry to ask, and the Host's own
    # setting is its handle — the first label of CSERIES_URL (home_origin).
    from server.network import local_bastion
    if local_bastion.standalone():
        from server.home_origin import HOME
        return HOME.handle
    cached = _self_handle_cache.get(entity_id)
    if cached:
        return cached
    try:
        data = await _bridge_op("synaptive.handle.self", {})
    except Exception as e:  # bridge down / op error — caller handles the None
        logger.info("self-handle resolve via bridge failed: %s", e)
        return None
    handle = (data.get("handle") or "").strip()
    if handle:
        _self_handle_cache[entity_id] = handle
    return handle or None


async def _rearm_pairing_token(pool, secret_hash: bytes) -> None:
    """Undo the single-use consume when enrolment failed before this host had a bearer.

    redeem() consumes the token atomically before the control-plane call, which is
    what makes it replay-safe. The cost was that any control-plane fault destroyed a
    single-use 15-minute credential and the user could not retry — worst exactly when
    the bridge is already unhealthy.

    This CANNOT weaken single-use. The consume is one PK-keyed UPDATE guarded by
    `used_at IS NULL`, so of two racing redeems the loser matches 0 rows and raises
    401 before ever reaching this function — only the request that actually consumed
    the token can re-arm it, and only its own mark. expires_at (NOT NULL) still bounds
    the token either way, and a successful enrolment never lands here.

    It does NOT mean nothing exists upstream: see the call site — three bridge paths
    commit a bastion_peers row before failing, and the orphan is inert. The client has
    held this line since 0.10.0 — see client-go/pair.go: "Conflicts are checked BEFORE
    the redeem so a refusal doesn't burn the single-use token."

    Best-effort by design: a failure here must never mask the original error.
    """
    try:
        status = await pool.execute(
            "UPDATE bastion_pairing_tokens SET used_at = NULL "
            "WHERE secret_hash = $1 AND expires_at > now()",
            secret_hash,
        )
        # asyncpg returns the command tag ("UPDATE 1" / "UPDATE 0"). Claiming a
        # re-arm that did not happen — the token expired in the meantime — would
        # send the next reader looking for a bug in the wrong place.
        if isinstance(status, str) and status.rsplit(" ", 1)[-1] == "0":
            logger.info("pairing token NOT re-armed — it expired during enrolment")
        else:
            logger.info("pairing token re-armed after failed enrolment")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("pairing token re-arm failed: %s", e)


async def mint_pairing_token(pool, *, entity_id: int, handle: Optional[str], name: str) -> dict:
    """Mint a short-lived single-use pairing token for THIS host. Returns the token
    (shown once) + the run instructions. Only the sha256 is stored."""
    handle = (handle or "").strip()
    if not handle:
        raise HTTPException(409, "this host has no handle yet — set one before issuing a bastion")
    secret = secrets.token_urlsafe(24)
    secret_hash = hashlib.sha256(secret.encode()).digest()
    await pool.execute(
        "INSERT INTO bastion_pairing_tokens (secret_hash, name, expires_at) "
        "VALUES ($1, $2, now() + ($3 || ' seconds')::interval)",
        secret_hash, name, str(PAIRING_TTL_SECONDS),
    )
    # The token carries WHERE to redeem (the handle) + the secret, in one string.
    token = "skb_" + base64.urlsafe_b64encode(f"{handle}.{secret}".encode()).decode().rstrip("=")
    logger.info("bastion pairing token minted: entity=%d name=%s ttl=%ds", entity_id, name, PAIRING_TTL_SECONDS)
    # No platform: the bastion is on this machine and redeems on the Host's
    # own socket (server/network/local_bastion.py), not the platform's ingress.
    from server.network import local_bastion
    pair = (f"{BASTION_BINARY} pair --redeem-url unix://{local_bastion.enroll_socket()} {token}"
            if local_bastion.standalone() else f"{BASTION_BINARY} pair {token}")
    return {
        "token": token,
        "expires_in_s": PAIRING_TTL_SECONDS,
        "name": name,
        "run": {
            "install": ("Windows (PowerShell):  irm https://privatae.ai/bastion/install.ps1 | iex\n"
                        "macOS / Linux:  curl -fsSL https://privatae.ai/bastion/install.sh | sh"),
            "pair": pair,
            # THE THIRD STEP, which this payload never mentioned and so neither did
            # the dashboard — the word "anchor" appeared in the Network app only as
            # a STATE to display, never as something to do. A machine other people's
            # hosts pair to has to be switched to networked mode and anchored, and a
            # user who was never told either command cannot get there from here.
            #
            # No token on the second command, deliberately: the box is already
            # paired, so the host it anchors to is not in question, and asking for
            # one meant minting a SECOND pairing token (this endpoint again) to
            # redeem against a machine already paired — the first is consumed at
            # redeem. Needs bastion >= 0.23.0 (where claim-anchor's
            # token is optional); older boxes still take a token there.
            "share": ("Only if OTHER people's hosts will pair to this machine — "
                      "run both on the machine, as administrator:\n"
                      f"  {BASTION_BINARY} set-mode networked\n"
                      f"  {BASTION_BINARY} claim-anchor\n"
                      "That makes this host the machine's anchor: every host that "
                      "pairs there afterwards waits for your approval. No second "
                      "token — it anchors to the pairing you just made."),
            "elevate": ("Linux / macOS:  prefix both with `sudo -H`\n"
                        "Windows:  run them from an Administrator PowerShell "
                        "(right-click → Run as administrator)"),
            # No MCP endpoint on the bastion:
            # nothing on that machine drives this host. The host reaches the
            # machine only through lanes the bastion opens and gates itself.
            "note": ("Run on the machine you want as a bastion. It generates its own "
                     "keys, pulls its cert from this host with the token, and opens the "
                     "lanes this host uses to reach that machine. The token is single-use "
                     "and expires in 15 minutes."),
        },
    }


async def redeem(pool, *, entity_id: int, handle: Optional[str], secret: str,
                 bastion_pubkey: str, operator_csr: str,
                 hostname: str = "", platform: str = "") -> dict:
    """Public, token-authed: verify the pairing token, sign the bastion's CSR +
    register its concentrator peer (via the bridge, scoped to THIS host), mint the
    bearer, and return the cert + routing. The bastion holds all private keys."""
    handle = (handle or "").strip()
    secret = (secret or "").strip()
    bastion_pubkey = (bastion_pubkey or "").strip()
    operator_csr = (operator_csr or "").strip()
    if not secret or not bastion_pubkey or "CERTIFICATE REQUEST" not in operator_csr:
        raise HTTPException(400, "secret, wg_public_key, operator_csr required")
    if not handle:
        raise HTTPException(409, "host handle unset")

    secret_hash = hashlib.sha256(secret.encode()).digest()
    # Verify + atomically consume the token (single-use): only succeeds if unused +
    # unexpired. Marking used in the same UPDATE prevents replay/races.
    row = await pool.fetchrow(
        "UPDATE bastion_pairing_tokens SET used_at = now() "
        "WHERE secret_hash = $1 AND used_at IS NULL AND expires_at > now() "
        "RETURNING name",
        secret_hash,
    )
    if row is None:
        raise HTTPException(401, "invalid, expired, or already-used pairing token")
    name = row["name"] or "bastion"

    # Control plane: sign the CSR + register the concentrator peer (self-scoped).
    # The token was consumed above, so without a re-arm any failure here permanently
    # destroys it. Most of these create nothing: bridge disconnected (503), no active
    # endpoint (503), CSR sign failure (500), pubkey already enrolled (409).
    #
    # THREE do leave an orphan behind, because the bridge INSERTs bastion_peers
    # (adapter.py:5839) BEFORE its own remaining checks: ca read failed (500,
    # adapter.py:5876), no pod-Hub identity (503, adapter.py:5886), and a client-side
    # timeout — bastion.enroll is not in LONG_OPS, so it gets REQUEST_TIMEOUT=30
    # (bridge_client.py:50) while the cert-manager sign loop alone can poll 30x1s
    # (adapter.py:5769-5779), and the bridge commits the row regardless.
    #
    # Re-arming anyway is deliberate. The orphan is inert: the client holds the WG
    # private key in memory and discards it on any error return (client-go/pair.go),
    # and its signed cert only ever existed in the reply nobody received — so nobody
    # can use that peer, and refusing the user's retry helps no one. The orphan row
    # (and the relay port it consumes) is pre-existing behaviour on this path, not
    # something the re-arm introduces; the bridge-side ordering is the real defect
    # and is outside this module.
    #
    # Scoped deliberately to the pre-mutation window HERE: once the bastion row below
    # is INSERTed this host has a bearer, the pairing is real, and it is never re-armed.
    #
    # The Hub key the machine pins is THIS Host's (hub_identity), never
    # the bridge's: the platform no longer makes or holds it, so the bridge's
    # endpoint_pubkey may be null and is used only while this pod's Hub still
    # predates /identity and serves the operator-made key.
    try:
        enroll = await _bridge_op("bastion.enroll", {
            "bastion_pubkey": bastion_pubkey,
            "operator_csr": operator_csr,
        })
        for k in ("operator_crt", "ca_crt", "endpoint",
                  "host_tunnel_ip", "bastion_tunnel_ip"):
            if not enroll.get(k):
                raise HTTPException(502, f"bridge bastion.enroll omitted {k}")
        from server.network import hub_identity
        endpoint_pubkey = await hub_identity.pubkey_for_new_machine(
            pool, enroll.get("endpoint_pubkey"))
        if not endpoint_pubkey:
            raise HTTPException(503, "this Host's Hub holds no WireGuard key it can "
                                     "pair a machine to yet — try again in a minute")
    except Exception:
        await _rearm_pairing_token(pool, secret_hash)
        raise

    # Fork 4: bind the device fingerprint NOW, from the device-signed CSR — not TOFU'd
    # on a later heartbeat (which raced the copy-config window and trusted a self-report).
    # NULL when the CSR carries none (old client) => a bearer-only token.
    fp = _fp_from_csr(operator_csr)
    # The bearer — minted locally into this host's brain DB.
    bearer, token_hash = _mint_bearer()
    url_handle = secrets.token_hex(8)
    notes = {
        "wg_bastion_pubkey": bastion_pubkey,
        "wg_bastion_ip": enroll["bastion_tunnel_ip"],
        "wg_host_ip": enroll["host_tunnel_ip"],
        "wg_endpoint": enroll["endpoint"],
        "wg_endpoints": enroll.get("endpoints") or [enroll["endpoint"]],
        "operator_uuid": enroll.get("operator_uuid"),
        "host_name": _host_name(handle),
        "machine": hostname or None,
        "platform": platform or None,
    }
    bastion_row = await pool.fetchrow(
        """
        INSERT INTO mcp_client_tokens
            (entity_id, name, token_hash, url_handle, scopes, operator_kind, notes,
             hardware_fingerprint, expires_at)
        -- B1: bastion bearer gets a sliding 7-day expiry, renewed on each heartbeat
        -- (topology.upsert_heartbeat). Bounds a leaked-but-unused bearer; a live bastion
        -- never expires (it keeps heartbeating). Tunable — defence-in-depth behind the
        -- forwarder's tunnel pinning + revoke.
        VALUES ($1, $2, $3, $4, $5, 'bastion', $6::jsonb, $7,
                now() + interval '7 days')
        RETURNING id
        """,
        entity_id, name, token_hash, url_handle, BASTION_SCOPES, json.dumps(notes), fp,
    )
    logger.info("bastion redeemed: entity=%d token_id=%s name=%s pubkey=%s… fp=%s",
                entity_id, bastion_row["id"], name, bastion_pubkey[:12],
                "csr-bound" if fp else "none(bearer-only)")

    # Hub relay cutover: program the pod-Hub to accept this bastion's WireGuard
    # handshake NOW — the client dials the moment `pair` returns, and the Hub
    # drops handshakes from peers it doesn't hold. The reconcile loop
    # (hub_peers.reconcile_hub_peers_loop) would converge within a tick, but that
    # window would fail the client's first connection. Best-effort: a dev
    # stand-in Hub no-ops the device add, and with no Hub at all the push just
    # logs — the concentrator path needs nothing from this.
    from server.network.hub_peers import push_bastion_peer
    await push_bastion_peer(bastion_pubkey, enroll["bastion_tunnel_ip"],
                            f"bastion-{bastion_row['id']}")

    return {
        "operator_crt": enroll["operator_crt"],
        "ca_crt": enroll["ca_crt"],
        "bearer": bearer,
        "endpoint": enroll["endpoint"],
        # Every relay entry this pairing is served on, preferred first
        # (the router's IPv6 address-per-pairing entry, then the v4 port entry). The
        # client probes in order with a real handshake and pins the first that
        # answers (client-go/rotate.go). A bridge that predates E2 omits it: one entry.
        "endpoints": enroll.get("endpoints") or [enroll["endpoint"]],
        "endpoint_pubkey": endpoint_pubkey,
        "bastion_tunnel_ip": enroll["bastion_tunnel_ip"],
        "host_tunnel_ip": enroll["host_tunnel_ip"],
        "host_name": _host_name(handle),
    }


async def redeem_local(pool, *, entity_id: int, handle: str, secret: str, operator_csr: str,
                       hostname: str = "", platform: str = "", transport: str = "unix") -> dict:
    """The redeem for a Host with no platform, served on enroll.sock and on
    the enroll port (server/network/local_bastion.py) and nowhere else. Same
    request body as redeem(). The token is checked and spent the same way; the
    CSR is signed by this Host's own CA (host_ca) instead of the bridge; there
    is no tunnel, no relay and no Hub, so the answer carries the transport the
    request came in on: "unix" names the lane socket and the runs directory,
    "tcp" the lane's address on the machine.

    redeem() above is not called and not changed: a Host with a platform
    never reaches this function, because only the standalone enroll listeners
    call it. The body's wg_public_key is not read: no tunnel is made."""
    from server.network import host_ca, local_bastion

    if transport not in ("unix", "tcp"):
        raise ValueError(f"no such local transport: {transport!r}")

    secret = (secret or "").strip()
    operator_csr = (operator_csr or "").strip()
    if not secret or "CERTIFICATE REQUEST" not in operator_csr:
        raise HTTPException(400, "secret, operator_csr required")
    own = await resolve_self_handle(entity_id)
    if not own:
        raise HTTPException(409, "host handle unset")
    sent = (handle or "").strip()
    if sent and sent != own:
        # Checked before the token is spent, so a mistyped target costs nothing.
        raise HTTPException(409, f"this token is for another Host ({sent}); this Host is {own}")
    # Refuse a CSR this Host will not sign before the token is spent, too.
    try:
        host_ca.checked_operator_csr(operator_csr)
    except host_ca.CSRRefused as e:
        raise HTTPException(400, str(e))

    secret_hash = hashlib.sha256(secret.encode()).digest()
    row = await pool.fetchrow(
        "UPDATE bastion_pairing_tokens SET used_at = now() "
        "WHERE secret_hash = $1 AND used_at IS NULL AND expires_at > now() "
        "RETURNING name",
        secret_hash,
    )
    if row is None:
        raise HTTPException(401, "invalid, expired, or already-used pairing token")
    name = row["name"] or "bastion"

    try:
        operator_cert, serial = await host_ca.sign_operator_csr(pool, operator_csr)
        ca_cert = await host_ca.ca_cert_pem(pool)
    except Exception:
        await _rearm_pairing_token(pool, secret_hash)
        raise

    fp = _fp_from_csr(operator_csr)
    bearer, token_hash = _mint_bearer()
    url_handle = secrets.token_hex(8)
    import uuid
    notes = {
        "transport": transport,
        "operator_uuid": str(uuid.uuid4()),
        # The lane on the enroll port finds this row by it (local_bastion).
        "cert_serial": serial,
        "host_name": local_bastion.pairing_host_name(own),
        "machine": hostname or None,
        "platform": platform or None,
    }
    bastion_row = await pool.fetchrow(
        """
        INSERT INTO mcp_client_tokens
            (entity_id, name, token_hash, url_handle, scopes, operator_kind, notes,
             hardware_fingerprint, expires_at)
        VALUES ($1, $2, $3, $4, $5, 'bastion', $6::jsonb, $7,
                now() + interval '7 days')
        RETURNING id
        """,
        entity_id, name, token_hash, url_handle, BASTION_SCOPES, json.dumps(notes), fp,
    )
    logger.info("bastion redeemed on the local %s listener: token_id=%s name=%s fp=%s",
                transport, bastion_row["id"], name, "csr-bound" if fp else "none(bearer-only)")
    out = {
        "operator_cert": operator_cert,
        "ca_cert": ca_cert,
        # Today's field names too: client-go/pair.go redeemResp reads these.
        "operator_crt": operator_cert,
        "ca_crt": ca_cert,
        "bearer": bearer,
        "transport": transport,
        "host_handle": own,
        # The one name (local_bastion.pairing_host_name): the bastion files
        # the pairing under it, and a coder grant names it as its subject. On
        # the unix lane it is also the name the lane certificate carries; the
        # TCP lane's is local_bastion.TCP_SERVER_NAME.
        "host_name": local_bastion.pairing_host_name(own),
    }
    if transport == "unix":
        out["lane_socket"] = local_bastion.lane_socket()
        out["runs_dir"] = local_bastion.runs_dir()
    else:
        out["lane_address"] = local_bastion.lane_address()
    return out


async def drop_concentrator_peer(bastion_pubkey: Optional[str]) -> None:
    """Best-effort on revoke: tell the control plane to drop the concentrator peer,
    so a revoked bastion's tunnel goes dead at the network level (the bearer is
    already revoked in-DB, which is the correctness control; this is hardening).

    Reused by C1 quarantine to CUT a still-enrolled bastion's whole mesh reach
    (bastion.unenroll sets bastion_peers.revoked_at; the forwarder drops the WG
    peer on its next reconcile). For quarantine the bearer is deliberately NOT
    revoked, so the cut is reversible via restore_concentrator_peer below."""
    if not bastion_pubkey:
        return
    try:
        await _bridge_op("bastion.unenroll", {"bastion_pubkey": bastion_pubkey})
        logger.info("concentrator peer dropped: %s…", bastion_pubkey[:12])
    except Exception as e:
        logger.info("concentrator peer drop deferred (%s…): %s", bastion_pubkey[:12], e)


async def restore_concentrator_peer(bastion_pubkey: Optional[str]) -> None:
    """C1 release: the exact inverse of drop_concentrator_peer — tell the control
    plane to UN-revoke the concentrator peer for this pubkey (clear revoked_at on
    the most-recent bastion_peers row), so the forwarder re-adds the WireGuard peer
    on its next reconcile and the SAME operator-cert→entity binding comes back live.

    No CSR and no new cert: the peer row (operator_uuid / cert_serial / tunnel IPs)
    is preserved across the hold, so the box returns exactly as it was — the whole
    point of a REVERSIBLE quarantine vs a re-enrol. Best-effort: a restore failure
    never breaks the release of the gate-level quarantine row (clearing that row
    already re-permits the mgmt/harness gates); the peer just rejoins on the next
    successful call / heartbeat reconcile.

    Bridge op `bastion.reenroll` (self-scoped, like unenroll): UPDATE bastion_peers
    SET revoked_at = NULL WHERE bastion_pubkey = $1 AND entity_id = <caller> ORDER BY
    id DESC LIMIT 1 — see the C1 follow-up note. Until that op lands the call no-ops
    cleanly (Unknown operation → logged, swallowed)."""
    if not bastion_pubkey:
        return
    try:
        await _bridge_op("bastion.reenroll", {"bastion_pubkey": bastion_pubkey})
        logger.info("concentrator peer restored: %s…", bastion_pubkey[:12])
    except Exception as e:
        logger.info("concentrator peer restore deferred (%s…): %s", bastion_pubkey[:12], e)
