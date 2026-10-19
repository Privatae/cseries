"""Replay: the grant gate, and where a saved password may be typed.

The SQL half (the grant table itself) is exercised against a REAL
Postgres by scripts/test_autofill_grant_pg.py, run inside the entity pod
— stubbing a database would test the stub. What is pinned HERE is the
decision logic, which is where the security property lives:

  - no live grant -> nothing is typed, at all;
  - a value SAVED as a password is never given allow_non_password, and
    the caller cannot influence that (it is read from the stored
    cred_type, not from the request);
  - a deactivated entry reads as absent, like everywhere else;
  - one field failing does not abandon the rest of the form.

Run: pytest tests/test_browser_autofill.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from server import browser_autofill as autofill  # noqa: E402

HOST = "acme.example"


def do_fill(*a, **kw):
    """Drive autofill.fill to completion, synchronously.

    Deliberately NOT pytest-asyncio: it is not in requirements.txt and no
    other test here uses it, so an @pytest.mark.asyncio suite would SKIP
    silently wherever it is absent — reporting "verified" for tests that
    never ran, which is the exact trap tests/test_browser_snapshot_js.py
    was written about.
    """
    return asyncio.run(autofill.fill(*a, **kw))


class FakePool:
    """Just enough asyncpg surface for the grant read + the last_used touch."""

    def __init__(self, granted=True):
        self.granted = granted
        self.touched = 0

    def acquire(self):
        return _Acquire(self)


class _Acquire:
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

    async def fetch(self, sql, *args):
        return []


SAVED = [
    {"logical_name": f"form:{HOST}:email", "cred_type": "form_entry",
     "deactivated_at": None},
    {"logical_name": f"form:{HOST}:new-password", "cred_type": "form_secret",
     "deactivated_at": None},
    {"logical_name": f"form:{HOST}:given-name", "cred_type": "form_entry",
     "deactivated_at": None},
    {"logical_name": "form:other.example:email", "cred_type": "form_entry",
     "deactivated_at": None},
    {"logical_name": "provider:anthropic", "cred_type": "api_key",
     "deactivated_at": None},
]

MAP = [{"ref": "e1", "key": "email"},
       {"ref": "e2", "key": "new-password"},
       {"ref": "e3", "key": "given-name"}]


class Rig:
    def __init__(self):
        self.calls = []
        self.saved = [dict(s) for s in SAVED]
        self.fail = {}


@pytest.fixture
def rig(monkeypatch):
    """Stub the vault + broker; record every browser_type request.

    String targets rather than attribute targets so the stubs name the
    exact module path they replace.
    """
    r = Rig()

    async def fake_list(pool, *, entity_id):
        return list(r.saved)

    async def fake_use(pool, *, entity_id, name, action, request=None,
                       actor=None):
        r.calls.append({"name": name, "action": action, **(request or {})})
        if name in r.fail:
            raise autofill.authz.VaultAuthzError(r.fail[name])
        return {"field_type": "password" if "password" in name else "text"}

    monkeypatch.setattr("server.browser_autofill.vault_api.list_secrets",
                        fake_list)
    monkeypatch.setattr("server.browser_autofill.authz.use_credential",
                        fake_use)
    return r


# ── the grant gate ───────────────────────────────────────────────────

def test_no_grant_means_nothing_is_typed(rig):
    """Deny by default. Not 'fills the harmless ones' — nothing."""
    with pytest.raises(autofill.AutofillError) as e:
        do_fill(FakePool(granted=False), entity_id=9003, host=HOST,
                            session="acme", mapping=MAP, actor="test")
    assert "no standing grant" in str(e.value)
    assert HOST in str(e.value), "the refusal must name the site"
    assert rig.calls == [], "not one field may be typed without a grant"


def test_a_grant_lets_the_fill_through(rig):
    out = do_fill(FakePool(), entity_id=9003, host=HOST,
                              session="acme", mapping=MAP, actor="test")
    assert [f["key"] for f in out["filled"]] == [
        "email", "new-password", "given-name"]
    assert out["skipped"] == []


# ── where a saved password may go ────────────────────────────────────

def test_a_saved_password_never_gets_allow_non_password(rig):
    """THE security property. A value stored as a password may only ever
    land in a real password box; allow_non_password is what would widen
    that, and it is decided from the stored cred_type."""
    do_fill(FakePool(), entity_id=9003, host=HOST, session="acme",
                        mapping=MAP, actor="test")
    by_name = {c["name"]: c for c in rig.calls}
    assert by_name[f"form:{HOST}:new-password"]["allow_non_password"] is False
    # ...while ordinary fields DO need it, because browser_type refuses a
    # non-password target by default.
    assert by_name[f"form:{HOST}:email"]["allow_non_password"] is True
    assert by_name[f"form:{HOST}:given-name"]["allow_non_password"] is True


def test_the_caller_cannot_widen_allow_non_password(rig):
    """A caller that smuggles the flag into the mapping must not reach the
    broker with it — this module builds the request, not the caller."""
    hostile = [{"ref": "e2", "key": "new-password",
                "allow_non_password": True, "expect_host": "evil.example"}]
    do_fill(FakePool(), entity_id=9003, host=HOST, session="acme",
                        mapping=hostile, actor="test")
    call = rig.calls[0]
    assert call["allow_non_password"] is False
    assert call["expect_host"] == HOST, \
        "expect_host comes from the grant, not the caller"


def test_expect_host_is_always_the_granted_host(rig):
    do_fill(FakePool(), entity_id=9003, host=HOST, session="acme",
                        mapping=MAP, actor="test")
    assert {c["expect_host"] for c in rig.calls} == {HOST}


def test_every_field_goes_through_the_broker_separately(rig):
    """One use_credential per field, so each keeps its own audit row and
    its own host check — the reason this is not a bulk broker action."""
    do_fill(FakePool(), entity_id=9003, host=HOST, session="acme",
                        mapping=MAP, actor="test")
    assert len(rig.calls) == 3
    assert {c["action"] for c in rig.calls} == {"browser_type"}


# ── what is and is not reachable ─────────────────────────────────────

def test_another_sites_entry_is_not_reachable(rig):
    """A grant for acme must resolve acme's email, never other.example's."""
    out = do_fill(
        FakePool(), entity_id=9003, host=HOST, session="acme",
        mapping=[{"ref": "e1", "key": "email"}], actor="test")
    assert rig.calls[0]["name"] == f"form:{HOST}:email"
    assert out["filled"][0]["key"] == "email"


def test_a_deactivated_entry_reads_as_absent(rig):
    """Keys' off-switch must work here too — off is off."""
    for s in rig.saved:
        if s["logical_name"].endswith(":email"):
            s["deactivated_at"] = "2026-08-30T00:00:00Z"
    out = do_fill(
        FakePool(), entity_id=9003, host=HOST, session="acme",
        mapping=[{"ref": "e1", "key": "email"}], actor="test")
    assert out["filled"] == []
    assert "nothing saved" in out["skipped"][0]["reason"]
    assert rig.calls == []


