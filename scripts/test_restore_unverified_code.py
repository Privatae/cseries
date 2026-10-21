#!/usr/bin/env python3
"""Gate: a backup this Host cannot confirm it made brings back no installed
code and no app row (server/host_restore.py leave_code_out, leave_app_rows_out,
_reinstall_apps), and its apps' data still comes back.

The backup is crafted, as anyone who can write where backups are kept could
make one (it is encrypted to the owner's public key):
  * a built-in app's code replaced (apps/claude-code/bin/main.py), with the
    .factory-fingerprint of the image's bundle beside it, so the boot's image
    install would keep it;
  * a store app's code replaced (apps/notes/index.js);
  * a Host-built app's installed code replaced (apps/board/main.py), while its
    project's files (projects/7) are the Host's source;
  * the installed App SDK replaced (sdk/), with a link to it;
  * installed_applications rows that forge trust: the store app marked
    first-party ('standard', so applications/proxy.py would serve it on the
    Host's own origin), an id that is not an app id;
  * each app's data (appdata/notes);
  * a platform skill (skills/desktop.md, the folder only the image's seeders
    write) with a seed marker that disagrees with it, so the boot's seeding
    would keep it as the owner's edit, and it would ride turns as the
    platform's own text;
  * a network passport (passport/: a key, a certificate and root.crt, the
    root the member door is verified against), and a link from an app's data
    to where this Host keeps its own passport key.

Staged with the owner's say-so and swapped in on a folder tree, the way a boot
does: no planted byte is anywhere in the live tree, no app row is left, every
app is put back from the image, the store or its project (or marked to
reinstall, with why), and the data is the backup's. A backup this Host made
still comes back whole.

    python3 scripts/test_restore_unverified_code.py
"""
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import host_backup as hb  # noqa: E402
from server import host_restore as hr  # noqa: E402
from server.applications import installer, proxy, registry  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


def put(base: Path, rel: str, text: str):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


PLANTED = "PLANTED-BY-THE-BACKUP"
BOARD_YAML = "id: board\nname: Board\nprovenance: custom\nentrypoint: index.html\n"


def crafted(staged: Path):
    put(staged, "apps/claude-code/bin/main.py", f"print('{PLANTED} 1')\n")
    put(staged, "apps/claude-code/app.yaml", "id: claude-code\nname: Claude Code\n")
    put(staged, "apps/claude-code/.factory-fingerprint", "ab" * 32)
    put(staged, "apps/notes/index.js", f"// {PLANTED} 2\n")
    put(staged, "apps/board/main.py", f"print('{PLANTED} 3')\n")
    put(staged, "sdk/1.0.0/app-sdk.js", f"// {PLANTED} 4\n")
    os.symlink("1.0.0", staged / "sdk/current")
    put(staged, "projects/7/app.yaml", BOARD_YAML)
    put(staged, "projects/7/main.py", "print('board, from its project')\n")
    put(staged, "appdata/notes/notes.json", '{"from": "the backup"}\n')
    put(staged, "skills/desktop.md", f"---\nname: desktop\n---\n{PLANTED} 5\n")
    put(staged, "skills/.seeded/desktop.md.sha256", "00" * 32 + "\n")
    put(staged, "passport/root.crt", f"{PLANTED} 6\n")
    put(staged, "passport/host.key", f"{PLANTED} 7\n")
    put(staged, "passport/passport.json", f'{{"door": "{PLANTED} 8"}}\n')


def live_tree(live: Path):
    put(live, "apps/claude-code/bin/main.py", "print('the image')\n")
    put(live, "apps/notes/index.js", "// the store's\n")
    put(live, "sdk/1.0.0/app-sdk.js", "// the image's SDK\n")
    put(live, "appdata/notes/notes.json", '{"from": "this machine"}\n')
    put(live, "skills/desktop.md", "---\nname: desktop\n---\nthe image's skill\n")
    put(live, "passport/host.key", "this Host's own passport key\n")
    put(live, "passport/root.crt", "the network's root\n")


