#!/usr/bin/env python3
"""A backup holds every app's own data, and a restore puts it back
(server/host_backup.py _add_app_data, server/host_restore.py unpack and
_restore_app_data, vm/runtime cseries_runtime/server.py export_app_data and
import_app_data).

An app's data lives in its kept data volume on the machine, outside the
Host's container, so the local runtime moves it with podman's own volume
export and import, over its socket:

  * a backup's file holds app-data/<app>.tar pieces for every installed app
    and for a removed one whose data the owner kept (the App Store's kept-data
    list), each with its sha256 in the manifest, and the README says so;
  * a restore stages each app's data, checked against the manifest, and once
    the Host has started on the restored database the runtime replaces the
    app's volume with it, its files owned by this machine's ids for the app
    (a fresh machine gives the app another range), no file left owned by an
    id outside it, no set-user bit kept;
  * until then the app is not woken (it would start on the data it is about
    to lose); after, it is;
  * an app whose data could not be read when the backup was made makes the
    run partial, saying whose and why, and is not put back, saying so;
  * the disk: an import that would leave the machine under the runtime's
    wake floor is refused before anything is touched, the existing data
    intact; one the machine guard stops (the stop floor) is answered 507; a
    Host that sends more than it declared is refused; a folder backup keeps
    the wake floor free (test_backup_stores.py).

The real runtime behind a real unix socket, over the fake podman (whose
volume export/import are tar, as podman's); real age; a stand-in pg_dump and
pg_restore. Ownership is checked for real when this runs as root, and through
a recorder otherwise.

    python3 scripts/test_backup_app_data.py
"""
import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("APP_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "vm/runtime/lib"))
sys.path.insert(0, str(HERE))
for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    os.environ.pop(v, None)

fake_main = types.ModuleType("server.main")
fake_main.get_bridge_client = lambda: None
fake_main._ws_connections = set()
sys.modules["server.main"] = fake_main

import gate_runner_limits as limits  # noqa: E402
import runtime_fake_podman as fake  # noqa: E402
from cseries_runtime import argv as A  # noqa: E402
from cseries_runtime import common as C  # noqa: E402
from cseries_runtime import guard as G  # noqa: E402
from cseries_runtime import server as S  # noqa: E402
from cseries_runtime.podman import Podman  # noqa: E402
from server import age_keys  # noqa: E402
from server import host_backup as hb  # noqa: E402
from server import host_restore as hr  # noqa: E402
from server.applications import local_runtime as LR  # noqa: E402

FAILED = []
# Root that can give files the app ids (not root in a user namespace
# that maps only its own ids, as under the CI runner's rootlesskit).
ROOTED = limits.owns_app_ids(C.UID_BASE, C.MAX_SLOTS * C.UID_RANGE)


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:500]}]"))
    if not cond:
        FAILED.append(label)


if not Path(hb.AGE).is_file():
    hb.AGE = shutil.which("age") or hb.AGE
if not Path(hb.AGE).is_file():
    print("SKIP-LOUD: no age binary here; this gate needs a real one")
    sys.exit(2)

PY = "ghcr.io/privatae/cseries-app-runtime-python@sha256:" + "1" * 64
# Each machine binds <tmp>/<name>/runtime.sock; "second" is the longest name.
tmp = limits.socket_tmp("bk-appdata-", "second/runtime.sock")


def own_cgroup():
    for line in open("/proc/self/cgroup", encoding="ascii"):
        if line.startswith("0::"):
            return line[3:].strip()
    return ""


def desc(app):
    return {"app_id": app, "enabled": True, "descriptor": {
        "budgetMi": 128, "network": ["api.example.test"], "version": "1.0.0", "form": "process",
        "image": "python", "command": ["python3", "main.py"], "sourceToken": "ab" * 32}}


