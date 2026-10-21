"""The coding engines' images, built on this machine.

Their images are never published (their vendors' binaries are not ours to
redistribute), so the release ships the sources the engines' Dockerfiles
copy, as one tarball, and pins its sha256 in the signed release manifest,
from which the machine's image table takes it. Enabling an engine
(workspace.set_harness) builds that one engine: the tarball is fetched once,
checked against the pinned sha256, unpacked (files and directories only),
and `podman build` runs on the engine's own Dockerfile with nothing of the
machine mounted. Every vendor download in those Dockerfiles is checked
against its own checksum; the system packages they install are not, so a
build's digest cannot be known in advance, and a wake runs only the image id
this machine recorded for these sources (built.json), never a name.

A wake for an engine still building waits for the build inside its own
window, then answers "still building"; a second wake joins the build already
running.
"""

import asyncio
import hashlib
import logging
import os
import pwd
import shutil
import tarfile
import time
import urllib.request

from . import common as C
from . import validate as V
from .podman import Podman, PodmanError, child_env

log = logging.getLogger("cseries-runtime")

MAX_TARBALL = 128 << 20
BUILD_TIMEOUT_S = 3600

# ── what a build's RUN steps may do ─────────────────────────────────────────
#
# The RUN steps are third-party code (apt's maintainer scripts, a vendor's
# install script, pip), run as root inside the build. Without these flags podman
# gives them the capabilities containers.conf lists by default (CHOWN,
# DAC_OVERRIDE, FOWNER, FSETID, KILL, NET_BIND_SERVICE, SETFCAP, SETGID,
# SETPCAP, SETUID, SYS_CHROOT).
#
# Capabilities: every one is dropped and only what the pinned Dockerfiles'
# steps use is given back (sidecars/harness-*/Dockerfile: apt-get install,
# curl, tar, chmod, useradd -m, chown -R, pip install):
#   CHOWN, FOWNER, FSETID   dpkg unpacking packages and setting their owners
#                           and modes; useradd -m; chown -R to the engine's user
#   DAC_OVERRIDE            apt as root reading its _apt-owned 0700 partial/
#                           directories
#   SETUID, SETGID          apt dropping to its _apt user to download
#   KILL                    apt stopping its own download methods, which run
#                           as _apt
# Not given back: NET_BIND_SERVICE (nothing listens), SETFCAP (no step sets
# file capabilities), SETPCAP and SYS_CHROOT (no step changes its capability
# set or chroots).
#
# Who runs them: the build user (common.BUILD_USER), with rootless podman
# against a store of its own under its home. A RUN step's root is that user's
# uid on the machine, its other ids the user's subordinate range (slot 0 of
# the runtime's id ranges), so a step that escaped its container is an
# unprivileged user that owns nothing of the Host's. The capabilities above
# are then capabilities in that user namespace only, and every step runs with
# no new privileges (a setuid program in a step gains nothing).
#
# Root's own podman is not used to build with a user namespace: on podman
# 5.4.2 with crun (Debian 13) a build mapped with --userns-uid-map commits each
# RUN step's layer with the machine's ids, so the next step cannot start
# ("did not get container create message from subprocess: EOF"; measured in
# the cloud VM). Rootless podman maps the ids itself, the way it is made to.
#
# The finished image goes to root's store, where the runtime runs it: podman
# save as the build user into podman load as root, both podman's own, then
# root's copy is checked to have the image id the build wrote (the sha256 of
# its config, which names every layer's digest). The build user's store is
# emptied before and after each build, so no layer outlives the build that
# made it and the disk holds each engine's image once.
#
# Its network: a namespace of its own (pasta). The firewalls (door.nft,
# runtime.nft) let the build user's sockets, which carry every RUN step's
# traffic and podman's own pulls, reach the machine's plain proxy on the
# runtime's gateway and nothing else; the proxy refuses private ranges.
#
# HARDEN_BUILDS turns the capability and no-new-privileges flags on. The VM
# harness builds opencode and claude-code this way and wakes them
# (vm/cloud/test/run-vm-test.sh, R4). Should a build fail under them, setting
# this to False gives podman's defaults back, still as the build user, and
# changes nothing else.
HARDEN_BUILDS = True
BUILD_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID")


def hardening_args():
    """The podman build flags HARDEN_BUILDS turns on (empty when it is off)."""
    if not HARDEN_BUILDS:
        return []
    return ["--cap-drop", "ALL", "--cap-add", ",".join(BUILD_CAPS),
            "--security-opt", "no-new-privileges"]


