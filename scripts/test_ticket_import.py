#!/usr/bin/env python3
"""Tickets: one system, two shapes — slice 3, the ticket-bundle import
(2026-09-24), on a REAL database.

runtime/ticket_import.py is mostly SQL whose correctness is a property of
Postgres (jsonb_set on projects.config, the phase-number subselect inside a
row-locked transaction, a session advisory lock, source_ref keys), so it runs
against one: in smoke via `docker exec` (the entity pod's own DB), standalone
with SMOKE_PG_* pointing at any DB carrying init-db.sql + migrations. The
bundle goes in the way a user's does — through the upload file lane
(server/routes/context._ingest_binary_file) — and the import runs through the
`tickets` tool envelope (runtime/tools._exec_tickets), not a helper.

What it pins, each by an input only that rule can move:
  - a bundle that is not gzip/JSON, not this format, not this version, whose
    sha256 does not match, or that breaks a limit is refused and writes
    NOTHING; a name nobody uploaded is refused naming what is uploaded.
  - tickets land OPEN, source 'manual', source_ref import:<bundle>:<key>,
    requester kind 'import', user_only (the Host's model can neither decide
    nor dismiss one; the person can), with exactly the bundle's options; a
    credential ask naming a key the vault holds (stored here first) is
    refused for that ticket alone and the rest still land; a NUL anywhere in
    the bundle refuses the whole bundle before anything is written.
  - projects are created once, keyed in config.import, with their directory
    and repo set up like every other creation path; phases are 'planning',
    ungated, no dependencies — nothing the scheduler can start.
  - archive sections land as corpus rows under the bundle's filename,
    source 'import', unembedded, no embed job queued (embed_jobs counted
    before and after); a long section is split on lines; a section cut short
    is written again whole by the next import.
  - importing again creates nothing; a newer export with the same bundle id
    adds only the new items.
  - a dispatched agent is refused THROUGH THE DISPATCH (_execute_meta_tool
    with a grant, which is how scoped_agent calls it); a Host without the
    bits to write projects is refused; a second import while one runs is
    refused.

Run: SMOKE_PG_HOST=<host or socket dir> [SMOKE_PG_PORT/USER/PASSWORD/DB] \\
     APP_ROOT=<repo> python3 scripts/test_ticket_import.py
Prints OK <n> checks on success (smoke reads the last line).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

FAILS: list = []
PASSED = 0


def check(label, cond, detail=""):
    global PASSED
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if cond:
        PASSED += 1
    else:
        FAILS.append(label)


BID = "gate-bundle"


def bundle(**over) -> dict:
    b = {
        "format": "privatae.tickets-bundle", "version": 1, "bundle_id": BID,
        "source": "gate ledger", "created_at": "2026-09-24T00:00:00+00:00",
        "tickets": [
            {"key": "T-1", "sitrep": "Pick one.", "priority": 70, "project": "Gate train",
             "options": [{"id": "a", "label": "A", "consequence": "does a", "recommend": True},
                         {"id": "b", "label": "B"}]},
            {"key": "T-2", "sitrep": "Only you can do this.",
             "options": [{"id": "done", "label": "Done"}]},
            {"key": "T-3", "sitrep": "Hand over a key.",
             "options": [{"id": "store-key", "label": "Store it",
                          "credential_request": {"name": "gate_import_new_key",
                                                 "cred_type": "opaque", "purpose": "gate"}},
                         {"id": "done", "label": "Done without a key"}]},
        ],
        "projects": [
            {"key": "train-1", "name": "Gate train", "description": "about the train",
             "phases": [{"key": "T-10", "title": "T-10 — build x", "spec": "build x"},
                        {"key": "T-11", "title": "T-11 — build y", "spec": "build y"}]},
        ],
        "archive": {"filename": "gate-ledger-archive.md", "sections": [
            {"key": "index", "title": "Where the open items went", "text": "# index\n- T-1\n"},
            {"key": "T-1", "title": "T-1 — pick", "text": "# T-1\n" + ("line of history\n" * 900)},
        ]},
    }
    b.update(over)
    return b


def seal(b: dict) -> bytes:
    from mastercode.runtime import ticket_import as ti
    b = dict(b)
    b["sha256"] = ti.digest(b)
    return gzip.compress(json.dumps(b).encode())


def pure() -> None:
    from mastercode.runtime import ticket_import as ti
    print("== the bundle checks (no DB) ==")
    good = seal(bundle())
    check("a sealed bundle decodes", ti.decode(good)["bundle_id"] == BID)
    check("plain (uncompressed) JSON decodes too",
          ti.decode(gzip.decompress(good))["bundle_id"] == BID)
    raw = json.loads(gzip.decompress(good))
    raw["tickets"][0]["sitrep"] = "Pick two."
    for label, blob, want in [
        ("an edited bundle (sha256 mismatch) is refused", json.dumps(raw).encode(), "sha256"),
        ("not JSON is refused", b"\x00garbage", "not JSON"),
        ("a truncated gzip is refused", good[: len(good) // 2], "gzip"),
        ("another format is refused", seal(bundle(format="x")), "format"),
        ("another version is refused", seal(bundle(version=2)), "version"),
    ]:
        try:
            ti.decode(blob)
            check(label, False, "decoded")
        except ti.BundleError as e:
            check(label, want in str(e), e)
    bad = []
    b = bundle(); b["tickets"][1]["key"] = "T-1"; bad.append(("a duplicate ticket key", b, "twice"))
    b = bundle(); b["tickets"][0]["options"][0]["value"] = "x"; bad.append(("an option field no ticket has", b, "unknown field"))
    b = bundle(); b["tickets"][0]["options"] = [{"id": f"o{i}", "label": "x"} for i in range(7)]; bad.append(("seven options", b, "1..6"))
    b = bundle(); b["tickets"][0]["priority"] = 101; bad.append(("priority 101", b, "0..100"))
    b = bundle(); b["tickets"][0]["priority"] = True; bad.append(("priority true", b, "0..100"))
    b = bundle(); b["tickets"][0]["options"][1]["id"] = "A B"; bad.append(("an option id with a space", b, "unique id"))
    b = bundle(bundle_id="has space"); bad.append(("a bundle id with a space", b, "bundle_id"))
    b = bundle(); b["archive"]["filename"] = "../x.md"; bad.append(("an archive path", b, "plain file name"))
    b = bundle(); b["projects"][0]["phases"][1]["key"] = "T-10"; bad.append(("a duplicate phase key", b, "twice"))
    b = bundle(); b["tickets"][0]["sitrep"] = "x" * 12001; bad.append(("a sitrep over 12,000", b, "longer than"))
    b = bundle(bundle_id="a:b"); bad.append(("a ':' in the bundle id (source_ref must split one way)", b, "bundle_id"))
    b = bundle(); b["tickets"][1]["sitrep"] = "ok\x00then"; bad.append(("a NUL in a sitrep", b, "NUL"))
    b = bundle(); b["tickets"][0]["options"][1]["label"] = "B\x00"; bad.append(("a NUL in an option label", b, "NUL"))
    b = bundle(); b["archive"]["sections"][1]["text"] += "\x00"; bad.append(("a NUL in an archive section", b, "NUL"))
    for label, b, want in bad:
        try:
            ti.decode(seal(b))
            check(f"{label} is refused", False, "decoded")
        except ti.BundleError as e:
            check(f"{label} is refused", want in str(e), e)
    text = "short\n" + ("y" * 20000) + "\n" + "tail line\n" * 3000
    ps = ti.pieces(text)
    check("pieces join back to the text exactly", "".join(ps) == text)
    check("every piece is within the cap", all(len(p) <= ti.PIECE_CHARS for p in ps), [len(p) for p in ps])
    check("a short section is one piece", ti.pieces("abc") == ["abc"])


async def db() -> None:
    import asyncpg
    from server import brain_service
    from server.routes import context as ctx
    from server.routes import tickets as T
    from mastercode.runtime import tools as tools
    from mastercode.runtime import ticket_import as ti

    pool = await asyncpg.create_pool(
        host=os.environ.get("SMOKE_PG_HOST", "/var/run/postgresql"),
        port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
        user=os.environ.get("SMOKE_PG_USER", "entity"),
        password=os.environ.get("SMOKE_PG_PASSWORD") or None,
        database=os.environ.get("SMOKE_PG_DB", "hostmem"),
        min_size=2, max_size=4)
    # Not the lowest id: v105 seeds entity 0 ('Platform') on every Host, and it
    # holds no capability rows — the Host is the other one.
    eid = await pool.fetchval("SELECT id FROM entity_registry WHERE id <> 0 ORDER BY id LIMIT 1")
    import tempfile
    from pathlib import Path
    from server.routes import projects as P
    saved = (brain_service._pool, brain_service._entity_id, P.PROJECT_FILES_ROOT, P.ensure_project_repo)
    brain_service._pool, brain_service._entity_id = pool, eid
    P.PROJECT_FILES_ROOT = Path(tempfile.mkdtemp(prefix="ticket-import-gate-"))
    repos: list = []

    async def _repo(pool_, project_id, name):
        repos.append((project_id, name))
        return {"repo": None, "reason": "gate: no gitea"}
    P.ensure_project_repo = _repo

    async def wipe():
        await pool.execute("DELETE FROM entity_tickets WHERE entity_id = $1 AND source_ref LIKE $2",
                           eid, f"import:{BID}:%")
        await pool.execute("DELETE FROM projects WHERE entity_id = $1 AND config->'import'->>'bundle_id' = $2",
                           eid, BID)
        await pool.execute("DELETE FROM user_context WHERE entity_id = $1 AND (metadata->>'import_bundle' = $2 "
                           "OR metadata->>'from_file' LIKE 'gate-import-%')", eid, BID)

    async def upload(name: str, blob: bytes):
        import uuid
        return await ctx._ingest_binary_file(pool, eid, blob, name, "", [], uuid.uuid4().hex)

    async def counts():
        return (await pool.fetchval("SELECT count(*) FROM entity_tickets WHERE entity_id=$1 AND source_ref LIKE $2",
                                    eid, f"import:{BID}:%"),
                await pool.fetchval("SELECT count(*) FROM projects WHERE entity_id=$1 AND config->'import'->>'bundle_id'=$2",
                                    eid, BID),
                await pool.fetchval("""SELECT count(*) FROM project_phases ph JOIN projects p ON p.id = ph.project_id
                                        WHERE p.entity_id=$1 AND p.config->'import'->>'bundle_id'=$2""", eid, BID),
                await pool.fetchval("SELECT count(*) FROM user_context WHERE entity_id=$1 AND metadata->>'import_bundle'=$2",
                                    eid, BID))

    async def imp(fname, grant=None):
        return await tools._exec_tickets({"op": "import", "file": fname}, agent_grant=grant)

    try:
        await wipe()
        print("== refusals write nothing ==")
        await upload("gate-import-good.tickets.gz", seal(bundle()))
        r = await tools._execute_meta_tool("tickets", {"op": "import", "file": "gate-import-good.tickets.gz"},
                                           eid, pool, grant_bits=0xFF)
        check("a dispatched agent is refused through the dispatch (grant carries every bit)",
              r.get("status") == "error" and "agent" in str(r.get("result")), r)
        check("...and nothing was written", await counts() == (0, 0, 0, 0), await counts())
        raw = json.loads(gzip.decompress(seal(bundle())))
        raw["tickets"][0]["sitrep"] = "edited"
        await upload("gate-import-edited.tickets.gz", json.dumps(raw).encode())
        r = await imp("gate-import-edited.tickets.gz")
        check("an edited bundle is refused on its sha256", r["status"] == "error" and "sha256" in r["result"], r)
        check("...and nothing was written", await counts() == (0, 0, 0, 0), await counts())
        r = await imp("gate-import-nobody-uploaded-this.gz")
        check("an unknown name is refused naming what is uploaded",
              r["status"] == "error" and "gate-import-good.tickets.gz" in r["result"], r)
        lock = await pool.acquire()
        await lock.execute("SELECT pg_advisory_lock($1, $2)", ti._LOCK_NS, eid)
        r = await imp("gate-import-good.tickets.gz")
        await lock.execute("SELECT pg_advisory_unlock($1, $2)", ti._LOCK_NS, eid)
        await pool.release(lock)
        check("a second import while one runs is refused", r["status"] == "error" and "already running" in r["result"], r)
        check("...and nothing was written", await counts() == (0, 0, 0, 0), await counts())

        nb = bundle()
        nb["tickets"][2]["sitrep"] = "late NUL\x00"
        await upload("gate-import-nul.tickets.gz", seal(nb))
        r = await imp("gate-import-nul.tickets.gz")
        check("a NUL anywhere refuses the bundle", r["status"] == "error" and "NUL" in r["result"], r)
        check("...and nothing was written", await counts() == (0, 0, 0, 0), await counts())
        acc = await pool.fetch("SELECT domain_id, access_level FROM entity_domain_access WHERE entity_id=$1", eid)
        # Bits are a union over domains (core.memory admin alone grants writes),
        # so a Host that cannot write projects is one read-only everywhere.
        await pool.execute("UPDATE entity_domain_access SET access_level='read' WHERE entity_id=$1", eid)
        try:
            r = await imp("gate-import-good.tickets.gz")
        finally:
            for a_ in acc:
                await pool.execute("UPDATE entity_domain_access SET access_level=$3 WHERE entity_id=$1 "
                                   "AND domain_id=$2", eid, a_["domain_id"], a_["access_level"])
        check("a Host that cannot write projects is refused",
              r["status"] == "error" and "capability" in r["result"], (r, [dict(a_) for a_ in acc]))
        check("...and nothing was written", await counts() == (0, 0, 0, 0), await counts())

        print("== the import ==")
        jobs_before = await pool.fetchval("SELECT count(*) FROM embed_jobs WHERE entity_id=$1", eid)
        r = await imp("gate-import-good.tickets.gz")
        check("the import succeeds", r["status"] == "ok", r)
        res = r.get("result") or {}
        check("it reports 3 tickets, 1 project, 2 phases created",
              (res.get("tickets", {}).get("created"), res.get("projects", {}).get("created"),
               res.get("phases", {}).get("created")) == (3, 1, 2), res)
        rows = {r_["source_ref"]: dict(r_) for r_ in await pool.fetch(
            "SELECT * FROM entity_tickets WHERE entity_id=$1 AND source_ref LIKE $2", eid, f"import:{BID}:%")}
        t1 = rows.get(f"import:{BID}:T-1") or {}
        md = json.loads(t1.get("metadata") or "{}")
        check("T-1 is an open ticket from 'manual'", (t1.get("status"), t1.get("source")) == ("open", "manual"), t1)
        check("T-1 is user_only and names its bundle key",
              md.get("user_only") is True and md.get("imported") == {"bundle_id": BID, "key": "T-1"}, md)
        check("T-1 carries no platform lock the bundle did not earn",
              "kind" not in md and "no_pattern" not in md, md)
        check("T-1's requester is the import", json.loads(t1.get("requester") or "{}").get("kind") == "import")
        check("T-1's options are the bundle's", json.loads(t1.get("options") or "[]") ==
              [{"id": "a", "label": "A", "consequence": "does a", "recommend": True}, {"id": "b", "label": "B"}])
        check("T-1's priority and project travel", t1.get("priority") == 70 and
              json.loads(t1.get("context") or "{}").get("project") == "Gate train", t1)
        t3 = rows.get(f"import:{BID}:T-3") or {}
        check("T-3's credential ask is on its option",
              json.loads(t3.get("options") or "[]")[0].get("credential_request", {}).get("name") == "gate_import_new_key")
        try:
            await T.decide_ticket_service(pool, eid, t1["id"], option_id="a", actor="host")
            check("the Host's model cannot decide an imported ticket", False, "decided")
        except T.TicketError as e:
            check("the Host's model cannot decide an imported ticket", e.status == 403, e.detail)
        try:
            await T.dismiss_ticket_service(pool, eid, t1["id"], actor="host")
            check("...nor dismiss one", False, "dismissed")
        except T.TicketError as e:
            check("...nor dismiss one", e.status == 403, e.detail)
        try:
            d = await T.decide_ticket_service(pool, eid, t1["id"], option_id="b", note="mine", actor="user")
            check("the person can decide it", d["decision"]["option_id"] == "b")
        except T.TicketError as e:
            check("the person can decide it", False, e.detail)

        proj = await pool.fetchrow("SELECT * FROM projects WHERE entity_id=$1 AND config->'import'->>'bundle_id'=$2",
                                   eid, BID)
        cfg = json.loads(proj["config"])
        check("the project is named and active", (proj["name"], proj["status"]) == ("Gate train", "active"))
        check("the project records its phases by key", sorted(cfg["import"]["phases"]) == ["T-10", "T-11"], cfg)
        check("the project's directory exists (compare/adopt/sync read it)",
              (P.PROJECT_FILES_ROOT / str(proj["id"])).is_dir())
        check("the project's repo was asked for, once, by name", repos == [(proj["id"], "Gate train")], repos)
        phs = await pool.fetch("SELECT * FROM project_phases WHERE project_id=$1 ORDER BY phase_number", proj["id"])
        check("both phases are 'planning', ungated, waiting for nothing",
              [(p["status"], p["gate"], p["depends_on"]) for p in phs] == [("planning", "none", [])] * 2,
              [(p["status"], p["gate"], p["depends_on"]) for p in phs])
        check("phase numbers are 1, 2 with the bundle's titles",
              [(p["phase_number"], p["title"]) for p in phs] == [(1, "T-10 — build x"), (2, "T-11 — build y")])
        check("the phase ids recorded are the rows'", sorted(cfg["import"]["phases"].values()) == sorted(p["id"] for p in phs))

        arch = await pool.fetch("""SELECT * FROM user_context WHERE entity_id=$1 AND metadata->>'import_bundle'=$2
                                    ORDER BY id""", eid, BID)
        mds = [json.loads(a["metadata"]) for a in arch]
        check("archive rows are filed under the bundle's filename",
              arch and all(m["from_file"] == "gate-ledger-archive.md" for m in mds))
        check("...from source 'import', live ('raw')", all((a["source"], a["status"]) == ("import", "raw") for a in arch))
        check("...and unembedded", all(a["embedding"] is None and a["embedding_768"] is None for a in arch))
        jobs = await pool.fetchval("SELECT count(*) FROM embed_jobs WHERE entity_id=$1", eid)
        check("...with no embed job queued by the import", jobs == jobs_before, (jobs_before, jobs))
        t1_rows = [a for a, m in zip(arch, mds) if m["import_key"] == "T-1"]
        # stage_context_service strips each row (context.py), so a piece's
        # closing line break is the row boundary; the words are all there, in order.
        want = [p.strip() for p in ti.pieces(bundle()["archive"]["sections"][1]["text"])]
        check("the long section is split into its pieces, in order",
              len(t1_rows) > 1 and [a["content"] for a in t1_rows] == want, (len(t1_rows), len(want)))

        print("== again, and a newer export ==")
        before = await counts()
        r = await imp("gate-import-good.tickets.gz")
        res = r.get("result") or {}
        check("importing again creates nothing", await counts() == before and
              (res["tickets"]["created"], res["phases"]["created"], res["projects"]["created"], res["archive"]["rows"])
              == (0, 0, 0, 0), res)
        check("...and reports every item present",
              (res["tickets"]["present"], res["phases"]["present"], res["archive"]["present"]) == (3, 2, 2), res)
        nb = bundle()
        nb["tickets"].append({"key": "T-4", "sitrep": "new", "options": [{"id": "done", "label": "Done"}]})
        nb["projects"][0]["phases"].append({"key": "T-12", "title": "T-12 — build z", "spec": "z"})
        nb["archive"]["sections"].append({"key": "T-4", "title": "T-4", "text": "# T-4\n"})
        await upload("gate-import-good.tickets.gz", seal(nb))
        r = await imp("gate-import-good.tickets.gz")
        res = r.get("result") or {}
        check("a newer export adds only its new items",
              (res["tickets"]["created"], res["phases"]["created"], res["projects"]["created"], res["archive"]["rows"])
              == (1, 1, 0, 1), res)
        ph3 = await pool.fetchrow("SELECT * FROM project_phases WHERE project_id=$1 AND title='T-12 — build z'", proj["id"])
        check("the new phase is numbered after the others", ph3 and ph3["phase_number"] == 3, ph3)

        print("== a section cut short is written again whole ==")
        cut = await pool.fetchval("""SELECT id FROM user_context WHERE entity_id=$1 AND metadata->>'import_bundle'=$2
                                      AND metadata->>'import_key'='T-1' AND status='raw'
                                    ORDER BY (metadata->>'chunk')::int DESC LIMIT 1""", eid, BID)
        await pool.execute("UPDATE user_context SET status='dismissed' WHERE id=$1", cut)
        r = await imp("gate-import-good.tickets.gz")
        res = r.get("result") or {}
        live = [x["content"] for x in await pool.fetch(
            """SELECT content FROM user_context WHERE entity_id=$1 AND metadata->>'import_bundle'=$2
                AND metadata->>'import_key'='T-1' AND status='raw' ORDER BY (metadata->>'chunk')::int""", eid, BID)]
        want = [p.strip() for p in ti.pieces(bundle()["archive"]["sections"][1]["text"])]
        check("the cut-short section is rewritten whole, once", live == want and not res.get("errors"),
              (len(live), len(want), res.get("errors")))

        print("== a promoted piece survives a repair ==")
        rows = await pool.fetch("""SELECT id, (metadata->>'chunk')::int AS n FROM user_context
                                    WHERE entity_id=$1 AND metadata->>'import_bundle'=$2
                                      AND metadata->>'import_key'='T-1' AND status='raw'
                                    ORDER BY (metadata->>'chunk')::int""", eid, BID)
        await pool.execute("UPDATE user_context SET status='promoted' WHERE id=$1", rows[0]["id"])
        await pool.execute("UPDATE user_context SET status='dismissed' WHERE id=$1", rows[-1]["id"])
        r = await imp("gate-import-good.tickets.gz")
        res = r.get("result") or {}
        live = await pool.fetch("""SELECT id, status, (metadata->>'chunk')::int AS n FROM user_context
                                    WHERE entity_id=$1 AND metadata->>'import_bundle'=$2
                                      AND metadata->>'import_key'='T-1' AND status <> 'dismissed'
                                    ORDER BY (metadata->>'chunk')::int""", eid, BID)
        check("the promoted piece is kept as it was, not copied",
              [x["n"] for x in live] == list(range(len(want)))
              and [x["id"] for x in live if x["status"] == "promoted"] == [rows[0]["id"]]
              and not res.get("errors"), ([(x["n"], x["status"]) for x in live], res.get("errors")))
        r = await imp("gate-import-good.tickets.gz")
        check("...and the section then reads as present", not (r.get("result") or {}).get("errors")
              and (r.get("result") or {}).get("archive", {}).get("rows") == 0, r)

        print("== a newer export that splits a promoted-from section differently ==")

        def split_text(k: int, ch: str) -> str:
            # k pieces exactly: each line fills one piece.
            return (ch * (ti.PIECE_CHARS - 1) + "\n") * k

        async def section(key, k, ch, fname):
            sb = bundle()
            sb["tickets"], sb["projects"] = [], []
            sb["archive"]["sections"] = [{"key": key, "title": key, "text": split_text(k, ch)}]
            await upload(fname, seal(sb))
            return await imp(fname)

        async def rows_of(key):
            return await pool.fetch("""SELECT id, status, (metadata->>'chunk')::int AS n,
                                              (metadata->>'chunk_count')::int AS of, left(content, 1) AS ch,
                                              metadata->>'upload_batch' AS batch, metadata->>'from_file' AS file
                                         FROM user_context WHERE entity_id=$1 AND metadata->>'import_bundle'=$2
                                          AND metadata->>'import_key'=$3 AND status <> 'dismissed'
                                        ORDER BY of, n""", eid, BID, key)

        for key, promote, drop, k2, ch2, label in (
                ("Y-fewer", [0], [1], 2, "n", "fewer pieces"),
                ("Y-more", [0], [1], 5, "n", "more pieces"),
                ("Y-kept", [0, 1], [2], 2, "n", "every promoted piece from the old split"),
                ("Y-changed", [0], [1], 3, "n", "the same split, the text changed"),
                ("Y-same", [0, 1], [], 3, "o", "the same split and text, pieces promoted")):
            await section(key, 3, "o", f"gate-import-{key}-v1.tickets.gz")
            old = await rows_of(key)
            for i in promote:
                await pool.execute("UPDATE user_context SET status='promoted' WHERE id=$1", old[i]["id"])
            for i in drop:
                await pool.execute("UPDATE user_context SET status='dismissed' WHERE id=$1", old[i]["id"])
            r1 = await section(key, k2, ch2, f"gate-import-{key}-v2.tickets.gz")
            r2 = await section(key, k2, ch2, f"gate-import-{key}-v2.tickets.gz")
            now = await rows_of(key)
            doc = [x for x in now if x["batch"] == f"import-{BID}"]
            out_of_doc = [x for x in now if x["batch"] != f"import-{BID}"]
            if key == "Y-same":
                # Nothing to rewrite: the promoted pieces ARE this export's.
                ok = (r1["result"]["archive"]["present"] == 1 and r1["result"]["archive"]["rows"] == 0
                      and [(x["n"], x["status"]) for x in doc] == [(0, "promoted"), (1, "promoted"), (2, "raw")]
                      and not out_of_doc)
            else:
                ok = (sorted((x["n"], x["of"], x["ch"]) for x in doc) == [(n, k2, ch2) for n in range(k2)]
                      and all(x["status"] == "raw" for x in doc)
                      and sorted(x["id"] for x in out_of_doc) == sorted(old[i]["id"] for i in promote)
                      and all(x["status"] == "promoted" for x in out_of_doc)
                      and all(x["file"] == ti.promoted_name("gate-ledger-archive.md") for x in out_of_doc))
            ok = ok and not r1["result"].get("errors") and not r2["result"].get("errors") \
                and r2["result"]["archive"]["present"] == 1 and r2["result"]["archive"]["rows"] == 0
            check(f"a newer export, {label}: the document holds this export only; promoted rows kept",
                  ok, ([(x["n"], x["of"], x["status"], x["ch"], x["batch"]) for x in now],
                       r1["result"]["archive"], r2["result"]["archive"]))
        read = await tools._exec_context_ingest({"op": "read", "file": "gate-ledger-archive.md",
                                                 "per_page": 200})
        body = json.dumps(read)
        # The only older-export text the document may still hold is Y-same's
        # three pieces, which ARE this export's text (unchanged).
        olds = body.count("o" * 7999)
        check("reading the document never mixes an older export's words in",
              read.get("status") == "ok" and olds == 3, (read.get("status"), olds, body[:300]))

        print("== a full-width last line ==")
        wb = bundle()
        wb["tickets"], wb["projects"] = [], []
        wb["archive"]["sections"] = [{"key": "W", "title": "W", "text": "a" * ti.PIECE_CHARS + "\n"}]
        await upload("gate-import-wide.tickets.gz", seal(wb))
        r1 = await imp("gate-import-wide.tickets.gz")
        r2 = await imp("gate-import-wide.tickets.gz")
        check("a section ending in a full-width line lands without an error, and only once",
              not r1["result"].get("errors") and not r2["result"].get("errors")
              and r2["result"]["archive"] == {"rows": 0, "present": 1, "filename": "gate-ledger-archive.md"},
              (r1["result"].get("errors"), r2["result"]))

        print("== one bad ticket does not stop the rest ==")
        # A row Postgres itself refuses (the class a NUL was, now refused up
        # front): the insert for T-41 raises a non-ticket error.
        real_create = T.create_ticket_service

        async def _flaky(pool_, eid_, **kw):
            if kw.get("source_ref", "").endswith(":T-41"):
                raise RuntimeError("gate: the database refused this row")
            return await real_create(pool_, eid_, **kw)
        fb = bundle()
        fb["tickets"] = [{"key": k, "sitrep": k, "options": [{"id": "done", "label": "Done"}]}
                         for k in ("T-40", "T-41", "T-42")]
        fb["projects"], fb["archive"] = [], None
        await upload("gate-import-flaky.tickets.gz", seal(fb))
        T.create_ticket_service = _flaky
        try:
            r = await imp("gate-import-flaky.tickets.gz")
        finally:
            T.create_ticket_service = real_create
        res = r.get("result") or {}
        check("a ticket the database refuses fails alone, named, and the rest land",
              r["status"] == "ok" and res.get("tickets", {}).get("created") == 2
              and [e["key"] for e in res.get("errors", [])] == ["T-41"], r)
        from server.vault import api as vault_api
        vault_ok = True
        try:
            await vault_api.store_secret(pool, entity_id=eid, name="gate_import_taken", cred_type="opaque",
                                         plaintext=b"x")
        except Exception as e:  # noqa: BLE001
            vault_ok = False
            check("the gate can store a key to collide with", False, e)
        if vault_ok:
            vb = bundle()
            vb["tickets"] = [
                {"key": "T-30", "sitrep": "asks under a name the vault already holds",
                 "options": [{"id": "store-key", "label": "x",
                              "credential_request": {"name": "gate_import_taken", "cred_type": "opaque"}}]},
                {"key": "T-31", "sitrep": "fine", "options": [{"id": "done", "label": "Done"}]}]
            vb["projects"], vb["archive"] = [], None
            await upload("gate-import-vault.tickets.gz", seal(vb))
            r = await imp("gate-import-vault.tickets.gz")
            res = r.get("result") or {}
            check("a credential ask naming a key the vault holds fails that ticket alone",
                  res.get("tickets", {}).get("created") == 1
                  and [e["key"] for e in res.get("errors", [])] == ["T-30"]
                  and "existing key" in str(res.get("errors")), res)
            await vault_api.revoke_secret(pool, entity_id=eid, name="gate_import_taken")
        eb = bundle(bundle_id=BID)
        eb["tickets"] = [
            {"key": "T-20", "sitrep": "asks for a key the vault already has",
             "options": [{"id": "store-key", "label": "x",
                          "credential_request": {"name": "", "cred_type": "opaque"}}]},
            {"key": "T-21", "sitrep": "fine", "options": [{"id": "done", "label": "Done"}]}]
        eb["projects"], eb["archive"] = [], None
        await upload("gate-import-mixed.tickets.gz", seal(eb))
        r = await imp("gate-import-mixed.tickets.gz")
        res = r.get("result") or {}
        check("a malformed credential ask fails that ticket alone",
              res.get("tickets", {}).get("created") == 1
              and [e["key"] for e in res.get("errors", [])] == ["T-20"], res)
    finally:
        await wipe()
        brain_service._pool, brain_service._entity_id, P.PROJECT_FILES_ROOT, P.ensure_project_repo = saved
        await pool.close()


def main() -> int:
    pure()
    asyncio.run(db())
    if FAILS:
        print(f"FAIL {len(FAILS)} of {len(FAILS) + PASSED}: " + "; ".join(FAILS))
        return 1
    print(f"OK {PASSED} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
