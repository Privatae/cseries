"""mirror — the user-held memory mirror.

A STREAMING REPLICA of
the entity's database, in a container on the user's OWN hardware, reached over
the bastion lane. The cloud copy stays the serving path; this copy is
possession and continuity, not failover.

WHAT THIS MODULE OWNS
  * the enable/disable decision and where it is stored
  * the physical replication slot's lifecycle, including dropping it when the
    bastion holding it is revoked
  * the status the Backup tab and the Host both read

WHAT IT DELIBERATELY DOES NOT OWN
  * the wire (server/network/pgwire_relay.py)
  * the replica itself — that runs on the user's machine, is started and
    stopped by their bastion, and this pod has no reach into it. Everything
    reported here is observed from the PRIMARY's own catalogs; nothing is
    taken on the bastion's word.

STORAGE. The config lives in behavioral_params["backup_mirror"] — a SIBLING of
behavioral_params["backup"], not a key inside it. That is not cosmetic:
routes/backup.py::set_backup_settings assigns bp["backup"] wholesale from the
schedule form, so a mirror stored inside it would be silently erased the next
time the user saved their backup time. behavioral_params is a nullable JSONB
column (init-db.sql:67), so no migration is needed either way.

RETENTION. The slot is what lets a mirror fall behind and catch up instead of
re-seeding. It is also what lets an offline machine pin WAL, so the DB image
bounds it (max_slot_wal_keep_size=1024MB in entrypoint-postgres.sh). Past that
bound PostgreSQL invalidates the slot rather than filling the volume, and this
module reports that as `needs_reseed` — an honest state the user can act on,
not a silent stall.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("cseries.network.mirror")

RECONCILE_INTERVAL_S = 60

# Slot names are matched by this prefix when reaping; keep it distinctive so a
# slot created by anything else is never touched.
_SLOT_PREFIX = "mirror_b"
_SLOT_RE = re.compile(r"^mirror_b(\d+)$")

# Published by the DB container into the socket emptyDir both containers
# mount (entrypoint-postgres.sh). Same directory and the same absent-is-normal
# contract as .container-stats — an older DB image writes nothing here.
_DB_BUILD_INFO = Path("/var/run/postgresql/.db-build-info")

# The capability a bastion advertises once it can actually hold a mirror (it
# needs a container runtime). Same shape as the embed lane's capability gate:
# absent capability = the box simply cannot do it, and the UI says so rather
# than offering a control that would fail.
MIRROR_CAPABILITY = "mirror"


class MirrorError(Exception):
    """Refusal with a user-facing reason. Carries an HTTP status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def slot_name(token_id: int) -> str:
    return f"{_SLOT_PREFIX}{int(token_id)}"


def application_name(bastion_name: str) -> str:
    """What pg_stat_replication will show for this stream.

    Set by the KERNEL in the startup rewrite, never by the bastion, so the
    attribution in the status below cannot be forged by a compromised box.
    Sanitised because it is rendered in a UI and lands in server logs.
    """
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", (bastion_name or "bastion"))[:48]
    return f"mirror:{clean}"


def db_build_info() -> dict:
    """Which cseries-postgres build this pod's database container is running.

    This is the pin the bastion's replica must match — a standby cannot replay
    WAL from a different PostgreSQL build. Absent is normal and returns
    {"known": False}: a DB image older than 2026-08-18 publishes nothing here,
    and the two images ship on independent cadences, so the kernel must
    degrade rather than assume.

    `digest` is frequently empty even when this file exists: the ship builds
    the image before it has a digest to bake (that is the order docker
    imposes), so the runbook passes PG_TAG and not PG_DIGEST. TAG is the
    reliable identifier and is exact — the image release step hard-fails
    if the tag already exists in the registry, so a tag is never reused and
    maps to exactly one digest. The bastion resolves tag -> digest + sha256
    from the published manifest and verifies what it downloads.
    """
    try:
        raw = json.loads(_DB_BUILD_INFO.read_text())
    except Exception:
        return {"known": False,
                "reason": "the database container has not published its build "
                          "(image predates this, or the socket volume is not shared)"}
    if not isinstance(raw, dict):
        return {"known": False, "reason": "unparseable build-info"}
    return {
        "known": True,
        "image": raw.get("image") or None,
        "tag": raw.get("tag") or None,
        "digest": raw.get("digest") or None,
        "built_at": raw.get("built_at") or None,
    }


