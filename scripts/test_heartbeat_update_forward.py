#!/usr/bin/env python3
"""A bastion update to a NEWER version reaches a gating box with no approval.

Bastion 0.44.0 takes a self-update to a newer version on the release signature
alone (client-go/update.go maybeSelfUpdate), and says so in its enforcing
report (`update_forward_needs_no_approval`); a move to an older version still
needs the person's approval. The beat (topology.upsert_heartbeat) used to
withhold every update from a gating box until an approval was stored, and the
completion task asked the person's device for one, one machine at a time. Now
the beat delivers a forward move to a box that says it needs no approval, and
still withholds (and asks for) everything else:

  says so,     0.44.0 -> 0.45.0, gating      -> delivered, nothing asked
  does not,    0.44.0 -> 0.45.0, gating      -> withheld, asked (never inferred
                                                from the version number)
  does not,    0.43.1 -> 0.44.0, gating      -> withheld, asked (the last round)
  says so,     0.44.0 -> 0.43.1, gating      -> withheld, asked
  says so,     0.44.0 -> 0.43.1, approved    -> delivered with the approval
  does not,    0.43.1 -> 0.44.0, not gating  -> delivered, nothing asked
  says so,     0.44.0 -> 0.45.0, a MEMBER     -> withheld (the box takes updates
                                                from its anchor alone)

Needs a real Postgres for the same reason test_heartbeat_anchor_clears.py does:
the directive is read back from the upserted row, and a stub pool would assert
my reading of the SQL.

Run:  TEST_DSN=... python3 scripts/test_heartbeat_update_forward.py
"""
import asyncio
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

TOKEN = 90417
ENTITY = 1
GATE = "test_heartbeat_update_forward"
FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


