#!/usr/bin/env python3
"""Coding engines built on the machine (vm/runtime/lib/cseries_runtime/build.py
and vm/runtime/build-harness-src.sh), against a fake podman.

  * the sources tarball is reproducible and holds what the engines'
    Dockerfiles copy;
  * enabling an engine builds it from the pinned tarball; a wake then runs
    exactly the image id the build recorded (built.json);
  * a tarball whose sha256 differs from the pin is refused, and nothing is
    recorded;
  * a tarball holding a link is refused before it is unpacked;
  * an engine off the list is refused; the browser is never built;
  * a second wake joins the build already running (one podman build);
  * a wake that outlasts its window answers "still building";
  * new sources (a release) build again.

Container-free: python3 scripts/test_runtime_build.py
"""
import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/runtime/lib"))
sys.path.insert(0, HERE)
import runtime_fake_podman as fake  # noqa: E402
from cseries_runtime import build as B  # noqa: E402
from cseries_runtime import common as C  # noqa: E402
from cseries_runtime import guard as G  # noqa: E402
from cseries_runtime import server as S  # noqa: E402
from cseries_runtime.podman import Podman  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


# ── the tarball ─────────────────────────────────────────────────────────────

with tempfile.TemporaryDirectory() as tmp:
    sh = os.path.join(ROOT, "vm/runtime/build-harness-src.sh")
    outs = []
    for i in (1, 2):
        d = os.path.join(tmp, f"o{i}")
        r = subprocess.run(["bash", sh, "--version", "1.2.3", "--out", d, "--epoch", "1760000000"],
                           capture_output=True, text=True, check=False)
        check(r.returncode == 0, f"the sources tarball builds ({i})", r.stderr[-300:])
        with open(os.path.join(d, "cseries-harness-src-1.2.3.tar.gz"), "rb") as f:
            outs.append(f.read())
    check(outs[0] == outs[1], "the same tree gives the same bytes")
    sha_line = open(os.path.join(tmp, "o1", "cseries-harness-src-1.2.3.tar.gz.sha256")).read().split()
    check(sha_line[0] == hashlib.sha256(outs[0]).hexdigest(), "its .sha256 is the file's")
    with tarfile.open(fileobj=io.BytesIO(outs[0])) as t:
        names = set(t.getnames())
    need = {"cli/cseries_sidecar.py", "system-apps/_harness_runtime/__main__.py",
            "system-apps/claude-code-harness/bin/main.py", "VERSION", "LICENSE"}
    need |= {f"sidecars/harness-{h}/Dockerfile" for h in C.CODING_ENGINES}
    check(need <= names, "it holds every engine's Dockerfile and what they copy", sorted(need - names))
    copies = set()
    for h in C.CODING_ENGINES:
        for line in open(os.path.join(ROOT, f"sidecars/harness-{h}/Dockerfile")):
            parts = line.split()
            if parts[:1] == ["COPY"] and not any(p.startswith("--from") for p in parts):
                srcs = [p for p in parts[1:-1] if not p.startswith("--")]
                copies.update(s.rstrip("/") for s in srcs)
    missing = [c for c in copies if not any(n == c or n.startswith(c + "/") for n in names)]
    check(not missing, "every path an engine's Dockerfile copies is in the tarball", missing)
    TARBALL = outs[0]
    TARSHA = hashlib.sha256(TARBALL).hexdigest()


# ── building ────────────────────────────────────────────────────────────────

def world(tmp, served):
    paths = C.Paths(tmp)
    os.environ["FAKE_PODMAN_STATE"] = os.path.join(tmp, "podman.json")
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    exe = fake.install(bindir)
    fetched = []

    def fetch(url, path, max_bytes):
        fetched.append(url)
        data = served[url]
        with open(path, "wb") as f:
            f.write(data)
        return hashlib.sha256(data).hexdigest()
    podman = Podman(exe)
    b = B.Builder(paths, podman, fetch=fetch)
    rt = S.Runtime(paths, podman, G.Guard(total_mb=16384, disk_path=tmp),
                   http_probe=lambda url: asyncio.sleep(0, result=True),
                   tcp_probe=lambda ip, port: asyncio.sleep(0, result=True),
                   chown=lambda *a: None, machine="x86_64", builder=b)
    b.bind(rt)
    return paths, rt, b, fetched


def images_json(paths, url, sha):
    os.makedirs(paths.etc, exist_ok=True)
    with open(paths.images_json(), "w") as f:
        json.dump({"images": {}, "harness_src": {"url": url, "sha256": sha}}, f)


def builds(tmp):
    return [c for c in fake.state(os.path.join(tmp, "podman.json"))["calls"] if c[:1] == ["build"]]


