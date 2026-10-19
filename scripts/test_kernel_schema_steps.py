#!/usr/bin/env python3
"""scripts/kernel_schema.py builds the schema by RUNNING entrypoint.sh's own
schema section — and refuses when that section could run differently in a gate
than at boot.

Every DB gate trusts kernel_schema.py's schema. Until 2026-10-02 it carried a
Python copy of entrypoint.sh's schema steps, held to the shell by a pin that
read entrypoint.sh command by command, and each review found a shell shape the
pin read differently from dash (review of preview-animated at 6c42a5ed: an
escaped space before `#`, or an escaped backslash at a line end, hid a
`continue` in the migration loop). Now the gate runs the boot's own lines with
the boot's own shell, and these are the checks that it does:

  A. today's entrypoint.sh: the section is marked once, between the restore
     swap and the restore carry, and runs as it stands;
  B. the runner runs THAT text with dash (a stand-in psql records each call):
     init-db.sql in one transaction, the entity's id before any migration,
     v139 asserted, then every migration once, all on the test database as
     role entity, with none of the gate's own environment (a PGOPTIONS set in
     the gate does not reach psql). The two shapes the last review found now
     skip the migrations in the gate exactly as dash skips them at boot — and
     the check accepts them, because they are the boot's text;
  C. what the section inherits from the lines above it is held: an exported
     PGOPTIONS (however it is spelled), any other PG*/PSQL*/PATH/HOME/IFS/LD_*,
     ENTITY_DB, ENTITY_ID or PG_SOCKET reassigned, read into, made readonly or
     set by getopts (however the line is split, quoted or hidden),
     $KERNEL_SCHEMA_APP set at boot, shell options other than -eu, a function
     or alias, the section inside a heredoc / if / function, a marker missing,
     doubled, indented or reversed, the swap or carry on the wrong side, and a
     section that reads a variable set above it through a default or in
     $(( )) — each REFUSES. A comment or log line above the section, and any
     edit inside it, does not;
  D. `upto=N` is the schema a Host had before vN existed: the tree just before
     vN's file entered the first-parent history (for a migration that came in
     by a merge, the merge's first parent — main as it stood, not the branch
     commit's parent; HEAD when vN is not committed), with vN and every later
     migration removed from it, and it REFUSES rather than fall back to
     today's files when the history is not there (a bare copy, a shallow
     clone). A TRUE rename (the old file deleted in the same commit: a
     renumber, or a new name within the number) is followed to the old file's
     first add; a copy (the source kept) dates from its own add. Today's
     init-db.sql already carries v165's 'app_delivery'; the tree before v165
     does not;
  E. a template is named for the entity id it was built for, and for `upto`,
     the version and the commit — so an entity-9003 schema is never an entity-1
     clone renumbered afterwards.

Container-free: text, git, dash and temp directories only. The database half
(the real build, the entity id before the migrations seed, stale templates
dropped) is scripts/test_kernel_schema_db.py.

Run:  python3 scripts/test_kernel_schema_steps.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))
import kernel_schema as ks  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


SRC = (ROOT / "entrypoint.sh").read_text()
work = Path(tempfile.mkdtemp(prefix="kernel-steps-"))


def refusal(text: str) -> list[str]:
    with tempfile.TemporaryDirectory(dir=work) as d:
        (Path(d) / "entrypoint.sh").write_text(text)
        return ks.entrypoint_matches(Path(d))


def sub(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new, 1)


def above(line: str, text: str = SRC) -> str:
    """`line` added just above the section's opening marker."""
    return sub(text, ks.BEGIN + "\n", line + "\n" + ks.BEGIN + "\n")


def around(before: str, after: str) -> str:
    """The section wrapped: `before` above its opening marker, `after` below its closing one."""
    return sub(sub(SRC, ks.BEGIN + "\n", before + "\n" + ks.BEGIN + "\n"), ks.END + "\n", ks.END + "\n" + after + "\n")


