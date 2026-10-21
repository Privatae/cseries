#!/usr/bin/env python3
"""Gate: the thread placement's SQL against a real kernel schema
(server/connectors/thread_pick.py).

  L. the Host is shown its OPEN threads and nothing else: the original
     conversation, open Circles threads, projects with a conversation;
     never a closed or deleted thread, an artifact, an archived project or
     a ticket project, and never another Host's rows. Newest first;
  E. each with its last exchange as the transcript shows it, markers and a
     wake-up's hidden reply left out, agreeing for every thread with that
     conversation's own clause;
  Q. the list's cost does not grow with the number of threads: a fixed
     number of queries (one grouped pass per kind of conversation), never
     one per thread;
  B. the list is cut to the budget, and the thread this chat used last
     stays in it whatever the budget;
  N. a new thread picked is a circles_threads row, origin 'user', with the
     Host's title, and the pick's marker is in that thread's transcript;
  K. a marker in a project, a Circles thread and the original conversation
     is read back by exactly that conversation's own clause, and by no
     other;
  X. end to end: place_connector on the real pool lands a message no thread
     clearly matches in the chat's last thread, and offers the turn only
     listed threads (the Host picks among them in its own turn);
  T. landing is one transaction: a marker that cannot be written leaves no
     new thread, and a thread closed or deleted (a project archived) while
     the Host chose is never landed in, with nothing written.

Fresh clone of the KERNEL's schema (scripts/kernel_schema.py) on TEST_DSN /
PEER_TEST_DSN, else a throwaway cseries-postgres through docker; no pod, no
network beyond loopback.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402
from mastercode import circles_threads as ct  # noqa: E402
from server.connectors import thread_pick as tp  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the pick's SQL.")
    sys.exit(2)

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


E = 1


async def turn(pool, content, role, *, ts, sctx=None, project_id=None, app_id=None, extra=None, entity=E):
    md = {"role": role}
    if sctx:
        md["source_context"] = sctx
    if app_id:
        md["app_id"] = app_id
    md.update(extra or {})
    await pool.execute(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, project_id, metadata, created_at)"
        " VALUES ($1, $2, 'turn', 'active', $3, $4::jsonb, now() - make_interval(mins => $5))",
        entity, content, project_id, json.dumps(md), ts)


async def shown(pool, kind, ref):
    """What that conversation's own reader returns, newest first."""
    args = [E]
    if kind == "project":
        args.append(int(ref))
        where = ["entity_id = $1", "memory_type = 'turn'", "status = 'active'", "project_id = $2"]
    else:
        where = ct.shown_where(args, None if kind == "app" else ref)
    return [r["content"] for r in await pool.fetch(
        f"SELECT content FROM entity_memory WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC",
        *args)]


async def newest_shown(pool, kind, ref):
    """The newest person and Host message that conversation's own reader
    shows, pick and split markers left out: one query per conversation, the
    shape the list's grouped pass must agree with."""
    rows = await shown_rows(pool, kind, ref)
    by_role = {}
    for content, md in rows:
        md = json.loads(md) if isinstance(md, str) else (md or {})
        if md.get("source_kind") in ("thread_marker", tp.PICK_MARKER_KIND):
            continue
        by_role.setdefault(md.get("role"), content)
    return (tp._clip(by_role.get("user"), tp._EXCHANGE_CHARS),
            tp._clip(by_role.get("assistant"), tp._EXCHANGE_CHARS))


async def shown_rows(pool, kind, ref):
    args = [E]
    if kind == "project":
        args.append(int(ref))
        where = ["entity_id = $1", "memory_type = 'turn'", "status = 'active'", "project_id = $2"]
    else:
        where = ct.shown_where(args, None if kind == "app" else ref)
    return [(r["content"], r["metadata"]) for r in await pool.fetch(
        f"SELECT content, metadata FROM entity_memory WHERE {' AND '.join(where)}"
        " ORDER BY created_at DESC, id DESC", *args)]


