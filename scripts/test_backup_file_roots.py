#!/usr/bin/env python3
"""The Host's other volumes in its backups (host_backup.FILE_ROOTS: secrets,
home, user-files, workspace), and a restore putting them back.

Pinned here, on temporary folders standing in for the four volumes, with real
`age`, a stand-in pg_dump and pg_restore, and the real writer, unpacker and
swap:

  1. a backup holds files/<key>/ for each volume, sealed in the same age
     stream as the database (nothing of them readable in the file without the
     key), and leaves out what a backup leaves out there (node_modules,
     .cache, the restore's own folder);
  2. a restore stages each volume inside itself, the swap puts the backup's
     files back and sets aside what came after, and undo puts it all back;
  3. a backup this Host cannot confirm it made leaves the home folder and the
     keys folder as they are, and resets the settings of the Git folders it
     brings into the workspace; this Host's own keeps them;
  4. a backup from before these volumes were backed up leaves them as they
     are;
  5. a volume that would take the backup past the list's limit, or past the
     room its destination has, is left out whole, the rest still taken, and
     the run says which and why (partial); the apps tree, the database and
     the Git mirrors are never what gives way.

    python3 scripts/test_backup_file_roots.py
"""
import asyncio
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import age_keys  # noqa: E402
from server import host_backup as hb  # noqa: E402
from server import host_restore as hr  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


tmp = Path(tempfile.mkdtemp(prefix="hb-roots-"))
if not os.path.isfile(hb.AGE):
    hb.AGE = shutil.which("age") or hb.AGE
ident, rec = age_keys.new_pair()

# ── stand-ins: pg_dump, pg_restore, psql ────────────────────────────────────
bin_dir = tmp / "bin"
bin_dir.mkdir()
DUMP = b"PGDMP" + os.urandom(500)
(tmp / "dump.bin").write_bytes(DUMP)
(bin_dir / "pg_dump").write_text(f'#!/bin/sh\nexec cat "{tmp / "dump.bin"}"\n')
restored_dump = tmp / "restored.dump"
(bin_dir / "pg_restore").write_text(
    '#!/bin/sh\nlast=""; list=""; world=""\n'
    'for a in "$@"; do last="$a"; [ "$a" = "-l" ] && list=1; [ "$a" = "--schema" ] && world=1; done\n'
    'if [ -n "$list" ]; then [ -z "$world" ] && echo "6001; 0 16800 TABLE DATA public t1 entity"; exit 0; fi\n'
    f'if [ -f "$last" ]; then cat "$last" > "{restored_dump}"; else cat > "{restored_dump}"; fi\n')
(bin_dir / "psql").write_text('#!/bin/sh\ncase "$*" in *"relkind::text"*) echo "r t1";;\n'
                              '  *"rolname = \'host_restore\'"*) echo 1;; esac\nexit 0\n')
for b in bin_dir.iterdir():
    b.chmod(0o755)
os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
hb.PG_DUMP = str(bin_dir / "pg_dump")
hr._build_own_schema = lambda: None
hr._room = lambda path: hr.Room(1 << 40, 4096, 0, 0, 0)


def put(base: Path, rel: str, data):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data)
    return p


# ── the Host: an apps volume and the four other volumes ─────────────────────
apps = tmp / "apps"
put(apps, "app-state/notes.txt", "apps notes\n")
hb.APPS_ROOT = str(apps)
hb.APPS_EXCLUDES = tuple(p.replace("/var/lib/cseries", str(apps)) for p in hb.APPS_EXCLUDES)
hb.LOCAL_DIR = apps / ".host-backups"
hb.WORKDIR = tmp / "work"
hb.DB_CHUNK = 300
vol = {k: tmp / "vol" / k for k in ("secrets", "home", "user-files", "workspace")}
hb.FILE_ROOTS = [(k, str(p)) for k, p in vol.items()]
for p in vol.values():
    p.mkdir(parents=True)


def fill():
    for p in vol.values():
        for child in p.iterdir():
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    put(vol["secrets"], "oauth-state.key", b"\x01" * 32).chmod(0o600)
    put(vol["home"], ".gitconfig", "[user]\n\tname = then\n")
    put(vol["home"], ".claude/settings.json", '{"then": true}\n')
    put(vol["home"], ".cache/pip/x", "cache\n")
    put(vol["user-files"], "letters/a.txt", "dear\n")
    (vol["user-files"] / "letters/latest").symlink_to("a.txt")
    put(vol["workspace"], "proj/main.py", "print('then')\n")
    put(vol["workspace"], "proj/node_modules/dep/index.js", "dep\n")
    put(vol["workspace"], "proj/.git/HEAD", "ref: refs/heads/main\n")
    put(vol["workspace"], "proj/.git/config",
        '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://git.example.org/me/proj.git\n')
    (vol["workspace"] / "proj/.git/objects").mkdir()
    (vol["workspace"] / "proj/.git/refs").mkdir()
    put(vol["workspace"], ".host-restore/leftover", "never backed up\n")