# ── config -------------------------------------------------------------------

async def _load_bp(pool, eid: int) -> dict:
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid)
    bp = row["behavioral_params"] if row and row["behavioral_params"] else {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = {}
    return bp or {}


async def _save_mirror_cfg(pool, eid: int, cfg: dict | None) -> None:
    bp = await _load_bp(pool, eid)
    if cfg is None:
        bp.pop("backup_mirror", None)
    else:
        bp["backup_mirror"] = cfg
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
        json.dumps(bp), eid)


async def get_config(pool, eid: int) -> dict:
    cfg = (await _load_bp(pool, eid)).get("backup_mirror") or {}
    return cfg if isinstance(cfg, dict) else {}


# ── slot lifecycle -----------------------------------------------------------

async def db_name(pool) -> str:
    """The pod's own database name, asked of the database.

    Never a literal: the database's name has changed before, and a constant here
    would be wrong on one side of such a change.
    """
    return await pool.fetchval("SELECT current_database()")


async def _slot_exists(pool, name: str) -> bool:
    return bool(await pool.fetchval(
        "SELECT 1 FROM pg_replication_slots WHERE slot_name = $1", name))


async def retention_bounded(pool) -> tuple[bool, str]:
    """Does the DATABASE CONTAINER bound what a slot may pin? (bounded, detail)

    This is the precondition for creating a slot at all, and it is not a
    formality. The bound ships in entrypoint-postgres.sh, but the DB image rolls
    on its OWN cadence — an entity can sit on a pre-bound image indefinitely,
    where max_slot_wal_keep_size is PostgreSQL's default of -1, i.e. unlimited.
    Create a slot there and the failure is not subtle: the bastion refuses to
    run until it knows the build, so nothing ever consumes the slot, WAL is
    retained forever, the 5Gi entity-data PVC fills, and because local-path is
    a hostPath bind it takes the NODE and every other entity on it.

    So: no bound, no slot. The user is told to update their host's database
    first, which is a real instruction with a real fix, rather than being handed
    a mirror that quietly eats their pod.
    """
    try:
        raw = await pool.fetchval("SHOW max_slot_wal_keep_size")
    except Exception as e:  # noqa: BLE001
        # Cannot prove it is bounded -> treat as unbounded. The whole point is
        # to fail toward "no slot".
        return False, f"could not read the database's WAL retention limit ({e})"
    txt = str(raw or "").strip()
    if txt in ("-1", "-1MB", ""):
        return False, ("this host's database does not limit how much write-ahead log "
                       "one mirror can hold, so a mirror that went offline could fill "
                       "the host's disk")
    return True, txt


async def _create_slot(pool, name: str) -> None:
    if await _slot_exists(pool, name):
        return
    # immediately_reserve=true so WAL is held from the moment the user enables
    # the mirror, not from the moment the bastion first connects — otherwise a
    # box that takes a while to download the image starts from a gap.
    await pool.execute(
        "SELECT pg_create_physical_replication_slot($1, true)", name)
    logger.info("mirror: replication slot %s created", name)


async def _drop_slot(pool, name: str) -> bool:
    """Drop a slot, terminating its walsender first if one is attached.

    A slot in use cannot be dropped; without the terminate, revoking a bastion
    that is currently streaming would leave the slot (and its WAL retention)
    behind forever, which is the exact failure the retention bound exists to
    prevent.

    Returns whether the slot is GONE afterwards. The caller needs to know:
    observe() bumps the reseed generation on the strength of this, and a
    persistent failure that reported success would bump every 60s and make the
    bastion wipe and re-seed its copy on every cycle, forever.
    """
    if not await _slot_exists(pool, name):
        return True
    try:
        await pool.execute(
            "SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots "
            "WHERE slot_name = $1 AND active_pid IS NOT NULL", name)
    except Exception as e:  # noqa: BLE001
        logger.info("mirror: could not terminate walsender on %s: %s", name, e)
    last = None
    for _ in range(3):
        try:
            await pool.execute("SELECT pg_drop_replication_slot($1)", name)
            logger.info("mirror: replication slot %s dropped", name)
            return True
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(0.5)
    logger.warning("mirror: slot %s could not be dropped: %s", name, last)
    return False


