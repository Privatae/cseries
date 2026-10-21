#!/usr/bin/env python3
"""The local runtime's lifecycle (vm/runtime/lib/cseries_runtime/server.py),
against a fake podman (scripts/runtime_fake_podman.py) that records calls.

  * a cold app wake creates the wall, runs the fetch step, starts the main
    container and polls it ready; a warm wake only re-stamps;
  * a connector capability is applied only on a cold start (creds_applied),
    kept, and given to every later cold start;
  * a release answers ready_duration_s > 0 and billing none, removes the
    containers and the per-wake volumes, and keeps the data volume;
  * the idle sweep stops what was not woken for 900 s;
  * a crash loop answers 504 terminal; a failing fetch step answers
    Init:Error; a wake that runs out answers "still starting" and leaves the
    container to come up; the monitor starts an exited held container again
    after its wait;
  * startup removes everything labelled as the runtime's;
  * startup removes the images and unpacked sources no release pin or build
    record names, and nothing else;
  * uninstall keeps the data volume and drops the descriptor and capability;
  * a harness wake makes a control capability on a cold start and reuses it
    on a warm one; the browser gets its control token;
  * the machine guard's refusal is the operator's 402; concurrent cold wakes
    are one; the container cap answers 429; capacity has the Host's shape;
  * env files are 0600 and hold the credentials no argument list does;
  * app "x-wall" and app "x-fetch" never share a container, credential
    directory or env file with app "x"'s wall and fetch step.

Container-free: python3 scripts/test_runtime_lifecycle.py
"""
import asyncio
import json
import os
import re
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/runtime/lib"))
sys.path.insert(0, HERE)
import runtime_fake_podman as fake  # noqa: E402
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


