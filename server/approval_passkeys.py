"""A passkey tap approves one exact request.

Every act reserved for the person reaches
them as a pop-up over the chat, and typing a fresh authenticator code on each
one is friction. A passkey is one tap, and it holds where the code does not:
its private key never leaves the person's device, while the code's seed sits
in kernel_secrets, which the Host's own workload can read (nac_stepup.py
header).

The passkey is the owner's own (server/owner_passkeys.py): the one they sign
in with, made on /passkey, kept with its public key in owner_passkeys, its
relying party this Host's desktop name exactly (or the remote address's, for
one made on a phone there). Nothing new is set up and nothing new is stored:
an approval is an assertion checked against a passkey this Host already holds.

WHAT ONE APPROVAL IS FOR (its purpose, computed by the server, never taken
from the request):
  - a card over the chat (a decision ticket): its id, the option pressed, and
    a digest of what the card says when it is answered: its words, that
    option, the platform's own payload (the machine, the Host, the check) and
    the boxes ticked (purpose_for_ticket);
  - one request a card sends to a route of its own (the machine-share card):
    its method, its path and a digest of its JSON body with the proof left out
    (purpose_for_request).

THE CHALLENGE IS DERIVED FROM THE PURPOSE: 16 random bytes, then the first 16
bytes of SHA-256(domain || those bytes || purpose). The browser signs the
challenge, so the passkey's signature covers the purpose's digest. When the
approval comes back the server computes the purpose again from what it is
about to act on and checks the challenge against it, so an approval made for
one request, one option or one set of boxes verifies for no other, even
before the store is consulted. Each challenge is also kept here, with its
purpose and the address it was asked on, for CHALLENGE_TTL_S, and is taken
out when it is checked, right or wrong: it is used once.

Every check asks the authenticator to verify the person (user verification:
the device's PIN, face or fingerprint), the browser's origin must be the
address the challenge was asked on, the passkey must be one made for that
address, and its signature counter must not go back.

THE ONE CHOKE POINT stays nac_stepup.verify_stepup. The proof travels in the
code's slot ("passkey:<json>"); verify_stepup accepts it only for a purpose
its caller passes, under the same attempt limit as a code, so a caller that
passes none (every Host tool, every route not changed for this) keeps taking
codes alone, and a proof relayed there is refused without being counted. The
code keeps working everywhere exactly as before.

Challenges live in this process (the server is one process, server/launch.py),
like the owner's sign-in ceremonies; a restart drops them, and the person taps
again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger("cseries.approval_passkeys")

# Long enough to read the device's prompt and answer it; an approval is asked
# for when the person presses the button, not before.
CHALLENGE_TTL_S = 120.0
PROOF_PREFIX = "passkey:"
PROOF_MAX_LEN = 16384
_NONCE_LEN = 16
_TAG_LEN = 16
_DOMAIN = b"cseries-approval/v1\x00"
_PENDING_MAX = 256

_TICKET_PREFIX = "ticket:"
_REQUEST_PREFIX = "request:"
# The field a proof rides in; never part of what a request's purpose digests.
PROOF_FIELD = "stepup_code"

# What a card says when a passkey approval was refused, in place of the
# code's "that code didn't work".
REFUSED = ("Your passkey didn't approve this: the request ran out or changed, or the passkey "
           "could not be checked. Try again, or use a code instead. Nothing was changed.")

# challenge -> (purpose, desktop name it was asked on, use by (monotonic))
_pending: dict[bytes, tuple[str, str, float]] = {}


class ApprovalPasskeyError(Exception):
    """A refusal a route answers with `status` and `detail` (plain words)."""

    def __init__(self, status: int, detail: str, *, no_passkey: bool = False):
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail
        self.no_passkey = bool(no_passkey)


def _now() -> float:
    return time.monotonic()


# ── purposes ──────────────────────────────────────────────────────────────

def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=str).encode()


def _digest(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()[:32]


def normalize_choices(choices: Any) -> Optional[list[str]]:
    """The boxes ticked, as a purpose digests them: None when the card sent
    none (its defaults), else the distinct ids in order. A list of anything
    else is digested as its strings, so it can only fail to match."""
    if choices is None:
        return None
    if not isinstance(choices, (list, tuple)):
        return ["<not a list>"]
    return sorted({str(c) for c in choices})


def purpose_for_ticket(ticket_id: int, option_id: str, *, sitrep: Any, option: Any,
                       kind: Any, payload: Any, choices: Any = None, acted_on: Any = None) -> str:
    """What answering this card with this option approves: the ticket, the
    option, and exactly what the card says (its words, the option's label and
    consequence, the platform's payload for its kind, the boxes ticked), plus
    `acted_on`: what the act reaches that the card does not hold (a messaging
    connector's card lets in the account that sent the code, kept on its
    grant). A card rewritten, or an account changed, after the challenge was
    asked for no longer matches."""
    content = {"sitrep": sitrep if isinstance(sitrep, str) else "",
               "option": option if isinstance(option, dict) else {},
               "kind": kind if isinstance(kind, str) else "",
               "payload": payload if payload is not None else None,
               "choices": normalize_choices(choices),
               "acted_on": acted_on}
    return f"{_TICKET_PREFIX}{int(ticket_id)}:{option_id}:{_digest(content)}"


# A kind whose act reads its payload from keys other than the kind's own
# name: a board post card keeps the request it posts under "board_post" and
# what runs after it under "board_post_then" (runtime/board_post_approval
# PAYLOAD_KEY, THEN_KEY), so md[kind] would digest nothing it acts on.
_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "board_post_proposal": ("board_post", "board_post_then"),
}


def ticket_purpose(ticket_id: int, sitrep: Any, options: Any, metadata: Any,
                   option_id: str, choices: Any = None, acted_on: Any = None) -> Optional[str]:
    """purpose_for_ticket from a ticket row as the decide reads it (options
    and metadata as stored, JSON text or parsed), or None when the option is
    not on the ticket."""
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except ValueError:
            options = []
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    md = metadata if isinstance(metadata, dict) else {}
    chosen = next((o for o in (options if isinstance(options, list) else [])
                   if isinstance(o, dict) and o.get("id") == option_id), None)
    if not option_id or chosen is None:
        return None
    kind = md.get("kind") if isinstance(md.get("kind"), str) else ""
    keys = _PAYLOAD_KEYS.get(kind)
    payload = ({k: md.get(k) for k in keys} if keys
               else md.get(kind) if kind else None)
    return purpose_for_ticket(ticket_id, option_id, sitrep=sitrep, option=chosen, kind=kind,
                              payload=payload, choices=choices, acted_on=acted_on)


async def acted_on(pool, metadata: Any) -> Any:
    """What a card's act reaches beyond the card itself, read now: for a
    messaging connector's card, the account its grant matched (the one 'Let
    this account in' admits). None for every other card, and None when it
    cannot be read (the purpose then cannot match one made while it could)."""
    md = metadata if isinstance(metadata, dict) else {}
    if md.get("kind") != "connector_pairing" or pool is None:
        return None
    try:
        gid = int((md.get("connector_pairing") or {}).get("grant_id"))
        row = await pool.fetchrow(
            "SELECT platform, account_id, pairing FROM nac_connector_grants WHERE id=$1", gid)
    except Exception:  # noqa: BLE001
        return {"unreadable": True}
    if row is None:
        return {"gone": True}
    return {"platform": row["platform"], "account": row["account_id"], "pairing": row["pairing"]}


def purpose_for_request(method: str, path: str, body: Any) -> Optional[str]:
    """What an approval of one request is for: its method, its path and a
    digest of its JSON body with the proof (PROOF_FIELD) left out. None for a
    body that is no JSON object."""
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return None
    m = str(method or "").upper()
    p = str(path or "")
    rest = {k: v for k, v in body.items() if k != PROOF_FIELD}
    return f"{_REQUEST_PREFIX}{m} {p}:{_digest(rest)}"


# ── the challenge ─────────────────────────────────────────────────────────

def _tag(nonce: bytes, purpose: str) -> bytes:
    return hashlib.sha256(_DOMAIN + nonce + purpose.encode("utf-8")).digest()[:_TAG_LEN]


def challenge_for(nonce: bytes, purpose: str) -> bytes:
    """The challenge for `purpose`: the nonce, then its tag."""
    return nonce + _tag(nonce, purpose)


def challenge_matches(challenge: bytes, purpose: str) -> bool:
    if not isinstance(challenge, (bytes, bytearray)) or len(challenge) != _NONCE_LEN + _TAG_LEN:
        return False
    return hmac.compare_digest(bytes(challenge[_NONCE_LEN:]), _tag(bytes(challenge[:_NONCE_LEN]), purpose))


def _keep(challenge: bytes, purpose: str, host: str) -> None:
    now = _now()
    for k in [k for k, v in _pending.items() if v[2] <= now]:
        del _pending[k]
    while len(_pending) >= _PENDING_MAX:
        del _pending[next(iter(_pending))]
    _pending[challenge] = (purpose, host.lower(), now + CHALLENGE_TTL_S)


def _origin_for(host: str):
    from server import home_origin
    return home_origin.desktop_origin(host)


async def available(pool, host: str) -> bool:
    """Whether a passkey can approve on the address `host` names: it is one
    of this Host's desktop names, and the owner has a passkey made for it."""
    origin = _origin_for(host)
    if origin is None or pool is None:
        return False
    try:
        from server import owner_passkeys
        return await owner_passkeys.passkey_count(pool, origin) > 0
    except Exception:  # noqa: BLE001 — no passkey state: the code decides
        return False


async def options(pool, host: str, purpose: str) -> dict:
    """Options for navigator.credentials.get, for exactly `purpose`, offering
    the owner's passkeys for the address `host` names. Refused when that is
    not one of this Host's desktop names, or there is no passkey for it."""
    if not purpose:
        raise ApprovalPasskeyError(400, "There is nothing to approve here.")
    origin = _origin_for(host)
    if origin is None:
        raise ApprovalPasskeyError(400, "A passkey approves only on this Host's own address.",
                                   no_passkey=True)
    from server import owner_passkeys
    import webauthn
    from webauthn.helpers import structs
    allow = await owner_passkeys._descriptors(pool, origin)
    if not allow:
        raise ApprovalPasskeyError(409, "This Host has no passkey for this address yet.",
                                   no_passkey=True)
    challenge = challenge_for(secrets.token_bytes(_NONCE_LEN), purpose)
    opts = webauthn.generate_authentication_options(
        rp_id=origin.host, challenge=challenge, timeout=int(CHALLENGE_TTL_S * 1000),
        allow_credentials=allow, user_verification=structs.UserVerificationRequirement.REQUIRED)
    _keep(challenge, purpose, origin.host)
    return {"options": json.loads(webauthn.options_to_json(opts))}


# ── the proof as it travels in `stepup_code` ──────────────────────────────

def is_proof(code: Any) -> bool:
    return isinstance(code, str) and code.startswith(PROOF_PREFIX)


def _b64u(s: Any) -> Optional[bytes]:
    if not isinstance(s, str) or not s or len(s) > PROOF_MAX_LEN:
        return None
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError):
        return None


