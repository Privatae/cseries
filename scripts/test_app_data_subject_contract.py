#!/usr/bin/env python3
# gate-scope: whole-tree — every system app manifest
"""Gate: a data-bearing app's tables all carry the owner-subject column.

    APP_ROOT=<repo> python3 scripts/test_app_data_subject_contract.py

WHAT IS BEING HELD. An app that keeps rows belonging to a person must say which
tables those rows live in and which column names the Authentik subject who owns
each row. That declaration is what makes a move between databases KEYED: the
export selects `WHERE <subject_column> = $1` and carries one person's rows.
Without the column there is no selection, and the only remaining "move" is the
whole table — which is every other member's rows. So a table declared without
its ownership key is not a smaller version of the contract, it is the failure
the contract exists to prevent, and it is refused.

WHY A REAL VALIDATOR RUN AND NOT A FIXTURE. A test that supplies the value
cannot test the code that produces it. So every case below is fed to the
SHIPPED `data_contract_violations` (and, for the end-to-end cases, through
`manifest.load` reading a real YAML file off disk and through
`build_contract.validate_publish` reading a real packed bundle). Nothing here
re-implements the rule, and nothing asserts against a hand-written expectation
of what the validator "should" have said beyond the substring that identifies
the refusal.

BOTH DIRECTIONS. The accept cases are load-bearing: a gate that only proves
refusals would stay green if the validator started refusing every app, which
would take the whole store down. Each refusal case is paired with the nearest
accepting manifest, so what is under test is the DISTINCTION, not the mood.

Runs container-free: imports the two modules directly, writes manifests into a
tempfile tree, builds bundle zips in memory. No container, no DB, no network,
no server.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from server.applications.build_contract import validate_publish  # noqa: E402
from server.applications.manifest import (  # noqa: E402
    DECLARABLE_KEYS, DataSpec, ManifestError, data_contract_violations, load,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"OK    {name}")
    else:
        print(f"FAIL  {name} — {detail}")
        failures.append(name)


def refuses(name: str, block, needle: str) -> None:
    """The validator must refuse `block`, and say why in words naming the cause."""
    errs = data_contract_violations({"id": "t", "data": block})
    check(name, bool(errs) and any(needle in e for e in errs),
          f"needle {needle!r} not in {errs!r}")


def accepts(name: str, block) -> None:
    errs = data_contract_violations({"id": "t", "data": block})
    check(name, errs == [], f"unexpected violations: {errs!r}")


# ── the schema field is real and author-facing ─────────────────────────────
check("`data` is a declarable manifest key", "data" in DECLARABLE_KEYS,
      f"DECLARABLE_KEYS={sorted(DECLARABLE_KEYS)}")
check("the default subject column is owner_subject",
      DataSpec().subject_column == "owner_subject", repr(DataSpec().subject_column))
check("an app with no data block is not data-bearing",
      DataSpec().bearing is False)

# ── not declaring data stays legal ─────────────────────────────────────────
# The overwhelming majority of apps keep nothing but their own config. If this
# case ever started failing, the gate would refuse every shipped app.
check("a manifest with no `data` block is accepted",
      data_contract_violations({"id": "t", "name": "T"}) == [])

# ── THE CHECK ──────────────────────────────────────────────────────────────
accepts("a table whose columns include the default subject column",
        {"tables": [{"name": "notes",
                     "columns": ["id", "owner_subject", "title", "body"]}]})
refuses("a table whose columns OMIT the subject column",
        {"tables": [{"name": "notes", "columns": ["id", "title", "body"]}]},
        "not one of its columns")
accepts("a manifest-wide subject_column override, present on the table",
        {"subject_column": "author_sub",
         "tables": [{"name": "posts", "columns": ["id", "author_sub", "body"]}]})
refuses("an override the table does not carry",
        {"subject_column": "author_sub",
         "tables": [{"name": "posts", "columns": ["id", "owner_subject"]}]},
        "not one of its columns")
accepts("a per-table override of the manifest-wide default",
        {"subject_column": "owner_subject",
         "tables": [{"name": "a", "columns": ["id", "owner_subject"]},
                    {"name": "b", "columns": ["id", "who"],
                     "subject_column": "who"}]})
refuses("ONE table of several missing the key still refuses",
        {"tables": [{"name": "a", "columns": ["id", "owner_subject"]},
                    {"name": "b", "columns": ["id", "body"]}]},
        "not one of its columns")

# ── the ways of opting out of the key, each closed ─────────────────────────
refuses("an empty subject_column", {"subject_column": "",
                                    "tables": [{"name": "a", "columns": ["id"]}]},
        "data.subject_column must be a bare lowercase identifier")
refuses("a null subject_column", {"subject_column": None,
                                  "tables": [{"name": "a", "columns": ["id"]}]},
        "data.subject_column must be a bare lowercase identifier")
refuses("a quoted/qualified subject_column",
        {"subject_column": "public.owner",
         "tables": [{"name": "a", "columns": ["id"]}]},
        "data.subject_column must be a bare lowercase identifier")
refuses("a table with no columns list at all",
        {"tables": [{"name": "a"}]}, "columns must be a non-empty list")
refuses("a table with an empty columns list",
        {"tables": [{"name": "a", "columns": []}]},
        "columns must be a non-empty list")
refuses("data.tables empty", {"tables": []}, "remove the `data` block")
refuses("data.tables missing entirely", {"subject_column": "owner_subject"},
        "declares no tables")

# ── shape refusals that keep the declaration machine-usable ────────────────
refuses("a schema-qualified table name",
        {"tables": [{"name": "public.notes", "columns": ["owner_subject"]}]},
        "bare lowercase identifier")
refuses("an uppercase table name",
        {"tables": [{"name": "Notes", "columns": ["owner_subject"]}]},
        "bare lowercase identifier")
refuses("a table name with a quote in it",
        {"tables": [{"name": 'n"; drop table x --', "columns": ["owner_subject"]}]},
        "bare lowercase identifier")
refuses("a column name with a quote in it",
        {"tables": [{"name": "n", "columns": ["owner_subject", "a\"b"]}]},
        "invalid name")
refuses("the same table declared twice",
        {"tables": [{"name": "n", "columns": ["owner_subject"]},
                    {"name": "n", "columns": ["owner_subject"]}]},
        "declared twice")
refuses("a duplicated column",
        {"tables": [{"name": "n", "columns": ["owner_subject", "owner_subject"]}]},
        "more than once")
refuses("an unknown key inside data", {"nope": 1, "tables": []}, "unknown data key(s)")
refuses("an unknown key inside a table",
        {"tables": [{"name": "n", "columns": ["owner_subject"], "nope": 1}]},
        "unknown key(s)")
refuses("data that is not a mapping", ["notes"], "data must be a mapping")
refuses("a table that is not a mapping",
        {"tables": ["notes"]}, "must be a mapping with a name and columns")

# ── PostgreSQL reserved words ──────────────────────────────────────────────
# Step 4 interpolates these names into an UNQUOTED
# `COPY <table> (<columns>) ... WHERE <subject_column> = $1`. A table called
# `user` or a column called `order` passes every shape check, ships, and then
# fails at the first COPY — the one operation this whole contract exists to
# enable, broken by a name nobody looked at.
from server.applications.manifest import PG_RESERVED_WORDS  # noqa: E402

check("the reserved-word list is populated", len(PG_RESERVED_WORDS) > 50,
      f"{len(PG_RESERVED_WORDS)} words")
for w in ("user", "order", "table", "select", "from", "where", "default",
          "check", "column", "primary", "references", "group", "limit"):
    check(f"{w!r} is known to be reserved", w in PG_RESERVED_WORDS)

refuses("a table named with a reserved word (`user`)",
        {"tables": [{"name": "user", "columns": ["id", "owner_subject"]}]},
        "reserved word")
refuses("a table named `table`",
        {"tables": [{"name": "table", "columns": ["owner_subject"]}]},
        "reserved word")
refuses("a COLUMN named with a reserved word (`order`)",
        {"tables": [{"name": "notes",
                     "columns": ["id", "order", "owner_subject"]}]},
        "reserved word")
refuses("a subject_column named with a reserved word",
        {"subject_column": "user", "tables": [{"name": "n", "columns": ["user"]}]},
        "reserved word")
refuses("a per-table subject_column that is reserved",
        {"tables": [{"name": "n", "columns": ["id", "user"],
                     "subject_column": "user"}]},
        "reserved word")

# The other direction, and it matters more than the refusals: over-refusing
# would tax every app for nothing. PostgreSQL's NON-reserved words and its
# "reserved (can be function or type name)" class are ordinary identifiers.
accepts("the common non-reserved words are still usable as columns",
        {"tables": [{"name": "notes",
                     "columns": ["id", "name", "value", "key", "text", "type",
                                 "data", "status", "owner_subject"]}]})
accepts("a table named `notes`, `posts`, `items`",
        {"tables": [{"name": "notes", "columns": ["owner_subject"]},
                    {"name": "posts", "columns": ["owner_subject"]},
                    {"name": "items", "columns": ["owner_subject"]}]})
for w in ("name", "value", "key", "text", "type", "data", "status", "id",
          "owner", "email", "title", "body", "created_at"):
    check(f"{w!r} is NOT treated as reserved", w not in PG_RESERVED_WORDS)


# ── end to end through manifest.load() on a real file ──────────────────────
_BASE = {"id": "probe-app", "name": "Probe", "entrypoint": "index.html"}


def _write(tmp: Path, block=None) -> Path:
    raw = dict(_BASE)
    if block is not None:
        raw["data"] = block
    p = tmp / "app.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    m = load(_write(tmp, {"tables": [
        {"name": "notes", "columns": ["id", "owner_subject", "body"]}]}))
    check("load() parses the block into a DataSpec",
          m.data.bearing and len(m.data.tables) == 1
          and m.data.tables[0].name == "notes"
          and m.data.tables[0].subject_column == "owner_subject"
          and m.data.tables[0].columns == ("id", "owner_subject", "body"),
          repr(m.data))
    m2 = load(_write(tmp))
    check("load() leaves a data-less app not data-bearing",
          m2.data.bearing is False and m2.data.tables == (), repr(m2.data))
    try:
        load(_write(tmp, {"tables": [{"name": "notes", "columns": ["id", "body"]}]}))
        check("load() RAISES on a table missing the key", False,
              "no ManifestError raised")
    except ManifestError as e:
        check("load() RAISES on a table missing the key",
              "not one of its columns" in str(e), str(e))

# ── end to end through validate_publish() on a real bundle ─────────────────
# The publish bar reads the packed manifest.yaml, so this proves the store path
# refuses independently of whether the app was ever loaded.
def _bundle(block) -> bytes:
    raw = dict(_BASE)
    raw.update({"runtime": {"kind": "static"}, "validated_under_sdk":
                {"version": "0", "hash": "0" * 64}})
    if block is not None:
        raw["data"] = block
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.yaml", yaml.safe_dump(raw))
        z.writestr("payload/index.html", "<!doctype html>")
    return buf.getvalue()


_bad = validate_publish(_bundle(
    {"tables": [{"name": "notes", "columns": ["id", "body"]}]}))
check("validate_publish refuses a store app whose table lacks the key",
      any("app-data contract" in e and "not one of its columns" in e
          for e in _bad), repr(_bad))
_good = validate_publish(_bundle(
    {"tables": [{"name": "notes", "columns": ["id", "owner_subject", "body"]}]}))
check("validate_publish raises no data-contract error on a compliant app",
      not any("app-data contract" in e for e in _good), repr(_good))
_none = validate_publish(_bundle(None))
check("validate_publish raises no data-contract error on a data-less app",
      not any("app-data contract" in e for e in _none), repr(_none))

# ── every shipped app.yaml still validates ─────────────────────────────────
# The blast radius check. `data` is optional with a default, so no existing
# manifest should have gained a violation; if one has, this names it rather
# than letting the store find out.
shipped = sorted(ROOT.glob("system-apps/*/app.yaml"))
check("there are shipped manifests to check", len(shipped) > 0, f"{ROOT}")
for p in shipped:
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:                                        # noqa: BLE001
        check(f"{p.parent.name}/app.yaml parses", False, str(e))
        continue
    errs = data_contract_violations(raw)
    check(f"{p.parent.name}/app.yaml has no data-contract violation",
          errs == [], repr(errs))

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nall green")
