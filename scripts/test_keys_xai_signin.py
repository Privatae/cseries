#!/usr/bin/env python3
"""Gate: "Sign in with xAI" from the Keys app (2026-10-05).

A user whose only subscription is Grok has no model, so no Host to ask
"connect Grok". The Keys app gets a real button for that one sign-in, wired to
the login the Host's harness_connections tool already drives
(review.harness_login / harness_login_finish). This pins, stub-only (no
Postgres, no pods, no vendor calls):

  1. POST /api/vault/providers/{provider}/signin and /signin/finish accept xai
     only. Every other provider is a 404 that starts nothing. Both need
     write:vault, the scope the Keys app declares, and refuse a token without
     it.
  2. start and finish hand the work to review.harness_login /
     harness_login_finish (stubbed) with harness 'grok', and answer in a state
     plus plain words: waiting (url, user_code), signed_in, failed, expired,
     unreachable. A call that outlasts the router's budget answers 202 + a job
     read at GET /api/vault/jobs/{id} under the same scope.
  3. a finished sign-in that the vault reads back routes chat to xai ONLY
     when chat has no route at all: the first recommended-routes.json xai chat
     candidate the xAI catalog serves, set_by='user'. An existing chat route
     is never touched.
  4. nothing in the sign-in path reads the xAI API key, and the sign-in token
     appears in no response.
  5. the Keys app, run under node against the real /api/vault/providers
     payload: the xai slot is a "Sign in with xAI" button, every other sign-in
     slot is the chip it was; the click opens the vendor link, polls finish,
     follows a 202 job, and ends on "Signed in with xAI" (+ "Your Host now
     runs on <model>" when chat was routed). Errors are sentences; closing the
     window or the 15-minute limit stops the polling.
  6. the shipped Keys bundles carry the button.

Run: APP_ROOT=<repo> python3 scripts/test_keys_xai_signin.py
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import types
import zipfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))
logging.disable(logging.CRITICAL)

failures: list = []


def check(label, ok, detail=""):
    print(f"[keys-signin] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


SIGNIN = "at-SIGNIN-keys-4c1d"
KEY = "xai-KEY-keys-9e2a"
GROK_URL = "https://accounts.x.ai/device?user_code=QWER-7788"


def _grok_bytes(delta_s: int = 3600) -> bytes:
    t = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delta_s)
    return json.dumps({"https://auth.x.ai::abc": {
        "expires_at": t.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
        "refresh_token": "rt", "key": SIGNIN}}).encode()


def _keys_permissions() -> list:
    """The scopes the Keys app actually declares — read from its manifest, so
    the route checks run against what a Keys window token carries."""
    perms, on = [], False
    for line in (_ROOT / "system-apps/keys/app.yaml").read_text().splitlines():
        if re.match(r"^permissions:\s*$", line):
            on = True
            continue
        if on:
            m = re.match(r"^\s+-\s+([\w:.-]+)\s*$", line)
            if m:
                perms.append(m.group(1))
            elif line and not line.startswith((" ", "#")):
                break
    return perms


def _xai_chat_candidates() -> list:
    """Read the data file directly, not through the code under test."""
    data = json.loads((_ROOT / "system-apps/orchestrator/recommended-routes.json").read_text())
    return list((((data.get("providers") or {}).get("xai") or {}).get("roles") or {})
                .get("chat", {}).get("candidates") or [])


def _xai_standard() -> dict:
    """xAI model id -> its entry in the Host model standard
    (brain/mastercode/runtime/approved_models.json), read directly, in the
    file's order."""
    data = json.loads((_ROOT / "brain/mastercode/runtime/approved_models.json").read_text())
    out: dict = {}
    for e in data.get("models") or []:
        c = str((e or {}).get("canonical") or "")
        if c.startswith("xai/"):
            out[c.split("/", 1)[1]] = e
    return out


def _eligible(entry: dict) -> bool:
    """Tests 1 and 2 of the standard: status 'eligible' with a linked card."""
    return (entry or {}).get("status") == "eligible" and bool((entry or {}).get("card"))


