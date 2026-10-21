#!/usr/bin/env python3
"""Restoring a Host (server/host_restore.py): the parts that decide.

Which backups can be restored, what a restore leaves alone, the file swap on a
real folder tree (a folder keeps its identity, because nine app-state folders
are bind-mounted into the kernel container), undo, and the swap's state machine
through a restart at every step and a kernel that never comes up. Plus the
route: user-only, behind the second factor.

Plus unpacking a real age-encrypted backup file (real `age`, a stand-in
pg_restore), the resealed key, and the routes. The databases are stubs and the
tree is a temporary folder. The whole thing against a real database inside
the kernel image is scripts/rig_host_backup.sh; the crash-safety of finishing
is scripts/test_restore_crash_safety.py.

    python3 scripts/test_host_restore.py
"""
import asyncio
import contextlib
import datetime as _dt_mod
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import host_restore as hr  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


print("== which backups can be restored")
objs = [{"name": "20260927T020700Z.tar.zst.age", "key": "d/b/20260927T020700Z.tar.zst.age", "size": 100},
        {"name": "20260927T020700Z.key.age", "key": "d/b/20260927T020700Z.key.age", "size": 1},
        {"name": "20260928T020700Z.tar.zst.age", "key": "d/b/20260928T020700Z.tar.zst.age", "size": 200},
        {"name": "junk.txt", "key": "d/b/junk.txt"}, "junk", {}]
pairs = hr.pair_objects(objs, "platform")
check("a backup is offered only with its key file next to it",
      list(pairs) == ["20260927T020700Z"] and pairs["20260927T020700Z"]["sidecar_key"].endswith(".key.age"),
      pairs)
check("its time is read from its name",
      pairs["20260927T020700Z"]["taken_at"] == "2026-09-27T02:07:00+00:00")
_listed = hr.pair_objects([{"name": "b/20260927T020700Z.tar.zst.age", "key": "d/b/20260927T020700Z.tar.zst.age",
                            "size": 100, "sha256": "c" * 64},
                           {"name": "b/20260927T020700Z.key.age", "key": "d/b/20260927T020700Z.key.age"}],
                          "platform")
check("the platform's names (b/<stamp>...) pair, and the sha256 it recorded at landing comes along",
      _listed["20260927T020700Z"]["sha256"] == "c" * 64, _listed)
check("a second copy's names (run and stamp in front) pair the same way",
      list(hr.pair_objects([{"key": "pre/20260927T020800Z-12-20260927T020700Z.tar.zst.age"},
                            {"key": "pre/20260927T020800Z-12-20260927T020700Z.key.age"}], "s3"))
      == ["20260927T020700Z"])

print("== what a restore leaves where it is (what backups leave out)")
for rel, kept in (("proj/node_modules/x/index.js", True), ("a/b/.venv/bin/python", True),
                  ("proj/.cache/c", True), ("proj/core.1234", True), ("proj/lib/vocabularies/core", False),
                  ("crash/core.1", True), ("host-sdk/scratch/t", True), ("host-sdk/tools/t", False),
                  (".host-restore/plan.json", True), ("app-state/user-files/notes.txt", False),
                  ("crashes/x", False)):
    check(f"{rel}: {'left alone' if kept else 'restored'}", hr._kept(rel) is kept)


# ── the file swap, on a real tree ────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp(prefix="hr-test-"))
live = tmp / "live"
hr.LIVE = live
hr.RESTORE_DIR = live / ".host-restore"
hr.STAGED = hr.RESTORE_DIR / "apps"
hr.PREVIOUS = hr.RESTORE_DIR / "previous"
hr.PLAN = hr.RESTORE_DIR / "plan.json"
hr.MANIFEST = hr.RESTORE_DIR / "manifest"


def put(base, rel, text):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def build():
    import shutil
    shutil.rmtree(live, ignore_errors=True)
    put(live, "app-state/user-files/notes.txt", "now\n")
    put(live, "app-state/user-files/later.txt", "made later\n")
    put(live, "proj/node_modules/y/index.js", "installed later\n")
    put(live, "crash/core.1", "dump\n")
    put(live, "gone-dir/f", "a file where the backup has a folder\n")
    (live / "link").symlink_to("app-state")
    put(hr.STAGED, "app-state/user-files/notes.txt", "then\n")
    put(hr.STAGED, "proj/deleted-since.txt", "back\n")
    put(hr.STAGED, "proj/newdir/file", "new folder\n")
    put(hr.STAGED, "gone-dir", "a file in the backup\n")
    hr._write_manifest()


print("== the file swap")
build()
inode = (live / "app-state/user-files").stat().st_ino
files = hr._manifest_files()
check("the manifest lists what the snapshot holds",
      files == {"app-state/user-files/notes.txt", "proj/deleted-since.txt", "proj/newdir/file", "gone-dir"},
      files)
aside = hr.set_aside(live, files)
check("live files the backup does not hold are set aside",
      not (live / "app-state/user-files/later.txt").exists()
      and (hr.PREVIOUS / "app-state/user-files/later.txt").read_text() == "made later\n", aside)
check("a link is set aside as a link, not followed",
      (hr.PREVIOUS / "link").is_symlink() and (live / "app-state").is_dir())
check("what backups leave out is not touched",
      (live / "proj/node_modules/y/index.js").exists() and (live / "crash/core.1").exists())
moved = hr.move_in(live)
check("restored files are in place", (live / "app-state/user-files/notes.txt").read_text() == "then\n"
      and (live / "proj/deleted-since.txt").read_text() == "back\n"
      and (live / "proj/newdir/file").read_text() == "new folder\n", moved)
check("the file each one replaced is kept", (hr.PREVIOUS / "app-state/user-files/notes.txt").read_text() == "now\n")
check("a folder where the backup has a file is set aside whole",
      (live / "gone-dir").read_text() == "a file in the backup\n"
      and (hr.PREVIOUS / "gone-dir/f").exists())
check("a folder is the same folder after the swap (bind mounts keep working)",
      (live / "app-state/user-files").stat().st_ino == inode)
check("run again after a restart, both steps change nothing",
      hr.set_aside(live, files) == 0 and hr.move_in(live) == 0)

print("== undo")
hr._write_plan({"state": hr.SWAPPED, "live_db": "hostmem"})
renames = []
hr._databases = lambda: {"hostmem", "hostmem_before_restore"}
hr._rename_pair = lambda a, b: renames.append((a, b))
hr.undo("hostmem", "test")
check("undo puts every file back",
      (live / "app-state/user-files/notes.txt").read_text() == "now\n"
      and (live / "app-state/user-files/later.txt").exists()
      and not (live / "proj/deleted-since.txt").exists()
      and not (live / "proj/newdir/file").exists() and (live / "link").is_symlink(), sorted(os.listdir(live)))
check("the restored files go back to the staged folder",
      (hr.STAGED / "proj/deleted-since.txt").read_text() == "back\n")
check("and the plan says the restore failed, and why",
      hr.read_plan()["state"] == hr.FAILED and hr.read_plan()["error"] == "test")
check("and the previous database takes its name back",
      renames == [(("hostmem", "hostmem_restore"), ("hostmem_before_restore", "hostmem"))], renames)


# ── the swap's state machine ────────────────────────────────────────────────
print("== the swap at boot")
dbs = {"hostmem", "hostmem_restore"}


def rename(a, b):
    renames.append((a, b))
    for old, new in (a, b):
        dbs.discard(old)
        dbs.add(new)


hr._databases = lambda: set(dbs)
hr._rename_pair = rename
build()
renames.clear()
hr._write_plan({"state": hr.STAGED_STATE, "taken_at": "x", "boots": 0})
check("an unknown database name is refused", hr.swap("postgres") == 1)
rc = hr.swap("hostmem")
check("staged: databases trade names in one step, then the files move",
      rc == 0 and renames == [(("hostmem", "hostmem_before_restore"), ("hostmem_restore", "hostmem"))]
      and hr.read_plan()["state"] == hr.SWAPPED and hr.read_plan()["boots"] == 1
      and (live / "app-state/user-files/notes.txt").read_text() == "then\n", (rc, renames, hr.read_plan()))
check("a boot that finds it swapped but not finished counts, and arms the undo again",
      hr.swap("hostmem") == 0 and hr.read_plan()["boots"] == 2)
rc = hr.swap("hostmem")
check("the third such boot puts everything back",
      rc == 1 and hr.read_plan()["state"] == hr.FAILED
      and renames[-1] == (("hostmem", "hostmem_restore"), ("hostmem_before_restore", "hostmem"))
      and (live / "app-state/user-files/notes.txt").read_text() == "now\n", (rc, renames[-1:]))
check("a failed restore is not swapped again", hr.swap("hostmem") == 3)

build()
dbs.clear()
dbs.update({"hostmem", "hostmem_restore"})
renames.clear()
hr._write_plan({"state": hr.DB_DONE, "live_db": "hostmem", "taken_at": "x"})
rc = hr.swap("hostmem")
check("restarted after the database swap: it goes on with the files and renames nothing again",
      rc == 0 and renames == [] and hr.read_plan()["state"] == hr.SWAPPED
      and (live / "proj/deleted-since.txt").exists(), (rc, renames))

build()
dbs.clear()
dbs.update({"hostmem", "hostmem_restore", "hostmem_before_restore"})
renames.clear()
hr._write_plan({"state": hr.STAGED_STATE, "taken_at": "x"})
rc = hr.swap("hostmem")
check("an earlier restore's leftover database stops the swap before anything moves",
      rc == 1 and renames == [] and hr.read_plan()["state"] == hr.FAILED
      and "before_restore" in hr.read_plan()["error"]
      and (live / "app-state/user-files/notes.txt").read_text() == "now\n", hr.read_plan())

build()
dbs.clear()
dbs.update({"hostmem", "hostmem_restore"})
renames.clear()
hr._write_plan({"state": hr.STAGED_STATE, "taken_at": "x"})
real_move_in = hr.move_in


def broken_move_in(root):
    raise OSError("disk trouble")


hr.move_in = broken_move_in
rc = hr.swap("hostmem")
hr.move_in = real_move_in
check("a swap that fails after the databases moved is undone at once",
      rc == 1 and hr.read_plan()["state"] == hr.FAILED and "disk trouble" in hr.read_plan()["error"]
      and renames[-1] == (("hostmem", "hostmem_restore"), ("hostmem_before_restore", "hostmem"))
      and (live / "app-state/user-files/later.txt").exists(), (rc, hr.read_plan()))
hr.PLAN.unlink()
check("no plan: nothing to swap", hr.swap("hostmem") == 3)


# ── unpacking a backup file ─────────────────────────────────────────────────
print("== unpacking a backup file")
# The room a restore sees is this test's own, never the machine's free disk:
# a fixed 1 TiB, with no disk size (so no eviction share is kept). The room
# checks below set their own.
_machine_room = hr._room
hr._room = lambda path: hr.Room(1 << 40, 4096, 0, 0, 0)
import hashlib  # noqa: E402
import io  # noqa: E402
import shutil as _sh  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402

from server import age_keys  # noqa: E402
from server import host_backup as hb  # noqa: E402

if not os.path.isfile(hb.AGE):
    hb.AGE = _sh.which("age") or hb.AGE
ident, rec = age_keys.new_pair()
DUMP = b"PGDMP" + os.urandom(700)
bin_dir = tmp / "bin"
bin_dir.mkdir()
restored_dump = tmp / "restored.dump"
# The restore loads in passes (host_restore._load_database): the dump file is
# an argument; `-l` lists one kernel table's rows (and nothing of the Host's own
# schema, so its pass is skipped); psql names that table in the schema the
# restore built. The schema build itself, and what the dump may run, are
# test_restore_untrusted_dump_pg's, on a real database. The fake keeps the
# bytes of the pass that loads the rows.
(bin_dir / "pg_restore").write_text(
    '#!/bin/sh\nlast=""; list=""; world=""\n'
    'for a in "$@"; do last="$a"; [ "$a" = "-l" ] && list=1; [ "$a" = "--schema" ] && world=1; done\n'
    'if [ -n "$list" ]; then [ -z "$world" ] && echo "6001; 0 16800 TABLE DATA public t1 entity"; exit 0; fi\n'
    f'if [ -f "$last" ]; then cat "$last" > "{restored_dump}"; else cat > "{restored_dump}"; fi\n'
    'if [ -n "$FAKE_RESTORE_FAIL" ]; then echo "pg_restore: bad" >&2; exit 1; fi\n')
(bin_dir / "pg_restore").chmod(0o755)
# The load's role is the boot's (migrations/v242_host_restore_role.sql): the
# stand-in database has it, unless FAKE_NO_ROLE says the boot never made it.
(bin_dir / "psql").write_text('#!/bin/sh\ncase "$*" in *"relkind::text"*) echo "r t1";;\n'
                              '  *"rolname = \'host_restore\'"*) [ -z "$FAKE_NO_ROLE" ] && echo 1;; esac\nexit 0\n')
(bin_dir / "psql").chmod(0o755)
os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
hr._build_own_schema = lambda: None


def backup_file(members, *, manifest=True, recipient=rec, db_sha=None, chunks=None, unlisted=(),
                listed_links=None):
    """A backup file laid out the way host_backup.write_file lays it out.
    `unlisted`: names left out of the manifest; `listed_links`: the manifest's
    links, in place of the ones in the file."""
    from compression import zstd
    buf = io.BytesIO()
    files = []
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for name, data, kind in members:
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type, ti.mode = tarfile.DIRTYPE, (data if isinstance(data, int) else 0o755)
                t.addfile(ti)
            elif kind == "hardlink":
                ti.type, ti.linkname = tarfile.LNKTYPE, data
                t.addfile(ti)
            elif kind == "fifo":
                ti.type = tarfile.FIFOTYPE
                t.addfile(ti)
            elif kind == "link":
                ti.type, ti.linkname = tarfile.SYMTYPE, data
                t.addfile(ti)
                if name.startswith("apps/"):
                    files.append({"path": name, "link": data})
            else:
                ti.size, ti.mode = len(data), 0o644
                t.addfile(ti, io.BytesIO(data))
                if name.startswith("apps/"):
                    files.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        files = [f for f in files if f["path"] not in unlisted]
        if listed_links is not None:
            files = [f for f in files if "link" not in f] + [{"path": p, "link": v} for p, v in listed_links]
        if manifest:
            m = json.dumps({"format": "privatae-backup/1", "entity_id": 17, "parts": {"db": {
                "chunks": chunks if chunks is not None else sum(1 for n, _, _ in members if n.startswith("db/")),
                "sha256": db_sha or hashlib.sha256(DUMP).hexdigest()}}, "files": files}).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(m)
            t.addfile(ti, io.BytesIO(m))
    return subprocess.run([hb.AGE, "-r", recipient], input=zstd.compress(buf.getvalue()),
                          capture_output=True).stdout


GOOD = [("db/hostmem.dump.00000", DUMP[:300], "file"), ("db/hostmem.dump.00001", DUMP[300:], "file"),
        ("apps/app-state", None, "dir"), ("apps/app-state/user-files", None, "dir"),
        ("apps/app-state/user-files/notes.txt", b"then\n", "file"),
        ("apps/link", "app-state", "link"), ("gitea-repos/x.git/HEAD", b"ref\n", "file")]


def unpack(blob, identity=ident, **kw):
    dest = tmp / "unpacked"
    _sh.rmtree(dest, ignore_errors=True)
    if restored_dump.exists():
        restored_dump.unlink()
    try:
        return hr.unpack(io.BytesIO(blob), identity, dest, **kw), dest
    except hr.RestoreError as e:
        return str(e), dest


out, dest = unpack(backup_file(GOOD))
check("the database pieces go, joined, into pg_restore", restored_dump.read_bytes() == DUMP, out)
check("unpacking says which Host the backup is from", isinstance(out, dict) and out.get("entity_id") == 17, out)
check("THE RULE: the backup's key is nowhere in what the restore wrote",
      not any(b"AGE-SECRET-KEY" in p.read_bytes().upper() for p in dest.rglob("*") if p.is_file()
              and not p.is_symlink()))
check("the files land in the staged tree, links as links, Git mirrors left out",
      (dest / "app-state/user-files/notes.txt").read_text() == "then\n"
      and os.readlink(dest / "link") == "app-state" and not (dest / "x.git").exists()
      and not any(p.name == "gitea-repos" for p in dest.iterdir()), out)
