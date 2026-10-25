"""The owner's passkeys and recovery codes.

A passkey (WebAuthn, through the PyPI `webauthn` library) signs the owner in
with the device's own lock, a fingerprint, a face or a PIN, instead of a
password. Its relying party is this Host's desktop name exactly (HOME.host),
not the zone: the app origins are sibling names in the zone and run apps'
code, and a relying party of the whole zone would let any of them ask for
the owner's passkey. Every check asks the authenticator to verify the person
(user verification), because the passkey stands in for the password.

Once a passkey exists the password no longer signs in (owner_login.sign_in).
Ten recovery codes are made with the first passkey and shown once; each one
signs in once and is spent. Every attempt with a passkey or a code goes
through the password's own wrong-try limit and lock (owner_login
_sign_in_lock, _locked_for, _note_failure).

A step-up is a fresh check by the signed-in owner. Opening the Host to
another machine (server/routes/network.py, pairing a bastion through the
server's firewall) asks for a passkey check made in the last STEP_UP_S.
Signing in with a passkey counts as one. Adding another passkey, or making
new recovery codes, once a passkey exists, asks for a passkey check or a
sign-in with a recovery code in that time: those are what a person who lost
every passkey has to do, and a stolen session cookie alone does neither.
Adding the first passkey asks for a sign-in with the password in that time
(or, on a cloud machine, from its unlock page, with one of the disk's keys),
when its ceremony starts: the first passkey refuses the password from then
on and comes with the recovery codes, so a session cookie taken from the
owner's browser must not be able to add it. Once it is added, every other
session ends: a session made with a password someone else learned must not
outlive the passkey that was meant to shut them out.

Removing a passkey, and signing out every other session, ask for the same
fresh check as adding another (or, before there is a passkey, a password
sign-in in that time); removing one also ends every other session, since a
lost device may still hold one. The last passkey is never removed: without
one the password would sign in again, so the owner adds another first.

Each ceremony's challenge lives in this process (the server is one process,
server/launch.py) for CEREMONY_TTL_S, bound to its purpose, the host and, for
a signed-in owner, the session; it is used once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from typing import Optional

from server import owner_login as L
from server.backup_key import new_recovery_code, normalize_recovery_code
from server.home_origin import HOME

logger = logging.getLogger("cseries.owner_passkeys")

RP_NAME = "C-Series"
CEREMONY_TTL_S = 300.0
STEP_UP_S = 300.0
RECOVERY_CODES = 10
# One owner per Host, so one user handle; it names nobody.
USER_HANDLE = b"cseries-owner"
_RECOVERY_HASH_PREFIX = b"cseries-owner-recovery/v1\x00"
_TRANSPORTS = ("usb", "nfc", "ble", "smart-card", "internal", "cable", "hybrid")

PASSKEY = "passkey"
RECOVERY = "recovery"
# A session made with the password (setup or sign-in): counts only for
# starting the first passkey's ceremony, never as a passkey check.
PASSWORD = "password"
# A session made from a cloud machine's unlock page, with one of the disk's
# keys (owner_login.claim_owner, box_sign_in): counts where the password's
# does. That owner has no password.
BOX = "box"
SIGNED_IN = (PASSWORD, BOX)

REGISTER, LOGIN, STEP_UP = "register", "login", "step-up"

# ceremony id -> (purpose, challenge, host, holder, use by). The holder is the
# session's hash for a signed-in owner, else whose wrong tries count
# (owner_login._failure_key). At most _PER_HOLDER each, _CEREMONIES_MAX in all.
_ceremonies: dict[str, tuple[str, bytes, str, str, float]] = {}
_CEREMONIES_MAX = 1024
_PER_HOLDER = 3
# session hash -> (when, PASSKEY or RECOVERY)
_step_ups: dict[str, tuple[float, str]] = {}
_STEP_UPS_MAX = 256


class PasskeyError(L.LoginError):
    """A refusal whose message is safe to show the person."""


class StepUpNeeded(PasskeyError):
    """Refused until the owner makes a fresh check (fresh_step_up)."""


class PasswordAgain(PasskeyError):
    """Refused until the owner signs in with the password again: there is no
    passkey yet to check with."""


def _webauthn():
    # Imported when first used, so the rest of the server loads without it.
    import webauthn
    from webauthn.helpers import structs
    return webauthn, structs


def _check_host(host: str) -> None:
    if (host or "").lower() != HOME.host:
        raise PasskeyError(f"Passkeys are used on this Host's own address, {HOME.origin}.")


def _holder_of_session(token: str) -> str:
    return "s:" + L._token_hash(token)


def _holder_of_client(client: str) -> str:
    return "c:" + L._failure_key(client)


# ── ceremonies ──────────────────────────────────────────────────────────────

def _new_ceremony(purpose: str, host: str, holder: str, challenge: bytes) -> str:
    now = time.monotonic()
    for k in [k for k, v in _ceremonies.items() if v[4] <= now]:
        del _ceremonies[k]
    mine = [k for k, v in _ceremonies.items() if v[3] == holder]
    for k in mine[:max(0, len(mine) - _PER_HOLDER + 1)]:
        del _ceremonies[k]
    while len(_ceremonies) >= _CEREMONIES_MAX:
        del _ceremonies[next(iter(_ceremonies))]
    cid = secrets.token_urlsafe(18)
    _ceremonies[cid] = (purpose, challenge, host.lower(), holder, now + CEREMONY_TTL_S)
    return cid


def _take_ceremony(cid, purpose: str, host: str, holder: str) -> bytes:
    entry = _ceremonies.pop(cid, None) if isinstance(cid, str) and len(cid) <= 64 else None
    if entry is None or entry[0] != purpose or entry[2] != host.lower() or entry[3] != holder \
            or entry[4] <= time.monotonic():
        raise PasskeyError("That passkey request has expired. Start again.")
    return entry[1]


def _rp_options(opts) -> dict:
    webauthn, _ = _webauthn()
    return json.loads(webauthn.options_to_json(opts))


def _credential_id(credential) -> bytes:
    """The raw credential id a browser's answer names, or a refusal."""
    webauthn, _ = _webauthn()
    try:
        raw = webauthn.base64url_to_bytes(credential["rawId"])
    except Exception:
        raise PasskeyError("That answer is not a passkey's.")
    if not raw or len(raw) > 1023:
        raise PasskeyError("That answer is not a passkey's.")
    return raw


