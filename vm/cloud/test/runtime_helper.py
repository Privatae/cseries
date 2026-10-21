"""The local runtime's VM test, from inside the Host container:

  podman exec cseries-host python3 /tmp/runtime_helper.py <command> [args]

It runs the Host's own code in a process of its own, in the Host
container (so the runtime's peer check, uid 1000 in cseries-host.service's
cgroup, admits it), against the Host's own database. It stands in for what a
Host's turn would do with its tools, which this test has no model to drive.

  status                         runtime.status, through the Host's client
  op <op> <params json>          one runtime operation, through the Host's client
                                 (a wake carries its subject's egress credential)
  install <app id>               fetch the app from the App Store and install it,
                                 as the Host's install_app tool does
  install-file <path>            install a signed bundle from a file, as an
                                 upload to the Host's install route does
  wake-app <app id>              wake an app's container as the app's window and
                                 its MCP connection do (ensure_awake_app)
  mcp-tools <app id>             list an app's MCP tools the way the Host does on
                                 first use (mcp_connections.fetch_tools: it wakes
                                 the app and asks with the app's own token)
  pair-telegram <key name> <bot token>
                                 put the bot token in Keys, start a Telegram
                                 pairing as propose_nac_policy kind=connector
                                 does, and wait for its capability to be live
  wake-browser                   wake the browser as the Host's browser lane does
  browser <method> <path> [json] call the awake browser's control API with the
                                 machine's control token
  browser-frame                  open the awake browser's viewport WebSocket with
                                 the control token, as the Host's proxy does, and
                                 wait for one screen frame
  release <app|harness> <id>     release a container through the Host's client
  http <url> [header: value]     GET url from the Host container; prints the status
"""

import asyncio
import json
import os
import sys
import time
import types
import urllib.error
import urllib.request

sys.path[:0] = ["/app/brain", "/app"]
# The Host's server.main is the whole web application; this process needs
# only its platform client, which on this Host is NoPlatform.
_main = types.ModuleType("server.main")
_main._ws_connections = set()


def _no_platform():
    from server.bridge.no_platform import NoPlatform
    return NoPlatform()


_main.get_bridge_client = _no_platform
sys.modules["server.main"] = _main


def dsn():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    try:
        with open("/proc/1/environ", "rb") as f:
            for item in f.read().split(b"\0"):
                if item.startswith(b"DATABASE_URL="):
                    return item.split(b"=", 1)[1].decode()
    except OSError:
        pass
    return "postgresql://entity@/hostmem?host=/var/run/postgresql"


async def pool():
    import asyncpg
    from server import brain_service
    p = await asyncpg.create_pool(dsn(), min_size=1, max_size=4)
    brain_service._pool = p
    return p


def out(obj):
    print(json.dumps(obj, sort_keys=True, default=str))


async def cmd_status():
    from server.applications.local_runtime import runtime_client
    out(await runtime_client().request_full("runtime.status", {}))


async def cmd_op(op, params):
    await pool()
    from server.applications.local_runtime import runtime_client
    out(await runtime_client().request_full(op, json.loads(params)))


async def cmd_install(app_id):
    p = await pool()
    from server.applications import installer
    from server.routes.applications import _fetch_bundle_from_store
    data = await _fetch_bundle_from_store(app_id, None)
    app = await installer.install_bundle(p, data, source="manual")
    out({"installed": app.id, "version": app.version})


async def cmd_install_file(path):
    p = await pool()
    from server.applications import installer
    with open(path, "rb") as f:
        data = f.read()
    app = await installer.install_bundle(p, data, source="manual")
    out({"installed": app.id, "version": app.version})


async def cmd_wake_app(app_id):
    await pool()
    from server.applications import harness_backend as hb
    try:
        host = await hb.ensure_awake_app(app_id)
    except Exception as e:  # noqa: BLE001 - the test reads why
        out({"upstream": None, "error": f"{type(e).__name__}: {e}"[:400]})
        return
    out({"upstream": host, "error": None})


async def cmd_mcp_tools(app_id):
    p = await pool()
    from server import brain_service, mcp_connections
    try:
        r = await mcp_connections.fetch_tools(p, int(brain_service._entity_id), app_id)
    except Exception as e:  # noqa: BLE001 - the test reads why
        out({"tools": 0, "error": f"{type(e).__name__}: {e}"[:400]})
        return
    out({"tools": r.get("tools"), "error": None})


