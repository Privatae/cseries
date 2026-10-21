"""
Restoring a Host from one of its backups.

A backup (server/host_backup.py) is one age-encrypted file. This Host cannot
open it: it is encrypted to the keys the user registered and to a key made for
that backup alone, whose private half sits in the backup's sidecar, sealed to
the same registered keys. So a restore starts with the user:

  1. KEY. The sidecar has to be sealed again to THIS Host's own vault key.
     Either the owner's browser does it (Settings: they paste or scan their
     Host recovery key, the page opens the sidecar, checks it, seals the same
     contents to this Host's recipient and sends only that; the recovery key
     never leaves the browser), or their bastion does it (mgmt op
     backup_reseal: the bastion opens the sidecar with the key it keeps, the
     person approves on the machine, and it seals the same contents to the key
     this Host names). Only then can this Host open that one backup. The
     resealed key is kept until the restore is over, and dropped then.
     The resealed sidecar also says whether this Host made the backup: its
     tag verifies under this Host's backup-auth key (provenance). A backup
     that does not verify (a lost Host's, or one this Host never wrote) is
     restored only when the owner says so, seeing why.
  2. STAGE, while the Host runs. Back up how it is now (so a restore can be
     undone from the same list), then stream the chosen file from where it is
     kept (the network's storage, the user's own S3, or the folder on this
     machine, which also holds exports and a file the owner uploaded:
     receive_upload) through age, zstd and tar:
     the database pieces go into pg_restore into a second database
     (hostmem_restore), the apps into .host-restore/apps on the apps volume.
     Write the plan; restart the kernel.
  3. SWAP, at boot, from entrypoint.sh before the schema step. Rename the two
     databases in one transaction, then move files between the staged tree
     and the live one. FILES ONLY, never directories: nine app-state/ folders
     are bind-mounted into the kernel container, and a renamed directory would
     leave those mounts pointing at the old copy.
  4. STARTED. When the kernel is up on the restored data (server/main.py
     lifespan) the plan moves to FINISHING, and from then on nothing ever
     puts the old data back: the restored data is the Host.
  5. FINISH. Drop what the swap set aside and record the restore. Resumable:
     a failure part way through is finished on the next try, never undone.

If the kernel cannot start on the restored data, entrypoint.sh runs undo()
when the boot stops before the server starts, and swap() counts boots that end
without step 4: the third one puts the previous database and files back. Undo
refuses once the previous database is gone, because then the restored data is
the only data there is.

What a backup leaves out (node_modules, .venv, caches, crash dumps, scratch;
host_backup.APPS_EXCLUDES) a restore leaves where it is. The Git repositories
are not written back: they live on the platform's Git server, and pushing a
mirror over them would erase what was committed since. Every backup file still
carries them, for the user.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat as _stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple, Optional

from server import age_keys
from server import backup_stores
from server import host_backup as hb

log = logging.getLogger("privatae.pc.host_restore")

LIVE = Path(hb.APPS_ROOT)             # the apps volume
RESTORE_DIR = LIVE / ".host-restore"
STAGED = RESTORE_DIR / "apps"          # the restored apps, staged here first
# Each app's kept data from the backup (host_backup._add_app_data), one tar a
# app, staged beside the apps and put back by the local runtime once the
# kernel has started on the restored database (finish, _restore_app_data).
APP_DATA = RESTORE_DIR / "app-data"
PREVIOUS = RESTORE_DIR / "previous"    # what the swap moved out of the way
PLAN = RESTORE_DIR / "plan.json"
MANIFEST = RESTORE_DIR / "manifest"    # every path the staged tree holds
RESTORE_DB = "hostmem_restore"
LIVE_DBS = ("hostmem", "parallax")     # entrypoint.sh resolves which one is live
BOOTS_BEFORE_UNDO = 3
# Room the staged copy needs on the apps volume beyond the file's own size.
SPACE_MARGIN = 256 * 1024 * 1024
READ_CHUNK = 1 << 20

# States, in order. Each names what is DONE.
STAGED_STATE = "staged"   # both copies staged; nothing live touched
DB_DONE = "db"            # databases swapped
ASIDE_DONE = "aside"      # live files the backup does not hold moved to previous/
SWAPPED = "swapped"       # restored files moved in; waiting for the kernel to start
FINISHING = "finishing"   # the kernel started on the restored data: never undone
UNDOING = "undoing"       # putting the previous data back; resumed until done
FAILED = "failed"

WHERE = ("platform", "s3", "local")
DATA_SUFFIX = ".tar.zst.age"
SIDECAR_SUFFIX = ".key.age"
# A stamp at the end of an object name: b/<stamp>.tar.zst.age, or the second
# copy's <stamp>-<run>-<stamp>.tar.zst.age (backup_ship.object_name).
_STAMP_AT_END = re.compile(r"([0-9]{8}T[0-9]{6}Z)\.(tar\.zst|key)\.age$")


class RestoreError(Exception):
    """A restore refused or failed, with a message for the user."""


class Room(NamedTuple):
    """What a filesystem has left for this process (statvfs)."""
    free: int           # bytes it may still write
    unit: int           # what it says it allocates in (f_frsize); see _alloc_unit
    inodes_free: int    # files, folders and links it may still make
    inodes: int         # all it holds; 0 on a filesystem that keeps no count
    total: int = 0      # all its bytes; 0 when not read


def _room(path) -> Room:
    """The room on the filesystem holding `path`. Both room checks read it
    here: stage, before the backup file is fetched, and unpack, which spends it
    entry by entry. One reading each; the rest of the Host keeps writing
    meanwhile, which is what the reserves are for."""
    st = os.statvfs(path)
    return Room(st.f_bavail * st.f_frsize, st.f_frsize, st.f_favail, st.f_files,
                st.f_blocks * st.f_frsize)


def _alloc_unit(folder: Path, reported: int) -> int:
    """What one small file really takes on the filesystem holding `folder`:
    one byte written to a probe file and synced, then its st_blocks. f_frsize
    is not that everywhere: NFS reports its transfer size and CephFS its
    object size (up to 4 MiB), which would charge a 10-byte file megabytes and
    refuse a restore that fits. When no probe can be made, the smaller of
    f_frsize and 4096, the usual unit on a local disk."""
    probe = folder / ".alloc-probe"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"\0")
            os.fsync(fd)
            blocks = os.fstat(fd).st_blocks
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                probe.unlink()
        if blocks > 0:
            return blocks * 512
    except OSError:
        pass
    return min(reported, 4096) if reported > 0 else 4096


def _share(name: str) -> float:
    """The fraction of the disk an operator's setting says to keep free: 0 up
    to, not including, 1. Anything else (a percentage typed as 15, a negative,
    nan, text) is refused by name, since a silent fallback would leave the node
    on a threshold the operator did not choose, and a share of 1 or more would
    refuse every restore."""
    raw = os.environ[name]
    try:
        v = float(raw)
    except ValueError:
        v = float("nan")
    if not 0.0 <= v < 1.0:
        raise RestoreError(f"{name} is {raw!r}: it must be a fraction of the disk from 0 up to "
                           "but not including 1 (0.15 for 15%); fix the setting and try again")
    return v


def in_cluster() -> bool:
    """Does this Host run as a pod on a Kubernetes node? The kubelet puts
    KUBERNETES_SERVICE_HOST into every pod's environment, service links or
    not. A C-Series machine runs the Host under podman (vm/wsl,
    cseries-host.container), where nothing sets it."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def _reserve(room: Room) -> tuple[int, int | None, str]:
    """(bytes, inodes, where the shares came from) a restore must leave free.

    IN THE CLUSTER (in_cluster) the apps volume is a folder on the node's own
    disk, shared with every Host on the node and the kubelet. Below its
    eviction threshold the kubelet starts evicting pods, other Hosts'
    included, and the restored data stays (it is on a persistent volume,
    which eviction does not count or free). So a restore stays above that
    threshold: the larger of SPACE_MARGIN and the threshold's share of the
    disk, and the same for inodes. The shares come from Kubernetes'
    documented defaults: bytes 0.15, because nodefs.available<10% and
    imagefs.available<15% are one signal when images and the kubelet share a
    disk, as they do here (the Host nodes' kubelets run nodefs<10%,
    imagefs<15%); inodes 0.05, nodefs.inodesFree<5% (those kubelets do not
    watch inodes at all, so this reserve is the only thing keeping a node's
    inodes).

    ON ONE MACHINE (a C-Series machine, a managed VPS) there is no kubelet and
    nothing is evicted: the disk holds one Host, its database and the system.
    What must not run out is the database's room for its write-ahead log and
    the system's own. A restore keeps the larger of SINGLE_DISK_FREE_MIN and
    SINGLE_DISK_FREE_SHARE of the disk:
      * 2 GiB covers the most write-ahead log this image's PostgreSQL may hold
        (entrypoint-postgres.sh: max_wal_size 256MB, and up to
        max_slot_wal_keep_size 1024MB more for the owner's memory mirror),
        with room left for the system's logs and an image pull;
      * 5% is the share ext4 itself reserves on a new filesystem (mke2fs -m's
        default) for the same reason, so it scales the floor on a large disk.
    15% would refuse a restore that fits on a small disk: 15 GB of a 100 GB
    disk kept empty for a kubelet that is not there. Inodes keep 5%, as in the
    cluster.

    CSERIES_NODE_EVICTION_FREE / CSERIES_NODE_EVICTION_INODES_FREE (fractions,
    for the operator to set from the node's kubelet config, which this pod
    cannot read) replace the share in either case."""
    if in_cluster():
        src = "Kubernetes' documented eviction defaults"
        share, floor = EVICTION_FREE_SHARE, SPACE_MARGIN
    else:
        src = "the reserve for one machine's disk"
        share, floor = SINGLE_DISK_FREE_SHARE, SINGLE_DISK_FREE_MIN
    ishare = INODE_MARGIN
    if "CSERIES_NODE_EVICTION_FREE" in os.environ:
        share = _share("CSERIES_NODE_EVICTION_FREE")
        src = "CSERIES_NODE_EVICTION_FREE"
    if "CSERIES_NODE_EVICTION_INODES_FREE" in os.environ:
        ishare = _share("CSERIES_NODE_EVICTION_INODES_FREE")
        src += ", CSERIES_NODE_EVICTION_INODES_FREE"
    nbytes = max(floor, int(room.total * share))
    inodes = int(room.inodes * ishare) if room.inodes else None
    return nbytes, inodes, src


# What one restored entry takes beyond its bytes. Measured on ext4
# with the Host nodes' features (4 KiB units, no inline data): 20,000
# files of 10 bytes took 82 MB, not 200 KB, and 20,000 empty folders took 82 MB,
# not nothing. A file takes whole units and a folder at least one. Every name
# also takes a directory entry in its folder: 8 bytes plus the name rounded up
# to 4 (ext4's record), in blocks that splitting leaves partly empty. Measured
# at 1.5 times the record for names of 6 to 255 bytes, so it is charged twice.
# Each entry also takes an inode, out of what the node keeps free (_reserve).
# A link whose target is shorter than LINK_INLINE bytes keeps it in its inode
# and takes no unit (ext4, measured: 59 bytes took none, 60 took one).
EVICTION_FREE_SHARE = 0.15
INODE_MARGIN = 0.05
# On one machine's disk (_reserve says why).
SINGLE_DISK_FREE_SHARE = 0.05
SINGLE_DISK_FREE_MIN = 2 * 1024 * 1024 * 1024
LINK_INLINE = 60


def _units(n: int, unit: int) -> int:
    """n bytes, as the whole allocation units they take."""
    return -(-max(0, int(n)) // unit) * unit


def _name_cost(name: str) -> int:
    """What a name takes in its folder's directory blocks."""
    return 2 * (8 + _units(len(os.fsencode(name)), 4))


# ── a backup's key, resealed to this Host, held only until the restore uses it ─
#
# NOT in the database: everything in it goes into the next backup (the one a
# restore takes first, among others), and a table is where a dump finds it.
# Not on the apps volume either, for the same reason. A private folder on the
# pod's own /tmp, one file per backup, dropped when staging has read it, on
# cancel, on any failure, at every finish, and after RESEALED_TTL_S whatever
# happens.
RESEALED_DIR = Path(os.environ.get("HOST_RESTORE_KEYS_DIR", "/tmp/host-restore-keys"))
RESEALED_TTL_S = 3600


def _resealed_path(stamp: str) -> Path:
    if not hb.STAMP_RE.match(stamp or ""):
        raise RestoreError("choose a backup from the list")
    return RESEALED_DIR / f"{stamp}.age"


def keep_resealed(stamp: str, armored: bytes) -> None:
    RESEALED_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RESEALED_DIR, 0o700)
    path = _resealed_path(stamp)
    tmp = path.with_suffix(".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(armored)
    os.replace(tmp, path)


def load_resealed(stamp: str) -> Optional[bytes]:
    """The resealed key for one backup, or None. One older than
    RESEALED_TTL_S is dropped instead."""
    path = _resealed_path(stamp)
    try:
        if time.time() - path.stat().st_mtime > RESEALED_TTL_S:
            path.unlink()
            return None
        return path.read_bytes()
    except FileNotFoundError:
        return None


def drop_resealed(stamp: Optional[str] = None) -> None:
    """Drop one backup's resealed key, or all of them."""
    if stamp is not None:
        with contextlib.suppress(FileNotFoundError):
            _resealed_path(stamp).unlink()
        return
    if RESEALED_DIR.is_dir():
        for p in RESEALED_DIR.iterdir():
            with contextlib.suppress(FileNotFoundError):
                p.unlink()


# ── the plan ─────────────────────────────────────────────────────────────────

def read_plan() -> Optional[dict]:
    try:
        return json.loads(PLAN.read_text())
    except FileNotFoundError:
        return None
    except ValueError:
        return {"state": FAILED, "error": "the restore plan on this Host is unreadable"}


def _write_plan(plan: dict) -> None:
    RESTORE_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = PLAN.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan))
    tmp.chmod(0o600)
    os.replace(tmp, PLAN)


# The states in which the restored database is in place (or being put in
# place) and an app's data from the backup may still be waiting to go back.
_APP_DATA_WAITS = (DB_DONE, ASIDE_DONE, SWAPPED, FINISHING)
APP_DATA_PENDING = "pending"
APP_DATA_RESTORED = "restored"
APP_DATA_NOT_RESTORED = "not restored"


def app_data_pending() -> frozenset:
    """The apps whose kept data this restore has yet to put back: from the
    database swap until finish() has handed each one to the local runtime.
    Their wakes wait (local_runtime.LocalRuntimeClient), so no app starts on
    the data it is about to lose. Empty with no plan, or a staged one (the
    Host still runs as it was)."""
    plan = read_plan()
    if not plan or plan.get("state") not in _APP_DATA_WAITS:
        return frozenset()
    return frozenset(a.get("app_id") for a in plan.get("app_data") or []
                     if isinstance(a, dict) and a.get("state") == APP_DATA_PENDING and a.get("app_id"))


def _drop_plan() -> None:
    for p in (PLAN, MANIFEST):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


# ── what can be restored ─────────────────────────────────────────────────────

def stamp_in(name: str) -> Optional[str]:
    m = _STAMP_AT_END.search(name or "")
    return m.group(1) if m else None


def pair_objects(objects: list, where: str) -> dict[str, dict]:
    """{stamp: {where, key, sidecar_key, bytes, taken_at}} for every backup
    file whose sidecar is next to it. A file without its sidecar cannot be
    restored here (its key is in the sidecar) and is not offered."""
    data, side = {}, {}
    for o in objects or []:
        if not isinstance(o, dict):
            continue
        name = str(o.get("name") or o.get("key") or "")
        stamp = stamp_in(name)
        if not stamp:
            continue
        if name.endswith(DATA_SUFFIX):
            data[stamp] = o
        elif name.endswith(SIDECAR_SUFFIX):
            side[stamp] = o
    out = {}
    for stamp, o in data.items():
        if stamp not in side:
            continue
        taken = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        out[stamp] = {"where": where, "key": o.get("key") or o.get("name"),
                      "sidecar_key": side[stamp].get("key") or side[stamp].get("name"),
                      "bytes": int(o.get("size") or 0), "taken_at": taken.isoformat()}
        # The sha256 the Host declared when the object landed, as the platform
        # recorded it (privatae host_backup_api list; absent for copies put
        # back by a bastion and for the owner's own storage).
        if _SHA.fullmatch(str(o.get("sha256") or "")):
            out[stamp]["sha256"] = o["sha256"]
    return out


async def _platform_backups() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for kind in (hb.KIND_STAGED, hb.KIND_BACKUP):   # a retained copy wins
        listed = await hb.call("backup.list", {"kind": kind})
        found.update(pair_objects(listed.get("objects") or [], "platform"))
    return found


async def _s3_target(pool, eid: int) -> Optional[dict]:
    from server.routes import backup as backup_routes
    eff = backup_routes._effective(await backup_routes._load_bp(eid))
    t = eff.get("target")
    return t if t and t.get("kind") == "s3" else None


def _s3_client(creds: dict, target: dict):
    """The owner's bucket, through the one client that checks its address
    (backup_stores.s3_client): a restore reads from where backups go."""
    try:
        return backup_stores.s3_client(creds, target)
    except hb.BackupError as e:
        raise RestoreError(str(e)) from None


