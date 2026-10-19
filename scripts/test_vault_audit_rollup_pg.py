#!/usr/bin/env python3
"""Gate: the credential audit rollup against a real Postgres — routine reads
older than 30 days become daily counts, and nothing else is touched.

vault_audit had no bound (measured on one Host: 80,759 rows, 19 MB
in about three months, nearly all get_envelope reads by the kernel's lanes), and a
messaging connector adds a get_envelope and a use:http_call row per platform
poll. The rule for the CEIGAS audit applies:
server/vault/audit_rollup.rollup_vault_reads turns routine credential reads
older than 30 days into daily counts in vault_audit_rollup (migrations/v205)
and deletes their raw rows in the same statement. Against the real tables
(init-db.sql's vault_audit and the v205 migration, read from the repo):

  - totals are conserved exactly: rows still raw + sum(rollup.count) == the
    rows there were, after every run;
  - what rolls up: get_envelope by a kernel lane, and use:http_call that came
    back 2xx or 3xx — a READ (GET/HEAD) by a kernel lane, or any call by a
    messaging connector (its poll is a POST; its sends are in
    connector_messages);
  - a successful WRITE stays raw (the rule rolls up
    reads only): the Host's own POST through keys_manage, a RunPod job it
    started, another Host's POST through a slip;
  - nothing else is touched — put, revoke, deactivate, activate,
    add_recipient, a use that failed (4xx, 5xx, or no status: it raised),
    use:sign, use:browser_type, a use row with no detail, every row a person
    made through the Keys app (a reveal among them), and recent reads all keep
    their exact ids;
  - the grain: one row per UTC day, actor, op, owner, name and — for a use —
    HTTP method, host and status, with first/last seen right;
  - a crash mid-run (a statement that fails between counting and deleting)
    loses nothing and counts nothing twice, and the re-run finishes; a second
    full run moves nothing;
  - 12,000 old reads roll up in one call (batches of 5,000); no pool does
    nothing;
  - the hourly loop runs it, and a failing cycle never stops the loop;
  - add_break_glass, the one write that left only a get_envelope row, now
    writes its own add_recipient row;
  - MUTATION: with use:browser_type among the rolled ops the untouched check
    fails.

SKIPS CLEANLY when TEST_DATABASE_URL is unset (the pattern of
scripts/test_ceigas_audit_pg.py). Works in a throwaway schema it drops after.

    TEST_DATABASE_URL=postgresql:///db?host=/sock APP_ROOT=<repo> python3 scripts/test_vault_audit_rollup_pg.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

DSN = os.environ.get("TEST_DATABASE_URL")
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"\n         {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def table_ddl() -> list[str]:
    """init-db.sql's vault_audit and its index, then the v205 rollup
    migration — the statements a Host's schema is made of, read from the repo."""
    sql = (ROOT / "init-db.sql").read_text()
    m = re.search(r"CREATE TABLE IF NOT EXISTS vault_audit \(.*?\n\);", sql, re.S)
    if not m:
        raise SystemExit("FAIL  init-db.sql no longer creates vault_audit the way this reads it")
    out = [m.group(0)]
    out += re.findall(r"CREATE INDEX IF NOT EXISTS idx_vault_audit_\w+\s+ON vault_audit \([^;]*;", sql)
    out.append((ROOT / "migrations" / "v205_vault_audit_rollup.sql").read_text())
    return out


HOST = "host:3"
PERSON = "user:alex@example.com ip:203.0.113.9"