class Collect:
    def __init__(self):
        self.buf = bytearray()

    def part(self, view):
        self.buf += bytes(view)


def backup(file_roots):
    c = Collect()
    out = hb.write_file(upload=c, recips=[rec], eid=7, stamp="20261102T000000Z",
                        kind=hb.KIND_BACKUP, dbname="hostmem", git_dir=None,
                        file_roots=file_roots)
    return bytes(c.buf), out


def members(blob) -> dict:
    """name -> bytes (files) or None, of a backup opened with the key."""
    from compression import zstd
    p = tmp / "id.txt"
    p.write_text(ident + "\n")
    raw = subprocess.run([hb.AGE, "-d", "-i", str(p)], input=blob, capture_output=True, check=True).stdout
    import tarfile
    out = {}
    with tarfile.open(fileobj=io.BytesIO(zstd.decompress(raw)), mode="r") as t:
        for m in t:
            out[m.name] = t.extractfile(m).read() if m.isfile() else None
    return out


print("== a backup holds the four volumes, sealed with the database")
fill()
roots = [(k, str(p)) for k, p in vol.items()]
blob, written = backup(roots)
got = members(blob)
for name in ("files/secrets/oauth-state.key", "files/home/.gitconfig", "files/home/.claude/settings.json",
             "files/user-files/letters/a.txt", "files/user-files/letters/latest",
             "files/workspace/proj/main.py", "files/workspace/proj/.git/config"):
    check(f"it holds {name}", name in got, sorted(got)[:40])
check("beside the database and the apps tree, in the one file",
      "db/hostmem.dump.00000" in got and "apps/app-state/notes.txt" in got)
check("what a backup leaves out stays out (node_modules, .cache, the restore's own folder)",
      not any("node_modules" in n or "/.cache/" in n or ".host-restore" in n for n in got), sorted(got))
check("nothing of the keys folder is readable in the file without the key: it is in the age stream",
      b"\x01" * 32 not in blob and b"oauth-state" not in blob and b"git.example.org" not in blob)
manifest = json.loads(got["manifest.json"])
check("the manifest lists each volume's files and names the volumes it holds",
      any(f["path"] == "files/secrets/oauth-state.key" for f in manifest["files"])
      and set(manifest["parts"]["files"]) == set(vol), manifest["parts"].get("files"))
check("the README says what files/ is", b"files/<name>/" in got["README.txt"])

print("== a restore puts them back")
hr.LIVE = apps
hr.RESTORE_DIR = apps / ".host-restore"
hr.STAGED = hr.RESTORE_DIR / "apps"
hr.PREVIOUS = hr.RESTORE_DIR / "previous"
hr.PLAN = hr.RESTORE_DIR / "plan.json"
hr.MANIFEST = hr.RESTORE_DIR / "manifest"
hr.APP_DATA = hr.RESTORE_DIR / "app-data"
# Changes after the backup: each must be undone by the restore.
put(vol["home"], ".gitconfig", "[user]\n\tname = now\n")
put(vol["user-files"], "letters/b.txt", "written after\n")
(vol["workspace"] / "proj/main.py").unlink()
put(vol["secrets"], "oauth-state.key", b"\x02" * 32)
inode = vol["user-files"].joinpath("letters").stat().st_ino


def unpack(blob, verified=True):
    shutil.rmtree(hr.RESTORE_DIR, ignore_errors=True)
    for fr in hr.restorable_roots(True):
        shutil.rmtree(fr.base, ignore_errors=True)
    hr.RESTORE_DIR.mkdir(parents=True)
    try:
        return hr.unpack(io.BytesIO(blob), ident, hr.STAGED, trust_links=verified,
                         stage_app_data=False, roots=hr.restorable_roots(verified))
    except hr.RestoreError as e:
        return str(e)


out = unpack(blob)
check("unpacking stages each volume inside itself, and names the four",
      isinstance(out, dict) and sorted(r["key"] for r in out["file_roots"]) == sorted(vol)
      and (vol["home"] / ".host-restore/staged/.gitconfig").read_text() == "[user]\n\tname = then\n", out)
