#!/usr/bin/env python3
"""Gate: support tickets — a draft can be changed, the decision list stays
small, the shared context names no provider account, and a ticket about a
harness run offers that run's log. Against a real kernel schema, through the
Host's own `tickets` tool (runtime/tools._exec_tickets) and the support
service it calls (server/routes/support.py).

Origin: one bug report took three drafts because a
draft could not be amended; `tickets op='list'` was called three times looking
for the draft and each call returned every open DECISION ticket whole
(205,519 characters of sitreps); the shared context carried a provider's
account and request identifiers; the harness run the ticket was about was not
attached.

  L. op='list' returns decision tickets as short rows (sitrep start, option
     ids + labels, next_offset), never a support ticket; op='get' still reads
     one whole; the Circles pane's list (list_tickets_service) is unchanged.
  U. op='support_update' rewrites a draft's fields in place, keeps what was
     not passed, assembles the shared context again (new files in, the
     person's untick kept, also when the untick races the update's write),
     and refuses anything that is not a draft.
  R. the assembled context carries no provider user/request identifier, and
     keeps the error text around it.
  H. a ticket naming a compare session (by harness_session or in its card)
     offers each run's outcome and the END of its transcript, within the item
     cap; a run of another entity's project is never offered; an explicit
     harness_session (empty = none) beats a session the card text names.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the support draft lane.")
    sys.exit(2)

FAILED = 0
E = 1
SESSION = "0123456789abcdef0123456789abcdef"


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


def _items(row) -> list:
    v = row["shared_context"]
    return json.loads(v) if isinstance(v, str) else (v or [])


async def main(dsn):
    from mastercode.runtime import tools as T
    from server import brain_service as _bs
    from server.routes import support as sp
    from server.routes import tickets as tk
    from brain.mastercode.context_store import ContextStore

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    _bs._pool = pool
    _bs._entity_id = E
    try:
        # ── L ────────────────────────────────────────────────────────────
        long_sitrep = "Decide whether the quote goes out. " + ("context " * 700)
        opts = json.dumps([{"id": "send", "label": "Send it", "consequence": "x" * 400},
                           {"id": "hold", "label": "Hold it", "consequence": "y" * 400}])
        for i in range(39):
            await pool.execute(
                "INSERT INTO entity_tickets (entity_id, source, sitrep, options, context, status, priority, metadata) "
                "VALUES ($1, 'runtime', $2, $3::jsonb, $4::jsonb, 'open', $5, $6::jsonb)",
                E, f"#{i} " + long_sitrep, opts, json.dumps({"blob": "z" * 3000}), 50,
                json.dumps({"note": "m" * 1000}))
        res = await T._exec_tickets({"op": "list", "status": "open", "limit": 30})
        out = res.get("result") or {}
        rows = out.get("tickets") or []
        size = len(json.dumps(res))
        check("L list answers 30 decision rows", res.get("status") == "ok" and len(rows) == 30, str(res)[:300])
        check("L each row carries at most the first 160 characters of the sitrep",
              rows and all(len(r["sitrep"]) <= 160 for r in rows), [len(r.get("sitrep") or "") for r in rows][:3])
        check("L each row carries the option ids and labels, not their bodies",
              rows and rows[0].get("options") == [{"id": "send", "label": "Send it"},
                                                   {"id": "hold", "label": "Hold it"}], str(rows[:1])[:300])
        check("L no context, metadata, decision or requester in a row",
              rows and not ({"context", "metadata", "decision", "requester"} & set(rows[0])),
              sorted(rows[0]) if rows else rows)
        check("L the whole answer is small (under 15,000 characters for 30 tickets)", size < 15_000, size)
        check("L next_offset pages on", out.get("next_offset") == 30, out.get("next_offset"))
        res2 = await T._exec_tickets({"op": "list", "status": "open", "offset": 30})
        out2 = res2.get("result") or {}
        check("L offset reads the next page (the last 9)",
              len(out2.get("tickets") or []) == 9 and out2.get("next_offset") is None, str(out2)[:300])
        check("L the op says where support tickets are", "support_list" in str(out.get("note")), out.get("note"))
        one = await T._exec_tickets({"op": "get", "ticket_id": rows[0]["id"] if rows else 0})
        check("L op='get' still reads one ticket whole",
              len((one.get("result") or {}).get("sitrep") or "") > 5000
              and (one.get("result") or {}).get("context") == {"blob": "z" * 3000}, str(one)[:300])
        check("L each row says how long the whole sitrep is",
              rows and rows[0].get("sitrep_chars") == len((one.get("result") or {}).get("sitrep") or ""),
              str(rows[:1])[:300])
        pane = await tk.list_tickets_service(pool, E, status_filter="open", limit=5)
        check("L the Circles pane's list is unchanged (full rows)",
              len(pane["tickets"]) == 5 and len(pane["tickets"][0]["sitrep"]) > 5000
              and "context" in pane["tickets"][0], sorted(pane["tickets"][0]) if pane["tickets"] else pane)

        # ── R ────────────────────────────────────────────────────────────
        uid = "user_3Zx9QwErTyUiOpAsDfGhJkLz"
        rid = "req_011CZyXwVuTsRqPoNmLkJiHg"
        await pool.execute(
            "INSERT INTO inference_cost_log (entity_id, purpose, source_module, provider, model_id, metadata) "
            "VALUES ($1, 'inference_error', 'session', 'openrouter', 'k9', $2::jsonb)",
            E, json.dumps({"error": 'openrouter 400: {"error":{"message":"k9 is not a valid model ID",'
                                    f'"code":400}},"user_id":"{uid}"}}'}))
        await pool.execute(
            "INSERT INTO inference_cost_log (entity_id, purpose, source_module, provider, model_id, metadata) "
            "VALUES ($1, 'inference_error', 'session', 'anthropic', 'm-1', $2::jsonb)",
            E, json.dumps({"error": f'404 {{"type":"error","error":{{"message":"model m-1 was not found"}},'
                                    f'"request_id":"{rid}"}} request-id: {rid}'}))

        # D: the ticket's evidence is the Host's own `diagnose` reading.
        # One Host's refusal, three times with
        # three request ids, and a failed tool call in the error lane.
        for n in range(3):
            await pool.execute(
                "INSERT INTO inference_cost_log (entity_id, purpose, source_module, provider, model_id, metadata) "
                "VALUES ($1, 'inference_error', 'entity_runtime', 'anthropic', 'claude-opus-5-5', $2::jsonb)",
                E, json.dumps({"error": 'anthropic 400: {"type":"error","error":{"type":"invalid_request_error",'
                                        '"message":"messages.4.content.6.image.title: Extra inputs are not permitted"},'
                                        f'"request_id":"req_011CfwD{n}kzTA3sy3jRGVGcMa"}}',
                               "picture_refused": True}))
        await pool.execute(
            "INSERT INTO error_events (source, kind, tool, message, context) VALUES "
            "('tool', 'error', 'gitea', 'gitea answered 502 after 900 ms', '{\"fault\": \"platform\"}'::jsonb)")
        await ContextStore(pool, E).put("notes/first.md", "first file body")
        await ContextStore(pool, E).put("notes/second.md", "second file body")
        opened = await T._exec_tickets({
            "op": "support_open", "category": "bug", "title": "Old title",
            "problem": "Old problem", "context": "Old context", "recommendation": "Keep",
            "files": ["notes/first.md"]})
        tid = (opened.get("result") or {}).get("ticket_id")
        check("R/U support_open drafts a ticket and names the view that shows it",
              opened.get("status") == "ok" and tid
              and (opened.get("result") or {}).get("view") == f"support:{tid}", opened)
        shared = _items(await pool.fetchrow("SELECT shared_context FROM support_tickets WHERE id = $1", tid))
        inf = next((i for i in shared if i["key"] == "inference"), None)
        check("R the model-call failures item is there with its error text",
              inf and "k9 is not a valid model ID" in inf["content"]
              and "model m-1 was not found" in inf["content"], inf)
        whole = json.dumps(shared)
        check("R no provider user id or request id anywhere in the shared context",
              uid not in whole and rid not in whole and "user_3Zx9" not in whole and "req_011C" not in whole,
              inf)
        check("R the removal is marked where it was made",
              inf and getattr(sp, "PROVIDER_ID_MARK", "\0") in inf["content"], inf)
        check("D the model-call item IS diagnose's reading: grouped, counted, the provider's words",
              inf and "(from diagnose)" in inf["label"]
              and "anthropic / claude-opus-5-5 400 · 3×" in inf["content"]
              and "messages.4.content.6.image.title: Extra inputs are not permitted" in inf["content"]
              and "pictures were dropped" in inf["content"], inf)
        err_item = next((i for i in shared if i["key"] == "errors"), None)
        check("D the failed-tool item IS diagnose's reading too, and the person can still untick it",
              err_item and "(from diagnose)" in err_item["label"]
              and "gitea · 1×" in err_item["content"] and "gitea answered 502 after 900 ms" in err_item["content"]
              and err_item["included"] is True, err_item)

        # ── U ────────────────────────────────────────────────────────────
        await sp.set_shared_service(pool, E, tid, {"errors": False, "versions": False})
        upd = await T._exec_tickets({
            "op": "support_update", "ticket_id": tid, "title": "New title",
            "problem": "New problem", "severity": "high",
            "files": ["notes/first.md", "notes/second.md"]})
        check("U support_update answers ok on a draft", upd.get("status") == "ok", upd)
        row = await pool.fetchrow("SELECT * FROM support_tickets WHERE id = $1", tid)
        check("U the passed fields changed in place (same ticket, still a draft)",
              row["title"] == "New title" and row["problem"] == "New problem"
              and row["severity"] == "high" and row["status"] == "draft",
              {k: row[k] for k in ("title", "problem", "severity", "status")})
        check("U the fields not passed are kept",
              row["context"] == "Old context" and row["recommendation"] == "Keep"
              and row["category"] == "bug", {k: row[k] for k in ("context", "recommendation", "category")})
        n_tickets = await pool.fetchval("SELECT count(*) FROM support_tickets WHERE entity_id = $1", E)
        check("U no second ticket was opened", n_tickets == 1, n_tickets)
        keys = {i["key"]: i for i in _items(row)}
        check("U the shared context was assembled again: the new file is offered",
              "file:notes/second.md" in keys and keys["file:notes/second.md"]["content"] == "second file body"
              and keys["file:notes/second.md"]["included"], sorted(keys))
        check("U what the person unticked stays unticked",
              keys.get("versions", {}).get("included") is False
              and (keys.get("errors") is None or keys["errors"]["included"] is False)
              and keys.get("inference", {}).get("included") is True,
              {k: v.get("included") for k, v in keys.items()})
        # The person unticks an item WHILE the update is assembling context:
        # their untick must survive the update's write.
        real_assemble = sp._assemble_context

        async def _untick_mid_assembly(*args, **kw):
            await sp.set_shared_service(pool, E, tid, {"inference": False})
            return await real_assemble(*args, **kw)

        sp._assemble_context = _untick_mid_assembly
        try:
            mid = await T._exec_tickets({"op": "support_update", "ticket_id": tid, "context": "Old context"})
        finally:
            sp._assemble_context = real_assemble
        keys = {i["key"]: i for i in _items(await pool.fetchrow(
            "SELECT shared_context FROM support_tickets WHERE id = $1", tid))}
        check("U an untick made while the update was assembling is kept",
              mid.get("status") == "ok" and keys.get("inference", {}).get("included") is False,
              (mid, {k: v.get("included") for k, v in keys.items()}))
        # Send names the version the person was shown: a draft the Host
        # rewrote after that is refused, nothing sent.
        shown = await sp.get_service(pool, E, tid, refresh=False)
        check("U each update counts a new revision of the draft",
              shown.get("revision") == 2 and (mid.get("result") or {}).get("revision") == 2, shown.get("revision"))
        stale = None
        try:
            await sp.send_service(pool, E, tid, revision=1)
        except Exception as e:  # noqa: BLE001 — the refusal is what is checked
            stale = e
        st_after = await pool.fetchval("SELECT status FROM support_tickets WHERE id = $1", tid)
        check("U a Send naming an older revision is refused and nothing is sent",
              getattr(stale, "status", None) == 409 and "changed this draft" in str(stale)
              and st_after == "draft", (repr(stale), st_after))
        bad = await T._exec_tickets({"op": "support_update", "ticket_id": tid, "category": "nonsense"})
        check("U a bad category is refused", bad.get("status") != "ok" and "category" in str(bad), bad)
        # The person's untick lands while a Host support_update is writing a
        # new item list: the untick's read-modify-write must
        # not put the older list back. The wrapper only holds the untick
        # between its read and its write until the update has finished (or
        # 2 s, when the update is waiting on the untick's row lock).
        await ContextStore(pool, E).put("notes/third.md", "third file body")
        racing: dict = {}

        async def _after_read():
            racing["task"] = asyncio.ensure_future(T._exec_tickets({
                "op": "support_update", "ticket_id": tid,
                "files": ["notes/first.md", "notes/second.md", "notes/third.md"]}))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(racing["task"]), 2.0)

        class _Reads:
            def __init__(self, inner):
                self._inner = inner

            async def fetchrow(self, sql, *a):
                r = await self._inner.fetchrow(sql, *a)
                if "shared_context, status" in sql and "task" not in racing:
                    await _after_read()
                return r

            def acquire(self):
                outer = self._inner.acquire()

                class _Acq:
                    async def __aenter__(_s):
                        return _Reads(await outer.__aenter__())

                    async def __aexit__(_s, *exc):
                        return await outer.__aexit__(*exc)
                return _Acq()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        await sp.set_shared_service(_Reads(pool), E, tid, {"file:notes/first.md": False})
        raced = await racing["task"]
        keys = {i["key"]: i for i in _items(await pool.fetchrow(
            "SELECT shared_context FROM support_tickets WHERE id = $1", tid))}
        check("U an untick racing a support_update keeps the update's new items and the untick",
              raced.get("status") == "ok" and "file:notes/third.md" in keys
              and keys.get("file:notes/first.md", {}).get("included") is False,
              (raced, {k: v.get("included") for k, v in keys.items()}))

        # The person's untick outlives the item leaving the list: an update
        # drops the unticked file, a later one brings it back, and it comes
        # back unticked. A re-tick is remembered the same way.
        async def _keys():
            return {i["key"]: i for i in _items(await pool.fetchrow(
                "SELECT shared_context FROM support_tickets WHERE id = $1", tid))}
        gone = await T._exec_tickets({"op": "support_update", "ticket_id": tid,
                                      "files": ["notes/second.md", "notes/third.md"]})
        keys = await _keys()
        check("U an update can drop an unticked item from the list",
              gone.get("status") == "ok" and "file:notes/first.md" not in keys, (gone, sorted(keys)))
        back = await T._exec_tickets({"op": "support_update", "ticket_id": tid, "files": [
            "notes/first.md", "notes/second.md", "notes/third.md"]})
        keys = await _keys()
        check("U an item the person unticked comes back unticked after leaving the list",
              back.get("status") == "ok"
              and keys.get("file:notes/first.md", {}).get("included") is False
              and keys.get("file:notes/second.md", {}).get("included") is True,
              (back, {k: v.get("included") for k, v in keys.items()}))
        await sp.set_shared_service(pool, E, tid, {"file:notes/first.md": True})
        await T._exec_tickets({"op": "support_update", "ticket_id": tid,
                               "files": ["notes/second.md"]})
        await T._exec_tickets({"op": "support_update", "ticket_id": tid, "files": [
            "notes/first.md", "notes/second.md", "notes/third.md"]})
        keys = await _keys()
        check("U a re-ticked item comes back ticked (the control)",
              keys.get("file:notes/first.md", {}).get("included") is True
              and keys.get("versions", {}).get("included") is False,
              {k: v.get("included") for k, v in keys.items()})
        await pool.execute("UPDATE support_tickets SET status = 'sent' WHERE id = $1", tid)
        sent = await T._exec_tickets({"op": "support_update", "ticket_id": tid, "problem": "late"})
        after = await pool.fetchval("SELECT problem FROM support_tickets WHERE id = $1", tid)
        check("U a sent ticket is refused and left as it was",
              sent.get("status") != "ok" and "not a draft" in str(sent) and after == "New problem",
              (sent, after))

        # ── H ────────────────────────────────────────────────────────────
        pid = await pool.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, 'compare') RETURNING id", E)
        other = await pool.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, 'theirs') RETURNING id",
                                    E + 1)
        transcript = ("early line\n" * 6000) + "the run stopped here: FINAL LINE"
        run_ids = []
        for h, ok in (("cursor", True), ("opencode", True), ("claude-code", False)):
            run_ids.append(await pool.fetchval(
                "INSERT INTO harness_run (session_id, project_id, harness, model, ok, exit_code, error, "
                "transcript, diff_bytes, elapsed_seconds) "
                "VALUES ($1, $2, $3, 'm', $4, $5, $6, $7, 10, 61.0) RETURNING id",
                SESSION, pid, h, ok, 0 if ok else 1, None if ok else "buffer overflow", transcript))
        foreign = await pool.fetchval(
            "INSERT INTO harness_run (session_id, project_id, harness, ok, transcript) "
            "VALUES ($1, $2, 'kimi', true, 'NOT YOURS') RETURNING id", SESSION, other)
        opened = await T._exec_tickets({
            "op": "support_open", "category": "bug", "title": "Compare went wrong",
            "problem": "Two runs misbehaved", "harness_session": SESSION})
        tid2 = (opened.get("result") or {}).get("ticket_id")
        shared = _items(await pool.fetchrow("SELECT shared_context FROM support_tickets WHERE id = $1", tid2)) \
            if tid2 else []
        runs = [i for i in shared if i["kind"] == "run"]
        check("H each run of the named session is offered, ticked",
              sorted(i["key"] for i in runs) == sorted(f"run:{r}" for r in run_ids)
              and all(i["included"] for i in runs), (opened, [(i["key"], i["included"]) for i in runs]))
        cc = next((i for i in runs if i["key"] == f"run:{run_ids[2]}"), {})
        check("H a run item says how it ended and carries the END of its transcript",
              "failed" in cc.get("content", "") and "buffer overflow" in cc.get("content", "")
              and cc.get("content", "").rstrip().endswith("FINAL LINE"), cc.get("content", "")[:300])
        check("H a run item fits the per-item cap",
              runs and all(len(i["content"]) <= sp.EXCERPT_CAP for i in runs), [len(i["content"]) for i in runs])
        check("H another entity's run is never offered",
              f"run:{foreign}" not in {i["key"] for i in shared} and "NOT YOURS" not in json.dumps(shared),
              [i["key"] for i in shared])
        check("H the draft stays within the item cap",
              sum(1 for i in shared if i["included"]) <= sp.SHARED_MAX, len(shared))
        opened = await T._exec_tickets({
            "op": "support_open", "category": "bug", "title": "Compare again",
            "problem": f"The compare session {SESSION} lost a run"})
        tid3 = (opened.get("result") or {}).get("ticket_id")
        shared = _items(await pool.fetchrow("SELECT shared_context FROM support_tickets WHERE id = $1", tid3))
        check("H a session id written in the card is picked up too",
              sorted(i["key"] for i in shared if i["kind"] == "run") == sorted(f"run:{r}" for r in run_ids),
              [i["key"] for i in shared])
        upd = await T._exec_tickets({"op": "support_update", "ticket_id": tid3,
                                     "problem": "Only one run matters", "harness_runs": [run_ids[0]],
                                     "harness_session": ""})
        shared = _items(await pool.fetchrow("SELECT shared_context FROM support_tickets WHERE id = $1", tid3))
        check("H support_update can narrow the evidence to one run",
              upd.get("status") == "ok"
              and [i["key"] for i in shared if i["kind"] == "run"] == [f"run:{run_ids[0]}"],
              (upd, [i["key"] for i in shared]))
        # An explicit empty harness_session beats a session the card text
        # still names: no session's runs are offered.
        opened = await T._exec_tickets({
            "op": "support_open", "category": "bug", "title": "Compare once more",
            "problem": f"The compare session {SESSION} lost a run"})
        tid5 = (opened.get("result") or {}).get("ticket_id")
        upd = await T._exec_tickets({"op": "support_update", "ticket_id": tid5,
                                     "harness_session": "", "severity": "high"})
        shared = _items(await pool.fetchrow("SELECT shared_context FROM support_tickets WHERE id = $1", tid5))
        check("H harness_session='' drops the runs while the card still names the session",
              upd.get("status") == "ok" and not [i for i in shared if i["kind"] == "run"],
              (upd, [i["key"] for i in shared]))
        upd = await T._exec_tickets({"op": "support_update", "ticket_id": tid5,
                                     "context": f"still about {SESSION}"})
        shared = _items(await pool.fetchrow("SELECT shared_context FROM support_tickets WHERE id = $1", tid5))
        check("H a later update passing only card text does not bring the dropped runs back",
              upd.get("status") == "ok" and not [i for i in shared if i["kind"] == "run"],
              (upd, [i["key"] for i in shared]))

        # A secret the run item's cuts split: every cut must
        # come after redaction, or the back half of a key — its recognisable
        # `sk-ant-` front cut away — reaches the draft untouched.
        body = "Qx7" * 30                      # the transcript's key
        key = "sk-ant-api03-" + body
        sbody = "Wv5" * 30                     # the stderr's key
        stderr = "s" * 1200 + "\nAPI key: sk-ant-api03-" + sbody + "\n" + "z" * 949
        assert stderr[-1000:].startswith(sbody[-50:])   # its last 1,000 start inside the key
        pem = ("-----BEGIN PRIVATE KEY-----\n" + ("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 2 + "\n") * 45
               + "-----END PRIVATE KEY-----")
        end = body[-60:] + "\n" + pem + "\n"
        last = "the run stopped here: FINAL RUN LINE"
        fill = sp.EXCERPT_CAP - len(end) - len(last)
        suffix = end + "x\n" * (fill // 2) + "y" * (fill % 2) + last
        leaky = "run start\n" * 500 + "export ANTHROPIC_API_KEY=" + key[:-60] + suffix
        # The last EXCERPT_CAP characters start inside the key, and a long
        # private key later in them shrinks under redaction by more than the
        # item's header takes, so a cut made before redaction would show the
        # key's back half.
        assert len(suffix) == sp.EXCERPT_CAP and leaky[-sp.EXCERPT_CAP:].startswith(body[-60:])
        rid_leak = await pool.fetchval(
            "INSERT INTO harness_run (session_id, project_id, harness, ok, exit_code, error, transcript, metadata) "
            "VALUES ('leak', $1, 'claude-code', false, 1, $2, $3, $4::jsonb) RETURNING id",
            pid, "x" * 990 + key, leaky, json.dumps({"stderr_tail": stderr}))
        opened = await T._exec_tickets({"op": "support_open", "category": "bug", "title": "Run leaked",
                                        "problem": "A run failed", "harness_runs": [rid_leak]})
        tid4 = (opened.get("result") or {}).get("ticket_id")
        shared = _items(await pool.fetchrow("SELECT shared_context FROM support_tickets WHERE id = $1", tid4)) \
            if tid4 else []
        item = next((i for i in shared if i["key"] == f"run:{rid_leak}"), {})
        content = item.get("content", "")

        def _around(mark):
            at = content.find(mark)
            return content[max(0, at - 60):at + 80] if at >= 0 else ""
        check("H no part of a key split by the stderr cut reaches the draft",
              item and "Wv5Wv5Wv5" not in content and "z" * 100 in content, _around("Wv5") or item)
        check("H no part of a key split by the transcript cut reaches the draft",
              item and "Qx7Qx7Qx7" not in content and "x\nx\nx\n" in content,
              _around("Qx7") or content[:300])
        check("H the run item still ends on the run's last line",
              content.rstrip().endswith("FINAL RUN LINE"), content[-200:])
    finally:
        await pool.close()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
