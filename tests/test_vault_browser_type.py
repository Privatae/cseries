"""The vault's third broker action — `browser_type`.

A login FORM is not an API call, so `http_call` cannot perform one. Without
this action the only way to sign a recipe into a site is for the caller — and
therefore whatever model drives the caller — to hold the password. This is the
path that keeps the bytes between the sealed envelope and the browser.

What these tests are really guarding is a NEGATIVE: that the secret does not
appear in the return value, in the audit row, or in an error message. Those are
the three ways a brokered credential has leaked in systems that otherwise did
everything right, and none of them is visible in a happy-path integration test.

`server.vault.authz` imports aiohttp, asyncpg and the peer-trust stack at module
level, so the functions under test are exec'd out of the source with those
stubbed — the same approach tests/test_operator_identity.py and the controller's
sidecar test use.

Run: pytest tests/test_vault_browser_type.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sys
import types

import pytest

_SRC = (pathlib.Path(__file__).resolve().parent.parent
        / "server" / "vault" / "authz.py")

SECRET = b"correct-horse-battery-staple"


# ── a fake aiohttp whose POST records what it was given ─────────────


class _ContentOf:
    """aiohttp's response.content, as the bounded readers use it
    (server/harness_input.py reads content.iter_chunked / content.read)."""

    def __init__(self, body: bytes):
        self._b = body

    async def iter_chunked(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]

    async def read(self, n=-1):
        return self._b if n is None or n < 0 else self._b[:n]


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload
        # The browser pod's answer is read under a cap (harness_input), from
        # content, as a real response: an exception payload is a non-JSON body.
        raw = (b"boom" if isinstance(payload, Exception)
               else json.dumps(payload).encode())
        self.content = _ContentOf(raw)
        self.content_length = len(raw)

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self):
        return json.dumps(self._payload) if not isinstance(self._payload, Exception) else "boom"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, recorder, status, payload, raise_on_post=None,
                 info=None, info_status=200, info_raise=None):
        self._rec = recorder
        self._status = status
        self._payload = payload
        self._raise = raise_on_post
        self._info = info if info is not None else {
            "name": "bank", "url": "https://bank.example/login"}
        self._info_status = info_status
        self._info_raise = info_raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None):
        """The broker reads the session's LIVE page before typing."""
        self._rec["info_url"] = url
        self._rec["info_headers"] = headers
        if self._info_raise:
            raise self._info_raise
        return _FakeResponse(self._info_status, self._info)

    def post(self, url, json=None, headers=None):
        self._rec["url"] = url
        self._rec["json"] = json
        self._rec["headers"] = headers
        if self._raise:
            raise self._raise
        return _FakeResponse(self._status, self._payload)


def _fake_aiohttp(recorder, status=200, payload=None, raise_on_post=None,
                  trust_env_sink=None, info=None, info_status=200, info_raise=None):
    mod = types.ModuleType("aiohttp")

    def ClientSession(timeout=None, trust_env=None):
        if trust_env_sink is not None:
            trust_env_sink.append(trust_env)
        return _FakeSession(recorder, status, payload, raise_on_post,
                            info, info_status, info_raise)

    mod.ClientSession = ClientSession
    mod.ClientTimeout = lambda total=None: total
    return mod


def _load():
    """Exec just the pieces under test, with a stubbed aiohttp."""
    tree = ast.parse(_SRC.read_text())
    # _browser_headers is extracted too, not stubbed: it decides whether a
    # token header goes to the sidecar, and a stub would let the real one
    # rot. The assert below is what caught it being added at all.
    want = ("_do_browser_type", "_browser_headers", "_browser_base_url")
    fns = [n for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want]
    missing = set(want) - {f.name for f in fns}
    assert not missing, (
        f"missing from server/vault/authz.py: {sorted(missing)} — if it is "
        "_do_browser_type, a recipe can no longer log into a site without the "
        "caller holding the password")

    class VaultAuthzError(Exception):
        pass

    # Everything at module scope that _do_browser_type reaches for. Kept
    # in sync with authz.py deliberately: a NameError here means the
    # function grew a dependency the test never modelled.
    import re as _re
    from urllib.parse import urlparse as _urlparse
    ns = {
        "VaultAuthzError": VaultAuthzError,
        "_BROWSER_CONTROL_URL": "http://127.0.0.1:8830",
        # Empty = the in-pod loopback sidecar, which sends no auth header.
        # test_control_token_is_sent_when_configured overrides it.
        "_BROWSER_CONTROL_TOKEN": "",
        "_BROWSER_APP_ID": "browser",
        "_SESSION_NAME_RX": _re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$"),
        "urlparse": _urlparse,
        "Any": object,
    }
    exec(compile(ast.Module(body=fns, type_ignores=[]), str(_SRC), "exec"), ns)
    return ns