BUILD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def podman_as_build_user(paths, binary="podman"):
    """podman as the build user, in the builds' slice, against its own store.
    Its own pulls (the Dockerfiles' base images) go through the plain proxy,
    the one way out its firewall rule leaves it."""
    run = paths.build_run()
    no_proxy = "localhost,127.0.0.1,::1"
    env = {"PATH": BUILD_PATH, "HOME": paths.build_home(), "XDG_RUNTIME_DIR": run,
           "LANG": "C.UTF-8", "NO_PROXY": no_proxy, "no_proxy": no_proxy}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[k] = C.SQUID_URL
    wrap = ["systemd-run", "--scope", "--quiet", f"--slice={C.BUILD_SLICE}",
            f"--uid={C.BUILD_UID}", f"--gid={C.BUILD_UID}", "--"]
    # cgroupfs: the build user has no systemd user manager to ask for
    # cgroups, so its containers stay in the slice its scope is in.
    glob = ["--root", paths.build_store(), "--runroot", os.path.join(run, "storage"),
            "--cgroup-manager", "cgroupfs"]
    return Podman(binary, wrap=wrap, global_args=glob, env=env, cwd=paths.build_home())


async def run_quiet(argv, timeout=120):
    """A system command (systemctl), its exit status; never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env=child_env())
    except OSError:
        return -1
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1


def fetch_to_file(url, path, max_bytes):
    """Download url to path; its sha256. http and https only."""
    if not url.startswith(("https://", "http://")):
        raise ValueError("the engines' sources come only from http(s)")
    h = hashlib.sha256()
    n = 0
    req = urllib.request.Request(url, headers={"User-Agent": "cseries-runtime/1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > max_bytes:
                raise ValueError(f"the engines' sources are larger than {max_bytes} bytes")
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()


def unpack(tgz, dest):
    """Unpack the sources: files and directories only, nothing outside dest."""
    tmp = dest + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(tgz, "r:gz") as t:
        for m in t.getmembers():
            if not (m.isfile() or m.isdir()):
                raise ValueError(f"the engines' sources hold {m.name!r}, which is not a file or a directory")
        t.extractall(tmp, filter="data")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)


HANDOVER_TIMEOUT_S = 1800


def has_subids(path, user=C.BUILD_USER, first=C.BUILD_SUBIDS[0], count=C.BUILD_SUBIDS[1]):
    """Whether /etc/subuid (or subgid) gives `user` exactly its range."""
    try:
        with open(path, encoding="utf-8") as f:
            return any(line.strip() == f"{user}:{first}:{count}" for line in f)
    except OSError:
        return False


class Builder:
    def __init__(self, paths, podman, fetch=fetch_to_file, clock=time.monotonic,
                 build_podman=None, system=run_quiet, chown=os.chown, user_of=pwd.getpwuid):
        self.paths = paths
        self.podman = podman           # root's: where the runtime runs images
        self.bpodman = build_podman if build_podman is not None else podman_as_build_user(
            paths, podman.binary)     # the build user's
        self.fetch = fetch
        self.clock = clock
        self.system = system
        self.chown = chown
        self.user_of = user_of
        self.tasks = {}          # (engine, sources sha256) -> Task
        self.errors = {}         # (engine, sources sha256) -> last failure
        self.state = None
        self.guard = None
        self._src_lock = asyncio.Lock()
        # One podman build at a time on the machine: every build uses the
        # build user's one store, emptied before and after it, and the one
        # builds' slice, stopped after it. (Before the build user, root's
        # builds collided too: podman 5.4.2 with crun ran every build's RUN
        # step in one unit named after --cgroup-parent.)
        self._build_lock = asyncio.Lock()

    def bind(self, runtime):
        self.state = runtime.state
        self.guard = runtime.guard
        if self.guard is not None and hasattr(self.guard, "watch_disk"):
            # A build's layers are written under the build user's home: the
            # disk floors hold there too.
            self.guard.watch_disk(self.paths.build_home())

    def prepare(self):
        """The build user, its subordinate ids, its home and runtime
        directory as it needs them; a ValueError naming what is missing."""
        try:
            name = self.user_of(C.BUILD_UID).pw_name
        except KeyError:
            name = None
        if name != C.BUILD_USER:
            raise ValueError(f"this machine has no build user {C.BUILD_USER} with uid {C.BUILD_UID} "
                             "(the guest's installer makes it: cseries-build-user)")
        for f in ("etc/subuid", "etc/subgid"):
            if not has_subids(self.paths.p(f)):
                raise ValueError(f"/{f} gives {C.BUILD_USER} no range {C.BUILD_SUBIDS[0]}:"
                                 f"{C.BUILD_SUBIDS[1]} (cseries-build-user makes it)")
        for d in (self.paths.build_home(), self.paths.build_run()):
            os.makedirs(d, mode=0o700, exist_ok=True)
            self.chown(d, C.BUILD_UID, C.BUILD_UID)
            os.chmod(d, 0o700)

    async def empty_store(self):
        """The build user's store emptied: nothing a build made is kept for
        the next one (no layer cache to reuse), and its disk is free again."""
        rc, _, err = await self.bpodman.run(["podman", "system", "reset", "--force"], timeout=600,
                                            check=False)
        if rc != 0:
            log.warning("emptying the build user's store failed: %s", err.strip()[-300:])
        return rc == 0

    async def hand_over(self, tag, image):
        """The build user's image into root's store: podman save into podman
        load, then root's copy checked to be the image id the build wrote."""
        r, w = os.pipe()
        procs = []
        try:
            try:
                procs.append(await asyncio.create_subprocess_exec(
                    *self.bpodman.argv(["podman", "save", "--format", "docker-archive", tag]),
                    stdin=asyncio.subprocess.DEVNULL, stdout=w, stderr=asyncio.subprocess.PIPE,
                    env=self.bpodman.proc_env(), cwd=self.bpodman.cwd))
            finally:
                os.close(w)
            try:
                procs.append(await asyncio.create_subprocess_exec(
                    *self.podman.argv(["podman", "load", "--quiet"]),
                    stdin=r, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=self.podman.proc_env(), cwd=self.podman.cwd))
            finally:
                os.close(r)
            save, load = procs
            (_, serr), (_, lerr) = await asyncio.wait_for(
                asyncio.gather(save.communicate(), load.communicate()), timeout=HANDOVER_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            for p in procs:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
            if isinstance(e, asyncio.CancelledError):
                raise
            raise ValueError(f"handing the image to the runtime took over {HANDOVER_TIMEOUT_S} s") from None
        if save.returncode != 0 or load.returncode != 0:
            raise PodmanError(["podman", "save" if save.returncode else "load"],
                              save.returncode or load.returncode,
                              (serr if save.returncode else lerr).decode("utf-8", "replace"))
        got = await self.podman.image_id(tag)
        got = got[len("sha256:"):] if got.startswith("sha256:") else got
        if got != image:
            await self.podman.image_remove(tag)
            raise ValueError(f"the runtime's store holds {tag} as {got[:12] or 'nothing'}, "
                             f"the build made {image[:12]}")

    def cancel_all(self):
        """Cancel every build in flight (the machine guard, on a short disk);
        podman.run kills each one's podman build. How many were cancelled."""
        n = 0
        for t in self.tasks.values():
            if not t.done():
                t.cancel()
                n += 1
        return n

    def building(self):
        return sorted(h for (h, _s), t in self.tasks.items() if not t.done())

    def start(self, hid, src):
        if hid not in C.CODING_ENGINES:
            raise V.Refusal(400, f"harness: {hid} is not an engine this machine builds")
        key = (hid, src["sha256"])
        t = self.tasks.get(key)
        if t is None or (t.done() and key in self.errors):
            self.errors.pop(key, None)
            t = self.tasks[key] = asyncio.ensure_future(self._build(hid, src))
        return t

    async def wait(self, hid, src, deadline):
        t = self.start(hid, src)
        try:
            return await asyncio.wait_for(asyncio.shield(t), timeout=max(1.0, deadline - self.clock()))
        except asyncio.TimeoutError:
            raise V.Refusal(504, "still building") from None
        except asyncio.CancelledError:
            if not t.cancelled():
                raise                      # this wake itself was cancelled
            raise V.Refusal(504, f"terminal: the build of {hid} was stopped: the disk was "
                                 "short") from None
        except V.Refusal:
            raise
        except Exception as e:  # noqa: BLE001 — answered terminal with what failed
            raise V.Refusal(504, f"terminal: the build of {hid} failed: {e}") from None

    async def sources(self, src):
        """The unpacked sources for this sha256, fetched and checked once."""
        sha = src["sha256"]
        ctx = os.path.join(self.paths.build_dir(), sha)
        marker = os.path.join(ctx, ".sources-sha256")
        async with self._src_lock:
            try:
                with open(marker, encoding="ascii") as f:
                    if f.read().strip() == sha:
                        return ctx
            except FileNotFoundError:
                pass
            os.makedirs(self.paths.build_dir(), mode=0o700, exist_ok=True)
            tgz = ctx + ".tar.gz"
            got = await asyncio.to_thread(self.fetch, src["url"], tgz, MAX_TARBALL)
            if got != sha:
                os.unlink(tgz)
                raise ValueError(f"the engines' sources hash to {got}, the release pins {sha}")
            await asyncio.to_thread(unpack, tgz, ctx)
            os.unlink(tgz)
            with open(marker, "w", encoding="ascii") as f:
                f.write(sha + "\n")
            return ctx

    async def _build(self, hid, src):
        key = (hid, src["sha256"])
        t0 = self.clock()
        try:
            # A build writes its layers to the disk the Host's database is on:
            # on a short disk it does not start (guard.DISK_WAKE_FLOOR).
            free = self.guard.disk_short() if self.guard is not None else None
            if free is not None:
                raise ValueError(self.guard.disk_text(free))
            ctx = await self.sources(src)
            dockerfile = os.path.join(ctx, "sidecars", f"harness-{hid}", "Dockerfile")
            if not os.path.isfile(dockerfile):
                raise ValueError(f"the sources have no Dockerfile for {hid}")
            tag = f"localhost/cseries-harness-{hid}:{src['sha256'][:12]}"
            log.info("building %s from the release's sources", hid)
            # The RUN steps (apt, npm, install scripts) are third-party code:
            # through the machine's plain proxy, as the engine itself runs.
            # The proxy settings are build arguments, so they are not kept in
            # the image.
            no_proxy = f"localhost,127.0.0.1,::1,{C.HOST_IP}"
            proxy = []
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                proxy += ["--build-arg", f"{k}={C.SQUID_URL}"]
            for k in ("NO_PROXY", "no_proxy"):
                proxy += ["--build-arg", f"{k}={no_proxy}"]
            async with self._build_lock:
                image = await self._build_as_user(hid, ctx, tag, proxy)
            if self.state is not None:
                self.state.record_build(hid, image, src["sha256"],
                                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            log.info("built %s in %.0f s", hid, self.clock() - t0)
            return image
        except asyncio.CancelledError:
            self.errors[key] = "cancelled: the disk was short"
            log.warning("the build of %s was cancelled: the disk was short", hid)
            raise
        except Exception as e:
            self.errors[key] = str(e)
            log.warning("the build of %s failed: %s", hid, e)
            raise

    async def _build_as_user(self, hid, ctx, tag, proxy):
        """One build as the build user, its image handed to root's store; the
        image id. The build user's store is emptied and its slice stopped
        afterwards, whatever happened."""
        self.prepare()
        run = self.paths.build_run()
        # The sources, copied where the build user can read them: root's
        # state directory stays root's alone.
        bctx = os.path.join(self.paths.build_home(), "context")
        shutil.rmtree(bctx, ignore_errors=True)
        await asyncio.to_thread(shutil.copytree, ctx, bctx, symlinks=True)
        # Its own copy, so the runtime's umask (0077) does not keep it out.
        for d, dirs, files in os.walk(bctx):
            for n in [d, *(os.path.join(d, f) for f in files)]:
                self.chown(n, C.BUILD_UID, C.BUILD_UID)
        dockerfile = os.path.join(bctx, "sidecars", f"harness-{hid}", "Dockerfile")
        iid = os.path.join(run, f"{hid}.iid")
        try:
            os.unlink(iid)
        except FileNotFoundError:
            pass
        try:
            await self.empty_store()
            # Its own network namespace (pasta); the firewalls leave the
            # build user only the plain proxy (see above). In the builds'
            # slice, under the Host's CPU weight (the wrapper's --slice): a
            # build compiles and installs for minutes.
            await self.bpodman.run(["podman", "build", "--pull=missing", "--layers",
                                    "--network", "pasta", *proxy, *hardening_args(),
                                    "--label", f"{C.LABEL_BUILD}={hid}",
                                    "-f", dockerfile, "-t", tag, "--iidfile", iid, bctx],
                                   timeout=BUILD_TIMEOUT_S)
            with open(iid, encoding="ascii") as f:
                image = f.read().strip()
            image = image[len("sha256:"):] if image.startswith("sha256:") else image
            if not V.HEX64_RE.match(image):
                raise ValueError("the build gave no image id")
            await self.hand_over(tag, image)
            return image
        finally:
            await self.empty_store()
            await self.system(["systemctl", "stop", C.BUILD_SLICE])
            shutil.rmtree(bctx, ignore_errors=True)