def main() -> int:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from server import brain_service
    from server.applications import long_request as LR
    from server.network import harness_state as hs
    from server.routes import inference as inf
    from server.routes import review
    from server.routes import vault as V
    from server.vault import api as vault_api
    from server.vault import providers as pv
    import mastercode.inference_config as IC
    from mastercode.runtime import web_research as WR

    slots = hs.oauth_login_slots()
    grok_name = (slots.get("xai") or {}).get("vault_name") or "harness:grok:auth"
    keys_perms = _keys_permissions()
    cands = _xai_chat_candidates()
    check("recommended-routes.json has an xai chat row to route on", bool(cands), repr(cands))

    # ── the world the stubs serve ────────────────────────────────────
    rows: list = []              # vault rows list_secrets returns
    sealed: dict = {}            # name -> bytes open_secret returns
    keys: dict = {"xai": KEY}    # a key IS held, so any read of it is recorded
    key_reads: list = []
    login_calls: list = []
    finish_calls: list = []
    login_script: dict = {}
    finish_script: list = []
    chat_route: dict = {}
    catalog: dict = {}
    applied: list = []
    apply_raise: list = []

    async def _list_secrets(pool, *, entity_id, **k):
        return list(rows)

    async def _open(pool, *, entity_id, name, actor=None, include_deactivated=False):
        return sealed.get(name)

    async def _cred(pool, eid, provider, cred_type="api_key", **kw):
        if provider == "xai" and cred_type == "api_key":
            key_reads.append("resolve_credential")
        return keys.get(provider) if cred_type == "api_key" else None

    async def _resolve(pool, eid, provider, accepted=("api_key",), *a, **k):
        if provider == "xai" and "api_key" in accepted:
            key_reads.append("resolve")
        sec = keys.get(provider) if "api_key" in accepted else None
        return pv.ResolvedCred(provider, provider, "api_key", sec, "vault") if sec else None

    async def _login(req):
        login_calls.append(req)
        if login_script.get("sleep"):
            await asyncio.sleep(login_script["sleep"])
        if login_script.get("raise"):
            raise login_script["raise"]
        return dict(login_script.get("out") or {})

    async def _finish(req):
        finish_calls.append(req)
        nxt = (finish_script.pop(0) if len(finish_script) > 1
               else (finish_script[0] if finish_script else {}))
        if isinstance(nxt, Exception):
            raise nxt
        return dict(nxt)

    async def _route(pool, eid, role, **kw):
        assert role == "chat"
        return dict(chat_route)

    async def _direct_models(provider):
        if catalog.get("err"):
            return [], catalog["err"]
        return [{"id": m} for m in catalog.get("ids", [])], None

    async def _apply(body, *, set_by="user", decision_id=None, rejected=None):
        applied.append((body, set_by))
        if apply_raise:
            raise apply_raise[0]
        return {"set": True, "role": body.role, "provider": body.provider,
                "model": body.model, "set_by": set_by}

    class P:
        async def fetchrow(self, *a, **k):
            return None

    saved = (vault_api.list_secrets, vault_api.open_secret, pv.resolve_credential,
             pv.resolve, review.harness_login, review.harness_login_finish,
             IC.resolve_role_route, inf._direct_models, inf.apply_role_route,
             brain_service.get_pool, os.environ.get("ENTITY_ID"), LR.BUDGET_S)
    vault_api.list_secrets, vault_api.open_secret = _list_secrets, _open
    pv.resolve_credential, pv.resolve = _cred, _resolve
    review.harness_login, review.harness_login_finish = _login, _finish
    IC.resolve_role_route = _route
    inf._direct_models = _direct_models
    inf.apply_role_route = _apply
    brain_service.get_pool = lambda: P()
    os.environ["ENTITY_ID"] = "7"

    app = FastAPI()

    @app.middleware("http")
    async def _token(request: Request, call_next):
        perms = request.headers.get("x-test-perms")
        if perms is not None:
            request.state.app_token = types.SimpleNamespace(
                app_id=request.headers.get("x-test-app") or "keys",
                permissions=[p for p in perms.split(",") if p],
                origin_key="")
        return await call_next(request)

    app.include_router(V.router)
    KEYS = {"x-test-perms": ",".join(keys_perms)}
    bodies: list = []
    payloads: dict = {}

    def reset(*, signed=False, route=None, ids=None, err=None):
        rows.clear(); sealed.clear(); key_reads.clear(); login_calls.clear()
        finish_calls.clear(); login_script.clear(); finish_script.clear()
        applied.clear(); apply_raise.clear(); chat_route.clear(); catalog.clear()
        if signed:
            sealed[grok_name] = _grok_bytes()
        chat_route.update(route or {"provider": "", "model": "",
                                    "unconfigured": "no chat route"})
        if err:
            catalog["err"] = err
        else:
            catalog["ids"] = list(ids if ids is not None else cands)

    def post(client, path, headers=KEYS):
        r = client.post(path, headers=headers, json={})
        bodies.append(r.text)
        return r

    try:
        with TestClient(app) as client:
            # ── 1. which providers, which scope ──────────────────────
            reset()
            login_script["out"] = {"harness": "grok", "ok": True, "url": GROK_URL, "done": False}
            r_x = post(client, "/api/vault/providers/xai/signin")
            check("start: xai is accepted (200) with the Keys app's own scopes",
                  r_x.status_code == 200, f"{r_x.status_code} {r_x.text[:200]}")
            refused = {}
            for p in ("anthropic", "openai", "google", "kimi", "openrouter",
                      "deepinfra", "nope"):
                login_calls.clear(); finish_calls.clear()
                a = post(client, f"/api/vault/providers/{p}/signin")
                b = post(client, f"/api/vault/providers/{p}/signin/finish")
                refused[p] = (a.status_code, b.status_code, len(login_calls), len(finish_calls))
            check("start/finish: every provider but xai is a 404 that starts nothing "
                  "(Anthropic and OpenAI stay out) — while xai is served",
                  r_x.status_code == 200
                  and all(v == (404, 404, 0, 0) for v in refused.values()), repr(refused))
            reset()
            login_script["out"] = {"harness": "grok", "ok": True, "url": GROK_URL, "done": False}
            r_up = post(client, "/api/vault/providers/XAI/signin")
            check("start: the provider id is matched case-blind ('XAI' is xai)",
                  r_up.status_code == 200 and r_up.json().get("provider") == "xai",
                  f"{r_up.status_code} {r_up.text[:160]}")
            r_none = client.post("/api/vault/providers/xai/signin", json={})
            r_read = post(client, "/api/vault/providers/xai/signin",
                          headers={"x-test-perms": "read:vault"})
            r_fin = post(client, "/api/vault/providers/xai/signin/finish",
                         headers={"x-test-perms": "read:vault"})
            check("scope: no token -> 401; read:vault only -> 403 on start and finish; "
                  "the Keys app declares write:vault",
                  r_none.status_code == 401 and r_read.status_code == 403
                  and r_fin.status_code == 403 and "write:vault" in keys_perms,
                  f"{r_none.status_code} {r_read.status_code} {r_fin.status_code} {keys_perms}")
            # Starting a sign-in is the user's own act in Keys: it holds the
            # metered Grok pod for up to 15 minutes and can set the chat route
            # (a write:inference_routing change). Every other app that holds
            # write:vault (read from the manifests, not listed here) is refused
            # on all three routes and starts nothing.
            others = sorted(
                d.name for d in (_ROOT / "system-apps").iterdir()
                if d.name != "keys" and (d / "app.yaml").is_file()
                and re.search(r"^\s+-\s+write:vault\s*$", (d / "app.yaml").read_text(), re.M))
            reset()
            login_script["out"] = {"harness": "grok", "ok": True, "url": GROK_URL, "done": False}
            denied = {}
            for app_id in others:
                hdr = {"x-test-perms": "read:vault,write:vault", "x-test-app": app_id}
                login_calls.clear(); finish_calls.clear()
                a = post(client, "/api/vault/providers/xai/signin", headers=hdr)
                b = post(client, "/api/vault/providers/xai/signin/finish", headers=hdr)
                c = client.get("/api/vault/jobs/anything", headers=hdr)
                denied[app_id] = (a.status_code, b.status_code, c.status_code,
                                  len(login_calls), len(finish_calls))
            check("scope: another app holding write:vault cannot start, check or read a "
                  "sign-in (403, nothing started) — only the Keys app can",
                  bool(others) and all(v == (403, 403, 403, 0, 0) for v in denied.values()),
                  repr(denied))

            # ── 2. start delegates and answers in words ──────────────
            reset()
            login_script["out"] = {"harness": "grok", "ok": True, "url": GROK_URL,
                                   "done": False, "output": "open " + GROK_URL}
            r = post(client, "/api/vault/providers/xai/signin")
            j = r.json() if r.status_code == 200 else {}
            check("start: one call to review.harness_login with harness 'grok'",
                  len(login_calls) == 1 and isinstance(login_calls[0], review.HarnessLoginReq)
                  and login_calls[0].harness == "grok", repr(login_calls))
            check("start: answers waiting + the vendor URL; no user_code when the URL "
                  "carries it; the vendor's raw output is not passed on",
                  j.get("state") == "waiting" and j.get("url") == GROK_URL
                  and j.get("ok") is True and j.get("done") is False
                  and not j.get("user_code") and "output" not in j, repr(j))
            reset()
            login_script["out"] = {"harness": "grok", "ok": True, "url": GROK_URL,
                                   "user_code": "QWER-7788", "done": False}
            r = post(client, "/api/vault/providers/xai/signin")
            j = r.json() if r.status_code == 200 else {}
            check("start: a one-time code rides along as user_code",
                  j.get("state") == "waiting" and j.get("user_code") == "QWER-7788", repr(j))
            reset()
            login_script["raise"] = HTTPException(503, "could not start grok: no url")
            r = post(client, "/api/vault/providers/xai/signin")
            j = r.json() if r.status_code == 200 else {}
            check("start: the Grok pod not starting -> 200, state failed, a sentence "
                  "naming xAI (not the pod's error text)",
                  r.status_code == 200 and j.get("state") == "failed" and j.get("ok") is False
                  and "xAI" in (j.get("error") or "") and "no url" not in (j.get("error") or ""),
                  f"{r.status_code} {r.text[:200]}")
            reset()
            login_script["out"] = {"harness": "grok", "ok": False, "done": True,
                                   "error": "grok login exited (code 1) without writing a grant"}
            r = post(client, "/api/vault/providers/xai/signin")
            j = r.json() if r.status_code == 200 else {}
            check("start: the vendor ending without a grant -> failed, in words",
                  j.get("state") == "failed" and "xAI" in (j.get("error") or "")
                  and "exited" not in (j.get("error") or ""), repr(j))
            reset()
            login_script["out"] = {"harness": "grok", "ok": True, "done": False,
                                   "url": "javascript:alert(1)"}
            r = post(client, "/api/vault/providers/xai/signin")
            j = r.json() if r.status_code == 200 else {}
            check("start: a link that is not https is never handed on",
                  r.status_code == 200 and j.get("state") == "failed" and not j.get("url"),
                  f"{r.status_code} {r.text[:200]}")

            # a start that outlasts the budget -> 202 + a job under write:vault
            reset()
            login_script.update(sleep=0.4, out={"harness": "grok", "ok": True,
                                                "url": GROK_URL, "done": False})
            LR.BUDGET_S = 0.05
            r = post(client, "/api/vault/providers/xai/signin")
            pend = r.json() if r.status_code == 202 else {}
            poll = str(pend.get("poll") or "")
            check("start: slower than the router allows -> 202 + a job at /api/vault/jobs/",
                  r.status_code == 202 and pend.get("pending") is True
                  and poll.startswith("/api/vault/jobs/"), f"{r.status_code} {r.text[:200]}")
            got: dict = {}
            low = 0
            if poll:
                for _ in range(200):
                    rr = client.get(poll, headers=KEYS)
                    got = rr.json() if rr.status_code == 200 else {"http": rr.status_code}
                    if got.get("status") != "running":
                        break
                    time.sleep(0.02)
                low = client.get(poll, headers={"x-test-perms": "read:vault"}).status_code
            check("job: the result lands on the job (state waiting + url); reading it "
                  "needs write:vault, like the start",
                  got.get("status") == "done"
                  and (got.get("result") or {}).get("state") == "waiting"
                  and (got.get("result") or {}).get("url") == GROK_URL
                  and low == 403, f"{got} {low}")
            LR._JOBS["foreign-job"] = {"what": "x", "area": "circles", "status": "running"}
            r_other = client.get("/api/vault/jobs/foreign-job", headers=KEYS)
            LR._JOBS.pop("foreign-job", None)
            check("job: another area's job is not readable here (404) — while this "
                  "area's job was",
                  r_other.status_code == 404 and got.get("status") == "done",
                  f"{r_other.status_code} {r_other.text[:120]}")
            LR.BUDGET_S = saved[11]

            # ── 2b. finish delegates and answers in words ────────────
            def finish(*answers):
                finish_script.clear(); finish_script.extend(answers)
                r = post(client, "/api/vault/providers/xai/signin/finish")
                return r.json() if r.status_code == 200 else {"http": r.status_code,
                                                              "text": r.text[:200]}

            reset()
            j = finish({"harness": "grok", "ok": True, "done": False, "login_waiting": True})
            check("finish: one call to review.harness_login_finish with harness 'grok'; "
                  "still waiting -> state waiting",
                  len(finish_calls) == 1 and finish_calls[0].harness == "grok"
                  and isinstance(finish_calls[0], review.HarnessLoginReq)
                  and j.get("state") == "waiting" and j.get("done") is False, repr(j))
            reset()
            j = finish({"harness": "grok", "ok": True, "done": False, "login_waiting": False,
                        "error": "no login in flight"})
            check("finish: no login waiting any more -> expired, in words",
                  j.get("state") == "expired" and "start again" in (j.get("error") or "").lower()
                  and "in flight" not in (j.get("error") or ""), repr(j))
            reset()
            j = finish({"harness": "grok", "done": False, "ok": False,
                        "error": "login status check failed: timeout"})
            check("finish: the pod not answering this once -> unreachable (keep checking)",
                  j.get("state") == "unreachable" and "timeout" not in (j.get("error") or ""),
                  repr(j))
            reset()
            j = finish(HTTPException(503, "could not reach grok: no url"))
            check("finish: the pod not waking this once -> unreachable, not an error page",
                  j.get("state") == "unreachable", repr(j))
            reset()
            j = finish({"harness": "grok", "ok": False, "done": True,
                        "error": "grok login exited (code 1) without writing a grant"})
            check("finish: the vendor ending without a grant -> failed, in words",
                  j.get("state") == "failed" and "xAI" in (j.get("error") or "")
                  and not applied, repr(j))
            reset()
            j = finish({"harness": "grok", "ok": False, "done": False, "supported": False,
                        "error": "grok's running sidecar has no login-status route"})
            check("finish: a sidecar that cannot report back -> failed, in words",
                  j.get("state") == "failed" and "sidecar" not in (j.get("error") or "")
                  and bool(j.get("error")), repr(j))

            # ── 3. a finished sign-in, and the chat route ────────────
            served = cands[1:] if len(cands) > 1 else cands
            want = served[0] if served else None
            reset(signed=True, ids=served)
            j = finish({"harness": "grok", "ok": True, "done": True, "login_waiting": False,
                        "persisted": True})
            ap = applied[0] if applied else (types.SimpleNamespace(role=None, provider=None,
                                                                   model=None), None)
            check("finish: done + the vault reads the sign-in back -> signed_in",
                  j.get("state") == "signed_in" and j.get("ok") is True
                  and j.get("done") is True, repr(j))
            check("auto-route: chat had NO route -> routed to xai on the first "
                  "recommended-routes.json candidate the xAI catalog serves, set_by='user'",
                  len(applied) == 1 and ap[0].role == "chat" and ap[0].provider == "xai"
                  and want is not None and ap[0].model == want and ap[1] == "user"
                  and (j.get("routed") or {}).get("model") == want
                  and (j.get("routed") or {}).get("provider") == "xai"
                  and (j.get("routed") or {}).get("role") == "chat"
                  and (j.get("routed") or {}).get("verified") is True,
                  repr((applied, j.get("routed"), want)))
            # The auto-route picks only a model the Host model standard lists
            # ELIGIBLE and the catalog serves on the sign-in. Feed it every
            # ineligible xAI model AHEAD of the eligible ones, all served: the
            # ineligible ones must be skipped however early they are listed.
            std = _xai_standard()
            elig = [m for m, e in std.items() if _eligible(e)]
            inelig = [m for m, e in std.items() if not _eligible(e)]
            mixed = inelig + elig
            saved_rc = WR.recommended_chat_candidates
            WR.recommended_chat_candidates = (
                lambda p: list(mixed) if p == "xai" else saved_rc(p))
            try:
                reset(signed=True, ids=mixed)
                j = finish({"harness": "grok", "ok": True, "done": True, "persisted": True})
                check("auto-route: ineligible models listed first and served are skipped; the "
                      "first ELIGIBLE one the catalog serves is routed",
                      bool(elig) and bool(inelig) and len(applied) == 1
                      and applied[0][0].model == elig[0]
                      and (j.get("routed") or {}).get("model") == elig[0],
                      repr((mixed, [a[0].model for a in applied], j.get("routed"))))
                reset(signed=True, ids=inelig)
                j = finish({"harness": "grok", "ok": True, "done": True, "persisted": True})
                check("auto-route: the catalog serves only ineligible models -> nothing routed, "
                      "still signed in, and the note says the model was not set",
                      not applied and j.get("state") == "signed_in" and j.get("routed") is None
                      and "not set" in (j.get("route_note") or ""), repr((applied, j)))
                reset(signed=True, err="catalog unreachable")
                j = finish({"harness": "grok", "ok": True, "done": True, "persisted": True})
                check("auto-route: catalog unreachable -> nothing routed (never an unverified "
                      "pick), and the note says so",
                      not applied and j.get("state") == "signed_in" and j.get("routed") is None
                      and "not set" in (j.get("route_note") or ""), repr((applied, j)))
            finally:
                WR.recommended_chat_candidates = saved_rc
            check("recommended-routes.json: the xai chat row is the xAI models the standard "
                  "lists eligible, highest score first (no pending-card or below-floor model)",
                  bool(elig) and cands == sorted(
                      elig, key=lambda m: -float(std[m].get("aa_score") or 0)),
                  repr((cands, elig)))
            reset(signed=True, route={"provider": "anthropic", "model": "m-1"})
            j = finish({"harness": "grok", "ok": True, "done": True, "persisted": True})
            check("auto-route: chat ALREADY routed -> nothing written, routed null",
                  j.get("state") == "signed_in" and not applied
                  and "routed" in j and j["routed"] is None, repr((applied, j)))
            reset(signed=True)
            apply_raise.append(HTTPException(409, "grok-x is not an approved Host model"))
            j = finish({"harness": "grok", "ok": True, "done": True, "persisted": True})
            check("auto-route: the writer refusing -> still signed in, routed null, the "
                  "reason in words",
                  j.get("state") == "signed_in" and "routed" in j and j["routed"] is None
                  and "approved" in (j.get("route_note") or ""), repr(j))
            reset(signed=False)
            j = finish({"harness": "grok", "ok": True, "done": True, "persisted": False})
            check("finish: the vendor done but the vault does NOT read a sign-in back -> "
                  "failed, nothing routed",
                  j.get("state") == "failed" and not applied
                  and "xAI" in (j.get("error") or ""), repr(j))
            reset(signed=True)
            login_script["out"] = {"harness": "grok", "ok": True, "done": True,
                                   "persisted": True}
            r = post(client, "/api/vault/providers/xai/signin")
            j = r.json() if r.status_code == 200 else {}
            check("start: the vendor finishing inside the start (already signed in) -> "
                  "signed_in, and the same chat rule applies",
                  j.get("state") == "signed_in" and len(applied) == 1
                  and (j.get("routed") or {}).get("model") == (cands[0] if cands else None),
                  repr((j, applied)))

            # ── 4. no key, no token ──────────────────────────────────
            # key_reads is cleared per reset(); collect across the whole run
            # by re-running every shape above once more and summing.
            all_reads: list = []
            for shape in (
                lambda: (reset(), login_script.update(out={"harness": "grok", "ok": True,
                                                           "url": GROK_URL, "done": False}),
                         post(client, "/api/vault/providers/xai/signin")),
                lambda: (reset(signed=True), finish({"harness": "grok", "ok": True,
                                                     "done": True, "persisted": True})),
                lambda: (reset(signed=False), finish({"harness": "grok", "ok": True,
                                                      "done": True})),
                lambda: (reset(), finish({"harness": "grok", "ok": True, "done": False,
                                          "login_waiting": True})),
            ):
                shape()
                all_reads.extend(key_reads)
            check("nothing in the sign-in path reads the xAI API key (one is held, and "
                  "any read of it is recorded)",
                  not all_reads and len(applied) == 0 and len(finish_calls) == 1,
                  repr(all_reads))
            check("the sign-in token appears in no response",
                  bodies and not any(SIGNIN in (b or "") for b in bodies), "leaked")

            # ── payload: the flag, xai only ──────────────────────────
            # stored = a live sign-in; expired / unreadable = the two
            # resolve_signin markers a Host's model call stops on.
            sealed_for = {"stored": _grok_bytes(3600), "expired": _grok_bytes(-3600),
                          "unreadable": json.dumps({"https://auth.x.ai::abc": {
                              "refresh_token": "rt"}}).encode(),
                          "off": _grok_bytes(3600)}
            for state in ("none", "stored", "expired", "unreadable", "off"):
                rows.clear(); sealed.clear()
                if state != "none":
                    rows.append({"logical_name": grok_name, "cred_type": "harness_grok",
                                 "created_at": _dt.datetime.now(_dt.timezone.utc),
                                 "deactivated_at": (_dt.datetime.now(_dt.timezone.utc)
                                                    if state == "off" else None)})
                    sealed[grok_name] = sealed_for[state]
                rp = client.get("/api/vault/providers", headers=KEYS)
                payloads[state] = rp.json() if rp.status_code == 200 else {}

            def xai_slot(state):
                for p in (payloads.get(state) or {}).get("providers", []):
                    if p["provider"] == "xai":
                        return next((s for s in p["slots"] if s.get("source") == "host"), {})
                return {}

            want_slots = {"none": ("none", "signin"), "stored": ("live", "reconnect"),
                          "expired": ("expired", "reconnect"),
                          "unreadable": ("unreadable", "reconnect"), "off": ("off", "signin")}
            got_slots = {st: (xai_slot(st).get("signin_state"), xai_slot(st).get("signin_action"))
                         for st in want_slots}
            check("payload: the xAI sign-in slot always carries its button — Sign in when "
                  "none or off, Reconnect when stored (live, expired or unreadable)",
                  got_slots == want_slots, repr(got_slots))
            # Every sentence that sends the user to Keys names the button Keys
            # shows in that state, in the slot's own words (signin_label).
            said = {"none": pv.no_credential_message("xai"),
                    "off": pv.no_credential_message("xai"),
                    "expired": pv.signin_expired_message("xai"),
                    "unreadable": pv.signin_unreadable_message("xai"),
                    "stored": pv.signin_refused_message("xai", 401)}
            mismatch = {st: (xai_slot(st).get("signin_label"), msg) for st, msg in said.items()
                        if not xai_slot(st).get("signin_label")
                        or xai_slot(st)["signin_label"] not in msg or "Keys app" not in msg}
            check("messages: no sign-in / switched off, expired, unreadable and refused each "
                  "name the exact button Keys shows in that state",
                  not mismatch, repr(mismatch)[:600])
            # The way onto the API key while signed in: the sign-in's own row
            # in the Keys Vault section (shown by its stored name, with
            # Deactivate — the Keys render below proves both).
            off_step = f"Deactivate on {grok_name} in the Vault section of the Keys app"
            no_step = {st: msg for st, msg in said.items()
                       if st in ("expired", "unreadable", "stored") and off_step not in msg}
            check("messages: the way onto the API key names the row and button Keys shows "
                  f"({off_step!r})", not no_step, repr(no_step)[:600])
            flagged = {p["provider"]: [s for s in p["slots"] if s.get("signin_from_keys")]
                       for p in payloads["none"].get("providers", [])}
            host_slots = {p["provider"] for p in payloads["none"].get("providers", [])
                          for s in p["slots"] if s.get("source") == "host"}
            check("payload: ONLY xai's sign-in slot carries signin_from_keys (vendor "
                  "'xAI'); every other host sign-in slot does not",
                  [p for p, s in flagged.items() if s] == ["xai"]
                  and flagged["xai"][0].get("vendor") == "xAI"
                  and len(host_slots) > 1, repr((flagged, host_slots)))
    finally:
        (vault_api.list_secrets, vault_api.open_secret, pv.resolve_credential,
         pv.resolve, review.harness_login, review.harness_login_finish,
         IC.resolve_role_route, inf._direct_models, inf.apply_role_route,
         brain_service.get_pool) = saved[:10]
        if saved[10] is None:
            os.environ.pop("ENTITY_ID", None)
        else:
            os.environ["ENTITY_ID"] = saved[10]
        LR.BUDGET_S = saved[11]

    _keys_app_checks(payloads, cands, grok_name)
    _bundle_checks()

    print(f"[keys-signin] {'PASS' if not failures else 'FAIL'} ({len(failures)} failing)")
    return 1 if failures else 0


