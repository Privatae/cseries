#!/usr/bin/env python3
"""Smoke checker: the boot memory backfill must route to the entity's ACTIVE
embedding dim column, and must say WHY when nothing lands.

Why this gate exists (2026-08-01). `_backfill_unembedded_memories` hardcoded
the 1024d `embedding` column in its count, its select and its update. On
a Host embedding with qwen3-embedding:4b (2560d) that meant:

  * every UPDATE was rejected by postgres — "expected 1024 dimensions, not
    2560" — 100 rows per boot, for two months; and
  * the predicate `embedding IS NULL` asked about a column that stays NULL
    forever on such an entity, so the pass could never converge even once the
    write was fixed.

The failure was invisible because the per-row error was logged at debug and
the summary said "N failed" without a cause. So this gate pins BOTH halves:
the routing, and the diagnosis.

Case D is the one to keep. brain_service._entity_id is a module default of 1
(brain_service.py:27), never None, so an `is None` guard on it is dead code —
the real hazard is resolving entity 1 before initialize() has run and reading
ANOTHER entity's config, which silently sends vectors back to the wrong
column. The backfill must refuse to run unless the entity it resolved is
actually present in this pod's registry.

Runs offline: no DB, no k8s, no embed lane. The function is loaded from source
because importing server.main drags in the whole FastAPI app.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT", pathlib.Path(__file__).resolve().parent.parent))
TARGET = ROOT / "server" / "main.py"
FUNC = "_backfill_unembedded_memories"

_COLS = {768: "embedding_768", 1024: "embedding", 2048: "embedding_2048",
         2560: "embedding_2560", 4096: "embedding_4096"}


class _Clock:
    """Stands in for the `time` module inside the extracted function."""
    def __init__(self): self.now = 1000.0
    def monotonic(self): return self.now


_CLOCK = _Clock()


class _Log:
    def __init__(self): self.lines: list[tuple[str, str]] = []
    def _add(self, lv, m, a): self.lines.append((lv, m % a if a else m))
    def info(self, m, *a): self._add("INFO", m, a)
    def warning(self, m, *a): self._add("WARN", m, a)
    def debug(self, m, *a): self._add("DEBUG", m, a)
    def error(self, m, *a): self._add("ERROR", m, a)
    def text(self, lv=None):
        return " | ".join(m for l, m in self.lines if lv is None or l == lv)


class _Pool:
    """Minimal asyncpg-shaped stub. Records what SQL the backfill emits."""
    def __init__(self, dim, vec, rows, known=1, write_error=None):
        self.dim, self.vec, self.rows = dim, vec, rows
        self.known, self.write_error = known, write_error
        self.updates: list[str] = []

    async def fetchval(self, q, *a):
        if "entity_registry" in q:
            return self.known
        if "COUNT(*)" in q:
            return len(self.rows)
        return None

    async def fetch(self, q, *a):
        self.select_sql, self.select_args = q, a
        m = re.search(r"ORDER BY \(id = ANY\(\$2::bigint\[\]\)\)( DESC)?", q)
        if m and len(a) > 1:
            # what Postgres does with the direction the SQL actually asks for:
            # ascending puts false (never failed) before true
            return sorted(self.rows, key=lambda r: r["id"] in a[1],
                          reverse=bool(m.group(1)))
        return self.rows

    async def execute(self, q, *a):
        if self.write_error:
            raise Exception(self.write_error)
        self.updates.append(q)


def _install_stubs():
    me = types.ModuleType("mastercode.memory_engine")

    async def active_embedding_column(pool, entity_id):
        return _COLS[pool.dim], pool.dim

    async def _get_memory_embedding(text, *, pool=None, entity_id=None):
        pool.calls = getattr(pool, "calls", []) + [text]
        return pool.vec(text) if callable(pool.vec) else pool.vec

    me.EMBEDDING_COLUMN_BY_DIM = dict(_COLS)
    me.active_embedding_column = active_embedding_column
    me._get_memory_embedding = _get_memory_embedding
    me.embedding_cast_type = lambda d: f"halfvec({d})" if d > 2000 else f"vector({d})"
    me.last_embed_model = lambda: "qwen3-embedding:4b"
    mc = types.ModuleType("mastercode")
    mc.memory_engine = me
    sys.modules["mastercode"] = mc
    sys.modules["mastercode.memory_engine"] = me
    bs = types.ModuleType("server.brain_service")
    srv = types.ModuleType("server")
    srv.brain_service = bs
    sys.modules["server"] = srv
    sys.modules["server.brain_service"] = bs
    return bs


def _load():
    tree = ast.parse(TARGET.read_text())
    fn = next((n for n in tree.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == FUNC), None)
    if fn is None:
        return None, None
    log = _Log()
    ns: dict = {"logger": log, "__name__": "backfill_under_test",
                "_backfill_fails": {}, "_backfill_retry_at": {},
                "_BACKFILL_INTERVAL_S": 900, "_BACKFILL_RETRY_CAP_S": 3600,
                "time": _CLOCK}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(TARGET), "exec"), ns)
    return ns[FUNC], log


def main() -> int:
    if not TARGET.is_file():
        print(f"FAIL: {TARGET} not found")
        return 1
    run, log = _load()
    if run is None:
        print(f"FAIL: {FUNC} missing from server/main.py — the boot memory "
              f"backfill gate cannot run")
        return 1
    bs = _install_stubs()
    rows = [{"id": 1, "content": "alpha"}, {"id": 2, "content": "beta"}]
    bad: list[str] = []

    def go(pool, entity_id=9003):
        bs._entity_id = entity_id
        log.lines.clear()
        asyncio.run(run(pool))
        return pool

    # A. a non-1024 entity must write its own column, with the halfvec cast.
    p = go(_Pool(2560, "[0.1]", rows))
    if not p.updates or "embedding_2560" not in p.updates[0]:
        bad.append("2560d entity did not UPDATE embedding_2560 "
                   f"(emitted: {p.updates[:1] or 'nothing'})")
    elif "halfvec(2560)" not in p.updates[0]:
        bad.append("2560d entity wrote embedding_2560 without the halfvec cast "
                   "— `::vector` is rejected on a halfvec column")

    # B. no vector produced must be named, not reported as "unknown".
    go(_Pool(2560, None, rows))
    w = log.text("WARN")
    if "no vector" not in w or "unknown" in w:
        bad.append(f"embed-returned-None is not diagnosed (WARN said: {w or 'nothing'})")

    # C. a write rejected by the DB must be distinguished from B, and must
    #    carry the DB's own message — this is the production string.
    go(_Pool(2560, "[0.1]", rows, write_error="expected 1024 dimensions, not 2560"))
    w = log.text("WARN")
    if "rejected on write" not in w or "1024 dimensions" not in w:
        bad.append(f"DB-rejected write is not diagnosed (WARN said: {w or 'nothing'})")

    # D. an entity absent from THIS pod's registry must abort before writing.
    p = go(_Pool(2560, "[0.1]", rows, known=None))
    if p.updates:
        bad.append("backfill WROTE despite the resolved entity being absent from "
                   "this pod's entity_registry — it may be reading another "
                   "entity's dim config")
    elif "entity_registry" not in log.text():
        bad.append("unverified entity was skipped without saying why")

    # E. the 1024d path must be untouched — this is most of the fleet.
    p = go(_Pool(1024, "[0.1]", rows))
    if not p.updates or "SET embedding =" not in p.updates[0] or \
            "vector(1024)" not in p.updates[0]:
        bad.append("1024d entity no longer writes `embedding` as vector(1024) "
                   f"(emitted: {p.updates[:1] or 'nothing'})")

    g = run.__globals__
    fails, retry_at = g["_backfill_fails"], g["_backfill_retry_at"]

    def reset():
        # B/C ran boot passes that failed: that state must not leak onward.
        fails.clear(); retry_at.clear(); bs._entity_id = 9003; log.lines.clear()

    def tick(pool, advance=900):
        asyncio.run(run(pool, periodic=True))
        _CLOCK.now += advance
        return pool

    # F. (2026-09-24) the periodic pass embeds like the boot pass...
    reset()
    p = tick(_Pool(2560, "[0.1]", rows))
    if len(p.updates) != 2:
        bad.append(f"periodic pass did not embed the pending rows (updates={len(p.updates)})")
    # G. ...but is SILENT when nothing landed: an unconfigured host must not be
    #    warned every tick. The boot pass (B) still warns once.
    reset()
    tick(_Pool(2560, None, rows))
    if log.text("WARN"):
        bad.append(f"periodic pass warned on no-vector (said: {log.text('WARN')})")

    # I. only rows with NO vector in ANY dim column: after a model switch the
    #    new dim is filled on the user's explicit request, never by this pass.
    reset()
    p = go(_Pool(2560, "[0.1]", rows))
    missing = [c for c in _COLS.values() if f"{c} IS NULL" not in p.select_sql]
    if missing:
        bad.append("backfill selects rows that already have a vector in another dim "
                   f"(no `IS NULL` on {missing}) — a model switch becomes an unasked "
                   "full re-embed")

    # J. an endpoint OUTAGE fails every row it touches. Those rows must be
    #    ordered last / backed off, never excluded: once the lane is back they
    #    are exactly what this loop exists to recover (within the 1h cap).
    reset()
    per_pass = [len(getattr(tick(_Pool(2560, None, rows)), "calls", []))
                for _ in range(3)]                      # 45 min of outage
    if any(n > 1 for n in per_pass):
        bad.append(f"a periodic pass kept calling after a no-vector row (calls/pass "
                   f"{per_pass}); the lane is in cooldown by then")
    _CLOCK.now += 3600                                  # lane back, past the cap
    p = tick(_Pool(2560, "[0.1]", rows))
    sql = getattr(p, "select_sql", "")
    if "NOT (id = ANY" in sql or "ORDER BY (id = ANY" not in sql:
        bad.append("failed rows must be ordered last, never excluded "
                   f"(SELECT was: {sql[:160]})")
    if len(p.updates) != 2 or fails or retry_at:
        bad.append(f"after the outage the rows were not recovered "
                   f"(updates={len(p.updates)}, still failed={fails})")

    # K. ONE row that always fails: it must not starve the rows behind it
    #    (ordered last), and must not be re-tried every tick (backed off; each
    #    failed call trips the lane cooldown and logs EMBEDDINGS DOWN).
    reset()
    three = rows + [{"id": 3, "content": "gamma"}]
    only_alpha_fails = lambda t: None if t == "alpha" else "[0.1]"
    p1 = tick(_Pool(2560, only_alpha_fails, three))
    p2 = tick(_Pool(2560, only_alpha_fails, three))
    if len(p2.updates) != 2 or p2.calls[:1] == ["alpha"]:
        bad.append(f"a row that always fails starves the rest (pass 2 calls="
                   f"{p2.calls}, updates={len(p2.updates)})")
    p3 = tick(_Pool(2560, only_alpha_fails, [three[0]]))
    if getattr(p3, "calls", []):
        bad.append("a row that just failed is re-tried on the very next tick "
                   "(no backoff): the lane trips its cooldown every 15 minutes")
    reset()

    # L. the counter forgets rows that left the candidate set (embedded
    #    elsewhere / archived) when the whole set fits in one batch.
    fails[99] = 1; retry_at[99] = _CLOCK.now + 10**6
    tick(_Pool(2560, "[0.1]", rows))
    if 99 in fails or 99 in retry_at:
        bad.append("failure counter kept an id that is no longer a candidate")
    reset()

    # H. the loop: one boot pass, then periodic passes, skipped while the
    #    embed lane is in cooldown.
    tree = ast.parse(TARGET.read_text())
    loop_fn = next((n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_backfill_unembedded_loop"), None)
    if loop_fn is None:
        bad.append("_backfill_unembedded_loop missing — the backfill runs at boot only")
    else:
        calls: list = []
        health = {"down": False}
        ticks = {"n": 0}

        async def fake_backfill(pool, *, periodic=False):
            calls.append(periodic)

        async def fake_sleep(_s):
            ticks["n"] += 1
            health["down"] = ticks["n"] == 2       # tick 2 is in cooldown
            if ticks["n"] > 3:
                raise asyncio.CancelledError

        sys.modules["mastercode.memory_engine"].embedding_health = lambda: dict(health)
        fake_asyncio = types.SimpleNamespace(sleep=fake_sleep,
                                             CancelledError=asyncio.CancelledError)
        lns = {"logger": _Log(), "asyncio": fake_asyncio, "_BACKFILL_INTERVAL_S": 900,
               "_backfill_unembedded_memories": fake_backfill}
        exec(compile(ast.Module(body=[loop_fn], type_ignores=[]), str(TARGET), "exec"), lns)
        asyncio.run(lns["_backfill_unembedded_loop"](None))
        if calls != [False, True, True]:
            bad.append("loop should run boot pass, skip the cooldown tick, then "
                       f"periodic passes: expected [False, True, True], got {calls}")

    if bad:
        print(f"FAIL: {len(bad)} memory-backfill dim-routing defect(s):")
        for b in bad:
            print("  " + b)
        return 1
    print("OK — memory backfill routes to the active dim column (768/1024/2048/"
          "2560/4096), refuses to run for an unverified entity, and reports "
          "no-vector vs write-rejected separately")
    return 0


if __name__ == "__main__":
    sys.exit(main())
