"""
The Host backs itself up, and nothing on the platform can open the backup.

If the platform is lost, people's data is still safe. There is no point in a
user controlling their own keys if a platform breach compromises all their
data. So:

  * The Host takes the backup inside its own container: its database
    (pg_dump), its apps tree (/var/lib/cseries, with the excludes below), the
    Git repositories it owns (mirror clones with its own Gitea credential),
    and, on a machine with the local runtime, each app's own data: the kept
    data volume the runtime keeps outside the Host's container for every
    installed app and every removed one whose data the owner kept, as
    podman's `volume export` writes it (the runtime streams it,
    _add_app_data).
  * They leave as ONE file, streamed with nothing written to disk:
        tar (db chunks, apps/, app-data/, gitea-repos/, manifest.json, README.txt)
          | zstd | age -r <recipients>  -> upload in parts
    README.txt inside the file says how it is laid out and how to open it.
  * THE RULE: the platform holds a backup OR a key, never both. The file is
    encrypted with age to EXACTLY the Host recovery key (made in the owner's
    browser; required) and any bastion keys (server/backup_key.py), plus a
    fresh key made for this backup alone. The per-backup key's private half
    goes into a small sidecar (<stamp>.key.age), sealed to the same keys, and
    is then dropped: once a run ends, this Host holds nothing that opens any
    backup. The sidecar exists so a restore never needs a recovery or bastion
    key inside a pod: the owner's browser (with the recovery key) or their
    bastion opens the sidecar and seals the per-backup key again to the Host
    doing the restore (server/host_restore.py).
  * Without a saved recovery key there is nobody to encrypt to, so no backup
    is written; the run says so ("no_recipient") and Settings says where to
    save one. The owner makes it in the Backups app on this Host
    (make_own_recovery_key: their browser makes the key pair and sends only
    the public half), or confirms one their account page offers
    (adopt_recovery_offer). A key made here stays the active one until the
    owner replaces it: an offer never displaces it.

WHERE IT GOES: the place the owner chose in Settings, Backup (destination_of),
else the network's storage when it takes this Host's backups, and a folder on
this machine otherwise. Platform storage always takes them; a network member's
door takes them only once it answers the backup ops (network_backups), and a
standalone Host has no network.
  * The network's storage (the platform's, or the member door's for a network
    member) holds ciphertext only. It gives the Host upload-only URLs for
    exactly one object at a time in its own folder (bridge ops backup.upload.*,
    backup.put_url): the Host can add a backup, never read another Host's,
    never delete one of its retained backups. Retention runs there over object
    names, which needs no key. When the user turned that storage off, the file
    is staged there instead (s/), the second copy (their bastion, or their own
    S3) is sent from it by server/backup_ship.py, and then the staged copy is
    dropped.
  * The owner's own S3-compatible bucket, or a folder on this machine
    (LOCAL_DIR), take the same stream directly (server/backup_stores.py); the
    folder keeps the newest local_keep() backups. A Host told to use the
    network while it is not on one writes nothing anywhere else and says so
    (STATE_NO_DESTINATION).
The run is reported to the network (backup.report) only by a Host that is on
it; a standalone Host tells nobody.

WHEN. On the schedule in Settings (default daily; a Host that never set one
runs in the 02 UTC hour, spread by id), catching up after a wake if it slept
through its time. And once more when the Host goes quiet after being used,
so what changed in that session is backed up before the idle freeze puts it
to sleep. While a run is going it tells the platform it is busy every minute,
which keeps the freeze away until it finishes.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import errno
import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat as _stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from server import age_keys

log = logging.getLogger("privatae.pc.host_backup")

AGE = os.environ.get("AGE_BIN", "/usr/local/bin/age")
PG_DUMP = os.environ.get("PG_DUMP_BIN", "pg_dump")
APPS_ROOT = "/var/lib/cseries"
PG_SOCKET = "/run/postgresql"
PG_USER = "entity"
# The run's scratch: the Git mirrors. Emptied before and after every run
# (runs are serialized by _run_lock).
WORKDIR = Path("/tmp/host-backup")

FORMAT = "privatae-backup/1"
KIND_BACKUP = "backup"          # retained in platform storage (b/)
KIND_STAGED = "staged"          # staged for a second copy (s/), then dropped
KIND_EXPORT = "export"          # an export the user asked for (exports/)
SIDECAR_KIND = {KIND_BACKUP: "sidecar", KIND_STAGED: "staged_sidecar"}

# Where backups go (destination_of), and who this Host is (host_mode).
WHERE_NETWORK, WHERE_S3, WHERE_LOCAL = "network", "s3", "local"
WHERES = (WHERE_NETWORK, WHERE_S3, WHERE_LOCAL)
MODE_PLATFORM, MODE_MEMBER, MODE_STANDALONE = "platform", "member", "standalone"
# The folder on this machine. Inside the apps volume by default, which every
# install has, and left out of the backups it holds (APPS_EXCLUDES); an
# install that mounts a volume of its own for it names it here.
LOCAL_DIR = Path(os.environ.get("CSERIES_BACKUP_DIR") or f"{APPS_ROOT}/.host-backups")
LOCAL_KEEP_DEFAULT = 7          # a week of daily backups
LOCAL_KEEP_MAX = 90
# tar needs a member's size before its bytes, and pg_dump's output length is
# not known up front, so the dump is cut into members of at most this size.
DB_CHUNK = 16 * 1024 * 1024
# One upload part. S3 needs >= 5 MiB for every part but the last and allows
# 10,000 parts: 16 MiB parts cover 156 GiB. Also the most the platform signs a
# part URL for (privatae host_backup_store.py PART_MAX): each part's URL is
# signed for its exact size, which the Host declares. The writer holds one part and one
# database piece at a time, each in a buffer it reuses: ~32 MiB, measured by
# scripts/rig_host_backup.sh.
PART_BYTES = 16 * 1024 * 1024
PART_TRIES = 3
# How long age may take to exit once its output has all been read.
AGE_EXIT_WAIT_S = 120
STAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")

# The old platform backup job's excludes.
# Anchored paths start with the apps root; bare names match that name at any
# depth. Core dumps by NAME only as core.<digits>: a bare "core" would drop
# real files (jsonschema's vocabularies/core, @babel/core).
APPS_EXCLUDES = (
    # A restore's staged copy (server/host_restore.py), never itself backed up.
    f"{APPS_ROOT}/.host-restore",
    # The backups kept on this machine (LOCAL_DIR): a backup never holds the
    # backups before it, and a restore leaves them where they are.
    f"{APPS_ROOT}/.host-backups",
    f"{APPS_ROOT}/host-sdk/scratch",
    f"{APPS_ROOT}/host-sdk/python-libs",
    f"{APPS_ROOT}/.vscode-server",
    f"{APPS_ROOT}/crash",
    "node_modules",
    ".venv",
    ".cache",
    "core.[0-9]*",
)

LOOP_SECONDS = 60
QUIET_AFTER = dt.timedelta(minutes=10)     # under the 15-minute idle freeze
QUIET_MIN_SPACING = dt.timedelta(hours=1)  # a busy day does not back up hourly
RETRY_AFTER = dt.timedelta(minutes=30)     # after a failed run
# After a run refused for holding more files than one backup can list, the
# retry waits RETRY_AFTER doubled for each such refusal in a row, up to this:
# the files do not go away by themselves, and each retry walks every file and
# mirrors every Git repository before it can tell. "Back up now" is not held,
# and a Back up now whose count passes (the files are gone) ends the back-off:
# the retries after it wait RETRY_AFTER again (_run).
TOO_MANY_FILES_RETRY_MAX = dt.timedelta(hours=24)
RUN_TIMEOUT_S = 4 * 3600
# The whole run, database, apps and Git together, is bounded below the
# platform's six hours for an unfinished upload (after which it drops the
# parts): past this the run stops, its processes are killed and the upload is
# aborted, never left half-done.
RUN_LIMIT_S = 5 * 3600
# The largest manifest.json a backup may carry, and a restore will read (it is
# parsed whole, host_restore.unpack, before anything in the file is checked).
# A manifest entry is about 170 bytes (measured on host_backup._add_member's
# entries with 60-character paths), so this lists about 390,000 files and
# Git objects; parsing one this size took 0.14 s and about 320 MiB at peak.
# A Host with more files than that is refused a backup, loudly, rather than
# given one its restore would refuse.
MANIFEST_MAX = 64 * 1024 * 1024
BUSY_EVERY_S = 60
# A Host's very first backup waits id % 10 minutes after the kernel starts, so
# the roll that ships this does not start every Host's first upload at once.
# Ten and not more: a metered Host stays up at least 15 minutes after it was
# last used (the idle freeze), so the first backup always fits.
FIRST_BACKUP_SPREAD_MINUTES = 10
# The loop's other duty, counted in loop ticks: delete this Host's own
# week-old exports once a day. (Exports are built only when the owner confirms
# one in Settings: server/host_export.py confirm.)
EXPIRE_TICKS = 24 * 60

STATE_OK, STATE_PARTIAL, STATE_FAILED = "ok", "partial", "failed"
STATE_NO_RECIPIENT = "no_recipient"
# Told to back up to the network's storage while not on the network: nothing
# is written anywhere, once per scheduled time, like STATE_NO_RECIPIENT.
STATE_NO_DESTINATION = "no_destination"
# The run states that record a backup not attempted.
NOT_ATTEMPTED = (STATE_NO_RECIPIENT, STATE_NO_DESTINATION)

_run_lock = asyncio.Lock()
_last_request_at: Optional[dt.datetime] = None


class BackupError(Exception):
    """A run's own failure, with a sentence for the user."""