# A stand-in psql: records every call (and what it sees of its environment)
# and answers the section's two questions — is the base schema there (not
# until init-db.sql has loaded) and is a version applied (never). With
# {state}/fail present, creating the ledger fails, as a dropped connection would.
STUB = """#!/bin/sh
printf '%s\\n' "PGOPTIONS=${{PGOPTIONS-unset}} HOME=$HOME PGSYSCONFDIR=${{PGSYSCONFDIR-unset}} PGPORT=${{PGPORT-unset}} \
ARGS=$(printf '%s' "$*" | tr '\\n' ' ')" >> {log}
case "$*" in *"CREATE TABLE IF NOT EXISTS schema_migrations"*) [ -e {state}/fail ] && exit 3 ;; esac
case "$*" in *--single-transaction*) : > {state}/loaded ;; esac
case "$*" in *"to_regclass('public.entity_registry')"*) if [ -e {state}/loaded ]; then echo t; else echo f; fi ;; esac
exit 0
"""
MIGS = ("v001_a.sql", "v002_b.sql", "v139_host_recall_role.sql")
DSN = "postgresql://entity@/kdb?host=/nonexistent/kernel-sock"


def stubbed(text: str, eid: int = 3, fail: bool = False, dsn: str = DSN) -> list[str]:
    """Run the section of entrypoint.sh text `text` through ks.run_section
    with the stand-in psql: each psql call's log line. `fail`: creating the
    ledger fails."""
    with tempfile.TemporaryDirectory(dir=work) as d:
        d = Path(d)
        root, app, stub, state = d / "root", d / "app", d / "bin", d / "state"
        for p in (root, app / "migrations", stub, state):
            p.mkdir(parents=True)
        (root / "entrypoint.sh").write_text(text)
        (app / "init-db.sql").write_text("-- fixture\n")
        for m in MIGS:
            (app / "migrations" / m).write_text("SELECT 1;\n")
        log = d / "psql.log"
        (stub / "psql").write_text(STUB.format(log=log, state=state))
        (stub / "psql").chmod(0o755)
        if fail:
            (state / "fail").touch()
        gate_env = {"PGOPTIONS": "-c search_path=gate_leak", "PGSYSCONFDIR": str(d)}   # the gate's own
        prior = {k: os.environ.get(k) for k in gate_env}
        os.environ.update(gate_env)
        try:
            ks.run_section(dsn, app, eid, root, path=f"{stub}:{os.environ['PATH']}")
        finally:
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        calls = log.read_text().splitlines()
        return [c.replace(str(app) + "/", "APP/") for c in calls]


def files_run(calls: list[str]) -> list[str]:
    out = []
    for c in calls:
        words = c.split(" ARGS=", 1)[1].split()
        out += [words[i + 1].replace("APP/", "") for i, w in enumerate(words[:-1]) if w == "-f"]
    return out


