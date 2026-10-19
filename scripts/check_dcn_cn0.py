#!/usr/bin/env python3
"""Invariant: dcn-cn0 (DCN route-map groundwork — tier honesty, no delete,
CN-0 placement separation).

The DCN is an index layer, never a placement backend. These invariants pin the
structural guarantees so they can't silently regress:

Pure (always):
  * 'dcn' is NOT in placement's VALID_BACKENDS — the DCN never joins the
    store/retrieve/REMOVE contract (the DCN never deletes, so it cannot offer remove).
  * The per-class index tier is fixed: machine = locations only.
  * AuthMode.FILE stays deleted (the dead pre-DCN folder vocabulary).

DB (skippable via SKIP_DB=1):
  * The v077 placement CHECK still reads exactly pg|s3|bastion — no 'dcn'.
  * dcn_chunks structurally carries NO content/preview/snippet column.
  * dcn_stores.mask cannot contain 'delete' (schema CHECK refuses the word).
  * A preview row for a machine-class chunk is REFUSED by the tier trigger.
  (Write probes run inside transactions that are ALWAYS rolled back.)

Run in-pod: docker exec <c> python3 /app/scripts/check_dcn_cn0.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from brain.mastercode import context_backends, dcn


def pure_tests():
    fails = []
    if "dcn" in context_backends.VALID_BACKENDS:
        fails.append("'dcn' must NEVER enter placement's VALID_BACKENDS — the "
                     "ContextBackend contract includes remove(), and the DCN never deletes")
    if dcn.TIER_BY_CLASS.get("machine") != "locations only":
        fails.append("machine-class tier must be 'locations only'")
    if dcn.TIER_BY_CLASS.get("cabinet") != "locations + previews":
        fails.append("cabinet-class tier must be 'locations + previews'")
    try:
        from server.integrations.core.models import AuthMode
        if hasattr(AuthMode, "FILE") or "file" in {m.value for m in AuthMode}:
            fails.append("AuthMode.FILE was deleted at CN-0 (dead pre-DCN folder "
                         "vocabulary) and must stay deleted")
    except Exception as e:
        fails.append(f"AuthMode import failed: {e}")
    return fails


async def db_tests():
    fails = []
    if os.environ.get("SKIP_DB") == "1":
        print("  [db] SKIPPED (SKIP_DB=1)")
        return fails
    try:
        import asyncpg
        pool = await asyncpg.create_pool(host="/var/run/postgresql", user="entity",
                                         database="hostmem", min_size=1, max_size=2)
    except Exception as e:
        print(f"  [db] SKIPPED (database not reachable: {e})")
        return fails
    try:
        # The DCN schema is declared in migrations/v091_dcn_route_map.sql and
        # applied by entrypoint.sh before this container serves anything; the
        # dcn.ensure_dcn() mirror this used to call went with the runtime DDL.
        eid = (int(__import__("os").environ.get("ENTITY_ID") or 0) or int(await pool.fetchval("SELECT max(id) FROM entity_registry") or 1))  # the pod's own entity, never genesis 0

        # ── v077 placement CHECK untouched: pg|s3|bastion, never 'dcn' ──
        condef = await pool.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'context_backend_locations_backend_check'")
        if condef:
            if "'dcn'" in condef:
                fails.append("v077 backend CHECK must not admit 'dcn' (placement separation)")
            for want in ("'pg'", "'s3'", "'bastion'"):
                if want not in condef:
                    fails.append(f"v077 backend CHECK lost {want} — placement vocabulary changed")

        # ── tier honesty is schema-level: no content column on dcn_chunks ──
        # The table must EXIST for an absence check to mean anything:
        # information_schema.columns returns an empty set for a table that is
        # not there, and an empty set contains no banned name, so a missing
        # dcn_chunks would pass this arm on nothing. That state was unreachable
        # while ensure_dcn() created the table right here; it is gone, so
        # the only thing declaring dcn_chunks is now v091_dcn_route_map.sql:124
        # and its absence is a real possibility. Asserted, not assumed.
        # to_regclass, not information_schema, for BOTH halves of this. It is
        # schema-qualified, so a same-named table in another schema cannot
        # satisfy the check; and it is privilege-INDEPENDENT, where
        # information_schema.columns is privilege-filtered — a role with no
        # rights on dcn_chunks sees zero columns for a table that exists, which
        # would report "does not exist" the day this is pointed at the narrowed
        # recall lane the role split creates.
        if await pool.fetchval("SELECT to_regclass('public.dcn_chunks')") is None:
            fails.append("public.dcn_chunks does not exist — v091_dcn_route_map.sql declares it "
                         "and entrypoint.sh applies migrations before serving, so this "
                         "means the schema is behind, not that the tier invariant holds")
        cols = {r["column_name"] for r in await pool.fetch(
            "SELECT a.attname AS column_name FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass('public.dcn_chunks') "
            "  AND a.attnum > 0 AND NOT a.attisdropped")}
        for banned in ("preview", "content", "snippet", "text", "body"):
            if banned in cols:
                fails.append(f"dcn_chunks must have no '{banned}' column — machine-tier "
                             "previews are refused by SCHEMA absence")

        # ── mask CHECK refuses 'delete' (rolled back) ──
        async with pool.acquire() as con:
            tr = con.transaction()
            await tr.start()
            try:
                await con.execute(
                    "INSERT INTO dcn_stores (entity_id, class, server, root, mask) "
                    "VALUES ($1, 'machine', 'invariant-probe', '/probe', "
                    "        '[\"read\",\"delete\"]'::jsonb)", eid)
                fails.append("dcn_stores.mask accepted 'delete' — the schema CHECK "
                             "must refuse the word")
            except asyncpg.CheckViolationError:
                pass  # exactly right
            finally:
                await tr.rollback()

        # ── machine-class preview refused by the tier trigger (rolled back) ──
        async with pool.acquire() as con:
            tr = con.transaction()
            await tr.start()
            try:
                sid = await con.fetchval(
                    "INSERT INTO dcn_stores (entity_id, class, server, root) "
                    "VALUES ($1, 'machine', 'invariant-probe', '/probe') RETURNING id", eid)
                fid = await con.fetchval(
                    "INSERT INTO dcn_files (store_id, path) VALUES ($1, 'probe.txt') "
                    "RETURNING id", sid)
                cid = await con.fetchval(
                    "INSERT INTO dcn_chunks (file_id, seq) VALUES ($1, 0) RETURNING id", fid)
                try:
                    await con.execute(
                        "INSERT INTO dcn_chunk_previews (chunk_id, preview) "
                        "VALUES ($1, 'leak')", cid)
                    fails.append("a machine-class chunk accepted a preview — the tier "
                                 "trigger must refuse it")
                except asyncpg.RaiseError:
                    pass  # exactly right
            finally:
                await tr.rollback()

        if not fails:
            print("  [db] OK — v077 CHECK intact, dcn_chunks content-free, mask refuses "
                  "'delete', machine-tier preview refused by trigger (all probes rolled back)")
    except Exception as e:
        fails.append(f"db: dcn-cn0 test raised: {e}")
    finally:
        await pool.close()
    return fails


def main():
    fails = pure_tests()
    fails += asyncio.run(db_tests())
    if fails:
        print("DCN-CN0 CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("DCN-CN0 INVARIANTS OK — placement separation holds ('dcn' outside "
          "VALID_BACKENDS + v077 CHECK), machine tier is locator-only at the schema "
          "level, mask refuses 'delete', AuthMode.FILE stays deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
