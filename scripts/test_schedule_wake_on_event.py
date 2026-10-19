#!/usr/bin/env python3
"""Gate: schedule_task wake_on — a scheduled task that runs when something
HAPPENS, with fire_at as its deadline (server/host_events.py).

2026-10-05, after a Telegram connector test on one Host: the Host scheduled a
turn where it should have waited on an event. The Host set a fixed fifteen-minute check after starting a
pairing, because nothing could wake it when the pairing matched or was
approved.

On the kernel's own schema in a real PostgreSQL, through the real code paths:

  A. schedule_task op=create with wake_on writes the wake and its plan; it
     refuses a recurrence, op_tool, an event outside the fixed set and a match
     key the event does not carry, each plainly;
  B. a REAL choke point drives it: the Host proposes a Telegram pairing
     (propose_nac_policy), the connector reports the code (submit_code), and
     the person's click on the approval card (tickets.decide_ticket_service,
     with their authenticator code) approves it. The task waiting on
     connector_approved for that grant is pulled forward with woke_on; one
     waiting on connector_paired woke when the code matched;
  C. it leaves alone a task waiting on another grant, one waiting on another
     event, and a cancelled one;
  D. the real runner loop claims the woken task, its turn opens with "The
     thing you were waiting for happened: …", and its report lands in the
     thread it was set in; a task whose deadline came first opens with "Your
     deadline … arrived, and no … was recorded since you set it" and is sent
     to read the state (never told the thing did not happen);
  E. emit never raises into the state change: with the wake statement broken,
     a decline still commits and returns;
  F. key_added carries the key's NAME and never its value, through the real
     vault path; an attribute outside an event's declared keys is dropped;
  G. the schema offers wake_on, and the skill's event table (where the Host
     reads the events) is exactly the fixed set with exactly emit()'s keys;
  H. emit never holds its caller: with the waiting row locked by another
     transaction, storing a key returns within EMIT_WAIT_S and the task is
     left for its deadline; slug, hostname and platform match in any case.

Server: WAKE_TEST_DSN / PEER_TEST_DSN / TEST_DSN when set, else a throwaway
cseries-postgres (docker). Exits 2 and says so without either.
Run: python3 scripts/test_schedule_wake_on_event.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]
_TMP = tempfile.TemporaryDirectory(prefix="wake-on-event-")
os.environ.setdefault("TM_JOBS_ROOT", os.path.join(_TMP.name, "tm-jobs"))

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

ENTITY = 1
APP = "tg-conn"
SESSION = "s-wake"
# The approval card's "Let this account in" option id
# (server/network/nac_connector.py APPROVE_OPTION).
LET_IN = "let_in"
PASSES = 0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail=None) -> None:
    global PASSES
    if ok:
        PASSES += 1
        print(f"  OK   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}" + (f"\n         {detail!r}"[:900] if detail is not None else ""))


def _manifest():
    return types.SimpleNamespace(
        runtime=types.SimpleNamespace(kind="process", image="python", lifecycle="resident"),
        network=["api.telegram.org"])


async def install_row(pool, app_id: str, provenance: str = "custom") -> None:
    from server.applications import registry
    from server.applications.manifest import BuildSpec, LicenseSpec, SourceSpec
    await registry.register(pool, types.SimpleNamespace(
        id=app_id, name=app_id.title(), version="1.0.0", provenance=provenance,
        entrypoint="index.html", permissions=[], source_dir=None, license=LicenseSpec(),
        source=SourceSpec(), build=BuildSpec()))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


async def run(dsn: str) -> None:
    import asyncpg

    from server import brain_service, host_events
    from server.applications import harness_backend as hb
    from server.applications.entity_signing import ensure_entity_signing_key
    from server.network import nac_connector as nc, nac_stepup
    from server.routes import tickets
    from server.vault import api as vault_api
    from mastercode import memory_engine as me
    from mastercode import public_tool_registry as reg
    from mastercode.brain import offline_task_runner as otr
    from mastercode.runtime import tools

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    brain_service._pool = pool
    brain_service._entity_id = ENTITY

    async def no_embed(*_a, **_k):
        raise RuntimeError("no embedder in this gate")
    me._get_memory_embedding = no_embed

    for app_id, prov in ((APP, "custom"), ("network", "native"), ("circles", "native")):
        await install_row(pool, app_id, prov)
    secret = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    await ensure_entity_signing_key(pool, ENTITY)
    nc._load_manifest = lambda app_id: _manifest()

    async def _noop(app_id):
        return None
    hb.recycle_app = hb._release_and_bill_app = hb.ensure_awake_app = hb.app_release = _noop

    async def fresh_code() -> str:
        await pool.execute("DELETE FROM kernel_secrets WHERE id = 'nac_stepup_last_counter'")
        return nac_stepup.current_code(secret)

    async def schedule(title, *, minutes=60, wake_on=None, **extra):
        args = {"op": "create", "title": title,
                "fire_at": _iso(datetime.now(timezone.utc) + timedelta(minutes=minutes)),
                "prompt": f"check {title}", **extra}
        if wake_on is not None:
            args["wake_on"] = wake_on
        return await tools._exec_schedule_task(args, session_id=SESSION,
                                               interaction_context="circles")

    async def row(event_uuid):
        r = await pool.fetchrow(
            "SELECT id, status, start_time, payload FROM calendar_events WHERE event_id=$1::uuid",
            event_uuid)
        if r is None:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if isinstance(d["payload"], str) else d["payload"]
        return d

    def due(r) -> bool:
        return r["start_time"] <= datetime.now(timezone.utc)

    try:
        # ── G. the registry and the fixed set agree ──
        print("G. the schema offers wake_on and the skill's table is the fixed set")
        spec = reg.PUBLIC_TOOL_SCHEMAS["schedule_task"]["function"]["parameters"]["properties"]
        check("schedule_task's schema carries wake_on and points at skill 'scheduling' for the events",
              spec.get("wake_on", {}).get("type") == "object"
              and "skill 'scheduling'" in spec["wake_on"].get("description", ""), spec.get("wake_on"))
        # Every row of the table, not only rows naming a known event: a row
        # for an event the platform does not emit (a stale or invented one)
        # would send the Host to wait for something that never comes.
        skill_rows = {}
        for line in (ROOT / "default-skills" / "scheduling.md").read_text().splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("| ") and len(cells) == 3 and cells[0] != "event":
                skill_rows[cells[0]] = tuple(k.strip() for k in cells[1].split(","))
        check("the skill's event table lists every event with exactly the keys emit() keeps",
              skill_rows == {e: tuple(s["keys"]) for e, s in host_events.EVENTS.items()},
              (skill_rows, {e: s["keys"] for e, s in host_events.EVENTS.items()}))

        # ── A. authoring ──
        print("A. schedule_task op=create with wake_on")
        res = await tools._exec_propose_nac_policy(
            {"kind": "connector", "app_id": APP, "platform": "telegram"})
        gid = int(res["result"]["draft_id"])
        code = nc._codes[gid][0]

        main = await schedule("Telegram pairing approved",
                              wake_on={"event": "connector_approved", "match": {"grant_id": gid}})
        check("create with wake_on is ok, names the wake and its deadline",
              main.get("status") == "ok" and main["result"].get("wake_on") ==
              {"event": "connector_approved", "match": {"grant_id": str(gid)}}
              and main["result"].get("deadline"), main)
        main_id = main["result"]["event_id"]
        r = await row(main_id)
        check("the row waits (not due) with payload.wake_on",
              r and r["status"] == "scheduled" and not due(r)
              and r["payload"].get("wake_on", {}).get("event") == "connector_approved", r)
        plan = await pool.fetchval(
            "SELECT content FROM entity_memory WHERE metadata->>'scheduled_event_id' = $1", main_id)
        check("the plan written into the thread says it runs when the event happens, or at the deadline",
              plan and "to run when connector_approved" in plan and "if it has not by then" in plan, plan)
        lst = await tools._exec_schedule_task({"op": "list"})
        item = next((i for i in lst["result"]["scheduled"] if i["event_id"] == main_id), {})
        check("op=list shows the wake", item.get("wake_on", {}).get("event") == "connector_approved", item)

        bad = await schedule("bad recurrence", recurrence="daily",
                             wake_on={"event": "key_added", "match": {"name": "x"}})
        check("wake_on with a recurrence is refused plainly",
              bad.get("status") != "ok" and "cannot repeat" in json.dumps(bad), bad)
        bad = await tools._exec_schedule_task(
            {"op": "create", "title": "bad op", "fire_at": _iso(datetime.now(timezone.utc)),
             "op_tool": "host_changelog", "wake_on": {"event": "key_added", "match": {"name": "x"}}},
            session_id=SESSION, interaction_context="circles")
        check("wake_on with op_tool is refused plainly",
              bad.get("status") != "ok" and "fixed op cannot tell" in json.dumps(bad), bad)
        bad = await schedule("bad event", wake_on={"event": "pairing_done"})
        check("an event outside the fixed set is refused, naming the set",
              bad.get("status") != "ok" and "connector_approved" in json.dumps(bad), bad)
        bad = await schedule("bad key", wake_on={"event": "connector_approved", "match": {"grantid": gid}})
        check("a match key the event does not carry is refused (it would never match)",
              bad.get("status") != "ok" and "grantid" in json.dumps(bad), bad)
        n_bad = await pool.fetchval("SELECT count(*) FROM calendar_events WHERE title LIKE 'bad %'")
        check("a refused wake writes no row", n_bad == 0, n_bad)

        paired = await schedule("code arrived",
                                wake_on={"event": "connector_paired", "match": {"grant_id": gid}})
        other_grant = await schedule("another grant",
                                     wake_on={"event": "connector_approved", "match": {"grant_id": gid + 999}})
        other_event = await schedule("another event",
                                     wake_on={"event": "connector_revoked", "match": {"grant_id": gid}})
        cancelled = await schedule("cancelled one",
                                   wake_on={"event": "connector_approved", "match": {"grant_id": gid}})
        c = await tools._exec_schedule_task({"op": "cancel", "event_id": cancelled["result"]["event_id"]})
        check("the cancelled one is cancelled", c.get("status") == "ok", c)
        deadline = await schedule("deadline first", minutes=-1,
                                  wake_on={"event": "key_added", "match": {"name": "never-stored"}})
        before = {k: await row(v["result"]["event_id"])
                  for k, v in (("other_grant", other_grant), ("other_event", other_event),
                               ("cancelled", cancelled))}

        # The runner is live from here, as in a pod: a kick polls at once.
        sent: list[tuple[str, str]] = []

        async def fake_send(**kw):
            sent.append((kw.get("thread_key"), kw["message"]))
            yield "0:" + json.dumps("checked: all good") + "\n"
        _bs = (brain_service.send_message, brain_service.get_runtime)
        brain_service.send_message, brain_service.get_runtime = fake_send, (lambda: object())
        loop_task = asyncio.create_task(otr.run_offline_tasks_loop(pool, ENTITY))
        await asyncio.sleep(0.3)

        # ── B. the real choke points ──
        print("B. a real pairing: the code arrives, the person approves on the card")
        out = await nc.submit_code(pool, app_id=APP, code=code, account_id="111", display_name="Alex")
        tid = out.get("ticket_id")
        check("submit_code matched and filed the card", out.get("paired") == "awaiting_approval" and tid, out)
        rp = await row(paired["result"]["event_id"])
        check("connector_paired woke the task waiting on it, with the card's id",
              (rp["payload"].get("woke_on") or {}).get("event") == "connector_paired"
              and rp["payload"]["woke_on"]["attrs"].get("ticket_id") == str(tid), rp["payload"])
        r = await row(main_id)
        check("…and not the task waiting on connector_approved",
              "woke_on" not in r["payload"] and not due(r), r["payload"])

        # The person's click (decide_ticket_service's default decider is the user).
        dec = await tickets.decide_ticket_service(
            pool, ENTITY, int(tid), option_id=LET_IN, stepup_code=await fresh_code())
        check("the person's click on the card approved the grant",
              (dec.get("connector") or {}).get("approved") is True, dec)
        r = await row(main_id)
        woke = r["payload"].get("woke_on") or {}
        check("connector_approved pulled the waiting task forward with woke_on",
              woke.get("event") == "connector_approved"
              and woke.get("attrs", {}).get("grant_id") == str(gid)
              and woke.get("attrs", {}).get("app_id") == APP and woke.get("at"), r["payload"])

        # ── C. what it leaves alone ──
        print("C. a non-matching event, another event, a cancelled task")
        for k, v in (("other_grant", other_grant), ("other_event", other_event),
                     ("cancelled", cancelled)):
            now_r = await row(v["result"]["event_id"])
            check(f"{k}: untouched (same time, same status, no woke_on)",
                  now_r["start_time"] == before[k]["start_time"]
                  and now_r["status"] == before[k]["status"] and "woke_on" not in now_r["payload"],
                  (before[k], now_r))

        # ── D. the runner ──
        print("D. the runner runs the woken task and the deadline task")
        main_int = r["id"]
        dl_int = (await row(deadline["result"]["event_id"]))["id"]
        pr_int = rp["id"]
        for _ in range(150):
            done = await pool.fetchval(
                "SELECT count(*) FROM calendar_events WHERE id = ANY($1::int[]) AND status='completed'",
                [main_int, dl_int, pr_int])
            if done == 3:
                break
            await asyncio.sleep(0.1)
        msgs = dict(sent)
        m_main = msgs.get(otr.wakeup_thread_key(ENTITY, main_int), "")
        m_dl = msgs.get(otr.wakeup_thread_key(ENTITY, dl_int), "")
        check("the woken turn opens with 'The thing you were waiting for happened', naming the event",
              m_main.startswith("(The thing you were waiting for happened: connector_approved")
              and f'"grant_id": "{gid}"' in m_main and "The plan: check Telegram pairing approved" in m_main,
              m_main)
        # Only what the platform knows: nothing was RECORDED. A wake that
        # could not be applied runs at its deadline too, so the line never
        # says the thing did not happen, and sends the Host to its state.
        check("the deadline turn says no such event was recorded, never that it did not happen",
              m_dl.startswith("(Your deadline for **deadline first** arrived, and no key_added")
              and "was recorded since you set it" in m_dl and "never-stored" in m_dl
              and "read its state" in m_dl and "had not happened" not in m_dl, m_dl)
        check("the woken turn does not claim it ran before a deadline it cannot see",
              "before its deadline" not in m_main and "runs now because of it" in m_main, m_main)
        report = await pool.fetchrow(
            "SELECT content, metadata FROM entity_memory WHERE memory_type='turn' "
            "AND metadata->>'scheduled_task_id' = $1", str(main_int))
        md = (json.loads(report["metadata"]) if report and isinstance(report["metadata"], str)
              else (report["metadata"] if report else {}))
        check("the woken task's report lands in the thread it was set in",
              report is not None and report["content"] == "checked: all good"
              and md.get("app_id") == "circles", (report, md))
        check("each ran once and completed",
              (await row(main_id))["status"] == "completed"
              and (await row(deadline["result"]["event_id"]))["status"] == "completed"
              and (await row(paired["result"]["event_id"]))["status"] == "completed", sent)
        check("nothing else ran (the untouched and cancelled tasks did not)",
              sorted(k for k, _ in sent) == sorted(otr.wakeup_thread_key(ENTITY, i)
                                                   for i in (main_int, dl_int, pr_int)),
              [k for k, _ in sent])

        loop_task.cancel()
        with contextlib.suppress(BaseException):
            await loop_task
        brain_service.send_message, brain_service.get_runtime = _bs

        # ── E. emit never raises into the state change ──
        print("E. a broken wake never breaks the state change")
        res2 = await tools._exec_propose_nac_policy(
            {"kind": "connector", "app_id": APP, "platform": "telegram"})
        g2 = int(res2["result"]["draft_id"])
        waiting = await schedule("decline seen",
                                 wake_on={"event": "connector_declined", "match": {"grant_id": g2}})
        class _WakeFails:
            """The real pool, except that the wake statement fails."""
            def __getattr__(self, name):
                return getattr(pool, name)

            async def fetch(self, sql, *a, **k):
                if "wake_on" in sql:
                    raise ConnectionError("the wake statement failed")
                return await pool.fetch(sql, *a, **k)
        d = await nc.decline_connector(_WakeFails(), g2)
        st = await pool.fetchrow("SELECT revoked_at, revoke_reason FROM nac_connector_grants WHERE id=$1", g2)
        check("the decline committed and returned although the wake statement failed",
              d == {"grant_id": g2, "declined": True} and st["revoked_at"] is not None, (d, dict(st)))
        check("…and the waiting task is left for its deadline",
              "woke_on" not in (await row(waiting["result"]["event_id"]))["payload"])

        class _Broken:
            async def fetch(self, *_a, **_k):
                raise ConnectionError("pool gone")
        check("emit on a failing pool returns 0 and raises nothing",
              await host_events.emit(_Broken(), ENTITY, "key_added", {"name": "x"}) == 0)
        check("emit of an event outside the set wakes nothing and raises nothing",
              await host_events.emit(pool, ENTITY, "pairing_done", {"grant_id": 1}) == 0)

        # ── F. the vault event carries the name, never the value ──
        print("F. key_added carries the NAME only")
        kw = await schedule("token stored", wake_on={"event": "key_added", "match": {"name": "TG_TOKEN"}})
        value = "123456:SECRET-BOT-TOKEN-VALUE"
        await vault_api.store_secret(pool, entity_id=ENTITY, name="TG_TOKEN", cred_type="token",
                                     plaintext=value.encode())
        rk = await row(kw["result"]["event_id"])
        check("storing the key woke the task waiting on its name",
              (rk["payload"].get("woke_on") or {}).get("attrs") == {"name": "TG_TOKEN"} and due(rk),
              rk["payload"])
        rows_text = json.dumps([r_["p"] for r_ in await pool.fetch(
            "SELECT payload::text AS p FROM calendar_events")])
        check("no calendar row carries the key's value", value not in rows_text and "SECRET" not in rows_text)
        mixed = await schedule("mixed", wake_on={"event": "key_added", "match": {"name": "K2"}})
        await host_events.emit(pool, ENTITY, "key_added", {"name": "K2", "value": "SECRET-2",
                                                           "plaintext": b"SECRET-2"})
        rm = await row(mixed["result"]["event_id"])
        check("an attribute outside the event's keys is dropped before it is written",
              (rm["payload"].get("woke_on") or {}).get("attrs") == {"name": "K2"}
              and "SECRET-2" not in json.dumps(rm["payload"]), rm["payload"])

        # ── H. emit never holds its caller; caseless keys match either case ──
        print("H. a locked waiting row does not hold a key store; slug/platform match any case")
        locked = await schedule("locked row", wake_on={"event": "key_added", "match": {"name": "LOCKED"}})
        holder = await asyncpg.connect(dsn)
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT 1 FROM calendar_events WHERE event_id=$1::uuid FOR UPDATE",
                             locked["result"]["event_id"])
        _wait = getattr(host_events, "EMIT_WAIT_S", None)
        host_events.EMIT_WAIT_S = 1.0
        stored = None
        t0 = asyncio.get_running_loop().time()
        try:
            # The real caller: a person storing a key while another
            # transaction holds the row that waits on it.
            stored = await asyncio.wait_for(vault_api.store_secret(
                pool, entity_id=ENTITY, name="LOCKED", cred_type="token",
                plaintext=b"locked-value"), 10)
        except asyncio.TimeoutError:
            stored = None
        finally:
            took = asyncio.get_running_loop().time() - t0
            await tx.rollback()
            await holder.close()
            if _wait is not None:
                host_events.EMIT_WAIT_S = _wait
        check("storing a key returns while another transaction holds the waiting row",
              stored is not None and took < 5, (stored, round(took, 2)))
        check("…and that task is left for its deadline",
              "woke_on" not in (await row(locked["result"]["event_id"]))["payload"])

        site = await schedule("site live", wake_on={"event": "site_published",
                                                    "match": {"slug": "MySite"}})
        await host_events.emit(pool, ENTITY, "site_published",
                               {"project_id": 7, "slug": "mysite", "hostname": "mysite.example"})
        check("a slug written in another case matches the published (lower-case) slug",
              ((await row(site["result"]["event_id"]))["payload"].get("woke_on") or {})
              .get("event") == "site_published")
        tg = await schedule("tg code", wake_on={"event": "connector_paired",
                                                "match": {"platform": "Telegram"}})
        await host_events.emit(pool, ENTITY, "connector_paired",
                               {"grant_id": 99, "app_id": APP, "platform": "telegram"})
        check("a platform written as 'Telegram' matches the connector's 'telegram'",
              ((await row(tg["result"]["event_id"]))["payload"].get("woke_on") or {})
              .get("event") == "connector_paired")
    finally:
        await pool.close()


def main() -> int:
    server = os.environ.get("WAKE_TEST_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if not server and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test database and docker unavailable — this gate proves nothing without one.",
              file=sys.stderr)
        return 2
    with (contextlib.nullcontext(server) if server else throwaway_server()) as srv:
        with kernel_schema_db(srv, entity_id=ENTITY) as dsn:
            print("schedule_task wake_on against a real PostgreSQL, on the kernel's schema")
            asyncio.run(run(dsn))
    print(f"\n{PASSES}/{PASSES + len(FAILURES)} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
