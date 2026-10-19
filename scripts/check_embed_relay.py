#!/usr/bin/env python3
"""Embed reverse-relay transport invariant: EmbedRelay dispatch is keyed by bastion token_id,
and a token_id with zero connected workers returns None (no live embedder -> no vector).

Exercises the REAL EmbedWorker.serve()/EmbedRelay.dispatch() machinery against an in-process
fake bastion worker whose _run returns a fixed 2560-d vector (no live pod, no WS). Mirrors
scripts/check_nac_mgmt.py. Exit 0 = invariant holds.
"""
import asyncio
import sys

sys.path.insert(0, "/app")
from server.network.embed_relay import EmbedWorker, EmbedRelay  # noqa: E402

fail = 0


def check(name, cond):
    global fail
    print(("OK   " if cond else "FAIL ") + name)
    if not cond:
        fail = 1


class _NeverWS:
    """A stand-in WS whose receive() never returns — an idle worker parks on it until a job
    is assigned, exactly like a live bastion socket between jobs."""
    async def receive(self):
        await asyncio.Event().wait()


class FakeEmbedWorker(EmbedWorker):
    """A fake bastion worker: instead of framing over a WS, _run sets a fixed vector."""
    def __init__(self, bastion_token_id: int, vector):
        super().__init__(_NeverWS(), bastion_token_id)
        self._vector = vector

    async def _run(self, model, text, result):
        if not result.done():
            result.set_result(list(self._vector))


async def main():
    VEC = [0.001 * i for i in range(2560)]
    relay = EmbedRelay()

    # A worker for token_id=7 serving a 2560-d vector.
    w = FakeEmbedWorker(bastion_token_id=7, vector=VEC)
    serve_task = asyncio.ensure_future(w.serve(relay))
    await asyncio.sleep(0)  # let serve() register the worker idle

    check("relay reports one worker for token 7", relay.worker_count(7) == 1)

    vec = await relay.dispatch(7, "qwen3-embedding:4b", "hello", timeout=5.0)
    check("dispatch(token 7) returns a vector", vec is not None)
    check("dispatch(token 7) returns the 2560-d vector", vec is not None and len(vec) == 2560)
    check("dispatch(token 7) vector matches the fake worker's",
          vec == VEC)

    # A token_id with zero connected workers -> None (no live embedder).
    check("no worker for token 999", relay.worker_count(999) == 0)
    none_vec = await relay.dispatch(999, "qwen3-embedding:4b", "hello", timeout=0.2)
    check("dispatch(token 999, no worker) returns None", none_vec is None)

    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


asyncio.run(main())
print("embed_relay transport gate: " + ("PASS" if not fail else "FAIL"))
sys.exit(fail)