def _transports(names) -> list:
    _, structs = _webauthn()
    return [structs.AuthenticatorTransport(t) for t in (names or ()) if t in _TRANSPORTS]


# ── what exists ─────────────────────────────────────────────────────────────

async def passkey_count(pool) -> int:
    return int(await pool.fetchval("SELECT count(*) FROM owner_passkeys") or 0)


async def unused_recovery_codes(pool) -> int:
    return int(await pool.fetchval(
        "SELECT count(*) FROM owner_recovery_codes WHERE used_at IS NULL") or 0)


async def list_passkeys(pool) -> list[dict]:
    """Every passkey, oldest first: its id (base64url), label and dates."""
    webauthn, _ = _webauthn()
    rows = await pool.fetch("SELECT credential_id, label, created_at, last_used_at "
                            "FROM owner_passkeys ORDER BY created_at")
    return [{"id": webauthn.helpers.bytes_to_base64url(bytes(r["credential_id"])),
             "label": r["label"], "created_at": r["created_at"], "last_used_at": r["last_used_at"]}
            for r in rows]


async def _descriptors(pool) -> list:
    _, structs = _webauthn()
    rows = await pool.fetch("SELECT credential_id, transports FROM owner_passkeys ORDER BY created_at")
    return [structs.PublicKeyCredentialDescriptor(id=bytes(r["credential_id"]),
                                                  transports=_transports(r["transports"]))
            for r in rows]


# ── step-up ─────────────────────────────────────────────────────────────────

def _note_step_up(token: str, kind: str) -> None:
    now = time.monotonic()
    for k in [k for k, (at, _kind) in _step_ups.items() if now - at > STEP_UP_S]:
        del _step_ups[k]
    while len(_step_ups) >= _STEP_UPS_MAX:
        del _step_ups[next(iter(_step_ups))]
    _step_ups[L._token_hash(token)] = (now, kind)


def note_password_sign_in(token: str) -> None:
    """`token` is a session just made with the password (routes/owner.py,
    setup and sign-in): for STEP_UP_S it may start adding the first passkey."""
    _note_step_up(token, PASSWORD)


