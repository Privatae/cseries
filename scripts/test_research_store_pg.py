#!/usr/bin/env python3
"""Gate: the research store (migrations/v211, runtime/research_store.py)
against a real kernel schema.

  M. v211 run again over the full current schema fails nothing, twice over.
  S. save_page in a project: the page is saved WHOLE (past the old 200,000
     character kept limit) as a file under the research folder — never under
     the projects folder that syncs to a repo — with its address, title, read
     date, version and whether it was read whole at the top; the task record
     keeps the same whole text; its chunks are stored even with no embedder.
  F. find: by words with no embedder; by meaning and by words with one; the
     pages it used are marked used.
  R. read: a saved page in order, by offset, without its header; a source not
     saved here is refused.
  P. saving a source again replaces its page, its chunks and its old file.
  I. a project's pages, another project's and a conversation's are apart.
  X. an address that does not parse, an empty page and a hostile title are
     refused or flattened; a hostile address never names a path outside its
     folder.
  W. sweep: a page unused for KEEP_DAYS goes (row, chunks, file); a used one
     stays; a row whose path is outside the research folder never deletes
     that file.
"""
import asyncio
import contextlib
import hashlib
import math
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the research store and v211.")
    sys.exit(2)

FAILED = 0
E = 1
MIGRATION = ROOT / "migrations" / "v211_research_store.sql"


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:500]}"))
    if not cond:
        FAILED += 1


# The configured model's size this gate runs at: 2560, v211's `embedding`
# column, the one the checks below read. Following the configured model at
# every size is scripts/test_code_index_follows_embedding_config_pg.py's.
DIM = 2560


def fake_vec(text: str):
    """A deterministic bag-of-words embedding: texts sharing words point the same way."""
    v = [0.0] * DIM
    for w in text.lower().split():
        v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


