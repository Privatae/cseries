"""Run as the Host's own code (PYTHONPATH=/app:/app/brain, the Host's DATABASE_URL): a machine this
Host is in charge of checks in, through the real heartbeat path (topology.upsert_heartbeat ->
pairing_grants.record_and_notify -> the card), carrying another Host's pairing that waits for it.
Invented names only. `check <subject>` prints the grant and the card the person's answer left."""
import asyncio, json, os, sys
import asyncpg

MACHINE = "studio-desktop"


async def main(mode, subject, handle):
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    from server.network import pairing_grants, topology
    if mode == "check":
        g = await pool.fetchrow("SELECT caps, approved_by, revoked_at FROM nac_pairing_grants "
                                "WHERE server=$1 AND subject=$2 ORDER BY id DESC LIMIT 1", MACHINE, subject)
        t = await pool.fetchrow("SELECT id, status, decision FROM entity_tickets WHERE metadata->>'kind'="
                                "'bastion_pairing' AND metadata->'bastion_pairing'->>'subject'=$1 "
                                "ORDER BY id DESC LIMIT 1", subject)
        pk = await pool.fetch("SELECT rp_id, sign_count, last_used_at IS NOT NULL AS used FROM owner_passkeys")
        print(json.dumps({"grant": dict(g) if g else None, "ticket": dict(t) if t else None,
                          "owner_passkeys": [dict(r) for r in pk]}, default=str))
        return
    # A name this Host answers to as a machine's anchor (pairing_grants._own_host_names: its
    # handle's names are always among them).
    from server.home_origin import HOME
    from server.network import bastion_enroll
    own = bastion_enroll._host_names(HOME.handle)[0]
    tid = await pool.fetchval("SELECT id FROM mcp_client_tokens WHERE name='studio-desktop-bastion'")
    if tid is None:
        tid = await pool.fetchval(
            "INSERT INTO mcp_client_tokens (entity_id, name, token_hash, url_handle, scopes, operator_kind, "
            "notes) VALUES (1, 'studio-desktop-bastion', $1, 'studio', '{}', 'bastion', '{}'::jsonb) RETURNING id",
            os.urandom(32))
    beat = {"token_id": tid, "hostname": MACHINE, "gateway_version": "0.62.0", "platform": "linux",
            "mode": "networked", "anchor": {"owner": "the-machine-owner", "anchor_host": own},
            "pairings": [{"host_name": own, "handle": own.split(".")[0], "state": "active"},
                         {"host_name": subject, "handle": handle, "state": "pending"}],
            "unapproved_pairing_attempts": [{"host_name": subject, "handle": handle}],
            "enforcing": {"is_anchor": True}}
    r = await topology.upsert_heartbeat(pool, 1, beat)
    print(json.dumps({"own": own, "token_id": tid, "beat": bool(r)}))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""))
