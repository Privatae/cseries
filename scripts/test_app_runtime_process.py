#!/usr/bin/env python3
"""Gate: a user's process app runs in its OWN runtime pod.

Origin: project 1. The host built a trading bot as a
`runtime.kind: process` app whose first boot ran apt-get, built TA-Lib and
made a python>=3.11 venv. That process would have been spawned INSIDE the
entity container (uid 1000, no sudo, no gcc/make, python 3.14 only) and the
only runtime-pod image was a scratch Go helper — the app could run nowhere on
the platform, while the workflow told the user to "open the live app".

What this pins (no DB, no pod, no container):
  1. manifest: `runtime.image` parses; a custom process app WITHOUT it is
     refused; an image app must declare baseline_memory_mi + network[];
     `image` on a non-process kind is refused; an image-baked (standard)
     process app still loads without one. `runs_in_pod` is the predicate.
  2. installer: the descriptor for an image app carries form=process, image,
     command, readyPath, portEnv, kernelUrl and a derived sourceToken; a
     standard process app writes NO descriptor.
  3. supervisor: for a pod-backed app open_window / start_app / stop_app /
     get / list_running route to the app-runtime lane (a stub bridge) and
     never spawn a child; the RunningSidecar-shaped view says pid=None and
     is_alive follows the lane; a resident start holds the lease; stop
     releases and bills nothing extra.
  4. harness_backend: app_runtime_state is running=False until a wake and
     True after; app_source_token is stable + HMAC-shaped; the source route
     exists and the middleware lets its shape through to in-handler auth.
  5. proxy: the process branch reads runtime.image and takes the pod lane.

Run: APP_ROOT=<repo> python3 scripts/test_app_runtime_process.py
"""
import asyncio
import os
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[app-rt-process] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[app-rt-process] FAIL {label}" + (f" — {detail}" if detail else ""))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _write(tmp: Path, app_id: str, provenance: str, *, image: str | None,
           extra: str = "", kind: str = "process", lifecycle: str = "window") -> Path:
    d = tmp / app_id
    d.mkdir()
    (d / "index.html").write_text("<html></html>")
    (d / "run.sh").write_text("#!/bin/sh\nexec sleep 1\n")
    img = f"  image: {image}\n" if image else ""
    rt = (f"runtime:\n  kind: {kind}\n  lifecycle: {lifecycle}\n{img}"
          f"  command: [\"bash\", \"run.sh\"]\n  ready_path: /api/v1/ping\n"
          if kind == "process" else f"runtime:\n  kind: {kind}\n{img}")
    head = textwrap.dedent(f"""\
        id: {app_id}
        name: T
        entrypoint: index.html
        provenance: {provenance}
        version: 0.3.0
        permissions: []
        """)
    (d / "app.yaml").write_text(head + extra + rt)
    return d