async def _s3_backups(pool, eid: int) -> dict[str, dict]:
    target = await _s3_target(pool, eid)
    if not target:
        return {}
    from server import backup_ship
    creds = await backup_ship._s3_credential(pool, eid, target["cred_name"])
    prefix = f"{target['prefix']}/" if target.get("prefix") else ""

    def listing():
        client = _s3_client(creds, target)
        out, token = [], None
        while True:
            kw = {"Bucket": target["address"], "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = client.list_objects_v2(**kw)
            out += [{"key": o["Key"], "size": o.get("Size")} for o in page.get("Contents") or []]
            if not page.get("IsTruncated"):
                return out
            token = page.get("NextContinuationToken")
    return pair_objects(await asyncio.to_thread(listing), "s3")


def _local_backups() -> dict[str, dict]:
    """The backups in the folder on this machine: a file the owner uploaded,
    then the exports, then the folder's own backups (which win on a stamp
    they share)."""
    found: dict[str, dict] = {}
    for sub in ("uploads", "exports", ""):
        found.update(hb.local_store(sub).pairs())
    return found


async def list_backups(pool, eid: int) -> tuple[list[dict], list[str]]:
    """Every backup this Host could restore, newest first, and what could not
    be listed. The network's storage is asked only by a Host on a network."""
    found: dict[str, dict] = {}
    problems = []
    try:
        found.update(await _s3_backups(pool, eid))
    except Exception as e:
        problems.append(f"your own storage could not be listed: {e}")
    try:
        found.update(await asyncio.to_thread(_local_backups))
    except Exception as e:
        problems.append(f"the backup folder on this machine could not be listed: {e}")
    mode = hb.host_mode()
    if mode != hb.MODE_STANDALONE:
        try:
            found.update(await _platform_backups())
        except Exception as e:
            where = "the network's storage" if mode == hb.MODE_MEMBER else "platform storage"
            problems.append(f"{where} could not be listed: {e}")
    return [dict(v, stamp=k) for k, v in sorted(found.items(), reverse=True)], problems


async def bastions(pool) -> list[dict]:
    """The user's connected bastions, for choosing which one reseals a key."""
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (h.token_id) h.token_id, h.hostname, h.last_seen_at "
            "FROM network_bastion_heartbeat h JOIN mcp_client_tokens t ON t.id = h.token_id "
            "WHERE t.revoked_at IS NULL AND t.operator_kind = 'bastion' "
            "ORDER BY h.token_id, h.last_seen_at DESC")
    except Exception:
        return []
    return [{"token_id": r["token_id"], "name": r["hostname"],
             "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None}
            for r in rows]


async def host_recipient(pool) -> str:
    from server.vault import identity
    return age_keys.recipient(await identity.host_pubkey(pool))


async def available(pool, eid: int) -> dict:
    """What Settings shows: the backups, whether each one's key is ready, the
    bastions that can reseal a key, and any restore that is waiting or just
    happened."""
    plan = read_plan()
    result: dict[str, Any] = {"backups": [], "pending": None, "problems": []}
    if plan:
        result["pending"] = {k: plan.get(k) for k in ("state", "taken_at", "stamp", "staged_at", "error")}
    missing = hb.configured()
    if missing:
        result["problem"] = missing
        return result
    result["host_recipient"] = await host_recipient(pool)
    result["bastions"] = await bastions(pool)
    backups, problems = await list_backups(pool, eid)
    result["problems"] = problems
    listed = {b["stamp"] for b in backups}
    held, bproblems = await bastion_copies(pool, eid)
    result["problems"] += [f"a bastion's copies could not be listed ({p})" for p in bproblems]
    own = await machine_copies(pool)
    for stamp in sorted(set(held) | set(own), reverse=True):
        if stamp in listed:
            continue
        c = held.get(stamp) or own[stamp]
        taken = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        backups.append({"stamp": stamp, "where": "machine", "machine": c["address"],
                        "token_id": c.get("token_id"), "bytes": c["bytes"],
                        "hash_pending": bool(c.get("hash_pending")),
                        "too_big": int(c["bytes"] or 0) > PUSH_BACK_MAX,
                        "taken_at": taken.isoformat()})
    backups.sort(key=lambda b: b["stamp"], reverse=True)
    for b in backups:
        push = await hb._state(pool, f"push:{b['stamp']}")
        if push:
            b["push"] = {k: push.get(k) for k in ("state", "error", "at")}
        b["key_ready"] = load_resealed(b["stamp"]) is not None
        if b["key_ready"]:
            # Known once the key is sealed here: the tag is in the sidecar.
            b["verified"] = await verified(pool, b["stamp"])
        job = await hb._state(pool, f"reseal:{b['stamp']}")
        if job:
            b["reseal"] = {k: job.get(k) for k in ("state", "bastion", "job_id", "error", "at")}
    result["backups"] = backups
    return result


# ── the key: resealed to this Host ───────────────────────────────────────────

async def _host_identity(pool) -> str:
    from server.vault import identity
    return age_keys.identity_from_pem(await identity.host_privkey(pool))


async def _fetch_small(pool, eid: int, entry: dict, key_field: str) -> bytes:
    """A sidecar's bytes, from wherever the backup is kept."""
    key = entry[key_field]
    if entry["where"] == "local":
        try:
            return await asyncio.to_thread(hb.local_store().read_small, key)
        except hb.BackupError as e:
            raise RestoreError(str(e)) from None
    if entry["where"] == "platform":
        url = (await hb.call("backup.get_url", {"key": key}))["url"]

        def get():
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read(1 << 20)
        return await asyncio.to_thread(get)
    target = await _s3_target(pool, eid)
    if not target:
        raise RestoreError("your own storage is no longer set as a destination")
    from server import backup_ship
    creds = await backup_ship._s3_credential(pool, eid, target["cred_name"])

    def get_s3():
        body = _s3_client(creds, target).get_object(Bucket=target["address"], Key=key)["Body"]
        return body.read(1 << 20)
    return await asyncio.to_thread(get_s3)


async def _entry(pool, eid: int, stamp: str, where: Optional[str] = None) -> dict:
    """The listed backup of `stamp`, from `where` when given. A file the
    owner uploaded or exported shares the list with the network's storage,
    which wins on a stamp both hold (list_backups), so a restore from a file
    names its place: the backup restored is the one the owner chose, the one
    whose key file their browser resealed."""
    if where == "local":
        local = await asyncio.to_thread(_local_backups)
        if stamp in local:
            return dict(local[stamp], stamp=stamp)
        raise RestoreError("that backup is not on this machine any more: upload it again")
    backups, problems = await list_backups(pool, eid)
    found = next((b for b in backups if b["stamp"] == stamp), None)
    if found is None:
        raise RestoreError("that backup is not in the list any more"
                           + (f" ({'; '.join(problems)})" if problems else ""))
    return found


async def sidecar_for_browser(pool, eid: int, stamp: str) -> dict:
    """What the owner's browser needs to reseal one backup's key to this Host
    with their recovery key: the sealed key file (ciphertext), this Host's own
    recipient, and what the browser checks the file against. The recovery key
    itself never comes here (routes/backup.py refuses any body carrying one)."""
    entry = await _entry(pool, eid, stamp)
    if entry["where"] == "machine":
        raise RestoreError("this backup is only on one of your machines: bring it back first")
    sidecar = await _fetch_small(pool, eid, entry, "sidecar_key")
    return {"stamp": stamp, "entity_id": eid, "host_recipient": await host_recipient(pool),
            "sidecar": sidecar.decode("ascii")}


UNVERIFIED_TEXT = ("this Host cannot confirm it made this backup. That is expected for a backup of "
                   "a Host you replaced; otherwise it may not be yours. Restore it only if you "
                   "know where it came from")


async def provenance(pool, body: dict) -> bool:
    """True when a (resealed) sidecar's tag was made by a key this Host holds:
    the backup is one this Host wrote (host_backup, 'proof that a backup is
    this Host's own'). False for anything else, including a sidecar with no
    tag and a Host whose keys cannot be read."""
    try:
        keys = await hb.auth_keys(pool)
    except Exception as e:
        log.warning("restore: the backup-auth keys could not be read: %s", e)
        return False
    return hb.auth_verifies(body, keys)


async def verified(pool, stamp: str) -> Optional[bool]:
    """Whether the backup whose key is resealed here verifies; None when no
    key is held for it."""
    held = load_resealed(stamp)
    if not held:
        return None
    try:
        body = await asyncio.to_thread(hb.open_sidecar, held, await _host_identity(pool), stamp=stamp)
    except hb.BackupError:
        return None
    return await provenance(pool, body)


async def accept_resealed(pool, stamp: str, armored: bytes, via: str) -> dict:
    """Keep a resealed key for one backup, after proving THIS Host's key opens
    it and that it names this backup. Says whether the backup verifies as
    this Host's own."""
    if not hb.STAMP_RE.match(stamp or ""):
        raise RestoreError("choose a backup from the list")
    try:
        body = await asyncio.to_thread(hb.open_sidecar, armored, await _host_identity(pool), stamp=stamp)
    except hb.BackupError as e:
        raise RestoreError(str(e)) from None
    armored.decode("ascii", errors="strict")
    keep_resealed(stamp, armored)
    await hb._drop_state(pool, f"reseal:{stamp}")
    return {"key_ready": True, "stamp": stamp, "verified": await provenance(pool, body)}


async def _bastion_op(pool, eid: int, token_id: int, op: str, req: dict) -> dict:
    """One mgmt op on the user's bastion, through the signed-grant gate
    (server/network/nac_mgmt.run_mgmt_op). Returns the JSON it printed."""
    from server.network import nac_mgmt
    host = await pool.fetchval(
        "SELECT hostname FROM network_bastion_heartbeat WHERE token_id=$1 "
        "ORDER BY last_seen_at DESC LIMIT 1", token_id)
    if not host:
        raise RestoreError("that bastion is not connected to this Host")
    try:
        res = await nac_mgmt.run_mgmt_op(pool, eid, host, op, [json.dumps(req)],
                                         token_id=token_id, origin="route",
                                         note="restoring a backup")
    except Exception as e:
        raise RestoreError(f"your bastion could not be reached: {e}") from None
    if res.get("exit") == 126:
        raise RestoreError(
            f"your bastion {host} has not been granted {op}. In the Network app, approve a "
            f"management grant for this machine covering the operation named {op}, then try "
            "again. " + (res.get("stderr") or "")[:300])
    if res.get("exit") != 0:
        raise RestoreError((res.get("stderr") or "your bastion refused")[:500])
    try:
        return json.loads(res.get("stdout") or "{}")
    except ValueError:
        raise RestoreError(f"your bastion answered something unreadable: {res.get('stdout')!r:.200}") from None


async def request_reseal(pool, eid: int, stamp: str, token_id: int) -> dict:
    """Ask the user's bastion to seal this backup's key again to this Host.
    The bastion asks the person on the machine first; poll_reseal follows."""
    entry = await _entry(pool, eid, stamp)
    sidecar = await _fetch_small(pool, eid, entry, "sidecar_key")
    to = await host_recipient(pool)
    got = await _bastion_op(pool, eid, token_id, "backup_reseal", {
        "op": "start", "sidecar": sidecar.decode("ascii"), "to": to, "stamp": stamp,
        "host_label": f"Host #{eid}"})
    job_id = got.get("job_id") or got.get("id")
    if not job_id:
        raise RestoreError(f"your bastion gave no job for the key: {got!r:.200}")
    state = {"state": "pending", "job_id": job_id, "token_id": token_id,
             "bastion": await pool.fetchval(
                 "SELECT hostname FROM network_bastion_heartbeat WHERE token_id=$1 "
                 "ORDER BY last_seen_at DESC LIMIT 1", token_id),
             "at": hb._now().isoformat()}
    await hb._set_state(pool, f"reseal:{stamp}", state)
    return state


async def poll_reseal(pool, eid: int, stamp: str) -> Optional[dict]:
    job = await hb._state(pool, f"reseal:{stamp}")
    if not job or job.get("state") != "pending":
        return job
    try:
        got = await _bastion_op(pool, eid, int(job["token_id"]), "backup_reseal",
                                {"op": "status", "job_id": job["job_id"]})
    except RestoreError as e:
        job.update(error=str(e))
        await hb._set_state(pool, f"reseal:{stamp}", job)
        return job
    st = got.get("state")
    if st == "done" and got.get("sidecar"):
        try:
            await accept_resealed(pool, stamp, str(got["sidecar"]).encode("ascii"),
                                  via=f"bastion {job.get('bastion')}")
            return {"state": "done"}
        except RestoreError as e:
            job.update(state="failed", error=str(e))
    elif st in ("refused", "failed", "unknown"):
        job.update(state="failed" if st != "refused" else "refused",
                   error=got.get("error") or ("refused on your bastion" if st == "refused"
                                              else "the bastion lost the job; ask again"))
    await hb._set_state(pool, f"reseal:{stamp}", job)
    return job


# ── a file the owner uploads, to restore from ────────────────────────────────
#
# A backup the owner holds as a file (one this Host's Backups app let them
# download, or an export from another C-Series Host) comes back through their
# browser: its key file first, then the backup itself in chunks, each sent at
# the offset this Host already holds, so a dropped connection carries on from
# there. It lands in the folder on this machine, under uploads/, and is
# restored from there like any other backup (the owner's browser reseals its
# key file to this Host first, as always). Ciphertext only: a body carrying a
# private key is refused, wherever it falls. One upload is kept at a time
# (a new one replaces it, never one a restore is reading), for
# host_backup.EXPORT_KEEP_DAYS, and never past the room the machine needs.
UPLOAD_REQUEST_MAX = 256 * 1024 * 1024      # what one request may carry
_SECRET_MARK = b"AGE-SECRET-KEY-"
_ARMOR_HEAD = b"-----BEGIN AGE ENCRYPTED FILE-----"


def upload_status(stamp: str) -> dict:
    """How much of an upload this Host holds."""
    store = hb.local_store("uploads")
    data = store.folder / f"{stamp}{DATA_SUFFIX}"
    part = store.folder / f".{stamp}{DATA_SUFFIX}.part"
    done = data.is_file()
    size = data.stat().st_size if done else (part.stat().st_size if part.is_file() else 0)
    return {"stamp": stamp, "key_file": (store.folder / f"{stamp}{SIDECAR_SUFFIX}").is_file(),
            "size": int(size), "done": done}


def _uploads_in_use() -> set[str]:
    out: set[str] = set()
    plan = read_plan()
    if plan and plan.get("stamp"):
        out.add(str(plan["stamp"]))
    with contextlib.suppress(OSError):
        if RESEALED_DIR.is_dir():
            out |= {p.name[:-len(".age")] for p in RESEALED_DIR.iterdir()
                    if hb.STAMP_RE.match(p.name[:-len(".age")])}
    return out


def _other_uploads(folder: Path, stamp: str) -> tuple[list[str], int]:
    """One upload at a time: the files of any other upload, which this one
    replaces once it is accepted, and the bytes they hold. Raises when a
    restore is reading one of them."""
    if not folder.is_dir():
        return [], 0
    names = [n for n in os.listdir(folder) if stamp not in n]
    held = {s for s in (backup_stores.stamp_of_key(n.lstrip(".").removesuffix(".part")) for n in names) if s}
    if held & _uploads_in_use():
        raise RestoreError("a restore is reading the file uploaded before this one; wait for it "
                           "to finish, or cancel it, then upload again")
    size = 0
    for n in names:
        with contextlib.suppress(OSError):
            size += os.lstat(folder / n).st_size
    return names, size


def _drop_uploads(folder: Path, names: list[str]) -> None:
    for n in names:
        with contextlib.suppress(OSError):
            (folder / n).unlink()


def _scan(carry: bytes, chunk: bytes) -> bytes:
    """Raise if a private key's mark is in `chunk`, counting the end of the
    bytes before it (`carry`); the new carry."""
    seen = (carry + chunk).upper()
    if _SECRET_MARK in seen:
        raise RestoreError("that file carries a private key. Keep your recovery key to yourself: "
                           "upload only the backup and its key file, never the key that opens them")
    return seen[-(len(_SECRET_MARK) - 1):]


async def receive_upload(stamp: str, part: str, offset: int, total: int, chunks,
                         final: bool) -> dict:
    """Take one request's worth of an upload. `part` is "key" (the key file,
    whole, in one request) or "data" (the backup, from `offset`, which must be
    what this Host already holds; 0 starts it again). `total` is the whole
    file's size, announced up front: the room is checked against it, and on
    `final` the file must be exactly that long. `chunks` yields the bytes."""
    if not hb.STAMP_RE.match(stamp or ""):
        raise RestoreError("name the backup by its time stamp, as its file is named")
    if part not in ("key", "data"):
        raise RestoreError("part must be key or data")
    try:
        total, offset = int(total), int(offset)
    except (TypeError, ValueError):
        raise RestoreError("total and offset are whole numbers") from None
    if total <= 0 or offset < 0 or offset > total:
        raise RestoreError("total must be the file's size, and offset within it")
    for sub in ("", "exports"):
        if stamp in hb.local_store(sub).pairs():
            raise RestoreError("this Host already holds that backup: restore it from the list")
    store = hb.local_store("uploads")
    folder = store.folder
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if part == "key":
        if offset != 0 or total > backup_stores.SIDECAR_MAX:
            raise RestoreError("a key file is sent whole, and is small")
        data, carry = b"", b""
        async for c in chunks:
            carry = _scan(carry, c)
            data += c
            if len(data) > total:
                raise RestoreError(f"the key file is longer than the {total} bytes announced")
        if len(data) != total:
            raise RestoreError(f"the key file is {len(data)} bytes, not the {total} announced")
        if not data.lstrip().startswith(_ARMOR_HEAD):
            raise RestoreError("that is not a backup's key file (it begins -----BEGIN AGE "
                               "ENCRYPTED FILE-----)")
        others, _ = _other_uploads(folder, stamp)
        await asyncio.to_thread(store.put_sidecar, stamp, data)
        _drop_uploads(folder, others)
        return upload_status(stamp)

    final_path = folder / f"{stamp}{DATA_SUFFIX}"
    tmp = folder / f".{stamp}{DATA_SUFFIX}.part"
    if final_path.exists():
        raise RestoreError("this file is already here whole: restore it from the list")
    have = tmp.stat().st_size if tmp.is_file() else 0
    others: list[str] = []
    if offset == 0:
        # The upload this one replaces is dropped once this one is accepted;
        # its room counts as free for this one.
        others, others_bytes = _other_uploads(folder, stamp)
        st = os.statvfs(folder)
        free = st.f_bavail * st.f_frsize + others_bytes
        keep = backup_stores.local_reserve(st.f_blocks * st.f_frsize)
        if free - total < keep:
            raise RestoreError(f"this machine has {free >> 20} MiB free and must keep {keep >> 20} MiB "
                               f"free to run, so a file of {total >> 20} MiB cannot be uploaded to it. "
                               "Free some space and try again")
        have = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    elif offset != have:
        raise RestoreError(f"this Host holds {have} bytes of that file: send it on from there")
    else:
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    carry = b""
    if have:
        with open(tmp, "rb") as f:
            f.seek(max(0, have - (len(_SECRET_MARK) - 1)))
            carry = f.read().upper()
    fd = os.open(tmp, flags, 0o600)
    sent = 0
    try:
        async for c in chunks:
            carry = _scan(carry, c)
            sent += len(c)
            if have + sent > total or sent > UPLOAD_REQUEST_MAX:
                raise RestoreError(f"more was sent than the {total} bytes announced"
                                   if have + sent > total else "send the file in smaller pieces")
            view = memoryview(c)
            while view:
                n = await asyncio.to_thread(os.write, fd, view)
                view = view[n:]
        os.fsync(fd)
    except RestoreError:
        # Refused: nothing of it is kept.
        os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    except BaseException:
        # The connection dropped: what arrived stays, and the next request
        # carries on from it (upload_status says where).
        with contextlib.suppress(OSError):
            os.fsync(fd)
        os.close(fd)
        raise
    os.close(fd)
    _drop_uploads(folder, others)
    size = have + sent
    if final:
        if size != total:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise RestoreError(f"the file arrived as {size} bytes, not the {total} announced: "
                               "upload it again")
        os.replace(tmp, final_path)
    return upload_status(stamp)


# ── a copy kept only on the user's bastion ───────────────────────────────────

async def machine_copies(pool) -> dict[str, dict]:
    """Backups this Host sent to one of the user's machines (server/backup_ship.py),
    by stamp: where each one is on that machine."""
    from server import backup_ship
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (r.id) r.id, r.stamp, r.object_key, r.sha256, r.sidecar_sha256, "
            "       r.size_bytes, r.sidecar_bytes, r.started_at, l.destination "
            "FROM host_backup_runs r JOIN backup_ship_log l ON l.export_id = r.id "
            "WHERE l.state = 'done' AND l.kind = 'machine' AND r.stamp IS NOT NULL "
            "ORDER BY r.id, l.finished_at DESC")
    except Exception:
        return {}
    out = {}
    for r in rows:
        address, _, folder = str(r["destination"] or "").partition(":")
        name = backup_ship.object_name(int(r["id"]), r["object_key"], r["started_at"])
        # In the bastion's own backup folder: "." to backup_push (the bastion
        # puts backup-named files there whatever folder the Host named).
        out[r["stamp"]] = {"address": address, "dir": ".",
                           "name": name, "sidecar_name": backup_ship.sidecar_name(name),
                           "sha256": r["sha256"], "sidecar_sha256": r["sidecar_sha256"],
                           "bytes": int(r["size_bytes"] or 0),
                           "sidecar_bytes": int(r["sidecar_bytes"] or 0)}
    return out


# A bastion puts a copy back with ONE presigned PUT, and a single PUT holds at
# most 5 GiB (S3's limit; privatae host_backup_store.py STAGED_PUT_MAX). A
# larger copy cannot come back that way; Settings says so when such a backup
# goes only to a machine (backup_ship), and the list marks it.
PUSH_BACK_MAX = 5 * 1024 ** 3
TOO_BIG_TO_BRING_BACK = (
    "this copy is larger than 5 GiB, the most your bastion can put back through platform storage "
    "in one piece, so it cannot be restored from here. Restore from a copy kept in platform "
    "storage or your own storage, or open it on your machine with your recovery key "
    "(the README inside says how)")

# How recently a bastion must have been seen for its copies to be listed.
BASTION_FRESH_S = 600
BASTION_LIST_TIMEOUT_S = 20.0
_SHA = re.compile(r"[0-9a-f]{64}")


async def bastion_copies(pool, eid: int) -> tuple[dict[str, dict], list[str]]:
    """Backups the owner's bastions hold (mgmt op backup_list), by stamp:
    the bastion's backup folder and anything the person shared with this
    Host. This is how a Host that replaced a lost one finds copies it has no
    record of sending. ({stamp: copy}, problems)."""
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (h.token_id) h.token_id, h.hostname, h.last_seen_at "
            "FROM network_bastion_heartbeat h JOIN mcp_client_tokens t ON t.id = h.token_id "
            "WHERE t.revoked_at IS NULL AND t.operator_kind = 'bastion' "
            "  AND h.last_seen_at > now() - ($1::text || ' seconds')::interval "
            "ORDER BY h.token_id, h.last_seen_at DESC", str(BASTION_FRESH_S))
    except Exception:
        return {}, []

    async def one(r):
        try:
            got = await asyncio.wait_for(
                _bastion_op(pool, eid, int(r["token_id"]), "backup_list", {"op": "list"}),
                BASTION_LIST_TIMEOUT_S)
            return r, got, None
        except (RestoreError, asyncio.TimeoutError) as e:
            return r, None, str(e) or "it did not answer in time"
    found: dict[str, dict] = {}
    problems = []
    for r, got, err in await asyncio.gather(*(one(r) for r in rows)):
        if err:
            problems.append(f"{r['hostname']}: {err}")
            continue
        for b in (got or {}).get("backups") or []:
            stamp = str(b.get("stamp") or "")
            # Relative to the bastion's backup folder: an absolute folder, or
            # one that climbs out, is not offered (checked before the slashes
            # are trimmed, or the absolute check could never fire).
            raw_folder = str(b.get("folder") or "")
            folder = raw_folder.strip("/")
            shas = (str(b.get("data_sha256") or ""), str(b.get("sidecar_sha256") or ""))
            if (not hb.STAMP_RE.match(stamp) or not b.get("has_sidecar") or not b.get("has_data", True)
                    or ".." in folder.split("/") or raw_folder.startswith("/")
                    or any(x and not _SHA.fullmatch(x) for x in shas)):
                continue
            found.setdefault(stamp, {
                "address": r["hostname"], "token_id": int(r["token_id"]), "dir": folder,
                "name": f"{stamp}{DATA_SUFFIX}", "sidecar_name": f"{stamp}{SIDECAR_SUFFIX}",
                "sha256": shas[0], "sidecar_sha256": shas[1],
                "bytes": int(b.get("data_bytes") or 0),
                "sidecar_bytes": int(b.get("sidecar_bytes") or 0),
                # The bastion hashes a file it has not seen in the background
                # and lists "" until then: offered, but not yet sendable.
                "hash_pending": not all(shas) or not b.get("data_bytes") or not b.get("sidecar_bytes")})
    return found, problems


async def request_push(pool, eid: int, stamp: str, token_id: int) -> dict:
    """Ask the user's bastion to put a copy it holds back into this Host's
    staging folder in platform storage (ciphertext, as it always was), so it
    can be restored like any other. Standing work on the bastion: its grant
    for backup_push is the approval."""
    listed, _ = await bastion_copies(pool, eid)
    copy = listed.get(stamp) if (listed.get(stamp) or {}).get("token_id") == token_id else None
    copy = copy or (await machine_copies(pool)).get(stamp)
    if copy is None:
        raise RestoreError("that bastion does not hold this backup")
    if isinstance(copy.get("bytes"), int) and copy["bytes"] > PUSH_BACK_MAX:
        raise RestoreError(TOO_BIG_TO_BRING_BACK)
    jobs = []
    for kind, name, sha, size in (
            ("staged", copy["name"], copy["sha256"], copy.get("bytes")),
            ("staged_sidecar", copy["sidecar_name"], copy["sidecar_sha256"], copy.get("sidecar_bytes"))):
        # The platform signs the upload for exactly this many bytes.
        if not _SHA.fullmatch(str(sha or "")) or not isinstance(size, int) or size <= 0:
            raise RestoreError("your bastion is still checking that copy; try again in a minute")
        signed = await hb.call("backup.put_url", {"kind": kind, "name": stamp, "size": size})
        # `headers`: what the PUT must carry (If-None-Match: *, signed into
        # the URL: storage refuses a second write and a PUT without it).
        got = await _bastion_op(pool, eid, token_id, "backup_push", {
            "op": "start", "path": f"{copy['dir']}/{name}".lstrip("/"), "url": signed["url"],
            "headers": signed.get("headers") or {}, "sha256": sha})
        job = got.get("job_id") or got.get("id")
        if not job:
            raise RestoreError(f"your bastion gave no job for the copy: {got!r:.200}")
        jobs.append(job)
    state = {"state": "pending", "jobs": jobs, "token_id": token_id, "at": hb._now().isoformat()}
    await hb._set_state(pool, f"push:{stamp}", state)
    return state


async def poll_push(pool, eid: int, stamp: str) -> Optional[dict]:
    job = await hb._state(pool, f"push:{stamp}")
    if not job or job.get("state") != "pending":
        return job
    states = []
    for j in job["jobs"]:
        try:
            got = await _bastion_op(pool, eid, int(job["token_id"]), "backup_push",
                                    {"op": "status", "job_id": j})
        except RestoreError as e:
            job.update(error=str(e))
            await hb._set_state(pool, f"push:{stamp}", job)
            return job
        states.append(got.get("state"))
        if got.get("state") in ("failed", "refused", "unknown"):
            job.update(state="failed", error=got.get("error") or "your bastion could not send the copy")
    if job["state"] == "pending" and all(s == "done" for s in states):
        job["state"] = "done"
    await hb._set_state(pool, f"push:{stamp}", job)
    return job


