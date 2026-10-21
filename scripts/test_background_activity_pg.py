#!/usr/bin/env python3
"""Gate: GET /api/inference/activity's failures-only option, against a real
kernel schema (a background failure once dropped out of sight).

The Circles working line read the newest 100 background calls and looked for
failures among them, so one failure followed by 100 calls that went well was
counted in `issues` and never listed. `failed=true` lists the failures in the
window by themselves:

  F. with failed=true, a failure 120 calls back is listed, alone, newest
     first, with its error; the chat turn's own calls, another Host's and
     those older than the window are not; `limit` still caps it;
  D. without it the answer is as before: the newest calls of every kind, the
     same keys, and `issues` counting every failure in the window;
  C. the route takes `failed`, false when absent;
  $. every entry carries its cost: cost_usd is the list-price estimate the
     gateway logged when cost_basis is 'list', and null otherwise (a
     subscription or unpriced call is not $0); include_turns adds the Host's
     own turn calls, so every model call can be shown with its cost.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; no pod, no network beyond
loopback.

  PYTHONPATH=brain python3 scripts/test_background_activity_pg.py
"""
import asyncio
import contextlib
import inspect
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the activity route's failures-only list.")
    sys.exit(2)

FAILED = 0
E = 1
OTHER_E = 2
KEYS = {"id", "at", "group", "detail", "source_module", "model", "provider", "tokens_in", "tokens_out",
        "latency_ms", "ok", "error", "error_class", "failed_over_to",
        "task_id",  # an agent's calls carry its task id (None for the Host's own)
        "cost_usd", "cost_basis"}  # what the call cost, and what kind of figure that is


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


async def row(pool, eid, source, purpose, minutes_ago, error=None, cost=(0.0, "unpriced")):
    meta = {"error": error, "error_class": None} if error else {}
    return await pool.fetchval(
        "INSERT INTO inference_cost_log (entity_id, source_module, model_id, provider, purpose, tokens_in, "
        "tokens_out, latency_ms, metadata, created_at, est_cost_usd, cost_method) VALUES ($1, $2, "
        "'claude-haiku-4-5', 'anthropic', $3, 900, 120, 800, $4::jsonb, "
        "now() - ($5::text || ' minutes')::interval, $6, $7) RETURNING id",
        eid, source, purpose, json.dumps(meta), str(minutes_ago), cost[0], cost[1])


async def run(dsn):
    from server import brain_service as _bs
    from server.routes import inference as inf
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    saved = (_bs._pool, _bs._entity_id)
    _bs._pool = pool
    _bs._entity_id = E
    try:
        # Outside the window, the chat turn's own, another Host's: never listed.
        await row(pool, E, "fact_deriver", "inference_error", 60 * 30, "anthropic 529: overloaded")
        chat = await row(pool, E, "entity_runtime", "inference_error", 40, "anthropic 500: chat")
        await row(pool, OTHER_E, "fact_deriver", "inference_error", 40, "anthropic 401: theirs")
        buried = await row(pool, E, "fact_deriver", "inference_error", 50,
                           'anthropic 401: {"error": {"message": "invalid x-api-key"}}')
        for i in range(120):
            await row(pool, E, "memory_recall", "inference", 48 - i * 0.3)

        print("F. failed=true")
        got = await inf.get_background_activity(limit=100, hours=24, failed=True)
        ids = [r["id"] for r in got["activity"]]
        check("F a failure 120 calls back is listed, and only it", ids == [buried], ids)
        first = (got["activity"] or [{}])[0]
        check("F as a failure, with its error", first.get("ok") is False
              and "invalid x-api-key" in (first.get("error") or ""), first)
        check("F issues counts the window's failures (not the chat's, another Host's, or older)",
              got["issues"] == 1, got["issues"])
        newer = await row(pool, E, "web_search", "inference_error", 1, "anthropic 529: overloaded")
        got = await inf.get_background_activity(limit=100, hours=24, failed=True)
        check("F newest first", [r["id"] for r in got["activity"]] == [newer, buried],
              [r["id"] for r in got["activity"]])
        got = await inf.get_background_activity(limit=1, hours=24, failed=True)
        check("F limit still caps it", [r["id"] for r in got["activity"]] == [newer] and got["issues"] == 2,
              (got["activity"], got["issues"]))

        print("D. without it, as before")
        got = await inf.get_background_activity(limit=100, hours=24)
        rows = got["activity"]
        check("D the newest 100 calls of every kind", len(rows) == 100 and rows[0]["id"] == newer
              and any(r["ok"] for r in rows), (len(rows), rows[:1]))
        check("D the buried failure is not among them (why the option exists)",
              buried not in [r["id"] for r in rows])
        check("D issues counts every failure in the window", got["issues"] == 2, got["issues"])
        check("D the same keys as before", all(set(r) == KEYS for r in rows) and set(got) ==
              {"activity", "issues", "window_hours"}, (set(rows[0]) ^ KEYS, set(got)))

        print("$. what each call cost")
        priced = await row(pool, E, "training_memory", "inference", 0.5, cost=(0.004213, "list"))
        flat = await row(pool, E, "web_search", "inference", 0.4, cost=(0.0, "subscription"))
        got = {r["id"]: r for r in (await inf.get_background_activity(limit=5, hours=24))["activity"]}
        check("$ a call priced at list rates shows what it cost",
              got.get(priced, {}).get("cost_usd") == 0.004213 and got[priced]["cost_basis"] == "list",
              got.get(priced))
        check("$ a subscription call shows no dollar figure, and says why",
              got.get(flat, {}).get("cost_usd") is None and got[flat]["cost_basis"] == "subscription",
              got.get(flat))
        ids = [r["id"] for r in (await inf.get_background_activity(limit=500, hours=24))["activity"]]
        check("$ the Host's own turn calls are left out by default", chat not in ids)
        ids = [r["id"] for r in (await inf.get_background_activity(limit=500, hours=24,
                                                                  include_turns=True))["activity"]]
        check("$ include_turns lists them too", chat in ids and priced in ids)
    finally:
        _bs._pool, _bs._entity_id = saved
        await pool.close()
    return 1 if FAILED else 0


def route_contract():
    from server.routes import inference as inf
    p = inspect.signature(inf.get_background_activity).parameters.get("failed")
    check("C the route takes failed, false when absent", p is not None and p.default is False, p)
    p = inspect.signature(inf.get_background_activity).parameters.get("include_turns")
    check("C ...and include_turns, false when absent", p is not None and p.default is False, p)


route_contract()
with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=E) as _db:
    rc = asyncio.run(run(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else rc)
