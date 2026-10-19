"""Land one person's rows from a public site into this Host's app tables.

THE FAR END OF "BRING MY DATA". A person used a free public webapp, liked it, and now
runs their own Host with the same app installed. The controller has already
decided that this Host's owner IS that site member — it compares two Authentik
subjects from the platform's own records and refuses otherwise — and the public
zone agent has already exported exactly that subject's rows with a keyed COPY.
This module is the last step: those rows become rows in this pod's database.

WHAT THIS MODULE IS DEFENDING AGAINST, given the gate upstream already held.
The subject decision is the controller's and it is not re-litigated here. What
is still open at this point is SCOPE — what the payload is allowed to touch —
and the payload arrives over a network from a process on another machine:

  * The INSTALLED app's own manifest is the authority on which tables exist and
    which column owns a row. A table named in the payload but not declared by
    the app on THIS Host is not written. That is the second half of "keyed, not
    a dump": the store manifest could be newer, older, or tampered with, and
    none of those may widen what lands here.
  * Every identifier is re-checked with the manifest validator's own fence
    (_bad_ident) before it reaches SQL, because these names are interpolated —
    there is no bind parameter for a table name — and the check belongs next to
    the interpolation, not only at the far end that sent them.
  * Every row's owner column is OVERWRITTEN with the envelope's subject rather
    than read from the row. A payload whose rows carry someone else's subject
    then lands under the mover's, which is a no-op for an honest export and the
    only safe reading of a dishonest one.
  * An existing row belonging to a DIFFERENT subject is never overwritten. See
    _upsert_sql: the conflict clause updates only where the stored row is
    already this subject's.

IDEMPOTENT PER (move_id, subject), AS A HARD REQUIREMENT. The controller's
agent feed takes no lease (webapps/service.py agent_subject_moves says so in as
many words), so a move is re-offered on every poll and this Host may fetch and
import the same payload more than once. Landing it twice must leave ONE copy of
each row. That is achieved by deduplicating on the TABLE'S OWN PRIMARY KEY plus
the subject — never on "have I already seen this move_id", which is a fact held
in memory and does not survive the restart that caused the second attempt.

A table with no primary key is REFUSED rather than imported. Without a key
there is no such thing as the same row twice, so a re-import would append a
second copy of everything and the idempotency requirement above would be a
sentence in a docstring rather than a property.

NO SCHEMA IS CREATED HERE. Tables are declared in migrations/*.sql and by the
app's own first migration; a missing one is reported by name and the import
fails. The kernel does not hold DDL rights at runtime
(scripts/test_no_runtime_ddl.py) and this module does not ask for any.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .manifest import AppManifest, TableSpec, _bad_ident

log = logging.getLogger(__name__)

# A type expression out of pg_catalog's own format_type(). Validated even
# though the catalog is the source, because it is the one string here that is
# neither a bind parameter nor an identifier passing _bad_ident, and "it came
# from a trusted place" is the reasoning that ages worst.
_PG_TYPE_RE = re.compile(r"^[a-z][a-z0-9 _]*(\(\d+(,\s*\d+)?\))?(\[\])?$")


class ImportRefused(Exception):
    """The payload cannot be landed, with the reason a person can act on."""


def _quote(ident: str) -> str:
    """A validated bare identifier, double-quoted.

    The contract's identifiers are already the subset that means the same thing
    quoted or not (manifest._SQL_IDENT_RE plus the reserved-word list), so the
    quotes add nothing a caller can see. They are here so that the rule "every
    interpolated identifier in this file is quoted" is checkable by reading,
    rather than depending on the fence upstream having been applied.
    """
    return '"' + ident + '"'


def _declared_tables(manifest: AppManifest) -> dict[str, TableSpec]:
    """The installed app's own data contract, by table name."""
    return {t.name: t for t in manifest.data.tables}


