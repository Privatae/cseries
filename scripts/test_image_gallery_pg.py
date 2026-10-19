#!/usr/bin/env python3
"""image_engine._gallery against a REAL Postgres (asyncpg), not a fake pool.

The gallery reads two stores (image_engine._gallery): the gallery entries
under entity_context images/*.json and the stored pictures no entry points at
(generated_images). test_caps_images drives it through a pool that never
parses SQL, so the statements themselves (their $n parameters, the LIKE and
position() match, the merge-paging) are only proven here.

  A  the four statements run on asyncpg and the merge pages newest first
     across both stores, with a true total and next_offset;
  B  a stored picture that only a bookkeeping row mentions (the last pasted
     picture's stash, an old understand analysis — both left out of the
     listing) is still listed: the "nothing points at it" test must ignore the
     same rows the listing ignores, or the picture is in neither half.

Runs on the kernel's own schema (scripts/kernel_schema.kernel_schema_db), on
TEST_PG_DSN or a throwaway server, like the other *_pg gates; with neither it
SKIPs loudly with exit 2.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import pathlib
import secrets
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the gallery's SQL.")
    sys.exit(2)

import asyncpg  # noqa: E402
from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

FAILED = 0
EID = 1


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


class Ctx:
    def __init__(self, pool, **args):
        self.pool, self.entity_id, self.args = pool, EID, args

    def log(self, *_a, **_k):
        pass


def gid() -> str:
    return "gimg_" + secrets.token_urlsafe(16)


async def run(dsn: str) -> None:
    from mastercode import image_engine as ie
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        t0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        listed, renders = [], []
        # Five generated pictures, each with its gallery entry (newest = i=4).
        for i in range(5):
            g = gid()
            listed.append(g)
            at = t0 + dt.timedelta(minutes=10 * i)
            await pool.execute("INSERT INTO generated_images (id, entity_id, content_type, bytes, "
                               "prompt, model, created_at) VALUES ($1,$2,'image/png',$3,$4,'m',$5)",
                               g, EID, b"\x89PNG", f"gen {i}", at)
            await pool.execute("INSERT INTO entity_context (entity_id, file_path, content, updated_at) "
                               "VALUES ($1,$2,$3,$4)", EID, f"images/gen_{i}.json",
                               json.dumps({"prompt": f"gen {i}", "url": f"/api/images/{g}"}), at)
        # Three app renders no entry points at, interleaved in time.
        for i in range(3):
            g = gid()
            renders.append(g)
            await pool.execute("INSERT INTO generated_images (id, entity_id, content_type, bytes, "
                               "prompt, model, created_at) VALUES ($1,$2,'image/png',$3,$4,$5,$6)",
                               g, EID, b"\x89PNG", f"blender/render {i}", "app:blender-engine",
                               t0 + dt.timedelta(minutes=10 * i + 5))
        # One more render that only bookkeeping mentions: an old analysis row
        # quoting its absolute link, and the last-pasted stash. Neither is a
        # gallery entry, so neither may hide it.
        hidden = gid()
        await pool.execute("INSERT INTO generated_images (id, entity_id, content_type, bytes, "
                           "prompt, model, created_at) VALUES ($1,$2,'image/png',$3,'cad/render',"
                           "'app:cadcraft',$4)", hidden, EID, b"\x89PNG", t0 + dt.timedelta(hours=2))
        await pool.execute("INSERT INTO entity_context (entity_id, file_path, content, updated_at) "
                           "VALUES ($1,'images/analysis_1760000000.json',$2,$3)", EID,
                           json.dumps({"question": "q", "analysis": "a",
                                       "source": f"https://host-a.example.test/api/images/{hidden}"}),
                           t0 + dt.timedelta(hours=3))
        await pool.execute("INSERT INTO entity_context (entity_id, file_path, content, updated_at) "
                           "VALUES ($1,'images/last_attachment.json',$2,$3)", EID,
                           json.dumps({"data": "iVBORw0KGgo", "media_type": "image/png"}),
                           t0 + dt.timedelta(hours=4))
        # Another Host's picture never shows.
        other = await pool.fetchval("SELECT id FROM entity_registry WHERE id <> $1 LIMIT 1", EID)
        if other is not None:
            await pool.execute("INSERT INTO generated_images (id, entity_id, content_type, bytes) "
                               "VALUES ($1, $2, 'image/png', $3)", gid(), other, b"\x89PNG")

        whole = await ie._gallery(Ctx(pool, offset=0, limit=50))
        urls = [i["url"] for i in whole.get("images") or []]
        check("A the gallery's statements run on asyncpg", whole.get("status") == "ok", whole)
        check("A the bookkeeping rows are not pictures in the listing",
              not any((i.get("path") or "").startswith(("images/analysis_", "images/last_attachment"))
                      for i in whole.get("images") or []), urls)
        check("B a render only a bookkeeping row mentions is still listed",
              f"/api/images/{hidden}" in urls, urls)
        check("A total counts both stores, each picture once (5 entries + 4 unlisted)",
              whole.get("total") == 9 and len(urls) == 9 and len(set(urls)) == 9, whole.get("total"))
        check("A newest first across both stores", urls[0] == f"/api/images/{hidden}"
              and urls[1] == f"/api/images/{listed[4]}", urls[:3])
        pages, off = [], 0
        while True:
            p = await ie._gallery(Ctx(pool, offset=off, limit=4))
            pages += [i["url"] for i in p["images"]]
            if "next_offset" not in p:
                break
            off = p["next_offset"]
        check("A paging by 4 returns the same order as one page, with no gap or repeat",
              pages == urls, (pages, urls))
    finally:
        await pool.close()


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=EID) as _db:
    asyncio.run(run(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