out, _ = unpack(backup_file(GOOD), identity=age_keys.new_pair()[0])
check("the wrong key cannot open it, and says so", isinstance(out, str) and "decrypt" in out, out)
out, _ = unpack(backup_file(GOOD, manifest=False))
check("a file cut short (no manifest) is refused", isinstance(out, str) and "manifest" in out, out)
out, _ = unpack(backup_file(GOOD, db_sha="0" * 64))
check("a database that does not match the manifest is refused",
      isinstance(out, str) and "does not match" in out, out)
out, _ = unpack(backup_file([GOOD[1], GOOD[0]] + GOOD[2:]))
check("database pieces out of order are refused", isinstance(out, str) and "out of order" in out, out)
out, _ = unpack(backup_file(GOOD[:2] + [("apps/../escape.txt", b"x", "file")]))
check("a path that climbs out of the tree is refused", isinstance(out, str) and "unsafe" in out, out)
out, _ = unpack(backup_file(GOOD[:2] + [("apps/out", str(tmp), "link"), ("apps/out/planted.txt", b"x", "file")]))
check("a file written through a link it just made is refused, nothing lands outside",
      isinstance(out, str) and "link" in out and not (tmp / "planted.txt").exists(), out)
out, _ = unpack(backup_file([m for m in GOOD if not m[0].startswith("db/")], chunks=0))
check("a backup with no database is refused", isinstance(out, str) and "no database" in out, out)
# A tree with a hard link, written by the Host's own writer (git clone --local
# and pnpm make these), must restore: every name comes back with its content.
hl = tmp / "hl-apps"
(hl / "proj/.git/objects").mkdir(parents=True)
(hl / "proj/a.txt").write_text("hello\n")
os.link(hl / "proj/a.txt", hl / "proj/.git/objects/b.txt")
from compression import zstd as _zstd  # noqa: E402
_buf = io.BytesIO()
_t = tarfile.open(fileobj=_buf, mode="w|", format=tarfile.PAX_FORMAT)
for i, piece in enumerate((DUMP[:300], DUMP[300:])):
    _ti = tarfile.TarInfo(f"db/hostmem.dump.{i:05d}")
    _ti.size = len(piece)
    _t.addfile(_ti, io.BytesIO(piece))
_files = []
hb._add_apps(_t, str(hl), _files)
_m = json.dumps({"format": hb.FORMAT, "parts": {"db": {"chunks": 2, "sha256": hashlib.sha256(DUMP).hexdigest()}},
                 "files": _files}).encode()
_ti = tarfile.TarInfo("manifest.json")
_ti.size = len(_m)
_t.addfile(_ti, io.BytesIO(_m))
_t.close()
_enc = subprocess.run([hb.AGE, "-r", rec], input=_zstd.compress(_buf.getvalue()), capture_output=True).stdout
out, dest = unpack(_enc)
check("a hard-linked file restores under both its names, with its content",
      isinstance(out, dict) and (dest / "proj/a.txt").read_text() == "hello\n"
      and (dest / "proj/.git/objects/b.txt").read_text() == "hello\n", out)
os.environ["FAKE_RESTORE_FAIL"] = "1"
out, _ = unpack(backup_file(GOOD))
os.environ.pop("FAKE_RESTORE_FAIL")
check("a database that will not load is refused, with pg_restore's words",
      isinstance(out, str) and "pg_restore: bad" in out, out)
os.environ["FAKE_NO_ROLE"] = "1"
restored_dump.unlink(missing_ok=True)
out, _ = unpack(backup_file(GOOD))
os.environ.pop("FAKE_NO_ROLE")
check("a database whose boot never made the load's role is refused, with the reason, and loads nothing",
      isinstance(out, str) and "no host_restore role" in out and not restored_dump.exists(), out)

# ── a hostile backup file ───────────────────────────────────────────────────
# Anyone who can write where backups are kept can make a file that decrypts
# (age encrypts to public keys). None of these may act outside the staged
# folder, load a database, or make a link before the whole file is checked.
print("== a hostile backup file")
DB = GOOD[:2]
spool = tmp / hr.SPOOL_NAME          # unpack spools beside the staged folder


def refused(out, word=""):
    return isinstance(out, str) and word in out and not restored_dump.exists() and not spool.exists()


out, dest = unpack(backup_file(GOOD, db_sha="0" * 64))
check("nothing is loaded and no link is made before the whole file is checked",
      refused(out, "does not match") and not os.path.lexists(dest / "link"), out)
out, _ = unpack(backup_file(GOOD + [("apps/extra.txt", b"x", "file")], unlisted=("apps/extra.txt",)))
check("a file the manifest does not list is refused (the manifest must match both ways)",
      refused(out, "extra.txt"), out)
out, _ = unpack(backup_file(GOOD[:4] + [("apps/app-state/user-files/notes.txt", b"again\n", "file")] + GOOD[4:]))
check("a file named twice is refused", refused(out, "twice"), out)
out, _ = unpack(backup_file(GOOD, listed_links=[("apps/link", "elsewhere")]))
check("a link whose target is not the manifest's is refused", refused(out, "link"), out)
for bad in ("apps//tmp/planted.txt", "apps/a/../../planted.txt", "apps/./x", "apps/"):
    out, _ = unpack(backup_file(DB + [(bad, b"x", "file")]))
    check(f"an unsafe name is refused: {bad!r}", refused(out, "unsafe") and not (tmp / "planted.txt").exists()
          and not Path("/tmp/planted.txt").exists(), out)
victim = tmp / "victim"
victim.mkdir(mode=0o700)
os.chmod(victim, 0o700)
out, _ = unpack(backup_file(DB + [("apps/a", str(victim), "link"), ("apps/a", 0o777, "dir")]))
check("a link and then a folder of the same name: refused, and nothing is changed through the link",
      refused(out, "twice") and stat.S_IMODE(os.stat(victim).st_mode) == 0o700,
      (out, oct(stat.S_IMODE(os.stat(victim).st_mode))))
out, _ = unpack(backup_file(DB + [("apps/a", str(victim), "link"), ("apps/a", b"x", "file")]))
check("  nor a file written through it", refused(out, "twice") and not any(victim.iterdir()), out)
out, _ = unpack(backup_file(DB + [("apps/f", b"x", "file"), ("apps/f/g", b"y", "file")]))
check("something below a file is refused", refused(out, "below"), out)
out, _ = unpack(backup_file(DB + [("apps/app-state/user-files/notes.txt", b"n", "file"),
                                  ("apps/hl", "apps/app-state/user-files/notes.txt", "hardlink")]))
check("a hard link is refused (the writer stores every name whole)", refused(out, "hard link"), out)
out, _ = unpack(backup_file(DB + [("apps/pipe", None, "fifo")]))
check("a pipe or a device is refused", refused(out, "not a file"), out)
# The room cases from here to the single-disk ones below are a Host in the
# cluster, on a node's disk: the kubelet's eviction threshold is what it keeps
# (host_restore._reserve, in_cluster). The kubelet sets this in every pod.
_K8S_WAS = os.environ.get("KUBERNETES_SERVICE_HOST")
os.environ["KUBERNETES_SERVICE_HOST"] = "10.96.0.1"
# The Host's room is this test's own number, not what the machine running it
# has free. Free space on a shared filesystem moves between two readings (other
# gates write and delete there): a margin set from one reading handed the Host
# whatever the rest of the machine freed before unpack took the next, and the
# 8000-byte file then fit (measured: 10 of 30 concurrent runs, each
# one where free space grew by 8 KiB or more between the two readings).
_real_room, _real_alloc_unit = hr._room, hr._alloc_unit     # the fixed room above
U = 4096


def restore_in(members, units, unit=U, inodes=0, inodes_free=0, total=0):
    """Unpack DB + members into a Host with `units` x U bytes of room past the
    margin, allocating in `unit`s (0 inodes: a filesystem that keeps no count;
    total 0: no disk size, so no eviction share is kept)."""
    hr._room = lambda path: hr.Room(hr.SPACE_MARGIN + units * U, unit, inodes_free, inodes, total)
    hr._alloc_unit = lambda folder, reported: reported        # the unit this case says
    try:
        return unpack(backup_file(DB + members))
    finally:
        hr._room, hr._alloc_unit = _real_room, _real_alloc_unit


# Three units: one for the database spool, one for a small file, and room left
# for the database's bytes and the names, but not a file of two units.
out, dest = restore_in([("apps/small.bin", os.urandom(1000), "file")], 3)
check("a file that fits in the room the Host has is written",
      isinstance(out, dict) and (dest / "small.bin").stat().st_size == 1000, out)
out, dest = restore_in([("apps/big.bin", os.urandom(8000), "file")], 3)
check("a file larger than the Host has room for is refused before it is written",
      refused(out, "room for (at apps/big.bin)") and not (dest / "big.bin").exists(), out)
# What an entry takes is not its size. Measured on ext4 (the apps volume's
# filesystem): 20,000 files of 10 bytes took 82 MB, and 20,000 empty folders
# 82 MB. Forty files of 10 bytes are 400 bytes and forty units.
tiny = [(f"apps/t{i:02d}", b"0123456789", "file") for i in range(40)]
out, dest = restore_in(tiny, 16)
check("many tiny files that fit by bytes but not by the units they take are refused, before the rest is written",
      refused(out, "room for (at apps/t") and len(list(dest.iterdir())) < 40, out)
out, dest = restore_in(tiny, 48)
check("  the same files restore where their units fit", isinstance(out, dict) and len(list(dest.iterdir())) == 40, out)
out, _ = restore_in(tiny, 48, unit=16 * U)
check("  the unit is the filesystem's: where it allocates 64 KiB, they do not fit there",
      refused(out, "room for (at apps/t"), out)
out, _ = restore_in([(f"apps/d{i:02d}", None, "dir") for i in range(40)], 16)
check("forty empty folders, nothing by bytes, are refused where their units do not fit", refused(out, "room for"), out)
out, _ = restore_in([(f"apps/p{i:02d}/x", b"", "file") for i in range(40)], 16)
check("  and so are the folders a path only implies", refused(out, "room for"), out)
names = [(f"apps/e{i:03d}", b"", "file") for i in range(500)]
out, _ = restore_in(names, 2)
check("500 empty files are refused where their names do not fit in their folder (ext4 took 25 bytes each)",
      refused(out, "room for (at apps/e"), out)
out, dest = restore_in(names, 5)
check("  and restore where they do", isinstance(out, dict) and len(list(dest.iterdir())) == 500, out)
many = [(f"apps/i{i:02d}", b"", "file") for i in range(60)]
out, _ = restore_in(many, 256, inodes=1000, inodes_free=100)      # 50 past the 5% kept free
check("more files than the filesystem has inodes to spare, past the 5% kept free, are refused",
      refused(out, "more files than this Host has room for"), out)
out, dest = restore_in(many, 256, inodes=1000, inodes_free=120)
check("  and restore where it has them", isinstance(out, dict) and len(list(dest.iterdir())) == 60, out)

