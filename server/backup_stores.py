"""
Where a Host's backups go besides the network: a folder on this machine, and
the owner's own S3-compatible bucket.

Both take the stream server/host_backup.py writes for the network's storage,
unchanged: tar | zstd | age, sent part by part (write_file), then a small
sidecar holding the backup's own key, sealed to the owner's keys
(make_sidecar). So a backup is the same two files wherever it lands, named
by its time stamp:

    <stamp>.tar.zst.age    the backup
    <stamp>.key.age        its key file (the sidecar)

and it is restored the same way from any of them (server/host_restore.py).
Neither place is given a key that opens anything: the files are already
encrypted to the owner's recovery key and bastion keys when they arrive.

A FOLDER ON THIS MACHINE (LocalStore). Written as a hidden .part file and
renamed into place only once whole, so a backup that is listed is a backup
that finished; a run that fails removes its .part. The machine keeps room to
run: a part that would leave the disk with less free than local_reserve() is
refused, and the backup fails saying so, rather than filling the disk the
Host's own database writes to. Before refusing, the run makes room by
dropping this folder's oldest backups, a pair at a time (make_room), never
the newest whole one and never one a restore is reading: a disk filled by
old backups would otherwise refuse every new one, and the newest stays the
only one until the next lands. The newest `keep` backups stay; older ones
are dropped in pairs, never one a restore is reading (prune's `protect`).
Exports and files the owner uploaded to restore from live in folders of
their own (exports/, uploads/) and are not pruned with the backups.

THE OWNER'S OWN BUCKET (S3Store). One multipart upload per backup; each part
carries its MD5, so the bucket refuses a part that arrived damaged, and its
ETag is compared as well where it is an MD5. Any failure aborts the upload,
so no half-written object is left (or billed). At the end the bucket is asked
how large the object is. The credential comes from the Host's vault
(server/backup_ship.py _s3_credential); it is never stored with the settings.
The bucket's address must be on the public internet (check_endpoint): the
client checks it, and every address its name resolves to, before each
request it sends.

ONE FILE TO DOWNLOAD (bundle). A backup is downloaded as one plain tar:
README.txt, then the key file, then the backup. The key file comes first so
a browser can read it from the start of the file without reading the rest
(system-apps/backups restore-file.js), and `tar -x` opens it by hand.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import os
import re
import socket
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlsplit

DATA_SUFFIX = ".tar.zst.age"
SIDECAR_SUFFIX = ".key.age"
SUBFOLDERS = ("exports", "uploads")
_STAMP = r"[0-9]{8}T[0-9]{6}Z"
_NAME = re.compile(rf"^(?:(exports|uploads)/)?({_STAMP})(\.tar\.zst\.age|\.key\.age)$")
SIDECAR_MAX = 1 << 20

# The machine keeps this much free whatever a backup needs: the Host's
# database writes to the same disk (its write-ahead log grows with every
# change), and a database that runs out of room stops. A twentieth of the
# disk, and never less than LOCAL_RESERVE_MIN: the local runtime's wake floor
# (vm/runtime cseries_runtime/guard.py DISK_WAKE_FLOOR, 2 GiB), since the
# folder is on the disk the runtime guards. Left with less, the runtime would
# refuse every app's wake, and under its stop floor (1280 MiB, what the
# database may need for its write-ahead log) stop the apps; a backup, which
# now carries each app's data too, is not allowed to cause either.
LOCAL_RESERVE_SHARE = 0.05
LOCAL_RESERVE_MIN = 2 << 30

# What a stopped run left in the folder is removed once it is this old
# (LocalStore.prune): far past the seconds between two parts of a backup.
LEFTOVER_AGE_S = 3600

# A part sent to a bucket is tried this many times, waiting between tries.
PART_TRIES = 3
PART_RETRY_WAIT_S = 2.0


def _err(msg: str) -> Exception:
    from server.host_backup import BackupError
    return BackupError(msg)


def local_reserve(total_bytes: int) -> int:
    """Bytes a folder backup leaves free on a filesystem of `total_bytes`."""
    return max(LOCAL_RESERVE_MIN, int(total_bytes * LOCAL_RESERVE_SHARE))


def taken_at(stamp: str) -> str:
    return dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).isoformat()


def stamp_of_key(key: str) -> Optional[str]:
    m = _NAME.match(key or "")
    return m.group(2) if m else None


# ── a folder on this machine ─────────────────────────────────────────────────

class LocalUploader:
    """One backup file written into the folder, part by part. The same
    interface as host_backup.Uploader: stamp, key, part(), complete(), abort()."""

    def __init__(self, folder: Path, rel: str, stamp: str,
                 reserve: Callable[[int], int],
                 make_room: Optional[Callable[[Callable[[], bool]], int]] = None):
        self.folder, self.stamp, self.reserve, self.make_room = folder, stamp, reserve, make_room
        self.final = folder / f"{stamp}{DATA_SUFFIX}"
        self.key = f"{rel}{stamp}{DATA_SUFFIX}"
        if self.final.exists() or os.path.lexists(self.final):
            raise _err(f"a backup with the time stamp {stamp} is already there")
        self.tmp = folder / f".{stamp}{DATA_SUFFIX}.part"
        with _suppress():
            self.tmp.unlink()
        self.fd: Optional[int] = os.open(self.tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self.size = 0
        self.h = hashlib.sha256()

    def _room(self) -> tuple[int, int]:
        """(bytes free, bytes the machine keeps free) on the folder's disk."""
        st = os.statvfs(self.folder)
        return st.f_bavail * st.f_frsize, self.reserve(st.f_blocks * st.f_frsize)

    def _fits(self, n: int) -> bool:
        free, keep = self._room()
        return free - n >= keep

    def part(self, data) -> None:
        view = memoryview(data)
        if not self._fits(len(view)) and self.make_room is not None:
            # Older backups go first, and only until this part fits.
            self.make_room(lambda: self._fits(len(view)))
        free, keep = self._room()
        if free - len(view) < keep:
            raise _err(f"this machine would have less than {keep >> 20} MiB free if the backup "
                       f"went on (it has {free >> 20} MiB free), so it was stopped: the Host needs "
                       "that room to keep running. Free some space, or choose another place for "
                       "backups in Settings, Backup")
        self.h.update(view)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]
        self.size += len(data)

    def complete(self) -> dict:
        os.fsync(self.fd)
        os.close(self.fd)
        self.fd = None
        os.replace(self.tmp, self.final)
        _fsync_dir(self.folder)
        return {"key": self.key, "size": self.size, "sha256": self.h.hexdigest()}

    def abort(self) -> None:
        if self.fd is not None:
            with _suppress():
                os.close(self.fd)
            self.fd = None
        with _suppress():
            self.tmp.unlink()


