"""
Backup status — read-only surface over the local-master backup tables.

The bastion daemon (user side) is the trust anchor for the user's
encrypted backups. It writes wrapped DEKs to `local_backup_envelope` and
per-row ciphertext to `local_backup_blob` in this container's PG, syncing
state in. The container never initiates a backup, never decrypts, never
holds keys.

GET /api/backup/local reports that state. Until the bastion daemon writes
anything, the response reads `{"configured": false}` and the dashboard renders
an empty state.

Trust direction is one-way: container → no introspection of the bastion.

GET/PUT /api/backup/settings are a DIFFERENT thing
and must not be confused with the above. They are the user's own schedule and
optional second destination for the PLATFORM-side backup — the one the fleet
job on the management plane runs. `/local` is the bastion's local-master
backup, which the user's own machine drives and the container cannot decrypt.
One surface, two independent copies, and the Settings app shows both.

POST /api/backup/key/* are the keys the Host's backups are encrypted to
(server/backup_key.py): register or retire a bastion's key, and adopt a new
recovery key from the account page. Every one of them is WALLED from the host
(brain/mastercode/runtime/kernel_ops.py `_WALLED_PATHS`): they decide who can
read the user's data, so they are user-only in the same way a credential reveal
is. The schedule/target half above stays kernel_op-exposed. No route here ever
accepts a private key (_no_private_keys).
"""

from __future__ import annotations

import json
from typing import Optional
import logging
import re

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from server import backup_key, backup_ship, brain_service
from server.applications.middleware import requires_permission
from server.network import nac_stepup
from server.vault import identity as vault_identity

logger = logging.getLogger("privatae.pc.backup")

def _key_in_json(body: bytes) -> bool:
    """A private key written with JSON escapes (\\u0041GE-...) reads
    differently in the raw body; check every string the body parses to, at
    any depth, keys included."""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    stack = [doc]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if "AGE-SECRET-KEY-" in v.upper():
                return True
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return False


async def _no_private_keys(request: Request) -> None:
    """THE RULE: the platform holds a backup OR a key,
    never both. No private age key is ever sent to this Host: a request body
    that carries one is refused before any route reads it, whatever the route
    and whoever sent it. (A restore sends the backup's key RESEALED to this
    Host, which is age ciphertext, never an identity.)"""
    body = await request.body()
    if body and (b"AGE-SECRET-KEY-" in body.upper() or _key_in_json(body)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "that request carries a private key. Keep your recovery key to "
                            "yourself: this Host never needs it, only what your browser or "
                            "bastion makes with it")


router = APIRouter(prefix="/api/backup", tags=["backup"],
                   dependencies=[Depends(_no_private_keys)])


