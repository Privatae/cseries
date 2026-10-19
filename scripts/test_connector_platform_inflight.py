#!/usr/bin/env python3
"""Gate: one connector app may have at most PLATFORM_INFLIGHT_MAX platform
calls in flight (server/routes/connector.py).

An upload can hold up to _PLATFORM_BODY_MAX of the kernel's memory, so a
connector caught in a retry loop must be refused BEFORE its body is read:

  L. the call past the limit is a 429 and its body is never read;
  F. a finished call frees its place, including one that failed;
  A. another connector app is not held back by the first one's calls;
  U. a body is measured in bytes, so a full non-Latin message fits;
  S. a lone surrogate escape is measured too (never a 500), and a large body
     of them is still a 413;
  C. a request whose client went away (mid-body, or cancelled mid-call)
     frees its place.

Drives the real handler with a fake request and the platform call replaced;
no DB, no network.
"""
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from fastapi import HTTPException  # noqa: E402

from server.network import nac_connector as nc  # noqa: E402
from server.routes import connector as route  # noqa: E402

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


class Req:
    """A request whose body read is recorded."""
    path = "getMe"

    def __init__(self):
        self.headers = {}
        self.read = False

    async def stream(self):
        self.read = True
        yield ('{"method": "GET", "path": "%s"}' % self.path).encode()


class Boom(Req):
    path = "boom"


def caller(app_id):
    return nc.ConnectorCaller(app_id=app_id, stages=frozenset({nc.STAGE_ACTIVE}))


async def main():
    gate = asyncio.Event()
    started = []

    async def slow_call(pool, who, body):
        started.append(who.app_id)
        await gate.wait()
        if body.get("path") == "boom":
            raise nc.ConnectorError(502, "upstream failed")
        return {"ok": True}

    real = nc.platform_call
    nc.platform_call = slow_call
    try:
        limit = getattr(route, "PLATFORM_INFLIGHT_MAX", 8)
        held = [asyncio.create_task(route.connector_platform(Req(), caller("tg")))
                for _ in range(limit)]
        await asyncio.sleep(0.05)
        check("L the limit's worth of calls are all in flight", started.count("tg") == limit, started)

        extra = Req()
        extra_task = asyncio.create_task(route.connector_platform(extra, caller("tg")))
        await asyncio.sleep(0.05)
        refused = None
        if extra_task.done():
            try:
                extra_task.result()
            except HTTPException as e:
                refused = e.status_code
        check("L the call past the limit is a 429", refused == 429, refused)
        check("L ... and its body was never read", extra.read is False)

        other = asyncio.create_task(route.connector_platform(Req(), caller("slack-app")))
        await asyncio.sleep(0.05)
        check("A another connector app is not held back", "slack-app" in started, started)

        gate.set()
        results = await asyncio.gather(*held, other, return_exceptions=True)
        check("F every held call finished", all(r == {"ok": True} for r in results), results)
        if not extra_task.done():
            await extra_task
        check("F nothing is left counted once they finish",
              getattr(route, "_platform_inflight", None) == {}, getattr(route, "_platform_inflight", None))

        gate.clear()
        failing = asyncio.create_task(route.connector_platform(Boom(), caller("tg")))
        await asyncio.sleep(0.05)
        gate.set()
        try:
            await failing
            status = None
        except HTTPException as e:
            status = e.status_code
        check("F a failed call is the platform's error", status == 502, status)
        check("F ... and frees its place", getattr(route, "_platform_inflight", None) == {},
              getattr(route, "_platform_inflight", None))

        # A full Telegram message in Cyrillic (4096 characters, about 8 KB as
        # UTF-8) is within the 16 KiB bound: measured as bytes, not as
        # json.dumps's escapes (six characters for each one).
        class Cyrillic(Req):
            async def stream(self):
                self.read = True
                yield json.dumps({"method": "POST", "path": "/sendMessage",
                                  "json": {"chat_id": 42, "text": "Ж" * 4096}},
                                 ensure_ascii=False).encode()
        gate.set()
        try:
            out = await route.connector_platform(Cyrillic(), caller("tg"))
            status = 200 if out == {"ok": True} else out
        except HTTPException as e:
            status = e.status_code
        check("U a full 4096-character Cyrillic message is not refused as too large", status == 200, status)

        # A lone surrogate escape is valid JSON that Python reads; measuring
        # it must not raise (a 500 with a traceback), and a large one is
        # still a 413.
        class Surrogate(Req):
            text = "\\ud800"

            async def stream(self):
                self.read = True
                yield ('{"method": "POST", "path": "/sendMessage", "json": {"chat_id": 42, '
                       '"text": "%s"}}' % self.text).encode()

        class BigSurrogate(Surrogate):
            text = "\\ud800" * 6000
        for req, want, label in ((Surrogate(), 200, "a lone surrogate is measured, not a 500"),
                                 (BigSurrogate(), 413, "…and 6000 of them are still too large")):
            try:
                out = await route.connector_platform(req, caller("tg"))
                status = 200 if out == {"ok": True} else out
            except HTTPException as e:
                status = e.status_code
            except Exception as e:  # noqa: BLE001 — what the route would turn into a 500
                status = f"raised {type(e).__name__}"
            check(f"S {label}", status == want, status)
        check("S ... and frees its place", getattr(route, "_platform_inflight", None) == {},
              getattr(route, "_platform_inflight", None))

        # C. A request whose client went away (Starlette raises
        # ClientDisconnect from the body read; a cancelled task raises
        # CancelledError in the platform call) still frees its place.
        from starlette.requests import ClientDisconnect

        class Gone(Req):
            async def stream(self):
                self.read = True
                yield b'{"method": "GET",'
                raise ClientDisconnect()
        try:
            await route.connector_platform(Gone(), caller("tg"))
            status = None
        except ClientDisconnect:
            status = "disconnect"
        check("C a client that disconnects mid-body is not served", status == "disconnect", status)
        check("C ... and frees its place", getattr(route, "_platform_inflight", None) == {},
              getattr(route, "_platform_inflight", None))
        gate.clear()
        started.clear()
        stuck = [asyncio.create_task(route.connector_platform(Req(), caller("tg")))
                 for _ in range(limit)]
        await asyncio.sleep(0.05)
        check("C the limit's worth are in flight", started.count("tg") == limit, started)
        for t in stuck:
            t.cancel()
        res = await asyncio.gather(*stuck, return_exceptions=True)
        check("C cancelled mid-call (the client went away)",
              all(isinstance(r, asyncio.CancelledError) for r in res), res)
        check("C ... every place is freed", getattr(route, "_platform_inflight", None) == {},
              getattr(route, "_platform_inflight", None))
        gate.set()
        out = await route.connector_platform(Req(), caller("tg"))
        check("C ... so the next call is served, not a 429", out == {"ok": True}, out)
    finally:
        nc.platform_call = real


asyncio.run(main())
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