async def cmd_pair_telegram(name, token):
    p = await pool()
    from server import brain_service
    from server.network import nac_connector
    from server.vault import api as vault_api
    await vault_api.store_secret(p, entity_id=int(brain_service._entity_id), name=name,
                                 cred_type="api_key", plaintext=token.encode(), actor="test")
    res = await nac_connector.propose_connector(p, app_id="telegram", platform="telegram",
                                                token_name=name)
    t = nac_connector._deliveries.get("telegram")
    if t is not None:
        await asyncio.wait_for(t, timeout=600)
    row = await p.fetchrow("SELECT cap_hash IS NOT NULL AS live, pending_hash IS NOT NULL AS pending "
                           "FROM nac_connector_caps WHERE app_id='telegram'")
    out({"grant_id": res.get("draft_id"), "capability": res.get("capability"),
         "live": bool(row and row["live"]), "pending": bool(row and row["pending"])})


async def cmd_wake_browser():
    await pool()
    from server.applications import harness_backend as hb
    host = await hb.ensure_awake("browser", "browser")
    out({"upstream": host, "error": (hb._state.get("browser") or {}).get("wake_error")})


def _browser_url():
    from server.applications.local_runtime import LocalRuntimeClient, socket_path
    st = asyncio.run(LocalRuntimeClient(socket_path()).request_full("runtime.status", {}))
    for u in (st.get("data") or {}).get("units") or []:
        if u.get("name") == "h-browser" and u.get("ip"):
            return f"http://{u['ip']}:8830"
    raise SystemExit("the browser is not awake")


def cmd_browser(method, path, body=None):
    url = _browser_url() + path
    req = urllib.request.Request(url, method=method, data=body.encode() if body else None,
                                 headers={"Authorization": "Bearer " + os.environ.get("BROWSER_CONTROL_TOKEN", ""),
                                          "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=90) as r:
            out({"status": r.status, "body": json.loads(r.read() or b"null")})
    except urllib.error.HTTPError as e:
        out({"status": e.code, "body": e.read().decode("utf-8", "replace")[:500]})


async def cmd_browser_frame(base):
    import aiohttp
    url = "ws://" + base[len("http://"):] + "/viewport/ws"
    headers = {"X-Browser-Token": os.environ.get("BROWSER_CONTROL_TOKEN", "")}
    seen, frame = [], False
    async with aiohttp.ClientSession(trust_env=False) as s:
        ws = await asyncio.wait_for(s.ws_connect(url, headers=headers), 30)
        async with ws:
            await ws.send_str(json.dumps({"t": "resize", "w": 800, "h": 600}))
            end = time.monotonic() + 45
            while not frame and time.monotonic() < end:
                try:
                    m = await asyncio.wait_for(ws.receive(), max(1.0, end - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if m.type == aiohttp.WSMsgType.BINARY:
                    frame = len(m.data) > 0
                    seen.append("binary")
                elif m.type == aiohttp.WSMsgType.TEXT:
                    try:
                        d = json.loads(m.data)
                    except ValueError:
                        d = {}
                    seen.append(str(d.get("t")))
                    frame = d.get("t") == "frame" and bool(d.get("data"))
                else:
                    seen.append(str(m.type))
                    break
    out({"frame": frame, "seen": seen[:8]})


async def cmd_release(kind, ident):
    from server.applications.local_runtime import runtime_client
    key = "app_rt" if kind == "app" else "harness"
    out(await runtime_client().request_full("harness.release", {key: ident}))


def cmd_http(url, header=None):
    headers = {}
    if header:
        k, _, v = header.partition(":")
        headers[k.strip()] = v.strip()
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as r:
            out({"status": r.status, "body": r.read(400).decode("utf-8", "replace")})
    except urllib.error.HTTPError as e:
        out({"status": e.code, "body": e.read(400).decode("utf-8", "replace")})
    except OSError as e:
        out({"status": None, "error": type(e).__name__})


def main(argv):
    cmd, args = argv[0], argv[1:]
    if cmd == "browser":
        return cmd_browser(*args)
    if cmd == "http":
        return cmd_http(*args)
    if cmd == "browser-frame":
        return asyncio.run(cmd_browser_frame(_browser_url()))
    fn = {"status": cmd_status, "op": cmd_op, "install": cmd_install,
          "install-file": cmd_install_file, "wake-app": cmd_wake_app, "mcp-tools": cmd_mcp_tools,
          "pair-telegram": cmd_pair_telegram, "wake-browser": cmd_wake_browser,
          "release": cmd_release}[cmd]
    asyncio.run(fn(*args))


if __name__ == "__main__":
    main(sys.argv[1:])
