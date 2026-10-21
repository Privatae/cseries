#!/usr/bin/env python3
"""Smoke gate: Circles threads.

WHAT THIS RATCHETS:
  a. TURN ZERO — a new thread's scope has 0 turns (the count FRAME renders,
     memory_engine.count_scope_turns → _turn_row_where) while the legacy
     app:circles scope's count is unchanged.
  b. NO CROSS-THREAD RECALL — recall_recent_turns on thread A never returns a
     thread-B turn. (recall_recent_turns is what session.py:3202 calls with
     the descriptor derive_source_context builds from sess.thread_id.)
  c. THE LEGACY SCOPE IS UNCHANGED — rows tagged the OLD way
     (metadata.app_id='circles', no source_context) AND the new way
     ({kind:app, ref:circles}) are both returned, and rows tagged
     {kind:thread} are NOT. This is the regression on the app_id OR clause at
     memory_engine._turn_row_where:1123-1130.
  d. A BOUNDED READ STAYS IN ITS THREAD — recall_recent_turns(...,
     before=<a turn's created_at>) on a thread's descriptor sees only that
     thread's turns before it, and the thread read (_read_self_thread) only
     the thread. (The Regenerate and A/B routes that used the bound left
     Circles with the rail.)
  e. SPLIT — circles_threads.split_thread re-tags from from_turn_id onward,
     leaves earlier rows, sets origin='split' + parent_thread_id, writes
     exactly one circles_thread_links row reason='split', and strips
     metadata.app_id from the moved rows.
  f. CLEAR — clearing one thread archives only that thread's turn rows.
  x. DELETE — deleting a thread archives its turn rows AND every row learned
     in it (summary, goal, preference), and nothing of any other thread or of
     the original conversation; it leaves the list, a read and a send; recall's
     filter and the thread's open-goal read stop returning what it learned;
     what it retired comes back unless a later correction moved on.
  + list_threads puts the legacy entry first with a turn count from the
    scope, and set_thread_topic writes circles_threads.topic.

Runs in-container against the live schema; seeds a synthetic entity; always
cleans up.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")
# Outside the pod the checkout is the app: the same three folders, and this
# one for kernel_schema (the throwaway database, below).
_HERE = pathlib.Path(__file__).resolve().parent
if not pathlib.Path("/app/brain").is_dir():
    for _p in (_HERE, _HERE.parent / "brain", _HERE.parent / "server", _HERE.parent):
        sys.path.insert(0, str(_p))

# A test server named the way the *_pg gates name one (TEST_PG_DSN /
# PEER_TEST_DSN / TEST_DSN): a fresh clone of the kernel's schema on it.
# Else the pod's own database (PGHOST, or the pod's socket), else a
# throwaway cseries-postgres through docker.
# The pod is the image's /app tree, not a socket file: a machine that once ran
# a database container can keep a dead /var/run/postgresql/.s.PGSQL.5432
# (a development machine may have one), and taking it for the pod's database
# refused every connect
# when the full gate ran this outside any pod.
SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
IN_POD = bool(os.environ.get("PGHOST")) or (
    pathlib.Path("/app/brain").is_dir()
    and pathlib.Path("/var/run/postgresql/.s.PGSQL.5432").exists())

TEST_EID = 99326  # synthetic; never a real entity
LEGACY_SRC = {"kind": "app", "ref": "circles"}
SESSION = "circles-threads-gate-session"

# Long enough that a truncated read is visible, short enough to stay readable.
PAD = "y" * 120


async def _seed_legacy_old(pool, n: int) -> list[int]:
    """Rows tagged the way Circles tagged them BEFORE the unified descriptor:
    metadata.app_id='circles' and no source_context at all. These exist in
    every live host and are the reason _turn_row_where's app branch carries
    the `OR metadata->>'app_id'` clause (memory_engine.py:1126-1130)."""
    out = []
    for i in range(n):
        md = {"role": "user" if i % 2 == 0 else "assistant",
              "app_id": "circles"}
        out.append(await pool.fetchval(
            """
            INSERT INTO entity_memory
              (entity_id, content, memory_type, importance, status,
               source_session, metadata)
            VALUES ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb)
            RETURNING id
            """,
            TEST_EID, f"legacy-old {i} {PAD}", SESSION, json.dumps(md)))
    return out


async def _seed_via_writer(pool, n: int, *, thread_id: str | None,
                           label: str, session: str = SESSION) -> list[int]:
    """Rows written through the REAL writer on the REAL tag shape.

    memory_engine.write_turn_row is what session.py:2660 / :4491 call, and
    `tags` is session.py's own `_turn_tags` (session.py:2608-2612): app_id as
    the caller declared it — circles.py:1705 passes app_id='circles' for a
    threaded send too — plus the descriptor derive_source_context returns for
    that thread. Seeding by hand here would test a shape nothing writes.
    """
    from mastercode.circles_threads import descriptor_for
    from mastercode.memory_engine import write_turn_row
    src = descriptor_for(thread_id)
    out = []
    for i in range(n):
        rid = await write_turn_row(
            pool, TEST_EID, session,
            "user" if i % 2 == 0 else "assistant",
            f"{label} {i} {PAD}",
            tags={"app_id": "circles", "source_context": src},
        )
        out.append(rid)
    return out


async def main(dsn: str | None = None) -> int:
    import asyncpg
    # PGHOST lets the same gate run against a throwaway cluster outside the pod.
    if dsn:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    else:
        pool = await asyncpg.create_pool(
            host=os.environ.get("PGHOST", "/var/run/postgresql"), user="entity",
            database="hostmem", min_size=1, max_size=3)
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [circles-threads] OK {name}")
        else:
            failures.append(name)
            print(f"  [circles-threads] FAIL {name} {detail}")

    try:
        from mastercode import circles_threads as ct
        from mastercode.memory_engine import (count_scope_turns,
                                              recall_recent_turns,
                                              _turn_row_where)

        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) "
            "VALUES ($1, 'CirclesThreadsGate', 'standard') "
            "ON CONFLICT (id) DO NOTHING", TEST_EID)
        await pool.execute(
            "DELETE FROM circles_thread_links WHERE entity_id=$1", TEST_EID)
        await pool.execute(
            "DELETE FROM circles_threads WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)

        # ── the legacy scope, seeded both ways ────────────────────────────
        old_ids = await _seed_legacy_old(pool, 2)
        new_ids = await _seed_via_writer(pool, 2, thread_id=None,
                                         label="legacy-new")
        check("legacy seeding landed (2 old-tagged + 2 descriptor-tagged)",
              len(old_ids) == 2 and all(old_ids)
              and len(new_ids) == 2 and all(new_ids),
              f"old={old_ids} new={new_ids}")

        legacy_before = await count_scope_turns(
            pool, TEST_EID, "", source_context=LEGACY_SRC)
        check("c. legacy app:circles scope returns BOTH tag shapes",
              legacy_before["count"] == 4, str(legacy_before))

        # ── a. turn zero ──────────────────────────────────────────────────
        thread_a = await ct.create_thread(pool, TEST_EID, title="Thread A")
        thread_b = await ct.create_thread(pool, TEST_EID, title="Thread B")
        a_id, b_id = thread_a["id"], thread_b["id"]
        check("create_thread returns a row with origin='user', no parent",
              thread_a["origin"] == "user"
              and thread_a["parent_thread_id"] is None
              and thread_a["turn_count"] == 0, json.dumps(thread_a))

        # Data-layer gate: get_thread with the UPPERCASE, non-canonical
        # form of a created id must return the CANONICAL lowercase id — this
        # is what _thread_id_from_body (circles.py) relies on to avoid
        # writing turns under a non-canonical ref that later reads (keyed on
        # str(row["id"])) would never match.
        upper_lookup = await ct.get_thread(pool, TEST_EID, a_id.upper())
        check("get_thread(uppercase id) returns the canonical lowercase id",
              upper_lookup is not None and upper_lookup["id"] == a_id.lower()
              and upper_lookup["id"] == a_id,
              f"upper={a_id.upper()} -> {upper_lookup}")

        zero = await count_scope_turns(
            pool, TEST_EID, "", source_context=ct.descriptor_for(a_id))
        check("a. a NEW thread's FRAME turn count is 0",
              zero["count"] == 0, str(zero))
        legacy_now = await count_scope_turns(
            pool, TEST_EID, "", source_context=LEGACY_SRC)
        check("a. creating a thread leaves the legacy count unchanged",
              legacy_now["count"] == legacy_before["count"],
              f"{legacy_before['count']} -> {legacy_now['count']}")

        # ── turns inside the two threads ──────────────────────────────────
        a_ids = await _seed_via_writer(pool, 2, thread_id=a_id, label="thread-A")
        b_ids = await _seed_via_writer(pool, 2, thread_id=b_id, label="thread-B")
        check("thread seeding landed",
              len(a_ids) == 2 and all(a_ids) and len(b_ids) == 2 and all(b_ids),
              f"a={a_ids} b={b_ids}")

        a_count = await count_scope_turns(
            pool, TEST_EID, "", source_context=ct.descriptor_for(a_id))
        check("a. the thread's own count is its own turns only",
              a_count["count"] == 2, str(a_count))

        # ── c. THE REGRESSION: thread turns must not answer app:circles ──
        legacy_after = await count_scope_turns(
            pool, TEST_EID, "", source_context=LEGACY_SRC)
        check("c. legacy scope count is UNCHANGED by thread turns "
              "(the app_id OR clause must not match {kind:thread} rows)",
              legacy_after["count"] == legacy_before["count"],
              f"{legacy_before['count']} -> {legacy_after['count']}")

        legacy_turns = await recall_recent_turns(
            pool, TEST_EID, SESSION, limit=50, source_context=LEGACY_SRC)
        legacy_text = [t.get("content") or "" for t in legacy_turns]
        check("c. legacy recall returns the OLD-tagged rows",
              sum(1 for c in legacy_text if c.startswith("legacy-old")) == 2,
              str(legacy_text))
        check("c. legacy recall returns the descriptor-tagged rows",
              sum(1 for c in legacy_text if c.startswith("legacy-new")) == 2,
              str(legacy_text))
        check("c. legacy recall returns NO thread-tagged rows",
              not any(c.startswith("thread-") for c in legacy_text),
              str(legacy_text))

        # ── b. no cross-thread recall ─────────────────────────────────────
        a_turns = await recall_recent_turns(
            pool, TEST_EID, SESSION, limit=50,
            source_context=ct.descriptor_for(a_id))
        a_text = [t.get("content") or "" for t in a_turns]
        check("b. recall on thread A returns thread A's turns",
              sum(1 for c in a_text if c.startswith("thread-A")) == 2,
              str(a_text))
        check("b. recall on thread A returns NO thread-B turn",
              not any(c.startswith("thread-B") for c in a_text), str(a_text))
        check("b. recall on thread A returns NO legacy turn",
              not any(c.startswith("legacy-") for c in a_text), str(a_text))

        # ── d. a bounded read inside a thread ────────────────────────────
        # recall_recent_turns with `before` on the thread descriptor: the
        # last A turn must be excluded and no B turn may appear.
        cutoff = await pool.fetchval(
            "SELECT created_at FROM entity_memory WHERE id=$1", a_ids[-1])
        replay_hist = await recall_recent_turns(
            pool, TEST_EID, SESSION, limit=50,
            source_context=ct.descriptor_for(a_id), before=cutoff)
        rep_text = [t.get("content") or "" for t in replay_hist]
        check("d. replay history inside thread A excludes the target turn",
              not any(c.startswith("thread-A 1") for c in rep_text),
              str(rep_text))
        check("d. replay history inside thread A sees only thread A",
              rep_text and all(c.startswith("thread-A") for c in rep_text),
              str(rep_text))

        # The route function itself, when it can be imported. It reads
        # brain_service's process globals, so they are set for the call.
        try:
            # Outside the pod server.main cannot load (it serves /app's
            # dashboard folders), so a stub stands in for it, as in
            # test_pending_words_pg.py; the pod and the smoke container load
            # the real one.
            if not pathlib.Path("/app/dashboard/base").is_dir() and "server.main" not in sys.modules:
                import types as _types
                import server as _srvpkg
                _stub = _types.ModuleType("server.main")
                sys.modules["server.main"] = _stub
                _srvpkg.main = _stub
            # The app module first, the way uvicorn loads it: circles.py
            # imports server.main, and server.main includes circles' router,
            # so importing the route module directly trips that cycle on
            # every host — the NOTE below fired in the smoke container too,
            # and the route-level checks silently ran nowhere.
            import server.main  # noqa: F401
            from server import brain_service
            from server.routes import circles as circles_routes
        except Exception as _imp:  # noqa: BLE001
            circles_routes = None
            _imp_detail = f"{type(_imp).__name__}: {_imp}"
        else:
            _imp_detail = ""
        # A FAILING check, not a NOTE. This gate degraded to data-layer-only
        # exactly when an import regression is what needs catching: the route
        # module imports server.main, which includes circles' own router, so
        # the cycle this trips is the one a real boot trips. The runner
        # creates the dashboard dirs server.main needs.
        check("route module importable (route-level checks d/f/9 run)",
              circles_routes is not None, _imp_detail)
        # A hidden-lane row: offline_task_runner writes a fired Circles
        # timer's reply with app_id='scheduled_task' and history_context
        # 'circles', so the row carries an app:circles DESCRIPTOR but a
        # foreign app_id (measured live: a Host holds 4). Recall
        # sees it; the original conversation never rendered it, and must not
        # start to. Seeded through the real writer with that exact tag shape.
        from mastercode.memory_engine import write_turn_row as _wtr
        hidden_id = await _wtr(
            pool, TEST_EID, SESSION, "assistant", f"hidden-lane 0 {PAD}",
            tags={"app_id": "scheduled_task", "source_context": LEGACY_SRC})
        # Two SUPERSEDED turn rows: consolidation set t_valid_until on turn
        # rows (consolidate_new and _merge_duplicates, before their guards),
        # and the conversation has always shown them while
        # recall never did — measured live: a Host holds 147,
        # a Host holds 3.
        superseded_ids = []
        for i in range(2):
            superseded_ids.append(await pool.fetchval(
                """
                INSERT INTO entity_memory
                  (entity_id, content, memory_type, importance, status,
                   source_session, metadata, t_valid_until)
                VALUES ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb,
                        NOW() - interval '1 hour')
                RETURNING id
                """,
                TEST_EID, f"legacy-superseded {i} {PAD}", SESSION,
                json.dumps({"role": "user" if i == 0 else "assistant",
                            "app_id": "circles",
                            "source_context": LEGACY_SRC})))
        legacy_before = await count_scope_turns(
            pool, TEST_EID, "", source_context=LEGACY_SRC)
        check("recall scope: hidden-lane row IN (descriptor match), superseded "
              "rows OUT (t_valid_until) -> 5",
              legacy_before["count"] == 5, str(legacy_before))
        ui_args: list = [TEST_EID]
        ui_where = ct.legacy_conversation_where(ui_args)
        ui_ids = [r["id"] for r in await pool.fetch(
            f"SELECT id FROM entity_memory WHERE {' AND '.join(ui_where)}",
            *ui_args)]
        ui_count = len(ui_ids)
        check("c. legacy_conversation_where shows the 4 visible rows AND the 2 "
              "superseded ones, NOT the hidden-lane row -> 6",
              ui_count == 6 and hidden_id not in ui_ids
              and all(s in ui_ids for s in superseded_ids),
              f"{ui_count} rows, hidden={hidden_id in ui_ids}, "
              f"superseded_in={[s in ui_ids for s in superseded_ids]}")

        if circles_routes is not None:
            _prev_pool = getattr(brain_service, "_pool", None)
            _prev_eid = getattr(brain_service, "_entity_id", None)
            brain_service._pool = pool
            brain_service._entity_id = TEST_EID
            try:
                # _normalize_turn_rows returns UI messages keyed `body`,
                # not entity_memory's `content`.
                rows_a = await circles_routes._read_self_thread(200, a_id)
                check("d. _read_self_thread on a thread reads only it",
                      bool(rows_a) and all(
                          (r.get("body") or "").startswith("thread-A")
                          for r in rows_a),
                      f"{len(rows_a or [])} rows: "
                      f"{[(r.get('body') or '')[:20] for r in (rows_a or [])]}")
                # There is no original conversation: the
                # legacy ids read as an empty page, though the legacy rows
                # above are still in the database (ui_count proves it).
                rows_l = await circles_routes._read_self_thread(200, None)
                rows_o = await circles_routes._read_self_thread(200, "original")
                check("d. _read_self_thread on the legacy ids reads an empty page, "
                      "though the legacy rows exist",
                      rows_l == [] and rows_o == [] and ui_count > 0,
                      f"none={len(rows_l or [])} original={len(rows_o or [])} legacy rows={ui_count}")
            finally:
                brain_service._pool = _prev_pool
                brain_service._entity_id = _prev_eid

        # ── list_threads + set_thread_topic ───────────────────────────────
        listed = await ct.list_threads(pool, TEST_EID)
        check("list_threads lists no original conversation: every entry is a thread row",
              listed and all(t["id"] != ct.LEGACY_THREAD_ID and t.get("origin") != "legacy"
                             and ct._is_uuid(t["id"]) for t in listed),
              json.dumps([t["id"] for t in listed]) if listed else "[]")
        check("...though the legacy scope still holds rows (it is listed nowhere)",
              ui_count > 0 and legacy_before["count"] > 0,
              f"ui={ui_count} recall={legacy_before['count']}")
        by_id = {t["id"]: t for t in listed}
        check("list_threads carries each thread's own turn count",
              by_id.get(a_id, {}).get("turn_count") == 2
              and by_id.get(b_id, {}).get("turn_count") == 2,
              json.dumps([by_id.get(a_id), by_id.get(b_id)]))
        # list_threads counts every thread in ONE grouped query now. That
        # query is the one hand-written scope clause in circles_threads, so
        # this pins it to _turn_count (shown_where → _turn_row_where) per
        # thread: a drift in either direction fails here instead of quietly
        # mis-counting a list.
        grouped = await ct._turn_counts_by_thread(pool, TEST_EID)
        per_thread = {t: await ct._turn_count(pool, TEST_EID, t)
                      for t in (a_id, b_id)}
        check("list_threads' grouped count agrees with _turn_count per thread",
              all(grouped.get(t, 0) == n for t, n in per_thread.items()),
              f"grouped={grouped} per_thread={per_thread}")

        await ct.set_thread_topic(pool, TEST_EID, b_id, "billing")
        b_row = await ct.get_thread(pool, TEST_EID, b_id)
        check("set_thread_topic writes circles_threads.topic",
              b_row is not None and b_row["topic"] == "billing",
              json.dumps(b_row))
        check("set_thread_topic does NOT overwrite a user's title",
              b_row is not None and b_row["title"] == "Thread B",
              json.dumps(b_row))
        untitled = await ct.create_thread(pool, TEST_EID)
        await ct.set_thread_topic(pool, TEST_EID, untitled["id"], "invoices")
        unt_row = await ct.get_thread(pool, TEST_EID, untitled["id"])
        check("set_thread_topic fills an ABSENT title from the topic",
              unt_row is not None and unt_row["title"] == "invoices",
              json.dumps(unt_row))
        await ct.set_thread_topic(pool, TEST_EID, ct.LEGACY_THREAD_ID, "nope")
        check("set_thread_topic on the legacy sentinel is a no-op",
              (await pool.fetchval(
                  "SELECT count(*) FROM circles_threads "
                  "WHERE entity_id=$1 AND topic='nope'", TEST_EID)) == 0)

        # ── e. split ──────────────────────────────────────────────────────
        thread_c = await ct.create_thread(pool, TEST_EID, title="Thread C")
        c_id = thread_c["id"]
        c_ids = await _seed_via_writer(pool, 4, thread_id=c_id, label="thread-C")
        split_at = c_ids[2]
        # A regenerate's row inside C, after the split point: tagged
        # app_id 'circles-replay' with C's descriptor (circles.py replay
        # path). The read hides it; Split must leave it in C and must not
        # count it, and it must stay hidden in whichever thread it is in.
        from mastercode.memory_engine import write_turn_row as _wtr2
        replay_c = await _wtr2(
            pool, TEST_EID, SESSION, "user", f"regen-in-C {PAD}",
            tags={"app_id": "circles-replay",
                  "source_context": ct.descriptor_for(c_id)})
        new_thread = await ct.split_thread(
            pool, TEST_EID, c_id, from_turn_id=split_at, title="Thread D")
        d_id = new_thread["id"]
        replay_row = await pool.fetchrow(
            "SELECT metadata->'source_context'->>'ref' AS ref, "
            "       metadata->>'app_id' AS app_id FROM entity_memory WHERE id=$1",
            replay_c)
        check("e. a regenerate's row is NOT moved by Split (stays in C, tag "
              "intact)",
              replay_row is not None and replay_row["ref"] == c_id
              and replay_row["app_id"] == "circles-replay",
              str(dict(replay_row) if replay_row else None))
        marker_moved = await pool.fetchval(
            "SELECT (metadata->'marker'->>'moved_turns')::int FROM entity_memory"
            " WHERE entity_id=$1 AND metadata->>'source_kind'='thread_marker'"
            "   AND metadata->'marker'->>'thread_id'=$2", TEST_EID, d_id)
        check("e. the marker counts only the VISIBLE rows moved (2, not the "
              "regenerate's row)",
              marker_moved == 2, str(marker_moved))
        listed_after = {t["id"]: t for t in await ct.list_threads(pool, TEST_EID)}
        check("e. list_threads counts what each thread SHOWS (D=2; C=2 visible "
              "+ marker, never the regenerate's row)",
              listed_after.get(d_id, {}).get("turn_count") == 2
              and listed_after.get(c_id, {}).get("turn_count") == 3,
              f"D={listed_after.get(d_id, {}).get('turn_count')} "
              f"C={listed_after.get(c_id, {}).get('turn_count')}")
        # The marker anchor uses the same rows the read renders: asked for the
        # first row at/after the regenerate row's own timestamp, it must skip
        # that hidden row and land on the next visible one (the split marker).
        replay_at = await pool.fetchval(
            "SELECT created_at FROM entity_memory WHERE id=$1", replay_c)
        anchor = await ct._first_turn_at_or_after(pool, TEST_EID, c_id, replay_at)
        check("e. the marker anchor never lands on a regenerate's hidden row",
              anchor is not None and anchor != replay_c,
              f"anchor={anchor} replay={replay_c}")

        # The cut is a moment, not an id. Two rows whose id order is the
        # reverse of their created_at order (the insert race write_turn_row
        # documents): the later-said row gets the SMALLER id. Splitting from
        # the earlier-said row must move both; an `id >=` cut would leave the
        # later-said one behind.
        from datetime import datetime, timedelta, timezone
        t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
        thread_h = await ct.create_thread(pool, TEST_EID, title="Thread H")
        h_id = thread_h["id"]
        h_later_said = await _wtr2(
            pool, TEST_EID, SESSION, "assistant", f"thread-H said-second {PAD}",
            tags={"app_id": "circles", "source_context": ct.descriptor_for(h_id)},
            created_at=t0 + timedelta(seconds=2))
        h_first_said = await _wtr2(
            pool, TEST_EID, SESSION, "user", f"thread-H said-first {PAD}",
            tags={"app_id": "circles", "source_context": ct.descriptor_for(h_id)},
            created_at=t0)
        check("e. fixture: the later-said row holds the SMALLER id",
              h_later_said < h_first_said, f"{h_later_said} < {h_first_said}")
        h_new = await ct.split_thread(
            pool, TEST_EID, h_id, from_turn_id=h_first_said, title="Thread I")
        i_args: list = [TEST_EID]
        i_where = ct.shown_where(i_args, h_new["id"])
        i_ids = {r["id"] for r in await pool.fetch(
            f"SELECT id FROM entity_memory WHERE {' AND '.join(i_where)}", *i_args)}
        check("e. splitting from the earlier-said row moves BOTH rows of the "
              "exchange (time cut, not id cut)",
              {h_later_said, h_first_said} <= i_ids, f"moved={sorted(i_ids)}")

        # A from_turn_id that is not a row of this conversation is refused
        # before any thread is created.
        threads_before = await pool.fetchval(
            "SELECT count(*) FROM circles_threads WHERE entity_id=$1", TEST_EID)
        try:
            await ct.split_thread(pool, TEST_EID, c_id, from_turn_id=2147483000,
                                  title="never")
            bad_split = "no error"
        except ValueError as exc:
            bad_split = str(exc)
        threads_after = await pool.fetchval(
            "SELECT count(*) FROM circles_threads WHERE entity_id=$1", TEST_EID)
        check("e. Split from a turn that is not this conversation's raises "
              "ValueError and creates NO thread",
              bad_split != "no error" and threads_after == threads_before,
              f"{bad_split!r} threads {threads_before}->{threads_after}")
        try:
            await ct.split_thread(pool, TEST_EID, d_id, from_turn_id=c_ids[0],
                                  title="never")
            cross = "no error"
        except ValueError as exc:
            cross = str(exc)
        check("e. Split from a row that belongs to a DIFFERENT thread is refused",
              cross != "no error", repr(cross))
        try:
            await ct.split_thread(pool, TEST_EID, c_id, from_turn_id=replay_c,
                                  title="never")
            hidden_anchor = "no error"
        except ValueError as exc:
            hidden_anchor = str(exc)
        check("e. Split anchored on a regenerate's hidden row is refused (the "
              "anchor must be a rendered row)",
              hidden_anchor == "turn not found in this conversation",
              repr(hidden_anchor))

        # A split anchored on C's own trailing marker has nothing after it to
        # move: refused, and no thread is created (the empty-thread case).
        c_marker_id = await pool.fetchval(
            "SELECT id FROM entity_memory WHERE entity_id=$1 "
            "  AND metadata->>'source_kind'='thread_marker' "
            "  AND metadata->'marker'->>'thread_id'=$2", TEST_EID, d_id)
        threads_before2 = await pool.fetchval(
            "SELECT count(*) FROM circles_threads WHERE entity_id=$1", TEST_EID)
        try:
            await ct.split_thread(pool, TEST_EID, c_id, from_turn_id=c_marker_id,
                                  title="never")
            trailing = "no error"
        except ValueError as exc:
            trailing = str(exc)
        threads_after2 = await pool.fetchval(
            "SELECT count(*) FROM circles_threads WHERE entity_id=$1", TEST_EID)
        check("e. Split anchored on a trailing marker is refused ('nothing to "
              "move') and creates NO thread",
              trailing == "nothing to move" and threads_after2 == threads_before2,
              f"{trailing!r} threads {threads_before2}->{threads_after2}")

        c_rows = await recall_recent_turns(
            pool, TEST_EID, SESSION, limit=50,
            source_context=ct.descriptor_for(c_id))
        c_text = sorted((r.get("content") or "")[:10] for r in c_rows)
        d_rows = await recall_recent_turns(
            pool, TEST_EID, SESSION, limit=50,
            source_context=ct.descriptor_for(d_id))
        d_text = sorted((r.get("content") or "")[:10] for r in d_rows)
        check("e. split MOVES the rows from from_turn_id onward",
              d_text == ["thread-C 2", "thread-C 3"], str(d_text))
        check("e. split LEAVES the rows before from_turn_id",
              [t for t in c_text if t.startswith("thread-C")]
              == ["thread-C 0", "thread-C 1"], str(c_text))
        check("e. the split leaves a marker turn behind in the source",
              any(t.startswith("→ Moved") for t in c_text), str(c_text))

        d_row = await ct.get_thread(pool, TEST_EID, d_id)
        check("e. the new thread has origin='split' and parent_thread_id",
              d_row["origin"] == "split" and d_row["parent_thread_id"] == c_id,
              json.dumps(d_row))

        links = await pool.fetch(
            "SELECT from_thread, to_thread, reason FROM circles_thread_links "
            " WHERE entity_id=$1 AND reason='split' AND from_thread=$2::uuid",
            TEST_EID, c_id)
        check("e. split writes EXACTLY ONE links row reason='split'",
              len(links) == 1
              and str(links[0]["from_thread"]) == c_id
              and str(links[0]["to_thread"]) == d_id,
              str([(str(r["from_thread"]), str(r["to_thread"]), r["reason"])
                   for r in links]))

        moved_app = await pool.fetch(
            "SELECT id, metadata->>'app_id' AS app_id, "
            "       metadata->>'split_prior_app_id' AS prior "
            "  FROM entity_memory "
            " WHERE entity_id=$1 AND id = ANY($2::int[])",
            TEST_EID, [c_ids[2], c_ids[3]])
        # A thread source keeps the tag: thread rows are matched by descriptor
        # only, and the tag is what hides a regenerate's rows. The strip is
        # load-bearing for a LEGACY source only — asserted in block g.
        check("e. rows moved from a THREAD keep metadata.app_id (no strip)",
              moved_app and all(r["app_id"] == "circles" for r in moved_app)
              and all(r["prior"] is None for r in moved_app),
              str([(r["id"], r["app_id"], r["prior"]) for r in moved_app]))
        legacy_post_split = await count_scope_turns(
            pool, TEST_EID, "", source_context=LEGACY_SRC)
        check("e. a split never changes the legacy scope's count",
              legacy_post_split["count"] == legacy_before["count"],
              f"{legacy_before['count']} -> {legacy_post_split['count']}")

        # ── e2. a split has no rolling summary to retire ──────────────────
        # No summary writer ships (migrations/v241 retired it and archived the
        # live rows): what falls out of a thread's window rides as its own
        # close_turn notes (evicted_record), so a split leaves nothing stale.
        check("e2. no rolling summary writer ships, so a split has none to retire",
              not (pathlib.Path(__file__).resolve().parents[1]
                   / "brain/mastercode/thread_consolidator.py").exists())

        # ── e3. split is ONE transaction ──────────────────────────────────
        # A failure AFTER the thread row is created must leave nothing. The
        # trailing-marker case raises before create_thread, so it cannot
        # prove that; the concurrent-split race can: the pre-flight count
        # sees rows, then the move UPDATE finds none. Drive that by handing
        # split_thread a pool whose connection answers the move UPDATE with
        # zero rows, and assert the raise rolled the thread row back.
        class _RaceConn:
            def __init__(self, conn):
                self._c = conn

            def __getattr__(self, name):
                return getattr(self._c, name)

            async def fetch(self, sql, *args):
                if "UPDATE entity_memory" in sql and "split_from" in sql:
                    return []
                return await self._c.fetch(sql, *args)

        class _RacePool:
            def __init__(self, real):
                self._p = real

            def __getattr__(self, name):
                return getattr(self._p, name)

            def acquire(self):
                real = self._p

                class _Ctx:
                    async def __aenter__(self_inner):
                        self_inner._cm = real.acquire()
                        conn = await self_inner._cm.__aenter__()
                        return _RaceConn(conn)

                    async def __aexit__(self_inner, *exc):
                        return await self_inner._cm.__aexit__(*exc)
                return _Ctx()

        thread_r = await ct.create_thread(pool, TEST_EID, title="Thread R")
        r_ids = await _seed_via_writer(pool, 2, thread_id=thread_r["id"],
                                       label="thread-R")
        threads_before = await pool.fetchval(
            "SELECT count(*) FROM circles_threads WHERE entity_id=$1", TEST_EID)
        links_before = await pool.fetchval(
            "SELECT count(*) FROM circles_thread_links WHERE entity_id=$1",
            TEST_EID)
        rolled_back = ""
        try:
            await ct.split_thread(_RacePool(pool), TEST_EID, thread_r["id"],
                                  from_turn_id=r_ids[0], title="orphan?")
        except ValueError as e:
            rolled_back = str(e)
        check("e3. a race that empties the move set AFTER the thread row is "
              "created raises and commits NOTHING (no orphan thread, no link)",
              rolled_back == "nothing to move"
              and (await pool.fetchval(
                  "SELECT count(*) FROM circles_threads WHERE entity_id=$1",
                  TEST_EID)) == threads_before
              and (await pool.fetchval(
                  "SELECT count(*) FROM circles_thread_links WHERE entity_id=$1",
                  TEST_EID)) == links_before,
              f"raised={rolled_back!r} threads={threads_before} "
              f"links={links_before}")
        r_rows = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE id = ANY($1::bigint[])"
            "   AND metadata->'source_context'->>'ref' = $2",
            r_ids, thread_r["id"])
        check("e3. the source thread's rows are untouched after the rolled-back "
              "split", r_rows == 2, str(r_rows))

        # ── e4. links are entity-scoped and never self-referential ────────
        other_eid = TEST_EID + 1
        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) "
            "VALUES ($1, 'CirclesThreadsGateOther', 'standard') "
            "ON CONFLICT (id) DO NOTHING", other_eid)
        await pool.execute(
            "DELETE FROM circles_thread_links WHERE entity_id=$1", other_eid)
        await pool.execute(
            "DELETE FROM circles_threads WHERE entity_id=$1", other_eid)
        foreign = await ct.create_thread(pool, other_eid, title="not yours")
        links_pre = await pool.fetchval(
            "SELECT count(*) FROM circles_thread_links WHERE entity_id=$1",
            TEST_EID)
        await ct.write_link(pool, TEST_EID, c_id, foreign["id"], "recall")
        await ct.write_link(pool, TEST_EID, foreign["id"], c_id, "recall")
        check("11. a link naming ANOTHER entity's thread inserts nothing "
              "(either end)",
              (await pool.fetchval(
                  "SELECT count(*) FROM circles_thread_links WHERE entity_id=$1",
                  TEST_EID)) == links_pre,
              f"links={links_pre}")
        await ct.write_link(pool, TEST_EID, c_id, c_id, "recall")
        check("11. a SELF link inserts nothing",
              (await pool.fetchval(
                  "SELECT count(*) FROM circles_thread_links WHERE entity_id=$1",
                  TEST_EID)) == links_pre,
              f"links={links_pre}")
        self_check = ""
        try:
            await pool.execute(
                "INSERT INTO circles_thread_links "
                "  (entity_id, from_thread, to_thread, reason) "
                "VALUES ($1, $2::uuid, $2::uuid, 'recall')", TEST_EID, c_id)
        except Exception as e:  # noqa: BLE001
            self_check = type(e).__name__
        check("11. the TABLE refuses a self link too (CHECK from_thread <> "
              "to_thread)",
              self_check == "CheckViolationError", self_check or "no error")
        await pool.execute(
            "DELETE FROM circles_threads WHERE entity_id=$1", other_eid)
        await pool.execute(
            "DELETE FROM entity_registry WHERE id=$1", other_eid)

        # ── 14. a marker prefers the recorded flip turn ───────────────────
        # _store_summary records flip_turn_id on a topic flip; markers() must
        # hand that back as from_turn_id instead of deriving one from the
        # summary row's write time (which is minutes later, so a Split there
        # left the new topic's first turns behind).
        thread_m = await ct.create_thread(pool, TEST_EID, title="Thread M")
        m_id = thread_m["id"]
        m_ids = await _seed_via_writer(pool, 4, thread_id=m_id, label="thread-M")
        m_src = ct.descriptor_for(m_id)
        for _v, _topic, _flip in ((1, "invoices", None), (2, "hiring", m_ids[1])):
            _md = {"source_kind": "thread_consolidator", "source_context": m_src,
                   "summary_version": _v, "turns_covered": 2,
                   "segment_topic": _topic}
            if _flip is not None:
                _md["flip_turn_id"] = _flip
            await pool.execute(
                """
                INSERT INTO entity_memory
                  (entity_id, content, memory_type, importance, status,
                   source_session, metadata)
                VALUES ($1, $2, 'summary', 1.0, 'active', $3, $4::jsonb)
                """,
                TEST_EID, f"Previously: invoices\n\nM summary v{_v}", SESSION,
                json.dumps(_md))
        m_markers = await ct.markers(pool, TEST_EID, m_id)
        check("14. markers() returns the summary row's recorded flip_turn_id "
              "as from_turn_id",
              len(m_markers) == 1 and m_markers[0]["from_turn_id"] == m_ids[1]
              and m_markers[0]["topic"] == "hiring",
              json.dumps(m_markers, default=str))
        # And the fallback still works where no flip turn was recorded.
        await pool.execute(
            "UPDATE entity_memory SET metadata = metadata - 'flip_turn_id' "
            " WHERE entity_id=$1 AND memory_type='summary' "
            "   AND metadata->'source_context'->>'ref'=$2", TEST_EID, m_id)
        m_markers_fb = await ct.markers(pool, TEST_EID, m_id)
        check("14. without flip_turn_id the marker falls back to the "
              "time-based anchor",
              len(m_markers_fb) == 1
              and m_markers_fb[0]["from_turn_id"] != m_ids[1],
              json.dumps(m_markers_fb, default=str))

        # ── 9. one canonical id, on every surface ─────────────────────────
        check("9. canonical_id folds a non-canonical uuid to the row's form "
              "and the sentinels to None",
              ct.canonical_id(a_id.upper()) == a_id
              and ct.canonical_id("original") is None
              and ct.canonical_id("self") is None
              and ct.canonical_id(None) is None,
              f"upper={ct.canonical_id(a_id.upper())} a={a_id}")
        if circles_routes is not None:
            import server.turn_registry as _treg
            _prev_pool = getattr(brain_service, "_pool", None)
            _prev_eid = getattr(brain_service, "_entity_id", None)
            _prev_compose = _treg.compose_thread_key
            seen: list = []

            def _recording_compose(*a, **kw):
                seen.append(kw.get("thread_id"))
                return _prev_compose(*a, **kw)

            brain_service._pool = pool
            brain_service._entity_id = TEST_EID
            _treg.compose_thread_key = _recording_compose
            try:
                await circles_routes.turn_active(thread=a_id.upper())
                await circles_routes.turn_active(thread="self")
            finally:
                _treg.compose_thread_key = _prev_compose
                brain_service._pool = _prev_pool
                brain_service._entity_id = _prev_eid
            check("9. GET /turns/active composes its lock key from the "
                  "CANONICAL thread id, the way the send path does",
                  seen == [a_id, None], str(seen))

        # ── f. Clear on one thread ────────────────────────────────────────
        thread_e = await ct.create_thread(pool, TEST_EID, title="Thread E")
        e_id = thread_e["id"]
        await _seed_via_writer(pool, 2, thread_id=e_id, label="thread-E")

        cleared_via = "route"
        if circles_routes is not None:
            _prev_pool = getattr(brain_service, "_pool", None)
            _prev_eid = getattr(brain_service, "_entity_id", None)
            brain_service._pool = pool
            brain_service._entity_id = TEST_EID
            try:
                res = await circles_routes.delete_thread("self", e_id, None)
                cleared = int((res or {}).get("cleared") or 0)
            finally:
                brain_service._pool = _prev_pool
                brain_service._entity_id = _prev_eid
        else:
            # The archive is inline SQL inside the route (circles.py:2664-2678)
            # with no data-layer function behind it, and the route module does
            # not import outside the container. Same statement, same scope
            # clause the route builds — reported as such.
            cleared_via = "route SQL replayed (module not importable)"
            _a: list = [TEST_EID]
            _w = _turn_row_where(_a, session_id="",
                                 source_context=ct.descriptor_for(e_id))
            rows = await pool.fetch(
                "UPDATE entity_memory SET status='archived', "
                "status_reason='circles_thread_cleared' "
                f"WHERE {' AND '.join(_w)} RETURNING id", *_a)
            cleared = len(rows)
        check(f"f. Clear archives this thread's turns ({cleared_via})",
              cleared == 2, str(cleared))

        e_left = await count_scope_turns(
            pool, TEST_EID, "", source_context=ct.descriptor_for(e_id))
        check("f. the cleared thread reads empty afterwards",
              e_left["count"] == 0, str(e_left))
        a_left = await count_scope_turns(
            pool, TEST_EID, "", source_context=ct.descriptor_for(a_id))
        b_left = await count_scope_turns(
            pool, TEST_EID, "", source_context=ct.descriptor_for(b_id))
        legacy_left = await count_scope_turns(
            pool, TEST_EID, "", source_context=LEGACY_SRC)
        check("f. Clear on a thread archives NOTHING in another thread",
              a_left["count"] == 2 and b_left["count"] == 2,
              f"A={a_left['count']} B={b_left['count']}")
        check("f. Clear on a thread archives NOTHING in the original "
              "conversation",
              legacy_left["count"] == legacy_before["count"],
              f"{legacy_before['count']} -> {legacy_left['count']}")
        archived = await pool.fetch(
            "SELECT DISTINCT left(content, 8) AS c FROM entity_memory "
            " WHERE entity_id=$1 AND status='archived'", TEST_EID)
        check("f. only thread-E rows carry status='archived'",
              [r["c"] for r in archived] == ["thread-E"],
              str([r["c"] for r in archived]))

        # ── FRAME's "new thread" line, through frame.py itself ─────────────
        # test_thread_consolidator.py and this file's earlier checks test
        # turn-zero through count_scope_turns directly; neither calls
        # runtime/frame.py, where the actual FRAME line lives
        # (frame_facts:139-168 counts the scope, render_frame:223-230 renders
        # "new thread — this is the first message" when that count is 0).
        # frame_facts(pool, sess, ...) only reads attributes off `sess`
        # (entity_id, session_id, interaction_context, frame, thread_id,
        # history_before — frame.py:86-151) and never constructs an
        # EntitySession itself, so a SimpleNamespace carrying exactly those
        # attributes calls the real function, not a re-implementation of it.
        from types import SimpleNamespace
        from mastercode.runtime.frame import frame_facts, render_frame

        thread_f = await ct.create_thread(pool, TEST_EID, title="Thread F")
        f_id = thread_f["id"]

        fresh_sess = SimpleNamespace(
            entity_id=TEST_EID, session_id=SESSION,
            interaction_context="circles", frame={}, thread_id=f_id,
            history_before=None)
        fresh_facts = await frame_facts(pool, fresh_sess)
        fresh_line = render_frame(fresh_facts)
        check("a fresh thread's FRAME facts report turns_before/"
              "user_turns_before == 0",
              fresh_facts.get("user_turns_before") == 0
              and fresh_facts.get("turns_before") == 0,
              json.dumps({k: fresh_facts.get(k) for k in
                          ("user_turns_before", "turns_before")}))
        check("FRAME renders 'new thread — this is the first message' "
              "for the fresh thread",
              "new thread — this is the first message" in fresh_line,
              fresh_line)

        legacy_sess = SimpleNamespace(
            entity_id=TEST_EID, session_id=SESSION,
            interaction_context="circles", frame={}, thread_id=None,
            history_before=None)
        legacy_facts = await frame_facts(pool, legacy_sess)
        legacy_line = render_frame(legacy_facts)
        check("thread_id=None (the legacy scope) keeps its own, "
              "UNCHANGED, non-zero turn count through frame.py "
              "('turns_before' is the unfiltered total; 'user_turns_before' "
              "is user-role only and is legitimately half of it here)",
              legacy_facts.get("turns_before") == legacy_before["count"],
              f"frame said {legacy_facts.get('turns_before')}, "
              f"legacy scope has {legacy_before['count']}")
        check("the legacy scope's FRAME line is the CONTINUING-thread "
              "line, not the fresh-thread line",
              "new thread — this is the first message" not in legacy_line,
              legacy_line)

        # ── g. the original conversation's own acts leave hidden rows alone ──
        # Split off the original conversation moves what the user SEES
        # (legacy_conversation_where), never a hidden-lane row.
        split_from = min(new_ids)  # the first descriptor-tagged visible row
        g_thread = await ct.split_thread(
            pool, TEST_EID, ct.LEGACY_THREAD_ID, from_turn_id=split_from,
            title="Thread G")
        # The destination is read the way the UI reads it (conversation_where,
        # no supersession filter) — recall_recent_turns would hide the moved
        # superseded rows and make this check unable to see a loss.
        g_args: list = [TEST_EID]
        g_where = ct.conversation_where(g_args, g_thread["id"])
        g_rows = await pool.fetch(
            f"SELECT id, content FROM entity_memory WHERE {' AND '.join(g_where)}"
            f" ORDER BY id", *g_args)
        g_bodies = [r["content"] or "" for r in g_rows]
        g_ids = [r["id"] for r in g_rows]
        check("g. a split off the original conversation moves only visible "
              "rows (no hidden-lane row)",
              g_bodies and not any(b.startswith("hidden-lane") for b in g_bodies),
              str([b[:14] for b in g_bodies]))
        check("g. the moved SUPERSEDED rows are visible in the new thread "
              "(a transcript never applies recall's supersession filter)",
              all(s in g_ids for s in superseded_ids),
              f"moved={[s in g_ids for s in superseded_ids]} ids={g_ids}")
        g_tags = await pool.fetch(
            "SELECT id, metadata->>'app_id' AS app_id, "
            "       metadata->>'split_prior_app_id' AS prior FROM entity_memory"
            " WHERE id = ANY($1::bigint[])", g_ids)
        check("g. rows moved off the ORIGINAL conversation have app_id "
              "stripped and kept under split_prior_app_id (so they answer "
              "one scope only)",
              g_tags and all(r["app_id"] is None for r in g_tags)
              and all(r["prior"] == "circles" for r in g_tags),
              str([(r["id"], r["app_id"], r["prior"]) for r in g_tags]))
        g_listed = {t["id"]: t for t in await ct.list_threads(pool, TEST_EID)}
        check("g. the list counts the thread's SUPERSEDED rows too (what it "
              "shows: 2 visible + 2 superseded = 4)",
              g_listed.get(g_thread["id"], {}).get("turn_count") == 4,
              f"G={g_listed.get(g_thread['id'], {}).get('turn_count')} "
              f"ids={g_ids}")
        recall_g = await recall_recent_turns(
            pool, TEST_EID, "", limit=50,
            source_context=ct.descriptor_for(g_thread["id"]))
        check("g. recall in the new thread still hides the superseded rows",
              not any((r.get("content") or "").startswith("legacy-superseded")
                      for r in recall_g),
              str([(r.get("content") or "")[:18] for r in recall_g]))
        if circles_routes is not None:
            brain_service._pool = pool
            brain_service._entity_id = TEST_EID
            try:
                g_read = await circles_routes._read_self_thread(200, g_thread["id"])
            finally:
                brain_service._pool = _prev_pool
                brain_service._entity_id = _prev_eid
            g_read_bodies = [(r.get("body") or "") for r in (g_read or [])]
            check("g. the thread READ (route) shows the moved superseded rows",
                  sum(b.startswith("legacy-superseded") for b in g_read_bodies) == 2,
                  str([b[:18] for b in g_read_bodies]))
        hidden_row = await pool.fetchrow(
            "SELECT status, metadata->'source_context' AS sc FROM entity_memory"
            " WHERE id=$1", hidden_id)
        check("g. the hidden-lane row keeps its app:circles descriptor after "
              "the split",
              hidden_row is not None
              and json.loads(hidden_row["sc"]).get("ref") == "circles",
              str(dict(hidden_row) if hidden_row else None))

        # Clear on the original conversation archives what it shows and
        # nothing else: the hidden-lane row stays active.
        if circles_routes is not None:
            brain_service._pool = pool
            brain_service._entity_id = TEST_EID
            try:
                res_l = await circles_routes.delete_thread(
                    "self", ct.LEGACY_THREAD_ID, None)
            finally:
                brain_service._pool = _prev_pool
                brain_service._entity_id = _prev_eid
            hidden_status = await pool.fetchval(
                "SELECT status FROM entity_memory WHERE id=$1", hidden_id)
            check("g. Clear on the original conversation leaves the "
                  "hidden-lane row ACTIVE",
                  hidden_status == "active",
                  f"status={hidden_status} cleared={res_l}")
            old_status = await pool.fetch(
                "SELECT status FROM entity_memory WHERE id = ANY($1::bigint[])",
                old_ids)
            check("g. Clear on the original conversation archives its visible "
                  "old-tagged rows",
                  old_status and all(r["status"] == "archived" for r in old_status),
                  str([r["status"] for r in old_status]))

        # ── w. a fired wake-up in a thread (turn types step 5) ────────────
        # The runner now runs a wake-up IN the Circles thread it was set in:
        # its own reply row carries the thread's descriptor on the hidden lane
        # (app_id 'scheduled_task'), and the runner's report (the same reply)
        # is the visible row. The thread shows and recalls the report once;
        # Clear takes both.
        from mastercode.memory_engine import write_turn_row as _wtr_w
        thread_w = await ct.create_thread(pool, TEST_EID, title="Thread W")
        w_id = thread_w["id"]
        w_src = ct.descriptor_for(w_id)
        w_user = await _wtr_w(pool, TEST_EID, SESSION, "user", f"wake-W ask {PAD}",
                              tags={"app_id": "circles", "source_context": w_src})
        w_hidden = await _wtr_w(pool, TEST_EID, SESSION, "assistant", f"wake-W reply {PAD}",
                                tags={"app_id": "scheduled_task", "source_context": w_src})
        w_report = await _wtr_w(pool, TEST_EID, SESSION, "assistant", f"wake-W reply {PAD}",
                                tags={"app_id": "circles", "source_context": w_src,
                                      "scheduled_task_id": 1})
        w_rec = await recall_recent_turns(pool, TEST_EID, SESSION, limit=50, source_context=w_src)
        w_rec_ids = [r.get("id") for r in w_rec]
        check("w. recall in the thread holds the wake-up reply ONCE (the report), "
              "not the hidden row",
              w_hidden not in w_rec_ids
              and sum(1 for r in w_rec if (r.get("content") or "").startswith("wake-W reply")) == 1,
              str([(r.get("id"), (r.get("content") or "")[:12]) for r in w_rec]))
        w_count = await count_scope_turns(pool, TEST_EID, "", source_context=w_src)
        check("w. the thread's turn count leaves the hidden row out -> 2",
              w_count["count"] == 2, str(w_count))
        w_args: list = [TEST_EID]
        w_where = ct.shown_where(w_args, w_id)
        w_shown = [r["id"] for r in await pool.fetch(
            f"SELECT id FROM entity_memory WHERE {' AND '.join(w_where)}", *w_args)]
        check("w. the transcript shows the ask and the report, not the hidden row",
              sorted(w_shown) == sorted([w_user, w_report]), str(w_shown))
        c_args: list = [TEST_EID]
        c_where = ct.conversation_where(c_args, w_id)
        await pool.execute(
            f"UPDATE entity_memory SET status='archived', status_reason='circles_thread_cleared' "
            f" WHERE {' AND '.join(c_where)}", *c_args)
        w_left = await pool.fetch(
            "SELECT id, status FROM entity_memory WHERE id = ANY($1::bigint[])",
            [w_user, w_hidden, w_report])
        check("w. Clear on the thread archives the hidden row with the rest",
              len(w_left) == 3 and all(r["status"] == "archived" for r in w_left),
              str([(r["id"], r["status"]) for r in w_left]))

        # ── x. Delete: the thread AND what was learned in it ──────────────
        # Clear leaves the rows a thread learned active, so a bad thread kept
        # steering the Host. Every learned row and every
        # stamp on what a thread retired below is written by a PRODUCTION
        # writer, in the turn: memory_curate (add, replace, drop), close_turn's
        # goal, the thread summary writer, the turn writer; never by hand. The
        # background batch consolidator and its per-candidate classifier, which
        # wrote them, are gone, and with them the checks
        # that ratcheted their races against a delete. Distinct marker words
        # (…marker55) keep each conversation's rows apart.
        from server import brain_service as _bs
        from mastercode.memory_engine import write_turn_row
        from mastercode.memory_retrieval import _canonical_where
        from mastercode.runtime import close_turn as _close
        from mastercode.runtime.tools import _exec_memory_curate
        from mastercode.runtime.turn_types import ensure_type_thread
        from server.routes.projects import adopt_thread_core

        async def _plain(src, mtype: str, text: str, meta: dict | None = None) -> int:
            """A row a conversation already held: what a thread later corrects
            or drops."""
            md = dict(meta or {})
            if src:
                md["source_context"] = src
            return await pool.fetchval(
                "INSERT INTO entity_memory (entity_id, content, memory_type, importance,"
                " status, metadata) VALUES ($1, $2, $3, 0.8, 'active', $4::jsonb) RETURNING id",
                TEST_EID, text, mtype, json.dumps(md))

        async def _curate(session: str, **args) -> dict:
            """memory_curate as a turn in `session` calls it: the conversation is
            the one that session's newest turn row names
            (memory_engine.session_source_context)."""
            prev = (getattr(_bs, "_pool", None), getattr(_bs, "_entity_id", None))
            _bs._pool, _bs._entity_id = pool, TEST_EID
            try:
                out = await _exec_memory_curate(args, session_id=session)
            finally:
                _bs._pool, _bs._entity_id = prev
            res = out.get("result")
            return res if out.get("status") == "ok" and isinstance(res, dict) else {"error": out}

        async def _fix(session: str, target: int, text: str):
            """memory_curate's replace: the correction, said in `session`."""
            r = await _curate(session, op="replace", memory_id=target, content=text, about="person")
            return r.get("memory_id")

        async def _drop(session: str, target: int) -> dict:
            """memory_curate's drop: retired, no replacement."""
            return await _curate(session, op="drop", memory_id=target)

        async def _goal(thread_id, session: str, **goal) -> dict:
            """close_turn's goal, in the thread's own turn."""
            res = await _close.apply(pool, TEST_EID, {"goal": goal}, session_id=session,
                                     thread_id=thread_id, turn_scope=ct.descriptor_for(thread_id))
            return res.get("goal") or {}

        async def _row(i: int) -> dict:
            r = await pool.fetchrow(
                "SELECT id, status, status_reason, t_valid_until, resolved_at,"
                " resolution_pointer, project_id, metadata FROM entity_memory WHERE id = $1", i)
            d = dict(r)
            d["metadata"] = json.loads(d["metadata"]) if isinstance(d["metadata"], str) else (d["metadata"] or {})
            return d

        async def _live(i: int) -> bool:
            r = await _row(i)
            return r["status"] == "active" and r["t_valid_until"] is None

        x_id = (await ct.create_thread(pool, TEST_EID, title="Thread X"))["id"]
        y_id = (await ct.create_thread(pool, TEST_EID, title="Thread Y"))["id"]
        X = ct.descriptor_for(x_id)
        Y = ct.descriptor_for(y_id)
        x_turns = await _seed_via_writer(pool, 2, thread_id=x_id, label="thread-X", session="sess-x")
        y_turns = await _seed_via_writer(pool, 2, thread_id=y_id, label="thread-Y", session="sess-y")

        # What the original conversation held before X.
        name_old = await _plain(LEGACY_SRC, "preference", "the person name is alex per namemarker66")
        home_old = await _plain(LEGACY_SRC, "preference", "the person lives in ohio near hometownmarker55")
        tea_old = await _plain(LEGACY_SRC, "preference", "the person likes tea teamarker99")
        g_open = await _plain(LEGACY_SRC, "goal", "ship the invoice app")

        # X learns a preference, a goal and a summary; corrects the name and
        # the home; drops "likes tea".
        x_pref = (await _curate("sess-x", op="add", about="person",
                                content="learned in X: always answer in French")).get("memory_id")
        name_x = await _fix("sess-x", name_old, "the person name is bob per namemarker66")
        home_x = await _fix("sess-x", home_old, "the person lives in utah near hometownmarker55")
        x_goal = ((await _goal(x_id, "sess-x", set="learned in X: goal")).get("set") or {}).get("id")
        # A rolling summary of X as a Host from before v241 holds one (the
        # writer is retired; its rows stay until a delete takes them).
        x_summary = await pool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status,"
            " source_session, metadata) VALUES ($1, 'learned in X: summary', 'summary', 1.0, 'active',"
            " 'sess-x', $2::jsonb) RETURNING id", TEST_EID,
            json.dumps({"source_kind": "thread_consolidator", "source_context": X,
                        "summary_version": 1, "turns_covered": len(x_turns)}))
        dropped = await _drop("sess-x", tea_old)
        st = {i: await _row(i) for i in (name_old, home_old, tea_old)}
        check("x. the in-turn writers stamp what X retired with X",
              all(r["metadata"].get("superseded_in") == X for r in st.values())
              and st[name_old]["resolution_pointer"] == name_x
              and st[tea_old]["resolution_pointer"] is None and dropped.get("dropped") is True,
              str({i: (r["metadata"].get("superseded_in"), r["resolution_pointer"])
                   for i, r in st.items()}) + f" drop={dropped}")
        learned_rows = [await _row(i) for i in (x_pref, x_goal, name_x, home_x, x_summary) if i]
        check("x. every row X learned carries X's scope",
              len(learned_rows) == 5
              and all(r["metadata"].get("source_context") == X for r in learned_rows),
              str([(r["id"], r["metadata"].get("source_context")) for r in learned_rows]))

        # Y corrects X's correction: the person moved on from Utah to Idaho.
        home_y = await _fix("sess-y", home_x, "the person lives in idaho near hometownmarker55")
        y_goal = ((await _goal(y_id, "sess-y", set="learned in Y: goal")).get("set") or {}).get("id")
        check("x. Y's correction retired X's (the chain Ohio → Utah → Idaho)",
              home_y is not None and y_goal is not None
              and (await _row(home_x))["resolution_pointer"] == home_y)

        # a turn type bound to X — drafted through the ONE writer of a type
        # (test_turn_types_approval sweeps the repo for any other), then bound
        from mastercode.runtime import turn_type_approval as tta
        drafted = await tta.draft(pool, TEST_EID, "x-type", {"keywords": ["x"]})
        tt_id = await pool.fetchval(
            "SELECT id FROM turn_types WHERE entity_id = $1 AND name = 'x-type'", TEST_EID)
        await pool.execute("UPDATE turn_types SET thread_id = $2::uuid WHERE id = $1::uuid",
                           tt_id, x_id)
        del drafted

        # wake-ups: the schedule tool stores the thread it was set in under
        # payload.origin.thread_id, in whatever form the turn carried
        async def _wakeup(title: str, status: str, thread_ref) -> int:
            payload: dict = {"prompt": "check in"}
            if thread_ref is not None:
                payload["origin"] = {"interaction_context": "chat", "thread_id": thread_ref}
            return await pool.fetchval(
                "INSERT INTO calendar_events (entity_id, title, start_time, end_time, event_type,"
                " event_at, summary, payload, source, status, recurrence) VALUES ($1, $2,"
                " NOW() + interval '1 hour', NOW() + interval '1 hour', 'scheduled_task',"
                " NOW() + interval '1 hour', $5, $3::jsonb, 'gate', $4, 'daily') RETURNING id",
                TEST_EID, title, json.dumps(payload), status, title)
        wk_next = await _wakeup("check the deploy", "scheduled", x_id.upper())
        wk_now = await _wakeup("summarise the logs", "running", x_id)
        wk_done = await _wakeup("an old one", "completed", x_id)
        wk_y = await _wakeup("Y's task", "scheduled", y_id)
        wk_orig = await _wakeup("the original's task", "scheduled", None)
        preview = await ct.wakeups_set_in(pool, TEST_EID, x_id)
        check("x. the confirm dialog lists X's unfinished wake-ups and nothing else",
              sorted(w["id"] for w in preview) == sorted([wk_next, wk_now])
              and {w["title"] for w in preview} == {"check the deploy", "summarise the logs"},
              str(preview))

        out = await ct.delete_thread(pool, TEST_EID, x_id.upper())
        check("x. delete archives the thread's messages and every row learned in it",
              out["id"] == x_id and out["messages"] == 2
              and out["learned"] == 5, str(out))   # preference, goal, Bob, Utah, summary
        rows = {i: await _row(i) for i in x_turns + [x_pref, x_summary, x_goal, name_x, home_x]}
        check("x. all of X is archived, with the delete's reason",
              all(r["status"] == "archived" and r["status_reason"] == ct.DELETED_REASON
                  for r in rows.values()), str(out))
        check("x. nothing in another thread or the original conversation was archived",
              all([await _live(i) for i in y_turns + [y_goal, home_y, g_open]]))
        check("x. what X retired comes back: the corrected name, the dropped preference",
              await _live(name_old) and await _live(tea_old)
              and out["restored"] == 2, str(out))
        restored = [await _row(i) for i in (name_old, tea_old)]
        check("x. a restored row carries no stamp and no pointer",
              all("superseded_in" not in r["metadata"] and r["resolution_pointer"] is None
                  for r in restored), str([r["metadata"] for r in restored]))
        check("x. a chain that moved on stays retired: Ohio does not come back beside Idaho",
              not await _live(home_old) and await _live(home_y))
        check("x. the original conversation's goal is untouched and nothing reopens",
              (await _row(g_open))["resolved_at"] is None and out["reopened"] == 0, str(out))

        wk = {r["id"]: (r["status"], r["cancelled_reason"]) for r in await pool.fetch(
            "SELECT id, status, cancelled_reason FROM calendar_events WHERE entity_id = $1", TEST_EID)}
        check("x. the wake-ups set in X are cancelled with the reason; finished and "
              "other threads' ones are not",
              wk[wk_next][0] == "cancelled" and wk[wk_now][0] == "cancelled"
              and "deleted" in (wk[wk_next][1] or "")
              and wk[wk_done][0] == "completed" and wk[wk_y][0] == "scheduled"
              and wk[wk_orig][0] == "scheduled" and out["cancelled"] == 2, str(wk))
        check("x. the delete names the wake-ups it cancelled, for the route to stop a firing one",
              sorted(out["cancelled_ids"]) == sorted([wk_next, wk_now]), str(out))
        # The wake-up that was firing finishes: it posts nothing (a report
        # re-homed to the original conversation would be learned there) and
        # is not re-armed.
        from mastercode.brain.offline_task_runner import _report_and_finalize
        start_now = await pool.fetchval("SELECT start_time FROM calendar_events WHERE id = $1", wk_now)
        before = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn'",
            TEST_EID)
        await _report_and_finalize(pool, TEST_EID, wk_now, "summarise the logs", "daily",
                                   start_now, True, "the logs, summarised", None,
                                   report_on_success=True,
                                   origin={"interaction_context": "chat", "thread_id": x_id})
        after = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn'",
            TEST_EID)
        check("x. a wake-up cancelled while it ran posts no report and is not re-armed",
              after == before
              and await pool.fetchval("SELECT status FROM calendar_events WHERE id = $1", wk_now)
              == "cancelled", f"turn rows {before} -> {after}")
        # ...while a live task's report still lands (the check above is not
        # passing because reports never post in this harness).
        await _report_and_finalize(pool, TEST_EID, wk_y, "Y's task", "daily",
                                   start_now, True, "Y's task ran", None,
                                   report_on_success=True,
                                   origin={"interaction_context": "chat", "thread_id": y_id})
        check("x. a task that was not cancelled still reports into its thread",
              await pool.fetchval(
                  "SELECT count(*) FROM entity_memory WHERE entity_id = $1"
                  " AND content = 'Y''s task ran'", TEST_EID) == 1)
        # The route: it lists a thread's wake-ups for the confirm dialog, and
        # a wake-up firing when the thread is deleted runs in its own turn lane
        # (offline_task_runner.wakeup_thread_key), which the route stops too.
        if circles_routes is not None:
            from mastercode.brain.offline_task_runner import wakeup_thread_key
            from server.turn_registry import get_turn_registry
            v_id = (await ct.create_thread(pool, TEST_EID, title="Thread V"))["id"]
            wk_v = await _wakeup("firing in V", "running", v_id)
            reg = get_turn_registry()
            stopped: list = []

            class _Firing:
                turn_id = "turn-firing-v"

            def _active(key):
                return _Firing() if key == wakeup_thread_key(TEST_EID, wk_v) else None

            async def _cancel(turn_id):
                stopped.append(turn_id)
                return {"ok": True}

            _prev_active, _prev_cancel = reg.active_for, reg.cancel
            _prev_pool = getattr(brain_service, "_pool", None)
            _prev_eid = getattr(brain_service, "_entity_id", None)
            brain_service._pool, brain_service._entity_id = pool, TEST_EID
            reg.active_for, reg.cancel = _active, _cancel
            try:
                listed_v = await circles_routes.circles_thread_wakeups(v_id)
                res_v = await circles_routes.delete_circles_thread(v_id)
            finally:
                reg.active_for, reg.cancel = _prev_active, _prev_cancel
                brain_service._pool, brain_service._entity_id = _prev_pool, _prev_eid
            check("x. the route lists the thread's wake-up, then stops its firing turn "
                  "when the thread is deleted",
                  [w["id"] for w in listed_v["wakeups"]] == [wk_v]
                  and stopped == ["turn-firing-v"] and res_v["deleted"]["cancelled"] == 1,
                  f"{listed_v} {stopped} {res_v}")
        listed = [t["id"] for t in await ct.list_threads(pool, TEST_EID)]
        check("x. the deleted thread leaves the list; the other stays",
              x_id not in listed and y_id in listed, str(listed))
        check("x. a deleted thread is absent to a read and to a send",
              await ct.get_thread(pool, TEST_EID, x_id) is None
              and await ct.thread_exists_open(pool, TEST_EID, x_id) is None)
        check("x. anything still addressed to it goes to the original conversation",
              await ct.live_thread_id(pool, TEST_EID, x_id) is None
              and await ct.live_thread_id(pool, TEST_EID, y_id) == y_id)
        live_sql, live_args = _canonical_where(TEST_EID)
        live_args.append([x_pref, x_summary, x_goal, name_x, y_goal])
        live = {r["id"] for r in await pool.fetch(
            f"SELECT id FROM entity_memory WHERE {live_sql} AND id = ANY(${len(live_args)}::int[])",
            *live_args)}
        check("x. recall's filter no longer returns what was learned in X",
              live == {y_goal}, str(live))
        x_open = {g["id"] for g in await _close._open_goals_in(pool, TEST_EID, X)}
        y_open = {g["id"] for g in await _close._open_goals_in(pool, TEST_EID, Y)}
        check("x. X's open goal no longer reaches a turn in its thread; Y's does",
              not x_open and y_goal in y_open, f"{x_open} {y_open}")
        # a late row: a reply that finished after the delete
        late = await write_turn_row(pool, TEST_EID, "sess-x", "assistant", "a reply that landed late",
                                    tags={"app_id": "circles", "source_context": X})
        late_row = await _row(late)
        check("x. a row that lands after the delete is archived on arrival",
              late_row["status"] == "archived", str(late_row["status"]))
        # a turn of X that was still running when the person deleted X writes
        # after the delete; the writer's own end takes all of it
        job_old = await _plain(LEGACY_SRC, "preference", "the person works at acme employermarker44")
        late_add = await _curate("sess-x", op="add", about="person",
                                 content="learned late in X: tabs over spaces")
        late_fix = await _curate("sess-x", op="replace", memory_id=job_old, about="person",
                                 content="the person works at globex employermarker44")
        late_learned = await pool.fetch(
            "SELECT id, status FROM entity_memory WHERE entity_id = $1"
            " AND content IN ('learned late in X: tabs over spaces',"
            "                 'the person works at globex employermarker44')", TEST_EID)
        check("x. what a turn still running in X writes after the delete is forgotten at its end",
              len(late_learned) == 2 and all(r["status"] == "archived" for r in late_learned)
              and late_add.get("thread_deleted") is True and late_fix.get("thread_deleted") is True
              and await _live(job_old),
              f"{[dict(r) for r in late_learned]} {late_add} {late_fix}")

        tt = dict(await pool.fetchrow("SELECT id, thread_id FROM turn_types WHERE id = $1", tt_id))
        tt_new = await ensure_type_thread(pool, TEST_EID, {"id": str(tt_id), "thread_id": str(x_id)},
                                          reopen=True)
        check("x. a turn type whose thread was deleted gets a new thread, never X back",
              tt["thread_id"] is None and tt_new and tt_new != x_id
              and await ct.get_thread(pool, TEST_EID, tt_new) is not None, f"{tt} -> {tt_new}")
        refused = []
        for label, call in (("again", lambda: ct.delete_thread(pool, TEST_EID, x_id)),
                            ("original", lambda: ct.delete_thread(pool, TEST_EID, "original")),
                            ("rename", lambda: ct.patch_thread(pool, TEST_EID, x_id, title="t"))):
            try:
                await call()
            except ValueError:
                refused.append(label)
        check("x. a second delete, the original conversation, and a rename of a deleted "
              "thread are all refused", refused == ["again", "original", "rename"], str(refused))

        # A chain through two deleted threads: P corrects chess → checkers, Q
        # corrects checkers → go. Deleting P leaves chess retired (Q's go is
        # live); deleting Q then brings chess back, walking through P's row.
        p_id = (await ct.create_thread(pool, TEST_EID, title="Thread P"))["id"]
        q_id = (await ct.create_thread(pool, TEST_EID, title="Thread Q"))["id"]
        await _seed_via_writer(pool, 2, thread_id=p_id, label="thread-P", session="sess-p")
        await _seed_via_writer(pool, 2, thread_id=q_id, label="thread-Q", session="sess-q")
        chess = await _plain(LEGACY_SRC, "preference", "the person plays chess gamemarker11")
        checkers = await _fix("sess-p", chess, "the person plays checkers gamemarker11")
        go = await _fix("sess-q", checkers, "the person plays go gamemarker11")
        out_p = await ct.delete_thread(pool, TEST_EID, p_id)
        chess_after_p = await _live(chess)
        out_q = await ct.delete_thread(pool, TEST_EID, q_id)
        check("x. a chain through two deleted threads: chess stays retired while Q's "
              "correction lives, and comes back when Q goes too",
              checkers is not None and go is not None
              and not chess_after_p and out_p["restored"] == 0
              and await _live(chess) and out_q["restored"] == 1, f"{out_p} {out_q}")

        # A chain whose last row a thread that still exists dropped (no
        # replacement): R corrects bike → scooter, Z drops the scooter.
        # Deleting R leaves the bike retired — Z's "no longer true" stands —
        # and deleting Z as well brings it back.
        r_id = (await ct.create_thread(pool, TEST_EID, title="Thread R"))["id"]
        z_id = (await ct.create_thread(pool, TEST_EID, title="Thread Z"))["id"]
        await _seed_via_writer(pool, 2, thread_id=r_id, label="thread-R", session="sess-r")
        await _seed_via_writer(pool, 2, thread_id=z_id, label="thread-Z", session="sess-z")
        bike = await _plain(LEGACY_SRC, "preference", "the person rides a bike bikemarker12")
        scooter = await _fix("sess-r", bike, "the person rides a scooter bikemarker12")
        await _drop("sess-z", scooter)
        out_r = await ct.delete_thread(pool, TEST_EID, r_id)
        bike_after_r = await _live(bike)
        out_z = await ct.delete_thread(pool, TEST_EID, z_id)
        check("x. a chain another thread ended with no replacement stays retired until "
              "that thread is deleted too",
              scooter is not None and not bike_after_r and out_r["restored"] == 0
              and await _live(bike) and out_z["restored"] == 1, f"{out_r} {out_z}")

        # Adopt, then delete the emptied thread: what the adopted messages
        # retired moved to the project with them and stays retired.
        a_id = (await ct.create_thread(pool, TEST_EID, title="Thread A"))["id"]
        await _seed_via_writer(pool, 2, thread_id=a_id, label="thread-A", session="sess-a")
        car_old = await _plain(LEGACY_SRC, "preference", "the person drives a sedan carmarker88")
        pet_old = await _plain(LEGACY_SRC, "preference", "the person has a cat petmarker77")
        truck = await _fix("sess-a", car_old, "the person drives a truck carmarker88")
        await _drop("sess-a", pet_old)
        proj = await pool.fetchval(
            "INSERT INTO projects (entity_id, name) VALUES ($1, 'Adopted') RETURNING id", TEST_EID)
        await adopt_thread_core(pool, TEST_EID, proj, "thread", a_id.upper())
        P_SCOPE = {"kind": "project", "ref": str(proj)}
        moved = [(await _row(i))["metadata"] for i in (car_old, pet_old)]
        check("x. an adopt moves the stamps with the messages that made them",
              {k: v for k, v in (moved[0].get("superseded_in") or {}).items() if k != "from_turns"} == P_SCOPE
              and {k: v for k, v in (moved[1].get("superseded_in") or {}).items() if k != "from_turns"} == P_SCOPE,
              str(moved))
        out_a = await ct.delete_thread(pool, TEST_EID, a_id)
        # The correction is the project's now (said there), and carries the
        # project tag a row written there has: none (circles_threads.
        # carry_learned).
        truck_row = await _row(truck) if truck is not None else {}
        check("x. deleting the emptied thread after an adopt restores and reopens nothing",
              out_a["restored"] == 0 and out_a["reopened"] == 0
              and not await _live(car_old) and not await _live(pet_old)
              and truck is not None and await _live(truck)
              and (truck_row.get("metadata") or {}).get("source_context") == P_SCOPE
              and truck_row.get("project_id") is None, f"{out_a} {truck_row}")

        # ── y/z. A deleted thread stops its own work and refuses new work ──
        # (Kept from the earlier gate: these four test the
        # kept turn and task paths, not the retired consolidator.)
        from mastercode.brain.offline_task_runner import _run_one
        from mastercode.runtime.tools import _exec_schedule_task

        async def _thread(label: str):
            tid = (await ct.create_thread(pool, TEST_EID, title=label))["id"]
            return tid, await _seed_via_writer(pool, 2, thread_id=tid, label=label,
                                               session=f"sess-{label}")

        # y9. A steps wake-up the delete cancels runs no further step
        y9, _ = await _thread("Thread Y9")
        calls9: list = []

        async def _send9(**kw):
            calls9.append(kw.get("message"))
            if len(calls9) == 1:
                await pool.execute(
                    "UPDATE calendar_events SET status = 'cancelled' WHERE id = $1", wk9)
            yield '0:"step done"\n'

        _prev_send, _prev_rt = _bs.send_message, _bs.get_runtime
        _bs.send_message, _bs.get_runtime = _send9, (lambda: object())
        try:
            wk9 = await _wakeup("three steps", "running", y9)
            await pool.execute(
                "UPDATE calendar_events SET payload = $2::jsonb WHERE id = $1", wk9,
                json.dumps({"steps": ["one", "two", "three"],
                            "origin": {"interaction_context": "chat", "thread_id": y9}}))
            row9 = await pool.fetchrow(
                "SELECT id, title, recurrence, start_time, payload FROM calendar_events"
                " WHERE id = $1", wk9)
            await _run_one(pool, TEST_EID, row9)
            steps_run = len(calls9)
            calls9.clear()
            wk9b = await _wakeup("claimed, then cancelled", "cancelled", y9)
            await pool.execute(
                "UPDATE calendar_events SET payload = $2::jsonb WHERE id = $1", wk9b,
                json.dumps({"prompt": "check", "origin": {"thread_id": y9}}))
            row9b = await pool.fetchrow(
                "SELECT id, title, recurrence, start_time, payload FROM calendar_events"
                " WHERE id = $1", wk9b)
            await _run_one(pool, TEST_EID, row9b)
        finally:
            _bs.send_message, _bs.get_runtime = _prev_send, _prev_rt
        check("y9. a steps task cancelled after step 1 runs no step 2, and a task "
              "cancelled before its turn starts none",
              steps_run == 1 and calls9 == [], f"steps={steps_run} second={calls9}")

        # y10. A turn addressed to a deleted thread is refused, not re-homed
        _prev_pool, _prev_eid = getattr(_bs, "_pool", None), getattr(_bs, "_entity_id", None)
        await ct.delete_thread(pool, TEST_EID, y9)
        _bs._pool, _bs._entity_id = pool, TEST_EID
        try:
            frames10 = [f async for f in _bs.send_message(
                entity_id=TEST_EID, message="hello", thread_id=y9, app_id="circles")]
        finally:
            _bs._pool, _bs._entity_id = _prev_pool, _prev_eid
        check("y10. send_message refuses a deleted thread",
              frames10 == ['3:"This thread was deleted."\n'], str(frames10))

        # z8. A task written as its thread was deleted is cancelled, not refused forever
        y8z, _ = await _thread("Thread ZY8")
        await ct.delete_thread(pool, TEST_EID, y8z)
        wk8 = await _wakeup("check the deploy z8", "running", y8z)
        row8 = await pool.fetchrow(
            "SELECT id, title, recurrence, start_time, payload FROM calendar_events WHERE id = $1",
            wk8)
        _prev = (_bs._pool, _bs._entity_id, _bs.get_runtime)
        _bs._pool, _bs._entity_id, _bs.get_runtime = pool, TEST_EID, (lambda: object())
        try:
            await _run_one(pool, TEST_EID, row8)
            sched8 = await _exec_schedule_task(
                {"op": "create", "title": "later", "prompt": "check",
                 "fire_at": "2099-01-01T00:00:00Z"}, thread_id=y8z)
        finally:
            _bs._pool, _bs._entity_id, _bs.get_runtime = _prev
        st8 = await pool.fetchval("SELECT status FROM calendar_events WHERE id = $1", wk8)
        reports8 = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id = $1"
            " AND metadata->>'scheduled_task_id' = $2", TEST_EID, str(wk8))
        check("z8. a task naming a deleted thread is cancelled at its first fire, posts "
              "nothing, and schedule_task refuses a deleted thread",
              st8 == "cancelled" and reports8 == 0 and sched8.get("status") == "error",
              f"status={st8} reports={reports8} schedule={sched8}")

        # z15. The turn writer binds the turn's scope where it derives it
        _sess_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                      "brain", "mastercode", "runtime", "session.py")).read()
        _i = _sess_src.find("sess.recall_scope = _src_ctx")
        check("z15. runtime/session.py sets CURRENT_TURN_SCOPE right where it derives the "
              "turn's scope",
              _i > 0 and 'CURRENT_TURN_SCOPE.set({"session_id": sess.session_id, "scope": _src_ctx})'
              in _sess_src[_i:_i + 400])


    finally:
        for _sql in ("DELETE FROM entity_tickets WHERE entity_id=$1",
                     "DELETE FROM turn_types WHERE entity_id=$1",
                     "DELETE FROM calendar_events WHERE entity_id=$1"):
            try:
                await pool.execute(_sql, TEST_EID)
            except Exception:
                pass
        try:
            await pool.execute(
                "DELETE FROM circles_thread_links WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "UPDATE circles_threads SET parent_thread_id=NULL "
                " WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM circles_threads WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM projects WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM entity_registry WHERE id=$1", TEST_EID)
        except Exception:
            pass
        await pool.close()

    if failures:
        print(f"  [circles-threads] {len(failures)} FAILURES: {failures}")
        return len(failures)
    print("  [circles-threads] all checks passed")
    return 0


if __name__ == "__main__":
    if IN_POD and not SERVER:
        sys.exit(asyncio.run(main()))
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no pod database, no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) "
              "and docker unavailable: this gate needs a real database.")
        sys.exit(2)
    from kernel_schema import kernel_schema_db, throwaway_server
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv) as _db:
        _rc = asyncio.run(main(_db))
    sys.exit(_rc)