# ── a restore brings back DATA, never older AUTHORIZATION ───────────────────
#
# The swap replaces the whole database and the apps tree with a backup's. Most
# of that is the point. But some rows are not the user's data: they say who
# and what may reach this Host, and the state of a second factor. Restoring
# those would undo every revocation made since the backup was taken: a
# revoked bastion back on the Hub, a management grant alive again, the step-up
# secret of a lost phone. So once the restored database has been migrated,
# and BEFORE the kernel serves anything (entrypoint.sh: `host_restore carry`),
# these are copied over from the database that was live until the swap
# (<live>_before_restore), replacing what the backup held. Once only: after
# that the restored Host's own changes are the live ones.
#
# Carried whole (the live table replaces the restored one; public keys, grants
# and revocations only, no user data):
CARRIED_TABLES = (
    # bastions and machine clients: tokens and their revocations
    # (init-db.sql mcp_client_tokens; revoked_at is the barrier:
    # server/network/hub_peers.py _ACTIVE_BASTIONS_SQL builds the Hub's
    # WireGuard peers from it, and every mgmt lane checks it)
    "mcp_client_tokens",
    "bastion_pairing_tokens",          # enrolment tokens (v145)
    "network_bastion_pairing",         # a box's pairing with this Host (v133)
    "nac_pairing_grants",              # what a pairing may do (v133)
    "network_bastion_peer",            # bastion-to-bastion peering (v132)
    # what the live bastions last said, keyed by their token ids: restored rows
    # for other ids would misdirect the hostname and box-gate lookups that
    # read them (nac_mgmt.run_mgmt_op's token lookup, nac_approval box_gate)
    "network_bastion_heartbeat",       # v056, renamed v057
    "network_bastion_other",           # v136
    "bastion_update_refusals",         # v190
    "bastion_autoupdate_watch",        # v176
    # management and network access control: grants, their revocations,
    # holds and containment (server/network/nac_mgmt.py _gate_state)
    "nac_mgmt_grants", "nac_bastion_suspend", "nac_bastion_quarantine",
    "nac_agent_delegations", "nac_agent_contain", "nac_finding_contain",
    "nac_policy", "nac_profiles", "nac_egress", "nac_endpoints",
    # messaging connectors (v202): who may start turns through a connector app,
    # and that app's capability hash — a backup's would bring back a revoked
    # account or a capability the person cleared
    "nac_connector_grants", "nac_connector_caps",
    "service_reach_grants",            # machines' services a peer may reach (v180)
    "network_machine_offers",          # machine use granted to or by peers (v140)
    # who else may reach this Host, and what it granted to others
    "domain_policy",                   # which peers may message it (v048)
    "entity_domain_access", "entity_permissions",
    "peer_app_slips", "group_slips", "held_slips",
    "group_app_grants", "group_members",
    "browser_form_grant",              # sites the browser may fill (v141)
    "bastion_autoupdate_settings",     # whether bastions update themselves (v176)
    "application_signing_keys",        # the app trust store and its revocations
    # who a backup and a sealed credential go to
    "backup_user_key", "vault_identity_pubkey",
    # the owner's passkeys and recovery codes (v240): a backup's would bring
    # back a passkey the owner removed or a recovery code already spent
    "owner_passkeys", "owner_recovery_codes",
    # and the owner they sign in (v228), with the owner's sessions. The
    # passkeys and codes name no account (owner_passkeys.USER_HANDLE is one
    # constant) and sign in only while there is one (owner_login.needs_setup).
    # A backup's account is another password and another cookie name
    # (cookie_suffix), or, taken before the owner was made, no account at all:
    # the owner signed out and sent to /setup, whose code only the kernel's
    # log shows. Kept, the browser that asked for the restore stays signed in
    # while its session lasts, and the owner signs in as before the restore;
    # the backup's sessions do not come back. owner_note says so when the
    # backup's owner had another email.
    "owner_account", "owner_sessions",
)
# Carried as single rows: the second factor (server/network/nac_stepup.py:51,
# 52, 147) and the app-token signing secret (server/applications/tokens.py:187;
# rotating it is how app tokens are revoked).
CARRIED_SECRETS = ("nac_stepup_totp", "nac_stepup_enrolled", "nac_stepup_last_counter",
                   # the attempt limit: the owner's record, the Host's, and the one
                   # every app shares (nac_stepup.py); each app's own record is
                   # carried by its prefix (CARRIED_SECRET_PREFIXES)
                   "nac_stepup_failures", "nac_stepup_failures_host",
                   "nac_stepup_failures_apps",
                   "app_token",
                   # whether the Browser app may keep a signed-in profile
                   # (server/applications/browser_profile.py:55)
                   "browser_profile_enabled",
                   # the Hub's WireGuard key (server/network/hub_identity.py):
                   # every machine paired to this Host pinned the live one, and
                   # the backup's (or none, from before it existed) would leave
                   # them all dialling a key the Hub no longer answers
                   "hub_wg")
# Carried by prefix, every row whose id starts with one: an app's own record of
# wrong second-factor codes (nac_stepup.py _TOTP_APP_FAILURES_PREFIX). The live
# rows replace the backup's, and a record the backup holds for an app that has
# none now is dropped with them. Like the records above: a restore must not
# hand a guesser back the free tries its wrong codes since the backup used up.
CARRIED_SECRET_PREFIXES = ("nac_stepup_failures_app:",)
# A recovery key the account page offers (server/host_backup.py pin_recovery_key),
# and which key the owner made on this Host (host_backup.OWN_KEY_STATE): both
# say who backups go to, so they follow the live keys (backup_user_key, above).
CARRIED_STATE = ("recovery_offer", "recovery_own")
# DELIBERATELY LEFT AS THE BACKUP HAD THEM:
#  - kernel_secrets vault_host_x25519 (server/vault/identity.py:30): it opens
#    the credentials the vault holds (vault_envelope), which are restored with
#    it; a live key would open none of them.
#  - kernel_secrets entity_sign (server/applications/entity_signing.py:35), after
#    a verified restore: the Host's signing identity, which peers pinned; it
#    does not change unless the Host is re-made (a backup from another Host
#    number: it is carried, below). After an unverified restore it is carried
#    too (carry_rows): the trust store that must hold its public key is the
#    live one, and the backup's key could be anyone's.
#    The Hub's WireGuard key, which machines pin the same way, is carried
#    always instead (hub_wg in CARRIED_SECRETS): no restored data depends on it,
#    and every live machine needs the live one.
#  - kernel_secrets backup_auth (server/host_backup.py auth_key), after a
#    verified restore: the key the restored data's backups were tagged with;
#    the live one is kept beside it (_keep_auth_key), never in its place.
#    After an unverified restore the live keys replace the backup's.
#  - vault_envelope and the credentials the integrations store holds
#    (connections): user data, restored as of the backup.
#  - installed_applications: after a verified restore, apps and their manifest
#    permissions come back with the app files they describe. After an
#    unverified one neither comes back: each app is installed again
#    (leave_code_out, _reinstall_apps).
#  - audit trails (app_token_audit, nac_mgmt_audit, nac_agent_audit, vault_audit)
#    and contacts, peer_cards: records, not grants.
# The integrations store's grants (which app may use which connection,
# integration_grants, v201) are carried too, filtered to the connections the
# restored database has: _carry_integration_grants.


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _owner_email(conn) -> Optional[str]:
    """The email of the owner account in `conn` (v228; one row), or None."""
    if "email" not in await _columns(conn, "owner_account"):
        return None
    rows = await conn.fetch('SELECT "email" FROM "owner_account"')
    return str(rows[0]["email"]) if rows else None


def owner_note(live: Optional[str], backup: Optional[str]) -> Optional[str]:
    """What the restore says about the owner's sign-in, given the owner's
    email live until the swap and the one the backup held; None when the
    owner signs in as before and nothing needs saying. The live account is
    the one kept either way (CARRIED_TABLES)."""
    if live and backup and live.strip().lower() != backup.strip().lower():
        return (f"This backup came from a Host whose owner signed in with another email. A restore "
                f"keeps this Host's own sign-in, so you still sign in as {live}, with the password "
                f"or passkey you used before the restore.")
    if not live and backup:
        return ("This Host had no owner account when it was restored, so it has none now: a restore "
                "keeps this Host's own sign-in and does not bring back the backup's. Create the owner "
                "account at /setup.")
    return None


async def _columns(conn, table: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1 "
        "AND is_generated = 'NEVER' ORDER BY ordinal_position", table)
    return [r["column_name"] for r in rows]


# ── a backup from another Host number ───────────────────────────────────────
#
# A Host that replaced a lost one may have a new number. The restored database
# is then the lost Host's, keyed to ITS number, while this kernel serves as
# the live number (entrypoint.sh reads ENTITY_ID from relay.json, :166; the
# kernel finds itself in entity_registry by it, server/main.py:398). So,
# before the access state is carried, the restored database is renumbered:
#   KEPT and renumbered (the restored Host's data becomes this Host's):
#     entity_registry, the row of the backup's Host becomes the live number's
#     (its persona, settings and codebook are the data being restored);
#     every column that references entity_registry(id) (init-db.sql: entity_
#     memory, entity_episodes, entity_sessions, entity_tasks, channels.owner_
#     entity_id, ...; migrations v001, v029, v088, v093, v167, v169, v183);
#     every other column named entity_id, which in this schema always means
#     "this Host" (projects, circles_threads, calendar_events, tool_call_log,
#     ...);
#     vault_envelope.owner_identity host:<old> and user:<old> (server/vault/
#     identity.py:39,43): the Keys-app credentials, which the restored vault
#     key (kept, see above) opens;
#     integration_connections.user_id, the Host number as text (v201),
#     including the connections read in from a pre-v201 backup's SQLite file.
#   LEFT AS THEY ARE: columns that name OTHER Hosts (peer_entity_id,
#     member_entity_id, voter_entity_id, from/to_entity_id, publisher_,
#     cross_, handler_entity_id, groups.owner_entity_id): peers know the lost
#     Host by its old number, and this Host cannot speak for it.
#   DROPPED: authorization held under the old number. Every CARRIED_TABLES
#     table is replaced whole by the live one, so its old-number rows go; and
#     the old Host's signing identity (entity_sign) is replaced by this
#     Host's, which is the one its peers pinned (RENUMBER_CARRIED_SECRETS).
RENUMBER_CARRIED_SECRETS = ("entity_sign",)


async def _self_columns(conn) -> list[tuple[str, str]]:
    """(table, column) of every column that holds THIS Host's number: a
    column that references entity_registry(id), or an integer column named
    entity_id."""
    fks = await conn.fetch(
        "SELECT cl.relname AS table_name, a.attname AS column_name "
        "FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid "
        "JOIN pg_class rf ON rf.oid = c.confrelid "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "JOIN pg_namespace n ON n.oid = cl.relnamespace "
        "WHERE c.contype = 'f' AND rf.relname = 'entity_registry' AND n.nspname = current_schema()")
    named = await conn.fetch(
        "SELECT c.table_name, c.column_name FROM information_schema.columns c "
        "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE' "
        "AND c.column_name = 'entity_id' AND c.data_type IN ('smallint', 'integer', 'bigint')")
    out = {(r["table_name"], r["column_name"]) for r in list(fks) + list(named)}
    return sorted(x for x in out if x[0] not in CARRIED_TABLES and x[0] != "entity_registry")


# Columns that name THIS Host by number without a foreign key or the name
# entity_id: its side of Circles messages (init-db.sql inbox_messages
# from_entity_id/to_entity_id). Renumbered
# only where they hold the old number, i.e. where they name this Host.
SELF_REFS = (("inbox_messages", "from_entity_id"), ("inbox_messages", "to_entity_id"))
# Recorded in the restored database, in the carry's own transaction, so a
# boot killed between the commit and the plan write never carries twice.
CARRIED_MARKER = "restore_carried"


async def renumber(conn, old: int, new: int) -> dict:
    """Make the restored database this Host's: the backup's number `old`
    becomes the live number `new` everywhere it means this Host. Inside the
    caller's transaction, with foreign-key triggers held (carry_rows checks
    every key before it commits). Idempotent: a database already renumbered
    (no row for `old`) is left as it is, and this Host's own row is never
    deleted then."""
    if not await conn.fetchval('SELECT 1 FROM "entity_registry" WHERE "id" = $1', old):
        return {}
    await conn.execute('DELETE FROM "entity_registry" WHERE "id" = $1', new)
    await conn.execute('UPDATE "entity_registry" SET "id" = $1 WHERE "id" = $2', new, old)
    await conn.execute('UPDATE "entity_registry" SET "privatae_entity_id" = $1 '
                       'WHERE "privatae_entity_id" = $2', new, old)
    moved = {}
    cols = list(await _self_columns(conn))
    for table, col in SELF_REFS:
        if col in await _columns(conn, table):
            cols.append((table, col))
    for table, col in cols:
        await conn.execute(f"UPDATE {_qi(table)} SET {_qi(col)} = $1 WHERE {_qi(col)} = $2", new, old)
        moved[f"{table}.{col}"] = True
    for prefix in ("host:", "user:"):
        await conn.execute('UPDATE "vault_envelope" SET "owner_identity" = $1 WHERE "owner_identity" = $2',
                           f"{prefix}{new}", f"{prefix}{old}")
    # integration_connections.user_id is the Host number as TEXT (str(ENTITY_ID):
    # server/routes/integrations.py _entity_user_id, connection.create_connection_vault),
    # and every read filters on it, so a connection still under the old number
    # is one this Host can neither list nor use.
    if "user_id" in await _columns(conn, "integration_connections"):
        await conn.execute('UPDATE "integration_connections" SET "user_id" = $1 WHERE "user_id" = $2',
                           str(new), str(old))
        moved["integration_connections.user_id"] = True
    return moved


_FKS_SQL = """
    SELECT c.conname, cl.relname AS child, rf.relname AS parent, c.confdeltype::text AS on_delete,
           ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(n, o)
                 JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.o) AS child_cols,
           ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY k(n, o)
                 JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n ORDER BY k.o) AS parent_cols
    FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid
    JOIN pg_class rf ON rf.oid = c.confrelid JOIN pg_namespace n ON n.oid = cl.relnamespace
    WHERE c.contype = 'f' AND n.nspname = current_schema()
"""


# Tables a carry never copies rows INTO the restored database from, even to
# satisfy a key: they hold secrets, and the restored database keeps its own
# (the vault key and the credentials it opens are restored together).
SETTLE_DENYLIST = ("vault_envelope", "kernel_secrets", "local_backup_envelope", "local_backup_blob")


def _orphans_where(fk: dict, alias: str = "c") -> str:
    """The rows of fk's child that have their key set and no parent."""
    set_ = " AND ".join(f"{alias}.{_qi(c)} IS NOT NULL" for c in fk["child_cols"])
    match = " AND ".join(f"p.{_qi(pc)} = {alias}.{_qi(cc)}"
                         for cc, pc in zip(fk["child_cols"], fk["parent_cols"]))
    return f"{set_} AND NOT EXISTS (SELECT 1 FROM {_qi(fk['parent'])} p WHERE {match})"


async def _settle_keys(src, dst, touched: set) -> dict:
    """With the carried tables replaced, make every foreign key that involves
    them hold again, the way the schema itself says to:
      * a carried row that needs a row of a table that is NOT carried (an
        access grant naming a memory domain, entity_domain_access ->
        domain_registry) gets that row from the live database;
      * a restored row of a table that is not carried, whose parent was access
        that is gone now, follows the key's own ON DELETE rule (cascade: gone;
        set null: cleared); a key that says neither makes the carry fail
        loudly rather than keep a dangling reference.
    Then every key touching `touched` is checked; any row still without its
    parent fails the carry (and so the restore, which is undone)."""
    fks = [dict(r) for r in await dst.fetch(_FKS_SQL)]
    out: dict[str, Any] = {}
    for fk in fks:
        if fk["child"] in CARRIED_TABLES and fk["parent"] not in CARRIED_TABLES:
            cols = ", ".join(f"c.{_qi(x)}" for x in fk["child_cols"])
            missing = await dst.fetch(f"SELECT DISTINCT {cols} FROM {_qi(fk['child'])} c WHERE {_orphans_where(fk)}")
            if not missing:
                continue
            if fk["parent"] in SETTLE_DENYLIST:
                raise RestoreError(f"{fk['child']} names {fk['parent']} rows the restored database does "
                                   f"not have, and secrets are never copied across ({fk['conname']})")
            pcols = [c for c in await _columns(dst, fk["parent"]) if c in await _columns(src, fk["parent"])]
            match = " AND ".join(f"{_qi(pc)} = ${i + 1}" for i, pc in enumerate(fk["parent_cols"]))
            for m in missing:
                row = await src.fetchrow(
                    f"SELECT {', '.join(map(_qi, pcols))} FROM {_qi(fk['parent'])} WHERE {match}",
                    *[m[c] for c in fk["child_cols"]])
                if row is None:
                    raise RestoreError(f"{fk['child']} names a {fk['parent']} row the live Host does not have")
                marks = ", ".join(f"${i + 1}" for i in range(len(pcols)))
                await dst.execute(f"INSERT INTO {_qi(fk['parent'])} ({', '.join(map(_qi, pcols))}) "
                                  f"VALUES ({marks}) ON CONFLICT DO NOTHING", *[row[c] for c in pcols])
            out[f"{fk['parent']} (for {fk['child']})"] = len(missing)
    for fk in fks:
        if fk["parent"] in CARRIED_TABLES and fk["child"] not in CARRIED_TABLES:
            where = _orphans_where(fk, _qi(fk["child"]))
            n = await dst.fetchval(f"SELECT count(*) FROM {_qi(fk['child'])} WHERE {where}")
            if not n:
                continue
            if fk["on_delete"] == "c":
                await dst.execute(f"DELETE FROM {_qi(fk['child'])} WHERE {where}")
            elif fk["on_delete"] == "n":
                sets = ", ".join(f"{_qi(c)} = NULL" for c in fk["child_cols"])
                await dst.execute(f"UPDATE {_qi(fk['child'])} SET {sets} WHERE {where}")
            else:
                raise RestoreError(f"{fk['child']} rows name {fk['parent']} rows that are no longer "
                                   f"there, and {fk['conname']} says neither cascade nor set null")
            out[f"{fk['child']} (after {fk['parent']})"] = n
    for fk in fks:
        if fk["child"] in touched or fk["parent"] in touched:
            n = await dst.fetchval(f"SELECT count(*) FROM {_qi(fk['child'])} c WHERE {_orphans_where(fk)}")
            if n:
                raise RestoreError(f"{n} {fk['child']} row(s) would be left without their "
                                   f"{fk['parent']} ({fk['conname']})")
    return out


async def carry_rows(src, dst, renumber_from: Optional[int] = None,
                     renumber_to: Optional[int] = None, stamp: Optional[str] = None,
                     verified: bool = True) -> dict:
    """Copy the authorization state from `src` (the database live until the
    swap) into `dst` (the restored one), in one transaction on `dst`. When the
    backup is another Host number's, `dst` is renumbered first (renumber).
    `verified`: the backup was proved to be this Host's own (_keep_auth_key).
    Tables are replaced with their foreign-key triggers held, then every key
    they touch is settled and checked before the commit (_settle_keys), so
    the order they are written in does not matter. Returns what was carried,
    by table."""
    done: dict[str, Any] = {}
    secrets = CARRIED_SECRETS
    touched: set = set(CARRIED_TABLES)
    async with dst.transaction():
        await dst.execute("SET LOCAL session_replication_role = replica")
        # Before renumber: the rows it brings in are the backup's, keyed to its
        # Host number like the rest of the restored data.
        done.update(await _import_backup_integrations(dst))
        if renumber_from is not None and renumber_to is not None and renumber_from != renumber_to:
            done["renumbered"] = await renumber(dst, int(renumber_from), int(renumber_to))
            secrets = CARRIED_SECRETS + RENUMBER_CARRIED_SECRETS
            touched.add("entity_registry")
        elif not verified:
            # The trust store is the live one (application_signing_keys, in
            # CARRIED_TABLES), so the signing identity is too: a backup this
            # Host cannot confirm it made could carry any key, and every
            # project built here after it (the restore's own reinstall among
            # them) would be signed with a key the trust store does not hold.
            secrets = CARRIED_SECRETS + RENUMBER_CARRIED_SECRETS
        # Read before the owner's account is replaced with the live one.
        owner = owner_note(await _owner_email(src), await _owner_email(dst))
        if owner:
            done["owner_note"] = owner
        for table in CARRIED_TABLES:
            s_cols = await _columns(src, table)
            d_cols = await _columns(dst, table)
            if not d_cols:
                continue
            cols = [c for c in d_cols if c in s_cols]
            await dst.execute(f"DELETE FROM {_qi(table)}")
            rows = (await src.fetch(f"SELECT {', '.join(map(_qi, cols))} FROM {_qi(table)}")
                    if s_cols else [])
            if rows:
                marks = ", ".join(f"${i + 1}" for i in range(len(cols)))
                await dst.executemany(
                    f"INSERT INTO {_qi(table)} ({', '.join(map(_qi, cols))}) VALUES ({marks})",
                    [tuple(r[c] for c in cols) for r in rows])
            # A serial column's sequence follows the rows it now holds.
            for c in cols:
                seq = await dst.fetchval("SELECT pg_get_serial_sequence($1, $2)", table, c)
                if seq:
                    await dst.execute(
                        f"SELECT setval($1, GREATEST(COALESCE((SELECT max({_qi(c)}) FROM {_qi(table)}), 0), 1), "
                        f"(SELECT count(*) > 0 FROM {_qi(table)}))", seq)
            done[table] = len(rows)
        for table, key, ids in (("kernel_secrets", "id", secrets),
                                ("host_backup_state", "k", CARRIED_STATE)):
            if not await _columns(dst, table):
                continue
            if table == "kernel_secrets":
                ids = tuple(ids) + await _prefixed_secret_ids(src, dst)
            cols = [c for c in await _columns(dst, table) if c in await _columns(src, table)]
            await dst.execute(f"DELETE FROM {_qi(table)} WHERE {_qi(key)} = ANY($1::text[])", list(ids))
            rows = await src.fetch(
                f"SELECT {', '.join(map(_qi, cols))} FROM {_qi(table)} WHERE {_qi(key)} = ANY($1::text[])",
                list(ids))
            if rows:
                marks = ", ".join(f"${i + 1}" for i in range(len(cols)))
                await dst.executemany(
                    f"INSERT INTO {_qi(table)} ({', '.join(map(_qi, cols))}) VALUES ({marks})",
                    [tuple(r[c] for c in cols) for r in rows])
            done[table] = sorted(r[key] for r in rows)
        if await _columns(dst, "kernel_secrets") and await _columns(src, "kernel_secrets"):
            done["backup_auth"] = await _keep_auth_key(src, dst, verified=verified)
        # The Browser app keeps a signed-in profile only while the owner has
        # that switched on (server/applications/browser_profile.py:55, and
        # set_enabled drops the sealed profile when it is turned off). Off now:
        # a profile sealed in the backup does not come back.
        if (await _columns(dst, "vault_envelope")
                and "browser_profile_enabled" not in (done.get("kernel_secrets") or [])):
            await dst.execute("UPDATE vault_envelope SET superseded_at = now() "
                              "WHERE logical_name = 'browser:profile' AND superseded_at IS NULL")
            done["browser_profile"] = "off: the sealed profile is not brought back"
        done.update(await _carry_integration_grants(src, dst))
        done["keys"] = await _settle_keys(src, dst, touched)
        await dst.execute("SET LOCAL session_replication_role = origin")
        if stamp is not None and await _columns(dst, "host_backup_state"):
            # With what the restore says about the owner's sign-in, which
            # finish() reads into the restore's result: written in this
            # transaction, so a boot killed after the commit still says it.
            await dst.execute(
                "INSERT INTO host_backup_state (k, v) VALUES ($1, $2::jsonb) "
                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", CARRIED_MARKER,
                json.dumps({"stamp": stamp, **({"owner": owner} if owner else {})}))
    return done