async def main(dsn) -> int:
    import asyncpg
    from server.network import nac_approval, nac_dcn, topology

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    # The tables are the KERNEL's (see _database below): no DDL here.
    await pool.execute("DELETE FROM bastion_update_refusals WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM nac_endpoints WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = $1", TOKEN)
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = $1", TOKEN)
    await pool.execute(
        "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
        "VALUES ($1, $2, 'gate', '\\x00'::bytea, $3, 'bastion')",
        TOKEN, ENTITY, f"update-forward-gate-{TOKEN}")

    # The completion task is what files the device ask. Recorded, not run: this
    # gate is about whether the beat decides to ask at all.
    asked = []
    real_schedule = nac_approval.schedule_directive_approvals
    real_expected = nac_dcn.expected_box_grants

    def fake_schedule(pool_, eid_, token_id_, server_):
        asked.append(token_id_)
        return True

    async def fake_expected(pool_, eid_, hostname_):
        return None

    async def beat(running, target, gating, says, approval=None, member=False, pad=False, factor=True,
                   extra=None, raw=None):
        # This Host's pairing: the anchor, unless `member` (the box is anchored
        # to another Host, anchor.go pairingEnforcing). `pad` makes the report
        # larger than the 64 KiB topology keeps whole; `factor` False is a box
        # that holds no way to take an approval; `raw` replaces the report.
        enforcing = {"grants": [], "write_gate": gating}
        if gating:
            enforcing["anchored"] = True
            if factor:
                enforcing["second_factor"] = True
            if not member:
                enforcing["is_anchor"] = True
        enforcing.update(extra or {})
        if raw is not None:
            enforcing = raw
        if says:
            enforcing[nac_approval.UPDATE_FORWARD_KEY] = True
        if pad:
            enforcing["pad"] = "x" * 70000
        payload = {"token_id": TOKEN, "hostname": "box", "platform": "linux",
                   "tunnel_status": "up", "device_health": "healthy", "gateway_version": running,
                   "enforcing": enforcing}
        asked.clear()
        nac_approval.schedule_directive_approvals = fake_schedule
        nac_dcn.expected_box_grants = fake_expected
        try:
            # The first beat writes the row with the box's version; the target
            # is queued the way nac_commit.update_bastion queues it; the second
            # beat is the one under test.
            if await topology.upsert_heartbeat(pool, ENTITY, payload) is None:
                raise SystemExit("upsert_heartbeat refused the gate's own token: the fixture is wrong")
            await pool.execute(
                "UPDATE network_bastion_heartbeat SET update_to_version = $2, update_approval = $3 "
                "WHERE token_id = $1", TOKEN, target, approval)
            asked.clear()
            return await topology.upsert_heartbeat(pool, ENTITY, payload)
        finally:
            nac_approval.schedule_directive_approvals = real_schedule
            nac_dcn.expected_box_grants = real_expected

    try:
        got = await beat("0.44.0", "0.45.0", True, True)
        check(got.get("update") == {"version": "0.45.0"} and "self-update" not in (got.get("approvals") or {}),
              "a gating box that says so gets a newer version with no approval", got)
        check(asked == [], "and nobody is asked", asked)

        got = await beat("0.44.0", "0.45.0", True, False)
        check("update" not in got,
              "a gating box on 0.44.0 that does NOT say so does not get it without an approval", got)
        check(asked == [TOKEN], "and the person is asked: the rule is read from the box, not its version", asked)

        got = await beat("0.43.1", "0.44.0", True, False)
        check("update" not in got, "a gating box on 0.43.1 does not get 0.44.0 without an approval", got)
        check(asked == [TOKEN], "and the person is asked, one last time", asked)

        got = await beat("0.44.0", "0.43.1", True, True)
        check("update" not in got, "a gating box that says so does not get an older version without one", got)
        check(asked == [TOKEN], "and the person is asked", asked)

        got = await beat("0.44.0", "0.43.1", True, True, approval='{"v":1,"sig":"x"}')
        check(got.get("update") == {"version": "0.43.1"}
              and (got.get("approvals") or {}).get("self-update") == '{"v":1,"sig":"x"}',
              "approved, the older version goes out with its approval", got)

        got = await beat("0.43.1", "0.44.0", False, False)
        check(got.get("update") == {"version": "0.44.0"} and asked == [],
              "a box that gates nothing gets the update as before", (got, asked))

        got = await beat("0.44.0", "0.45.0", True, True, member=True)
        check("update" not in got,
              "a box anchored to ANOTHER Host is never sent an update: it would refuse it every beat, "
              "and its one refusal slot would hide the anchor's own refused approvals", got)

        got = await beat("0.44.0", "0.45.0", True, True, approval='{"v":1,"sig":"x"}', member=True)
        stored = await pool.fetchval("SELECT update_approval FROM network_bastion_heartbeat WHERE token_id=$1", TOKEN)
        check("update" not in got and stored is None,
              "…not even with an approval stored for it, and that approval is dropped", (got, stored))

        got = await beat("0.43.1", "0.44.0", True, False, pad=True)
        check("update" not in got and asked == [TOKEN],
              "a report too big to keep whole still says the box gates writes: withheld and asked, "
              "not sent unapproved", (got, asked))

        # What the card says, from the real list (topology._mark_update_waits).
        async def card_says(**kw):
            await beat(**kw)
            rows = await topology.list_bastions(pool, ENTITY)
            b = next((x for x in rows if x["id"] == TOKEN), {})
            return b.get("update_waits_on"), b.get("update_to_version")
        says_approval = await card_says(running="0.43.1", target="0.44.0", gating=True, says=False)
        await pool.execute("INSERT INTO bastion_update_refusals (token_id, version) VALUES ($1, '0.44.0')", TOKEN)
        says_refused = await card_says(running="0.43.1", target="0.44.0", gating=True, says=False)
        await pool.execute("DELETE FROM bastion_update_refusals WHERE token_id = $1", TOKEN)
        says_anchor = await card_says(running="0.44.0", target="0.45.0", gating=True, says=True, member=True)
        says_nothing = await card_says(running="0.44.0", target="0.45.0", gating=True, says=True)
        check(says_approval == ("approval", "0.44.0") and says_refused == ("refused", "0.44.0")
              and says_anchor == ("anchor", "0.45.0") and says_nothing == (None, "0.45.0"),
              "the card says what a standing update waits on: approval, refused, its anchor, or nothing",
              (says_approval, says_refused, says_anchor, says_nothing))

        # Refusals, on the real table: every one is kept, whichever way the target moves.
        await nac_approval._remember_update_refused(pool, TOKEN, "0.45.0")
        await nac_approval._remember_update_refused(pool, TOKEN, "0.44.0")
        await nac_approval._remember_update_refused(pool, TOKEN, "0.44.0")
        kept = sorted(r["version"] for r in await pool.fetch(
            "SELECT version FROM bastion_update_refusals WHERE token_id=$1", TOKEN))
        check(kept == ["0.44.0", "0.45.0"] and await nac_approval._update_refused(pool, TOKEN, "0.45.0")
              and await nac_approval._update_refused(pool, TOKEN, "0.44.0")
              and not await nac_approval._update_refused(pool, TOKEN, "0.46.0"),
              "a refusal is remembered per pairing and version, and a later one never erases it", kept)
        await pool.execute("DELETE FROM bastion_update_refusals WHERE token_id = $1", TOKEN)

        # What else the card says, and what a big report keeps.
        await beat(running="0.43.1", target="0.44.0", gating=True, says=False, approval='{"v":1,"sig":"x"}')
        b = next(x for x in await topology.list_bastions(pool, ENTITY) if x["id"] == TOKEN)
        check(b.get("update_waits_on") is None and b.get("update_approved") is True,
              "an update that carries a stored approval waits on nobody", (b.get("update_waits_on"), b.get("update_approved")))
        says_no_way = await card_says(running="0.43.1", target="0.44.0", gating=True, says=False, factor=False)
        says_machine = await card_says(running="0.44.0", target="0.45.0", gating=True, says=True, extra={
            "last_directive_refusal": {"what": "self-update", "why": "refused: directives are switched off here",
                                       "at": "2026-09-28T01:00:00Z"}})
        rows = await topology.list_bastions(pool, ENTITY)
        why = next(x for x in rows if x["id"] == TOKEN).get("update_refused_why")
        check(says_no_way == ("no_way", "0.44.0") and says_machine == ("machine", "0.45.0")
              and "switched off" in (why or ""),
              "a box with no way to approve says so, and one that refused the update itself says why",
              (says_no_way, says_machine, why))
        old_member = {"grants": [], "anchored": True}
        got = await beat("0.30.0", "0.45.0", False, False, raw=old_member)
        says_old_member = await card_says(running="0.30.0", target="0.45.0", gating=False, says=False, raw=old_member)
        check("update" not in got and says_old_member == ("anchor", "0.45.0"),
              "a member box too old to report the write gate is still never sent an update, and the card says why",
              (got, says_old_member))
        got = await beat("0.44.0", "0.45.0", True, True, pad=True)
        check(got.get("update") == {"version": "0.45.0"} and asked == [],
              "a report too big to keep whole still says this Host is the anchor and the box takes it forward", got)
        got = await beat("0.44.0", "0.45.0", True, True, pad=True, member=True)
        check("update" not in got, "…and still says when it is a member", got)

        # A beat closes this box's leftover update cards, by token, whatever
        # hostname they were filed under, and nothing of another token.
        from server.network import nac_ops as _ops
        _ops._update_cards_closed.clear()
        md = lambda server, token: json.dumps({"kind": "op_ask", "gate": GATE, "op_ask": {
            "shape": "mgmt", "op": "self-update", "server": server, "token_id": token,
            "purpose": nac_approval.mgmt_op_purpose("self-update", server, ["0.44.0"])}})
        left = [await pool.fetchval("INSERT INTO entity_tickets (entity_id, source, status, metadata) "
                                    "VALUES ($1, 'runtime', 'open', $2::jsonb) RETURNING id", ENTITY, md(h, t))
                for h, t in (("box", TOKEN), ("box-old-name", TOKEN), ("box", TOKEN + 1))]
        await beat("0.44.0", "0.45.0", True, True)
        st = [await pool.fetchval("SELECT status FROM entity_tickets WHERE id=$1", i) for i in left]
        check(st == ["expired", "expired", "open"],
              "a beat closes this box's leftover update cards, under any name, and no other box's", st)

        # Closing leftover update cards, on the real tickets table: exactly this
        # box's open update cards, and nothing of another box, op, kind or Host.
        from server.network import nac_ops

        async def card(status, server, token, op="self-update", version="0.45.0", kind="op_ask",
                       decision=None, eid=ENTITY):
            md = {"kind": kind, "gate": GATE, "op_ask": {
                "shape": "mgmt", "op": op, "server": server, "token_id": token,
                "purpose": nac_approval.mgmt_op_purpose(op, server, [version])}}
            return await pool.fetchval(
                "INSERT INTO entity_tickets (entity_id, source, status, metadata, decision) "
                "VALUES ($1, 'runtime', $2, $3::jsonb, $4::jsonb) RETURNING id",
                eid, status, json.dumps(md), json.dumps(decision or {}))
        ids = {
            "this box, open, this version": await card("open", "box", TOKEN),
            "this box, open, older version": await card("open", "box", TOKEN, version="0.40.0"),
            "same name, another token": await card("open", "box", TOKEN + 1),
            "another server": await card("open", "other-box", TOKEN),
            "a mirror card": await card("open", "box", TOKEN, op="mirror"),
            "another kind": await card("open", "box", TOKEN, kind="not_an_op_ask"),
            "another Host": await card("open", "box", TOKEN, eid=ENTITY + 1),
            "decided": await card("decided", "box", TOKEN, version="0.41.0", decision={"option_id": "no"}),
        }
        final = await nac_ops.settle_update_cards(pool, ENTITY, "box", TOKEN, ["0.45.0"])
        got_status = {k: await pool.fetchval("SELECT status FROM entity_tickets WHERE id=$1", v) for k, v in ids.items()}
        want = {k: "open" for k in ids}
        want.update({"this box, open, this version": "expired", "this box, open, older version": "expired",
                     "decided": "decided"})
        check(final is None and got_status == want,
              "settling closes exactly this box's open update cards, whatever their version", got_status)
        await card("decided", "box", TOKEN, version="0.46.0", decision={"option_id": "no", "note": "not now"})
        final = await nac_ops.settle_update_cards(pool, ENTITY, "box", TOKEN, ["0.46.0"])
        check(bool(final) and final.get("state") == "denied" and "not now" in final.get("detail", ""),
              "…and a no an earlier card recorded for this update still stands", final)
    finally:
        await pool.execute("DELETE FROM entity_tickets WHERE metadata->>'gate' = $1", GATE)
        await pool.execute("DELETE FROM bastion_update_refusals WHERE token_id = $1", TOKEN)
        await pool.execute("DELETE FROM nac_endpoints WHERE token_id = $1", TOKEN)
        await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = $1", TOKEN)
        await pool.execute("DELETE FROM mcp_client_tokens WHERE id = $1", TOKEN)
        await pool.close()

    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    return 1 if FAILS else 0


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


if __name__ == "__main__":
    _ctx = _database()
    if _ctx is None:
        sys.exit(_no_database(2))
    with _ctx as _dsn:
        sys.exit(asyncio.run(main(_dsn)))
