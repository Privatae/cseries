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


nac_mgmt.run_mgmt_op = fake_run_mgmt_op
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
                               ({"resource": "https://elsewhere.example/mcp"}, "another server's resource")):
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