# ── enable / disable ---------------------------------------------------------

async def _bastion(pool, eid: int, token_id: int) -> dict:
    """The bastion row, if it is genuinely still usable.

    list_bastions filters revoked_at but NOT expires_at, so an expired token
    still appears there. Every place that decides access filters both — the WS
    auth in main.py and the reconciler below — and this must agree with them, or
    the mirror would be enabled for (or reported as live on) a bastion that can
    no longer authenticate.
    """
    from server.network import topology
    live = await pool.fetch(
        "SELECT id FROM mcp_client_tokens WHERE entity_id = $1 "
        "AND operator_kind = 'bastion' AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now())", eid)
    live_ids = {int(r["id"]) for r in live}
    for b in await topology.list_bastions(pool, eid):
        if int(b.get("id") or 0) == int(token_id) and int(token_id) in live_ids:
            return b
    raise MirrorError(404, f"no active bastion with token_id {token_id}")


async def enable(pool, eid: int, token_id: int, stepup_code: str) -> dict:
    """Turn the mirror on for ONE bastion. Requires the user's 2FA.

    This is a WIDENING action — it grants a machine a complete, continuously
    updated copy of the user's memory — so it takes the same step-up as every
    other widen in the mesh (restoring a quarantined bastion, allowing an
    egress rule). Enrolling or revoking a bastion does not, because those are
    neutral or tighten. An agent outside this pod cannot produce the user's code,
    so it cannot grant itself a copy of the database. This does NOT hold against
    the entity's own workload, which can read the TOTP secret out of the in-pod
    trust-auth database — see nac_stepup's docstring.

    verify_stepup returns False when NO factor is enrolled (see nac_stepup.verify_stepup
    — named, not line-cited, because the line number was already stale), so an
    un-enrolled host fails closed here rather than sliding through.
    """
    from server.network import nac_stepup
    if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
        raise MirrorError(
            403,
            "Two-factor confirmation is required: a memory mirror gives that machine "
            "a complete, continuously updated copy of your memory. If you have not set "
            "up two-factor yet, do that first — this cannot be enabled without it.")
    b = await _bastion(pool, eid, token_id)
    caps = b.get("capabilities") or []
    if MIRROR_CAPABILITY not in caps:
        raise MirrorError(
            409,
            f"{b.get('hostname') or b.get('name') or 'That machine'} cannot hold a "
            "mirror yet — it needs Docker or Podman installed. Install one and it will "
            "offer itself on its next check-in.")

    # PRECONDITION, not a warning. A slot on a database that does not bound
    # retention is the one failure here that reaches past this entity.
    bounded, detail = await retention_bounded(pool)
    if not bounded:
        raise MirrorError(
            409,
            f"Your host's database needs its next update before it can hold a mirror — "
            f"{detail}. Nothing has been changed. This clears itself when the host "
            f"updates; ask it to check for updates.")
    build = db_build_info()
    if not build.get("known"):
        raise MirrorError(
            409,
            "Your host's database container has not reported which version it runs, so "
            "the machine holding the mirror would have no way to match it. This clears "
            "itself when the host updates its database.")

    name = slot_name(token_id)
    cfg = {
        "enabled": True,
        "token_id": int(token_id),
        "bastion_name": b.get("hostname") or b.get("name") or f"bastion-{token_id}",
        "slot": name,
        "enabled_at": _now_iso(),
        # Bumped whenever the primary sheds this slot. The bastion compares it
        # to what it last seeded at; a change means "your copy can no longer
        # catch up, start again".
        "reseed_generation": 1,
        # Set the first time a stream is actually OBSERVED. Until then no copy
        # is known to exist, and nothing may tell the user one is safe.
        "seen_streaming_at": None,
    }
    # Config FIRST, then the slot. The reverse order has a window: the
    # reconciler runs every 60s and drops any mirror_b<n> slot it cannot match
    # to an enabled config, so a tick landing between the two calls would drop
    # the slot that was just created and leave the config pointing at nothing.
    await _save_mirror_cfg(pool, eid, cfg)
    try:
        await _create_slot(pool, name)
    except Exception:
        # Do not leave a config claiming a mirror that has no slot behind it.
        await _save_mirror_cfg(pool, eid, None)
        raise
    logger.info("mirror: ENABLED for bastion %s (token %s), slot %s, retention %s",
                cfg["bastion_name"], token_id, name, detail)
    return await status(pool, eid)