def proof_challenge(cred: Any) -> Optional[bytes]:
    """The challenge the browser says it signed (clientDataJSON.challenge),
    used to find the challenge; the library checks it again."""
    resp = cred.get("response") if isinstance(cred, dict) else None
    raw = _b64u(resp.get("clientDataJSON")) if isinstance(resp, dict) else None
    if raw is None:
        return None
    try:
        cd = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return _b64u(cd.get("challenge")) if isinstance(cd, dict) else None


def parse_proof(code: Any) -> Optional[dict]:
    """The browser's assertion (a PublicKeyCredential as JSON) carried as
    "passkey:<json>", or None when it is not shaped like one. A shape check
    only: the signature is checked in verify_in."""
    if not is_proof(code) or len(code) > PROOF_MAX_LEN:
        return None
    try:
        cred = json.loads(code[len(PROOF_PREFIX):])
    except (ValueError, TypeError):
        return None
    if not isinstance(cred, dict) or cred.get("type") != "public-key":
        return None
    resp = cred.get("response")
    if not isinstance(resp, dict):
        return None
    for k in ("clientDataJSON", "authenticatorData", "signature"):
        if not isinstance(resp.get(k), str) or not resp.get(k):
            return None
    if _b64u(cred.get("rawId") or cred.get("id")) is None or proof_challenge(cred) is None:
        return None
    return cred


