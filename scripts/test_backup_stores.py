#!/usr/bin/env python3
"""Where a Host's backups go besides the network (server/backup_stores.py).

A folder on the machine, and the owner's own S3-compatible bucket, take the
same stream the network's storage takes (server/host_backup.py write_file):
part after part, the file named by its time stamp, its sidecar beside it.
This checks, for the folder: a backup appears only once it is whole, a run
that fails leaves nothing that looks like a backup, the machine's disk is
never filled past what it needs to keep running, the newest backups are kept
and the rest dropped, never one a restore is reading, and nothing outside the
folder can be named. For the bucket (a stand-in client with S3's calls): one
multipart upload per backup with every part's MD5 checked, aborted on any
failure, and the sizes asked of the bucket at the end. And the one-file form
a backup is downloaded in: a plain tar holding a README, the key file and
the backup, in that order, which `tar -x` opens.

    python3 scripts/test_backup_stores.py
"""
import base64
import hashlib
import io
import os
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import backup_stores as bs  # noqa: E402
from server import host_backup as hb  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


def raises(label, fn, exc=Exception, contains=""):
    try:
        fn()
    except exc as e:
        check(label, contains in str(e), str(e))
        return
    except Exception as e:  # noqa: BLE001
        check(label, False, f"raised {type(e).__name__}: {e}")
        return
    check(label, False, "nothing was raised")


tmp = Path(tempfile.mkdtemp(prefix="bs-test-"))
S1, S2, S3, S4 = "20261001T020000Z", "20261002T020000Z", "20261003T020000Z", "20261004T020000Z"

print("== a folder on this machine")
store = bs.LocalStore(tmp / "backups")
up = store.uploader(S1)
check("a backup being written is not listed", store.pairs() == {})
up.part(memoryview(b"a" * 1000))
up.part(b"b" * 500)
check("  not even with parts written (only a hidden .part file exists)",
      store.pairs() == {} and [p.name for p in (tmp / "backups").iterdir()] == [f".{S1}.tar.zst.age.part"],
      list((tmp / "backups").iterdir()))
got = up.complete()
want = hashlib.sha256(b"a" * 1000 + b"b" * 500).hexdigest()
check("complete names the file by its stamp, with its size and sha256",
      got == {"key": f"{S1}.tar.zst.age", "size": 1500, "sha256": want}, got)
check("  and it is the bytes written, readable only by the Host",
      (tmp / "backups" / f"{S1}.tar.zst.age").read_bytes() == b"a" * 1000 + b"b" * 500
      and (tmp / "backups" / f"{S1}.tar.zst.age").stat().st_mode & 0o077 == 0)
check("a file without its key file is not offered", store.pairs() == {})
side = store.put_sidecar(S1, b"-----BEGIN AGE ENCRYPTED FILE-----\nx\n")
check("the key file goes beside it", side["key"] == f"{S1}.key.age" and side["size"] == 37, side)
pairs = store.pairs()
check("a whole pair is listed by its stamp, with the time it was taken",
      list(pairs) == [S1] and pairs[S1]["key"] == f"{S1}.tar.zst.age"
      and pairs[S1]["sidecar_key"] == f"{S1}.key.age" and pairs[S1]["bytes"] == 1500
      and pairs[S1]["taken_at"].startswith("2026-10-01T02:00:00"), pairs)
raises("a second backup with the same stamp is refused, never written over",
       lambda: store.uploader(S1), hb.BackupError, "already")

up = store.uploader(S2)
up.part(b"x" * 10)
up.abort()
check("a backup that failed leaves nothing behind", sorted(p.name for p in (tmp / "backups").iterdir())
      == [f"{S1}.key.age", f"{S1}.tar.zst.age"], list((tmp / "backups").iterdir()))

print("== the machine keeps room to run")
small = bs.LocalStore(tmp / "small", reserve=lambda total: 1 << 62)
up = small.uploader(S1)
raises("a part that would leave less free than the machine needs is refused, saying why",
       lambda: up.part(b"z" * 100), hb.BackupError, "free")
up.abort()
check("  and nothing is left", small.pairs() == {} and not any((tmp / "small").iterdir()))
check("the reserve is a share of the disk with a floor",
      bs.local_reserve(10 << 30) == bs.LOCAL_RESERVE_MIN
      and bs.local_reserve(1000 << 30) == int((1000 << 30) * bs.LOCAL_RESERVE_SHARE))
