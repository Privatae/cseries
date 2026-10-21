#!/usr/bin/env python3
"""Gate: the restore's carry (server/host_restore.py carry_rows) on the REAL schema.

The unit gate (scripts/test_host_restore.py) drives carry_rows over a fake
database. This one builds the real schema, init-db.sql and every migration,
in a throwaway Postgres (the project's own cseries-postgres image, reached over
its unix socket), and runs the carry the way a boot does:

  1. same Host number, with an approved ingress rule (nac_policy ->
     nac_profiles, nac_endpoints -> nac_profiles) and a memory-domain grant
     (entity_domain_access -> domain_registry, a table that is NOT carried)
     that the live Host added after the backup: the carry must succeed, the
     live rule and grant are what the restored database holds, the domain
     they name is brought across, and every foreign key in the schema holds;
  2. another Host number (the backup is Host 3's, this Host is 5), carried
     TWICE as if the boot were killed after the commit: Host 5's row is kept
     and holds the restored data, this Host's side of its Circles messages is
     renumbered, and every foreign key holds;
  3. a restored row whose parent was access that is gone now follows the
     key's own ON DELETE rule, and a key that says neither cascade nor set
     null fails the carry instead of leaving a dangling reference;
  7. the owner's sign-in: after the carry the restored Host starts (the real
     owner_login.init) with the live owner and makes no setup code; the
     browser that asked for the restore is still signed in; the owner signs in
     with the live password, or the live passkey and a recovery code; the
     backup's owner, password and sessions do not come back; and a backup
     whose owner had another email is said so in the restore's result.

Needs only docker (DOCKER_HOST is honoured, as in the CI runner's rootless
Docker): psql runs inside the gate's own container, and the database image is
built from this tree's Dockerfile.postgres under a tag named by its content
hash when that tag is not there yet, so a stale image is never used. Exits 3
only when docker itself cannot be reached; pre_merge_gates counts that as a
failure (a gate that cannot run is not a pass).

    python3 scripts/test_restore_carry_real_schema.py
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


import hashlib  # noqa: E402

_PG_FILES = ("Dockerfile.postgres", "entrypoint-postgres.sh", "LICENSE", "NOTICE",
             "THIRD-PARTY-NOTICES.md")
_PG_HASH = hashlib.sha256(b"".join((ROOT / f).read_bytes() for f in _PG_FILES)).hexdigest()[:12]
# The tag's repository part: this gate's own images, and only these, are
# replaced when a new one is built. CSERIES_GATE_IMAGE_PREFIX gives a run its
# own set (a local check of the build path must never touch the shared one).
IMAGE_REPO = os.environ.get("CSERIES_GATE_IMAGE_PREFIX") or "cseries-postgres-carry"
IMAGE = os.environ.get("PG_IMG") or f"{IMAGE_REPO}:{_PG_HASH}"


def stale_carry_tags(tags: list[str], current: str, repo: str = IMAGE_REPO) -> list[str]:
    """This gate's own images other than the one it uses now: each Dockerfile
    change leaves one behind otherwise."""
    return sorted(t for t in tags if t.startswith(repo + ":") and t != current)


# Checked before anything else runs, on every path: the build-and-replace
# branch below only runs on a machine without the image, which is exactly
# where a mistake in it would otherwise hide.
check("only this gate's own older images are replaced",
      stale_carry_tags([f"{IMAGE_REPO}:old1", f"{IMAGE_REPO}:{_PG_HASH}",
                        "cseries-postgres:smoke", "postgres:17",
                        f"{IMAGE_REPO}-other:x"], f"{IMAGE_REPO}:{_PG_HASH}") == [f"{IMAGE_REPO}:old1"])
if not shutil.which("docker") or subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
    print("cannot run: docker is not reachable")
    sys.exit(3)
if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
    # Built from this tree, in a context of just the files it copies.
    ctx = Path(tempfile.mkdtemp(prefix="carry-pgctx-"))
    for f in _PG_FILES:
        shutil.copy(ROOT / f, ctx / f)
    print(f"building {IMAGE} from Dockerfile.postgres")
    b = subprocess.run(["docker", "build", "-q", "-f", str(ctx / "Dockerfile.postgres"), "-t", IMAGE, str(ctx)],
                       capture_output=True, text=True)
    shutil.rmtree(ctx, ignore_errors=True)
    if b.returncode != 0:
        print(f"FAIL: the database image did not build: {b.stderr.strip()[-400:]}")
        sys.exit(1)
    # A new image replaces this gate's older ones (CSERIES_GATE_KEEP_OLD_IMAGES=1
    # keeps them).
    listed = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                            capture_output=True, text=True).stdout.split()
    stale = stale_carry_tags(listed, IMAGE)
    if os.environ.get("CSERIES_GATE_KEEP_OLD_IMAGES") == "1":
        print(f"built; keeping this gate's older images: {stale or 'none'}")
    else:
        for old in stale:
            subprocess.run(["docker", "image", "rm", old], capture_output=True)
        print(f"built; replaced this gate's older images: {stale or 'none'}")

import asyncpg  # noqa: E402

from server import host_restore as hr  # noqa: E402

# WHERE THE SOCKET LIVES. The database's socket is bind-mounted from a folder
# both this process and the docker daemon see, and this process connects to
# it: kernel_schema's rule (shared_dir_candidates, pick_work_dir), which every
# throwaway database follows ("A TEST SERVER IS REACHED OVER ITS UNIX SOCKET").


# A run stopped before its own cleanup (a runner's timeout sends SIGTERM,
# which ends Python without its `finally`) left its database running and its
# folder behind: measured 2026-10-01 on the build machine, carry-real-1182899 up two hours
# after its gate was gone. So SIGTERM now ends this run through its cleanup,
# and each run first stops what an earlier run of THIS gate provably left: a
# container labelled with this gate's kind whose maker is gone, by
# kernel_schema's rule (maker_gone; the labels are maker_labels'). A container
# without the labels is not provably this gate's, and is left be.
#
# "Gone" is that rule, never "no /proc/<pid>": the CI runner's jobs run side
# by side under ProtectProc=invisible, where one job's /proc hides another
# job's processes and kill(pid, 0) still finds them. This gate read the missing
# entry as "gone", so it stopped any concurrent job's database as a leftover;
# two CI runs failed with theirs shut down mid-run ("the
# database system is shutting down") while another gates job ran beside them.
sys.path.insert(0, str(ROOT / "scripts"))
import kernel_schema as ks  # noqa: E402

KIND = "carry-real"
_ns, _boot = str(ks.PIDNS), ks.BOOT_ID
# A stand-in for /proc and kill(pid, 0): 2 runs, started when its label says;
# 6 runs but this /proc does not show it; 7 is a newer process than its label's;
# every other pid is gone.
_now = {2: ("200", "shown"), 6: ("", "hidden, kill finds it"), 7: ("999", "shown")}
check("a run reaps only this gate's databases whose run is gone, labelled in its own PID namespace "
      "and on this boot",
      [(c, p) for c, p, _why in ks.leftover_rows(
          ["carry-real-1\t\t\t\t", f"carry-real-2\t2\t{_ns}\t200\t{_boot}",
           f"carry-real-5\t5\t{_ns}\t500\t{_boot}", f"carry-real-3\t3\t999\t300\t{_boot}",
           f"carry-real-6\t6\t{_ns}\t600\t{_boot}", f"carry-real-7\t7\t{_ns}\t700\t{_boot}",
           f"carry-real-8\t8\t{_ns}\t800\tanother-boot", "carry-real-x\tx\t\t\t"],
          status_of=lambda pid: _now.get(pid, (None, "gone")))]
      == ([("carry-real-5", 5), ("carry-real-7", 7)] if ks.PIDNS and ks.BOOT_ID else []))
for _cname, _pid, _why in ks.reap_leftovers(KIND, action=("stop",)):     # --rm removes it
    for _base in ks.shared_dir_candidates():
        shutil.rmtree(_base / f".cr-{_pid}", ignore_errors=True)

import signal  # noqa: E402

signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # through `finally`, not around it

try:
    work = ks.pick_work_dir(ks.shared_dir_candidates(), f".cr-{os.getpid()}", mode=0o755)
except ks.SchemaError as _e:
    print(f"FAIL: {_e}")
    sys.exit(1)
print(f"socket folder: {work}")
sock = work / "s"
sock.mkdir()
sock.chmod(0o777)
_labels = ks.maker_labels(KIND)
# The start time in the name, not only the pid: the reaper before this one
# stops any running carry-real-<pid> whose /proc entry it cannot see, and jobs
# for branches that predate this still run it beside this one. It parses no
# other name.
name = f"carry-real-{os.getpid()}-{_labels['cseries.gate.pidstart'] or 0}"
started = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "none",
                          *(a for k, v in _labels.items() for a in ("--label", f"{k}={v}")),
                          "-v", f"{sock}:/var/run/postgresql", IMAGE], capture_output=True, text=True)
if started.returncode != 0:
    print(f"FAIL: the database did not start ({started.stderr.strip()[-200:]})")
    sys.exit(1)


def psql(db, *args, sql_file=None):
    """psql INSIDE the gate's container (the image has it; the host need not).
    A file is fed on stdin."""
    cmd = ["docker", "exec", "-i", name, "psql", "-h", "/var/run/postgresql", "-U", "entity", "-d", db,
           "-v", "ON_ERROR_STOP=1", "-Atq", *args]
    if sql_file is not None:
        cmd += ["-f", "-"]
        return subprocess.run(cmd, input=Path(sql_file).read_text(), capture_output=True, text=True)
    return subprocess.run(cmd, capture_output=True, text=True)


try:
    for _ in range(90):
        if psql("postgres", "-c", "SELECT 1").returncode == 0:
            break
        time.sleep(1)
    else:
        print("FAIL: the database never became ready")
        sys.exit(1)
    psql("postgres", "-c", "CREATE DATABASE tmpl")
    psql("tmpl", "--single-transaction", sql_file=ROOT / "init-db.sql")
    for f in sorted((ROOT / "migrations").glob("v*.sql"), key=lambda p: int(p.name[1:].split("_")[0])):
        r = psql("tmpl", sql_file=f)
        if r.returncode != 0:
            print(f"FAIL: {f.name} did not apply: {r.stderr[-300:]}")
            sys.exit(1)
    tables = int(psql("tmpl", "-c", "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").stdout)
    check(f"the real schema is built ({tables} tables)", tables > 100, tables)
    check("the socket folder is next to the checkout (both CI units see it), not in a private /tmp",
          os.environ.get("CSERIES_GATE_SHARED_DIR") or work.parent == Path(os.path.abspath(ks.ROOT.parent)),
          work)
    check("the socket's path fits a unix socket address",
          len(f"{sock}/{ks.SOCKET_FILE}") <= ks.SOCK_PATH_MAX)
    # A folder that EXISTS and is writable, but whose socket path would be
    # too long: the guard, not a failed mkdir, must pass it over. Both folders
    # are made in this run's own work folder and the limit is set between
    # their lengths, so the caller's TMPDIR cannot decide it (under a long
    # TMPDIR the probe's "short" folder was itself too long, and the gate
    # stopped with no folder to pick).
    _short, _long = work / "p", work / ("p" * 12)
    _short.mkdir()
    _long.mkdir()
    _picked = ks.pick_work_dir([_long, _short], ".cr-1", limit=len(f"{_short}/.cr-1/s/{ks.SOCKET_FILE}"))
    check("a folder whose socket path would be too long is passed over for the next one",
          _picked.parent == _short, _picked)

    for db in ("live1", "restored1", "live2", "restored2", "live3", "restored3"):
        psql("postgres", "-c", f"CREATE DATABASE {db} TEMPLATE tmpl")
    # carry_rows reads a pre-v201 backup's integrations SQLite file from the
    # apps volume (hr.LIVE) and a pre-v201 live Host's from what the swap set
    # aside (hr.PREVIOUS): this run's own folders, never the machine's.
    hr.LIVE, hr.PREVIOUS = work / "apps-live", work / "apps-previous"

    def old_integrations_store(base, conns, grants):
        """The SQLite file the integrations store was until v201, on an apps tree."""
        import sqlite3
        f = base / hr.INTEGRATIONS_LEGACY_REL
        f.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(f)
        c.executescript(
            "CREATE TABLE connections (id TEXT PRIMARY KEY, provider_key TEXT, user_id TEXT, status TEXT, "
            "credentials_ref TEXT, connection_config TEXT, created_at TEXT, updated_at TEXT, "
            "last_sync_at TEXT, last_error TEXT);"
            "CREATE TABLE integration_grants (connection_id TEXT, app_id TEXT, methods TEXT, granted_at TEXT);")
        c.executemany("INSERT INTO connections (id, provider_key, user_id, status, connection_config, "
                      "created_at, updated_at) VALUES (?, 'gmail', ?, 'active', '{}', 't', 't')", conns)
        c.executemany("INSERT INTO integration_grants VALUES (?, ?, ?, 't')", grants)
        c.commit()
        c.close()
        return f

    async def integ_conn(c, cid, user):
        await c.execute("INSERT INTO integration_connections (id, provider_key, user_id, status, "
                        "created_at, updated_at) VALUES ($1, 'gmail', $2, 'active', 't', 't')", cid, user)

    async def integ_grants(c):
        return sorted(tuple(x) for x in await c.fetch(
            "SELECT connection_id, app_id, methods FROM integration_grants"))

    async def conn(db):
        return await asyncpg.connect(host=str(sock), user="entity", database=db)

    async def all_keys_hold(c):
        bad = []
        for fk in [dict(r) for r in await c.fetch(hr._FKS_SQL)]:
            n = await c.fetchval(f"SELECT count(*) FROM {hr._qi(fk['child'])} c WHERE {hr._orphans_where(fk)}")
            if n:
                bad.append((fk["conname"], n))
        return bad

    async def run():
        print("== 1. same Host number; an approved ingress rule; a memory-domain grant added since")
        r1, l1 = await conn("restored1"), await conn("live1")
        for c, prof, rule, dom in ((r1, 901, "old rule", "d_old"), (l1, 902, "new rule", "d_new")):
            await c.execute("INSERT INTO entity_registry (id, display_name) VALUES (5, 'h') ON CONFLICT (id) DO NOTHING")
            await c.execute("INSERT INTO nac_profiles (id, name, enforcement) VALUES ($1, $2, 'allow')", prof, f"p{prof}")
            await c.execute("INSERT INTO nac_policy (name, profile_id, proposed_by, approved_at) "
                            "VALUES ($1, $2, 'user', now())", rule, prof)
            await c.execute("INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
                            "VALUES (70, 5, 'laptop', '\\x01', 'h70', 'bastion')")
            await c.execute("INSERT INTO nac_endpoints (token_id, assigned_profile_id) VALUES (70, $1)", prof)
            await c.execute("INSERT INTO domain_registry (id, name, domain_group) VALUES ($1, $1, 'g')", dom)
            await c.execute("INSERT INTO entity_domain_access (entity_id, domain_id, access_level) "
                            "VALUES (5, $1, 'read')", dom)
        await l1.execute("UPDATE mcp_client_tokens SET revoked_at = now() WHERE id = 70")
        # The owner's passkeys and recovery codes (v240): the backup holds a
        # passkey the owner has since removed and a code still unspent; the
        # live database holds the passkey added since and that code spent.
        await r1.execute("INSERT INTO owner_passkeys (credential_id, public_key) VALUES ('\\x0a', '\\x01')")
        await l1.execute("INSERT INTO owner_passkeys (credential_id, public_key) VALUES ('\\x0b', '\\x02')")
        await r1.execute("INSERT INTO owner_recovery_codes (code_hash) VALUES ('h1')")
        await l1.execute("INSERT INTO owner_recovery_codes (code_hash, used_at) VALUES ('h1', now())")
        # Each database's own backup-auth key (server/host_backup.py auth_key).
        k_live, k_restored = os.urandom(32), os.urandom(32)
        await l1.execute("INSERT INTO kernel_secrets (id, secret) VALUES ('backup_auth', $1)", k_live)
        await r1.execute("INSERT INTO kernel_secrets (id, secret) VALUES ('backup_auth', $1)", k_restored)
        try:
            await hr.carry_rows(l1, r1, stamp="20260927T020000Z")
            ok = True
        except Exception as e:
            ok = e
        check("the carry succeeds with an approved ingress rule in both databases", ok is True, ok)
        check("the live ingress rule is the one that holds, and its profile came with it",
              [tuple(x) for x in await r1.fetch("SELECT p.name, f.name FROM nac_policy p "
                                                "JOIN nac_profiles f ON f.id = p.profile_id ORDER BY p.id")]
              == [("new rule", "p902")] or
              ("new rule", "p902") in [tuple(x) for x in await r1.fetch(
                  "SELECT p.name, f.name FROM nac_policy p JOIN nac_profiles f ON f.id = p.profile_id")])
        check("the old rule is gone", not await r1.fetchval("SELECT 1 FROM nac_policy WHERE name = 'old rule'"))
        check("the owner's live passkeys hold: the one removed since does not come back",
              [bytes(x["credential_id"]) for x in await r1.fetch("SELECT credential_id FROM owner_passkeys")]
              == [b"\x0b"])
        check("a recovery code spent since stays spent",
              await r1.fetchval("SELECT used_at IS NOT NULL FROM owner_recovery_codes WHERE code_hash = 'h1'") is True)
        check("the revoked bastion stays revoked",
              await r1.fetchval("SELECT revoked_at FROM mcp_client_tokens WHERE id = 70") is not None)
        grants = {x[0] for x in await r1.fetch("SELECT domain_id FROM entity_domain_access WHERE entity_id = 5")}
        check("the live memory-domain grants hold (the one added since, not the backup's), and the "
              "domain the new one names was brought across",
              "d_new" in grants and "d_old" not in grants
              and await r1.fetchval("SELECT 1 FROM domain_registry WHERE id = 'd_new'"), grants)
        check("the restored data table keeps its own rows (the old domain is still there)",
              await r1.fetchval("SELECT 1 FROM domain_registry WHERE id = 'd_old'"))
        bad = await all_keys_hold(r1)
        check("every foreign key in the schema holds afterwards", not bad, bad)
        check("the carry is recorded in the restored database",
              await hr.carried_already(r1, "20260927T020000Z"))
        auth_rows = {x["id"]: bytes(x["secret"]) for x in await r1.fetch(
            "SELECT id, secret FROM kernel_secrets WHERE id LIKE 'backup_auth%'")}
        check("the restored backup-auth key stays, and the live one is kept beside it",
              auth_rows == {"backup_auth": k_restored, f"backup_auth:{hr.hb.auth_kid(k_live)}": k_live},
              sorted(auth_rows))
        check("  and both verify on the real schema", set(await hr.hb.auth_keys(r1))
              == {hr.hb.auth_kid(k_live), hr.hb.auth_kid(k_restored)})
        await r1.close()
        await l1.close()

        print("== 2. the backup is Host 3's, this Host is 5; carried twice")
        r2, l2 = await conn("restored2"), await conn("live2")
        await r2.execute("INSERT INTO entity_registry (id, display_name) VALUES (3, 'lost host')")
        await r2.execute("INSERT INTO projects (entity_id, name) VALUES (3, 'site')")
        await r2.execute("INSERT INTO inbox_messages (from_address, to_address, subject, from_entity_id, to_entity_id) "
                         "VALUES ('a', 'b', 'in', 8, 3), ('b', 'a', 'out', 3, 8)")
        await l2.execute("INSERT INTO entity_registry (id, display_name) VALUES (5, 'fresh host')")
        # Its integrations: a connection in its database (v201) and one in a
        # pre-v201 SQLite file the swap brought back; both are Host 3's.
        await integ_conn(r2, "ic-db", "3")
        legacy2 = old_integrations_store(hr.LIVE, [("ic-file", "3")], [("ic-file", "mail", None)])
        for i in (1, 2):
            try:
                await hr.carry_rows(l2, r2, 3, 5, stamp="20260927T020000Z")
                ok = True
            except Exception as e:
                ok = e
            check(f"carry {i} succeeds", ok is True, ok)
        regs = [tuple(x) for x in await r2.fetch("SELECT id, display_name FROM entity_registry WHERE id IN (3, 5)")]
        check("Host 5's row is there after both, holding the restored Host's data", regs == [(5, "lost host")], regs)
        check("its project is Host 5's", await r2.fetchval("SELECT entity_id FROM projects WHERE name = 'site'") == 5)
        check("this Host's side of its messages is 5 now; the other side is still 8",
              sorted(tuple(x) for x in await r2.fetch("SELECT from_entity_id, to_entity_id FROM inbox_messages"))
              == [(5, 8), (8, 5)])
        users = sorted(tuple(x) for x in await r2.fetch("SELECT id, user_id FROM integration_connections"))
        check("its integration connections are Host 5's, the database's and the SQLite file's both "
              "(every read filters on user_id = this Host's number)",
              users == [("ic-db", "5"), ("ic-file", "5")], users)
        check("... and the file's grant did not come with them", await integ_grants(r2) == [])
        legacy2.unlink()
        bad = await all_keys_hold(r2)
        check("every foreign key in the schema holds afterwards", not bad, bad)
        await r2.close()
        await l2.close()

        print("== 3. restored rows whose parent was access that is gone")
        r3, l3 = await conn("restored3"), await conn("live3")
        await r3.execute("CREATE TABLE rig_child_cascade (id int, token_id int REFERENCES mcp_client_tokens(id) ON DELETE CASCADE)")
        await r3.execute("CREATE TABLE rig_child_null (id int, token_id int REFERENCES mcp_client_tokens(id) ON DELETE SET NULL)")
        await r3.execute("INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle) "
                         "VALUES (80, 5, 'gone', '\\x02', 'h80')")
        await r3.execute("INSERT INTO rig_child_cascade VALUES (1, 80)")
        await r3.execute("INSERT INTO rig_child_null VALUES (1, 80)")
        await hr.carry_rows(l3, r3)
        check("a child whose key cascades goes with the access that is gone",
              await r3.fetchval("SELECT count(*) FROM rig_child_cascade") == 0)
        check("a child whose key sets null is kept, without the reference",
              await r3.fetchval("SELECT token_id FROM rig_child_null") is None
              and await r3.fetchval("SELECT count(*) FROM rig_child_null") == 1)
        await r3.execute("CREATE TABLE rig_child_restrict (id int, token_id int REFERENCES mcp_client_tokens(id))")
        await r3.execute("INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle) "
                         "VALUES (81, 5, 'gone2', '\\x03', 'h81')")
        await r3.execute("INSERT INTO rig_child_restrict VALUES (1, 81)")
        try:
            await hr.carry_rows(l3, r3)
            check("a key that says neither fails the carry, and nothing of it is committed", False)
        except hr.RestoreError:
            check("a key that says neither fails the carry, and nothing of it is committed",
                  await r3.fetchval("SELECT count(*) FROM mcp_client_tokens WHERE id = 81") == 1)
        await r3.close()
        await l3.close()

    async def run_more():
        print("== 4. the last check catches what settling does not; secrets are never copied across")
        for db in ("live4", "restored4", "live5", "restored5"):
            psql("postgres", "-c", f"CREATE DATABASE {db} TEMPLATE tmpl")
        r4, l4 = await conn("restored4"), await conn("live4")
        # The live database itself holds a rule whose profile is missing
        # (written with keys off): between two carried tables, so nothing
        # settles it, and only the final check stands between it and a commit.
        await l4.execute("SET session_replication_role = replica")
        await l4.execute("INSERT INTO nac_policy (name, profile_id, proposed_by) VALUES ('dangling', 999, 'user')")
        await l4.execute("SET session_replication_role = origin")
        try:
            await hr.carry_rows(l4, r4)
            check("a row left without its parent fails the carry, and nothing is committed", False)
        except hr.RestoreError as e:
            check("a row left without its parent fails the carry, and nothing is committed",
                  "nac_policy" in str(e) and not await r4.fetchval("SELECT 1 FROM nac_policy WHERE name = 'dangling'"),
                  str(e))
        await r4.close()
        await l4.close()
        r5, l5 = await conn("restored5"), await conn("live5")
        for c in (r5, l5):
            await c.execute("CREATE TABLE rig_secret_ref (id int, secret_id text REFERENCES kernel_secrets(id))")
        await l5.execute("INSERT INTO kernel_secrets (id, secret) VALUES ('rig_only_live', '\\x01')")
        await l5.execute("INSERT INTO rig_secret_ref VALUES (1, 'rig_only_live')")
        real = hr.CARRIED_TABLES
        hr.CARRIED_TABLES = real + ("rig_secret_ref",)
        try:
            await hr.carry_rows(l5, r5)
            check("a carried row naming a secret the restored database lacks fails the carry", False)
        except hr.RestoreError as e:
            check("a carried row naming a secret the restored database lacks fails the carry, and the "
                  "secret is not copied", "secrets are never copied" in str(e)
                  and not await r5.fetchval("SELECT 1 FROM kernel_secrets WHERE id = 'rig_only_live'"), str(e))
        finally:
            hr.CARRIED_TABLES = real
        check("the secrets tables are on the list", {"vault_envelope", "kernel_secrets"} <= set(hr.SETTLE_DENYLIST))
        await r5.close()
        await l5.close()

        print("== 6. which app may use which connection: the live grants, for the restored connections")
        for db in ("live6", "restored6", "restored7"):
            psql("postgres", "-c", f"CREATE DATABASE {db} TEMPLATE tmpl")
        # live7 is a database from before v201: the same files, stopped short of it.
        psql("postgres", "-c", "CREATE DATABASE live7")
        psql("live7", "--single-transaction", sql_file=ROOT / "init-db.sql")
        for f in sorted((ROOT / "migrations").glob("v*.sql"), key=lambda p: int(p.name[1:].split("_")[0])):
            if int(f.name[1:].split("_")[0]) < 201:
                r = psql("live7", sql_file=f)
                if r.returncode != 0:
                    raise SystemExit(f"FAIL: {f.name} did not apply to the pre-v201 database: {r.stderr[-300:]}")
        r6, l6 = await conn("restored6"), await conn("live6")
        # Restored: c1 (both have it) and cR (only the backup). Live: c1 and cL
        # (connected since). The backup granted old-app on c1 and x on cR; live,
        # old-app was revoked and mail was granted read-only on c1 and cal on cL.
        for cid in ("c1", "cR"):
            await integ_conn(r6, cid, "5")
        for cid in ("c1", "cL"):
            await integ_conn(l6, cid, "5")
        await r6.execute("INSERT INTO integration_grants VALUES ('c1', 'old-app', NULL, 't'), "
                         "('cR', 'x', NULL, 't')")
        await l6.execute("INSERT INTO integration_grants VALUES ('c1', 'mail', '[\"GET\"]', 't'), "
                         "('cL', 'cal', NULL, 't')")
        await hr.carry_rows(l6, r6)
        check("the live grants hold: one revoked since stays revoked, one on a connection only the backup "
              "has does not come back, one on a connection the backup lacks is dropped",
              await integ_grants(r6) == [("c1", "mail", '["GET"]')], await integ_grants(r6))
        check("the restored connections stay as the backup had them",
              sorted(x[0] for x in await r6.fetch("SELECT id FROM integration_connections")) == ["c1", "cR"])
        bad = await all_keys_hold(r6)
        check("every foreign key in the schema holds afterwards", not bad, bad)
        await r6.close()
        await l6.close()

        r7, l7 = await conn("restored7"), await conn("live7")
        # The database live until the swap is from before v201 (a restore
        # staged on the old version): no integration tables; its grants are in
        # the SQLite file the swap set aside. The backup is from before v201
        # too: its connections are in the file the swap brought back.
        check("the pre-v201 live database has no integration tables",
              not await hr._columns(l7, "integration_grants"))
        prev7 = old_integrations_store(hr.PREVIOUS, [("c1", "5")],
                                       [("c1", "mail", '["GET"]'), ("c1", "cal", None), ("gone", "x", None)])
        live7 = old_integrations_store(hr.LIVE, [("c1", "5"), ("c2", "5")],
                                       [("c1", "old-app", None), ("c2", "mail", None)])
        await hr.carry_rows(l7, r7)
        check("version skew: the live grants come from the file the swap set aside, for the connections "
              "the backup's file brought in; the backup's own grants do not come back",
              await integ_grants(r7) == [("c1", "cal", None), ("c1", "mail", '["GET"]')], await integ_grants(r7))
        check("... and the backup's connections are in the restored database",
              sorted(x[0] for x in await r7.fetch("SELECT id FROM integration_connections")) == ["c1", "c2"])
        bad = await all_keys_hold(r7)
        check("every foreign key in the schema holds afterwards", not bad, bad)
        prev7.unlink()
        live7.unlink()
        await r7.close()
        await l7.close()

        print("== 5. what the live bastions said is carried with them")
        check("rows keyed by bastion token ids are carried",
              {"network_bastion_heartbeat", "network_bastion_other", "bastion_update_refusals",
               "bastion_autoupdate_watch"} <= set(hr.CARRIED_TABLES))

    async def run_owner():
        # The owner's sign-in across a restore, driven through the real
        # owner_login and owner_passkeys on the restored database, the way the
        # kernel's start runs them (server/main.py: owner_login.init). Before
        # the owner's account was carried, the restored database held the
        # backup's: another email, password and cookie name, the browser that
        # asked for the restore signed out, and from a backup taken before the
        # owner was made no account at all, so a setup code only the kernel's
        # log shows.
        print("== 7. the owner signs in after a restore as before it, and no setup code is made")
        import base64
        import json
        import logging
        import struct
        from datetime import datetime, timezone

        import cbor2
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        from server import owner_login as L
        from server import owner_passkeys as P
        from server.home_origin import HOME
        os.environ.pop("CSERIES_SETUP_CODE_FILE", None)
        for db in ("live8", "restored8", "live9", "restored9", "live10", "restored10"):
            psql("postgres", "-c", f"CREATE DATABASE {db} TEMPLATE tmpl")
        live_email, backup_email = "owner@live.example", "someone@backup.example"
        pw_live, pw_backup = "live password, long enough", "backup password, long enough"
        client, app_host = "198.51.100.7", f"apps-settings-{HOME.host}"

        class Said(logging.Handler):
            def __init__(self):
                super().__init__(logging.DEBUG)
                self.lines = []

            def emit(self, record):
                self.lines.append(record.getMessage())
        said = Said()
        logging.getLogger("cseries.owner_login").addHandler(said)
        logging.getLogger("cseries.owner_login").setLevel(logging.DEBUG)

        async def owner(c, email, password, suffix):
            await c.execute("INSERT INTO owner_account (id, email, password_hash, cookie_suffix) "
                            "VALUES (1, $1, $2, $3)", email, await L.hash_password(password), suffix)

        async def session(c, token, host=None, parent=None):
            now = datetime.now(timezone.utc)
            await c.execute("INSERT INTO owner_sessions (token_hash, created_at, last_seen_at, expires_at, "
                            "origin, parent) VALUES ($1, $2, $2, $3, $4, $5)", L._token_hash(token), now,
                            now + L.SESSION_MAX, host or HOME.host, L._token_hash(parent) if parent else None)

        async def boot(c):
            """The kernel's start on the restored database: owner_login.init."""
            L._pool = lambda: c
            L._owner_email = L._setup_code = L._cookie_suffix = None
            for d in (L._cache, L._touched, L._failures, L._signed_out, L._joins, P._ceremonies, P._step_ups):
                d.clear()
            L._password_failures = (0, 0.0, 0.0)
            said.lines.clear()
            await L.init()

        def no_setup_code():
            return (not L.needs_setup() and L._setup_code is None
                    and not any("SETUP CODE" in m or "no owner yet" in m for m in said.lines))

        async def refused(coro):
            try:
                await coro
                return None
            except L.LoginError as e:
                return str(e)

        def b64u(x):
            return base64.urlsafe_b64encode(x).decode().rstrip("=")

        class Passkey:
            """A passkey on a pretend device (ES256), as scripts/test_owner_passkeys_unit.py has it."""

            def __init__(self):
                self.key, self.cred_id, self.counter = ec.generate_private_key(ec.SECP256R1()), os.urandom(16), 0

            def cose(self):
                n = self.key.public_key().public_numbers()
                return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

            def get(self, options):
                self.counter += 1
                cdj = json.dumps({"type": "webauthn.get", "challenge": options["challenge"],
                                  "origin": HOME.origin, "crossOrigin": False}).encode()
                auth = hashlib.sha256(options["rpId"].encode()).digest() + b"\x05" + struct.pack(">I", self.counter)
                sig = self.key.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
                return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                        "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth),
                                     "signature": b64u(sig), "userHandle": b64u(P.USER_HANDLE)},
                        "clientExtensionResults": {}}

        print("-- a password owner; the backup's owner had another email")
        r8, l8 = await conn("restored8"), await conn("live8")
        await owner(l8, live_email, pw_live, "live0001")
        await session(l8, "the-restoring-browser")
        await session(l8, "its-app-origin", host=app_host, parent="the-restoring-browser")
        await owner(r8, backup_email, pw_backup, "back0001")
        await session(r8, "a-session-in-the-backup")
        try:
            done8 = await hr.carry_rows(l8, r8, stamp="20261019T020000Z")
        except Exception as e:
            done8 = {"failed": repr(e)}
        check("the carry succeeds with both owners", "failed" not in done8, done8)
        check("every foreign key in the schema holds afterwards (a session's parent included)",
              not await all_keys_hold(r8), await all_keys_hold(r8))
        await boot(r8)
        check("the restored Host starts with an owner and makes no setup code", no_setup_code(), said.lines)
        check("the owner is the live one, not the backup's", L.owner_email() == live_email, L.owner_email())
        check("the session cookie keeps its name, so the browser's cookie still counts",
              L.cookie_name().endswith("_live0001"), L.cookie_name())
        check("the browser that asked for the restore is still signed in, and its app origin too",
              await L.session_email("the-restoring-browser", HOME.host) == live_email
              and await L.session_email("its-app-origin", app_host) == live_email)
        check("a session the backup held does not come back",
              await L.session_email("a-session-in-the-backup", HOME.host) is None)
        try:
            token = await L.sign_in(live_email, pw_live, client, HOME.host)
        except L.LoginError as e:
            token = e
        check("the owner signs in with the live email and password",
              isinstance(token, str) and await L.session_email(token, HOME.host) == live_email, token)
        check("the backup's email and password do not sign in",
              await refused(L.sign_in(backup_email, pw_backup, client, HOME.host)) is not None)
        note = done8.get("owner_note") or ""
        check("the restore says plainly that the backup's owner had another email, and who signs in",
              "another email" in note and live_email in note and backup_email not in note, note)
        mark = await r8.fetchval("SELECT v FROM host_backup_state WHERE k = $1", hr.CARRIED_MARKER)
        mark = json.loads(mark) if isinstance(mark, str) else mark
        check("  and it is kept with the carry, for the restore's result", (mark or {}).get("owner") == note, mark)
        await r8.close()
        await l8.close()

        print("-- a passkey owner; the backup was taken before the owner was made")
        r9, l9 = await conn("restored9"), await conn("live9")
        dev = Passkey()
        await owner(l9, live_email, pw_live, "live0002")
        await l9.execute("INSERT INTO owner_passkeys (credential_id, public_key) VALUES ($1, $2)",
                         dev.cred_id, dev.cose())
        codes = await P.new_recovery_codes(l9)
        await session(l9, "the-restoring-browser-2")
        try:
            done9 = await hr.carry_rows(l9, r9, stamp="20261019T030000Z")
        except Exception as e:
            done9 = {"failed": repr(e)}
        check("the carry succeeds onto a backup with no owner", "failed" not in done9, done9)
        await boot(r9)
        check("the restored Host starts with the live owner and makes no setup code",
              no_setup_code() and L.owner_email() == live_email, said.lines)
        check("the browser that asked for the restore is still signed in",
              await L.session_email("the-restoring-browser-2", HOME.host) == live_email)
        check("the password the passkey retired stays retired",
              "passkey" in (await refused(L.sign_in(live_email, pw_live, client, HOME.host)) or ""))
        try:
            opts = await P.login_options(client, HOME.host)
            token = await P.passkey_sign_in(opts["ceremony"], dev.get(opts["options"]), client, HOME.host)
        except L.LoginError as e:
            token = e
        check("the owner signs in with the live passkey",
              isinstance(token, str) and await L.session_email(token, HOME.host) == live_email, token)
        try:
            token = await P.recovery_sign_in(codes[0], client, HOME.host)
        except L.LoginError as e:
            token = e
        check("  and with a live recovery code",
              isinstance(token, str) and await L.session_email(token, HOME.host) == live_email, token)
        check("  and the restore has nothing to say about the sign-in", "owner_note" not in done9, done9)
        await r9.close()
        await l9.close()

        print("-- no owner yet when the restore ran; the backup had one")
        r10, l10 = await conn("restored10"), await conn("live10")
        await owner(r10, backup_email, pw_backup, "back0003")
        done10 = await hr.carry_rows(l10, r10)
        check("the backup's owner is not brought back: this Host had none",
              await r10.fetchval("SELECT count(*) FROM owner_account") == 0)
        check("  and the restore says so, and where to make one",
              "no owner account" in (done10.get("owner_note") or "") and "/setup" in done10.get("owner_note", ""),
              done10)
        await r10.close()
        await l10.close()
        logging.getLogger("cseries.owner_login").removeHandler(said)

    asyncio.run(run())
    asyncio.run(run_more())
    asyncio.run(run_owner())
finally:
    subprocess.run(["docker", "stop", name], capture_output=True)   # --rm removes it
    shutil.rmtree(work, ignore_errors=True)

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: the carry holds on the real schema")
