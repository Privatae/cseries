#!/usr/bin/env python3
"""GET /mcp's event stream is an outside agent's alone, and what it keeps in
memory stays bounded.

  * a caller with any other credential (a bastion's device-bound bearer, a
    pasted bearer) asking GET /mcp for an event stream is told there is none
    (405), as before the agent stream existed: its token is not looked up,
    its rate budget is not spent, and a device-bound token sent without its
    fingerprint header is never answered 401 (which a standard MCP client
    takes as "sign in again");
  * an agent token without a session id: 405 too, with no lookup;
  * one ask sent in many sessions keeps a bounded number of them
    (agent_access.sessions.ASK_SESSIONS_MAX), the newest, so a client that
    calls the same tool from fresh session ids does not grow it without end.

In-process; no database, no network.
"""
import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from server import brain_service  # noqa: E402
from server.agent_access import sessions  # noqa: E402
from server.routes import mcp as M  # noqa: E402

failed = 0


def check(ok, what, detail=None):
    global failed
    print(("ok   " if ok else "FAIL ") + what + ("" if ok or detail is None else f"  -> {detail!r}"))
    if not ok:
        failed += 1


LOOKUPS = []


async def fake_validate(pool, bearer):
    LOOKUPS.append(bearer)
    return {"id": 7, "entity_id": 1, "scopes": [], "hardware_fingerprint": "fp-of-the-bastion"}


async def main() -> int:
    M._validate_bearer = fake_validate
    brain_service.get_pool = lambda: object()
    app = FastAPI()
    app.include_router(M.router)
    sse = {"Accept": "text/event-stream"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://h") as c:
        before = dict(M._rate_buckets)
        for sid_hdr in ({}, {"Mcp-Session-Id": "a" * 32}):
            r = await c.get("/mcp", headers={**sse, **sid_hdr, "Authorization": "Bearer bastion-token"})
            check(r.status_code == 405,
                  f"a bastion's bearer asking for a stream {'with' if sid_hdr else 'without'} a session id: "
                  "405, as before", (r.status_code, r.text[:200]))
        check(not LOOKUPS, "and its token is not looked up for it", LOOKUPS)
        check(M._rate_buckets == before, "nor its rate budget spent", M._rate_buckets)
        r = await c.get("/mcp", headers={**sse, "Authorization": "Bearer agt_whatever"})
        check(r.status_code == 405, "an agent token with no session id: 405", r.status_code)

    sessions._asks.clear()
    for i in range(sessions.ASK_SESSIONS_MAX * 50):
        sessions.asked("one-ask", f"session-{i:06d}-padding")
    kept = sessions._asks.get("one-ask") or {}
    check(len(kept) <= sessions.ASK_SESSIONS_MAX,
          "one ask sent in many sessions keeps a bounded number of them", len(kept))
    check(f"session-{sessions.ASK_SESSIONS_MAX * 50 - 1:06d}-padding" in kept,
          "the newest session is among them", list(kept)[-3:])
    sessions._asks.clear()

    print("FAILED" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