async def _prefixed_secret_ids(src, dst) -> tuple:
    """Every kernel_secrets id, on either side, that CARRIED_SECRET_PREFIXES
    carries: the live ones are copied, the restored ones replaced."""
    out: set = set()
    for db in (src, dst):
        if not await _columns(db, "kernel_secrets"):
            continue
        out |= {r["id"] for r in await db.fetch('SELECT "id" FROM "kernel_secrets"')
                if any(str(r["id"]).startswith(pre) for pre in CARRIED_SECRET_PREFIXES)}
    return tuple(sorted(out))


def _is_auth_id(i) -> bool:
    return i == hb.AUTH_KEY_ID or str(i).startswith(hb.AUTH_KEY_ID + ":")


async def _auth_rows(conn) -> list:
    """Every backup-auth key row: backup_auth and the backup_auth:<kid> kept beside it."""
    ids = [r["id"] for r in await conn.fetch('SELECT "id" FROM "kernel_secrets"') if _is_auth_id(r["id"])]
    if not ids:
        return []
    return await conn.fetch('SELECT "id", "secret" FROM "kernel_secrets" WHERE "id" = ANY($1::text[])', ids)


async def _keep_auth_key(src, dst, verified: bool = True) -> Optional[str]:
    """The backup-auth key (host_backup 'proof that a backup is this Host's
    own'). After a VERIFIED restore the restored database keeps its own, which
    the restored data's backups were tagged with, and the key this Host held
    until the swap is kept beside it (backup_auth:<kid>), so the backup taken
    just before the restore still verifies.

    After an UNVERIFIED restore (the owner restored a backup this Host could
    not confirm it made, as a replacement Host always does) every key the
    restored database brought is dropped and this Host's keys take their
    place, the live one still the one new backups are tagged with. Those keys
    came from a file anyone who can write where backups are kept could have
    made; kept, whoever made it could tag later backups that this Host would
    take for its own. Returns the kid of the key new backups are tagged with."""
    if not verified:
        theirs = await _auth_rows(dst)
        if theirs:
            await dst.execute('DELETE FROM "kernel_secrets" WHERE "id" = ANY($1::text[])',
                              [r["id"] for r in theirs])
        ours = await _auth_rows(src)
        if ours:
            await dst.executemany('INSERT INTO "kernel_secrets" ("id", "secret") VALUES ($1, $2)',
                                  [(r["id"], bytes(r["secret"])) for r in ours])
        primary = next((r for r in ours if r["id"] == hb.AUTH_KEY_ID), None)
        return hb.auth_kid(bytes(primary["secret"])) if primary else None
    live = await src.fetch('SELECT "id", "secret" FROM "kernel_secrets" WHERE "id" = ANY($1::text[])',
                           [hb.AUTH_KEY_ID])
    if not live:
        return None
    key = bytes(live[0]["secret"])
    kid = hb.auth_kid(key)
    have = await dst.fetch('SELECT "id", "secret" FROM "kernel_secrets" WHERE "id" = ANY($1::text[])',
                           [hb.AUTH_KEY_ID, f"{hb.AUTH_KEY_ID}:{kid}"])
    if any(hb.auth_kid(bytes(r["secret"])) == kid for r in have):
        return kid
    rid = f"{hb.AUTH_KEY_ID}:{kid}" if any(r["id"] == hb.AUTH_KEY_ID for r in have) else hb.AUTH_KEY_ID
    await dst.executemany('INSERT INTO "kernel_secrets" ("id", "secret") VALUES ($1, $2)', [(rid, key)])
    return kid


async def carried_already(dst, stamp: Optional[str]) -> bool:
    """Did an earlier boot carry for this restore (committed, then killed
    before the plan said so)?"""
    if stamp is None or not await _columns(dst, "host_backup_state"):
        return False
    v = await dst.fetchval("SELECT v FROM host_backup_state WHERE k = $1", CARRIED_MARKER)
    v = json.loads(v) if isinstance(v, str) else v
    return bool(v) and v.get("stamp") == stamp


#: Where the integrations store's SQLite file sat on the apps volume until
#: v201 (/app/integrations/integrations.db in the kernel). A backup taken before
#: then still carries one.
INTEGRATIONS_LEGACY_REL = Path("app-state/integrations/integrations.db")


async def _import_backup_integrations(dst, live_root: Optional[Path] = None) -> dict:
    """A backup from before v201 kept the integrations store in a SQLite file,
    which the swap brought back on the apps volume. Its connections, syncs,
    records and OAuth clients are read into the restored database here (user
    data, as of the backup); its grants are not, being authorization the live
    Host may have withdrawn since (_carry_integration_grants keeps the live
    ones). carry_rows runs this BEFORE renumber, so a backup from another Host
    number has these connections renumbered with the rest of its data. carry()
    removes the file once the carry has committed, so the boot's
    retire_sqlite_store cannot bring its grants back."""
    from server.integrations.core import storage as integ
    restored_file = Path(live_root or LIVE) / INTEGRATIONS_LEGACY_REL
    if not restored_file.is_file() or not await _columns(dst, "integration_connections"):
        return {}
    return {"integrations (the backup's SQLite store)": await integ.copy_legacy_rows(
        dst, integ.read_legacy(restored_file), skip=("integration_grants",))}


async def _carry_integration_grants(src, dst, previous: Optional[Path] = None) -> dict:
    """Which app may use which connection (integration_grants), as it was
    live, into the restored database; a grant for a connection the restored
    database does not have is dropped. The restored connections themselves
    stay, as the backup had them. Runs inside carry_rows' transaction, with
    foreign keys held, so it writes only grants whose connection is there.
    If the database live until the swap is itself from before v201, its
    grants are in the SQLite file the swap moved aside, and are read from
    there."""
    from server.integrations.core import storage as integ
    if not await _columns(dst, "integration_grants"):
        return {}
    previous = Path(previous or PREVIOUS)
    cols = ("connection_id", "app_id", "methods", "granted_at")
    if await _columns(src, "integration_grants"):
        live = [dict(r) for r in await src.fetch(
            'SELECT "connection_id", "app_id", "methods", "granted_at" FROM "integration_grants"')]
    else:
        was = previous / INTEGRATIONS_LEGACY_REL
        live = integ.read_legacy(was)["integration_grants"] if was.is_file() else []
    have = {r["id"] for r in await dst.fetch('SELECT "id" FROM "integration_connections"')}
    kept = [g for g in live if g.get("connection_id") in have]
    await dst.execute('DELETE FROM "integration_grants"')
    if kept:
        await dst.executemany(
            'INSERT INTO "integration_grants" ("connection_id", "app_id", "methods", "granted_at") '
            "VALUES ($1, $2, $3, $4)",
            [tuple(g.get(c) for c in cols) for g in kept])
    return {"integration_grants": len(kept)}


async def _connect(db: str):
    import asyncpg
    return await asyncpg.connect(host=hb.PG_SOCKET, user=hb.PG_USER, database=db)


def carry(live: str) -> int:
    """entrypoint.sh, after the schema step, before the server starts:
    `python3 -m server.host_restore carry "$ENTITY_DB"`. 0 = carried or
    nothing to do; 1 = failed (the boot stops, and the restore is undone)."""
    plan = read_plan()
    if not plan or plan.get("state") != SWAPPED or plan.get("carried"):
        return 0
    live = plan.get("live_db") or live
    before = f"{live}_before_restore"
    if not plan.get("settled"):
        # The migrations newer than the backup have just run over its rows:
        # what they left of the load's own columns and checks is settled
        # before anything serves.
        try:
            said = settle(live, plan.get("load_columns") or [], plan.get("deferred_checks") or [])
        except Exception as e:
            print(f"restore: could not settle the restored database: {e}", file=sys.stderr)
            return 1
        if said:
            plan["db_note"] = " ".join(n for n in (plan.get("db_note"), said) if n)
            print(f"restore: {said}")
        plan["settled"] = True
        _write_plan(plan)

    async def go():
        src, dst = await _connect(before), await _connect(live)
        try:
            if await carried_already(dst, plan.get("stamp")):
                return {"already": True}
            return await carry_rows(src, dst, plan.get("backup_entity_id"), plan.get("live_entity_id"),
                                    stamp=plan.get("stamp"), verified=plan.get("verified") is True)
        finally:
            await src.close()
            await dst.close()
    try:
        done = asyncio.run(go())
        # The backup's integrations SQLite file (from before v201), read into
        # the restored database above: removed now that the carry committed,
        # so the boot's retire_sqlite_store cannot bring its grants back.
        from server.integrations.core import storage as _integ
        _integ.remove_legacy_file(LIVE / INTEGRATIONS_LEGACY_REL)
    except Exception as e:
        print(f"restore: could not keep the live access and second factor: {e}", file=sys.stderr)
        return 1
    plan.update(carried=True, carried_at=hb._now().isoformat())
    _write_plan(plan)
    print(f"restore: kept the live access and second factor ({len(done)} tables)")
    if done.get("owner_note"):
        print(f"restore: {done['owner_note']}")
    return 0


# ── step 1: stage, while the Host runs ───────────────────────────────────────

def _progress(pool, **v):
    return hb._set_state(pool, "restore", {**v, "at": hb._now().isoformat()})


async def stage(pool, eid: int, stamp: str, unverified_ok: bool = False,
                where: Optional[str] = None) -> dict:
    """Stage a restore and restart the kernel to swap it in. Runs as a
    background task from the route; progress is in host_backup_state
    'restore'. Serialized with backups (host_backup._run_lock).
    `unverified_ok`: the owner said to restore a backup this Host cannot
    verify as its own (UNVERIFIED_TEXT). `where`: "local" for a file on this
    machine the owner chose (_entry)."""
    async with hb._run_lock:
        try:
            # First, whatever else happens: a restore killed in the middle of
            # its load left LOAD_ROLE able to log in.
            await asyncio.to_thread(lock_load_role)
        except RestoreError as e:
            await _progress(pool, state=FAILED, error=str(e), stamp=stamp)
            return {"state": FAILED, "error": str(e)}
        if read_plan() is not None:
            # Checked before anything is cleared: the staged copy belongs to it.
            return {"state": FAILED,
                    "error": "a restore is already waiting for this Host to restart"}
        try:
            return await _stage(pool, eid, stamp, unverified_ok, where)
        except Exception as e:
            msg = str(e)[:600] or type(e).__name__
            log.warning("restore: staging failed: %s", msg)
            drop_resealed(stamp)
            await _clear_staging(pool)
            _drop_plan()
            await _progress(pool, state=FAILED, error=msg, stamp=stamp)
            return {"state": FAILED, "error": msg}


async def _open_source(pool, eid: int, entry: dict):
    """A readable stream of the encrypted file."""
    if entry["where"] == "local":
        try:
            return await asyncio.to_thread(hb.local_store().open, entry["key"])
        except hb.BackupError as e:
            raise RestoreError(str(e)) from None
    if entry["where"] == "platform":
        url = (await hb.call("backup.get_url", {"key": entry["key"]}))["url"]
        return await asyncio.to_thread(urllib.request.urlopen, url, None, hb.RUN_TIMEOUT_S)
    target = await _s3_target(pool, eid)
    if not target:
        raise RestoreError("your own storage is no longer set as a destination")
    from server import backup_ship
    creds = await backup_ship._s3_credential(pool, eid, target["cred_name"])
    return await asyncio.to_thread(
        lambda: _s3_client(creds, target).get_object(Bucket=target["address"],
                                                     Key=entry["key"])["Body"])


# The columns of the before-restore run carried into the restored database.
_UNDO_COLS = ("started_at", "finished_at", "reason", "state", "stamp", "stored", "object_key",
              "sidecar_key", "size_bytes", "sha256", "sidecar_sha256", "sidecar_bytes",
              "recipients", "parts", "error")


async def _run_record(pool, run_id) -> Optional[dict]:
    """The before-restore run's row, as JSON the plan can hold."""
    if run_id is None:
        return None
    row = await pool.fetchrow(
        f"SELECT {', '.join(_UNDO_COLS)} FROM host_backup_runs WHERE id = $1", int(run_id))
    if not row:
        return None
    out = {}
    for k in _UNDO_COLS:
        v = row[k]
        if isinstance(v, dt.datetime):
            v = v.isoformat()
        elif isinstance(v, str) and k in ("recipients", "parts"):
            v = json.loads(v)
        out[k] = v
    return out


async def _keep_undo_point(pool, plan: dict) -> bool:
    """Record the backup taken just before this restore in the restored
    database. It was recorded in the database the restore replaced, which is
    dropped at finish; without this the restored Host has no record of it, so
    Settings would not list it and, with platform storage off, the second copy
    would never send it and the staged copy would lapse. Idempotent (by stamp)."""
    u = plan.get("undo_point")
    if not u or not u.get("stamp") or u.get("reason") != "before-restore":
        return False
    if await pool.fetchval("SELECT 1 FROM host_backup_runs WHERE stamp = $1 AND reason = 'before-restore'",
                           u["stamp"]):
        return False
    vals = [u.get(k) for k in _UNDO_COLS]
    for i, k in enumerate(_UNDO_COLS):
        if k in ("started_at", "finished_at") and vals[i]:
            vals[i] = dt.datetime.fromisoformat(vals[i])
        elif k in ("recipients", "parts"):
            vals[i] = json.dumps(vals[i] if vals[i] is not None else ([] if k == "recipients" else {}))
    marks = ", ".join(f"${i + 1}" + ("::jsonb" if k in ("recipients", "parts") else "")
                      for i, k in enumerate(_UNDO_COLS))
    await pool.execute(f"INSERT INTO host_backup_runs ({', '.join(_UNDO_COLS)}) VALUES ({marks})", *vals)
    return True


async def new_plan(pool, eid: int, stamp: str, entry: dict, got: dict) -> dict:
    """The plan a staged restore is swapped in by."""
    return {"state": STAGED_STATE, "stamp": stamp, "where": entry["where"],
            "taken_at": entry["taken_at"], "staged_at": hb._now().isoformat(), "boots": 0,
            "files": got.get("files"), "db_bytes": got.get("db_bytes"),
            # The Host the backup was taken on, and this one. They differ when
            # a Host that replaced a lost one (with a new number) restores it.
            "backup_entity_id": got.get("entity_id"), "live_entity_id": int(eid),
            # Each app's kept data from the backup, put back at finish.
            "app_data": got.get("app_data") or []}


async def _stage(pool, eid: int, stamp: str, unverified_ok: bool = False,
                 where: Optional[str] = None) -> dict:
    missing = hb.configured()
    if missing:
        raise RestoreError(missing)
    held = load_resealed(stamp)
    if not held:
        raise RestoreError("this backup's key has not been sealed to this Host yet (or it was "
                           "more than an hour ago): get the key again")
    body = await asyncio.to_thread(hb.open_sidecar, held, await _host_identity(pool), stamp=stamp)
    is_own = await provenance(pool, body)
    if not is_own and not unverified_ok:
        raise RestoreError(UNVERIFIED_TEXT)
    # The encrypted file must be the one the sidecar names (always checked
    # when the sidecar says; the tag makes that binding this Host's own).
    expect = ({"sha256": body["data_sha256"], "bytes": body.get("data_bytes")}
              if body.get("data_sha256") else None)
    drop_resealed(stamp)            # read once; the key lives on in memory until unpacked
    await _progress(pool, state="staging", step="finding the backup", stamp=stamp)
    entry = await _entry(pool, eid, stamp, where)
    listed = entry.get("sha256")
    if listed and expect and listed != expect["sha256"]:
        raise RestoreError("the platform's record of this backup names another file than its key "
                           "file does: it was changed or replaced after it was written")
    if listed and not expect:
        expect = {"sha256": listed, "bytes": entry.get("bytes") or None}
    room = _room(LIVE)
    keep, _, _ = _reserve(room)
    need = entry["bytes"] + keep
    if room.free < need:
        raise RestoreError(f"this Host needs at least {need >> 20} MiB free to set the restored "
                           f"files out before swapping them in, and has {room.free >> 20} MiB "
                           f"({keep >> 20} MiB of that stays free for the machine it runs on)")

    own = await pool.fetchval(
        "SELECT 1 FROM host_backup_runs WHERE state IN ('ok', 'partial') "
        "AND reason <> 'restore' LIMIT 1")
    undo_point = None
    if own:
        # How the Host is now goes into the list first, so this is undoable. A
        # Host with no backups of its own is a new one standing in for a lost
        # Host: there is nothing on it to keep. With platform storage off that
        # backup is staged, and staging never drops the copy being restored
        # (keep), which a bastion may just have put back there.
        await _progress(pool, state="staging", step="backing up how the Host is now", stamp=stamp)
        before = await hb._run(pool, eid, "before-restore", keep=(stamp,))
        if before["state"] not in (hb.STATE_OK, hb.STATE_PARTIAL):
            raise RestoreError("could not back up how the Host is now, so a restore could not "
                               f"be undone: {before.get('error') or before['state']}")
        undo_point = await _run_record(pool, before.get("run_id"))

    await _progress(pool, state="staging", step="restoring the database and files", stamp=stamp)
    await _clear_staging(pool)
    await pool.execute(f'CREATE DATABASE "{RESTORE_DB}"')
    src = await _open_source(pool, eid, entry)
    try:
        got = await asyncio.to_thread(unpack, src, body["identity"], STAGED,
                                      expect=expect, trust_links=is_own)
    finally:
        with contextlib.suppress(Exception):
            src.close()
    body = None  # noqa: F841 - the backup's key is not kept past this point
    code_left_out, apps_wanted = [], []
    if not is_own:
        # No installed code and no app row from a backup this Host cannot
        # confirm it made (leave_code_out, below).
        code_left_out = await asyncio.to_thread(leave_code_out, STAGED)
        conn = await _connect(RESTORE_DB)
        try:
            apps_wanted = await leave_app_rows_out(conn)
        finally:
            await conn.close()
    await asyncio.to_thread(_write_manifest)

    plan = await new_plan(pool, eid, stamp, entry, got)
    plan["verified"] = is_own
    plan["code_left_out"] = code_left_out
    # This Host's own, through the swap (set_aside), after an unverified one.
    plan["live_kept"] = [] if is_own else list(LIVE_KEPT_DIRS)
    plan["apps_reinstall"] = apps_wanted
    plan["links_refused"] = got.get("links_refused") or []
    plan["links_refused_count"] = int(got.get("links_refused_count") or len(plan["links_refused"]))
    # What the load left out (said to the owner at finish), and what the
    # boot settles once its migrations have run over the rows (carry, settle).
    plan["db_note"] = got.get("world_note")
    plan["load_columns"] = got.get("load_columns") or []
    plan["deferred_checks"] = got.get("deferred_checks") or []
    if undo_point:
        plan["undo_point"] = undo_point
    _write_plan(plan)
    await _progress(pool, state=STAGED_STATE, taken_at=entry["taken_at"], stamp=stamp)
    log.info("restore: staged the backup of %s; restarting to swap it in", entry["taken_at"])
    _restart()
    return {"state": STAGED_STATE, "taken_at": entry["taken_at"]}


# ── unpacking: a backup file is hostile input until it is checked ────────────
#
# A backup is encrypted to public keys, so anyone who can write where backups
# are kept can make a file that decrypts. Until it is checked, nothing in it
# is allowed to act outside the staged folder or reach the database:
#   * names: relative, no "..", no empty part, each named once, nothing below
#     a file or a link, nothing in the restore's own folder; only files,
#     folders and links (no hard links, devices or fifos: the writer never
#     makes them, host_backup._add_member);
#   * no link is made while the file is read, every file is opened with
#     O_EXCL|O_NOFOLLOW and its mode set through its own descriptor, so
#     nothing written can follow a link;
#   * the database pieces are spooled, not loaded; what is written is
#     bounded by the room measured first, each entry charged the units, the
#     directory entry and the inode it takes (_room, _units, _name_cost);
#   * at the end, the whole encrypted file must match what its sidecar says
#     (sha256 and size, when it says: `expect`), and the database, every file
#     and every link must match manifest.json exactly, both ways;
#   * only then are the links made (a link that resolves outside the restored
#     tree from where it will sit once live is left out, unless `trust_links`:
#     the backup was proved to be this Host's own) and the database loaded.

MANIFEST_MAX = hb.MANIFEST_MAX      # the writer refuses to make a larger one
SPOOL_NAME = "hostmem.dump"
# Everything below is read before the file's hash can be checked, so each is
# bounded here, while the restore holds the backup lock (stage):
#  * a tar header that carries a name or attributes (GNU long name, PAX
#    record) is read whole into memory by tarfile. The writer's hold a path
#    or a link target (PATH_MAX is 4096 bytes), so one past TAR_HEADER_MAX is
#    refused before it is read; sparse files, which the writer never makes,
#    are refused before their maps are read;
#  * the decompressed stream may not grow past INFLATE_FLOOR plus
#    INFLATE_RATIO times the encrypted bytes read so far. The floor lets a
#    small backup hold a large run of zeros; a file built to inflate without
#    end is stopped within it (about 16 s of zstd at 1 GB/s);
#  * after the tar's end the writer leaves at most one 10 KiB record, so more
#    than TRAILER_MAX there is refused;
#  * the whole unpack stops at UNPACK_LIMIT_S, the time a backup run may take
#    to write (host_backup.RUN_LIMIT_S): age is killed, which ends every read.
TAR_HEADER_MAX = 64 * 1024
INFLATE_FLOOR = 16 * 1024 ** 3
INFLATE_RATIO = 1000
TRAILER_MAX = 1024 * 1024
UNPACK_LIMIT_S = hb.RUN_LIMIT_S


