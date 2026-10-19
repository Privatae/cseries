"""
Application tokens — HMAC-signed proof that a window belongs to a given
registered app with a given set of permissions.

Token shape:
    base64url(body_json) + "." + base64url(hmac_sha256(body_json))

Body fields:
    app_id      — registered app id (must be in installed_applications)
    permissions — list of permission strings (subset of app's manifest)
    window_id   — random per-window identifier
    iat         — issued-at unix seconds
    origin_key  — ONLY on an isolated app's window token: the key of the app
                  origin it was minted on (app_origin.py). Such a token is
                  accepted on that origin and nowhere else; a token without one
                  is refused on every app origin. Absent from every other token,
                  so their bodies are byte-identical to before.

Lifetime: tokens do NOT carry a time-based expiry. They are bearer
proofs whose validity ends when:
  - the kernel secret rotates (admin action)
  - the container restarts and a new secret is provisioned
  - (future) the container's idle-timer fires after 3600s of zero
    human + entity activity, terminating the workload entirely

The 5-minute exp this used to carry was security theater — it
interrupted long-running inference and stranded users mid-conversation
without buying real protection in a single-tenant per-entity container.
Idle policy is a CONTAINER concern, not an APP concern. Removed;
the verifier no longer checks any exp field.

The kernel secret lives in PG (`kernel_secrets`, one row, 256-bit
random). First mint generates + stores it. The TABLE is declared in
migrations/v147_band_c_kernel_vault_apps.sql and created at boot — this
module writes the row, never the table.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

import asyncpg


# Kept for backwards-compat with old mint() callers that still pass
# ttl_seconds=. The new verifier ignores exp entirely (see module docstring).
DEFAULT_TTL_SECONDS = 0


# THE REVOKE IS PART OF THE SCHEMA, NOT AN AFTERTHOUGHT, AND IT IS ALL THAT IS
# LEFT IN THIS STRING.
#
# The CREATE TABLE that used to open this block is gone.
# `kernel_secrets` is declared in migrations/v147_band_c_kernel_vault_apps.sql
# and created at boot, with the matching REVOKE beside it, because a table
# created inside the migration loop is created AFTER v139's ALTER DEFAULT
# PRIVILEGES has already armed on that same boot.
#
# The revoke stays HERE as well, and v139's own comment says why in one line:
# both halves are needed, neither is sufficient.
#   - v139 runs on every boot and revokes, guarded on to_regclass.
#   - v147 revokes on the single boot where it creates the table, which is the
#     one boot v139's guard finds nothing.
#   - this one converges an out-of-band re-grant without waiting for a reboot,
#     on the four paths that actually reach the key drawer.
#
# GUARDED ON to_regclass, which the CREATE beside it used to make unnecessary:
# `'public.kernel_secrets'::regclass` RAISES on a table that does not exist, and
# with the CREATE gone that state is reachable — a database restored from
# `pg_dump --no-privileges` of a host that predates v147, before the loop has
# run. Same shape and same reason as nac_agents.py `_DELEGATIONS_DDL`.
#
# WHAT IS IN HERE, which is why this is not hygiene. `secret` is plain BYTEA and
# it is not one key but the host's whole key drawer: the entity's Ed25519
# signing key (applications/entity_signing.py), `vault_host_x25519` — the X25519
# private key that opens every sealed credential the user has stored, provider
# API keys, harness sign-ins, site databases, SSH keys (vault/identity.py) — the
# app-token HMAC minted below, and the TOTP step-up seed, enrolled marker and
# counter (network/nac_stepup.py). Read access to that last one alone forges
# every step-up on the host. The whole point of splitting the recall role out is
# that a query reaching it through the model cannot do more than read memory.
#
# THE PRIVILEGE CHECK IS NOT AN OPTIMISATION — IT IS THE FIX FOR A REGRESSION
# THIS FILE SHIPPED. A REVOKE that changes nothing still rewrites the table's
# pg_class row: measured on PG 17.10, three consecutive no-op runs moved xmin
# 741 -> 742 -> 743. Two sessions overlapping on that raise
# `ERROR: tuple concurrently updated`, and this string is executed UNCACHED on
# hot paths — get_secret() below, called from _require_own_token
# (applications/proxy.py:114) on every app helper request, from every host tool
# call (kernel_ops), every 2FA verify/enroll (nac_stepup), and every vault open
# (vault/identity). Measured at 8x40 concurrency: 40/320 failures with the bare
# REVOKE, 0/320 without it. proxy.py's `except Exception` would have surfaced
# each one as a bogus 403 "invalid app token".
#
# THE GUARD TESTS THE ACL, NOT has_table_privilege(), AND THE DIFFERENCE IS THE
# WHOLE PROPERTY. The first cut of this guard asked
# has_table_privilege(...,'SELECT'), which is a question about EFFECTIVE table
# privilege, while the statement it guards is REVOKE ALL — table AND column.
# The two disagree in four states, measured on PG 17.10:
#   GRANT SELECT (secret) ON kernel_secrets  -> has_table_privilege false, and
#       host_recall reads `secret` anyway. The guard skipped; the bare REVOKE
#       would have closed it. The guard made the fix WEAKER than no guard.
#   GRANT UPDATE, DELETE, TRUNCATE           -> same miss, and the recall lane
#       can then destroy the key drawer rather than merely read it.
#   GRANT entity TO host_recall (membership) -> has_table_privilege TRUE, but a
#       REVOKE ... FROM host_recall cannot remove a membership-derived
#       privilege, so it fired on every call and never converged: the original
#       catalog-churn failure back, in the exact drift state v139 documents.
#   GRANT SELECT ... TO PUBLIC               -> same.
#
# aclexplode over relacl + attacl asks the only question that matches the
# statement: is there a DIRECT ACL entry for this role that a REVOKE can
# actually remove. Verified in all six states — steady f, direct t, column-only
# t, non-SELECT-only t, membership-only f, PUBLIC-only f.
#
# NESTED IF, and read the two levels separately — the outer one is now a
# conjunction and that is deliberate. `::regrole` and `::regclass` both RAISE on
# an object that does not exist, and SQL does not guarantee left-to-right
# short-circuit, so no test that CASTS may share a conjunction with the test
# that makes the cast safe. The OUTER condition is a conjunction because neither
# half of it can raise: to_regclass() returns NULL for a missing table and the
# pg_roles lookup is an ordinary SELECT. The INNER IF is where the casts live,
# and it is reached only once both of those have passed. Same shape as
# nac_agents.py `_DELEGATIONS_DDL`. Do not flatten the inner one into the
# outer.
#
# RESIDUAL, stated rather than implied: this removes the STEADY-STATE churn, not
# a transient. In the window right after an out-of-band re-grant, concurrent
# callers can still collide once before the first one converges (measured: 14/40
# over five re-grant bursts). v139 does not open that window — its GRANT ON ALL
# TABLES and this exception REVOKE are inside one transaction, so the granted
# state is never committed on its own.
_SECRET_SCHEMA = """
DO $$ BEGIN
    IF to_regclass('public.kernel_secrets') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_recall') THEN
        IF EXISTS (SELECT 1 FROM pg_class c, aclexplode(c.relacl) a
                    WHERE c.oid = 'public.kernel_secrets'::regclass
                      AND a.grantee = 'host_recall'::regrole)
           OR EXISTS (SELECT 1 FROM pg_attribute at, aclexplode(at.attacl) a
                       WHERE at.attrelid = 'public.kernel_secrets'::regclass
                         AND at.attnum > 0 AND NOT at.attisdropped
                         AND a.grantee = 'host_recall'::regrole) THEN
            REVOKE ALL ON public.kernel_secrets FROM host_recall CASCADE;
        END IF;
    END IF;
