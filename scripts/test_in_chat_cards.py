#!/usr/bin/env python3
"""Gate: cards come up in the chat they belong to, and each chat reopens where
it was left (after pairing a Telegram connector: a person should not
chase apps around in the middle of a conversation; tickets render in the chat;
Circles and the Workbench open on the last open project or thread).

Against the kernel's own schema in a real PostgreSQL, through the real code
paths (the Host's tool, the connector's pairing, the ticket decide, the routes
behind their scopes):

  1. a pairing the Host starts in a turn records that turn's room on the
     grant (v204 asked_in); the Host's result still carries no code, in any
     form, and tells it the code card is in the chat;
  2. the approval ticket the connector's /pair files outside any turn carries
     that room as metadata.asked_in, so GET /api/tickets?asked_in= for that
     thread lists it (and another thread's filter does not);
  3. the code route answers the Workbench window with the token its OWN
     manifest mints (read:topology), and refuses an app that is not a person's
     window;
  4. deciding the approval card delivers the person's answer into the
     conversation that asked (it used to return before that step), with no
     authenticator code in it; a refused decide (wrong code) delivers nothing;
     answering in the NETWORK app (its real approve/decline routes) delivers
     it the same way, and exactly once whichever surface answered: a second
     action on the same grant, from any surface, is refused and tells nothing;
  5. any ticket filed during a turn is linked to the turn's room; outside a
     turn it is not, and a caller's own asked_in is still stripped by the route;
  6. GET/PUT /api/circles/last-open and /api/projects/last-open: stored per
     Host, a deleted thread or a missing project reads as null, a bad ref 400s,
     and each needs only the scope its app already holds;
  7. approving a gated phase in the Workbench (its real approve route) closes
     the phase's gate card and starts NO Host turn for it — the person acted
     in the Workbench, not on the card; the person's own approve on the card
     still reaches the Host once; both emit ticket_decided once (schedule_task's
     wake_on), the platform-made close included.

Server: PEER_TEST_DSN / TEST_DSN when set, else a throwaway cseries-postgres (docker). Exits 2
and says so without either. Run: python3 scripts/test_in_chat_cards.py
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
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]
_TMP = tempfile.TemporaryDirectory(prefix="in-chat-cards-")
os.environ.setdefault("TM_JOBS_ROOT", os.path.join(_TMP.name, "tm-jobs"))

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

ENTITY = 1
APP = "tg-conn"
PASSES = 0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail=None) -> None:
    global PASSES
    if ok:
        PASSES += 1
        print(f"  OK   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}" + (f"\n         {detail!r}"[:700] if detail is not None else ""))


async def install_row(pool, app_id: str, provenance: str) -> None:
    from server.applications import registry
    from server.applications.manifest import BuildSpec, LicenseSpec, SourceSpec
    await registry.register(pool, types.SimpleNamespace(
        id=app_id, name=app_id.title(), version="1.0.0", provenance=provenance,
        entrypoint="index.html", permissions=[], source_dir=None, license=LicenseSpec(),
        source=SourceSpec(), build=BuildSpec()))


def manifest_permissions(app_id: str) -> list[str]:
    """What an app token for this system app is minted with: its own app.yaml."""
    from server.applications.manifest import load
    return list(load(ROOT / "system-apps" / app_id / "app.yaml").permissions)


async def run(dsn: str) -> None:
    import asyncpg
    import httpx
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware

    from server import brain_service
    from server.applications import harness_backend as hb
    from server.applications.entity_signing import ensure_entity_signing_key
    from server.network import nac_connector as nc, nac_stepup
    # routes/circles does `from server import main` at module scope, and
    # server.main mounts /app/dashboard/base (a container path) at import time.
    # Stubbed as scripts/test_circles_peer_route.py does; nothing here uses it.
    _stub_main = types.ModuleType("server.main")
    _stub_main.app = None
    sys.modules.setdefault("server.main", _stub_main)
    from server.routes import circles as circles_routes, network as net_routes
    from server.routes import projects as projects_routes, tickets
    from mastercode import circles_threads as ct, memory_engine
    from mastercode.runtime import session as sess_mod, tools

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    brain_service._pool = pool
    brain_service._entity_id = ENTITY
    for app_id, prov in ((APP, "custom"), ("network", "native"), ("circles", "native"),
                         ("workbench", "native"), ("notes", "custom")):
        await install_row(pool, app_id, prov)
    secret = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    await ensure_entity_signing_key(pool, ENTITY)

    async def fresh_code() -> str:
        await pool.execute("DELETE FROM kernel_secrets WHERE id = 'nac_stepup_last_counter'")
        return nac_stepup.current_code(secret)

    # A connector app that passes the pin, without an install on disk.
    nc._load_manifest = lambda app_id: types.SimpleNamespace(
        runtime=types.SimpleNamespace(kind="process", image="python", lifecycle="resident"),
        network=["api.telegram.org"])

    # The capability's delivery wakes the app's pod; there is no cluster here.
    async def _nothing(*_a, **_k):
        return None
    hb.recycle_app, hb._release_and_bill_app, hb.ensure_awake_app = _nothing, _nothing, _nothing
    hb.app_release = _nothing

    # The answer's turn: the runtime would start a real model turn; record
    # what the decide hands it instead. Everything up to that call is real.
    started: list[tuple] = []

    async def _answer_after_turn(entity_id, room, text, *, asked_session=None, max_chars=None):
        started.append((entity_id, room, text, asked_session))
        return True
    sess_mod.answer_after_turn = _answer_after_turn

    # The routes, behind their real scope checks: the token the middleware
    # would attach is stamped from a header, with the permissions given.
    tokens: dict[str, list[str]] = {
        "workbench": manifest_permissions("workbench"),
        "circles": manifest_permissions("circles"),
        "network": manifest_permissions("network"),
        "notes": ["read:topology"],
    }

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            app_id = request.headers.get("x-test-app")
            request.state.app_token = (types.SimpleNamespace(app_id=app_id,
                                                             permissions=tokens[app_id])
                                       if app_id else None)
            return await call_next(request)

    api = FastAPI()
    api.add_middleware(_Stamp)
    for r in (tickets.router, circles_routes.router, projects_routes.router, net_routes.router):
        api.include_router(r)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://kernel")

    async def call(method, path, app, body=None):
        return await http.request(method, path, json=body, headers={"x-test-app": app})

    thread = await ct.create_thread(pool, ENTITY, title="Telegram")
    tid = str(thread["id"])
    room = {"app_id": "circles", "project_id": None, "thread_id": tid,
            "interaction_context": "circles"}

    try:
        # ── 1. the Host proposes inside a turn ──
        print("1. propose_nac_policy kind=connector, in a turn in a Circles thread")
        tok = memory_engine.CURRENT_TURN_ROOM.set(room)
        try:
            res = await tools._exec_propose_nac_policy(
                {"kind": "connector", "app_id": APP, "platform": "telegram"})
        finally:
            memory_engine.CURRENT_TURN_ROOM.reset(tok)
        r = res.get("result") if isinstance(res.get("result"), dict) else {}
        check("the draft is made", res.get("status") == "ok" and r.get("draft_id"), res)
        if not r.get("draft_id"):
            return
        gid = int(r["draft_id"])
        code = nc._codes[gid][0]
        blob = json.dumps(res)
        check("the Host-visible result has no code: raw, shown or any case",
              nc.well_formed(code) and code not in blob and nc.show_code(code) not in blob
              and code.lower() not in blob.lower() and nc.show_code(code).lower() not in blob.lower())
        check("the Host is told the code card is in the chat",
              "card" in r.get("card", "") and "chat" in r.get("card", "")
              and "the code card is in the chat and in Network; never ask for the code"
              in r.get("next", ""), r)
        # Pairing expiry is LAZY (nac_connector.expire_pairings runs only when
        # the pairings are read), so no connector_expired event fires on time:
        # the safety net is one fixed-time check that reads nac_status.
        nxt = r.get("next", "")
        check("…and its safety net is one check after the pairing's expires_at, with no "
              "wake_on, that reads nac_status — never a connector_expired wake",
              "connector_expired" not in nxt and "schedule_task op=create" in nxt
              and "expires_at" in nxt and "no wake_on" in nxt and "nac_status" in nxt
              and "op=cancel" in nxt and isinstance(r.get("expires_at"), str), nxt)
        stored = await pool.fetchval("SELECT asked_in FROM nac_connector_grants WHERE id=$1", gid)
        stored = json.loads(stored) if isinstance(stored, str) else stored
        check("the grant records the turn's room", stored == room, stored)
        lst = await nc.list_connectors(pool)
        mine = [c for c in lst["connectors"] if c["id"] == gid]
        check("the person's connector list names that room, never the code",
              mine and mine[0]["asked_in"] == room and code not in json.dumps(lst, default=str), mine)

        # ── 3. the code route answers the person's Workbench window ──
        print("3. GET /api/network/nac/connector/{id}/code")
        rr = await call("GET", f"/api/network/nac/connector/{gid}/code", "workbench")
        check("Workbench, with the token its own manifest mints, reads the code",
              rr.status_code == 200 and rr.json().get("code") == nc.show_code(code)
              and isinstance(rr.json().get("seconds_left"), int), (rr.status_code, rr.text[:200]))
        rr = await call("GET", f"/api/network/nac/connector/{gid}/code", "notes")
        check("an app that is not the person's window is refused even with read:topology",
              rr.status_code == 403, (rr.status_code, rr.text[:200]))

        # ── 2. the connector reports the code; the ticket comes up in that thread ──
        print("2. the approval ticket carries the room and lists under it")
        paired = await nc.submit_code(pool, app_id=APP, code=code, account_id="123456789",
                                      display_name="Alex")
        tk = int(paired["ticket_id"])
        md = await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id=$1", tk)
        md = json.loads(md) if isinstance(md, str) else md
        check("the ticket's asked_in is the grant's room", md.get("asked_in") == room, md)
        rr = await call("GET", f"/api/tickets?status=open&asked_in=thread:{tid}", "circles")
        ids = [t["id"] for t in rr.json().get("tickets", [])] if rr.status_code == 200 else rr.text
        check("GET /api/tickets?asked_in=thread:<id> lists it", isinstance(ids, list) and tk in ids, ids)
        rr = await call("GET", "/api/tickets?status=open&asked_in=thread:original", "workbench")
        ids = [t["id"] for t in rr.json().get("tickets", [])] if rr.status_code == 200 else rr.text
        check("…the original conversation's filter does not", isinstance(ids, list) and tk not in ids, ids)
        rr = await call("GET", "/api/tickets?asked_in=project:abc", "circles")
        check("a malformed room is a 400", rr.status_code == 400, (rr.status_code, rr.text[:200]))

        # ── 4. the decide reaches the Host in that conversation ──
        print("4. deciding the approval card")
        try:
            await tickets.decide_ticket_service(pool, ENTITY, tk, option_id="let_in",
                                                stepup_code="000000", actor="user")
            check("a wrong authenticator code is refused", False)
        except tickets.TicketError as e:
            check("a wrong authenticator code is refused", e.status == 403, e.detail)
        check("…and the Host is told nothing", started == [], started)
        c = await fresh_code()
        out = await tickets.decide_ticket_service(pool, ENTITY, tk, option_id="let_in",
                                                  stepup_code=c, actor="user")
        check("the account is let in", out.get("connector", {}).get("approved") is True, out)
        check("the answer starts the Host's turn in the room that asked",
              out.get("answer_turn_started") is True and len(started) == 1
              and started[0][1] == room and started[0][0] == ENTITY, (out, started))
        text = started[0][2] if started else ""
        check("in the ask card's words", text == f"Ticket #{tk} — I chose “Let this account in”.", text)
        check("the authenticator code is not in it", c not in text and c not in json.dumps(out), text)
        check("the card is decided once", await pool.fetchval(
            "SELECT status FROM entity_tickets WHERE id=$1", tk) == "decided")
        rr = await call("POST", f"/api/network/nac/connector/{gid}/approve", "network",
                        {"stepup_code": await fresh_code()})
        rr2 = await call("POST", f"/api/network/nac/connector/{gid}/decline", "network", {})
        check("…then Network's approve and decline on the same grant are refused, and the Host "
              "is not told twice", rr.status_code == 409 and rr2.status_code == 409
              and len(started) == 1, (rr.status_code, rr2.status_code, started))

        # ── 4b. the same answer given in the NETWORK app reaches the Host too ──
        print("4b. answering in the Network app (POST /api/network/nac/connector/{id}/...)")

        async def pairing_in_room(matched: bool) -> tuple[int, int | None]:
            tok_ = memory_engine.CURRENT_TURN_ROOM.set(room)
            try:
                res_ = await tools._exec_propose_nac_policy(
                    {"kind": "connector", "app_id": APP, "platform": "telegram"})
            finally:
                memory_engine.CURRENT_TURN_ROOM.reset(tok_)
            g = int(res_["result"]["draft_id"])
            if not matched:
                return g, None
            p = await nc.submit_code(pool, app_id=APP, code=nc._codes[g][0],
                                     account_id="222333444", display_name="Alex")
            return g, int(p["ticket_id"])

        g2, tk2 = await pairing_in_room(True)
        n0 = len(started)
        c2 = await fresh_code()
        rr = await call("POST", f"/api/network/nac/connector/{g2}/approve", "network",
                        {"stepup_code": c2})
        check("Network's 'Let this account in' lets it in", rr.status_code == 200
              and rr.json().get("approved") is True, (rr.status_code, rr.text[:300]))
        new = started[n0:]
        check("…and the Host is told once, in the conversation that started the pairing, in "
              "the card's words", len(new) == 1 and new[0][1] == room
              and new[0][2] == f"Ticket #{tk2} — I chose “Let this account in”.", new)
        check("…with no authenticator code in it", bool(new) and c2 not in new[0][2], new)
        rr = await call("POST", f"/api/network/nac/connector/{g2}/approve", "network",
                        {"stepup_code": await fresh_code()})
        rr2 = await call("POST", f"/api/network/nac/connector/{g2}/decline", "network", {})
        try:
            await tickets.decide_ticket_service(pool, ENTITY, tk2, option_id="let_in",
                                                stepup_code=await fresh_code(), actor="user")
            dec = 200
        except tickets.TicketError as e:
            dec = e.status
        check("a second action on that grant — Network again, or its card — is refused and "
              "tells the Host nothing", rr.status_code == 409 and rr2.status_code == 409
              and dec == 409 and len(started) == n0 + 1,
              (rr.status_code, rr2.status_code, dec, started[n0:]))

        g3, tk3 = await pairing_in_room(True)
        n0 = len(started)
        rr = await call("POST", f"/api/network/nac/connector/{g3}/decline", "network", {})
        new = started[n0:]
        check("Network's \"Don't pair\" tells the Host once, in that conversation",
              rr.status_code == 200 and len(new) == 1 and new[0][1] == room
              and new[0][2] == f"Ticket #{tk3} — I chose “Don't pair”.", (rr.status_code, new))
        rr = await call("POST", f"/api/network/nac/connector/{g3}/decline", "network", {})
        try:
            await tickets.decide_ticket_service(pool, ENTITY, tk3, option_id="dont_pair",
                                                actor="user")
            dec = 200
        except tickets.TicketError as e:
            dec = e.status
        try:
            await tickets.dismiss_ticket_service(pool, ENTITY, tk3, actor="user")
            dis = 200
        except tickets.TicketError as e:
            dis = e.status
        check("…and declining again, deciding or dismissing its card is refused and tells "
              "nothing", rr.status_code == 404 and dec == 409 and dis == 404
              and len(started) == n0 + 1, (rr.status_code, dec, dis, started[n0:]))

        g4, _ = await pairing_in_room(False)
        n0 = len(started)
        rr = await call("POST", f"/api/network/nac/connector/{g4}/decline", "network", {})
        new = started[n0:]
        check("declining a pairing whose code was never sent (no card yet) tells the Host too",
              rr.status_code == 200 and len(new) == 1 and new[0][1] == room
              and "Don't pair" in new[0][2] and f"grant {g4}" in new[0][2], (rr.status_code, new))

        g5, tk5 = await pairing_in_room(True)
        n0 = len(started)
        await tickets.dismiss_ticket_service(pool, ENTITY, tk5, actor="user")
        new = started[n0:]
        check("dismissing the card in Tickets declines the pairing and tells the Host once",
              len(new) == 1 and new[0][2] == f"Ticket #{tk5} — I chose “Don't pair”.", new)

        tok = memory_engine.CURRENT_TURN_ROOM.set(None)
        try:
            res6 = await tools._exec_propose_nac_policy(
                {"kind": "connector", "app_id": APP, "platform": "telegram"})
        finally:
            memory_engine.CURRENT_TURN_ROOM.reset(tok)
        g6 = int(res6["result"]["draft_id"])
        n0 = len(started)
        rr = await call("POST", f"/api/network/nac/connector/{g6}/decline", "network", {})
        check("a pairing started outside any conversation tells nobody",
              rr.status_code == 200 and len(started) == n0, (rr.status_code, started[n0:]))

        # ── 5. any ticket filed during a turn is linked to it ──
        print("5. tickets filed during a turn")
        tok = memory_engine.CURRENT_TURN_ROOM.set(room)
        try:
            t_in = await tickets.create_ticket_service(
                pool, ENTITY, source="runtime", sitrep="a route change", priority=50,
                options=[{"id": "apply", "label": "Apply"}], metadata={"kind": "x"})
            t_sup = await tickets.create_ticket_service(
                pool, ENTITY, source="runtime", sitrep="another Host's report", priority=50,
                options=[{"id": "fix", "label": "Fix"}], metadata={"kind": "support"})
            t_imp = await tickets.create_ticket_service(
                pool, ENTITY, source="manual", sitrep="an imported ticket", priority=50,
                options=[{"id": "ok", "label": "OK"}], requester={"kind": "import"}, metadata={})
        finally:
            memory_engine.CURRENT_TURN_ROOM.reset(tok)
        for label, t_ in (("another Host's support card", t_sup), ("an imported ticket", t_imp)):
            m_ = await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id=$1", t_["id"])
            m_ = json.loads(m_) if isinstance(m_, str) else m_
            check(f"{label}, even filed in a turn, is not this conversation's", "asked_in" not in m_, m_)
        t_out = await tickets.create_ticket_service(
            pool, ENTITY, source="runtime", sitrep="from a collector", priority=50,
            options=[{"id": "ok", "label": "OK"}], metadata={})
        md_in = await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id=$1", t_in["id"])
        md_out = await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id=$1", t_out["id"])
        md_in = json.loads(md_in) if isinstance(md_in, str) else md_in
        md_out = json.loads(md_out) if isinstance(md_out, str) else md_out
        check("filed in a turn: linked to its room", md_in.get("asked_in") == room, md_in)
        check("filed outside a turn: no room", "asked_in" not in md_out, md_out)
        rr = await call("POST", "/api/tickets", "circles",
                        {"source": "manual", "sitrep": "an app's own", "options": [
                            {"id": "a", "label": "A"}], "metadata": {"asked_in": room}})
        md_post = await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id=$1",
                                      rr.json().get("id")) if rr.status_code == 200 else None
        md_post = json.loads(md_post) if isinstance(md_post, str) else (md_post or {})
        check("POST /api/tickets still strips a caller's asked_in",
              rr.status_code == 200 and "asked_in" not in md_post, (rr.status_code, md_post))

        # ── 6. where each app reopens ──
        print("6. last-open")
        rr = await call("GET", "/api/circles/last-open", "circles")
        check("nothing stored: ref is null", rr.status_code == 200 and rr.json().get("ref") is None, rr.text)
        rr = await call("PUT", "/api/circles/last-open", "circles", {"ref": tid.upper()})
        check("Circles stores a thread with write:circles", rr.status_code == 200, rr.text)
        rr = await call("GET", "/api/circles/last-open", "circles")
        check("…and reads it back, canonical", rr.json().get("ref") == tid, rr.text)
        rr = await call("PUT", "/api/circles/last-open", "circles", {"ref": "../../etc"})
        check("a ref that is no thread is a 400", rr.status_code == 400, (rr.status_code, rr.text))
        await ct.delete_thread(pool, ENTITY, tid)
        rr = await call("GET", "/api/circles/last-open", "circles")
        check("a deleted thread reads as null", rr.json().get("ref") is None, rr.text)
        rr = await call("PUT", "/api/circles/last-open", "circles", {"ref": "original"})
        check("an older page storing 'original' is still accepted", rr.status_code == 200, rr.text)
        rr = await call("GET", "/api/circles/last-open", "circles")
        check("...and reads as null: there is no original conversation to reopen",
              rr.status_code == 200 and rr.json().get("ref") is None, rr.text)
        # The Host is told where it is from this block every Circles turn
        # (prompt_builder._build_surface_role): it must not describe a page
        # that no longer exists.
        import yaml as _yaml
        _pb = (_yaml.safe_load((ROOT / "system-apps" / "circles" / "app.yaml").read_text())
               .get("prompt_behavior") or {})
        _brief = json.dumps(_pb).lower()
        check("the Circles brief the Host reads names no original conversation",
              bool(_pb.get("what_it_is")) and "original conversation" not in _brief,
              [ln.strip() for ln in str(_pb.get("what_it_is") or "").splitlines()
               if "original" in ln.lower()])
        rr = await call("GET", "/api/circles/last-open", "workbench")
        check("Workbench cannot read Circles' (no read:circles)", rr.status_code == 403, rr.status_code)

        pid = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Telegram bot', 'archived') "
            "RETURNING id", ENTITY)
        rr = await call("PUT", "/api/projects/last-open", "workbench", {"ref": pid})
        check("Workbench stores a project with write:projects", rr.status_code == 200, rr.text)
        rr = await call("GET", "/api/projects/last-open", "workbench")
        check("…and reads it back, archived or not", rr.json().get("ref") == str(pid), rr.text)
        await pool.execute("DELETE FROM projects WHERE id=$1", pid)
        rr = await call("GET", "/api/projects/last-open", "workbench")
        check("a project that is gone reads as null", rr.json().get("ref") is None, rr.text)
        rr = await call("PUT", "/api/projects/last-open", "workbench", {"ref": "0"})
        check("a ref that is no project id is a 400", rr.status_code == 400, rr.status_code)
        rows = await pool.fetch("SELECT surface, kind FROM ui_last_open ORDER BY surface")
        check("one row per surface", [(r["surface"], r["kind"]) for r in rows]
              == [("circles", "thread"), ("workbench", "project")], rows)

        # ── 7. a phase approved in the Workbench: its gate card closes quietly ──
        print("7. approving a gated phase in the Workbench (POST /api/projects/{id}/phases/{id}/approve)")
        # The build the approval starts: recorded, not run (no build host here).
        # The route starts it, then decides the gate card, whose apply asks
        # again; the real start_approved_phase finds the phase building then
        # and starts nothing (tools.py: status not ready/failed), which this
        # recorder does not model — so only the route's own first start counts.
        builds: list[tuple] = []

        async def _start(_pool, _eid, project_id, phase_id):
            builds.append((project_id, phase_id))
            return {"started": True}
        tools.start_approved_phase = _start
        pid7 = await pool.fetchval(
            "INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Gated build', 'active') "
            "RETURNING id", ENTITY)
        proom = {"app_id": "workbench", "project_id": pid7, "thread_id": None,
                 "interaction_context": "workbench"}

        async def gated_phase(n: int) -> tuple[int, int]:
            ph = await pool.fetchval(
                "INSERT INTO project_phases (project_id, phase_number, title, gate) "
                "VALUES ($1, $2, $3, 'user') RETURNING id", pid7, n, f"Phase {n}")
            tok_ = memory_engine.CURRENT_TURN_ROOM.set(proom)
            try:
                g = await tickets.raise_phase_gate_ticket(pool, ENTITY, project_id=pid7, phase_id=ph)
            finally:
                memory_engine.CURRENT_TURN_ROOM.reset(tok_)
            return ph, int(g["ticket_id"])

        # What a schedule_task wake_on waits on: every decide emits
        # ticket_decided once, a platform-made close included — recorded
        # around the real emit.
        from server import host_events
        emitted: list[tuple] = []
        _real_emit = host_events.emit

        async def _rec_emit(pool_, eid_, event, attrs):
            emitted.append((event, dict(attrs or {})))
            return await _real_emit(pool_, eid_, event, attrs)
        host_events.emit = _rec_emit

        def decided_events(tid: int) -> list:
            return [a for e, a in emitted if e == "ticket_decided" and a.get("ticket_id") == tid]

        ph7, gt7 = await gated_phase(1)
        md7 = await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id=$1", gt7)
        md7 = json.loads(md7) if isinstance(md7, str) else md7
        check("the phase's gate card, raised in a turn, belongs to that project's conversation",
              md7.get("asked_in") == proom, md7)
        n0 = len(started)
        rr = await call("POST", f"/api/projects/{pid7}/phases/{ph7}/approve", "workbench")
        check("Workbench approves the phase and starts its build",
              rr.status_code == 200 and rr.json().get("approved") is True
              and builds[:1] == [(pid7, ph7)], (rr.status_code, rr.text[:300], builds))
        row7 = await pool.fetchrow("SELECT status, decision FROM entity_tickets WHERE id=$1", gt7)
        dec7 = json.loads(row7["decision"]) if isinstance(row7["decision"], str) else row7["decision"]
        check("…its gate card is closed as approved, so nothing dangles in the chat",
              row7["status"] == "decided" and (dec7 or {}).get("option_id") == "approve", dict(row7))
        check("…and no Host turn is started for it: the person approved in the Workbench, "
              "not on the card", len(started) == n0, started[n0:])
        check("…while a task waiting on that card still wakes: ticket_decided is emitted once",
              len(decided_events(gt7)) == 1
              and decided_events(gt7)[0].get("option_id") == "approve", emitted)

        ph8, gt8 = await gated_phase(2)
        n0 = len(started)
        out8 = await tickets.decide_ticket_service(pool, ENTITY, gt8, option_id="approve",
                                                   actor="user")
        new = started[n0:]
        check("the person's own 'approve' on the gate card still reaches the Host once, in "
              "that project, in the card's words",
              (out8.get("phase_approve") or {}).get("applied") is True and len(new) == 1
              and new[0][1] == proom and new[0][2] == f"Ticket #{gt8} — I chose “Approve — build it”.",
              (out8, new))
        check("…and emits ticket_decided once for it", len(decided_events(gt8)) == 1, emitted)
        host_events.emit = _real_emit
    finally:
        await http.aclose()
        await pool.close()


def main() -> int:
    server = os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if not server and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test database and docker unavailable — this gate proves nothing without one.",
              file=sys.stderr)
        return 2
    with (contextlib.nullcontext(server) if server else throwaway_server()) as srv:
        with kernel_schema_db(srv, entity_id=ENTITY) as dsn:
            print("in-chat cards and last-open against a real PostgreSQL, on the kernel's schema")
            asyncio.run(run(dsn))
    print(f"\n{PASSES}/{PASSES + len(FAILURES)} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
