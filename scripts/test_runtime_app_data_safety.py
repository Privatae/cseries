#!/usr/bin/env python3
"""An app's data into a backup and back, the two ways it could go wrong
(vm/runtime cseries_runtime/server.py export_app_data, gate_tar).

  * A restore runs podman's volume import as root, and podman does not
    unpack inside the volume alone: a tar with a link `x` to a folder outside
    and then `x/file` writes that file outside, a hard link through such a
    link points at a file outside, and device nodes are made (measured on
    podman 5.4.2). The fake podman's import unpacks the same way (Python's
    tarfile, fully trusted), so each case here would land outside without
    the runtime's gate. With it each is refused, nothing outside is touched,
    and a tar as podman's own export writes one still goes back whole.
  * A backup copies an app's data while the app may be writing. Here a writer
    appends to a.log and then records its length in z.idx, so at every moment
    z.idx is at most a.log's length; a copy that reads a.log, lets the writer
    go on, and reads z.idx later holds an index past the log, which no moment
    ever held. With the app paused for its export (the fake podman's pause
    stops the writer as the cgroup freezer would) the copy holds one moment.
    Meanwhile a wake is answered "still starting", and the app runs again
    after, also when the export fails and when the Host goes away mid-way.

The real runtime behind a real unix socket, over the fake podman.

    python3 scripts/test_runtime_app_data_safety.py
"""
import asyncio
import contextlib
import fcntl
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import threading
import time
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
from cseries_runtime import common as C  # noqa: E402
from cseries_runtime import guard as G  # noqa: E402
from cseries_runtime import server as S  # noqa: E402
from cseries_runtime.podman import Podman  # noqa: E402
from server.applications import harness_backend as hb  # noqa: E402
from server.applications import local_runtime as LR  # noqa: E402
from server.applications import proxy  # noqa: E402

FAILED = []
# Root that can give files the app ids (not root in a user namespace
# that maps only its own ids, as under the CI runner's rootlesskit).
ROOTED = limits.owns_app_ids(C.UID_BASE, C.MAX_SLOTS * C.UID_RANGE)
PY = "ghcr.io/privatae/cseries-app-runtime-python@sha256:" + "1" * 64
# The runtime binds <tmp>/machine/runtime.sock.
tmp = limits.socket_tmp("rt-appdata-safety-", "machine/runtime.sock")


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:500]}]"))
    if not cond:
        FAILED.append(label)


def own_cgroup():
    for line in open("/proc/self/cgroup", encoding="ascii"):
        if line.startswith("0::"):
            return line[3:].strip()
    return ""


def desc(app):
    return {"app_id": app, "enabled": True, "descriptor": {
        "budgetMi": 128, "network": ["api.example.test"], "version": "1.0.0", "form": "process",
        "image": "python", "command": ["python3", "main.py"], "sourceToken": "cd" * 32}}


def _try(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — the check reads what it was
        return e


# ── crafted tars (invented names) ───────────────────────────────────────────
def _tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for ti, data in members:
            t.addfile(ti, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def reg(name, data=b"invented\n", mode=0o644):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode = len(data), mode
    return ti, data


def link(name, target, kind=tarfile.SYMTYPE):
    ti = tarfile.TarInfo(name)
    ti.type, ti.linkname = kind, target
    return ti, None


def folder(name):
    ti = tarfile.TarInfo(name)
    ti.type, ti.mode = tarfile.DIRTYPE, 0o755
    return ti, None


def node(name, kind, major, minor):
    ti = tarfile.TarInfo(name)
    ti.type, ti.devmajor, ti.devminor, ti.mode = kind, major, minor, 0o666
    return ti, None


def fifo(name):
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.FIFOTYPE
    return ti, None


def sparse():
    """A GNU 1.0 sparse member: 4 KiB in the tar, 64 MiB once unpacked."""
    ti = tarfile.TarInfo("./GNUSparseFile.0/holes")
    real = 64 << 20
    mapblock = (b"1\n" + str(real - 4096).encode() + b"\n4096\n").ljust(512, b"\0")
    ti.size = 512 + 4096
    ti.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0",
                      "GNU.sparse.name": "./holes", "GNU.sparse.realsize": str(real)}
    return ti, mapblock + b"S" * 4096


def _odd(path):
    """A device node or a hard-linked file under path (a link out shares
    its target's inode)."""
    for d, ds, fs in os.walk(path):
        for n in ds + fs:
            st = os.lstat(os.path.join(d, n))
            if stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode):
                return os.path.join(d, n)
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                return os.path.join(d, n)
    return None