# A network filesystem's f_frsize is not what a file takes: NFS reports its
# transfer size and CephFS its object size, up to 4 MiB. The unit is measured
# with a probe file instead. statvfs is faked here to report 4 MiB on this
# (local) disk: 4 MiB of room past the margin holds the forty tiny files by
# what they really take, and would hold one of them by f_frsize.
import types as _types  # noqa: E402
_real_statvfs = hr.os.statvfs
MiB4 = 4 << 20
hr.os.statvfs = lambda path: _types.SimpleNamespace(
    f_bavail=hr.SPACE_MARGIN // MiB4 + 1, f_frsize=MiB4, f_bsize=MiB4, f_favail=0, f_files=0, f_blocks=0)
hr._room = _machine_room                 # the real reading, of the faked statvfs
try:
    out, dest = unpack(backup_file(DB + tiny))
finally:
    hr.os.statvfs, hr._room = _real_statvfs, _real_room
check("on a filesystem that reports a 4 MiB unit, the unit is measured, and tiny files restore by what they take",
      isinstance(out, dict) and len(list(dest.iterdir())) == 40, out)
_probe_dir = tmp / "probe"
_probe_dir.mkdir()
check("  the measured unit is a local disk's (one small file, 4 KiB here), and the probe is gone",
      hr._alloc_unit(_probe_dir, MiB4) == os.stat(tmp).st_blksize and not any(_probe_dir.iterdir()))
check("  with no probe to be made, the smaller of f_frsize and 4096",
      hr._alloc_unit(tmp / "no-such-folder", MiB4) == 4096 and hr._alloc_unit(tmp / "no-such-folder", 512) == 512)

# What stays free is the node's eviction threshold too, not only the margin:
# the apps volume is the node's own disk, and below the kubelet's threshold it
# evicts pods, other Hosts' included. On a 10 GiB disk 15% is 1.5 GiB.
G = 1 << 30
roomy = 10 * G
def restore_on(free, share=None):
    old = os.environ.pop("CSERIES_NODE_EVICTION_FREE", None)
    if share is not None:
        os.environ["CSERIES_NODE_EVICTION_FREE"] = share
    hr._room = lambda path: hr.Room(free, U, 0, 0, roomy)
    hr._alloc_unit = lambda folder, reported: reported
    try:
        return unpack(backup_file(DB + [("apps/small.bin", b"x" * 1000, "file")]))
    finally:
        hr._room, hr._alloc_unit = _real_room, _real_alloc_unit
        os.environ.pop("CSERIES_NODE_EVICTION_FREE", None)
        if old is not None:
            os.environ["CSERIES_NODE_EVICTION_FREE"] = old
out, _ = restore_on(int(1.4 * G))
check("a restore that would leave the disk under 15% free is refused, though over the 256 MiB margin",
      refused(out, "room for"), out)
out, _ = restore_on(int(1.6 * G))
check("  and goes ahead with 15% left", isinstance(out, dict), out)
out, _ = restore_on(int(1.4 * G), share="0.05")
check("  the share is the node's when the operator gives it (CSERIES_NODE_EVICTION_FREE)", isinstance(out, dict), out)
for bad in ("15", "-0.1", "1", "nan", "inf", "15%", ""):
    out, _ = restore_on(int(1.6 * G), share=bad)
    check(f"  a share of {bad!r} is refused by name, not read as the default", refused(out, "CSERIES_NODE_EVICTION_FREE"), out)

# ON ONE MACHINE (no kubelet: a C-Series machine runs the Host under podman)
# nothing is evicted, and 15% of a disk kept empty refuses a restore that
# fits: the reserve is the larger of 2 GiB (this image's PostgreSQL may hold
# max_wal_size 256MB plus max_slot_wal_keep_size 1024MB of write-ahead log)
# and 5% (ext4's own reserve on a new filesystem).
os.environ.pop("KUBERNETES_SERVICE_HOST", None)
check("off the cluster the Host knows it runs on one machine", hr.in_cluster() is False)
roomy = 100 * G
out, _ = restore_on(6 * G)
check("on one machine's 100 GiB disk, a restore that leaves 6 GiB free goes ahead (15% would keep 15 GiB)",
      isinstance(out, dict), out)
check("  the reserve there is 5% of the disk", hr._reserve(hr.Room(6 * G, U, 0, 0, roomy))[0] == 5 * G
      and "one machine" in hr._reserve(hr.Room(6 * G, U, 0, 0, roomy))[2])
out, _ = restore_on(int(4.9 * G))
check("  and one that would leave under 5% free is refused", refused(out, "room for"), out)
roomy = 10 * G
out, _ = restore_on(int(1.9 * G))
check("on a small disk (10 GiB) the reserve is never under 2 GiB", refused(out, "room for"), out)
out, _ = restore_on(int(2.1 * G))
check("  and a restore past it goes ahead", isinstance(out, dict), out)
out, _ = restore_on(4 * G, share="0.5")
check("  the operator's share still applies on one machine (CSERIES_NODE_EVICTION_FREE: half of 10 GiB)",
      refused(out, "room for"), out)
os.environ["KUBERNETES_SERVICE_HOST"] = "10.96.0.1"
roomy = 100 * G
out, _ = restore_on(6 * G)
check("in the cluster the same restore on a 100 GiB node disk is still refused: 15% stays free for the kubelet",
      refused(out, "room for"), out)
if _K8S_WAS is None:
    os.environ.pop("KUBERNETES_SERVICE_HOST", None)
else:
    os.environ["KUBERNETES_SERVICE_HOST"] = _K8S_WAS
out, _ = unpack(backup_file(GOOD), expect={"sha256": "0" * 64, "bytes": 1})
check("an encrypted file that is not the one its key file names is refused, and nothing is loaded",
      refused(out, "not the one its key file was made for"), out)
good_blob = backup_file(GOOD)
out, _ = unpack(good_blob, expect={"sha256": hashlib.sha256(good_blob).hexdigest(), "bytes": len(good_blob)})
check("the one it names restores", isinstance(out, dict) and restored_dump.read_bytes() == DUMP
      and not spool.exists(), out)

print("== what is read before the hash check is bounded")
import time  # noqa: E402


def seal_raw(tar_bytes):
    from compression import zstd
    return subprocess.run([hb.AGE, "-r", rec], input=zstd.compress(tar_bytes), capture_output=True).stdout


def tar_of(members, fmt=tarfile.PAX_FORMAT):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as t:
        for ti, data in members:
            t.addfile(ti, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def ti_file(name, data):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    return ti, data


check("the manifest limit is the writer's, well under the 256 MiB it was",
      hr.MANIFEST_MAX == hb.MANIFEST_MAX <= 64 * 1024 * 1024)
_mm = hr.MANIFEST_MAX
hr.MANIFEST_MAX = 100
out, _ = unpack(backup_file(GOOD))
hr.MANIFEST_MAX = _mm
check("a manifest over the limit is refused before it is read", refused(out, "manifest"), out)
long_name = "apps/" + "n" * (hr.TAR_HEADER_MAX + 10)
out, _ = unpack(seal_raw(tar_of([ti_file("db/hostmem.dump.00000", DUMP), ti_file(long_name, b"x")])))
check("a PAX header past the limit is refused before it is read", refused(out, "tar header"), out)
out, _ = unpack(seal_raw(tar_of([ti_file("db/hostmem.dump.00000", DUMP), ti_file(long_name, b"x")],
                                fmt=tarfile.GNU_FORMAT)))
check("so is a GNU long name", refused(out, "tar header"), out)
sp = tarfile.TarInfo("apps/sparse")
sp.type, sp.size = tarfile.GNUTYPE_SPARSE, 0
out, _ = unpack(seal_raw(tar_of([ti_file("db/hostmem.dump.00000", DUMP), (sp, None)], fmt=tarfile.GNU_FORMAT)))
check("a sparse file is refused before its map is read", refused(out, "sparse"), out)
_fl, _ra = hr.INFLATE_FLOOR, hr.INFLATE_RATIO
hr.INFLATE_FLOOR, hr.INFLATE_RATIO = 1 << 20, 2
out, _ = unpack(backup_file(GOOD + [("gitea-repos/x.git/zeros", bytes(8 << 20), "file")]))
hr.INFLATE_FLOOR, hr.INFLATE_RATIO = _fl, _ra
check("a file that inflates far past its size is stopped, even in a part the restore skips",
      refused(out, "unpacks to more than"), out)
out, _ = unpack(backup_file(GOOD + [("gitea-repos/x.git/zeros", bytes(8 << 20), "file")]))
check("  and with the real limits a large run of zeros in a small backup restores", isinstance(out, dict), out)
# The same good backup, with 3 MiB after the tar's end.
_buf = io.BytesIO()
with tarfile.open(fileobj=_buf, mode="w", format=tarfile.PAX_FORMAT) as _t:
    for name, data, kind in GOOD:
        if kind == "file":
            _ti, _d0 = ti_file(name, data)
            _t.addfile(_ti, io.BytesIO(_d0))
        elif kind == "dir":
            _d = tarfile.TarInfo(name)
            _d.type = tarfile.DIRTYPE
            _t.addfile(_d)
        elif kind == "link":
            _l = tarfile.TarInfo(name)
            _l.type, _l.linkname = tarfile.SYMTYPE, data
            _t.addfile(_l)
    _files = [{"path": n, "sha256": hashlib.sha256(d).hexdigest()} for n, d, k in GOOD
              if k == "file" and n.startswith("apps/")] + [{"path": n, "link": d} for n, d, k in GOOD
                                                            if k == "link"]
    _ti, _d0 = ti_file("manifest.json", json.dumps({"parts": {"db": {
        "chunks": 2, "sha256": hashlib.sha256(DUMP).hexdigest()}}, "files": _files}).encode())
    _t.addfile(_ti, io.BytesIO(_d0))
out, _ = unpack(seal_raw(_buf.getvalue()))
check("  (the same backup built by hand restores)", isinstance(out, dict), out)
out, _ = unpack(seal_raw(_buf.getvalue() + bytes(3 << 20)))
check("more after the tar's end than a backup leaves there is refused", refused(out, "after its end"), out)


class Slow:
    """A download that trickles."""

    def __init__(self, data, every=0.2, chunk=4096):
        self.data, self.every, self.chunk = data, every, chunk

    def read(self, n=-1):
        time.sleep(self.every)
        b, self.data = self.data[:self.chunk], self.data[self.chunk:]
        return b

    def close(self):
        pass


_ul = hr.UNPACK_LIMIT_S
hr.UNPACK_LIMIT_S = 1
slow = backup_file(GOOD + [("apps/big.bin", os.urandom(80_000), "file")])
_sh.rmtree(tmp / "unpacked", ignore_errors=True)
t_start = time.monotonic()
try:
    out = hr.unpack(Slow(slow), ident, tmp / "unpacked")
except hr.RestoreError as e:
    out = str(e)
took = time.monotonic() - t_start
hr.UNPACK_LIMIT_S = _ul
check(f"an unpack that runs past its time limit is stopped, saying so ({took:.1f} s for a 1 s limit)",
      refused(out, "took longer") and took < 10, out)
seen_lock = []


async def _stage_sees_lock(pool, eid, stamp, unverified_ok=False, where=None):
    seen_lock.append(hb._run_lock.locked())
    return {"state": "staged"}
_real_stage = hr._stage
hr._stage = _stage_sees_lock
asyncio.run(hr.stage(None, 42, "20260927T020000Z"))
hr._stage = _real_stage
check("all of it runs holding the backup lock", seen_lock == [True], seen_lock)

print("== links that lead out of the restored tree")
live_abs = str(hr.LIVE)
LINKY = DB + [("apps/deep", None, "dir"), ("apps/deep/deeper", None, "dir"),
              ("apps/deep/deeper/in", "../sibling", "link"),               # inside
              ("apps/deep/deeper/X", "../..", "link"),                     # the staged root: inside
              ("apps/deep/deeper/Y", "X/../..", "link"),                   # through X: outside
              ("apps/up", "../../etc/passwd", "link"),                     # outside
              ("apps/abs-out", "/tmp/host-restore-keys", "link"),          # outside
              ("apps/abs-in", live_abs + "/deep/deeper", "link"),          # the apps root: inside
              ("apps/abs-sneak", live_abs + "-x/secret", "link")]          # <root>-x is not <root>
out, dest = unpack(backup_file(LINKY))
check("a backup with links that lead out still restores",
      isinstance(out, dict) and restored_dump.read_bytes() == DUMP, out)
check("  without them, and says which",
      isinstance(out, dict) and out["links_refused"] == ["abs-out", "abs-sneak", "deep/deeper/Y", "up"]
      and not any(os.path.lexists(dest / p) for p in ("up", "abs-out", "abs-sneak", "deep/deeper/Y")),
      out)
check("  and links inside the tree, or to the apps root, are made as they were",
      os.readlink(dest / "deep/deeper/in") == "../sibling" and os.readlink(dest / "deep/deeper/X") == "../.."
      and os.readlink(dest / "abs-in") == live_abs + "/deep/deeper")
out, dest = unpack(backup_file(LINKY), trust_links=True)
check("a backup proved to be this Host's own keeps its links as they were",
      isinstance(out, dict) and out["links_refused"] == [] and os.path.islink(dest / "up"), out)
# Resolved from where each link will sit once live (LIVE/<path>), not from the
# staged folder, which is two levels further down.
LIVE_LINKS = DB + [("apps/deep", None, "dir"), ("apps/deep/deeper", None, "dir"),
                   ("apps/climb", "../apps/x", "link"),              # out of .host-restore/apps and back in
                   ("apps/climb-staged", f"../{dest.name}/x", "link"),  # the same, by this test's staged name
                   ("apps/deep/climb2", "../../.host-restore/apps/x", "link"),
                   ("apps/abs-plan", live_abs + "/.host-restore/plan.json", "link"),
                   ("apps/abs-restore", live_abs + "/.host-restore", "link"),
                   ("apps/rel-plan", ".host-restore/plan.json", "link"),
                   ("apps/abs-in", live_abs + "/deep/deeper", "link"),
                   ("apps/via-abs", "abs-in/../..", "link"),         # through abs-in: the apps root, inside
                   ("apps/via-abs-out", "abs-in/../../..", "link"),  # one more: outside
                   ("apps/loop-a", "loop-b", "link"), ("apps/loop-b", "loop-a", "link"),
                   ("apps/abs-dotdot", live_abs + "/../" + os.path.basename(live_abs) + "/deep", "link")]
out, dest = unpack(backup_file(LIVE_LINKS))
# loop-a goes round and round and is left out; loop-b then names a missing
# name inside the tree, which leads nowhere, so it stays.
want_out = ["abs-dotdot", "abs-plan", "abs-restore", "climb", "climb-staged", "deep/climb2", "loop-a",
            "rel-plan", "via-abs-out"]
check("a link that leaves the tree once live is left out, though it resolves inside the staged copy "
      "(../apps/x, and anything into .host-restore)",
      isinstance(out, dict) and out["links_refused"] == want_out
      and not any(os.path.lexists(dest / p) for p in want_out), out)
check("  a chain through an absolute link is followed as it will be live, and kept when it stays in",
      isinstance(out, dict) and os.readlink(dest / "via-abs") == "abs-in/../.."
      and os.readlink(dest / "abs-in") == live_abs + "/deep/deeper", out)
# What backups leave out
# (node_modules, .venv, crash, ...) stays live through the swap, so it is not in
# the staged copy the walk reads. A link that climbs through one of those is
# resolved on the Host through whatever is there, such as a node_modules/pkg
# that is itself a link to /usr/lib.
KEPT_LINKS = DB + [("apps/p", None, "dir"), ("apps/p/q", None, "dir"), ("apps/p/q/r", None, "dir"),
                   ("apps/p/q/r/proj", None, "dir"),
                   ("apps/evil", "p/q/r/proj/node_modules/pkg/../../../../../etc/hostname", "link"),
                   ("apps/via-venv", "p/.venv/x/../../q", "link"),
                   ("apps/via-crash", live_abs + "/crash/x/../../p", "link"),
                   ("apps/fine", "p/q/r/proj", "link")]
out, dest = unpack(backup_file(KEPT_LINKS))
check("a link that passes through a path the restore leaves alone is left out, though the staged "
      "copy has nothing there (node_modules, .venv, crash)",
      isinstance(out, dict) and out["links_refused"] == ["evil", "via-crash", "via-venv"]
      and os.readlink(dest / "fine") == "p/q/r/proj", out)
# The same backup, swapped in over a Host whose kept node_modules/pkg is a link
# out of the apps volume: the escape as it would happen.
_saved_paths = (hr.LIVE, hr.RESTORE_DIR, hr.STAGED, hr.PREVIOUS, hr.PLAN, hr.MANIFEST)
kept_live = tmp / "kept-live"
outside = tmp / "kept-outside"
(outside / "usr/lib/node_modules/pkg").mkdir(parents=True)
(outside / "etc").mkdir()
(outside / "etc/hostname").write_text("not the Host's\n")
hr.LIVE = kept_live
hr.RESTORE_DIR = kept_live / ".host-restore"
hr.STAGED = hr.RESTORE_DIR / "apps"
hr.PREVIOUS = hr.RESTORE_DIR / "previous"
hr.PLAN = hr.RESTORE_DIR / "plan.json"
hr.MANIFEST = hr.RESTORE_DIR / "manifest"
(kept_live / "p/q/r/proj/node_modules").mkdir(parents=True)
(kept_live / "p/q/r/proj/node_modules/pkg").symlink_to(outside / "usr/lib/node_modules/pkg")
hr.RESTORE_DIR.mkdir()
try:
    kept_out = hr.unpack(io.BytesIO(backup_file(KEPT_LINKS[:7])), ident, hr.STAGED)   # DB, p/q/r/proj, evil
    hr._write_manifest()
    hr.set_aside(kept_live, hr._manifest_files())
    hr.move_in(kept_live)
    lands = os.path.realpath(kept_live / "evil") if os.path.lexists(kept_live / "evil") else None
except hr.RestoreError as e:
    kept_out, lands = str(e), None
hr.LIVE, hr.RESTORE_DIR, hr.STAGED, hr.PREVIOUS, hr.PLAN, hr.MANIFEST = _saved_paths
check("  swapped in, no restored link leads out through the Host's own node_modules link",
      isinstance(kept_out, dict) and (lands is None or hr._inside(os.path.realpath(kept_live), lands)),
      (kept_out, lands))
check("  and the Host's node_modules link is left where it was",
      os.path.islink(kept_live / "p/q/r/proj/node_modules/pkg"))
out, _ = unpack(backup_file(DB + [("apps/.host-restore/plan.json", b"{}", "file")]))
check("a file in the restore's own folder is refused (a backup never holds one; moved in it would "
      "replace the plan)", refused(out, "never hold"), out)
# _safe_rel once let a newline through, and a name the restore
# repeats (a link it left out, a file that does not match) reached the run
# list, which Settings shows with white-space: pre-line.
NL, LS, RLO = chr(10), chr(0x2028), chr(0x202e)
for label, members_ in (
        ("a file", DB + [("apps/odd" + NL + "Restored to the backup of yesterday.", b"x", "file")]),
        ("a link", DB + [("apps/a" + LS + "b", "../../etc/passwd", "link")]),
        ("a folder", DB + [("apps/d" + RLO + "txt.exe", None, "dir")]),
        ("a part the restore skips", DB + [("gitea-repos/x" + chr(13) + ".git/HEAD", b"ref\n", "file")])):
    out, _ = unpack(backup_file(members_))
    check(f"{label} whose name holds a line break or another control character is refused, "
          f"and the refusal is one line", refused(out, "control character") and not any(
              c in out for c in (NL, LS, RLO, chr(13))), out)
out, _ = unpack(backup_file(GOOD, listed_links=[("apps/link", "app-state"),
                                                 ("apps/x" + NL + "FAKE", "y")]))
check("a manifest that names such a path is refused without repeating it",
      refused(out, "manifest names a path") and NL not in out, out)
check("the string-prefix escape check is a path-boundary check now",
      not hr._inside("/var/lib/cseries", "/var/lib/cseries-x/y") and hr._inside("/var/lib/cseries", "/var/lib/cseries/y")
      and hr._inside("/var/lib/cseries", "/var/lib/cseries"))


# ── the resealed key ────────────────────────────────────────────────────────
print("== the resealed key")
stamp = "20260927T020700Z"
body = json.dumps({"v": 1, "kind": "privatae-backup-key", "identity": ident,
                   "object": f"b/{stamp}.tar.zst.age", "entity_id": 42, "stamp": stamp}).encode()
host_ident, host_rec = age_keys.new_pair()
resealed = subprocess.run([hb.AGE, "-a", "-r", host_rec], input=body, capture_output=True).stdout
saved = {}


async def _set_state(pool, key, value):
    saved[key] = value


async def _drop_state(pool, key):
    saved.pop(key, None)


async def _host_identity(pool):
    return host_ident
hb._set_state = _set_state
hb._drop_state = _drop_state
hr._host_identity = _host_identity
hr.RESEALED_DIR = tmp / "pod-tmp" / "host-restore-keys"
res = asyncio.run(hr.accept_resealed(None, stamp, resealed, via="pasted"))
kept = hr.RESEALED_DIR / f"{stamp}.age"
check("a key file sealed to this Host, for this backup, is kept in the pod's private folder",
      res["key_ready"] and kept.read_bytes() == resealed
      and (kept.stat().st_mode & 0o777) == 0o600 and (hr.RESEALED_DIR.stat().st_mode & 0o777) == 0o700)
check("THE RULE: and not in the database, where the next backup would carry it",
      not any("resealed" in k for k in saved) and "BEGIN AGE" not in json.dumps(saved))
check("nor on the apps volume a backup takes", not str(hr.RESEALED_DIR).startswith(str(hr.LIVE)))


class KPool:
    async def execute(self, *a):
        return "OK"

    async def fetch(self, *a):
        return []

    async def fetchval(self, *a):
        return None


print("== a resealed key does not outlive its use")
asyncio.run(hr.cancel(KPool()))
check("cancel drops it", hr.load_resealed(stamp) is None)
hr.keep_resealed(stamp, resealed)
_real_configured = hb.configured
hb.configured = lambda: "age is not installed"
asyncio.run(hr.stage(KPool(), 42, stamp))
hb.configured = _real_configured
check("staging that fails drops it", hr.load_resealed(stamp) is None)
hr.keep_resealed(stamp, resealed)
_real_entry = hr._entry


async def _gone(*a):
    raise hr.RestoreError("that backup is not in the list any more")
hr._entry = _gone
hb.configured = lambda: None
try:
    asyncio.run(hr._stage(KPool(), 42, stamp, unverified_ok=True))
except hr.RestoreError:
    pass
hb.configured = _real_configured
hr._entry = _real_entry
check("staging drops it as soon as it has read it", hr.load_resealed(stamp) is None)
hr.keep_resealed(stamp, resealed)
asyncio.run(hr.finish(KPool(), 42))
check("finish drops it", hr.load_resealed(stamp) is None)


class RunPool(KPool):
    """Records the restore's run row (host_backup_runs.parts)."""

    def __init__(self):
        self.parts = []

    async def execute(self, sql, *a):
        if "INSERT INTO host_backup_runs" in sql:
            self.parts.append(json.loads(a[3]))
        return "OK"


# A replacement Host's restore is always unverified, and its links that lead
# outside the restored files are left out: the owner is told which, in the
# run and in what the Backup tab reads once the Host is back.
hr._write_plan({"state": hr.FINISHING, "live_db": "hostmem", "stamp": stamp,
                "taken_at": "2026-09-27T02:07:00+00:00", "staged_at": "2026-09-29T01:00:00+00:00",
                "verified": False, "links_refused": ["apps-link", "deep/out"], "links_refused_count": 57})
rp = RunPool()
asyncio.run(hr.finish(rp, 42))
got_run = (rp.parts[0] if rp.parts else {}).get("restored") or {}
check("the run says which links were left out, and how many",
      got_run.get("links_left_out") == ["apps-link", "deep/out"] and got_run.get("links_left_out_count") == 57
      and got_run.get("verified") is False, got_run)
check("  and so does what Settings reads once the Host is back",
      saved.get("restore", {}).get("state") == "ok"
      and saved["restore"].get("links_left_out") == ["apps-link", "deep/out"]
      and saved["restore"].get("links_left_out_count") == 57 and saved["restore"].get("verified") is False,
      saved.get("restore"))
# Restored onto a machine with no recovery key (a new one: a restore keeps
# this machine's keys): the Host is told, so its next turn tells the owner
# backups are paused until they add theirs.
told = []
_real_recips, _real_tell = hb.recipients, hb.tell_host_paused


async def _none(pool, eid, bp=None):
    return []


async def _with_key(pool, eid, bp=None):
    return [{"kind": "host-recovery"}]


async def _tell(pool, eid, why=""):
    told.append(why)
    return True
hb.recipients, hb.tell_host_paused = _none, _tell
for recips in (_none, _with_key):
    hb.recipients = recips
    hr._write_plan({"state": hr.FINISHING, "live_db": "hostmem", "stamp": stamp,
                    "taken_at": "2026-09-27T02:07:00+00:00", "staged_at": "2026-09-29T01:00:00+00:00",
                    "verified": False})
    asyncio.run(hr.finish(RunPool(), 42))
hb.recipients, hb.tell_host_paused = _real_recips, _real_tell
check("a restore onto a machine with no recovery key tells the Host backups are paused, saying why; "
      "one onto a machine with a key does not", told == [hb.RESTORED_WITHOUT_KEY], told)
# What the carry said about the owner's sign-in (carry_rows keeps it with its
# marker in the restored database): the restore's result says it, in the run
# and in what Settings reads, and the Host is told, so its next turn tells the
# owner. Nothing to say, nothing said.
SIGN_IN_NOTE = "This backup came from a Host whose owner signed in with another email."


class MarkedPool(RunPool):
    """A restored database whose carry marker holds a word on the sign-in."""

    async def fetchval(self, sql, *a):
        if sql.startswith("SELECT v FROM host_backup_state") and a and a[0] == hr.CARRIED_MARKER:
            return json.dumps({"stamp": stamp, "owner": SIGN_IN_NOTE})
        return None


signed_in_told = []


async def _sign_in_told(pool, eid, note):
    signed_in_told.append(note)
_real_sign_in_tell = getattr(hr, "_tell_host_sign_in", None)
hr._tell_host_sign_in = _sign_in_told
hb.recipients = _with_key
got_owner = []
for pool_cls in (MarkedPool, RunPool):
    hr._write_plan({"state": hr.FINISHING, "live_db": "hostmem", "stamp": stamp,
                    "taken_at": "2026-09-27T02:07:00+00:00", "staged_at": "2026-09-29T01:00:00+00:00"})
    _p = pool_cls()
    asyncio.run(hr.finish(_p, 42))
    got_owner.append(((_p.parts[0].get("restored") or {}).get("owner") if _p.parts else "no run",
                      saved.get("restore", {}).get("owner")))
hb.recipients = _real_recips
hr._tell_host_sign_in = _real_sign_in_tell
check("the restore's result says what the carry said about the owner's sign-in, in the run and in "
      "what Settings reads; a restore with nothing to say says nothing",
      got_owner == [(SIGN_IN_NOTE, SIGN_IN_NOTE), (None, None)], got_owner)
check("  and the Host is told it, once, so its next turn tells the owner",
      signed_in_told == [SIGN_IN_NOTE], signed_in_told)


class FixPool(RunPool):
    """Records every statement finish() sends."""

    def __init__(self):
        super().__init__()
        self.sent = []

    async def execute(self, sql, *a):
        self.sent.append((" ".join(sql.split()), a))
        return await super().execute(sql, *a)
fp = FixPool()
hr._write_plan({"state": hr.FINISHING, "live_db": "hostmem", "stamp": stamp,
                "taken_at": "2026-09-27T02:07:00+00:00", "staged_at": "2026-09-29T01:00:00+00:00"})
asyncio.run(hr.finish(fp, 42))
fix = [x for x in fp.sent if x[0].startswith("UPDATE host_backup_runs SET state = 'ok'")]
check("the backup a restore came from is recorded as the good backup it was, not the 'running' its own "
      "dump caught (which the boot sweep calls interrupted)",
      len(fix) == 1 and fix[0][1][0] == stamp
      and fix[0][1][1] == _dt_mod.datetime(2026, 9, 27, 2, 7, tzinfo=_dt_mod.timezone.utc)
      and "state = 'running'" in fix[0][0] and "error LIKE 'interrupted:%'" in fix[0][0]
      and "reason <> 'restore'" in fix[0][0], fp.sent[:3])
check("  and it is set before the restore's own record and before the Host is told",
      fp.sent.index(fix[0]) < next(i for i, x in enumerate(fp.sent) if x[0].startswith("INSERT INTO host_backup_runs")))

print("== the apps that were running come back when the restore finishes")
# At the Host's start on the restored data each resident app is held and
# woken (supervisor.ensure_resident_apps), and a wake of an app whose data is
# still to be put back is answered "still starting" (local_runtime,
# app_data_pending). The hold re-wakes on its own doubling wait
# (harness_backend.resident_retry_wait): measured on the cloud VM, Telegram
# stayed stopped about two minutes after an unverified restore. Once finish
# has put the data back, the held apps are woken then; an app nobody held
# (it was not running) is not.
from server.applications import harness_backend as HBK  # noqa: E402
_real_wake, _real_retry = HBK._wake_app, HBK.APP_RT_RESIDENT_RETRY_S
_real_rad = hr._restore_app_data
woken_by = []


async def _wake_stub(app_id, creds=None):
    woken_by.append(app_id)
    if app_id in hr.app_data_pending():
        return {"ok": False, "code": 503,
                "error": f"still starting: the data of {app_id} is being put back from a backup"}
    return {"ok": True, "url": "http://10.89.78.20:8850"}


async def _put_back(plan):
    for a in plan.get("app_data") or []:
        a["state"] = hr.APP_DATA_RESTORED
    hr._write_plan(plan)


async def _resume_after_finish():
    HBK._wake_app = _wake_stub
    HBK.APP_RT_RESIDENT_RETRY_S = 30     # the hold's own next try: far past this check
    hr._restore_app_data = _put_back
    hr._write_plan({"state": hr.FINISHING, "live_db": "hostmem", "stamp": stamp,
                    "taken_at": "2026-09-27T02:07:00+00:00", "staged_at": "2026-09-29T01:00:00+00:00",
                    "app_data": [{"app_id": "telegram", "state": hr.APP_DATA_PENDING},
                                 {"app_id": "notes", "state": hr.APP_DATA_PENDING}]})
    HBK.app_hold("telegram")             # resident, held at the Host's start
    refused = await HBK._wake_app("telegram")
    woken_by.clear()
    await hr.finish(RunPool(), 42)
    for _ in range(40):
        if (HBK._state.get("telegram") or {}).get("url"):
            break
        await asyncio.sleep(0.05)
    url = (HBK._state.get("telegram") or {}).get("url")
    HBK._stop_lease("telegram")
    return refused, url


try:
    refused, url = asyncio.run(_resume_after_finish())
finally:
    HBK._wake_app, HBK.APP_RT_RESIDENT_RETRY_S = _real_wake, _real_retry
    hr._restore_app_data = _real_rad
check("precondition: while its data is pending, the held app's wake is answered still starting",
      not refused["ok"] and refused.get("code") == 503, refused)
check("once finish has put the data back, the held app is woken within two seconds, not at its "
      "backoff", url == "http://10.89.78.20:8850" and woken_by[:1] == ["telegram"], (url, woken_by))
check("  an app in the restore that nothing held (not running) is not woken", "notes" not in woken_by,
      woken_by)
HBK._state.pop("telegram", None)
hr.keep_resealed(stamp, resealed)
os.utime(kept, (0, 0))
check("one older than an hour is dropped, not used", hr.load_resealed(stamp) is None and not kept.exists())
res = asyncio.run(hr.accept_resealed(None, stamp, resealed, via="pasted"))
try:
    asyncio.run(hr.accept_resealed(None, "20260101T000000Z", resealed, via="pasted"))
    check("a key file for another backup is refused", False)
except hr.RestoreError:
    check("a key file for another backup is refused", True)
other_resealed = subprocess.run([hb.AGE, "-a", "-r", age_keys.new_pair()[1]], input=body,
                                capture_output=True).stdout
try:
    asyncio.run(hr.accept_resealed(None, stamp, other_resealed, via="pasted"))
    check("a key file sealed to some other key is refused", False)
except hr.RestoreError:
    check("a key file sealed to some other key is refused", True)

print("== the backup a restore takes first")
BEFORE_ROW = {"started_at": _dt_mod.datetime(2026, 9, 29, 1, 0, tzinfo=_dt_mod.timezone.utc),
              "finished_at": _dt_mod.datetime(2026, 9, 29, 1, 5, tzinfo=_dt_mod.timezone.utc),
              "reason": "before-restore", "state": "ok", "stamp": "20260929T010000Z", "stored": "staged",
              "object_key": "d/s/20260929T010000Z.tar.zst.age", "sidecar_key": "d/s/20260929T010000Z.key.age",
              "size_bytes": 10, "sha256": "a" * 64, "sidecar_sha256": "b" * 64, "sidecar_bytes": 3,
              "recipients": '[{"kind": "host-recovery"}]', "parts": '{"db": {"ok": true}}', "error": None}


class SPool(KPool):
    def __init__(self):
        self.inserted = []
        self.have = False

    async def fetchval(self, q, *a):
        if "reason <> 'restore'" in q:
            return 1                      # this Host has backups of its own
        if "reason = 'before-restore'" in q:
            return 1 if self.have else None
        return None

    async def fetchrow(self, q, *a):
        return dict(BEFORE_ROW) if "FROM host_backup_runs WHERE id" in q else None

    async def execute(self, q, *a):
        if q.startswith("INSERT INTO host_backup_runs"):
            self.inserted.append(a)
            self.have = True
        return "OK"


runs_asked = []


async def _fake_run(pool, eid, reason, keep=()):
    runs_asked.append((reason, keep))
    return {"state": "ok", "run_id": 9, "stamp": BEFORE_ROW["stamp"]}


async def _entry_here(pool, eid, st, where=None):
    return {"where": "platform", "key": f"d/s/{st}.tar.zst.age", "bytes": 10, "taken_at": "t"}


async def _stop_source(pool, eid, entry):
    raise hr.RestoreError("stopped here by the test")
_saved = (hb._run, hr._entry, hr._open_source, hb.configured)
hb._run, hr._entry, hr._open_source, hb.configured = _fake_run, _entry_here, _stop_source, (lambda: None)
hr.keep_resealed(stamp, resealed)
spool = SPool()
got = asyncio.run(hr.stage(spool, 42, stamp, unverified_ok=True))
hb._run, hr._entry, hr._open_source, hb.configured = _saved
check("with platform storage off, the backup taken first never drops the copy being restored",
      runs_asked == [("before-restore", (stamp,))], runs_asked)
_undo = asyncio.run(hr._run_record(spool, 9))
check("that backup's record is kept for the plan, in JSON",
      _undo["stamp"] == BEFORE_ROW["stamp"] and _undo["parts"] == {"db": {"ok": True}}
      and isinstance(_undo["started_at"], str), _undo)
spool = SPool()
kept_it = asyncio.run(hr._keep_undo_point(spool, {"undo_point": _undo}))
check("finish records it in the restored database, which had no record of it",
      kept_it and len(spool.inserted) == 1 and spool.inserted[0][4] == BEFORE_ROW["stamp"]
      and spool.inserted[0][0] == BEFORE_ROW["started_at"], spool.inserted)
check("  once only", asyncio.run(hr._keep_undo_point(spool, {"undo_point": _undo})) is False
      and len(spool.inserted) == 1)

print("== a backup this Host made, and one it did not")
AUTH = os.urandom(32)
_blob = backup_file(GOOD)


class APool(SPool):
    async def fetch(self, q, *a):
        if "FROM kernel_secrets" in q:
            return [{"id": hb.AUTH_KEY_ID, "secret": AUTH}]
        return []


def sidecar_for(key=AUTH, **over):
    doc = {"v": 1, "kind": "privatae-backup-key", "identity": ident, "object": f"b/{stamp}.tar.zst.age",
           "entity_id": 42, "stamp": stamp, "made_at": "2026-09-27T02:07:00+00:00",
           "data_sha256": hashlib.sha256(_blob).hexdigest(), "data_bytes": len(_blob)}
    if key is not None:
        doc["auth"] = hb.auth_tag(doc, key)
    doc.update(over)
    return subprocess.run([hb.AGE, "-a", "-r", host_rec], input=json.dumps(doc).encode(),
                          capture_output=True).stdout


res = asyncio.run(hr.accept_resealed(APool(), stamp, sidecar_for(), via="pasted"))
check("a key file this Host tagged verifies when it is sealed here", res["verified"] is True, res)
check("  and the list says so", asyncio.run(hr.verified(APool(), stamp)) is True)
for label, armored in (("with no tag", sidecar_for(key=None)),
                       ("tagged by a key this Host does not hold (what platform storage could make)",
                        sidecar_for(key=os.urandom(32))),
                       ("naming another encrypted file than the one it was tagged for",
                        sidecar_for(data_sha256="0" * 64))):
    res = asyncio.run(hr.accept_resealed(APool(), stamp, armored, via="pasted"))
    check(f"a key file {label} does not verify", res["verified"] is False, res)
runs_asked.clear()
unpacked_with = []


async def _source_here(pool, eid, entry):
    return io.BytesIO(_blob)


def _record_unpack(src, identity, staged, **kw):
    unpacked_with.append(kw)
    raise hr.RestoreError("stopped here by the test")


_saved = (hb._run, hr._entry, hr._open_source, hb.configured, hr.unpack)
hb._run, hr._entry, hr._open_source, hb.configured, hr.unpack = (_fake_run, _entry_here, _source_here,
                                                                  (lambda: None), _record_unpack)
got = asyncio.run(hr.stage(APool(), 42, stamp))          # the last key kept was not this Host's
check("a backup this Host cannot verify is not staged unless the owner says so, and nothing runs",
      got["state"] == hr.FAILED and "cannot confirm" in got["error"] and runs_asked == [] and not unpacked_with,
      got)
hr.keep_resealed(stamp, sidecar_for(key=os.urandom(32)))
asyncio.run(hr.stage(APool(), 42, stamp, unverified_ok=True))
check("  with the owner's say-so it is, its links held to the restored tree",
      runs_asked and unpacked_with and unpacked_with[-1]["trust_links"] is False, unpacked_with)
hr.keep_resealed(stamp, sidecar_for())
asyncio.run(hr.stage(APool(), 42, stamp))
check("a backup this Host made is staged, and the encrypted file must be the one its key file names",
      unpacked_with[-1] == {"expect": {"sha256": hashlib.sha256(_blob).hexdigest(), "bytes": len(_blob)},
                            "trust_links": True}, unpacked_with[-1])


async def _entry_other_sha(pool, eid, st, where=None):
    return dict(await _entry_here(pool, eid, st), sha256="0" * 64)
hr._entry = _entry_other_sha
hr.keep_resealed(stamp, sidecar_for())
n_unpacked = len(unpacked_with)
got = asyncio.run(hr.stage(APool(), 42, stamp))
check("a backup the platform recorded with another sha256 than its key file names is not downloaded",
      got["state"] == hr.FAILED and "platform's record" in got["error"] and len(unpacked_with) == n_unpacked, got)
hr._entry = _entry_here
hb._run, hr._entry, hr._open_source, hb.configured, hr.unpack = _saved


# ── a restore brings back data, never older authorization ───────────────────
print("== a restore never brings back access revoked since")
import datetime as _dt0  # noqa: E402
import re as _re  # noqa: E402
import sqlite3  # noqa: E402


class FakeDB:
    """A database as carry_rows sees it: tables of rows, just enough SQL."""

    def __init__(self, tables, fks=()):
        # tables: {name: {"cols": [...], "rows": [dict], "serial": col or None}}
        # fks: (table, column) pairs that reference entity_registry(id)
        self.t = {k: {"cols": list(v["cols"]), "rows": [dict(r) for r in v["rows"]],
                      "serial": v.get("serial")} for k, v in tables.items()}
        self.setvals = []
        self.fks = list(fks)
        self.replica = False

    def transaction(self):
        class T:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False
        return T()

    @staticmethod
    def _table(sql):
        return _re.search(r'(?:FROM|INTO) "([a-z_]+)"', sql).group(1)

    async def fetch(self, sql, *a):
        if "confdeltype" in sql:
            return []                      # the real key graph is tested on a real schema
        if "FROM pg_constraint" in sql:
            return [{"table_name": t, "column_name": c} for t, c in self.fks]
        if "information_schema.columns" in sql and "column_name = 'entity_id'" in sql:
            return [{"table_name": t, "column_name": "entity_id"}
                    for t, v in self.t.items() if "entity_id" in v["cols"]]
        if "information_schema.columns" in sql:
            return [{"column_name": c} for c in self.t.get(a[0], {}).get("cols", [])]
        t = self.t[self._table(sql)]
        cols = _re.findall(r'"([a-z_]+)"', sql.split(" FROM ")[0])
        rows = t["rows"]
        if "= ANY(" in sql:
            key = _re.search(r'WHERE "([a-z_]+)" = ANY', sql).group(1)
            rows = [r for r in rows if r[key] in a[0]]
        return [{c: r.get(c) for c in cols} for r in rows]

    async def execute(self, sql, *a):
        if sql.startswith("SET LOCAL session_replication_role"):
            self.replica = sql.endswith("replica")
            return
        m = _re.match(r'UPDATE "([a-z_]+)" SET "([a-z_]+)" = \$1 WHERE "([a-z_]+)" = \$2$', sql)
        if m:
            for r in self.t[m.group(1)]["rows"]:
                if r.get(m.group(3)) == a[1]:
                    r[m.group(2)] = a[0]
            return
        m = _re.match(r'DELETE FROM "([a-z_]+)" WHERE "([a-z_]+)" = \$1$', sql)
        if m:
            t = self.t[m.group(1)]
            t["rows"] = [r for r in t["rows"] if r.get(m.group(2)) != a[0]]
            return
        if sql.startswith("DELETE FROM"):
            t = self.t[self._table(sql)]
            if "= ANY(" in sql:
                key = _re.search(r'WHERE "([a-z_]+)" = ANY', sql).group(1)
                t["rows"] = [r for r in t["rows"] if r[key] not in a[0]]
            else:
                t["rows"] = []
            return
        if sql.startswith("SELECT setval"):
            self.setvals.append(a[0])
            return
        if sql.startswith("UPDATE vault_envelope SET superseded_at"):
            for r in self.t["vault_envelope"]["rows"]:
                if r.get("logical_name") == "browser:profile" and r.get("superseded_at") is None:
                    r["superseded_at"] = "now"
            return
        if sql.startswith("INSERT INTO host_backup_state (k, v)"):
            t = self.t["host_backup_state"]
            t["rows"] = [r for r in t["rows"] if r["k"] != a[0]] + [{"k": a[0], "v": json.loads(a[1])}]
            return
        raise AssertionError(sql)

    async def executemany(self, sql, rows):
        t = self.t[self._table(sql)]
        cols = _re.findall(r'"([a-z_]+)"', sql.split("VALUES")[0])[1:]
        for r in rows:
            t["rows"].append(dict(zip(cols, r)))

    async def fetchval(self, sql, *a):
        if sql.startswith('SELECT 1 FROM "entity_registry"'):
            return 1 if any(r["id"] == a[0] for r in self.t.get("entity_registry", {"rows": []})["rows"]) else None
        if sql.startswith("SELECT v FROM host_backup_state"):
            return next((r["v"] for r in self.t.get("host_backup_state", {"rows": []})["rows"] if r["k"] == a[0]), None)
        if "pg_get_serial_sequence" in sql:
            t = self.t.get(a[0])
            return f"{a[0]}_{a[1]}_seq" if t and t.get("serial") == a[1] else None
        raise AssertionError(sql)


T0 = _dt0.datetime(2026, 9, 20, tzinfo=_dt0.timezone.utc)
K1, K2, B1, B2 = (bytes([i]) * 32 for i in (1, 2, 3, 4))
TOK = {"cols": ["id", "name", "operator_kind", "revoked_at"], "serial": "id"}
GRANT = {"cols": ["id", "token_id", "op", "revoked_at"], "serial": "id"}
KEYS = {"cols": ["id", "identity", "kind", "label", "pubkey", "superseded_at"], "serial": "id"}
SECRETS = {"cols": ["id", "secret"]}
STATE = {"cols": ["k", "v"]}
# The database live until the swap: bastion 5 revoked, its grant revoked, the
# step-up re-enrolled on a new phone, the bastion key retired and a new
# recovery key adopted, and a recovery-key offer pending.
live_db = FakeDB({
    "mcp_client_tokens": dict(TOK, rows=[{"id": 5, "name": "old-laptop", "operator_kind": "bastion", "revoked_at": T0},
                                         {"id": 6, "name": "nas", "operator_kind": "bastion", "revoked_at": None}]),
    "nac_mgmt_grants": dict(GRANT, rows=[{"id": 1, "token_id": 5, "op": "uptime", "revoked_at": T0}]),
    "backup_user_key": dict(KEYS, rows=[
        {"id": 1, "identity": "user:42", "kind": "host-recovery", "label": "", "pubkey": K1, "superseded_at": T0},
        {"id": 2, "identity": "user:42", "kind": "bastion", "label": "old-laptop", "pubkey": B1, "superseded_at": T0},
        {"id": 3, "identity": "user:42", "kind": "host-recovery", "label": "", "pubkey": K2, "superseded_at": None},
        {"id": 4, "identity": "user:42", "kind": "bastion", "label": "nas", "pubkey": B2, "superseded_at": None}]),
    "kernel_secrets": dict(SECRETS, rows=[{"id": "nac_stepup_totp", "secret": b"NEW-PHONE"},
                                          {"id": "nac_stepup_enrolled", "secret": b"1"},
                                          {"id": "vault_host_x25519", "secret": b"LIVE-VAULT-KEY"},
                                          {"id": "app_token", "secret": b"ROTATED"},
                                          # wrong codes since the backup: the apps' shared
                                          # record and one app's own
                                          {"id": "nac_stepup_failures_apps", "secret": b'{"n": 9, "at": 1}'},
                                          {"id": "nac_stepup_failures_app:browser", "secret": b'{"n": 9, "at": 1}'},
                                          # the Hub key every live machine pinned
                                          {"id": "hub_wg", "secret": b"LIVE-HUB-WG"}]),
    "host_backup_state": dict(STATE, rows=[{"k": "recovery_offer", "v": {"fingerprint": "live-offer"}},
                                           {"k": "recovery_own", "v": {"fingerprint": "live-own"}},
                                           {"k": "restore", "v": {"state": "staging"}}]),
})
# The restored database: the backup's day, when all of that was still open.
restored_db = FakeDB({
    "mcp_client_tokens": dict(TOK, rows=[{"id": 5, "name": "old-laptop", "operator_kind": "bastion", "revoked_at": None}]),
    "nac_mgmt_grants": dict(GRANT, rows=[{"id": 1, "token_id": 5, "op": "uptime", "revoked_at": None}]),
    "backup_user_key": dict(KEYS, rows=[
        {"id": 1, "identity": "user:17", "kind": "host-recovery", "label": "", "pubkey": K1, "superseded_at": None},
        {"id": 2, "identity": "user:17", "kind": "bastion", "label": "old-laptop", "pubkey": B1, "superseded_at": None}]),
    "kernel_secrets": dict(SECRETS, rows=[{"id": "nac_stepup_totp", "secret": b"LOST-PHONE"},
                                          {"id": "nac_stepup_enrolled", "secret": b"1"},
                                          {"id": "vault_host_x25519", "secret": b"BACKUP-VAULT-KEY"},
                                          {"id": "app_token", "secret": b"OLD"},
                                          {"id": "entity_sign", "secret": b"SIGN"},
                                          {"id": "hub_wg", "secret": b"BACKUP-HUB-WG"},
                                          {"id": "nac_stepup_failures_app:terminal", "secret": b'{"n": 2, "at": 1}'}]),
    "host_backup_state": dict(STATE, rows=[{"k": "recovery_offer", "v": {"fingerprint": "old-offer"}},
                                           {"k": "recovery_own", "v": {"fingerprint": "old-own"}},
                                           {"k": "restore", "v": {"state": "old"}}]),
})
done = asyncio.run(hr.carry_rows(live_db, restored_db))
r = restored_db.t
check("a bastion revoked since stays revoked, and one added since is there",
      {t["id"]: t["revoked_at"] for t in r["mcp_client_tokens"]["rows"]} == {5: T0, 6: None})
check("its management grant stays revoked", r["nac_mgmt_grants"]["rows"][0]["revoked_at"] == T0)
secrets = {x["id"]: x["secret"] for x in r["kernel_secrets"]["rows"]}
check("the second factor is the new phone's, never the lost one's", secrets["nac_stepup_totp"] == b"NEW-PHONE")
check("app tokens stay revoked (the rotated signing secret)", secrets["app_token"] == b"ROTATED")
# The owner's and the Host's records were carried, the
# apps' were not, so a restore gave a guesser holding app tokens its free
# tries back.
check("the apps' wrong-code records are the live ones: the shared one and each app's own "
      "(and one only the backup held is dropped)",
      secrets.get("nac_stepup_failures_apps") == b'{"n": 9, "at": 1}'
      and secrets.get("nac_stepup_failures_app:browser") == b'{"n": 9, "at": 1}'
      and "nac_stepup_failures_app:terminal" not in secrets,
      sorted(k for k in secrets if "failures" in k))
from server.network import nac_stepup as _NS  # noqa: E402
check("every attempt-limit record nac_stepup keeps is carried, by id or by prefix",
      {_NS._TOTP_FAILURES_ID, _NS._TOTP_HOST_FAILURES_ID, _NS._TOTP_APPS_FAILURES_ID} <= set(hr.CARRIED_SECRETS)
      and _NS._TOTP_APP_FAILURES_PREFIX in getattr(hr, "CARRIED_SECRET_PREFIXES", ()))
check("the vault key and the signing identity stay the backup's (they open and sign the restored data)",
      secrets["vault_host_x25519"] == b"BACKUP-VAULT-KEY" and secrets["entity_sign"] == b"SIGN")
check("the Hub's WireGuard key is the live one: every paired machine pinned it",
      secrets.get("hub_wg") == b"LIVE-HUB-WG", secrets.get("hub_wg"))
active = sorted((k["kind"], k["pubkey"]) for k in r["backup_user_key"]["rows"] if k["superseded_at"] is None)
check("the next backup goes to the live keys: the new recovery key and the live bastion, never the "
      "retired ones, with no duplicate rows", active == [("bastion", B2), ("host-recovery", K2)]
      and len(r["backup_user_key"]["rows"]) == 4, r["backup_user_key"]["rows"])
state = {x["k"]: x["v"] for x in r["host_backup_state"]["rows"]}
check("the live recovery-key offer is kept; other state stays the backup's",
      state["recovery_offer"] == {"fingerprint": "live-offer"} and state["restore"] == {"state": "old"})
check("which key the owner made on this Host is the live record, as the keys are",
      state.get("recovery_own") == {"fingerprint": "live-own"}, state.get("recovery_own"))
check("serial sequences follow the carried rows",
      {"mcp_client_tokens_id_seq", "nac_mgmt_grants_id_seq", "backup_user_key_id_seq"} <= set(restored_db.setvals))
check("every table that holds access is on the list",
      {"mcp_client_tokens", "nac_mgmt_grants", "nac_bastion_suspend", "nac_bastion_quarantine",
       "nac_pairing_grants", "bastion_pairing_tokens", "service_reach_grants", "backup_user_key",
       "vault_identity_pubkey", "entity_permissions", "browser_form_grant"} <= set(hr.CARRIED_TABLES)
      and {"nac_stepup_totp", "nac_stepup_enrolled", "nac_stepup_last_counter"} <= set(hr.CARRIED_SECRETS))

print("== the owner's account and sessions are the live ones")
# The passkeys and recovery codes were carried, and the account they sign in
# to was the backup's: another password and cookie name, or none at all, and
# the owner sent to /setup (scripts/test_restore_carry_real_schema.py drives
# the sign-in itself on the real schema).
OWN = {"cols": ["id", "email", "password_hash", "cookie_suffix"]}
SESS = {"cols": ["token_hash", "origin", "parent"]}
LIVE_OWNER = {"id": 1, "email": "owner@live.example", "password_hash": "scrypt$live", "cookie_suffix": "live0001"}
LIVE_SESSIONS = [{"token_hash": "h-browser", "origin": "home.box.example", "parent": None},
                 {"token_hash": "h-app", "origin": "apps-settings-home.box.example", "parent": "h-browser"}]
for label, backup_owner, want_note in (
        ("the backup's owner had another email", [{"id": 1, "email": "someone@backup.example",
                                                   "password_hash": "scrypt$old", "cookie_suffix": "back0001"}],
         "another email"),
        ("the backup's owner is the same, its email typed in other case",
         [dict(LIVE_OWNER, email="Owner@Live.example", password_hash="scrypt$old", cookie_suffix="back0002")], None),
        ("the backup was taken before the owner was made", [], None)):
    o_src = FakeDB({"owner_account": dict(OWN, rows=[LIVE_OWNER]), "owner_sessions": dict(SESS, rows=LIVE_SESSIONS),
                    "owner_passkeys": {"cols": ["credential_id", "public_key"], "rows": [{"credential_id": b"k", "public_key": b"p"}]},
                    "host_backup_state": dict(STATE, rows=[])})
    o_dst = FakeDB({"owner_account": dict(OWN, rows=backup_owner),
                    "owner_sessions": dict(SESS, rows=[{"token_hash": "h-backup", "origin": "home.box.example",
                                                        "parent": None}]),
                    "owner_passkeys": {"cols": ["credential_id", "public_key"], "rows": []},
                    "host_backup_state": dict(STATE, rows=[])})
    o_done = asyncio.run(hr.carry_rows(o_src, o_dst, stamp="20261019T020000Z"))
    check(f"{label}: the live owner account and the live sessions are kept, the backup's are not",
          o_dst.t["owner_account"]["rows"] == [LIVE_OWNER] and o_dst.t["owner_sessions"]["rows"] == LIVE_SESSIONS,
          (o_dst.t["owner_account"]["rows"], o_dst.t["owner_sessions"]["rows"]))
    o_mark = next((r["v"] for r in o_dst.t["host_backup_state"]["rows"] if r["k"] == hr.CARRIED_MARKER), {})
    note = o_done.get("owner_note")
    check(f"  and the restore says {'so' if want_note else 'nothing about the sign-in'}, with the carry's marker",
          ((want_note in (note or "") and "owner@live.example" in note and "someone@backup" not in note)
           if want_note else note is None) and o_mark.get("owner") == note, (note, o_mark))
_owner_note = getattr(hr, "owner_note", lambda live, backup: "no owner_note")
check("no owner when the restore ran: the backup's is not brought back, and the restore says to make one",
      "/setup" in (_owner_note(None, "someone@backup.example") or ""))
check("nothing on either side: nothing to say", _owner_note(None, None) is None)

print("== a backup from another Host number: renumbered, and only live access kept")
# Host 17 was lost; this Host is 42. The restored database is Host 17's.
REG = {"cols": ["id", "privatae_entity_id", "display_name", "behavioral_params"], "serial": "id"}
MEM = {"cols": ["id", "entity_id", "text"], "serial": "id"}
CHAN = {"cols": ["id", "owner_entity_id", "title"], "serial": "id"}
PROJ = {"cols": ["id", "entity_id", "name"], "serial": "id"}
PEERS = {"cols": ["peer_entity_id", "phone"]}
ENV = {"cols": ["id", "owner_identity", "logical_name"]}
PERM = {"cols": ["id", "entity_id", "permission_key", "revoked_at"], "serial": "id"}
FKS = [("entity_memory", "entity_id"), ("channels", "owner_entity_id"), ("entity_permissions", "entity_id")]
live42 = FakeDB({
    "entity_registry": dict(REG, rows=[{"id": 42, "privatae_entity_id": 42, "display_name": "New", "behavioral_params": {}}]),
    "mcp_client_tokens": dict(TOK, rows=[{"id": 9, "name": "desk", "operator_kind": "bastion", "revoked_at": None}]),
    "nac_mgmt_grants": dict(GRANT, rows=[]),
    "entity_permissions": dict(PERM, rows=[{"id": 1, "entity_id": 42, "permission_key": "tool.x", "revoked_at": None}]),
    "backup_user_key": dict(KEYS, rows=[{"id": 1, "identity": "user:42", "kind": "host-recovery", "label": "",
                                          "pubkey": K2, "superseded_at": None}]),
    "kernel_secrets": dict(SECRETS, rows=[{"id": "nac_stepup_totp", "secret": b"42-PHONE"},
                                          {"id": "entity_sign", "secret": b"42-SIGN"},
                                          {"id": "vault_host_x25519", "secret": b"42-VAULT"}]),
    "host_backup_state": dict(STATE, rows=[]),
}, fks=FKS)
lost17 = FakeDB({
    "entity_registry": dict(REG, rows=[{"id": 17, "privatae_entity_id": 17, "display_name": "Nova",
                                        "behavioral_params": {"timezone": "Europe/Zurich"}}]),
    "entity_memory": dict(MEM, rows=[{"id": 1, "entity_id": 17, "text": "the lost Host's memory"}]),
    "channels": dict(CHAN, rows=[{"id": 1, "owner_entity_id": 17, "title": "chat"}]),
    "projects": dict(PROJ, rows=[{"id": 1, "entity_id": 17, "name": "site"}]),
    "peer_cards": dict(PEERS, rows=[{"peer_entity_id": 17, "phone": "+41"}, {"peer_entity_id": 8, "phone": "+1"}]),
    "inbox_messages": {"cols": ["id", "from_entity_id", "to_entity_id"],
                       "rows": [{"id": 1, "from_entity_id": 8, "to_entity_id": 17},
                                {"id": 2, "from_entity_id": 17, "to_entity_id": 8}]},
    "vault_envelope": dict(ENV, rows=[{"id": "a", "owner_identity": "host:17", "logical_name": "gmail"},
                                      {"id": "b", "owner_identity": "user:17", "logical_name": "sealed"},
                                      {"id": "c", "owner_identity": "user:8", "logical_name": "other"}]),
    "mcp_client_tokens": dict(TOK, rows=[{"id": 3, "name": "old-laptop", "operator_kind": "bastion", "revoked_at": None}]),
    "nac_mgmt_grants": dict(GRANT, rows=[{"id": 1, "token_id": 3, "op": "uptime", "revoked_at": None}]),
    "entity_permissions": dict(PERM, rows=[{"id": 5, "entity_id": 17, "permission_key": "tool.y", "revoked_at": None}]),
    "backup_user_key": dict(KEYS, rows=[{"id": 1, "identity": "user:17", "kind": "host-recovery", "label": "",
                                          "pubkey": K1, "superseded_at": None}]),
    "kernel_secrets": dict(SECRETS, rows=[{"id": "nac_stepup_totp", "secret": b"17-PHONE"},
                                          {"id": "entity_sign", "secret": b"17-SIGN"},
                                          {"id": "vault_host_x25519", "secret": b"17-VAULT"}]),
    "host_backup_state": dict(STATE, rows=[]),
}, fks=FKS)
done = asyncio.run(hr.carry_rows(live42, lost17, 17, 42))
L = lost17.t
check("the restored Host's row is this Host's number now, with the restored persona and settings",
      L["entity_registry"]["rows"] == [{"id": 42, "privatae_entity_id": 42, "display_name": "Nova",
                                        "behavioral_params": {"timezone": "Europe/Zurich"}}], L["entity_registry"]["rows"])
check("its data is this Host's: memory, chats and projects renumbered",
      L["entity_memory"]["rows"][0]["entity_id"] == 42 and L["channels"]["rows"][0]["owner_entity_id"] == 42
      and L["projects"]["rows"][0]["entity_id"] == 42)
check("columns that name other Hosts are left as they were",
      sorted(r["peer_entity_id"] for r in L["peer_cards"]["rows"]) == [8, 17])
check("this Host's side of its Circles messages follows the number; the other side does not",
      [(r["from_entity_id"], r["to_entity_id"]) for r in L["inbox_messages"]["rows"]] == [(8, 42), (42, 8)])
check("the Keys-app credentials follow the number, and the restored vault key that opens them stays",
      sorted((r["id"], r["owner_identity"]) for r in L["vault_envelope"]["rows"])
      == [("a", "host:42"), ("b", "user:42"), ("c", "user:8")]
      and {x["id"]: x["secret"] for x in L["kernel_secrets"]["rows"]}["vault_host_x25519"] == b"17-VAULT")
check("the old Host's access is gone: its bastion, its grant, its permissions, its backup key",
      [t["name"] for t in L["mcp_client_tokens"]["rows"]] == ["desk"] and L["nac_mgmt_grants"]["rows"] == []
      and [p["permission_key"] for p in L["entity_permissions"]["rows"]] == ["tool.x"]
      and [k["pubkey"] for k in L["backup_user_key"]["rows"]] == [K2])
secrets17 = {x["id"]: x["secret"] for x in L["kernel_secrets"]["rows"]}
check("this Host's second factor and signing identity are the live ones, not the lost Host's",
      secrets17["nac_stepup_totp"] == b"42-PHONE" and secrets17["entity_sign"] == b"42-SIGN")
check("foreign-key checks are held only while the carry rewrites, and back on before it commits",
      lost17.replica is False)
before = [dict(r) for r in lost17.t["entity_registry"]["rows"]]
asyncio.run(hr.carry_rows(live42, lost17, 17, 42))
check("carried twice (a boot killed after the commit), this Host's own row is kept",
      lost17.t["entity_registry"]["rows"] == before, lost17.t["entity_registry"]["rows"])
same = FakeDB({"kernel_secrets": dict(SECRETS, rows=[{"id": "entity_sign", "secret": b"OLD-SIGN"}]),
               "host_backup_state": dict(STATE, rows=[])})
asyncio.run(hr.carry_rows(FakeDB({"kernel_secrets": dict(SECRETS, rows=[{"id": "entity_sign", "secret": b"LIVE"}]),
                                  "host_backup_state": dict(STATE, rows=[])}), same, 42, 42))
check("a backup of this same Host keeps its own signing identity (nothing is renumbered)",
      same.t["kernel_secrets"]["rows"][0]["secret"] == b"OLD-SIGN")

plan = asyncio.run(hr.new_plan(None, 42, "20260927T020000Z", {"where": "platform", "taken_at": "t"},
                               {"entity_id": 17, "files": 1}))
check("a staged plan records the backup's Host number and this Host's",
      plan["backup_entity_id"] == 17 and plan["live_entity_id"] == 42, plan)
seen_args = []
_real_carry_rows = hr.carry_rows


async def _rec_carry(src, dst, *ids, **kw):
    seen_args.append(ids)
    return {}
hr.carry_rows = _rec_carry
_real_connect0 = hr._connect


async def _any(db):
    return FakeDB({})
hr._connect = _any
FakeDB.close = lambda self: asyncio.sleep(0)
hr._write_plan({"state": hr.SWAPPED, "live_db": "hostmem", "backup_entity_id": 17, "live_entity_id": 42})
hr.carry("hostmem")
check("the boot's carry renumbers from the backup's Host to this one", seen_args == [(17, 42)], seen_args)
hr.carry_rows, hr._connect = _real_carry_rows, _real_connect0
hr.PLAN.unlink()

print("== the Browser app's signed-in profile follows the live switch")
check("bastion self-update and the browser-profile switch are carried",
      "bastion_autoupdate_settings" in hr.CARRIED_TABLES and "browser_profile_enabled" in hr.CARRIED_SECRETS)
BENV = {"cols": ["id", "owner_identity", "logical_name", "superseded_at"]}
for live_on in (False, True):
    src = FakeDB({"kernel_secrets": dict(SECRETS, rows=[{"id": "browser_profile_enabled", "secret": b"1"}] if live_on else []),
                  "host_backup_state": dict(STATE, rows=[])})
    dst = FakeDB({"kernel_secrets": dict(SECRETS, rows=[{"id": "browser_profile_enabled", "secret": b"1"}]),
                  "host_backup_state": dict(STATE, rows=[]),
                  "vault_envelope": dict(BENV, rows=[{"id": "p", "owner_identity": "host:42",
                                                      "logical_name": "browser:profile", "superseded_at": None}])})
    asyncio.run(hr.carry_rows(src, dst))
    kept = dst.t["vault_envelope"]["rows"][0]["superseded_at"] is None
    on = any(x["id"] == "browser_profile_enabled" for x in dst.t["kernel_secrets"]["rows"])
    check(f"switched {'on' if live_on else 'off'} now: the switch is as it is now, and the sealed "
          f"profile is {'kept' if live_on else 'not brought back'}", on == live_on and kept == live_on, (on, kept))

print("== the backup-auth keys across a restore")
K_LIVE, K_OLD = os.urandom(32), os.urandom(32)
for label, restored_rows, want in (
        ("a backup of another Host: its key stays this Host's, and the key held until the swap is kept "
         "beside it, so the backup taken just before still verifies",
         [{"id": "backup_auth", "secret": K_OLD}],
         {"backup_auth": K_OLD, f"backup_auth:{hb.auth_kid(K_LIVE)}": K_LIVE}),
        ("a backup of this Host (same key): nothing is added", [{"id": "backup_auth", "secret": K_LIVE}],
         {"backup_auth": K_LIVE}),
        ("a restored database with no key takes the live one", [], {"backup_auth": K_LIVE})):
    src = FakeDB({"kernel_secrets": dict(SECRETS, rows=[{"id": "backup_auth", "secret": K_LIVE}]),
                  "host_backup_state": dict(STATE, rows=[])})
    dst = FakeDB({"kernel_secrets": dict(SECRETS, rows=restored_rows),
                  "host_backup_state": dict(STATE, rows=[])})
    asyncio.run(hr.carry_rows(src, dst))
    got_keys = {r["id"]: r["secret"] for r in dst.t["kernel_secrets"]["rows"] if r["id"].startswith("backup_auth")}
    check(label, got_keys == want, sorted(got_keys))
# An UNVERIFIED restore: the backup's keys came from a
# file nobody could vouch for, so they are dropped and this Host's stay.
K_KEPT = os.urandom(32)
for label, live_rows, restored_rows, want in (
        ("unverified: the backup's key does not become the one new backups are tagged with; the live "
         "one stays, and the backup's is not kept as one to verify with either",
         [{"id": "backup_auth", "secret": K_LIVE}],
         [{"id": "backup_auth", "secret": K_OLD}, {"id": f"backup_auth:{hb.auth_kid(K_KEPT)}", "secret": K_KEPT}],
         {"backup_auth": K_LIVE}),
        ("unverified: the keys this Host kept from before an earlier restore come along",
         [{"id": "backup_auth", "secret": K_LIVE}, {"id": f"backup_auth:{hb.auth_kid(K_KEPT)}", "secret": K_KEPT}],
         [{"id": "backup_auth", "secret": K_OLD}],
         {"backup_auth": K_LIVE, f"backup_auth:{hb.auth_kid(K_KEPT)}": K_KEPT}),
        ("unverified, and this Host never made a backup: no key at all, so the next backup makes a new one",
         [], [{"id": "backup_auth", "secret": K_OLD}], {})):
    src = FakeDB({"kernel_secrets": dict(SECRETS, rows=live_rows + [{"id": "app_token", "secret": b"T"}]),
                  "host_backup_state": dict(STATE, rows=[])})
    dst = FakeDB({"kernel_secrets": dict(SECRETS, rows=restored_rows), "host_backup_state": dict(STATE, rows=[])})
    done = asyncio.run(hr.carry_rows(src, dst, verified=False))
    got_keys = {r["id"]: r["secret"] for r in dst.t["kernel_secrets"]["rows"] if r["id"].startswith("backup_auth")}
    check(label, got_keys == want and done.get("backup_auth") == (hb.auth_kid(K_LIVE) if live_rows else None),
          (sorted(got_keys), done.get("backup_auth")))
    first_unverified = globals().get("first_unverified") or dst


class _RestoredPool:
    """host_backup.auth_key's one read, answered from the restored database."""

    def __init__(self, db):
        self.db = db

    async def fetchval(self, sql, *a):
        return next((r["secret"] for r in self.db.t["kernel_secrets"]["rows"] if r["id"] == a[0]), None)


check("  and the key the next backup is tagged with (host_backup.auth_key, on the restored database) "
      "is the live one", asyncio.run(hb.auth_key(_RestoredPool(first_unverified))) == K_LIVE)

print("== an unverified restore keeps this Host's signing identity, the one its trust store holds")
# A crafted backup of this same Host number carries an entity_sign of its own,
# while the trust store (application_signing_keys, carried) is the live Host's.
# Kept, every project this Host builds is signed with a key it does not trust:
# each build, the restore's own reinstall of a Host-built app among them, is
# refused at install's first gate. The build below is the real one
# (entity_signing.sign_with_entity_key, then trust_store.list_active_keys and
# bundles.verify_any, as installer.install_bundle checks a bundle).
from server.applications import bundles as _bundles  # noqa: E402
from server.applications import entity_signing as _es  # noqa: E402
from server.applications import trust_store as _ts  # noqa: E402
from server.applications.tokens import _SECRET_SCHEMA  # noqa: E402

SIGNKEYS = {"cols": ["fingerprint", "public_key", "publisher", "provenance_allowed", "registered_at", "revoked_at"]}
LIVE_PEM, LIVE_PUB = _es._generate_keypair()
CRAFTED_PEM, CRAFTED_PUB = _es._generate_keypair()


def _trusted(pub):
    return {"fingerprint": _ts._fingerprint(pub), "public_key": pub, "publisher": "Entity-42",
            "provenance_allowed": ["custom"], "registered_at": T0, "revoked_at": None}


class _BuildPool:
    """What a project build reads, answered from the restored database."""

    def __init__(self, db):
        self.db = db

    def acquire(self):
        db = self.db

        class C:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

            async def execute(s, q, *a):          # the kernel_secrets schema step, only
                assert q == _SECRET_SCHEMA, q[:80]
                return "OK"

            async def fetchrow(s, q, *a):
                assert "FROM kernel_secrets WHERE id" in q, q
                rows = [r for r in db.t["kernel_secrets"]["rows"] if r["id"] == a[0]]
                return {"secret": rows[0]["secret"]} if rows else None

            async def fetch(s, q, *a):
                assert "FROM application_signing_keys" in q, q
                return [r for r in db.t["application_signing_keys"]["rows"] if r["revoked_at"] is None]
        return C()


async def _project_build_verifies(db) -> bool:
    pool = _BuildPool(db)
    unsigned = _bundles._build_zip(b"id: board\nname: Board\nprovenance: custom\n",
                                   {"index.html": b"<p>board</p>"}, {}, signature=None)
    signed = await _es.sign_with_entity_key(pool, 42, unsigned)
    keys = await _ts.list_active_keys(pool)
    try:
        _bundles.verify_any(signed, trusted_keys=[k.public_key for k in keys])
    except _bundles.BundleError:
        return False
    return True


for verified in (False, True):
    src = FakeDB({"kernel_secrets": dict(SECRETS, rows=[{"id": "entity_sign", "secret": LIVE_PEM}]),
                  "application_signing_keys": dict(SIGNKEYS, rows=[_trusted(LIVE_PUB)]),
                  "host_backup_state": dict(STATE, rows=[])})
    dst = FakeDB({"kernel_secrets": dict(SECRETS, rows=[{"id": "entity_sign", "secret": CRAFTED_PEM}]),
                  "application_signing_keys": dict(SIGNKEYS, rows=[_trusted(CRAFTED_PUB)]),
                  "host_backup_state": dict(STATE, rows=[])})
    asyncio.run(hr.carry_rows(src, dst, 42, 42, verified=verified))
    sign = [r["secret"] for r in dst.t["kernel_secrets"]["rows"] if r["id"] == "entity_sign"]
    if not verified:
        check("unverified: the signing identity is the live one, never the backup's",
              sign == [LIVE_PEM], [s[:40] for s in sign])
        check("  so a project built after the restore passes install's signature check",
              asyncio.run(_project_build_verifies(dst)))
    else:
        check("verified: the signing identity stays the backup's, as before", sign == [CRAFTED_PEM],
              [s[:40] for s in sign])
src = FakeDB({"kernel_secrets": dict(SECRETS, rows=[]), "host_backup_state": dict(STATE, rows=[])})
dst = FakeDB({"kernel_secrets": dict(SECRETS, rows=[{"id": "entity_sign", "secret": CRAFTED_PEM}]),
              "host_backup_state": dict(STATE, rows=[])})
asyncio.run(hr.carry_rows(src, dst, verified=False))
check("unverified, and this Host never signed anything: the backup's key is dropped, so the first "
      "build makes this Host's own",
      not [r for r in dst.t["kernel_secrets"]["rows"] if r["id"] == "entity_sign"], dst.t["kernel_secrets"]["rows"])
seen_kw = []


async def _rec_kw(src, dst, *ids, **kw):
    seen_kw.append(kw.get("verified"))
    return {}
_r1, _r2 = hr.carry_rows, hr._connect
hr.carry_rows, hr._connect = _rec_kw, _any
for v in (False, True, None):
    plan0 = {"state": hr.SWAPPED, "live_db": "hostmem"}
    if v is not None:
        plan0["verified"] = v
    hr._write_plan(plan0)
    hr.carry("hostmem")
    hr.PLAN.unlink()
check("the boot's carry passes the plan's verdict: unverified, verified, and no verdict read as unverified",
      seen_kw == [False, True, False], seen_kw)
hr.carry_rows, hr._connect = _r1, _r2

print("== the carry is recorded in the same transaction")
m = FakeDB({"host_backup_state": dict(STATE, rows=[])})
asyncio.run(hr.carry_rows(FakeDB({"host_backup_state": dict(STATE, rows=[])}), m, stamp="20260927T020000Z"))
check("the restored database records that it was carried for this restore",
      asyncio.run(hr.carried_already(m, "20260927T020000Z")) and not asyncio.run(hr.carried_already(m, "20200101T000000Z")))
calls2 = []


async def _marked(db):
    calls2.append(db)
    return m if db == "hostmem" else FakeDB({})
_rc = hr._connect
hr._connect = _marked
_rcr = hr.carry_rows


async def _must_not(*a, **k):
    raise AssertionError("carried twice")
hr.carry_rows = _must_not
hr._write_plan({"state": hr.SWAPPED, "live_db": "hostmem", "stamp": "20260927T020000Z"})
check("a boot that finds it already carried (plan not yet updated) does not carry again",
      hr.carry("hostmem") == 0 and hr.read_plan().get("carried") is True)
hr._connect, hr.carry_rows = _rc, _rcr
hr.PLAN.unlink()

print("== the carry runs once, before anything serves")
calls = []


async def fake_connect(db):
    calls.append(db)
    return {"hostmem_before_restore": live_db, "hostmem": FakeDB({})}[db]
_real_connect = hr._connect
hr._connect = fake_connect
for db in (live_db,):
    db.close = lambda: asyncio.sleep(0)


async def _close():
    return None
live_db.close = _close
FakeDB.close = lambda self: _close()
hr._write_plan({"state": hr.SWAPPED, "live_db": "hostmem"})
rc1 = hr.carry("hostmem")
rc2 = hr.carry("hostmem")
check("carried from the database live until the swap into the restored one, then never again",
      rc1 == 0 and rc2 == 0 and calls == ["hostmem_before_restore", "hostmem"]
      and hr.read_plan().get("carried") is True, (calls, hr.read_plan()))
hr._write_plan({"state": hr.STAGED_STATE})
calls.clear()
check("nothing is carried before the swap", hr.carry("hostmem") == 0 and calls == [])


async def broken_connect(db):
    raise OSError("the database is not there")
hr._connect = broken_connect
hr._write_plan({"state": hr.SWAPPED, "live_db": "hostmem"})
check("a carry that fails stops the boot (so the restore is undone)", hr.carry("hostmem") == 1
      and not hr.read_plan().get("carried"))
hr._connect = _real_connect
hr.PLAN.unlink()
src_ep = (Path(ROOT) / "entrypoint.sh").read_text()
check("entrypoint.sh carries after the schema step and before the server starts",
      src_ep.index("python3 -m server.host_restore carry") > src_ep.index("Applying migration")
      and src_ep.index("python3 -m server.host_restore carry") < src_ep.index("exec python3 -m server.launch"))

print("== the integrations store's grants")
# integration_grants (v201): which app may use which connection. A restore
# brings back the backup's connections and keeps the grants that are live now.
ICONN = {"cols": ["id", "provider_key", "user_id", "status", "credentials_ref", "connection_config",
                  "created_at", "updated_at", "last_sync_at", "last_error"]}
IGRANT = {"cols": ["connection_id", "app_id", "methods", "granted_at"]}
ISYNC = {"cols": ["connection_id", "resource", "cursor", "mode", "last_run_at", "next_run_at", "last_error"]}
IREC = {"cols": ["id", "connection_id", "resource", "canonical_model", "data", "raw_json",
                 "received_at", "created_at"]}
ICLI = {"cols": ["family", "credentials_ref", "label", "created_at", "updated_at"]}


def _integ_db(conns, grants, with_tables=True):
    if not with_tables:
        return FakeDB({})
    return FakeDB({
        "integration_connections": dict(ICONN, rows=[{"id": c} for c in conns]),
        "integration_grants": dict(IGRANT, rows=[{"connection_id": c, "app_id": a, "methods": m,
                                                  "granted_at": "t"} for c, a, m in grants]),
        "integration_syncs": dict(ISYNC, rows=[]), "integration_records": dict(IREC, rows=[]),
        "integration_oauth_clients": dict(ICLI, rows=[])})


def _grants(db):
    return sorted((g["connection_id"], g["app_id"], g["methods"])
                  for g in db.t["integration_grants"]["rows"])


def _old_store(base, conns, grants):
    """The SQLite file the store was until v201, on an apps tree."""
    (base / "app-state/integrations").mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(base / hr.INTEGRATIONS_LEGACY_REL)) as c:
        c.execute("CREATE TABLE connections (id TEXT PRIMARY KEY, provider_key TEXT, user_id TEXT, "
                  "status TEXT, credentials_ref TEXT, connection_config TEXT, created_at TEXT, "
                  "updated_at TEXT, last_sync_at TEXT, last_error TEXT)")
        c.execute("CREATE TABLE integration_grants (connection_id TEXT, app_id TEXT, methods TEXT, granted_at TEXT)")
        c.executemany("INSERT INTO connections (id, provider_key, user_id, status, connection_config, "
                      "created_at, updated_at) VALUES (?, 'gmail', '3', 'active', '{}', 't', 't')",
                      [(x,) for x in conns])
        c.executemany("INSERT INTO integration_grants VALUES (?,?,?,'t')", grants)
        c.commit()


live_root, prev = tmp / "carry-live", tmp / "carry-prev"
live_root.mkdir(parents=True, exist_ok=True)
prev.mkdir(parents=True, exist_ok=True)
# Restored: the backup's connection c1 and its grants then. Live: grants on c1 and c2.
dst = _integ_db(["c1"], [("c1", "mail", '["GET","POST"]'), ("c1", "old-app", '["GET"]')])
src = _integ_db(["c1", "c2"], [("c1", "mail", '["GET"]'), ("c2", "cal", '["GET"]')])
r = asyncio.run(hr._carry_integration_grants(src, dst, prev))
check("which app may use which connection is as it was live: a grant withdrawn since stays withdrawn, "
      "and one for a connection the backup does not have is dropped",
      _grants(dst) == [("c1", "mail", '["GET"]')] and r.get("integration_grants") == 1, (_grants(dst), r))
check("the restored connections themselves stay as the backup had them",
      [c["id"] for c in dst.t["integration_connections"]["rows"]] == ["c1"])

# A backup from before v201: its store is the SQLite file the swap brought back.
_old_store(live_root, ["c1", "c3"], [("c1", "old-app", '["GET"]'), ("c3", "mail", None)])
dst = _integ_db([], [])
src = _integ_db(["c3"], [("c3", "mail", '["GET"]'), ("c9", "gone", None)])
r = asyncio.run(hr._import_backup_integrations(dst, live_root))
r.update(asyncio.run(hr._carry_integration_grants(src, dst, prev)))
check("a backup from before v201: its connections are read in from its SQLite file",
      sorted(c["id"] for c in dst.t["integration_connections"]["rows"]) == ["c1", "c3"], dst.t["integration_connections"])
check("... and its grants are not: the live ones are kept, for the connections it has",
      _grants(dst) == [("c3", "mail", '["GET"]')], _grants(dst))

# The database live until the swap is itself from before v201: its grants are
# in the SQLite file the swap moved aside.
_old_store(prev, ["c1"], [("c1", "mail", '["GET"]'), ("c1", "cal", None)])
dst = _integ_db(["c1"], [("c1", "old-app", None)])
r = asyncio.run(hr._carry_integration_grants(_integ_db([], [], with_tables=False), dst, prev))
check("live grants still in the SQLite file the swap moved aside are the ones kept",
      _grants(dst) == [("c1", "cal", None), ("c1", "mail", '["GET"]')], _grants(dst))
check("a restored database without the integrations tables is left alone",
      asyncio.run(hr._carry_integration_grants(src, FakeDB({}), prev)) == {}
      and asyncio.run(hr._import_backup_integrations(FakeDB({}), live_root)) == {})

# carry() removes the backup's SQLite file once the carry committed, so the
# boot's retire_sqlite_store cannot read its grants back in.
_saved_live, _saved_cr, _saved_conn = hr.LIVE, hr.carry_rows, hr._connect


async def _carried(src, dst, *a, **k):
    return {}
hr.LIVE, hr.carry_rows, hr._connect = live_root, _carried, _any
hr._write_plan({"state": hr.SWAPPED, "live_db": "hostmem"})
check("after a carry the backup's integrations SQLite file is gone",
      (live_root / hr.INTEGRATIONS_LEGACY_REL).exists() and hr.carry("hostmem") == 0
      and not (live_root / hr.INTEGRATIONS_LEGACY_REL).exists())
hr.LIVE, hr.carry_rows, hr._connect = _saved_live, _saved_cr, _saved_conn
hr.PLAN.unlink()

hr._write_plan({"state": hr.SWAPPED})
check("no backup runs between the swap and finish (the keys are the restored ones until then)",
      hb.restore_in_progress())
async def _no_runs(pool, limit=10):
    return []
_real_last_runs, _real_conf = hb.last_runs, hb.configured
hb.last_runs, hb.configured = _no_runs, (lambda: None)
from server.routes import backup as _Rr  # noqa: E402
_Rr.brain_service.get_pool = lambda: object()
runs = asyncio.run(_Rr.get_backup_runs())
check("and Settings is told, in a sentence, that backups are paused until the restore finishes",
      "backups are paused" in (runs["problem"] or ""), runs)
check("  and its verdict line says so, reading nothing of the runs mid-swap",
      runs["status"]["state"] == "restoring" and "restore" in runs["status"]["headline"], runs.get("status"))
hb.last_runs, hb.configured = _real_last_runs, _real_conf
hr._write_plan({"state": hr.STAGED_STATE})
check("a restore only staged holds nothing up", not hb.restore_in_progress())
hr.PLAN.unlink()

# ── copies only a bastion holds (a Host that replaced a lost one) ───────────
print("== copies on a bastion")
from server.network import nac_approval  # noqa: E402
check("listing a bastion's backups is a read: no one is asked",
      "backup_list" in nac_approval.READ_OPS)
check("a reseal is approved on the machine and a push is its standing work: the pod rings nobody",
      {"backup_reseal", "backup_push"} <= nac_approval.OWN_APPROVAL_OPS)
import datetime as _dt  # noqa: E402
SHA_A, SHA_B = "a" * 64, "b" * 64


class BPool:
    async def fetch(self, q, *a):
        if "network_bastion_heartbeat" in q:
            return [{"token_id": 5, "hostname": "nas-box", "last_seen_at": _dt.datetime.now(_dt.timezone.utc)},
                    {"token_id": 6, "hostname": "laptop", "last_seen_at": _dt.datetime.now(_dt.timezone.utc)}]
        return []


listing = {5: {"backups": [      # bastion 0.46.0 backup_list, newest first
    {"folder": ".", "stamp": "20260923T020000Z", "data_bytes": 1, "has_data": True, "has_sidecar": True,
     "sidecar_bytes": 300, "data_sha256": SHA_A, "sidecar_sha256": SHA_B},
    {"folder": "shared/host-7", "stamp": "20260922T020000Z", "data_bytes": 10, "has_data": True,
     "has_sidecar": False, "sidecar_bytes": 0, "data_sha256": SHA_A, "sidecar_sha256": ""},
    {"folder": "shared/host-7", "stamp": "20260921T020000Z", "data_bytes": 10, "has_data": True,
     "has_sidecar": True, "sidecar_bytes": 300, "data_sha256": "", "sidecar_sha256": ""},
    {"folder": ".", "stamp": "20260920T020000Z", "data_bytes": 10, "has_data": True, "has_sidecar": True,
     "sidecar_bytes": 300, "data_sha256": SHA_A, "sidecar_sha256": SHA_B},
    {"folder": ".", "stamp": "20260919T020000Z", "data_bytes": 10, "has_data": True, "has_sidecar": True,
     "data_sha256": SHA_A, "sidecar_sha256": SHA_B},
    {"folder": ".", "stamp": "20260918T020000Z", "data_bytes": 0, "has_data": False, "has_sidecar": True,
     "sidecar_bytes": 300, "data_sha256": "", "sidecar_sha256": SHA_B},
    {"folder": "../etc", "stamp": "20260917T020000Z", "data_bytes": 1, "has_data": True, "has_sidecar": True,
     "sidecar_bytes": 1, "data_sha256": SHA_A, "sidecar_sha256": SHA_B},
    {"folder": "/etc", "stamp": "20260916T020000Z", "data_bytes": 1, "has_data": True, "has_sidecar": True,
     "sidecar_bytes": 1, "data_sha256": SHA_A, "sidecar_sha256": SHA_B}]}}
ops = []


async def fake_bastion_op(pool, eid, token_id, op, req):
    ops.append((token_id, op, req))
    if op == "backup_push" and req.get("op") == "start" and req.get("headers") != {"If-None-Match": "*"}:
        # Bastion 0.47.0: the PUT's headers must be the put_url reply's,
        # or storage answers 403 to the PUT.
        raise hr.RestoreError("backup_push: headers must be the put_url reply's headers")
    if op == "backup_list":
        if token_id == 6:
            raise hr.RestoreError("not granted backup_list")
        return listing[token_id]
    return {"job_id": f"j{len(ops)}", "state": "pending"}
hr._bastion_op = fake_bastion_op
held, probs = asyncio.run(hr.bastion_copies(BPool(), 42))
check("a bastion's copies are listed, with its folder and checksums",
      held["20260920T020000Z"]["dir"] == "." and held["20260920T020000Z"]["token_id"] == 5
      and not held["20260920T020000Z"]["hash_pending"])
check("a copy the bastion has not checked yet is offered as not ready",
      held["20260921T020000Z"]["hash_pending"] is True)
check("and so is one whose key file's size it did not say (the upload is signed for a size)",
      held["20260919T020000Z"]["hash_pending"] is True)
check("a copy without its key file, without its data, or in a folder that climbs out, is not offered",
      "20260922T020000Z" not in held and "20260918T020000Z" not in held
      and "20260917T020000Z" not in held, sorted(held))
check("  nor one in an absolute folder", "20260916T020000Z" not in held, sorted(held))
check("a bastion that refuses the listing is named, and the others still count",
      len(probs) == 1 and "laptop" in probs[0], probs)
put_urls = []


async def fake_call(op, params=None):
    put_urls.append((op, params))
    return {"url": f"https://store/{params['kind']}/{params['name']}", "key": "k",
            "headers": {"If-None-Match": "*"}}
_real_call = hb.call
hb.call = fake_call


async def no_copies(pool):
    return {}
hr.machine_copies = no_copies
ops.clear()
st = asyncio.run(hr.request_push(BPool(), 42, "20260920T020000Z", 5))
pushes = [r for _, op, r in ops if op == "backup_push"]
check("bringing it back pushes the backup and its key file from that folder, with their checksums",
      [(p["path"], p["sha256"]) for p in pushes]
      == [("./20260920T020000Z.tar.zst.age", SHA_A),
          ("./20260920T020000Z.key.age", SHA_B)]
      and [(p["kind"], p["size"]) for _, p in put_urls] == [("staged", 10), ("staged_sidecar", 300)]
      and st["state"] == "pending",
      pushes)
check("  and the headers the PUT must carry go to the bastion with each URL (If-None-Match: *)",
      [p.get("headers") for p in pushes] == [{"If-None-Match": "*"}] * 2, pushes)
listing[5]["backups"].append({"folder": ".", "stamp": "20260915T020000Z", "data_bytes": 6 * 1024 ** 3,
                              "has_data": True, "has_sidecar": True, "sidecar_bytes": 300,
                              "data_sha256": SHA_A, "sidecar_sha256": SHA_B})
ops.clear()
try:
    asyncio.run(hr.request_push(BPool(), 42, "20260915T020000Z", 5))
    check("a copy over 5 GiB (one PUT's limit) is not asked for, and the owner is told why", False)
except hr.RestoreError as e:
    check("a copy over 5 GiB (one PUT's limit) is not asked for, and the owner is told why",
          "larger than 5 GiB" in str(e) and not [o for o in ops if o[1] == "backup_push"], str(e))
try:
    asyncio.run(hr.request_push(BPool(), 42, "20260921T020000Z", 5))
    check("a copy not yet checked is not sent", False)
except hr.RestoreError as e:
    check("a copy not yet checked is not sent", "still checking" in str(e), str(e))
try:
    asyncio.run(hr.request_push(BPool(), 42, "20260920T020000Z", 6))
    check("only the bastion that holds it is asked", False)
except hr.RestoreError:
    check("only the bastion that holds it is asked", True)
hb.call = _real_call
from server.routes import backup as _R_limit  # noqa: E402
_big = {"target": {"kind": "machine", "address": "nas"}, "platform": False,
        "ship": {"artifact_bytes": 6 * 1024 ** 3}}
check("Settings says so at backup time when backups go only to a machine and the newest is over 5 GiB",
      "larger than 5 GiB" in (_R_limit._ship_limit_note(_big) or ""))
check("  and not when platform storage keeps a copy, or the backup is smaller",
      _R_limit._ship_limit_note(dict(_big, platform=True)) is None
      and _R_limit._ship_limit_note(dict(_big, ship={"artifact_bytes": 10})) is None)

# ── the routes ──────────────────────────────────────────────────────────────
print("== the routes")
from fastapi import HTTPException  # noqa: E402
from server import brain_service  # noqa: E402
from server.routes import backup as R  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "brain"))
from mastercode.runtime import kernel_ops  # noqa: E402

