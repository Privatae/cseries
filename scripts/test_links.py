"""The `links` tool, end to end against a real PostgreSQL.

Runs on a fresh clone of the KERNEL's schema (scripts/kernel_schema.py —
entrypoint.sh's procedure, every migration; entity_links is v115's, as on a
host) and drives runtime/link_ops through every op. The test server is
TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else a throwaway cseries-postgres; with
neither it exits 2 and says so — a skip proves nothing.

It used to DROP entity_links on whatever database TEST_PG_DSN — or, failing
that, DATABASE_URL, which inside a Host's pod is that Host's own database —
named, rebuild it from v115 alone and drop it again afterwards.
scripts/test_no_hand_kernel_tables.py now refuses that shape.

link_ops wraps whatever pool it is handed in SandboxedPool itself, so the real
capability wall is exercised here — which is how the first run caught
entity_links missing from sql_proxy.WRITE_MEMORY_TABLES.
"""
import asyncio, contextlib, os, subprocess, sys, pathlib
# The tree this file lives in (APP_ROOT to point elsewhere) — it used to read
# a fixed shared checkout, so it never tested the branch under review.
ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg
from mastercode.runtime import link_ops
from kernel_schema import kernel_schema_db, throwaway_server

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the SQL and the capability wall.")
    sys.exit(2)

class RawPool:
    """A bare connection shim. link_ops wraps whatever it is given in
    SandboxedPool itself, so the real capability proxy IS exercised here —
    that is how the first run caught entity_links missing from the allowlist."""
    def __init__(self, c): self.c = c
    async def fetch(self, s, *a): return await self.c.fetch(s, *a)
    async def fetchrow(self, s, *a): return await self.c.fetchrow(s, *a)
    async def fetchval(self, s, *a): return await self.c.fetchval(s, *a)
    async def execute(self, s, *a): return await self.c.execute(s, *a)

FAILED = 0
def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond: FAILED += 1

async def main(dsn):
    conn = await asyncpg.connect(dsn)
    p = RawPool(conn)
    E, OTHER = 4242, 9999
    try:
        r = await link_ops.run_link_op(p, E, {"action":"save","url":"HTTPS://Example.COM/Docs/Guide?a=1#frag",
                                              "title":"Guide","note":"the setup steps","tags":["Docs"," setup"],"source":"browser"})
        check("save returns ok", r.get("status")=="ok", r)
        check("save reports created", r.get("created") is True, r)
        check("scheme+host lowercased, PATH preserved, fragment dropped",
              r["url"]=="https://example.com/Docs/Guide?a=1", r.get("url"))
        check("tags cleaned + lowercased", r["tags"]==["docs","setup"], r.get("tags"))
        lid = r["id"]

        r2 = await link_ops.run_link_op(p, E, {"action":"save","url":"https://example.com/Docs/Guide?a=1"})
        check("re-save UPDATES, does not duplicate", r2["id"]==lid and r2["created"] is False, r2)
        check("bare re-save KEEPS the existing note", r2["note"]=="the setup steps", r2.get("note"))
        check("bare re-save KEEPS the existing tags", r2["tags"]==["docs","setup"], r2.get("tags"))

        r3 = await link_ops.run_link_op(p, E, {"action":"save","url":"javascript:alert(1)"})
        check("javascript: URL refused", r3["status"]=="error" and "payload" in r3["result"], r3)
        r4 = await link_ops.run_link_op(p, E, {"action":"save","url":"data:text/html,<b>x"})
        check("data: URL refused", r4["status"]=="error", r4)

        await link_ops.run_link_op(p, OTHER, {"action":"save","url":"https://other.example/secret","note":"the setup steps"})
        lst = await link_ops.run_link_op(p, E, {"action":"list"})
        check("list is entity-scoped", lst["count"]==1 and lst["links"][0]["id"]==lid, lst)
        srch = await link_ops.run_link_op(p, E, {"action":"search","query":"setup steps"})
        check("search finds it by the note", srch["count"]==1, srch)
        check("search is entity-scoped (other host's identical note absent)",
              all(l["id"]==lid for l in srch["links"]), srch)
        tagged = await link_ops.run_link_op(p, E, {"action":"list","tags":["docs"]})
        check("tag filter hits", tagged["count"]==1, tagged)
        missed = await link_ops.run_link_op(p, E, {"action":"list","tags":["nope"]})
        check("tag filter misses cleanly", missed["count"]==0, missed)
        g = await link_ops.run_link_op(p, E, {"action":"get","url":"https://EXAMPLE.com/Docs/Guide?a=1"})
        check("get by url normalizes too", g.get("id")==lid, g)

        punct = await link_ops.run_link_op(p, E, {"action":"search","query":"what's this? (setup!)"})
        check("punctuation in a query does not raise", punct["status"]=="ok", punct)

        cross = await link_ops.run_link_op(p, E, {"action":"remove","id": (await conn.fetchval(
            "SELECT id FROM entity_links WHERE entity_id=$1", OTHER))})
        check("cannot remove ANOTHER host's link", cross["status"]=="error", cross)
        nobulk = await link_ops.run_link_op(p, E, {"action":"remove"})
        check("remove without an id refuses", nobulk["status"]=="error" and "no bulk remove" in nobulk["result"], nobulk)
        rm = await link_ops.run_link_op(p, E, {"action":"remove","id":lid})
        check("remove returns what it deleted", rm["status"]=="ok" and rm["removed"]["id"]==lid, rm)
        check("it is gone", (await link_ops.run_link_op(p, E, {"action":"list"}))["count"]==0)
        check("the other host's link SURVIVED",
              await conn.fetchval("SELECT count(*) FROM entity_links WHERE entity_id=$1", OTHER) == 1)

        bad = await link_ops.run_link_op(p, E, {"action":"nuke"})
        check("unknown action refused", bad["status"]=="error", bad)
    finally:
        await conn.close()
    print()
    print("FAILED" if FAILED else "PASS"); return 1 if FAILED else 0

with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, kernel_schema_db(_srv) as _db:
    _rc = asyncio.run(main(_db))
sys.exit(_rc)