class LocalStore:
    """Backups in a folder on this machine. `sub` is one of SUBFOLDERS, or
    "" for the backups themselves. Keys are paths relative to the folder's
    root: <stamp>.tar.zst.age, exports/<stamp>.tar.zst.age, ..."""

    where = "local"

    def __init__(self, root: Path, sub: str = "",
                 reserve: Callable[[int], int] = local_reserve):
        if sub and sub not in SUBFOLDERS:
            raise ValueError(f"unknown backup folder {sub!r}")
        self.root, self.sub, self.reserve = Path(root), sub, reserve
        self.folder = self.root / sub if sub else self.root
        self.rel = f"{sub}/" if sub else ""
        # Stamps a run must not drop to make room (the run sets them: the
        # copies a restore may be reading), besides the newest whole backup.
        self.protect: set = set()

    def _ensure(self) -> None:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    def uploader(self, stamp: str) -> LocalUploader:
        self._ensure()
        return LocalUploader(self.folder, self.rel, stamp, self.reserve,
                             make_room=lambda enough: self.make_room(enough, {stamp}))

    def make_room(self, enough: Callable[[], bool], protect: Optional[set] = None) -> list[str]:
        """Free room for a backup being written: what a stopped run left
        (as prune removes it), then this folder's backups oldest first, a
        pair at a time, until enough() says the part fits. Never the newest
        whole backup, never a stamp in `protect` or self.protect. Returns
        the stamps dropped."""
        protect = set(protect or ()) | self.protect
        self._drop_leftovers(protect)
        dropped: list[str] = []
        stamps = sorted(self.pairs())           # oldest first
        for stamp in stamps[:-1]:               # the newest stays
            if enough():
                break
            if stamp in protect:
                continue
            self.drop(stamp)
            dropped.append(stamp)
        if dropped:
            _fsync_dir(self.folder)
        return dropped

    def object_key(self, stamp: str) -> str:
        return f"{self.rel}{stamp}{DATA_SUFFIX}"

    def put_sidecar(self, stamp: str, data: bytes) -> dict:
        self._ensure()
        final = self.folder / f"{stamp}{SIDECAR_SUFFIX}"
        tmp = self.folder / f".{stamp}{SIDECAR_SUFFIX}.part"
        with _suppress():
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
        _fsync_dir(self.folder)
        return {"key": f"{self.rel}{stamp}{SIDECAR_SUFFIX}", "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def pairs(self) -> dict[str, dict]:
        """{stamp: {where, key, sidecar_key, bytes, taken_at}} for every
        whole backup in this folder (a file with its key file beside it)."""
        out: dict[str, dict] = {}
        try:
            names = os.listdir(self.folder)
        except FileNotFoundError:
            return out
        data, side = {}, set()
        for name in names:
            m = _NAME.match(name)
            if not m or m.group(1):
                continue
            full = self.folder / name
            try:
                st = os.lstat(full)
            except FileNotFoundError:
                continue
            if not _is_regular(st):
                continue
            if m.group(3) == DATA_SUFFIX:
                data[m.group(2)] = st.st_size
            else:
                side.add(m.group(2))
        for stamp, size in data.items():
            if stamp in side:
                out[stamp] = {"where": self.where, "key": f"{self.rel}{stamp}{DATA_SUFFIX}",
                              "sidecar_key": f"{self.rel}{stamp}{SIDECAR_SUFFIX}",
                              "bytes": int(size), "taken_at": taken_at(stamp)}
        return dict(sorted(out.items(), reverse=True))

    def path_of(self, key: str) -> Path:
        """The file a key names, only when it is a backup's name in one of
        this store's folders; raises BackupError for anything else."""
        m = _NAME.match(key or "")
        if not m:
            raise _err("that is not the name of a backup on this machine")
        return self.root / key

    def open(self, key: str):
        """The named file, opened for reading; never through a link."""
        path = self.path_of(key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as e:
            raise _err(f"the backup file could not be opened: {e.strerror}") from None
        st = os.fstat(fd)
        if not _is_regular(st):
            os.close(fd)
            raise _err("that is not a backup file")
        return os.fdopen(fd, "rb")

    def read_small(self, key: str, limit: int = SIDECAR_MAX) -> bytes:
        with self.open(key) as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise _err("that key file is larger than any key file")
        return data

    def drop(self, stamp: str) -> None:
        for suffix in (DATA_SUFFIX, SIDECAR_SUFFIX):
            with _suppress():
                (self.folder / f"{stamp}{suffix}").unlink()

    def prune(self, keep: int, protect: Optional[set] = None) -> list[str]:
        """Drop all but the newest `keep` backups in this folder, a pair at a
        time, never a stamp in `protect`. Returns the stamps dropped.

        Also what no pair accounts for: a hidden .part a run left when the
        kernel stopped under it (abort() never ran), a backup file whose key
        file was never written, and a key file whose backup is gone. Each is
        as large as a backup or opens nothing, and none is ever listed, so
        nothing else would remove them and a machine that restarts mid-backup
        now and then would fill its disk with them. Only one untouched for
        LEFTOVER_AGE_S: a file being written now is touched with every part,
        and a backup's key file follows it within a second, so another writer
        of this folder (a second kernel process, a script) never loses a file
        it is still writing, whatever lock it holds."""
        protect = set(protect or ())
        stamps = sorted(self.pairs(), reverse=True)
        dropped = []
        for stamp in stamps[max(0, int(keep)):]:
            if stamp in protect:
                continue
            self.drop(stamp)
            dropped.append(stamp)
        self._drop_leftovers(protect)
        return dropped

    def _drop_leftovers(self, protect: set) -> None:
        """What no pair accounts for, untouched for LEFTOVER_AGE_S (prune)."""
        cutoff = time.time() - LEFTOVER_AGE_S
        whole = set(self.pairs())
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        for name in names:
            part = name.startswith(".") and name.endswith(".part")
            m = _NAME.match(name[1:-len(".part")] if part else name)
            if not m or m.group(1) or m.group(2) in protect:
                continue
            if part or m.group(2) not in whole:
                with _suppress():
                    st = os.lstat(self.folder / name)
                    if _is_regular(st) and st.st_mtime < cutoff:
                        (self.folder / name).unlink()

    def bundle(self, stamp: str) -> list[tuple[str, Any, int]]:
        """The members of the one-file download for `stamp`:
        (name, bytes or a path, size), README first, then the key file, then
        the backup."""
        data = self.path_of(self.object_key(stamp))
        side = self.read_small(f"{self.rel}{stamp}{SIDECAR_SUFFIX}")
        st = os.lstat(data)
        if not _is_regular(st):
            raise _err("that is not a backup file")
        readme = bundle_readme(stamp).encode()
        return [("README.txt", readme, len(readme)),
                (f"{stamp}{SIDECAR_SUFFIX}", side, len(side)),
                (f"{stamp}{DATA_SUFFIX}", data, st.st_size)]


# ── the owner's own bucket ───────────────────────────────────────────────────
#
# WHERE IT MAY BE. The bucket's address is typed by the owner, and the Host
# then sends requests there signed with the bucket's key. Every machine has
# addresses that are not the internet's: loopback (this Host's own kernel and
# database), link-local (169.254.169.254, the metadata service of a cloud
# machine, which hands out that machine's own credentials), and the private
# ranges (the LAN, a cluster's pod and service networks, a provider's private
# network). A Host on the platform runs inside its cluster, beside services no
# Host may reach. So the address must be a public one: its name is checked,
# and every address the name resolves to, when it is saved and again before
# each request the client sends (a name can be made to answer differently
# later). An address inside another (an IPv4-mapped, 6to4, Teredo or NAT64
# IPv6 address) is judged by the IPv4 address it carries.

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
                          "metadata", "metadata.google.internal", "instance-data"})
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".svc", ".cluster.local")


