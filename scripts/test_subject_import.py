#!/usr/bin/env python3
"""Gate: the subject importer writes only what the installed app declares.

    APP_ROOT=<repo> python3 scripts/test_subject_import.py

WHAT IS BEING HELD. A payload arrives over the network carrying one person's
rows from a public site, and this Host writes them into its own tables. The
controller has already decided WHO — it compares two Authentik subjects and
refuses a move where the requesting Host's owner is not that site member — so
what is still open by the time the importer runs is SCOPE: what the payload is
allowed to touch, and whose rows it can end up owning.

Four properties, and each one is a refusal the importer must make rather than a
feature it must have:

  1. A table the INSTALLED app does not declare is not written. The store's
     manifest could be newer, older or tampered with; the app installed here is
     the authority on what exists.
  2. A column outside the declared list is not written, for the same reason.
  3. The owner column the payload claims must be the one the app declares —
     they are the key of the whole operation and a disagreement is not
     something to split the difference on.
  4. Every row's owner column is OVERWRITTEN with the envelope's subject. A
     payload whose rows name somebody else then lands under the mover, which is
     a no-op for an honest export and the only safe reading of a dishonest one.

AND TWO PROPERTIES OF THE SQL ITSELF, checked by reading the statement the
module builds rather than by running it:

  5. Every interpolated identifier is double-quoted and every VALUE is a bind
     parameter. There is no bind parameter for a table name, so the names ARE
     interpolated; that is exactly why the quoting is asserted here.
  6. The conflict clause updates only where the stored row is already this
     subject's. Site row ids and Host row ids are independent sequences, so an
     incoming id CAN collide with a row already here belonging to someone else,
     and a bare DO UPDATE would overwrite it.

WHY NO DATABASE. The four refusals live in `validate_payload`, which is pure by
construction for this reason: a security property that can only be exercised
against a live Postgres is one that gets exercised once. The two SQL properties
are read off `_upsert_sql`, also pure. What is NOT covered here is stated
plainly at the bottom rather than implied by silence.

BOTH DIRECTIONS. Every refusal is paired with the nearest ACCEPTING payload.
A gate that only proved refusals would stay green if the importer started
refusing everything, which is the failure that delivers nothing while looking
perfectly safe.

Runs container-free: imports the module directly, builds manifests in memory.
No container, no DB, no network, no server.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from server.applications.manifest import (  # noqa: E402
    AppManifest, DataSpec, TableSpec,
)
from server.applications.subject_import import (  # noqa: E402
    ImportRefused, _upsert_sql, validate_payload,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"OK    {name}")
    else:
        print(f"FAIL  {name} — {detail}")
        failures.append(name)


SUBJECT = "a" * 64


def manifest_with(*tables: TableSpec) -> AppManifest:
    """An installed manifest declaring exactly `tables`."""
    return AppManifest(id="notes", name="Notes", entrypoint="index.html",
                       data=DataSpec(tables=tuple(tables)))


NOTES = TableSpec(name="notes", columns=("id", "owner_subject", "body"),
                  subject_column="owner_subject")

GOOD_MANIFEST = manifest_with(NOTES)


def payload(tables, subject=SUBJECT, app_id="notes"):
    return {"move_id": 1, "subject": subject, "app_id": app_id,
            "target_entity_id": 9001, "tables": tables}


def good_block(rows=None):
    return {"name": "notes", "columns": ["id", "owner_subject", "body"],
            "subject_column": "owner_subject",
            "rows": rows if rows is not None else
            [{"id": 1, "owner_subject": SUBJECT, "body": "hi"}]}


def refuses(name: str, p, needle: str) -> None:
    """validate_payload must refuse `p`, and say why in words naming the cause."""
    try:
        validate_payload(p, GOOD_MANIFEST, expect_app_id="notes")
    except ImportRefused as e:
        check(name, needle in str(e), f"needle {needle!r} not in {str(e)!r}")
        return
    check(name, False, "the payload was ACCEPTED")


def accepts(name: str, p, manifest=None) -> tuple:
    try:
        out = validate_payload(p, manifest or GOOD_MANIFEST, expect_app_id="notes")
    except ImportRefused as e:
        check(name, False, f"refused: {e}")
        return "", []
    check(name, True)
    return out


print("── the accepting case, first: a gate that only refuses is not a gate ──")
subject, plan = accepts("a declared table with declared columns is accepted",
                        payload([good_block()]))
check("the accepted plan carries the envelope's subject", subject == SUBJECT,
      repr(subject))
check("the accepted plan carries one table with one row",
      len(plan) == 1 and plan[0][0] == "notes" and len(plan[0][3]) == 1,
      repr(plan))

print("\n── property 1: an undeclared table is not written ──")
refuses("a table the installed app does not declare is refused",
        payload([{"name": "secrets", "columns": ["id", "owner_subject"],
                  "subject_column": "owner_subject", "rows": []}]),
        "does not declare")
try:
    validate_payload(payload([good_block()]), manifest_with(),
                     expect_app_id="notes")
    check("an app declaring no data tables refuses the payload", False,
          "ACCEPTED")
except ImportRefused as e:
    check("an app declaring no data tables refuses the payload",
          "no data tables" in str(e), str(e))

print("\n── property 2: an undeclared column is not written ──")
refuses("a column outside the declared list is refused",
        payload([{"name": "notes",
                  "columns": ["id", "owner_subject", "body", "admin_note"],
                  "subject_column": "owner_subject", "rows": []}]),
        "does not declare")
accepts("a SUBSET of the declared columns is accepted",
        payload([{"name": "notes", "columns": ["id", "owner_subject"],
                  "subject_column": "owner_subject",
                  "rows": [{"id": 1, "owner_subject": SUBJECT}]}]))

print("\n── property 3: the owner column must be the declared one ──")
refuses("a payload naming a different owner column is refused",
        payload([{"name": "notes", "columns": ["id", "owner_subject", "body"],
                  "subject_column": "body", "rows": []}]),
        "owner column")
refuses("a payload whose owner column is not among its own columns is refused",
        payload([{"name": "notes", "columns": ["id", "body"],
                  "subject_column": "owner_subject", "rows": []}]),
        "does not carry its own owner column")

print("\n── the envelope itself ──")
refuses("a payload with no subject is refused",
        payload([good_block()], subject=""), "names no subject")
refuses("a payload for a different app is refused",
        payload([good_block()], app_id="other"), "not 'notes'")
refuses("a payload with no tables is refused", payload([]), "no tables")
refuses("the same table twice is refused",
        payload([good_block(), good_block()]), "twice")
refuses("a row that is not an object is refused",
        payload([good_block(rows=["not-an-object"])]), "not an object")

print("\n── identifiers go through the manifest's own fence ──")
# Not a re-implementation of the fence: this asserts the importer CONSULTS it,
# by feeding a name the shipped validator rejects and requiring the refusal to
# quote that validator's own words.
refuses("a reserved word as a table name is refused",
        payload([{"name": "user", "columns": ["id", "owner_subject"],
                  "subject_column": "owner_subject", "rows": []}]),
        "reserved word")
refuses("a non-bare identifier as a table name is refused",
        payload([{"name": "Public.Notes", "columns": ["id", "owner_subject"],
                  "subject_column": "owner_subject", "rows": []}]),
        "bare lowercase identifier")

print("\n── properties 5 and 6: the statement the module builds ──")
sql = _upsert_sql("notes", ["id", "owner_subject", "body"],
                  {"id": "integer", "owner_subject": "text", "body": "text"},
                  ["id"], "owner_subject")
check("every interpolated identifier is double-quoted",
      '"notes"' in sql and '"owner_subject"' in sql and '"body"' in sql
      and '"id"' in sql, sql)
check("no bare identifier leaks into the statement",
      " notes " not in sql.replace('"notes"', '"x"'), sql)
check("every value is a bind parameter, never a literal",
      "$1::text::integer" in sql and "$2::text::text" in sql
      and "$3::text::text" in sql, sql)
check("the conflict target is the primary key",
      "ON CONFLICT (\"id\")" in sql, sql)
check("the update is restricted to a row already owned by this subject",
      'WHERE "notes"."owner_subject" = EXCLUDED."owner_subject"' in sql, sql)
check("the key column is not itself rewritten by the update",
      '"id" = EXCLUDED."id"' not in sql, sql)
check("the statement reports whether it wrote", sql.endswith("RETURNING 1"), sql)

# A table whose every column is part of the key has nothing to update, and
# DO UPDATE with an empty SET list is a syntax error rather than a no-op.
sql_all_key = _upsert_sql("pins", ["owner_subject", "tag"],
                          {"owner_subject": "text", "tag": "text"},
                          ["owner_subject", "tag"], "owner_subject")
check("an all-key table becomes DO NOTHING rather than an empty SET",
      "DO NOTHING" in sql_all_key and "DO UPDATE" not in sql_all_key,
      sql_all_key)

print("\n── property 4: the owner column is forced, not read from the row ──")
# The forcing itself happens in import_payload, which needs a connection. What
# is checkable here is that validate_payload does NOT reject a row carrying a
# foreign subject — it must pass validation and be REWRITTEN, because refusing
# it would make a hostile payload indistinguishable from a malformed one and
# would leave the honest half of the export unlanded.
_s, _plan = accepts(
    "a row naming a foreign subject passes validation (it is rewritten, not refused)",
    payload([good_block(rows=[{"id": 1, "owner_subject": "b" * 64, "body": "x"}])]))
check("validation hands the ENVELOPE's subject to the writer, not the row's",
      _s == SUBJECT, repr(_s))

# NOT COVERED HERE, stated rather than left to be discovered: the forcing write
# itself, the missing-relation and missing-primary-key refusals, and the
# transaction boundary all need a live connection. They are exercised by the
# platform invariant's must_work leg (intent app-data-subject-keyed), which runs
# a real move end to end. This gate holds the half that can be held honestly
# without one.

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nall green")
