#!/usr/bin/env python3
"""A connection request right after the anchor approves a pairing.

The anchor approves a second Host on its machine; its own latest heartbeat from
that machine, a second older, still names the Host as pending; its connection
request to that Host twenty seconds later used to be refused "that host is not
on a machine you share"; and the next beat, a minute after the one before,
reports the pairing active. The box beats on a 60-second ticker and an approval
triggers no beat, so for up to a minute a person who had just approved a
pairing was told something false.

The fix reads what the box confirmed to this Host after the beat: the pairing
grant the anchor signed, which the box accepted (pairing_grant_sync exited 0,
which it does only after writing the pairing active). This drives that path for
real: the approval through pairing_grants.sign_and_push_grant and the revoke
through pairing_grants.revoke_pairing, with only the box's answer, the signing
key and the audit row stubbed, and the request through the real
POST /api/contacts/request-peer route. The heartbeat row is written directly at
a chosen time, because the time of the beat against the time of the approval is
the property under test.

What must stay refused, because the request is the one thing a peering
authorizes: an approval the box has not confirmed, a confirmation older than the
latest beat, an expired or revoked grant, a grant for another machine, a pairing
the box reports revoked, one the box no longer names, and one this Host removed
after the beat. A refusal names its real reason.

Every name, number and host below is invented.

  PYTHONPATH=brain python3 scripts/test_peer_request_after_approval.py

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else
a throwaway cseries-postgres through docker.
"""
import asyncio
import base64
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove list_peerings' SQL.")
    sys.exit(2)

ME = 9041                       # the anchor Host
TOKEN, OTHER_TOKEN = 7, 8
MACHINE, OTHER_MACHINE = "box-example-7", "box-example-8"
ANCHOR = "anchor9041.hosts.example.test"

# Each Host on the machine, its entity id, and the state the beat reports for it.
HOSTS = {
    "window":  (9042, "pending"),   # just approved, confirmed by the box
    "offline": (9043, "pending"),   # approved, the box never confirmed it
    "expired": (9044, "pending"),   # confirmed, then the grant expired
    "elsewhere": (9045, "pending"),  # the grant is for another machine
    "revokedbeat": (9046, "revoked"),  # the box reports it removed
    "dormant": (9047, None),        # the box no longer names it
    "removed": (9048, "active"),    # this Host removed it after the beat
    "waiting": (9049, "pending"),   # nobody approved it
    "refusedgrant": (9050, "pending"),  # approved; the box refused the grant
    "reapproved": (9051, "active"),  # active; re-approved while the box is offline
}
STRANGER = 9999


def host_name(key: str) -> str:
    return f"{key}.hosts.example.test"


FAILS = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(label)


