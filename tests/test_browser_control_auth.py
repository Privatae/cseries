"""The browser control API's bearer-token gate.

The control API shipped with NO authentication. Loopback-only was the
containment — the sidecar shares the entity pod's network namespace, so only
the entity's own kernel could dial it. That holds exactly as long as the
container stays in that pod, and putting the browser on the on-demand
scale-to-zero lifecycle takes it out: a Deployment of its own is reachable
over the pod network. This API drives allow-listed sites with stored logins
and types vault-held passwords, so the token lands BEFORE the move.

The middleware is exec'd out of control/app.py and mounted on a real
FastAPI app, because playwright is not installed in the test env and
app.py imports it at module scope. The function body under test is the
real one — not a paraphrase — which is the only version worth testing for
an auth gate.

Run: pytest tests/test_browser_control_auth.py
"""
from __future__ import annotations

import ast
import pathlib
import secrets as _secrets

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi import FastAPI                      # noqa: E402
from fastapi.responses import JSONResponse       # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402

_SRC = (pathlib.Path(__file__).resolve().parents[1]
        / "sidecars" / "browser" / "control" / "app.py")

TOKEN = "s3cr3t-browser-token-abcdefghijklmnop"


def _extract(name: str):
    """Pull one async function out of control/app.py by name."""
    tree = ast.parse(_SRC.read_text())
    fns = [n for n in tree.body
           if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name]
    assert fns, (f"{name} is gone from control/app.py — the control API may be "
                 "unauthenticated again")
    return fns[0]


def _app(token: str | None):
    """A FastAPI app carrying the REAL middleware, with `token` configured."""
    node = _extract("_require_token")
    # Strip the @app.middleware decorator; we attach it ourselves below.
    node.decorator_list = []
    ns = {
        "secrets": _secrets,
        "JSONResponse": JSONResponse,
        "log": type("L", (), {"warning": staticmethod(lambda *a, **k: None),
                              "info": staticmethod(lambda *a, **k: None)})(),
        "_CONTROL_TOKEN": token or "",
        "_OPEN_PATHS": frozenset({"/health"}),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(_SRC), "exec"), ns)

    app = FastAPI()
    app.middleware("http")(ns["_require_token"])

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/docs")
    async def docs():
        return {"surface": "enumerated"}

    @app.post("/v1/sessions")
    async def create():
        return {"created": True}

    @app.post("/v1/sessions/x/act")
    async def act():
        return {"acted": True}

    return TestClient(app)


PROTECTED = ["/docs", "/v1/sessions", "/v1/sessions/x/act"]


def _call(client, path):
    return client.post(path) if path.startswith("/v1") else client.get(path)


# ── unset: the in-pod sidecar today, unchanged ──────────────────────


@pytest.mark.parametrize("path", PROTECTED + ["/health"])
def test_no_token_configured_leaves_every_route_open(path):
    """Shipping this must not break the loopback sidecar that is running
    right now. Empty token = previous behaviour, byte for byte."""
    assert _call(_app(None), path).status_code == 200


def test_empty_string_token_is_the_same_as_unset():
    assert _call(_app(""), "/v1/sessions").status_code == 200


# ── configured: everything but the probe is gated ───────────────────


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_refuse_a_request_with_no_token(path):
    r = _call(_app(TOKEN), path)
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_refuse_a_wrong_token(path):
    c = _app(TOKEN)
    r = (c.post(path, headers={"X-Browser-Token": "wrong"})
         if path.startswith("/v1") else
         c.get(path, headers={"X-Browser-Token": "wrong"}))
    assert r.status_code == 401


def test_docs_is_gated_because_it_enumerates_the_surface():
    assert _app(TOKEN).get("/docs").status_code == 401


def test_health_stays_open_so_the_kubelet_probe_needs_no_secret():
    """An exec probe that has to carry a credential is a worse thing to own
    than a liveness signal an on-path attacker could infer anyway."""
    r = _app(TOKEN).get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


# ── accepted forms ──────────────────────────────────────────────────


def test_x_browser_token_header_is_accepted():
    assert _app(TOKEN).post("/v1/sessions",
                            headers={"X-Browser-Token": TOKEN}).status_code == 200