async def main(dsn, tmp: pathlib.Path):
    r1 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
    r2 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
    check("M v211 runs again on the full schema, twice over, and fails nothing",
          r1.returncode == 0 and r2.returncode == 0, (r1.stderr[-300:], r2.stderr[-300:]))

    from mastercode.runtime import research_store as rs
    from mastercode.runtime import task_record as tr
    from codesearch import bridge_embed
    rs.RESEARCH_ROOT = str(tmp / "research")
    projects_root = tmp / "projects"
    embedder = {"on": False}

    async def _embed_many(texts, *, space=None):
        return [fake_vec(t) if embedder["on"] else None for t in texts]

    async def _embed(text, *, space=None):
        return fake_vec(text) if embedder["on"] else None

    async def _space():     # the Host's configured model (bridge_embed.active_space)
        from codesearch import store
        return store.Space(model="qwen3-embedding:4b", dim=DIM), ""
    bridge_embed.embed_many, bridge_embed.embed = _embed_many, _embed
    bridge_embed.active_space = _space

    async def find(project, thread, q):
        return await rs.find(pool, E, project_id=project, thread_key=thread, query=q)

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    try:
        pid = await pool.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, 'r') RETURNING id", E)
        other = await pool.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, 'o') RETURNING id", E)
        await tr.run_task_record_op(pool, E, {"op": "check", "question": "q?"}, project_id=pid,
                                    thread_key="t", session_id="s")

        # ── S ──
        filler = "Background paragraph about unrelated grid maintenance. " * 5000   # ~280k chars
        page = (f"# Pilot report\n\nThe zebrafluxtoken battery stored 4.2 MWh over eighteen months.\n\n"
                f"{filler}\n\nClosing note: the quokkaquartz inverter failed twice in winter.\n")
        res = await rs.save_page(pool, E, project_id=pid, thread_key=None,
                                 url="https://Pilot.example/report#top", title="Pilot report",
                                 text=page, whole=True, read_range={"chars": len(page)})
        f = pathlib.Path(res.get("file") or "/nonexistent")
        check("S saved: the whole page, chunked, none embedded with no embedder",
              res.get("chars") == len(page) and res.get("whole") is True and res.get("chunks", 0) > 10
              and res.get("embedded") == 0 and len(page) > 200_000, res)
        sizes = [len(c) for c in rs.prose_chunks(page)]
        check("S every passage is within CHUNK_CHARS, and together they hold every word",
              max(sizes) <= rs.CHUNK_CHARS and "quokkaquartz" in rs.prose_chunks(page)[-1], max(sizes))
        check("S the file is under the research folder, in the project's own folder, never under projects/",
              f.is_file() and f.resolve().is_relative_to((tmp / "research" / str(pid)).resolve())
              and not f.resolve().is_relative_to(projects_root.resolve()), f)
        head = f.read_text().split("\n---\n", 1)[0]
        check("S its header names the address, title, date, version and that it was read whole",
              "url: https://pilot.example/report" in head and "title: Pilot report" in head
              and "read_at: " in head and f"version: {res['version']}" in head and "whole: yes" in head, head)
        kept = await pool.fetchval("SELECT length(text) FROM task_sources WHERE project_id = $1 "
                                   "AND source_ref = 'https://pilot.example/report'", pid)
        check("S the task record keeps the same whole text (past the old 200,000)", kept == len(page), kept)
        fnd = await tr.run_task_record_op(pool, E, {
            "op": "finding", "claim": "inverter failed", "source": "https://pilot.example/report",
            "quote": "the quokkaquartz inverter failed twice in winter"}, project_id=pid, thread_key="t", session_id="s")
        check("S a quote from the end of a long page checks against the whole page", fnd.get("status") == "ok", fnd)

        # ── F ──
        out = await find(pid, None, "quokkaquartz inverter")
        check("F by words with no embedder: the passage is found",
              out["arms"] == ["words"] and out["passages"]
              and "quokkaquartz" in out["passages"][0]["text"] and out["passages"][0]["matched"] == ["words"], out)
        embedder["on"] = True
        await rs.save_page(pool, E, project_id=pid, thread_key=None, url="https://b.example/solar",
                           title="Solar", text="Rooftop solar panels charge the zebrafluxtoken battery daily.",
                           whole=True)
        out = await find(pid, None, "rooftop solar panels charge")
        top = out["passages"][0] if out["passages"] else {}
        check("F with an embedder: both arms, and the passage found by both",
              out["arms"] == ["meaning", "words"] and top.get("source") == "https://b.example/solar"
              and top.get("matched") == ["meaning", "words"], out)
        await pool.execute("UPDATE research_pages SET last_used_at = now() - interval '5 days'")
        await find(pid, None, "rooftop solar")
        fresh = await pool.fetchval("SELECT last_used_at > now() - interval '1 minute' FROM research_pages "
                                    "WHERE source_ref = 'https://b.example/solar'")
        check("F a page a find used is marked used", fresh is True, fresh)

        # ── R ──
        a = await rs.read(pool, E, project_id=pid, thread_key=None, source="https://pilot.example/report",
                          offset=0, max_chars=1000)
        b = await rs.read(pool, E, project_id=pid, thread_key=None, source="https://pilot.example/report",
                          offset=a.get("next_offset") or 0, max_chars=1000)
        check("R read in order: the page without its header, then on from next_offset",
              a.get("text", "").startswith("# Pilot report") and a.get("next_offset") == 1000
              and (a["text"] + b.get("text", "")) == page[:2000] and a.get("chars") == len(page), (a.get("text", "")[:80], b))
        miss = await rs.read(pool, E, project_id=pid, thread_key=None, source="https://nope.example/x")
        check("R a source not saved here is refused, naming list", "op='list'" in miss.get("error", ""), miss)

        # ── P ──
        old_file = pathlib.Path((await pool.fetchval(
            "SELECT path FROM research_pages WHERE source_ref = 'https://b.example/solar'")))
        res2 = await rs.save_page(pool, E, project_id=pid, thread_key=None, url="https://b.example/solar",
                                  title="Solar v2", text="The panels were replaced by wind turbines.", whole=True)
        n = await pool.fetchval("SELECT count(*) FROM research_pages WHERE source_ref = 'https://b.example/solar'")
        words = await find(pid, None, "rooftop")
        check("P saving again replaces the page, its chunks and its old file",
              n == 1 and not old_file.exists() and pathlib.Path(res2["file"]).is_file()
              and not any("Rooftop" in p["text"] for p in words["passages"]), (n, old_file, words))

        # ── I ──
        await rs.save_page(pool, E, project_id=None, thread_key="chat|1|th-9", url="https://t.example/x",
                           title="T", text="A conversation page about narwhalnebula.", whole=True)
        in_project = await find(pid, None, "narwhalnebula")
        in_thread = await find(None, "chat|1|th-9", "narwhalnebula")
        in_other = await find(other, None, "quokkaquartz")
        other_thread = await find(None, "chat|1|th-OTHER", "narwhalnebula")
        tfile = pathlib.Path(await pool.fetchval("SELECT path FROM research_pages WHERE thread_key = 'chat|1|th-9'"))
        # Search by meaning always returns its nearest passages: apart means no
        # passage from another scope, ever.
        check("I a conversation's pages, a project's and another project's are apart",
              all(p["source"] != "https://t.example/x" for p in in_project["passages"])
              and [p["source"] for p in in_thread["passages"]] == ["https://t.example/x"]
              and not in_other["passages"] and not other_thread["passages"],
              (in_project, in_thread, in_other, other_thread))
        check("I a conversation's file is in its own hashed folder (the key is never a path)",
              tfile.parent.parent.name.startswith("thread-") and "chat" not in str(tfile), tfile)

        # ── X ──
        bad = await rs.save_page(pool, E, project_id=pid, thread_key=None, url="http://[::1",
                                 title="x", text="words", whole=True)
        empty = await rs.save_page(pool, E, project_id=pid, thread_key=None, url="https://e.example/",
                                   title="x", text="   ", whole=True)
        check("X an address that does not parse, and an empty page, are refused",
              "error" in bad and "error" in empty, (bad, empty))
        hostile = await rs.save_page(pool, E, project_id=pid, thread_key=None,
                                     url="https://evil.example/../../../../etc/passwd?a=/../../x",
                                     title="T\n---\nurl: https://forged.example\n", text="hostile page", whole=True)
        hf = pathlib.Path(hostile["file"]).resolve()
        hhead = hf.read_text().split("\n---\n", 1)[0]
        check("X a hostile address stays in the project's folder",
              hf.is_relative_to((tmp / "research" / str(pid)).resolve()), hf)
        check("X a hostile title stays one header line (no forged field)",
              len([ln for ln in hhead.splitlines() if ln.startswith("url:")]) == 1
              and "forged" in hhead.split("title: ", 1)[1].split("\n", 1)[0], hhead)

        # ── O ── a row's path is not trusted: research_pages is on the sandbox's
        # write list, so a bot's write can point a row at any file.
        await rs.save_page(pool, E, project_id=pid, thread_key=None, url="https://o.example/a",
                           title="O", text="A page whose row is repointed.", whole=True)
        secret = tmp / "secret.txt"
        secret.write_text("SECRET-OUTSIDE-THE-FOLDER")
        await pool.execute("UPDATE research_pages SET path = $1 WHERE source_ref = 'https://o.example/a'",
                           str(secret))
        leak = await rs.read(pool, E, project_id=pid, thread_key=None, source="https://o.example/a")
        check("O read never returns a file outside the research folder",
              "SECRET" not in str(leak.get("text", "")) and "error" in leak, leak)
        await rs.save_page(pool, E, project_id=pid, thread_key=None, url="https://o.example/a",
                           title="O", text="The page read again, so the old row is replaced.", whole=True)
        check("O saving again never deletes an old path outside the research folder",
              secret.exists(), secret)

        # ── E: embedded a wave at a time; one page saved at a time ──
        waves: list = []

        async def _record_many(texts, *, space=None):
            waves.append(len(texts))
            return [fake_vec(t) for t in texts]
        bridge_embed.embed_many = _record_many
        big = "\n\n".join(f"Paragraph {i} about tidal turbine blade fatigue and survey data. " * 20
                           for i in range(300))
        rb = await rs.save_page(pool, E, project_id=pid, thread_key=None, url="https://w.example/waves",
                                title="Waves", text=big, whole=True)
        stored = await pool.fetchval("SELECT count(*) FROM research_chunks c JOIN research_pages p "
                                     "ON p.id = c.page_id WHERE p.source_ref = 'https://w.example/waves' "
                                     "AND c.embedding IS NOT NULL")
        check("E a big page is embedded a wave at a time, and every chunk gets its embedding",
              rb.get("chunks", 0) > getattr(rs, "EMBED_WAVE", 64) and max(waves) <= getattr(rs, "EMBED_WAVE", 64)
              and rb.get("embedded") == rb.get("chunks") == stored, (rb, waves[:5], stored))
        bridge_embed.embed_many = _embed_many
        both = await asyncio.gather(*(rs.save_page(pool, E, project_id=pid, thread_key=None,
                                                   url="https://same.example/p", title=f"v{i}",
                                                   text=f"Version {i} of the same page.", whole=True)
                                      for i in range(2)))
        n_same = await pool.fetchval("SELECT count(*) FROM research_pages WHERE source_ref = 'https://same.example/p'")
        check("E two saves of one page at once both land, one after the other; one page remains",
              all(not b.get("error") for b in both) and n_same == 1, (both, n_same))
        check("E no save lock is left behind once its saves are done", not rs._SAVE_LOCKS, rs._SAVE_LOCKS)

        # ── W ──
        await pool.execute("UPDATE research_pages SET last_used_at = now() WHERE source_ref = 'https://pilot.example/report'")
        await pool.execute("UPDATE research_pages SET last_used_at = now() - make_interval(days => $1) "
                           "WHERE source_ref = 'https://b.example/solar'", rs.KEEP_DAYS + 1)
        outside = tmp / "outside.txt"
        outside.write_text("keep me")
        await pool.execute("UPDATE research_pages SET path = $1, last_used_at = now() - make_interval(days => $2) "
                           "WHERE source_ref = 'https://t.example/x'", str(outside), rs.KEEP_DAYS + 1)
        solar_file = pathlib.Path(res2["file"])
        gone = await rs.sweep(pool)
        left = {r["source_ref"] for r in await pool.fetch("SELECT source_ref FROM research_pages")}
        orphan_chunks = await pool.fetchval("SELECT count(*) FROM research_chunks c LEFT JOIN research_pages p "
                                            "ON p.id = c.page_id WHERE p.id IS NULL")
        check("W a page unused for KEEP_DAYS goes: its row, its chunks and its file; a used one stays",
              gone >= 2 and "https://b.example/solar" not in left and "https://pilot.example/report" in left
              and orphan_chunks == 0 and not solar_file.exists(), (gone, left, orphan_chunks))
        check("W a row whose path is outside the research folder never deletes that file",
              outside.exists() and "https://t.example/x" not in left, left)
    finally:
        await pool.close()
    return 1 if FAILED else 0


with tempfile.TemporaryDirectory() as _tmp, \
        (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db, pathlib.Path(_tmp)))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