def _public_ip(ip) -> bool:
    if ip.version == 6:
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is None and ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            return _public_ip(inner)
    return bool(ip.is_global) and not ip.is_multicast


def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to here (a seam the gate replaces)."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def endpoint_problem(host: str, port: Optional[int] = None) -> Optional[str]:
    """Why the owner's bucket may not be at `host`, or None."""
    name = (host or "").strip().rstrip(".").lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not name:
        return "it names no host"
    try:
        literal = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is None and (name in _LOCAL_NAMES or name.endswith(_LOCAL_SUFFIXES)):
        return f"{host} is a name for this machine or the network it is on, not the internet"
    if literal is not None:
        addrs = [str(literal)]
    else:
        try:
            addrs = _resolve(name, port or 443)
        except (OSError, UnicodeError):
            return f"{host} could not be found"
        if not addrs:
            return f"{host} could not be found"
    for a in addrs:
        try:
            ip = ipaddress.ip_address(str(a).split("%", 1)[0])
        except ValueError:
            return f"{host} resolves to {a}, which is not an address"
        if not _public_ip(ip):
            return (f"{host} is {ip}, an address on this machine or a private network, not the "
                    "internet")
    return None


def check_endpoint(endpoint: Optional[str]) -> None:
    """Raise ValueError when the owner's bucket may not be at `endpoint`
    (an https URL; None or empty is Amazon S3 itself)."""
    if not endpoint:
        return
    parts = urlsplit(str(endpoint).strip())
    if parts.scheme != "https":
        raise ValueError("the bucket's address must start with https://")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("the bucket's address has a port that is not a number") from None
    why = endpoint_problem(parts.hostname or "", port)
    if why:
        raise ValueError(f"the bucket's address cannot be used: {why}")


