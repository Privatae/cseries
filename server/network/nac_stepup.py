"""
Host-local step-up + policy signing for the NAC user-gate (P4).

Committing an ALLOW policy requires a USER step-up — a host-local
TOTP second factor the HOST owns (sovereign; no platform IdP). deny/tighten need only a
normal authenticated approval, no TOTP. The host also signs the approved policy terms
with its own entity key (entity_signing), which ARMS the PDP's fail-closed verify (P0):
a mutation after approval breaks the signature and the mesh fails closed.

Everything here is host-local: the TOTP secret and the signing key live in the pod's
kernel_secrets (alongside entity_signing + tokens) and never leave the container. A
compromised host owns its own enforcement regardless of signer — so the step-up's job is
to ensure a real USER (not a manipulated agent) authorized an allow.

LIMIT OF THAT GUARANTEE, and it is load-bearing. This gate does NOT hold
against the entity's own workload. The in-pod Postgres is initdb'd with --auth=trust
(entrypoint-postgres.sh) on a socket the agent can reach as uid 1000, so anything
running in the entity container can read `nac_stepup_totp` and compute the code with
the plain HMAC in _hotp below — and can read `entity_sign` and re-sign mutated terms,
so the tamper-evidence above does not hold against it either. It is NOT a boundary
between the agent and a WIDEN.

Where it DOES hold, stated only as far as it was measured (pod specs, 2026-09-09):
against a remote caller, and against the `hub` sidecar, which mounts no
`run-pgsock` and so cannot reach the socket at all. It does NOT hold against the
`postgres` container, which mounts `run-pgsock` and owns the data outright. Do not
widen this to "the other containers in the pod" without re-reading the mounts.

Moving the step-up factor and the policy signer out of workload reach is the open
half of that limit. The mechanism for it already exists — `box_confirmation` below runs
the approval on the user's own machine, where the factor is the bastion's, not
ours — it is simply not yet wired to the widen paths in nac_commit. Do not restate
the stronger claim here until that lands: a false invariant in a comment is how
this went unnoticed for so long.
"""
import base64
import contextlib
import contextvars
import functools
import hashlib
import hmac
import json
import os
import struct
import time
from typing import Optional

from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.applications.entity_signing import ensure_entity_signing_key
from server.applications.tokens import _SECRET_SCHEMA

_TOTP_SECRET_ID = "nac_stepup_totp"
_TOTP_ENROLLED_ID = "nac_stepup_enrolled"
_TOTP_DIGITS = 6
_TOTP_PERIOD = 30
_TOTP_DRIFT = 1  # accept +/- 1 window for clock skew


# ── TOTP (RFC 6238, HMAC-SHA1) — no external dependency ───────────────────

def _b32encode(raw: bytes) -> str:
    return base64.b32encode(raw).decode().rstrip("=")


def _b32decode(s: str) -> bytes:
    s = (s or "").strip().replace(" ", "").upper()
    pad = "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s + pad)


def _hotp(secret_raw: bytes, counter: int) -> str:
    h = hmac.new(secret_raw, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** _TOTP_DIGITS)
    return str(code).zfill(_TOTP_DIGITS)