for path in ("/api/backup/restore", "/api/backup/restore/key", "/api/backup/restore/fetch",
             "/api/backup/restore/sidecar"):
    check(f"{path} is walled from the Host: the user's act alone",
          path in kernel_ops._WALLED_PATHS and kernel_ops.exposed_route("POST", path) is None)
brain_service._entity_id = 42
R.brain_service.get_pool = lambda: object()


async def enrolled(_p):
    return True


codes_tried = []


async def verify(_p, code):
    codes_tried.append(code)
    return code == "123456"


R.nac_stepup.is_enrolled = enrolled
R.nac_stepup.verify_stepup = verify
started = []


async def fake_stage(pool, eid, st, unverified_ok=False):
    started.append((eid, st))
    return {"state": "staged"}


hr.stage = fake_stage
ready = {}


async def _state(pool, key):
    return ready.get(key)
hb._state = _state


def status_of(coro):
    try:
        asyncio.run(coro)
    except HTTPException as e:
        return e.status_code
    return 200


hr.keep_resealed(stamp, resealed)           # a key file with no tag: this Host cannot verify it
check("a backup this Host cannot verify is refused unless the owner says so, before a code is spent",
      status_of(R.start_restore(R.RestoreReq(stepup_code="123456", stamp=stamp))) == 409
      and not started and not codes_tried, codes_tried)
