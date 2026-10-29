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
from typing import NamedTuple, Optional

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


async def _fresh_passkey(request) -> bool:
    """A passkey check this session made within STEP_UP_S. The session is the
    first live one the request carries (owner_session.live_session), the one
    the step-up route records the check on: an older cookie of the same name
    sent ahead of it (an app origin can set one for the whole zone) must not
    hide the check the person just made."""
    try:
        from server import owner_passkeys
        from server.middleware.owner_session import live_session
        _email, token = await live_session(request.scope)
        return owner_passkeys.fresh_step_up(token)
    except Exception:  # noqa: BLE001 — no passkey state: the code decides
        return False


def locked_words(locked) -> str:
    return f"Too many wrong codes. Try again in {int(locked.wait_s)} seconds."


NO_FACTOR_WORDS = ("Your Host has no passkey or authenticator code set up yet, so it cannot check "
                   "that this is you. Add a passkey on your Host's /passkey page, then open this "
                   "page again.")
# On the remote address (home_origin.REMOTE) there is no /passkey page (it is
# the desktop's: routes/owner.py passkey_page), and a passkey for this address
# is added from the desktop's Remote access page, "Add a phone"
# (routes/remote_access.py, server/remote_join.py).
NO_FACTOR_REMOTE_WORDS = ("Your Host has no passkey for this address and no authenticator code set "
                          "up, so it cannot check that this is you here. On your Host's own "
                          "address, open Remote access and choose Add a phone for this device, "
                          "then open this page again.")
PASSKEY_WORDS = ("Approve with your passkey: choose your answer, then confirm on your device. This "
                 "page needs its script turned on to use it.")


class Factors(NamedTuple):
    """What this Host can check the person with on these pages: the code
    from their authenticator, and an owner passkey made for the address this
    page is on (owner_passkeys: a passkey works only on the address it was
    made on). `remote`: the page is on the remote address."""
    code: bool
    passkey: bool
    remote: bool = False


def no_factor_words(factors) -> str:
    """What the page says when the person has neither factor here."""
    return NO_FACTOR_REMOTE_WORDS if getattr(factors, "remote", False) else NO_FACTOR_WORDS


async def factors(pool, request) -> Factors:
    from server.network import nac_stepup
    code = await nac_stepup.is_enrolled(pool)
    remote = False
    try:
        from server import home_origin, owner_passkeys
        from server.middleware.owner_session import request_host
        origin = home_origin.desktop_origin(request_host(request.scope))
        remote = origin is not None and origin is home_origin.REMOTE
        passkey = origin is not None and await owner_passkeys.passkey_count(pool, origin) > 0
    except Exception:  # noqa: BLE001 — no passkey state: the code decides
        passkey = False
    return Factors(code=bool(code), passkey=bool(passkey), remote=bool(remote))


async def confirm(pool, request, code: str) -> tuple[bool, str]:
    """(confirmed, why not). A fresh passkey check, else the authenticator
    code, burned on success."""
    from server.network import nac_stepup
    typed = (code or "").strip()
    if await _fresh_passkey(request) and not typed:
        return True, ""
    enrolled = await nac_stepup.is_enrolled(pool)
    if not typed or not enrolled:
        # No code typed on a page that offers the passkey: the passkey is
        # what is missing, never a wrong code.
        have = await factors(pool, request)
        if have.passkey:
            return False, PASSKEY_WORDS
        if not enrolled:
            return False, no_factor_words(have)
    # A caller whose record is waiting gets StepupLocked from here, raised
    # as every code check raises it; the page says how long (locked_words).
    ok = await nac_stepup.verify_stepup(pool, (code or "").strip())
    if not ok:
        return False, "That code was not right, or it was already used. Try the next one."
    return True, ""