class Machine:
    """One machine: its runtime state, its fake podman's state and volumes,
    and the runtime serving its socket."""

    def __init__(self, name):
        self.dir = tmp / name
        self.paths = C.Paths(str(self.dir))
        os.makedirs(self.paths.etc)
        with open(self.paths.images_json(), "w") as f:
            json.dump({"images": {"python": PY}}, f)
        self.state = str(self.dir / "podman.json")
        st = fake.state(self.state)
        st["images"] = {PY: "a" * 64}
        fake.write_state(self.state, st)
        self.exe = fake.install(str(self.dir))
        self.sock = str(self.dir / "runtime.sock")
        self.rt = None
        self.server = None

    async def start(self):
        os.environ["FAKE_PODMAN_STATE"] = self.state

        async def ready(*a):
            return True
        self.rt = S.Runtime(self.paths, Podman(self.exe), G.Guard(total_mb=16384, disk_path=str(self.dir), held=lambda: 0, psi_path=os.devnull),
                            http_probe=ready, tcp_probe=ready,
                            chown=(os.chown if ROOTED else (lambda *a: None)), machine="x86_64",
                            peer_uid=os.getuid(), peer_cgroups=(own_cgroup().rstrip("/") + "/",))
        self.server = await self.rt.serve(S.listening_socket(self.sock))
        os.environ[LR.ENV_SOCKET] = self.sock
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def volumes(self):
        return fake.state(self.state)["volumes"]

    def mount(self, app):
        return Path(self.volumes()[C.data_volume(app)]["mount"])


def tree(root):
    """{relative path: (kind, bytes or link target, mode bits)} of a tree."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in sorted(dirnames + filenames):
            p = Path(dirpath) / n
            rel = str(p.relative_to(root))
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p), 0)
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", b"", stat.S_IMODE(st.st_mode))
            else:
                out[rel] = ("file", p.read_bytes(), stat.S_IMODE(st.st_mode) & 0o777)
    return out


def owners(root):
    ids = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for n in [dirpath] + [os.path.join(dirpath, x) for x in dirnames + filenames]:
            st = os.lstat(n)
            ids.add(st.st_uid)
            ids.add(st.st_gid)
    return ids


# ── the backup writer and the restore, pointed at this test's folders ───────
dump = tmp / "pg_dump"
dump.write_text('#!/bin/sh\nexec python3 -c "import sys; sys.stdout.buffer.write(b\'PGDMP\' + bytes(range(256)) * 8)"\n')
dump.chmod(0o755)
hb.PG_DUMP = str(dump)
apps_root = tmp / "host-apps"
(apps_root / "app-state").mkdir(parents=True)
(apps_root / "app-state/notes.txt").write_text("the Host's own file\n")
hb.APPS_ROOT = str(apps_root)
hb.APPS_EXCLUDES = tuple(p.replace("/var/lib/cseries", str(apps_root)) for p in hb.APPS_EXCLUDES)
hb.DB_CHUNK = 64 * 1024        # small, so an app's data is really cut into pieces
live = tmp / "restored-host"
live.mkdir()
hr.LIVE = live
hr.RESTORE_DIR = live / ".host-restore"
hr.STAGED = hr.RESTORE_DIR / "apps"
hr.APP_DATA = hr.RESTORE_DIR / "app-data"
hr.PREVIOUS = hr.RESTORE_DIR / "previous"
hr.PLAN = hr.RESTORE_DIR / "plan.json"
hr.MANIFEST = hr.RESTORE_DIR / "manifest"
hr._room = lambda path: hr.Room(1 << 40, 4096, 0, 0, 0)
hr._load_database = lambda spool: None
ident, rec = age_keys.new_pair()


class Upload:
    def __init__(self):
        self.buf = bytearray()

    def part(self, data):
        self.buf += bytes(data)


def write_backup(listing):
    up = Upload()
    out = hb.write_file(upload=up, recips=[rec], eid=9101, stamp="20261020T030000Z", kind=hb.KIND_BACKUP,
                        dbname="hostmem", git_dir=None, app_data=listing)
    return bytes(up.buf), out


def decrypt(blob):
    from compression import zstd
    argv, fd = hb.age_decrypt_args(ident)
    try:
        plain = subprocess.run(argv, input=blob, capture_output=True, pass_fds=(fd,), check=True).stdout
    finally:
        os.close(fd)
    return zstd.decompress(plain)


def members(blob):
    t = tarfile.open(fileobj=io.BytesIO(decrypt(blob)), mode="r:")
    return {m.name: (t.extractfile(m).read() if m.isfile() else None) for m in t.getmembers()}


def fill(m, app, files, uid=None):
    """Write an app's data into its volume, owned by `uid` (its container
    uid's machine id) when that can be set here."""
    root = m.mount(app)
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "current").symlink_to("db.sqlite")
    if ROOTED and uid is not None:
        for dirpath, dirnames, filenames in os.walk(root):
            for n in [dirpath] + [os.path.join(dirpath, x) for x in dirnames + filenames]:
                os.lchown(n, uid, uid)


