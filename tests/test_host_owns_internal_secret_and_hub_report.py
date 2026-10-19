"""Host sovereignty, Phase 2 stage A (cseries half).

1. INTERNAL_API_SECRET is made by the Host at process start when nothing
   supplied it (the operator stops supplying it past its version gate), and a
   supplied one is kept (an operator below the gate, the smoke test).
2. Every heartbeat.ping carries which WireGuard key the Hub runs, read from the
   reconcile tick's copy of GET /identity without any I/O, so the platform can
   tell which Hosts still serve the operator-made key without exec.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import launch  # noqa: E402
from server.bridge import bridge_client  # noqa: E402
from server.network import hub_identity  # noqa: E402


# ── 1. INTERNAL_API_SECRET ─────────────────────────────────────────────────

def test_secret_is_generated_when_absent(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    launch.ensure_internal_api_secret()
    got = os.environ["INTERNAL_API_SECRET"]
    assert re.fullmatch(r"[0-9a-f]{64}", got)  # 32 random bytes, hex


def test_empty_value_counts_as_absent(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "")
    launch.ensure_internal_api_secret()
    assert re.fullmatch(r"[0-9a-f]{64}", os.environ["INTERNAL_API_SECRET"])


def test_each_start_makes_a_new_one(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    launch.ensure_internal_api_secret()
    first = os.environ.pop("INTERNAL_API_SECRET")
    launch.ensure_internal_api_secret()
    assert os.environ["INTERNAL_API_SECRET"] != first


def test_supplied_value_is_kept(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "smoke-internal-secret")
    launch.ensure_internal_api_secret()
    assert os.environ["INTERNAL_API_SECRET"] == "smoke-internal-secret"


def test_main_sets_it_before_anything_binds(monkeypatch):
    """main() must set the secret before it binds the listener, and so before
    the server and the app it imports exist at all."""
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    seen = {}

    class _Stop(Exception):
        pass

    def _listener(host, port):
        seen["secret"] = os.environ.get("INTERNAL_API_SECRET")
        raise _Stop

    monkeypatch.setattr(launch, "_listener", _listener)
    with pytest.raises(_Stop):
        launch.main()
    assert seen["secret"] and re.fullmatch(r"[0-9a-f]{64}", seen["secret"])


def test_every_reader_reads_the_process_env_per_call():
    """Each in-pod reader takes the value from os.environ inside a function, so
    the value main() sets is the one they all see. A module-level read (cached
    at import) or a file the operator wrote would break that."""
    readers = {
        "server/applications/middleware.py": 2,
        # one since 2026-10-13: ship_project's loopback went with the
        # /mcp factory scope
        "server/routes/mcp.py": 1,
        "server/routes/synaptive.py": 1,
    }
    for rel, n in readers.items():
        lines = open(os.path.join(ROOT, rel)).read().splitlines()
        hits = [ln for ln in lines if 'environ.get("INTERNAL_API_SECRET"' in ln]
        assert len(hits) == n, (rel, len(hits))
        assert all(ln[:1].isspace() for ln in hits), (rel, hits)


# ── 2. hub_key on the heartbeat ────────────────────────────────────────────

def _set(status, age=0.0):
    hub_identity._status = (time.monotonic() - age, status)


def test_report_own_when_legacy_retired():
    _set({"public_key": "H", "peers": [{"public_key": "m1", "on": "host"}]})
    assert hub_identity.report_state() == {
        "state": "own", "machines": {"host": 1, "legacy": 0, "none": 0}}


def test_report_own_plus_legacy_while_machines_move():
    _set({"public_key": "H", "legacy_public_key": "L",
          "peers": [{"public_key": "m1", "on": "host"},
                    {"public_key": "m2", "on": "legacy"},
                    {"public_key": "m3"}]})
    r = hub_identity.report_state()
    assert r["state"] == "own+legacy"
    assert r["machines"] == {"host": 1, "legacy": 1, "none": 1}


def test_report_legacy_for_unarmed_or_old_hub():
    _set({"public_key": "", "legacy_public_key": "L", "peers": []})
    assert hub_identity.report_state()["state"] == "legacy"
    _set(hub_identity.PRE_IDENTITY)
    assert hub_identity.report_state()["state"] == "legacy"


def test_report_none_and_unknown():
    _set({"peers": []})
    assert hub_identity.report_state()["state"] == "none"
    _set(None)
    assert hub_identity.report_state()["state"] == "unknown"
    # A stale copy says nothing about now: "own" read long ago is not "own".
    _set({"public_key": "H"}, age=hub_identity.STATUS_MAX_AGE_S + 1)
    assert hub_identity.report_state()["state"] == "unknown"
    hub_identity._status = (0.0, None)
    assert hub_identity.report_state()["state"] == "unknown"


def test_report_never_carries_a_key():
    _set({"public_key": "HOSTPUB", "legacy_public_key": "LEGPUB",
          "peers": [{"public_key": "MPUB", "on": "legacy"}]})
    flat = repr(hub_identity.report_state())
    assert "HOSTPUB" not in flat and "LEGPUB" not in flat and "MPUB" not in flat


def test_heartbeat_ping_carries_the_report(monkeypatch):
    """The liveness ping sends hub_key, and does so without touching the Hub."""
    _set({"public_key": "H", "peers": []})
    sent = []

    class _Client(bridge_client.BridgeClient):
        def __init__(self):
            self._running = True
            self._connected = True

        async def request(self, op, params=None, **kw):
            sent.append((op, params))
            self._running = False  # one beat, then the loop ends
            return {"ok": True}

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(bridge_client.asyncio, "sleep", _no_sleep)
    asyncio.run(_Client()._heartbeat_loop())
    assert sent == [("heartbeat.ping", {"hub_key": {
        "state": "own", "machines": {"host": 0, "legacy": 0, "none": 0}}})]


def test_heartbeat_params_never_raise(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(hub_identity, "report_state", boom)
    assert bridge_client._heartbeat_params() == {}
