"""The owner's login for a standalone Host.

A C-Series Host has one owner. On first boot there is no owner yet, so the Host
prints a one-time setup code to its log; whoever opens /setup must type that
code to create the owner account, which keeps a Host whose port was published
too early from being claimed by the first visitor.

After that the owner signs in with email and password and gets a session
cookie; once the owner adds a passkey, with the passkey or a recovery code
instead, and the password no longer signs in (server/owner_passkeys.py). A
session ends after SESSION_IDLE of no use or SESSION_MAX in total, whichever
comes first. On an https address the cookie is a __Host- cookie.

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
import ipaddress
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from server.home_origin import HOME

logger = logging.getLogger("cseries.owner_login")

COOKIE_PREFIX = "cseries_session"
# On an https address the cookie's name starts with __Host-: a browser keeps
# such a cookie only when it came Secure, for path / and with no Domain, so no
# other name under the same registrable domain (another Host, one of this
# Host's app origins, a network service) can set one this Host would read.
HOST_ONLY_PREFIX = "__Host-"
# The setup code's shape: three groups of six hex characters, 72 bits.
SETUP_CODE_RE = re.compile(r"[0-9a-f]{6}-[0-9a-f]{6}-[0-9a-f]{6}")
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
# who -> (wrong tries, locked until, last wrong try), oldest first. Bounded:
# an entry is forgotten _FAILURE_FORGET_S after its wait ends, and past
# _FAILURES_MAX the oldest go, so a stream of sources cannot grow it without
# end on a Host the internet reaches.
_failures: dict[str, tuple[int, float, float]] = {}
_FAILURES_MAX = 10_000
_FAILURE_FORGET_S = 3600.0
# Every wrong password on this Host, whoever sent it: (wrong tries, locked
# until, last wrong try). The record above bounds one source, and an
# attacker has many (one /48 is 65,536 /64s, five free tries each); a Host
# has one account, so the guessing that counts is the total. Past
# GLOBAL_FREE wrong passwords, each one doubles a wait that holds every
# password sign-in, capped at 15 minutes: about four guesses an hour from the
# whole internet, under NIST SP 800-63B 5.2.2's limit of 100 failed attempts
# on one account. Forgotten _FAILURE_FORGET_S after its wait ends, like the
# rest. Only the password counts here and only the password waits: a passkey
# or a recovery code signs in whatever this says, so a flood of wrong
# passwords never keeps the owner out once a passkey exists.
_password_failures: tuple[int, float, float] = (0, 0.0, 0.0)
GLOBAL_FREE = 20
_GLOBAL_WAIT_MAX = 900
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
    from_file = _setup_code_from_file()
    if from_file:
        # The code whoever installed this Host already holds; it stays the
        # same across restarts until the owner exists, and is not logged.
        _setup_code = from_file
        logger.warning("owner login: no owner yet; the setup code is the one in "
                       "CSERIES_SETUP_CODE_FILE — open /setup in a browser to create the owner account")
        return
    _setup_code = "-".join(secrets.token_hex(3) for _ in range(3))
    # WARNING so it shows at the default log level. This is the one place the
    # code is ever written; it changes on every boot until the owner exists.
    logger.warning("CSERIES SETUP CODE: %s — open /setup in a browser and enter it "
                   "to create the owner account", _setup_code)


def _setup_code_from_file() -> Optional[str]:
    """The setup code in CSERIES_SETUP_CODE_FILE, when that is set, readable
    and holds one code of the usual shape; else None, and a fresh code is
    made as without it. A Host installed for someone else gets its code this
    way, so the code handed over before the first boot is the one it takes."""
    path = os.environ.get("CSERIES_SETUP_CODE_FILE", "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read(256)
    except (OSError, UnicodeDecodeError) as exc:
        logger.error("owner login: CSERIES_SETUP_CODE_FILE %s cannot be read (%s); "
                     "a new setup code is made instead", path, exc)
        return None
    code = raw.strip().lower()
    if not SETUP_CODE_RE.fullmatch(code):
        logger.error("owner login: CSERIES_SETUP_CODE_FILE %s does not hold a setup code "
                     "(three groups of six hex characters); a new one is made instead", path)
        return None
    return code


def owner_email() -> Optional[str]:
    return _owner_email


def needs_setup() -> bool:
    return _owner_email is None


def secure_cookie() -> bool:
    """Whether the session cookie is Secure and __Host- named: CSERIES_URL is
    an https address. A plain-http Host (the local default) keeps its cookie
    as it was; a browser would drop a Secure cookie set over plain HTTP."""
    return HOME.scheme == "https"


def cookie_name() -> str:
    """The session cookie's name for this Host. Cookies are shared by every
    port of a host name, so the name carries a suffix chosen when the owner was
    created: a second Host on the same machine uses a different cookie."""
    name = f"{COOKIE_PREFIX}_{_cookie_suffix}" if _cookie_suffix else COOKIE_PREFIX
    return HOST_ONLY_PREFIX + name if secure_cookie() else name


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


async def create_owner(code: str, email: str, password: str, client: str = "") -> None:
    """Make the owner account with the setup code. A wrong code counts
    against `client` like a wrong password, so the code cannot be guessed at
    the rate requests arrive."""
    global _owner_email, _setup_code, _cookie_suffix
    async with _sign_in_lock:
        wait = _locked_for(client)
        if wait > 0:
            raise LoginError(f"Too many wrong tries. Wait {int(wait) + 1} seconds.")
        if not needs_setup():
            raise LoginError("This Host already has an owner. Sign in instead.")
        if not _setup_code or not _same((code or "").strip().lower(), _setup_code):
            _note_failure(client)
            raise LoginError("That setup code is not right. It is printed in the Host's log, "
                             "or was given to you with the Host.")
        _forget_failures(client)
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


def _failure_key(client: str) -> str:
    """Whose wrong tries these are: the address, or for IPv6 its /64, the
    block one home or one server is given, so stepping through the addresses
    of one's own block does not reset the count."""
    try:
        addr = ipaddress.ip_address(client)
    except ValueError:
        return client
    if addr.version == 6 and addr.ipv4_mapped:
        return str(addr.ipv4_mapped)
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return client