@router.get("/local", dependencies=[Depends(requires_permission("read:backup_list"))])
async def local_backup_status() -> dict:
    pool = brain_service.get_pool()
    if not pool:
        return {"configured": False, "reason": "brain_not_initialized"}

    async with pool.acquire() as conn:
        try:
            envelope_rows = await conn.fetch(
                """
                SELECT kdf_source, kdf_algo, kdf_params, created_at, is_active
                  FROM local_backup_envelope
                 WHERE is_active = TRUE
                """
            )
        except asyncpg.UndefinedTableError:
            return {"configured": False, "reason": "schema_not_initialized"}

        blob_stats = await conn.fetchrow(
            """
            SELECT COUNT(*)::BIGINT                          AS blob_count,
                   COALESCE(SUM(OCTET_LENGTH(ciphertext)), 0)::BIGINT AS blob_bytes,
                   MAX(created_at)                            AS last_sync_at
              FROM local_backup_blob
             WHERE superseded_at IS NULL
            """
        )

        by_kind_rows = await conn.fetch(
            """
            SELECT resource_kind,
                   COUNT(*)::BIGINT       AS count,
                   MAX(created_at)        AS last_synced
              FROM local_backup_blob
             WHERE superseded_at IS NULL
             GROUP BY resource_kind
             ORDER BY resource_kind
            """
        )

    envelopes = {"passphrase": None, "recovery_code": None}
    for r in envelope_rows:
        envelopes[r["kdf_source"]] = {
            "active": r["is_active"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "kdf_algo": r["kdf_algo"],
            "kdf_params": r["kdf_params"],
        }

    configured = envelopes["passphrase"] is not None

    return {
        "configured": configured,
        "envelopes": envelopes,
        "blob_count": int(blob_stats["blob_count"] or 0),
        "blob_bytes": int(blob_stats["blob_bytes"] or 0),
        "last_sync_at": blob_stats["last_sync_at"].isoformat() if blob_stats["last_sync_at"] else None,
        "by_kind": [
            {
                "resource_kind": r["resource_kind"],
                "count": int(r["count"]),
                "last_synced": r["last_synced"].isoformat() if r["last_synced"] else None,
            }
            for r in by_kind_rows
        ],
    }


# ── The user's own backup schedule and destination ───────────────────────────
# Each user's backup is THEIRS: their schedule, their key, their
# target, their mirror. This is the schedule and target half; the Host's own
# backup loop (server/host_backup.py) reads these values out of this same row.
#
# Two things this deliberately does NOT do:
#   * It never holds a credential. A user-designated target NAMES one that
#     lives in the Keys app / vault (`cred_name`), the same shape propose_store
#     uses for context stores. A secret here would be a secret in every pg_dump.
#   * It never describes custody the Host does not have: who can open a backup
#     is the list of registered keys (_key_block), read from the same rows the
#     backup encrypts to.
#
# Times are stored in the USER'S timezone (behavioral_params.timezone), not
# UTC: "3am" has to mean their 3am or the control is a lie. The timezone is
# already stored right beside this by the settings app, for the same reason.

FREQUENCIES = ("daily", "weekly", "off")
# `nfs` and `smb` are gone. There was no NFS or SMB client in
# the entity image and no routed path to a user's LAN to use one over — the
# bastion lane is a CONNECT relay, not a route — so they could only ever have
# been stored and never used. "My own machine" replaces both: the artifact goes
# to the user's bastion, which writes it wherever they point it, including a NAS
# mount. The definition lives in server/backup_ship.py so the route, the ship
# worker and the tests cannot drift apart.
TARGET_KINDS = backup_ship.TARGET_KINDS

# What an entity gets when it has never touched this surface: the fleet
# default that has always applied. Written out rather than left implicit so
# the UI can show the real answer instead of an empty form.
DEFAULTS = {
    "frequency": "daily",
    "hour": 2,
    "minute": 30,
    "weekday": 0,          # Monday; only meaningful when frequency == weekly
    "target": None,
}


async def _load_bp(eid: int) -> dict:
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid)
    bp = row["behavioral_params"] if row and row["behavioral_params"] else {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = {}
    return bp or {}


def _readable_target(stored_target):
    """A saved target as the READ path should present it.

    A destination saved by an older build can name a kind that no longer exists
    (nfs / smb, now removed). Validation belongs on the WRITE path; on
    read, an unknown kind must degrade to "no destination, and here is why"
    rather than raising out of GET /settings and taking the whole Backup tab
    down with a 500 — the one screen where the user could fix it."""
    if not stored_target:
        return None, None
    try:
        return backup_ship.validate_target(stored_target), None
    except ValueError as e:
        kind = (stored_target or {}).get("kind")
        return None, (
            f"the saved destination ({kind}) is no longer supported — "
            "choose a new one below" if kind in ("nfs", "smb")
            else f"the saved destination cannot be used: {e}")


def _effective(bp: dict) -> dict:
    """Stored settings merged over the fleet defaults, plus the timezone the
    times are expressed in. Never returns a partial record — the caller
    rendering a form should not have to know which keys happen to be set."""
    stored = bp.get("backup") or {}
    if not isinstance(stored, dict):
        stored = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in stored:
            out[k] = stored[k]
    out["timezone"] = bp.get("timezone") or None
    # "Has the user set a SCHEDULE?" — keyed off the schedule fields, not off
    # the presence of the object: the storage choice lives in the same object,
    # and saving it must not move the Host off the fleet's 02:00-UTC default
    # onto 02:00 in its own timezone.
    out["is_default"] = not any(k in stored for k in DEFAULTS)
    # Keep backups in platform storage (ciphertext only). Not in DEFAULTS: a
    # storage choice is not a schedule, and must not move when the backup runs.
    out["platform"] = bool(stored.get("platform", True))
    out["target"], out["target_problem"] = _readable_target(out.get("target"))
    return out


def _user_identity() -> str:
    """The identity this Host's backup keys are filed under: `user:<entity_id>`,
    the same identity the seal-for-user lane uses (server/vault/identity.py)."""
    return vault_identity.user_identity(str(brain_service._entity_id))


async def _key_block(pool) -> dict:
    """The keys this Host's backups are encrypted to. The backup reads these
    same rows (host_backup.recipients), so this IS who can open the next
    backup: the recovery key (pinned, and any different one the account page
    now offers), older recovery keys that still open older backups, and any
    bastion keys."""
    from server import host_backup
    rec = await host_backup.pin_recovery_key(pool, brain_service._entity_id)
    older = [{"fingerprint": backup_key.fingerprint(r["pubkey"]), "label": r["label"],
              "created_at": r["created_at"].isoformat() if r.get("created_at") else None}
             for r in await backup_key.list_all(pool, _user_identity(), backup_key.KIND_HOST_RECOVERY)
             if r.get("superseded_at") is not None]
    bastions = [{"kind": r["kind"], "label": r["label"],
                 "fingerprint": backup_key.fingerprint(r["pubkey"]),
                 "created_at": r["created_at"].isoformat() if r.get("created_at") else None}
                for r in await backup_key.list_active(pool, _user_identity())
                if r["kind"] == backup_key.KIND_BASTION]
    return {"registered": rec["pinned"] is not None,
            "recovery": rec["pinned"], "recovery_offer": rec["offer"],
            "recovery_older": older, "bastions": bastions,
            # Using a recovery key, the first one included, takes a code from
            # the second factor: Settings says how to enrol one when there is none.
            "stepup_enrolled": await nac_stepup.is_enrolled(pool)}


@router.get(
    "/settings",
    dependencies=[Depends(requires_permission("read:backup_settings"))],
)
async def get_backup_settings() -> dict:
    """The user's backup schedule, target, key state and custody."""
    out = _effective(await _load_bp(brain_service._entity_id))
    pool = brain_service.get_pool()
    if pool is None:
        # Schedule still answers from the row we already loaded; say the key
        # half is unknown rather than reporting "no key" on a pool blip.
        out["key"] = {"registered": None, "reason": "brain pool down"}
        return out
    out["key"] = await _key_block(pool)
    out["ship"] = await _ship_block(pool)
    note = _ship_limit_note(out)
    if note:
        out["ship"]["note"] = note
    return out


def _ship_limit_note(eff: dict) -> Optional[str]:
    """Said when the owner's backups go only to one of their machines and the
    newest is larger than a bastion can put back through platform storage in
    one piece (host_restore.PUSH_BACK_MAX): restoring from that copy on this
    Host would be refused, and the owner should know now, not then."""
    from server import host_restore
    ship = eff.get("ship") or {}
    if ((eff.get("target") or {}).get("kind") != backup_ship.KIND_MACHINE or eff.get("platform") is not False
            or int(ship.get("artifact_bytes") or 0) <= host_restore.PUSH_BACK_MAX):
        return None
    return ("Your newest backup is larger than 5 GiB, and your backups are kept only on your "
            "machine. A copy that size cannot be brought back through platform storage to restore "
            "this host; keep backups in platform storage too, or send them to your own storage, "
            "to be able to restore from here.")


async def _ship_block(pool) -> dict:
    """What the user's own destination has actually received.

    Three separate facts, kept separate: whether an artifact is even available
    to send, what the last attempt did, and — when it failed — WHICH KIND of
    failure it was. A single "last backup: failed" tells a user nothing they can
    act on; the reason class maps to a sentence and the provider's own words sit
    under it."""
    try:
        src = await backup_ship.get_source(pool)
        attempts = await backup_ship.last_attempts(pool, limit=3)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("backup: ship state unreadable: %r", e)
        return {"available": None, "reason": "state unreadable"}
    last = attempts[0] if attempts else None
    return {
        # An artifact exists to send. Absent is the NORMAL state until the first
        # platform backup after a destination is saved — not an error.
        "available": src is not None,
        "artifact_bytes": int(src["size_bytes"]) if src else None,
        "artifact_at": (src["snapshot_time"].isoformat()
                        if src and src.get("snapshot_time") else None),
        "last": None if not last else {
            "state": last["state"],
            "destination": last["destination"],
            "bytes": int(last["bytes_sent"] or 0),
            "at": last["started_at"].isoformat() if last["started_at"] else None,
            "finished_at": (last["finished_at"].isoformat()
                            if last["finished_at"] else None),
            "reason": last["reason"],
            # The class rendered as a sentence, so the UI never has to carry a
            # second copy of this mapping and drift from it.
            "reason_text": backup_ship.REASONS.get(last["reason"] or "", None),
            "detail": (last["detail"] or "")[:400],
        },
    }


# Reducing how the Host is protected is the owner's act (coordinator
# decision): turning backups off, backing up weekly instead of
# daily (fewer restore points), taking them out of platform storage, or
# removing or moving the second copy. Two checks, both needed:
#  1. the caller is the Settings app. Every request here carries an app token
#     (requires_permission answers 401 without one, server/applications/
#     middleware.py:252-257). The Host reaches this route through kernel_op
#     with a token minted as app id "host" (brain/mastercode/runtime/
#     kernel_ops.py run_kernel_op); the middleware puts it on
#     request.state.app_token (middleware.py:179). A first filter only: a
#     "settings" token can also be minted through POST /api/applications/
#     {app_id}/launch by a process on loopback (a browser page gets
#     no Settings token from /launch, and Settings' own token is honoured only
#     on Settings' origin — server/routes/applications.py), so the app id
#     alone proves nothing about who is at the keyboard;
#  2. the owner's second factor (_require_stepup), the same code that guards
#     their keys and a restore. The Settings app asks for it only when a
#     change weakens protection. Anything else, including moving the time or
#     turning protection back on, needs neither.
OWNER_APP_IDS = ("settings",)


def _from_owner(request: Request) -> bool:
    tok = getattr(request.state, "app_token", None)
    return getattr(tok, "app_id", None) in OWNER_APP_IDS


async def _require_owner_code(request: Request, pool, code, what: str) -> None:
    """The two checks above, for a change that weakens protection."""
    if not _from_owner(request):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"only the owner can {what}: they do it in the Settings app, Backup tab, with "
            "their six-digit code. Other schedule changes are fine from here.")
    if not code:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            f"enter your six-digit code to {what}")
    await _require_stepup(pool, str(code), what=f"choosing to {what}")


