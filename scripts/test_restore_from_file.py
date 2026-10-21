#!/usr/bin/env python3
"""Restoring from this machine's folder, and from a file the owner uploads
(server/host_restore.py).

  * The restore list holds the backups in the folder on this machine (and its
    exports, and a file the owner uploaded), read from there; a standalone
    Host never asks a network it is not on, and says nothing about one.
  * A file the owner uploads arrives as its key file and then the backup, in
    chunks, each at the offset the Host already holds, so a dropped
    connection picks up where it stopped. Nothing becomes a backup until the
    whole file is there at the size announced. A body carrying a private key
    is refused, wherever it falls in the chunks. One upload is kept at a time,
    never in place of one a restore is reading, and never past the room the
    machine needs to keep running.

    python3 scripts/test_restore_from_file.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import backup_stores  # noqa: E402
from server import host_backup as hb  # noqa: E402
from server import host_restore as hr  # noqa: E402
from server.bridge.no_platform import NoPlatform  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


def run(coro):
    return asyncio.run(coro)


async def chunks(*parts):
    for p in parts:
        yield p


tmp = Path(tempfile.mkdtemp(prefix="rf-test-"))
hb.LOCAL_DIR = tmp / "backups"
S1, S2, S3, S4 = "20261001T020000Z", "20261002T020000Z", "20261003T020000Z", "20261004T020000Z"
BRIDGE = {"bc": NoPlatform()}
hb._raw_bridge = lambda: BRIDGE["bc"]
hb._bridge = lambda: BRIDGE["bc"]
asked = []


async def _no_s3(pool, eid):
    return None
hr._s3_target = _no_s3


async def _call(op, params=None):
    asked.append(op)
    return {"objects": []}
hb.call = _call

store = backup_stores.LocalStore(hb.LOCAL_DIR)
for s in (S1, S2):
    u = store.uploader(s)
    u.part(s.encode() * 10)
    u.complete()
    store.put_sidecar(s, b"key-" + s.encode())
ex = backup_stores.LocalStore(hb.LOCAL_DIR, sub="exports")
u = ex.uploader(S3)
u.part(b"export")
u.complete()
ex.put_sidecar(S3, b"key-export")

print("== the restore list on a standalone Host")
backups, problems = run(hr.list_backups(None, 1))
by = {b["stamp"]: b for b in backups}
check("the backups in this machine's folder are listed, newest first, with the export",
      [b["stamp"] for b in backups] == [S3, S2, S1] and all(b["where"] == "local" for b in backups), backups)
check("  the export by its own name", by[S3]["key"] == f"exports/{S3}.tar.zst.age", by[S3])
check("a standalone Host asks no network and reports no problem with one",
      "backup.list" not in asked and problems == [], (asked, problems))
check("the key file is read from the folder", run(hr._fetch_small(None, 1, by[S2], "sidecar_key")) == b"key-" + S2.encode())
src = run(hr._open_source(None, 1, by[S1]))
check("and the backup opens from the folder for the restore to read", src.read() == S1.encode() * 10)
src.close()
check("restore knows the folder as a place backups are", "local" in hr.WHERE)

print("== the same Host as a network member")


class Member:
    network_member = True
    _connected = True
BRIDGE["bc"] = Member()


async def _call_down(op, params=None):
    asked.append(op)
    raise hb.BackupError("backup.list: the member door is not answering")
hb.call = _call_down
backups, problems = run(hr.list_backups(None, 1))
check("a member asks the network too, and says plainly when it cannot",
      "backup.list" in asked and any("network's storage" in p for p in problems)
      and len(backups) == 3, (asked, problems))
BRIDGE["bc"] = NoPlatform()

print("== a file the owner uploads")
up = hr.LOCAL_UPLOADS = None  # noqa: F841 - the module decides; nothing to set
side = b"-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOQ\n-----END AGE ENCRYPTED FILE-----\n"
got = run(hr.receive_upload(S4, "key", 0, len(side), chunks(side), final=True))
check("the key file arrives whole", got["key_file"] and (hb.LOCAL_DIR / "uploads" / f"{S4}.key.age").read_bytes() == side, got)
data = os.urandom(3000)
got = run(hr.receive_upload(S4, "data", 0, len(data), chunks(data[:1000]), final=False))
check("the backup arrives in chunks: the Host says how much it holds", got["size"] == 1000 and not got["done"], got)
check("  and a backup half-uploaded is not in the restore list",
      S4 not in {b["stamp"] for b in run(hr.list_backups(None, 1))[0]})
try:
    run(hr.receive_upload(S4, "data", 500, len(data), chunks(data[500:1500]), final=False))
    check("a chunk at the wrong offset is refused, saying where to go on from", False)
except hr.RestoreError as e:
    check("a chunk at the wrong offset is refused, saying where to go on from", "1000" in str(e), str(e))
got = run(hr.receive_upload(S4, "data", 1000, len(data), chunks(data[1000:2000], data[2000:]), final=True))
check("the last chunk makes it a backup in the folder for uploads",
      got["done"] and (hb.LOCAL_DIR / "uploads" / f"{S4}.tar.zst.age").read_bytes() == data, got)
backups, _ = run(hr.list_backups(None, 1))
by = {b["stamp"]: b for b in backups}
check("  and it is in the restore list, read from there", by.get(S4, {}).get("key") == f"uploads/{S4}.tar.zst.age", by.get(S4))
try:
    run(hr.receive_upload(S1, "data", 0, 10, chunks(b"0123456789"), final=True))
    check("a stamp the Host already holds is not uploaded over", False)
except hr.RestoreError as e:
    check("a stamp the Host already holds is not uploaded over", "already" in str(e), str(e))
try:
    run(hr.receive_upload("20261005T020000Z", "data", 0, 10, chunks(b"abcd"), final=True))
    check("a size other than the one announced never becomes a backup", False)
except hr.RestoreError as e:
    check("a size other than the one announced never becomes a backup", "10" in str(e) and "4" in str(e), str(e))
check("  and nothing of it is kept",
      not any("20261005" in p.name for p in (hb.LOCAL_DIR / "uploads").iterdir()))

print("== a dropped connection")


async def drops(first):
    yield first
    raise ConnectionResetError("the browser went away")
SD = "20261011T020000Z"
blob = os.urandom(5000)
try:
    run(hr.receive_upload(SD, "data", 0, len(blob), drops(blob[:2000]), final=False))
except ConnectionResetError:
    pass
check("what arrived before the connection dropped stays", hr.upload_status(SD)["size"] == 2000,
      hr.upload_status(SD))
got = run(hr.receive_upload(SD, "data", 2000, len(blob), chunks(blob[2000:]), final=True))
check("  and the upload carries on from there to a whole file",
      got["done"] and (hb.LOCAL_DIR / "uploads" / f"{SD}.tar.zst.age").read_bytes() == blob, got)

print("== nothing that opens anything is uploaded")
for name, parts in (("in one chunk", [b"xx AGE-SECRET-KEY-1" + b"Q" * 58]),
                    ("split across chunks", [b"xxxx AGE-SECRET-", b"KEY-1" + b"Q" * 58]),
                    ("in lower case", [b"age-secret-key-1" + b"q" * 58])):
    try:
        run(hr.receive_upload("20261006T020000Z", "data", 0, sum(map(len, parts)), chunks(*parts), final=True))
        check(f"a private key {name} is refused", False)
    except hr.RestoreError as e:
        check(f"a private key {name} is refused", "private key" in str(e), str(e))
check("  and nothing of it is kept", not any("20261006" in p.name for p in (hb.LOCAL_DIR / "uploads").iterdir()))
try:
    run(hr.receive_upload("20261007T020000Z", "key", 0, 20, chunks(b"not an age key file."), final=True))
    check("a key file that is not an age file is refused", False)
except hr.RestoreError as e:
    check("a key file that is not an age file is refused", True)

print("== one upload at a time")
run(hr.receive_upload(S4, "data", 0, 3, chunks(b"abc"), final=False))
hr.read_plan = lambda: None
got = run(hr.receive_upload("20261008T020000Z", "data", 0, 3, chunks(b"new"), final=False))
left = sorted(p.name for p in (hb.LOCAL_DIR / "uploads").iterdir())
check("a new upload replaces the last one", not any(S4 in n for n in left), left)
hr.read_plan = lambda: {"state": "staged", "stamp": "20261008T020000Z"}
try:
    run(hr.receive_upload("20261009T020000Z", "data", 0, 3, chunks(b"new"), final=False))
    check("never one a restore is reading", False)
except hr.RestoreError as e:
    check("never one a restore is reading", "restore" in str(e), str(e))
hr.read_plan = lambda: None

print("== the machine keeps room to run")
real = backup_stores.local_reserve
hr.backup_stores.local_reserve = lambda total: 1 << 62
try:
    run(hr.receive_upload("20261010T020000Z", "data", 0, 100, chunks(b"x" * 100), final=True))
    check("an upload larger than the room left is refused before a byte is kept", False)
except hr.RestoreError as e:
    check("an upload larger than the room left is refused before a byte is kept", "free" in str(e), str(e))
hr.backup_stores.local_reserve = real

shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all ok")
sys.exit(1 if FAILED else 0)
