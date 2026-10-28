"""Is this the person, and did they just confirm it?

Two checks stand before any page that connects an agent or answers its
request:

  * the request is the person's own: the identity the front door vouched for
    (operator_identity.check_operator: FrontDoorProof strips that header from
    anything the front door did not forward), never the peer's address;
  * a fresh factor: a passkey check this session made in the last
    owner_passkeys.STEP_UP_S (the passkey is the factor, the code the
    fallback), or the code from their authenticator, checked and burned by
    the same nac_stepup.verify_stepup every approval pop-up uses, with its
    lock-out on repeated wrong codes.

A page's form carries a token this Host signed over what the page showed, so
the answer posted is bound to the page the person read and cannot be forged
by another site (it cannot read the page to copy the token).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

_FORM_KEY_ID = "agent_access_form_key"
FORM_TTL_S = 1800


def person_email(request) -> str:
    from server.operator_identity import check_operator
    ok, email = check_operator(request.headers)
    return email if ok and email else ""


async def _form_key(pool) -> bytes:
    from server.applications.tokens import _SECRET_SCHEMA
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id=$1", _FORM_KEY_ID)
        if row is None:
            await conn.execute(
                "INSERT INTO kernel_secrets (id, secret) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                _FORM_KEY_ID, os.urandom(32))
            row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id=$1", _FORM_KEY_ID)
        return bytes(row["secret"])


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


async def sign_form(pool, payload: dict) -> str:
    body = json.dumps({**payload, "exp": int(time.time()) + FORM_TTL_S},
                      separators=(",", ":"), sort_keys=True).encode()
    mac = hmac.new(await _form_key(pool), body, hashlib.sha256).digest()
    return _b64(body) + "." + _b64(mac)


async def read_form(pool, token: str) -> Optional[dict]:
    try:
        body_b64, mac_b64 = (token or "").split(".", 1)
        body, mac = _unb64(body_b64), _unb64(mac_b64)
    except (ValueError, TypeError):
        return None
    want = hmac.new(await _form_key(pool), body, hashlib.sha256).digest()
    if not hmac.compare_digest(want, mac):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or int(data.get("exp") or 0) < time.time():
        return None
    return data


def _fresh_passkey(request) -> bool:
    """A passkey check this session made within STEP_UP_S."""
    try:
        from server import owner_passkeys
        from server.middleware.owner_session import _cookie
        return owner_passkeys.fresh_step_up(_cookie(request.scope))
    except Exception:  # noqa: BLE001 — no passkey state: the code decides
        return False


def locked_words(locked) -> str:
    return f"Too many wrong codes. Try again in {int(locked.wait_s)} seconds."


async def confirm(pool, request, code: str) -> tuple[bool, str]:
    """(confirmed, why not). A fresh passkey check, else the authenticator
    code, burned on success."""
    from server.network import nac_stepup
    if _fresh_passkey(request) and not (code or "").strip():
        return True, ""
    if not await nac_stepup.is_enrolled(pool):
        return False, ("Your Host has no authenticator set up yet, so it cannot confirm that this "
                       "is you. Set one up in the Network app, then open this page again.")
    # A caller whose record is waiting gets StepupLocked from here, raised
    # as every code check raises it; the page says how long (locked_words).
    ok = await nac_stepup.verify_stepup(pool, (code or "").strip())
    if not ok:
        return False, "That code was not right, or it was already used. Try the next one."
    return True, ""
