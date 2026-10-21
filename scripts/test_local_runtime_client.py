#!/usr/bin/env python3
"""The Host's side of the local runtime (server/applications/local_runtime.py
and the lanes that use it), against a fake runtime on a real unix socket.

  * with CSERIES_RUNTIME_SOCKET set, the app lane (ensure_awake_app), the
    harness lane (ensure_awake, review's wake/capacity/login) and the
    installer's descriptor and engine syncs send their operations to the
    socket; without it they go to the bridge client, as before;
  * every wake carries the Host's egress credential for the container's
    subject (app:<id>, harness:<id>), and a wake that already carries one
    keeps it;
  * the runtime's 402 for an app that cannot fit raises AppRuntimeOverPlan
    with its numbers, and says "this machine", not "this plan";
  * an appRuntimes refusal makes the app lane re-sync its descriptor once and
    retry once; review's login wake still retries the not-provisioned answer;
  * a release answered billing none sends no compute charge and logs no
    ERROR; a run's compute on the runtime is not billed either;
  * the kernel URL the containers get is http://$CSERIES_RUNTIME_KERNEL_ADDR;
  * a runtime that does not answer is a transport failure, which the release
    retry treats as one to try again.

Container-free: python3 scripts/test_local_runtime_client.py
"""
import asyncio
import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "vm/runtime/lib"))

FAILURES = []


def check(cond, name, detail=""):
    if cond:
        print(f"OK    {name}")
    else:
        print(f"FAIL  {name} {detail}")
        FAILURES.append(name)


# A stand-in for server.main: only get_bridge_client, which answers a stub.
class StubBridge:
    _connected = True
    connected = True

    def __init__(self):
        self.calls = []

    async def request_full(self, op, params=None):
        self.calls.append((op, params))
        if op == "harness.wake":
            return {"ok": True, "data": {"url": "http://bridge-pod.test:8850", "ready": True}}
        return {"ok": True, "data": {}}

    async def request(self, op, params=None, **kw):
        return (await self.request_full(op, params)).get("data")


STUB = StubBridge()
fake_main = types.ModuleType("server.main")
fake_main.get_bridge_client = lambda: STUB
fake_main._ws_connections = set()
sys.modules["server.main"] = fake_main

from server import brain_service  # noqa: E402
from server.applications import harness_backend as hb  # noqa: E402
from server.applications import installer  # noqa: E402
from server.applications import local_runtime as lr  # noqa: E402
from server.bridge.bridge_client import release_error_is_transport  # noqa: E402
from cseries_runtime.guard import over_plan_text  # noqa: E402


class FakeRuntime:
    """A unix socket server that records each request and answers from a
    handler the scenario sets."""

    def __init__(self, path):
        self.path = path
        self.seen = []
        self.answer = lambda op, params: {"ok": True, "data": {}}
        self.server = None

    async def _conn(self, reader, writer):
        line = await reader.readline()
        msg = json.loads(line)
        self.seen.append((msg["op"], msg.get("params") or {}))
        ans = self.answer(msg["op"], msg.get("params") or {})
        if asyncio.iscoroutine(ans):
            ans = await ans
        writer.write(json.dumps(ans).encode() + b"\n")
        await writer.drain()
        writer.close()

    async def start(self):
        self.server = await asyncio.start_unix_server(self._conn, path=self.path)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


async def fake_egress(pool, subject):
    return "e" * 60 + subject[-4:].encode().hex()[:4]


async def fake_source_token(pool, app_id):
    return "ab" * 32


hb.egress_subject_token = fake_egress
hb.app_source_token = fake_source_token
brain_service._pool = object()


def fresh():
    hb._state.clear()
    hb._wake_locks.clear()