def test_an_unknown_key_is_skipped_with_a_reason(rig):
    out = do_fill(
        FakePool(), entity_id=9003, host=HOST, session="acme",
        mapping=[{"ref": "e9", "key": "not-saved"}], actor="test")
    assert out["filled"] == []
    assert out["skipped"][0]["key"] == "not-saved"


def test_a_non_form_credential_is_not_reachable(rig):
    """provider:anthropic sits in the same vault. Autofill must not see
    it — the form: prefix is the whole boundary."""
    out = do_fill(
        FakePool(), entity_id=9003, host=HOST, session="acme",
        mapping=[{"ref": "e1", "key": "anthropic"},
                 {"ref": "e2", "key": "provider:anthropic"}], actor="test")
    assert out["filled"] == []
    assert rig.calls == []


# ── partial failure ──────────────────────────────────────────────────

def test_one_refused_field_does_not_abandon_the_form(rig):
    """A half-filled form the caller can see beats an opaque abort — and
    the caller learns exactly which element refused."""
    rig.fail = {f"form:{HOST}:new-password":
                "browser_type REFUSED: session is on evil.example"}
    out = do_fill(FakePool(), entity_id=9003, host=HOST,
                              session="acme", mapping=MAP, actor="test")
    assert [f["key"] for f in out["filled"]] == ["email", "given-name"]
    assert out["skipped"][0]["key"] == "new-password"
    assert "evil.example" in out["skipped"][0]["reason"]


def test_a_broker_failure_reason_never_carries_a_value(rig, monkeypatch):
    """VaultAuthzError is documented to hold no secret bytes; anything
    else is reduced to its TYPE, so a stray __str__ cannot leak one."""
    class Boom(Exception):
        def __str__(self):
            return "hunter2-REAL-PASSWORD"

    async def blow_up(pool, **kw):
        raise Boom()

    monkeypatch.setattr("server.browser_autofill.authz.use_credential",
                        blow_up)
    out = do_fill(FakePool(), entity_id=9003, host=HOST,
                              session="acme", mapping=MAP, actor="test")
    assert "hunter2-REAL-PASSWORD" not in str(out)
    assert out["skipped"][0]["reason"] == "Boom"


# ── last_used_at is NOT this module's job any more ───────────────────

def test_fill_does_not_record_last_used_itself(rig):
    """It moved to the broker guard (authz._guard_form_entry) on kimi's
    second pass: fill() was the only writer, so a use through
    POST /api/vault/secrets/{name}/use left the grant reading "never used"
    — the signal Keys offers for pruning. One writer on every path beats
    one writer on one path. Asserted here so nobody re-adds it and
    double-counts. Coverage of the writer itself lives in
    tests/test_autofill_broker_guard.py.
    """
    pool = FakePool()
    do_fill(pool, entity_id=9003, host=HOST, session="acme",
                        mapping=MAP, actor="test")
    assert pool.touched == 0


def test_an_empty_mapping_is_refused(rig):
    with pytest.raises(autofill.AutofillError):
        do_fill(FakePool(), entity_id=9003, host=HOST,
                            session="acme", mapping=[], actor="test")


def test_a_bad_host_is_refused_before_anything_else(rig):
    with pytest.raises(autofill.AutofillError):
        do_fill(FakePool(), entity_id=9003, host="not a host",
                            session="acme", mapping=MAP, actor="test")
    assert rig.calls == []
