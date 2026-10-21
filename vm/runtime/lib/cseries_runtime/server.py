"""The service: one unix socket only the Host can use, the operations
(validate.OPS), and the loops that keep what it started honest.

  * Every connection is checked first: the peer must be uid 1000 AND its
    process must live in the Host container's cgroup (on WSL the owner's own
    user is uid 1000 too, so the uid alone cannot tell them apart).
  * One JSON request line, one JSON answer line, in the bridge's envelope:
    {"ok": true, "data": {...}} or {"ok": false, "error": ..., "code": N}.
    The errors follow the operator's text, because the Host parses it.
  * A wake is single-flight per name; at most 4 cold starts run at once,
    30 container starts a minute, 32 containers on the machine.
  * Every 120 s an idle sweep stops what nobody has woken for 900 s (the
    operator's sweep; the Host re-stamps what it holds every 300 s).
  * A container that exits while it is held is started again after 10 s,
    doubling to 300 s; a wake that finds it crash-looping is answered
    terminal and the unit is stopped.
  * Every 2 s the machine guard may stop one container (guard.py); on a short
    disk it also measures the apps' data and stops pulls and builds.
  * An app's data volume is never deleted by the runtime on its own: only
    workspace.delete_app_data, for an app already removed, deletes one.
  * On start, everything labelled as this runtime's is removed: nothing that
    survives a reboot should, and the Host brings back what it holds.

It logs decisions under the journal identifier cseries-runtime: op, unit,
outcome, durations, the refusal. Never an env value, a credential, a
command or a destination.
"""

import argparse
import asyncio
import contextlib
import ctypes
import errno
import json
import logging
import os
import platform
import secrets
import shutil
import socket
import stat
import struct
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections import deque

from . import argv as A
from . import common as C
from . import guard as GD
from . import validate as V
from .guard import (DATA_SAMPLE_EVERY_S, DATA_WATCH_BELOW, DISK_STOP_FLOOR, Guard, dir_bytes,
                    disk_free, over_plan_text, parse_meminfo, parse_psi_full, read_text)
from .podman import Podman, PodmanError
from .state import State

log = logging.getLogger("cseries-runtime")

PEER_UID = 1000
PEER_CGROUPS = ("/system.slice/cseries-host.service/",)
CRASHLOOP_AFTER = 3          # exits in a row before a wake is answered terminal
MONITOR_EVERY_S = 5.0
REFUSED = "only the Host may use this socket"


def restart_wait(failures):
    """10 s, doubling, at most 300 s: Kubernetes' CrashLoopBackOff."""
    return float(min(C.RESTART_FIRST_S * (2 ** max(0, failures - 1)), C.RESTART_MAX_S))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A readiness probe never follows a redirect. The answer comes from the
    container's own code, and this probe runs as root on the machine: a
    followed Location would have the runtime GET any address the machine
    reaches (its own services, the LAN, a metadata address) on an app's
    behalf. The 3xx itself is the answer, as Kubernetes counts a probe's
    redirect to another host a success without following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def probe_http(url, timeout=2.0):
    def _get():
        req = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as r:
                return 200 <= r.status < 400
        except urllib.error.HTTPError as e:
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass
            return 300 <= e.code < 400
        except (urllib.error.URLError, OSError, ValueError):
            return False
    return await asyncio.to_thread(_get)


async def probe_tcp(ip, port, timeout=2.0):
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    w.close()
    return True


class _Cut:
    """A restore of an app's data in flight, as stop_writes sees it: cancel()
    stops podman's import, and the frames still coming are read and dropped."""

    def __init__(self, proc):
        self.proc, self.cut = proc, False

    def done(self):
        return self.cut

    def cancel(self):
        self.cut = True
        try:
            self.proc.kill()
        except ProcessLookupError:
            pass


def remap_owners(root, old_base, new_base):
    """Move every name under root, root itself too, onto this machine's id
    range for the app (new_base up): an id in the backup's range (old_base
    up, when known) keeps its place in it, an id already in the new range
    stays, and any other id becomes the range's NOBODY_ID, so nothing in an
    app's data is owned by an id outside its own range (root's, the Host's,
    another app's). Set-user and set-group bits on files are cleared: the
    containers run with no-new-privileges, so they are never wanted there.
    Links are changed, never followed. Run with the app stopped (its lock
    held), so nothing moves in the tree meanwhile. Returns how many names
    were changed."""
    def to(i):
        if old_base is not None and old_base <= i < old_base + C.UID_RANGE:
            return new_base + (i - old_base)
        if new_base <= i < new_base + C.UID_RANGE:
            return i
        return new_base + C.NOBODY_ID

    moved = 0

    def one(path):
        nonlocal moved
        st = os.lstat(path)
        uid, gid = to(st.st_uid), to(st.st_gid)
        changed = False
        if (uid, gid) != (st.st_uid, st.st_gid):
            os.lchown(path, uid, gid)
            changed = True
        if stat.S_ISREG(st.st_mode) and st.st_mode & (stat.S_ISUID | stat.S_ISGID):
            os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~(stat.S_ISUID | stat.S_ISGID))
            changed = True
        moved += changed

    one(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames + filenames:
            one(os.path.join(dirpath, n))
    return moved


_AT_FDCWD = -100
_RENAME_EXCHANGE = 2


def exchange_dirs(a, b):
    """Swap two directories on one filesystem in one step (renameat2 with
    RENAME_EXCHANGE, Linux 3.15 on): each name holds the other's tree after,
    and at no moment does either name hold nothing or half. Raises OSError
    when it cannot be made, both left as they were."""
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        raise OSError(errno.ENOSYS, "renameat2 is not in this C library")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(_AT_FDCWD, os.fsencode(a), _AT_FDCWD, os.fsencode(b), _RENAME_EXCHANGE) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), a)


class TarRefused(Exception):
    """A restore's tar holds what an app's data never holds (gate_tar)."""


def _tar_rel(name):
    """A member's name as a path inside the volume ("" for the volume's own
    folder), or None when it climbs out of it."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts)


def _link_leaves(folder, target):
    """Whether a relative link in `folder` (its parts inside the volume)
    names a place above the volume, read by its words alone as podman's
    import does (containers/storage pkg/archive: the link's folder joined
    with its target must stay under the volume, else the whole import is
    refused). An absolute target stays under the volume by that rule. One
    that climbs above the volume and comes back into it by the volume's own
    directory name on the machine is refused here too: that name is not the
    app's to know."""
    if target.startswith("/"):
        return False
    depth = len(folder)
    for p in target.split("/"):
        if p in ("", "."):
            continue
        depth += -1 if p == ".." else 1
        if depth < 0:
            return True
    return False