async def gate_cases(mount, outside):
    victim = outside / "victim"
    victim.write_bytes(b"the machine's own file\n")
    os.chmod(victim, 0o600)
    v0 = victim.stat()
    cases = [
        ("a link to a folder outside, then a file through it",
         [link("./esc", str(outside)), reg("./esc/written")], "is inside esc"),
        ("a hard link through a link to a file outside",
         [link("./out", str(outside)), link("./h", "out/victim", tarfile.LNKTYPE)], "hard link"),
        ("a hard link to a name the data does not hold",
         [link("./h2", "nothing-here", tarfile.LNKTYPE)], "hard link"),
        ("a name that climbs out", [reg("../../climbed")], "climbs out"),
        ("a device node", [node("./blk", tarfile.BLKTYPE, 7, 0), reg("./after")], "is a device"),
        ("a sparse file (far more on disk than the bytes checked)", [sparse()], "sparse"),
        ("a name given twice (a link, then a folder of it)",
         [link("./twice", str(outside)), folder("./twice"), reg("./twice/inner")], "twice"),
    ]
    for label, members, want in cases:
        data = _tar(members)
        got = await asyncio.to_thread(_try, lambda: LR.import_app_data("vault", io.BytesIO(data), len(data), None))
        landed = sorted(p.name for p in outside.iterdir() if p.name != "victim")
        st = victim.stat()
        check(f"refused: {label}",
              isinstance(got, LR.RuntimeRefusal) and got.code == 400 and want in got.text, got)
        check("  nothing outside the volume was made or changed",
              not landed and st.st_nlink == 1 and (st.st_uid, st.st_gid) == (v0.st_uid, v0.st_gid)
              and victim.read_bytes() == b"the machine's own file\n", (landed, st.st_nlink, st.st_uid))
        check("  and no device or hard link out is left in the volume", _odd(mount) is None, _odd(mount))
    # What podman's own export writes: relative names, folders, a hard link
    # to a file before it, links (one absolute, inside the container's view),
    # a fifo; and a set-user bit, which is not kept.
    good = _tar([reg("a.txt", b"invented note\n"), link("b", "a.txt", tarfile.LNKTYPE), folder("d/"),
                 reg("d/x", b"x" * 5000), link("current", "/data/a.txt"), link("rel", "d/x"), fifo("pipe"),
                 reg("tool", b"#!/bin/sh\n", 0o4755)])
    ok = await asyncio.to_thread(_try, lambda: LR.import_app_data("vault", io.BytesIO(good), len(good), None))
    m = Path(mount)
    check("a tar as podman's export writes one goes back whole",
          isinstance(ok, dict) and ok.get("imported") and (m / "a.txt").read_bytes() == b"invented note\n"
          and os.stat(m / "b").st_ino == os.stat(m / "a.txt").st_ino and (m / "d/x").read_bytes() == b"x" * 5000
          and os.readlink(m / "current") == "/data/a.txt" and os.readlink(m / "rel") == "d/x"
          and stat.S_ISFIFO(os.lstat(m / "pipe").st_mode), ok)
    check("  a set-user bit is not kept", (m / "tool").is_file() and not (m / "tool").stat().st_mode & stat.S_ISUID)


# ── a restore is all or nothing ─────────────────────────────────────────────
def tree(root):
    """Every name under root: its kind, mode, and bytes or link target."""
    out = {}
    for d, ds, fs in os.walk(root):
        for n in ds + fs:
            p = os.path.join(d, n)
            st = os.lstat(p)
            what = (os.readlink(p) if stat.S_ISLNK(st.st_mode) else
                    open(p, "rb").read() if stat.S_ISREG(st.st_mode) else None)
            out[os.path.relpath(p, root)] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), what)
    return out


