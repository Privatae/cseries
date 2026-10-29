#!/usr/bin/env python3
"""An outside agent's whole path through this Host, on the kernel's schema.

An outside agent (Claude Code, Codex, an agent inside T3 Code) reaches the
person's machines only by asking the Host over MCP; what the person approved
for it runs, anything new comes back as a link to a page on the Host
(server/agent_access, migrations/v255_agent_access.sql).

Driven through the real routes (agent_access.oauth, agent_access.asks,
routes/mcp) with httpx against the ASGI app, on a fresh clone of the kernel's
schema, so the SQL that spends codes, rotates tokens and binds a 'just this
once' approval to exact arguments is the SQL that runs. The machine side
(nac_mgmt.run_mgmt_op, nac_commit.bastions_list) is stubbed: it has its own
gates, and what is proved here is what reaches it and what does not.

  * /mcp without a token: 401 naming the protected-resource metadata
  * metadata, registration (and its refusals), the connect page behind the
    person's identity and their authenticator code, PKCE, one-time codes,
    refresh rotation, revocation
  * the agent's tools: a read runs on 'read'; a change files an ask and
    answers -32042 with its link (or the link in text for a client without
    URL elicitation), and nothing reaches the machine until the person says
    once / always; a refusal stands; a machine holding the op for its own
    approval gets the same link and its card is answered from the page

Run:  python3 scripts/test_agent_access_pg.py
      (TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN names a server; without one it
      starts a throwaway one through docker, as the other _pg gates do.)
"""
import asyncio
import base64
import contextlib
import hashlib
import html
import os
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

import asyncpg  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the SQL behind an agent's codes, tokens and asks.")
    sys.exit(2)

ENTITY = 9001
# Invented: the person's sign-in as the front door would vouch for it.
PERSON = "owner@example.com"
os.environ["ENTITY_OPERATOR_EMAILS"] = PERSON
os.environ["ENTITY_ID"] = str(ENTITY)

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402
from server import brain_service  # noqa: E402
from server.agent_access import asks as agent_asks, oauth as agent_oauth, store  # noqa: E402
from server.network import nac_commit, nac_mgmt, nac_stepup  # noqa: E402
from server.routes import mcp as mcp_route, tickets  # noqa: E402

FAILS: list[str] = []
CHECKS = 0
REDIRECT = "http://127.0.0.1:33418/callback"


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:600]) if extra else ""))
    if not cond:
        FAILS.append(msg)


# ── the machine side, stubbed ───────────────────────────────────────────────

RAN: list[tuple] = []
MACHINE_STATE: dict = {"next": None}


async def fake_run_mgmt_op(pool, eid, server, op, args, *, token_id=None, note="",
                           approval_wait_s=None, origin="service", budget_s=None):
    RAN.append((server, op, list(args or []), origin, approval_wait_s, note))
    held = MACHINE_STATE.get("next")
    if held is not None:
        MACHINE_STATE["next"] = None
        if isinstance(held, Exception):
            raise held
        return held
    return {"server": server, "op": op, "exit": 0, "stdout": f"{op} ok", "stderr": ""}


async def fake_bastions_list(pool, eid):
    return {"bastions": [{"hostname": "box-a", "online": True, "platform": "linux",
                          "bastion_version": "0.53.0",
                          "enforcing": {"mgmt_ops": ["uptime", "restart_service"]}}]}


DECIDED: list[tuple] = []


async def fake_get_ticket(pool, eid, ticket_id):
    return {"id": ticket_id, "status": "open",
            "sitrep": "box-a asks to run restart_service once. Approve to send it to your device."}


async def fake_decide(pool, eid, ticket_id, *, option_id=None, actor="user", **kw):
    DECIDED.append((ticket_id, option_id, actor))
    return {"ticket_id": ticket_id, "decision": option_id}


# The Host's own pod gate (nac_mgmt.gate_op): it allows every (machine, op)
# here except those in GATE_DENY, which it refuses as the real one words it.
GATE_DENY: set = set()
GATE_ASKED: list[tuple] = []
# The reason the gate gives for a (machine, op) in GATE_DENY, when not the
# default-deny's: the person's own words in their grant (nac_mgmt.deny_reason).
GATE_WHY: dict = {}


async def fake_gate_op(pool, eid, server, op):
    GATE_ASKED.append((server, op))
    if (server, op) in GATE_DENY:
        return False, GATE_WHY.get((server, op), "no signed grant (default-deny)"), "denied-pod"
    return True, "", "ran"


REAL_RUN_MGMT_OP, REAL_GATE_OP = nac_mgmt.run_mgmt_op, nac_mgmt.gate_op
nac_mgmt.run_mgmt_op = fake_run_mgmt_op
nac_mgmt.gate_op = fake_gate_op
nac_commit.bastions_list = fake_bastions_list
tickets.get_ticket_service = fake_get_ticket
tickets.decide_ticket_service = fake_decide


# ── helpers ─────────────────────────────────────────────────────────────────