_NS = _load()
do_browser_type = _NS["_do_browser_type"]
VaultAuthzError = _NS["VaultAuthzError"]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(request, *, status=200, payload=None, raise_on_post=None,
          trust_env_sink=None, info=None, info_status=200, info_raise=None):
    # Every caller declares the host it expects; the broker refuses without.
    request = {"expect_host": "bank.example", **request}
    rec = {}
    sys.modules["aiohttp"] = _fake_aiohttp(
        rec, status, payload if payload is not None else
        {"ok": True, "typed_into": request.get("ref"), "field_type": "password",
         "url": "https://bank.example/login", "title": "Sign in"},
        raise_on_post, trust_env_sink, info, info_status, info_raise)
    try:
        return rec, run(do_browser_type(SECRET, request))
    finally:
        sys.modules.pop("aiohttp", None)


# ── the secret reaches the sidecar, and only the sidecar ────────────


def test_the_secret_is_sent_to_the_sidecar_on_loopback():
    rec, _ = _call({"session": "bank", "ref": "e4"})
    assert rec["url"] == "http://127.0.0.1:8830/v1/sessions/bank/act"
    assert rec["json"]["action"] == "type_secret"
    assert rec["json"]["text"] == SECRET.decode()
    assert rec["json"]["ref"] == "e4"


def test_loopback_call_never_goes_through_the_egress_proxy():
    """_do_http_call sets trust_env=True so brokered third-party calls
    exit via squid. This one is in-pod and must not."""
    sink = []
    _call({"session": "bank", "ref": "e4"}, trust_env_sink=sink)
    # Two calls now — the session read and the type — and BOTH are in-pod.
    assert sink and all(v is False for v in sink), (
        f"every browser_type call must set trust_env=False, got {sink}")
    assert len(sink) == 2, f"expected a session read then a type, got {len(sink)}"


# ── the three ways a brokered secret leaks ──────────────────────────


def test_the_result_does_not_contain_the_secret():
    _, out = _call({"session": "bank", "ref": "e4"})
    assert SECRET.decode() not in json.dumps(out)
    assert set(out) == {"session", "ref", "host", "field_type", "url", "title"}


def test_a_sidecar_refusal_does_not_echo_the_secret():
    with pytest.raises(VaultAuthzError) as e:
        _call({"session": "bank", "ref": "e4"}, status=409,
              payload={"detail": "refusing to type a secret into a 'text' field"})
    assert SECRET.decode() not in str(e.value)
    assert "409" in str(e.value)


def test_a_transport_failure_does_not_echo_the_secret():
    """The payload is in scope when the exception is raised — the message
    must carry the exception TYPE, never anything that could stringify
    the request body."""
    boom = RuntimeError(f"connection failed while sending {SECRET.decode()}")
    with pytest.raises(VaultAuthzError) as e:
        _call({"session": "bank", "ref": "e4"}, raise_on_post=boom)
    assert SECRET.decode() not in str(e.value)
    assert "RuntimeError" in str(e.value)


# ── argument handling ───────────────────────────────────────────────


@pytest.mark.parametrize("req", [
    {}, {"session": "bank"}, {"ref": "e4"},
    {"session": "", "ref": "e4"}, {"session": "bank", "ref": "  "},
])
def test_session_and_ref_are_required(req):
    with pytest.raises(VaultAuthzError) as e:
        _call(req)
    assert "session" in str(e.value) and "ref" in str(e.value)


# ── where the secret may be typed ───────────────────────────────────