def current_code(secret_b32: str, *, at: Optional[float] = None) -> str:
    """The current TOTP code — used by tests/tools, never by the verifier."""
    counter = int((at if at is not None else time.time()) // _TOTP_PERIOD)
    return _hotp(_b32decode(secret_b32), counter)


def matched_counter(secret_b32: str, code: str, *, at: Optional[float] = None) -> Optional[int]:
    """The TOTP counter (30s window) a code matches, allowing +/- _TOTP_DRIFT windows
    for skew — or None when it matches nothing. The counter is what burn-on-use
    records (RFC 6238 §5.2: a code accepted once must not be accepted again)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != _TOTP_DIGITS:
        return None
    try:
        raw = _b32decode(secret_b32)
    except Exception:
        return None
    now = int((at if at is not None else time.time()) // _TOTP_PERIOD)
    hit: Optional[int] = None
    for w in range(-_TOTP_DRIFT, _TOTP_DRIFT + 1):
        if hmac.compare_digest(_hotp(raw, now + w), code):
            hit = now + w  # don't early-return — keep the timing flat
    return hit


def verify_totp(secret_b32: str, code: str, *, at: Optional[float] = None) -> bool:
    """Verify a TOTP code against the secret, allowing +/- _TOTP_DRIFT windows for skew.
    Stateless — the burn-on-use lives in verify_stepup (it needs the pod's store)."""
    return matched_counter(secret_b32, code, at=at) is not None


# ── TOTP secret lifecycle (host-local, in kernel_secrets) ─────────────────

async def ensure_stepup_secret(pool) -> str:
    """Generate-or-fetch the host's base32 TOTP secret. Stored in kernel_secrets; never
    leaves the pod."""
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id=$1", _TOTP_SECRET_ID)
        if row is not None:
            return bytes(row["secret"]).decode()
        secret = _b32encode(os.urandom(20))
        await conn.execute(
            "INSERT INTO kernel_secrets (id, secret) VALUES ($1,$2) ON CONFLICT (id) DO NOTHING",
            _TOTP_SECRET_ID, secret.encode())
        row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id=$1", _TOTP_SECRET_ID)
        return bytes(row["secret"]).decode()


def provisioning_uri(secret_b32: str, *, account: str = "host", issuer: str = "Privatae NAC") -> str:
    """otpauth:// URI for the user to scan into an authenticator app."""
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(issuer + ':' + account)}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&digits={_TOTP_DIGITS}&period={_TOTP_PERIOD}")


async def is_enrolled(pool) -> bool:
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        return await conn.fetchrow("SELECT 1 FROM kernel_secrets WHERE id=$1", _TOTP_ENROLLED_ID) is not None


async def mark_enrolled(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        await conn.execute(
            "INSERT INTO kernel_secrets (id, secret) VALUES ($1,$2) ON CONFLICT (id) DO NOTHING",
            _TOTP_ENROLLED_ID, b"1")


_TOTP_LAST_COUNTER_ID = "nac_stepup_last_counter"


async def _consume_counter_in(conn, counter: int) -> bool:
    """consume_counter's body, inside the caller's transaction on `conn`."""
    # FOR UPDATE locks nothing when the row does not exist yet (the very
    # first approval ever); the advisory lock serializes that case too.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _TOTP_LAST_COUNTER_ID)
    row = await conn.fetchrow(
        "SELECT secret FROM kernel_secrets WHERE id=$1 FOR UPDATE", _TOTP_LAST_COUNTER_ID)
    last = -1
    if row is not None:
        try:
            last = int(bytes(row["secret"]).decode())
        except (ValueError, UnicodeDecodeError):
            last = -1
    if counter <= last:
        return False
    await conn.execute(
        "INSERT INTO kernel_secrets (id, secret) VALUES ($1,$2) "
        "ON CONFLICT (id) DO UPDATE SET secret = EXCLUDED.secret",
        _TOTP_LAST_COUNTER_ID, str(int(counter)).encode())
    return True


async def consume_counter(pool, counter: int) -> bool:
    """Burn-on-use (the host must never elevate or bypass the second factor). Accept
    `counter` only if it is newer than the last one accepted,
    and record it — so a code the user typed for ONE widen cannot be replayed by the
    host for a second widen inside the same window (the model relays the code as a
    tool argument; before this, nothing stopped a re-use for up to 90 seconds).
    Row-locked so two concurrent approvals cannot both win with one code."""
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        async with conn.transaction():
            return await _consume_counter_in(conn, counter)


# ── Attempt limit ──────────────────────────────────────────────────────────
# A six-digit code with a +/-1 window is right about 3 times in a million, so
# anything that can submit codes (a remote caller holding a session, a
# same-origin page) must not be able to submit many. NIST SP 800-63B 5.2.2
# requires limiting consecutive failed attempts (no more than 100); RFC 6238
# 5.2 asks for throttling. Here: STEPUP_FREE_FAILURES wrong codes in a row are
# free, then each further try must wait, from the last failure,
# STEPUP_LOCK_BASE_S doubling up to a ceiling: STEPUP_LOCK_MAX_S, or
# STEPUP_APP_LOCK_MAX_S on an app's own record (below). A try inside the wait
# is refused without looking at the code and is not counted, so one record
# gives a guesser at most 33 guesses a day (an app's own record 22). A right
# code clears that record.
#
# An app's own record and the one all apps share (below) also start again from
# zero once STEPUP_APP_QUIET_S has passed with no wrong code on them. The owner
# rarely types a code into such an app, so a right code seldom clears them, and
# without this a count that once reached the ceiling stayed there: a wrong
# code a month later still started the longest wait. It gives a guesser
# nothing: starting from zero again needs a whole quiet STEPUP_APP_QUIET_S
# first, so no 24 hours can hold more tries than one record's day. The owner's
# and the Host's records do not age; a right code clears them.
#
# Only a well-formed code is an attempt. A missing or malformed one (not six
# digits) is refused without being counted: it cannot be right, so counting it
# slows no guesser and lets any caller that omits the code hold the lock.
#
# Records. The Host's own tool calls and requests carrying the Host's kernel-op
# token count on nac_stepup_failures_host. The owner's own windows that ask
# for a code (OWNER_UI_APP_IDS, installed as the platform signed it) and
# callers with no app token count on nac_stepup_failures, the owner's. A
# request carrying any other app's token counts on two records at once: that
# app's own, nac_stepup_failures_app:<app id>, and one record every such app
# shares, nac_stepup_failures_apps. A try waits while either of them waits, a
# wrong code is counted on both, and a right code clears both. So all the
# other apps together get at most 33 guesses a day, however many app tokens a
# caller holds: a page that can mint the tokens of several apps (see below)
# used to get about 30 a day on each of them. Neither of those two records is
# the owner's or the Host's, so no app token can make the owner wait.
#
# What one app's wrong codes do to the other apps: the shared record is one
# budget, so whatever spends it makes the others wait, and one app alone
# could keep it waiting all day (at the ceiling, one wrong code an hour
# started each hour's wait as the last one ended). Now it cannot: its own
# record waits up to STEPUP_APP_LOCK_MAX_S, twice the shared record's
# ceiling, so once its own count is at that ceiling (its 12th wrong code in a
# row, about two hours of guessing as fast as it may) each of its wrong codes
# holds the shared record at most STEPUP_LOCK_MAX_S and then leaves the other
# apps at least that long before it may try again. Before that, during its
# first two hours, its waits and the shared ones are about the same length.
# Several apps together can still keep the shared record waiting, and spend
# its 33 guesses between them: keeping them apart would take a budget per
# app, which a caller holding many app tokens multiplies.
#
# What that does NOT cover, stated as far as it holds: a record is chosen by
# the token a request carries, and a page on the shell's own origin can carry
# the token of one of the owner's own windows. Every app framed same-origin is
# such a page: the first-party apps outside kernel.SDK_MANAGED_APPS and every
# Host-built (`custom`) app (proxy.is_isolated; app-runner.js frames them with
# allow-same-origin). Sending no token does not reach the owner's record:
# every route that takes a code requires an app token with a write: or
# resolve:decision scope (requires_permission), so a request with none gets a
# 401 before verify_stepup runs, and the shell's own token (SHELL_SCOPES,
# read-only) gets a 403. Nor does Settings' token: /launch gives a
# browser no isolated app's token, and Settings' own is minted on Settings'
# own origin and refused anywhere else (routes/applications.py launch_app and
# frame_token, applications/app_origin.py). What does reach it: the owner's
# windows still served on the desktop's origin (OWNER_UI_APP_IDS outside
# SDK_MANAGED_APPS: circles, workbench, network, keys, context-manager; Circles
# and Keys hold write:vault and write:network, Workbench write:vault, Network
# and Context Manager write:network). The page has the shell launch
# one (POST /api/applications/circles/launch with no token, or the shell's own
# launch function through window.parent) and sends the token that comes back,
# which counts as the owner's. That way it can keep the OWNER's record waiting
# (one wait at a time, each at most STEPUP_LOCK_MAX_S), and it adds up to 33
# guesses a day on that record to the apps' 33. /launch cannot tell such a
# page from the shell: its request is the shell's, from the shell's own window
# if it likes (routes/applications.py launch_app, C-2). Only serving those
# windows and Host-built apps from origins of their own would; proxy.is_isolated
# leaves Host-built apps out on purpose (per-origin storage and session
# cookie). The entity's own workload is not among the callers this can stop
# either: it can write these rows directly, and mint any app's token (see
# the module docstring).
_TOTP_FAILURES_ID = "nac_stepup_failures"
_TOTP_HOST_FAILURES_ID = "nac_stepup_failures_host"
_TOTP_APP_FAILURES_PREFIX = "nac_stepup_failures_app:"
_TOTP_APPS_FAILURES_ID = "nac_stepup_failures_apps"   # shared by every app of _TOTP_APP_FAILURES_PREFIX
STEPUP_FREE_FAILURES = 5
STEPUP_LOCK_BASE_S = 60
STEPUP_LOCK_MAX_S = 3600
STEPUP_APP_LOCK_MAX_S = 2 * STEPUP_LOCK_MAX_S   # an app's own record
STEPUP_APP_QUIET_S = 24 * 3600                  # an app's records restart after this with no wrong code

BY_OWNER, BY_HOST = "owner", "host"
_BY_APP = "app:"
_FAILURES_ID_BY = {BY_OWNER: _TOTP_FAILURES_ID, BY_HOST: _TOTP_HOST_FAILURES_ID}

# The system apps whose windows ask the owner for a code: Settings (backups
# and restores), Network (NAC widens, grants, the factor itself), Keys (vault
# reveal), Context Manager (store grants), and Circles and Workbench (ticket
# decisions). Their codes stay on the owner's record. The app id alone is not
# enough: a Host-built app could take one of these ids, so the installed row
# must also carry a provenance only the platform's key can sign
# (installer.install_bundle's provenance gate).
OWNER_UI_APP_IDS: frozenset[str] = frozenset(
    {"settings", "network", "keys", "context-manager", "circles", "workbench"})
_OWNER_UI_PROVENANCES = frozenset({"native", "standard"})

# Who is trying a code. Set to BY_HOST around every Host tool call
# (brain/mastercode/runtime/tools.py _execute_meta_tool, through
# counts_as_host) and around every request whose app token is the Host's
# kernel-op token, app id "host" (server/applications/middleware.py; minted
# only by kernel_ops.run_kernel_op, and reserved so no app can hold it:
# server/applications/manifest.py RESERVED_APP_IDS). Set to "app:<id>" around
# every request carrying any other app's token (the same middleware). With no
# app token, the owner's.
_ATTEMPTS_BY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nac_stepup_attempts_by", default=BY_OWNER)


def attempts_by() -> str:
    return _ATTEMPTS_BY.get()


@contextlib.contextmanager
def host_attempts():
    """Codes tried inside this block count on the Host's own record."""
    token = _ATTEMPTS_BY.set(BY_HOST)
    try:
        yield
    finally:
        _ATTEMPTS_BY.reset(token)


@contextlib.contextmanager
def app_attempts(app_id: str):
    """Codes tried inside this block were sent with `app_id`'s token: they count
    on that app's own record, or the owner's for the owner's own windows."""
    token = _ATTEMPTS_BY.set(_BY_APP + str(app_id))
    try:
        yield
    finally:
        _ATTEMPTS_BY.reset(token)


async def _owner_ui(pool, app_id: str) -> bool:
    """Is `app_id` one of the owner's own code-asking windows, as the platform
    signed it? Anything else, a failed read included, is not: its codes then
    count on a record of its own, which can never hold up the owner."""
    if app_id not in OWNER_UI_APP_IDS:
        return False
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT provenance FROM installed_applications WHERE id=$1", app_id)
    except Exception:
        return False
    return row is not None and row["provenance"] in _OWNER_UI_PROVENANCES


async def _records_of(pool, by: str) -> tuple[tuple[str, ...], str]:
    """(the failures rows a code counts on, whose they are: BY_OWNER, BY_HOST
    or "app:<id>") for the caller `by`. An app that is not one of the owner's
    windows counts on its own row and on the row all such apps share."""
    if by in _FAILURES_ID_BY:
        return (_FAILURES_ID_BY[by],), by
    if by.startswith(_BY_APP):
        app_id = by[len(_BY_APP):]
        if await _owner_ui(pool, app_id):
            return (_TOTP_FAILURES_ID,), BY_OWNER
        return (_TOTP_APP_FAILURES_PREFIX + app_id, _TOTP_APPS_FAILURES_ID), by
    return (_TOTP_FAILURES_ID,), BY_OWNER


def counts_as_host(fn):
    """Decorate an async function so every code it tries counts as the Host's."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with host_attempts():
            return await fn(*args, **kwargs)
    return wrapper


class StepupLocked(HTTPException):
    """A code was refused because this caller's record is waiting (or this
    wrong code started the wait). Raised, not returned as False, so every
    route that asks for a code answers the same 429 with the wait, and the
    Host's tools read the same sentence through their `detail`. `shared`: the
    wait is the one record every app shares, not the app's own."""

    def __init__(self, wait_s: int, by: str, shared: bool = False):
        self.wait_s = int(wait_s)
        self.by = by
        self.shared = bool(shared)
        minutes = max(1, (self.wait_s + 59) // 60)
        if by == BY_HOST:
            detail = (f"too many wrong codes came through the Host's own tools: they can try "
                      f"again in {minutes} minute(s). The owner's own codes are counted apart "
                      f"and are not held up by this.")
        elif by.startswith(_BY_APP) and shared:
            detail = (f"too many wrong codes came through apps on this Host (this one is "
                      f"'{by[len(_BY_APP):]}'): apps can try again in {minutes} minute(s). The "
                      f"owner's own codes are counted apart and are not held up by this.")
        elif by.startswith(_BY_APP):
            detail = (f"too many wrong codes came through the app '{by[len(_BY_APP):]}': it can "
                      f"try again in {minutes} minute(s). The owner's own codes are counted "
                      f"apart and are not held up by this.")
        else:
            detail = f"too many wrong codes: try again in {minutes} minute(s)"
        super().__init__(status_code=429, detail=detail,
                         headers={"Retry-After": str(self.wait_s)})


def _lock_seconds(failures: int, ceiling: int = STEPUP_LOCK_MAX_S) -> int:
    if failures < STEPUP_FREE_FAILURES:
        return 0
    # The exponent is capped: the ceiling is reached long before 32 doublings,
    # and an absurd count must not become a huge integer.
    steps = min(failures - STEPUP_FREE_FAILURES, 32)
    return min(STEPUP_LOCK_BASE_S * 2 ** steps, ceiling)


def _ceiling(failures_id: str) -> int:
    """The longest wait on this record: an app's own record waits up to twice
    what every other record does (the attempt limit says why)."""
    if failures_id.startswith(_TOTP_APP_FAILURES_PREFIX):
        return STEPUP_APP_LOCK_MAX_S
    return STEPUP_LOCK_MAX_S


def _ages(failures_id: str) -> bool:
    """Does this record start again from zero after STEPUP_APP_QUIET_S with
    no wrong code? The apps' records do; the owner's and the Host's do not."""
    return failures_id.startswith(_TOTP_APP_FAILURES_PREFIX) or failures_id == _TOTP_APPS_FAILURES_ID


def _wait_from(n: int, last: float, now: float, ceiling: int = STEPUP_LOCK_MAX_S) -> int:
    """Seconds still to wait on a record of `n` failures, the last at `last`.
    Never more than one lock: _failures_in never hands back a last failure
    later than now."""
    lock = _lock_seconds(n, ceiling)
    return max(0, min(lock, int(last + lock - now + 0.999)))


def _well_formed(code) -> bool:
    c = str(code or "").strip().replace(" ", "")
    return c.isdigit() and len(c) == _TOTP_DIGITS


def _parse_failures(raw) -> Optional[tuple[int, float]]:
    """(count, last failure) from a failures row, or None when it is not one."""
    try:
        v = json.loads(bytes(raw).decode())
        n, last = int(v.get("n") or 0), float(v.get("at") or 0.0)
    except (ValueError, TypeError, OverflowError, UnicodeDecodeError, AttributeError):
        return None
    if n < 0 or last != last or last in (float("inf"), float("-inf")):
        return None
    return n, last


async def _failures_in(conn, failures_id: str, now: float) -> tuple[int, float]:
    """The record's (count, last failure). A row that cannot be trusted is
    repaired, not believed and not ignored, and the repair is written back so
    it happens once:
      - unreadable: written back as the first wait starting now, so it holds
        the record for STEPUP_LOCK_BASE_S and then reads like any other (it
        used to be read as 'locked, from now' on every read, which never
        ended);
      - a last failure dated after now (a clock that stepped back, a row
        written by hand): written back as of now, with its count, so it holds
        the record for one wait from the first read that sees it. Capping
        each read at one wait from that read's own 'now' was not enough: the
        cap moved with every read, and a row dated two days ahead held the
        record for two days.
    An app's record whose last failure is STEPUP_APP_QUIET_S or more ago
    reads as empty (_ages); the next wrong code writes it over from one.
    Callers hold the record's advisory lock inside a transaction."""
    row = await conn.fetchrow("SELECT secret FROM kernel_secrets WHERE id=$1", failures_id)
    if row is None:
        return 0, 0.0
    got = _parse_failures(row["secret"])
    if got is not None and got[1] <= now:
        if _ages(failures_id) and now - got[1] >= STEPUP_APP_QUIET_S:
            return 0, 0.0
        return got
    n = STEPUP_FREE_FAILURES if got is None else got[0]
    await conn.execute(
        "INSERT INTO kernel_secrets (id, secret) VALUES ($1,$2) "
        "ON CONFLICT (id) DO UPDATE SET secret = EXCLUDED.secret",
        failures_id, json.dumps({"n": n, "at": now}).encode())
    return n, now


async def _lock_records(conn, fids) -> None:
    """The advisory locks of every record a code counts on, always in the same
    order, so two callers sharing a record cannot each hold one the other needs."""
    for fid in sorted(set(fids)):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", fid)


def _longest(waits: dict) -> tuple[int, str]:
    """(the longest wait, the record it is on) of {record: wait}; on a tie the
    caller's own record rather than the shared one."""
    fid = max(waits, key=lambda f: (waits[f], f != _TOTP_APPS_FAILURES_ID))
    return waits[fid], fid


async def stepup_wait_seconds(pool, *, at: Optional[float] = None, by: Optional[str] = None) -> int:
    """Seconds until a code may be tried again by `by` (default: the current
    caller, attempts_by()), the longest wait of the records it counts on; 0
    when it may be now."""
    if pool is None:
        return 0
    now = at if at is not None else time.time()
    fids, _ = await _records_of(pool, by or attempts_by())
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        async with conn.transaction():
            await _lock_records(conn, fids)
            waits = {}
            for fid in fids:
                n, last = await _failures_in(conn, fid, now)
                waits[fid] = _wait_from(n, last, now, _ceiling(fid))
    return _longest(waits)[0]


async def verify_stepup(pool, code: str, *, at: Optional[float] = None) -> bool:
    """Verify a step-up TOTP code against the host's ENROLLED secret and BURN it.
    Returns False if no factor is enrolled yet — so an allow cannot commit without an
    armed second factor — False for a missing or malformed code (not counted), and
    False for a wrong code or one already accepted once (replay), both counted on
    every record of the caller's. Raises StepupLocked (429) when any of those
    records is waiting, without looking at the code, and when this wrong code
    starts a wait."""
    if pool is None or not await is_enrolled(pool):
        return False
    if not _well_formed(code):
        return False
    fids, by = await _records_of(pool, attempts_by())
    secret = await ensure_stepup_secret(pool)
    now = at if at is not None else time.time()
    ok, wait, held = False, 0, fids[0]
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        async with conn.transaction():
            await _lock_records(conn, fids)
            counts = {}
            for fid in fids:
                counts[fid] = await _failures_in(conn, fid, now)
            wait, held = _longest({f: _wait_from(n, last, now, _ceiling(f))
                                   for f, (n, last) in counts.items()})
            if not wait:
                counter = matched_counter(secret, code, at=now)
                ok = counter is not None and await _consume_counter_in(conn, counter)
                if ok:
                    for fid in fids:
                        await conn.execute("DELETE FROM kernel_secrets WHERE id=$1", fid)
                else:
                    for fid, (n, _last) in counts.items():
                        await conn.execute(
                            "INSERT INTO kernel_secrets (id, secret) VALUES ($1,$2) "
                            "ON CONFLICT (id) DO UPDATE SET secret = EXCLUDED.secret",
                            fid, json.dumps({"n": n + 1, "at": now}).encode())
                    wait, held = _longest({f: _wait_from(n + 1, now, now, _ceiling(f))
                                           for f, (n, _last) in counts.items()})
    # Raised after the transaction, so the count this wrong code added is kept.
    if wait:
        raise StepupLocked(wait, by, shared=held == _TOTP_APPS_FAILURES_ID)
    return ok


# ── Second factor ON THE BOX (the host must never elevate or bypass the ────────
# second factor). This pod holds its own TOTP secret, so code running
# in the pod could compute a code — and a pod that merely forwarded a code the
# user typed could attach it to something else. So when the user enrolls a factor
# on their bastion (`cseries-bastion stepup-enroll`; the box reports `stepup: true`
# in its enforcing report), BOTH the secret and the confirmation live there:
#   pod → box: stepup_request <purpose> <details>   — no code travels
#   user, on the machine: `cseries-bastion approvals` shows the request in plain
#         words (from details the box verified against the purpose it will
#         enforce); `cseries-bastion approve` takes the code THERE
#   pod → box: stepup_status <request> → approved → the approval id goes into the
#         signed terms; the box enforces only terms carrying a matching approval
# The pod never sees the code and never chooses what the user confirms. A
# compromised pod can still sign anything — the box will not enforce it.
#
# The purpose strings are built identically in the gateway's stepup.go
# (purposeForGrant / purposeForPlan) — plain strings, never cross-language JSON.

_RESERVED_FIELD = ("|", "\t", "\n")
_RESERVED_ENTRY = ("|", ",", "\t", "\n")


def _purpose_check(fields, entries) -> None:
    """Refuse any component that would collide with the purpose's own delimiters —
    a pod could otherwise sign excludes [".env,.ssh"] under an approval the user
    read as two patterns. Mirrors the gateway's stepup.go purposeBad."""
    for f in fields:
        if any(c in str(f) for c in _RESERVED_FIELD):
            raise ValueError("a term contains a reserved character (|)")
    for e in entries:
        if any(c in str(e) for c in _RESERVED_ENTRY):
            raise ValueError("a list entry contains a reserved character (| or ,)")


def purpose_for_store_terms(terms: dict) -> str:
    mask = sorted(str(m) for m in (terms.get("mask") or []))
    excl = sorted(str(e) for e in (terms.get("excludes") or []))
    _purpose_check([terms.get("server") or "", terms.get("root") or "", terms.get("sweep") or ""],
                   mask + excl)
    sens = "true" if bool(terms.get("sensitive")) else "false"
    return (f"dcn_store_grant|{terms.get('server') or ''}|{terms.get('root') or ''}|"
            f"{','.join(mask)}|{','.join(excl)}|{terms.get('sweep') or ''}|{sens}|"
            f"ttl:{int(terms.get('ttl_days') or 0)}")


def store_request_details(terms: dict, *, display_name=None, note=None) -> dict:
    """The plain details the box shows the user for a store grant — exactly the
    fields the purpose is built from (the box recomputes it), plus labels."""
    return {"kind": "dcn_store_grant", "server": terms.get("server") or "",
            "root": terms.get("root") or "", "mask": list(terms.get("mask") or []),
            "excludes": list(terms.get("excludes") or []), "sweep": terms.get("sweep") or "",
            "sensitive": bool(terms.get("sensitive")), "ttl_days": int(terms.get("ttl_days") or 0),
            "display_name": display_name or "", "note": note or ""}


def plan_request_details(terms: dict, *, title=None) -> dict:
    return {"kind": "dcn_move_plan", "store_id": int(terms.get("store_id")),
            "plan_id": int(terms.get("plan_id")), "root": terms.get("root") or "",
            "archive": terms.get("archive") or "",
            "moves": [{"from": m.get("from"), "to": m.get("to")} for m in (terms.get("moves") or [])],
            "title": title or ""}


def purpose_for_plan_terms(terms: dict) -> str:
    _purpose_check([terms.get("root") or "", terms.get("archive") or ""], [])
    h = hashlib.sha256()
    for m in terms.get("moves") or []:
        if any(c in f"{m.get('from')}{m.get('to')}" for c in ("\t", "\n")):
            raise ValueError("a move path contains a reserved character")
        h.update(f"{m.get('from')}\t{m.get('to')}\n".encode())
    return (f"dcn_move_plan|{int(terms.get('store_id'))}|{int(terms.get('plan_id'))}|"
            f"{terms.get('root') or ''}|{terms.get('archive') or ''}|{h.hexdigest()}")


async def box_stepup_enrolled(pool, server: Optional[str]) -> bool:
    """Does the box named `server` hold its own second factor? Read from its last
    enforcing report (network_bastion_heartbeat.enforcing->>'stepup', v119)."""
    if not server or pool is None:
        return False
    try:
        v = await pool.fetchval(
            "SELECT enforcing->>'stepup' FROM network_bastion_heartbeat WHERE hostname=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", server)
    except Exception:
        return False
    return str(v).lower() == "true"


# The box's own sentence for an act the person refused at its console
# (client-go/stepup.go); the pod reads the person's refusal off it, and nothing else.
BOX_REFUSED_WORDS = "you refused this on this machine"


async def box_confirmation(pool, eid: int, *, server: Optional[str], purpose: Optional[str],
                           details: Optional[dict], existing: Optional[dict] = None,
                           report_delivered: bool = False) -> dict:
    """Drive the confirmation for something bound to a box.
    Returns one of:
      {"state": "not_enrolled"}                     — the box holds no factor; use the host's
      {"state": "pending", "request": {...}}        — filed (or still) waiting on the machine
      {"state": "approved", "approval": "<id>", "request": {...}}
      {"state": "denied"|"expired"|"offline", "detail": "..."}
      {"state": "delivered", "detail": "..."}       — only with report_delivered=True
    An enrolled box that is unreachable is 'offline' — never a fallback to the host's
    factor, which is exactly the bypass the box factor exists to close. `existing`
    is the row's stored request (box_request), polled before filing a new one.

    `report_delivered` (the signed-grant approve paths pass True): the box hands an
    approval out ONCE and answers a later status read with "delivered" (bastion stepup.go
    handleStepupStatus). By default that falls through to filing a fresh ask, as it
    always has — the management-op and session-lane callers keep that. For a grant it is
    wrong: "delivered" means a concurrent approval already took the approval, and a fresh
    ask would be an orphan on the machine — denied, the box then refuses that purpose for
    24h (blocking an identical re-draft); approved, the box hands the undelivered approval
    back to the next identical ask. So with True it is reported, and nothing is filed."""
    if not (server and purpose and details) or pool is None:
        return {"state": "not_enrolled"}
    if not await box_stepup_enrolled(pool, server):
        return {"state": "not_enrolled"}
    from server.network.nac_dcn import _dispatch_box
    if existing and existing.get("request") and existing.get("purpose") == purpose:
        res = await _dispatch_box(pool, eid, server, "stepup_status", [str(existing["request"])])
        if res.get("_offline"):
            return {"state": "offline",
                    "detail": f"'{server}' holds your second factor and is not connected right now"}
        try:
            st = json.loads(res.get("stdout") or "{}") if res.get("exit") == 0 else {}
        except json.JSONDecodeError:
            st = {}
        state = str(st.get("state") or "expired")
        if state == "approved" and st.get("approval"):
            return {"state": "approved", "approval": str(st["approval"]), "request": existing}
        if state == "pending":
            return {"state": "pending", "request": existing}
        if state == "denied":
            return {"state": "denied", "detail": f"you refused this on '{server}'", "by_person": True}
        if state == "delivered" and report_delivered:
            return {"state": "delivered",
                    "detail": f"'{server}' already handed out the approval for this request; "
                              f"approve again to ask afresh"}
        # expired / unknown (and "delivered" by default) → fall through and file a fresh request
    raw = base64.b64encode(json.dumps(details).encode()).decode()
    res = await _dispatch_box(pool, eid, server, "stepup_request", [purpose, raw])
    if res.get("_offline"):
        return {"state": "offline",
                "detail": f"'{server}' holds your second factor and is not connected right now"}
    if res.get("exit") != 0:
        # `by_person`: the box holds the person's refusal of this act at its
        # console (stepup.go: "you refused this on this machine; it cannot be
        # asked again for 24h") — its own words tell a refusal from a failure.
        words = (res.get("stderr") or "the machine refused the request").strip()
        return {"state": "denied", "detail": words, "by_person": words.startswith(BOX_REFUSED_WORDS)}
    try:
        out = json.loads(res.get("stdout") or "{}")
    except json.JSONDecodeError:
        out = {}
    rid = out.get("request")
    if not rid:
        return {"state": "denied", "detail": "the machine filed no request", "by_person": False}
    import datetime as _dt
    req = {"request": str(rid), "server": server, "purpose": purpose,
           "at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()}
    if str(out.get("state") or "") == "approved" and out.get("approval"):
        # The tap the pod missed: the box (0.33.0) hands an approved,
        # undelivered ask back on the file, once.
        return {"state": "approved", "approval": str(out["approval"]), "request": req}
    return {"state": "pending", "request": req}


def pending_on_box_message(server: str, request_id: str) -> str:
    return (f"waiting for your approval on {server}: on that machine run "
            f"`privatae-bastion approvals` to read it, then `privatae-bastion approve {request_id[:8]}` "
            "with a code from the authenticator enrolled there. Your host completes it on its "
            "next check-in, or click Approve again.")


# ── Policy signing (host entity key) — arms the PDP's tamper-detection ─────

# Must match NACController._canonical exactly (duplicated to avoid an import cycle).
def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


async def _entity_priv(pool, entity_id: int) -> Ed25519PrivateKey:
    pem = await ensure_entity_signing_key(pool, entity_id)
    return serialization.load_pem_private_key(pem, password=None)


async def sign_policy_terms(pool, entity_id: int, terms: dict) -> tuple[str, str]:
    """Sign approved policy terms with the host's own entity key (tamper-evidence).
    Returns (key_id, signature_b64); the PDP verifies with the matching public key."""
    key = await _entity_priv(pool, entity_id)
    return "entity_sign", base64.b64encode(key.sign(_canonical(terms))).decode()


async def entity_verify_pubkey(pool, entity_id: int) -> bytes:
    """The host entity public key (raw 32 bytes) the PDP arms its tamper-verify with."""
    key = await _entity_priv(pool, entity_id)
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
