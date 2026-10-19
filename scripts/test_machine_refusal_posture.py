#!/usr/bin/env python3
"""A4.4: the box's OWN refusals reach the machine card, and NEVER degrade it.

The bastion rolls `mgmt_audit.jsonl` up into its enforcing report:

    "audit": {"lines": N, "refused_last_hour": M,
              "last_refusal": {"ts": ..., "op": ..., "why": ...}}

The host side reads it on the beat that already turns grant drift into
'degraded' (topology.upsert_heartbeat) and writes the refusal into the same
`detail` -> nac_endpoints.last_decision the machine card renders.

THE CASE THAT MATTERS IS THE ABSENT ONE. The whole live fleet is 0.28.0 and
sends no `audit` key at all, so absent has to be indistinguishable from before:
same posture, same last_decision, no exception swallowed on the way (the block
is wrapped in a bare `except` that logs at info — a TypeError in here would
disappear and take the posture write with it, which is why the DB half asserts
the row rather than the return value).

AND A REFUSAL IS NOT A DEGRADE. posture is a policy input the PDP matches on
(nac_controller._match / min_posture :250) and a quarantine profile denies
inbound on it, so degrading a box for refusing an op it holds no grant for would
cut the mesh to a machine that behaved correctly. Asserted here so a later
"tighten it up" edit has to argue with a gate.

Pure half runs anywhere. DB half needs a real Postgres, for the same reason
test_heartbeat_anchor_clears.py does: the behaviour ends in an ON CONFLICT
upsert and a stub pool would assert my reading of the SQL.

Run:  python3 scripts/test_machine_refusal_posture.py
      eval "$(scripts/peer_test_db.sh)" first for the DB half
"""
import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network.nac_controller import POSTURE_DETAIL_MAX  # noqa: E402
from server.network.nac_dcn import (  # noqa: E402
    compose_posture_detail, grant_drift, refusal_note,
)

TOKEN = 90014
ENTITY = 1
FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


AUDIT_REFUSED = {"lines": 12, "refused_last_hour": 3,
                 "last_refusal": {"ts": "2026-09-15T10:04:00Z", "op": "ctx_del",
                                  "why": "no grant for this store"}}
AUDIT_CLEAN = {"lines": 12, "refused_last_hour": 0}