# ── 5. the Keys app itself, under node ───────────────────────────────────
# Runs the real app.js against a stub DOM and a stub fetch: what it RENDERS
# from the real /api/vault/providers payload, and what one click on the
# button does, end to end.
_NODE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const scen = JSON.parse(process.argv[2]);
const realSetTimeout = setTimeout;
const realNow = Date.now;
const tick = () => new Promise(r => realSetTimeout(r, 0));
async function until(cond, n) { for (let i = 0; i < (n || 3000); i++) { if (cond()) return true; await tick(); } return !!cond(); }

class El {
  constructor(id) { this.id = id; this._html = ''; this.textContent = ''; this.hidden = false;
    this.disabled = false; this.value = ''; this.className = ''; this.children = [];
    this._l = {}; this._attrs = {}; this.history = [];
    this.classList = { add() {}, remove() {}, toggle() {} }; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); if (v === '') this.children = []; this.history.push(this._html); }
  addEventListener(t, fn) { (this._l[t] = this._l[t] || []).push(fn); }
  appendChild(c) { this.children.push(c); return c; }
  querySelector() { return null; } querySelectorAll() { return []; }
  focus() {} getAttribute(n) { return n in this._attrs ? this._attrs[n] : null; }
  setAttribute(n, v) { this._attrs[n] = String(v); } removeAttribute(n) { delete this._attrs[n]; }
  closest() { return null; }
}