# The folder is on the disk the local runtime guards (the Host's apps volume,
# under /var/lib/containers): a backup that left it under the runtime's wake
# floor would have every app's wake refused, and under its stop floor the
# apps stopped. Backups now carry each app's data, so they can be large.
sys.path.insert(0, os.path.join(ROOT, "vm/runtime/lib"))
from cseries_runtime import guard as _rg  # noqa: E402
check("a folder backup never leaves the disk under the local runtime's floors",
      bs.LOCAL_RESERVE_MIN >= _rg.DISK_WAKE_FLOOR > _rg.DISK_STOP_FLOOR,
      (bs.LOCAL_RESERVE_MIN, _rg.DISK_WAKE_FLOOR, _rg.DISK_STOP_FLOOR))

print("== a disk filled by old backups")
# A small disk (invented sizes, in bytes) whose only files are the folder's:
# 3 old backups take it to under the reserve, so before a run made room
# every new backup was refused at its first part, and kept being refused.
F1, F2, F3, NEW = "20260901T020000Z", "20260902T020000Z", "20260903T020000Z", "20260904T020000Z"
full = tmp / "full"
CAP, RESERVE = 1500, 300


def _used(path):
    return sum(os.lstat(os.path.join(d, n)).st_size for d, _ds, fs in os.walk(path) for n in fs)


class _Vfs:
    def __init__(self, path):
        self.f_frsize, self.f_blocks = 1, CAP
        self.f_bavail = CAP - _used(full)


def _fill(stamps):
    for s in stamps:
        u = disk.uploader(s)
        u.part(b"o" * 400)
        u.complete()
        disk.put_sidecar(s, b"k")


real_statvfs = os.statvfs
disk = bs.LocalStore(full, reserve=lambda total: RESERVE)
try:
    os.statvfs = lambda p: _Vfs(p) if str(p).startswith(str(full)) else real_statvfs(p)
    full.mkdir()
    CAP = 10_000
    _fill((F1, F2, F3))
    CAP = 1500
    check("the disk is past its reserve before the run", CAP - _used(full) < RESERVE, _used(full))
    up = disk.uploader(NEW)
    got = None
    try:
        for _ in range(4):
            up.part(b"n" * 100)
        got = up.complete()
        disk.put_sidecar(NEW, b"k")
    except Exception as e:  # noqa: BLE001 — the check says what it was
        up.abort()
        got = e
    check("a run on a disk filled by old backups makes room and lands",
          isinstance(got, dict) and got["size"] == 400 and NEW in disk.pairs(), got)
    check("  the oldest went first, only as many as it needed, and the newest old one stays",
          sorted(disk.pairs()) == [F3, NEW], sorted(disk.pairs()))
    check("  and the machine's reserve was kept the whole way", CAP - _used(full) >= RESERVE, _used(full))
    # Again, with the one a restore may be reading protected and no room
    # without it: refused, saying why, the newest and the protected kept.
    for s in list(disk.pairs()):
        disk.drop(s)
    CAP = 10_000
    _fill((F1, F2, F3))
    CAP = 1350
    disk.protect = {F1}
    up = disk.uploader(NEW)
    raises("with nothing it may drop left, a run is refused, saying why",
           lambda: [up.part(b"n" * 100) for _ in range(4)], hb.BackupError, "free")
    up.abort()
    check("  never the newest whole backup, never one a restore may be reading",
          sorted(disk.pairs()) == [F1, F3], sorted(disk.pairs()))
    disk.protect = set()
    CAP = 10_000
    _fill((F2,))
    before = sorted(disk.pairs())
    up = disk.uploader(NEW)
    up.part(b"n" * 100)
    up.abort()
    check("on a disk with room nothing is dropped before the run lands", sorted(disk.pairs()) == before,
          sorted(disk.pairs()))
finally:
    os.statvfs = real_statvfs

print("== keeping the newest")
for s in (S2, S3, S4):
    u = store.uploader(s)
    u.part(s.encode())
    u.complete()
    store.put_sidecar(s, b"k")
dropped = store.prune(2, protect={S1})
check("the newest are kept, older ones dropped, never a protected one",
      sorted(dropped) == [S2] and sorted(store.pairs()) == [S1, S3, S4], (dropped, sorted(store.pairs())))
check("a pair goes whole: no key file is left without its backup",
      not (tmp / "backups" / f"{S2}.key.age").exists())