class _BoundedTarInfo(tarfile.TarInfo):
    """tarfile's member header, with the reads above bounded."""

    def _proc_member(self, tf):
        if self.type in (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE,
                         tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE) and self.size > TAR_HEADER_MAX:
            raise RestoreError(f"the backup has a tar header of {self.size} bytes; a backup's are "
                               f"at most {TAR_HEADER_MAX}")
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise RestoreError("the backup holds a sparse file, which backups never hold")
        return super()._proc_member(tf)

    def _proc_gnusparse_00(self, *a):
        raise RestoreError("the backup holds a sparse file, which backups never hold")

    _proc_gnusparse_01 = _proc_gnusparse_10 = _proc_gnusparse_00


def _too_long() -> str:
    return (f"reading the backup took longer than {UNPACK_LIMIT_S // 60} minutes, the longest a "
            "backup may take to write, and it was stopped")


class _Inflated:
    """The decompressed stream, counted against INFLATE_FLOOR + INFLATE_RATIO x
    the encrypted bytes read so far (`fed`), and stopped once `expired`."""

    def __init__(self, raw, fed: dict, expired: threading.Event):
        self.raw, self.fed, self.expired, self.n = raw, fed, expired, 0

    def read(self, size: int = -1) -> bytes:
        if self.expired.is_set():
            raise RestoreError(_too_long())
        b = self.raw.read(size)
        self.n += len(b)
        if self.n > INFLATE_FLOOR + INFLATE_RATIO * self.fed["n"]:
            raise RestoreError(f"the backup unpacks to more than {INFLATE_RATIO} times its size; "
                               "a backup does not")
        return b


def _safe_rel(name: str, prefix: str) -> list[str]:
    rel = name[len(prefix):]
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") or "\0" in p for p in parts):
        raise RestoreError(f"the backup holds an unsafe path: {name!r}")
    return parts


def _inside(root_real: str, path: str) -> bool:
    """A path boundary, not a string prefix: <root>-x is not inside <root>."""
    try:
        return os.path.commonpath([root_real, path]) == root_real
    except ValueError:
        return False


def _make_dirs(staged: Path, parts: list[str]) -> None:
    """Every folder in parts, below staged, made as a real folder. Anything
    else in the way (a file, a link) is refused, never followed."""
    cur = staged
    for p in parts:
        cur = cur / p
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, 0o755)
            continue
        if not _stat.S_ISDIR(st.st_mode):
            raise RestoreError(f"the backup has something below a file or a link ({'/'.join(parts)})")


LINK_HOPS = 40      # links followed in one resolution, as Linux's MAXSYMLINKS


def _link_escapes(staged_real: str, live_root: str, parts: list[str], target: str) -> bool:
    """Does a restored link lead outside the restored tree once it is live?

    A link staged at STAGED/<parts> is moved to LIVE/<parts> (move_in), so it
    is resolved from there, not from the staged folder two levels further
    down: a relative "../apps/x" that climbs out of the staged tree and back
    in lands somewhere else once it is live. Resolved one name at a time, with
    the restored links in place (read from the staged tree, where they are the
    same links), so a chain is followed as it will be on the Host. It leads
    out when any step climbs above the apps root, when an absolute target (its
    own or a link's on the way) names anything but the apps root, when it
    reaches a path the restore leaves alone (_kept: the restore's own folder,
    and what backups leave out, such as node_modules, .venv or crash), or after
    LINK_HOPS links.

    The kept paths are why the staged tree is not enough: they stay live
    through the swap (set_aside skips them, and move_in makes only the folders
    the backup holds), so what is there on the Host, a link to /usr/lib or
    anywhere, is not in the staged copy for this walk to see. A link placed
    there by the backup itself is no different."""
    root = live_root.rstrip("/")
    cur = list(parts[:-1])
    pending: list[str] = []          # names still to walk, next one last
    hops = 0
    def kept(rel: str) -> bool:
        # Only an unverified restore checks its links here (unpack
        # trust_links), and it takes these from the image or this Host.
        return _kept(rel) or _left_out_unverified(rel)

    if any(kept("/".join(cur[:i])) for i in range(1, len(cur) + 1)):
        return True                  # the link itself sits in a kept path

    def follow(t: str) -> bool:
        nonlocal cur
        if os.path.isabs(t):
            if t != root and not t.startswith(root + "/"):
                return False
            cur, t = [], t[len(root):]
        pending.extend(reversed(t.split("/")))
        return True

    if not follow(target):
        return True
    while pending:
        name = pending.pop()
        if name in ("", "."):
            continue
        if name == "..":
            if not cur:
                return True
            cur.pop()
            continue
        cur.append(name)
        if kept("/".join(cur)):
            return True
        here = os.path.join(staged_real, *cur)
        if os.path.islink(here):
            hops += 1
            cur.pop()
            if hops > LINK_HOPS or not follow(os.readlink(here)):
                return True
    return False


APP_DATA_PREFIX = "app-data/"
# One piece of an app's data, as host_backup._add_app_data names it: the app
# id as the runtime checks one (cseries_runtime/validate.py APP_ID_RE), then
# the piece's number.
_APP_DATA_MEMBER = re.compile(r"^app-data/([a-z0-9][a-z0-9-]{0,63})\.tar\.([0-9]{5})\Z")
_APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")


def _check_app_data(manifest: dict, got: dict, app_dir: Path, staged: bool) -> list[dict]:
    """Each app's data in the backup against its manifest entry, both ways:
    every piece is listed, every app the manifest says was read whole is
    there, piece for piece, byte for byte. An app the manifest says was cut
    short when the backup was made is not put back (its staged tar goes) and
    is listed so with why. Returns the apps, each {app_id, installed,
    uid_base, bytes, state[, error]}: state pending when it is staged to go
    back, not restored otherwise."""
    part = (manifest.get("parts") or {}).get("app_data") or {}
    listed: dict[str, dict] = {}
    for a in (part.get("apps") or []) if isinstance(part, dict) else []:
        if not isinstance(a, dict) or not isinstance(a.get("app_id"), str) or not _APP_ID.match(a["app_id"]):
            raise RestoreError("the backup's manifest lists an app's data without an app id")
        listed[a["app_id"]] = a
    out: list[dict] = []
    for aid in sorted(set(got) | set(listed)):
        w, g = listed.get(aid), got.get(aid)
        if w is None:
            raise RestoreError(f"the backup holds data of {aid} that its manifest does not list")
        base = w.get("uid_base")
        entry: dict[str, Any] = {"app_id": aid, "installed": bool(w.get("installed")),
                                 "uid_base": base if isinstance(base, int) and not isinstance(base, bool)
                                 else None}
        if not w.get("ok"):
            with contextlib.suppress(FileNotFoundError):
                (app_dir / f"{aid}.tar").unlink()
            entry.update(bytes=0, state=APP_DATA_NOT_RESTORED,
                         error=(str(w.get("error") or "") if w.get("refused") and w.get("error")
                                else "it could not be read whole when this backup was made: "
                                + str(w.get("error") or "no reason was kept"))[:600])
            out.append(entry)
            continue
        if (g is None or g["h"].hexdigest() != w.get("sha256") or g["chunks"] != w.get("chunks")
                or g["bytes"] != w.get("bytes")):
            raise RestoreError(f"the data of {aid} in the backup does not match its manifest")
        entry["bytes"] = g["bytes"]
        if staged:
            entry["state"] = APP_DATA_PENDING
        else:
            entry.update(state=APP_DATA_NOT_RESTORED,
                         error="this machine runs no local runtime for an app's data to go back to")
        out.append(entry)
    return out


