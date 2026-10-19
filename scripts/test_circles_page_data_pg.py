#!/usr/bin/env python3
"""Gate: Circles' page data, against a real kernel schema.

  M. the marks table on the full current schema: its columns, its key, the
     CHECKs that refuse a malformed mark and the thread list's four indexes
     (built, and valid), and a second run of migrations/v219 changes nothing.
  S. PUT /api/circles/thread/{kind}/{id}/seen: the mark only moves forward, a
     second call is harmless, and of two PUTs at once the one that began first
     and wrote last never moves it back; a watched turn's token is kept on your Host's
     page (the last 32, newest last) and refused on any other kind; the
     original conversation answers to 'original' and the client's old 'self';
     a deleted thread, another entity's thread and an id that is not one are
     refused; a person's mark belongs to the operator reading it.
  A. a reply watched to its end never lights its page, on a thread and on the
     original conversation, even when its row lands after later marks.
  B. what the Host wrote lights it: a reply of a turn not watched, a scheduled
     task's report (the runner's real writer) and an agent's delivered result,
     on a thread and on the original conversation; the person's own words, a
     split's marker, a fired wake-up's hidden reply, anything from before the
     mark, another thread's rows and a page never marked do not.
  C. GET /api/circles/threads: every page, the original conversation
     included, carries its turn count as before, last_activity_at,
     last_landed_at, unseen_landed_at, seen_at and waiting_on_you, which is
     the count GET /api/tickets?status=open&asked_in= gives for that page; the
     rows the list reads for each page are shown_where's.
  T. GET /api/circles/thread/self/{id}/page: thread_state_parts' text, the
     running totals (uncounted and unpriced turns kept apart, another
     thread's turns never counted), the list's marks; other kinds refused,
     an unknown thread 404, read:inference_log required.
  D. the totals and the transcript: on a thread they read the same rows until
     a row is superseded; on the original conversation the totals' scope also
     takes a fired wake-up's hidden row the transcript leaves out, and the
     transcript takes an untagged row the totals leave out; the page says so
     (totals_vs_transcript) instead of differing silently.
  U. GET /api/circles/conversations: `unread` (the field circles-web reads)
     and `unread_count` carry the other side's messages after the mark, for a
     peer Host, a person (per operator) and a room (through the bridge); this
     Host's own messages never count; a room the bridge does not answer for
     says unread_unknown, with no made-up unread_count; a room the bridge is
     slow for, or whose page cannot be counted, says the same and never holds
     or breaks the list.
  N. POST /api/circles/nudge takes the sender's id and passes it to
     session.push_nudge as client_id; an id that is not one is a 400.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; no pod, no network beyond
loopback.

  PYTHONPATH=brain python3 scripts/test_circles_page_data_pg.py
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import time
import types
import uuid
from datetime import datetime, timedelta

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the marks' SQL and v219.")
    sys.exit(2)

FAILED = 0
E = 1
OTHER_E = 2
VERSION = 219
MIGRATION = next(iter(sorted((ROOT / "migrations").glob(f"v{VERSION}_*.sql"))), None)
INDEXES = ("idx_entity_memory_original_replies", "idx_entity_memory_original_rows",
           "idx_entity_memory_thread_replies", "idx_entity_memory_thread_rows")
HOST = {"gen_ms": 1500, "model": "anthropic/claude-opus-5"}
ME_EMAIL = "robin@example.com"
OTHER_OPERATOR = "sam@example.com"

# circles.py imports server.main at module scope, and server.main mounts a
# container path at import time. Stub it: no bridge, no broadcast.
_stub_main = types.ModuleType("server.main")
_stub_main.app = None
_stub_main.get_bridge_client = lambda: None


async def _no_broadcast(*_a, **_k):
    return None
_stub_main.broadcast = _no_broadcast
sys.modules.setdefault("server.main", _stub_main)


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


def request(body=None, headers=None, method="PUT"):
    from starlette.requests import Request
    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": "/", "headers": hdrs,
                    "query_string": b""}, receive)


def me(email=ME_EMAIL):
    return {"X-Authentik-Email": email}


# ── M. the marks table ──────────────────────────────────────────────────────

async def schema_of(pool):
    cols = await pool.fetch(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'circles_page_marks' ORDER BY column_name")
    cons = await pool.fetch(
        "SELECT conname, pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conrelid = to_regclass('circles_page_marks') ORDER BY conname")
    idx = await pool.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY($1::text[]) ORDER BY indexname",
        list(INDEXES))
    return ([tuple(r) for r in cols], [tuple(r) for r in cons], [tuple(r) for r in idx])


async def migration_checks(dsn):
    check(f"M the migration file exists (v{VERSION})", MIGRATION is not None and MIGRATION.is_file(), MIGRATION)
    if MIGRATION is None:
        return
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        after = await schema_of(pool)
        cols = {c[0] for c in after[0]}
        check("M the table has entity_id, kind, ref, reader, seen_at, seen_turns",
              cols == {"entity_id", "kind", "ref", "reader", "seen_at", "seen_turns"}, cols)
        check("M ...its key is (entity_id, kind, ref, reader)",
              any(c[0] == "circles_page_marks_pkey" and "(entity_id, kind, ref, reader)" in c[1]
                  for c in after[1]), after[1])
        valid = await pool.fetchval(
            "SELECT count(*) FILTER (WHERE i.indisvalid) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY($1::text[])", list(INDEXES))
        check("M the list's four indexes are built, and valid",
              [i[0] for i in after[2]] == list(INDEXES) and valid == len(INDEXES), (after[2], valid))
        r2 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
        check("M a second run changes nothing and fails nothing",
              r2.returncode == 0 and await schema_of(pool) == after, r2.stderr[-300:])
        bad = (
            ("a kind that is not one", ("room", "7", "", None)),
            ("a person's mark with no reader", ("dm", "a@b.c", "", None)),
            ("a peer's mark with a reader", ("entity", "7", "x@y.z", None)),
            ("watched turns on a peer's mark", ("entity", "7", "", ["t1"])),
            ("33 watched turns", ("self", "original", "", [f"t{i}" for i in range(33)])),
            ("a NULL watched turn", ("self", "original", "", ["a", None])),
            ("an empty ref", ("self", "", "", None)),
        )
        for label, (kind, ref, reader, turns) in bad:
            try:
                await pool.execute(
                    "INSERT INTO circles_page_marks (entity_id, kind, ref, reader, seen_at, seen_turns) "
                    "VALUES (1, $1, $2, $3, now(), $4)", kind, ref, reader, turns)
                check(f"M the CHECKs refuse {label}", False)
            except asyncpg.CheckViolationError:
                check(f"M the CHECKs refuse {label}", True)
    finally:
        await pool.close()


# ── the behaviour ───────────────────────────────────────────────────────────

async def behaviour(dsn):
    from fastapi import HTTPException

    from mastercode import circles_threads as ct
    from mastercode import memory_engine as ME
    from mastercode.brain import offline_task_runner as otr
    from mastercode.runtime import session as S
    from server import brain_service as bs
    import server.routes.circles as C
    import server.routes.tickets as TK

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    saved = (bs._pool, bs._entity_id)
    bs._pool, bs._entity_id = pool, E
    real_embed = ME._get_memory_embedding

    async def no_embed(*_a, **_k):
        return None
    ME._get_memory_embedding = no_embed
    await pool.execute("INSERT INTO entity_registry (id, display_name) VALUES ($1, 'other host') "
                       "ON CONFLICT (id) DO NOTHING", OTHER_E)

    async def now():
        return await pool.fetchval("SELECT now()")

    async def thread(title, eid=E):
        return str(await pool.fetchval(
            "INSERT INTO circles_threads (entity_id, title, created_at) "
            "VALUES ($1, $2, now() - interval '2 days') RETURNING id", eid, title))

    def tags(tid, token=None, **extra):
        t = {"source_context": ct.descriptor_for(tid), "app_id": "circles", **extra}
        if token:
            t["turn_token"] = token
        return t

    async def say(tid, role, text, token=None, *, began=None, **extra):
        more = {**(HOST if role == "assistant" else {}), **extra}
        return await ME.write_turn_row(pool, E, f"s-{tid}", role, text, tags=tags(tid, token, **more),
                                       created_at=began or await now())

    async def report(tid, text="The nightly backup finished."):
        """A scheduled task's report, through the runner's own writer."""
        origin = {"thread_id": tid} if tid else None
        ok = await otr.post_to_origin_thread(pool, E, origin, text, extra_tags={"scheduled_task_id": 41})
        return await pool.fetchval(
            "SELECT id FROM entity_memory WHERE metadata->>'scheduled_task_id' = '41' "
            "ORDER BY id DESC LIMIT 1") if ok else None

    async def agent_result(tid, text="The research agent finished: 3 sources."):
        """An agent's delivered result (session.deliver_agent_result's
        _agent_tags): the Host's row, no turn token."""
        return await ME.write_turn_row(pool, E, f"agent-delivery:{tid}", "assistant", text,
                                       tags={"source_context": ct.descriptor_for(tid), "app_id": "circles",
                                             "origin": "agent", "task_id": 77, "agent_title": "research",
                                             "agent_status": "completed"},
                                       created_at=await now())

    async def landed_of(rid):
        return await pool.fetchval("SELECT COALESCE(landed_at, created_at) FROM entity_memory WHERE id = $1", rid)

    async def mark(kind, ref, token=None, email=ME_EMAIL):
        body = {"turn_token": token} if token is not None else None
        return await C.mark_circles_seen(kind, ref, request(body, me(email) if email else {}))

    async def entry(tid):
        rows = (await C.list_circles_threads())["threads"]
        want = ct.LEGACY_THREAD_ID if tid is None else tid
        return next((r for r in rows if r["id"] == want), None)

    async def unseen(tid):
        e = await entry(tid)
        return e.get("unseen_landed_at", "missing") if e else "no entry"

    def iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v

    async def tick():
        await asyncio.sleep(0.02)

    async def clear_original():
        await pool.execute("DELETE FROM entity_memory WHERE project_id IS NULL "
                           "AND COALESCE(metadata->'source_context'->>'kind', '') <> 'thread'")
        await pool.execute("DELETE FROM circles_page_marks WHERE kind = 'self' AND ref = 'original'")

    try:
        # ── S. the mark route ────────────────────────────────────────────────
        async def marks():
            t = await thread("Marks")
            first = await mark("self", t)
            check("S a mark returns {kind, id, seen_at} for the thread",
                  set(first) == {"kind", "id", "seen_at"} and first["id"] == t and first["kind"] == "self", first)
            await tick()
            second = await mark("self", t)
            check("S a second mark moves it forward and is harmless",
                  second["seen_at"] > first["seen_at"]
                  and await pool.fetchval("SELECT count(*) FROM circles_page_marks WHERE ref = $1", t) == 1,
                  (first, second))
            future = await pool.fetchval("SELECT now() + interval '1 hour'")
            await pool.execute("UPDATE circles_page_marks SET seen_at = $2 WHERE ref = $1", t, future)
            third = await mark("self", t)
            check("S the mark never moves back (a later one from another device stays)",
                  third["seen_at"] == future, (third, future))
            # Two PUTs at once, on a page never marked: the one whose
            # transaction began first writes last. now() is each transaction's
            # start, so only GREATEST keeps the later mark; a plain
            # SET seen_at = now() would move it back.
            from server import circles_marks as CM
            both = await thread("Two at once")
            async with pool.acquire() as conn:
                tr = conn.transaction()
                await tr.start()
                try:
                    early = await conn.fetchval("SELECT now()")
                    await tick()
                    late = await mark("self", both)
                    slow = await CM.mark_seen(conn, E, "self", both)
                    await tr.commit()
                except BaseException:
                    await tr.rollback()
                    raise
            stored = await pool.fetchval("SELECT seen_at FROM circles_page_marks WHERE ref = $1", both)
            check("S two PUTs at once: the one that began first and wrote last never moves the mark back",
                  early < late["seen_at"] and stored == late["seen_at"] and slow["seen_at"] == late["seen_at"],
                  (early, late, slow, stored))
            await pool.execute("UPDATE circles_page_marks SET seen_at = now() - interval '1 hour' WHERE ref = $1", t)
            toks = [uuid.uuid4().hex for _ in range(34)]
            for x in toks:
                await mark("self", t, x)
            kept = await pool.fetchval("SELECT seen_turns FROM circles_page_marks WHERE ref = $1", t)
            check("S the last 32 watched turns are kept, newest last", kept == toks[-32:], (len(kept or []),))
            await mark("self", t, toks[-20])
            kept2 = await pool.fetchval("SELECT seen_turns FROM circles_page_marks WHERE ref = $1", t)
            check("S the same token again moves to the end, never twice",
                  kept2 == [x for x in toks[-32:] if x != toks[-20]] + [toks[-20]], kept2)
            await mark("self", t)
            check("S a mark with no body keeps the tokens",
                  await pool.fetchval("SELECT seen_turns FROM circles_page_marks WHERE ref = $1", t) == kept2)
            o1 = await mark("self", "original")
            o2 = await mark("self", "self")
            check("S the original conversation answers to 'original' and to the client's old 'self'",
                  o1["id"] == o2["id"] == "original", (o1, o2))
            upper = await mark("self", t.upper())
            check("S a thread id in another spelling marks the same page", upper["id"] == t, upper)

            async def refused(label, status, *a, **k):
                try:
                    await mark(*a, **k)
                    check(f"S {label} is refused ({status})", False)
                except HTTPException as e:
                    check(f"S {label} is refused ({status})", e.status_code == status, (e.status_code, e.detail))
            gone = await thread("Deleted")
            await pool.execute("UPDATE circles_threads SET deleted_at = now() WHERE id = $1::uuid", gone)
            await refused("a deleted thread", 404, "self", gone)
            await refused("another entity's thread", 404, "self", await thread("Theirs", eid=OTHER_E))
            await refused("a thread id that is not one", 404, "self", "not-a-uuid")
            await refused("a kind that is not one", 400, "room", "7")
            await refused("a watched turn on a peer's conversation", 400, "entity", "7", uuid.uuid4().hex)
            await refused("a turn token that is not one", 400, "self", t, "not a token!")
            await refused("a peer id that is not a number", 400, "entity", "abc")
            await refused("a person with no verified operator", 403, "dm", "ana@example.com", email="")
            p = await mark("dm", "Ana@Example.com")
            q = await mark("dm", "ana@example.com", email=OTHER_OPERATOR)
            rows = await pool.fetch("SELECT reader FROM circles_page_marks WHERE kind = 'dm' "
                                    "AND ref = 'ana@example.com' ORDER BY reader")
            check("S a person's mark is the reading operator's own, keyed by the lowercased address",
                  p["id"] == q["id"] == "ana@example.com"
                  and [r["reader"] for r in rows] == [ME_EMAIL, OTHER_OPERATOR], [dict(r) for r in rows])
            await pool.execute("DELETE FROM circles_page_marks WHERE kind = 'dm'")

        # ── A. a reply watched to its end never lights ──────────────────────
        async def watched():
            for label, tid in (("a thread", await thread("Watched")),):
                ref = tid
                await say(tid, "user", "Plan the dinner.")
                await mark("self", ref)
                tok = uuid.uuid4().hex
                began = await now()
                await mark("self", ref, tok)              # the turn ends in front of the person
                await tick()
                await mark("self", ref)                   # and the page is opened again
                await tick()
                rid = await say(tid, "assistant", "Corvo at 7.", tok, began=began)
                e = await entry(tid)
                check(f"A {label}: the watched reply's row landed after the marks",
                      await landed_of(rid) > datetime.fromisoformat(e["seen_at"]), (await landed_of(rid), e))
                check(f"A {label}: ...and its page never lights", e["unseen_landed_at"] is None, e)
                check(f"A {label}: last_landed_at still moves with it",
                      e["last_landed_at"] == iso(await landed_of(rid)), e)
            # There is no original conversation: no page.
            check("A the old original conversation has no page entry", await entry(None) is None)

        # ── B. what the Host wrote lights it; the person's words do not ─────
        async def lights():
            for label, tid in (("a thread", await thread("Lit")),):
                ref = tid
                await mark("self", ref)
                await tick()
                rid = await say(tid, "assistant", "A reply to a message sent from the phone.", uuid.uuid4().hex)
                check(f"B {label}: a reply of a turn not watched here lights it",
                      await unseen(tid) == iso(await landed_of(rid)), await unseen(tid))
                await mark("self", ref)
                await tick()
                rid = await report(tid)
                check(f"B {label}: a scheduled task's report (the runner's writer) lights it",
                      rid is not None and await unseen(tid) == iso(await landed_of(rid)), (rid, await unseen(tid)))
                await mark("self", ref)
                await tick()
                rid = await agent_result(tid)
                check(f"B {label}: an agent's delivered result lights it",
                      await unseen(tid) == iso(await landed_of(rid)), await unseen(tid))
                await mark("self", ref)
                check(f"B {label}: opening the page again darkens it", await unseen(tid) is None)
                await tick()
                await say(tid, "user", "Make it 7:30.", uuid.uuid4().hex)
                await say(tid, "user", "and a table outside", nudge=True)
                check(f"B {label}: the person's own words never light it", await unseen(tid) is None,
                      await unseen(tid))
                await pool.execute(
                    "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, "
                    "source_session, metadata) VALUES ($1, '→ Moved 2 messages', 'turn', 0.1, 'active', "
                    "'split:x', $2::jsonb)", E,
                    json.dumps({"role": "assistant", "source_kind": "thread_marker", "app_id": "circles",
                                "source_context": ct.descriptor_for(tid)}))
                check(f"B {label}: a split's marker never lights it", await unseen(tid) is None, await unseen(tid))
                await ME.write_turn_row(pool, E, "wake", "assistant", "The wake-up's own thinking.",
                                        tags={"source_context": ct.descriptor_for(tid),
                                              "app_id": "scheduled_task", **HOST}, created_at=await now())
                check(f"B {label}: a fired wake-up's hidden reply never lights it (its report does)",
                      await unseen(tid) is None, await unseen(tid))
            # A scheduled task's report that names no thread (the platform's
            # own standing tasks, one set in the old original conversation)
            # starts a topic of its own: there is no original conversation.
            rid = await report(None, "A report that named no thread.")
            sc = await pool.fetchval("SELECT metadata->'source_context' FROM entity_memory WHERE id = $1", rid)
            sc = json.loads(sc) if isinstance(sc, str) else (sc or {})
            check("B a report that names no thread lands in a topic of its own, listed as a page",
                  rid is not None and sc.get("kind") == "thread" and await entry(sc.get("ref")) is not None, sc)
            other, mine = await thread("Other"), await thread("Mine")
            await mark("self", mine)
            await tick()
            await say(other, "assistant", "Elsewhere.", uuid.uuid4().hex)
            check("B another thread's reply never lights this one", await unseen(mine) is None, await unseen(mine))
            never = await thread("Never opened")
            await say(never, "assistant", "Hello.", uuid.uuid4().hex)
            e = await entry(never)
            check("B a page never marked has nothing unseen (the book holds it against what it first read)",
                  e["seen_at"] is None and e["unseen_landed_at"] is None and e["last_landed_at"] is not None, e)
            before = await thread("Before the look")
            await say(before, "assistant", "Earlier.", uuid.uuid4().hex)
            await tick()
            await mark("self", before)
            check("B nothing that landed before the mark lights it", await unseen(before) is None)

        # ── C. the list ─────────────────────────────────────────────────────
        async def the_list():
            rows = (await C.list_circles_threads())["threads"]
            fields = {"seen_at", "last_activity_at", "last_landed_at", "unseen_landed_at",
                      "waiting_on_you", "ask_cards_open", "turn_count"}
            check("C every page carries the marks, and none is the old original conversation",
                  rows and all(fields <= set(r) for r in rows) and all(r["id"] != "original" for r in rows),
                  [sorted(fields - set(r)) for r in rows[:2]])
            for r in rows:
                want = await ct._turn_count(pool, E, r["id"])
                if r["turn_count"] != want:
                    check(f"C {r['title']}: the turn count is the transcript's", False, (r["turn_count"], want))
                    break
            else:
                check("C every page's turn count is still the transcript's (shown_where)", True)
            bad = []
            for r in rows:
                args = [E]
                shown = {x["id"] for x in await pool.fetch(
                    f"SELECT id FROM entity_memory WHERE {' AND '.join(ct.shown_where(args, r['id']))}", *args)}
                lifted = {x["id"] for x in await pool.fetch(
                    f"SELECT em.id FROM entity_memory em WHERE {ct._THREAD_ROWS} "
                    "AND (em.metadata->'source_context'->>'ref') = $2", E, r["id"])}
                if shown != lifted:
                    bad.append((r["title"], len(shown), len(lifted)))
            check("C the rows the list reads for a thread are shown_where's, for every thread", not bad, bad)
            t = await thread("Activity")
            e0 = await entry(t)
            check("C a thread with no rows reads its own creation as last activity, and no landing",
                  e0["last_activity_at"] == e0["created_at"] and e0["last_landed_at"] is None, e0)
            await say(t, "user", "Hi.")
            rid = await say(t, "assistant", "Hello.", uuid.uuid4().hex)
            uid = await say(t, "user", "One more thing.")
            e1 = await entry(t)
            check("C last_activity_at is the newest row, the person's included; last_landed_at the Host's",
                  e1["last_activity_at"] == iso(await landed_of(uid))
                  and e1["last_landed_at"] == iso(await landed_of(rid)), e1)

            async def ticket(asked_in, status="open"):
                await pool.execute(
                    "INSERT INTO entity_tickets (entity_id, source, sitrep, status, metadata) "
                    "VALUES ($1, 'manual', 'Pick one', $2, $3::jsonb)",
                    E, status, json.dumps({"asked_in": asked_in} if asked_in is not None else {}))
            await ticket({"app_id": "circles", "thread_id": t.upper(), "project_id": None})
            await ticket({"app_id": "circles", "thread_id": t, "project_id": None})
            await ticket({"app_id": "circles", "thread_id": t}, status="decided")
            await ticket({"app_id": "circles", "thread_id": None, "project_id": None})
            await ticket({"app_id": "workbench", "thread_id": None, "project_id": 5})
            await ticket(None)
            rows = (await C.list_circles_threads())["threads"]
            mismatch = []
            for r in rows:
                room = f"thread:{r['id']}"
                want = len((await TK.list_tickets_service(pool, E, status_filter="open", asked_in=room,
                                                          limit=200))["tickets"])
                if r["waiting_on_you"] != want or r["ask_cards_open"] != want:
                    mismatch.append((r["title"], r["waiting_on_you"], want))
            here = next(r for r in rows if r["id"] == t)
            check("C waiting_on_you is GET /api/tickets?status=open&asked_in=<page>'s count, for every page",
                  not mismatch and here["waiting_on_you"] == 2, (mismatch, here["waiting_on_you"]))

        # ── T. the page route ───────────────────────────────────────────────
        async def page_route():
            t = await thread("Gift for Dad")
            other = await thread("Elsewhere")

            async def priced(tid, *, spend=None, est=None, basis=None):
                md = {"role": "assistant", "app_id": "circles", "source_context": ct.descriptor_for(tid), **HOST}
                if spend is not None:
                    md["context_ledger"] = {"spend": spend}
                if est is not None:
                    md["est_cost_usd"] = est
                if basis is not None:
                    md["price_basis"] = basis
                return await pool.fetchval(
                    "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, "
                    "source_session, metadata) VALUES ($1, 'r', 'turn', 0.5, 'active', 's', $2::jsonb) "
                    "RETURNING id", E, json.dumps(md))
            sp = {"calls": 2, "tokens": {"uncached": 1000, "cache_read": 500, "cache_write": 0, "output": 200}}
            await priced(t, spend=sp, est=0.01)
            await priced(t, spend=sp, est=0.02)
            await priced(t, est=0.03)                      # a reply from before the meter: tokens unknown
            await priced(t, spend=sp, basis="unpriced")    # a price nobody knows
            await priced(t, spend=sp, basis="subscription")
            await priced(other, spend={"calls": 9, "tokens": {"uncached": 99999}}, est=9.0)
            await pool.execute(
                "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, metadata) "
                "VALUES ($1, 'Pick a gift Dad will use', 'goal', 0.8, 'active', $2::jsonb)",
                E, json.dumps({"source_context": ct.descriptor_for(t)}))
            await mark("self", t)
            pg = await C.get_circles_page("self", t)
            tot = pg.get("totals") or {}
            check("T totals: four turns counted, one unknown, tokens summed from their spend only",
                  tot.get("turns_counted") == 4 and tot.get("turns_unknown") == 1
                  and tot.get("tokens_total") == 4 * 1700, tot)
            check("T totals: dollars are the printed prices summed (0.06), one unpriced, partial",
                  abs((tot.get("est_usd") or 0) - 0.06) < 1e-9 and tot.get("turns_unpriced") == 1
                  and tot.get("usd_partial") is True, tot)
            check("T another thread's turns never count", (tot.get("tokens") or {}).get("uncached") == 4000, tot)
            ws = pg.get("where_this_stands") or {}
            check("T where_this_stands is thread_state_parts' on the page's scope",
                  ws.get("goal") == "Pick a gift Dad will use" and "Pick a gift Dad will use" in (ws.get("text") or ""),
                  ws)
            e = await entry(t)
            check("T the page carries the list's marks for it",
                  all(pg.get(k) == e.get(k) for k in ("seen_at", "last_activity_at", "last_landed_at",
                                                      "unseen_landed_at", "waiting_on_you", "turn_count")),
                  (pg, e))
            try:
                await C.get_circles_page("self", "original")
                check("T the old original conversation has no page (404)", False)
            except HTTPException as ex:
                check("T the old original conversation has no page (404)", ex.status_code == 404, ex.status_code)
            for kind in ("entity", "dm", "circle"):
                try:
                    await C.get_circles_page(kind, "7")
                    check(f"T a {kind} conversation's page is refused", False)
                except HTTPException as ex:
                    check(f"T a {kind} conversation's page is refused (400, runs on the other host)",
                          ex.status_code == 400 and "other host" in str(ex.detail), ex.detail)
            try:
                await C.get_circles_page("self", str(uuid.uuid4()))
                check("T an unknown thread's page is a 404", False)
            except HTTPException as ex:
                check("T an unknown thread's page is a 404", ex.status_code == 404, ex.status_code)
            route = next(r for r in C.router.routes
                         if getattr(r, "path", "") == "/api/circles/thread/{kind}/{thread_id}/page")
            scopes = set()
            for d in route.dependant.dependencies:
                for cell in (getattr(d.call, "__closure__", None) or ()):
                    if isinstance(cell.cell_contents, str):
                        scopes.add(cell.cell_contents)
            check("T the page needs read:circles and read:inference_log",
                  {"read:circles", "read:inference_log"} <= scopes, scopes)

        # ── D. the totals and the transcript ────────────────────────────────
        async def two_ways():
            from server import circles_marks as CM
            t = await thread("Read two ways")
            await say(t, "user", "Hi.")
            await say(t, "assistant", "Hello.", uuid.uuid4().hex)
            same = await CM.totals_scope_rows(pool, E, t)
            check("D a thread: the totals read the transcript's rows",
                  same["same"] is True and same["shown"] == same["counted"] == 2, same)
            await pool.execute(
                "UPDATE entity_memory SET t_valid_until = now() - interval '1 minute' WHERE id = ("
                "SELECT id FROM entity_memory WHERE metadata->'source_context'->>'ref' = $1 ORDER BY id LIMIT 1)", t)
            sup = await CM.totals_scope_rows(pool, E, t)
            check("D a thread with a superseded row: shown, not in the totals, and the page says so",
                  sup["same"] is False and sup["shown_not_counted"] == 1 and sup["counted_not_shown"] == 0, sup)
            pg = await C.get_circles_page("self", t)
            check("D ...the page carries it as totals_vs_transcript", pg.get("totals_vs_transcript") == sup,
                  pg.get("totals_vs_transcript"))
            # A fired wake-up THINKS in its thread on the hidden lane: its reply
            # is in the totals' scope and not the transcript; its report is in both.
            b4 = await CM.totals_scope_rows(pool, E, t)
            await ME.write_turn_row(pool, E, "wake", "assistant", "The wake-up's own thinking.",
                                    tags={"source_context": ct.descriptor_for(t), "app_id": "scheduled_task",
                                          "context_ledger": {"spend": {"calls": 1, "tokens": {"output": 40}}},
                                          **HOST}, created_at=await now())
            await report(t, "Tonight: water the plants.")
            af = await CM.totals_scope_rows(pool, E, t)
            check("D a thread: a fired wake-up's hidden reply is in the totals' scope, not the transcript, "
                  "and its report is in both",
                  af["counted_not_shown"] == b4["counted_not_shown"] + 1
                  and af["shown"] == b4["shown"] + 1 and af["counted"] == b4["counted"] + 2, (b4, af))
            tot = (await C.get_circles_page("self", t)).get("totals") or {}
            check("D ...whose tokens the thread's total holds (running_spend as it is)",
                  (tot.get("tokens") or {}).get("output") == 40, tot.get("tokens"))

        # ── U. unread in the conversations list ─────────────────────────────
        async def unread():
            peer, quiet = 120, 121

            async def inbox(frm, to, body, *, from_user=None, to_user=None, ago=timedelta(0)):
                # A person's message from another Host is stored as the relay
                # stores it: no contact here names that person's Host, so the
                # relay's check is what makes it theirs (circles_reading.
                # person_checked_sql).
                relayed = bool(from_user and to_user and frm != E)
                return await pool.fetchval(
                    "INSERT INTO inbox_messages (from_address, to_address, from_entity_id, to_entity_id, "
                    "from_user_email, to_user_email, subject, body, message_kind, payload, sent_at, delivered_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'Message', $7, 'message', $9::jsonb, now() - $8::interval, "
                    "        now() - $8::interval) RETURNING message_id",
                    f"{frm}@entity.localhost", f"{to}@entity.localhost", frm, to, from_user, to_user, body, ago,
                    json.dumps({"person_checked_by": "relay"} if relayed else {}))

            await inbox(peer, E, "old one", ago=timedelta(hours=2))
            await inbox(E, peer, "our reply", ago=timedelta(hours=1))
            await inbox(peer, E, "two", ago=timedelta(minutes=30))
            await inbox(E, quiet, "hello?")
            await inbox(peer, E, "a person's note to a person", from_user="joel@example.com", to_user=ME_EMAIL)

            async def listed(email=ME_EMAIL):
                return await C.list_conversations(request(None, me(email), method="GET"))

            def item(payload, section_name, key):
                return next((i for i in payload[section_name] if str(i["id"]) == str(key)), None)
            pl = await listed()
            p = item(pl, "entities", peer)
            check("U a peer Host never opened: every message it sent is unread, ours are not (2)",
                  p and p["unread"] == 2 and p["unread_count"] == 2 and p["seen_at"] is None, p)
            q = item(pl, "entities", quiet)
            check("U a peer Host that sent nothing has 0", q and q["unread"] == 0 and q["unread_count"] == 0, q)
            await mark("entity", str(peer))
            await tick()
            await inbox(peer, E, "three")
            p = item(await listed(), "entities", peer)
            check("U after the mark only what the peer sent since counts (1)",
                  p["unread"] == p["unread_count"] == 1 and p["seen_at"] is not None, p)
            await inbox(E, peer, "ours again")
            p = item(await listed(), "entities", peer)
            check("U this Host's own messages never count", p["unread"] == 1, p)
            await mark("entity", str(peer))
            p = item(await listed(), "entities", peer)
            check("U opening it again clears it", p["unread"] == 0, p)

            ana = "ana@example.com"
            await inbox(130, E, "dinner?", from_user=ana, to_user=ME_EMAIL, ago=timedelta(minutes=10))
            await inbox(E, 130, "sure", from_user=ME_EMAIL, to_user=ana, ago=timedelta(minutes=9))
            await inbox(130, E, "7:30?", from_user=ana, to_user=ME_EMAIL, ago=timedelta(minutes=8))
            await inbox(130, E, "for Sam", from_user=ana, to_user=OTHER_OPERATOR)
            a = item(await listed(), "people", ana)
            check("U a person never opened: every message they sent this operator is unread (2)",
                  a and a["unread"] == a["unread_count"] == 2, a)
            await mark("dm", ana, email=OTHER_OPERATOR)
            a = item(await listed(), "people", ana)
            check("U another operator's mark does not touch this operator's count", a["unread"] == 2, a)
            s = item(await listed(OTHER_OPERATOR), "people", ana)
            check("U ...and that operator reads their own (0 since their mark)", s and s["unread"] == 0, s)
            await mark("dm", ana)
            await tick()
            await inbox(130, E, "or 8?", from_user=ana, to_user=ME_EMAIL)
            a = item(await listed(), "people", ana)
            check("U after this operator's mark only the new message counts", a["unread"] == 1, a)
            h = item(await listed(), "entities", 130)
            check("U a person's message to a person never counts under the peer Host",
                  h is None or h["unread"] == 0, h)

            # Rooms: their messages are on the platform, read through the bridge.
            seen_at = await now()
            room_msgs = {
                5: [{"id": 3, "sender_entity_id": 140, "content": "new",
                     "created_at": (seen_at + timedelta(seconds=5)).isoformat()},
                    {"id": 2, "sender_entity_id": E, "content": "ours",
                     "created_at": (seen_at + timedelta(seconds=4)).isoformat()},
                    {"id": 1, "sender_entity_id": 140, "content": "old",
                     "created_at": (seen_at - timedelta(minutes=5)).isoformat()}],
                6: [{"id": 9, "sender_entity_id": 141, "content": "x",
                     "created_at": (seen_at - timedelta(minutes=1)).isoformat()}],
            }
            calls = []

            async def fake_bridge(op, params):
                calls.append((op, dict(params)))
                if op == "circles.list":
                    return {"circles": [{"id": 5, "circle_name": "Book club"}, {"id": 6, "circle_name": "Ops"},
                                        {"id": 7, "circle_name": "Down"}]}
                if op == "circles.messages":
                    cid = params["circle_id"]
                    if cid == 7:
                        return None
                    return {"messages": room_msgs[cid], "total": len(room_msgs[cid]), "next_before": None}
                return None
            await pool.execute(
                "INSERT INTO circles_page_marks (entity_id, kind, ref, reader, seen_at) "
                "VALUES ($1, 'circle', '5', '', $2)", E, seen_at)
            real_bridge = C._bridge_call
            C._bridge_call = fake_bridge
            try:
                pl = await listed()
            finally:
                C._bridge_call = real_bridge
            r5, r6, r7 = (item(pl, "circles", i) for i in (5, 6, 7))
            check("U a room: other Hosts' messages after its mark (1), never this Host's own",
                  r5 and r5["unread"] == r5["unread_count"] == 1, r5)
            check("U a room never opened: every message from another Host on its newest page (1)",
                  r6 and r6["unread"] == 1 and r6["seen_at"] is None, r6)
            check("U a room the bridge did not answer for says unread_unknown, never a count",
                  r7 and r7.get("unread_unknown") is True and "unread" not in r7, r7)
            check("U each room is one circles.messages call for its newest page",
                  sorted(c[1]["circle_id"] for c in calls if c[0] == "circles.messages") == [5, 6, 7]
                  and all(c[1].get("limit") == 100 for c in calls if c[0] == "circles.messages"), calls)
            check("U ...and that room carries no made-up unread_count (null, never the constant 0)",
                  r7 and "unread_count" in r7 and r7["unread_count"] is None, r7)

            # A room the bridge is slow for never holds the list: it says
            # unread_unknown and the other rooms still count. The slow read
            # outlasts the bridge client's own 30 s wait (REQUEST_TIMEOUT).
            async def slow_bridge(op, params):
                if op == "circles.list":
                    return {"circles": [{"id": 5, "circle_name": "Book club"}, {"id": 8, "circle_name": "Slow"}]}
                if op == "circles.messages":
                    if params["circle_id"] == 8:
                        await asyncio.sleep(60)
                    return {"messages": room_msgs[5], "total": 3, "next_before": None}
                return None
            C._bridge_call = slow_bridge
            began = time.monotonic()
            try:
                pl = await asyncio.wait_for(listed(), timeout=10)
            except asyncio.TimeoutError:
                pl = None
            finally:
                C._bridge_call = real_bridge
            took = time.monotonic() - began
            r5, r8 = ((item(pl, "circles", i) if pl else None) for i in (5, 8))
            check("U a slow room never holds the list: it answers in seconds, that room unread_unknown, "
                  "the others counted",
                  pl is not None and took < 10 and r8 and r8.get("unread_unknown") is True and "unread" not in r8
                  and r5 and r5["unread"] == 1, (round(took, 2), r5, r8))

            # A room whose page cannot be held against its mark (a time with no
            # offset; the mark has one) never breaks the list either.
            await pool.execute(
                "INSERT INTO circles_page_marks (entity_id, kind, ref, reader, seen_at) "
                "VALUES ($1, 'circle', '9', '', $2)", E, seen_at)

            async def odd_bridge(op, params):
                if op == "circles.list":
                    return {"circles": [{"id": 5, "circle_name": "Book club"}, {"id": 9, "circle_name": "Odd clock"}]}
                if op == "circles.messages":
                    if params["circle_id"] == 9:
                        naive = (seen_at + timedelta(seconds=5)).replace(tzinfo=None).isoformat()
                        return {"messages": [{"id": 1, "sender_entity_id": 140, "created_at": naive}],
                                "total": 1, "next_before": None}
                    return {"messages": room_msgs[5], "total": 3, "next_before": None}
                return None
            C._bridge_call = odd_bridge
            err = None
            try:
                pl = await listed()
            except Exception as ex:  # noqa: BLE001 — the failure this check reports
                pl, err = None, f"{type(ex).__name__}: {ex}"
            finally:
                C._bridge_call = real_bridge
            r5, r9 = ((item(pl, "circles", i) if pl else None) for i in (5, 9))
            check("U a room whose page cannot be counted never breaks the list: that room unread_unknown, "
                  "the others counted",
                  err is None and r9 and r9.get("unread_unknown") is True and r5 and r5["unread"] == 1,
                  (err, r5, r9))
            from server import circles_marks as CM

            async def full_bridge(op, params):
                return {"messages": [{"id": i, "sender_entity_id": 140,
                                      "created_at": (seen_at + timedelta(seconds=i + 1)).isoformat()}
                                     for i in range(100)], "total": 250, "next_before": "c"}
            capped = await CM.circle_unread(pool, E, [5], full_bridge)
            check("U a room with more unread than its newest page says unread_capped",
                  capped.get("5", {}).get("unread") == 100 and capped["5"].get("unread_capped") is True, capped)

        # ── N. the nudge route ──────────────────────────────────────────────
        async def nudge():
            sid = uuid.uuid4().hex
            try:
                await C.nudge(request({"session_id": sid, "text": "use the blue one", "client_id": "n-123_abc"},
                                      method="POST"))
                ids = list(S._nudge_ids.get(sid) or [])
                keeps = list(S._nudge_keeps_copy.get(sid) or [])
                # The route's words are the server's (pending_words):
                # no client keeps a copy to send again.
                check("N the sender's id reaches push_nudge as client_id (and the words are the server's, "
                      "not left to a client's copy)",
                      ids == ["n-123_abc"] and keeps == [False], (ids, keeps))
            finally:
                S.consume_nudges(sid)
            sid2 = uuid.uuid4().hex
            try:
                await C.nudge(request({"session_id": sid2, "text": "no id"}, method="POST"))
                check("N a nudge with no id still queues, with none",
                      list(S._nudge_ids.get(sid2) or []) == [None], S._nudge_ids.get(sid2))
            finally:
                S.consume_nudges(sid2)
            sid3 = uuid.uuid4().hex
            try:
                await C.nudge(request({"session_id": sid3, "text": "x", "client_id": "not an id!"}, method="POST"))
                check("N an id that is not one is refused", False)
            except HTTPException as ex:
                check("N an id that is not one is refused (400), and nothing is queued",
                      ex.status_code == 400 and sid3 not in S._pending_nudges, ex.detail)
            finally:
                S.consume_nudges(sid3)

        await section("S. the mark route", marks)
        await section("A. a reply watched to its end never lights", watched)
        await section("B. what the Host wrote lights a page", lights)
        await section("C. the thread list", the_list)
        await section("T. the page route", page_route)
        await section("D. the totals and the transcript", two_ways)
        await section("U. unread in the conversations list", unread)
        await section("N. the nudge route", nudge)
    finally:
        ME._get_memory_embedding = real_embed
        bs._pool, bs._entity_id = saved
        await pool.close()


async def main(dsn):
    await section("M. the marks table", lambda: migration_checks(dsn))
    await behaviour(dsn)
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