def _coerce(value: Any) -> Optional[str]:
    """One JSON value as the TEXT that Postgres will cast to the column's type.

    Everything travels as text and is cast to the column's real type in the
    statement, so this module needs no type map of its own and cannot disagree
    with the table about what a column is. NULL survives as NULL — which is the
    reason the export ships row_to_json rather than a CSV, and throwing the
    distinction away here would waste that.

    A dict or list is re-serialised rather than str()'d: `{'a': 1}` casts to
    jsonb, `"{'a': 1}"` does not.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, str):
        return value
    return str(value)


async def _column_types(conn, table: str) -> dict[str, str]:
    """{column: pg type expression} for a table that exists, else {}.

    Read from the catalog rather than assumed from the manifest: the manifest
    says which columns carry the app's data, never what type they are, and the
    table on this Host is the only authority on that.
    """
    rows = await conn.fetch(
        "SELECT a.attname AS name,"
        "       pg_catalog.format_type(a.atttypid, a.atttypmod) AS typ"
        "  FROM pg_catalog.pg_attribute a"
        " WHERE a.attrelid = to_regclass($1) AND a.attnum > 0"
        "   AND NOT a.attisdropped", table)
    return {r["name"]: r["typ"] for r in rows}


async def _primary_key(conn, table: str) -> list[str]:
    """The table's primary-key columns in index order, or [].

    This is what makes a second import a no-op instead of a second copy. It is
    read from the live table rather than declared in the manifest on purpose:
    the key is a property of the schema the app's own migration created, and a
    manifest that declared a different one would be a second source of truth
    that could drift from the index the database will actually enforce — and
    ON CONFLICT is resolved against that index, not against the declaration.
    """
    rows = await conn.fetch(
        "SELECT a.attname AS name"
        "  FROM pg_catalog.pg_index i"
        "  JOIN pg_catalog.pg_attribute a"
        "    ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
        " WHERE i.indrelid = to_regclass($1) AND i.indisprimary"
        " ORDER BY array_position(i.indkey, a.attnum)", table)
    return [r["name"] for r in rows]


def _upsert_sql(table: str, columns: list[str], types: dict[str, str],
                pkey: list[str], subject_column: str) -> str:
    """One parameterised INSERT ... ON CONFLICT for a row of `table`.

    THE CONFLICT CLAUSE IS THE SAFETY PROPERTY, not just the idempotency one.
    A site's row ids and this Host's row ids are independent sequences, so an
    incoming id can collide with a row that is already here and belongs to
    SOMEBODY ELSE. A bare DO UPDATE would overwrite it. The WHERE on the
    conflict target restricts the update to a stored row whose owner column is
    already this same subject, so a collision with another person's row writes
    nothing at all — and the caller counts those and reports them rather than
    letting a silent skip pass for a successful import.
    """
    cols = ", ".join(_quote(c) for c in columns)
    vals = ", ".join(f"${i + 1}::text::{types[c]}" for i, c in enumerate(columns))
    conflict = ", ".join(_quote(c) for c in pkey)
    # The key columns are not rewritten by the update — they are what matched.
    updatable = [c for c in columns if c not in pkey]
    if updatable:
        setters = ", ".join(f"{_quote(c)} = EXCLUDED.{_quote(c)}" for c in updatable)
        action = (f"DO UPDATE SET {setters}"
                  f" WHERE {_quote(table)}.{_quote(subject_column)}"
                  f" = EXCLUDED.{_quote(subject_column)}")
    else:
        # Every column is part of the key: there is nothing to update and the
        # row is already identical. DO NOTHING is the honest no-op.
        action = "DO NOTHING"
    return (f"INSERT INTO {_quote(table)} ({cols}) VALUES ({vals})"
            f" ON CONFLICT ({conflict}) {action} RETURNING 1")


def _plan_table(block: Any, declared: dict[str, TableSpec]) -> tuple:
    """(name, columns, subject_column) for one payload table, or raise.

    Everything the payload asserts about itself is checked against the INSTALLED
    manifest here, before any of it reaches the database.
    """
    if not isinstance(block, dict):
        raise ImportRefused("a table block in the payload is not an object")
    name = str(block.get("name") or "")
    bad = _bad_ident(name)
    if bad:
        raise ImportRefused(f"payload table name rejected: {bad}")
    spec = declared.get(name)
    if spec is None:
        raise ImportRefused(
            f"the payload carries table {name!r}, which the app installed on "
            f"this Host does not declare. Only the installed app's own declared "
            f"tables can be written, so nothing was imported")
    columns = [str(c) for c in (block.get("columns") or [])]
    if not columns:
        raise ImportRefused(f"the payload declares no columns for {name!r}")
    for c in columns:
        bad = _bad_ident(c)
        if bad:
            raise ImportRefused(f"payload column in {name!r} rejected: {bad}")
    extra = sorted(set(columns) - set(spec.columns))
    if extra:
        raise ImportRefused(
            f"the payload carries columns {extra} for {name!r} that the "
            f"installed app does not declare — refusing rather than writing "
            f"data into columns outside the contract")
    subject_column = str(block.get("subject_column") or "")
    if subject_column != spec.subject_column:
        raise ImportRefused(
            f"the payload says {name!r} is owned by {subject_column!r} but the "
            f"installed app declares {spec.subject_column!r}. The owner column "
            f"is the whole key of this operation and the two must agree")
    if subject_column not in columns:
        raise ImportRefused(
            f"{name!r} does not carry its own owner column {subject_column!r}")
    return name, columns, subject_column


def validate_payload(payload: Any, manifest: AppManifest,
                     *, expect_app_id: str = "") -> tuple:
    """(subject, [(table, columns, subject_column, rows)]) or raise ImportRefused.

    Pure — no database, no I/O — so the gate can exercise every refusal without
    a Postgres. That is deliberate: these refusals ARE the security property of
    this module, and a property that can only be tested against live
    infrastructure is one that gets tested once.
    """
    if not isinstance(payload, dict):
        raise ImportRefused("payload is not an object")
    subject = str(payload.get("subject") or "")
    if not subject:
        raise ImportRefused(
            "the payload names no subject. Every row this lands is written "
            "under the envelope's subject, so an absent one would write rows "
            "owned by nobody")
    app_id = str(payload.get("app_id") or "")
    if expect_app_id and app_id != expect_app_id:
        raise ImportRefused(
            f"the payload is for app {app_id!r}, not {expect_app_id!r}")
    if not manifest.data.bearing:
        raise ImportRefused(
            f"the app installed here declares no data tables, so there is "
            f"nowhere contracted for these rows to go")
    declared = _declared_tables(manifest)
    blocks = payload.get("tables")
    if not isinstance(blocks, list) or not blocks:
        raise ImportRefused("the payload carries no tables")
    plan = []
    seen = set()
    for block in blocks:
        name, columns, subject_column = _plan_table(block, declared)
        if name in seen:
            raise ImportRefused(f"the payload carries {name!r} twice")
        seen.add(name)
        rows = block.get("rows")
        if not isinstance(rows, list):
            raise ImportRefused(f"the rows for {name!r} are not a list")
        for r in rows:
            if not isinstance(r, dict):
                raise ImportRefused(f"a row of {name!r} is not an object")
        plan.append((name, columns, subject_column, rows))
    return subject, plan


async def import_payload(pool, payload: Any, manifest: AppManifest,
                         *, expect_app_id: str = "") -> dict:
    """Land a verified export. Returns what happened; raises ImportRefused.

    ONE TRANSACTION for the whole payload. A person's data is one thing, and a
    half-landed move — three tables in, the fourth refused — would leave a Host
    with rows that reference rows that are not there, and a retry that cannot
    tell what it already did. Either all of it lands or none of it does, and the
    retry is then simply the same import again.
    """
    subject, plan = validate_payload(payload, manifest,
                                     expect_app_id=expect_app_id)
    written = 0
    skipped = 0
    tables_done: list[str] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for table, columns, subject_column, rows in plan:
                types = await _column_types(conn, table)
                if not types:
                    raise ImportRefused(
                        f"this Host has no relation named {table!r}. The app "
                        f"declares it, so its own migration should have "
                        f"declared it too; the kernel does not add schema at "
                        f"runtime and will not do so here")
                missing = sorted(set(columns) - set(types))
                if missing:
                    raise ImportRefused(
                        f"{table!r} on this Host is missing the declared "
                        f"columns {missing}")
                for c in columns:
                    if not _PG_TYPE_RE.match(types[c]):
                        raise ImportRefused(
                            f"{table}.{c} has a type this importer will not "
                            f"interpolate ({types[c]!r})")
                pkey = await _primary_key(conn, table)
                if not pkey:
                    raise ImportRefused(
                        f"{table!r} has no primary key, so the same row cannot "
                        f"be recognised twice and a repeated import would "
                        f"duplicate every row. Give the table a primary key in "
                        f"the app's own migration")
                if set(pkey) - set(columns):
                    raise ImportRefused(
                        f"{table!r} is keyed on {sorted(set(pkey) - set(columns))}, "
                        f"which the app does not declare as data columns — the "
                        f"import cannot supply the key it would be matched on")
                sql = _upsert_sql(table, columns, types, pkey, subject_column)
                for row in rows:
                    args = []
                    for c in columns:
                        # THE OWNER COLUMN IS NOT READ FROM THE ROW. See the
                        # module docstring: the envelope's subject is the one
                        # the controller decided on, and it is the only value
                        # allowed to own anything this import writes.
                        args.append(subject if c == subject_column
                                    else _coerce(row.get(c)))
                    got = await conn.fetchval(sql, *args)
                    if got:
                        written += 1
                    else:
                        skipped += 1
                tables_done.append(table)
    if skipped:
        log.warning(
            "subject import: %d row(s) not written — the primary key already "
            "belongs to a different subject on this Host", skipped)
    return {"tables": tables_done, "rows_imported": written,
            "rows_skipped": skipped, "subject_rows": sum(len(p[3]) for p in plan)}