def gate_tar(fin, fout):
    """Copy the tar a restore sends (fin) to podman's volume import (fout),
    member by member, written afresh: podman unpacks only what was checked
    here, never the Host's own bytes. As root, and not chrooted into the
    volume, podman's import writes through a link the same tar made (a link
    `x` to /etc, then `x/passwd`), and makes a hard link to any file a link
    leads to (measured: podman 5.4.2), so nothing that could lead out of
    the volume gets that far:

      * a name that climbs out (..), or one inside a link or a file the tar
        made earlier, or a name it gave already, is refused;
      * a hard link must name a file the tar made earlier;
      * a relative link that leads above the volume is refused, with its
        name: podman would refuse the whole tar for it, saying less;
      * device nodes and sparse files are refused: podman's own export of an
        app's data never writes one (the app runs in a user namespace, which
        cannot make a device; podman writes holes as zeros), and a sparse
        member would unpack to far more than the bytes checked against the
        disk;
      * extended attributes are dropped (a file capability among them), and
        set-user and set-group bits are cleared on files;
      * owners stay as numbers (remap_owners moves them).

    Raises TarRefused, saying what and where; a tar that is not one raises
    tarfile's own errors. Synchronous: it runs in a worker thread between
    the frames and podman's stdin."""
    seen = {}                   # path inside the volume -> "d", "f", "l" (link), "o" (other)
    implied = set()             # folders a name inside them made (podman makes them)
    with tarfile.open(fileobj=fin, mode="r|") as tin, \
            tarfile.open(fileobj=fout, mode="w|", format=tarfile.PAX_FORMAT) as tout:
        for m in tin:
            rel = _tar_rel(m.name)
            if rel is None:
                raise TarRefused(f"{m.name[:200]} climbs out of the app's data")
            parts = rel.split("/") if rel else []
            ups = ["/".join(parts[:i]) for i in range(1, len(parts))]
            for up in ups:
                if seen.get(up, "d") != "d":
                    raise TarRefused(f"{m.name[:200]} is inside {up[:200]}, which is not a folder")
            if rel in seen or ((rel == "" or rel in implied) and not m.isdir()):
                raise TarRefused(f"{m.name[:200]} is in the data twice")
            implied.update(up for up in ups if up not in seen)
            if m.ischr() or m.isblk():
                raise TarRefused(f"{m.name[:200]} is a device")
            if m.issparse():
                raise TarRefused(f"{m.name[:200]} is a sparse file")
            ti = tarfile.TarInfo("./" + rel if rel else ".")
            ti.uid, ti.gid, ti.mtime = m.uid, m.gid, m.mtime
            ti.mode = m.mode & 0o7777
            if m.isdir():
                ti.type, kind = tarfile.DIRTYPE, "d"
            elif m.isreg():
                ti.type, ti.size, kind = tarfile.REGTYPE, m.size, "f"
                ti.mode &= ~(stat.S_ISUID | stat.S_ISGID)
            elif m.issym():
                if _link_leaves(parts[:-1], m.linkname):
                    raise TarRefused(f"{m.name[:200]} is a link to {m.linkname[:200]}, which leads out of "
                                     "the app's data; podman puts back no tar that holds one")
                ti.type, ti.linkname, kind = tarfile.SYMTYPE, m.linkname, "l"
            elif m.islnk():
                to = _tar_rel(m.linkname)
                if to is None or seen.get(to) != "f":
                    raise TarRefused(f"{m.name[:200]} is a hard link to {m.linkname[:200]}, "
                                     "not to a file the data holds")
                ti.type, ti.linkname, kind = tarfile.LNKTYPE, "./" + to, "f"
                ti.mode &= ~(stat.S_ISUID | stat.S_ISGID)
            elif m.isfifo():
                ti.type, kind = tarfile.FIFOTYPE, "o"
            else:
                raise TarRefused(f"{m.name[:200]} is not a file, a folder or a link")
            tout.addfile(ti, tin.extractfile(m) if ti.type == tarfile.REGTYPE else None)
            seen[rel] = kind


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class _Dropped:
    """Where gate_tar writes when only its verdict is wanted (an export)."""

    def write(self, b):
        return len(b)

    def flush(self):
        return None


class Unit:
    def __init__(self, name, kind, ident):
        self.name = name
        self.kind = kind
        self.ident = ident
        self.plan = None
        self.state = "stopped"      # starting | running | stopped
        self.started = None         # monotonic
        self.started_wall = None
        self.last_wake = None
        self.cap = None             # an engine's control capability, this container only
        self.connector_cap = None
        self.creds = {}             # what the last cold start was given, for a restart
        self.failures = 0
        self.last_exit = None
        self.next_restart = None
        self.resident = False
        self.gen = 0                # bumped by every cold start and every stop
        self.lock = asyncio.Lock()
        self.stopped_why = None
        # The app's kept data volume, measured by the watchdog while the disk
        # is short: its directory, its last size, and when it last grew.
        self.data_mount = None
        self.data_bytes = None
        self.data_grew_at = None
        self.data_sampled_at = None

    def guard_view(self):
        return {"kind": self.kind, "ident": self.ident, "name": self.name,
                "memory_mi": self.plan.memory_mi if self.plan else 0,
                "started": self.started or 0, "resident": self.resident,
                "running": self.state in ("starting", "running"),
                "has_data": bool(self.plan and any(scope == "keep" for _n, scope, _u, _m
                                                   in self.plan.volumes)),
                "data_bytes": self.data_bytes, "data_grew_at": self.data_grew_at}

    @property
    def main(self):
        return f"cseries-{self.name}"

    def url(self):
        return f"http://{self.plan.ip}:{self.plan.port}"