check("this Host's own backup keeps the workspace's Git settings",
      "git.example.org" in (vol["workspace"] / ".host-restore/staged/proj/.git/config").read_text())
hr._write_manifest()
for r in out["file_roots"]:
    fr = hr.FileRoot(r["key"], Path(r["live"]))
    hr._write_manifest(fr.staged, fr.manifest)
hr._write_plan({"state": hr.STAGED_STATE, "taken_at": "2026-11-02T00:00:00+00:00",
                "file_roots": out["file_roots"]})
dbs = {"hostmem", "hostmem_restore"}
hr._databases = lambda: set(dbs)
hr._rename_pair = lambda a, b: None
rc = hr.swap("hostmem")
check("the swap at boot succeeds", rc == 0, rc)
check("each volume's files are as they were in the backup",
      (vol["home"] / ".gitconfig").read_text() == "[user]\n\tname = then\n"
      and (vol["workspace"] / "proj/main.py").read_text() == "print('then')\n"
      and (vol["secrets"] / "oauth-state.key").read_bytes() == b"\x01" * 32
      and os.readlink(vol["user-files"] / "letters/latest") == "a.txt")
check("the keys file keeps its mode", stat.S_IMODE((vol["secrets"] / "oauth-state.key").stat().st_mode) == 0o600)
check("what came after the backup is set aside in the volume's own previous/",
      not (vol["user-files"] / "letters/b.txt").exists()
      and (vol["user-files"] / ".host-restore/previous/letters/b.txt").read_text() == "written after\n"
      and (vol["home"] / ".host-restore/previous/.gitconfig").read_text() == "[user]\n\tname = now\n")
check("what backups leave out is left where it is",
      (vol["workspace"] / "proj/node_modules/dep/index.js").exists()
      and (vol["home"] / ".cache/pip/x").exists())
check("a folder is the same folder after the swap", vol["user-files"].joinpath("letters").stat().st_ino == inode)

print("== undo puts the volumes back too")
dbs.clear()
dbs.update({"hostmem", "hostmem_before_restore"})
hr.undo("hostmem", "test")
check("each volume is as it was before the restore",
      (vol["home"] / ".gitconfig").read_text() == "[user]\n\tname = now\n"
      and (vol["user-files"] / "letters/b.txt").exists()
      and not (vol["workspace"] / "proj/main.py").exists()
      and (vol["secrets"] / "oauth-state.key").read_bytes() == b"\x02" * 32,
      (vol["home"] / ".gitconfig").read_text())
hr._drop_plan()

print("== a backup this Host cannot confirm it made")
out = unpack(blob, verified=False)
check("home and the keys folder are not staged, and the plan will not touch them",
      isinstance(out, dict) and sorted(r["key"] for r in out["file_roots"]) == ["user-files", "workspace"]
      and not (vol["home"] / ".host-restore/staged/.gitconfig").exists()
      and not (vol["secrets"] / ".host-restore/staged/oauth-state.key").exists(), out)
check("the workspace's Git folders come back with their settings reset",
      "git.example.org" not in (vol["workspace"] / ".host-restore/staged/proj/.git/config").read_text()
      and "files/workspace/proj/.git" in out["git_dirs_reset"], out.get("git_dirs_reset"))

print("== a backup from before the volumes were backed up")
old_blob, _ = backup(None)
out = unpack(old_blob)
check("it restores and names no volume, so the swap leaves all four as they are",
      isinstance(out, dict) and out["file_roots"] == []
      and not any((p / ".host-restore/staged").exists() and any((p / ".host-restore/staged").iterdir())
                  for p in vol.values()), out)

print("== a hostile file cannot reach the restore's own folder in a volume")
import tarfile  # noqa: E402
from compression import zstd  # noqa: E402
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
    ti = tarfile.TarInfo("db/hostmem.dump.00000")
    ti.size = len(DUMP)
    t.addfile(ti, io.BytesIO(DUMP))
    ti = tarfile.TarInfo("files/home/.host-restore/previous/x")
    ti.size = 1
    t.addfile(ti, io.BytesIO(b"x"))
bad = subprocess.run([hb.AGE, "-r", rec], input=zstd.compress(buf.getvalue()), capture_output=True).stdout
out = unpack(bad)
check("refused", isinstance(out, str) and "never hold" in out, out)
for fr in hr.restorable_roots(True):
    shutil.rmtree(fr.base, ignore_errors=True)
