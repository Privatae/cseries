#!/usr/bin/env python3
"""Gate: messaging connector grants, pairing and capability, on the kernel's own
schema in a real PostgreSQL.

What it proves, end to end through the real code paths (the Host's tools, the
routes, the ticket decide, the wake, the uninstall paths):

  - the one-time code is never in a tool result, nac_status or a ticket; only
    the person's own window (Network, Circles, Workbench as the platform signed
    them) can read it from the code route;
  - the code: os.urandom from the 20-consonant alphabet; input upper-cased with
    spaces and hyphens dropped; a malformed code is not a try; 5 wrong
    well-formed codes void the pairing; it expires; a code from another
    connector app is refused without being compared with that app's pairing;
  - the capability is minted at pairing start, travels in the wake's creds, and
    goes live ONLY on creds_applied: true (absent and false keep it pending);
    the resident heartbeat never mints; a re-pair keeps the live one with no
    restart; a pairing after a revoke mints a fresh one;
  - a pairing-stage capability opens /pair and /platform and nothing else; an approved grant
    opens the message routes; a grant whose signature fails opens nothing;
  - approve_nac_draft refuses kind connector (and burns no code); the approval
    needs the authenticator code and burns it, is the person's alone, and is
    ONE transaction that also revokes the older grant of the app — a race that
    loses leaves the older grant in force;
  - revoke (Network, revoke_nac_rule, both uninstall paths, re-pair) runs the
    turn-cancel hook after commit, clears the capability, does not stop the
    app, and the very next post is 403;
  - the network[] pin refuses a foreign host and a non-process / non-resident app,
    at pairing start and again on every request.

Server: CONNECTOR_TEST_DSN / PEER_TEST_DSN / TEST_DSN when set, else a
throwaway cseries-postgres (docker). Exits 2 and says so without either.
Run: python3 scripts/test_connector_grants.py
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
_TMP = tempfile.TemporaryDirectory(prefix="connector-grants-")
os.environ.setdefault("TM_JOBS_ROOT", os.path.join(_TMP.name, "tm-jobs"))

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

ENTITY = 1
APP, OTHER = "tg-conn", "tg-other"
PASSES = 0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail=None) -> None:
    global PASSES
    if ok:
        PASSES += 1
        print(f"  OK   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}" + (f"\n         {detail!r}"[:600] if detail is not None else ""))


def _manifest(kind="process", image="python", lifecycle="resident", network=("api.telegram.org",)):
    return types.SimpleNamespace(
        runtime=types.SimpleNamespace(kind=kind, image=image, lifecycle=lifecycle),
        network=list(network))


async def install_row(pool, app_id: str, provenance: str = "custom") -> None:
    """An installed_applications row, written by the registry's own register()."""
    from server.applications import registry
    from server.applications.manifest import BuildSpec, LicenseSpec, SourceSpec
    await registry.register(pool, types.SimpleNamespace(
        id=app_id, name=app_id.title(), version="1.0.0", provenance=provenance,
        entrypoint="index.html", permissions=[], source_dir=None, license=LicenseSpec(),
        source=SourceSpec(), build=BuildSpec()))


class _Req:
    """The slice of a Starlette Request the Network routes read."""
    def __init__(self, app_id=None, body=None):
        self.state = types.SimpleNamespace(
            app_token=types.SimpleNamespace(app_id=app_id) if app_id else None)
        self._body = body or {}
        self.headers = {"content-type": "application/json"}
        self.scope = {"path": "/api/network/nac/connector"}

    async def stream(self):
        # The routes read a body through the bounded reader (network.py _json_in ->
        # server/harness_input.py::read_request), which takes it from stream().
        yield json.dumps(self._body).encode()


class _Bridge:
    def __init__(self):
        self._connected = True
        self.calls = []
        self.answer = {"ok": True, "data": {"url": "10.0.0.9:8080"}}

    async def request_full(self, op, params=None):
        self.calls.append((op, json.loads(json.dumps(params))))
        return self.answer


