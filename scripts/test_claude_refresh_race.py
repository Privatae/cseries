#!/usr/bin/env python3
"""Gate: a credential refresh racing a Disconnect never brings the credential back.

The defect. Both writers of a refreshed Claude credential check the disconnect marker
and then write: the kernel's token refresh (brain/mastercode/runtime/tools.py
_exec_system_refresh_sdk_auth, which then seals the file into the Vault) and a run's
rotated credential (server/routes/review.py _persist_rotated_creds, whose login callers
then seal). A Disconnect that completes between the check and the write removes the
file and the Vault copy first, and the write puts the credential straight back:
connected again on disk (dispatch gates on the file), and in the Vault after the seal.

The fix re-checks the marker after the write and removes what it wrote. That works
because harness_state.disconnect_for_credential sets the marker BEFORE it removes the
file; this gate pins that order too.

Real harness_state, nac_harness, review and tools code; a stand-in pool, a stand-in
Vault, temp files, and the token endpoint stood in. No network, no pod.

Run: APP_ROOT=<repo> python3 scripts/test_claude_refresh_race.py
"""
import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

logging.disable(logging.CRITICAL)
ROOT = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]

FAILS: list[str] = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail != "" and not ok else ""))
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
        _eid, path, value = args
        self.params[path[0]] = json.loads(value)
        return "UPDATE 1"


from server import brain_service  # noqa: E402
from server.network import harness_state as hs  # noqa: E402
from server.network import nac_harness as nh  # noqa: E402
from server.routes import review  # noqa: E402
from server.vault import api as vault_api  # noqa: E402
from mastercode.runtime import tools  # noqa: E402

OLD = json.dumps({"claudeAiOauth": {"accessToken": "at-old-invented", "refreshToken": "rt-old-invented",
                                    "expiresAt": 4102444800000}})
NEW = json.dumps({"claudeAiOauth": {"accessToken": "at-new-invented", "refreshToken": "rt-new-invented",
                                    "expiresAt": 4102444900000}})

pool = StubPool()
brain_service._pool = pool
os.environ["ENTITY_ID"] = "7"
tmp = Path(tempfile.mkdtemp(prefix="claude-refresh-race-"))
cfile = tmp / ".claude" / ".credentials.json"
cfile.parent.mkdir(parents=True)
VAULT: dict = {}
ORDER: list[str] = []


async def _open_secret(pool, *, entity_id, name, actor=None, **kw):
    v = VAULT.get(name)
    return v.encode() if v else None


async def _store_secret(pool, *, entity_id, name, cred_type, plaintext, actor=None, **kw):
    VAULT[name] = plaintext.decode()
    return "invented-envelope"


async def _revoke_secret(pool, *, entity_id, name, actor=None, **kw):
    ORDER.append("vault revoked")
    return VAULT.pop(name, None) is not None


real_mark = hs.mark_disconnected
real_is = hs.is_disconnected


async def _mark(provider):
    ORDER.append("marker set")
    return await real_mark(provider)


real_unlink = os.unlink


def _unlink(path, *a, **kw):
    if str(path) == str(cfile):
        ORDER.append("file removed")
    return real_unlink(path, *a, **kw)


def disconnect_after(nth):
    """is_disconnected that answers the real value, and on its nth call has a Disconnect
    complete right after the answer was read: the window between the check and the write."""
    calls = {"n": 0}

    async def _is(provider):
        calls["n"] += 1
        got = await real_is(provider)
        if calls["n"] == nth:
            await hs.disconnect_for_credential("harness:claude_oauth", actor="test")
        return got
    return _is


class _TokenResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def open(self, req, timeout=None):
        return _TokenResp(json.dumps({"access_token": "at-new-invented", "refresh_token": "rt-new-invented",
                                      "expires_in": 3600, "scope": "user:inference"}).encode())


saved = (vault_api.open_secret, vault_api.store_secret, vault_api.revoke_secret, nh._AGENT_CREDS_PATH,
         nh.read_agent_creds, review._HARNESS_CRED_FILES, hs.mark_disconnected, hs.is_disconnected,
         urllib.request.build_opener, os.unlink)
vault_api.open_secret, vault_api.store_secret, vault_api.revoke_secret = \
    _open_secret, _store_secret, _revoke_secret