def beat_peers(overrides=None):
    """The peers array the box sends on its beat (topology.upsert_heartbeat's
    narrowed shape); the dormant Host is not named."""
    out = []
    for key, (_eid, state) in HOSTS.items():
        state = (overrides or {}).get(key, state)
        if key == "dormant":
            continue
        out.append({"host_name": host_name(key), "handle": key, "state": state})
    return out


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    async with pool.acquire() as c:
        for tid, name in ((TOKEN, "desk"), (OTHER_TOKEN, "lab")):
            await c.execute(
                "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, "
                "operator_kind) VALUES ($1, $2, $3, $4, $5, 'bastion')",
                tid, ME, name, bytes([tid]), f"{name}-{tid}")
        # THE BEAT BEFORE THE APPROVAL: one second old.
        await c.execute(
            "INSERT INTO network_bastion_heartbeat (token_id, hostname, peers, anchor, last_seen_at) "
            "VALUES ($1, $2, $3::jsonb, $4::jsonb, now() - interval '1 second')",
            TOKEN, MACHINE, json.dumps(beat_peers()), json.dumps({"anchor_host": ANCHOR}))
        await c.execute(
            "INSERT INTO network_bastion_heartbeat (token_id, hostname, peers, anchor, last_seen_at) "
            "VALUES ($1, $2, '[]'::jsonb, $3::jsonb, now() - interval '1 second')",
            OTHER_TOKEN, OTHER_MACHINE, json.dumps({"anchor_host": ANCHOR}))
        for key, (eid, _state) in HOSTS.items():
            await c.execute(
                "INSERT INTO network_bastion_peer (token_id, peer_host_name, peer_handle, peer_entity_id) "
                "VALUES ($1, $2, $3, $4)", TOKEN, host_name(key), key, eid)

    _stub = types.ModuleType("server.main"); _stub.app = None
    _stub.get_bridge_client = lambda: None
    sys.modules.setdefault("server.main", _stub)

    from server import brain_service as real_bs
    real_bs._entity_id = ME
    real_bs._pool = pool
    import server.routes.contacts as contacts
    from server.network import pairing_grants as pg, peer_discovery
    import server.network.nac_mgmt as nac_mgmt
    import server.peer.peer_client as PCM

    class Svc:
        _entity_id = ME
        @staticmethod
        def get_pool(): return pool
    contacts.brain_service = Svc()

    knocks, bridged = [], []

    async def ok_knock(peer_entity_id, params, **kw):
        knocks.append((peer_entity_id, params)); return {"ok": True, "data": {"message_id": "m-1"}}

    async def fake_bridge(op, params):
        bridged.append((op, params)); return {"message_id": "m-2"}
    PCM.knock_peer = ok_knock
    contacts._bridge_op = fake_bridge

    # The box and the key. The box's answer to pairing_grant_sync and
    # pairing_grant_drop is the one thing outside this pod the approval needs.
    box = {"answer": {"exit": 0, "stdout": '{"state":"ok"}'}}
    dispatched = []

    async def fake_dispatch(pool_, eid, server, op, args, timeout=None):
        dispatched.append((server, op)); return dict(box["answer"])

    async def fake_sign(pool_, eid, terms):
        return base64.b64encode(pg.canonical(terms)).decode(), "c2lnbmF0dXJl"

    async def fake_verify_key(pool_, eid):
        return "dmVyaWZ5LWtleQ=="

    async def no_audit(*a, **kw):
        return None
    pg._dispatch, pg._sign, pg._verify_pubkey_b64 = fake_dispatch, fake_sign, fake_verify_key
    nac_mgmt.audit_mgmt = no_audit

    def row_for(token_id, machine):
        return {"token_id": token_id, "hostname": machine, "anchor": {"anchor_host": ANCHOR},
                "pairings": None, "unapproved_pairing_attempts": None, "gateway_version": "0.40.0"}

    async def approve(key, *, machine=MACHINE, token_id=TOKEN, delivered=True, refused=False):
        box["answer"] = ({"exit": 126, "stderr": "grant does not verify: invented reason"} if refused
                         else {"exit": 0, "stdout": '{"state":"ok"}'} if delivered
                         else {"exit": None, "_offline": True, "stderr": "machine not connected"})
        return await pg.sign_and_push_grant(
            pool, ME, machine, host_name(key), key, ["observe"], [],
            row=row_for(token_id, machine), approved_by="person@example.test")

    async def anchored_here(pool_, eid, server):
        return row_for(TOKEN, server)
    pg._anchored_here = anchored_here

    from fastapi import FastAPI
    import httpx
    app = FastAPI()
    app.include_router(contacts.router)
    for r in contacts.router.routes:
        for d in getattr(getattr(r, "dependant", None), "dependencies", []) or []:
            if d.call is not None:
                app.dependency_overrides[d.call] = lambda: None

    def by_entity(listed):
        return {p["peer_entity_id"]: p for p in listed}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async def request(eid):
            before = len(knocks) + len(bridged)
            r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": eid})
            detail = ""
            try:
                detail = r.json().get("detail") or ""
            except Exception:
                pass
            return r.status_code, detail, len(knocks) + len(bridged) - before

        print("\n── 1. before the approval: the box reports the Host pending ──")
        win = HOSTS["window"][0]
        code, detail, n = await request(win)
        check(f"a pending pairing nobody approved yet is refused 409 (got {code})", code == 409, detail)
        # Nobody has approved it, so nothing here may promise it works soon
        # (an earlier sentence said "try again in a minute").
        check("...and the person reads that the machine reports it waiting for approval, "
              "with no promise of a time and not that the Host shares no machine",
              "reports that host's pairing as waiting for approval" in detail
              and "minute" not in detail and "not on a machine you share" not in detail, detail)
        check("...and nothing was sent", n == 0, n)

        print("\n── 2. THE WINDOW: approved and confirmed by the box, before its next beat ──")
        res = await approve("window")
        check("the approval was delivered and the box confirmed it",
              res.get("delivered") is True and res.get("state") == "active", res)
        g = await pool.fetchrow(
            "SELECT g.synced_at > h.last_seen_at AS after_beat FROM nac_pairing_grants g "
            "JOIN network_bastion_heartbeat h ON h.hostname = g.server "
            "WHERE g.subject = $1 AND g.revoked_at IS NULL", host_name("window"))
        check("the box's confirmation is newer than the latest beat (the 60-second window)",
              g is not None and g["after_beat"] is True, dict(g) if g else None)
        beat_state = await pool.fetchval(
            "SELECT e->>'state' FROM network_bastion_heartbeat h, jsonb_array_elements(h.peers) e "
            "WHERE h.token_id = $1 AND e->>'host_name' = $2", TOKEN, host_name("window"))
        check("the latest beat still reports the pairing pending", beat_state == "pending", beat_state)
        code, detail, n = await request(win)
        check(f"the request right after the approval is accepted (got {code})", code == 200, detail)
        check("...and the connection request went out", n == 1, n)
        p = by_entity(await peer_discovery.list_peerings(pool, ME)).get(win) or {}
        check("list_peerings keeps the box's word (pending) and says the box confirmed the "
              "approval since, so the peering is requestable",
              p.get("state") == "pending" and p.get("confirmed_at") and p.get("requestable") is True,
              {k: p.get(k) for k in ("state", "confirmed_at", "requestable")})

        print("\n── 3. what stays refused inside the same window ──")
        await approve("offline", delivered=False)
        code, detail, n = await request(HOSTS["offline"][0])
        check(f"an approval the box has NOT confirmed is refused 409 (got {code})", code == 409, detail)
        check("...saying this Host approved it and the machine hasn't confirmed the approval",
              "you approved that host's pairing on" in detail
              and "hasn't confirmed the approval yet" in detail and "minute" not in detail, detail)
        check("...and nothing was sent", n == 0, n)

        # The box refused the grant. The heartbeat retry never resends refused
        # bytes, so waiting does not help and the sentence must not say it does.
        await approve("refusedgrant", refused=True)
        code, detail, n = await request(HOSTS["refusedgrant"][0])
        check(f"an approval the box refused is refused 404 (got {code})", code == 404 and n == 0, detail)
        check("...saying the machine refused this Host's approval",
              "refused your approval of that host's pairing" in detail
              and "hasn't confirmed" not in detail, detail)

        await approve("expired")
        async with pool.acquire() as c:
            await c.execute("UPDATE nac_pairing_grants SET expires_at = now() - interval '1 second' "
                            "WHERE subject = $1 AND revoked_at IS NULL", host_name("expired"))
        code, detail, n = await request(HOSTS["expired"][0])
        check(f"a confirmed grant that has expired is refused (got {code})", code == 409 and n == 0, detail)
        check("...and an expired approval is not described as one waiting on the machine",
              "waiting for approval" in detail and "you approved" not in detail, detail)

        # A grant on ANOTHER machine, for the same Host, says nothing about
        # this machine's pairing.
        await approve("elsewhere", machine=OTHER_MACHINE, token_id=OTHER_TOKEN)
        code, detail, n = await request(HOSTS["elsewhere"][0])
        check(f"a confirmed grant for another machine does not lift this one (got {code})",
              code == 409 and n == 0, detail)

        # The box refuses a grant for a revoked pairing; even a confirmation on
        # record does not make a pairing the beat reports revoked usable.
        await approve("revokedbeat")
        code, detail, n = await request(HOSTS["revokedbeat"][0])
        check(f"a pairing the box reports revoked stays refused 404 (got {code})",
              code == 404 and n == 0, detail)
        check("...saying the machine reports it removed", "reports that host's pairing as removed" in detail,
              detail)

        await approve("dormant")
        code, detail, n = await request(HOSTS["dormant"][0])
        check(f"a Host the box no longer names stays refused 404 (got {code})", code == 404 and n == 0, detail)
        check("...saying the machine stopped reporting it",
              "no longer on a machine you share" in detail and MACHINE in detail, detail)

        # The mirror of the window: the beat still says active, and this Host
        # removed the pairing a moment ago. Its grant dates from long before
        # the beat, as an active pairing's does.
        await approve("removed")
        async with pool.acquire() as c:
            await c.execute("UPDATE nac_pairing_grants SET issued_at = now() - interval '1 hour', "
                            "synced_at = now() - interval '1 hour' WHERE subject = $1", host_name("removed"))
        code, detail, n = await request(HOSTS["removed"][0])
        check(f"a pairing the beat reports active is requestable before any change (got {code})",
              code == 200 and n == 1, detail)
        res = await pg.revoke_pairing(pool, ME, MACHINE, host_name("removed"))
        check("the revoke ran against the box", res.get("revoked") is True and res.get("had_grant") is True,
              res)
        code, detail, n = await request(HOSTS["removed"][0])
        check(f"a pairing this Host removed after the beat is refused 404 (got {code})",
              code == 404 and n == 0, detail)
        # "revoked on", not "removed from": with the box offline the machine
        # still holds the pairing (revoke_pairing's own note says so).
        check("...saying you revoked it", "you revoked that host's pairing on" in detail, detail)

        # A re-approval supersedes the old grant (revoked_at, no revoke terms).
        # That is not a removal: with the box offline, the beat's "active"
        # still stands.
        await approve("reapproved")
        async with pool.acquire() as c:
            await c.execute("UPDATE nac_pairing_grants SET issued_at = now() - interval '1 hour', "
                            "synced_at = now() - interval '1 hour' WHERE subject = $1",
                            host_name("reapproved"))
        await approve("reapproved", delivered=False)
        code, detail, n = await request(HOSTS["reapproved"][0])
        check(f"an active pairing re-approved while the box is offline stays requestable (got {code})",
              code == 200 and n == 1, detail)

        code, detail, n = await request(HOSTS["waiting"][0])
        check(f"a pending pairing nobody approved stays refused 409 (got {code})", code == 409 and n == 0,
              detail)

        code, detail, n = await request(STRANGER)
        check(f"a Host on no machine you share is refused 404 (got {code})", code == 404 and n == 0, detail)
        check("...with the not-shared sentence", "not on a machine you share" in detail, detail)

        print("\n── 4. the next beat speaks for itself ──")
        # A beat AFTER the confirmation that still reports pending (composed
        # before the box wrote it active): the newer word is the beat's, and
        # the confirmation no longer counts.
        async with pool.acquire() as c:
            await c.execute("UPDATE network_bastion_heartbeat SET last_seen_at = now(), peers = $2::jsonb "
                            "WHERE token_id = $1", TOKEN, json.dumps(beat_peers()))
        code, detail, n = await request(win)
        check(f"a beat newer than the confirmation that says pending is believed: refused 409 (got {code})",
              code == 409 and n == 0, detail)
        p = by_entity(await peer_discovery.list_peerings(pool, ME)).get(win) or {}
        check("...and list_peerings no longer counts the older confirmation",
              p.get("confirmed_at") is None and p.get("requestable") is False, p)
        async with pool.acquire() as c:
            await c.execute("UPDATE network_bastion_heartbeat SET last_seen_at = now(), peers = $2::jsonb "
                            "WHERE token_id = $1", TOKEN,
                            json.dumps(beat_peers({"window": "active", "removed": "revoked"})))
        code, detail, n = await request(win)
        check(f"the next beat reporting it active is accepted (got {code})", code == 200 and n == 1, detail)
        code, detail, n = await request(HOSTS["removed"][0])
        check(f"the beat after the revoke reports it removed: still refused (got {code})",
              code == 404 and n == 0 and "reports that host's pairing as removed" in detail, detail)

    await pool.close()
    print(f"\nFAILURES: {len(FAILS)}")
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=ME) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if not FAILS else f"RESULT: {len(FAILS)} FAILED")
sys.exit(rc)