shutil.rmtree(hr.RESTORE_DIR, ignore_errors=True)

print("== a volume that is a folder of the apps tree comes back with the apps tree")
# A platform pod mounts /app/user-files from the apps volume (subPath
# app-state/user-files). A backup that holds files/user-files (one made on a
# Host with its own volume) must not stage it inside the apps tree, where the
# apps tree's swap would set the restored copy aside.
fill()
pod_uf = apps / "app-state" / "user-files"
pod_uf.mkdir(parents=True, exist_ok=True)
saved_roots = list(hb.FILE_ROOTS)
hb.FILE_ROOTS = [(k, str(pod_uf) if k == "user-files" else p) for k, p in saved_roots]
try:
    out = unpack(blob)
    staged_in_apps = (pod_uf / ".host-restore/staged").exists() and any((pod_uf / ".host-restore/staged").iterdir())
    check("it is not staged inside the apps tree, and the plan does not name it",
          isinstance(out, dict) and "user-files" not in [r["key"] for r in out["file_roots"]]
          and not staged_in_apps, (out if not isinstance(out, dict) else out["file_roots"], staged_in_apps))
finally:
    hb.FILE_ROOTS = saved_roots
    for fr in hr.restorable_roots(True):
        shutil.rmtree(fr.base, ignore_errors=True)
    shutil.rmtree(pod_uf / ".host-restore", ignore_errors=True)
    shutil.rmtree(hr.RESTORE_DIR, ignore_errors=True)

print("== what does not fit is left out whole, the rest still taken")
fill()
for i in range(200):
    put(vol["workspace"], f"many/f{i:03d}.txt", "x\n")
put(vol["workspace"], "big.bin", os.urandom(300_000))
sizes = {}
for k, p in hb.FILE_ROOTS:
    trial = hb._Listing(0)
    hb._add_apps(None, p, trial, prefix=f"files/{k}", skip=hb._root_skip(p))
    sizes[k] = (trial.size, sum(int(e.get("bytes") or 0) for e in trial))