async def wiring_checks(dcm) -> None:
    """The loop, with a recording pool: no database needed."""
    class _Pool:
        def __init__(self, fail_first=False):
            self.calls, self.fail_first = [], fail_first

        async def fetchrow(self, sql, *args):
            self.calls.append(sql)
            if self.fail_first and len(self.calls) == 1:
                raise RuntimeError("the first cycle failed")
            if len(self.calls) > 2:
                raise asyncio.CancelledError()
            return {"moved": 0}

    p = _Pool()
    await asyncio.wait_for(dcm.run_vault_audit_rollup_loop(p, first_delay_s=0, every_s=0), 5)
    check("the hourly loop runs the vault rollup as ONE statement per batch",
          len(p.calls) >= 2 and "DELETE FROM vault_audit" in p.calls[0]
          and "INSERT INTO vault_audit_rollup" in p.calls[0] and p.calls[0].count(";") == 0,
          str([c[:60] for c in p.calls]))
    p = _Pool(fail_first=True)
    await asyncio.wait_for(dcm.run_vault_audit_rollup_loop(p, first_delay_s=0, every_s=0), 5)
    check("a failing cycle does not stop the loop", len(p.calls) >= 2 and "DELETE FROM vault_audit" in p.calls[1])
    src_main = (ROOT / "server" / "main.py").read_text()
    check("server/main.py starts the loop once the Host's database is up",
          "run_vault_audit_rollup_loop(brain_pool)" in src_main)
    check("no pool: nothing to do", await dcm.rollup_vault_reads(None) == 0)
    check("30 days and 5,000 a batch; the rolled ops are exactly "
          "get_envelope and use:http_call",
          dcm.AUDIT_ROLLUP_AFTER_DAYS == 30 and dcm.AUDIT_ROLLUP_BATCH == 5000
          and dcm.VAULT_ROLLUP_READ_OPS == ("get_envelope",)
          and dcm.VAULT_ROLLUP_USE_OPS == ("use:http_call",)
          and getattr(dcm, "VAULT_ROLLUP_READ_METHODS", None) == ("GET", "HEAD")
          and getattr(dcm, "VAULT_ROLLUP_POLL_ACTOR", None) == r"^connector:")
    src = (ROOT / "server" / "vault" / "api.py").read_text()
    body = src[src.find("async def add_break_glass"):]
    body = body[:body.find("\nasync def ", 10)] if "\nasync def " in body[10:] else body
    check("add_break_glass writes its own add_recipient row, in the transaction that changes the key",
          "conn.transaction()" in body and '"add_recipient"' in body and "store._audit(" in body)


