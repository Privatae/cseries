#!/usr/bin/env python3
"""Gate: prompt provenance + kill-switch removal.

Why this gate exists: a refactor wave severed three of the
personality subsystem's four layers and NOTHING caught it — the per-turn
codebook block bypassed prompt_log entirely, every failure on the turn
path was a swallowed log.warning, and a bare /tmp file could disable all
personality injection in production. This gate makes each of those
regressions loud and permanent to re-introduce.

Checks:
  1. BEHAVIORAL — prompt_schema.log_injected_block writes a real
     prompt_log row (scope per_turn, named block, correct char count)
     against the container DB, visible to inspectors.
  2. SOURCE RATCHET — session.py's constitution-block append is followed
     by a log_injected_block call (the provenance can't silently drop out).
  3. SOURCE RATCHET — the turn-path failure sites log.error with entity id
     (no reversion to swallowed warnings).
  4. The simulated psyche stays gone:
     no personality-expression block is appended to the prompt and the
     personality package (its expression synthesis, the drift loop and the
     test-mode kill switch) is not in the tree. Its checks here (the
     expression append, the drift-loop and drift-check error lines, the
     CSERIES_DIRECTIVE_TEST_ENABLE gate, every dimension expressible under a
     spike boost) went with it.

Run inside the cseries container (needs /app + the embedded PG):
    python3 /app/scripts/test_prompt_provenance.py
Exit 0 = pass, 1 = fail. Prints one line per check.
"""
import asyncio
import os
import re
import sys
import uuid

sys.path.insert(0, "/app/brain")
sys.path.insert(0, "/app")

from turn_source import turn_source  # noqa: E402  session.py and the modules split out of it

DSN = "postgresql://entity@/hostmem?host=/var/run/postgresql"
APP = "/app"
PERSONALITY_PKG = "/app/brain/mastercode/personality"

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)


async def behavioral_gate() -> None:
    import asyncpg
    from mastercode.runtime.prompt_schema import log_injected_block

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=2)

    class _StubRuntime:
        def __init__(self, p):
            self.pool = p

    class _StubSess:
        entity_id = 1
        session_id = f"provenance-gate-{uuid.uuid4()}"

    sess = _StubSess()
    text = "## Relevant knowledge\nGate-0 provenance probe."
    await log_injected_block(_StubRuntime(pool), sess, "constitution_context", text)

    row = await pool.fetchrow(
        "SELECT scope, block_count, total_chars, blocks::text AS blocks "
        "FROM prompt_log WHERE session_id = $1",
        sess.session_id,
    )
    check("prompt_log row lands for injected block", row is not None)
    if row:
        check("scope is per_turn", row["scope"] == "per_turn", row["scope"])
        check("block named constitution_context", "constitution_context" in row["blocks"])
        check("char count recorded", row["total_chars"] == len(text), str(row["total_chars"]))
        await pool.execute("DELETE FROM prompt_log WHERE session_id = $1", sess.session_id)
    await pool.close()


def source_ratchets() -> None:
    src = turn_source(APP)

    # 2. the constitution append is followed by provenance within the same block.
    m2 = re.search(r"_sys_content \+= \"\\n\\n\" \+ _const_block(.{0,400})", src, re.S)
    check(
        "session.py: constitution block append carries log_injected_block",
        bool(m2) and "log_injected_block(" in m2.group(1),
    )

    # 3. turn-path failures are loud errors with entity context.
    check(
        "session.py: per-turn schema failure is log.error (PROMPT DEGRADED)",
        "per-turn schema block failed" in src.lower() and src.count("PROMPT DEGRADED") >= 2,
    )
    check(
        "session.py: no swallowed 'failed (non-fatal)' warnings on the turn path",
        "Per-turn schema block failed (non-fatal)" not in src
        and "Codebook core failed (non-fatal)" not in src
        and "personality expression failed (non-fatal)" not in src,
    )

    # 4. the simulated psyche stays gone.
    check(
        "session.py: no personality-expression block is appended to the prompt",
        not re.search(r"_sys_content \+= [^\n]*_expr_block", src),
    )
    check(
        "the personality package (expression synthesis, drift loop, kill switch) is not in the tree",
        not os.path.exists(PERSONALITY_PKG),
    )


def main() -> int:
    print("[prompt-provenance gate]")
    try:
        asyncio.run(behavioral_gate())
    except Exception as e:
        check("behavioral gate ran", False, f"{type(e).__name__}: {e}")
    source_ratchets()
    if fails:
        print(f"prompt-provenance gate: FAIL ({len(fails)}: {', '.join(fails)})")
        return 1
    print("prompt-provenance gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
