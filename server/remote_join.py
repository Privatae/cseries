"""Adding a phone: a one-time code that lets a phone make a passkey for the
Host's remote address.

On the machine, the signed-in owner chooses "Add a phone" (server/routes/
remote_access.py). That needs a fresh check with a passkey or a recovery code,
the same rule as adding a passkey (owner_passkeys._may_add), and a passkey on
the desktop's own address first: once any passkey exists the password no
longer signs in, so a phone's passkey alone would leave the owner nothing to
sign in with on the machine itself.

The page shows a QR code for https://<remote address>/remote/join#c=<code>.
The code is 32 random bytes, good for JOIN_TTL_S (10 minutes) and one use; the
Host keeps only its SHA-256. It sits after the "#", so it is in no server log
and no Referer.

The phone's page posts the code (start). The Host checks the hash, the expiry
and that it is unused, and spends it; the answer is a passkey ceremony for the
remote address's relying party, bound to that code and good for
owner_passkeys.CEREMONY_TTL_S. The phone makes the passkey, with user
verification, and posts it back (finish); the Host keeps it and starts a
session on the remote address. A wrong code counts as a wrong try, like a
wrong password.

The codes live in this process, like the other one-time codes (the server is
one process, server/launch.py): a restart forgets them, and the owner makes a
new one.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from server import home_origin
from server import owner_login as L
from server import owner_passkeys as P
from server.home_origin import HOME

JOIN_TTL_S = 600.0
JOIN = "phone-join"
_JOINS_MAX = 64
JOIN_PATH = "/remote/join"
# code hash -> (use by (monotonic), use by (wall clock), made by session hash)
_codes: dict[str, tuple[float, float, str]] = {}


class JoinError(P.PasskeyError):
    """A refusal whose message is safe to show the person."""


def _hash(code: str) -> str:
    return hashlib.sha256(b"cseries-remote-join/v1\x00" + code.encode("ascii", "ignore")).hexdigest()


def _forget_old(now: float) -> None:
    for k in [k for k, v in _codes.items() if v[0] <= now]:
        del _codes[k]
    while len(_codes) >= _JOINS_MAX:
        del _codes[next(iter(_codes))]


def remote() -> home_origin.HomeOrigin:
    if home_origin.REMOTE is None:
        raise JoinError("Remote access is not set up on this Host.")
    return home_origin.REMOTE


async def new_code(token: str, host: str) -> dict:
    """A join code for a phone, made by the signed-in owner's session `token`
    on the desktop's own address `host`. Returns {"url", "expires_at"}; the
    code is in the url's fragment and nowhere else."""
    origin = remote()
    if (host or "").lower() != HOME.host:
        raise JoinError(f"Add a phone from this Host's own address, {HOME.origin}.")
    pool = L._pool()
    if not await P.passkey_count(pool, HOME):
        raise JoinError("Add a passkey on this computer first, so you can still sign in here "
                        "without the phone.")
    if not P.fresh_step_up(token, (P.PASSKEY, P.RECOVERY)):
        raise P.StepUpNeeded("Adding a phone needs a check with your passkey, or a recovery "
                             "code, in the last five minutes.")
    now = time.monotonic()
    _forget_old(now)
    code = secrets.token_urlsafe(32)
    ends = time.time() + JOIN_TTL_S
    _codes[_hash(code)] = (now + JOIN_TTL_S, ends, L._token_hash(token))
    return {"url": f"{origin.origin}{JOIN_PATH}#c={code}", "expires_at": int(ends)}


async def start(code, client: str, host: str) -> dict:
    """Spend a join code on the remote address and return the passkey
    ceremony {"ceremony", "options"} the phone answers."""
    origin = remote()
    if (host or "").lower() != origin.host:
        raise JoinError(f"A phone joins on this Host's remote address, {origin.origin}.")
    pool = L._pool()
    async with L._sign_in_lock:
        wait = L._locked_for(client)
        if wait > 0:
            raise JoinError(f"Too many wrong tries. Wait {int(wait) + 1} seconds.")
        if L.needs_setup():
            raise JoinError("This Host has no owner yet.")
        h = _hash(code) if isinstance(code, str) and 0 < len(code) <= 200 else None
        now = time.monotonic()
        entry = _codes.pop(h, None) if h else None
        if entry is None or entry[0] <= now:
            L._note_failure(client)
            raise JoinError("That code is not right, or it was used already, or it is more than "
                            "ten minutes old. Make a new one on the computer the Host runs on.")
        L._forget_failures(client)
    return await P.creation_options(pool, origin, JOIN, host, "j:" + h)


async def finish(ceremony, code, credential, host: str, label=None) -> str:
    """Keep the phone's new passkey and return a session token for the
    remote address. `code` is the join code start spent: the ceremony is
    bound to it, so only the page that spent it can finish."""
    origin = remote()
    if (host or "").lower() != origin.host:
        raise JoinError(f"A phone joins on this Host's remote address, {origin.origin}.")
    if not isinstance(code, str) or not 0 < len(code) <= 200:
        raise JoinError("That passkey request has expired. Make a new code and start again.")
    pool = L._pool()
    challenge = P._take_ceremony(ceremony, JOIN, host, "j:" + _hash(code))
    await P.keep_new_passkey(pool, origin, challenge, credential, label or "phone")
    token = await L._new_session(pool, origin.host)
    P._note_step_up(token, P.PASSKEY)
    L.logger.warning("owner login: a phone was added for the remote address")
    return token
