#!/usr/bin/env python3
"""Gate: words a person SAVES are kept as the turn keeps a message
(server/secret_cut.py): every hard secret shape is cut to its
[REDACTED:<id>] marker before a row keeps the words and before anything embeds
them, and soft shapes and plain words are kept exactly as typed.
runtime/session.send_message cuts a message the same way, and the doors that
read words before the turn cut them too
(scripts/test_secret_cut_doors_pg.py). Uploaded files and documents stay
whole.

Each saving route runs for real against the kernel's schema. Only the Host's
embedding endpoint is a fake, and it records what it is handed:
memory_engine._embed_text_config_driven, the dialer every text embed of the
Host's goes through, with _entity_embedding_config saying a model is set up.
A made-up key in a shape brain/mastercode/secret_shapes.json knows is planted
in what is saved.

  E  a memory edit (POST /api/brain/manage/memories/{id}/edit, the Memory
     app's Correct): the new version keeps the marker and never the key, its
     embedding call is handed the cut words, and re-typing a key into a
     memory that already holds it cut changes nothing.
  P  a project's name and description (PUT /api/projects/{id}): kept cut, and
     the project search's refresh, which embeds both, is never handed a key.
  T  a learned rule the person rewrites in Training Memory's review
     (PATCH /api/training-memory/jobs/{id}/review/{pair_id}): kept cut, and
     the commit embeds and stores the cut rule.
  C  a Circles thread's title, typed when the thread is started
     (POST /api/circles/threads), renamed (PATCH /api/circles/threads/{id})
     or given to a split (POST /api/circles/threads/{id}/split): the thread
     row keeps the marker and never the key, and so does the marker row the
     split leaves in entity_memory, in its words and its metadata.
  W  words with no hard shape (plain words, a soft assignment, a
     random-looking token, a hash) are kept and embedded exactly as typed at
     each of those routes, and an update that sends no name or description
     leaves both as they were.
  U  an upload stays whole: a text file dropped on Documents
     (POST /api/brain/context/upload) and a document staged by import keep
     the key, and so does a document promoted into memory
     (POST /api/brain/context/{id}/promote), whose content is the document's.

Run against another tree (APP_ROOT=<that tree>) it reports which checks that
tree fails. On main before this change every "planted key" check fails; the W
and U checks pass on both trees, since they hold what this change must leave
alone.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker.

  PYTHONPATH=brain python3 scripts/test_secret_cut_saved_pg.py
"""
import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate runs the saving routes against a real database.")
    sys.exit(2)

FAILED = 0
KEY_FAILED = 0
E = 7

# Made-up keys in shapes secret_shapes.json knows (hard), the run of each that
# must never be kept or embedded, and the marker each is cut to.
ANTHROPIC = "sk-ant-api03-" + "Wv5" * 20
GITHUB = "ghp_" + "Qz7" * 12
BODIES = {ANTHROPIC: "Wv5Wv5Wv5", GITHUB: "Qz7Qz7Qz7"}
MARKS = {ANTHROPIC: "[REDACTED:anthropic-key]", GITHUB: "[REDACTED:github-token]"}
# Words with no hard shape: a soft assignment, a random-looking token (soft),
# a hash (nothing).
PLAIN = "api_key: hunter2hunter2xyz Zx8Qp2Lm9Rt4Vw7Ny3Kb6Hc1Jd5Fg0Sa 4b24b79b8a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e"
ROLE = "gate-saved-words"


def check(label, cond, detail=""):
    global FAILED, KEY_FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1
        if "planted key" in label:
            KEY_FAILED += 1


def load(name):
    """A module of the tree under test, or the exception that stopped it."""
    try:
        return __import__(name, fromlist=["_"])
    except Exception as e:  # noqa: BLE001
        return e


def keyless(text) -> bool:
    return not any(b in str(text or "") for b in BODIES.values())


# ── the endpoint: a fake that records what it is handed ────────────────────

SENT: list = []        # every text the Host's embedding endpoint was handed, in order


def vec_of(text: str) -> list:
    """A 1024-d vector (the schema's base column) that depends on the text."""
    h = hashlib.sha256(text.encode()).digest()
    return [(h[i % 32] + i) % 251 / 251.0 + 0.001 for i in range(1024)]