def _reduces_protection(prior: dict, new: dict) -> Optional[str]:
    """What, if anything, a change of backup settings takes away."""
    was = str(prior.get("frequency", "daily")).lower()
    if new.get("frequency") == "off" and was != "off":
        return "turn backups off"
    if new.get("frequency") == "weekly" and was == "daily":
        return "back up weekly instead of daily"
    if prior.get("platform", True) is not False and new.get("platform") is False:
        return "stop keeping backups in platform storage"
    # Adding a destination counts too: it sends the owner's backups (ciphertext)
    # somewhere and spends a credential from their Keys app on it, so the Host
    # must not choose one for them.
    old_target = prior.get("target") or None
    new_target = new.get("target") or None
    if new_target != old_target:
        return ("send a second copy of your backups somewhere new" if old_target is None
                else "remove or change where the second copy goes")
    return None


@router.put(
    "/settings",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def set_backup_settings(request: Request) -> dict:
    """Persist the schedule/target. Validated hard, and rejected loudly.

    A silently-clamped or silently-dropped field here means a user believes
    their host is backed up on a schedule that nothing is running. Every bad
    value is a 400 naming the field.
    """
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")

    incoming = dict(DEFAULTS)
    incoming.update({k: v for k, v in body.items() if k in DEFAULTS})

    freq = str(incoming["frequency"]).strip().lower()
    if freq not in FREQUENCIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"frequency must be one of {', '.join(FREQUENCIES)} (got {freq!r})")

    def _int_in(name: str, lo: int, hi: int) -> int:
        raw = incoming[name]
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{name} must be a whole number (got {raw!r})") from None
        if not lo <= n <= hi:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{name} must be between {lo} and {hi} (got {n})")
        return n

    hour = _int_in("hour", 0, 23)
    minute = _int_in("minute", 0, 59)
    weekday = _int_in("weekday", 0, 6)

    try:
        target = backup_ship.validate_target(incoming["target"])
    except ValueError as e:
        # Every rejection names its field. A silently-clamped destination means
        # a user believing a second copy exists that nothing is writing.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None

    settings = {
        "frequency": freq,
        "hour": hour,
        "minute": minute,
        "weekday": weekday,
        "target": target,
    }

    if "platform" in body and not isinstance(body["platform"], bool):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "platform must be true or false")
    eid = brain_service._entity_id
    # Read, check and write under the row's lock, and write only the `backup`
    # key: the weakening check is against what is stored when the write
    # happens (not an earlier read another request may have changed since),
    # and another writer of behavioral_params (the timezone, say) is never
    # overwritten.
    async with pool.acquire() as conn:
        async with conn.transaction():
            raw = await conn.fetchval(
                "SELECT behavioral_params FROM entity_registry WHERE id = $1 FOR UPDATE", eid)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    raw = {}
            bp = raw if isinstance(raw, dict) else {}
            prior = bp.get("backup") or {}
            if not isinstance(prior, dict):
                prior = {}
            # Platform storage (ciphertext only): kept unless this request
            # says otherwise, because the whole `backup` object is replaced.
            if "platform" in body:
                settings["platform"] = body["platform"]
            elif "platform" in prior:
                settings["platform"] = bool(prior["platform"])
            if settings.get("platform") is False and not target:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "with platform storage off, choose where your backups go instead "
                    "(your bastion or your own storage); otherwise they would go nowhere")
            reduced = _reduces_protection(prior, settings)
            if reduced:
                await _require_owner_code(request, pool, body.get("stepup_code"), reduced)
            await conn.execute(
                "UPDATE entity_registry SET behavioral_params = jsonb_set("
                "  CASE WHEN jsonb_typeof(behavioral_params) = 'object' THEN behavioral_params "
                "       ELSE '{}'::jsonb END, '{backup}', $1::jsonb) WHERE id = $2",
                json.dumps(settings), eid)
    bp["backup"] = settings
    logger.info("backup settings updated for entity %s: %s/%02d:%02d target=%s",
             eid, freq, hour, minute, (target or {}).get("kind") or "platform-only")
    return _effective(bp)