def pure_tests():
    print("[pure] refusal_note")
    n = refusal_note({"audit": AUDIT_REFUSED})
    check(isinstance(n, str) and "3 management ops refused" in n,
          "refusals are counted in plain words", f"got {n!r}")
    check(n is not None and "ctx_del" in n and "no grant for this store" in n,
          "the last refusal's op and why ride along", f"got {n!r}")
    check(refusal_note({"audit": AUDIT_CLEAN}) is None,
          "refused_last_hour 0 -> nothing to say")
    check(refusal_note({"grants": []}) is None,
          "NO audit block (the live 0.28.0 fleet) -> nothing to say, no raise")
    check(refusal_note(None) is None, "enforcing None -> None")
    check(refusal_note({"audit": "yes"}) is None, "a malformed audit block reads as silence")
    check(refusal_note({"audit": {"refused_last_hour": "three"}}) is None,
          "a non-numeric count reads as silence rather than raising")
    one = refusal_note({"audit": {"refused_last_hour": 1}})
    check(one is not None and "1 management op refused" in one,
          "singular reads as one op, not '1 ops'", f"got {one!r}")

    # The 500-char cap. last_decision stores detail[:POSTURE_DETAIL_MAX]
    # (nac_controller :374), and drift prose + refusal note together exceed it, so
    # ordering decides which signal survives. Appended note-LAST the note was the
    # casualty — the one thing this path exists to carry, lost with no marker.
    print("[pure] compose_posture_detail (the %d-char budget)" % POSTURE_DETAIL_MAX)
    big_note = refusal_note({"audit": {
        "refused_last_hour": 999999,
        "last_refusal": {"op": "o" * 80, "why": "w" * 200, "ts": "t" * 60}}})
    # A maximal drift line built from grant_drift itself, not a hand-typed string,
    # so a change to its wording is reflected here instead of drifting from it.
    maximal_drift = grant_drift(
        [{"store_id": 999999, "signature": "a"}],
        {"grants": [{"store_id": s, "signature": "x"} for s in (999991, 999992, 999993)],
         "mgmt_ops": ["an_unknown_op_name_%d" % i for i in range(6)]})
    drift_detail = "drift: " + "; ".join(maximal_drift[:4])
    check(len(drift_detail) + len(big_note) > POSTURE_DETAIL_MAX,
          "the fixture is genuinely over the cap (else this proves nothing)",
          f"drift={len(drift_detail)} note={len(big_note)} cap={POSTURE_DETAIL_MAX}")

    out = compose_posture_detail(big_note, drift_detail, POSTURE_DETAIL_MAX)
    check(len(out) <= POSTURE_DETAIL_MAX,
          "the composed detail fits the cap BY CONSTRUCTION, not by the DB cutting it",
          f"len={len(out)}")
    check("999999 management ops refused" in out,
          "the refusal COUNT survives a maximal drift (the defect: it did not)",
          f"got {out[:120]!r}")
    check(out.startswith(big_note[:40]), "the note leads", f"got {out[:60]!r}")
    check(out.endswith("…"), "the elided half ends in an ellipsis, so the cut is visible",
          f"got ...{out[-40:]!r}")
    check("drift:" in out, "and as much drift prose as fits is still carried")

    # Both halves whole when they fit — no gratuitous mangling of the common case.
    small = compose_posture_detail("note", "drift: one thing", POSTURE_DETAIL_MAX)
    check(small == "note | drift: one thing", "under the cap both halves ride whole",
          f"got {small!r}")
    check(compose_posture_detail(None, "drift: x", POSTURE_DETAIL_MAX) == "drift: x",
          "no note -> the drift line is passed through untouched")
    check(compose_posture_detail("note", None, POSTURE_DETAIL_MAX) == "note",
          "no drift -> the note alone")
    # A note that fills the cap on its own leaves no room; drift's prose goes, its
    # verdict does not (it is in the posture column).
    filled = compose_posture_detail("n" * POSTURE_DETAIL_MAX, "drift: x", POSTURE_DETAIL_MAX)
    check(len(filled) == POSTURE_DETAIL_MAX and filled == "n" * POSTURE_DETAIL_MAX,
          "a cap-filling note drops drift's prose rather than overflowing",
          f"len={len(filled)}")