def _guard_request(request, **_kw) -> None:
    """botocore's before-send: the address of each request, as sent."""
    parts = urlsplit(getattr(request, "url", "") or "")
    why = endpoint_problem(parts.hostname or "", parts.port)
    if why:
        raise _err(f"your bucket's address cannot be used: {why}")


def s3_client(creds: dict, target: dict):
    """A client for the owner's bucket. Plain Content-Length bodies, not
    aws-chunked framing: many S3-compatible stores refuse the latter
    (server/backup_ship.py _ship_to_s3_blocking says why). The address is
    checked here and before every request (check_endpoint)."""
    import boto3
    from botocore.config import Config
    try:
        check_endpoint(target.get("endpoint"))
    except ValueError as e:
        raise _err(str(e)) from None
    client = boto3.session.Session(
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
        aws_session_token=creds.get("session_token") or None,
        region_name=target.get("region") or None,
    ).client("s3", endpoint_url=target.get("endpoint") or None,
             config=Config(signature_version="s3v4", retries={"max_attempts": 3},
                           request_checksum_calculation="when_required"))
    client.meta.events.register("before-send", _guard_request)
    return client


def _md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")


class S3Uploader:
    """One backup, one multipart upload. Same interface as LocalUploader."""

    def __init__(self, client, bucket: str, key: str, stamp: str):
        self.client, self.bucket, self.key, self.stamp = client, bucket, key, stamp
        self.upload_id = client.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
        self.parts: list[dict] = []
        self.size = 0
        self.h = hashlib.sha256()

    def part(self, data) -> None:
        body = bytes(data)
        n = len(self.parts) + 1
        self.h.update(body)
        self.size += len(body)
        last: Optional[Exception] = None
        for attempt in range(PART_TRIES):
            try:
                r = self.client.upload_part(Bucket=self.bucket, Key=self.key, PartNumber=n,
                                            UploadId=self.upload_id, Body=body,
                                            ContentMD5=_md5_b64(body))
                etag = (r.get("ETag") or "").strip('"')
                kms = r.get("SSEKMSKeyId") or r.get("ServerSideEncryption") in ("aws:kms", "aws:kms:dsse")
                if (not kms and re.fullmatch(r"[0-9a-f]{32}", etag.lower())
                        and etag.lower() != hashlib.md5(body).hexdigest()):
                    raise _err(f"part {n} arrived damaged in your bucket")
                self.parts.append({"ETag": r["ETag"], "PartNumber": n})
                return
            except Exception as e:  # retried
                last = e
                if attempt + 1 < PART_TRIES:
                    time.sleep(PART_RETRY_WAIT_S * (attempt + 1))
        raise _err(f"part {n} could not be sent to your bucket: {last}")

    def complete(self) -> dict:
        self.client.complete_multipart_upload(Bucket=self.bucket, Key=self.key,
                                              UploadId=self.upload_id,
                                              MultipartUpload={"Parts": self.parts})
        head = self.client.head_object(Bucket=self.bucket, Key=self.key)
        if int(head.get("ContentLength", -1)) != self.size:
            raise _err(f"your bucket holds {head.get('ContentLength')} bytes for {self.key}, but "
                       f"{self.size} were sent")
        return {"key": self.key, "size": self.size, "sha256": self.h.hexdigest()}

    def abort(self) -> None:
        with _suppress():
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=self.key,
                                               UploadId=self.upload_id)