# ── The Host's own backups (server/host_backup.py) ───────────────────────────
# The Host backs itself up, encrypted to keys only the user
# holds. These are its runs: when each ran, why, what it saved, who it was
# encrypted to, and how it ended. Exposed to the Host too (kernel_ops), so "did
# my backup run last night" gets a true answer, and so does "back me up now" —
# a deterministic act with nothing to approve.

def _run_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
        "finished_at": row["finished_at"].isoformat() if row.get("finished_at") else None,
        "reason": row["reason"],
        "state": row["state"],
        "stamp": row.get("stamp"),
        "stored": row.get("stored"),
        "size_bytes": row.get("size_bytes"),
        "recipients": row.get("recipients") or [],
        "parts": row.get("parts") or {},
        "error": row.get("error"),
    }


@router.get(
    "/runs",
    dependencies=[Depends(requires_permission("read:backup_settings"))],
)
async def get_backup_runs() -> dict:
    """The Host's recent backup runs, newest first, and whether it can back up
    at all (`problem` says what is missing when it cannot)."""
    from server import host_backup
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    problem = host_backup.configured()
    if problem is None and host_backup.restore_in_progress():
        # Said plainly, so a restore that has not finished never silently
        # holds backups up.
        problem = host_backup.RESTORE_IN_PROGRESS_TEXT
    if problem is None:
        await host_backup.pin_recovery_key(pool, brain_service._entity_id)
        if not await host_backup.recipients(pool, brain_service._entity_id):
            problem = host_backup.NO_RECIPIENT_TEXT
    return {
        "problem": problem,
        "running": host_backup._run_lock.locked(),
        "runs": [_run_json(r) for r in await host_backup.last_runs(pool, limit=10)],
    }


