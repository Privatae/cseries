#!/usr/bin/env python3
"""Gate: the exact seen mark against a real kernel schema.

A page's tab lights green when something lands on it after the person last had
it open. The reply the person WATCHED arrive (its turn ended with the page in
front) is written down after its stream has ended, by a task that first embeds
it (memory_engine._write_turn_row_landed: a failing embedder holds it ~92 s; in
PII mode the drain worker writes it later still), so it lands after the mark
the page sent when the turn ended. 3a told it from new work by time; now the
mark names the turn (PUT /seen {turn_token}, projects.seen_turns, v217) and the
list's unseen_landed_at leaves that reply out, whenever it lands. Two
properties, both through the real turn writers:

  A. a reply the person watched arrive never lights its tab, however slowly it
     is saved: an embedder that takes seconds, a row written after later
     marks, a held turn written over after the mark, two watched turns whose
     rows land after both marks and out of order, a turn marked twice.
  B. anything else that lands after the mark always does: an agent's report
     landing while the watched reply is still being written, a reply of
     another turn, a reply carrying a token watched on another page, a task
     that finished; and nothing from before the mark, the person's own words,
     or a page never marked does.

  M. migrations/v217 on the full current schema: the column and its CHECK are
     there, running the file over a populated table rewrites no row, a second
     run changes nothing, and the CHECK refuses 33 tokens and a NULL one.
  S. PUT /seen: a token is kept on the page, newest last, the last 32; the
     same token again moves to the end; a mark with no body works as before
     and keeps them; a token that is not one is refused (400) and stores
     nothing; another entity's project is a 404.
  P. GET /api/projects/{id}/page carries unseen_landed_at, the list row's.
  Q. the list is still one query, and the read starts at the mark: the
     replies index under a Limit, a handful of buffers on a page of 2,000
     replies.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; no pod, no network beyond
loopback.

  PYTHONPATH=brain python3 scripts/test_workbench_seen_mark_pg.py
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import uuid
from datetime import timedelta

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the seen mark's SQL and v217.")
    sys.exit(2)

FAILED = 0
E = 1
OTHER_E = 2
VERSION = 217
MIGRATION = next(iter(sorted((ROOT / "migrations").glob(f"v{VERSION}_*.sql"))), None)
# The slow save: the embedder takes this long before the row's INSERT. The real
# worst case is ~92 s (three tries of the bridge's 30 s timeout); what matters
# is that the row lands after the mark and after list reads, which this does.
SLOW_EMBED_S = 2.5
HOST = {"gen_ms": 1500, "model": "anthropic/claude-opus-5"}


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


async def section(name, coro_fn):
    """One lettered section; an exception fails that section, and the run goes
    on, so a tree without the feature shows every part it lacks."""
    print(f"-- {name}")
    try:
        await coro_fn()
    except Exception as e:  # noqa: BLE001 — reported as the section's failure
        check(f"{name}: ran to the end", False, f"{type(e).__name__}: {e}")


async def attempt(coro):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 — reported by the check that reads it
        return e


# ── M. the column ───────────────────────────────────────────────────────────

async def column_and_check(pool):
    col = await pool.fetchrow(
        "SELECT data_type, udt_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'projects' AND column_name = 'seen_turns'")
    chk = await pool.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'projects_seen_turns_check' AND conrelid = 'projects'::regclass")
    return (dict(col) if col else None), chk


async def migration_checks(dsn):
    check(f"M the migration file exists (v{VERSION})", MIGRATION is not None and MIGRATION.is_file(), MIGRATION)
    if MIGRATION is None:
        return
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        await pool.execute("INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Before', 'active')", E)
        node = await pool.fetchval("SELECT pg_relation_filenode('projects')")
        r1 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
        col, chk = await column_and_check(pool)
        check("M v217 runs again on the full schema and fails nothing", r1.returncode == 0, r1.stderr[-400:])
        check("M projects.seen_turns is text[], nullable, no default",
              col is not None and col["data_type"] == "ARRAY" and col["udt_name"] == "_text"
              and col["is_nullable"] == "YES" and col["column_default"] is None, col)
        check("M ...and the run rewrites no row (the same file, every row NULL)",
              await pool.fetchval("SELECT pg_relation_filenode('projects')") == node
              and await pool.fetchval("SELECT count(*) FROM projects WHERE seen_turns IS NOT NULL") == 0)
        check("M its CHECK holds at most 32 tokens and no NULL among them",
              chk is not None and "cardinality(seen_turns) <= 32" in chk and "array_position" in chk, chk)
        r2 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
        again = await column_and_check(pool)
        check("M a second run changes nothing and fails nothing",
              r2.returncode == 0 and again == (col, chk), (r2.stderr[-300:], again))
        pid = await pool.fetchval("SELECT id FROM projects WHERE name = 'Before'")
        for label, val in (("33 tokens", [f"t{i}" for i in range(33)]), ("a NULL token", ["a", None])):
            r = await attempt(pool.execute("UPDATE projects SET seen_turns = $2 WHERE id = $1", pid, val))
            check(f"M the CHECK refuses {label}", isinstance(r, asyncpg.CheckViolationError), r)
    finally:
        await pool.close()


# ── the behaviour ───────────────────────────────────────────────────────────

async def behaviour(dsn):
    from fastapi import HTTPException

    from mastercode import memory_engine as ME
    from server import brain_service as bs
    import server.routes.projects as P

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    saved = (bs._pool, bs._entity_id)
    bs._pool, bs._entity_id = pool, E
    real_embed = ME._get_memory_embedding

    async def no_embed(*_a, **_k):
        return None

    async def slow_embed(*_a, **_k):
        await asyncio.sleep(SLOW_EMBED_S)
        return None
    ME._get_memory_embedding = no_embed

    async def project(name, eid=E):
        return await pool.fetchval(
            "INSERT INTO projects (entity_id, name, status, updated_at, created_at) "
            "VALUES ($1, $2, 'active', now() - interval '2 days', now() - interval '2 days') RETURNING id",
            eid, name)

    async def row(pid):
        rows = (await P.list_projects(include_archived=True, limit=1000))["projects"]
        return next(r for r in rows if r["id"] == pid)

    async def unseen(pid):
        return (await row(pid)).get("unseen_landed_at", "missing")

    def tags(pid, token=None, **extra):
        t = {"source_context": {"kind": "project", "ref": str(pid)}, "app_id": "workbench", **extra}
        if token:
            t["turn_token"] = token
        return t

    async def reply(pid, token, text="Done.", *, began=None):
        """A Host reply through the real writer (session.py's assistant row)."""
        return await ME.write_turn_row(pool, E, f"s-{pid}", "assistant", text, project_id=pid,
                                       tags=tags(pid, token, **HOST), created_at=began)

    async def report(pid, text="The build finished: 4 of 4 checks passed."):
        """An agent's delivered report (session.py deliver_agent_result's
        _agent_tags): the Host's row, no turn token."""
        return await ME.write_turn_row(pool, E, f"agent-delivery:{pid}", "assistant", text, project_id=pid,
                                       tags={"source_context": {"kind": "project", "ref": str(pid)},
                                             "app_id": "workbench", "origin": "agent", "task_id": 77,
                                             "agent_title": "build", "agent_status": "completed"})

    async def landed_of(rid):
        return await pool.fetchval("SELECT COALESCE(landed_at, created_at) FROM entity_memory WHERE id = $1", rid)

    async def mark(pid, token=None):
        body = P.SeenMark(turn_token=token) if token is not None else None
        return (await P.mark_project_seen(pid, body))["seen_at"]

    async def finished_task(pid, *, ago=timedelta(0)):
        return await pool.fetchval(
            "INSERT INTO entity_tasks (entity_id, title, task_type, status, result, created_at, started_at, "
            "completed_at) VALUES ($1, 'build', 'agent', 'completed', $2::jsonb, now() - $3::interval, "
            "now() - $3::interval, now() - $3::interval) RETURNING completed_at",
            E, json.dumps({"project_id": pid}), ago)

    try:
        # ── A. what the person watched never lights its tab ─────────────────
        async def watched():
            # The turn ends in front of the person: the page marks it with the
            # turn's token at once. Its row is written meanwhile by the real
            # writer, through an embedder that takes SLOW_EMBED_S.
            a = await project("Watched, slow save")
            await mark(a)
            await reply(a, uuid.uuid4().hex, "An earlier reply, read long ago.",
                        began=await pool.fetchval("SELECT now() - interval '1 hour'"))
            await mark(a)
            tok = uuid.uuid4().hex
            began = await pool.fetchval("SELECT now()")
            ME._get_memory_embedding = slow_embed
            try:
                write = asyncio.create_task(reply(a, tok, "Here is the plan.", began=began))
                await asyncio.sleep(0.2)
                seen = await mark(a, tok)
                during = [await unseen(a)]
                await asyncio.sleep(SLOW_EMBED_S / 2)
                during.append(await unseen(a))
                rid = await write
            finally:
                ME._get_memory_embedding = no_embed
            landed = await landed_of(rid)
            check("A the watched reply's row landed after the mark (a slow save)",
                  rid is not None and landed is not None and landed > seen, (rid, landed, seen))
            check("A ...and its tab never lights: nothing unseen while it was saved, nor after",
                  during == [None, None] and await unseen(a) is None, (during, await unseen(a)))
            check("A the list's last_landed_at still moves with it (the page re-reads on it)",
                  (await row(a))["last_landed_at"] == landed, (await row(a))["last_landed_at"])
            await mark(a)
            check("A a later plain mark keeps it out (opening the page again)", await unseen(a) is None)

            # Written long after: a row the PII drain worker lays down after
            # the person has opened, left and opened the page again.
            late = await project("Watched, written much later")
            tok_l = uuid.uuid4().hex
            await mark(late)
            await mark(late, tok_l)
            await mark(late)
            await asyncio.sleep(0.05)
            await mark(late)
            rid_l = await reply(late, tok_l, "Saved at last.")
            check("A a watched reply written after several later marks still never lights",
                  rid_l is not None and await landed_of(rid_l) > (await row(late))["seen_at"]
                  and await unseen(late) is None, await unseen(late))

            # A held turn: its row is written before the turn holds (the reply
            # so far), then written over with the final reply after the stream
            # ends, which moves landed_at past the mark.
            h = await project("Watched, held")
            await mark(h)
            tok_h = uuid.uuid4().hex
            began_h = await pool.fetchval("SELECT now()")
            rid_h = await ME.write_turn_row_by_token(
                pool, E, f"s-{h}", "assistant", "Started the build; waiting on it.", token=tok_h, held=True,
                project_id=h, tags=tags(h, **HOST), created_at=began_h)
            await mark(h)                                   # the list showed the held row while in front
            seen_h = await mark(h, tok_h)                   # the turn ends in front of the person
            await asyncio.sleep(0.05)
            await ME.write_turn_row_by_token(
                pool, E, f"s-{h}", "assistant", "The build passed. Here is what changed.", token=tok_h,
                held=False, row_id=rid_h, project_id=h, tags=tags(h, **HOST), created_at=began_h)
            check("A a held turn written over after the mark: it landed after the mark, and stays dark",
                  await landed_of(rid_h) > seen_h and await unseen(h) is None, await unseen(h))

            # Two watched turns, the first one's row landing after the
            # second's mark (an embedder failing through both).
            two = await project("Two watched")
            await mark(two)
            t1, t2 = uuid.uuid4().hex, uuid.uuid4().hex
            await mark(two, t1)
            await mark(two, t2)
            await asyncio.sleep(0.05)
            await reply(two, t2, "Second reply.")
            await reply(two, t1, "First reply, saved last.")
            check("A two watched replies landing after both marks, out of order: dark", await unseen(two) is None)

            # The same turn marked twice (a mark that raced its own retry).
            again = await project("Marked twice")
            t = uuid.uuid4().hex
            await mark(again, t)
            await mark(again, t)
            stored = await pool.fetchval("SELECT seen_turns FROM projects WHERE id = $1", again)
            await asyncio.sleep(0.05)
            await reply(again, t)
            check("A the same turn marked twice is kept once, and its reply stays dark",
                  stored == [t] and await unseen(again) is None, (stored, await unseen(again)))

        # ── B. anything else that lands after the mark lights it ───────────
        async def everything_else():
            b = await project("Report while the reply is saved")
            await mark(b)
            tok = uuid.uuid4().hex
            ME._get_memory_embedding = slow_embed
            try:
                write = asyncio.create_task(reply(b, tok, "Starting the build."))
                await asyncio.sleep(0.2)
                seen = await mark(b, tok)
                # The report's writer embeds quickly; the watched reply's is
                # still waiting on its embedder.
                ME._get_memory_embedding = no_embed
                rep = await report(b)
                lit = await unseen(b)
                await write
            finally:
                ME._get_memory_embedding = no_embed
            rep_at = await landed_of(rep)
            check("B an agent's report landing while the watched reply is still being saved lights the tab",
                  rep_at > seen and lit == rep_at, (lit, rep_at, seen))
            check("B ...and still does once the watched reply has landed", await unseen(b) == rep_at,
                  (await unseen(b), rep_at))
            await mark(b)
            check("B opening the page again darkens it", await unseen(b) is None, await unseen(b))

            c = await project("Another turn")
            mine, other = uuid.uuid4().hex, uuid.uuid4().hex
            await mark(c, mine)
            await asyncio.sleep(0.05)
            await reply(c, mine, "The reply the person watched.")
            rid_o = await reply(c, other, "A reply to a message sent from the phone.")
            check("B a reply of another turn (sent elsewhere, never watched here) lights it",
                  await unseen(c) == await landed_of(rid_o), await unseen(c))

            d, e = await project("Watched here"), await project("Not watched here")
            tok_d = uuid.uuid4().hex
            await mark(d, tok_d)
            await mark(e)
            await asyncio.sleep(0.05)
            rid_e = await reply(e, tok_d, "Filed under the other page.")
            check("B a token watched on one page is no pass on another: a reply landing there with it lights it",
                  await unseen(e) == await landed_of(rid_e) and await unseen(d) is None,
                  (await unseen(e), await unseen(d)))

            t = await project("Task finished")
            await mark(t)
            await asyncio.sleep(0.05)
            done = await finished_task(t)
            check("B a task that finished after the mark lights it", await unseen(t) == done, (await unseen(t), done))

            n = await project("Nothing new")
            await reply(n, uuid.uuid4().hex, "Before the look.")
            await finished_task(n, ago=timedelta(hours=1))
            await asyncio.sleep(0.05)
            await mark(n)
            check("B nothing that landed before the mark lights it", await unseen(n) is None, await unseen(n))

            never = await project("Never opened")
            await reply(never, uuid.uuid4().hex)
            check("B a page with no mark has nothing unseen (the book holds it against what it first read)",
                  (await row(never))["seen_at"] is None and await unseen(never) is None, await row(never))

            u = await project("The person's own words")
            await mark(u)
            await asyncio.sleep(0.05)
            await ME.write_turn_row(pool, E, f"s-{u}", "user", "Make the button say Pay now.", project_id=u,
                                    tags=tags(u, uuid.uuid4().hex))
            check("B the person's own message is not something that landed", await unseen(u) is None)

        # ── S. the route ────────────────────────────────────────────────────
        async def route():
            s = await project("Marks")
            toks = [uuid.uuid4().hex for _ in range(34)]
            for t in toks:
                await mark(s, t)
            kept = await pool.fetchval("SELECT seen_turns FROM projects WHERE id = $1", s)
            check("S the last 32 tokens are kept, oldest first, newest last",
                  kept == toks[-32:], (len(kept or []), (kept or [])[:2], toks[-32:][:2]))
            await mark(s, toks[-20])
            kept2 = await pool.fetchval("SELECT seen_turns FROM projects WHERE id = $1", s)
            check("S the same token again moves to the end, never twice",
                  kept2 == [t for t in toks[-32:] if t != toks[-20]] + [toks[-20]], kept2)
            before = await pool.fetchrow("SELECT seen_at, seen_turns, updated_at FROM projects WHERE id = $1", s)
            r = await P.mark_project_seen(s)
            after = await pool.fetchrow("SELECT seen_at, seen_turns, updated_at FROM projects WHERE id = $1", s)
            check("S a mark with no body works as before: {project_id, seen_at}, forward, tokens kept, "
                  "updated_at untouched",
                  set(r) == {"project_id", "seen_at"} and r["seen_at"] == after["seen_at"] >= before["seen_at"]
                  and after["seen_turns"] == before["seen_turns"] and after["updated_at"] == before["updated_at"],
                  (r, dict(before), dict(after)))
            for bad in ("not a token!", "x" * 65, "a/b", "tok\n", "  "):
                try:
                    await P.mark_project_seen(s, P.SeenMark(turn_token=bad))
                    check(f"S a token that is not one ({bad[:12]!r}) is refused", False)
                except HTTPException as e:
                    now_row = await pool.fetchrow("SELECT seen_at, seen_turns FROM projects WHERE id = $1", s)
                    check(f"S a token that is not one ({bad[:12]!r}) is refused (400), nothing stored",
                          e.status_code == 400 and tuple(now_row) == (after["seen_at"], after["seen_turns"]),
                          (e.status_code, dict(now_row)))
            empty = await P.mark_project_seen(s, P.SeenMark(turn_token=""))
            check("S an empty token is a plain mark",
                  empty["seen_at"] >= after["seen_at"]
                  and await pool.fetchval("SELECT seen_turns FROM projects WHERE id = $1", s) == after["seen_turns"])
            other = await project("Someone else's", eid=OTHER_E)
            try:
                await P.mark_project_seen(other, P.SeenMark(turn_token=uuid.uuid4().hex))
                check("S another entity's project is refused", False)
            except HTTPException as e:
                theirs = await pool.fetchrow("SELECT seen_at, seen_turns FROM projects WHERE id = $1", other)
                check("S another entity's project is refused (404), its marks untouched",
                      e.status_code == 404 and tuple(theirs) == (None, None), (e.status_code, dict(theirs)))

        # ── P. the page route ───────────────────────────────────────────────
        async def page():
            pid = await project("Page")
            await mark(pid)
            await asyncio.sleep(0.05)
            rid = await report(pid)
            pg = await P.get_project_page(pid)
            r = await row(pid)
            check("P the page carries unseen_landed_at, the list row's",
                  "unseen_landed_at" in pg and pg["unseen_landed_at"] == r["unseen_landed_at"]
                  == await landed_of(rid), (pg.get("unseen_landed_at"), r.get("unseen_landed_at")))

        # ── Q. what the read costs ──────────────────────────────────────────
        async def cost():
            big = await project("Two thousand replies")
            await pool.execute(
                "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, source_session, "
                "project_id, metadata, created_at, landed_at) "
                "SELECT $1, 'reply ' || g, 'turn', 0.5, 'active', 's', $2, jsonb_build_object("
                "  'role', 'assistant', 'turn_token', md5(g::text), 'gen_ms', 900, 'filler', repeat('x', 3000), "
                "  'source_context', jsonb_build_object('kind', 'project', 'ref', $3::text)), "
                "  now() - make_interval(secs => 10000 - g), now() - make_interval(secs => 10000 - g) "
                "FROM generate_series(1, 2000) g", E, big, str(big))
            # Marked before the newest reply, with that reply's token: the
            # read has one landing after the mark to look at, and passes it.
            newest = await pool.fetchval("SELECT md5('2000')")
            await P.mark_project_seen_service(pool, E, big, turn_token=newest)
            await pool.execute(
                "UPDATE projects SET seen_at = (SELECT landed_at - interval '0.5 seconds' FROM entity_memory "
                "WHERE project_id = $1 AND metadata->>'turn_token' = $2) WHERE id = $1", big, newest)
            await pool.execute("ANALYZE entity_memory")
            q = (f"WITH {P._PAGE_MARKS_CTES} SELECT p.id, {P._PAGE_MARKS_COLS} FROM projects p "
                 f"{P._PAGE_MARKS_JOINS} WHERE p.entity_id = $1 AND p.id = $2")
            async with pool.acquire() as c:
                raw = await c.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + q, E, big)
            plan = (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]
            nodes = []

            def walk(n, parent):
                nodes.append((n.get("Node Type"), n.get("Relation Name"), n.get("Index Name"), parent,
                              n.get("Shared Hit Blocks", 0) + n.get("Shared Read Blocks", 0),
                              n.get("Index Cond") or "", n.get("Actual Rows")))
                for ch in n.get("Plans") or []:
                    walk(ch, n.get("Node Type"))
            walk(plan, None)
            seq = [x for x in nodes if x[0] == "Seq Scan" and x[1] == "entity_memory"]
            replies = [x for x in nodes if x[2] == "idx_entity_memory_project_replies"]
            from_mark = [x for x in replies if "seen_at" in x[5]]
            check("Q no sequential scan of entity_memory", not seq, seq)
            check("Q the unseen read starts at the mark in the replies index, under a Limit",
                  bool(from_mark) and all(x[3] == "Limit" for x in replies), replies)
            check("Q ...and on a page of 2,000 replies reads a handful of buffers",
                  bool(replies) and all(x[4] <= 12 for x in replies), [(x[4], x[5][:70]) for x in replies])
            check("Q (the one reply after the mark is the watched one, and it is passed over)",
                  await unseen(big) is None, await unseen(big))
            calls = []

            class Counting:
                """The pool, counting every call the list makes on it."""

                def __getattr__(self, name):
                    attr = getattr(pool, name)
                    if name in ("fetch", "fetchrow", "fetchval", "execute", "executemany", "acquire"):
                        def counted(*a, **k):
                            calls.append(name)
                            return attr(*a, **k)
                        return counted
                    return attr
            bs._pool = Counting()
            try:
                await P.list_projects(include_archived=True, limit=1000)
            finally:
                bs._pool = pool
            check("Q the list is still one query", calls == ["fetch"], calls)

        await section("A. a reply the person watched never lights its tab", watched)
        await section("B. anything else that lands after the mark lights it", everything_else)
        await section("S. PUT /seen", route)
        await section("P. the page route", page)
        await section("Q. what the read costs", cost)
    finally:
        ME._get_memory_embedding = real_embed
        bs._pool, bs._entity_id = saved
        await pool.close()


async def main(dsn):
    await section("M. the column", lambda: migration_checks(dsn))
    await behaviour(dsn)
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