class FakeRestoreDb:
    """The restore's database, as the load left it: the backup's rows."""

    def __init__(self):
        self.rows = [
            {"id": "board", "provenance": "custom", "status": "active"},
            {"id": "claude-code", "provenance": "native", "status": "active"},
            {"id": "notes", "provenance": "standard", "status": "active"},   # forged: a store app
            {"id": "../../app/server", "provenance": "native", "status": "active"},
            {"id": "old", "provenance": "community", "status": "disabled"},
        ]

    async def fetch(self, q, *a):
        if "information_schema.columns" in q:
            return [{"column_name": c} for c in ("id", "name", "version", "provenance", "entrypoint",
                                                 "source_dir", "status")] if a[0] == "installed_applications" else []
        if 'FROM "installed_applications"' in q:
            return [dict(r) for r in self.rows]
        raise AssertionError(q)

    async def execute(self, q, *a):
        if q == 'DELETE FROM "installed_applications"':
            self.rows = []
            return "DELETE"
        raise AssertionError(q)

    async def close(self):
        pass


class FakePool:
    async def fetchval(self, q, *a):
        return None             # no backup of its own: no before-restore backup

    async def execute(self, q, *a):
        return "OK"

    async def fetch(self, q, *a):
        if "FROM projects" in q:
            return [{"id": 7, "name": "Board"}]
        if "FROM project_modules" in q:
            return []
        raise AssertionError(q)


def run_restore(verified: bool):
    base = Path(tempfile.mkdtemp(prefix="unverified-code-", dir=os.environ.get("TMPDIR")))
    live = base / "cseries"
    hr.LIVE = live
    hr.RESTORE_DIR = live / ".host-restore"
    hr.STAGED = hr.RESTORE_DIR / "apps"
    hr.APP_DATA = hr.RESTORE_DIR / "app-data"
    hr.PREVIOUS = hr.RESTORE_DIR / "previous"
    hr.PLAN = hr.RESTORE_DIR / "plan.json"
    hr.MANIFEST = hr.RESTORE_DIR / "manifest"
    live_tree(live)
    db = FakeRestoreDb()

    async def _a(*a, **k):
        return None

    async def _prov(pool, body):
        return verified

    async def _entry(pool, eid, stamp, where=None):
        return {"where": "local", "key": "k", "bytes": 10, "taken_at": "2026-10-01T00:00:00+00:00"}

    async def _src(pool, eid, entry):
        return io.BytesIO(b"")

    def _unpack(src, identity, staged, **kw):
        crafted(staged)
        return {"files": 9, "entity_id": 42}

    async def _connect(name):
        assert name == hr.RESTORE_DB, name
        return db

    async def _ident(pool):
        return "AGE-SECRET-KEY-1INVENTED"

    hb.configured = lambda: None
    hb.open_sidecar = lambda held, ident, stamp=None: {"identity": "AGE-SECRET-KEY-1INVENTED"}
    hr.load_resealed = lambda stamp: b"held"
    hr.drop_resealed = lambda stamp=None: None
    hr.provenance, hr._host_identity, hr._progress = _prov, _ident, _a
    hr._entry, hr._open_source, hr.unpack, hr._connect = _entry, _src, _unpack, _connect
    hr._clear_staging = _a
    hr._room = lambda p: hr.Room(free=1 << 40, unit=4096, inodes_free=1 << 30, inodes=1 << 30)
    hr._reserve = lambda room: (0, None, "")
    hr._restart = lambda: None
    hr._databases = lambda: {"hostmem", hr.RESTORE_DB}
    hr._rename_pair = lambda a, b: None
    got = asyncio.run(hr._stage(FakePool(), 42, "20261001T000000Z", unverified_ok=True))
    rc = hr.swap("hostmem")
    return live, db, got, rc