(tmp / "backups" / "notes.txt").write_text("mine")
store.prune(1)
check("files that are not backups are never touched", (tmp / "backups" / "notes.txt").exists())
# What a run leaves when the kernel stops under it (abort() never ran): a
# hidden .part as large as the backup got, a backup whose key file was never
# written, a key file with no backup. None is ever listed, so nothing but
# prune would remove them, and a machine that restarts mid-backup now and then
# would fill its disk with them.
S5, S6, S7, S8 = "20261005T020000Z", "20261006T020000Z", "20261007T020000Z", "20261008T020000Z"
folder = tmp / "backups"
(folder / f".{S5}.tar.zst.age.part").write_bytes(b"x" * 1000)
(folder / f"{S6}.tar.zst.age").write_bytes(b"y" * 1000)
(folder / f"{S7}.key.age").write_bytes(b"k")
(folder / f".{S8}.tar.zst.age.part").write_bytes(b"z" * 10)
(folder / f".{S6}.key.age.part").write_bytes(b"k")
_old = __import__("time").time() - 2 * bs.LEFTOVER_AGE_S
for n in (f".{S5}.tar.zst.age.part", f"{S6}.tar.zst.age", f"{S7}.key.age", f".{S8}.tar.zst.age.part",
          f".{S6}.key.age.part"):
    os.utime(folder / n, (_old, _old))
store.prune(5, protect={S8})
left = sorted(os.listdir(folder))
check("prune removes what a stopped run left: its .part, a backup with no key file, a key file with no backup",
      not any(n in left for n in (f".{S5}.tar.zst.age.part", f"{S6}.tar.zst.age", f"{S7}.key.age",
                                  f".{S6}.key.age.part")), left)
check("  but never one a restore may be reading, nor a whole backup, nor what is not a backup",
      f".{S8}.tar.zst.age.part" in left and f"{S4}.tar.zst.age" in left and f"{S4}.key.age" in left
      and "notes.txt" in left, left)
(folder / f".{S8}.tar.zst.age.part").unlink()
# Another writer of the folder (a second kernel process, a script), part way
# through a backup when this one prunes: its .part is fresh, and stays, and
# its backup completes.
S9 = "20261009T020000Z"
other = store.uploader(S9)
other.part(b"w" * 1000)
(folder / f"{S7}.key.age").write_bytes(b"k")         # a key file whose backup is a moment away
store.prune(5)
check("a .part another writer is still writing is never removed, and its backup completes",
      (folder / f".{S9}.tar.zst.age.part").exists() and other.complete()["size"] == 1000
      and (folder / f"{S9}.tar.zst.age").exists(), sorted(os.listdir(folder)))
check("  nor a fresh key file whose backup has not landed yet", (folder / f"{S7}.key.age").exists())
store.put_sidecar(S9, b"k")
(folder / f"{S7}.key.age").unlink()

print("== nothing outside the folder can be named")
for bad in ("../x.tar.zst.age", "/etc/passwd", f"exports/../{S3}.tar.zst.age", "notes.txt",
            f"sub/{S3}.tar.zst.age", f"{S3}.tar.zst.age/x", ""):
    raises(f"open refuses {bad!r}", lambda b=bad: store.open(b), hb.BackupError)
with store.open(f"{S4}.tar.zst.age") as f:
    check("a listed backup opens for reading", f.read() == S4.encode())
check("a key file is read whole, up to a bound", store.read_small(f"{S4}.key.age") == b"k")
ex = bs.LocalStore(tmp / "backups", sub="exports")
u = ex.uploader(S4)
u.part(b"export")
u.complete()
ex.put_sidecar(S4, b"ek")
check("an export lives in its own folder, named exports/<stamp>",
      ex.pairs()[S4]["key"] == f"exports/{S4}.tar.zst.age" and S4 in store.pairs()
      and store.pairs()[S4]["key"] == f"{S4}.tar.zst.age")
with store.open(f"exports/{S4}.tar.zst.age") as f:
    check("  and opens from the folder's root by that name", f.read() == b"export")

print("== one file to download")
parts = store.bundle(S4)
data = b"".join(bs.bundle_stream(parts))
check("the stream is exactly as long as it says", len(data) == bs.bundle_size(parts), (len(data), bs.bundle_size(parts)))
with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
    names = t.getnames()
    body = {m.name: t.extractfile(m).read() for m in t if m.isfile()}
check("a plain tar: the README, then the key file, then the backup", names
      == ["README.txt", f"{S4}.key.age", f"{S4}.tar.zst.age"], names)