TOKEN = "ab" * 32
EGRESS = "cd" * 32
D = "sha256:" + "1" * 64
PY = "ghcr.io/privatae/cseries-app-runtime-python@" + D
BROWSER_IMG = "ghcr.io/privatae/cseries-browser@sha256:" + "3" * 64


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class World:
    """A temporary machine: paths, a fake podman, a clock, a probe the test
    controls, and a Runtime over them."""

    def __init__(self, tmp, total_mb=16384, ready=True):
        self.tmp = tmp
        self.paths = C.Paths(tmp)
        self.state_file = os.path.join(tmp, "podman-state.json")
        os.environ["FAKE_PODMAN_STATE"] = self.state_file
        os.environ.pop("FAKE_FETCH_RC", None)
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        exe = fake.install(bindir)
        os.makedirs(self.paths.etc, exist_ok=True)
        with open(self.paths.images_json(), "w") as f:
            json.dump({"images": {"python": PY, "browser": BROWSER_IMG},
                       "harness_src": {"url": "https://example.test/h.tar.gz", "sha256": "9" * 64}}, f)
        with open(self.paths.browser_token(), "w") as f:
            f.write("b" * 43 + "\n")
        st = fake.state(self.state_file)
        st["images"] = {PY: "a" * 64, BROWSER_IMG: "b" * 64}
        fake.write_state(self.state_file, st)
        self.clock = Clock()
        self.ready = ready
        self.probes = []
        self.chowns = []
        psi = os.path.join(tmp, "psi")
        with open(psi, "w") as f:
            f.write("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.guard = G.Guard(total_mb=total_mb, psi_path=psi, disk_path=tmp)
        self.rt = S.Runtime(self.paths, Podman(exe), self.guard, http_probe=self.http_probe,
                            tcp_probe=self.tcp_probe, clock=self.clock,
                            chown=lambda p, u, g: self.chowns.append((p, u)),
                            machine="x86_64", sleep=self.sleep)

    async def http_probe(self, url):
        self.probes.append(url)
        r = self.ready(url) if callable(self.ready) else self.ready
        return r

    async def tcp_probe(self, ip, port):
        return True

    async def sleep(self, s):
        self.clock.t += s
        await asyncio.sleep(0)

    def podman(self):
        return fake.state(self.state_file)

    def calls(self, sub=None):
        return [c for c in self.podman()["calls"] if sub is None or c[:len(sub)] == sub]

    async def op(self, op, params):
        return await self.rt.handle(op, params)


def proc_desc(**kw):
    d = {"budgetMi": 128, "network": ["api.telegram.example"], "version": "1.0.0", "form": "process",
         "image": "python", "command": ["python3", "connector.py"], "readyPath": "/health",
         "portEnv": "PORT", "readyTimeoutS": 30, "cpuLimitM": 1000, "wakeTimeoutS": 60,
         "sourceToken": TOKEN, "lifecycle": "resident"}
    d.update(kw)
    return d


async def install(w, app="telegram", **kw):
    return await w.op("workspace.set_app_runtime", {"app_id": app, "enabled": True,
                                                    "descriptor": proc_desc(**kw)})


def run(coro):
    return asyncio.run(coro)


# ── app: cold, warm, creds, release ─────────────────────────────────────────

async def scenario_app():
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        # The runtime is socket-activated, so these are in its environment;
        # podman run with them set failed on the box with "disk I/O error:
        # bad file descriptor" and made no container.
        os.environ.update(LISTEN_FDS="1", LISTEN_PID="1", LISTEN_FDNAMES="runtime.sock",
                          NOTIFY_SOCKET="/run/systemd/notify")
        a = await install(w)
        check(a["ok"] and a["data"]["app_runtimes"] == ["telegram"], "an app runtime descriptor is kept", a)
        check(stat.S_IMODE(os.stat(w.paths.descriptor("telegram")).st_mode) == 0o600,
              "the kept descriptor (it holds the source token) is 0600")
        a = await w.op("harness.wake", {"app_rt": "telegram",
                                        "creds": {"connector-cap": "k" * 43, "egress-subject-token": EGRESS}})
        check(a["ok"] and a["data"] == {"app_rt": "telegram", "ready": True,
                                        "url": "http://10.89.78.16:8850", "creds_applied": True},
              "a cold wake answers the app's address and creds_applied", a)
        runs = w.calls(["run"])
        names = [r[r.index("--name") + 1] for r in runs]
        check(names == ["cseries-app-telegram.wall", "cseries-app-telegram.fetch", "cseries-app-telegram"],
              "the wall, then the fetch step, then the main container", names)
        check(w.probes and w.probes[-1] == "http://10.89.78.16:8850/health", "readiness is the app's own path")
        envp = w.paths.env_file("app-telegram")
        env = open(envp).read()
        check(stat.S_IMODE(os.stat(envp).st_mode) == 0o600 and f"APP_RUNTIME_SOURCE_TOKEN={TOKEN}" in env and
              "CSERIES_CONNECTOR_CAP=" + "k" * 43 in env,
              "the main env file is 0600 and carries the source token and the capability")
        check(all(TOKEN not in " ".join(r) and "k" * 43 not in " ".join(r) and EGRESS not in " ".join(r)
                  for r in runs), "no credential is in any podman argument list")
        leaked = [e for e in w.podman().get("env", []) if any(e.values())]
        check(not leaked, "podman is never given the runtime's socket-activation or notify variables",
              leaked[:2])
        for k in ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES", "NOTIFY_SOCKET"):
            os.environ.pop(k, None)
        auth = os.path.join(w.paths.creds_dir("app-telegram.wall"), "auth")
        check(open(auth).read() == f"app:telegram:{EGRESS}" and stat.S_IMODE(os.stat(auth).st_mode) == 0o440,
              "the wall's credential file holds app:<id>:<token>")
        vols = w.podman()["volumes"]
        check(set(vols) == {"cseries-app-telegram-src", "cseries-app-telegram-data"} and
              vols["cseries-app-telegram-src"]["labels"][C.LABEL_SCOPE] == "wake" and
              vols["cseries-app-telegram-data"]["labels"][C.LABEL_SCOPE] == "keep",
              "a per-wake source volume and a kept data volume", vols)
        slot = w.rt.state.alloc["app-telegram"]["slot"]
        want = C.UID_BASE + slot * C.UID_RANGE + 1000
        check((vols["cseries-app-telegram-data"]["mount"], want) in w.chowns,
              "the data volume belongs to the app's uid as mapped into its own range", w.chowns[-3:])

        n_runs = len(w.calls(["run"]))
        w.clock.t += 100
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        check(a["ok"] and len(w.calls(["run"])) == n_runs and "creds_applied" not in a["data"],
              "a warm wake only re-stamps: nothing is created", a)
        a = await w.op("harness.wake", {"app_rt": "telegram", "creds": {"connector-cap": "n" * 43}})
        check(a["ok"] and a["data"]["creds_applied"] is False,
              "a new capability on a warm wake is not applied (the Host then releases and wakes cold)", a)

        w.clock.t += 50
        # Released with podman ORing its label filters (it ANDs them): still
        # only this unit's per-wake volume goes.
        os.environ["FAKE_VOLUME_FILTER_OR"] = "1"
        r = await w.op("harness.release", {"app_rt": "telegram"})
        os.environ.pop("FAKE_VOLUME_FILTER_OR")
        check(r["ok"] and r["data"]["ready_duration_s"] == 150.0 and r["data"]["billing"] == "none" and
              r["data"]["scaled_to"] == 0, "a release answers the ready duration and billing none", r)
        check(not w.podman()["containers"], "the release removed every container")
        check(set(w.podman()["volumes"]) == {"cseries-app-telegram-data"},
              "the per-wake volume is gone and the data volume kept", w.podman()["volumes"])
        check(not os.path.exists(w.paths.env_file("app-telegram")) and not os.path.exists(auth),
              "its env and credential files are gone")

        a = await w.op("harness.wake", {"app_rt": "telegram", "creds": {"egress-subject-token": EGRESS}})
        env = open(w.paths.env_file("app-telegram")).read()
        check(a["ok"] and "CSERIES_CONNECTOR_CAP=" + "k" * 43 in env and "creds_applied" not in a["data"],
              "a later cold start gets the capability the first one applied, without the Host resending it")
        check(w.rt.state.alloc["app-telegram"]["ip"] == 16, "the app keeps its address")

        # The idle sweep.
        w.clock.t += C.IDLE_GRACE_S - 10
        await w.rt.sweep_once()
        check(w.rt.units["app-telegram"].state == "running", "the sweep leaves a unit woken within 900 s")
        w.clock.t += 20
        await w.rt.sweep_once()
        check(w.rt.units["app-telegram"].state == "stopped" and not w.podman()["containers"],
              "the sweep stops a unit not woken for 900 s")

        # Uninstall.
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        r = await w.op("workspace.set_app_runtime", {"app_id": "telegram", "enabled": False})
        check(r["ok"] and r["data"]["removed"] is True and r["data"]["app_runtimes"] == [] and
              not w.podman()["containers"] and "cseries-app-telegram-data" in w.podman()["volumes"] and
              not os.path.exists(w.paths.descriptor("telegram")) and
              w.rt.state.connector_cap("telegram") is None,
              "uninstall stops it and drops its descriptor and capability, and keeps its data volume", r)
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        check(not a["ok"] and a["code"] == 404 and "no appRuntimes descriptor for telegram" in a["error"],
              "a wake with no descriptor answers the operator's text, which makes the Host re-sync", a)
        # A removed app gives up its address, so the machine does not run out
        # of them as apps come and go; it keeps its slot (its data volume's
        # owner), here and after a restart.
        tg_slot = w.rt.state.alloc["app-telegram"]["slot"]
        await install(w, app="weather")
        a = await w.op("harness.wake", {"app_rt": "weather"})
        check(a["ok"] and a["data"]["url"] == "http://10.89.78.16:8850" and
              w.rt.state.alloc["app-weather"]["slot"] != tg_slot,
              "a new app may take a removed app's address, never its slot", (a, w.rt.state.alloc))
        await install(w)
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        w.rt.state.load()
        check(a["ok"] and a["data"]["url"] == "http://10.89.78.17:8850" and
              w.rt.state.alloc["app-telegram"] == {"ip": 17, "slot": tg_slot},
              "a reinstalled app gets an address again and its own slot back", (a, w.rt.state.alloc))
        many = [f"a{i}" for i in range(C.IP_LAST - C.IP_FIRST + 1)]
        for app in many:
            w.rt.state.allocate(C.unit_name("app", app))
            w.rt.state.drop_descriptor(app)
        ip, _slot = w.rt.state.allocate("app-one-more")
        check(C.IP_FIRST <= ip <= C.IP_LAST,
              "after more apps came and went than there are addresses, one more still gets one", ip)


# ── failures ────────────────────────────────────────────────────────────────

async def scenario_failures():
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        await install(w)
        os.environ["FAKE_FETCH_RC"] = "1"
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        check(not a["ok"] and a["code"] == 504 and a["error"] == "terminal: Init:Error (exit 1)" and
              not w.podman()["containers"], "a failing fetch step answers terminal and leaves nothing", a)
        os.environ.pop("FAKE_FETCH_RC")

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp, ready=False)
        await install(w)

        def crash():
            st = w.podman()
            c = st["containers"].get("cseries-app-telegram")
            if c:
                c["running"], c["exit"] = False, 3
                fake.write_state(w.state_file, st)
        orig = w.http_probe

        async def probe_and_crash(url):
            crash()
            return await orig(url)
        w.rt.http_probe = probe_and_crash
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        check(not a["ok"] and a["code"] == 504 and a["error"] == "terminal: CrashLoopBackOff (exit 3)",
              "an app that keeps exiting while it starts answers terminal CrashLoopBackOff", a)
        check(len(w.calls(["start"])) == 2, "it was started again twice before that",
              len(w.calls(["start"])))

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp, ready=False)
        await install(w, wakeTimeoutS=20)
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        check(not a["ok"] and a["code"] == 504 and a["error"] == "still starting" and
              "cseries-app-telegram" in w.podman()["containers"],
              "a wake that runs out answers still starting and leaves the app to come up", a)
        w.ready = True
        await w.rt.monitor_once()
        check(w.rt.units["app-telegram"].state == "running", "the monitor sees it become ready")
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        check(a["ok"] and len(w.calls(["run"])) == 3, "the next wake is warm", a)
        # A held container that exits is started again after its wait.
        st = w.podman()
        st["containers"]["cseries-app-telegram"].update(running=False, exit=1)
        fake.write_state(w.state_file, st)
        await w.rt.monitor_once()
        u = w.rt.units["app-telegram"]
        check(u.next_restart == w.clock.t + 10 and not w.calls(["start"]),
              "an exited held app is started again 10 s later, not at once", u.next_restart)
        w.clock.t += 11
        await w.rt.monitor_once()
        check(w.calls(["start"]) and w.podman()["containers"]["cseries-app-telegram"]["running"] and
              u.state == "running", "and is started again after the wait", w.calls(["start"]))

    # A release that lands while a cold start waits (here on its image) is
    # not lost: the start stops before any container runs.
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        await install(w)
        gate = asyncio.Event()
        real_ensure = w.rt.ensure_image

        async def slow_image(ref, deadline):
            await gate.wait()
            return await real_ensure(ref, deadline)
        w.rt.ensure_image = slow_image
        wake = asyncio.ensure_future(w.op("harness.wake", {"app_rt": "telegram"}))
        for _ in range(5):
            await asyncio.sleep(0)
        rel = await w.op("harness.release", {"app_rt": "telegram"})
        gate.set()
        a = await wake
        check(rel["ok"] and not a["ok"] and a["code"] == 504 and "released while starting" in a["error"],
              "a release during a cold start ends the start", (rel, a))
        check(not w.podman()["containers"] and not w.calls(["run"]) and
              w.rt.units["app-telegram"].state == "stopped" and
              not os.path.exists(w.paths.env_file("app-telegram")) and
              "cseries-app-telegram-src" not in w.podman()["volumes"],
              "and nothing of it is left running or on disk",
              (w.podman()["containers"], w.calls(["run"]), sorted(w.podman()["volumes"])))

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        await install(w, image="creative")
        a = await w.op("harness.wake", {"app_rt": "telegram"})
        check(not a["ok"] and a["code"] == 504 and "no creative image is pinned" in a["error"],
              "a family the image table does not pin is refused", a)


