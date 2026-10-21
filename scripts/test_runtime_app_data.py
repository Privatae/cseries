#!/usr/bin/env python3
"""An app's kept data on a machine with the local runtime, from the Host's
routes to the runtime and back (server/routes/applications.py runtime_app_data,
delete_runtime_app_data, uninstall; vm/runtime/lib/cseries_runtime/server.py
app_data, delete_app_data).

The runtime keeps an app's data volume across releases and uninstalls and
never deletes one on its own: nothing the owner kept goes unless the owner
chooses it. Here the real runtime runs behind a real unix socket (its peer
check admitting this process), over the fake podman, and the Host's routes and
client talk to it:

  * GET /api/applications/runtime/app-data lists each app's kept data with its
    size, installed or not, and the disk's floors;
  * an uninstall without purge keeps the data; with ?purge=true (the App
    Store's "Also delete this app's data") it is deleted after the app's
    descriptor is gone, and the answer says how much was freed;
  * DELETE /api/applications/runtime/app-data/<id> deletes a removed app's
    kept data, is refused 409 for an app that is still installed, and 400 for
    something that is not an app id;
  * a Host whose machine has no local runtime says so and touches nothing.

Container-free: python3 scripts/test_runtime_app_data.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path(os.environ.get("APP_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "vm/runtime/lib"))
sys.path.insert(0, str(HERE))

FAILURES = []


def check(cond, name, detail=""):
    if cond:
        print(f"OK    {name}")
    else:
        print(f"FAIL  {name} {detail}")
        FAILURES.append(name)


# server.main is the kernel's whole web app; the routes reach only for this.
fake_main = types.ModuleType("server.main")
fake_main.get_bridge_client = lambda: None
fake_main._ws_connections = set()
sys.modules["server.main"] = fake_main

import runtime_fake_podman as fake  # noqa: E402
from cseries_runtime import common as C  # noqa: E402
from cseries_runtime import guard as G  # noqa: E402
from cseries_runtime import server as S  # noqa: E402
from cseries_runtime.podman import Podman  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from server import brain_service  # noqa: E402
from server.applications import installer, local_runtime, registry  # noqa: E402
from server.routes import applications as APPS  # noqa: E402

PY = "ghcr.io/privatae/cseries-app-runtime-python@sha256:" + "1" * 64


def own_cgroup():
    for line in open("/proc/self/cgroup", encoding="ascii"):
        if line.startswith("0::"):
            return line[3:].strip()
    return ""


def dv(app):
    """The kept data volume's name (common.data_volume), spelled out so this
    test reads the same against a runtime that has no such helper."""
    return f"cseries-app-{app}-data"


def desc(app):
    return {"app_id": app, "enabled": True, "descriptor": {
        "budgetMi": 128, "network": ["api.example.test"], "version": "1.0.0", "form": "process",
        "image": "python", "command": ["python3", "main.py"], "sourceToken": "ab" * 32}}


async def code_of(coro):
    try:
        return await coro
    except HTTPException as e:
        return e.status_code
    except AttributeError as e:
        return f"no such route ({e})"


def volumes():
    return fake.state(os.environ["FAKE_PODMAN_STATE"])["volumes"]


async def main():
    tmp = tempfile.mkdtemp(prefix="appdata-", dir=os.environ.get("TMPDIR"))
    paths = C.Paths(tmp)
    os.environ["FAKE_PODMAN_STATE"] = os.path.join(tmp, "podman.json")
    exe = fake.install(tmp)
    os.makedirs(paths.etc)
    with open(paths.images_json(), "w") as f:
        json.dump({"images": {"python": PY}}, f)
    st = fake.state(os.environ["FAKE_PODMAN_STATE"])
    st["images"] = {PY: "a" * 64}
    fake.write_state(os.environ["FAKE_PODMAN_STATE"], st)

    async def ready(*a):
        return True
    rt = S.Runtime(paths, Podman(exe), G.Guard(total_mb=16384, disk_path=tmp),
                   http_probe=ready, tcp_probe=ready, chown=lambda *a: None, machine="x86_64",
                   peer_uid=os.getuid(), peer_cgroups=(own_cgroup().rstrip("/") + "/",))
    sock_path = os.path.join(tmp, "runtime.sock")
    server = await rt.serve(S.listening_socket(sock_path))

    installed = {"notes", "photos"}

    async def reg_get(pool, app_id):
        return object() if app_id in installed else None

    async def fake_uninstall(pool, app_id, *, purge=False):
        # What installer.uninstall_app does that matters here: the app goes,
        # and its runtime descriptor with it (sync_app_runtime(app_id, False)).
        installed.discard(app_id)
        await installer.sync_app_runtime(app_id, False, bc=local_runtime.runtime_client())
        return {"uninstalled": app_id, "purge": purge}

    saved = (brain_service.get_pool, registry.get, installer.uninstall_app)
    brain_service.get_pool = lambda: object()
    registry.get = reg_get
    installer.uninstall_app = fake_uninstall
    try:
        os.environ.pop(local_runtime.ENV_SOCKET, None)
        r = await code_of(APPS.runtime_app_data())
        check(r == {"available": False, "apps": []},
              "a Host with no local runtime has no app data on its machine to list", r)

        os.environ[local_runtime.ENV_SOCKET] = sock_path
        for app in ("notes", "photos"):
            a = await rt.handle("workspace.set_app_runtime", desc(app))
            w = await rt.handle("harness.wake", {"app_rt": app})
            check(a["ok"] and w["ok"], f"{app} is installed and woken", (a, w))
            with open(os.path.join(volumes()[dv(app)]["mount"], "db"), "wb") as f:
                f.write(os.urandom((2 if app == "notes" else 1) << 20))

        r = await code_of(APPS.runtime_app_data())
        by = {a["app_id"]: a for a in (r.get("apps", []) if isinstance(r, dict) else [])}
        check(isinstance(r, dict) and r.get("available") and set(by) == {"notes", "photos"} and
              by["notes"]["bytes"] >= 2 << 20 and by["notes"]["installed"] and
              r.get("stop_floor_bytes") == G.DISK_STOP_FLOOR and r.get("wake_floor_bytes") == G.DISK_WAKE_FLOOR,
              "the Host lists each app's kept data on this machine, with its size", r)

        check(await code_of(APPS.delete_runtime_app_data("notes")) == 409 and dv("notes") in volumes(),
              "an installed app's data cannot be deleted from under it")
        check(await code_of(APPS.delete_runtime_app_data("..")) == 400 and
              await code_of(APPS.delete_runtime_app_data("Notes")) == 400,
              "something that is not an app id is refused")

        out = await APPS.uninstall("photos", purge=False)
        check(out.get("uninstalled") == "photos" and "app_data_deleted" not in out and
              dv("photos") in volumes(),
              "an uninstall the owner did not tick keeps the app's data", (out, sorted(volumes())))
        r = await code_of(APPS.runtime_app_data())
        by = {a["app_id"]: a for a in (r.get("apps", []) if isinstance(r, dict) else [])}
        check(by.get("photos", {}).get("installed") is False,
              "and the list shows it as kept from a removed app", by)

        out = await APPS.uninstall("notes", purge=True)
        check(out.get("app_data_deleted") is True and out.get("app_data_freed_bytes", 0) >= 2 << 20 and
              dv("notes") not in volumes() and dv("photos") in volumes(),
              "an uninstall with \"Also delete this app's data\" deletes that app's data and no other",
              (out, sorted(volumes())))

        out = await code_of(APPS.delete_runtime_app_data("photos"))
        check(isinstance(out, dict) and out.get("deleted") is True and out.get("freed_bytes", 0) >= 1 << 20 and
              dv("photos") not in volumes(),
              "a removed app's kept data is deleted when the owner asks", (out, sorted(volumes())))
        r = await code_of(APPS.runtime_app_data())
        check(isinstance(r, dict) and r.get("available") and r.get("apps") == [],
              "and nothing is listed after", r)
    finally:
        brain_service.get_pool, registry.get, installer.uninstall_app = saved
        os.environ.pop(local_runtime.ENV_SOCKET, None)
        server.close()
        await server.wait_closed()


asyncio.run(main())
print()
if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    sys.exit(1)
print("all runtime app data checks passed")
