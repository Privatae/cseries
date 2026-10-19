#!/usr/bin/env python3
"""Gate: an app's config value is bounded where it protects the Host — before
the body is read — and nowhere near a legitimate app's state.

WHY THIS EXISTS. PUT /api/applications/{id}/config/{key} read the WHOLE body
with request.json() and only then refused anything over 64 KiB
(storage.MAX_CONFIG_VALUE_BYTES): it refused an app's settings blob or long
list, and protected nothing, because the memory was already spent.

What this pins (container-free: a stub request, a stub pool):
  1. a 200 KB value is stored;
  2. a body past the route's bound is refused AS IT STREAMS (most of it is
     never read), and a declared Content-Length past it is refused unread,
     each with a 413 that says where data that size belongs;
  3. the storage layer's own limit is Postgres's limit on one jsonb value;
  4. a body that is not JSON is still a 400.

Measured red against origin/main 300bb1b9 (APP_ROOT=<pristine export>).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO / "brain"))
sys.path.insert(0, str(REPO))

FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


from fastapi import HTTPException                       # noqa: E402
from server.routes import applications as A             # noqa: E402
from server.applications import storage as ST            # noqa: E402
from server import brain_service                         # noqa: E402

stored: list = []


class _Conn:
    async def execute(self, sql, *args):
        stored.append(args)
        return "INSERT 0 1"


class _Acq:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *a):
        return False


class _Pool:
    def acquire(self):
        return _Acq()


brain_service.get_pool = lambda: _Pool()
A._require_self = lambda request, app_id: None


class _Req:
    """What the route reads: headers and the body as it streams."""

    def __init__(self, body: bytes, *, declare: bool = True, chunk: int = 1 << 16):
        self._body, self._chunk = body, chunk
        self.read = 0
        self.headers = {"content-length": str(len(body))} if declare else {}

    async def stream(self):
        for i in range(0, len(self._body), self._chunk):
            piece = self._body[i:i + self._chunk]
            self.read += len(piece)
            yield piece

    async def body(self):
        self.read = len(self._body)
        return self._body

    async def json(self):
        return json.loads(await self.body())


def put(req):
    try:
        return asyncio.run(A.put_app_config("notes", "settings", req))
    except HTTPException as e:
        return {"status": e.status_code, "detail": str(e.detail)}


print("1. a legitimate app state is stored")
value = {"items": [{"id": i, "title": f"note {i}", "tags": ["a", "b"]} for i in range(4000)]}
body = json.dumps(value).encode()
res = put(_Req(body))
check(f"1: a {len(body) // 1024} KB value is stored", res.get("ok") is True, str(res)[:200])
check("1: the stored value is the whole value",
      bool(stored) and json.loads(stored[-1][2]) == value)

print("2. the bound is before the body is read")
cap = getattr(A, "CONFIG_BODY_MAX", None)
check("2: the route declares its body bound", isinstance(cap, int) and cap >= 1 << 20, str(cap))
over = b'{"x": "' + b"y" * ((cap or (16 << 20)) + (4 << 20)) + b'"}'
req = _Req(over, declare=False)
res = put(req)
check("2: an over-bound chunked body is a 413", res.get("status") == 413, str(res)[:200])
check("2: refused as it streams — most of it is never read",
      req.read <= (cap or 0) + (1 << 16), f"read {req.read} of {len(over)}")
check("2: the refusal says where data that size belongs",
      "app's files" in str(res.get("detail")), str(res)[:200])
req = _Req(over, declare=True)
res = put(req)
check("2: a declared over-bound Content-Length is refused unread",
      res.get("status") == 413 and req.read == 0, f"{res} read={req.read}")

print("3. the storage layer's limit is the database's")
check("3: one value may be as large as one jsonb value",
      ST.MAX_CONFIG_VALUE_BYTES == (1 << 28) - 1, str(ST.MAX_CONFIG_VALUE_BYTES))

print("4. a body that is not JSON is still a 400")
res = put(_Req(b"{not json"))
check("4: 400", res.get("status") == 400, str(res))

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("PASS: app config is bounded before it is read, at the Host's real limits")