check("a wrong second-factor code is refused",
      status_of(R.start_restore(R.RestoreReq(stepup_code="000000", stamp=stamp, unverified_ok=True))) == 403
      and not started)
check("a stamp that is not a backup's is refused before anything else",
      status_of(R.start_restore(R.RestoreReq(stepup_code="123456", stamp="latest"))) == 400)
hr.drop_resealed()
check("a backup whose key is not sealed to this Host yet is refused",
      status_of(R.start_restore(R.RestoreReq(stepup_code="123456", stamp=stamp, unverified_ok=True))) == 409
      and not started)
hr.keep_resealed(stamp, resealed)
hr._write_plan({"state": hr.STAGED_STATE})
check("a restore already waiting is not replaced",
      status_of(R.start_restore(R.RestoreReq(stepup_code="123456", stamp=stamp, unverified_ok=True))) == 409
      and not started)
hr.PLAN.unlink()


async def go():
    res = await R.start_restore(R.RestoreReq(stepup_code="123456", stamp=stamp, unverified_ok=True))
    await asyncio.sleep(0)
    return res


res = asyncio.run(go())
check("the right code, with the key ready, starts staging in the background",
      res == {"started": True} and started == [(42, stamp)], (res, started))
check("asking a bastion for the key needs the second factor",
      status_of(R.restore_key(R.RestoreKeyReq(stepup_code="000000", stamp=stamp, via="bastion",
                                              token_id=3))) == 403)
