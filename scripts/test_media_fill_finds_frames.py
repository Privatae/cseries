#!/usr/bin/env python3
"""Gate: a video frame whose embedding failed is FOUND AGAIN by the media fill.

This one needs a real Postgres, because the bug it exists to prevent was
a SELECT predicate and nothing but a real planner can tell you whether a row is
in a population or not.

THE BUG, in full, because the shape of it is the lesson:

    A frame row is metadata.kind='frame'. NOT_FILE_KIND_SQL excludes it from
    every TEXT selector — correct, a frame is not text. The MEDIA lane selects
    POSITIVELY, and it used to select `metadata->>'kind' = 'file'`. So a frame
    was in NEITHER population: excluded from one, never chosen by the other.

    The only thing that ever embedded a frame was the inline pass at extraction
    time. When that failed part way — a provider timeout, a 502 on frame 180 of
    240, the request aborted, the endpoint down for a minute — those rows existed
    with no embedding and NOTHING IN THE SYSTEM WOULD EVER PICK THEM UP AGAIN.
    The coverage lens reported the clip as unsearchable, which was honest, and
    there was no path to make it searchable short of re-pulling the whole clip
    across the user's link and decoding it again — to repair an embedding that
    had failed for a reason having nothing to do with the frames.

So: frames are written WITH their jpegs and no vector, the fill is the one lane
that embeds them, and the vector's arrival is what drops the staged bytes. The
12x storage measurement still holds in steady state; what changed is that there
is now something to retry FROM in between.

RUN IT:  python3 scripts/test_media_fill_finds_frames.py

It runs on a fresh clone of the KERNEL's schema (scripts/kernel_schema.py —
entrypoint.sh's procedure, every migration) on a test server: MEDIA_FILL_TEST_DSN
/ PEER_TEST_DSN / TEST_DSN, else a throwaway cseries-postgres started through
scripts/peer_test_db.sh, reached over its unix socket (no TCP listener, no port).
Exits 2 and says so with neither. It used to DROP user_context on whatever
database the DSN named and rebuild it from v090 + v135 alone — a hand-picked
pair whose result differs from a full build (the note below) — which
scripts/test_no_hand_kernel_tables.py now refuses.

To point it at a server of your own, note the socket, because the product
image has NO TCP LISTENER:

THE IMAGE HAS NO NETWORK LISTENER, BY DESIGN. cseries-postgres writes
`listen_addresses = ''` into postgresql.conf from its own entrypoint, and its
auth is `trust` PRECISELY BECAUSE there is nothing to authenticate to:
"the cluster is unix-socket-only, the socket dir is an emptyDir shared with
exactly one peer container in the same pod, and auth is `trust` because there is
no network listener to authenticate to" (/app/entrypoint-postgres.sh:18-21).

So `-p 5432` publishes a port nothing is bound to, and a tcp DSN gets
ConnectionResetError. Appending `-c listen_addresses='*'` to the run command
does not help either — the entrypoint rewrites the conf after the flag is read.

DO NOT "FIX" THAT BY OPENING THE LISTENER. Setting listen_addresses and adding a
`host all all 0.0.0.0/0 trust` line to pg_hba does make this connect, and it
turns a cluster whose whole auth story is "there is no listener" into an open
one. The socket below costs nothing and changes nothing.

PG_SOCKET is the image's own knob (entrypoint-postgres.sh:98) and the
postmaster is started with `-k "$PG_SOCKET"`, so pointing it at a mounted
directory is one way in. A mount over its default, /var/run/postgresql, is the
other, and is what peer_test_db.sh does: measured, /run/postgresql
is a plain directory in this image (no tmpfs, no VOLUME), and the socket
reaches the host through the mount.

    SOCKDIR=$(mktemp -d /tmp/vfsock.XXXXXX) && chmod 777 "$SOCKDIR"
    docker run -d --name vidframe-pg \
        -e PG_SOCKET=/tmp/pgsock -v "$SOCKDIR":/tmp/pgsock \
        cseries-postgres:smoke
    # wait for "$SOCKDIR/.s.PGSQL.5432" to appear, then:
    MEDIA_FILL_TEST_DSN="postgres://entity@/postgres?host=$SOCKDIR" \
        APP_ROOT=<repo> python3 scripts/test_media_fill_finds_frames.py
    docker rm -f vidframe-pg

chmod 777 on the directory is needed because the container runs as uid 1000 and
has to create the socket in it. No password: the image's auth is `trust` over
the socket, which is the posture described above and not something this gate
relaxes.

Declared in scripts/gates-infra-only.txt with its dependency named, which is
this repo's existing home for a gate that needs live infrastructure — NOT in
gates-container-free.txt, because that list is the half that must run without
one. The source-level invariants it rests on are pinned in
scripts/test_video_frame_lane.py, which IS container-free, so a revert of the
predicate fails the pre-merge suite too rather than waiting for someone to
remember this file.
"""
import asyncio
import json
import os
import sys