class TooManyFiles(BackupError):
    """More files than one backup can list (MANIFEST_MAX): `count`, as far as
    the walk got. Recorded on the run (parts.too_many_files), so the next
    retry backs off (retry_after)."""

    def __init__(self, count: int):
        super().__init__(_too_many_files(count))
        self.count = int(count)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp_of(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── when a person last used this Host ────────────────────────────────────────

def note_request(path: str) -> None:
    """Stamp a request, for the backup the Host takes when it goes quiet.
    Called by the kernel's HTTP middleware. The readiness and liveness probes
    hit /api/system/health* every few seconds and are not a person."""
    global _last_request_at
    if path.startswith("/api/system/health"):
        return
    _last_request_at = _now()


# ── the schedule (pure, so the gate can drive it) ────────────────────────────

def schedule_of(bp: dict, entity_id: int) -> Optional[dict]:
    """The Host's backup schedule from behavioral_params, or None when off.

    Same fields and defaults as server/routes/backup.py (DEFAULTS, _effective):
    a Host that never set a schedule keeps the fleet default, the 02 UTC hour,
    spread over the hour by id so every Host does not start at once. A set
    schedule is in the user's own timezone."""
    stored = (bp or {}).get("backup") or {}
    if not isinstance(stored, dict):
        stored = {}
    if str(stored.get("frequency", "daily")).lower() == "off":
        return None
    is_default = not any(k in stored for k in ("frequency", "hour", "minute", "weekday", "target"))
    if is_default:
        return {"frequency": "daily", "hour": 2, "minute": entity_id % 60,
                "weekday": 0, "tz": "UTC"}
    return {
        "frequency": "weekly" if str(stored.get("frequency")).lower() == "weekly" else "daily",
        "hour": int(stored.get("hour", 2)),
        "minute": int(stored.get("minute", 30)),
        "weekday": int(stored.get("weekday", 0)),
        "tz": (bp or {}).get("timezone") or "UTC",
    }


def last_slot(sched: dict, now: dt.datetime) -> dt.datetime:
    """The most recent scheduled moment at or before `now`, in UTC."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(sched["tz"])
    except Exception:
        tz = dt.timezone.utc
    local = now.astimezone(tz)
    slot = local.replace(hour=sched["hour"], minute=sched["minute"], second=0, microsecond=0)
    if sched["frequency"] == "weekly":
        slot -= dt.timedelta(days=(slot.weekday() - sched["weekday"]) % 7)
        if slot > local:
            slot -= dt.timedelta(days=7)
    elif slot > local:
        slot -= dt.timedelta(days=1)
    return slot.astimezone(dt.timezone.utc)


def first_backup_waits(entity_id: int, booted: dt.datetime, now: dt.datetime) -> bool:
    """True while a Host that has never backed up is still inside its slot of
    the first-backup spread (FIRST_BACKUP_SPREAD_MINUTES)."""
    return now - booted < dt.timedelta(minutes=entity_id % FIRST_BACKUP_SPREAD_MINUTES)


def retry_after(recent: list) -> dt.timedelta:
    """How long after a failed run the next one waits. `recent`: the latest
    finished runs, newest first, each with its `parts` (a dict or its JSON).
    RETRY_AFTER, or, after runs refused for too many files in a row, RETRY_AFTER
    doubled for each of them, up to TOO_MANY_FILES_RETRY_MAX."""
    refused = 0
    for r in recent:
        parts = r.get("parts") if isinstance(r, dict) else r["parts"]
        if isinstance(parts, str):
            try:
                parts = json.loads(parts)
            except ValueError:
                parts = None
        if not (isinstance(parts, dict) and parts.get("too_many_files")):
            break
        refused += 1
    if not refused:
        return RETRY_AFTER
    return min(RETRY_AFTER * 2 ** min(refused, 16), TOO_MANY_FILES_RETRY_MAX)


def due(sched: Optional[dict], now: dt.datetime, *, last_ok: Optional[dt.datetime],
        last_attempt: Optional[dt.datetime], last_failed: bool,
        last_request: Optional[dt.datetime],
        retry: dt.timedelta = RETRY_AFTER) -> Optional[str]:
    """Why a backup should run now, or None. The reasons are recorded per run.

    - schedule / catch-up: the last scheduled moment has no successful backup
      after it. "catch-up" when that moment is over an hour old, i.e. the Host
      was asleep or down when it came round.
    - quiet: someone used the Host since its last backup, has not for
      QUIET_AFTER, and the last backup is at least QUIET_MIN_SPACING old.
    A failed run is retried after `retry` (RETRY_AFTER, or longer after
    refusals for too many files: retry_after), never in a tight loop."""
    if sched is None:
        return None
    if last_failed and last_attempt and now - last_attempt < retry:
        return None
    slot = last_slot(sched, now)
    if last_ok is None or last_ok < slot:
        if last_attempt is None or last_attempt < slot or last_failed:
            return "catch-up" if now - slot > dt.timedelta(hours=1) else "schedule"
    if (last_request is not None and (last_ok is None or last_request > last_ok)
            and now - last_request >= QUIET_AFTER
            and (last_ok is None or now - last_ok >= QUIET_MIN_SPACING)):
        return "quiet"
    return None


# A name that could read as more than one line, or out of order, wherever it is
# shown (Settings lists runs with white-space: pre-line): C0 and C1 control
# characters, DEL, the Unicode line and paragraph separators, and the bidi
# controls: the embeddings, overrides and isolates (U+202A-202E, U+2066-2069)
# and the marks (U+200E, U+200F, U+061C), which reorder the text around them
# as well. Linux allows them in file names; a backup leaves such a name out,
# and a restore refuses a backup that holds one (host_restore.unpack), so a
# crafted backup cannot write lines of its own into what the owner reads.
# Settings' app.js oneLine() replaces the same set.
_UNSHOWABLE = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e"
                         "\u2066-\u2069]")


def unshowable(name: str) -> bool:
    return bool(_UNSHOWABLE.search(name))


# What shown() rewrites: the unshowable set, and the backslash its codes use.
_SHOWN_ESCAPES = re.compile(_UNSHOWABLE.pattern[:-1] + r"\\]")


def shown(name: str) -> str:
    """`name` with every unshowable character written as its code (\\n,
    \\u2028), so it can be shown on one line and found. A backslash is
    doubled, so what is shown reads back to one name only."""
    def esc(m):
        c = m.group(0)
        if c == "\\":
            return "\\\\"
        o = ord(c)
        return {10: "\\n", 13: "\\r", 9: "\\t"}.get(o) or (f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}")
    return _SHOWN_ESCAPES.sub(esc, name)


def excluded(rel: str) -> bool:
    """A path under the apps root (relative, '/'-separated) that backups leave
    out. The restore uses the same test to leave those paths where they are."""
    parts = rel.split("/")
    for pattern in APPS_EXCLUDES:
        if pattern.startswith(APPS_ROOT + "/"):
            anchored = pattern[len(APPS_ROOT) + 1:]
            if rel == anchored or rel.startswith(anchored + "/"):
                return True
        elif any(fnmatch.fnmatchcase(p, pattern) for p in parts):
            return True
    return False


# ── the user's choices ───────────────────────────────────────────────────────

async def _behavioral_params(pool, eid: int) -> dict:
    bp = await pool.fetchval("SELECT behavioral_params FROM entity_registry WHERE id = $1", eid)
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except ValueError:
            bp = {}
    return bp or {}


def _stored(bp: dict) -> dict:
    s = (bp or {}).get("backup") or {}
    return s if isinstance(s, dict) else {}


def platform_on(bp: dict) -> bool:
    """Keep backups in platform storage (ciphertext only). On unless turned off."""
    return bool(_stored(bp).get("platform", True))


_LOOK_UP = object()


def host_mode(bc: Any = _LOOK_UP) -> str:
    """Who this Host is to its backups: on the platform (its cloud bridge), a
    network member (the member door's client), or standalone (NoPlatform, or
    no client yet). `bc` is the client to judge; by default the current one."""
    if bc is _LOOK_UP:
        # The connected client, else the one there is (a member whose door is
        # down for a moment is still a member).
        bc = _bridge() or _raw_bridge()
    if bc is None:
        return MODE_STANDALONE
    from server.bridge.no_platform import NoPlatform
    if isinstance(bc, NoPlatform):
        return MODE_STANDALONE
    if getattr(bc, "network_member", False):
        return MODE_MEMBER
    return MODE_PLATFORM


# Whether the network's storage takes this Host's backups (network_backups).
# Platform storage always does. The member door reports nothing about what it
# serves, so a member Host asks it the one backup op that changes nothing,
# backup.list, and reads the answer:
#   * answered: the door keeps backups;
#   * code 400, which is what a door without the backup ops answers for any op
#     it does not have ("Unknown operation: backup.list"), or code 503, a door
#     that has them and no storage behind them: it does not;
#   * no answer from the door (not connected, a timeout) or any other error:
#     nothing is learned, and the last answer stands. Before the first answer
#     the network's storage is not offered.
# Asked again NETWORK_ASK_S after the last answer, so a door that starts
# keeping backups is seen within that time, and a Host that joins another
# network (a new client) asks again at once. An ask that learned nothing is
# not repeated for NETWORK_RETRY_S: a door that answers its heartbeat while
# this one question hangs (the storage behind it does) would otherwise hold
# every look up for the ask's whole limit, and Settings looks several times
# as it opens (GET /api/backup/runs alone twice). One ask is out at a time; a
# look that arrives meanwhile waits for it and reads its answer.
NETWORK_ASK_S = 600.0
NETWORK_RETRY_S = 60.0
# Settings, Backup waits on the question when it opens, so it is not left to
# the bridge's own 30-second request timeout: a door that is there lists one
# Host's folder in well under a second.
NETWORK_ASK_TIMEOUT_S = 10.0
NETWORK_NOT_KEPT = frozenset({400, 503})
# `for_s`: how long the ask made `at` stands (NETWORK_ASK_S after an answer,
# NETWORK_RETRY_S after none).
_network_answer: dict = {"client": None, "at": None, "offers": None, "for_s": NETWORK_ASK_S}
_network_asking: list = [None, None]     # [the event loop, its lock]


def _network_lock() -> asyncio.Lock:
    """The one-ask-at-a-time lock, made for the running event loop (a lock
    belongs to the loop it first waits in)."""
    loop = asyncio.get_running_loop()
    if _network_asking[0] is not loop:
        _network_asking[:] = [loop, asyncio.Lock()]
    return _network_asking[1]


async def network_backups(mode: Optional[str] = None) -> Optional[bool]:
    """Whether the network's storage takes this Host's backups: True on the
    platform, False for a standalone Host, and for a network member what its
    door last answered (above), or None while it has not answered yet."""
    if mode is None:
        mode = host_mode()
    if mode == MODE_PLATFORM:
        return True
    if mode != MODE_MEMBER:
        return False
    async with _network_lock():
        return await _ask_network()


async def _ask_network() -> Optional[bool]:
    bc = _bridge() or _raw_bridge()
    seen = _network_answer
    if seen["client"] is not bc:
        seen.update(client=bc, at=None, offers=None)
    if (seen["at"] is not None
            and time.monotonic() - seen["at"] < seen.get("for_s", NETWORK_ASK_S)):
        return seen["offers"]
    full = getattr(bc, "request_full", None)
    if full is None or not getattr(bc, "_connected", True):
        return seen["offers"]
    try:
        env = await asyncio.wait_for(full("backup.list", {"kind": KIND_BACKUP}),
                                     NETWORK_ASK_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — no answer (a timeout too): nothing learned
        log.info("backup: the network did not say whether it keeps backups: %s", e)
        env = None
    if isinstance(env, dict) and env.get("ok"):
        offers = True
    elif isinstance(env, dict) and env.get("code") in NETWORK_NOT_KEPT:
        offers = False
    else:
        # Nothing learned: the last answer stands, and the door is left alone
        # for NETWORK_RETRY_S.
        seen.update(at=time.monotonic(), for_s=NETWORK_RETRY_S)
        return seen["offers"]
    if offers != seen["offers"]:
        log.info("backup: the network's storage %s this Host's backups",
                 "takes" if offers else "does not take")
    seen.update(at=time.monotonic(), offers=offers, for_s=NETWORK_ASK_S)
    return offers


def destination_of(bp: dict, mode: str, network: Optional[bool] = None) -> str:
    """Where this Host's backups go: the owner's choice in Settings, Backup
    (`where`), else the network's storage when it takes them and a folder on
    this machine when it does not. Platform storage always takes them; a
    network member's only when `network` (network_backups) is True, so a
    member whose door does not keep backups, or has not said, keeps them in
    the folder."""
    where = str(_stored(bp).get("where") or "").strip().lower()
    if where in WHERES:
        return where
    if mode == MODE_PLATFORM or (mode == MODE_MEMBER and network is True):
        return WHERE_NETWORK
    return WHERE_LOCAL


def network_unavailable(mode: str, network: Optional[bool]) -> Optional[str]:
    """Why backups set to go to the network's storage cannot go there, or
    None: this Host is on no network, or its network said it keeps no
    backups. A member whose door has not answered yet is not refused: its run
    tries, and says what went wrong if the door is still not there."""
    if mode == MODE_STANDALONE:
        return NO_DESTINATION_TEXT
    if mode == MODE_MEMBER and network is False:
        return NO_NETWORK_STORAGE_TEXT
    return None


def local_keep(bp: dict) -> int:
    """How many backups the folder on this machine keeps: 1 to LOCAL_KEEP_MAX."""
    raw = _stored(bp).get("local_keep", LOCAL_KEEP_DEFAULT)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return LOCAL_KEEP_DEFAULT
    return max(1, min(n, LOCAL_KEEP_MAX))


def local_store(sub: str = ""):
    from server import backup_stores
    return backup_stores.LocalStore(LOCAL_DIR, sub=sub)


def _s3_client_for(creds: dict, target: dict):
    """The client for the owner's bucket (a seam the gate replaces)."""
    from server import backup_stores
    return backup_stores.s3_client(creds, target)


async def s3_store(pool, eid: int, bp: dict):
    """The owner's own bucket, as saved in Settings, Backup, with its key
    read from the vault. Raises BackupError saying what is missing."""
    from server import backup_ship, backup_stores
    try:
        target = backup_ship.validate_target(_stored(bp).get("target"))
    except ValueError as e:
        raise BackupError(f"the bucket saved for backups cannot be used: {e}") from None
    if not target or target.get("kind") != backup_ship.KIND_S3:
        raise BackupError("your backups go to your own bucket, and no bucket is saved: save one in "
                          "Settings, Backup")
    try:
        creds = await backup_ship._s3_credential(pool, eid, target["cred_name"])
    except backup_ship.ShipError as e:
        raise BackupError(f"{backup_ship.REASONS.get(e.reason, e.reason)}: {e.detail}") from None
    return backup_stores.S3Store(creds, target, client=_s3_client_for(creds, target))


async def recipients(pool, eid: int, bp: Optional[dict] = None) -> list[dict]:
    """Everyone a backup is encrypted to, besides the per-backup key, as
    {kind, label, fingerprint, recipient}: the pinned Host recovery key and any
    bastion keys (server/backup_key.py list_active returns only those kinds).
    EMPTY when no recovery key is pinned: a backup that only a bastion could
    open would be lost with that bastion, and the recovery key is what the
    owner was asked to keep."""
    from server import backup_key
    from server.vault import identity as vault_identity
    rows = await backup_key.list_active(pool, vault_identity.user_identity(str(eid)))
    # THE RULE, held here as well as in the table: only the recipient kinds,
    # and never this Host's own vault key, whatever row claims to be it (a key
    # this Host holds would make the Host able to open its own backups).
    own = bytes(await vault_identity.host_pubkey(pool))
    out = [{"kind": r["kind"], "label": r["label"],
            "fingerprint": backup_key.fingerprint(bytes(r["pubkey"])),
            "recipient": age_keys.recipient(bytes(r["pubkey"]))}
           for r in rows
           if r["kind"] in backup_key.RECIPIENT_KINDS and bytes(r["pubkey"]) != own]
    if not any(r["kind"] == backup_key.KIND_HOST_RECOVERY for r in out):
        return []
    return out


def _owner_since(got: Optional[dict]) -> Optional[dt.datetime]:
    """When this Host's current owner took over, from the platform's answer
    (bridge backup.recovery_key owner_since), or None: never transferred, no
    answer, or a value that is not a moment in the past. A date later than
    now is ignored rather than trusted: it would retire every key, the one
    the owner confirms next included."""
    raw = (got or {}).get("owner_since")
    if not raw:
        return None
    try:
        when = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        log.warning("backup: the platform sent an owner_since that is not a date: %.40r", raw)
        return None
    if when.tzinfo is None or when > _now() + dt.timedelta(minutes=10):
        log.warning("backup: the platform sent an owner_since that is not a past moment: %s", raw)
        return None
    return when


# How the last ask for the recovery key went, logged only when that changes:
# the backup loop asks every minute while a backup is due and there is no key
# (next_action), and a standalone Host has no platform to answer it, ever. None
# before the first ask, RECOVERY_FETCHED once the platform answered, else
# why not: standalone, not connected, or failed.
_recovery_fetch: Optional[str] = None
RECOVERY_FETCHED = "fetched"


def _note_recovery_fetch(failure: Optional[Exception]) -> None:
    """Log the recovery key's fetch once per change of how it went: an
    expected state (no platform, or not connected to it) at INFO, a platform
    that is there and did not answer at WARNING, and an answer again after
    either at INFO."""
    global _recovery_fetch
    if failure is None:
        if _recovery_fetch not in (None, RECOVERY_FETCHED):
            log.info("backup: the recovery key is fetched from the platform again")
        _recovery_fetch = RECOVERY_FETCHED
        return
    if host_mode() == MODE_STANDALONE:
        state, level, why = MODE_STANDALONE, logging.INFO, (
            "; this Host runs without the platform, so its owner makes their recovery key on the Host")
    elif _bridge() is None:
        state, level, why = "not connected", logging.INFO, ""
    else:
        state, level, why = "failed", logging.WARNING, ""
    if state != _recovery_fetch:
        log.log(level, "backup: recovery key not fetched: %s%s (said again only when that changes)",
                failure, why)
    _recovery_fetch = state


async def pin_recovery_key(pool, eid: int) -> dict:
    """Ask the platform for this Host's recovery key (the public half of the
    key made in the owner's browser; bridge op backup.recovery_key, answered
    only once the owner confirmed they saved it) and keep it as an OFFER.

    Nothing the platform sends is pinned on its word, the first key included:
    a platform that answered with a key of its own before the owner's first
    backup would otherwise receive every backup. Settings shows the offer's
    fingerprint; the owner compares it with the one on the recovery key they
    saved (the file and the printout carry it) and adopts it with their second
    factor (adopt_recovery_offer), the way an export's key is confirmed. Until
    then there is no recovery key and no backup.

    And the pinned key is used only while it is the owner's current one: when
    the platform answers that the owner holds none ({}), or holds a different
    one (a new key, or a new owner after a transfer), the pinned key is retired
    (kept listed: older backups open with it) and nothing is sealed to it; a
    different key waits for confirmation. No answer at all changes nothing.

    After a transfer the answer also says when the current owner took over
    (owner_since). Every key registered before then is a previous owner's
    and is retired: the recovery key, and the bastion keys, which only this
    Host holds (a transfer cannot reach them on the platform). Without that
    the new owner's backups and exports would be sealed to the previous
    owner's bastions as soon as the new owner confirmed a recovery key.

    A key the owner made on this Host (make_own_recovery_key) is not the
    platform's to retire: the platform never saw it, so its saying the owner
    holds no key, or holds another one, says nothing about this one. It stays
    the active key; another key the platform holds waits as an offer, which
    the owner may adopt in its place. A transfer still retires it.
    Returns {pinned, offer} as fingerprints and labels; pinned.made_on_host
    says which kind the active key is."""
    from server import backup_key
    from server.vault import identity as vault_identity
    ident = vault_identity.user_identity(str(eid))
    try:
        got: Optional[dict] = await call("backup.recovery_key", {})
        _note_recovery_fetch(None)
    except Exception as e:
        # No answer (not connected, or the platform failed): nothing is known,
        # so nothing changes.
        _note_recovery_fetch(e)
        got = None
    since = _owner_since(got)
    if since is not None:
        for r in await backup_key.retire_registered_before(pool, ident, since):
            log.warning("backup: %s key %r (%s) was registered before this Host's owner took over "
                        "at %s; nothing more is sealed to it", r["kind"], r["label"],
                        r["fingerprint"], since.isoformat())
    pinned = await backup_key.get_active(pool, ident, backup_key.KIND_HOST_RECOVERY)
    own = await made_on_host(pool, pinned)
    offer = None
    # Only a key the owner confirmed they saved counts: the platform answers
    # {} until there is one (privatae bridge op_backup_recovery_key).
    if got is not None and not (got.get("recipient") and got.get("saved_at")):
        # The platform ANSWERED that this Host's owner holds no recovery key:
        # they retired it, or the Host changed hands (a transfer retires the
        # previous owner's key). The key pinned here is nobody's now, so
        # nothing more is sealed to it; the owner saves and confirms a new one.
        # Unless the owner made it here: the platform knows nothing of it.
        if pinned is not None and not own:
            await backup_key.retire_recovery(pool, ident)
            log.warning("backup: the owner holds no recovery key any more; %s is no longer used",
                        backup_key.fingerprint(bytes(pinned["pubkey"])))
            pinned = None
        await _drop_state(pool, "recovery_offer")
    elif got is not None:
        label = str(got.get("label") or "")[:backup_key.MAX_LABEL]
        if "AGE-SECRET-KEY-" in label.upper() or any(ord(c) < 32 for c in label):
            label = ""                      # a label is a name, never anything else
        try:
            raw = age_keys.recipient_bytes(got["recipient"])
        except ValueError:
            raw = None
        if raw is None or backup_key.fingerprint(raw) != got.get("fingerprint"):
            log.warning("backup: the platform sent a recovery key that does not match its fingerprint")
            await _drop_state(pool, "recovery_offer")
        elif pinned is None or bytes(pinned["pubkey"]) != raw:
            if pinned is not None and not own:
                # The owner's key is a different one now (they made a new
                # one, or the Host changed hands): the pinned one is not
                # their current key, so it receives nothing more. The new one
                # waits for the owner's confirmation like the first.
                await backup_key.retire_recovery(pool, ident)
                log.warning("backup: the account page holds another recovery key; %s is no longer "
                            "used until the owner confirms %s",
                            backup_key.fingerprint(bytes(pinned["pubkey"])), got["fingerprint"])
                pinned = None
            offer = {"recipient": got["recipient"], "fingerprint": got["fingerprint"],
                     "label": label,
                     "saved_at": got.get("saved_at"), "seen_at": _now().isoformat()}
            await _set_state(pool, "recovery_offer", offer)
        else:
            await _drop_state(pool, "recovery_offer")
    if offer is None:
        offer = await _state(pool, "recovery_offer")
    return {"pinned": ({"fingerprint": backup_key.fingerprint(bytes(pinned["pubkey"])),
                        "label": pinned["label"],
                        "created_at": pinned["created_at"].isoformat() if pinned.get("created_at") else None,
                        "made_on_host": own}
                       if pinned else None),
            "offer": offer}


# The owner's own key, made on this Host: its fingerprint, in
# host_backup_state (carried over a restore with the keys themselves:
# host_restore.CARRIED_STATE). It counts only while it names the active key.
OWN_KEY_STATE = "recovery_own"
OWN_LABEL_DEFAULT = "Recovery key made on this Host"


async def made_on_host(pool, active: Optional[dict]) -> bool:
    """Is `active` (the active host-recovery row) the key the owner made on
    this Host?"""
    if not active:
        return False
    from server import backup_key
    mark = await _state(pool, OWN_KEY_STATE)
    return bool(mark) and mark.get("fingerprint") == backup_key.fingerprint(bytes(active["pubkey"]))


def _clean_label(label: Optional[str]) -> str:
    from server import backup_key
    text = " ".join(str(label or "").split())[:backup_key.MAX_LABEL]
    if "AGE-SECRET-KEY-" in text.upper() or unshowable(text):
        return OWN_LABEL_DEFAULT
    return text or OWN_LABEL_DEFAULT


async def make_own_recovery_key(pool, eid: int, recipient: str, fingerprint: Optional[str],
                                label: str = "", replace: bool = False) -> dict:
    """The owner made a recovery key in their browser on this Host and kept
    its private half; this is its public half. It becomes the active recovery
    key: every backup and export from now on is encrypted to it. The caller
    has checked their second factor (routes/backup.py own_recovery_key).

    Refused: anything that is not an age recipient, a fingerprint that is not
    its own, this Host's own vault key (a key the Host holds would let it
    open its own backups), and a second key without `replace` (the backups
    already taken stay encrypted to the old key, so replacing it is asked
    for, never slipped in). The key before it stays listed: older backups
    open only with it."""
    from server import backup_key
    from server.vault import identity as vault_identity
    try:
        raw = age_keys.recipient_bytes(recipient)
    except ValueError:
        raise BackupError("that is not a recovery key's public half (age1...)") from None
    fp = backup_key.fingerprint(raw)
    if fingerprint is not None and (fingerprint or "").strip() != fp:
        raise BackupError("the fingerprint sent is not this key's: nothing was changed")
    if raw == bytes(await vault_identity.host_pubkey(pool)):
        raise BackupError("that is this Host's own key; a recovery key must be one only you hold")
    ident = vault_identity.user_identity(str(eid))
    prev = await backup_key.get_active(pool, ident, backup_key.KIND_HOST_RECOVERY)
    prev_fp = backup_key.fingerprint(bytes(prev["pubkey"])) if prev else None
    if prev is not None and prev_fp != fp and not replace:
        raise BackupError(f"this Host already has a recovery key (fingerprint {prev_fp}). Replacing it "
                          "does not re-encrypt the backups you have: keep that key too, because it is "
                          "what opens them")
    if prev_fp != fp:
        await backup_key.put_active(pool, ident=ident, pub_raw=raw, sealed=None,
                                    origin="owner-browser", kind=backup_key.KIND_HOST_RECOVERY,
                                    label=_clean_label(label))
    await _set_state(pool, OWN_KEY_STATE, {"fingerprint": fp, "made_at": _now().isoformat()})
    log.info("backup: recovery key %s made on this Host by the owner", fp)
    return {"fingerprint": fp, "first": prev is None, "replaced": prev_fp if prev_fp != fp else None}


async def adopt_recovery_offer(pool, eid: int, fingerprint: str) -> dict:
    """The owner adopts the recovery key on offer, having compared its
    fingerprint: the first one, or one in place of the pinned one. The caller
    has checked their second factor (routes/backup.py adopt_recovery_key).
    Older keys stay listed: backups taken before are encrypted to them."""
    from server import backup_key
    from server.vault import identity as vault_identity
    offer = await _state(pool, "recovery_offer")
    if not offer or offer.get("fingerprint") != (fingerprint or "").strip():
        raise BackupError("that is not the recovery key the account page holds for this Host")
    raw = age_keys.recipient_bytes(offer["recipient"])
    if backup_key.fingerprint(raw) != offer["fingerprint"]:
        raise BackupError("the offered key does not match its fingerprint")
    ident = vault_identity.user_identity(str(eid))
    first = await backup_key.get_active(pool, ident, backup_key.KIND_HOST_RECOVERY) is None
    await backup_key.put_active(pool, ident=ident, pub_raw=raw, sealed=None,
                                origin="owner-browser", kind=backup_key.KIND_HOST_RECOVERY,
                                label=offer.get("label") or "")
    await _drop_state(pool, "recovery_offer")
    log.info("backup: recovery key %s adopted by the owner", offer["fingerprint"])
    return {"fingerprint": offer["fingerprint"], "first": first}


def public_recipients(recips: list[dict]) -> list[dict]:
    """What a manifest, a run record or a report says about recipients: who,
    never the key itself."""
    return [{"kind": r["kind"], "label": r["label"], "fingerprint": r["fingerprint"]}
            for r in recips]


# ── the bridge ───────────────────────────────────────────────────────────────

def _raw_bridge():
    """The current platform client, connected or not (host_mode judges it)."""
    try:
        from server.main import get_bridge_client
        return get_bridge_client()
    except Exception:
        return None


def _bridge():
    bc = _raw_bridge()
    return bc if bc is not None and getattr(bc, "_connected", True) else None


async def call(op: str, params: Optional[dict] = None) -> dict:
    """A bridge op that must answer. Raises BackupError with the platform's own
    words when it does not."""
    bc = _bridge()
    if bc is None:
        raise BackupError("the Host is not connected to the platform")
    full = getattr(bc, "request_full", None)
    if full is not None:
        env = await full(op, params or {})
        if not env.get("ok"):
            raise BackupError(f"{op}: {env.get('error') or 'refused'}")
        return env.get("data") or {}
    res = await bc.request(op, params or {})
    if res is None:
        raise BackupError(f"{op}: no answer")
    return res


# ── uploading one object in parts (runs in a worker thread) ──────────────────

def _put(url: str, data: bytes, timeout: float = 600, headers: Optional[dict] = None) -> str:
    """PUT to a presigned URL; the ETag it answers. Through the pod's egress
    proxy, which urllib takes from the environment (the pod's only way out).
    `headers`: what the platform said the PUT must carry (backup.put_url
    answers If-None-Match: *, signed into the URL, so storage refuses a second
    write to the object; a PUT without it fails the signature)."""
    h = {"Content-Type": "application/octet-stream"}
    h.update({str(k): str(v) for k, v in (headers or {}).items()})
    req = urllib.request.Request(url, data=data, method="PUT", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return (resp.headers.get("ETag") or "").strip()


class Uploader:
    """One object, uploaded part by part through presigned URLs.

    `sync(op, params)` runs a bridge op from this worker thread on the event
    loop and returns its answer. The platform names the object. For a backup
    (kind backup or staged) it also picks the STAMP, from its own clock: a
    Host that has been taken over cannot write a backup dated in the future or
    the past and so push real backups out of retention. The Host sends only
    the kind, and a name for an export."""

    def __init__(self, sync: Callable[[str, dict], dict], kind: str, name: Optional[str] = None):
        self.sync, self.kind = sync, kind
        got = sync("backup.upload.start", {"kind": kind} if name is None else {"kind": kind, "name": name})
        self.upload_id, self.key = got["upload_id"], got["key"]
        self.stamp = got.get("stamp")
        if name is None and not STAMP_RE.match(str(self.stamp or "")):
            self.abort()
            raise BackupError("the platform did not give this backup a time stamp")
        self.parts: list[dict] = []
        self.size = 0
        self.h = hashlib.sha256()

    def part(self, data: bytes) -> None:
        n = len(self.parts) + 1
        self.h.update(data)
        self.size += len(data)
        last: Optional[Exception] = None
        for attempt in range(PART_TRIES):
            try:
                # The part's URL is signed for exactly this many bytes.
                url = self.sync("backup.upload.part",
                                {"upload_id": self.upload_id, "key": self.key, "part": n,
                                 "size": len(data)})["url"]
                etag = _put(url, data)
                self.parts.append({"part": n, "etag": etag})
                return
            except Exception as e:  # retried with a fresh URL
                last = e
                time.sleep(2 * (attempt + 1))
        raise BackupError(f"part {n} could not be uploaded: {last}")

    def complete(self) -> dict:
        self.sync("backup.upload.complete", {
            "upload_id": self.upload_id, "key": self.key, "parts": self.parts,
            "size": self.size, "sha256": self.h.hexdigest()})
        return {"key": self.key, "size": self.size, "sha256": self.h.hexdigest()}

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self.sync("backup.upload.abort", {"upload_id": self.upload_id, "key": self.key})


def _syncer(loop: asyncio.AbstractEventLoop) -> Callable[[str, dict], dict]:
    def sync(op: str, params: dict) -> dict:
        return asyncio.run_coroutine_threadsafe(call(op, params), loop).result(timeout=300)
    return sync


# ── the file ─────────────────────────────────────────────────────────────────

class _Exact(io.RawIOBase):
    """Exactly `size` bytes of a file that may change while it is read: a short
    read is padded with zeros, anything past `size` is left, and the file is
    marked as changed. tar has already written the size."""

    def __init__(self, f, size: int):
        self.f, self.left, self.changed = f, size, False
        self.h = hashlib.sha256()

    def readable(self):
        return True

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        b = self.f.read(n)
        if len(b) < n:
            self.changed = True
            b = b + b"\0" * (n - len(b))
        self.left -= len(b)
        self.h.update(b)
        return b


def _fill(stream, buf: bytearray) -> int:
    """Fill `buf` from a pipe, reusing it; the count read, short only at the
    pipe's end. No copy: a buffer of the part size is all this ever holds."""
    view = memoryview(buf)
    got = 0
    while got < len(buf):
        n = stream.readinto(view[got:])
        if not n:
            break
        got += n
    return got


class _View(io.RawIOBase):
    """A read-only file over the first `n` bytes of a reused buffer, for
    tarfile.addfile, which copies from it in small blocks."""

    def __init__(self, buf: bytearray, n: int):
        self.view, self.pos, self.n = memoryview(buf)[:n], 0, n

    def readable(self):
        return True

    def readinto(self, b):
        k = min(len(b), self.n - self.pos)
        b[:k] = self.view[self.pos:self.pos + k]
        self.pos += k
        return k


def _readme(kind: str, eid: int, made_at: str) -> str:
    who = ("The export key you made when you asked for this export opens it, and so\n"
           "  do your Host recovery key and your bastions' keys (manifest.json names them)."
           if kind == KIND_EXPORT else
           "Your Host recovery key opens it, and so does the key of any bastion you\n"
           "  added (manifest.json names them). Privatae holds none of these keys.")
    return f"""Privatae - {'export' if kind == KIND_EXPORT else 'backup'} of your Host (Host #{eid})
Made {made_at} by your Host, and encrypted on your Host before it left.

HOW TO OPEN
  age -d -i <your key file> <this file> | zstd -d | tar -x
  {who}

WHAT IS INSIDE
  db/hostmem.dump.NNNNN  your Host's memory database, PostgreSQL custom format,
                         cut into pieces. Join them first:
                           cat db/hostmem.dump.* > hostmem.dump
                         then pg_restore -l hostmem.dump lists it, and
                         pg_restore -d <database> hostmem.dump restores it.
  apps/                  your Host's apps volume: installed apps, project files,
                         its workspace and saved state
  app-data/<app>.tar.NNNNN
                         each app's own data (what it keeps between runs, kept
                         too for an app you removed and chose to keep the data
                         of), as podman volume export writes it, cut into
                         pieces. Join them first:
                           cat app-data/<app>.tar.* > <app>.tar
                         then tar -tvf <app>.tar lists it
  gitea-repos/           mirrors of your Host's own Git repositories, when there
                         are any; git clone gitea-repos/<name>.git gives a copy
  manifest.json          every file above with its size and sha256, every link
                         with its target, and each app's data with its sha256
"""


def write_file(*, upload: "Uploader", recips: list[str], eid: int, stamp: str, kind: str,
               dbname: str, git_dir: Optional[Path], apps_root: Optional[str] = None,
               who: Optional[list[dict]] = None, app_data: Optional[dict] = None) -> dict:
    """Stream one backup file into `upload`. Synchronous; runs in a worker
    thread. Raises (and the caller aborts the upload) if the database dump or
    age fails: a backup without its database is not a backup."""
    from compression import zstd

    cmd = [AGE]
    for r in recips:
        cmd += ["-r", r]
    age = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    sent: dict[str, Any] = {"err": None}
    # The watchdog: at RUN_LIMIT_S every process of this run is killed, which
    # ends every read and write below, and the run fails with that reason.
    procs: list = [age]
    expired = threading.Event()

    def expire():
        expired.set()
        for p in list(procs):
            with contextlib.suppress(Exception):
                p.kill()
    watchdog = threading.Timer(RUN_LIMIT_S, expire)
    watchdog.daemon = True
    watchdog.start()

    def pump():
        buf = bytearray(PART_BYTES)
        try:
            while True:
                n = _fill(age.stdout, buf)
                if not n:
                    return
                upload.part(memoryview(buf)[:n])
        except Exception as e:  # raised by the caller after the pipes close
            sent["err"] = e
            with contextlib.suppress(Exception):
                age.stdout.close()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    made_at = _now().isoformat(timespec="seconds")
    parts: dict[str, Any] = {}
    manifest = _manifest_head(eid, stamp, made_at, kind, who)
    manifest["parts"] = parts
    files = _Listing(len(json.dumps(manifest)))
    manifest["files"] = files
    failure: Optional[BaseException] = None
    zf = zstd.ZstdFile(age.stdin, mode="wb", level=3)
    tout = tarfile.open(fileobj=zf, mode="w|", format=tarfile.PAX_FORMAT)
    try:
        parts["db"] = _add_database(tout, dbname, procs)
        parts["apps"] = _add_apps(tout, apps_root or APPS_ROOT, files)
        if app_data is not None:
            parts["app_data"] = _add_app_data(tout, app_data, procs)
        parts["gitea"] = _add_tree(tout, git_dir, "gitea-repos", files) if git_dir else {"repos": 0}
        listed = json.dumps(manifest)
        # The running count left out only `parts`, which is small; this is exact.
        if len(listed.encode()) > MANIFEST_MAX:
            raise TooManyFiles(len(files))
        for fname, text in (("manifest.json", listed),
                            ("README.txt", _readme(kind, eid, made_at))):
            data = text.encode()
            ti = tarfile.TarInfo(fname)
            ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o644
            tout.addfile(ti, io.BytesIO(data))
    except BaseException as e:
        failure = e
    finally:
        try:
            tout.close()
            zf.close()
        except Exception as e:  # the pipe broke: age or the upload died
            failure = failure or e
        finally:
            with contextlib.suppress(Exception):
                age.stdin.close()
    # The uploader first: age cannot exit before its last bytes are read, and
    # the last part may take a while on a slow link. The pump ends at age's
    # end of output, or on its own failure, when it closes the pipe and age
    # stops.
    t.join()
    age_rc = age.wait(timeout=AGE_EXIT_WAIT_S)
    watchdog.cancel()
    if expired.is_set():
        raise BackupError(f"the backup ran past its time limit ({RUN_LIMIT_S // 60} minutes) and "
                          "was stopped; nothing was kept")
    if failure is not None:
        raise failure
    if sent["err"] is not None:
        raise sent["err"]
    if age_rc != 0:
        raise BackupError(f"age exited {age_rc}: {_tail(age.stderr.read().decode(errors='replace'))}")
    return {"parts": parts, "files": len(files)}


def _add_database(tout: tarfile.TarFile, dbname: str, procs: Optional[list] = None) -> dict:
    """pg_dump, streamed into tar members of at most DB_CHUNK bytes."""
    with tempfile.TemporaryFile() as err:
        dump = subprocess.Popen(
            [PG_DUMP, "-Fc", "-Z0", "--no-owner", "--no-privileges",
             "-h", PG_SOCKET, "-U", PG_USER, dbname],
            stdout=subprocess.PIPE, stderr=err)
        if procs is not None:
            procs.append(dump)
        h, total, n = hashlib.sha256(), 0, 0
        buf = bytearray(DB_CHUNK)
        try:
            while True:
                got = _fill(dump.stdout, buf)
                if not got:
                    break
                ti = tarfile.TarInfo(f"db/hostmem.dump.{n:05d}")
                ti.size, ti.mtime, ti.mode = got, int(time.time()), 0o600
                tout.addfile(ti, _View(buf, got))
                h.update(memoryview(buf)[:got])
                total += got
                n += 1
        finally:
            rc = dump.wait(timeout=RUN_TIMEOUT_S)
        if rc != 0:
            err.seek(0)
            raise BackupError("the database dump failed: "
                              + _tail(err.read().decode(errors="replace")))
    if total == 0:
        raise BackupError("the database dump was empty")
    return {"chunks": n, "bytes": total, "sha256": h.hexdigest()}


def _manifest_head(eid: int, stamp: str, made_at: str, kind: str, who) -> dict:
    return {"format": FORMAT, "entity_id": eid, "stamp": stamp, "made_at": made_at,
            "kind": kind, "recipients": who or [], "parts": {}, "files": []}


def _too_many_files(n: int) -> str:
    return (f"this Host holds more files than one backup can list ({n}; the list may be at most "
            f"{MANIFEST_MAX >> 20} MiB), so it was not backed up")


# Added to a backup run's refusal (_run), not to an export's: an export
# refused this way is asked for again where it was asked for, and adds nothing
# to the scheduled backup's back-off.
TOO_MANY_FILES_NEXT = (". Once you have removed files it does not need, press Back up now in "
                       "Settings, Backup: after each refusal like this the scheduled backup waits "
                       "longer before it tries again, up to a day")


class _Listing(list):
    """manifest.json's `files`, counting as each entry is added the bytes it adds
    to the manifest (`size`, from the manifest's size with no files). A Host
    with more than MANIFEST_MAX of them is refused as the walk reaches the
    limit, not after every file has been read, encrypted and sent."""

    def __init__(self, base: int):
        super().__init__()
        self.size = base

    def append(self, entry: dict) -> None:
        # json.dumps writes ", " between list items, and escapes to ASCII, so
        # characters are bytes.
        self.size += len(json.dumps(entry)) + (2 if self else 0)
        if self.size > MANIFEST_MAX:
            raise TooManyFiles(len(self) + 1)
        super().append(entry)


LEFT_OUT_SHOWN = 20     # left-out names kept on a run, with their paths


def _files_under(full: str) -> int:
    """How many files (anything not a folder) a left-out folder holds. Links
    are not followed."""
    n = 0
    for _dirpath, _dirnames, filenames in os.walk(full):
        n += len(filenames)
    return n


def _open_regular(full: str, st: os.stat_result, root: Optional[str]):
    """The file at `full` opened for reading, only while it is still the
    regular file lstat saw and only from inside `root` (a real path). Raises
    OSError otherwise.

    A backup (and an export, which is a backup) stores a link AS a link;
    between the lstat that said "regular file" and the open, a name can be
    swapped for a link, and a folder on the way can be swapped for a link to
    somewhere else. O_NOFOLLOW refuses the first, the inode must be the one
    lstat saw, and the opened file's own path (/proc/self/fd) must lie under
    `root`, which refuses the second. A restore writes what was read back
    as a real file where the name was, so a followed link would hand its
    target to whoever can read that folder."""
    fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        fst = os.fstat(fd)
        if not _stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise OSError(errno.ESTALE, "it changed while it was being backed up")
        if root is not None:
            real = os.readlink(f"/proc/self/fd/{fd}")
            if not real.startswith(root.rstrip("/") + "/"):
                raise OSError(errno.EXDEV, "it is reached through a link out of the backed-up folder")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _add_member(tout: Optional[tarfile.TarFile], full: str, arc: str, files: list,
                notes: dict, *, root: Optional[str] = None) -> None:
    """Write one name into the backup and its manifest. With `tout` None,
    only list it as the writer would (the entry's size is what counts:
    _Listing), reading no file: the pre-walk (precount). `root` is the real
    path of the folder being walked; a file is read only from inside it
    (_open_regular)."""
    if unshowable(arc):
        # Its restore would refuse the whole backup (host_restore.unpack), so
        # the name is left out and counted; the walkers do not enter it. Where
        # it is, and how much went with it, is kept so the owner can find it.
        notes["left_out"] = notes.get("left_out", 0) + 1
        if tout is None:
            return
        entry: dict[str, Any] = {"path": shown(arc)}
        with contextlib.suppress(OSError):
            if _stat.S_ISDIR(os.lstat(full).st_mode):
                entry["files"] = _files_under(full)
        notes["left_out_files"] = notes.get("left_out_files", 0) + entry.get("files", 1)
        if len(notes.setdefault("left_out_names", [])) < LEFT_OUT_SHOWN:
            notes["left_out_names"].append(entry)
        return
    try:
        st = os.lstat(full)
    except OSError:
        return
    if tout is None:
        if _stat.S_ISREG(st.st_mode) and os.access(full, os.R_OK):
            files.append({"path": arc, "bytes": st.st_size, "sha256": "0" * 64})
        elif _stat.S_ISLNK(st.st_mode):
            with contextlib.suppress(OSError):
                files.append({"path": arc, "link": os.readlink(full)})
        return
    if _stat.S_ISREG(st.st_mode):
        try:
            f = _open_regular(full, st, root)
        except OSError as e:
            notes.setdefault("unreadable", []).append(f"{arc}: {e.strerror}")
            return
        with f:
            ti = tout.gettarinfo(fileobj=f, arcname=arc)
            # A second name for a file already written (a hard link, as git
            # clone --local and pnpm make) would go in as a link with no
            # content, which a restore cannot check against the manifest. Every
            # name is stored as a whole file.
            ti.type, ti.linkname, ti.size = tarfile.REGTYPE, "", os.fstat(f.fileno()).st_size
            ex = _Exact(f, ti.size)
            tout.addfile(ti, ex)
        files.append({"path": arc, "bytes": ti.size, "sha256": ex.h.hexdigest()})
        notes["files"] = notes.get("files", 0) + 1
        notes["bytes"] = notes.get("bytes", 0) + ti.size
        if ex.changed:
            notes.setdefault("changed", []).append(arc)
    elif _stat.S_ISLNK(st.st_mode):
        ti = tout.gettarinfo(full, arcname=arc)
        tout.addfile(ti)
        # Links are listed with their targets, so a restore checks them as it
        # checks every file (host_restore.unpack).
        files.append({"path": arc, "link": ti.linkname})
    elif _stat.S_ISDIR(st.st_mode):
        tout.addfile(tout.gettarinfo(full, arcname=arc))
    # sockets, fifos and devices are not backed up


def _add_apps(tout: Optional[tarfile.TarFile], root: str, files: list) -> dict:
    notes: dict[str, Any] = {"files": 0, "bytes": 0}
    if not os.path.isdir(root):
        return notes
    real_root = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if excluded(rel):
                continue
            _add_member(tout, os.path.join(dirpath, d), f"apps/{rel}", files, notes,
                        root=real_root)
            if not os.path.islink(os.path.join(dirpath, d)) and not unshowable(d):
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if not excluded(rel):
                _add_member(tout, os.path.join(dirpath, f), f"apps/{rel}", files, notes,
                            root=real_root)
    for k in ("unreadable", "changed"):
        if k in notes:
            notes[k] = notes[k][:20]
    return notes


# The runtime's answer for an app's data a restore would refuse
# (cseries_runtime/server.py _export_app_data runs the restore's tar check).
UNRESTORABLE = 422


def _add_app_data(tout: tarfile.TarFile, listing: dict, procs: Optional[list] = None) -> dict:
    """Each app's kept data volume, streamed from the local runtime (podman's
    volume tar, local_runtime.export_app_data) into tar members of at most
    DB_CHUNK bytes, app-data/<app>.tar.NNNNN, one app after another. Each
    app's entry carries its pieces, bytes and sha256, which a restore checks
    (host_restore._check_app_data). An app whose data could not be read
    whole is listed with ok false and why, and the run is partial; one whose
    data was deleted between the listing and its turn is not listed at all.

    `listing`: {"apps": the runtime's workspace.app_data list} or {"error":
    why it could not be listed}. The volume is read while the app may run,
    as the apps tree is: an app writing at that moment can leave a file in
    its data as it was part way through a write."""
    from server.applications import local_runtime
    if listing.get("error"):
        return {"ok": False, "apps": [], "bytes": 0, "error": listing["error"]}
    out: list[dict] = []
    total = 0
    buf = bytearray(DB_CHUNK)
    for app in sorted(listing.get("apps") or [], key=lambda a: str(a.get("app_id"))):
        aid = str(app.get("app_id") or "")
        st = {"h": hashlib.sha256(), "n": 0, "fill": 0, "bytes": 0}

        def flush(st=st, aid=aid):
            if not st["fill"]:
                return
            ti = tarfile.TarInfo(f"app-data/{aid}.tar.{st['n']:05d}")
            ti.size, ti.mtime, ti.mode = st["fill"], int(time.time()), 0o600
            tout.addfile(ti, _View(buf, st["fill"]))
            st["n"] += 1
            st["fill"] = 0

        def write(b: bytes, st=st, flush=flush):
            st["h"].update(b)
            st["bytes"] += len(b)
            view = memoryview(b)
            while view:
                k = min(len(view), DB_CHUNK - st["fill"])
                buf[st["fill"]:st["fill"] + k] = view[:k]
                st["fill"] += k
                view = view[k:]
                if st["fill"] == DB_CHUNK:
                    flush()

        entry: dict[str, Any] = {"app_id": aid, "installed": bool(app.get("installed"))}
        try:
            head = local_runtime.export_app_data(aid, write, procs)
            flush()
            entry.update(ok=True, uid_base=head.get("uid_base"), chunks=st["n"], bytes=st["bytes"],
                         sha256=st["h"].hexdigest())
        except local_runtime.RuntimeRefusal as e:
            flush()
            if e.code == 404 and not st["bytes"]:
                continue                # deleted since it was listed: nothing of it is here
            entry.update(ok=False, chunks=st["n"], bytes=st["bytes"], error=e.text[:600])
            if e.code == UNRESTORABLE:
                # Read whole, but the runtime's restore check refuses it (a
                # link out of its volume, say): the runtime's words name the
                # app and what in its data a restore refuses.
                entry["refused"] = True
        except OSError as e:
            flush()
            entry.update(ok=False, chunks=st["n"], bytes=st["bytes"],
                         error=f"the connection to the runtime broke ({type(e).__name__})")
        total += st["bytes"]
        out.append(entry)
    bad = [a for a in out if not a.get("ok")]
    part: dict[str, Any] = {"ok": not bad, "apps": out, "bytes": total}
    if bad:
        part["error"] = "; ".join(str(a.get("error")) if a.get("refused")
                                  else f"{a['app_id']}'s data could not be read: {a.get('error')}"
                                  for a in bad[:5])[:RUN_ERROR_MAX]
    return part


async def app_data_listing() -> Optional[dict]:
    """What _add_app_data takes, from this machine's local runtime: None
    where there is none (a platform Host's apps keep their data elsewhere)."""
    from server.applications import local_runtime
    if not local_runtime.in_use():
        return None
    env = await local_runtime.runtime_client().request_full("workspace.app_data", {})
    if not env.get("ok"):
        return {"error": f"the apps' data could not be listed: {env.get('error') or 'no answer'}"[:300]}
    return {"apps": [a for a in (env.get("data") or {}).get("apps") or []
                     if isinstance(a, dict) and isinstance(a.get("app_id"), str)]}


def precount(apps_root: str, git_dir: Optional[Path], files: "_Listing") -> None:
    """List the apps tree and the Git mirrors as the writer will, into
    `files`, reading no file: TooManyFiles as soon as the list passes
    MANIFEST_MAX. Run before anything is dumped or sent (write_object), so a
    Host over the limit is refused without its database being dumped,
    encrypted and uploaded first. The writer still counts as it writes."""
    _add_apps(None, apps_root, files)
    if git_dir:
        _add_tree(None, git_dir, "gitea-repos", files)


def _add_tree(tout: Optional[tarfile.TarFile], base: Path, prefix: str, files: list) -> dict:
    notes: dict[str, Any] = {"files": 0, "bytes": 0}
    repos = 0
    real_base = os.path.realpath(base)
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames.sort()
        if not rel_dir:
            repos = len(dirnames)
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            _add_member(tout, os.path.join(dirpath, d), f"{prefix}/{rel}", files, notes,
                        root=real_base)
        dirnames[:] = [d for d in dirnames if not unshowable(d)]
        for f in sorted(filenames):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            _add_member(tout, os.path.join(dirpath, f), f"{prefix}/{rel}", files, notes,
                        root=real_base)
    out = {"repos": repos, "bytes": notes["bytes"]}
    for k in ("left_out", "left_out_files", "left_out_names"):
        if notes.get(k):
            out[k] = notes[k]
    return out


# ── proof that a backup is this Host's own ──────────────────────────────────
#
# age encrypts to public keys, so whoever can write where backups are kept can
# make a file (and a sidecar) that the owner's recovery key opens. What such a
# file cannot carry is a tag made with a key only this Host holds: every
# sidecar carries an HMAC-SHA256, under this Host's backup-auth key, over its
# whole body, which names the encrypted file's sha256 and size. A restore
# checks the tag against the keys this Host holds (host_restore.provenance)
# and the encrypted file against the sha256 as it streams (host_restore.unpack),
# so a backup that verifies is byte for byte one this Host wrote.
#
# The key is a MAC key and nothing else: it opens no backup, so holding it
# changes nothing about THE RULE. It lives in kernel_secrets (which the next
# backup carries, encrypted) and is never sent anywhere. A Host that replaced a
# lost one does not have the lost Host's key, so a backup of that Host cannot
# be verified here and is restored only when the owner says so; after it this
# Host keeps its own keys and drops the ones the backup brought, since nobody
# could vouch for them (host_restore._keep_auth_key). After a restore that did
# verify, the restored database's key is this Host's, and the key this Host had
# before is kept beside it, so the backup taken just before still verifies.
AUTH_KEY_ID = "backup_auth"
AUTH_ALG = "hmac-sha256"


def auth_kid(key: bytes) -> str:
    return hashlib.sha256(b"privatae-backup-auth/1\0" + bytes(key)).hexdigest()[:16]


async def auth_key(pool) -> bytes:
    """This Host's backup-auth key, made on first use."""
    got = await pool.fetchval("SELECT secret FROM kernel_secrets WHERE id = $1", AUTH_KEY_ID)
    if got is None:
        await pool.execute("INSERT INTO kernel_secrets (id, secret) VALUES ($1, $2) "
                           "ON CONFLICT (id) DO NOTHING", AUTH_KEY_ID, os.urandom(32))
        got = await pool.fetchval("SELECT secret FROM kernel_secrets WHERE id = $1", AUTH_KEY_ID)
    return bytes(got)


async def auth_keys(pool) -> dict[str, bytes]:
    """Every key a tag may verify under here, by kid: this Host's own and the
    ones it kept from before a restore."""
    rows = await pool.fetch("SELECT id, secret FROM kernel_secrets "
                            "WHERE id = $1 OR id LIKE $2", AUTH_KEY_ID, AUTH_KEY_ID + ":%")
    return {auth_kid(bytes(r["secret"])): bytes(r["secret"]) for r in rows}


def _auth_message(body: dict) -> bytes:
    return json.dumps({k: v for k, v in body.items() if k != "auth"},
                      sort_keys=True, separators=(",", ":")).encode()


def auth_tag(body: dict, key: bytes) -> dict:
    import hmac
    return {"alg": AUTH_ALG, "kid": auth_kid(key),
            "tag": hmac.new(bytes(key), _auth_message(body), hashlib.sha256).hexdigest()}


def auth_verifies(body: dict, keys: dict[str, bytes]) -> bool:
    """Does a sidecar body carry a tag made by one of `keys`?"""
    import hmac
    a = body.get("auth") if isinstance(body, dict) else None
    if not isinstance(a, dict) or a.get("alg") != AUTH_ALG:
        return False
    key = keys.get(str(a.get("kid") or ""))
    if key is None:
        return False
    return hmac.compare_digest(auth_tag(body, key)["tag"], str(a.get("tag") or ""))


def make_sidecar(recips: list[str], *, eid: int, stamp: str, object_name: str,
                 identity: str, data: Optional[dict] = None, key: Optional[bytes] = None) -> bytes:
    """The per-backup key, sealed (armored age) to the same keys as the backup:
    the Host recovery key and any bastion keys. Never to this Host's own key.
    `data`: the encrypted file's {sha256, size}; `key`: this Host's backup-auth
    key, which tags the body (above)."""
    doc = {"v": 1, "kind": "privatae-backup-key", "identity": identity,
           "object": object_name, "entity_id": eid, "stamp": stamp,
           "made_at": _now().isoformat(timespec="seconds")}
    if data:
        doc["data_sha256"], doc["data_bytes"] = data["sha256"], int(data["size"])
    if key is not None:
        doc["auth"] = auth_tag(doc, key)
    body = json.dumps(doc).encode()
    cmd = [AGE, "-a"]
    for r in recips:
        cmd += ["-r", r]
    p = subprocess.run(cmd, input=body, capture_output=True, timeout=60)
    if p.returncode != 0:
        raise BackupError("sealing the backup key failed: "
                          + _tail(p.stderr.decode(errors="replace")))
    return p.stdout


def age_decrypt_args(identity: str) -> tuple[list[str], int]:
    """(age -d argv, fd) with the identity in a pipe that age reads as a file:
    the key never touches a disk. The caller passes the fd with pass_fds and
    closes it after starting age."""
    rfd, wfd = os.pipe()
    try:
        os.write(wfd, (identity + "\n").encode())
    finally:
        os.close(wfd)
    return [AGE, "-d", "-i", f"/dev/fd/{rfd}"], rfd


def open_sidecar(armored: bytes, identity: str, *, stamp: str) -> dict:
    """Open a (resealed) sidecar with an age identity and check it names this
    backup."""
    argv, fd = age_decrypt_args(identity)
    try:
        p = subprocess.run(argv, input=armored, capture_output=True, timeout=60, pass_fds=(fd,))
    finally:
        os.close(fd)
    if p.returncode != 0:
        raise BackupError("this key file does not open with this Host's key ("
                          + _tail(p.stderr.decode(errors="replace"), 200) + ")")
    try:
        body = json.loads(p.stdout.decode())
    except ValueError:
        raise BackupError("the key file is not a Privatae backup key") from None
    if not isinstance(body, dict) or body.get("kind") != "privatae-backup-key" or body.get("v") != 1:
        raise BackupError("the key file is not a Privatae backup key")
    if body.get("stamp") != stamp or not str(body.get("object") or "").endswith(f"{stamp}.tar.zst.age"):
        raise BackupError("the key file is for a different backup")
    if not str(body.get("identity") or "").startswith("AGE-SECRET-KEY-1"):
        raise BackupError("the key file holds no key")
    return body


async def put_small(kind: str, name: str, data: bytes) -> dict:
    """A small object (a sidecar) through one presigned PUT, signed by the
    platform for exactly this many bytes."""
    got = await call("backup.put_url", {"kind": kind, "name": name, "size": len(data)})
    await asyncio.to_thread(_put, got["url"], data, 120, got.get("headers"))
    return {"key": got["key"], "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


# ── the Git repositories the Host owns ───────────────────────────────────────

async def _owned_repos() -> tuple[str, str, str, list[str]]:
    """(user, token, host, ['owner/name', ...]) for the repos this Host owns."""
    from server.routes.gitea_client import gitea_creds, gitea_request
    cred = gitea_creds()
    if not cred:
        return "", "", "", []
    user, token, host = cred
    names: list[str] = []
    page = 1
    while True:
        batch = await gitea_request("GET", "/api/v1/user/repos",
                                    params={"limit": 50, "page": page})
        if not isinstance(batch, list) or not batch:
            break
        names += [r["full_name"] for r in batch
                  if isinstance(r, dict) and (r.get("owner") or {}).get("login") == user
                  and r.get("full_name")]
        if len(batch) < 50:
            break
        page += 1
    return user, token, host, names


async def mirror_repos(workdir: Path) -> tuple[Optional[Path], dict]:
    """Mirror-clone the repos this Host owns into workdir/git. (folder or None,
    the part's record). A clone that fails fails the part, naming it; the rest
    are still saved."""
    user, token, host, names = await _owned_repos()
    if not names:
        return None, {"ok": True, "repos": 0}
    from server.routes.gitea_client import gitea_proxy
    mirrors = workdir / "git"
    mirrors.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"CSERIES_ENTITY_GIT_TOKEN": token, "GIT_TERMINAL_PROMPT": "0",
                "https_proxy": gitea_proxy(), "HTTPS_PROXY": gitea_proxy()})
    helper = ('!f() { echo "username=%s"; echo "password=$CSERIES_ENTITY_GIT_TOKEN"; }; f'
              % user)
    failed = []
    for full in names:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "credential.helper=", "-c", f"credential.helper={helper}",
            "clone", "--mirror", "--quiet", f"https://{host}/{full}.git",
            str(mirrors / f"{full.replace('/', '__')}.git"),
            env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            failed.append(f"{full}: {_tail(err.decode(errors='replace'), 160)}")
    part: dict[str, Any] = {"ok": not failed, "repos": len(names) - len(failed)}
    if failed:
        part["error"] = "; ".join(failed)[:800]
    return mirrors, part


# ── one run ──────────────────────────────────────────────────────────────────

async def run_once(pool, eid: int, reason: str) -> dict:
    """Take one backup now. Serialized: a second caller waits for the first."""
    async with _run_lock:
        return await _run(pool, eid, reason)


async def _store_uploader(store, stamp: Optional[str]):
    """An uploader into `store` for `stamp`, or for now. Two backups started
    in the same second would share a stamp; the second waits for the next."""
    for attempt in range(3):
        st = stamp or stamp_of(_now())
        try:
            return await asyncio.to_thread(store.uploader, st)
        except BackupError as e:
            if stamp or attempt == 2 or "already there" not in str(e):
                raise
            await asyncio.sleep(1.0)
    raise BackupError("no time stamp was free for this backup")


async def write_object(pool, eid: int, *, kind: str, name: Optional[str] = None,
                       stamp: Optional[str] = None, recips: list[str], who: list[dict],
                       with_git: bool = True, on_stamp=None,
                       on_counted=None, store=None) -> tuple[dict, dict]:
    """Capture and upload one file: (upload result with its stamp, parts).
    A backup's stamp is the platform's (Uploader); an export passes its own
    name and stamp. With a `store` (server/backup_stores.py: a folder on this
    machine, or the owner's bucket) the file goes there instead, stamped by
    this Host's clock. `on_stamp(stamp)` is awaited as soon as the stamp is
    known, and `on_counted()` called once the list's limit is checked and
    passed. Aborts the upload on any failure, so a half-written object is never
    left looking whole. Callers hold _run_lock."""
    shutil.rmtree(WORKDIR, ignore_errors=True)
    WORKDIR.mkdir(mode=0o700, parents=True)
    try:
        # The list's limit first, before the database is dumped or anything is
        # sent: the apps tree, then (once mirrored) the Git trees. The stamp
        # is not known yet; a placeholder of its length stands in.
        listed = _Listing(len(json.dumps(_manifest_head(
            eid, stamp or "0" * 16, _now().isoformat(timespec="seconds"), kind, who))))
        await asyncio.to_thread(precount, APPS_ROOT, None, listed)
        if with_git:
            git_dir, git_part = await mirror_repos(WORKDIR)
        else:
            git_dir, git_part = None, {"ok": True, "repos": 0}
        if git_dir:
            await asyncio.to_thread(_add_tree, None, git_dir, "gitea-repos", listed)
        if on_counted is not None:
            on_counted()
        dbname = await pool.fetchval("SELECT current_database()")
        # Listed before the stamp and the upload: what each app's data takes
        # is not known until it is read, so nothing here waits on it.
        app_data = await app_data_listing()
        if store is not None:
            up = await _store_uploader(store, stamp)
            stamp = up.stamp
        else:
            sync = _syncer(asyncio.get_running_loop())
            up = await asyncio.to_thread(Uploader, sync, kind, name)
            stamp = up.stamp if name is None else stamp
        try:
            if on_stamp is not None:
                await on_stamp(stamp)
            written = await asyncio.to_thread(
                write_file, upload=up, recips=recips, eid=eid, stamp=stamp, kind=kind,
                dbname=dbname, git_dir=git_dir, who=who, app_data=app_data)
            result = dict(await asyncio.to_thread(up.complete), stamp=stamp)
        except BaseException:
            await asyncio.to_thread(up.abort)
            raise
    finally:
        shutil.rmtree(WORKDIR, ignore_errors=True)
    parts = written["parts"]
    parts["db"]["ok"] = True
    parts["apps"]["ok"] = not parts["apps"].get("unreadable") and not parts["apps"].get("left_out")
    if parts["apps"].get("left_out") and not parts["apps"].get("unreadable"):
        parts["apps"]["error"] = _left_out_text(parts["apps"], APPS_ROOT)
    git_notes = {k: parts["gitea"][k] for k in ("left_out", "left_out_files", "left_out_names")
                 if k in parts["gitea"]}
    parts["gitea"] = {**git_part, "bytes": parts["gitea"].get("bytes", 0), **git_notes}
    if git_notes.get("left_out") and parts["gitea"].get("ok"):
        parts["gitea"].update(ok=False, error=_left_out_text(git_notes, None))
    return result, parts


LEFT_OUT_IN_TEXT = 5    # left-out names spelled out in the run's sentence


def _left_out_text(notes: dict, root: Optional[str], limit: Optional[int] = None) -> str:
    """The run's sentence for the names it left out: how many, how many files
    went with them, what to do, and then where the first few are (on the Host,
    under `root`, for the apps tree; as stored, for the Git mirrors), each
    written with its odd characters as codes. A run that leaves nothing out is
    whole again, so renaming or removing them is all it takes. With `limit`,
    only as many whole paths as fit in `limit` characters are spelled out, the
    rest counted: the instruction comes first and is never the part cut."""
    n = int(notes.get("left_out") or 0)
    files = int(notes.get("left_out_files") or n)
    names = notes.get("left_out_names") or []

    def where(e):
        path = e["path"]
        if root is not None and path.startswith("apps/"):
            path = root.rstrip("/") + "/" + path[len("apps/"):]
        if "files" in e:
            return f"{path} (a folder, {e['files']} file(s) in it)"
        return path
    head = (f"{n} name(s) with a line break or another control character in them were left out, "
            f"{files} file(s) in all, because a restore could not bring them back. Rename or remove "
            f"them and the next backup is whole again")

    def said(shown):
        more = n - len(shown)
        return (head + (f". Where they are: {'; '.join(shown)}" if shown else "")
                + (f", and {more} more" if shown and more > 0 else ""))
    shown: list[str] = []
    for e in names[:LEFT_OUT_IN_TEXT]:
        if limit is not None and len(said(shown + [where(e)])) > limit:
            break
        shown.append(where(e))
    return said(shown)


RUN_ERROR_MAX = 800     # the longest error a run records


def _partial_error(parts: dict) -> str:
    """A partial run's error: one clause for each part not whole, apps, the
    apps' own data, then gitea. Together they fit RUN_ERROR_MAX, each clause getting its share, so
    a long list of paths in one cannot push out the other; a clause that
    names left-out paths is shortened by spelling out fewer of them
    (_left_out_text), never by cutting its instruction."""
    bad = [p for p in ("apps", "app_data", "gitea")
           if not (parts.get(p) or {"ok": p == "app_data"}).get("ok")]
    texts = {p: (parts.get(p) or {}).get("error") or "some files could not be read" for p in bad}
    label = {"app_data": "app data"}
    room = RUN_ERROR_MAX - len("; ") * (len(bad) - 1) - sum(len(f"{label.get(p, p)}: ") for p in bad)
    for i, p in enumerate(sorted(bad, key=lambda q: len(texts[q]))):
        share = room // (len(bad) - i)
        if len(texts[p]) > share:
            part = parts.get(p) or {}
            root = APPS_ROOT if p == "apps" else None
            if part.get("left_out") and texts[p] == _left_out_text(part, root):
                texts[p] = _left_out_text(part, root, share)
            texts[p] = texts[p][:share]
        room -= len(texts[p])
    return "; ".join(f"{label.get(p, p)}: {texts[p]}" for p in bad)[:RUN_ERROR_MAX]


RESTORE_IN_PROGRESS_TEXT = ("a restore has not finished yet, so backups are paused. They start "
                            "again as soon as it is done; if this stays, restart your host")


def restore_in_progress() -> bool:
    """True from the moment a restore's databases are swapped until it is
    finished: until then the Host may still be put back to what it was, and
    a backup of a Host that is about to be undone would be one of neither.
    A restore only staged is not in progress: the Host is still as it was."""
    from server import host_restore
    plan = host_restore.read_plan()
    return bool(plan) and plan.get("state") not in (host_restore.STAGED_STATE, host_restore.FAILED)


async def _run(pool, eid: int, reason: str, keep: tuple = ()) -> dict:
    """One run. `keep`: stamps of staged copies this run must not drop (the
    copy a restore is about to read)."""
    if restore_in_progress():
        rid = await pool.fetchval(
            "INSERT INTO host_backup_runs (reason, state, finished_at, error) "
            "VALUES ($1, 'failed', now(), $2) RETURNING id", reason, RESTORE_IN_PROGRESS_TEXT)
        return {"run_id": rid, "reason": reason, "state": STATE_FAILED, "parts": {},
                "error": RESTORE_IN_PROGRESS_TEXT, "object": None}
    bp = await _behavioral_params(pool, eid)
    await pin_recovery_key(pool, eid)
    recips = await recipients(pool, eid, bp)
    who = public_recipients(recips)
    keys = [r["recipient"] for r in recips]
    if not recips:
        return await _record_no_recipient(pool, reason)
    mode = host_mode()
    network = await network_backups(mode)
    where = destination_of(bp, mode, network)
    nowhere = network_unavailable(mode, network) if where == WHERE_NETWORK else None
    if nowhere:
        return await _record_no_destination(pool, reason, nowhere)
    if where == WHERE_NETWORK:
        kind = KIND_BACKUP if platform_on(bp) else KIND_STAGED
        stored = kind
    else:
        # The file goes straight to the owner's bucket or this machine's
        # folder: a backup in every respect, recorded by where it went.
        kind, stored = KIND_BACKUP, where
    stamp = None
    run_id = await pool.fetchval(
        "INSERT INTO host_backup_runs (reason, state, stored, recipients) "
        "VALUES ($1, 'running', $2, $3::jsonb) RETURNING id",
        reason, stored, json.dumps(who))

    async def got_stamp(st):
        nonlocal stamp
        stamp = st
        await pool.execute("UPDATE host_backup_runs SET stamp=$2 WHERE id=$1", run_id, st)
    counted = []
    stop = asyncio.Event()
    awake = asyncio.create_task(_keep_awake(stop))
    parts: dict[str, Any] = {}
    error = None
    obj: dict = {}
    side: dict = {}
    try:
        # Made before the dump, so the dump carries it.
        tag_key = await auth_key(pool)
        store = None
        if where == WHERE_S3:
            store = await s3_store(pool, eid, bp)
        elif where == WHERE_LOCAL:
            store = local_store()
            # What the run may drop to make room on a full disk (make_room):
            # never a copy a restore may be reading.
            store.protect = set(keep) | await _staged_in_use(pool)
        identity, pb_recipient = age_keys.new_pair()
        try:
            obj, parts = await write_object(
                pool, eid, kind=kind, recips=[pb_recipient] + keys, who=who,
                on_stamp=got_stamp, on_counted=lambda: counted.append(True), store=store)
            sealed = make_sidecar(keys, eid=eid, stamp=stamp,
                                  object_name=obj["key"].split("/", 1)[-1], identity=identity,
                                  data=obj, key=tag_key)
        finally:
            # From here on the per-backup key's private half exists only inside
            # the sealed sidecar.
            identity = None  # noqa: F841
        if store is not None:
            side = await asyncio.to_thread(store.put_sidecar, stamp, sealed)
        else:
            side = await put_small(SIDECAR_KIND[kind], stamp, sealed)
        if where == WHERE_LOCAL:
            # The folder keeps the newest; never one a restore may be reading.
            protect = {stamp, *keep} | await _staged_in_use(pool)
            await asyncio.to_thread(store.prune, local_keep(bp), protect)
        elif kind == KIND_STAGED:
            await _drop_older_staged(pool, stamp, keep)
    except Exception as e:  # the run's own failure, recorded and reported
        error = str(e)[:RUN_ERROR_MAX] or type(e).__name__
        if isinstance(e, TooManyFiles):
            error = (str(e) + TOO_MANY_FILES_NEXT)[:RUN_ERROR_MAX]
        # What retry_after backs off on. Not on Back up now (routes/backup.py
        # start_backup_run) once its count passed: the owner removed files and
        # asked, so the back-off ends there even if the list then grew past
        # the limit while the run read it.
        if isinstance(e, TooManyFiles) and not (reason == "manual" and counted):
            parts["too_many_files"] = e.count
        log.warning("backup: run %s failed: %s", run_id, error)
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await awake
    if error or not side:
        state = STATE_FAILED
    elif (all((parts.get(p) or {}).get("ok") for p in ("db", "apps", "gitea"))
          and (parts.get("app_data") or {"ok": True}).get("ok")):
        state = STATE_OK
    else:
        state = STATE_PARTIAL
        error = _partial_error(parts)
    parts["recipients"] = who
    kept = bool(side) and not (error and state == STATE_FAILED)
    finished = await pool.fetchval(
        "UPDATE host_backup_runs SET finished_at=now(), state=$2, object_key=$3, sidecar_key=$4, "
        "size_bytes=$5, sha256=$6, sidecar_sha256=$7, sidecar_bytes=$8, parts=$9::jsonb, error=$10 "
        "WHERE id=$1 RETURNING finished_at",
        run_id, state, obj.get("key") if kept else None, side.get("key") if kept else None,
        obj.get("size") if kept else None, obj.get("sha256") if kept else None,
        side.get("sha256"), side.get("size"), json.dumps(parts), error)
    report = {"run_id": run_id, "reason": reason, "state": state, "stamp": stamp,
              "stored": stored, "object": obj.get("key") if kept else None,
              "parts": parts, "error": error, "finished_at": finished.isoformat()}
    await _report(pool, report)
    return report


NO_RECIPIENT_TEXT = ("not backed up until this Host has a recovery key. Make one in the Backups "
                     "app: your browser makes it, shows it to you once, and you save it. Or, if "
                     "your account page holds one for this Host, check its fingerprint in "
                     "Settings, Backup against the one on the key you saved and use it there. "
                     "Either takes a code from your second factor (none yet? set one up in the "
                     "Network app: \"Second factor: Enroll\" at the top). Your Host backs up "
                     "from then on")

NO_DESTINATION_TEXT = ("not backed up: your backups are set to go to the network's storage, and "
                       "this Host is not on the network. Join it in Settings, Network, or choose "
                       "another place for your backups in Settings, Backup")

NO_NETWORK_STORAGE_TEXT = ("not backed up: your backups are set to go to the network's storage, and "
                           "the network does not keep backups yet. Choose a folder on this machine "
                           "or your own bucket in Settings, Backup")


async def _record_not_attempted(pool, reason: str, state: str, text: str) -> dict:
    rid = await pool.fetchval(
        "INSERT INTO host_backup_runs (reason, state, finished_at, error, recipients) "
        "VALUES ($1, $2, now(), $3, '[]'::jsonb) RETURNING id",
        reason, state, text)
    return {"run_id": rid, "reason": reason, "state": state, "parts": {},
            "error": text, "object": None}


async def _record_no_recipient(pool, reason: str) -> dict:
    """Nobody to encrypt to: nothing is written. One row says so, so the run
    list and Settings can say why there is no backup."""
    return await _record_not_attempted(pool, reason, STATE_NO_RECIPIENT, NO_RECIPIENT_TEXT)


async def _record_no_destination(pool, reason: str, text: str = NO_DESTINATION_TEXT) -> dict:
    """Told to use the network's storage while not on the network, or on a
    network that keeps no backups (network_unavailable gives the text):
    nothing is written, here or anywhere else (a backup that went somewhere
    the owner did not choose is one they do not know about). One row says so."""
    return await _record_not_attempted(pool, reason, STATE_NO_DESTINATION, text)


async def destination_problem(pool, eid: int) -> Optional[str]:
    """Why this Host cannot back up to where its backups go, or None."""
    bp = await _behavioral_params(pool, eid)
    mode = host_mode()
    network = await network_backups(mode)
    if destination_of(bp, mode, network) == WHERE_NETWORK:
        return network_unavailable(mode, network)
    return None


# ── how the backups are going, in one line ───────────────────────────────────
#
# What Settings, Backup and the Backups app open with, and what the Host reads
# (GET /api/backup/runs `status`): one verdict, said plainly, and when the last
# backup finished. An owner who had just keyed a Host could not tell from the
# tab whether backups were working, and a Host restored onto a
# new machine stops backing up until its owner adds their recovery key again,
# because a restore keeps this machine's keys and a new machine has none
# (host_restore.CARRIED_STATE): that state is named here, not left to a run
# list that shows nothing new.
PAUSED_HEADLINE = "Backups are paused: add your recovery key"
PAUSED_TEXT = ("This Host has no recovery key, so it has nobody to encrypt a backup to and takes "
               "none. Add yours in the Backups app: \"Make a recovery key\", or \"I already have a "
               "recovery key\" for the key you keep. Backups start again at the next scheduled time")
RESTORED_WITHOUT_KEY = ("It was restored from a backup onto this machine, and a restore keeps this "
                        "machine's own keys, which had no recovery key yet.")
# A message in the Host's own list (context store inbox/messages.json), read
# into its next turn as "Messages For You" (runtime/session.py), once per
# pause: keyed to the last good backup before it (PAUSED_TOLD_STATE).
PAUSED_TOLD_STATE = "paused_told"


def _iso(t) -> Optional[str]:
    return t.isoformat() if t else None


async def backup_status(pool, eid: int) -> dict:
    """{state, headline, detail, last_finished_at, last_ok_at, running}.

    state is one of: unavailable, restoring, off, paused (no recovery key),
    no_destination, failing (the newest backup failed), working (the newest
    succeeded, or partly: `partial`), none_yet. last_finished_at is when the
    newest backup that kept anything finished; last_attempt_at when the newest
    one of any outcome did. A restore is not a backup and counts as neither.
    While a restore is in progress the runs table is not read: it is the
    restored one from the swap on, and the restore is what there is to say."""
    base = {"last_finished_at": None, "last_ok_at": None, "last_attempt_at": None,
            "running": _run_lock.locked(), "partial": False}
    problem = configured()
    if problem:
        return dict(base, state="unavailable", headline="Backups cannot run on this Host", detail=problem)
    if restore_in_progress():
        return dict(base, state="restoring", headline="Backups are paused while a restore finishes",
                    detail=RESTORE_IN_PROGRESS_TEXT[0].upper() + RESTORE_IN_PROGRESS_TEXT[1:] + ".")
    last_ok = await pool.fetchrow(
        "SELECT finished_at, state FROM host_backup_runs "
        "WHERE state IN ('ok', 'partial') AND reason <> 'restore' "
        "ORDER BY started_at DESC, id DESC LIMIT 1")
    last = await pool.fetchrow(
        "SELECT finished_at, state, error FROM host_backup_runs "
        "WHERE state IN ('ok', 'partial', 'failed') AND reason <> 'restore' "
        "ORDER BY started_at DESC, id DESC LIMIT 1")
    out = dict(base,
               last_finished_at=_iso(last_ok["finished_at"]) if last_ok else None,
               last_ok_at=(_iso(last_ok["finished_at"])
                           if last_ok and last_ok["state"] == STATE_OK else None),
               last_attempt_at=_iso(last["finished_at"]) if last else None)

    def says(state: str, headline: str, detail: str = "") -> dict:
        return dict(out, state=state, headline=headline, detail=detail)

    bp = await _behavioral_params(pool, eid)
    if schedule_of(bp, eid) is None:
        return says("off", "Backups are off",
                    "This Host takes no backups until you turn them on again below.")
    if not await recipients(pool, eid, bp):
        return says("paused", PAUSED_HEADLINE, PAUSED_TEXT + ".")
    nowhere = await destination_problem(pool, eid)
    if nowhere:
        return says("no_destination", "Backups are paused: choose where they go",
                    nowhere[0].upper() + nowhere[1:] + ".")
    if last is None:
        return says("none_yet", "No backup yet",
                    "Your Host takes its first one at its next scheduled time, or when you press "
                    "Back up now.")
    if last["state"] == STATE_FAILED:
        return says("failing", "The last backup failed",
                    (last["error"] or "it did not finish") + ". Your Host tries again on its own.")
    out["partial"] = last["state"] == STATE_PARTIAL
    return says("working", "Backups are working",
                "The last one saved only part of the Host." if out["partial"] else "")


async def tell_host_paused(pool, eid: int, why: str = "") -> bool:
    """Tell the Host, once per pause, that backups are paused for want of a
    recovery key, so it tells its owner on its next turn. Only a Host that has
    backed up before is paused; one that never had a key reads the same
    verdict in GET /api/backup/runs. Best effort; True when a message was
    written."""
    after = await pool.fetchval(
        "SELECT max(id) FROM host_backup_runs WHERE state IN ('ok', 'partial') "
        "AND reason <> 'restore'")
    if after is None:
        return False
    mark = await _state(pool, PAUSED_TOLD_STATE)
    if mark and mark.get("after_run") == after:
        return False
    import uuid
    from mastercode.context_store import ContextStore
    text = ("Your backups are paused: you have no recovery key, so you take no backups. "
            + (why + " " if why else "")
            + "Tell the owner now, plainly, and that they add their recovery key in the Backups "
              "app (\"Make a recovery key\", or \"I already have a recovery key\" for the key they "
              "keep). You cannot do it for them: the key is made and kept in their browser. Backups "
              "start again at the next scheduled time after they do.")
    await ContextStore(pool, eid).append_to_list("inbox/messages.json", {
        "id": str(uuid.uuid4()),
        "content": text,
        "source": "backups",
        "action": "backups_paused",
        "timestamp": _now().isoformat(),
        "delivered_to_session": None,
        "read": False,
    }, max_items=50)
    await _set_state(pool, PAUSED_TOLD_STATE, {"after_run": after, "at": _now().isoformat()})
    log.info("backup: paused for want of a recovery key; the Host was told")
    return True


# The stamp of a staged object, from the name the platform lists it by:
# s/<stamp>.tar.zst.age and s/<stamp>.key.age (privatae host_backup_store.py
# Store.list: the name is the key relative to the Host's folder, so it keeps
# its s/). A bare <stamp>... is read the same way.
_NAME_STAMP = re.compile(r"(?:^|/)([0-9]{8}T[0-9]{6}Z)\.(?:tar\.zst|key)\.age$")


def stamp_of_name(name: str) -> Optional[str]:
    m = _NAME_STAMP.search(name or "")
    return m.group(1) if m else None


async def _staged_in_use(pool) -> set[str]:
    """Stamps of staged copies a restore may still read: the one a restore is
    staging or has staged, any a bastion put back (push:<stamp>), and any whose
    key is resealed to this Host and waiting."""
    from server import host_restore
    out: set[str] = set()
    plan = host_restore.read_plan()
    if plan and plan.get("stamp"):
        out.add(str(plan["stamp"]))
    with contextlib.suppress(Exception):
        progress = await _state(pool, "restore")
        if progress and progress.get("state") == "staging" and progress.get("stamp"):
            out.add(str(progress["stamp"]))
    with contextlib.suppress(Exception):
        rows = await pool.fetch("SELECT k FROM host_backup_state WHERE k LIKE 'push:%'")
        out |= {str(r["k"])[len("push:"):] for r in rows}
    with contextlib.suppress(Exception):
        if host_restore.RESEALED_DIR.is_dir():
            out |= {p.name[:-len(".age")] for p in host_restore.RESEALED_DIR.iterdir()
                    if STAMP_RE.match(p.name[:-len(".age")])}
    return out


async def _drop_older_staged(pool, keep_stamp: str, keep: tuple = ()) -> list[str]:
    """A staged copy exists only to be sent on, and the second copy sends the
    newest, so an older one this Host wrote and never sent is dropped once a
    newer one exists. Nothing else is: not a copy a restore may read (a bastion
    puts a copy back into s/ to restore it, and a restore takes a backup of the
    Host first, which lands here), not the copy of how the Host was just before
    a restore (backup_ship sends that one on: get_undo_point), and not a copy
    this Host has no record of writing. The platform drops staged copies after
    a day in any case. Returns the keys dropped."""
    protected = {keep_stamp, *keep} | await _staged_in_use(pool)
    dropped: list[str] = []
    with contextlib.suppress(Exception):
        rows = await pool.fetch(
            "SELECT r.stamp FROM host_backup_runs r WHERE r.stored = 'staged' "
            "AND r.stamp IS NOT NULL AND r.reason <> 'before-restore' "
            "AND NOT EXISTS (SELECT 1 FROM backup_ship_log l "
            "                WHERE l.export_id = r.id AND l.state = 'done')")
        superseded = {str(r["stamp"]) for r in rows}
        listed = await call("backup.list", {"kind": KIND_STAGED})
        for o in listed.get("objects") or []:
            st = stamp_of_name(str(o.get("name") or ""))
            if st and st < keep_stamp and st in superseded and st not in protected:
                await call("backup.drop", {"key": o["key"]})
                dropped.append(o["key"])
    return dropped


async def host_key(pool) -> str:
    """This Host's vault public key (base64 of 32 raw bytes), reported with
    every run: what a backup's per-backup key is resealed to, so that this
    Host can restore it."""
    import base64
    from server.vault import identity as vault_identity
    return base64.b64encode(await vault_identity.host_pubkey(pool)).decode()


async def _report(pool, report: dict) -> None:
    """Tell the platform how the run ended, so a Host whose backups stop is
    noticed without anyone reading its data. Best effort. A run with nobody
    to encrypt to is not reported: nothing was attempted. Nor is any run of a
    standalone Host: it is on no network, and tells nobody."""
    if report["state"] not in (STATE_OK, STATE_PARTIAL, STATE_FAILED):
        return
    if host_mode() == MODE_STANDALONE:
        return
    bc = _bridge()
    if bc is None:
        return
    parts = report.get("parts") or {}
    payload = {
        "state": report["state"], "reason": report["reason"], "repo": None,
        "error": report.get("error"), "finished_at": report["finished_at"],
        "parts": {**{p: {k: v for k, v in (parts.get(p) or {}).items()
                         if k in ("ok", "bytes", "files", "repos", "chunks")}
                     for p in ("db", "apps", "gitea")},
                  "recipients": parts.get("recipients") or []},
    }
    with contextlib.suppress(Exception):
        payload["host_key"] = await host_key(pool)
    with contextlib.suppress(Exception):
        await bc.request("backup.report", payload)


async def _keep_awake(stop: asyncio.Event) -> None:
    """Keep the idle freeze away while a run is going: entity.busy stamps the
    lease's lastActiveAt, which is what the freeze measures from."""
    bc = _bridge()
    while not stop.is_set():
        if bc is not None:
            with contextlib.suppress(Exception):
                await bc.request("entity.busy", {"busy": True})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), BUSY_EVERY_S)
    if bc is not None:
        with contextlib.suppress(Exception):
            await bc.request("entity.busy", {"busy": False})