check("and says which bastion",
      status_of(R.restore_key(R.RestoreKeyReq(stepup_code="123456", stamp=stamp, via="bastion"))) == 400)
check("a pasted key file must be a whole age file",
      status_of(R.restore_key(R.RestoreKeyReq(stepup_code="123456", stamp=stamp, via="paste",
                                              sidecar="AGE-SECRET-KEY-1ABC"))) == 400)
got_sidecar = []


async def fake_sidecar(pool, eid, st):
    got_sidecar.append(st)
    return {"stamp": st, "entity_id": eid, "host_recipient": host_rec, "sidecar": "-----BEGIN AGE"}
hr.sidecar_for_browser = fake_sidecar
out = asyncio.run(R.restore_sidecar(stamp))
check("the browser gets the sealed key file, this Host's recipient and the ids to check, and no key",
      out["host_recipient"] == host_rec and out["entity_id"] == 42 and got_sidecar == [stamp]
      and "AGE-SECRET-KEY" not in json.dumps(out))
check("a sealed key file for a made-up stamp is refused", status_of(R.restore_sidecar("latest")) == 400)
check("an unknown way to get the key is refused",
      status_of(R.restore_key(R.RestoreKeyReq(stepup_code="123456", stamp=stamp, via="platform"))) == 400)