def planted_in(live: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(live):
        if Path(dirpath) == live:
            dirnames[:] = [d for d in dirnames if d != ".host-restore"]
        for f in filenames:
            p = Path(dirpath) / f
            if not p.is_symlink() and PLANTED in p.read_text(errors="replace"):
                out.append(str(p.relative_to(live)))
    return sorted(out)


print("== a backup this Host cannot confirm it made")
live, db, got, rc = run_restore(verified=False)
check("it stages and swaps in", got.get("state") == hr.STAGED_STATE and rc == 0, (got, rc))
check("no planted byte is anywhere in the live tree (built-in app, store app, Host-built app, SDK)",
      planted_in(live) == [], planted_in(live))
check("  no installed code at all is left from the backup or the machine: the boot installs from the image",
      not [p for p in (live / "apps").rglob("*") if p.is_file() or p.is_symlink()]
      and not [p for p in (live / "sdk").rglob("*") if p.is_file() or p.is_symlink()],
      sorted(str(p) for p in live.rglob("*") if "apps/" in str(p) or "sdk/" in str(p)))
check("  so no restored .factory-fingerprint can make the boot keep a built-in app",
      not (live / "apps/claude-code/.factory-fingerprint").exists())
check("no installed_applications row of the backup is left (each is written again by its install)",
      db.rows == [], db.rows)
forged = [r["id"] for r in db.rows if not proxy.is_isolated(r["id"], r["provenance"])]
check("  so no forged row serves a store app on the Host's own origin", "notes" not in forged, forged)
plan = hr.read_plan() or {}
check("the plan names the apps to put back: app ids only, active ones, the Host-built one marked",
      [(a["app_id"], a["built_here"], a["state"]) for a in plan.get("apps_reinstall") or []]
      == [("board", True, "pending"), ("claude-code", False, "pending"), ("notes", False, "pending")],
      plan.get("apps_reinstall"))
check("  and says what it left out", plan.get("code_left_out") == ["apps", "sdk", "skills", "passport"],
      plan.get("code_left_out"))
check("no platform skill comes from the backup, nor its seed marker: the boot seeds them from the image",
      not (live / "skills/desktop.md").exists() and not (live / "skills/.seeded/desktop.md.sha256").exists(),
      sorted(str(p.relative_to(live)) for p in (live / "skills").rglob("*")) if (live / "skills").exists() else [])
check("this Host's passport is the one it had, through the swap (not the backup's, not set aside)",
      (live / "passport/host.key").read_text() == "this Host's own passport key\n"
      and (live / "passport/root.crt").read_text() == "the network's root\n"
      and not (live / "passport/passport.json").exists(),
      sorted(p.name for p in (live / "passport").iterdir()) if (live / "passport").exists() else [])
check("  and the plan says the swap keeps it", plan.get("live_kept") == ["passport"], plan.get("live_kept"))
# A link the backup holds into what the restore takes from this Host leads to
# this Host's own once live: an unverified restore leaves it out (the unpack's
# link check, the same one every unverified restore runs).
_lk = Path(tempfile.mkdtemp(prefix="unverified-links-", dir=os.environ.get("TMPDIR")))
for rel in ("passport/host.key", "skills/desktop.md", "appdata/notes/notes.json"):
    put(_lk, rel, "x\n")
_real = os.path.realpath(_lk)
for where, target, want in ((["appdata", "notes", "key"], "../../passport/host.key", True),
                            (["appdata", "notes", "skill"], "/var/lib/cseries/skills/desktop.md", True),
                            (["projects", "7", "data"], "../../appdata/notes/notes.json", False)):
    got_l = hr._link_escapes(_real, "/var/lib/cseries", where, target)
    check(f"a link {'/'.join(where)} -> {target} is {'left out' if want else 'kept'}", got_l is want, got_l)
check("the apps' data comes back as the backup had it",
      (live / "appdata/notes/notes.json").read_text() == '{"from": "the backup"}\n')
check("  and the Host's project source comes back as files",
      (live / "projects/7/main.py").read_text() == "print('board, from its project')\n")

print("== at finish, each app goes back from where this Host takes code from")
store_calls, project_calls = [], []


async def _store_ok(pool, aid):
    store_calls.append(aid)


async def _project_ok(pool, pid, module):
    project_calls.append((pid, module))


async def _reg_get(pool, aid):
    return object() if aid == "claude-code" else None


installer.image_baked_ids = lambda: frozenset({"claude-code"})
registry.get = _reg_get
hr._install_from_store, hr._install_from_project = _store_ok, _project_ok
plan = plan if plan.get("apps_reinstall") else {"apps_reinstall": [
    {"app_id": i, "built_here": i == "board", "state": "pending"} for i in ("board", "claude-code", "notes")]}
try:
    asyncio.run(hr._reinstall_apps(FakePool(), plan))
except AttributeError as e:
    check("finish puts the apps back", False, repr(e))
st = {a["app_id"]: (a["state"], a.get("source"), a.get("project")) for a in plan["apps_reinstall"]}
check("the built-in app is the image's (the boot installed it)", st["claude-code"] == ("reinstalled", "image", None), st)
check("the store app is the App Store's signed bundle, through the install path", st["notes"][:2] == ("reinstalled", "store")
      and store_calls == ["notes"], (st, store_calls))
check("the Host-built app is built again from its project, through the normal ship path",
      st["board"] == ("reinstalled", "project", "Board") and project_calls == [(7, None)], (st, project_calls))
check("  the plan holds each outcome, so a finish cut short does not repeat them",
      (hr.read_plan() or {}).get("apps_reinstall") == plan["apps_reinstall"])
note = hr.apps_note(plan) if hasattr(hr, "apps_note") else None
check("the owner is told no installed code came back, and what was installed again",
      note and "could not confirm" in note and "notes (from the App Store)" in note
      and "board (from project Board)" in note, note)
check("  and that its network passport did not come back", note and "network passport" in note, note)

print("== and one that cannot be put back is said, with why")
for a in plan["apps_reinstall"]:
    a.update(state="pending", error=None, project=None)


async def _store_down(pool, aid):
    raise hr.RestoreError("the App Store did not give it: no App Store is reachable")


async def _project_fails(pool, pid, module):
    raise hr.RestoreError("app build rejected: no index.html")


async def _reg_none(pool, aid):
    return None


hr._install_from_store, hr._install_from_project, registry.get = _store_down, _project_fails, _reg_none
try:
    asyncio.run(hr._reinstall_apps(FakePool(), plan))
except AttributeError as e:
    check("finish says which apps it could not put back", False, repr(e))
st = {a["app_id"]: (a["state"], a.get("error")) for a in plan["apps_reinstall"]}
check("every one is marked to reinstall, each with its reason",
      all(s == "needs reinstall" and e for s, e in st.values()), st)
note = hr.apps_note(plan) if hasattr(hr, "apps_note") else None
check("  the owner's line names the project to reinstall from, and the store being unreachable",
      note and "board: reinstall it from project Board (app build rejected: no index.html)" in note
      and "notes: reinstall it from the App Store (the App Store did not give it: no App Store is reachable)"
      in note and "3 app(s) need reinstalling" in note, note)

print("== a backup this Host made comes back whole")
live, db, got, rc = run_restore(verified=True)
check("its apps' files come back as they were", (live / "apps/notes/index.js").read_text() == f"// {PLANTED} 2\n")
check("  and its rows", len(db.rows) == 5, db.rows)
check("  and its skills and passport, as it holds them",
      PLANTED in (live / "skills/desktop.md").read_text() and PLANTED in (live / "passport/host.key").read_text())
check("  and the swap keeps nothing of this Host's in their place", not (hr.read_plan() or {}).get("live_kept"))
check("  and nothing is to be put back", not (hr.read_plan() or {}).get("apps_reinstall")
      and (not hasattr(hr, "apps_note") or hr.apps_note(hr.read_plan() or {}) is None))

print("RESULT: " + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAIL"))
sys.exit(1 if FAILED else 0)