class Runtime:
    def __init__(self, paths=None, podman=None, guard=None, *, http_probe=probe_http,
                 tcp_probe=probe_tcp, clock=time.monotonic, chown=os.chown,
                 machine=None, builder=None, peer_uid=PEER_UID, peer_cgroups=PEER_CGROUPS,
                 sleep=asyncio.sleep):
        self.paths = paths or C.Paths()
        self.podman = podman or Podman()
        self.guard = guard or Guard()
        self.http_probe = http_probe
        self.tcp_probe = tcp_probe
        self.clock = clock
        self.chown = chown
        self.sleep = sleep
        self.machine = machine or platform.machine()
        self.builder = builder
        self.peer_uid = peer_uid
        self.peer_cgroups = tuple(peer_cgroups)
        self.state = State(self.paths).load()
        self.units = {}
        self.cold = asyncio.Semaphore(C.MAX_COLD_STARTS)
        self.creates = deque()
        self.in_flight = {}
        self.pulls = {}
        self.imports = set()        # restores of an app's data writing to the disk now
        self.exporting = set()      # units whose data a backup is copying now (paused)

    # ── helpers ─────────────────────────────────────────────────────────────

    def unit(self, kind, ident):
        name = C.unit_name(kind, ident)
        u = self.units.get(name)
        if u is None:
            u = self.units[name] = Unit(name, kind, ident)
        return u

    def images(self):
        try:
            raw = json.loads(read_text(self.paths.images_json()) or "{}")
        except ValueError:
            log.warning("images.json is not JSON; no image is used")
            raw = {}
        return V.image_table(raw)

    def arm64(self):
        return self.machine in ("aarch64", "arm64")

    def live(self):
        return [u for u in self.units.values() if u.state in ("starting", "running")]

    @staticmethod
    def _busy(u):
        """Running, starting, or a wake holds its lock: a cold start that is
        still waiting for a build, an image or a slot is not yet "starting",
        and a release or removal then must still reach it."""
        return u.state != "stopped" or u.lock.locked()

    def browser_token(self):
        tok = read_text(self.paths.browser_token()).strip()
        if not tok:
            raise V.Refusal(504, "terminal: this machine has no browser control token "
                                 "(cseries-guest configure makes it)")
        return tok

    def _image_for_family(self, family):
        if self.arm64() and family == "creative":
            raise V.Refusal(400, "no arm64 build of creative")
        images, _src = self.images()
        ref = images.get(family)
        if not ref:
            raise V.Refusal(504, f"terminal: no {family} image is pinned on this machine")
        return ref

    async def ensure_image(self, ref, deadline):
        if await self.podman.image_exists(ref):
            return
        task = self.pulls.get(ref)
        if task is None or task.done():
            task = self.pulls[ref] = asyncio.ensure_future(
                self.podman.pull(ref, timeout=max(30, int(deadline - self.clock()))))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(1, deadline - self.clock()))
        except asyncio.TimeoutError:
            raise V.Refusal(504, "still starting (pulling its image)") from None
        except asyncio.CancelledError:
            if not task.cancelled():
                raise                      # this wake itself was cancelled
            # The machine guard stopped the pull (stop_writes): the disk is short.
            raise V.Refusal(507, "its image pull was stopped: this machine's disk is short") from None
        except PodmanError as e:
            raise V.Refusal(504, f"terminal: ErrImagePull: {e}") from None

    def _note_create(self):
        now = self.clock()
        while self.creates and now - self.creates[0] > 60:
            self.creates.popleft()
        self.creates.append(now)

    def _limits(self, plan):
        now = self.clock()
        while self.creates and now - self.creates[0] > 60:
            self.creates.popleft()
        if len(self.creates) + len(plan.containers) > C.MAX_CREATES_PER_MINUTE:
            raise V.Refusal(429, "this machine started 30 containers in the last minute; try again shortly")
        running = sum(len([c for c in u.plan.containers if not c.wait])
                      for u in self.live() if u.plan)
        if running + len([c for c in plan.containers if not c.wait]) > C.MAX_CONTAINERS:
            raise V.Refusal(429, f"this machine already runs {running} containers "
                                 f"(at most {C.MAX_CONTAINERS})")

    # ── files the containers mount ──────────────────────────────────────────

    def _fresh_dir(self, path, owner):
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        os.mkdir(path, 0o700)
        self.chown(path, owner, owner)

    def _write_file(self, path, data, mode, owner):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
        try:
            os.write(fd, data)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        self.chown(path, owner, owner)

    def _write_files(self, u, plan, slot):
        made = set()
        for path, (data, mode, uid) in sorted(plan.files.items()):
            d = os.path.dirname(path)
            owner = A.host_uid(slot, uid)
            if d not in made:
                self._fresh_dir(d, owner)
                made.add(d)
            self._write_file(path, data, mode, owner)
        # A harness's creds directory is mounted even when it has no file yet:
        # the engine's own login writes there.
        if u.kind == "harness":
            d = self.paths.creds_dir(u.name)
            if d not in made:
                self._fresh_dir(d, A.host_uid(slot, C.HARNESSES[u.ident]["uid"]))
        os.makedirs(self.paths.env_dir(), mode=0o700, exist_ok=True)

    def _write_env(self, path, env):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._write_file(path, A.env_file_text(env), 0o600, 0)

    async def _make_volumes(self, u, plan, slot):
        for name, scope, uid, mode in plan.volumes:
            mnt = await self.podman.volume_ensure(name, plan.unit, scope)
            if mnt:
                owner = A.host_uid(slot, uid)
                self.chown(mnt, owner, owner)
                os.chmod(mnt, mode)
                if scope == "keep":
                    u.data_mount = mnt

    def _clear_files(self, name):
        for d in (self.paths.creds_dir(name), self.paths.creds_dir(C.wall_of(name))):
            shutil.rmtree(d, ignore_errors=True)
        for e in (name, C.wall_of(name), C.fetch_of(name)):
            try:
                os.unlink(self.paths.env_file(e))
            except FileNotFoundError:
                pass

    # ── the disk ────────────────────────────────────────────────────────────

    async def _disk_check(self):
        """A wake on a short disk is refused 507, naming the floor and what
        each app's data takes (measured only then)."""
        free = self.guard.disk_short()
        if free is not None:
            raise V.Refusal(507, self.guard.disk_text(free, await self.app_data_sizes()))

    async def app_data_sizes(self):
        """Every kept data volume on the machine: [{app_id, bytes, complete,
        installed}], largest first. Found by the runtime's own scope label;
        the app id is read back from the volume's name (common.data_volume)."""
        out = []
        for vol in await self.podman.volumes_scoped("keep"):
            aid = C.app_of_data_volume(vol)
            if aid is None or not V.APP_ID_RE.match(aid):
                continue
            mnt = await self.podman.volume_mount(vol)
            size, complete = (0, False)
            if mnt:
                size, complete = await asyncio.to_thread(dir_bytes, mnt)
            out.append({"app_id": aid, "bytes": size, "complete": complete,
                        "installed": aid in self.state.descriptors})
        return sorted(out, key=lambda d: (-d["bytes"], d["app_id"]))

    async def app_data(self):
        free = disk_free(self.guard.disk_path)
        return {"apps": await self.app_data_sizes(), "free_bytes": free,
                "wake_floor_bytes": GD.DISK_WAKE_FLOOR, "stop_floor_bytes": DISK_STOP_FLOOR}

    async def delete_app_data(self, aid):
        """Delete one removed app's kept data volume: the owner chose it. An
        app that is still installed (its descriptor is kept) is refused, and so
        is one with a container up or a wake in flight; the unit's lock is held
        so no wake starts while the volume goes."""
        u = self.unit("app", aid)
        async with u.lock:
            if aid in self.state.descriptors:
                raise V.Refusal(409, f"{aid} is installed; its data is deleted with it, when "
                                     "you uninstall it and choose to")
            if u.state != "stopped":
                raise V.Refusal(409, f"{aid} is still running")
            # Found by both labels, and then by name: a deletion cannot be
            # undone, so it never rests on how podman combines two filters.
            want = C.data_volume(aid)
            vols = [v for v in await self.podman.volumes_scoped("keep", unit=u.name) if v == want]
            if not vols:
                return {"app_id": aid, "deleted": False, "freed_bytes": 0}
            freed = 0
            for vol in vols:
                mnt = await self.podman.volume_mount(vol)
                if mnt:
                    freed += (await asyncio.to_thread(dir_bytes, mnt))[0]
            await self.podman.volume_remove(vols)
            left = [v for v in await self.podman.volumes_scoped("keep", unit=u.name) if v == want]
            if left:
                raise V.Refusal(500, f"the data of {aid} could not be deleted")
            self.state.forget_slot(u.name)
            u.data_mount = u.data_bytes = u.data_grew_at = u.data_sampled_at = None
            log.info("%s: its kept data was deleted (%d MiB)", u.name, freed >> 20)
            return {"app_id": aid, "deleted": True, "freed_bytes": freed}

    async def sample_data(self, now, every=DATA_SAMPLE_EVERY_S):
        """Measure each running app's data volume, at most every `every`
        seconds, and note when it grew."""
        for u in self.live():
            if u.kind != "app" or not u.data_mount:
                continue
            if u.data_sampled_at is not None and now - u.data_sampled_at < every:
                continue
            size, _complete = await asyncio.to_thread(dir_bytes, u.data_mount)
            if u.data_bytes is not None and size > u.data_bytes:
                u.data_grew_at = now
            u.data_bytes, u.data_sampled_at = size, now

    def stop_writes(self):
        """Cancel the image pulls, engine builds and restores of an app's data
        in flight: on a disk under the stop floor they would take what is
        left."""
        n = 0
        for t in list(self.pulls.values()) + list(self.imports):
            if not t.done():
                t.cancel()
                n += 1
        if self.builder is not None:
            n += self.builder.cancel_all()
        if n:
            log.warning("the machine guard cancelled %d image pull(s), build(s) and data restore(s): "
                        "the disk is short", n)
        return n

    # ── an app's kept data, into a backup and back ──────────────────────────
    #
    # A backup holds everything the Host owns, and an app's data lives in its
    # kept data volume here, outside the Host's container: the Host's backup
    # cannot read it, and a restore cannot write it. So the runtime moves it,
    # with podman's own `volume export` and `volume import` (the volume's
    # tree as one tar, owners and modes kept), streamed over the Host's own
    # socket (Runtime.stream): nothing is written to the disk on the way out,
    # and on the way back only the volume itself.

    async def _kept_volume(self, aid):
        """(unit, the app's kept data volume or None): by the runtime's own
        labels and then by name, as delete_app_data finds it."""
        u = self.unit("app", aid)
        want = C.data_volume(aid)
        vols = [v for v in await self.podman.volumes_scoped("keep", unit=u.name) if v == want]
        return u, (vols[0] if vols else None)

    async def export_app_data(self, aid, writer):
        """Send one app's kept data: the first answer line says whose it is
        and which machine ids own it here, then podman's volume tar in
        frames, then a frame of length 0.

        Copied whole at one moment: under the unit's lock, with a running
        app's containers paused (the cgroup freezer) from before podman reads
        the first file until after the last, and started again on every way
        out (done, refused, the Host gone, the idle limit). So the copy is
        what the disk held at one instant, as if the machine had lost power
        then: an app's own database comes back as after a crash, which it
        recovers from, never half of one write. Meanwhile a wake of the app
        is answered "still starting" (wake_app), never left waiting. A
        volume deleted meanwhile makes podman fail, and the answer says so."""
        u = self.unit("app", aid)
        if u.name in self.exporting:
            raise V.Refusal(409, f"the data of {aid} is being copied into a backup already")
        self.exporting.add(u.name)
        try:
            async with u.lock:
                paused = []
                try:
                    if u.state != "stopped":
                        names = [c.name for c in u.plan.containers] if u.plan else C.container_names(u.name)
                        paused = await self.podman.pause(names)
                        if paused:
                            log.info("%s: paused while its data is copied into a backup", u.name)
                    return await self._export_app_data(u, aid, writer)
                finally:
                    if paused:
                        # Shielded: a second cancel must not leave the app frozen.
                        with contextlib.suppress(Exception):
                            await asyncio.shield(self.podman.unpause(paused))
                        log.info("%s: started again after its data was copied", u.name)
        finally:
            self.exporting.discard(u.name)

    async def _export_app_data(self, u, aid, writer):
        _u, vol = await self._kept_volume(aid)
        if vol is None:
            raise V.Refusal(404, f"{aid} has no kept data on this machine")
        a = self.state.alloc.get(u.name)
        head = {"app_id": aid, "installed": aid in self.state.descriptors,
                "uid_base": A.host_uid(a["slot"], 0) if a else None, "uid_range": C.UID_RANGE}
        try:
            proc = await self.podman.volume_stream(vol, out=True)
        except OSError as e:
            raise V.Refusal(500, f"podman could not be started: {e.strerror}") from None
        err = asyncio.ensure_future(proc.stderr.read())
        # The same tar also goes through gate_tar, the restore's own check, in
        # a worker thread with what it writes dropped: data a restore would
        # refuse (a link that leads out of the volume, which podman's import
        # refuses whole) is said now, in the restore's words, not accepted
        # into a backup that can never put it back.
        r_chk, w_chk = os.pipe()
        fchk = os.fdopen(r_chk, "rb")
        feeding = [True]

        def run_check():
            try:
                gate_tar(fchk, _Dropped())
                return None
            except TarRefused as e:
                return str(e)
            except (tarfile.TarError, EOFError, OSError) as e:
                return f"it is not a whole volume tar ({type(e).__name__})"
            finally:
                fchk.close()

        def end_check():
            if feeding[0]:
                feeding[0] = False
                os.close(w_chk)

        async def check_chunk(chunk):
            if not feeding[0]:
                return
            try:
                await asyncio.to_thread(_write_all, w_chk, chunk)
            except OSError:
                end_check()         # the check has stopped: it says why at the end

        checking = asyncio.ensure_future(asyncio.to_thread(run_check))
        sent = 0
        writer.write(json.dumps({"ok": True, "data": head}, sort_keys=True).encode("utf-8") + b"\n")
        try:
            while True:
                # Each wait bounded: the app stays paused while this runs, so
                # a Host that stops reading, or a podman that stops writing,
                # must not hold it there.
                chunk = await asyncio.wait_for(proc.stdout.read(C.FRAME_MAX), C.STREAM_IDLE_S)
                if not chunk:
                    break
                writer.write(struct.pack(">I", len(chunk)) + chunk)
                await asyncio.gather(asyncio.wait_for(writer.drain(), C.STREAM_IDLE_S), check_chunk(chunk))
                sent += len(chunk)
            rc = await asyncio.wait_for(proc.wait(), C.STREAM_IDLE_S)
        except BaseException:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            err.cancel()
            end_check()
            with contextlib.suppress(Exception):
                await asyncio.shield(checking)
            raise
        end_check()
        why = await checking
        tail = " / ".join((await err).decode("utf-8", "replace").strip().splitlines()[-3:])
        writer.write(struct.pack(">I", 0))
        if rc != 0:
            raise V.Refusal(500, f"podman volume export exited {rc}: {tail[:400]}")
        if why is not None:
            log.warning("%s: its kept data went to a backup, but a restore would refuse it: %s",
                        u.name, why[:300])
            raise V.Refusal(422, f"a restore would refuse the data of {aid}: {why}")
        log.info("%s: its kept data went to a backup (%d MiB)", u.name, sent >> 20)
        return {"app_id": aid, "bytes": sent}

    @staticmethod
    async def _frames_in(reader, limit, sink):
        """Read frames until the one of length 0, handing each to sink;
        their bytes together, at most `limit`. A frame past FRAME_MAX or past
        the limit is refused: the disk was checked for `limit` bytes."""
        got = 0
        while True:
            n = struct.unpack(">I", await reader.readexactly(4))[0]
            if n == 0:
                return got
            if n > C.FRAME_MAX or got + n > limit:
                raise V.Refusal(400, f"the data sent is more than the {limit} bytes it was said to be")
            data = await reader.readexactly(n)
            got += n
            await sink(data)

    async def import_app_data(self, aid, nbytes, old_base, reader, writer):
        """Put one app's kept data back from a restore, all or nothing: podman
        imports the tar the Host sends into a volume of its own (the restore
        volume, common.restore_volume), its files are moved onto this
        machine's id range for the app, and only then is it swapped with the
        app's kept data volume in one step (exchange_dirs) and the old data
        deleted. A restore that fails part way (the tar refused, podman's
        import failing, the machine guard stopping it, the Host gone) deletes
        only its own volume: the app's data is exactly as it was.

        Under the unit's lock, with the app stopped first: nothing reads or
        writes the data while it is replaced, and no wake starts meanwhile.
        Both copies are on the disk until the swap, so it is refused 507
        before anything is touched when the new one would leave the disk
        under DISK_WAKE_FLOOR beside the old; an import is stopped like a
        pull when the disk falls under the stop floor anyway (stop_writes).
        The data already here is replaced only once the Host has said what it
        restores: the owner chose the restore, and the backup taken just
        before it holds the data as it was."""
        u = self.unit("app", aid)
        async with u.lock:
            want = C.data_volume(aid)
            have = [v for v in await self.podman.volumes_scoped("keep", unit=u.name) if v == want]
            if not have and await self.podman.volume_mount(want):
                raise V.Refusal(500, f"a volume named {want} that is not this app's kept data is in the way; "
                                     f"the data of {aid} was not put back")
            free = disk_free(self.guard.disk_path)
            if free is not None and free - nbytes < GD.DISK_WAKE_FLOOR:
                left = max(0, free - nbytes)
                raise V.Refusal(507, f"putting back the data of {aid} takes {GD.fmt_bytes(nbytes)}, beside the "
                                     "data it replaces (kept until the backup's is in whole), and would leave "
                                     f"this machine {GD.fmt_bytes(left)} free, under the "
                                     f"{GD.DISK_WAKE_FLOOR >> 30} GiB it keeps free so its apps can start. "
                                     "Free some space and restore again")
            # The lock is held here, so _busy() would say yes to a stopped app.
            if u.state != "stopped":
                await self.teardown(u, "its data is being restored from a backup")
            had_slot = u.name in self.state.alloc
            slot = self.state.reserve_slot(u.name)
            stage = C.restore_volume(aid)
            done = False
            try:
                await self._drop_restore_volume(u, stage)       # one a stopped runtime left
                smnt = await self.podman.volume_ensure(stage, u.name, C.RESTORE_SCOPE)
                if not smnt:
                    raise V.Refusal(500, f"podman made no volume for the data of {aid}")
                got = await self._import_into(u, aid, stage, nbytes, reader, writer)
                if got != nbytes:
                    # A tar cut at a member's end reads as whole: only the count tells.
                    raise V.Refusal(400, f"the data of {aid} sent was {got} bytes, not the {nbytes} "
                                         "it was said to be")
                moved = await asyncio.to_thread(remap_owners, smnt, old_base, A.host_uid(slot, 0))
                mnt = await self._swap_in(u, aid, want, bool(have), smnt)
                done = True
            finally:
                # After the swap it holds the data that was here; before it, the part the import wrote.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._drop_restore_volume(u, stage))
                if not done and not had_slot:
                    self.state.forget_slot(u.name)
            u.data_mount = u.data_bytes = u.data_grew_at = u.data_sampled_at = None
            size = (await asyncio.to_thread(dir_bytes, mnt))[0]
            log.info("%s: its kept data was restored from a backup (%d MiB, %d owners moved)",
                     u.name, size >> 20, moved)
            return {"app_id": aid, "imported": True, "bytes": got, "data_bytes": size,
                    "owners_moved": moved}

    async def _drop_restore_volume(self, u, stage):
        """Delete a restore volume, found by its label and then by name."""
        vols = [v for v in await self.podman.volumes_scoped(C.RESTORE_SCOPE, unit=u.name) if v == stage]
        await self.podman.volume_remove(vols)

    async def _swap_in(self, u, aid, want, had, smnt):
        """Swap the restored tree at smnt in as the app's kept data (made
        empty first when the app had none here); returns its directory. One
        rename that exchanges the two directories, so at every moment the
        volume holds the old data whole or the new whole. When the swap
        cannot be made nothing has changed, and the refusal says so."""
        mnt = await self.podman.volume_ensure(want, u.name, "keep")
        try:
            if not mnt:
                raise OSError(errno.ENOENT, "podman made no volume for it")
            await asyncio.to_thread(exchange_dirs, smnt, mnt)
        except OSError as e:
            if not had:
                with contextlib.suppress(Exception):
                    await self.podman.volume_remove([want])
            raise V.Refusal(500, f"the data of {aid} could not be swapped in ({e.strerror or e}); "
                                 "its data here is as it was") from None
        return mnt

    async def _import_into(self, u, aid, vol, nbytes, reader, writer):
        """podman's import of the Host's frames, through gate_tar, into vol;
        the bytes received, or a refusal (vol then holds part of the tar)."""
        # podman first, and the import known to the machine guard, then
        # the Host is told to send: a guard round from here on stops it.
        # The frames go to gate_tar (a worker thread) through one pipe,
        # and what it writes afresh goes to podman through another.
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        try:
            proc = await self.podman.volume_stream(vol, out=False, stdin=r_out)
        except OSError as e:
            for fd in (r_in, w_in, r_out, w_out):
                os.close(fd)
            raise V.Refusal(500, f"podman could not be started: {e.strerror}") from None
        os.close(r_out)
        fin, fout = os.fdopen(r_in, "rb"), os.fdopen(w_out, "wb")
        frames_open = [True]

        def end_frames():
            if frames_open[0]:
                frames_open[0] = False
                os.close(w_in)

        def run_gate():
            try:
                gate_tar(fin, fout)
                fout.flush()
                return None
            except TarRefused as e:
                return str(e)
            except (tarfile.TarError, EOFError, OSError) as e:
                return f"it is not a whole volume tar ({type(e).__name__})"
            finally:
                fin.close()     # what still comes is dropped (sink sees a broken pipe)

        writing = [None]            # the frame being written to the gate, in a worker thread

        async def finish_gate():
            # podman is stopped first, so a tar cut short is never taken as whole;
            # a frame still being written ends with it (the gate stops and
            # closes its end), and only then is the pipe closed under it.
            if writing[0] is not None and not writing[0].done():
                await asyncio.wait({writing[0]})
            end_frames()
            why = await gate
            with contextlib.suppress(OSError):
                fout.close()
            return why

        err = asyncio.ensure_future(proc.stderr.read())
        gate = asyncio.ensure_future(asyncio.to_thread(run_gate))
        cut = _Cut(proc)
        broken = []
        self.imports.add(cut)
        try:
            writer.write(json.dumps({"ok": True, "data": {"send": nbytes}}, sort_keys=True).encode("utf-8")
                         + b"\n")
            await writer.drain()
        except BaseException:
            self.imports.discard(cut)
            cut.cancel()
            await finish_gate()
            await proc.wait()
            err.cancel()
            raise

        async def sink(data):
            if broken or cut.cut:
                return              # podman or the gate stopped: the rest is read and dropped
            writing[0] = asyncio.ensure_future(asyncio.to_thread(_write_all, w_in, data))
            try:
                # Shielded: cancelled here, the write still runs in its
                # thread, and finish_gate waits for it.
                await asyncio.shield(writing[0])
            except OSError as e:
                broken.append(e)

        # The frames are read to their end whatever happens to podman, so
        # the Host always gets the answer line, never a reset.
        try:
            got = await self._frames_in(reader, nbytes, sink)
        except BaseException:
            cut.cancel()
            await finish_gate()
            await proc.wait()
            err.cancel()
            raise
        finally:
            self.imports.discard(cut)
        if cut.cut:
            await finish_gate()
            await proc.wait()
            err.cancel()
            raise V.Refusal(507, f"putting back the data of {aid} was stopped: this machine's disk "
                                 f"fell under the {DISK_STOP_FLOOR >> 20} MiB it keeps for the Host's "
                                 "database. Free some space and restore again")
        end_frames()
        why = await gate
        if why is not None:
            cut.cancel()
            await finish_gate()
            await proc.wait()
            err.cancel()
            log.warning("%s: a restore of its data was refused: %s", u.name, why[:300])
            raise V.Refusal(400, f"the data of {aid} in the backup was refused: {why}")
        with contextlib.suppress(OSError):
            fout.close()
        rc = await proc.wait()
        tail = " / ".join((await err).decode("utf-8", "replace").strip().splitlines()[-3:])
        if rc != 0:
            raise V.Refusal(500, f"podman's import of the data of {aid} exited {rc}: {tail[:400]}")
        return got

    @staticmethod
    async def _discard(_data):
        return None

    async def stream(self, op, params, reader, writer):
        """A STREAM_OPS operation: its first answer line, its frames, and one
        line with the outcome (a refusal before any frame is that one line)."""
        t0 = self.clock()
        target = str(params.get("app_id") or "")[:64]
        try:
            if op == "workspace.export_app_data":
                data = await self.export_app_data(*V.export_app_data(params), writer)
            else:
                data = await self.import_app_data(*V.import_app_data(params), reader, writer)
            ans = {"ok": True, "data": data}
            log.info("%s %s ok in %.1f s", op, target, self.clock() - t0)
        except V.Refusal as e:
            log.info("%s %s refused %d after %.1f s: %s", op, target, e.code, self.clock() - t0, e.text[:300])
            ans = {"ok": False, "error": e.text, "code": e.code}
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
            log.info("%s %s: the Host's connection ended after %.1f s (%s)", op, target,
                     self.clock() - t0, type(e).__name__)
            return
        except Exception as e:  # noqa: BLE001 — answered, and logged by type
            log.exception("%s %s failed", op, target)
            ans = {"ok": False, "error": f"runtime error: {type(e).__name__}", "code": 500}
        writer.write(json.dumps(ans, sort_keys=True).encode("utf-8") + b"\n")
        await writer.drain()

    # ── stopping ────────────────────────────────────────────────────────────

    async def teardown(self, u, why):
        u.gen += 1
        names = [c.name for c in reversed(u.plan.containers)] if u.plan else C.container_names(u.name)
        await self.podman.remove(names)
        # The unit's per-wake volumes, by both labels and then by name (its
        # plan's, or ones named for it that are not an app's kept data): a
        # release must never take the data, nor another unit's volumes, on how
        # podman combines two filters.
        wake = await self.podman.volumes_scoped("wake", unit=u.name)
        if u.plan:
            mine = {n for n, scope, _uid, _mode in u.plan.volumes if scope == "wake"}
            wake = [v for v in wake if v in mine]
        else:
            wake = [v for v in wake if v.startswith(f"cseries-{u.name}-")
                    and C.app_of_data_volume(v) is None]
        await self.podman.volume_remove(wake)
        self._clear_files(u.name)
        was = u.state
        u.state = "stopped"
        u.cap = None
        u.next_restart = None
        u.stopped_why = why
        if was != "stopped":
            log.info("%s stopped: %s", u.name, why)

    # ── starting ────────────────────────────────────────────────────────────

    async def _cold_start(self, u, plan, slot, creds, deadline, gen0):
        """Create and start the unit's containers and wait for it to be
        ready. Leaves it running and answers "still starting" when the wait
        runs out (the platform's 504 that keeps the pod)."""
        needs = plan.memory_mi
        self._limits(plan)
        why = self.guard.refusal(needs, [x.guard_view() for x in self.live() if x is not u],
                                 list(self.in_flight.values()))
        if why:
            raise V.Refusal(402, why)
        await self._disk_check()
        self.in_flight[u.name] = needs
        # A release does not wait for the unit's lock (release()); it bumps
        # the generation. One that lands after the wake took the lock (gen0)
        # and while it waits for an engine's build, a cold slot, an image or
        # a volume is seen here, before any container runs: without this the
        # start went on and the released unit ran until the idle sweep.
        try:
            async with self.cold:
                await self.ensure_image(plan.image, deadline)
                await self.podman.remove([c.name for c in reversed(plan.containers)])
                self._clear_files(u.name)
                self._write_files(u, plan, slot)
                await self._make_volumes(u, plan, slot)
                if u.gen != gen0:
                    await self.teardown(u, "released while starting")
                    raise V.Refusal(504, "released while starting")
                u.gen += 1
                gen = u.gen
                u.plan, u.creds, u.state = plan, dict(creds), "starting"
                u.started, u.started_wall = self.clock(), time.time()
                u.last_wake = self.clock()
                u.next_restart = None
                for c in plan.containers:
                    self._write_env(c.argv[c.argv.index("--env-file") + 1], c.env)
                    self._note_create()
                    if c.wait:
                        rc, _out, _err = await self.podman.run(
                            c.argv, timeout=max(5, int(deadline - self.clock())), check=False)
                        if rc != 0:
                            await self.teardown(u, f"its fetch step exited {rc}")
                            raise V.Refusal(504, f"terminal: Init:Error (exit {rc})")
                    else:
                        try:
                            await self.podman.run(c.argv, timeout=120)
                        except PodmanError as e:
                            await self.teardown(u, "a container did not start")
                            raise V.Refusal(504, f"terminal: CreateContainerError: {e}") from None
                    if c.name.endswith(C.WALL_SUFFIX):
                        await self._wait_wall(u, plan, gen, deadline)
        finally:
            self.in_flight.pop(u.name, None)
        await self._wait_ready(u, gen, deadline)

    async def _wait_wall(self, u, plan, gen, deadline):
        end = min(deadline, self.clock() + C.WALL_READY_S)
        while not await self.tcp_probe(plan.ip, plan.wall_port):
            if u.gen != gen:
                raise V.Refusal(504, "released while starting")
            if self.clock() >= end:
                st = await self.podman.state(plan.containers[0].name)
                await self.teardown(u, "its egress wall did not start")
                code = (st or {}).get("exit_code")
                raise V.Refusal(504, f"terminal: the egress wall did not start (exit {code})")
            await self.sleep(0.5)

    async def _wait_ready(self, u, gen, deadline):
        while True:
            if u.gen != gen:
                raise V.Refusal(504, "released while starting")
            if await self.http_probe(u.plan.ready_url):
                u.state = "running"
                u.failures = 0
                log.info("%s ready in %.1f s", u.name, self.clock() - u.started)
                return
            st = await self.podman.state(u.main)
            if st is None or not st["running"]:
                code = (st or {}).get("exit_code")
                u.failures += 1
                u.last_exit = "OOMKilled" if (st or {}).get("oom") else code
                if u.failures >= CRASHLOOP_AFTER:
                    await self.teardown(u, f"it exited {u.failures} times while starting")
                    raise V.Refusal(504, f"terminal: CrashLoopBackOff (exit {u.last_exit})")
                await self.sleep(min(restart_wait(u.failures), max(0.0, deadline - self.clock())))
                if u.gen != gen:
                    raise V.Refusal(504, "released while starting")
                await self.podman.run(["podman", "start", u.main], timeout=60, check=False)
            if self.clock() >= deadline:
                raise V.Refusal(504, "still starting")
            await self.sleep(1.0)

    async def _alive(self, u):
        st = await self.podman.state(u.main)
        return bool(st and st["running"])

    # ── the operations ──────────────────────────────────────────────────────

    async def wake_app(self, req):
        desc = self.state.descriptors.get(req.app_id)
        if desc is None:
            raise V.Refusal(404, f"no appRuntimes descriptor for {req.app_id}")
        u = self.unit("app", req.app_id)
        u.resident = desc.resident
        if u.name in self.exporting:
            # Paused while a backup copies its data (export_app_data): the
            # answer a slow start gets, so the Host waits and wakes it again.
            raise V.Refusal(504, "still starting: its data is being copied into a backup")
        async with u.lock:
            if u.state == "running" and await self._alive(u):
                u.last_wake = self.clock()
                out = {"app_rt": req.app_id, "ready": True, "url": u.url()}
                if C.CONNECTOR_CRED in req.creds:
                    out["creds_applied"] = req.creds[C.CONNECTOR_CRED] == u.connector_cap
                return out
            if u.state in ("running", "starting") and u.failures >= CRASHLOOP_AFTER:
                await self.teardown(u, "crash loop")
                raise V.Refusal(504, f"terminal: CrashLoopBackOff (exit {u.last_exit})")
            deadline = self.clock() + desc.wake_timeout_s
            if u.state == "starting" and u.plan:
                u.last_wake = self.clock()
                await self._wait_ready(u, u.gen, deadline)
                return {"app_rt": req.app_id, "ready": True, "url": u.url()}
            gen0 = u.gen
            creds = dict(req.creds)
            sent_cap = creds.get(C.CONNECTOR_CRED)
            if sent_cap is None:
                kept = self.state.connector_cap(req.app_id)
                if kept:
                    creds[C.CONNECTOR_CRED] = kept
            family = C.HELPER_FAMILY if desc.form == "helper" else desc.family
            image = self._image_for_family(family)
            ip, slot = self.state.allocate(u.name)
            build = A.helper_app if desc.form == "helper" else A.process_app
            plan = build(desc, image, slot, A.ip_for(ip), creds, self.paths)
            await self._cold_start(u, plan, slot, creds, deadline, gen0)
            u.connector_cap = creds.get(C.CONNECTOR_CRED)
            if sent_cap:
                self.state.put_connector_cap(req.app_id, sent_cap)
            out = {"app_rt": req.app_id, "ready": True, "url": u.url()}
            if sent_cap:
                out["creds_applied"] = True
            return out

    async def _engine_image(self, hid, deadline):
        if hid == C.BROWSER:
            return self._image_for_family(C.BROWSER_FAMILY)
        _images, src = self.images()
        built = self.state.built.get(hid)
        if built and src and built.get("src_sha256") == src["sha256"]:
            return built["image"]
        if self.builder is None or src is None:
            raise V.Refusal(504, f"terminal: no build of {hid} on this machine")
        return await self.builder.wait(hid, src, deadline)

    def _harness_answer(self, u):
        p = u.plan
        return {"harness": u.ident, "ready": True, "url": u.url(), "slots": p.slots,
                "image": p.image, "cap": u.cap, "memory_limit_bytes": p.memory_mi << 20}

    async def wake_harness(self, req):
        hid = req.harness
        if hid in C.CODING_ENGINES and self.arm64():
            raise V.Refusal(400, f"no arm64 build of {hid}")
        u = self.unit("harness", hid)
        async with u.lock:
            if u.state == "running" and await self._alive(u):
                u.last_wake = self.clock()
                return self._harness_answer(u)
            if u.state in ("running", "starting") and u.failures >= CRASHLOOP_AFTER:
                await self.teardown(u, "crash loop")
                raise V.Refusal(504, f"terminal: CrashLoopBackOff (exit {u.last_exit})")
            deadline = self.clock() + C.HARNESS_WAKE_TIMEOUT_S
            if u.state == "starting" and u.plan:
                u.last_wake = self.clock()
                await self._wait_ready(u, u.gen, deadline)
                return self._harness_answer(u)
            gen0 = u.gen
            # Before the image, which may be a build: refused on a short disk
            # as a container start is (_cold_start checks again).
            await self._disk_check()
            image = await self._engine_image(hid, deadline)
            cap = None if hid == C.BROWSER else secrets.token_urlsafe(32)
            token = self.browser_token() if hid == C.BROWSER else ""
            # A coding engine's address is one the plain proxy admits; the
            # browser's is an app's, which it does not.
            ip, slot = self.state.allocate(u.name, *C.ip_range("harness", hid))
            plan = A.harness(hid, image, slot, A.ip_for(ip), req.creds, cap or "", self.paths,
                             vcpus=req.vcpus, slots=req.slots, browser_token=token)
            u.cap = cap
            await self._cold_start(u, plan, slot, req.creds, deadline, gen0)
            return self._harness_answer(u)

    async def release(self, kind, ident):
        u = self.units.get(C.unit_name(kind, ident))
        answer = {"ready_duration_s": 0.0, "vcpus": 1, "window_id": None, "scaled_to": 0,
                  "billing": "none"}
        if u is None or not self._busy(u):
            return answer
        # Not under the unit's lock: a wake still waiting on a slow start must
        # not hold a release back; it sees the generation change and stops.
        # A start the release cut short never ran: no duration.
        dur = max(0.0, self.clock() - (u.started or self.clock())) if u.state != "stopped" else 0.0
        vcpus = max(1, int(u.plan.cpus)) if u.plan else 1
        await self.teardown(u, "released")
        answer.update(ready_duration_s=round(dur, 1), vcpus=vcpus)
        return answer

    async def set_app_runtime(self, aid, enabled, desc):
        if not enabled:
            u = self.units.get(C.unit_name("app", aid))
            if u is not None and self._busy(u):
                await self.teardown(u, "its app was removed")
            existed = self.state.drop_descriptor(aid)
            return {"app_id": aid, "enabled": False, "removed": existed,
                    "app_runtimes": sorted(self.state.descriptors)}
        needs = desc.budget_mi + (C.WALL_MEM_MI if desc.form == "process" else 0)
        budget = self.guard.budget()
        if needs > budget:
            # The installer reads this the way it reads the operator's.
            raise V.Refusal(402, over_plan_text(needs, budget).replace(
                "/harness/wake", "/set-app-runtime"))
        self.state.put_descriptor(desc)
        return {"app_id": aid, "enabled": True, "removed": False,
                "app_runtimes": sorted(self.state.descriptors)}

    async def set_harness(self, hid, enabled):
        self.state.set_harness(hid, enabled)
        image_state = "image" if hid == C.BROWSER else "none"
        if not enabled:
            u = self.units.get(C.unit_name("harness", hid))
            if u is not None and self._busy(u):
                await self.teardown(u, "its engine was turned off")
        elif hid in C.CODING_ENGINES:
            _images, src = self.images()
            built = self.state.built.get(hid)
            if self.arm64():
                image_state = "unsupported"
            elif built and src and built.get("src_sha256") == src["sha256"]:
                image_state = "built"
            elif self.builder is not None and src is not None:
                self.builder.start(hid, src)
                image_state = "building"
            else:
                image_state = "no-sources"
        return {"harness": hid, "enabled": enabled, "harnesses": sorted(self.state.harnesses),
                "image_state": image_state}

    async def capacity(self, ids, slots):
        units = [u.guard_view() for u in self.live()]
        head = self.guard.headroom(units, list(self.in_flight.values()))
        remaining = head
        fits, refused, granted, images = [], [], {}, {}
        for h in ids:
            u = self.units.get(C.unit_name("harness", h))
            if u is not None and u.state in ("starting", "running") and u.plan:
                fits.append(h)
                granted[h] = u.plan.slots
                images[h] = u.plan.image
                continue
            need = A.harness_memory_mi(h, 1)
            if need > remaining:
                refused.append({"harness": h, "needs_mi": need})
                continue
            if h == C.BROWSER:
                granted[h] = 1
            else:
                extra = (remaining - need) // C.HARNESS_SLOT_MI
                granted[h] = max(1, min(slots.get(h, 1), 1 + extra))
            remaining -= A.harness_memory_mi(h, granted[h])
            fits.append(h)
        return {"gated": True, "fits": fits, "refused": refused, "headroom_mi": head,
                "remaining_mi": remaining, "upgrade_to": None, "pool_add_mi": None,
                "slots": granted, "images": images, "scope": "machine"}

    def status(self):
        now = self.clock()
        images, src = self.images()
        units = [{"name": u.name, "kind": u.kind, "ident": u.ident, "state": u.state,
                  "ip": u.plan.ip if u.plan else None,
                  "memory_mi": u.plan.memory_mi if u.plan else 0,
                  "up_s": round(now - u.started, 1) if u.started and u.state != "stopped" else None,
                  "idle_s": round(now - u.last_wake, 1) if u.last_wake else None,
                  "failures": u.failures, "resident": u.resident, "stopped_why": u.stopped_why}
                 for u in sorted(self.units.values(), key=lambda x: x.name)]
        live = [u.guard_view() for u in self.live()]
        return {"units": units, "total_mi": self.guard.total_mb(), "budget_mi": self.guard.budget(),
                "held_mi": self.guard.held_mb(),
                "headroom_mi": self.guard.headroom(live, list(self.in_flight.values())),
                "images": sorted(images), "harness_src": bool(src),
                "harnesses": sorted(self.state.harnesses),
                "app_runtimes": sorted(self.state.descriptors),
                "built": {h: b.get("src_sha256", "")[:12] for h, b in self.state.built.items()},
                "building": self.builder.building() if self.builder else []}

    async def handle(self, op, params):
        t0 = self.clock()
        target = str(params.get("app_rt") or params.get("harness") or params.get("app_id")
                     or params.get("harness_id") or "")[:64]
        try:
            if op == "harness.wake":
                req = V.wake(params)
                data = await (self.wake_app(req) if isinstance(req, V.AppWake) else self.wake_harness(req))
            elif op == "harness.release":
                data = await self.release(*V.release(params))
            elif op == "harness.capacity":
                data = await self.capacity(*V.capacity(params))
            elif op == "workspace.set_app_runtime":
                data = await self.set_app_runtime(*V.set_app_runtime(params))
            elif op == "workspace.set_harness":
                data = await self.set_harness(*V.set_harness(params))
            elif op == "workspace.app_data":
                V.app_data(params)
                data = await self.app_data()
            elif op == "workspace.delete_app_data":
                data = await self.delete_app_data(*V.delete_app_data(params))
            elif op in V.STREAM_OPS:
                raise V.Refusal(400, f"{op} streams its data: it is answered on a connection of its own")
            else:
                data = self.status()
        except V.Refusal as e:
            log.info("%s %s refused %d after %.1f s: %s", op, target, e.code, self.clock() - t0, e.text[:300])
            return {"ok": False, "error": e.text, "code": e.code}
        except Exception as e:  # noqa: BLE001 — answered, and logged by type
            log.exception("%s %s failed", op, target)
            return {"ok": False, "error": f"runtime error: {type(e).__name__}", "code": 500}
        if op != "runtime.status":
            log.info("%s %s ok in %.1f s", op, target, self.clock() - t0)
        return {"ok": True, "data": data}

    # ── the loops ───────────────────────────────────────────────────────────

    async def sweep_once(self):
        now = self.clock()
        for u in list(self.live()):
            if u.last_wake is not None and now - u.last_wake > C.IDLE_GRACE_S:
                await self.teardown(u, f"idle for {now - u.last_wake:.0f} s")

    async def monitor_once(self):
        """A held container that exited is started again after a growing
        wait; one left starting after a wake gave up is checked for ready."""
        now = self.clock()
        for u in list(self.live()):
            if u.lock.locked() or not u.plan:
                continue
            if u.state == "starting":
                if await self.http_probe(u.plan.ready_url):
                    u.state, u.failures = "running", 0
                    log.info("%s became ready", u.name)
                continue
            if await self._alive(u):
                continue
            if u.next_restart is None:
                st = await self.podman.state(u.main)
                u.failures += 1
                u.last_exit = "OOMKilled" if (st or {}).get("oom") else (st or {}).get("exit_code")
                u.next_restart = now + restart_wait(u.failures)
                log.info("%s exited (%s); starting it again in %.0f s", u.name, u.last_exit,
                         u.next_restart - now)
                continue
            if now < u.next_restart:
                continue
            async with u.lock:
                u.next_restart = None
                gen = u.gen
                if u.kind == "app" and u.plan.wall_port:
                    # The wall owns the network namespace: start them all again.
                    for c in u.plan.containers:
                        if not c.wait:
                            await self.podman.run(["podman", "start", c.name], timeout=60, check=False)
                else:
                    await self.podman.run(["podman", "start", u.main], timeout=60, check=False)
                try:
                    await self._wait_ready(u, gen, self.clock() + 60)
                except V.Refusal as e:
                    log.info("%s did not come back: %s", u.name, e.text)

    async def watchdog_once(self):
        _total, avail = parse_meminfo(read_text(self.guard.meminfo_path))
        psi = parse_psi_full(read_text(self.guard.psi_path))
        disk = disk_free(self.guard.disk_path)
        now = self.clock()
        if disk is not None and disk < DATA_WATCH_BELOW:
            # Whose data grew last decides who the disk floor stops first; at
            # the floor itself it is measured every round.
            await self.sample_data(now, every=0.0 if disk < DISK_STOP_FLOOR else DATA_SAMPLE_EVERY_S)
        if disk is not None and disk < DISK_STOP_FLOOR:
            self.stop_writes()
        victim, why = self.guard.round(now, [u.guard_view() for u in self.live()],
                                       avail_mb=avail, psi=psi, disk=disk)
        if victim is None:
            return None
        u = self.units[victim["name"]]
        st = await self.podman.state(u.main)
        cg = (st or {}).get("cgroup")
        killed = False
        if cg:
            # Through the cgroup, not the container engine, which can take
            # minutes to answer on a machine short of memory.
            try:
                with open(os.path.join("/sys/fs/cgroup", cg.lstrip("/"), "cgroup.kill"), "w") as f:
                    f.write("1")
                killed = True
            except OSError:
                pass
        if not killed:
            await self.podman.kill(u.main)
        log.warning("%s stopped by the machine guard: %s", u.name, why)
        await self.teardown(u, f"the machine guard: {why}")
        return u.name

    async def _loop(self, every, fn):
        while True:
            await asyncio.sleep(every)
            try:
                await fn()
            except Exception:  # noqa: BLE001 — a loop never dies
                log.exception("%s failed", fn.__name__)

    async def startup(self):
        """Nothing this runtime starts should outlive it: the Host wakes again
        what it holds, and every wake is cold after this."""
        leftovers = await self.podman.labelled_containers()
        await self.podman.remove(leftovers)
        await self.podman.volume_remove(await self.podman.volumes_scoped("wake"))
        # A restore cut short by a stop: never the app's data (that was swapped
        # whole or not at all), so the Host's restore runs it again.
        await self.podman.volume_remove(await self.podman.volumes_scoped(C.RESTORE_SCOPE))
        for d in (self.paths.creds_root(), self.paths.env_dir()):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, mode=0o700, exist_ok=True)
        if leftovers:
            log.info("removed %d containers left from before this start", len(leftovers))
        await self.prune_images()

    async def prune_images(self):
        """Remove the images a release left behind: a family's image that
        images.json no longer pins, an engine image built from other sources
        than the pinned ones (or never recorded), and other releases'
        unpacked sources. Only images of the families' repositories and the
        runtime's own builds are looked at, so the Host's, its database's and
        the build bases are never touched; with nothing pinned (images.json
        missing or unreadable) nothing is removed."""
        images, src = self.images()
        cur = src["sha256"] if src else None
        pinned = set(images.values())
        repos = {C.image_repo(r) for r in pinned} | set(C.FAMILY_REPOS)
        keep_built = {b.get("image") for b in self.state.built.values()
                      if cur and b.get("src_sha256") == cur}
        stale = []
        for img in await self.podman.images():
            iid = str(img.get("Id") or "")
            iid = iid[len("sha256:"):] if iid.startswith("sha256:") else iid
            if not V.HEX64_RE.match(iid):
                continue
            names = set(img.get("Names") or []) | set(img.get("RepoDigests") or [])
            if (img.get("Labels") or {}).get(C.LABEL_BUILD):
                if cur and iid not in keep_built:
                    stale.append(iid)
                continue
            ours = {C.image_repo(n) for n in names} & repos
            if not ours or not pinned or names & pinned:
                continue
            digest = img.get("Digest") or ""
            if digest and any(p == f"{C.image_repo(p)}@{digest}" and C.image_repo(p) in ours
                              for p in pinned):
                continue
            stale.append(iid)
        removed = 0
        for iid in stale:
            removed += bool(await self.podman.image_remove(iid))
        if cur:
            bd = self.paths.build_dir()
            try:
                entries = os.listdir(bd)
            except FileNotFoundError:
                entries = []
            for e in entries:
                sha = e[:-len(".tar.gz")] if e.endswith(".tar.gz") else e
                if V.HEX64_RE.match(sha) and sha != cur:
                    path = os.path.join(bd, e)
                    if os.path.isdir(path) and not os.path.islink(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            os.unlink(path)
                        except FileNotFoundError:
                            pass
        if stale:
            log.info("removed %d of %d images no release pin or build record names", removed,
                     len(stale))

    # ── the socket ──────────────────────────────────────────────────────────

    def peer_refusal(self, sock):
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, _gid = struct.unpack("3i", raw)
        except OSError:
            return REFUSED
        if uid != self.peer_uid:
            return REFUSED
        cg = ""
        for line in read_text(f"/proc/{pid}/cgroup").splitlines():
            if line.startswith("0::"):
                cg = line[3:].strip()
        if not cg or not any((cg + "/").startswith(p) for p in self.peer_cgroups):
            return REFUSED
        return None

    async def _conn(self, reader, writer):
        try:
            why = self.peer_refusal(writer.get_extra_info("socket"))
            if why:
                log.warning("a connection was refused: %s", why)
                ans = {"ok": False, "error": why, "code": 403}
            else:
                ans = None
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError:
                    ans = {"ok": False, "error": f"request: longer than {C.MAX_LINE} bytes", "code": 400}
                    await self._discard_line(reader)
                except asyncio.IncompleteReadError as e:
                    line = e.partial
                if ans is None:
                    try:
                        op, params = V.parse_line(line)
                    except V.Refusal as e:
                        ans = {"ok": False, "error": e.text, "code": e.code}
                    else:
                        if op in V.STREAM_OPS:
                            await self.stream(op, params, reader, writer)
                            return
                        ans = await self.handle(op, params)
            writer.write(json.dumps(ans, sort_keys=True).encode("utf-8") + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _discard_line(reader, cap=16 << 20, timeout=5.0):
        """Read and drop the rest of an over-long request, so the refusal
        reaches a peer still writing it (closing with its bytes unread would
        reset the connection under the answer)."""
        seen = 0
        try:
            while seen < cap:
                chunk = await asyncio.wait_for(reader.read(1 << 16), timeout=timeout)
                if not chunk or b"\n" in chunk:
                    return
                seen += len(chunk)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return

    async def serve(self, sock):
        return await asyncio.start_unix_server(self._conn, sock=sock, limit=C.MAX_LINE + 2)

    async def run(self, sock):
        await self.startup()
        server = await self.serve(sock)
        tasks = [asyncio.ensure_future(self._loop(C.SWEEP_EVERY_S, self.sweep_once)),
                 asyncio.ensure_future(self._loop(MONITOR_EVERY_S, self.monitor_once)),
                 asyncio.ensure_future(self._loop(2.0, self.watchdog_once))]
        log.info("listening; %d app runtimes and %d engines known", len(self.state.descriptors),
                 len(self.state.harnesses))
        async with server:
            try:
                await server.serve_forever()
            finally:
                for t in tasks:
                    t.cancel()


def listening_socket(path=None):
    """The socket systemd passed (cseries-runtime.socket), or one made at
    path for a test."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        # Taken once; nothing started from here may think it was handed fds.
        for k in ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES"):
            os.environ.pop(k, None)
        return socket.socket(fileno=3)
    if not path:
        raise SystemExit("cseries-runtime: no socket from systemd and no --socket")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o660)
    s.listen(64)
    return s


def make_runtime():
    from .build import Builder
    builder = Builder(C.Paths(), Podman())
    rt = Runtime(builder=builder)
    builder.bind(rt)
    return rt


def main(argv):
    ap = argparse.ArgumentParser(prog="cseries-runtime")
    ap.add_argument("--socket")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    rt = make_runtime()
    asyncio.run(rt.run(listening_socket(a.socket)))
    return 0
