"""The owner's login for a standalone Host.

A C-Series Host has one owner. On first boot there is no owner yet, so the Host
prints a one-time setup code to its log; whoever opens /setup must type that
code to create the owner account, which keeps a Host whose port was published
too early from being claimed by the first visitor.

After that the owner signs in with email and password and gets a session
cookie. A session ends after SESSION_IDLE of no use or SESSION_MAX in total,
whichever comes first.

A session is bound to the host name it was issued on. Each origin-isolated
app (apps-<key>-<handle><zone>) gets a session of its own through a one-time
join from the desktop (start_join / finish_join): a child of the desktop
session, valid only on that app's host, alive only while its parent is, and
ended with it. So no app's page ever holds a session another origin accepts.

Stored: the password as a scrypt hash, each session as the SHA-256 of its
cookie value (migrations/v228_owner_login.sql). Neither table is readable by
the AI's read lane (v139's exception list).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("cseries.owner_login")

COOKIE_PREFIX = "cseries_session"
SESSION_IDLE = timedelta(hours=1)
SESSION_MAX = timedelta(hours=24)
MIN_PASSWORD_LEN = 12

# scrypt at the OWASP Password Storage Cheat Sheet's floor: N=2^17, r=8, p=1.
# One hash uses 128 MiB for a fraction of a second, once per sign-in.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2 ** 17, 8, 1, 32
_SCRYPT_MAXMEM = 256 * 1024 * 1024

# A verified session is trusted for this long before the database is asked
# again, so loading a page with fifty assets is one query, not fifty.
_CACHE_S = 30.0
# How often a session's last_seen_at is written while it is in use.
_TOUCH_S = 60.0

_setup_code: Optional[str] = None
_owner_email: Optional[str] = None
_cookie_suffix: Optional[str] = None
# token hash -> (email, checked at, origin, parent hash or None)
_cache: dict[str, tuple[str, float, str, Optional[str]]] = {}
_touched: dict[str, float] = {}               # token hash -> last write
_failures: dict[str, tuple[int, float]] = {}  # client -> (count, locked until)
# Sessions signed out in this process -> when. A check that read the row just
# before the sign-out deleted it must not then cache it as live.
_signed_out: dict[str, float] = {}
_SIGNED_OUT_KEEP_S = 300.0
# One-time join codes: code hash -> (parent token hash, target host, use by).
# In memory: the server is one process (server/launch.py), and a code lives
# for seconds, across one redirect.
JOIN_TTL_S = 60.0
_JOIN_MAX = 256
_joins: dict[str, tuple[str, str, float]] = {}
# One password check at a time. The lockout is read before the hash and
# written after it, so concurrent sign-ins would all pass the check before the
# first failure was recorded; and each hash holds 128 MiB, so a burst of them
# could take the Host's memory.
_sign_in_lock = asyncio.Lock()


def _pool():
    from server import brain_service
    return brain_service.get_pool()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hash_password_sync(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                          r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN,
                          maxmem=_SCRYPT_MAXMEM)


async def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = await asyncio.to_thread(_hash_password_sync, password, salt)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


async def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        want = _unb64(digest)
        got = await asyncio.to_thread(
            lambda: hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt),
                                   n=int(n), r=int(r), p=int(p), dklen=len(want),
                                   maxmem=_SCRYPT_MAXMEM))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


def _same(a: str, b: str) -> bool:
    """Constant-time equality for any text. hmac.compare_digest raises
    TypeError on a str that is not ASCII, so compare the UTF-8 bytes."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ── boot ────────────────────────────────────────────────────────────────────

async def init() -> None:
    """Load the owner, or make and print a setup code when there is none.
    Called once from the server lifespan after the database pool exists."""
    global _owner_email, _setup_code, _cookie_suffix
    pool = _pool()
    if pool is None:
        logger.error("owner login: no database pool — nobody can sign in until restart")
        return
    row = await pool.fetchrow("SELECT email, cookie_suffix FROM owner_account WHERE id = 1")
    _owner_email = row["email"] if row else None
    _cookie_suffix = row["cookie_suffix"] if row else None
    await pool.execute("DELETE FROM owner_sessions WHERE expires_at < now()")
    if _owner_email:
        _setup_code = None
        logger.info("owner login: owner account present")
        return
    _setup_code = "-".join(secrets.token_hex(3) for _ in range(3))
    # WARNING so it shows at the default log level. This is the one place the
    # code is ever written; it changes on every boot until the owner exists.
    logger.warning("CSERIES SETUP CODE: %s — open /setup in a browser and enter it "
                   "to create the owner account", _setup_code)