nh._AGENT_CREDS_PATH = str(cfile)
nh.read_agent_creds = lambda: cfile.read_text().strip() if cfile.exists() else None
review._HARNESS_CRED_FILES = {**review._HARNESS_CRED_FILES,
                              "claude-code": (str(cfile.parent), (".credentials.json",))}
hs.mark_disconnected = _mark
urllib.request.build_opener = lambda *a, **kw: _Opener()
os.unlink = _unlink
try:
    # ── 0. the order the fix relies on ────────────────────────────────────────────────────
    print("0. a Disconnect sets the marker before it removes the file")
    cfile.write_text(OLD)
    VAULT["harness:claude_oauth"] = OLD
    run(hs.disconnect_for_credential("harness:claude_oauth", actor="test"))
    check("marker set, then the Vault copy revoked and the file removed",
          ORDER[:1] == ["marker set"] and "file removed" in ORDER
          and ORDER.index("marker set") < ORDER.index("file removed"), ORDER)

    # ── 1. the kernel's token refresh ─────────────────────────────────────────────────────
    print("1. the kernel's token refresh, with a Disconnect between its last check and its write")
    run(hs.clear_disconnected("claude-sdk"))
    cfile.write_text(OLD)
    VAULT["harness:claude_oauth"] = OLD
    ORDER.clear()
    # Call 1 is the check before the POST, call 2 the check after it: the Disconnect
    # completes right after call 2 read "connected", before the write.
    hs.is_disconnected = disconnect_after(2)
    out = run(tools._exec_system_refresh_sdk_auth({}))
    check("the Disconnect did run in the window", "marker set" in ORDER and run(real_is("claude-sdk")), ORDER)
    check("the refresh leaves no credential file", not cfile.exists(),
          cfile.read_text()[:80] if cfile.exists() else "")
    check("...and no Vault copy", "harness:claude_oauth" not in VAULT, list(VAULT))
    check("...and does not report a refresh", out.get("status") != "ok", out)

    print("1b. with no Disconnect, the refresh writes and seals as before")
    hs.is_disconnected = real_is
    run(hs.clear_disconnected("claude-sdk"))
    cfile.write_text(OLD)
    VAULT.pop("harness:claude_oauth", None)
    out = run(tools._exec_system_refresh_sdk_auth({}))
    check("refreshed: the new token is on disk and sealed",
          out.get("status") == "ok" and "at-new-invented" in cfile.read_text()
          and "at-new-invented" in VAULT.get("harness:claude_oauth", ""), out)

    # ── 2. a run's rotated credential ─────────────────────────────────────────────────────
    print("2. a run's rotated credential, with a Disconnect between the check and the write")
    run(hs.clear_disconnected("claude-sdk"))
    cfile.write_text(OLD)
    VAULT["harness:claude_oauth"] = OLD
    ORDER.clear()
    hs.is_disconnected = disconnect_after(1)
    run(review._persist_rotated_creds("claude-code", {"rotated_creds": {".credentials.json": NEW}}))
    check("the Disconnect did run in the window", "marker set" in ORDER and run(real_is("claude-sdk")), ORDER)
    check("the rotated credential is not left on disk", not cfile.exists(),
          cfile.read_text()[:80] if cfile.exists() else "")
    check("...and nothing is in the Vault for a later seal to find or keep",
          "harness:claude_oauth" not in VAULT, list(VAULT))

    print("2b. with no Disconnect, the rotation is persisted as before")
    hs.is_disconnected = real_is
    run(hs.clear_disconnected("claude-sdk"))
    cfile.write_text(OLD)
    run(review._persist_rotated_creds("claude-code", {"rotated_creds": {".credentials.json": NEW}}))
    check("persisted", cfile.exists() and "at-new-invented" in cfile.read_text())
finally:
    (vault_api.open_secret, vault_api.store_secret, vault_api.revoke_secret, nh._AGENT_CREDS_PATH,
     nh.read_agent_creds, review._HARNESS_CRED_FILES, hs.mark_disconnected, hs.is_disconnected,
     urllib.request.build_opener, os.unlink) = saved
    for p in sorted(tmp.rglob("*"), reverse=True):
        try:
            p.unlink() if p.is_file() else p.rmdir()
        except OSError:
            pass
    try:
        tmp.rmdir()
    except OSError:
        pass

print(f"{'PASS' if not FAILS else 'FAIL'}: a refresh racing a Disconnect brings nothing back"
      + (f" — {len(FAILS)} failing: {FAILS}" if FAILS else ""))
sys.exit(1 if FAILS else 0)
