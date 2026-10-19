"""The three doors to browser_type, and the guard that now covers all of them.

`keys_manage` op='use' passed a caller-chosen `action` straight into
`use_credential`, so a model could name a saved `form_secret` and pass `allow_non_password: true` —
typing the user's password into a visible text box, with no standing grant
checked anywhere. That was demonstrated end to end, not by reading.

Asking whether the same shape existed elsewhere found a second door:
`POST /api/vault/secrets/{name}/use` (server/routes/vault.py) does exactly
the same pass-through, gated only on `write:vault` — which the Browser app
holds.

So the fix did NOT go where the report prescribed it (the keys_manage use arm).
It went to `authz._guard_form_entry`, the one place all three callers
converge and the only place that can read the stored cred_type instead of
believing the caller about it. The keys_manage allowlist landed too, as a
second layer and because an unenforced schema enum is its own defect.

These tests reproduce the EXPLOIT, not just the fix: each one asks for the
thing shown to be possible and asserts it is now refused.

Run: pytest tests/test_autofill_broker_guard.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "brain"))

from server.vault import authz  # noqa: E402
from server import browser_autofill as autofill  # noqa: E402

HOST = "acme.example"
SECRET_NAME = f"form:{HOST}:new-password"
PLAIN_NAME = f"form:{HOST}:email"
PASSWORD = b"Tr0ub4dor&3-real-signup"


class Pool:
    """Enough asyncpg surface for the grant read."""

    def __init__(self, granted: bool):
        self.granted = granted
        self.touched = 0

    def acquire(self):
        return _Acq(self)


class _Acq:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *args):
        if "last_used_at=now()" in sql:
            self.pool.touched += 1
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        return {"ok": 1} if self.pool.granted else None

    async def fetchval(self, sql, *args):
        return None


CRED_TYPES = {SECRET_NAME: "form_secret", PLAIN_NAME: "form_entry"}


@pytest.fixture
def broker(monkeypatch):
    """Stub the vault + the sidecar; record what the browser leg was ASKED
    to do, which is the thing under test."""
    seen = {}

    async def fake_open(pool, *, entity_id, name, actor=None,
                        include_deactivated=False):
        return PASSWORD

    async def fake_cred_type(pool, *, owner_identity, logical_name):
        return CRED_TYPES.get(logical_name)

    async def fake_type(secret, request):
        seen.clear()
        seen.update(request)
        return {"field_type": "password", "url": "https://acme.example/login"}

    async def fake_audit(pool, *, actor, owner, name, detail):
        return None

    monkeypatch.setattr("server.vault.authz.vault_api.open_secret", fake_open)
    monkeypatch.setattr("server.vault.authz.store.cred_type_of", fake_cred_type)
    monkeypatch.setattr("server.vault.authz._do_browser_type", fake_type)
    monkeypatch.setattr("server.vault.authz._audit_use", fake_audit)
    return seen


def use(pool, name, request):
    return asyncio.run(authz.use_credential(
        pool, entity_id=9003, name=name, action="browser_type",
        request=request, actor="test"))


# ── the exploit, reproduced ──────────────────────────────────────────

def test_the_exploit_is_refused_when_there_is_no_grant(broker):
    """THE finding. A caller names a saved password and asks for
    allow_non_password. Before the fix this typed the password into
    whatever element it named, with no grant anywhere in the picture."""
    with pytest.raises(authz.VaultAuthzError) as e:
        use(Pool(granted=False), SECRET_NAME,
            {"session": "s", "ref": "e1", "expect_host": HOST,
             "allow_non_password": True})
    assert "no standing grant" in str(e.value)
    assert broker == {}, "nothing may reach the sidecar without a grant"


def test_allow_non_password_is_stripped_even_with_a_grant(broker):
    """Grant present, caller still asks to widen. The stored cred_type
    decides, not the request."""
    use(Pool(granted=True), SECRET_NAME,
        {"session": "s", "ref": "e1", "allow_non_password": True})
    assert broker["allow_non_password"] is False


def test_expect_host_is_taken_from_the_name_not_the_caller(broker):
    """A caller could otherwise point a saved credential at whatever host
    its session happened to be sitting on."""
    use(Pool(granted=True), SECRET_NAME,
        {"session": "s", "ref": "e1", "expect_host": "evil.example"})
    assert broker["expect_host"] == HOST


def test_a_plain_form_entry_may_still_be_widened(broker):
    """Not a blanket clamp: email/name fields legitimately need
    allow_non_password, because browser_type refuses a non-password target
    by default. Only a stored form_secret is pinned."""
    use(Pool(granted=True), PLAIN_NAME,
        {"session": "s", "ref": "e1", "allow_non_password": True})
    assert broker["allow_non_password"] is True


def test_a_malformed_form_name_is_refused(broker):
    with pytest.raises(authz.VaultAuthzError):
        use(Pool(granted=True), "form:justonepart", {"session": "s", "ref": "e1"})


def test_a_non_form_credential_is_untouched(broker):
    """The guard narrows nothing that was not autofill's. A hand-added site
    login reaching browser_type behaves exactly as before — no grant lookup,
    caller's expect_host and allow_non_password respected."""
    use(Pool(granted=False), "acme-site-login",
        {"session": "s", "ref": "e1", "expect_host": "elsewhere.example",
         "allow_non_password": True})
    assert broker["expect_host"] == "elsewhere.example"
    assert broker["allow_non_password"] is True


