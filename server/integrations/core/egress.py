"""The one way an integration reaches its vendor: the pod's governed egress proxy.

Entity pods run under a default-deny EGRESS policy (set by the operator): the
node's squid on :3128 is the only route to the internet, and aiohttp takes it only when a session is built with
`trust_env=True`, which reads HTTPS_PROXY / NO_PROXY from the environment.

Measured from a live Host's pod on 2026-09-23: a direct HTTPS request to
gmail.googleapis.com never connected (curl `000` after 10 s), and the same
request through the pod's HTTPS_PROXY reached Google. Every integration in this
package talks to a vendor on the internet, and every one of them was opening a
bare `aiohttp.ClientSession()` — so the token exchange, the refresh, the account
lookup, revoke and every sync loop could not leave the pod. Nothing had ever
exercised them end to end: the Google client they need is not registered yet.

NO_PROXY keeps cluster-internal names direct, so this is the only kind of
session the package needs, and scripts/test_integrations_egress.py refuses a
bare one anywhere under server/integrations or server/reachout.
"""
from __future__ import annotations

from typing import Any

import aiohttp


def session(**kwargs: Any) -> aiohttp.ClientSession:
    """An aiohttp session that leaves the pod through the egress proxy.

    `trust_env` is forced, not defaulted: a caller that needs a direct session
    is calling something inside the cluster, and that is not an integration.
    """
    kwargs["trust_env"] = True
    return aiohttp.ClientSession(**kwargs)