async def all_or_nothing(rt, state, mount):
    m = Path(mount)
    before = tree(m)
    vol = C.data_volume("vault")

    def restore_volumes():
        return sorted(n for n, v in fake.state(state)["volumes"].items()
                      if v["labels"].get(C.LABEL_SCOPE) == "restore" or n.startswith("cseries-restore."))

    def as_it_was(label):
        check(f"  {label}: the app's data is exactly as it was", tree(m) == before
              and fake.state(state)["volumes"][vol]["mount"] == mount, sorted(set(tree(m)) ^ set(before)))
        check("  and no restore volume is left", not restore_volumes(), restore_volumes())

    big = b"new data, invented\n" * 4000
    cases = [
        ("the tar refused part way (a device after a file)",
         [reg("./new.txt", big), node("./blk", tarfile.CHRTYPE, 1, 3)], {}, 400, "is a device"),
        ("podman's import failing part way",
         [reg("./new.txt", big), reg("./second")], {"FAKE_IMPORT_FAIL": "1"}, 500, "exited 125"),
        ("a relative link that leads out of the app's data, as an app's own data may hold",
         [reg("./new.txt", big), folder("./etc"), link("./etc/lib", "../../../usr/lib/invented")], {}, 400,
         "./etc/lib is a link to ../../../usr/lib/invented, which leads out of the app's data"),
    ]
    for label, members, env, code, want in cases:
        data = _tar(members)
        os.environ.update(env)
        try:
            got = await asyncio.to_thread(_try, lambda: LR.import_app_data("vault", io.BytesIO(data), len(data), None))
        finally:
            for k in env:
                os.environ.pop(k, None)
        check(f"refused {code}, naming the app and why: {label}",
              isinstance(got, LR.RuntimeRefusal) and got.code == code and want in got.text and "vault" in got.text,
              got)
        as_it_was(label)
    whole = _tar([reg("./new.txt", b"n" * 1024), reg("./more", b"m" * 1024)])
    short = whole[:512 + 1024]      # cut at the end of the first member: a tar reads it as whole
    got = await asyncio.to_thread(_try, lambda: LR.import_app_data("vault", io.BytesIO(short), len(whole), None))
    check("a tar shorter than the Host said is refused, not swapped in",
          isinstance(got, LR.RuntimeRefusal) and got.code == 400 and "not the" in got.text, got)
    as_it_was("a short tar")
    inside = _tar([folder("./etc"), link("./etc/lib", "../share/x"), link("./abs", "/usr/lib/x")])
    ok = await asyncio.to_thread(_try, lambda: LR.import_app_data("vault", io.BytesIO(inside), len(inside), None))
    check("links that stay inside the data (or are absolute, as the container sees them) go back",
          isinstance(ok, dict) and ok.get("imported") and os.readlink(m / "etc/lib") == "../share/x"
          and os.readlink(m / "abs") == "/usr/lib/x" and not (m / "a.txt").exists(), ok)
    check("  in the app's own volume, at its own place, and no restore volume is left",
          fake.state(state)["volumes"][vol]["mount"] == mount and not restore_volumes(), restore_volumes())

    # An app with no data here yet: a failed restore leaves none, nor an id range.
    unit = C.unit_name("app", "fresh")
    bad = _tar([reg("./x", big), node("./d", tarfile.BLKTYPE, 7, 0)])
    got = await asyncio.to_thread(_try, lambda: LR.import_app_data("fresh", io.BytesIO(bad), len(bad), None))
    check("a failed restore of an app with no data here leaves it none, and no id range taken",
          isinstance(got, LR.RuntimeRefusal) and got.code == 400
          and C.data_volume("fresh") not in fake.state(state)["volumes"] and not restore_volumes()
          and unit not in rt.state.alloc, (got, sorted(fake.state(state)["volumes"])))
    good = _tar([reg("./x", b"fresh, invented\n")])
    ok = await asyncio.to_thread(_try, lambda: LR.import_app_data("fresh", io.BytesIO(good), len(good), None))
    fv = fake.state(state)["volumes"].get(C.data_volume("fresh")) or {}
    check("  and a whole one makes its kept data volume, holding it",
          isinstance(ok, dict) and ok.get("imported") and fv.get("labels") == {C.LABEL_SCOPE: "keep", C.LABEL_NAME: unit}
          and Path(fv["mount"], "x").read_bytes() == b"fresh, invented\n" and not restore_volumes(), (ok, fv))


