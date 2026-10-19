"""Plain-text management grants: deny beats allow, and the draft refuses a
contradiction.

The gate's pure decision logic (no DB) plus the propose validator against a stub pool.
The signature/expiry path is unchanged and covered by scripts/check_nac_mgmt.py.

Run: pytest tests/test_nac_mgmt_deny.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from server.network import nac_mgmt as M  # noqa: E402
from server.network import nac_propose as P  # noqa: E402


# ── the gate ────────────────────────────────────────────────────────────────

ALLOW = {"server": "box", "ops": ["uptime", "df", M.CONTAIN_OP]}


def test_deny_beats_allow_in_the_same_grant():
    g = [{**ALLOW, "deny": ["df"]}]
    assert M.mgmt_decision(g, 1, "box", "uptime") is True
    assert M.mgmt_decision(g, 1, "box", "df") is False


def test_deny_in_another_grant_beats_this_grants_allow():
    g = [ALLOW, {"server": "box", "ops": [], "deny": [M.CONTAIN_OP],
                 "intent": {"want": "", "do_not_want": "never kill anything"}}]
    assert M.mgmt_decision(g, 1, "box", M.CONTAIN_OP) is False
    assert M.mgmt_decision(g, 1, "box", "uptime") is True
    denied = M.mgmt_denied_by(g, "box", M.CONTAIN_OP)
    assert denied is not None and denied["intent"]["do_not_want"] == "never kill anything"
    assert "never kill anything" in M.deny_reason(denied)


def test_star_server_deny_covers_every_box_and_deny_all_forbids_everything():
    g = [ALLOW, {"server": "*", "deny": ["df"]}]
    assert M.mgmt_decision(g, 1, "box", "df") is False
    assert M.mgmt_decision(g, 1, "box", "uptime") is True
    g2 = [ALLOW, {"server": "box", "deny": [M.DENY_ALL]}]
    for op in ("uptime", "df", M.CONTAIN_OP):
        assert M.mgmt_decision(g2, 1, "box", op) is False


def test_deny_is_scoped_to_its_server():
    g = [{"server": "other", "ops": ["df"]}, {"server": "box", "deny": ["df"]}]
    assert M.mgmt_decision(g, 1, "other", "df") is True


def test_pre_v125_grant_without_deny_key_still_allows():
    assert M.mgmt_decision([{"server": "box", "ops": ["uptime"]}], 1, "box", "uptime") is True
    assert M.mgmt_denied_by([{"server": "box", "ops": ["uptime"]}], "box", "uptime") is None


def test_harness_gate_honours_deny_on_run_image_and_mode():
    img = "ghcr.io/x/y:1"
    star = [{"server": "box", "ops": [M.HARNESS_RUN_OP, "image:*", M.LAB_MODE_TOKEN]}]
    assert M.harness_image_allowed(star, 1, "box", img) is True
    assert M.harness_image_allowed(star + [{"server": "box", "deny": [M.HARNESS_RUN_OP]}], 1, "box", img) is False
    assert M.harness_image_allowed(star + [{"server": "*", "deny": ["image:" + img]}], 1, "box", img) is False
    assert M.harness_image_allowed(star + [{"server": "*", "deny": ["image:" + img]}], 1, "box", "z:2") is True
    assert M.harness_image_allowed(star + [{"server": "box", "deny": [M.LAB_MODE_TOKEN]}], 1, "box", img, mode="lab") is False
    assert M.harness_image_allowed(star + [{"server": "box", "deny": [M.LAB_MODE_TOKEN]}], 1, "box", img) is True


def test_deny_reason_without_words_still_names_the_grant():
    assert "deny beats allow" in M.deny_reason({"server": "box", "deny": ["df"]})
    assert "deny beats allow" in M.deny_reason(None)


# ── the draft ───────────────────────────────────────────────────────────────

class _Pool:
    """Records the INSERT; any other execute() is swallowed."""

    def __init__(self):
        self.inserts = []

    async def execute(self, *a):
        return None

    async def fetchval(self, sql, *args):
        self.inserts.append((sql, args))
        return 7


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_propose_carries_deny_and_the_users_words():
    pool = _Pool()
    res = _run(P.propose_mgmt_grant(
        pool, server="box", ops=["uptime", " observe:disk "], deny=["contain", "contain"],
        want="check the disks every morning", do_not_want="  never   kill anything "))
    assert res["ops"] == ["uptime", "observe:disk"] and res["deny"] == ["contain"]
    assert res["intent"] == {"want": "check the disks every morning", "do_not_want": "never kill anything"}
    assert res["requires_stepup"] is True
    sql, args = pool.inserts[0]
    assert "deny" in sql and "intent" in sql
    assert json.loads(args[2]) == ["contain"]
    assert json.loads(args[3])["do_not_want"] == "never kill anything"


def test_deny_only_draft_is_a_tighten():
    res = _run(P.propose_mgmt_grant(_Pool(), server="box", deny=["harness:run"],
                                    do_not_want="no coding agents on my laptop"))
    assert res["ops"] == [] and res["deny"] == ["harness:run"]
    assert res["requires_stepup"] is False


def test_contradiction_and_empty_drafts_are_refused():
    with pytest.raises(ValueError, match="both allowed and denied"):
        _run(P.propose_mgmt_grant(_Pool(), server="box", ops=["observe:disk"], deny=["observe:disk"]))
    with pytest.raises(ValueError, match="both allowed and denied"):
        _run(P.propose_mgmt_grant(_Pool(), server="box", ops=["observe:disk"], deny=[M.DENY_ALL]))
    with pytest.raises(ValueError, match="ops .* or deny"):
        _run(P.propose_mgmt_grant(_Pool(), server="box"))
    with pytest.raises(ValueError, match="server"):
        _run(P.propose_mgmt_grant(_Pool(), server="", ops=["observe:disk"]))
    with pytest.raises(ValueError, match="too long"):
        _run(P.propose_mgmt_grant(_Pool(), server="box", ops=["observe:disk"], want="x" * 601))


def test_nul_in_words_is_dropped_not_a_db_error():
    res = _run(P.propose_mgmt_grant(_Pool(), server="box", ops=["observe:disk"], want="check\x00 disks"))
    assert res["intent"]["want"] == "check disks"


# ── revoking a held deny is a WIDEN ─────────────────────────────────────────

from server.network import nac_commit as C  # noqa: E402
from server.network import nac_stepup  # noqa: E402


class _Conn:
    def __init__(self, row):
        self.row, self.executed = row, []

    async def fetchrow(self, sql, *a):
        return self.row

    async def execute(self, sql, *a):
        self.executed.append(sql)
        # asyncpg answers a write with its status ("UPDATE <n>"); revoke_mgmt_grant reads it
        # to tell a still-draft decline from one that raced an approval (nac_commit).
        return "UPDATE 1"


class _AcqPool(_Pool):
    def __init__(self, row):
        super().__init__()
        self.conn = _Conn(row)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def test_grant_revoke_widens_only_on_signed_deny():
    assert C.grant_revoke_widens(None) is False
    assert C.grant_revoke_widens({"approved_at": None, "signed_terms": json.dumps({"deny": ["df"]})}) is False
    assert C.grant_revoke_widens({"approved_at": 1, "signed_terms": json.dumps({"ops": ["df"], "deny": []})}) is False
    assert C.grant_revoke_widens({"approved_at": 1, "signed_terms": json.dumps({"ops": [], "deny": ["df"]})}) is True
    assert C.grant_revoke_widens({"approved_at": 1, "signed_terms": {"deny": ["*"]}}) is True
    assert C.grant_revoke_widens({"approved_at": 1, "signed_terms": None}) is False


def test_revoke_of_a_held_deny_needs_the_code(monkeypatch):
    held = {"id": 9, "approved_at": 1, "signed_terms": json.dumps({"ops": [], "deny": ["contain"]})}
    codes = []

    async def verify(pool, code):
        codes.append(code)
        return code == "123456"
    monkeypatch.setattr(nac_stepup, "verify_stepup", verify)

    pool = _AcqPool(held)
    with pytest.raises(C.CommitError) as ei:
        _run(C.revoke_mgmt_grant(pool, 7, 9))
    assert ei.value.status == 403 and pool.conn.executed == []
    res = _run(C.revoke_mgmt_grant(pool, 7, 9, stepup_code="123456"))
    assert res == {"grant_id": 9, "revoked": True, "stepped_up": True} and len(pool.conn.executed) == 1

    # an allow-only grant, and a draft, still revoke free
    for row in ({"id": 1, "approved_at": 1, "signed_terms": json.dumps({"ops": ["df"], "deny": []})},
                {"id": 2, "approved_at": None, "signed_terms": None}):
        p = _AcqPool(row)
        res = _run(C.revoke_mgmt_grant(p, 7, row["id"]))
        assert res["stepped_up"] is False and len(p.conn.executed) == 1
    with pytest.raises(C.CommitError) as ei:
        _run(C.revoke_mgmt_grant(_AcqPool(None), 7, 404))
    assert ei.value.status == 404


def test_tokens_only_draft_has_no_intent():
    res = _run(P.propose_mgmt_grant(_Pool(), server="box", ops=["observe:disk"]))
    assert res["intent"] is None