import contextlib
import subprocess

# The tree this file lives in (APP_ROOT to point elsewhere).
ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "brain"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

SERVER = (os.environ.get("MEDIA_FILL_TEST_DSN") or os.environ.get("PEER_TEST_DSN")
          or os.environ.get("TEST_DSN") or "").strip()
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (MEDIA_FILL_TEST_DSN/PEER_TEST_DSN/TEST_DSN) and docker "
          "unavailable. This gate asserts a SQL predicate against a real planner and cannot be "
          "faked; see this file's header.", file=sys.stderr)
    sys.exit(2)

import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

from mastercode.media_engine import (  # noqa: E402
    MEDIA_DROP_STAGED_CONTENT_SQL, media_embeddable_sql, media_fresh_sql,
)

failures = []
checks = 0

ENTITY = 4242
MODEL = "test-media-model"
COL = "media_embedding_1024"

# The predicate the fill used BEFORE this change. Kept here as the RED arm: the
# test runs the same rows through both and the difference IS the finding.
LEGACY_PREDICATE = ("metadata->>'kind' = 'file' "
                    "AND metadata->>'mime' LIKE 'image/%'")


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok    {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def vec(seed: float) -> str:
    return "[" + ",".join(str(seed) for _ in range(1024)) + "]"


async def main(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)

    # The REAL table: a fresh clone of the kernel's whole schema
    # (scripts/kernel_schema.py), so this cannot drift from what a host gets.
    # It used to rebuild user_context from v090 + v135 alone, and v090 restates
    # `vector(2048)`/`vector(4096)` where v071:45-52 converts them to halfvec and
    # carries none of v078's indexes — a pair that differs from a full build.
    # The columns this test touches (content, media_embedding_1024,
    # media_embed_model, media_parent_id, media_offset_ms) come from v135 here
    # exactly as on a host.
    async def add(kind, mime, content, *, vector=None, model=None, parent=None,
                  offset_ms=None, title="row"):
        return await conn.fetchval(
            f"""INSERT INTO user_context
                    (entity_id, content, source, title, metadata,
                     media_parent_id, media_offset_ms, {COL}, media_embed_model)
                VALUES ($1, $2, 'test', $3, $4::jsonb, $5, $6, $7::halfvec(1024), $8)
                RETURNING id""",
            ENTITY, content, title,
            json.dumps({"kind": kind, "mime": mime}),
            parent, offset_ms, vector, model)

    # ── the corpus ──────────────────────────────────────────────────────────
    clip = await add("file", "video/mp4", "AAAA", title="demo.mp4")
    # Three frames of that clip, staged with their jpegs, NO vector — exactly
    # the state an inline pass that died on frame 1 of 3 leaves behind.
    f0 = await add("frame", "image/jpeg", "ZmFrZWpwZWcw", parent=clip, offset_ms=0)
    f1 = await add("frame", "image/jpeg", "ZmFrZWpwZWcx", parent=clip, offset_ms=6000)
    f2 = await add("frame", "image/jpeg", "ZmFrZWpwZWcy", parent=clip, offset_ms=12000)
    # An uploaded picture, unembedded — the population that already worked.
    pic = await add("file", "image/png", "cG5nYnl0ZXM=", title="shot.png")
    # An already-embedded frame with its bytes dropped: the steady state. It must
    # NOT come back, or the fill loops on rows it can never do work on.
    done = await add("frame", "image/jpeg", "", parent=clip, offset_ms=18000,
                     vector=vec(0.1), model=MODEL)
    # A text row, which must never enter the media lane.
    await add("text", None, "some words", title="note")

    async def select(predicate):
        rows = await conn.fetch(
            f"""SELECT id FROM user_context
                 WHERE entity_id = $1 AND status != 'dismissed' AND {predicate}
                 ORDER BY id""", ENTITY)
        return [r["id"] for r in rows]

    # ── RED: the predicate as it was ────────────────────────────────────────
    legacy = await select(f"{LEGACY_PREDICATE} AND "
                          f"({COL} IS NULL OR media_embed_model IS DISTINCT FROM "
                          f"'{MODEL}')")
    print(f"\n  [red arm] the old predicate selects: {legacy}")
    check("RED — the old predicate could NOT see a single staged frame "
          "(this is the finding)",
          f0 not in legacy and f1 not in legacy and f2 not in legacy,
          f"legacy={legacy} frames={[f0, f1, f2]}")
    check("RED — ...while it did see the uploaded picture, which is why the gap "
          "was invisible",
          pic in legacy)

    # ── GREEN: the predicate now ───────────────────────────────────────────
    live = await select(media_embeddable_sql(COL, f"'{MODEL}'"))
    print(f"  [green arm] the live predicate selects: {live}\n")
    check("GREEN — every staged frame is found by the fill",
          f0 in live and f1 in live and f2 in live,
          f"live={live} frames={[f0, f1, f2]}")
    check("GREEN — the uploaded picture is still found (no regression)",
          pic in live)
    check("a frame already embedded, with its bytes dropped, is NOT selected — "
          "the fill must not loop on a row it can do no work on",
          done not in live)
    check("the clip itself is not a media row", clip not in live)
    check("a text row never enters the media lane",
          len(live) == 4, f"live={live}")

    # ── the denominator is the same predicate ──────────────────────────────
    total = await conn.fetchval(
        f"""SELECT COUNT(*) FROM user_context
             WHERE entity_id = $1 AND status != 'dismissed'
               AND {media_embeddable_sql(COL, "'" + MODEL + "'")}""", ENTITY)
    check("the denominator agrees with the fetch exactly — a disagreement "
          "reports done with work outstanding, or never reaches its own total",
          int(total) == len(live), f"total={total} fetched={len(live)}")

    # ── the writer: a vector lands, the staged bytes go ────────────────────
    await conn.execute(
        f"""UPDATE user_context
               SET {COL} = $1::halfvec(1024),
                   media_embed_model = $3,
                   {MEDIA_DROP_STAGED_CONTENT_SQL},
                   updated_at = now()
             WHERE id = $2""", vec(0.5), f1, MODEL)
    row = await conn.fetchrow(
        f"SELECT content, {COL} IS NOT NULL AS has_vec, media_embed_model "
        f"FROM user_context WHERE id = $1", f1)
    check("a frame's staged bytes are dropped in the same statement that writes "
          "its vector",
          row["content"] == "" and row["has_vec"] and row["media_embed_model"] == MODEL,
          f"content={row['content']!r} vec={row['has_vec']}")
    picrow = await conn.fetchrow("SELECT content FROM user_context WHERE id = $1", pic)
    check("a PICTURE's content is untouched by that fragment — it is the user's "
          "stored file, not staging",
          picrow["content"] == "cG5nYnl0ZXM=", f"content={picrow['content']!r}")

    after = await select(media_embeddable_sql(COL, f"'{MODEL}'"))
    check("the embedded frame leaves the queue and the rest stay in it",
          f1 not in after and f0 in after and f2 in after, f"after={after}")

    # ── and it is searchable, which is the point of all of it ──────────────
    hits = await conn.fetch(
        f"""SELECT id FROM user_context
             WHERE entity_id = $1 AND {media_fresh_sql(COL, "'" + MODEL + "'")}
             ORDER BY {COL} <=> $2::halfvec(1024) LIMIT 5""", ENTITY, vec(0.5))
    check("a repaired frame is reachable by a media nearest-neighbour search",
          f1 in [h["id"] for h in hits], f"hits={[h['id'] for h in hits]}")

    await conn.execute("DELETE FROM user_context WHERE entity_id = $1", ENTITY)
    await conn.close()


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    asyncio.run(main(_db))

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS: {checks} checks — a frame whose embedding failed is found again")