async def db_tests(dsn):
    # Out of pod: a throwaway from peer_test_db.sh. In pod (smoke_test drives this
    # through docker exec): the entity's own socket-only Postgres, the same way
    # check_posture_redecides.py reaches it.
    print("\n[db] heartbeat -> nac_endpoints")
    import asyncpg
    from server.network import topology

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    # The tables are the KERNEL's (see _database below): no DDL here.
    await pool.execute("DELETE FROM nac_endpoints WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = $1", TOKEN)
    # url_handle is NOT NULL and uniquely indexed among live rows, so it is derived
    # from TOKEN rather than hardcoded: two gates sharing a literal would collide on
    # that index in a pod where both have run.
    await pool.execute(
        "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
        "VALUES ($1, $2, 'gate', '\\x00'::bytea, $3, 'bastion')",
        TOKEN, ENTITY, f"refusal-gate-{TOKEN}")

    # `expected` is PATCHED, never left to the environment. Checks 1, 3 and 4 need
    # expected to be None — "this host cannot compute its grant set" — because that
    # is the branch where the refusal note is the only thing writing `detail`.
    #
    # Left unpatched this test passed here and would have FAILED in a pod, for a
    # reason that is nothing to do with refusals: out of pod the throwaway Postgres
    # has no trust-store tables, so expected_box_grants raises and returns None; in
    # a pod ensure_entity_signing_key (server/applications/entity_signing.py:53-80)
    # CREATES the key instead of raising, _signed_dcn_grants returns [], and
    # expected is [] — not None. The old fixture was encoding the exception path
    # and calling it the normal one. Patching makes the branch under test explicit
    # instead of a property of whichever database happens to be attached.
    from server.network import nac_dcn
    real_expected = nac_dcn.expected_box_grants

    async def beat(enforcing, expected=None):
        async def fake_expected(pool_, eid_, hostname_):
            return expected
        payload = {"token_id": TOKEN, "hostname": "box", "platform": "linux",
                   "tunnel_status": "up", "device_health": "healthy"}
        if enforcing is not None:
            payload["enforcing"] = enforcing
        nac_dcn.expected_box_grants = fake_expected
        try:
            got = await topology.upsert_heartbeat(pool, ENTITY, payload)
        finally:
            nac_dcn.expected_box_grants = real_expected
        if got is None:
            raise SystemExit("upsert_heartbeat refused the gate's own token — "
                             "the fixture is wrong, not the code under test")
        return await pool.fetchrow(
            "SELECT posture, last_decision FROM nac_endpoints WHERE token_id = $1", TOKEN)

    # 1. audit ABSENT — the live fleet. Nothing about this beat may change.
    r = await beat({"grants": []})
    check(r is not None and r["posture"] == "healthy",
          "no audit block: posture is the self-reported healthy, unchanged",
          f"got {r and r['posture']!r}")
    check(r is not None and not (r["last_decision"] or ""),
          "no audit block: no refusal text invented",
          f"got {r and r['last_decision']!r}")

    # 2. refusals — surfaced, and still HEALTHY. The box enforced correctly.
    r = await beat({"grants": [], "audit": AUDIT_REFUSED})
    check(r["posture"] == "healthy",
          "3 refusals do NOT degrade the machine (a refusal is the box working)",
          f"got {r['posture']!r}")
    check("3 management ops refused" in (r["last_decision"] or "") and "ctx_del" in (r["last_decision"] or ""),
          "the refusal and its last op land on the machine card's why-line",
          f"got {r['last_decision']!r}")

    # 3. the count clears — the stale note must be retracted, not COALESCEd forever.
    r = await beat({"grants": [], "audit": AUDIT_CLEAN})
    check(r["posture"] == "healthy", "0 refusals: still healthy", f"got {r['posture']!r}")
    check("refused nothing" in (r["last_decision"] or ""),
          "0 refusals from an audit-carrying box RETRACTS the previous note",
          f"got {r['last_decision']!r}")

    # 4. and a box with no audit block does not retract it either way — it has
    #    nothing to say, so the column is left exactly as it was.
    await beat({"grants": [], "audit": AUDIT_REFUSED})
    r = await beat({"grants": []})
    check("3 management ops refused" in (r["last_decision"] or ""),
          "an audit-less beat leaves the last note alone (silence is not a retraction)",
          f"got {r['last_decision']!r}")

    # 5. THE CAP, end to end. Drift and a refusal on the SAME beat, both maximal.
    #    The stored value has to still carry the refusal count: that was the
    #    defect, and asserting the composer alone would not prove that
    #    the value which reaches the column is the composed one.
    #
    #    Drift needs a non-None `expected`, which normally comes from signed DCN
    #    grants — so this one is driven with a granted store, through the same
    #    patch seam as every other beat here. The real grant_drift, the real
    #    composer and the real INSERT all run.
    r = await beat({"grants": [{"store_id": s, "signature": "x"} for s in (999991, 999992, 999993)],
                    "mgmt_ops": ["an_unknown_op_name_%d" % i for i in range(6)],
                    "audit": {"refused_last_hour": 999999,
                              "last_refusal": {"op": "o" * 80, "why": "w" * 200,
                                               "ts": "t" * 60}}},
                   expected=[{"store_id": 999999, "signature": "a"}])

    stored = r["last_decision"] or ""
    expected_note = refusal_note({"audit": {
        "refused_last_hour": 999999,
        "last_refusal": {"op": "o" * 80, "why": "w" * 200, "ts": "t" * 60}}})
    check(r["posture"] == "degraded",
          "drift on the same beat still degrades (the refusal did not displace its verdict)",
          f"got {r['posture']!r}")
    # The WHOLE note, not just its count. Measured while falsifying this gate: with
    # the note appended last, the count still landed — it sits in the first 62 chars
    # and the 500-char cut fell past it — while the op, the why and the ts were
    # silently gone. Asserting the count alone would have called the defect fixed.
    check(expected_note and expected_note in stored,
          "THE FIX: the refusal note survives a maximal drift WHOLE in the stored value",
          f"stored={stored[:140]!r}")
    check("999999 management ops refused" in stored,
          "including its count", f"got {stored[:80]!r}")
    check(len(stored) <= POSTURE_DETAIL_MAX,
          "and the stored string is within the cap", f"len={len(stored)}")
    check("drift:" in stored, "with the drift prose still carried after it")

    # 6. THREE signals in one column — the seam this file and the unknown-ops line
    #    created between them. That change added an informational "ops this host
    #    does not know" line to the same `detail`; this file added the refusal
    #    note. Neither change could gate the combination, because neither existed
    #    when the other's gate was written, and the merge of the two is where a
    #    composition bug would actually live.
    #
    #    ORDER IS THE ASSERTION, not just survival. The refusal note leads because it
    #    sets no posture and its prose is therefore the whole signal; drift follows
    #    because 'degraded' survives in the posture column without its words; the
    #    unknown-op line is last because it is explicitly informational. Check 5 above
    #    already drives all three maximal — it proves the cap holds. This one is the
    #    ordinary case, where all three FIT, and proves none of them is dropped when
    #    there is room for every one.
    r = await beat({"grants": [{"store_id": 1, "signature": "STALE"}],
                    "mgmt_ops": ["uptime", "op_from_a_future_bastion"],
                    "audit": {"refused_last_hour": 2,
                              "last_refusal": {"op": "ctx_del", "why": "no grant for this store",
                                               "ts": "2026-09-15T10:04:00Z"}}},
                   expected=[{"store_id": 1, "signature": "CURRENT"}])
    stored = r["last_decision"] or ""
    check(stored.startswith("2 management ops refused"),
          "all three present: the refusal note still leads", f"got {stored[:70]!r}")
    check("drift:" in stored, "the drift prose is still carried")
    check("op_from_a_future_bastion" in stored,
          "and the unknown op is not dropped to make room", f"got {stored!r}")
    check(r["posture"] == "degraded",
          "the drift verdict is unchanged by either note", f"got {r['posture']!r}")
    check(len(stored) <= POSTURE_DETAIL_MAX,
          "the three-way string is within the cap", f"len={len(stored)}")

    await pool.execute("DELETE FROM nac_endpoints WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = $1", TOKEN)
    await pool.close()


