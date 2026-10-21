#!/usr/bin/env python3
"""The localhost gate lets the local runtime's containers reach the few Host
routes they need, from the runtime's network only
(server/middleware/localhost_only.py, server/middleware/owner_session.py).

A container the local runtime starts calls this Host from its own address on
the runtime's network (CSERIES_RUNTIME_NET). Without these exemptions every
such call was refused 403: an app could not fetch its source or ship its log,
a coding engine could not reach /mcp, and the ship proof's browser could not
load the page it proves. Each route still checks its own capability; the gate
only stops refusing on the caller's address.

  * from inside the runtime network: /api/applications/<id>/source and
    /runtime-log, /mcp carrying the harness capability header, /system-apps/
    carrying the verify header, and /js/app-sdk.js with a LIVE verify cap
    reach their handlers;
  * the same paths from any other address, or with the variable unset, are
    403, as before; every other path from inside is 403;
  * a page request from inside is not sent to the sign-in page (it has no
    browser session to make), and from outside it still is.

Container-free: python3 scripts/test_localhost_gate_runtime_net.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

FAILURES = []


def check(cond, name, detail=""):
    if cond:
        print(f"OK    {name}")
    else:
        print(f"FAIL  {name} {detail}")
        FAILURES.append(name)


from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from mastercode.runtime import app_verify as AV  # noqa: E402
from server.middleware.localhost_only import LocalhostOnlyMiddleware  # noqa: E402
from server.middleware.owner_session import OwnerSessionGate  # noqa: E402


async def reached(request):
    return JSONResponse({"reached": request.url.path})

app = Starlette(routes=[
    Route("/api/applications/{app_id}/source", reached, methods=["GET"]),
    Route("/api/applications/{app_id}/runtime-log", reached, methods=["POST"]),
    Route("/api/applications/{app_id}/other", reached, methods=["GET"]),
    Route("/api/applications/{a}/{b}/source", reached, methods=["GET"]),
    Route("/mcp", reached, methods=["POST"]),
    Route("/system-apps/{rest:path}", reached, methods=["GET"]),
    Route("/js/app-sdk.js", reached, methods=["GET"]),
    Route("/api/brain/x", reached, methods=["POST"]),
])
app.add_middleware(LocalhostOnlyMiddleware)
app = OwnerSessionGate(app)

INSIDE = "10.89.78.20"
# 10.89.78.1 is the runtime network's gateway: the machine itself. Anything on
# the machine (its 127.0.0.1:8800 port forward, which podman may point at this
# network; WSL's localhost forwarding) reaches the Host from there, and none of
# it is a container.
OUTSIDE = ("10.89.77.5", "192.0.2.10", "fd00::5", "10.89.79.20", "10.89.78.1")
CAP = AV._mint_verify_cap("tradebot")


def req(ip, method, path, headers=None):
    c = TestClient(app, client=(ip, 43210), follow_redirects=False)
    return c.request(method, path, headers=headers or {}, json={} if method == "POST" else None)


EXEMPT = [
    ("GET", "/api/applications/telegram/source", {}),
    ("POST", "/api/applications/telegram/runtime-log", {}),
    ("POST", "/mcp", {"X-CSeries-Harness-Cap": "cap-from-wake"}),
    ("GET", "/system-apps/tradebot/index.html", {"X-CSeries-Verify-Cap": "any-value"}),
    ("GET", "/js/app-sdk.js", {"X-CSeries-Verify-Cap": CAP}),
]
REFUSED_INSIDE = [
    ("POST", "/mcp", {}),
    ("GET", "/system-apps/tradebot/index.html", {}),
    ("GET", "/js/app-sdk.js", {"X-CSeries-Verify-Cap": "not-a-live-cap"}),
    ("GET", "/api/applications/telegram/other", {}),
    ("GET", "/api/applications/a/b/source", {}),
    ("POST", "/api/brain/x", {"X-CSeries-Harness-Cap": "cap", "X-CSeries-Verify-Cap": CAP}),
]

os.environ["CSERIES_RUNTIME_NET"] = "10.89.78.0/24"
for method, path, h in EXEMPT:
    r = req(INSIDE, method, path, h)
    check(r.status_code == 200 and r.json().get("reached") == path,
          f"from the runtime network {method} {path} reaches its handler", f"{r.status_code} {r.text[:120]}")
    for ip in OUTSIDE:
        r = req(ip, method, path, h)
        check(r.status_code == 403, f"from {ip} {method} {path} is 403", f"{r.status_code}")
for method, path, h in REFUSED_INSIDE:
    r = req(INSIDE, method, path, h)
    check(r.status_code == 403, f"from the runtime network {method} {path} {sorted(h)} is still 403",
          f"{r.status_code} {r.text[:80]}")

r = req(INSIDE, "GET", "/system-apps/tradebot/index.html", {"Accept": "text/html", "X-CSeries-Verify-Cap": "x"})
check(r.status_code == 200, "a page request from the runtime network is not sent to the sign-in page",
      f"{r.status_code} {r.headers.get('location')}")
r = req("192.0.2.10", "GET", "/system-apps/tradebot/index.html", {"Accept": "text/html"})
check(r.status_code == 303, "from outside a page request without a session is still sent to sign in",
      f"{r.status_code}")
r = req("10.89.78.1", "GET", "/system-apps/tradebot/index.html", {"Accept": "text/html", "X-CSeries-Verify-Cap": "x"})
check(r.status_code == 303, "from the runtime network's gateway (the machine) a page request is sent to sign in",
      f"{r.status_code}")

# The proof page's own WebSocket: the cap rides a cookie (a browser cannot set
# a header on a handshake), opens only THAT app, and only from the network.
from server.middleware.localhost_only import websocket_refusal  # noqa: E402


def ws_scope(ip, path, cookie=None):
    headers = [(b"host", b"10.89.78.2:8800")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return {"type": "websocket", "path": path, "client": (ip, 5000), "headers": headers}


check(websocket_refusal(ws_scope(INSIDE, "/system-apps/tradebot/ws", f"a=b; cseries_verify_cap={CAP}")) is None,
      "the proof page's WebSocket to its own app opens from the runtime network with the cap cookie")
check(websocket_refusal(ws_scope(INSIDE, "/system-apps/other/ws", f"cseries_verify_cap={CAP}")) is not None,
      "the same cap opens no other app's WebSocket")
check(websocket_refusal(ws_scope("10.89.78.1", "/system-apps/tradebot/ws", f"cseries_verify_cap={CAP}")) is not None,
      "the verify cookie opens nothing from the runtime network's gateway (the machine)")
check(websocket_refusal(ws_scope("192.0.2.10", "/system-apps/tradebot/ws", f"cseries_verify_cap={CAP}")) is not None,
      "nor from outside the runtime network")
check(websocket_refusal(ws_scope(INSIDE, "/ws", f"cseries_verify_cap={CAP}")) is not None,
      "and the cookie opens nothing but /system-apps/")

os.environ.pop("CSERIES_RUNTIME_NET")
for method, path, h in EXEMPT:
    r = req(INSIDE, method, path, h)
    check(r.status_code == 403, f"without CSERIES_RUNTIME_NET, {method} {path} from that address is 403",
          f"{r.status_code}")
os.environ["CSERIES_RUNTIME_NET"] = "not a network"
r = req(INSIDE, "GET", "/api/applications/telegram/source")
check(r.status_code == 403, "a CSERIES_RUNTIME_NET that is not a network opens nothing", f"{r.status_code}")
os.environ.pop("CSERIES_RUNTIME_NET")
AV._forget_verify_cap("tradebot")

print()
if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    sys.exit(1)
print("all runtime-network gate checks passed")