def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class Clock:
    """nac_stepup's clock, moved on a window for each code instead of waiting
    30 seconds: verify_stepup burns each window's code once, so every
    approval in this gate takes the next window's code, read at that window."""

    def __init__(self):
        self.now = float(int(time.time()) // 30 * 30) + 5

    def time(self) -> float:
        return self.now


CLOCK = Clock()
nac_stepup.time = CLOCK


class Codes:
    """The person's authenticator, on CLOCK: next() moves to a fresh window
    and returns its code; peek() is the current window's, not yet used."""

    def __init__(self, secret_b32: str):
        self.raw = nac_stepup._b32decode(secret_b32)

    def peek(self) -> str:
        return nac_stepup._hotp(self.raw, int(CLOCK.now) // 30)

    def next(self) -> str:
        CLOCK.now += 30
        return self.peek()


def form_token(page: str) -> str:
    m = re.search(r'name="form" value="([^"]+)"', page)
    return html.unescape(m.group(1)) if m else ""


def split_agent_words(page: str) -> tuple[str, str]:
    """(the page outside the agent's own-words box, the box). The box is the
    one place an agent's name or note may stand."""
    m = re.search(r'<div class="box agent-words">.*?</div>', page, re.S)
    if not m:
        return page, ""
    return page[:m.start()] + page[m.end():], m.group(0)


async def rpc(c, token, method, params=None, rid=1):
    r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                     headers={"Authorization": f"Bearer {token}"} if token else {})
    return r


async def call_tool(c, token, name, args, rid=7):
    r = await rpc(c, token, "tools/call", {"name": name, "arguments": args}, rid)
    return r.json()


async def main(dsn: str) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    brain_service.get_pool = lambda: pool
    brain_service._entity_id = ENTITY
    app = FastAPI()
    app.include_router(mcp_route.router)
    app.include_router(agent_oauth.router)
    app.include_router(agent_asks.router)
    person = {"X-Authentik-Email": PERSON}
    secret = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    codes = Codes(secret)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://host.test") as c:
            # ── discovery ──
            r = await rpc(c, None, "initialize")
            www = r.headers.get("www-authenticate", "")
            check(r.status_code == 401 and 'resource_metadata="http://host.test/.well-known/'
                  'oauth-protected-resource/mcp"' in www,
                  "/mcp without a token: 401 naming the protected-resource metadata", (r.status_code, www))
            r = await rpc(c, "agt_not-a-real-token", "initialize")
            check(r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", ""),
                  "/mcp with an unknown agent token: the same 401")
            pr = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
            check(pr.get("resource") == "http://host.test/mcp"
                  and pr.get("authorization_servers") == ["http://host.test"],
                  "protected-resource metadata names /mcp and this Host as its server", pr)
            md = (await c.get("/.well-known/oauth-authorization-server")).json()
            check(md.get("issuer") == "http://host.test" and md.get("code_challenge_methods_supported") == ["S256"]
                  and md.get("registration_endpoint", "").endswith("/oauth/register")
                  and md.get("client_id_metadata_document_supported") is False,
                  "authorization-server metadata: S256 only, registration, no CIMD", md)

            # ── registration ──
            r = await c.post("/oauth/register", json={"client_name": "x", "redirect_uris": ["http://evil.example/cb"]})
            check(r.status_code == 400 and r.json().get("error") == "invalid_redirect_uri",
                  "registration refuses a plain-http redirect off the loopback", r.text)
            r = await c.post("/oauth/register", json={"client_name": "x", "redirect_uris": [REDIRECT],
                                                      "token_endpoint_auth_method": "client_secret_basic"})
            check(r.status_code == 400, "registration refuses a confidential client", r.text)
            r = await c.post("/oauth/register", json={"client_name": "Agent <script>alert(1)</script>",
                                                      "redirect_uris": [REDIRECT]})
            reg = r.json()
            client_id = reg.get("client_id", "")
            check(r.status_code == 201 and client_id.startswith("agent-")
                  and reg.get("token_endpoint_auth_method") == "none",
                  "registration hands out a public client id", reg)

            # ── the connect page ──
            verifier, challenge = pkce()
            q = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
                 "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
                 "resource": "http://host.test/mcp"}
            r = await c.get("/oauth/authorize", params=q)
            check(r.status_code == 401 and "Sign in" in r.text, "the connect page needs the person's sign-in")
            r = await c.get("/oauth/authorize", params={**q, "redirect_uri": "http://127.0.0.1:1/other"},
                            headers=person)
            check(r.status_code == 400 and r.headers.get("location") is None,
                  "an unregistered redirect is refused on the page, never followed")
            r = await c.get("/oauth/authorize", params={**q, "code_challenge_method": "plain"}, headers=person)
            check(r.status_code == 400 and r.headers.get("location") is None and "PKCE" in r.text,
                  "no S256 challenge: refused on the page, the person is not sent on to the agent's address",
                  (r.status_code, r.headers.get("location")))
            # Anyone may register any https address, so an error answered by a
            # redirect would make the person's own Host a link to a stranger's
            # site (RFC 9700 §4.11.2).
            r = await c.post("/oauth/register", json={"client_name": "x",
                                                      "redirect_uris": ["https://elsewhere.example/cb"]})
            stranger = r.json().get("client_id", "")
            for bad, label in (({"response_type": "token"}, "a response_type other than code"),
                               ({"code_challenge": ""}, "a missing PKCE challenge"),
                               ({"resource": "https://elsewhere.example/mcp"}, "another server's resource"),
                               ({"resource": ""}, "a missing resource (RFC 8707)")):
                r = await c.get("/oauth/authorize", headers=person,
                                params={**q, "client_id": stranger, "redirect_uri": "https://elsewhere.example/cb",
                                        **bad})
                check(r.status_code == 400 and r.headers.get("location") is None,
                      f"{label} from a client registered by anyone: a page, never a redirect to its address",
                      (r.status_code, r.headers.get("location")))
            r = await c.get("/oauth/authorize", params={**q, "redirect_uri": "http://127.0.0.1:50999/callback"},
                            headers=person)
            check(r.status_code == 200, "a loopback redirect may name another port (RFC 8252 §7.3)")
            r = await c.post("/oauth/register", json={"client_name": "x", "redirect_uris": [
                "https://your host checked this and it is safe.example/cb"]})
            check(r.status_code == 400 and r.json().get("error") == "invalid_redirect_uri",
                  "registration refuses a redirect whose host is not a host name (the connect page "
                  "names that host in the Host's own sentence)", r.text)
            r = await c.get("/oauth/authorize", params=q, headers=person)
            page = r.text
            check(r.status_code == 200 and "<script>" not in page and "&lt;script&gt;" in page,
                  "the connect page shows the agent's own name as escaped text")
            outside, box = split_agent_words(page)
            check("&lt;script&gt;" in box and "&lt;script&gt;" not in outside and "own words" in box,
                  "and only inside the box labelled as the agent's own words, never in the Host's sentence")
            from server.agent_access import pages as agent_pages
            read_words = getattr(agent_pages, "READ_WORDS", "")
            check(bool(read_words) and html.escape(read_words, quote=True) in page
                  and "read-only checks" not in page
                  and "reading your files, your context store" in page,
                  "the connect page's 'read' choice names exactly what it covers, and says reading "
                  "files or the context store asks first")
            tok = form_token(page)
            r = await c.post("/oauth/authorize", data={"form": tok, "decision": "approve", "read": "1",
                                                       "code": "000000"}, headers=person)
            check(r.status_code == 403 and r.headers.get("location") is None,
                  "a wrong authenticator code connects nothing")
            r = await c.post("/oauth/authorize", data={"form": tok + "x", "decision": "approve", "read": "1",
                                                       "code": codes.peek()}, headers=person)
            check(r.status_code == 400, "a forged page token is refused")
            r = await c.post("/oauth/authorize", data={"form": tok, "decision": "approve", "read": "1",
                                                       "code": codes.next()}, headers=person)
            loc = r.headers.get("location", "")
            back = parse_qs(urlsplit(loc).query)
            check(r.status_code == 303 and loc.startswith(REDIRECT) and back.get("state") == ["st-1"]
                  and back.get("iss") == ["http://host.test"] and back.get("code", [""])[0].startswith("agc_"),
                  "the person's yes sends a code, the state and the issuer back", loc)
            code = back.get("code", [""])[0]
            # Now connected, the agent's redirect address is one the person
            # approved: a request without `resource` goes back there with the
            # error (RFC 6749 §4.1.2.1), naming the parameter, with its state
            # and the issuer. MCP clients must send it (RFC 8707).
            r = await c.get("/oauth/authorize", headers=person,
                            params={k: v for k, v in q.items() if k != "resource"})
            loc = r.headers.get("location", "")
            back_err = parse_qs(urlsplit(loc).query)
            check(r.status_code == 303 and loc.startswith(REDIRECT)
                  and back_err.get("error") == ["invalid_request"]
                  and "resource parameter" in back_err.get("error_description", [""])[0]
                  and back_err.get("state") == ["st-1"] and back_err.get("iss") == ["http://host.test"]
                  and "code" not in back_err,
                  "a connected agent's request without resource: back to its address with "
                  "error=invalid_request naming the parameter, and no code", (r.status_code, loc))

            # ── tokens ──
            r = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": REDIRECT, "client_id": client_id,
                                                   "code_verifier": verifier[:-1] + ("A" if verifier[-1] != "A" else "B")})
            check(r.status_code == 400 and r.json().get("error") == "invalid_grant",
                  "a wrong PKCE verifier gets no token", r.text)
            r = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": REDIRECT, "client_id": client_id,
                                                   "code_verifier": verifier})
            check(r.status_code == 400, "and the code is spent by that attempt (single use)", r.text)
            # a second connect round for the working token
            verifier, challenge = pkce()
            q.update(code_challenge=challenge, state="st-2")
            tok = form_token((await c.get("/oauth/authorize", params=q, headers=person)).text)
            r = await c.post("/oauth/authorize", data={"form": tok, "decision": "approve", "read": "1",
                                                       "code": codes.next()}, headers=person)
            code = parse_qs(urlsplit(r.headers.get("location", "")).query).get("code", [""])[0]
            r = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": REDIRECT, "client_id": client_id,
                                                   "code_verifier": verifier})
            t = r.json()
            access, refresh = t.get("access_token", ""), t.get("refresh_token", "")
            check(r.status_code == 200 and access.startswith("agt_") and refresh.startswith("agr_")
                  and r.headers.get("cache-control") == "no-store",
                  "the right verifier gets an access and a refresh token", t)
            raw_rows = await pool.fetchval("SELECT count(*) FROM agent_tokens WHERE token_hash = $1",
                                           access.encode())
            hashed = await pool.fetchval("SELECT count(*) FROM agent_tokens WHERE token_hash = $1",
                                         hashlib.sha256(access.encode()).digest())
            check(raw_rows == 0 and hashed == 1, "a token is stored as its SHA-256, never its value")

            # ── the agent's MCP session ──
            r = await rpc(c, access, "initialize", {"protocolVersion": "2025-11-25",
                                                    "capabilities": {"elicitation": {"url": {}, "form": {}}},
                                                    "clientInfo": {"name": "gate", "version": "1"}})
            check(r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-11-25",
                  "initialize answers the client's own 2025-11-25", r.text)
            from server.routes import system as system_route
            built = str(system_route._load_build_info().get("tag") or "dev")
            check(r.json()["result"]["serverInfo"] == {"name": "host", "version": built},
                  "serverInfo names the Host's own version (build-info's tag, as Settings shows it)",
                  (r.json()["result"].get("serverInfo"), built))
            note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            h = {"Authorization": f"Bearer {access}"}
            r1 = await c.post("/mcp", json=note, headers=h)
            r2 = await c.post("/mcp", json=[note, note], headers=h)
            check(r1.status_code == 202 and r1.content == b"" and r2.status_code == 202 and r2.content == b"",
                  "a notification, alone or in a batch, is answered 202 Accepted with no body "
                  "(MCP Streamable HTTP)", (r1.status_code, r2.status_code))
            names = [x["name"] for x in (await rpc(c, access, "tools/list")).json()["result"]["tools"]]
            check(names == ["machines", "machine_op"], "an agent sees the machine tools and nothing else", names)
            out = await call_tool(c, access, "machines", {})
            check(out["result"]["structuredContent"]["machines"][0]["name"] == "box-a",
                  "machines: the person's machines, with read", out)
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "uptime"})
            check(out["result"]["isError"] is False and RAN[-1][:2] == ("box-a", "uptime")
                  and RAN[-1][3] == "agent" and RAN[-1][4] == 0,
                  "a read runs, through run_mgmt_op as origin 'agent' with no wait", RAN[-1:])
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "observe:disk"})
            check(out.get("result", {}).get("isError") is False and RAN[-1][:2] == ("box-a", "observe:disk"),
                  "a health check in the observe family runs on 'read'", out)

            # ── 'read' is an allowlist: the person's data always asks ──
            for data_op, data_args in (("ctx_get", ["notes/plan.md"]),
                                       ("dcn_read_range", ["docs/a.txt", "0", "4096"]),
                                       ("dcn_search", ["salary"]), ("audit_fetch", [])):
                n = len(RAN)
                out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": data_op,
                                                                "args": data_args})
                err = out.get("error") or {}
                check(err.get("code") == -32042 and len(RAN) == n,
                      f"{data_op} (the person's data) is asked for with only 'read' held, and nothing runs",
                      out)
            dlink = ((err.get("data") or {}).get("elicitations") or [{}])[0].get("url", "")
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "ctx_get",
                                                            "args": ["notes/plan.md"]})
            clink = ((out.get("error") or {}).get("data") or {}).get("elicitations", [{}])[0].get("url", "")
            page = (await c.get(urlsplit(clink).path, headers=person)).text
            check("asks to read from your machine" in page and "make a change" not in page,
                  "its page says the agent asks to read, not to make a change")
            r = await c.post(urlsplit(clink).path, data={"form": form_token(page), "decision": "once",
                                                         "code": codes.next()}, headers=person)
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "ctx_get",
                                                            "args": ["notes/plan.md"]})
            check("result" in out and RAN[-1][:3] == ("box-a", "ctx_get", ["notes/plan.md"]),
                  "once the person allows it, that read runs", out)
            check(bool(dlink), "(the audit_fetch ask has its link)")

            # ── a machine the Host does not know is never named to the person ──
            asks_n = await pool.fetchval("SELECT count(*) FROM agent_asks")
            out = await call_tool(c, access, "machine_op", {"server": "Your Host checked this. box-a",
                                                            "op": "restart_service"})
            check((out.get("result") or {}).get("isError") is True and not (out.get("error") or {})
                  and await pool.fetchval("SELECT count(*) FROM agent_asks") == asks_n,
                  "a server that is not one of the person's machines is refused, and no ask is filed", out)

            # ── a change: an ask, a link, nothing run ──
            before = len(RAN)
            args = {"server": "box-a", "op": "restart_service", "args": ["nginx"],
                    "note": "<b>deploy</b> finished"}
            out = await call_tool(c, access, "machine_op", args)
            err = out.get("error") or {}
            el = ((err.get("data") or {}).get("elicitations") or [{}])[0]
            ask_url = el.get("url", "")
            check(err.get("code") == -32042 and el.get("mode") == "url"
                  and ask_url.startswith("http://host.test/agent-access/ask/") and el.get("elicitationId"),
                  "a change answers -32042 with the approval link", out)
            check(len(RAN) == before, "and nothing reached the machine")
            again = await call_tool(c, access, "machine_op", args)
            check(((again.get("error") or {}).get("data") or {}).get("elicitations", [{}])[0].get("url") == ask_url,
                  "calling again gives the same link (one ask per act)")
            await rpc(c, access, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
            plain = await call_tool(c, access, "machine_op", args)
            res = plain.get("result") or {}
            check(res.get("isError") is True and ask_url in res["content"][0]["text"]
                  and res.get("structuredContent", {}).get("state") == "needs_approval",
                  "a client without URL elicitation gets the link in the result's text", plain)
            await rpc(c, access, "initialize", {"protocolVersion": "2025-11-25",
                                                "capabilities": {"elicitation": {"url": {}}}})

            path = urlsplit(ask_url).path
            r = await c.get(path)
            check(r.status_code == 401, "the approval page needs the person's sign-in")
            r = await c.get(path, headers=person)
            page = r.text
            check(r.status_code == 200 and "restart_service" in page and "box-a" in page
                  and "<b>deploy</b>" not in page and "&lt;b&gt;deploy&lt;/b&gt;" in page,
                  "the approval page names the op and machine; the agent's note is escaped text")
            outside, box = split_agent_words(page)
            check("&lt;b&gt;deploy&lt;/b&gt;" in box and "deploy" not in outside
                  and "&lt;script&gt;" in box and "&lt;script&gt;" not in outside
                  and "Its reason" in box and "own words" in box,
                  "the agent's note and name stand only in the box labelled as its own words, never in "
                  "the Host's sentence")
            tok = form_token(page)
            r = await c.post(path, data={"form": tok, "decision": "once", "code": "111111"}, headers=person)
            check(r.status_code == 403, "a wrong code allows nothing")
            r = await c.post(path, data={"form": tok, "decision": "once", "code": codes.next()}, headers=person)
            check(r.status_code == 200 and "once" in r.text, "the person allows it just this once", r.text[-300:])
            out = await call_tool(c, access, "machine_op", args)
            check("result" in out and RAN[-1][:3] == ("box-a", "restart_service", ["nginx"]),
                  "the agent's next identical call runs it", out)
            check(RAN[-1][5] == "",
                  "and the agent's note is not handed to the machine (its card and device ask carry "
                  "only the Host's and the machine's words)", RAN[-1:])
            out = await call_tool(c, access, "machine_op", {**args, "args": ["postgres"]})
            check((out.get("error") or {}).get("code") == -32042,
                  "'once' bound the exact arguments: another argument asks again")
            n = len(RAN)
            out = await call_tool(c, access, "machine_op", args)
            check((out.get("error") or {}).get("code") == -32042 and len(RAN) == n,
                  "and 'once' is spent: the same call asks again")

            spent_out = out  # the 'always' section below answers this ask
            # ── the Host's own permissions come first ──
            conn_now = await pool.fetchval("SELECT id FROM agent_connections WHERE client_id=$1", client_id)
            asks_n = await pool.fetchval("SELECT count(*) FROM agent_asks")
            n = len(RAN)
            GATE_DENY.add(("box-a", "reboot"))
            # Invented: the person's own words in their grant.
            GATE_WHY[("box-a", "reboot")] = 'denied by your grant — you said: "never the drawing tablet"'
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "reboot", "args": []})
            res = out.get("result") or {}
            text = (res.get("content") or [{}])[0].get("text", "")
            last = await pool.fetchval("SELECT outcome FROM agent_calls WHERE connection_id=$1 "
                                       "ORDER BY id DESC LIMIT 1", conn_now)
            check(res.get("isError") is True and not (out.get("error") or {}) and len(RAN) == n
                  and "not permitted by this Host's own permissions" in text
                  and await pool.fetchval("SELECT count(*) FROM agent_asks") == asks_n
                  and last == "refused: the Host's own permissions"
                  and GATE_ASKED[-1] == ("box-a", "reboot"),
                  "an op the Host's own gate refuses: an error, no ask and no link for the "
                  "person, nothing run, recorded as refused by the Host's own permissions", (out, last))
            check("drawing tablet" not in text and "denied by your grant" not in text,
                  "and an agent the person never let run that op is not told why: the person's own "
                  "words in their grant (or a quarantine) stay on the Host", text)
            # An agent the person let run the op 'always' is told why, as it was
            # by the run itself before this check came first.
            await pool.execute("INSERT INTO agent_grants (connection_id, kind, server, op) "
                               "VALUES ($1, 'op', 'box-a', 'reboot')", conn_now)
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "reboot", "args": []})
            text = ((out.get("result") or {}).get("content") or [{}])[0].get("text", "")
            check("not permitted — denied by your grant" in text and "drawing tablet" in text and len(RAN) == n,
                  "an agent holding the op 'always' gets the Host's reason, and nothing runs", out)
            await pool.execute("UPDATE agent_grants SET revoked_at = now() WHERE connection_id=$1 "
                               "AND kind='op' AND server='box-a' AND op='reboot'", conn_now)
            GATE_WHY.clear()

            # Allowed once, then the Host's gate refuses it at the run (a grant
            # taken back in between): the person's 'once' is given back.
            fargs = {"server": "box-a", "op": "flush_cache", "args": ["x"]}
            out = await call_tool(c, access, "machine_op", fargs)
            fpath = urlsplit(out["error"]["data"]["elicitations"][0]["url"]).path
            page = (await c.get(fpath, headers=person)).text
            await c.post(fpath, data={"form": form_token(page), "decision": "once", "code": codes.next()},
                         headers=person)
            MACHINE_STATE["next"] = {"server": "box-a", "op": "flush_cache", "exit": 126, "stdout": "",
                                     "stderr": "op 'flush_cache' on 'box-a' not permitted — no signed "
                                               "grant (default-deny)",
                                     "refused_by": getattr(nac_mgmt, "REFUSED_BY_HOST", "host")}
            out = await call_tool(c, access, "machine_op", fargs)
            res = out.get("result") or {}
            state = await pool.fetchval("SELECT state FROM agent_asks WHERE id=$1", fpath.rsplit("/", 1)[1])
            last = await pool.fetchval("SELECT outcome FROM agent_calls WHERE connection_id=$1 "
                                       "ORDER BY id DESC LIMIT 1", conn_now)
            check(res.get("isError") is True and not (out.get("error") or {})
                  and "not permitted" in (res.get("content") or [{}])[0].get("text", "")
                  and state == "once" and last == "refused: the Host's own permissions",
                  "the Host's gate refusing at the run: an error with its words, the 'once' given back, "
                  "never recorded as ran", (out, state, last))
            out = await call_tool(c, access, "machine_op", fargs)
            check("result" in out and not out["result"].get("isError") and RAN[-1][1] == "flush_cache",
                  "so once the Host may run it, the person's 'once' still runs it", out)

            # Every other way a call ends with nothing reaching the machine
            # gives the 'once' back as well, read from run_mgmt_op's NOT_RUN
            # code (never its words), and is recorded as not run: the machine
            # can take no approval, the person refused at their device, the
            # act was already done, an error before the machine took the job.
            async def once_for(arg):
                a = {"server": "box-a", "op": "drain_queue", "args": [arg]}
                o = await call_tool(c, access, "machine_op", a)
                p = urlsplit(o["error"]["data"]["elicitations"][0]["url"]).path
                pg = (await c.get(p, headers=person)).text
                await c.post(p, data={"form": form_token(pg), "decision": "once", "code": codes.next()},
                             headers=person)
                return a, p.rsplit("/", 1)[1]

            not_run_key = getattr(nac_mgmt, "NOT_RUN", "not_run")
            base = {"server": "box-a", "op": "drain_queue", "stdout": ""}
            for label, shape, recorded in (
                    ("no way to take an approval",
                     {**base, "exit": 126, "stderr": "refused: drain_queue on 'box-a' — no factor",
                      not_run_key: "no_way"}, "not run: no_way"),
                    ("refused at the person's device",
                     {**base, "exit": 126, "stderr": "refused: drain_queue on 'box-a' — denied",
                      not_run_key: "denied"}, "not run: denied"),
                    ("already done", {**base, "exit": 0, "stderr": "", not_run_key: "already-done"},
                     "not run: already-done"),
                    ("an error before the machine took it",
                     nac_mgmt.MgmtError(502, "no live management worker"), "not run: error 502")):
                a, ask_id = await once_for(label)
                MACHINE_STATE["next"] = shape
                await call_tool(c, access, "machine_op", a)
                state = await pool.fetchval("SELECT state FROM agent_asks WHERE id=$1", ask_id)
                last = await pool.fetchval("SELECT outcome FROM agent_calls WHERE connection_id=$1 "
                                           "ORDER BY id DESC LIMIT 1", conn_now)
                check(state == "once" and last == recorded,
                      f"nothing reached the machine ({label}): the 'once' is given back and the call is "
                      "recorded as not run", (state, last))
            # The machine took the job and did not answer in time: it may have
            # run. The 'once' stays spent, so it can never run a second time.
            a, ask_id = await once_for("took it, no answer")
            late = nac_mgmt.MgmtError(504, "'box-a' took this job and had not answered")
            late.reached = True
            MACHINE_STATE["next"] = late
            await call_tool(c, access, "machine_op", a)
            state = await pool.fetchval("SELECT state FROM agent_asks WHERE id=$1", ask_id)
            last = await pool.fetchval("SELECT outcome FROM agent_calls WHERE connection_id=$1 "
                                       "ORDER BY id DESC LIMIT 1", conn_now)
            n = len(RAN)
            again = await call_tool(c, access, "machine_op", a)
            check(state == "spent" and last == "sent (once), no answer: error 504"
                  and (again.get("error") or {}).get("code") == -32042 and len(RAN) == n,
                  "the machine took it and did not answer: the 'once' stays spent (the same call asks "
                  "again) and the record says it was sent", (state, last))

            # An ask filed while the Host could run the op, opened after its
            # grant was taken back: nothing to approve.
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "prune_images", "args": []})
            ppath = urlsplit(out["error"]["data"]["elicitations"][0]["url"]).path
            page = (await c.get(ppath, headers=person)).text
            GATE_DENY.add(("box-a", "prune_images"))
            r = await c.get(ppath, headers=person)
            check(r.status_code == 409 and "may not run" in r.text and 'name="form"' not in r.text,
                  "the approval page of an op the Host may no longer run offers no approval", r.text[-300:])
            r = await c.post(ppath, data={"form": form_token(page), "decision": "once", "code": codes.next()},
                             headers=person)
            check(await pool.fetchval("SELECT state FROM agent_asks WHERE id=$1",
                                      ppath.rsplit("/", 1)[1]) == "waiting",
                  "and a yes sent from the page opened before is not taken", r.status_code)
            GATE_DENY.clear()
            # The real gate, on this schema with no signed grant: run_mgmt_op's
            # refusal carries the marker the agent path reads, and gate_op (the
            # agent path's check) says no for the same reason.
            real = await REAL_RUN_MGMT_OP(pool, ENTITY, "box-a", "reboot", [], origin="agent")
            gate = await REAL_GATE_OP(pool, ENTITY, "box-a", "reboot")
            check(real.get("exit") == 126 and real.get("refused_by") == getattr(nac_mgmt, "REFUSED_BY_HOST", "?")
                  and real.get(getattr(nac_mgmt, "NOT_RUN", "not_run")) == "host"
                  and gate[0] is False and gate[1] in real.get("stderr", ""),
                  "the real pod gate's refusal is marked refused_by the Host, and gate_op gives its reason",
                  (real, gate))
            out = spent_out

            # ── always, refuse, host-only ──
            link = out["error"]["data"]["elicitations"][0]["url"]
            p2 = urlsplit(link).path
            page = (await c.get(p2, headers=person)).text
            r = await c.post(p2, data={"form": form_token(page), "decision": "always", "code": codes.next()},
                             headers=person)
            check(r.status_code == 200, "the person allows it always for this agent", r.text[-200:])
            n = len(RAN)
            a1 = await call_tool(c, access, "machine_op", args)
            a2 = await call_tool(c, access, "machine_op", args)
            check("result" in a1 and "result" in a2 and len(RAN) == n + 2,
                  "'always' runs it again and again without asking")
            grants = await pool.fetch("SELECT kind, server, op FROM agent_grants WHERE revoked_at IS NULL")
            check({(g["kind"], g["server"], g["op"]) for g in grants} == {("read", "*", "*"),
                                                                         ("op", "box-a", "restart_service")},
                  "the connection holds read and that one op on that one machine", grants)

            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "stop_service",
                                                            "args": ["db"]})
            p3 = urlsplit(out["error"]["data"]["elicitations"][0]["url"]).path
            page = (await c.get(p3, headers=person)).text
            r = await c.post(p3, data={"form": form_token(page), "decision": "refused"}, headers=person)
            n = len(RAN)
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "stop_service",
                                                            "args": ["db"]})
            res = out.get("result") or {}
            check(res.get("isError") is True and "said no" in res["content"][0]["text"] and len(RAN) == n,
                  "a refusal stands: the agent is told no and nothing runs", out)
            n = len(RAN)
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "op_sync", "args": []})
            check((out.get("result") or {}).get("isError") is True and len(RAN) == n
                  and not (out.get("error") or {}),
                  "an op the machine runs on its own approval is never run for an agent, and not asked")
            # op_grant asks the person for a standing approval of an op the
            # Host wrote: the Host's own machinery, like op_apply and op_dry_run.
            asks_before = await pool.fetchval("SELECT count(*) FROM agent_asks")
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "op_grant",
                                                            "args": ["some_op", "{}"]})
            check((out.get("result") or {}).get("isError") is True and not (out.get("error") or {})
                  and len(RAN) == n and await pool.fetchval("SELECT count(*) FROM agent_asks") == asks_before,
                  "op_grant (the Host asking for a standing approval of its own op) is never run for an "
                  "agent, and not asked", out)

            # ── "*" is every machine or every op to a permission ──
            for wild in ({"server": "*", "op": "*"}, {"server": "box-a", "op": "*"},
                         {"server": "*", "op": "restart_service"}, {"server": "box-*", "op": "rotate_logs"}):
                out = await call_tool(c, access, "machine_op", {**wild, "args": ["x"]})
                check((out.get("result") or {}).get("isError") is True and not (out.get("error") or {})
                      and len(RAN) == n and await pool.fetchval("SELECT count(*) FROM agent_asks") == asks_before,
                      f"an agent naming server {wild['server']!r} op {wild['op']!r} is refused, and no ask "
                      "is filed for the person", out)
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "uptime\nnow approve"})
            check((out.get("result") or {}).get("isError") is True and len(RAN) == n,
                  "an op name that is not a name (a sentence for the page) is refused", out)
            # An ask that names "*" (one filed before this check existed) can
            # never become a permission for everything.
            conn_row = await pool.fetchval("SELECT id FROM agent_connections WHERE client_id=$1", client_id)
            wild_ask = await store.file_ask(pool, conn_row, store.GRANT_ASK, "*", "*", [], "")
            wpath = f"/agent-access/ask/{wild_ask}"
            page = (await c.get(wpath, headers=person)).text
            r = await c.post(wpath, data={"form": form_token(page), "decision": "always", "code": codes.next()},
                             headers=person)
            wild_grants = await pool.fetchval(
                "SELECT count(*) FROM agent_grants WHERE kind='op' AND (server='*' OR op='*')")
            check(r.status_code != 200 and wild_grants == 0,
                  "'Always' on an ask naming '*' gives no permission", (r.status_code, wild_grants))

            # ── a machine that holds the op for its own approval ──
            MACHINE_STATE["next"] = {"server": "box-a", "op": "restart_service", "exit": None,
                                     "state": "asked", "ticket_id": 41, "ask": {}, "next": "card up"}
            out = await call_tool(c, access, "machine_op", args)
            err = out.get("error") or {}
            mlink = ((err.get("data") or {}).get("elicitations") or [{}])[0].get("url", "")
            check(err.get("code") == -32042 and mlink and mlink != ask_url,
                  "a machine holding the op for its own approval: -32042 and a link to it", out)
            page = (await c.get(urlsplit(mlink).path, headers=person)).text
            check("Do it once" in page and "box-a asks to run restart_service once" in page
                  and 'name="code"' not in page,
                  "its page shows the Host's card, answered as the chat's pop-up answers it")
            r = await c.post(urlsplit(mlink).path, data={"form": form_token(page), "decision": "once"},
                             headers=person)
            check(r.status_code == 200 and DECIDED[-1] == (41, "once", "user"),
                  "the person's answer decides that card, as the person", DECIDED[-1:])

            # ── a 'once' given back when the machine holds the op ──
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "rotate_logs", "args": []})
            p4 = urlsplit(out["error"]["data"]["elicitations"][0]["url"]).path
            page = (await c.get(p4, headers=person)).text
            await c.post(p4, data={"form": form_token(page), "decision": "once", "code": codes.next()},
                         headers=person)
            MACHINE_STATE["next"] = {"server": "box-a", "op": "rotate_logs", "exit": None,
                                     "state": "pending_approval", "request": {"via": "device"},
                                     "next": "waiting for your approval on your device"}
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "rotate_logs", "args": []})
            check((out.get("error") or {}).get("code") == -32042, "the machine waits on the device: a link")
            out = await call_tool(c, access, "machine_op", {"server": "box-a", "op": "rotate_logs", "args": []})
            check("result" in out and RAN[-1][1] == "rotate_logs",
                  "the 'once' was given back, so the call after the device's tap runs", out)

            # ── refresh, revoke ──
            r = await c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                                   "client_id": client_id})
            t2 = r.json()
            check(r.status_code == 200 and t2.get("access_token", "").startswith("agt_"),
                  "a refresh token gets a new pair", t2)
            r = await rpc(c, t2["access_token"], "tools/list")
            check(r.status_code == 200, "the new access token works", r.status_code)
            r = await c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                                   "client_id": client_id})
            check(r.status_code == 400, "and is rotated: the old one is refused")
            # Presented again after rotation, the old refresh token means two
            # parties hold it: the pair it was rotated into ends too, so a
            # copied token dies the moment the real agent next refreshes.
            r = await rpc(c, t2["access_token"], "tools/list")
            check(r.status_code == 401, "a rotated refresh token presented again ends the access token it "
                                        "was rotated into", r.status_code)
            r = await c.post("/oauth/token", data={"grant_type": "refresh_token",
                                                   "refresh_token": t2.get("refresh_token", ""),
                                                   "client_id": client_id})
            check(r.status_code == 400, "and the refresh token it was rotated into", r.text)
            conn_id = await pool.fetchval("SELECT id FROM agent_connections WHERE client_id=$1", client_id)
            live = await pool.fetchrow("SELECT connected_at, revoked_at FROM agent_connections WHERE id=$1", conn_id)
            check(live["connected_at"] is not None and live["revoked_at"] is None
                  and await pool.fetchval("SELECT count(*) FROM agent_grants WHERE connection_id=$1 "
                                          "AND revoked_at IS NULL", conn_id) == 2,
                  "while the connection and its permissions stay: the agent signs in again")

            # ── connecting again with the box clear takes the read back ──
            verifier, challenge = pkce()
            q.update(code_challenge=challenge, state="st-3")
            tok = form_token((await c.get("/oauth/authorize", params=q, headers=person)).text)
            r = await c.post("/oauth/authorize", data={"form": tok, "decision": "approve", "code": codes.next()},
                             headers=person)
            code = parse_qs(urlsplit(r.headers.get("location", "")).query).get("code", [""])[0]
            r = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": REDIRECT, "client_id": client_id,
                                                   "code_verifier": verifier})
            t3 = r.json()
            out = await call_tool(c, t3.get("access_token", ""), "machines", {})
            check((out.get("result") or {}).get("isError") is True
                  and await pool.fetchval("SELECT count(*) FROM agent_grants WHERE connection_id=$1 "
                                          "AND kind='read' AND revoked_at IS NULL", conn_id) == 0,
                  "an agent connected again without 'see your machines' ticked no longer sees them", out)

            # ── a spent code presented again ends every token issued from it ──
            r = await c.post("/oauth/token", data={"grant_type": "refresh_token",
                                                   "refresh_token": t3.get("refresh_token", ""),
                                                   "client_id": client_id})
            t4 = r.json()
            check(r.status_code == 200 and (await rpc(c, t4.get("access_token", ""), "tools/list")).status_code == 200,
                  "(the pair from that code refreshes as usual)", t4)
            r = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": REDIRECT, "client_id": client_id,
                                                   "code_verifier": verifier})
            check(r.status_code == 400 and r.json().get("error") == "invalid_grant",
                  "a spent authorization code presented again gets no token", r.text)
            dead = [(await rpc(c, t.get("access_token", ""), "tools/list")).status_code for t in (t3, t4)]
            r = await c.post("/oauth/token", data={"grant_type": "refresh_token",
                                                   "refresh_token": t4.get("refresh_token", ""),
                                                   "client_id": client_id})
            check(dead == [401, 401] and r.status_code == 400,
                  "and every token issued from that code, through its refreshes, is revoked "
                  "(RFC 6749 §4.1.2)", (dead, r.status_code))

            verifier, challenge = pkce()
            q.update(code_challenge=challenge, state="st-5")
            tok = form_token((await c.get("/oauth/authorize", params=q, headers=person)).text)
            r = await c.post("/oauth/authorize", data={"form": tok, "decision": "approve", "code": codes.next()},
                             headers=person)
            code5 = parse_qs(urlsplit(r.headers.get("location", "")).query).get("code", [""])[0]
            t5 = (await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code5,
                                                     "redirect_uri": REDIRECT, "client_id": client_id,
                                                     "code_verifier": verifier})).json()
            check((await rpc(c, t5.get("access_token", ""), "tools/list")).status_code == 200,
                  "a fresh sign-in after that works")
            check(await store.revoke_connection(pool, ENTITY, conn_id), "the person disconnects the agent")
            r = await rpc(c, t5.get("access_token", ""), "tools/list")
            check(r.status_code == 401, "and its token stops at once", r.status_code)

            # ── the owner's passkey on both pages ──
            # On the desktop's own address (home_origin.HOME): an owner passkey
            # is for one address, and the page offers it only there.
            from server import owner_login, owner_passkeys
            from server.agent_access import pages as agent_pages
            from server.home_origin import HOME
            home = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOME.origin)
            r = await home.post("/oauth/register", json={"client_name": "pk agent", "redirect_uris": [REDIRECT]})
            pk_client = r.json().get("client_id", "")
            pk_verifier, pk_challenge = pkce()
            pq = {"response_type": "code", "client_id": pk_client, "redirect_uri": REDIRECT,
                  "code_challenge": pk_challenge, "code_challenge_method": "S256", "state": "pk"}
            r = await home.get("/oauth/authorize", params={**pq, "resource": HOME.origin + "/mcp"}, headers=person)
            csp = r.headers.get("content-security-policy", "")
            check(r.status_code == 200 and "data-passkey" not in r.text and "<script" not in r.text
                  and "script-src" not in csp and 'name="code"' in r.text,
                  "with no owner passkey, the connect page takes the code and runs no script", csp)

            made: list[bytes] = []  # the passkeys this section adds, and removes
            made.append(os.urandom(16))
            await pool.execute("INSERT INTO owner_passkeys (credential_id, public_key, rp_id) "
                               "VALUES ($1, $2, $3)", made[-1], b"\x00", HOME.host)
            r = await c.get("/oauth/authorize", params={**pq, "resource": "http://host.test/mcp"}, headers=person)
            check(r.status_code == 200 and "data-passkey" not in r.text,
                  "a passkey made for the desktop's address is not offered on another address")
            r = await home.get("/oauth/authorize", params={**pq, "resource": HOME.origin + "/mcp"}, headers=person)
            page, csp = r.text, r.headers.get("content-security-policy", "")
            nonce = (re.search(r"script-src 'nonce-([^']+)'", csp) or [None, ""])[1]
            check(bool(nonce) and f'<script nonce="{nonce}">' in page and page.count("<script") == 1
                  and "connect-src 'self'" in csp and "default-src 'none'" in csp,
                  "with an owner passkey, the page carries one script, allowed by its nonce alone", csp)
            check("data-passkey" in page and "Use a code instead" in page
                  and "/api/owner/passkey/stepup/options" in agent_pages.PASSKEY_SCRIPT
                  and "/api/owner/passkey/stepup/verify" in agent_pages.PASSKEY_SCRIPT,
                  "the passkey comes first (the owner's own step-up check); the code stands behind "
                  "'Use a code instead'")
            from server.middleware.localhost_only import origin_allowed
            check('<meta name="referrer" content="same-origin">' in page and "no-referrer" not in page
                  and not origin_allowed("null", urlsplit(HOME.origin).netloc, own_origin=True),
                  "the page's own post carries its Origin (referrer policy same-origin): under "
                  "no-referrer a browser posts the form with Origin: null, which the localhost gate "
                  "refuses")
            tok = form_token(page)

            # A real owner and a live session on the desktop's address: the
            # passkey check is recorded on the live session the request carries.
            await owner_login.init()
            await owner_login.create_owner(owner_login._setup_code, PERSON, "invented pass phrase 7731", "")
            # (An owner passkey exists by now, so the password no longer signs in:
            # the session is made as every way of signing in makes it.)
            session = await owner_login._new_session(pool, HOME.host)
            owner_passkeys._step_ups.clear()
            cookie_name = owner_login.cookie_name(HOME.host)
            jar = {cookie_name: session}
            r = await home.post("/oauth/authorize", data={"form": tok, "decision": "approve", "read": "1"},
                                headers=person, cookies=jar)
            check(r.status_code == 403 and r.headers.get("location") is None,
                  "no passkey check and no code: nothing is connected")
            owner_passkeys._note_step_up(session, owner_passkeys.PASSKEY)
            r = await home.post("/oauth/authorize", data={"form": tok, "decision": "approve", "read": "1"},
                                headers=person, cookies={jar_name: "another-session" for jar_name in jar})
            check(r.status_code == 403, "another session's passkey check confirms nothing here")
            check("Approve with your passkey" in r.text and "That code was not right" not in r.text,
                  "a yes with no code on a page that offers the passkey says the passkey is missing, "
                  "never that a code was wrong", r.text[-300:])
            r = await home.post("/oauth/authorize", data={"form": tok, "decision": "approve", "read": "1"},
                                headers={**person, "Cookie": f"{cookie_name}=an-old-cookie; {cookie_name}={session}"})
            check(r.status_code == 303 and r.headers.get("location", "").startswith(REDIRECT),
                  "an old cookie of the same name sent ahead of the live session does not hide the "
                  "passkey check that session made", (r.status_code, r.text[-300:]))
            r = await home.post("/oauth/authorize", data={"form": tok, "decision": "approve", "read": "1"},
                                headers=person, cookies=jar)
            loc = r.headers.get("location", "")
            check(r.status_code == 303 and loc.startswith(REDIRECT)
                  and parse_qs(urlsplit(loc).query).get("code", [""])[0].startswith("agc_"),
                  "this session's passkey check from the last five minutes connects the agent",
                  (r.status_code, r.text[-300:]))

            await pool.execute("DELETE FROM kernel_secrets WHERE id = $1", nac_stepup._TOTP_ENROLLED_ID)
            owner_passkeys._step_ups.clear()
            r = await home.get("/oauth/authorize", params={**pq, "resource": HOME.origin + "/mcp"}, headers=person)
            page = r.text
            check("data-passkey" in page and 'name="code"' not in page,
                  "with an owner passkey and no code, the page asks for the passkey alone")
            r = await home.post("/oauth/authorize", data={"form": form_token(page), "decision": "approve"},
                                headers=person, cookies=jar)
            check(r.status_code == 403 and "passkey" in r.text and "Network app" not in r.text,
                  "and a yes without the check says to approve with the passkey", r.text[-300:])
            await pool.execute("DELETE FROM owner_passkeys WHERE credential_id = ANY($1::bytea[])", made)
            r = await home.get("/oauth/authorize", params={**pq, "resource": HOME.origin + "/mcp"}, headers=person)
            check("/passkey page" in r.text and "data-passkey" not in r.text,
                  "with neither, the page says where to add a passkey")

            # On the remote address (home_origin.REMOTE): only its own passkeys,
            # and no /passkey page there (that page is the desktop's).
            from server import home_origin
            os.environ.pop(home_origin.REMOTE_URL_ENV, None)
            home_origin.set_remote(f"https://{HOME.handle}.k7x2m9.remote.example")
            far = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url=home_origin.REMOTE.origin)
            r = await far.get("/oauth/authorize", params={**pq, "resource": home_origin.REMOTE.origin + "/mcp"}, headers=person)
            check(r.status_code == 200 and "/passkey" not in r.text and "Remote access" in r.text,
                  "on the remote address with neither, the page names the way to a passkey there, "
                  "never the desktop's /passkey page", r.text[-400:])
            made.append(os.urandom(16))
            await pool.execute("INSERT INTO owner_passkeys (credential_id, public_key, rp_id) "
                               "VALUES ($1, $2, $3)", made[-1], b"\x00", HOME.host)
            r = await far.get("/oauth/authorize", params={**pq, "resource": home_origin.REMOTE.origin + "/mcp"}, headers=person)
            check("data-passkey" not in r.text and "/passkey" not in r.text,
                  "a passkey made for the desktop is not offered on the remote address")
            made.append(os.urandom(16))
            await pool.execute("INSERT INTO owner_passkeys (credential_id, public_key, rp_id) "
                               "VALUES ($1, $2, $3)", made[-1], b"\x00", home_origin.REMOTE.host)
            r = await far.get("/oauth/authorize", params={**pq, "resource": home_origin.REMOTE.origin + "/mcp"}, headers=person)
            check("data-passkey" in r.text
                  and "script-src 'nonce-" in r.headers.get("content-security-policy", ""),
                  "the remote address's own passkey is offered there")
            await pool.execute("DELETE FROM owner_passkeys WHERE credential_id = ANY($1::bytea[])", made)
            home_origin.set_remote(None)
            await far.aclose()
            await nac_stepup.mark_enrolled(pool)
            await home.aclose()

            # ── an agent on this computer: 127.0.0.1, the connect page on the desktop ──
            # An installed Host's desktop is plain http on a *.localhost name;
            # MCP clients send a token request over plain http only to
            # localhost, 127.0.0.1 or ::1. So an agent uses 127.0.0.1 with the
            # desktop's port, and only its connect page is the desktop's own,
            # where the person is signed in and their passkey works.
            from server.agent_access import origin as agent_origin
            loop_origin = f"http://127.0.0.1:{HOME.port or 80}"
            lo = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=loop_origin)
            home = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOME.origin)
            r = await rpc(lo, None, "initialize")
            check(r.status_code == 401 and f'resource_metadata="{loop_origin}/.well-known/' in
                  r.headers.get("www-authenticate", ""),
                  "on 127.0.0.1, /mcp's 401 names 127.0.0.1's metadata", r.headers.get("www-authenticate"))
            pr = (await lo.get("/.well-known/oauth-protected-resource/mcp")).json()
            md = (await lo.get("/.well-known/oauth-authorization-server")).json()
            check(pr.get("resource") == loop_origin + "/mcp" and pr.get("authorization_servers") == [loop_origin]
                  and md.get("issuer") == loop_origin
                  and all(md.get(k, "").startswith(loop_origin + "/oauth/")
                          for k in ("token_endpoint", "registration_endpoint", "revocation_endpoint")),
                  "on 127.0.0.1 the resource, the issuer and the token, registration and revocation "
                  "endpoints stay on 127.0.0.1 (a client sends its token request there)", (pr, md))
            check(md.get("authorization_endpoint") == HOME.origin + "/oauth/authorize",
                  "and the connect page is the desktop's own, where the person is signed in",
                  md.get("authorization_endpoint"))
            r = await lo.post("/oauth/register", json={"client_name": "Loop agent", "redirect_uris": [REDIRECT]})
            lo_client = r.json().get("client_id", "")
            lo_verifier, lo_challenge = pkce()
            lq = {"response_type": "code", "client_id": lo_client, "redirect_uri": REDIRECT,
                  "code_challenge": lo_challenge, "code_challenge_method": "S256", "state": "lo-1",
                  "resource": loop_origin + "/mcp", "scope": "machines"}
            r = await c.get("/oauth/authorize", params=lq, headers=person)
            check(r.status_code == 400 and r.headers.get("location") is None,
                  "127.0.0.1's resource is taken only on the desktop's own connect page", r.status_code)
            lport = HOME.port or 80
            near_misses = [f"http://127.0.0.1:{lport + 1}/mcp", f"http://LOCALHOST:{lport}/mcp",
                           f"https://127.0.0.1:{lport}/mcp", f"http://127.0.0.1:{lport}/mcp/",
                           f"http://127.0.0.1:{lport}", f"http://[0:0:0:0:0:0:0:1]:{lport}/mcp",
                           f"http://127.0.0.2:{lport}/mcp", f"http://127.0.0.1.evil.example:{lport}/mcp"]
            took = [x for x in near_misses
                    if (await home.get("/oauth/authorize", params={**lq, "resource": x},
                                       headers=person)).status_code != 400]
            check(not took, "the desktop's connect page takes the loopback resources exactly: another "
                  "port, scheme, spelling or path of them is refused", took)
            r = await home.get("/oauth/authorize", params=lq, headers=person)
            outside, box = split_agent_words(r.text)
            check(r.status_code == 200 and "Loop agent" in box and "127.0.0.1:33418" in outside,
                  "the desktop's connect page takes it, and still names the agent and where its code goes",
                  (r.status_code, r.text[-400:]))
            r = await home.post("/oauth/authorize", data={"form": form_token(r.text), "decision": "approve",
                                                          "read": "1", "code": codes.next()}, headers=person)
            back = parse_qs(urlsplit(r.headers.get("location", "")).query)
            check(r.status_code == 303 and back.get("iss") == [loop_origin] and back.get("state") == ["lo-1"],
                  "the code goes back with iss = the issuer the client found (127.0.0.1's, RFC 9207)",
                  r.headers.get("location"))
            lo_code = back.get("code", [""])[0]
            tq = {"grant_type": "authorization_code", "code": lo_code, "redirect_uri": REDIRECT,
                  "client_id": lo_client, "code_verifier": lo_verifier}
            r = await lo.post("/oauth/token", data={**tq, "resource": HOME.origin + "/mcp"})
            check(r.status_code == 400 and r.json().get("error") == "invalid_target",
                  "a token request naming another address than the one connected gets nothing (RFC 8707)",
                  r.text)
            # (that attempt spent the code: connect again)
            lo_verifier, lo_challenge = pkce()
            lq.update(code_challenge=lo_challenge, state="lo-2")
            r = await home.get("/oauth/authorize", params=lq, headers=person)
            r = await home.post("/oauth/authorize", data={"form": form_token(r.text), "decision": "approve",
                                                          "read": "1", "code": codes.next()}, headers=person)
            lo_code = parse_qs(urlsplit(r.headers.get("location", "")).query).get("code", [""])[0]
            r = await lo.post("/oauth/token", data={**tq, "code": lo_code, "code_verifier": lo_verifier,
                                                    "resource": loop_origin + "/mcp"})
            lt = r.json()
            check(r.status_code == 200 and lt.get("access_token", "").startswith("agt_"),
                  "the token request on 127.0.0.1 gets the tokens", r.text)
            r = await rpc(lo, lt.get("access_token"), "tools/list")
            check(r.status_code == 200 and "machine_op" in r.text, "and /mcp on 127.0.0.1 takes them", r.text)
            await rpc(lo, lt.get("access_token"), "initialize",
                      {"protocolVersion": "2025-11-25", "capabilities": {"elicitation": {"url": {}}}})
            out = await call_tool(lo, lt.get("access_token"), "machine_op",
                                  {"server": "box-a", "op": "restart_service", "args": ["loop"]})
            llink = ((out.get("error") or {}).get("data") or {}).get("elicitations", [{}])[0].get("url", "")
            check(llink.startswith(HOME.origin + "/agent-access/ask/"),
                  "the approval link a 127.0.0.1 agent gets is the desktop's own page, where the person "
                  "is signed in", out)
            r = await rpc(home, lt.get("access_token"), "tools/list")
            check(r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", ""),
                  "a token for 127.0.0.1 is not taken on another address of this Host (its audience)",
                  r.status_code)
            r = await lo.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": lo_client,
                                                    "refresh_token": lt.get("refresh_token", ""),
                                                    "resource": "https://elsewhere.example/mcp"})
            check(r.status_code == 400 and r.json().get("error") == "invalid_target",
                  "a refresh naming another address gets nothing", r.text)
            r = await lo.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": lo_client,
                                                    "refresh_token": lt.get("refresh_token", ""),
                                                    "resource": loop_origin + "/mcp"})
            lt2 = r.json()
            check(r.status_code == 200 and (await rpc(lo, lt2.get("access_token"), "tools/list")).status_code == 200
                  and (await rpc(home, lt2.get("access_token"), "tools/list")).status_code == 401,
                  "and the refreshed token is for 127.0.0.1 alone, like the first", r.text)
            info = (await home.get("/mcp")).json()
            check(info.get("address") == loop_origin + "/mcp" and loop_origin + "/mcp" in info.get("note", "")
                  and agent_origin.agent_address(HOME.origin) == loop_origin + "/mcp",
                  "the desktop's /mcp page and Settings name 127.0.0.1's address for agents", info)

            # ── the remote (https) address works as before, on its own ──
            os.environ.pop(home_origin.REMOTE_URL_ENV, None)
            home_origin.set_remote(f"https://{HOME.handle}.k7x2m9.remote.example")
            far_origin = home_origin.REMOTE.origin
            far = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=far_origin)
            pr = (await far.get("/.well-known/oauth-protected-resource/mcp")).json()
            md = (await far.get("/.well-known/oauth-authorization-server")).json()
            check(pr.get("resource") == far_origin + "/mcp" and md.get("issuer") == far_origin
                  and all(md.get(k, "").startswith(far_origin + "/oauth/")
                          for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint",
                                    "revocation_endpoint")),
                  "on the https remote address every endpoint stays on it, as before", md)
            r = await far.post("/oauth/register", json={"client_name": "Far agent", "redirect_uris": [REDIRECT]})
            far_client = r.json().get("client_id", "")
            f_verifier, f_challenge = pkce()
            fq = {"response_type": "code", "client_id": far_client, "redirect_uri": REDIRECT,
                  "code_challenge": f_challenge, "code_challenge_method": "S256", "state": "far",
                  "resource": far_origin + "/mcp"}
            r = await far.get("/oauth/authorize", params={**fq, "resource": loop_origin + "/mcp"}, headers=person)
            check(r.status_code == 400, "the remote address's connect page does not take 127.0.0.1's resource")
            r = await far.get("/oauth/authorize", params=fq, headers=person)
            r = await far.post("/oauth/authorize", data={"form": form_token(r.text), "decision": "approve",
                                                         "read": "1", "code": codes.next()}, headers=person)
            back = parse_qs(urlsplit(r.headers.get("location", "")).query)
            r = await far.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": far_client,
                                                     "code": back.get("code", [""])[0], "redirect_uri": REDIRECT,
                                                     "code_verifier": f_verifier, "resource": far_origin + "/mcp"})
            ft = r.json()
            check(back.get("iss") == [far_origin] and r.status_code == 200
                  and (await rpc(far, ft.get("access_token"), "tools/list")).status_code == 200,
                  "and the whole sign-in runs there: iss is its own, its token works on it", (back, r.text))
            check((await rpc(far, lt2.get("access_token"), "tools/list")).status_code == 401
                  and (await rpc(lo, ft.get("access_token"), "tools/list")).status_code == 401,
                  "a token for 127.0.0.1 is refused on the remote address, and the remote one on 127.0.0.1")
            check((await far.get("/mcp")).json().get("address") == far_origin + "/mcp"
                  and agent_origin.agent_address(far_origin) == far_origin + "/mcp",
                  "on the remote address the address for agents is the remote address itself")
            # The address a token is for lives on its code's row. The schema
            # lets that row be pruned (v255: from_code has no foreign key): a
            # token whose code row is gone is taken on no address and its
            # refresh renews nothing, never read as bound to none.
            ft_code = await pool.fetchval("SELECT from_code FROM agent_tokens WHERE token_hash=$1",
                                          store._sha(ft.get("access_token", "")))
            await pool.execute("DELETE FROM agent_codes WHERE code_hash=$1", ft_code)
            seen = [(await rpc(cl, ft.get("access_token"), "tools/list")).status_code for cl in (far, lo, home)]
            r = await far.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": far_client,
                                                     "refresh_token": ft.get("refresh_token", "")})
            check(seen == [401, 401, 401] and r.status_code == 400 and r.json().get("error") == "invalid_grant",
                  "a token whose code's row is gone is taken on no address, and its refresh renews nothing",
                  (seen, r.text))
            # From before this Host required the resource: a code issued with
            # none, tokens from such a code, and tokens from before tokens
            # named their code. None of them binds an address, so none
            # survives the upgrade: the code redeems nothing, the tokens are
            # taken on no address, and their refresh renews nothing.
            far_conn = await pool.fetchval("SELECT id FROM agent_connections WHERE client_id=$1", far_client)
            old_v, old_ch = pkce()
            old_code, old_hash = store.new_secret(store.CODE_PREFIX)
            await pool.execute(
                "INSERT INTO agent_codes (code_hash, connection_id, redirect_uri, code_challenge, resource, "
                "expires_at) VALUES ($1, $2, $3, $4, '', now() + interval '5 minutes')",
                old_hash, far_conn, REDIRECT, old_ch)
            r = await far.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": far_client,
                                                     "code": old_code, "redirect_uri": REDIRECT,
                                                     "code_verifier": old_v})
            check(r.status_code == 400 and r.json().get("error") == "invalid_grant",
                  "a code issued without a resource (before this Host required one) redeems nothing", r.text)
            for label, from_code in (("a code issued without a resource", old_hash),
                                     ("no code at all (tokens from before they named theirs)", None)):
                async with pool.acquire() as tconn:
                    async with tconn.transaction():
                        old_t = await store._tokens(tconn, far_conn, from_code)
                seen = [(await rpc(cl, old_t["access_token"], "tools/list")).status_code for cl in (far, lo, home)]
                r = await far.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": far_client,
                                                         "refresh_token": old_t["refresh_token"]})
                check(seen == [401, 401, 401] and r.status_code == 400
                      and r.json().get("error") == "invalid_grant",
                      f"tokens from {label} are taken on no address, and their refresh renews nothing",
                      (seen, r.text))
            home_origin.set_remote(None)
            for cl in (lo, home, far):
                await cl.aclose()

            # ── the registration bound ──
            store.MAX_UNCONNECTED = 3
            codes_seen = []
            for _ in range(5):
                r = await c.post("/oauth/register", json={"client_name": "flood", "redirect_uris": [REDIRECT]})
                codes_seen.append(r.status_code)
            waiting = await pool.fetchval("SELECT count(*) FROM agent_connections WHERE entity_id=$1 "
                                          "AND connected_at IS NULL", ENTITY)
            newest = r.json().get("client_id", "")
            check(waiting <= 3 and codes_seen == [201] * 5
                  and await store.client(pool, ENTITY, newest) is not None,
                  "registrations that never connect are bounded, and a stranger who fills the bound "
                  "never stops the next registration (the oldest unconnected one goes)",
                  (codes_seen, waiting))
    finally:
        await pool.close()
    return 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=ENTITY) as _dsn:
    rc = asyncio.run(main(_dsn))
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"an outside agent's path through the Host: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
