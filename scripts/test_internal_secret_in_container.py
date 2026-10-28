#!/usr/bin/env python3
"""Gate: INTERNAL_API_SECRET is compared only for a caller inside the container.

The /mcp handler had compared the secret for any caller that reached it (a
container on the runtime's network, anything the mTLS front door carried), so
a caller outside could test guesses against it, and the right value made it
the internal caller. The same compare stood in three more places that read
the X-CSeries-Internal-Auth header:

  * applications/middleware._admit (requires_permission / requires_any_
    permission): the secret admits POST /api/applications/build;
  * ApplicationTokenMiddleware in strict mode: the secret is not counted or
    refused as a request with no app token;
  * routes/synaptive.create_token: the secret widens the scopes a token gets.

Each now asks middleware.localhost_only.in_container first: a loopback peer
whose request the mTLS front door did not carry (FrontDoorProof leaves
X-Client-Cert-CN only on what it forwarded). Driven here through the real
dependency, the real middleware and the real route function, from a container
on the runtime's network, a door's proxy, the bridge, the front door's loopback
hop and the container's own loopback. The /mcp handler's half is
scripts/test_localhost_gate_runtime_net.py.

Run: APP_ROOT=<repo> python3 scripts/test_internal_secret_in_container.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

SECRET = "internal-secret-for-this-gate-9f8e7d6c5b4a"
os.environ["INTERNAL_API_SECRET"] = SECRET

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


OUTSIDE = (("10.89.78.20", {}, "a container on the runtime's network"),
           ("fd00:10:89::20", {}, "a peer over IPv6"),
           ("10.89.77.1", {}, "a door's proxy"),
           ("172.17.0.1", {}, "the bridge"),
           ("127.0.0.1", {"X-Client-Cert-CN": "operator"}, "the mTLS front door's loopback hop"))
INSIDE = (("127.0.0.1", "loopback"), ("::1", "IPv6 loopback"), ("::ffff:127.0.0.1", "mapped loopback"))


def _admit_tests() -> None:
    from fastapi import Depends, FastAPI
    from starlette.testclient import TestClient
    from server.applications.middleware import requires_permission

    app = FastAPI()

    @app.post("/api/applications/build", dependencies=[Depends(requires_permission("write:apps"))])
    async def build():
        return {"built": True}

    def post(ip, headers):
        return TestClient(app, client=(ip, 43210)).post("/api/applications/build", headers=headers)

    for ip, extra, where in OUTSIDE:
        r = post(ip, {"X-CSeries-Internal-Auth": SECRET, **extra})
        check(f"requires_permission: the right secret from {where} does not admit the build route (401)",
              r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    for ip, where in INSIDE:
        r = post(ip, {"X-CSeries-Internal-Auth": SECRET})
        check(f"requires_permission: the right secret from this container's {where} still admits it",
              r.status_code == 200 and r.json().get("built") is True, f"{r.status_code} {r.text[:120]}")
    r = post("127.0.0.1", {"X-CSeries-Internal-Auth": "wrong"})
    check("requires_permission: a wrong secret from inside admits nothing (no app token: 401)",
          r.status_code == 401, f"{r.status_code}")


def _strict_middleware_tests() -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from server.applications import middleware as MW

    app = FastAPI()
    app.add_middleware(MW.ApplicationTokenMiddleware)
    app.state.kernel_secret = b"k" * 32

    @app.get("/api/gate-probe")
    async def probe():
        return {"reached": True}

    old = MW._MODE
    MW._MODE = "strict"
    try:
        for ip, extra, where in OUTSIDE:
            r = TestClient(app, client=(ip, 43210)).get(
                "/api/gate-probe", headers={"X-CSeries-Internal-Auth": SECRET, **extra})
            check(f"strict app-token middleware: the right secret from {where} is a request with no "
                  "token (401)", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
        r = TestClient(app, client=("127.0.0.1", 43210)).get(
            "/api/gate-probe", headers={"X-CSeries-Internal-Auth": SECRET})
        check("strict app-token middleware: from this container's loopback it still passes",
              r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    finally:
        MW._MODE = old


def _synaptive_tests() -> None:
    from fastapi import HTTPException
    from starlette.requests import Request
    from server import brain_service
    from server.routes import synaptive as S

    class _Pool:
        """Answers nothing: a call that gets past the secret's check reaches it
        and fails with something other than the check's 401."""
        async def fetchval(self, *a, **k):
            raise RuntimeError("reached the pool")
        execute = fetch = fetchrow = fetchval

    brain_service.get_pool = lambda: _Pool()

    def _request(ip: str, cn: str | None = None) -> Request:
        headers = [(b"x-client-cert-cn", cn.encode())] if cn else []
        return Request({"type": "http", "method": "POST", "path": "/api/synaptive/tokens",
                        "headers": headers, "client": (ip, 43210), "query_string": b""})

    async def _call(req: Request):
        try:
            await S.create_token(S.CreateToken(name="gate"), req, x_cseries_internal_auth=SECRET,
                                 x_cseries_operator_kind=None, x_cseries_hardware_fingerprint=None,
                                 x_cseries_anchor_id=None)
        except HTTPException as e:
            return e.status_code, str(e.detail)
        except Exception as e:  # noqa: BLE001
            return None, repr(e)
        return 200, ""

    for ip, extra, where in OUTSIDE:
        status, detail = asyncio.run(_call(_request(ip, extra.get("X-Client-Cert-CN"))))
        check(f"synaptive tokens: the right secret from {where} is refused before it is compared",
              status == 401 and "inside this container" in detail, f"{status} {detail[:120]}")
    status, detail = asyncio.run(_call(_request("127.0.0.1")))
    check("synaptive tokens: from this container's loopback the secret is compared and the call goes on",
          status != 401, f"{status} {detail[:120]}")


def main() -> int:
    _admit_tests()
    _strict_middleware_tests()
    _synaptive_tests()
    if FAILED:
        print(f"FAILED — {len(FAILED)}: " + "; ".join(FAILED))
        return 1
    print("the internal secret is compared only inside the container: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