print("== the Host is told what the restore kept of the owner's sign-in")
import mastercode.context_store as _cs  # noqa: E402
sign_in_written = []


class _Context:
    """The Host's own message list, read into its next turn (runtime/session.py)."""

    def __init__(self, pool_, eid):
        self.eid = eid

    async def append_to_list(self, path, item, max_items=100):
        sign_in_written.append((self.eid, path, item))
_real_ctx = _cs.ContextStore
_cs.ContextStore = _Context
try:
    if getattr(hr, "_tell_host_sign_in", None):
        asyncio.run(hr._tell_host_sign_in(object(), 42, SIGN_IN_NOTE))
finally:
    _cs.ContextStore = _real_ctx
sign_in_msg = sign_in_written[-1][2] if sign_in_written else {}
check("a message in the Host's own list, unread, for its next turn, carrying what the restore said",
      len(sign_in_written) == 1 and sign_in_written[0][:2] == (42, "inbox/messages.json")
      and sign_in_msg.get("read") is False and sign_in_msg.get("source") == "backups"
      and SIGN_IN_NOTE in sign_in_msg.get("content", "") and "Tell the owner" in sign_in_msg.get("content", ""),
      sign_in_written)

print("== a backup's Git directories run nothing (host_restore.defuse_git)")
# A crafted backup: a project whose .git/config has a clean filter (git add
# and git status run it), a signing program, a work tree elsewhere, an include
# and a hook; a project folder shaped as a bare repository with its own
# filter; a .git whose commondir sends git to another folder's config; and a
# sha256 repository. Real git, run the way the Host runs it in a project
# (routes/project_git.py HARDENED_GIT_CONFIG).
from server.routes.project_git import HARDENED_GIT_CONFIG  # noqa: E402
_marker = tmp / "planted-program-ran"
_evil = (f'[filter "x"]\n\tclean = "touch {_marker}; cat"\n\tsmudge = "touch {_marker}; cat"\n'
         f'[core]\n\tworktree = {tmp}\n\tsshCommand = touch {_marker}\n'
         f'[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /bin/false\n'
         f'[include]\n\tpath = {tmp}/elsewhere.cfg\n').encode()


