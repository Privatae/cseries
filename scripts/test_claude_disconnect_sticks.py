#!/usr/bin/env python3
"""Gate: the Claude Code app's Disconnect stays disconnected, and connecting again works.

The defect. The app's Disconnect deleted only ~/.claude/.credentials.json. Nothing set
the disconnect marker, so the Host's reconcile (nac_harness.materialize_agent_creds, at
boot and every 300 s) found the file absent and the Vault copy harness:claude_oauth
present, and wrote the file back: Claude reconnected on its own. The full disconnect
already existed — revoking harness:claude_oauth (DELETE /api/vault/secrets/{name} ->
harness_state.disconnect_for_credential) sets the marker, revokes the Vault copy and
deletes the file — but the app held no scope that reaches it.

Two more holes on the same path. review._persist_rotated_creds read the marker under the
app id "claude-code" while it is only ever filed as "claude-sdk", so a run finishing after
a Disconnect wrote the credential back. And once the marker is honoured everywhere, every
way of connecting Claude again has to lift it, or a person who disconnects can never come
back: Keys (POST /api/vault/secrets) did; the in-pod sign-in (sdk_auth finish) and the
Host-run vendor login did not.

Pins, each against the real code with a stand-in pool, a stand-in vault and temp files:
  1. the app's Disconnect calls the Vault delete, and the app holds the narrow scope for it
  2. that scope opens the delete for the app's own sign-in only
  3. after the disconnect, no reconcile and no finishing run brings the credential back
  4. each way of connecting again lifts the marker, and the reconcile then restores again
  5. a disconnect whose marker cannot be written answers an error and removes nothing

Run: APP_ROOT=<repo> python3 scripts/test_claude_disconnect_sticks.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import types
from pathlib import Path

logging.disable(logging.CRITICAL)
ROOT = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

FAILS: list[str] = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def run(coro):
    return asyncio.run(coro)


class StubPool:
    """The behavioral_params reads and jsonb_set writes harness_state makes."""

    def __init__(self):
        self.params: dict = {}

    async def fetchrow(self, sql, *args):
        if "behavioral_params" in sql:
            return {"behavioral_params": json.dumps(self.params)}
        return None

    async def execute(self, sql, *args):
        assert "jsonb_set" in sql, sql
        if getattr(self, "fail_writes", False):
            raise RuntimeError("invented write fault")
        _eid, path, value = args
        self.params[path[0]] = json.loads(value)
        return "UPDATE 1"


CREDS = json.dumps({"claudeAiOauth": {"accessToken": "at-invented", "refreshToken": "rt-invented",
                                      "expiresAt": 4102444800000}})

# ── 1. the app's Disconnect is the full disconnect ─────────────────────────────────────
print("1. the Claude Code app's Disconnect goes through the Host's disconnect")
app_js = (Path(ROOT) / "system-apps/claude-code/app.js").read_text()
fn = app_js[app_js.find("async function disconnect()"):]
fn = fn[:fn.find("\n    }\n") + 6]
vault_at = fn.find("/api/vault/secrets/")
side_at = fn.find("sidecarUrl('disconnect')")
check("Disconnect calls DELETE /api/vault/secrets/<harness:claude_oauth>",
      vault_at >= 0 and "'DELETE'" in fn and "harness:claude_oauth" in app_js
      and "encodeURIComponent(CLAUDE_KEY)" in fn, fn[:400])
check("...before its own sidecar /disconnect (which only clears the file and the cache)",
      0 <= vault_at < side_at, (vault_at, side_at))
_comment = app_js[:app_js.find("async function disconnect()")]
_comment = " ".join(l.strip().lstrip("/").strip() for l in _comment[_comment.rfind("// ---- Disconnect"):].splitlines())
check("...and its comment no longer says a 404 means nothing was sealed (the Host answers 200 "
      "for the app with no Vault copy)",
      "404 means" not in _comment and "answers 200" in _comment, _comment)
check("...so every answer but success is reported, a 404 included",
      "if (!r.ok) {" in fn and "!== 404" not in fn, fn[:600])

from server.applications import scopes  # noqa: E402
from server.applications.manifest import load as load_manifest  # noqa: E402
man = load_manifest(Path(ROOT) / "system-apps/claude-code/app.yaml")
check("the app declares disconnect:own_harness and nothing wider",
      man.permissions == ["disconnect:own_harness"], man.permissions)
check("disconnect:own_harness is a known scope (an unknown one fails the manifest)",
      "disconnect:own_harness" in scopes.PERMISSIONS)

# ── 2. the scope opens the delete for the app's own sign-in only ───────────────────────
print("2. disconnect:own_harness reaches only the app's own sign-in")
from fastapi import HTTPException  # noqa: E402
from server.routes import vault as vault_routes  # noqa: E402

route = next(r for r in vault_routes.router.routes
             if getattr(r, "path", "").endswith("/secrets/{name}") and "DELETE" in r.methods)
scopes_on_route = [getattr(d.call, "_app_scopes", (getattr(d.call, "_app_scope", None),))
                   for d in route.dependant.dependencies]
check("DELETE /api/vault/secrets/{name} takes write:vault or disconnect:own_harness",
      ("write:vault", "disconnect:own_harness") in scopes_on_route, scopes_on_route)


def _req(app_id, perms):
    return types.SimpleNamespace(state=types.SimpleNamespace(
        app_token=types.SimpleNamespace(app_id=app_id, permissions=list(perms))))


def _admits(app_id, perms, name):
    guard = getattr(vault_routes, "_own_harness_only", None)
    if guard is None:
        return None
    try:
        guard(_req(app_id, perms), name)
        return True
    except HTTPException as e:
        return e.status_code


narrow = ["disconnect:own_harness"]
check("the Claude Code app may remove harness:claude_oauth",
      _admits("claude-code", narrow, "harness:claude_oauth") is True)
check("...but not another key", _admits("claude-code", narrow, "openrouter") == 403)
check("...nor another engine's sign-in",
      _admits("claude-code", narrow, "harness:kimi:provider") == 403)
check("another app holding the scope may not remove Claude's sign-in",
      _admits("notes", narrow, "harness:claude_oauth") == 403)
check("write:vault still removes any key", _admits("keys", ["write:vault"], "openrouter") is True)

# ── 3. after the disconnect nothing brings it back ──────────────────────────────────────
print("3. after Disconnect, no reconcile and no finishing run writes the credential back")
from server import brain_service  # noqa: E402
from server.network import harness_state as hs  # noqa: E402
from server.network import nac_harness as nh  # noqa: E402
from server.routes import review  # noqa: E402
from server.vault import api as vault_api  # noqa: E402

pool = StubPool()
brain_service._pool = pool
os.environ["ENTITY_ID"] = "7"
tmp = Path(tempfile.mkdtemp(prefix="claude-disconnect-"))
cfile = tmp / ".claude" / ".credentials.json"
cfile.parent.mkdir(parents=True)
VAULT: dict = {}


async def _open_secret(pool, *, entity_id, name, actor=None, **kw):
    v = VAULT.get(name)
    return v.encode() if v else None


async def _store_secret(pool, *, entity_id, name, cred_type, plaintext, actor=None, **kw):
    VAULT[name] = plaintext.decode()
    return 1


async def _revoke_secret(pool, *, entity_id, name, actor=None, **kw):
    return VAULT.pop(name, None) is not None


saved = (vault_api.open_secret, vault_api.store_secret, vault_api.revoke_secret,
         nh._AGENT_CREDS_PATH, nh.read_agent_creds, review._HARNESS_CRED_FILES)
vault_api.open_secret, vault_api.store_secret, vault_api.revoke_secret = \
    _open_secret, _store_secret, _revoke_secret
nh._AGENT_CREDS_PATH = str(cfile)
nh.read_agent_creds = lambda: cfile.read_text().strip() if cfile.exists() else None
review._HARNESS_CRED_FILES = {**review._HARNESS_CRED_FILES,
                              "claude-code": (str(cfile.parent), (".credentials.json",))}
try:
    cfile.write_text(CREDS)
    VAULT["harness:claude_oauth"] = CREDS
    check("precondition: connected — file and Vault copy present, reconcile is a no-op",
          run(nh.materialize_agent_creds(pool, 7)) == "present")

    # What the Vault delete runs once the route has revoked the key.
    run(hs.disconnect_for_credential("harness:claude_oauth", actor="test"))
    check("the disconnect marks Claude disconnected, revokes the Vault copy, removes the file",
          run(hs.is_disconnected("claude-sdk")) and "harness:claude_oauth" not in VAULT
          and not cfile.exists(), (pool.params, list(VAULT), cfile.exists()))
    # The restore loop: boot, then every 300 s. Even with a Vault copy put back (the
    # original bug: the app removed only the file), the marker holds it off.
    VAULT["harness:claude_oauth"] = CREDS
    outcomes = [run(nh.materialize_agent_creds(pool, 7)) for _ in range(3)]
    check("three reconcile passes leave it disconnected and write no file",
          outcomes == ["disconnected"] * 3 and not cfile.exists(), (outcomes, cfile.exists()))
    VAULT.pop("harness:claude_oauth", None)
    run(review._persist_rotated_creds("claude-code", {"rotated_creds": {".credentials.json": CREDS}}))
    check("a Claude run finishing after the Disconnect does not write its rotated credential "
          "back (the marker is read under claude-sdk, not the app id)", not cfile.exists())

    # The app's Disconnect through the real route, on a Host whose sign-in has no Vault copy
    # yet (the reconcile seals one within 300 s of a sign-in, so a Disconnect inside that
    # window finds none). The route must still run the disconnect: without the marker, a
    # run that finishes afterwards writes the credential straight back.
    run(hs.clear_disconnected("claude-sdk"))
    cfile.write_text(CREDS)
    VAULT.pop("harness:claude_oauth", None)
    _app_req = types.SimpleNamespace(
        headers={}, client=None,
        state=types.SimpleNamespace(app_token=types.SimpleNamespace(
            app_id="claude-code", permissions=["disconnect:own_harness"])))
    try:
        _route_out = run(vault_routes.revoke_secret("harness:claude_oauth", _app_req))
    except HTTPException as e:
        _route_out = e.status_code
    check("the app's Disconnect with no Vault copy still marks Claude disconnected and "
          "removes the file",
          isinstance(_route_out, dict) and _route_out.get("disconnected") == "claude-sdk"
          and run(hs.is_disconnected("claude-sdk")) and not cfile.exists(),
          (_route_out, pool.params, cfile.exists()))
    cfile.unlink(missing_ok=True)   # the app's second call: its sidecar removes the file
    run(review._persist_rotated_creds("claude-code", {"rotated_creds": {".credentials.json": CREDS}}))
    check("...so a run finishing after it does not write the credential back", not cfile.exists())
    _keys_req = types.SimpleNamespace(
        headers={}, client=None,
        state=types.SimpleNamespace(app_token=types.SimpleNamespace(
            app_id="keys", permissions=["write:vault"])))
    try:
        _keys_out = run(vault_routes.revoke_secret("openrouter", _keys_req))
    except HTTPException as e:
        _keys_out = e.status_code
    check("a write:vault revoke of a key that is not there still answers 404", _keys_out == 404,
          _keys_out)

    # ── 4. connecting again lifts the marker, and restore works again ──────────────────
    print("4. each way of connecting Claude again lifts the marker")
    # Keys (and keys_manage): POST /api/vault/secrets -> reconnect_for_credential.
    check("Keys: storing harness:claude_oauth reconnects claude-sdk",
          run(hs.reconnect_for_credential("harness:claude_oauth")) == "claude-sdk"
          and not run(hs.is_disconnected("claude-sdk")))

    # The Host-run vendor login hands its grant back as rotated_creds.
    run(hs.mark_disconnected("claude-sdk"))
    lift = getattr(review, "_reconnect_on_login", None)
    if lift is not None:
        run(lift("claude-code"))
    run(review._persist_rotated_creds("claude-code", {"rotated_creds": {".credentials.json": CREDS}}))
    check("Host-run login: the grant lifts the marker and lands on disk",
          not run(hs.is_disconnected("claude-sdk")) and cfile.exists())
    check("...and its status reads connected",
          hs.key_state(present=cfile.exists(), valid=True,
                       disconnected=run(hs.is_disconnected("claude-sdk"))) == "connected")

    # The in-pod sign-in (sdk_auth finish), with the token endpoint stood in.
    cfile.unlink()
    run(hs.mark_disconnected("claude-sdk"))
    from server.routes import sdk_auth

    class _Resp:
        status = 200

        async def text(self):
            return json.dumps({"access_token": "at-new-invented", "refresh_token": "rt-new-invented",
                               "expires_in": 3600, "scope": "user:inference"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **kw):
            pass

        def post(self, *a, **kw):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    saved_sdk = (sdk_auth.aiohttp.ClientSession, sdk_auth.CREDS_PATH)
    sdk_auth.aiohttp.ClientSession = _Session
    sdk_auth.CREDS_PATH = cfile
    try:
        sdk_auth._SESSIONS["s-invented"] = {"verifier": "v" * 43, "state": "st", "created_at": 0,
                                            "auth_url": "https://example.invalid/"}
        out = run(sdk_auth.finish(sdk_auth.FinishBody(session_id="s-invented", code="code#st")))
    finally:
        sdk_auth.aiohttp.ClientSession, sdk_auth.CREDS_PATH = saved_sdk
    check("sdk_auth finish: the sign-in lifts the marker and writes the credential",
          out.get("ok") is True and not run(hs.is_disconnected("claude-sdk")) and cfile.exists(),
          (out, pool.params))
    check("...and seals the Vault copy", "harness:claude_oauth" in VAULT, list(VAULT))

    # Restore works again: the file is gone (a pod roll), the Vault copy writes it back.
    cfile.unlink()
    check("restore: with the marker lifted, the reconcile writes the file back from the Vault",
          run(nh.materialize_agent_creds(pool, 7)) == "materialized" and cfile.exists())

    # ── 5. a marker that cannot be written stops the disconnect ────────────────────────
    print("5. a disconnect whose marker cannot be written fails loudly and removes nothing")

    def _intact():
        return ("harness:claude_oauth" in VAULT and cfile.exists()
                and not run(hs.is_disconnected("claude-sdk")))

    check("precondition: connected, with a Vault copy and the file", _intact(),
          (list(VAULT), cfile.exists(), pool.params))
    pool.fail_writes = True
    try:
        for _who, _r in (("the app's Disconnect", _app_req), ("a Keys revoke", _keys_req)):
            try:
                _o = run(vault_routes.revoke_secret("harness:claude_oauth", _r))
            except HTTPException as e:
                _o = e
            check(f"{_who}: answers 503 saying the disconnect was not recorded",
                  isinstance(_o, HTTPException) and _o.status_code == 503
                  and "could not record" in str(_o.detail), _o)
            check(f"{_who}: removes nothing (Vault copy, file) and leaves no marker",
                  _intact(), (list(VAULT), cfile.exists(), pool.params))
        _raised = None
        try:
            run(hs.disconnect_for_credential("harness:claude_oauth", actor="test"))
        except Exception as e:
            _raised = e
        check("disconnect_for_credential raises before removing the file",
              isinstance(_raised, getattr(hs, "DisconnectNotRecorded", ())) and cfile.exists(),
              (_raised, cfile.exists()))
        sys.path.insert(0, os.path.join(ROOT, "brain"))
        from mastercode.runtime import tools as _tools
        _t = run(_tools._exec_keys_manage({"op": "revoke", "name": "harness:claude_oauth"}))
        check("the Host's keys_manage revoke reports the failure and revokes nothing",
              _t.get("status") == "error" and "could not record" in str(_t.get("result"))
              and _intact(), (_t, list(VAULT), cfile.exists()))
    finally:
        pool.fail_writes = False
    VAULT.pop("harness:claude_oauth", None)
    try:
        run(vault_routes.revoke_secret("harness:claude_oauth", _keys_req))
        _o = None
    except HTTPException as e:
        _o = e.status_code
    check("a Keys revoke that finds nothing still answers 404 and leaves no marker behind",
          _o == 404 and not run(hs.is_disconnected("claude-sdk")), (_o, pool.params))
    run(hs.mark_disconnected("claude-sdk"))
    try:
        run(vault_routes.revoke_secret("harness:claude_oauth", _keys_req))
        _o = None
    except HTTPException as e:
        _o = e.status_code
    check("...and a marker an earlier disconnect set stays", _o == 404
          and run(hs.is_disconnected("claude-sdk")), (_o, pool.params))
finally:
    (vault_api.open_secret, vault_api.store_secret, vault_api.revoke_secret,
     nh._AGENT_CREDS_PATH, nh.read_agent_creds, review._HARNESS_CRED_FILES) = saved
    for p in sorted(tmp.rglob("*"), reverse=True):
        try:
            p.unlink() if p.is_file() else p.rmdir()
        except OSError:
            pass
    try:
        tmp.rmdir()
    except OSError:
        pass

print(f"{'PASS' if not FAILS else 'FAIL'}: Claude disconnect sticks and reconnect works"
      + (f" — {len(FAILS)} failing: {FAILS}" if FAILS else ""))
sys.stdout.flush()
sys.exit(1 if FAILS else 0)