class Counting:
    """The pool, counting the queries made through it."""
    def __init__(self, pool):
        self.pool, self.n = pool, 0

    def __getattr__(self, name):
        f = getattr(self.pool, name)
        if name not in ("fetch", "fetchrow", "fetchval", "execute"):
            return f

        async def counted_call(*a, **kw):
            self.n += 1
            return await f(*a, **kw)
        return counted_call


async def counted(pool):
    cp = Counting(pool)
    got = await tp.list_candidates(cp, E, budget_tokens=1_000_000)
    return cp.n, got


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        other = await pool.fetchval("SELECT id FROM entity_registry WHERE id <> $1 LIMIT 1", E)
        if other is None:
            other = await pool.fetchval(
                "INSERT INTO entity_registry (display_name) VALUES ('other-host') RETURNING id")
        orig = ct.descriptor_for(None)
        # The original conversation: oldest activity.
        await turn(pool, "orig question", "user", ts=50, sctx=orig, app_id="circles")
        await turn(pool, "orig answer", "assistant", ts=49, sctx=orig, app_id="circles")
        # Thread A (open), newest; a split marker after its last exchange.
        a = (await ct.create_thread(pool, E, title="Q3 plan"))["id"]
        await turn(pool, "draft the Q3 plan", "user", ts=10, sctx=ct.descriptor_for(a), app_id="circles")
        await turn(pool, "Here is the Q3 draft", "assistant", ts=9, sctx=ct.descriptor_for(a), app_id="circles")
        await turn(pool, "→ Moved 3 messages into “x”", "assistant", ts=1, sctx=ct.descriptor_for(a),
                   extra={"source_kind": "thread_marker"})
        # Thread B closed, thread C deleted: both newer than A, both left out.
        b = (await ct.create_thread(pool, E, title="Closed one"))["id"]
        await turn(pool, "closed talk", "user", ts=2, sctx=ct.descriptor_for(b), app_id="circles")
        await pool.execute("UPDATE circles_threads SET closed_at = now() WHERE id = $1::uuid", b)
        c = (await ct.create_thread(pool, E, title="Deleted one"))["id"]
        await turn(pool, "deleted talk", "user", ts=3, sctx=ct.descriptor_for(c), app_id="circles")
        await pool.execute("UPDATE circles_threads SET deleted_at = now() WHERE id = $1::uuid", c)
        # Projects: P a real one; Q an artifact, R archived, S a ticket.
        async def project(name, **cols):
            keys = ["entity_id", "name"] + list(cols)
            vals = [E, name] + list(cols.values())
            ph = ", ".join(f"${i + 1}" + ("::jsonb" if k == "config" else "") for i, k in enumerate(keys))
            return await pool.fetchval(
                f"INSERT INTO projects ({', '.join(keys)}) VALUES ({ph}) RETURNING id", *vals)
        p = await project("Website")
        q = await project("Orbit", kind="artifact")
        r = await project("Old", status="archived")
        s = await project("Ticket 9", config=json.dumps({"ticket": {"n": 9}}))
        for pid, ts in ((p, 30), (q, 4), (r, 5), (s, 6)):
            await turn(pool, f"work on {pid}", "user", ts=ts, project_id=pid,
                       sctx={"kind": "project", "ref": str(pid)})
        # Another Host's thread, newest of all: never shown.
        x = (await ct.create_thread(pool, other, title="Not yours"))["id"]
        await turn(pool, "secret", "user", ts=0, sctx=ct.descriptor_for(x), entity=other)

        cands = await tp.list_candidates(pool, E, budget_tokens=100_000)
        keys = [cd.key for cd in cands]
        # There is no original conversation: its rows are
        # in the database (orig question / orig answer) and offered nowhere.
        check("L open threads only, newest first: thread A, project P; never the original conversation",
              keys == [f"thread:{a}", f"project:{p}"], keys)
        ca = cands[0] if cands else None
        check("E thread A's last exchange, the split marker left out",
              ca and (ca.last_user, ca.last_host, ca.title) == ("draft the Q3 plan", "Here is the Q3 draft", "Q3 plan"),
              ca)
        check("E ...though the original conversation's rows are still there",
              await pool.fetchval("SELECT count(*) FROM entity_memory WHERE content IN ('orig question', 'orig answer')") == 2)

        tiny = await tp.list_candidates(pool, E, budget_tokens=1, current=("thread", a))
        check("B a budget that fits nothing still carries the thread this chat used last",
              [cd.key for cd in tiny] == [f"thread:{a}"], [cd.key for cd in tiny])
        tiny_o = await tp.list_candidates(pool, E, budget_tokens=1, current=("app", "circles"))
        check("B ...and a chat last in the old original conversation carries nothing of it",
              "app:circles" not in [cd.key for cd in tiny_o], [cd.key for cd in tiny_o])
        mid = await tp.list_candidates(pool, E, budget_tokens=60)
        check("B a small budget keeps the newest that fit", mid and mid[0].key == f"thread:{a}" and len(mid) < 3,
              [cd.key for cd in mid])

        new = await tp.land_pick(pool, E, tp.Pick(ok=True, kind="thread", new=True, title="Garden plans",
                                                  reason="Nothing open is about the garden."), platform="telegram")
        row = await pool.fetchrow("SELECT title, origin, entity_id FROM circles_threads WHERE id = $1::uuid", new.ref)
        check("N a new thread is a circles_threads row, origin user, the Host's title",
              row and (row["title"], row["origin"], row["entity_id"]) == ("Garden plans", "user", E) and new.new, row)
        check("N the marker is in the new thread's transcript",
              await shown(pool, "thread", new.ref)
              == ["Telegram message, started this thread: Nothing open is about the garden."])

        await tp.land_pick(pool, E, tp.Pick(ok=True, kind="project", ref=str(p), reason="website"), platform="slack")
        # There is no original conversation: a pick naming
        # it lands nowhere and writes nothing, like a thread that is gone.
        try:
            await tp.land_pick(pool, E, tp.Pick(ok=True, kind="app", ref="circles", reason="chat"),
                               platform="discord")
            gone = False
        except tp.PickGone:
            gone = True
        await tp.land_pick(pool, E, tp.Pick(ok=True, kind="thread", ref=a, reason="q3"), platform="telegram")
        mp, ma = await shown(pool, "project", p), await shown(pool, "thread", a)
        check("K the project's marker is read by the project's clause",
              mp[0] == "Slack message, picked this thread: website", mp)
        check("K a pick naming the old original conversation is refused (PickGone) and writes no marker",
              gone and not await pool.fetchval(
                  "SELECT count(*) FROM entity_memory WHERE content = 'Discord message, picked this thread: chat'"))
        check("K thread A's marker is read by thread A's clause", ma[0] == "Telegram message, picked this thread: q3", ma)
        check("K and each marker only there",
              "Telegram message, picked this thread: q3" not in mp
              and "Slack message, picked this thread: website" not in ma)
        md = await pool.fetchval(
            "SELECT metadata FROM entity_memory WHERE content = 'Telegram message, picked this thread: q3'")
        md = json.loads(md) if isinstance(md, str) else md
        check("K a marker is assistant-visible furniture: its own source_kind",
              md.get("role") == "assistant" and md.get("source_kind") == tp.PICK_MARKER_KIND, md)
        again = await tp.list_candidates(pool, E, budget_tokens=100_000)
        ca2 = next(cd for cd in again if cd.key == f"thread:{a}")
        check("E a pick marker is never shown as the thread's last exchange",
              ca2.last_host == "Here is the Q3 draft", ca2)

        pl = await tp.place_connector(pool, E, message="did the site deploy?", platform="telegram",
                                      current=("thread", a))
        check("X place_connector on the real pool: no clear match lands in the chat's last thread",
              pl and (pl.kind, pl.ref, pl.new) == ("thread", str(a), False), pl)
        offered = [f"{c['kind']}:{c['ref']}" for c in (pl.choice if pl else ())]
        listed = [cd.key for cd in await tp.list_candidates(pool, E, budget_tokens=1_000_000)]
        never = {f"thread:{b}", f"thread:{c}", f"thread:{x}", f"project:{q}", f"project:{r}", f"project:{s}",
                 f"thread:{a}"}
        check("X the turn is offered the listed threads only: no closed, deleted, artifact, archived, ticket "
              "or other-Host one, and never the one it landed in",
              offered and set(offered) == set(listed) - {f"thread:{a}"} and not set(offered) & never,
              (offered, listed))

        # A wake-up's own reply (the hidden lane) is the newest row of thread
        # A; the transcript never shows it, so neither does the list.
        await turn(pool, "hidden wake-up reply", "assistant", ts=0, sctx=ct.descriptor_for(a),
                   app_id="scheduled_task")
        n_small, small = await counted(pool)
        for i in range(30):
            t = (await ct.create_thread(pool, E, title=f"Extra {i}"))["id"]
            await turn(pool, f"extra question {i}", "user", ts=200 + i, sctx=ct.descriptor_for(t), app_id="circles")
            await turn(pool, f"extra answer {i}", "assistant", ts=199 + i, sctx=ct.descriptor_for(t),
                       app_id="circles")
        n_big, big = await counted(pool)
        check("Q the list costs the same number of queries with 30 more threads",
              n_big == n_small and len(big) == len(small) + 30, (n_small, n_big, len(small), len(big)))
        a_big = next(cd for cd in big if cd.key == f"thread:{a}")
        check("E a wake-up's hidden reply is never shown as a thread's last exchange",
              a_big.last_host == "Here is the Q3 draft", a_big)
        wrong = []
        for cd in big:
            want = await newest_shown(pool, cd.kind, cd.ref)
            if (cd.last_user, cd.last_host) != want:
                wrong.append((cd.key, (cd.last_user, cd.last_host), want))
        check(f"E every one of the {len(big)} threads shows the exchange its own transcript clause reads",
              not wrong, wrong[:3])

        # T: landing is one transaction. A marker that cannot be written
        # leaves no new thread behind.
        real_write = tp.write_pick_marker

        async def broken(*a, **kw):
            raise RuntimeError("the marker write failed")
        tp.write_pick_marker = broken
        try:
            await tp.land_pick(pool, E, tp.Pick(ok=True, kind="thread", new=True, title="Orphan check",
                                                reason="r"), platform="telegram")
            raised = False
        except RuntimeError:
            raised = True
        finally:
            tp.write_pick_marker = real_write
        orphans = await pool.fetchval("SELECT count(*) FROM circles_threads WHERE title = 'Orphan check'")
        check("T a marker that cannot be written leaves no new thread behind", raised and orphans == 0,
              (raised, orphans))

        # A thread closed or deleted, or a project archived, while the Host
        # was choosing is never landed in, and nothing is written there.
        late = (await ct.create_thread(pool, E, title="Closed during the pick"))["id"]
        await pool.execute("UPDATE circles_threads SET closed_at = now() WHERE id = $1::uuid", late)
        for label, kind, ref in (("closed thread", "thread", str(late)), ("deleted thread", "thread", str(c)),
                                 ("archived project", "project", str(r))):
            reason = f"late {label}"
            try:
                await tp.land_pick(pool, E, tp.Pick(ok=True, kind=kind, ref=ref, reason=reason), platform="telegram")
                refused = False
            except getattr(tp, "PickGone", ValueError):
                refused = True
            written = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE content = $1",
                                          f"Telegram message, picked this thread: {reason}")
            check(f"T a {label} during the pick is never landed in", refused and written == 0, (refused, written))
    finally:
        await pool.close()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
