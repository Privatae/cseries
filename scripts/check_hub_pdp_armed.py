#!/usr/bin/env python3
"""Invariant: hub-pdp-armed.

The Hub sidecar is this host's network PDP. It starts UNARMED — it admits grants
and policy without verifying the host's signature — because its verify key is
derived in-pod from kernel_secrets on first boot and so cannot be handed to it as
env at render time. The host arms it over the loopback control API instead
(hub_peers.arm_hub_pdp, every reconcile tick).

Measured before the fix: HUB_NAC_VERIFY_KEY was unset on all 8 live
hub sidecars, i.e. every production Host was running an unarmed PDP, and
`/status` reported `tampered: false` throughout — which reads as "no tampering"
but means "cannot detect tampering". This probe exists so that state is loud.

Two parts:

  1. PURE-LOGIC (always runs; no Hub required) — arm_hub_pdp against a stub
     control API:
       * 204            -> returns True
       * 409 (different key armed)  -> False, and it is an ERROR not a shrug
       * 404 (hub predates /verifykey) -> False
       * unreachable    -> False, never raises
       * no ENTITY_ID   -> _pod_entity_id() is None (never falls back to 1)

  2. LIVE HUB (skipped when the Hub is unreachable, e.g. genesis / no sidecar) —
     GET the real /status and assert grants_armed AND policy_armed are true.

Making an unarmed Hub a fatal startup error may only land once part 2 is
green fleet-wide. Until then an unarmed Hub is a WARN here, not a silent pass.

Non-zero exit on any failure. Run in-pod:
  docker exec <c> python3 /app/scripts/check_hub_pdp_armed.py
"""
import asyncio
import base64
import os
import sys

# /app in-pod; the repo root when run from a checkout (this file lives in scripts/).
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402

FAILED = []


def check(ok: bool, label: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        FAILED.append(label)


async def _stub(status: int, port_holder: dict):
    """A stub /verifykey returning `status`. Returns the runner to clean up."""
    async def handler(request):
        port_holder["body"] = await request.json()
        return web.Response(status=status)

    app = web.Application()
    app.router.add_post("/verifykey", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port_holder["port"] = runner.addresses[0][1]
    return runner


async def part1() -> None:
    print("\n1. arm_hub_pdp against a stub control API")
    from server.network import hub_peers

    # A pool stub whose verify key is a known 32 bytes. arm_hub_pdp reaches the
    # key through nac_stepup.entity_verify_pubkey, so patch that seam.
    pub = bytes(range(32))
    from server.network import nac_stepup

    async def fake_pubkey(pool, eid):
        return pub

    real = nac_stepup.entity_verify_pubkey
    nac_stepup.entity_verify_pubkey = fake_pubkey
    orig_control = hub_peers.HUB_CONTROL
    try:
        for status, expect, label in [
            (204, True, "204 -> armed"),
            (409, False, "409 (different key) -> not armed"),
            (404, False, "404 (hub predates /verifykey) -> not armed"),
            (500, False, "500 -> not armed"),
        ]:
            holder: dict = {}
            runner = await _stub(status, holder)
            hub_peers.HUB_CONTROL = f"http://127.0.0.1:{holder['port']}"
            try:
                got = await hub_peers.arm_hub_pdp(None, 122)
                check(got is expect, label)
                if status == 204:
                    sent = holder.get("body", {}).get("key")
                    check(sent == base64.b64encode(pub).decode(),
                          "sends the host verify key, base64 raw")
            finally:
                await runner.cleanup()

        # Unreachable Hub must be a quiet False, never an exception: this runs in
        # a forever-loop that must survive a Hub that is simply not there.
        hub_peers.HUB_CONTROL = "http://127.0.0.1:1"
        try:
            got = await hub_peers.arm_hub_pdp(None, 122)
            check(got is False, "unreachable hub -> False, no raise")
        except Exception as e:
            check(False, f"unreachable hub raised {e!r}")
    finally:
        nac_stepup.entity_verify_pubkey = real
        hub_peers.HUB_CONTROL = orig_control

    # ENTITY_ID is the only identity source. brain_service._entity_id is a
    # hardcoded 1 for the personal-computer case; falling back to it here would
    # arm a Host's Hub with entity 1's key.
    saved = os.environ.pop("ENTITY_ID", None)
    try:
        check(hub_peers._pod_entity_id() is None, "no ENTITY_ID -> None (no fallback to 1)")
        os.environ["ENTITY_ID"] = "122"
        check(hub_peers._pod_entity_id() == 122, "ENTITY_ID=9122 -> 122")
        os.environ["ENTITY_ID"] = "not-a-number"
        check(hub_peers._pod_entity_id() is None, "malformed ENTITY_ID -> None")
    finally:
        os.environ.pop("ENTITY_ID", None)
        if saved is not None:
            os.environ["ENTITY_ID"] = saved


async def part_loop() -> None:
    """The reconcile loop must actually CALL the arming path.

    part1 exercises arm_hub_pdp in isolation, so deleting or mis-gating the call
    site in reconcile_hub_peers_loop leaves it green — and in the smoke container
    there is no Hub, so part2 skips and would not catch it either. This closes
    that hole: run one real iteration of the loop and assert it armed.
    """
    print("\n1b. reconcile_hub_peers_loop calls the arming path")
    from server.network import hub_peers

    called: list[int] = []

    async def fake_arm(pool, eid):
        called.append(eid)
        return True

    async def fake_converge(pool):
        raise asyncio.CancelledError  # end the loop after one pass

    async def no_splay(*a, **k):
        return None

    import server.loop_splay as loop_splay
    real_arm, real_conv, real_splay = (
        hub_peers.arm_hub_pdp, hub_peers._converge_once, loop_splay.splay)
    saved = os.environ.get("ENTITY_ID")
    os.environ["ENTITY_ID"] = "122"
    hub_peers.arm_hub_pdp = fake_arm
    hub_peers._converge_once = fake_converge
    loop_splay.splay = no_splay
    try:
        try:
            await hub_peers.reconcile_hub_peers_loop(None)
        except asyncio.CancelledError:
            pass
        check(called == [122], f"loop armed for the pod's entity (called={called})")
    finally:
        hub_peers.arm_hub_pdp = real_arm
        hub_peers._converge_once = real_conv
        loop_splay.splay = real_splay
        os.environ.pop("ENTITY_ID", None)
        if saved is not None:
            os.environ["ENTITY_ID"] = saved


async def part2() -> None:
    print("\n2. the live Hub reports ARMED")
    import aiohttp
    from server.network.hub_peers import HUB_CONTROL

    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{HUB_CONTROL}/status") as r:
                st = await r.json()
    except Exception as e:
        print(f"  SKIP  no Hub on {HUB_CONTROL} ({e}) — genesis or no sidecar")
        return

    if "grants_armed" not in st:
        check(False, "hub /status has no grants_armed field — image predates PDP arming")
        return
    check(st.get("grants_armed") is True, "grants_armed is true")
    check(st.get("policy_armed") is True, "policy_armed is true")
    if st.get("tampered"):
        check(False, "PDP reports tampered — a signed rule failed verification")


async def main() -> int:
    print(__doc__.splitlines()[0])
    await part1()
    await part_loop()
    await part2()
    if FAILED:
        print(f"\nFAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
