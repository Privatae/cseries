#!/usr/bin/env python3
"""Gate: the Workbench page data against a real kernel schema.

  M. migrations/v215 on the full current schema: the three projects columns
     exist; running the file again, twice over, changes nothing and fails
     nothing; a negative waiting count is refused.
  S. the seen mark: PUT /api/projects/{id}/seen stores it and returns it; a
     second PUT moves it forward, a later mark already stored is kept (never
     backwards); updated_at, and so the list's order, is untouched; another
     entity's project is a 404.
  L. last_landed_at: the Host's reply and a task that finished move it; the
     person's own message, a task still queued or running and a cancelled one
     do not; another project's rows never count. A reply lands when the turn
     writers write it, not when it began: a held turn's final reply written
     over its row, a long reply finished after the person's last look. The
     writers keep that moment in entity_memory.landed_at (v216), set at every
     write and never earlier than when the reply began; a landed_epoch key in
     a row's metadata, as older writers stamped it, is never read.
  W. waiting_on_you: the open ask cards are the project's open tickets, the
     same count the Workbench's own card poll gets (list_tickets_service
     asked_in=project:<id>); the Code Approvals count is the copy each forge
     read keeps: written by list_approvals (the bench's "N waiting"), by a
     save point that opens the Code Approval or moves it to a new version, and
     by the Host's Auto-Approve merge. The list is one query whatever the
     number of projects, and reaches no forge.
  F. a forge read that fails (an error, a failed second page, a 2xx with no
     body) never moves the stored count or its checked time: not through the
     listing, not through a refresh, not after a save point or a merge that
     went through.
  T. the running totals (context_budget.running_spend): tokens are the turns'
     own spend records summed, never the 24-entry per-call list, with unknown
     turns counted; dollars are the prices printed under the replies
     (est_cost_usd) summed and labelled an estimate, also on a thread with no
     dollar allowance, where the meter records none; subscription and free
     replies add nothing, unpriced and partly priced ones make it partial;
     another project's turns, a Circles row and an archived row never count;
     the same function sums a Circles thread by its descriptor.
  P. GET /api/projects/{id}/page: its shape; where_this_stands is the text
     thread_state() builds, and its fields are
     the goal and handoff rows; totals are running_spend's; engines are the
     Compute app's own row for the project; the marks match the list row; the
     route needs read:brain_memory and read:inference_log; 404 for a project
     the entity does not own.
  E. thread_state() gives main's text byte for byte (a verbatim copy of the
     function from before this change runs on the same rows): goals live and
     not, id ties, an empty close_turn, the hidden lanes, odd handoff values, a
     handoff stored as a string, every edge scope, on the platform's pool and
     on one that decodes JSON. The text is in every Host prompt.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; no pod, no network beyond
loopback (the forge is a stub in this process).

  PYTHONPATH=brain python3 scripts/test_workbench_page_data_pg.py
"""
import asyncio
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the page data's SQL and v215.")
    sys.exit(2)

FAILED = 0
E = 1
OTHER_E = 2
MIGRATION = ROOT / "migrations" / "v215_project_page_marks.sql"
NEW_COLS = ("seen_at", "approvals_waiting", "approvals_checked_at")


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


async def section(name, coro_fn):
    """One lettered section. An exception is a failure of that section, not
    the end of the run, so a tree without the feature shows every part it
    lacks."""
    print(f"-- {name}")
    try:
        await coro_fn()
    except Exception as e:  # noqa: BLE001 — reported as the section's failure
        check(f"{name}: ran to the end", False, f"{type(e).__name__}: {e}")


async def attempt(coro):
    """The awaited value, or the exception it raised: a tree without a column
    fails the one check that reads it, and the section runs on."""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 — reported by the check that reads it
        return e


# ── fixtures ────────────────────────────────────────────────────────────────

async def project(pool, name, *, eid=E, status="active", config=None, repo=None):
    return await pool.fetchval(
        "INSERT INTO projects (entity_id, name, status, config, repo, updated_at, created_at) "
        "VALUES ($1, $2, $3, $4::jsonb, $5, now() - interval '2 days', now() - interval '2 days') "
        "RETURNING id", eid, name, status, json.dumps(config or {}), repo)


HOST = {"gen_ms": 1500, "model": "anthropic/claude-opus-5"}


def spend(calls, usd, uncached, cache_read, cache_write, output, **extra):
    return {"allowance_basis": "usd" if usd is not None else "none", "calls": calls,
            "spent_usd": usd, "spent_tokens": uncached + cache_read + cache_write + output,
            "tokens": {"uncached": uncached, "cache_read": cache_read,
                       "cache_write": cache_write, "output": output},
            "last_call": {"in": 10, "cached": 0, "cache_write": 0, "out": 1},
            "stopped_on_allowance": False, **extra}


async def turn(pool, pid, role, *, secs_ago, sp=None, per_call=None, extra=None,
               sctx=None, status="active"):
    """One turn row as the writers lay it down (memory_engine._write_turn_row_landed):
    metadata.role, the source_context descriptor, and on a Host reply the
    metrics stamp and the context ledger with its spend record."""
    md = {"source_kind": "chat_turn", "role": role, "app_id": "workbench",
          "source_context": sctx or {"kind": "project", "ref": str(pid)}}
    if sp is not None or per_call is not None:
        led = {"calls": len(per_call or []), "per_call": per_call or []}
        if sp is not None:
            led["spend"] = sp
        md["context_ledger"] = led
    md.update(extra or {})
    return await pool.fetchrow(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, project_id, metadata, created_at)"
        " VALUES ($1, $2, 'turn', $3, $4, $5::jsonb, now() - make_interval(secs => $6))"
        " RETURNING id, created_at",
        E, f"{role} {secs_ago}", status, pid, json.dumps(md), float(secs_ago))


async def task(pool, pid, status, *, secs_ago):
    done = status in ("completed", "failed", "cancelled")
    return await pool.fetchrow(
        "INSERT INTO entity_tasks (entity_id, title, task_type, status, result, created_at, started_at,"
        " completed_at) VALUES ($1, $2, 'agent', $3, $4::jsonb, now() - make_interval(secs => $5),"
        " now() - make_interval(secs => $5),"
        " CASE WHEN $6 THEN now() - make_interval(secs => $5) END) RETURNING task_id, completed_at",
        E, f"{status} task", status, json.dumps({"project_id": pid}), float(secs_ago), done)