async def run(dsn: str) -> None:
    import asyncpg
    from fastapi import FastAPI, HTTPException
    import httpx

    from server import brain_service
    from server.applications import harness_backend as hb, installer, registry
    from server.applications.entity_signing import ensure_entity_signing_key
    from server.network import nac_connector as nc, nac_stepup
    from server.routes import connector as conn_routes, network as net_routes, tickets
    from mastercode.runtime import tools

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    brain_service._pool = pool
    brain_service._entity_id = ENTITY

    for app_id, prov in ((APP, "custom"), (OTHER, "custom"), ("network", "native"),
                         ("circles", "native"), ("workbench", "native")):
        await install_row(pool, app_id, prov)
    secret = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    await ensure_entity_signing_key(pool, ENTITY)

    async def fresh_code() -> str:
        # Burn-on-use keeps the last accepted counter; each approval in this
        # gate takes the current window again, so forget the last one first.
        await pool.execute("DELETE FROM kernel_secrets WHERE id = 'nac_stepup_last_counter'")
        return nac_stepup.current_code(secret)

    manifests = {APP: _manifest(), OTHER: _manifest()}
    nc._load_manifest = lambda app_id: manifests[app_id]

    lane = {"recycle": [], "release": [], "awake": [], "app_release": []}

    async def _recycle(app_id):
        lane["recycle"].append(app_id)
        return False

    async def _release(app_id):
        lane["release"].append(app_id)

    async def _awake(app_id):
        lane["awake"].append(app_id)
        return None

    async def _app_release(app_id):
        lane["app_release"].append(app_id)
        return True

    hb.recycle_app, hb._release_and_bill_app, hb.ensure_awake_app = _recycle, _release, _awake
    hb.app_release = _app_release

    cancelled = []

    async def _canceller(*, app_id, grant_id, reason):
        cancelled.append((app_id, grant_id, reason))

    nc.register_turn_canceller(_canceller)

    api = FastAPI()
    api.include_router(conn_routes.router)
    # In THIS event loop: the pool belongs to it (a TestClient runs the app on
    # a loop of its own, where every pool call fails — and is refused, 503).
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://kernel")

    async def post(path, cap, body=None, app=APP, method="POST"):
        h = {"X-CSeries-App-Id": app, "X-CSeries-Connector-Cap": cap} if cap else {}
        return await http.request(method, path, json=body or {}, headers=h)

    def opened(rr) -> bool:
        """The message route answered (no chat_id is its 400), not the gate
        (403): the capability opened it."""
        return rr.status_code == 400 and "chat_id" in rr.text

    async def live_cap(app_id) -> str:
        """Take the pending capability the wake would carry and confirm it, as
        an operator answering creds_applied: true does."""
        creds, h = nc.pending_creds(app_id)
        assert await nc.on_wake_answer(app_id, h, True)
        return creds[nc.CAP_CREDS_KEY]

    try:
        # ── 1. the Host proposes: a draft, a pairing, no code anywhere it can read ──
        print("1. propose_nac_policy kind=connector")
        res = await tools._exec_propose_nac_policy(
            {"kind": "connector", "app_id": APP, "platform": "telegram"})
        r = res.get("result") if isinstance(res.get("result"), dict) else {}
        check("the Host's tool makes a connector draft", res.get("status") == "ok" and r.get("draft_id"), res)
        if not r.get("draft_id"):
            return
        gid = int(r["draft_id"])
        code = nc._codes.get(gid, ("",))[0]
        blob = json.dumps(res)
        check("the code exists only in the kernel's memory", nc.well_formed(code), code)
        check("the tool result carries neither the code nor its shown form",
              code not in blob and nc.show_code(code) not in blob and code.lower() not in blob.lower())
        check("the chat gets the kernel's words",
              r.get("card") == "The pairing code is on a card in your chat, and in Network.", r)
        caps = await pool.fetchrow("SELECT cap_hash, pending_hash FROM nac_connector_caps WHERE app_id=$1", APP)
        check("the capability is minted at pairing start, pending, not live",
              caps and caps["pending_hash"] and caps["cap_hash"] is None, caps)
        await asyncio.sleep(0.05)
        check("delivery releases the pod and cold-wakes it", lane["release"] == [APP] and lane["awake"] == [APP], lane)
        st = await tools._exec_nac_status({})
        check("nac_status shows the pairing but never the code",
              code not in json.dumps(st, default=str) and f'"id": {gid}' in json.dumps(st, default=str), st)
        stored = await pool.fetchval("SELECT code_hash FROM nac_connector_grants WHERE id=$1", gid)
        check("only a hash of the code is stored", stored and code not in stored and len(stored) == 64, stored)

        # ── 2. the wake carries the capability; it goes live only on creds_applied: true ──
        print("2. the capability travels in the wake and switches only on creds_applied: true")
        br = _Bridge()
        hb._bridge = lambda: br
        pend = (await pool.fetchval("SELECT pending_hash FROM nac_connector_caps WHERE app_id=$1", APP))
        raw = nc._pending[APP][0]
        await hb._wake_app(APP)
        sent = br.calls[-1][1]
        check("the wake carries creds {connector-cap: <value>} with app_rt",
              sent.get("app_rt") == APP and sent.get("creds") == {"connector-cap": raw}, sent)
        live = await pool.fetchval("SELECT cap_hash FROM nac_connector_caps WHERE app_id=$1", APP)
        check("an answer WITHOUT creds_applied keeps it pending", live is None, live)
        br.answer = {"ok": True, "data": {"url": "10.0.0.9:8080", "creds_applied": False}}
        for _ in range(3):   # the resident heartbeat, warm wakes
            await hb._wake_app(APP)
        row = await pool.fetchrow("SELECT cap_hash, pending_hash FROM nac_connector_caps WHERE app_id=$1", APP)
        check("creds_applied false keeps it pending, and heartbeats never mint a new one",
              row["cap_hash"] is None and row["pending_hash"] == pend
              and all(c[1].get("creds") == {"connector-cap": raw} for c in br.calls), row)
        br.answer = {"ok": True, "data": {"url": "10.0.0.9:8080", "creds_applied": True}}
        await hb._wake_app(APP)
        row = await pool.fetchrow("SELECT cap_hash, pending_hash FROM nac_connector_caps WHERE app_id=$1", APP)
        check("creds_applied true switches to it", row["cap_hash"] == pend and row["pending_hash"] is None, row)
        await hb._wake_app(APP)
        check("…and later wakes carry no creds", "creds" not in br.calls[-1][1], br.calls[-1])
        cap = raw

        # ── 3. the network[] pin and the app's form ──
        print("3. the network[] pin")
        check("a foreign host is refused, by name",
              "evil.example" in (nc.app_refusal(APP, "telegram", _manifest(network=("api.telegram.org", "evil.example"))) or ""))
        check("the platform's host passes", nc.app_refusal(APP, "telegram", _manifest()) is None)
        check("a look-alike suffix is not a subdomain",
              nc.app_refusal(APP, "slack", _manifest(network=("slack.com.evil.io",))) is not None)
        check("Slack's Socket Mode and upload hosts are subdomains of slack.com",
              nc.app_refusal(APP, "slack", _manifest(network=("slack.com", "wss-primary.slack.com",
                                                              "files.slack.com"))) is None)
        check("Discord's REST, gateway and CDN hosts pass",
              nc.app_refusal(APP, "discord", _manifest(network=("discord.com", "gateway.discord.gg",
                                                                "cdn.discordapp.com"))) is None)
        check("a helper-form app is refused (only process pods reach 8800)",
              nc.app_refusal(APP, "telegram", _manifest(kind="helper")) is not None)
        check("a process app with no runtime image is refused",
              nc.app_refusal(APP, "telegram", _manifest(image=None)) is not None)
        check("a window-lifecycle app is refused (a connector is resident)",
              nc.app_refusal(APP, "telegram", _manifest(lifecycle="window")) is not None)
        manifests["tg-bad"] = _manifest(network=("api.telegram.org", "pastebin.com"))
        n_before = await pool.fetchval("SELECT count(*) FROM nac_connector_grants")
        bad = await tools._exec_propose_nac_policy(
            {"kind": "connector", "app_id": "tg-bad", "platform": "telegram"})
        check("the tool refuses that app, and writes no draft and mints nothing",
              bad.get("status") != "ok" and "pastebin.com" in json.dumps(bad)
              and await pool.fetchval("SELECT count(*) FROM nac_connector_grants") == n_before
              and await pool.fetchval("SELECT count(*) FROM nac_connector_caps WHERE app_id='tg-bad'") == 0, bad)

        # ── 4. the code's shape ──
        print("4. the code")
        codes = [nc.new_code() for _ in range(300)]
        check("every code is 8 of BCDFGHJKLMNPQRSTVWXZ", all(nc.well_formed(c) for c in codes))
        check("…and they differ", len(set(codes)) > 290)
        check("input is upper-cased with spaces and hyphens dropped",
              nc.normalize_code(" bcdf - ghjk ") == "BCDFGHJK" and nc.show_code("BCDFGHJK") == "BCDF-GHJK")

        # ── 5. the code route answers the person's own windows only ──
        print("5. the code is shown only in the person's own window")
        shown = await net_routes.nac_connector_code(gid, _Req("network"))
        shown = json.loads(shown.body) if hasattr(shown, "body") else shown
        check("Network reads the code", shown.get("code") == nc.show_code(code), shown)
        for who in ("host", APP, None):
            try:
                await net_routes.nac_connector_code(gid, _Req(who))
                check(f"the code route refuses {who or 'no token'}", False)
            except HTTPException as e:
                check(f"the code route refuses {who or 'no token'}", e.status_code in (401, 403), e.status_code)
        await pool.execute("DELETE FROM installed_applications WHERE id='circles'")
        await install_row(pool, "circles", "custom")
        try:
            await net_routes.nac_connector_code(gid, _Req("circles"))
            check("a Host-built app that took the id 'circles' cannot read it", False)
        except HTTPException as e:
            check("a Host-built app that took the id 'circles' cannot read it", e.status_code == 403)
        await pool.execute("DELETE FROM installed_applications WHERE id='circles'")
        await install_row(pool, "circles", "native")

        # ── 6. /pair ──
        print("6. POST /api/connector/pair")
        for path, m in (("/api/connector/messages", "POST"), ("/api/connector/messages/sent", "POST"),
                        ("/api/connector/turns/t1", "GET")):
            rr = await post(path, cap, method=m)
            check(f"a pairing-stage capability does not open {m} {path}", rr.status_code == 403, rr.status_code)
        # Before approval the connector needs the platform
        # (getUpdates) to receive the code, so /platform opens with /pair. The
        # route answers (an empty call is a 400 from the request builder), not
        # the gate.
        rr = await post("/api/connector/platform", cap, {})
        check("a pairing-stage capability opens POST /api/connector/platform",
              rr.status_code == 400 and "path" in rr.text, (rr.status_code, rr.text))
        rr = await post("/api/connector/pair", cap, {"code": "123", "account_id": "111"})
        tries = await pool.fetchval("SELECT wrong_tries FROM nac_connector_grants WHERE id=$1", gid)
        check("a malformed code is refused and not counted", rr.status_code == 400 and tries == 0, (rr.status_code, tries))
        # another connector app with its own open pairing
        res2 = await tools._exec_propose_nac_policy({"kind": "connector", "app_id": OTHER, "platform": "telegram"})
        gid2 = int(res2["result"]["draft_id"])
        code2 = nc._codes[gid2][0]
        cap2 = await live_cap(OTHER)
        rr = await post("/api/connector/pair", cap, {"code": code2, "account_id": "111"})
        o = await pool.fetchrow("SELECT pairing, wrong_tries FROM nac_connector_grants WHERE id=$1", gid2)
        check("another app's code is refused, and never compared with that app's pairing",
              rr.status_code == 403 and o["pairing"] == "open" and o["wrong_tries"] == 0, (rr.status_code, dict(o)))
        rr = await post("/api/connector/pair", cap2, {"code": code, "account_id": "111"}, app=OTHER)
        check("…in either direction", rr.status_code == 403, rr.status_code)
        rr = await post("/api/connector/pair", cap, {"code": code2, "account_id": "111"}, app=OTHER)
        check("APP's capability with OTHER's id is no capability at all", rr.status_code == 403
              and "capability" in rr.text, rr.text)
        for i in range(2):
            await post("/api/connector/pair", cap, {"code": "BBBBBBBB", "account_id": "111"})
        tries = await pool.fetchval("SELECT wrong_tries FROM nac_connector_grants WHERE id=$1", gid)
        check("wrong well-formed codes count, from any sender", tries == 3, tries)
        rr = await post("/api/connector/pair", cap, {"code": nc.show_code(code).lower().replace("-", " - "),
                                               "account_id": "bad id"})
        check("a malformed account id is refused before the code counts", rr.status_code == 400, rr.text)
        rr = await post("/api/connector/pair", cap,
                  {"code": " " + nc.show_code(code).lower() + " ", "account_id": "123456789",
                   "display_name": "Alex R\x07"})
        g = await pool.fetchrow("SELECT pairing, account_id, display_name, code_hash, ticket_id "
                                "FROM nac_connector_grants WHERE id=$1", gid)
        check("the right code, lower-case and spaced, binds the reported account",
              rr.status_code == 200 and g["pairing"] == "matched" and g["account_id"] == "123456789"
              and g["display_name"] == "Alex R" and g["code_hash"] is None, (rr.text, dict(g)))
        check("…and is gone from memory", gid not in nc._codes)
        t = await pool.fetchrow("SELECT sitrep, options, metadata, status FROM entity_tickets WHERE id=$1",
                                g["ticket_id"])
        md = json.loads(t["metadata"]) if isinstance(t["metadata"], str) else t["metadata"]
        check("a ticket only the person can answer is filed, in the kernel's words",
              t["status"] == "open" and md.get("user_only") and md.get("kind") == "connector_pairing"
              and "Telegram account “Alex R” (id 123456789)" in t["sitrep"]
              and "cannot approve anything" in t["sitrep"], dict(t))
        check("the ticket carries no code", code not in json.dumps(dict(t), default=str))
        rr = await post("/api/connector/pair", cap, {"code": code, "account_id": "1"})
        check("the pairing is closed once matched", rr.status_code == 403, rr.status_code)
        rr = await post("/api/connector/platform", cap, {})
        check("…but while the person has not approved, the capability still reaches the platform "
              "(to say the code was accepted, and to keep reading)",
              rr.status_code == 400 and "path" in rr.text, (rr.status_code, rr.text))
        caller = await nc.check_connector_cap(pool, APP, cap)
        check("…at the 'matched' stage alone, with no grant yet (the key it binds: section 12)",
              caller is not None and caller.stages == frozenset({nc.STAGE_MATCHED})
              and caller.grant_id is None, caller)
        rr = await post("/api/connector/messages", cap, {"account_id": "123456789"})
        check("…and starts no turn: the message routes are still 403", rr.status_code == 403, rr.status_code)
        # five wrong codes void a pairing
        # OTHER's pairing already took one wrong code above (APP's, sent with cap2)
        t0 = await pool.fetchval("SELECT wrong_tries FROM nac_connector_grants WHERE id=$1", gid2)
        for i in range(5 - t0):
            last = await post("/api/connector/pair", cap2, {"code": "CCCCCCCC", "account_id": "1"}, app=OTHER)
        o = await pool.fetchrow("SELECT pairing, wrong_tries, revoked_at FROM nac_connector_grants WHERE id=$1", gid2)
        check("the fifth wrong code voids the pairing", o["pairing"] == "void" and o["wrong_tries"] == 5
              and o["revoked_at"] is not None and "void" in last.text, (dict(o), last.text))
        rr = await post("/api/connector/pair", cap2, {"code": code2, "account_id": "1"}, app=OTHER)
        check("…and then even the right code gets nothing", rr.status_code == 403, rr.status_code)
        # expiry
        res3 = await tools._exec_propose_nac_policy({"kind": "connector", "app_id": OTHER, "platform": "telegram"})
        check("a re-pair of an app with a live capability keeps it (no restart)",
              res3["result"]["capability"] == "live" and lane["release"].count(OTHER) == 1, (res3, lane))
        gid3 = int(res3["result"]["draft_id"])
        await pool.execute("UPDATE nac_connector_grants SET code_expires_at = now() - interval '1 second' "
                           "WHERE id=$1", gid3)
        rr = await post("/api/connector/pair", cap2, {"code": nc._codes[gid3][0], "account_id": "1"}, app=OTHER)
        await nc.list_connectors(pool)
        o = await pool.fetchrow("SELECT pairing, revoked_at FROM nac_connector_grants WHERE id=$1", gid3)
        check("an expired pairing takes no code and is closed as expired",
              rr.status_code == 403 and o["pairing"] == "expired" and o["revoked_at"] is not None, (rr.text, dict(o)))

        # ── 7. approval ──
        print("7. approval")
        c = await fresh_code()
        refused = await tools._exec_approve_nac_draft({"kind": "connector", "id": gid, "stepup_code": c})
        check("approve_nac_draft refuses kind connector", refused.get("status") != "ok"
              and "Network" in json.dumps(refused), refused)
        check("…without burning the code it was handed",
              await pool.fetchval("SELECT approved_at FROM nac_connector_grants WHERE id=$1", gid) is None)
        try:
            await tickets.decide_ticket_service(pool, ENTITY, g["ticket_id"], option_id="let_in",
                                                stepup_code=c, actor="host")
            check("the Host cannot answer the approval ticket", False)
        except tickets.TicketError as e:
            check("the Host cannot answer the approval ticket", e.status == 403, e.detail)
        for wrong in (None, "000000"):
            try:
                await tickets.decide_ticket_service(pool, ENTITY, g["ticket_id"], option_id="let_in",
                                                    stepup_code=wrong, actor="user")
                check(f"'Let this account in' with {wrong or 'no'} code is refused", False)
            except tickets.TicketError as e:
                check(f"'Let this account in' with {wrong or 'no'} code is refused", e.status == 403, e.detail)
        st = await pool.fetchrow("SELECT status FROM entity_tickets WHERE id=$1", g["ticket_id"])
        check("…and nothing changed: the card is still open, the grant not live",
              st["status"] == "open" and await pool.fetchval(
                  "SELECT approved_at FROM nac_connector_grants WHERE id=$1", gid) is None)
        out = await tickets.decide_ticket_service(pool, ENTITY, g["ticket_id"], option_id="let_in",
                                                  stepup_code=c, actor="user")
        gr = await pool.fetchrow("SELECT approved_at, approved_by, signed_terms, signature, stepup_at "
                                 "FROM nac_connector_grants WHERE id=$1", gid)
        terms = json.loads(gr["signed_terms"]) if isinstance(gr["signed_terms"], str) else gr["signed_terms"]
        check("the person's code approves it", gr["approved_at"] is not None and gr["stepup_at"] is not None, out)
        check("the signed terms are exactly {kind, app_id, platform, account_id}",
              terms == {"kind": "connector", "app_id": APP, "platform": "telegram", "account_id": "123456789"}, terms)
        pub = await nac_stepup.entity_verify_pubkey(pool, ENTITY)
        check("…signed with the Host's entity key", nc._terms_verify(pub, terms, gr["signature"]) == terms)
        check("the code was burned", not await nac_stepup.verify_stepup(pool, c))
        check("the ticket is decided", await pool.fetchval("SELECT status FROM entity_tickets WHERE id=$1",
                                                           g["ticket_id"]) == "decided")
        rr = await post("/api/connector/messages", cap, {"account_id": "123456789"})
        check("the same capability now opens the message routes (the route answers, not the gate)",
              opened(rr), (rr.status_code, rr.text))
        rr = await post("/api/connector/pair", cap, {"code": code, "account_id": "1"})
        check("…and /pair is closed (no open pairing)", rr.status_code == 403, rr.status_code)
        caller = await nc.check_connector_cap(pool, APP, cap)
        check("the caller carries the grant's SIGNED account and platform",
              caller.grant_id == gid and caller.account_id == "123456789" and caller.platform == "telegram", caller)

        # ── 8. re-pair: one account per app, one transaction ──
        print("8. re-pair")
        res4 = await tools._exec_propose_nac_policy({"kind": "connector", "app_id": APP, "platform": "telegram"})
        gid4 = int(res4["result"]["draft_id"])
        check("re-pairing keeps the live capability, with no restart",
              res4["result"]["capability"] == "live" and lane["release"].count(APP) == 1, lane)
        rr = await post("/api/connector/messages", cap, {"account_id": "123456789"})
        check("the old account keeps working while the new one pairs", opened(rr), rr.status_code)
        rr = await post("/api/connector/pair", cap, {"code": nc._codes[gid4][0], "account_id": "222333"})
        check("the new account pairs with the same capability", rr.status_code == 200, rr.text)
        # a race lost inside the approval leaves the older grant in force
        real_verify = nac_stepup.verify_stepup

        async def _verify_then_decline(p, code_, **kw):
            ok = await real_verify(p, code_, **kw)
            await pool.execute("UPDATE nac_connector_grants SET revoked_at=now(), revoke_reason='declined' "
                               "WHERE id=$1", gid4)
            return ok
        nac_stepup.verify_stepup = _verify_then_decline
        try:
            await net_routes.nac_connector_approve(gid4, _Req("network", {"stepup_code": await fresh_code()}), x_authentik_email=None)
            check("an approval that loses the race says so", False)
        except HTTPException as e:
            check("an approval that loses the race says so", e.status_code in (404, 409), e.detail)
        finally:
            nac_stepup.verify_stepup = real_verify
        check("…and rolled back whole: the older grant is still live, nothing cancelled",
              await pool.fetchval("SELECT revoked_at FROM nac_connector_grants WHERE id=$1", gid) is None
              and not cancelled, cancelled)
        res5 = await tools._exec_propose_nac_policy({"kind": "connector", "app_id": APP, "platform": "telegram"})
        gid5 = int(res5["result"]["draft_id"])
        await post("/api/connector/pair", cap, {"code": nc._codes[gid5][0], "account_id": "444555"})
        out = await net_routes.nac_connector_approve(gid5, _Req("network", {"stepup_code": await fresh_code()}), x_authentik_email=None)
        old = await pool.fetchrow("SELECT revoked_at, revoke_reason FROM nac_connector_grants WHERE id=$1", gid)
        live_n = await pool.fetchval("SELECT count(*) FROM nac_connector_grants WHERE app_id=$1 "
                                     "AND approved_at IS NOT NULL AND revoked_at IS NULL", APP)
        check("approving the new account revokes the old grant in the same step",
              old["revoked_at"] is not None and "replaced" in old["revoke_reason"] and live_n == 1, (dict(old), out))
        check("…and the old grant's turn is cancelled through the hook",
              [x[:2] for x in cancelled] == [(APP, gid)], cancelled)
        caller = await nc.check_connector_cap(pool, APP, cap)
        check("the capability is kept and now speaks for the new account",
              caller and caller.account_id == "444555" and caller.grant_id == gid5, caller)

        # ── 9. revoke: 403 on the next post, the hook runs, the app keeps running ──
        print("9. revoke")
        cancelled.clear()
        rv = await tools._exec_revoke_nac_rule({"kind": "connector", "id": gid5})
        rr = await post("/api/connector/messages", cap)
        check("revoke_nac_rule kind=connector revokes, and the very next post is 403",
              rv.get("status") == "ok" and rr.status_code == 403, (rv, rr.status_code))
        check("the turn in flight is cancelled through the hook", [x[:2] for x in cancelled] == [(APP, gid5)], cancelled)
        check("the capability is cleared", await pool.fetchval(
            "SELECT cap_hash FROM nac_connector_caps WHERE app_id=$1", APP) is None)
        check("the app is not stopped", lane["app_release"] == [], lane)
        res6 = await tools._exec_propose_nac_policy({"kind": "connector", "app_id": APP, "platform": "telegram"})
        newpend = await pool.fetchval("SELECT pending_hash FROM nac_connector_caps WHERE app_id=$1", APP)
        check("a pairing after a revoke mints a fresh capability",
              res6["result"]["capability"] == "delivering" and newpend and newpend != pend, res6)
        cap = await live_cap(APP)

        async def make_live(app_id, account):
            r_ = await tools._exec_propose_nac_policy({"kind": "connector", "app_id": app_id,
                                                       "platform": "telegram"})
            g_ = int(r_["result"]["draft_id"])
            await post("/api/connector/pair", cap, {"code": nc._codes[g_][0], "account_id": account}, app=app_id)
            await net_routes.nac_connector_approve(g_, _Req("network", {"stepup_code": await fresh_code()}), x_authentik_email=None)
            return g_

        g7 = await make_live(APP, "777")
        cancelled.clear()
        await net_routes.nac_connector_revoke(g7, _Req("network"))
        check("the Network app's Revoke: 403 next, hook ran",
              (await post("/api/connector/messages", cap)).status_code == 403
              and [x[:2] for x in cancelled] == [(APP, g7)], cancelled)
        # the uninstall paths
        await nc.issue_connector_cap(pool, APP)
        cap = await live_cap(APP)
        g8 = await make_live(APP, "888")
        cancelled.clear()
        await registry.uninstall(pool, APP)
        check("registry.uninstall revokes the grant, clears the capability and runs the hook",
              await pool.fetchval("SELECT revoked_at FROM nac_connector_grants WHERE id=$1", g8) is not None
              and await pool.fetchval("SELECT cap_hash FROM nac_connector_caps WHERE app_id=$1", APP) is None
              and [x[:2] for x in cancelled] == [(APP, g8)], cancelled)
        await install_row(pool, APP)
        await nc.issue_connector_cap(pool, APP)
        cap = await live_cap(APP)
        g9 = await make_live(APP, "999")
        cancelled.clear()
        installer.INSTALL_ROOT = Path(_TMP.name) / "apps"

        async def _no_runtime(*a, **k):
            return True
        installer.sync_app_runtime = _no_runtime
        await installer.uninstall_app(pool, APP)
        check("installer.uninstall_app revokes the grant, clears the capability and runs the hook",
              await pool.fetchval("SELECT revoked_at FROM nac_connector_grants WHERE id=$1", g9) is not None
              and await pool.fetchval("SELECT cap_hash FROM nac_connector_caps WHERE app_id=$1", APP) is None
              and [x[:2] for x in cancelled] == [(APP, g9)], cancelled)

        # ── 10. fail closed on a tampered grant ──
        print("10. a grant whose signature fails opens nothing")
        await install_row(pool, APP)
        await nc.issue_connector_cap(pool, APP)
        cap = await live_cap(APP)
        g10 = await make_live(APP, "1010")
        check("a live grant opens the message routes",
              opened(await post("/api/connector/messages", cap, {"account_id": "1010"})))
        manifests[APP] = _manifest(network=("api.telegram.org", "evil.example"))
        check("a re-install that widens network[] closes the routes on the next request",
              (await post("/api/connector/messages", cap)).status_code == 403)
        manifests[APP] = _manifest()
        check("…and the pinned manifest opens them again",
              opened(await post("/api/connector/messages", cap, {"account_id": "1010"})))
        await pool.execute("UPDATE nac_connector_grants SET signed_terms = jsonb_set(signed_terms, "
                           "'{account_id}', '\"6666\"') WHERE id=$1", g10)
        check("a grant edited after signing opens nothing", (await post("/api/connector/messages", cap)).status_code == 403)
        await pool.execute("UPDATE installed_applications SET status='disabled' WHERE id=$1", APP)
        check("nor does a capability whose app is no longer active",
              await nc.check_connector_cap(pool, APP, cap) is None)

        # ── 11. the capability's delivery, the person's cards, the reported name ──
        print("11. delivery keeps one value in flight; only the grant's own card is closed")
        T3, T4 = "tg-third", "tg-fourth"
        for a in (T3, T4):
            manifests[a] = _manifest()
            await install_row(pool, a)

        async def pending_hash(app_id):
            return await pool.fetchval("SELECT pending_hash FROM nac_connector_caps WHERE app_id=$1", app_id)

        # a second pairing while the first one's value is still on its way
        await tools._exec_propose_nac_policy({"kind": "connector", "app_id": T3, "platform": "telegram"})
        first_raw, first_h = nc._pending[T3]
        r2 = await tools._exec_propose_nac_policy({"kind": "connector", "app_id": T3, "platform": "telegram"})
        check("a second pairing while the capability is on its way keeps that value (no new mint)",
              r2["result"]["capability"] == "delivering" and nc._pending.get(T3, (None, None))[1] == first_h
              and await pending_hash(T3) == first_h, (first_h, nc._pending.get(T3), await pending_hash(T3)))
        check("…so the pod that started with the first value is the one the kernel switches to",
              await nc.on_wake_answer(T3, first_h, True)
              and await pool.fetchval("SELECT cap_hash FROM nac_connector_caps WHERE app_id=$1", T3) == first_h)
        nc._pending.pop(T4, None)
        await nc.issue_connector_cap(pool, T4)
        lost_h = nc._pending.pop(T4)[1]          # a kernel restart: the raw value is gone
        await tools._exec_propose_nac_policy({"kind": "connector", "app_id": T4, "platform": "telegram"})
        check("…but a pending value this kernel no longer holds is minted again",
              nc._pending.get(T4) and nc._pending[T4][1] != lost_h
              and await pending_hash(T4) == nc._pending[T4][1], (lost_h, await pending_hash(T4)))

        # wakes that carry a capability go one at a time
        class _GateBridge:
            def __init__(self):
                self._connected = True
                self.calls, self.inflight, self.max_inflight = [], 0, 0
                self.gate = asyncio.Event()
                self.answer = {"ok": True, "data": {"url": "10.0.0.9:8080", "creds_applied": True}}

            async def request_full(self, op, params=None):
                p = json.loads(json.dumps(params))
                self.calls.append(p)
                carries = "creds" in p
                if carries:
                    self.inflight += 1
                    self.max_inflight = max(self.max_inflight, self.inflight)
                try:
                    await self.gate.wait()
                finally:
                    if carries:
                        self.inflight -= 1
                return self.answer

        gb = _GateBridge()
        hb._bridge = lambda: gb
        w1 = asyncio.create_task(hb._wake_app(T4))
        await asyncio.sleep(0.05)
        await nc.revoke_connector(pool, app_id=T4, reason="revoked mid-wake", actor="user")
        await nc.issue_connector_cap(pool, T4)
        newer_raw, newer_h = nc._pending[T4]
        w2 = asyncio.create_task(hb._wake_app(T4))
        w3 = asyncio.create_task(hb._wake_app(T4))
        await asyncio.sleep(0.05)
        check("wakes that carry a capability are never in flight together for one app",
              gb.max_inflight == 1, gb.max_inflight)
        gb.gate.set()
        await asyncio.gather(w1, w2, w3)
        check("…the one queued behind sends the value pending when it is sent, and that one goes live",
              gb.calls[1].get("creds") == {"connector-cap": newer_raw}
              and await pool.fetchval("SELECT cap_hash FROM nac_connector_caps WHERE app_id=$1", T4) == newer_h
              and "creds" not in gb.calls[2], gb.calls)

        # a newer value minted while a delivery runs is delivered too
        awakes = []

        async def _awake_minting(app_id):
            awakes.append(app_id)
            if len(awakes) == 1:
                await nc.revoke_connector(pool, app_id=app_id, reason="revoked mid-delivery", actor="user")
                await nc.issue_connector_cap(pool, app_id)
            return None
        hb.ensure_awake_app = _awake_minting
        try:
            await nc.issue_connector_cap(pool, T4)
            await nc._deliver(T4)
            check("a revoke and a new pairing during a delivery get a second release and cold wake",
                  len(awakes) == 2, awakes)
            awakes.clear()
            awakes.append("skip the mint")
            await nc._deliver(T4)
            check("…while the same value still pending afterwards is not restarted in a loop",
                  len(awakes) == 2, awakes)
        finally:
            hb.ensure_awake_app = _awake

        # only the grant's own card is ever closed
        await nc.revoke_connector(pool, app_id=T3, reason="reset", actor="user")
        await nc.issue_connector_cap(pool, T3)
        cap3 = await live_cap(T3)

        async def matched(account):
            r_ = await tools._exec_propose_nac_policy({"kind": "connector", "app_id": T3,
                                                       "platform": "telegram"})
            g_ = int(r_["result"]["draft_id"])
            rr_ = await post("/api/connector/pair", cap3,
                             {"code": nc._codes[g_][0], "account_id": account}, app=T3)
            assert rr_.status_code == 200, rr_.text
            return g_

        async def unrelated(text):
            t_ = await tickets.create_ticket_service(
                pool, ENTITY, source="manual", sitrep=text, options=[{"id": "yes", "label": "Yes"}])
            return int(t_["id"])

        ga = await matched("31337")
        u1 = await unrelated("the person's own question, which a restore gave this ticket id")
        await pool.execute("UPDATE nac_connector_grants SET ticket_id=$2 WHERE id=$1", ga, u1)
        await net_routes.nac_connector_approve(ga, _Req("network", {"stepup_code": await fresh_code()}),
                                               x_authentik_email=None)
        u1row = await pool.fetchrow("SELECT status, decision FROM entity_tickets WHERE id=$1", u1)
        check("approving a grant whose ticket id now names another ticket leaves that ticket alone",
              await pool.fetchval("SELECT approved_at FROM nac_connector_grants WHERE id=$1", ga) is not None
              and u1row["status"] == "open" and u1row["decision"] is None, dict(u1row))
        gb_ = await matched("4242")
        u2 = await unrelated("another of the person's questions")
        await pool.execute("UPDATE nac_connector_grants SET ticket_id=$2 WHERE id=$1", gb_, u2)
        await net_routes.nac_connector_decline(gb_, _Req("network"))
        check("…and so does declining one",
              await pool.fetchval("SELECT status FROM entity_tickets WHERE id=$1", u2) == "open")

        # a card filed for a grant closed in the meantime is not left open
        gc = await matched("5151")
        own = await pool.fetchval("SELECT ticket_id FROM nac_connector_grants WHERE id=$1", gc)
        await nc.decline_connector(pool, gc, actor="user")
        late = await nc._file_approval_ticket(pool, gc)
        open_cards = await pool.fetch(
            "SELECT id FROM entity_tickets WHERE status='open' AND metadata->>'kind'='connector_pairing' "
            "AND metadata->'connector_pairing'->>'grant_id' = $1", str(gc))
        check("a card filed after its pairing closed is dismissed and not named on the grant",
              late is None and not open_cards
              and await pool.fetchval("SELECT ticket_id FROM nac_connector_grants WHERE id=$1", gc) == own,
              (late, [r["id"] for r in open_cards]))

        # the reported name cannot reorder the card around the account id
        _acc, nm = nc.check_account("telegram", "123456789", "Alex‮)987654321 di(‍⁦x")
        check("bidi overrides, isolates and zero-width characters are dropped from the reported name",
              not any(ch in nm for ch in "‮‍⁦") and nm.startswith("Alex"), ascii(nm))

        # ── 12. the 'matched' stage: what it opens, which key it binds, when it ends ──
        print("12. the matched stage")
        from server.vault import api as vault_api
        KEY_A, KEY_B = "tg-key-approved", "tg-key-proposed"
        for nm_ in (KEY_A, KEY_B):
            await vault_api.store_secret(pool, entity_id=ENTITY, name=nm_, cred_type="token",
                                         plaintext=b"123:" + nm_.encode(), actor="gate")
        T5 = "tg-fifth"
        manifests[T5] = _manifest()
        await install_row(pool, T5)

        async def pair_code(gid_, account):
            return await post("/api/connector/pair", cap5,
                              {"code": nc._codes[gid_][0], "account_id": account}, app=T5)

        async def approve(gid_):
            await net_routes.nac_connector_approve(
                gid_, _Req("network", {"stepup_code": await fresh_code()}), x_authentik_email=None)

        g51 = int((await nc.propose_connector(pool, app_id=T5, platform="telegram",
                                              token_name=KEY_A))["draft_id"])
        cap5 = await live_cap(T5)
        rr = await pair_code(g51, "5001")
        c5 = await nc.check_connector_cap(pool, T5, cap5)
        check("a matched code alone: the 'matched' stage only, bound to the matched grant's key",
              rr.status_code == 200 and c5 is not None and c5.stages == frozenset({nc.STAGE_MATCHED})
              and c5.token_name == KEY_A and c5.grant_id is None and c5.account_id is None, c5)
        for path, m in (("/api/connector/pair", "POST"), ("/api/connector/messages", "POST"),
                        ("/api/connector/messages/sent", "POST"), ("/api/connector/turns/t1", "GET")):
            rr = await post(path, cap5, {"code": "BBBBBBBB", "account_id": "5001"}, app=T5, method=m)
            check(f"…it never opens {m} {path} (403 at the gate)", rr.status_code == 403,
                  (rr.status_code, rr.text))
        await approve(g51)
        c5 = await nc.check_connector_cap(pool, T5, cap5)
        check("approval ends it: 'active' alone, the approved key",
              c5.stages == frozenset({nc.STAGE_ACTIVE}) and c5.token_name == KEY_A
              and c5.grant_id == g51, c5)
        # Another bot (another key) cannot be paired while a grant is live: the
        # connector reaches the platform with the LIVE grant's key until the
        # approval, so it would poll the old bot and never see the code sent to
        # the new one. Revoke first, then pair the new bot.
        n_before = await pool.fetchval("SELECT count(*) FROM nac_connector_grants WHERE app_id=$1", T5)
        try:
            await nc.propose_connector(pool, app_id=T5, platform="telegram", token_name=KEY_B)
            refused = None
        except nc.ConnectorError as e:
            refused = (e.status, e.detail)
        check("pairing another key while a grant is live is refused, saying to revoke first, "
              "and changes nothing",
              refused is not None and refused[0] == 409 and "revoke" in refused[1]
              and await pool.fetchval("SELECT count(*) FROM nac_connector_grants WHERE app_id=$1", T5)
              == n_before, refused)
        g52 = int((await nc.propose_connector(pool, app_id=T5, platform="telegram",
                                              token_name=KEY_A))["draft_id"])
        await pair_code(g52, "5002")
        c5 = await nc.check_connector_cap(pool, T5, cap5)
        check("a re-pair of the same bot whose code matched: active and matched, bound to the live "
              "grant's approved key and account until the person approves",
              c5.stages == frozenset({nc.STAGE_ACTIVE, nc.STAGE_MATCHED}) and c5.token_name == KEY_A
              and c5.grant_id == g51 and c5.account_id == "5001", c5)
        await nc.decline_connector(pool, g52, actor="user")
        c5 = await nc.check_connector_cap(pool, T5, cap5)
        check("a decline ends the matched stage; the live grant stays",
              c5.stages == frozenset({nc.STAGE_ACTIVE}) and c5.grant_id == g51, c5)
        g53 = int((await nc.propose_connector(pool, app_id=T5, platform="telegram",
                                              token_name=KEY_A))["draft_id"])
        await pair_code(g53, "5003")
        r54 = await nc.propose_connector(pool, app_id=T5, platform="telegram", token_name=KEY_A)
        g54 = int(r54["draft_id"])
        c5 = await nc.check_connector_cap(pool, T5, cap5)
        check("a newer pairing voids the matched one: pairing again, not matched",
              c5.stages == frozenset({nc.STAGE_ACTIVE, nc.STAGE_PAIRING}) and g53 in r54["replaced"],
              (c5, r54))
        await pair_code(g54, "5004")
        await approve(g54)
        c5 = await nc.check_connector_cap(pool, T5, cap5)
        check("approving the re-pair binds its account from then on",
              c5.stages == frozenset({nc.STAGE_ACTIVE}) and c5.token_name == KEY_A
              and c5.grant_id == g54 and c5.account_id == "5004", c5)
        await nc.revoke_connector(pool, app_id=T5, reason="switching bots", actor="user")
        g56 = int((await nc.propose_connector(pool, app_id=T5, platform="telegram",
                                              token_name=KEY_B))["draft_id"])
        cap5 = await live_cap(T5)
        await pair_code(g56, "5006")
        await approve(g56)
        c5 = await nc.check_connector_cap(pool, T5, cap5)
        check("after a revoke, another bot pairs and binds its own key",
              c5 is not None and c5.stages == frozenset({nc.STAGE_ACTIVE}) and c5.token_name == KEY_B
              and c5.grant_id == g56 and c5.account_id == "5006", c5)
        g55 = int((await nc.propose_connector(pool, app_id=T5, platform="telegram",
                                              token_name=KEY_B))["draft_id"])
        await pair_code(g55, "5005")
        await nc.revoke_connector(pool, app_id=T5, reason="the gate revokes", actor="user")
        check("a revoke ends the matched stage with the rest: the capability opens nothing",
              await nc.check_connector_cap(pool, T5, cap5) is None
              and (await post("/api/connector/platform", cap5, {}, app=T5)).status_code == 403)
    finally:
        await pool.close()


def main() -> int:
    server = os.environ.get("CONNECTOR_TEST_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if not server and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test database and docker unavailable — this gate proves nothing without one.",
              file=sys.stderr)
        return 2
    with (contextlib.nullcontext(server) if server else throwaway_server()) as srv:
        with kernel_schema_db(srv, entity_id=ENTITY) as dsn:
            print("connector grants against a real PostgreSQL, on the kernel's schema")
            asyncio.run(run(dsn))
    print(f"\n{PASSES}/{PASSES + len(FAILURES)} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