def owner_email() -> Optional[str]:
    return _owner_email


def needs_setup() -> bool:
    return _owner_email is None


def cookie_name() -> str:
    """The session cookie's name for this Host. Cookies are shared by every
    port of a host name, so the name carries a suffix chosen when the owner was
    created: a second Host on the same machine uses a different cookie."""
    return f"{COOKIE_PREFIX}_{_cookie_suffix}" if _cookie_suffix else COOKIE_PREFIX


# ── setup and sign-in ───────────────────────────────────────────────────────

class LoginError(Exception):
    """A refusal whose message is safe to show the person."""


def _normal_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 254 or any(c.isspace() for c in email):
        raise LoginError("Enter a valid email address.")
    return email


def _check_new_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LEN:
        raise LoginError(f"Use a password of at least {MIN_PASSWORD_LEN} characters.")


async def create_owner(code: str, email: str, password: str) -> None:
    global _owner_email, _setup_code, _cookie_suffix
    if not needs_setup():
        raise LoginError("This Host already has an owner. Sign in instead.")
    if not _setup_code or not _same((code or "").strip().lower(), _setup_code):
        raise LoginError("That setup code is not right. It is printed in the Host's log.")
    email = _normal_email(email)
    _check_new_password(password)
    pw_hash = await hash_password(password)
    pool = _pool()
    suffix = secrets.token_hex(4)
    inserted = await pool.fetchval(
        "INSERT INTO owner_account (id, email, password_hash, cookie_suffix) "
        "VALUES (1, $1, $2, $3) ON CONFLICT (id) DO NOTHING RETURNING email",
        email, pw_hash, suffix)
    if not inserted:
        raise LoginError("This Host already has an owner. Sign in instead.")
    _owner_email, _setup_code, _cookie_suffix = email, None, suffix
    logger.warning("owner login: owner account created")


def _locked_for(client: str) -> float:
    count, until = _failures.get(client, (0, 0.0))
    return max(0.0, until - time.monotonic())


def _note_failure(client: str) -> None:
    count, _ = _failures.get(client, (0, 0.0))
    count += 1
    # Five tries free, then a wait that doubles per failure, capped at 15 min.
    wait = 0.0 if count < 5 else min(2 ** (count - 5), 900)
    _failures[client] = (count, time.monotonic() + wait)