try:
    print("A. today's entrypoint.sh: the section is marked, in place, and runs as it stands")
    check("entrypoint.sh's section can be run as the boot runs it", ks.entrypoint_matches(ROOT) == [],
          ks.entrypoint_matches(ROOT))
    section, first = ks.schema_section(ROOT)
    lines = SRC.splitlines()
    check("the section is the text between the markers, from the schema probe through the migration loop",
          lines[first - 2] == ks.BEGIN and "schema_present=\"\"" in section
          and "for f in ${KERNEL_SCHEMA_APP:-/app}/migrations/v*.sql; do" in section
          and section.rstrip().endswith("fi") and ks.SWAP not in section and ks.CARRY not in section,
          section[:200])
    check("the restore swap is above it and the carry below it",
          SRC.index(ks.SWAP) < SRC.index(ks.BEGIN) < SRC.index(ks.END) < SRC.index(ks.CARRY))

    print("B. the runner runs the boot's own lines with dash")
    calls = stubbed(SRC)
    check("every psql call is on the test database as role entity, through the section's own -h/-U/-d",
          calls and all("-h /nonexistent/kernel-sock -U entity -d kdb" in c for c in calls), calls[:3])
    check("init-db.sql in one transaction, v139 asserted, then every migration once, in glob order",
          files_run(calls) == ["init-db.sql", "migrations/v139_host_recall_role.sql", "migrations/v001_a.sql",
                               "migrations/v002_b.sql", "migrations/v139_host_recall_role.sql"]
          and any("--single-transaction -f APP/init-db.sql" in c for c in calls), files_run(calls))
    renumber = next((i for i, c in enumerate(calls) if "UPDATE entity_registry SET id = 3" in c), None)
    first_mig = next((i for i, c in enumerate(calls) if "-f APP/migrations/v001_a.sql" in c), None)
    check("the entity's own id lands before any migration",
          renumber is not None and first_mig is not None and renumber < first_mig, (renumber, first_mig))
    recorded = [c.split("VALUES ", 1)[1] for c in calls if "INSERT INTO schema_migrations(version, name) VALUES (" in c]
    check("each migration is recorded as it lands",
          recorded == ["(1, 'v001_a')", "(2, 'v002_b')", "(139, 'v139_host_recall_role')"], recorded)
    def psql_env(call: str) -> dict[str, str]:
        return dict(kv.split("=", 1) for kv in call.split(" ARGS=", 1)[0].split())
    run_dir = f"{tempfile.gettempdir()}/kernel-schema-run-"
    check("psql gets none of the gate's environment: a PGOPTIONS or PGSYSCONFDIR set in the gate does not reach "
          "it, and HOME and PGSYSCONFDIR are the runner's scratch (no .psqlrc / .pgpass / system psqlrc of this "
          "machine)",
          bool(calls) and all(e["PGOPTIONS"] == "unset" and e["HOME"].startswith(run_dir)
                              and e["PGSYSCONFDIR"] == e["HOME"] for e in map(psql_env, calls)), calls[:2])
    tcp = stubbed(SRC, dsn="postgresql://entity:s3cret@127.0.0.1:55432/kdb")
    check("a TCP DSN: the section's psql gets the host as -h and the port as PGPORT",
          bool(tcp) and all("-h 127.0.0.1 -U entity -d kdb" in c and psql_env(c)["PGPORT"] == "55432" for c in tcp),
          tcp[:2])
    linked0 = stubbed(SRC, eid=0)
    check("a genesis entity (id 0) keeps the row at 1: no UPDATE, as at boot",
          not any("UPDATE entity_registry" in c for c in linked0))
    try:
        stubbed(SRC, fail=True)
        check("a failing step stops the build, as `set -e` stops the boot", False, "built")
    except ks.SchemaError as e:
        check("a failing step stops the build, as `set -e` stops the boot", "exit 3" in str(e), e)
    unset_at = 'if [ "$schema_present" != "t" ]; then\n'
    unset_line = SRC[:SRC.index(unset_at)].count("\n") + 1
    try:
        stubbed(sub(SRC, unset_at, 'echo "$NOT_SET_ANYWHERE"\n' + unset_at))
        check("a variable nobody set stops the build, as `set -u` stops the boot", False, "built")
    except ks.SchemaError as e:
        check("a variable nobody set stops the build, as `set -u` stops the boot — and says entrypoint.sh's line",
              f": {unset_line}: NOT_SET_ANYWHERE: parameter not set" in str(e), e)
    LOG = '        echo "Applying migration: $base"\n'
    review = {
        "an escaped space before # hiding `; continue`": sub(SRC, LOG, '        echo "Applying migration: $base"\\ #; continue\n'),
        "an escaped backslash at a line end, then `continue`":
            sub(SRC, LOG, '        echo "Applying migration: $base" \\\\\n        continue\n'),
    }
    for label, text in review.items():
        got = stubbed(text)
        check(f"review of 6c42a5ed — {label}: the gate skips every migration, exactly as dash does at boot",
              files_run(got) == ["init-db.sql", "migrations/v139_host_recall_role.sql"], files_run(got))
        check(f"...and the check accepts it: it is the boot's own text ({label})", refusal(text) == [],
              refusal(text))
    try:
        ks._conn_env("postgresql://postgres@127.0.0.1:5432/kdb")
        check("a DSN naming another role refuses (the section connects as entity)", False, "accepted")
    except ks.SchemaError as e:
        check("a DSN naming another role refuses (the section connects as entity)", "role entity" in str(e), e)
    try:
        ks._conn_env("postgresql://entity@/kdb")
        check("a DSN with no host refuses (never psql's default socket: a Host's own database)", False, "accepted")
    except ks.SchemaError as e:
        check("a DSN with no host refuses (never psql's default socket: a Host's own database)",
              "no host" in str(e), e)
    try:
        ks._conn_env("postgresql://entity@h/kdb?options=-csearch_path%3Dx")
        check("a DSN parameter psql cannot be given refuses", False, "accepted")
    except ks.SchemaError as e:
        check("a DSN parameter psql cannot be given refuses", "options" in str(e), e)
    try:
        ks.run_section(DSN, work / "a b", 1, ROOT)
        check("an app path the section would split (a space) refuses", False, "ran")
    except ks.SchemaError as e:
        check("an app path the section would split (a space) refuses", "unquoted" in str(e), e)
    # The runner's own scratch is expanded unquoted too ($KERNEL_SCHEMA_TMP):
    # a TMPDIR with a space refuses before the section runs (no psql on PATH,
    # so a runner without the check fails for another reason).
    spaced, prior_tmp = work / "tmp with space", tempfile.gettempdir()
    spaced.mkdir()
    tempfile.tempdir = str(spaced)
    try:
        ks.run_section(DSN, work, 1, ROOT, path="/nonexistent")
        check("a scratch path the section would split (TMPDIR with a space) refuses", False, "ran")
    except ks.SchemaError as e:
        check("a scratch path the section would split (TMPDIR with a space) refuses", "scratch path" in str(e), e)
    finally:
        tempfile.tempdir = prior_tmp
    env = ks._conn_env("postgresql://entity:s3cret@%2Fsrv%2FSock/kdb?sslmode=disable")
    check("a socket directory, password and sslmode reach the section's psql",
          env == {"PG_SOCKET": "/srv/Sock", "ENTITY_DB": "kdb", "PGPASSWORD": "s3cret", "PGSSLMODE": "disable"}, env)

    print("C. what the section inherits from above it is held")
    swap_block = ("    python3 -m server.host_restore swap \"$ENTITY_DB\" || _restore_rc=$?\n")
    carry_block = ('if [ "$_restore_armed" = 1 ]; then\n    python3 -m server.host_restore carry "$ENTITY_DB"\nfi\n')
    mutants = {
        "an exported PGOPTIONS": above('export PGOPTIONS="-c search_path=scratch"'),
        "PGOPTIONS set, then exported on its own": above("PGOPTIONS=-csearch_path=scratch; export PGOPTIONS"),
        "an export whose name is split by quotes": above('export PG""OPTIONS=-csearch_path=scratch'),
        "a PG* variable set without export (the pod may already export it)": above("PGHOST=/elsewhere"),
        "PATH changed": above("PATH=/opt/other:$PATH"),
        "HOME changed": above("HOME=/var/lib/cseries"),
        "IFS changed (dash splits the section's unquoted paths on it)": above("IFS=/"),
        "a loader variable set (LD_PRELOAD)": above("LD_PRELOAD=/opt/hook.so"),
        "a psql variable set (PSQLRC)": above("PSQLRC=/var/lib/cseries/.psqlrc"),
        "an env name split by single quotes (readonly PG'OPTIONS')": above("readonly PG'OPTIONS'=-csearch_path=x"),
        "another variable exported": above("export LC_ALL=C"),
        "ENTITY_DB reassigned": above("ENTITY_DB=scratch"),
        "ENTITY_DB reassigned behind an escaped space before #": above("echo ready\\ #; ENTITY_DB=scratch"),
        "ENTITY_DB reassigned across a line continuation": above("ENTITY_\\\nDB=scratch"),
        "ENTITY_DB reassigned after an escaped backslash at a line end": above("echo ready \\\\\nENTITY_DB=scratch"),
        "ENTITY_DB split by a continuation, right after an escaped backslash at a line end":
            above("echo ready\\\\\nENTITY_\\\nDB=scratch"),
        "ENTITY_DB's resolution changed": sub(SRC, '        ENTITY_DB="hostmem"\n', '        ENTITY_DB="scratch"\n'),
        "readonly ENTITY_DB, its name split by single quotes": above("readonly ENTITY_'DB'=scratch"),
        "ENTITY_DB read from a file, its name split by single quotes":
            above("read -r ENTITY_'DB' </var/lib/cseries/.db-hint || true"),
        "getopts into ENTITY_ID, its name split by single quotes": above("getopts x ENTITY_'ID' || true"),
        "ENTITY_DB read into through $'…' (bash assigns ENTITY_DB; dash 0.5.12 refuses the name)":
            above("read -r ENTITY_$'DB' </var/lib/cseries/.db-hint || true"),
        "a declare naming PG_SOCKET": above("declare PG_SOCKET=/run/other"),
        "the entity id forced": above("ENTITY_ID=1"),
        "PG_SOCKET pointed elsewhere": above("PG_SOCKET=/run/other"),
        "$KERNEL_SCHEMA_APP set at boot": above("KERNEL_SCHEMA_APP=/opt/other-tree"),
        "$KERNEL_SCHEMA_APP defaulted at boot (${…:=…} assigns it)": above(': "${KERNEL_SCHEMA_APP:=/opt/other-tree}"'),
        "set +e": above("set +e"),
        "an option escaped (set \\+e: dash still reads +e)": above("set \\+e"),
        "set -f (no globbing: a booting Host would apply no migration)": above("set -f"),
        "a comment line ending in a backslash, then set +e": above("# note \\\nset +e"),
        "a function standing in for psql": above("psql() { command psql \"$@\" -v ON_ERROR_STOP=0; }"),
        "an alias": above("alias psql='psql -v ON_ERROR_STOP=0'"),
        "the section inside a heredoc (never run at boot)": around(": <<'SKIP'", "SKIP"),
        "the section inside if false": around("if false; then", "fi"),
        "the section inside a function nobody calls": around("skipped() {", "}"),
        "the opening marker twice": above(ks.BEGIN),
        "the closing marker missing": sub(SRC, ks.END + "\n", ""),
        "the opening marker indented (not a line of its own)": sub(SRC, ks.BEGIN + "\n", "  " + ks.BEGIN + "\n"),
        "a line continuation running into the opening marker": above("echo ready\\"),
        "the closing marker inside the migration loop (the section cannot stand alone)":
            sub(sub(SRC, ks.END + "\n", ""), "    done\nfi\n", "    done\n" + ks.END + "\nfi\n"),
        "the markers reversed": SRC.replace(ks.BEGIN, "@@B@@").replace(ks.END, ks.BEGIN).replace("@@B@@", ks.END),
        "the restore carry moved above the section": above(carry_block.rstrip("\n"), sub(SRC, carry_block, "")),
        "the restore swap moved into the section": sub(sub(SRC, swap_block, "    :\n"), "schema_present=\"\"\n",
                                                       "schema_present=\"\"\n" + swap_block.strip() + "\n"),
        "the section reads a for-loop variable from above it through a default":
            sub(SRC, 'schema_present=""\n', 'schema_present="${_cand:-}"\n'),
        "the section reads a variable `read` above it through a default":
            sub(above("read -r SCHEMA_HINT </var/lib/cseries/.schema-hint || true"), 'schema_present=""\n',
                'schema_present="${SCHEMA_HINT:-}"\n'),
        "the section reads a variable set above it through a default": sub(
            SRC, 'if [ "$schema_present" != "t" ]; then',
            'if [ "${STANDALONE_MODE:-0}" != 1 ] && [ "$schema_present" != "t" ]; then'),
        "the section reads a variable set above it as a bare name in $(( )) (unset reads 0, even under -u)":
            sub(SRC, 'schema_present=""\n',
                'schema_present=""\nif [ $((_restore_armed)) -eq 1 ]; then echo restored; fi\n'),
        "the section reads a variable set above it, its name closed by a quote (\"$X\"y)":
            sub(SRC, 'schema_present=""\n', 'schema_present=""\necho "$_restore_armed"x\n'),
        "the section reads a variable that only a $(( )) above it assigns":
            sub(above(": $((SCHEMA_ARITH = 1))"), 'schema_present=""\n',
                'schema_present=""\n[ "${SCHEMA_ARITH:-0}" = 1 ] && echo arith\n'),
    }
    for name, text in mutants.items():
        why = refusal(text)
        check(f"refuses: {name}", bool(why), "accepted")
        if why:
            print(f"          ({'; '.join(why)[:160]})")
    why = refusal(mutants["the markers reversed"])
    check("reversed markers are refused for their order (not only for what then lands above the section)",
          any("comes before" in w for w in why), why)
    why = "; ".join(refusal(mutants["the closing marker inside the migration loop (the section cannot stand alone)"]))
    at = re.search(r"the section, on its own: line (\d+):", why)
    check("a section that cannot stand alone is reported at entrypoint.sh's own line",
          at is not None and int(at.group(1)) > SRC[:SRC.index(ks.BEGIN)].count("\n") + 1, why)
    benign = {
        "a comment edited above the section": sub(SRC, "# The kernel's schema step is every line",
                                                  "# The kernel's schema step is each line"),
        "a log line added above the section": above('echo "building the schema"'),
        "a log line and a reflowed psql inside the section": sub(
            sub(SRC, LOG, LOG + '        echo "  from $f" >&2\n'),
            'psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -v ON_ERROR_STOP=1 -c \\\n',
            'psql -h "$PG_SOCKET" -U entity \\\n     -d "$ENTITY_DB" -v ON_ERROR_STOP=1 -c \\\n'),
    }
    for name, text in benign.items():
        check(f"accepted: {name}", refusal(text) == [], refusal(text))

    print("D. upto=N is the tree before vN existed, without vN or anything later, or a refusal")
    # The measurement on THIS tree needs its history. CI fetches a PR with its
    # history (.gitea/workflows/check.yml, since 2026-10-06); in a checkout
    # without it (a shallow clone, a bare copy) the measurement is not run and
    # says so, and the mechanism is still proven below on histories this gate
    # builds — including a shallow clone's refusal.
    try:
        rev = ks.rev_before(ROOT, 165)
    except ks.SchemaError as e:
        rev = None
        print(f"  not run: the v165 measurement on this tree ({e})")
    if rev:
        with ks.tree_at(ROOT, rev) as before:
            old_init = (before / "init-db.sql").read_text()
            check("before v165: init-db.sql does not allow 'app_delivery' (today's does — why upto reads git)",
                  "'app_delivery'" not in old_init and "'app_delivery'" in (ROOT / "init-db.sql").read_text())
        with ks._app_tree(ROOT, 165) as app:
            check("before v165: what the section reads carries no v165 and nothing after it",
                  max(v for v, _ in ks.migrations(app)) < 165)
        check("the tree before v165 does not carry v165 (the first parent of the commit that brought it in)",
              subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{rev}:migrations/v165_inbox_app_delivery.sql"],
                             capture_output=True).returncode != 0)

    os.environ["GIT_CEILING_DIRECTORIES"] = str(work)   # no repo above the scratch trees
    bare = work / "bare"
    (bare / "migrations").mkdir(parents=True)
    shutil.copy(ROOT / "init-db.sql", bare / "init-db.sql")
    shutil.copy(ROOT / "migrations" / "v165_inbox_app_delivery.sql", bare / "migrations")
    try:
        ks.rev_before(bare, 165)
        check("a bare copy (no git history) refuses upto", False, "built")
    except ks.SchemaError as e:
        check("a bare copy (no git history) refuses upto", "no git history" in str(e), str(e))

    repo = work / "repo"
    (repo / "migrations").mkdir(parents=True)

    def git(*a, at=repo):
        r = subprocess.run(["git", "-C", str(at), "-c", "user.name=gate", "-c", "user.email=gate@example.invalid",
                            "-c", "commit.gpgsign=false", *a], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    git("init", "-q")
    (repo / "init-db.sql").write_text("CREATE TABLE fixture_registry (id int);\n")
    (repo / "migrations" / "v001_a.sql").write_text("SELECT 1;\n")
    git("add", "-A")
    git("commit", "-qm", "one")
    c1 = git("rev-parse", "HEAD")
    (repo / "migrations" / "v002_b.sql").write_text("SELECT 2;\n")
    check("a migration not committed yet is built on HEAD", ks.rev_before(repo, 2) == c1)
    git("add", "-A")
    git("commit", "-qm", "two")
    (repo / "init-db.sql").write_text("CREATE TABLE fixture_registry (id int);\n-- v002 folded in\n")
    git("mv", "migrations/v002_b.sql", "migrations/v002_c.sql")
    git("commit", "-qam", "fold and rename")
    check("a committed migration renamed within its number (v002_b -> v002_c, the old file deleted) is built on "
          "the commit before v002_b was added", ks.rev_before(repo, 2) == c1, ks.rev_before(repo, 2))
    # A migration written on a branch while main moved on: the Host's schema
    # before it is main's tree before the MERGE (its first parent), which has
    # main's own change; the branch commit's parent does not.
    main_branch = git("rev-parse", "--abbrev-ref", "HEAD")
    git("checkout", "-qb", "side")
    (repo / "migrations" / "v003_side.sql").write_text("SELECT 3;\n")
    git("add", "-A")
    git("commit", "-qm", "side adds v003")
    side_parent = git("rev-parse", "HEAD^")
    git("checkout", "-q", main_branch)
    (repo / "init-db.sql").write_text((repo / "init-db.sql").read_text() + "-- main moved on\n")
    git("commit", "-qam", "main changes init-db.sql")
    main_before = git("rev-parse", "HEAD")
    git("merge", "-q", "--no-ff", "side", "-m", "merge side")
    got = ks.rev_before(repo, 3)
    check("a migration that came in by a merge is built on the merge's FIRST parent (main as it stood)",
          got == main_before and got != side_parent, f"got {got[:10]}, main {main_before[:10]}, "
          f"branch parent {side_parent[:10]}")
    with ks.tree_at(repo, got) as t:
        check("that tree has main's change and no v003",
              "main moved on" in (t / "init-db.sql").read_text() and 3 not in [v for v, _ in ks.migrations(t)])
    with ks.tree_at(repo, ks.rev_before(repo, 2)) as t:
        check("that tree has neither v002 nor the folded init-db.sql",
              [v for v, _ in ks.migrations(t)] == [1] and "folded" not in (t / "init-db.sql").read_text())
    # A higher number that landed first: the tree before v004 already carries
    # v005, which a Host before v004 had — but upto=4 is the schema before v4,
    # so the section is given neither.
    (repo / "migrations" / "v005_early.sql").write_text("SELECT 5;\n")
    git("add", "-A")
    git("commit", "-qm", "v005 lands first")
    (repo / "migrations" / "v004_late.sql").write_text("SELECT 4;\n")
    git("add", "-A")
    git("commit", "-qm", "v004 lands after it")
    with ks.tree_at(repo, ks.rev_before(repo, 4)) as t:
        in_tree = [v for v, _ in ks.migrations(t)]
    with ks._app_tree(repo, 4) as app:
        given = [v for v, _ in ks.migrations(app)]
    check("upto=4: the tree before v004 carries v005, and the section is given everything below 4 and nothing else",
          5 in in_tree and given == [1, 2, 3], (in_tree, given))
    # Two branches that picked one number: v006_zz landed first, then v006_aa.
    # upto=6 reads v006_aa (migrations() orders a number's files by name), so
    # the tree before it already carries v006_zz, and only `>= upto` (not
    # `> upto`) keeps the section from being given it.
    for name, sql in (("v006_zz.sql", "CREATE TABLE zz_six (id int);\n"),
                      ("v006_aa.sql", "ALTER TABLE fixture_registry ADD COLUMN six text DEFAULT 'six';\n")):
        (repo / "migrations" / name).write_text(sql)
        git("add", "-A")
        git("commit", "-qm", f"{name} lands")
    with ks.tree_at(repo, ks.rev_before(repo, 6)) as t:
        in_tree = [f.name for _v, f in ks.migrations(t)]
    with ks._app_tree(repo, 6) as app:
        given = [f.name for _v, f in ks.migrations(app)]
    check("upto=6 with two migrations numbered 6: the tree before v006_aa carries v006_zz, and the section is "
          "given neither", "v006_zz.sql" in in_tree and "v006_aa.sql" not in in_tree
          and not any(n.startswith("v006") for n in given) and "v005_early.sql" in given, (in_tree, given))
    with ks._app_tree(repo, None) as app:
        check("a full build is given the tree's own init-db.sql and every migration, as a copy",
              [v for v, _ in ks.migrations(app)] == [1, 2, 3, 4, 5, 6, 6] and app != repo
              and (app / "init-db.sql").read_text() == (repo / "init-db.sql").read_text())
    tu = ks.template_name(repo, 1, upto=2) if (repo / "entrypoint.sh").write_text(SRC) or True else ""
    check("an upto template names the version and the commit it was built from",
          tu.startswith(ks.template_name(repo, 1) + "_u2_") and tu.endswith(c1[:10]), tu)
    shallow = work / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], capture_output=True)
    try:
        ks.rev_before(shallow, 2)
        check("a shallow clone refuses upto", False, "built")
    except ks.SchemaError as e:
        check("a shallow clone refuses upto", "shallow" in str(e), str(e))

    # Renames and copies, in a repo of their own. A TRUE rename (the old file
    # deleted in the same commit) is followed to the old file's first add; a
    # copy (the source kept) is not. The renumber is the shape of this repo's
    # own v153 -> v156: the tree before the renumber commit still holds the
    # migration as v002_x, so finding the first add by NUMBER would give the
    # section the very migration upto=5 is before. The copy is what
    # `git log --follow` gets wrong: it finds copies too, and dates v008 from
    # v007's add.
    rr = work / "renames"
    (rr / "migrations").mkdir(parents=True)
    git("init", "-q", at=rr)
    (rr / "init-db.sql").write_text("CREATE TABLE fixture_registry (id int);\n")
    (rr / "migrations" / "v001_a.sql").write_text("SELECT 1;\n")
    git("add", "-A", at=rr)
    git("commit", "-qm", "base", at=rr)
    r_base = git("rev-parse", "HEAD", at=rr)
    (rr / "migrations" / "v002_x.sql").write_text("".join(f"CREATE TABLE x{i} (id int);\n" for i in range(20)))
    git("add", "-A", at=rr)
    git("commit", "-qm", "v002_x", at=rr)
    body = "".join(f"-- line {i}\nALTER TABLE t ADD COLUMN c{i} int;\n" for i in range(40))
    (rr / "migrations" / "v007_seven.sql").write_text(body)
    git("add", "-A", at=rr)
    git("commit", "-qm", "v007", at=rr)
    r_v7 = git("rev-parse", "HEAD", at=rr)
    (rr / "migrations" / "v008_eight.sql").write_text(body.replace("c39", "c40"))
    git("add", "-A", at=rr)
    git("commit", "-qm", "v008 made from v007, which stays", at=rr)
    got = ks.rev_before(rr, 8)
    check("a copy (v008 made from v007, both kept) dates from its own add, not v007's", got == r_v7,
          f"got {got[:10]}, the commit before v008 {r_v7[:10]}")
    git("mv", "migrations/v002_x.sql", "migrations/v005_x.sql", at=rr)
    git("commit", "-qm", "renumber v002_x to v005_x", at=rr)
    got = ks.rev_before(rr, 5)
    with ks._app_tree(rr, 5) as app:
        given = [f.name for _v, f in ks.migrations(app)]
    check("a renumber (v002_x renamed to v005_x, the old file deleted): upto=5 is the tree before v002_x existed, "
          "and the section is not given that migration under its old number",
          got == r_base and given == ["v001_a.sql"], f"got {got[:10]}, before v002_x {r_base[:10]}, given {given}")
    git("mv", "migrations/v008_eight.sql", "migrations/v008_eight_renamed.sql", at=rr)
    git("commit", "-qm", "rename v008 within its number", at=rr)
    got = ks.rev_before(rr, 8)
    check("a rename within the number (v008_eight -> v008_eight_renamed) keeps v008's own first add",
          got == r_v7, f"got {got[:10]}, the commit before v008 {r_v7[:10]}")
    # Reverted and relanded under one name: the path's OLDEST add. The commit
    # that first adds it also renames another migration, which is not followed.
    r_v9 = git("rev-parse", "HEAD", at=rr)
    (rr / "migrations" / "v009_r.sql").write_text("SELECT 9;\n")
    git("mv", "migrations/v001_a.sql", "migrations/v001_a_renamed.sql", at=rr)
    git("add", "-A", at=rr)
    git("commit", "-qm", "v009_r lands; v001_a renamed", at=rr)
    git("rm", "-q", "migrations/v009_r.sql", at=rr)
    git("commit", "-qm", "revert v009_r", at=rr)
    (rr / "migrations" / "v009_r.sql").write_text("SELECT 9;\n")
    git("add", "-A", at=rr)
    git("commit", "-qm", "reland v009_r", at=rr)
    got = ks.rev_before(rr, 9)
    check("a migration reverted and relanded dates from its first add, and another file renamed in that commit "
          "is not followed", got == r_v9, f"got {got[:10]}, before the first add {r_v9[:10]}")

    print("E. a template is named for what it was built as")
    t1, t3 = ks.template_name(ROOT, 1), ks.template_name(ROOT, 3)
    check("entity 1 and entity 9003 are different templates", t1 != t3 and t1.endswith("_e1") and t3.endswith("_e3"),
          (t1, t3))
    longest = f"{t1}_u99999_{'0' * 10}"
    check("every name fits postgres' 63 characters (the longest an upto name can be)", len(longest) <= 63, longest)
    with tempfile.TemporaryDirectory(dir=work) as d:
        d = Path(d)
        shutil.copy(ROOT / "init-db.sql", d)
        shutil.copytree(ROOT / "migrations", d / "migrations")
        (d / "entrypoint.sh").write_text(SRC)
        same = ks.tree_digest(d)
        (d / "entrypoint.sh").write_text(SRC.replace(LOG, LOG + '        echo "  from $f"\n', 1))
        check("the template digest covers entrypoint.sh: a change to the section builds a new template",
              same == ks.tree_digest(ROOT) and ks.tree_digest(d) != same)
finally:
    shutil.rmtree(work, ignore_errors=True)
    os.environ.pop("GIT_CEILING_DIRECTORIES", None)

# Review of e0b359b0: a ) inside quotes within $(( )) does not end it, and
# ${X:=v} above the section assigns X.
check("the $(( )) scan reads past a quoted ) (a name after it still counts as read)",
      "_restore_armed" in ks._arith_names("[ $(( $(echo 1 | tr -d '))') + _restore_armed )) -eq 2 ]"))
check("${X:=v} and ${X=v} count as assignments",
      "X" in ks._assigned(': "${X:=1}"') and "Y" in ks._assigned(": ${Y=2}"))

print()
if fails:
    print(f"FAIL — {fails} check(s)")
    sys.exit(1)
print("PASS kernel_schema_steps")
