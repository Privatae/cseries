# Schema migrations

SQL scripts that bring a Host's database forward from the schema in
`init-db.sql`. The database server runs in its own container (the `postgres`
service in `compose.yaml`), but the schema belongs to the kernel:
`entrypoint.sh` loads `init-db.sql` and these migrations with `psql` over the
shared socket, after the database answers and before the server starts.

## Why

`init-db.sql` only loads into an empty database (when `entity_registry` does
not exist yet). A table or column added to the source after a Host's database
was first created reaches that database only through a migration.

## Rules for new migration files

- File name: `v{NNN}_{slug}.sql`, where `NNN` is a zero-padded three-digit
  number that no other file uses. Files run in the shell's glob order, which
  is alphabetical, so the padding is what keeps them in numeric order.
- **Idempotent.** Use `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` /
  `WHERE NOT EXISTS` patterns. Crashing on re-run is a bug.
- **Forward-only.** No down step. Roll forward to fix mistakes.
- **One feature per file.** Unrelated changes in one file make failures and
  reviews harder to read.
- Never modify a migration after it has shipped. Add a new one.

## How it runs

The "kernel schema section" of `entrypoint.sh`, on every boot:

1. Creates the `schema_migrations` table if it is missing
   (`version`, `name`, `applied_at`).
2. For each `migrations/v*.sql` in glob order, reads the version from the
   file name (`v001_foo` → 1) and skips it if `schema_migrations` already has
   that version. Otherwise it runs the file with `ON_ERROR_STOP=1` and then
   records the version and name.
3. A failed migration stops the boot.

So each migration runs once per Host database. Two files are the exception,
because they create roles and grants rather than schema: `v139_host_recall_role`
and `v233_host_world` run on every boot, outside the loop, and are recorded so
the loop skips them.

`scripts/kernel_schema.py` runs the same section of `entrypoint.sh` against a
throwaway database, so the test gates build the schema the way a booting Host
does.