check("  holding the files as they are", body[f"{S4}.key.age"] == b"k" and body[f"{S4}.tar.zst.age"] == S4.encode())
check("  and the README says how to open it by hand and how to restore it",
      "age -d -i" in body["README.txt"].decode() and "Backups app" in body["README.txt"].decode())
check("its name says what it is", bs.bundle_name(S4) == f"cseries-backup-{S4}.tar")
sc, off, size = bs.read_bundle(io.BytesIO(data))
check("read back: the key file, and where the backup sits in it",
      sc["name"] == f"{S4}.key.age" and sc["data"] == b"k" and data[off:off + size] == S4.encode(), (sc, off, size))
# A backup larger than 8 GiB does not fit a plain tar header's size field; the
# header the stream writes says its size in a PAX record instead.
big = bs.tar_header("x.tar.zst.age", 9 << 30)
with tarfile.open(fileobj=io.BytesIO(big + b"\0" * 1024), mode="r:") as t:
    m = t.next()
check("a member over 8 GiB has its size in a PAX record tar reads", m.size == 9 << 30, m.size)


print("== the owner's own bucket")


class FakeS3:
    """S3's calls, as boto3 makes them, against a dict."""

    def __init__(self):
        self.objects, self.uploads, self.calls = {}, {}, []
        self.fail_part = None
        self.lie_size = False

    def create_multipart_upload(self, Bucket, Key):
        self.calls.append(("create", Key))
        uid = f"u{len(self.uploads) + 1}"
        self.uploads[uid] = {"key": Key, "parts": {}}
        return {"UploadId": uid}

    def upload_part(self, Bucket, Key, PartNumber, UploadId, Body, ContentMD5):
        self.calls.append(("part", PartNumber))
        if self.fail_part == PartNumber:
            raise RuntimeError("connection reset")
        body = bytes(Body)
        if base64.b64encode(hashlib.md5(body).digest()).decode() != ContentMD5:
            raise RuntimeError("BadDigest")
        self.uploads[UploadId]["parts"][PartNumber] = body
        return {"ETag": '"' + hashlib.md5(body).hexdigest() + '"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self.calls.append(("complete", Key))
        up = self.uploads.pop(UploadId)
        self.objects[Key] = b"".join(up["parts"][p["PartNumber"]] for p in MultipartUpload["Parts"])

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.calls.append(("abort", Key))
        self.uploads.pop(UploadId, None)

    def put_object(self, Bucket, Key, Body, ContentMD5):
        self.calls.append(("put", Key))
        self.objects[Key] = bytes(Body)

    def head_object(self, Bucket, Key):
        n = len(self.objects[Key])
        return {"ContentLength": n + (1 if self.lie_size else 0)}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k, "Size": len(v)} for k, v in sorted(self.objects.items())
                             if k.startswith(Prefix)], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


fake = FakeS3()
s3 = bs.S3Store({"access_key_id": "AKEXAMPLE", "secret_access_key": "example-secret"},
                {"kind": "s3", "address": "my-backups", "prefix": "host-a", "endpoint": "https://s3.example.test"},
                client=fake)
up = s3.uploader(S1)
up.part(memoryview(b"p" * 100))
up.part(b"q" * 50)
got = up.complete()
check("one multipart upload, named prefix/<stamp>.tar.zst.age", got["key"] == f"host-a/{S1}.tar.zst.age"
      and fake.objects[got["key"]] == b"p" * 100 + b"q" * 50 and got["size"] == 150
      and [c[0] for c in fake.calls] == ["create", "part", "part", "complete"], (got, fake.calls))
side = s3.put_sidecar(S1, b"sealed")
check("the key file beside it", side["key"] == f"host-a/{S1}.key.age" and fake.objects[side["key"]] == b"sealed")
pairs = s3.pairs()
check("the bucket lists the pair by its stamp", list(pairs) == [S1] and pairs[S1]["bytes"] == 150
      and pairs[S1]["where"] == "s3", pairs)
fake.fail_part = 2
bs.PART_RETRY_WAIT_S = 0
up = s3.uploader(S2)
up.part(b"1" * 10)
raises("a part that cannot be sent fails the backup (after its retries)", lambda: up.part(b"2" * 10),
       hb.BackupError, "part 2")
up.abort()
check("  and the upload is aborted: nothing half-written is left", fake.calls[-1][0] == "abort"
      and not fake.uploads and f"host-a/{S2}.tar.zst.age" not in fake.objects, fake.calls[-3:])