def _tail(text: str, n: int = 400) -> str:
    return " ".join((text or "").split())[-n:]


# ── state ────────────────────────────────────────────────────────────────────

async def _state(pool, key: str) -> Optional[dict]:
    v = await pool.fetchval("SELECT v FROM host_backup_state WHERE k = $1", key)
    return json.loads(v) if isinstance(v, str) else v


async def _set_state(pool, key: str, value: dict) -> None:
    await pool.execute(
        "INSERT INTO host_backup_state (k, v, updated_at) VALUES ($1, $2::jsonb, now()) "
        "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now()",
        key, json.dumps(value))


async def _drop_state(pool, key: str) -> None:
    await pool.execute("DELETE FROM host_backup_state WHERE k = $1", key)


async def last_runs(pool, limit: int = 5) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, started_at, finished_at, reason, state, stamp, stored, object_key, "
        "size_bytes, recipients, parts, error "
        "FROM host_backup_runs ORDER BY started_at DESC, id DESC LIMIT $1", limit)
    out = []
    for row in rows:
        d = dict(row)
        for k in ("parts", "recipients"):
            if isinstance(d.get(k), str):
                d[k] = json.loads(d[k])
        out.append(d)
    return out


# ── an export: the whole Host as one file, to take anywhere ──────────────────
#
# The owner asks for it in the Backups app; it is a backup in every respect
# (the same stream, encrypted to the same keys, with its key file), written to
# this machine's folder under exports/ whatever place the backups go, and
# downloaded from there as one file (server/backup_stores.py bundle). Any
# C-Series Host restores it (Backups app, "Restore from a file"). It is not a
# backup run: it is recorded in host_backup_state (EXPORT_STATE), so the
# schedule, the heartbeat's last good backup and the second copy never count
# it. One is kept at a time, for EXPORT_KEEP_DAYS.
EXPORT_STATE = "local_export"
EXPORT_KEEP_DAYS = 7