async def disable(pool, eid: int) -> dict:
    """Turn the mirror off and release its WAL immediately.

    The copy already on the user's machine is THEIRS and is not touched — this
    pod has no reach into it and would not exercise one if it had. All that
    stops is the flow of new writes.
    """
    cfg = await get_config(pool, eid)
    if cfg.get("slot"):
        await _drop_slot(pool, str(cfg["slot"]))
    await _save_mirror_cfg(pool, eid, None)
    logger.info("mirror: disabled (slot released)")
    return await status(pool, eid)


# ── status -------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else (v if isinstance(v, str) else None)


async def status(pool, eid: int) -> dict:
    """Everything the Backup tab and the Host need, in one read.

    Never raises. "I could not tell" is reported as a state, because a status
    surface that 500s teaches the user their backup is broken when the truth
    may be that one catalog query hiccuped.
    """
    out = {"configured": False, "checked_at": _now_iso()}
    if pool is None:
        return {**out, "reason": "brain_not_initialized"}
    try:
        cfg = await get_config(pool, eid)
    except Exception as e:  # noqa: BLE001
        logger.info("mirror status: config unreadable: %s", e)
        return {**out, "reason": "unreadable"}
    if not cfg.get("enabled"):
        # Which machines COULD hold one. Returned from this route on purpose:
        # the Settings app owns the enable control and already holds
        # read:backup_settings, so shipping the choices here means it needs no
        # roster scope and no second call. Only boxes that can actually do it —
        # online, and advertising the capability, which means a container
        # runtime is installed.
        cands = []
        try:
            from server.network import topology
            # Same expiry filter as _bastion() and the reconciler. list_bastions
            # filters revoked_at only, so without this an EXPIRED token shows in
            # the picker as ready and 404s the moment it is clicked — the user
            # spends an authenticator code to be told the machine does not exist.
            live = await pool.fetch(
                "SELECT id FROM mcp_client_tokens WHERE entity_id = $1 "
                "AND operator_kind = 'bastion' AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())", eid)
            live_ids = {int(r["id"]) for r in live}
            for b in await topology.list_bastions(pool, eid):
                if int(b.get("id") or 0) not in live_ids:
                    continue
                caps = b.get("capabilities") or []
                cands.append({
                    "token_id": b.get("id"),
                    "name": b.get("hostname") or b.get("name"),
                    "online": bool(b.get("online")),
                    "capable": MIRROR_CAPABILITY in caps,
                })
        except Exception as e:  # noqa: BLE001
            logger.info("mirror status: roster unreadable: %s", e)
        out["candidates"] = cands
        # Whether the DATABASE could host one at all — the UI must not offer a
        # control that enable() will refuse.
        bounded, detail = await retention_bounded(pool)
        out["can_enable"] = bool(bounded and db_build_info().get("known"))
        if not out["can_enable"]:
            out["blocked_reason"] = (
                "Your host's database needs its next update before it can hold a "
                f"mirror — {detail}." if not bounded else
                "Your host's database container has not reported its version yet.")
        return {**out, "reason": "no_mirror_configured"}

    name = str(cfg.get("slot") or slot_name(int(cfg.get("token_id") or 0)))
    out["configured"] = True
    out["bastion"] = {
        "name": cfg.get("bastion_name"),
        "token_id": cfg.get("token_id"),
        "enabled_at": cfg.get("enabled_at"),
        "last_seen_at": None,
        "online": False,
    }
    # Whether a copy is known to EXIST, separate from whether it is current.
    # The UI needs both: "offline, and there is a good copy over there" and
    # "offline, and it never finished its first one" are different sentences and
    # only one of them is reassuring. seen_streaming_at is set by observe().
    out["copy"] = {
        "exists": bool(cfg.get("seen_streaming_at")),
        "first_seen_at": cfg.get("seen_streaming_at"),
        "reseed_generation": int(cfg.get("reseed_generation") or 1),
        # The bastion's own last word — the only source for states the primary
        # cannot observe (see record_bastion_report).
        "bastion_report": cfg.get("last_report") or None,
        # How the user opens their own copy, in the words of the machine that
        # holds it (its runtime, its container name). Only that box knows, so
        # it is reported rather than reconstructed here and guessed wrong.
        "open_with": (cfg.get("last_report") or {}).get("open_with"),
    }
    try:
        b = await _bastion(pool, eid, int(cfg.get("token_id") or 0))
        out["bastion"]["last_seen_at"] = _iso(b.get("last_seen_at"))
        out["bastion"]["online"] = bool(b.get("online"))
    except Exception:  # noqa: BLE001
        # Revoked out from under us; the reconciler will drop the slot shortly.
        out["bastion"]["revoked"] = True

    slot, slot_read_ok = None, True
    try:
        slot = await pool.fetchrow(
            "SELECT slot_name, active, wal_status, safe_wal_size, restart_lsn "
            "FROM pg_replication_slots WHERE slot_name = $1", name)
    except Exception as e:  # noqa: BLE001
        # A FAILED read is not an absent slot. Conflating them told the user
        # "your mirror must start over" every time one catalog query hiccuped —
        # the most alarming message this surface has, on no evidence.
        slot_read_ok = False
        logger.info("mirror status: slot read failed: %s", e)
    out["slot"] = {
        "name": name,
        "known": slot_read_ok,
        "present": (slot is not None) if slot_read_ok else None,
        "active": bool(slot["active"]) if slot else False,
        "wal_status": (slot["wal_status"] if slot else None),
        "retention_headroom_bytes": (int(slot["safe_wal_size"])
                                     if slot and slot["safe_wal_size"] is not None else None),
    }

    stream = None
    try:
        stream = await pool.fetchrow(
            """
            SELECT application_name, state, sync_state, backend_start,
                   EXTRACT(EPOCH FROM replay_lag)::float8 AS replay_lag_s,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint AS lag_bytes
              FROM pg_stat_replication
             WHERE application_name = $1
             ORDER BY backend_start DESC
             LIMIT 1
            """,
            application_name(str(cfg.get("bastion_name") or "")))
    except Exception as e:  # noqa: BLE001
        logger.info("mirror status: pg_stat_replication read failed: %s", e)
    out["stream"] = {
        "state": (stream["state"] if stream else None),
        "lag_bytes": (int(stream["lag_bytes"]) if stream and stream["lag_bytes"] is not None else None),
        "replay_lag_seconds": (stream["replay_lag_s"] if stream else None),
        "connected_since": _iso(stream["backend_start"]) if stream else None,
    }

    # The pin. The bastion runs THIS build or it does not run at all — a
    # standby cannot replay WAL from a different PostgreSQL.
    try:
        out["primary"] = {
            "database": await db_name(pool),
            "server_version": await pool.fetchval("SHOW server_version"),
            "db_image": db_build_info(),
        }
    except Exception:  # noqa: BLE001
        out["primary"] = {"database": None, "server_version": None,
                          "db_image": db_build_info()}

    out["health"] = _health(out)
    return out