@router.post(
    "/runs",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def start_backup_run() -> dict:
    """Back up now. Returns at once; the run shows up in GET /runs. A run that
    is already going is not started twice."""
    import asyncio
    from server import host_backup
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    problem = host_backup.configured()
    if problem:
        raise HTTPException(status.HTTP_409_CONFLICT, problem)
    await host_backup.pin_recovery_key(pool, brain_service._entity_id)
    if not await host_backup.recipients(pool, brain_service._entity_id):
        raise HTTPException(status.HTTP_409_CONFLICT, host_backup.NO_RECIPIENT_TEXT)
    if host_backup._run_lock.locked():
        return {"started": False, "running": True}
    asyncio.create_task(host_backup.run_once(pool, brain_service._entity_id, "manual"))
    return {"started": True, "running": True}


# ── Restoring the Host from a backup (server/host_restore.py) ────────────────
#
# A restore replaces everything on the Host with an earlier backup, so it is
# the user's act alone: walled from the Host (kernel_ops._WALLED_PATHS) and
# behind the same second factor as the backup keys below. It takes two acts:
# get the backup's key sealed to THIS Host (the owner's browser does it with
# their recovery key, which never leaves the browser, or their bastion does it
# with its key), then restore.

_RESTORE_TASKS: set = set()
_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")


class RestoreReq(BaseModel):
    stepup_code: str
    stamp: str
    # The owner read that this Host cannot confirm it made this backup
    # (host_restore.UNVERIFIED_TEXT) and restores it anyway.
    unverified_ok: bool = False


class RestoreKeyReq(BaseModel):
    stepup_code: str
    stamp: str
    via: str                      # "browser" | "bastion" | "paste"
    token_id: int | None = None   # which bastion, for via=bastion
    sidecar: str | None = None    # the resealed key file (age ciphertext), for browser/paste


@router.get(
    "/restore",
    dependencies=[Depends(requires_permission("read:backup_settings"))],
)
async def get_restore() -> dict:
    """The backups this Host can be restored from, whether each one's key is
    ready, the bastions that can reseal a key, and any restore that is
    staged, running or just finished. A key a bastion is resealing is checked
    on here, so the card moves on by itself."""
    from server import host_backup, host_restore
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    eid = brain_service._entity_id
    out = await host_restore.available(pool, eid)
    changed = False
    for b in out.get("backups") or []:
        if (b.get("reseal") or {}).get("state") == "pending":
            await host_restore.poll_reseal(pool, eid, b["stamp"])
            changed = True
        if (b.get("push") or {}).get("state") == "pending":
            await host_restore.poll_push(pool, eid, b["stamp"])
            changed = True
    if changed:
        out = await host_restore.available(pool, eid)
    out["progress"] = await host_backup._state(pool, "restore")
    out["running"] = host_backup._run_lock.locked()
    return out


@router.post(
    "/restore/key",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def restore_key(req: RestoreKeyReq) -> dict:
    """Get one backup's key sealed to this Host: the owner's browser resealed
    it with their recovery key (via=browser: the key never left the browser,
    only the resealed file comes here), or a bastion does it after the person
    approves on that machine (via=bastion). Either way this Host checks the
    result opens with its own key and names this backup before keeping it.
    The second factor gates the REQUEST; it never holds a key."""
    from server import host_restore
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    if not _STAMP.fullmatch(req.stamp or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "choose a backup from the list")
    if req.via not in ("browser", "bastion", "paste"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "via must be browser, bastion or paste")
    await _require_stepup(pool, req.stepup_code, what="restoring a backup")
    eid = brain_service._entity_id
    try:
        if req.via == "bastion":
            if not req.token_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "choose which bastion")
            return await host_restore.request_reseal(pool, eid, req.stamp, int(req.token_id))
        text = (req.sidecar or "").strip()
        if not text.startswith("-----BEGIN AGE ENCRYPTED FILE-----") or len(text) > 16384:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "paste the whole resealed key file, from -----BEGIN AGE "
                                "ENCRYPTED FILE----- to its END line")
        return await host_restore.accept_resealed(pool, req.stamp, (text + "\n").encode("ascii"),
                                                  via=req.via)
    except host_restore.RestoreError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except UnicodeEncodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a key file is plain text") from None


@router.get(
    "/restore/sidecar",
    dependencies=[Depends(requires_permission("read:backup_settings"))],
)
async def restore_sidecar(stamp: str) -> dict:
    """What the owner's browser needs to reseal one backup's key to this Host:
    the backup's sealed key file (age ciphertext: only the recovery key or a
    bastion key opens it), this Host's own recipient, and the ids the browser
    checks the file against. No key that opens anything is in it."""
    from server import host_restore
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    if not _STAMP.fullmatch(stamp or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "choose a backup from the list")
    try:
        return await host_restore.sidecar_for_browser(pool, brain_service._entity_id, stamp)
    except host_restore.RestoreError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None


# ── Exports the owner asked for on the account page (server/host_export.py) ──
# An export is a full copy of the Host, so the Host builds one only when the
# owner confirms, here, the key it is for: THE RULE. Walled from the Host
# (kernel_ops), behind the second factor.

_EXPORT_TASKS: set = set()


class ExportConfirmReq(BaseModel):
    stepup_code: str
    id: int
    fingerprint: str


@router.get(
    "/exports",
    dependencies=[Depends(requires_permission("read:backup_settings"))],
)
async def get_exports() -> dict:
    """Exports waiting for the owner's confirmation, with each key's
    fingerprint to compare with the account page."""
    from server import host_export
    try:
        return {"pending": await host_export.pending()}
    except Exception as e:
        return {"pending": [], "problem": f"the platform could not be asked: {e}"}