async def ticket(pool, asked_in, *, status="open", eid=E):
    md = {"asked_in": asked_in} if asked_in is not None else {}
    return await pool.fetchval(
        "INSERT INTO entity_tickets (entity_id, source, sitrep, options, status, metadata)"
        " VALUES ($1, 'runtime', 'which one?', $2::jsonb, $3, $4::jsonb) RETURNING id",
        eid, json.dumps([{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]), status, json.dumps(md))


class FakeForge:
    """The project's forge, in this process: open and closed pull requests,
    answering the calls list_approvals, _ensure_pr and _merge_rolling_pr make."""

    def __init__(self, repo):
        self.repo, self.prs, self.calls, self._n = repo, {}, [], 0

    def add(self, ref, sha, *, merged=False):
        self._n += 1
        self.prs[self._n] = {
            "number": self._n, "title": f"change {self._n}", "body": "", "user": {"login": "host"},
            "state": "closed" if merged else "open", "merged": merged,
            "merged_at": "2026-10-08T00:00:00Z" if merged else None,
            "updated_at": "2026-10-08T00:00:00Z",
            "head": {"ref": ref, "sha": sha, "repo_id": 1}, "base": {"ref": "main", "repo_id": 1}}
        return self._n

    async def request(self, method, path, params=None, json=None, **kw):  # noqa: A002 — the forge's own name
        self.calls.append((method, path, dict(params or {})))
        base = f"/api/v1/repos/{self.repo}"
        rest = path[len(base):] if path.startswith(base) else None
        if rest == "" and method == "GET":
            return {"default_branch": "main", "id": 1, "empty": False}
        if rest == "/pulls" and method == "GET":
            want_open = (params or {}).get("state", "open") == "open"
            page = int((params or {}).get("page", 1))
            items = [p for p in self.prs.values() if (p["state"] == "open") == want_open]
            return items[(page - 1) * 50: page * 50]
        if rest == "/pulls" and method == "POST":
            n = self.add(json["head"], f"new{self._n + 1}")
            self.prs[n].update(title=json.get("title"), body=json.get("body"))
            return self.prs[n]
        m = re.fullmatch(r"/pulls/(\d+)(/commits|/merge)?", rest or "")
        if m:
            pr = self.prs[int(m.group(1))]
            if m.group(2) == "/commits":
                return []
            if m.group(2) == "/merge" and method == "POST":
                pr.update(state="closed", merged=True, merged_at="2026-10-08T01:00:00Z")
                return {}
            if method == "PATCH":
                pr.update({k: v for k, v in (json or {}).items() if k in ("title", "body")})
            return pr
        raise AssertionError(f"unexpected forge call {method} {path}")


class BrokenForge(FakeForge):
    """FakeForge whose read of the open pull requests fails once `broken` is
    set: "raise" (the forge clients' HTTPException for a non-2xx), "page2" (the
    first page answers, the second fails) or "empty" (a 2xx with no body, which
    the clients hand back as {}). `break_after(method, path)` sets it after a
    call that answered, so a save point or a merge goes through and only the
    count's read after it fails."""

    def __init__(self, repo, broken=None, *, break_after=None, mode="raise"):
        super().__init__(repo)
        self.broken, self.break_after, self.mode = broken, break_after, mode

    async def request(self, method, path, params=None, json=None, **kw):  # noqa: A002
        from fastapi import HTTPException
        if (self.broken and method == "GET" and path == f"/api/v1/repos/{self.repo}/pulls"
                and (params or {}).get("state", "open") == "open"):
            page = int((params or {}).get("page", 1))
            if self.broken == "raise" or (self.broken == "page2" and page >= 2):
                self.calls.append((method, path, dict(params or {})))
                raise HTTPException(502, "gitea: <html>502 Bad Gateway</html>")
            if self.broken == "empty":
                self.calls.append((method, path, dict(params or {})))
                return {}
        out = await super().request(method, path, params=params, json=json, **kw)
        if self.break_after is not None and self.break_after(method, path):
            self.broken = self.mode
        return out


# thread_state() exactly as it stood on main 41f8837fc
# (brain/mastercode/runtime/close_turn.py:832-884), before this change moved
# its two queries into _goal_row_sql/_handoff_row_sql for thread_state_parts.
# Section E runs it and today's thread_state() on the same rows: the text goes
# into every Host prompt ("Where this thread stands"), so a refactor that
# changed one byte of it would change prompts and break their cache.
async def _thread_state_main(pool, entity_id: int, scope):
    if not (isinstance(scope, dict) and scope.get("kind")):
        return None
    kind, ref = str(scope["kind"]), str(scope.get("ref") or "")
    goal = await pool.fetchval(
        """
        SELECT content FROM entity_memory
         WHERE entity_id = $1 AND memory_type = 'goal' AND status = 'active'
           AND resolved_at IS NULL AND t_valid_until IS NULL
           AND metadata->'source_context'->>'kind' = $2
           AND metadata->'source_context'->>'ref' = $3
         ORDER BY created_at DESC, id DESC LIMIT 1
        """, entity_id, kind, ref)
    # A replay, an A/B variant or a fired wake-up runs under this descriptor
    # without being part of the conversation: its handoff is never the last
    # turn's (memory_engine.NOT_HIDDEN_LANE_ROW, the transcript's own filter).
    from mastercode.memory_engine import NOT_HIDDEN_LANE_ROW
    ho = await pool.fetchval(
        f"""
        SELECT metadata->'close_turn'->'handoff' FROM entity_memory
         WHERE entity_id = $1 AND memory_type = 'turn'
           AND metadata->>'role' = 'assistant' AND metadata ? 'close_turn'
           AND metadata->'source_context'->>'kind' = $2
           AND metadata->'source_context'->>'ref' = $3
           AND {NOT_HIDDEN_LANE_ROW}
         ORDER BY created_at DESC, id DESC LIMIT 1
        """, entity_id, kind, ref)
    if isinstance(ho, str):
        try:
            ho = json.loads(ho)
        except (TypeError, ValueError):
            ho = None
    ho = ho if isinstance(ho, dict) else {}
    lines = []
    if goal:
        lines.append(f"Goal: {goal}")
    if ho.get("next_step"):
        lines.append(f"Next step (from the last turn): {ho['next_step']}")
    if ho.get("open_work"):
        lines.append("Open work: " + "; ".join(str(x) for x in ho["open_work"]))
    if ho.get("identifiers"):
        lines.append("Identifiers: " + ", ".join(str(x) for x in ho["identifiers"]))
    if not lines:
        return None
    from mastercode.context_budget import THREAD_STATE_PREFIX
    return THREAD_STATE_PREFIX + "\n" + "\n".join(lines)


def forge_session(fake):
    return types.SimpleNamespace(request=fake.request, user="host", pr_url=lambda pr: None,
                                 forge=types.SimpleNamespace(repo=fake.repo, on_machine=False,
                                                             machine=None))


class CountingPool:
    """The pool, counting every call the code under test makes on it."""

    def __init__(self, pool):
        self._pool, self.calls = pool, []

    def __getattr__(self, name):
        attr = getattr(self._pool, name)
        if name in ("fetch", "fetchrow", "fetchval", "execute", "executemany", "acquire"):
            def counted(*a, **k):
                self.calls.append(name)
                return attr(*a, **k)
            return counted
        return attr


def by_id(listing):
    return {r["id"]: r for r in listing["projects"]}


# ── M. the columns ──────────────────────────────────────────────────────────

async def migration_checks(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async def cols():
            return {r["column_name"] for r in await pool.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'projects' AND column_name = ANY($1::text[])", list(NEW_COLS))}
        check("M projects has seen_at, approvals_waiting, approvals_checked_at", await cols() == set(NEW_COLS),
              await cols())
        r1 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
        check("M v215 runs again on the full schema and fails nothing", r1.returncode == 0, r1.stderr[-400:])
        r2 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
        check("M a second run fails nothing and changes nothing",
              r2.returncode == 0 and await cols() == set(NEW_COLS), r2.stderr[-400:])
        pid = await pool.fetchval(
            "INSERT INTO projects (entity_id, name) VALUES ($1, 'check') RETURNING id", E)
        try:
            await pool.execute("UPDATE projects SET approvals_waiting = -1 WHERE id = $1", pid)
            check("M a negative waiting count is refused", False, "the UPDATE went through")
        except asyncpg.CheckViolationError:
            check("M a negative waiting count is refused", True)
        await pool.execute("DELETE FROM projects WHERE id = $1", pid)
    finally:
        await pool.close()


# ── the behaviour, on the full schema ───────────────────────────────────────

async def behaviour(dsn):
    from fastapi import HTTPException

    from server import brain_service as bs
    import server.routes.projects as P
    from server.routes import code_approvals as CA
    from server.routes import project_forge as PF
    from server.routes import project_git as PG
    from server.routes import compute as CMP
    from server.routes import tickets as TK

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    saved = (bs._pool, bs._entity_id)
    bs._pool, bs._entity_id = pool, E

    # No address for this Host in a test: a link is dropped, never looked up.
    async def _no_origin(_eid):
        return ""
    real_origin = CA.host_origin
    CA.host_origin = _no_origin
    try:
        async def seen_mark():
            pid = await project(pool, "Seen")
            other = await project(pool, "Someone else's", eid=OTHER_E)
            before = await pool.fetchval("SELECT updated_at FROM projects WHERE id = $1", pid)
            r1 = await P.mark_project_seen(pid)
            stored = await pool.fetchval("SELECT seen_at FROM projects WHERE id = $1", pid)
            check("S PUT seen stores the mark and returns the stored time",
                  r1["project_id"] == pid and r1["seen_at"] is not None and r1["seen_at"] == stored, r1)
            r2 = await P.mark_project_seen(pid)
            check("S a second PUT moves it forward", r2["seen_at"] >= r1["seen_at"], (r1, r2))
            later = await pool.fetchval(
                "UPDATE projects SET seen_at = now() + interval '1 hour' WHERE id = $1 RETURNING seen_at", pid)
            r3 = await P.mark_project_seen(pid)
            check("S ...and never backwards: a later mark already stored is kept",
                  r3["seen_at"] == later
                  and await pool.fetchval("SELECT seen_at FROM projects WHERE id = $1", pid) == later, (r3, later))
            check("S marking a page seen never touches updated_at (being read is not activity)",
                  await pool.fetchval("SELECT updated_at FROM projects WHERE id = $1", pid) == before)
            try:
                await P.mark_project_seen(other)
                check("S another entity's project is refused", False)
            except HTTPException as e:
                check("S another entity's project is refused (404), its mark untouched",
                      e.status_code == 404
                      and await pool.fetchval("SELECT seen_at FROM projects WHERE id = $1", other) is None,
                      e.status_code)
            row = by_id(await P.list_projects(include_archived=True, limit=1000)).get(pid) or {}
            check("S the list row carries the mark", row.get("seen_at") == later, row.get("seen_at"))

        async def landed():
            a = await project(pool, "Landed A")
            b = await project(pool, "Landed B")

            async def row(pid):
                return by_id(await P.list_projects(include_archived=True, limit=1000))[pid]
            await turn(pool, a, "user", secs_ago=600)
            check("L the person's own message is not something that landed",
                  (await row(a))["last_landed_at"] is None, (await row(a))["last_landed_at"])
            reply = await turn(pool, a, "assistant", secs_ago=480, extra=HOST)
            check("L the Host's reply lands", (await row(a))["last_landed_at"] == reply["created_at"],
                  (await row(a))["last_landed_at"])
            mine = await turn(pool, a, "user", secs_ago=60)
            r = await row(a)
            check("L a newer message of the person's moves the activity but not the landing",
                  r["last_landed_at"] == reply["created_at"] and r["last_activity_at"] == mine["created_at"], r)
            for st, ago in (("queued", 30), ("in_progress", 30), ("cancelled", 20)):
                await task(pool, a, st, secs_ago=ago)
            check("L a task still queued or running, or cancelled, does not land",
                  (await row(a))["last_landed_at"] == reply["created_at"], (await row(a))["last_landed_at"])
            done = await task(pool, a, "failed", secs_ago=120)
            check("L a task that finished (failed too: the person needs to see it) lands",
                  (await row(a))["last_landed_at"] == done["completed_at"], (await row(a))["last_landed_at"])
            elsewhere = await turn(pool, b, "assistant", secs_ago=5, extra=HOST)
            await task(pool, b, "completed", secs_ago=1)
            check("L another project's reply and task never land here",
                  (await row(a))["last_landed_at"] == done["completed_at"]
                  and (await row(b))["last_landed_at"] > elsewhere["created_at"], (await row(b)))

            # Through the turn writers themselves (memory_engine), which stamp
            # created_at with when a reply BEGAN. No embedder here: a row is
            # written without a vector, as it is when the embedder is down.
            from mastercode import memory_engine as ME
            real_embed = ME._get_memory_embedding

            async def _no_embed(*_a, **_k):
                return None
            ME._get_memory_embedding = _no_embed
            try:
                h = await project(pool, "Held reply")
                tags = {"source_context": {"kind": "project", "ref": str(h)}, "app_id": "workbench", **HOST}
                began = await pool.fetchval("SELECT now() - interval '20 minutes'")
                rid = await ME.write_turn_row_by_token(
                    pool, E, "s-held", "assistant", "Started the build; waiting on it.", token="tok-held",
                    held=True, project_id=h, tags=tags, created_at=began)
                landing = ("SELECT landed_at, created_at, metadata ? 'landed_epoch' AS stamped"
                           " FROM entity_memory WHERE id = $1")
                first = await attempt(pool.fetchrow(landing, rid))
                seen = (await P.mark_project_seen(h))["seen_at"]
                await asyncio.sleep(0.05)
                await ME.write_turn_row_by_token(
                    pool, E, "s-held", "assistant", "The build passed. Here is what changed.", token="tok-held",
                    held=False, row_id=rid, project_id=h, tags=tags, created_at=began)
                kept = await pool.fetchrow("SELECT created_at, content FROM entity_memory WHERE id = $1", rid)
                landed_h = (await row(h))["last_landed_at"]
                check("L a held turn's final reply, written over its row after the person's last look, lands "
                      "after that look (the row keeps when the reply began)",
                      kept is not None and kept["created_at"] == began
                      and kept["content"].startswith("The build passed")
                      and landed_h is not None and landed_h > seen, (dict(kept or {}), landed_h, seen))
                last = await attempt(pool.fetchrow(landing, rid))
                check("L the turn writers keep when a row landed in its landed_at column: set by the held write, "
                      "moved past the person's look by the rewrite, and that is what the list reads; nothing is "
                      "stamped in metadata",
                      not isinstance(first, BaseException) and not isinstance(last, BaseException)
                      and first["landed_at"] is not None and first["landed_at"] >= began
                      and last["landed_at"] > first["landed_at"] and last["landed_at"] > seen
                      and landed_h == last["landed_at"] and not first["stamped"] and not last["stamped"],
                      (first, last, landed_h))

                w = await project(pool, "Long reply")
                began_w = await pool.fetchval("SELECT now() - interval '5 minutes'")
                seen_w = (await P.mark_project_seen(w))["seen_at"]
                await asyncio.sleep(0.05)
                rid_w = await ME.write_turn_row(
                    pool, E, "s-long", "assistant", "Done: three files changed.", project_id=w,
                    tags={"source_context": {"kind": "project", "ref": str(w)}, **HOST}, created_at=began_w)
                landed_w = (await row(w))["last_landed_at"]
                check("L a reply that began before the person's last look and was written after it lands after "
                      "the look", rid_w is not None and landed_w is not None and landed_w > seen_w,
                      (rid_w, landed_w, seen_w))

                # landed_at is never earlier than created_at, so the list's
                # COALESCE(landed_at, created_at) is the later of the two, as
                # main's GREATEST of the stamp and created_at was.
                f = await project(pool, "Reply dated ahead")
                ahead = await pool.fetchval("SELECT now() + interval '1 hour'")
                ftags = {"source_context": {"kind": "project", "ref": str(f)}, **HOST}
                rid_a = await ME.write_turn_row(pool, E, "s-ahead", "assistant", "Dated ahead of the clock.",
                                                project_id=f, tags=ftags, created_at=ahead)
                rid_b = await ME.write_turn_row_by_token(
                    pool, E, "s-ahead-held", "assistant", "Held.", token="tok-ahead", held=True, project_id=f,
                    tags=ftags, created_at=ahead)
                await ME.write_turn_row_by_token(
                    pool, E, "s-ahead-held", "assistant", "Rewritten.", token="tok-ahead", held=False,
                    row_id=rid_b, project_id=f, tags=ftags, created_at=ahead)
                ahead_rows = await attempt(pool.fetch(
                    "SELECT landed_at, created_at FROM entity_memory WHERE id = ANY($1::int[])", [rid_a, rid_b]))
                check("L a row never lands before it began: written, and written over, with a start ahead of the "
                      "clock, its landed_at is that start, and the list reads it",
                      not isinstance(ahead_rows, BaseException) and len(ahead_rows) == 2
                      and all(x["landed_at"] == x["created_at"] == ahead for x in ahead_rows)
                      and (await row(f))["last_landed_at"] == ahead, (ahead_rows, ahead))
            finally:
                ME._get_memory_embedding = real_embed
            o = await project(pool, "Stamps in metadata")
            stamped = [await turn(pool, o, "assistant", secs_ago=s, extra={**HOST, "landed_epoch": v})
                       for s, v in ((300, "soon"), (290, 1e30), (280, -5), (270, True), (260, {"at": 1}), (250, None))]
            later = await pool.fetchval("SELECT extract(epoch FROM now() + interval '10 minutes')::float8")
            stamped.append(await turn(pool, o, "assistant", secs_ago=240, extra={**HOST, "landed_epoch": later}))
            check("L a landed_epoch key in a row's metadata (what older writers stamped), sane or not, is never "
                  "read and never fails the list: landed_at is the only source, else the row's own time",
                  (await row(o))["last_landed_at"] == max(x["created_at"] for x in stamped), (await row(o)))

        async def waiting():
            a = await project(pool, "Waiting A", repo="o/r", config={"code_approval_changes": {"3": {"head": "c"}}})
            b = await project(pool, "Waiting B")
            await ticket(pool, {"app_id": "workbench", "project_id": a, "thread_id": None})
            await ticket(pool, {"project_id": str(a)})
            await ticket(pool, {"project_id": a, "thread_id": ""})
            await ticket(pool, {"project_id": a}, status="decided")
            await ticket(pool, {"project_id": a, "thread_id": "0f8b1c3e-2d4a-4b5c-9e6f-7a8b9c0d1e2f"})
            await ticket(pool, {"project_id": a}, eid=OTHER_E)
            await ticket(pool, {"project_id": b})
            await ticket(pool, None)
            rows = by_id(await P.list_projects(include_archived=True, limit=1000))
            polled = {pid: len((await TK.list_tickets_service(
                pool, E, status_filter="open", asked_in=f"project:{pid}"))["tickets"]) for pid in (a, b)}
            check("W ask cards: the project's open tickets, none decided, none a thread's, none of another "
                  "entity or project",
                  rows[a]["ask_cards_open"] == 3 and rows[b]["ask_cards_open"] == 1, (rows[a], rows[b]))
            check("W ...the same count the Workbench's card poll gets (GET /api/tickets?status=open"
                  "&asked_in=project:<id>)",
                  rows[a]["ask_cards_open"] == polled[a] and rows[b]["ask_cards_open"] == polled[b], polled)
            check("W no forge read yet: no approvals count, no checked time, waiting_on_you is the cards",
                  rows[a]["approvals_waiting"] is None and rows[a]["approvals_checked_at"] is None
                  and rows[a]["waiting_on_you"] == 3, rows[a])

            fake = FakeForge("o/r")
            fake.add("feature-x", "a")
            fake.add("feature-y", "b")
            fake.add("host/build", "c")          # changes asked at this version
            fake.add("old", "z", merged=True)

            @contextlib.asynccontextmanager
            async def _stub_forge(_pool, _eid, _pid):
                yield types.SimpleNamespace(on_machine=False, machine=None), fake.request, fake.repo, None
            real_forge = CA._forge
            CA._forge = _stub_forge
            try:
                listed = await CA.list_approvals(pool, E, a)
            finally:
                CA._forge = real_forge
            bench = sum(1 for r in listed["approvals"] if r["state"] == "waiting")
            r = await pool.fetchrow("SELECT approvals_waiting, approvals_checked_at FROM projects WHERE id=$1", a)
            check("W listing the Code Approvals stores the bench's own \"N waiting\" (changes asked and "
                  "merged ones are not waiting) with the time it was read",
                  bench == 2 and r["approvals_waiting"] == 2 and r["approvals_checked_at"] is not None,
                  (bench, dict(r)))
            row = by_id(await P.list_projects(include_archived=True, limit=1000))[a]
            check("W the list returns the copy and its time, and adds it to the cards",
                  row["approvals_waiting"] == 2 and row["approvals_checked_at"] == r["approvals_checked_at"]
                  and row["waiting_on_you"] == 5, row)

            # A save point moves the work branch: the version changes were asked
            # on is gone, so the Code Approval waits for the person again.
            fake.prs[3]["head"]["sha"] = "d"
            before = r["approvals_checked_at"]
            out = await PG._ensure_pr("o/r", "Waiting A", "phase 2: tabs", [], None,
                                      sess=forge_session(fake), pool=pool, project_id=a)
            r = await pool.fetchrow("SELECT approvals_waiting, approvals_checked_at FROM projects WHERE id=$1", a)
            check("W a save point that moves the Code Approval to a new version refreshes the count",
                  out.get("pr") == 3 and r["approvals_waiting"] == 3 and r["approvals_checked_at"] > before,
                  (out, dict(r)))

            c = await project(pool, "Opens one", repo="o/c")
            fresh = FakeForge("o/c")
            out = await PG._ensure_pr("o/c", "Opens one", "phase 1: start", [], None,
                                      sess=forge_session(fresh), pool=pool, project_id=c)
            opened = [k for k in fresh.calls if k[0] == "POST" and k[1].endswith("/pulls")]
            check("W the platform opening a Code Approval stores it as waiting",
                  len(opened) == 1 and out.get("pr") == 1
                  and await pool.fetchval("SELECT approvals_waiting FROM projects WHERE id=$1", c) == 1, out)

            async def _forge_of(_pool, _pid):
                return types.SimpleNamespace(repo="o/c", on_machine=False, machine=None)

            @contextlib.asynccontextmanager
            async def _open(_pool, _eid, _forge):
                yield forge_session(fresh)
            real_pf = (PF.project_forge, PF.open_forge)
            PF.project_forge, PF.open_forge = _forge_of, _open
            try:
                merged = await PG.merge_project_pr(pool, c)
            finally:
                PF.project_forge, PF.open_forge = real_pf
            check("W the Host's Auto-Approve merge takes it off the count",
                  merged.get("merged") is True
                  and await pool.fetchval("SELECT approvals_waiting FROM projects WHERE id=$1", c) == 0, merged)

            # The list reads only the database: one query, and no forge at all.
            def _no_forge(*a, **k):
                raise AssertionError("the projects list called the forge")
            seen_calls = len(fake.calls) + len(fresh.calls)
            real = (CA._forge, PF.project_forge, PF.open_forge)
            CA._forge = PF.project_forge = PF.open_forge = _no_forge
            counted = CountingPool(pool)
            bs._pool = counted
            try:
                await P.list_projects(include_archived=True, limit=1000)
                few = list(counted.calls)
                for i in range(40):
                    await project(pool, f"bulk {i}")
                counted.calls.clear()
                many = await P.list_projects(include_archived=True, limit=1000)
            finally:
                bs._pool = pool
                CA._forge, PF.project_forge, PF.open_forge = real
            check("W the list is one query, with a handful of projects and with forty more (no N+1)",
                  few == ["fetch"] and counted.calls == ["fetch"] and len(many["projects"]) > 40,
                  (few, counted.calls))
            check("W ...and reaches no forge", len(fake.calls) + len(fresh.calls) == seen_calls)

        async def totals():
            from mastercode.context_budget import running_spend

            def priced(usd, basis="list"):
                """The price stamp session.py writes on a Host reply
                (inference.estimate_turn_cost): the figure under the reply."""
                return {**HOST, "est_cost_usd": usd, "price_basis": basis}
            t1 = await project(pool, "Totals")
            sc1 = {"kind": "project", "ref": str(t1)}
            await turn(pool, t1, "user", secs_ago=1800)
            await turn(pool, t1, "assistant", secs_ago=1740, extra=HOST,
                       per_call=[{"in": 10, "cached": 0, "out": 1}])          # ledger, no spend yet
            await turn(pool, t1, "assistant", secs_ago=1680, extra={"gen_ms": 900})  # metrics only
            p1 = await turn(pool, t1, "assistant", secs_ago=1200, extra=priced(0.012),
                            sp=spend(3, 0.012, 100, 1000, 50, 200),
                            per_call=[{"in": 500, "cached": 0, "out": 50, "usd": 0.004}] * 3)
            # A 40-call turn: the row keeps 24 calls, whose sum is nothing
            # like the turn's; the spend record is totalled over all 40.
            await turn(pool, t1, "assistant", secs_ago=600, extra=priced(0.9),
                       sp=spend(40, 0.9, 4000, 40000, 0, 8000),
                       per_call=[{"in": 50000, "cached": 0, "cache_write": 0, "out": 5000, "usd": 1.0}] * 24)
            for note in ({"origin": "agent", "task_id": 7}, {"source_kind": "thread_marker"},
                         {"origin": "cancelled", "stopped_reason": "cancelled"}):
                await turn(pool, t1, "assistant", secs_ago=500, extra=note)
            await turn(pool, t1, "assistant", secs_ago=400, extra=priced(5.0), status="archived",
                       sp=spend(7, 5.0, 9, 9, 9, 9))
            t_other = await project(pool, "Other totals")
            await turn(pool, t_other, "assistant", secs_ago=300, extra=priced(1.0), sp=spend(5, 1.0, 1, 1, 1, 1))
            await turn(pool, None, "assistant", secs_ago=300, extra=priced(1.0), sp=spend(5, 1.0, 1, 1, 1, 1),
                       sctx={"kind": "app", "ref": "circles"})
            got = await running_spend(pool, E, sc1)
            check("T the turns' own spend records summed: calls, tokens by kind and the total",
                  got["turns_counted"] == 2 and got["calls"] == 43
                  and got["tokens"] == {"uncached": 4100, "cache_read": 41000, "cache_write": 50,
                                        "output": 8200}
                  and got["tokens_total"] == 53350, got)
            check("T ...never the 24-entry per-call list (it would read 1.32M tokens and $24)",
                  got["tokens_total"] < 100000 and got.get("est_usd") == 0.912, got)
            check("T Host replies from before the meter count as unknown, and the total starts at the first "
                  "counted turn",
                  got["turns_unknown"] == 2 and got["first_counted_at"] == p1["created_at"], got)
            check("T another project's turns, a Circles row, platform notes and an archived row never count "
                  "(the last-turn reader's scope), in tokens or in dollars",
                  got["turns_counted"] == 2 and got["turns_unknown"] == 2 and got.get("est_usd") == 0.912
                  and got.get("turns_priced") == 2, got)
            check("T the dollars are the replies' printed prices summed; replies from before the price stamp "
                  "leave them partial",
                  got.get("est_usd") == 0.912 and got.get("turns_unpriced") == 2
                  and got.get("usd_partial") is True, got)
            check("T the dollars are labelled an estimate",
                  str(got.get("usd_label") or "").startswith("ESTIMATE") and "spent_usd" not in got, got)

            # No dollar allowance on the thread (the default): the meter records
            # tokens only (spend.spent_usd null, allowance_basis "none"), and
            # every reply still prints its price.
            t2 = await project(pool, "No allowance")
            await turn(pool, t2, "assistant", secs_ago=100, extra=priced(0.5), sp=spend(2, None, 10, 0, 0, 5))
            await turn(pool, t2, "assistant", secs_ago=50, extra=priced(0.2), sp=spend(1, None, 20, 0, 0, 10))
            await turn(pool, t2, "assistant", secs_ago=45, extra=priced(None, "subscription"),
                       sp=spend(1, None, 1, 0, 0, 1))
            await turn(pool, t2, "assistant", secs_ago=44, extra=priced(None, "free"),
                       sp=spend(1, None, 1, 0, 0, 1))
            g2 = await running_spend(pool, E, {"kind": "project", "ref": str(t2)})
            check("T no dollar allowance on the thread: the page's dollars are the prices printed under the "
                  "replies, summed, beside the tokens from the spend records",
                  g2.get("est_usd") == 0.7 and g2.get("turns_priced") == 2 and g2["turns_counted"] == 4
                  and g2["tokens_total"] == 49, g2)
            check("T ...a subscription or free reply adds no dollars and leaves nothing partial",
                  g2.get("turns_unpriced") == 0 and g2.get("usd_partial") is False, g2)
            t3 = await project(pool, "Unpriced reply")
            await turn(pool, t3, "assistant", secs_ago=40, extra=priced(0.2), sp=spend(3, None, 10, 0, 0, 5))
            await turn(pool, t3, "assistant", secs_ago=39, extra=priced(None, "unpriced"),
                       sp=spend(1, None, 10, 0, 0, 5))
            g3 = await running_spend(pool, E, {"kind": "project", "ref": str(t3)})
            check("T a reply nobody could price leaves the dollars partial, and is counted",
                  g3.get("est_usd") == 0.2 and g3.get("turns_unpriced") == 1 and g3.get("usd_partial") is True, g3)
            t4 = await project(pool, "Unknown usage")
            await turn(pool, t4, "assistant", secs_ago=30,
                       extra={**priced(0.1, "partial"), "tokens_unknown_calls": 2},
                       sp=spend(3, None, 10, 0, 0, 5))
            g4 = await running_spend(pool, E, {"kind": "project", "ref": str(t4)})
            check("T a reply whose price covers only the calls that reported usage is summed and partial, "
                  "and the calls with no usage are counted",
                  g4.get("est_usd") == 0.1 and g4.get("usd_partial") is True and g4.get("turns_unpriced") == 0
                  and g4["calls_usage_unknown"] == 2, g4)
            t5 = await project(pool, "Quiet")
            g5 = await running_spend(pool, E, {"kind": "project", "ref": str(t5)})
            check("T a conversation with no turns: zeros, no dollars, nothing partial, no first turn",
                  g5["turns_counted"] == 0 and g5["turns_unknown"] == 0 and g5.get("est_usd") is None
                  and g5.get("usd_partial") is False and g5["first_counted_at"] is None
                  and g5["tokens_total"] == 0, g5)
            check("T no scope, no total (never an entity-wide sum)", await running_spend(pool, E, None) is None)
            th = {"kind": "thread", "ref": "6a1f0c2e-1111-4b5c-9e6f-7a8b9c0d1e2f"}
            await turn(pool, None, "assistant", secs_ago=20, extra=priced(0.3), sctx=th,
                       sp=spend(2, 0.3, 5, 5, 5, 5))
            await turn(pool, None, "assistant", secs_ago=10, sctx=th, sp=spend(9, 9.0, 9, 9, 9, 9),
                       extra={**priced(9.0), "app_id": "scheduled_task"})
            gt = await running_spend(pool, E, th)
            # A fired wake-up's hidden row counts in its thread's total: it is
            # what the run that thought there spent, and with no original
            # conversation every scheduled run is in a
            # topic, so leaving it out would show its cost on no page.
            check("T the same function sums a Circles thread by its descriptor, in that thread's scope, "
                  "a wake-up's hidden row that ran there included",
                  gt["turns_counted"] == 2 and abs((gt.get("est_usd") or 0) - 9.3) < 1e-9
                  and gt["scope"] == th, gt)

        async def page():
            from mastercode.context_budget import THREAD_STATE_PREFIX, running_spend
            from mastercode.runtime.close_turn import thread_state
            pid = await project(pool, "The book")
            scope = {"kind": "project", "ref": str(pid)}
            goal = await pool.fetchrow(
                "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata, created_at)"
                " VALUES ($1, 'Ship the book of pages', 'goal', 'active', $2::jsonb, now() - interval '25 min')"
                " RETURNING created_at", E, json.dumps({"source_context": scope}))
            await turn(pool, pid, "user", secs_ago=900)
            await turn(pool, pid, "assistant", secs_ago=880, extra={
                **HOST, "close_turn": {"handoff": {"next_step": "an older step"}}},
                sp=spend(2, 0.02, 10, 10, 0, 10))
            last = await turn(pool, pid, "assistant", secs_ago=600, extra={
                **HOST, "close_turn": {"handoff": {"next_step": "wire the tabs",
                                                   "open_work": ["seen mark", "totals"],
                                                   "identifiers": ["ABC-110", "  /srv/a b.txt"]}}},
                sp=spend(3, 0.03, 20, 20, 5, 20))
            await ticket(pool, {"project_id": pid})
            await pool.execute("UPDATE projects SET approvals_waiting = 2,"
                               " approvals_checked_at = now() - interval '1 min' WHERE id = $1", pid)
            await P.mark_project_seen(pid)
            for harness, tokens, src, cost, method, vendor in (
                    ("claude-code", 12000, "sdk", 5000, "billed", 250000),
                    ("codex", 0, "unknown", 3000, "compute", None),
                    ("kimi", 800, "stream", 0, "unpriced", None)):
                await pool.execute(
                    "INSERT INTO harness_usage (session_id, project_id, harness, tokens_total, token_source,"
                    " cost_microdollars, cost_method, ok, vendor_cost_microdollars, compute_s)"
                    " VALUES ('s1', $1, $2, $3, $4, $5, $6, true, $7, 12.5)",
                    pid, harness, tokens, src, cost, method, vendor)
            await pool.execute("INSERT INTO harness_usage (project_id, harness, tokens_total, token_source)"
                               " VALUES ($1, 'codex', 999, 'sdk')", pid + 100000)
            pg = await P.get_project_page(pid)
            check("P the page's shape",
                  set(pg) >= {"project_id", "where_this_stands", "totals", "engines", "seen_at",
                              "last_landed_at", "waiting_on_you", "ask_cards_open", "approvals_waiting",
                              "approvals_checked_at"} and pg["project_id"] == pid, sorted(pg))
            ws = pg["where_this_stands"] or {}
            check("P where this stands is the text thread_state() builds",
                  ws.get("text") and ws["text"] == await thread_state(pool, E, scope)
                  and ws["text"].startswith(THREAD_STATE_PREFIX), ws.get("text"))
            check("P ...and its parts are the goal row and the last handoff, each with when it was written",
                  ws.get("goal") == "Ship the book of pages" and ws.get("goal_at") == goal["created_at"]
                  and ws.get("next_step") == "wire the tabs" and ws.get("open_work") == ["seen mark", "totals"]
                  and ws.get("identifiers") == ["ABC-110", "  /srv/a b.txt"]
                  and ws.get("handoff_at") == last["created_at"], ws)
            check("P totals are running_spend over the Workbench thread's scope",
                  pg["totals"] == await running_spend(pool, E, scope)
                  and pg["totals"]["turns_counted"] == 2 and pg["totals"]["calls"] == 5, pg["totals"])
            proj = next(r for r in (await CMP.by_project(window="all"))["projects"] if r["project_id"] == pid)
            eng = {k: v for k, v in (pg["engines"] or {}).items() if k != "lanes"}
            compute_row = {k: v for k, v in proj.items() if k not in ("project_id", "project_name", "chat")}
            check("P engines are the Compute app's own row for the project (one pricing path)",
                  eng == compute_row and eng["runs"] == 3 and eng["tokens"] == 12800
                  and eng["billed_usd"] == 0.005 and eng["est_compute_usd"] == 0.003
                  and eng["vendor_est_usd"] == 0.25, (eng, compute_row))
            check("P ...labelled in the Compute app's words, the estimates as estimates",
                  (pg["engines"] or {}).get("lanes", {}).get("vendor_est_usd", "").startswith("ESTIMATE")
                  and "estimated" in pg["engines"]["lanes"].get("est_compute_usd", ""), pg["engines"])
            row = by_id(await P.list_projects(include_archived=True, limit=1000))[pid]
            check("P the seen mark and what is waiting are the list row's",
                  all(pg[k] == row[k] for k in ("seen_at", "last_landed_at", "waiting_on_you", "ask_cards_open",
                                                "approvals_waiting", "approvals_checked_at"))
                  and pg["waiting_on_you"] == 3 and pg["ask_cards_open"] == 1 and pg["approvals_waiting"] == 2,
                  {k: (pg[k], row[k]) for k in ("waiting_on_you", "ask_cards_open", "approvals_waiting")})
            other = await project(pool, "Not yours", eid=OTHER_E)
            for bad in (other, pid + 999999):
                try:
                    await P.get_project_page(bad)
                    check(f"P a project the entity does not own ({bad}) is refused", False)
                except HTTPException as e:
                    check(f"P a project the entity does not own ({'other entity' if bad == other else 'missing'})"
                          " is a 404", e.status_code == 404, e.status_code)
            from server.applications.api_contract import SCOPE_ATTR
            scopes = {}
            for r in P.router.routes:
                if r.path in ("/api/projects/{project_id}/page", "/api/projects/{project_id}/seen"):
                    calls = [d.call for d in r.dependant.dependencies]
                    scopes[(r.path, tuple(sorted(r.methods)))] = sorted(
                        getattr(c, SCOPE_ATTR) for c in calls if getattr(c, SCOPE_ATTR, None))
            check("P the page needs read:brain_memory and read:inference_log, not the store-grantable "
                  "read:projects; the seen mark needs write:projects",
                  scopes.get(("/api/projects/{project_id}/page", ("GET",)))
                  == ["read:brain_memory", "read:inference_log"]
                  and scopes.get(("/api/projects/{project_id}/seen", ("PUT",))) == ["write:projects"], scopes)

        async def failed_reads():
            from server.routes.project_forge import ForgeError
            f = await project(pool, "Failed reads", repo="o/f")
            await pool.execute("UPDATE projects SET approvals_waiting = 2,"
                               " approvals_checked_at = now() - interval '1 hour' WHERE id = $1", f)

            async def stored(pid):
                r = await pool.fetchrow(
                    "SELECT approvals_waiting, approvals_checked_at FROM projects WHERE id = $1", pid)
                return r["approvals_waiting"], r["approvals_checked_at"]
            before = await stored(f)
            for mode in ("raise", "page2", "empty"):
                bf = BrokenForge("o/f", broken=mode)
                for i in range(50 if mode == "page2" else 1):
                    bf.add(f"feature-{i}", f"s{i}")

                @contextlib.asynccontextmanager
                async def _stub(_pool, _eid, _pid, bf=bf):
                    yield types.SimpleNamespace(on_machine=False, machine=None), bf.request, bf.repo, None
                real_forge = CA._forge
                CA._forge = _stub
                raised = None
                try:
                    await CA.list_approvals(pool, E, f)
                except (HTTPException, ForgeError) as e:
                    raised = e
                finally:
                    CA._forge = real_forge
                check(f"F a failed forge read ({mode}) fails the listing, and the stored count and its "
                      "checked time stay as they were",
                      raised is not None and await stored(f) == before, (mode, raised, await stored(f)))
                n = await CA.refresh_waiting(pool, f, bf.request, "o/f")
                check(f"F ...and a refresh on it ({mode}) stores nothing",
                      n is None and await stored(f) == before, (mode, n, await stored(f)))

            # A save point and an Auto-Approve merge whose own forge calls answer
            # and whose count read after them fails: the work goes through, the
            # stored count is left as it was.
            g = await project(pool, "Failed refresh", repo="o/g")
            await pool.execute("UPDATE projects SET approvals_waiting = 1,"
                               " approvals_checked_at = now() - interval '1 hour' WHERE id = $1", g)
            g_before = await stored(g)
            sf = BrokenForge("o/g", break_after=lambda m, p: m == "PATCH", mode="empty")
            sf.add("host/build", "a")
            out = await PG._ensure_pr("o/g", "Failed refresh", "phase 1", [], None,
                                      sess=forge_session(sf), pool=pool, project_id=g)
            check("F a save point whose count read fails still returns its Code Approval, and the count stays",
                  out.get("pr") == 1 and await stored(g) == g_before, (out, await stored(g)))

            h = await project(pool, "Failed refresh after a merge", repo="o/h")
            await pool.execute("UPDATE projects SET approvals_waiting = 1,"
                               " approvals_checked_at = now() - interval '1 hour' WHERE id = $1", h)
            h_before = await stored(h)

            async def _forge_of(_pool, _pid):
                return types.SimpleNamespace(repo="o/h", on_machine=False, machine=None)
            mf = BrokenForge("o/h", break_after=lambda m, p: p.endswith("/merge"), mode="empty")
            mf.add("host/build", "a")

            @contextlib.asynccontextmanager
            async def _open(_pool, _eid, _forge):
                yield forge_session(mf)
            real_pf = (PF.project_forge, PF.open_forge)
            PF.project_forge, PF.open_forge = _forge_of, _open
            try:
                merged = await PG.merge_project_pr(pool, h)
            finally:
                PF.project_forge, PF.open_forge = real_pf
            check("F an Auto-Approve merge whose count read fails still reports the merge, and the count stays",
                  merged.get("merged") is True and await stored(h) == h_before, (merged, await stored(h)))

            # The bench's listing is still reading the forge when a save point
            # opens a second Code Approval and its refresh stores the newer
            # count: the listing's older view must not land on top of it.
            k = await project(pool, "Overtaken listing", repo="o/k")
            newer = FakeForge("o/k")
            newer.add("host/build", "n1")
            newer.add("feature-b", "n2")

            class Overtaken(FakeForge):
                raced = False

                async def request(self, method, path, params=None, json=None, **kw):  # noqa: A002
                    if (not self.raced and method == "GET" and path.endswith("/pulls")
                            and (params or {}).get("state", "open") == "open"):
                        self.raced = True
                        await asyncio.sleep(0.01)
                        await CA.refresh_waiting(pool, k, newer.request, "o/k")
                    return await super().request(method, path, params=params, json=json, **kw)
            old_view = Overtaken("o/k")

            @contextlib.asynccontextmanager
            async def _stub_k(_pool, _eid, _pid):
                yield types.SimpleNamespace(on_machine=False, machine=None), old_view.request, old_view.repo, None
            real_forge = CA._forge
            CA._forge = _stub_k
            try:
                await CA.list_approvals(pool, E, k)
            finally:
                CA._forge = real_forge
            check("F a listing overtaken by a save point's refresh leaves the refresh's newer count",
                  old_view.raced and (await stored(k))[0] == 2, (old_view.raced, await stored(k)))
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            m = await project(pool, "Read order", repo="o/m")
            t_new = _dt.now(_tz.utc)
            try:
                await CA.record_waiting(pool, m, 3, t_new)
                await CA.record_waiting(pool, m, 0, t_new - _td(seconds=5))
                kept_m = await stored(m)
                await CA.record_waiting(pool, m, 1, t_new + _td(seconds=5))
                later_m = await stored(m)
            except TypeError as e:
                kept_m = later_m = repr(e)
            check("F a count from a read that began earlier never replaces one from a later read; a later one does, "
                  "stamped with when its read began",
                  kept_m == (3, t_new) and later_m == (1, t_new + _td(seconds=5)), (kept_m, later_m))

        async def same_state():
            from mastercode.context_budget import THREAD_STATE_PREFIX
            from mastercode.runtime.close_turn import thread_state, thread_state_parts

            async def goal(sc, text, *, secs_ago=600, status="active", resolved=False, until=False, at=None):
                return await pool.fetchval(
                    "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata, created_at,"
                    " resolved_at, t_valid_until)"
                    " VALUES ($1, $2, 'goal', $3, $4::jsonb,"
                    " COALESCE($8::timestamptz, now() - make_interval(secs => $5)),"
                    " CASE WHEN $6 THEN now() END, CASE WHEN $7 THEN now() END) RETURNING id",
                    E, text, status, json.dumps({"source_context": sc}), float(secs_ago), resolved, until, at)

            async def said(sc, *, secs_ago, close=None, role="assistant", at=None, **more):
                """A turn row in scope `sc`: a Host reply with its close_turn
                signal (`close`), or without one when close is None."""
                md = {"source_kind": "chat_turn", "role": role, "source_context": sc, **HOST, **more}
                if close is not None:
                    md["close_turn"] = close
                pid = int(sc["ref"]) if sc["kind"] == "project" else None
                return await pool.fetchval(
                    "INSERT INTO entity_memory (entity_id, content, memory_type, status, project_id, metadata,"
                    " created_at) VALUES ($1, 'said', 'turn', 'active', $2, $3::jsonb,"
                    " COALESCE($5::timestamptz, now() - make_interval(secs => $4))) RETURNING id",
                    E, pid, json.dumps(md), float(secs_ago), at)

            async def scope_of(name):
                return {"kind": "project", "ref": str(await project(pool, name))}
            scopes = {}
            sc = scopes["everything"] = await scope_of("E everything")
            await goal(sc, "Ship the book of pages")
            await said(sc, role="user", secs_ago=500)
            await said(sc, secs_ago=400, close={"handoff": {
                "next_step": "wire the tabs", "open_work": ["seen mark", "totals", 3],
                "identifiers": ["ABC-110", "  /srv/a b.txt", "ü/ä.md"]}})
            sc = scopes["goal only"] = await scope_of("E goal only")
            await goal(sc, "Only a goal\nover two lines")
            sc = scopes["handoff only"] = await scope_of("E handoff only")
            await said(sc, secs_ago=300, close={"handoff": {"next_step": "just a step"}})
            sc = scopes["nothing"] = await scope_of("E nothing")
            await said(sc, role="user", secs_ago=300)
            await said(sc, secs_ago=200)
            sc = scopes["goals that are not the goal"] = await scope_of("E skipped goals")
            await goal(sc, "the live one", secs_ago=900)
            await goal(sc, "resolved", secs_ago=100, resolved=True)
            await goal(sc, "superseded", secs_ago=100, until=True)
            await goal(sc, "archived", secs_ago=100, status="archived")
            await goal(scopes["nothing"], "another thread's goal", secs_ago=5000, status="archived")
            sc = scopes["goal tie"] = await scope_of("E goal tie")
            tie = await pool.fetchval("SELECT now() - interval '10 min'")
            await goal(sc, "first of the tie", at=tie)
            await goal(sc, "second of the tie (the higher id)", at=tie)
            sc = scopes["a close_turn with no handoff leads"] = await scope_of("E empty close")
            await goal(sc, "goal stays")
            await said(sc, secs_ago=300, close={"handoff": {"next_step": "an older step"}})
            await said(sc, secs_ago=100, close={"type": {"name": "chat"}})
            th = {"kind": "thread", "ref": "7b2e1d3f-2222-4c5d-8e9f-0a1b2c3d4e5f"}
            sc = scopes["hidden lanes"] = th
            await said(sc, secs_ago=300, close={"handoff": {"next_step": "visible step"}})
            for lane in ("circles-replay", "circles-ab", "scheduled_task"):
                await said(sc, secs_ago=100, close={"handoff": {"next_step": f"hidden {lane}"}}, app_id=lane)
            sc = scopes["not a reply, or no signal"] = await scope_of("E not signals")
            await said(sc, secs_ago=300, close={"handoff": {"next_step": "kept"}})
            await said(sc, secs_ago=200)
            await said(sc, role="user", secs_ago=100, close={"handoff": {"next_step": "a user row"}})
            sc = scopes["odd values"] = await scope_of("E odd values")
            await goal(sc, "  spaced  goal ")
            await said(sc, secs_ago=100, close={"handoff": {
                "next_step": "", "open_work": "a string", "identifiers": [1, 2.5, True, None, "x"]}})
            sc = scopes["handoff tie"] = await scope_of("E handoff tie")
            await said(sc, secs_ago=0, at=tie, close={"handoff": {"next_step": "first of the tie"}})
            await said(sc, secs_ago=0, at=tie, close={"handoff": {"next_step": "second (the higher id)"}})
            sc = scopes["handoff stored as a JSON string"] = await scope_of("E string handoff")
            await said(sc, secs_ago=100, close={"handoff": json.dumps({"next_step": "inside a string"})})
            sc = scopes["the original conversation"] = {"kind": "app", "ref": "circles"}
            await goal(sc, "a Circles goal")
            await pool.execute(
                "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata)"
                " VALUES ($1, 'said', 'turn', 'active', $2::jsonb)", E,
                json.dumps({"role": "assistant", "source_context": sc, **HOST,
                            "close_turn": {"handoff": {"open_work": ["one"], "identifiers": ["id-1"]}}}))
            edge = {"no scope": None, "empty scope": {}, "no kind": {"kind": ""},
                    "no ref": {"kind": "project"}, "ref None": {"kind": "project", "ref": None}}

            async def _json_codec(conn):
                for t in ("json", "jsonb"):
                    await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
            decoding = await asyncpg.create_pool(dsn, min_size=1, max_size=2, init=_json_codec)
            try:
                differ, texts = [], {}
                for label_pool, p in (("the platform's pool (JSON as text)", pool),
                                      ("a pool that decodes JSON", decoding)):
                    for name, s in {**scopes, **edge}.items():
                        old = await _thread_state_main(p, E, s)
                        new = await thread_state(p, E, s)
                        parts = await thread_state_parts(p, E, s)
                        ptext = parts["text"] if isinstance(parts, dict) else parts
                        if old != new or ptext != new or (parts is None) != (s is None or not (s or {}).get("kind")):
                            differ.append((label_pool, name, old, new, ptext))
                        texts[(label_pool, name)] = new
            finally:
                await decoding.close()
            plain = "the platform's pool (JSON as text)"
            check(f"E thread_state() is main's text byte for byte on every scope ({len(texts)} reads: "
                  f"{len(scopes) + len(edge)} scopes on two pool codecs), and thread_state_parts' text is it too",
                  not differ, differ)
            check("E ...and the rows reached every branch: a full state, the newest live goal, the id tie, "
                  "an empty close_turn, the hidden lanes, a string handoff, nothing at all",
                  texts[(plain, "everything")] == (
                      THREAD_STATE_PREFIX + "\nGoal: Ship the book of pages\n"
                      "Next step (from the last turn): wire the tabs\nOpen work: seen mark; totals; 3\n"
                      "Identifiers: ABC-110,   /srv/a b.txt, ü/ä.md")
                  and "Goal: the live one" in texts[(plain, "goals that are not the goal")]
                  and "second of the tie" in texts[(plain, "goal tie")]
                  and texts[(plain, "a close_turn with no handoff leads")] == THREAD_STATE_PREFIX + "\nGoal: goal stays"
                  and "visible step" in texts[(plain, "hidden lanes")]
                  and "hidden" not in texts[(plain, "hidden lanes")]
                  and "kept" in texts[(plain, "not a reply, or no signal")]
                  and "second (the higher id)" in texts[(plain, "handoff tie")]
                  and texts[(plain, "nothing")] is None and texts[(plain, "no scope")] is None
                  and texts[(plain, "handoff stored as a JSON string")] is None
                  and "inside a string" in (texts[("a pool that decodes JSON", "handoff stored as a JSON string")]
                                            or ""),
                  {k[1]: v for k, v in texts.items() if k[0] == plain})

        await section("S. the seen mark", seen_mark)
        await section("L. last_landed_at", landed)
        await section("W. waiting on you", waiting)
        await section("F. a failed forge read never moves the stored count", failed_reads)
        await section("T. the running totals", totals)
        await section("P. the page route", page)
        await section("E. thread_state() unchanged", same_state)
    finally:
        CA.host_origin = real_origin
        bs._pool, bs._entity_id = saved
        await pool.close()


async def main(dsn):
    await section("M. the columns", lambda: migration_checks(dsn))
    await behaviour(dsn)
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
