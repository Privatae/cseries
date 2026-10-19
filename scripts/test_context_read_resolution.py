#!/usr/bin/env python3
"""Smoke gate: relayed-identifier resolution in context_ingest op='read'.

THE CLASS THIS RATCHETS: identifiers
that reach a tool RELAYED through prose — a host note, a dispatched agent's
task prompt — arrive abbreviated or mangled, and an exact-match filter then
returns empty, which the caller reads as "the document doesn't exist". Three
runs over two days concluded a fully-staged file was missing.

The wall is at the tool boundary, not in prompts: op='read' must
  1. prefix-resolve an abbreviated upload_batch key (with batch_note),
  2. fall back to the filename's newest upload on a wrong key (noted),
  3. resolve case/separator-drifted filenames (with file_note),
  4. on a true filename miss, ERROR naming the closest real filenames and
     pointing at op='files' — never a bare empty result,
  5. leave exact-match calls untouched (baseline behavior preserved).

Runs in-container against the live schema: seeds a synthetic entity's
corpus, exercises _exec_context_ingest directly (the real handler, no HTTP
shim), always cleans up.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")

TEST_EID = 99321  # synthetic; never a real entity, deleted in finally

OLD_BATCH = "aaaa1111aaaa1111aaaa1111aaaa1111"
NEW_BATCH = "bbbb2222bbbb2222bbbb2222bbbb2222"
RES_BATCH = "0b5e7a11c3d4e5f60718293a4b5c6d7e"  # the shape of a real key


async def seed(pool) -> None:
    import json
    rows = []
    # spend account.csv — TWO uploads (older 1 chunk, newer 3 chunks)
    rows.append(("spend account.csv (1/1)", "old spend body",
                 {"from_file": "spend account.csv", "upload_batch": OLD_BATCH,
                  "chunk": 0}, "now() - interval '1 day'"))
    for i in range(3):
        rows.append((f"spend account.csv ({i + 1}/3)", f"spend chunk {i}",
                     {"from_file": "spend account.csv", "upload_batch": NEW_BATCH,
                      "chunk": i}, "now()"))
    # budget.csv — ONE upload, 2 chunks
    for i in range(2):
        rows.append((f"budget.csv ({i + 1}/2)", f"budget chunk {i}",
                     {"from_file": "budget.csv", "upload_batch": RES_BATCH,
                      "chunk": i}, "now()"))
    for title, content, meta, created in rows:
        await pool.execute(
            f"INSERT INTO user_context (entity_id, content, source, title, "
            f"metadata, status, created_at) "
            f"VALUES ($1, $2, 'smoke', $3, $4::jsonb, 'raw', {created})",
            TEST_EID, content, title, json.dumps(meta))


async def main() -> int:
    import asyncpg
    pool = await asyncpg.create_pool(
        host="/var/run/postgresql", user="entity", database="hostmem",
        min_size=1, max_size=2)
    failures: list[str] = []
    try:
        # user_context is declared (init-db.sql, migrations/v090) and applied by
        # entrypoint.sh before this container serves anything; the ensure_table()
        # this used to call went with the runtime DDL.
        # A prior aborted run must not double-seed.
        await pool.execute(
            "DELETE FROM user_context WHERE entity_id = $1", TEST_EID)
        await seed(pool)

        from server import brain_service
        brain_service._pool = pool
        brain_service._entity_id = TEST_EID
        from mastercode.runtime.tools import _exec_context_ingest as run

        def check(name: str, cond: bool, detail: str = "") -> None:
            if cond:
                print(f"  [read-resolution] OK {name}")
            else:
                failures.append(name)
                print(f"  [read-resolution] FAIL {name} {detail}")

        # 5. baseline: exact filename, exact batch — untouched
        r = await run({"op": "read", "file": "budget.csv",
                       "upload_batch": RES_BATCH})
        check("exact file + exact batch reads clean",
              r["status"] == "ok" and r["result"]["chunks"] == 2
              and "batch_note" not in r["result"]
              and "file_note" not in r["result"], f"got {r}")

        # 1. abbreviated batch key (the incident, verbatim shape)
        r = await run({"op": "read", "file": "budget.csv",
                       "upload_batch": "0b5e7a11"})
        check("abbreviated batch key prefix-resolves",
              r["status"] == "ok" and r["result"]["chunks"] == 2
              and RES_BATCH in (r["result"].get("batch_note") or ""), f"got {r}")

        # 2. wrong batch key + good filename → newest upload, noted
        r = await run({"op": "read", "file": "spend account.csv",
                       "upload_batch": "ffffffff"})
        check("wrong batch key falls back to newest upload",
              r["status"] == "ok" and r["result"]["chunks"] == 3
              and r["result"].get("upload_batch") == NEW_BATCH
              and "batch_note" in r["result"], f"got {r}")

        # 3a. case-drifted filename resolves
        r = await run({"op": "read", "file": "Budget.CSV"})
        check("case-drifted filename resolves with file_note",
              r["status"] == "ok" and r["result"]["chunks"] == 2
              and "budget.csv" in (r["result"].get("file_note") or ""),
              f"got {r}")

        # 3b. separator-drifted filename resolves
        r = await run({"op": "read", "file": "spend_account.csv"})
        check("separator-drifted filename resolves",
              r["status"] == "ok" and r["result"]["chunks"] == 3
              and "file_note" in r["result"], f"got {r}")

        # newest-upload default still notes older copies
        r = await run({"op": "read", "file": "spend account.csv"})
        check("multi-upload file notes older copies",
              r["status"] == "ok" and r["result"]["chunks"] == 3
              and "older_copies" in r["result"], f"got {r}")

        # 4. true miss ERRORS with real candidates, never a bare empty
        r = await run({"op": "read", "file": "revserve.csv"})
        msg = str(r.get("result") or "")
        check("unknown filename errors naming closest real files",
              r["status"] == "error" and "budget.csv" in msg
              and "op='files'" in msg, f"got {r}")
    finally:
        await pool.execute(
            "DELETE FROM user_context WHERE entity_id = $1", TEST_EID)
        await pool.close()

    if failures:
        print(f"read-resolution gate: {len(failures)} FAILURE(S): {failures}")
        return 1
    print("read-resolution gate: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
