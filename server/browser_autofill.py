"""Autofill — the standing per-site grant, and the replay it authorizes.

The other half of "save these entries" (server/routes/browser_forms.py).
Capture seals what the user typed as `form:<host>:<key>`; this decides
whether an unattended run may use it, and performs the use.

## The grant

The grant is a STANDING PER-SITE one, chosen over per-run
approval and over a split that gated only passwords. The reasoning is
that per-run approval does not deliver automation — it delivers a faster
way to type, with the user present every time — and the guards that make
unattended use defensible already exist rather than needing to be
invented:

  - the broker re-reads the session's LIVE url and refuses if the page is
    not the host the credential was declared for (authz._do_browser_type);
  - the sidecar refuses to put a secret in anything that is not a real
    `input[type=password]`, judged from the DOM, not from an attribute
    the page chose;
  - every field typed writes its own vault_audit row naming the session,
    the ref and the field type — never the value;
  - Keys can deactivate any entry, and a deactivated credential reads as
    absent to every resolver, so the off-switch works without revoking.

A grant is therefore a small thing: it says "unattended runs may use the
entries I already saved for this host". It grants no new capability — the
values were already sealed and the broker could already type them; what
it withholds, until given, is doing that WITHOUT the user watching.

Deny by default. No grant, no replay, and the refusal names the host so
the caller can ask for one.

## The replay

One `use_credential(action="browser_type")` per field. Not a new bulk
broker action: each field then keeps its own audit row and its own
expect_host check, which is exactly what a bulk action would have had to
re-implement and could get wrong once for all fields at a time.

The CALLER still chooses which saved value goes into which element,
because only it can see the page. That is the same trust position
`type_secret` has always been in, and the same guard answers it: the
sidecar checks the DOM. What this module adds is that a value SAVED as a
secret is never typed into a non-password box — the caller cannot widen
that with allow_non_password, because this module decides that flag from
the stored cred_type rather than taking it from the request.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import asyncpg

from server.vault import api as vault_api
from server.vault import authz

log = logging.getLogger("privatae.browser_autofill")

# Mirrors server/routes/browser_forms.py. Imported there rather than
# redefined, so the capture and replay halves cannot drift on the name.
NAME_PREFIX = "form"

# cred_type tells replay whether a saved value is a password. Capture sets
# it; without the split, replay would have to ask the CALLER whether the
# target is a password field, and a caller that answered wrongly (or was
# talked into it by a hostile page) would get a saved password typed into
# a visible text box. The DOM check in the sidecar is the backstop; this
# is the thing that stops us asking it the wrong question.
CRED_TYPE_PLAIN = "form_entry"
CRED_TYPE_SECRET = "form_secret"

_HOST_RX = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")

# browser_form_grant is created by migrations/v141_band_b_app_and_autofill.sql,
# which runs before the server starts.


class AutofillError(Exception):
    """Refused. Carries no secret bytes — same contract as VaultAuthzError."""


# ── the grant ─────────────────────────────────────────────────────────
async def grant(pool: asyncpg.Pool, *, entity_id: int, host: str,
                actor: str) -> dict[str, Any]:
    """Allow unattended runs to use the entries saved for `host`.

    Idempotent: granting an already-granted host returns the live grant
    rather than stacking a second one, so a double-click cannot produce
    two rows that a single revoke would only half close.
    """
    host = (host or "").strip().lower()
    if not _HOST_RX.match(host):
        raise AutofillError(f"{host!r} is not a usable site name")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, granted_at, granted_by FROM browser_form_grant "
            "WHERE entity_id=$1 AND host=$2 AND revoked_at IS NULL",
            entity_id, host)
        if row is not None:
            return {"host": host, "granted": True, "already": True,
                    "granted_at": row["granted_at"].isoformat(),
                    "granted_by": row["granted_by"]}
        row = await conn.fetchrow(
            "INSERT INTO browser_form_grant (entity_id, host, granted_by) "
            "VALUES ($1, $2, $3) RETURNING id, granted_at",
            entity_id, host, actor)
    log.info("autofill grant: %s allowed for entity %s by %s", host, entity_id, actor)
    return {"host": host, "granted": True, "already": False,
            "granted_at": row["granted_at"].isoformat(), "granted_by": actor}


async def revoke(pool: asyncpg.Pool, *, entity_id: int, host: str,
                 actor: str) -> bool:
    """Withdraw the standing grant. The saved entries are untouched — the
    user can still use them by hand; what stops is unattended use."""
    host = (host or "").strip().lower()
    async with pool.acquire() as conn:
        n = await conn.execute(
            "UPDATE browser_form_grant SET revoked_at=now(), revoked_by=$3 "
            "WHERE entity_id=$1 AND host=$2 AND revoked_at IS NULL",
            entity_id, host, actor)
    revoked = n.endswith(" 1")
    if revoked:
        log.info("autofill grant REVOKED: %s for entity %s by %s",
                 host, entity_id, actor)
    return revoked


async def is_granted(pool: asyncpg.Pool, *, entity_id: int, host: str) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM browser_form_grant "
            "WHERE entity_id=$1 AND host=$2 AND revoked_at IS NULL",
            entity_id, (host or "").strip().lower())
    return row is not None


async def list_grants(pool: asyncpg.Pool, *, entity_id: int) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT host, granted_at, granted_by, last_used_at "
            "FROM browser_form_grant "
            "WHERE entity_id=$1 AND revoked_at IS NULL ORDER BY host",
            entity_id)
    return [{"host": r["host"],
             "granted_at": r["granted_at"].isoformat(),
             "granted_by": r["granted_by"],
             "last_used_at": (r["last_used_at"].isoformat()
                              if r["last_used_at"] else None)}
            for r in rows]


async def _touch(pool: asyncpg.Pool, *, entity_id: int, host: str) -> None:
    """Record that the grant was exercised. This is what makes an unused
    grant visible as unused — the thing a user needs in order to prune."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE browser_form_grant SET last_used_at=now() "
            "WHERE entity_id=$1 AND host=$2 AND revoked_at IS NULL",
            entity_id, host)


