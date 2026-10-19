#!/usr/bin/env python3
"""Gate: creating a project from a conversation moves nothing out of it, and
the new project's thread opens with a marker pointing back to it.

Measured on one Host 2026-10-05 (Circles thread "Telegram connector",
a46c9283…): the person typed "Create a telegram connector please…", the Host
ran `operate` create_project in that turn, and the carry-forward
(tools._detect_active_arc) re-tagged every untagged turn of the Host from the
last four hours back to the newest five-minute silence into project 417. The
turn's own user row was already written; its reply was not, so the request
(row 9190) went to the project and the reply (9192) stayed in the thread. The
thread opened with an answer to nothing, the project with a question nobody
answered, and nothing said why. The selection also never asked WHICH
conversation a row was in, so any other thread's or the original
conversation's recent rows went too.

Against the KERNEL's schema (scripts/kernel_schema.py), the turn's real order
— the user row, then create_project, then the reply:

  S. no split: the request and the reply both stay in the thread they were
     typed in, as that thread's own reader shows it, in order;
  X. no cross-conversation reach: another Circles thread, the original
     conversation and an untagged row from another app keep exactly the
     project_id and metadata they had (a full before/after snapshot of every
     row but the marker);
  M. the marker: exactly one row in the new project, read by the project's
     own clause (project_id) and by no Circles conversation, a thread_marker
     with the thread's title and a link that opens it in Circles;
  O. from the original conversation: nothing moves, and the marker names it
     and links to thread:original;
  R. the result tells the Host where it was started and claims no carried
     turns.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; no pod, no network beyond
loopback.
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
# The link is made absolute on the Host's own origin; the handle is read from
# here first (network/bastion_enroll.resolve_self_handle), never the bridge.
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402
from mastercode import circles_threads as ct  # noqa: E402
from mastercode.runtime import tools as T  # noqa: E402
import server.routes.projects as P  # noqa: E402
from server.home_origin import HOME  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove nothing moves.")
    sys.exit(2)

FAILED = 0
E = 1
ORIGIN = HOME.origin   # the Host's one address (reachout.resolve_host_origin)


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


async def turn(pool, content, role, *, mins_ago, sctx=None, app_id=None, project_id=None):
    md = {"role": role}
    if sctx:
        md["source_context"] = sctx
    if app_id:
        md["app_id"] = app_id
    return await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, project_id, metadata, created_at)"
        " VALUES ($1, $2, 'turn', 'active', $3, $4::jsonb, now() - make_interval(secs => $5))"
        " RETURNING id",
        E, content, project_id, json.dumps(md), float(mins_ago * 60))


async def snapshot(pool):
    return {r["id"]: (r["project_id"], r["metadata"]) for r in await pool.fetch(
        "SELECT id, project_id, metadata FROM entity_memory WHERE entity_id = $1 ORDER BY id", E)}


async def shown(pool, thread_id):
    args = [E]
    where = ct.shown_where(args, thread_id)
    return [r["content"] for r in await pool.fetch(
        f"SELECT content FROM entity_memory WHERE {' AND '.join(where)} ORDER BY created_at, id", *args)]


async def in_project(pool, pid):
    return [(r["content"], json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"])
            for r in await pool.fetch(
                "SELECT content, metadata FROM entity_memory WHERE entity_id = $1"
                " AND memory_type = 'turn' AND status = 'active' AND project_id = $2"
                " ORDER BY created_at, id", E, pid)]


async def main(dsn):
    # No disk and no gitea from a test: the project row is what is under test.
    async def _no_repo(pool, pid, name):
        return {"repo": None, "reason": "test"}
    P.ensure_project_dir = lambda pid: None
    P.ensure_project_repo = _no_repo

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        orig = ct.descriptor_for(None)
        a = (await ct.create_thread(pool, E, title="Telegram connector"))["id"]
        b = (await ct.create_thread(pool, E, title="Gmail connector"))["id"]
        # Every row inside the old arc: four hours, no five-minute silence.
        await turn(pool, "orig question", "user", mins_ago=12, sctx=orig, app_id="circles")
        await turn(pool, "orig answer", "assistant", mins_ago=11, sctx=orig, app_id="circles")
        await turn(pool, "gmail status?", "user", mins_ago=8, sctx=ct.descriptor_for(b), app_id="circles")
        await turn(pool, "gmail is waiting on its ticket", "assistant", mins_ago=7,
                   sctx=ct.descriptor_for(b), app_id="circles")
        await turn(pool, "a notes chat", "user", mins_ago=6, sctx={"kind": "app", "ref": "notes"},
                   app_id="notes")
        await turn(pool, "earlier in this thread", "assistant", mins_ago=4, sctx=ct.descriptor_for(a),
                   app_id="circles")
        # The turn: the person's row is written first ...
        await turn(pool, "Create a telegram connector please", "user", mins_ago=1,
                   sctx=ct.descriptor_for(a), app_id="circles")
        before = await snapshot(pool)

        # ... then the Host's create_project, from that thread (the session's
        # interaction_context is "chat"; the thread rides thread_id) ...
        out = await T._exec_create_project({"name": "Telegram connector"}, E, pool,
                                           origin_context="chat", thread_id=a.upper())
        pid = (out.get("result") or {}).get("project_id")
        # ... then the reply, written after the tools ran.
        await turn(pool, "Telegram connector project created — building it now.", "assistant",
                   mins_ago=0, sctx=ct.descriptor_for(a), app_id="circles")
        after = await snapshot(pool)

        check("R the project was created", out.get("status") == "ok" and isinstance(pid, int), out)
        thread_a = await shown(pool, a)
        check("S no split: the request and its reply stay together in the thread they were typed in",
              thread_a[-2:] == ["Create a telegram connector please",
                                "Telegram connector project created — building it now."]
              and "earlier in this thread" in thread_a, thread_a)
        new_rows = [i for i in after if i not in before]
        unchanged = all(after.get(i) == v for i, v in before.items())
        check("X no row that existed is touched — other threads, the original conversation, "
              "another app's chat, and this thread", unchanged,
              [i for i, v in before.items() if after.get(i) != v])
        check("X the other thread and the original conversation read exactly as before",
              await shown(pool, b) == ["gmail status?", "gmail is waiting on its ticket"]
              and await shown(pool, None) == ["orig question", "orig answer"],
              (await shown(pool, b), await shown(pool, None)))

        rows = await in_project(pool, pid)
        link = f"{ORIGIN}/?open=circles&view=thread:{a}"
        md = rows[0][1] if len(rows) == 1 else {}
        mk = md.get("marker") or {}
        check("M exactly one row in the new project: the marker (plus the reply row, new)",
              len(rows) == 1 and len(new_rows) == 2, (rows, new_rows))
        check("M it is a thread_marker in the project's thread, naming the thread it came from",
              md.get("source_kind") == "thread_marker" and md.get("role") == "assistant"
              and md.get("source_context") == {"kind": "project", "ref": str(pid)}
              and mk.get("started_from") == {"kind": "thread", "ref": a}
              and mk.get("title") == "Telegram connector" and mk.get("link") == link, md)
        check("M its words carry the title and the link back",
              rows and "“Telegram connector” in Circles" in rows[0][0] and f"({link})" in rows[0][0],
              rows[:1])
        check("M no Circles conversation shows it",
              all("Started from" not in c for c in thread_a + await shown(pool, b) + await shown(pool, None)))
        sf = (out.get("result") or {}).get("started_from") or {}
        check("R the result says where it was started, and claims no carried turns",
              sf == {"kind": "thread", "ref": a, "title": "Telegram connector", "link": link}
              and "carried_turns" not in (out.get("result") or {}), out)

        # O. From the original conversation.
        before2 = await snapshot(pool)
        out2 = await T._exec_create_project({"name": "From the original"}, E, pool, origin_context="chat")
        pid2 = (out2.get("result") or {}).get("project_id")
        after2 = await snapshot(pool)
        rows2 = await in_project(pool, pid2)
        mk2 = (rows2[0][1].get("marker") or {}) if rows2 else {}
        check("O from the original conversation nothing moves",
              all(after2.get(i) == v for i, v in before2.items()) and len(after2) == len(before2) + 1)
        check("O ...and the marker names it and links to it",
              mk2.get("started_from") == {"kind": "app", "ref": "circles"}
              and mk2.get("title") == ct.LEGACY_TITLE
              and mk2.get("link") == f"{ORIGIN}/?open=circles&view=thread:original", mk2)
    finally:
        await pool.close()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
print("test_create_project_keeps_exchange_pg: " + ("FAILED" if rc else "ok"))
sys.exit(rc)
