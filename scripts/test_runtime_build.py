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

import types  # noqa: E402

USERS = {}
system_calls = []


def world(tmp, served):
    USERS.clear()
    USERS[getattr(C, "BUILD_UID", -1)] = types.SimpleNamespace(pw_name=getattr(C, "BUILD_USER", ""))
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
    # The build user's podman as podman_as_build_user makes it, without the
    # wrapper that turns into that user (systemd-run; checked on its own).
    if not hasattr(B, "podman_as_build_user"):     # code from before the build user
        b = B.Builder(paths, podman, fetch=fetch)
        rt = S.Runtime(paths, podman, G.Guard(total_mb=16384, disk_path=tmp, held=lambda: 0, psi_path=os.devnull),
                       http_probe=lambda url: asyncio.sleep(0, result=True),
                       tcp_probe=lambda ip, port: asyncio.sleep(0, result=True),
                       chown=lambda *a: None, machine="x86_64", builder=b)
        b.bind(rt)
        return paths, rt, b, fetched
    real = B.podman_as_build_user(paths, exe)
    bpodman = Podman(exe, global_args=real.global_args, cwd=real.cwd,
                     env=dict(real.env, FAKE_PODMAN_STATE=os.environ["FAKE_PODMAN_STATE"]))
    for f in ("etc/subuid", "etc/subgid"):
        os.makedirs(os.path.dirname(paths.p(f)), exist_ok=True)
        with open(paths.p(f), "w") as fh:
            fh.write(f"someone:100000:65536\n{C.BUILD_USER}:{C.BUILD_SUBIDS[0]}:{C.BUILD_SUBIDS[1]}\n")
    system_calls.clear()

    async def system(argv, timeout=120):
        system_calls.append(argv)
        return 0
    b = B.Builder(paths, podman, fetch=fetch, build_podman=bpodman, system=system,
                  chown=lambda *a: None, user_of=lambda uid: USERS[uid])
    rt = S.Runtime(paths, podman, G.Guard(total_mb=16384, disk_path=tmp, held=lambda: 0, psi_path=os.devnull),
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
        st = fake.state(os.path.join(tmp, "podman.json"))
        BH = getattr(paths, "build_home", lambda: "/no-build-home")()
        BS = getattr(paths, "build_store", lambda: "/no-build-store")()
        bctx = os.path.join(BH, "context")
        check(bargs[-1] == bctx and bargs[bargs.index("-f") + 1] == f"{bctx}/sidecars/harness-opencode/Dockerfile"
              and "-v" not in bargs and "--volume" not in bargs,
              "the build runs the engine's own Dockerfile from a copy of the sources in the build user's home, "
              "nothing mounted", bargs)
        # Who builds: the build user, with rootless podman against its own
        # store (podman 5.4.2 cannot build in a user namespace as root, and
        # root's RUN steps would be the machine's root).
        bi = st["calls"].index(bargs)
        check(st["roots"][bi] == BS and st["homes"][bi] == BH and
              st["cwds"][bi] == BH,
              "the build runs against the build user's own store, in its home", (st["roots"][bi], st["homes"][bi]))
        mk = getattr(B, "podman_as_build_user", lambda p: Podman("podman"))
        wrap = mk(C.Paths()).argv(["podman", "build"]) if hasattr(Podman, "argv") else ["--"]
        check(wrap[:wrap.index("--") + 1] == ["systemd-run", "--scope", "--quiet",
                                              f"--slice={getattr(C, 'BUILD_SLICE', '')}", f"--uid={getattr(C, 'BUILD_UID', 0)}",
                                              f"--gid={getattr(C, 'BUILD_UID', 0)}", "--"] and
              wrap[wrap.index("--") + 1:] == ["podman", "--root", "/var/lib/cseries-build/storage",
                                              "--runroot", "/run/cseries-build/storage",
                                              "--cgroup-manager", "cgroupfs", "build"],
              "on a machine the build user's podman runs as uid BUILD_UID in the builds' slice", wrap)
        check(getattr(C, "BUILD_UID", 0) not in range(0, 60001) and getattr(C, 'BUILD_SUBIDS', ()) == (C.UID_BASE, C.UID_RANGE) and
              all(a["slot"] != 0 for a in rt.state.alloc.values()) and rt.state.allocate("app-probe")[1] != 0,
              "the build user's uid is outside the system and login ranges, its ids slot 0, which no "
              "container is given", (getattr(C, 'BUILD_UID', 0), getattr(C, 'BUILD_SUBIDS', ())))
        # Its RUN steps (apt, npm, an install script) run third-party code:
        # in a network namespace of their own, through the machine's plain
        # proxy, the one way out the firewalls leave the build user.
        bargv = " ".join(bargs)
        benv = getattr(mk(C.Paths()), "env", None) or {}
        check("--network" in bargs and bargs[bargs.index("--network") + 1] == "pasta" and
              "--network host" not in bargv and
              all(f"--build-arg {k}={C.SQUID_URL}" in bargv
                  for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")) and
              f"--build-arg NO_PROXY=localhost,127.0.0.1,::1,{C.HOST_IP}" in bargv and
              all(benv.get(k) == C.SQUID_URL for k in ("HTTP_PROXY", "HTTPS_PROXY")),
              "the build's steps and podman's own pulls go through the machine's plain proxy", bargs)
        # The builds' slice sits under the containers' one, whose CPU weight
        # is under the Host's, and is stopped after each build (it holds
        # rootless podman's pause process).
        check(getattr(C, 'BUILD_SLICE', '').endswith(".slice") and getattr(C, 'BUILD_SLICE', '').startswith(C.CGROUP_PARENT[:-len(".slice")] + "-")
              and ["systemctl", "stop", getattr(C, 'BUILD_SLICE', '')] in system_calls,
              "the build runs in a slice under the containers' slice, stopped after the build", system_calls)
        # The image goes to root's store by podman's own save and load, and
        # the build user's store is emptied before the build and after.
        seq = [(c[0] if c[:1] != ["system"] else "reset", r) for c, r in zip(st["calls"], st["roots"])
               if c[:1] in (["build"], ["save"], ["load"], ["system"])]
        check(seq == [("reset", BS), ("build", BS),
                      ("save", BS), ("load", None), ("reset", BS)],
              "the store is emptied, the image built, saved by the build user, loaded by root, emptied again",
              seq)
        # Those RUN steps run as root in the build user's namespace, with
        # every capability dropped but the ones the pinned Dockerfiles use,
        # and no new privileges.
        caps = bargs[bargs.index("--cap-add") + 1].split(",") if "--cap-add" in bargs else []
        check(getattr(B, "HARDEN_BUILDS", False) and "--cap-drop" in bargs and bargs[bargs.index("--cap-drop") + 1] == "ALL" and
              sorted(caps) == ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID"] and
              bargs.index("--cap-drop") < bargs.index("--cap-add") and
              "--security-opt no-new-privileges" in bargv and
              "--privileged" not in bargs and "--cap-add=ALL" not in bargv and "SYS_ADMIN" not in bargv,
              "the build's RUN steps keep only the capabilities its Dockerfile needs, with no new privileges",
              bargs)
        # Rootless podman maps the ids itself: no map is passed (on podman
        # 5.4.2 with crun a map passed to root's build left the machine's
        # ids in each layer and no second RUN step could start).
        check(not [a for a in bargs if a.startswith(("--userns", "--uidmap", "--gidmap"))],
              "the build passes no user namespace map of its own",
              [a for a in bargs if a.startswith(("--userns", "--uidmap", "--gidmap"))])
        if hasattr(B, "hardening_args"):
            B.HARDEN_BUILDS = False
            check(B.hardening_args() == [], "HARDEN_BUILDS off gives the old flags back")
            B.HARDEN_BUILDS = True
        rec = json.load(open(paths.built_json()))
        check(rec["opencode"]["image"] == "e" * 64 and rec["opencode"]["src_sha256"] == TARSHA and
              rec["opencode"].get("build_key") == getattr(C, "BUILD_KEY", None),
              "built.json records the image id, the sources it came from and how it was built", rec)
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

    # An engine image recorded before builds ran as the build user (no
    # build_key in built.json, as a box that built at 67ad950 or 3e5ce4c
    # has) is built again: buildah's layer cache ignores podman's flags, so
    # such an image or its layers would carry the old build's ids.
    with tempfile.TemporaryDirectory() as tmp:
        paths, rt, b, fetched = world(tmp, {url: TARBALL})
        images_json(paths, url, TARSHA)
        os.makedirs(paths.state, exist_ok=True)
        with open(paths.built_json(), "w") as f:
            json.dump({"opencode": {"image": "d" * 64, "src_sha256": TARSHA, "built_at": "x"}}, f)
        rt.state.load()
        st0 = await rt.handle("runtime.status", {})
        s = await rt.handle("workspace.set_harness", {"harness_id": "opencode", "enabled": True})
        check(s["ok"] and s["data"]["image_state"] == "building" and "opencode" not in st0["data"]["built"],
              "an engine image built another way (no build key) counts as not built", (s, st0["data"]["built"]))
        a = await rt.handle("harness.wake", {"harness": "opencode"})
        check(a["ok"] and a["data"]["image"] == "e" * 64 and len(builds(tmp)) == 1,
              "and the wake runs the image built again, not the old one", a)

    # No build user (an older guest), or no subordinate ids for it: nothing
    # is built, and the refusal says what is missing.
    with tempfile.TemporaryDirectory() as tmp:
        paths, rt, b, fetched = world(tmp, {url: TARBALL})
        images_json(paths, url, TARSHA)
        USERS.clear()
        a = await rt.handle("harness.wake", {"harness": "codex"})
        check(not a["ok"] and a["code"] == 504 and "build user" in a["error"] and not builds(tmp),
              "without the build user no build runs, and the answer names it", a)
        USERS[C.BUILD_UID] = types.SimpleNamespace(pw_name=C.BUILD_USER)
        with open(paths.p("etc/subgid"), "w") as fh:
            fh.write("someone:100000:65536\n")
        a = await rt.handle("harness.wake", {"harness": "codex"})
        check(not a["ok"] and "/etc/subgid" in a["error"] and not builds(tmp),
              "without its subordinate ids no build runs", a)

    # The image root's store holds after the load is not the one the build
    # made: nothing is recorded, root's tag is removed, the build user's
    # store is emptied and its slice stopped all the same.
    with tempfile.TemporaryDirectory() as tmp:
        paths, rt, b, fetched = world(tmp, {url: TARBALL})
        images_json(paths, url, TARSHA)
        os.environ["FAKE_LOAD_ID"] = "9" * 64
        try:
            a = await rt.handle("harness.wake", {"harness": "codex"})
        finally:
            del os.environ["FAKE_LOAD_ID"]
        st = fake.state(os.path.join(tmp, "podman.json"))
        tag = f"localhost/cseries-harness-codex:{TARSHA[:12]}"
        check(not a["ok"] and "the build made" in a["error"] and not os.path.exists(paths.built_json()) and
              tag not in st["images"] and st["calls"][-1][:2] == ["system", "reset"] and
              ["systemctl", "stop", C.BUILD_SLICE] in system_calls,
              "an image that is not the build's after the hand-over is refused and removed", (a, st["calls"][-3:]))
        check(paths.build_home() in rt.guard.disk_paths,
              "the disk floors watch the build user's store too", rt.guard.disk_paths)

    # Two engines enabled together build one after the other. On podman 5.4.2
    # every build's RUN step runs in one transient unit, cseries.slice.scope,
    # so a second build in a RUN step meanwhile fails ("Unit
    # cseries.slice.scope was already loaded"; measured in the cloud VM).
    with tempfile.TemporaryDirectory() as tmp2:
        ctx = os.path.join(tmp2, "ctx")
        for h in ("opencode", "claude-code"):
            os.makedirs(os.path.join(ctx, "sidecars", f"harness-{h}"))
            with open(os.path.join(ctx, "sidecars", f"harness-{h}", "Dockerfile"), "w") as f:
                f.write("FROM scratch\n")
        live = {"now": 0, "most": 0}

        class OneAtATime:
            binary = "podman"

            async def run(self, argv, timeout=None, check=True):
                if "--iidfile" not in argv:
                    return 0, "", ""
                live["now"] += 1
                live["most"] = max(live["most"], live["now"])
                await asyncio.sleep(0.05)
                with open(argv[argv.index("--iidfile") + 1], "w") as f:
                    f.write("sha256:" + "a" * 64)
                live["now"] -= 1
                return 0, "", ""
        paths2 = C.Paths(tmp2)
        os.makedirs(paths2.build_dir(), exist_ok=True)
        for f in ("etc/subuid", "etc/subgid"):
            os.makedirs(os.path.dirname(paths2.p(f)), exist_ok=True)
            with open(paths2.p(f), "w") as fh:
                fh.write(f"{C.BUILD_USER}:{C.BUILD_SUBIDS[0]}:{C.BUILD_SUBIDS[1]}\n")

        async def no_system(argv, timeout=120):
            return 0
        b2 = B.Builder(paths2, OneAtATime(), build_podman=OneAtATime(), system=no_system,
                       chown=lambda *a: None,
                       user_of=lambda uid: types.SimpleNamespace(pw_name=C.BUILD_USER))

        async def handed(tag, image):
            return None
        b2.hand_over = handed

        async def same_sources(src):
            return ctx
        b2.sources = same_sources
        src = {"sha256": "b" * 64, "url": "http://example.test/s.tgz"}
        got = await asyncio.gather(b2.start("opencode", src), b2.start("claude-code", src))
        check(live["most"] == 1 and got == ["a" * 64] * 2,
              "two engines enabled together build one after the other, never at once", (live, got))


asyncio.run(scenario())

print()
print("FAIL" if failed else "all runtime build checks passed")
sys.exit(failed)