fake.fail_part = None
fake.lie_size = True
up = s3.uploader(S3)
up.part(b"x")
raises("the bucket is asked what it holds; a size it did not receive fails the backup",
       up.complete, hb.BackupError, "bucket")
check("an S3 store with no prefix names files at the bucket's top",
      bs.S3Store({}, {"kind": "s3", "address": "b"}, client=fake).object_key(S1) == f"{S1}.tar.zst.age")

print("== where the owner's bucket may be: the internet, never this machine or a private network")
from server import host_restore as hr  # noqa: E402
_real_resolve = bs._resolve
ADDRS = {"s3.example.test": ["34.120.0.10"], "inside.example.test": ["10.20.30.40"],
         "mixed.example.test": ["34.120.0.11", "fd00::5"], "v6.example.test": ["2600:1f18::10"]}


def _resolve(host, port):
    if host not in ADDRS:
        raise OSError("no such host")
    return list(ADDRS[host])
bs._resolve = _resolve
for endpoint in (None, "", "https://s3.example.test", "https://s3.example.test:9000/", "https://v6.example.test",
                 "https://34.120.0.10"):
    try:
        bs.check_endpoint(endpoint)
        check(f"{endpoint!r} may be used", True)
    except ValueError as e:
        check(f"{endpoint!r} may be used", False, e)
for endpoint, why in (
        ("http://s3.example.test", "https://"),
        ("https://127.0.0.1", "private network"), ("https://127.9.9.9:9000", "private network"),
        ("https://localhost:9000", "this machine"), ("https://0.0.0.0", "private network"),
        ("https://169.254.169.254", "private network"), ("https://[fe80::1]", "private network"),
        ("https://[::1]", "private network"), ("https://[fd00::1]", "private network"),
        ("https://10.0.0.1", "private network"), ("https://172.16.0.9", "private network"),
        ("https://192.168.1.10", "private network"), ("https://100.64.0.1", "private network"),
        ("https://224.0.0.1", "private network"),
        ("https://[::ffff:10.0.0.1]", "private network"),
        ("https://[64:ff9b::a9fe:a9fe]", "private network"),
        ("https://[2002:c0a8:101::1]", "private network"),
        ("https://metadata.google.internal", "this machine"), ("https://metadata", "this machine"),
        ("https://minio.backup.svc", "this machine"), ("https://minio.backup.svc.cluster.local.", "this machine"),
        ("https://nas.local", "this machine"),
        ("https://inside.example.test", "10.20.30.40"), ("https://mixed.example.test", "fd00::5"),
        ("https://gone.example.test", "could not be found"), ("https://s3.example.test:abc", "port")):
    try:
        bs.check_endpoint(endpoint)
        check(f"{endpoint!r} is refused", False, "it was accepted")
    except ValueError as e:
        check(f"{endpoint!r} is refused ({why})", why in str(e), e)

import botocore.httpsession  # noqa: E402
sent = []
_real_send = botocore.httpsession.URLLib3Session.send


def _no_send(self, request):
    sent.append(request.url)
    raise RuntimeError("no request leaves this gate")
botocore.httpsession.URLLib3Session.send = _no_send
try:
    creds = {"access_key_id": "AKEXAMPLE", "secret_access_key": "example-secret"}
    raises("a client is not made for a bucket on a private network", lambda: bs.s3_client(
        creds, {"endpoint": "https://inside.example.test", "region": "us-east-1"}), hb.BackupError, "10.20.30.40")
    client = bs.s3_client(creds, {"endpoint": "https://s3.example.test", "region": "us-east-1"})
    raises("a request to a public bucket goes to the wire (stopped here, at the HTTP layer)",
           lambda: client.list_objects_v2(Bucket="my-backups"), RuntimeError, "no request leaves")
    check("  that one was sent", len(sent) == 1 and sent[0].startswith("https://s3.example.test"), sent)
    ADDRS["s3.example.test"] = ["10.9.8.7"]
    raises("a name that answers a private address later is refused before the request is sent",
           lambda: client.list_objects_v2(Bucket="my-backups"), hb.BackupError, "10.9.8.7")
    check("  and nothing more was sent", len(sent) == 1, sent)
    raises("the restore's client is the same checked one", lambda: hr._s3_client(
        creds, {"endpoint": "https://inside.example.test"}), hr.RestoreError, "private network")
finally:
    botocore.httpsession.URLLib3Session.send = _real_send
    bs._resolve = _real_resolve

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all ok")
sys.exit(1 if FAILED else 0)