async def export_to_file(pool, eid: int) -> dict:
    """Write one export now. Returns {state, stamp, size} or raises
    BackupError saying why there is none."""
    async with _run_lock:
        if restore_in_progress():
            raise BackupError(RESTORE_IN_PROGRESS_TEXT)
        await pin_recovery_key(pool, eid)
        recips = await recipients(pool, eid)
        if not recips:
            raise BackupError("there is no export until this Host has a recovery key to encrypt it "
                              "to: make one first")
        keys = [r["recipient"] for r in recips]
        who = public_recipients(recips)
        store = local_store("exports")
        await _set_state(pool, EXPORT_STATE, {"state": "running", "at": _now().isoformat()})
        try:
            tag_key = await auth_key(pool)
            identity, pb_recipient = age_keys.new_pair()
            try:
                obj, parts = await write_object(pool, eid, kind=KIND_BACKUP,
                                                recips=[pb_recipient] + keys, who=who, store=store)
                sealed = make_sidecar(keys, eid=eid, stamp=obj["stamp"],
                                      object_name=obj["key"].split("/", 1)[-1], identity=identity,
                                      data=obj, key=tag_key)
            finally:
                identity = None  # noqa: F841
            await asyncio.to_thread(store.put_sidecar, obj["stamp"], sealed)
            await asyncio.to_thread(store.prune, 1, {obj["stamp"]})
        except Exception as e:
            error = str(e)[:RUN_ERROR_MAX] or type(e).__name__
            await _set_state(pool, EXPORT_STATE, {"state": "failed", "error": error,
                                                  "at": _now().isoformat()})
            raise BackupError(error) from None
        done = {"state": "ok", "stamp": obj["stamp"], "size": obj["size"], "at": _now().isoformat(),
                "partial": not all((parts.get(p) or {}).get("ok") for p in ("apps", "gitea"))}
        await _set_state(pool, EXPORT_STATE, done)
        return done