listed = hb._Listing(1000)
hb.precount(str(apps), None, listed)
base = listed.size
saved_max = hb.MANIFEST_MAX
# The plan keeps 1/FILES_LIST_SPARE of the list free (plan_file_roots): a
# limit whose rest just holds the other three.
three = base + sum(s for k, (s, _b) in sizes.items() if k != "workspace") + 50
hb.MANIFEST_MAX = -(-three * hb.FILES_LIST_SPARE // (hb.FILES_LIST_SPARE - 1))
try:
    take, left = hb.plan_file_roots(listed, None)
finally:
    hb.MANIFEST_MAX = saved_max
check("past the list's limit, the workspace is left out and the other three are taken",
      [k for k, _ in take] == ["secrets", "home", "user-files"] and list(left) == ["workspace"]
      and "more files" in left["workspace"], (take, left))
# A volume the plan took may gain files before the write reads it again; the
# write must not then pass the limit (TooManyFiles stops the whole run, the
# database with it). A limit the four just fit in, then the workspace grows.
hb.MANIFEST_MAX = base + sum(s for s, _b in sizes.values()) + 50
try:
    listed = hb._Listing(1000)
    hb.precount(str(apps), None, listed)
    take, left = hb.plan_file_roots(listed, None)
    for i in range(20):
        put(vol["workspace"], f"grew/g{i:03d}.txt", "y\n")
    again = hb._Listing(1000)
    hb.precount(str(apps), None, again)
    try:
        for k, p in take:
            hb._add_apps(None, p, again, prefix=f"files/{k}", skip=hb._root_skip(p))
        grew_ok = True
    except hb.TooManyFiles:
        grew_ok = False
finally:
    hb.MANIFEST_MAX = saved_max
    shutil.rmtree(vol["workspace"] / "grew", ignore_errors=True)
check("a volume that gains a few files between the plan and the write does not stop the run",
      grew_ok, (take, left))
listed = hb._Listing(1000)
take, left = hb.plan_file_roots(listed, sum(b for k, (_s, b) in sizes.items() if k != "workspace") + 10)
check("past the destination's room, the workspace is left out, with its size",
      [k for k, _ in take] == ["secrets", "home", "user-files"] and "do not fit" in left.get("workspace", ""),
      (take, left))
part = hb._files_part({}, left)
parts = {"db": {"ok": True}, "apps": {"ok": True}, "gitea": {"ok": True}, "files": part}
check("the run is partial and says which volume and why",
      part["ok"] is False and part["roots"]["workspace"]["ok"] is False
      and "files: " in hb._partial_error(parts) and str(vol["workspace"]) in hb._partial_error(parts),
      hb._partial_error(parts))
listed = hb._Listing(1000)
take, left = hb.plan_file_roots(listed, None)
check("with room for all, all four are taken", [k for k, _ in take] == list(vol) and not left, (take, left))
# A platform pod mounts /app/user-files from the apps volume (subPath
# app-state/user-files): the same folder, reached by another name.
(apps / "app-state/user-files").mkdir(parents=True, exist_ok=True)
hb.FILE_ROOTS.append(("pod-user-files", str(apps / "app-state" / "user-files")))
take, left = hb.plan_file_roots(hb._Listing(0), None)
hb.FILE_ROOTS.pop()
check("a volume that is a folder of the apps tree (a pod's subPath mount) is not taken twice",
      "pod-user-files" not in dict(take) and "pod-user-files" not in left, (take, left))
hb.FILE_ROOTS.append(("absent", str(tmp / "absent")))
take, left = hb.plan_file_roots(hb._Listing(0), None)
hb.FILE_ROOTS.pop()
check("a volume an install does not have is skipped, and not called left out",
      "absent" not in dict(take) and "absent" not in left, (take, left))


print("== the room on this machine counts only the backups a run may drop")
from server import backup_stores  # noqa: E402
store_dir = tmp / "store"
store = backup_stores.LocalStore(store_dir, reserve=lambda total: 0)
store._ensure()
for stamp, n in (("20261101T010000Z", 100_000), ("20261101T020000Z", 200_000),
                 ("20261101T030000Z", 400_000)):
    (store_dir / f"{stamp}{backup_stores.DATA_SUFFIX}").write_bytes(b"\0" * n)
    store.put_sidecar(stamp, b"k")
store.protect = {"20261101T010000Z"}        # a copy a restore is reading


class _NoFree:
    f_bavail, f_frsize, f_blocks = 0, 1, 0


saved_statvfs = hb.os.statvfs
hb.os.statvfs = lambda p: _NoFree()
try:
    room = hb._files_room(store, 0)
finally:
    hb.os.statvfs = saved_statvfs
# make_room never drops the newest (400 kB) nor a protected one (100 kB):
# only the 200 kB backup can give way, so that is all the room there is.
check("a full disk's room is only the older, unprotected backups (never the newest)",
      room == 200_000, room)


async def _write_object_left_out():
    """write_object end to end: the room passed down leaves the workspace out
    and the backup still has the database, the apps tree and the rest."""
    class Pool:
        async def fetchval(self, q, *a):
            if "pg_database_size" in q:
                return len(DUMP)
            if "current_database" in q:
                return "hostmem"
            raise AssertionError(q)

    class Store:
        def __init__(self):
            self.c = Collect()

    saved = (hb._files_room, hb._store_uploader, hb.mirror_repos, hb.app_data_listing)
    small = sum(b for k, (_s, b) in sizes.items() if k != "workspace") + 10

    class Up(Collect):
        stamp = "20261102T010000Z"

        def complete(self):
            return {"key": "b/x", "size": len(self.buf), "sha256": hashlib.sha256(self.buf).hexdigest()}

        def abort(self):
            pass
    up = Up()

    async def _up(store, stamp):
        return up

    async def _no_git(workdir):
        return None, {"ok": True, "repos": 0}

    async def _no_app_data():
        return None
    hb._files_room = lambda store, base: small
    hb._store_uploader, hb.mirror_repos, hb.app_data_listing = _up, _no_git, _no_app_data
    try:
        obj, parts = await hb.write_object(Pool(), 7, kind=hb.KIND_BACKUP, recips=[rec], who=[],
                                           store=Store())
    finally:
        hb._files_room, hb._store_uploader, hb.mirror_repos, hb.app_data_listing = saved
    names = members(bytes(up.buf))
    return parts, names


parts, names = asyncio.run(_write_object_left_out())
check("write_object: the database, the apps tree and three volumes are in the file, the workspace not",
      "db/hostmem.dump.00000" in names and "apps/app-state/notes.txt" in names
      and "files/home/.gitconfig" in names and not any(n.startswith("files/workspace/") for n in names),
      sorted(n for n in names if n.startswith("files/"))[:10])
check("and its parts say the workspace was left out, the others whole",
      parts["files"]["ok"] is False and parts["files"]["roots"]["workspace"]["ok"] is False
      and parts["files"]["roots"]["home"]["ok"] is True, parts["files"])

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: backup file roots")