def unpack(src, identity: str, staged: Path, *, expect: Optional[dict] = None,
           trust_links: bool = False, spool: Optional[Path] = None,
           stage_app_data: Optional[bool] = None) -> dict:
    """Decrypt, check and unpack one backup file: apps/ into `staged`, each
    app's kept data into app-data/ beside it (one tar an app, put back by the
    local runtime at finish), the database into RESTORE_DB, only after
    everything checked (above). `expect`: {"sha256", "bytes"} of the
    encrypted file, from its sidecar. `stage_app_data`: write the apps' data
    out (default: when this machine runs the local runtime, the only place it
    can go back to); otherwise it is still read and checked, and listed as
    not restored. Synchronous; runs in a worker thread."""
    from compression import zstd

    staged.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(staged.iterdir()):
        raise RestoreError("the staging folder is not empty; cancel and try again")
    spool = spool or staged.parent / SPOOL_NAME
    with contextlib.suppress(FileNotFoundError):
        spool.unlink()
    if stage_app_data is None:
        from server.applications import local_runtime
        stage_app_data = local_runtime.in_use()
    app_dir = staged.parent / APP_DATA.name
    shutil.rmtree(app_dir, ignore_errors=True)
    staged_real = os.path.realpath(staged)
    room = _room(staged)
    unit = max(1, _alloc_unit(staged, room.unit))
    keep, keep_inodes, keep_src = _reserve(room)
    log.info("restore: room %d B, unit %d B, keeping %d B and %s inodes free (%s)",
             room.free, unit, keep, keep_inodes, keep_src)
    budget = {"bytes": room.free - keep,
              "inodes": room.inodes_free - keep_inodes if keep_inodes is not None else None}

    def spend(n: int, name: str) -> None:
        budget["bytes"] -= max(0, int(n))
        if budget["bytes"] < 0:
            raise RestoreError(f"the backup is larger than this Host has room for (at {name})")

    def make(name: str, data: int) -> None:
        """A new file, folder or link: its data in whole units, its name in
        its folder, and an inode. Spent before it is made."""
        if budget["inodes"] is not None:
            budget["inodes"] -= 1
            if budget["inodes"] < 0:
                raise RestoreError(f"the backup holds more files than this Host has room for (at {name})")
        spend(_units(data, unit) + _name_cost(name.rsplit("/", 1)[-1]), name)

    # The database spool: one file, its pieces spent as they arrive, and a
    # unit for the last one it only partly fills.
    make(spool.name, unit)

    argv, fd = hb.age_decrypt_args(identity)
    try:
        age = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, pass_fds=(fd,))
    finally:
        os.close(fd)
    fed: dict[str, Any] = {"e": None, "h": hashlib.sha256(), "n": 0, "done": False}

    def feed():
        try:
            while True:
                b = src.read(READ_CHUNK)
                if not b:
                    break
                fed["h"].update(b)
                fed["n"] += len(b)
                age.stdin.write(b)
            fed["done"] = True
        except Exception as e:
            fed["e"] = e
        finally:
            with contextlib.suppress(Exception):
                age.stdin.close()

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    # The time bound: age is killed, which ends every read below.
    expired = threading.Event()

    def expire():
        expired.set()
        with contextlib.suppress(Exception):
            age.kill()
    watchdog = threading.Timer(UNPACK_LIMIT_S, expire)
    watchdog.daemon = True
    watchdog.start()
    db_hash, db_bytes, next_chunk = hashlib.sha256(), 0, 0
    # app id -> {h, chunks, bytes, f (the staged tar, open while its pieces come)}
    app_data: dict[str, dict] = {}
    app_cur: list = [None]          # the app whose pieces are arriving
    files: dict[str, str] = {}
    links: list[tuple[list[str], str]] = []
    seen: dict[str, str] = {}       # rel -> d (folder named), i (folder implied), f, l
    manifest = None
    failure: Optional[BaseException] = None
    rc = None
    try:
        out = open(spool, "xb")
        os.chmod(spool, 0o600)
        with out, zstd.ZstdFile(age.stdout, mode="rb") as zr, \
                tarfile.open(fileobj=(inflated := _Inflated(zr, fed, expired)), mode="r|",
                             tarinfo=_BoundedTarInfo) as tin:
            for m in tin:
                name = m.name
                if hb.unshowable(name):
                    # A backup never holds one (host_backup._add_member leaves
                    # it out); it would add lines of its own wherever the
                    # restore names it to the owner.
                    raise RestoreError(f"the backup holds a name with a line break or another "
                                       f"control character in it ({name!r}), which backups never hold")
                if name.startswith("db/hostmem.dump."):
                    try:
                        idx = int(name.rsplit(".", 1)[1])
                    except ValueError:
                        idx = -1
                    if idx != next_chunk or not m.isfile():
                        raise RestoreError(f"the database pieces are out of order at {name}")
                    spend(m.size, name)
                    f = tin.extractfile(m)
                    while True:
                        b = f.read(READ_CHUNK)
                        if not b:
                            break
                        db_hash.update(b)
                        db_bytes += len(b)
                        out.write(b)
                    next_chunk += 1
                elif name == "manifest.json":
                    if manifest is not None or not m.isfile() or m.size > MANIFEST_MAX:
                        raise RestoreError("the backup's manifest is not one readable file")
                    try:
                        manifest = json.loads(tin.extractfile(m).read())
                    except ValueError:
                        raise RestoreError("the backup's manifest is not readable") from None
                    if not isinstance(manifest, dict):
                        raise RestoreError("the backup's manifest is not readable")
                elif name.startswith(APP_DATA_PREFIX):
                    got_app = _APP_DATA_MEMBER.match(name)
                    if not got_app or not m.isfile():
                        raise RestoreError(f"the backup holds {name}, which is not a piece of an app's data")
                    aid, idx = got_app.group(1), int(got_app.group(2))
                    one = app_data.get(aid)
                    if aid != app_cur[0]:
                        # The writer sends one app's pieces together, in order.
                        if one is not None or idx != 0:
                            raise RestoreError(f"the pieces of {aid}'s data are out of order at {name}")
                        if app_cur[0] is not None and app_data[app_cur[0]].get("f"):
                            app_data[app_cur[0]].pop("f").close()
                        one = app_data[aid] = {"h": hashlib.sha256(), "chunks": 0, "bytes": 0}
                        if stage_app_data:
                            if not app_dir.exists():
                                app_dir.mkdir(mode=0o700)
                            make(f"{APP_DATA.name}/{aid}.tar", unit)
                            fd = os.open(app_dir / f"{aid}.tar",
                                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                            one["f"] = os.fdopen(fd, "wb")
                        app_cur[0] = aid
                    if idx != one["chunks"]:
                        raise RestoreError(f"the pieces of {aid}'s data are out of order at {name}")
                    if stage_app_data:
                        spend(m.size, name)
                    f = tin.extractfile(m)
                    while True:
                        b = f.read(READ_CHUNK)
                        if not b:
                            break
                        one["h"].update(b)
                        one["bytes"] += len(b)
                        if one.get("f"):
                            one["f"].write(b)
                    one["chunks"] += 1
                elif name.startswith("apps/"):
                    parts = _safe_rel(name, "apps/")
                    if parts[0] == RESTORE_DIR.name:
                        # Never written by a backup (host_backup.APPS_EXCLUDES); moved
                        # in, it would land on this restore's own plan and staging.
                        raise RestoreError(f"the backup holds {name}, which backups never hold")
                    rel = "/".join(parts)
                    if rel in seen and not (seen[rel] == "i" and m.isdir()):
                        raise RestoreError(f"the backup holds {name} twice")
                    for i in range(1, len(parts)):
                        if seen.get("/".join(parts[:i]), "i") not in ("d", "i"):
                            raise RestoreError(f"the backup has something below a file or a link ({name})")
                    for i in range(1, len(parts)):
                        here = "/".join(parts[:i])
                        if here not in seen:
                            seen[here] = "i"
                            make("apps/" + here, unit)       # a folder the path implies
                    target = staged.joinpath(*parts)
                    if m.isdir():
                        if rel not in seen:
                            make(name, unit)
                        _make_dirs(staged, parts)
                        dfd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            os.fchmod(dfd, (m.mode & 0o777) | 0o700)
                        finally:
                            os.close(dfd)
                        seen[rel] = "d"
                    elif m.issym():
                        if not m.linkname or "\0" in m.linkname:
                            raise RestoreError(f"the backup holds a link with no target ({name})")
                        target_len = len(os.fsencode(m.linkname))
                        make(name, target_len if target_len >= LINK_INLINE else 0)
                        links.append((parts, m.linkname))
                        seen[rel] = "l"
                    elif m.isfile():
                        make(name, m.size)
                        _make_dirs(staged, parts[:-1])
                        h = hashlib.sha256()
                        ffd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(ffd, "wb") as w:
                            f = tin.extractfile(m)
                            while True:
                                b = f.read(READ_CHUNK)
                                if not b:
                                    break
                                h.update(b)
                                w.write(b)
                            w.flush()
                            os.fchmod(w.fileno(), m.mode & 0o777)
                            with contextlib.suppress(OSError):
                                os.utime(w.fileno(), (m.mtime, m.mtime))
                        files[name] = h.hexdigest()
                        seen[rel] = "f"
                    else:
                        raise RestoreError(f"the backup holds {name}, which is not a file, a folder "
                                           "or a link (a hard link, a device or a pipe)")
                # gitea-repos/ and README.txt are for the user, not for the
                # Host: never written. Anything else is ignored the same way.
            trailing = 0                 # the rest of the file, so age can finish
            while b := inflated.read(READ_CHUNK):
                trailing += len(b)
                if trailing > TRAILER_MAX:
                    raise RestoreError("the backup has more data after its end than a backup "
                                       "leaves there")
    except BaseException as e:
        failure = e
    finally:
        for one in app_data.values():
            if one.get("f"):
                with contextlib.suppress(Exception):
                    one.pop("f").close()
        if failure is None:
            with contextlib.suppress(Exception):
                while age.stdout.read(READ_CHUNK):
                    pass
        else:
            with contextlib.suppress(Exception):
                age.kill()          # refused part way: the rest is not read
        with contextlib.suppress(Exception):
            age.stdout.close()
        rc = age.wait(timeout=600)
        feeder.join(timeout=600)
        watchdog.cancel()
    try:
        if expired.is_set():
            raise RestoreError(_too_long())
        if isinstance(failure, RestoreError):
            raise failure
        if failure is not None:
            msg = _tail(age.stderr.read().decode(errors="replace")) if rc else ""
            raise RestoreError(f"the backup could not be {'decrypted' if msg else 'read'}: "
                               f"{msg or failure}") from None
        if rc != 0:
            raise RestoreError("the backup could not be decrypted: "
                               + _tail(age.stderr.read().decode(errors="replace")))
        if fed["e"] is not None or not fed["done"]:
            raise RestoreError(f"the backup could not be downloaded: {fed['e'] or 'cut short'}")
        if expect:
            if (fed["h"].hexdigest() != expect.get("sha256")
                    or (expect.get("bytes") is not None and fed["n"] != int(expect["bytes"]))):
                raise RestoreError("the backup file is not the one its key file was made for: "
                                   "it was changed or replaced after it was written")
        if manifest is None:
            raise RestoreError("the backup has no manifest: it was cut short")
        if next_chunk == 0:
            raise RestoreError("the backup holds no database")
        want_db = (manifest.get("parts") or {}).get("db") or {}
        if want_db.get("sha256") != db_hash.hexdigest() or want_db.get("chunks") != next_chunk:
            raise RestoreError("the database in the backup does not match its manifest")
        want_files, want_links = {}, {}
        for f in manifest.get("files") or []:
            p = str((f or {}).get("path") or "") if isinstance(f, dict) else ""
            if hb.unshowable(p):
                raise RestoreError("the backup's manifest names a path with a line break or another "
                                   "control character in it, which backups never hold")
            if not p.startswith("apps/"):
                continue
            if "link" in f:
                want_links[p] = f.get("link")
            else:
                want_files[p] = f.get("sha256")
        got_links = {"apps/" + "/".join(parts): t for parts, t in links}
        for p in sorted(set(files) | set(want_files)):
            if files.get(p) != want_files.get(p):
                raise RestoreError(f"{p} does not match the manifest")
        for p in sorted(set(got_links) | set(want_links)):
            if got_links.get(p) != want_links.get(p):
                raise RestoreError(f"the link {p} does not match the manifest")
        restored_apps = _check_app_data(manifest, app_data, app_dir, stage_app_data)
        # Checked. Now the links, then the database.
        for parts, t in links:
            _make_dirs(staged, parts[:-1])
            os.symlink(t, staged.joinpath(*parts))
        refused: list[str] = []
        if not trust_links:
            live_root = str(LIVE)
            left = list(links)
            changed = True
            while changed:           # a link left out can change where others lead
                changed = False
                for parts, t in list(left):
                    path = staged.joinpath(*parts)
                    if _link_escapes(staged_real, live_root, parts, t):
                        os.unlink(path)
                        left.remove((parts, t))
                        refused.append("/".join(parts))
                        changed = True
        git_dirs = defuse_git(staged)
        loaded = _load_database(spool) or {}
    finally:
        with contextlib.suppress(FileNotFoundError):
            spool.unlink()
    return {"files": len(files), "db_bytes": db_bytes, "links_refused": sorted(refused)[:50],
            "links_refused_count": len(refused), "world_note": loaded.get("note"),
            "load_columns": loaded.get("columns") or [], "deferred_checks": loaded.get("checks") or [],
            "app_data": restored_apps, "git_dirs_reset": git_dirs,
            "entity_id": manifest.get("entity_id") if isinstance(manifest.get("entity_id"), int) else None}


# A Git directory's own settings can make git run a program (a filter's clean
# or smudge command, gpg.program, core.sshCommand, a credential helper, a
# merge or diff driver, uploadpack), work on another folder (core.worktree),
# or read objects or settings from another folder (alternates, commondir,
# config.worktree, include.path). The Host runs git in its projects (commit,
# status, diff, push) with only the hooks, fsmonitor and external diff turned
# off (routes/project_git.py HARDENED_GIT_CONFIG), and a backup's projects are
# anyone's. So every folder of the staged tree that git would take for a Git
# directory (HEAD, and objects and refs or a commondir: git's own test) gets a
# config of only the format it is written in, and loses what points elsewhere
# and its hooks. The Host sets origin itself before it pushes
# (project_git._sync_blocking), and each fetch and push names its URL. A
# pointer reached through a link to a folder is left: removing it would write
# outside the staged tree before the swap.
_GIT_POINTERS = ("config.worktree", "commondir", "objects/info/alternates",
                 "objects/info/http-alternates")


def _git_object_format(config: Path) -> str:
    """The object format a Git directory's config says (sha1 or sha256), read
    without following anything it includes."""
    if not config.is_file() or config.is_symlink():
        return "sha1"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent"}
    try:
        r = subprocess.run(["git", "config", "--file", str(config), "--no-includes",
                            "--get", "extensions.objectformat"],
                           capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return "sha1"
    return "sha256" if r.returncode == 0 and r.stdout.strip().lower() == "sha256" else "sha1"


def defuse_git(staged: Path) -> list[str]:
    """Reset every Git directory in the staged tree (above). Returns their paths
    relative to it, sorted."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(staged):
        names = set(dirnames) | set(filenames)
        if "HEAD" not in names or not ({"objects", "refs"} <= names or "commondir" in names):
            continue
        d = Path(dirpath)
        fmt = _git_object_format(d / "config")
        for rel in ("config", *_GIT_POINTERS):
            p = d / rel
            if any((d / up).is_symlink() for up in Path(rel).parents if str(up) != "."):
                continue
            with contextlib.suppress(FileNotFoundError):
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
        hooks = d / "hooks"
        if hooks.is_symlink() or (hooks.exists() and not hooks.is_dir()):
            hooks.unlink()
        elif hooks.is_dir():
            for h in hooks.iterdir():
                if h.is_dir() and not h.is_symlink():
                    shutil.rmtree(h)
                else:
                    h.unlink()
        # A Git directory named anything but .git is a bare repository: said
        # so, git never takes the folder around it for its work tree.
        bare = "false" if d.name == ".git" else "true"
        text = (f"[core]\n\trepositoryformatversion = 1\n\tbare = {bare}\n"
                "[extensions]\n\tobjectformat = sha256\n" if fmt == "sha256"
                else f"[core]\n\trepositoryformatversion = 0\n\tbare = {bare}\n")
        fd = os.open(d / "config", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as w:
            w.write(text)
        rel_d = os.path.relpath(dirpath, staged)
        found.append("" if rel_d == "." else rel_d)
    return sorted(found)


# The Host's own schema and its role (migrations/v233_host_world.sql).
# Same file the boot runs.
WORLD_SCHEMA = "world"
WORLD_ROLE = "host_world"
WORLD_ROLE_SQL = Path(__file__).resolve().parents[1] / "migrations" / "v233_host_world.sql"


# The kernel's schema is built from this tree's own files by the boot's own
# schema step (entrypoint.sh, between these two markers), never from a dump.
APP_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = APP_ROOT / "entrypoint.sh"
SCHEMA_SECTION = ("# >>> kernel schema section >>>", "# <<< kernel schema section <<<")
# The kernel's tables, the only place a backup's rows go besides `world`.
CORE_SCHEMA = "public"
# The role the backup's rows are loaded as: no superuser, a member of no
# role, INSERT on the kernel's tables and UPDATE on their sequences in the
# restore database while it loads, nothing after.
LOAD_ROLE = "host_restore"
# One line of `pg_restore -l` for a row entry: "<id>; <tableoid> <oid> TABLE
# DATA public <table> <owner>". Only a line of exactly this shape is read.
_ROW_ENTRY = re.compile(r"(\d+); \d+ \d+ (TABLE DATA|SEQUENCE SET) (\S+) (\S+)(?: \S*)?")
# The same for a table's own entry: "<id>; <tableoid> <oid> TABLE public <table> <owner>".
_TABLE_ENTRY = re.compile(r"(\d+); \d+ \d+ TABLE (\S+) (\S+)(?: \S*)?")
# An identifier as pg_dump writes one: plain, or in double quotes.
_IDENT = r'(?:"(?:[^"]|"")+"|[a-z_][a-z0-9_$]*)'
_CREATE_TABLE = re.compile(rf"CREATE (?:UNLOGGED )?TABLE ({_IDENT})\.({_IDENT}) \(")
_COLUMN_LINE = re.compile(rf"    ({_IDENT}) (.+?),?")
# Where a column's type ends in pg_dump's text of it.
_TYPE_END = re.compile(r" (?:COLLATE|DEFAULT|NOT NULL|NULL|GENERATED|CONSTRAINT|CHECK)\b")
# The characters a built-in type's name and modifier are written in. Anything
# else in the dump's text of a type is refused before it reaches a query.
_TYPE_TEXT = re.compile(r'[A-Za-z0-9_ ."(),\[\]]{1,200}')


def _run_quiet(args: list[str], env: Optional[dict] = None) -> tuple[int, str]:
    """(exit code, the tail of what it said) of a client run."""
    with tempfile.TemporaryFile() as out:
        rc = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT, env=env,
                            timeout=hb.RUN_TIMEOUT_S).returncode
        out.seek(0)
        return rc, _tail(out.read().decode(errors="replace"))


def _schema_section() -> str:
    text = ENTRYPOINT.read_text()
    start = text.index(SCHEMA_SECTION[0])
    return text[start:text.index(SCHEMA_SECTION[1], start)]


def _build_own_schema() -> None:
    """The boot's own schema step, run against RESTORE_DB: init-db.sql, every
    migration in this tree, and the two roles' files, as a fresh Host gets
    them. Entity id 0, so the step renumbers nothing: the rows that step puts
    in are emptied before the backup's go in."""
    with tempfile.TemporaryDirectory(prefix="host-restore-schema-") as tmp:
        script = Path(tmp) / "schema.sh"
        script.write_text(_schema_section())
        env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin", "HOME": tmp, "PGSYSCONFDIR": tmp,
               "PG_SOCKET": hb.PG_SOCKET, "ENTITY_DB": RESTORE_DB, "ENTITY_ID": "0",
               "KERNEL_SCHEMA_APP": str(APP_ROOT), "KERNEL_SCHEMA_TMP": tmp}
        if os.environ.get("PGPORT"):
            env["PGPORT"] = os.environ["PGPORT"]
        rc, said = _run_quiet(["sh", "-eu", str(script)], env=env)
    if rc != 0:
        raise RestoreError(f"could not build this Host's own schema to restore into: {said}")


def lock_load_role() -> None:
    """LOAD_ROLE as it stands between restores: it cannot log in, it is a
    member of no role and no role is a member of it, it has no setting in any
    database, and it holds nothing in the restore's database or the Host's.

    _load_rows gives it a login and its grants for the load and takes them
    back after; a restore killed in between (the machine off, the container
    stopped) leaves them. So every restore runs this first (stage), and the
    database container's boot does the same before it serves
    (entrypoint-postgres.sh, lock_restore_role). Raises RestoreError when it
    cannot."""
    r = _psql(f"""DO $$ DECLARE r record; BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LOAD_ROLE}') THEN RETURN; END IF;
          ALTER ROLE "{LOAD_ROLE}" NOLOGIN NOBYPASSRLS;
          ALTER ROLE "{LOAD_ROLE}" RESET ALL;
          FOR r IN SELECT d.datname FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase
                    WHERE s.setrole = '{LOAD_ROLE}'::regrole LOOP
              EXECUTE format('ALTER ROLE %I IN DATABASE %I RESET ALL', '{LOAD_ROLE}', r.datname);
          END LOOP;
          FOR r IN SELECT g.rolname AS granted, u.rolname AS member, w.rolname AS grantor
                     FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                     JOIN pg_roles u ON u.oid = m.member JOIN pg_roles w ON w.oid = m.grantor
                    WHERE m.member = '{LOAD_ROLE}'::regrole OR m.roleid = '{LOAD_ROLE}'::regrole LOOP
              EXECUTE format('REVOKE %I FROM %I GRANTED BY %I CASCADE', r.granted, r.member, r.grantor);
          END LOOP;
        END $$""", db="postgres")
    if r.returncode != 0:
        raise RestoreError(f"could not lock the role a restore loads as: {_tail(r.stderr)}")
    have = _databases()
    for db in (RESTORE_DB, *LIVE_DBS):
        if db not in have:
            continue
        r = _psql(f"""DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LOAD_ROLE}') THEN
                  EXECUTE 'DROP OWNED BY "{LOAD_ROLE}"';
              END IF;
            END $$""", db=db)
        if r.returncode != 0:
            raise RestoreError(f"could not take back what the restore's role holds in {db}: {_tail(r.stderr)}")


def _row_entries(listing: str) -> list[tuple[str, str, str, str]]:
    """(id, kind, schema, name) of each row entry `pg_restore -l` lists."""
    out = []
    for ln in listing.splitlines():
        m = _ROW_ENTRY.fullmatch(ln.rstrip())
        if m:
            out.append(m.groups())
    return out


def _unident(s: str) -> str:
    return s[1:-1].replace('""', '"') if s.startswith('"') else s


def _dump_columns(spool: Path, listing: str, tables: set) -> dict[str, list[tuple[str, str]]]:
    """Each of `tables` (kernel tables the backup holds rows of) with its
    columns as the dump's CREATE TABLE names them: (name, type as written).

    Read as text: `pg_restore -f -` writes the archive's SQL out and connects
    to nothing, and nothing read here is run. A generated column is left out,
    as the dump holds no values for it."""
    ids = []
    for ln in listing.splitlines():
        m = _TABLE_ENTRY.fullmatch(ln.rstrip())
        if m and m.group(2) == CORE_SCHEMA and m.group(3) in tables:
            ids.append(m.group(1))
    if not ids:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as lf:
        lf.write("".join(f"{i};\n" for i in ids))
        list_path = lf.name
    try:
        r = subprocess.run(["pg_restore", "--schema-only", "-f", "-", "-L", list_path, str(spool)],
                           capture_output=True, text=True, errors="replace", timeout=hb.RUN_TIMEOUT_S)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(list_path)
    if r.returncode != 0:
        raise RestoreError("could not read the backup's table definitions: " + _tail(r.stderr))
    out: dict[str, list[tuple[str, str]]] = {}
    cur = None
    for ln in r.stdout.splitlines():
        if cur is None:
            m = _CREATE_TABLE.fullmatch(ln)
            if m and _unident(m.group(1)) == CORE_SCHEMA and _unident(m.group(2)) in tables:
                cur = out.setdefault(_unident(m.group(2)), [])
            continue
        if ln.startswith(")"):
            cur = None
            continue
        m = _COLUMN_LINE.fullmatch(ln)
        if not m or m.group(1) == "CONSTRAINT":
            continue
        rest = m.group(2)
        if " GENERATED ALWAYS AS (" in rest:
            continue
        end = _TYPE_END.search(rest)
        cur.append((_unident(m.group(1)), rest[:end.start()] if end else rest))
    return out


def _load_type(written: str) -> str:
    """The type a column added for the load is made with: `written` (a type
    as the dump's CREATE TABLE writes it) as this database names it.

    Only a base type PostgreSQL itself defines (pg_catalog), or one an
    extension of this Host's own schema defines (pgvector's vector, which
    every backup from before v235 holds a column of), or an array of either.
    Nothing else: a domain carries CHECK expressions, and the dump's own
    types never reach this database, so any other name is one this Host's
    schema made for its own columns. The name is resolved with an empty
    search path, as pg_dump wrote it, and what is returned is the
    database's own spelling of the type, never the dump's text."""
    if not _TYPE_TEXT.fullmatch(written):
        raise RestoreError(f"the backup names a column type this version cannot add for the load: {written!r}")
    lit = "'" + written.replace("'", "''") + "'"
    r = _psql("SET search_path = ''",
              f"""SELECT CASE
                     WHEN to_regtype({lit}) IS NULL THEN 'refused' || chr(9) || 'no such type here'
                     ELSE (SELECT CASE
                         WHEN b.typtype <> 'b' THEN 'refused' || chr(9) || 'not a built-in base type'
                         WHEN b.typnamespace = 'pg_catalog'::regnamespace
                              OR EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_type'::regclass
                                           AND d.objid = b.oid AND d.deptype = 'e')
                           THEN 'ok' || chr(9) || format_type(t.oid, to_regtypemod({lit}))
                         ELSE 'refused' || chr(9) || 'a type made in the database, not a built-in one' END
                       FROM pg_type t
                       JOIN pg_type b ON b.oid = CASE WHEN t.typcategory = 'A' AND t.typelem <> 0
                                                      THEN t.typelem ELSE t.oid END
                      WHERE t.oid = to_regtype({lit}))
                   END""", db=RESTORE_DB)
    said = r.stdout.strip().split("\t", 1)
    if r.returncode != 0 or len(said) != 2 or said[0] != "ok" or not _TYPE_TEXT.fullmatch(said[1]):
        why = said[1] if len(said) == 2 else _tail(r.stderr) or "it could not be read"
        raise RestoreError(f"the backup holds a column of type {written!r}, which this version no longer "
                           f"has and cannot add for the load: {why}")
    return said[1]


def _add_load_columns(spool: Path, listing: str, tables: set) -> list[list[str]]:
    """Each column a table's COPY in the dump names that this version's table
    no longer has, added to RESTORE_DB for the load: the type the dump gives
    it (_load_type), no default, no constraint, no collation. A migration
    newer than the backup moves its contents where this version keeps them
    when the boot runs it; settle() drops what is left. Returns [table,
    column, type] for each one added."""
    if not tables:
        return []
    wanted = _dump_columns(spool, listing, tables)
    if not wanted:
        return []
    r = _psql("SELECT json_agg(json_build_array(c.relname, a.attname)) FROM pg_attribute a "
              "JOIN pg_class c ON c.oid = a.attrelid "
              f"WHERE c.relnamespace = '{CORE_SCHEMA}'::regnamespace AND c.relkind IN ('r', 'p') "
              "AND a.attnum > 0 AND NOT a.attisdropped", db=RESTORE_DB)
    if r.returncode != 0:
        raise RestoreError("could not read the restore database's columns: " + _tail(r.stderr))
    have = {(t, c) for t, c in json.loads(r.stdout.strip() or "null") or []}
    added, alters = [], []
    for table in sorted(wanted):
        for col, written in wanted[table]:
            if (table, col) in have:
                continue
            # Every column this Host's schema ever made is named this way.
            if not _TABLE_NAME.fullmatch(col):
                raise RestoreError(f"the backup names a column of {table} this version cannot add "
                                   "for the load: its name is not one this Host's schema gives")
            typ = _load_type(written)
            alters.append(f'ALTER TABLE "{CORE_SCHEMA}".{_qi(table)} ADD COLUMN {_qi(col)} {typ}')
            added.append([table, col, typ])
            have.add((table, col))
    if alters:
        r = _psql(*alters, db=RESTORE_DB)
        if r.returncode != 0:
            raise RestoreError("could not add the columns the backup's rows need for the load: "
                               + _tail(r.stderr))
        log.info("restore: columns this version no longer has, added for the load: %s",
                 ", ".join(f"{t}.{c}" for t, c, _ in added[:50]))
    return added


def _set_checks_aside() -> list[list[str]]:
    """Every CHECK constraint of the kernel's tables, dropped from RESTORE_DB
    for the load: an older backup's rows may hold values this version's
    checks no longer allow (a memory type v235 retires), which the migrations
    newer than the backup change when the boot runs them over these rows.
    Returns [table, name, definition] for each, as this database's own
    catalog writes it."""
    r = _psql("SELECT json_agg(json_build_array(t.relname, c.conname, pg_get_constraintdef(c.oid))) "
              "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
              f"WHERE c.contype = 'c' AND c.coninhcount = 0 AND t.relkind IN ('r', 'p') "
              f"AND t.relnamespace = '{CORE_SCHEMA}'::regnamespace", db=RESTORE_DB)
    if r.returncode != 0:
        raise RestoreError("could not read the restore database's checks: " + _tail(r.stderr))
    checks = [list(c) for c in json.loads(r.stdout.strip() or "null") or []]
    if checks:
        r = _psql(*[f'ALTER TABLE "{CORE_SCHEMA}".{_qi(t)} DROP CONSTRAINT {_qi(n)}' for t, n, _ in checks],
                  db=RESTORE_DB)
        if r.returncode != 0:
            raise RestoreError("could not set the checks aside for the load: " + _tail(r.stderr))
    return checks


def _put_checks_back(db: str, checks: list, not_valid_after: bool) -> list[list[str]]:
    """Each of `checks` ([table, name, definition], _set_checks_aside) added
    back to `db`, checked over its rows, unless the table already has a
    constraint of that name (a migration put it back). Returns the ones the
    rows did not meet: left out, or with `not_valid_after` added NOT VALID,
    so new rows meet it and the rows the backup brought stay as they were."""
    if not checks:
        return []
    fallback = ("EXECUTE format('ALTER TABLE %I.%I ADD CONSTRAINT %I %s NOT VALID', "
                f"'{CORE_SCHEMA}', c->>0, c->>1, c->>2);" if not_valid_after else "")
    body = json.dumps(checks).replace("'", "''")
    r = _psql("SET standard_conforming_strings = on",
              f"SELECT set_config('cseries_restore.checks', '{body}', false)",
              f"""DO $$ DECLARE c jsonb; unmet jsonb := '[]'; BEGIN
                    FOR c IN SELECT * FROM jsonb_array_elements(current_setting('cseries_restore.checks')::jsonb) LOOP
                        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_constraint k
                                               WHERE k.conrelid = format('%I.%I', '{CORE_SCHEMA}', c->>0)::regclass
                                                 AND k.conname = c->>1);
                        BEGIN
                            EXECUTE format('ALTER TABLE %I.%I ADD CONSTRAINT %I %s',
                                           '{CORE_SCHEMA}', c->>0, c->>1, c->>2);
                        EXCEPTION WHEN check_violation THEN
                            unmet := unmet || jsonb_build_array(c);
                            {fallback}
                        END;
                    END LOOP;
                    PERFORM set_config('cseries_restore.unmet', unmet::text, false);
                  END $$""",
              "SELECT current_setting('cseries_restore.unmet')", db=db, timeout=hb.RUN_TIMEOUT_S)
    if r.returncode != 0:
        raise RestoreError("could not put this version's checks back: " + _tail(r.stderr))
    return [list(c) for c in json.loads(r.stdout.strip().splitlines()[-1])]


def settle(db: str, columns: list, checks: list) -> Optional[str]:
    """After the boot's migrations ran over a restore's rows (carry, below the
    schema step): each column added for the load that the migrations left
    behind is dropped, and each check the backup's rows did not meet at the
    load is put back, NOT VALID when they still do not. Idempotent. Returns a
    note naming what was dropped or left unchecked, or None."""
    notes = []
    cols = [(str(t), str(c)) for t, c, *_ in columns or []]
    if cols:
        body = json.dumps(cols).replace("'", "''")
        r = _psql("SELECT json_agg(x) FROM jsonb_array_elements("
                  f"'{body}'::jsonb) x JOIN pg_attribute a ON a.attrelid = to_regclass("
                  f"format('%I.%I', '{CORE_SCHEMA}', x->>0)) AND a.attname = x->>1 AND NOT a.attisdropped",
                  db=db)
        if r.returncode != 0:
            raise RestoreError("could not read the restored database's columns: " + _tail(r.stderr))
        left = [(t, c) for t, c in json.loads(r.stdout.strip() or "null") or []]
        if left:
            r = _psql(*[f'ALTER TABLE "{CORE_SCHEMA}".{_qi(t)} DROP COLUMN IF EXISTS {_qi(c)}'
                        for t, c in left], db=db)
            if r.returncode != 0:
                raise RestoreError("could not drop the columns added for the load: " + _tail(r.stderr))
            names = ", ".join(f"{t}.{c}" for t, c in left[:50])
            log.warning("restore: columns no update moved, dropped: %s", names)
            notes.append("The backup held columns this version of your Host no longer has, and no "
                         f"update moved what they held, so they were left out: {names}.")
    unmet = _put_checks_back(db, checks or [], not_valid_after=True)
    if unmet:
        names = ", ".join(f"{t} ({n})" for t, n, _ in unmet[:20])
        log.warning("restore: rows that do not meet this version's checks, kept as they were: %s", names)
        notes.append("Some of the backup's rows do not meet rules this version of your Host keeps for "
                     f"new rows, and were kept as they were: {names}.")
    return " ".join(notes) or None


def _load_rows(spool: Path, listing: str) -> dict:
    """The backup's rows of the kernel's tables and its sequences' positions,
    into the schema _build_own_schema made, and nothing else of the dump.

    Every other entry is left out: a function, trigger, index, view, rule,
    policy, extension, default privilege or event trigger in a backup never
    reaches the database. Leaving them out is not enough on its own, since
    pg_restore sends some of the archive's own text as it stands (the search
    path line before anything, and each table's COPY statement), and anyone can
    write an archive that decrypts here. So the rows load as LOAD_ROLE, which
    can insert into the kernel's tables and do nothing a superuser alone may
    do (run a program, read or write a server file, make an extension or an
    event trigger), and whose session runs as a replica: no trigger and no
    foreign-key check fires while the rows go in, as when the dump made its
    keys after its rows. The keys are checked after, by this Host's own
    catalog.

    A backup from before a migration that dropped a column still names that
    column in its COPY: the column is added for the load (_add_load_columns)
    and the boot's migrations move what it holds. Its rows may hold values a
    check of this version no longer allows: the checks are set aside for the
    load and put back after it; one the rows do not meet yet is put back by
    settle() once the migrations have run.

    Returns {"gone": the tables the backup holds rows of that this Host's
    schema no longer has (those rows are left out and named), "columns":
    [table, column, type] added for the load, "checks": [table, name,
    definition] the rows did not meet at the load}."""
    q = f'"{CORE_SCHEMA}"'
    r = _psql("SELECT c.relkind::text || ' ' || c.relname FROM pg_class c "
              f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = '{CORE_SCHEMA}' "
              "AND c.relkind IN ('r', 'p', 'S')", db=RESTORE_DB)
    if r.returncode != 0:
        raise RestoreError("could not read the restore database's tables: " + _tail(r.stderr))
    have = {tuple(ln.split(" ", 1)) for ln in r.stdout.splitlines() if " " in ln}
    keep, gone, loaded = [], [], set()
    for dump_id, kind, schema, name in _row_entries(listing):
        if schema != CORE_SCHEMA:
            continue
        want = ("S",) if kind == "SEQUENCE SET" else ("r", "p")
        if any((k, name) in have for k in want):
            keep.append(dump_id)
            if kind == "TABLE DATA":
                loaded.add(name)
        elif kind == "TABLE DATA":
            gone.append(name)
    added = _add_load_columns(spool, listing, loaded)
    checks = _set_checks_aside()
    role = f'"{LOAD_ROLE}"'
    # The role is the boot's (migrations/v242_host_restore_role.sql, which
    # _build_own_schema ran just now): made there, unable to log in, holding
    # nothing. The load only lends it a login and its grants, here.
    made = _psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{LOAD_ROLE}'", db=RESTORE_DB)
    if made.returncode != 0 or made.stdout.strip() != "1":
        raise RestoreError(f"the database has no {LOAD_ROLE} role for the load (its schema step makes "
                           "it: migrations/v242_host_restore_role.sql)")
    # The rows the schema step put in go: every row comes from the backup.
    tables = ", ".join(f'{q}."{n}"' for k, n in sorted(have) if k in ("r", "p"))
    setup = [
        f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS NOINHERIT",
        f"""DO $$ DECLARE r record; BEGIN
              FOR r IN SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                        WHERE m.member = '{LOAD_ROLE}'::regrole LOOP
                  EXECUTE format('REVOKE %I FROM {LOAD_ROLE}', r.rolname);
              END LOOP;
            END $$""",
        f"ALTER ROLE {role} RESET ALL",
        f'ALTER ROLE {role} IN DATABASE "{RESTORE_DB}" SET session_replication_role = replica',
        f"GRANT USAGE ON SCHEMA {q} TO {role}",
        f"GRANT INSERT ON ALL TABLES IN SCHEMA {q} TO {role}",
        f"GRANT UPDATE ON ALL SEQUENCES IN SCHEMA {q} TO {role}",
    ]
    if tables:
        setup.append(f"TRUNCATE {tables} RESTART IDENTITY")
    r = _psql(*setup, db=RESTORE_DB, timeout=hb.RUN_TIMEOUT_S)
    if r.returncode != 0:
        raise RestoreError("could not make the restore database ready for the backup's rows: "
                           + _tail(r.stderr))
    loaded_ok = False
    try:
        if keep:
            with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as lf:
                lf.write("\n".join(f"{i};" for i in keep) + "\n")
                list_path = lf.name
            try:
                rc, said = _run_quiet(["pg_restore", "-h", hb.PG_SOCKET, "-U", LOAD_ROLE, "-d", RESTORE_DB,
                                       "--no-owner", "--no-privileges", "--exit-on-error",
                                       "-L", list_path, str(spool)])
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(list_path)
            if rc != 0:
                raise RestoreError(f"could not load the database backup: {said}")
        loaded_ok = True
    finally:
        # Nothing of the role is left in the database the Host will run on:
        # its grants, its replica setting, and anything it made (a large
        # object, say) go; it cannot log in until the next restore.
        done = _psql(f"DROP OWNED BY {role}",
                     f'ALTER ROLE {role} IN DATABASE "{RESTORE_DB}" RESET ALL',
                     f"ALTER ROLE {role} NOLOGIN NOBYPASSRLS", db=RESTORE_DB)
        if done.returncode != 0:
            log.warning("restore: could not take back what %s held: %s", LOAD_ROLE, _tail(done.stderr))
            if loaded_ok:
                # This database becomes the Host's: it is never swapped in
                # with a role in it that can log in and write every table.
                raise RestoreError(f"could not take back what {LOAD_ROLE} held after the load: "
                                   + _tail(done.stderr))
    # Each foreign key, as this Host's catalog declares it, holds over the rows.
    r = _psql(f"""DO $$ DECLARE fk record; bad boolean; BEGIN
          FOR fk IN SELECT c.conname, c.conrelid::regclass AS child, c.confrelid::regclass AS parent,
                           ARRAY(SELECT format('%I', a.attname) FROM unnest(c.conkey) WITH ORDINALITY k(n, o)
                                 JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n
                                 ORDER BY k.o) AS cc,
                           ARRAY(SELECT format('%I', a.attname) FROM unnest(c.confkey) WITH ORDINALITY k(n, o)
                                 JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n
                                 ORDER BY k.o) AS pc
                      FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                     WHERE c.contype = 'f' AND n.nspname = '{CORE_SCHEMA}' LOOP
              EXECUTE format('SELECT EXISTS (SELECT 1 FROM %s x WHERE %s AND NOT EXISTS '
                             '(SELECT 1 FROM %s p WHERE %s))', fk.child,
                             (SELECT string_agg('x.' || c || ' IS NOT NULL', ' AND ') FROM unnest(fk.cc) c),
                             fk.parent,
                             (SELECT string_agg('p.' || fk.pc[i] || ' = x.' || fk.cc[i], ' AND ')
                                FROM generate_subscripts(fk.cc, 1) i)) INTO bad;
              IF bad THEN
                  RAISE EXCEPTION 'rows of % name rows of % that are not there (%)',
                      fk.child, fk.parent, fk.conname;
              END IF;
          END LOOP;
        END $$""", db=RESTORE_DB, timeout=hb.RUN_TIMEOUT_S)
    if r.returncode != 0:
        raise RestoreError("the database backup's rows do not fit together: " + _tail(r.stderr))
    unmet = _put_checks_back(RESTORE_DB, checks, not_valid_after=False)
    if unmet:
        log.info("restore: checks the backup's rows meet only after the migrations: %s",
                 ", ".join(f"{t}.{n}" for t, n, _ in unmet[:20]))
    return {"gone": sorted(set(gone)), "columns": added, "checks": unmet}


def _load_database(spool: Path) -> dict:
    """The checked dump into RESTORE_DB.

    Everything that runs in a database is this Host's own code or runs
    without a superuser's rights:
      1. the kernel's schema from this tree's files (_build_own_schema), as a
         fresh Host gets it, `world` and its role included;
      2. the backup's rows of the kernel's tables, and nothing else of the
         dump, loaded as LOAD_ROLE (_load_rows). Its schema_migrations rows
         come with them, so the boot after the swap runs the migrations the
         backup had not had, over these rows, as it always has;
      3. `world`'s objects and rows, connected as host_world. Building an
         index or refreshing a materialized view runs its expressions as the
         object's owner, and after --no-owner that owner is whoever loads it,
         so the Host's own code runs as the Host's own role. Not strict: a
         Host object that will not build as host_world is left out and named,
         and the kernel's restore still lands.
    Returns {"note": what was left out, or None; "columns" and "checks":
    what settle() takes up at the boot (_load_rows)}."""
    listed = subprocess.run(["pg_restore", "-l", str(spool)],
                            capture_output=True, text=True, timeout=hb.RUN_TIMEOUT_S)
    if listed.returncode != 0:
        raise RestoreError("could not read the backup's database listing: " + _tail(listed.stderr))
    _build_own_schema()
    rows = _load_rows(spool, listed.stdout)
    gone = rows["gone"]
    notes = []
    if gone:
        log.warning("restore: the backup holds rows of tables this version no longer has: %s",
                    ", ".join(gone[:50]))
        # Only a name of the shape this Host gives its tables is repeated: the
        # note reaches the owner and the Host's next turn (finish).
        shown = [g for g in gone if _TABLE_NAME.fullmatch(g)]
        more = len(gone) - len(shown[:50])
        notes.append("The backup holds rows of tables this version of your Host no longer has, "
                     f"which were left out: {', '.join(shown[:50]) or 'tables'}"
                     + (f" and {more} more" if more and shown else "") + ".")
    left_out = _load_world(spool)
    if left_out:
        notes.append(left_out)
    return {"note": " ".join(notes) or None, "columns": rows["columns"], "checks": rows["checks"]}


def _load_world(spool: Path) -> Optional[str]:
    """Step 3 of _load_database: `world`'s entries, as host_world."""
    listed = subprocess.run(["pg_restore", "-l", "--schema", WORLD_SCHEMA, str(spool)],
                            capture_output=True, text=True, timeout=hb.RUN_TIMEOUT_S)
    if listed.returncode != 0:
        raise RestoreError("could not read the backup's own-schema listing: " + _tail(listed.stderr))
    # The schema itself exists, owned by host_world (step 1); its CREATE would fail.
    entries = [ln for ln in listed.stdout.splitlines()
               if ln.strip() and not ln.startswith(";")
               and ln.split()[3:6] != ["SCHEMA", "-", WORLD_SCHEMA]]
    if not entries:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as lf:
        lf.write("\n".join(entries) + "\n")
        list_path = lf.name
    try:
        with tempfile.TemporaryFile() as out:
            rc = subprocess.run(["pg_restore", "-h", hb.PG_SOCKET, "-U", WORLD_ROLE, "-d", RESTORE_DB,
                                 "--no-owner", "--no-privileges", "-L", list_path, str(spool)],
                                stdout=out, stderr=subprocess.STDOUT, timeout=hb.RUN_TIMEOUT_S).returncode
            out.seek(0)
            said = out.read(1 << 20).decode(errors="replace")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(list_path)
    if rc != 0:
        log.warning("restore: some of the Host's own objects did not load as %s: %s",
                    WORLD_ROLE, _tail(said))
        # Named by the kind and name of each command that failed, and nothing
        # else of it: the rest quotes the dump's text, which goes to the owner
        # and the Host's next turn (finish) and is the backup's to write.
        failed = []
        for m in _FAILED_CMD.finditer(said):
            what = f"{(m.group(1) or m.group(2) or m.group(3)).lower()} {m.group(4)}"
            if _SHOWABLE.fullmatch(what) and what not in failed:
                failed.append(what)
        named = f": {', '.join(failed[:20])}" if failed else ""
        return f"Some of your Host's own tables or functions did not come back from the backup{named}."
    return None


# pg_restore's "Command was: ..." after a failed command: its kind and the
# name it acts on.
_FAILED_CMD = re.compile(r"Command was: (?:CREATE (?:OR REPLACE )?(?:UNIQUE )?((?:MATERIALIZED )?[A-Z]+)"
                         r"(?: IF NOT EXISTS)?|ALTER (TABLE)(?: ONLY)?|(COPY)) ([A-Za-z0-9_.]{1,120})")
# What may be shown of a name from a backup: a kind, then letters, digits,
# dots and underscores; nothing that reads as words of its own.
_SHOWABLE = re.compile(r"[a-z]{1,20}(?: view)? [A-Za-z0-9_.]{1,120}")
_TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _tail(text: str, n: int = 300) -> str:
    return " ".join((text or "").split())[-n:]


def _write_manifest() -> None:
    """Every file and folder the staged tree holds, relative to the apps root.
    The swap decides what belongs from this list, never from what is left in
    the staged folder, so a swap that restarts halfway decides the same way."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(STAGED):
        rel_dir = os.path.relpath(dirpath, STAGED)
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = f"{rel_dir}/{name}" if rel_dir else name
            kind = "d" if os.path.isdir(full) and not os.path.islink(full) else "f"
            entries.append(kind + rel)
    MANIFEST.write_bytes("\0".join(entries).encode("utf-8", "surrogateescape"))
    MANIFEST.chmod(0o600)


def _manifest_files() -> set[str]:
    raw = MANIFEST.read_bytes().decode("utf-8", "surrogateescape")
    return {e[1:] for e in raw.split("\0") if e.startswith("f")}


async def _clear_staging(pool) -> None:
    await pool.execute(f'DROP DATABASE IF EXISTS "{RESTORE_DB}" WITH (FORCE)')
    await asyncio.to_thread(shutil.rmtree, STAGED, True)
    await asyncio.to_thread(shutil.rmtree, APP_DATA, True)
    with contextlib.suppress(FileNotFoundError):
        (STAGED.parent / SPOOL_NAME).unlink()


def _restart() -> None:
    """Restart the kernel container so entrypoint.sh swaps the restore in:
    SIGTERM to PID 1 (tini), after the response has gone. The same move
    genesis makes after linking (server/routes/genesis.py)."""
    async def later():
        await asyncio.sleep(2)
        try:
            os.kill(1, signal.SIGTERM)
        except Exception as e:
            log.error("restore: restart failed: %s", e)
            os._exit(0)
    asyncio.get_running_loop().create_task(later())


async def cancel(pool) -> bool:
    """Drop a staged restore that has not been swapped in."""
    async with hb._run_lock:
        drop_resealed()
        plan = read_plan()
        if plan is None or plan.get("state") != STAGED_STATE:
            return False
        await _clear_staging(pool)
        _drop_plan()
        await _progress(pool, state="cancelled")
        return True


# ── step 3: swap, at boot (entrypoint.sh) ────────────────────────────────────

def _kept(rel: str) -> bool:
    """A path the backup leaves out, which a restore therefore leaves alone."""
    if rel == RESTORE_DIR.name or rel.startswith(RESTORE_DIR.name + "/"):
        return True
    return hb.excluded(rel)


def _move(src: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def _is_folder(p: Path) -> bool:
    return p.is_dir() and not p.is_symlink()


def _files(base: Path, *, skip_kept: bool) -> list[tuple[str, str]]:
    """(path relative to base, full path) of every file and link under base.
    A link to a folder counts as a file and is never followed. With skip_kept,
    what a restore leaves alone is neither listed nor entered."""
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        rel_dir = "" if rel_dir == "." else rel_dir
        names, keep = list(filenames), []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if skip_kept and _kept(rel):
                continue
            (names if os.path.islink(os.path.join(dirpath, d)) else keep).append(d)
        dirnames[:] = keep
        for f in names:
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if not (skip_kept and _kept(rel)):
                out.append((rel, os.path.join(dirpath, f)))
    return out


def _remove_empty_folders(path: Path) -> None:
    """Remove a folder that holds only empty folders; raises if anything else is in it."""
    for dirpath, _, _ in sorted(os.walk(path), key=lambda w: -len(w[0])):
        os.rmdir(dirpath)


def set_aside(root: Path, manifest_files: set[str], keep=()) -> int:
    """Move every live file the backup does not hold into previous/, except
    under the top-level folders `keep` names (an unverified restore's
    LIVE_KEPT_DIRS). Folders stay where they are (bind mounts). Idempotent."""
    moved = 0
    keep = tuple(keep or ())
    for rel, full in _files(root, skip_kept=True):
        if rel.split("/", 1)[0] in keep:
            continue
        if rel not in manifest_files:
            _move(full, PREVIOUS / rel)
            moved += 1
    return moved


def move_in(root: Path) -> int:
    """Move the restored files from the staged tree into the live one; a live
    file in the way goes to previous/ first. Folders are made, never moved.
    Idempotent: a moved file has left the staged tree."""
    for dirpath, dirnames, _ in os.walk(STAGED):
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, STAGED)
            target = root / rel
            if os.path.lexists(target) and not _is_folder(target):
                _move(str(target), PREVIOUS / rel)      # a file where the backup has a folder
            target.mkdir(exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(target, os.stat(full).st_mode & 0o7777)
    moved = 0
    for rel, full in _files(STAGED, skip_kept=False):
        dst = root / rel
        if _is_folder(dst):
            # A folder where the backup has a file. set_aside already moved
            # the files in it that the backup does not hold; what is left is
            # what a restore leaves alone, and it goes to previous/ as well.
            for sub, inner in _files(dst, skip_kept=False):
                _move(inner, PREVIOUS / rel / sub)
            _remove_empty_folders(dst)
        elif os.path.lexists(dst) and not os.path.lexists(PREVIOUS / rel):
            _move(str(dst), PREVIOUS / rel)
        os.replace(full, dst)
        moved += 1
    return moved


def _psql(*commands: str, db: str = "postgres", timeout: float = 120) -> subprocess.CompletedProcess:
    args = ["psql", "-h", hb.PG_SOCKET, "-U", hb.PG_USER, "-d", db,
            "-v", "ON_ERROR_STOP=1", "-Atq"]
    for c in commands:
        args += ["-c", c]
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _databases() -> set[str]:
    r = _psql("SELECT datname FROM pg_database")
    if r.returncode != 0:
        raise RestoreError(f"cannot list the databases: {r.stderr.strip()[-300:]}")
    return set(r.stdout.split())


def _rename_pair(first: tuple[str, str], second: tuple[str, str]) -> None:
    """Two renames in one transaction, after closing every other session on
    the databases involved. Retried: a closed session takes a moment to go."""
    import time
    names = {first[0], second[0]}
    quoted = ", ".join(f"'{n}'" for n in sorted(names))
    last = ""
    for _ in range(5):
        r = _psql(
            f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            f"WHERE datname IN ({quoted}) AND pid <> pg_backend_pid()",
            "BEGIN",
            f'ALTER DATABASE "{first[0]}" RENAME TO "{first[1]}"',
            f'ALTER DATABASE "{second[0]}" RENAME TO "{second[1]}"',
            "COMMIT")
        if r.returncode == 0:
            return
        last = r.stderr.strip()[-300:]
        time.sleep(1)
    raise RestoreError(f"could not swap the databases: {last}")


def swap(live: str) -> int:
    """entrypoint.sh: `python3 -m server.host_restore swap "$ENTITY_DB"`.
    0 = restored data is in place and not yet confirmed (arm the undo),
    3 = nothing to do, 1 = the swap failed and the Host boots on what it had."""
    if live not in LIVE_DBS:
        print(f"restore: refusing an unknown database name {live!r}", file=sys.stderr)
        return 1
    plan = read_plan()
    if plan is None or plan.get("state") in (FAILED, FINISHING):
        # FINISHING: the kernel already started on the restored data. It is
        # the Host now; finish() clears up and nothing here undoes it.
        return 3
    if plan.get("state") == UNDOING:
        # An undo cut short (a restart in the middle of it): carry it on.
        try:
            undo(live, plan.get("undo_why") or "resumed")
        except Exception as e:
            print(f"restore: putting the previous data back failed again: {e}", file=sys.stderr)
        return 1
    before = f"{live}_before_restore"
    try:
        if plan["state"] == SWAPPED:
            # Swapped on an earlier boot and the kernel has not said it started
            # since (mark_started): give it a few boots, then put things back.
            plan["boots"] = int(plan.get("boots") or 0) + 1
            _write_plan(plan)
            if plan["boots"] >= BOOTS_BEFORE_UNDO:
                undo(live, f"the Host did not start on the restored data in "
                           f"{BOOTS_BEFORE_UNDO} tries")
                return 1
            return 0
        if plan["state"] == STAGED_STATE:
            dbs = _databases()
            if before in dbs:
                raise RestoreError(f"an earlier restore left {before} behind; "
                                   "it has to be looked at before another swap")
            if RESTORE_DB not in dbs:
                raise RestoreError("the restored database is missing")
            _rename_pair((live, before), (RESTORE_DB, live))
            plan.update(state=DB_DONE, live_db=live)
            _write_plan(plan)
        manifest = _manifest_files()
        root = LIVE
        if plan["state"] == DB_DONE:
            plan["set_aside"] = set_aside(root, manifest, keep=plan.get("live_kept") or ())
            plan["state"] = ASIDE_DONE
            _write_plan(plan)
        if plan["state"] == ASIDE_DONE:
            plan["moved_in"] = move_in(root)
            plan.update(state=SWAPPED, swapped_at=hb._now().isoformat(), boots=1)
            _write_plan(plan)
        print(f"restore: swapped in the backup of {plan.get('taken_at')} "
              f"({plan.get('moved_in')} files; {plan.get('set_aside')} set aside)")
        return 0
    except Exception as e:
        msg = str(e)[:600] or type(e).__name__
        print(f"restore: the swap failed: {msg}", file=sys.stderr)
        if plan.get("state") == STAGED_STATE:
            plan.update(state=FAILED, error=msg)
            _write_plan(plan)
        else:
            undo(live, f"the swap failed: {msg}")
        return 1


def undo(live: str, why: str) -> None:
    """Put the previous database and files back and mark the restore failed.

    Only while the previous database is still there. Once it is gone the
    restored data is the only data this Host has, and moving files back
    would leave a Host with neither: the plan goes to FINISHING instead."""
    plan = read_plan()
    if plan is None or plan.get("state") not in (DB_DONE, ASIDE_DONE, SWAPPED, UNDOING):
        return
    live = plan.get("live_db") or live
    before = f"{live}_before_restore"
    try:
        dbs = _databases()
    except RestoreError as e:
        print(f"restore: not undoing, the databases cannot be listed: {e}", file=sys.stderr)
        return
    resuming = plan.get("state") == UNDOING
    if resuming:
        why = plan.get("undo_why") or why
    elif before not in dbs:
        plan.update(state=FINISHING, note=f"not undone ({why}): the previous database is gone")
        _write_plan(plan)
        print("restore: not undoing: the previous database is gone, so the restored data stays",
              file=sys.stderr)
        return
    # Recorded BEFORE anything moves: an undo cut short is resumed at the next
    # boot (swap), never taken for a restore that finished.
    plan.update(state=UNDOING, undo_why=why)
    _write_plan(plan)
    root = LIVE
    # A restored file is one that has left the staged tree; send those back,
    # then return everything the swap set aside.
    for rel in _manifest_files():
        cur = root / rel
        if os.path.lexists(cur) and not os.path.lexists(STAGED / rel) and not _is_folder(cur):
            _move(str(cur), STAGED / rel)
    if PREVIOUS.exists():
        for rel, full in _files(PREVIOUS, skip_kept=False):
            dst = root / rel
            if _is_folder(dst):
                try:
                    _remove_empty_folders(dst)    # a folder the restore made where a file was
                except OSError:
                    print(f"restore: {rel} is a folder with things in it now, so the file "
                          "that was there is not put back", file=sys.stderr)
                    continue
            _move(full, dst)
    if before in dbs:
        _rename_pair((live, RESTORE_DB), (before, live))
    elif not (RESTORE_DB in dbs and live in dbs):
        raise RestoreError(f"cannot put the previous database back: neither {before} nor "
                           f"{RESTORE_DB} is where it should be")
    plan.update(state=FAILED, error=why, undone=True)
    _write_plan(plan)
    print(f"restore: put the previous data back ({why})", file=sys.stderr)


# ── step 4: the kernel started on the restored data ─────────────────────────

def mark_started() -> bool:
    """Called by the kernel (server/main.py lifespan) once it is up. A swapped
    restore is then the Host: from here it is finished, never undone."""
    plan = read_plan()
    if not plan or plan.get("state") != SWAPPED:
        return False
    plan.update(state=FINISHING, started_at=hb._now().isoformat())
    _write_plan(plan)
    return True


# ── step 5: finish ───────────────────────────────────────────────────────────

async def finish(pool, eid: int) -> Optional[dict]:
    """Clear up after a restore the kernel started on, or after one that
    failed, and record the outcome. Idempotent: run again after a failure part
    way through, it carries on and never goes back."""
    async with hb._run_lock:
        plan = read_plan()
        if plan is None:
            drop_resealed()
            if STAGED.exists() or RESTORE_DB in await _db_names(pool):
                await _clear_staging(pool)
            return None
        state = plan.get("state")
        if state in (STAGED_STATE, DB_DONE, ASIDE_DONE, UNDOING):
            # Waiting for a boot to swap it in, or to finish putting the
            # previous data back: neither is an outcome to record.
            return None
        if state == SWAPPED:
            # finish() only runs in a kernel that started: that is the same
            # fact mark_started() records, recorded here if it was missed.
            plan.update(state=FINISHING, started_at=hb._now().isoformat())
            _write_plan(plan)
            state = FINISHING
        started = plan.get("staged_at") or hb._now().isoformat()
        if state == FINISHING and plan.get("stamp"):
            # The backup this came from is in the restored database as its
            # own dump caught it: 'running' (its row is written before the
            # dump and finished after), which the backup loop's boot sweep
            # makes "interrupted". It did finish: it was just restored whole.
            # Recorded so, or the Host reads its one good backup as a failure
            # (backup_status, tell_host_paused) right after every restore.
            taken = plan.get("taken_at")
            await pool.execute(
                "UPDATE host_backup_runs SET state = 'ok', error = NULL, finished_at = $2::timestamptz "
                "WHERE stamp = $1 AND reason <> 'restore' AND (state = 'running' "
                "OR (state = 'failed' AND error LIKE 'interrupted:%'))",
                plan["stamp"], dt.datetime.fromisoformat(taken) if taken else hb._now())
        if state == FINISHING:
            live = plan.get("live_db") or "hostmem"
            # Each app's data first: until it is back the app is not woken
            # (app_data_pending), and the backup lock held here keeps a backup
            # from reading it half put back. A runtime that cannot be reached
            # raises, and finish_soon tries again with what is still pending.
            await _restore_app_data(plan)
            await asyncio.to_thread(shutil.rmtree, APP_DATA, True)
            # After a backup this Host could not confirm as its own: each app
            # it had, installed again from where this Host trusts code from.
            await _reinstall_apps(pool, plan)
            await _keep_undo_point(pool, plan)
            await pool.execute(f'DROP DATABASE IF EXISTS "{live}_before_restore" WITH (FORCE)')
            await asyncio.to_thread(shutil.rmtree, PREVIOUS, True)
            await asyncio.to_thread(shutil.rmtree, STAGED, True)
            # What the carry said about the owner's sign-in (carry_rows), from
            # its marker in this database, before the marker is dropped below.
            carried = await hb._state(pool, CARRIED_MARKER)
            owner = carried.get("owner") if isinstance(carried, dict) else None
            app_note = app_data_note(plan.get("app_data") or [])
            parts: dict[str, Any] = {"restored": {"taken_at": plan.get("taken_at"),
                                                  "owner": owner,
                                                  "code_left_out": plan.get("code_left_out") or [],
                                                  "apps": {a["app_id"]: {k: a.get(k) for k in
                                                                         ("state", "source", "project", "error")}
                                                           for a in plan.get("apps_reinstall") or []
                                                           if isinstance(a, dict) and a.get("app_id")},
                                                  "apps_note": apps_note(plan),
                                                  "app_data": {a["app_id"]: a.get("state")
                                                               for a in plan.get("app_data") or []},
                                                  "app_data_note": app_note,
                                                  "stamp": plan.get("stamp"),
                                                  "files": plan.get("moved_in"),
                                                  "verified": plan.get("verified"),
                                                  "links_left_out": plan.get("links_refused") or [],
                                                  "links_left_out_count": plan.get("links_refused_count")
                                                  or len(plan.get("links_refused") or []),
                                                  "database_note": plan.get("db_note")}}
            outcome, error = "ok", None
        else:
            # FAILED: nothing was moved (a stage or swap that failed before the
            # database swap), or undo() has put everything back and the
            # staged tree holds only the restored copy.
            await _clear_staging(pool)
            await asyncio.to_thread(shutil.rmtree, PREVIOUS, True)
            parts, outcome, error = {}, "failed", plan.get("error") or "the restore failed"
        if not plan.get("recorded"):
            await pool.execute(
                "INSERT INTO host_backup_runs (started_at, finished_at, reason, state, stamp, parts, error) "
                "VALUES ($1::timestamptz, now(), 'restore', $2, $3, $4::jsonb, $5)",
                dt.datetime.fromisoformat(started), outcome, plan.get("stamp"),
                json.dumps(parts), error)
            plan["recorded"] = True
            _write_plan(plan)
        drop_resealed()
        await hb._drop_state(pool, CARRIED_MARKER)
        if plan.get("stamp"):
            await hb._drop_state(pool, f"reseal:{plan['stamp']}")
        _drop_plan()
        # The apps that were running come back now. At this start they were
        # held and woken (supervisor.ensure_resident_apps), and an app whose
        # data was still to be put back was answered still starting
        # (app_data_pending); its hold would try again only at the end of
        # its doubling wait, minutes later. With the plan gone nothing is
        # pending, so each held app is woken now; an app nobody held is not.
        try:
            from server.applications import harness_backend as _hbk
            back = _hbk.rewake_resident_now()
            if back:
                log.info("restore: woken again now that their data is back: %s", ", ".join(sorted(back)))
        except Exception as e:  # noqa: BLE001 — their holds still retry on their own
            log.warning("restore: the apps that were running could not be woken now: %s", e)
        # What the owner is shown once the Host is back (Settings, Backup tab):
        # the links an unverified restore left out are said there, not only
        # in the run record.
        restored = parts.get("restored") or {}
        await _progress(pool, state=outcome, taken_at=plan.get("taken_at"), error=error,
                        verified=restored.get("verified"),
                        app_data_note=restored.get("app_data_note"),
                        links_left_out=restored.get("links_left_out") or [],
                        links_left_out_count=restored.get("links_left_out_count") or 0,
                        owner=restored.get("owner"),
                        database_note=restored.get("database_note"),
                        apps_note=restored.get("apps_note"))
        log.info("restore: %s (%s)", outcome, error or plan.get("taken_at"))
        if outcome == "ok" and restored.get("apps_note"):
            try:
                await _tell_host_apps_note(pool, eid, restored["apps_note"])
            except Exception as e:  # noqa: BLE001 — the restore itself is done
                log.warning("restore: the Host could not be told which apps came back: %s", e)
        if outcome == "ok" and restored.get("owner"):
            try:
                await _tell_host_sign_in(pool, eid, restored["owner"])
            except Exception as e:  # noqa: BLE001 — the restore itself is done
                log.warning("restore: the Host could not be told about the owner's sign-in: %s", e)
        if outcome == "ok" and restored.get("database_note"):
            try:
                await _tell_host_database_note(pool, eid, restored["database_note"])
            except Exception as e:  # noqa: BLE001 — the restore itself is done
                log.warning("restore: the Host could not be told what the restore left out: %s", e)
        if outcome == "ok":
            # Restored onto a machine with no recovery key (a new one): the
            # restore kept this machine's keys (CARRIED_STATE), so nothing is
            # encrypted, and nothing backed up, until the owner adds theirs.
            # Said to the Host now, so its next turn tells them.
            try:
                if not await hb.recipients(pool, eid):
                    await hb.tell_host_paused(pool, eid, why=hb.RESTORED_WITHOUT_KEY)
            except Exception as e:  # noqa: BLE001 — the restore itself is done
                log.warning("restore: the Host could not be told backups are paused: %s", e)
        return {"state": outcome, "error": error, "parts": parts}


# ── a backup this Host cannot confirm brings back no installed code ──────────
#
# A backup that does not verify (provenance) is a file anyone who can write
# where backups are kept could have made, and the code in it would run here as
# this Host: an installed app's files (apps/, installer.INSTALL_ROOT: the
# supervisor runs a process app beside the kernel from there, and the boot's
# image install keeps a built-in app whose restored .factory-fingerprint and
# row match) and the installed App SDK (sdk/, served into every app window and
# checked only against a hash kept beside it). Its installed_applications rows
# say what each app is (provenance decides whether an app is served on the
# Host's own origin, applications/proxy.py is_isolated), where it lives and
# which version it is. So after such a restore none of that comes back:
#   * apps/ and sdk/ are left out of the staged tree, so the swap sets the live
#     ones aside as well, and the boot installs every built-in app and the SDK
#     floor from the image (no fingerprint is left to match);
#   * so is skills/, the platform's skills (skill_loader.get_location): only
#     the image's seeders write there, and what is there rides a turn as the
#     platform's own text ("source": "platform", host_skills.load_type_skill),
#     without the skill standard's check or a mark, and the boot's seeding
#     keeps a file whose seed marker disagrees with it as the owner's edit.
#     The boot seeds it again from the image, and each app's install its own;
#   * passport/ (server/bridge/passport.py) is this Host's network membership:
#     its key, the certificate it presents, and root.crt, the one root the
#     member door is verified against (member_client.MemberClient). The
#     backup's is not brought back and the live one is not set aside
#     (LIVE_KEPT_DIRS, plan live_kept): this Host stays the member it was;
#   * a link the backup holds into any of these leads to what this Host has
#     there, not to the backup's, so it is left out (_link_escapes);
#   * the restored database keeps no installed_applications row: each is
#     written again by the install that puts its app back;
#   * once the kernel has started, every other app the backup had is installed
#     again from where this Host takes code from (_reinstall_apps): its own
#     project, through the normal ship path, for an app it built; the App
#     Store's signed bundle for the rest. One that cannot be is named for the
#     owner, with why (apps_note);
#   * each app's data (its volume, appdata/, its config rows) comes back as
#     from any backup.
# A backup that verifies carries a tag only a key this Host holds can make,
# over a body naming the encrypted file's sha256 (host_backup, 'proof that a
# backup is this Host's own'), so it is byte for byte one this Host wrote, and
# comes back whole.
CODE_DIRS = ("apps", "sdk", "skills")
LIVE_KEPT_DIRS = ("passport",)
APPS_PENDING = "pending"
APPS_REINSTALLED = "reinstalled"
APPS_NEEDS_REINSTALL = "needs reinstall"
APPS_WANTED_MAX = 500
_APP_YAML_MAX = 256 * 1024


def _left_out_unverified(rel: str) -> bool:
    """A path an unverified restore takes from the image or from this Host,
    never from the backup (CODE_DIRS, LIVE_KEPT_DIRS)."""
    top = rel.split("/", 1)[0]
    return top in CODE_DIRS or top in LIVE_KEPT_DIRS


def leave_code_out(staged: Path) -> list[str]:
    """Remove the installed code and trust (CODE_DIRS, LIVE_KEPT_DIRS) from a
    staged tree; returns the names removed. A link in its place is removed,
    never followed."""
    out = []
    for name in CODE_DIRS + LIVE_KEPT_DIRS:
        p = staged / name
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            continue
        out.append(name)
    return out


async def leave_app_rows_out(conn) -> list[dict]:
    """The apps a restored database's installed_applications names, then every
    row of it deleted. Only an app id is taken from a row, and whether the row
    said the Host built the app (provenance custom), which decides only which
    trusted place it is installed from again: the gates of that install decide
    what it is."""
    cols = await _columns(conn, "installed_applications")
    if not cols or "id" not in cols:
        return []
    sel = ", ".join(_qi(c) for c in ("id", "provenance", "status") if c in cols)
    wanted = []
    for r in await conn.fetch(f'SELECT {sel} FROM "installed_applications" ORDER BY "id"'):
        aid = r["id"]
        if not isinstance(aid, str) or not _APP_ID.match(aid):
            continue
        if "status" in cols and r["status"] != "active":
            continue
        wanted.append({"app_id": aid, "state": APPS_PENDING,
                       "built_here": "provenance" in cols and r["provenance"] == "custom"})
        if len(wanted) >= APPS_WANTED_MAX:
            break
    await conn.execute('DELETE FROM "installed_applications"')
    return wanted


def _declared_id(folder: Path) -> Optional[str]:
    """The id a project folder's app.yaml declares, read as data."""
    import yaml
    p = folder / "app.yaml"
    try:
        if p.is_symlink() or not p.is_file() or p.stat().st_size > _APP_YAML_MAX:
            return None
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    aid = doc.get("id") if isinstance(doc, dict) else None
    return aid if isinstance(aid, str) else None


async def _project_for_app(pool, app_id: str) -> Optional[dict]:
    """The Host's project (and module) whose app.yaml declares `app_id`, from
    the project files the restore brought back."""
    root = LIVE / "projects"
    for p in await pool.fetch("SELECT id, name FROM projects ORDER BY id"):
        folder = root / str(int(p["id"]))
        if folder.is_symlink():
            continue
        if _declared_id(folder) == app_id:
            return {"id": int(p["id"]), "name": p["name"], "module": None}
        for m in await pool.fetch("SELECT name FROM project_modules WHERE project_id = $1 ORDER BY name",
                                  int(p["id"])):
            name = str(m["name"])
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name) or (folder / name).is_symlink():
                continue
            if _declared_id(folder / name) == app_id:
                return {"id": int(p["id"]), "name": p["name"], "module": name}
    return None


