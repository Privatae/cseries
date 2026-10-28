#!/usr/bin/env python3
"""Memory integrity against a REAL database (2026-09-26).

scripts/test_memory_integrity.py holds the logic with fakes; this runs the SQL
the fakes stand in for, on the live schema (init-db.sql + migrations/v*.sql,
v187 included):

  1. the thread's set — retrieve(source_context=…) returns this thread's rows
     plus the entity-wide person types, never another thread's (a preference
     scoped elsewhere included); the turn-scope clause recall reads through;
     a Host-set scope surviving a restatement; with
     count_access=False nothing is counted; without a scope, everything
     comes back as before
  2. the retired types (migrations/v235_one_memory.sql, which ran the old
     boot archive/retype pass for the last time) — the CHECK refuses a row
     of a retired type; a 'retired' row keeps its old name in metadata
  3. the audit is deterministic only — judged findings an earlier cut left
     open are never shown and are closed once; a cross-thread finding expires
  4. the cross-thread scan reads prompt_log's item_meta and records a finding
     only for another thread's own words
  5. the thread's memory block leads with its open findings
  6. "Things You Were Taught" — the per-turn block, the standalone teaching
     block and keyword teaching retrieval — carries live rows only: an
     archived or superseded rule never reaches the prompt; nor does an
     archived or corrected rule of the constitution

In-container (smoke): APP_ROOT=/app python3 /app/scripts/test_memory_integrity_db.py
Last line: "OK …" or "FAIL …". Seeds a synthetic entity; always cleans up.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")

# Synthetic; never a real entity. A runner that may overlap another run on the
# same database (a mutation harness) names its own, so two runs never share one.
TEST_EID = int(os.environ.get("MEMORY_INTEGRITY_TEST_EID") or 99331)
A = {"kind": "project", "ref": "9933101"}
B = {"kind": "app", "ref": "circles"}

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [memory-integrity-db] OK {name}")
    else:
        FAILURES.append(name)
        print(f"  [memory-integrity-db] FAIL {name} {detail}")


async def _row(pool, content, mtype, *, scope=None, meta=None, importance=1.0):
    md = dict(meta or {})
    if scope:
        md["source_context"] = scope
    return await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, importance, "
        "metadata) VALUES ($1, $2, $3, 'active', $4, $5::jsonb) "
        "RETURNING id", TEST_EID, content, mtype, importance, json.dumps(md))


async def _clean(pool) -> None:
    for sql in ("DELETE FROM entity_memory WHERE entity_id=$1",
                "DELETE FROM memory_audit_findings WHERE entity_id=$1",
                "DELETE FROM prompt_log WHERE entity_id=$1",
                "DELETE FROM circles_thread_links WHERE entity_id=$1",
                "DELETE FROM circles_threads WHERE entity_id=$1"):
        try:
            await pool.execute(sql, TEST_EID)
        except Exception as e:  # noqa: BLE001
            print(f"  [memory-integrity-db] cleanup: {e}")


def _section(text: str, head: str) -> str:
    """The lines of one '### head' section of a memory block."""
    if f"### {head}" not in text:
        return ""
    return text.split(f"### {head}", 1)[1].split("\n###", 1)[0]


async def _rules_are_preferences(pool, eng) -> None:
    """7. Rules are preferences (host harness plan, after Phase 2;
    2026-09-30). A rule is a preference marked rule_kind (v235 moved the
    last skill_memory rules); rules ride in "Things You Were Taught" by
    weight and scope, never in the preference sections; no automatic path
    retires one; the Host corrects or drops one in the open and a correction
    stays a rule; the writers write rules."""
    from datetime import timedelta
    from mastercode import circles_threads as ct
    from mastercode.memory_consolidation import MemoryConsolidator
    from mastercode.memory_engine import active_embedding_column, embedding_cast_type
    TAUGHT = {"trigger": "remember this:", "skill_memory": True}
    LEARNED = {"trigger": "entity_learned", "skill_id": "learned/7001", "skill_memory": True}
    GOV = {"source": "governance_pre_seed"}

    async def _kind(mid):
        r = await pool.fetchrow(
            "SELECT memory_type, metadata->>'rule_kind' AS rk, metadata->>'prior_memory_type' AS prior, "
            "status, t_valid_until IS NULL AS live, importance FROM entity_memory WHERE id = $1", mid)
        return dict(r) if r else {}

    # 7a. the rule shape. The boot pass that moved taught and learned
    # skill_memory rows to preference rules ran its last time as
    # migrations/v235_one_memory.sql (§2 pins its CHECK): the rows below are
    # what it leaves — rules as preferences marked with their kind, every
    # other skill_memory row 'retired' and archived, its old name kept.
    t = await _row(pool, "when a tool fails read its source first", "preference", scope=A,
                   meta={**TAUGHT, "rule_kind": "taught",
                         "held_in": [{**A, "at": "2026-09-30T00:00:00+00:00", "taught": True}]},
                   importance=2.0)
    le = await _row(pool, "verify a build's terminal status before reporting it", "preference",
                    meta={**LEARNED, "rule_kind": "learned"}, importance=1.2)
    gv = await _row(pool, "no emoji in plans", "retired",
                    meta={**GOV, "prior_memory_type": "skill_memory"}, importance=1.5)
    other = await _row(pool, "research a topic with the web_search tool", "retired",
                       meta={"prior_memory_type": "skill_memory"}, importance=3.0)
    arch = await _row(pool, "an archived taught rule", "retired",
                      meta={**TAUGHT, "prior_memory_type": "skill_memory"})
    await pool.execute("UPDATE entity_memory SET status = 'archived' WHERE id = ANY($1::int[])",
                       [gv, other, arch])
    # Rules written as rules carry their kind: a taught one, a memory_curate
    # correction of a learned one, a thread delete's restatement of one.
    from mastercode.runtime import tools as _rb_tools
    await eng._store_teaching_memory(TEST_EID, {"content": "a rule taught after the ship",
                                                "trigger": "remember this:"})
    post = await pool.fetchval("SELECT id FROM entity_memory WHERE entity_id = $1 "
                               "AND content = 'a rule taught after the ship'", TEST_EID)
    to_fix = await _row(pool, "a learned rule to correct", "preference",
                        meta={"rule_kind": "learned"}, importance=0.5)
    fixed = (await _rb_tools._curate_replace(pool, MemoryConsolidator(pool), TEST_EID, to_fix,
                                             "a learned rule, corrected", "", None, None,
                                             "rules-kind")).get("memory_id")
    rb_th = await ct.create_thread(pool, TEST_EID, title="rules kind thread")
    await _row(pool, "a learned rule said in the kind thread", "preference",
               scope=ct.descriptor_for(str(rb_th["id"])), importance=0.5,
               meta={"rule_kind": "learned",
                     "held_in": [{**A, "at": "2026-09-30T00:00:00+00:00",
                                  "words": "the kind thread's rule restated"}]})
    await ct.delete_thread(pool, TEST_EID, str(rb_th["id"]))
    restated = await pool.fetchval("SELECT id FROM entity_memory WHERE entity_id = $1 AND status = 'active' "
                                   "AND content = 'the kind thread''s rule restated'", TEST_EID)
    kinds = {m: await _kind(m) for m in (post, fixed, restated) if m is not None}
    check("rules: a rule written since is a preference marked with its kind — a taught one, a curate "
          "correction and a thread delete's restatement of a learned one",
          None not in (post, fixed, restated)
          and [(kinds[m].get("memory_type"), kinds[m].get("rk")) for m in (post, fixed, restated)]
          == [("preference", "taught"), ("preference", "learned"), ("preference", "learned")],
          f"{post} {fixed} {restated} {kinds}")
    await pool.execute("DELETE FROM entity_memory WHERE entity_id = $1 AND NOT (id = ANY($2::int[]))",
                       TEST_EID, [t, le, gv, other, arch])
    await pool.execute("DELETE FROM circles_thread_links WHERE entity_id = $1", TEST_EID)
    await pool.execute("DELETE FROM circles_threads WHERE entity_id = $1", TEST_EID)

    # 7b. where rules ride: "Things You Were Taught", by weight, both shapes;
    # a rule decided about a place only there; never in the preference
    # sections (standing list or pre-fire), even when the message matches it.
    await _row(pool, "prefers tables over prose", "preference")
    placed = await _row(pool, "invoices go out on the first", "preference",
                        meta={"rule_kind": "taught", "about": dict(A)}, importance=2.0)
    thr_place = ct.descriptor_for(str((await ct.create_thread(
        pool, TEST_EID, title="rules place thread"))["id"]))
    await _row(pool, "invoices in this thread go out weekly", "preference",
               meta={"rule_kind": "taught", "about": dict(thr_place)}, importance=2.0)
    # A fourth and a fifth rule fill A's taught five (the retired skill row
    # 7a left, archived, takes no place); a sixth, lighter one is in no
    # taught block there, and still in no preference section.
    await _row(pool, "a fourth rule that fills the taught list", "preference",
               meta={"rule_kind": "taught"}, importance=1.1)
    await _row(pool, "a fifth rule that fills the taught list", "preference",
               meta={"rule_kind": "taught"}, importance=1.0)
    await _row(pool, "a quiet rule below the taught five", "preference",
               meta={"rule_kind": "taught"}, importance=0.3)
    blocks = {}
    for name, sc in (("A", A), ("B", B), ("T", thr_place)):
        blocks[name] = await eng.build_memory_context(
            TEST_EID, {}, max_tokens=4000, query="when a tool fails read its source first",
            source_context=sc) or ""
    taught_a, taught_b = _section(blocks["A"], "Things You Were Taught"), _section(blocks["B"], "Things You Were Taught")
    taught_t = _section(blocks["T"], "Things You Were Taught")
    check("rules: they ride in Things You Were Taught, taught and learned alike; a retired skill row never",
          "read its source first" in taught_a and "terminal status" in taught_a and "web_search" not in taught_a,
          blocks["A"][:1500])
    check("rules: a rule decided about a place rides only there",
          "invoices go out on the first" in taught_a and "invoices go out on the first" not in taught_b,
          f"A={taught_a!r} B={taught_b!r}")
    check("rules: a rule decided about a thread rides only in that thread",
          "go out weekly" in taught_t and "go out weekly" not in taught_a
          and "go out weekly" not in taught_b and "read its source first" in taught_t,
          f"T={taught_t!r} A={taught_a!r}")
    # The keyword reader and the standalone block (the timeline path) apply
    # the same place rule: with no scope, only the rules that ride everywhere.
    kw_q = "invoices go out tool source"

    async def _kw(sc):
        return " | ".join(r["content"] for r in await eng.retrieve_teaching(
            TEST_EID, kw_q, limit=10, source_context=sc))
    kw_none, kw_a, kw_t = await _kw(None), await _kw(A), await _kw(thr_place)
    check("rules: keyword teaching retrieval reads a placed rule only in its place, none with no scope",
          "read its source first" in kw_none and "go out on the first" not in kw_none
          and "go out weekly" not in kw_none
          and "go out on the first" in kw_a and "go out weekly" not in kw_a
          and "go out weekly" in kw_t and "go out on the first" not in kw_t,
          f"none={kw_none!r} A={kw_a!r} T={kw_t!r}")
    sb_none = await eng._build_teaching_block(TEST_EID, "", 4000) or ""
    sb_a = await eng._build_teaching_block(TEST_EID, "", 4000, source_context=A) or ""
    sb_t = await eng._build_teaching_block(TEST_EID, "", 4000, source_context=thr_place) or ""
    check("rules: the standalone teaching block reads a placed rule only in its place, none with no scope",
          "read its source first" in sb_none and "go out on the first" not in sb_none
          and "go out weekly" not in sb_none
          and "go out on the first" in sb_a and "go out weekly" not in sb_a
          and "go out weekly" in sb_t and "go out on the first" not in sb_t,
          f"none={sb_none!r} A={sb_a!r} T={sb_t!r}")
    # With a conversation context, both readers go through the keyword
    # reader, and hand it the turn's place.
    sk_none = await eng._build_teaching_block(TEST_EID, kw_q, 4000) or ""
    sk_a = await eng._build_teaching_block(TEST_EID, kw_q, 4000, source_context=A) or ""
    ck_a = _section(await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, conversation_context=kw_q,
                                                   source_context=A) or "", "Things You Were Taught")
    ck_b = _section(await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, conversation_context=kw_q,
                                                   source_context=B) or "", "Things You Were Taught")
    check("rules: with a conversation context, the standalone block and the per-turn block read a "
          "placed rule only in its place",
          "go out on the first" in sk_a and "go out on the first" not in sk_none
          and "go out on the first" in ck_a and "go out on the first" not in ck_b
          and "read its source first" in ck_b,
          f"sk_none={sk_none!r} sk_a={sk_a!r} ck_a={ck_a!r} ck_b={ck_b!r}")
    prefs_a = _section(blocks["A"], "Their Preferences") + _section(blocks["A"], "Relevant Right Now")
    check("rules: never in the preference sections, even when the message matches one",
          "prefers tables over prose" in prefs_a
          and not any(w in prefs_a for w in ("read its source first", "invoices go out", "terminal status",
                                             "a quiet rule below the taught five"))
          and "a quiet rule below the taught five" not in blocks["A"],
          prefs_a)
    await _clean(pool)

    # 7b'. the pre-fire leaves rules out IN the recall query, so rules the
    # message matches never use up its slots; `recall` as a tool still finds
    # them.
    from mastercode.memory_retrieval import federated_recall
    G0 = {"about": {"kind": "global"}}
    for i in range(12):   # fill the standing list, so only the pre-fire can show the target
        await _row(pool, f"likes topic {i} discussed plainly", "preference", meta=dict(G0), importance=2.0)
    target = "prefers harness failures reported with the raw error text"
    await _row(pool, target, "preference", meta=dict(G0), importance=0.3)
    for i in range(8):
        await _row(pool, f"rule {i}: when a harness failure shows a raw error read the error first",
                   "preference", meta={"rule_kind": "learned", "trigger": "entity_learned"}, importance=1.2)
    pq = "the harness failure raw error"
    pblock = await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, query=pq, source_context=B) or ""
    relevant = _section(pblock, "Relevant Right Now")
    check("rules: a relevant preference still pre-fires beside eight rules the message matches",
          target in relevant and "read the error first" not in relevant, pblock[:1500])
    tool = await federated_recall(pool, TEST_EID, pq, scope="memory")
    tool_hits = [h.get("content") or "" for h in (tool.get("memory") or {}).get("hits", [])]
    pf = await federated_recall(pool, TEST_EID, pq, scope="memory", exclude_rules=True)
    pf_hits = [h.get("content") or "" for h in (pf.get("memory") or {}).get("hits", [])]
    check("rules: recall as a tool still finds rules; only the pre-fire's call leaves them out",
          any("read the error first" in c for c in tool_hits)
          and not any("read the error first" in c for c in pf_hits) and target in pf_hits,
          f"tool={tool_hits} prefire={pf_hits}")
    await _clean(pool)

    # Copies of the taught block's words never take a pre-fire slot: dropped
    # before the limit (recall_memories skip_words), six distinct
    # preferences still show beside three copies that outrank them.
    # (The copies say the query twice and weigh more, so they rank first.)
    for i in range(3):
        rw = f"harness failure raw error, harness failure raw error, rule {i}"
        await _row(pool, rw, "preference", meta={"rule_kind": "taught"}, importance=2.0)
        await _row(pool, rw, "preference", meta={"about": {"kind": "global"}}, importance=2.0)
    for i in range(6):
        await _row(pool, f"harness failure raw error note {i}", "preference",
                   meta={"about": {"kind": "global"}}, importance=0.5)
    sblk = await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, query="harness failure raw error",
                                          source_context=B) or ""
    srel = _section(sblk, "Relevant Right Now")
    check("rules: copies of the taught rules' words never take a pre-fire slot: six distinct "
          "preferences still show",
          sum(f"harness failure raw error note {i}" in srel for i in range(6)) == 6
          and ", rule " not in srel
          and all(sblk.count(f"harness failure raw error, harness failure raw error, rule {i}") == 1
                  for i in range(3)), srel)
    await _clean(pool)

    # 7b''. "Remember this:" on words held as a plain preference teaches them
    # as any rule is taught: a fresh taught rule, now, undecided and read
    # everywhere, the plain preference retired into it WITHOUT its decision
    # (a preference about project A, taught in conversation B, rides in A, B
    # and C). The preference is OLDER than five taught rules; the rule still
    # renders, out of the preference sections. Deleting the conversation that
    # taught it brings the plain preference back.
    C = {"kind": "project", "ref": "9933103"}
    up = await _row(pool, "invoices go by post", "preference",
                    meta={"about": dict(A), "scoped_by": "host", "source_context": dict(A)})
    await pool.execute("UPDATE entity_memory SET created_at = now() - interval '10 days' "
                       "WHERE id = $1", up)
    for i in range(5):
        r5 = await _row(pool, f"standing taught rule number {i} about replies", "preference",
                        meta={"rule_kind": "taught", "trigger": "remember this:"}, importance=2.0)
        await pool.execute("UPDATE entity_memory SET created_at = now() - interval '2 days' "
                           "WHERE id = $1", r5)
    ub0 = await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, query="what is on the agenda",
                                         source_context=A) or ""
    teach_th = await ct.create_thread(pool, TEST_EID, title="rules teaching thread")
    teach_sc = ct.descriptor_for(str(teach_th["id"]))
    taught_row = await eng._store_teaching_memory(TEST_EID, {"content": "invoices go by post",
                                                             "trigger": "remember this:"},
                                                  source_context=teach_sc)
    ub1 = await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, query="what is on the agenda",
                                         source_context=A) or ""
    rides = {}
    for n, sc in (("B", teach_sc), ("C", C)):
        rides[n] = _section(await eng.build_memory_context(
            TEST_EID, {}, max_tokens=4000, query="what is on the agenda", source_context=sc) or "",
            "Things You Were Taught")
    new_rule = int((taught_row or {}).get("id") or 0)
    uk, nk = await _kind(up), await _kind(new_rule)
    ptr = await pool.fetchval("SELECT resolution_pointer FROM entity_memory WHERE id = $1", up)
    nabout = await pool.fetchval("SELECT metadata->'about' FROM entity_memory WHERE id = $1", new_rule)
    check("rules: Remember this: on an older plain preference writes a fresh taught rule that renders "
          "in Things You Were Taught beside five newer taught rules; the preference is retired into it",
          "invoices go by post" in _section(ub0, "Their Preferences")
          and "invoices go by post" in _section(ub1, "Things You Were Taught")
          and "invoices go by post" not in _section(ub1, "Their Preferences")
          and new_rule != up and nk.get("rk") == "taught" and float(nk.get("importance") or 0) == 2.0
          and not uk["live"] and uk["rk"] is None and ptr == new_rule,
          f"{uk} {nk} ptr={ptr} before={ub0[:600]!r} after={ub1[:600]!r}")
    check("rules: a plain preference about project A, taught in conversation B, becomes a rule nobody "
          "has scoped that rides in A, B and C",
          nabout is None and "invoices go by post" in _section(ub1, "Things You Were Taught")
          and all("invoices go by post" in rides[n] for n in ("B", "C")),
          f"about={nabout} rides={rides}")
    await ct.delete_thread(pool, TEST_EID, str(teach_th["id"]))
    check("rules: deleting the conversation that taught it brings the plain preference back",
          (await _kind(up))["live"] and (await _kind(new_rule)).get("status") == "archived",
          f"{await _kind(up)} {await _kind(new_rule)}")
    await _clean(pool)
    # A teaching retires every plain copy the rule is read in every turn of,
    # typed turns included. The fresh rule rides every normal turn; a
    # project-scoped type reads it only by its project tag (typed_rule_clause,
    # the pre-fire's own test), so the first project copy gives the rule its
    # tag and is retired. A copy about a second project stays live: a rule
    # carries one tag, and retiring it would take the words from that
    # project's typed turns. A copy no typed turn reads is retired.
    async def _typed(proj_ref, query):
        return await eng.build_memory_context(
            TEST_EID, {}, max_tokens=4000, query=query, source_context=B,
            context={"memory": "projects", "projects": [int(proj_ref)], "documents": False}) or ""

    st_words = "statements go by post"
    cp_a = await _row(pool, st_words, "preference",
                      meta={"about": dict(A), "scoped_by": "host", "source_context": dict(A)})
    cp_c = await _row(pool, st_words, "preference",
                      meta={"about": dict(C), "scoped_by": "host", "source_context": dict(C)})
    cp_t = await _row(pool, st_words, "preference", scope={"kind": "app", "ref": "finance"})
    typed_c_before = await _typed(C["ref"], st_words)
    two = int((await eng._store_teaching_memory(
        TEST_EID, {"content": st_words, "trigger": "remember this:"},
        source_context=B) or {}).get("id") or 0)
    ptrs = {m: await pool.fetchval("SELECT resolution_pointer FROM entity_memory WHERE id = $1", m)
            for m in (cp_a, cp_c, cp_t)}
    two_tag = await pool.fetchval("SELECT project_id FROM entity_memory WHERE id = $1", two)
    typed_a, typed_c = await _typed(A["ref"], st_words), await _typed(C["ref"], st_words)
    check("rules: a teaching retires the copy about A (the rule takes A's project tag) and the copy no "
          "typed turn reads; the copy about C stays live, and C's typed turn still reads the words",
          two and ptrs == {cp_a: two, cp_c: None, cp_t: two} and two_tag == int(A["ref"])
          and (await _kind(cp_c))["live"] and (await _kind(two))["rk"] == "taught"
          and st_words in typed_c_before and st_words in _section(typed_a, "Things You Were Taught")
          and st_words in _section(typed_c, "Relevant Right Now")
          and st_words not in _section(typed_c, "Things You Were Taught"),
          f"two={two} tag={two_tag} ptrs={ptrs} A={typed_a[:500]!r} C={typed_c[:500]!r}")
    # The same words never render twice in one turn: in C's ordinary turn the
    # rule rides in Things You Were Taught and the live copy about C is
    # skipped by both preference sections (the pre-fire, when the message
    # matches it, and the standing list); A's turn shows them once too.
    once = {}
    for name, sc, q in (("C asked", C, st_words), ("C", C, "what is on the agenda"),
                        ("A asked", A, st_words), ("A", A, "what is on the agenda")):
        blk = await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, query=q, source_context=sc) or ""
        once[name] = (blk.count(st_words), st_words in _section(blk, "Things You Were Taught"))
    check("rules: the same words never render twice in a turn: C's and A's ordinary turns show them once, "
          "in Things You Were Taught, while C's project-scoped turn still reads them",
          all(v == (1, True) for v in once.values()) and st_words in _section(typed_c, "Relevant Right Now"),
          f"{once}")
    # The same words differently cased or spaced are the same words
    # (same_words_key): a live copy so written is skipped too.
    variant = await _row(pool, "STATEMENTS go  by post", "preference", meta={"about": {"kind": "global"}})
    vblk = await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, query="statements by post",
                                          source_context=C) or ""
    check("rules: a preference in a rule's words, cased or spaced otherwise, is not rendered beside the rule",
          "STATEMENTS go  by post" not in vblk and st_words in _section(vblk, "Things You Were Taught"),
          f"{vblk[:700]!r}")
    await pool.execute("DELETE FROM entity_memory WHERE id = $1", variant)
    # Every surface that shows rules and preferences together says which
    # copy a rule holds: memory_scope's list, recall, the Memory app.
    from mastercode.memory_retrieval import federated_recall as _fr8, format_recall_text as _frt8
    from mastercode.runtime import tools as _tools8
    import server.brain_service as _bs8
    from server.routes import brain as _brain8
    listed = "\n".join(sum((await _tools8._memory_scope_list(pool, TEST_EID))["result"]["by_scope"].values(), []))
    recalled = _frt8(await _fr8(pool, TEST_EID, st_words, scope="memory"))
    _saved8 = (_bs8.get_pool, getattr(_bs8, "_entity_id", None))
    # The Memory app's own request: oracle is kernel-native, so the route's
    # audience (_memory_audience) is the full corpus.
    _memory_app8 = type("R", (), {"state": type("S", (), {"app_token": type(
        "T", (), {"app_id": "oracle", "permissions": ["read:brain_memory"]})()})()})()
    _bs8.get_pool, _bs8._entity_id = (lambda: pool), TEST_EID
    try:
        app_copy, app_rule = (await _brain8.get_memory_detail(cp_c, _memory_app8),
                              await _brain8.get_memory_detail(two, _memory_app8))
    finally:
        _bs8.get_pool, _bs8._entity_id = _saved8
    check("rules: memory_scope's list, recall and the Memory app each say the copy about C is held by the rule",
          f"#{cp_c} [held by rule #{two}] {st_words}" in listed and f"#{two} [rule] {st_words}" in listed
          and f"held by rule #{two}" in recalled and f"#{two} | preference | rule" in recalled
          and app_copy.get("held_by_rule") == two and app_rule.get("rule_kind") == "taught"
          and app_rule.get("held_by_rule") is None,
          f"list={listed!r} recall={recalled[:600]!r} app={app_copy.get('held_by_rule')} {app_rule.get('rule_kind')}")
    await _clean(pool)

    # A project-scoped type reads rules by exactly the pre-fire's project test.
    # A copy tagged with project X (said there), then a teaching: X's typed
    # turn still reads the words, in the rule. A rule nobody decided about or
    # tagged rides every normal turn and no typed one (as an untagged plain
    # preference); one decided about X is read in X's typed turn, not Y's; one
    # decided global in both (as a global plain preference is: measured, the
    # pre-fire's clause admits a decision about the person).
    X, Y = A["ref"], C["ref"]
    tagged = await _row(pool, "deliveries leave from dock two", "preference", scope=dict(A))
    await pool.execute("UPDATE entity_memory SET project_id = $2 WHERE id = $1", tagged, int(X))
    x_before = await _typed(X, "deliveries leave from dock two")
    x_rule = int((await eng._store_teaching_memory(
        TEST_EID, {"content": "deliveries leave from dock two", "trigger": "remember this:"},
        source_context=B) or {}).get("id") or 0)
    x_after = await _typed(X, "deliveries leave from dock two")
    check("rules: a plain copy tagged with project X, then a teaching: X's typed turn still reads the words",
          "deliveries leave from dock two" in _section(x_before, "Relevant Right Now")
          and not (await _kind(tagged))["live"]
          and "deliveries leave from dock two" in _section(x_after, "Things You Were Taught"),
          f"rule={x_rule} before={x_before[:400]!r} after={x_after[:400]!r}")
    await _row(pool, "an everywhere rule nobody scoped", "preference", meta={"rule_kind": "taught"},
               importance=2.0)
    await _row(pool, "a rule decided global", "preference",
               meta={"rule_kind": "taught", "about": {"kind": "global"}}, importance=2.0)
    await _row(pool, "a global plain preference about dock work", "preference",
               meta={"about": {"kind": "global"}})
    await _row(pool, "a rule decided about project X", "preference",
               meta={"rule_kind": "taught", "about": dict(A)}, importance=2.0)
    tx, ty = await _typed(X, "global plain preference about dock work"), await _typed(Y, "dock work")
    normal = await eng.build_memory_context(TEST_EID, {}, max_tokens=4000, query="dock work",
                                            source_context=B) or ""
    tx_t, ty_t = _section(tx, "Things You Were Taught"), _section(ty, "Things You Were Taught")
    check("rules: a rule nobody decided about or tagged rides every normal turn and no project-scoped one",
          "an everywhere rule nobody scoped" in _section(normal, "Things You Were Taught")
          and "an everywhere rule nobody scoped" not in tx_t
          and "an everywhere rule nobody scoped" not in ty_t, f"X={tx_t!r} Y={ty_t!r}")
    check("rules: a rule decided about project X is read in X's typed turn and not in Y's",
          "a rule decided about project X" in tx_t and "a rule decided about project X" not in ty_t,
          f"X={tx_t!r} Y={ty_t!r}")
    check("rules: a rule decided global is read in a project-scoped turn, as a global plain preference is",
          "a rule decided global" in tx_t and "a rule decided global" in ty_t
          and "a global plain preference about dock work" in _section(tx, "Relevant Right Now"),
          f"X={tx[:600]!r}")
    # The keyword reader (a conversation context) applies the same test.
    kx = _section(await eng.build_memory_context(
        TEST_EID, {}, max_tokens=4000, conversation_context="rule decided everywhere scoped project",
        source_context=B, context={"memory": "projects", "projects": [int(X)], "documents": False}) or "",
        "Things You Were Taught")
    check("rules: with a conversation context, a project-scoped turn reads rules by the same test",
          "a rule decided about project X" in kx and "an everywhere rule nobody scoped" not in kx, f"{kx!r}")
    await _clean(pool)
    # A global plain copy: the fresh rule takes its decision about the person
    # (it adds no normal reach: the rule rides everywhere already), so every
    # project-scoped turn still reads the words, and the copy is retired.
    g_copy = await _row(pool, "keep answers in plain words", "preference",
                        meta={"about": {"kind": "global"}, "scoped_by": "host"})
    g_rule = int((await eng._store_teaching_memory(
        TEST_EID, {"content": "keep answers in plain words", "trigger": "remember this:"},
        source_context=B) or {}).get("id") or 0)
    g_about = json.loads(await pool.fetchval("SELECT metadata->'about' FROM entity_memory WHERE id = $1",
                                             g_rule) or "null")
    g_typed = _section(await _typed(Y, "keep answers in plain words"), "Things You Were Taught")
    check("rules: a global plain copy, then a teaching: the rule takes the decision about the person and "
          "every project-scoped turn still reads the words",
          not (await _kind(g_copy))["live"] and g_about == {"kind": "global"}
          and "keep answers in plain words" in g_typed, f"{g_about} {g_typed!r}")
    await _clean(pool)

    fact = await _row(pool, "the office closes on fridays", "cited_fact")
    goal = await _row(pool, "ship the invoice export", "goal")
    beside = {}
    for w, other in (("the office closes on fridays", fact), ("ship the invoice export", goal)):
        got = await eng._store_teaching_memory(TEST_EID, {"content": w, "trigger": "remember this:"},
                                               source_context=B)
        beside[w] = (got, await _kind(other))
    check("rules: Remember this: in the words of a cited fact or a goal stores a rule beside it, the "
          "fact and the goal left as they are",
          all(got and got.get("memory_type") == "preference" and got.get("id") != o_id
              and ok["live"] and ok["memory_type"] == t
              for (got, ok), o_id, t in zip(beside.values(), (fact, goal), ("cited_fact", "goal"))),
          f"{beside}")
    await _clean(pool)
    # The retirement is the teaching conversation's own act (superseded_in):
    # deleting the thread the plain preference was said in gives another
    # conversation's restatement of it a row retired as it was, not a live
    # plain copy beside the rule; deleting the teaching thread then brings
    # that restatement back.
    x_th = await ct.create_thread(pool, TEST_EID, title="plain said here")
    t_th = await ct.create_thread(pool, TEST_EID, title="taught here")
    p2 = await _row(pool, "parcels go by courier", "preference",
                    scope=ct.descriptor_for(str(x_th["id"])),
                    meta={"held_in": [{**A, "at": "2026-09-30T00:00:00+00:00",
                                       "words": "parcels go by courier every week"}]})
    r2 = int((await eng._store_teaching_memory(
        TEST_EID, {"content": "parcels go by courier", "trigger": "remember this:"},
        source_context=ct.descriptor_for(str(t_th["id"]))) or {}).get("id") or 0)
    await ct.delete_thread(pool, TEST_EID, str(x_th["id"]))

    async def _weekly():
        return [dict(r) for r in await pool.fetch(
            "SELECT id, t_valid_until IS NULL AS live, resolution_pointer AS ptr FROM entity_memory "
            "WHERE entity_id = $1 AND content = 'parcels go by courier every week' AND status = 'active'",
            TEST_EID)]
    wk1 = await _weekly()
    await ct.delete_thread(pool, TEST_EID, str(t_th["id"]))
    wk2 = await _weekly()
    check("rules: a plain preference retired by a teaching stays retired for the conversations that "
          "restated it when its own thread goes, and comes back when the teaching thread goes",
          (await _kind(p2))["status"] == "archived" and len(wk1) == 1 and not wk1[0]["live"]
          and wk1[0]["ptr"] == r2 and len(wk2) == 1 and wk2[0]["live"],
          f"p2={await _kind(p2)} r2={r2} after X={wk1} after T={wk2}")
    await _clean(pool)

    # 7c. no automatic path retires a rule (the merge and decay passes are
    # gone with slice 2; the supersession search is what is left).
    col, dim = await active_embedding_column(pool, TEST_EID)
    cast = embedding_cast_type(dim)
    evec = "[" + ",".join(["0.1"] * dim) + "]"

    async def _vrow(content, meta, age, mtype="preference", vec=None):
        return await pool.fetchval(
            f"INSERT INTO entity_memory (entity_id, content, memory_type, status, "
            f"metadata, created_at, {col}) VALUES ($1, $2, $5, 'active', "
            f"$3::jsonb, now() - $4::interval, $6::{cast}) RETURNING id",
            TEST_EID, content, json.dumps(meta), age, mtype, vec or evec)
    G = {"about": {"kind": "global"}}
    r_old = await _vrow("always send invoices as pdf files", {**G, "rule_kind": "taught"}, timedelta(days=2))
    r_new = await _vrow("always send invoices as pdf files please", dict(G), timedelta(0))
    cons = MemoryConsolidator(pool)
    cands = {c["id"] for c in await cons._find_candidates(
        TEST_EID, "always send invoices as pdf files", evec, "preference", k=10)}
    check("rules: a rule is never a supersession target",
          r_old not in cands and r_new in cands, f"{cands}")
    await _clean(pool)

    # 7d. memory_curate: the Host corrects or drops a rule in the open
    # (2026-09-30); a correction stays a rule, read where the rule was.
    import server.brain_service as _bs
    from mastercode.runtime import tools as _tools
    _saved_bs = (_bs.get_pool, getattr(_bs, "_entity_id", None))
    _bs.get_pool, _bs._entity_id = (lambda: pool), TEST_EID
    try:
        taught = await _row(pool, "answer in plain words", "preference", scope=A,
                            meta={"rule_kind": "taught"}, importance=2.0)
        # The corrected words already held as a GLOBAL plain preference: read
        # everywhere the rule was (close_turn._covers passes), and still not
        # what the rule is retired into — a rule is absorbed only by a rule.
        plain_dup = await _row(pool, "answer in very plain words", "preference", meta=dict(G))
        from mastercode.runtime.close_turn import _covers as _cov
        covers = await _cov(pool, TEST_EID, plain_dup, taught)
        cr = await _tools._exec_memory_curate({"op": "replace", "memory_id": taught, "about": "person",
                                               "content": "answer in very plain words"})
        new_id = (cr.get("result") or {}).get("memory_id")
        nk = await _kind(new_id) if new_id else {}
        nmd = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1",
                                             new_id or 0) or "{}")
        check("rules: a correction of a rule is a new rule, read everywhere the rule was, the old one "
              "retired — never into a plain preference in the same words, even one read everywhere",
              covers and cr.get("status") == "ok" and new_id != plain_dup
              and nk.get("rk") == "taught" and nk.get("memory_type") == "preference"
              and nmd.get("about") == {"kind": "global"} and not (await _kind(taught))["live"]
              and (await _kind(plain_dup))["live"] and (await _kind(plain_dup))["rk"] is None
              and float(nk.get("importance") or 0) == 2.0, f"{covers} {cr} {nk} {nmd}")
        cd = await _tools._exec_memory_curate({"op": "drop", "memory_id": new_id})
        check("rules: the Host may drop a rule with memory_curate (2026-09-30)",
              (cd.get("result") or {}).get("dropped") is True and not (await _kind(new_id))["live"], f"{cd}")
        await _clean(pool)

        # The writer: "Remember this:" writes rules. (<learned>, and the
        # skill events that read rules, left with the simulation 2026-10-17.)
        await eng._store_teaching_memory(TEST_EID, {"content": "invoices are due in 30 days",
                                                    "trigger": "remember this:"}, source_context=A)
        tk = await pool.fetchrow("SELECT memory_type, metadata->>'rule_kind' AS rk FROM entity_memory "
                                 "WHERE entity_id = $1 AND content = 'invoices are due in 30 days'", TEST_EID)
        plain = await _row(pool, "invoices go by email", "preference")
        await eng._store_teaching_memory(TEST_EID, {"content": "invoices go by email",
                                                    "trigger": "remember this:"}, source_context=B)
        live_email = [dict(r) for r in await pool.fetch(
            "SELECT id, metadata->>'rule_kind' AS rk FROM entity_memory WHERE entity_id = $1 "
            "AND content = 'invoices go by email' AND status = 'active' AND t_valid_until IS NULL",
            TEST_EID)]
        again_taught = await eng._store_teaching_memory(TEST_EID, {"content": "invoices go by email",
                                                                   "trigger": "remember this:"},
                                                        source_context=A)
        check("rules: Remember this: writes a taught rule; on a plain preference's words, one live taught "
              "rule replaces it; taught again, the rule holds them",
              tk and tk["memory_type"] == "preference" and tk["rk"] == "taught"
              and not (await _kind(plain))["live"] and len(live_email) == 1
              and live_email[0]["rk"] == "taught" and live_email[0]["id"] != plain
              and again_taught is None
              and await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 "
                                      "AND content = 'invoices go by email' AND status = 'active' "
                                      "AND t_valid_until IS NULL", TEST_EID) == 1,
              f"{tk} {live_email} {again_taught}")
        # A rule and two plain preferences already in the same words (one
        # plain older, one about another place): taught again, the rule holds
        # them, no second rule, and every plain copy is retired into it, as a
        # first teaching would — no plain copy outlives a teaching.
        both_plain = await _row(pool, "receipts go in the shared folder", "preference")
        both_rule = await _row(pool, "receipts go in the shared folder", "preference",
                               meta={"rule_kind": "taught"}, importance=2.0)
        both_plain_b = await _row(pool, "receipts go in the shared folder", "preference",
                                  meta={"about": dict(B), "scoped_by": "host"})
        both_again = await eng._store_teaching_memory(TEST_EID, {"content": "receipts go in the shared folder",
                                                                 "trigger": "remember this:"},
                                                      source_context=A)
        both_rules = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND content = 'receipts go in the "
            "shared folder' AND metadata ? 'rule_kind' AND t_valid_until IS NULL", TEST_EID)
        both_ptr = {m: await pool.fetchval("SELECT resolution_pointer FROM entity_memory WHERE id = $1", m)
                    for m in (both_plain, both_plain_b)}
        both_rule_about = await pool.fetchval("SELECT metadata->'about' FROM entity_memory WHERE id = $1",
                                              both_rule)
        check("rules: taught again, words held by a rule are held by it — no second rule — and every plain "
              "copy in those words is retired into it, without carrying a decision",
              both_again is None and both_rules == 1 and (await _kind(both_rule))["live"]
              and not (await _kind(both_plain))["live"] and not (await _kind(both_plain_b))["live"]
              and both_ptr == {both_plain: both_rule, both_plain_b: both_rule} and both_rule_about is None,
              f"{both_again} {both_rules} {both_ptr} {both_rule_about}")
        # A rule the Host narrowed to A: taught again, a plain copy about C
        # stays live (the rule is not read in C) and C still reads it; a
        # plain copy about A is retired into the rule. A teaching never
        # reduces where the words are read.
        from mastercode.memory_retrieval import _canonical_where as _cw
        C7 = {"kind": "project", "ref": "9933103"}
        nr_words = "quotes go out within a day"
        nr_rule = await _row(pool, nr_words, "preference", importance=2.0,
                             meta={"rule_kind": "taught", "about": dict(A), "scoped_by": "host"})
        nr_c = await _row(pool, nr_words, "preference", meta={"about": dict(C7), "scoped_by": "host"})
        nr_a = await _row(pool, nr_words, "preference", meta={"about": dict(A), "scoped_by": "host"})
        nr_f = await _row(pool, nr_words, "preference", scope={"kind": "app", "ref": "finance"})
        nr_again = await eng._store_teaching_memory(TEST_EID, {"content": nr_words, "trigger": "remember this:"},
                                                    source_context=B)
        cw_sql, cw_params = _cw(TEST_EID, memory_types=["preference"], turn_scope=C7)
        c_reads = [int(r["id"]) for r in await pool.fetch(
            f"SELECT id FROM entity_memory WHERE {cw_sql} AND content = ${len(cw_params) + 1}",
            *cw_params, nr_words)]
        nr_ptr_a = await pool.fetchval("SELECT resolution_pointer FROM entity_memory WHERE id = $1", nr_a)
        nr_tag = await pool.fetchval("SELECT project_id FROM entity_memory WHERE id = $1", nr_rule)
        check("rules: taught again, a rule the Host narrowed to A leaves a plain copy about C live (C still "
              "reads the words) and retires the copy about A into itself; a decided rule is given no tag",
              nr_again is None and (await _kind(nr_c))["live"] and c_reads == [nr_c] and nr_tag is None
              and (await _kind(nr_f))["live"]
              and not (await _kind(nr_a))["live"] and nr_ptr_a == nr_rule and (await _kind(nr_rule))["live"],
              f"{nr_again} C live={(await _kind(nr_c))['live']} C reads={c_reads} A ptr={nr_ptr_a}")
        # A repeat teaching of a rule the Host narrowed to A, said in D: the
        # rule rides in D too (a held_in record that reads), still in A, and
        # nowhere else (C).
        D9 = ct.descriptor_for(str((await ct.create_thread(pool, TEST_EID, title="taught again here"))["id"]))
        n9_words = "statements are sent on the fifth"
        n9 = await _row(pool, n9_words, "preference", importance=2.0,
                        meta={"rule_kind": "taught", "about": dict(A), "scoped_by": "host"})
        n9_again = await eng._store_teaching_memory(TEST_EID, {"content": n9_words, "trigger": "remember this:"},
                                                    source_context=D9)
        rides9 = {}
        for name, sc in (("A", A), ("C", C7), ("D", D9)):
            rides9[name] = n9_words in _section(await eng.build_memory_context(
                TEST_EID, {}, max_tokens=4000, query="what is on the agenda", source_context=sc) or "",
                "Things You Were Taught")
        n9_about = json.loads(await pool.fetchval("SELECT metadata->'about' FROM entity_memory WHERE id = $1",
                                                  n9) or "null")
        check("rules: taught again in D, a rule the Host narrowed to A rides in D too, still in A, and not in C",
              n9_again is None and rides9 == {"A": True, "C": False, "D": True} and n9_about == dict(A),
              f"{rides9} {n9_about}")
        # close_turn in D for those words, scoped to D: held already (the
        # rule reads in D: _held_row counts a rule's records), no new row.
        from mastercode.runtime import close_turn as _ct9b
        r9b = await _ct9b.apply(pool, TEST_EID, {"preferences": [
            {"text": n9_words, "reason": "said so", "scope": {"kind": "thread", "ref": str(D9["ref"])}}]},
            session_id="rules-n9b", turn_scope=D9)
        n9_rows = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND content = $2 "
                                      "AND status = 'active' AND t_valid_until IS NULL", TEST_EID, n9_words)
        check("rules: close_turn in D, where a narrowed rule was taught again, finds it held there already",
              ((r9b.get("preferences") or [{}])[0]).get("status") == "already_held" and n9_rows == 1,
              f"{r9b} rows={n9_rows}")
        # Taught again with no conversation to name: no place to keep, so the
        # record is ordinary and the narrowed rule does not ride everywhere.
        u9_words = "receipts are scanned the same day"
        u9 = await _row(pool, u9_words, "preference", importance=2.0,
                        meta={"rule_kind": "taught", "about": dict(A), "scoped_by": "host"})
        await eng._store_teaching_memory(TEST_EID, {"content": u9_words, "trigger": "remember this:"})
        u9_held = json.loads(await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1",
                                                 u9) or "[]")
        u9_c = u9_words in _section(await eng.build_memory_context(
            TEST_EID, {}, max_tokens=4000, query="what is on the agenda", source_context=C7) or "",
            "Things You Were Taught")
        check("rules: a narrowed rule taught again with no conversation to name gets no taught place and "
              "does not ride everywhere",
              not u9_c and not any(e.get("taught") for e in u9_held), f"{u9_held}")
        # close_turn in D naming thread T, for a rule decided about T: held
        # there, and D recorded as provenance only — the person did not teach
        # it in D, so it does not ride there (held_already counts a rule's
        # records as reach).
        T9 = await ct.create_thread(pool, TEST_EID, title="the rule's own thread")
        t9_words = "proposals are reviewed by two people"
        t9 = await _row(pool, t9_words, "preference", importance=2.0,
                        meta={"rule_kind": "taught", "about": ct.descriptor_for(str(T9["id"])),
                              "scoped_by": "host"})
        from mastercode.runtime import close_turn as _ct9
        r9 = await _ct9.apply(pool, TEST_EID, {"preferences": [
            {"text": t9_words, "reason": "said so", "scope": {"kind": "thread", "ref": str(T9["id"])}}]},
            session_id="rules-t9", turn_scope=D9)
        held9 = json.loads(await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1",
                                               t9) or "[]")
        d9_reads = t9_words in _section(await eng.build_memory_context(
            TEST_EID, {}, max_tokens=4000, query="what is on the agenda", source_context=D9) or "",
            "Things You Were Taught")
        check("rules: close_turn in D naming the thread a rule is decided about records D (an echo, not "
              "a teaching), and the rule does not ride in D",
              ((r9.get("preferences") or [{}])[0]).get("status") == "already_held"
              and len(held9) == 1 and not held9[0].get("taught") and not d9_reads, f"{r9} {held9} {d9_reads}")
        # The same covers test, in memory_curate: correcting a plain
        # preference to words a rule nobody scoped holds (read everywhere)
        # retires it into that rule instead of writing a second copy.
        cd_plain = await _row(pool, "answer briefly", "preference", scope=A)
        cd_rule = await _row(pool, "answer very briefly", "preference", scope=B, meta={"rule_kind": "taught"})
        cd = await _tools._curate_replace(pool, MemoryConsolidator(pool), TEST_EID, cd_plain,
                                          "answer very briefly", "", A, A, "rules-covers")
        check("rules: memory_curate correcting a plain preference to a rule's words retires it into the rule "
              "(read everywhere, it covers the preference)",
              cd.get("memory_id") == cd_rule and cd.get("replaced") == cd_plain
              and not (await _kind(cd_plain))["live"], f"{cd}")
        await _clean(pool)

        # The other readers: the constitution's "Relevant knowledge" (no
        # scope: never a rule decided for a place), the stale-rules probe, the
        # external identity block (never a rule), the Memory app's sweep.
        from mastercode.constitution.context import _retrieve_teaching
        from mastercode.runtime.diagnostics import default_probes
        from server.synaptive_handler import recent_identity_memories
        from server.routes import brain as _brain_routes
        everywhere = await _row(pool, "rule for every place", "preference", meta={"rule_kind": "taught"},
                                importance=2.0)
        one_place = await _row(pool, "rule for project A only", "preference",
                               meta={"rule_kind": "taught", "about": dict(A)}, importance=2.5)
        g_plain = await _row(pool, "prefers weekly summaries", "preference", meta=dict(G))
        await _row(pool, "a global rule said today", "preference", meta={"rule_kind": "taught", **G})
        await pool.execute("UPDATE entity_memory SET created_at = now() - interval '200 days' "
                           "WHERE id = $1", everywhere)
        facts = await _retrieve_teaching(pool, TEST_EID, "anything", max_facts=10)
        stale = await default_probes(pool, TEST_EID)["stale_rules"]()
        ident = [c for _t, c in await recent_identity_memories(pool, TEST_EID)]

        class _Req:
            async def json(self):
                return {"memory_type": "preference", "unretrieved_only": True, "reason": "sweep test"}
        swept = await _brain_routes.archive_memories(_Req())
        check("rules: the constitution's knowledge reads a rule that rides everywhere, never one decided for a place",
              "rule for every place" in facts and "rule for project A only" not in facts, f"{facts}")
        check("rules: the stale-rules probe reads rules as preferences",
              [r["id"] for r in stale] == [everywhere], f"{stale}")
        check("rules: the external identity block carries a global preference, never a rule",
              "prefers weekly summaries" in ident and "a global rule said today" not in ident, f"{ident}")
        check("rules: the Memory app's sweep of unretrieved preferences leaves the rules",
              swept.get("archived") == 1 and (await _kind(g_plain))["status"] == "archived"
              and (await _kind(everywhere))["status"] == "active"
              and (await _kind(one_place))["status"] == "active", f"{swept}")
        await _clean(pool)
    finally:
        _bs.get_pool, _bs._entity_id = _saved_bs

    # 7e. thread delete: a rule restated in other words elsewhere stays a
    # rule there; a dropped rule is linked only by a rule.
    th = await ct.create_thread(pool, TEST_EID, title="rules gate thread")
    tid = str(th["id"])
    base = await _row(pool, "rule said in the thread", "preference", scope=ct.descriptor_for(tid),
                      meta={"rule_kind": "learned",
                            "held_in": [{**A, "at": "2026-09-30T00:00:00+00:00",
                                         "words": "rule said again in project A"}]})
    await ct.delete_thread(pool, TEST_EID, tid)
    own = await pool.fetchrow("SELECT memory_type, metadata->>'rule_kind' AS rk, "
                              "metadata->'source_context' AS sc FROM entity_memory WHERE entity_id = $1 "
                              "AND content = 'rule said again in project A' AND status = 'active'", TEST_EID)
    check("rules: a thread delete gives a rule's restatement its own row, and it is still a rule",
          (await _kind(base))["status"] == "archived" and own and own["memory_type"] == "preference"
          and own["rk"] == "learned", f"{dict(own) if own else None}")
    th2 = await ct.create_thread(pool, TEST_EID, title="rules gate thread 2")
    stamp = ct.descriptor_for(str(th2["id"]))
    dropped = await _row(pool, "reply in short paragraphs", "preference",
                         meta={"rule_kind": "taught", "superseded_in": stamp})
    await pool.execute("UPDATE entity_memory SET t_valid_until = NOW() - interval '1 minute' WHERE id = $1",
                       dropped)
    plain_new = await _row(pool, "reply in short paragraphs", "preference")
    linked_by_plain = await MemoryConsolidator(pool).link_dropped(
        TEST_EID, "reply in short paragraphs", "preference", None, plain_new, corrects=False)
    rule_new = await _row(pool, "reply in short paragraphs", "preference", meta={"rule_kind": "taught"})
    linked_by_rule = await MemoryConsolidator(pool).link_dropped(
        TEST_EID, "reply in short paragraphs", "preference", None, rule_new, corrects=False, rule=True)
    check("rules: a dropped rule is linked by the rule that replaces it, never by a plain preference",
          linked_by_plain is None and linked_by_rule == dropped, f"{linked_by_plain} {linked_by_rule}")
    await _clean(pool)

    # 7f. twins: a rule and a plain preference in the same words are two
    # rows. A thread delete never holds one on the other — not when it brings
    # a dropped row back (_live_twin), not when it gives a restatement a row
    # of its own (_own_words).
    th3 = await ct.create_thread(pool, TEST_EID, title="rules twin thread")
    t3 = ct.descriptor_for(str(th3["id"]))
    d_rule = await _row(pool, "twin words dropped as a rule", "preference",
                        meta={"rule_kind": "taught", "superseded_in": t3})
    d_plain = await _row(pool, "twin words dropped as a plain one", "preference",
                         meta={"superseded_in": t3})
    await pool.execute("UPDATE entity_memory SET t_valid_until = NOW() - interval '1 minute' "
                       "WHERE id = ANY($1::int[])", [d_rule, d_plain])
    live_plain = await _row(pool, "twin words dropped as a rule", "preference")
    live_rule = await _row(pool, "twin words dropped as a plain one", "preference",
                           meta={"rule_kind": "taught"})
    # Restatements in other words, each with a live row of the other kind.
    said = "2026-09-30T00:00:00+00:00"
    b_rule = await _row(pool, "a rule said in the twin thread", "preference", scope=t3,
                        meta={"rule_kind": "learned",
                              "held_in": [{**A, "at": said, "words": "twin restated rule"}]})
    b_plain = await _row(pool, "a plain one said in the twin thread", "preference", scope=t3,
                         meta={"held_in": [{**A, "at": said, "words": "twin restated plain"}]})
    other_plain = await _row(pool, "twin restated rule", "preference")
    other_rule = await _row(pool, "twin restated plain", "preference", meta={"rule_kind": "taught"})
    await ct.delete_thread(pool, TEST_EID, str(th3["id"]))
    back_rule, back_plain = await _kind(d_rule), await _kind(d_plain)
    ptr = {m: await pool.fetchval("SELECT resolution_pointer FROM entity_memory WHERE id = $1", m)
           for m in (d_rule, d_plain)}
    check("rules: a thread delete brings a dropped rule back though a plain preference holds its words "
          "(and a dropped plain one though a rule does): a twin is of the same kind",
          back_rule["live"] and back_rule["rk"] == "taught" and back_plain["live"] and back_plain["rk"] is None
          and ptr[d_rule] != live_plain and ptr[d_plain] != live_rule,
          f"{back_rule} {back_plain} {ptr}")

    async def _restated(words):
        return [dict(r) for r in await pool.fetch(
            "SELECT id, metadata->>'rule_kind' AS rk, metadata->'held_in' AS held FROM entity_memory "
            "WHERE entity_id = $1 AND content = $2 AND status = 'active' ORDER BY id", TEST_EID, words)]
    r_rows, p_rows = await _restated("twin restated rule"), await _restated("twin restated plain")
    check("rules: a thread delete gives a rule's restatement a rule row of its own beside a plain "
          "preference in those words (and a plain one's a plain row beside a rule), never holding it there",
          (await _kind(b_rule))["status"] == "archived" and (await _kind(b_plain))["status"] == "archived"
          and [r["rk"] for r in r_rows] == [None, "learned"] and r_rows[0]["id"] == other_plain
          and r_rows[0]["held"] is None
          and [r["rk"] for r in p_rows] == ["taught", None] and p_rows[0]["id"] == other_rule
          and p_rows[0]["held"] is None, f"rule={r_rows} plain={p_rows}")
    await _clean(pool)

    # ... nor when a row the archived one replaced comes back and holds the
    # restatement's words: it keeps the record only if it is of the same kind.
    th4 = await ct.create_thread(pool, TEST_EID, title="rules holder thread")
    t4 = ct.descriptor_for(str(th4["id"]))
    h_rule = await _row(pool, "a rule said in the holder thread", "preference", scope=t4,
                        meta={"rule_kind": "learned",
                              "held_in": [{**A, "at": said, "words": "holder words for the rule"}]})
    h_plain = await _row(pool, "a plain one said in the holder thread", "preference", scope=t4,
                         meta={"held_in": [{**A, "at": said, "words": "holder words for the plain"}]})
    p_by_rule = await _row(pool, "holder words for the rule and more", "preference",
                           meta={"superseded_in": t4})
    r_by_plain = await _row(pool, "holder words for the plain and more", "preference",
                            meta={"rule_kind": "taught", "superseded_in": t4})
    for old, new in ((p_by_rule, h_rule), (r_by_plain, h_plain)):
        await pool.execute("UPDATE entity_memory SET t_valid_until = NOW() - interval '1 minute', "
                           "resolution_pointer = $2 WHERE id = $1", old, new)
    await ct.delete_thread(pool, TEST_EID, str(th4["id"]))
    hr, hp = await _restated("holder words for the rule"), await _restated("holder words for the plain")
    held_on = {m: await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1", m)
               for m in (p_by_rule, r_by_plain)}
    check("rules: a thread delete never records a rule's restatement on a plain row it replaced "
          "(nor a plain one's on a rule): each gets a row of its own kind",
          [r["rk"] for r in hr] == ["learned"] and [r["rk"] for r in hp] == [None]
          and held_on[p_by_rule] is None and held_on[r_by_plain] is None,
          f"rule={hr} plain={hp} held_on={held_on}")
    await _clean(pool)

    # 7g. "already held" records who said it again. The same words said in a
    # thread X and again in B — through close_turn and memory_curate add, a
    # rule and a plain preference alike — are held by X's row, with B
    # recorded on it (close_turn.held_already), so deleting X leaves B its words.
    from mastercode.memory_consolidation import scope_stamp as _stamp
    from mastercode.runtime import close_turn as _ct7
    from mastercode.runtime.tools import _curate_add
    said_again = []
    for writer in ("close_turn", "memory_curate add"):
        for kind in ("rule", "plain"):
            thx = await ct.create_thread(pool, TEST_EID, title=f"said in X {writer} {kind}")
            X = ct.descriptor_for(str(thx["id"]))
            w = f"{kind} words said in X then again in B via {writer}"
            if kind == "rule":
                await eng._store_teaching_memory(TEST_EID, {"content": w, "trigger": "remember this:"},
                                                 source_context=X)
            else:
                await _row(pool, w, "preference", scope=X)
            if writer == "close_turn":
                r7 = await _ct7.apply(pool, TEST_EID, {"preferences": [
                    {"text": w, "reason": "said so", "scope": {"kind": "global"}}]},
                    session_id=f"rules-said-{kind}", turn_scope=B)
                answer = ((r7.get("preferences") or [{}])[0]).get("status")
            else:
                answer = (await _curate_add(pool, MemoryConsolidator(pool), TEST_EID, w, "preference", B,
                                            _stamp(B), f"rules-said-{kind}")).get("action")
            await ct.delete_thread(pool, TEST_EID, str(thx["id"]))
            live7 = [dict(r) for r in await pool.fetch(
                "SELECT id, metadata->>'rule_kind' AS rk, metadata->'source_context' AS sc "
                "FROM entity_memory WHERE entity_id = $1 AND content = $2 AND status = 'active' "
                "AND t_valid_until IS NULL", TEST_EID, w)]
            said_again.append((writer, kind, answer, live7))
    check("rules: words said in a thread and again in B (close_turn, memory_curate add; a rule and a "
          "plain preference) are held with B recorded, so deleting the thread leaves B its words, "
          "of the same kind",
          all(ans in ("already_held", "rescoped") and len(lv) == 1
              and (lv[0]["rk"] is not None) == (kind == "rule")
              and json.loads(lv[0]["sc"] or "null") == dict(B)
              for _w, kind, ans, lv in said_again), f"{said_again}")
    # The one case held_already records as PROVENANCE ONLY: close_turn in B
    # names a place (thread Y) that an undecided row said there reaches. B is
    # recorded with the Host's `about`; scope reading skips it, so B gains no
    # reach while Y exists. Deleting Y re-homes the row to B, exactly as the
    # stored-row path (no row yet) leaves its own row read in B.
    from mastercode.memory_retrieval import _canonical_where
    from mastercode.runtime.close_turn import _covers as _cov7, _held_row as _hr7

    async def _read_in(words, scope):
        where, params = _canonical_where(TEST_EID, memory_types=["preference"], turn_scope=scope)
        return [int(r["id"]) for r in await pool.fetch(
            f"SELECT id FROM entity_memory WHERE {where} AND content = ${len(params) + 1}",
            *params, words)]

    async def _say_about_thread(words, th_id, turn_scope):
        r = await _ct7.apply(pool, TEST_EID, {"preferences": [
            {"text": words, "reason": "said so", "scope": {"kind": "thread", "ref": str(th_id)}}]},
            session_id=f"rules-said-{th_id}", turn_scope=turn_scope)
        return ((r.get("preferences") or [{}])[0]).get("status")

    thy = await ct.create_thread(pool, TEST_EID, title="said in Y only")
    Y = ct.descriptor_for(str(thy["id"]))
    wy = "a preference about thread Y only"
    in_y = await _row(pool, wy, "preference", scope=Y)
    st_y = await _say_about_thread(wy, thy["id"], B)
    held_y = json.loads(await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1",
                                            in_y) or "null")
    before_b, before_y = await _read_in(wy, B), await _read_in(wy, Y)
    twin_y = await _row(pool, "a twin said in Y only", "preference", scope=Y)
    twin_y2 = await _row(pool, "a twin said in Y only", "preference", scope=Y,
                         meta={"held_in": [dict(held_y[0])] if held_y else []})
    cov_y = await _cov7(pool, TEST_EID, twin_y, twin_y2)
    _rid, _ab, reach_y = _hr7({"id": in_y, "about": None, "sc": json.dumps(Y), "has_about": False,
                               "has_sc": True, "held_in": json.dumps(held_y)})
    await ct.delete_thread(pool, TEST_EID, str(thy["id"]))
    after_b = await _read_in(wy, B)
    # The stored-row path: no prior row; the Host names thread Y2 in B.
    thy2 = await ct.create_thread(pool, TEST_EID, title="said about Y2")
    wy2 = "a preference about thread Y2 said in B"
    st_y2 = await _say_about_thread(wy2, thy2["id"], B)
    before_b2 = await _read_in(wy2, B)
    await ct.delete_thread(pool, TEST_EID, str(thy2["id"]))
    after_b2 = await _read_in(wy2, B)
    check("rules: held for a thread Y the Host named, a row said only in Y records B as provenance only "
          "(no reach in B while Y exists) and, once Y is deleted, is read in B — as the stored-row path's "
          "own row is",
          st_y == "already_held" and len(held_y or []) == 1
          and held_y[0].get("about") == {"kind": Y["kind"], "ref": str(Y["ref"])}
          and before_b == [] and before_y == [in_y] and after_b == [in_y]
          and st_y2 == "stored" and before_b2 == [] and len(after_b2) == 1,
          f"{st_y} {held_y} before B={before_b} Y={before_y} after B={after_b} | "
          f"stored path {st_y2} before={before_b2} after={after_b2}")
    check("rules: a provenance-only record changes no reading: the merge key (place_key_sql) and "
          "close_turn's reach (_held_row) skip it",
          cov_y is True and reach_y == [{"kind": Y["kind"], "ref": str(Y["ref"])}],
          f"covers={cov_y} reach={reach_y}")
    # Kept by another record (Q, said first), the row goes to Q when Y is
    # deleted; B's mark goes with Y, so it is read in B too.
    thy3 = await ct.create_thread(pool, TEST_EID, title="said in Y3 and Q")
    thq = await ct.create_thread(pool, TEST_EID, title="Q")
    Y3, Q = ct.descriptor_for(str(thy3["id"])), ct.descriptor_for(str(thq["id"]))
    wy3 = "a preference said in Y3 and again in Q"
    in_y3 = await _row(pool, wy3, "preference", scope=Y3,
                       meta={"held_in": [{**Q, "at": "2026-09-01T00:00:00+00:00"}]})
    st_y3 = await _say_about_thread(wy3, thy3["id"], B)
    before_b3 = await _read_in(wy3, B)
    await ct.delete_thread(pool, TEST_EID, str(thy3["id"]))
    sc3 = json.loads(await pool.fetchval("SELECT metadata->'source_context' FROM entity_memory WHERE id = $1",
                                         in_y3) or "null")
    after_b3, after_q3 = await _read_in(wy3, B), await _read_in(wy3, Q)
    check("rules: kept by another conversation's record when Y is deleted, the row loses B's mark with Y "
          "and is read in B as well",
          st_y3 == "already_held" and before_b3 == [] and sc3 == {"kind": Q["kind"], "ref": str(Q["ref"])}
          and after_b3 == [in_y3] and after_q3 == [in_y3],
          f"{st_y3} before B={before_b3} source={sc3} after B={after_b3} Q={after_q3}")
    # A provenance-only record never absorbs a record that reads: B saying
    # the words for itself afterwards (memory_curate add) makes the row read
    # in B, the mark untouched beside it.
    thy6 = await ct.create_thread(pool, TEST_EID, title="said in Y6 only")
    wy6 = "a preference said in Y6, then kept in B"
    in_y6 = await _row(pool, wy6, "preference", scope=ct.descriptor_for(str(thy6["id"])))
    await _say_about_thread(wy6, thy6["id"], B)
    b6_before = await _read_in(wy6, B)
    add6 = await _curate_add(pool, MemoryConsolidator(pool), TEST_EID, wy6, "preference", B,
                             _stamp(B), "rules-said-y6")
    b6_after = await _read_in(wy6, B)
    check("rules: B keeping the words itself after a provenance-only record makes the row read in B",
          b6_before == [] and add6.get("action") == "already_held" and add6.get("memory_id") == in_y6
          and b6_after == [in_y6], f"{b6_before} {add6} {b6_after}")
    # A mark naming a place that still exists is no home at a delete: the row
    # said in X7, read in Q7 too, goes to Q7 when X7 is deleted, and B (which
    # said it about Q7) still does not read it.
    thx7 = await ct.create_thread(pool, TEST_EID, title="said in X7")
    thq7 = await ct.create_thread(pool, TEST_EID, title="Q7")
    Q7 = ct.descriptor_for(str(thq7["id"]))
    wy7 = "a preference said in X7 and in Q7"
    in_y7 = await _row(pool, wy7, "preference", scope=ct.descriptor_for(str(thx7["id"])),
                       meta={"held_in": [{**Q7, "at": "2099-01-01T00:00:00+00:00"}]})
    await _say_about_thread(wy7, thq7["id"], B)
    await ct.delete_thread(pool, TEST_EID, str(thx7["id"]))
    sc7 = json.loads(await pool.fetchval("SELECT metadata->'source_context' FROM entity_memory WHERE id = $1",
                                         in_y7) or "null")
    check("rules: at a delete, a provenance-only record about a place that still exists is no home: the "
          "row goes to the conversation that reads it, and B still does not read it",
          sc7 == {"kind": Q7["kind"], "ref": str(Q7["ref"])} and await _read_in(wy7, B) == []
          and await _read_in(wy7, Q7) == [in_y7], f"source={sc7} B={await _read_in(wy7, B)}")
    # No conversation to name (UNSCOPED): the mark keeps the row from being
    # read everywhere while Y4 exists.
    thy4 = await ct.create_thread(pool, TEST_EID, title="said in Y4 only")
    wy4 = "a preference said in Y4, again with no scope"
    in_y4 = await _row(pool, wy4, "preference", scope=ct.descriptor_for(str(thy4["id"])))
    st_y4 = await _say_about_thread(wy4, thy4["id"], None)
    twin_y4 = await _row(pool, "a twin said in Y4", "preference", scope=ct.descriptor_for(str(thy4["id"])))
    twin_y4b = await _row(pool, "a twin said in Y4", "preference", scope=ct.descriptor_for(str(thy4["id"])),
                          meta={"held_in": json.loads(await pool.fetchval(
                              "SELECT metadata->'held_in' FROM entity_memory WHERE id = $1", in_y4) or "[]")})
    check("rules: a provenance-only record for no conversation does not make the row read everywhere",
          st_y4 == "already_held" and await _read_in(wy4, {}) == []
          and await _cov7(pool, TEST_EID, twin_y4, twin_y4b) is True,
          f"{st_y4} {await _read_in(wy4, {})}")
    # A late write (Y5's delete under way: deleted, not yet forgotten): the
    # conversation takes the row over with the Host's decision on it, as its
    # own copy would carry.
    thx5 = await ct.create_thread(pool, TEST_EID, title="said in X5, going")
    thq5 = await ct.create_thread(pool, TEST_EID, title="Q5")
    Q5 = ct.descriptor_for(str(thq5["id"]))
    wy5 = "a preference said in X5 and again in Q5"
    in_y5 = await _row(pool, wy5, "preference", scope=ct.descriptor_for(str(thx5["id"])),
                       meta={"held_in": [{**Q5, "at": "2026-09-01T00:00:00+00:00"}]})
    await pool.execute("UPDATE circles_threads SET deleted_at = NOW() WHERE id = $1::uuid", str(thx5["id"]))
    st_y5 = await _say_about_thread(wy5, thq5["id"], B)
    md5 = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", in_y5))
    check("rules: taking over a row whose thread is being deleted, the conversation takes the Host's "
          "decision with it (read only where the Host put it)",
          st_y5 == "already_held" and md5.get("source_context") == dict(B)
          and md5.get("about") == {"kind": Q5["kind"], "ref": str(Q5["ref"])},
          f"{st_y5} {md5}")
    await _clean(pool)

    # 7h. Where a narrowed rule still rides: where the person TAUGHT it
    # (first time and again), never where the Host or a writer merely said it
    # again (review of 551d5a5f, P3-P6). memory_scope names those places.
    from mastercode.runtime import tools as _tools7h
    from mastercode.runtime import close_turn as _ct7h
    from mastercode.memory_consolidation import scope_stamp as _stamp7h

    async def _thread(title):
        t = await ct.create_thread(pool, TEST_EID, title=title)
        return str(t["id"]), ct.descriptor_for(str(t["id"]))

    async def _rides(words, sc):
        return words in _section(await eng.build_memory_context(
            TEST_EID, {}, max_tokens=4000, query="what is on the agenda", source_context=sc) or "",
            "Things You Were Taught")

    b_id, B7 = await _thread("taught first here")
    d_id, D7 = await _thread("narrowed to here")
    e_id, E7 = await _thread("the Host echoed it here (close_turn)")
    f_id, F7 = await _thread("the Host echoed it here (memory_curate)")
    g_id, G7 = await _thread("taught again here")
    o_id, O7 = await _thread("an unrelated thread")
    w7 = "contracts are signed by both founders"
    r7 = int((await eng._store_teaching_memory(TEST_EID, {"content": w7, "trigger": "remember this:"},
                                               source_context=B7) or {}).get("id") or 0)
    await _ct7h.apply(pool, TEST_EID, {"preferences": [
        {"text": w7, "reason": "said so", "scope": {"kind": "global"}}]}, session_id="rules-7h-e", turn_scope=E7)
    await _tools7h._curate_add(pool, MemoryConsolidator(pool), TEST_EID, w7, "preference", F7,
                               _stamp7h(F7), "rules-7h-f")
    await eng._store_teaching_memory(TEST_EID, {"content": w7, "trigger": "remember this:"}, source_context=G7)
    held7 = json.loads(await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1",
                                           r7) or "[]")
    taught7 = sorted(e["ref"] for e in held7 if e.get("taught"))
    said7 = sorted(e["ref"] for e in held7 if not e.get("taught"))
    check("rules: the first and the repeat teaching place are recorded taught; the Host's echoes are not",
          taught7 == sorted([b_id, g_id]) and said7 == sorted([e_id, f_id]), f"{held7}")
    scoped7 = await _tools7h.apply_memory_scope(pool, TEST_EID, r7, "thread", d_id)
    rides7 = {n: await _rides(w7, sc) for n, sc in
              (("D", D7), ("B", B7), ("G", G7), ("E", E7), ("F", F7), ("O", O7))}
    check("rules: a rule the Host narrows still rides where the person taught it, first time and again, "
          "and never where the Host merely said it again",
          rides7 == {"D": True, "B": True, "G": True, "E": False, "F": False, "O": False}, f"{rides7}")
    res7 = scoped7.get("result") or {}
    kept7 = " ".join(res7.get("still_read_where_taught") or [])
    listed7 = "\n".join(sum((await _tools7h._memory_scope_list(pool, TEST_EID))["result"]["by_scope"]
                            .values(), []))
    check("rules: memory_scope's result and its list name the places a narrowing keeps",
          scoped7.get("status") == "ok" and b_id in kept7 and g_id in kept7 and e_id not in kept7
          and "still read where the person taught it" in (res7.get("note") or "")
          and f"#{r7} [rule; also where taught: " in listed7 and b_id in listed7.split(f"#{r7} ")[1].split("]")[0],
          f"{res7} {listed7!r}")
    # The Host saying it again AFTER the narrowing adds no place to the rule
    # either: close_turn for that place keeps a row of its own there (the
    # Host's decision for that place); memory_curate add likewise.
    await _ct7h.apply(pool, TEST_EID, {"preferences": [
        {"text": w7, "reason": "said so", "scope": {"kind": "thread", "ref": o_id}}]},
        session_id="rules-7h-o", turn_scope=O7)
    await _tools7h._curate_add(pool, MemoryConsolidator(pool), TEST_EID, w7, "preference", O7,
                               _stamp7h(O7), "rules-7h-o2")
    held7o = json.loads(await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1",
                                            r7) or "[]")
    check("rules: after a narrowing, the Host echoing a rule elsewhere never widens it",
          not await _rides(w7, O7) and await _rides(w7, D7)
          and not any(e.get("ref") == o_id and e.get("taught") for e in held7o), f"{held7o}")
    # A delete of the first teaching thread re-homes the rule to a
    # conversation that taught it again: still taught there, still riding.
    await ct.delete_thread(pool, TEST_EID, b_id)
    sc7 = json.loads(await pool.fetchval("SELECT metadata->'source_context' FROM entity_memory WHERE id = $1",
                                         r7) or "null")
    held7b = await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1", r7)
    rides7b = {n: await _rides(w7, sc) for n, sc in (("G", G7), ("D", D7), ("E", E7))}
    check("rules: deleting the first teaching thread keeps the rule where it was taught again",
          (await _kind(r7))["status"] == "active" and rides7b == {"G": True, "D": True, "E": False},
          f"source={sc7} g={g_id} e={e_id} f={f_id} o={o_id} held={held7b} rides={rides7b} {await _kind(r7)}")
    await _clean(pool)

    # An echo's record becomes taught when the person teaches the words there;
    # a delete that hands the rule to its only other teaching place keeps it
    # taught there; close_turn counts only taught places as where a decided
    # rule is read.
    b9_id, B9 = await _thread("taught first, then deleted")
    h9_id, H9 = await _thread("echoed, then taught")
    g9_id, G9 = await _thread("taught again")
    d9_id, D9h = await _thread("narrowed here (2)")
    w9 = "quotes include the delivery date"
    r9h = int((await eng._store_teaching_memory(TEST_EID, {"content": w9, "trigger": "remember this:"},
                                                source_context=B9) or {}).get("id") or 0)
    await _ct7h.apply(pool, TEST_EID, {"preferences": [
        {"text": w9, "reason": "said so", "scope": {"kind": "global"}}]}, session_id="rules-7h-h9", turn_scope=H9)
    await eng._store_teaching_memory(TEST_EID, {"content": w9, "trigger": "remember this:"}, source_context=H9)
    held9h = json.loads(await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1",
                                            r9h) or "[]")
    check("rules: the person teaching words the Host had echoed there makes that record taught (one record)",
          [e.get("taught") for e in held9h if e.get("ref") == h9_id] == [True], f"{held9h}")
    # The Host says it again in G (an echo record), then narrows the rule.
    await _tools7h._curate_add(pool, MemoryConsolidator(pool), TEST_EID, w9, "preference", G9,
                               _stamp7h(G9), "rules-7h-g9a")
    await _tools7h.apply_memory_scope(pool, TEST_EID, r9h, "thread", d9_id)
    c9 = await _ct7h.apply(pool, TEST_EID, {"preferences": [
        {"text": w9, "reason": "said so", "scope": {"kind": "thread", "ref": g9_id}}]},
        session_id="rules-7h-g9", turn_scope=G9)
    held9g = json.loads(await pool.fetchval("SELECT metadata->'held_in' FROM entity_memory WHERE id = $1",
                                            r9h) or "[]")
    rows9 = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND content = $2 "
                                "AND status = 'active' AND t_valid_until IS NULL", TEST_EID, w9)
    check("rules: close_turn reads a decided rule only where it is decided or taught: in G, where the "
          "Host only echoed it, the words are held by the rule and nothing new is stored",
          any(e.get("ref") == g9_id and not e.get("taught") for e in held9g)
          and ((c9.get("preferences") or [{}])[0]).get("status") == "held_by_rule" and rows9 == 1,
          f"{c9} {held9g} rows={rows9}")
    # close_turn naming a narrowed rule's words as global changes nothing: a
    # rule's scope moves only through memory_scope, and the result says so.
    about9 = await pool.fetchval("SELECT metadata->'about' FROM entity_memory WHERE id = $1", r9h)
    cg9 = await _ct7h.apply(pool, TEST_EID, {"preferences": [
        {"text": w9, "reason": "said so", "scope": {"kind": "global"}}]},
        session_id="rules-7h-glob", turn_scope=G9)
    pg9 = (cg9.get("preferences") or [{}])[0]
    rows9g = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND content = $2 "
                                 "AND status = 'active' AND t_valid_until IS NULL", TEST_EID, w9)
    check("rules: close_turn with scope global on a narrowed rule's words leaves its scope as it is, stores "
          "nothing new, and names memory_scope",
          pg9.get("status") == "held_by_rule" and pg9.get("id") == r9h
          and "memory_scope" in (pg9.get("note") or "") and f"#{r9h}" in (pg9.get("note") or "")
          and await pool.fetchval("SELECT metadata->'about' FROM entity_memory WHERE id = $1", r9h) == about9
          and rows9g == 1 and not await _rides(w9, G9),
          f"{cg9} rows={rows9g}")
    await _clean(pool)
    b2_id, B2 = await _thread("taught first (2)")
    g2_id, G2 = await _thread("taught again (2)")
    d2_id, D2 = await _thread("narrowed here (3)")
    w2 = "board minutes are kept for ten years"
    r2 = int((await eng._store_teaching_memory(TEST_EID, {"content": w2, "trigger": "remember this:"},
                                               source_context=B2) or {}).get("id") or 0)
    await eng._store_teaching_memory(TEST_EID, {"content": w2, "trigger": "remember this:"}, source_context=G2)
    await _tools7h.apply_memory_scope(pool, TEST_EID, r2, "thread", d2_id)
    await ct.delete_thread(pool, TEST_EID, b2_id)
    check("rules: a delete that hands a narrowed rule to its other teaching place keeps it taught there",
          (await _kind(r2))["status"] == "active" and await _rides(w2, G2) and await _rides(w2, D2),
          f"{await pool.fetchval('SELECT metadata::text FROM entity_memory WHERE id = $1', r2)}")
    await _clean(pool)

    # Re-homed onto an echo, a narrowed rule does not ride there, until the
    # person teaches it there (a teaching in the row's own conversation marks it).
    t_id, T8 = await _thread("taught here, then deleted")
    e8_id, E8 = await _thread("echoed here")
    d8_id, D8 = await _thread("narrowed here")
    w8 = "the office closes early on fridays"
    r8 = int((await eng._store_teaching_memory(TEST_EID, {"content": w8, "trigger": "remember this:"},
                                               source_context=T8) or {}).get("id") or 0)
    await _ct7h.apply(pool, TEST_EID, {"preferences": [
        {"text": w8, "reason": "said so", "scope": {"kind": "global"}}]}, session_id="rules-7h-e8", turn_scope=E8)
    await ct.delete_thread(pool, TEST_EID, t_id)
    await _tools7h.apply_memory_scope(pool, TEST_EID, r8, "thread", d8_id)
    on_echo = await _rides(w8, E8)
    await eng._store_teaching_memory(TEST_EID, {"content": w8, "trigger": "remember this:"}, source_context=E8)
    check("rules: a narrowed rule re-homed onto an echo does not ride there until the person teaches it "
          "there",
          not on_echo and await _rides(w8, E8) and await _rides(w8, D8), f"{on_echo}")
    await _clean(pool)


async def _rule_reach_paths(pool, eng) -> None:
    """8. ONE definition of where a rule rides (memory_engine.rule_reach): its
    decided place (or everywhere) and every place the person taught it. The
    review of 2d9491fb found memory_curate replace (P7) and its duplicate
    shortcut (P7b) dropping taught places, and a deleted narrowing making a
    rule ride everywhere (P8). Then a property check: random rules, random
    operations, the taught places always within the reach — unless the
    person's own thread delete removed them — and the Python definition and
    the SQL read agreeing on every place."""
    import random
    from mastercode import circles_threads as ct
    from mastercode.memory_consolidation import MemoryConsolidator
    from mastercode.memory_engine import rule_place_clause, rule_reach, rule_row_sql
    from mastercode.runtime import close_turn as _ct8
    from mastercode.runtime import tools as _t8

    async def _thread(title):
        t = await ct.create_thread(pool, TEST_EID, title=title)
        return ct.descriptor_for(str(t["id"]))

    async def _reads(rid, place):
        sql, args = rule_place_clause(place, 3)
        return bool(await pool.fetchval(
            f"SELECT 1 FROM entity_memory WHERE id = $1 AND entity_id = $2 AND {rule_row_sql()} "
            "AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW()) "
            f"AND {sql}", rid, TEST_EID, *args))

    async def _teach(words, place):
        return await eng._store_teaching_memory(TEST_EID, {"content": words, "trigger": "remember this:"},
                                                source_context=place)

    async def _rule_id(words):
        return await pool.fetchval(
            "SELECT id FROM entity_memory WHERE entity_id = $1 AND content = $2 AND status = 'active' "
            "AND t_valid_until IS NULL AND metadata ? 'rule_kind' ORDER BY id DESC LIMIT 1", TEST_EID, words)

    async def _narrow(rid, place):
        return await _t8.apply_memory_scope(pool, TEST_EID, rid, place["kind"], place["ref"])

    cons = MemoryConsolidator(pool)
    # P7: replace keeps every place the person taught the rule.
    A, B, C = await _thread("A (narrowed)"), await _thread("B (taught)"), await _thread("C (taught)")
    w7 = "replies stay under five lines"
    await _teach(w7, C)
    await _teach(w7, B)
    await _narrow(await _rule_id(w7), A)
    old7 = await _rule_id(w7)
    r7 = await _t8._curate_replace(pool, cons, TEST_EID, old7, "replies stay under six lines", "",
                                   None, None, "rules-p7")
    new7 = r7.get("memory_id")
    reads7 = {n: await _reads(new7, p) for n, p in (("A", A), ("B", B), ("C", C))}
    check("rules (P7): memory_curate replace of a narrowed rule keeps every place the person taught it, "
          "and its result names them",
          reads7 == {"A": True, "B": True, "C": True} and new7 != old7
          and sorted(r7.get("still_read_where_taught") or []) == sorted(
              [f"thread:{B['ref']} (B (taught))", f"thread:{C['ref']} (C (taught))"]), f"{r7} {reads7}")
    await _clean(pool)

    # P7b: replacing into words another rule holds: that rule takes the
    # corrected rule's taught places, so nothing is lost.
    A, B, C = await _thread("A (narrowed)"), await _thread("B (taught)"), await _thread("C (taught)")
    x, y = "notes are kept in markdown", "notes are kept in plain markdown"
    await _teach(x, C)
    await _teach(x, B)
    await _narrow(await _rule_id(x), A)
    await _teach(y, A)
    await _narrow(await _rule_id(y), A)
    xid, yid = await _rule_id(x), await _rule_id(y)
    r7b = await _t8._curate_replace(pool, cons, TEST_EID, xid, y, "", None, None, "rules-p7b")
    reads7b = {n: await _reads(yid, p) for n, p in (("A", A), ("B", B), ("C", C))}
    check("rules (P7b): replacing a narrowed rule into another rule's words gives that rule its taught "
          "places, and the result names them",
          r7b.get("memory_id") == yid and r7b.get("replaced") == xid
          and reads7b == {"A": True, "B": True, "C": True}
          and len(r7b.get("still_read_where_taught") or []) == 2, f"{r7b} {reads7b}")
    await _clean(pool)

    # P8: deleting the thread a rule was narrowed to falls back to where the
    # person taught it; a rule taught nowhere still live rides everywhere.
    C8, T8, U8 = await _thread("C (taught)"), await _thread("T (narrowed, then deleted)"), await _thread("U")
    w8 = "drafts are reviewed on fridays"
    await _teach(w8, C8)
    r8 = await _rule_id(w8)
    await _narrow(r8, T8)
    bare = await _row(pool, "a narrowed rule taught nowhere", "preference", importance=2.0,
                      meta={"rule_kind": "taught", "about": dict(T8), "scoped_by": "host"})
    await ct.delete_thread(pool, TEST_EID, T8["ref"])
    listed8 = (await _t8._memory_scope_list(pool, TEST_EID))["result"]["by_scope"]
    check("rules (P8): memory_scope names a deleted thread a decision still points at as deleted, not "
          "by its old title",
          f"thread:{T8['ref']} (deleted)" in listed8
          and not any("narrowed, then deleted" in k for k in listed8), f"{list(listed8)}")
    check("rules (P8): deleting the thread a rule was narrowed to leaves it riding where it was taught, "
          "not everywhere; one taught nowhere rides everywhere",
          await _reads(r8, C8) and not await _reads(r8, U8)
          and await _reads(bare, U8) and await _reads(bare, C8), "")
    await _clean(pool)

    # No decision is carried onto a rule from another: a fresh teaching that
    # replaces a dropped rule stays undecided, and a rule taking a corrected
    # one in keeps its own reach.
    A9, B9, D9 = await _thread("A9"), await _thread("B9"), await _thread("D9 (drops it)")
    wd = "invoices carry the order number"
    await _teach(wd, A9)
    dropped = await _rule_id(wd)
    await _narrow(dropped, A9)
    await pool.execute(
        "UPDATE entity_memory SET t_valid_until = NOW() - interval '1 minute', "
        "metadata = metadata || jsonb_build_object('superseded_in', $2::jsonb) WHERE id = $1",
        dropped, json.dumps(D9))
    await _teach(wd, B9)
    fresh = await _rule_id(wd)
    fresh_about = await pool.fetchval("SELECT metadata->'about' FROM entity_memory WHERE id = $1", fresh)
    linked = await pool.fetchval("SELECT resolution_pointer FROM entity_memory WHERE id = $1", dropped)
    check("rules: a fresh teaching that replaces a dropped narrowed rule takes no decision from it",
          fresh != dropped and linked == fresh and fresh_about is None and await _reads(fresh, D9), "")
    await _clean(pool)
    A9 = await _thread("A9 again")
    xw, yw = "statements are emailed", "statements are emailed as pdf"
    await _teach(xw, A9)
    await _narrow(await _rule_id(xw), A9)
    await _teach(yw, A9)
    yid9, xid9 = await _rule_id(yw), await _rule_id(xw)
    r9a = await _t8._curate_replace(pool, cons, TEST_EID, xid9, yw, "", None, None, "rules-absorb")
    y_about = await pool.fetchval("SELECT metadata->'about' FROM entity_memory WHERE id = $1", yid9)
    U9 = await _thread("U9")
    check("rules: a rule nobody narrowed that takes a corrected narrowed rule in keeps riding everywhere",
          r9a.get("memory_id") == yid9 and y_about is None and await _reads(yid9, U9), f"{r9a} {y_about}")
    await _clean(pool)

    # Retire-into-rule reads the rule's whole reach (place_key's places for a
    # rule): a plain copy about a place the person taught a narrowed rule in
    # is retired by the teaching there.
    A10, T10 = await _thread("A10 (narrowed)"), await _thread("T10 (taught again, copy here)")
    w10 = "meetings end with action items"
    await _teach(w10, A10)
    r10 = await _rule_id(w10)
    await _narrow(r10, A10)
    copy10 = await _row(pool, w10, "preference", meta={"about": dict(T10), "scoped_by": "host"})
    await _teach(w10, T10)
    ptr10 = await pool.fetchval("SELECT resolution_pointer FROM entity_memory WHERE id = $1", copy10)
    check("rules: a teaching in T retires a plain copy about T into a narrowed rule (T is now in its reach)",
          ptr10 == r10 and await _reads(r10, T10), f"{ptr10} {r10}")
    await _clean(pool)

    # A delete that re-homes a rule narrowed to the deleted thread keeps the
    # decision while it was taught somewhere still live (circles_threads.
    # _keep_held), and a late takeover does the same (note_held).
    X11, G11, U11 = await _thread("X11 (taught, narrowed, deleted)"), await _thread("G11 (taught)"), \
        await _thread("U11")
    w11 = "the backlog is groomed on mondays"
    await _teach(w11, X11)
    r11 = await _rule_id(w11)
    await _teach(w11, G11)
    await _narrow(r11, X11)
    await ct.delete_thread(pool, TEST_EID, X11["ref"])
    check("rules: deleting the thread a rule was both said in and narrowed to keeps it riding where it was "
          "taught again, not everywhere",
          await pool.fetchval("SELECT status FROM entity_memory WHERE id = $1", r11) == "active"
          and await _reads(r11, G11) and not await _reads(r11, U11),
          f"{await pool.fetchval('SELECT metadata::text FROM entity_memory WHERE id = $1', r11)}")
    await _clean(pool)
    X12, G12, B12, U12 = (await _thread("X12"), await _thread("G12"), await _thread("B12"),
                          await _thread("U12"))
    w12 = "invoices are approved by finance"
    await _teach(w12, X12)
    r12 = await _rule_id(w12)
    await _teach(w12, G12)
    await _narrow(r12, X12)
    await pool.execute("UPDATE circles_threads SET deleted_at = NOW() WHERE id = $1::uuid", X12["ref"])
    await _teach(w12, B12)        # a late teaching takes the row over (note_held)
    check("rules: a late teaching that takes over a rule narrowed to a thread being deleted keeps it riding "
          "where it was taught, not everywhere",
          await _reads(r12, G12) and await _reads(r12, B12) and not await _reads(r12, U12),
          f"{await pool.fetchval('SELECT metadata::text FROM entity_memory WHERE id = $1', r12)}")
    await _clean(pool)

    # The property: random decisions, teachings and operations.
    # A fixed seed for the gate; MEMORY_INTEGRITY_SEED explores others.
    rng = random.Random(int(os.environ.get("MEMORY_INTEGRITY_SEED") or 20261001))
    failures: list = []
    scenarios = 0
    # What the run reached: a narrow that silently errored would leave every
    # check below true of an undecided rule, so the run must show it got there.
    reached = {"narrows": 0, "narrowed_steps": 0, "narrowed_with_taught": 0, "replaces": 0}
    for scen in range(10):
        threads = [await _thread(f"place {scen}.{i}") for i in range(5)]
        app_place = {"kind": "app", "ref": "circles"}
        unrelated = await _thread(f"never touched {scen}")
        alive = {t["ref"] for t in threads}
        words = f"rule {scen} words version 0"
        version = 0
        first = rng.choice(threads)
        await _teach(words, first)
        rid = await _rule_id(words)
        taught = {first["ref"]}       # the model: where the person taught it
        decided = None                # the model: a narrowing (a thread), or None
        scenarios += 1
        for step in range(8):
            op = rng.choice(["teach", "narrow", "replace", "delete", "echo_ct", "echo_ct_global",
                             "echo_curate"])
            live = [t for t in threads if t["ref"] in alive]
            if not live:
                break
            place = rng.choice(live)
            if op == "teach":
                await _teach(words, place)
                taught.add(place["ref"])
            elif op == "narrow":
                if (await _narrow(rid, place)).get("status") == "ok":
                    decided = place["ref"]
                    reached["narrows"] += 1
            elif op == "replace":
                version += 1
                new_words = f"rule {scen} words version {version}"
                res = await _t8._curate_replace(pool, cons, TEST_EID, rid, new_words, "", None, None,
                                                f"rules-prop-{scen}")
                if res.get("memory_id"):
                    rid, words = int(res["memory_id"]), new_words
                    reached["replaces"] += 1
                    if decided is None:
                        decided = "global"   # a correction of an undecided rule is decided global
            elif op == "delete":
                src = await pool.fetchval("SELECT metadata->'source_context'->>'ref' FROM entity_memory "
                                          "WHERE id = $1", rid)
                await ct.delete_thread(pool, TEST_EID, place["ref"])
                alive.discard(place["ref"])
                taught.discard(place["ref"])          # the person's own delete removes it
                if decided == place["ref"] and not (taught & alive):
                    decided = None
                status = await pool.fetchval("SELECT status FROM entity_memory WHERE id = $1", rid)
                if status != "active":
                    if src != place["ref"]:
                        failures.append((scen, step, "rule archived by a delete of another thread"))
                    break
            elif op == "echo_ct":
                await _ct8.apply(pool, TEST_EID, {"preferences": [
                    {"text": words, "reason": "said so", "scope": {"kind": "thread", "ref": place["ref"]}}]},
                    session_id=f"rules-prop-{scen}-{step}", turn_scope=place)
            elif op == "echo_ct_global":
                await _ct8.apply(pool, TEST_EID, {"preferences": [
                    {"text": words, "reason": "said so", "scope": {"kind": "global"}}]},
                    session_id=f"rules-prop-{scen}-{step}g", turn_scope=place)
            elif op == "echo_curate":
                await _t8._curate_add(pool, cons, TEST_EID, words, "preference", place,
                                      dict(place), f"rules-prop-{scen}-{step}c")
            md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", rid)
                            or "{}")
            everywhere, places = rule_reach(md)
            if decided not in (None, "global"):
                reached["narrowed_steps"] += 1
                if (taught & alive) - {decided}:
                    reached["narrowed_with_taught"] += 1
            for ref in taught & alive:
                if not await _reads(rid, {"kind": "thread", "ref": ref}):
                    failures.append((scen, step, op, "taught place not read", ref))
            want_everywhere = decided in (None, "global")
            if everywhere != want_everywhere:
                failures.append((scen, step, op, "everywhere", everywhere, decided))
            for q in threads + [app_place, unrelated]:
                sql_reads = await _reads(rid, q)
                py_reads = everywhere or {"kind": q["kind"], "ref": str(q["ref"])} in places
                if sql_reads != py_reads:
                    failures.append((scen, step, op, "python and SQL disagree", q["ref"]))
                # A deleted thread is no turn's place; only live places count.
                if (not everywhere and q["ref"] not in taught and q["ref"] != decided and sql_reads
                        and (q["kind"] != "thread" or q["ref"] in alive or q is unrelated)):
                    failures.append((scen, step, op, "read where neither decided nor taught", q["ref"],
                                     q["ref"] in alive, md.get("held_in"), md.get("about")))
        await _clean(pool)
    check(f"rules (property): over {scenarios} random rules, the taught places are always within the reach "
          "unless the person deleted them, nothing else widens a narrowed rule, and the Python and SQL "
          "forms of the reach agree",
          not failures, f"{failures[:6]}")
    print(f"  [memory-integrity-db] property run reached {reached}", flush=True)
    check("rules (property): the run reached the states it checks — narrowed rules, narrowed rules also "
          "taught elsewhere, and replaced rules — so a narrow that errors cannot pass it",
          reached["narrows"] >= 3 and reached["narrowed_steps"] >= 15
          and reached["narrowed_with_taught"] >= 10 and reached["replaces"] >= 1, f"{reached}")


async def main() -> int:
    import asyncpg
    import os
    # PGHOST lets the same gate run against a throwaway cluster outside the pod.
    pool = await asyncpg.create_pool(
        host=os.environ.get("PGHOST", "/var/run/postgresql"), user="entity",
        database="hostmem", min_size=1, max_size=2)
    from mastercode import thread_audit as ta
    from mastercode.memory_engine import MemoryEngine
    try:
        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) "
            "VALUES ($1, 'MemIntegrityGate', 'standard') ON CONFLICT (id) DO NOTHING", TEST_EID)
        await _clean(pool)

        # ── 1. the thread's set
        a_fact = await _row(pool, "decided the invoice app ships monthly", "cited_fact", scope=A)
        b_fact = await _row(pool, "decided the garden plan waits for spring", "cited_fact", scope=B)
        pref = await _row(pool, "prefers dashboards over walls of text", "preference")
        await _row(pool, "prefers the garden plan in metric units", "preference", scope=B)
        rule = await _row(pool, "verify a build's terminal status before reporting it",
                          "preference", meta={"trigger": "remember this:", "rule_kind": "taught"})
        await _row(pool, "genesis: the host was born on the invoice project", "cited_fact")
        await _row(pool, "an unscoped old reply from somewhere", "turn", importance=0.6)
        await _row(pool, "they settle invoices before weekends", "summary",
                   meta={"source": "consolidation_pattern"})
        eng = MemoryEngine(pool)
        rows = await eng.retrieve(TEST_EID, {}, limit=20, source_context=A, count_access=False)
        got = {r["content"] for r in rows}
        check("thread set: this thread's rows and the person types, not another thread's",
              "decided the invoice app ships monthly" in got
              and "prefers dashboards over walls of text" in got
              and "verify a build's terminal status before reporting it" in got
              and "decided the garden plan waits for spring" not in got
              and "prefers the garden plan in metric units" not in got, f"{sorted(got)}")
        check("thread set: rows written outside any thread and person patterns stay; "
              "a scope-less turn does not",
              "genesis: the host was born on the invoice project" in got
              and "they settle invoices before weekends" in got
              and "an unscoped old reply from somewhere" not in got, f"{sorted(got)}")
        acc = await pool.fetchval(
            "SELECT COALESCE(SUM(access_count),0) FROM entity_memory WHERE entity_id=$1", TEST_EID)
        check("thread set: count_access=False counts nothing", acc == 0, f"access={acc}")
        rows = await eng.retrieve(TEST_EID, {}, limit=20)
        got = {r["content"] for r in rows}
        check("no scope: every row, as before",
              "decided the garden plan waits for spring" in got and len(got) >= 6, f"{sorted(got)}")
        acc = await pool.fetchval(
            "SELECT COALESCE(SUM(access_count),0) FROM entity_memory WHERE entity_id=$1", TEST_EID)
        check("no scope, default: retrieve still counts access (other callers unchanged)",
              acc >= 4, f"access={acc}")
        del a_fact, b_fact, pref, rule
        await _clean(pool)

        # ── 1b. the turn-scope clause every recall arm reads through, and the
        # Host's decision about a row (metadata.about) surviving what moves it
        from mastercode.memory_retrieval import recall_memories
        from mastercode.memory_consolidation import MemoryConsolidator
        await _row(pool, "invoices: prefers totals rounded to the cent", "preference")
        await _row(pool, "invoices: prefers the project's invoices grouped by client", "preference", scope=A)
        await _row(pool, "invoices: prefers card payments kept out of general bills", "preference", scope=B)
        await _row(pool, "invoices: the person laughs about late invoices", "relationship", scope=B)
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata) "
            "VALUES ($1, 'invoices: a row whose scope is a JSON null', 'preference', 'active', "
            "'{\"source_context\": null}'::jsonb)", TEST_EID)

        async def _seen(scope):
            r = await recall_memories(pool, TEST_EID, "invoices", limit=20,
                                      memory_types=["preference", "relationship"],
                                      turn_scope=scope, scope_everywhere_types=["relationship"])
            return {h["content"] for h in r["hits"]}
        got = await _seen(A)
        check("turn scope: global, this scope and relationship notes; another scope never",
              "invoices: prefers totals rounded to the cent" in got
              and "invoices: prefers the project's invoices grouped by client" in got
              and "invoices: the person laughs about late invoices" in got
              and "invoices: prefers card payments kept out of general bills" not in got, f"{sorted(got)}")
        check("turn scope: a present-but-null scope is not global (one rule, in SQL)",
              "invoices: a row whose scope is a JSON null" not in got, f"{sorted(got)}")
        got = await _seen({})
        check("turn scope: no scope known reads global rows only",
              "invoices: prefers totals rounded to the cent" in got
              and "invoices: prefers the project's invoices grouped by client" not in got, f"{sorted(got)}")
        got = await _seen([A, B])
        check("turn scope: a list of scopes reads each of them",
              "invoices: prefers card payments kept out of general bills" in got
              and "invoices: prefers the project's invoices grouped by client" in got, f"{sorted(got)}")

        # The Host's decision (metadata.about) decides where a row is read,
        # over where it was said; a JSON-null provenance set global reads global.
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata) VALUES "
            "($1, 'invoices: said in the finance app, about the person', 'preference', 'active', "
            "'{\"source_context\": {\"kind\": \"app\", \"ref\": \"circles\"}, \"about\": {\"kind\": \"global\"}}'::jsonb), "
            "($1, 'invoices: said here, about the finance app', 'preference', 'active', "
            "'{\"source_context\": {\"kind\": \"project\", \"ref\": \"9933101\"}, "
            "\"about\": {\"kind\": \"app\", \"ref\": \"circles\"}}'::jsonb)", TEST_EID)
        got = await _seen(A)
        check("about: a row set global rides a scope it was never said in",
              "invoices: said in the finance app, about the person" in got, f"{sorted(got)}")
        check("about: a row set to an app leaves the scope it was said in",
              "invoices: said here, about the finance app" not in got, f"{sorted(got)}")
        rows = await eng.retrieve(TEST_EID, {}, limit=50, source_context=A, count_access=False)
        got = {r["content"] for r in rows}
        check("about: the standing list reads the same scope",
              "invoices: said in the finance app, about the person" in got
              and "invoices: said here, about the finance app" not in got, f"{sorted(got)}")
        await _clean(pool)

        # A restatement keeps the Host's decision, and only that: provenance
        # stays the new row's own, so thread delete still finds it.
        from mastercode import circles_threads as ct
        from mastercode.memory_engine import active_embedding_column, embedding_cast_type
        HOST = {"about": {"kind": "global"}, "scoped_by": "host",
                "scoped_at": "2026-09-30T00:00:00+00:00",
                "scope_history": [{"from": "where said", "at": "x"}]}
        t = await ct.create_thread(pool, TEST_EID, title="scope gate thread")
        tid = str(t["id"])
        g = await _row(pool, "prefers dashboards", "preference", meta=HOST)
        n = await _row(pool, "prefers dashboards, always", "preference",
                       scope=ct.descriptor_for(tid))
        await MemoryConsolidator(pool)._supersede(TEST_EID, g, n, superseded_in=ct.descriptor_for(tid))
        md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", n))
        check("restatement: the new row takes the decision and keeps its own provenance",
              md.get("about") == {"kind": "global"} and md.get("scope_history")
              and md.get("source_context") == ct.descriptor_for(tid), f"{md}")
        await ct.delete_thread(pool, TEST_EID, tid)
        st = {r["id"]: (r["status"], r["live"]) for r in await pool.fetch(
            "SELECT id, status, t_valid_until IS NULL AS live FROM entity_memory WHERE id = ANY($1::int[])",
            [g, n])}
        check("thread delete: what was said in the thread goes; the row it replaced comes back",
              st.get(n, ("",))[0] == "archived" and st.get(g) == ("active", True), f"{st}")
        fact = await _row(pool, "prefers dashboards, as a fact", "cited_fact")
        g2 = await _row(pool, "prefers kanban", "preference", meta=HOST)
        await MemoryConsolidator(pool)._supersede(TEST_EID, g2, fact)
        md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", fact))
        check("restatement: a row of another type never inherits the decision", "about" not in md, f"{md}")
        await _clean(pool)

        # The vector column, for the rows below that need a real embedding.
        col, dim = await active_embedding_column(pool, TEST_EID)
        cast = embedding_cast_type(dim)
        evec = "[" + ",".join(["0.1"] * dim) + "]"

        async def _vrow(content, meta, age, mtype="preference", vec=None):
            return await pool.fetchval(
                f"INSERT INTO entity_memory (entity_id, content, memory_type, status, "
                f"metadata, created_at, {col}) VALUES ($1, $2, $6, 'active', "
                f"$3::jsonb, now() - $4::interval, $5::{cast}) RETURNING id",
                TEST_EID, content, json.dumps(meta), age, vec or evec, mtype)
        from datetime import timedelta

        # An undecided row is read wherever it was said (held_in); a decision
        # replaces that.
        C = {"kind": "project", "ref": "9933102"}

        async def _seen_types(scope, q="prefers"):
            r = await recall_memories(pool, TEST_EID, q, limit=30,
                                      memory_types=["preference"], turn_scope=scope)
            return {h["content"] for h in r["hits"]}
        await _row(pool, "prefers dark mode", "preference", scope=A,
                   meta={"held_in": [{**B, "at": "2026-09-30T00:00:00+00:00"}]})
        check("held_in: a row said again in B reaches B, not a third place",
              "prefers dark mode" in await _seen_types(B)
              and "prefers dark mode" not in await _seen_types(C))
        await _row(pool, "prefers dark mode everywhere", "preference", scope=A,
                   meta={"held_in": [{"kind": "app", "ref": "memory", "at": "x"}]})
        check("held_in: a row said again where no scope applied is global",
              "prefers dark mode everywhere" in await _seen_types(C))
        await _row(pool, "prefers dark mode in one project", "preference", scope=A,
                   meta={"about": {"kind": "project", "ref": "9933102"},
                         "held_in": [{**B, "at": "x"}, {"kind": "app", "ref": "memory", "at": "x"}]})
        check("held_in: a decision replaces where it was said",
              "prefers dark mode in one project" in await _seen_types(C)
              and "prefers dark mode in one project" not in await _seen_types(B)
              and "prefers dark mode in one project" not in await _seen_types({}))
        await _clean(pool)

        # A decision that names the thread being deleted leaves with it, and
        # never strands what a live conversation also said.
        t = await ct.create_thread(pool, TEST_EID, title="scope gate D1")
        D = str(t["id"])
        w = await _row(pool, "prefers grams", "preference", scope=ct.descriptor_for(D),
                       meta={"about": {"kind": "thread", "ref": D},
                             "held_in": [{**A, "at": "2026-09-29T00:00:00+00:00"}]})
        await ct.delete_thread(pool, TEST_EID, D)
        st = await pool.fetchrow("SELECT status, metadata::text AS md FROM entity_memory WHERE id = $1", w)
        md = json.loads(st["md"])
        check("thread delete: a row A said word for word stays A's, undecided, and A reads it",
              st["status"] == "active" and md.get("source_context") == A and "about" not in md
              and "prefers grams" in await _seen_types(A, "grams"), f"{dict(st)}")
        await _clean(pool)
        t = await ct.create_thread(pool, TEST_EID, title="scope gate D2")
        D = str(t["id"])
        await _row(pool, "prefers kilograms", "preference", scope=ct.descriptor_for(D),
                   meta={"about": {"kind": "thread", "ref": D},
                         "held_in": [{**A, "at": "2026-09-29T00:00:00+00:00",
                                      "words": "prefers kilograms always please"}]})
        await ct.delete_thread(pool, TEST_EID, D)
        live = await pool.fetch("SELECT content, metadata::text AS md FROM entity_memory "
                                "WHERE entity_id = $1 AND status = 'active' AND memory_type = 'preference'",
                                TEST_EID)
        check("thread delete: A's own words become A's row, without a decision about the deleted thread",
              len(live) == 1 and live[0]["content"] == "prefers kilograms always please"
              and "about" not in json.loads(live[0]["md"])
              and "prefers kilograms always please" in await _seen_types(A, "kilograms"),
              f"{[dict(x) for x in live]}")
        await _clean(pool)

        # Consolidator off (Phase 2.3): a turn starts from the thread's goal and
        # the last close_turn handoff, read from the real rows.
        from mastercode.runtime import close_turn as _ctn2
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata) VALUES "
            "($1, 'Ship the new desktop', 'goal', 'active', $2::jsonb), "
            "($1, 'the reply', 'turn', 'active', $3::jsonb)", TEST_EID,
            json.dumps({"source_context": A}),
            json.dumps({"source_context": A, "role": "assistant",
                        "close_turn": {"handoff": {"next_step": "wire the taskbar",
                                                   "identifiers": ["artifact:9f2"]}}}))
        st = await _ctn2.thread_state(pool, TEST_EID, A)
        check("thread state: the goal and the last handoff of THIS thread, from real rows",
              st and "Ship the new desktop" in st and "wire the taskbar" in st and "artifact:9f2" in st
              and await _ctn2.thread_state(pool, TEST_EID, B) is None, st)
        # A later replay, A/B variant or wake-up under the same descriptor is
        # not the conversation: its handoff never leads, its rows never ride.
        for tag in ("circles-replay", "circles-ab", "scheduled_task"):
            await pool.execute(
                "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata, created_at) "
                "VALUES ($1, $2, 'turn', 'active', $3::jsonb, now() + interval '1 minute')", TEST_EID,
                f"variant reply {tag}",
                json.dumps({"source_context": A, "role": "assistant", "app_id": tag,
                            "close_turn": {"handoff": {"next_step": f"from {tag}"}}}))
        st = await _ctn2.thread_state(pool, TEST_EID, A)
        check("thread state: a replay, A/B or wake-up handoff never becomes the last turn's",
              st and "wire the taskbar" in st
              and not any(f"from {t}" in st for t in ("circles-replay", "circles-ab", "scheduled_task")), st)
        tn2 = await ct.create_thread(pool, TEST_EID, title="scope gate hidden lanes")
        TD = ct.descriptor_for(str(tn2["id"]))
        for tag, content in (("", "the real reply"), ("circles-replay", "a replayed reply"),
                             ("circles-ab", "an A/B reply"), ("scheduled_task", "a wake-up reply")):
            await pool.execute(
                "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata) "
                "VALUES ($1, $2, 'turn', 'active', $3::jsonb)", TEST_EID, content,
                json.dumps({"source_context": TD, "role": "assistant", **({"app_id": tag} if tag else {})}))
        from mastercode.memory_engine import recall_recent_turns
        got = {t.get("content") for t in await recall_recent_turns(pool, TEST_EID, "", limit=20,
                                                                    source_context=TD)}
        check("thread history: replayed, A/B and wake-up rows never ride the next turn",
              got == {"the real reply"}, f"{got}")
        await ct.delete_thread(pool, TEST_EID, str(tn2["id"]))
        await _clean(pool)

        # close_turn: a preference in a held row's topic replaces it, never a
        # second live row (memory_slots); a near wording with no topic is no
        # longer merged by its vector (the embedding auto-supersede is gone).
        from mastercode.memory_slots import key_row as _key_row
        _vec_str = "[" + ",".join(["0.1"] * dim) + "]"

        async def _fixed_vec(*a, **k):
            return _vec_str
        _saved_embed = _ctn2._embed
        _ctn2._embed = _fixed_vec
        try:
            held_near = await _vrow("prefers dark themes in every app", {"about": {"kind": "global"}},
                                    timedelta(days=1))
            await _key_row(pool, TEST_EID, held_near, "theme")
            wn = await _ctn2.apply(pool, TEST_EID, {"preferences": [
                {"text": "prefers dark themes in every single app", "reason": "r", "topic": "theme",
                 "scope": {"kind": "global"}}]}, session_id="gate-near", turn_scope=A)
            live_n = await pool.fetchval(
                "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND memory_type = 'preference' "
                "AND status = 'active' AND t_valid_until IS NULL AND content LIKE 'prefers dark themes%'",
                TEST_EID)
            check("close_turn same topic: replaces the held row (one live row, the newest wording)",
                  (wn.get("preferences") or [{}])[0].get("replaced") == held_near and live_n == 1, f"{wn} {live_n}")
            await _clean(pool)
            await _vrow("prefers dark themes in every app", {"about": {"kind": "global"}}, timedelta(days=1))
            wn = await _ctn2.apply(pool, TEST_EID, {"preferences": [
                {"text": "prefers dark themes in every single app", "reason": "r",
                 "scope": {"kind": "global"}}]}, session_id="gate-near", turn_scope=A)
            live_n = await pool.fetchval(
                "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND memory_type = 'preference' "
                "AND status = 'active' AND t_valid_until IS NULL AND content LIKE 'prefers dark themes%'",
                TEST_EID)
            got = (wn.get("preferences") or [{}])[0]
            check("close_turn near wording with no topic: no longer merged by its vector; both stay, and the "
                  "answer says how to give it one",
                  got.get("status") == "stored" and not got.get("replaced") and live_n == 2
                  and "op='key'" in (got.get("note") or ""), f"{wn} {live_n}")
            await _clean(pool)

            async def _live(prefix):
                return {r["content"] for r in await pool.fetch(
                    "SELECT content FROM entity_memory WHERE entity_id = $1 AND memory_type = 'preference' "
                    "AND status = 'active' AND t_valid_until IS NULL AND content LIKE $2", TEST_EID, prefix + "%")}
            tn = await ct.create_thread(pool, TEST_EID, title="scope gate near")
            TN = ct.descriptor_for(str(tn["id"]))
            # A correction, in the thread where an undecided row was said: its
            # slot is that thread's, so the same topic there replaces it.
            said = await _vrow("prefers dark mode in the editor", {"source_context": TN}, timedelta(days=1))
            await _key_row(pool, TEST_EID, said, "editor_mode")
            wn = await _ctn2.apply(pool, TEST_EID, {"preferences": [
                {"text": "prefers light mode in the editor", "reason": "corrected", "topic": "editor_mode",
                 "scope": {"kind": "thread"}}]}, session_id="gate-near", turn_scope=TN)
            got = (wn.get("preferences") or [{}])[0]
            check("close_turn same topic: a correction where the held row was said replaces it",
                  got.get("status") == "stored" and got.get("replaced") == said
                  and await _live("prefers ") == {"prefers light mode in the editor"}, f"{wn}")
            await pool.execute("UPDATE entity_memory SET resolution_pointer = NULL WHERE entity_id = $1", TEST_EID)
            await pool.execute("DELETE FROM entity_memory WHERE entity_id = $1 AND memory_type = 'preference'",
                               TEST_EID)
            # An exception for one thread to a rule held everywhere: the same
            # topic in two scopes is two slots, and both stay.
            wide = await _vrow("prefers replies in english", {"about": {"kind": "global"}}, timedelta(days=1))
            await _key_row(pool, TEST_EID, wide, "reply_language")
            wn = await _ctn2.apply(pool, TEST_EID, {"preferences": [
                {"text": "prefers replies in french", "reason": "this thread", "topic": "reply_language",
                 "scope": {"kind": "thread"}}]}, session_id="gate-near", turn_scope=TN)
            got = (wn.get("preferences") or [{}])[0]
            check("close_turn same topic, narrower scope: an exception is its own row; the wider rule stays",
                  got.get("status") == "stored" and not got.get("replaced")
                  and await _live("prefers replies") == {"prefers replies in english", "prefers replies in french"},
                  f"{wn}")
            await ct.delete_thread(pool, TEST_EID, str(tn["id"]))
        finally:
            _ctn2._embed = _saved_embed
        await _clean(pool)

        # An external client's identity prompt: prompt types, global preferences.
        from server.synaptive_handler import recent_identity_memories
        await _row(pool, "id pref global", "preference")
        await _row(pool, "id pref scoped", "preference", scope=A)
        await _row(pool, "id pref said again unscoped", "preference", scope=A,
                   meta={"held_in": [{"kind": "app", "ref": "memory"}]})
        await _row(pool, "id rel scoped", "relationship", scope=A)
        await _row(pool, "id fact", "cited_fact")
        got = {c for _t, c in await recent_identity_memories(pool, TEST_EID)}
        check("identity: global preferences and relationship notes, nothing scoped, no facts",
              got == {"id pref global", "id pref said again unscoped", "id rel scoped"}, f"{sorted(got)}")
        await _clean(pool)

        # The link of a dropped row, a winner's own decision, typed project turns.
        dropped = await pool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, t_valid_until, "
            "metadata) VALUES ($1, 'prefers serif fonts', 'preference', 'active', NOW(), $2::jsonb) "
            "RETURNING id", TEST_EID, json.dumps(HOST))
        linked = await _row(pool, "prefers serif fonts in documents", "preference", scope=A)
        ok_link = await MemoryConsolidator(pool)._link(TEST_EID, dropped, linked)
        md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", linked))
        check("link: the row that replaces a dropped one takes its decision",
              ok_link and md.get("about") == {"kind": "global"}, f"{ok_link} {md}")
        await _clean(pool)
        pa = await _row(pool, "invoices: prefers the project's own layout", "preference",
                        meta={"about": {"kind": "project", "ref": "9933101"}})
        await _row(pool, "invoices: prefers plain words", "preference", meta={"about": {"kind": "global"}})
        await _row(pool, "invoices: prefers a finance-only rule", "preference",
                   meta={"about": {"kind": "app", "ref": "circles"}})
        r = await recall_memories(pool, TEST_EID, "invoices", limit=20, memory_types=["preference"],
                                  project_ids=[9933101])
        got = {h["content"] for h in r["hits"]}
        check("typed project turn: rows about that project and global ones; nothing else",
              got == {"invoices: prefers the project's own layout", "invoices: prefers plain words"},
              f"{sorted(got)}")
        del pa
        await _clean(pool)

        # Deleting a thread drops a decision naming it; the row, said
        # elsewhere, lives on there, undecided.
        t = await ct.create_thread(pool, TEST_EID, title="scope gate about-thread")
        tid = str(t["id"])
        about_t = await _row(pool, "prefers this recipe thread's metric units", "preference", scope=A,
                             meta={"about": {"kind": "thread", "ref": tid}, "scoped_by": "host"})
        await ct.delete_thread(pool, TEST_EID, tid)
        st = await pool.fetchrow("SELECT status, metadata::text AS md FROM entity_memory WHERE id = $1", about_t)
        md = json.loads(st["md"])
        check("thread delete: a row said elsewhere loses only the decision naming the thread",
              st["status"] == "active" and "about" not in md and "scoped_by" not in md
              and md.get("source_context") == A, f"{dict(st)}")
        await _clean(pool)

        # memory_scope's thread write, on the real schema: a live thread is
        # written under its share lock; a deleted one is refused.
        import server.brain_service as _bs
        from mastercode.runtime import tools as _tools
        _saved_bs = (_bs.get_pool, getattr(_bs, "_entity_id", None))
        _bs.get_pool, _bs._entity_id = (lambda: pool), TEST_EID
        try:
            t = await ct.create_thread(pool, TEST_EID, title="scope gate live")
            live_t = str(t["id"])
            p1 = await _row(pool, "prefers bread recipes by weight", "preference", scope=A)
            r1 = await _tools._exec_memory_scope({"memory_id": p1, "kind": "thread", "ref": live_t.upper()})
            md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", p1))
            check("memory_scope: a live thread is written (canonical id), on the real schema",
                  r1.get("status") == "ok" and md.get("about") == {"kind": "thread", "ref": live_t}, f"{r1} {md}")
            await ct.delete_thread(pool, TEST_EID, live_t)
            p2 = await _row(pool, "prefers rye", "preference", scope=A)
            r2 = await _tools._exec_memory_scope({"memory_id": p2, "kind": "thread", "ref": live_t})
            md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", p2))
            check("memory_scope: a deleted thread is refused, nothing written",
                  r2.get("status") == "error" and "about" not in md, f"{r2} {md}")
            # The write's own guard, with the earlier live-thread check out of
            # the way (the race it closes: a delete landing between the two).
            _ref_check = _tools._memory_scope_ref

            async def _stale_ok(pool_, eid, kind, ref):
                return ref, None
            _tools._memory_scope_ref = _stale_ok
            try:
                p3 = await _row(pool, "prefers sourdough", "preference", scope=A)
                r4 = await _tools._exec_memory_scope({"memory_id": p3, "kind": "thread", "ref": live_t})
            finally:
                _tools._memory_scope_ref = _ref_check
            md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", p3))
            check("memory_scope: the write itself refuses a deleted thread (the guard, not the check before it)",
                  r4.get("status") == "error" and "was deleted" in str(r4.get("result")) and "about" not in md,
                  f"{r4} {md}")
            r3 = await _tools._exec_memory_scope({"kind": "list"})
            check("memory_scope: list runs on the real schema",
                  r3.get("status") == "ok" and r3["result"]["preferences"] >= 1, f"{r3}")
            await _clean(pool)

            # memory_curate: the Host's add / replace / drop, carried out as
            # given — no model, on the real schema.
            async def _live_row(mid):
                return await pool.fetchrow(
                    "SELECT memory_type, content, status, t_valid_until IS NULL AS live, "
                    "resolution_pointer, metadata::text AS md FROM entity_memory WHERE id = $1", mid)
            c1 = await _tools._exec_memory_curate(
                {"op": "add", "content": "prefers invoices sent on the first", "about": "person"})
            m1 = (c1.get("result") or {}).get("memory_id")
            row = await _live_row(m1) if m1 else None
            check("memory_curate add: a live preference, written by memory_curate, retrievable",
                  c1.get("status") == "ok" and row and row["memory_type"] == "preference" and row["live"]
                  and json.loads(row["md"]).get("written_by") == "memory_curate"
                  and (c1["result"].get("retrievable") or {}).get("retrievable") is True, f"{c1}")
            c2 = await _tools._exec_memory_curate(
                {"op": "add", "content": "prefers invoices sent on the first", "about": "person"})
            check("memory_curate add: the same words again are held once",
                  (c2.get("result") or {}).get("action") == "already_held"
                  and c2["result"].get("memory_id") == m1, f"{c2}")
            c3 = await _tools._exec_memory_curate(
                {"op": "replace", "memory_id": m1, "content": "prefers invoices sent on the fifth",
                 "about": "person"})
            m3 = (c3.get("result") or {}).get("memory_id")
            old_row, new_row = await _live_row(m1), (await _live_row(m3) if m3 else None)
            check("memory_curate replace: the new row is live and the old one retired into it",
                  c3.get("status") == "ok" and c3["result"].get("replaced") == m1 and new_row
                  and new_row["live"] and not old_row["live"] and old_row["resolution_pointer"] == m3,
                  f"{c3}")
            c4 = await _tools._exec_memory_curate({"op": "drop", "memory_id": m3})
            check("memory_curate drop: retired with no replacement",
                  (c4.get("result") or {}).get("dropped") is True
                  and not (await _live_row(m3))["live"], f"{c4}")
            c6 = await _tools._exec_memory_curate({"op": "drop", "memory_id": m3})
            check("memory_curate drop: a row already dropped is done, not refused, and says so",
                  c6.get("status") == "ok" and (c6.get("result") or {}).get("dropped") is False
                  and (c6["result"].get("already") or "").startswith(f"#{m3} was already dropped on "),
                  f"{c6}")
            arch = await _row(pool, "prefers the quarterly report on paper", "preference",
                              meta={"rule_kind": "taught"})
            await pool.execute("UPDATE entity_memory SET status = 'archived', status_changed_at = "
                               "'2026-03-04 12:00+00', status_reason = 'a bulk pass' WHERE id = $1", arch)
            c7 = await _tools._exec_memory_curate({"op": "drop", "memory_id": arch})
            check("memory_curate drop: an archived row is done, not refused: 'already archived on <date>'",
                  c7.get("status") == "ok" and (c7.get("result") or {}).get("already")
                  == f"#{arch} was already archived on 2026-03-04; nothing to drop", f"{c7}")
            c8 = await _tools._exec_memory_curate({"op": "drop", "memory_id": 987654321})
            check("memory_curate drop: an id this Host never held is still refused",
                  c8.get("status") == "error" and "not a live memory" in c8.get("result", ""), f"{c8}")
            # A row dated to leave later has not left (t_valid_until ahead), so
            # its drop is refused as before, never "already dropped on <tomorrow>".
            ahead = await _row(pool, "prefers the minutes by noon", "preference")
            await pool.execute("UPDATE entity_memory SET t_valid_until = now() + interval '1 day' "
                               "WHERE id = $1", ahead)
            c8b = await _tools._exec_memory_curate({"op": "drop", "memory_id": ahead})
            check("memory_curate drop: a row dated to leave later is refused, not called already dropped",
                  c8b.get("status") == "error" and "already" not in json.dumps(c8b), f"{c8b}")
            c9 = await _tools._exec_memory_curate({"op": "replace", "memory_id": arch, "about": "person",
                                                   "content": "prefers the quarterly report by email"})
            check("memory_curate replace: an archived row is still refused (nothing to correct)",
                  c9.get("status") == "error" and "not a live memory" in c9.get("result", ""), f"{c9}")
            await _clean(pool)

            # A correction keeps every place the corrected row was read, from
            # whichever conversation it is made in; the same words from a
            # conversation that does not read the held row make it read there.
            import mastercode.memory_engine as _me2
            C = {"kind": "project", "ref": "9933103"}
            _tok = _me2.CURRENT_TURN_SCOPE.set({"session_id": "cur-B", "scope": B})
            try:
                async def _cur(args):
                    return await _tools._exec_memory_curate(args, session_id="cur-B")
                g = await _row(pool, "prefers amounts rounded to the cent", "preference")
                r1 = await _cur({"op": "replace", "memory_id": g, "about": "person",
                                 "content": "prefers amounts rounded to the dollar"})
                seen1 = [await _seen_types(sc, "rounded") for sc in (A, B, C)]
                check("memory_curate replace: a global rule corrected in one conversation stays global",
                      r1.get("status") == "ok"
                      and all("prefers amounts rounded to the dollar" in x
                              and "prefers amounts rounded to the cent" not in x for x in seen1),
                      f"{r1} {seen1}")
                inA = await _row(pool, "prefers invoices grouped by month", "preference", scope=A)
                r2 = await _cur({"op": "replace", "memory_id": inA, "about": "person",
                                 "content": "prefers invoices grouped by quarter"})
                check("memory_curate replace: a rule said in A, corrected from B, is read where it was: in A",
                      "prefers invoices grouped by quarter" in await _seen_types(A, "invoices")
                      and "prefers invoices grouped by month" not in await _seen_types(A, "invoices")
                      and "prefers invoices grouped by quarter" not in await _seen_types(C, "invoices"), f"{r2}")
                multi = await _row(pool, "prefers short invoices", "preference", scope=A,
                                   meta={"held_in": [{**C, "at": "x"}]})
                rm = await _cur({"op": "replace", "memory_id": multi, "about": "person",
                                 "content": "prefers very short invoices"})
                check("memory_curate replace: a rule read in several places with no scope is scoped first",
                      rm.get("status") == "error" and "memory_scope" in rm.get("result", ""), f"{rm}")
                rel = await _row(pool, "their sister lives in Austin", "relationship")
                r3 = await _cur({"op": "replace", "memory_id": rel, "about": "person",
                                 "content": "their sister lives in Denver"})
                row3 = await pool.fetchrow("SELECT memory_type, t_valid_until IS NULL AS live FROM entity_memory "
                                           "WHERE id = $1", (r3.get("result") or {}).get("memory_id") or 0)
                check("memory_curate replace: a relationship note stays a relationship note",
                      row3 and row3["memory_type"] == "relationship" and row3["live"], f"{r3}")
                fact = await _row(pool, "the invoices go out monthly", "cited_fact")
                r4 = await _cur({"op": "replace", "memory_id": fact, "about": "person",
                                 "content": "prefers invoices to go out monthly"})
                check("memory_curate replace: a fact is not rewritten as a preference (drop and add instead)",
                      r4.get("status") == "error" and "drop it" in r4.get("result", "").lower(), f"{r4}")
                said_a = await _row(pool, "prefers receipts as pdf files", "preference", scope=A)
                r5 = await _cur({"op": "add", "about": "person", "content": "prefers receipts as pdf files"})
                check("memory_curate add: the same words said in A, added from B, are read in B too",
                      (r5.get("result") or {}).get("memory_id") == said_a
                      and "prefers receipts as pdf files" in await _seen_types(B, "receipts"), f"{r5}")
                dec_c = await _row(pool, "prefers dark charts", "preference", meta={"about": dict(C)})
                r6 = await _cur({"op": "add", "about": "person", "content": "prefers dark charts"})
                check("memory_curate add: words decided for another place get a row of their own here",
                      (r6.get("result") or {}).get("action") == "add"
                      and (r6.get("result") or {}).get("memory_id") != dec_c
                      and "prefers dark charts" in await _seen_types(B, "charts"), f"{r6}")
                # "Remember this:" kept the words as they arrived: close_turn does
                # not keep them a second time as a preference in the same turn.
                _tt = _me2.CURRENT_TURN_TAUGHT.set({"session_id": "cur-B",
                                                    "texts": ["invoices go out on the 1st"]})
                try:
                    from mastercode.runtime import close_turn as _ctt
                    ra = await _ctt.apply(pool, TEST_EID, {"preferences": [
                        {"text": "prefers invoices go out on the 1st", "reason": "said so",
                         "scope": {"kind": "global"}}]}, session_id="cur-B", turn_scope=B)
                finally:
                    _me2.CURRENT_TURN_TAUGHT.reset(_tt)
                got = (ra.get("preferences") or [{}])[0]
                check("close_turn: a rule kept word for word this turn is not stored again as a preference",
                      got.get("status") == "already_held" and got.get("taught") is True, f"{ra}")
                # PII mode: memory_curate's words go through the PII queue too.
                from mastercode import pii_queue as _pq2, pii_client as _pc2
                _env2 = {k: os.environ.get(k) for k in ("PII_MODE", "PII_QUEUE_WORKER")}
                _st2 = _pc2.sterilize
                os.environ["PII_MODE"], os.environ["PII_QUEUE_WORKER"] = "on", "off"

                async def _fake_st2(text, scope, **kw):
                    return _pc2.PiiResult(ok=True, text=text.replace("Bob", "[PERSON_2]"))
                _pc2.sterilize = _fake_st2
                try:
                    rq = await _cur({"op": "add", "about": "person", "content": "prefers Bob on the invoices"})
                    before = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 "
                                                 "AND content LIKE '%Bob%'", TEST_EID)
                    while await _pq2.drain_once():
                        pass
                    landed = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 "
                                                 "AND content = 'prefers [PERSON_2] on the invoices'", TEST_EID)
                    raw = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 "
                                              "AND (content LIKE '%Bob%' OR metadata::text LIKE '%Bob%')", TEST_EID)
                    check("memory_curate in PII mode: queued, then only the tokenized words land",
                          (rq.get("result") or {}).get("queued") is True and before == 0 and landed == 1
                          and raw == 0, f"{rq} before={before} landed={landed} raw={raw}")
                finally:
                    _pc2.sterilize = _st2
                    for k, v in _env2.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
            finally:
                _me2.CURRENT_TURN_SCOPE.reset(_tok)
            await _clean(pool)

            # Deleting the conversation a correction was made in undoes it:
            # the correction goes with the conversation, the corrected row
            # comes back, and nothing else changes.
            tt = await ct.create_thread(pool, TEST_EID, title="scope gate correct")
            TT = ct.descriptor_for(str(tt["id"]))
            _tok = _me2.CURRENT_TURN_SCOPE.set({"session_id": "cur-T", "scope": TT})
            try:
                async def _curT(args):
                    return await _tools._exec_memory_curate(args, session_id="cur-T")
                k_glob = await _row(pool, "prefers totals rounded to the cent", "preference")
                k_dec = await _row(pool, "prefers totals shown in euros", "preference",
                                   meta={"about": {"kind": "global"}, "scoped_by": "host"})
                k_rel = await _row(pool, "their brother lives in Austin", "relationship")
                k_a = await _row(pool, "prefers totals at the top", "preference", scope=A)
                for tid, words in ((k_glob, "prefers totals rounded to the dollar"),
                                   (k_dec, "prefers totals shown in dollars"),
                                   (k_rel, "their brother lives in Denver"),
                                   (k_a, "prefers totals at the bottom")):
                    rr = await _curT({"op": "replace", "memory_id": tid, "about": "person", "content": words})
                    assert rr.get("status") == "ok", rr
                seen_before = [await _seen_types(sc, "totals") for sc in (A, C)]
            finally:
                _me2.CURRENT_TURN_SCOPE.reset(_tok)
            await ct.delete_thread(pool, TEST_EID, str(tt["id"]))
            live_after = {r["content"] for r in await pool.fetch(
                "SELECT content FROM entity_memory WHERE entity_id = $1 AND status = 'active' "
                "AND t_valid_until IS NULL AND memory_type IN ('preference', 'relationship')", TEST_EID)}
            seen_after = [await _seen_types(sc, "totals") for sc in (A, C)]
            check("memory_curate replace: corrections are read where the old rows were (global, decided, in A)",
                  "prefers totals rounded to the dollar" in seen_before[1]
                  and "prefers totals shown in dollars" in seen_before[1]
                  and "prefers totals at the bottom" in seen_before[0]
                  and "prefers totals at the bottom" not in seen_before[1], f"{seen_before}")
            # ...and deleting the conversation the corrected rule was said in
            # takes the correction too: it was read only there.
            t_ca = await ct.create_thread(pool, TEST_EID, title="scope gate corrected-in")
            TA = ct.descriptor_for(str(t_ca["id"]))
            k_ta = await _row(pool, "prefers receipts filed weekly", "preference", scope=TA)
            _tok = _me2.CURRENT_TURN_SCOPE.set({"session_id": "cur-B2", "scope": B})
            try:
                rk = await _tools._exec_memory_curate({"op": "replace", "memory_id": k_ta, "about": "person",
                                                       "content": "prefers receipts filed daily"},
                                                      session_id="cur-B2")
            finally:
                _me2.CURRENT_TURN_SCOPE.reset(_tok)
            read_before = await _seen_types(TA, "receipts")
            await ct.delete_thread(pool, TEST_EID, str(t_ca["id"]))
            left = await pool.fetchval(
                "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND status = 'active' "
                "AND content LIKE 'prefers receipts filed%'", TEST_EID)
            check("memory_curate replace: deleting the corrected rule's conversation takes the correction too",
                  rk.get("status") == "ok" and "prefers receipts filed daily" in read_before
                  and left == 0 and "prefers receipts filed daily" not in await _seen_types(B, "receipts"),
                  f"{rk} {read_before} left={left}")
            check("memory_curate replace: deleting the conversation a correction was made in undoes it",
                  live_after == {"prefers totals rounded to the cent", "prefers totals shown in euros",
                                 "their brother lives in Austin", "prefers totals at the top"}
                  and "prefers totals at the top" in seen_after[0]
                  and "prefers totals rounded to the cent" in seen_after[1], f"{live_after} {seen_after}")
        finally:
            _bs.get_pool, _bs._entity_id = _saved_bs
        await _clean(pool)

        # A late row of a deleted thread taken over by another conversation
        # drops a decision naming that thread.
        t = await ct.create_thread(pool, TEST_EID, title="scope gate late")
        late_t = str(t["id"])
        late = await _row(pool, "prefers rye crackers", "preference", scope=ct.descriptor_for(late_t),
                          meta={"about": {"kind": "thread", "ref": late_t}, "scoped_by": "host"})
        await pool.execute("UPDATE circles_threads SET deleted_at = NOW() WHERE id = $1::uuid", late_t)
        held = await MemoryConsolidator(pool).note_held(TEST_EID, late, A)
        md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", late))
        check("takeover: the late row becomes A's and loses the decision naming the deleted thread",
              held and md.get("source_context") == A and "about" not in md and "scoped_by" not in md, f"{held} {md}")
        await _clean(pool)

        # close_turn's reading of a held row (_held_row) agrees with recall on
        # every shape: where _held_row says a row reaches, recall reads it.
        from mastercode.runtime import close_turn as _ctr
        C3 = {"kind": "project", "ref": "9933102"}
        MEM = {"kind": "app", "ref": "memory"}
        shapes = {
            "about global": {"about": {"kind": "global"}},
            "about project int ref": {"about": {"kind": "project", "ref": 9933101}},
            "about null": {"about": None},
            "about global empty ref": {"about": {"kind": "global", "ref": ""}},
            "no scope at all": {},
            "source_context null": {"source_context": None},
            "said in A": {"source_context": A},
            "said in A, again in B": {"source_context": A, "held_in": [{**B, "at": "x"}]},
            "said in A, again in C (int ref)": {"source_context": A,
                                               "held_in": [{"kind": "project", "ref": 9933102, "at": "x"}]},
            "said in A, again unscoped": {"source_context": A, "held_in": [{**MEM, "at": "x"}]},
            "about app memory": {"about": MEM},
        }
        ids = {}
        for n, (label, meta) in enumerate(shapes.items()):
            ids[label] = await pool.fetchval(
                "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata) "
                "VALUES ($1, $2, 'preference', 'active', $3::jsonb) RETURNING id",
                TEST_EID, f"agreeword shape {n}", json.dumps(meta))
        places = [A, B, C3, MEM, {}]
        seen_by = {}
        for pl in places:
            r = await recall_memories(pool, TEST_EID, "agreeword", limit=50,
                                      memory_types=["preference"], turn_scope=pl)
            seen_by[json.dumps(pl, sort_keys=True)] = {h["id"] for h in r["hits"]}
        disagree = []
        for label, rid in ids.items():
            row = await pool.fetchrow(
                "SELECT id, metadata->'about' AS about, metadata->'source_context' AS sc, "
                "metadata->'held_in' AS held_in, metadata ? 'about' AS has_about, "
                "metadata ? 'source_context' AS has_sc FROM entity_memory WHERE id = $1", rid)
            _, _, reach = _ctr._held_row(row)
            for pl in places:
                want = {"kind": "global"} in reach or (bool(pl) and pl in reach)
                got = rid in seen_by[json.dumps(pl, sort_keys=True)]
                if want != got:
                    disagree.append(f"{label} @ {pl or 'no scope'}: held_row={want} recall={got}")
        check("close_turn's reading of a held row agrees with recall on every shape", not disagree,
              "; ".join(disagree))
        await _clean(pool)

        # close_turn writes a preference the same way: the Host's decision in
        # about, where it was said in source_context, one row per wording.
        from mastercode.runtime import close_turn as _ctn
        t = await ct.create_thread(pool, TEST_EID, title="scope gate close_turn")
        ct_t = str(t["id"])
        ct_scope = ct.descriptor_for(ct_t)
        w1 = await _ctn.apply(pool, TEST_EID, {"preferences": [
            {"text": "prefers invoices summarised in one line", "reason": "said so",
             "scope": {"kind": "global"}}]}, session_id="gate-ct", turn_scope=ct_scope)
        pid = (w1.get("preferences") or [{}])[0].get("id")
        md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", pid)) if pid else {}
        check("close_turn: a global preference said in a thread is about=global, provenance the thread, "
              "and reaches a project it was never said in",
              md.get("about") == {"kind": "global"} and md.get("source_context") == ct_scope
              and "prefers invoices summarised in one line" in await _seen_types(A, "invoices"),
              f"{w1} {md}")
        w2 = await _ctn.apply(pool, TEST_EID, {"preferences": [
            {"text": "prefers invoices summarised in one line", "reason": "about this thread",
             "scope": {"kind": "thread"}}]}, session_id="gate-ct", turn_scope=ct_scope)
        n = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND content = $2 "
                                "AND status = 'active'", TEST_EID, "prefers invoices summarised in one line")
        check("close_turn: said again narrower, a global preference stays global and reaches the project still",
              n == 1 and (w2.get("preferences") or [{}])[0].get("status") == "already_held"
              and "prefers invoices summarised in one line" in await _seen_types(A, "invoices"), f"{w2}")
        t2 = await ct.create_thread(pool, TEST_EID, title="scope gate close_turn B")
        ct_b = ct.descriptor_for(str(t2["id"]))
        await _ctn.apply(pool, TEST_EID, {"preferences": [
            {"text": "prefers metric units", "reason": "r", "scope": {"kind": "thread"}}]},
            session_id="gate-ct", turn_scope=ct_scope)
        await _ctn.apply(pool, TEST_EID, {"preferences": [
            {"text": "prefers metric units", "reason": "r", "scope": {"kind": "thread"}}]},
            session_id="gate-ct", turn_scope=ct_b)
        check("close_turn: said in a second thread, a thread preference is added there and kept in the first",
              "prefers metric units" in await _seen_types(ct_scope, "metric")
              and "prefers metric units" in await _seen_types(ct_b, "metric"))
        w3 = await _ctn.apply(pool, TEST_EID, {"preferences": [
            {"text": "prefers metric units", "reason": "r", "scope": {"kind": "global"}}]},
            session_id="gate-ct", turn_scope=ct_b)
        check("close_turn: said as global, the held row is widened (scope_history kept), not duplicated",
              (w3.get("preferences") or [{}])[0].get("status") == "rescoped"
              and "prefers metric units" in await _seen_types(A, "metric"), f"{w3}")
        await _row(pool, "prefers short replies here", "preference", scope=ct_scope)
        w5 = await _ctn.apply(pool, TEST_EID, {"preferences": [
            {"text": "prefers short replies here", "reason": "r", "scope": {"kind": "thread"}}]},
            session_id="gate-ct", turn_scope=ct_scope)
        n = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND content = $2 "
                                "AND status = 'active'", TEST_EID, "prefers short replies here")
        check("close_turn: a row said in this thread (undecided) already reaches it — no second row",
              n == 1 and (w5.get("preferences") or [{}])[0].get("status") == "already_held", f"{w5} n={n}")
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, status, t_valid_until, metadata) "
            "VALUES ($1, 'prefers dark themes', 'preference', 'active', NOW(), '{}'::jsonb)", TEST_EID)
        w4 = await _ctn.apply(pool, TEST_EID, {"preferences": [
            {"text": "prefers dark themes", "reason": "r", "scope": {"kind": "global"}}]},
            session_id="gate-ct", turn_scope=ct_scope)
        check("close_turn: a replaced row with the same words is never picked; the preference is stored",
              (w4.get("preferences") or [{}])[0].get("status") == "stored"
              and "prefers dark themes" in await _seen_types(A, "dark"), f"{w4}")
        await _clean(pool)

        # PII mode: close_turn's words go through the PII queue — nothing lands
        # until it drains, and what lands is the tokenized text.
        from mastercode import pii_queue as _pq, pii_client as _pc
        _saved_env = {k: os.environ.get(k) for k in ("PII_MODE", "PII_QUEUE_WORKER")}
        _saved_st = _pc.sterilize
        os.environ["PII_MODE"], os.environ["PII_QUEUE_WORKER"] = "on", "off"

        async def _fake_sterilize(text, scope, **kw):
            return _pc.PiiResult(ok=True, text=text.replace("Alice", "[PERSON_1]"))
        _pc.sterilize = _fake_sterilize
        try:
            tp = await ct.create_thread(pool, TEST_EID, title=None)
            TP = ct.descriptor_for(str(tp["id"]))
            old_goal = await _row(pool, "draft the march report", "goal", scope=TP)
            wp = await _ctn.apply(pool, TEST_EID, {
                "topic": "invoices for Alice", "title": "Alice invoices",
                "preferences": [
                    {"text": "prefers Alice to review the invoices", "reason": "Alice said so",
                     "scope": {"kind": "global"}}],
                "goal": {"set": "send Alice the invoices"}}, session_id="gate-pii", turn_scope=TP)
            # The next turn says the goal is met before the queue has drained.
            wm = await _ctn.apply(pool, TEST_EID, {"goal": {"met": True}},
                                  session_id="gate-pii", turn_scope=TP)
            before = await pool.fetchval(
                "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND content LIKE '%Alice%'", TEST_EID)
            before_thread = await pool.fetchval(
                "SELECT count(*) FROM circles_threads WHERE id = $1 AND (COALESCE(topic, '') LIKE '%Alice%' "
                "OR COALESCE(title, '') LIKE '%Alice%')", tp["id"])
            from mastercode.runtime import tool_call_ledger as _tcl
            await _tcl.record(pool, entity_id=TEST_EID, lane="host", role="chat", provider="p", model="m",
                              tool="close_turn", reason=_tcl.OK,
                              status="ok", session_id="gate-pii",
                              args={"preferences": [{"text": "prefers Alice to review", "reason": "Alice"}]})
            logged = await pool.fetchval(
                "SELECT args::text FROM tool_call_log WHERE entity_id = $1 AND session_id = 'gate-pii' "
                "ORDER BY id DESC LIMIT 1", TEST_EID)
            while await _pq.drain_once():
                pass
            g_state = {r["content"]: (r["live"], r["met"]) for r in await pool.fetch(
                "SELECT content, t_valid_until IS NULL AS live, resolved_at IS NOT NULL AS met "
                "FROM entity_memory WHERE entity_id = $1 AND memory_type = 'goal'", TEST_EID)}
            title = await pool.fetchval("SELECT title FROM circles_threads WHERE id = $1", tp["id"])
            check("close_turn in PII mode: a 'met' in the next turn closes the goal still in the queue",
                  (wm.get("goal") or {}).get("status") == "queued"
                  and g_state.get("send [PERSON_1] the invoices") == (True, True)
                  and g_state.get("draft the march report", (None,))[0] is False, f"{wm} {g_state}")
            check("close_turn in PII mode: the title keeps its own words; nothing raw in the thread row "
                  "or the tool log",
                  title == "[PERSON_1] invoices" and before_thread == 0
                  and logged and "Alice" not in logged and "chars>" in logged, f"{title} {logged}")
            del old_goal
            rows = {r["memory_type"]: r["content"] for r in await pool.fetch(
                "SELECT memory_type, content FROM entity_memory WHERE entity_id = $1 "
                "AND memory_type IN ('preference', 'goal') AND status = 'active' "
                "AND t_valid_until IS NULL", TEST_EID)}
            topic = await pool.fetchval("SELECT topic FROM circles_threads WHERE id = $1", tp["id"])
            raw = await pool.fetchval(
                "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND (content LIKE '%Alice%' "
                "OR metadata::text LIKE '%Alice%')", TEST_EID)
            check("close_turn in PII mode: queued, then only tokenized words land (preference, goal, topic)",
                  (wp.get("preferences") or [{}])[0].get("status") == "queued"
                  and (wp.get("goal") or {}).get("status") == "queued" and before == 0
                  and rows.get("preference") == "prefers [PERSON_1] to review the invoices"
                  and rows.get("goal") == "send [PERSON_1] the invoices"
                  and topic == "invoices for [PERSON_1]" and raw == 0, f"{wp} {rows} {topic} raw={raw}")
            await ct.delete_thread(pool, TEST_EID, str(tp["id"]))
        finally:
            _pc.sterilize = _saved_st
            for k, v in _saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        await _clean(pool)

        # A split moves provenance, never the decision.
        t = await ct.create_thread(pool, TEST_EID, title="scope gate split")
        tid = str(t["id"])
        desc = ct.descriptor_for(tid)
        turn_ids = []
        for k in range(2):
            turn_ids.append(await pool.fetchval(
                "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata) "
                "VALUES ($1, $2, 'turn', 'active', $3::jsonb) RETURNING id",
                TEST_EID, f"turn {k}", json.dumps({"source_context": desc, "app_id": "circles",
                                                   "role": "user"})))
        p = await _row(pool, "prefers metric units", "preference",
                       meta={**HOST, "source_context": desc, "from_turns": [turn_ids[1]]})
        try:
            await ct.split_thread(pool, TEST_EID, tid, from_turn_id=turn_ids[1], title="split off")
            md = json.loads(await pool.fetchval("SELECT metadata::text FROM entity_memory WHERE id = $1", p))
            check("split: the row moves with its turns and keeps the decision",
                  md.get("source_context") != desc and md.get("about") == {"kind": "global"}, f"{md}")
        except Exception as e:  # noqa: BLE001
            check("split: the row moves with its turns and keeps the decision", False, f"split raised {e!r}")
        await _clean(pool)
        await _clean(pool)

        # ── 2. the retired types. migrations/v235_one_memory.sql ran the boot
        # archive/retype pass one last time and narrowed the CHECK: a row of a
        # retired type is refused, and 'retired' keeps the old name.
        import asyncpg as _apg
        refused = []
        for gone in ("fact", "decision", "topic", "emotion", "unresolved", "knowledge",
                     "skill_memory", "moral_precedent", "legacy"):
            try:
                await _row(pool, f"a {gone} row written after v235", gone)
            except _apg.CheckViolationError:
                refused.append(gone)
        kept = await _row(pool, "a row v235 retired", "retired", meta={"prior_memory_type": "fact"})
        check("retired types: the CHECK refuses every retired type and takes a 'retired' row",
              len(refused) == 9 and kept is not None, f"refused={refused}")
        await _clean(pool)

        # ── 3. the audit is deterministic only: judged findings left open by an
        # earlier cut are closed, never shown, never written again.
        for kind, said in (("contradiction", "the invoice app ships weekly"),
                           ("unsupported_state", "dispatch on this host currently fails")):
            await pool.execute(
                "INSERT INTO memory_audit_findings (entity_id, scope_kind, scope_ref, kind, said, "
                "fingerprint) VALUES ($1, $2, $3, $4, $5, $6)",
                TEST_EID, A["kind"], A["ref"], kind, said, f"judged-{kind}")
        check("audit: judged findings are never shown", await ta.open_findings(pool, TEST_EID, A) == [])
        closed = await ta.close_judged(pool, TEST_EID)
        again = await ta.close_judged(pool, TEST_EID)
        check("audit: judged findings left open are closed as 'expired', once",
              closed == 2 and again == 0 and await pool.fetchval(
                  "SELECT COUNT(*) FROM memory_audit_findings WHERE entity_id=$1 "
                  "AND close_reason='expired'", TEST_EID) == 2, f"closed={closed} again={again}")

        # ── 4. the cross-thread scan
        blocks = [{"name": "memory_prefire", "items": ["x", "y", "z", "w"], "turn_scope": A,
                   "item_meta": [
            {"id": 1, "type": "turn", "scope": B, "pattern": False},
            {"id": 2, "type": "turn", "scope": A, "pattern": False},
            {"id": 3, "type": "preference", "scope": B, "pattern": False},
            {"id": 4, "type": "summary", "scope": None, "pattern": True},
        ]}]
        # The log LABEL differs from the recall scope on purpose for replay /
        # A-B; the scan must judge against turn_scope, not this column.
        replay = {"kind": "app", "ref": "circles-replay"}
        await pool.execute(
            "INSERT INTO prompt_log (entity_id, session_id, scope, block_count, total_chars, "
            "has_cache_break, blocks, source_context) "
            "VALUES ($1, 'mi-gate', 'per_turn', 1, 10, false, $2::jsonb, $3::jsonb)",
            TEST_EID, json.dumps(blocks), json.dumps(replay))
        n1 = await ta.audit_cross_thread(pool, TEST_EID)
        n2 = await ta.audit_cross_thread(pool, TEST_EID)
        ct = await pool.fetch(
            "SELECT said, scope_kind, scope_ref FROM memory_audit_findings "
            "WHERE entity_id=$1 AND kind='cross_thread'", TEST_EID)
        check("cross-thread: only another thread's own words, judged against the recall "
              "scope, once — a person pattern is never a finding",
              n1 == 1 and n2 == 0 and len(ct) == 1
              and ct[0]["said"] == "memory #1 (turn) from app:circles"
              and (ct[0]["scope_kind"], ct[0]["scope_ref"]) == (A["kind"], A["ref"]),
              f"n1={n1} n2={n2} {[dict(r) for r in ct]}")
        # A closed finding that happens again reopens (N8).
        await pool.execute("UPDATE memory_audit_findings SET closed_at = NOW(), "
                           "close_reason = 'expired' WHERE entity_id=$1 AND kind='cross_thread'",
                           TEST_EID)
        n3 = await ta.audit_cross_thread(pool, TEST_EID)
        check("audit: re-reading the SAME evidence never reopens a closed finding (R1)",
              n3 == 0 and await pool.fetchval(
                  "SELECT closed_at IS NOT NULL FROM memory_audit_findings "
                  "WHERE entity_id=$1 AND kind='cross_thread'", TEST_EID), f"n3={n3}")
        await pool.execute(
            "INSERT INTO prompt_log (entity_id, session_id, scope, block_count, total_chars, "
            "has_cache_break, blocks, source_context) "
            "VALUES ($1, 'mi-gate-2', 'per_turn', 1, 10, false, $2::jsonb, $3::jsonb)",
            TEST_EID, json.dumps(blocks), json.dumps(A))
        n4 = await ta.audit_cross_thread(pool, TEST_EID)
        check("audit: the leak HAPPENING AGAIN after the close reopens it (N8)",
              n4 == 1 and await pool.fetchval(
                  "SELECT closed_at IS NULL FROM memory_audit_findings "
                  "WHERE entity_id=$1 AND kind='cross_thread'", TEST_EID), f"n4={n4}")

        await pool.execute(
            "UPDATE memory_audit_findings SET last_seen_at = NOW() - interval '30 days' "
            "WHERE entity_id=$1 AND kind='cross_thread' AND closed_at IS NULL", TEST_EID)
        check("audit: a finding past its age is never shown, even before a pass closes it",
              await ta.open_findings(pool, TEST_EID, A) == [])
        check("audit: a cross-thread finding not seen again ages out as 'expired'",
              await ta.expire_findings(pool, TEST_EID) == 1)
        # With the consolidator off the audit runs on its own clock: one pass
        # finds the leak again (a fresh prompt) and expires nothing current.
        await pool.execute(
            "UPDATE memory_audit_findings SET closed_at = NULL, close_reason = NULL, "
            "last_seen_at = NOW() - interval '30 days' WHERE entity_id=$1 AND kind='cross_thread'", TEST_EID)
        res = await ta.audit_pass(pool)
        fresh = await pool.fetchval(
            "SELECT closed_at IS NULL AND last_seen_at > NOW() - interval '1 day' "
            "FROM memory_audit_findings WHERE entity_id=$1 AND kind='cross_thread'", TEST_EID)
        check("audit pass (consolidator off): a leak still in recent prompts is seen again, kept open",
              res["entities"] >= 1 and fresh, f"{res}")
        await pool.execute("UPDATE prompt_log SET created_at = NOW() - interval '2 days' "
                           "WHERE entity_id = $1 AND scope = 'per_turn'", TEST_EID)
        await pool.execute("UPDATE memory_audit_findings SET last_seen_at = NOW() - interval '30 days' "
                           "WHERE entity_id=$1 AND kind='cross_thread'", TEST_EID)
        res = await ta.audit_pass(pool)
        check("audit pass (consolidator off): one not seen again ages out",
              res["expired"] >= 1 and await pool.fetchval(
                  "SELECT close_reason FROM memory_audit_findings "
                  "WHERE entity_id=$1 AND kind='cross_thread'", TEST_EID) == "expired", f"{res}")
        await pool.execute("UPDATE prompt_log SET created_at = NOW() "
                           "WHERE entity_id = $1 AND scope = 'per_turn'", TEST_EID)
        await pool.execute(
            "UPDATE memory_audit_findings SET closed_at = NULL, close_reason = NULL, "
            "last_seen_at = NOW() WHERE entity_id=$1 AND kind='cross_thread'", TEST_EID)

        # ── 5. the thread's memory block leads with its findings
        block = await eng.build_memory_context(
            TEST_EID, {}, max_tokens=2000, source_context=A)
        check("memory block: the thread's open findings are shown, the leak NAMED not quoted",
              "### Audit of this thread" in (block or "")
              and "memory #1 (turn) from app:circles" in block, (block or "")[:400])
        block_b = await eng.build_memory_context(
            TEST_EID, {}, max_tokens=2000, source_context=B)
        check("memory block: another thread does not see them",
              "### Audit of this thread" not in (block_b or ""), (block_b or "")[:300])

        # ── 6. "Things You Were Taught" carries live rows only. The per-turn
        # call passes no conversation_context, so the top-by-importance branch
        # runs on every turn; it had no status filter, and one Host's archived
        # "dispatch currently fails" rule stayed in its prompt.
        stale = await _row(pool, "Dispatch on this host currently fails with "
                           "AgentDispatch._run_and_record() got an unexpected keyword argument "
                           "'grant'", "preference", meta={"rule_kind": "learned"}, importance=3.0)
        replaced = await _row(pool, "dispatch rule someone replaced", "preference",
                              meta={"rule_kind": "taught"}, importance=3.0)
        live = await _row(pool, "report a dispatch only after reading its terminal status",
                          "preference", meta={"rule_kind": "taught"}, importance=2.5)
        await pool.execute("UPDATE entity_memory SET status = 'archived', status_reason = "
                           "'memory cleanup' WHERE id = $1", stale)
        await pool.execute("UPDATE entity_memory SET t_valid_until = NOW() - interval '1 minute' "
                           "WHERE id = $1", replaced)
        per_turn = await eng.build_memory_context(TEST_EID, {}, max_tokens=2000) or ""
        standalone = await eng._build_teaching_block(TEST_EID, "", 1600) or ""
        keyword = " ".join(t["content"] for t in await eng.retrieve_teaching(
            TEST_EID, "dispatch rule replaced terminal status", limit=5))
        for label, text in (("the per-turn memory block", per_turn),
                            ("the standalone teaching block", standalone),
                            ("keyword teaching retrieval", keyword)):
            check(f"taught ({label}): an archived or superseded rule is never shown; a live one is",
                  "currently fails" not in text and "someone replaced" not in text
                  and "only after reading its terminal status" in text, text[:500])
        del live
        # The constitution block is the platform's five values, from code
        # (constitution/platform_values.py, v236): a constitutional row in the
        # database, live or archived, never reaches it. A Host's own rule is a
        # preference rule now, and the checks above hold its archive and
        # correction rules.
        await _row(pool, "never mention the old pricing page", "constitutional", importance=3.0)
        from mastercode.constitution.platform_values import PLATFORM_VALUES, render
        block = render()
        check("constitution: the block is the platform's values from code, never a database row",
              "old pricing page" not in block and all(t in block for t, _ in PLATFORM_VALUES),
              block[:300])
        await _clean(pool)
        await _rules_are_preferences(pool, eng)
        await _clean(pool)
        await _rule_reach_paths(pool, eng)
    finally:
        await _clean(pool)
        await pool.close()

    if FAILURES:
        print(f"FAIL {len(FAILURES)}: " + "; ".join(FAILURES))
        return 1
    print("OK memory integrity on a real database (thread set, retired types, audit writes, "
          "cross-thread scan, findings in the thread's block)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