def expire_local(now: Optional[dt.datetime] = None, protect: Optional[set] = None) -> list[str]:
    """Drop exports and uploaded files older than EXPORT_KEEP_DAYS, except
    `protect` (a restore may be reading one). Returns what was dropped."""
    from server import backup_stores
    now = now or _now()
    gone = []
    for sub in ("exports", "uploads"):
        store = local_store(sub)
        for stamp in list(store.pairs()):
            try:
                mtime = (store.folder / f"{stamp}{backup_stores.DATA_SUFFIX}").stat().st_mtime
            except OSError:
                continue
            made = dt.datetime.fromtimestamp(mtime, dt.timezone.utc)
            if now - made >= dt.timedelta(days=EXPORT_KEEP_DAYS) and stamp not in (protect or set()):
                store.drop(stamp)
                gone.append(f"{sub}/{stamp}")
    return gone


# ── the loop ─────────────────────────────────────────────────────────────────

def configured() -> Optional[str]:
    """None when this Host can back up, else what is missing."""
    if not Path(AGE).is_file():
        return f"age is not installed at {AGE}"
    return None


async def next_action(pool, eid: int, now: dt.datetime, booted: dt.datetime) -> Optional[tuple[str, str]]:
    """What the loop does this minute: ("run", why), ("no_recipient", why),
    ("no_destination", why) or None. Pure over the database, so the gate can
    drive it."""
    if restore_in_progress():
        return None
    bp = await _behavioral_params(pool, eid)
    sched = schedule_of(bp, eid)
    last = await pool.fetchrow(
        "SELECT started_at, state FROM host_backup_runs "
        "WHERE state NOT IN ('running', 'no_recipient', 'no_destination') "
        "ORDER BY started_at DESC LIMIT 1")
    last_ok = await pool.fetchval(
        "SELECT max(started_at) FROM host_backup_runs WHERE state IN ('ok', 'partial')")
    retry = RETRY_AFTER
    if last and last["state"] == "failed":
        retry = retry_after(await pool.fetch(
            "SELECT parts FROM host_backup_runs "
            "WHERE state NOT IN ('running', 'no_recipient', 'no_destination') "
            "ORDER BY started_at DESC LIMIT 16"))
    why = due(sched, now, last_ok=last_ok,
              last_attempt=last["started_at"] if last else None,
              last_failed=bool(last and last["state"] == "failed"),
              last_request=_last_request_at, retry=retry)
    if why and last is None and first_backup_waits(eid, booted, now):
        return None
    if not why:
        return None
    has_key = bool(await recipients(pool, eid, bp))
    if not has_key:
        # Maybe the owner saved their recovery key since the last look.
        await pin_recovery_key(pool, eid)
        has_key = bool(await recipients(pool, eid, bp))
    mode = host_mode()
    blocked = None
    if not has_key:
        blocked = STATE_NO_RECIPIENT
    else:
        network = await network_backups(mode)
        if (destination_of(bp, mode, network) == WHERE_NETWORK
                and network_unavailable(mode, network)):
            blocked = STATE_NO_DESTINATION
    if blocked is None:
        return ("run", why)
    # Nothing can be written. One row per scheduled slot says so; checking
    # every minute adds nothing.
    seen = await pool.fetchval(
        "SELECT max(started_at) FROM host_backup_runs WHERE state = $1", blocked)
    if seen is None or seen < last_slot(sched, now):
        return (blocked, why)
    return None