pure_tests()


def _database():
    """Where this gate runs: only a database it may write to, because it
    DELETEs and INSERTs rows under fixed ids.
      - Out of pod (PEER_TEST_DSN / TEST_DSN, a test SERVER): a fresh clone of
        the KERNEL's schema (scripts/kernel_schema.py, the schema entrypoint.sh
        builds), made for this run and dropped after it.
      - In smoke (docker exec): the database smoke made for this run, named by
        scripts/smoke_test.sh in GATE_SCRATCH_DSN (its own throwaway
        container's).
    Nothing else. It used to fall back to the pod socket whenever
    /var/run/postgresql existed, and inside a Host's pod that is the Host's own
    database. Never a hand copy either: one let this gate pass out of pod and
    fail in the pod on a release (url_handle NOT NULL)."""
    import contextlib
    server = os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if server:
        sys.path.insert(0, os.path.join(_ROOT, "scripts"))
        from kernel_schema import kernel_schema_db
        return kernel_schema_db(server, entity_id=ENTITY)
    scratch = os.environ.get("GATE_SCRATCH_DSN")
    if scratch:
        return contextlib.nullcontext(scratch)
    return None


def _no_database(skip_code: int) -> int:
    """No database this gate may write to. In a gate lane (GATE_LANE, which
    smoke sets) that is a FAILURE, never a quiet skip; by hand it is a skip."""
    lane = os.environ.get("GATE_LANE")
    if lane:
        print(f"FAIL — the {lane} lane named no database this gate may write to "
              "(GATE_SCRATCH_DSN, or a test server in PEER_TEST_DSN/TEST_DSN). It never "
              "picks one itself: a Host's pod socket is that Host's own database.")
        return 1
    print("SKIP — no test database (it writes rows, so it never picks one itself). "
          f"Start one:  eval \"$(bash {os.path.join(_ROOT, 'scripts/peer_test_db.sh')})\"")
    return skip_code


_ctx = _database()
if _ctx is None:
    print("\n[db]", end=" ")
    if _no_database(0):
        FAILS.append("the DB half had no database it may write to, in a gate lane")
else:
    with _ctx as _dsn:
        asyncio.run(db_tests(_dsn))
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"machine refusal posture: {CHECKS}/{CHECKS} checks passed")
sys.exit(0)