# ── harnesses ───────────────────────────────────────────────────────────────

async def scenario_harness():
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        a = await w.op("harness.wake", {"harness": "browser", "creds": {"egress-subject-token": EGRESS}})
        check(a["ok"] and a["data"]["url"] == "http://10.89.78.16:8830" and a["data"]["cap"] is None and
              a["data"]["image"] == BROWSER_IMG and a["data"]["memory_limit_bytes"] == 1024 << 20,
              "the browser wakes on its address with its image", a)
        env = open(w.paths.env_file("h-browser")).read()
        check("BROWSER_CONTROL_TOKEN=" + "b" * 43 in env and f"NAC_EGRESS_SUBJECT_TOKEN={EGRESS}" in env,
              "the browser's env carries the machine's control token and its egress token")
        a = await w.op("harness.wake", {"harness": "codex"})
        check(not a["ok"] and a["code"] == 504 and "no build of codex" in a["error"],
              "an engine this machine has not built is refused (no builder in this test)", a)
        w.rt.state.record_build("codex", "e" * 64, "9" * 64, "2026-10-19T00:00:00Z")
        st = w.podman()
        st["images"]["e" * 64] = "e" * 64
        fake.write_state(w.state_file, st)
        a = await w.op("harness.wake", {"harness": "codex", "creds": {"credentials.json": "{}"},
                                        "vcpus": 2, "slots": 2})
        cap = a["data"]["cap"] if a["ok"] else None
        check(a["ok"] and a["data"]["image"] == "e" * 64 and isinstance(cap, str) and len(cap) >= 40 and
              a["data"]["slots"] == 2, "a built engine wakes with a fresh control capability", a)
        # The plain proxy admits only the engines' and the builds' addresses
        # (common.PROXY_CLIENTS): an engine is placed there, the browser and
        # every app outside.
        check(a["ok"] and a["data"]["url"] == f"http://{C.IP_PREFIX}{C.ENGINE_IP_FIRST}:8840",
              "a coding engine's address is one the plain proxy admits", a.get("data"))
        w.rt.state.alloc["h-kimi"] = {"ip": 30, "slot": 77}
        ip, slot = w.rt.state.allocate("h-kimi", *C.ip_range("harness", "kimi"))
        kept = json.load(open(w.paths.alloc_json()))["names"]["h-kimi"]
        check(C.ENGINE_IP_FIRST < ip <= C.ENGINE_IP_LAST and slot == 77 and kept == {"ip": ip, "slot": 77},
              "an engine kept at an app's address moves into the engines' range and keeps its slot",
              (ip, slot, kept))
        ip2, _ = w.rt.state.allocate("app-x", *C.ip_range("app", "x"))
        check(C.IP_FIRST <= ip2 <= C.IP_LAST, "an app's address is outside it", ip2)
        check(open(os.path.join(w.paths.creds_dir("h-codex"), "credentials.json")).read() == "{}",
              "its cred file is written into its creds directory")
        a2 = await w.op("harness.wake", {"harness": "codex"})
        check(a2["ok"] and a2["data"]["cap"] == cap, "a warm wake answers the same capability")
        await w.op("harness.release", {"harness": "codex"})
        check(not os.path.exists(w.paths.creds_dir("h-codex")), "a release removes its creds directory")
        a3 = await w.op("harness.wake", {"harness": "codex"})
        check(a3["ok"] and a3["data"]["cap"] != cap, "the next cold wake makes a new capability")
        c = await w.op("harness.capacity", {"harnesses": ["codex", "kimi"], "slots": {"kimi": 3}})
        d = c["data"]
        check(c["ok"] and d["gated"] is True and d["fits"] == ["codex", "kimi"] and d["refused"] == [] and
              d["slots"] == {"codex": 1, "kimi": 3} and set(d) >= {"headroom_mi", "remaining_mi",
                                                                   "upgrade_to", "pool_add_mi"},
              "capacity answers in the Host's shape; a running engine fits as it is", d)
        s = await w.op("workspace.set_harness", {"harness_id": "kimi", "enabled": True})
        check(s["ok"] and s["data"]["image_state"] == "no-sources" and "kimi" in s["data"]["harnesses"],
              "enabling an engine with no builder says so", s)
        s = await w.op("workspace.set_harness", {"harness": "codex", "enabled": False})
        check(s["ok"] and w.rt.units["h-codex"].state == "stopped", "turning an engine off stops it", s)