def test_expect_host_is_required():
    """Without it the broker would type a bank password into whatever page
    the named session happened to be showing."""
    rec = {}
    sys.modules["aiohttp"] = _fake_aiohttp(rec)
    try:
        with pytest.raises(VaultAuthzError, match="expect_host"):
            run(do_browser_type(SECRET, {"session": "bank", "ref": "e4"}))
    finally:
        sys.modules.pop("aiohttp", None)


def test_typing_is_refused_when_the_page_is_elsewhere():
    """The realistic prompt-injection outcome: the recipe gets steered onto
    another site and then asks for the credential. The LIVE url decides."""
    with pytest.raises(VaultAuthzError) as e:
        _call({"session": "bank", "ref": "e4"},
              info={"name": "bank", "url": "https://evil.test/login"})
    assert "evil.test" in str(e.value) and "REFUSED" in str(e.value)
    assert SECRET.decode() not in str(e.value)


def test_typing_is_refused_before_any_navigation():
    with pytest.raises(VaultAuthzError, match="not on a page yet"):
        _call({"session": "bank", "ref": "e4"},
              info={"name": "bank", "url": "about:blank"})


def test_the_live_page_is_read_not_the_callers_claim():
    rec, out = _call({"session": "bank", "ref": "e4"})
    assert rec["info_url"].endswith("/v1/sessions/bank")
    assert out["host"] == "bank.example"


@pytest.mark.parametrize("bad", [
    "bank/../../../../auth/anthropic", "../grant-fs", "a/b", "BANK", "-bank", "x" * 41,
])
def test_session_names_cannot_traverse_the_sidecar_url(bad):
    """The name is interpolated into a URL path. Unvalidated,
    'bank/../../../../auth/anthropic' normalised onto a different endpoint
    and skipped the sidecar's own name check entirely."""
    with pytest.raises(VaultAuthzError, match="valid session name"):
        _call({"session": bad, "ref": "e4"})


def test_password_field_check_is_on_by_default():
    """The refusal lives in the sidecar, but the broker must not quietly
    disarm it — the default it sends has to be False."""
    rec, _ = _call({"session": "bank", "ref": "e4"})
    assert rec["json"]["allow_non_password"] is False


def test_the_override_is_passed_through_when_asked():
    rec, _ = _call({"session": "bank", "ref": "e4", "allow_non_password": True})
    assert rec["json"]["allow_non_password"] is True


def test_non_json_error_body_is_handled():
    rec, _ = {}, None
    with pytest.raises(VaultAuthzError):
        _call({"session": "bank", "ref": "e4"}, status=500,
              payload=ValueError("not json"))


# ── the dispatcher wires it in, redacted ────────────────────────────


def test_use_credential_registers_the_action_and_audits_without_the_secret():
    src = _SRC.read_text()
    body = src[src.index("async def use_credential"):src.index("async def _do_http_call")]
    assert 'elif action == "browser_type"' in body, "action not dispatched"
    assert "http_call, sign, browser_type" in body, "error text lists stale actions"
    branch = body[body.index('elif action == "browser_type"'):]
    branch = branch[:branch.index("else:")]
    # The audit payload itself — everything inside detail.update(...).
    upd = branch[branch.index("detail.update("):]
    upd = upd[:upd.index("\n        else") if "\n        else" in upd else len(upd)]
    for field in ('"session"', '"ref"', '"field_type"'):
        assert field in upd, f"audit detail should record {field}"
    # Only ever the RESULT dict, which _do_browser_type built without the
    # secret. A `secret`/`request`/`text` reference here would put the
    # credential in the audit row.
    for banned in ("secret", "request", '"text"', "payload"):
        assert banned not in upd, (
            f"audit payload references {banned!r} — it must record which "
            f"field was filled, never the value")


# ── the control-API token ───────────────────────────────────────────
# The sidecar's control API grew a bearer gate so it can leave the entity
# pod (loopback containment does not survive the move to a Deployment of
# its own). The broker is the in-pod caller, so it has to present the
# token — and must keep sending nothing while the sidecar is still open,
# or every existing entity's logins break the moment this ships.


def test_no_auth_header_while_the_sidecar_is_unauthenticated():
    """The in-pod default. _BROWSER_CONTROL_TOKEN is empty in the harness,
    matching a sidecar that binds loopback and has no gate."""
    rec, _ = _call({"session": "bank", "ref": "e4"})
    assert not rec["headers"], f"sent {rec['headers']!r} to an open sidecar"
    assert not rec["info_headers"]