# ── last_used_at, on every path ──────────────────────────────────────

def test_the_grant_is_marked_used_on_every_path(broker):
    """autofill.fill was the only writer, so a use
    through the vault HTTP route left the grant reading "never used". Keys
    offers that as the signal to prune on, so under-reporting would have a
    user revoke a grant that is in fact in use."""
    pool = Pool(granted=True)
    use(pool, SECRET_NAME, {"session": "s", "ref": "e1"})
    assert pool.touched == 1


def test_a_refused_use_does_not_mark_the_grant_used(broker):
    """No grant, nothing typed, nothing recorded."""
    pool = Pool(granted=False)
    with pytest.raises(authz.VaultAuthzError):
        use(pool, SECRET_NAME, {"session": "s", "ref": "e1"})
    assert pool.touched == 0


def test_a_non_form_credential_never_touches_a_grant(broker):
    pool = Pool(granted=True)
    use(pool, "acme-site-login", {"session": "s", "ref": "e1"})
    assert pool.touched == 0


# ── the second layer, and the drift it exists to stop ────────────────

def test_keys_manage_refuses_browser_type():
    from mastercode.runtime import tools
    assert "browser_type" not in tools._KEYS_MANAGE_USE_ACTIONS
    out = asyncio.run(tools._exec_keys_manage(
        {"op": "use", "name": SECRET_NAME, "action": "browser_type",
         "request": {"session": "s", "ref": "e1", "allow_non_password": True}}))
    assert out["status"] == "error"
    assert "autofill" in out["result"], (
        "the refusal should name the tool that IS allowed to do this")


def test_the_allowlist_equals_the_advertised_enum():
    """The defect was an enum nobody enforced. Pin the two together so they
    cannot drift apart again in either direction."""
    from mastercode.runtime import tools
    from mastercode import public_tool_registry as reg
    advertised = set(reg.PUBLIC_TOOL_SCHEMAS["keys_manage"]["function"]
                     ["parameters"]["properties"]["action"]["enum"])
    assert set(tools._KEYS_MANAGE_USE_ACTIONS) == advertised


def test_the_form_constants_match_browser_autofill():
    """authz declares these locally to avoid an import cycle. If
    browser_autofill renames either, the guard silently stops matching."""
    assert authz._FORM_PREFIX == autofill.NAME_PREFIX
    assert authz._FORM_SECRET_TYPE == autofill.CRED_TYPE_SECRET
