"""POST /api/network/mgmt/run and /api/network/harness/run are APP-SCOPED.

THE HOLE THIS CLOSES. Both routes used to carry no `requires_permission`
dependency; their docstrings said the user's signed grant was the authorization.
A grant is bound to the ENTITY, not to the app holding the window token
(server/network/nac_mgmt.py:757 run_mgmt_op takes `eid`, never an app id), so on
any machine where the host already held a grant, ANY installed app with ANY
valid token could dispatch an op — an observe op, a `contain`, a harness run.
Every other network mutation in that file already required `write:network`
(server/routes/network.py:1209 session_close, :1220 session_ticket).

WHAT IS REAL HERE. The REAL router and the REAL `requires_permission` dependency
(server/applications/middleware.py:208-263) run. That dependency reads
`request.state.app_token`, which is exactly what the app-token middleware puts
there for the browser, so the scope check exercised below is the production one.
A caller with no token at all lands on the same branch a token-less browser call
lands on (middleware.py:247-252 -> 401), in EVERY APP_TOKEN_MODE — the mode flag
governs the blanket middleware, not this dependency (middleware.py:17-21).

Only the pod-external edges are stood in for: the DB pool, and the service
functions the route dispatches to. The scoped call is asserted to REACH the
service, which is the half a 401/403 test cannot prove on its own.

MUTATION (run before shipping):
  - Drop `dependencies=[Depends(requires_permission("write:network"))]` from
    @router.post("/mgmt/run") -> test_mgmt_run_wrong_scope_is_403 and
    test_mgmt_run_without_a_token_is_401 both fail (the call reaches the
    service and returns 200).
  - Same on @router.post("/harness/run") -> the two harness tests fail the
    same way.

Run: pytest tests/test_network_run_scope.py
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

import server.brain_service as BS  # noqa: E402
import server.routes.network as N  # noqa: E402
from server.applications.tokens import AppToken  # noqa: E402
from server.network import nac_harness, nac_mgmt  # noqa: E402

MGMT_URL = "/api/network/mgmt/run"
HARNESS_URL = "/api/network/harness/run"

MGMT_BODY = {"server": "box", "op": "uptime", "args": []}
HARNESS_BODY = {"server": "box", "harness_id": "claude", "prompt": "hi", "mode": "echo"}


class _Pool:
    """Enough of an asyncpg pool that get_pool() is not None. Nothing here is
    reached: the service functions that would query are stood in for."""

    async def execute(self, *a):
        return None

    async def fetch(self, *a):
        return []

    async def fetchrow(self, *a):
        return None

    async def fetchval(self, *a):
        return None


def _client(permissions: list[str] | None) -> TestClient:
    """The network router behind a token with exactly `permissions`.
    `permissions=None` means NO app token at all — the token-less caller."""
    app = FastAPI()

    if permissions is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.app_token = AppToken(
                app_id="nosy", permissions=list(permissions), window_id="w1", iat=0)
            return await call_next(request)

    app.include_router(N.router)
    return TestClient(app)


@pytest.fixture()
def reached(monkeypatch):
    """Records every dispatch that got past the scope gate into the service."""
    calls: list[tuple] = []

    async def _mgmt(pool, eid, server, op, args, **kw):  # (+ origin since 0.33.0)
        calls.append(("mgmt", server, op, list(args or [])))
        return {"server": server, "op": op, "exit": 0, "stdout": "up 1 day", "stderr": ""}

    async def _harness(pool, eid, server, harness_id, **kw):
        calls.append(("harness", server, harness_id, kw.get("mode")))
        return {"server": server, "harness_id": harness_id, "exit": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", _mgmt)
    monkeypatch.setattr(nac_harness, "run_harness_op", _harness)
    monkeypatch.setattr(BS, "get_pool", lambda: _Pool())
    monkeypatch.setattr(BS, "_entity_id", 3, raising=False)
    return calls


# ── no token at all: 401, and nothing is dispatched ──────────────────────────

def test_mgmt_run_without_a_token_is_401(reached):
    r = _client(None).post(MGMT_URL, json=MGMT_BODY)
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "application token required"
    assert reached == [], f"a token-less call reached the machine: {reached}"


def test_harness_run_without_a_token_is_401(reached):
    r = _client(None).post(HARNESS_URL, json=HARNESS_BODY)
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "application token required"
    assert reached == []


# ── a valid token that did not declare write:network: 403 ────────────────────

_EXPECTED_403 = "app 'nosy' did not declare permission 'write:network'"


def test_mgmt_run_wrong_scope_is_403(reached):
    # read:network is NOT enough: reading the network is not operating a machine.
    r = _client(["read:network", "read:topology"]).post(MGMT_URL, json=MGMT_BODY)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == _EXPECTED_403
    assert reached == [], f"an under-scoped app reached the machine: {reached}"


def test_harness_run_wrong_scope_is_403(reached):
    r = _client(["read:network", "read:topology"]).post(HARNESS_URL, json=HARNESS_BODY)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == _EXPECTED_403
    assert reached == []


def test_a_token_with_no_scopes_at_all_is_403(reached):
    assert _client([]).post(MGMT_URL, json=MGMT_BODY).status_code == 403
    assert _client([]).post(HARNESS_URL, json=HARNESS_BODY).status_code == 403
    assert reached == []


# ── the scope holder gets through to the service ─────────────────────────────

def test_mgmt_run_with_write_network_reaches_the_service(reached):
    r = _client(["write:network"]).post(MGMT_URL, json=MGMT_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["stdout"] == "up 1 day"
    assert reached == [("mgmt", "box", "uptime", [])], reached


def test_harness_run_with_write_network_reaches_the_service(reached):
    r = _client(["write:network"]).post(HARNESS_URL, json=HARNESS_BODY)
    assert r.status_code == 200, r.text
    assert reached == [("harness", "box", "claude", "echo")], reached


# ── the scope is declared where the contract reader can find it ──────────────

def test_both_routes_advertise_write_network_to_the_contract_reader():
    """server/applications/api_contract.py recovers a route's required scope from
    the dependency's `_app_scope` tag (middleware.py:259-262) rather than parsing
    source, so a scope that is not tagged is invisible to the app-facing contract."""
    scopes = {}
    for route in N.router.routes:
        path = getattr(route, "path", "")
        if path not in (MGMT_URL, HARNESS_URL):
            continue
        scopes[path] = {
            getattr(d.dependency, "_app_scope", None)
            for d in getattr(route, "dependencies", [])
        }
    assert scopes == {MGMT_URL: {"write:network"},
                      HARNESS_URL: {"write:network"}}, scopes