async def run_host_backup_loop(pool, eid: int) -> None:
    """Started once at boot (server/main.py). Checks every minute whether a
    backup is due and runs it; never raises out of the loop."""
    missing = configured()
    if missing:
        log.warning("backup: not running backups: %s", missing)
        return
    await pool.execute(
        "UPDATE host_backup_runs SET state='failed', finished_at=now(), "
        "error='interrupted: the Host restarted during this run' WHERE state='running'")
    booted = _now()
    tick = 0
    while True:
        try:
            await asyncio.sleep(LOOP_SECONDS)
            tick += 1
            if tick % EXPIRE_TICKS == 0:
                if host_mode() != MODE_STANDALONE:
                    from server import host_export
                    with contextlib.suppress(Exception):
                        await host_export.expire_old(pool, eid)
                protect = await _staged_in_use(pool)
                for gone in await asyncio.to_thread(expire_local, None, protect):
                    log.info("backup: dropped %s, older than %d days", gone, EXPORT_KEEP_DAYS)
            if _run_lock.locked():
                continue
            act = await next_action(pool, eid, _now(), booted)
            if act is None:
                continue
            if act[0] == STATE_NO_RECIPIENT:
                await _record_no_recipient(pool, act[1])
                try:
                    await tell_host_paused(pool, eid)
                except Exception as e:  # noqa: BLE001 — the record above is what counts
                    log.warning("backup: the Host could not be told backups are paused: %s", e)
            elif act[0] == STATE_NO_DESTINATION:
                # The reason, read again as the row is written. If it no
                # longer holds (the network answered in between), nothing is
                # recorded: a row with the standalone text would tell a member
                # it is on no network, and the next minute runs the backup.
                nowhere = await destination_problem(pool, eid)
                if nowhere:
                    await _record_no_destination(pool, act[1], nowhere)
            else:
                await run_once(pool, eid, act[1])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - the loop must survive anything
            log.warning("backup: loop error: %s", e)