def _locked_for(client: str) -> float:
    _count, until, _last = _failures.get(_failure_key(client), (0, 0.0, 0.0))
    return max(0.0, until - time.monotonic())


def _note_failure(client: str) -> None:
    key = _failure_key(client)
    now_m = time.monotonic()
    count, until, last = _failures.pop(key, (0, 0.0, 0.0))
    if now_m - max(until, last) > _FAILURE_FORGET_S:
        count = 0           # forgotten, as the others below are
    for old in [k for k, (_c, until, last) in _failures.items()
                if now_m - max(until, last) > _FAILURE_FORGET_S]:
        del _failures[old]
    while len(_failures) >= _FAILURES_MAX:
        del _failures[next(iter(_failures))]
    count += 1
    # Five tries free, then a wait that doubles per failure, capped at 15 min.
    wait = 0.0 if count < 5 else min(2 ** (count - 5), 900)
    _failures[key] = (count, now_m + wait, now_m)


def _forget_failures(client: str) -> None:
    _failures.pop(_failure_key(client), None)


def _password_locked_for() -> float:
    _count, until, _last = _password_failures
    return max(0.0, until - time.monotonic())


def _note_password_failure() -> None:
    global _password_failures
    now_m = time.monotonic()
    count, until, last = _password_failures
    if now_m - max(until, last) > _FAILURE_FORGET_S:
        count = 0
    count += 1
    wait = 0.0 if count < GLOBAL_FREE else min(2 ** (count - GLOBAL_FREE), _GLOBAL_WAIT_MAX)
    _password_failures = (count, now_m + wait, now_m)


async def _new_session(pool, origin: str) -> str:
    """A new desktop session valid on host `origin` only; the token to set
    as its cookie. Every way of signing in ends here, so each issues the
    same session."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    await pool.execute(
        "INSERT INTO owner_sessions (token_hash, created_at, last_seen_at, expires_at, origin) "
        "VALUES ($1, $2, $2, $3, $4)", _token_hash(token), now, now + SESSION_MAX,
        (origin or "").lower())
    return token


async def sign_in(email: str, password: str, client: str, origin: str) -> str:
    """Check the owner's credentials and return a new session token, valid
    on host `origin` only."""
    pool = _pool()
    async with _sign_in_lock:
        wait = _locked_for(client)
        if wait > 0:
            raise LoginError(f"Too many wrong tries. Wait {int(wait) + 1} seconds.")
        # A passkey replaces the password (server/owner_passkeys.py).
        if await pool.fetchval("SELECT EXISTS (SELECT 1 FROM owner_passkeys)"):
            raise LoginError("This Host signs in with a passkey now. Use the passkey "
                             "button, or one of your recovery codes.")
        row = await pool.fetchrow("SELECT email, password_hash FROM owner_account WHERE id = 1")
        # Every visitor's wrong passwords together (_password_failures). Only
        # once there is an owner: before that no password can be right, and
        # setup signs the new owner in through here.
        wait = _password_locked_for() if row else 0.0
        if wait > 0:
            raise LoginError(f"Too many wrong passwords on this Host. Wait {int(wait) + 1} seconds.")
        email = (email or "").strip().lower()
        ok = bool(row) and _same(email, row["email"])
        # Always run the hash, so a wrong email takes as long as a wrong password.
        pw_ok = await check_password(password or "", row["password_hash"] if row else
                                     f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$AAAA$AAAA")
        if not (ok and pw_ok):
            _note_failure(client)
            if row:
                _note_password_failure()
            raise LoginError("Wrong email or password.")
        _forget_failures(client)
    return await _new_session(pool, origin)


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


async def end_other_sessions(keep: str) -> int:
    """End every session but the desktop session `keep` and the app-origin
    sessions joined from it; how many ended. An app-origin session joined
    from an ended one ends with it (ON DELETE CASCADE, and named here too, so
    each is remembered). Each ended session is remembered as signed out, so a
    check reading one right now does not cache it as live, and a join code
    made from one is dropped."""
    pool = _pool()
    keep_h = _token_hash(keep)
    row = await pool.fetchrow("SELECT parent FROM owner_sessions WHERE token_hash = $1", keep_h)
    if row is not None and row["parent"] is not None:
        # An app origin's session: ending "the others" would end its own
        # desktop session, and this one with it. (A `keep` that has already
        # ended keeps nothing: every session ends.)
        raise LoginError("Sign in on this Host's own address first.")
    rows = await pool.fetch(
        "DELETE FROM owner_sessions WHERE token_hash <> $1 AND parent IS DISTINCT FROM $1 "
        "RETURNING token_hash", keep_h)
    gone = {r["token_hash"] for r in rows}
    now_m = time.monotonic()
    for old in [k for k, at in _signed_out.items() if now_m - at > _SIGNED_OUT_KEEP_S]:
        del _signed_out[old]
    for h in gone:
        _signed_out[h] = now_m
        _touched.pop(h, None)
    for h in [h for h, v in _cache.items() if h in gone or (v[3] is not None and v[3] in gone)]:
        del _cache[h]
    for code in [c for c, v in _joins.items() if v[0] in gone]:
        del _joins[code]
    if gone:
        logger.warning("owner login: %d other session(s) signed out", len(gone))
    return len(gone)


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
