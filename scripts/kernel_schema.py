# gate-reads: migrations/ — every migrations/v*.sql, applied in version order (migrations() below)
# gate-reads: init-db.sql — the base schema an empty database starts from
# gate-reads: entrypoint.sh — the kernel schema section it cuts out and runs
"""Build the schema the kernel actually runs, in a throwaway database.

The kernel owns the entity schema, and entrypoint.sh builds it on every boot,
in the section between its two "kernel schema section" markers: it probes for
the base schema; on an empty database it loads init-db.sql in ONE transaction,
checks entity_registry, and puts a linked entity's own id on it BEFORE any
migration; then the schema_migrations ledger, v139 (the host_recall role)
asserted on its own, and every migrations/v*.sql in glob order, ON_ERROR_STOP,
each version once.

build() RUNS THAT SECTION. It cuts the marked lines out of the tree's
entrypoint.sh and runs them with dash, the image's /bin/sh, under `set -eu` as
the boot does, with the variables the section reads pointed at the test
database (PG_SOCKET, ENTITY_DB, ENTITY_ID; the port and password from the DSN)
and $KERNEL_SCHEMA_APP / $KERNEL_SCHEMA_TMP at a copy of the tree's init-db.sql
and migrations/ and at a scratch directory. psql gets nothing else from the
gate's environment: HOME and PGSYSCONFDIR are that scratch directory, so no
.psqlrc or .pgpass of the machine running the gate is read. This module once
carried a Python copy of the section and a pin that read
entrypoint.sh command by command to keep the two equal, and every look found
a shell shape the Python reader read differently from dash (an escaped space
before `#`, an escaped backslash at a line end, each hiding a `continue`). A
gate's schema now comes from the boot's own text, read by the boot's own shell.

WHY A GATE NEEDS THIS. Two infra gates failed on correct code:
test_contain_finding_sql applied a HAND-PICKED list of three migrations to a
stock postgres:18.4, and test_heartbeat_anchor_clears CREATEd a HAND COPY of
network_bastion_heartbeat "in the shape a pod carries".
v179 then added nac_finding_contain.approval and v146 network_bastion_heartbeat
.detect; the code writes both, the copies had neither, and the gates went red
on main. Neither failure was the database image. A copy of the schema drifts
the day the next migration lands; the schema the kernel builds cannot. So a
gate that needs kernel tables builds them HERE, on the kernel's own database
image (cseries-postgres — init-db.sql needs pgvector and AGE, which a stock
postgres does not carry).

WHAT IS STILL CHECKED (entrypoint_matches), because the section runs in a shell
that never ran the lines above it:
  - the markers: each on a line of its own, exactly once, the restore swap
    above the section and the restore carry below it;
  - the section is reached at the top level: dash parses entrypoint.sh, the
    lines above the section on their own (no if, loop, function or quote left
    open across the marker) and the section on its own, and a `)` right after
    the lines above it is a syntax error (no heredoc left open that would take
    the section in as text);
  - what the section inherits from the lines above it. Those lines are read as
    text — comments included, double and single quotes and backslashes
    dropped, once with line continuations joined and once without — so no way
    of quoting or splitting a name hides it: the only shell options set are
    -eu; the variables the section reads from above (_INPUTS: PG_SOCKET,
    ENTITY_ID, ENTITY_DB, $KERNEL_SCHEMA_APP, $KERNEL_SCHEMA_TMP) appear there,
    other than as a plain $NAME or ${NAME} read, only on the lines listed, so
    an ENTITY_DB reassigned, read into, made readonly or set by getopts before
    the section refuses, however its name is quoted; the only exports are
    ENTITY_ID and ENTITY_DB, and no name libpq, psql or the loader reads from
    the environment (PG*, except PGDATA, which only the server reads; PSQL*;
    PATH; HOME; IFS; LD_*) appears at all, so an exported PGOPTIONS refuses on
    both counts; no function or alias is defined;
  - the section reads no variable the lines above it assign, other than the
    runner's own: a `${X:-…}` would read the runner's default, and a `$((X))`
    would read 0, where the boot reads the value set above — neither stops on
    `set -u`. A bare name inside $(( )) counts as read in the section and as
    assigned above it (`$((X = 1))` assigns X).
What it does NOT cover: a name computed at run time above the section (eval, a
name put together from an expansion such as `read -r ENTITY_${e}DB`, a sourced
file); a trap set above the section, which dash runs at boot on a signal or at
exit — a CHLD trap after each command the section runs — and a gate never sets;
the environment the pod gives the container; and a schema step written outside
the markers, which no gate runs.

THE SCHEMA BEFORE A MIGRATION (`upto=N`) is the schema a Host had just before
migration N existed: the section run on init-db.sql and migrations/ as they
stood just before vN entered this tree's FIRST-PARENT history, read from git,
with vN and every later migration removed. A migration written on a branch
enters main at the merge, so the tree before it is the merge's first parent
(main as it was), not the branch commit's parent, which lacks whatever main
gained while the branch was open. A migration that was RENAMED — its old file
deleted in the commit that added the new one, as each renumber in this repo's
history did (v022→v033, v095→v105, v112→v116, v119→v122, v153→v156) — existed
from its old file's first add; a COPY (a new file made from one that stays)
exists from its own. It is not today's init-db.sql minus later
migrations: init-db.sql absorbs migrations as they land, so today's file can
already carry vN's change. Measured: today's init-db.sql already
allows inbox_messages.message_kind 'app_delivery', the very value v165 adds; a
"pre-v165" schema built from it rejected the value only because v155 happens to
rewrite the constraint without it. Without the tree's git history (an APP_ROOT
that is a bare copy, a shallow clone) an `upto` build REFUSES — it never falls
back to today's files. A migration not committed yet is built on top of HEAD,
which by definition is the tree before it. A gate then applies vN itself and
calls complete(), which runs the section again over the tree under test, as an
upgrading Host's boot would.

ONE BUILD PER SERVER, ONE CLONE PER GATE. The build takes about a minute; the
lane has a dozen DB gates. So the schema is built ONCE per (server, tree) into a
template database named for a digest of the files that make it (entrypoint.sh
among them), the entity id it was built for and, for `upto`, the version and
the commit it was built from (kernel_tmpl_<digest>_e<id>[_u<N>_<commit>]).
Every gate gets its own copy with CREATE DATABASE ... TEMPLATE, which is a file
copy and takes about a second. Each gate still works in a database nobody else
touches, dropped when it is done. A tree that changes a migration has a
different digest and builds its own template, and building one DROPS every
template of any other digest on that server: a long-lived test server keeps the
current tree's schemas and nothing else (a gate whose template vanished under it
rebuilds it). The template is built under a temporary name and renamed only
when the build succeeded, so a half-built schema is never cloned and two gates
that race to build need no lock (the loser drops its copy and clones the
winner's). A build left behind by a killed process carries its pid in its name
and is dropped once that process is gone.

Usage (a gate):
    from kernel_schema import kernel_schema_db, throwaway_server
    with throwaway_server() as server_dsn:          # or a PEER_TEST_DSN
        with kernel_schema_db(server_dsn, entity_id=9003) as dsn:
            ...   # a fresh database carrying the kernel's schema, dropped after
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])

BEGIN = "# >>> kernel schema section >>>"
END = "# <<< kernel schema section <<<"
APP_VAR, TMP_VAR = "KERNEL_SCHEMA_APP", "KERNEL_SCHEMA_TMP"
SWAP = 'python3 -m server.host_restore swap "$ENTITY_DB"'
CARRY = 'python3 -m server.host_restore carry "$ENTITY_DB"'

# The variables the section reads that entrypoint.sh sets above it, and the
# only lines above it where each may stand as a word other than a plain read
# ($NAME or ${NAME}), as _views() shows them (quotes and backslashes dropped; a
# line counted once per such occurrence). The runner sets every one itself.
_INPUTS = {
    "PG_SOCKET": ["PG_SOCKET=/var/run/postgresql"],
    "ENTITY_ID": ["ENTITY_ID=1",
                  "ENTITY_ID=$(python3 -c import json; print(json.load(open($CERT_DIR/relay.json))[entity_id]))",
                  "|*[!0-9]*) echo ERROR: Invalid ENTITY_ID: $ENTITY_ID >&2; exit 1 ;;",
                  "export ENTITY_ID"],
    "ENTITY_DB": ["ENTITY_DB=parallax", "ENTITY_DB=hostmem", "ENTITY_DB=${ENTITY_DB_PRESENT% }",
                  "export ENTITY_DB"],
    APP_VAR: [],
    TMP_VAR: [],
}
_EXPORTS = ["export ENTITY_ID", "export ENTITY_DB"]
_SHELL_OPTIONS = {"-eu"}
# Names read from the environment by libpq and psql (PGDATA only by the
# server), by the shell and the loader that find and start psql.
_ENV_NAMES = re.compile(r"\b(?:PG(?!DATA\b)[A-Z]\w*|PSQL\w*|PATH|HOME|IFS|LD_\w+)\b")
_FUNCTION_DEF = re.compile(r"(?:^|[;&|{()]|\b(?:then|do|else)\b)\s*[A-Za-z_]\w*\s*\(\s*\)", re.M)
# A path the section expands unquoted ($KERNEL_SCHEMA_APP/migrations/v*.sql):
# nothing a shell would split or glob.
_SAFE_PATH = re.compile(r"[A-Za-z0-9_./+-]+")
# DSN parameters the section's psql can take from the environment.
_LIBPQ_ENV = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
              "sslkey": "PGSSLKEY", "connect_timeout": "PGCONNECT_TIMEOUT",
              "application_name": "PGAPPNAME", "gssencmode": "PGGSSENCMODE"}


class SchemaError(RuntimeError):
    pass


def _dash() -> str | None:
    """dash — the image's /bin/sh (python:3.14-slim-bookworm) — or None."""
    for cand in (shutil.which("dash"), "/bin/sh"):
        if cand and os.path.basename(os.path.realpath(cand)) == "dash":
            return cand
    return None