class S3Store:
    """Backups in the owner's own S3-compatible bucket: target as
    server/backup_ship.py validate_target leaves it (address = the bucket)."""

    where = "s3"

    def __init__(self, creds: dict, target: dict, client=None):
        self.creds, self.target = creds, target
        self.bucket = target["address"]
        self.prefix = (target.get("prefix") or "").strip("/")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = s3_client(self.creds, self.target)
        return self._client

    def object_key(self, stamp: str, suffix: str = DATA_SUFFIX) -> str:
        return f"{self.prefix}/{stamp}{suffix}" if self.prefix else f"{stamp}{suffix}"

    def uploader(self, stamp: str) -> S3Uploader:
        return S3Uploader(self.client, self.bucket, self.object_key(stamp), stamp)

    def put_sidecar(self, stamp: str, data: bytes) -> dict:
        key = self.object_key(stamp, SIDECAR_SUFFIX)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentMD5=_md5_b64(data))
        return {"key": key, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def pairs(self) -> dict[str, dict]:
        prefix = f"{self.prefix}/" if self.prefix else ""
        objects, token = [], None
        while True:
            kw: dict = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kw)
            objects += page.get("Contents") or []
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        data, side = {}, {}
        for o in objects:
            key = str(o.get("Key") or "")
            name = key[len(prefix):]
            m = _NAME.match(name)
            if not m or m.group(1):
                continue
            (data if m.group(3) == DATA_SUFFIX else side)[m.group(2)] = o
        out = {}
        for stamp, o in data.items():
            if stamp in side:
                out[stamp] = {"where": self.where, "key": o["Key"], "sidecar_key": side[stamp]["Key"],
                              "bytes": int(o.get("Size") or 0), "taken_at": taken_at(stamp)}
        return dict(sorted(out.items(), reverse=True))