def note_box_sign_in(token: str) -> None:
    """`token` is a session just made from the machine's unlock page: for
    STEP_UP_S it may start adding the first passkey, as after a password."""
    _note_step_up(token, BOX)


def fresh_step_up(token: Optional[str], kinds=(PASSKEY,)) -> bool:
    """Whether the session `token` (or, for an app origin's session, the
    desktop session it joined from) made a check of one of `kinds` in the
    last STEP_UP_S."""
    if not token:
        return False
    h = L._token_hash(token)
    hashes = [h]
    cached = L._cache.get(h)
    if cached and cached[3]:
        hashes.append(cached[3])
    now = time.monotonic()
    for x in hashes:
        at_kind = _step_ups.get(x)
        if at_kind and now - at_kind[0] <= STEP_UP_S and at_kind[1] in kinds:
            return True
    return False


# ── recovery codes ──────────────────────────────────────────────────────────

def _code_hash(code: str) -> Optional[str]:
    norm = normalize_recovery_code(code if isinstance(code, str) else "")
    if len(norm) != 20:
        return None
    return hashlib.sha256(_RECOVERY_HASH_PREFIX + norm.encode("ascii", "ignore")).hexdigest()


async def new_recovery_codes(pool) -> list[str]:
    """RECOVERY_CODES fresh codes in place of every earlier one; the codes
    themselves, to be shown once. Only their hashes are kept."""
    codes = [new_recovery_code() for _ in range(RECOVERY_CODES)]
    await pool.execute(
        "WITH gone AS (DELETE FROM owner_recovery_codes) "
        "INSERT INTO owner_recovery_codes (code_hash) SELECT unnest($1::text[])",
        [_code_hash(c) for c in codes])
    return codes


async def recovery_sign_in(code: str, client: str, host: str) -> str:
    """Spend one recovery code and return a new session token for `host`."""
    _check_host(host)
    pool = L._pool()
    async with L._sign_in_lock:
        wait = L._locked_for(client)
        if wait > 0:
            raise PasskeyError(f"Too many wrong tries. Wait {int(wait) + 1} seconds.")
        if L.needs_setup():
            raise PasskeyError("This Host has no owner yet.")
        h = _code_hash(code)
        spent = h and await pool.fetchval(
            "UPDATE owner_recovery_codes SET used_at = now() "
            "WHERE code_hash = $1 AND used_at IS NULL RETURNING code_hash", h)
        if not spent:
            L._note_failure(client)
            raise PasskeyError("That recovery code is not right, or it was used already.")
        L._forget_failures(client)
    token = await L._new_session(pool, host)
    _note_step_up(token, RECOVERY)
    logger.warning("owner login: signed in with a recovery code")
    return token


# ── registration (a signed-in owner adds a passkey) ─────────────────────────

async def _fresh_check(pool, token: str, what: str) -> None:
    """Refused unless the session `token` made a fresh check: once a passkey
    exists, a passkey or a recovery code in the last STEP_UP_S; before that, a
    password sign-in in that time. `what` names the act for the refusal."""
    if await passkey_count(pool):
        if not fresh_step_up(token, (PASSKEY, RECOVERY)):
            raise StepUpNeeded(f"{what} needs a check with your passkey, or a recovery code, "
                               "in the last five minutes.")
    elif not fresh_step_up(token, SIGNED_IN):
        raise PasswordAgain(f"{what} needs a sign-in from the last five minutes. "
                            "Sign in again at /login, then come back here.")


async def end_other_sessions(token: str, host: str) -> int:
    """Sign out every session but this one (and the app origins it opened),
    after a fresh check; how many ended."""
    _check_host(host)
    await _fresh_check(L._pool(), token, "Signing out every other session")
    return await L.end_other_sessions(token)


