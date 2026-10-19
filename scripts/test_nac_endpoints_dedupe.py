#!/usr/bin/env python3
"""One posture card per machine, and only for a live enrolment — real Postgres.

nac_endpoints keys on token_id, and re-pairing a machine mints a NEW token. So
every enrolment a box ever had left its own posture row behind and /nac/endpoints
returned all of them. Measured on one Host, 2026-09-13: 33 rows for 27 machines,
one desktop holding FIVE, and six hostnames with no live enrolment at all —
including a server decommissioned 2026-08-11, still drawn as a card with a
working Quarantine button.

Why a real database: the whole fix is one DISTINCT ON + JOIN. A stub pool would
assert my reading of the SQL rather than the SQL.

Run:  python3 scripts/test_nac_endpoints_dedupe.py
"""
import asyncio, os, sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]
import asyncpg  # noqa: E402

DSN = os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not DSN:
    print(f"SKIP — no test database. Start one:  bash {os.path.join(_ROOT, 'scripts/peer_test_db.sh')}")
    sys.exit(2)

# THE ROUTE'S OWN QUERY, IMPORTED. A copy pasted here would make this gate a
# test of a snapshot: change the route's ORDER BY or its JOIN and every check
# below would go on passing about a query nobody runs. Importing it means a
# drift in the route is a drift in the test, which is the only version of this
# that is worth running.
from server.routes.network import NAC_ENDPOINTS_SQL as QUERY  # noqa: E402

FAILS, CHECKS = [], 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


async def main(dsn):
    # The KERNEL's nac_endpoints and mcp_client_tokens, on a fresh clone of the
    # schema the migrations build (scripts/kernel_schema.py). This gate used to
    # CREATE its own nac_endpoints and top up a SHARED mcp_client_tokens with
    # ALTER ... ADD COLUMN, because the other gates on the same database each
    # built a different copy and the result depended on which ran first
    # (measured 2026-09-13). A clone per gate ends both: no copies, no sharing.
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=3)

    async def tok(i, revoked=False, kind='bastion'):
        # The real table's required columns, token_hash and url_handle — both
        # unique among live tokens (uniq_mcp_token_hash_active), which no copy
        # of this table ever enforced — and the ones the query under test reads.
        await p.execute("INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, "
                        "operator_kind, revoked_at) VALUES ($1,1,'t',$5,$4,$2,$3)",
                        i, kind,
                        __import__('datetime').datetime.now(__import__('datetime').timezone.utc) if revoked else None,
                        f't-{i}', i.to_bytes(4, 'big'))

    async def ep(i, host, posture, days_ago):
        await p.execute("INSERT INTO nac_endpoints (token_id, hostname, posture, posture_at) "
                        "VALUES ($1,$2,$3, now() - ($4 || ' days')::interval)", i, host, posture, str(days_ago))

    # one machine, three enrolments — the desktop's shape
    for i, (posture, age) in enumerate([("healthy", 76), ("healthy", 66), ("healthy", 0)]):
        await tok(900 + i); await ep(900 + i, "desk", posture, age)
    # a machine whose enrolment was REVOKED — a decommissioned server
    await tok(910, revoked=True); await ep(910, "dead-server", "unknown", 32)
    # a posture row whose token is not a bastion at all
    await tok(911, kind='operator'); await ep(911, "not-a-bastion", "healthy", 1)
    # a normal, single-enrolment machine
    await tok(912); await ep(912, "normal", "degraded", 0)

    rows = await p.fetch(QUERY)
    hosts = [r["hostname"] for r in rows]
    check(len(rows) == 2, "three enrolments + a revoked one + a non-bastion collapse to 2 cards", hosts)
    check(hosts.count("desk") == 1, "the multi-enrolment machine appears ONCE", hosts)
    kept = [r["token_id"] for r in rows if r["hostname"] == "desk"]
    check(kept == [902], "and the kept row is the newest enrolment", kept)
    check("dead-server" not in hosts, "a REVOKED enrolment is not drawn as a machine", hosts)
    check("not-a-bastion" not in hosts, "a non-bastion token is not drawn as a machine", hosts)
    check("normal" in hosts, "an ordinary machine still appears", hosts)

    # the falsifying check: the OLD query would have shown all five
    old = await p.fetch("SELECT token_id, hostname FROM nac_endpoints ORDER BY hostname")
    check(len(old) == 6, "the unfiltered query really does return all six — 3 enrolments of one "
          "machine, a revoked one, a non-bastion and a normal box — so this test can fail", len(old))

    # ── A RE-PAIRED MACHINE THAT HAS NOT BEEN JUDGED YET ────────────────────
    # Ordering by `posture_at DESC NULLS LAST` alone preferred a DECIDED STALE
    # row over an UNDECIDED LIVE one — backwards for a page whose job is to say
    # what is on the network NOW. This is that exact shape: a laptop re-paired
    # minutes ago whose first posture has not landed, beside its old enrolment
    # decided a week back.
    #
    # The assertion above cannot see this on its own: there, the newest enrolment
    # is ALSO the most recently decided, so both orderings agree and the check
    # passes either way. This is the case that tells them apart.
    await tok(920); await ep(920, "laptop", "healthy", 7)
    await tok(921)
    await p.execute("INSERT INTO nac_endpoints (token_id, hostname, posture, posture_at) "
                    "VALUES (921,'laptop','unknown',NULL)")
    rows = await p.fetch(QUERY)
    kept = [r["token_id"] for r in rows if r["hostname"] == "laptop"]
    check(kept == [921],
          "a re-paired machine with no posture yet beats its own older decided row — "
          "the page shows the enrolment that is actually here", kept)

    # ── WHICH FINGERPRINT DECIDES "GOVERNED", AND AN EXPIRED PAIRING ────────
    # The Posture card's `governed` must come from the TOKEN's hardware_fingerprint
    # (what _nac_gate reads), not the endpoint row's, which the heartbeat upsert
    # COALESCEs and can keep after the token's is gone. token_expired must be the
    # bearer check's rule (expires_at <= now()).
    await p.execute("INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, "
                    "operator_kind, hardware_fingerprint, expires_at) VALUES "
                    "(930,1,'t','\\x0930'::bytea,'t-930','bastion',NULL, now() + interval '7 days'), "
                    "(931,1,'t','\\x0931'::bytea,'t-931','bastion','fp-931', now() - interval '1 day')")
    await p.execute("INSERT INTO nac_endpoints (token_id, hostname, posture, posture_at, fingerprint) "
                    "VALUES (930,'unbound','healthy',now(),'fp-old'), "
                    "(931,'lapsed','healthy',now(),'fp-931')")
    rows = {r["hostname"]: r for r in await p.fetch(QUERY)}
    check(rows["unbound"]["bound_fingerprint"] is None and rows["unbound"]["fingerprint"] == "fp-old",
          "bound_fingerprint is the TOKEN's (NULL), not the endpoint row's old one",
          dict(rows["unbound"]))
    check(rows["unbound"]["token_expired"] is False and rows["lapsed"]["token_expired"] is True,
          "token_expired follows the token's expires_at", (dict(rows["unbound"]), dict(rows["lapsed"])))

    await p.close()

from kernel_schema import kernel_schema_db  # noqa: E402
with kernel_schema_db(DSN) as _db:
    asyncio.run(main(_db))
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS: print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran"); sys.exit(1)
print(f"nac endpoints dedupe: {CHECKS}/{CHECKS} checks passed")