# ── the one-file download ────────────────────────────────────────────────────

def bundle_name(stamp: str) -> str:
    return f"cseries-backup-{stamp}.tar"


def bundle_readme(stamp: str) -> str:
    return f"""A backup of a C-Series Host, taken {taken_at(stamp)}.

This file holds two files:
  {stamp}{SIDECAR_SUFFIX}   the backup's own key, sealed to your recovery key
  {stamp}{DATA_SUFFIX}  the backup itself, encrypted to your recovery key

TO RESTORE IT on any C-Series Host: open the Backups app, choose "Restore from
a file", pick this file and give the page your recovery key. The key stays in
your browser; the Host receives only what your browser makes with it.

TO OPEN IT BY HAND, with age (age-encryption.org) and zstd:
  tar -xf {bundle_name(stamp)}
  age -d -i <your recovery key file> {stamp}{DATA_SUFFIX} | zstd -d | tar -x
The backup, opened, holds a README.txt of its own that says what each part
is and how to load it.
"""


def tar_header(name: str, size: int) -> bytes:
    """A tar member's header, in the PAX format: a size over 8 GiB, which a
    plain header cannot hold, goes in a PAX record that tar reads."""
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime = int(size), 0o600, int(time.time())
    ti.type = tarfile.REGTYPE
    return ti.tobuf(format=tarfile.PAX_FORMAT, encoding="utf-8", errors="strict")


def _padding(size: int) -> int:
    return (-size) % tarfile.BLOCKSIZE


def bundle_size(parts: list) -> int:
    total = 0
    for name, _src, size in parts:
        total += len(tar_header(name, size)) + size + _padding(size)
    return total + 2 * tarfile.BLOCKSIZE


def bundle_stream(parts: list, chunk: int = 1 << 20) -> Iterator[bytes]:
    """The tar, a chunk at a time, read from disk as it goes."""
    for name, src, size in parts:
        yield tar_header(name, size)
        if isinstance(src, (bytes, bytearray)):
            yield bytes(src)
        else:
            left = size
            with open(src, "rb") as f:
                while left > 0:
                    b = f.read(min(chunk, left))
                    if not b:
                        raise _err(f"{name} became shorter while it was being sent")
                    left -= len(b)
                    yield b
        pad = _padding(size)
        if pad:
            yield b"\0" * pad
    yield b"\0" * (2 * tarfile.BLOCKSIZE)


def read_bundle(f) -> tuple[dict, int, int]:
    """Read a downloaded bundle the way the browser does: the key file whole,
    and only where the backup sits. ({name, data} of the key file, offset of
    the backup's bytes, its size)."""
    side: Optional[dict] = None
    with tarfile.open(fileobj=f, mode="r:") as t:
        for m in t:
            if not m.isfile():
                continue
            if m.name.endswith(SIDECAR_SUFFIX):
                if m.size > SIDECAR_MAX:
                    raise _err("that key file is larger than any key file")
                side = {"name": m.name, "data": t.extractfile(m).read()}
            elif m.name.endswith(DATA_SUFFIX):
                if side is None:
                    raise _err("the file holds a backup with no key file before it")
                return side, m.offset_data, m.size
    raise _err("that file holds no backup")


# ── small helpers ────────────────────────────────────────────────────────────

class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, et, e, tb):
        return et is not None and issubclass(et, OSError)


def _is_regular(st) -> bool:
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)


def _fsync_dir(path: Path) -> None:
    with _suppress():
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