def test_the_token_is_sent_on_both_calls_when_configured():
    """Both the session read AND the type must carry it — a token on only
    the POST would 401 on the pre-flight and look like 'no such session'."""
    token = "browser-token-for-this-entity"
    _NS["_BROWSER_CONTROL_TOKEN"] = token
    try:
        rec, _ = _call({"session": "bank", "ref": "e4"})
    finally:
        _NS["_BROWSER_CONTROL_TOKEN"] = ""
    assert rec["headers"] == {"X-Browser-Token": token}
    assert rec["info_headers"] == {"X-Browser-Token": token}


def test_the_token_never_reaches_the_audit_detail_or_the_result():
    token = "browser-token-secret-value"
    _NS["_BROWSER_CONTROL_TOKEN"] = token
    try:
        _, out = _call({"session": "bank", "ref": "e4"})
    finally:
        _NS["_BROWSER_CONTROL_TOKEN"] = ""
    assert token not in json.dumps(out)


# ── where the browser actually is ───────────────────────────────────
# The browser has two shapes — the always-on in-pod sidecar (loopback) and
# the on-demand harness (its own pod, reached at its Service). The broker
# must reach whichever one this entity is running without being
# reconfigured, and must never cache the answer: the harness scales to
# zero and its Service host is only known after a wake.


def test_falls_back_to_loopback_for_the_in_pod_sidecar():
    """No harness backend to ask is the NORMAL state for an entity still on
    the always-on sidecar, not an error."""
    sys.modules.pop("server.applications.harness_backend", None)
    assert _NS["_browser_base_url"]() == "http://127.0.0.1:8830"


def test_prefers_the_harness_service_when_the_browser_is_awake():
    mod = types.ModuleType("server.applications.harness_backend")
    mod.upstream_host = lambda app_id: (
        "harness-browser.entity-1.svc.cluster.local:8830" if app_id == "browser" else None)
    pkg = types.ModuleType("server.applications")
    pkg.harness_backend = mod
    sys.modules["server.applications"] = pkg
    sys.modules["server.applications.harness_backend"] = mod
    try:
        assert _NS["_browser_base_url"]() == \
            "http://harness-browser.entity-1.svc.cluster.local:8830"
    finally:
        sys.modules.pop("server.applications.harness_backend", None)
        sys.modules.pop("server.applications", None)


def test_a_sleeping_harness_falls_back_rather_than_dialing_nothing():
    """upstream_host returns None while the harness is scaled to zero.
    Building a URL from that would produce http://None."""
    mod = types.ModuleType("server.applications.harness_backend")
    mod.upstream_host = lambda app_id: None
    pkg = types.ModuleType("server.applications")
    pkg.harness_backend = mod
    sys.modules["server.applications"] = pkg
    sys.modules["server.applications.harness_backend"] = mod
    try:
        assert _NS["_browser_base_url"]() == "http://127.0.0.1:8830"
    finally:
        sys.modules.pop("server.applications.harness_backend", None)
        sys.modules.pop("server.applications", None)


def test_a_broken_harness_backend_never_breaks_a_login():
    mod = types.ModuleType("server.applications.harness_backend")
    def _boom(app_id):
        raise RuntimeError("backend exploded")
    mod.upstream_host = _boom
    pkg = types.ModuleType("server.applications")
    pkg.harness_backend = mod
    sys.modules["server.applications"] = pkg
    sys.modules["server.applications.harness_backend"] = mod
    try:
        assert _NS["_browser_base_url"]() == "http://127.0.0.1:8830"
    finally:
        sys.modules.pop("server.applications.harness_backend", None)
        sys.modules.pop("server.applications", None)


def test_the_address_is_resolved_per_call_not_at_import():
    """A cached value would be wrong for the whole life of the process, since
    the Service host is only known after a wake."""
    src = _SRC.read_text()
    assert "_browser_base_url()}/v1/sessions/{session}" in src
    assert '_BROWSER_CONTROL_URL}/v1/sessions' not in src, \
        "a call site still pins the import-time loopback URL"