END $$;
"""


class TokenError(ValueError):
    """Raised when a token is malformed, forged, or expired."""


@dataclass(frozen=True)
class AppToken:
    app_id: str
    permissions: list[str]
    window_id: str
    iat: int
    # exp is retained for body-format stability but no longer enforced by
    # verify(). Old tokens with non-zero exp still parse; new tokens write 0.
    exp: int = 0
    # "" for every token not minted on an app origin (see module docstring).
    origin_key: str = ""


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


async def get_secret(pool: asyncpg.Pool) -> bytes:
    async with pool.acquire() as conn:
        await conn.execute(_SECRET_SCHEMA)
        row = await conn.fetchrow(
            "SELECT secret FROM kernel_secrets WHERE id = 'app_token'"
        )
        if row is not None:
            return bytes(row["secret"])

        secret = secrets.token_bytes(32)
        await conn.execute(
            "INSERT INTO kernel_secrets (id, secret) VALUES ('app_token', $1) "
            "ON CONFLICT (id) DO NOTHING",
            secret,
        )
        row = await conn.fetchrow(
            "SELECT secret FROM kernel_secrets WHERE id = 'app_token'"
        )
        return bytes(row["secret"])


def mint(
    secret: bytes,
    *,
    app_id: str,
    permissions: list[str],
    window_id: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,  # ignored — kept for callsite compat
    origin_key: str | None = None,
) -> tuple[str, AppToken]:
    # Every token is minted here, so this is where a retired permission stops:
    # an installed app's registry row written by an older kernel can still hold
    # one (scopes.RETIRED_PERMISSIONS), and launch mints from that row.
    from server.applications.scopes import RETIRED_PERMISSIONS
    now = int(time.time())
    body = {
        "app_id": app_id,
        "permissions": [p for p in permissions if p not in RETIRED_PERMISSIONS],
        "window_id": window_id or secrets.token_urlsafe(12),
        "iat": now,
        # Stays in the body for backwards-compat parsing. Verifier no longer
        # enforces it — see module docstring on lifetime model.
        "exp": 0,
    }
    if origin_key:
        body["origin_key"] = str(origin_key)
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret, body_json, hashlib.sha256).digest()
    token = f"{_b64u(body_json)}.{_b64u(sig)}"
    return token, AppToken(
        app_id=body["app_id"],
        permissions=body["permissions"],
        window_id=body["window_id"],
        iat=body["iat"],
        exp=body["exp"],
        origin_key=body.get("origin_key", ""),
    )


def verify(secret: bytes, token: str) -> AppToken:
    try:
        body_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        raise TokenError("malformed token") from None

    try:
        body_json = _b64u_decode(body_b64)
        sig = _b64u_decode(sig_b64)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        raise TokenError("malformed token") from None

    expected_sig = hmac.new(secret, body_json, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        raise TokenError("bad signature")

    try:
        body = json.loads(body_json)
    except json.JSONDecodeError:
        raise TokenError("malformed body") from None

    # NOTE: no expiry check — per-app token TTL is removed. Tokens
    # live until kernel-secret rotation or container restart. Container-level
    # idle policy is a separate (future) concern; inference must never be
    # interrupted by a token timer.

    try:
        return AppToken(
            app_id=str(body["app_id"]),
            permissions=list(body["permissions"]),
            window_id=str(body["window_id"]),
            iat=int(body["iat"]),
            exp=int(body.get("exp", 0)),
            origin_key=str(body.get("origin_key") or ""),
        )
    except (KeyError, TypeError, ValueError):
        raise TokenError("missing fields") from None