async def main():
    tmp = tempfile.mkdtemp()
    sock = os.path.join(tmp, "runtime.sock")
    rt = FakeRuntime(sock)
    await rt.start()
    try:
        # ── routing ──────────────────────────────────────────────────────────
        os.environ.pop(lr.ENV_SOCKET, None)
        check(lr.runtime_client() is STUB and hb._bridge() is STUB,
              "without CSERIES_RUNTIME_SOCKET the lanes use the bridge client, as before")
        fresh()
        url = await hb.ensure_awake_app("telegram")
        check(url == "bridge-pod.test:8850" and STUB.calls and STUB.calls[-1][0] == "harness.wake" and
              not rt.seen, "and an app wake goes to the bridge", (url, STUB.calls[-1:]))
        STUB.calls.clear()

        os.environ[lr.ENV_SOCKET] = sock
        c = lr.runtime_client()
        check(isinstance(c, lr.LocalRuntimeClient) and c._connected and hb._bridge() is c,
              "with it set, the lanes use the local runtime's client")
        check(not lr.LocalRuntimeClient(os.path.join(tmp, "absent.sock"))._connected,
              "a client whose socket is not there reports not connected")

        # ── the app lane ─────────────────────────────────────────────────────
        fresh()
        rt.answer = lambda op, p: {"ok": True, "data": {"app_rt": p.get("app_rt"), "ready": True,
                                                        "url": "http://10.89.78.16:8850"}}
        url = await hb.ensure_awake_app("telegram")
        op, params = rt.seen[-1]
        check(url == "10.89.78.16:8850" and op == "harness.wake" and params.get("app_rt") == "telegram" and
              not STUB.calls, "an app wake goes to the runtime and its address comes back", (url, rt.seen[-1]))
        check((params.get("creds") or {}).get("egress-subject-token") == await fake_egress(None, "app:telegram"),
              "the app wake carries the egress credential for app:<id>", params)

        # appRuntimes: one re-sync, one retry.
        fresh()
        resyncs = []

        async def resync(app_id, enabled):
            resyncs.append(app_id)
            return True
        hb.set_descriptor_resync(resync)
        answers = iter([{"ok": False, "error": "no appRuntimes descriptor for photocraft", "code": 404},
                        {"ok": True, "data": {"url": "http://10.89.78.17:8850", "ready": True}}])
        rt.answer = lambda op, p: next(answers)
        n0 = len(rt.seen)
        url = await hb.ensure_awake_app("photocraft")
        check(url == "10.89.78.17:8850" and resyncs == ["photocraft"] and len(rt.seen) - n0 == 2,
              "the runtime's appRuntimes refusal re-syncs the descriptor once and retries once",
              (url, resyncs, len(rt.seen) - n0))

        # The installer's descriptor sync, and the runtime's 402.
        inst = pathlib.Path(tmp) / "apps"
        (inst / "telegram").mkdir(parents=True)
        shutil.copy(ROOT / "system-apps/telegram/app.yaml", inst / "telegram/app.yaml")
        installer.INSTALL_ROOT = inst
        os.environ[lr.ENV_KERNEL_ADDR] = "10.89.78.2:8800"
        rt.answer = lambda op, p: {"ok": True, "data": {"app_id": p["app_id"], "enabled": True}}
        sent = await installer.sync_app_runtime("telegram", True)
        op, params = rt.seen[-1]
        d = params.get("descriptor") or {}
        check(sent is True and op == "workspace.set_app_runtime" and d.get("form") == "process" and
              d.get("image") == "python" and d.get("lifecycle") == "resident" and
              d.get("kernelUrl") == "http://10.89.78.2:8800" and d.get("sourceToken") == "ab" * 32,
              "the installer's descriptor goes to the runtime, with the lifecycle and the runtime's kernel URL", d)
        text = over_plan_text(4160, 2764).replace("/harness/wake", "/set-app-runtime")
        rt.answer = lambda op, p: {"ok": False, "error": text, "code": 402}
        try:
            await installer.sync_app_runtime("telegram", True)
            check(False, "the runtime's 402 raises AppRuntimeOverPlan")
        except installer.AppRuntimeOverPlan as e:
            check(e.needs_mi == 4160 and e.headroom_mi == 2764 and "this machine has 2764MiB spare" in str(e)
                  and "plan" not in str(e), "the runtime's 402 raises AppRuntimeOverPlan saying this machine", str(e))
        plat = installer.AppRuntimeOverPlan("x", {"needs_mi": 1, "headroom_mi": 0, "upgrade_to": "Pro"})
        check("this plan has" in str(plat), "a plan's refusal still names the plan", str(plat))

        # Engines: the installer's set_harness goes to the runtime.
        rt.answer = lambda op, p: {"ok": True, "data": {"harness": "opencode", "enabled": True}}
        ok = await installer._sync_app_harness("opencode", True)
        check(ok and rt.seen[-1] == ("workspace.set_harness", {"harness_id": "opencode", "enabled": True}),
              "the installer's engine sync goes to the runtime", rt.seen[-1])

        # ── the harness lane ─────────────────────────────────────────────────
        fresh()
        rt.answer = lambda op, p: {"ok": True, "data": {"harness": "browser", "ready": True,
                                                        "url": "http://10.89.78.18:8830", "image": "img",
                                                        "cap": None, "slots": 1}}
        host = await hb.ensure_awake("browser", "browser")
        op, params = rt.seen[-1]
        check(host == "10.89.78.18:8830" and params.get("harness") == "browser" and
              (params.get("creds") or {}).get("egress-subject-token") == await fake_egress(None, "harness:browser"),
              "the browser wake goes to the runtime and keeps the egress credential it carries", params)

        from server.routes import review
        review._PROVISION_WAKE_RETRY_S = 0.01
        seq = []

        def login_answers(op, p):
            seq.append(op)
            if op == "workspace.set_harness":
                return {"ok": True, "data": {}}
            if seq.count("harness.wake") == 1:
                return {"ok": False, "error": "is 'kimi' in Entity.spec.harnesses?", "code": 404}
            return {"ok": True, "data": {"harness": "kimi", "url": "http://10.89.78.19:8847", "cap": "c" * 43,
                                         "ready": True, "slots": 1}}
        rt.answer = login_answers
        w = await review._wake_for_login("kimi")
        wake_params = [p for o, p in rt.seen if o == "harness.wake"][-1]
        check(w.get("ok") and w.get("url") == "http://10.89.78.19:8847" and
              seq == ["workspace.set_harness", "harness.wake", "harness.wake"],
              "review's login wake goes to the runtime and retries the not-provisioned answer", (w, seq))
        check((wake_params.get("creds") or {}).get("egress-subject-token") == await fake_egress(None, "harness:kimi"),
              "a coding engine's wake carries the egress credential for harness:<id>", wake_params)
        rt.answer = lambda op, p: {"ok": True, "data": {"gated": True, "fits": ["codex"], "refused": [{
            "harness": "kimi", "needs_mi": 1024}], "headroom_mi": 900, "remaining_mi": 0, "upgrade_to": None,
            "pool_add_mi": None, "slots": {"codex": 1}}}
        cap = await review._fanout_capacity(["codex", "kimi"])
        check(cap and cap["fits"] == ["codex"] and cap["refused"] == [{"harness": "kimi", "needs_mi": 1024}] and
              rt.seen[-1][0] == "harness.capacity",
              "review's fan-out capacity check reads the runtime's answer", cap)

        # ── billing ──────────────────────────────────────────────────────────
        charges = []
        from server.routes import compute as compute_mod

        async def charge(**kw):
            charges.append(kw)
            return None, None
        orig_charge = compute_mod.charge_harness_compute
        compute_mod.charge_harness_compute = charge
        errors = []

        class Catch(logging.Handler):
            def emit(self, rec):
                if rec.levelno >= logging.ERROR:
                    errors.append(rec.getMessage())
        logging.getLogger().addHandler(Catch())
        fresh()
        hb._st("telegram", hb._APP_RT_MARK)
        rt.answer = lambda op, p: {"ok": True, "data": {"ready_duration_s": 42.0, "vcpus": 1,
                                                        "window_id": None, "scaled_to": 0, "billing": "none"}}
        await hb._release_and_bill_app("telegram")
        await hb._release_and_bill("browser", "browser")
        check(rt.seen[-1][0] == "harness.release" and not charges and not errors,
              "a release answered billing none sends no compute charge and logs no error", (charges, errors))
        compute_mod.charge_harness_compute = orig_charge
        got = await compute_mod.charge_harness_compute(session_id="s", harness="codex", compute_s=30.0)
        check(got == (None, None) and not errors and not STUB.calls,
              "a run's compute on the runtime is not billed and not logged as unbilled", (got, errors))

        # ── the kernel URL, and a runtime that does not answer ───────────────
        check(hb.kernel_url_for_pods() == "http://10.89.78.2:8800" and
              hb.kernel_routable_base() == "http://10.89.78.2:8800",
              "the containers' kernel URL is the runtime's address for the Host")
        os.environ.pop(lr.ENV_KERNEL_ADDR, None)
        os.environ["ENTITY_ID"] = "9001"
        check(hb.kernel_url_for_pods().startswith("http://entity-9001-0."),
              "without it, the cluster name, as before")
        dead = os.path.join(tmp, "dead.sock")
        open(dead, "w").close()
        env = await lr.LocalRuntimeClient(dead).request_full("harness.release", {"app_rt": "telegram"})
        check(env.get("ok") is False and env.get("error_kind") == "infra" and release_error_is_transport(env),
              "a runtime that does not answer is a transport failure the release retries", env)
        env = await lr.LocalRuntimeClient(sock).request_full("memory.search", {})
        check(env.get("ok") is False and "not a local runtime operation" in env.get("error", ""),
              "an operation that is not the runtime's is never sent to it", env)
    finally:
        await rt.stop()
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main())
print()
if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    sys.exit(1)
print("all local runtime client checks passed")
