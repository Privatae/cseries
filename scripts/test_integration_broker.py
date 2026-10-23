#!/usr/bin/env python3
"""Gate: the integration broker — where a call may go, and who may make it.

Container-free. Two things decide whether handing an app a provider connection
is safe, and both are pure functions of the request:

  * WHERE the call lands. The app names a PATH, never a URL. If that path can
    be made to resolve off the provider's own origin, the user's access token
    goes to whoever the app chose — the whole point of brokering it is lost.
    urljoin ALONE does not give this: it replaces the base for an absolute URL
    and swaps the host for a protocol-relative one, which is exactly the bug
    this pins.
  * WHO may call at all. A grant is per (connection, app) and absence must mean
    REFUSE. A default-allow here would let any app holding use:integration read
    the mailbox the user connected for their mail client. That half needs the
    store, which lives in the Host's Postgres: scripts/test_integrations_store_pg.py.

Run:  python3 scripts/test_integration_broker.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    from fastapi import HTTPException
    from server.routes.integrations import _resolve_under

    BASE = "https://gmail.googleapis.com/gmail/v1"

    def allowed(path: str) -> str | None:
        try:
            return _resolve_under(BASE, path)
        except HTTPException:
            return None

    # ── where a call may land ──────────────────────────────────────────
    check("a plain relative path resolves under the base",
          allowed("users/me/messages") == f"{BASE}/users/me/messages",
          f"got={allowed('users/me/messages')}")
    check("a leading slash is the same path, not a root escape",
          allowed("/users/me/messages") == f"{BASE}/users/me/messages",
          f"got={allowed('/users/me/messages')}")
    check("query-ish and id-bearing paths survive",
          allowed("users/me/messages/18f2c") == f"{BASE}/users/me/messages/18f2c")

    # The four ways out, each of which would leak the access token.
    check("an ABSOLUTE url is refused", allowed("https://evil.test/steal") is None)
    check("a PROTOCOL-RELATIVE url is refused (keeps scheme, swaps host)",
          allowed("//evil.test/steal") is None)
    check("dot-dot traversal off the base path is refused",
          allowed("../../../oauth2/v4/token") is None)
    check("traversal hidden mid-path is refused",
          allowed("users/../../../admin") is None)
    check("a different scheme is refused", allowed("http://gmail.googleapis.com/x") is None)

    # Same-origin but OUTSIDE the declared base path is still refused: the
    # manifest named gmail/v1, not the whole googleapis host.
    check("same host but outside the declared base path is refused",
          allowed("/../oauth2/v4/token") is None,
          f"got={allowed('/../oauth2/v4/token')}")

    # ── who may call ───────────────────────────────────────────────────
    # The grant rules (absence refuses, per app and per connection, cascade on
    # disconnect, method narrowing) are pinned against a real Postgres since the
    # store moved there (2026-10-03): scripts/test_integrations_store_pg.py, G1.

    # ── the scope is inert on its own ──────────────────────────────────
    from server.applications import scopes as sc
    from server.applications import build_contract as bc
    check("use:integration is a real declarable scope",
          "use:integration" in sc.PERMISSIONS)
    check("write:inbox_delivery is a real declarable scope",
          "write:inbox_delivery" in sc.PERMISSIONS)
    check("every community-allowed scope is a real one (no typo can sneak in)",
          bc.COMMUNITY_ALLOWED_SCOPES <= sc.PERMISSIONS,
          f"unknown={sorted(bc.COMMUNITY_ALLOWED_SCOPES - sc.PERMISSIONS)}")
    check("a community app MAY declare use:integration (it is inert without a grant)",
          "use:integration" in bc.COMMUNITY_ALLOWED_SCOPES)
    check("a community app may NOT declare write:portal_sources (it creates connections)",
          "write:portal_sources" not in bc.COMMUNITY_ALLOWED_SCOPES)
    check("a community app may NOT declare write:inbox (it can forge a sender)",
          "write:inbox" not in bc.COMMUNITY_ALLOWED_SCOPES)

    # ── the delivery payload cannot carry reserved keys ────────────────
    #
    # REGRESSION TEST for a privilege escalation found in review. /deliver used
    # to merge the app's payload into the inbox row's payload, and
    # handle_inbox_append reads keys back OUT of that dict and turns them into
    # rows: `contact_slip` -> held_slips plus a contacts row via
    # upsert_contact_from_grant (status forced to 'active'), and
    # `from_display_name` -> the dashboard broadcast. An app holding only
    # write:inbox_delivery could therefore plant a contact under a handle and
    # display name of its choosing — the exact thing the scope's own rationale
    # promises is impossible.
    import asyncio
    from server.routes import inbox as inbox_routes

    captured: dict = {}

    async def fake_append(payload):
        captured.clear()
        captured.update(payload)
        return {"ok": True, "message_id": "00000000-0000-0000-0000-000000000000",
                "duplicate": False}

    class _Tok:
        app_id = "mail-sorter"

    class _State:
        app_token = _Tok()

    class _Req:
        state = _State()

    real_append = inbox_routes.handle_inbox_append
    real_entity = inbox_routes._entity_id
    inbox_routes.handle_inbox_append = fake_append
    inbox_routes._entity_id = lambda: 9003
    try:
        hostile = inbox_routes.InboxDeliverRequest(
            subject="Invoice", body="…", idempotency_key="r7:a1:m42",
            payload={
                "contact_slip": {"contract_id": "forged-1", "terms": {}},
                "from_handle": "privatae-support",
                "from_display_name": "Privatae Support",
                "delivered_by_app": "some-other-app",
                "rule": "Invoices",
            },
        )
        asyncio.run(inbox_routes.deliver_message(hostile, _Req()))
    finally:
        inbox_routes.handle_inbox_append = real_append
        inbox_routes._entity_id = real_entity

    p = captured.get("payload") or {}
    check("the app's payload is NESTED, never merged",
          set(p) == {"delivered_by_app", "idempotency_key", "app"}, f"top-level={sorted(p)}")
    check("contact_slip cannot reach the handler's harvest",
          "contact_slip" not in p, f"payload={p}")
    check("from_display_name cannot reach the dashboard broadcast",
          "from_display_name" not in p)
    check("an app cannot overwrite its own stamped provenance",
          p.get("delivered_by_app") == "mail-sorter")
    check("the app's own content survives underneath",
          (p.get("app") or {}).get("rule") == "Invoices")
    check("the delivery is a self-send at the GATE, not just in the row",
          captured.get("from_entity_id") == 9003 and captured.get("message_kind") == "app_delivery",
          f"from_entity_id={captured.get('from_entity_id')}")
    check("the sender names the app, not the platform",
          captured.get("from_address") == "app:mail-sorter@host",
          f"from_address={captured.get('from_address')}")

    # The handler-side half, tested by BEHAVIOUR rather than by grepping the
    # source. A source-text assertion passes if someone appends `or True` and
    # fails on a reformat, so it measures the wrong thing. This drives the real
    # handle_inbox_append with a recording pool and asserts on whether the
    # held_slips write actually happens, per kind.
    from server.bridge import memory_handlers as mh

    class _RecordingPool:
        def __init__(self):
            self.writes: list[str] = []

        async def fetchval(self, sql, *a):
            if "FROM contacts WHERE peer_entity_id" in " ".join(sql.split()):
                # memory_handlers._grant_admissible: a permission_grant is kept
                # only from a pending/active contact. Answered on purpose, so
                # this test exercises the storing path it is about rather than
                # passing the gate because every fetchval returns a value.
                return 1
            self.writes.append(sql.strip().split()[0].upper() + ":inbox_messages")
            return "00000000-0000-0000-0000-000000000001"

        async def execute(self, sql, *a):
            if "held_slips" in sql:
                self.writes.append("held_slips")
            return "INSERT 0 1"

        async def fetchrow(self, sql, *a):
            return None

        async def fetch(self, sql, *a):
            return []

    def _slip_written_for(kind: str) -> bool:
        pool = _RecordingPool()
        real_pool, real_eid = mh.brain_service.get_pool, mh._entity_id
        mh.brain_service.get_pool = lambda: pool
        mh._entity_id = lambda: 9003
        try:
            asyncio.run(mh.handle_inbox_append({
                "subject": "x", "body": "y", "message_kind": kind,
                "payload": {"contact_slip": {"contract_id": "c1", "terms": {}},
                            "from_handle": "privatae-support",
                            "from_display_name": "Privatae Support"},
            }))
        except Exception:
            pass
        finally:
            mh.brain_service.get_pool = real_pool
            mh._entity_id = real_eid
        return "held_slips" in pool.writes

    check("kind='app_delivery' does NOT reach the slip harvest",
          not _slip_written_for("app_delivery"))
    check("kind='message' does NOT reach the slip harvest",
          not _slip_written_for("message"))
    check("kind='system_notification' does NOT reach the slip harvest",
          not _slip_written_for("system_notification"))
    # The control: if these were also refused the test would prove nothing.
    check("kind='endpoint_grant' STILL harvests (the real grant flow works)",
          _slip_written_for("endpoint_grant"))
    check("kind='permission_grant' STILL harvests (the reciprocal works)",
          _slip_written_for("permission_grant"))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
