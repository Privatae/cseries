"""The `autofill` host tool: wired in ALL the places, and leaking nothing.

Two jobs.

FIRST — the wiring. A tool has to appear in four independent structures,
and this repo has shipped the same bug at least four times: a schema plus
a working handler, but the name missing from the dispatch set, so every
call fell through to "Unknown tool" while the host could still SEE the
tool in its list. The registry comments name the victims — community_board,
archive_project, coding_bench, dcn, explore_environment. The failure is
invisible to any test that only checks the schema, so this checks all of
them at once and fails loudly if a future edit adds one without the rest.

SECOND — the promise. This tool exists to let a MODEL drive autofill, so
the thing that matters is that no operation of it can hand a model a
stored value. Every op is exercised against stubs and the whole response
is searched for the secret.

Run: pytest tests/test_autofill_tool_wiring.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest
import sys as _ts_sys, os as _ts_os
_ts_sys.path.insert(0, _ts_os.path.join(_ts_os.path.dirname(_ts_os.path.dirname(_ts_os.path.abspath(__file__))), 'scripts'))
from tools_source import tools_source  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "brain"))

from mastercode.public_tool_registry import (  # noqa: E402
    PUBLIC_META_TOOL_NAMES, PUBLIC_TOOL_SCHEMAS)
from mastercode.runtime.meta_tool_caps import (  # noqa: E402
    META_TOOL_CAPS, READ_SAFE_OPS, op_param_for)
from mastercode.runtime import tools  # noqa: E402

TOOL = "autofill"
PASSWORD = "Tr0ub4dor&3-real-signup"
EMAIL = "alex@example.test"
HOST = "acme.example"


# ── 1. the wiring ────────────────────────────────────────────────────

def test_the_model_is_shown_the_tool():
    assert TOOL in PUBLIC_TOOL_SCHEMAS


def test_the_router_will_dispatch_the_tool():
    """The one that has broken four times. A name here is what turns a
    visible tool into a callable one."""
    assert TOOL in PUBLIC_META_TOOL_NAMES, (
        "autofill has a schema but is missing from PUBLIC_META_TOOL_NAMES — "
        "every call would return 'Unknown tool' while the host still sees it")


def test_the_capability_gate_knows_the_tool():
    caps = META_TOOL_CAPS.get(TOOL)
    assert caps, "no META_TOOL_CAPS entry — the gate cannot authorize it"
    assert caps["domain"] == "core.entity"
    # Same bits as keys_manage: this brokers a USE of the user's sealed
    # entries and never returns bytes, which is exactly that tool's shape.
    assert caps["required_bits"] == META_TOOL_CAPS["keys_manage"]["required_bits"]


def test_the_handler_exists():
    assert hasattr(tools, "_exec_autofill")


def test_the_dispatch_arm_routes_to_the_handler():
    """A handler nothing calls is the other half of the same bug."""
    src = tools_source(REPO)
    assert 'elif name == "autofill":' in src
    assert "_exec_autofill(args, entity_id, pool)" in src


def test_only_the_read_ops_are_marked_read_safe():
    """grant/revoke/fill change what may happen unattended. If any of them
    were read-safe they would slip the write gate."""
    safe = READ_SAFE_OPS.get(TOOL) or {}
    assert set(safe) == {"saved", "list"}, safe
    for write_op in ("grant", "revoke", "fill"):
        assert write_op not in safe


def test_the_op_parameter_resolves():
    """READ_SAFE_OPS is looked up by the tool's op parameter name; a tool
    whose param is not 'op' must declare it in OP_PARAM or the read-safe
    map silently never matches."""
    assert op_param_for(TOOL) == "op"
    schema_params = PUBLIC_TOOL_SCHEMAS[TOOL]["function"]["parameters"]["properties"]
    assert "op" in schema_params


def test_every_schema_op_is_handled():
    """The enum the model is shown and the ops the handler implements must
    be the same set — an advertised op with no arm is a dead end."""
    enum = set(PUBLIC_TOOL_SCHEMAS[TOOL]["function"]["parameters"]
               ["properties"]["op"]["enum"])
    src = tools_source(REPO)
    body = src.split("async def _exec_autofill", 1)[1]
    for op in enum:
        assert f'"{op}"' in body, f"op {op!r} is advertised but not handled"


def test_the_description_tells_the_model_it_never_sees_values():
    """The tool's own description is the only thing a model reads before
    calling it. It has to say what it will not get back."""
    desc = PUBLIC_TOOL_SCHEMAS[TOOL]["function"]["description"]
    assert "never" in desc.lower()
    assert "grant" in desc.lower()


# ── 2. the promise: no op returns a stored value ─────────────────────

class FakePool:
    def acquire(self):
        return _Acq()


class _Acq:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *a):
        return False


class _Conn:
    async def execute(self, sql, *args):
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        if "INSERT" in sql:
            import datetime
            return {"id": 1, "granted_at": datetime.datetime(2026, 8, 30)}
        return None                      # not granted, by default

    async def fetch(self, sql, *args):
        return []


@pytest.fixture
def stubbed(monkeypatch):
    """Stub the vault so a stored PASSWORD is genuinely present — the
    point is that no response carries it out."""
    saved = [
        {"logical_name": f"form:{HOST}:email", "cred_type": "form_entry",
         "deactivated_at": None},
        {"logical_name": f"form:{HOST}:new-password", "cred_type": "form_secret",
         "deactivated_at": None},
    ]

    async def fake_list(pool, *, entity_id):
        return saved

    async def fake_use(pool, *, entity_id, name, action, request=None, actor=None):
        return {"field_type": "password" if "password" in name else "text"}

    monkeypatch.setattr("server.browser_autofill.vault_api.list_secrets", fake_list)
    monkeypatch.setattr("server.browser_autofill.authz.use_credential", fake_use)
    return saved


def call(**args):
    return asyncio.run(tools._exec_autofill(args, 3, FakePool()))


def test_saved_lists_keys_and_flags_but_no_values(stubbed):
    out = call(op="saved", host=HOST)
    assert out["status"] == "ok"
    entries = out["result"]["entries"]
    assert {e["key"] for e in entries} == {"email", "new-password"}
    pw = [e for e in entries if e["key"] == "new-password"][0]
    assert pw["secret"] is True
    assert PASSWORD not in str(out) and EMAIL not in str(out)
    assert not any("value" in e for e in entries)


def test_saved_says_plainly_when_there_is_no_grant(stubbed):
    """The model has to know to ask the user, rather than trying to fill
    and getting an opaque refusal."""
    out = call(op="saved", host=HOST)
    assert out["result"]["granted"] is False
    assert "NO standing grant" in out["result"]["note"]


def test_fill_without_a_grant_is_denied_not_errored(stubbed):
    """status='denied' with a usable sentence — the host can read this out
    to the user as the reason."""
    out = call(op="fill", host=HOST, session="acme",
               mapping=[{"ref": "e1", "key": "email"}])
    assert out["status"] == "denied"
    assert "no standing grant" in out["result"]
    assert HOST in out["result"]


def test_fill_needs_a_session_and_a_mapping(stubbed):
    assert call(op="fill", host=HOST)["status"] == "error"
    assert call(op="fill", host=HOST, session="acme")["status"] == "error"


def test_ops_needing_a_host_say_so(stubbed):
    for op in ("saved", "grant", "revoke", "fill"):
        out = call(op=op)
        assert out["status"] == "error"
        assert "host" in out["result"]


def test_list_never_needs_a_host(stubbed):
    out = call(op="list")
    assert out["status"] == "ok"
    assert "grants" in out["result"]


def test_an_unknown_op_is_refused(stubbed):
    out = call(op="delete-everything", host=HOST)
    assert out["status"] == "error"
    assert "unknown autofill op" in out["result"]


def test_revoke_says_the_entries_are_kept(stubbed):
    """A user revoking must not think they have deleted their data."""
    out = call(op="revoke", host=HOST)
    assert out["status"] == "ok"
    assert "kept" in out["result"]["note"]


# ── the grant is NOT the Host's to make (kimi B1, 2026-08-30) ──

def test_grant_is_not_an_advertised_op():
    """A grant WIDENS what happens unattended, so it must not originate from
    a model a hostile page can talk to. It is made in Keys."""
    enum = PUBLIC_TOOL_SCHEMAS[TOOL]["function"]["parameters"]["properties"]["op"]["enum"]
    assert "grant" not in enum, enum
    assert "revoke" in enum, "revoking only tightens — it stays the Host's to do"


def test_calling_grant_anyway_is_denied_and_says_where_to_go():
    """Belt to the schema's braces: a model that calls it regardless gets a
    refusal it can read out to the user, naming the surface."""
    out = call(op="grant", host=HOST)
    assert out["status"] == "denied"
    assert "Keys" in out["result"]
    assert HOST in out["result"]


def test_grant_denial_does_not_write_anything(stubbed, monkeypatch):
    """The refusal must be a refusal, not a grant with a grumpy message."""
    called = []

    async def boom(pool, **kw):
        called.append(kw)
        raise AssertionError("autofill.grant must not be reachable from the tool")

    monkeypatch.setattr("server.browser_autofill.grant", boom)
    out = call(op="grant", host=HOST)
    assert out["status"] == "denied"
    assert called == []


def test_the_description_tells_the_host_it_cannot_grant():
    desc = PUBLIC_TOOL_SCHEMAS[TOOL]["function"]["description"]
    # Matched case-insensitively on purpose. This asserted the shouted
    # "CANNOT create a grant" until e3233b12 rewrote the registry so that
    # descriptions say what a tool does and doctrine moved into the skills,
    # softening it to "You cannot create a grant". That was deliberate and the
    # assertion was never updated for it, so this has been failing on main.
    # What has to hold is that the host is told the grant is not its to make;
    # how loudly the sentence says so is the registry's business.
    assert "cannot create a grant" in desc.lower()
    assert "Keys" in desc


def test_no_op_of_this_tool_can_return_a_stored_value(stubbed):
    """The blanket check. Whatever the op, the response is searched for
    the secret — this is the promise that makes the tool safe to hand a
    model at all."""
    for args in (
        {"op": "saved", "host": HOST},
        {"op": "list"},
        {"op": "grant", "host": HOST},
        {"op": "revoke", "host": HOST},
        {"op": "fill", "host": HOST, "session": "acme",
         "mapping": [{"ref": "e1", "key": "new-password"}]},
    ):
        blob = str(call(**args))
        assert PASSWORD not in blob, f"{args['op']} leaked the password"
        assert EMAIL not in blob, f"{args['op']} leaked the email"
