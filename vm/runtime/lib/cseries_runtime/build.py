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
import shutil
import tarfile
import time
import urllib.request

from . import common as C
from . import validate as V

log = logging.getLogger("cseries-runtime")

MAX_TARBALL = 128 << 20
BUILD_TIMEOUT_S = 3600

# ── what a build's RUN steps may do ─────────────────────────────────────────
#
# The RUN steps are third-party code (apt's maintainer scripts, a vendor's
# install script, pip), run as root inside the build. Without these flags podman
# gives them its default capability set and no user namespace, so that root is
# the machine's root, with the capabilities containers.conf lists by default
# (CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, NET_BIND_SERVICE, SETFCAP,
# SETGID, SETPCAP, SETUID, SYS_CHROOT).
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
# User namespace: the RUN steps run in one, with the build's root mapped to
# host ids nothing else uses. `--userns=auto` is a podman run/create option;
# podman build's --userns takes "", "container", "host" or a namespace path
# (podman-build(1)), and maps ids with --userns-uid-map/--userns-gid-map, so
# the build gets the same effect that way: slot 0 of the runtime's id ranges
# (common.UID_BASE), which State.allocate never hands to a container (its
# slots start at 1).
#
# HARDEN_BUILDS turns both on. Whether the builds still work this way is proved
# on a real machine, not here: the VM harness builds opencode and wakes it
# (vm/cloud/test/run-vm-test.sh, R4), and the other six Dockerfiles take the same
# kinds of step (claude-code's pip install aside). Should a build fail under
# them, setting this to False restores the old flags and changes nothing else.
HARDEN_BUILDS = True
BUILD_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID")
BUILD_ID_SLOT = 0


def hardening_args():
    """The podman build flags HARDEN_BUILDS turns on (empty when it is off)."""
    if not HARDEN_BUILDS:
        return []
    base = C.UID_BASE + BUILD_ID_SLOT * C.UID_RANGE
    return ["--cap-drop", "ALL", "--cap-add", ",".join(BUILD_CAPS),
            "--userns", "container",
            "--userns-uid-map", f"0:{base}:{C.UID_RANGE}",
            "--userns-gid-map", f"0:{base}:{C.UID_RANGE}"]


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


class Builder:
    def __init__(self, paths, podman, fetch=fetch_to_file, clock=time.monotonic):
        self.paths = paths
        self.podman = podman
        self.fetch = fetch
        self.clock = clock
        self.tasks = {}          # (engine, sources sha256) -> Task
        self.errors = {}         # (engine, sources sha256) -> last failure
        self.state = None
        self.guard = None
        self._src_lock = asyncio.Lock()

    def bind(self, runtime):
        self.state = runtime.state
        self.guard = runtime.guard

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
            iid = os.path.join(self.paths.build_dir(), f"{hid}.iid")
            try:
                os.unlink(iid)
            except FileNotFoundError:
                pass
            tag = f"localhost/cseries-harness-{hid}:{src['sha256'][:12]}"
            log.info("building %s from the release's sources", hid)
            # The RUN steps (apt, npm, install scripts) are third-party code:
            # on the runtime's network, through the machine's plain proxy, as
            # the engine itself runs. The proxy settings are build arguments,
            # so they are not kept in the image.
            no_proxy = f"localhost,127.0.0.1,::1,{C.HOST_IP}"
            proxy = []
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                proxy += ["--build-arg", f"{k}={C.SQUID_URL}"]
            for k in ("NO_PROXY", "no_proxy"):
                proxy += ["--build-arg", f"{k}={no_proxy}"]
            # In the containers' slice, under the Host's CPU weight: a build
            # compiles and installs for minutes (common.CGROUP_PARENT).
            await self.podman.run(["podman", "build", "--pull=missing", "--layers",
                                   "--cgroup-parent", C.CGROUP_PARENT,
                                   "--network", C.NETWORK, *proxy, *hardening_args(),
                                   "--label", f"{C.LABEL_BUILD}={hid}",
                                   "-f", dockerfile, "-t", tag, "--iidfile", iid, ctx],
                                  timeout=BUILD_TIMEOUT_S)
            with open(iid, encoding="ascii") as f:
                image = f.read().strip()
            image = image[len("sha256:"):] if image.startswith("sha256:") else image
            if not V.HEX64_RE.match(image):
                raise ValueError("the build gave no image id")
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