# ── names: no app's id makes it another's container ────────────────────────

async def scenario_names():
    """App ids may hold a "-", so app "chat-wall" and app "chat-fetch" are
    apps in their own right, beside app "chat" whose wall and fetch step
    they would name with a "-" suffix. Nothing one of them owns (a
    container, a credential directory, an env file) may be another's."""
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        apps = ("chat", "chat-wall", "chat-fetch")
        for app in apps:
            await install(w, app=app)
        woke = []
        for app in apps:
            w.clock.t += 61
            woke.append(await w.op("harness.wake", {"app_rt": app,
                                                    "creds": {"egress-subject-token": EGRESS}}))
        check(all(a["ok"] for a in woke), "app chat, app chat-wall and app chat-fetch all wake",
              [a.get("error") for a in woke])
        names = [r[r.index("--name") + 1] for r in w.calls(["run"])]
        check(len(names) == 9 and len(set(names)) == 9, "their nine containers have nine names", names)
        units = [C.unit_name("app", a) for a in apps]
        owned = [{C.wall_of(u), C.fetch_of(u), u} for u in units]
        check(all(not (owned[i] & owned[j]) for i in range(3) for j in range(3) if i != j),
              "no app's env-file or credential-directory names are another's", owned)
        auth = os.path.join(w.paths.creds_dir(C.wall_of("app-chat")), "auth")
        r = await w.op("harness.release", {"app_rt": "chat-wall"})
        left = set(w.podman()["containers"])
        check(r["ok"] and {"cseries-app-chat", "cseries-app-chat.wall"} <= left and
              not any(n.startswith("cseries-app-chat-wall") for n in left),
              "releasing app chat-wall removes its own containers and none of app chat's", sorted(left))
        check(os.path.exists(auth) and open(auth).read() == f"app:chat:{EGRESS}" and
              os.path.exists(w.paths.env_file(C.wall_of("app-chat"))),
              "app chat's wall keeps its credential and env file")


