#!/usr/bin/env python3
"""Smoke gate: the knowledge table of contents.

WHAT THIS RATCHETS:
  1. THE PUSHED BLOCK IS BOUNDED — knowledge_toc.render_block returns at most
     twelve lines (twelve is the cap).
  2. NO NUMBER DERIVED FROM WEIGHT REACHES THE TEXT (weight ranks, it
     never recites). thread_weight sums est_cost_usd and counts supersessions,
     and rank_key orders RELATED with them — none of it may appear. The block
     is grepped for cost / score / importance / weight / $, and EVERY integer
     in it must be one of the counts this gate seeded.
  3. EVERY TOC ITEM CARRIES AN id (every item carries its row id, which
     is what makes provenance answerable by `read` instead of recited):
     episodes, artifacts, tool_trail.tasks, related, goals.open,
     goals.resolved. No summary lane: the rolling summary is retired.
  4. A READ IS AN EVENT — `chat_threads op='read'` on a thread ref, from
     inside a turn that has a thread, writes EXACTLY ONE circles_thread_links
     row reason='recall'. The same call with no thread on the turn
     writes none: there is no "from" to record.
  5. op='toc' ON THE LEGACY REF WORKS — the un-migrated app:circles scope is
     a real scope with real rows and is served exactly like a thread.
  6. EPISODES ANSWER ABOUT THE REF, NOT THE CALLER. A
     kind='session' scope read from a DIFFERENT session returns that scope's
     rows, and an episode over superseded turn rows counts them — an episode
     is a transcript window, so its count must agree with an `op='read'` of
     the same window.
  7. THE BLOCK IS CACHED PER (entity, scope) ON THE SCOPE'S NEWEST ROW:
     a second render with no new rows re-reads nothing
     but the stamp, and no render counts the same scope twice.
  8. A THREAD REF IS CANONICALISED AT THE TOOL BOUNDARY: an
     uppercase uuid is the same thread — same sections, and no self-link.
  9. NO VALUE CAN BREAK THE LINE CAP: a title carrying a newline
     is flattened, and the cap constant itself is pinned at twelve.

Seeded through the real writers wherever one exists: circles_threads.
create_thread, memory_engine.write_turn_row (what session.py:2660 calls, with
session.py:2608-2612's tag shape), circles_threads.set_thread_topic for the
topic (close_turn's own call, its one writer). The artifact is a user_context
row in the shape the retired tool-result capture wrote (nothing writes new
ones; a Host keeps those it has).
Typed goal rows are inserted directly, in the scope shape the in-turn writers
stamp (the thread's source_context). No row can be 'unresolved' since v235
(the type is retired and the CHECK refuses it), so the block must name none.

Runs in-container against the live schema; seeds a synthetic entity; always
cleans up.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")

TEST_EID = 99327  # synthetic; never a real entity
SESSION = "knowledge-toc-gate-session"
# A kind='session' scope belongs to the session in its REF, and the caller
# asking about it is somewhere else entirely — which is the whole point of
# check 6. Two different strings, deliberately.
VOICE_SESSION = "knowledge-toc-gate-voice-session"
CALLER_SESSION = "knowledge-toc-gate-other-session"
TOPIC = "billing"

# Everything the block may legitimately count, seeded below. Any OTHER integer
# in the rendered text is a number the host was handed that it cannot act on.
SEEDED_TURNS = 3
SEEDED_OPEN_GOALS = 2
SEEDED_RESOLVED_GOALS = 1
SEEDED_UNRESOLVED = 0   # retired type (v235): nothing can write one
SEEDED_ARTIFACTS = 1
SEEDED_RELATED = 1
SEEDED_TASKS = 1
SEEDED_REPOS = 1
# A non-zero cost and one supersession so thread_weight's cost_usd and
# superseded are exercised for real instead of trivially 0.
SEEDED_COST_USD = 0.0473
ALLOWED_INTEGERS = {
    SEEDED_TURNS, SEEDED_OPEN_GOALS, SEEDED_RESOLVED_GOALS, SEEDED_UNRESOLVED,
    SEEDED_ARTIFACTS, SEEDED_RELATED, SEEDED_TASKS, SEEDED_REPOS,
}

# The host is never handed a number it could narrate as knowledge.
BANNED_SUBSTRINGS = ("cost", "score", "importance", "weight", "$")

# One persisted tool-call chip, the shape session.py:180-230 writes onto the
# assistant turn row (metadata.tool_calls) with tool_trail.digest_tool_action's
# digest underneath.
TOOL_CHIP = {
    "tool": "dispatch",
    "op": "build",
    "status": "ok",
    "digest": {
        "task_id": "TASK-90001",
        "repo": "alex/project",
        "url": "https://forge.example/alex/project/pulls/9",
    },
}

ARTIFACT_TEXT = ("A page the host read for the user. " * 12)  # > ARTIFACT_MIN_CHARS


async def _seed_turn(pool, thread_id, i: int, *, tool_calls=None,
                     est_cost_usd=None) -> int | None:
    from mastercode.circles_threads import descriptor_for
    from mastercode.memory_engine import write_turn_row
    tags = {"app_id": "circles", "source_context": descriptor_for(thread_id)}
    if tool_calls:
        tags["tool_calls"] = tool_calls
    if est_cost_usd is not None:
        # session.py:5880 stamps this key on assistant turn rows from
        # server.routes.inference.estimate_turn_cost; write_turn_row's tags
        # merge (memory_engine.py:1492 "anything else passes through to
        # JSONB") carries it straight onto metadata.est_cost_usd, the exact
        # key thread_weight's cost query reads.
        tags["est_cost_usd"] = est_cost_usd
    return await write_turn_row(
        pool, TEST_EID, SESSION,
        "user" if i % 2 == 0 else "assistant",
        f"alpha turn {i} " + ("z" * 200), tags=tags)


async def _seed_typed(pool, thread_id, memory_type: str, content: str,
                      *, resolved: bool = False) -> int:
    """A typed memory in this thread's scope, stamped with the thread's
    source_context as the in-turn writers stamp it; resolved_at is what
    memory_consolidation._mark_resolved sets."""
    from mastercode.circles_threads import descriptor_for
    src = descriptor_for(thread_id)
    return await pool.fetchval(
        f"""
        INSERT INTO entity_memory
          (entity_id, content, memory_type, importance, status,
           source_session, metadata, resolved_at)
        VALUES ($1, $2, '{memory_type}', 1.0, 'active', $3, $4::jsonb,
                {'NOW()' if resolved else 'NULL'})
        RETURNING id
        """,
        TEST_EID, content, SESSION,
        json.dumps({"source_context": {"kind": src["kind"],
                                       "ref": str(src["ref"])}}))


async def _seed_session_turn(pool, session: str, i: int) -> int | None:
    """A turn in a kind='session' scope — the voice / cron / bg shape, whose
    ref IS the session id (memory_engine.derive_source_context's contract).
    Written through the same writer as every other turn here."""
    from mastercode.memory_engine import write_turn_row
    return await write_turn_row(
        pool, TEST_EID, session,
        "user" if i % 2 == 0 else "assistant",
        f"voice turn {i} " + ("z" * 50),
        tags={"source_context": {"kind": "session", "ref": session}})


class _CountingPool:
    """A pool that records the SQL it is asked to run and delegates the rest.

    The cache check needs the query COUNT of a render, and a fake pool cannot
    give it — the block's shape depends on real rows. So this wraps the live
    pool and only watches.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    async def fetch(self, q, *a, **k):
        self.calls.append(q)
        return await self._inner.fetch(q, *a, **k)

    async def fetchrow(self, q, *a, **k):
        self.calls.append(q)
        return await self._inner.fetchrow(q, *a, **k)

    async def fetchval(self, q, *a, **k):
        self.calls.append(q)
        return await self._inner.fetchval(q, *a, **k)

    async def execute(self, q, *a, **k):
        self.calls.append(q)
        return await self._inner.execute(q, *a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _missing_ids(payload: dict) -> list[str]:
    """Every item in the pulled payload that does not carry an `id`."""
    bad: list[str] = []
    for lane in ("episodes", "artifacts", "related"):
        for i, item in enumerate(payload.get(lane) or []):
            if not isinstance(item, dict) or item.get("id") in (None, ""):
                bad.append(f"{lane}[{i}]")
    for i, item in enumerate((payload.get("tool_trail") or {}).get("tasks") or []):
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            bad.append(f"tool_trail.tasks[{i}]")
    for bucket in ("open", "resolved"):
        for i, item in enumerate((payload.get("goals") or {}).get(bucket) or []):
            if not isinstance(item, dict) or item.get("id") in (None, ""):
                bad.append(f"goals.{bucket}[{i}]")
    return bad


async def main() -> int:
    import asyncpg
    pool = await asyncpg.create_pool(
        host="/var/run/postgresql", user="entity", database="hostmem",
        min_size=1, max_size=3)
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [knowledge-toc] OK {name}")
        else:
            failures.append(name)
            print(f"  [knowledge-toc] FAIL {name} {detail}")

    try:
        from mastercode import circles_threads as ct
        from mastercode import knowledge_toc as kt
        from mastercode.runtime import tools as rt

        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) "
            "VALUES ($1, 'KnowledgeTocGate', 'standard') "
            "ON CONFLICT (id) DO NOTHING", TEST_EID)
        await pool.execute(
            "DELETE FROM circles_thread_links WHERE entity_id=$1", TEST_EID)
        await pool.execute(
            "DELETE FROM circles_threads WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
        await pool.execute("DELETE FROM user_context WHERE entity_id=$1", TEST_EID)

        # ── the thread under test, and a sibling on the same topic ────────
        thread_a = await ct.create_thread(pool, TEST_EID, title="Alpha")
        thread_b = await ct.create_thread(pool, TEST_EID, title="Beta")
        a_id, b_id = thread_a["id"], thread_b["id"]
        src_a = ct.descriptor_for(a_id)

        turn_ids = []
        for i in range(SEEDED_TURNS):
            turn_ids.append(await _seed_turn(
                pool, a_id, i,
                tool_calls=[TOOL_CHIP] if i == 1 else None,
                est_cost_usd=SEEDED_COST_USD if i == 1 else None))
        check("turn rows landed through write_turn_row",
              len(turn_ids) == SEEDED_TURNS and all(turn_ids), str(turn_ids))

        # The topic, as close_turn (its one writer) sets it.
        await ct.set_thread_topic(pool, TEST_EID, a_id, TOPIC)
        a_row = await ct.get_thread(pool, TEST_EID, a_id)
        check("the topic is on circles_threads.topic",
              a_row is not None and a_row["topic"] == TOPIC, json.dumps(a_row))
        # Beta shares the topic, so it is RELATED by lookup with no
        # link row — the link half is exercised separately below.
        await ct.set_thread_topic(pool, TEST_EID, b_id, TOPIC)

        art = await pool.fetchval(
            "INSERT INTO user_context (entity_id, title, content, source, status, metadata)"
            " VALUES ($1, 'read_url result', $2, 'tool:read_url', 'raw', $3::jsonb) RETURNING id",
            TEST_EID, ARTIFACT_TEXT, json.dumps({"artifact": True, "source_context": src_a,
                                                 "turn_id": str(turn_ids[0])}))
        check("a captured tool result (the retired capture's row shape) is in place", art is not None)

        for i in range(SEEDED_OPEN_GOALS):
            await _seed_typed(pool, a_id, "goal", f"open goal {i}")
        for i in range(SEEDED_RESOLVED_GOALS):
            await _seed_typed(pool, a_id, "goal", f"done goal {i}", resolved=True)
        for i in range(SEEDED_UNRESOLVED):
            await _seed_typed(pool, a_id, "unresolved", f"open question {i}")

        # One 'cited_fact' row SUPERSEDED the way memory_consolidation._supersede
        # does it (:854-864) — t_valid_until = NOW(), resolution_pointer set to
        # a replacement id. This is what thread_weight's `superseded`
        # predicate counts (memory_type NOT IN ('summary','turn') AND
        # resolution_pointer IS NOT NULL AND t_valid_until IS NOT NULL).
        superseded_id = await _seed_typed(pool, a_id, "cited_fact", "old billing address")
        replacement_id = await _seed_typed(pool, a_id, "cited_fact", "new billing address")
        await pool.execute(
            "UPDATE entity_memory SET t_valid_until = NOW(), "
            "resolution_pointer = $2 WHERE id = $1", superseded_id, replacement_id)

        # ── 1 + 2. the pushed block ───────────────────────────────────────
        block = await kt.render_block(pool, TEST_EID, a_id, session_id=SESSION)
        print("  [knowledge-toc] --- rendered block ---")
        for ln in block.split("\n"):
            print(f"  [knowledge-toc] | {ln}")
        print("  [knowledge-toc] --- end block ---")
        lines = block.split("\n") if block else []
        check("1. the pushed block is at most 12 lines",
              0 < len(lines) <= kt.BLOCK_MAX_LINES,
              f"{len(lines)} lines (cap {kt.BLOCK_MAX_LINES})")
        # ...and the cap is TWELVE. Comparing the block to the constant alone
        # blesses whatever the constant becomes: raising it to 30 would have
        # kept this gate green with a block three times the size. The cap is a
        # number, so the gate names it too.
        check("1. the cap constant itself is 12, not merely self-consistent",
              kt.BLOCK_MAX_LINES == 12, f"BLOCK_MAX_LINES={kt.BLOCK_MAX_LINES}")

        low = block.lower()
        hits = [s for s in BANNED_SUBSTRINGS if s in low]
        check("2. the block names no cost / score / importance / weight / $",
              not hits, str(hits))

        ints = [int(n) for n in re.findall(r"\d+", block)]
        stray = sorted({n for n in ints if n not in ALLOWED_INTEGERS})
        check("2. every integer in the block is a count this gate seeded",
              not stray, f"stray={stray} all={ints}")
        # "N messages", not "turn N": FRAME already renders `turn <user turns
        # + 1>` (runtime/frame.py:228-229) and this counts every row in the
        # transcript, so the two blocks would have said "turn N" about the
        # same thread with two different N.
        check("2. the block reports the seeded size as MESSAGES",
              f"{SEEDED_TURNS} messages" in block and "turn " not in block,
              block)
        check("2. the block reports the seeded goal counts",
              f"{SEEDED_OPEN_GOALS} open goals" in block
              and "unresolved" not in block, block)
        check("2. the block reports the seeded artifact count",
              f"ARTIFACTS    {SEEDED_ARTIFACTS} in this thread" in block)
        check("2. the block reports the seeded task/repo counts",
              f"{SEEDED_TASKS} task" in block and f"{SEEDED_REPOS} repo" in block)
        check("2. the block names the sibling thread by TITLE, not by number",
              "RELATED      Beta" in block)
        check("2. the depth signal is the topic reach, in words",
              f"seen in {SEEDED_RELATED} other thread" in block)

        # The weight that ordered RELATED is real and is not in the text.
        weight = await kt.thread_weight(pool, TEST_EID, a_id)
        check("2. thread_weight IS computed (it just never reaches the text)",
              weight["goals_open"] == SEEDED_OPEN_GOALS
              and weight["goals_resolved"] == SEEDED_RESOLVED_GOALS
              and weight["unresolved"] == SEEDED_UNRESOLVED
              and weight["turns"] == SEEDED_TURNS,
              json.dumps(weight))
        # The two fields the weak "no weight number" assertion never
        # actually exercised (nothing seeded a cost, nothing superseded a
        # typed row, so both were trivially 0 during every prior run of this
        # gate) are now seeded non-zero and must be REPORTED non-zero.
        check("thread_weight reports the seeded cost non-zero",
              abs(weight["cost_usd"] - SEEDED_COST_USD) < 1e-9,
              json.dumps(weight))
        check("thread_weight reports the seeded supersession non-zero",
              weight["superseded"] == 1, json.dumps(weight))
        # ...and still never as text, or as ANY decimal number (a cost is a
        # decimal by construction; the earlier integer-only check would not
        # have caught one).
        check("no decimal number (a cost) appears in the rendered block",
              not re.search(r"\d+\.\d+", block), block)

        # ── 3. every item in the pulled payload carries an id ─────────────
        payload = await kt.build_toc(pool, TEST_EID, a_id, session_id=SESSION)
        check("3. the TOC returned every lane, and no summary lane",
              all(k in payload for k in ("thread", "episodes", "artifacts",
                                         "tool_trail", "related", "goals"))
              and "summary" not in payload,
              str(sorted(payload.keys())))
        check("3. the TOC is not empty (episodes, artifacts, tasks, related, "
              "goals all present)",
              bool(payload["episodes"]) and bool(payload["artifacts"])
              and bool(payload["tool_trail"]["tasks"])
              and bool(payload["related"]) and bool(payload["goals"]["open"])
              and bool(payload["goals"]["resolved"]),
              json.dumps({k: (len(v) if isinstance(v, list) else "…")
                          for k, v in payload.items()}))
        missing = _missing_ids(payload)
        check("3. EVERY item in the TOC carries an id", not missing, str(missing))
        check("3. the episode's id is a real, openable turn row",
              payload["episodes"][0]["id"] in turn_ids,
              f"{payload['episodes'][0]['id']} not in {turn_ids}")

        # ── 4. a read is an event ─────────────────────────────────────────
        await pool.execute(
            "DELETE FROM circles_thread_links WHERE entity_id=$1", TEST_EID)
        res = await rt._exec_chat_threads(
            {"op": "read", "kind": "thread", "ref": b_id},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        check("4. the read itself succeeds",
              (res or {}).get("status") == "ok", str(res)[:300])
        links = await pool.fetch(
            "SELECT from_thread, to_thread, reason FROM circles_thread_links "
            " WHERE entity_id=$1", TEST_EID)
        check("4. a read of thread B from inside thread A writes EXACTLY ONE "
              "link row reason='recall'",
              len(links) == 1 and links[0]["reason"] == "recall"
              and str(links[0]["from_thread"]) == a_id
              and str(links[0]["to_thread"]) == b_id,
              str([(str(r["from_thread"]), str(r["to_thread"]), r["reason"])
                   for r in links]))

        res_none = await rt._exec_chat_threads(
            {"op": "read", "kind": "thread", "ref": b_id},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=None)
        check("4. the same read with no thread on the turn still succeeds",
              (res_none or {}).get("status") == "ok", str(res_none)[:300])
        n_links = await pool.fetchval(
            "SELECT count(*) FROM circles_thread_links WHERE entity_id=$1",
            TEST_EID)
        check("4. a read with thread_id=None writes ZERO links",
              n_links == 1, f"links now {n_links}, expected still 1")

        res_self = await rt._exec_chat_threads(
            {"op": "read", "kind": "thread", "ref": a_id},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        n_links2 = await pool.fetchval(
            "SELECT count(*) FROM circles_thread_links WHERE entity_id=$1",
            TEST_EID)
        check("4. a thread reading ITSELF writes no link",
              (res_self or {}).get("status") == "ok" and n_links2 == 1,
              f"links now {n_links2}")

        # The link is what RELATED counts, so it must reach the payload.
        rel = await kt.related_threads(pool, TEST_EID, a_id)
        check("4. the recorded link makes Beta a related thread",
              any(r["id"] == b_id for r in rel), json.dumps(rel))

        # ── 5. op='toc' through the tool, including the legacy ref ────────
        toc_res = await rt._exec_chat_threads(
            {"op": "toc", "kind": "thread", "ref": a_id},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        check("5. op='toc' on a thread ref returns the payload",
              (toc_res or {}).get("status") == "ok"
              and not _missing_ids((toc_res or {}).get("result") or {}),
              str(toc_res)[:300])

        legacy_res = await rt._exec_chat_threads(
            {"op": "toc", "kind": "app", "ref": "circles"},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        # There is no original conversation: its ref is
        # refused with where to look, never answered with an empty index.
        check("5. op='toc' on the legacy app:circles ref is REFUSED: there is no original conversation",
              (legacy_res or {}).get("status") == "error"
              and "no original conversation" in str((legacy_res or {}).get("result") or ""),
              str(legacy_res)[:300])
        orig_res = await rt._exec_chat_threads(
            {"op": "toc", "kind": "thread", "ref": "original"},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        check("5. ...and so is the 'original' sentinel as a thread ref",
              (orig_res or {}).get("status") == "error", str(orig_res)[:200])

        bare_res = await rt._exec_chat_threads(
            {"op": "toc"}, entity_id=TEST_EID, pool=pool,
            session_id=SESSION, thread_id=a_id)
        check("5. a bare op='toc' is the topic the turn is in",
              (bare_res or {}).get("status") == "ok"
              and (((bare_res or {}).get("result") or {}).get("thread") or {})
              .get("id") == a_id,
              str(bare_res)[:300])
        nowhere = await rt._exec_chat_threads(
            {"op": "toc"}, entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=None)
        check("5. ...and a bare op='toc' from a turn in no topic is refused, never the legacy scope",
              (nowhere or {}).get("status") == "error", str(nowhere)[:200])

        refused = await rt._exec_chat_threads(
            {"op": "toc", "kind": "project", "ref": "213"},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        check("5. op='toc' on a project ref is REFUSED, not answered empty",
              (refused or {}).get("status") == "error", str(refused)[:200])

        refused_app = await rt._exec_chat_threads(
            {"op": "toc", "kind": "app", "ref": "workbench"},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        check("5. op='toc' on kind='app', ref='workbench' is REFUSED, not "
              "silently answered with the legacy scope",
              (refused_app or {}).get("status") == "error",
              str(refused_app)[:200])

        # kind omitted, foreign ref: a thread ref is a uuid or 'original';
        # anything else is refused rather than answered with an empty index.
        refused_bare = await rt._exec_chat_threads(
            {"op": "toc", "ref": "workbench"},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        check("5. op='toc' with kind omitted and ref='workbench' is REFUSED",
              (refused_bare or {}).get("status") == "error",
              str(refused_bare)[:200])

        # ── 6. episodes answer about the REF, not the caller ──────────────
        # The session arm of memory_engine._turn_row_where used to match the
        # CALLER's session_id and ignore the descriptor's ref, so this call —
        # a host asking about a voice session from a chat turn — returned []
        # for every caller but one. Pre-PR the op matched {kind, ref} exactly
        # (git show origin/main:brain/mastercode/runtime/tools.py:6760+).
        for i in range(3):
            await _seed_session_turn(pool, VOICE_SESSION, i)
        sess_eps = await rt._exec_chat_threads(
            {"op": "episodes", "kind": "session", "ref": VOICE_SESSION},
            entity_id=TEST_EID, pool=pool, session_id=CALLER_SESSION,
            thread_id=a_id)
        sess_list = ((sess_eps or {}).get("result") or {}).get("episodes") or []
        check("6. op='episodes' on a kind='session' ref returns THAT session's "
              "rows when the caller's session id is different",
              (sess_eps or {}).get("status") == "ok"
              and sum(int(e["turn_count"]) for e in sess_list) == 3,
              str(sess_eps)[:300])

        # An episode is a transcript window, and `op='read'` has never applied
        # recall's supersession filter — so an episode must count a superseded
        # turn row or the two disagree about the same window.
        thread_c = await ct.create_thread(pool, TEST_EID, title="Gamma")
        c_id = thread_c["id"]
        c_turns = [await _seed_turn(pool, c_id, i) for i in range(2)]
        await pool.execute(
            "UPDATE entity_memory SET t_valid_until = NOW() - interval '1 minute' "
            " WHERE id = $1", c_turns[0])
        c_eps = await kt.scope_episodes(pool, TEST_EID, ct.descriptor_for(c_id),
                                        session_id=SESSION)
        c_read = await rt._exec_chat_threads(
            {"op": "read", "kind": "thread", "ref": c_id},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=c_id)
        c_rows = ((c_read or {}).get("result") or {}).get("turns") or []
        check("6. an episode over a SUPERSEDED turn row counts it, and agrees "
              "with an op='read' of the same window",
              sum(int(e["turn_count"]) for e in c_eps) == 2 == len(c_rows),
              f"episodes={[e['turn_count'] for e in c_eps]} read={len(c_rows)}")

        # ── 7. the block is cached on the scope's newest row ───────────────
        kt._BLOCK_CACHE.clear()
        cpool = _CountingPool(pool)
        miss = await kt.render_block(cpool, TEST_EID, a_id, session_id=SESSION)
        miss_sql = list(cpool.calls)
        check("7. the cold render still produces the block",
              miss == block, repr(miss)[:200])
        # The third full-scope count is gone: count_scope_turns is the only
        # query that sums content length, so its absence pins the dedup.
        check("7. a render counts the scope's turn rows ONCE (no "
              "count_scope_turns recount behind thread_weight)",
              not [q for q in miss_sql if "sum(length(content))" in q],
              f"{len([q for q in miss_sql if 'sum(length(content))' in q])} recounts "
              f"of {len(miss_sql)} queries")
        cpool.calls.clear()
        hit = await kt.render_block(cpool, TEST_EID, a_id, session_id=SESSION)
        hit_sql = list(cpool.calls)
        check("7. a second render with no new rows reads ONLY the stamp — no "
              "weight, artifact or trail query",
              hit == miss and len(hit_sql) == 1
              and "max(created_at)" in hit_sql[0],
              f"{len(hit_sql)} queries: {[q.strip()[:60] for q in hit_sql]}")
        # ...and a new row in the scope invalidates it.
        await _seed_turn(pool, a_id, 99)
        cpool.calls.clear()
        after = await kt.render_block(cpool, TEST_EID, a_id, session_id=SESSION)
        check("7. one new turn in the scope MISSES the cache and re-renders",
              len(cpool.calls) > 1 and f"{SEEDED_TURNS + 1} messages" in after,
              f"{len(cpool.calls)} queries; {after.splitlines()[2:3]}")

        # ── 8. an uppercase thread ref is the same thread ─────────────────
        await pool.execute(
            "DELETE FROM circles_thread_links WHERE entity_id=$1", TEST_EID)
        upper = await rt._exec_chat_threads(
            {"op": "toc", "kind": "thread", "ref": a_id.upper()},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        upper_payload = (upper or {}).get("result") or {}
        check("8. op='toc' on an UPPERCASE thread ref returns the same "
              "populated index, not empty sections",
              (upper or {}).get("status") == "ok"
              and bool(upper_payload.get("episodes"))
              and bool(upper_payload.get("goals", {}).get("open")),
              str(upper)[:300])
        check("8. ...and the thread is not listed as its own RELATED",
              all(str(r.get("id")) != a_id
                  for r in (upper_payload.get("related") or [])),
              json.dumps(upper_payload.get("related")))
        upper_read = await rt._exec_chat_threads(
            {"op": "read", "kind": "thread", "ref": a_id.upper()},
            entity_id=TEST_EID, pool=pool, session_id=SESSION, thread_id=a_id)
        n_self = await pool.fetchval(
            "SELECT count(*) FROM circles_thread_links WHERE entity_id=$1",
            TEST_EID)
        check("8. an UPPERCASE self-read writes no link (the compare is on "
              "canonical ids, not on spelling)",
              (upper_read or {}).get("status") == "ok" and n_self == 0,
              f"links={n_self}")

        # ── 9. no value can break the line cap ────────────────────────────
        # A title is free text the user types. One newline in it turns one
        # element of the rendered list into two lines and the twelve-line cap
        # stops holding, so every interpolated value is flattened.
        await ct.patch_thread(pool, TEST_EID, a_id,
                              title="Alpha\nINJECTED SECOND LINE\nAND A THIRD")
        kt._BLOCK_CACHE.clear()  # a rename moves no row, so nothing else would
        nl_block = await kt.render_block(pool, TEST_EID, a_id, session_id=SESSION)
        nl_lines = nl_block.split("\n")
        check("9. a newline-bearing title cannot add lines to the block",
              0 < len(nl_lines) <= kt.BLOCK_MAX_LINES,
              f"{len(nl_lines)} lines: {nl_lines}")
        check("9. ...and the title still reads, flattened onto its own line",
              any("Alpha INJECTED SECOND LINE AND A THIRD" in ln
                  for ln in nl_lines), nl_block)
        await ct.patch_thread(pool, TEST_EID, a_id, title="Alpha")
        kt._BLOCK_CACHE.clear()

        # ── the legacy scope renders nothing when it has nothing to say ───
        legacy_block = await kt.render_block(
            pool, TEST_EID, None, session_id=SESSION)
        check("the quiet legacy scope pushes ZERO prompt bytes",
              legacy_block == "", repr(legacy_block)[:200])

    finally:
        try:
            await pool.execute(
                "DELETE FROM circles_thread_links WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "UPDATE circles_threads SET parent_thread_id=NULL "
                " WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM circles_threads WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM user_context WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM entity_memory WHERE entity_id=$1", TEST_EID)
            await pool.execute(
                "DELETE FROM entity_registry WHERE id=$1", TEST_EID)
        except Exception:
            pass
        await pool.close()

    if failures:
        print(f"  [knowledge-toc] {len(failures)} FAILURES: {failures}")
        return len(failures)
    print("  [knowledge-toc] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