async def verify_in(conn, cred: dict, purpose: str) -> bool:
    """Check one approval made with a passkey, on `conn` (inside
    nac_stepup.verify_stepup's transaction, which holds the attempt limit).
    True only when all of these hold: the challenge was asked for here, for
    exactly `purpose`, under CHALLENGE_TTL_S ago and not used; it is the one
    derived from `purpose`; the passkey is one of the owner's, made for the
    address the challenge was asked on; the browser was on that address; the
    device verified the person; the signature checks; the counter did not go
    back. The challenge is used up whatever the answer; on True the passkey's
    counter is recorded."""
    if not purpose or not isinstance(cred, dict):
        return False
    chal = proof_challenge(cred)
    if chal is None:
        return False
    entry = _pending.pop(chal, None)
    if entry is None:
        return False
    want, host, use_by = entry
    if use_by <= _now() or not hmac.compare_digest(want.encode(), purpose.encode()):
        return False
    if not challenge_matches(chal, purpose):
        return False
    origin = _origin_for(host)
    raw = _b64u(cred.get("rawId") or cred.get("id"))
    if origin is None or raw is None or len(raw) > 1023:
        return False
    from server import owner_passkeys
    row = await conn.fetchrow(
        "SELECT public_key, sign_count FROM owner_passkeys "
        "WHERE credential_id = $1 AND rp_id = ANY($2::text[]) FOR UPDATE",
        raw, owner_passkeys.rp_ids(origin))
    if row is None:
        return False
    handle = (cred.get("response") or {}).get("userHandle")
    if handle and _b64u(handle) != owner_passkeys.USER_HANDLE:
        return False
    import webauthn
    try:
        got = webauthn.verify_authentication_response(
            credential=cred, expected_challenge=chal, expected_rp_id=origin.host,
            expected_origin=origin.origin, credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=int(row["sign_count"]), require_user_verification=True)
    except Exception as exc:  # noqa: BLE001 — every library refusal is a refusal
        logger.info("approval passkey refused: %s", type(exc).__name__)
        return False
    await conn.execute(
        "UPDATE owner_passkeys SET sign_count = GREATEST(sign_count, $2), last_used_at = now() "
        "WHERE credential_id = $1", raw, int(got.new_sign_count))
    return True