def main() -> int:
    from server.applications import manifest as M
    from server.applications import harness_backend as hb
    from server.applications import supervisor as S
    from server.applications import registry as R

    tmp = Path(tempfile.mkdtemp(prefix="app-rt-proc-"))
    res_net = "resources:\n  baseline_memory_mi: 1024\nnetwork: [github.com, pypi.org]\n"

    # ── 1. manifest ──────────────────────────────────────────────────────
    d_pod = _write(tmp, "trading-bot", "custom", image="python", extra=res_net)
    m_pod = M.load(d_pod / "app.yaml")
    check("runtime.image parses", m_pod.runtime.image == "python")
    check("runs_in_pod: custom image app", M.runs_in_pod(m_pod) is True)
    check("ALLOWED_RUNTIME_IMAGES carries python", "python" in M.ALLOWED_RUNTIME_IMAGES)
    m_cr = M.load(_write(tmp, "wallpaper-render", "custom", image="creative",
                         extra=res_net) / "app.yaml")
    check("runtime.image: creative parses and runs in its own pod",
          m_cr.runtime.image == "creative" and M.runs_in_pod(m_cr) is True)
    m_v = M.load(_write(tmp, "render-4", "custom", image="creative",
                        extra=res_net.replace("baseline_memory_mi: 1024\n",
                                              "baseline_memory_mi: 1024\n  vcpus: 4\n")) / "app.yaml")
    check("resources.vcpus: a process app declares its pod's CPU ceiling", m_v.resources.vcpus == 4)
    for bad, why in (("9", "1 to 8"), ("0", "1 to 8"), ("true", "1 to 8")):
        try:
            M.load(_write(tmp, f"render-bad-{bad}", "custom", image="creative",
                          extra=res_net.replace("baseline_memory_mi: 1024\n",
                                                f"baseline_memory_mi: 1024\n  vcpus: {bad}\n")) / "app.yaml")
            check(f"resources.vcpus {bad} is refused", False, "loaded")
        except M.ManifestError as e:
            check(f"resources.vcpus {bad} is refused", why in str(e), str(e))

    try:
        M.load(_write(tmp, "noimg", "custom", image=None, extra=res_net) / "app.yaml")
        check("custom process app without image is refused", False)
    except M.ManifestError as e:
        check("custom process app without image is refused", "runtime.image" in str(e), str(e))

    d_std = _write(tmp, "baked", "standard", image=None)
    m_std = M.load(d_std / "app.yaml")
    check("image-baked (standard) process app loads without image", m_std.runtime.image is None)
    check("runs_in_pod: standard process app is in-entity", M.runs_in_pod(m_std) is False)

    try:
        M.load(_write(tmp, "nores", "custom", image="python") / "app.yaml")
        check("image app without baseline_memory_mi/network is refused", False)
    except M.ManifestError as e:
        check("image app without baseline_memory_mi/network is refused",
              "baseline_memory_mi" in str(e) or "network" in str(e), str(e))

    # A process app that reaches nothing outside the platform declares an
    # empty network[] and installs (a game that keeps its own state: entity 9003,
    # project 9425, could not). A helper still must name hosts.
    m_none = M.load(_write(tmp, "noegress", "custom", image="python",
                           extra="resources:\n  baseline_memory_mi: 128\nnetwork: []\n") / "app.yaml")
    check("an image app with an empty network[] (reaches nothing) loads",
          m_none.network == [] and M.runs_in_pod(m_none) is True)
    from server.applications.build_contract import validate_build
    d_dep = _write(tmp, "noegress-deps", "custom", image="python",
                   extra="resources:\n  baseline_memory_mi: 128\nnetwork: []\n")
    (d_dep / "requirements.txt").write_text("requests\n")
    errs = validate_build(d_dep, M.load(d_dep / "app.yaml"))
    check("...but one that pip-installs at first start is told to list the package index",
          any("network[] is empty but the app installs dependencies" in e for e in errs), str(errs))
    errs = validate_build(tmp / "noegress", m_none)
    check("...and one with no dependencies passes the build check",
          not any("network" in e for e in errs), str(errs))
    # With hosts declared but not the index's, pip cannot
    # reach PyPI either — the app's container has no proxy of its own now.
    d_some = _write(tmp, "someegress-deps", "custom", image="python",
                    extra="resources:\n  baseline_memory_mi: 128\n"
                          "network: [accounts.google.com]\n")
    (d_some / "requirements.txt").write_text("requests\n")
    errs = validate_build(d_some, M.load(d_some / "app.yaml"))
    check("an app that pip-installs and declares other hosts is told which index hosts to add",
          any("add pypi.org, files.pythonhosted.org" in e for e in errs), str(errs))
    d_ok = _write(tmp, "someegress-ok", "custom", image="python",
                  extra="resources:\n  baseline_memory_mi: 128\n"
                        "network: [accounts.google.com, pypi.org, files.pythonhosted.org]\n")
    (d_ok / "requirements.txt").write_text("requests\n")
    errs = validate_build(d_ok, M.load(d_ok / "app.yaml"))
    check("...and one that declares both index hosts passes",
          not any("package index" in e for e in errs), str(errs))
    # What counts is what the file asks pip for, not that the file exists: a
    # standard-library app with a requirements.txt that names nothing, or a
    # declared lockfile that pins nothing, installs nothing at first start.
    d_stray = _write(tmp, "noegress-stray", "custom", image="python",
                     extra="resources:\n  baseline_memory_mi: 128\nnetwork: []\n")
    (d_stray / "requirements.txt").write_text("# standard library only\n\n")
    errs = validate_build(d_stray, M.load(d_stray / "app.yaml"))
    check("...and a requirements.txt that names nothing is not a dependency",
          not any("installs dependencies" in e for e in errs), str(errs))
    d_elock = _write(tmp, "noegress-elock", "custom", image="python",
                     extra=("resources:\n  baseline_memory_mi: 128\nnetwork: []\n"
                            "build:\n  lockfile: requirements.lock\n"))
    (d_elock / "requirements.lock").write_text("# nothing pinned\n")
    errs = validate_build(d_elock, M.load(d_elock / "app.yaml"))
    check("...nor is a declared lockfile that pins nothing",
          not any("installs dependencies" in e for e in errs), str(errs))
    (d_elock / "requirements.lock").write_text(
        "requests==2.32.3 \\\n    --hash=sha256:" + "a" * 64 + "\n")
    errs = validate_build(d_elock, M.load(d_elock / "app.yaml"))
    check("...but a declared lockfile that pins a package is",
          any("installs dependencies" in e and "(requirements.lock)" in e for e in errs),
          str(errs))
    try:
        M.load(_write(tmp, "helper-none", "custom", image=None, kind="helper",
                      extra="resources:\n  baseline_memory_mi: 128\nnetwork: []\n") / "app.yaml")
        check("a helper with an empty network[] is still refused", False)
    except M.ManifestError as e:
        check("a helper with an empty network[] is still refused", "network" in str(e), str(e))

    # The pod's limit is baseline_memory_mi and its request is the operator's
    # (manifest.APP_POD_REQUEST_MI): a limit under the request is a pod
    # Kubernetes refuses, so the app installs and never starts. Slack and
    # Discord shipped declaring 80 against a 128 request.
    low = lambda mi: f"resources:\n  baseline_memory_mi: {mi}\nnetwork: [github.com]\n"
    floor_p = getattr(M, "APP_POD_REQUEST_MI", {}).get("process", 128)
    floor_h = getattr(M, "APP_POD_REQUEST_MI", {}).get("helper", 64)
    try:
        M.load(_write(tmp, "lowproc", "custom", image="python", extra=low(80)) / "app.yaml")
        check("an image app whose limit is under its pod's request is refused", False)
    except M.ManifestError as e:
        check("an image app whose limit is under its pod's request is refused",
              "at least 128" in str(e), str(e))
    check("an image app at its pod's request loads",
          M.load(_write(tmp, "atproc", "custom", image="python",
                        extra=low(floor_p)) / "app.yaml").resources.baseline_memory_mi == 128)
    try:
        M.load(_write(tmp, "lowhelp", "community", image=None, kind="helper", extra=low(32)) / "app.yaml")
        check("a helper whose limit is under its pod's request is refused", False)
    except M.ManifestError as e:
        check("a helper whose limit is under its pod's request is refused",
              "at least 64" in str(e), str(e))
    check("a helper at its pod's request loads",
          M.load(_write(tmp, "athelp", "community", image=None, kind="helper",
                        extra=low(floor_h)) / "app.yaml").resources.baseline_memory_mi == 64)
    apps_root = Path(__file__).resolve().parent.parent / "system-apps"
    unloadable = []
    for y in sorted(apps_root.glob("*/app.yaml")):
        if y.parent.name.startswith("_"):
            continue
        try:
            M.load(y)
        except M.ManifestError as e:
            unloadable.append(f"{y.parent.name}: {e}")
    check("every first-party app's manifest loads (none under its pod's request)",
          not unloadable, "; ".join(unloadable))

    try:
        M.load(_write(tmp, "badimg", "custom", image="rust", extra=res_net) / "app.yaml")
        check("unknown image family is refused", False)
    except M.ManifestError as e:
        check("unknown image family is refused", "runtime.image" in str(e))

    try:
        M.load(_write(tmp, "staticimg", "custom", image="python", kind="static",
                      extra=res_net) / "app.yaml")
        check("image on a non-process kind is refused", False)
    except M.ManifestError as e:
        check("image on a non-process kind is refused", "process" in str(e))

    d_helper = _write(tmp, "helperapp", "community", image=None, kind="helper", extra=res_net)
    check("runs_in_pod: helper", M.runs_in_pod(M.load(d_helper / "app.yaml")) is True)

    # ── 2. installer descriptor ──────────────────────────────────────────
    from server.applications import installer as I
    sent: list = []

    class Bridge:
        _connected = True

        # request_full, mirroring the real client: the descriptor write reads
        # the ENVELOPE now, because the operator's install-time 402 has to be
        # told apart from "the bridge was down" (scripts/test_app_rt_over_plan.py).
        async def request_full(self, op, params=None):
            sent.append((op, params))
            return {"ok": True, "data": {}}

    class Pool:  # tokens.get_secret reads app_config through this
        async def fetchval(self, *a, **k):
            return None

    # Point the installer at the temp tree + a fixed secret.
    from server.applications import tokens as T
    orig_root, orig_secret = I.INSTALL_ROOT, T.get_secret
    I.INSTALL_ROOT = tmp

    async def _fixed_secret(pool):
        return b"k" * 32
    T.get_secret = _fixed_secret
    os.environ["ENTITY_ID"] = "9128"
    from server import brain_service as BS
    orig_pool = BS.get_pool
    BS.get_pool = lambda: Pool()
    try:
        ok = run(I.sync_app_runtime("trading-bot", True, bc=Bridge()))
        check("image app: descriptor sent", ok is True and sent, str(sent))
        desc = sent[-1][1]["descriptor"] if sent else {}
        check("descriptor form=process + image + command",
              desc.get("form") == "process" and desc.get("image") == "python"
              and desc.get("command") == ["bash", "run.sh"], str(desc))
        check("descriptor readyPath/portEnv/budget/network",
              desc.get("readyPath") == "/api/v1/ping" and desc.get("portEnv") == "PORT"
              and desc.get("budgetMi") == 1024 and desc.get("network") == ["github.com", "pypi.org"])
        check("descriptor kernelUrl is the entity pod's StatefulSet address",
              desc.get("kernelUrl") == "http://entity-9128-0.entity-9128.entity-9128.svc.cluster.local:8800",
              str(desc.get("kernelUrl")))
        check("descriptor wakeTimeoutS follows the manifest's ready timeout "
              "(the operator's 60s default outran every real first boot)",
              desc.get("wakeTimeoutS") == desc.get("readyTimeoutS") == m_pod.runtime.ready_timeout_seconds,
              str((desc.get("wakeTimeoutS"), desc.get("readyTimeoutS"))))
        tok = desc.get("sourceToken") or ""
        want = run(hb.app_source_token(Pool(), "trading-bot"))
        check("sourceToken is the derived HMAC (64 hex)", tok == want and len(tok) == 64)
        check("check_app_source_token accepts it / refuses others",
              run(hb.check_app_source_token(Pool(), "trading-bot", tok)) is True
              and run(hb.check_app_source_token(Pool(), "trading-bot", "x" * 64)) is False
              and run(hb.check_app_source_token(Pool(), "other-app", tok)) is False)
        sent.clear()
        ok = run(I.sync_app_runtime("baked", True, bc=Bridge()))
        check("standard process app writes NO descriptor", ok is False and not sent)
        sent.clear()
        ok = run(I.sync_app_runtime("noegress", True, bc=Bridge()))
        desc = sent[-1][1]["descriptor"] if sent else {}
        check("an image app that reaches nothing still gets its pod: descriptor sent, network []",
              ok is True and desc.get("form") == "process" and desc.get("network") == [], str(sent))
    finally:
        I.INSTALL_ROOT, T.get_secret, BS.get_pool = orig_root, orig_secret, orig_pool

    # ── 3+4. supervisor routes pod-backed apps to the lane ───────────────
    calls: list = []

    class FakeBridge:
        _connected = True

        async def request_full(self, op, params):
            calls.append((op, dict(params)))
            if op == "harness.wake":
                return {"ok": True, "data": {"url": "http://[fd00::9]:8851", "ready": True}}
            return {"ok": True, "data": {"ready_duration_s": 12.0}}

    hb._bridge = lambda: FakeBridge()
    hb._state.pop("trading-bot", None)
    billed: list = []

    async def _charge(**kw):
        billed.append(kw)
    import server.routes.compute as _compute
    _compute.charge_harness_compute = _charge

    check("app_runtime_state: not running before any wake",
          hb.app_runtime_state("trading-bot") == {"running": False, "url": None, "since": None})

    def installed(app_id, src, enabled=None):
        return R.InstalledApp(id=app_id, name="T", version="0.3.0", provenance="custom",
                              entrypoint="index.html", permissions=[], source_dir=str(src),
                              source_signature=None, installed_at=datetime.now(timezone.utc),
                              upgraded_at=None, status="active", app_license={},
                              authored_here=True, resident_enabled=enabled)

    sup = S.Supervisor()
    view = run(sup.open_window(installed=installed("trading-bot", d_pod),
                               manifest=m_pod, window_id="w1"))
    check("open_window on a pod app woke the lane, spawned nothing",
          isinstance(view, S.PodRuntime) and calls and calls[0][1] == {"app_rt": "trading-bot"}
          and not sup._by_app, str(calls))
    check("PodRuntime shape: pid None, base_url is the pod, port from the wake",
          view.pid is None and view.base_url == "http://[fd00::9]:8851" and view.port == 8851)
    check("is_alive follows the lane", view.is_alive() is True)
    check("app_runtime_state: running after the wake",
          hb.app_runtime_state("trading-bot")["running"] is True)
    check("supervisor.get returns the pod view", isinstance(sup.get("trading-bot"), S.PodRuntime))
    check("list_running includes it", [r.app_id for r in sup.list_running()] == ["trading-bot"])
    check("window-lifecycle open takes no resident hold",
          not (hb._state["trading-bot"].get("resident")))

    # runtime route semantics: start (resident) → hold; stop → release + bill
    class PoolWithRow:
        async def fetchval(self, *a, **k):
            return None

    orig_get = R.get

    async def _get(pool, app_id):
        return installed(app_id, d_pod, enabled=None)
    R.get = _get
    try:
        calls.clear()
        v2 = run(sup.start_app("trading-bot", pool=PoolWithRow(), resident=True))
        check("start_app(resident) on a pod app holds the lease",
              isinstance(v2, S.PodRuntime) and v2.resident is True
              and hb._state["trading-bot"].get("resident") is True
              and hb._state["trading-bot"].get("lease_task") is not None)
        hb.app_window_closed("trading-bot")
        check("a resident pod app ignores the window-close release",
              hb._state["trading-bot"].get("release_task") is None)
        gone = run(sup.stop_app("trading-bot"))
        check("stop_app releases the pod and bills the window",
              gone is True and any(op == "harness.release" for op, _p in calls)
              and billed and billed[-1]["session_id"] == "app-rt:trading-bot")
        check("after stop: not running, no view",
              hb.app_runtime_state("trading-bot")["running"] is False
              and sup.get("trading-bot") is None)

        # A resident start whose wake does NOT answer (slow first boot: the
        # operator 504s "still starting") must still leave the hold in place,
        # so the lane keeps re-waking and the sweep never reaps the pod.
        class StillStarting(FakeBridge):
            async def request_full(self, op, params):
                calls.append((op, dict(params)))
                if op == "harness.wake":
                    return {"ok": False, "error": "operator /harness/wake 504: still starting"}
                return {"ok": True, "data": {"ready_duration_s": None}}
        hb._bridge = lambda: StillStarting()
        hb._state.pop("trading-bot", None)
        try:
            run(sup.start_app("trading-bot", pool=PoolWithRow(), resident=True))
            check("a resident start with an unanswered wake raises", False)
        except S.SupervisorError as e:
            check("a resident start with an unanswered wake raises honestly",
                  "did not wake" in str(e) and "keeps re-waking" in str(e), str(e))
        st = hb._state.get("trading-bot") or {}
        check("...but the resident hold is already in place",
              st.get("resident") is True and st.get("lease_task") is not None
              and not st["lease_task"].done(), str({k: st.get(k) for k in ("resident", "url")}))
        run(hb.app_release("trading-bot"))
    finally:
        R.get = orig_get
        hb._stop_lease("trading-bot")

    # ── 4b. the source route + middleware shape ──────────────────────────
    from server.routes import applications as A
    paths = {getattr(r, "path", "") for r in A.router.routes}
    check("GET /api/applications/{app_id}/source exists",
          "/api/applications/{app_id}/source" in paths)
    from server.middleware import localhost_only as L
    # A standalone Host has no app runtime pods: the source path is not
    # exempt, so a fetch from off the box needs the owner's session.
    check("middleware does not exempt the source path on a standalone Host",
          not any("/api/applications/x/source".startswith(p) for p in L._SELF_AUTH_PREFIXES)
          and not hasattr(L, "_SELF_AUTH_SHAPES"))

    # ── 5. proxy reads runtime.image ─────────────────────────────────────
    from server.applications import proxy as P
    check("proxy._read_image: image app", P._read_image(d_pod) == "python")
    check("proxy._read_image: baked app", P._read_image(d_std) is None)
    src = (_ROOT / "server/applications/proxy.py").read_text()
    i = src.index('if kind == "process":')
    j = src.index('if kind == "helper":')
    check("process branch takes the pod lane when an image is declared",
          "_read_image(install_dir)" in src[i:j] and "ensure_awake_app" in src[i:j])

    # ── contract text advertises the family the loader accepts ──────────
    from server.applications import build_contract as BC
    text = BC.app_sdk_build_contract()
    check("build contract names runtime.image and the toolchain",
          "image: python" in text and "listens on every interface" in text
          and "POD IP" in text)
    brief = BC.host_runtime_brief()
    check("host runtime brief says a process app has a real toolchain",
          "toolchain" in brief and "python" in brief)

    print()
    if FAILED:
        print(f"[app-rt-process] {len(FAILED)} FAILED: {FAILED}")
        return 1
    print("[app-rt-process] all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
