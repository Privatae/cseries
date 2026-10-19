#!/usr/bin/env python3
"""The anchor column can go back to NONE — against a REAL Postgres.

Why a real database: the whole behaviour is one CASE inside an ON CONFLICT
upsert (topology.py). A stub pool would assert my reading of the SQL rather than
the SQL, which is the failure this gate exists to prevent.

WHAT WAS WRONG. Every column on that upsert is COALESCEd so a beat from an older
bastion cannot retract what a newer one reported. Right for `peers`,
`owner_claim`, `prep_tools`, `other_bastions` — for all of them "absent" means
only "this build does not know about the field".

For `anchor` it means two different things. The box omits the key when it is too
old to know about anchoring AND when it is simply not anchored (the gateway
sends it only for a verified claim). So a box that loses anchor.json, or whose
claim stops verifying, went on reading `anchored` on this side for ever, and
nothing here could tell. Found in review, 2026-09-12.

`mode` is the discriminator and it already exists: a bastion that knows about
anchoring sends mode on EVERY beat, so a beat carrying mode is authoritative
about its anchor too — including saying it has none.

This matters more from bastion 0.24.0, where a networked box with no anchor
serves no lane at all. "Unanchored" stopped being a cosmetic column and became
the difference between a machine that works and one that refuses everything.

THE SCHEMA IS THE KERNEL'S (2026-09-30). This gate used to CREATE a hand copy
of network_bastion_heartbeat ("the shape a pod carries, measured 2026-09-15")
and of mcp_client_tokens. v146 had added heartbeat.detect, the upsert writes
it, and the gate failed on main with "column detect does not exist" — correct
code, stale copy (its mcp_client_tokens copy had drifted too: JSONB scopes, a
nullable url_handle). It now runs on a fresh database carrying the schema the
kernel builds — scripts/kernel_schema.py runs entrypoint.sh's own schema section:
init-db.sql, v139, then EVERY migration in order — on the server PEER_TEST_DSN
names, so the next migration is in this gate the day it lands.

Run:  python3 scripts/test_heartbeat_anchor_clears.py
"""
import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

import asyncpg  # noqa: E402

DSN = os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not DSN:
    print(f"SKIP — no test database. Start one:  bash {os.path.join(_ROOT, 'scripts/peer_test_db.sh')}")
    sys.exit(2)

from server.network import topology  # noqa: E402
from kernel_schema import kernel_schema_db  # noqa: E402

TOKEN = 90001
ENTITY = 1
FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


ANCHOR = {
    "owner": "Acme IT",
    "anchor_host": "acme.host.example.test",
    "anchor_handle": "acme",
    "claimed_at": "2026-09-12T22:28:53Z",
    "public_key": "GflCZwbMdc6+FKDfySaEboSfRb9pn1dX71LFiHl6Wf0=",
    "signature": "Yg" + "A" * 86,
}


async def main(dsn: str) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    # network_bastion_heartbeat and mcp_client_tokens are the KERNEL's tables
    # here (kernel_schema_db below), never a copy: every column the upsert
    # writes exists because the migrations that add it ran.
    #
    # upsert_heartbeat refuses a token that is not an ACTIVE BASTION of this
    # entity ("the bearer is not authority to write arbitrary rows"), so the gate
    # stands up a real one rather than passing an id and hoping.
    await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = $1", TOKEN)
    await pool.execute(
        "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
        "VALUES ($1, $2, 'gate', '\\x00'::bytea, 'anchor-gate', 'bastion')", TOKEN, ENTITY)

    async def beat(**payload):
        payload["token_id"] = TOKEN
        got = await topology.upsert_heartbeat(pool, ENTITY, payload)
        if got is None:
            raise SystemExit("upsert_heartbeat refused the gate's own token — "
                             "the fixture is wrong, not the code under test")
        return got

    async def stored():
        r = await pool.fetchrow(
            "SELECT mode, anchor, peers FROM network_bastion_heartbeat WHERE token_id = $1", TOKEN)
        if r is None:
            raise SystemExit("no heartbeat row after a beat — fixture broken")
        return r

    # 1. an anchor-aware box reports an anchor -> stored
    await beat(hostname="box", mode="networked", anchor=ANCHOR)
    r = await stored()
    check(r["anchor"] is not None, "an anchor-aware box's anchor is stored", f"got {r['anchor']!r}")

    # 2. THE FIX: the same box beats again with NO anchor -> cleared. It carries
    #    mode, so it knows about anchoring, and absent therefore means unanchored.
    await beat(hostname="box", mode="networked")
    r = await stored()
    check(r["anchor"] is None,
          "a mode-carrying beat with no anchor CLEARS it (the box says it is unanchored)",
          f"got {r['anchor']!r}")
    check(r["mode"] == "networked", "and mode is untouched", f"got {r['mode']!r}")

    # 3. an OLD bastion (no mode at all) must not retract a real anchor
    await beat(hostname="box", mode="networked", anchor=ANCHOR)
    check((await stored())["anchor"] is not None, "re-anchored for the next case")
    await beat(hostname="box")                      # no mode, no anchor: old build
    r = await stored()
    check(r["anchor"] is not None,
          "a beat with NO mode keeps the stored anchor (too old to know is not unanchored)",
          f"got {r['anchor']!r}")
    check(r["mode"] == "networked", "and the stored mode survives an old beat too")

    # 4. a local box: mode rides, anchor stays absent, nothing breaks
    await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = $1", TOKEN)
    await beat(hostname="box", mode="local")
    r = await stored()
    check(r["mode"] == "local" and r["anchor"] is None,
          "a local box stores mode with no anchor", f"mode={r['mode']!r} anchor={r['anchor']!r}")

    # 5. the OTHER COALESCEd columns must be untouched by this change. An old
    #    beat still must not retract peers — that is the regression this fix
    #    could plausibly cause, so it is checked rather than assumed.
    await beat(hostname="box", mode="local", peers=[{"host_name": "a.host.example.test"}])
    before = (await stored())["peers"]
    check(before not in (None, "null", "[]"), "peers stored for the retraction check", f"got {before!r}")
    await beat(hostname="box")
    after = (await stored())["peers"]
    check(after == before,
          "an old beat still does not retract peers (the COALESCE rule is unchanged elsewhere)",
          f"before={before!r} after={after!r}")

    await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = $1", TOKEN)
    await pool.close()
    return 0


with kernel_schema_db(DSN, entity_id=ENTITY) as _dsn:
    rc = asyncio.run(main(_dsn))
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"anchor clears correctly: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
