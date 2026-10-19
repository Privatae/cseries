#!/usr/bin/env python3
"""Every integration leaves the pod through the egress proxy, or not at all.

Entity pods run under a default-deny egress policy; the node's squid is the
only route to the internet, and aiohttp takes it only with trust_env=True.
Measured 2026-09-23 from one Host's pod: a direct HTTPS request to
gmail.googleapis.com never connected, the same request through HTTPS_PROXY
reached Google. Every session under server/integrations and server/reachout
had been a bare aiohttp.ClientSession(), so the Google connector's token
exchange, refresh, account lookup, revoke and sync could not leave the pod.

The fix is ONE helper (server/integrations/core/egress.session) that forces
trust_env. This ratchet keeps it the only way: a bare ClientSession anywhere
under those two packages fails the gate, with the file and line.

Run: APP_ROOT=<repo> python3 scripts/test_integrations_egress.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PACKAGES = ("server/integrations", "server/reachout")
ALLOWED = {os.path.normpath("server/integrations/core/egress.py")}
BARE = re.compile(r"\bClientSession\s*\(")


def offenders() -> list[str]:
    out = []
    for pkg in PACKAGES:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, pkg)):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.normpath(os.path.relpath(path, ROOT))
                if rel in ALLOWED:
                    continue
                with open(path, encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if BARE.search(line) and not line.lstrip().startswith("#"):
                            out.append(f"{rel}:{n}: {line.strip()}")
    return out


def test_no_integration_opens_its_own_session():
    bad = offenders()
    assert not bad, ("these open an aiohttp session of their own, which cannot leave an "
                     "entity pod — use server.integrations.core.egress.session():\n  "
                     + "\n  ".join(bad))


def test_the_helper_forces_the_proxy():
    import asyncio
    sys.path.insert(0, ROOT)
    from server.integrations.core import egress

    async def run():
        s = egress.session(trust_env=False)
        try:
            return s.trust_env
        finally:
            await s.close()
    assert asyncio.run(run()) is True, "egress.session must force trust_env=True"


if __name__ == "__main__":
    failed = 0
    for fn in (test_no_integration_opens_its_own_session, test_the_helper_forces_the_proxy):
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}\n       {e}")
    sys.exit(1 if failed else 0)