# ── the guard, single flight, limits, startup ───────────────────────────────

async def scenario_guard():
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp, total_mb=3819)
        a = await w.op("harness.wake", {"harness": "browser"})
        check(a["ok"], "the browser fits a 3.7 GiB machine")
        w.rt.state.record_build("opencode", "e" * 64, "9" * 64, "2026-10-19T00:00:00Z")
        st = w.podman()
        st["images"]["e" * 64] = "e" * 64
        fake.write_state(w.state_file, st)
        a = await w.op("harness.wake", {"harness": "opencode", "slots": 3})
        m = re.search(r"/harness/wake 402: (\{.*\})\s*$", a.get("error", ""), re.S)
        d = json.loads(m.group(1))["detail"] if m else {}
        check(not a["ok"] and a["code"] == 402 and d.get("error") == "capability_over_plan" and
              d.get("needs_mi") == 2176 and d.get("headroom_mi") == 3819 - 1055 - 1024,
              "an engine that does not fit is refused 402 in the operator's text", a)
        r = await w.op("workspace.set_app_runtime", {"app_id": "big", "enabled": True,
                                                     "descriptor": proc_desc(budgetMi=4096)})
        check(not r["ok"] and r["code"] == 402 and "/set-app-runtime 402: " in r["error"],
              "an app that could never fit the machine is refused at install, 402", r)

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        await install(w)
        a, b = await asyncio.gather(w.op("harness.wake", {"app_rt": "telegram"}),
                                    w.op("harness.wake", {"app_rt": "telegram"}))
        check(a["ok"] and b["ok"] and len(w.calls(["run"])) == 3,
              "two cold wakes at once start the app once", len(w.calls(["run"])))

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        for i in range(17):
            await install(w, app=f"app{i}")
        oks = []
        for i in range(17):
            w.clock.t += 61   # past the per-minute window, so only the count limits
            oks.append(await w.op("harness.wake", {"app_rt": f"app{i}"}))
        refused = [x for x in oks if not x["ok"]]
        check(refused and refused[0]["code"] == 429 and "at most 32" in refused[0]["error"] and
              sum(1 for x in oks if x["ok"]) == 16 and not oks[16]["ok"],
              "16 apps (32 containers) start; the 17th is refused 429",
              [(x["ok"], x.get("error")) for x in oks])

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        st = w.podman()
        # A process app left behind: its wall listed first, as podman ps may.
        st["containers"] = {"cseries-app-old.wall": {"running": True, "exit": None,
                                                     "labels": {C.LABEL: "app"}, "argv": []},
                            "cseries-app-old": {"running": True, "exit": None,
                                                "labels": {C.LABEL: "app"},
                                                "argv": ["run", "--network", "container:cseries-app-old.wall"]},
                            "cseries-host": {"running": True, "exit": None, "labels": {}, "argv": []}}
        # Each volume its own directory, as podman's are: the fake's `volume
        # rm` deletes it with everything in it, as podman's does.
        mounts = {n: os.path.join(tmp, "vols", n) for n in
                  ("cseries-h-codex-ws", "cseries-app-x-data", "cseries-restore.x")}
        for n, m in mounts.items():
            os.makedirs(m)
            with open(os.path.join(m, "f"), "w") as f:
                f.write("invented\n")
        st["volumes"] = {"cseries-h-codex-ws": {"labels": {C.LABEL_SCOPE: "wake"},
                                                "mount": mounts["cseries-h-codex-ws"]},
                         "cseries-app-x-data": {"labels": {C.LABEL_SCOPE: "keep"},
                                                "mount": mounts["cseries-app-x-data"]},
                         "cseries-restore.x": {"labels": {C.LABEL_SCOPE: "restore"},
                                               "mount": mounts["cseries-restore.x"]}}
        fake.write_state(w.state_file, st)
        os.makedirs(w.paths.creds_dir("h-old"))
        await w.rt.startup()
        st = w.podman()
        check(set(st["containers"]) == {"cseries-host"} and set(st["volumes"]) == {"cseries-app-x-data"} and
              not os.path.exists(w.paths.creds_dir("h-old")),
              "startup removes the runtime's labelled containers, per-wake and restore volumes and creds, "
              "nothing else", (st["containers"].keys(), st["volumes"].keys()))
        check(os.path.exists(os.path.join(mounts["cseries-app-x-data"], "f"))
              and not os.path.exists(mounts["cseries-h-codex-ws"])
              and not os.path.exists(mounts["cseries-restore.x"]),
              "  an app's kept data is on the disk after it, a per-wake volume's files are not",
              sorted(os.listdir(os.path.join(tmp, "vols"))))
        s = await w.op("runtime.status", {})
        check(s["ok"] and s["data"]["budget_mi"] == 16384 - G.reserve_mb(16384) and
              s["data"]["images"] == ["browser", "python"], "runtime.status reports the budget and images", s)

    # Startup also removes the images a release left behind: a family's image
    # under another digest than images.json pins, a family no longer pinned,
    # an engine built from other sources or never recorded, and the unpacked
    # sources of other releases. The Host's and Postgres's images, the build
    # bases and everything pinned or recorded stay.
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        cur, old = "9" * 64, "0" * 64
        old_py = "ghcr.io/privatae/cseries-app-runtime-python@sha256:" + "5" * 64
        old_creative = "ghcr.io/privatae/cseries-app-runtime-creative@sha256:" + "6" * 64
        host = "ghcr.io/privatae/cseries@sha256:" + "7" * 64
        st = w.podman()
        st["images"].update({
            old_py: "5" * 64, old_creative: "6" * 64,
            host: "7" * 64, "localhost/cseries:dev": "7" * 64,
            "ghcr.io/privatae/cseries-postgres@sha256:" + "4" * 64: "4" * 64,
            "docker.io/library/node:22-slim": "8" * 64,
            f"localhost/cseries-harness-opencode:{cur[:12]}": "c" * 64,
            f"localhost/cseries-harness-codex:{old[:12]}": "d" * 64,
            f"localhost/cseries-harness-kimi:{cur[:12]}": "f0" * 32,
        })
        st["image_labels"] = {"c" * 64: {C.LABEL + ".build": "opencode"},
                              "d" * 64: {C.LABEL + ".build": "codex"},
                              "f0" * 32: {C.LABEL + ".build": "kimi"}}
        fake.write_state(w.state_file, st)
        os.makedirs(w.paths.state, exist_ok=True)
        with open(w.paths.built_json(), "w") as f:
            json.dump({"opencode": {"image": "c" * 64, "src_sha256": cur, "built_at": "x"},
                       "codex": {"image": "d" * 64, "src_sha256": old, "built_at": "x"}}, f)
        w.rt.state.load()
        bd = w.paths.build_dir()
        for d in (cur, old):
            os.makedirs(os.path.join(bd, d))
        open(os.path.join(bd, old + ".tar.gz"), "w").close()
        open(os.path.join(bd, "opencode.iid"), "w").close()
        await w.rt.startup()
        left = set(w.podman()["images"])
        check(left == {PY, BROWSER_IMG, host, "localhost/cseries:dev",
                       "ghcr.io/privatae/cseries-postgres@sha256:" + "4" * 64,
                       "docker.io/library/node:22-slim", f"localhost/cseries-harness-opencode:{cur[:12]}"},
              "startup removes the images no pin or build record names, and only those",
              sorted(left))
        check(sorted(os.listdir(bd)) == sorted([cur, "opencode.iid"]),
              "startup removes the unpacked sources of other releases", sorted(os.listdir(bd)))
        rms = [c for c in w.calls() if c[:2] == ["image", "rm"]]
        check(rms and all("--force" not in c and "-f" not in c for c in rms),
              "an image is removed without --force, so one a container still uses stays", rms)


