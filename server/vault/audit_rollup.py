"""server/vault/audit_rollup.py — the credential audit's retention bound.

The credential audit rolls up the way the domain audit did (the same
rule, applied to vault_audit): nothing else bounds it — 80,759 rows
on one Host in about three months, nearly all get_envelope reads by
the kernel's own lanes — and a messaging connector adds a get_envelope and a
use:http_call row per platform poll. So a routine credential read older than
AUDIT_ROLLUP_AFTER_DAYS becomes a count in vault_audit_rollup
(migrations/v205), one row per (UTC day, actor, op, owner, key, method, host,
status), with first and last time, and its raw row is deleted in the SAME
statement that counts it: a crash never drops a row that was not counted, and
a re-run never counts one twice.

This once ran inside the domain controller's audit loop, before that
controller's decision engine was retired; the rule and its SQL are unchanged.

Eligibility is POSITIVE, read from the writers (server/vault/store.py _audit,
server/vault/authz.py use_credential):
  get_envelope   store.get_envelope writes it after its SELECT, for a hit or a
                 miss alike; what it returns is ciphertext.
  use:http_call  authz.use_credential writes it in a `finally`, with the HTTP
                 status only when the call came back, so a 2xx or 3xx status
                 is a call that succeeded and a row with no status is one that
                 raised.
Every other op is a change or has an effect, and stays raw: put, revoke,
deactivate, activate, add_recipient, use:sign (the secret signs something),
use:browser_type (the secret is typed into a page). So does every row a PERSON
made through the Keys app (routes/vault.py _actor writes 'user:<email> ip:<ip>
<action>'): a reveal returns the plaintext, and a person's use is rare and
deliberate. The rollup key keeps the actor verbatim (the kernel's lanes are a
short fixed set) and, for a use, the method, host and status, so a rolled day
still says who used which key, how and with what result; only each row's own
time goes.
"""

import asyncio
import logging

logger = logging.getLogger("privatae.vault.audit_rollup")

AUDIT_ROLLUP_AFTER_DAYS = 30
AUDIT_ROLLUP_BATCH = 5000

VAULT_ROLLUP_READ_OPS = ("get_envelope",)
VAULT_ROLLUP_USE_OPS = ("use:http_call",)
# A person's rows: routes/vault.py _actor, and a bare 'user' some routes write.
VAULT_ROLLUP_PERSON_ACTOR = r"^user(:|$)"
# Which successful calls are READS. Only allowed reads roll up; writes stay
# raw. A use row records method, host and status, never the path, so the
# method is what says read: GET and HEAD. A POST, PUT, PATCH or DELETE is a
# write with an effect outside the Host — the Host's own `keys_manage op=use`
# call to an API, a RunPod job started (a spend), another Host's use through a
# slip (`peer:<id>`) — and keeps its row. The one exception is a messaging
# connector (`connector:<app_id>`, the actor nac_connector.platform_call
# passes): its poll is itself a POST (Telegram's getUpdates,
# default-skills/connectors.md), so excluding POST would leave the volume this
# rollup exists for, and every message it sends is kept one per row in
# connector_messages (migrations/v203).
VAULT_ROLLUP_READ_METHODS = ("GET", "HEAD")
VAULT_ROLLUP_POLL_ACTOR = r"^connector:"

_VAULT_ROLLUP_SQL = r"""
WITH moved AS (
    DELETE FROM vault_audit
    WHERE id IN (
        SELECT id FROM vault_audit
        WHERE at < NOW() - make_interval(days => $1)
          AND actor !~ $5
          AND (op = ANY($3::text[])
               OR (op = ANY($4::text[])
                   AND COALESCE(detail->>'status', '') ~ '^[23][0-9][0-9]$'
                   AND (upper(COALESCE(detail->>'method', '')) = ANY($6::text[])
                        OR actor ~ $7)))
        ORDER BY id
        LIMIT $2)
    RETURNING at, actor, op, owner_identity, logical_name, detail
), rolled AS (
    INSERT INTO vault_audit_rollup AS r
        (day, actor, op, owner_identity, logical_name,
         http_method, http_host, http_status, count, first_seen, last_seen)
    SELECT (at AT TIME ZONE 'UTC')::date,
           actor,
           op,
           COALESCE(owner_identity, ''),
           COALESCE(logical_name, ''),
           COALESCE(detail->>'method', ''),
           COALESCE(detail->>'host', ''),
           COALESCE(detail->>'status', ''),
           count(*),
           min(at),
           max(at)
    FROM moved
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
    ON CONFLICT (day, actor, op, owner_identity, logical_name,
                 http_method, http_host, http_status)
    DO UPDATE SET count      = r.count + EXCLUDED.count,
                  first_seen = LEAST(r.first_seen, EXCLUDED.first_seen),
                  last_seen  = GREATEST(r.last_seen, EXCLUDED.last_seen)
    RETURNING 1
)
SELECT (SELECT count(*) FROM moved) AS moved, (SELECT count(*) FROM rolled) AS groups
"""


async def rollup_vault_reads(
    pool,
    older_than_days: int = AUDIT_ROLLUP_AFTER_DAYS,
    batch: int = AUDIT_ROLLUP_BATCH,
    pause_s: float = 0.2,
) -> int:
    """Count routine credential reads older than `older_than_days` into
    vault_audit_rollup and delete their raw rows, `batch` at a time with a
    pause between, until none are left. Each batch is ONE statement (a
    DELETE ... RETURNING feeding the rollup upsert), so its rows are counted
    and removed together or not at all, and a re-run counts nothing twice.
    Returns the rows rolled up. No pool: nothing to do."""
    if pool is None:
        return 0
    total = 0
    while True:
        row = await pool.fetchrow(
            _VAULT_ROLLUP_SQL, int(older_than_days), int(batch),
            list(VAULT_ROLLUP_READ_OPS), list(VAULT_ROLLUP_USE_OPS),
            VAULT_ROLLUP_PERSON_ACTOR, list(VAULT_ROLLUP_READ_METHODS),
            VAULT_ROLLUP_POLL_ACTOR)
        moved = int(row["moved"]) if row else 0
        total += moved
        if moved < batch:
            return total
        await asyncio.sleep(pause_s)


async def run_vault_audit_rollup_loop(pool, first_delay_s: float = 600,
                                      every_s: float = 3600) -> None:
    """The only caller of rollup_vault_reads, started from server/main.py's
    lifespan once the Host's database is up: first pass `first_delay_s` after
    boot, then every `every_s`. A cycle's error is logged, never raised."""
    await asyncio.sleep(first_delay_s)
    while True:
        try:
            rolled = await rollup_vault_reads(pool)
            if rolled:
                logger.info("vault audit rollup: %d routine credential-read rows "
                            "counted into vault_audit_rollup", rolled)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("vault audit rollup cycle error (non-fatal): %s", e)
        await asyncio.sleep(every_s)