async def fake_embed_config(pool, entity_id):
    return "fake", "fake-embed"


async def fake_embed_text(text, model, **kw):
    SENT.append(str(text))
    return vec_of(str(text))


def mark() -> int:
    return len(SENT)


def embed_check(label, since: int, word: str):
    """The endpoint was handed the saved words (`word` reached it) and never a
    planted key."""
    got = [t for t in SENT[since:] if word in t]
    check(f"{label}: planted key — the embedding endpoint was handed the words and never a key",
          bool(got) and all(keyless(t) for t in got),
          f"handed {len(got)} text(s) with {word!r}: {[t[:160] for t in got][:3]}")


async def wait_for(cond, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return bool(cond())


def request(body=None):
    async def _json():
        return body or {}
    return types.SimpleNamespace(state=types.SimpleNamespace(app_token=None), json=_json,
                                 headers={}, query_params={})


def upload(raw: bytes, content_type: str = "text/plain"):
    """A request carrying a dropped file's bytes, as the Documents drop sends it."""
    async def _stream():
        yield raw
    return types.SimpleNamespace(state=types.SimpleNamespace(app_token=None), stream=_stream,
                                 headers={"content-type": content_type, "content-length": str(len(raw))},
                                 query_params={})


def install(patches) -> list:
    saved = []
    for mod, attr, val in patches:
        saved.append((mod, attr, getattr(mod, attr)))
        setattr(mod, attr, val)
    return saved


def restore(saved) -> None:
    for mod, attr, val in reversed(saved):
        setattr(mod, attr, val)


async def memory_row(pool, content: str) -> int:
    return await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status)"
        " VALUES ($1, $2, 'cited_fact', 1.0, 'active') RETURNING id", E, content)


async def edited(pool, out) -> dict | None:
    if not isinstance(out, dict) or not out.get("new_id"):
        return None
    row = await pool.fetchrow("SELECT content, embedding IS NOT NULL AS embedded FROM entity_memory"
                              " WHERE id = $1", out["new_id"])
    return dict(row) if row else None


def review_job(jobs: pathlib.Path, name: str, pairs: dict) -> pathlib.Path:
    """A Training Memory job whose classify phase has finished: classified.jsonl
    and the review state it builds (worker.build_review_state's entry shape)."""
    jd = jobs / name
    jd.mkdir()
    state, lines = {}, []
    for pid, (rule, context) in pairs.items():
        lines.append(json.dumps({"pair_id": pid, "session_id": "gate-session", "kind": "correction",
                                 "rejected_preview": context, "correction_text": "no, the other way",
                                 "classifier": {"is_feedback": True, "type": "correction",
                                                "confidence": 0.9, "rule": rule}}))
        state[pid] = {"pair_id": pid, "session_id": "gate-session", "kind": "correction",
                      "classifier_is_feedback": True, "classifier_type": "correction",
                      "classifier_confidence": 0.9, "rule_original": rule, "rule_final": rule,
                      "role": ROLE, "severity": "gentle", "status": "pending", "would_escalate_id": None}
    (jd / "classified.jsonl").write_text("\n".join(lines) + "\n")
    (jd / "review_state.json").write_text(json.dumps(state))
    return jd


# ── the routes ──────────────────────────────────────────────────────────────