async def _install_from_project(pool, project_id: int, module: Optional[str]) -> None:
    """The normal ship path (validate, pack, sign with this Host's key,
    install), without the post-ship proof."""
    from server.routes.applications import ShipError, ship_project_service
    try:
        await ship_project_service(pool, project_id, verify=False, module=module)
    except ShipError as e:
        raise RestoreError(str(e.detail)) from None


async def _install_from_store(pool, app_id: str) -> None:
    """The App Store's signed bundle for `app_id`, through the one install
    path (installer.install_bundle checks its signature and its signer)."""
    from server.applications import installer
    from server.routes.applications import _fetch_bundle_from_store, _StoreError
    try:
        bundle = await _fetch_bundle_from_store(app_id, None)
    except _StoreError as e:
        raise RestoreError(f"the App Store did not give it: {e}") from None
    try:
        await installer.install_bundle(pool, bundle, source="store")
    except installer.InstallError as e:
        raise RestoreError(f"the App Store's copy did not install: {e}") from None


async def _reinstall_apps(pool, plan: dict) -> None:
    """Each app an unverified restore left out (plan apps_reinstall), installed
    again, its outcome written to the plan as it lands: from the image (the
    boot already did, so it is only looked up), from the Host's own project
    when the backup said the Host built it, else from the App Store. One that
    cannot be is marked to reinstall, with why."""
    apps = [a for a in plan.get("apps_reinstall") or [] if isinstance(a, dict)]
    if not apps:
        return
    from server.applications import installer, registry
    baked = installer.image_baked_ids()
    for a in apps:
        if a.get("state") != APPS_PENDING:
            continue
        aid = str(a.get("app_id") or "")
        try:
            if not _APP_ID.match(aid):
                raise RestoreError("its name is not an app id")
            if aid in baked:
                a["source"] = "image"
                if await registry.get(pool, aid) is None:
                    raise RestoreError("this Host's image did not install it at this start")
            elif a.get("built_here"):
                a["source"] = "project"
                proj = await _project_for_app(pool, aid)
                if proj is None:
                    raise RestoreError("no project of this Host declares it")
                a["project"] = proj["name"]
                await _install_from_project(pool, proj["id"], proj["module"])
            else:
                a["source"] = "store"
                await _install_from_store(pool, aid)
        except Exception as e:  # noqa: BLE001 — that app's outcome, said to the owner
            a.update(state=APPS_NEEDS_REINSTALL, error=(str(e) or type(e).__name__)[:300])
        else:
            a.update(state=APPS_REINSTALLED, error=None)
        _write_plan(plan)
        log.info("restore: app %s: %s%s", aid, a.get("state"), f" ({a['error']})" if a.get("error") else "")