def _health(s: dict) -> str:
    """One word the UI can render without doing its own reasoning.

    Order matters: needs_reseed outranks everything, because a lost slot is
    the one state where the mirror will NOT recover on its own and the user
    has to be told.
    """
    slot, stream = s.get("slot") or {}, s.get("stream") or {}
    # A read we could not complete is UNKNOWN, never a diagnosis. needs_reseed
    # tells the user their copy must start over; saying that because one catalog
    # query failed is the worst kind of wrong this surface can be.
    if slot.get("known") is False:
        return "unknown"
    if slot.get("wal_status") == "lost" or not slot.get("present"):
        return "needs_reseed"
    if stream.get("state") == "streaming":
        lag = stream.get("lag_bytes")
        # One WAL segment. Inside a segment a standby is, for a user's
        # purposes, current; beyond it there is a real backlog worth showing.
        return "streaming" if (lag is not None and lag < 16 * 1024 * 1024) else "catching_up"
    if stream.get("state"):
        return "catching_up"
    return "disconnected"


# ── observation: the write half of status -----------------------------------

async def observe(pool, eid: int) -> dict:
    """Record what status() can only see, and repair what it can only report.

    status() is a READ — it is served from a GET and must stay one. But two
    facts can only be learned by looking, and both change what the user is
    told, so something write-capable has to look. This runs from the mirror
    reconcile loop (reconcile_mirror_loop, every RECONCILE_INTERVAL_S) and
    nowhere else — in particular NOT from the heartbeat, which is a hot path
    per bastion and must not carry a slot recut.

    1. seen_streaming_at. Until a stream has actually been observed, NO copy is
       known to exist on that machine. Without this the UI cannot tell "offline
       with a good copy" from "never finished its first copy", and it was
       saying "the copy on that machine is safe" for both.

    2. reseed_generation. When the primary sheds the slot (past the retention
       bound) the replica can never catch up again — it needs a fresh base
       backup, and a LOST slot cannot be reused for one. Nothing was doing
       this, so needs_reseed was a dead end: the UI promised a recovery that no
       code performed. Here the slot is dropped and recreated and the
       generation is bumped, which is what the bastion watches to know its copy
       is void.
    """
    s = await status(pool, eid)
    if not s.get("configured") or s.get("health") == "unknown":
        return s
    cfg = await get_config(pool, eid)
    if not cfg.get("enabled"):
        return s
    changed = False

    if (s.get("stream") or {}).get("state") == "streaming" and not cfg.get("seen_streaming_at"):
        cfg["seen_streaming_at"] = _now_iso()
        changed = True
        logger.info("mirror: first stream observed on %s — a copy now exists",
                    cfg.get("bastion_name"))

    if s.get("health") == "needs_reseed":
        name = str(cfg.get("slot") or "")
        # Only act once the bastion could actually redo it; re-cutting the slot
        # on every tick while the box is off achieves nothing and churns WAL.
        if name and (s.get("bastion") or {}).get("online"):
            try:
                # Bump ONLY on a real recut. The generation is what makes the
                # bastion throw its copy away, so a drop that keeps failing must
                # not keep incrementing — that would wipe and re-seed the user's
                # copy every 60s while never fixing anything.
                if await _drop_slot(pool, name):
                    await _create_slot(pool, name)
                    cfg["reseed_generation"] = int(cfg.get("reseed_generation") or 1) + 1
                    cfg["seen_streaming_at"] = None
                    changed = True
                    logger.info("mirror: slot %s was lost — recut at generation %s; "
                                "the bastion will take a fresh base backup",
                                name, cfg["reseed_generation"])
                else:
                    logger.warning("mirror: slot %s is lost but could not be dropped — "
                                   "generation NOT bumped; will retry next sweep", name)
            except Exception as e:  # noqa: BLE001
                logger.warning("mirror: could not recut lost slot %s: %s", name, e)

    if changed:
        await _save_mirror_cfg(pool, eid, cfg)
        s = await status(pool, eid)
    return s