async def main() -> int:
    from server.vault import audit_rollup as dcm
    await wiring_checks(dcm)
    if not DSN:
        print("SKIP  TEST_DATABASE_URL is unset — no Postgres to run the rollup against")
        return 1 if failures else 0
    import asyncpg

    schema = f"vault_audit_rollup_{os.getpid()}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2,
                                     server_settings={"search_path": schema, "TimeZone": "UTC"})
    try:
        for stmt in table_ddl():
            await pool.execute(stmt)

        async def add(n, op, days, *, actor=HOST, name="telegram_token", owner=HOST, detail=None,
                      raw_detail=False, hours=0):
            await pool.executemany(
                "INSERT INTO vault_audit (at, actor, op, owner_identity, logical_name, detail) "
                "VALUES (date_trunc('day', NOW()) - make_interval(days => $1) + make_interval(hours => $2), "
                "$3, $4, $5, $6, $7::jsonb)",
                [(days, hours, actor, op, owner, name,
                  detail if raw_detail else json.dumps(detail or {}))] * n)

        def call(method="POST", status=200, host="api.telegram.org"):
            d = {"action": "http_call", "method": method, "host": host, "path_secret": True}
            if status is not None:
                d["status"] = status
            return d

        tg = "connector:telegram-connector"
        # What rolls up (old, a kernel lane, a read or a use that came back):
        await add(5, "get_envelope", 40, actor="harness:3", name="anthropic", hours=3)
        await add(2, "get_envelope", 40, actor="harness:3", name="anthropic", hours=9)
        await add(4, "get_envelope", 41, actor="harness:3", name="anthropic", hours=1)   # another day
        await add(3, "get_envelope", 40, actor="harness_keys:grok:3", name="grok", hours=2)
        await add(6, "get_envelope", 40, actor=tg, hours=4)
        await add(6, "use:http_call", 40, actor=tg, detail=call(), hours=4)
        await add(1, "use:http_call", 40, actor=tg, detail=call(status=302), hours=5)
        await add(2, "use:http_call", 40, actor="keys_manage", name="gitea", detail=call("GET", 200, "git.example"),
                  hours=6)
        await add(1, "get_envelope", 40, actor="inference", name=None, owner=None, hours=7)  # NULL name/owner
        # What must stay raw:
        await add(3, "get_envelope", 5, actor="harness:3", name="anthropic")              # recent
        await add(2, "use:http_call", 5, actor=tg, detail=call())                         # recent
        await add(1, "put", 40, actor="harness:seal-sweep:grok", name="grok")
        await add(1, "revoke", 40, name="old")
        await add(1, "deactivate", 40)
        await add(1, "activate", 40)
        await add(1, "add_recipient", 40, detail={"recipients": 2})
        await add(1, "use:http_call", 40, actor=tg, detail=call(status=401))               # refused upstream
        await add(1, "use:http_call", 40, actor=tg, detail=call(status=409))
        await add(1, "use:http_call", 40, actor=tg, detail=call(status=502))
        await add(1, "use:http_call", 40, actor=tg, detail=call(status=None))              # it raised
        await add(1, "use:http_call", 40, actor=tg, detail=None, raw_detail=True)          # no detail
        await add(1, "use:sign", 40, detail={"action": "sign", "alg": "hmac-sha256"})
        await add(1, "use:browser_type", 40, detail={"action": "browser_type", "session": "bank",
                                                     "ref": "e12", "field_type": "password"})
        await add(1, "get_envelope", 40, actor=f"{PERSON} reveal")                         # a reveal
        await add(1, "get_envelope", 40, actor=f"{PERSON} use:http_call")
        await add(1, "use:http_call", 40, actor=f"{PERSON} use:http_call", detail=call("GET", 200))
        await add(1, "get_envelope", 40, actor="user")
        # Successful WRITES by anyone but a connector stay raw:
        await add(1, "use:http_call", 40, name="webhook", detail=call("POST", 200, "hooks.example"))
        await add(1, "use:http_call", 40, name="runpod", detail=call("POST", 200, "api.runpod.ai"))
        await add(1, "use:http_call", 40, actor="peer:7", detail=call("PUT", 204, "api.example"))
        await add(1, "use:http_call", 40, name="gitea", detail=call("DELETE", 204, "git.example"))

        original = await pool.fetchval("SELECT count(*) FROM vault_audit")
        rollable_sql = (
            "SELECT count(*) FROM vault_audit WHERE at < NOW() - INTERVAL '30 days' "
            "AND actor NOT LIKE 'user:%' AND actor <> 'user' AND (op = 'get_envelope' OR "
            "(op = 'use:http_call' AND COALESCE(detail->>'status', '') IN ('200', '302') "
            "AND (detail->>'method' = 'GET' OR actor LIKE 'connector:%')))")
        original_rollable = await pool.fetchval(rollable_sql)
        keep_ids = {r["id"] for r in await pool.fetch(
            "SELECT id FROM vault_audit WHERE NOT (at < NOW() - INTERVAL '30 days' "
            "AND actor NOT LIKE 'user:%' AND actor <> 'user' AND (op = 'get_envelope' OR "
            "(op = 'use:http_call' AND COALESCE(detail->>'status', '') IN ('200', '302') "
            "AND (detail->>'method' = 'GET' OR actor LIKE 'connector:%'))))")}
        check("the fixture holds 30 rows to roll and 25 that stay",
              original_rollable == 30 and len(keep_ids) == 25 and original == 55,
              f"{original_rollable} {len(keep_ids)} {original}")

        async def conserved() -> tuple[bool, str]:
            rolled = await pool.fetchval("SELECT COALESCE(sum(count), 0) FROM vault_audit_rollup")
            raw = await pool.fetchval("SELECT count(*) FROM vault_audit")
            return rolled + raw == original, f"rolled {rolled} + raw {raw} vs {original}"

        async def untouched() -> tuple[bool, str]:
            now_ids = {r["id"] for r in await pool.fetch("SELECT id FROM vault_audit")}
            gone = keep_ids - now_ids
            return not gone, f"{len(gone)} kept row(s) gone"

        # A crash between counting and deleting: a statement that fails part
        # way. The trigger raises for one actor, whose rows are near the end of
        # the rows to roll, so the first batches of 4 commit and a later one
        # fails.
        await pool.execute("""
            CREATE FUNCTION crash_on_probe() RETURNS trigger LANGUAGE plpgsql AS $f$
            BEGIN
                IF NEW.actor = 'keys_manage' THEN
                    RAISE EXCEPTION 'probe crash between count and delete';
                END IF;
                RETURN NEW;
            END $f$""")
        await pool.execute("CREATE TRIGGER crash_on_probe BEFORE INSERT ON vault_audit_rollup "
                           "FOR EACH ROW EXECUTE FUNCTION crash_on_probe()")
        crashed = None
        try:
            await dcm.rollup_vault_reads(pool, batch=4, pause_s=0)
        except Exception as e:
            crashed = str(e)
        ok, why = await conserved()
        stuck = await pool.fetchval("SELECT count(*) FROM vault_audit WHERE actor = 'keys_manage'")
        rolled_before = await pool.fetchval("SELECT COALESCE(sum(count), 0) FROM vault_audit_rollup")
        check("a crash mid-run raises, and the failed batch is neither counted nor deleted "
              "(its raw rows are all still there)",
              crashed is not None and "probe crash" in crashed and stuck == 2 and 0 < rolled_before < 30,
              f"{crashed} {stuck} {rolled_before}")
        check("... and nothing is lost or double-counted across the crash", ok, why)
        await pool.execute("DROP TRIGGER crash_on_probe ON vault_audit_rollup")

        await dcm.rollup_vault_reads(pool, batch=4, pause_s=0)
        ok, why = await conserved()
        check("the re-run finishes the count: every rolled row is counted exactly once", ok, why)
        left = await pool.fetchval(rollable_sql)
        check("no old routine read is left raw", left == 0, str(left))
        ok, why = await untouched()
        check("everything else keeps its exact row: put, revoke, deactivate, activate, add_recipient, "
              "4xx/5xx and status-less uses, a use with no detail, use:sign, use:browser_type, a "
              "person's reveal and uses, recent reads, and every successful write but a connector's "
              "(a POST, a PUT, a DELETE)", ok, why)
        check("a second full run moves nothing", await dcm.rollup_vault_reads(pool, pause_s=0) == 0)
        ok, why = await conserved()
        check("... and the totals do not move", ok, why)

        roll = {(str(r["day"]), r["actor"], r["op"], r["owner_identity"], r["logical_name"],
                 r["http_method"], r["http_host"], r["http_status"]): r
                for r in await pool.fetch("SELECT * FROM vault_audit_rollup")}
        day40 = str(await pool.fetchval("SELECT (date_trunc('day', NOW()) - INTERVAL '40 days')::date"))
        day41 = str(await pool.fetchval("SELECT (date_trunc('day', NOW()) - INTERVAL '41 days')::date"))

        def cnt(key):
            return roll[key]["count"] if key in roll else None
        ant = (day40, "harness:3", "get_envelope", HOST, "anthropic", "", "", "")
        check("one row per actor and key per UTC day, with its count (7 on one day, 4 the day before)",
              cnt(ant) == 7
              and cnt((day41, "harness:3", "get_envelope", HOST, "anthropic", "", "", "")) == 4,
              str({k: v["count"] for k, v in roll.items() if k[1] == "harness:3"}))
        r = roll.get(ant)
        check("first_seen / last_seen are the earliest and latest raw rows",
              r is not None and r["first_seen"].hour == 3 and r["last_seen"].hour == 9, str(r))
        check("per actor: the grok key sync is its own row",
              cnt((day40, "harness_keys:grok:3", "get_envelope", HOST, "grok", "", "", "")) == 3)
        check("the connector's poll pair: its reads, and its uses by method, host and status",
              cnt((day40, tg, "get_envelope", HOST, "telegram_token", "", "", "")) == 6
              and cnt((day40, tg, "use:http_call", HOST, "telegram_token", "POST", "api.telegram.org", "200")) == 6
              and cnt((day40, tg, "use:http_call", HOST, "telegram_token", "POST", "api.telegram.org", "302")) == 1,
              str({k[2:]: v["count"] for k, v in roll.items() if k[1] == tg}))
        check("another lane's GET to another host is its own row",
              cnt((day40, "keys_manage", "use:http_call", HOST, "gitea", "GET", "git.example", "200")) == 2)
        check("a row with no owner or name rolls up under ''",
              cnt((day40, "inference", "get_envelope", "", "", "", "", "")) == 1)
        check("nothing a person did, and no failed use, is in the rollup",
              not any(k[1].startswith("user") for k in roll)
              and not any(k[7] and not k[7].startswith(("2", "3")) for k in roll)
              and not any(k[2] not in ("get_envelope", "use:http_call") for k in roll)
              and not any(k[2] == "use:http_call" and k[5] != "GET" and not k[1].startswith("connector:")
                          for k in roll),
              str(sorted({(k[1], k[2], k[7]) for k in roll})))

        # ── bulk ──
        await add(12000, "get_envelope", 45, actor="harness:3", name="anthropic")
        original += 12000
        moved = await dcm.rollup_vault_reads(pool, pause_s=0)
        ok, why = await conserved()
        check("12,000 old reads roll up in one call (batches of 5,000), totals conserved",
              moved == 12000 and ok, f"{moved} {why}")

        # ── MUTATION: a use with an effect among the rolled ops must be caught ──
        # (by a connector, so the op list is the only thing that holds it back)
        saved = dcm.VAULT_ROLLUP_USE_OPS
        await add(2, "use:browser_type", 40, actor=tg, detail={"action": "browser_type", "status": 200})
        keep_ids |= {r["id"] for r in await pool.fetch(
            "SELECT id FROM vault_audit WHERE op = 'use:browser_type'")}
        dcm.VAULT_ROLLUP_USE_OPS = saved + ("use:browser_type",)
        try:
            await dcm.rollup_vault_reads(pool, pause_s=0)
        finally:
            dcm.VAULT_ROLLUP_USE_OPS = saved
        ok, why = await untouched()
        check("MUTATION: with use:browser_type among the rolled ops, the untouched check fails (it is "
              "not vacuous)", not ok, why)

        # ── MUTATION: POST, PUT and DELETE read as reads — the writes must go ──
        write_ids = {r["id"] for r in await pool.fetch(
            "SELECT id FROM vault_audit WHERE op = 'use:http_call' AND actor NOT LIKE 'connector:%' "
            "AND detail->>'method' IN ('POST', 'PUT', 'DELETE') "
            "AND COALESCE(detail->>'status', '') ~ '^[23]' AND at < NOW() - INTERVAL '30 days'")}
        saved_m = getattr(dcm, "VAULT_ROLLUP_READ_METHODS", ())
        dcm.VAULT_ROLLUP_READ_METHODS = tuple(saved_m) + ("POST", "PUT", "DELETE")
        try:
            await dcm.rollup_vault_reads(pool, pause_s=0)
        finally:
            dcm.VAULT_ROLLUP_READ_METHODS = saved_m
        left_w = {r["id"] for r in await pool.fetch("SELECT id FROM vault_audit")} & write_ids
        check("MUTATION: with POST, PUT and DELETE counted as reads, the four successful writes roll up "
              "(the check that keeps them raw is not vacuous)", len(write_ids) == 4 and not left_w,
              f"{len(write_ids)} write rows before, {len(left_w)} left")
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()

    if failures:
        print(f"\n{len(failures)} FAILED")
        return 1
    print("\nPASS — routine credential reads roll up with every total conserved, and every other "
          "vault_audit row stays as it was")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
