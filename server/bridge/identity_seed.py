"""Boot-time identity seed: a new Host writes its own first-run identity.

init-db.sql creates the Host's entity_registry row as a PLACEHOLDER (display
name 'Host', all-1.0 weights, no directive), which entrypoint.sh renumbers to
$ENTITY_ID. A Host left on that row runs generic: no response_style (which
zeroes the chat tool surface in session.py, so the Host narrates tool calls it
cannot make), no personality and no tools grant.

At boot, when the row is still the placeholder, this writes the first-run
identity (server/local_identity.py) through the same guarded path genesis
uses (routes.genesis._seed_codebook: the Invariant-9 shape gate and audit rows).

Invariants:
  - Seeds ONLY while the row is the placeholder (_is_placeholder). A Host in
    use is never overwritten.
  - Behavioral keys written locally before the seed (model routing, engine
    source) survive: the first-run values are merged UNDER them.
  - Fail-LOUD: state is exported to /api/system/health/deep as the
    "identity" check, and a failure logs IDENTITY_PLACEHOLDER at ERROR.
  - A turn never runs toolless while this is still in flight: the seed is
    what writes the tools grant (permission_overrides.bot_capabilities), so
    a turn whose tools gate is closed while the seed is unfinished holds for
    it (wait_for_identity, called from session.send_message through
    runtime/identity_hold.py) instead of answering as a chat-only Host.
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("privatae.pc.identity_seed")

# Must mirror init-db.sql's placeholder row exactly. The seed name became
# "Host" on 2026-09-22 (the word is Host, never entity); pods initialised before
# that still hold "Entity" and must still be recognised, so both are accepted.
PLACEHOLDER_NAME = "Host"
PLACEHOLDER_NAMES = frozenset({"Host", "Entity"})
PLACEHOLDER_WEIGHTS = {
    "warmth": 1.0, "directness": 1.0, "humor": 1.0, "formality": 1.0,
    "curiosity": 1.0, "assertiveness": 1.0, "patience": 1.0,
    "vulnerability": 1.0, "energy": 1.0, "depth": 1.0,
}

# The longest a TURN holds for the seed (wait_for_identity). The seed is a few
# local writes: 8 ms from IDENTITY_PLACEHOLDER to IDENTITY_SEEDED on a fresh
# compose boot (2026-10-15). The bound also covers the first two retries of a
# boot read that failed (_READ_RETRY_BASE_S: 2 s, then 4 s). Past it the turn
# stops holding and the person is told why instead of waiting longer.
TURN_WAIT_S = 10.0

# Exported to /api/system/health/deep. status:
#   unknown     — the seed was never dispatched in this process (not a pod
#                 boot: an offline harness or script)
#   checking    — dispatched, reading the local row
#   genesis     — ENTITY_ID=0, nothing to seed
#   ok          — real identity present (either already, or after a seed)
#   placeholder — detected, first-run write in progress
#   failed      — the first-run write failed; `retrying` is False (nothing
#                 tries a local write again), True only while the boot read
#                 of the row is being retried
# `started` / `done` count write attempts begun and finished, so a waiter
# can tell when an attempt has come back.
#
# `placeholder_seen` is True only once the seed has READ this Host's row and
# found the init-db placeholder, i.e. only when it KNOWS a grant is still to
# come. A crash before that read leaves it False and sets `read_failed`: the
# state is then "unknown", the read is retried, and a turn is never refused on
# the strength of it (a chat-only Host whose boot read hit a transient DB error
# must keep answering). `began` is when this process started the seed — the
# Host's uptime, for wording.
SEED_STATE: dict[str, Any] = {"status": "unknown", "detail": "", "ts": None,
                              "entity_id": None, "retrying": False,
                              "started": 0, "done": 0,
                              "placeholder_seen": False, "read_failed": False,
                              "began": None}

# Between retries of the boot READ of the identity row (first failure waits the
# base, doubling to the cap). Short: the read is one local query.
_READ_RETRY_BASE_S = 2
_READ_RETRY_CAP_S = 30

def _jsonb(v: Any) -> Optional[dict]:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


async def _fetch_row(pool, entity_id: int):
    return await pool.fetchrow(
        "SELECT display_name, seed_weights, behavioral_params, current_weights, "
        "master_directive FROM entity_registry WHERE id = $1",
        entity_id,
    )


def _is_placeholder(row) -> bool:
    """True only for the untouched init-db placeholder. Conservative on
    purpose: all three signals must agree, so a user who merely renamed
    their host 'Host' or 'Entity' (real weights, real behavioral) is never reseeded."""
    if row is None:
        return False
    if (row["display_name"] or "").strip() not in PLACEHOLDER_NAMES:
        return False
    if _jsonb(row["seed_weights"]) != PLACEHOLDER_WEIGHTS:
        return False
    bp = _jsonb(row["behavioral_params"]) or {}
    if "response_style" in bp:
        return False
    # The first-run identity (server/local_identity.py) starts from these same
    # neutral weights, so seed_weights cannot tell a Host in use from the
    # placeholder. A Host whose personality has moved off them, or that has a
    # directive, is in use whatever its name is: a reseed would overwrite
    # both. An older caller's row without these columns is judged as before.
    cw = _jsonb(row.get("current_weights"))
    if cw not in (None, {}, PLACEHOLDER_WEIGHTS):
        return False
    return not (row.get("master_directive") or "").strip()


def _set_state(status: str, detail: str = "", *, retrying: bool = False) -> None:
    SEED_STATE.update({"status": status, "detail": detail, "ts": time.time(),
                       "retrying": retrying})


def identity_unresolved(entity_id: int) -> bool:
    """True when THIS process's boot seed for `entity_id` is still reading the
    row (including while that read is being retried after a failure), or has
    read it and found the placeholder (and not yet imported) — i.e. the tools
    grant may still be on its way. False for an entity the seed is not about,
    for a seed that never ran here (status 'unknown'), once it is 'ok' or
    'genesis', and for a 'failed' seed that never saw the placeholder (no grant
    is known to be owed). A turn WAITS on the first two states; whether it may
    be REFUSED is a separate rule (the placeholder must have been seen)."""
    if SEED_STATE.get("entity_id") != entity_id:
        return False
    st = SEED_STATE.get("status")
    if st == "checking":
        return True
    if st in ("placeholder", "failed"):
        return bool(SEED_STATE.get("placeholder_seen"))
    return False


def host_uptime_s() -> Optional[float]:
    """Seconds since this process started its identity seed, or None."""
    began = SEED_STATE.get("began")
    return None if began is None else max(0.0, time.time() - float(began))


async def wait_for_identity(timeout: Optional[float] = None,
                            poll_s: float = 0.1) -> dict:
    """Hold until the boot seed resolves, for a turn that must not run
    toolless meanwhile.

    Returns as soon as one of these is true: the identity is real
    ('ok'/'genesis'); the seed has failed for good; an attempt that began
    after this call has come back failed; or `timeout` (default TURN_WAIT_S)
    has passed. Returns a snapshot of SEED_STATE — the caller decides from it
    whether the turn may run.
    """
    if timeout is None:
        timeout = TURN_WAIT_S
    target = int(SEED_STATE.get("started") or 0) + 1
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        st = SEED_STATE.get("status")
        if st in ("ok", "genesis"):
            break
        if st == "failed" and not SEED_STATE.get("retrying"):
            break
        if st == "failed" and int(SEED_STATE.get("done") or 0) >= target:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(poll_s)
    return dict(SEED_STATE)


async def run_identity_seed(entity_id: int) -> None:
    """Detect-and-write, dispatched once from the server lifespan.

    Returns quietly when there is nothing to do; otherwise writes the
    first-run identity once. A failure to READ the row is retried with capped
    backoff. Never raises into the lifespan.
    """
    SEED_STATE.update({"entity_id": entity_id, "placeholder_seen": False,
                       "read_failed": False, "began": time.time()})
    _set_state("checking", "reading this Host's identity row", retrying=True)
    read_tries = 0
    while True:
        try:
            await _run(entity_id)
            await _release_waiting(entity_id)
            return
        except Exception as exc:  # the loop itself must never kill the server
            if SEED_STATE.get("placeholder_seen"):
                # The placeholder was seen, so a grant IS owed and the seed
                # has now died without delivering it: failed, loudly.
                _set_state("failed", f"identity seed loop crashed: {exc}")
                logger.error("IDENTITY_PLACEHOLDER eid=%s — seed loop crashed: %s",
                             entity_id, exc, exc_info=True)
                return
            # The crash came BEFORE the row was read (a DB error, no pool):
            # nothing is known about this Host yet, so this is not "failed
            # and owed a grant". Retry the read.
            read_tries += 1
            SEED_STATE["read_failed"] = True
            _set_state("checking", f"reading the identity row failed, retrying: {exc}",
                       retrying=True)
            logger.error("IDENTITY_PLACEHOLDER eid=%s — could not read the identity "
                         "row (try %d), retrying: %s", entity_id, read_tries, exc,
                         exc_info=True)
            await asyncio.sleep(min(_READ_RETRY_BASE_S * (2 ** (read_tries - 1)),
                                    _READ_RETRY_CAP_S))
            SEED_STATE["read_failed"] = False


async def _release_waiting(entity_id: int) -> None:
    """The identity resolved: requests a person sent before the tools arrived
    (saved by runtime/identity_hold.py) become due now and the scheduled-task
    runner polls now. Never raises."""
    if SEED_STATE.get("status") not in ("ok", "genesis"):
        return
    try:
        from server import brain_service
        from mastercode.brain import offline_task_runner as otr
        pool = brain_service.get_pool()
        if pool is not None:
            await otr.release_saved_requests(pool, entity_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("identity seed: releasing saved requests failed (non-fatal): %s", exc)


async def _run(entity_id: int) -> None:
    from server import brain_service

    if entity_id <= 0:
        _set_state("genesis", "genesis mode — no identity to seed")
        return

    pool = brain_service.get_pool()
    if pool is None:
        # Before the row was read, so "unknown": raised into the read-retry
        # in run_identity_seed rather than recorded as a failed seed.
        raise RuntimeError("no DB pool at boot — cannot check identity")

    row = await _fetch_row(pool, entity_id)
    if row is None:
        # entrypoint renumbering failed entirely — worth screaming about,
        # but there is nothing to seed onto.
        _set_state("failed", "no entity_registry row for this ENTITY_ID")
        logger.error("IDENTITY_PLACEHOLDER eid=%s — no entity_registry row",
                     entity_id)
        return
    if not _is_placeholder(row):
        _set_state("ok", "identity already seeded")
        return

    SEED_STATE["placeholder_seen"] = True
    SEED_STATE["read_failed"] = False
    _set_state("placeholder", "placeholder row detected — writing the first-run identity",
               retrying=True)
    logger.warning(
        "IDENTITY_PLACEHOLDER eid=%s — entity_registry is the init-db "
        "placeholder; writing the first-run identity", entity_id,
    )

    # A standalone Host has no platform to import an identity from, so it
    # writes its own (server/local_identity.py) through the same guarded
    # seeder. One attempt: it is a local write, and a failure here is a bug
    # to report, not a network blip to wait out.
    from server.local_identity import first_run_bundle
    SEED_STATE["started"] = int(SEED_STATE.get("started") or 0) + 1
    try:
        seeded = await _seed(pool, entity_id, first_run_bundle())
    except Exception as exc:
        seeded = False
        logger.error("IDENTITY_PLACEHOLDER eid=%s — first-run identity failed: %s",
                     entity_id, exc, exc_info=True)
        _set_state("failed", f"first-run identity failed: {exc}")
    finally:
        SEED_STATE["done"] = int(SEED_STATE.get("done") or 0) + 1
    if not seeded:
        # Nothing retries a local write, so the state must not say it will.
        detail = (SEED_STATE.get("detail") if SEED_STATE.get("status") == "failed"
                  else "first-run identity did not replace the placeholder row")
        _set_state("failed", detail, retrying=False)


async def _seed(pool, entity_id: int, data: dict) -> bool:
    """One write: re-check placeholder, merge behavioral, run the guarded
    genesis seeder, verify. Returns True when the row stops being the
    placeholder."""
    # Re-check: another writer (a manual reseed, a second worker) may have
    # replaced the placeholder since _run read it.
    row = await _fetch_row(pool, entity_id)
    if not _is_placeholder(row):
        _set_state("ok", "identity seeded by another writer")
        return True

    codebook = dict(data.get("codebook") or {})
    display_name = (data.get("display_name") or "").strip()
    for k in ("seed_weights", "current_weights", "behavioral_params"):
        v = codebook.get(k)
        if isinstance(v, str):
            codebook[k] = _jsonb(v)

    # Merge the first-run behavioral UNDER local: Orchestrator keys written
    # before the seed (model routing, engine source) must survive it.
    local_bp = _jsonb(row["behavioral_params"]) or {}
    first_run_bp = codebook.get("behavioral_params") or {}
    codebook["behavioral_params"] = {**first_run_bp, **local_bp}

    from server.routes.genesis import _seed_codebook
    await _seed_codebook(entity_id, display_name, codebook)

    # Third hole of the same class (2026-07-19): the chat tools gate reads
    # permission_overrides.bot_capabilities (session.py) and a NULL reads as
    # enrollment-tier chat-only — so tools stayed dead even after a codebook
    # seed. The first-run bundle carries the grant; apply it ONLY when the
    # local column is still NULL (never clobber a live grant set).
    po = codebook.get("permission_overrides")
    if isinstance(po, str):
        po = _jsonb(po)
    if po:
        await pool.execute(
            "UPDATE entity_registry SET permission_overrides = $2::jsonb "
            "WHERE id = $1 AND permission_overrides IS NULL",
            entity_id, json.dumps(po),
        )

    row = await _fetch_row(pool, entity_id)
    if _is_placeholder(row):
        # _seed_codebook swallows its own failures (guard denial, SQL error)
        # — surface that here instead of reporting a phantom success.
        _set_state("failed", "seed ran but the row is still the placeholder "
                             "(guard denial or bundle itself is a skeleton?)")
        logger.error(
            "IDENTITY_PLACEHOLDER eid=%s — _seed_codebook ran but the row is "
            "still the placeholder; refusing to report ok", entity_id,
        )
        return False

    _set_state("ok", f"first-run identity written (display_name={display_name!r})")
    logger.warning("IDENTITY_SEEDED eid=%s display_name=%r — first-run "
                   "identity written at boot", entity_id, display_name)
    return True