async def remove_passkey(token: str, host: str, credential_id) -> int:
    """Take one passkey off this Host after a fresh check, and end every other
    session; how many sessions ended. The last passkey stays."""
    _check_host(host)
    webauthn, _ = _webauthn()
    pool = L._pool()
    await _fresh_check(pool, token, "Removing a passkey")
    try:
        raw = webauthn.base64url_to_bytes(credential_id) if isinstance(credential_id, str) else b""
    except Exception:
        raw = b""
    if not raw or len(raw) > 1023:
        raise PasskeyError("That passkey is not on this Host.")
    # One removal at a time, so two at once cannot both see a second passkey
    # and leave none (the server is one process).
    async with L._sign_in_lock:
        if await passkey_count(pool) <= 1:
            raise PasskeyError("This is the only passkey on this Host, and it stays: without a "
                               "passkey the password would sign in again. Add another passkey "
                               "first, then remove this one.")
        gone = await pool.fetchval(
            "DELETE FROM owner_passkeys WHERE credential_id = $1 RETURNING credential_id", raw)
    if not gone:
        raise PasskeyError("That passkey is not on this Host.")
    logger.warning("owner login: a passkey was removed")
    return await L.end_other_sessions(token)


async def _may_add(pool, token: str, starting: bool = False) -> None:
    """Whether the session `token` may add a passkey now. Once one exists:
    a passkey check or a recovery-code sign-in in the last STEP_UP_S. Before
    that, when the ceremony starts (`starting`): a password sign-in in that
    time; the challenge, used once within CEREMONY_TTL_S by this session
    alone, carries that check to the answer."""
    if await passkey_count(pool):
        if not fresh_step_up(token, (PASSKEY, RECOVERY)):
            raise StepUpNeeded("Adding another passkey needs a check with one you have, or a "
                               "recovery code, in the last five minutes.")
    elif starting and not fresh_step_up(token, SIGNED_IN):
        raise PasswordAgain("Adding your first passkey needs a sign-in from the last five "
                            "minutes. Sign in again at /login, then add it here.")


async def registration_options(token: str, host: str) -> dict:
    _check_host(host)
    webauthn, structs = _webauthn()
    pool = L._pool()
    await _may_add(pool, token, starting=True)
    email = L.owner_email() or "owner"
    opts = webauthn.generate_registration_options(
        rp_id=HOME.host, rp_name=RP_NAME, user_id=USER_HANDLE, user_name=email,
        user_display_name=email,
        attestation=structs.AttestationConveyancePreference.NONE,
        authenticator_selection=structs.AuthenticatorSelectionCriteria(
            resident_key=structs.ResidentKeyRequirement.REQUIRED,
            user_verification=structs.UserVerificationRequirement.REQUIRED),
        exclude_credentials=await _descriptors(pool))
    cid = _new_ceremony(REGISTER, host, _holder_of_session(token), opts.challenge)
    return {"ceremony": cid, "options": _rp_options(opts)}


def _label(raw) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    text = "".join(c for c in text if c.isprintable())[:64]
    return text or time.strftime("added %Y-%m-%d", time.gmtime())


async def register(token: str, host: str, ceremony, credential, label=None) -> dict:
    """Keep the passkey the browser just made. Returns {"recovery_codes": [...],
    "sessions_ended": n}: fresh codes when the owner has none left unused,
    else None there; and how many other sessions ended (only the first
    passkey ends them)."""
    _check_host(host)
    webauthn, _ = _webauthn()
    pool = L._pool()
    challenge = _take_ceremony(ceremony, REGISTER, host, _holder_of_session(token))
    await _may_add(pool, token)
    first = not await passkey_count(pool)
    if not isinstance(credential, dict):
        raise PasskeyError("That answer is not a passkey's.")
    try:
        got = webauthn.verify_registration_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=HOME.host,
            expected_origin=HOME.origin, require_user_verification=True)
    except Exception as exc:
        logger.info("owner passkey: registration refused: %s", exc)
        raise PasskeyError("The passkey could not be checked. Start again.")
    response = credential.get("response") if isinstance(credential.get("response"), dict) else {}
    transports = [t for t in (response.get("transports") or []) if t in _TRANSPORTS][:8]
    inserted = await pool.fetchval(
        "INSERT INTO owner_passkeys (credential_id, public_key, sign_count, transports, label) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (credential_id) DO NOTHING RETURNING credential_id",
        got.credential_id, got.credential_public_key, int(got.sign_count), transports, _label(label))
    if not inserted:
        raise PasskeyError("That passkey is already on this Host.")
    codes = await new_recovery_codes(pool) if not await unused_recovery_codes(pool) else None
    logger.warning("owner login: a passkey was added; the password no longer signs in")
    # The first passkey: every session the password made, but this one, ends.
    ended = await L.end_other_sessions(token) if first else 0
    return {"recovery_codes": codes, "sessions_ended": ended}