async def run(dsn):
    from server import brain_service as bs
    from mastercode import memory_engine
    BR = load("server.routes.brain")
    P = load("server.routes.projects")
    TM = load("server.routes.training_memory")
    TW = load("brain.training_memory.worker")
    CTX = load("server.routes.context")
    # The circles routes import server.main, which imports them back for their
    # router: server.main loads first, so it finds the module whole.
    load("server.main")
    CR = load("server.routes.circles")
    for name, mod in (("brain", BR), ("projects", P), ("training_memory", TM),
                      ("training_memory.worker", TW), ("context", CTX), ("circles", CR)):
        check(f"the tree's {name} module loads", not isinstance(mod, Exception), mod)
    if any(isinstance(m, Exception) for m in (BR, P, TM, TW, CTX, CR)):
        return

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    jobs = pathlib.Path(tempfile.mkdtemp(prefix="gate-saved-words-"))
    saved = install([
        (bs, "_pool", pool), (bs, "_entity_id", E),
        (memory_engine, "_entity_embedding_config", fake_embed_config),
        (memory_engine, "_embed_text_config_driven", fake_embed_text),
        (TM, "JOBS_ROOT", jobs),
    ])
    try:
        # ── E: a memory edit ────────────────────────────────────────────────
        print("-- E: a memory edit (POST /api/brain/manage/memories/{id}/edit)")
        mid = await memory_row(pool, "quokka-e1 the deploy key lives in the vault")
        m = mark()
        out = await BR.edit_single_memory(mid, request({"content": f"quokka-e1 the deploy key is {ANTHROPIC} now"}))
        row = await edited(pool, out)
        check("E the edit wrote an embedded new version (the checks below are not vacuous)",
              row is not None and row["embedded"] and "quokka-e1" in row["content"], (out, row))
        check("E planted key — the new version keeps the marker and never the key",
              row is not None and keyless(row["content"])
              and row["content"] == f"quokka-e1 the deploy key is {MARKS[ANTHROPIC]} now", row)
        embed_check("E the new version's embedding call", m, "quokka-e1")
        cut = await memory_row(pool, f"quokka-e2 the token is {MARKS[GITHUB]}")
        out = await BR.edit_single_memory(cut, request({"content": f"quokka-e2 the token is {GITHUB}"}))
        latest = await pool.fetchval(
            "SELECT content FROM entity_memory WHERE entity_id = $1 AND content LIKE 'quokka-e2%'"
            " AND t_valid_until IS NULL ORDER BY id DESC LIMIT 1", E)
        check("E planted key — re-typing the key into a memory that holds it cut changes nothing",
              isinstance(out, dict) and out.get("unchanged") is True and keyless(latest), (out, latest))

        # ── P: a project's name and description ─────────────────────────────
        print("-- P: a project's name and description (PUT /api/projects/{id})")
        await wait_for(lambda: E not in P._EMBED_REFRESH_INFLIGHT)
        pid = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description) VALUES ($1, 'Quokka tracker',"
            " 'count the quokkas') RETURNING id", E)
        out = await P.update_project(pid, P.UpdateProject(
            name=f"quokka-p1 rotate {ANTHROPIC}",
            description=f"quokka-p1-desc ship with {GITHUB}, then rotate it"))
        row = await pool.fetchrow("SELECT name, description FROM projects WHERE id = $1", pid)
        check("P the update renamed the project (the checks below are not vacuous)",
              out == {"updated": True} and row is not None and "quokka-p1-desc" in (row["description"] or ""),
              (out, dict(row) if row else None))
        check("P planted key — the name and the description keep the markers and never a key",
              row is not None and row["name"] == f"quokka-p1 rotate {MARKS[ANTHROPIC]}"
              and row["description"] == f"quokka-p1-desc ship with {MARKS[GITHUB]}, then rotate it",
              dict(row) if row else None)
        m = mark()
        await P.search_projects(P.SearchProjects(q="quokka-p1", limit=8))
        await wait_for(lambda: any("quokka-p1-desc" in t for t in SENT[m:])
                       and E not in P._EMBED_REFRESH_INFLIGHT)
        embed_check("P the project search's refresh, which embeds the name and description", m,
                    "quokka-p1-desc")

        # ── T: a learned rule rewritten in Training Memory's review ─────────
        print("-- T: a rule rewritten in Training Memory's review (PATCH .../review/{pair_id})")
        jd = review_job(jobs, "gate-job", {
            "p1": ("Keep deploy keys in the vault", "quokka-t1ctx the earlier reply"),
            "p2": ("Say which file changed", "quokka-t2ctx the earlier reply")})
        entry = await TM.patch_review("gate-job", "p1", TM.ReviewPatch(
            rule_final=f"quokka-t1rule deploy with {ANTHROPIC}, never paste it"))
        kept = json.loads((jd / "review_state.json").read_text()).get("p1", {})
        check("T the review kept the rewritten rule (the checks below are not vacuous)",
              "quokka-t1rule" in str(kept.get("rule_final")) and kept.get("status") == "edited", kept)
        check("T planted key — the review keeps the rule with the marker and never the key",
              entry.get("rule_final") == kept.get("rule_final")
              == f"quokka-t1rule deploy with {MARKS[ANTHROPIC]}, never paste it", (entry, kept))
        m = mark()
        last = None
        async for ev in TW.run_commit_phase(jd, {"entity_id": E, "role": ROLE}):
            last = ev
        rules = [r for (r,) in await pool.fetch(
            "SELECT correction FROM behavioral_corrections WHERE entity_id = $1 AND role = $2", E, ROLE)]
        check("T the commit stored the rewritten rule (the checks below are not vacuous)",
              any("quokka-t1rule" in r for r in rules), (rules, last))
        check("T planted key — the stored rule keeps the marker and never the key",
              any("quokka-t1rule" in r for r in rules) and all(keyless(r) for r in rules), rules)
        embed_check("T the commit's embedding call", m, "quokka-t1rule")

        # ── C: a Circles thread's title ─────────────────────────────────────
        print("-- C: a Circles thread's title (POST /threads, PATCH /threads/{id}, POST .../split)")
        from mastercode import circles_threads as ct
        from mastercode.memory_engine import write_turn_row

        async def thread_title(tid):
            return await pool.fetchval("SELECT title FROM circles_threads WHERE id = $1::uuid", str(tid))

        async def split_marker(new_id):
            row = await pool.fetchrow(
                "SELECT content, metadata->'marker'->>'thread_title' AS title FROM entity_memory"
                " WHERE entity_id = $1 AND source_session = $2", E, f"split:{new_id}")
            return dict(row) if row else None

        async def turns(tid, label, n=3):
            return [await write_turn_row(pool, E, "gate-saved-words", "user" if i % 2 == 0 else "assistant",
                                         f"{label} {i}", tags={"app_id": "circles",
                                                               "source_context": ct.descriptor_for(tid)})
                    for i in range(n)]

        out = await CR.create_circles_thread(request({"title": f"quokka-c1 {ANTHROPIC}"}))
        c1 = (out or {}).get("thread", {}).get("id")
        kept = await thread_title(c1) if c1 else None
        check("C planted key — a new thread keeps its title with the marker and never the key",
              kept == f"quokka-c1 {MARKS[ANTHROPIC]}", (out, kept))
        out = await CR.patch_circles_thread(str(c1), request({"title": f"quokka-c2 {GITHUB}"}))
        kept = await thread_title(c1)
        check("C planted key — a renamed thread keeps its title with the marker and never the key",
              kept == f"quokka-c2 {MARKS[GITHUB]}", (out, kept))
        ids = await turns(c1, "quokka-c3turn")
        out = await CR.split_circles_thread(str(c1), request({"from_turn_id": ids[1],
                                                               "title": f"quokka-c3 {ANTHROPIC}"}))
        c3 = (out or {}).get("thread", {}).get("id")
        kept = await thread_title(c3) if c3 else None
        marker = await split_marker(c3) if c3 else None
        check("C the split moved turns and left its marker row (the checks below are not vacuous)",
              marker is not None and "quokka-c3" in (marker["title"] or ""), (out, marker))
        check("C planted key — a split's new thread keeps its title with the marker and never the key",
              kept == f"quokka-c3 {MARKS[ANTHROPIC]}", (out, kept))
        check("C planted key — the split's marker row keeps the title with the marker and never the key",
              marker is not None and keyless(marker["content"]) and keyless(marker["title"])
              and f"quokka-c3 {MARKS[ANTHROPIC]}" in marker["content"]
              and marker["title"] == f"quokka-c3 {MARKS[ANTHROPIC]}", marker)

        # ── W: words with no hard shape are kept and embedded as typed ──────
        print("-- W: words with no hard shape are kept and embedded exactly as typed")
        mid = await memory_row(pool, "quokka-w1 an older note")
        typed = f"quokka-w1 {PLAIN}"
        m = mark()
        out = await BR.edit_single_memory(mid, request({"content": typed}))
        row = await edited(pool, out)
        check("W a memory edit keeps the words exactly as typed",
              row is not None and row["content"] == typed, (out, row))
        check("W a memory edit's embedding call is handed the words exactly as typed",
              typed in SENT[m:], [t[:120] for t in SENT[m:]])
        name, desc = f"quokka-w2 {PLAIN}", f"quokka-w2-desc {PLAIN}"
        await P.update_project(pid, P.UpdateProject(name=name, description=desc))
        row = await pool.fetchrow("SELECT name, description FROM projects WHERE id = $1", pid)
        check("W a project's name and description are kept exactly as typed",
              row is not None and (row["name"], row["description"]) == (name, desc),
              dict(row) if row else None)
        await P.update_project(pid, P.UpdateProject(status="active"))
        again = await pool.fetchrow("SELECT name, description, status FROM projects WHERE id = $1", pid)
        check("W an update that sends no name or description leaves both as they were",
              again is not None and (again["name"], again["description"], again["status"])
              == (name, desc, "active"), dict(again) if again else None)
        entry = await TM.patch_review("gate-job", "p2", TM.ReviewPatch(rule_final=f"quokka-t2rule {PLAIN}"))
        check("W a rule rewritten in the review is kept exactly as typed",
              entry.get("rule_final") == f"quokka-t2rule {PLAIN}", entry)
        out = await CR.create_circles_thread(request({"title": f"quokka-w3 {PLAIN}"}))
        w3 = (out or {}).get("thread", {}).get("id")
        check("W a new thread's title is kept exactly as typed",
              w3 is not None and await thread_title(w3) == f"quokka-w3 {PLAIN}", out)
        await CR.patch_circles_thread(str(w3), request({"title": f"quokka-w4 {PLAIN}"}))
        check("W a renamed thread's title is kept exactly as typed",
              await thread_title(w3) == f"quokka-w4 {PLAIN}", await thread_title(w3))
        await CR.patch_circles_thread(str(w3), request({"closed": True}))
        check("W closing a thread without a title leaves its title as it was",
              await thread_title(w3) == f"quokka-w4 {PLAIN}", await thread_title(w3))
        ids = await turns(w3, "quokka-w5turn")
        out = await CR.split_circles_thread(str(w3), request({"from_turn_id": ids[1],
                                                               "title": f"quokka-w5 {PLAIN}"}))
        w5 = (out or {}).get("thread", {}).get("id")
        marker = await split_marker(w5) if w5 else None
        check("W a split's title is kept exactly as typed, in the thread and in its marker row",
              w5 is not None and await thread_title(w5) == f"quokka-w5 {PLAIN}"
              and marker is not None and marker["title"] == f"quokka-w5 {PLAIN}", (out, marker))

        # ── U: an upload stays whole ────────────────────────────────────────
        print("-- U: an upload stays whole (POST /api/brain/context/upload, /import, /{id}/promote)")
        raw = f"quokka-u1 deploy notes\nthe key is {ANTHROPIC}\nrotate monthly\n".encode()
        out = await CTX.upload_context(upload(raw), x_filename="quokka-u1-notes.txt", x_tags=None)
        texts = [c for (c,) in await pool.fetch(
            "SELECT content FROM user_context WHERE entity_id = $1 AND metadata->>'from_file' = $2",
            E, "quokka-u1-notes.txt")]
        check("U a text file dropped on Documents keeps the key as it was written",
              any(ANTHROPIC in c for c in texts), (out, [c[:80] for c in texts]))
        out = await CTX.import_context(request({"source": "manual", "items": [
            {"content": f"quokka-u2 notes {GITHUB} for the build", "title": "quokka-u2"}]}))
        doc = await pool.fetchrow("SELECT id, content FROM user_context WHERE entity_id = $1"
                                  " AND title = 'quokka-u2'", E)
        check("U a document staged by import keeps the key as it was written",
              doc is not None and GITHUB in doc["content"], (out, dict(doc) if doc else None))
        if doc is not None:
            out = await CTX.promote_context(doc["id"], request({"memory_type": "cited_fact"}))
            mem = await pool.fetchval("SELECT content FROM entity_memory WHERE id = $1",
                                      (out or {}).get("memory_id"))
            check("U a document promoted into memory keeps the document's words whole",
                  mem is not None and GITHUB in mem, (out, mem))
    finally:
        restore(saved)
        await pool.close()
        shutil.rmtree(jobs, ignore_errors=True)


def main():
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv, \
            kernel_schema_db(srv, entity_id=E) as dsn:
        asyncio.run(run(dsn))
    print(f"planted-key checks failed: {KEY_FAILED}")
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