async def record_bastion_report(pool, eid: int, token_id: int, report: dict) -> None:
    """Keep the bastion's own view of its replica.

    It is NOT used for lag — pg_stat_replication on the primary is, because it
    is the one source a compromised box cannot make comforting. What this
    carries is the set of states the primary cannot see at all: no container
    runtime, image not downloaded, container dead, version mismatch, and the
    replayed LSN of a replica that is currently disconnected (the primary has
    no walsender row then, so it cannot say how far the copy actually got).

    Two things this must not do, both learned the hard way:

    * It must not read-modify-write the whole behavioral_params blob. That
      column is shared — observe() and set_backup_settings write it too — and a
      heartbeat landing between another writer's read and its write silently
      drops that write. Losing a reseed_generation bump around a recut would
      strand the standby on a slot that no longer exists, which is the exact
      failure the generation was added to prevent. So this writes ONE key with
      jsonb_set and touches nothing else.

    * It must not write on every heartbeat. The replayed LSN advances
      constantly, so keying "changed" on the whole payload would be an UPDATE
      per bastion per minute forever. Only a change in the fields that alter
      what the user is TOLD counts; the LSN rides along when one of those moves,
      and is refreshed at most once a minute by the staleness bound below.
    """
    if not isinstance(report, dict):
        return
    cfg = await get_config(pool, eid)
    if not cfg.get("enabled") or int(cfg.get("token_id") or 0) != int(token_id):
        return

    # Fields whose change is worth a write. replay_lsn is deliberately NOT here.
    meaningful = ("state", "image_tag", "open_with", "container", "runtime", "error")
    prev = cfg.get("last_report") or {}
    keep = {k: report.get(k) for k in meaningful if report.get(k) is not None}
    if report.get("replay_lsn"):
        keep["replay_lsn"] = report["replay_lsn"]

    same = all(keep.get(k) == prev.get(k) for k in meaningful)
    if same:
        # Nothing the user would notice changed. Refresh at most hourly so a
        # long-running healthy mirror still has a recent LSN on record without
        # writing every minute.
        try:
            from datetime import datetime, timedelta, timezone
            at = datetime.fromisoformat(str(prev.get("at") or ""))
            if datetime.now(timezone.utc) - at < timedelta(hours=1):
                return
        except Exception:  # noqa: BLE001
            pass
    keep["at"] = _now_iso()
    # Single-key write: never clobbers a concurrent writer of another key.
    await pool.execute(
        "UPDATE entity_registry "
        "SET behavioral_params = jsonb_set("
        "      COALESCE(behavioral_params, '{}'::jsonb),"
        "      '{backup_mirror,last_report}', $1::jsonb, true) "
        "WHERE id = $2 AND behavioral_params ? 'backup_mirror'",
        json.dumps(keep), eid)