# ── the disk: the floor stops writers, the owner deletes data ──────────────

class FakeDisk:
    """os.statvfs for the guard's disk path: `free` bytes available."""

    def __init__(self, free):
        self.free = free
        self.real = os.statvfs

    def __call__(self, path):
        class St:
            f_frsize = 4096
            f_bavail = self.free // 4096
        return St()


def fill(w, app, nbytes):
    mnt = w.podman()["volumes"][C.data_volume(app)]["mount"]
    with open(os.path.join(mnt, f"blob-{len(os.listdir(mnt))}"), "wb") as f:
        f.write(os.urandom(nbytes))


async def scenario_disk():
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        disk = FakeDisk(50 << 30)
        os.statvfs = disk
        try:
            await install(w, app="telegram")
            await install(w, app="notes", lifecycle="window")
            for a in ("telegram", "notes"):
                w.clock.t += 61
                r = await w.op("harness.wake", {"app_rt": a, "creds": {"egress-subject-token": EGRESS}})
                check(r["ok"], f"{a} wakes", r)
            w.clock.t += 61
            check((await w.op("harness.wake", {"harness": "browser"}))["ok"], "the browser wakes")
            fill(w, "telegram", 3 << 20)
            fill(w, "notes", 1 << 20)

            # The disk gets short (under twice the wake floor): the watchdog
            # starts measuring the apps' data, and stops nothing yet.
            disk.free = 3 << 30
            await w.rt.watchdog_once()
            w.clock.t += G.DATA_SAMPLE_EVERY_S + 1
            fill(w, "notes", 2 << 20)                # notes grows; telegram does not
            await w.rt.watchdog_once()
            live = {u.ident for u in w.rt.live()}
            check(live == {"telegram", "notes", "browser"}, "above the stop floor nothing is stopped", live)

            # The wake floor: refused, naming the floor and each app's data.
            disk.free = 1500 << 20
            r = await w.op("harness.wake", {"harness": "codex"})
            check(not r["ok"] and r["code"] == 507 and "under the 2 GiB" in r["error"] and
                  "telegram 3 MiB" in r["error"] and "notes 3 MiB" in r["error"],
                  "under the wake floor any wake is refused with the floor and each app's data named", r)

            # Under the stop floor (1100 MiB: inside the database's reserve,
            # above the old 1 GiB floor): the browser, then the app whose data
            # grew last, then the other; one a round, and no data is deleted.
            disk.free = 1100 << 20
            stopped = []
            for _ in range(4):
                w.clock.t += 2
                stopped.append(await w.rt.watchdog_once())
            check(stopped == ["h-browser", "app-notes", "app-telegram", None],
                  "the disk floor stops the browser, then the app whose data grew last, then the rest",
                  stopped)
            vols = set(w.podman()["volumes"])
            check({C.data_volume("telegram"), C.data_volume("notes")} <= vols and not w.podman()["containers"],
                  "every container is stopped and every app's data is kept", (vols, w.podman()["containers"]))
            check(w.rt.units["app-notes"].stopped_why.startswith("the machine guard: this machine had 1100 MiB"),
                  "the stop says why", w.rt.units["app-notes"].stopped_why)

            # A pull in flight when the disk is short is stopped, and its wake
            # is answered (not left hanging, not a crash).
            disk.free = 50 << 30
            st = w.podman()
            st["images"].pop(PY, None)
            fake.write_state(w.state_file, st)
            started = asyncio.Event()

            async def slow_pull(ref, timeout):
                started.set()
                await asyncio.sleep(3600)
            w.rt.podman.pull = slow_pull
            wake = asyncio.ensure_future(w.op("harness.wake", {"app_rt": "telegram"}))
            await asyncio.wait_for(started.wait(), 5)
            disk.free = 900 << 20
            await w.rt.watchdog_once()
            r = await asyncio.wait_for(wake, 5)
            check(not r["ok"] and r["code"] == 507 and "pull was stopped" in r["error"],
                  "under the stop floor a pull in flight is cancelled and its wake answered", r)

            # What each app's data takes, and deleting a removed app's.
            disk.free = 50 << 30
            r = await w.op("workspace.app_data", {})
            sizes = {a["app_id"]: a for a in (r.get("data") or {}).get("apps", [])}
            check(r["ok"] and set(sizes) == {"telegram", "notes"} and
                  sizes["notes"]["bytes"] >= 3 << 20 and sizes["notes"]["installed"] is True and
                  r["data"]["stop_floor_bytes"] == G.DISK_STOP_FLOOR,
                  "workspace.app_data lists each app's kept data and its size", r)
            r = await w.op("workspace.delete_app_data", {"app_id": "notes"})
            check(not r["ok"] and r["code"] == 409 and C.data_volume("notes") in w.podman()["volumes"],
                  "an installed app's data cannot be deleted this way", r)
            await w.op("workspace.set_app_runtime", {"app_id": "notes", "enabled": False})
            r = await w.op("workspace.app_data", {})
            sizes = {a["app_id"]: a for a in r["data"]["apps"]}
            check(sizes["notes"]["installed"] is False, "an uninstalled app's data is listed as kept", sizes)
            # Even were podman to OR its two label filters, the delete takes
            # only the app's own volume, by name.
            os.environ["FAKE_VOLUME_FILTER_OR"] = "1"
            r = await w.op("workspace.delete_app_data", {"app_id": "notes"})
            os.environ.pop("FAKE_VOLUME_FILTER_OR")
            check(r["ok"] and r["data"]["deleted"] is True and r["data"]["freed_bytes"] >= 3 << 20 and
                  C.data_volume("notes") not in w.podman()["volumes"] and
                  C.data_volume("telegram") in w.podman()["volumes"] and
                  "app-notes" not in w.rt.state.alloc,
                  "a removed app's data is deleted when asked, its id range freed, and nothing else",
                  (r, sorted(w.podman()["volumes"])))
            r = await w.op("workspace.delete_app_data", {"app_id": "notes"})
            check(r["ok"] and r["data"]["deleted"] is False, "deleting it again finds nothing", r)
            for bad in ("../telegram", "telegram\n", "", None, "Telegram"):
                r = await w.op("workspace.delete_app_data", {"app_id": bad})
                check(not r["ok"] and r["code"] == 400, f"app_id {bad!r} is refused", r)
            check(C.data_volume("telegram") in w.podman()["volumes"], "telegram's data is untouched")
        finally:
            os.statvfs = disk.real


run(scenario_app())
run(scenario_failures())
run(scenario_harness())
run(scenario_names())
run(scenario_guard())
run(scenario_disk())

print()
print("FAIL" if failed else "all runtime lifecycle checks passed")
sys.exit(failed)