def test_authorization_bearer_is_accepted():
    assert _app(TOKEN).post("/v1/sessions",
                            headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_bearer_scheme_is_case_insensitive(scheme):
    assert _app(TOKEN).post("/v1/sessions",
                            headers={"Authorization": f"{scheme} {TOKEN}"}).status_code == 200


def test_bearer_value_tolerates_surrounding_whitespace():
    assert _app(TOKEN).post("/v1/sessions",
                            headers={"Authorization": f"Bearer  {TOKEN}  "}).status_code == 200


@pytest.mark.parametrize("auth", [
    f"Basic {TOKEN}", f"Token {TOKEN}", TOKEN, f"Bearer{TOKEN}", "Bearer",
])
def test_other_authorization_shapes_are_refused(auth):
    """Only the two documented forms work. A bare token in Authorization, or
    a different scheme, must not slip through."""
    assert _app(TOKEN).post("/v1/sessions",
                            headers={"Authorization": auth}).status_code == 401


def test_x_browser_token_wins_and_a_bad_bearer_does_not_override_it():
    assert _app(TOKEN).post("/v1/sessions", headers={
        "X-Browser-Token": TOKEN, "Authorization": "Bearer nonsense"}).status_code == 200


# ── what a refusal must not leak ────────────────────────────────────


def test_refusal_never_echoes_what_was_presented():
    presented = "GUESS-THIS-abc123"
    r = _app(TOKEN).post("/v1/sessions", headers={"X-Browser-Token": presented})
    assert presented not in r.text
    assert TOKEN not in r.text


def test_absent_and_wrong_are_indistinguishable_to_a_prober():
    c = _app(TOKEN)
    a = c.post("/v1/sessions")
    b = c.post("/v1/sessions", headers={"X-Browser-Token": "wrong"})
    assert (a.status_code, a.text) == (b.status_code, b.text)


def test_comparison_is_constant_time():
    """Guards against a later 'simplification' to ==. A token recoverable a
    byte at a time from response timing is not a token."""
    src = ast.get_source_segment(_SRC.read_text(), _extract("_require_token"))
    assert "compare_digest" in src, "the token comparison must stay constant-time"


def test_a_non_ascii_token_is_refused_not_a_500():
    """compare_digest raises TypeError on a str with codepoints > 127, and
    starlette decodes headers as latin-1 — so a header of raw high bytes
    arrives as exactly such a str. Passing it to compare_digest unencoded
    would turn an auth failure into a 500. Sent as bytes because httpx will
    not encode a non-ASCII str into a header at all."""
    r = _app(TOKEN).post("/v1/sessions",
                         headers={"X-Browser-Token": "pässwörd".encode("latin-1")})
    assert r.status_code == 401, "a non-ASCII token must be refused, not crash the API"


# ── the third consumer: the kernel proxy ────────────────────────────
# Easiest one to miss. The Browser system-app is `kind: external, port:
# 8830` (system-apps/browser/app.yaml), so every click in its UI is
# forwarded by server/applications/proxy.py to the control API. If the
# proxy does not present the token, setting one 401s the entire UI while
# the vault broker keeps working — which reads as "the app is broken",
# not "auth is on".

_PROXY_SRC = (pathlib.Path(__file__).resolve().parents[1]
              / "server" / "applications" / "proxy.py")


def _upstream_auth(token: str, app_id: str) -> dict:
    tree = ast.parse(_PROXY_SRC.read_text())
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "_upstream_auth"]
    assert fns, ("_upstream_auth is gone from proxy.py — the Browser UI will 401 "
                 "as soon as a control token is set")
    ns = {"_BROWSER_CONTROL_APP_ID": "browser", "_BROWSER_CONTROL_TOKEN": token}
    exec(compile(ast.Module(body=fns, type_ignores=[]), str(_PROXY_SRC), "exec"), ns)
    return ns["_upstream_auth"](app_id)


def test_proxy_sends_the_token_for_the_browser_app():
    assert _upstream_auth(TOKEN, "browser") == {"X-Browser-Token": TOKEN}


def test_proxy_sends_nothing_while_the_sidecar_is_open():
    assert _upstream_auth("", "browser") == {}


@pytest.mark.parametrize("app_id", ["browser-novnc", "vscode", "designs", "keys"])
def test_proxy_never_leaks_the_token_to_another_app(app_id):
    """Any other app id — including browser-novnc, the retired noVNC
    sister app, kept here as a regression id — must see no credential."""
    assert _upstream_auth(TOKEN, app_id) == {}


def test_the_forward_path_actually_applies_it():
    """A helper nobody calls is worse than no helper — it reads as covered."""
    src = _PROXY_SRC.read_text()
    assert "headers.update(_upstream_auth(app_id))" in src, (
        "_serve_external no longer applies _upstream_auth")


def test_the_websocket_path_applies_it_too():
    """The viewport rides serve_app_ws, and the sidecar's WS endpoint
    enforces the same token (HTTP middleware never runs for websocket
    scope) — so the WS handshake must carry the header or the viewport
    1008s in every harness deployment, where the token is always set."""
    src = _PROXY_SRC.read_text()
    assert "ws_headers.update(_upstream_auth(app_id))" in src, (
        "serve_app_ws no longer applies _upstream_auth to the WS handshake")
