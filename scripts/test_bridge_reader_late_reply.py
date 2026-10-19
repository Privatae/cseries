#!/usr/bin/env python3
"""The bridge reader survives a reply whose waiter was cancelled in the same
loop turn.

asyncio handles incoming data before due timers in one loop iteration, so a
reply can be read just as its request's timeout cancels the wait. Setting a
result on that cancelled future raises; the reader loop used to end on it
and mark the connection down, dropping every request in flight. The reply is
now dropped and the reader keeps going.

Runs the real BridgeClient.request_full and _reader_loop over an in-memory
reader and writer. Container-free."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), ".."))
from server.bridge.bridge_client import BridgeClient  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok    " if ok else "FAIL  ") + name + ("" if ok else f" — {detail}"))
    if not ok:
        fails += 1


class _Writer:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    async def drain(self):
        return None


def _client():
    bc = BridgeClient()
    bc._reader = asyncio.StreamReader()
    bc._writer = _Writer()
    bc._connected = True
    bc._reader_task = asyncio.create_task(bc._reader_loop())
    return bc


def _reply(mid, **data):
    return json.dumps({"id": mid, "ok": True, "data": data}).encode() + b"\n"


async def main():
    for order in ("reply then cancel", "cancel then reply"):
        bc = _client()
        task = asyncio.create_task(bc.request_full("support.operator_verdict", {"x": 1}))
        await asyncio.sleep(0.01)
        mid = next(iter(bc._pending))
        if order == "reply then cancel":
            bc._reader.feed_data(_reply(mid))     # read in the same turn the wait is cut
            task.cancel()
        else:
            task.cancel()
            bc._reader.feed_data(_reply(mid))
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.01)
        check(f"{order}: the reader is still running and the connection still up",
              not bc._reader_task.done() and bc._connected,
              f"reader done={bc._reader_task.done()} connected={bc._connected}")
        # The next request on the same connection gets its own reply.
        nxt = asyncio.create_task(bc.request_full("support.operator_verdict", {"x": 2}))
        await asyncio.sleep(0.01)
        mid2 = next(iter(bc._pending))
        bc._reader.feed_data(_reply(mid2, n=2))
        got = await asyncio.wait_for(nxt, timeout=2)
        check(f"{order}: the next request on that connection is answered",
              got.get("ok") is True and (got.get("data") or {}).get("n") == 2, str(got))
        bc._reader_task.cancel()


asyncio.run(main())
print("PASS" if not fails else f"FAILED ({fails} failing)")
sys.exit(1 if fails else 0)
