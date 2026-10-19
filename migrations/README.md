# Schema migrations

Idempotent SQL scripts applied at every kernel boot, after the DB container
is ready, before the app starts. Each script runs once per cluster — the
`schema_migrations` table records `(version, name, applied_at)`.

The database server runs in the `cseries-postgres` container, but the schema
is the KERNEL's: `entrypoint.sh` loads `init-db.sql` and these migrations
with `psql` over the shared socket.

## Why

`init-db.sql` only runs on first-ever boot (when `entity_registry` does not
yet exist in the entity database). Any column added to source after an
entity was first provisioned is silently absent from that entity's PG. A
`codebook_training.entity_id` bug — runtime stayed `None`, every chat
returned 503, message vanished — was exactly this shape.

Migrations close that gap.

## Rules for new migration files

- File name: `v{NNN}_{slug}.sql` where `NNN` is a zero-padded integer
  (`v001_codebook_training_entity_id.sql`, `v002_...`).
- **Idempotent.** Use `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` /
  `WHERE NOT EXISTS` patterns. Crashing on re-run is a bug.
- **Forward-only.** No `DOWN` step. Roll forward to fix mistakes.
- **One feature per file.** Don't bundle unrelated changes — diffs and
  reverts get nasty.
- Never modify a migration after it has shipped. Add a new one.

## How it runs

`entrypoint.sh` after PG init / before uvicorn:

```bash
for f in /app/migrations/v*.sql; do
    version=$(basename "$f" | sed 's/^v0*//; s/_.*//')
    name=$(basename "$f" .sql)
    if [ "$(psql ... -At -c "SELECT 1 FROM schema_migrations WHERE version=$version")" = "1" ]; then
        continue
    fi
    psql ... -v ON_ERROR_STOP=1 -f "$f" || { echo "migration $name FAILED"; exit 1; }
    psql ... -c "INSERT INTO schema_migrations(version, name) VALUES ($version, '$name')"
done
```

Failures abort container startup. Loud failure beats silent breakage.

## Backfilling old entities

Existing entities provisioned before this framework existed have PG
states that predate many tables. The migrations dir captures what's
shipped so far. To catch up an old entity:

1. Boot it — migrations run automatically.
2. Each missing `ALTER TABLE IF NOT EXISTS` lands its column.
3. Missing whole tables aren't backfilled here — those land via
   feature-specific migrations as the feature is needed by that entity.

Don't try to mass-add every drifted column at once. Add migrations
per-feature so failures are scoped.
