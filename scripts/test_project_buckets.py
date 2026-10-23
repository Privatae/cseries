#!/usr/bin/env python3
"""Gate: project BUCKETS (the v130 folders) — grouping, placement, never a delete path.

Folders are called buckets in the UI and API, and a project has a place in
its bucket. The rules that cannot move:
  - a bucket holds archived AND active projects together (grouping is
    orthogonal to status — the counts below prove both land in one bucket);
  - deleting a bucket NEVER deletes a project (ON DELETE SET NULL: the
    projects survive, unfiled);
  - one drop is one request: PATCH /{id}/place files the project and
    renumbers the target bucket 1..n, the project in front of the one named.

Container-free by default (static checks + stub pools). With
CSERIES_TEST_PG_HOST=<dir holding .s.PGSQL.5432> it also runs the gates against
a real database that carries init-db.sql + migrations.

  APP_ROOT=<repo> python3 scripts/test_project_buckets.py
  CSERIES_TEST_PG_HOST=/tmp/wbpg-sock python3 scripts/test_project_buckets.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT), str(ROOT / "brain")):
    if p not in sys.path:
        sys.path.insert(0, p)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from server.routes import projects as P  # noqa: E402

# ── 1. schema: declared for fresh entities AND migrated for long-lived ones ──
init = (ROOT / "init-db.sql").read_text()
mig = (ROOT / "migrations" / "v130_project_folders.sql").read_text()
check("init-db.sql declares project_folders", "CREATE TABLE project_folders" in init)
check("init-db.sql: projects.folder_id references the folder ON DELETE SET NULL",
      re.search(r"folder_id\s+INTEGER\s+REFERENCES\s+project_folders\(id\)\s+ON\s+DELETE\s+SET\s+NULL", init) is not None)
check("v130 is idempotent (IF NOT EXISTS on table, index and column)",
      "CREATE TABLE IF NOT EXISTS project_folders" in mig
      and "ADD COLUMN IF NOT EXISTS folder_id" in mig
      and "CREATE INDEX IF NOT EXISTS idx_projects_folder" in mig)
check("v130: the FK is ON DELETE SET NULL, never CASCADE",
      "ON DELETE SET NULL" in mig and "CASCADE" not in mig)
check("bucket names are unique per entity, case-insensitively",
      "lower(name)" in mig and "lower(name)" in init)
mig173 = (ROOT / "migrations" / "v173_projects_sort_order.sql").read_text()
check("v173 adds projects.sort_order idempotently, nullable (never placed = NULL)",
      "ADD COLUMN IF NOT EXISTS sort_order INTEGER;" in mig173)
check("init-db.sql declares projects.sort_order for fresh entities",
      re.search(r"\n\s+sort_order\s+INTEGER,\s*\n\s+-- v131", init) is not None)

# ── 2. routes: literal paths registered before /{project_id} ─────────────────
paths = [r.path for r in P.router.routes]
first_dyn = next(i for i, p in enumerate(paths) if p == "/api/projects/{project_id}")
for lit in ("/api/projects/buckets", "/api/projects/buckets/{bucket_id}", "/api/projects/search"):
    check(f"{lit} is registered before /{{project_id}}",
          lit in paths and paths.index(lit) < first_dyn, repr(paths[:12]))
check("the old /folders paths are gone (renamed, no alias)",
      not any("/folders" in p for p in paths), repr([p for p in paths if "folders" in p]))
check("PATCH /api/projects/{project_id}/place is registered",
      "/api/projects/{project_id}/place" in paths)
check("PUT /api/projects/{id} accepts bucket_id (not folder_id)",
      "bucket_id" in P.UpdateProject.model_fields and "folder_id" not in P.UpdateProject.model_fields)
route_src = (ROOT / "server/routes/projects.py").read_text()
check("the PUT route tells an explicit null (unfile) from an absent field",
      'if "bucket_id" in body.model_fields_set else _UNSET' in route_src)


# ── 3. stub pools: the service writes what it says ───────────────────────────
class _Pool:
    def __init__(self, *, folder_row=None, execute_result="UPDATE 1", count=0):
        self.folder_row, self.execute_result, self.count = folder_row, execute_result, count
        self.calls: list[tuple[str, str, tuple]] = []

    async def fetchrow(self, q, *a):
        self.calls.append(("fetchrow", q, a))
        if "FROM project_folders" in q:
            return self.folder_row
        return None

    async def fetchval(self, q, *a):
        self.calls.append(("fetchval", q, a))
        return self.count

    async def fetch(self, q, *a):
        self.calls.append(("fetch", q, a))
        return []

    async def execute(self, q, *a):
        self.calls.append(("execute", q, a))
        return self.execute_result

    # acquire()/transaction(): the connection is this stub itself.
    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False
        return _Ctx()

    def transaction(self):
        return self.acquire()


async def _stub_checks():
    pool = _Pool()
    await P.update_project_service(pool, 9003, 10, bucket_id=None)
    upd = next(q for k, q, _ in pool.calls if k == "execute")
    check("bucket_id=None UNFILES (folder_id = NULL), entity-scoped",
          "folder_id = NULL" in upd and "entity_id = $2" in upd, upd)

    pool = _Pool(folder_row=None)
    try:
        await P.update_project_service(pool, 9003, 10, bucket_id=99)
        check("filing into a bucket the entity does not own is refused", False)
    except P.ProjectsError as e:
        check("filing into a bucket the entity does not own is refused (404)", e.status == 404, str(e))

    pool = _Pool(folder_row={"id": 7})
    await P.update_project_service(pool, 9003, 10, bucket_id=7)
    writes = [q for k, q, _ in pool.calls if k == "execute"]
    upd = next(q for q in writes if "UPDATE projects" in q)
    check("filing writes folder_id = $N after the ownership check", "folder_id = $3" in upd, upd)
    check("...under that bucket's drop lock, taken before the write",
          "pg_advisory_xact_lock" in writes[0] and writes.index(upd) > 0, repr(writes))

    pool = _Pool()
    await P.update_project_service(pool, 9003, 10, name="x")
    upd = next(q for k, q, _ in pool.calls if k == "execute")
    check("a rename leaves folder_id alone (the _UNSET default)", "folder_id" not in upd, upd)

    pool = _Pool(folder_row={"id": 7}, count=12)
    r = await P.delete_bucket_service(pool, 9003, 7)
    dels = [q for k, q, _ in pool.calls if k == "execute"]
    locks = [q for q in dels if "pg_advisory_xact_lock" in q]
    dels = [q for q in dels if "pg_advisory_xact_lock" not in q]
    deletes = [q for q in dels if "DELETE" in q]
    others = [q for q in dels if "DELETE" not in q]
    check("deleting a bucket takes the drop lock of that bucket and of unfiled first", len(locks) == 2, repr(locks))
    check("deleting a bucket deletes ONLY the project_folders row and reports how many were unfiled",
          len(deletes) == 1 and "DELETE FROM project_folders" in deletes[0]
          and r == {"deleted": True, "unfiled": 12}, repr((dels, r)))
    check("...and the only other write clears its projects' positions, entity-scoped",
          len(others) == 1 and "SET sort_order = NULL" in others[0] and "entity_id = $2" in others[0],
          repr(others))

    try:
        await P.create_bucket_service(_Pool(), 9003, "   ")
        check("an empty bucket name is refused", False)
    except P.ProjectsError as e:
        check("an empty bucket name is refused (400)", e.status == 400)
    try:
        await P.create_bucket_service(_Pool(), 9003, "Clients", color="javascript:alert(1)")
        check("a non-swatch color is refused", False)
    except P.ProjectsError as e:
        check("a non-swatch color is refused (400)", e.status == 400)


    try:
        await P.place_project_service(_Pool(), 9003, 10, bucket_id=None, before_id=10)
        check("a project cannot be placed in front of itself", False)
    except P.ProjectsError as e:
        check("a project cannot be placed in front of itself (400)", e.status == 400, str(e))


asyncio.run(_stub_checks())


# ── 4. the plan's gate, against a real database when one is offered ──────────
async def _db_gate(host: str):
    import asyncpg
    pool = await asyncpg.create_pool(host=host, user=os.environ.get("CSERIES_TEST_PG_USER", "entity"),
                                     database=os.environ.get("CSERIES_TEST_PG_DB", "hostmem"),
                                     min_size=1, max_size=2)
    eid = 990_130
    try:
        await pool.execute("DELETE FROM projects WHERE entity_id = $1", eid)
        await pool.execute("DELETE FROM project_folders WHERE entity_id = $1", eid)
        f = await P.create_bucket_service(pool, eid, "Clients", color="#3366ff")
        fid = f["id"]
        try:
            await P.create_bucket_service(pool, eid, "clients")
            check("db: a same-name bucket (any case) is refused", False)
        except P.ProjectsError as e:
            check("db: a same-name bucket (any case) is refused (409)", e.status == 409)
        active = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Acme site', 'active') RETURNING id", eid)
        archived = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Old Acme site', 'archived') RETURNING id", eid)
        loose = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Unfiled thing', 'planning') RETURNING id", eid)
        await P.update_project_service(pool, eid, active, bucket_id=fid)
        await P.update_project_service(pool, eid, archived, bucket_id=fid)
        listing = await P.list_buckets_service(pool, eid)
        row = next(x for x in listing["buckets"] if x["id"] == fid)
        check("db: the bucket holds the archived and the active project together (counts)",
              row["counts"]["active"] == 1 and row["counts"]["archived"] == 1 and row["counts"]["total"] == 2,
              repr(row["counts"]))
        check("db: unfiled counts the loose project only",
              listing["unfiled"]["counts"] == {"active": 0, "pending": 1, "completed": 0, "archived": 0, "total": 1},
              repr(listing["unfiled"]))
        # The rail's one list call sees all four groups; the default still hides archived.
        P._pool = lambda: pool  # noqa: E731 — the route reads the module-level pool/eid
        P._eid = lambda: eid  # noqa: E731
        all_rows = (await P.list_projects(include_archived=True, limit=100))["projects"]
        dflt_rows = (await P.list_projects(limit=100))["projects"]
        check("db: include_archived=true returns every status with bucket_id + sort_order + group on each row",
              {r["id"] for r in all_rows} == {active, archived, loose}
              and next(r for r in all_rows if r["id"] == archived)["group"] == "archived"
              and next(r for r in all_rows if r["id"] == active)["bucket_id"] == fid
              and all("sort_order" in r for r in all_rows), repr(all_rows))

        # ── placement: one drop = one request, the bucket renumbered 1..n ──
        stamp = await pool.fetchval("SELECT updated_at FROM projects WHERE id = $1", loose)
        r1 = await P.place_project_service(pool, eid, loose, bucket_id=fid, before_id=None)
        check("db: a drop with no target row files the project LAST in the bucket",
              r1["order"][-1] == loose and set(r1["order"]) == {active, archived, loose}, repr(r1))
        r2 = await P.place_project_service(pool, eid, loose, bucket_id=fid, before_id=active)
        rows = await pool.fetch(
            "SELECT id, folder_id, sort_order FROM projects WHERE entity_id = $1 AND folder_id = $2 ORDER BY sort_order",
            eid, fid)
        check("db: a drop in front of a row puts it there, positions 1..n with no gaps",
              [r["sort_order"] for r in rows] == [1, 2, 3]
              and [r["id"] for r in rows].index(loose) == [r["id"] for r in rows].index(active) - 1
              and r2["order"] == [r["id"] for r in rows], repr([dict(r) for r in rows]))
        check("db: placing never touches updated_at (reordering is not work on the project)",
              await pool.fetchval("SELECT updated_at FROM projects WHERE id = $1", loose) == stamp)
        r3 = await P.place_project_service(pool, eid, loose, bucket_id=None, before_id=None)
        check("db: a drop on Unfiled takes it out of the bucket",
              await pool.fetchval("SELECT folder_id FROM projects WHERE id = $1", loose) is None
              and r3["bucket_id"] is None, repr(r3))
        try:
            await P.place_project_service(pool, eid, loose, bucket_id=fid, before_id=loose + 99999)
            check("db: a target row that is not in the bucket is refused", False)
        except P.ProjectsError as e:
            check("db: a target row that is not in the bucket is refused (409), nothing moved",
                  e.status == 409 and await pool.fetchval(
                      "SELECT folder_id FROM projects WHERE id = $1", loose) is None, str(e))
        try:
            await P.place_project_service(pool, eid + 1, loose, bucket_id=None)
            check("db: another entity cannot place this project", False)
        except P.ProjectsError as e:
            check("db: another entity cannot place this project (404)", e.status == 404)
        # A move through PUT (the viewer's bulk move) carries no position in.
        await P.place_project_service(pool, eid, loose, bucket_id=fid, before_id=None)
        await P.update_project_service(pool, eid, loose, bucket_id=None)
        check("db: a bucket change through PUT clears the old position",
              await pool.fetchval("SELECT sort_order FROM projects WHERE id = $1", loose) is None)

        # Overlapping drops into ONE bucket: serialized, never a deadlock, and
        # the bucket stays 1..n with no duplicate position.
        extra = [await pool.fetchval(
            "INSERT INTO projects (entity_id, name, status) VALUES ($1, $2, 'active') RETURNING id",
            eid, f"race {i}") for i in range(4)]
        errs = []
        for _ in range(25):
            res = await asyncio.gather(*[
                P.place_project_service(pool, eid, x, bucket_id=fid, before_id=None) for x in extra],
                return_exceptions=True)
            errs += [type(e).__name__ for e in res if isinstance(e, BaseException)]
        pos = [r["sort_order"] for r in await pool.fetch(
            "SELECT sort_order FROM projects WHERE entity_id = $1 AND folder_id = $2", eid, fid)]
        check("db: 100 overlapping drops into one bucket all succeed (no deadlock)", not errs, repr(errs[:5]))
        check("db: ...and the bucket reads 1..n with no duplicate position",
              sorted(pos) == list(range(1, len(pos) + 1)), repr(sorted(pos)))
        # Two drops SWAPPING projects between two buckets at the same instant:
        # both buckets are locked in one order by every drop, so no deadlock.
        other = (await P.create_bucket_service(pool, eid, "Swap target"))["id"]
        a, b = extra[0], extra[1]
        await P.place_project_service(pool, eid, b, bucket_id=other, before_id=None)
        swap_errs = []
        for _ in range(40):
            res = await asyncio.gather(
                P.place_project_service(pool, eid, a, bucket_id=other, before_id=None),
                P.place_project_service(pool, eid, b, bucket_id=fid, before_id=None),
                return_exceptions=True)
            swap_errs += [type(e).__name__ for e in res if isinstance(e, BaseException)]
            res = await asyncio.gather(
                P.place_project_service(pool, eid, a, bucket_id=fid, before_id=None),
                P.place_project_service(pool, eid, b, bucket_id=other, before_id=None),
                return_exceptions=True)
            swap_errs += [type(e).__name__ for e in res if isinstance(e, BaseException)]
        check("db: 80 pairs of simultaneous opposite cross-bucket drops all succeed (no deadlock)",
              not swap_errs, repr(swap_errs[:5]))
        # A PUT "move" to the bucket it is already in keeps its place.
        await P.place_project_service(pool, eid, a, bucket_id=fid, before_id=None)
        kept = await pool.fetchval("SELECT sort_order FROM projects WHERE id = $1", a)
        await P.update_project_service(pool, eid, a, bucket_id=fid)
        check("db: a PUT to the bucket it is already in keeps its position",
              kept is not None and await pool.fetchval(
                  "SELECT sort_order FROM projects WHERE id = $1", a) == kept)
        # A bucket deleted while drops land in it: the delete queues behind
        # (or ahead of) them on the bucket's lock — never a deadlock.
        race_errs = []
        for i in range(15):
            doomed = (await P.create_bucket_service(pool, eid, f"Doomed {i}"))["id"]
            for x in extra[:2]:
                await P.place_project_service(pool, eid, x, bucket_id=doomed, before_id=None)
            res = await asyncio.gather(
                P.place_project_service(pool, eid, extra[2], bucket_id=doomed, before_id=None),
                P.place_project_service(pool, eid, extra[3], bucket_id=doomed, before_id=None),
                P.delete_bucket_service(pool, eid, doomed),
                return_exceptions=True)
            race_errs += [type(e).__name__ for e in res
                          if isinstance(e, BaseException) and not (
                              isinstance(e, P.ProjectsError) and e.status == 404)]
        check("db: deleting a bucket while drops land in it never deadlocks",
              not race_errs, repr(race_errs[:5]))
        # The same race through PUT (the viewer's bulk move).
        put_errs = []
        for i in range(15):
            doomed = (await P.create_bucket_service(pool, eid, f"Doomed put {i}"))["id"]
            res = await asyncio.gather(
                P.update_project_service(pool, eid, extra[0], bucket_id=doomed),
                P.update_project_service(pool, eid, extra[1], bucket_id=doomed),
                P.delete_bucket_service(pool, eid, doomed),
                return_exceptions=True)
            put_errs += [type(e).__name__ for e in res
                         if isinstance(e, BaseException) and not (
                             isinstance(e, P.ProjectsError) and e.status == 404)]
        check("db: filing into a bucket through PUT while it is deleted is a 404 or lands, never a 500",
              not put_errs, repr(put_errs[:5]))
        await P.delete_bucket_service(pool, eid, other)
        await pool.execute("DELETE FROM projects WHERE id = ANY($1::int[])", extra)
        await P.place_project_service(pool, eid, loose, bucket_id=fid, before_id=None)
        await P.update_project_service(pool, eid, loose, bucket_id=None)
        check("db: the default list still hides archived (host tool contract untouched)",
              {r["id"] for r in dflt_rows} == {active, loose})
        await P.place_project_service(pool, eid, active, bucket_id=fid, before_id=None)
        d = await P.delete_bucket_service(pool, eid, fid)
        check("db: a deleted bucket's projects come back unfiled with no position",
              await pool.fetchval("SELECT sort_order FROM projects WHERE id = $1", active) is None)
        check("db: delete reports the two projects it unfiled", d == {"deleted": True, "unfiled": 2}, repr(d))
        survivors = await pool.fetch(
            "SELECT id, folder_id, status FROM projects WHERE entity_id = $1 ORDER BY id", eid)
        check("db: both projects still exist after the bucket delete, unfiled, statuses intact",
              [(r["id"], r["folder_id"], r["status"]) for r in survivors]
              == [(active, None, "active"), (archived, None, "archived"), (loose, None, "planning")],
              repr([dict(r) for r in survivors]))
        check("db: the bucket row is gone",
              await pool.fetchval("SELECT COUNT(*) FROM project_folders WHERE entity_id = $1", eid) == 0)
    finally:
        await pool.execute("DELETE FROM projects WHERE entity_id = $1", eid)
        await pool.execute("DELETE FROM project_folders WHERE entity_id = $1", eid)
        await pool.close()


pg_host = os.environ.get("CSERIES_TEST_PG_HOST")
if pg_host:
    asyncio.run(_db_gate(pg_host))
else:
    print("  skip  real-database gate (set CSERIES_TEST_PG_HOST to run it)")

print("\nPASS project_buckets" if not fails else f"\nFAIL project_buckets: {fails} check(s)")
sys.exit(1 if fails else 0)