# ── reconcile ----------------------------------------------------------------

async def reconcile_once(pool, eid: int) -> None:
    """Drop mirror slots that no longer have a live, enabled bastion behind
    them. Same shape and the same reason as hub_peers._converge_once: a slot
    is a resource held on behalf of a bearer, so it must die with the bearer.

    Only slots matching mirror_b<digits> are ever considered — a slot created
    by anything else is not this module's to reap.
    """
    try:
        rows = await pool.fetch(
            "SELECT slot_name FROM pg_replication_slots WHERE slot_name LIKE $1",
            _SLOT_PREFIX + "%")
    except Exception as e:  # noqa: BLE001
        logger.debug("mirror reconcile: slot scan failed: %s", e)
        return
    if not rows:
        return

    cfg = await get_config(pool, eid)
    wanted = str(cfg.get("slot") or "") if cfg.get("enabled") else ""
    live_ids = set()
    if wanted:
        try:
            rows_live = await pool.fetch(
                "SELECT id FROM mcp_client_tokens WHERE entity_id = $1 "
                "AND operator_kind = 'bastion' AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())", eid)
            live_ids = {int(r["id"]) for r in rows_live}
        except Exception as e:  # noqa: BLE001
            # Cannot prove a bastion is gone -> change nothing. Dropping a slot
            # on an unreadable roster would destroy a healthy mirror over a
            # transient error.
            logger.debug("mirror reconcile: roster unreadable, no action: %s", e)
            return

    for r in rows:
        name = r["slot_name"]
        m = _SLOT_RE.match(name)
        if not m:
            continue
        if name == wanted and int(m.group(1)) in live_ids:
            continue
        reason = "mirror disabled" if not wanted else "bastion revoked"
        logger.info("mirror reconcile: dropping slot %s (%s)", name, reason)
        await _drop_slot(pool, name)
        if name == wanted:
            await _save_mirror_cfg(pool, eid, None)


async def reconcile_mirror_loop(pool, eid: int) -> None:
    """Forever-loop, dispatched at lifespan like the other guarded background
    tasks. Never raises out: a mirror bookkeeping error must not take down the
    entity's boot or its event loop."""
    logger.info("mirror reconciler online — entity %d, every %ds",
                eid, RECONCILE_INTERVAL_S)
    # The NAC reaper shares this 60s cadence and is dispatched from the same
    # lifespan block, so without a phase offset the two wake together on every
    # cycle for the life of the pod. Cadence unchanged.
    from server.loop_splay import splay
    await splay(RECONCILE_INTERVAL_S, "mirror_reconcile")
    while True:
        try:
            await asyncio.sleep(RECONCILE_INTERVAL_S)
            await reconcile_once(pool, eid)
            # observe() is the write half of status: it records the first
            # observed stream and recuts a slot the primary has shed. Runs
            # AFTER reconcile so a slot belonging to a revoked bastion is
            # already gone and is never recut for a box that cannot connect.
            await observe(pool, eid)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("mirror reconcile cycle error (non-fatal): %s", e)