# ── the writer an export must not tear ──────────────────────────────────────
class Writer(threading.Thread):
    """Appends 1 KiB to a.log, then writes its length into z.idx (replaced
    whole), and again; stops while the fake podman says a container is
    paused, holding the fake's lock for each step so a pause that returned
    never finds it part way."""

    def __init__(self, root, lockpath, marks, side):
        super().__init__(daemon=True)
        self.root, self.lockpath, self.marks, self.side = Path(root), lockpath, marks, Path(side)
        self.stop, self.n, self.steps = threading.Event(), 0, 0

    def run(self):
        lf = open(self.lockpath, "a+")
        (self.root / "a.log").write_bytes(b"")
        (self.root / "m.bin").write_bytes(b"m" * 4096)
        while not self.stop.is_set():
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                if not os.path.exists(self.marks):
                    with open(self.root / "a.log", "ab") as f:
                        f.write(b"r" * 1024)
                    self.n += 1024
                    (self.side / "z.idx").write_text(str(self.n))
                    os.replace(self.side / "z.idx", self.root / "z.idx")
                    self.steps += 1
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
            time.sleep(0.002)


def copy_of(raw):
    t = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    got = {m.name.lstrip("./"): t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
    return len(got.get("a.log", b"")), int(got.get("z.idx", b"0") or 0)


def nothing_paused(state, marks):
    return not os.path.exists(marks) and not any(
        c.get("paused") for c in fake.state(state)["containers"].values())


async def main():
    d = tmp / "machine"
    paths = C.Paths(str(d))
    os.makedirs(paths.etc)
    with open(paths.images_json(), "w") as f:
        json.dump({"images": {"python": PY}}, f)
    state = str(d / "podman.json")
    st = fake.state(state)
    st["images"] = {PY: "a" * 64}
    fake.write_state(state, st)
    exe = fake.install(str(d))
    sock = str(d / "runtime.sock")
    marks = str(tmp / "paused")
    os.environ["FAKE_PODMAN_STATE"] = state
    os.environ["FAKE_PAUSED_FILE"] = marks

    async def ready(*a):
        return True
    rt = S.Runtime(paths, Podman(exe), G.Guard(total_mb=16384, disk_path=str(d), held=lambda: 0, psi_path=os.devnull),
                   http_probe=ready, tcp_probe=ready,
                   chown=(os.chown if ROOTED else (lambda *a: None)), machine="x86_64",
                   peer_uid=os.getuid(), peer_cgroups=(own_cgroup().rstrip("/") + "/",))
    server = await rt.serve(S.listening_socket(sock))
    os.environ[LR.ENV_SOCKET] = sock
    if not ROOTED:
        S.os.lchown = lambda *a: None     # owners are checked in test_backup_app_data as root

    print("== a restore's tar is checked before podman unpacks it")
    await rt.handle("workspace.set_app_runtime", desc("vault"))
    outside = tmp / "outside"
    outside.mkdir()
    vol = C.data_volume("vault")
    await rt.podman.volume_ensure(vol, C.unit_name("app", "vault"), "keep")
    mount = fake.state(state)["volumes"][vol]["mount"]
    await gate_cases(mount, outside)

    print("== a restore that fails part way leaves the app's data as it was")
    await all_or_nothing(rt, state, mount)

    print("== data a restore would refuse is said when the backup is made")
    mount = Path(fake.state(state)["volumes"][vol]["mount"])
    for p in mount.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    (mount / "d").mkdir()
    os.symlink("../x", mount / "d" / "l")      # leads to x inside the volume: podman takes it
    fine = await asyncio.to_thread(_try, lambda: LR.export_app_data("vault", io.BytesIO().write))
    check("a link that stays inside the volume (d/l -> ../x) goes into a backup as before",
          isinstance(fine, dict) and fine.get("bytes"), fine)
    os.symlink("../x", mount / "l")            # leads above the volume: podman refuses the whole tar
    sent = io.BytesIO()
    told = await asyncio.to_thread(_try, lambda: LR.export_app_data("vault", sent.write))
    check("one that leads out (l -> ../x) is refused when the backup is made, naming the link",
          isinstance(told, LR.RuntimeRefusal) and told.code == 422
          and told.text.startswith("a restore would refuse the data of vault: ")
          and "is a link to ../x, which leads out of the app's data" in told.text, told)
    check("  its bytes still went to the backup (the Host records the app as not whole)",
          len(sent.getvalue()) > 0)
    back = await asyncio.to_thread(_try, lambda: LR.import_app_data(
        "vault", io.BytesIO(sent.getvalue()), len(sent.getvalue()), None))
    why_backup = told.text.split(": ", 1)[1] if isinstance(told, LR.RuntimeRefusal) else None
    why_restore = back.text.split(": ", 1)[1] if isinstance(back, LR.RuntimeRefusal) else None
    check("  in the very words the restore of the same bytes is refused with",
          isinstance(back, LR.RuntimeRefusal) and back.code == 400 and why_backup == why_restore,
          (told, back))
    check("  and the app is not left paused", nothing_paused(state, marks))

    print("== an app's export is one moment of its data")
    w = await rt.handle("harness.wake", {"app_rt": "vault"})
    check("vault is installed and running", w.get("ok"), w)
    mount = fake.state(state)["volumes"][vol]["mount"]
    for p in Path(mount).iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    side = tmp / "side"
    side.mkdir()
    writer = Writer(mount, state + ".lock", marks, side)
    writer.start()
    for _ in range(200):
        if writer.steps > 20:
            break
        await asyncio.sleep(0.01)
    # The window's lane holds vault's address from a wake a moment ago, so its
    # next request would be sent straight there, with no wake first.
    cached = await hb.ensure_awake_app("vault")
    os.environ["FAKE_EXPORT_DELAY"] = "0.25"
    buf = io.BytesIO()
    loop = asyncio.get_running_loop()
    job = loop.run_in_executor(None, lambda: _try(lambda: LR.export_app_data("vault", buf.write)))
    await asyncio.sleep(0.15)
    wake = await rt.handle("harness.wake", {"app_rt": "vault"})
    paused_now = any(c.get("paused") for c in fake.state(state)["containers"].values())
    straight = await hb.ensure_awake_app("vault")
    answer = proxy._not_awake("vault", "vault is starting — retry") if straight is None else None
    head = await job
    os.environ.pop("FAKE_EXPORT_DELAY", None)
    n_log, n_idx = copy_of(buf.getvalue())
    check("the copy holds one moment: its index is never past its log",
          isinstance(head, dict) and n_idx <= n_log and n_log > 0, (head, n_log, n_idx))
    check("a wake meanwhile is answered still starting, not an error and not left waiting",
          not wake.get("ok") and wake.get("code") == 504 and "still starting" in wake.get("error", ""), wake)
    check("a request the Host would send straight to the paused app (its address cached) is not sent there",
          cached and paused_now and straight is None, (cached, paused_now, straight))
    check("  it is answered 504, still starting: its data is being copied into a backup",
          answer is not None and answer.status_code == 504
          and answer.detail == "still starting: its data is being copied into a backup",
          answer and (answer.status_code, answer.detail))
    check("  and once the copy is done the lane sends to the app again, at the same address",
          await hb.ensure_awake_app("vault") == cached
          and not getattr(LR, "exporting_app_data", lambda _a: False)("vault"))
    after = writer.steps
    await asyncio.sleep(0.2)
    pauses = [p[0] for p in fake.state(state).get("pauses", [])]
    check("the app runs again after: nothing is left paused, and it writes again",
          nothing_paused(state, marks) and writer.steps > after
          and pauses.count("pause") == pauses.count("unpause") > 0, pauses)
    check("  a wake then is answered as before", (await rt.handle("harness.wake", {"app_rt": "vault"}))["ok"])

    os.environ["FAKE_EXPORT_FAIL"] = vol
    failed = await asyncio.to_thread(_try, lambda: LR.export_app_data("vault", lambda b: None))
    os.environ.pop("FAKE_EXPORT_FAIL", None)
    check("an export that fails still starts the app again",
          isinstance(failed, LR.RuntimeRefusal) and failed.code == 500 and nothing_paused(state, marks), failed)

    os.environ["FAKE_EXPORT_DELAY"] = "0.25"

    def gone(b):
        raise OSError("the Host went away (invented)")
    lost = await asyncio.to_thread(_try, lambda: LR.export_app_data("vault", gone))
    for _ in range(300):
        if nothing_paused(state, marks) and not getattr(rt, "exporting", None):
            break
        await asyncio.sleep(0.02)
    os.environ.pop("FAKE_EXPORT_DELAY", None)
    check("an export whose Host goes away mid-way starts the app again",
          isinstance(lost, OSError) and nothing_paused(state, marks) and not getattr(rt, "exporting", None), lost)
    writer.stop.set()
    writer.join(5)
    server.close()
    await server.wait_closed()


try:
    asyncio.run(main())
finally:
    with contextlib.suppress(Exception):
        shutil.rmtree(tmp)
print()
if FAILED:
    print(f"FAIL ({len(FAILED)})")
    sys.exit(1)
print("all app data safety checks passed")