function attrsOf(tagHtml) {
  const out = {}; const re = /([\w-]+)="([^"]*)"/g; let m;
  while ((m = re.exec(tagHtml))) out[m[1]] = m[2].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  return out;
}

async function runOne(s) {
  const els = {}; const clicks = []; const fetches = []; const delays = []; const alerts = [];
  let now = 1.7e12; let finishN = 0; let jobN = 0;
  global.window = {};
  global.document = {
    getElementById(id) { return els[id] || (els[id] = new El(id)); },
    createElement(tag) { return new El(tag); },
    addEventListener(t, fn) { if (t === 'click') clicks.push(fn); },
  };
  global.navigator = {};
  global.alert = (m) => alerts.push(String(m));
  global.setTimeout = (fn, ms) => { delays.push(ms || 0); return realSetTimeout(fn, 0); };
  Date.now = () => now;
  const reply = (status, body) => Promise.resolve({ ok: status >= 200 && status < 300, status,
      json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
  global.fetch = (url, opts) => {
    const method = (opts && opts.method) || 'GET';
    fetches.push(method + ' ' + url);
    if (url === '/api/vault/secrets') return reply(200, { secrets: s.secrets || [] });
    if (url === '/api/vault/providers') return reply(200, s.providers);
    if (url === '/api/browser/forms') return reply(200, { sites: [] });
    if (url === '/api/browser/forms/grants') return reply(200, { grants: [] });
    if (/\/signin$/.test(url) && method === 'POST') { const a = s.start || [200, {}]; return reply(a[0], a[1]); }
    if (/\/signin\/finish$/.test(url) && method === 'POST') {
      const list = s.finish || [[200, { state: 'waiting' }]];
      const a = list[Math.min(finishN, list.length - 1)]; finishN += 1;
      if (s.advance_on_finish) now += s.advance_on_finish;
      if (s.close_after_finish && finishN === s.close_after_finish) {
        ((els['keys-modal-close'] || new El('x'))._l.click || []).forEach(fn => fn({}));
      }
      return reply(a[0], a[1]);
    }
    if (url.indexOf('/api/vault/jobs/') === 0) { const list = s.jobs || [[200, { status: 'running' }]];
      const a = list[Math.min(jobN, list.length - 1)]; jobN += 1; return reply(a[0], a[1]); }
    return reply(404, { detail: 'no stub for ' + url });
  };
  (new Function(src))();
  // keys-view is created on the app's first getElementById, after load().
  const view = () => els['keys-view'];
  await until(() => view() && /keys-provider/.test(view().innerHTML));
  if (s.section) {
    // The left menu: one click on the section's [data-section] item.
    const nav = new El('nav'); nav._attrs = { 'data-section': s.section };
    clicks.forEach(fn => fn({ target: { closest(sel) { return sel === '[data-section]' ? nav : null; } } }));
    await tick();
  }
  const out = { view: view() ? view().innerHTML : '', modal: [], actions: [], fetches, delays, alerts, title: '' };
  if (s.click) {
    const tag = (out.view.match(/<button[^>]*data-act="signin"[^>]*>/) || [''])[0];
    const a = attrsOf(tag);
    const btn = new El('btn'); btn._attrs = a;
    const target = { closest(sel) { return sel === '[data-act]' && tag ? btn : null; } };
    clicks.forEach(fn => fn({ target }));
    const body = () => els['keys-modal-body'];
    await until(() => body() && (s.until ? new RegExp(s.until).test(body().innerHTML) : false), s.ticks || 3000);
    for (let i = 0; i < 50; i++) await tick();     // let any stray loop show itself
    out.modal = body() ? body().history : [];
    out.title = els['keys-modal-title'] ? els['keys-modal-title'].textContent : '';
    const act = els['keys-modal-actions'];
    out.actions = act ? act.children.map(c => c.textContent) : [];
    out.finish_calls = finishN;
  }
  Date.now = realNow; global.setTimeout = realSetTimeout;
  return out;
}

(async () => {
  const results = {};
  for (const [name, s] of Object.entries(scen)) {
    try { results[name] = await runOne(s); } catch (e) { results[name] = { error: String(e && e.stack || e) }; }
  }
  process.stdout.write(JSON.stringify(results));
})();
"""


def _esc_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def _keys_app_checks(payloads: dict, cands: list, grok_name: str = "harness:grok:auth") -> None:
    node = shutil.which("node")
    if not node:
        check("Keys app: node is on PATH to run the Keys app", False, "no node")
        return
    if not payloads.get("none"):
        check("Keys app: the real /api/vault/providers payload was read", False, "none")
        return
    app_js = str(_ROOT / "system-apps/keys/app.js")
    model = (cands[1] if len(cands) > 1 else (cands[0] if cands else "m-1"))
    waiting = {"provider": "xai", "state": "waiting", "ok": True, "done": False,
               "url": GROK_URL, "user_code": "QWER-7788"}
    signed = {"provider": "xai", "state": "signed_in", "ok": True, "done": True,
              "routed": {"role": "chat", "provider": "xai", "model": model, "verified": True}}
    scen = {
        "render": {"providers": payloads["none"]},
        "stored": {"providers": payloads.get("stored") or {"providers": []}},
        "expired_slot": {"providers": payloads.get("expired") or {"providers": []}},
        "unreadable_slot": {"providers": payloads.get("unreadable") or {"providers": []}},
        "off": {"providers": payloads.get("off") or {"providers": []}},
        # The Vault section, holding the sealed sign-in row the way GET
        # /api/vault/secrets lists it.
        "vault_view": {"providers": payloads.get("stored") or {"providers": []},
                       "section": "vault",
                       "secrets": [{"name": grok_name, "cred_type": "harness_grok",
                                    "created_at": "2026-10-05T00:00:00Z",
                                    "deactivated": False}]},
        # A Reconnect click runs the same dialog as Sign in.
        "reconnect_flow": {"providers": payloads.get("expired") or {"providers": []},
                           "click": True, "start": [200, waiting],
                           "finish": [[200, signed]], "until": "Signed in with xAI"},
        "flow": {"providers": payloads["none"], "click": True,
                 # the start outlasts the router: 202 + a job, then the answer
                 "start": [202, {"pending": True, "job": "j1",
                                 "poll": "/api/vault/jobs/j1", "message": "starting"}],
                 "jobs": [[200, {"status": "running"}],
                          [200, {"status": "done", "result": waiting}]],
                 "finish": [[200, {"provider": "xai", "state": "waiting"}],
                            [200, {"provider": "xai", "state": "unreachable",
                                   "error": "Your Host could not reach the xAI sign-in "
                                            "just now."}],
                            [200, signed]],
                 "until": "Signed in with xAI"},
        "already_routed": {"providers": payloads["none"], "click": True,
                           "start": [200, waiting],
                           "finish": [[200, {**signed, "routed": None}]],
                           "until": "Signed in with xAI"},
        "expired": {"providers": payloads["none"], "click": True,
                    "start": [200, waiting],
                    "finish": [[200, {"provider": "xai", "state": "expired",
                                      "error": "That xAI sign-in is no longer waiting. "
                                               "Start again for a fresh link."}]],
                    "until": "no longer waiting"},
        "failed_start": {"providers": payloads["none"], "click": True,
                         "start": [200, {"provider": "xai", "state": "failed", "ok": False,
                                         "error": "Your Host could not start the xAI "
                                                  "sign-in just now. Try again in a minute."}],
                         "until": "could not start"},
        "bad_link": {"providers": payloads["none"], "click": True,
                     "start": [200, {**waiting, "url": "javascript:alert(1)"}],
                     "until": "did not give", "ticks": 600},
        "timeout": {"providers": payloads["none"], "click": True,
                    "start": [200, waiting],
                    "finish": [[200, {"provider": "xai", "state": "waiting"}]],
                    "advance_on_finish": 16 * 60 * 1000,
                    "until": "timed out"},
        "closed": {"providers": payloads["none"], "click": True,
                   "start": [200, waiting],
                   "finish": [[200, {"provider": "xai", "state": "waiting"}]],
                   "close_after_finish": 2, "until": "^$", "ticks": 600},
    }
    try:
        out = subprocess.run([node, "-e", _NODE, app_js, json.dumps(scen)],
                             capture_output=True, text=True, timeout=120)
        res = json.loads(out.stdout) if out.returncode == 0 and out.stdout else {}
        err = out.stderr[-600:]
    except Exception as e:  # noqa: BLE001
        res, err = {}, repr(e)
    if not res:
        check("Keys app: ran under node", False, err)
        return
    for k, v in res.items():
        if v.get("error"):
            check(f"Keys app: scenario {k} ran", False, v["error"][:400])

    def last(name):
        m = (res.get(name) or {}).get("modal") or [""]
        return m[-1] if m else ""

    def joined(name):
        return "\n".join((res.get(name) or {}).get("modal") or [])

    view = (res.get("render") or {}).get("view", "")
    btns = re.findall(r"<button[^>]*data-act=\"signin\"[^>]*>.*?</button>", view, re.S)
    check("Keys: the xai sign-in slot renders as a 'Sign in with xAI' button — the only one",
          len(btns) == 1 and 'data-provider="xai"' in btns[0]
          and "Sign in with xAI" in btns[0], repr(btns)[:400])
    other_names = sorted({(s.get("harness_name") or "")
                          for p in payloads["none"].get("providers", [])
                          if p["provider"] != "xai"
                          for s in p["slots"] if s.get("source") == "host"})
    check("Keys: every other sign-in slot is still the 'Connect …' chip, not a control "
          "(Claude Code, Codex, Antigravity unchanged)",
          bool(other_names) and all(f"Connect {_esc_html(n)}" in view for n in other_names)
          and view.count("keys-slot-waiting") == len(other_names), repr(other_names))
    # Each state: the chip it reads as, and the one button beside it. A sign-in
    # that is stored keeps its chip and gains Reconnect — the way back when it
    # has expired, cannot be read, or xAI refuses it (which Keys cannot see).
    for name, word, label in (("stored", "via ", "Reconnect with xAI"),
                              ("expired_slot", "Expired", "Reconnect with xAI"),
                              ("unreadable_slot", "Unreadable", "Reconnect with xAI"),
                              ("off", "Off — ", "Sign in with xAI")):
        v = (res.get(name) or {}).get("view", "")
        sb = re.findall(r"<button[^>]*data-act=\"signin\"[^>]*>.*?</button>", v, re.S)
        check(f"Keys: a {name.replace('_slot', '')} xAI sign-in reads '{word.strip()}' and "
              f"offers exactly one '{label}' button",
              word in v and len(sb) == 1 and label in sb[0]
              and 'data-provider="xai"' in sb[0], (v[:200], sb))
    vv = (res.get("vault_view") or {}).get("view", "")
    check("Keys: the Vault section shows the sign-in's row by its stored name with a "
          "Deactivate button — what the messages tell the user to press",
          f'<div class="keys-row-name">{_esc_html(grok_name)}' in vv
          and re.search(r'data-act="deactivate" data-name="' + re.escape(_esc_html(grok_name))
                        + r'">Deactivate<', vv) is not None
          and "<h2>Vault</h2>" in vv, vv[:400])
    rf = res.get("reconnect_flow") or {}
    check("click: Reconnect with xAI runs the same sign-in (start, link, poll, signed in)",
          "Signed in with xAI" in last("reconnect_flow")
          and "POST /api/vault/providers/xai/signin" in rf.get("fetches", [])
          and rf.get("finish_calls") == 1, repr((rf.get("fetches"), last("reconnect_flow"))))

    fl = res.get("flow") or {}
    seq = joined("flow")
    link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>\s*Open xAI sign-in\s*</a>', seq)
    check("click: a 202 start is followed to its job, then the dialog links the vendor "
          "page ('Open xAI sign-in', in a new tab) and shows the code",
          link is not None and link.group(1) == _esc_html(GROK_URL)
          and 'target="_blank"' in seq and "QWER-7788" in seq
          and "GET /api/vault/jobs/j1" in fl.get("fetches", []), seq[:600])
    check("click: finish is polled about every 3 s, through a passing outage, until "
          "it says signed in",
          fl.get("finish_calls") == 3 and 3000 in fl.get("delays", [])
          and "Signed in with xAI" in last("flow"),
          repr((fl.get("finish_calls"), sorted(set(fl.get("delays", []))), last("flow"))))
    check("click: chat was routed -> 'Your Host now runs on <model>'; the providers "
          "list is read again",
          f"Your Host now runs on {model}" in last("flow")
          and fl.get("fetches", []).count("GET /api/vault/providers") >= 2, last("flow"))
    check("click: chat already routed -> signed in, and no claim about the Host model",
          "Signed in with xAI" in last("already_routed")
          and "now runs on" not in last("already_routed"), last("already_routed"))
    check("click: a sign-in no longer waiting -> its sentence, and polling stops",
          "no longer waiting" in last("expired")
          and (res.get("expired") or {}).get("finish_calls") == 1, last("expired"))
    check("click: a start that failed -> its sentence, no link, no polling",
          "could not start the xAI sign-in" in last("failed_start")
          and "<a" not in joined("failed_start")
          and (res.get("failed_start") or {}).get("finish_calls") == 0, joined("failed_start"))
    check("click: a vendor link that is not https is never rendered",
          "did not give" in last("bad_link") and "javascript:" not in joined("bad_link")
          and (res.get("bad_link") or {}).get("finish_calls") == 0, joined("bad_link"))
    check("click: the 15-minute limit stops polling with a sentence",
          "timed out" in last("timeout")
          and (res.get("timeout") or {}).get("finish_calls") == 1, last("timeout"))
    check("click: closing the window stops polling",
          (res.get("closed") or {}).get("finish_calls") == 2
          and "Open xAI sign-in" in joined("closed"),
          repr((res.get("closed") or {}).get("finish_calls")))
    check("click: the dialog never names a subscription product",
          bool(seq) and not re.search(r"SuperGrok|X Premium", "\n".join(
              joined(n) for n in ("flow", "already_routed", "expired", "timeout"))), "")


# ── 6. what ships ────────────────────────────────────────────────────────
def _bundle_checks() -> None:
    for d in ("native-bundles", "factory-bundles"):
        b = _ROOT / d / "keys.cseries"
        try:
            with zipfile.ZipFile(b) as z:
                js = z.read("payload/app.js").decode("utf-8")
        except Exception as e:  # noqa: BLE001
            check(f"bundle: {d}/keys.cseries readable", False, repr(e))
            continue
        check(f"bundle: {d}/keys.cseries carries the sign-in button",
              'data-act="signin"' in js and "Sign in with " in js
              and "/signin/finish" in js, d)


if __name__ == "__main__":
    sys.exit(main())