@router.post(
    "/exports/confirm",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def confirm_export(req: ExportConfirmReq) -> dict:
    """The owner confirms one export and the key it is for. The Host claims
    exactly that request, if its key is still that one, and builds it in the
    background."""
    import asyncio
    from server import host_export
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    if not re.fullmatch(r"[0-9a-f]{16}", req.fingerprint or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "give the key's 16-character fingerprint")
    await _require_stepup(pool, req.stepup_code, what="confirming an export")

    async def _run():
        try:
            await host_export.confirm(pool, brain_service._entity_id, req.id, req.fingerprint)
        except host_export.ExportError as e:
            logger.info("export %s not built: %s", req.id, e)
        except Exception:  # pragma: no cover - defensive
            logger.exception("export %s failed", req.id)
    # Refusals that need no build answer at once; the build runs on.
    pending = {p["id"]: p for p in await host_export.pending()}
    p = pending.get(req.id)
    if p is None or p.get("fingerprint") != req.fingerprint:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "that export is not waiting with that key: compare it with the "
                            "fingerprint in your export key file")
    task = asyncio.create_task(_run())
    _EXPORT_TASKS.add(task)
    task.add_done_callback(_EXPORT_TASKS.discard)
    return {"started": True, "note": "Confirmed. Your Host is building the export; the account "
                                     "page offers it when it is ready."}


class RestoreFetchReq(BaseModel):
    stepup_code: str
    stamp: str
    token_id: int


@router.post(
    "/restore/fetch",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def restore_fetch(req: RestoreFetchReq) -> dict:
    """A backup kept only on one of the user's machines: ask that bastion to
    put it back into this Host's staging folder (ciphertext, as it always
    was), after the person approves on the machine. Then it is restored like
    any other."""
    from server import host_restore
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    if not _STAMP.fullmatch(req.stamp or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "choose a backup from the list")
    await _require_stepup(pool, req.stepup_code, what="restoring a backup")
    try:
        return await host_restore.request_push(pool, brain_service._entity_id, req.stamp,
                                               int(req.token_id))
    except host_restore.RestoreError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None


@router.post(
    "/restore",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def start_restore(req: RestoreReq) -> dict:
    """Restore the chosen backup. Returns at once: the Host backs up how it is
    now, stages the backup, then restarts onto it. GET /restore shows the
    progress until the restart."""
    import asyncio
    from server import host_backup, host_restore
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    if not _STAMP.fullmatch(req.stamp or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "choose a backup from the list")
    # Before the code is spent: a refusal here must not cost the owner a code.
    if host_restore.load_resealed(req.stamp) is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "this backup's key has not been sealed to this Host yet")
    if not req.unverified_ok and not await host_restore.verified(pool, req.stamp):
        raise HTTPException(status.HTTP_409_CONFLICT, host_restore.UNVERIFIED_TEXT)
    await _require_stepup(pool, req.stepup_code, what="restoring a backup")
    if host_restore.read_plan() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a restore is already waiting for this Host to restart")
    if host_backup._run_lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a backup or restore is running; try again when it has finished")
    task = asyncio.create_task(host_restore.stage(pool, brain_service._entity_id, req.stamp,
                                                  unverified_ok=req.unverified_ok))
    _RESTORE_TASKS.add(task)
    task.add_done_callback(_RESTORE_TASKS.discard)
    return {"started": True}


@router.delete(
    "/restore",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def cancel_restore() -> dict:
    """Drop a restore that is staged but not yet swapped in."""
    from server import host_restore
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    return {"cancelled": await host_restore.cancel(pool)}


# ── The keys backups are encrypted to (server/backup_key.py) ─────────────────
#
# Every route below is in kernel_ops._WALLED_PATHS: registering or adopting a
# key decides who can read the user's data. The TOTP step-up is the same factor
# the Network app enrols and the Keys app uses for a reveal:
# server/network/nac_stepup.py.

# Strong references to in-flight ship tasks (see ship_backup_now).
_SHIP_TASKS: set = set()

KEY_ON_BASTION = ("Make a bastion key on that bastion with `privatae-bastion backup key init` "
                  "(it keeps the private half and prints the public one), then register "
                  "the public half here.")


class RegisterKeyReq(BaseModel):
    stepup_code: str
    pubkey_b64: str
    kind: str = backup_key.KIND_BASTION   # the only kind registered here
    label: str = ""
    # Replacing a key is not the same act as registering the first one: every
    # backup already taken stays encrypted to the OLD key, so a silent
    # supersede quietly changes which key opens which backup.
    replace: bool = False


class RetireKeyReq(BaseModel):
    stepup_code: str
    kind: str = backup_key.KIND_BASTION
    label: str = ""


class AdoptRecoveryReq(BaseModel):
    stepup_code: str
    fingerprint: str


async def _require_replace_consent(pool, replace: bool, kind: str, label: str) -> None:
    """A second key for the same (kind, label) must be asked for, not slipped in."""
    row = await backup_key.get_active(pool, _user_identity(), kind, label)
    if row is not None and not replace:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a {kind} key{(' for ' + label) if label else ''} is already registered "
            f"(fingerprint {backup_key.fingerprint(row['pubkey'])}, added "
            f"{row['created_at'].date().isoformat()}). Replacing it does NOT re-encrypt the "
            "backups you already have: keep the old key, because it is what opens them. "
            "Send replace=true if that is what you want.")


# How the owner enrols the second factor these routes ask for (the Network
# app's banner chip, system-apps/network/app.js renderNacStepupChip).
ENROL_HOW = ("open the Network app, click \"Second factor: Enroll\" at the top, scan the code "
             "with an authenticator app and enter the code it shows")


async def _require_stepup(pool, code: str, what: str = "changing your backup keys") -> None:
    if not await nac_stepup.is_enrolled(pool):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{what} needs a code from your second factor, and you have not set one up yet: "
            f"{ENROL_HOW}. Then come back and try again")
    # Too many wrong codes raises nac_stepup.StepupLocked, a 429 with the wait,
    # the same answer every route that asks for a code gives.
    if not await nac_stepup.verify_stepup(pool, code):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid step-up code")