def apps_note(plan: dict) -> Optional[str]:
    """For the owner, after a restore that brought back no installed code:
    that it did not, which apps were installed again and from where, and which
    need reinstalling and why. None after any other restore."""
    if not plan.get("code_left_out") and not plan.get("apps_reinstall"):
        return None
    apps = [a for a in plan.get("apps_reinstall") or [] if isinstance(a, dict)]
    where = {"image": "from this Host's image", "store": "from the App Store"}

    def src(a):
        return f"from project {a.get('project')}" if a.get("project") else where.get(a.get("source"), "")

    back = [f"{a.get('app_id')} ({src(a)})" for a in apps if a.get("state") == APPS_REINSTALLED]
    left = [f"{a.get('app_id')}: reinstall it {src(a)}".rstrip() + f" ({a.get('error') or 'no reason was kept'})"
            for a in apps if a.get("state") != APPS_REINSTALLED]
    text = ("This Host could not confirm it made this backup, so no app's installed code came back from "
            "it, only each app's data.")
    if back:
        text += " Installed again: " + "; ".join(back[:20]) + ("; and more" if len(back) > 20 else "") + "."
    if left:
        text += (f" {len(left)} app(s) need reinstalling: " + "; ".join(left[:10])
                 + (f"; and {len(left) - 10} more" if len(left) > 10 else "") + ".")
    if "passport" in (plan.get("code_left_out") or []):
        text += (" Its network passport did not come back either: this Host is a member of the "
                 "network only if it was one before the restore.")
    return text[:1500]


async def _tell_host_apps_note(pool, eid: int, note: str) -> None:
    """apps_note, told to the Host the way _tell_host_database_note is, so it
    tells the owner; the restore's result says it too."""
    import uuid
    from mastercode.context_store import ContextStore
    await ContextStore(pool, eid).append_to_list("inbox/messages.json", {
        "id": str(uuid.uuid4()),
        "content": ("You were restored from a backup, and its apps were installed again rather than "
                    "brought back. Tell the owner this now, plainly, in these words or your own: "
                    f"\"{note[:1500]}\""),
        "source": "backups",
        "action": "restore_apps",
        "timestamp": hb._now().isoformat(),
        "delivered_to_session": None,
        "read": False,
    }, max_items=50)


def app_data_note(apps: list) -> Optional[str]:
    """One sentence for the owner naming each app whose data the restore did
    not put back, and why; None when every app's data went back."""
    left = [a for a in apps if isinstance(a, dict) and a.get("state") == APP_DATA_NOT_RESTORED]
    if not left:
        return None
    said = "; ".join(f"{a.get('app_id')}: {a.get('error') or 'no reason was kept'}" for a in left[:10])
    more = f"; and {len(left) - 10} more" if len(left) > 10 else ""
    return (f"The data of {len(left)} app(s) was not put back, and is as it was on this machine: "
            f"{said}{more}.")[:1200]


async def _restore_app_data(plan: dict) -> None:
    """Hand each app's staged data to the local runtime, which replaces the
    app's kept data volume with it (cseries_runtime/server.py
    import_app_data). Each app's outcome is written to the plan as it lands,
    so a finish cut short carries on with the apps still pending. A refusal
    (the disk too short, the data refused) is that app's outcome, said to the
    owner (app_data_note), and its staged copy goes; a runtime that cannot be
    reached is raised, so finish_soon tries again."""
    from server.applications import local_runtime as LR
    apps = [a for a in plan.get("app_data") or [] if isinstance(a, dict)]
    for a in apps:
        if a.get("state") != APP_DATA_PENDING:
            continue
        aid = str(a.get("app_id") or "")
        path = APP_DATA / f"{aid}.tar"
        if not _APP_ID.match(aid):
            a.update(state=APP_DATA_NOT_RESTORED, error="its name is not an app id")
        elif not LR.in_use():
            a.update(state=APP_DATA_NOT_RESTORED,
                     error="this machine runs no local runtime for an app's data to go back to")
        else:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                a.update(state=APP_DATA_NOT_RESTORED, error="its staged copy was gone")
            else:
                with os.fdopen(fd, "rb") as f:
                    try:
                        got = await asyncio.to_thread(LR.import_app_data, aid, f, int(a.get("bytes") or 0),
                                                      a.get("uid_base"))
                    except LR.RuntimeRefusal as e:
                        if e.code in (502, 503):
                            raise RestoreError(f"the data of {aid} could not be put back yet: {e.text}") from None
                        a.update(state=APP_DATA_NOT_RESTORED, error=e.text[:300])
                    else:
                        a.update(state=APP_DATA_RESTORED, data_bytes=got.get("data_bytes"))
        _write_plan(plan)
        if a.get("state") != APP_DATA_PENDING:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        log.info("restore: the data of %s: %s%s", aid, a.get("state"),
                 f" ({a['error']})" if a.get("error") else "")


async def _tell_host_sign_in(pool, eid: int, note: str) -> None:
    """What the restore said about the owner's sign-in (owner_note), as a
    message in the Host's own list, read into its next turn as Messages For
    You (host_backup.tell_host_paused writes the same kind), so the Host tells
    the owner; the restore's result says it too."""
    import uuid
    from mastercode.context_store import ContextStore
    await ContextStore(pool, eid).append_to_list("inbox/messages.json", {
        "id": str(uuid.uuid4()),
        "content": ("You were restored from a backup, and the restore kept the owner's sign-in. Tell "
                    f"the owner this now, plainly, in these words or your own: \"{note}\""),
        "source": "backups",
        "action": "restore_sign_in",
        "timestamp": hb._now().isoformat(),
        "delivered_to_session": None,
        "read": False,
    }, max_items=50)


async def _tell_host_database_note(pool, eid: int, note: str) -> None:
    """What the restore left out of the backup's database (tables and
    columns this version no longer has, rows kept that do not meet a rule, the
    Host's own objects that did not load), told to the Host the same way, so
    it tells the owner; the restore's result says it too."""
    import uuid
    from mastercode.context_store import ContextStore
    await ContextStore(pool, eid).append_to_list("inbox/messages.json", {
        "id": str(uuid.uuid4()),
        "content": ("You were restored from a backup, and the restore could not bring back all of "
                    "it. Tell the owner this now, plainly, in these words or your own: "
                    f"\"{note[:1500]}\""),
        "source": "backups",
        "action": "restore_left_out",
        "timestamp": hb._now().isoformat(),
        "delivered_to_session": None,
        "read": False,
    }, max_items=50)


async def finish_soon(pool, eid: int, tries: int = 30, every_s: float = 60.0) -> None:
    """finish(), retried: a database blip at boot must not leave a restore
    unfinished. Started from the kernel's lifespan."""
    for _ in range(tries):
        try:
            await finish(pool, eid)
            return
        except Exception as e:
            log.warning("restore: finishing failed, trying again: %s", e)
            await asyncio.sleep(every_s)


async def _db_names(pool) -> set[str]:
    return {r["datname"] for r in await pool.fetch("SELECT datname FROM pg_database")}


# ── entrypoint.sh ────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "swap":
        return swap(argv[1])
    if len(argv) >= 2 and argv[0] == "carry":
        return carry(argv[1])
    if len(argv) >= 2 and argv[0] == "undo":
        undo(argv[1], argv[2] if len(argv) > 2 else "undone by hand")
        return 0
    print("usage: python3 -m server.host_restore swap|carry|undo <live database> [why]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