def _syntax_error(dash: str, text: str) -> str | None:
    """What `dash -n` says about `text`, or None when it parses."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", prefix="kernel-schema-parse-") as f:
        f.write(text)
        f.flush()
        r = subprocess.run([dash, "-n", f.name], capture_output=True, text=True, timeout=60)
    if r.returncode == 0:
        return None
    return re.sub(r"^.*?: (\d+): ", r"line \1: ", r.stderr.strip().replace(f.name, "")) or f"exit {r.returncode}"


def _views(text: str) -> list[str]:
    """`text` as the checks read it, twice: as written, and with every line
    continuation (an odd run of backslashes before a newline) joined; both with
    double quotes, single quotes and backslashes dropped (and the `$` of a
    `$'…'`: bash reads ENTITY_$'DB' as ENTITY_DB; dash 0.5.12 does not). Nothing is
    parsed, so nothing a shell reads can be read differently here: a name split
    by quotes or a continuation is whole in one of them, and a comment is read
    like code."""
    joined = re.sub(r"(?<!\\)((?:\\\\)*)\\\n", r"\1", text)
    return [v.replace("$'", "'").replace('"', "").replace("'", "").replace("\\", "") for v in (text, joined)]


def _arith_names(view: str) -> set[str]:
    """Every name inside a $(( )) of `view`: dash reads each one as a variable
    (an unset one as 0, even under `set -u`) and `$((X = 1))` assigns X. An
    expansion left open runs to the end of `view`, so it over-reads."""
    names = set()
    for m in re.finditer(r"\$\(\(", view):
        depth, i, quote = 2, m.end(), None
        while i < len(view) and depth:
            c = view[i]
            if quote:                       # a ) inside quotes does not close it
                quote = None if c == quote else quote
            elif c in "'\"":
                quote = c
            else:
                depth += {"(": 1, ")": -1}.get(c, 0)
            i += 1
        names |= set(re.findall(r"(?<!\w)[A-Za-z_]\w*", view[m.end():i]))
    return names


def _reads(view: str) -> set[str]:
    """The names `view` expands: $NAME, ${NAME…}, and a bare name in $(( ))."""
    return set(re.findall(r"\$\{?([A-Za-z_]\w*)", view)) | _arith_names(view)


def _set_lines(view: str, name: str) -> list[str]:
    """Each line of `view` that carries `name` other than as a plain read
    ($NAME or ${NAME}), once per such occurrence."""
    out = []
    for line in view.splitlines():
        for m in re.finditer(rf"\b{re.escape(name)}\b", line):
            s, e = m.start(), m.end()
            if line[max(0, s - 1):s] == "$" or (line[max(0, s - 2):s] == "${" and line[e:e + 1] == "}"):
                continue
            out.append(line.strip())
    return out


def _assigned(view: str) -> set[str]:
    """The names `view` assigns by any shape the checks can see: NAME=, a for
    loop's variable, a name after export / readonly / read / unset / local /
    getopts, or any name inside $(( ))."""
    names = set(re.findall(r"(?:^|[\s;&|({`])([A-Za-z_]\w*)=", view, re.M))
    # ${X:=v} / ${X=v} assign X when it is unset or empty
    names |= set(re.findall(r"\$\{([A-Za-z_]\w*):?=", view))
    names |= set(re.findall(r"\bfor\s+([A-Za-z_]\w*)", view))
    for words in re.findall(r"\b(?:export|readonly|read|unset|local|getopts)\b([^\n;&|]*)", view):
        names |= set(re.findall(r"[A-Za-z_]\w*", words))
    return names | _arith_names(view)


def _split(src: str) -> tuple[str, str, str, int]:
    """(lines above the section, the section, lines below it, the section's
    first line number). The markers must already be known to stand once each."""
    lines = src.splitlines(keepends=True)
    b = next(i for i, ln in enumerate(lines) if ln.rstrip("\n") == BEGIN)
    e = next(i for i, ln in enumerate(lines) if ln.rstrip("\n") == END)
    return "".join(lines[:b]), "".join(lines[b + 1:e]), "".join(lines[e + 1:]), b + 2


def entrypoint_matches(root: Path = ROOT) -> list[str]:
    """Why the section of entrypoint.sh cannot be run as the boot runs it
    ([] when it can): see "WHAT IS STILL CHECKED" in the module docstring."""
    src = (root / "entrypoint.sh").read_text()
    problems = []
    for mark in (BEGIN, END):
        whole = sum(1 for ln in src.splitlines() if ln == mark)
        if src.count(mark) != 1 or whole != 1:
            problems.append(f"the marker {mark!r} must stand on a line of its own exactly once "
                            f"(found {src.count(mark)} times, {whole} as a whole line)")
    if problems:
        return problems
    if src.index(BEGIN) > src.index(END):
        return [f"the marker {END!r} comes before {BEGIN!r}"]
    above, section, below, first = _split(src)
    for what, anchor, where, side in (("the restore swap", SWAP, above, "above"),
                                      ("the restore carry", CARRY, below, "below")):
        if src.count(anchor) != 1 or anchor not in where:
            problems.append(f"{what} ({anchor}) must appear exactly once, {side} the section "
                            f"(found {src.count(anchor)} times, {where.count(anchor)} {side} it)")
    dash = _dash()
    if dash is None:
        return problems + ["no dash on this machine: the section runs with dash, the image's /bin/sh"]
    for label, text in (("entrypoint.sh", src), ("the lines above the section, on their own", above),
                        ("the section, on its own", "\n" * (first - 1) + section)):
        err = _syntax_error(dash, text)
        if err:
            problems.append(f"dash cannot parse {label}: {err}")
    err = _syntax_error(dash, above + "\n)\n")
    if not err or '")" unexpected' not in err:
        problems.append("a `)` right after the lines above the section is not a syntax error there: something "
                        "above the section (a heredoc) is still open and would take the section in as text")
    seen = set()
    for how, view in zip(("as written", "with continuations joined"), _views(above)):
        def note(msg):
            if msg not in seen:
                seen.add(msg)
                problems.append(msg)
        for name, want in _INPUTS.items():
            got = _set_lines(view, name)
            if got != want:
                note(f"above the section ({how}), {name} is set by {got or 'nothing'}; the section reads it, "
                     f"and only {want or 'nothing'} may set it there")
        exports = [ln.strip() for ln in view.splitlines() if re.search(r"\bexport\b", ln)]
        if exports != _EXPORTS:
            note(f"above the section ({how}), the exports are {exports}; only {_EXPORTS} may be exported "
                 "there — psql in the section reads its environment, and a gate gives it a clean one")
        options = set(re.findall(r"\bset\s+([-+][A-Za-z]*)", view))
        if options - _SHELL_OPTIONS:
            note(f"above the section ({how}), shell options {sorted(options - _SHELL_OPTIONS)} are set; "
                 f"the section runs under {sorted(_SHELL_OPTIONS)} alone")
        env = sorted(set(_ENV_NAMES.findall(view)))
        if env:
            note(f"above the section ({how}), {env} — a name libpq, psql or the shell reads from the "
                 "environment — is named; a booting Host's psql would read it, a gate's would not")
        defs = [m.group(0).strip() for m in _FUNCTION_DEF.finditer(view)] + re.findall(r"\balias\b", view)
        if defs:
            note(f"above the section ({how}), a function or alias is defined ({defs}): it would stand in "
                 "for a command the section runs at boot, and not in a gate")
    set_above = set().union(*(_assigned(v) for v in _views(above)))
    # The section as written too: with quotes dropped, `$X'y'` reads as $Xy.
    reads = set().union(*(_reads(v) for v in (section, *_views(section))))
    leaks = sorted((reads & set_above) - set(_INPUTS))
    if leaks:
        problems.append(f"the section reads {leaks}, set above it: a gate runs the section without those lines, "
                        "so pass it in as an input (_INPUTS) or set it inside the section")
    return problems


def schema_section(root: Path = ROOT) -> tuple[str, int]:
    """entrypoint.sh's schema section, as text, and the line it starts on.
    Raises SchemaError when it cannot be run as the boot runs it."""
    stale = entrypoint_matches(root)
    if stale:
        raise SchemaError("entrypoint.sh's schema section cannot be run the way a booting Host runs it ("
                          + "; ".join(stale) + ")")
    _above, section, _below, first = _split((root / "entrypoint.sh").read_text())
    return section, first


def _conn_env(dsn: str) -> dict[str, str]:
    """The variables that point the section's psql at the database `dsn`
    names: PG_SOCKET (psql -h: a socket directory or a host), ENTITY_DB
    (psql -d), and the port, password and TLS settings libpq reads from the
    environment. The section connects as role `entity`, as the boot does."""
    p = urlsplit(dsn)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    user = q.pop("user", None) or (unquote(p.username) if p.username else None)
    if user not in (None, "entity"):
        raise SchemaError(f"the schema section connects as role entity, as a booting Host does; "
                          f"the test server's DSN names role {user!r}")
    hostport = p.netloc.rpartition("@")[2]
    host = q.pop("host", None) or (hostport[1:hostport.find("]")] if hostport.startswith("[")
                                    else unquote(hostport.split(":", 1)[0]))
    if not host:
        raise SchemaError("the test server's DSN names no host or socket directory; the section would "
                          "connect to psql's default socket, which in a Host's pod is that Host's database")
    db = q.pop("dbname", None) or unquote(p.path.lstrip("/"))
    if not db:
        raise SchemaError("the DSN names no database")
    env = {"PG_SOCKET": host, "ENTITY_DB": db}
    port = q.pop("port", None) or (str(p.port) if p.port else None)
    if port:
        env["PGPORT"] = port
    password = q.pop("password", None) or (unquote(p.password) if p.password else None)
    if password:
        env["PGPASSWORD"] = password
    for k in [k for k in q if k in _LIBPQ_ENV]:
        env[_LIBPQ_ENV[k]] = q.pop(k)
    if q:
        raise SchemaError(f"the DSN carries {sorted(q)}, which the schema section's psql cannot be given")
    return env


def run_section(dsn: str, app: Path, entity_id: int, root: Path = ROOT,
                path: str | None = None) -> subprocess.CompletedProcess:
    """Run entrypoint.sh's schema section, with dash under `set -eu`, against
    the database `dsn` names, reading init-db.sql and migrations/ under `app`.
    Raises SchemaError when it fails, with what it printed. `path` replaces
    PATH (a self-check's stand-in psql)."""
    dash = _dash()
    if dash is None:
        raise SchemaError("no dash on this machine: the section runs with dash, the image's /bin/sh")
    section, first = schema_section(root)
    if not _SAFE_PATH.fullmatch(str(app)):
        raise SchemaError(f"{app}: the section expands this path unquoted, so it may hold only letters, digits "
                          "and _ . / + -")
    with tempfile.TemporaryDirectory(prefix="kernel-schema-run-") as tmp:
        if not _SAFE_PATH.fullmatch(tmp):
            raise SchemaError(f"{tmp}: the section expands its scratch path unquoted (set TMPDIR to a plain path)")
        script = Path(tmp) / "entrypoint-schema-section.sh"
        script.write_text("\n" * (first - 1) + section)      # dash's messages carry entrypoint.sh's line numbers
        env = {"PATH": path or os.environ.get("PATH") or "/usr/bin:/bin",
               "HOME": tmp, "PGSYSCONFDIR": tmp,              # no .psqlrc / .pgpass / psqlrc of this machine
               "ENTITY_ID": str(int(entity_id)), APP_VAR: str(app), TMP_VAR: tmp, **_conn_env(dsn)}
        r = subprocess.run([dash, "-eu", str(script)], env=env, cwd=tmp, capture_output=True, text=True,
                           timeout=1800)
    if r.returncode:
        raise SchemaError(f"entrypoint.sh's schema section failed (exit {r.returncode}):\n"
                          + (r.stdout[-1500:] + "\n" + r.stderr[-2500:]).strip())
    return r


def _psql(dsn: str, *args: str, sql: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["psql", dsn, "-X", "-q", *args]
    return subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=600)


def _with_db(dsn: str, db: str) -> str:
    p = urlsplit(dsn)
    return urlunsplit((p.scheme, p.netloc, "/" + db, p.query, p.fragment))


def server_reachable(server_dsn: str | None) -> bool:
    """Whether a test server answers at `server_dsn` (a gate SKIPs when not)."""
    if not server_dsn:
        return False
    r = subprocess.run(["psql", server_dsn, "-X", "-Atc", "SELECT 1"], capture_output=True, text=True,
                       timeout=30, env={**os.environ, "PGCONNECT_TIMEOUT": "5"})
    return r.stdout.strip() == "1"


# A TEST SERVER IS REACHED OVER ITS UNIX SOCKET, NEVER TCP. A gate's
# throwaway database (throwaway_server below, and the restore gate's) has its
# /var/run/postgresql bind-mounted from a folder on this machine, and the DSN
# names that folder: postgresql://entity@/postgres?host=<folder>&port=5432.
# throwaway_server once published a TCP port on 127.0.0.1, and on
# the CI runner a running server refused connections mid-gate. The runner's
# firewall (/etc/nftables.d-cirunner.nft, input chain) lets a runner's new
# loopback connection in only when it can match the socket it reaches to
# cirunner.slice, and rejects the rest. asyncpg's close sends Terminate and
# waits for the server to hang up, so each asyncpg connection leaves a
# TIME_WAIT socket on the server side (measured on the runner's Docker: 50
# asyncpg connections left 50 there, 50 psql connections none), and that is
# not a full socket: a new connection whose source port reused its 4-tuple
# matched nothing and was refused. A unix socket has no ports and no
# TIME_WAIT, and a server with no TCP port listens on no interface.
#
# The folder must be one this process and the docker daemon both see. On the
# CI runner the job and the daemon each have a PRIVATE /tmp (ci-runner.service
# and ci-runner-docker.service, PrivateTmp=yes), so a folder under /tmp is not
# the same folder for both. So: CSERIES_GATE_SHARED_DIR when set, else next to
# this checkout (the runner's work tree, which both see), and TMPDIR only when
# neither will do. The socket's whole path must fit a unix socket address (107
# bytes), and holds only characters a DSN carries as they are.
SOCK_PATH_MAX = 107
SOCKET_FILE = ".s.PGSQL.5432"
_DSN_PATH = re.compile(r"/[A-Za-z0-9_./-]+")


def shared_dir_candidates() -> list[Path]:
    """Where a gate may make the folder a database socket is bind-mounted
    from, in order (see above)."""
    out = []
    if os.environ.get("CSERIES_GATE_SHARED_DIR"):
        out.append(Path(os.environ["CSERIES_GATE_SHARED_DIR"]))
    out.append(ROOT.parent)
    out.append(Path(tempfile.gettempdir()))
    return [Path(os.path.abspath(p)) for p in out]


def pick_work_dir(candidates: list[Path], dirname: str, limit: int = SOCK_PATH_MAX,
                  mode: int = 0o700) -> Path:
    """A new folder `dirname` in the first of `candidates` where it can be
    made and where a socket in its `s` subfolder (<folder>/s/.s.PGSQL.5432)
    fits `limit` bytes and a DSN can name it. Raises SchemaError when none
    will do. The `s` subfolder is the caller's to make: the database runs as
    the image's own user, or under rootless docker a subordinate uid, so it
    must be mode 0777, and this folder (0700 by default) keeps other accounts
    out of it."""
    for base in candidates:
        d = base / dirname
        if len(f"{d}/s/{SOCKET_FILE}") > limit or not _DSN_PATH.fullmatch(str(d)):
            print(f"  (skipping {base}: a socket there would be over {limit} bytes, or a path a DSN "
                  "cannot carry as it is)", file=sys.stderr)
            continue
        try:
            d.mkdir(mode=mode)
        except OSError:
            continue
        return d
    raise SchemaError(f"no folder for the database socket fits {limit} bytes "
                      f"(tried {', '.join(str(c) for c in candidates)})")


def migrations(root: Path = ROOT) -> list[tuple[int, Path]]:
    """(version, file) for every migrations/v*.sql, in version order."""
    out = []
    for f in (root / "migrations").glob("v*.sql"):
        m = re.match(r"v0*(\d+)", f.name)
        if m:
            out.append((int(m.group(1)), f))
    return sorted(out, key=lambda vf: (vf[0], vf[1].name))


def _ledger_size(dsn: str) -> int:
    r = _psql(dsn, "-Atc", "SELECT count(*) FROM schema_migrations")
    return int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0


def complete(dsn: str, root: Path = ROOT, already: tuple = ()) -> int:
    """Bring a database built with `upto` to the full current schema: run the
    section again over the tree under test, as an upgrading Host's boot does.
    A gate that tested a migration's TRANSITION by applying it itself names
    that version in `already`, so it is recorded rather than run a third time.
    Returns how many versions the ledger gained."""
    for v in already:
        f = next((f for ver, f in migrations(root) if ver == v), None)
        _psql(dsn, "-c", f"INSERT INTO schema_migrations(version, name) VALUES ({int(v)}, "
                         f"'{f.stem if f else v}') ON CONFLICT (version) DO NOTHING")
    if _psql(dsn, "-Atc", "SELECT to_regclass('public.entity_registry') IS NOT NULL").stdout.strip() != "t":
        raise SchemaError("complete() brings a built schema forward; this database carries none")
    # The section reads ENTITY_ID only to number an EMPTY database; this one is not.
    eid = _psql(dsn, "-Atc", "SELECT coalesce(min(id), 0) FROM entity_registry WHERE id > 0").stdout.strip()
    before = _ledger_size(dsn)
    with _app_tree(root, None) as app:
        run_section(dsn, app, int(eid) if eid.isdigit() else 0, root)
    return _ledger_size(dsn) - before


def _git(root: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=not binary, timeout=120)


def rev_before(root: Path, version: int) -> str:
    """The commit a Host's schema came from just before migration `version`
    existed: the FIRST parent of the commit that brought its file into this
    tree's first-parent history — for a migration written on a branch, the
    merge's first parent, main as it stood — or HEAD when that migration is not
    committed yet. A file that came from a TRUE rename (the old file deleted in
    the same commit: a renumber, or a new name within the number) is followed
    to the old file's first add; a copy (the source kept) is not.
    Raises SchemaError when the tree's git history cannot say."""
    f = next((f for v, f in migrations(root) if v == version), None)
    if f is None:
        raise SchemaError(f"upto={version}: this tree has no migrations/v{version}_*.sql")
    rel = f.relative_to(root).as_posix()
    why = (f"upto={version} builds the schema from the tree as it stood BEFORE {rel} was added, "
           f"read from git; {root} cannot provide it")
    if _git(root, "rev-parse", "--git-dir").returncode:
        raise SchemaError(why + " (no git history — a bare copy of the tree?)")
    # --first-parent walks main's own line and diffs a merge against its first
    # parent, so a migration that arrived by a merge is "added" AT the merge.
    # The file is followed by hand, never with --follow, which also finds COPIES
    # (it looks for a source among files the commit left alone): a v008 made
    # from v007 resolved to v007's commit. At the commit that first added the
    # path, `diff-tree -M` pairs the add only with a file that commit DELETED
    # (and at least half the same, -M's default; v022 -> v033 is 62%); such a
    # rename is followed to the old file's first add. A renumber is one
    # (v153 -> v156): the tree before that commit still holds the migration as
    # v153, so stopping there would apply it under its old number.
    path, seen, base = rel, {rel}, "HEAD"
    while True:
        added = _git(root, "log", "--first-parent", "--no-renames", "--diff-filter=A", "--format=%H", "--", path)
        if added.returncode:
            raise SchemaError(why + f": {added.stderr.strip()[-300:]}")
        shas = added.stdout.split()
        if not shas:
            if path != rel:
                raise SchemaError(why + f" (git log finds no commit adding {path}, which {rel} was renamed from)")
            # Not committed yet: HEAD is the tree before it — if HEAD really lacks it.
            if any(re.match(rf"migrations/v0*{version}(?!\d)", n)
                   for n in _git(root, "ls-tree", "--name-only", "HEAD", "migrations/").stdout.split()):
                raise SchemaError(why + f" (HEAD carries v{version} but git log finds no commit adding {rel})")
            break
        first = shas[-1]                         # the OLDEST add: when this path first entered the tree
        base = first + "^1"
        renames = _git(root, "diff-tree", "-r", "-z", "-M", "--name-status", base, first, "--",
                       rel.rsplit("/", 1)[0] + "/")
        if renames.returncode:                   # no first parent (a root commit, a shallow edge): refused below
            break
        old, fields = None, iter(renames.stdout.split("\0"))
        for status in fields:
            src = next(fields, "")
            dst = next(fields, "") if status[:1] in ("R", "C") else src
            if status[:1] == "R" and dst == path:
                old = src
        if old is None or old in seen:
            break
        seen.add(old)
        path = old
    r = _git(root, "rev-parse", "--verify", "--quiet", base + "^{commit}")
    if r.returncode or not r.stdout.strip():
        raise SchemaError(why + f" ({base} is not in this clone's history — a shallow clone?)")
    return r.stdout.strip()


@contextlib.contextmanager
def tree_at(root: Path, rev: str):
    """init-db.sql and migrations/ exactly as they stood at commit `rev`, in a
    temporary directory that is removed afterwards."""
    r = _git(root, "archive", "--format=tar", rev, "init-db.sql", "migrations", binary=True)
    if r.returncode:
        raise SchemaError(f"could not read the tree at {rev[:12]}: {r.stderr.decode(errors='replace')[-300:]}")
    with tempfile.TemporaryDirectory(prefix="kernel-schema-before-") as d:
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            tar.extractall(d, filter="data")
        yield Path(d)


@contextlib.contextmanager
def _app_tree(root: Path, upto: int | None):
    """What the section reads as /app: a copy of the tree's init-db.sql and
    migrations/, or, with `upto`, the tree before vN existed (tree_at) with vN
    and every later migration removed."""
    if upto is None:
        with tempfile.TemporaryDirectory(prefix="kernel-schema-app-") as d:
            shutil.copy2(root / "init-db.sql", d)
            shutil.copytree(root / "migrations", Path(d) / "migrations")
            yield Path(d)
        return
    with tree_at(root, rev_before(root, upto)) as before:
        for version, f in migrations(before):
            if version >= upto:
                f.unlink()
        yield before


def build(dsn: str, root: Path = ROOT, entity_id: int = 1, upto: int | None = None) -> None:
    """Build the kernel's schema into the (empty) database at `dsn` by running
    entrypoint.sh's own schema section — or, with `upto`, the schema a Host had
    just BEFORE migration `upto` existed (the tree before the commit that added
    it; see the module docstring), so a gate can apply that migration itself
    and test the transition on the real prior state, then complete() the rest."""
    with _app_tree(root, upto) as app:
        run_section(dsn, app, entity_id, root)


def tree_digest(root: Path = ROOT) -> str:
    """What the schema is made of: init-db.sql, entrypoint.sh (whose section
    builds it), every migration (name and bytes) and this module. Equal digests
    build equal schemas."""
    h = hashlib.sha256()
    for f in [root / "init-db.sql", root / "entrypoint.sh", *(f for _v, f in migrations(root)), Path(__file__)]:
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def template_name(root: Path = ROOT, entity_id: int = 1, upto: int | None = None) -> str:
    """kernel_tmpl_<tree digest>_e<entity id>[_u<N>_<commit before N>]."""
    name = f"kernel_tmpl_{tree_digest(root)}_e{int(entity_id)}"
    if upto is not None:
        name += f"_u{int(upto)}_{rev_before(root, upto)[:10]}"
    return name


def _db_exists(server_dsn: str, name: str) -> bool:
    r = _psql(server_dsn, "-Atc", f"SELECT 1 FROM pg_database WHERE datname = '{name}'")
    return r.stdout.strip() == "1"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def drop_stale(server_dsn: str, root: Path = ROOT) -> list[str]:
    """Drop every kernel template on this server that is not this tree's (any
    other digest), and every half-finished build whose builder is gone. Returns
    the names dropped. A template another gate is cloning at that moment is
    waited for (DROP DATABASE takes the lock the clone holds); a gate whose
    template disappears before it clones rebuilds it (kernel_schema_db)."""
    keep = f"kernel_tmpl_{tree_digest(root)}_"
    names = _psql(server_dsn, "-Atc",
                  r"SELECT datname FROM pg_database WHERE datname LIKE 'kernel\_tmpl\_%' "
                  r"OR datname LIKE 'kernel\_build\_%'").stdout.split()
    dropped = []
    for name in names:
        if name.startswith("kernel_tmpl_"):
            if name.startswith(keep):
                continue
        else:
            m = re.match(r"kernel_build_(\d+)_", name)
            if not m or _pid_alive(int(m.group(1))):
                continue
        _psql(server_dsn, "-c", f'ALTER DATABASE "{name}" WITH IS_TEMPLATE false')
        r = _psql(server_dsn, "-c", "SET lock_timeout = '60s'", "-c", f'DROP DATABASE IF EXISTS "{name}"')
        if r.returncode == 0:
            dropped.append(name)
    return dropped


def ensure_template(server_dsn: str, root: Path = ROOT, upto: int | None = None, entity_id: int = 1) -> str:
    """The template database carrying this tree's kernel schema for `entity_id`
    on this server (or, with `upto`, the schema before that migration), built
    now if it is not there yet — dropping the templates of other trees first.
    Returns its name."""
    tmpl = template_name(root, entity_id, upto)
    if _db_exists(server_dsn, tmpl):
        return tmpl
    drop_stale(server_dsn, root)
    building = f"kernel_build_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    r = _psql(server_dsn, "-v", "ON_ERROR_STOP=1", "-c", f'CREATE DATABASE "{building}"')
    if r.returncode:
        raise SchemaError(f"could not create a database on the test server: {r.stderr[-500:]}")
    try:
        build(_with_db(server_dsn, building), root, entity_id=entity_id, upto=upto)
        r = _psql(server_dsn, "-v", "ON_ERROR_STOP=1", "-c",
                  f'ALTER DATABASE "{building}" RENAME TO "{tmpl}"')
        if r.returncode:
            if _db_exists(server_dsn, tmpl):      # another gate finished first
                return tmpl
            raise SchemaError(f"could not publish the template: {r.stderr[-500:]}")
        # Nobody connects to a template: a connection would block every clone.
        _psql(server_dsn, "-c", f'ALTER DATABASE "{tmpl}" WITH ALLOW_CONNECTIONS false IS_TEMPLATE true')
        building = None
        return tmpl
    finally:
        if building:
            _psql(server_dsn, "-c", f'DROP DATABASE IF EXISTS "{building}" WITH (FORCE)')


@contextlib.contextmanager
def kernel_schema_db(server_dsn: str, root: Path = ROOT, entity_id: int = 1, upto: int | None = None):
    """A FRESH database on the server at `server_dsn`, carrying the kernel's
    schema as a Host with `entity_id` carries it (or, with `upto`, as it stood
    before that migration); yields its DSN and drops it afterwards. A fresh
    database per use, so gates that share one test server never see each
    other's rows. It is a clone of a template (built on first use, see
    ensure_template) — the entity id is set when the template is BUILT, right
    after init-db.sql as entrypoint.sh does, never patched onto the clone."""
    name = "kernel_" + uuid.uuid4().hex[:12]
    r = None
    for attempt in range(20):
        tmpl = ensure_template(server_dsn, root, upto, entity_id)
        r = _psql(server_dsn, "-v", "ON_ERROR_STOP=1", "-c", f'CREATE DATABASE "{name}" TEMPLATE "{tmpl}"')
        if r.returncode == 0:
            break
        if "is being accessed by other users" in r.stderr:   # a concurrent clone; wait it out
            time.sleep(0.5)
            continue
        if "does not exist" in r.stderr:                      # another tree dropped it; rebuild
            continue
        raise SchemaError(f"could not clone the kernel schema: {r.stderr[-500:]}")
    else:
        raise SchemaError(f"could not clone the kernel schema (template busy): {r.stderr[-300:]}")
    dsn = _with_db(server_dsn, name)
    try:
        yield dsn
    finally:
        _psql(server_dsn, "-c", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# A gate stopped before its `finally` (SIGKILL — hang_watch's first signal on a
# hang — SIGABRT, or SIGTERM, whose default action skips `finally`) left its
# throwaway container running, and nothing removed it. So each container is
# labelled with the process that made it (maker_labels), and every
# throwaway_server() first removes the labelled containers whose maker is gone.
# Gates run in parallel: a container whose maker is alive is never touched. Nor
# is one from another PID namespace (its pid means nothing here), nor one
# without the label. The maker's start time is in the label so a pid reused by
# a newer process does not keep a dead gate's container alive.
#
# The kernel's boot id is in the label too, and a container is judged only by
# a gate on the same boot. The initial PID namespace has the same inode number
# on every Linux machine, so without it a gate on another machine sharing this
# docker daemon (DOCKER_HOST over tcp/ssh) would look its pid up in the wrong
# /proc and remove a live gate's database. A container left from before a
# reboot is therefore not reaped here; nothing reaped it before this either.
#
# THIS IS THE ONE RULE for "the process that made it is gone" (maker_gone), and
# every reaper in this tree uses it: this module's, the connector gates'
# (reap_leftovers), the restore gate's, and smoke's through its shell copy,
# scripts/gate_maker.sh (scripts/test_gate_reapers_hidden_proc.py holds the two
# to one table of cases). The maker is gone only when kill(pid, 0) finds
# no such process, /proc shows it a zombie, or /proc shows its pid started at
# another time than the label says. A pid that /proc does not show is NOT gone,
# and neither is one kill(pid, 0) answers with EPERM. Measured: the CI
# runner runs three jobs at once under ProtectProc=invisible, so one job's /proc
# hides another job's processes while kill(pid, 0) still finds them. Smoke's
# reaper read "no /proc entry" as "gone" and removed a concurrent job's live pod
# (one CI run removed another's); the restore gate's read it the same way, and
# its database was shut down mid-run while another gates job ran beside it.
THROWAWAY_LABEL = "cseries.gate.throwaway"
THROWAWAY_KIND = "kernel-schema"
# A container's name and its maker labels, as `docker ps --format` gives them
# to leftover_rows (tab-separated).
LEFTOVER_FORMAT = ('{{.Names}}\t{{.Label "cseries.gate.pid"}}\t{{.Label "cseries.gate.pidns"}}'
                   '\t{{.Label "cseries.gate.pidstart"}}\t{{.Label "cseries.gate.boot"}}')
# Read at import, so it must not raise: every gate that only builds the schema
# imports this module. 0 means unknown, and the reap then judges nothing: a
# partial /proc can refuse this path and still give a boot id, and gates in
# different PID namespaces would all label 0.
try:
    PIDNS = os.stat("/proc/self/ns/pid").st_ino
except OSError:
    PIDNS = 0
try:
    BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
except OSError:
    BOOT_ID = ""          # no boot id: label nothing as judgeable, reap nothing
# Where a maker's /proc/<pid>/stat is read: /proc, or a test's stand-in.
PROC = Path("/proc")


def _proc_status(pid: int) -> tuple[str | None, str]:
    """(start, how) for process `pid`. start is its start time (clock ticks
    since boot, /proc/<pid>/stat field 22); None when it is gone or a zombie
    (it can run no cleanup); "" when it exists but /proc does not show it
    (hidepid, ProtectProc=invisible, another user's process): alive, start
    unknown. how says which, in words, for the reap's log line."""
    try:
        stat = (PROC / str(pid) / "stat").read_text()
    except OSError:
        # hidepid hides another user's /proc/<pid> entirely (ENOENT), and so
        # does the CI runner's ProtectProc=invisible for another job's, so a
        # missing file alone does not prove the process is gone. kill(pid, 0)
        # is subject to neither: EPERM means it exists.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None, "kill(pid, 0) finds no such process"
        except OSError as e:
            return "", f"/proc does not show it, and kill(pid, 0) says {e.strerror or e}"
        return "", "/proc does not show it, and kill(pid, 0) finds it"
    fields = stat[stat.rindex(")") + 2:].split()     # state is fields[0], starttime fields[19]
    if fields[0] in ("Z", "X"):
        return None, f"/proc shows it a zombie (state {fields[0]})"
    return fields[19], f"/proc shows it started at {fields[19]}"


def _proc_start(pid: int) -> str | None:
    """The start time of process `pid`; None when it is gone or a zombie, ""
    when it exists but /proc does not show it (_proc_status)."""
    return _proc_status(pid)[0]


def maker_labels(kind: str) -> dict[str, str]:
    """The labels a gate puts on each container (or volume) it makes, which
    tell a later run when this process is gone: the kind (what each reap
    filters on), its pid, PID namespace and start time, and the boot id."""
    labels = {THROWAWAY_LABEL: kind, "cseries.gate.pid": str(os.getpid()), "cseries.gate.pidns": str(PIDNS),
              "cseries.gate.pidstart": _proc_start(os.getpid()) or ""}
    if BOOT_ID:
        labels["cseries.gate.boot"] = BOOT_ID
    return labels


def maker_gone(pid: str, pidns: str, start: str, boot: str, status_of=_proc_status) -> str | None:
    """Why the process these labels name is gone (the rule above), or None
    when it is alive or cannot be judged here: labelled in another PID
    namespace, on another boot, or not labelled. `status_of` stands in for
    _proc_status in tests."""
    pidns, start, boot = (str(x or "").strip() for x in (pidns, start, boot))
    if not str(pid).isdigit() or not PIDNS or pidns != str(PIDNS) or not BOOT_ID or boot != BOOT_ID:
        return None
    now, how = status_of(int(pid))
    if now is None:
        return how
    # Without a readable start time (either side) only "the pid is gone"
    # proves it.
    if start.isdigit() and now.isdigit() and now != start:
        return f"{how}, not at {start}: a newer process has its pid"
    return None


def leftover_rows(rows: list[str], status_of=_proc_status) -> list[tuple[str, int, str]]:
    """(name, maker's pid, why) for each `docker ps` row in LEFTOVER_FORMAT
    whose maker is gone (maker_gone)."""
    out = []
    for row in rows:
        cname, pid, ns, start, boot = (row.split("\t") + ["", "", "", "", ""])[:5]
        why = maker_gone(pid, ns, start, boot, status_of) if cname else None
        if why:
            out.append((cname, int(pid), why))
    return out


def throwaway_leftovers(rows: list[str], start_of=None) -> list[str]:
    """The names among `docker ps` rows "<name>\\t<pid>\\t<pidns>\\t<start>\\t<boot>"
    (LEFTOVER_FORMAT) whose maker is gone. `start_of` stands in for
    _proc_start in tests."""
    status_of = _proc_status if start_of is None else (lambda pid: (start_of(pid), "the stand-in's answer"))
    return [cname for cname, _pid, _why in leftover_rows(rows, status_of)]


def reap_leftovers(kind: str, action: tuple[str, ...] = ("rm", "-f")) -> list[tuple[str, int, str]]:
    """`docker <action>` each container labelled `kind` whose maker is gone,
    with a line on stderr naming it, its maker's pid and why; returns (name,
    pid, why) for each. Best effort: no docker, no reap, and never an error."""
    try:
        ps = subprocess.run(["docker", "ps", "-a", "--filter", f"label={THROWAWAY_LABEL}={kind}",
                             "--format", LEFTOVER_FORMAT], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    gone = leftover_rows(ps.stdout.splitlines()) if ps.returncode == 0 else []
    for cname, pid, why in gone:
        # Before the call, so a reap that hangs is still on record.
        print(f"reaping {cname} (docker {' '.join(action)}): the process that made it, pid {pid}, "
              f"is gone: {why}", file=sys.stderr, flush=True)
        try:
            subprocess.run(["docker", *action, cname], capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
    return gone


# The file in a throwaway server's folder (<base>/.ks-<name>) that says the
# folder was made for that server: it holds the server's name, and
# scripts/peer_test_db.sh (MARKER there) empties, removes, opens up or mounts a
# socket folder only in a folder that carries it.
SERVER_MARKER = ".cseries-gate-server"
# Each throwaway container's name and its socket folder, as `docker ps
# --format` gives them (tab-separated).
SOCKET_FORMAT = '{{.Names}}\t{{.Label "cseries.gate.socket"}}'


def labelled_work_dir(name: str, sock: str) -> Path | None:
    """The folder throwaway_server made for the server `name`, from the socket
    folder its container's cseries.gate.socket label names (<folder>/s), or
    None, and nothing is to be removed, when the label names anything else: a
    path that is not absolute, a socket folder not named s, a folder not named
    .ks-<name> (the name throwaway_server gives it), or one that is a symlink or
    no folder at all. Where it is does not matter: a gate may make it next to
    another checkout, under CSERIES_GATE_SHARED_DIR or in TMPDIR."""
    p = Path(sock or "")
    if not p.is_absolute() or p.name != "s" or p.parent.name != f".ks-{name}":
        return None
    work = p.parent
    if work.is_symlink() or not work.is_dir():
        return None
    return work


def reap_throwaway_leftovers() -> list[str]:
    """Remove the throwaway containers left by gates that are gone, and the
    folder each one's socket was made in, read from its cseries.gate.socket label
    before the container goes (labelled_work_dir): so a folder made under
    another parent than this gate's (another checkout's, another
    CSERIES_GATE_SHARED_DIR, another TMPDIR) goes too. Returns their names. Best
    effort: no docker, no reap, and never an error."""
    socks: dict[str, str] = {}
    try:
        ps = subprocess.run(["docker", "ps", "-a", "--filter", f"label={THROWAWAY_LABEL}={THROWAWAY_KIND}",
                             "--format", SOCKET_FORMAT], capture_output=True, text=True, timeout=60)
        if ps.returncode == 0:
            socks = {row.split("\t")[0]: (row.split("\t") + [""])[1] for row in ps.stdout.splitlines() if row}
    except (OSError, subprocess.SubprocessError):
        pass
    gone = [cname for cname, _pid, _why in reap_leftovers(THROWAWAY_KIND)]
    for cname in gone:
        work = labelled_work_dir(cname, socks.get(cname, ""))
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return gone


# A THROWAWAY SERVER THAT STOPS SERVING EXPLAINS ITSELF. In several CI runs
# a gate's throwaway database stopped answering partway
# through the schema load, and the gate said only "Connection refused" or "the
# schema section failed (exit 2)". By the time anyone looked, nothing could say
# why: the CI Docker logs neither who stopped a container nor its exit code,
# its event buffer holds about a minute, and a reaper's removal line prints
# only in the reaping gate's own output, which CI drops when that gate passes.
# So when a gate finds its server down, it asks docker while docker still
# knows, and the answer goes into the SchemaError. Each docker question has its
# own time limit and a question docker does not answer becomes a line saying
# so: the explanation is added to an error, never put in its place.
#
# This explanation found the cause in one CI run: a running server refused TCP
# connections that reused a server-side TIME_WAIT 4-tuple, under the runner's
# firewall. Servers are reached over a unix socket since (see "A TEST SERVER IS
# REACHED OVER ITS UNIX SOCKET"), so what is said of how the gate reaches one
# is its socket folder and the socket in it; there is no port to ask about.
#
# It says nothing of the container's memory.events. On the CI runner's rootless
# docker a container's process runs in the daemon's own cgroup
# (/cirunner.slice/ci-runner-docker.service, as measured), so there is
# no cgroup of the container's own to read, and the line could only ever say
# "unreadable" where these servers fail. Docker's own OOMKilled is in the state
# line, and no throwaway server is given a memory limit.
LOG_TAIL = 30
DOCKER_TIMEOUT = 10.0
_EVENT_ACTIONS = {"create", "start", "restart", "kill", "stop", "die", "oom", "pause", "destroy"}
_EVENT_FORMAT = ('{{.TimeNano}}\t{{.Action}}\t{{index .Actor.Attributes "signal"}}'
                 '\t{{index .Actor.Attributes "exitCode"}}')


def _docker(*args: str, merge: bool = False) -> subprocess.CompletedProcess | None:
    """`docker <args>`, or None when docker cannot be run or does not answer in
    DOCKER_TIMEOUT. `merge` puts stderr into stdout, in the order written."""
    try:
        return subprocess.run(["docker", *args], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                              text=True, errors="replace", timeout=DOCKER_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def _first_line(text: object) -> str:
    """The first non-blank line of `text` (a string or an error), at most 300
    characters. Never raises: it reads the gate's own error, and an error whose
    __str__ fails must still leave as itself, not as that failure."""
    try:
        s = str(text)
    except Exception as e:
        s = f"(its text could not be read: {type(e).__name__})"
    return next((ln.strip() for ln in s.splitlines() if ln.strip()), "")[:300]


def server_down(name: str, dsn: str | None = None) -> str | None:
    """Why the throwaway server `name` is not serving, or None when it is, or
    when nothing can tell: docker has no such container, or says it is not
    running, or (with `dsn`) psql gets no answer from it. Never raises."""
    try:
        r = _docker("inspect", "--format", "{{.State.Status}}", name)
        if r is not None and r.returncode != 0 and "no such" in (r.stderr or "").lower():
            return "is gone: docker has no such container"
        if r is not None and r.returncode == 0 and r.stdout.strip() != "running":
            return f"is not running: docker says {r.stdout.strip() or 'nothing'}"
        if dsn is None:
            return None
        p = subprocess.run(["psql", dsn, "-X", "-Atc", "SELECT 1"], capture_output=True, text=True,
                           timeout=3 * DOCKER_TIMEOUT, env={**os.environ, "PGCONNECT_TIMEOUT": "5"})
        if p.stdout.strip() != "1":
            return f"does not answer: {_first_line(p.stderr) or f'psql exit {p.returncode}'}"
    except Exception:       # an explanation that fails explains nothing, and must not replace the error
        return None
    return None


def _socket_line(sock: str | Path) -> str:
    """Whether the socket folder `sock` and the server's socket in it
    (SOCKET_FILE) exist, with each one's kind, owner (uid:gid) and mode. The
    server makes the socket when it starts serving and removes it when it shuts
    down cleanly; one left by a server that was killed refuses connections.
    Read with lstat, so a symlink is named as one and never followed: two
    lstat calls, no subprocess. Never raises."""
    import stat as st

    def one(p: Path) -> str:
        try:
            s = os.lstat(p)
        except FileNotFoundError:
            return "missing"
        except Exception as e:
            return f"unreadable ({type(e).__name__}: {_first_line(e)})"
        kind = next((k for is_kind, k in ((st.S_ISSOCK, "a socket"), (st.S_ISDIR, "a folder"),
                                         (st.S_ISLNK, "a symlink"), (st.S_ISREG, "a file"))
                     if is_kind(s.st_mode)), "another kind of file")
        return f"exists, {kind}, owner {s.st_uid}:{s.st_gid}, mode {st.S_IMODE(s.st_mode):04o}"
    try:
        folder = Path(sock)
        return f"  socket folder {folder}: {one(folder)}; its socket {SOCKET_FILE}: {one(folder / SOCKET_FILE)}"
    except Exception as e:
        return f"  socket folder {sock}: could not say ({type(e).__name__}: {_first_line(e)})"


def explain_server(name: str, sock: str | Path, since: float | None = None) -> str:
    """What docker says about the throwaway server `name`, as lines for an
    error: the last LOG_TAIL lines of its log, the lifecycle events docker
    still holds for it since `since` (time.time() at its start), its socket
    folder `sock` and the socket in it (_socket_line), its labels and, last,
    so it ends a gate's output, its state: whether it exists, Status,
    ExitCode, OOMKilled, StartedAt and FinishedAt. Three docker calls, each
    bounded by DOCKER_TIMEOUT. Never raises."""
    import json
    out = []

    def said(r: subprocess.CompletedProcess | None, what: str) -> str | None:
        if r is None:
            return f"  {what}: docker did not answer in {DOCKER_TIMEOUT:g}s"
        if r.returncode:
            return f"  {what}: {_first_line(r.stderr or r.stdout) or f'exit {r.returncode}'}"
        return None

    try:
        r = _docker("logs", "--tail", str(LOG_TAIL), name, merge=True)
        bad = said(r, f"docker logs {name}")
        lines = r.stdout.splitlines()[-LOG_TAIL:] if not bad else []
        out += [bad] if bad else [f"  docker logs --tail {LOG_TAIL} {name}:"] + [f"    | {ln}" for ln in lines]

        since_arg = f"{since - 5:.3f}" if since else str(int(time.time()) - 600)
        r = _docker("events", "--since", since_arg, "--until", f"{time.time():.3f}", "--filter", "type=container",
                    "--filter", f"container={name}", "--format", _EVENT_FORMAT)
        bad = said(r, "docker events")
        evs = []
        for row in ([] if bad else r.stdout.splitlines()):
            ns, action, sig, code = (row.split("\t") + ["", "", "", ""])[:4]
            action = action.split(":")[0].strip()
            if action in _EVENT_ACTIONS and ns.isdigit():
                at = time.strftime("%H:%M:%S", time.gmtime(int(ns) // 10**9)) + f".{int(ns) % 10**9 // 10**6:03d}"
                evs.append(at + " " + action + (f" signal {sig}" if sig else "") + (f" exit {code}" if code else ""))
        out.append(bad or "  events docker still holds for it (UTC): " + ("; ".join(evs[-12:]) or "none"))
        out.append(_socket_line(sock))

        r = _docker("inspect", name)
        asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if r is not None and r.returncode != 0 and "no such" in (r.stderr or "").lower():
            out += ["  labels: unknown, the container is gone",
                    f"  state: the container does not exist (asked at {asked}): removed, or never made"]
        elif said(r, "docker inspect"):
            out.append(said(r, "docker inspect"))
        else:
            c = json.loads(r.stdout)[0]
            st, labels = c.get("State") or {}, (c.get("Config") or {}).get("Labels") or {}
            out.append("  labels: " + (" ".join(f"{k}={v}" for k, v in sorted(labels.items())) or "none"))
            out.append(f"  state: exists, Status {st.get('Status')}, ExitCode {st.get('ExitCode')}, "
                       f"OOMKilled {str(st.get('OOMKilled')).lower()}, StartedAt {st.get('StartedAt')}, "
                       f"FinishedAt {st.get('FinishedAt')}"
                       + (f", Error {st.get('Error')!r}" if st.get("Error") else "") + f" (asked at {asked})")
    except Exception as e:      # never more than a line about itself
        out.append(f"  (explaining stopped: {type(e).__name__}: {_first_line(e)})")
    return "\n".join(out)


def _refused(e: BaseException | None, sock: str | Path) -> bool:
    """Whether the error `e`, or one it was raised from, says a connection to
    this server's socket (<sock>/.s.PGSQL.5432) was refused or found no socket
    there. psql names the path: 'connection to server on socket "<path>"
    failed: Connection refused' (a socket no server listens on) or '... failed:
    No such file or directory' (no socket). Only an error naming this path
    counts, so one from another server is never put on this one. asyncpg's
    names no path (0.31.0, measured: ConnectionRefusedError and
    FileNotFoundError, no filename), so it counts only through server_down,
    when the server is down. Never raises."""
    path = str(Path(sock) / SOCKET_FILE)
    for _ in range(10):
        if e is None:
            return False
        try:
            text = str(e)
        except Exception:
            text = ""
        if path in text and ("Connection refused" in text or "No such file or directory" in text):
            return True
        e = e.__cause__ or e.__context__
    return False


def _down_note(name: str, sock: str | Path, since: float, dsn: str | None, when: str,
               err: BaseException | None = None) -> str | None:
    """The explanation for an error, when the server `name` is down (server_down)
    or the error `err` is a connection its socket `sock` did not take
    (_refused), else None. Such an error counts even when the server answers
    again by the time it is asked: in one CI run three gates were refused by
    containers that ran on until the gates' own cleanup. Never
    raises."""
    try:
        why = server_down(name, dsn)
        if why is None and _refused(err, sock):
            why = "did not take a connection on its socket, and answers again now"
        if why is None:
            return None
        return (f"the throwaway server {name} {why}, {when}. What docker says about it:\n"
                + explain_server(name, sock, since))
    except Exception:
        return None


@contextlib.contextmanager
def throwaway_server(name: str | None = None):
    """A throwaway cseries-postgres (the kernel's database image) reachable over
    its unix socket, in a folder of its own, and over no TCP port (see "A TEST
    SERVER IS REACHED OVER ITS UNIX SOCKET"), via scripts/peer_test_db.sh;
    yields its server DSN and removes it and its folder afterwards. The name is
    unique per call unless given, so two gates (or two sessions) never tear
    down each other's container. A container a killed gate left behind is
    removed by the next call (see above), and its folder with it.

    A server that stops serving explains itself (see above): an error leaving
    the `with` while the server is gone, stopped or not answering, or an error
    that is itself a connection its socket did not take, becomes a SchemaError
    carrying what docker says about it, raised FROM the original, which Python
    prints first; any other error while the server still answers leaves
    unchanged. A `with` that ends normally costs one `docker inspect`, and
    raises the same SchemaError when the container is gone or stopped by
    then."""
    bases = shared_dir_candidates()
    reap_throwaway_leftovers()
    name = name or f"kernel-schema-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    for base in bases:      # left by an earlier server given the same name
        shutil.rmtree(base / f".ks-{name}", ignore_errors=True)
    work = pick_work_dir(bases, f".ks-{name}")
    # Made for this server: peer_test_db.sh acts on no socket folder whose
    # parent does not say so.
    (work / SERVER_MARKER).write_text(name + "\n")
    sock = work / "s"
    labels = " ".join(f"{k}={v}" for k, v in maker_labels(THROWAWAY_KIND).items())
    env = {**os.environ, "PEER_TEST_PG_NAME": name, "PEER_TEST_PG_SOCKET_DIR": str(sock),
           "PEER_TEST_PG_LABELS": labels}
    helper = str(ROOT / "scripts" / "peer_test_db.sh")
    since = time.time()
    # One `finally` around the start as well as the use: a start that times
    # out or is interrupted removes its container too.
    try:
        r = subprocess.run(["bash", helper], env=env, capture_output=True, text=True, timeout=300)
        m = re.search(r"export PEER_TEST_DSN='([^']+)'", r.stdout)
        if not m:
            raise SchemaError(f"the throwaway cseries-postgres did not start:\n{r.stderr[-1500:]}\n"
                              f"What docker says about {name}:\n" + explain_server(name, sock, since))
        dsn = m.group(1)
        try:
            yield dsn
        except Exception as e:
            note = _down_note(name, sock, since, dsn,
                              f"found after this error: {type(e).__name__}: {_first_line(e)}", e)
            if note is None:
                raise
            raise SchemaError(note) from e
        note = _down_note(name, sock, since, None, "found when the gate was done with it")
        if note is not None:
            raise SchemaError(note)
    finally:
        _stop_throwaway(helper, env, name)
        shutil.rmtree(work, ignore_errors=True)


def _stop_throwaway(helper: str, env: dict, name: str) -> None:
    """`peer_test_db.sh --stop` for the server `name`, bounded by DOCKER_TIMEOUT
    like every docker question here: a docker that hangs on the removal must
    not hang the gate. A server left behind gets one line on stderr; it carries
    this process's maker labels, so reap_throwaway_leftovers removes it once
    this process is gone. Never raises: it runs in a `finally`, under the
    gate's own result or error."""
    try:
        subprocess.run(["bash", helper, "--stop"], env=env, capture_output=True, text=True,
                       timeout=DOCKER_TIMEOUT)
    except Exception as e:
        why = (f"did not finish in {DOCKER_TIMEOUT:g}s" if isinstance(e, subprocess.TimeoutExpired)
               else f"could not run ({type(e).__name__}: {_first_line(e)})")
        try:
            print(f"left the throwaway server {name} behind: peer_test_db.sh --stop {why}; "
                  "the next throwaway_server reaps it once this process is gone", file=sys.stderr, flush=True)
        except Exception:
            pass