# ── what is saved for a site ──────────────────────────────────────────
def _split_name(name: str) -> Optional[tuple[str, str]]:
    parts = name.split(":", 2)
    if len(parts) != 3 or parts[0] != NAME_PREFIX:
        return None
    return parts[1], parts[2]


async def saved_for(pool: asyncpg.Pool, *, entity_id: int,
                    host: str) -> list[dict]:
    """Metadata of the entries saved for a host — never values.

    `secret` comes from the stored cred_type, which is what replay uses to
    decide where a value may be typed. Surfaced here so a caller can build
    its ref mapping knowing which keys are passwords, without being able
    to influence the decision.
    """
    host = (host or "").strip().lower()
    rows = await vault_api.list_secrets(pool, entity_id=entity_id)
    out = []
    for r in rows:
        if r.get("deactivated_at"):
            continue          # off is off — a deactivated entry is absent
        split = _split_name(r["logical_name"])
        if not split or split[0] != host:
            continue
        out.append({"key": split[1], "name": r["logical_name"],
                    "secret": r["cred_type"] == CRED_TYPE_SECRET})
    return sorted(out, key=lambda x: x["key"])


# ── the replay ────────────────────────────────────────────────────────
async def fill(pool: asyncpg.Pool, *, entity_id: int, host: str, session: str,
               mapping: list[dict], actor: str) -> dict[str, Any]:
    """Type the saved entries into a browser session, one field at a time.

    `mapping` is [{"ref": "e5", "key": "email"}, ...] — the caller matched
    the page's elements to saved keys from a snapshot, because only it can
    see the page.

    Refuses entirely without a live grant for `host`. Each field then goes
    through the vault broker, so each gets its own expect_host check
    against the session's LIVE url and its own audit row. A field that
    fails does not stop the others: a signup form half-filled and reported
    is more useful than an opaque abort, and the caller can see exactly
    which element refused.
    """
    host = (host or "").strip().lower()
    if not _HOST_RX.match(host):
        raise AutofillError(f"{host!r} is not a usable site name")
    if not await is_granted(pool, entity_id=entity_id, host=host):
        raise AutofillError(
            f"no standing grant for {host} — unattended autofill is refused. "
            f"The user grants it once, per site, and can revoke it in Keys.")
    if not mapping:
        raise AutofillError("nothing to fill: the ref -> key mapping is empty")

    known = {s["key"]: s for s in
             await saved_for(pool, entity_id=entity_id, host=host)}
    filled, skipped = [], []

    for item in mapping:
        ref = str((item or {}).get("ref") or "").strip()
        key = str((item or {}).get("key") or "").strip().lower()
        if not ref or not key:
            skipped.append({"ref": ref, "key": key,
                            "reason": "ref and key are both required"})
            continue
        saved = known.get(key)
        if saved is None:
            skipped.append({"ref": ref, "key": key,
                            "reason": f"nothing saved under {key!r} for {host}"})
            continue
        try:
            result = await authz.use_credential(
                pool, entity_id=entity_id, name=saved["name"],
                action="browser_type",
                request={
                    "session": session,
                    "ref": ref,
                    # The broker checks this against the page's LIVE url and
                    # refuses if the session drifted somewhere else.
                    "expect_host": host,
                    # Decided HERE from what was stored, never taken from the
                    # request: a value saved as a password may only ever go
                    # into a real password box. A caller (or a page that
                    # talked one into it) cannot widen this.
                    "allow_non_password": not saved["secret"],
                },
                actor=actor,
            )
        except Exception as e:
            # VaultAuthzError carries no secret bytes; anything else gets
            # reduced to its type for the same reason.
            detail = (str(e) if isinstance(e, authz.VaultAuthzError)
                      else type(e).__name__)
            skipped.append({"ref": ref, "key": key, "reason": detail})
            continue
        filled.append({"ref": ref, "key": key, "secret": saved["secret"],
                       "field_type": result.get("field_type")})

    # last_used_at is NOT recorded here. It moved into the broker guard
    # (authz._guard_form_entry), because this function was the only thing
    # touching it and a use through POST /api/vault/secrets/{name}/use left
    # the grant reading "never used" — which is the signal Keys offers for
    # pruning. One writer, on every path, rather than one writer on one path.
    log.info("autofill %s: %d filled, %d skipped (session %s)",
             host, len(filled), len(skipped), session)
    return {"host": host, "session": session,
            "filled": filled, "skipped": skipped}
