"""The one answer the apps gate their platform-only controls on
(server/bridge/platform_reach.py), and the launcher's use of it.

These pin:

  1. a Host with no passport is not a member and can send no op;
  2. an active member (a passport that has not expired, not frozen, no
     refusal from the door) is a member, and its ops are its client's own
     list; a dropped connection alone keeps it a member;
  3. a member whose membership lapsed (expired, frozen, any refusal reason
     the door gives) reads as not a member, with no ops: its controls are
     hidden, never shown as needing membership;
  4. a client with no op list, and an answer that cannot be read, give no
     ops: a failure never shows a control;
  5. today's member door carries none of the ops the gated controls need, so
     a member Host shows none of them yet either;
  6. GET /api/applications leaves out an app that does nothing without the
     platform unless this Host can send its op, and keeps every other app;
  7. GET /api/system/status carries the answer as "network".

Container-free: python3 -m pytest tests/test_platform_reach.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

from server.bridge import platform_reach  # noqa: E402
from server.bridge.member_client import NETWORK_OPS, REFUSAL_REASONS  # noqa: E402

# The ops the system apps gate on (dashboard/base/js/network-reach.js callers).
GATED_OPS = {"bastion.rollout", "pc.update_apply", "design.publish", "gitea.user_token",
             "webapp.publish_bundle"}

NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)


class Passport:
    def __init__(self, not_after):
        self.not_after = not_after


class Membership:
    """Stands in for server/bridge/membership.Membership: the four things
    membership_active reads."""

    def __init__(self, passport=None, frozen=False, reason=None):
        self.passport, self.frozen, self.reason = passport, frozen, reason

    def _expired(self, p):
        return NOW >= p.not_after


class Door:
    offered_ops = NETWORK_OPS


class Cloud:
    """A client that lists no ops."""


def valid():
    return Passport(NOW + dt.timedelta(days=30))


def test_1_no_passport_is_not_a_member():
    assert platform_reach.reach(Membership(), Door()) == {"member": False, "ops": []}


def test_2_an_active_member_has_its_clients_ops():
    r = platform_reach.reach(Membership(valid()), Door())
    assert r["member"] is True
    assert r["ops"] == sorted(NETWORK_OPS)


@pytest.mark.parametrize("m", [
    Membership(Passport(NOW - dt.timedelta(seconds=1))),
    Membership(valid(), frozen=True),
    *[Membership(valid(), reason=why) for why in sorted(REFUSAL_REASONS)],
], ids=["expired", "frozen", *sorted(REFUSAL_REASONS)])
def test_3_a_lapsed_member_reads_as_not_a_member(m):
    assert platform_reach.reach(m, Door()) == {"member": False, "ops": []}


def test_4_no_op_list_and_unreadable_give_no_ops():
    assert platform_reach.reach(Membership(valid()), Cloud()) == {"member": True, "ops": []}

    class Broken(Membership):
        def _expired(self, p):
            raise RuntimeError("unreadable")
    assert platform_reach.reach(Broken(valid()), Door()) == {"member": False, "ops": []}


def test_5_todays_member_door_carries_no_gated_op():
    r = platform_reach.reach(Membership(valid()), Door())
    assert not GATED_OPS & set(r["ops"])
    assert platform_reach.APP_NEEDS_OP["sites"] not in r["ops"]


def test_6_launcher_leaves_out_platform_apps(monkeypatch):
    from server.routes import applications as apps_route

    class App:
        def __init__(self, i):
            self.id = i

    ids = ["settings", "sites", "community", "publisher", "network", "designs"]
    monkeypatch.setattr(apps_route.brain_service, "get_pool", lambda: object())

    async def list_active(pool):
        return [App(i) for i in ids]
    monkeypatch.setattr(apps_route.registry, "list_active", list_active)
    monkeypatch.setattr(apps_route, "_serialize", lambda a, keys: {"id": a.id})
    from server.applications import app_origin
    monkeypatch.setattr(app_origin, "keys_for", lambda apps: {})

    def listed(reach):
        monkeypatch.setattr(platform_reach, "reach", lambda: reach)
        return [a["id"] for a in asyncio.run(apps_route.list_apps())["applications"]]

    assert listed({"member": False, "ops": []}) == ["settings", "network", "designs"]
    # A member today: the board and store publishing ride the member door; sites do not.
    assert listed({"member": True, "ops": sorted(NETWORK_OPS)}) == [
        "settings", "community", "publisher", "network", "designs"]
    assert listed({"member": True, "ops": ["webapp.publish_bundle", "community.threads",
                                           "appstore.publish_bundle"]}) == ids
    # An op list without membership shows nothing.
    assert listed({"member": False, "ops": ["webapp.publish_bundle"]}) == [
        "settings", "network", "designs"]


def test_7_system_status_carries_the_answer(monkeypatch):
    # A stand-in server.main only for this test (status() reads nothing from it
    # here): left in sys.modules it would shadow the real one for every later
    # test in the run (tests/test_undefined_logger_except_branches.py).
    if "server.main" not in sys.modules:
        monkeypatch.setitem(sys.modules, "server.main", types.ModuleType("server.main"))
    from server.routes import system
    monkeypatch.setattr(platform_reach, "reach", lambda: {"member": False, "ops": []})
    out = asyncio.run(system.status())
    assert out["network"] == {"member": False, "ops": []}