async def sign_in(email: str, password: str, client: str, origin: str) -> str:
    """Check the owner's credentials and return a new session token, valid
    on host `origin` only."""
    pool = _pool()
    async with _sign_in_lock:
        wait = _locked_for(client)
        if wait > 0:
            raise LoginError(f"Too many wrong tries. Wait {int(wait) + 1} seconds.")
        row = await pool.fetchrow("SELECT email, password_hash FROM owner_account WHERE id = 1")
        email = (email or "").strip().lower()
        ok = bool(row) and _same(email, row["email"])
        # Always run the hash, so a wrong email takes as long as a wrong password.
        pw_ok = await check_password(password or "", row["password_hash"] if row else
                                     f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$AAAA$AAAA")
        if not (ok and pw_ok):
            _note_failure(client)
            raise LoginError("Wrong email or password.")
        _failures.pop(client, None)
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    await pool.execute(
        "INSERT INTO owner_sessions (token_hash, created_at, last_seen_at, expires_at, origin) "
        "VALUES ($1, $2, $2, $3, $4)", _token_hash(token), now, now + SESSION_MAX,
        (origin or "").lower())
    return token


async def sign_out(token: Optional[str]) -> None:
    if not token:
        return
    h = _token_hash(token)
    _cache.pop(h, None)
    _touched.pop(h, None)
    pool = _pool()
    if pool is None:
        return
    # The row's app-origin children go with it (ON DELETE CASCADE).
    status = await pool.execute("DELETE FROM owner_sessions WHERE token_hash = $1", h)
    if status and status != "DELETE 0":
        # A live session ended. A check that read its row before the delete
        # may still be running; remembering the hash stops it caching the
        # session as live (session_email looks before it caches). Only real
        # sessions are remembered, so a stream of made-up sign-outs adds
        # nothing here.
        now_m = time.monotonic()
        for old in [k for k, at in _signed_out.items() if now_m - at > _SIGNED_OUT_KEEP_S]:
            del _signed_out[old]
        _signed_out[h] = now_m
        _cache.pop(h, None)


def _gone(h: str, parent: Optional[str]) -> bool:
    return h in _signed_out or (parent is not None and parent in _signed_out)


async def session_email(token: Optional[str], host: str) -> Optional[str]:
    """The owner's email when `token` is a live session issued on `host`,
    else None. An app origin's session is live only while the desktop session
    it joined from is; using it counts as using that one."""
    if not token or len(token) > 200 or _owner_email is None:
        return None
    host = (host or "").lower()
    h = _token_hash(token)
    now_m = time.monotonic()
    hit = _cache.get(h)
    if hit and now_m - hit[1] < _CACHE_S:
        return hit[0] if hit[2] == host and not _gone(h, hit[3]) else None
    if h in _signed_out:
        return None
    pool = _pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        "SELECT s.last_seen_at, s.expires_at, s.origin, s.parent, "
        "       p.last_seen_at AS parent_seen, p.expires_at AS parent_expires "
        "  FROM owner_sessions s LEFT JOIN owner_sessions p ON p.token_hash = s.parent "
        " WHERE s.token_hash = $1", h)
    now = datetime.now(timezone.utc)
    if row is None:
        _cache.pop(h, None)
        return None
    parent = row["parent"]
    # Whose clock decides: the desktop session's, for a child.
    seen, ends = ((row["parent_seen"], row["parent_expires"]) if parent
                  else (row["last_seen_at"], row["expires_at"]))
    if seen is None or ends <= now or row["expires_at"] <= now or now - seen > SESSION_IDLE:
        _cache.pop(h, None)
        await pool.execute("DELETE FROM owner_sessions WHERE token_hash = $1", h)
        return None
    if row["origin"] != host:
        return None
    if now_m - _touched.get(h, 0.0) >= _TOUCH_S:
        await pool.execute("UPDATE owner_sessions SET last_seen_at = $2 "
                           "WHERE token_hash = $1 OR token_hash = $3", h, now, parent)
        _touched[h] = now_m
    if _gone(h, parent):   # signed out while this check was reading the row
        return None
    _cache[h] = (_owner_email, now_m, row["origin"], parent)
    return _owner_email


# ── app-origin sessions ─────────────────────────────────────────────────────

def start_join(token: str, target_host: str) -> str:
    """A one-time code that makes `target_host` a session joined to the
    desktop session `token`. The caller has checked that `token` is live on
    the desktop and that `target_host` is one of this Host's app origins."""
    now_m = time.monotonic()
    for k in [k for k, v in _joins.items() if v[2] <= now_m]:
        del _joins[k]
    while len(_joins) >= _JOIN_MAX:
        del _joins[next(iter(_joins))]
    code = secrets.token_urlsafe(24)
    _joins[_token_hash(code)] = (_token_hash(token), (target_host or "").lower(), now_m + JOIN_TTL_S)
    return code


async def finish_join(code: Optional[str], host: str) -> Optional[str]:
    """Redeem a join code on `host`: a new session token for that host, a
    child of the desktop session the code was made from, or None when the
    code is unknown, used, expired, made for another host, or its desktop
    session has ended."""
    if not code or len(code) > 200:
        return None
    entry = _joins.pop(_token_hash(code), None)
    host = (host or "").lower()
    if entry is None or entry[2] <= time.monotonic() or entry[1] != host:
        return None
    parent = entry[0]
    if parent in _signed_out:
        return None
    pool = _pool()
    row = await pool.fetchrow("SELECT last_seen_at, expires_at, parent FROM owner_sessions "
                              "WHERE token_hash = $1", parent)
    now = datetime.now(timezone.utc)
    if row is None or row["parent"] is not None or row["expires_at"] <= now \
            or now - row["last_seen_at"] > SESSION_IDLE:
        return None
    token = secrets.token_urlsafe(32)
    import asyncpg
    try:
        await pool.execute(
            "INSERT INTO owner_sessions (token_hash, created_at, last_seen_at, expires_at, origin, parent) "
            "VALUES ($1, $2, $2, $3, $4, $5)", _token_hash(token), now, row["expires_at"], host, parent)
    except asyncpg.ForeignKeyViolationError:
        return None    # the desktop session was signed out since it was read
    return token