def _repo(base, gitdir="", config=_evil):
    g = f"{base}/{gitdir}" if gitdir else base
    return [(f"{g}", None, "dir"), (f"{g}/HEAD", b"ref: refs/heads/main\n", "file"),
            (f"{g}/objects", None, "dir"), (f"{g}/objects/info", None, "dir"),
            (f"{g}/refs", None, "dir"), (f"{g}/refs/heads", None, "dir"),
            (f"{g}/config", config, "file")]


GIT_EVIL = (DB + [("apps/projects", None, "dir"), ("apps/projects/1", None, "dir")]
            + _repo("apps/projects/1", ".git")
            + [("apps/projects/1/.git/hooks", None, "dir"),
               ("apps/projects/1/.git/hooks/pre-commit", f"#!/bin/sh\ntouch {_marker}\n".encode(), "file"),
               ("apps/projects/1/.git/objects/info/alternates", b"/etc\n", "file"),
               ("apps/projects/1/.gitattributes", b"* filter=x diff=x\n", "file"),
               ("apps/projects/1/a.txt", b"hello\n", "file"),
               ("apps/projects/2", None, "dir")]
            + [m for m in _repo("apps/projects/2") if m[0] != "apps/projects/2"]
            + [("apps/projects/2/.gitattributes", b"* filter=x\n", "file"),
               ("apps/projects/3", None, "dir"), ("apps/projects/3/.git", None, "dir"),
               ("apps/projects/3/.git/HEAD", b"ref: refs/heads/main\n", "file"),
               ("apps/projects/3/.git/commondir", b"../../4\n", "file"),
               ("apps/projects/3/.gitattributes", b"* filter=x\n", "file"),
               ("apps/projects/3/b.txt", b"b\n", "file"),
               ("apps/projects/4", None, "dir"), ("apps/projects/4/config", _evil, "file"),
               ("apps/projects/4/objects", None, "dir"), ("apps/projects/4/refs", None, "dir"),
               ("apps/projects/5", None, "dir")]
            + _repo("apps/projects/5", ".git", b'[core]\n\trepositoryformatversion = 1\n'
                    b'[extensions]\n\tobjectformat = sha256\n' + _evil))
out, dest = unpack(backup_file(GIT_EVIL))
check("the crafted backup unpacks", isinstance(out, dict), out)
check("every Git directory is named as reset, and nothing else",
      isinstance(out, dict) and out.get("git_dirs_reset") == ["projects/1/.git", "projects/2",
                                                               "projects/3/.git", "projects/5/.git"],
      out.get("git_dirs_reset") if isinstance(out, dict) else out)
_cfg1 = (dest / "projects/1/.git/config").read_text() if (dest / "projects/1/.git/config").exists() else ""
check("a project's .git/config keeps only its format",
      _cfg1 == "[core]\n\trepositoryformatversion = 0\n\tbare = false\n", _cfg1)
check("...its hooks, alternates and commondir are gone",
      not any((dest / "projects/1/.git/hooks").iterdir())
      and not (dest / "projects/1/.git/objects/info/alternates").exists()
      and not (dest / "projects/3/.git/commondir").exists())
check("a folder shaped as a bare repository is said to be bare",
      "bare = true" in (dest / "projects/2/config").read_text())
check("a sha256 repository stays sha256",
      "objectformat = sha256" in (dest / "projects/5/.git/config").read_text()
      and "filter" not in (dest / "projects/5/.git/config").read_text())
_genv = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
         "GIT_TERMINAL_PROMPT": "0", "GIT_CEILING_DIRECTORIES": str(tmp)}
_ran = []
for _proj, _args in (("projects/1", ("add", "-A")), ("projects/1", ("status", "--porcelain")),
                     ("projects/1", ("-c", "user.name=t", "-c", "user.email=t@example.test",
                                     "commit", "-qm", "x")),
                     ("projects/1", ("diff", "--cached", "--no-ext-diff", "--no-textconv")),
                     ("projects/2", ("status",)), ("projects/3", ("add", "-A")),
                     ("projects/5", ("status",))):
    _r = subprocess.run(["git", *HARDENED_GIT_CONFIG, "-C", str(dest / _proj), *_args],
                        env=_genv, capture_output=True, text=True, timeout=60)
    _ran.append((_proj, _args[-1], _r.returncode, _r.stderr.strip()[-120:]))
check("git add, status, commit and diff in the restored projects ran no planted program",
      not _marker.exists(), _ran)
check("...and the project's own repository still works: its file was committed",
      subprocess.run(["git", *HARDENED_GIT_CONFIG, "-C", str(dest / "projects/1"), "ls-files"],
                     env=_genv, capture_output=True, text=True).stdout.split() == [".gitattributes", "a.txt"],
      _ran)

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: host restore gates pass")