KEY_MADE_ELSEWHERE = ("Backup keys are no longer made or kept on the Host. Your recovery key "
                      "is made on your account page; a bastion's key is made on that bastion "
                      "with `privatae-bastion backup key init`, and its public half is added "
                      "here.")


@router.post(
    "/key/generate",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def generate_backup_key() -> dict:
    """RETIRED: a key made here had its private half in this pod."""
    raise HTTPException(status.HTTP_410_GONE, KEY_MADE_ELSEWHERE)


@router.post(
    "/key/escrow",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def set_backup_escrow() -> dict:
    """RETIRED: there is no platform recovery key."""
    raise HTTPException(status.HTTP_410_GONE, "There is no platform recovery: backups are "
                        "encrypted only to your recovery key and your bastions' keys. "
                        + KEY_MADE_ELSEWHERE)


@router.get(
    "/key/sealed",
    dependencies=[Depends(requires_permission("read:backup_settings"))],
)
async def get_sealed_backup_key() -> dict:
    """RETIRED: no key's private half is kept on the Host."""
    raise HTTPException(status.HTTP_410_GONE, KEY_MADE_ELSEWHERE)


@router.post(
    "/key/register",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def register_backup_key(req: RegisterKeyReq) -> dict:
    """Register the PUBLIC half of a key one of the owner's bastions made.
    From the next backup on, backups are also encrypted to it. Nothing about
    the private key ever exists in this pod. The recovery key is not
    registered here: it comes from the account page (host_backup.pin_recovery_key)."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    kind = (req.kind or backup_key.KIND_BASTION).strip()
    if kind not in backup_key.REGISTERABLE_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "only a bastion's key is registered here; your recovery key is "
                            "made and saved on your account page")
    label = (req.label or "").strip()
    if len(label) > backup_key.MAX_LABEL or any(ord(c) < 32 for c in label):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"label must be plain text of at most {backup_key.MAX_LABEL} characters")
    if not label:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name the bastion this key belongs to")
    # The SAME gate as every custody change: registering a key decides who can
    # decrypt this Host's backups.
    await _require_stepup(pool, req.stepup_code)
    await _require_replace_consent(pool, req.replace, kind, label)
    import base64 as _b64
    try:
        pub = _b64.b64decode(req.pubkey_b64.strip(), validate=True)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "pubkey_b64 is not valid base64") from None
    if len(pub) != 32:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"an X25519 public key is 32 raw bytes; this is {len(pub)}. "
            "Did you paste the PRIVATE key file by mistake?")
    ident = _user_identity()
    await backup_key.put_active(pool, ident=ident, pub_raw=pub, sealed=None,
                                origin="self-generated", kind=kind, label=label)
    fp = backup_key.fingerprint(pub)
    logger.info("backup key registered for %s: %s %r (fingerprint %s)", ident, kind, label, fp)
    return {"ok": True, "fingerprint": fp, "identity": ident, "kind": kind, "label": label,
            "next": "Your next backup is encrypted to this key as well."}


@router.post(
    "/key/retire",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def retire_backup_key(req: RetireKeyReq) -> dict:
    """Stop encrypting NEW backups to one bastion's key. Backups already taken
    stay encrypted to it; the row is kept so the Host can still say which key
    opens which backup. The recovery key is not retired here: it is only
    replaced, by adopting a new one."""
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    if req.kind != backup_key.KIND_BASTION:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "only a bastion's key can be retired; a recovery key is replaced "
                            "by saving a new one on your account page")
    await _require_stepup(pool, req.stepup_code)
    if not await backup_key.retire(pool, _user_identity(), req.kind, (req.label or "").strip()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key is registered")
    return {"ok": True, "note": "Backups taken before stay encrypted to that key."}


@router.post(
    "/key/recovery/adopt",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def adopt_recovery_key(req: AdoptRecoveryReq) -> dict:
    """Adopt the recovery key the account page holds for this Host: the
    first one, or one in place of the one this Host uses. Never automatic:
    a key arriving from the platform is exactly what a platform that wanted
    to read the next backups would send. The owner compares the fingerprint
    with the one on the key they saved and says so, with their second factor.
    An older key stays listed: backups taken before are still encrypted to it."""
    from server import host_backup
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    await _require_stepup(pool, req.stepup_code, what="choosing your recovery key")
    try:
        got = await host_backup.adopt_recovery_offer(pool, brain_service._entity_id, req.fingerprint)
    except host_backup.BackupError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    note = ("Your host backs up to this recovery key from now on." if got["first"] else
            "Your next backup is encrypted to this recovery key. Keep the old one too: "
            "backups taken before open only with it.")
    return {"ok": True, "fingerprint": got["fingerprint"], "note": note}


@router.post(
    "/ship",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def ship_backup_now() -> dict:
    """START sending the newest backup to the saved destination.

    STARTS, and returns. A transfer can run for hours, and holding an HTTP
    request open for that would be dead by proxy timeout long before it
    finished. The work runs as a task; progress is the `ship` block of
    GET /api/backup/settings, and the attempt is recorded in backup_ship_log
    either way.

    WALLED from the host: this spends the user's egress and writes into storage
    they pay for. A button they press, not something an agent decides to do.
    The unattended path is the ship worker (backup_ship.run_backup_ship_loop),
    which runs the same code after each backup."""
    import asyncio

    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    eid = brain_service._entity_id
    eff = _effective(await _load_bp(eid))
    target = eff.get("target")

    # The cheap refusals, inline.
    if not target:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "reason": "no_target", "reason_text": backup_ship.REASONS["no_target"],
            "detail": "save a destination first"})
    src = await backup_ship.get_source(pool)
    if not src:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "reason": "no_source", "reason_text": backup_ship.REASONS["no_source"],
            "detail": "one is sent after your next backup"})

    async def _run():
        try:
            await backup_ship.ship_now(pool, eid, target)
        except backup_ship.ShipError:
            pass          # already recorded with its class, and shown in the tab
        except Exception:  # pragma: no cover - defensive
            logger.exception("backup ship task failed")

    # HOLD THE REFERENCE. asyncio keeps only a weak reference to a task, so a
    # bare create_task can be garbage-collected mid-flight.
    task = asyncio.create_task(_run())
    _SHIP_TASKS.add(task)
    task.add_done_callback(_SHIP_TASKS.discard)
    return {"ok": True, "started": True,
            "note": "Sending. Progress shows on this tab; it keeps going if you close it."}


# ── The user's own MEMORY MIRROR ─────────────────────────────────────────────
# A third, independent copy, and the only one the user holds themselves: a
# streaming PostgreSQL replica of this entity's database, running in a
# container on their own hardware and fed over the bastion lane: a local copy is
# their continuity.
#
# Read is on read:backup_settings, NOT read:backup_list, and that is
# deliberate. backup_list is operator-domain (0 bits in ENTITY_SCOPE_TO_BITS)
# so the Host cannot reach it; backup_settings is entity-domain, which lets
# the Host answer "is my memory mirrored, and how far behind is it?" — the
# question the surface exists for. Reusing the scope also means no new entry
# in scopes.py and no manifest change for an app that already holds it.
# /api/backup/local stays walled exactly as it is.
#
# Writing is a WIDEN (it hands a machine a full copy of the memory), so the
# service layer takes a TOTP step-up. That gate lives in mirror.enable, not
# here, so this route and any future caller cannot diverge from each other.

@router.get(
    "/mirror",
    dependencies=[Depends(requires_permission("read:backup_settings"))],
)
async def get_backup_mirror() -> dict:
    """Mirror status. Absent is the normal answer and is never an error."""
    from server.network import mirror
    return await mirror.status(brain_service.get_pool(), brain_service._entity_id)


@router.put(
    "/mirror",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def set_backup_mirror(request: Request) -> dict:
    """Turn the mirror on for a bastion, or off.

    Body: {"enabled": true, "token_id": <int>, "stepup_code": "<6 digits>"}
          {"enabled": false, "stepup_code": "<6 digits>"}

    Turning it OFF takes the owner's code too: it ends a live copy
    of the memory on a machine they own, which weakens protection, and a
    "settings" app token alone does not prove the owner asked
    (_require_owner_code). It still drops the replication slot at once.
    """
    from server.network import mirror
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")

    eid = brain_service._entity_id
    try:
        if not body.get("enabled"):
            await _require_owner_code(request, pool, body.get("stepup_code"),
                                      "stop the memory mirror")
            return await mirror.disable(pool, eid)
        try:
            token_id = int(body.get("token_id"))
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "token_id is required — name the bastion that will hold the mirror"
            ) from None
        return await mirror.enable(pool, eid, token_id,
                                   str(body.get("stepup_code") or ""))
    except mirror.MirrorError as e:
        raise HTTPException(e.status, e.detail) from None


@router.delete(
    "/mirror",
    dependencies=[Depends(requires_permission("write:backup_settings"))],
)
async def delete_backup_mirror(request: Request) -> dict:
    """Turn the mirror off, with the owner's code (body {"stepup_code"}), the
    same as PUT {"enabled": false}.

    Separate from PUT so both write methods can be walled from the Host in one
    place (kernel_ops._WALLED_OPS) while the read stays open to it.
    """
    from server.network import mirror
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    try:
        body = await request.json()
    except Exception:
        body = {}
    await _require_owner_code(request, pool, (body or {}).get("stepup_code") if isinstance(body, dict)
                              else None, "stop the memory mirror")
    try:
        return await mirror.disable(pool, brain_service._entity_id)
    except mirror.MirrorError as e:
        raise HTTPException(e.status, e.detail) from None