def _try(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — the check reads what it was
        return e


async def _stop(rt):
    rt.stop_writes()


def _send_more(path, app, declared, data):
    """An import that declares `declared` bytes and sends `data`, longer, the
    way a Host that does not keep to its word would: the runtime's answer."""
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(path)
    f = s.makefile("rb")
    try:
        s.sendall(json.dumps({"op": "workspace.import_app_data",
                              "params": {"app_id": app, "bytes": declared}}).encode() + b"\n")
        first = json.loads(f.readline())
        if not first.get("ok"):
            return first
        try:
            s.sendall(len(data).to_bytes(4, "big") + data + b"\0\0\0\0")
        except OSError:
            pass
        return json.loads(f.readline())
    finally:
        f.close()
        s.close()


async def _raw(sock, msg):
    r, w = await asyncio.open_unix_connection(sock)
    w.write(json.dumps(msg).encode() + b"\n")
    await w.drain()
    line = await r.readline()
    w.close()
    return json.loads(line)


async def _without_runtime(fn):
    saved = os.environ.pop(LR.ENV_SOCKET, None)
    try:
        return await fn()
    finally:
        if saved:
            os.environ[LR.ENV_SOCKET] = saved


def _tamper(blob):
    """The same backup with one byte of notes' data changed and the manifest
    left as it was, encrypted again to the same key."""
    from compression import zstd
    src = tarfile.open(fileobj=io.BytesIO(decrypt(blob)), mode="r:")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None
            if m.name == "app-data/notes.tar.00001":
                data = bytes([data[0] ^ 1]) + data[1:]
            t.addfile(m, io.BytesIO(data) if data is not None else None)
    return subprocess.run([hb.AGE, "-r", rec], input=zstd.compress(buf.getvalue()),
                          capture_output=True, check=True).stdout


async def main():
    one = await Machine("first").start()
    rt = one.rt
    for app in ("notes", "photos"):
        a = await rt.handle("workspace.set_app_runtime", desc(app))
        w = await rt.handle("harness.wake", {"app_rt": app})
        check(f"{app} is installed and woken on the first machine", a["ok"] and w["ok"], (a, w))
    notes_slot = rt.state.alloc[C.unit_name("app", "notes")]["slot"]
    notes_uid = A.host_uid(notes_slot, 1000)
    big = os.urandom(200 * 1024)                       # more than one piece of DB_CHUNK
    fill(one, "notes", {"db.sqlite": big, "cache/a.json": b'{"invented": true}\n'}, notes_uid)
    if ROOTED:
        # A file owned by an id outside the app's range, set-user bit on:
        # neither survives a restore.
        stray = one.mount("notes") / "stray"
        stray.write_bytes(b"x")
        os.chown(stray, 0, 0)
        os.chmod(stray, 0o4755)
    fill(one, "photos", {"album/1.jpg": b"\xff\xd8 invented jpeg\n"},
         A.host_uid(rt.state.alloc[C.unit_name("app", "photos")]["slot"], 1000))
    # photos is removed and its data kept, as an uninstall the owner did not tick.
    r = await rt.handle("workspace.set_app_runtime", {"app_id": "photos", "enabled": False})
    check("photos is removed, its data kept", r["ok"] and C.data_volume("photos") in one.volumes(), r)
    want_notes, want_photos = tree(one.mount("notes")), tree(one.mount("photos"))

    print("== a backup holds each app's data")
    listing = await hb.app_data_listing()
    check("the Host lists every kept data volume on its machine, installed or not",
          sorted(a["app_id"] for a in listing.get("apps", [])) == ["notes", "photos"]
          and {a["app_id"]: a["installed"] for a in listing["apps"]} == {"notes": True, "photos": False},
          listing)
    blob, written = await asyncio.to_thread(write_backup, listing)
    part = written["parts"].get("app_data") or {}
    by = {a["app_id"]: a for a in part.get("apps", [])}
    check("its manifest part lists both, read whole, with the ids that own them",
          part.get("ok") and set(by) == {"notes", "photos"} and by["notes"]["installed"]
          and not by["photos"]["installed"] and by["notes"]["uid_base"] == A.host_uid(notes_slot, 0)
          and by["notes"]["chunks"] >= 3, part)
    got = members(blob)
    pieces = sorted(n for n in got if n.startswith("app-data/notes.tar."))
    joined = b"".join(got[n] for n in pieces)
    inner = tarfile.open(fileobj=io.BytesIO(joined), mode="r:")
    names = {m.name[2:] if m.name.startswith("./") else m.name for m in inner.getmembers()}
    check("the file holds notes' data as podman's volume tar, in pieces, with its own sha256",
          len(pieces) == by["notes"]["chunks"] and {"db.sqlite", "cache/a.json", "current"} <= names
          and inner.extractfile("./db.sqlite").read() == big
          and hashlib.sha256(joined).hexdigest() == by["notes"]["sha256"], (pieces, sorted(names)))
    check("and the kept data of the removed app", any(n.startswith("app-data/photos.tar.") for n in got))
    manifest = json.loads(got["manifest.json"])
    check("the manifest carries the part a restore checks",
          manifest["parts"]["app_data"]["apps"] == part["apps"])
    check("the README inside says what app-data/ is and how to open it",
          b"app-data/<app>.tar.NNNNN" in got["README.txt"] and b"cat app-data/<app>.tar.*" in got["README.txt"])
    check("a run with every app's data whole is not partial (nothing to say)",
          hb._partial_error({"db": {"ok": True}, "apps": {"ok": True}, "gitea": {"ok": True},
                             "app_data": part}) == "")

    # ── an app whose data cannot be read ────────────────────────────────────
    os.environ["FAKE_EXPORT_FAIL"] = C.data_volume("photos")
    try:
        blob_cut, written2 = await asyncio.to_thread(write_backup, await hb.app_data_listing())
    finally:
        os.environ.pop("FAKE_EXPORT_FAIL", None)
    p2 = written2["parts"]["app_data"]
    by2 = {a["app_id"]: a for a in p2["apps"]}
    err2 = hb._partial_error({"db": {"ok": True}, "apps": {"ok": True}, "gitea": {"ok": True}, "app_data": p2})
    check("an app whose data could not be read whole makes the part not ok, saying whose and why",
          not p2["ok"] and by2["notes"]["ok"] and not by2["photos"]["ok"]
          and "podman volume export exited 125" in by2["photos"]["error"]
          and err2.startswith("app data: photos's data could not be read"), (p2, err2))
    # ── an app whose data a restore would refuse ────────────────────────────
    out_link = one.mount("photos") / "out"
    os.symlink("../elsewhere", out_link)     # leads above the volume: podman's import refuses it
    try:
        _blob3, written3 = await asyncio.to_thread(write_backup, await hb.app_data_listing())
    finally:
        out_link.unlink()
    p3 = written3["parts"]["app_data"]
    by3 = {a["app_id"]: a for a in p3["apps"]}
    err3 = hb._partial_error({"db": {"ok": True}, "apps": {"ok": True}, "gitea": {"ok": True}, "app_data": p3})
    check("an app whose data holds a link out of its volume makes the run partial, naming the app and "
          "the link in the restore's words",
          not p3["ok"] and by3["notes"]["ok"] and not by3["photos"]["ok"] and by3["photos"].get("refused")
          and err3.startswith("app data: a restore would refuse the data of photos: ./out is a link to "
                              "../elsewhere, which leads out of the app's data"), (p3, err3))
    check("a machine with no local runtime lists nothing and adds nothing",
          await _without_runtime(hb.app_data_listing) is None)

    print("== a restore stages it, checked")
    staged = hr.STAGED
    out = await asyncio.to_thread(hr.unpack, io.BytesIO(blob), ident, staged)
    apps = {a["app_id"]: a for a in out.get("app_data", [])}
    check("each app's data is staged beside the files, pending",
          set(apps) == {"notes", "photos"} and all(a["state"] == hr.APP_DATA_PENDING for a in apps.values())
          and (hr.APP_DATA / "notes.tar").read_bytes() == joined
          and apps["notes"]["uid_base"] == A.host_uid(notes_slot, 0), out.get("app_data"))
    shutil.rmtree(hr.RESTORE_DIR)
    out_cut = await asyncio.to_thread(hr.unpack, io.BytesIO(blob_cut), ident, staged)
    cut = {a["app_id"]: a for a in out_cut.get("app_data", [])}
    check("the data a backup could not read whole is not staged, and says why",
          cut["photos"]["state"] == hr.APP_DATA_NOT_RESTORED and "read whole" in cut["photos"]["error"]
          and not (hr.APP_DATA / "photos.tar").exists() and cut["notes"]["state"] == hr.APP_DATA_PENDING,
          out_cut.get("app_data"))
    shutil.rmtree(hr.RESTORE_DIR)
    out_none = await asyncio.to_thread(hr.unpack, io.BytesIO(blob), ident, staged, stage_app_data=False)
    check("a machine with no local runtime reads and checks it, stages nothing, and says so",
          all(a["state"] == hr.APP_DATA_NOT_RESTORED for a in out_none["app_data"])
          and not hr.APP_DATA.exists(), out_none.get("app_data"))
    shutil.rmtree(hr.RESTORE_DIR)
    try:
        await asyncio.to_thread(hr.unpack, io.BytesIO(_tamper(blob)), ident, staged)
        check("data that does not match the manifest is refused", False)
    except hr.RestoreError as e:
        check("data that does not match the manifest is refused", "does not match its manifest" in str(e), e)
    shutil.rmtree(hr.RESTORE_DIR, ignore_errors=True)
    out = await asyncio.to_thread(hr.unpack, io.BytesIO(blob), ident, staged)
    await one.stop()

    print("== the restore puts it back on a fresh machine")
    two = await Machine("second").start()
    # Another app here first, so the restored apps get other id ranges.
    await two.rt.handle("workspace.set_app_runtime", desc("weather"))
    await two.rt.handle("harness.wake", {"app_rt": "weather"})
    await two.rt.handle("workspace.set_app_runtime", desc("notes"))
    plan = {"state": hr.FINISHING, "stamp": "20261020T030000Z", "app_data": out["app_data"]}
    hr._write_plan(plan)
    check("while it is pending, the app is not woken", "notes" in hr.app_data_pending())
    gate = await LR.runtime_client().request_full("harness.wake", {"app_rt": "notes"})
    check("  a wake is answered as still starting, with why, and never reaches the runtime",
          not gate["ok"] and gate.get("code") == 503 and "being put back from a backup" in gate["error"]
          and C.data_volume("notes") not in two.volumes(), gate)
    check("  an app not in the restore wakes as before",
          (await LR.runtime_client().request_full("harness.wake", {"app_rt": "weather"}))["ok"])
    real_lchown = os.lchown
    seen_owners = []
    if not ROOTED:
        S.os.lchown = lambda p, u, g: seen_owners.append((p, u, g))
    try:
        await hr._restore_app_data(plan)
    finally:
        S.os.lchown = real_lchown
    st = {a["app_id"]: a for a in plan["app_data"]}
    check("each app's data is put back, and the plan says so",
          st["notes"]["state"] == hr.APP_DATA_RESTORED and st["photos"]["state"] == hr.APP_DATA_RESTORED
          and json.loads(hr.PLAN.read_text())["app_data"] == plan["app_data"], plan["app_data"])
    check("  the staged copies are gone once they are back", not any(hr.APP_DATA.glob("*.tar")))
    check("notes' data is as it was, file for file, link for link",
          {k: v for k, v in tree(two.mount("notes")).items() if k != "stray"}
          == {k: v for k, v in want_notes.items() if k != "stray"}, sorted(tree(two.mount("notes"))))
    check("the removed app's kept data is back as kept data, not installed",
          tree(two.mount("photos")) == want_photos and "photos" not in two.rt.state.descriptors
          and two.volumes()[C.data_volume("photos")]["labels"] == {C.LABEL_SCOPE: "keep",
                                                                    C.LABEL_NAME: "app-photos"})
    new_slot = two.rt.state.alloc[C.unit_name("app", "notes")]["slot"]
    new_base = A.host_uid(new_slot, 0)
    check("this machine gave notes another id range", new_slot != notes_slot, (new_slot, notes_slot))
    if ROOTED:
        ids = owners(two.mount("notes"))
        check("its files are owned by this machine's ids for it, none outside its range",
              all(new_base <= i < new_base + C.UID_RANGE for i in ids)
              and new_base + 1000 in ids and new_base + C.NOBODY_ID in ids, sorted(ids))
        check("the file owned by root is now the range's nobody, its set-user bit gone",
              (two.mount("notes") / "stray").stat().st_uid == new_base + C.NOBODY_ID
              and not (two.mount("notes") / "stray").stat().st_mode & stat.S_ISUID)
    else:
        # The plan restores photos too; only notes' files are in question here.
        # They get their owners in notes' restore volume, before it is moved
        # into place.
        moved = {u for p, u, _g in seen_owners if C.restore_volume("notes") in Path(p).parts}
        check("its files are moved to this machine's ids for it (recorded: not root here)",
              moved and all(new_base <= u < new_base + C.UID_RANGE for u in moved), sorted(moved))
    hr._write_plan(plan)
    check("once back, the app wakes again", "notes" not in hr.app_data_pending()
          and (await LR.runtime_client().request_full("harness.wake", {"app_rt": "notes"}))["ok"])
    check("a restore that put everything back has nothing to tell the owner",
          hr.app_data_note(plan["app_data"]) is None)

    print("== the disk")
    vol_before = tree(two.mount("notes"))
    real_free = S.disk_free
    S.disk_free = lambda path: G.DISK_WAKE_FLOOR + 1000
    try:
        refused = await asyncio.to_thread(
            _try, lambda: LR.import_app_data("notes", io.BytesIO(b"\0" * (1 << 20)), 1 << 20, None))
    finally:
        S.disk_free = real_free
    check("an import that would leave the disk under the wake floor is refused 507, saying so",
          isinstance(refused, LR.RuntimeRefusal) and refused.code == 507
          and "keeps free so its apps can start" in refused.text, refused)
    check("  and nothing was touched: the app's data is as it was", tree(two.mount("notes")) == vol_before)
    pending = [{"app_id": "notes", "installed": True, "uid_base": None, "bytes": len(joined),
                "state": hr.APP_DATA_PENDING}]
    hr.APP_DATA.mkdir(parents=True, exist_ok=True)
    (hr.APP_DATA / "notes.tar").write_bytes(joined)
    S.disk_free = lambda path: G.DISK_WAKE_FLOOR
    try:
        await hr._restore_app_data({"app_data": pending})
    finally:
        S.disk_free = real_free
    note = hr.app_data_note(pending)
    check("a restore refused for room records that app as not put back, and tells the owner why",
          pending[0]["state"] == hr.APP_DATA_NOT_RESTORED and "notes:" in (note or "")
          and "Free some space" in (note or "") and not (hr.APP_DATA / "notes.tar").exists(), (pending, note))
    loop = asyncio.get_running_loop()

    class Slow(io.RawIOBase):
        """The staged tar, read slowly: the machine guard stops the import
        part way (the stop floor)."""

        def __init__(self):
            self.src, self.n = io.BytesIO(joined), 0

        def read(self, k=-1):
            self.n += 1
            if self.n == 2:
                asyncio.run_coroutine_threadsafe(_stop(two.rt), loop).result(10)
            return self.src.read(min(k, 32 * 1024))
    running = two.rt.units[C.unit_name("app", "notes")].state
    stopped = await asyncio.to_thread(_try, lambda: LR.import_app_data("notes", Slow(), len(joined), None))
    check("an import stops the app first when it runs: nothing writes its data while it is replaced",
          running == "running" and two.rt.units[C.unit_name("app", "notes")].state == "stopped"
          and "cseries-app-notes" not in fake.state(two.state)["containers"], running)
    check("an import the machine guard stops is answered 507, saying the disk fell under the stop floor",
          isinstance(stopped, LR.RuntimeRefusal) and stopped.code == 507
          and "fell under the" in stopped.text, stopped)
    check("  and no import is left in flight", not two.rt.imports)
    check("  and the app's data is exactly as it was, nothing of the cut import in it",
          tree(two.mount("notes")) == vol_before
          and not [v for v in two.volumes() if v.startswith("cseries-restore.")], sorted(two.volumes()))
    # A backup whose notes data holds a relative link out of the volume, as an
    # app's own data may (podman refuses the whole tar for one): the owner is
    # told which app and why, and that is true.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        ti = tarfile.TarInfo("./lib")
        ti.type, ti.linkname = tarfile.SYMTYPE, "../../../usr/lib/invented"
        t.addfile(ti)
    (hr.APP_DATA / "notes.tar").write_bytes(buf.getvalue())
    linked = [{"app_id": "notes", "installed": True, "uid_base": None, "bytes": len(buf.getvalue()),
               "state": hr.APP_DATA_PENDING}]
    await hr._restore_app_data({"app_data": linked})
    note = hr.app_data_note(linked) or ""
    check("a restore refused for a link out tells the owner which app and why, and its data is as it was",
          linked[0]["state"] == hr.APP_DATA_NOT_RESTORED and "notes: " in note
          and "./lib is a link to ../../../usr/lib/invented, which leads out of the app's data" in note
          and "is as it was" in note and tree(two.mount("notes")) == vol_before, (note, linked))
    over = await asyncio.to_thread(_send_more, two.sock, "notes", 10, joined[:100])
    check("a Host that sends more than it said is refused",
          over.get("code") == 400 and "more than" in over.get("error", ""), over)
    bad_base = await _raw(two.sock, {"op": "workspace.import_app_data",
                                     "params": {"app_id": "notes", "bytes": 1, "uid_base": C.UID_BASE + 7}})
    check("a uid range that is not one is refused", bad_base.get("code") == 400, bad_base)
    nothing = await asyncio.to_thread(_try, lambda: LR.export_app_data("nothing-here", lambda b: None))
    check("an export of an app with no data here is 404",
          isinstance(nothing, LR.RuntimeRefusal) and nothing.code == 404, nothing)
    plain = await two.rt.handle("workspace.export_app_data", {"app_id": "notes"})
    check("a streamed operation sent the plain way is refused, not answered with the status",
          plain.get("code") == 400, plain)
    await two.stop()


try:
    asyncio.run(main())
finally:
    with contextlib.suppress(Exception):
        shutil.rmtree(tmp)
print()
if FAILED:
    print(f"FAIL ({len(FAILED)})")
    sys.exit(1)
print("all backup app data checks passed")
