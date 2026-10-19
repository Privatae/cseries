"""The pairing token must survive an enrolment that failed.

The failure behind this: a fleet outage in which every NEW bastion pairing
returned 502 for ~22 hours while already-paired bastions kept working.

`redeem()` consumes the single-use token atomically BEFORE calling the control
plane -- that ordering is what makes it replay-safe and it is correct. But there
was no compensating re-arm, so every control-plane fault destroyed a 15-minute
credential and left the user unable to retry: bridge disconnected (503), no
active endpoint (503), CSR sign failure (500), pubkey already enrolled (409), no
pod-Hub identity (503), or a malformed bridge reply (502). It bit hardest exactly
when the bridge was already unhealthy -- the moment a retry is most likely to be
what they need.

Deliberately NOT "created nothing" (here or in the source): the bridge INSERTs
bastion_peers at adapter.py:5839 and only then checks the CA (:5876) and the
pod-Hub identity (:5886), so those two -- and a client-side timeout -- leave an
inert orphan peer row. The token is re-armed on those paths anyway, because the
orphan is unusable and refusing the retry helps no one. What the re-arm is scoped
to is this host having minted a bearer.

The client author had already held this line; the server had not.
See client-go/pair.go: "Conflicts are checked BEFORE the redeem so a refusal
doesn't burn the single-use token."

These tests assert the BEHAVIOUR that was missing, not the implementation: after
a failed enrolment the token is spendable again, after a successful one it is
not, and the re-arm never swallows the error that caused it.

Run: pytest tests/test_bastion_redeem_token_burn.py
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
import sys

import pytest
from fastapi import HTTPException

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "brain"))

from server.network import bastion_enroll  # noqa: E402

CSR = "-----BEGIN CERTIFICATE REQUEST-----\nnot-a-real-csr\n-----END CERTIFICATE REQUEST-----"
PUBKEY = "x" * 44
HANDLE = "99dab9c47d60edff"
SECRET = "s3cret"
# The re-arm is PK-keyed; asserting the exact value is what stops a regression
# that drops `WHERE secret_hash = $1` or passes the wrong hash from passing.
EXPECTED_HASH = hashlib.sha256(SECRET.encode()).digest()

COMPLETE_ENROLL = {
    "operator_crt": "crt", "ca_crt": "ca", "endpoint": "host:52001",
    "endpoint_pubkey": "pk", "host_tunnel_ip": "2001:db8::e:1",
    "bastion_tunnel_ip": "2001:db8::a:1", "operator_uuid": "uuid",
}


class Pool:
    """Enough asyncpg surface for redeem(), and it TRACKS the token's state.

    The token is modelled as a real value rather than as 'was some UPDATE
    called', so the assertions below are about whether the token is spendable
    -- which is the property the user actually experiences.
    """

    def __init__(self, rearm_raises: bool = False):
        self.used = False          # None-vs-set on bastion_pairing_tokens.used_at
        self.rearm_raises = rearm_raises
        self.rearm_calls = 0
        self.rearm_args = []
        self.bastion_rows = 0

    async def execute(self, sql, *args):
        if "SET used_at = NULL" in sql:
            self.rearm_calls += 1
            self.rearm_args.append(args)
            if self.rearm_raises:
                raise RuntimeError("pg gone at the worst moment")
            # Honour the real WHERE: a re-arm that did not name THIS token's
            # secret_hash must not clear it. Without this the fake would pass a
            # regression that dropped the predicate or passed the wrong hash.
            if args != (EXPECTED_HASH,):
                return "UPDATE 0"
            self.used = False
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        if "SET used_at = now()" in sql:
            if self.used:            # the real WHERE used_at IS NULL guard
                return None
            self.used = True
            return {"name": "desktop"}
        if "INSERT INTO mcp_client_tokens" in sql:
            self.bastion_rows += 1
            return {"id": 7}
        return None


async def _redeem(pool):
    return await bastion_enroll.redeem(
        pool, entity_id=9128, handle=HANDLE, secret=SECRET,
        bastion_pubkey=PUBKEY, operator_csr=CSR,
        hostname="desktop", platform="linux",
    )


@pytest.fixture
def stub(monkeypatch):
    """Neutralise everything downstream of the bridge call; the bridge itself is
    set per-test because it is the thing that fails."""
    async def no_push(*a, **k):
        return None
    monkeypatch.setattr(bastion_enroll, "push_bastion_peer", no_push, raising=False)
    monkeypatch.setattr(
        "server.network.hub_peers.push_bastion_peer", no_push, raising=False)

    # The pod's Hub answers as one whose image predates GET /identity, so the
    # machine is handed the bridge's endpoint_pubkey (hub_identity
    # pubkey_for_new_machine). A Hub that does not answer at all refuses the
    # pairing instead (scripts/test_hub_host_identity.py part 8), and these tests
    # are about the token, not the Hub key.
    from server.network import hub_identity

    async def old_hub():
        return hub_identity.PRE_IDENTITY
    monkeypatch.setattr(hub_identity, "fetch_status", old_hub)
    return monkeypatch


# ── the regression: a failed enrolment must leave the token spendable ────────

@pytest.mark.parametrize(
    "code,detail",
    [
        # These four fail with nothing created at all.
        (503, "bridge not connected — cannot enroll the bastion"),
        (503, "no active endpoint for entity 9128"),
        (500, "csr sign failed: boom"),
        (409, "bastion pubkey already enrolled"),
        # This one leaves an inert orphan bastion_peers row (adapter.py:5839 runs
        # before the check at :5886). The token is still re-armed — see the module
        # docstring for why that is the wanted behaviour, not an oversight.
        (503, "host has no pod-Hub identity; bastion enrolment requires the Hub sidecar"),
    ],
)
def test_token_survives_every_control_plane_failure(stub, code, detail):
    """Each of these once destroyed the token. The user must be able to retry."""
    async def failing_bridge(op, params):
        raise HTTPException(code, detail)
    stub.setattr(bastion_enroll, "_bridge_op", failing_bridge)

    pool = Pool()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_redeem(pool))

    assert ei.value.status_code == code, "the original error must reach the client"
    assert ei.value.detail == detail
    assert pool.used is False, "token was burned by a failure the user could retry"
    assert pool.rearm_args == [(EXPECTED_HASH,)], "re-arm must name THIS token"
    assert pool.bastion_rows == 0


def test_token_survives_a_malformed_bridge_reply(stub):
    """The 502 'omitted <field>' path is inside the guarded block too."""
    async def short_bridge(op, params):
        return {k: v for k, v in COMPLETE_ENROLL.items() if k != "ca_crt"}
    stub.setattr(bastion_enroll, "_bridge_op", short_bridge)

    pool = Pool()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_redeem(pool))
    assert ei.value.status_code == 502
    assert "ca_crt" in ei.value.detail
    assert pool.used is False


def test_rearmed_token_can_actually_be_redeemed_again(stub):
    """The point of the fix: retry succeeds once the control plane recovers.

    Asserted end to end rather than by inspecting used_at, because 're-armed'
    only means anything if the SAME token spends on the next attempt.
    """
    calls = {"n": 0}

    async def flaky_bridge(op, params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPException(503, "bridge not connected — cannot enroll the bastion")
        return dict(COMPLETE_ENROLL)
    stub.setattr(bastion_enroll, "_bridge_op", flaky_bridge)

    pool = Pool()
    with pytest.raises(HTTPException):
        asyncio.run(_redeem(pool))

    out = asyncio.run(_redeem(pool))          # same token, second attempt
    assert out["operator_crt"] == "crt"
    assert out["bearer"]
    assert pool.bastion_rows == 1
    assert pool.used is True, "a SUCCESSFUL redeem must still consume the token"


# ── the boundaries: do not re-arm what was really spent ──────────────────────

def test_successful_enrolment_does_not_rearm(stub):
    """Once a bastion exists the token is genuinely spent; re-arming it would
    hand out a second enrolment on one credential."""
    async def ok_bridge(op, params):
        return dict(COMPLETE_ENROLL)
    stub.setattr(bastion_enroll, "_bridge_op", ok_bridge)

    pool = Pool()
    asyncio.run(_redeem(pool))
    assert pool.rearm_calls == 0
    assert pool.used is True


def test_an_already_used_token_is_still_refused(stub):
    """The re-arm must not weaken single-use: a token spent by a SUCCESSFUL
    enrolment stays spent."""
    async def ok_bridge(op, params):
        return dict(COMPLETE_ENROLL)
    stub.setattr(bastion_enroll, "_bridge_op", ok_bridge)

    pool = Pool()
    asyncio.run(_redeem(pool))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_redeem(pool))
    assert ei.value.status_code == 401


def test_a_failing_rearm_never_masks_the_original_error(stub):
    """If the re-arm UPDATE itself dies, the client must still be told what
    actually went wrong -- not 'pg gone at the worst moment'."""
    async def failing_bridge(op, params):
        raise HTTPException(503, "bridge not connected — cannot enroll the bastion")
    stub.setattr(bastion_enroll, "_bridge_op", failing_bridge)

    pool = Pool(rearm_raises=True)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_redeem(pool))
    assert ei.value.status_code == 503
    assert "bridge not connected" in ei.value.detail
    assert pool.rearm_calls == 1


# ── the log must not claim a re-arm that did not happen ─────────────────────

class _StatusPool:
    """Returns a chosen asyncpg command tag from execute()."""

    def __init__(self, status):
        self.status = status

    async def execute(self, sql, *args):
        return self.status


def test_log_says_not_rearmed_when_the_token_had_expired(caplog):
    """`UPDATE 0` means the row was gone/expired. Logging 're-armed' there sends
    the next reader looking for a bug in the wrong place."""
    caplog.set_level("INFO")
    asyncio.run(bastion_enroll._rearm_pairing_token(_StatusPool("UPDATE 0"), EXPECTED_HASH))
    assert "NOT re-armed" in caplog.text


def test_log_says_rearmed_when_a_row_was_updated(caplog):
    caplog.set_level("INFO")
    asyncio.run(bastion_enroll._rearm_pairing_token(_StatusPool("UPDATE 1"), EXPECTED_HASH))
    assert "re-armed after failed enrolment" in caplog.text