async def scenario():
    url = "https://example.test/cseries-harness-src-1.2.3.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        paths, rt, b, fetched = world(tmp, {url: TARBALL})
        images_json(paths, url, TARSHA)
        s = await rt.handle("workspace.set_harness", {"harness_id": "opencode", "enabled": True})
        check(s["ok"] and s["data"]["image_state"] == "building", "enabling an engine starts its build", s)
        a, c = await asyncio.gather(rt.handle("harness.wake", {"harness": "opencode"}),
                                    rt.handle("harness.wake", {"harness": "opencode"}))
        check(a["ok"] and c["ok"] and len(builds(tmp)) == 1 and fetched == [url],
              "two wakes join the one build; the sources are fetched once", (a, len(builds(tmp))))
        bargs = builds(tmp)[0]
        ctx = os.path.join(paths.build_dir(), TARSHA)
        check(bargs[-1] == ctx and bargs[bargs.index("-f") + 1] == f"{ctx}/sidecars/harness-opencode/Dockerfile"
              and "-v" not in bargs and "--volume" not in bargs,
              "the build runs the engine's own Dockerfile from the unpacked sources, nothing mounted", bargs)
        # The build's RUN steps (apt, npm, an install script) run third-party
        # code: on the runtime's network, through the machine's plain proxy,
        # as an engine does at run time. podman's default network would reach
        # anything routed, and needs a resolver the box's firewall may drop.
        bargv = " ".join(bargs)
        check("--network" in bargs and bargs[bargs.index("--network") + 1] == C.NETWORK and
              "--network host" not in bargv and
              all(f"--build-arg {k}={C.SQUID_URL}" in bargv
                  for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")) and
              f"--build-arg NO_PROXY=localhost,127.0.0.1,::1,{C.HOST_IP}" in bargv,
              "the build runs on the runtime's network, through the machine's plain proxy", bargs)
        # Its RUN steps compile and install for minutes: in the containers'
        # slice, whose CPU weight is under the Host's, never beside the Host.
        check(bargs[bargs.index("--cgroup-parent") + 1] == getattr(C, "CGROUP_PARENT", None)
              if "--cgroup-parent" in bargs else False,
              "the build runs in the runtime's containers' slice", bargs)
        # Those RUN steps run as root: with every capability dropped but the
        # ones the pinned Dockerfiles use, and in a user namespace on ids no
        # container is given (slot 0; State.allocate starts at 1).
        base = C.UID_BASE
        caps = bargs[bargs.index("--cap-add") + 1].split(",") if "--cap-add" in bargs else []
        check(getattr(B, "HARDEN_BUILDS", False) and "--cap-drop" in bargs and bargs[bargs.index("--cap-drop") + 1] == "ALL" and
              sorted(caps) == ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID"] and
              bargs.index("--cap-drop") < bargs.index("--cap-add") and
              bargs[bargs.index("--userns") + 1] == "container" and
              bargs[bargs.index("--userns-uid-map") + 1] == f"0:{base}:{C.UID_RANGE}" and
              bargs[bargs.index("--userns-gid-map") + 1] == f"0:{base}:{C.UID_RANGE}" and
              "--privileged" not in bargs and "--cap-add=ALL" not in bargv and "SYS_ADMIN" not in bargv,
              "the build's RUN steps keep only the capabilities its Dockerfile needs, in a user namespace",
              bargs)
        slot0 = getattr(B, "BUILD_ID_SLOT", 0)
        check(all(a["slot"] != slot0 for a in rt.state.alloc.values()) and
              rt.state.allocate("app-probe")[1] != slot0,
              "no container is ever given the builds' id range", rt.state.alloc)
        if hasattr(B, "hardening_args"):
            B.HARDEN_BUILDS = False
            check(B.hardening_args() == [], "HARDEN_BUILDS off gives the old flags back")
            B.HARDEN_BUILDS = True
        rec = json.load(open(paths.built_json()))
        check(rec["opencode"]["image"] == "e" * 64 and rec["opencode"]["src_sha256"] == TARSHA,
              "built.json records the image id and the sources it came from", rec)
        check(a["data"]["image"] == "e" * 64, "the wake runs the recorded image id", a["data"])
        runs = [x for x in fake.state(os.path.join(tmp, "podman.json"))["calls"] if x[:1] == ["run"]]
        check(runs and runs[-1][-1] == "e" * 64, "the podman run names the id, not a tag", runs[-1][-3:])
        s = await rt.handle("workspace.set_harness", {"harness_id": "opencode", "enabled": True})
        check(s["data"]["image_state"] == "built" and len(builds(tmp)) == 1, "a built engine is not built again")
        s = await rt.handle("workspace.set_harness", {"harness_id": "browser", "enabled": True})
        check(s["ok"] and s["data"]["image_state"] == "image" and len(builds(tmp)) == 1,
              "the browser is an image, never built")
        s = await rt.handle("workspace.set_harness", {"harness_id": "vscode", "enabled": True})
        check(not s["ok"] and s["code"] == 400, "an engine off the list is refused", s)

        # New sources (a release) the machine cannot fetch: the engine's next
        # cold wake builds nothing and does not fall back to the older image.
        await rt.handle("harness.release", {"harness": "opencode"})
        images_json(paths, "https://example.test/cseries-harness-src-1.2.4.tar.gz", "7" * 64)
        a = await rt.handle("harness.wake", {"harness": "opencode"})
        check(not a["ok"] and a["code"] == 504 and "terminal" in a["error"] and len(builds(tmp)) == 1,
              "new sources that cannot be fetched build nothing and run nothing older", a)

    with tempfile.TemporaryDirectory() as tmp:
        paths, rt, b, fetched = world(tmp, {url: TARBALL})
        images_json(paths, url, "0" * 64)
        a = await rt.handle("harness.wake", {"harness": "codex"})
        check(not a["ok"] and a["code"] == 504 and "hash to" in a["error"] and not builds(tmp) and
              not os.path.exists(paths.built_json()),
              "sources whose sha256 is not the pin are refused, and nothing is built or recorded", a)

    with tempfile.TemporaryDirectory() as tmp:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            ti = tarfile.TarInfo("sidecars/harness-codex/Dockerfile")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/shadow"
            t.addfile(ti)
        bad = buf.getvalue()
        paths, rt, b, fetched = world(tmp, {url: bad})
        images_json(paths, url, hashlib.sha256(bad).hexdigest())
        a = await rt.handle("harness.wake", {"harness": "codex"})
        check(not a["ok"] and "not a file or a directory" in a["error"] and not builds(tmp),
              "sources holding a link are refused before anything is unpacked", a)

    with tempfile.TemporaryDirectory() as tmp:
        paths, rt, b, fetched = world(tmp, {url: TARBALL})
        images_json(paths, url, TARSHA)
        gate = asyncio.Event()
        real = b._build

        async def slow(hid, src):
            await gate.wait()
            return await real(hid, src)
        b._build = slow
        t = b.start("kimi", {"url": url, "sha256": TARSHA})
        try:
            await b.wait("kimi", {"url": url, "sha256": TARSHA}, deadline=b.clock() + 0.2)
            r = None
        except S.V.Refusal as e:
            r = (e.code, e.text)
        check(r == (504, "still building"), "a wake that outlasts its window answers still building", r)
        gate.set()
        await t

    # A build writes its layers to the disk the Host's database is on: on a
    # short disk it never starts, from a wake or from enabling the engine, and
    # one in flight when the disk falls under the stop floor is stopped.
    with tempfile.TemporaryDirectory() as tmp:
        paths, rt, b, fetched = world(tmp, {url: TARBALL})
        images_json(paths, url, TARSHA)
        real_statvfs = os.statvfs

        class Short:
            f_frsize = 4096
            f_bavail = (1500 << 20) // 4096
        os.statvfs = lambda p: Short()
        try:
            a = await rt.handle("harness.wake", {"harness": "codex"})
            check(not a["ok"] and a["code"] == 507 and "2 GiB" in a["error"] and not builds(tmp) and
                  not fetched, "on a short disk a wake for an engine is refused before any build", a)
            s = await rt.handle("workspace.set_harness", {"harness_id": "kimi", "enabled": True})
            try:
                await b.tasks[("kimi", TARSHA)]
            except Exception:  # noqa: BLE001
                pass
            check(s["ok"] and not builds(tmp) and not fetched and
                  "2 GiB" in b.errors.get(("kimi", TARSHA), ""),
                  "and enabling one does not start its build", (s, b.errors))
        finally:
            os.statvfs = real_statvfs
        gate = asyncio.Event()
        real = b._build

        async def stuck(hid, src):
            await gate.wait()
            return await real(hid, src)
        b._build = stuck
        b.errors.clear()
        b.tasks.clear()
        b.start("grok", {"url": url, "sha256": TARSHA})
        waiter = asyncio.ensure_future(b.wait("grok", {"url": url, "sha256": TARSHA},
                                              deadline=b.clock() + 30))
        await asyncio.sleep(0)
        n = rt.stop_writes()
        try:
            await asyncio.wait_for(waiter, 5)
            r = None
        except S.V.Refusal as e:
            r = (e.code, e.text)
        check(n == 1 and r and r[0] == 504 and "stopped" in r[1] and "disk" in r[1],
              "a build in flight is stopped by the machine guard, and its wake answered", (n, r))

    # Cancelling a podman call (that pull or build) kills its process, so it
    # stops writing. A stand-in process: nothing real is started or signalled.
    import cseries_runtime.podman as PM

    class Proc:
        killed = False
        returncode = None

        async def communicate(self):
            await asyncio.sleep(3600)

        def kill(self):
            Proc.killed = True

    async def fake_exec(*a, **kw):
        return Proc()
    real_exec = PM.asyncio.create_subprocess_exec
    PM.asyncio.create_subprocess_exec = fake_exec
    try:
        t = asyncio.ensure_future(PM.Podman("podman").run(["podman", "pull", "x"], timeout=3600))
        await asyncio.sleep(0.01)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    finally:
        PM.asyncio.create_subprocess_exec = real_exec
    check(Proc.killed, "a cancelled podman call kills its process")


asyncio.run(scenario())

print()
print("FAIL" if failed else "all runtime build checks passed")
sys.exit(failed)
