"""The editor/browser lane bills a release at the vCPUs the pod RAN AT.

harness_backend._release_and_bill is the only kernel path that releases
the on-demand browser and editor harnesses. The operator's release
response carries `vcpus` (the per-run bump, else the ceiling rendered
into the Deployment template), and the bridge prices compute_s × vcpus. When
this function dropped the value, every such window was
billed at 1 vCPU regardless of the pod's ceiling; the browser harness
renders at 2, which would have left half its compute unbilled.

Run: pytest tests/test_harness_backend_billing.py
"""
from __future__ import annotations

import asyncio

import pytest

from server.applications import harness_backend as hb


class _Bridge:
    _connected = True

    def __init__(self, data):
        self._data = data
        self.calls = []

    async def request_full(self, op, params):
        self.calls.append((op, params))
        return {"ok": True, "data": self._data}


@pytest.fixture()
def charge_spy(monkeypatch):
    seen = {}

    async def fake_charge(*, session_id, harness, compute_s, vcpus=None,
                          run_id=None):
        seen.update(session_id=session_id, harness=harness,
                    compute_s=compute_s, vcpus=vcpus, run_id=run_id)
        return "ref", 123

    import server.routes.compute as compute
    monkeypatch.setattr(compute, "charge_harness_compute", fake_charge)
    return seen


def _release(monkeypatch, data):
    bridge = _Bridge(data)
    monkeypatch.setattr(hb, "_bridge", lambda: bridge)
    # the lane's own per-app state (harness_backend._state); a released
    # entry is what _release_and_bill tears down
    monkeypatch.setitem(hb._state, "browser",
                        {"harness": "browser", "url": "http://x", "ws": 0,
                         "release_task": None, "lease_task": None})
    asyncio.run(hb._release_and_bill("browser", "browser"))
    return bridge


def test_release_bills_at_the_vcpus_the_operator_reports(monkeypatch, charge_spy):
    _release(monkeypatch, {"ready_duration_s": 90.0, "vcpus": 2, "scaled_to": 0})
    assert charge_spy == {"session_id": "vscode:browser", "harness": "browser",
                          "compute_s": 90.0, "vcpus": 2, "run_id": None}


def test_release_with_no_vcpus_prices_at_the_bridge_default(monkeypatch, charge_spy):
    """None → the charge carries no vcpus and the bridge prices at 1 —
    the old behaviour, for an operator that reports
    nothing (an older operator, or a template with no CPU limit)."""
    _release(monkeypatch, {"ready_duration_s": 30.0, "vcpus": None})
    assert charge_spy["vcpus"] is None
    assert charge_spy["compute_s"] == 30.0


def test_nothing_metered_means_nothing_charged(monkeypatch, charge_spy):
    _release(monkeypatch, {"ready_duration_s": None, "vcpus": 2})
    assert charge_spy == {}


# ── the window key the operator hands back ─────────────────────────────
#
# A window the operator's sweep closed out can be billed by TWO parties: this
# release, and the operator's own recovery pass for windows no caller ever
# returns to (it has to exist, because the reason to return lives in
# harness_backend._state, which every restart of this process empties). The
# operator reports `window_id` — an id derived from the window itself — and
# both sides charge under it, so harness_compute_log's UNIQUE (entity_id,
# run_id) collapses the two into one row instead of billing the customer twice.


def test_a_closed_out_window_is_charged_under_the_operators_key(monkeypatch, charge_spy):
    _release(monkeypatch, {"ready_duration_s": 90.0, "vcpus": 2,
                           "window_id": "win-deadbeef"})
    assert charge_spy["run_id"] == "win-deadbeef"


def test_an_open_window_keeps_the_callers_own_key(monkeypatch, charge_spy):
    """No window_id = the sweep never closed this out, so nothing else can bill
    it and charge_harness_compute mints its own uuid exactly as before."""
    _release(monkeypatch, {"ready_duration_s": 90.0, "vcpus": 2})
    assert charge_spy["run_id"] is None


def test_the_app_runtime_lane_passes_the_key_too(monkeypatch, charge_spy):
    """The app-runtime lane is where the defect showed: a pod app's window
    delivered thousands of seconds with no harness_compute_log row at any
    timestamp."""
    bridge = _Bridge({"ready_duration_s": 4321.5,
                      "window_id": "win-abc123"})
    monkeypatch.setattr(hb, "_bridge", lambda: bridge)
    asyncio.run(hb._release_and_bill_app("widget-runner"))
    assert charge_spy["session_id"] == "app-rt:widget-runner"
    assert charge_spy["harness"] == hb._APP_RT_MARK
    assert charge_spy["run_id"] == "win-abc123"
    assert charge_spy["compute_s"] == pytest.approx(4321.5)


def test_an_app_window_is_billed_at_the_ceiling_it_ran_at(monkeypatch, charge_spy):
    """A process app may declare a CPU ceiling above 1
    (manifest resources.vcpus; Blender Engine renders at 4). The operator
    reports the ceiling of the window it closed; dropping it here billed a
    4-vCPU window at 1."""
    bridge = _Bridge({"ready_duration_s": 300.0, "vcpus": 4})
    monkeypatch.setattr(hb, "_bridge", lambda: bridge)
    asyncio.run(hb._release_and_bill_app("blender-engine"))
    assert charge_spy["vcpus"] == 4
    assert charge_spy["compute_s"] == 300.0


def test_an_app_window_with_no_vcpus_prices_at_one(monkeypatch, charge_spy):
    bridge = _Bridge({"ready_duration_s": 60.0})
    monkeypatch.setattr(hb, "_bridge", lambda: bridge)
    asyncio.run(hb._release_and_bill_app("telegram"))
    assert charge_spy["vcpus"] is None


def test_a_caller_key_the_bridge_would_refuse_falls_back_to_a_uuid(monkeypatch):
    """op_harness_compute_charge refuses a run_id over 128 chars or empty, and
    a refusal is a 400 that is NOT retried — i.e. an UNBILLED run. The guard
    must degrade to a fresh uuid rather than fail the charge."""
    import sys
    import types

    import server.routes.compute as compute
    sent = {}

    class _BC:
        _connected = True

        async def request_full(self, op, params):
            sent.update(params)
            return {"ok": True, "data": {"cost_microdollars": 1}}

    # charge_harness_compute imports get_bridge_client from server.main at CALL
    # time; server.main pulls the whole brain in and cannot be imported outside
    # the entity container, so stand in for it for the duration of this test.
    fake_main = types.ModuleType("server.main")
    fake_main.get_bridge_client = lambda: _BC()
    monkeypatch.setitem(sys.modules, "server.main", fake_main)
    for bad in ("", "x" * 129, 12345, None):
        sent.clear()
        asyncio.run(compute.charge_harness_compute(
            session_id="s", harness="h", compute_s=1.0, run_id=bad))
        assert sent["run_id"] != bad
        assert 0 < len(sent["run_id"]) <= 128

    sent.clear()
    asyncio.run(compute.charge_harness_compute(
        session_id="s", harness="h", compute_s=1.0, run_id="win-ok"))
    assert sent["run_id"] == "win-ok"


@pytest.mark.parametrize("raw, want", [
    (2, 2), ("2", 2), (2.0, 2), (1, 1),
    # the bridge refuses vcpus outside 1..8 with a 400 that is NOT retried,
    # which would make the release unbilled — clamp, never drop
    (9, 8), (100, 8),
    (0, None), (-1, None), (None, None), ("", None), ("two", None),
])
def test_vcpus_of_is_whole_clamped_and_never_raises(raw, want):
    assert hb._vcpus_of(raw) == want