# ── assertions: sign-in and step-up ─────────────────────────────────────────

async def _options_for(purpose: str, host: str, holder: str, allow: list) -> dict:
    webauthn, structs = _webauthn()
    opts = webauthn.generate_authentication_options(
        rp_id=HOME.host, allow_credentials=allow,
        user_verification=structs.UserVerificationRequirement.REQUIRED)
    return {"ceremony": _new_ceremony(purpose, host, holder, opts.challenge),
            "options": _rp_options(opts)}


async def login_options(client: str, host: str) -> dict:
    """Options for signing in. No credential is named: the browser offers the
    passkeys it holds for this Host, so nobody learns which exist."""
    _check_host(host)
    if L.needs_setup():
        raise PasskeyError("This Host has no owner yet.")
    return await _options_for(LOGIN, host, _holder_of_client(client), [])


async def step_up_options(token: str, host: str) -> dict:
    _check_host(host)
    pool = L._pool()
    allow = await _descriptors(pool)
    if not allow:
        raise PasskeyError("There is no passkey on this Host yet.")
    return await _options_for(STEP_UP, host, _holder_of_session(token), allow)


async def _assert(pool, challenge: bytes, credential, client: str) -> None:
    """Check an assertion against the passkey it names and move that
    passkey's counter on. Call with owner_login._sign_in_lock held."""
    webauthn, _ = _webauthn()
    if not isinstance(credential, dict):
        L._note_failure(client)
        raise PasskeyError("That answer is not a passkey's.")
    try:
        raw = _credential_id(credential)
    except PasskeyError:
        L._note_failure(client)
        raise
    row = await pool.fetchrow(
        "SELECT public_key, sign_count FROM owner_passkeys WHERE credential_id = $1", raw)
    if row is None:
        L._note_failure(client)
        raise PasskeyError("That passkey is not one this Host knows.")
    try:
        got = webauthn.verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=HOME.host,
            expected_origin=HOME.origin, credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=int(row["sign_count"]), require_user_verification=True)
    except Exception as exc:
        logger.info("owner passkey: assertion refused: %s", exc)
        L._note_failure(client)
        raise PasskeyError("The passkey could not be checked.")
    await pool.execute(
        "UPDATE owner_passkeys SET sign_count = $2, last_used_at = now() WHERE credential_id = $1",
        raw, int(got.new_sign_count))
    L._forget_failures(client)


async def passkey_sign_in(ceremony, credential, client: str, host: str) -> str:
    """Check a passkey sign-in and return a new session token for `host`."""
    _check_host(host)
    pool = L._pool()
    async with L._sign_in_lock:
        wait = L._locked_for(client)
        if wait > 0:
            raise PasskeyError(f"Too many wrong tries. Wait {int(wait) + 1} seconds.")
        challenge = _take_ceremony(ceremony, LOGIN, host, _holder_of_client(client))
        await _assert(pool, challenge, credential, client)
    token = await L._new_session(pool, host)
    _note_step_up(token, PASSKEY)
    return token


async def step_up(token: str, ceremony, credential, client: str, host: str) -> None:
    """Check a signed-in owner's passkey now; fresh_step_up then holds for
    STEP_UP_S."""
    _check_host(host)
    pool = L._pool()
    async with L._sign_in_lock:
        wait = L._locked_for(client)
        if wait > 0:
            raise PasskeyError(f"Too many wrong tries. Wait {int(wait) + 1} seconds.")
        challenge = _take_ceremony(ceremony, STEP_UP, host, _holder_of_session(token))
        await _assert(pool, challenge, credential, client)
    _note_step_up(token, PASSKEY)


async def replace_recovery_codes(token: str, host: str) -> list[str]:
    """New codes for a signed-in owner, in place of every earlier one."""
    _check_host(host)
    pool = L._pool()
    if not await passkey_count(pool):
        raise PasskeyError("Recovery codes come with a passkey. Add a passkey first.")
    if not fresh_step_up(token, (PASSKEY, RECOVERY)):
        raise StepUpNeeded("New recovery codes need a check with your passkey, or a recovery "
                           "code, in the last five minutes.")
    return await new_recovery_codes(pool)
